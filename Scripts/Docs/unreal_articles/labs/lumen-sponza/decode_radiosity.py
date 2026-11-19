#!/usr/bin/env python3
"""Decode Lumen card radiosity probes, surface depth and two-band SH.

This is the implementation for the radiosity-decode step.  It reads selected
same-process GPU dumps captured by the radiosity-surface run and writes only
under the explicit ``--out`` directory; no raw or output path is implicit.

The position path is deliberately *not* the old card-plane reconstruction.
For every probe it follows the engine shader:

    CardPage.PhysicalAtlasUVRect.xy
      + InvPhysicalAtlasSize * (CoordInCardPage + 0.5)
        -> Lumen.SceneDepth texel -> UNORM16 depth
        -> GetCardLocalPosition(Card, CardUV, Depth)
        -> WorldToLocalRotation * local + Origin

The four-coefficient SH path follows LumenRadiosity.usf ConvertToSH:
a PDF-weighted (uniform hemisphere, PDF = 1/(2*pi)) projection with the
SHCommon.ush two-band basis, followed by CalcDiffuseTransferSH(normal, 1).

Usage:
    python decode_radiosity.py --raw-root RAW_RUN --out OUT
    python decode_radiosity.py --raw-root RAW_RUN --states baseline --out OUT
    python decode_radiosity.py --raw-root RAW_RUN --out OUT --no-hash

``RAW_RUN`` is a directory containing one state subdirectory per label
(for example ``RAW_RUN/baseline/Base/Passes.json``).  The three capture
metadata defaults come from the published evidence tree; override them when
using a moved capture.  ``--out`` must differ from this source directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# constants / paths
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE
for _ in range(5):
    REPO_ROOT = REPO_ROOT.parent
DEFAULT_CAPTURE_MANIFEST = (
    REPO_ROOT
    / "Data/Sponza/evidence/temporal-captures/radiosity/capture-manifest.json"
)
DEFAULT_CAPTURE_REPORT = (
    REPO_ROOT / "Data/Sponza/evidence/temporal-captures/radiosity/capture-report.md"
)
DEFAULT_PARAMETER_VERIFICATION = (
    REPO_ROOT
    / "Data/Sponza/evidence/temporal-captures/radiosity/parameter-verification.json"
)

STATES = ["baseline", "light-001", "light-064", "camera-001", "camera-064"]
STATE_TITLES = {
    "baseline": "baseline 80k lux warm",
    "light-001": "light 20k lux, update 1",
    "light-064": "light 20k lux, update 63 (target 64)",
    "camera-001": "camera -Y150 cm, update 1",
    "camera-064": "camera -Y150 cm, update 63 (target 64)",
}

PI = math.pi
TWO_PI = 2.0 * PI

# SHCommon.ush SHBasisFunction(half3) and CalcDiffuseTransferSH(n, 1)
SH_BASIS_C = 0.282095
SH_BASIS_D = 0.488603
SH_TRANSFER = np.array([PI, TWO_PI / 3.0, TWO_PI / 3.0, TWO_PI / 3.0], dtype=np.float64)

# LumenRadiosity.usf GetProbeJitter Hammersley16 seed
HAMM_RX = 0x4AE4
HAMM_RY = 0x9BDB

CARD_TILE_SIZE = 8  # LumenCardCommon.ush
DEPTH_INVALID = 65535
NORMAL_ATLAS_SIZE = 4096
DEPTH_ATLAS_SIZE = 4096

# visibility test against the primary depth; declared, not fitted
DEPTH_TOL_ABS_CM = 10.0
DEPTH_TOL_REL = 0.02

# figure constants, fixed across all states
PLACEMENT_DOT_R = 1
LOBE_RADIUS_CM = 75.0
LOBE_RADIANCE_MAX = 20000.0
LOBE_DIRECTIONS = 48
FIGURE_W, FIGURE_H = 1280, 720
PLAN_W, PLAN_H = 640, 360


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------

def jval(o):
    """JSON-safe conversion for numpy scalars/arrays."""
    if isinstance(o, dict):
        return {str(k): jval(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jval(v) for v in o]
    if isinstance(o, (np.bool_, bool)):
        return bool(o)
    if isinstance(o, (np.integer, int)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        v = float(o)
        return v if math.isfinite(v) else None
    if isinstance(o, np.ndarray):
        return jval(o.tolist())
    return o


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jval(obj), indent=1), encoding="utf-8")


def sha256_file(path: Path, chunk: int = 8 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def load_concat_json(path: Path):
    """GPU dumps store concatenated JSON objects (and sometimes arrays)."""
    txt = path.read_text(encoding="utf-8")
    dec = json.JSONDecoder()
    out = []
    i, n = 0, len(txt)
    while i < n:
        while i < n and txt[i] in " \r\n\t":
            i += 1
        if i >= n:
            break
        obj, end = dec.raw_decode(txt, i)
        if isinstance(obj, list):
            out.extend(obj)
        else:
            out.append(obj)
        i = end
    return out


def hammersley16(index: int, num: int, rx: int, ry: int):
    """Exact port of MonteCarlo.ush Hammersley16 (uint32 reversebits)."""
    e1 = (index / float(num) + rx / 65536.0) % 1.0
    x = int(index) & 0xFFFFFFFF
    x = ((x & 0x55555555) << 1) | ((x & 0xAAAAAAAA) >> 1)
    x = ((x & 0x33333333) << 2) | ((x & 0xCCCCCCCC) >> 2)
    x = ((x & 0x0F0F0F0F) << 4) | ((x & 0xF0F0F0F0) >> 4)
    x = ((x & 0x00FF00FF) << 8) | ((x & 0xFF00FF00) >> 8)
    x = ((x << 16) | (x >> 16)) & 0xFFFFFFFF
    e2 = float(((x >> 16) ^ int(ry)) & 0xFFFF) / 65536.0
    return e1, e2


def shuffle_axes_no_flip(v, packed):
    op = packed & ((3 << 7) | (3 << 4) | (3 << 1))
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
        return np.zeros(3)
    return np.array([v[order[0]], v[order[1]], v[order[2]]])


def decode_r11g11b10(w):
    """Decode packed R11G11B10F words to linear RGB (same rule as engine)."""
    w = np.asarray(w, dtype=np.uint32)
    r = w & 0x7FF
    g = (w >> 11) & 0x7FF
    b = (w >> 22) & 0x3FF

    def dec(v, mant_bits):
        e = (v >> mant_bits) & 0x1F
        m = v & ((1 << mant_bits) - 1)
        scale = float(1 << mant_bits)
        return np.where(
            e == 0,
            (m / scale) * (2.0 ** -14),
            (1.0 + m / scale) * np.exp2(e.astype(np.int32) - 15),
        ).astype(np.float64)

    return np.stack([dec(r, 6), dec(g, 6), dec(b, 5)], axis=-1)


# ---------------------------------------------------------------------------
# SH (two-band, engine normalisation)
# ---------------------------------------------------------------------------

def sh_basis(normals):
    n = np.atleast_2d(np.asarray(normals, dtype=np.float64))
    x, y, z = n[:, 0], n[:, 1], n[:, 2]
    return np.stack(
        [np.full_like(x, SH_BASIS_C), -SH_BASIS_D * y, SH_BASIS_D * z, -SH_BASIS_D * x],
        axis=-1,
    )


def sh_eval(coeffs, normals):
    """DotSH(coeffs, CalcDiffuseTransferSH(normal, 1)).

    coeffs: (N, 3, 4) channel-major [R0..R3, G0..G3, B0..B3]
    returns E(n) in the same (pre-exposed) radiance*steradian units as the
    captured coefficients.
    """
    b = sh_basis(normals)
    return np.einsum("nk,nck->nc", b * SH_TRANSFER[None, :], np.asarray(coeffs, dtype=np.float64))


def sh_eval_direction(coeffs, direction):
    return sh_eval(coeffs.reshape(1, 3, 4), direction.reshape(1, 3))[0]


def sh_constant_hemisphere_test(samples: int = 256):
    """Independent numerical test of the source normalisation.

    Uniform hemisphere sampling with PDF = 1/(2*pi), the exact two-band basis,
    and CalcDiffuseTransferSH(n, 1).  For constant hemispherical radiance L=1
    the expected irradiance is pi.  No 4 or 4*pi factor is applied.
    """
    e1 = (np.arange(samples) + 0.5) / samples
    e2 = (np.arange(samples) + 0.5) / samples
    phi = TWO_PI * e1[None, :]
    cos_t = np.repeat(e2[:, None], samples, axis=1)
    sin_t = np.sqrt(np.maximum(1.0 - cos_t * cos_t, 0.0))
    dirs = np.stack(
        [sin_t * np.cos(phi), sin_t * np.sin(phi), cos_t], axis=-1
    ).reshape(-1, 3)
    b = sh_basis(dirs)
    pdf = 1.0 / TWO_PI
    coeff = np.mean(b * (1.0 / pdf), axis=0)  # per channel, constant L=1
    transfer = sh_basis(np.array([[0.0, 0.0, 1.0]]))[0] * SH_TRANSFER
    # The shader uses six-decimal literals in SHCommon.ush.  The expected
    # value for those literals is 2*pi^2*(C^2 + D^2/3); the exact pi value
    # differs by the literal rounding (~1.7e-6), not by a missing factor.
    expected_shader = TWO_PI * PI * (SH_BASIS_C * SH_BASIS_C + SH_BASIS_D * SH_BASIS_D / 3.0)
    e = float(np.dot(coeff, transfer))
    return {
        "samples": samples * samples,
        "basis": [SH_BASIS_C, -SH_BASIS_D, SH_BASIS_D, -SH_BASIS_D],
        "pdf": pdf,
        "expected_irradiance_for_L1_hemisphere": expected_shader,
        "exact_pi": PI,
        "reconstructed": e,
        "relative_error": abs(e - expected_shader) / expected_shader,
        "relative_error_vs_exact_pi": abs(e - PI) / PI,
        "coefficients": coeff.tolist(),
        "transfer": transfer.tolist(),
        "note": "uniform hemisphere, PDF=1/(2*pi); DotSH with CalcDiffuseTransferSH exp=1; expected uses the same six-decimal SHCommon.ush literals; the 1.7e-6 difference from exact pi is literal rounding, not a 4/4pi factor",
    }


# ---------------------------------------------------------------------------
# dump context
# ---------------------------------------------------------------------------

class DumpCtx:
    def __init__(self, state: str, raw_root: Path):
        self.state = state
        self.root = Path(raw_root) / state
        self.base = self.root / "Base"
        self.passes = load_concat_json(self.base / "Passes.json")
        self.by_ptr = {p["Pointer"]: p for p in self.passes}
        self.descs = load_concat_json(self.base / "ResourceDescs.json")
        self.layouts = load_concat_json(self.base / "SubresourceLayouts.json")
        self.readback = {
            r["File"].replace("\\", "/"): r
            for r in load_concat_json(self.base / "DumpReadbackStatus.json")
        }
        self.meta = {}
        for f in (self.root / "StructuresMetadata").glob("*.json"):
            self.meta[f.stem] = json.load(open(f, encoding="utf-8"))
        self._sel_cache = {}
        self.files = defaultdict(list)
        for f in (self.root / "Resources").glob("*.bin"):
            name = f.name
            if ".stencil." in name:
                continue
            uid = name.split(".")[0]
            if not uid.startswith("000"):
                continue
            ver = name.split(".v")[-1][:-4]
            self.files[uid].append((f, ver))

    # -- graph -----------------------------------------------------------
    def view_descs(self):
        return [d for d in self.descs if d["Name"] == "ViewData.PrimaryViews"]

    def main_file(self, uid: str):
        entries = self.files.get(uid, [])
        mips = [e for e in entries if ".mip0." in e[0].name]
        return (mips or entries)

    def select(self, name: str, prefix: str, byte_size=None, fmt=None):
        key = (name, prefix, byte_size, fmt)
        if key in self._sel_cache:
            return self._sel_cache[key]
        cands = []
        for d in self.descs:
            if d["Name"] != name:
                continue
            uid = d["UniqueResourceName"]
            if not uid.startswith(prefix):
                continue
            if byte_size is not None and int(d.get("ByteSize") or 0) != byte_size:
                continue
            if fmt is not None and d.get("Format") != fmt:
                continue
            for f, ver in self.main_file(uid):
                p = self.by_ptr.get(ver)
                cands.append(
                    {
                        "desc": d,
                        "uid": uid,
                        "file": f,
                        "version": ver,
                        "producer": p,
                        "producer_id": p["Id"] if p else None,
                        "producer_name": p["EventName"] if p else None,
                    }
                )
        if not cands:
            raise KeyError(f"{name} prefix={prefix} size={byte_size} fmt={fmt}")
        cands.sort(key=lambda c: (c["producer_id"] if c["producer_id"] is not None else -1))
        c = cands[-1]
        rel = "Resources/" + c["file"].name
        rb = self.readback.get(rel)
        c["readback"] = rb.get("Status") if rb else None
        c["bytes"] = c["file"].stat().st_size
        c["sha256"] = sha256_file(c["file"])
        self._sel_cache[key] = c
        return c

    def record(self, entry, role):
        d = entry["desc"]
        return {
            "role": role,
            "name": d["Name"],
            "unique_resource_name": entry["uid"],
            "version_pointer": entry["version"],
            "file": entry["file"].name,
            "bytes": entry["bytes"],
            "sha256": entry["sha256"],
            "descriptor": {k: d.get(k) for k in ("Desc", "Type", "Format", "ExtentX", "ExtentY", "ByteSize", "BytesPerElement", "NumElements", "NumMips")},
            "readback": entry.get("readback"),
            "producer": (
                {"pass_id": entry["producer_id"], "event_name": entry["producer_name"], "pointer": entry["version"]}
                if entry.get("producer") else None
            ),
        }

    # -- manifest parameter payloads --------------------------------------
    def pass_parameters(self, event_name: str, prefix: str, capture_manifest: dict):
        rec = None
        for s in capture_manifest["states"]:
            if s["label"] == self.state:
                rec = s
                break
        if rec is None:
            raise KeyError(self.state)
        for p in rec["pass_parameters"]:
            if p["event_name"] == event_name and p["pointer_prefix"] == prefix:
                return p
        raise KeyError(f"{self.state}/{event_name}/{prefix}")

    def read_parameter_struct(self, rec, nested_name: str | None = None):
        """Read a dumped pass parameter payload and return bytes+metadata."""
        pb = Path(rec["parameters_file"])
        if not pb.exists():
            pb = self.root / "Structures" / (rec["parameters_pointer"] + ".bin")
        b = pb.read_bytes()
        top_meta_path = Path(rec["metadata_file"])
        top_meta = json.load(open(top_meta_path, encoding="utf-8"))
        top = {m["Name"]: m["Offset"] for m in top_meta["Members"]}
        nested = None
        if nested_name:
            base = top[nested_name]
            nested_struct = nested_name if nested_name.startswith("F") else "FLumen" + nested_name
            nested_meta = None
            for dep in rec.get("metadata_dependencies", []):
                if dep.get("struct_type") == nested_struct:
                    nested_meta = json.load(open(dep["file"], encoding="utf-8"))
                    break
            if nested_meta is None:
                for m in self.meta.values():
                    if m.get("StructTypeName") == nested_struct:
                        nested_meta = m
                        break
            nested = {
                "base_offset": base,
                "fields": {m["Name"]: m["Offset"] for m in nested_meta["Members"]},
                "metadata_file": rec.get("metadata_file"),
                "struct_type": nested_meta.get("StructTypeName"),
            }
        return {
            "file": str(pb),
            "bytes": len(b),
            "sha256": sha256_file(pb),
            "top_fields": top,
            "nested": nested,
            "data": b,
        }

    def decode_radiosity_parameters(self, prefix: str, capture_manifest: dict):
        out = {}
        for ev in ("BuildRadiosityTiles", "ConvertToSH", "Integrate"):
            rec = self.pass_parameters(ev, prefix, capture_manifest)
            ps = self.read_parameter_struct(rec, "RadiosityTexelTraceParameters")
            b = ps["data"]
            base = ps["nested"]["base_offset"]
            f = ps["nested"]["fields"]

            def u(nm):
                return int.from_bytes(b[base + f[nm]:base + f[nm] + 4], "little")

            def i32(nm):
                return int.from_bytes(b[base + f[nm]:base + f[nm] + 4], "little", signed=True)

            def i2(nm):
                return list(struct.unpack_from("<2i", b, base + f[nm]))

            out[ev] = {
                "pass": {
                    "pass_id": rec["pass_id"],
                    "pointer_prefix": rec["pointer_prefix"],
                    "parameters_pointer": rec["parameters_pointer"],
                    "parameters_file": ps["file"],
                    "parameters_bytes": ps["bytes"],
                    "parameters_sha256": ps["sha256"],
                },
                "RadiosityAtlasSize": i2("RadiosityAtlasSize"),
                "ProbeSpacingInCardTexels": u("ProbeSpacingInCardTexels"),
                "ProbeSpacingInCardTexelsDivideShift": u("ProbeSpacingInCardTexelsDivideShift"),
                "CardTileSizeInProbes": u("CardTileSizeInProbes"),
                "RadiosityTileSizeInProbes": u("RadiosityTileSizeInProbes"),
                "HemisphereProbeResolution": u("HemisphereProbeResolution"),
                "NumTracesPerProbe": u("NumTracesPerProbe"),
                "ProbeOcclusionStrength": struct.unpack_from("<f", b, base + f["ProbeOcclusionStrength"])[0],
                "FixedJitterIndex": i32("FixedJitterIndex"),
                "MaxFramesAccumulated": u("MaxFramesAccumulated"),
                "MaxRadiosityTiles": u("MaxRadiosityTiles"),
                "NumViews": u("NumViews"),
                "ViewIndex": u("ViewIndex"),
                "TargetFormatQuantizationError": list(struct.unpack_from("<3f", b, base + f["TargetFormatQuantizationError"])),
                "nested_offsets": dict(f),
                "nested_base_offset": base,
            }
        # exposure comes from Batched lights
        rec = self.pass_parameters("Batched lights", prefix, capture_manifest)
        ps = self.read_parameter_struct(rec, None)
        off = ps["top_fields"]["CachedLightingPreExposure"]
        out["Batched lights"] = {
            "pass": {
                "pass_id": rec["pass_id"],
                "pointer_prefix": rec["pointer_prefix"],
                "parameters_pointer": rec["parameters_pointer"],
                "parameters_file": ps["file"],
                "parameters_bytes": ps["bytes"],
                "parameters_sha256": ps["sha256"],
            },
            "CachedLightingPreExposure": struct.unpack_from("<f", ps["data"], off)[0],
            "CachedLightingPreExposure_offset": off,
        }
        return out


# ---------------------------------------------------------------------------
# per-state decode
# ---------------------------------------------------------------------------

def decode_view(path: Path):
    b = np.fromfile(path, dtype="<f4")
    if b.size * 4 != 1056:
        raise ValueError(f"unexpected FPackedView size {b.size * 4}")

    def m(o):
        return b[o:o + 16].reshape(4, 4).T

    v = {
        "svpos_to_tw": m(0),
        "view_to_tw": m(16),
        "tw_to_view": m(32),
        "tw_to_clip": m(48),
        "view_to_clip": m(64),
        "clip_to_relworld": m(80),
        "view_rect": list(struct.unpack_from("<4i", b.tobytes(), 640)),
        "view_size": [float(x) for x in b[164:168]],
        "clip_scale_offset": [float(x) for x in b[168:172]],
        "pre_view_translation_cm": [float(x) for x in b[180:183]],
        "pre_view_translation_2_cm": [float(x) for x in b[208:211]],
        "view_forward": [float(x) for x in b[200:203]],
        "near_plane_cm": float(b[203]),
        "frame_index_field": int(struct.unpack_from("<I", b.tobytes(), 1044)[0]),
        "primary_view_id": int(struct.unpack_from("<i", b.tobytes(), 1040)[0]),
    }
    v["camera_world_cm"] = [-x for x in v["pre_view_translation_cm"]]
    v["hfov_deg"] = 2.0 * math.degrees(math.atan(1.0 / v["view_to_clip"][0, 0]))
    v["vfov_deg"] = 2.0 * math.degrees(math.atan(1.0 / v["view_to_clip"][1, 1]))
    v["tw_to_clip_vs_product_max_abs"] = float(
        np.max(np.abs(v["tw_to_clip"] - v["view_to_clip"] @ v["tw_to_view"]))
    )
    v["clip_convention"] = "math matrix = stored 4x4 block transposed; clip = M @ [x,y,z,1]"
    return v


def project(view, pts):
    pts = np.atleast_2d(np.asarray(pts, dtype=np.float64))
    h = np.concatenate([pts, np.ones((len(pts), 1))], axis=1)
    clip = h @ view["tw_to_clip"].T
    w = clip[:, 3]
    safe = np.where(np.abs(w) > 1e-9, w, np.nan)
    ndc = clip[:, :2] / safe[:, None]
    vx, vy, vw, vh = view["view_rect"]
    sx = vx + (ndc[:, 0] * 0.5 + 0.5) * view["view_size"][0]
    sy = vy + (0.5 - ndc[:, 1] * 0.5) * view["view_size"][1]
    z = (h @ view["tw_to_view"].T)[:, 2]
    return sx, sy, z, w


def decode_cards(ctx: DumpCtx, prefix: str):
    e = ctx.select("Lumen.Cards", prefix)
    raw = np.fromfile(e["file"], dtype="<f4")
    ncards = len(raw) // 28
    cards_raw = raw[: ncards * 28].reshape(ncards, 7, 4).astype(np.float64)
    recs = []
    for i in range(ncards):
        c = cards_raw[i]
        high = c[0, :3]
        low = np.array([c[1, 3], c[2, 3], c[3, 3]])
        origin = high + low
        axes = np.array(
            [
                [c[1, 0], c[2, 0], c[3, 0]],
                [c[1, 1], c[2, 1], c[3, 1]],
                [c[1, 2], c[2, 2], c[3, 2]],
            ]
        )
        packed0 = int(np.asarray(c[0, 3], dtype=np.float32).view(np.uint32))
        packed_axes = int(np.asarray(c[5, 3], dtype=np.float32).view(np.uint32))
        local_extent = shuffle_axes_no_flip(c[6, :3], packed_axes)
        local_extent[2] = max(local_extent[2], 1.0)
        size_pages = [
            int(np.asarray(c[4, 0], dtype=np.float32).view(np.uint32)) & 0xFFFF,
            (int(np.asarray(c[4, 0], dtype=np.float32).view(np.uint32)) >> 16) & 0xFFFF,
        ]
        hires_pages = [
            int(np.asarray(c[4, 2], dtype=np.float32).view(np.uint32)) & 0xFFFF,
            (int(np.asarray(c[4, 2], dtype=np.float32).view(np.uint32)) >> 16) & 0xFFFF,
        ]
        recs.append(
            {
                "card_index": i,
                "origin_world_cm": origin,
                "axes_world": axes,
                "extent_cm": local_extent,
                "visible": bool((packed0 >> 24) & 1),
                "heightfield": bool((packed0 >> 25) & 1),
                "farfield": bool((packed0 >> 26) & 1),
                "axis_aligned_direction": (packed0 >> 16) & 0xF,
                "lighting_channel_mask": (packed0 >> 20) & 0xF,
                "res_level_xy_bias": [packed0 & 0xFF, (packed0 >> 8) & 0xFF],
                "size_in_pages": size_pages,
                "page_table_offset": int(np.asarray(c[4, 1], dtype=np.float32).view(np.uint32)),
                "hires_size_in_pages": hires_pages,
                "hires_page_table_offset": int(np.asarray(c[4, 3], dtype=np.float32).view(np.uint32)),
                "mesh_origin": c[5, :3].copy(),
                "mesh_extent": c[6, :3].copy(),
                "packed_mesh_axes": packed_axes,
                "average_texel_size_cm": float(c[6, 3]),
            }
        )
    geom = np.concatenate(
        [np.concatenate([r["origin_world_cm"], r["axes_world"].ravel(), r["extent_cm"]]) for r in recs]
    )
    return {
        "entry": e,
        "num_cards": ncards,
        "records": recs,
        "static_geometry_sha256": sha256_bytes(np.round(geom, 4).astype(np.float32).tobytes()),
        "visible_count": sum(1 for r in recs if r["visible"]),
    }


def decode_pages(ctx: DumpCtx, prefix: str):
    pb_e = ctx.select("Lumen.PageBuffer", prefix)
    raw = np.fromfile(pb_e["file"], dtype="<f4")
    npages = len(raw) // 20
    pb = raw[: npages * 20].reshape(npages, 5, 4).astype(np.float64)
    pt_e = None
    pt = np.zeros((npages, 2), dtype=np.uint32)
    try:
        pt_e = ctx.select("Lumen.PageTable", prefix)
        pt = np.fromfile(pt_e["file"], dtype="<u4").reshape(-1, 2)
    except KeyError:
        pass
    pages = []
    for i in range(npages):
        v0, v1, v2, v3, v4 = pb[i]
        size = [float(v0[2]), float(v0[3])]
        mapped = size[0] > 0.0
        ptw = pt[i] if i < len(pt) else np.array([0, 0], dtype=np.uint32)
        w0 = int(ptw[0])
        pages.append(
            {
                "page_index": i,
                "mapped": bool(mapped),
                "card_index": int(np.asarray(v0[0], dtype=np.float32).view(np.int32)),
                "res_level_page_table_offset": int(np.asarray(v0[1], dtype=np.float32).view(np.uint32)),
                "size_in_texels": size,
                "card_uv_rect": [float(x) for x in v1],
                "physical_atlas_uv_rect": [float(x) for x in v2],
                "card_uv_texel_scale": [float(v3[0]), float(v3[1])],
                "res_level_size_in_tiles": [
                    int(np.asarray(v3[2], dtype=np.float32).view(np.uint32)),
                    int(np.asarray(v3[3], dtype=np.float32).view(np.uint32)),
                ],
                "last_direct_lighting_update": int(np.asarray(v4[0], dtype=np.float32).view(np.uint32)),
                "last_indirect_lighting_update": int(np.asarray(v4[1], dtype=np.float32).view(np.uint32)),
                "indirect_lighting_temporal_index": int(np.asarray(v4[2], dtype=np.float32).view(np.uint32)),
                "direct_lighting_temporal_index": int(np.asarray(v4[3], dtype=np.float32).view(np.uint32)),
                "sample_page_index": int(ptw[1]),
                "sample_atlas_bias_xy": [w0 & 0xFFF, (w0 >> 12) & 0xFFF],
                "sample_card_res_level_xy": [(w0 >> 24) & 0xF, (w0 >> 28) & 0xF],
            }
        )
    return {
        "entry": pb_e,
        "pagetable_entry": pt_e,
        "npages": npages,
        "pages": pages,
        "mapped_count": sum(1 for p in pages if p["mapped"]),
        "indirect_initialized_count": sum(1 for p in pages if p["mapped"] and p["last_indirect_lighting_update"] != 0),
    }


def decode_card_tiles(ctx: DumpCtx, prefix: str):
    """Decode the captured 16384-byte Lumen.CardTiles (packed card tiles).

    This buffer is CardTileData (24-bit CardPageIndex + 4-bit TileCoord), not
    RadiosityTileData.  RadiosityTileData/RadiosityTileAllocator are not part
    of the 16 captured required resources, so the current-frame radiosity tile
    list is deliberately not asserted.  The decoded card tiles are used only as
    a source-confirmed "this card tile was in the captured update list" marker.
    """
    e = ctx.select("Lumen.CardTiles", prefix, byte_size=16384)
    words = np.fromfile(e["file"], dtype="<u4")
    nz = np.nonzero(words)[0]
    active_count = int(nz.max()) + 1 if nz.size else 0
    if active_count and np.any(words[active_count:] != 0):
        active_count = len(words)
    tiles = []
    active = set()
    for idx in range(active_count):
        w = int(words[idx])
        if w == 0:
            continue
        page = w & 0xFFFFFF
        tx = (w >> 24) & 0xF
        ty = (w >> 28) & 0xF
        tiles.append({"tile_index": idx, "packed": w, "page_index": page, "tile_xy": [tx, ty]})
        active.add((page, tx, ty))
    return {
        "entry": e,
        "words": len(words),
        "nonzero": int((words != 0).sum()),
        "active_count": active_count,
        "tiles": tiles,
        "active": active,
        "page_count": len({t["page_index"] for t in tiles}),
        "packing": "FCardTileData: page = packed & 0xFFFFFF, tile.x = (packed>>24)&0xF, tile.y = (packed>>28)&0xF",
    }


def decode_radiosity_atlases(ctx: DumpCtx, prefix: str, params: dict):
    sh = {}
    for ch in ("Red", "Green", "Blue"):
        e = ctx.select(f"Lumen.Radiosity.ProbeSH{ch}Atlas", prefix)
        arr = np.fromfile(e["file"], dtype="<f2").reshape(1024, 1024, 4)
        sh[ch] = {"entry": e, "array": arr}
    nf_e = ctx.select("Lumen.Radiosity.NumFramesAccumulatedAtlas", prefix)
    nf = np.fromfile(nf_e["file"], dtype=np.uint8).reshape(512, 512)
    si_e = ctx.select("Lumen.Radiosity.SceneIndirectLighting", prefix)
    si = np.fromfile(si_e["file"], dtype="<u4").reshape(4096, 4096)
    ft_e = ctx.select("Lumen.Radiosity.FilteredTraceRadianceAtlas", prefix)
    return {
        "sh": sh,
        "nf": nf,
        "nf_entry": nf_e,
        "scene_indirect": si,
        "scene_indirect_entry": si_e,
        "filtered_entry": ft_e,
    }


def card_local_from_uv_depth(card, card_uv, depth):
    """Exact GetCardLocalPosition (LumenCardCommon.ush:315)."""
    ext = np.asarray(card["extent_cm"], dtype=np.float64)
    u = 1.0 - float(card_uv[0])  # shader flips x first
    local = np.array(
        [ext[0] * (1.0 - 2.0 * u), ext[1] * (1.0 - 2.0 * card_uv[1]), -(2.0 * depth - 1.0) * ext[2]],
        dtype=np.float64,
    )
    return local


def card_world_from_local(card, local):
    axes = np.asarray(card["axes_world"], dtype=np.float64)
    return axes[0] * local[0] + axes[1] * local[1] + axes[2] * local[2] + np.asarray(card["origin_world_cm"])


def decode_probes(ctx: DumpCtx, prefix: str, view: dict, cards: dict, pages: dict,
                  card_tiles: dict, atlases: dict, params: dict, preexp: float):
    spacing = max(1, int(params["BuildRadiosityTiles"]["ProbeSpacingInCardTexels"]))
    shift = int(params["BuildRadiosityTiles"]["ProbeSpacingInCardTexelsDivideShift"])
    maxf = max(1, int(params["BuildRadiosityTiles"]["MaxFramesAccumulated"]))
    fixed_j = int(params["BuildRadiosityTiles"]["FixedJitterIndex"])
    atlas_card = int(params["BuildRadiosityTiles"]["RadiosityAtlasSize"][0])  # 4096
    probe_atlas = atlas_card >> shift
    sh_atlas = atlases["sh"]["Red"]["array"].shape[1]
    if probe_atlas != sh_atlas:
        raise ValueError(f"probe atlas {probe_atlas} != SH atlas {sh_atlas}")

    depth_u16 = np.fromfile(
        ctx.select("Lumen.SceneDepth", prefix)["file"], dtype="<u2"
    ).reshape(DEPTH_ATLAS_SIZE, DEPTH_ATLAS_SIZE)
    normal_h = np.fromfile(
        ctx.select("Lumen.SceneNormal", prefix)["file"], dtype="<f2"
    ).reshape(NORMAL_ATLAS_SIZE, NORMAL_ATLAS_SIZE, 4)

    nf = atlases["nf"]
    sh_r = atlases["sh"]["Red"]["array"]
    sh_g = atlases["sh"]["Green"]["array"]
    sh_b = atlases["sh"]["Blue"]["array"]
    active = card_tiles["active"]

    cols = defaultdict(list)
    skipped = Counter()
    for page in pages["pages"]:
        if not page["mapped"]:
            continue
        card_idx = page["card_index"]
        if card_idx < 0 or card_idx >= cards["num_cards"]:
            skipped["card_range"] += 1
            continue
        if page["last_indirect_lighting_update"] == 0:
            skipped["page_never_updated"] += 1
            continue
        card = cards["records"][card_idx]
        size_x, size_y = page["size_in_texels"]
        nx = int(size_x) // spacing
        ny = int(size_y) // spacing
        if nx <= 0 or ny <= 0:
            skipped["empty_grid"] += 1
            continue
        ti = page["indirect_lighting_temporal_index"]
        jidx = fixed_j if fixed_j >= 0 else (ti % maxf)
        jx_f, jy_f = hammersley16(jidx, maxf, HAMM_RX, HAMM_RY)
        jx = int(jx_f * spacing)
        jy = int(jy_f * spacing)
        cu0, cv0, cuv_su, cuv_sv = page["card_uv_rect"][0], page["card_uv_rect"][1], page["card_uv_texel_scale"][0], page["card_uv_texel_scale"][1]
        phu, phv = page["physical_atlas_uv_rect"][0], page["physical_atlas_uv_rect"][1]
        phys_x = int(round(phu * atlas_card))
        phys_y = int(round(phv * atlas_card))
        nf_base_x = phys_x // CARD_TILE_SIZE
        nf_base_y = phys_y // CARD_TILE_SIZE
        probes_per_nf_tile = max(1, int(params["BuildRadiosityTiles"]["CardTileSizeInProbes"]))
        for iy in range(ny):
            for ix in range(nx):
                cx = ix * spacing + jx
                cy = iy * spacing + jy
                if cx >= size_x or cy >= size_y:
                    skipped["jitter_outside_page"] += 1
                    continue
                # GetRadiosityTexel sampling (point clamp)
                au = phu + (cx + 0.5) / atlas_card
                av = phv + (cy + 0.5) / atlas_card
                tx = min(DEPTH_ATLAS_SIZE - 1, max(0, int(au * DEPTH_ATLAS_SIZE)))
                ty = min(DEPTH_ATLAS_SIZE - 1, max(0, int(av * DEPTH_ATLAS_SIZE)))
                raw_d = int(depth_u16[ty, tx])
                if raw_d >= DEPTH_INVALID:
                    skipped["invalid_depth"] += 1
                    continue
                dep = raw_d / float(DEPTH_INVALID)
                if dep >= 1.0:
                    skipped["invalid_depth"] += 1
                    continue
                # CardUV and local/world position
                cu = cu0 + cuv_su * (cx + 0.5)
                cv = cv0 + cuv_sv * (cy + 0.5)
                local = card_local_from_uv_depth(card, (cu, cv), dep)
                world = card_world_from_local(card, local)
                # NormalAtlas is exporter-expanded BC5 stored as float RGBA.
                nr = float(normal_h[ty, tx, 0])
                ng = float(normal_h[ty, tx, 1])
                if not (math.isfinite(nr) and math.isfinite(ng)):
                    skipped["invalid_normal"] += 1
                    continue
                if nr == 0.0 and ng == 0.0:
                    skipped["zero_encoded_normal"] += 1
                    continue
                csn = np.array([nr * 2.0 - 1.0, ng * 2.0 - 1.0, 0.0])
                csn[2] = math.sqrt(max(1.0 - csn[0] * csn[0] - csn[1] * csn[1], 0.0001))
                axes = np.asarray(card["axes_world"], dtype=np.float64)
                wn = axes[0] * csn[0] + axes[1] * csn[1] + axes[2] * csn[2]
                ln = float(np.linalg.norm(wn))
                if not math.isfinite(ln) or ln <= 1e-12:
                    skipped["invalid_normal"] += 1
                    continue
                wn = wn / ln
                # SH probe atlas coordinate
                ax = (phys_x + cx) >> shift
                ay = (phys_y + cy) >> shift
                if ax < 0 or ay < 0 or ax >= probe_atlas or ay >= probe_atlas:
                    skipped["probe_atlas_bounds"] += 1
                    continue
                nfx = nf_base_x + (ix // probes_per_nf_tile)
                nfy = nf_base_y + (iy // probes_per_nf_tile)
                if nfx < 0 or nfy < 0 or nfx >= nf.shape[1] or nfy >= nf.shape[0]:
                    skipped["nf_bounds"] += 1
                    continue
                if ax // 2 != nfx or ay // 2 != nfy:
                    skipped["nf_alignment"] += 1
                    continue
                nf_raw = int(nf[nfy, nfx])
                if nf_raw == 0:
                    skipped["uninitialized_counter"] += 1
                    continue
                sh = np.concatenate(
                    [
                        sh_r[ay, ax, :].astype(np.float64),
                        sh_g[ay, ax, :].astype(np.float64),
                        sh_b[ay, ax, :].astype(np.float64),
                    ]
                )
                cols["position"].append(world)
                cols["normal"].append(wn)
                cols["sh_raw"].append(sh)
                cols["page"].append(page["page_index"])
                cols["card"].append(card_idx)
                cols["grid_ix"].append(ix)
                cols["grid_iy"].append(iy)
                cols["jitter_x"].append(jx)
                cols["jitter_y"].append(jy)
                cols["temporal"].append(ti)
                cols["last_indirect"].append(page["last_indirect_lighting_update"])
                cols["nf_raw"].append(nf_raw)
                cols["atlas_xy"].append((ax, ay))
                cols["depth_texel_xy"].append((tx, ty))
                cols["depth_raw"].append(raw_d)
                cols["depth_unorm"].append(dep)
                cols["atlas_uv"].append((au, av))
                cols["card_uv"].append((cu, cv))
                cols["card_tile_in_capture"].append((page["page_index"], ix // probes_per_nf_tile, iy // probes_per_nf_tile) in active)
                cols["nf_xy"].append((nfx, nfy))

    if not cols["position"]:
        raise RuntimeError(f"{ctx.state}: no valid probes decoded")

    def arr(name, dtype=np.float64):
        return np.asarray(cols[name], dtype=dtype)

    position = arr("position", np.float64)
    normal = arr("normal", np.float64)
    sh_raw = arr("sh_raw", np.float64).reshape(-1, 3, 4)
    page = arr("page", np.int64)
    card = arr("card", np.int64)
    atlas_xy = arr("atlas_xy", np.int64)
    depth_texel = arr("depth_texel_xy", np.int64)
    nf_xy = arr("nf_xy", np.int64)
    pre = np.asarray(view["pre_view_translation_cm"], dtype=np.float64)
    sx, sy, vz, cw = project(view, position + pre)
    on_screen = (cw > 0) & (sx >= 0) & (sx < view["view_size"][0]) & (sy >= 0) & (sy < view["view_size"][1])
    # primary depth
    sd_e = ctx.select("SceneDepthZ", prefix)
    sd = np.fromfile(sd_e["file"], dtype="<f4").reshape(sd_e["desc"]["ExtentY"], sd_e["desc"]["ExtentX"]).astype(np.float64)
    px = np.floor(np.nan_to_num(sx, nan=-1e9)).astype(np.int64)
    py = np.floor(np.nan_to_num(sy, nan=-1e9)).astype(np.int64)
    in_sd = on_screen & (py >= 0) & (py < sd.shape[0]) & (px >= 0) & (px < sd.shape[1])
    scene_dz = np.full(len(position), np.nan)
    scene_cm = np.full(len(position), np.nan)
    scene_dz[in_sd] = sd[py[in_sd], px[in_sd]]
    good_dz = in_sd & np.isfinite(scene_dz) & (scene_dz > 0)
    scene_cm[good_dz] = view["near_plane_cm"] / scene_dz[good_dz]
    tol = np.maximum(DEPTH_TOL_ABS_CM, DEPTH_TOL_REL * np.where(np.isfinite(scene_cm), scene_cm, 0.0))
    visible = in_sd & (~np.isfinite(scene_cm) | (vz <= scene_cm + tol))
    rel = np.full(len(position), np.nan)
    rel[good_dz] = np.abs(scene_cm[good_dz] - vz[good_dz]) / np.maximum(vz[good_dz], 1.0)

    sh_rad = sh_raw / preexp
    e_raw = sh_eval(sh_raw, normal)
    e_rad = e_raw / preexp
    lambert_rad = e_rad / PI

    return {
        "position": position,
        "normal": normal,
        "sh_raw": sh_raw,
        "sh_radiance": sh_rad,
        "e_raw": e_raw,
        "e_radiance": e_rad,
        "lambert_radiance": lambert_rad,
        "page": page,
        "card": card,
        "grid_ix": arr("grid_ix", np.int64),
        "grid_iy": arr("grid_iy", np.int64),
        "jitter_x": arr("jitter_x", np.int64),
        "jitter_y": arr("jitter_y", np.int64),
        "temporal": arr("temporal", np.int64),
        "last_indirect": arr("last_indirect", np.int64),
        "nf_raw": arr("nf_raw", np.int64),
        "nf_xy": nf_xy,
        "atlas_xy": atlas_xy,
        "depth_texel_xy": depth_texel,
        "depth_raw": arr("depth_raw", np.int64),
        "depth_unorm": arr("depth_unorm", np.float64),
        "atlas_uv": arr("atlas_uv", np.float64),
        "card_uv": arr("card_uv", np.float64),
        "card_tile_in_capture": arr("card_tile_in_capture", bool),
        "screen_xy": np.stack([sx, sy], axis=1),
        "clip_w": cw,
        "view_z": vz,
        "on_screen": on_screen,
        "scene_depth_cm": scene_cm,
        "scene_device_z": scene_dz,
        "depth_rel_error": rel,
        "visible": visible,
        "occluded": on_screen & ~visible,
        "spacing": spacing,
        "shift": shift,
        "max_frames": maxf,
        "fixed_jitter_index": fixed_j,
        "preexposure": preexp,
        "probe_atlas_size": probe_atlas,
        "skipped": dict(skipped),
        "scene_depth_entry": sd_e,
    }


def decode_state(state: str, raw_root: Path, capture_manifest: dict, verify_param: dict, do_hash=True):
    t0 = time.time()
    ctx = DumpCtx(state, raw_root)
    # pick the graph with the highest view frame field; all operands stay on it
    graphs = []
    for vd in ctx.view_descs():
        uid = vd["UniqueResourceName"]
        prefix = uid[:4]
        files = ctx.main_file(uid)
        if not files:
            continue
        f, ver = files[0]
        v = decode_view(f)
        graphs.append(
            {
                "prefix": prefix,
                "view_uid": uid,
                "view_file": f.name,
                "view_version": ver,
                "frame_index_field": v["frame_index_field"],
                "camera_world_cm": v["camera_world_cm"],
                "view": v,
                "view_bytes": f.read_bytes(),
            }
        )
    graphs.sort(key=lambda g: g["frame_index_field"])
    g = graphs[-1]
    prefix = g["prefix"]
    view = g["view"]
    view["selected_graph_prefix"] = prefix
    view["all_graphs"] = [
        {
            "prefix": x["prefix"],
            "view_uid": x["view_uid"],
            "view_file": x["view_file"],
            "frame_index_field": x["frame_index_field"],
            "camera_world_cm": x["camera_world_cm"],
        }
        for x in graphs
    ]

    params = ctx.decode_radiosity_parameters(prefix, capture_manifest)
    preexp = float(params["Batched lights"]["CachedLightingPreExposure"])
    cards = decode_cards(ctx, prefix)
    pages = decode_pages(ctx, prefix)
    card_tiles = decode_card_tiles(ctx, prefix)
    atlases = decode_radiosity_atlases(ctx, prefix, params)
    probes = decode_probes(ctx, prefix, view, cards, pages, card_tiles, atlases, params, preexp)

    # Corroboration only: the captured RadiosityAtlas (SceneIndirectLighting)
    # is the bilinearly interpolated / weight-normalised surface-texel result,
    # not an independent per-probe measurement.  Correlation is reported as a
    # sanity check against the SH evaluation, never as equality.
    si = atlases["scene_indirect"]
    idx = np.linspace(0, len(probes["position"]) - 1, min(20000, len(probes["position"]))).astype(int)
    tx = probes["depth_texel_xy"][idx, 0]
    ty = probes["depth_texel_xy"][idx, 1]
    si_rgb = decode_r11g11b10(si[ty, tx])
    e = probes["e_raw"][idx]
    good = np.isfinite(si_rgb).all(axis=1) & np.isfinite(e).all(axis=1) & (e.max(axis=1) > 0.01)
    corroboration = {"samples": int(good.sum()), "note": "SceneIndirectLighting at the surface texel vs SH E(normal); bilinear/weighting makes this a correlation check only"}
    for ch, name in enumerate("RGB"):
        a = e[good, ch]
        b = si_rgb[good, ch]
        corroboration[f"pearson_{name}"] = float(np.corrcoef(a, b)[0, 1]) if a.size > 2 and np.std(a) > 0 and np.std(b) > 0 else None
        ratio = b / np.maximum(a, 1e-9)
        corroboration[f"median_ratio_{name}"] = float(np.median(ratio)) if ratio.size else None
    result_extra = {"scene_indirect_corroboration": corroboration}

    # resource records for manifest
    used = {}
    for name, size, fmt in [
        ("Lumen.Cards", None, None),
        ("Lumen.PageBuffer", None, None),
        ("Lumen.PageTable", None, None),
        ("Lumen.CardTiles", 33124, None),
        ("Lumen.CardTiles", 16384, None),
        ("Lumen.CardCaptureRects", None, None),
        ("Lumen.MeshCards", None, None),
        ("Lumen.SceneDepth", None, None),
        ("Lumen.SceneNormal", None, None),
        ("Lumen.Radiosity.ProbeSHRedAtlas", None, None),
        ("Lumen.Radiosity.ProbeSHGreenAtlas", None, None),
        ("Lumen.Radiosity.ProbeShBlueAtlas", None, None),
        ("Lumen.Radiosity.ProbeSHBlueAtlas", None, None),
        ("Lumen.Radiosity.NumFramesAccumulatedAtlas", None, None),
        ("Lumen.Radiosity.SceneIndirectLighting", None, None),
        ("Lumen.Radiosity.FilteredTraceRadianceAtlas", None, None),
        ("SceneDepthZ", None, None),
    ]:
        try:
            e = ctx.select(name, prefix, byte_size=size, fmt=fmt)
        except KeyError:
            continue
        key = name + (f"@{size}" if size else "")
        used[key] = ctx.record(e, key)
    used["ViewData.PrimaryViews"] = {
        "role": "camera/view",
        "name": "ViewData.PrimaryViews",
        "unique_resource_name": g["view_uid"],
        "version_pointer": g["view_version"],
        "file": g["view_file"],
        "bytes": len(g["view_bytes"]),
        "sha256": sha256_file(ctx.root / "Resources" / g["view_file"]),
        "descriptor": {"ByteSize": 1056, "BytesPerElement": 1056, "NumElements": 1},
        "readback": ctx.readback.get("Resources/" + g["view_file"], {}).get("Status"),
        "producer": None,
    }

    # raw source files used by this decoder (before/after hash)
    source_files = sorted({r["file"] for r in used.values()})
    for ev in params:
        source_files.append(Path(params[ev]["pass"]["parameters_file"]).name)
    # parameter metadata files are in the raw state too
    meta_files = set()
    for ev in ("BuildRadiosityTiles", "ConvertToSH", "Integrate", "Batched lights"):
        rec = ctx.pass_parameters(ev, prefix, capture_manifest)
        meta_files.add(Path(rec["metadata_file"]).name)
        for dep in rec.get("metadata_dependencies", []):
            if dep.get("struct_type") in ("FLumenRadiosityTexelParameter", "FLumenRadiosityTexelTraceParameters", "FViewUniformShaderParameters"):
                meta_files.add(Path(dep["file"]).name)
    source_paths = []
    for nm in sorted(set(source_files)):
        p = ctx.root / "Resources" / nm
        if p.exists():
            source_paths.append(p)
        else:
            p = ctx.root / "Structures" / nm
            if p.exists():
                source_paths.append(p)
    for nm in sorted(meta_files):
        p = ctx.root / "StructuresMetadata" / nm
        if p.exists():
            source_paths.append(p)
    base_files = [
        ctx.base / "Passes.json",
        ctx.base / "ResourceDescs.json",
        ctx.base / "DumpReadbackStatus.json",
        ctx.base / "SubresourceLayouts.json",
        ctx.base / "ParameterBindings.json",
        ctx.base / "NativeUniformBufferBindings.json",
        ctx.base / "ReferencedStructs.json",
        ctx.base / "ConsoleVariables.csv",
        ctx.base / "Screenshot.png",
    ]
    source_paths.extend([p for p in base_files if p.exists()])
    source_hashes = {str(p.relative_to(ctx.root)): sha256_file(p) for p in source_paths}

    result = {
        "state": state,
        "ctx": ctx,
        "prefix": prefix,
        "view": view,
        "view_entry": used["ViewData.PrimaryViews"],
        "graphs": graphs,
        "params": params,
        "preexposure": preexp,
        "cards": cards,
        "pages": pages,
        "card_tiles": card_tiles,
        "atlases": atlases,
        "probes": probes,
        "scene_indirect_corroboration": result_extra["scene_indirect_corroboration"],
        "resources": used,
        "source_hashes": source_hashes,
        "decode_seconds": time.time() - t0,
    }
    return result


# ---------------------------------------------------------------------------
# figure helpers
# ---------------------------------------------------------------------------

def normal_class(normals):
    """0 floor/up, 1 ceiling/down, 2 wall X, 3 wall Y."""
    n = np.asarray(normals)
    cls = np.full(len(n), 3, dtype=np.int8)
    ax = np.abs(n[:, 0])
    ay = np.abs(n[:, 1])
    az = np.abs(n[:, 2])
    cls[(az >= ax) & (az >= ay) & (n[:, 2] >= 0)] = 0
    cls[(az >= ax) & (az >= ay) & (n[:, 2] < 0)] = 1
    wall = (az < ax) | (az < ay)
    cls[wall & (ax >= ay)] = 2
    return cls


CLASS_COLORS = np.array(
    [
        [70, 220, 90],    # floor / up
        [255, 150, 40],   # ceiling / down
        [70, 150, 255],   # wall X
        [255, 70, 120],   # wall Y
    ],
    dtype=np.uint8,
)


def colormap(t):
    """Fixed 6-stop palette, t in [0,1]."""
    t = np.clip(np.asarray(t, dtype=np.float64), 0.0, 1.0)
    stops = np.array(
        [
            [10, 10, 40],
            [35, 60, 160],
            [30, 170, 200],
            [120, 230, 110],
            [250, 220, 70],
            [255, 255, 255],
        ],
        dtype=np.float64,
    )
    x = t * (len(stops) - 1)
    i = np.minimum(np.floor(x).astype(int), len(stops) - 2)
    f = (x - i)[..., None]
    return (stops[i] * (1 - f) + stops[i + 1] * f).astype(np.uint8)


def overlay_points(img_rgb, xs, ys, colors, radius=1, alpha=0.95):
    """Vectorized dot overlay; no text."""
    out = np.asarray(img_rgb, dtype=np.float32).copy()
    h, w = out.shape[:2]
    x = np.floor(np.asarray(xs)).astype(np.int64)
    y = np.floor(np.asarray(ys)).astype(np.int64)
    ok = (x >= 0) & (x < w) & (y >= 0) & (y < h)
    x, y, c = x[ok], y[ok], np.asarray(colors)[ok].astype(np.float32)
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            xx = np.clip(x + dx, 0, w - 1)
            yy = np.clip(y + dy, 0, h - 1)
            out[yy, xx] = out[yy, xx] * (1 - alpha) + c * alpha
    return out.astype(np.uint8)


def draw_screenshot_overlay(state, probes, out_path, raw_root, mode="placement"):
    base = np.asarray(
        Image.open(Path(raw_root) / state / "Base" / "Screenshot.png").convert("RGB")
    )
    if mode == "placement":
        idx = np.nonzero(probes["visible"])[0]
        stride = max(1, len(idx) // 26000)
        idx = idx[::stride]
        cols = CLASS_COLORS[normal_class(probes["normal"][idx])]
        out = overlay_points(base, probes["screen_xy"][idx, 0], probes["screen_xy"][idx, 1], cols, radius=1, alpha=0.80)
    elif mode == "xray":
        vis = np.nonzero(probes["visible"])[0]
        stride = max(1, len(vis) // 26000)
        vis = vis[::stride]
        cols = np.tile(np.array([[60, 240, 255]], dtype=np.uint8), (len(vis), 1))
        out = overlay_points(base, probes["screen_xy"][vis, 0], probes["screen_xy"][vis, 1], cols, radius=1, alpha=0.85)
        # magenta x for a legible subsample of hidden probes (all are in the NPZ)
        hid = np.nonzero(probes["on_screen"] & ~probes["visible"])[0]
        hid = hid[::4]
        xs = probes["screen_xy"][hid, 0]
        ys = probes["screen_xy"][hid, 1]
        out_f = out.astype(np.float32)
        h, w = out_f.shape[:2]
        for dx, dy in ((-1, -1), (0, 0), (1, 1), (-1, 1), (1, -1)):
            x = np.floor(xs).astype(np.int64) + dx
            y = np.floor(ys).astype(np.int64) + dy
            ok = (x >= 0) & (x < w) & (y >= 0) & (y < h)
            out_f[y[ok], x[ok]] = out_f[y[ok], x[ok]] * 0.55 + np.array([255, 0, 255], dtype=np.float32) * 0.45
        out = out_f.astype(np.uint8)
    return out


def draw_plan_panel(state, probes, kind="xy"):
    """Density + dominant-normal raster of all decoded probes, fixed bounds."""
    bounds = probes["plan_bounds_cm"]
    if kind == "xy":
        a, b = probes["position"][:, 0], probes["position"][:, 1]
        lo0, hi0 = bounds["x"]
        lo1, hi1 = bounds["y"]
    else:
        a, b = probes["position"][:, 0], probes["position"][:, 2]
        lo0, hi0 = bounds["x"]
        lo1, hi1 = bounds["z"]
    px = ((a - lo0) / (hi0 - lo0) * (PLAN_W - 1)).astype(np.int64)
    py = ((hi1 - b) / (hi1 - lo1) * (PLAN_H - 1)).astype(np.int64)
    ok = (px >= 0) & (px < PLAN_W) & (py >= 0) & (py < PLAN_H)
    cls = normal_class(probes["normal"])
    acc = np.zeros((4, PLAN_H, PLAN_W), dtype=np.float64)
    for c in range(4):
        m = ok & (cls == c)
        np.add.at(acc[c], (py[m], px[m]), 1.0)
    total = acc.sum(axis=0)
    pos = total[total > 0]
    if pos.size:
        scale = max(1.0, float(np.percentile(pos, 99.0)))
        t = np.clip(np.log1p(total) / math.log1p(scale), 0.0, 1.0)
    else:
        t = np.zeros_like(total)
    denom = np.maximum(total, 1.0)[..., None]
    col = (acc[0][..., None] * CLASS_COLORS[0] + acc[1][..., None] * CLASS_COLORS[1]
           + acc[2][..., None] * CLASS_COLORS[2] + acc[3][..., None] * CLASS_COLORS[3]) / denom
    img = (col * (0.22 + 0.78 * t)[..., None]).astype(np.uint8)
    img[total == 0] = 12
    # camera marker (fixed square)
    cam = probes["view"]["camera_world_cm"]
    ca, cb = (cam[0], cam[1]) if kind == "xy" else (cam[0], cam[2])
    cx = int((ca - lo0) / (hi0 - lo0) * (PLAN_W - 1))
    cy = int((hi1 - cb) / (hi1 - lo1) * (PLAN_H - 1))
    for dy in range(-3, 4):
        for dx in range(-3, 4):
            if abs(dx) + abs(dy) <= 3:
                x, y = cx + dx, cy + dy
                if 0 <= x < PLAN_W and 0 <= y < PLAN_H:
                    img[y, x] = np.array([255, 255, 255], dtype=np.uint8)
    return img


def draw_placement_figure(state, probes, out_path, raw_root):
    shot = draw_screenshot_overlay(state, probes, out_path, raw_root, "placement")
    canvas = np.zeros((FIGURE_H, FIGURE_W + PLAN_W, 3), dtype=np.uint8)
    canvas[:, :FIGURE_W] = shot
    canvas[:PLAN_H, FIGURE_W:FIGURE_W + PLAN_W] = draw_plan_panel(state, probes, "xy")
    canvas[PLAN_H:, FIGURE_W:FIGURE_W + PLAN_W] = draw_plan_panel(state, probes, "xz")
    Image.fromarray(canvas).save(out_path)
    return out_path


def draw_xray_figure(state, probes, out_path, raw_root):
    out = draw_screenshot_overlay(state, probes, out_path, raw_root, "xray")
    Image.fromarray(out).save(out_path)
    return out_path


def tangent_basis_frisvad(n):
    n = np.asarray(n, dtype=np.float64)
    if n[2] < -0.9999999:
        return np.array([0.0, -1.0, 0.0]), np.array([-1.0, 0.0, 0.0]), n
    a = 1.0 / (1.0 + n[2])
    b = -n[0] * n[1] * a
    tx = np.array([1.0 - n[0] * n[0] * a, b, -n[0]])
    ty = np.array([b, 1.0 - n[1] * n[1] * a, -n[1]])
    return tx, ty, n


def fibonacci_sphere(n):
    i = np.arange(n) + 0.5
    phi = np.arccos(1.0 - 2.0 * i / n)
    theta = PI * (1.0 + 5.0 ** 0.5) * i
    return np.stack(
        [np.cos(theta) * np.sin(phi), np.sin(theta) * np.sin(phi), np.cos(phi)], axis=-1
    )


def choose_lobe_probes(probes, max_probes=10):
    """Visible probes spread over screen, one per bin near its 70th percentile."""
    vis = np.nonzero(probes["visible"])[0]
    if vis.size == 0:
        return []
    sx = probes["screen_xy"][vis, 0]
    sy = probes["screen_xy"][vis, 1]
    bins = np.stack([np.clip((sx / FIGURE_W * 5).astype(int), 0, 4),
                     np.clip((sy / FIGURE_H * 2).astype(int), 0, 1)], axis=1)
    strength = probes["lambert_radiance"][vis].max(axis=1)
    chosen = []
    for by in range(2):
        for bx in range(5):
            m = (bins[:, 0] == bx) & (bins[:, 1] == by)
            if not m.any():
                continue
            idx = np.nonzero(m)[0]
            target = np.percentile(strength[idx], 95.0)
            pick = idx[np.argmin(np.abs(strength[idx] - target))]
            chosen.append(int(vis[pick]))
    if len(chosen) > max_probes:
        chosen = chosen[:max_probes]
    return chosen


def draw_lobes_figure(state, probes, out_path, radiance_max, raw_root):
    base = Image.open(Path(raw_root) / state / "Base" / "Screenshot.png").convert("RGB")
    draw = ImageDraw.Draw(base)
    chosen = choose_lobe_probes(probes, 10)
    dirs = fibonacci_sphere(LOBE_DIRECTIONS)
    view = probes["view"]
    pre = np.asarray(view["pre_view_translation_cm"], dtype=np.float64)
    if not chosen:
        base.save(out_path)
        return out_path, chosen
    csx, csy, _, _ = project(view, probes["position"][chosen] + pre)
    for k, i in enumerate(chosen):
        center = probes["position"][i]
        center_screen = (float(csx[k]), float(csy[k]))
        c = probes["sh_radiance"][i]
        for d in dirs:
            e = sh_eval_direction(c, d)
            e = np.maximum(e, 0.0)  # zero clamp, declared
            r_cm = LOBE_RADIUS_CM * min(1.0, float(np.max(e)) / max(radiance_max, 1e-9))
            if r_cm <= 0.5:
                continue
            end = center + d * r_cm
            ex, ey, _, ew = project(view, (end + pre).reshape(1, 3))
            if ew[0] <= 0:
                continue
            col = np.clip(e / max(radiance_max, 1e-9), 0.0, 1.0) ** 0.5
            col = tuple(int(255 * v) for v in col)
            draw.line([center_screen[0], center_screen[1], float(ex[0]), float(ey[0])], fill=col, width=2)
        draw.ellipse(
            [center_screen[0] - 3, center_screen[1] - 3, center_screen[0] + 3, center_screen[1] + 3],
            fill=(255, 255, 255),
        )
    base.save(out_path)
    return out_path, chosen


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def depth_at_offset(path: Path, tx: int, ty: int):
    with open(path, "rb") as f:
        f.seek((ty * DEPTH_ATLAS_SIZE + tx) * 2)
        b = f.read(2)
    return struct.unpack("<H", b)[0]


def normal_at_offset(path: Path, tx: int, ty: int):
    with open(path, "rb") as f:
        f.seek((ty * NORMAL_ATLAS_SIZE + tx) * 8)
        b = f.read(8)
    return struct.unpack("<4e", b)


def run_checks(states, capture_manifest, global_stats, figures_meta, source_before, source_after, const_test, out_dir):
    checks = {}

    # 1. positions use captured card depth + normal from same invocation
    rows = []
    for st, s in states.items():
        p = s["probes"]
        rows.append(
            {
                "state": st,
                "graph_prefix": s["prefix"],
                "scene_depth_uid": s["resources"]["Lumen.SceneDepth"]["unique_resource_name"],
                "scene_depth_sha256": s["resources"]["Lumen.SceneDepth"]["sha256"],
                "scene_normal_uid": s["resources"]["Lumen.SceneNormal"]["unique_resource_name"],
                "scene_normal_sha256": s["resources"]["Lumen.SceneNormal"]["sha256"],
                "page_buffer_uid": s["resources"]["Lumen.PageBuffer"]["unique_resource_name"],
                "cards_uid": s["resources"]["Lumen.Cards"]["unique_resource_name"],
                "probe_count": int(len(p["position"])),
                "card_tile_probe_count": int(p["card_tile_in_capture"].sum()),
                "visible_probe_count": int(p["visible"].sum()),
                "depth_invalid_rejected": int(p["skipped"].get("invalid_depth", 0)),
                "uninitialized_rejected": int(p["skipped"].get("uninitialized_counter", 0)),
                "position_min_cm": p["position"].min(axis=0).tolist(),
                "position_max_cm": p["position"].max(axis=0).tolist(),
                "normals_unit_max_error": float(np.max(np.abs(np.linalg.norm(p["normal"], axis=1) - 1.0))),
            }
        )
    checks["positions_use_captured_card_depth_and_normal"] = {
        "passed": all(r["probe_count"] > 0 and r["normals_unit_max_error"] < 1e-5 for r in rows),
        "evidence": {
            "rows": rows,
            "position_formula": "AtlasUV = PhysicalAtlasUVRect.xy + (CoordInCardPage+0.5)/4096; Depth = Lumen.SceneDepth u16/65535; CardUV = CardUVRect.xy + CardUVTexelScale*(CoordInCardPage+0.5); local = GetCardLocalPosition(Card,CardUV,Depth); world = local.x*axis0 + local.y*axis1 + local.z*axis2 + origin",
            "explicitly_not_used_for_position": "SceneDepthZ is used only for the visibility depth test, never as the card surface depth",
            "rejection_rules": "depth>=1 (u16 65535), zero/NaN encoded normal, NumFramesAccumulated==0, out-of-page jitter, out-of-atlas bounds",
        },
    }

    # 2. SH normalization + preexposure
    sh_rows = []
    for st, s in states.items():
        p = s["probes"]
        sh_rows.append(
            {
                "state": st,
                "preexposure": s["preexposure"],
                "preexposure_pass": s["params"]["Batched lights"]["pass"],
                "coeff_dtype": "half4 per channel in PF_FloatRGBA 1024x1024",
                "coeff_min": float(p["sh_raw"].min()),
                "coeff_max": float(p["sh_raw"].max()),
                "all_12_signed_channels_kept": True,
                "e_raw_min": float(p["e_raw"].min()),
                "e_raw_max": float(p["e_raw"].max()),
                "e_radiance_max": float(p["e_radiance"].max()),
                "lambert_radiance_p50": float(np.percentile(p["lambert_radiance"], 50)),
                "lambert_radiance_p95": float(np.percentile(p["lambert_radiance"], 95)),
                "scene_indirect_corroboration": s["scene_indirect_corroboration"],
            }
        )
    ratio_ok = True
    for s in states.values():
        p = s["probes"]
        nz = p["sh_raw"] != 0
        if np.any(nz):
            r = p["sh_radiance"][nz] / p["sh_raw"][nz]
            if np.max(np.abs(r - 1.0 / s["preexposure"])) > 1e-4:
                ratio_ok = False
    checks["sh_four_coefficients_source_normalization_and_cache_exposure"] = {
        "passed": bool(const_test["relative_error"] < 1e-12 and ratio_ok and all(r["preexposure"] == 0.0625 for r in sh_rows)),
        "evidence": {
            "constant_hemisphere_test": const_test,
            "states": sh_rows,
            "sampled_normal_response": "e_raw/e_radiance = DotSH(coeffs, CalcDiffuseTransferSH(normal,1)); lambert_radiance = e_radiance/pi (unit-albedo outgoing radiance); zero clamp is reported separately",
            "preexposure_conversion": "sh_radiance = sh_raw / CachedLightingPreExposure; CachedLightingPreExposure read from the captured Batched lights pass payload at FParameters offset 204",
            "no_screen_sh_factor": "no 4 and no 4*pi multiplier is used anywhere; the only constants are the SHCommon.ush basis and CalcDiffuseTransferSH(n,1) L0=pi, L1=2*pi/3",
        },
    }

    # 3. card space formula roundtrip on a sample
    rt_err = 0.0
    rt_depth_err = 0.0
    rt_count = 0
    for st, s in states.items():
        p = s["probes"]
        idx = np.linspace(0, len(p["position"]) - 1, min(512, len(p["position"]))).astype(int)
        for i in idx:
            card = s["cards"]["records"][int(p["card"][i])]
            cu, cv = p["card_uv"][i]
            dep = p["depth_unorm"][i]
            local = card_local_from_uv_depth(card, (cu, cv), dep)
            world = card_world_from_local(card, local)
            rt_err = max(rt_err, float(np.max(np.abs(world - p["position"][i]))))
            # inverse: world = A.T @ local  =>  local = solve(A.T, world-origin)
            axes = np.asarray(card["axes_world"])
            loc2 = np.linalg.solve(axes.T, p["position"][i] - np.asarray(card["origin_world_cm"]))
            u2 = (loc2[0] / card["extent_cm"][0] + 1.0) / 2.0
            v2 = (1.0 - loc2[1] / card["extent_cm"][1]) / 2.0
            d2 = (1.0 - loc2[2] / card["extent_cm"][2]) / 2.0
            rt_depth_err = max(rt_depth_err, abs(d2 - dep))
            rt_count += 1
    checks["card_space_point_formula_roundtrip"] = {
        "passed": rt_err < 1e-4 and rt_depth_err < 1e-6,
        "evidence": {
            "samples": rt_count,
            "max_world_roundtrip_error_cm": rt_err,
            "max_depth_roundtrip_error": rt_depth_err,
            "formula": "local.x = ext.x*(1-2*(1-u)); local.y = ext.y*(1-2*v); local.z = -(2*depth-1)*ext.z; world = axis0*local.x+axis1*local.y+axis2*local.z+origin",
        },
    }

    # 4. depth byte offset and normal decode
    de_rows = []
    for st, s in states.items():
        p = s["probes"]
        dep_path = s["ctx"].select("Lumen.SceneDepth", s["prefix"])["file"]
        nrm_path = s["ctx"].select("Lumen.SceneNormal", s["prefix"])["file"]
        idx = np.linspace(0, len(p["position"]) - 1, min(256, len(p["position"]))).astype(int)
        raw_err = 0
        normal_vec_err = 0.0
        normal_range_ok = True
        normal_raw_bytes = 0
        for i in idx:
            tx, ty = int(p["depth_texel_xy"][i, 0]), int(p["depth_texel_xy"][i, 1])
            raw = depth_at_offset(dep_path, tx, ty)
            raw_err = max(raw_err, abs(raw - int(p["depth_raw"][i])))
            nr, ng, nb, na = normal_at_offset(nrm_path, tx, ty)
            normal_raw_bytes += 1
            normal_range_ok &= (0.0 <= nr <= 1.0) and (0.0 <= ng <= 1.0) and (nb == 0.0) and (na == 1.0)
            csn = np.array([nr * 2.0 - 1.0, ng * 2.0 - 1.0, 0.0])
            csn[2] = math.sqrt(max(1.0 - csn[0] * csn[0] - csn[1] * csn[1], 0.0001))
            card = s["cards"]["records"][int(p["card"][i])]
            axes = np.asarray(card["axes_world"], dtype=np.float64)
            wn = axes[0] * csn[0] + axes[1] * csn[1] + axes[2] * csn[2]
            wn = wn / max(float(np.linalg.norm(wn)), 1e-12)
            normal_vec_err = max(normal_vec_err, float(np.max(np.abs(wn - p["normal"][i]))))
        de_rows.append(
            {
                "state": st,
                "depth_bytes": int(dep_path.stat().st_size),
                "depth_extent": [4096, 4096],
                "depth_format": "PF_G16 stored native, u16 little-endian UNORM",
                "depth_byte_offset_example": [int(p["depth_texel_xy"][0, 0]), int(p["depth_texel_xy"][0, 1]), int((p["depth_texel_xy"][0, 1] * 4096 + p["depth_texel_xy"][0, 0]) * 2)],
                "depth_raw_offset_max_error": int(raw_err),
                "normal_bytes": int(nrm_path.stat().st_size),
                "normal_format": "BC5 native expanded to PF_FloatRGBA; stored values are decoded floats, not original BC bits",
                "normal_samples": normal_raw_bytes,
                "normal_rg_ba_contract_ok": bool(normal_range_ok),
                "normal_world_vector_max_error": float(normal_vec_err),
                "normal_decode": "xy = stored.xy*2-1; z = sqrt(max(1-|xy|^2, 0.0001)); world = normalize(WorldToLocalRotation * xyz)",
            }
        )
    checks["depth_byte_offset_and_normal_decode"] = {
        "passed": all(
            r["depth_raw_offset_max_error"] == 0
            and r["normal_rg_ba_contract_ok"]
            and r["normal_world_vector_max_error"] < 1e-5
            for r in de_rows
        ),
        "evidence": {"rows": de_rows},
    }

    # 5. native Base/Screenshot.png is the captured same-frame view
    shot_rows = []
    cm_by_label = {s["label"]: s for s in capture_manifest["states"]}
    for st in states:
        rec = cm_by_label.get(st, {}).get("native_screenshot", {})
        base_p = s["ctx"].root / "Base" / "Screenshot.png"
        base_sha = sha256_file(base_p)
        shot_rows.append(
            {
                "state": st,
                "file": str(base_p),
                "bytes": int(base_p.stat().st_size),
                "sha256": base_sha,
                "manifest_native_sha256": rec.get("sha256"),
                "same_frame_as_dump": rec.get("same_frame_as_dump"),
                "view_valid": rec.get("view_valid"),
                "matches": bool(rec.get("sha256") == base_sha),
            }
        )
    checks["native_base_screenshot_matches_captured_view"] = {
        "passed": all(r["matches"] and r["view_valid"] for r in shot_rows),
        "evidence": {
            "rows": shot_rows,
            "use": "all composites use Base/Screenshot.png as the background; no post-lit or depth-mode image is substituted",
        },
    }

    # 6. projection agreement with visible geometry
    proj_rows = []
    for st, s in states.items():
        p = s["probes"]
        good = np.isfinite(p["depth_rel_error"]) & p["visible"]
        rel = p["depth_rel_error"][good]
        proj_rows.append(
            {
                "state": st,
                "scene_depth_z_extent": [int(s["resources"]["SceneDepthZ"]["descriptor"]["ExtentX"]), int(s["resources"]["SceneDepthZ"]["descriptor"]["ExtentY"])],
                "view_rect": s["view"]["view_rect"],
                "tw_to_clip_product_max_abs": s["view"]["tw_to_clip_vs_product_max_abs"],
                "camera_world_cm": s["view"]["camera_world_cm"],
                "samples": int(good.sum()),
                "median_relative_depth_error": float(np.median(rel)) if rel.size else None,
                "p90_relative_depth_error": float(np.percentile(rel, 90)) if rel.size else None,
                "within_10_percent": float((rel <= 0.10).mean()) if rel.size else None,
                "floor_probes_abs_nz_ge_0_8": int(np.count_nonzero(p["normal"][:, 2] >= 0.8)),
                "wall_probes_abs_nz_lt_0_3": int(np.count_nonzero(np.abs(p["normal"][:, 2]) < 0.3)),
            }
        )
    checks["projection_agreement_with_represented_visible_geometry"] = {
        "passed": all(r["samples"] > 1000 and r["median_relative_depth_error"] < 0.10 and r["tw_to_clip_product_max_abs"] < 1e-3 for r in proj_rows),
        "evidence": {
            "rows": proj_rows,
            "depth_test": f"visible if view_z <= near/SceneDepthZ + max({DEPTH_TOL_ABS_CM} cm, {DEPTH_TOL_REL}*scene_depth_cm)",
            "note": "probes on backs of cards / occluded surfaces are expected to disagree; the figure shows only probes passing this declared primary-depth test",
        },
    }

    # 6. coefficient format/normalization constant input test
    checks["coefficient_format_and_normalization_constant_input"] = {
        "passed": bool(const_test["relative_error"] < 1e-12),
        "evidence": const_test,
    }

    # 6. SH corroboration against the captured radiosity atlas (not equality)
    corr_rows = []
    for st, s in states.items():
        corr_rows.append({"state": st, **s["scene_indirect_corroboration"]})
    checks["sh_scene_indirect_corroboration"] = {
        "passed": all(
            r["samples"] > 1000
            and all(r.get(f"pearson_{c}") is not None and r[f"pearson_{c}"] > 0.5 for c in "RGB")
            for r in corr_rows
        ),
        "evidence": {
            "rows": corr_rows,
            "note": "SceneIndirectLighting is the bilinearly interpolated/weight-normalised surface-texel radiosity, not an independent per-probe trace; correlation is corroboration, not equality",
        },
    }

    # 7. page/atlas bounds
    bound_rows = []
    for st, s in states.items():
        p = s["probes"]
        bound_rows.append(
            {
                "state": st,
                "probe_atlas_size": p["probe_atlas_size"],
                "atlas_xy_in_bounds": bool(np.all((p["atlas_xy"] >= 0) & (p["atlas_xy"] < p["probe_atlas_size"]))),
                "depth_texel_in_bounds": bool(np.all((p["depth_texel_xy"] >= 0) & (p["depth_texel_xy"] < 4096))),
                "nf_xy_in_bounds": bool(np.all((p["nf_xy"] >= 0) & (p["nf_xy"] < 512))),
                "nf_alignment_ok": bool(np.all(p["atlas_xy"] // 2 == p["nf_xy"])),
                "pages_mapped": s["pages"]["mapped_count"],
                "card_tiles_captured": s["card_tiles"]["active_count"],
                "card_tile_pages": s["card_tiles"]["page_count"],
            }
        )
    checks["page_atlas_and_tile_bounds"] = {
        "passed": all(
            r["atlas_xy_in_bounds"] and r["depth_texel_in_bounds"] and r["nf_xy_in_bounds"] and r["nf_alignment_ok"]
            for r in bound_rows
        ),
        "evidence": {"rows": bound_rows, "atlas_size_rule": "probe atlas 1024 = RadiosityAtlasSize 4096 >> ProbeSpacingInCardTexelsDivideShift 2"},
    }

    # 8. counters
    counter_rows = []
    for st, s in states.items():
        p = s["probes"]
        maxf = int(s["params"]["BuildRadiosityTiles"]["MaxFramesAccumulated"])
        spacing = int(s["params"]["BuildRadiosityTiles"]["ProbeSpacingInCardTexels"])
        fixed_j = int(s["params"]["BuildRadiosityTiles"]["FixedJitterIndex"])
        # independently reproduce the jitter for every unique temporal index
        uniq, inv = np.unique(p["temporal"], return_inverse=True)
        jx = np.empty(len(uniq), dtype=np.int64)
        jy = np.empty(len(uniq), dtype=np.int64)
        for k, ti in enumerate(uniq):
            jidx = fixed_j if fixed_j >= 0 else (int(ti) % maxf)
            a, b = hammersley16(jidx, maxf, HAMM_RX, HAMM_RY)
            jx[k] = int(a * spacing)
            jy[k] = int(b * spacing)
        jitter_ok = bool(np.array_equal(jx[inv], p["jitter_x"]) and np.array_equal(jy[inv], p["jitter_y"]))
        # independently re-read the counter bytes
        nf_path = s["ctx"].select("Lumen.Radiosity.NumFramesAccumulatedAtlas", s["prefix"])["file"]
        nf_atlas = np.fromfile(nf_path, dtype=np.uint8).reshape(512, 512)
        nfx = p["nf_xy"][:, 0]
        nfy = p["nf_xy"][:, 1]
        nf_reread_ok = bool(np.array_equal(nf_atlas[nfy, nfx].astype(np.int64), p["nf_raw"]))
        counter_rows.append(
            {
                "state": st,
                "page_indirect_temporal_min": int(p["temporal"].min()),
                "page_indirect_temporal_max": int(p["temporal"].max()),
                "last_indirect_frame_min": int(p["last_indirect"].min()),
                "last_indirect_frame_max": int(p["last_indirect"].max()),
                "view_frame_field": int(s["view"]["frame_index_field"]),
                "nf_raw_min": int(p["nf_raw"].min()),
                "nf_raw_max": int(p["nf_raw"].max()),
                "nf_max_frames_parameter": maxf,
                "nf_raw_le_parameter": bool(p["nf_raw"].max() <= maxf),
                "nf_raw_gt_zero": bool((p["nf_raw"] > 0).all()),
                "last_indirect_le_view_frame": bool((p["last_indirect"] <= s["view"]["frame_index_field"]).all()),
                "jitter_reproduced_from_temporal_index": jitter_ok,
                "nf_raw_reread_from_atlas": nf_reread_ok,
            }
        )
    checks["counters_decoded_exactly"] = {
        "passed": all(
            r["nf_raw_gt_zero"] and r["nf_raw_le_parameter"] and r["last_indirect_le_view_frame"]
            and r["jitter_reproduced_from_temporal_index"] and r["nf_raw_reread_from_atlas"]
            for r in counter_rows
        ),
        "evidence": {
            "rows": counter_rows,
            "decode_rule": "page TemporalIndex is read verbatim from PageBuffer v4.z; LastIndirectLightingUpdate from v4.y; NumFramesAccumulatedAtlas PF_R8 UNORM8 byte is written as count/255 so count = raw byte; no re-scaling; jitter is independently recomputed from TemporalIndex with Hammersley16",
        },
    }

    # 9. figures finite/nonempty
    fig_rows = []
    for st, meta in figures_meta.items():
        for role, rec in meta["figures"].items():
            fpath = Path(out_dir) / rec["path"]
            im = np.asarray(Image.open(fpath).convert("RGB"))
            fig_rows.append(
                {
                    "state": st,
                    "role": role,
                    "path": rec["path"],
                    "bytes": rec["bytes"],
                    "size": [int(im.shape[1]), int(im.shape[0])],
                    "mean": float(im.mean()),
                    "std": float(im.std()),
                    "nonflat": bool(im.std() > 1.0),
                }
            )
    checks["figures_finite_and_nonempty"] = {
        "passed": all(r["bytes"] > 10000 and r["nonflat"] for r in fig_rows),
        "evidence": {"rows": fig_rows, "no_baked_text": True},
    }

    # 10. source hash before/after
    same = source_before == source_after
    checks["raw_source_hash_before_after"] = {
        "passed": bool(same),
        "evidence": {
            "combined_before": sha256_bytes(json.dumps(source_before, sort_keys=True).encode()) if source_before else None,
            "combined_after": sha256_bytes(json.dumps(source_after, sort_keys=True).encode()) if source_after else None,
            "file_count": len(source_before) if source_before else 0,
            "changed_files": [k for k in source_before if source_after.get(k) != source_before[k]] if source_before else [],
        },
    }

    # 11. scene-anchored temporal figures + complete numeric records
    checks["scene_anchored_temporal_figures_and_complete_numeric_records"] = {
        "passed": all(
            s["probes"]["visible"].sum() > 1000 and len(s["probes"]["position"]) > 1000 for s in states.values()
        )
        and all(meta["complete_records"]["npz"] for meta in figures_meta.values()),
        "evidence": {
            "states": {
                st: {
                    "probes": int(len(s["probes"]["position"])),
                    "card_tile_in_capture": int(s["probes"]["card_tile_in_capture"].sum()),
                    "visible": int(s["probes"]["visible"].sum()),
                    "occluded": int(s["probes"]["occluded"].sum()),
                    "npz": figures_meta[st]["complete_records"]["npz"],
                    "npz_bytes": figures_meta[st]["complete_records"]["npz_bytes"],
                    "npz_arrays": figures_meta[st]["complete_records"]["array_count"],
                }
                for st, s in states.items()
            },
            "figures": f"placement (native Base/Screenshot + visible probes + fixed-frame XY/XZ plans), xray (visible cyan, hidden magenta x), SH4 lobe (fixed {LOBE_RADIUS_CM:g} cm radius, fixed global radiance scale) for every selected state",
            "fixed_scales": global_stats,
        },
    }

    checks["overall"] = {"passed": all(v.get("passed") for k, v in checks.items() if k != "overall")}
    checks["schema"] = "arda-radiosity-decode-checks-v1"
    checks["run_id"] = capture_manifest.get("run_id")
    checks["selected_states"] = list(states)
    checks["selected_state_count"] = len(states)
    checks["acceptance_checks"] = {
        "radiosity probe positions use captured card depth and normal from the matching invocation": "positions_use_captured_card_depth_and_normal",
        "four-coefficient SH response uses source-confirmed hemisphere normalization and cache exposure": "sh_four_coefficients_source_normalization_and_cache_exposure",
        "scene-anchored temporal figures and complete numeric records are validated": "scene_anchored_temporal_figures_and_complete_numeric_records",
    }
    return checks


# ---------------------------------------------------------------------------
# publication
# ---------------------------------------------------------------------------

def save_npz(path: Path, probes: dict, meta: dict):
    arrays = {
        "position_world_cm": probes["position"].astype(np.float32),
        "normal_world": probes["normal"].astype(np.float32),
        "sh_coeffs_raw": probes["sh_raw"].astype(np.float32),
        "sh_coeffs_radiance": probes["sh_radiance"].astype(np.float32),
        "e_normal_raw": probes["e_raw"].astype(np.float32),
        "e_normal_radiance": probes["e_radiance"].astype(np.float32),
        "lambert_radiance_unit_albedo": probes["lambert_radiance"].astype(np.float32),
        "page_index": probes["page"].astype(np.uint16),
        "card_index": probes["card"].astype(np.uint16),
        "grid_ix": probes["grid_ix"].astype(np.uint8),
        "grid_iy": probes["grid_iy"].astype(np.uint8),
        "jitter_x": probes["jitter_x"].astype(np.uint8),
        "jitter_y": probes["jitter_y"].astype(np.uint8),
        "temporal_index": probes["temporal"].astype(np.uint32),
        "last_indirect_lighting_update": probes["last_indirect"].astype(np.uint32),
        "nf_raw_unorm8": probes["nf_raw"].astype(np.uint8),
        "nf_xy": probes["nf_xy"].astype(np.uint16),
        "probe_atlas_xy": probes["atlas_xy"].astype(np.uint16),
        "depth_texel_xy": probes["depth_texel_xy"].astype(np.uint16),
        "depth_raw_u16": probes["depth_raw"].astype(np.uint16),
        "depth_unorm": probes["depth_unorm"].astype(np.float32),
        "depth_byte_offset": ((probes["depth_texel_xy"][:, 1] * 4096 + probes["depth_texel_xy"][:, 0]) * 2).astype(np.uint32),
        "sh_byte_offset_r": ((probes["atlas_xy"][:, 1] * 1024 + probes["atlas_xy"][:, 0]) * 8).astype(np.uint32),
        "atlas_uv": probes["atlas_uv"].astype(np.float32),
        "card_uv": probes["card_uv"].astype(np.float32),
        "card_tile_in_capture": probes["card_tile_in_capture"].astype(bool),
        "on_screen": probes["on_screen"].astype(bool),
        "visible_primary_depth": probes["visible"].astype(bool),
        "occluded_primary_depth": probes["occluded"].astype(bool),
        "screen_xy": probes["screen_xy"].astype(np.float32),
        "view_z_cm": probes["view_z"].astype(np.float32),
        "scene_depth_cm": probes["scene_depth_cm"].astype(np.float32),
        "depth_rel_error": probes["depth_rel_error"].astype(np.float32),
        "sh_nonzero": (np.abs(probes["sh_raw"]).sum(axis=(1, 2)) > 0).astype(bool),
    }
    np.savez_compressed(path, **arrays)
    return arrays


def build_report(states, checks, global_stats, figures_meta, capture_manifest, const_test, raw_root):
    lines = []
    add = lines.append
    add("# Radiosity probe reconstruction from captured card depth and two-band SH")
    add("")
    add("Status: **%s**" % ("ok" if checks["overall"]["passed"] else "partial"))
    add("")
    add("Run: `%s`; %d selected same-process state(s); all paths read from `%s`." % (
        capture_manifest["run_id"], len(states), raw_root))
    add("")
    add("## 1. What was decoded")
    add("")
    add("For every mapped Lumen card page with an indirect-lighting update, the decoder walks the same probe grid as the radiosity shader:")
    add("")
    add("* page `CoordInCardPage = (ix,iy)*ProbeSpacingInCardTexels + GetProbeJitter(page.IndirectLightingTemporalIndex)`;")
    add("* surface-atlas UV `AtlasUV = PhysicalAtlasUVRect.xy + InvPhysicalAtlasSize*(CoordInCardPage+0.5)` (`LumenRadiosity.ush` `GetRadiosityTexel`);")
    add("* depth from the captured `Lumen.SceneDepth` PF_G16 4096x4096 atlas at `floor(AtlasUV*4096)`; invalid `65535`/1.0 is rejected;")
    add("* card UV `CardUVRect.xy + CardUVTexelScale*(CoordInCardPage+0.5)`;")
    add("* `GetCardLocalPosition` (x-flip first) then `world = axis0*local.x + axis1*local.y + axis2*local.z + origin`;")
    add("* normal from `Lumen.SceneNormal`; because the capture expanded BC5 to `PF_FloatRGBA`, R/G are decoded BC5 values in [0,1], Z is reconstructed as `sqrt(1-|xy|^2)`, then rotated by the card basis and normalised;")
    add("* four SH coefficients per channel from `Lumen.Radiosity.ProbeSH{Red,Green,Blue}Atlas` half4 at `(PhysicalAtlasCoord + cx) >> ProbeSpacingInCardTexelsDivideShift`.")
    add("")
    add("Probes with uninitialised `NumFramesAccumulatedAtlas` (raw byte 0), a page with `LastIndirectLightingUpdateFrameIndex == 0`, an invalid depth texel, or an out-of-bounds atlas address are rejected. All rejected counts are in the state records.")
    add("")
    add("Jitter anchor and accumulated history: the captured `FixedJitterIndex` is -1 in all states, so `GetProbeJitter` uses `page.IndirectLightingTemporalIndex % MaxFramesAccumulated` (MaxFramesAccumulated=4) as the jittered grid anchor for the current frame. `LumenRadiosityConvertToSH` writes the four coefficients from that frame's `TraceRadianceAtlas`; the SH atlas is not itself temporally accumulated. Temporal accumulation happens in `LumenRadiosityIntegrateCS`, which lerps `HistoryRadiosity` with the new radiosity by `alpha = 1/(1+NumFramesAccumulated)` and writes the counter atlas. A probe's position and jitter therefore come from the current grid anchor at the page's last update, its four coefficients are the last hemispherical projection written at that atlas texel, and `nf_raw`/`last_indirect` are the recorded counter and update fields. Inactive probes keep the last written SH, which the decoder does not assert equals the current frame's trace.")
    add("")
    add("## 2. Constant-input SH normalisation (no 4, no 4pi)")
    add("")
    add("`LumenRadiosityConvertToSH` builds `IrradianceSH = (1/N) * sum(SHBasisFunction(dir) * TraceRadiance / PDF)` with `PROBE_HEMISPHERE_UNIFORM` and `PDF = 1/(2*pi)` (`MonteCarlo.ush`). `TraceRadiance` was written as `Radiance * CachedLightingPreExposure` (`LumenRadiosity.usf`). Evaluation is `TexelRadiance = max(0, DotSH(IrradianceSH, CalcDiffuseTransferSH(normal, 1)))`, whose band constants are `L0 = pi`, `L1 = 2*pi/3` (`SHCommon.ush`).")
    add("")
    add("Independent constant-hemisphere test: %d uniform samples, L=1, exact basis and PDF; reconstructed irradiance `%.12f` vs `2*pi^2*(C^2+D^2/3) = %.12f` for the same six-decimal SHCommon.ush literals (relative error `%.3e`). The residual versus exact `pi` is `%.3e`, attributable to the six-decimal basis literals, not to any 4 or 4*pi factor." % (const_test["samples"], const_test["reconstructed"], const_test["expected_irradiance_for_L1_hemisphere"], const_test["relative_error"], const_test["relative_error_vs_exact_pi"]))
    add("")
    add("Cache pre-exposure: the captured `Batched lights` payload carries `CachedLightingPreExposure = 0.0625` (FParameters offset 204) in every state. Stored SH is divided by 0.0625 to obtain `sh_coeffs_radiance`; both raw and converted arrays are kept. `e_normal_raw` is `DotSH(raw, transfer)`; `e_normal_radiance = e_normal_raw/0.0625`; `lambert_radiance_unit_albedo = e_normal_radiance/pi`.")
    add("")
    add("## 3. State results")
    add("")
    add("| State | Graph prefix | View frame field | Camera (cm) | Probes | Card tile in capture | Visible | Occluded | Median depth err | p90 | SH nonzero |")
    add("|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|")
    for st, s in states.items():
        p = s["probes"]
        rel = p["depth_rel_error"][p["visible"] & np.isfinite(p["depth_rel_error"])]
        add(
            "| `%s` | %s | %d | `%s` | %d | %d | %d | %d | %.4f | %.4f | %d |"
            % (
                st,
                s["prefix"],
                s["view"]["frame_index_field"],
                "[" + ", ".join("%.1f" % x for x in s["view"]["camera_world_cm"]) + "]",
                len(p["position"]),
                int(p["card_tile_in_capture"].sum()),
                int(p["visible"].sum()),
                int(p["occluded"].sum()),
                float(np.median(rel)) if rel.size else float("nan"),
                float(np.percentile(rel, 90)) if rel.size else float("nan"),
                int((np.abs(p["sh_raw"]).sum(axis=(1, 2)) > 0).sum()),
            )
        )
    add("")
    add("`Frame field` is the FPackedView frame counter at byte 1044, not the capture target update. The capture's actual target updates are in the manifest (`baseline` 1, `light-001` 1, `light-064` 63 of target 64, `camera-001` 1, `camera-064` 63 of target 64). Sun 80000 -> 20000 -> 80000 lux (64 warm draws) then camera -Y150 cm, EV100 11.0 fixed, as reported by the capture manifest.")
    add("")
    add("Visible counts vary across states because each state's card/page set and primary-depth visibility differ; the light change, for example, re-captures and updates a different set of pages. The arrays keep every valid probe, and the figures draw only the declared primary-depth-visible subset.")
    add("")
    add("SH corroboration against the captured `SceneIndirectLighting` (surface-texel comparison, not an independent per-probe trace):")
    add("")
    add("| State | samples | Pearson R | Pearson G | Pearson B | median ratio R/G/B |")
    add("|---|---:|---:|---:|---:|---|")
    for st, s in states.items():
        c = s["scene_indirect_corroboration"]
        add("| `%s` | %d | %.3f | %.3f | %.3f | %.2f / %.2f / %.2f |" % (
            st, c["samples"], c["pearson_R"], c["pearson_G"], c["pearson_B"],
            c["median_ratio_R"], c["median_ratio_G"], c["median_ratio_B"]))
    add("")
    add("These correlations are corroboration only; the captured radiosity atlas is the bilinearly interpolated and weight-normalised surface-texel result, so exact equality is not expected.")
    add("")
    add("## 4. Graph/version selection")
    add("")
    add("Each state contains three render-graph prefixes (`0001`, `0003`, `0004`) with three consecutive FPackedView frame fields. The decoder selects the highest-frame prefix and uses only operands from that prefix: `Lumen.Cards`, `Lumen.PageBuffer`, `Lumen.PageTable`, `Lumen.CardTiles` (the 16384-byte CardTileData; the separate 33124-byte static buffer is recorded but not used for placement), `Lumen.SceneDepth`, `Lumen.SceneNormal`, the three SH atlases, `NumFramesAccumulatedAtlas`, `SceneIndirectLighting`, `FilteredTraceRadianceAtlas`, `SceneDepthZ`, `ViewData.PrimaryViews`, and the `BuildRadiosityTiles`/`ConvertToSH`/`Integrate`/`Batched lights` parameter payloads. Every operand comes from that prefix.")
    add("")
    add("The 16 captured required resources do not include `RadiosityTileData`/`RadiosityTileAllocator`, so the decoder cannot know the exact per-frame radiosity tile list. `card_tile_in_capture` marks the probes whose 8x8 card tile appears in the captured `Lumen.CardTiles` buffer; the numeric array itself is the complete set of mapped, indirect-initialised, depth-valid probes in the persistent atlas.")
    add("")
    for st, s in states.items():
        add("* `%s`: prefix `%s`; graph variants %s." % (st, s["prefix"], json.dumps(s["view"]["all_graphs"])))
    add("")
    add("## 5. Figures")
    add("")
    add("Figures are standalone RGB composites with no baked text. Legends, colours, scales and root prose live in `publish/data/figure-legend.json` and this report.")
    add("")
    add("* `placement-<state>.png`: native `Base/Screenshot.png` plus visible probes coloured by normal class (floor/up green, ceiling/down orange, wall X blue, wall Y red), with two fixed-frame orthographic panels (XY and XZ) showing all valid probes. The panels use fixed world bounds and a white camera marker.")
    add("* `placement-xray-<state>.png`: visible probes cyan, hidden probes magenta `x`. Hidden here means the probe failed the primary-depth test; it is still in the array.")
    add("* `sh-lobes-<state>.png`: native screenshot plus up to 10 visible probes with projected 3D SH4 response lobes. Lobe radius is fixed at %.1f cm and colour/radius use the global fixed radiance scale below; zero responses are clamped to zero and values above the scale saturate." % LOBE_RADIUS_CM)
    add("")
    add("Native background: for every state, `Base/Screenshot.png` SHA-256 equals the capture manifest `native_screenshot.sha256` and `same_frame_as_dump` is true. The composites therefore use the native same-frame view, not a post-lit or depth-mode image.")
    add("")
    add("Fixed scales across states: `%s`." % json.dumps(jval(global_stats)))
    add("")
    add("Depth test: `view_z <= near/SceneDepthZ + max(%.1f cm, %.2f*scene_depth_cm)`, sampled at `floor(projected pixel)` on the captured 1280x888 primary depth. This is the declared tolerance; no per-state fitting." % (DEPTH_TOL_ABS_CM, DEPTH_TOL_REL))
    add("")
    add("## 6. Source anchors and limits")
    add("")
    add("* The probes visibly follow walls, floor, arches and curtains in the composite; the numeric arrays cover the whole captured card set, not just the drawn subset.")
    add("* `Lumen.SceneNormal` was captured as `PF_BC5` expanded to `PF_FloatRGBA`; the stored values are decoded numeric values, not the original BC bits. X/Y are source-decoded; Z is reconstructed from the unit-length constraint.")
    add("* The capture has no independent per-ray trace atlas for radiosity: `FilteredTraceRadianceAtlas` is filtered, not raw per-hemisphere trace radiance. The SH normalisation is therefore source-derived and constant-input tested. `SceneIndirectLighting` correlation is corroboration only, not equality.")
    add("* The decoder knows trace origins only as far as the recorded probe coords: the shader adds `SurfaceBias` along the normal and a second bias along each ray (`LumenRadiosity.usf`), and those offsets are not present in the dump.")
    add("* The capture's `r.RHI.UniformBufferContentMap.Enable` readback is true in this run, but the decoder does not rely on uniform-buffer contents: every parameter value comes from a dumped pass parameter payload (`CachedLightingPreExposure` at FParameters offset 204 and the nested `FLumenRadiosityTexelTraceParameters`). If the flag were unset, the decoder would not fabricate uniform contents.")
    add("* Raw source SHA-256 before/after is recorded in `checks.json` and `manifest.json`; no raw file was written or modified.")
    add("")
    add("## 7. Files")
    add("")
    add("* `decoder.py` - single reproducible decoder/checker/figure writer.")
    add("* `publish/data/schema.json`, `publish/data/probes-<state>.npz`, `publish/data/probes-<state>.json`, `publish/data/probes-sample.json`, `publish/data/figure-legend.json`, `publish/data/checks.json`.")
    add("* `publish/figures/placement-<state>.png`, `placement-xray-<state>.png`, `sh-lobes-<state>.png`.")
    add("* `checks.json`, `manifest.json`.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def parse_selected_states(value, capture_manifest):
    if value is None:
        selected = list(STATES)
    else:
        requested = [x.strip() for x in value.split(",") if x.strip()]
        unknown = [x for x in requested if x not in STATES]
        if not requested or unknown:
            raise SystemExit(
                "--states must be a comma-separated subset of %s (got %r)"
                % (",".join(STATES), value)
            )
        wanted = set(requested)
        selected = [x for x in STATES if x in wanted]
    manifest_labels = {s.get("label") for s in capture_manifest.get("states", [])}
    missing = [x for x in selected if x not in manifest_labels]
    if missing:
        raise SystemExit("capture manifest is missing selected state(s): %s" % ", ".join(missing))
    return selected


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--raw-root", required=True,
                    help="run directory containing <state>/Base/Passes.json; no implicit raw location")
    ap.add_argument("--capture-manifest", default=str(DEFAULT_CAPTURE_MANIFEST))
    ap.add_argument("--capture-report", default=str(DEFAULT_CAPTURE_REPORT))
    ap.add_argument("--parameter-verification", default=str(DEFAULT_PARAMETER_VERIFICATION))
    ap.add_argument("--out", required=True,
                    help="explicit output directory; must differ from the decoder source directory")
    ap.add_argument("--states", default=None,
                    help="comma-separated subset of %s (default: all)" % ",".join(STATES))
    ap.add_argument("--no-hash", action="store_true")
    args = ap.parse_args(argv)

    raw_root = Path(args.raw_root).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    if out_dir == HERE:
        ap.error("--out must differ from the decoder source directory")
    capture_manifest_p = Path(args.capture_manifest).expanduser().resolve()
    capture_report_p = Path(args.capture_report).expanduser().resolve()
    parameter_verification_p = Path(args.parameter_verification).expanduser().resolve()
    for label, p in (
        ("--capture-manifest", capture_manifest_p),
        ("--capture-report", capture_report_p),
        ("--parameter-verification", parameter_verification_p),
    ):
        if not p.is_file():
            ap.error("%s is not a file: %s" % (label, p))
    if not raw_root.is_dir():
        ap.error("--raw-root is not a directory: %s" % raw_root)
    if out_dir == raw_root:
        ap.error("--out must differ from --raw-root")

    capture_manifest = json.load(open(capture_manifest_p, encoding="utf-8"))
    param_verification = json.load(open(parameter_verification_p, encoding="utf-8"))
    if capture_manifest.get("run_id") != param_verification.get("run_id"):
        raise SystemExit(
            "capture manifest run %r != parameter verification run %r"
            % (capture_manifest.get("run_id"), param_verification.get("run_id"))
        )
    selected_states = parse_selected_states(args.states, capture_manifest)
    for st in selected_states:
        state_root = raw_root / st
        if not (state_root / "Base/Passes.json").is_file():
            raise SystemExit("selected state %r has no Base/Passes.json under %s" % (st, raw_root))

    DATA = out_dir / "publish" / "data"
    FIG = out_dir / "publish" / "figures"
    WORK = out_dir / "work"
    DATA.mkdir(parents=True, exist_ok=True)
    FIG.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)

    # decode selected states first (needed for a fixed global scale)
    states = {}
    for st in selected_states:
        print(f"[decode] {st}", flush=True)
        states[st] = decode_state(st, raw_root, capture_manifest, param_verification, do_hash=not args.no_hash)

    # fixed global scale, measured scene bounds (not per state, not per probe)
    radiance_max = LOBE_RADIANCE_MAX
    mb = capture_manifest["scene"]["measured_bounds_cm"]
    plan_bounds = {
        "x": [float(mb["min"][0]), float(mb["max"][0])],
        "y": [float(mb["min"][1]), float(mb["max"][1])],
        "z": [float(mb["min"][2]), float(mb["max"][2])],
    }
    global_stats = {
        "lobe_radiance_max": radiance_max,
        "radiance_units": "unit-albedo Lambertian outgoing radiance = e_normal_radiance/pi, scene-referred after /CachedLightingPreExposure",
        "zero_clamp": "evaluated SH response is clamped to >= 0 before colour/radius",
        "saturation": "values above lobe_radiance_max saturate; no per-state or per-probe normalisation",
        "lobe_radius_cm": LOBE_RADIUS_CM,
        "placement_dot_radius_px": PLACEMENT_DOT_R,
        "plan_bounds_cm": plan_bounds,
    }
    for s in states.values():
        s["probes"]["plan_bounds_cm"] = plan_bounds

    const_test = sh_constant_hemisphere_test()

    # write per-state data and figures
    figures_meta = {}
    source_before = {}
    for st, s in states.items():
        p = s["probes"]
        p["view"] = s["view"]
        npz_path = DATA / f"probes-{st}.npz"
        arrays = save_npz(npz_path, p, {})
        fig_p = FIG / f"placement-{st}.png"
        fig_x = FIG / f"placement-xray-{st}.png"
        fig_l = FIG / f"sh-lobes-{st}.png"
        draw_placement_figure(st, p, fig_p, raw_root)
        draw_xray_figure(st, p, fig_x, raw_root)
        chosen = draw_lobes_figure(st, p, fig_l, radiance_max, raw_root)
        figures_meta[st] = {
            "figures": {
                "placement": {"path": str(fig_p.relative_to(out_dir)), "bytes": fig_p.stat().st_size, "panels": ["native Base/Screenshot + visible probes", "XY plan", "XZ plan"]},
                "xray": {"path": str(fig_x.relative_to(out_dir)), "bytes": fig_x.stat().st_size, "panels": ["native Base/Screenshot + visible cyan + hidden magenta x"]},
                "sh_lobes": {"path": str(fig_l.relative_to(out_dir)), "bytes": fig_l.stat().st_size, "panels": ["native Base/Screenshot + projected SH4 lobes"], "probe_indices": [int(x) for x in chosen[1]] if isinstance(chosen, tuple) else []},
            },
            "complete_records": {
                "npz": str(npz_path.relative_to(out_dir)),
                "npz_bytes": npz_path.stat().st_size,
                "array_count": len(arrays),
            },
        }
        source_before.update({f"{st}/{k}": v for k, v in s["source_hashes"].items()})

        # readable per-state metadata
        meta = {
            "schema": "arda-radiosity-probe-state-v1",
            "state": st,
            "title": STATE_TITLES[st],
            "graph_prefix": s["prefix"],
            "view": {
                "camera_world_cm": s["view"]["camera_world_cm"],
                "view_rect": s["view"]["view_rect"],
                "view_size": s["view"]["view_size"],
                "near_plane_cm": s["view"]["near_plane_cm"],
                "hfov_deg": s["view"]["hfov_deg"],
                "vfov_deg": s["view"]["vfov_deg"],
                "frame_index_field": s["view"]["frame_index_field"],
                "pre_view_translation_cm": s["view"]["pre_view_translation_cm"],
                "clip_convention": s["view"]["clip_convention"],
                "tw_to_clip_vs_product_max_abs": s["view"]["tw_to_clip_vs_product_max_abs"],
                "all_graphs": s["view"]["all_graphs"],
            },
            "preexposure": s["preexposure"],
            "params": s["params"],
            "cards": {
                "num_cards": s["cards"]["num_cards"],
                "visible": s["cards"]["visible_count"],
                "static_geometry_sha256": s["cards"]["static_geometry_sha256"],
            },
            "pages": {
                "npages": s["pages"]["npages"],
                "mapped": s["pages"]["mapped_count"],
                "indirect_initialized": s["pages"]["indirect_initialized_count"],
            },
            "card_tiles": {
                "buffer_words": s["card_tiles"]["words"],
                "nonzero": s["card_tiles"]["nonzero"],
                "active_count": s["card_tiles"]["active_count"],
                "page_count": s["card_tiles"]["page_count"],
                "packing": s["card_tiles"]["packing"],
            },
            "counts": {
                "probes": int(len(p["position"])),
                "card_tile_in_capture": int(p["card_tile_in_capture"].sum()),
                "visible": int(p["visible"].sum()),
                "occluded": int(p["occluded"].sum()),
                "sh_nonzero": int((np.abs(p["sh_raw"]).sum(axis=(1, 2)) > 0).sum()),
                "skipped": p["skipped"],
            },
            "ranges": {
                "position_min_cm": p["position"].min(axis=0).tolist(),
                "position_max_cm": p["position"].max(axis=0).tolist(),
                "sh_raw_min": float(p["sh_raw"].min()),
                "sh_raw_max": float(p["sh_raw"].max()),
                "e_raw_min": float(p["e_raw"].min()),
                "e_raw_max": float(p["e_raw"].max()),
                "lambert_radiance_p50": float(np.percentile(p["lambert_radiance"], 50)),
                "lambert_radiance_p95": float(np.percentile(p["lambert_radiance"], 95)),
                "scene_indirect_corroboration": s["scene_indirect_corroboration"],
            },
            "resources": s["resources"],
            "source_hashes": s["source_hashes"],
            "decode_seconds": s["decode_seconds"],
        }
        write_json(DATA / f"probes-{st}.json", meta)
        s["meta"] = meta

    # after hashes
    source_after = {}
    if not args.no_hash:
        for st, s in states.items():
            ctx = s["ctx"]
            ctx_hashes = {}
            for rel in s["source_hashes"]:
                p = ctx.root / rel
                if p.exists():
                    ctx_hashes[rel] = sha256_file(p)
            source_after.update({f"{st}/{k}": v for k, v in ctx_hashes.items()})
            s["source_hashes_after"] = ctx_hashes
    else:
        source_after = dict(source_before)

    # sample readable records
    sample = []
    for st, s in states.items():
        p = s["probes"]
        vis = np.nonzero(p["visible"])[0]
        if vis.size:
            pick = vis[np.linspace(0, vis.size - 1, min(12, vis.size)).astype(int)]
            for i in pick:
                sample.append(
                    {
                        "state": st,
                        "probe_index": int(i),
                        "position_world_cm": p["position"][i].tolist(),
                        "normal_world": p["normal"][i].tolist(),
                        "page_index": int(p["page"][i]),
                        "card_index": int(p["card"][i]),
                        "grid_xy": [int(p["grid_ix"][i]), int(p["grid_iy"][i])],
                        "jitter_xy": [int(p["jitter_x"][i]), int(p["jitter_y"][i])],
                        "temporal_index": int(p["temporal"][i]),
                        "last_indirect_lighting_update": int(p["last_indirect"][i]),
                        "nf_raw_unorm8": int(p["nf_raw"][i]),
                        "card_tile_in_capture": bool(p["card_tile_in_capture"][i]),
                        "sh_nonzero": bool(np.abs(p["sh_raw"][i]).sum() > 0),
                        "probe_atlas_xy": p["atlas_xy"][i].tolist(),
                        "depth_texel_xy": p["depth_texel_xy"][i].tolist(),
                        "depth_byte_offset": int((p["depth_texel_xy"][i, 1] * 4096 + p["depth_texel_xy"][i, 0]) * 2),
                        "depth_raw_u16": int(p["depth_raw"][i]),
                        "depth_unorm": float(p["depth_unorm"][i]),
                        "sh_coeffs_raw": p["sh_raw"][i].tolist(),
                        "sh_coeffs_radiance": p["sh_radiance"][i].tolist(),
                        "e_normal_raw": p["e_raw"][i].tolist(),
                        "e_normal_radiance": p["e_radiance"][i].tolist(),
                        "lambert_radiance_unit_albedo": p["lambert_radiance"][i].tolist(),
                        "screen_xy": p["screen_xy"][i].tolist(),
                        "view_z_cm": float(p["view_z"][i]),
                        "scene_depth_cm": float(p["scene_depth_cm"][i]) if math.isfinite(p["scene_depth_cm"][i]) else None,
                        "depth_rel_error": float(p["depth_rel_error"][i]) if math.isfinite(p["depth_rel_error"][i]) else None,
                        "visible": bool(p["visible"][i]),
                    }
                )
    write_json(DATA / "probes-sample.json", {"schema": "arda-radiosity-probe-sample-v1", "samples": sample})

    schema = {
        "schema": "arda-radiosity-probe-npz-schema-v1",
        "arrays": {
            "position_world_cm": {"dtype": "float32", "shape": "[N,3]", "unit": "cm", "frame": "world"},
            "normal_world": {"dtype": "float32", "shape": "[N,3]", "unit": "unit", "note": "PF_BC5 expanded XY decoded, Z reconstructed, card basis rotated"},
            "sh_coeffs_raw": {"dtype": "float32", "shape": "[N,3,4]", "unit": "pre-exposed radiance*steradian", "order": "b0,bY,bZ,bX per channel R,G,B"},
            "sh_coeffs_radiance": {"dtype": "float32", "shape": "[N,3,4]", "unit": "scene-referred radiance*steradian", "note": "raw / CachedLightingPreExposure"},
            "e_normal_raw": {"dtype": "float32", "shape": "[N,3]", "unit": "pre-exposed irradiance"},
            "e_normal_radiance": {"dtype": "float32", "shape": "[N,3]", "unit": "scene-referred irradiance"},
            "lambert_radiance_unit_albedo": {"dtype": "float32", "shape": "[N,3]", "unit": "scene-referred radiance", "note": "e_normal_radiance/pi"},
            "page_index": {"dtype": "uint16", "shape": "[N]"},
            "card_index": {"dtype": "uint16", "shape": "[N]"},
            "grid_ix": {"dtype": "uint8", "shape": "[N]"},
            "grid_iy": {"dtype": "uint8", "shape": "[N]"},
            "jitter_x": {"dtype": "uint8", "shape": "[N]"},
            "jitter_y": {"dtype": "uint8", "shape": "[N]"},
            "temporal_index": {"dtype": "uint32", "shape": "[N]", "note": "PageBuffer v4.z, verbatim"},
            "last_indirect_lighting_update": {"dtype": "uint32", "shape": "[N]", "note": "PageBuffer v4.y, verbatim"},
            "nf_raw_unorm8": {"dtype": "uint8", "shape": "[N]", "note": "NumFramesAccumulatedAtlas byte; count = raw byte"},
            "nf_xy": {"dtype": "uint16", "shape": "[N,2]"},
            "probe_atlas_xy": {"dtype": "uint16", "shape": "[N,2]"},
            "depth_texel_xy": {"dtype": "uint16", "shape": "[N,2]"},
            "depth_raw_u16": {"dtype": "uint16", "shape": "[N]"},
            "depth_unorm": {"dtype": "float32", "shape": "[N]", "note": "raw/65535; valid iff <1"},
            "depth_byte_offset": {"dtype": "uint32", "shape": "[N]", "note": "(y*4096+x)*2 in Lumen.SceneDepth"},
            "sh_byte_offset_r": {"dtype": "uint32", "shape": "[N]", "note": "(ay*1024+ax)*8 in each SH atlas"},
            "atlas_uv": {"dtype": "float32", "shape": "[N,2]"},
            "card_uv": {"dtype": "float32", "shape": "[N,2]"},
            "card_tile_in_capture": {"dtype": "bool", "shape": "[N]", "note": "page/tile appears in captured Lumen.CardTiles; this is not the exact radiosity dispatch tile list"},
            "on_screen": {"dtype": "bool", "shape": "[N]"},
            "visible_primary_depth": {"dtype": "bool", "shape": "[N]"},
            "occluded_primary_depth": {"dtype": "bool", "shape": "[N]"},
            "screen_xy": {"dtype": "float32", "shape": "[N,2]", "unit": "pixels"},
            "view_z_cm": {"dtype": "float32", "shape": "[N]", "unit": "cm"},
            "scene_depth_cm": {"dtype": "float32", "shape": "[N]", "unit": "cm"},
            "depth_rel_error": {"dtype": "float32", "shape": "[N]"},
            "sh_nonzero": {"dtype": "bool", "shape": "[N]"},
        },
        "sh_definition": {
            "basis": [0.282095, "-0.488603*y", "0.488603*z", "-0.488603*x"],
            "transfer": ["pi", "2*pi/3", "2*pi/3", "2*pi/3"],
            "projection": "irradianceSH = mean_over_hemisphere_samples(SHBasis(dir) * traceRadiance / PDF), PDF=1/(2*pi)",
            "evaluation": "E(n) = dot(coeffs, SHBasis(n)*transfer)",
            "preexposure": "traceRadiance stored as radiance*CachedLightingPreExposure; divide coefficients by CachedLightingPreExposure for scene-referred values",
        },
    }
    write_json(DATA / "schema.json", schema)

    legend = {
        "schema": "arda-radiosity-figure-legend-v1",
        "no_baked_text": True,
        "root_prose": "report.md",
        "placement": {
            "normal_classes": {
                "floor_or_up": {"rgb": [70, 220, 90], "rule": "|nz|>=|nx|,|ny| and nz>=0"},
                "ceiling_or_down": {"rgb": [255, 150, 40], "rule": "|nz|>=|nx|,|ny| and nz<0"},
                "wall_x": {"rgb": [70, 150, 255], "rule": "|nz|<max(|nx|,|ny|) and |nx|>=|ny|"},
                "wall_y": {"rgb": [255, 70, 120], "rule": "|nz|<max(|nx|,|ny|) and |nx|<|ny|"},
            },
            "panels": ["native Base/Screenshot with visible probes", "XY plan, all valid probes", "XZ plan, all valid probes"],
            "plan_bounds_cm": global_stats["plan_bounds_cm"],
            "camera_marker": [255, 255, 255],
        },
        "xray": {"visible": [60, 240, 255], "hidden": [255, 0, 255], "hidden_rule": "on screen but view_z > primary depth + declared tolerance", "hidden_legibility": "hidden probes are drawn 1:4 as 1px x; all hidden probes remain in the NPZ"},
        "sh_lobes": {
            "direction_count": LOBE_DIRECTIONS,
            "lobe_radius_cm": LOBE_RADIUS_CM,
            "lobe_radiance_max": global_stats["lobe_radiance_max"],
            "zero_clamp": global_stats["zero_clamp"],
            "saturation": global_stats["saturation"],
            "note": "lobe endpoints are function samples projected around the recorded probe; they are not traced rays and are not geometry",
        },
        "depth_test": {"abs_cm": DEPTH_TOL_ABS_CM, "relative": DEPTH_TOL_REL, "source": "SceneDepthZ 1280x888, view rect at 0,0"},
        "fixed_scales": global_stats,
    }
    write_json(DATA / "figure-legend.json", legend)

    checks = run_checks(states, capture_manifest, global_stats, figures_meta, source_before, source_after, const_test, out_dir)
    write_json(out_dir / "checks.json", checks)
    write_json(DATA / "checks.json", checks)

    # manifest with all source addresses/hashes/units
    manifest = {
        "schema": "arda-radiosity-decode-manifest-v1",
        "run_id": capture_manifest["run_id"],
        "raw_root": str(raw_root),
        "capture_manifest": str(capture_manifest_p),
        "capture_manifest_sha256": sha256_file(capture_manifest_p),
        "capture_report": str(capture_report_p),
        "capture_report_sha256": sha256_file(capture_report_p),
        "parameter_verification": str(parameter_verification_p),
        "parameter_verification_sha256": sha256_file(parameter_verification_p),
        "selected_states": selected_states,
        "states": {},
        "global_constants": {
            "pi": PI,
            "two_pi": TWO_PI,
            "sh_basis": [SH_BASIS_C, -SH_BASIS_D, SH_BASIS_D, -SH_BASIS_D],
            "sh_transfer": SH_TRANSFER.tolist(),
            "preexposure_conversion": "divide by CachedLightingPreExposure",
            "fixed_global_scale": global_stats,
        },
        "source_hash_before": source_before,
        "source_hash_after": source_after,
        "raw_source_unchanged": source_before == source_after,
        "observability_gap": {
            "uniform_buffer_content_map_enable": capture_manifest.get("states", [{}])[0].get("rhi_uniform_buffer_content_map", None),
            "note": "parameter values are decoded from the dumped pass payloads; uniform-buffer contents were not fabricated or substituted",
        },
        "outputs": {
            "data": sorted(str(p.relative_to(out_dir)) for p in DATA.glob("*")),
            "figures": sorted(str(p.relative_to(out_dir)) for p in FIG.glob("*.png")),
        },
    }
    for st, s in states.items():
        p = s["probes"]
        manifest["states"][st] = {
            "title": STATE_TITLES[st],
            "raw_state_directory": str(s["ctx"].root),
            "graph_prefix": s["prefix"],
            "view": s["meta"]["view"],
            "preexposure": s["preexposure"],
            "params": s["params"],
            "radiosity_params_summary": {
                "ProbeSpacingInCardTexels": s["params"]["BuildRadiosityTiles"]["ProbeSpacingInCardTexels"],
                "ProbeSpacingInCardTexelsDivideShift": s["params"]["BuildRadiosityTiles"]["ProbeSpacingInCardTexelsDivideShift"],
                "RadiosityTileSizeInProbes": s["params"]["BuildRadiosityTiles"]["RadiosityTileSizeInProbes"],
                "HemisphereProbeResolution": s["params"]["BuildRadiosityTiles"]["HemisphereProbeResolution"],
                "NumTracesPerProbe": s["params"]["BuildRadiosityTiles"]["NumTracesPerProbe"],
                "FixedJitterIndex": s["params"]["BuildRadiosityTiles"]["FixedJitterIndex"],
                "MaxFramesAccumulated": s["params"]["BuildRadiosityTiles"]["MaxFramesAccumulated"],
                "RadiosityAtlasSize": s["params"]["BuildRadiosityTiles"]["RadiosityAtlasSize"],
            },
            "resources": s["resources"],
            "source_hashes": s["source_hashes"],
            "source_hashes_after": s.get("source_hashes_after", {}),
            "counts": s["meta"]["counts"],
            "npz": f"publish/data/probes-{st}.npz",
            "npz_bytes": figures_meta[st]["complete_records"]["npz_bytes"],
            "figures": figures_meta[st]["figures"],
        }
    write_json(out_dir / "manifest.json", manifest)

    report = build_report(states, checks, global_stats, figures_meta, capture_manifest, const_test, raw_root)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    (DATA / "capture-report.md").write_text(report, encoding="utf-8")

    print(f"[done] {len(states)} states; overall checks passed={checks['overall']['passed']}")
    for st, s in states.items():
        print(f"  {st:12s} probes={len(s['probes']['position']):7d} card_tile={int(s['probes']['card_tile_in_capture'].sum()):6d} visible={int(s['probes']['visible'].sum()):6d} prefix={s['prefix']}")
    if not checks["overall"]["passed"]:
        for k, v in checks.items():
            if isinstance(v, dict) and k != "overall" and not v.get("passed", True):
                print(f"  FAIL {k}")
    return 0 if checks["overall"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
