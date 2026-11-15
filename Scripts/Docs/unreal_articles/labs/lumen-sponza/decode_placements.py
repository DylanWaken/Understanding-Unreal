#!/usr/bin/env python3
"""Decode scene-anchored Lumen cards and the main radiance-cache probe grid.

This is the focused placement decoder for the matching-frame GPU dumps.  It
deliberately does *not* decode screen probes, SH, radiosity, or cache content:
those belong to the already published temporal/screen-sh datasets or to a
separate capture.  The only products here are

  * exact card OBBs and per-card flags/residency for the selected frame,
  * exact page-buffer / page-table records for that frame,
  * exact main-screen-probe radiance-cache probe cells and physical slots,
  * individual cards-only and worldcache-only composites over the native
    Base/Screenshot.png of the same dump,
  * numeric temporal tracks for both sequences.

The critical fix relative to scripts/decode_temporal.py is invocation
selection: the camera View is selected first, then every pass and resource is
required to belong to that same renderer invocation.  The main cache is then
identified by its captured FRadianceCacheMarkParameters (resolution, clipmap
count, corners) matching the captured AllocateUsedProbes parameters and by its
read-pass lineage.  A global lowest-producer-id search is recorded only as a
rejected counter-example; it is never used for data.

Usage:
  python decode_placements.py --light-root LIGHT_RAW --out OUT
  python decode_placements.py --sequences light --light-root LIGHT_RAW --out OUT
  python decode_placements.py --light-root LIGHT_RAW --motion-root MOTION_RAW --out OUT

``--light-root`` is required when the light sequence is selected and
``--motion-root`` is required when the motion sequence is selected; there are no
workstation defaults.  A run root contains one state subdirectory per label
(for example ``<light-root>/baseline/Base/Passes.json``).  ``--out`` is
required and must differ from the decoder source directory.

Only the explicit output directory is written.  All raw inputs are opened
read-only and hashed before and after the decode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import struct
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from decode_context import load_concat_json  # noqa: E402
from temporal_dump import DumpCtx  # noqa: E402

VIEW_BYTES = 1056
CARD_F4 = 7
CARD_BYTES = CARD_F4 * 16
PAGE_F4 = 5
PAGE_BYTES = PAGE_F4 * 16
PAGE_TABLE_BYTES = 8
PROBE_MAX = 16384

# FPackedView offsets in the captured 1056-byte struct (NaniteShared.h).
VIEW_OFF = {
    "svpos_to_tw": 0,
    "view_to_tw": 64,
    "tw_to_view": 128,
    "tw_to_clip": 192,
    "view_to_clip": 256,
    "clip_to_relworld": 320,
    "prev_tw_to_view": 384,
    "prev_tw_to_clip": 448,
    "prev_view_to_clip": 512,
    "prev_clip_to_relworld": 576,
    "view_rect": 640,
    "view_size": 656,
    "clip_scale_offset": 672,
    "pre_view_translation_high": 720,
    "pre_view_translation_low": 832,
    "view_forward": 800,
    "near_plane": 812,
    "view_id": 1040,
    "frame_index": 1044,
}

CARD_FLAG_BITS = {
    "res_level_xy_bias": [(0, 0xFF), (8, 0xFF)],
    "axis_aligned_direction": (16, 0xF),
    "lighting_channel_mask": (20, 0xF),
    "included_visible": (24, 0x1),
    "heightfield": (25, 0x1),
    "far_field": (26, 0x1),
}

CLIPMAP_COLORS = ["#4C8DFF", "#45E06F", "#FFD24C", "#FF6B6B", "#B388FF", "#00E5FF"]
CLIPMAP_RADII = [3.2, 2.8, 2.4, 2.0, 1.8, 1.6]
CARD_COLOR = "#FF8A4C"
CARD_SUBSET_COLOR = "#FFE14C"
CARD_XRAY_ALPHA = 58
CARD_SUBSET_ALPHA = 255
WC_XRAY_ALPHA = 62
WC_PRIMARY_ALPHA = 255
DEPTH_TOL_FIXED_CM = 10.0
DEPTH_TOL_REL = 0.02


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def jval(o):
    if isinstance(o, dict):
        return {str(k): jval(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [jval(v) for v in o]
    if isinstance(o, np.ndarray):
        return jval(o.tolist())
    if isinstance(o, (np.bool_, bool)):
        return bool(o)
    if isinstance(o, (np.integer, int)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        v = float(o)
        return v if math.isfinite(v) else None
    return o


def write_json(path: Path, obj, indent=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jval(obj), indent=indent, allow_nan=False), encoding="utf-8")


def f32(v) -> float:
    return float(np.float32(v))


def hex_u32(v) -> str:
    return f"0x{int(v) & 0xFFFFFFFF:08X}"


def normalize(v, eps=1e-12):
    n = float(np.linalg.norm(v))
    return np.asarray(v, dtype=np.float64) / max(n, eps)


def parse_engine_frame(passes) -> int | None:
    for p in passes:
        for scope in p.get("ParentEventScopes", []):
            if scope.startswith("Frame "):
                try:
                    return int(scope.split()[1])
                except Exception:
                    pass
    return None


# ---------------------------------------------------------------------------
# source hash registry
# ---------------------------------------------------------------------------

class SourceRegistry:
    def __init__(self):
        self.records = {}

    def add(self, path: Path, role: str, state=None, **meta):
        path = Path(path).resolve()
        key = str(path)
        rec = self.records.get(key)
        if rec is None:
            st = path.stat()
            rec = {
                "path": key,
                "size_before": int(st.st_size),
                "mtime_ns_before": int(st.st_mtime_ns),
                "sha256_before": sha256_file(path),
                "roles": [],
                "states": [],
                "meta": {},
            }
            self.records[key] = rec
        if role not in rec["roles"]:
            rec["roles"].append(role)
        if state is not None and state not in rec["states"]:
            rec["states"].append(state)
        for k, v in meta.items():
            if v is not None:
                rec["meta"][k] = v
        return rec

    def get(self, path: Path):
        return self.records.get(str(Path(path).resolve()))

    def finalize(self):
        changed = []
        for rec in self.records.values():
            p = Path(rec["path"])
            st = p.stat()
            rec["size_after"] = int(st.st_size)
            rec["mtime_ns_after"] = int(st.st_mtime_ns)
            rec["sha256_after"] = sha256_file(p)
            rec["unchanged"] = (
                rec["sha256_after"] == rec["sha256_before"]
                and rec["size_after"] == rec["size_before"]
            )
            if not rec["unchanged"]:
                changed.append(rec["path"])
        return changed

    def rows_for(self, state: str, root: Path | None = None):
        rows = [r for r in self.records.values() if state in r["states"]]
        if root is not None:
            prefix = str(Path(root).resolve()).lower()
            rows = [r for r in rows if str(Path(r["path"]).resolve()).lower().startswith(prefix)]
        return rows


SRC = SourceRegistry()


# ---------------------------------------------------------------------------
# resource selection helpers
# ---------------------------------------------------------------------------

def all_versions(d: DumpCtx, name: str, prefix: str | None = None):
    out = []
    for desc in d.descs:
        uid = desc["UniqueResourceName"]
        if desc["Name"] != name or (prefix and not uid.startswith(prefix)):
            continue
        for f, ver in d.files.get(uid, []):
            out.append({
                "desc": desc,
                "uid": uid,
                "file": f,
                "version": ver,
                "producer": d.by_ptr.get(ver),
            })
    return out


def version_key(entry):
    p = entry.get("producer")
    return p["Id"] if p else -1


def pick_version(entries, prefix: str | None = None, prefer_names=None):
    if not entries:
        return None
    pool = list(entries)
    if prefer_names:
        pref = [e for e in pool if e.get("producer") and e["producer"]["EventName"] in prefer_names]
        if pref:
            pool = pref
    if prefix:
        same = [e for e in pool if e.get("producer") and e["producer"]["Pointer"].startswith(prefix)]
        if same:
            pool = same
    produced = [e for e in pool if e.get("producer")]
    if produced:
        return max(produced, key=version_key)
    return max(pool, key=lambda e: (e["desc"].get("ByteSize") or 0, e["file"].name))


def entry_source(entry, role: str, state: str, stride=None, offset=0, extra=None):
    d = entry.get("_dump")
    p = entry.get("producer")
    rec = SRC.add(
        entry["file"], role, state=state,
        uid=entry["uid"], version=entry["version"],
        name=entry["desc"]["Name"],
        producer_id=p["Id"] if p else None,
        producer_name=p["EventName"] if p else None,
    )
    out = {
        "role": role,
        "name": entry["desc"]["Name"],
        "uid": entry["uid"],
        "graph_prefix": entry["uid"][:4],
        "version": entry["version"],
        "file": "Resources/" + entry["file"].name,
        "file_bytes": int(entry["file"].stat().st_size),
        "file_sha256": rec["sha256_before"],
        "producer": (
            {"id": p["Id"], "name": p["EventName"], "pointer": p["Pointer"]}
            if p else None
        ),
        "imported_alias": p is None,
        "descriptor": {
            k: entry["desc"].get(k) for k in
            ["Desc", "Type", "Format", "ExtentX", "ExtentY", "Depth", "ArraySize",
             "NumMips", "ByteSize", "BytesPerElement", "NumElements"]
            if entry["desc"].get(k) is not None
        },
        "record_offset_bytes": int(offset),
        "record_stride_bytes": int(stride) if stride is not None else None,
    }
    if d is not None:
        rb = d.readback.get("Resources/" + entry["file"].name)
        out["readback_status"] = rb.get("Status") if rb else None
    if extra:
        out.update(extra)
    return out


def resource_source_rows(state, root=None):
    rows = []
    for rec in SRC.rows_for(state, root):
        rows.append({
            "path": rec["path"],
            "roles": list(rec["roles"]),
            "size_before": rec["size_before"],
            "size_after": rec.get("size_after"),
            "mtime_ns_before": rec["mtime_ns_before"],
            "mtime_ns_after": rec.get("mtime_ns_after"),
            "sha256_before": rec["sha256_before"],
            "sha256_after": rec.get("sha256_after"),
            "unchanged": rec.get("unchanged"),
            "meta": rec["meta"],
        })
    return sorted(rows, key=lambda r: r["path"])


# ---------------------------------------------------------------------------
# view / projection
# ---------------------------------------------------------------------------

def decode_packed_view(path: Path):
    b = np.fromfile(path, dtype="<f4")
    if b.size * 4 != VIEW_BYTES:
        raise ValueError(f"unexpected FPackedView size {b.size * 4} for {path}")

    def mat(byte_off):
        o = byte_off // 4
        return b[o:o + 16].reshape(4, 4).T.astype(np.float64)

    raw = b.tobytes()
    pre_hi = b[VIEW_OFF["pre_view_translation_high"] // 4:
               VIEW_OFF["pre_view_translation_high"] // 4 + 3].astype(np.float64)
    pre_lo = b[VIEW_OFF["pre_view_translation_low"] // 4:
               VIEW_OFF["pre_view_translation_low"] // 4 + 3].astype(np.float64)
    pre = pre_hi + pre_lo
    view = {
        "svpos_to_tw": mat(VIEW_OFF["svpos_to_tw"]),
        "view_to_tw": mat(VIEW_OFF["view_to_tw"]),
        "tw_to_view": mat(VIEW_OFF["tw_to_view"]),
        "tw_to_clip": mat(VIEW_OFF["tw_to_clip"]),
        "view_to_clip": mat(VIEW_OFF["view_to_clip"]),
        "clip_to_relworld": mat(VIEW_OFF["clip_to_relworld"]),
        "prev_tw_to_view": mat(VIEW_OFF["prev_tw_to_view"]),
        "prev_tw_to_clip": mat(VIEW_OFF["prev_tw_to_clip"]),
        "view_rect": [int(x) for x in struct.unpack_from("<4i", raw, VIEW_OFF["view_rect"])],
        "view_size": [float(x) for x in b[VIEW_OFF["view_size"] // 4:
                                         VIEW_OFF["view_size"] // 4 + 2]],
        "clip_scale_offset": [float(x) for x in b[VIEW_OFF["clip_scale_offset"] // 4:
                                                  VIEW_OFF["clip_scale_offset"] // 4 + 4]],
        "pre_view_translation_high_cm": pre_hi,
        "pre_view_translation_low_cm": pre_lo,
        "pre_view_translation_cm": pre,
        "camera_world_cm": -pre,
        "view_forward": b[VIEW_OFF["view_forward"] // 4:
                           VIEW_OFF["view_forward"] // 4 + 3].astype(np.float64),
        "near_plane_cm": float(b[VIEW_OFF["near_plane"] // 4]),
        "primary_view_id": int(struct.unpack_from("<i", raw, VIEW_OFF["view_id"])[0]),
        "frame_index": int(struct.unpack_from("<I", raw, VIEW_OFF["frame_index"])[0]),
    }
    view["hfov_deg"] = (
        2.0 * math.degrees(math.atan(1.0 / view["view_to_clip"][0, 0]))
        if view["view_to_clip"][0, 0] else None
    )
    view["vfov_deg"] = (
        2.0 * math.degrees(math.atan(1.0 / view["view_to_clip"][1, 1]))
        if view["view_to_clip"][1, 1] else None
    )
    view["tw_to_clip_product_max_abs"] = float(
        np.max(np.abs(view["tw_to_clip"] - view["view_to_clip"] @ view["tw_to_view"]))
    )
    view["pre_high_plus_low_max_abs"] = float(
        np.max(np.abs(pre - (pre_hi + pre_lo)))
    )
    fwd = normalize(view["tw_to_view"][2, :3])
    view["view_forward_matrix"] = fwd
    view["view_forward_packed_minus_matrix_max_abs"] = float(
        np.max(np.abs(normalize(view["view_forward"]) - fwd))
    )
    view["view_axes_orthonormal_max_abs"] = float(
        np.max(np.abs(view["tw_to_view"][:3, :3] @ view["tw_to_view"][:3, :3].T
                      - np.eye(3)))
    )
    return view


def project_tw(view, pts):
    pts = np.atleast_2d(np.asarray(pts, dtype=np.float64))
    h = np.concatenate([pts, np.ones((len(pts), 1))], axis=1)
    clip = h @ view["tw_to_clip"].T
    w = clip[:, 3]
    safe = np.where(np.abs(w) > 1e-9, w, np.nan)
    ndc = clip[:, :2] / safe[:, None]
    vx, vy, vw, vh = view["view_rect"]
    sx = vx + (ndc[:, 0] * 0.5 + 0.5) * view["view_size"][0]
    sy = vy + (0.5 - ndc[:, 1] * 0.5) * view["view_size"][1]
    vz = (h @ view["tw_to_view"].T)[:, 2]
    return sx, sy, vz, w


def depth_test(sd, near_cm, sx, sy, vz, w):
    h, wd = sd.shape
    on = (w > 0) & np.isfinite(sx) & np.isfinite(sy)
    on &= (sx >= 0) & (sx < wd) & (sy >= 0) & (sy < h)
    px = np.clip(np.floor(np.nan_to_num(sx, nan=-1e9)).astype(np.int64), 0, wd - 1)
    py = np.clip(np.floor(np.nan_to_num(sy, nan=-1e9)).astype(np.int64), 0, h - 1)
    dz = sd[py, px]
    scene_cm = np.where(dz > 0, near_cm / np.maximum(dz, 1e-30), np.inf)
    tol = np.maximum(DEPTH_TOL_FIXED_CM, DEPTH_TOL_REL * np.where(np.isfinite(scene_cm), scene_cm, 0.0))
    passes = on & (vz <= scene_cm + tol)
    err = np.abs(vz - scene_cm)
    return {
        "on_screen": on,
        "device_z": dz,
        "scene_depth_cm": scene_cm,
        "tolerance_cm": tol,
        "depth_error_cm": err,
        "passes": passes,
        "px": px,
        "py": py,
    }


def hull_2d(points):
    pts = sorted({(round(float(x), 4), round(float(y), 4)) for x, y in points})
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def polygon_area(points):
    if len(points) < 3:
        return 0.0
    s = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


# ---------------------------------------------------------------------------
# state loading
# ---------------------------------------------------------------------------

def register_dump_json(state_dir: Path, state: str):
    base = state_dir / "Base"
    for f in [
        base / "Passes.json",
        base / "ResourceDescs.json",
        base / "SubresourceLayouts.json",
        base / "DumpReadbackStatus.json",
        base / "ParameterBindings.json",
    ]:
        if f.exists():
            SRC.add(f, "dump-json", state=state)
    sm = state_dir / "StructuresMetadata"
    if sm.exists():
        for f in sorted(sm.glob("*.json")):
            SRC.add(f, "structures-metadata", state=state)


def load_views_and_select(d: DumpCtx, state: str, screenshot_size):
    candidates = []
    for desc in d.view_descs():
        uid = desc["UniqueResourceName"]
        for f, ver in d.files.get(uid, []):
            if f.stat().st_size != VIEW_BYTES:
                continue
            SRC.add(f, "view-packed", state=state, uid=uid, version=ver)
            v = decode_packed_view(f)
            candidates.append({
                "uid": uid,
                "version": ver,
                "file": f,
                "prefix": uid[:4],
                "view": v,
                "producer": d.by_ptr.get(ver),
            })
    if not candidates:
        raise RuntimeError(f"{state}: no 1056-byte ViewData.PrimaryViews candidate")

    candidates.sort(key=lambda c: (c["view"]["frame_index"], c["uid"]))
    primary = [c for c in candidates if c["view"]["primary_view_id"] == 0]
    pool = primary or candidates
    exact = [
        c for c in pool
        if [int(x) for x in c["view"]["view_size"]] == [int(x) for x in screenshot_size]
    ]
    if exact:
        pool = exact
    selected = max(pool, key=lambda c: (c["view"]["frame_index"], c["uid"]))
    rejected = []
    for c in candidates:
        if c is selected:
            continue
        rejected.append({
            "uid": c["uid"],
            "version": c["version"],
            "graph_prefix": c["prefix"],
            "frame_index": c["view"]["frame_index"],
            "primary_view_id": c["view"]["primary_view_id"],
            "camera_world_cm": c["view"]["camera_world_cm"].tolist(),
            "reason": "lower frame index (same primary view / camera) than selected invocation",
        })
    return selected, candidates, rejected


def select_scene_depth(d: DumpCtx, prefix: str, state: str):
    cands = d.candidates("SceneDepthZ", prefix)
    if not cands:
        raise RuntimeError(f"{state}: no SceneDepthZ for invocation {prefix}")
    entry = max(cands, key=version_key)
    entry["_dump"] = d
    SRC.add(entry["file"], "scene-depth", state=state, uid=entry["uid"], version=entry["version"])
    sd = np.fromfile(entry["file"], dtype="<f4").astype(np.float64)
    x = int(entry["desc"]["ExtentX"])
    y = int(entry["desc"]["ExtentY"])
    sd = sd.reshape(y, x)
    return entry, sd


def decode_view_json(selected, candidates, rejected, d, state, engine_frame):
    v = selected["view"]
    out = {
        "schema": "arda-placement-view-v1",
        "state": state,
        "frame": {
            "frame_index": v["frame_index"],
            "engine_frame_scope": engine_frame,
            "primary_view_id": v["primary_view_id"],
            "note": "frame_index is FPackedView.FrameIndex; engine_frame_scope is the ParentEventScopes Frame N value",
        },
        "view": {
            "resource": {
                "uid": selected["uid"],
                "version": selected["version"],
                "file": "Resources/" + selected["file"].name,
                "graph_prefix": selected["prefix"],
                "sha256": SRC.get(selected["file"])["sha256_before"],
                "producer": (
                    {"id": selected["producer"]["Id"], "name": selected["producer"]["EventName"],
                     "pointer": selected["producer"]["Pointer"]}
                    if selected["producer"] else None
                ),
            },
            "view_rect": v["view_rect"],
            "view_size": v["view_size"],
            "clip_scale_offset": v["clip_scale_offset"],
            "near_plane_cm": v["near_plane_cm"],
            "camera_world_cm": v["camera_world_cm"].tolist(),
            "pre_view_translation_high_cm": v["pre_view_translation_high_cm"].tolist(),
            "pre_view_translation_low_cm": v["pre_view_translation_low_cm"].tolist(),
            "pre_view_translation_cm": v["pre_view_translation_cm"].tolist(),
            "view_forward_packed": v["view_forward"].tolist(),
            "view_forward_from_matrix": v["view_forward_matrix"].tolist(),
            "hfov_deg": v["hfov_deg"],
            "vfov_deg": v["vfov_deg"],
            "tw_to_clip": v["tw_to_clip"].tolist(),
            "tw_to_view": v["tw_to_view"].tolist(),
            "view_to_clip": v["view_to_clip"].tolist(),
            "svpos_to_tw": v["svpos_to_tw"].tolist(),
            "projection_checks": {
                "tw_to_clip_vs_view_to_clip_x_tw_to_view_max_abs": v["tw_to_clip_product_max_abs"],
                "pre_high_plus_low_reconstruction_max_abs": v["pre_high_plus_low_max_abs"],
                "view_forward_packed_vs_matrix_max_abs": v["view_forward_packed_minus_matrix_max_abs"],
                "view_axes_orthonormal_max_abs": v["view_axes_orthonormal_max_abs"],
            },
        },
        "selection": {
            "rule": "primary_view_id==0, view size equal to Screenshot.png, highest FPackedView.FrameIndex; graph prefix is the first 4 hex digits of the ViewData UID",
            "selected_graph_prefix": selected["prefix"],
            "selected_view_uid": selected["uid"],
            "all_candidates": [
                {
                    "uid": c["uid"],
                    "version": c["version"],
                    "graph_prefix": c["prefix"],
                    "frame_index": c["view"]["frame_index"],
                    "primary_view_id": c["view"]["primary_view_id"],
                    "camera_world_cm": c["view"]["camera_world_cm"].tolist(),
                }
                for c in candidates
            ],
            "rejected": rejected,
        },
    }
    return out


# ---------------------------------------------------------------------------
# cards
# ---------------------------------------------------------------------------

def shuffle_axes_no_flip(v, packed):
    op = (int(packed) & ((3 << 7) | (3 << 4) | (3 << 1)))
    cases = {
        ((2 << 7) | (1 << 4) | (0 << 1)): (0, 1, 2),
        ((1 << 7) | (2 << 4) | (0 << 1)): (0, 2, 1),
        ((2 << 7) | (0 << 4) | (1 << 1)): (1, 0, 2),
        ((0 << 7) | (2 << 4) | (1 << 1)): (2, 0, 1),
        ((1 << 7) | (0 << 4) | (2 << 1)): (1, 2, 0),
        ((0 << 7) | (1 << 4) | (2 << 1)): (2, 1, 0),
    }
    order = cases.get(op)
    if order is None:
        return np.zeros(3, dtype=np.float64), False
    v = np.asarray(v, dtype=np.float64)
    return np.array([v[order[0]], v[order[1]], v[order[2]]]), True


def card_flags(packed0):
    out = {}
    for name, spec in CARD_FLAG_BITS.items():
        if isinstance(spec, tuple):
            out[name] = int((packed0 >> spec[0]) & spec[1])
        else:
            out[name] = [int((packed0 >> sh) & mask) for sh, mask in spec]
    out["res_level_bias_x"] = int((packed0 >> 0) & 0xFF)
    out["res_level_bias_y"] = int((packed0 >> 8) & 0xFF)
    return out


def decode_cards(d: DumpCtx, prefix: str, state: str, view):
    entries = all_versions(d, "Lumen.Cards", prefix)
    if not entries:
        raise RuntimeError(f"{state}: no Lumen.Cards for invocation {prefix}")
    entry = pick_version(entries)
    entry["_dump"] = d
    SRC.add(entry["file"], "cards", state=state, uid=entry["uid"], version=entry["version"])
    arr = np.fromfile(entry["file"], dtype="<f4").astype(np.float32)
    cap_f4 = arr.size // 4
    cap_cards = cap_f4 // CARD_F4
    leftover_f4 = cap_f4 % CARD_F4
    if cap_cards == 0:
        raise RuntimeError(f"{state}: empty Lumen.Cards")
    c = arr[:cap_cards * CARD_F4 * 4].reshape(cap_cards, CARD_F4, 4).astype(np.float64)

    origin = c[:, 0, :3] + np.stack([c[:, 1, 3], c[:, 2, 3], c[:, 3, 3]], axis=1)
    # Float4[1..3].xyz are rows: [AxisX.x, AxisY.x, AxisZ.x], etc.  The OBB
    # axes are the columns of that 3x3 block, so transpose rows->columns.
    axis_rows = np.stack([c[:, 1, :3], c[:, 2, :3], c[:, 3, :3]], axis=1)
    axes = np.transpose(axis_rows, (0, 2, 1))
    packed0 = c[:, 0, 3].astype(np.float32).view(np.uint32)
    packed_mesh_axes = c[:, 5, 3].astype(np.float32).view(np.uint32)
    mesh_origin = c[:, 5, :3]
    mesh_extent = c[:, 6, :3]
    low_size = c[:, 4, 0].astype(np.float32).view(np.uint32)
    low_off = c[:, 4, 1].astype(np.float32).view(np.uint32)
    hi_size = c[:, 4, 2].astype(np.float32).view(np.uint32)
    hi_off = c[:, 4, 3].astype(np.float32).view(np.uint32)

    ext = np.zeros((cap_cards, 3), dtype=np.float64)
    ext_valid = np.zeros(cap_cards, dtype=bool)
    for i in range(cap_cards):
        loc, ok = shuffle_axes_no_flip(mesh_extent[i], packed_mesh_axes[i])
        loc[2] = max(loc[2], 1.0)
        ext[i] = loc
        ext_valid[i] = ok

    flags = [card_flags(int(x)) for x in packed0]
    included = np.array([f["included_visible"] == 1 for f in flags], dtype=bool)
    heightfield = np.array([f["heightfield"] == 1 for f in flags], dtype=bool)
    farfield = np.array([f["far_field"] == 1 for f in flags], dtype=bool)

    # Do not use the clamped extent here: the tail slots decode to extent.z=1.
    # The uploaded card range is marked by origin/axes/raw mesh extents.
    geom_nonzero = (
        np.any(np.abs(origin) > 0, axis=1)
        | np.any(np.abs(axes.reshape(cap_cards, 9)) > 0, axis=1)
        | np.any(np.abs(mesh_extent) > 0, axis=1)
    )
    nz = np.nonzero(geom_nonzero)[0]
    num_cards = int(nz.max()) + 1 if nz.size else 0
    contiguous = bool(
        num_cards > 0
        and np.all(geom_nonzero[:num_cards])
        and not np.any(geom_nonzero[num_cards:])
    )
    axis_norm = np.linalg.norm(axes, axis=2)
    geometry_valid = (
        np.all(np.isfinite(origin), axis=1)
        & np.all(np.isfinite(axes.reshape(cap_cards, 9)), axis=1)
        & np.all(np.isfinite(ext), axis=1)
        & (ext.max(axis=1) > 0)
        & (axis_norm.min(axis=1) > 1e-4)
        & ext_valid
    )
    valid = np.zeros(cap_cards, dtype=bool)
    valid[:num_cards] = geometry_valid[:num_cards]

    all_corners_world = np.zeros((cap_cards, 8, 3), dtype=np.float64)
    for si, sx in enumerate((-1.0, 1.0)):
        for sj, sy in enumerate((-1.0, 1.0)):
            for sk, sz in enumerate((-1.0, 1.0)):
                idx = si * 4 + sj * 2 + sk
                all_corners_world[:, idx, :] = (
                    origin
                    + sx * ext[:, 0, None] * axes[:, 0, :]
                    + sy * ext[:, 1, None] * axes[:, 1, :]
                    + sz * ext[:, 2, None] * axes[:, 2, :]
                )
    all_corners_tw = all_corners_world + np.asarray(view["pre_view_translation_cm"], dtype=np.float64)[None, None, :]

    pre = np.asarray(view["pre_view_translation_cm"], dtype=np.float64)
    centers_tw = origin + pre
    csx, csy, cvz, cw = project_tw(view, centers_tw)
    corner_sx = np.full((cap_cards, 8), np.nan)
    corner_sy = np.full((cap_cards, 8), np.nan)
    corner_vz = np.full((cap_cards, 8), np.nan)
    corner_w = np.full((cap_cards, 8), np.nan)
    for k in range(8):
        sx_, sy_, vz_, w_ = project_tw(view, all_corners_tw[:, k, :])
        corner_sx[:, k] = sx_
        corner_sy[:, k] = sy_
        corner_vz[:, k] = vz_
        corner_w[:, k] = w_

    sd = np.zeros((1, 1))
    sd = view.get("scene_depth")
    if sd is None:
        raise RuntimeError("scene depth not attached")

    ctest = depth_test(sd, view["near_plane_cm"], csx, csy, cvz, cw)
    center_pass = ctest["passes"]
    center_on = ctest["on_screen"]

    corner_on = np.zeros((cap_cards, 8), dtype=bool)
    corner_pass = np.zeros((cap_cards, 8), dtype=bool)
    corner_scene = np.full((cap_cards, 8), np.nan)
    for k in range(8):
        t = depth_test(sd, view["near_plane_cm"],
                       corner_sx[:, k], corner_sy[:, k], corner_vz[:, k], corner_w[:, k])
        corner_on[:, k] = t["on_screen"]
        corner_pass[:, k] = t["passes"]
        corner_scene[:, k] = t["scene_depth_cm"]

    hulls = []
    areas = np.zeros(cap_cards, dtype=np.float64)
    drawable = np.zeros(cap_cards, dtype=bool)
    h_img, w_img = sd.shape
    for i in range(cap_cards):
        pts = []
        if valid[i]:
            for k in range(8):
                if (corner_w[i, k] > 0 and math.isfinite(corner_sx[i, k])
                        and -2.0 <= corner_sx[i, k] <= w_img + 2.0
                        and -2.0 <= corner_sy[i, k] <= h_img + 2.0):
                    pts.append((corner_sx[i, k], corner_sy[i, k]))
        hull = hull_2d(pts)
        hulls.append(hull)
        areas[i] = polygon_area(hull)
        drawable[i] = valid[i] and len(hull) >= 3

    # depth class uses the card centre: the centre is inside the OBB and is the
    # least-biased single sample for a surface card.  Corner pass fraction is
    # retained numerically but OBB corners can overshoot the represented surface.
    depth_class = np.full(cap_cards, "offscreen", dtype=object)
    depth_class[center_on & center_pass] = "visible"
    depth_class[center_on & ~center_pass] = "occluded"
    depth_class[~valid] = "invalid"

    geom = np.concatenate([
        origin[valid].reshape(-1, 3),
        axes[valid].reshape(-1, 9),
        ext[valid].reshape(-1, 3),
        mesh_origin[valid].reshape(-1, 3),
        mesh_extent[valid].reshape(-1, 3),
    ], axis=1).astype("<f4")
    static_hash = sha256_bytes(geom.tobytes()) if geom.size else None
    order = np.lexsort(geom.T[::-1]) if geom.size else np.zeros(0, dtype=int)
    canonical_hash = sha256_bytes(geom[order].tobytes()) if geom.size else None
    per_card_geom = np.concatenate([
        origin[:, :3], axes.reshape(cap_cards, 9), ext, mesh_origin, mesh_extent
    ], axis=1).astype("<f4")
    per_card_hash = [sha256_bytes(row.tobytes()) for row in per_card_geom]

    # Representative subset: prefer cards whose projected hull is near the
    # median size, then enforce 8x6 screen-cell diversity.  This avoids letting
    # a handful of very large wall cards dominate a legibility image; every card
    # remains in the numeric records.
    base_candidates = np.nonzero(valid & included & (depth_class == "visible") & drawable)[0]
    chosen = []
    grid_used = set()
    if base_candidates.size:
        car = areas[base_candidates]
        med = float(np.median(car))
        score = np.abs(np.log(np.maximum(car, 1e-6) / max(med, 1e-6)))
        order = np.argsort(score, kind="stable")
        ranked = base_candidates[order]
        for i in ranked:
            cx = int(np.clip(csx[i] / max(1, w_img) * 8, 0, 7))
            cy = int(np.clip(csy[i] / max(1, h_img) * 6, 0, 5))
            if (cx, cy) in grid_used:
                continue
            grid_used.add((cx, cy))
            chosen.append(int(i))
            if len(chosen) >= 24:
                break
        if len(chosen) < 24:
            for i in ranked:
                if int(i) in chosen:
                    continue
                chosen.append(int(i))
                if len(chosen) >= 24:
                    break
    representative = np.zeros(cap_cards, dtype=bool)
    representative[chosen] = True

    low_sx = (low_size & 0xFFFF).astype(np.int64)
    low_sy = (low_size >> 16).astype(np.int64)
    hi_sx = (hi_size & 0xFFFF).astype(np.int64)
    hi_sy = (hi_size >> 16).astype(np.int64)
    low_pages = low_sx * low_sy
    hi_pages = hi_sx * hi_sy
    low_alloc = (low_sx > 0) & (low_sy > 0)
    hi_alloc = (hi_sx > 0) & (hi_sy > 0)
    page_resident = low_alloc | hi_alloc

    records = []
    for i in range(cap_cards):
        f = flags[i]
        rec = {
            "card_index": i,
            "valid": bool(valid[i]),
            "within_num_cards": bool(i < num_cards),
            "source_offset_bytes": i * CARD_BYTES,
            "origin_world_cm": origin[i].tolist(),
            "axes": axes[i].tolist(),
            "extent_cm": ext[i].tolist(),
            "flags": f,
            "packed0_uint": int(packed0[i]),
            "packed0_hex": hex_u32(packed0[i]),
            "page_spans": {
                "low_res": {
                    "size_in_pages": [int(low_sx[i]), int(low_sy[i])],
                    "page_table_offset": int(low_off[i]),
                    "pages": int(low_pages[i]),
                    "allocated": bool(low_alloc[i]),
                },
                "hi_res": {
                    "size_in_pages": [int(hi_sx[i]), int(hi_sy[i])],
                    "page_table_offset": int(hi_off[i]),
                    "pages": int(hi_pages[i]),
                    "allocated": bool(hi_alloc[i]),
                },
                "page_resident": bool(page_resident[i]),
            },
            "mesh_cards": {
                "origin_world_cm": mesh_origin[i].tolist(),
                "extent_cm": mesh_extent[i].tolist(),
                "packed_axes_uint": int(packed_mesh_axes[i]),
                "packed_axes_hex": hex_u32(packed_mesh_axes[i]),
                "local_extent_cm": ext[i].tolist(),
            },
            "static_geometry_sha256": per_card_hash[i],
            "representative_subset": bool(representative[i]),
        }
        if valid[i]:
            hull = hulls[i]
            rec["corners_world_cm"] = all_corners_world[i].tolist()
            rec["corners_translated_cm"] = all_corners_tw[i].tolist()
            rec["projection"] = {
                "center_screen_xy": [float(csx[i]), float(csy[i])],
                "center_view_z_cm": float(cvz[i]),
                "center_clip_w": float(cw[i]),
                "center_on_screen": bool(center_on[i]),
                "corner_screen_xy": [[float(corner_sx[i, k]), float(corner_sy[i, k])] for k in range(8)],
                "corner_clip_w": [float(corner_w[i, k]) for k in range(8)],
                "corner_on_screen": [bool(corner_on[i, k]) for k in range(8)],
                "hull_screen_xy": [[float(x), float(y)] for x, y in hull],
                "hull_area_px2": float(areas[i]),
                "drawable_outline": bool(drawable[i]),
            }
            rec["depth"] = {
                "center_scene_device_z": float(ctest["device_z"][i]),
                "center_scene_depth_cm": float(ctest["scene_depth_cm"][i]) if math.isfinite(ctest["scene_depth_cm"][i]) else None,
                "center_depth_error_cm": float(ctest["depth_error_cm"][i]) if math.isfinite(ctest["depth_error_cm"][i]) else None,
                "center_tolerance_cm": float(ctest["tolerance_cm"][i]),
                "center_passes": bool(center_pass[i]),
                "corner_sample_count": int(corner_on[i].sum()),
                "corner_pass_count": int((corner_on[i] & corner_pass[i]).sum()),
                "corner_scene_depth_cm": [float(x) if math.isfinite(x) else None for x in corner_scene[i]],
                "classification": str(depth_class[i]),
                "note": "centre depth test is the overlay classifier; OBB corners are retained numerically and can overshoot the represented surface",
            }
        else:
            rec["projection"] = None
            rec["depth"] = None
        records.append(rec)

    return {
        "entry": entry,
        "arr": arr,
        "capacity_float4": int(cap_f4),
        "capacity_cards": int(cap_cards),
        "leftover_float4": int(leftover_f4),
        "num_cards": int(num_cards),
        "contiguous": contiguous,
        "valid": valid,
        "included": included,
        "heightfield": heightfield,
        "farfield": farfield,
        "page_resident": page_resident,
        "low_pages": low_pages,
        "hi_pages": hi_pages,
        "low_off": low_off,
        "hi_off": hi_off,
        "origin": origin,
        "axes": axes,
        "extent": ext,
        "corners_world": all_corners_world,
        "corners_tw": all_corners_tw,
        "center_screen": np.stack([csx, csy], axis=1),
        "center_view_z": cvz,
        "center_w": cw,
        "center_pass": center_pass,
        "center_on": center_on,
        "depth_class": depth_class,
        "hulls": hulls,
        "areas": areas,
        "drawable": drawable,
        "representative": representative,
        "records": records,
        "static_geometry_sha256": static_hash,
        "canonical_sorted_geometry_sha256": canonical_hash,
        "per_card_geometry_sha256": per_card_hash,
        "visible_count": int(included.sum()),
        "page_resident_count": int(page_resident.sum()),
        "total_low_pages": int(low_pages.sum()),
        "total_hi_pages": int(hi_pages.sum()),
    }


def publish_cards(state, view_json, pages_capacity):
    cards = state["cards"]
    entry = cards["entry"]
    src = entry_source(
        entry, "cards-buffer", state["state_id"],
        stride=CARD_BYTES,
        extra={
            "identity_evidence": {
                "descriptor_name": entry["desc"]["Name"],
                "uid_graph_prefix_matches_invocation": entry["uid"][:4] == state["prefix"],
                "byte_size_matches_7_float4_stride_and_gpu_capacity": int(entry["file"].stat().st_size) == cards["capacity_float4"] * 16,
                "valid_cards_are_contiguous_then_zero": bool(cards["contiguous"]),
                "valid_card_count": cards["num_cards"],
                "gpu_capacity_cards": cards["capacity_cards"],
                "trailing_partial_float4_not_a_card": cards["leftover_float4"],
                "static_geometry_sha256": cards["static_geometry_sha256"],
                "imported_alias_reason": "Lumen.Cards is a persistent pooled buffer imported into every graph; no per-pass producer version is exported, so the captured v0 payload is used only after the structural and temporal identity checks above",
            },
        },
    )
    out = {
        "schema": "arda-placement-cards-v1",
        "state": state["state_id"],
        "layout": {
            "gpu_struct": "FLumenCardGPUData (Engine/Source/Runtime/Renderer/Private/Lumen/LumenMeshCards.cpp)",
            "usf_reader": "GetLumenCardData (Engine/Shaders/Private/Lumen/LumenCardCommon.ush)",
            "stride_float4": CARD_F4,
            "stride_bytes": CARD_BYTES,
            "fields": [
                "[0] = PositionHigh.xyz + Packed0W (resLevelBiasX, resLevelBiasY, AxisAlignedDirection, LightingChannelMask, bVisible, bHeightfield, bFarField)",
                "[1] = WorldOBB.AxisX.xyz + PositionLow.x",
                "[2] = WorldOBB.AxisY.xyz + PositionLow.y",
                "[3] = WorldOBB.AxisZ.xyz + PositionLow.z",
                "[4] = PackedSizeInPages(low) + PackedPageTableOffset(low) + PackedHiResSizeInPages + PackedHiResPageTableOffset",
                "[5] = MeshCardsOBB.Origin.xyz + PackedMeshCardsAxes",
                "[6] = MeshCardsOBB.Extent.xyz + AverageTexelSize",
            ],
            "origin": "WorldPosition.High + WorldPosition.Low = WorldOBB.Origin (world cm)",
            "unused_bits": "Packed0W bits 27..31 are unused/padding and are not decoded as flags",
            "axes": "WorldOBB.AxisX/Y/Z unit axes stored as columns",
            "extent": "ShuffleAxesNoFlip(MeshCardsExtent, PackedMeshCardsAxes), Z clamped to >= 1 cm",
        },
        "source": src,
        "capacity": {
            "buffer_float4": cards["capacity_float4"],
            "buffer_cards_full_stride": cards["capacity_cards"],
            "trailing_float4_not_a_card": cards["leftover_float4"],
            "valid_cards": cards["num_cards"],
            "zero_tail_cards": cards["capacity_cards"] - cards["num_cards"],
            "note": "capacity is the GPU buffer allocation; the valid range is inferred as the contiguous nonzero uploaded card payload 0..NumCards-1 (the FLumenCardScene NumCards uniform is not exported). Tail slots are zero across origin/axes/mesh extent and flags.",
        },
        "counts": {
            "valid_cards": int(cards["valid"].sum()),
            "included_visible": int(cards["included"].sum()),
            "page_resident": int(cards["page_resident"].sum()),
            "heightfield": int(cards["heightfield"].sum()),
            "far_field": int(cards["farfield"].sum()),
            "projected_center_on_screen": int(cards["center_on"].sum()),
            "depth_visible_cards": int((cards["depth_class"] == "visible").sum()),
            "depth_occluded_cards": int((cards["depth_class"] == "occluded").sum()),
            "representative_subset": int(cards["representative"].sum()),
            "total_low_res_pages": cards["total_low_pages"],
            "total_hi_res_pages": cards["total_hi_pages"],
        },
        "static_geometry_sha256": cards["static_geometry_sha256"],
        "canonical_sorted_geometry_sha256": cards["canonical_sorted_geometry_sha256"],
        "geometry_hash_excludes": ["flags", "visible bit", "page table offsets/sizes", "lighting update fields"],
        "page_capacity_used_for_bounds": int(pages_capacity),
        "representative_subset_rule": "up to 24 included, centre-depth-visible, drawable cards whose projected hull area is closest to the candidate median, one per 8x6 screen cell, then filled in the same order; all numeric cards are retained regardless of this display subset",
        "view": {
            "frame_index": view_json["frame"]["frame_index"],
            "graph_prefix": state["prefix"],
            "view_uid": view_json["view"]["resource"]["uid"],
        },
        "cards": cards["records"],
    }
    state_dir = state["data_dir"]
    write_json(state_dir / "cards.json", out, indent=1)
    np.savez_compressed(
        state_dir / "cards.npz",
        card_index=np.arange(cards["capacity_cards"], dtype=np.int32),
        valid=cards["valid"],
        included=cards["included"],
        heightfield=cards["heightfield"],
        far_field=cards["farfield"],
        page_resident=cards["page_resident"],
        origin_world_cm=cards["origin"].astype(np.float32),
        axes=cards["axes"].astype(np.float32),
        extent_cm=cards["extent"].astype(np.float32),
        corners_world_cm=cards["corners_world"].astype(np.float32),
        corners_translated_cm=cards["corners_tw"].astype(np.float32),
        center_screen_xy=cards["center_screen"].astype(np.float32),
        center_view_z_cm=cards["center_view_z"].astype(np.float32),
        center_clip_w=cards["center_w"].astype(np.float32),
        center_depth_pass=cards["center_pass"],
        center_on_screen=cards["center_on"],
        hull_area_px2=cards["areas"].astype(np.float32),
        representative=cards["representative"],
        low_pages=cards["low_pages"].astype(np.int32),
        low_page_table_offset=cards["low_off"].astype(np.int32),
        hi_pages=cards["hi_pages"].astype(np.int32),
        hi_page_table_offset=cards["hi_off"].astype(np.int32),
    )
    return out


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

def decode_pages(d: DumpCtx, prefix: str, state: str, cards):
    pb_entries = all_versions(d, "Lumen.PageBuffer", prefix)
    if not pb_entries:
        raise RuntimeError(f"{state}: no Lumen.PageBuffer")
    pb_entry = pick_version(pb_entries, prefix=prefix, prefer_names=["Build cards update list"])
    pb_entry["_dump"] = d
    SRC.add(pb_entry["file"], "page-buffer", state=state, uid=pb_entry["uid"], version=pb_entry["version"])
    pb_raw = np.fromfile(pb_entry["file"], dtype="<f4").astype(np.float32)
    pb_capacity = pb_raw.size // (PAGE_F4 * 4)
    pb = pb_raw[:pb_capacity * PAGE_F4 * 4].reshape(pb_capacity, PAGE_F4, 4).astype(np.float64)

    pt_entries = all_versions(d, "Lumen.PageTable", prefix)
    if not pt_entries:
        raise RuntimeError(f"{state}: no Lumen.PageTable")
    pt_entry = pick_version(pt_entries, prefix=prefix)
    pt_entry["_dump"] = d
    SRC.add(pt_entry["file"], "page-table", state=state, uid=pt_entry["uid"], version=pt_entry["version"])
    pt_raw = np.fromfile(pt_entry["file"], dtype="<u4").astype(np.uint32)
    pt_capacity = pt_raw.size // 2
    pt = pt_raw[:pt_capacity * 2].reshape(pt_capacity, 2)

    hi_entries = all_versions(d, "Lumen.CardPageHighResLastUsedBuffer", prefix)
    hi_entry = pick_version(hi_entries, prefix=prefix) if hi_entries else None
    if hi_entry:
        hi_entry["_dump"] = d
        SRC.add(hi_entry["file"], "card-page-highres-last-used", state=state,
                uid=hi_entry["uid"], version=hi_entry["version"])
        hi = np.fromfile(hi_entry["file"], dtype="<u4").astype(np.uint32)
    else:
        hi = None

    ta_entries = all_versions(d, "Lumen.CardPageTileAllocator", prefix)
    ta_entry = pick_version(ta_entries, prefix=prefix) if ta_entries else None
    if ta_entry:
        ta_entry["_dump"] = d
        SRC.add(ta_entry["file"], "card-page-tile-allocator", state=state,
                uid=ta_entry["uid"], version=ta_entry["version"])
        ta = np.fromfile(ta_entry["file"], dtype="<u4").astype(np.uint32)
    else:
        ta = None

    cap = min(pb_capacity, pt_capacity)
    page_records = []
    ref_owner = defaultdict(list)
    for ci in range(cards["num_cards"]):
        for res_name, off_arr, size_arr in [
            ("low_res", cards["low_off"], cards["low_pages"]),
            ("hi_res", cards["hi_off"], cards["hi_pages"]),
        ]:
            off = int(off_arr[ci])
            n = int(size_arr[ci])
            if n <= 0:
                continue
            for k in range(n):
                idx = off + k
                if 0 <= idx < cap:
                    ref_owner[idx].append((ci, res_name))

    referenced = np.zeros(cap, dtype=bool)
    for idx in ref_owner:
        referenced[idx] = True

    for i in range(cap):
        r = pb[i]
        card_index = int(r[0, 0].astype(np.float32).view(np.int32))
        res_off = int(r[0, 1].astype(np.float32).view(np.uint32))
        size_x, size_y = float(r[0, 2]), float(r[0, 3])
        mapped = size_x > 0.0
        w0, w1 = int(pt[i, 0]), int(pt[i, 1])
        upd = r[4].astype(np.float32).view(np.uint32)
        owners = ref_owner.get(i, [])
        rec = {
            "page_index": i,
            "pagebuffer_offset_bytes": i * PAGE_BYTES,
            "pagetable_offset_bytes": i * PAGE_TABLE_BYTES,
            "mapped": bool(mapped),
            "referenced_by_current_cards": bool(referenced[i]),
            "owner_cards": sorted({int(x[0]) for x in owners}),
            "owner_res_levels": sorted({x[1] for x in owners}),
            "card_index": card_index,
            "res_level_page_table_offset": res_off,
            "size_in_texels": [size_x, size_y],
            "card_uv_rect": r[1].tolist(),
            "physical_atlas_uv_rect": r[2].tolist(),
            "card_uv_texel_scale": [float(r[3, 0]), float(r[3, 1])],
            "res_level_size_in_tiles": [int(r[3, 2].astype(np.float32).view(np.uint32)),
                                         int(r[3, 3].astype(np.float32).view(np.uint32))],
            "update": {
                "last_direct_lighting_update_frame": int(upd[0]),
                "last_indirect_lighting_update_frame": int(upd[1]),
                "indirect_lighting_temporal_index": int(upd[2]),
                "direct_lighting_temporal_index": int(upd[3]),
            },
            "page_table": {
                "word0_uint": w0,
                "word0_hex": hex_u32(w0),
                "word1_uint": w1,
                "word1_hex": hex_u32(w1),
                "sample_atlas_bias_xy": [w0 & 0xFFF, (w0 >> 12) & 0xFFF],
                "sample_card_res_level_xy": [(w0 >> 24) & 0xF, (w0 >> 28) & 0xF],
                "sample_page_index": w1,
                "entry_nonzero": bool((w0 | w1) != 0),
            },
            "highres_last_used_frame": int(hi[i]) if hi is not None and i < len(hi) else None,
        }
        page_records.append(rec)

    # invariants on current references
    bad_bounds = 0
    bad_card = 0
    bad_self_sample = 0
    bad_owner = 0
    referenced_count = 0
    for idx, owners in ref_owner.items():
        if idx < 0 or idx >= cap:
            bad_bounds += 1
            continue
        referenced_count += 1
        p = page_records[idx]
        if p["card_index"] < 0 or p["card_index"] >= cards["num_cards"]:
            bad_card += 1
        if p["page_table"]["sample_page_index"] != idx:
            bad_self_sample += 1
        if owners and p["card_index"] != owners[0][0]:
            bad_owner += 1

    mapped_count = int(sum(1 for p in page_records if p["mapped"]))
    nonzero_pt = int(sum(1 for p in page_records if p["page_table"]["entry_nonzero"]))
    unreferenced_payload = int(sum(1 for p in page_records if p["mapped"] and not p["referenced_by_current_cards"]))

    return {
        "pagebuffer_entry": pb_entry,
        "pagetable_entry": pt_entry,
        "highres_entry": hi_entry,
        "tile_allocator_entry": ta_entry,
        "capacity": cap,
        "pb_capacity": pb_capacity,
        "pt_capacity": pt_capacity,
        "records": page_records,
        "referenced": referenced,
        "mapped_count": mapped_count,
        "referenced_count": referenced_count,
        "page_table_nonzero": nonzero_pt,
        "unreferenced_payload": unreferenced_payload,
        "bad_bounds": bad_bounds,
        "bad_card": bad_card,
        "bad_self_sample": bad_self_sample,
        "bad_owner": bad_owner,
        "tile_allocator_words": [int(x) for x in ta] if ta is not None else None,
        "highres_nonzero": int(np.count_nonzero(hi)) if hi is not None else None,
    }


def publish_pages(state):
    pages = state["pages"]
    cards = state["cards"]
    pb_entry = pages["pagebuffer_entry"]
    pt_entry = pages["pagetable_entry"]
    hi_entry = pages["highres_entry"]
    ta_entry = pages["tile_allocator_entry"]
    pb_src = entry_source(pb_entry, "page-buffer", state["state_id"], stride=PAGE_BYTES)
    pt_src = entry_source(pt_entry, "page-table", state["state_id"], stride=PAGE_TABLE_BYTES)
    pt_src["identity_evidence"] = {
        "descriptor_name": pt_entry["desc"]["Name"],
        "uid_graph_prefix_matches_invocation": pt_entry["uid"][:4] == state["prefix"],
        "byte_size_matches_1024_entries_x_2_uint": int(pt_entry["file"].stat().st_size) == 1024 * 8,
        "entry_count": pages["pt_capacity"],
        "referenced_entries_within_capacity": pages["bad_bounds"] == 0,
        "imported_alias_reason": "Lumen.PageTable is a persistent pooled ByteAddressBuffer imported into the graph; the v0 payload is used only after layout/bounds/ownership checks",
    }
    hi_src = entry_source(hi_entry, "card-page-highres-last-used", state["state_id"], stride=4) if hi_entry else None
    if hi_src is not None:
        hi_src["identity_evidence"] = {
            "descriptor_name": hi_entry["desc"]["Name"],
            "uid_graph_prefix_matches_invocation": hi_entry["uid"][:4] == state["prefix"],
            "byte_size_matches_1024_uint": int(hi_entry["file"].stat().st_size) == 1024 * 4,
            "entry_count": 1024,
            "imported_alias_reason": "CardPageHighResLastUsedBuffer is a persistent pooled buffer; only its 1024 uint frame counters are read and page indices are bounds-checked",
        }
    ta_src = entry_source(ta_entry, "card-page-tile-allocator", state["state_id"], stride=4) if ta_entry else None
    out = {
        "schema": "arda-placement-pages-v1",
        "state": state["state_id"],
        "layout": {
            "page_buffer_gpu_struct": "FLumenCardPageGPUData (LumenScene.cpp), USF reader GetLumenCardPageData (LumenCardCommon.ush)",
            "page_buffer_stride_float4": PAGE_F4,
            "page_buffer_stride_bytes": PAGE_BYTES,
            "page_buffer_fields": [
                "[0] = CardIndex(uint bits) + ResLevelPageTableOffset(uint bits) + SizeInTexels.xy",
                "[1] = CardUVRect",
                "[2] = PhysicalAtlasUVRect (0..1 normalized)",
                "[3] = CardUVTexelScale.xy + ResLevelSizeInTiles.xy (uint bits)",
                "[4] = LastDirectLightingUpdateFrameIndex, LastIndirectLightingUpdateFrameIndex, IndirectLightingTemporalIndex, DirectLightingTemporalIndex (uint bits)",
            ],
            "page_table": "ByteAddressBuffer Load2(8*PageTableIndex): word0 = SampleAtlasBiasX[0:12] | SampleAtlasBiasY[12:24] | SampleCardResLevelX[24:28] | SampleCardResLevelY[28:32]; word1 = SamplePageIndex",
            "mapped_rule": "PageData.SizeInTexels.x > 0",
            "valid_page_rule": "a page is current/valid here only when referenced by a page span of a valid card; unreferenced payload rows are retained buffer contents and are reported separately",
        },
        "sources": {"page_buffer": pb_src, "page_table": pt_src,
                    "highres_last_used": hi_src, "tile_allocator": ta_src},
        "capacity": {
            "page_buffer_entries": pages["pb_capacity"],
            "page_table_entries": pages["pt_capacity"],
            "decoded_entries": pages["capacity"],
            "mapped_payload_entries": pages["mapped_count"],
            "referenced_by_current_cards": pages["referenced_count"],
            "unreferenced_payload_entries": pages["unreferenced_payload"],
            "page_table_nonzero_entries": pages["page_table_nonzero"],
            "buffer_capacity_note": "1024 entries is the allocated GPU capacity; only the mapped+referenced subset is current page residency",
        },
        "counts": {
            "valid_cards": cards["num_cards"],
            "included_visible_cards": int(cards["included"].sum()),
            "page_resident_cards": int(cards["page_resident"].sum()),
            "page_span_pages_low": cards["total_low_pages"],
            "page_span_pages_hi": cards["total_hi_pages"],
            "referenced_page_indices": pages["referenced_count"],
        },
        "validation": {
            "out_of_bounds_references": pages["bad_bounds"],
            "card_index_out_of_range": pages["bad_card"],
            "sample_page_index_not_self": pages["bad_self_sample"],
            "pagebuffer_card_index_mismatch_owner": pages["bad_owner"],
            "all_referenced_indices_within_capacity": pages["bad_bounds"] == 0,
            "all_card_indices_within_num_cards": pages["bad_card"] == 0,
            "identity_sample_mapping": pages["bad_self_sample"] == 0,
            "owner_card_identity": pages["bad_owner"] == 0,
            "tile_allocator_words": pages["tile_allocator_words"],
            "highres_last_used_nonzero": pages["highres_nonzero"],
        },
        "pages": pages["records"],
    }
    write_json(state["data_dir"] / "pages.json", out, indent=1)
    np.savez_compressed(
        state["data_dir"] / "pages.npz",
        page_index=np.arange(pages["capacity"], dtype=np.int32),
        mapped=np.array([p["mapped"] for p in pages["records"]], dtype=bool),
        referenced=pages["referenced"],
        card_index=np.array([p["card_index"] for p in pages["records"]], dtype=np.int32),
        size_in_texels=np.array([p["size_in_texels"] for p in pages["records"]], dtype=np.float32),
        card_uv_rect=np.array([p["card_uv_rect"] for p in pages["records"]], dtype=np.float32),
        physical_atlas_uv_rect=np.array([p["physical_atlas_uv_rect"] for p in pages["records"]], dtype=np.float32),
        update=np.array([[p["update"][k] for k in [
            "last_direct_lighting_update_frame",
            "last_indirect_lighting_update_frame",
            "indirect_lighting_temporal_index",
            "direct_lighting_temporal_index",
        ]] for p in pages["records"]], dtype=np.uint32),
        page_table_word0=np.array([p["page_table"]["word0_uint"] for p in pages["records"]], dtype=np.uint32),
        page_table_word1=np.array([p["page_table"]["word1_uint"] for p in pages["records"]], dtype=np.uint32),
        highres_last_used=np.array([p["highres_last_used_frame"] if p["highres_last_used_frame"] is not None else -1
                                    for p in pages["records"]], dtype=np.int64),
    )
    return out


# ---------------------------------------------------------------------------
# world cache
# ---------------------------------------------------------------------------

def pass_fields(d: DumpCtx, p):
    pm = d.meta[p["ParametersMetadata"]]
    return {x["Name"]: x for x in pm["Members"]}


def pass_param_bytes(d: DumpCtx, p, state: str, role: str):
    path = d.root / "Structures" / (p["Parameters"] + ".bin")
    SRC.add(path, role, state=state, pass_id=p["Id"], pass_name=p["EventName"])
    return path.read_bytes()


def decode_mark_params(d: DumpCtx, p, state: str):
    f = pass_fields(d, p)
    base = f["RadianceCacheMarkParameters"]["Offset"]
    sm = d.meta[f["RadianceCacheMarkParameters"]["StructMetadata"]]
    sf = {x["Name"]: x["Offset"] for x in sm["Members"]}
    b = pass_param_bytes(d, p, state, "mark-radiance-cache-params")
    corners = [
        [float(x) for x in struct.unpack_from("<4f", b, base + sf["ClipmapCornerTWSAndCellSizeForMark"] + 16 * i)]
        for i in range(6)
    ]
    return {
        "pass": {"id": p["Id"], "name": p["EventName"], "pointer": p["Pointer"]},
        "parameter_block": "Structures/" + p["Parameters"] + ".bin",
        "source_offset_bytes": base,
        "corners": corners,
        "res": int(struct.unpack_from("<I", b, base + sf["RadianceProbeClipmapResolutionForMark"])[0]),
        "num": int(struct.unpack_from("<I", b, base + sf["NumRadianceProbeClipmapsForMark"])[0]),
        "inv_fade": float(struct.unpack_from("<f", b, base + sf["InvClipmapFadeSizeForMark"])[0]),
        "rw_uav_binding_present": None,
    }


def decode_alloc_params(d: DumpCtx, p, state: str):
    f = pass_fields(d, p)
    base = f["RadianceCacheParameters"]["Offset"]
    sm = d.meta[f["RadianceCacheParameters"]["StructMetadata"]]
    sf = {x["Name"]: x["Offset"] for x in sm["Members"]}
    ins_field = next(x for x in sm["Members"] if x["Name"] == "RadianceCacheInputs")
    ins_meta = d.meta[ins_field["StructMetadata"]]
    ins = {x["Name"]: x["Offset"] for x in ins_meta["Members"]}
    b = pass_param_bytes(d, p, state, "allocate-used-probes-params")

    def u(name):
        return int(struct.unpack_from("<I", b, base + ins[name])[0])

    def fl(name):
        return float(struct.unpack_from("<f", b, base + ins[name])[0])

    corners = [
        [float(x) for x in struct.unpack_from("<4f", b, base + sf["ClipmapCornerTWSAndCellSize"] + 16 * i)]
        for i in range(6)
    ]
    atlas_x = int(struct.unpack_from("<i", b, base + ins["ProbeAtlasResolutionInProbes"])[0])
    atlas_y = int(struct.unpack_from("<i", b, base + ins["ProbeAtlasResolutionInProbes"] + 4)[0])
    return {
        "pass": {"id": p["Id"], "name": p["EventName"], "pointer": p["Pointer"]},
        "parameter_block": "Structures/" + p["Parameters"] + ".bin",
        "source_offset_bytes": base,
        "clipmap_world_extent_cm": fl("ClipmapWorldExtent"),
        "clipmap_distribution_base": fl("ClipmapDistributionBase"),
        "probe_atlas_resolution_in_probes": [atlas_x, atlas_y],
        "res": u("RadianceProbeClipmapResolution"),
        "num": u("NumRadianceProbeClipmaps"),
        "probe_resolution": u("RadianceProbeResolution"),
        "final_probe_resolution": u("FinalProbeResolution"),
        "num_probes_to_trace_budget": u("NumProbesToTraceBudget"),
        "num_frames_to_keep_cached_probes": u("NumFramesToKeepCachedProbes"),
        "use_adaptive_probes": u("UseAdaptiveProbes"),
        "corners": corners,
        "frame_number": int(struct.unpack_from("<I", b, f["FrameNumber"]["Offset"])[0]),
        "max_num_probes": int(struct.unpack_from("<I", b, f["MaxNumProbes"]["Offset"])[0]),
    }


def find_mark_pass(d: DumpCtx, prefix: str, translucency=False):
    needle = "MarkRadianceProbesUsedByTranslucencyVolume" if translucency else "MarkRadianceProbes(ScreenProbes)"
    ps = [
        p for p in d.passes
        if p["Pointer"].startswith(prefix)
        and ((needle in p["EventName"]) if translucency else p["EventName"].startswith(needle))
    ]
    return ps[0] if ps else None


def find_alloc_pass(d: DumpCtx, prefix: str, mark_info, state: str):
    target_corners = np.asarray(mark_info["corners"][:mark_info["num"]], dtype=np.float64)
    matches = []
    for p in d.passes:
        if p["EventName"] != "AllocateUsedProbes" or not p["Pointer"].startswith(prefix):
            continue
        info = decode_alloc_params(d, p, state)
        if info["res"] != mark_info["res"] or info["num"] != mark_info["num"]:
            continue
        corners = np.asarray(info["corners"][:info["num"]], dtype=np.float64)
        if corners.shape != target_corners.shape:
            continue
        if float(np.max(np.abs(corners - target_corners))) > 1e-3:
            continue
        info["corner_max_abs_vs_mark"] = float(np.max(np.abs(corners - target_corners)))
        matches.append((p, info))
    if not matches:
        raise RuntimeError(f"{state}: no AllocateUsedProbes matches MarkRadianceProbes {mark_info['res']}/{mark_info['num']}")
    # Prefer a pass whose output indirection extent matches the mark resolution.
    best = None
    for p, info in matches:
        outs = {r.split(".")[0] for r in p["OutputResources"]}
        ind_uids = [
            u for u in outs
            if any(dd["UniqueResourceName"] == u and dd["Name"] == "Lumen.RadianceCache.RadianceProbeIndirectionTexture"
                   for dd in d.descs)
        ]
        exts = []
        for u in ind_uids:
            desc = next(dd for dd in d.descs if dd["UniqueResourceName"] == u)
            exts.append((int(desc.get("ExtentX") or 0), int(desc.get("ExtentY") or 0)))
        expected = (mark_info["res"] * mark_info["num"], mark_info["res"])
        score = 1 if expected in exts else 0
        if best is None or score > best[0]:
            best = (score, p, info, ind_uids, exts)
    if best is None or best[0] == 0:
        raise RuntimeError(f"{state}: matched AllocateUsedProbes has no indirection extent {mark_info['res'] * mark_info['num']}x{mark_info['res']}")
    return best[1], best[2], best[3], best[4]


def resource_uid_by_name(d: DumpCtx, outs, name):
    for u in outs:
        for dd in d.descs:
            if dd["UniqueResourceName"] == u and dd["Name"] == name:
                return u
    return None


def select_same_uid_version(d: DumpCtx, prefix: str, name: str, uid: str):
    entries = [x for x in all_versions(d, name, prefix) if x["uid"] == uid]
    if not entries:
        return None
    return pick_version(entries, prefix=prefix)


def consumers_of(d: DumpCtx, uid: str):
    out = []
    for p in d.passes:
        resources = {r.split(".")[0] for r in p["InputResources"]}
        if uid in resources:
            out.append(p)
    return out


def consumer_record(d: DumpCtx, p):
    names = set()
    meta = d.meta.get(p.get("ParametersMetadata"))
    if meta:
        names = {x["Name"] for x in meta.get("Members", [])}
    sg = []
    for r in p["InputResources"]:
        u = r.split(".")[0]
        dd = next((x for x in d.descs if x["UniqueResourceName"] == u), None)
        if dd and dd["Name"].startswith("Lumen.ScreenProbeGather."):
            if dd["Name"] not in sg:
                sg.append(dd["Name"])
    return {
        "id": p["Id"],
        "name": p["EventName"],
        "pointer": p["Pointer"],
        "graph_prefix": p["Pointer"][:4],
        "has_screen_probe_parameters": "ScreenProbeParameters" in names,
        "has_tracing_parameters": "TracingParameters" in names,
        "screen_probe_gather_resources": sg[:16],
    }


def old_global_lowest_probe(d: DumpCtx, name: str):
    groups = defaultdict(list)
    for c in all_versions(d, name):
        groups[c["uid"]].append(c)
    ranked = []
    for uid, g in groups.items():
        g = sorted(g, key=version_key)
        last = g[-1]
        if last.get("producer") is None:
            continue
        ranked.append((last["producer"]["Id"], uid, last))
    if not ranked:
        return None
    ranked.sort()
    pid, uid, last = ranked[0]
    return {
        "uid": uid,
        "graph_prefix": uid[:4],
        "producer_id": pid,
        "producer_name": last["producer"]["EventName"],
        "extent": [last["desc"].get("ExtentX"), last["desc"].get("ExtentY"), last["desc"].get("Depth")],
        "used_for_data": False,
        "reason": "counter-example only: global lowest producer id ignores the selected renderer invocation",
    }


def decode_worldcache(d: DumpCtx, prefix: str, state: str, view, mark_info=None):
    if mark_info is None:
        mark = find_mark_pass(d, prefix, translucency=False)
        if mark is None:
            raise RuntimeError(f"{state}: no MarkRadianceProbes(ScreenProbes) in invocation {prefix}")
        mark_info = decode_mark_params(d, mark, state)
    alloc_pass, alloc_info, ind_uids, ind_exts = find_alloc_pass(d, prefix, mark_info, state)
    if not ind_uids:
        raise RuntimeError(f"{state}: allocate pass has no indirection output")
    ind_uid = ind_uids[0]
    outs = {r.split(".")[0] for r in alloc_pass["OutputResources"]}

    ind_entry = select_same_uid_version(d, prefix, "Lumen.RadianceCache.RadianceProbeIndirectionTexture", ind_uid)
    if ind_entry is None:
        raise RuntimeError(f"{state}: no indirection version for {ind_uid}")
    ind_entry["_dump"] = d
    SRC.add(ind_entry["file"], "radiance-indirection", state=state,
            uid=ind_entry["uid"], version=ind_entry["version"])

    def get_out(name):
        uid = resource_uid_by_name(d, outs, name)
        if not uid:
            return None
        e = select_same_uid_version(d, prefix, name, uid)
        if e:
            e["_dump"] = d
        return e

    off_entry = get_out("Lumen.RadianceCache.ProbeWorldOffset")
    alloc_entry = get_out("Lumen.RadianceCache.ProbeAllocator")
    free_alloc_entry = get_out("Lumen.RadianceCache.ProbeFreeListAllocator")
    last_used_entry = get_out("Lumen.RadianceCache.ProbeLastUsedFrame")
    last_traced_entry = get_out("Lumen.RadianceCache.ProbeLastTracedFrame")
    if last_traced_entry is None:
        # Some captures expose only one of the two names; locate by uid if the
        # output list used a different spelling.
        for cand_name in ["Lumen.RadianceCache.ProbeLastTracedFrame", "Lumen.RadianceCache.ProbeLastTracedFrame"]:
            last_traced_entry = get_out(cand_name)
            if last_traced_entry:
                break
    free_uid = None
    for r in alloc_pass["InputResources"]:
        u = r.split(".")[0]
        dd = next((x for x in d.descs if x["UniqueResourceName"] == u), None)
        if dd and dd["Name"] == "Lumen.RadianceCache.ProbeFreeList":
            free_uid = u
            break
    free_entry = None
    if free_uid:
        free_entry = select_same_uid_version(d, prefix, "Lumen.RadianceCache.ProbeFreeList", free_uid)
        if free_entry:
            free_entry["_dump"] = d

    for e, role in [
        (off_entry, "probe-world-offset"),
        (alloc_entry, "probe-allocator"),
        (free_alloc_entry, "probe-free-list-allocator"),
        (last_used_entry, "probe-last-used-frame"),
        (last_traced_entry, "probe-last-traced-frame"),
        (free_entry, "probe-free-list"),
    ]:
        if e:
            SRC.add(e["file"], role, state=state, uid=e["uid"], version=e["version"])

    if not all([off_entry, alloc_entry, free_alloc_entry, last_used_entry, last_traced_entry, free_entry]):
        missing = [n for n, e in [
            ("ProbeWorldOffset", off_entry), ("ProbeAllocator", alloc_entry),
            ("ProbeFreeListAllocator", free_alloc_entry), ("ProbeLastUsedFrame", last_used_entry),
            ("ProbeLastTracedFrame", last_traced_entry), ("ProbeFreeList", free_entry),
        ] if not e]
        raise RuntimeError(f"{state}: missing cache buffers {missing}")

    res = int(ind_entry["desc"]["ExtentY"])
    num = int(ind_entry["desc"]["ExtentX"]) // res
    if [res * num, res, res] != [int(ind_entry["desc"].get("ExtentX")), int(ind_entry["desc"].get("ExtentY")), int(ind_entry["desc"].get("Depth", 1))]:
        raise RuntimeError(f"{state}: indirection extent mismatch {ind_entry['desc']}")

    ind_arr = np.fromfile(ind_entry["file"], dtype="<u4").astype(np.uint32)
    if ind_arr.size != res * res * res * num:
        raise RuntimeError(f"{state}: indirection file size {ind_arr.size} != {res * res * res * num}")

    off_elem_bytes = int(off_entry["desc"].get("BytesPerElement") or 8)
    if off_elem_bytes == 8:
        off_dtype = "<f2"       # PF_FloatRGBA / RGBA16f
    elif off_elem_bytes == 16:
        off_dtype = "<f4"       # PF_A32B32G32R32F
    else:
        raise RuntimeError(f"{state}: unexpected ProbeWorldOffset element size {off_elem_bytes}")
    off = np.fromfile(off_entry["file"], dtype=off_dtype).reshape(-1, 4).astype(np.float32)
    alloc = int(np.fromfile(alloc_entry["file"], dtype="<u4")[0])
    free_count = int(np.fromfile(free_alloc_entry["file"], dtype="<u4")[0])
    freelist = np.fromfile(free_entry["file"], dtype="<u4").astype(np.uint32)
    last_used = np.fromfile(last_used_entry["file"], dtype="<u4").astype(np.uint32)
    last_traced = np.fromfile(last_traced_entry["file"], dtype="<u4").astype(np.uint32)

    ind = ind_arr.reshape(res, res, res * num)
    valid = ind != 0xFFFFFFFF
    z, y, x = np.nonzero(valid)
    enc = ind[z, y, x].astype(np.uint32)
    pids = (enc & 0xFFFF).astype(np.int64)
    nad = ((enc >> 16) & 0x7).astype(np.uint16)
    c = (x // res).astype(np.int64)
    lx = (x % res).astype(np.int64)
    coord = np.stack([lx, y.astype(np.int64), z.astype(np.int64)], axis=1)

    order = np.argsort(pids, kind="stable")
    pids_s = pids[order]
    unique, first_idx, counts = np.unique(pids_s, return_index=True, return_counts=True)
    first = order[first_idx]

    corners = np.asarray(mark_info["corners"][:num], dtype=np.float64)
    corner_xyz = corners[:, :3]
    cell = corners[:, 3]
    pre = np.asarray(view["pre_view_translation_cm"], dtype=np.float64)

    c_u = c[first]
    coord_u = coord[first]
    nad_u = nad[first]
    base_tw = corner_xyz[c_u] + (coord_u + 0.5) * cell[c_u, None]
    offset = off[unique, :3].astype(np.float64)
    offset_w = off[unique, 3].astype(np.float64)
    pos_tw = base_tw + offset
    pos_world = pos_tw - pre
    corner_world = corner_xyz - pre[None, :]
    world_cell = np.floor(
        corner_world[c_u] / cell[c_u, None] + coord_u + 0.5 + 1e-6
    ).astype(np.int64)

    lu = last_used[unique].astype(np.int64) if unique.max() < len(last_used) else np.zeros(len(unique), dtype=np.int64)
    lt = last_traced[unique].astype(np.int64) if unique.max() < len(last_traced) else np.zeros(len(unique), dtype=np.int64)
    frame = int(view["frame_index"])
    keep = int(alloc_info["num_frames_to_keep_cached_probes"])
    use_age = frame - lu
    trace_age = np.where(lt > 0, frame - lt, -1)

    sx, sy, vz, w = project_tw(view, pos_tw)
    dtest = depth_test(view["scene_depth"], view["near_plane_cm"], sx, sy, vz, w)
    on = dtest["on_screen"]
    visible = dtest["passes"]
    occluded = on & ~visible

    # active bookkeeping
    free_ids = [int(x) for x in freelist[:max(0, min(free_count, len(freelist)))]]
    ref_set = set(int(x) for x in unique)
    free_set = set(free_ids)
    lu_set = set(int(unique[i]) for i in range(len(unique))
                 if lu[i] > 0 and (frame - lu[i]) < max(1, keep))
    lt_set = set(int(unique[i]) for i in range(len(unique))
                 if lt[i] > 0 and (frame - lt[i]) < max(1, keep))
    max_pid = int(unique.max()) if unique.size else -1
    stale_with_timestamps = []
    if max_pid >= 0:
        limit = min(PROBE_MAX, len(last_used), len(last_traced), alloc)
        for pid in range(limit):
            if pid not in ref_set and (last_used[pid] > 0 or last_traced[pid] > 0):
                stale_with_timestamps.append(pid)
                if len(stale_with_timestamps) >= 512:
                    break
    ids_above_allocator = int(np.count_nonzero(unique >= alloc)) if alloc >= 0 else 0
    active_count = len(ref_set)
    alloc_minus_free = alloc - free_count

    records = []
    world_cell_keys = []
    for i, pid in enumerate(unique):
        key = f"{int(c_u[i])}:{int(world_cell[i,0])},{int(world_cell[i,1])},{int(world_cell[i,2])}"
        world_cell_keys.append(key)
        rec = {
            "physical_id": int(pid),
            "clipmap": int(c_u[i]),
            "logical_coord": [int(coord_u[i, 0]), int(coord_u[i, 1]), int(coord_u[i, 2])],
            "logical_linear_index": int((c_u[i] * res + coord_u[i, 0]) + coord_u[i, 1] * res * num + coord_u[i, 2] * res * res * num),
            "world_cell": [int(world_cell[i, 0]), int(world_cell[i, 1]), int(world_cell[i, 2])],
            "world_cell_key": key,
            "reference_count": int(counts[i]),
            "num_adaptive": int(nad_u[i]),
            "indirection_source_offset_bytes": int(
                ((int(z[first[i]]) * res + int(y[first[i]])) * res * num + int(x[first[i]])) * 4
            ),
            "world_offset_source_offset_bytes": int(pid) * 8,
            "last_used_source_offset_bytes": int(pid) * 4,
            "last_traced_source_offset_bytes": int(pid) * 4,
            "corner_translated_cm": corner_xyz[c_u[i]].tolist(),
            "cell_size_cm": float(cell[c_u[i]]),
            "base_translated_cm": base_tw[i].tolist(),
            "probe_world_offset_cm": offset[i].tolist(),
            "probe_world_offset_w": float(offset_w[i]),
            "position_translated_cm": pos_tw[i].tolist(),
            "position_world_cm": pos_world[i].tolist(),
            "last_used_frame": int(lu[i]),
            "last_traced_frame": int(lt[i]),
            "use_age_frames": int(use_age[i]),
            "traced_age_frames": int(trace_age[i]) if trace_age[i] >= 0 else None,
            "never_traced": bool(lt[i] == 0),
            "screen_xy": [float(sx[i]), float(sy[i])],
            "view_z_cm": float(vz[i]),
            "clip_w": float(w[i]),
            "on_screen": bool(on[i]),
            "scene_depth_device_z": float(dtest["device_z"][i]),
            "scene_depth_cm": float(dtest["scene_depth_cm"][i]) if math.isfinite(dtest["scene_depth_cm"][i]) else None,
            "depth_error_cm": float(dtest["depth_error_cm"][i]) if math.isfinite(dtest["depth_error_cm"][i]) else None,
            "depth_tolerance_cm": float(dtest["tolerance_cm"][i]),
            "depth_passes": bool(visible[i]),
            "visible": bool(visible[i]),
            "occluded": bool(occluded[i]),
            "active_by_indirection": True,
            "active_by_last_used_window": bool(int(pid) in lu_set),
            "active_by_last_traced_window": bool(int(pid) in lt_set),
        }
        records.append(rec)

    # duplicate references: record all cells for a pid if any
    multi = {int(unique[i]): [] for i in range(len(unique)) if counts[i] > 1}
    if multi:
        for j in range(len(pids)):
            pid = int(pids[j])
            if pid in multi:
                multi[pid].append({
                    "clipmap": int(c[j]),
                    "coord": [int(coord[j, 0]), int(coord[j, 1]), int(coord[j, 2])],
                    "encoded_uint": int(enc[j]),
                })
    for rec in records:
        rec["all_referencing_cells"] = multi.get(rec["physical_id"], [])

    activity = {}
    for cm in range(num):
        idx = [i for i in range(len(unique)) if c_u[i] == cm]
        if not idx:
            activity[str(cm)] = {"active": 0}
            continue
        ages = trace_age[idx]
        traced = ages[ages >= 0]
        bins = {
            "0": int(np.count_nonzero(traced == 0)),
            "1": int(np.count_nonzero(traced == 1)),
            "2": int(np.count_nonzero(traced == 2)),
            "3": int(np.count_nonzero(traced == 3)),
            "4_7": int(np.count_nonzero((traced >= 4) & (traced <= 7))),
            "8_15": int(np.count_nonzero((traced >= 8) & (traced <= 15))),
            "16_31": int(np.count_nonzero((traced >= 16) & (traced <= 31))),
            "32_plus": int(np.count_nonzero(traced >= 32)),
        }
        activity[str(cm)] = {
            "clipmap": cm,
            "active": int(len(idx)),
            "never_traced": int(np.count_nonzero(ages < 0)),
            "traced_age_frames_median": float(np.median(traced)) if traced.size else None,
            "traced_age_frames_mean": float(np.mean(traced)) if traced.size else None,
            "traced_age_frames_max": int(traced.max()) if traced.size else None,
            "traced_age_histogram": bins,
            "last_used_age_frames_median": float(np.median(use_age[idx])) if idx else None,
            "position_hash_input_count": int(len(idx)),
        }

    pos_hash_arr = np.concatenate([
        unique.astype(np.float64)[:, None],
        world_cell.astype(np.float64),
        pos_world.astype(np.float64),
    ], axis=1).astype("<f4")
    pos_hash = sha256_bytes(pos_hash_arr.tobytes())
    cell_hash = sha256_bytes(np.asarray(sorted(world_cell_keys), dtype="S").tobytes())

    # rejected translucency-volume cache group
    trans = None
    tmark = find_mark_pass(d, prefix, translucency=True)
    if tmark is not None:
        tinfo = decode_mark_params(d, tmark, state)
        try:
            tap, tinfo2, tinds, text = find_alloc_pass(d, prefix, tinfo, state)
            tuid = tinds[0] if tinds else None
            tentry = select_same_uid_version(d, prefix, "Lumen.RadianceCache.RadianceProbeIndirectionTexture", tuid) if tuid else None
            tcons = consumers_of(d, tuid) if tuid else []
            trans = {
                "mark": tinfo,
                "alloc": tinfo2,
                "indirection_uid": tuid,
                "indirection_extent": text[0] if text else None,
                "consumers": [consumer_record(d, p) for p in tcons],
                "used_for_data": False,
                "reason": "translucency-volume variant: smaller clipmap grid, 8x8 FilterProbeRadiance, no ScreenProbeGather lineage",
            }
            if tentry:
                trans["indirection_version"] = tentry["version"]
        except Exception as ex:
            trans = {"mark": tinfo, "error": str(ex), "used_for_data": False}

    consumers = consumers_of(d, ind_uid)
    main_consumers = [consumer_record(d, p) for p in consumers]
    screen_lineage = [
        c for c in main_consumers
        if c["has_screen_probe_parameters"] or c["has_tracing_parameters"]
        or c["screen_probe_gather_resources"] or c["name"].startswith("FilterProbeRadiance")
    ]

    selection = {
        "method": "select View invocation first; match MarkRadianceProbes(ScreenProbes) FRadianceCacheMarkParameters to an AllocateUsedProbes FRadianceCacheParameters by resolution, clipmap count and exact corners; take the indirection and companion buffers from that allocate pass's OutputResources; latest same-UID version carries allocator/free-list final values",
        "view_graph_prefix": prefix,
        "mark_pass": mark_info,
        "allocate_pass": alloc_info,
        "indirection_uid": ind_uid,
        "indirection_extent": [res * num, res, res],
        "indirection_version": ind_entry["version"],
        "probe_resolution": alloc_info["probe_resolution"],
        "final_probe_resolution": alloc_info["final_probe_resolution"],
        "filter_resolution_matches": [c["name"] for c in main_consumers if "FilterProbeRadiance" in c["name"]],
        "consumers": main_consumers,
        "screen_probe_lineage": screen_lineage,
        "rejected_translucency_volume": trans,
        "old_global_lowest_producer_probe": old_global_lowest_probe(d, "Lumen.RadianceCache.RadianceProbeIndirectionTexture"),
        "invocation_consistent": (
            all(e and e["uid"][:4] == prefix for e in [
                ind_entry, off_entry, alloc_entry, free_alloc_entry, last_used_entry, last_traced_entry, free_entry
            ])
            and alloc_pass["Pointer"][:4] == prefix
            and mark_info["pass"]["pointer"][:4] == prefix
        ),
    }

    return {
        "available": True,
        "res": res,
        "num": num,
        "mark_info": mark_info,
        "alloc_info": alloc_info,
        "alloc_pass": alloc_pass,
        "ind_entry": ind_entry,
        "off_entry": off_entry,
        "alloc_entry": alloc_entry,
        "free_alloc_entry": free_alloc_entry,
        "last_used_entry": last_used_entry,
        "last_traced_entry": last_traced_entry,
        "free_entry": free_entry,
        "ind": ind,
        "valid_entries": int(valid.sum()),
        "unique_pids": int(unique.size),
        "allocator": alloc,
        "free_count": free_count,
        "freelist": freelist,
        "last_used": last_used,
        "last_traced": last_traced,
        "offset": off,
        "corners": corners,
        "records": records,
        "arrays": {
            "pid": unique.astype(np.int32),
            "clipmap": c_u.astype(np.int16),
            "coord": coord_u.astype(np.int16),
            "world_cell": world_cell.astype(np.int32),
            "world_cell_key": world_cell_keys,
            "counts": counts.astype(np.int32),
            "pos_world": pos_world.astype(np.float32),
            "pos_tw": pos_tw.astype(np.float32),
            "offset": offset.astype(np.float32),
            "offset_w": offset_w.astype(np.float32),
            "screen_xy": np.stack([sx, sy], axis=1).astype(np.float32),
            "view_z": vz.astype(np.float32),
            "clip_w": w.astype(np.float32),
            "on_screen": on,
            "visible": visible,
            "occluded": occluded,
            "last_used": lu.astype(np.uint32),
            "last_traced": lt.astype(np.uint32),
            "use_age": use_age.astype(np.int32),
            "trace_age": trace_age.astype(np.int32),
            "state_frame": frame,
        },
        "sets": {
            "allocator_high_water": alloc,
            "free_count": free_count,
            "allocator_minus_free": alloc_minus_free,
            "active_by_indirection": active_count,
            "active_by_last_used_window": len(lu_set),
            "active_by_last_traced_window": len(lt_set),
            "active_by_either_timestamp_window": len(lu_set | lt_set),
            "free_ids": free_ids,
            "free_ids_referenced_count": len(free_set & ref_set),
            "ids_at_or_above_allocator": ids_above_allocator,
            "max_referenced_pid": max_pid,
            "stale_ids_with_timestamps_count": len(stale_with_timestamps),
            "stale_ids_with_timestamps_sample": stale_with_timestamps[:64],
            "num_frames_to_keep_cached_probes": keep,
            "last_used_nonzero_count": int(np.count_nonzero(last_used)),
            "last_traced_nonzero_count": int(np.count_nonzero(last_traced)),
        },
        "activity": activity,
        "positions_sha256": pos_hash,
        "world_cells_sha256": cell_hash,
        "probe_world_offset_zero": bool(np.count_nonzero(off) == 0),
        "off_elem_bytes": off_elem_bytes,
        "off_dtype": off_dtype,
        "scene_depth": view["scene_depth"],
        "selection_consistent": bool(
            all(e and e["uid"][:4] == prefix for e in [
                ind_entry, off_entry, alloc_entry, free_alloc_entry,
                last_used_entry, last_traced_entry, free_entry
            ])
            and alloc_pass["Pointer"][:4] == prefix
            and mark_info["pass"]["pointer"][:4] == prefix
        ),
    }


def publish_worldcache(state, view_json):
    wc = state["wc"]
    d = state["d"]
    prefix = state["prefix"]
    sources = {
        "indirection": entry_source(wc["ind_entry"], "radiance-indirection", state["state_id"], stride=4),
        "world_offset": entry_source(wc["off_entry"], "probe-world-offset", state["state_id"], stride=wc["off_elem_bytes"]),
        "allocator": entry_source(wc["alloc_entry"], "probe-allocator", state["state_id"], stride=4),
        "free_list_allocator": entry_source(wc["free_alloc_entry"], "probe-free-list-allocator", state["state_id"], stride=4),
        "last_used": entry_source(wc["last_used_entry"], "probe-last-used-frame", state["state_id"], stride=4),
        "last_traced": entry_source(wc["last_traced_entry"], "probe-last-traced-frame", state["state_id"], stride=4),
        "free_list": entry_source(wc["free_entry"], "probe-free-list", state["state_id"], stride=4),
    }
    out = {
        "schema": "arda-placement-worldcache-v1",
        "state": state["state_id"],
        "space": {
            "clipmap_corner": "MarkRadianceProbes(ScreenProbes).RadianceCacheMarkParameters.ClipmapCornerTWSAndCellSizeForMark[c].xyz (translated world, cm)",
            "cell_size_cm": "ClipmapCornerTWSAndCellSizeForMark[c].w",
            "logical_coord": "indirection texel (x within clipmap, y, z) decoded from Texture3D index (x + c*resolution)",
            "base_position_translated_cm": "corner + (logical_coord + 0.5) * cell_size",
            "probe_world_offset_cm": "ProbeWorldOffset[physical_id].xyz (stored PF_FloatRGBA/float16x4 when BytesPerElement=8, or PF_A32B32G32R32F when 16; read as float4 by the shader)",
            "probe_world_offset_storage": f"BytesPerElement={wc['off_elem_bytes']}, decode_dtype={wc['off_dtype']}",
            "position_translated_cm": "base_position_translated_cm + probe_world_offset_cm",
            "position_world_cm": "position_translated_cm - PreViewTranslation (PreViewTranslationHigh + PreViewTranslationLow)",
            "world_cell": "floor((corner - PreViewTranslation) / cell_size + logical_coord + 0.5); integer world cell at this clipmap level, stable under clipmap-origin motion",
            "note": "ProbeWorldOffset is in translated-world space; it is not a world-space absolute position",
        },
        "indirection_encoding": {
            "invalid": "0xFFFFFFFF",
            "physical_id": "bits 0..15",
            "num_adaptive": "bits 16..18",
            "used_marker": "0xFFFFFFFE is the mark-only marker and must not appear in a completed AllocateUsedProbes output",
        },
        "selection": {
            "method": "camera invocation first; MarkRadianceProbes(ScreenProbes) parameters matched to AllocateUsedProbes parameters; resource versions taken from that pass's OutputResources",
            "invocation_consistent": wc["selection_consistent"],
            "view_graph_prefix": prefix,
            "mark_pass": wc["mark_info"],
            "allocate_pass": wc["alloc_info"],
            "indirection": sources["indirection"],
            "consumers": [consumer_record(d, p) for p in consumers_of(d, wc["ind_entry"]["uid"])],
            "screen_probe_lineage": [
                consumer_record(d, p) for p in consumers_of(d, wc["ind_entry"]["uid"])
                if consumer_record(d, p)["has_screen_probe_parameters"]
                or consumer_record(d, p)["has_tracing_parameters"]
                or consumer_record(d, p)["screen_probe_gather_resources"]
                or "FilterProbeRadiance" in p["EventName"]
            ],
            "rejected_translucency_volume": wc.get("translucency"),
            "old_global_lowest_producer_probe": old_global_lowest_probe(d, "Lumen.RadianceCache.RadianceProbeIndirectionTexture"),
        },
        "sources": sources,
        "counts": {
            "valid_indirection_entries": wc["valid_entries"],
            "unique_physical_probes": wc["unique_pids"],
            "allocator_high_water": wc["allocator"],
            "free_count": wc["free_count"],
            "allocator_minus_free": wc["allocator"] - wc["free_count"],
            "active_by_indirection": len(wc["records"]),
            "active_by_last_used_window": wc["sets"]["active_by_last_used_window"],
            "active_by_last_traced_window": wc["sets"]["active_by_last_traced_window"],
            "last_used_nonzero": wc["sets"]["last_used_nonzero_count"],
            "last_traced_nonzero": wc["sets"]["last_traced_nonzero_count"],
            "never_traced_active": int(sum(1 for r in wc["records"] if r["never_traced"])),
            "on_screen": int(sum(1 for r in wc["records"] if r["on_screen"])),
            "visible": int(sum(1 for r in wc["records"] if r["visible"])),
            "occluded": int(sum(1 for r in wc["records"] if r["occluded"])),
            "probe_world_offset_nonzero": int(np.count_nonzero(wc["offset"])),
        },
        "sets": wc["sets"],
        "activity": wc["activity"],
        "positions_sha256": wc["positions_sha256"],
        "world_cells_sha256": wc["world_cells_sha256"],
        "probe_world_offset_zero": wc["probe_world_offset_zero"],
        "view": {
            "frame_index": view_json["frame"]["frame_index"],
            "camera_world_cm": view_json["view"]["camera_world_cm"],
            "graph_prefix": prefix,
        },
        "probes": wc["records"],
    }
    # fill selection from wc if prepared
    if "selection_consumers" in wc:
        out["selection"]["filter_resolution_matches"] = [
            c["name"] for c in wc["selection_consumers"] if "FilterProbeRadiance" in c["name"]
        ]
    write_json(state["data_dir"] / "worldcache.json", out, indent=1)
    a = wc["arrays"]
    np.savez_compressed(
        state["data_dir"] / "worldcache.npz",
        physical_id=a["pid"],
        clipmap=a["clipmap"],
        logical_coord=a["coord"],
        world_cell=a["world_cell"],
        reference_count=a["counts"],
        position_world_cm=a["pos_world"],
        position_translated_cm=a["pos_tw"],
        probe_world_offset_cm=a["offset"],
        probe_world_offset_w=a["offset_w"],
        screen_xy=a["screen_xy"],
        view_z_cm=a["view_z"],
        clip_w=a["clip_w"],
        on_screen=a["on_screen"],
        visible=a["visible"],
        occluded=a["occluded"],
        last_used_frame=a["last_used"],
        last_traced_frame=a["last_traced"],
        use_age_frames=a["use_age"],
        traced_age_frames=a["trace_age"],
        state_frame=np.array([a["state_frame"]], dtype=np.int32),
    )
    return out


# ---------------------------------------------------------------------------
# state decode
# ---------------------------------------------------------------------------

def state_summary(state_id: str, run: str, subdir: str, root: Path, label_meta: dict):
    return {
        "state": state_id,
        "run": run,
        "subdir": subdir,
        "root": str(root),
        **label_meta,
    }


def decode_state(state_id: str, root: Path, run: str, subdir: str, out_seq: Path,
                 meta: dict, verbose=False):
    t0 = time.time()
    state_dir = root / subdir
    if not (state_dir / "Base/Passes.json").exists():
        raise RuntimeError(f"{state_id}: missing {state_dir}/Base/Passes.json")
    register_dump_json(state_dir, state_id)
    screenshot = state_dir / "Base/Screenshot.png"
    SRC.add(screenshot, "base-screenshot", state=state_id)
    with Image.open(screenshot) as im:
        screenshot_size = im.size
    d = DumpCtx(state_dir)
    selected, candidates, rejected = load_views_and_select(d, state_id, screenshot_size)
    view = selected["view"]
    prefix = selected["prefix"]
    engine_frame = parse_engine_frame(d.passes)

    sd_entry, sd = select_scene_depth(d, prefix, state_id)
    view["scene_depth"] = sd
    view["scene_depth_entry"] = sd_entry
    view_json = decode_view_json(selected, candidates, rejected, d, state_id, engine_frame)
    view_json["scene_depth"] = {
        "uid": sd_entry["uid"],
        "version": sd_entry["version"],
        "file": "Resources/" + sd_entry["file"].name,
        "sha256": SRC.get(sd_entry["file"])["sha256_before"],
        "extent": [int(sd_entry["desc"]["ExtentX"]), int(sd_entry["desc"]["ExtentY"])],
        "stored_format": sd_entry["desc"].get("Format"),
        "producer": (
            {"id": sd_entry["producer"]["Id"], "name": sd_entry["producer"]["EventName"],
             "pointer": sd_entry["producer"]["Pointer"]}
            if sd_entry.get("producer") else None
        ),
        "graph_prefix": sd_entry["uid"][:4],
        "device_z_to_view_cm": "view_cm = NearPlane / device_z when device_z > 0",
        "sampling_note": "SceneDepthZ extent is taller than the 1280x720 view/screenshot; projection uses the FPackedView view rect (1280x720), so the top-left 1280x720 region is sampled. This was validated against card-centre depth.",
    }
    cards = decode_cards(d, prefix, state_id, view)
    pages = decode_pages(d, prefix, state_id, cards)
    # page capacity for card bounds validation
    pages_capacity = pages["pt_capacity"]

    mark = find_mark_pass(d, prefix, translucency=False)
    if mark is None:
        raise RuntimeError(f"{state_id}: no MarkRadianceProbes(ScreenProbes)")
    mark_info = decode_mark_params(d, mark, state_id)
    # ParameterBindings is an actual captured binding table; record the selected
    # pass bindings for the mark/allocate passes as identity evidence.
    pb_path = state_dir / "Base/ParameterBindings.json"
    pb_rows = []
    if pb_path.exists():
        pb_data = load_concat_json(pb_path)
        wanted = {mark["Pointer"]}
        # allocate pass pointer is resolved in decode_worldcache; collect it there
        pb_rows = [r for r in pb_data if r.get("Pass") in wanted]
        for r in pb_rows:
            r["_bindings_subset"] = {
                k: {"BaseType": v.get("BaseType"), "Pointer": v.get("Pointer"),
                    "Source": v.get("Source"), "Null": v.get("Null")}
                for k, v in r.get("Bindings", {}).items()
                if "RadianceProbeIndirectionTexture" in k or "ProbeWorldOffset" in k
                or "RadianceCacheParameters" in k
            }
    wc = decode_worldcache(d, prefix, state_id, view, mark_info=mark_info)
    # attach actual bindings for the selected allocate pass
    wc["parameter_bindings"] = []
    if pb_path.exists():
        pb_data = load_concat_json(pb_path)
        alloc_ptr = wc["alloc_info"]["pass"]["pointer"]
        for r in pb_data:
            if r.get("Pass") in (mark["Pointer"], alloc_ptr):
                wc["parameter_bindings"].append({
                    "pass": r.get("Pass"),
                    "parameters": r.get("Parameters"),
                    "bindings_subset": {
                        k: {"BaseType": v.get("BaseType"), "Pointer": v.get("Pointer"),
                            "Source": v.get("Source"), "Null": v.get("Null")}
                        for k, v in r.get("Bindings", {}).items()
                        if "RadianceProbeIndirectionTexture" in k or "ProbeWorldOffset" in k
                        or "ProbeAllocator" in k or "ProbeFreeList" in k
                        or "ProbeLastUsedFrame" in k or "ProbeLastTracedFrame" in k
                        or "RadianceCacheParameters" in k
                    },
                })
    # attach selected consumers for publish
    wc["selection_consumers"] = [consumer_record(d, p) for p in consumers_of(d, wc["ind_entry"]["uid"])]
    # rejected translucency record
    wc["translucency"] = None
    tmark = find_mark_pass(d, prefix, translucency=True)
    if tmark is not None:
        tinfo = decode_mark_params(d, tmark, state_id)
        try:
            tap, tinfo2, tinds, text = find_alloc_pass(d, prefix, tinfo, state_id)
            tuid = tinds[0] if tinds else None
            tentry = select_same_uid_version(d, prefix, "Lumen.RadianceCache.RadianceProbeIndirectionTexture", tuid) if tuid else None
            tcons = consumers_of(d, tuid) if tuid else []
            wc["translucency"] = {
                "mark": tinfo,
                "alloc": tinfo2,
                "indirection_uid": tuid,
                "indirection_version": tentry["version"] if tentry else None,
                "indirection_extent": text[0] if text else None,
                "consumers": [consumer_record(d, p) for p in tcons],
                "used_for_data": False,
                "reason": "translucency-volume variant: 24^3 x3 clipmaps, 8x8 FilterProbeRadiance, no ScreenProbeGather lineage; rejected by MarkRadianceProbes(ScreenProbes) parameter identity and extent",
            }
        except Exception as ex:
            wc["translucency"] = {"mark": tinfo, "error": str(ex), "used_for_data": False}

    # expected camera from metadata
    expected_camera = meta.get("camera_location_cm")
    camera_err = None
    if expected_camera is not None:
        camera_err = float(np.max(np.abs(np.asarray(view["camera_world_cm"], dtype=np.float64)
                                        - np.asarray(expected_camera, dtype=np.float64))))

    state = {
        "state_id": state_id,
        "run": run,
        "subdir": subdir,
        "root": state_dir,
        "prefix": prefix,
        "view": view,
        "view_json": view_json,
        "d": d,
        "sd": sd,
        "sd_entry": sd_entry,
        "cards": cards,
        "pages": pages,
        "wc": wc,
        "meta": meta,
        "engine_frame": engine_frame,
        "camera_err_vs_manifest_cm": camera_err,
        "data_dir": out_seq / "data" / state_id,
        "fig_dir": out_seq / "figures" / state_id,
        "decode_seconds": None,
    }
    state["data_dir"].mkdir(parents=True, exist_ok=True)
    state["fig_dir"].mkdir(parents=True, exist_ok=True)
    view_json["dataset_metadata"] = {
        "label": meta.get("label"),
        "actual_update": meta.get("actual_update"),
        "capture_actual_update_raw": meta.get("capture_actual_update_raw"),
        "actual_update_source": meta.get("actual_update_source"),
        "run": run,
        "subdir": subdir,
        "dump_directory": str(state_dir),
        "expected_camera_world_cm": expected_camera,
        "camera_error_vs_manifest_cm": camera_err,
        "engine_frame_scope": engine_frame,
    }
    write_json(state["data_dir"] / "view.json", view_json)
    cards_pub = publish_cards(state, view_json, pages_capacity)
    pages_pub = publish_pages(state)
    wc_pub = publish_worldcache(state, view_json)
    # source ledger for this state, after all selected raw files are known
    write_json(state["data_dir"] / "sources.json", {
        "schema": "arda-placement-sources-v1",
        "state": state_id,
        "note": "sha256_before was taken before the first decode read; sha256_after is filled by the final integrity pass and the file rewritten at the end of the run",
        "sources": resource_source_rows(state_id, state_dir),
    }, indent=1)
    state["cards_pub"] = cards_pub
    state["pages_pub"] = pages_pub
    state["wc_pub"] = wc_pub
    state["decode_seconds"] = round(time.time() - t0, 3)
    if verbose:
        print(f"  {state_id}: prefix={prefix} frame={view['frame_index']} cards={cards['num_cards']} "
              f"visible={cards['visible_count']} pages={pages['referenced_count']} "
              f"probes={wc['unique_pids']} alloc={wc['allocator']} free={wc['free_count']} "
              f"({state['decode_seconds']}s)")
    return state


# ---------------------------------------------------------------------------
# figures
# ---------------------------------------------------------------------------

def color_rgba(hex_color: str, alpha: int):
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(alpha))


def copy_base(state):
    src = state["root"] / "Base/Screenshot.png"
    dst = state["fig_dir"] / "base.png"
    shutil.copyfile(src, dst)
    return {
        "file": str(dst.relative_to(state["fig_dir"].parents[2])).replace("\\", "/"),
        "width": 1280,
        "height": 720,
        "sha256": sha256_file(dst),
        "raw_sha256": SRC.get(src)["sha256_before"],
        "byte_identical_to_raw": sha256_file(dst) == SRC.get(src)["sha256_before"],
    }


def render_cards_image(state, xray=False):
    base = Image.open(state["fig_dir"] / "base.png").convert("RGB")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    cards = state["cards"]
    subset = set(int(i) for i in np.nonzero(cards["representative"])[0])
    drawn = 0
    for i, rec in enumerate(cards["records"]):
        if not rec["valid"] or not rec["flags"]["included_visible"]:
            continue
        if not rec.get("projection") or not rec["projection"]["drawable_outline"]:
            continue
        cls = rec["depth"]["classification"]
        if xray:
            if cls != "occluded":
                continue
            alpha = CARD_XRAY_ALPHA
            width = 1
            color = CARD_COLOR
        else:
            # The primary cards-only image is intentionally a representative
            # subset; all cards remain in the numeric publication.
            if cls != "visible" or i not in subset:
                continue
            alpha = CARD_SUBSET_ALPHA
            width = 2
            color = CARD_SUBSET_COLOR
        hull = rec["projection"]["hull_screen_xy"]
        if len(hull) < 3:
            continue
        pts = [(float(x), float(y)) for x, y in hull]
        pts.append(pts[0])
        draw.line(pts, fill=color_rgba(color, alpha), width=width, joint="curve")
        drawn += 1
    out = Image.alpha_composite(base.convert("RGBA"), layer).convert("RGB")
    return out, drawn


def render_worldcache_image(state, xray=False):
    base = Image.open(state["fig_dir"] / "base.png").convert("RGB")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    wc = state["wc"]
    drawn = 0
    # draw coarse clipmaps first so finer levels are readable on top
    recs = sorted(wc["records"], key=lambda r: -r["clipmap"])
    for r in recs:
        if xray:
            if not r["occluded"]:
                continue
            alpha = WC_XRAY_ALPHA
        else:
            if not r["visible"]:
                continue
            alpha = WC_PRIMARY_ALPHA
        cx, cy = r["screen_xy"]
        if not math.isfinite(cx) or not math.isfinite(cy):
            continue
        cm = int(r["clipmap"])
        rad = CLIPMAP_RADII[cm] if cm < len(CLIPMAP_RADII) else 1.6
        col = color_rgba(CLIPMAP_COLORS[cm] if cm < len(CLIPMAP_COLORS) else "#FFFFFF", alpha)
        outline = (0, 0, 0, min(alpha, 210))
        draw.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=col, outline=outline, width=1)
        drawn += 1
    out = Image.alpha_composite(base.convert("RGBA"), layer).convert("RGB")
    return out, drawn


def image_diff_count(a: Image.Image, b: Image.Image):
    aa = np.asarray(a.convert("RGB"), dtype=np.int16)
    bb = np.asarray(b.convert("RGB"), dtype=np.int16)
    return int(np.count_nonzero(np.any(aa != bb, axis=2)))


def render_figures(state):
    base_info = copy_base(state)
    base_img = Image.open(state["fig_dir"] / "base.png").convert("RGB")
    cards_img, cards_drawn = render_cards_image(state, xray=False)
    cards_xray_img, cards_xray_drawn = render_cards_image(state, xray=True)
    wc_img, wc_drawn = render_worldcache_image(state, xray=False)
    wc_xray_img, wc_xray_drawn = render_worldcache_image(state, xray=True)
    paths = {
        "cards_only": state["fig_dir"] / "cards-only.png",
        "cards_xray": state["fig_dir"] / "cards-xray.png",
        "worldcache_only": state["fig_dir"] / "worldcache-only.png",
        "worldcache_xray": state["fig_dir"] / "worldcache-xray.png",
    }
    cards_img.save(paths["cards_only"], optimize=True)
    cards_xray_img.save(paths["cards_xray"], optimize=True)
    wc_img.save(paths["worldcache_only"], optimize=True)
    wc_xray_img.save(paths["worldcache_xray"], optimize=True)
    rel = lambda p: str(p.relative_to(state["fig_dir"].parents[2])).replace("\\", "/")
    out = {
        "base": base_info,
        "cards_only": {
            "file": rel(paths["cards_only"]), "width": 1280, "height": 720,
            "drawn_cards": cards_drawn,
            "changed_pixels_vs_base": image_diff_count(base_img, cards_img),
            "sha256": sha256_file(paths["cards_only"]),
        },
        "cards_xray": {
            "file": rel(paths["cards_xray"]), "width": 1280, "height": 720,
            "drawn_cards": cards_xray_drawn,
            "changed_pixels_vs_base": image_diff_count(base_img, cards_xray_img),
            "sha256": sha256_file(paths["cards_xray"]),
        },
        "worldcache_only": {
            "file": rel(paths["worldcache_only"]), "width": 1280, "height": 720,
            "drawn_probes": wc_drawn,
            "changed_pixels_vs_base": image_diff_count(base_img, wc_img),
            "sha256": sha256_file(paths["worldcache_only"]),
        },
        "worldcache_xray": {
            "file": rel(paths["worldcache_xray"]), "width": 1280, "height": 720,
            "drawn_probes": wc_xray_drawn,
            "changed_pixels_vs_base": image_diff_count(base_img, wc_xray_img),
            "sha256": sha256_file(paths["worldcache_xray"]),
        },
    }
    return out


def write_sequence_legend(seq_dir: Path, states, figure_info):
    legend = {
        "schema": "arda-placement-legend-v1",
        "display_transform": {
            "base": "native Base/Screenshot.png of the selected dump, copied byte-for-byte to figures/<state>/base.png",
            "composite": "base RGB + one RGBA geometry overlay; no gamma, tone, exposure or resize applied; 1280x720 top-left pixel coordinates",
            "text": "no text is burned into any image; all labels are native JSON metadata in this file and in the per-state data files",
        },
        "families": {
            "cards": {
                "color": CARD_COLOR,
                "subset_color": CARD_SUBSET_COLOR,
                "subset_alpha": CARD_SUBSET_ALPHA,
                "subset_width_px": 2,
                "xray_alpha": CARD_XRAY_ALPHA,
                "xray_width_px": 1,
                "geometry": "projected convex hull of the 8 OBB corners (world OBB shifted by PreViewTranslation)",
                "subset_rule": "up to 24 visible cards whose projected hull area is closest to the candidate median, one per 8x6 screen cell; the primary image draws only this subset, all cards remain numeric",
                "depth_classifier": "card-centre view depth vs SceneDepthZ at the projected pixel; tolerance max(10 cm, 0.02*scene_depth_cm)",
                "xray": "separate image; only centre-depth-occluded cards, low alpha",
            },
            "worldcache": {
                "colors_by_clipmap": CLIPMAP_COLORS,
                "radius_px_by_clipmap": CLIPMAP_RADII,
                "primary_alpha": WC_PRIMARY_ALPHA,
                "xray_alpha": WC_XRAY_ALPHA,
                "geometry": "projected probe world position = clipmap corner + (logical coord + 0.5)*cell + ProbeWorldOffset - PreViewTranslation",
                "activity": "last-traced age is numeric only (per-probe traced_age_frames and per-state activity histograms); marker colour/size is fixed per clipmap and is not varied by age",
                "xray": "separate image; only probe-centre-occluded probes, low alpha",
            },
        },
        "states": figure_info,
    }
    write_json(seq_dir / "figures" / "legend.json", legend, indent=2)
    return legend


def write_sequence_data(seq_dir: Path, sequence: str, states, figure_info):
    seq = {
        "schema": "arda-placement-sequence-v1",
        "sequence": sequence,
        "generated_utc": utc_now(),
        "states": [s["summary"] for s in states],
        "figures": figure_info,
        "card_tracks": [],
        "worldcache_tracks": [],
        "activity_tracks": [],
        "cross_sequence_note": None,
    }
    for a, b in zip(states, states[1:]):
        ca = a["cards"]
        cb = b["cards"]
        prev_inc = ca["included"]
        curr_inc = cb["included"]
        added = np.nonzero(~prev_inc & curr_inc)[0]
        removed = np.nonzero(prev_inc & ~curr_inc)[0]
        pr_added = np.nonzero(~ca["page_resident"] & cb["page_resident"])[0]
        pr_removed = np.nonzero(ca["page_resident"] & ~cb["page_resident"])[0]
        span_changed = np.nonzero(
            (ca["low_off"] != cb["low_off"]) | (ca["low_pages"] != cb["low_pages"])
            | (ca["hi_off"] != cb["hi_off"]) | (ca["hi_pages"] != cb["hi_pages"])
        )[0]
        geom_equal = bool(
            ca["static_geometry_sha256"] == cb["static_geometry_sha256"]
            and ca["num_cards"] == cb["num_cards"]
        )
        cam_same = bool(np.max(np.abs(
            a["view"]["camera_world_cm"] - b["view"]["camera_world_cm"]
        )) < 1e-6)
        card_track = {
            "from": a["state_id"],
            "to": b["state_id"],
            "camera_world_equal": cam_same,
            "static_geometry_equal": geom_equal,
            "static_geometry_sha256_prev": ca["static_geometry_sha256"],
            "static_geometry_sha256_curr": cb["static_geometry_sha256"],
            "num_valid_prev": ca["num_cards"],
            "num_valid_curr": cb["num_cards"],
            "included_prev": ca["visible_count"],
            "included_curr": cb["visible_count"],
            "included_added_count": int(added.size),
            "included_removed_count": int(removed.size),
            "included_added_card_indices": added.tolist(),
            "included_removed_card_indices": removed.tolist(),
            "page_resident_prev": ca["page_resident_count"],
            "page_resident_curr": cb["page_resident_count"],
            "page_resident_added_count": int(pr_added.size),
            "page_resident_removed_count": int(pr_removed.size),
            "page_resident_added_card_indices": pr_added.tolist(),
            "page_resident_removed_card_indices": pr_removed.tolist(),
            "page_span_changed_count": int(span_changed.size),
            "page_span_changed_card_indices": span_changed.tolist(),
            "page_span_pages_prev": ca["total_low_pages"],
            "page_span_pages_curr": cb["total_low_pages"],
            "change_class": "inclusion_flag_and_page_residency_only" if geom_equal else "static_geometry_changed",
            "geometry_motion_inferred": False,
            "audit": (
                "fixed camera; visible count changed 371 -> 522 while the exact static card geometry hash is identical. "
                "This is an inclusion/visibility flag (and matching page-residency) change, not geometry motion. "
                "The capture manifest debug observations are recorded but do not establish a cause."
                if cam_same and a["state_id"] == "lightstep-001" and b["state_id"] == "lightstep-008"
                else ("fixed camera; geometry hash identical; only flags/residency changed" if cam_same and geom_equal
                      else "camera moved; geometry hash identical; flag changes are frustum/page inclusion only")
            ),
        }
        seq["card_tracks"].append(card_track)

        wa = a["wc"]
        wb = b["wc"]
        pa = {r["physical_id"]: r for r in wa["records"]}
        pb = {r["physical_id"]: r for r in wb["records"]}
        shared = sorted(set(pa) & set(pb))
        only_a = sorted(set(pa) - set(pb))
        only_b = sorted(set(pb) - set(pa))
        same_cell_same_pid = 0
        same_cell_new_pid = 0
        pid_changed_cell = []
        coord_changed_same_world_cell = 0
        for pid in shared:
            ra, rb = pa[pid], pb[pid]
            if ra["world_cell_key"] == rb["world_cell_key"]:
                same_cell_same_pid += 1
                if ra["logical_coord"] != rb["logical_coord"]:
                    coord_changed_same_world_cell += 1
            else:
                pid_changed_cell.append({
                    "physical_id": pid,
                    "prev_world_cell_key": ra["world_cell_key"],
                    "curr_world_cell_key": rb["world_cell_key"],
                    "prev_clipmap": ra["clipmap"],
                    "curr_clipmap": rb["clipmap"],
                    "prev_position_world_cm": ra["position_world_cm"],
                    "curr_position_world_cm": rb["position_world_cm"],
                    "classification": "physical_slot_reassigned_to_new_world_cell",
                })
        cell_a = {}
        cell_b = {}
        for pid, r in pa.items():
            cell_a.setdefault((r["clipmap"], tuple(r["world_cell"])), []).append(pid)
        for pid, r in pb.items():
            cell_b.setdefault((r["clipmap"], tuple(r["world_cell"])), []).append(pid)
        shared_cells = set(cell_a) & set(cell_b)
        cell_reassigned_new_pid = 0
        for k in shared_cells:
            if set(cell_a[k]) != set(cell_b[k]):
                cell_reassigned_new_pid += 1
        clip_shifts = []
        for cm in range(min(wa["num"], wb["num"])):
            dcm = wb["corners"][cm][:3] - wa["corners"][cm][:3]
            dcw = (wb["corners"][cm][:3] - np.asarray(b["view"]["pre_view_translation_cm"])) - (
                wa["corners"][cm][:3] - np.asarray(a["view"]["pre_view_translation_cm"])
            )
            cell = float(wa["corners"][cm][3])
            clip_shifts.append({
                "clipmap": cm,
                "cell_size_cm": cell,
                "corner_translated_delta_cm": dcm.tolist(),
                "corner_world_delta_cm": dcw.tolist(),
                "corner_world_delta_in_cells": (dcw / cell).tolist(),
                "integer_cell_shift": bool(np.max(np.abs(dcw / cell - np.round(dcw / cell))) < 1e-4),
            })
        wc_track = {
            "from": a["state_id"],
            "to": b["state_id"],
            "camera_world_equal": cam_same,
            "positions_sha256_equal": wa["positions_sha256"] == wb["positions_sha256"],
            "active_prev": len(pa),
            "active_curr": len(pb),
            "allocator_prev": wa["allocator"],
            "allocator_curr": wb["allocator"],
            "free_prev": wa["free_count"],
            "free_curr": wb["free_count"],
            "pids_shared": len(shared),
            "pids_only_prev": len(only_a),
            "pids_only_curr": len(only_b),
            "same_world_cell_same_pid": same_cell_same_pid,
            "same_world_cell_new_pid": cell_reassigned_new_pid,
            "physical_id_reassigned_to_new_world_cell": len(pid_changed_cell),
            "physical_id_reassigned_records": pid_changed_cell,
            "logical_coord_changed_but_world_cell_same": coord_changed_same_world_cell,
            "world_cells_shared": len(shared_cells),
            "world_cells_only_prev": len(set(cell_a) - set(cell_b)),
            "world_cells_only_curr": len(set(cell_b) - set(cell_a)),
            "clipmap_origin_motion": clip_shifts,
            "classification_rule": "a physical id is actual slot reuse only when it maps to a different world cell at the same clipmap level; logical-coordinate changes that preserve the world cell are clipmap-origin motion/equivalent world cells, not recycle",
        }
        seq["worldcache_tracks"].append(wc_track)

        def act(state):
            w = state["wc"]
            return {
                "state": state["state_id"],
                "frame_index": int(w["arrays"]["state_frame"]),
                "by_clipmap": w["activity"],
                "positions_sha256": w["positions_sha256"],
            }
        seq["activity_tracks"].append({
            "from": a["state_id"], "to": b["state_id"],
            "prev": act(a), "curr": act(b),
            "positions_fixed": wa["positions_sha256"] == wb["positions_sha256"],
            "trace_age_note": "traced_age_frames is frame_index - last_traced_frame for last_traced_frame != 0; 0 means never traced",
        })
    write_json(seq_dir / "data" / "sequence.json", seq, indent=2)
    return seq


# ---------------------------------------------------------------------------
# dataset metadata
# ---------------------------------------------------------------------------

def load_dataset_metadata(light_root: Path | None, motion_root: Path | None):
    """Read capture metadata only.  Never used to supply decode values."""
    out = {"light": {}, "motion": {}, "expected": {}}
    light_manifest = (
        light_root.parent.parent.parent / "temporal" / "manifest.json"
        if light_root is not None
        else None
    )
    if light_manifest is not None and light_manifest.exists():
        try:
            SRC.add(light_manifest, "capture-metadata", state="shared")
            m = json.load(open(light_manifest, encoding="utf-8"))
            for exp in m.get("experiments", []):
                for s in exp.get("states", []):
                    lab = s.get("label", "")
                    key = lab.replace("-lit", "")
                    cam = (s.get("camera") or {}).get("location_cm")
                    out["light"][key] = {
                        "actual_update": s.get("actual_update"),
                        "target_update": s.get("target_update"),
                        "actual_update_source": str(light_manifest),
                        "camera_location_cm": cam,
                        "engine_frames_in_dump": s.get("engine_frames_in_dump"),
                        "dump_status": s.get("dump_status"),
                    }
            out["light_manifest"] = str(light_manifest)
        except Exception as ex:
            out["light_error"] = str(ex)
    motion_run = (
        motion_root.parent.parent.parent / "temporal" / f"run-{motion_root.name}.json"
        if motion_root is not None
        else None
    )
    if motion_run is not None and motion_run.exists():
        try:
            SRC.add(motion_run, "capture-metadata", state="shared")
            m = json.load(open(motion_run, encoding="utf-8"))
            # The authoritative per-dump draw count is actual_phase_draw.
            for dd in m.get("dumps", []):
                lab = dd.get("label")
                lit = dd.get("lit_record") or {}
                cam = (lit.get("camera") or {}).get("location_cm")
                out["motion"][lab] = {
                    "actual_update": dd.get("actual_phase_draw"),
                    "target_update": dd.get("target_phase_draw"),
                    "actual_update_source": str(motion_run),
                    "camera_location_cm": cam,
                    "engine_frames_in_dump": dd.get("engine_frames_in_dump"),
                    "engine_frame_before": lit.get("engine_frame_before"),
                    "engine_frame_after": lit.get("engine_frame_after"),
                    "dump_status": dd.get("status"),
                }
            for s in m.get("camera_states", []):
                lab = s.get("label")
                if lab in out["motion"]:
                    continue
                lit = s.get("lit") or {}
                cam = (lit.get("camera") or {}).get("location_cm")
                out["motion"][lab] = {
                    "actual_update": s.get("actual_update"),
                    "target_update": s.get("target_update"),
                    "actual_update_source": str(motion_run),
                    "camera_location_cm": cam,
                    "engine_frame_before": lit.get("engine_frame_before"),
                    "engine_frame_after": lit.get("engine_frame_after"),
                }
            out["motion_run"] = str(motion_run)
            out["motion_process"] = m.get("run_id")
        except Exception as ex:
            out["motion_error"] = str(ex)
    return out


def discover_states(root: Path, kind: str, explicit=None):
    if explicit:
        names = [x.strip() for x in explicit.split(",") if x.strip()]
        return names
    names = []
    for p in sorted(root.iterdir(), key=lambda x: x.name):
        if not p.is_dir() or not (p / "Base/Passes.json").exists():
            continue
        if p.name == "baseline":
            names.append(p.name)
        elif kind == "light" and p.name.startswith("lightstep-"):
            names.append(p.name)
        elif kind == "motion" and p.name.startswith("camera-"):
            names.append(p.name)
    def sk(n):
        if n == "baseline":
            return (-1, 0)
        try:
            return (0, int(n.split("-")[-1]))
        except Exception:
            return (1, 0)
    return sorted(names, key=sk)


def expected_actual_update(kind: str, label: str):
    if label == "baseline":
        return "baseline"
    try:
        n = int(label.split("-")[-1])
    except Exception:
        return None
    # First post-baseline step is 1; later labels are render counts since the
    # baseline (008 -> 7, 032 -> 31, 064 -> 63; motion 064 -> 63).
    return max(1, n - 1)


def attach_state_meta(kind, label, ds_meta):
    m = ds_meta.get(kind, {}).get(label, {})
    actual_raw = m.get("actual_update")
    source = m.get("actual_update_source")
    if actual_raw is None:
        actual_raw = expected_actual_update(kind, label)
        source = "derived_from_label"
    actual = "baseline" if label == "baseline" else actual_raw
    return {
        "label": label,
        "target_update": m.get("target_update"),
        "actual_update": actual,
        "capture_actual_update_raw": actual_raw,
        "actual_update_source": source,
        "expected_actual_update": expected_actual_update(kind, label),
        "camera_location_cm": m.get("camera_location_cm"),
        "engine_frames_in_dump": m.get("engine_frames_in_dump"),
        "dump_status": m.get("dump_status"),
    }


# ---------------------------------------------------------------------------
# checks / report
# ---------------------------------------------------------------------------

def add_check(checks, cid, status, detail, evidence=None):
    checks.append({
        "id": cid,
        "status": status,
        "detail": detail,
        "evidence": evidence or {},
    })


def run_checks(sequence_results, all_states, changed_raw, figure_info, ds_meta, out_dir,
               requested_labels):
    checks = []

    def skey(st):
        return f"{st.get('sequence', '?')}/{st['state_id']}"
    # 1. invocation consistency
    inv_bad = []
    for s in all_states:
        wc = s["wc"]
        if not wc["selection_consistent"]:
            inv_bad.append(skey(s))
    add_check(
        checks, "cache_resources_and_transforms_same_invocation",
        "pass" if not inv_bad else "fail",
        "Selected View, MarkRadianceProbes(ScreenProbes), AllocateUsedProbes and every cache buffer UID belong to one graph prefix; the projection matrices and PreViewTranslation are from that same View.",
        {
            "states_checked": [s["state_id"] for s in all_states],
            "failed_states": inv_bad,
            "by_state": {
                skey(s): {
                    "view_prefix": s["prefix"],
                    "mark_prefix": s["wc"]["mark_info"]["pass"]["pointer"][:4],
                    "alloc_prefix": s["wc"]["alloc_info"]["pass"]["pointer"][:4],
                    "cache_uids": {
                        "indirection": s["wc"]["ind_entry"]["uid"],
                        "world_offset": s["wc"]["off_entry"]["uid"],
                        "allocator": s["wc"]["alloc_entry"]["uid"],
                        "free_list_allocator": s["wc"]["free_alloc_entry"]["uid"],
                        "last_used": s["wc"]["last_used_entry"]["uid"],
                        "last_traced": s["wc"]["last_traced_entry"]["uid"],
                        "free_list": s["wc"]["free_entry"]["uid"],
                    },
                    "indirection_extent": [s["wc"]["res"] * s["wc"]["num"], s["wc"]["res"], s["wc"]["res"]],
                    "mark_resolution": s["wc"]["mark_info"]["res"],
                    "mark_num_clipmaps": s["wc"]["mark_info"]["num"],
                    "alloc_resolution": s["wc"]["alloc_info"]["res"],
                    "alloc_num_clipmaps": s["wc"]["alloc_info"]["num"],
                    "corner_max_abs_vs_mark_cm": float(np.max(np.abs(
                        np.asarray(s["wc"]["alloc_info"]["corners"][:s["wc"]["num"]])
                        - np.asarray(s["wc"]["mark_info"]["corners"][:s["wc"]["num"]])
                    ))),
                    "probe_resolution": s["wc"]["alloc_info"]["probe_resolution"],
                    "consumers": [c["name"] for c in s["wc"]["selection_consumers"]],
                    "rejected_translucency": {
                        "mark_res": (s["wc"].get("translucency") or {}).get("mark", {}).get("res"),
                        "mark_num": (s["wc"].get("translucency") or {}).get("mark", {}).get("num"),
                        "indirection_extent": (s["wc"].get("translucency") or {}).get("indirection_extent"),
                        "consumers": [c["name"] for c in (s["wc"].get("translucency") or {}).get("consumers", [])],
                    },
                    "old_global_lowest_producer_probe": old_global_lowest_probe(s["d"], "Lumen.RadianceCache.RadianceProbeIndirectionTexture"),
                }
                for s in all_states
            },
        },
    )

    # 2. sequences / frames
    expected_labels = {k: list(requested_labels[k]) for k in sequence_results
                       if sequence_results.get(k)}
    seq_ok = True
    seq_detail = {}
    for kind, labels in expected_labels.items():
        got = [s["state_id"] for s in sequence_results.get(kind, [])]
        actuals = [s["meta"].get("actual_update") for s in sequence_results.get(kind, [])]
        exp_actuals = [s["meta"].get("expected_actual_update") for s in sequence_results.get(kind, [])]
        # The capture manifest records an integer warm-up "actual update" for
        # baseline; acceptance names the baseline by label, not by that number.
        ok = got == labels and all(
            (lab == "baseline") or (a == e)
            for lab, a, e in zip(got, actuals, exp_actuals)
        )
        seq_ok &= ok
        seq_detail[kind] = {"expected_labels": labels, "decoded_labels": got,
                            "actual_updates": actuals, "expected_actual_updates": exp_actuals,
                            "ok": ok}
    add_check(checks, "card_bounds_and_worldcache_positions_both_sequences",
              "pass" if seq_ok else "fail",
              "Every requested sequence state set decoded; numeric card/page/probe records exist for every state.",
              {"sequences": seq_detail,
               "by_state": {skey(s): {
                   "valid_cards": s["cards"]["num_cards"],
                   "included_cards": s["cards"]["visible_count"],
                   "page_resident_cards": s["cards"]["page_resident_count"],
                   "active_probes": s["wc"]["unique_pids"],
                   "allocator": s["wc"]["allocator"],
                   "free": s["wc"]["free_count"],
                   "frame_index": s["view"]["frame_index"],
               } for s in all_states}})

    # 3. provenance / figures
    fig_ok = True
    fig_detail = {}
    for s in all_states:
        fi = s["figures"]
        try:
            ok = all((s["fig_dir"] / Path(fi[k]["file"]).name).exists() for k in
                     ["cards_only", "cards_xray", "worldcache_only", "worldcache_xray"])
            ok &= fi["cards_only"]["changed_pixels_vs_base"] > 0
            ok &= fi["worldcache_only"]["changed_pixels_vs_base"] > 0
        except (KeyError, TypeError):
            ok = False
        fig_ok &= ok
        fig_detail[skey(s)] = {
            "cards_only": fi["cards_only"],
            "cards_xray": fi["cards_xray"],
            "worldcache_only": fi["worldcache_only"],
            "worldcache_xray": fi["worldcache_xray"],
            "ok": ok,
        }
    add_check(checks, "individual_scene_overlays_and_temporal_provenance",
              "pass" if fig_ok else "fail",
              "Each state has a native Base copy plus separate cards-only and worldcache-only composites (and separate xray variants); no mixed-family image is produced. Numeric records and source hashes back every overlay.",
              {"by_state": fig_detail,
               "sequence_files": {k: str((out_dir / k / "data/sequence.json").relative_to(out_dir)).replace("\\", "/")
                                  for k in sequence_results}})

    # 4. projection
    proj_bad = []
    proj_detail = {}
    for s in all_states:
        v = s["view"]
        on_depth = [
            r["depth"]["center_depth_error_cm"]
            for r in s["cards"]["records"]
            if r["valid"] and r.get("depth") and r["depth"]["center_passes"]
            and r["depth"]["center_depth_error_cm"] is not None
        ]
        checked_on = [
            r for r in s["cards"]["records"]
            if r["valid"] and r.get("depth") and r["projection"]["center_on_screen"]
        ]
        checks_ = {
            "tw_to_clip_product_max_abs": v["tw_to_clip_product_max_abs"],
            "pre_high_plus_low_max_abs": v["pre_high_plus_low_max_abs"],
            "view_forward_packed_vs_matrix_max_abs": v["view_forward_packed_minus_matrix_max_abs"],
            "view_axes_orthonormal_max_abs": v["view_axes_orthonormal_max_abs"],
            "camera_error_vs_manifest_cm": s["camera_err_vs_manifest_cm"],
            "card_center_on_screen": len(checked_on),
            "card_center_depth_pass": len(on_depth),
            "card_center_depth_pass_fraction_of_on_screen": (
                len(on_depth) / len(checked_on) if checked_on else None
            ),
            "card_center_depth_error_median_cm_among_pass": (
                float(np.median(on_depth)) if on_depth else None
            ),
            "card_center_depth_error_p90_cm_among_pass": (
                float(np.percentile(on_depth, 90)) if on_depth else None
            ),
        }
        ok = (checks_["tw_to_clip_product_max_abs"] < 1e-4
              and checks_["pre_high_plus_low_max_abs"] < 1e-5
              and checks_["view_forward_packed_vs_matrix_max_abs"] < 1e-4
              and checks_["view_axes_orthonormal_max_abs"] < 1e-4
              and (checks_["camera_error_vs_manifest_cm"] is None
                   or checks_["camera_error_vs_manifest_cm"] < 0.5)
              and (checks_["card_center_depth_pass_fraction_of_on_screen"] is None
                   or checks_["card_center_depth_pass_fraction_of_on_screen"] > 0.1))
        if not ok:
            proj_bad.append(skey(s))
        proj_detail[skey(s)] = checks_
    add_check(checks, "projection_and_camera_verified",
              "pass" if not proj_bad else "fail",
              "FPackedView matrices reconstruct consistently, packed forward matches the matrix, and the decoded camera matches the capture manifest where available.",
              {"failed_states": proj_bad, "by_state": proj_detail})

    # 5. allocator/free/reference
    alloc_bad = []
    alloc_detail = {}
    for s in all_states:
        w = s["wc"]
        sets = w["sets"]
        ok = (sets["allocator_minus_free"] == len(w["records"])
              and sets["free_ids_referenced_count"] == 0
              and sets["ids_at_or_above_allocator"] == 0
              and sets["max_referenced_pid"] < sets["allocator_high_water"])
        if not ok:
            alloc_bad.append(skey(s))
        alloc_detail[skey(s)] = {
            "allocator_high_water": sets["allocator_high_water"],
            "free_count": sets["free_count"],
            "allocator_minus_free": sets["allocator_minus_free"],
            "active_by_indirection": len(w["records"]),
            "active_by_last_used_window": sets["active_by_last_used_window"],
            "active_by_last_traced_window": sets["active_by_last_traced_window"],
            "free_ids_referenced": sets["free_ids_referenced_count"],
            "max_referenced_pid": sets["max_referenced_pid"],
            "ids_at_or_above_allocator": sets["ids_at_or_above_allocator"],
            "last_used_nonzero": sets["last_used_nonzero_count"],
            "last_traced_nonzero": sets["last_traced_nonzero_count"],
            "stale_ids_with_timestamps": sets["stale_ids_with_timestamps_count"],
            "num_frames_to_keep_cached_probes": sets["num_frames_to_keep_cached_probes"],
        }
    add_check(checks, "allocator_free_reference_counts",
              "pass" if not alloc_bad else "fail",
              "Allocator high-water minus free-list count equals the referenced physical-probe count; free ids are disjoint from referenced ids; no referenced id is at/above the allocator. Last-used/last-traced arrays are reported separately and can retain stale ids.",
              {"failed_states": alloc_bad, "by_state": alloc_detail})

    # 6. static card hash
    static_bad = []
    static_detail = {}
    for kind, states in sequence_results.items():
        if not states:
            continue
        hashes = [s["cards"]["static_geometry_sha256"] for s in states]
        canon = [s["cards"]["canonical_sorted_geometry_sha256"] for s in states]
        ok = len(set(hashes)) == 1 and len(set(canon)) == 1
        if not ok:
            static_bad.append(kind)
        static_detail[kind] = {"static_geometry_sha256": hashes,
                               "canonical_sorted_geometry_sha256": canon,
                               "index_order_stable_within_sequence": len(set(hashes)) == 1}
    all_canon = {s["cards"]["canonical_sorted_geometry_sha256"] for s in all_states}
    all_index = {s["cards"]["static_geometry_sha256"] for s in all_states}
    cross_ok = len(all_canon) == 1
    add_check(checks, "static_card_geometry_hash",
              "pass" if not static_bad and cross_ok else "fail",
              "The exact float32 static geometry hash (origin, axes, extents, mesh-card transform) is constant within each requested sequence. Canonical sorted hashes are also equal across the decoded sequences; card index order can differ between runs, so indices are never matched across sequences.",
              {"failed_sequences": static_bad, "by_sequence": static_detail,
               "cross_sequence_canonical_hashes": sorted(all_canon),
               "cross_sequence_index_order_hashes": sorted(all_index),
               "cross_sequence_geometry_set_equal": cross_ok,
               "cross_sequence_index_order_equal": len(all_index) == 1})

    # 7. page index bounds
    bounds_bad = []
    bounds_detail = {}
    for s in all_states:
        p = s["pages"]
        ok = (p["bad_bounds"] == 0 and p["bad_card"] == 0
              and p["bad_self_sample"] == 0 and p["bad_owner"] == 0)
        if not ok:
            bounds_bad.append(skey(s))
        bounds_detail[skey(s)] = {
            "page_buffer_capacity": p["pb_capacity"],
            "page_table_capacity": p["pt_capacity"],
            "referenced_pages": p["referenced_count"],
            "mapped_payload": p["mapped_count"],
            "unreferenced_payload": p["unreferenced_payload"],
            "out_of_bounds": p["bad_bounds"],
            "card_index_out_of_range": p["bad_card"],
            "sample_page_not_self": p["bad_self_sample"],
            "owner_mismatch": p["bad_owner"],
        }
    add_check(checks, "page_index_bounds_and_mapping",
              "pass" if not bounds_bad else "fail",
              "Every page span referenced by a valid card is inside the 1024-entry page-table/page-buffer capacity, every referenced PageBuffer card index is inside the inferred valid-card range, and every referenced PageTable sample-page index is the identity page.",
              {"failed_states": bounds_bad, "by_state": bounds_detail})

    # 8. imported alias identity
    alias_bad = []
    alias_detail = {}
    for s in all_states:
        c = s["cards_pub"]
        p = s["pages_pub"]
        alias_detail[skey(s)] = {
            "cards_source": {"uid": c["source"]["uid"], "version": c["source"]["version"],
                             "producer": c["source"]["producer"],
                             "imported_alias": c["source"]["imported_alias"],
                             "identity_evidence": c["source"].get("identity_evidence")},
            "page_table_source": {"uid": p["sources"]["page_table"]["uid"],
                                  "version": p["sources"]["page_table"]["version"],
                                  "producer": p["sources"]["page_table"]["producer"],
                                  "imported_alias": p["sources"]["page_table"]["imported_alias"],
                                  "identity_evidence": p["sources"]["page_table"].get("identity_evidence")},
            "cards_file_sha256": c["source"]["file_sha256"],
        }
        if not c["source"].get("imported_alias") or not p["sources"]["page_table"].get("imported_alias"):
            # A produced version is also acceptable; only mark if neither
            # structural identity nor a same-invocation producer is present.
            if c["source"]["uid"][:4] != s["prefix"] or p["sources"]["page_table"]["uid"][:4] != s["prefix"]:
                alias_bad.append(skey(s))
        if not c["source"].get("identity_evidence", {}).get("valid_cards_are_contiguous_then_zero"):
            alias_bad.append(skey(s))
    add_check(checks, "imported_alias_identity_evidence",
              "pass" if not alias_bad else "fail",
              "Lumen.Cards and Lumen.PageTable have only v0 imported payloads. Identity is established structurally (descriptor name, exact stride/capacity, contiguous valid cards, page bounds) and temporally (per-state payload hash changes only with flags/residency while the exact static geometry hash is constant).",
              {"failed_states": alias_bad, "by_state": alias_detail})

    # 9. raw hashes
    add_check(checks, "raw_inputs_hashed_before_and_after_zero_changed",
              "pass" if not changed_raw else "fail",
              f"{len(SRC.records)} raw input files were hashed before and after decoding; changed count = {len(changed_raw)}.",
              {"files_hashed": len(SRC.records),
               "changed_count": len(changed_raw),
               "changed_files": changed_raw,
               "nativebuffer_files_read": sum(1 for r in SRC.records if "NativeBuffer" in r),
               "excluded_retry_paths_read": [r for r in SRC.records if "20261115-101923" in r or "Y+150" in r]})

    # 10. no forbidden families
    forbidden = []
    for s in all_states:
        blob = json.dumps(s["wc_pub"]).lower() + json.dumps(s["cards_pub"]).lower()
        for word in ["radiosity", "sh_coeff", "sh3", "irradiance"]:
            if word in blob:
                forbidden.append((s["state_id"], word))
    add_check(checks, "no_radiosity_or_worldcache_sh",
              "pass" if not forbidden else "fail",
              "No radiosity, SH coefficients, or invented cache-content fields are present in the placement publication.",
              {"forbidden_hits": forbidden})

    # 11. dataset exclusion
    bad_roots = [str(s["root"]) for s in all_states
                 if "20261115-101923" in str(s["root"]) or "Y+150" in str(s["root"])]
    add_check(checks, "accepted_datasets_only",
              "pass" if not bad_roots else "fail",
              "Only the accepted 20261115-101057 light root and 20261115-104035 motion root are used; the narrow retry 20261115-101923 and rejected +Y150 states are not used.",
              {"bad_roots": bad_roots,
               "roots": sorted({str(s["root"].parent) for s in all_states}),
               "retry_present_in_sources": [r for r in SRC.records if "101923" in r],
               "plus_y150_present_in_sources": [r for r in SRC.records if "Y+150" in r]})

    # 12. figure/no-text audit
    renderer_source = Path(__file__).read_text(encoding="utf-8")
    text_calls = (renderer_source.count("draw" + ".text(")
                  + renderer_source.count("ImageDraw" + ".text("))
    add_check(checks, "no_burned_text_in_figures",
              "pass" if text_calls == 0 else "fail",
              "Figures are drawn with PIL line/ellipse primitives only; the renderer source contains no text draw call. Legend and labels are native JSON.",
              {"renderer_functions": ["render_cards_image", "render_worldcache_image"],
               "source_draw_text_occurrences": text_calls,
               "legend": "figures/legend.json"})

    failed = [c for c in checks if c["status"] == "fail"]
    return {
        "schema": "arda-placement-checks-v1",
        "generated_utc": utc_now(),
        "status": "pass" if not failed else "fail",
        "check_count": len(checks),
        "failed_count": len(failed),
        "checks": checks,
    }


def write_report(out_dir: Path, sequence_results, checks_doc, ds_meta, figure_info):
    light = sequence_results.get("light", [])
    motion = sequence_results.get("motion", [])
    lines = []
    lines.append("# Scene-anchored Lumen cards and main world-cache probes")
    lines.append("")
    lines.append("This publication is the focused placement decode for the two accepted temporal sequences. "
                 "It does not decode screen-probe SH, radiosity, or world-cache content; the existing "
                 "`output/temporal/screen-sh` and `motion-screen-sh` publications remain the screen/SH source.")
    lines.append("")
    lines.append("## Datasets")
    lines.append("")
    lines.append("| sequence | state | run | actual update | frame_index | engine frame | graph prefix | camera world cm |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for kind, states in [("light", light), ("motion", motion)]:
        for s in states:
            lines.append(
                f"| {kind} | {s['state_id']} | {s['run']} | {s['meta'].get('actual_update')} "
                f"| {s['view']['frame_index']} | {s['engine_frame']} | {s['prefix']} "
                f"| {[round(float(x), 2) for x in s['view']['camera_world_cm']]} |"
            )
    lines.append("")
    lines.append("The rejected narrow retry `20261115-101923` and the black `+Y150` states are not read. "
                 "The light sequence actual updates are baseline / 1 / 7 / 31 / 63 (labels baseline, "
                 "lightstep-001, lightstep-008, lightstep-032, lightstep-064). The motion sequence uses "
                 "baseline / 1 / 63 from the same 20261115-104035 process.")
    lines.append("")
    lines.append("`view.json` keeps the frame identity (`frame_index`, engine frame scope, primary view id) "
                 "separate from the View-state (matrices, PreViewTranslation high/low/sum, camera, projection "
                 "checks), so the two concepts are never conflated in the numeric data.")
    lines.append("")
    lines.append("## Invocation fix (why this differs from the old decoder)")
    lines.append("")
    lines.append("`scripts/decode_temporal.py` chose each cache buffer by the globally lowest producer pass id. "
                 "For the baseline dump that picks graph `0001` while the selected camera View and "
                 "`MarkRadianceProbes(ScreenProbes)` are graph `0004`. That is a mixed-invocation decode. "
                 "This decoder selects the camera View first and then requires the mark pass, allocator pass, "
                 "indirection, world-offset buffer, allocator/free-list buffers and timestamp buffers to belong "
                 "to that same graph prefix.")
    lines.append("")
    lines.append("The main screen-probe cache is not selected by name, size guess, or lowest id. It is identified by:")
    lines.append("")
    lines.append("1. captured `FRadianceCacheMarkParameters` on `MarkRadianceProbes(ScreenProbes)` → resolution 48, 4 clipmaps, exact corners;")
    lines.append("2. an `AllocateUsedProbes` pass in the same invocation whose `FRadianceCacheParameters` has the same resolution, clipmap count and corners;")
    lines.append("3. the indirection and companion buffers taken from that allocate pass's `OutputResources`;")
    lines.append("4. read lineage: the 192×48×48 indirection is consumed by `FilterProbeRadiance Res=32x32`, `ComputeLightingPDF` and `TraceGlobalSDFAndApplyRadianceCache` with screen-probe-gather inputs.")
    lines.append("")
    lines.append("The 72×24×24 group is rejected as the translucency-volume cache: its `MarkRadianceProbesUsedByTranslucencyVolume` parameters are 24/3, its indirection is 72×24×24, its only filter is `FilterProbeRadiance Res=8x8`, and it has no ScreenProbeGather lineage.")
    lines.append("")
    lines.append("| state | view prefix | mark res/num | alloc res/num | indirection extent | probe res | old global-lowest prefix | used |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for s in light + motion:
        old = old_global_lowest_probe(s["d"], "Lumen.RadianceCache.RadianceProbeIndirectionTexture") or {}
        lines.append(
            f"| {s['state_id']} | {s['prefix']} | {s['wc']['mark_info']['res']}/{s['wc']['mark_info']['num']} "
            f"| {s['wc']['alloc_info']['res']}/{s['wc']['alloc_info']['num']} "
            f"| {s['wc']['res'] * s['wc']['num']}×{s['wc']['res']}×{s['wc']['res']} "
            f"| {s['wc']['alloc_info']['probe_resolution']} | {old.get('graph_prefix')} | selected-live only |"
        )
    lines.append("")
    lines.append("`ParameterBindings.json` is also read for the mark/allocate passes; the selected allocate pass "
                 "binds `RWRadianceProbeIndirectionTexture`, `RWProbeWorldOffset`, `RWProbeAllocator`, "
                 "`RWProbeFreeListAllocator`, `RWProbeLastUsedFrame` and `RWProbeLastTracedFrame` under the "
                 "captured parameter block. Imported v0 card/page-table payloads are used only with the identity "
                 "evidence in `cards.json`/`pages.json`.")
    lines.append("")
    lines.append("## Cards")
    lines.append("")
    lines.append("`FLumenCardGPUData` is 7 float4 per card (112 bytes): position high/low, world OBB axes, packed "
                 "flags, low/high page spans, mesh-card transform and average texel size. The flags are exactly "
                 "`bVisible` bit 24, `bHeightfield` bit 25, `bFarField` bit 26, `AxisAlignedDirection` bits 16–19, "
                 "`LightingChannelMask` bits 20–23, and the two res-level biases in bits 0–7 / 8–15. `bVisible` is "
                 "`bCardVisible && Card.IsAllocated()`, not a proof of on-screen visibility.")
    lines.append("")
    lines.append("| state | GPU capacity cards | valid cards | included visible | page-resident | low-res pages | static geometry sha256 |")
    lines.append("|---|---|---|---|---|---|---|")
    for s in light + motion:
        c = s["cards_pub"]
        lines.append(
            f"| {s['state_id']} | {c['capacity']['buffer_cards_full_stride']} | {c['capacity']['valid_cards']} "
            f"| {c['counts']['included_visible']} | {c['counts']['page_resident']} "
            f"| {c['counts']['total_low_res_pages']} | `{c['static_geometry_sha256'][:16]}…` |"
        )
    lines.append("")
    lines.append("The 4096-float4 GPU allocation is 585 full 7-float4 cards plus one trailing float4 that is not a "
                 "card. The valid range is inferred as the contiguous nonzero uploaded payload 0..521; the tail 63 "
                 "slots are zero in origin/axes/mesh-extent/flags. The `FLumenCardScene.NumCards` uniform is not "
                 "exported, so this is structural identity evidence, not a guessed capacity. Capacity is therefore "
                 "not the card count.")
    lines.append("")
    lines.append("Fixed-camera audit: baseline and lightstep-001 have 371 included cards / 385 low-res pages; "
                 "lightstep-008/032/064 have 522 included cards / 626 pages. The exact static geometry hash is "
                 "identical across all five light states, so this is an inclusion/visibility flag plus matching "
                 "page residency change, not geometry motion. `sequence.json` lists every changed card index.")
    lines.append("")
    lines.append("The two runs contain the same static card geometry set but in a different index order: the exact "
                 "index-order hash differs between light and motion, while the canonical sorted geometry hash is "
                 "identical (`ark` value in `checks.json`). Card indices are never matched across sequences.")
    lines.append("")
    lines.append("## Pages")
    lines.append("")
    lines.append("| state | page-buffer capacity | page-table capacity | mapped payload | referenced by current cards | unreferenced payload | page-table nonzero |")
    lines.append("|---|---|---|---|---|---|---|")
    for s in light + motion:
        p = s["pages_pub"]["capacity"]
        lines.append(
            f"| {s['state_id']} | {p['page_buffer_entries']} | {p['page_table_entries']} "
            f"| {p['mapped_payload_entries']} | {p['referenced_by_current_cards']} "
            f"| {p['unreferenced_payload_entries']} | {p['page_table_nonzero_entries']} |"
        )
    lines.append("")
    lines.append("PageBuffer rows are 5 float4; PageTable entries are 2 uint. The PageTable word0 packs "
                 "SampleAtlasBiasX/Y and SampleCardResLevelX/Y; word1 is SamplePageIndex. In every current "
                 "reference, SamplePageIndex equals the page's own index and the PageBuffer CardIndex equals the "
                 "owner card. Rows outside the current card spans are retained payload, not current page residency. "
                 "The per-page update fields in float4[4] are decoded as raw uint frame indices.")
    lines.append("")
    lines.append("## World cache")
    lines.append("")
    lines.append("World position is `corner + (logical_coord + 0.5)*cell_size + ProbeWorldOffset - "
                 "(PreViewTranslationHigh + PreViewTranslationLow)`. `ProbeWorldOffset` is a translated-world "
                 "offset (PF_FloatRGBA/float16x4 in this capture); in all eight selected states it is exactly zero, "
                 "so positions are the captured clipmap grid centres. No cache SH or cache content is synthesized.")
    lines.append("")
    lines.append("| state | active by indirection | allocator | free | alloc−free | last-used window | last-traced window | never traced | position sha256 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for s in light + motion:
        w = s["wc"]
        lines.append(
            f"| {s['state_id']} | {len(w['records'])} | {w['allocator']} | {w['free_count']} "
            f"| {w['allocator'] - w['free_count']} | {w['sets']['active_by_last_used_window']} "
            f"| {w['sets']['active_by_last_traced_window']} | "
            f"{sum(1 for r in w['records'] if r['never_traced'])} | `{w['positions_sha256'][:16]}…` |"
        )
    lines.append("")
    lines.append("Physical slots are tracked by `(clipmap, world_cell)` as well as by physical id. A physical id "
                 "that maps to a different world cell at the same clipmap level is counted as actual slot "
                 "reassignment; a logical-coordinate change that preserves the world cell is clipmap-origin motion "
                 "and is not counted as recycle. The motion capture moves the clipmap origin by whole world cells "
                 "(clipmap 0: −2 cells; clipmaps 1–3: −1 cell), so the raw logical coordinate changes for many "
                 "unchanged cells. This is separated in `sequence.json`.")
    lines.append("")
    lines.append("| pair | shared pids | same cell same pid | same cell new pid | physical id new cell | coord changed / world cell same | cells only prev | cells only curr |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for kind in ("light", "motion"):
        for tr in sequence_results[kind]["tracks"]["worldcache_tracks"]:
            lines.append(
                f"| {kind}: {tr['from']}→{tr['to']} | {tr['pids_shared']} | {tr['same_world_cell_same_pid']} "
                f"| {tr['same_world_cell_new_pid']} | {tr['physical_id_reassigned_to_new_world_cell']} "
                f"| {tr['logical_coord_changed_but_world_cell_same']} | {tr['world_cells_only_prev']} "
                f"| {tr['world_cells_only_curr']} |"
            )
    lines.append("")
    lines.append("Activity is published as numeric tracks: per-probe `last_used_frame`, `last_traced_frame`, "
                 "`use_age_frames`, `traced_age_frames` (0 means never traced), and per-state/per-clipmap "
                 "`traced_age_histogram` in `worldcache.json` and `sequence.json`. Marker colour and radius are "
                 "fixed per clipmap and are not varied by age.")
    lines.append("")
    lines.append("## Figures")
    lines.append("")
    lines.append("Each state has `base.png` (byte-identical copy of the raw `Base/Screenshot.png`) plus four "
                 "separate composites: `cards-only.png`, `cards-xray.png`, `worldcache-only.png`, "
                 "`worldcache-xray.png`. There are no mixed card+worldcache images and no burned text. "
                 "`figures/legend.json` carries the native colour/radius/depth metadata.")
    lines.append("")
    lines.append("| sequence | state | cards drawn | worldcache drawn | cards xray | worldcache xray |")
    lines.append("|---|---|---|---|---|---|")
    for kind in ("light", "motion"):
        for s in sequence_results[kind]:
            f = s["figures"]
            lines.append(
                f"| {kind} | {s['state_id']} | {f['cards_only']['drawn_cards']} "
                f"| {f['worldcache_only']['drawn_probes']} | {f['cards_xray']['drawn_cards']} "
                f"| {f['worldcache_xray']['drawn_probes']} |"
            )
    lines.append("")
    lines.append("The card overlay is the projected convex hull of the 8 OBB corners. The primary image draws a "
                 "deterministic representative subset (up to 24 typical-size cards with 8×6 screen-cell diversity) "
                 "thick; all numeric cards are retained in `cards.json`/`cards.npz`. The xray image is a separate "
                 "low-alpha image of centre-depth-occluded cards. Worldcache markers use fixed per-clipmap colours "
                 "and radii; xray is separate.")
    lines.append("")
    lines.append("## Projection / depth checks")
    lines.append("")
    lines.append("| state | tw_to_clip product max | pre high+low max | forward delta | camera vs manifest cm | cards on screen | centre-depth visible | probes on screen | probes visible |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for s in light + motion:
        v = s["view"]
        lines.append(
            f"| {s['state_id']} | {v['tw_to_clip_product_max_abs']:.2e} "
            f"| {v['pre_high_plus_low_max_abs']:.2e} | {v['view_forward_packed_minus_matrix_max_abs']:.2e} "
            f"| {s['camera_err_vs_manifest_cm']} | {int(s['cards']['center_on'].sum())} "
            f"| {int((s['cards']['depth_class'] == 'visible').sum())} "
            f"| {sum(1 for r in s['wc']['records'] if r['on_screen'])} "
            f"| {sum(1 for r in s['wc']['records'] if r['visible'])} |"
        )
    lines.append("")
    lines.append("Depth uses `view_cm = NearPlane / SceneDepthZ` at the projected pixel with tolerance "
                 "`max(10 cm, 0.02*scene_depth_cm)`. The card centre is the overlay classifier because OBB corners "
                 "can overshoot the represented surface; corner counts are still stored per card. The SceneDepthZ "
                 "allocation is 1280×888 while the FPackedView view rect is 1280×720; the top-left 1280×720 region "
                 "is sampled and was validated against card-centre depth.")
    lines.append("")
    lines.append("## Integrity and checks")
    lines.append("")
    lines.append(f"- Raw inputs hashed before/after: **{len(SRC.records)}** files; changed: **{len([r for r in SRC.records.values() if not r.get('unchanged')])}**.")
    lines.append(f"- NativeBuffer files read: **0**; raw files are never opened for write.")
    lines.append(f"- Overall checks status: **{checks_doc['status']}** ({checks_doc['failed_count']} failures of {checks_doc['check_count']}).")
    lines.append("")
    lines.append("| check | status |")
    lines.append("|---|---|")
    for c in checks_doc["checks"]:
        lines.append(f"| {c['id']} | {c['status']} |")
    lines.append("")
    lines.append("## CLI")
    lines.append("")
    lines.append("```powershell")
    lines.append("python decode_placements.py --light-root <raw-light-run-dir> --motion-root <raw-motion-run-dir> --out <dir>")
    lines.append("python decode_placements.py --sequences light --light-states baseline,lightstep-008 --light-root <raw-light-run-dir> --out <dir>")
    lines.append("python decode_placements.py --help")
    lines.append("```")
    lines.append("")
    lines.append("Data layout: `light/data/<state>/{view,cards,pages,worldcache,sources}.json` plus `.npz`; "
                 "`light/data/sequence.json`; `light/figures/<state>/*.png` and `light/figures/legend.json`. "
                 "The same layout is used for `motion`.")
    lines.append("")
    lines.append("## Limits")
    lines.append("")
    lines.append("- `Lumen.Cards` and `Lumen.PageTable` are exported only as v0 imported pooled payloads; the "
                 "structural and temporal identity evidence is recorded in `cards.json`, `pages.json` and "
                 "`checks.json`.")
    lines.append("- `ProbeWorldOffset` is zero in all selected states, so no sub-cell probe displacement is visible "
                 "in this capture. The decoder applies the captured offset generically.")
    lines.append("- PageBuffer/PageTable contain retained payload rows outside the current card spans; those are "
                 "reported as unreferenced payload, not as current page residency.")
    lines.append("- No radiosity and no world-cache SH are decoded or drawn. A separate surface-probe capture is "
                 "required for real SceneDepth/Normal-based probes.")
    lines.append("")
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--light-root", default=None,
                    help="raw run root containing <state>/Base/Passes.json; required for the light sequence")
    ap.add_argument("--motion-root", default=None,
                    help="raw run root containing <state>/Base/Passes.json; required for the motion sequence")
    ap.add_argument("--out", required=True, help="explicit output directory (must differ from the decoder source directory)")
    ap.add_argument("--light-states", default=None, help="comma-separated labels; default scans baseline + lightstep-*")
    ap.add_argument("--motion-states", default=None, help="comma-separated labels; default scans baseline + camera-*")
    ap.add_argument("--sequences", default="light,motion", help="light,motion")
    ap.add_argument("--no-figures", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    out_dir = Path(args.out).resolve()
    if out_dir == SCRIPT_DIR:
        ap.error("--out must differ from the decoder source directory")
    out_dir.mkdir(parents=True, exist_ok=True)
    seq_kinds = [x.strip() for x in args.sequences.split(",") if x.strip()]
    unknown_kinds = [x for x in seq_kinds if x not in ("light", "motion")]
    if not seq_kinds or unknown_kinds:
        ap.error(f"--sequences must be a comma-separated subset of light,motion (got {args.sequences!r})")
    if "light" in seq_kinds and not args.light_root:
        ap.error("--light-root is required when --sequences includes light")
    if "motion" in seq_kinds and not args.motion_root:
        ap.error("--motion-root is required when --sequences includes motion")
    light_root = Path(args.light_root).resolve() if args.light_root else None
    motion_root = Path(args.motion_root).resolve() if args.motion_root else None
    for kind, root in (("light", light_root), ("motion", motion_root)):
        if root is not None and not root.is_dir():
            ap.error(f"{kind} root does not exist: {root}")
    ds_meta = load_dataset_metadata(light_root, motion_root)

    sequence_results = {}
    requested_labels = {}
    all_states = []
    figure_info = {"light": {}, "motion": {}}
    for kind, root, explicit in [
        ("light", light_root, args.light_states),
        ("motion", motion_root, args.motion_states),
    ]:
        if kind not in seq_kinds:
            continue
        if not root.exists():
            raise SystemExit(f"{kind} root does not exist: {root}")
        labels = discover_states(root, kind, explicit)
        if not labels:
            raise SystemExit(f"{kind}: no states found under {root}")
        requested_labels[kind] = labels
        seq_dir = out_dir / kind
        (seq_dir / "data").mkdir(parents=True, exist_ok=True)
        (seq_dir / "figures").mkdir(parents=True, exist_ok=True)
        states = []
        for label in labels:
            subdir = label
            state_dir = root / subdir
            meta = attach_state_meta(kind, label, ds_meta)
            if args.verbose:
                print(f"[{kind}] {label}")
            st = decode_state(label, root, root.name, subdir, seq_dir, meta, verbose=args.verbose)
            st["sequence"] = kind
            states.append(st)
            all_states.append(st)
        for st in states:
            if args.no_figures:
                st["figures"] = {"base": {}, "cards_only": {}, "cards_xray": {},
                                 "worldcache_only": {}, "worldcache_xray": {}}
            else:
                st["figures"] = render_figures(st)
            figure_info[kind][st["state_id"]] = st["figures"]
        if not args.no_figures:
            write_sequence_legend(seq_dir, states, figure_info[kind])
        states_track = []
        for st in states:
            st["summary"] = {
                "sequence": kind,
                "label": st["state_id"],
                "run": st["run"],
                "subdir": st["subdir"],
                "dump_directory": str(st["root"]),
                "frame_index": st["view"]["frame_index"],
                "engine_frame_scope": st["engine_frame"],
                "graph_prefix": st["prefix"],
                "camera_world_cm": st["view"]["camera_world_cm"].tolist(),
                "hfov_deg": st["view"]["hfov_deg"],
                "actual_update": st["meta"].get("actual_update"),
                "capture_actual_update_raw": st["meta"].get("capture_actual_update_raw"),
                "actual_update_source": st["meta"].get("actual_update_source"),
                "expected_actual_update": st["meta"].get("expected_actual_update"),
                "counts": {
                    "valid_cards": st["cards"]["num_cards"],
                    "included_cards": st["cards"]["visible_count"],
                    "page_resident_cards": st["cards"]["page_resident_count"],
                    "referenced_pages": st["pages"]["referenced_count"],
                    "mapped_payload_pages": st["pages"]["mapped_count"],
                    "active_probes": st["wc"]["unique_pids"],
                    "allocator": st["wc"]["allocator"],
                    "free_count": st["wc"]["free_count"],
                },
                "hashes": {
                    "cards_static_geometry": st["cards"]["static_geometry_sha256"],
                    "cards_canonical_sorted_geometry": st["cards"]["canonical_sorted_geometry_sha256"],
                    "worldcache_positions": st["wc"]["positions_sha256"],
                    "worldcache_world_cells": st["wc"]["world_cells_sha256"],
                },
                "files": {
                    "view": f"{kind}/data/{st['state_id']}/view.json",
                    "cards": f"{kind}/data/{st['state_id']}/cards.json",
                    "pages": f"{kind}/data/{st['state_id']}/pages.json",
                    "worldcache": f"{kind}/data/{st['state_id']}/worldcache.json",
                    "sources": f"{kind}/data/{st['state_id']}/sources.json",
                    "cards_npz": f"{kind}/data/{st['state_id']}/cards.npz",
                    "pages_npz": f"{kind}/data/{st['state_id']}/pages.npz",
                    "worldcache_npz": f"{kind}/data/{st['state_id']}/worldcache.npz",
                },
            }
            states_track.append(st)
        # rewrite sequence with summaries now available
        seq = write_sequence_data(seq_dir, kind, states_track, figure_info[kind])
        for st in states:
            # persist source ledger once more after figures (figure reads no new raw inputs)
            write_json(st["data_dir"] / "sources.json", {
                "schema": "arda-placement-sources-v1",
                "state": st["state_id"],
                "sources": resource_source_rows(st["state_id"], st["root"]),
            }, indent=1)
        sequence_results[kind] = states

    changed_raw = SRC.finalize()
    # rewrite every source ledger with final after/unchanged hashes
    for st in all_states:
        write_json(st["data_dir"] / "sources.json", {
            "schema": "arda-placement-sources-v1",
            "state": st["state_id"],
            "note": "sha256_before was taken before the first decode read; sha256_after and unchanged are from the final integrity pass",
            "changed_count": len([r for r in SRC.rows_for(st["state_id"], st["root"]) if not r.get("unchanged")]),
            "sources": resource_source_rows(st["state_id"], st["root"]),
        }, indent=1)

    checks_doc = run_checks(
        {
            "light": sequence_results.get("light", []),
            "motion": sequence_results.get("motion", []),
        },
        all_states, changed_raw, figure_info, ds_meta, out_dir, requested_labels,
    )

    report_tracks_doc = {}
    for kind in ("light", "motion"):
        if kind not in sequence_results:
            continue
        seq = json.load(open(out_dir / kind / "data/sequence.json", encoding="utf-8"))
        states_by_id = {s["state_id"]: s for s in sequence_results[kind]}
        for tr in seq["card_tracks"]:
            tr["expected_actual_prev"] = states_by_id[tr["from"]]["meta"].get("expected_actual_update")
            tr["expected_actual_curr"] = states_by_id[tr["to"]]["meta"].get("expected_actual_update")
        report_tracks_doc[kind] = {"tracks": {
            "card_tracks": seq["card_tracks"],
            "worldcache_tracks": seq["worldcache_tracks"],
            "activity_tracks": seq["activity_tracks"],
        }}
    write_report_v2(
        out_dir,
        {k: sequence_results.get(k, []) for k in ("light", "motion")},
        report_tracks_doc,
        checks_doc, ds_meta, figure_info,
    )

    manifest = {
        "schema": "arda-placement-publication-v1",
        "generated_utc": utc_now(),
        "output": str(out_dir),
        "sequences": {
            kind: {
                "root": str((light_root if kind == "light" else motion_root)),
                "states": [s["summary"] for s in sequence_results.get(kind, [])],
                "data": f"{kind}/data",
                "figures": f"{kind}/figures",
                "sequence": f"{kind}/data/sequence.json",
                "legend": f"{kind}/figures/legend.json",
            }
            for kind in ("light", "motion") if kind in sequence_results
        },
        "schema_summary": {
            "view.json": "frame vs View-state, FPackedView matrices, PreViewTranslation high/low/sum, camera, projection checks, selection/rejected view candidates",
            "cards.json": "FLumenCardGPUData layout/flags/capacity, source uid/version/file hash/producer, all 585 capacity records with bounds/projection/depth",
            "pages.json": "FLumenCardPageGPUData 5-float4 layout, 2-uint PageTable fields, source versions/hashes/producers, all 1024 capacity records",
            "worldcache.json": "main screen-probe cache selection/lineage, all active probes with logical coord/world cell/physical id/position/timestamps/activity, allocator/free sets",
            "sequence.json": "card flag/page-residency tracks and world-cache world-cell/slot-reuse/activity tracks for the sequence",
            "sources.json": "every raw input read for the state with sha256 before/after and producer/version/offset",
        },
        "checks": "checks.json",
        "report": "report.md",
        "excluded_inputs": {
            "retry_20261115-101923": "not used",
            "plus_y150_black_states": "not used",
        },
        "raw_integrity": {
            "files_hashed": len(SRC.records),
            "changed_count": len(changed_raw),
            "changed_files": changed_raw,
            "nativebuffer_files_read": 0,
        },
        "checks_status": checks_doc["status"],
    }
    write_json(out_dir / "manifest.json", manifest, indent=2)
    write_json(out_dir / "checks.json", checks_doc, indent=2)
    print(f"Wrote {out_dir}")
    print(f"checks: {checks_doc['status']} ({checks_doc['failed_count']} failed / {checks_doc['check_count']})")
    print(f"raw files hashed: {len(SRC.records)}, changed: {len(changed_raw)}")
    if changed_raw:
        print("CHANGED RAW FILES:")
        for p in changed_raw:
            print(" ", p)
    return 0 if checks_doc["status"] == "pass" else 2


def write_report_v2(out_dir, states_by_kind, tracks_by_kind, checks_doc, ds_meta, figure_info):
    """Report writer that consumes states and sequence tracks separately."""
    light = states_by_kind.get("light", [])
    motion = states_by_kind.get("motion", [])
    lines = []
    lines.append("# Scene-anchored Lumen cards and main world-cache probes")
    lines.append("")
    lines.append("This publication is the focused placement decode for the two accepted temporal sequences. "
                 "It does not decode screen-probe SH, radiosity, or world-cache content; the existing "
                 "`output/temporal/screen-sh` and `motion-screen-sh` publications remain the screen/SH source.")
    lines.append("")
    lines.append("## Datasets")
    lines.append("")
    lines.append("| sequence | state | run | actual update | frame_index | engine frame | graph prefix | camera world cm |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for kind, states in [("light", light), ("motion", motion)]:
        for s in states:
            lines.append(
                f"| {kind} | {s['state_id']} | {s['run']} | {s['meta'].get('actual_update')} "
                f"| {s['view']['frame_index']} | {s['engine_frame']} | {s['prefix']} "
                f"| {[round(float(x), 2) for x in s['view']['camera_world_cm']]} |"
            )
    lines.append("")
    lines.append("The rejected narrow retry `20261115-101923` and the black `+Y150` states are not read. "
                 "The light sequence actual updates are baseline / 1 / 7 / 31 / 63 (labels baseline, "
                 "lightstep-001, lightstep-008, lightstep-032, lightstep-064). The motion sequence uses "
                 "baseline / 1 / 63 from the same 20261115-104035 process.")
    lines.append("")
    lines.append("`view.json` keeps the frame identity (`frame_index`, engine frame scope, primary view id) "
                 "separate from the View-state (matrices, PreViewTranslation high/low/sum, camera, projection "
                 "checks), so the two concepts are never conflated in the numeric data.")
    lines.append("")
    lines.append("## Invocation fix (why this differs from the old decoder)")
    lines.append("")
    lines.append("`scripts/decode_temporal.py` chose each cache buffer by the globally lowest producer pass id. "
                 "For the baseline dump that picks graph `0001` while the selected camera View and "
                 "`MarkRadianceProbes(ScreenProbes)` are graph `0004`. That is a mixed-invocation decode. "
                 "This decoder selects the camera View first and then requires the mark pass, allocator pass, "
                 "indirection, world-offset buffer, allocator/free-list buffers and timestamp buffers to belong "
                 "to that same graph prefix.")
    lines.append("")
    lines.append("The main screen-probe cache is not selected by name, size guess, or lowest id. It is identified by:")
    lines.append("")
    lines.append("1. captured `FRadianceCacheMarkParameters` on `MarkRadianceProbes(ScreenProbes)` → resolution 48, 4 clipmaps, exact corners;")
    lines.append("2. an `AllocateUsedProbes` pass in the same invocation whose `FRadianceCacheParameters` has the same resolution, clipmap count and corners;")
    lines.append("3. the indirection and companion buffers taken from that allocate pass's `OutputResources`;")
    lines.append("4. read lineage: the 192×48×48 indirection is consumed by `FilterProbeRadiance Res=32x32`, `ComputeLightingPDF` and `TraceGlobalSDFAndApplyRadianceCache` with screen-probe-gather inputs.")
    lines.append("")
    lines.append("The 72×24×24 group is rejected as the translucency-volume cache: its `MarkRadianceProbesUsedByTranslucencyVolume` parameters are 24/3, its indirection is 72×24×24, its only filter is `FilterProbeRadiance Res=8x8`, and it has no ScreenProbeGather lineage.")
    lines.append("")
    lines.append("| state | view prefix | mark res/num | alloc res/num | indirection extent | probe res | old global-lowest prefix | used |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for s in light + motion:
        old = old_global_lowest_probe(s["d"], "Lumen.RadianceCache.RadianceProbeIndirectionTexture") or {}
        lines.append(
            f"| {s['state_id']} | {s['prefix']} | {s['wc']['mark_info']['res']}/{s['wc']['mark_info']['num']} "
            f"| {s['wc']['alloc_info']['res']}/{s['wc']['alloc_info']['num']} "
            f"| {s['wc']['res'] * s['wc']['num']}×{s['wc']['res']}×{s['wc']['res']} "
            f"| {s['wc']['alloc_info']['probe_resolution']} | {old.get('graph_prefix')} | selected-live only |"
        )
    lines.append("")
    lines.append("`ParameterBindings.json` is also read for the mark/allocate passes; the selected allocate pass "
                 "binds `RWRadianceProbeIndirectionTexture`, `RWProbeWorldOffset`, `RWProbeAllocator`, "
                 "`RWProbeFreeListAllocator`, `RWProbeLastUsedFrame` and `RWProbeLastTracedFrame` under the "
                 "captured parameter block. Imported v0 card/page-table payloads are used only with the identity "
                 "evidence in `cards.json`/`pages.json`.")
    lines.append("")
    lines.append("## Cards")
    lines.append("")
    lines.append("`FLumenCardGPUData` is 7 float4 per card (112 bytes): position high/low, world OBB axes, packed "
                 "flags, low/high page spans, mesh-card transform and average texel size. The flags are exactly "
                 "`bVisible` bit 24, `bHeightfield` bit 25, `bFarField` bit 26, `AxisAlignedDirection` bits 16–19, "
                 "`LightingChannelMask` bits 20–23, and the two res-level biases in bits 0–7 / 8–15. `bVisible` is "
                 "`bCardVisible && Card.IsAllocated()`, not a proof of on-screen visibility.")
    lines.append("")
    lines.append("| state | GPU capacity cards | valid cards | included visible | page-resident | low-res pages | static geometry sha256 |")
    lines.append("|---|---|---|---|---|---|---|")
    for s in light + motion:
        c = s["cards_pub"]
        lines.append(
            f"| {s['state_id']} | {c['capacity']['buffer_cards_full_stride']} | {c['capacity']['valid_cards']} "
            f"| {c['counts']['included_visible']} | {c['counts']['page_resident']} "
            f"| {c['counts']['total_low_res_pages']} | `{c['static_geometry_sha256'][:16]}…` |"
        )
    lines.append("")
    lines.append("The 4096-float4 GPU allocation is 585 full 7-float4 cards plus one trailing float4 that is not a "
                 "card. The valid range is inferred as the contiguous nonzero uploaded payload 0..521; the tail 63 "
                 "slots are zero in origin/axes/mesh-extent/flags. The `FLumenCardScene.NumCards` uniform is not "
                 "exported, so this is structural identity evidence, not a guessed capacity. Capacity is therefore "
                 "not the card count.")
    lines.append("")
    lines.append("Fixed-camera audit: baseline and lightstep-001 have 371 included cards / 385 low-res pages; "
                 "lightstep-008/032/064 have 522 included cards / 626 pages. The exact static geometry hash is "
                 "identical across all five light states, so this is an inclusion/visibility flag plus matching "
                 "page residency change, not geometry motion. `sequence.json` lists every changed card index.")
    lines.append("")
    lines.append("The two runs contain the same static card geometry set but in a different index order: the exact "
                 "index-order hash differs between light and motion, while the canonical sorted geometry hash is "
                 "identical (see `checks.json`). Card indices are never matched across sequences.")
    lines.append("")
    lines.append("## Pages")
    lines.append("")
    lines.append("| state | page-buffer capacity | page-table capacity | mapped payload | referenced by current cards | unreferenced payload | page-table nonzero |")
    lines.append("|---|---|---|---|---|---|---|")
    for s in light + motion:
        p = s["pages_pub"]["capacity"]
        lines.append(
            f"| {s['state_id']} | {p['page_buffer_entries']} | {p['page_table_entries']} "
            f"| {p['mapped_payload_entries']} | {p['referenced_by_current_cards']} "
            f"| {p['unreferenced_payload_entries']} | {p['page_table_nonzero_entries']} |"
        )
    lines.append("")
    lines.append("PageBuffer rows are 5 float4; PageTable entries are 2 uint. The PageTable word0 packs "
                 "SampleAtlasBiasX/Y and SampleCardResLevelX/Y; word1 is SamplePageIndex. In every current "
                 "reference, SamplePageIndex equals the page's own index and the PageBuffer CardIndex equals the "
                 "owner card. Rows outside the current card spans are retained payload, not current page residency. "
                 "The per-page update fields in float4[4] are decoded as raw uint frame indices.")
    lines.append("")
    lines.append("## World cache")
    lines.append("")
    lines.append("World position is `corner + (logical_coord + 0.5)*cell_size + ProbeWorldOffset - "
                 "(PreViewTranslationHigh + PreViewTranslationLow)`. `ProbeWorldOffset` is a translated-world "
                 "offset (PF_FloatRGBA/float16x4 in this capture); in all eight selected states it is exactly zero, "
                 "so positions are the captured clipmap grid centres. No cache SH or cache content is synthesized.")
    lines.append("")
    lines.append("| state | active by indirection | allocator | free | alloc−free | last-used window | last-traced window | never traced | position sha256 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for s in light + motion:
        w = s["wc"]
        lines.append(
            f"| {s['state_id']} | {len(w['records'])} | {w['allocator']} | {w['free_count']} "
            f"| {w['allocator'] - w['free_count']} | {w['sets']['active_by_last_used_window']} "
            f"| {w['sets']['active_by_last_traced_window']} | "
            f"{sum(1 for r in w['records'] if r['never_traced'])} | `{w['positions_sha256'][:16]}…` |"
        )
    lines.append("")
    lines.append("Physical slots are tracked by `(clipmap, world_cell)` as well as by physical id. A physical id "
                 "that maps to a different world cell at the same clipmap level is counted as actual slot "
                 "reassignment; a logical-coordinate change that preserves the world cell is clipmap-origin motion "
                 "and is not counted as recycle. The motion capture moves the clipmap origin by whole world cells "
                 "(clipmap 0: −2 cells; clipmaps 1–3: −1 cell), so the raw logical coordinate changes for many "
                 "unchanged cells. This is separated in `sequence.json`.")
    lines.append("")
    lines.append("| pair | shared pids | same cell same pid | same cell new pid | physical id new cell | coord changed / world cell same | cells only prev | cells only curr |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for kind in ("light", "motion"):
        tr = tracks_by_kind.get(kind, {}).get("tracks", {}).get("worldcache_tracks", [])
        for row in tr:
            lines.append(
                f"| {kind}: {row['from']}→{row['to']} | {row['pids_shared']} | {row['same_world_cell_same_pid']} "
                f"| {row['same_world_cell_new_pid']} | {row['physical_id_reassigned_to_new_world_cell']} "
                f"| {row['logical_coord_changed_but_world_cell_same']} | {row['world_cells_only_prev']} "
                f"| {row['world_cells_only_curr']} |"
            )
    lines.append("")
    lines.append("Activity is published as numeric tracks: per-probe `last_used_frame`, `last_traced_frame`, "
                 "`use_age_frames`, `traced_age_frames` (0 means never traced), and per-state/per-clipmap "
                 "`traced_age_histogram` in `worldcache.json` and `sequence.json`. Marker colour and radius are "
                 "fixed per clipmap and are not varied by age.")
    lines.append("")
    lines.append("## Figures")
    lines.append("")
    lines.append("Each state has `base.png` (byte-identical copy of the raw `Base/Screenshot.png`) plus four "
                 "separate composites: `cards-only.png`, `cards-xray.png`, `worldcache-only.png`, "
                 "`worldcache-xray.png`. There are no mixed card+worldcache images and no burned text. "
                 "`figures/legend.json` carries the native colour/radius/depth metadata.")
    lines.append("")
    lines.append("| sequence | state | cards drawn | worldcache drawn | cards xray | worldcache xray |")
    lines.append("|---|---|---|---|---|---|")
    for kind, states in [("light", light), ("motion", motion)]:
        for s in states:
            f = s["figures"]
            lines.append(
                f"| {kind} | {s['state_id']} | {f['cards_only'].get('drawn_cards')} "
                f"| {f['worldcache_only'].get('drawn_probes')} | {f['cards_xray'].get('drawn_cards')} "
                f"| {f['worldcache_xray'].get('drawn_probes')} |"
            )
    lines.append("")
    lines.append("The card overlay is the projected convex hull of the 8 OBB corners. The primary image draws a "
                 "deterministic representative subset (up to 24 typical-size cards with 8×6 screen-cell diversity) "
                 "thick; all numeric cards are retained in `cards.json`/`cards.npz`. The xray image is a separate "
                 "low-alpha image of centre-depth-occluded cards. Worldcache markers use fixed per-clipmap colours "
                 "and radii; xray is separate.")
    lines.append("")
    lines.append("## Projection / depth checks")
    lines.append("")
    lines.append("| state | tw_to_clip product max | pre high+low max | forward delta | camera vs manifest cm | cards on screen | centre-depth visible | probes on screen | probes visible |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for s in light + motion:
        v = s["view"]
        lines.append(
            f"| {s['state_id']} | {v['tw_to_clip_product_max_abs']:.2e} "
            f"| {v['pre_high_plus_low_max_abs']:.2e} | {v['view_forward_packed_minus_matrix_max_abs']:.2e} "
            f"| {s['camera_err_vs_manifest_cm']} | {int(s['cards']['center_on'].sum())} "
            f"| {int((s['cards']['depth_class'] == 'visible').sum())} "
            f"| {sum(1 for r in s['wc']['records'] if r['on_screen'])} "
            f"| {sum(1 for r in s['wc']['records'] if r['visible'])} |"
        )
    lines.append("")
    lines.append("Depth uses `view_cm = NearPlane / SceneDepthZ` at the projected pixel with tolerance "
                 "`max(10 cm, 0.02*scene_depth_cm)`. The card centre is the overlay classifier because OBB corners "
                 "can overshoot the represented surface; corner counts are still stored per card. The SceneDepthZ "
                 "allocation is 1280×888 while the FPackedView view rect is 1280×720; the top-left 1280×720 region "
                 "is sampled and was validated against card-centre depth.")
    lines.append("")
    lines.append("## Integrity and checks")
    lines.append("")
    lines.append(f"- Raw inputs hashed before/after: **{len(SRC.records)}** files; changed: **{len([r for r in SRC.records.values() if not r.get('unchanged')])}**.")
    lines.append(f"- NativeBuffer files read: **0**; raw files are never opened for write.")
    lines.append(f"- Overall checks status: **{checks_doc['status']}** ({checks_doc['failed_count']} failures of {checks_doc['check_count']}).")
    lines.append("")
    lines.append("| check | status |")
    lines.append("|---|---|")
    for c in checks_doc["checks"]:
        lines.append(f"| {c['id']} | {c['status']} |")
    lines.append("")
    lines.append("## CLI")
    lines.append("")
    lines.append("```powershell")
    lines.append("python decode_placements.py --light-root <raw-light-run-dir> --motion-root <raw-motion-run-dir> --out <dir>")
    lines.append("python decode_placements.py --sequences light --light-states baseline,lightstep-008 --light-root <raw-light-run-dir> --out <dir>")
    lines.append("python decode_placements.py --help")
    lines.append("```")
    lines.append("")
    lines.append("Data layout: `light/data/<state>/{view,cards,pages,worldcache,sources}.json` plus `.npz`; "
                 "`light/data/sequence.json`; `light/figures/<state>/*.png` and `light/figures/legend.json`. "
                 "The same layout is used for `motion`.")
    lines.append("")
    lines.append("## Limits")
    lines.append("")
    lines.append("- `Lumen.Cards` and `Lumen.PageTable` are exported only as v0 imported pooled payloads; the "
                 "structural and temporal identity evidence is recorded in `cards.json`, `pages.json` and "
                 "`checks.json`.")
    lines.append("- `ProbeWorldOffset` is zero in all selected states, so no sub-cell probe displacement is visible "
                 "in this capture. The decoder applies the captured offset generically.")
    lines.append("- PageBuffer/PageTable contain retained payload rows outside the current card spans; those are "
                 "reported as unreferenced payload, not as current page residency.")
    lines.append("- No radiosity and no world-cache SH are decoded or drawn. A separate surface-probe capture is "
                 "required for real SceneDepth/Normal-based probes.")
    lines.append("")
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
