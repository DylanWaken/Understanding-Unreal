#!/usr/bin/env python3
"""Decode native Unreal DumpGPU screen-probe placement and radiance SH.

The decoder is deliberately name-driven.  It accepts one or more native
DumpGPU roots, finds the final producing pass by event name, resolves the
produced resource version from that pass's captured Pointer, and reads the
subresource layout / parameter metadata recorded by the dump.  No resource id
from any earlier dump is embedded in this file.

Usage
-----
python decoder.py --dump-root D:/captures/run \\
    --states baseline,lightstep-001,lightstep-008,lightstep-032,lightstep-064 \\
    --out D:/work/temporal-sh --stage all

or repeat ``--state NAME=PATH`` for arbitrary selected roots.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# declared conventions / fixed presentation scales
# ---------------------------------------------------------------------------

STATES_DEFAULT = ["baseline", "lightstep-001", "lightstep-008",
                  "lightstep-032", "lightstep-064"]

# UE FThreeBandSHVector / SHBasisFunction3 order.  The coefficient list is
# [L0, L1.y, L1.z, L1.x, L2.xy, L2.yz, L2.z, L2.xz, L2.xy2] in UE's packing.
SH_ORDER = ["L0", "L1_y", "L1_z", "L1_x", "L2_xy", "L2_yz", "L2_z", "L2_xz", "L2_xy2"]

# Sphere-average radiance-SH convention: each coefficient is the mean over
# the captured 8x8 equi-area directions of basis(direction) * radiance.
# D(n) = 4 * sum_i sh_i * CalcDiffuseTransferSH3(n)_i (AO=1, exponent=1).
# The extra 4 makes a constant radiance field evaluate to that radiance.
FOUR_PI = 4.0 * math.pi

LOBE_RADIUS_WORLD_CM = 60.0      # fixed 3-D glyph radius at full response
LOBE_DIRECTIONS_LAT = 13          # inclusive poles
LOBE_DIRECTIONS_LON = 25          # 24 azimuth cells
ROSETTE_BINS = 24
ROSETTE_RADIUS_PX = 55.0
ACTIVE_MARKER_UNIFORM = (0, 220, 255, 150)
ACTIVE_MARKER_ADAPTIVE = (255, 145, 0, 210)
FLOOR_MARKER = (40, 255, 120, 255)
ARCADE_MARKER = (255, 70, 210, 255)

COLOR_STOPS = [
    (0.0, (10, 25, 80)),
    (0.30, (0, 150, 210)),
    (0.60, (80, 235, 190)),
    (0.82, (255, 220, 70)),
    (1.0, (255, 255, 255)),
]

CHANNELS = {
    "PF_R32_UINT": 1, "PF_R32_FLOAT": 1, "PF_R8": 1, "PF_R8G8": 2,
    "PF_R16_UINT": 1, "PF_R16F": 1, "PF_FloatR11G11B10": 1,
    "PF_FloatRGB": 1, "PF_FloatRGBA": 4, "PF_A32B32G32R32F": 4,
    "PF_B8G8R8A8": 4, "PF_G8": 1, "PF_A2B10G10R10": 1,
}
DTYPES = {
    "PF_R32_UINT": "<u4", "PF_R32_FLOAT": "<f4", "PF_R8": "u1",
    "PF_R8G8": "u1", "PF_R16_UINT": "<u2", "PF_R16F": "<f2",
    "PF_FloatR11G11B10": "<u4", "PF_FloatRGB": "<u4",
    "PF_FloatRGBA": "<f2", "PF_A32B32G32R32F": "<f4",
    "PF_B8G8R8A8": "u1", "PF_G8": "u1", "PF_A2B10G10R10": "<u4",
}

_HASH = {}


def sha256_file(path: Path) -> str:
    path = Path(path)
    key = (str(path), path.stat().st_size, path.stat().st_mtime_ns)
    if key not in _HASH:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        _HASH[key] = h.hexdigest()
    return _HASH[key]


def load_concat_json(path: Path):
    s = Path(path).read_text(encoding="utf-8")
    dec = json.JSONDecoder()
    out, i, n = [], 0, len(s)
    while i < n:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        o, j = dec.raw_decode(s, i)
        out.append(o)
        i = j
    return out


def js(v):
    if isinstance(v, dict):
        return {str(k): js(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [js(x) for x in v]
    if isinstance(v, np.ndarray):
        return js(v.tolist())
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, (np.integer, int)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        x = float(v)
        return x if math.isfinite(x) else None
    return v


def f3(v):
    return [round(float(x), 6) for x in v]


def safe_median(a):
    a = np.asarray(a, dtype=np.float64)
    a = a[np.isfinite(a)]
    return float(np.median(a)) if a.size else None


def safe_max(a):
    a = np.asarray(a, dtype=np.float64)
    a = a[np.isfinite(a)]
    return float(np.max(a)) if a.size else None


def safe_p95(a):
    a = np.asarray(a, dtype=np.float64)
    a = a[np.isfinite(a)]
    return float(np.percentile(a, 95)) if a.size else None


# ---------------------------------------------------------------------------
# dump reader
# ---------------------------------------------------------------------------

class Dump:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.base = self.root / "Base"
        if not self.base.exists():
            raise FileNotFoundError(f"not a DumpGPU root (missing Base): {self.root}")
        self.passes = load_concat_json(self.base / "Passes.json")
        self.passes_by_id = {p["Id"]: p for p in self.passes}
        self.descs = load_concat_json(self.base / "ResourceDescs.json")
        self.desc_by_uid = {d["UniqueResourceName"]: d for d in self.descs}
        self.layouts = load_concat_json(self.base / "SubresourceLayouts.json")
        self.layouts_by_res = {}
        for r in self.layouts:
            self.layouts_by_res.setdefault(r["Resource"], []).append(r)
        self.readbacks = load_concat_json(self.base / "DumpReadbackStatus.json")
        self.readback_by_file = {r["File"].replace("\\", "/"): r for r in self.readbacks}
        self.refs_by_pass = {}
        for r in load_concat_json(self.base / "ReferencedStructs.json"):
            self.refs_by_pass[r.get("Pass")] = r
        self.param_bindings = load_concat_json(self.base / "ParameterBindings.json")
        self.native_bindings = load_concat_json(self.base / "NativeUniformBufferBindings.json")
        self.meta_cache = {}
        self._info = None

    # -- basic metadata ----------------------------------------------------
    def info(self):
        if self._info is None:
            self._info = json.loads((self.base / "Infos.json").read_text(encoding="utf-8"))
        return self._info

    def meta(self, meta_id):
        meta_id = str(meta_id)
        if meta_id not in self.meta_cache:
            f = self.root / "StructuresMetadata" / f"{meta_id}.json"
            self.meta_cache[meta_id] = json.loads(f.read_text(encoding="utf-8"))
        return self.meta_cache[meta_id]

    def struct_bytes(self, content_id):
        f = self.root / "Structures" / f"{content_id}.bin"
        if not f.exists():
            raise FileNotFoundError(f)
        return f.read_bytes()

    def find_final(self, prefix):
        hits = [p for p in self.passes if p.get("EventName", "").startswith(prefix)]
        if not hits:
            raise KeyError(f"no pass whose EventName starts with {prefix!r}")
        return max(hits, key=lambda p: p["Id"])

    def frame_scope(self):
        scopes = set()
        for p in self.passes:
            for s in p.get("ParentEventScopes", []):
                if s.startswith("Frame "):
                    scopes.add(s)
        return sorted(scopes)

    # -- layout / resource -------------------------------------------------
    def layout(self, uid, version, mip=0, slice_index=0):
        cand = []
        for r in self.layouts_by_res.get(uid, []):
            if version not in r["File"] or r.get("MipLevel", 0) != mip:
                continue
            if r.get("StencilSubresource"):
                continue
            if slice_index is not None and r.get("FirstArraySlice", 0) != slice_index:
                continue
            cand.append(r)
        if not cand:
            raise KeyError(f"no layout {uid} v{version} mip{mip}")
        return cand[0]

    def read_texture(self, uid, version, mip=0, slice_index=0):
        r = self.layout(uid, version, mip=mip, slice_index=slice_index)
        fmt = r["StoredFormat"]
        ch = CHANNELS.get(fmt)
        if ch is None:
            raise KeyError(f"unsupported stored format {fmt}")
        a = np.fromfile(self.root / r["File"], dtype=DTYPES[fmt])
        need = int(r["ExtentX"]) * int(r["ExtentY"]) * ch
        if a.size != need:
            raise ValueError(f"{r['File']}: {a.size} values, expected {need}")
        arr = a.reshape(int(r["ExtentY"]), int(r["ExtentX"]), ch) if ch > 1 else \
            a.reshape(int(r["ExtentY"]), int(r["ExtentX"]))
        return arr, r

    def read_buffer(self, uid, version):
        d = self.desc_by_uid.get(uid, {})
        path = self.root / "Resources" / f"{uid}.v{version}.bin"
        if not path.exists():
            cands = sorted((self.root / "Resources").glob(f"{uid}.v*.bin"))
            if not cands:
                raise FileNotFoundError(path)
            path = cands[-1]
        raw = path.read_bytes()
        be = int(d.get("BytesPerElement", 4))
        n = int(d.get("NumElements", len(raw) // max(be, 1)))
        if be == 4:
            a = np.frombuffer(raw, dtype="<u4")
        elif be == 2:
            a = np.frombuffer(raw, dtype="<u2")
        elif be == 1:
            a = np.frombuffer(raw, dtype="u1")
        else:
            a = raw
        return a, {
            "kind": "buffer",
            "resource": uid,
            "name": d.get("Name"),
            "desc": d.get("Desc"),
            "bytes_per_element": be,
            "num_elements": n,
            "file": str(path.relative_to(self.root)).replace("\\", "/"),
            "path": str(path),
            "sha256": sha256_file(path),
            "format": f"FRDGBufferDesc BytesPerElement={be} NumElements={n}",
        }

    def tex_record(self, role, uid, version, arr, layout, passrec):
        path = self.root / layout["File"]
        rb = self.readback_by_file.get(layout["File"].replace("\\", "/"))
        d = self.desc_by_uid.get(uid, {})
        return {
            "role": role,
            "kind": "texture",
            "resource": uid,
            "name": d.get("Name"),
            "version": version,
            "address": f"{uid}.v{version}",
            "producer_pass": {"id": passrec["Id"], "name": passrec["EventName"]},
            "file": layout["File"].replace("\\", "/"),
            "path": str(path),
            "sha256": sha256_file(path),
            "format": layout["StoredFormat"],
            "native_format": layout.get("NativeFormat"),
            "extent": [int(layout["ExtentX"]), int(layout["ExtentY"])],
            "row_pitch_bytes": layout.get("RowPitchBytes"),
            "byte_size": layout.get("ByteSize"),
            "readback_status": rb.get("Status") if rb else None,
            "readback_timing": rb.get("Timing") if rb else None,
        }

    def buffer_record(self, role, rec, passrec):
        rec = dict(rec)
        rec["role"] = role
        rec["address"] = f"{rec['resource']}.v{passrec['Pointer']}"
        rec["producer_pass"] = {"id": passrec["Id"], "name": passrec["EventName"]}
        return rec

    # -- structures --------------------------------------------------------
    def member(self, meta, name):
        for m in meta.get("Members", []):
            if m.get("Name") == name:
                return m
        raise KeyError(f"{meta.get('StructTypeName')}: no member {name!r}")

    def field(self, buf, meta, name):
        m = self.member(meta, name)
        base = m["Offset"]
        bt = m.get("BaseType")
        count = int(m.get("NumRows", 1)) * int(m.get("NumColumns", 1)) * max(1, int(m.get("NumElements", 0)))
        if bt == "UBMT_FLOAT32":
            return np.frombuffer(buf, dtype="<f4", count=count, offset=base).astype(np.float64)
        if bt == "UBMT_UINT32":
            return np.frombuffer(buf, dtype="<u4", count=count, offset=base).astype(np.uint32)
        if bt == "UBMT_INT32":
            return np.frombuffer(buf, dtype="<i4", count=count, offset=base).astype(np.int32)
        raise TypeError(f"{name}: unsupported field base type {bt}")

    def struct_record(self, role, content_id, meta_id, passrec, extra=None):
        f = self.root / "Structures" / f"{content_id}.bin"
        meta = self.meta(meta_id)
        rec = {
            "role": role,
            "kind": "struct",
            "content": content_id,
            "address": content_id,
            "metadata": meta_id,
            "struct_type": meta.get("StructTypeName"),
            "declared_size": meta.get("Size"),
            "file": f"Structures/{content_id}.bin",
            "path": str(f),
            "sha256": sha256_file(f) if f.exists() else None,
            "format": meta.get("StructTypeName"),
            "producer_pass": {"id": passrec["Id"], "name": passrec["EventName"]} if passrec else None,
        }
        if extra:
            rec.update(extra)
        return rec


# ---------------------------------------------------------------------------
# numeric helpers
# ---------------------------------------------------------------------------

def decode_r11g11b10(words):
    w = np.asarray(words, dtype=np.uint32)
    r, g, b = w & 0x7FF, (w >> 11) & 0x7FF, (w >> 22) & 0x3FF

    def dec(v, mant_bits):
        e = (v >> mant_bits) & 0x1F
        m = v & ((1 << mant_bits) - 1)
        scale = float(1 << mant_bits)
        return np.where(e == 0,
                        (m / scale) * (2.0 ** -14),
                        (1.0 + m / scale) * np.exp2(e.astype(np.int32) - 15)).astype(np.float64)

    return np.stack([dec(r, 6), dec(g, 6), dec(b, 5)], axis=-1)


def octahedron_to_unit_vector(o):
    o = np.asarray(o, dtype=np.float64) * 2.0 - 1.0
    u, v = o[..., 0].copy(), o[..., 1].copy()
    z = 1.0 - np.abs(u) - np.abs(v)
    neg = z < 0.0
    ux, uy = u[neg].copy(), v[neg].copy()
    u[neg] = (1.0 - np.abs(uy)) * np.sign(ux)
    v[neg] = (1.0 - np.abs(ux)) * np.sign(uy)
    n = np.stack([u, v, z], axis=-1)
    return n / np.maximum(np.linalg.norm(n, axis=-1, keepdims=True), 1e-30)


def sh_basis_function3(n):
    x, y, z = float(n[0]), float(n[1]), float(n[2])
    xx, yy, zz = x * x, y * y, z * z
    return np.array([0.282095,
                     -0.488603 * y, 0.488603 * z, -0.488603 * x,
                     1.092548 * x * y, -1.092548 * y * z,
                     0.315392 * (3.0 * zz - 1.0),
                     -1.092548 * x * z, 0.546274 * (xx - yy)], dtype=np.float64)


def calc_diffuse_transfer_sh3(n, exponent=1.0):
    b = sh_basis_function3(n)
    b[0] *= 2.0 * math.pi / (1.0 + exponent)
    b[1:4] *= 2.0 * math.pi / (2.0 + exponent)
    b[4:] *= exponent * 2.0 * math.pi / (3.0 + 4.0 * exponent + exponent * exponent)
    return b


def reverse_bits32(x):
    x = int(x) & 0xFFFFFFFF
    x = ((x & 0x55555555) << 1) | ((x & 0xAAAAAAAA) >> 1)
    x = ((x & 0x33333333) << 2) | ((x & 0xCCCCCCCC) >> 2)
    x = ((x & 0x0F0F0F0F) << 4) | ((x & 0xF0F0F0F0) >> 4)
    x = ((x & 0x00FF00FF) << 8) | ((x & 0xFF00FF00) >> 8)
    return ((x << 16) | (x >> 16)) & 0xFFFFFFFF


def screen_tile_jitter(temporal_index):
    """uint2(Hammersley16(TemporalIndex, 8, 0) * ScreenProbeDownsampleFactor)."""
    idx = int(temporal_index)
    jx = int((idx / 8.0) % 1.0 * 16.0)
    jy = int(((reverse_bits32(idx) >> 16) / 65536.0) * 16.0)
    return jx, jy


def equi_area_directions(res=8):
    """The 8x8 layout used by ScreenProbeConvertToIrradiance."""
    out = []
    for y in range(res):
        for x in range(res):
            uv = ((x + 0.5) / res, (y + 0.5) / res)
            u, v = 2.0 * uv[0] - 1.0, 2.0 * uv[1] - 1.0
            d = 1.0 - (abs(u) + abs(v))
            r = 1.0 - abs(d)
            phi = 0.0 if r == 0 else (math.pi / 4.0) * ((abs(v) - abs(u)) / r + 1.0)
            f = r * math.sqrt(max(2.0 - r * r, 0.0))
            out.append([f * math.copysign(abs(math.cos(phi)), u),
                        f * math.copysign(abs(math.sin(phi)), v),
                        math.copysign(1.0 - r * r, d)])
    a = np.asarray(out, dtype=np.float64)
    return a / np.maximum(np.linalg.norm(a, axis=-1, keepdims=True), 1e-30)


def lobe_directions(lat=LOBE_DIRECTIONS_LAT, lon=LOBE_DIRECTIONS_LON):
    """Spherical grid directions; poles are single vertices per ring."""
    dirs, mesh = [], []
    for i in range(lat):
        theta = math.pi * i / (lat - 1)
        row = []
        for j in range(lon - 1):
            phi = 2.0 * math.pi * j / (lon - 1)
            d = np.array([math.sin(theta) * math.cos(phi),
                          math.sin(theta) * math.sin(phi),
                          math.cos(theta)])
            row.append(len(dirs))
            dirs.append(d)
        mesh.append(row)
    return np.asarray(dirs, dtype=np.float64), mesh


def fibonacci_directions(n=162):
    out = []
    ga = math.pi * (3.0 - math.sqrt(5.0))
    for i in range(n):
        z = 1.0 - 2.0 * (i + 0.5) / n
        r = math.sqrt(max(0.0, 1.0 - z * z))
        a = ga * i
        out.append([r * math.cos(a), r * math.sin(a), z])
    return np.asarray(out, dtype=np.float64)


# ---------------------------------------------------------------------------
# state decoding
# ---------------------------------------------------------------------------

def _param_block(dump, passrec):
    meta = dump.meta(passrec["ParametersMetadata"])
    m = dump.member(meta, "ScreenProbeParameters")
    buf = dump.struct_bytes(passrec["Parameters"])
    return buf, m["Offset"], m["StructMetadata"], meta


def _view_block(dump, passrec):
    rec = dump.refs_by_pass.get(passrec["Pointer"])
    if rec is None:
        rec = dump.refs_by_pass.get(passrec.get("Parameters"))
    if rec is None or "View" not in rec.get("ReferencedStructs", {}):
        raise KeyError(f"no captured View for pass {passrec['Id']}")
    v = rec["ReferencedStructs"]["View"]
    if v.get("Null") or not v.get("ContentAvailable"):
        raise KeyError("View is null/unavailable")
    meta = dump.meta(v["Metadata"])
    buf = dump.struct_bytes(v["Content"])
    return buf, meta, v


def decode_view(dump, passrec):
    buf, meta, v = _view_block(dump, passrec)
    fl = lambda name: dump.field(buf, meta, name)
    M = fl("TranslatedWorldToClip").reshape(4, 4)
    Mvw = fl("TranslatedWorldToView").reshape(4, 4)
    Mvc = fl("ViewToClip").reshape(4, 4)
    vrmin = fl("ViewRectMin")[:2]
    vrsize = fl("ViewSizeAndInvSize")[:2]
    pre = fl("PreViewTranslationHigh")[:3] + fl("PreViewTranslationLow")[:3]
    pre_hi = fl("PreViewTranslationHigh")[:3]
    pre_lo = fl("PreViewTranslationLow")[:3]
    fwd = fl("ViewForward")[:3]
    cam_t = fl("TranslatedWorldCameraOrigin")[:3]

    state_frame_index = int(fl("StateFrameIndex")[0])
    state_mod8 = int(fl("StateFrameIndexMod8")[0])
    frame_number = int(fl("FrameNumber")[0])
    frame_counter = int(fl("FrameCounter")[0])
    preexp = float(fl("PreExposure")[0])
    inv_preexp = float(fl("OneOverPreExposure")[0])
    near = float(fl("NearPlane")[0])
    taa_jitter = fl("TemporalAAJitter")[:2]
    rect_min_i = fl("ViewRectMinAndSize") if False else None
    view_rec = dump.struct_record("view", v["Content"], v["Metadata"], passrec)
    view_rec["fields_used"] = [
        "TranslatedWorldToClip", "TranslatedWorldToView", "ViewToClip",
        "ViewRectMin", "ViewSizeAndInvSize", "BufferSizeAndInvSize",
        "PreViewTranslationHigh", "PreViewTranslationLow", "ViewForward",
        "TranslatedWorldCameraOrigin", "NearPlane", "PreExposure", "OneOverPreExposure",
        "FrameNumber", "FrameCounter", "StateFrameIndex", "StateFrameIndexMod8",
        "TemporalAAJitter",
    ]
    return {
        "M_translated_world_to_clip": M,
        "M_translated_world_to_view": Mvw,
        "M_view_to_clip": Mvc,
        "view_rect_min": vrmin,
        "view_size": vrsize,
        "buffer_size": fl("BufferSizeAndInvSize")[:2],
        "pre_view_translation_cm": pre,
        "pre_view_translation_high_cm": pre_hi,
        "pre_view_translation_low_cm": pre_lo,
        "view_forward": fwd,
        "translated_world_camera_origin_cm": cam_t,
        "world_camera_origin_cm": cam_t - pre,
        "near_plane_cm": near,
        "preexposure": preexp,
        "one_over_preexposure": inv_preexp,
        "preexposure_reciprocal_ok": bool(preexp and abs(preexp * inv_preexp - 1.0) < 1e-6),
        "frame_number": frame_number,
        "frame_counter": frame_counter,
        "state_frame_index": state_frame_index,
        "state_frame_index_mod8": state_mod8,
        "temporal_aa_jitter": taa_jitter,
        "matrix_convention": "row-vector v @ reshape(4,4) of the captured float block; "
                             "validated against the captured uniform-probe screen centers",
        "view_array": {
            "content": v["Content"],
            "metadata": v["Metadata"],
            "struct_type": meta.get("StructTypeName"),
            "byte_size": meta.get("Size"),
        },
        "record": view_rec,
    }


def project(view, p):
    v = np.array([float(p[0]), float(p[1]), float(p[2]), 1.0])
    clip = v @ view["M_translated_world_to_clip"]
    w = float(clip[3])
    if abs(w) < 1e-12:
        return None
    ndc = clip[:2] / w
    vr, vs = view["view_rect_min"], view["view_size"]
    return (float(vr[0] + (ndc[0] * 0.5 + 0.5) * vs[0]),
            float(vr[1] + (0.5 - ndc[1] * 0.5) * vs[1]), w)


def decode_screen_probes(dump):
    spawn = dump.find_final("AdaptivePlacementSpawn")
    shp = dump.find_final("ScreenProbeConvertToIrradiance")
    pbuf, nested_off, nested_meta_id, pmeta = _param_block(dump, spawn)
    nmeta = dump.meta(nested_meta_id)
    nested_bytes = pbuf[int(nested_off):int(nested_off) + int(nmeta.get("Size", 0))]
    sh_pbuf, sh_nested_off, sh_nested_meta_id, sh_pmeta = _param_block(dump, shp)
    sh_nmeta = dump.meta(sh_nested_meta_id)
    sh_nested_bytes = sh_pbuf[int(sh_nested_off):int(sh_nested_off) + int(sh_nmeta.get("Size", 0))]
    geti = lambda n: int(dump.field(nested_bytes, nmeta, n)[0])
    get2 = lambda n: [int(x) for x in dump.field(nested_bytes, nmeta, n)[:2]]

    params = {
        "NumUniformScreenProbes": geti("NumUniformScreenProbes"),
        "MaxNumAdaptiveProbes": geti("MaxNumAdaptiveProbes"),
        "ScreenProbeAtlasViewSize": get2("ScreenProbeAtlasViewSize"),
        "ScreenProbeAtlasBufferSize": get2("ScreenProbeAtlasBufferSize"),
        "ScreenProbeViewSize": get2("ScreenProbeViewSize"),
        "ScreenProbeDownsampleFactor": geti("ScreenProbeDownsampleFactor"),
        "ScreenProbeGatherOctahedronResolution": geti("ScreenProbeGatherOctahedronResolution"),
        "ScreenProbeTracingOctahedronResolution": geti("ScreenProbeTracingOctahedronResolution"),
        "FixedJitterIndex": int(dump.field(nested_bytes, nmeta, "FixedJitterIndex")[0]),
        "ScreenProbeRayDirectionFrameIndex": geti("ScreenProbeRayDirectionFrameIndex"),
        "nested_offset_in_FParameters": int(nested_off),
        "nested_struct": nmeta.get("StructTypeName"),
    }

    # resolve pass outputs by the recorded resource name, never by an id
    roles = {
        "ScreenProbeTranslatedWorldPosition": "position",
        "ScreenProbeSceneDepth": "depth",
        "ScreenProbeWorldNormal": "normal",
        "ScreenProbeWorldSpeed": "speed",
        "NumAdaptiveScreenProbes": "num_adaptive",
        "AdaptiveScreenProbeData": "adaptive_data",
        "ScreenTileAdaptiveProbeHeader": "adaptive_header",
        "ScreenTileAdaptiveProbeIndices": "adaptive_indices",
    }
    got = {}
    for out in spawn.get("OutputResources", []):
        uid = out.split(".")[0]
        name = (dump.desc_by_uid.get(uid) or {}).get("Name", "")
        role = next((r for k, r in roles.items() if name.endswith(k)), None)
        if role:
            got[role] = uid
    need = {"position", "depth", "normal", "num_adaptive", "adaptive_data", "adaptive_header"}
    if not need.issubset(got):
        raise KeyError(f"spawn pass {spawn['Id']} missing outputs: {need - set(got)}")

    def tex(role):
        uid = got[role]
        arr, lay = dump.read_texture(uid, spawn["Pointer"])
        return arr, dump.tex_record(role, uid, spawn["Pointer"], arr, lay, spawn)

    pos, pos_rec = tex("position")
    dep_words, dep_rec = tex("depth")
    nor, nor_rec = tex("normal")
    header, hdr_rec = tex("adaptive_header")
    speed, speed_rec = tex("speed") if "speed" in got else (None, None)

    count_arr, count_rec = dump.read_buffer(got["num_adaptive"], spawn["Pointer"])
    count_raw = int(count_arr[0]) if count_arr.size else 0
    adata_arr, adata_rec = dump.read_buffer(got["adaptive_data"], spawn["Pointer"])

    sh_roles = {"ScreenProbeRadianceSHAmbient": "sh_ambient",
                "ScreenProbeRadianceSHDirectional": "sh_directional"}
    sh_got = {}
    for out in shp.get("OutputResources", []):
        uid = out.split(".")[0]
        name = (dump.desc_by_uid.get(uid) or {}).get("Name", "")
        role = next((r for k, r in sh_roles.items() if name.endswith(k)), None)
        if role:
            sh_got[role] = uid
    if set(sh_got) != {"sh_ambient", "sh_directional"}:
        raise KeyError(f"SH pass {shp['Id']} outputs not resolved: {sh_got}")
    amb_words, amb_lay = dump.read_texture(sh_got["sh_ambient"], shp["Pointer"])
    amb = decode_r11g11b10(amb_words)
    dir_tex, dir_lay = dump.read_texture(sh_got["sh_directional"], shp["Pointer"])
    amb_rec = dump.tex_record("sh_ambient", sh_got["sh_ambient"], shp["Pointer"], amb_words, amb_lay, shp)
    dir_rec = dump.tex_record("sh_directional", sh_got["sh_directional"], shp["Pointer"], dir_tex, dir_lay, shp)

    if dep_words.dtype != np.uint32:
        raise TypeError("ScreenProbeSceneDepth is not stored as uint32 words")
    depth = dep_words.view(np.float32)
    H, W = depth.shape
    if pos.shape[:2] != (H, W) or nor.shape[:2] != (H, W):
        raise ValueError("placement texture extents disagree")
    if amb.shape[:2] != (H, W):
        raise ValueError("SH ambient extent disagrees with placement")
    if dir_tex.shape[1] != 6 * W:
        raise ValueError("directional SH width is not 6 * atlas width")

    view = decode_view(dump, shp)
    view_rec = view.pop("record")
    jx, jy = screen_tile_jitter(
        params["FixedJitterIndex"] if params["FixedJitterIndex"] >= 0
        else view["state_frame_index_mod8"])

    num_uniform = int(params["NumUniformScreenProbes"])
    max_adapt = int(params["MaxNumAdaptiveProbes"])
    view_capacity = int(params["ScreenProbeAtlasViewSize"][0]) * int(params["ScreenProbeAtlasViewSize"][1])
    storage_capacity = int(params["ScreenProbeAtlasBufferSize"][0]) * int(params["ScreenProbeAtlasBufferSize"][1])
    active_bound = max(0, min(count_raw, max_adapt, max(0, view_capacity - num_uniform)))

    ys, xs = np.mgrid[0:H, 0:W]
    linear = (ys * W + xs).reshape(-1)
    depf = depth.reshape(-1)
    active_mask = (depf > 0) & ((linear < num_uniform) |
                                ((linear >= num_uniform) & ((linear - num_uniform) < active_bound)))
    unlit_mask = (depf < 0) & (linear < num_uniform)
    stale_mask = (depf != 0) & (linear >= num_uniform) & ((linear - num_uniform) >= active_bound)

    def sh_at(ax, ay):
        sh = np.zeros((3, 9), dtype=np.float64)
        for c in range(3):
            sh[c, 0] = amb[ay, ax, c]
            sh[c, 1:5] = dir_tex[ay, ax + (2 * c) * W, :]
            sh[c, 5:9] = dir_tex[ay, ax + (2 * c + 1) * W, :]
        return sh

    normal_uv = nor.astype(np.float64) / 255.0
    normal = octahedron_to_unit_vector(normal_uv)

    active = []
    inactive = []
    stale = []
    for k in np.nonzero(active_mask)[0]:
        ax, ay = int(xs.reshape(-1)[k]), int(ys.reshape(-1)[k])
        p = pos[ay, ax, :3].astype(np.float64)
        if not np.all(np.isfinite(p)):
            continue
        world = p - view["pre_view_translation_cm"]
        sh = sh_at(ax, ay)
        pr = project(view, p)
        if pr is None:
            continue
        is_uniform = ax + ay * W < num_uniform
        sh_finite = bool(np.all(np.isfinite(sh)))
        nrm = normal[ay, ax]
        d_actual = 4.0 * (sh @ calc_diffuse_transfer_sh3(nrm)) if sh_finite else np.zeros(3)
        preexp = float(view["preexposure"]) if view["preexposure"] else float("nan")
        irr_pre = math.pi * d_actual
        irr_unpre = (irr_pre / preexp) if preexp else np.full(3, float("nan"))
        active.append({
            "atlas": [ax, ay],
            "linear": int(ax + ay * W),
            "kind": "uniform" if is_uniform else "adaptive",
            "uniform_index": int(ax + ay * W) if is_uniform else None,
            "adaptive_index": None if is_uniform else int(ax + ay * W - num_uniform),
            "status": "active",
            "depth_cm": float(depf[ay * W + ax]),
            "translated_world_cm": f3(p),
            "world_cm": f3(world),
            "normal": f3(normal[ay, ax]),
            "screen_xy": f3(pr[:2]),
            "screen_depth_cm": round(pr[2], 6),
            "sh": f3(sh.reshape(-1)),
            "sh_finite": sh_finite,
            "sh_min": round(float(np.min(sh)), 8) if sh_finite else None,
            "sh_max": round(float(np.max(sh)), 8) if sh_finite else None,
            "d_at_captured_normal_signed_rgb": f3(d_actual),
            "irradiance_preexposed_rgb": f3(irr_pre),
            "irradiance_unpreexposed_rgb": f3(irr_unpre),
            "response_units": "D is 4*EvaluateSHIrradiance, view-pre-exposed; "
                              "irradiance_preexposed=pi*D; irradiance_unpreexposed=pi*D/PreExposure",
        })
    for k in np.nonzero(unlit_mask | stale_mask)[0]:
        ax, ay = int(xs.reshape(-1)[k]), int(ys.reshape(-1)[k])
        p = pos[ay, ax, :3].astype(np.float64)
        rec = {
            "atlas": [ax, ay],
            "linear": int(ax + ay * W),
            "kind": "uniform" if ax + ay * W < num_uniform else "adaptive",
            "adaptive_index": None if ax + ay * W < num_uniform else int(ax + ay * W - num_uniform),
            "status": "inactive_unlit" if bool(unlit_mask.reshape(-1)[k]) else "stale",
            "depth_cm": float(depf[ay * W + ax]),
            "translated_world_cm": f3(p) if np.all(np.isfinite(p)) else None,
            "world_cm": f3(p - view["pre_view_translation_cm"]) if np.all(np.isfinite(p)) else None,
        }
        (inactive if rec["status"] == "inactive_unlit" else stale).append(rec)

    # screen-center validation: uniform probes use the shader's tile formula;
    # adaptive probes use the encoded AdaptiveScreenProbeData word.
    vrmin, vsize = view["view_rect_min"], view["view_size"]
    ux, uy = [], []
    axx = []
    for rec in active:
        if rec["kind"] == "uniform":
            ax, ay = rec["atlas"]
            ex = int(min(vrmin[0] + ax * params["ScreenProbeDownsampleFactor"] + jx,
                         vrmin[0] + vsize[0] - 1)) + 0.5
            ey = int(min(vrmin[1] + ay * params["ScreenProbeDownsampleFactor"] + jy,
                         vrmin[1] + vsize[1] - 1)) + 0.5
            ux.append(rec["screen_xy"][0] - ex)
            uy.append(rec["screen_xy"][1] - ey)
        else:
            i = rec["adaptive_index"]
            if i is not None and i < len(adata_arr):
                word = int(adata_arr[i])
                ex = float(word & 0xFFFF) + 0.5
                ey = float((word >> 16) & 0xFFFF) + 0.5
                axx.append([rec["screen_xy"][0] - ex, rec["screen_xy"][1] - ey])

    def err(a):
        a = np.asarray(a, dtype=np.float64)
        return {"n": int(a.size), "median_px": safe_median(a), "p95_px": safe_p95(a),
                "max_px": safe_max(np.abs(a)) if a.size else None}

    e_uniform = err(np.concatenate([np.abs(ux), np.abs(uy)])) if ux else {"n": 0}
    e_adapt = err(np.asarray(axx).reshape(-1)) if axx else {"n": 0}
    hdr = header.astype(np.uint32)
    counts = {
        "num_uniform_screen_probes": num_uniform,
        "max_adaptive_probes": max_adapt,
        "atlas_view_size": params["ScreenProbeAtlasViewSize"],
        "atlas_buffer_size": params["ScreenProbeAtlasBufferSize"],
        "atlas_view_capacity": view_capacity,
        "atlas_storage_capacity": storage_capacity,
        "captured_num_adaptive_screen_probes": count_raw,
        "active_adaptive_bound": active_bound,
        "active_uniform_count": int(sum(1 for r in active if r["kind"] == "uniform")),
        "active_adaptive_count": int(sum(1 for r in active if r["kind"] == "adaptive")),
        "active_total_count": len(active),
        "inactive_unlit_uniform_count": len(inactive),
        "stale_finite_adaptive_count": len(stale),
        "finite_nonzero_storage_count": int(np.count_nonzero(depf != 0)),
        "adaptive_header_nonzero_tiles": int(np.count_nonzero(hdr)),
        "adaptive_header_value_sum": int(hdr.sum()),
        "count_matches_header_sum": bool(int(hdr.sum()) == count_raw),
        "occupied_header_tiles_not_greater_than_allocated": bool(int(np.count_nonzero(hdr)) <= count_raw),
        "occupied_header_tile_count": int(np.count_nonzero(hdr)),
        "active_bound_within_max": bool(active_bound <= max_adapt),
        "active_bound_within_atlas": bool(num_uniform + active_bound <= view_capacity),
        "uniform_plus_adaptive_fits_atlas_view": bool(num_uniform + active_bound <= view_capacity),
        "adaptive_bound_fits_storage": bool(num_uniform + active_bound <= storage_capacity),
        "uniform_equals_atlas_view_product": bool(num_uniform == view_capacity),
        "uniform_equals_atlas_view_product_expected": False,
        "uniform_equals_atlas_view_product_reason": (
            "the view atlas is 80x67 because it reserves rows for adaptive probes; the uniform "
            "grid is only the 80x45 screen-tile grid, so equality is expected to be false"),
        "screen_temporal_index": int(params["FixedJitterIndex"] if params["FixedJitterIndex"] >= 0
                                     else view["state_frame_index_mod8"]),
        "screen_tile_jitter_px": [int(jx), int(jy)],
    }

    records = {
        "position": pos_rec, "depth": dep_rec, "normal": nor_rec,
        "sh_ambient": amb_rec, "sh_directional": dir_rec,
        "adaptive_header": hdr_rec,
        "num_adaptive": dump.buffer_record("num_adaptive", count_rec, spawn),
        "adaptive_data": dump.buffer_record("adaptive_data", adata_rec, spawn),
        "view": view_rec,
        "spawn_parameters": dump.struct_record(
            "spawn_parameters", spawn["Parameters"], spawn["ParametersMetadata"], spawn,
            {"nested_struct": nmeta.get("StructTypeName"),
             "nested_offset_in_FParameters": int(nested_off)}),
        "sh_parameters": dump.struct_record(
            "sh_parameters", shp["Parameters"], shp["ParametersMetadata"], shp,
            {"nested_struct": sh_nmeta.get("StructTypeName"),
             "nested_offset_in_FParameters": int(sh_nested_off)}),
    }
    if speed_rec:
        records["speed"] = speed_rec

    # exact ParameterBindings cross-check of the nested struct offsets
    param_bindings = {}
    for rec in dump.param_bindings:
        if rec.get("Parameters") == spawn["Parameters"]:
            for name in ("ScreenProbeParameters.NumAdaptiveScreenProbes",
                         "ScreenProbeParameters.ScreenProbeTranslatedWorldPosition",
                         "ScreenProbeParameters.ScreenProbeSceneDepth"):
                b = rec.get("Bindings", {}).get(name)
                if b:
                    param_bindings[name] = int(b.get("Offset", -1))
    field_offsets = {
        "ScreenProbeParameters.NumAdaptiveScreenProbes":
            int(dump.member(nmeta, "NumAdaptiveScreenProbes")["Offset"]),
        "ScreenProbeParameters.ScreenProbeTranslatedWorldPosition":
            int(dump.member(nmeta, "ScreenProbeTranslatedWorldPosition")["Offset"]),
        "ScreenProbeParameters.ScreenProbeSceneDepth":
            int(dump.member(nmeta, "ScreenProbeSceneDepth")["Offset"]),
    }
    binding_ok = bool(param_bindings) and all(
        param_bindings.get(k) == int(nested_off) + v for k, v in field_offsets.items())
    binding_validation = {
        "parameters_id": spawn["Parameters"],
        "bindings": param_bindings,
        "metadata_field_offsets": field_offsets,
        "nested_offset_in_FParameters": int(nested_off),
        "ok": binding_ok,
    }
    sh_counts_ok = all(
        int(dump.field(sh_nested_bytes, sh_nmeta, n)[0]) == int(dump.field(nested_bytes, nmeta, n)[0])
        for n in ("NumUniformScreenProbes", "MaxNumAdaptiveProbes",
                  "ScreenProbeGatherOctahedronResolution", "FixedJitterIndex"))

    def fmt_ok(rec, fmt, extent=None, be=None, elements=None):
        ok = rec.get("format") == fmt
        if extent is not None:
            ok = ok and rec.get("extent") == list(extent)
        if be is not None:
            ok = ok and int(rec.get("bytes_per_element", -1)) == int(be)
        if elements is not None:
            ok = ok and int(rec.get("num_elements", -1)) == int(elements)
        return bool(ok)

    field_formats = {
        "position": {"format": pos_rec.get("format"), "extent": pos_rec.get("extent"),
                     "ok": fmt_ok(pos_rec, "PF_A32B32G32R32F", [W, H])},
        "depth": {"format": dep_rec.get("format"), "extent": dep_rec.get("extent"),
                  "ok": fmt_ok(dep_rec, "PF_R32_UINT", [W, H])},
        "normal": {"format": nor_rec.get("format"), "extent": nor_rec.get("extent"),
                   "ok": fmt_ok(nor_rec, "PF_R8G8", [W, H])},
        "sh_ambient": {"format": amb_rec.get("format"), "extent": amb_rec.get("extent"),
                       "ok": fmt_ok(amb_rec, "PF_FloatR11G11B10", [W, H])},
        "sh_directional": {"format": dir_rec.get("format"), "extent": dir_rec.get("extent"),
                           "ok": fmt_ok(dir_rec, "PF_FloatRGBA", [6 * W, H])},
        "adaptive_header": {"format": hdr_rec.get("format"), "extent": hdr_rec.get("extent"),
                            "ok": fmt_ok(hdr_rec, "PF_R32_UINT", [80, 56])},
        "num_adaptive": {"bytes_per_element": records["num_adaptive"].get("bytes_per_element"),
                         "num_elements": records["num_adaptive"].get("num_elements"),
                         "ok": fmt_ok(records["num_adaptive"], records["num_adaptive"].get("format"), be=4, elements=1)},
        "adaptive_data": {"bytes_per_element": records["adaptive_data"].get("bytes_per_element"),
                          "num_elements": records["adaptive_data"].get("num_elements"),
                          "ok": fmt_ok(records["adaptive_data"], records["adaptive_data"].get("format"), be=4)},
    }

    return {
        "spawn_pass": {"id": spawn["Id"], "name": spawn["EventName"], "pointer": spawn["Pointer"]},
        "sh_pass": {"id": shp["Id"], "name": shp["EventName"], "pointer": shp["Pointer"]},
        "params": params,
        "view": view,
        "counts": counts,
        "active": active,
        "inactive": inactive,
        "stale": stale,
        "records": records,
        "validation": {
            "uniform_screen_center_error": e_uniform,
            "adaptive_screen_center_error": e_adapt,
            "field_formats": field_formats,
            "parameter_bindings": binding_validation,
            "sh_parameter_counts_match_spawn": bool(sh_counts_ok),
            "sh_layout": {
                "ambient_format": amb_lay["StoredFormat"],
                "directional_format": dir_lay["StoredFormat"],
                "ambient_extent": [int(amb_lay["ExtentX"]), int(amb_lay["ExtentY"])],
                "directional_extent": [int(dir_lay["ExtentX"]), int(dir_lay["ExtentY"])],
                "directional_width_equals_6x_atlas": bool(dir_tex.shape[1] == 6 * W),
                "channel_texel_order": "for channel c: coeffs 1..4 at x=ax+2c*W, coeffs 5..8 at x=ax+(2c+1)*W",
            },
            "sh_finite_active_count": int(sum(1 for r in active if r["sh_finite"])),
            "sh_nonfinite_active_count": int(sum(1 for r in active if not r["sh_finite"])),
            "active_sh_min": min((r["sh_min"] for r in active if r["sh_min"] is not None), default=None),
            "active_sh_max": max((r["sh_max"] for r in active if r["sh_max"] is not None), default=None),
        },
    }


# ---------------------------------------------------------------------------
# constant-radiance and lobe math
# ---------------------------------------------------------------------------

def constant_radiance_validation():
    # 1) exact unit-normalization of the declared basis
    sh_exact = np.zeros(9)
    sh_exact[0] = 0.282095  # SHAmbientFunction / Y00
    normals = fibonacci_directions(256)
    D_exact = np.array([4.0 * float(np.dot(calc_diffuse_transfer_sh3(n), sh_exact)) for n in normals])
    # 2) the captured 8x8 equi-area quadrature used by the shader
    dirs = equi_area_directions(8)
    sh_discrete = np.zeros(9)
    for d in dirs:
        sh_discrete += sh_basis_function3(d)
    sh_discrete /= len(dirs)
    D_disc = np.array([4.0 * float(np.dot(calc_diffuse_transfer_sh3(n), sh_discrete)) for n in normals])
    return {
        "radiance": 1.0,
        "projected_l0": float(sh_discrete[0]),
        "expected_l0": 0.282095,
        "l0_abs_error": abs(float(sh_discrete[0]) - 0.282095),
        "max_other_coefficient": float(np.max(np.abs(sh_discrete[1:]))),
        "exact_sh_max_abs_D_minus_radiance": float(np.max(np.abs(D_exact - 1.0))),
        "discrete_8x8_max_abs_D_minus_radiance": float(np.max(np.abs(D_disc - 1.0))),
        "discrete_8x8_mean_D": float(np.mean(D_disc)),
        "directions_used_for_projection": len(dirs),
        "directions_used_for_evaluation": len(normals),
        "normalization_ok": bool(abs(float(sh_discrete[0]) - 0.282095) < 1e-9 and
                                 np.max(np.abs(D_exact - 1.0)) < 1e-4),
        "note": "sphere-average radiance SH; D=4*EvaluateSHIrradiance; the exact convention maps "
                "constant radiance to itself.  The captured 8x8 equi-area quadrature leaves a "
                "few-percent residual in the higher bands; that is reported separately and is not "
                "treated as a decoder error.",
    }


def evaluate_lobes(sh, dirs):
    transfer = np.array([calc_diffuse_transfer_sh3(n) for n in dirs])
    signed = 4.0 * (sh @ transfer.T)          # (3, ndirs)
    clamped = np.maximum(signed, 0.0)
    lum_signed = 0.2126 * signed[0] + 0.7152 * signed[1] + 0.0722 * signed[2]
    lum_clamped = np.maximum(lum_signed, 0.0)
    return signed.T, clamped.T, lum_signed, lum_clamped


# ---------------------------------------------------------------------------
# focused-probe selection and matching
# ---------------------------------------------------------------------------

def choose_focused(active, count=10):
    floor, arc = [], []
    for r in active:
        if not r["sh_finite"]:
            continue
        x, y = r["screen_xy"]
        z = r["normal"][2]
        wz = r["world_cm"][2]
        if z > 0.95 and 520 <= y <= 690 and -5.0 <= wz <= 5.0 and 430 <= x <= 850:
            floor.append(r)
        elif ((80 < x < 360) or (920 < x < 1200)) and 260 <= y <= 620 and abs(z) < 0.55:
            arc.append(r)
    floor.sort(key=lambda r: r["screen_xy"][0])
    left = sorted([r for r in arc if r["screen_xy"][0] < 390], key=lambda r: r["screen_xy"][1])
    right = sorted([r for r in arc if r["screen_xy"][0] > 890], key=lambda r: r["screen_xy"][1])
    picks = []

    def spread(cands, n):
        if not cands:
            return []
        if len(cands) <= n:
            return list(cands)
        idx = np.linspace(0, len(cands) - 1, n).round().astype(int)
        return [cands[i] for i in sorted(set(idx.tolist()))]

    picks += [("floor", r) for r in spread(floor, max(0, count - 5))]
    picks += [("arcade-left", r) for r in spread(left, 3)]
    picks += [("arcade-right", r) for r in spread(right, 2)]
    # top up from arcade/floor if a region was sparse
    if len(picks) < count:
        for r in spread(arc, count - len(picks)):
            picks.append(("arcade", r))
    if len(picks) < count:
        for r in spread(floor, count - len(picks)):
            picks.append(("floor", r))
    out, seen = [], set()
    for region, r in picks:
        key = tuple(r["atlas"])
        if key in seen:
            continue
        seen.add(key)
        out.append((region, r))
        if len(out) == count:
            break
    return out


def match_probes(ref, active):
    """Nearest active probe by world-space distance (not atlas identity)."""
    if not active:
        return None
    P = np.array([r["world_cm"] for r in active], dtype=np.float64)
    d = np.linalg.norm(P - np.asarray(ref["world_cm"], dtype=np.float64), axis=1)
    i = int(np.argmin(d))
    return active[i], float(d[i])


# ---------------------------------------------------------------------------
# figure helpers
# ---------------------------------------------------------------------------

def color_for(t):
    t = min(1.0, max(0.0, float(t)))
    for i in range(len(COLOR_STOPS) - 1):
        a, ca = COLOR_STOPS[i]
        b, cb = COLOR_STOPS[i + 1]
        if t <= b:
            f = (t - a) / max(b - a, 1e-12)
            return tuple(int(ca[k] + (cb[k] - ca[k]) * f) for k in range(3))
    return COLOR_STOPS[-1][1]


def open_screenshot(state_json):
    p = state_json["screenshot"]["path"]
    return Image.open(p).convert("RGBA")


def scene_depth_array(state_json):
    rec = state_json.get("scene_depth")
    if not rec:
        return None
    f = Path(rec["path"])
    if not f.exists():
        return None
    lay = rec
    a = np.fromfile(f, dtype="<f4")
    h, w = int(lay["extent"][1]), int(lay["extent"][0])
    if a.size != h * w:
        return None
    return a.reshape(h, w)


def scene_cm(scene, x, y, near_cm):
    if scene is None:
        return None, False
    h, w = scene.shape
    ix, iy = int(round(x)), int(round(y))
    if ix < 0 or iy < 0 or ix >= w or iy >= h:
        return None, False
    d = float(scene[iy, ix])
    if not math.isfinite(d) or d <= 1e-8:
        return None, False
    return float(near_cm) / d, True


def draw_lobe(draw, view, probe_world, sh, dirs, mesh, lobe_max, scene, depth_test=True):
    """Project a real 3-D diffuse-response lobe into the captured screenshot."""
    lat, lon = len(mesh), len(mesh[0])
    resp = np.zeros((lat, lon))
    for i in range(lat):
        for j in range(lon):
            d = dirs[mesh[i][j]]
            transfer = calc_diffuse_transfer_sh3(d)
            rgb = 4.0 * (np.asarray(sh, dtype=np.float64) @ transfer)
            lum = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
            resp[i, j] = max(0.0, lum)
    verts, vis = [], []
    for i in range(lat):
        rowv, rowvis = [], []
        for j in range(lon):
            d = dirs[mesh[i][j]]
            t = min(1.0, resp[i, j] / max(lobe_max, 1e-9))
            world = np.asarray(probe_world) + d * (LOBE_RADIUS_WORLD_CM * t)
            pr = project(view, world)
            if pr is None or pr[2] <= 0:
                rowv.append(None)
                rowvis.append(False)
                continue
            x, y, w = pr
            v = 0 <= x < 100000 and 0 <= y < 100000
            if depth_test:
                scm, ok = scene_cm(scene, x, y, view["near_plane_cm"])
                if ok:
                    v = v and (w <= scm + max(2.0, 0.02 * w))
            rowv.append((x, y, w, t))
            rowvis.append(v)
        verts.append(rowv)
        vis.append(rowvis)
    quads = []
    hidden_quads = []
    for i in range(lat - 1):
        for j in range(lon - 1):
            ids = [(i, j), (i, j + 1), (i + 1, j + 1), (i + 1, j)]
            pts = [verts[a][b] for a, b in ids]
            if any(p is None for p in pts):
                continue
            wmean = sum(p[2] for p in pts) / 4.0
            tmean = sum(p[3] for p in pts) / 4.0
            if all(vis[a][b] for a, b in ids):
                quads.append((wmean, pts, tmean))
            elif tmean > 0.02:
                hidden_quads.append((wmean, pts, tmean))
    quads.sort(key=lambda q: -q[0])
    for _, pts, t in quads:
        rgb = color_for(t)
        draw.polygon([(p[0], p[1]) for p in pts], fill=rgb + (110,))
    # Depth-occluded parts are drawn as an explicit x-ray function glyph, never
    # as a physical surface.  The manifest/report name this magenta outline.
    hidden_quads.sort(key=lambda q: -q[0])
    for _, pts, t in hidden_quads:
        draw.polygon([(p[0], p[1]) for p in pts], outline=(255, 40, 255, 90))
    # a few longitudinal / latitudinal contour lines make the 3-D shape readable
    for i in range(0, lat, max(1, lat // 4)):
        for j in range(lon - 1):
            a, b = verts[i][j], verts[i][j + 1]
            if a and b and vis[i][j] and vis[i][j + 1]:
                rgb = color_for((a[3] + b[3]) * 0.5)
                draw.line([(a[0], a[1]), (b[0], b[1])], fill=rgb + (210,), width=1)
    for j in range(0, lon - 1, max(1, (lon - 1) // 6)):
        for i in range(lat - 1):
            a, b = verts[i][j], verts[i + 1][j]
            if a and b and vis[i][j] and vis[i + 1][j]:
                rgb = color_for((a[3] + b[3]) * 0.5)
                draw.line([(a[0], a[1]), (b[0], b[1])], fill=rgb + (180,), width=1)


def draw_active(img, active, faint=False):
    d = ImageDraw.Draw(img, "RGBA")
    for r in active:
        x, y = r["screen_xy"]
        if not (0 <= x < img.width and 0 <= y < img.height):
            continue
        if r["kind"] == "uniform":
            a = 80 if faint else 140
            d.ellipse([x - 1.0, y - 1.0, x + 1.0, y + 1.0], fill=(0, 220, 255, a))
        else:
            d.ellipse([x - 2.3, y - 2.3, x + 2.3, y + 2.3],
                      fill=(255, 145, 0, 190 if faint else 215))
            d.ellipse([x - 3.3, y - 3.3, x + 3.3, y + 3.3],
                      outline=(255, 255, 255, 200 if faint else 230), width=1)
    return img


def draw_focus_marks(img, focus_state):
    d = ImageDraw.Draw(img, "RGBA")
    for item in focus_state:
        x, y = item["matched"]["screen_xy"]
        col = FLOOR_MARKER if item["region"].startswith("floor") else ARCADE_MARKER
        d.ellipse([x - 5, y - 5, x + 5, y + 5], outline=col, width=2)
        d.line([x - 7, y, x + 7, y], fill=col, width=1)
        d.line([x, y - 7, x, y + 7], fill=col, width=1)
    return img


def render_rosettes(path, states, focus, lobes, lobe_max):
    cell = 150
    cols = len(focus)
    rows = len(states)
    img = Image.new("RGB", (cell * cols, cell * rows), (8, 10, 16))
    d = ImageDraw.Draw(img)
    for r, state in enumerate(states):
        for c, item in enumerate(focus):
            cx, cy = c * cell + cell / 2, r * cell + cell / 2
            R = ROSETTE_RADIUS_PX
            # 24 azimuth bins: maximum clamped luminance over the elevation ring
            n = ROSETTE_BINS
            radii = []
            az = np.arctan2(lobes["dirs"][:, 1], lobes["dirs"][:, 0]) % (2.0 * math.pi)
            lum = 0.2126 * lobes["clamped"][r, c][:, 0] + \
                  0.7152 * lobes["clamped"][r, c][:, 1] + \
                  0.0722 * lobes["clamped"][r, c][:, 2]
            for b in range(n):
                a0 = 2 * math.pi * b / n
                a1 = 2 * math.pi * (b + 1) / n
                mask = (az >= a0) & (az < a1)
                if not np.any(mask):
                    radii.append(0.0)
                else:
                    radii.append(max(0.0, min(1.0, float(np.max(lum[mask])) / max(lobe_max, 1e-9))))
            for b in range(n):
                a0 = 2 * math.pi * b / n - math.pi / 2
                a1 = 2 * math.pi * (b + 1) / n - math.pi / 2
                r0, r1 = radii[b] * R, radii[b] * R
                rgb = color_for(radii[b])
                d.polygon([(cx, cy),
                           (cx + r0 * math.cos(a0), cy + r0 * math.sin(a0)),
                           (cx + r1 * math.cos(a1), cy + r1 * math.sin(a1))],
                          fill=rgb)
            d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=(45, 55, 75))
            d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=(235, 240, 255))
    img.save(path)


# ---------------------------------------------------------------------------
# data stage
# ---------------------------------------------------------------------------

def decode_state(root: Path):
    dump = Dump(root)
    placement = decode_screen_probes(dump)
    info = dump.info()
    scopes = dump.frame_scope()
    state = {
        "source_root": str(root),
        "dump": {
            "project": info.get("Project"),
            "engine": f"UE {info.get('EngineMajorVersion')}.{info.get('EngineMinorVersion')}.{info.get('EnginePatchVersion')}",
            "build": info.get("BuildTarget"),
            "config": info.get("BuildConfiguration"),
            "platform": info.get("Platform"),
            "gpu": info.get("GPUDeviceDescription"),
            "driver": info.get("GPUDriverUserVersion"),
            "rhi": info.get("RHI"),
            "dump_time": info.get("DumpTime"),
            "frame_scopes": scopes,
            "frame_scope": scopes[0] if scopes else None,
            "frame_number": placement["view"]["frame_number"],
            "frame_counter": placement["view"]["frame_counter"],
            "state_frame_index": placement["view"]["state_frame_index"],
            "state_frame_index_mod8": placement["view"]["state_frame_index_mod8"],
        },
        "screenshot": {
            "path": str(dump.base / "Screenshot.png"),
            "sha256": sha256_file(dump.base / "Screenshot.png"),
            "extent": list(Image.open(dump.base / "Screenshot.png").size),
        },
        "view": {k: js(v) for k, v in placement["view"].items()},
        "params": placement["params"],
        "counts": placement["counts"],
        "spawn_pass": placement["spawn_pass"],
        "sh_pass": placement["sh_pass"],
        "active": placement["active"],
        "inactive": placement["inactive"],
        "stale": placement["stale"],
        "validation": placement["validation"],
        "records": placement["records"],
    }
    # final scene depth for the figure stage: last DepthPassParallel output named SceneDepthZ
    depth_rec = None
    for p in sorted(dump.passes, key=lambda p: p["Id"]):
        for out in p.get("OutputResources", []):
            uid = out.split(".")[0]
            d = dump.desc_by_uid.get(uid) or {}
            if d.get("Name") == "SceneDepthZ" and out.endswith(".mip0") and p.get("EventName", "").startswith("DepthPassParallel"):
                try:
                    arr, lay = dump.read_texture(uid, p["Pointer"])
                except Exception:
                    continue
                depth_rec = dump.tex_record("scene_depth", uid, p["Pointer"], arr, lay, p)
    state["scene_depth"] = depth_rec
    return state


def build_data(state_roots, out_dir: Path, focus_count=10):
    data_dir = out_dir / "publish" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    states = {}
    for name, root in state_roots:
        print(f"[decode] {name}: {root}")
        st = decode_state(Path(root))
        st["state"] = name
        states[name] = st
        (data_dir / "states").mkdir(parents=True, exist_ok=True)
        (data_dir / "states" / f"{name}.json").write_text(
            json.dumps(js(st), separators=(",", ":"), allow_nan=False), encoding="utf-8")
        c = st["counts"]
        print(f"  active={c['active_total_count']} uniform={c['active_uniform_count']} "
              f"adaptive={c['active_adaptive_count']} stale={c['stale_finite_adaptive_count']}")

    # choose the focused subset on the reference state
    ref_name = state_roots[0][0]
    ref = states[ref_name]
    focus = choose_focused(ref["active"], focus_count)
    if len(focus) < 8:
        raise RuntimeError(f"focused subset too small: {len(focus)}")
    dirs = fibonacci_directions(162)
    transfer = np.array([calc_diffuse_transfer_sh3(n) for n in dirs])
    focus_json = []
    signed_all, clamped_all = [], []
    state_order = [n for n, _ in state_roots]
    for idx, (region, r) in enumerate(focus):
        item = {
            "id": f"{region}-{idx + 1}",
            "region": region,
            "reference": {
                "state": ref_name,
                "atlas": r["atlas"],
                "world_cm": r["world_cm"],
                "normal": r["normal"],
                "depth_cm": r["depth_cm"],
                "sh": r["sh"],
            },
            "matches": {},
        }
        signed_i, clamped_i = [], []
        for name in state_order:
            act = states[name]["active"]
            m, dist = match_probes(r, act)
            if m is None:
                item["matches"][name] = {"missing": True}
                signed_i.append(np.zeros((len(dirs), 3)))
                clamped_i.append(np.zeros((len(dirs), 3)))
                continue
            tile_cm = 16.0 * float(r["depth_cm"]) / (0.5 * float(states[name]["view"]["view_size"][0]) *
                                                      abs(float(states[name]["view"]["M_translated_world_to_clip"][1][0])))
            nd = float(np.dot(np.asarray(r["normal"]), np.asarray(m["normal"])))
            sh_m = np.asarray(m["sh"], dtype=np.float64).reshape(3, 9)
            sh_r = np.asarray(r["sh"], dtype=np.float64).reshape(3, 9)
            delta = sh_m - sh_r
            sgn, clp, lum_s, lum_c = evaluate_lobes(sh_m, dirs)
            signed_i.append(sgn)
            clamped_i.append(clp)
            same_atlas = list(m["atlas"]) == list(r["atlas"])
            identical = bool(dist < 1e-6 and nd > 0.999999)
            incompatible = bool(dist > max(1.0, 0.75 * tile_cm) or nd < 0.90)
            item["matches"][name] = {
                "matched_atlas": m["atlas"],
                "matched_kind": m["kind"],
                "matched_world_cm": m["world_cm"],
                "matched_translated_world_cm": m["translated_world_cm"],
                "matched_normal": m["normal"],
                "matched_depth_cm": m["depth_cm"],
                "matched_screen_xy": m["screen_xy"],
                "distance_cm": round(dist, 6),
                "distance_m": round(dist / 100.0, 8),
                "normal_dot": round(nd, 8),
                "same_atlas_coord": same_atlas,
                "physical_sampling_position_identical": identical,
                "tile_footprint_cm": round(tile_cm, 6),
                "geometric_incompatibility_threshold_cm": round(max(1.0, 0.75 * tile_cm), 6),
                "geometric_incompatible": incompatible,
                "sh_state": sh_m.reshape(-1).round(8).tolist(),
                "sh_delta_rgb": delta.reshape(-1).round(8).tolist(),
                "sh_delta_l2": round(float(np.linalg.norm(delta)), 8),
                "delta_l0_rgb": delta[:, 0].round(8).tolist(),
                "lobe_signed_min": round(float(np.min(lum_s)), 8),
                "lobe_signed_max": round(float(np.max(lum_s)), 8),
                "lobe_clamped_max": round(float(np.max(lum_c)), 8),
                "lobe_positive_fraction": round(float(np.mean(lum_s > 0.0)), 6),
                "lobe_negative_clamped_fraction": round(float(np.mean(lum_s < 0.0)), 6),
            }
        item["directions"] = len(dirs)
        focus_json.append(item)
        signed_all.append(signed_i)
        clamped_all.append(clamped_i)

    signed_arr = np.transpose(np.asarray(signed_all), (1, 0, 2, 3))   # state, probe, dir, rgb
    clamped_arr = np.transpose(np.asarray(clamped_all), (1, 0, 2, 3))
    lum_signed = 0.2126 * signed_arr[..., 0] + 0.7152 * signed_arr[..., 1] + 0.0722 * signed_arr[..., 2]
    lum_clamped = np.maximum(lum_signed, 0.0)
    pos = lum_clamped[lum_clamped > 0]
    lobe_max = float(np.percentile(pos, 99)) if pos.size else 1.0
    lobe_max = max(0.02, float(round(lobe_max, 4)))

    np.savez_compressed(
        data_dir / "lobes.npz",
        dirs=dirs, signed=signed_arr, clamped=clamped_arr,
        luminance_signed=lum_signed, luminance_clamped=lum_clamped,
        state_names=np.array(state_order), probe_ids=np.array([i["id"] for i in focus_json]),
        lobe_max=np.array([lobe_max]),
    )
    summary = {
        "reference_state": ref_name,
        "focus_count": len(focus_json),
        "lobe_directions": int(len(dirs)),
        "lobe_max_luminance": lobe_max,
        "lobe_radius_world_cm": LOBE_RADIUS_WORLD_CM,
        "rosette_radius_px": ROSETTE_RADIUS_PX,
        "lobe_negative_signed_fraction": float(np.mean(lum_signed < 0.0)),
        "lobe_display_saturated_fraction": float(np.mean(lum_clamped > lobe_max)),
        "states": {n: states[n]["counts"] for n in state_order},
        "frame_scopes": {n: states[n]["dump"]["frame_scope"] for n in state_order},
        "screenshots": {n: states[n]["screenshot"] for n in state_order},
    }
    (data_dir / "focused_probes.json").write_text(
        json.dumps(js({"scales": summary, "probes": focus_json}), indent=1, allow_nan=False),
        encoding="utf-8")
    (data_dir / "summary.json").write_text(json.dumps(js(summary), indent=1, allow_nan=False),
                                           encoding="utf-8")
    cr = constant_radiance_validation()
    vals = {
        "constant_radiance_sh": cr,
        "states": {
            n: {
                "counts": states[n]["counts"],
                "uniform_screen_center_error": states[n]["validation"]["uniform_screen_center_error"],
                "adaptive_screen_center_error": states[n]["validation"]["adaptive_screen_center_error"],
                "sh_finite_active_count": states[n]["validation"]["sh_finite_active_count"],
                "sh_nonfinite_active_count": states[n]["validation"]["sh_nonfinite_active_count"],
                "active_nonempty": states[n]["counts"]["active_total_count"] > 0,
            } for n in state_order
        },
    }
    vals["all_states_nonempty"] = all(v["active_nonempty"] for v in vals["states"].values())
    vals["all_active_sh_finite"] = all(v["sh_nonfinite_active_count"] == 0 for v in vals["states"].values())
    vals["uniform_centers_ok"] = all(
        (v["uniform_screen_center_error"].get("max_px") or 0.0) < 1.0
        for v in vals["states"].values())
    vals["adaptive_centers_ok"] = all(
        (v["adaptive_screen_center_error"].get("max_px") or 0.0) < 1.0
        for v in vals["states"].values())
    vals["ok"] = bool(cr["normalization_ok"] and vals["all_states_nonempty"] and
                      vals["all_active_sh_finite"] and vals["uniform_centers_ok"] and
                      vals["adaptive_centers_ok"])
    (data_dir / "validation.json").write_text(json.dumps(js(vals), indent=1, allow_nan=False),
                                              encoding="utf-8")
    return states, focus_json, summary, vals


# ---------------------------------------------------------------------------
# figure stage
# ---------------------------------------------------------------------------

def build_figures(state_roots, out_dir: Path):
    data_dir = out_dir / "publish" / "data"
    fig_dir = out_dir / "publish" / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    summary = json.loads((data_dir / "summary.json").read_text(encoding="utf-8"))
    focus_doc = json.loads((data_dir / "focused_probes.json").read_text(encoding="utf-8"))
    focus = focus_doc["probes"]
    lobe_max = float(summary["lobe_max_luminance"])
    npz = np.load(data_dir / "lobes.npz", allow_pickle=False)
    states = {n: json.loads((data_dir / "states" / f"{n}.json").read_text(encoding="utf-8"))
              for n, _ in state_roots}
    dirs, mesh = lobe_directions()

    # keep the rosette code's azimuth order consistent with the NPZ direction order
    state_order = [n for n, _ in state_roots]
    for si, name in enumerate(state_order):
        st = states[name]
        scene = scene_depth_array(st)
        base = open_screenshot(st)
        # active placement overlay
        act_img = draw_active(base.copy(), st["active"])
        act_img.convert("RGB").save(fig_dir / f"active_{name}.png")
        # projected lobes; the separate active_* figure carries the placement dots
        lob_img = base.copy()
        focus_state = []
        for pi, item in enumerate(focus):
            m = item["matches"].get(name)
            if not m or m.get("missing"):
                continue
            sh = np.asarray(m["sh_state"], dtype=np.float64).reshape(3, 9)
            probe_world = m.get("matched_translated_world_cm")
            if probe_world is None:
                probe_world = np.asarray(m["matched_world_cm"]) + np.asarray(
                    st["view"]["pre_view_translation_cm"])
            # use the full 3x9 SH, not just luminance
            draw_lobe(ImageDraw.Draw(lob_img, "RGBA"), st["view"], probe_world, sh,
                      dirs, mesh, lobe_max, scene, depth_test=scene is not None)
            x, y = m["matched_screen_xy"]
            focus_state.append({"region": item["region"], "matched": {"screen_xy": [x, y]}})
        draw_focus_marks(lob_img, focus_state)
        lob_img.convert("RGB").save(fig_dir / f"lobes_{name}.png")
        print(f"[figure] {name}: active_{name}.png lobes_{name}.png")

    render_rosettes(fig_dir / "lobes_rosettes.png", state_order, focus, npz, lobe_max)
    return fig_dir


# ---------------------------------------------------------------------------
# provenance / manifest / report
# ---------------------------------------------------------------------------

def collect_manifest(state_roots, out_dir, states, focus, summary, vals):
    files = []
    for p in sorted(out_dir.rglob("*")):
        if not p.is_file() or p.name in (".gitkeep", "manifest.json"):
            continue
        rel = p.relative_to(out_dir)
        rel_s = str(rel).replace("\\", "/")
        if not (rel.parts and (rel.parts[0] == "publish" or rel_s in ("decoder.py", "report.md"))):
            continue
        files.append({
            "path": rel_s,
            "bytes": p.stat().st_size,
            "sha256": sha256_file(p),
        })
    raw = {}
    for name, _ in state_roots:
        st = states[name]
        raw[name] = {
            "source_root": st["source_root"],
            "frame_scope": st["dump"]["frame_scope"],
            "frame_number": st["dump"]["frame_number"],
            "frame_counter": st["dump"]["frame_counter"],
            "state_frame_index": st["dump"]["state_frame_index"],
            "spawn_pass": st["spawn_pass"],
            "sh_pass": st["sh_pass"],
            "parameters": {
                "nested_struct": st["params"]["nested_struct"],
                "nested_offset_in_FParameters": st["params"]["nested_offset_in_FParameters"],
            },
            "operands": {**st["records"], **({"scene_depth": st["scene_depth"]} if st.get("scene_depth") else {})},
            "view_struct": st["records"].get("view"),
        }
    checks = [
        {"name": "all_states_nonempty", "ok": bool(vals["all_states_nonempty"]),
         "evidence": {n: states[n]["counts"]["active_total_count"] for n, _ in state_roots}},
        {"name": "constant_radiance_sh_normalization", "ok": bool(vals["constant_radiance_sh"]["normalization_ok"]),
         "evidence": vals["constant_radiance_sh"]},
        {"name": "projection_stored_positions_to_screen_centers", "ok": bool(vals["uniform_centers_ok"]),
         "evidence": {n: states[n]["validation"]["uniform_screen_center_error"] for n, _ in state_roots}},
        {"name": "adaptive_encoded_centers", "ok": bool(vals["adaptive_centers_ok"]),
         "evidence": {n: states[n]["validation"]["adaptive_screen_center_error"] for n, _ in state_roots}},
        {"name": "active_bound_allocator_header", "ok": all(
            states[n]["counts"]["active_bound_within_max"] and
            states[n]["counts"]["active_bound_within_atlas"] and
            states[n]["counts"]["count_matches_header_sum"] for n, _ in state_roots),
         "evidence": {n: states[n]["counts"] for n, _ in state_roots}},
        {"name": "known_field_formats", "ok": all(
            all(v["ok"] for v in states[n]["validation"]["field_formats"].values())
            for n, _ in state_roots),
         "evidence": {n: states[n]["validation"]["field_formats"] for n, _ in state_roots}},
        {"name": "parameter_bindings_match_metadata", "ok": all(
            states[n]["validation"]["parameter_bindings"]["ok"] and
            states[n]["validation"]["sh_parameter_counts_match_spawn"] for n, _ in state_roots),
         "evidence": {n: {"bindings": states[n]["validation"]["parameter_bindings"],
                          "sh_parameter_counts_match_spawn":
                              states[n]["validation"]["sh_parameter_counts_match_spawn"]}
                      for n, _ in state_roots}},
        {"name": "captured_view_exposure_and_pretranslation", "ok": all(
            states[n]["view"].get("preexposure_reciprocal_ok") and
            len(states[n]["view"].get("pre_view_translation_cm", [])) == 3
            for n, _ in state_roots),
         "evidence": {n: {"preexposure": states[n]["view"]["preexposure"],
                          "one_over_preexposure": states[n]["view"]["one_over_preexposure"],
                          "pre_view_translation_cm": states[n]["view"]["pre_view_translation_cm"]}
                      for n, _ in state_roots}},
        {"name": "finite_nonempty_outputs", "ok": bool(vals["all_active_sh_finite"] and
                                                     all((out_dir / "publish" / "figures" / f"lobes_{n}.png").exists()
                                                         for n, _ in state_roots)),
         "evidence": {n: states[n]["validation"]["sh_nonfinite_active_count"] for n, _ in state_roots}},
        {"name": "active_vs_stale_preserved", "ok": all(
            states[n]["counts"]["stale_finite_adaptive_count"] == len(states[n]["stale"])
            for n, _ in state_roots),
         "evidence": {n: {"active": states[n]["counts"]["active_total_count"],
                          "stale": states[n]["counts"]["stale_finite_adaptive_count"],
                          "inactive_unlit": states[n]["counts"]["inactive_unlit_uniform_count"]}
                      for n, _ in state_roots}},
        {"name": "focused_subset_8_to_12", "ok": 8 <= len(focus) <= 12, "evidence": len(focus)},
    ]
    manifest = {
        "schema": "temporal-sh-decode/1",
        "generated_by": "temporal-sh/decoder.py",
        "dump_root": str(Path(state_roots[0][1]).parent),
        "states": [{"name": n, "root": str(r)} for n, r in state_roots],
        "basis": {
            "order": SH_ORDER,
            "shader_function": "SHBasisFunction3 / CalcDiffuseTransferSH3 (SHCommon.ush)",
            "projects_with": "sphere average over the captured 8x8 equi-area directions",
            "storage": "ScreenProbeRadianceSHAmbient = L0 RGB (R11G11B10); "
                       "ScreenProbeRadianceSHDirectional = 4 channels x 2 texels per RGB channel (RGBA16F)",
            "flat_order": "each sh array is 27 signed numbers: R0..R8, G0..G8, B0..B8",
            "channel_texel_order": "R: x+0*W,x+1*W; G: x+2*W,x+3*W; B: x+4*W,x+5*W",
            "sign": "stored coefficients are signed; no clamp is applied to the stored data",
        },
        "formula": {
            "D": "4 * sum_i sh_i * CalcDiffuseTransferSH3(normal)_i (AO=1, exponent=1)",
            "evaluate_sh_irradiance_source": "SHCommon.ush EvaluateSHIrradiance clamps to zero; "
                                             "the decoder keeps the signed dot and separately reports the explicit display clamp",
            "irradiance_preexposed": "pi * D  (equals the shader's 4*pi*DotSH3 form)",
            "irradiance_unpreexposed": "pi * D / PreExposure",
            "preexposure": "captured View.PreExposure, not a constant",
        },
        "scales": {
            "lobe_radius_world_cm": LOBE_RADIUS_WORLD_CM,
            "lobe_response_scale_luminance": summary["lobe_max_luminance"],
            "rosette_radius_px": ROSETTE_RADIUS_PX,
            "fixed_across_states_and_probes": True,
            "negative_display_clamp": "max(0, signed response) before radius/color",
            "color_stops": COLOR_STOPS,
            "marker_colors": {
                "uniform": list(ACTIVE_MARKER_UNIFORM),
                "adaptive": list(ACTIVE_MARKER_ADAPTIVE),
                "floor_focus": list(FLOOR_MARKER),
                "arcade_focus": list(ARCADE_MARKER),
            },
        },
        "figure_legend": {
            "active_*": "captured screenshot + every active placement dot; cyan uniform, orange adaptive",
            "lobes_*": "captured screenshot + the 10 focused projected response lobes; no all-active dot grid",
            "visible_lobe": "colored filled surface, depth-tested against the captured SceneDepthZ",
            "xray_lobe": "magenta outline: depth-occluded part of the same function lobe, drawn as an "
                         "explicit x-ray function glyph; it is not a physical object",
            "focus_markers": "green cross = floor reference, magenta cross = arcade reference",
            "rosettes": "fixed-scale azimuth reduction of the same 162-direction clamped luminance",
        },
        "focus": {
            "count": len(focus),
            "reference_state": summary["reference_state"],
            "probes": [{"id": p["id"], "region": p["region"], "reference_atlas": p["reference"]["atlas"]}
                       for p in focus],
        },
        "raw_operands": raw,
        "checks": checks,
        "validation": vals,
        "files": files,
        "limitations": [
            "The five roots are separate captures; probe sampling positions move with the captured TAA/probe jitter even for the fixed camera.",
            "Nearest-reference matching is by world distance; it is not a claim that the two samples are the same physical location.",
            "No shader-local blend weights, ray weights, or per-index dispatch tables are claimed; only captured resource bytes are decoded.",
            "Stored values beyond the captured allocator count are stale and are not interpreted as active coefficients.",
        ],
    }
    (out_dir / "manifest.json").write_text(json.dumps(js(manifest), indent=1, allow_nan=False),
                                           encoding="utf-8")
    return manifest


def update_output_hashes(manifest, out_dir):
    """Refresh non-data output hashes that are written after file collection."""
    for rel in ("report.md",):
        p = out_dir / rel
        if not p.exists():
            continue
        entry = next((f for f in manifest["files"] if f["path"] == rel), None)
        if entry is None:
            entry = {"path": rel}
            manifest["files"].append(entry)
        entry["bytes"] = p.stat().st_size
        entry["sha256"] = sha256_file(p)
    return manifest


def write_report(out_dir, states, focus, summary, vals, manifest):
    state_order = list(states.keys())
    lines = []
    A = lines.append
    A("# Screen-probe SH over the captured light steps")
    A("")
    A("Generated by `temporal-sh/decoder.py`.  All placement, SH, view and depth values below "
      "are decoded from named DumpGPU resources; no id from an earlier dump is embedded.")
    A("")
    A("## Captured states")
    A("")
    A("| state | frame scope | frame/state | active | uniform | adaptive | stale finite | screenshot |")
    A("|---|---|---|---:|---:|---:|---:|---|")
    for n in state_order:
        st = states[n]
        A(f"| {n} | {st['dump']['frame_scope']} | {st['dump']['frame_number']} / {st['dump']['state_frame_index']} | "
          f"{st['counts']['active_total_count']} | {st['counts']['active_uniform_count']} | "
          f"{st['counts']['active_adaptive_count']} | {st['counts']['stale_finite_adaptive_count']} | "
          f"`{st['screenshot']['path']}` |")
    A("")
    A("The screenshots are the captured presented frames.  The probe atlas is 80x84 storage, "
      "80x45 uniform screen tiles (16 px downsample), 3600 uniform slots and a captured adaptive "
      "allocator count per state.  The allocator bound is "
      "`min(NumAdaptiveScreenProbes, MaxNumAdaptiveProbes, atlas_view_capacity - NumUniformScreenProbes)`; "
      "the activity header sum is reported with each state.")
    A("")
    A("## SH convention and response units")
    A("")
    A("The stored SH is the sphere-average radiance projection used by "
      "`ScreenProbeConvertToIrradiance`: L0 in `ScreenProbeRadianceSHAmbient`, coefficients 1..8 "
      "in six RGBA16F texel columns per probe in `ScreenProbeRadianceSHDirectional`.  "
      "Coefficient order is `" + ", ".join(SH_ORDER) + "`; every flat 27-value array in the data "
      "is R0..R8, G0..G8, B0..B8.")
    A("")
    A("The signed unit-albedo Lambert response is")
    A("")
    A("`D(n) = 4 * sum_i sh_i * CalcDiffuseTransferSH3(n)_i`")
    A("")
    A("with AO=1 and exponent=1.  This is the shader's `EvaluateSHIrradiance` without its "
      "display zero-clamp and with the factor 4 that maps a constant radiance field back to its "
      "radiance.  `D` is still view-pre-exposed.  The physical irradiance forms are "
      "`pi*D` (the shader's `4*pi*DotSH3`) and `pi*D/PreExposure` (un-preexposed), where "
      "`PreExposure` is read from the captured View uniform buffer.")
    A("")
    cr = vals["constant_radiance_sh"]
    A("Constant-radiance self-test: projected L0 = `%.9f` (expected `%.9f`), max other coefficient "
      "= `%.3e`, exact-basis max |D-1| = `%.3e`, captured-8x8-quadrature max |D-1| = `%.3e` over %d "
      "normals.  Normalization: **%s**." %
      (cr["projected_l0"], cr["expected_l0"], cr["max_other_coefficient"],
       cr["exact_sh_max_abs_D_minus_radiance"], cr["discrete_8x8_max_abs_D_minus_radiance"],
       cr["directions_used_for_evaluation"],
       "PASS" if cr["normalization_ok"] else "FAIL"))
    A("")
    A("The exact-basis test is the normalization check.  The captured 8x8 equi-area projection "
      "leaves a small higher-band residual (`max other coefficient` above); that is the shader's "
      "discrete quadrature, not a decoder error, and is reported rather than hidden.")
    A("")
    A("## Placement and projection checks")
    A("")
    A("| state | uniform center max err (px) | adaptive center max err (px) | count == header sum | active bound |")
    A("|---|---:|---:|---|---|")
    for n in state_order:
        st = states[n]
        u = st["validation"]["uniform_screen_center_error"]
        a = st["validation"]["adaptive_screen_center_error"]
        c = st["counts"]
        A(f"| {n} | {u.get('max_px')} | {a.get('max_px')} | {c['count_matches_header_sum']} | "
          f"{c['active_adaptive_bound']} / {c['max_adaptive_probes']} |")
    A("")
    A("Uniform probes are checked against `ViewRectMin + tile*16 + integer Hammersley jitter + 0.5`. "
      "Adaptive probes are checked against the captured `AdaptiveScreenProbeData` encoded screen "
      "position.  Both use the captured `TranslatedWorldToClip` with the validated `v @ reshape(4,4)` "
      "row-vector convention.")
    A("")
    A("## Focused floor / arcade subset")
    A("")
    A("`%d` reference probes were selected on the baseline floor (`normal.z > 0.95`, central lower "
      "screen band) and the side arcades (left/right screen bands).  For every other state each "
      "reference probe is "
      "matched to its nearest active probe by world-space distance.  The match is a nearest sample, "
      "not an identity claim: the fixed camera still moves probe sampling positions through the "
      "captured TAA/probe jitter." % len(focus))
    A("")
    A("| id | region | ref atlas | ref world (cm) | worst match dist (cm) | worst normal dot | incompatible states |")
    A("|---|---|---|---|---:|---:|---|")
    for item in focus:
        worst_d = max((m.get("distance_cm", 0) or 0) for m in item["matches"].values())
        worst_nd = min((m.get("normal_dot", 1) for m in item["matches"].values() if "normal_dot" in m), default=1)
        bad = [s for s, m in item["matches"].items() if m.get("geometric_incompatible")]
        A(f"| {item['id']} | {item['region']} | {item['reference']['atlas']} | "
          f"({item['reference']['world_cm'][0]:.2f}, {item['reference']['world_cm'][1]:.2f}, "
          f"{item['reference']['world_cm'][2]:.2f}) | {worst_d:.3f} | {worst_nd:.4f} | "
          f"{', '.join(bad) if bad else 'none'} |")
    A("")
    A("Per-state distances, normal dots, actual matched world positions, signed coefficient deltas "
      "(27 values), L0 deltas and geometric flags are in `publish/data/focused_probes.json`.  "
      "The full signed coefficients for every active probe are in `publish/data/states/*.json`.")
    A("")
    A("## Lobes and figures")
    A("")
    A("The projected lobes are real 3-D response surfaces: for each of %d hypothetical receiver "
      "normals the signed RGB response is evaluated, negative display response is explicitly clamped "
      "to zero, and the vertex radius is `%s cm * clamp(L / %s, 0, 1)` in world space.  "
      "The same world radius and luminance scale is used for every state and every focused probe; "
      "nothing is normalized per probe.  Vertices are projected with the captured view and depth-tested "
      "against the captured `SceneDepthZ`; visible quads are filled and depth-occluded quads are drawn "
      "as an explicit magenta x-ray function glyph (not a physical object).  The negative signed luminance "
      "fraction is `%.4f`; the display-saturated fraction is `%.4f`." %
      (summary["lobe_directions"], LOBE_RADIUS_WORLD_CM, summary["lobe_max_luminance"],
       summary["lobe_negative_signed_fraction"], summary["lobe_display_saturated_fraction"]))
    A("")
    A("Figures (no burned text; HTML labels are the collector's responsibility):")
    A("")
    for n in state_order:
        A(f"- `publish/figures/active_{n}.png`: captured screenshot + active placement (cyan uniform, orange adaptive).")
        A(f"- `publish/figures/lobes_{n}.png`: captured screenshot + the {len(focus)} focused projected lobes "
          f"(no all-active dot grid; magenta outline = depth-occluded x-ray function glyph).")
    A("- `publish/figures/lobes_rosettes.png`: fixed-scale 24-azimuth rosettes, one row per state and one column per focused probe.")
    A("")
    A("## Active vs stale")
    A("")
    A("Only adaptive indices below the captured allocator bound are active.  Finite storage beyond "
      "that bound is recorded as `stale` and its SH is not decoded.  Uniform probes with negative "
      "scene depth are recorded as `inactive_unlit`; uniform cells with zero depth are counted but "
      "not listed.  No shader-local adaptive-index assignment table, blend weight or ray weight is "
      "claimed: the per-index atlas position is the captured `AdaptiveScreenProbeData` word and the "
      "SH is the captured texture value at the corresponding atlas coordinate.")
    A("")
    A("## Reuse")
    A("")
    A("`python temporal-sh/decoder.py --dump-root <run-dir> --states baseline,lightstep-... "
      "--out <out-dir> --stage all`")
    A("")
    A("`--stage data` writes `publish/data`; `--stage figures` re-renders figures from the decoded "
      "data.  `--state NAME=PATH` accepts an explicit selected root.  The JSON/NPZ schema is stable "
      "for the main temporal collector: states, active probes, signed SH, view matrices/exposure, "
      "focused matches/deltas and lobe arrays.")
    A("")
    A("## Validation summary")
    A("")
    for c in manifest["checks"]:
        A(f"- **{c['name']}**: {'PASS' if c['ok'] else 'FAIL'}")
    A("")
    A("Provenance: every texture/buffer operand record in `manifest.json` has resource id, version, "
      "relative path, format, byte count and SHA-256; every parameter/view struct has content id, "
      "metadata id, struct type, path and SHA-256.")
    A("")
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_state_roots(args):
    if args.state:
        out = []
        for s in args.state:
            if "=" not in s:
                raise SystemExit(f"--state must be NAME=PATH, got {s!r}")
            n, p = s.split("=", 1)
            out.append((n, Path(p)))
        return out
    if not args.dump_root:
        raise SystemExit("provide --dump-root or --state NAME=PATH")
    name_list = args.states.split(",") if args.states else STATES_DEFAULT
    return [(n, Path(args.dump_root) / n) for n in name_list]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dump-root", type=Path, help="directory containing named state roots")
    ap.add_argument("--states", default=",".join(STATES_DEFAULT),
                    help="comma-separated state directory names")
    ap.add_argument("--state", action="append", help="explicit NAME=PATH selected root (repeatable)")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent)
    ap.add_argument("--stage", choices=["data", "figures", "all"], default="all")
    ap.add_argument("--focus-count", type=int, default=10)
    args = ap.parse_args()
    state_roots = parse_state_roots(args)
    for n, p in state_roots:
        if not Path(p).exists():
            raise SystemExit(f"state {n}: missing {p}")
    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.stage in ("data", "all"):
        states, focus, summary, vals = build_data(state_roots, out_dir, args.focus_count)
    else:
        states = {n: json.loads((out_dir / "publish" / "data" / "states" / f"{n}.json").read_text(encoding="utf-8"))
                  for n, _ in state_roots}
        focus_doc = json.loads((out_dir / "publish" / "data" / "focused_probes.json").read_text(encoding="utf-8"))
        focus = focus_doc["probes"]
        summary = json.loads((out_dir / "publish" / "data" / "summary.json").read_text(encoding="utf-8"))
        vals = json.loads((out_dir / "publish" / "data" / "validation.json").read_text(encoding="utf-8"))
    if args.stage in ("figures", "all"):
        build_figures(state_roots, out_dir)
    manifest = collect_manifest(state_roots, out_dir, states, focus, summary, vals)
    write_report(out_dir, states, focus, summary, vals, manifest)
    update_output_hashes(manifest, out_dir)
    (out_dir / "manifest.json").write_text(json.dumps(js(manifest), indent=1, allow_nan=False),
                                           encoding="utf-8")
    ok = all(c["ok"] for c in manifest["checks"])
    print(f"[done] {out_dir}  checks={'PASS' if ok else 'FAIL'}")
    if not ok:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
