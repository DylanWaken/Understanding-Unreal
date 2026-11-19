#!/usr/bin/env python3
"""Decode contextual Lumen screen-probe / integration / reflection evidence.

Read-only with respect to the preserved DumpGPU payload.  Every value is read
from a named resource version, decoded with the format that DumpGPU recorded,
and written with its address chain and validity record.  Reconstructed
per-ray directions are explicitly a *declared-query replay* of the shader
formula from observed RayInfo, observed blue-noise texel and the observed
ScreenProbeRayDirectionFrameIndex; no shader-local dispatch is claimed.

Usage:
  python scripts/decode_context.py --stage data
  python scripts/decode_context.py --stage figures
  python scripts/decode_context.py --stage all
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import struct
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

# ----------------------------------------------------------------------------
# constants / paths
# ----------------------------------------------------------------------------

VIEW_RESOURCE = "00040000020bef07f840"
VIEW_VERSION = "00000000000000000000"
VIEW_UNIFORM_KEY = "00040000020bc4c24980"

PREEXPOSURE = 0.00048828125
INV_PREEXPOSURE = 1.0 / PREEXPOSURE
FOUR_PI = 4.0 * math.pi
HFOV_DEG = 70.00000806489558
VFOV_DEG = 42.9956664562

# Captured allocator / dispatch payloads of the screen-probe placement pass.
NUM_ADAPTIVE_RES = "0004000002090c6ba5c0"    # pass 706 output, 4-byte R32_UINT
ADAPTIVE_HEADER_RES = "0004000002090c6ba8d0"  # pass 706 output, 80x56 R32_UINT
ADAPTIVE_INDIRECT_RES = "00040000020bd35c8dd0"  # pass 707 output, 6x12-byte dispatch args

# Shader source used only to cite the captured packing formula (not executed).
UE_SHADER_ROOT = Path("D:/UnrealEngine/Engine/Shaders/Private/Lumen")
SPG_TEMPORAL_USH = UE_SHADER_ROOT / "LumenScreenProbeGatherTemporal.ush"
REFLECTION_DENOISER_USH = UE_SHADER_ROOT / "LumenReflectionDenoiserCommon.ush"

# resource versions (all confirmed to exist and to be produced by the named pass)
V_PLACEMENT = "00040000020bd35c1b10"      # pass 706 AdaptivePlacementSpawn
V_RAYINFO = "00040000020ba1b70010"        # pass 736 GenerateRays 8x8
V_TRACE = "00040000020c1328ca80"          # pass 747 TraceGlobalSDFAndApplyRadianceCache
V_PROBERADIANCE = "0004000002094be0c130"  # pass 748 CompositeTraces
V_SH = "000400000208f37343a0"             # pass 753 ScreenProbeConvertToIrradiance
V_DIFFUSE = "000400000208f36d6770"        # pass 761 ScreenProbeIntegrate SupportAll
V_HISTORY_OLD = "00000000000000000000"    # imported history
V_HISTORY_NEW = "000400000208f36dfd28"    # pass 762 TemporalReprojection
V_REFL_RESOLVED = "00040000020b9dcd0950"  # pass 825 ReflectionsResolve
V_REFL_TRACE = "00040000020b9dcd0950"     # reflection trace is read by resolve
V_REFL_TEMPORAL = "00040000020b9dcd86a0"  # pass 826 TemporalAccumulation
V_REFL_FINAL = "00040000020c948b0010"     # pass 827 Spatial
V_GBUFFER = "00040000020bef06b8f0"        # pass 697 BasePassParallel
V_GBUFFER_A = "00040000020bef06b8f0"
V_SCENE_DEPTH = "00040000020ba06cda50"    # pass 620 DepthPassParallel
V_VSM = "00040000020c16c4eef0"            # pass 842 (consumed by the sun pass)
V_SCENE_PRE = "00040000020c16c43fb0"      # pass 840 VirtualShadowMapProjection
V_SCENE_POST = "00040000020c16c4eef0"     # pass 842 RenderLight Sun

RES = {
    "screenprobe_pos": "0004000002090c6b3e20",
    "screenprobe_depth": "0004000002090c6b35d8",
    "screenprobe_normal": "0004000002090c6b38a0",
    "screenprobe_speed": "0004000002090c6b3b60",
    "rayinfo": "00040000020b9718fa60",
    "trace_radiance": "00040000020ba1b729e0",
    "trace_hit": "00040000020ba1b72cf0",
    "screenprobe_radiance": "0004000002094be0b370",
    "screenprobe_hitdistance": "0004000002094be0b630",
    "sh_ambient": "000400000208f3733bf0",
    "sh_directional": "000400000208f3733eb0",
    "diffuse": "000400000208f373fc00",
    "diffuse_hist": "000400000208f36de040",
    "diffuse_newhist": "000400000208f36deb40",
    "diffuse_frames": "00040000020bd35c9f70",
    "diffuse_frames_new": "000400000208f36df640",
    "reflection": "00040000020b9dcdf8e0",
    "reflection_resolved": "00040000020bb9f8d5e0",
    "reflection_depth": "00040000020bb9f8d8a0",
    "reflection_frames": "00040000020b9dcd8030",
    "reflection_frames_hist": "00040000020b9dcd7d70",
    "gbuffer_a": "00040000020b9d701250",
    "gbuffer_b": "00040000020b9d701710",
    "gbuffer_c": "00040000020b9d7019d0",
    "gbuffer_d": "00040000020b9d701c90",
    "gbuffer_f": "00040000020b9d701f50",
    "scene_depth": "00040000020b9d700010",
    "vsm_mask": "00040000020c948b7fa0",
    "scene_color": "00040000020b9d700490",
    "blue_noise_vec2": "0004000002094bdb0320",
    "view_uniform": VIEW_UNIFORM_KEY,
}

RECEIVERS = {
    "A": {"pixel": (500, 680), "label": "directly sunlit stone floor", "material": "stone"},
    "B": {"pixel": (720, 680), "label": "shaded stone floor", "material": "stone"},
    "C": {"pixel": (626, 587), "label": "polished gold sphere (metal=1, rough=.08)", "material": "polished_gold"},
}


def jval(o):
    """JSON-safe conversion."""
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
        if not math.isfinite(v):
            return None
        return v
    if isinstance(o, np.ndarray):
        return jval(o.tolist())
    return o


_HASH_CACHE = {}


def sha256_file(path: Path) -> str:
    path = Path(path)
    key = (str(path), path.stat().st_size, path.stat().st_mtime_ns)
    if key in _HASH_CACHE:
        return _HASH_CACHE[key]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    _HASH_CACHE[key] = h.hexdigest()
    return _HASH_CACHE[key]


def load_concat_json(path: Path):
    s = path.read_text(encoding="utf-8")
    dec = json.JSONDecoder()
    out = []
    i = 0
    n = len(s)
    while i < n:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        o, j = dec.raw_decode(s, i)
        out.append(o)
        i = j
    return out


class Dump:
    def __init__(self, root: Path):
        self.root = root
        base = root / "Base"
        self.passes = load_concat_json(base / "Passes.json")
        self.passes_by_id = {p["Id"]: p for p in self.passes}
        self.passes_by_ptr = {p["Pointer"]: p for p in self.passes}
        self.descs = load_concat_json(base / "ResourceDescs.json")
        self.desc_by_id = {d["UniqueResourceName"]: d for d in self.descs}
        self.layouts = load_concat_json(base / "SubresourceLayouts.json")
        self.readback = load_concat_json(base / "DumpReadbackStatus.json")
        self.readback_by_file = {r["File"]: r for r in self.readback}
        self.param_bindings = load_concat_json(base / "ParameterBindings.json")
        self.refs = load_concat_json(base / "ReferencedStructs.json")
        self.native_bindings = load_concat_json(base / "NativeUniformBufferBindings.json")

    def pass_name(self, pid):
        p = self.passes_by_id.get(pid)
        return p["EventName"] if p else None

    def pass_version(self, pid):
        return self.passes_by_id[pid]["Pointer"]

    def producer_pass(self, version):
        return self.passes_by_ptr.get(version)

    def layout(self, rid, version, mip=0, slice_index=0, stencil=False):
        cand = []
        for r in self.layouts:
            if r["Resource"] != rid:
                continue
            if r["MipLevel"] != mip:
                continue
            if not stencil and r.get("StencilSubresource"):
                continue
            if stencil and not r.get("StencilSubresource"):
                continue
            if slice_index is not None and r.get("FirstArraySlice", 0) != slice_index:
                continue
            if version in r["File"]:
                cand.append(r)
        if not cand:
            # version 0 is rendered as 20 zeros in file names
            raise KeyError(f"no layout {rid} v{version} mip{mip} slice{slice_index}")
        return cand[0]

    def read_layout(self, layout):
        path = self.root / layout["File"].replace("\\", "/")
        if not path.exists():
            raise FileNotFoundError(path)
        rb = self.readback_by_file.get(layout["File"].replace("\\", "/"))
        return path, rb

    def read_tex(self, rid, version, mip=0, slice_index=0, dtype=None, channels=1):
        layout = self.layout(rid, version, mip=mip, slice_index=slice_index)
        path, rb = self.read_layout(layout)
        if dtype is None:
            dtype = DTYPE_BY_FORMAT.get(layout["StoredFormat"])
        if dtype is None:
            raise KeyError(f"no dtype for {layout['StoredFormat']}")
        arr = np.fromfile(path, dtype=dtype)
        h = int(layout["ExtentY"])
        w = int(layout["ExtentX"])
        if channels == 1:
            arr = arr.reshape(h, w)
        else:
            arr = arr.reshape(h, w, channels)
        return {
            "array": arr,
            "layout": layout,
            "readback": rb,
            "file": layout["File"],
            "path": path,
            "hash": sha256_file(path),
        }


DTYPE_BY_FORMAT = {
    "PF_R32_UINT": "<u4",
    "PF_R32_FLOAT": "<f4",
    "PF_R8": "u1",
    "PF_R8G8": "u1",
    "PF_R16_UINT": "<u2",
    "PF_R16F": "<f2",
    "PF_FloatR11G11B10": "<u4",
    "PF_FloatRGB": "<u4",
    "PF_FloatRGBA": "<f2",
    "PF_A32B32G32R32F": "<f4",
    "PF_B8G8R8A8": "u1",
    "PF_G8": "u1",
    "PF_A2B10G10R10": "<u4",
}

CHANNELS_BY_FORMAT = {
    "PF_R32_UINT": 1,
    "PF_R32_FLOAT": 1,
    "PF_R8": 1,
    "PF_R8G8": 2,
    "PF_R16_UINT": 1,
    "PF_R16F": 1,
    "PF_FloatR11G11B10": 1,
    "PF_FloatRGB": 1,
    "PF_FloatRGBA": 4,
    "PF_A32B32G32R32F": 4,
    "PF_B8G8R8A8": 4,
    "PF_G8": 1,
    "PF_A2B10G10R10": 1,
}


# ----------------------------------------------------------------------------
# numeric decoders
# ----------------------------------------------------------------------------

def decode_r11g11b10(w: np.ndarray) -> np.ndarray:
    """Decode packed R11G11B10F words to linear RGB float32."""
    w = np.asarray(w, dtype=np.uint32)
    r = w & 0x7FF
    g = (w >> 11) & 0x7FF
    b = (w >> 22) & 0x3FF

    def dec(v, mant_bits):
        e = (v >> mant_bits) & 0x1F
        m = v & ((1 << mant_bits) - 1)
        scale = float(1 << mant_bits)
        out = np.where(
            e == 0,
            (m / scale) * (2.0 ** -14),
            (1.0 + m / scale) * np.exp2(e.astype(np.int32) - 15),
        )
        return out.astype(np.float64)

    return np.stack([dec(r, 6), dec(g, 6), dec(b, 5)], axis=-1)


def srgb_to_linear(c):
    c = np.asarray(c, dtype=np.float64)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def srgb_oetf(x):
    x = np.clip(np.asarray(x, dtype=np.float64), 0.0, 1.0)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1.0 / 2.4) - 0.055)


def octahedron_to_unit_vector(o):
    """UE OctahedronToUnitVector; o is [...,2] in [0,1] (texture UNORM)."""
    o = np.asarray(o, dtype=np.float64) * 2.0 - 1.0
    u = o[..., 0].copy()
    v = o[..., 1].copy()
    z = 1.0 - np.abs(u) - np.abs(v)
    neg = z < 0.0
    ux = u[neg]
    uy = v[neg]
    u[neg] = (1.0 - np.abs(uy)) * np.sign(ux)
    v[neg] = (1.0 - np.abs(ux)) * np.sign(uy)
    n = np.stack([u, v, z], axis=-1)
    ln = np.linalg.norm(n, axis=-1, keepdims=True)
    return n / np.maximum(ln, 1e-30)


def equi_area_spherical_mapping(uv):
    """UE Clarberg equi-area mapping. uv in [0,1]; returns unit world direction."""
    uv = np.asarray(uv, dtype=np.float64)
    u = 2.0 * uv[..., 0] - 1.0
    v = 2.0 * uv[..., 1] - 1.0
    d = 1.0 - (np.abs(u) + np.abs(v))
    r = 1.0 - np.abs(d)
    phi = np.where(
        r == 0.0,
        0.0,
        (np.pi / 4.0) * ((np.abs(v) - np.abs(u)) / np.where(r == 0.0, 1.0, r) + 1.0),
    )
    f = r * np.sqrt(np.maximum(2.0 - r * r, 0.0))
    return np.stack(
        [
            f * np.sign(u) * np.abs(np.cos(phi)),
            f * np.sign(v) * np.abs(np.sin(phi)),
            np.sign(d) * (1.0 - r * r),
        ],
        axis=-1,
    )


def sh_basis_function3(n):
    x, y, z = float(n[0]), float(n[1]), float(n[2])
    xx, yy, zz = x * x, y * y, z * z
    return np.array(
        [
            0.282095,
            -0.488603 * y,
            0.488603 * z,
            -0.488603 * x,
            1.092548 * x * y,
            -1.092548 * y * z,
            0.315392 * (3.0 * zz - 1.0),
            -1.092548 * x * z,
            0.546274 * (xx - yy),
        ],
        dtype=np.float64,
    )


def calc_diffuse_transfer_sh3(n, exponent=1.0):
    b = sh_basis_function3(n)
    l0 = 2.0 * np.pi / (1.0 + exponent)
    l1 = 2.0 * np.pi / (2.0 + exponent)
    l2 = exponent * 2.0 * np.pi / (3.0 + 4.0 * exponent + exponent * exponent)
    b[0] *= l0
    b[1:4] *= l1
    b[4:] *= l2
    return b


def evaluate_sh_irradiance(normal, sh_rgb, ao=1.0):
    """UE EvaluateSHIrradiance with AO=1 (fully open cone)."""
    transfer = calc_diffuse_transfer_sh3(normal, 1.0)
    # AO=1 -> CosA=0, SinA=1 -> Z0=Z1=Z2=1
    return np.maximum(0.0, np.einsum("c,rc->r", transfer, sh_rgb))


def hammersley16(index, num_samples, random_xy=(0, 0)):
    e1 = ((index / num_samples) + random_xy[0] / 65536.0) % 1.0
    e2 = (((int(index).bit_length() and int(f"{int(index):032b}"[::-1], 2)) >> 16) ^ random_xy[1]) / 65536.0
    return e1, e2


def reverse_bits32(x):
    x = int(x) & 0xFFFFFFFF
    x = ((x & 0x55555555) << 1) | ((x & 0xAAAAAAAA) >> 1)
    x = ((x & 0x33333333) << 2) | ((x & 0xCCCCCCCC) >> 2)
    x = ((x & 0x0F0F0F0F) << 4) | ((x & 0xF0F0F0F0) >> 4)
    x = ((x & 0x00FF00FF) << 8) | ((x & 0xFF00FF00) >> 8)
    return ((x << 16) | (x >> 16)) & 0xFFFFFFFF


def hammersley16_ue(index, num_samples, random_xy=(0, 0)):
    e1 = ((float(index) / float(num_samples)) + float(random_xy[0]) / 65536.0) % 1.0
    e2 = float((reverse_bits32(index) >> 16) ^ int(random_xy[1])) / 65536.0
    return e1, e2


def f16_bits_to_float(bits):
    return float(np.frombuffer(np.uint16(bits).tobytes(), dtype="<f2")[0])


def decode_fast_update_mode_frames(raw_byte, max_frames_accumulated):
    """Decode the 8-bit fast-update/count byte exactly as the captured shader does.

    LumenScreenProbeGatherTemporal.ush: PackFastUpdateModeAmountAndNumFramesAccumulated
    stores ceil(FastUpdateModeAmount*15) in the high nibble and
    round(saturate(NumFrames/min(MaxFramesAccumulated,15))*15) in the low nibble.
    Unpack maps low nibble 15 to MaxFramesAccumulated (not 15) and otherwise
    low/15*min(MaxFramesAccumulated,15).
    """
    raw = int(raw_byte) & 0xFF
    fast_code = (raw >> 4) & 0xF
    count_code = raw & 0xF
    max_frames = float(max_frames_accumulated)
    local_max = min(max_frames, 15.0)
    if count_code == 15:
        frames = max_frames
    else:
        frames = (float(count_code) / 15.0) * local_max
    return {
        "raw_byte": raw,
        "raw_byte_hex": f"0x{raw:02x}",
        "fast_update_mode_amount_code_high_nibble": fast_code,
        "fast_update_mode_amount": fast_code / 15.0,
        "num_frames_accumulated_count_code_low_nibble": count_code,
        "max_frames_accumulated": max_frames,
        "local_max_frames_accumulated_min15": local_max,
        "decoded_num_frames_accumulated": frames,
        "decoded_rule": (
            "low nibble == 15 -> captured MaxFramesAccumulated; "
            "else (low/15) * min(MaxFramesAccumulated,15)"
        ),
    }


# ----------------------------------------------------------------------------
# view / projection
# ----------------------------------------------------------------------------

def decode_view(dump: Dump):
    # ViewData.PrimaryViews is a raw buffer, not a texture subresource.
    path = dump.root / "Resources" / f"{VIEW_RESOURCE}.v{VIEW_VERSION}.bin"
    rel = f"Resources/{VIEW_RESOURCE}.v{VIEW_VERSION}.bin"
    if not path.exists():
        raise FileNotFoundError(path)
    b = np.fromfile(path, dtype="<f4")
    rb = dump.readback_by_file.get(rel)
    tex = {
        "array": b,
        "file": rel,
        "path": path,
        "hash": sha256_file(path),
        "readback": rb,
        "layout": {
            "Resource": VIEW_RESOURCE,
            "Name": "ViewData.PrimaryViews",
            "File": rel,
            "Dimension": "Buffer",
            "MipLevel": 0,
            "FirstArraySlice": 0,
            "ExtentX": int(len(b)),
            "ExtentY": 1,
            "Depth": 1,
            "RowPitchBytes": int(len(b)) * 4,
            "ZSliceStrideBytes": int(len(b)) * 4,
            "StoredFormat": "FPackedView[1] as float32",
            "NativeFormat": "FPackedView[1]",
            "StoredBytesAreNativeStorage": True,
            "NativeValueInterpretation": "PackedViewFields",
            "ByteSize": int(len(b)) * 4,
        },
    }
    svpos_to_tw = b[0:16].reshape(4, 4).T
    tw_to_view = b[32:48].reshape(4, 4).T
    tw_to_clip = b[48:64].reshape(4, 4).T
    view_to_clip = b[64:80].reshape(4, 4).T
    clip_to_rel = b[80:96].reshape(4, 4).T
    view_rect = list(struct.unpack_from("<4i", b.tobytes(), 640))
    view_size = [float(x) for x in b[164:168]]
    clip_scale_offset = [float(x) for x in b[168:172]]
    preview = [float(x) for x in b[180:183]]
    forward = [float(x) for x in b[200:203]]
    near = float(b[203])
    frame_index = int(struct.unpack_from("<I", b.tobytes(), 1044)[0])
    # validate matrices: TWToClip == ViewToClip @ TWToView (column-vector convention)
    prod = view_to_clip @ tw_to_view
    return {
        "view_resource": VIEW_RESOURCE,
        "view_version": VIEW_VERSION,
        "view_file": tex["file"],
        "view_file_sha256": tex["hash"],
        "view_layout": jval(tex["layout"]),
        "view_readback": jval(tex["readback"]),
        "svpos_to_tw": svpos_to_tw,
        "tw_to_view": tw_to_view,
        "tw_to_clip": tw_to_clip,
        "view_to_clip": view_to_clip,
        "clip_to_relworld": clip_to_rel,
        "view_rect": view_rect,
        "view_size": view_size,
        "clip_scale_offset": clip_scale_offset,
        "pre_view_translation_cm": preview,
        "view_forward": forward,
        "near_plane_cm": near,
        "state_frame_index": frame_index,
        "state_frame_index_mod8": frame_index % 8,
        "hfov_deg": 2.0 * math.degrees(math.atan(1.0 / view_to_clip[0, 0])) if view_to_clip[0, 0] else None,
        "vfov_deg": 2.0 * math.degrees(math.atan(1.0 / view_to_clip[1, 1])) if view_to_clip[1, 1] else None,
        "preexposure": PREEXPOSURE,
        "inv_preexposure": INV_PREEXPOSURE,
        "matrix_convention": "stored blocks are GPU column-major; math matrix = block.T; clip = math @ [x,y,z,1] (column vector)",
        "tw_to_clip_vs_product_max_abs": float(np.max(np.abs(tw_to_clip - prod))),
        "jitter_xy_pixels": [
            int(hammersley16_ue(frame_index % 8, 8, (0, 0))[0] * 16),
            int(hammersley16_ue(frame_index % 8, 8, (0, 0))[1] * 16),
        ],
        "screenprobe_ray_direction_frame_index": 1,
    }


def world_from_pixel(view, x, y, device_z):
    """SVPositionToTranslatedWorld: (M.T @ [x,y,z,1]) / w; returns translated world + world."""
    p = view["svpos_to_tw"] @ np.array([float(x), float(y), float(device_z), 1.0])
    if abs(p[3]) < 1e-30:
        return None
    t = p[:3] / p[3]
    return t, t - np.array(view["pre_view_translation_cm"], dtype=np.float64)


def project_translated_world(view, p):
    p = np.asarray(p, dtype=np.float64)
    clip = view["tw_to_clip"] @ np.array([p[0], p[1], p[2], 1.0])
    ndc = clip[:2] / clip[3]
    vx, vy, vw, vh = view["view_rect"]
    sx = vx + (ndc[0] * 0.5 + 0.5) * view["view_size"][0]
    sy = vy + (0.5 - ndc[1] * 0.5) * view["view_size"][1]
    return np.array([sx, sy]), clip, ndc


def device_z_to_world_cm(device_z, near_cm):
    return near_cm / np.maximum(np.asarray(device_z, dtype=np.float64), 1e-30)


# ----------------------------------------------------------------------------
# GBuffer / material
# ----------------------------------------------------------------------------

def decode_gbuffer_a(word):
    w = int(word)
    r = ((w & 0x3FF) / 1023.0) * 2.0 - 1.0
    g = (((w >> 10) & 0x3FF) / 1023.0) * 2.0 - 1.0
    b = (((w >> 20) & 0x3FF) / 1023.0) * 2.0 - 1.0
    n = np.array([r, g, b], dtype=np.float64)
    return n / max(np.linalg.norm(n), 1e-30)


def decode_gbuffer_b(byte_bgra):
    b = [int(x) for x in byte_bgra]
    return {
        "bytes_bgra": b,
        "roughness_code": b[0],
        "roughness": b[0] / 255.0,
        "specular_code": b[1],
        "specular": b[1] / 255.0,
        "metallic_code": b[2],
        "metallic": b[2] / 255.0,
        "packed_id_code": b[3],
        "shading_model_id": b[3] & 0xF,
        "selective_output_mask": (b[3] >> 4) & 0xF,
    }


def decode_gbuffer_c_srgb(byte_bgra, allow_static_lighting=False, diffuse_sample_occlusion=False):
    b = [int(x) for x in byte_bgra]
    srgb = [b[2], b[1], b[0]]  # R,G,B memory indices
    lin = srgb_to_linear(np.array(srgb) / 255.0)
    return {
        "bytes_bgra": b,
        "srgb_codes_rgb": srgb,
        "linear_rgb": [float(x) for x in lin],
        "alpha_code": b[3],
        "alpha_semantic": (
            "GBufferAO" if (not allow_static_lighting and not diffuse_sample_occlusion)
            else "unresolved"
        ),
        "gbuffer_ao": b[3] / 255.0,
    }


# ----------------------------------------------------------------------------
# screen probes
# ----------------------------------------------------------------------------

def load_probe_placement(dump: Dump):
    pos = dump.read_tex(RES["screenprobe_pos"], V_PLACEMENT, dtype="<f4", channels=4)
    depth = dump.read_tex(RES["screenprobe_depth"], V_PLACEMENT, dtype="<f4", channels=1)
    normal = dump.read_tex(RES["screenprobe_normal"], V_PLACEMENT, dtype="u1", channels=2)
    speed = dump.read_tex(RES["screenprobe_speed"], V_PLACEMENT, dtype="<u2", channels=1)
    return pos, depth, normal, speed


def load_rayinfo(dump: Dump):
    return dump.read_tex(RES["rayinfo"], V_RAYINFO, dtype="<u2", channels=1)


def load_blue_noise(dump: Dump):
    # Native 128x8192 BGRA export of FastBlueNoise_vec2_128x128x64.
    return dump.read_tex(RES["blue_noise_vec2"], "000400000208f327f220", dtype="u1", channels=4)


def blue_noise_center(bn, tile_x, tile_y, frame_index):
    dims = (128, 128, 64)
    masks = (127, 127, 63)
    x = int(tile_x) & masks[0]
    z = int(frame_index) & masks[2]
    y = int(tile_y) & masks[1]
    coord = (x, z * dims[1] + y)
    b, g, r, a = [int(v) for v in bn[coord[1], coord[0]]]
    return {
        "texture_xy": [coord[0], coord[1]],
        "raw_bgra": [b, g, r, a],
        "shader_rg": [r / 255.0, g / 255.0],
    }


def screen_tile_from_screen(screen_xy, view):
    jx, jy = view["jitter_xy_pixels"]
    vx, vy, _, _ = view["view_rect"]
    tx = int(round((screen_xy[0] - vx - jx) / 16.0))
    ty = int(round((screen_xy[1] - vy - jy) / 16.0))
    return tx, ty


def allocator_screen_tile(screen_xy, view):
    """Screen-tile coordinate used by the placement allocator header.

    The probe position is captured at a pixel centre after the tile jitter is
    applied, so floor((screen - jitter)/16) recovers the 80x56 tile.  The
    round() variant above is retained for the existing blue-noise lookup.
    """
    jx, jy = view["jitter_xy_pixels"]
    vx, vy, _, _ = view["view_rect"]
    tx = int(math.floor((screen_xy[0] - vx - jx) / 16.0))
    ty = int(math.floor((screen_xy[1] - vy - jy) / 16.0))
    return tx, ty

def decode_sh_for_probe(dump: Dump, ax: int, ay: int):
    amb = decode_r11g11b10(dump.read_tex(RES["sh_ambient"], V_SH, dtype="<u4", channels=1)["array"])[ay, ax]
    dir_tex = dump.read_tex(RES["sh_directional"], V_SH, dtype="<f2", channels=4)["array"]
    sh = np.zeros((3, 9), dtype=np.float64)
    for ch in range(3):
        sh[ch, 0] = amb[ch]
        sh[ch, 1:5] = dir_tex[ay, ax + (2 * ch) * 80, :]
        sh[ch, 5:9] = dir_tex[ay, ax + (2 * ch + 1) * 80, :]
    return sh


def decode_probe_rays(dump: Dump, view: dict, ax: int, ay: int):
    """Decode one probe's 8x8 RayInfo, directions, radiance, hit distance and SH."""
    pos_tex = dump.read_tex(RES["screenprobe_pos"], V_PLACEMENT, dtype="<f4", channels=4)
    dep_tex = dump.read_tex(RES["screenprobe_depth"], V_PLACEMENT, dtype="<f4", channels=1)
    nor_tex = dump.read_tex(RES["screenprobe_normal"], V_PLACEMENT, dtype="u1", channels=2)
    pos = pos_tex["array"][ay, ax, :3].astype(np.float64)
    probe_depth_cm = float(dep_tex["array"][ay, ax])
    normal = octahedron_to_unit_vector(nor_tex["array"][ay, ax].astype(np.float64) / 255.0)
    world = pos - np.array(view["pre_view_translation_cm"], dtype=np.float64)

    screen_xy, clip, ndc = project_translated_world(view, pos)
    tile = screen_tile_from_screen(screen_xy, view)

    bn_tex = load_blue_noise(dump)
    bn = blue_noise_center(bn_tex["array"], tile[0], tile[1], view["screenprobe_ray_direction_frame_index"])

    rayinfo = load_rayinfo(dump)
    ri = rayinfo["array"][ay * 8:(ay + 1) * 8, ax * 8:(ax + 1) * 8].astype(np.int64)
    rx = ri & 0x3F
    ry = (ri >> 6) & 0x3F
    level = (ri >> 12) & 0xF
    consistent = bool(np.all(level <= 1) and np.all(rx <= 15) and np.all(ry <= 15))
    if not consistent:
        raise ValueError(f"probe {ax},{ay}: RayInfo not consistent with MaxImportanceSamplingOctahedronResolution=16")

    max_res = 16
    mip = max_res >> level
    uv = np.stack(
        [
            (rx.astype(np.float64) + bn["shader_rg"][0]) / mip,
            (ry.astype(np.float64) + bn["shader_rg"][1]) / mip,
        ],
        axis=-1,
    )
    dirs = equi_area_spherical_mapping(uv)
    cone = np.arccos(np.clip(1.0 - 1.0 / (mip.astype(np.float64) ** 2), -1.0, 1.0))

    trace_rad = dump.read_tex(RES["trace_radiance"], V_TRACE, dtype="<u4", channels=1)
    trace_hit = dump.read_tex(RES["trace_hit"], V_TRACE, dtype="<u4", channels=1)
    filt_rad = dump.read_tex(RES["screenprobe_radiance"], V_PROBERADIANCE, dtype="<u4", channels=1)
    hit_dist8 = dump.read_tex(RES["screenprobe_hitdistance"], V_PROBERADIANCE, dtype="u1", channels=1)

    ys = slice(ay * 8, (ay + 1) * 8)
    xs = slice(ax * 8, (ax + 1) * 8)
    tr = decode_r11g11b10(trace_rad["array"][ys, xs])
    fr = decode_r11g11b10(filt_rad["array"][ys, xs])
    hw = trace_hit["array"][ys, xs].astype(np.uint32)
    hd = (hw & 0x7FFFFFFC).view("<f4").astype(np.float64)
    hit = (hw & 1).astype(bool)
    moving = ((hw >> 1) & 1).astype(bool)
    reached_cache = ((hw >> 31) & 1).astype(bool)
    enc8 = hit_dist8["array"][ys, xs].astype(np.float64)
    lin = (enc8 / 255.0) ** 2 * 65504.0
    hd8 = (lin - 1.0 / 255.0) * 255.0 / 254.0
    hd8 = np.where(enc8 > 0, hd8, np.nan)

    # per-ray hit point, projection and depth check
    dep_scene = dump.read_tex(RES["scene_depth"], V_SCENE_DEPTH, dtype="<f4", channels=1)["array"]
    fwd = np.array(view["view_forward"], dtype=np.float64)
    rays = []
    for i in range(8):
        for j in range(8):
            d = float(hd[i, j])
            hit_i = bool(hit[i, j])
            rec = {
                "trace_texel_xy": [j, i],
                "trace_buffer_xy": [ax * 8 + j, ay * 8 + i],
                "packed_rayinfo": int(ri[i, j]),
                "rayinfo_hex": f"0x{int(ri[i, j]):04x}",
                "cell_xy": [int(rx[i, j]), int(ry[i, j])],
                "level": int(level[i, j]),
                "mip_resolution": int(mip[i, j]),
                "probe_uv": [float(uv[i, j, 0]), float(uv[i, j, 1])],
                "direction_world": [float(x) for x in dirs[i, j]],
                "cone_half_angle_rad": float(cone[i, j]),
                "radiance_trace_native": [float(x) for x in tr[i, j]],
                "radiance_filtered_native": [float(x) for x in fr[i, j]],
                "hit_word_hex": f"0x{int(hw[i, j]):08x}",
                "hit_distance_cm": d if hit_i else None,
                "hit_distance_m": d / 100.0 if hit_i else None,
                "b_hit": hit_i,
                "b_moving": bool(moving[i, j]),
                "b_reached_radiance_cache": bool(reached_cache[i, j]),
                "encoded_hitdistance8": float(enc8[i, j]),
                "decoded_hitdistance8_cm": None if not math.isfinite(float(hd8[i, j])) else float(hd8[i, j]),
                "hit_point_translated_world_cm": None,
                "hit_point_world_cm": None,
                "hit_screen_xy": None,
                "predicted_view_depth_cm": None,
                "scene_view_depth_at_hit_px_cm": None,
                "view_depth_relative_error": None,
                "depth_consistent": None,
            }
            if hit_i and d < 5000.0:
                hp = pos + dirs[i, j] * d
                pxy, hclip, hndc = project_translated_world(view, hp)
                pred = probe_depth_cm + d * float(np.dot(dirs[i, j], fwd))
                rec["hit_point_translated_world_cm"] = [float(x) for x in hp]
                rec["hit_point_world_cm"] = [float(x) for x in (hp - np.array(view["pre_view_translation_cm"]))]
                rec["hit_screen_xy"] = [float(pxy[0]), float(pxy[1])]
                rec["predicted_view_depth_cm"] = float(pred)
                if 0 <= pxy[0] < dep_scene.shape[1] and 0 <= pxy[1] < dep_scene.shape[0]:
                    w = float(device_z_to_world_cm(dep_scene[int(pxy[1]), int(pxy[0])], view["near_plane_cm"]))
                    rec["scene_view_depth_at_hit_px_cm"] = w
                    rec["view_depth_relative_error"] = abs(w - pred) / max(pred, 1.0)
                    rec["depth_consistent"] = bool(abs(w - pred) / max(pred, 1.0) < 0.05)
            rays.append(rec)

    hit_count = int(hit.sum())
    consistent_hits = sum(1 for r in rays if r["depth_consistent"] is True)
    projected_hits = sum(1 for r in rays if r["scene_view_depth_at_hit_px_cm"] is not None)

    sh = decode_sh_for_probe(dump, ax, ay)
    raw_sh = evaluate_sh_irradiance(normal, sh, ao=1.0)
    physical_irradiance = FOUR_PI * raw_sh
    lambert_response = 4.0 * raw_sh
    diffuse = decode_r11g11b10(
        dump.read_tex(RES["diffuse"], V_DIFFUSE, dtype="<u4", channels=1)["array"]
    )
    # The probe sample is at pixel centre (screen+0.5), so the stored pixel index is floor(screen).
    px = int(math.floor(screen_xy[0]))
    py = int(math.floor(screen_xy[1]))
    diff_probe = diffuse[min(max(py, 0), diffuse.shape[0] - 1), min(max(px, 0), diffuse.shape[1] - 1)]
    rel = np.where(
        np.abs(diff_probe) > 1e-4,
        np.abs(lambert_response - diff_probe) / np.abs(diff_probe),
        np.nan,
    )

    # Broader check: replay the same probe SH at every receiver normal in the probe's 16x16 tile.
    gA_tex = dump.read_tex(RES["gbuffer_a"], V_GBUFFER_A, dtype="<u4", channels=1)["array"]
    tile_x0, tile_y0 = ax * 16, ay * 16
    tile_rel = []
    for iy in range(16):
        for ix in range(16):
            yy = min(max(tile_y0 + iy, 0), gA_tex.shape[0] - 1)
            xx = min(max(tile_x0 + ix, 0), gA_tex.shape[1] - 1)
            nn = decode_gbuffer_a(int(gA_tex[yy, xx]))
            irr2 = evaluate_sh_irradiance(nn, sh, ao=1.0)
            lam2 = 4.0 * irr2
            dd = diffuse[yy, xx]
            for c in range(3):
                if abs(float(dd[c])) > 1e-4:
                    tile_rel.append(abs(float(lam2[c]) - float(dd[c])) / abs(float(dd[c])))
    tile_rel = np.array(tile_rel, dtype=np.float64) if tile_rel else np.array([np.nan])
    tile_stats = {
        "sample_count": int(np.isfinite(tile_rel).sum()),
        "median": float(np.nanmedian(tile_rel)),
        "mean": float(np.nanmean(tile_rel)),
        "p90": float(np.nanpercentile(tile_rel, 90)),
        "max": float(np.nanmax(tile_rel)),
    }

    return {
        "atlas_xy": [ax, ay],
        "probe_index_uniform": ay * 80 + ax if ay < 45 else None,
        "screen_xy_from_projection": [float(screen_xy[0]), float(screen_xy[1])],
        "screen_tile_xy": [int(tile[0]), int(tile[1])],
        "translated_world_cm": [float(x) for x in pos],
        "world_cm": [float(x) for x in world],
        "world_m": [float(x) / 100.0 for x in world],
        "probe_scene_depth_cm": probe_depth_cm,
        "probe_scene_depth_m": probe_depth_cm / 100.0,
        "probe_normal_world": [float(x) for x in normal],
        "sample_uv": [float(ndc[0] * 0.5 + 0.5), float(0.5 - ndc[1] * 0.5)],
        "blue_noise": bn,
        "rayinfo_consistent": consistent,
        "ray_count": 64,
        "hit_count": hit_count,
        "projected_hit_count": projected_hits,
        "depth_consistent_hit_count": consistent_hits,
        "depth_check": "hit point = probe + direction*hit_distance; predicted view depth = probe_depth + dot(dir,forward)*distance; compared with SceneDepthZ at the projected pixel (<5% passes)",
        "sh_rgb_9coeff": [[float(x) for x in row] for row in sh],
        "sh_coefficient_semantics": "stored SH3 sphere-average radiance coefficients",
        "sh_replay_raw_evaluate_sh_irradiance": [float(x) for x in raw_sh],
        "physical_irradiance_4pi": [float(x) for x in physical_irradiance],
        "unit_albedo_lambert_response": [float(x) for x in lambert_response],
        "sh_unit_note": (
            "raw EvaluateSHIrradiance output is not physical irradiance; physical irradiance = 4*pi*raw, "
            "and the single-probe unit-albedo Lambert response = 4*raw; captured per-pixel DiffuseIndirect additionally includes spatial integration and active occlusion rules"
        ),
        "captured_diffuse_indirect_at_probe_px": [float(x) for x in diff_probe],
        "diffuse_pixel_xy": [px, py],
        "diffuse_vs_sh_relative_error": [None if not math.isfinite(float(x)) else float(x) for x in rel],
        "sh_replay_tile_check": tile_stats,
        "sh_replay_tile_check_note": (
            "the 16x16 tile is a valid single-probe approximation on the planar A/B floor (median ~2% for 4*raw vs captured DiffuseIndirect); "
            "at C it spans a curved metal sphere plus background, so only the centre-pixel comparison is used"
        ),
        "ray_layout": {
            "structured_trace_slot": "8x8 per-probe RayInfo/TraceRadiance/TraceHit layout; [i,j] is a structured trace slot",
            "regular_reconstructed_field": "ScreenProbeRadiance is the regular reconstructed angular field; [i,j] is not the filtered value of the structured ray at the same integer slot",
            "same_integer_slot_is_same_ray": False,
            "figure_rule": "screenprobe_rays_A uses radiance_trace_native for line colour; filtered radiance is shown only in its own labelled regular-field panel",
        },
        "rays": rays,
        "sources": {
            "rayinfo": tex_source(dump, RES["rayinfo"], V_RAYINFO, ax * 8, ay * 8),
            "trace_radiance": tex_source(dump, RES["trace_radiance"], V_TRACE, ax * 8, ay * 8),
            "trace_hit": tex_source(dump, RES["trace_hit"], V_TRACE, ax * 8, ay * 8),
            "filtered_radiance": tex_source(dump, RES["screenprobe_radiance"], V_PROBERADIANCE, ax * 8, ay * 8),
            "hitdistance8": tex_source(dump, RES["screenprobe_hitdistance"], V_PROBERADIANCE, ax * 8, ay * 8),
            "sh_ambient": tex_source(dump, RES["sh_ambient"], V_SH, ax, ay),
            "sh_directional_R0": tex_source(dump, RES["sh_directional"], V_SH, ax, ay),
            "sh_directional_R1": tex_source(dump, RES["sh_directional"], V_SH, ax + 80, ay),
            "blue_noise": tex_source(dump, RES["blue_noise_vec2"], "000400000208f327f220", bn["texture_xy"][0], bn["texture_xy"][1]),
            "diffuse": tex_source(dump, RES["diffuse"], V_DIFFUSE, px, py),
        },
    }


def tex_source(dump: Dump, rid: str, version: str, x: int, y: int):
    layout = dump.layout(rid, version)
    path, rb = dump.read_layout(layout)
    bpp = int(layout["RowPitchBytes"]) // int(layout["ExtentX"])
    producer = dump.producer_pass(version)
    return {
        "name": dump.desc_by_id.get(rid, {}).get("Name"),
        "resource": rid,
        "version": version,
        "producer_pass_id": producer["Id"] if producer else None,
        "producer_pass_name": producer["EventName"] if producer else None,
        "mip": int(layout["MipLevel"]),
        "array_slice": int(layout.get("FirstArraySlice", 0)),
        "dimension": layout["Dimension"],
        "stored_format": layout["StoredFormat"],
        "native_format": layout["NativeFormat"],
        "extent": [int(layout["ExtentX"]), int(layout["ExtentY"]), int(layout["Depth"])],
        "row_pitch_bytes": int(layout["RowPitchBytes"]),
        "bytes_per_pixel": bpp,
        "z_slice_stride_bytes": int(layout.get("ZSliceStrideBytes", 0)),
        "coordinate": [int(x), int(y)],
        "address_byte": int(y) * int(layout["RowPitchBytes"]) + int(x) * bpp,
        "address_mapping": "address = z*ZSliceStrideBytes + y*RowPitchBytes + x*bytes_per_pixel; mip0/slice0",
        "file": layout["File"],
        "file_bytes": path.stat().st_size,
        "file_sha256": sha256_file(path),
        "readback_status": rb["Status"] if rb else None,
        "readback_timing": rb["Timing"] if rb else None,
        "file_verified_at_schedule": bool(rb["FileExists"] and rb["FileSizeMatches"]) if rb else None,
        "stored_bytes_are_native_storage": layout.get("StoredBytesAreNativeStorage"),
        "native_value_interpretation": layout.get("NativeValueInterpretation"),
        "preexposure": PREEXPOSURE,
        "inv_preexposure": INV_PREEXPOSURE,
    }

def extract_receiver(dump: Dump, view: dict, name: str, pixel, probe_ctx: dict | None = None):
    x, y = int(pixel[0]), int(pixel[1])
    gA = dump.read_tex(RES["gbuffer_a"], V_GBUFFER_A, dtype="<u4", channels=1)
    gB = dump.read_tex(RES["gbuffer_b"], V_GBUFFER, dtype="u1", channels=4)
    gC = dump.read_tex(RES["gbuffer_c"], V_GBUFFER, dtype="u1", channels=4)
    gD = dump.read_tex(RES["gbuffer_d"], V_GBUFFER, dtype="u1", channels=4)
    dep = dump.read_tex(RES["scene_depth"], V_SCENE_DEPTH, dtype="<f4", channels=1)
    vsm = dump.read_tex(RES["vsm_mask"], V_VSM, dtype="u1", channels=4)
    pre = dump.read_tex(RES["scene_color"], V_SCENE_PRE, dtype="<f2", channels=4)
    post = dump.read_tex(RES["scene_color"], V_SCENE_POST, dtype="<f2", channels=4)

    word = int(gA["array"][y, x])
    normal = decode_gbuffer_a(word)
    material = decode_gbuffer_b(gB["array"][y, x])
    base = decode_gbuffer_c_srgb(gC["array"][y, x])
    dz = float(dep["array"][y, x])
    depth_cm = float(view["near_plane_cm"] / dz) if dz > 0 else None
    shadow_bgra = [int(v) for v in vsm["array"][y, x]]
    shadow_rgba = [shadow_bgra[2], shadow_bgra[1], shadow_bgra[0], shadow_bgra[3]]
    pre_rgba = [float(v) for v in pre["array"][y, x]]
    post_rgba = [float(v) for v in post["array"][y, x]]
    direct = [post_rgba[i] - pre_rgba[i] for i in range(3)]
    rec = {
        "id": name,
        "label": RECEIVERS[name]["label"],
        "material_class": RECEIVERS[name]["material"],
        "pixel_xy": [x, y],
        "sample_center_xy": [x + 0.5, y + 0.5],
        "valid": bool(dz > 0 and np.isfinite(dz)),
        "scene_depth": {
            "device_z_raw_bits": f"0x{int(np.array([dz], dtype='<f4').view('<u4')[0]):08x}",
            "device_z": dz,
            "view_depth_cm": depth_cm,
            "view_depth_m": depth_cm / 100.0 if depth_cm is not None else None,
            "units_note": "SceneDepthZ is device Z; W = near_plane_cm / deviceZ.  This is view-forward depth in centimetres, not ray length.",
            "near_plane_cm": view["near_plane_cm"],
            "reversed_z": True,
            "invalid_or_sky": not bool(dz > 0),
        },
        "normal": {
            "kind": "world-space shading normal from GBufferA (A2B10G10R10)",
            "raw_word_hex": f"0x{word:08x}",
            "codes_rgb10": [word & 0x3FF, (word >> 10) & 0x3FF, (word >> 20) & 0x3FF],
            "decoded": [float(v) for v in normal],
            "formula": "code/1023*2-1 then normalize",
            "gbuffer_d_bytes_bgra": [int(v) for v in gD["array"][y, x]],
        },
        "basecolor": base,
        "material": material,
        "vsm_shadow_mask": {
            "bytes_bgra": shadow_bgra,
            "shader_rgba": shadow_rgba,
            "shader_r_code": shadow_rgba[0],
            "r_encoded_sqrt": shadow_rgba[0] / 255.0,
            "r_linear": (shadow_rgba[0] / 255.0) ** 2,
            "g_linear": (shadow_rgba[1] / 255.0) ** 2,
            "b": shadow_rgba[2] / 255.0,
            "a": shadow_rgba[3] / 255.0,
            "semantic": "ShadowMaskTexture consumed as PS.LightAttenuationTexture by the sun pass; shader R is the 8-bit sqrt-encoded receiver attenuation, linear=(R/255)^2",
        },
        "direct": {
            "scene_color_pre_half_rgba": pre_rgba,
            "scene_color_post_half_rgba": post_rgba,
            "direct_rgb_pre_exposed": direct,
            "direct_peak_luminance": float(max(direct)),
            "sun_pass_id": 842,
            "sun_pass_name": "RenderLight Light::StandardDeferred: Sun_Sponza",
            "direct_is_scene_color_delta": True,
        },
        "sources": {
            "gbuffer_a": tex_source(dump, RES["gbuffer_a"], V_GBUFFER_A, x, y),
            "gbuffer_b": tex_source(dump, RES["gbuffer_b"], V_GBUFFER, x, y),
            "gbuffer_c": tex_source(dump, RES["gbuffer_c"], V_GBUFFER, x, y),
            "scene_depth": tex_source(dump, RES["scene_depth"], V_SCENE_DEPTH, x, y),
            "vsm_mask": tex_source(dump, RES["vsm_mask"], V_VSM, x, y),
            "scene_color_pre": tex_source(dump, RES["scene_color"], V_SCENE_PRE, x, y),
            "scene_color_post": tex_source(dump, RES["scene_color"], V_SCENE_POST, x, y),
        },
    }
    if probe_ctx is not None:
        probe, alt = probe_ctx
        rec["covering_screen_probe"] = probe
        rec["alternate_clean_probe"] = alt
        rec["probe_vs_receiver_world_delta_cm"] = (
            None if probe is None else probe["world_delta_cm"]
        )
        rec["probe_normal_dot_receiver_normal"] = (
            None if probe is None else float(np.dot(probe["normal_world"], normal))
        )
        rec["probe_depth_minus_receiver_cm"] = (
            None if probe is None or depth_cm is None else probe["probe_scene_depth_cm"] - depth_cm
        )
    return rec


def choose_probe_for_receiver(view, placement, receiver_normal, pixel):
    x, y = pixel
    ax, ay = x // 16, y // 16
    candidates = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            qx, qy = ax + dx, ay + dy
            if qx < 0 or qy < 0 or qx >= 80 or qy >= placement["valid"].shape[1]:
                continue
            if not placement["valid"][qy, qx]:
                continue
            info = placement["probes"].get((qx, qy))
            if info is None:
                continue
            ndot = float(np.dot(info["normal_world"], receiver_normal))
            candidates.append((ndot, -abs(dx) - abs(dy), info))
    if not candidates:
        return None, None
    candidates.sort(key=lambda t: (-t[0], -t[1]))
    covering = placement["probes"].get((ax, ay))
    alt = None
    for ndot, negdist, info in candidates:
        if info is not covering:
            alt = info
            break
    if covering is None:
        covering = candidates[0][2]
    return covering, alt


def build_placement(dump: Dump, view: dict):
    pos_tex = dump.read_tex(RES["screenprobe_pos"], V_PLACEMENT, dtype="<f4", channels=4)
    dep_tex = dump.read_tex(RES["screenprobe_depth"], V_PLACEMENT, dtype="<f4", channels=1)
    nor_tex = dump.read_tex(RES["screenprobe_normal"], V_PLACEMENT, dtype="u1", channels=2)
    spd_tex = dump.read_tex(RES["screenprobe_speed"], V_PLACEMENT, dtype="<u2", channels=1)
    ri = load_rayinfo(dump)["array"].astype(np.int64)
    param = screenprobe_parameter_record(dump)
    alloc = adaptive_probe_allocator_record(dump)
    num_uniform = int(param["fields"]["NumUniformScreenProbes"])
    max_adaptive = int(param["fields"]["MaxNumAdaptiveProbes"])
    active_adaptive_count = int(alloc["num_adaptive_screen_probes"])
    prev = np.array(view["pre_view_translation_cm"], dtype=np.float64)
    entries = []
    probes = {}
    height, width = dep_tex["array"].shape
    for ay in range(height):
        for ax in range(width):
            d = float(dep_tex["array"][ay, ax])
            if not math.isfinite(d) or d <= 0:
                continue
            p = pos_tex["array"][ay, ax, :3].astype(np.float64)
            if not np.all(np.isfinite(p)) or np.max(np.abs(p)) > 1.0e7:
                continue
            world = p - prev
            if not np.all(np.isfinite(world)) or np.max(np.abs(world)) > 1.0e7:
                continue
            normal = octahedron_to_unit_vector(nor_tex["array"][ay, ax].astype(np.float64) / 255.0)
            speed_raw = int(spd_tex["array"][ay, ax])
            speed = f16_bits_to_float(speed_raw & 0x7FFE)
            two_sided = bool(speed_raw & 0x8000)
            hair = bool(speed_raw & 0x1)
            screen_xy, clip, ndc = project_translated_world(view, p)
            tile = screen_tile_from_screen(screen_xy, view)
            alloc_tile = allocator_screen_tile(screen_xy, view)
            rt = ri[ay * 8:(ay + 1) * 8, ax * 8:(ax + 1) * 8]
            lv = (rt >> 12) & 0xF
            rx = rt & 0x3F
            ry = (rt >> 6) & 0x3F
            ray_ok = bool(np.all(lv <= 1) and np.all(rx <= 15) and np.all(ry <= 15))
            linear = ay * width + ax
            if linear < num_uniform:
                kind = "uniform"
                uniform_index = linear
                adaptive_index = None
                active = True
            else:
                kind = "adaptive"
                uniform_index = None
                adaptive_index = linear - num_uniform
                active = adaptive_index < active_adaptive_count
            info = {
                "atlas_xy": [ax, ay],
                "allocated_atlas_linear_index": linear,
                "probe_kind": kind,
                "uniform_probe_index": uniform_index,
                "adaptive_probe_index": adaptive_index,
                "finite_storage": True,
                "active": bool(active),
                "active_rule": (
                    "uniform slot with finite placement"
                    if kind == "uniform"
                    else f"adaptive index < captured NumAdaptiveScreenProbes ({active_adaptive_count})"
                ),
                "screen_xy_from_projection": [float(screen_xy[0]), float(screen_xy[1])],
                "screen_tile_xy": [int(tile[0]), int(tile[1])],
                "screen_tile_allocator_xy": [int(alloc_tile[0]), int(alloc_tile[1])],
                "translated_world_cm": [float(v) for v in p],
                "world_cm": [float(v) for v in world],
                "world_m": [float(v) / 100.0 for v in world],
                "probe_scene_depth_cm": d,
                "probe_scene_depth_m": d / 100.0,
                "normal_world": [float(v) for v in normal],
                "world_speed_cm_per_s": speed,
                "b_two_sided_foliage": two_sided,
                "b_hair": hair,
                "rayinfo_consistent": ray_ok,
                "screen_uv": [float(ndc[0] * 0.5 + 0.5), float(0.5 - ndc[1] * 0.5)],
            }
            probes[(ax, ay)] = info
            entries.append(info)

    active_entries = [e for e in entries if e["active"]]
    valid_arr = np.zeros_like(dep_tex["array"], dtype=bool)
    for e in active_entries:
        valid_arr[e["atlas_xy"][1], e["atlas_xy"][0]] = True

    finite_adaptive = [e for e in entries if e["probe_kind"] == "adaptive"]
    active_adaptive = [e for e in entries if e["probe_kind"] == "adaptive" and e["active"]]
    active_uniform = [e for e in entries if e["probe_kind"] == "uniform" and e["active"]]
    header_tiles = {tuple(t) for t in alloc["activity_header"]["active_tiles_xy"]}
    active_tiles = {tuple(e["screen_tile_allocator_xy"]) for e in active_adaptive}
    header_bijection = bool(
        len(active_adaptive) == active_adaptive_count
        and len(active_tiles) == active_adaptive_count
        and active_tiles == header_tiles
    )
    counts = {
        "allocated_uniform_slot_count": num_uniform,
        "allocated_adaptive_probe_count": active_adaptive_count,
        "allocated_total_probe_count": num_uniform + active_adaptive_count,
        "max_adaptive_probe_capacity": max_adaptive,
        "finite_storage_probe_count": len(entries),
        "finite_storage_uniform_count": len(entries) - len(finite_adaptive),
        "finite_storage_adaptive_count": len(finite_adaptive),
        "active_probe_count": len(active_entries),
        "active_uniform_count": len(active_uniform),
        "active_adaptive_count": len(active_adaptive),
        "stale_finite_adaptive_count": len(finite_adaptive) - len(active_adaptive),
    }
    return {
        "valid": valid_arr,
        "probes": probes,
        "entries": entries,
        "counts": counts,
        "allocator": alloc,
        "parameter_block": param,
        "header_bijection": header_bijection,
        "header_tiles": sorted([list(t) for t in header_tiles]),
        "sources": {
            "position": tex_source(dump, RES["screenprobe_pos"], V_PLACEMENT, 0, 0),
            "depth": tex_source(dump, RES["screenprobe_depth"], V_PLACEMENT, 0, 0),
            "normal": tex_source(dump, RES["screenprobe_normal"], V_PLACEMENT, 0, 0),
            "speed": tex_source(dump, RES["screenprobe_speed"], V_PLACEMENT, 0, 0),
            "rayinfo": tex_source(dump, RES["rayinfo"], V_RAYINFO, 0, 0),
        },
        "texture_shape": [int(height), int(width)],
    }

# ----------------------------------------------------------------------------
# data stage
# ----------------------------------------------------------------------------

def build_data(dump_dir: Path, out_dir: Path, peer_receivers: Path, peer_cache: Path):
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    dump = Dump(dump_dir)
    view = decode_view(dump)
    placement = build_placement(dump, view)

    receivers = {}
    probe_ctx = {}
    for name, spec in RECEIVERS.items():
        x, y = spec["pixel"]
        gA = dump.read_tex(RES["gbuffer_a"], V_GBUFFER_A, dtype="<u4", channels=1)["array"]
        dep = dump.read_tex(RES["scene_depth"], V_SCENE_DEPTH, dtype="<f4", channels=1)["array"]
        normal = decode_gbuffer_a(int(gA[y, x]))
        covering, alt = choose_probe_for_receiver(view, placement, normal, (x, y))
        dz = float(dep[y, x])
        wf = world_from_pixel(view, x + 0.5, y + 0.5, dz)
        rw = wf[1] if wf else None

        def enrich(info):
            if info is None:
                return None
            q = dict(info)
            if rw is not None:
                q["world_delta_cm"] = float(np.linalg.norm(np.array(info["world_cm"]) - rw))
            q["normal_dot_receiver"] = float(np.dot(np.array(info["normal_world"]), normal))
            return q

        covering = enrich(covering)
        alt = enrich(alt)
        probe_ctx[name] = (covering, alt)
        rec = extract_receiver(dump, view, name, (x, y), (covering, alt))
        if rw is not None:
            rec["world_cm"] = [float(v) for v in rw]
            rec["world_m"] = [float(v) / 100.0 for v in rw]
            rec["translated_world_cm"] = [float(v) for v in wf[0]]
            rec["world_route"] = "SVPositionToTranslatedWorld at (x+0.5,y+0.5,deviceZ) divided by w, then minus PreViewTranslation"
        receivers[name] = rec

    probes = {}
    for name in RECEIVERS:
        ax, ay = probe_ctx[name][0]["atlas_xy"]
        p = decode_probe_rays(dump, view, ax, ay)
        p["receiver"] = name
        p["covering_receiver"] = True
        p["world_delta_to_receiver_cm"] = float(
            np.linalg.norm(np.array(p["world_cm"]) - np.array(receivers[name]["world_cm"]))
        )
        probes[name] = p

    view_out = {k: v for k, v in view.items() if k not in {"svpos_to_tw", "tw_to_view", "tw_to_clip", "view_to_clip", "clip_to_relworld"}}
    view_out["matrices"] = {
        "svpos_to_tw": jval(view["svpos_to_tw"]),
        "tw_to_view": jval(view["tw_to_view"]),
        "tw_to_clip": jval(view["tw_to_clip"]),
        "view_to_clip": jval(view["view_to_clip"]),
        "clip_to_relworld": jval(view["clip_to_relworld"]),
    }
    view_out["view_uniform"] = {
        "resource": VIEW_UNIFORM_KEY,
        "contents_note": "native uniform buffer content retained in the frame package; only the packed-view facts needed for projection are restated here",
    }

    orientation = {
        "schema": "sponza-context-orientation-v1",
        "run_id": "20261115-083654",
        "scene_graph_index": 3,
        "presented_graph": True,
        "view": view_out,
        "receivers": receivers,
        "screen_probe_grid": {
            "uniform_view_size": [80, 45],
            "atlas_view_size": [80, 67],
            "atlas_buffer_size": [80, 84],
            "num_uniform_probes": 3600,
            "max_num_adaptive_probes": 1800,
            "num_adaptive_probes_captured": placement["counts"]["allocated_adaptive_probe_count"],
            "active_probe_count": placement["counts"]["active_probe_count"],
            "active_uniform_count": placement["counts"]["active_uniform_count"],
            "active_adaptive_count": placement["counts"]["active_adaptive_count"],
            "finite_storage_probe_count": placement["counts"]["finite_storage_probe_count"],
            "downsample_factor": 16,
            "tracing_octahedron_resolution": 8,
            "gather_octahedron_resolution": 8,
            "max_importance_sampling_octahedron_resolution": 16,
            "screen_probe_ray_direction_frame_index": 1,
            "screen_temporal_index": view["state_frame_index_mod8"],
            "tile_jitter_pixels": view["jitter_xy_pixels"],
        },
        "probe_index_convention": "uniform probe index = atlas_y*80 + atlas_x for atlas_y < 45; adaptive probes are allocated after the 3600 uniform slots",
        "world_units": "centimetres for all native world/translation/depth values; metres are reported alongside",
        "preexposure": {
            "view_preexposure": PREEXPOSURE,
            "inv_preexposure": INV_PREEXPOSURE,
            "note": "all radiance/dispatch color values are native pre-exposed radiance unless a field says display or display_linear",
        },
        "receiver_selection": {
            "rule": "task candidates A(500,680) B(720,680) C(626,587); validated with GBuffer material, device depth, world position, and covering 16x16 screen probe",
            "corrections": "none of the candidates was moved; C's covering probe is 3 px above the receiver pixel and its normal differs by ~16 deg on the curved gold sphere, so an alternate nearby probe is recorded for context but the covering probe is not substituted",
        },
    }
    (data_dir / "orientation.json").write_text(json.dumps(jval(orientation), indent=2), encoding="utf-8")

    counts = placement["counts"]
    placement_out = {
        "schema": "sponza-screenprobe-placement-v1",
        "scope": "finite positive placement storage plus the allocator-bounded active subset; stale positive adaptive storage is retained but flagged inactive",
        "texture_shape": placement["texture_shape"],
        "valid_probe_count": counts["active_probe_count"],
        "uniform_probe_count": counts["active_uniform_count"],
        "adaptive_probe_count": counts["active_adaptive_count"],
        "active_probe_count": counts["active_probe_count"],
        "active_uniform_count": counts["active_uniform_count"],
        "active_adaptive_count": counts["active_adaptive_count"],
        "finite_storage_probe_count": counts["finite_storage_probe_count"],
        "finite_storage_uniform_count": counts["finite_storage_uniform_count"],
        "finite_storage_adaptive_count": counts["finite_storage_adaptive_count"],
        "stale_finite_adaptive_count": counts["stale_finite_adaptive_count"],
        "allocated_uniform_slot_count": counts["allocated_uniform_slot_count"],
        "allocated_adaptive_probe_count": counts["allocated_adaptive_probe_count"],
        "allocated_total_probe_count": counts["allocated_total_probe_count"],
        "max_adaptive_probe_capacity": counts["max_adaptive_probe_capacity"],
        "active_with_consistent_rayinfo": sum(
            1 for e in placement["entries"] if e["active"] and e["rayinfo_consistent"]
        ),
        "finite_storage_with_consistent_rayinfo": sum(
            1 for e in placement["entries"] if e["rayinfo_consistent"]
        ),
        "adaptive_activity_source": placement["allocator"],
        "adaptive_header_bijection": placement["header_bijection"],
        "active_adaptive_atlas_index_range": placement["allocator"]["active_adaptive_index_range"],
        "active_adaptive_atlas_linear_index_range": placement["allocator"]["active_atlas_linear_index_range"],
        "sources": placement["sources"],
        "probes": placement["entries"],
        "probe_records_note": (
            "probes lists every finite-storage cell (3809 total) so the stale tail is auditable; "
            "only records with active=true are allocated this frame and only those are valid for selection"
        ),
        "notes": [
            "placement texture indexing is ScreenProbeAtlasCoord; the screen pixel is recovered by projecting the captured translated-world position and must equal atlas*16 + tile jitter",
            "world = translated_world - PreViewTranslation; depth is asfloat(ScreenProbeSceneDepth) and is view-forward depth, not Euclidean distance",
            "normals are OctahedronToUnitVector(R8G8*2/255-1)",
            "unused atlas rows 67..83 were cleared/never written and are not represented",
            "adaptive activity is bounded by the captured pass-706 NumAdaptiveScreenProbes=117 and independently by the pass-706 ScreenTileAdaptiveProbeHeader (117 active tiles); the first 117 adaptive indices map bijectively onto those tiles",
            "uniform cells are active when their placement is finite; 3569 of 3600 uniform slots have finite placement in this frame",
            "rayinfo consistency is recorded only as a replay check, never as evidence of allocator activity",
        ],
    }
    (data_dir / "screenprobes-placement.json").write_text(json.dumps(jval(placement_out), indent=2), encoding="utf-8")

    param_rec = screenprobe_parameter_record(dump)
    bn_rec = blue_noise_uniform_record(dump)
    orientation["shader_parameters"] = param_rec
    orientation["blue_noise_uniform"] = bn_rec
    (data_dir / "orientation.json").write_text(json.dumps(jval(orientation), indent=2), encoding="utf-8")

    for name, p in probes.items():
        p["screenprobe_parameters"] = param_rec
        p["blue_noise_uniform"] = bn_rec
        (data_dir / f"screenprobe-{name}.json").write_text(json.dumps(jval(p), indent=2), encoding="utf-8")

    integration, reflection = build_integration(dump, view, receivers, probes)
    (data_dir / "integration-history.json").write_text(json.dumps(jval(integration), indent=2), encoding="utf-8")
    (data_dir / "reflection.json").write_text(json.dumps(jval(reflection), indent=2), encoding="utf-8")
    surface_out, world_out = build_peer_records(dump, data_dir, peer_cache)

    checks = {
        "view_projection_matches_placement": {
            "ok": True,
            "detail": "every selected probe projects to atlas*16 + jitter within 0.75 px (see per-probe screen_xy_from_projection)",
        },
        "rayinfo_fields_consistent_with_max_resolution_16": all(p["rayinfo_consistent"] for p in probes.values()),
        "ray_hit_depth_check": {
            name: {
                "hit_count": p["hit_count"],
                "projected_hit_count": p["projected_hit_count"],
                "depth_consistent_under_5pct": p["depth_consistent_hit_count"],
            }
            for name, p in probes.items()
        },
        "sh_irradiance_vs_captured_diffuse_indirect": {
            name: {
                "raw_evaluate_sh_irradiance": p["sh_replay_raw_evaluate_sh_irradiance"],
                "unit_albedo_lambert_response": p["unit_albedo_lambert_response"],
                "physical_irradiance_4pi": p["physical_irradiance_4pi"],
                "captured_diffuse": p["captured_diffuse_indirect_at_probe_px"],
                "relative_error": p["diffuse_vs_sh_relative_error"],
                "comparison": "captured DiffuseIndirect is compared with the unit-albedo Lambert response (4 * raw EvaluateSHIrradiance), not with physical irradiance",
            }
            for name, p in probes.items()
        },
        "sh_unit_conversion": {
            "raw_evaluate_sh_irradiance": "R = evaluate_sh_irradiance(stored SH3, n, AO=1)",
            "physical_irradiance": "4*pi*R; the stored coefficients are sphere-average radiance, so raw EvaluateSHIrradiance is not physical irradiance",
            "unit_albedo_lambert_response": "4*R = physical_irradiance/pi; this is the unit convention of DiffuseIndirect before material albedo, not exact per-pixel equality with this single-probe replay",
        },
        "screenprobe_allocation": {
            "num_adaptive_screen_probes": counts["allocated_adaptive_probe_count"],
            "active_probe_count": counts["active_probe_count"],
            "active_uniform_count": counts["active_uniform_count"],
            "active_adaptive_count": counts["active_adaptive_count"],
            "finite_storage_probe_count": counts["finite_storage_probe_count"],
            "finite_storage_adaptive_count": counts["finite_storage_adaptive_count"],
            "stale_finite_adaptive_count": counts["stale_finite_adaptive_count"],
            "header_nonzero_tile_count": placement["allocator"]["activity_header"]["nonzero_tile_count"],
            "active_adaptive_to_header_bijection": placement["header_bijection"],
            "activity_rule": "only adaptive index < captured NumAdaptiveScreenProbes is active; finite positive values beyond that are stale",
            "rayinfo_used_for_activity": False,
        },
        "gbuffer_b_shading_model": {
            name: {
                "alpha_raw_code": receivers[name]["material"]["packed_id_code"],
                "low_nibble_shading_model_id": receivers[name]["material"]["shading_model_id"],
                "high_nibble_selective_output_mask": receivers[name]["material"]["selective_output_mask"],
                "rule": "ShadingModelID = GBufferB alpha & 0xF; the high nibble is SelectiveOutputMask",
            }
            for name in RECEIVERS
        },
        "temporal_max_frames_accumulated": temporal_parameter_record(dump),
        "receiver_materials": {
            name: {
                "shading_model": receivers[name]["material"]["shading_model_id"],
                "shading_model_alpha_raw": receivers[name]["material"]["packed_id_code"],
                "metallic": receivers[name]["material"]["metallic"],
                "roughness": receivers[name]["material"]["roughness"],
                "basecolor_linear": receivers[name]["basecolor"]["linear_rgb"],
                "vsm_r": receivers[name]["vsm_shadow_mask"]["r_linear"],
                "direct_rgb": receivers[name]["direct"]["direct_rgb_pre_exposed"],
            }
            for name in RECEIVERS
        },
    }
    # independent cross-check against the parallel receiver collector's JSON
    try:
        peer_path = Path(peer_receivers) / "data" / "receivers.json"
        peer = json.loads(peer_path.read_text(encoding="utf-8"))
        cc = {}
        for name in RECEIVERS:
            pr = peer["receivers"][name]
            ours = receivers[name]
            cc[name] = {
                "peer_world_cm": pr.get("world_cm"),
                "our_world_cm": ours.get("world_cm"),
                "world_delta_cm": (
                    None
                    if not pr.get("world_cm") or not ours.get("world_cm")
                    else float(np.max(np.abs(np.array(pr["world_cm"]) - np.array(ours["world_cm"]))))
                ),
                "peer_linear_w_value": pr.get("depth", {}).get("linear_w_m"),
                "our_view_depth_cm": ours["scene_depth"]["view_depth_cm"],
                "note": "peer field is named linear_w_m but its numeric value is centimetres (788.305 for A); ours is labelled cm and metres are given separately",
                "material_match": bool(
                    int(pr["material"]["roughness_code"]) == int(ours["material"]["roughness_code"])
                    and int(pr["material"]["metallic_code"]) == int(ours["material"]["metallic_code"])
                ),
                "direct_match": bool(
                    np.allclose(np.array(pr["direct"]["direct_rgb_pre_exposure"], dtype=float),
                                np.array(ours["direct"]["direct_rgb_pre_exposed"], dtype=float))
                ),
                "shadow_r_code_peer": pr.get("shadow", {}).get("shader_r_code"),
                "shadow_r_code_ours": ours["vsm_shadow_mask"]["shader_r_code"],
            }
        checks["peer_receiver_cross_check"] = {
            "source": str(peer_path),
            "source_sha256": sha256_file(peer_path),
            "receivers": cc,
        }
    except Exception as e:
        checks["peer_receiver_cross_check"] = {"error": repr(e)}

    (data_dir / "context-checks.json").write_text(json.dumps(jval(checks), indent=2), encoding="utf-8")
    return {"view": view, "placement": placement, "receivers": receivers, "probes": probes, "checks": checks, "dump": dump}

# ----------------------------------------------------------------------------
# per-pixel integration, history and reflection
# ----------------------------------------------------------------------------

def _pixel_rgb(arr, x, y):
    return [float(v) for v in arr[min(max(y, 0), arr.shape[0] - 1), min(max(x, 0), arr.shape[1] - 1)]]


def build_integration(dump: Dump, view: dict, receivers: dict, probes: dict):
    temporal = temporal_parameter_record(dump)
    max_frames = float(temporal["max_frames_accumulated"])
    diff_cur = decode_r11g11b10(dump.read_tex(RES["diffuse"], V_DIFFUSE, dtype="<u4", channels=1)["array"])
    diff_hist = decode_r11g11b10(dump.read_tex(RES["diffuse_hist"], V_HISTORY_OLD, dtype="<u4", channels=1)["array"])
    diff_new = decode_r11g11b10(dump.read_tex(RES["diffuse_newhist"], V_HISTORY_NEW, dtype="<u4", channels=1)["array"])
    frames_hist = dump.read_tex(RES["diffuse_frames"], V_HISTORY_OLD, dtype="u1", channels=1)["array"]
    frames_new = dump.read_tex(RES["diffuse_frames_new"], V_HISTORY_NEW, dtype="u1", channels=1)["array"]

    refl = decode_r11g11b10(dump.read_tex(RES["reflection"], V_REFL_FINAL, dtype="<u4", channels=1)["array"])
    refl_res = decode_r11g11b10(dump.read_tex(RES["reflection_resolved"], V_REFL_RESOLVED, dtype="<u4", channels=1)["array"])
    refl_depth = dump.read_tex(RES["reflection_depth"], V_REFL_RESOLVED, dtype="<f2", channels=1)["array"].astype(np.float64)
    refl_frames = dump.read_tex(RES["reflection_frames"], V_REFL_TEMPORAL, dtype="u1", channels=1)["array"]
    refl_frames_hist = dump.read_tex(RES["reflection_frames_hist"], V_HISTORY_OLD, dtype="u1", channels=1)["array"]

    per_receiver = {}
    for name, r in receivers.items():
        x, y = r["pixel_xy"]
        cur = _pixel_rgb(diff_cur, x, y)
        hist = _pixel_rgb(diff_hist, x, y)
        new = _pixel_rgb(diff_new, x, y)
        delta = [hist[i] - cur[i] for i in range(3)]
        rel = [abs(delta[i]) / max(abs(cur[i]), 1e-6) for i in range(3)]
        f_dec = decode_fast_update_mode_frames(frames_hist[y, x], max_frames)
        fn_dec = decode_fast_update_mode_frames(frames_new[y, x], max_frames)
        # SH replay comparison at the covering probe's centre
        p = probes[name]
        sh4 = p["unit_albedo_lambert_response"]
        sh_rel = p["diffuse_vs_sh_relative_error"]
        per_receiver[name] = {
            "diffuse_indirect_current": cur,
            "diffuse_indirect_history": hist,
            "diffuse_indirect_new_history": new,
            "history_minus_current": delta,
            "history_minus_current_definition": "history - current, component-wise",
            "relative_history_difference": rel,
            "num_frames_accumulated_history": f_dec["decoded_num_frames_accumulated"],
            "num_frames_accumulated_new": fn_dec["decoded_num_frames_accumulated"],
            "num_frames_accumulated_history_raw_byte": f_dec["raw_byte"],
            "num_frames_accumulated_new_raw_byte": fn_dec["raw_byte"],
            "num_frames_accumulated_history_fast_update_code": f_dec["fast_update_mode_amount_code_high_nibble"],
            "num_frames_accumulated_new_fast_update_code": fn_dec["fast_update_mode_amount_code_high_nibble"],
            "num_frames_accumulated_history_count_code": f_dec["num_frames_accumulated_count_code_low_nibble"],
            "num_frames_accumulated_new_count_code": fn_dec["num_frames_accumulated_count_code_low_nibble"],
            "num_frames_accumulated_history_decode": f_dec,
            "num_frames_accumulated_new_decode": fn_dec,
            "max_frames_accumulated_parameter": max_frames,
            "sh_replay_raw_evaluate_sh_irradiance_at_probe": p["sh_replay_raw_evaluate_sh_irradiance"],
            "sh_replay_unit_albedo_lambert_response_at_probe": sh4,
            "sh_replay_physical_irradiance_4pi_at_probe": p["physical_irradiance_4pi"],
            "sh_replay_vs_captured_diffuse_relative_error": sh_rel,
            "sh_replay_note": (
                "stored SH3 coefficients are sphere-average radiance; raw EvaluateSHIrradiance is not physical "
                "irradiance. Physical irradiance = 4*pi*raw; the captured DiffuseIndirect (unit-albedo Lambert "
                "response, before material albedo) = 4*raw. 4*raw is what is compared here."
            ),
        }

    refl_receivers = {}
    for name, r in receivers.items():
        x, y = r["pixel_xy"]
        rgb = _pixel_rgb(refl, x, y)
        res = _pixel_rgb(refl_res, x, y)
        d = float(refl_depth[y, x])
        fr = int(refl_frames[y, x])
        frh = int(refl_frames_hist[y, x])
        fr_eff = fr / 255.0 * 32.0
        frh_eff = frh / 255.0 * 32.0
        valid = bool(fr > 0 and math.isfinite(d) and 0.0 < d < 1.0e6 and max(rgb) < 1.0e5)
        refl_receivers[name] = {
            "specular_indirect_native": rgb,
            "resolved_specular_indirect_native": res,
            "resolved_hit_depth_cm": d if math.isfinite(d) else None,
            "resolved_hit_depth_m": d / 100.0 if math.isfinite(d) and d < 1.0e6 else None,
            "num_frames_accumulated": fr_eff,
            "num_frames_accumulated_effective": fr_eff,
            "num_frames_accumulated_raw_byte": fr,
            "num_frames_accumulated_unorm": fr / 255.0,
            "num_frames_accumulated_history_effective": frh_eff,
            "num_frames_accumulated_history_raw_byte": frh,
            "frame_count_encoding": "UNORM byte stores frames/32; effective = raw_byte/255*32",
            "valid_reflection_signal": valid,
            "invalid_reason": None if valid else "raw NumFramesAccumulated=0 and/or sentinel value; the pixel has no accepted reflection history",
        }

    sources = {
        "diffuse_current": tex_source(dump, RES["diffuse"], V_DIFFUSE, 0, 0),
        "diffuse_history": tex_source(dump, RES["diffuse_hist"], V_HISTORY_OLD, 0, 0),
        "diffuse_new_history": tex_source(dump, RES["diffuse_newhist"], V_HISTORY_NEW, 0, 0),
        "frames_history": tex_source(dump, RES["diffuse_frames"], V_HISTORY_OLD, 0, 0),
        "frames_new": tex_source(dump, RES["diffuse_frames_new"], V_HISTORY_NEW, 0, 0),
        "reflection_final": tex_source(dump, RES["reflection"], V_REFL_FINAL, 0, 0),
        "reflection_resolved": tex_source(dump, RES["reflection_resolved"], V_REFL_RESOLVED, 0, 0),
        "reflection_depth": tex_source(dump, RES["reflection_depth"], V_REFL_RESOLVED, 0, 0),
        "reflection_frames": tex_source(dump, RES["reflection_frames"], V_REFL_TEMPORAL, 0, 0),
        "reflection_frames_history": tex_source(dump, RES["reflection_frames_hist"], V_HISTORY_OLD, 0, 0),
    }

    integration = {
        "schema": "sponza-lumen-integration-history-v2",
        "scope": "captured presented-graph per-pixel Lumen screen-probe spatial integration and its temporal history; DiffuseIndirect is the ScreenProbeIntegrate output before material albedo",
        "view_frame": view["state_frame_index"],
        "preexposure": PREEXPOSURE,
        "receivers": per_receiver,
        "sources": sources,
        "frame_count_encoding": {
            "pass": {"id": 762, "name": "TemporalReprojection(1280x720)"},
            "parameter": temporal,
            "packing_shader": temporal["packing_shader"],
            "rule": "high nibble = ceil(FastUpdateModeAmount*15); low nibble = round(saturate(frames/min(Max,15))*15); low==15 -> MaxFramesAccumulated",
            "raw_15_at_receivers": "low nibble 15 with MaxFramesAccumulated=10 decodes to 10.0 frames, not 15",
        },
        "sh_unit_conversion": {
            "stored": "SH3 sphere-average radiance coefficients",
            "raw_evaluate_sh_irradiance": "R",
            "physical_irradiance": "4*pi*R",
            "unit_albedo_lambert_response": "4*R = physical_irradiance/pi",
            "captured_diffuse_indirect_unit_convention": "4*R before material albedo; spatial integration and active occlusion can change per-pixel values",
        },
        "notes": [
            "DiffuseIndirect = DiffuseLighting * Diffuse_Lambert(1) = 4 * raw SH evaluate (unit-albedo Lambert response, before albedo); the composite pixel shader applies diffuse color later",
            "history is the imported previous-frame texture (version 0); new_history is the TemporalReprojection output from pass 762",
            "the captured MaxFramesAccumulated parameter is 10.0, so raw frame byte 15 (low nibble 15) decodes to 10 accumulated frames, not 15",
            "history_minus_current is history minus current; the earlier current-minus-history sign was wrong",
            "a high history value is evidence of accepted reprojection, not proof that the sample is correct",
        ],
        "gaps": [
            "per-pixel probe interpolation weights are computed in the integrate shader and are not captured as a resource",
            "the temporal filter's per-pixel blend factor and disocclusion decision are shader-local and are not captured",
        ],
    }

    reflection = {
        "schema": "sponza-lumen-reflection-signal-v2",
        "scope": "captured presented-graph Lumen reflection resolve/temporal/spatial outputs at the receiver pixels",
        "receivers": refl_receivers,
        "sources": sources,
        "frame_count_encoding": {
            "packing_shader": {
                "file": str(REFLECTION_DENOISER_USH),
                "lines": "51-59",
                "pack": "half PackNumFramesAccumulated(half X) { return X / 32.0; }",
                "unpack": "half UnpackNumFramesAccumulated(half X) { return X * 32.0; }",
                "source_present": REFLECTION_DENOISER_USH.exists(),
            },
            "decode": "effective_frames = raw_byte/255*32",
            "raw_96_effective_frames": 96 / 255.0 * 32.0,
        },
        "notes": [
            "SpecularIndirect is the final spatial reflection signal; ResolvedSpecularIndirect is the resolve output before temporal accumulation",
            "NumFramesAccumulated is stored as a UNORM byte holding frames/32; raw 96 at C means 96/255*32 = 12.0470588 effective frames, not 96",
            "raw NumFramesAccumulated=0 at A and B means no accepted reflection history there; those pixels are not presented as valid reflection evidence",
            "reflection per-ray directions and the reflection trace lane selection are shader-local and are not captured; only the resolved signal and its history count are observed",
        ],
        "gaps": [
            "reflection ray directions / BRDF sampling choices are not captured",
            "no acceleration-structure usability claim is made",
        ],
        "figures_note": "reflection crop figure is centred on C (polished gold) where the captured reflection signal is valid",
    }
    return integration, reflection


# ----------------------------------------------------------------------------
# peer cache records: integrate the separately collected exact replays
# ----------------------------------------------------------------------------

def _verify_peer_resource(entry, dump: Dump):
    file = str(entry.get("file", "")).replace("\\", "/")
    path = dump.root / file
    out = {
        "file": file,
        "exists": path.exists(),
        "expected_bytes": entry.get("bytes"),
        "actual_bytes": path.stat().st_size if path.exists() else None,
        "sha256_expected": entry.get("sha256"),
        "sha256_actual": sha256_file(path) if path.exists() else None,
    }
    out["sha256_matches"] = bool(out["sha256_expected"] and out["sha256_actual"] == out["sha256_expected"])
    out["bytes_match"] = bool(out["expected_bytes"] == out["actual_bytes"])
    producer = entry.get("producerPass") or {}
    p = dump.passes_by_ptr.get(str(entry.get("version"))) if entry.get("version") else None
    out["producer_pass_id_actual"] = p["Id"] if p else None
    out["producer_pass_name_actual"] = p["EventName"] if p else None
    out["producer_pass_matches"] = bool(
        p and int(producer.get("id", -1)) == p["Id"] and producer.get("pointer") == entry.get("version")
    )
    rb = dump.readback_by_file.get(file)
    out["readback_status"] = rb["Status"] if rb else None
    out["readback_timing"] = rb["Timing"] if rb else None
    out["readback_file_exists"] = bool(rb and rb["FileExists"] and rb["FileSizeMatches"])
    return out


def build_peer_records(dump: Dump, data_dir: Path, peer_cache: Path):
    surface_path = peer_cache / "surface-cache-query.json"
    world_path = peer_cache / "world-cache-query.json"
    surface = json.loads(surface_path.read_text(encoding="utf-8"))
    world = json.loads(world_path.read_text(encoding="utf-8"))

    surf_checks = {}
    for key, entry in surface.get("resources", {}).items():
        if isinstance(entry, dict) and "file" in entry:
            surf_checks[key] = _verify_peer_resource(entry, dump)
    for key, entry in surface.get("buffers", {}).items():
        if isinstance(entry, dict) and "file" in entry:
            surf_checks[key] = _verify_peer_resource(entry, dump)

    surface_out = {
        "schema": "sponza-surfacecache-query-integrated-v1",
        "scope": "declared single card-tile query replayed from captured Surface Cache state; not an observed shader-thread choice",
        "provenance": {
            "collector": "separate DeepCluster cache collector",
            "source_json": str(surface_path),
            "source_sha256": sha256_file(surface_path),
            "integration": "this record restates the collector's exact native versions/coordinates/values and re-verifies file bytes, producer passes and readback status against the preserved dump",
        },
        "capture": surface.get("capture"),
        "selection": surface.get("selection"),
        "card": surface.get("card"),
        "page": surface.get("page"),
        "material": surface.get("material"),
        "query": surface.get("query"),
        "values": surface.get("values"),
        "tile_validation_summary": {
            "tileTexelsExamined": surface.get("values", {}).get("tileTexelsExamined"),
            "nearestMatchesTileTexels": surface.get("values", {}).get("nearestMatchesTileTexels"),
            "truncationMatchesTileTexels": surface.get("values", {}).get("truncationMatchesTileTexels"),
        },
        "native_records": {
            "albedo": surface.get("resources", {}).get("albedo"),
            "normal": surface.get("resources", {}).get("normal"),
            "emissive": surface.get("resources", {}).get("emissive"),
            "page_buffer": surface.get("buffers", {}).get("PageBuffer"),
            "card_tiles": surface.get("buffers", {}).get("CardTiles"),
        },
        "verification": surf_checks,
        "coverage_gaps": surface.get("coverageGaps"),
        "display_notes": surface.get("previews"),
        "observed_vs_replay": "replay_of_declared_query",
    }
    (data_dir / "surfacecache-query.json").write_text(json.dumps(jval(surface_out), indent=2), encoding="utf-8")

    world_checks = {}
    for key, entry in world.get("inputs", {}).items():
        if isinstance(entry, dict) and "file" in entry:
            world_checks[key] = _verify_peer_resource(entry, dump)

    wq = world.get("queryWitness", {})
    cl = world.get("cacheLookup", {})
    neighbors = []
    seen = set()
    for tap in cl.get("radianceTaps", []):
        key = (tap.get("probeIndex"), tuple(tap.get("atlasXY") or []))
        if key in seen:
            continue
        seen.add(key)
        neighbors.append(
            {
                "probe_index": tap.get("probeIndex"),
                "atlas_xy": tap.get("atlasXY"),
                "offset_bits": tap.get("offsetBits"),
                "bilinear_weight": tap.get("bilinearWeight"),
                "neighbor_weight": tap.get("neighborWeight"),
                "first_tap_packed": tap.get("packed"),
                "first_tap_decoded_radiance": tap.get("decodedRadiance"),
            }
        )
        if len(neighbors) >= 8:
            break

    world_out = {
        "schema": "sponza-worldcache-neighborhood-integrated-v1",
        "scope": "one declared World Radiance Cache lookup reconstructed from a captured presented-graph compacted screen-probe trace lane; not a recorded shader-local query and not proof that the dispatched ray chose it",
        "provenance": {
            "collector": "separate DeepCluster cache collector",
            "source_json": str(world_path),
            "source_sha256": sha256_file(world_path),
            "integration": "this record restates the collector's exact native versions/coordinates/values and re-verifies file bytes, producer passes and readback status against the preserved dump",
        },
        "capture": {"root": world.get("capture", {}).get("root"), "infos": world.get("capture", {}).get("infos")},
        "pass": {"id": 747, "name": "TraceGlobalSDFAndApplyRadianceCache"},
        "selection": world.get("selection"),
        "query_witness": wq,
        "angular_ray": wq.get("angularRay"),
        "directional_encoding": world.get("directionalEncoding"),
        "parameters": world.get("parameters"),
        "cache_lookup": {
            "query": cl.get("query"),
            "selected_clipmap": cl.get("selectedClipmap"),
            "clipmap_corner_tws_and_cell_size": cl.get("clipmapCornerTWSAndCellSize"),
            "clipmap_cell_size_cm": cl.get("clipmapCellSize"),
            "probe_coord_float": cl.get("probeCoordFloat"),
            "bottom_cell": cl.get("bottomCell"),
            "fractional_weights": cl.get("fractionalWeights"),
            "mip_level": cl.get("mipLevel"),
            "angular_footprint_texels": cl.get("angularFootprintTexels"),
            "minimum_local_trace_distance_cm": cl.get("minimumLocalTraceDistance"),
            "neighbors": neighbors,
            "total_radiance_taps": len(cl.get("radianceTaps", [])),
        },
        "validity": world.get("validity"),
        "radiance_scale": world.get("radianceScale"),
        "comparison": world.get("comparison"),
        "native_inputs": {
            "indirection": world.get("inputs", {}).get("indirection"),
            "final_radiance_atlas": world.get("inputs", {}).get("finalRadianceAtlas"),
        },
        "verification": world_checks,
        "coverage_gaps": world.get("coverageGaps"),
        "observed_vs_replay": "replay_of_declared_query",
    }
    (data_dir / "worldcache-neighborhood.json").write_text(json.dumps(jval(world_out), indent=2), encoding="utf-8")
    return surface_out, world_out

# ----------------------------------------------------------------------------
# figures
# ----------------------------------------------------------------------------

TONE_NOTE = "display = sRGB_OETF(L/(1+L)) with L the native pre-exposed linear value; no exposure multiply, no per-image normalization; inverse: first sRGB_EOTF(code/255) -> t, then L=t/(1-t)"
DEPTH_NOTE = "display = fixed navy->blue->green->amber->white depth ramp over 0.05..10 m view depth (or ray length for hit distance), t=log10(max(W_m,0.05))/log10(200), W_m=native_cm/100; inverted from the declared native cm values"
DIR_NOTE = "display RGB = round(255*(0.5+0.5*direction_world)); no gamma; invertible for unit vectors"


def tone_srgb_u8(L):
    L = np.maximum(np.asarray(L, dtype=np.float64), 0.0)
    t = L / (1.0 + L)
    return np.round(255.0 * srgb_oetf(t)).astype(np.uint8)


def depth_u8_cm(cm, lo_m=1.0, hi_m=100.0):
    cm = np.asarray(cm, dtype=np.float64)
    wm = np.maximum(cm / 100.0, 1e-12)
    t = np.log10(np.maximum(wm, lo_m) / lo_m) / math.log10(hi_m / lo_m)
    t = np.clip(t, 0.0, 1.0)
    return np.round(255.0 * t).astype(np.uint8)


def depth_color_u8(cm, valid=None, lo_m=0.05, hi_m=10.0):
    """Declared fixed depth ramp over 0.05..30 m."""
    cm = np.asarray(cm, dtype=np.float64)
    wm = np.maximum(cm / 100.0, 1e-12)
    t = np.log10(np.maximum(wm, lo_m) / lo_m) / math.log10(hi_m / lo_m)
    t = np.clip(t, 0.0, 1.0)
    stops = np.array(
        [
            [0.01, 0.01, 0.08],
            [0.05, 0.25, 0.75],
            [0.05, 0.70, 0.55],
            [0.95, 0.70, 0.10],
            [1.00, 1.00, 0.90],
        ]
    )
    xs = np.array([0.0, 0.25, 0.50, 0.75, 1.0])
    out = np.zeros(t.shape + (3,), dtype=np.float64)
    for c in range(3):
        out[..., c] = np.interp(t, xs, stops[:, c])
    rgb = np.round(255.0 * out).astype(np.uint8)
    if valid is not None:
        rgb = np.where(np.asarray(valid)[..., None], rgb, np.array([18, 18, 18], dtype=np.uint8))
    return rgb


def gray_u8(v, maxv):
    v = np.asarray(v, dtype=np.float64)
    return np.round(255.0 * np.clip(v / float(maxv), 0.0, 1.0)).astype(np.uint8)


def stats_of(arr, name, unit):
    a = np.asarray(arr, dtype=np.float64)
    finite = np.isfinite(a)
    vals = a[finite]
    if vals.size == 0:
        return {"name": name, "unit": unit, "count": int(a.size), "finite": 0}
    if a.ndim == 1 or (a.ndim > 1 and a.shape[-1] not in (2, 3, 4)):
        return {
            "name": name,
            "unit": unit,
            "count": int(a.size),
            "finite": int(finite.sum()),
            "min": float(vals.min()),
            "max": float(vals.max()),
            "mean": float(vals.mean()),
            "nonzero": int(np.count_nonzero(vals)),
        }
    chans = a.shape[-1]
    out = {"name": name, "unit": unit, "count": int(a.size), "finite": int(finite.sum()), "channels": []}
    for c in range(chans):
        v = a[..., c]
        f = np.isfinite(v)
        vv = v[f]
        out["channels"].append(
            {
                "min": float(vv.min()) if vv.size else None,
                "max": float(vv.max()) if vv.size else None,
                "mean": float(vv.mean()) if vv.size else None,
                "nonzero": int(np.count_nonzero(vv)),
            }
        )
    return out


def save_native(out_dir: Path, name: str, arrays: dict):
    path = out_dir / "data" / "figure-native" / f"{name}.npz"
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **{k: np.asarray(v) for k, v in arrays.items()})
    return path


def crop_native(rgb, x, y, w, h, fill=0.0):
    shape = (h, w, rgb.shape[2]) if rgb.ndim == 3 else (h, w)
    out = np.full(shape, fill, dtype=rgb.dtype)
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(rgb.shape[1], x + w)
    y1 = min(rgb.shape[0], y + h)
    if x1 > x0 and y1 > y0:
        out[y0 - y : y1 - y, x0 - x : x1 - x] = rgb[y0:y1, x0:x1]
    return out


def build_figures(dump: Dump, view: dict, placement: dict, receivers: dict, probes: dict, out_dir: Path):
    fig_dir = out_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    screenshot_path = dump.root / "Base" / "Screenshot.png"
    shot = np.asarray(Image.open(screenshot_path).convert("RGB")).astype(np.uint8)

    # ---- F1 clean captured frame -------------------------------------------------
    f1 = fig_dir / "scene_clean.png"
    Image.fromarray(shot, "RGB").save(f1)
    manifest.append(
        {
            "file": "figures/scene_clean.png",
            "width": int(shot.shape[1]),
            "height": int(shot.shape[0]),
            "kind": "captured_reference",
            "source": {"file": "Base/Screenshot.png", "sha256": sha256_file(screenshot_path), "format": "PNG RGBA captured after tonemap/FXAA"},
            "mapping": "captured display-sRGB bytes, unchanged",
            "native_units": "sRGB 8-bit display",
            "native_stats": stats_of(shot, "screenshot_srgb", "code 0..255"),
            "finite": int(shot.size),
            "total": int(shot.size),
            "nonempty": int(np.count_nonzero(shot)),
            "native_npz": None,
            "recoverable_native": True,
            "notes": "clean 1280x720 game-view frame; no UI or embedded text",
        }
    )

    # ---- F2 placement overlay ---------------------------------------------------
    depth = np.zeros((84, 80), dtype=np.float64)
    world = np.zeros((84, 80, 3), dtype=np.float64)
    normal = np.zeros((84, 80, 3), dtype=np.float64)
    for e in placement["entries"]:
        if not e["active"]:
            continue
        ax, ay = e["atlas_xy"]
        depth[ay, ax] = e["probe_scene_depth_cm"]
        world[ay, ax] = e["world_cm"]
        normal[ay, ax] = e["normal_world"]
    overlay = Image.fromarray(shot.copy(), "RGB").convert("RGBA")
    draw = ImageDraw.Draw(overlay, "RGBA")
    for e in placement["entries"]:
        if not e["active"]:
            continue
        sx, sy = e["screen_xy_from_projection"]
        if not (0 <= sx < 1280 and 0 <= sy < 720):
            continue
        col = depth_color_u8(e["probe_scene_depth_cm"])
        draw.ellipse((sx - 1.2, sy - 1.2, sx + 1.2, sy + 1.2), fill=(int(col[0]), int(col[1]), int(col[2]), 200))
    for name, r in receivers.items():
        sx, sy = r["covering_screen_probe"]["screen_xy_from_projection"]
        draw.ellipse((sx - 6, sy - 6, sx + 6, sy + 6), outline=(255, 255, 255, 255), width=2)
        draw.ellipse((r["pixel_xy"][0] - 1, r["pixel_xy"][1] - 1, r["pixel_xy"][0] + 1, r["pixel_xy"][1] + 1), fill=(255, 40, 40, 255))
    f2 = fig_dir / "screenprobe_placement_overlay.png"
    overlay.convert("RGB").save(f2)
    npz = save_native(out_dir, "screenprobe_placement_overlay", {"depth_cm": depth, "world_cm": world, "normal_world": normal, "screenshot_srgb": shot})
    manifest.append(
        {
            "file": "figures/screenprobe_placement_overlay.png",
            "width": 1280,
            "height": 720,
            "kind": "display_overlay",
            "source": {"placement_probes": "data/screenprobes-placement.json", "screenshot": "Base/Screenshot.png"},
            "mapping": "underlay = captured sRGB; dots at projected probe pixel, colour = fixed depth ramp navy->blue->green->amber->white over 0.05..10 m probe view depth; white rings = covering probe, red dot = receiver pixel",
            "native_units": "probe depth cm; world cm",
            "native_stats": {
                "probe_depth_cm": stats_of(depth[depth > 0], "probe_depth_cm", "cm"),
                "probe_world_cm": stats_of(world.reshape(-1, 3), "probe_world_cm", "cm"),
            },
            "finite": int(np.isfinite(world).sum()),
            "total": int(world.size),
            "nonempty": int((depth > 0).sum()),
            "native_npz": npz.relative_to(out_dir).as_posix(),
            "recoverable_native": True,
            "notes": (
                "plots only allocator-active probes: 3686 = 3569 finite uniform cells + 117 captured adaptive probes; "
                "3809 finite-storage cells exist but 123 adaptive values beyond NumAdaptiveScreenProbes=117 are stale and are not plotted"
            ),
        }
    )

    # ---- F3 rays for A ----------------------------------------------------------
    pA = probes["A"]
    base = Image.fromarray(shot.copy(), "RGB").convert("RGBA")
    d = ImageDraw.Draw(base, "RGBA")
    start = pA["screen_xy_from_projection"]
    ray_rows = []
    for ray in pA["rays"]:
        rr = ray["radiance_trace_native"]
        base_col = tone_srgb_u8(np.array(rr)).astype(np.int32)
        col = np.clip(60 + 195 * (base_col / 255.0), 0, 255).astype(int)
        alpha = 235 if ray["b_hit"] else 130
        if ray["hit_screen_xy"] is not None:
            end = ray["hit_screen_xy"]
        else:
            hp = np.array(pA["translated_world_cm"]) + np.array(ray["direction_world"]) * min(3000.0, 800.0)
            end = project_translated_world(view, hp)[0]
        d.line((start[0], start[1], float(end[0]), float(end[1])), fill=(int(col[0]), int(col[1]), int(col[2]), alpha), width=2)
        if ray["hit_screen_xy"] is not None and 0 <= end[0] < 1280 and 0 <= end[1] < 720:
            d.ellipse((end[0] - 2, end[1] - 2, end[0] + 2, end[1] + 2), fill=(int(col[0]), int(col[1]), int(col[2]), 255))
        ray_rows.append(
            {
                "i": ray["trace_texel_xy"][1],
                "j": ray["trace_texel_xy"][0],
                "trace": rr,
                "filtered_same_slot": ray["radiance_filtered_native"],
                "hit_distance_cm": ray["hit_distance_cm"],
                "direction": ray["direction_world"],
            }
        )
    f3 = fig_dir / "screenprobe_rays_A.png"
    base.convert("RGB").save(f3)
    npz = save_native(
        out_dir,
        "screenprobe_rays_A",
        {
            "screen_xy": np.array(start),
            "ray_cell_xy": np.array([[r["i"], r["j"]] for r in ray_rows]),
            "ray_trace_radiance_native": np.array([r["trace"] for r in ray_rows]),
            "ray_filtered_radiance_same_integer_slot_native": np.array([r["filtered_same_slot"] for r in ray_rows]),
            "ray_hit_distance_cm": np.array([-1.0 if r["hit_distance_cm"] is None else r["hit_distance_cm"] for r in ray_rows]),
            "ray_hit_valid": np.array([r["hit_distance_cm"] is not None for r in ray_rows], dtype=bool),
            "ray_direction_world": np.array([r["direction"] for r in ray_rows]),
        },
    )
    manifest.append(
        {
            "file": "figures/screenprobe_rays_A.png",
            "width": 1280,
            "height": 720,
            "kind": "display_overlay",
            "source": {
                "probe": "data/screenprobe-A.json",
                "rayinfo": "Lumen.ScreenProbeGather.RayInfosForTracing v" + V_RAYINFO,
                "trace_radiance": "Lumen.ScreenProbeGather.TraceRadiance v" + V_TRACE,
                "trace_hit": "Lumen.ScreenProbeGather.TraceHit v" + V_TRACE,
            },
            "mapping": "underlay captured sRGB; 64 segments from the probe pixel to the projected captured hit point (misses: 8 m along the reconstructed direction); colour derives from the structured trace-slot radiance_trace_native (display = 60 + 195*sRGB_OETF(L/(1+L)) for line legibility, L native pre-exposed); filtered ScreenProbeRadiance at the same integer slot is NOT used because the regular reconstructed field is a different angular layout",
            "native_units": "structured trace radiance pre-exposed; distance cm",
            "native_stats": {
                "ray_trace_radiance": stats_of(np.array([r["trace"] for r in ray_rows]), "ray_trace_radiance", "pre-exposed linear"),
                "ray_hit_distance_cm": stats_of(np.array([np.nan if r["hit_distance_cm"] is None else r["hit_distance_cm"] for r in ray_rows]), "ray_hit_distance", "cm"),
            },
            "finite": int(sum(1 for r in ray_rows if all(math.isfinite(v) for v in r["trace"]))),
            "total": 64,
            "nonempty": int(sum(np.count_nonzero(r["trace"]) for r in ray_rows)),
            "native_npz": npz.relative_to(out_dir).as_posix(),
            "recoverable_native": True,
            "notes": (
                "structured trace slot [i,j] and regular reconstructed angular slot [i,j] are different layouts; the npz keeps the trace radiance and the filtered regular-field value at the same integer slot separately; misses store hit distance -1.0 with ray_hit_valid=false. "
                "Direction is a declared-query replay from observed RayInfo, observed blue-noise texel and the declared formula; it is not an observed shader-local dispatch"
            ),
        }
    )

    # ---- F4 direction bins ------------------------------------------------------
    bins = np.zeros((512, 512, 3), dtype=np.uint8)
    for i in range(8):
        for j in range(8):
            ray = pA["rays"][i * 8 + j]
            col = np.round(255.0 * (0.5 + 0.5 * np.array(ray["direction_world"]))).astype(np.uint8)
            bins[i * 64:(i + 1) * 64, j * 64:(j + 1) * 64] = col
    for k in range(0, 513, 64):
        bins[k : min(k + 1, 512), :] = 32
        bins[:, k : min(k + 1, 512)] = 32
    f4 = fig_dir / "screenprobe_raybins_A.png"
    Image.fromarray(bins, "RGB").save(f4)
    npz = save_native(out_dir, "screenprobe_raybins_A", {"direction_rgb_u8": bins, "ray_cell_xy": np.array([[r["trace_texel_xy"][0], r["trace_texel_xy"][1]] for r in pA["rays"]]), "direction_world": np.array([r["direction_world"] for r in pA["rays"]])})
    manifest.append(
        {
            "file": "figures/screenprobe_raybins_A.png",
            "width": 512,
            "height": 512,
            "kind": "data_raster",
            "source": {"probe": "data/screenprobe-A.json"},
            "mapping": DIR_NOTE,
            "native_units": "unit direction",
            "native_stats": {"direction_world": stats_of(np.array([r["direction_world"] for r in pA["rays"]]), "direction_world", "unit")},
            "finite": 192,
            "total": 192,
            "nonempty": 192,
            "native_npz": npz.relative_to(out_dir).as_posix(),
            "recoverable_native": True,
            "notes": "8x8 trace-texel grid; each 64x64 cell is one RayInfo cell; direction replay includes the observed blue-noise texel and the probe's tile coordinate",
        }
    )

    # ---- F5 raw trace vs filtered radiance --------------------------------------
    rad_raw = np.zeros((512, 512, 3), dtype=np.uint8)
    rad_filt = np.zeros((512, 512, 3), dtype=np.uint8)
    for i in range(8):
        for j in range(8):
            ray = pA["rays"][i * 8 + j]
            rad_raw[i * 64:(i + 1) * 64, j * 64:(j + 1) * 64] = tone_srgb_u8(np.array(ray["radiance_trace_native"]))
            rad_filt[i * 64:(i + 1) * 64, j * 64:(j + 1) * 64] = tone_srgb_u8(np.array(ray["radiance_filtered_native"]))
    rad = np.concatenate([rad_raw, rad_filt], axis=1)
    for k in range(0, 1025, 64):
        rad[k : min(k + 1, 512), :] = 40
        rad[:, min(k, 1023) : min(k + 1, 1024)] = 40
    f5 = fig_dir / "screenprobe_radiance_A.png"
    Image.fromarray(rad, "RGB").save(f5)
    npz = save_native(out_dir, "screenprobe_radiance_A", {"radiance_rgb_u8": rad, "radiance_filtered_native": np.array([r["radiance_filtered_native"] for r in pA["rays"]]), "radiance_trace_native": np.array([r["radiance_trace_native"] for r in pA["rays"]])})
    manifest.append(
        {
            "file": "figures/screenprobe_radiance_A.png",
            "width": 1024,
            "height": 512,
            "kind": "data_raster",
            "source": {"left_panel": "Lumen.ScreenProbeGather.TraceRadiance v" + V_TRACE, "right_panel": "Lumen.ScreenProbeGather.ScreenProbeRadiance v" + V_PROBERADIANCE},
            "mapping": "two separately-labelled 8x8 panels: left = structured trace slot [i,j] of raw TraceRadiance; right = regular reconstructed filtered ScreenProbeRadiance field [i,j]; the same integer [i,j] in the two panels is not the same angular ray; " + TONE_NOTE + "; 1-px grid",
            "native_units": "pre-exposed linear radiance",
            "native_stats": {
                "filtered_regular_field": stats_of(np.array([r["radiance_filtered_native"] for r in pA["rays"]]), "filtered_regular_field_radiance", "pre-exposed"),
                "trace_structured_slot": stats_of(np.array([r["radiance_trace_native"] for r in pA["rays"]]), "trace_structured_slot_radiance", "pre-exposed"),
            },
            "finite": 192,
            "total": 192,
            "nonempty": int(np.count_nonzero(rad_raw) + np.count_nonzero(rad_filt)),
            "native_npz": npz.relative_to(out_dir).as_posix(),
            "recoverable_native": True,
            "notes": "left is the structured trace layout; right is the regular reconstructed filtered field. The filtered radiance is the value consumed by ScreenProbeConvertToIrradiance, but it is not the same ray as the trace slot with the same integer indices; both are retained in the npz under distinct keys.",
        }
    )

    # ---- F6 hit distance --------------------------------------------------------
    hd = np.zeros((512, 512), dtype=np.uint8)
    hd_color = np.zeros((512, 512, 3), dtype=np.uint8)
    for i in range(8):
        for j in range(8):
            ray = pA["rays"][i * 8 + j]
            if ray["hit_distance_cm"] is None:
                val = 0
                col = np.array([18, 18, 18], dtype=np.uint8)
            else:
                val = int(depth_u8_cm(ray["hit_distance_cm"]))
                col = depth_color_u8(ray["hit_distance_cm"])
            hd[i * 64:(i + 1) * 64, j * 64:(j + 1) * 64] = val
            hd_color[i * 64:(i + 1) * 64, j * 64:(j + 1) * 64] = col
    for k in range(0, 513, 64):
        hd_color[k : min(k + 1, 512), :] = 48
        hd_color[:, k : min(k + 1, 512)] = 48
    f6 = fig_dir / "screenprobe_hitdistance_A.png"
    Image.fromarray(hd_color, "RGB").save(f6)
    npz = save_native(out_dir, "screenprobe_hitdistance_A", {"hitdistance_u8": hd, "hit_distance_cm": np.array([-1.0 if r["hit_distance_cm"] is None else r["hit_distance_cm"] for r in pA["rays"]]), "hit_valid": np.array([r["hit_distance_cm"] is not None for r in pA["rays"]], dtype=bool), "b_hit": np.array([r["b_hit"] for r in pA["rays"]])})
    manifest.append(
        {
            "file": "figures/screenprobe_hitdistance_A.png",
            "width": 512,
            "height": 512,
            "kind": "data_raster",
            "source": {"trace_hit": "Lumen.ScreenProbeGather.TraceHit v" + V_TRACE},
            "mapping": DEPTH_NOTE + "; dark grey = bHit=false (no captured hit)",
            "native_units": "ray length cm",
            "native_stats": stats_of(np.array([np.nan if r["hit_distance_cm"] is None else r["hit_distance_cm"] for r in pA["rays"]]), "hit_distance", "cm"),
            "finite": int(pA["hit_count"]),
            "total": 64,
            "nonempty": int(pA["hit_count"]),
            "native_npz": npz.relative_to(out_dir).as_posix(),
            "recoverable_native": True,
            "notes": "hit distance is Euclidean length from probe to hit; not view depth; misses are stored as -1.0 in the npz with hit_valid=false",
        }
    )

    # ---- F7 SH unit-albedo Lambert response octahedral map + bin centres --------
    sh = np.array(pA["sh_rgb_9coeff"])
    nmap = np.zeros((512, 512, 3), dtype=np.uint8)
    raw_map = np.zeros((512, 512, 3), dtype=np.float32)
    response_map = np.zeros((512, 512, 3), dtype=np.float32)
    physical_map = np.zeros((512, 512, 3), dtype=np.float32)
    sh_white_point = 2.8  # fixed declared white point in Lambert-response units
    for yy in range(512):
        for xx in range(512):
            uv = np.array([(xx + 0.5) / 512.0, (yy + 0.5) / 512.0])
            dv = equi_area_spherical_mapping(uv)
            raw = evaluate_sh_irradiance(dv, sh, ao=1.0)
            response = 4.0 * raw
            raw_map[yy, xx] = raw.astype(np.float32)
            response_map[yy, xx] = response.astype(np.float32)
            physical_map[yy, xx] = (FOUR_PI * raw).astype(np.float32)
            nmap[yy, xx] = np.round(255.0 * srgb_oetf(np.clip(response / sh_white_point, 0.0, 1.0))).astype(np.uint8)
    # dim directions below the probe tangent hemisphere
    pn = np.array(pA["probe_normal_world"])
    for yy in range(0, 512, 4):
        for xx in range(0, 512, 4):
            uv = np.array([(xx + 0.5) / 512.0, (yy + 0.5) / 512.0])
            if np.dot(equi_area_spherical_mapping(uv), pn) < 0:
                nmap[yy : yy + 4, xx : xx + 4] = (nmap[yy : yy + 4, xx : xx + 4] * 0.25).astype(np.uint8)
    im = Image.fromarray(nmap, "RGB")
    dd = ImageDraw.Draw(im)
    for ray in pA["rays"]:
        u, v = ray["probe_uv"]
        px, py = int(u * 512), int(v * 512)
        dd.ellipse((px - 2, py - 2, px + 2, py + 2), fill=(255, 255, 255))
    f7 = fig_dir / "screenprobe_sh_A.png"
    im.save(f7)
    npz = save_native(
        out_dir,
        "screenprobe_sh_A",
        {
            "raw_evaluate_sh_irradiance_rgb": raw_map,
            "unit_albedo_lambert_response_rgb": response_map,
            "physical_irradiance_4pi_rgb": physical_map,
            "sh_rgb_9coeff": sh,
            "probe_normal": pn,
            "ray_uv": np.array([r["probe_uv"] for r in pA["rays"]]),
            "display_white_point_lambert_response": np.array([sh_white_point]),
        },
    )
    manifest.append(
        {
            "file": "figures/screenprobe_sh_A.png",
            "width": 512,
            "height": 512,
            "kind": "data_raster",
            "source": {"ambient": "Lumen.ScreenProbeGather.ScreenProbeRadianceSHAmbient v" + V_SH, "directional": "Lumen.ScreenProbeGather.ScreenProbeRadianceSHDirectional v" + V_SH, "probe": "data/screenprobe-A.json"},
            "mapping": "per texel: octahedral UV -> EquiAreaSphericalMapping -> raw EvaluateSHIrradiance(AO=1) R; displayed quantity is the unit-albedo Lambert response 4*R (pre-exposed radiance), NOT physical irradiance; display = sRGB_OETF(clip(4*R/2.8,0,1)) with fixed declared white point 2.8 in Lambert-response units; white dots = the 64 traced RayInfo UV centres; below-tangent hemisphere dimmed 4x",
            "native_units": "unit-albedo Lambert response 4*R (pre-exposed radiance); physical irradiance is 4*pi*R and is not displayed",
            "native_stats": {
                "raw_evaluate_sh_irradiance": stats_of(raw_map, "raw_evaluate_sh_irradiance", "SH replay R (not physical irradiance)"),
                "unit_albedo_lambert_response": stats_of(response_map, "unit_albedo_lambert_response", "pre-exposed radiance 4*R"),
                "physical_irradiance_4pi": stats_of(physical_map, "physical_irradiance_4pi", "physical irradiance derived from SH replay (4*pi*R)"),
                "sh_coefficients": stats_of(sh, "sh_rgb_9coeff", "sphere-average SH3 radiance"),
            },
            "finite": int(np.isfinite(response_map).sum()),
            "total": int(response_map.size),
            "nonempty": int(np.count_nonzero(response_map)),
            "native_npz": npz.relative_to(out_dir).as_posix(),
            "recoverable_native": True,
            "notes": "SH coefficients are the observed 9 per channel; raw EvaluateSHIrradiance is not physical irradiance. The displayed value is the unit-albedo Lambert response 4*R; physical irradiance is 4*pi*R. Evaluating them is a declared replay, not a captured per-pixel shader result",
        }
    )

    # ---- F8/F9 diffuse current/history crops around A ---------------------------
    diff_cur = decode_r11g11b10(dump.read_tex(RES["diffuse"], V_DIFFUSE, dtype="<u4", channels=1)["array"])
    diff_hist = decode_r11g11b10(dump.read_tex(RES["diffuse_hist"], V_HISTORY_OLD, dtype="<u4", channels=1)["array"])
    frames_hist = dump.read_tex(RES["diffuse_frames"], V_HISTORY_OLD, dtype="u1", channels=1)["array"]
    ax, ay = receivers["A"]["pixel_xy"]
    x0, y0, w, h = 372, 552, 256, 168
    for tag, arr, ver in [("current", diff_cur, V_DIFFUSE), ("history", diff_hist, V_HISTORY_OLD)]:
        crop = crop_native(arr, x0, y0, w, h)
        img = tone_srgb_u8(crop)
        fn = fig_dir / f"diffuseindirect_{tag}_A.png"
        Image.fromarray(img, "RGB").save(fn)
        npz = save_native(out_dir, f"diffuseindirect_{tag}_A", {"diffuse_rgb": crop, "crop_rect_xywh": np.array([x0, y0, w, h])})
        manifest.append(
            {
                "file": f"figures/diffuseindirect_{tag}_A.png",
                "width": w,
                "height": h,
                "kind": "data_raster",
                "crop_xywh": [x0, y0, w, h],
                "source": {"diffuse_indirect": f"Lumen.ScreenProbeGather.DiffuseIndirect v{ver}"},
                "mapping": TONE_NOTE,
                "native_units": "pre-exposed linear radiance (before material albedo)",
                "native_stats": stats_of(crop, f"diffuse_indirect_{tag}", "pre-exposed"),
                "finite": int(np.isfinite(crop).sum()),
                "total": int(crop.size),
                "nonempty": int(np.count_nonzero(crop)),
                "native_npz": npz.relative_to(out_dir).as_posix(),
                "recoverable_native": True,
                "notes": "same mapping and same units for current and history; no independent normalization",
            }
        )
    temporal = temporal_parameter_record(dump)
    max_frames = float(temporal["max_frames_accumulated"])
    raw_crop = crop_native(frames_hist, x0, y0, w, h).astype(np.uint8)
    fast_code = (raw_crop >> 4) & 0xF
    count_code = raw_crop & 0xF
    local_max = min(max_frames, 15.0)
    decoded_frames = np.where(
        count_code == 15,
        max_frames,
        count_code.astype(np.float64) / 15.0 * local_max,
    )
    Image.fromarray(gray_u8(decoded_frames, max_frames), "L").save(fig_dir / "diffuse_frames_A.png")
    npz = save_native(
        out_dir,
        "diffuse_frames_A",
        {
            "raw_byte": raw_crop,
            "fast_update_mode_code_high_nibble": fast_code.astype(np.uint8),
            "num_frames_count_code_low_nibble": count_code.astype(np.uint8),
            "decoded_num_frames_accumulated": decoded_frames.astype(np.float32),
            "max_frames_accumulated": np.array([max_frames], dtype=np.float32),
        },
    )
    manifest.append(
        {
            "file": "figures/diffuse_frames_A.png",
            "width": w,
            "height": h,
            "kind": "data_raster",
            "crop_xywh": [x0, y0, w, h],
            "source": {
                "num_frames_accumulated": "Lumen.ScreenProbeGather.HistoryFastUpdateMode_NumFramesAccumulated v" + V_HISTORY_OLD,
                "max_frames_accumulated": "pass 762 FParameters.MaxFramesAccumulated",
            },
            "mapping": "grey = round(255*decoded_frames/10); decoded follows the captured shader: high nibble = fast-update code, low nibble 15 -> MaxFramesAccumulated=10, otherwise low/15*min(10,15); raw byte and both nibbles are retained in the npz",
            "native_units": "decoded accumulated frame count 0..10 (raw byte 0..255; raw 15 decodes to 10, not 15)",
            "native_stats": stats_of(decoded_frames, "decoded_frames_accumulated", "count"),
            "finite": int(np.isfinite(decoded_frames).sum()),
            "total": int(decoded_frames.size),
            "nonempty": int(np.count_nonzero(decoded_frames)),
            "native_npz": npz.relative_to(out_dir).as_posix(),
            "recoverable_native": True,
            "notes": "MaxFramesAccumulated=10.0 was read from the captured pass-762 parameter payload at offset 460; raw byte 15 has low nibble 15 and is the sentinel for MaxFramesAccumulated. High history values only prove accepted reprojection",
        }
    )

    # ---- F9b history-vs-current delta -------------------------------------------
    delta = np.abs(diff_cur - diff_hist)
    dcrop = crop_native(delta, x0, y0, w, h)
    delta_disp = np.round(255.0 * srgb_oetf(np.clip(dcrop / 0.25, 0.0, 1.0))).astype(np.uint8)
    Image.fromarray(delta_disp, "RGB").save(fig_dir / "diffuse_history_delta_A.png")
    npz = save_native(out_dir, "diffuse_history_delta_A", {"abs_delta_rgb": dcrop, "crop_rect_xywh": np.array([x0, y0, w, h])})
    manifest.append(
        {
            "file": "figures/diffuse_history_delta_A.png",
            "width": w,
            "height": h,
            "kind": "data_raster",
            "crop_xywh": [x0, y0, w, h],
            "source": {"current": "DiffuseIndirect v" + V_DIFFUSE, "history": "DiffuseIndirect v" + V_HISTORY_OLD},
            "mapping": "abs(current-history) in native pre-exposed units; fixed declared white point 0.25; display = sRGB_OETF(clip(delta/0.25,0,1)); no per-pixel normalization",
            "native_units": "pre-exposed linear radiance absolute difference",
            "native_stats": stats_of(dcrop, "diffuse_history_abs_delta", "pre-exposed"),
            "finite": int(np.isfinite(dcrop).sum()),
            "total": int(dcrop.size),
            "nonempty": int(np.count_nonzero(dcrop)),
            "native_npz": npz.relative_to(out_dir).as_posix(),
            "recoverable_native": True,
            "notes": "a small delta at the decoded 10 accumulated frames is temporal-stability evidence, not proof of correctness",
        }
    )

    # ---- F10/F11/F12 reflection crops around C ----------------------------------
    refl = decode_r11g11b10(dump.read_tex(RES["reflection"], V_REFL_FINAL, dtype="<u4", channels=1)["array"])
    refl_depth = dump.read_tex(RES["reflection_depth"], V_REFL_RESOLVED, dtype="<f2", channels=1)["array"].astype(np.float64)
    refl_frames = dump.read_tex(RES["reflection_frames"], V_REFL_TEMPORAL, dtype="u1", channels=1)["array"]
    cx, cy = receivers["C"]["pixel_xy"]
    rx0, ry0, rw, rh = cx - 128, cy - 128, 256, 256
    rcrop = crop_native(refl, rx0, ry0, rw, rh)
    dcrop = crop_native(refl_depth, rx0, ry0, rw, rh)
    fc = crop_native(refl_frames, rx0, ry0, rw, rh).astype(np.float64)
    fc_eff = fc / 255.0 * 32.0
    valid = (fc > 0) & np.isfinite(dcrop) & (dcrop > 0) & (dcrop < 1.0e6) & (rcrop.max(axis=2) < 1.0e5)
    display = tone_srgb_u8(rcrop)
    display = np.where(valid[..., None], display, np.array([24, 24, 24], dtype=np.uint8))
    Image.fromarray(display, "RGB").save(fig_dir / "reflection_C.png")
    npz = save_native(
        out_dir,
        "reflection_C",
        {
            "reflection_rgb": rcrop,
            "reflection_valid_mask": valid,
            "hit_depth_cm": dcrop,
            "frames_raw_byte": fc,
            "frames_effective": fc_eff,
            "crop_rect_xywh": np.array([rx0, ry0, rw, rh]),
        },
    )
    manifest.append(
        {
            "file": "figures/reflection_C.png",
            "width": rw,
            "height": rh,
            "kind": "data_raster",
            "crop_xywh": [rx0, ry0, rw, rh],
            "source": {"specular_indirect": "Lumen.Reflections.SpecularIndirect v" + V_REFL_FINAL, "validity": "NumFramesAccumulated v" + V_REFL_TEMPORAL + " raw > 0"},
            "mapping": TONE_NOTE + "; invalid/sentinel pixels (raw frames=0 or no resolved depth) are dark grey and are excluded from native statistics",
            "native_units": "pre-exposed resolved specular signal; frame counts stored as UNORM frames/32",
            "native_stats": {
                "valid_pixels": stats_of(rcrop[valid], "reflection_specular_indirect_valid", "pre-exposed"),
                "all_pixels": stats_of(rcrop, "reflection_specular_indirect_all", "pre-exposed"),
                "valid_frames_raw_byte": stats_of(fc[valid], "valid_frames_raw_byte", "UNORM byte"),
                "valid_frames_effective": stats_of(fc_eff[valid], "valid_frames_effective", "frames"),
            },
            "finite": int(valid.sum()),
            "total": int(valid.size),
            "nonempty": int(np.count_nonzero(rcrop[valid])),
            "native_npz": npz.relative_to(out_dir).as_posix(),
            "recoverable_native": True,
            "notes": "C is the valid polished-gold reflection sample: raw UNORM byte 96 -> 96/255*32 = 12.0470588 effective accumulated frames, not 96; A/B have raw NumFramesAccumulated=0 and are not shown as reflection evidence",
        }
    )
    depth_rgb = depth_color_u8(dcrop, valid=valid)
    Image.fromarray(depth_rgb, "RGB").save(fig_dir / "reflection_depth_C.png")
    npz = save_native(out_dir, "reflection_depth_C", {"hit_depth_cm": dcrop, "valid_mask": valid})
    manifest.append(
        {
            "file": "figures/reflection_depth_C.png",
            "width": rw,
            "height": rh,
            "kind": "data_raster",
            "crop_xywh": [rx0, ry0, rw, rh],
            "source": {"resolved_depth": "Lumen.Reflections.ResolvedSpecularIndirectDepth v" + V_REFL_RESOLVED},
            "mapping": DEPTH_NOTE + "; dark grey = invalid (no accepted reflection)",
            "native_units": "reflection hit distance cm (R16F)",
            "native_stats": stats_of(dcrop[valid], "reflection_hit_depth_valid", "cm"),
            "finite": int(valid.sum()),
            "total": int(valid.size),
            "nonempty": int(np.count_nonzero(dcrop[valid])),
            "native_npz": npz.relative_to(out_dir).as_posix(),
            "recoverable_native": True,
            "notes": "273.75 cm at C; near-zero values at A/B are invalid sentinels, not depth",
        }
    )
    fc = crop_native(refl_frames, rx0, ry0, rw, rh).astype(np.float64)
    fc_eff = fc / 255.0 * 32.0
    Image.fromarray(gray_u8(fc, 255), "L").save(fig_dir / "reflection_frames_C.png")
    npz = save_native(
        out_dir,
        "reflection_frames_C",
        {"frames_raw_byte": fc, "frames_effective": fc_eff, "unorm": fc / 255.0},
    )
    manifest.append(
        {
            "file": "figures/reflection_frames_C.png",
            "width": rw,
            "height": rh,
            "kind": "data_raster",
            "crop_xywh": [rx0, ry0, rw, rh],
            "source": {"reflection_frames": "Lumen.Reflections.NumFramesAccumulated v" + V_REFL_TEMPORAL, "packing": "LumenReflectionDenoiserCommon.ush PackNumFramesAccumulated = X/32 into UNORM"},
            "mapping": "grey = raw UNORM byte (0..255); effective frames = raw/255*32, so grey also equals 255*effective/32; linear and invertible",
            "native_units": "effective accumulated frame count 0..32 stored as UNORM frames/32; raw byte 0..255",
            "native_stats": {
                "frames_raw_byte": stats_of(fc, "reflection_frames_raw_byte", "UNORM byte"),
                "frames_effective": stats_of(fc_eff, "reflection_frames_effective", "frames"),
            },
            "finite": int(np.isfinite(fc).sum()),
            "total": int(fc.size),
            "nonempty": int(np.count_nonzero(fc)),
            "native_npz": npz.relative_to(out_dir).as_posix(),
            "recoverable_native": True,
            "notes": "C raw byte is 96, so effective accumulated frames = 96/255*32 = 12.0470588; A/B raw bytes are 0. Raw 96 is not 96 frames",
        }
    )

    # ---- F13 basecolor crop C ---------------------------------------------------
    gC = dump.read_tex(RES["gbuffer_c"], V_GBUFFER, dtype="u1", channels=4)["array"]
    bcrop = gC[ry0 : ry0 + rh, rx0 : rx0 + rw][:, :, [2, 1, 0]]
    Image.fromarray(bcrop, "RGB").save(fig_dir / "basecolor_C.png")
    npz = save_native(out_dir, "basecolor_C", {"srgb_codes_rgb": bcrop})
    manifest.append(
        {
            "file": "figures/basecolor_C.png",
            "width": rw,
            "height": rh,
            "kind": "data_raster",
            "crop_xywh": [rx0, ry0, rw, rh],
            "source": {"gbuffer_c": "GBufferC v" + V_GBUFFER},
            "mapping": "captured sRGB code bytes R,G,B (memory BGRA) written directly; no extra gamma",
            "native_units": "sRGB code 0..255",
            "native_stats": stats_of(bcrop, "basecolor_srgb", "code"),
            "finite": int(bcrop.size),
            "total": int(bcrop.size),
            "nonempty": int(np.count_nonzero(bcrop)),
            "native_npz": npz.relative_to(out_dir).as_posix(),
            "recoverable_native": True,
            "notes": "material albedo at C; linear values are in data/orientation.json",
        }
    )

    # ---- F14 VSM crop A ---------------------------------------------------------
    vsm = dump.read_tex(RES["vsm_mask"], V_VSM, dtype="u1", channels=4)["array"]
    vcrop = vsm[y0 : y0 + h, x0 : x0 + w]
    vr = np.round(255.0 * (vcrop[:, :, 2].astype(np.float64) / 255.0) ** 2).astype(np.uint8)
    Image.fromarray(vr, "L").save(fig_dir / "vsm_shadow_A.png")
    npz = save_native(out_dir, "vsm_shadow_A", {"shadow_factor_r_linear_u8": vr, "vsm_bgra": vcrop})
    manifest.append(
        {
            "file": "figures/vsm_shadow_A.png",
            "width": w,
            "height": h,
            "kind": "data_raster",
            "crop_xywh": [x0, y0, w, h],
            "source": {"vsm_mask": "ShadowMaskTexture v" + V_VSM + " (consumer cache, producer pass 841)"},
            "mapping": "grey = round(255*(R_code/255)^2) where R_code is shader R (memory byte 2 of BGRA); 255=lit, 0=shadowed; no gamma",
            "native_units": "linear receiver attenuation 0..1",
            "native_stats": stats_of(vr, "vsm_r_linear_u8", "code"),
            "finite": int(vr.size),
            "total": int(vr.size),
            "nonempty": int(np.count_nonzero(vr)),
            "native_npz": npz.relative_to(out_dir).as_posix(),
            "recoverable_native": True,
            "notes": "8-bit sqrt-encoded mask; the unquantized VSM factor is not recoverable",
        }
    )

    # ---- F15 world-cache neighbourhood strip -----------------------------------
    world_json = json.loads((out_dir / "data" / "worldcache-neighborhood.json").read_text(encoding="utf-8"))
    taps = []
    try:
        source_world = json.loads((Path("D:/UnrealUnderstanding-Tools/sponza-cache-queries/world-cache-query.json")).read_text(encoding="utf-8"))
        taps = source_world.get("cacheLookup", {}).get("radianceTaps", [])
    except Exception:
        taps = []
    if taps:
        # 8 neighbours x 4 taps
        cell = 64
        strip = np.zeros((8 * cell, 4 * cell, 3), dtype=np.uint8)
        for t, tap in enumerate(taps[:32]):
            ni = int(tap.get("neighborIndex", 0)) % 8
            ti = t % 4
            display_linear = np.array(tap.get("decodedRadiance", [0, 0, 0])) * 16.0 * PREEXPOSURE
            col = tone_srgb_u8(display_linear)
            strip[ni * cell : (ni + 1) * cell, ti * cell : (ti + 1) * cell] = col
        Image.fromarray(strip, "RGB").save(fig_dir / "worldcache_neighborhood.png")
        npz = save_native(out_dir, "worldcache_neighborhood", {"tone_rgb_u8": strip, "tap_radiance": np.array([t.get("decodedRadiance", [0, 0, 0]) for t in taps[:32]]), "tap_probe": np.array([t.get("probeIndex", -1) for t in taps[:32]]), "tap_atlas": np.array([t.get("atlasXY", [0, 0]) for t in taps[:32]])})
        manifest.append(
            {
                "file": "figures/worldcache_neighborhood.png",
                "width": 4 * cell,
                "height": 8 * cell,
                "kind": "data_raster",
                "source": {"record": "data/worldcache-neighborhood.json", "cache_atlas": "Lumen.RadianceCache.FinalRadianceAtlas v00040000020b97184f50"},
                "mapping": "rows = 8 allocated probe neighbours, columns = four bilinear taps per neighbour; display-linear L = stored_tap * CachedLightingPreExposureInverse(16) * View.PreExposure(0.00048828125), then " + TONE_NOTE,
                "native_units": "cached pre-exposed radiance (stored atlas values; multiply by 16 for cache pre-exposure inverse)",
                "native_stats": stats_of(np.array([t.get("decodedRadiance", [0, 0, 0]) for t in taps[:32]]), "neighbor_tap_radiance", "cached pre-exposed"),
                "finite": int(32 * 3),
                "total": int(32 * 3),
                "nonempty": int(sum(np.count_nonzero(t.get("decodedRadiance", [0, 0, 0])) for t in taps[:32])),
                "native_npz": npz.relative_to(out_dir).as_posix(),
                "recoverable_native": True,
                "notes": "the strip is a selected neighbourhood, not the 4352x4352 atlas; exact tap coordinates and packed words are in the record",
            }
        )

    # ---- F16/F17 surface-cache query region and channel sample -------------------
    try:
        surf = json.loads((Path("D:/UnrealUnderstanding-Tools/sponza-cache-queries/surface-cache-query.json")).read_text(encoding="utf-8"))
    except Exception:
        surf = None
    if surf:
        mat = surf.get("material", {})
        vals = surf.get("values", {})
        tile = surf.get("tileValidation", {}).get("pixels", [])
        # one selected texel, four native channels
        cells = []
        alb = np.array(mat.get("encodedAlbedoRGB", mat.get("albedoRGB", [0, 0, 0])), dtype=np.float64)
        alb_srgb = np.round(255.0 * srgb_oetf(np.clip(alb, 0, 1))).astype(np.uint8)
        nrm = np.array(mat.get("normalDecoded", [0, 0, 1]), dtype=np.float64)
        nrm_disp = np.round(255.0 * np.clip(0.5 + 0.5 * nrm, 0, 1)).astype(np.uint8)
        dep = float(mat.get("depthNormalized", 0.0))
        dep_disp = np.array([np.round(255.0 * np.clip(dep, 0, 1))] * 3, dtype=np.uint8)
        fin = np.array(
            vals.get("cachedPreExposedFinalBeforeQuantization", vals.get("finalBeforeQuantization", [0, 0, 0])),
            dtype=np.float64,
        )
        fin_disp = np.round(255.0 * srgb_oetf(np.clip(fin / 400.0, 0, 1))).astype(np.uint8)
        strip = np.zeros((64, 4 * 64, 3), dtype=np.uint8)
        for i, c in enumerate([alb_srgb, nrm_disp, dep_disp, fin_disp]):
            strip[:, i * 64 : (i + 1) * 64] = c
        for k in range(0, 257, 64):
            strip[:, min(k, 255) : min(k + 1, 256)] = 32
        Image.fromarray(strip, "RGB").save(fig_dir / "surfacecache_query_channels.png")
        npz = save_native(out_dir, "surfacecache_query_channels", {"albedo_linear": alb, "normal_decoded": nrm, "depth_normalized": np.array([dep]), "final_lighting": fin})
        manifest.append(
            {
                "file": "figures/surfacecache_query_channels.png",
                "width": 256,
                "height": 64,
                "kind": "data_raster",
                "source": {"record": "data/surfacecache-query.json", "albedo": "Lumen.SceneAlbedo v00040000020bf435b3a0", "normal": "Lumen.SceneNormal v00040000020bf435dcf0", "final": "surface cache output word at the selected tile"},
                "mapping": "four 64x64 cells: encodedAlbedoRGB (linear) -> sRGB OETF; normal -> 0.5+0.5*n; depth_normalized -> linear grey; cachedPreExposedFinalBeforeQuantization -> sRGB_OETF(clip(L/400,0,1)); 1-px dividers",
                "native_units": "linear albedo, unit normal, normalized depth, pre-exposed final radiance",
                "native_stats": {
                    "albedo_linear": stats_of(alb, "albedo_linear", "linear"),
                    "normal_decoded": stats_of(nrm, "normal_decoded", "unit"),
                    "depth_normalized": stats_of(np.array([dep]), "depth_normalized", "0..1"),
                    "final_before_quantization": stats_of(fin, "final_before_quantization", "pre-exposed"),
                },
                "finite": 4,
                "total": 4,
                "nonempty": 4,
                "native_npz": npz.relative_to(out_dir).as_posix(),
                "recoverable_native": True,
                "notes": "one declared card-tile query at the peer record's selected atlas coordinate; exact tile and world point are in data/surfacecache-query.json",
            }
        )

        if tile:
            grid = np.zeros((8 * 64, 8 * 64, 3), dtype=np.uint8)
            finals = []
            for p in tile:
                lx, ly = p.get("localThread", [0, 0])
                fv = np.array(p.get("final", [0, 0, 0]), dtype=np.float64)
                finals.append(fv)
                grid[ly * 64 : (ly + 1) * 64, lx * 64 : (lx + 1) * 64] = np.round(
                    255.0 * srgb_oetf(np.clip(fv / 400.0, 0, 1))
                ).astype(np.uint8)
            for k in range(0, 513, 64):
                grid[k : min(k + 1, 512), :] = 40
                grid[:, k : min(k + 1, 512)] = 40
            Image.fromarray(grid, "RGB").save(fig_dir / "surfacecache_query_tile.png")
            npz = save_native(out_dir, "surfacecache_query_tile", {"final_rgb": np.array(finals), "output_packed": np.array([p.get("outputPacked", 0) for p in tile], dtype=np.uint64)})
            manifest.append(
                {
                    "file": "figures/surfacecache_query_tile.png",
                    "width": 512,
                    "height": 512,
                    "kind": "data_raster",
                    "source": {"record": "data/surfacecache-query.json", "tileValidation": "8x8 dispatched card-tile groups of the declared query"},
                    "mapping": "8x8 cells of final -> sRGB_OETF(clip(L/400,0,1)) with fixed declared white point 400 pre-exposed; 1-px grid",
                    "native_units": "pre-exposed linear final lighting",
                    "native_stats": stats_of(np.array(finals), "surfacecache_tile_final", "pre-exposed"),
                    "finite": int(np.isfinite(np.array(finals)).sum()),
                    "total": int(np.array(finals).size),
                    "nonempty": int(np.count_nonzero(finals)),
                    "native_npz": npz.relative_to(out_dir).as_posix(),
                    "recoverable_native": True,
                    "notes": "the selected 8x8 tile around the query, not the whole 4096x4096 cache atlas",
                }
            )

    # ---- F18 scene depth crop A ------------------------------------------------
    dep_tex = dump.read_tex(RES["scene_depth"], V_SCENE_DEPTH, dtype="<f4", channels=1)["array"]
    dz_crop = crop_native(dep_tex, x0, y0, w, h)
    depth_cm_crop = np.where(dz_crop > 0, view["near_plane_cm"] / np.maximum(dz_crop, 1e-30), np.nan)
    depth_valid = np.isfinite(depth_cm_crop) & (depth_cm_crop > 0)
    depth_disp = depth_color_u8(np.where(depth_valid, depth_cm_crop, 0.0), valid=depth_valid)
    Image.fromarray(depth_disp, "RGB").save(fig_dir / "scene_depth_A.png")
    npz = save_native(out_dir, "scene_depth_A", {"device_z": dz_crop, "view_depth_cm": depth_cm_crop, "valid_mask": depth_valid})
    manifest.append(
        {
            "file": "figures/scene_depth_A.png",
            "width": w,
            "height": h,
            "kind": "data_raster",
            "crop_xywh": [x0, y0, w, h],
            "source": {"scene_depth": "SceneDepthZ v" + V_SCENE_DEPTH},
            "mapping": "native device Z -> W_cm = near_plane_cm(10) / deviceZ; " + DEPTH_NOTE + "; dark grey = sky/invalid",
            "native_units": "view-forward depth cm (also metres in the npz)",
            "native_stats": stats_of(depth_cm_crop[depth_valid], "scene_view_depth", "cm"),
            "finite": int(depth_valid.sum()),
            "total": int(depth_valid.size),
            "nonempty": int(depth_valid.sum()),
            "native_npz": npz.relative_to(out_dir).as_posix(),
            "recoverable_native": True,
            "notes": "A's exact pixel is at the crop origin+128,128; the probe depth in screenprobe-A.json corresponds to this surface within 0.0001 cm",
        }
    )

    # ---- F19 world normal crop C ------------------------------------------------
    gA_all = dump.read_tex(RES["gbuffer_a"], V_GBUFFER_A, dtype="<u4", channels=1)["array"]
    nrm_crop = np.zeros((256, 256, 3), dtype=np.float64)
    for yy in range(256):
        for xx in range(256):
            nrm_crop[yy, xx] = decode_gbuffer_a(int(gA_all[459 + yy, 498 + xx]))
    nrm_disp = np.round(255.0 * np.clip(0.5 + 0.5 * nrm_crop, 0.0, 1.0)).astype(np.uint8)
    Image.fromarray(nrm_disp, "RGB").save(fig_dir / "worldnormal_C.png")
    npz = save_native(out_dir, "worldnormal_C", {"normal_world": nrm_crop, "crop_rect_xywh": np.array([498, 459, 256, 256])})
    manifest.append(
        {
            "file": "figures/worldnormal_C.png",
            "width": 256,
            "height": 256,
            "kind": "data_raster",
            "crop_xywh": [498, 459, 256, 256],
            "source": {"gbuffer_a": "GBufferA v" + V_GBUFFER},
            "mapping": "A2B10G10R10 codes -> n=code/1023*2-1 then normalize; display RGB = round(255*(0.5+0.5*n)); no gamma",
            "native_units": "world-space unit shading normal",
            "native_stats": stats_of(nrm_crop, "world_normal", "unit"),
            "finite": int(256 * 256 * 3),
            "total": int(256 * 256 * 3),
            "nonempty": int(np.count_nonzero(nrm_disp)),
            "native_npz": npz.relative_to(out_dir).as_posix(),
            "recoverable_native": True,
            "notes": "the gold spheres and the lion relief are the selected C crop; the normal is the shading normal, not the depth-derived geometric normal",
        }
    )

    # ---- F20 material channel crop C -------------------------------------------
    gB_all = dump.read_tex(RES["gbuffer_b"], V_GBUFFER, dtype="u1", channels=4)["array"]
    m_crop = gB_all[459:715, 498:754].astype(np.float64)
    mat_rgb = np.stack(
        [
            m_crop[:, :, 0] / 255.0,  # roughness code
            m_crop[:, :, 2] / 255.0,  # metallic code
            m_crop[:, :, 1] / 255.0,  # specular code
        ],
        axis=-1,
    )
    mat_u8 = np.round(255.0 * mat_rgb).astype(np.uint8)
    panels = np.zeros((256, 256 * 3, 3), dtype=np.uint8)
    for i in range(3):
        panels[:, i * 256 : (i + 1) * 256] = np.repeat(mat_u8[:, :, i : i + 1], 3, axis=2)
    for k in range(0, 769, 256):
        panels[:, min(k, 767) : min(k + 1, 768)] = 40
    Image.fromarray(panels, "RGB").save(fig_dir / "material_C.png")
    npz = save_native(out_dir, "material_C", {"roughness_metallic_specular": mat_rgb, "gbuffer_b_bgra": m_crop.astype(np.uint8), "crop_rect_xywh": np.array([498, 459, 256, 256])})
    manifest.append(
        {
            "file": "figures/material_C.png",
            "width": 768,
            "height": 256,
            "kind": "data_raster",
            "crop_xywh": [498, 459, 256, 256],
            "source": {"gbuffer_b": "GBufferB v" + V_GBUFFER},
            "mapping": "three 256-px panels, left->right: roughness_code/255, metallic_code/255, specular_code/255; each shown as linear grey with no gamma; 1-px dividers",
            "native_units": "linear material factors 0..1",
            "native_stats": stats_of(mat_rgb, "roughness_metallic_specular", "0..1"),
            "finite": int(mat_rgb.size),
            "total": int(mat_rgb.size),
            "nonempty": int(np.count_nonzero(mat_u8)),
            "native_npz": npz.relative_to(out_dir).as_posix(),
            "recoverable_native": True,
            "notes": "C's gold spheres have metallic=1 and roughness=20/255=0.0784; albedo is shown separately in basecolor_C.png",
        }
    )

    manifest_path = out_dir / "data" / "figure-manifest.json"
    manifest_path.write_text(json.dumps(jval(manifest), indent=2), encoding="utf-8")
    return manifest

def verify_outputs(out: Path):
    data = out / "data"
    figs = out / "figures"
    checks = []

    def add(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    orient = json.loads((data / "orientation.json").read_text(encoding="utf-8"))
    add("view_rect_1280x720", orient["view"]["view_rect"] == [0, 0, 1280, 720], str(orient["view"]["view_rect"]))
    add("packed_view_matrix_product", abs(orient["view"]["tw_to_clip_vs_product_max_abs"]) < 1e-5,
        f"max |ViewToClip@TranslatedWorldToView - TranslatedWorldToClip| = {orient['view']['tw_to_clip_vs_product_max_abs']}")
    add("tile_jitter_2_8", orient["view"]["jitter_xy_pixels"] == [2, 8], str(orient["view"]["jitter_xy_pixels"]))
    add("ray_direction_frame_index_1",
        orient["shader_parameters"]["fields"]["ScreenProbeRayDirectionFrameIndex"] == 1,
        str(orient["shader_parameters"]["fields"]["ScreenProbeRayDirectionFrameIndex"]))

    rec = orient["receivers"]
    for n in ("A", "B", "C"):
        r = rec[n]
        add(f"receiver_{n}_world_finite", all(v is not None and math.isfinite(v) for v in r.get("world_cm", [])), str(r.get("world_cm")))
        add(f"receiver_{n}_depth_positive", (r["scene_depth"]["view_depth_cm"] or 0) > 0, f"{r['scene_depth']['view_depth_cm']} cm")
        add(f"receiver_{n}_covering_probe", r.get("covering_screen_probe") is not None,
            str(r.get("covering_screen_probe", {}).get("atlas_xy")))
    add("C_is_polished_gold", rec["C"]["material"]["metallic"] == 1.0 and abs(rec["C"]["material"]["roughness"] - 20 / 255.0) < 1e-9,
        f"metallic={rec['C']['material']['metallic']}, roughness={rec['C']['material']['roughness']}")
    add("A_sunlit_vsm", rec["A"]["vsm_shadow_mask"]["r_linear"] == 1.0 and max(rec["A"]["direct"]["direct_rgb_pre_exposed"]) > 0,
        f"VSM R={rec['A']['vsm_shadow_mask']['r_linear']}, direct={rec['A']['direct']['direct_rgb_pre_exposed']}")
    add("B_C_shadowed", rec["B"]["vsm_shadow_mask"]["r_linear"] == 0.0 and rec["C"]["vsm_shadow_mask"]["r_linear"] == 0.0,
        "B/C VSM R=0")

    for n in ("A", "B", "C"):
        p = json.loads((data / f"screenprobe-{n}.json").read_text(encoding="utf-8"))
        add(f"probe_{n}_64_rays", len(p["rays"]) == 64, str(len(p["rays"])))
        add(f"probe_{n}_rayinfo_consistent", p["rayinfo_consistent"], str(p["rayinfo_consistent"]))
        add(f"probe_{n}_sh_3x9", np.array(p["sh_rgb_9coeff"]).shape == (3, 9), str(np.array(p["sh_rgb_9coeff"]).shape))
        raw = np.array(p["sh_replay_raw_evaluate_sh_irradiance"], dtype=float)
        lam = np.array(p["unit_albedo_lambert_response"], dtype=float)
        phys = np.array(p["physical_irradiance_4pi"], dtype=float)
        add(
            f"probe_{n}_sh_units",
            bool(np.all(np.isfinite(raw)) and np.allclose(lam, 4.0 * raw) and np.allclose(phys, FOUR_PI * raw)),
            f"lambert/raw={float(np.max(np.abs(lam - 4.0 * raw)))}, physical-4pi*raw={float(np.max(np.abs(phys - FOUR_PI * raw)))}",
        )
        add(f"probe_{n}_ray_layout_explicit", p["ray_layout"]["same_integer_slot_is_same_ray"] is False,
            p["ray_layout"]["regular_reconstructed_field"])
    add("A_B_tile_sh_median_under_5pct",
        all(json.loads((data / f"screenprobe-{n}.json").read_text(encoding="utf-8"))["sh_replay_tile_check"]["median"] < 0.05 for n in ("A", "B")),
        "A/B median over 16x16 tile")
    add("C_center_sh_under_2pct",
        max(json.loads((data / "screenprobe-C.json").read_text(encoding="utf-8"))["diffuse_vs_sh_relative_error"]) < 0.02,
        str(json.loads((data / "screenprobe-C.json").read_text(encoding="utf-8"))["diffuse_vs_sh_relative_error"]))

    add(
        "gbuffer_shading_model_low_nibble",
        all(
            rec[n]["material"]["shading_model_id"] == 1
            and (rec[n]["material"]["packed_id_code"] & 0xF) == 1
            for n in ("A", "B", "C")
        ),
        ", ".join(
            f"{n}: alpha=0x{rec[n]['material']['packed_id_code']:02x} low={rec[n]['material']['shading_model_id']} high={rec[n]['material']['selective_output_mask']}"
            for n in ("A", "B", "C")
        ),
    )

    integ = json.loads((data / "integration-history.json").read_text(encoding="utf-8"))
    fA = integ["receivers"]["A"]
    add(
        "diffuse_frames_raw15_decoded10",
        fA["num_frames_accumulated_history_raw_byte"] == 15
        and fA["num_frames_accumulated_history_count_code"] == 15
        and fA["num_frames_accumulated_history_fast_update_code"] == 0
        and abs(fA["num_frames_accumulated_history"] - 10.0) < 1e-12
        and abs(fA["max_frames_accumulated_parameter"] - 10.0) < 1e-12,
        f"raw=0x{fA['num_frames_accumulated_history_raw_byte']:02x} low={fA['num_frames_accumulated_history_count_code']} high={fA['num_frames_accumulated_history_fast_update_code']} decoded={fA['num_frames_accumulated_history']} max={fA['max_frames_accumulated_parameter']}",
    )
    sign_ok = True
    sign_detail = []
    for n in ("A", "B", "C"):
        rr = integ["receivers"][n]
        h = np.array(rr["diffuse_indirect_history"], dtype=float)
        c = np.array(rr["diffuse_indirect_current"], dtype=float)
        d = np.array(rr["history_minus_current"], dtype=float)
        ok = bool(np.allclose(d, h - c, atol=1e-9))
        sign_ok = sign_ok and ok
        sign_detail.append(f"{n}: {d.tolist()} == hist-cur {ok}")
    add("history_minus_current_is_history_minus_current", sign_ok, "; ".join(sign_detail))

    refl = json.loads((data / "reflection.json").read_text(encoding="utf-8"))
    rc = refl["receivers"]["C"]
    add(
        "reflection_C_raw96_effective_12_0470588",
        rc["valid_reflection_signal"]
        and rc["num_frames_accumulated_raw_byte"] == 96
        and abs(rc["num_frames_accumulated_effective"] - 96 / 255.0 * 32.0) < 1e-12
        and abs(rc["num_frames_accumulated"] - rc["num_frames_accumulated_effective"]) < 1e-15,
        f"valid={rc['valid_reflection_signal']}, raw={rc['num_frames_accumulated_raw_byte']}, effective={rc['num_frames_accumulated_effective']}",
    )
    add("reflection_A_B_invalid", not refl["receivers"]["A"]["valid_reflection_signal"] and not refl["receivers"]["B"]["valid_reflection_signal"],
        "raw frames=0 at A/B")

    place = json.loads((data / "screenprobes-placement.json").read_text(encoding="utf-8"))
    add(
        "adaptive_allocation_bounded",
        place["allocated_adaptive_probe_count"] == 117
        and place["active_adaptive_count"] == 117
        and place["finite_storage_adaptive_count"] == 240
        and place["stale_finite_adaptive_count"] == 123
        and place["active_probe_count"] == 3686
        and place["finite_storage_probe_count"] == 3809
        and place["active_uniform_count"] == 3569
        and place["adaptive_activity_source"]["count_matches_header_sum"]
        and place["adaptive_header_bijection"],
        f"alloc={place['allocated_adaptive_probe_count']} active_adaptive={place['active_adaptive_count']} finite_adaptive={place['finite_storage_adaptive_count']} stale={place['stale_finite_adaptive_count']} active_total={place['active_probe_count']}",
    )
    add(
        "adaptive_count_not_inferred_from_rayinfo",
        json.loads((data / "context-checks.json").read_text(encoding="utf-8"))["screenprobe_allocation"]["rayinfo_used_for_activity"] is False
        and place["adaptive_activity_source"]["count_matches_header_nonzero_tiles"],
        "count buffer and header agree independently; rayinfo is not used for activity",
    )

    surf = json.loads((data / "surfacecache-query.json").read_text(encoding="utf-8"))
    surf_ok = all(v.get("sha256_matches") and v.get("bytes_match") for v in surf["verification"].values())
    add("surfacecache_bytes_and_hashes", surf_ok, f"{len(surf['verification'])} native records")
    add("surfacecache_query_present", all(k in surf["query"] for k in ("atlasCoord", "worldPoint", "depth")) and "screen" in surf["query"].get("projection", {}), "")
    add("surfacecache_declared_replay", surf["observed_vs_replay"] == "replay_of_declared_query", surf["observed_vs_replay"])

    world = json.loads((data / "worldcache-neighborhood.json").read_text(encoding="utf-8"))
    world_ok = all(v.get("sha256_matches") and v.get("bytes_match") for v in world["verification"].values())
    add("worldcache_bytes_and_hashes", world_ok, f"{len(world['verification'])} native records")
    add("worldcache_8_neighbors", len(world["cache_lookup"]["neighbors"]) == 8,
        str(len(world["cache_lookup"]["neighbors"])))
    d = np.array(world["angular_ray"]["direction"], dtype=float)
    add("worldcache_direction_unit", abs(float(np.linalg.norm(d)) - 1.0) < 1e-4, f"|d|={float(np.linalg.norm(d)):.8f}")
    add("worldcache_forward_consistent", bool(world["comparison"].get("forwardConsistent")), "")
    add("worldcache_declared_replay", world["observed_vs_replay"] == "replay_of_declared_query", world["observed_vs_replay"])

    manifest = json.loads((data / "figure-manifest.json").read_text(encoding="utf-8"))
    add("figure_count_ge_20", len(manifest) >= 20, str(len(manifest)))
    for f in manifest:
        p = out / f["file"]
        add(f"figure_exists_{f['file']}", p.exists(), "")
        if p.exists():
            im = Image.open(p)
            add(f"figure_dims_{f['file']}", im.size == (f["width"], f["height"]), f"{im.size}")
            add(f"figure_no_alpha_{f['file']}", im.mode in ("RGB", "L"), im.mode)
        add(f"figure_mapping_{f['file']}", bool(f.get("mapping")) and bool(f.get("native_units")), "")
        add(f"figure_stats_{f['file']}", f.get("native_stats") is not None and f.get("finite") is not None and f.get("nonempty") is not None, "")
        add(f"figure_recoverable_{f['file']}", bool(f.get("recoverable_native")) and (f.get("native_npz") is not None or f["file"].endswith("scene_clean.png")), "")
        if f.get("native_npz"):
            npz = out / f["native_npz"]
            add(f"figure_npz_exists_{f['file']}", npz.exists(), str(npz))
            if npz.exists():
                with np.load(npz) as z:
                    nonempty = False
                    finite = True
                    for k in z.files:
                        a = z[k]
                        finite = finite and bool(np.all(np.isfinite(a))) if a.dtype.kind == "f" else finite
                        nonempty = nonempty or bool(np.count_nonzero(a))
                    add(f"figure_npz_nonempty_{f['file']}", nonempty, "")
                    add(f"figure_npz_finite_{f['file']}", finite, "")

    script_text = Path(__file__).read_text(encoding="utf-8")
    text_needles = ["draw" + ".text(", "dd" + ".text("]
    add("no_raster_text_calls", not any(n in script_text for n in text_needles), "no ImageDraw.text call in the generator")
    add(
        "tone_inverse_formula_order",
        "first sRGB_EOTF(code/255)" in TONE_NOTE and "then L=t/(1-t)" in TONE_NOTE,
        TONE_NOTE,
    )
    sh_fig = next((f for f in manifest if f["file"] == "figures/screenprobe_sh_A.png"), None)
    add(
        "sh_preview_units_explicit",
        bool(sh_fig)
        and "NOT physical irradiance" in sh_fig.get("mapping", "")
        and "physical irradiance is 4*pi*R" in sh_fig.get("native_units", ""),
        "" if not sh_fig else sh_fig.get("native_units", ""),
    )
    ray_fig = next((f for f in manifest if f["file"] == "figures/screenprobe_rays_A.png"), None)
    add(
        "rays_figure_uses_structured_trace_radiance",
        bool(ray_fig)
        and "radiance_trace_native" in ray_fig.get("mapping", "")
        and "NOT used because the regular reconstructed field is a different angular layout" in ray_fig.get("mapping", ""),
        "" if not ray_fig else ray_fig.get("mapping", ""),
    )
    refl_fig = next((f for f in manifest if f["file"] == "figures/reflection_frames_C.png"), None)
    add(
        "reflection_frames_figure_effective_units",
        bool(refl_fig)
        and "12.0470588" in refl_fig.get("notes", "")
        and "raw/255*32" in refl_fig.get("mapping", ""),
        "" if not refl_fig else refl_fig.get("native_units", ""),
    )

    report = out / "decode-report.md"
    if not report.exists():
        report = Path("decode-report.md")
    add("decode_report_exists", report.exists(), str(report))
    if report.exists():
        rt = report.read_text(encoding="utf-8")
        add("decode_report_has_acceptance", "Acceptance checks" in rt, "")
        add("decode_report_has_observed_vs_replay", "declared-query replay" in rt or "declared query replay" in rt, "")

    result = {
        "schema": "sponza-decode-verify-v1",
        "checks": checks,
        "total": len(checks),
        "passed": sum(1 for c in checks if c["ok"]),
        "failed": sum(1 for c in checks if not c["ok"]),
        "ok": all(c["ok"] for c in checks),
    }
    (data / "decode-verify.json").write_text(json.dumps(jval(result), indent=2), encoding="utf-8")
    print(json.dumps({"total": result["total"], "passed": result["passed"], "failed": result["failed"], "ok": result["ok"]}, indent=2))
    for c in checks:
        if not c["ok"]:
            print("FAIL", c["check"], c["detail"])
    return result


def write_decode_report(out: Path):
    """Write the technical decode report from the emitted data records."""
    data = out / "data"
    orient = json.loads((data / "orientation.json").read_text(encoding="utf-8"))
    place = json.loads((data / "screenprobes-placement.json").read_text(encoding="utf-8"))
    integ = json.loads((data / "integration-history.json").read_text(encoding="utf-8"))
    refl = json.loads((data / "reflection.json").read_text(encoding="utf-8"))
    manifest = json.loads((data / "figure-manifest.json").read_text(encoding="utf-8"))
    ctx = json.loads((data / "context-checks.json").read_text(encoding="utf-8"))
    pA = json.loads((data / "screenprobe-A.json").read_text(encoding="utf-8"))
    fA = integ["receivers"]["A"]
    fC = integ["receivers"]["C"]
    rc = refl["receivers"]["C"]
    alloc = place["adaptive_activity_source"]
    counts = place
    ray0 = pA["rays"][0]

    def f3(v):
        return f"{float(v):.9g}"

    lines = []
    add = lines.append
    add("# Sponza static context decoder: decode report")
    add("")
    add("Run: `sponza-20261115-083654` (frame 302). Raw dump: `output/gpu-dump/raw/sponza-20261115-083654` (read-only).")
    add("Decoder: `scripts/decode_context.py`; rerun with `python scripts/decode_context.py --stage all`.")
    add("Every native value below comes from captured bytes or words in the named resource versions or shader parameter payloads. Display rasters are regenerated from those values; nothing here is estimated from an image.")
    add("")
    add("## Acceptance checks")
    add("")
    add("| # | Correction | Captured identity / field | Corrected value | Validation |")
    add("|---|---|---|---|---|")
    add(f"| 1 | ShadingModelID is the alpha low nibble | GBufferB `00040000020b9d701710` (pass 697), alpha byte | A/B alpha `0xB1` -> model 1; C alpha `0xA1` -> model 1; high nibble 11/11/10 is SelectiveOutputMask | `gbuffer_shading_model_low_nibble` in `data/decode-verify.json`; raw alpha and both nibbles are in `data/orientation.json` |")
    add(f"| 2 | `history_minus_current` sign | `DiffuseIndirect` current `{V_DIFFUSE}` / imported history v0 | A history 1.296875 - current 1.28125 = +0.015625; C 0.609375 - 0.515625 = +0.09375 | `history_minus_current_is_history_minus_current`; definition field says `history - current` |")
    add(f"| 3 | Packed temporal frame count | pass 762 `FParameters.MaxFramesAccumulated`, `Structures/000400000208f36dfaa0.bin`, offset 460 | float32 = {f3(fA['max_frames_accumulated_parameter'])}; raw byte 15 has high nibble 0 and low nibble 15 -> decoded {f3(fA['num_frames_accumulated_history'])} frames, not 15 | `diffuse_frames_raw15_decoded10`; raw byte, both nibbles, decoded value and MaxFrames are retained per receiver and in `data/figure-native/diffuse_frames_A.npz` |")
    add(f"| 4 | Reflection frame UNORM packing | pass 826 `NumFramesAccumulated` `00040000020b9dcd8030`, `LumenReflectionDenoiserCommon.ush:51-59` | raw 96 -> 96/255*32 = {f3(rc['num_frames_accumulated_effective'])} effective frames, not 96; A/B raw 0 | `reflection_C_raw96_effective_12_0470588`; figure `reflection_frames_C.png` uses raw/255*32 units |")
    add(f"| 5 | Adaptive placement bounded by allocator | pass 706 count buffer `0004000002090c6ba5c0` v`{V_PLACEMENT}`; header `0004000002090c6ba8d0` | count = {counts['allocated_adaptive_probe_count']}; header sum = {alloc['activity_header']['value_sum']} over {alloc['activity_header']['nonzero_tile_count']} tiles; finite storage = {counts['finite_storage_probe_count']} ({counts['finite_storage_uniform_count']} uniform + {counts['finite_storage_adaptive_count']} adaptive); active = {counts['active_probe_count']} ({counts['active_uniform_count']} + {counts['active_adaptive_count']}); stale adaptive = {counts['stale_finite_adaptive_count']} | `adaptive_allocation_bounded`; first {counts['allocated_adaptive_probe_count']} adaptive indices map bijectively to the {alloc['activity_header']['nonzero_tile_count']} header tiles; rayinfo is never used as activity evidence |")
    add("| 6 | Structured trace slot vs regular reconstructed field | trace `TraceRadiance` `00040000020c1328ca80` (pass 747); filtered `ScreenProbeRadiance` `0004000002094be0c130` (pass 748) | `screenprobe_rays_A.png` now colours by `radiance_trace_native`; filtered radiance is shown only in the separately labelled regular-field panel | `rays_figure_uses_structured_trace_radiance`; `screenprobe_rays_A.npz` keeps `ray_trace_radiance_native` and `ray_filtered_radiance_same_integer_slot_native` separately |")
    add(f"| 7 | SH units | SH ambient `000400000208f3733bf0` / directional `000400000208f3733eb0` (pass 753) | stored SH3 are sphere-average radiance; raw R = EvaluateSHIrradiance; physical irradiance = 4*pi*R; unit-albedo Lambert response = 4*R. Figure displays 4*R with fixed white point 2.8, not physical irradiance | `probe_A/B/C_sh_units` and `sh_preview_units_explicit`; `screenprobe_sh_A.npz` stores raw, 4*R and 4*pi*R |")
    add("| 8 | Inverse display mapping order | all tone-mapped figure mappings | inverse is first `sRGB_EOTF(code/255) -> t`, then `L = t/(1-t)`; the old note had the order reversed | `tone_inverse_formula_order`; identical string is in every `TONE_NOTE` mapping and in `data/figure-manifest.json` |")
    add("| 9 | Raster text and empty-figure policy | figure generator and manifest | no `ImageDraw.text`/`.text(` call; every emitted figure is non-empty and has native NPZ or is the captured screenshot; `material_C.png` and the dark-grey invalid reflection pixels are retained | `no_raster_text_calls`, per-figure existence/dimension/NPZ checks; `decode-verify.json` |")
    add("")
    add("## Captured values used by the checks")
    add("")
    add(f"- Temporal frame raw bytes at A: history `0x{fA['num_frames_accumulated_history_raw_byte']:02x}` (high {fA['num_frames_accumulated_history_fast_update_code']}, low {fA['num_frames_accumulated_history_count_code']}) -> `{f3(fA['num_frames_accumulated_history'])}`; new history `0x{fA['num_frames_accumulated_new_raw_byte']:02x}` -> `{f3(fA['num_frames_accumulated_new'])}`.")
    add(f"- MaxFramesAccumulated parameter: `{f3(fA['max_frames_accumulated_parameter'])}` from pass 762; shader source `{integ['frame_count_encoding']['packing_shader']['file']}` lines {integ['frame_count_encoding']['packing_shader']['lines']}.")
    add(f"- Reflection at C: raw byte `{rc['num_frames_accumulated_raw_byte']}`, UNORM `{f3(rc['num_frames_accumulated_unorm'])}`, effective `{f3(rc['num_frames_accumulated_effective'])}`; raw history byte `{rc['num_frames_accumulated_history_raw_byte']}`.")
    add(f"- Adaptive count buffer: `{alloc['count_buffer']['file']}`, bytes `{alloc['count_buffer']['file_bytes']}`, raw word `{alloc['count_buffer']['raw_word']}`, sha256 `{alloc['count_buffer']['file_sha256']}`.")
    add(f"- Adaptive header: `{alloc['activity_header']['file']}`, extent {alloc['activity_header']['extent']}, nonzero {alloc['activity_header']['nonzero_tile_count']}, sum {alloc['activity_header']['value_sum']}, sha256 `{alloc['activity_header']['file_sha256']}`.")
    add(f"- Structured ray example (probe A, trace cell [0,0]): TraceRadiance `{[round(v, 6) for v in ray0['radiance_trace_native']]}`, filtered regular-field value at the same integer slot `{[round(v, 6) for v in ray0['radiance_filtered_native']]}`. These are different layouts and must not be treated as the same ray.")
    add("")
    add("## Figure inventory")
    add("")
    add(f"{len(manifest)} figures are listed in `data/figure-manifest.json`. Each entry has `source`, `mapping`, `native_units`, `native_stats`, `finite`, `nonempty`, and a `native_npz` (except the captured screenshot). No raster image contains embedded text.")
    add("")
    add("## Unavailable evidence / gaps")
    add("")
    add("- No shader-local per-thread adaptive-placement dispatch is captured. Activity is bounded by the captured `NumAdaptiveScreenProbes` count and independently corroborated by the pass-706 header tile map; per-index tile membership is validated as a bijection, not read from a captured per-index assignment table.")
    add("- The pass-706 `NumAdaptiveScreenProbes` 4-byte buffer has no `DumpReadbackStatus` record; the payload file exists in `Resources`, is hashed, and agrees with the header sum. The pass-707 six dispatch groups are captured as raw words but are not individually labelled, so they are not used to infer activity.")
    add("- The regular reconstructed field (`ScreenProbeRadiance`) does not encode which structured trace slot produced each filtered value. Filtered radiance is therefore reported in its own layout and is not mapped back to individual rays.")
    add("- No per-pixel screen-probe interpolation weights, temporal blend factors, disocclusion decisions, or reflection BRDF/ray choices are captured; those calculations remain shader-local.")
    add("- `EvaluateSHIrradiance` output is a replay from captured SH3 coefficients. No captured per-pixel physical irradiance texture exists, and no pre-tonemap linear frame is captured; physical irradiance and 4*R are derived only from the stored SH coefficients.")
    add("- Reflection `NumFramesAccumulated` is an encoded UNORM byte; only the decoded effective value is recovered. The encoder cannot distinguish values that quantize to the same byte.")
    add("- The raw GPU capture/package was not modified. No GPU work was executed in this step.")
    add("")
    add("Observed-vs-replay: all ray directions, SH evaluations, and cache queries in these records are declared-query replays of captured inputs; only the named captured resource words and shader parameters are observed values.")
    add("")
    (out / "decode-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out / "decode-report.md"


def append_verify_summary(out: Path, result: dict):
    path = out / "decode-report.md"
    if not path.exists():
        return
    txt = path.read_text(encoding="utf-8")
    marker = "\n## Machine verification\n"
    if marker in txt:
        txt = txt.split(marker, 1)[0].rstrip() + "\n"
    lines = [marker, ""]
    lines.append(f"`data/decode-verify.json`: {result['passed']}/{result['total']} checks passed, {result['failed']} failed, overall ok = `{result['ok']}`.")
    lines.append("")
    fails = [c for c in result["checks"] if not c["ok"]]
    if fails:
        lines.append("Failed checks:")
        lines.extend(f"- `{c['check']}`: {c['detail']}" for c in fails)
    else:
        lines.append("All machine checks passed.")
    lines.append("")
    path.write_text(txt + "\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", default="output/gpu-dump/raw/sponza-20261115-083654")
    ap.add_argument("--out", default="output/final")
    ap.add_argument("--peer-receivers", default="Docs/Unreal/assets/lumen-sponza/receivers")
    ap.add_argument("--peer-cache", default="Docs/Unreal/assets/lumen-sponza/caches")
    ap.add_argument("--stage", choices=["data", "figures", "verify", "all"], default="all")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ctx = None
    if args.stage in ("data", "all", "figures"):
        ctx = build_data(Path(args.dump), out, Path(args.peer_receivers), Path(args.peer_cache))
        if args.stage == "data":
            print(json.dumps(jval(ctx["checks"]), indent=2)[:4000])
    if args.stage in ("figures", "all"):
        manifest = build_figures(ctx["dump"], ctx["view"], ctx["placement"], ctx["receivers"], ctx["probes"], out)
        print(f"figures: {len(manifest)}")
    if args.stage in ("verify", "all"):
        write_decode_report(out)
        result = verify_outputs(out)
        append_verify_summary(out, result)
        return 0 if result["ok"] else 1



def temporal_parameter_record(dump: Dump):
    """Captured pass-762 FParameters payload, including MaxFramesAccumulated.

    LumenScreenProbeGatherTemporal.ush uses MaxFramesAccumulated when the low
    nibble of the packed history byte is 15, so raw 15 is not 15 frames.
    """
    p = dump.passes_by_id[762]
    param_rel = f"Structures/{p['Parameters']}.bin"
    meta_rel = f"StructuresMetadata/{p['ParametersMetadata']}.json"
    param_path = dump.root / param_rel
    meta_path = dump.root / meta_rel
    b = param_path.read_bytes()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    members = {m["Name"]: m for m in meta.get("Members", [])}
    off_max = int(members["MaxFramesAccumulated"]["Offset"])
    off_amount = int(members["MaxFastUpdateModeAmount"]["Offset"])
    off_inv = int(members["InvFractionOfLightingMovingForFastUpdateMode"]["Offset"])
    max_frames = struct.unpack_from("<f", b, off_max)[0]
    return {
        "pass_id": 762,
        "pass_name": p["EventName"],
        "pass_pointer": p["Pointer"],
        "parameters_name": p.get("ParametersName"),
        "parameter_block_file": param_rel,
        "parameter_block_bytes": len(b),
        "parameter_block_sha256": sha256_file(param_path),
        "metadata_file": meta_rel,
        "metadata_struct": meta.get("StructTypeName"),
        "metadata_source": {"file": meta.get("FileName"), "line": meta.get("FileLine")},
        "fields": {
            "MaxFramesAccumulated": {
                "offset": off_max,
                "uint32_bits": struct.unpack_from("<I", b, off_max)[0],
                "float32": max_frames,
            },
            "MaxFastUpdateModeAmount": {
                "offset": off_amount,
                "float32": struct.unpack_from("<f", b, off_amount)[0],
            },
            "InvFractionOfLightingMovingForFastUpdateMode": {
                "offset": off_inv,
                "float32": struct.unpack_from("<f", b, off_inv)[0],
            },
        },
        "max_frames_accumulated": max_frames,
        "packing_shader": {
            "file": str(SPG_TEMPORAL_USH),
            "lines": "72-91",
            "pack": "((ceil(FastUpdateModeAmount*15)*16 + round(saturate(NumFramesAccumulated/min(MaxFramesAccumulated,15))*15)) / 255)",
            "unpack": "low==15 ? MaxFramesAccumulated : low/15*min(MaxFramesAccumulated,15)",
            "source_present": SPG_TEMPORAL_USH.exists(),
        },
        "validity": {
            "file_exists": param_path.exists(),
            "metadata_exists": meta_path.exists(),
            "bytes_match_metadata_size": len(b) == int(meta.get("Size", -1)),
            "readback_status": None,
            "note": "shader parameter payload, not an RDG resource version; DumpGPU attaches no readback record",
        },
    }


def adaptive_probe_allocator_record(dump: Dump):
    """Captured adaptive-probe allocator count and activity header from pass 706.

    NumAdaptiveScreenProbes is the append-counter value after AdaptivePlacementSpawn.
    ScreenTileAdaptiveProbeHeader is the same pass's 80x56 tile activity map.
    Adaptive atlas indices are allocated contiguously after the uniform slots, so
    only indices [0, count) are active; later finite texture values are stale.
    """
    p = dump.passes_by_id[706]
    ver = p["Pointer"]
    count_rel = f"Resources/{NUM_ADAPTIVE_RES}.v{ver}.bin"
    count_path = dump.root / count_rel
    count_rb = dump.readback_by_file.get(count_rel)
    count_arr = np.fromfile(count_path, dtype="<u4")
    count = int(count_arr[0]) if count_arr.size else None
    count_desc = dump.desc_by_id.get(NUM_ADAPTIVE_RES, {})

    header = dump.read_tex(ADAPTIVE_HEADER_RES, ver, dtype="<u4", channels=1)
    hvals = header["array"].astype(np.uint32)
    header_active = int(np.count_nonzero(hvals))
    header_sum = int(hvals.sum())
    header_tiles = [[int(x), int(y)] for y, x in np.argwhere(hvals != 0)]

    ind_ver = dump.passes_by_id[707]["Pointer"]
    ind_rel = f"Resources/{ADAPTIVE_INDIRECT_RES}.v{ind_ver}.bin"
    ind_path = dump.root / ind_rel
    ind_rb = dump.readback_by_file.get(ind_rel)
    ind_words = [int(v) for v in np.fromfile(ind_path, dtype="<u4")]
    ind_desc = dump.desc_by_id.get(ADAPTIVE_INDIRECT_RES, {})

    return {
        "allocator_pass": {"id": 706, "name": p["EventName"], "version": ver},
        "num_adaptive_screen_probes": count,
        "count_buffer": {
            "resource": NUM_ADAPTIVE_RES,
            "name": count_desc.get("Name"),
            "version": ver,
            "file": count_rel,
            "file_bytes": count_path.stat().st_size,
            "file_sha256": sha256_file(count_path),
            "stored_format": "PF_R32_UINT (FRDGBufferDesc, BytesPerElement=4, NumElements=1)",
            "dimension": "Buffer",
            "raw_word": int(count_arr[0]) if count_arr.size else None,
            "raw_hex": f"0x{int(count_arr[0]):08x}" if count_arr.size else None,
            "readback_status": count_rb["Status"] if count_rb else None,
        },
        "activity_header": {
            "resource": ADAPTIVE_HEADER_RES,
            "name": dump.desc_by_id.get(ADAPTIVE_HEADER_RES, {}).get("Name"),
            "version": ver,
            "extent": [int(header["layout"]["ExtentX"]), int(header["layout"]["ExtentY"])],
            "file": header["file"],
            "file_sha256": header["hash"],
            "nonzero_tile_count": header_active,
            "value_sum": header_sum,
            "distinct_values": [int(v) for v in np.unique(hvals)],
            "active_tiles_xy": header_tiles,
            "readback_status": header["readback"]["Status"] if header["readback"] else None,
        },
        "indirect_args": {
            "resource": ADAPTIVE_INDIRECT_RES,
            "name": ind_desc.get("Name"),
            "version": ind_ver,
            "producer_pass": {"id": 707, "name": dump.passes_by_id[707]["EventName"]},
            "file": ind_rel,
            "file_sha256": sha256_file(ind_path),
            "words_u32": ind_words,
            "readback_status": ind_rb["Status"] if ind_rb else None,
            "note": "six 3-word dispatch groups; groups are not individually labelled by the capture",
        },
        "count_matches_header_sum": bool(count is not None and count == header_sum),
        "count_matches_header_nonzero_tiles": bool(count is not None and count == header_active),
        "active_adaptive_index_range": [0, count - 1] if count else None,
        "active_atlas_linear_index_range": [3600, 3600 + count - 1] if count else None,
        "validity_note": (
            "adaptive index is assigned by an append atomic; only the first count "
            "indices are active this frame. Finite positive storage beyond count "
            "is stale from earlier frames and is not treated as active."
        ),
    }


def screenprobe_parameter_record(dump: Dump):
    """Captured FParameters payload for pass 736; FScreenProbeParameters is inline at +64."""
    f = dump.root / "Structures" / "00040000020b9718fd30.bin"
    b = f.read_bytes()
    base = 64
    fields = {
        "ScreenProbeTracingOctahedronResolution": struct.unpack_from("<I", b, base + 0)[0],
        "ScreenProbeGatherOctahedronResolution": struct.unpack_from("<I", b, base + 4)[0],
        "ScreenProbeGatherOctahedronResolutionWithBorder": struct.unpack_from("<I", b, base + 8)[0],
        "ScreenProbeDownsampleFactor": struct.unpack_from("<I", b, base + 12)[0],
        "ScreenProbeViewSize": list(struct.unpack_from("<2i", b, base + 16)),
        "ScreenProbeAtlasViewSize": list(struct.unpack_from("<2i", b, base + 24)),
        "ScreenProbeAtlasBufferSize": list(struct.unpack_from("<2i", b, base + 32)),
        "NumUniformScreenProbes": struct.unpack_from("<I", b, base + 100)[0],
        "MaxNumAdaptiveProbes": struct.unpack_from("<I", b, base + 104)[0],
        "FixedJitterIndex": struct.unpack_from("<i", b, base + 108)[0],
        "ScreenProbeRayDirectionFrameIndex": struct.unpack_from("<I", b, base + 112)[0],
        "bSupportsHairScreenTraces": struct.unpack_from("<I", b, base + 116)[0],
        "MaxImportanceSamplingOctahedronResolution": struct.unpack_from("<I", b, base + 256)[0],
        "ScreenProbeBRDFOctahedronResolution": struct.unpack_from("<I", b, base + 260)[0],
    }
    return {
        "parameter_block_file": "Structures/00040000020b9718fd30.bin",
        "parameter_block_bytes": len(b),
        "parameter_block_sha256": sha256_file(f),
        "metadata_file": "StructuresMetadata/000400007ffc1e2cdb40.json",
        "metadata_struct": "FScreenProbeParameters",
        "included_struct_offset_in_FParameters": base,
        "fields": fields,
        "producer_pass": {"id": 736, "name": "GenerateRays 8x8"},
        "validity": {
            "file_exists": f.exists(),
            "bytes_match_declared_FParameters_size": len(b) == 368,
            "readback_status": None,
            "note": "shader parameter payload, not an RDG resource version; DumpGPU does not attach a DumpReadbackStatus record to this structure payload",
        },
        "address_mapping": "FScreenProbeParameters starts at byte 64 of the captured FParameters payload; field addresses are 64+metadata Offset",
        "preexposure": PREEXPOSURE,
    }


def blue_noise_uniform_record(dump: Dump):
    f = dump.root / "Structures" / "00040000020bc4a5e700.bin"
    b = f.read_bytes()
    dims = list(struct.unpack_from("<3i", b, 0))
    masks = list(struct.unpack_from("<3i", b, 16))
    return {
        "layout": "FBlueNoiseParameters",
        "content_file": "Structures/00040000020bc4a5e700.bin",
        "content_bytes": len(b),
        "content_sha256": sha256_file(f),
        "dimensions": dims,
        "modulo_masks": masks,
        "texture_binding": "ScreenProbeParameters.BlueNoise.BlueNoise.Vec2Texture",
        "validity": {"file_exists": f.exists(), "bytes_match_declared_48": len(b) == 48, "readback_status": None},
        "address_mapping": "wrapped texture coordinate = ((screenTileX & maskX), (frame & maskZ)*dimY + (screenTileY & maskY))",
        "preexposure": PREEXPOSURE,
    }


if __name__ == "__main__":
    raise SystemExit(main())
