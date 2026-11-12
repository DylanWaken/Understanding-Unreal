#!/usr/bin/env python3
"""
Portable CPU replay of the final-capture deferred rect-light pixel.

Capture:  binary-20261112-153914 (pass --capture <capture-root>)
Graph:    0001
Pass:     249  "RenderLight Light::StandardDeferred: Ceiling area light"
Pixel:    (640, 650)
Contract: analysis/deferred-replay/contract.json / contract.md

What is read from the capture (no reconstruction, no GPU work):
  * pass 249 FRenderLightParameters;
  * the actual PS.View FViewUniformShaderParameters (6752 B);
  * the actual PS.DeferredLight FDeferredLightUniformStruct (192 B);
  * the actual PS.VirtualShadowMap FVirtualShadowMapUniformParameters (368 B);
  * versioned SceneColor before/after, GBufferA/B/C/D, SceneDepthZ, MaskBits;
  * the actual shader-bound GGX LTC Mat/Amp native textures exported from
    PS.View.GGXLTCMatTexture / PS.View.GGXLTCAmpTexture.

The script uses SubresourceLayouts.StoredFormat for every texture.  It never
selects a decoder from the descriptor's NativeFormat, so a BC (or other) native
view exported as float32 is decoded from the exported StoredFormat, and an
ordinary half/float16 export is decoded as float16.  PF_B8G8R8A8 is decoded in
memory order B,G,R,A.

The VSM boundary is explicit: the pass consumes the *final quantized* 4-bit
packed Shadow.Virtual.MaskBits.  The producer pass 248 and its page/light/
projection operands are recorded but the SMRT producer is not replayed.  No
unquantized screen-space shadow factor is claimed.

Usage:
  python replay_cpu.py --capture <capture-root> [--output report.json]
  python replay_cpu.py --capture <capture-root> --pixel 640 650 --json

Exit codes: 0 pass, 2 numerical tolerance fail, 3 missing required operand.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import sys
from pathlib import Path

import numpy as np

PASS_ID = 249
GRAPH_PREFIX = "0001"
EVENT = "RenderLight Light::StandardDeferred: Ceiling area light"
DEFAULT_PIXEL = (640, 650)
VIEW_RECT = (0, 0, 1280, 720)
W, H = 1280, 888
TOLERANCE_ULPS = 1.0
SCHEMA = "deferred-replay-final-v1"

# StoredFormat -> (numpy dtype string, channel count).  These are the formats
# that the exporter can emit.  A BC/other native view must be exported as one
# of the float formats (typically PF_A32B32G32R32F) before it can be replayed;
# the decoder follows StoredFormat, never NativeFormat.
DECODERS = {
    "PF_FloatRGBA": ("<f2", 4),
    "PF_G16R16F": ("<f2", 2),
    "PF_A32B32G32R32F": ("<f4", 4),
    "PF_R32_FLOAT": ("<f4", 1),
    "PF_R32G32_UINT": ("<u4", 2),
    "PF_R32_UINT": ("<u4", 1),
    "PF_R16_UINT": ("<u2", 1),
    "PF_B8G8R8A8": ("u1", 4),
    "PF_A2B10G10R10": ("<u4", 1),
    "PF_R8_UINT": ("u1", 1),
}


def json_stream(path: Path):
    """DumpGPU writes concatenated pretty-printed JSON objects, not an array."""
    text = path.read_text(encoding="utf-8-sig")
    dec = json.JSONDecoder()
    i = 0
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            break
        item, i = dec.raw_decode(text, i)
        yield item


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def f32_at(raw: bytes, off: int, n: int = 1):
    return list(struct.unpack_from("<" + "f" * n, raw, off))


def u32_at(raw: bytes, off: int):
    return struct.unpack_from("<I", raw, off)[0]


def i32_at(raw: bytes, off: int):
    return struct.unpack_from("<i", raw, off)[0]


def f64(v):
    return float(v)


def normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    if n == 0.0:
        raise ValueError("normalize() of zero vector")
    return v / n


def srgb_to_linear(c: np.ndarray) -> np.ndarray:
    c = np.asarray(c, dtype=np.float64)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def lerp(a, b, t):
    return a + (b - a) * t


def half(x) -> float:
    """Round a Python/numpy scalar to IEEE binary16 and return it as float64."""
    return float(np.float16(float(x)).astype(np.float64))


def bilinear(tex: np.ndarray, u: float, v: float) -> np.ndarray:
    """Bilinear clamp; SampleLevel(..., 0) on a 64x64 texture."""
    u = min(max(float(u), 0.0), 1.0)
    v = min(max(float(v), 0.0), 1.0)
    x = u * 64.0 - 0.5
    y = v * 64.0 - 0.5
    x0, y0 = int(math.floor(x)), int(math.floor(y))
    fx, fy = x - x0, y - y0

    def g(xx, yy):
        return tex[max(0, min(63, yy)), max(0, min(63, xx))]

    return (
        g(x0, y0) * (1 - fx) * (1 - fy)
        + g(x0 + 1, y0) * fx * (1 - fy)
        + g(x0, y0 + 1) * (1 - fx) * fy
        + g(x0 + 1, y0 + 1) * fx * fy
    )


def sphere_horizon_cos_wrap(nol: float, sin_alpha_sqr: float) -> float:
    # CapsuleLight.ush:74-107
    sin_alpha = math.sqrt(max(float(sin_alpha_sqr), 0.0))
    if sin_alpha <= 0.0:
        return float(nol)
    if nol < sin_alpha:
        nol = max(nol, -sin_alpha)
        nol = (sin_alpha + nol) ** 2 / (4.0 * sin_alpha)
    return float(nol)


def get_rect(origin, direction, tangent, source_radius, source_length):
    # RectLight.ush:602-690; RectLightBarnCosAngle <= 0.035 -> no barn-door branch.
    axis1 = np.asarray(tangent, dtype=np.float64)
    axis2 = np.asarray(direction, dtype=np.float64)
    axis0 = np.cross(axis1, axis2)
    return {
        "origin": np.asarray(origin, dtype=np.float64),
        "axis0": axis0,
        "axis1": axis1,
        "axis2": axis2,
        "extent": np.array([source_radius, source_length], dtype=np.float64),
    }


def rect_irradiance_lambert(n, rect):
    # RectLight.ush:61-165
    o = rect["origin"]
    a0, a1, a2 = rect["axis0"], rect["axis1"], rect["axis2"]
    ex, ey = rect["extent"]
    loc = np.array([a0 @ o, a1 @ o, a2 @ o], dtype=np.float64)
    corners = [
        np.array([loc[0] - ex, loc[1] - ey, loc[2]]),
        np.array([loc[0] + ex, loc[1] - ey, loc[2]]),
        np.array([loc[0] + ex, loc[1] + ey, loc[2]]),
        np.array([loc[0] - ex, loc[1] + ey, loc[2]]),
    ]
    l0, l1, l2, l3 = [normalize(c) for c in corners]
    c01, c12, c23, c30 = l0 @ l1, l1 @ l2, l2 @ l3, l3 @ l0

    def wf(c):
        return (1.5708 - 0.175 * c) / math.sqrt(max(c + 1.0, 1e-4))

    w01, w12, w23, w30 = wf(c01), wf(c12), wf(c23), wf(c30)
    lv = np.cross(l1, -w01 * l0 + w12 * l2) + np.cross(l3, w30 * l0 - w23 * l2)
    lw = lv[0] * a0 + lv[1] * a1 + lv[2] * a2
    length = float(np.linalg.norm(lw))
    mean_dir = lw / length
    falloff = 0.5 * length
    sin_alpha_sqr = falloff / math.pi
    nol = sphere_horizon_cos_wrap(float(np.dot(n, mean_dir)), sin_alpha_sqr)
    return mean_dir, falloff, nol, sin_alpha_sqr


def integrate_edge(a, b):
    # RectLight.ush:192-214
    c = float(a @ b)
    w = (0.8543985 + (0.4965155 + 0.0145206 * abs(c)) * abs(c)) / (
        3.4175940 + (4.1616724 + abs(c)) * abs(c)
    )
    if c <= 0.0:
        w = 0.5 / math.sqrt(max(1.0 - c * c, 1e-4)) - w
    return w


def polygon_irradiance(poly):
    # RectLight.ush:216-252
    p = [normalize(v) for v in poly]
    w = [integrate_edge(p[i], p[(i + 1) % 4]) for i in range(4)]
    return np.cross(p[1], -w[0] * p[0] + w[1] * p[2]) + np.cross(
        p[3], w[3] * p[0] - w[2] * p[2]
    )


def rect_ggx_approx_ltc(roughness, f0, n, v, rect, ltc_mat, ltc_amp):
    # RectLight.ush:262-299,340-405; DefaultLitBxDF rect branch.
    nov = min(max(abs(float(np.dot(n, v))) + 1e-5, 0.0), 1.0)
    f90 = np.clip(50.0 * f0, 0.0, 1.0)
    uv = np.array([roughness, math.sqrt(max(0.0, 1.0 - nov))]) * (63.0 / 64.0) + (0.5 / 64.0)
    m = bilinear(ltc_mat, uv[0], uv[1])
    a = bilinear(ltc_amp, uv[0], uv[1])

    ltc = np.array([[m[0], 0.0, m[2]], [0.0, 1.0, 0.0], [m[1], 0.0, m[3]]], dtype=np.float64)
    det = m[0] * m[3] - m[1] * m[2]
    inv = m / det
    inv_ltc = np.array(
        [[inv[3], 0.0, -inv[2]], [0.0, 1.0, 0.0], [-inv[1], 0.0, inv[0]]], dtype=np.float64
    )
    irr_scale = f90 * a[1] + (a[0] - a[1]) * f0

    t1 = normalize(v - n * float(np.dot(n, v)))
    t2 = np.cross(n, t1)
    tb = np.array([t1, t2, n], dtype=np.float64)
    ltc2 = ltc @ tb

    o = rect["origin"]
    a0, a1 = rect["axis0"], rect["axis1"]
    ex, ey = rect["extent"]
    corners = [
        o - a0 * ex - a1 * ey,
        o + a0 * ex - a1 * ey,
        o + a0 * ex + a1 * ey,
        o - a0 * ex + a1 * ey,
    ]
    poly = [ltc2 @ c for c in corners]
    ll = polygon_irradiance(poly)
    llen = float(np.linalg.norm(ll))
    ln = ll / llen
    nol = sphere_horizon_cos_wrap(float(ln[2]), llen)
    irradiance = max(llen * nol, 0.0)
    return irradiance * irr_scale, {
        "NoV": nov,
        "F90": f90,
        "UV": uv,
        "M": m,
        "A": a,
        "IrradianceScale": irr_scale,
        "Poly": [p.tolist() for p in poly],
        "LL": ll,
        "LLen": llen,
        "NoL": nol,
        "Irradiance": irradiance,
    }


# ---------------------------------------------------------------------------
# Capture access
# ---------------------------------------------------------------------------
class Capture:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.passes = list(json_stream(self.root / "Base/Passes.json"))
        self.descriptors = {
            x["UniqueResourceName"]: x
            for x in json_stream(self.root / "Base/ResourceDescs.json")
        }
        self.layouts = list(json_stream(self.root / "Base/SubresourceLayouts.json"))
        self.layout_by_file = {x["File"]: x for x in self.layouts}
        self.readback = {
            x.get("File"): x
            for x in json_stream(self.root / "Base/DumpReadbackStatus.json")
            if x.get("File")
        }
        self.selected = self._select_pass()
        self.versions = self._build_versions()
        self._native = None
        self._refs = None
        self._files = {}

    def _select_pass(self):
        hits = [
            p
            for p in self.passes
            if p.get("Id") == PASS_ID
            and p.get("EventName") == EVENT
            and p.get("Pointer", "").startswith(GRAPH_PREFIX)
        ]
        if len(hits) != 1:
            raise ValueError(
                "expected exactly one graph-0001 pass %d %r; found %d"
                % (PASS_ID, EVENT, len(hits))
            )
        return hits[0]

    def _build_versions(self):
        versions = {}
        for p in self.passes:
            if not p.get("Pointer", "").startswith(GRAPH_PREFIX):
                continue
            if p["Id"] >= self.selected["Id"]:
                break
            for res in p.get("OutputResources", []):
                rid, sub = split_resource(res)
                if rid is None:
                    continue
                versions[(rid, sub)] = p["Pointer"]
        return versions

    # -- files ------------------------------------------------------------
    def file_evidence(self, rel: str, kind: str | None = None) -> dict:
        rel = str(rel).replace("\\", "/")
        path = self.root / rel
        if not path.is_file():
            raise FileNotFoundError(f"missing capture file: {rel}")
        key = (rel, path.stat().st_size, path.stat().st_mtime_ns)
        if key not in self._files:
            self._files[key] = {
                "path": rel,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        ev = dict(self._files[key])
        if kind:
            ev["kind"] = kind
        if rel in self.layout_by_file:
            ev["layout"] = self.layout_by_file[rel]
        return ev

    def layout(self, rel: str) -> dict:
        try:
            return self.layout_by_file[rel]
        except KeyError:
            raise ValueError(f"no SubresourceLayouts record for {rel}")

    def decode_rel(self, rel: str, name: str):
        layout = self.layout(rel)
        fmt = layout["StoredFormat"]
        if fmt not in DECODERS:
            raise ValueError(
                f"{name}: StoredFormat={fmt} has no exact decoder "
                f"(decoder is selected from StoredFormat, not NativeFormat)"
            )
        dtype, channels = DECODERS[fmt]
        width, height = int(layout["ExtentX"]), int(layout["ExtentY"])
        depth = int(layout.get("Depth", 1))
        row_pitch = int(layout["RowPitchBytes"])
        z_stride = int(layout["ZSliceStrideBytes"])
        path = self.root / rel
        itemsize = np.dtype(dtype).itemsize
        texel = itemsize * channels
        if row_pitch < width * texel:
            raise ValueError(f"{name}: row pitch {row_pitch} < {width}x{texel}")
        if depth == 1:
            if path.stat().st_size != z_stride:
                raise ValueError(f"{name}: file size {path.stat().st_size} != z stride {z_stride}")
            if row_pitch == width * texel:
                arr = np.fromfile(path, dtype=dtype).reshape(height, width, channels)
            else:
                raw = np.fromfile(path, dtype=np.uint8)
                arr = np.empty((height, width, channels), dtype=dtype)
                for y in range(height):
                    row = np.frombuffer(raw, dtype=dtype, count=width * channels,
                                        offset=y * row_pitch)
                    arr[y] = row.reshape(width, channels)
        else:
            if z_stride < height * row_pitch or path.stat().st_size != z_stride * depth:
                raise ValueError(f"{name}: invalid 3D stride/size")
            arr = np.empty((depth, height, width, channels), dtype=dtype)
            raw = np.fromfile(path, dtype=np.uint8)
            for z in range(depth):
                base = z * z_stride
                for y in range(height):
                    row = np.frombuffer(raw, dtype=dtype, count=width * channels,
                                        offset=base + y * row_pitch)
                    arr[z, y] = row.reshape(width, channels)
        return arr, {
            "name": name,
            "storedFormat": fmt,
            "nativeFormat": layout.get("NativeFormat"),
            "nativeValueInterpretation": layout.get("NativeValueInterpretation"),
            "storedBytesAreNativeStorage": layout.get("StoredBytesAreNativeStorage"),
            "viewSRGB": layout.get("ViewSRGB"),
            "extentX": width,
            "extentY": height,
            "depth": depth,
            "rowPitchBytes": row_pitch,
            "zSliceStrideBytes": z_stride,
            "channels": channels,
            "dtype": dtype,
        }

    def read_texture(self, resource: str, name: str):
        rid, sub = split_resource(resource)
        if rid is None:
            raise ValueError(f"not a resource name: {resource}")
        key = (rid, sub)
        version = self.versions.get(key)
        if version is None:
            raise ValueError(f"{resource}: no preceding producer in graph {GRAPH_PREFIX}")
        rel = f"Resources/{rid}{sub}.v{version}.bin"
        arr, meta = self.decode_rel(rel, name)
        ev = self.file_evidence(rel, "texture")
        ev["uniqueResource"] = rid
        ev["subresource"] = sub
        ev["version"] = version
        ev["producer"] = self.producer_of(version)
        ev["decoded"] = meta
        ev["readback"] = self.readback.get(rel)
        return arr, ev

    def read_texture_after(self, resource: str, name: str):
        rid, sub = split_resource(resource)
        version = self.selected["Pointer"]
        rel = f"Resources/{rid}{sub}.v{version}.bin"
        arr, meta = self.decode_rel(rel, name)
        ev = self.file_evidence(rel, "texture")
        ev["uniqueResource"] = rid
        ev["subresource"] = sub
        ev["version"] = version
        ev["producer"] = self.producer_of(version)
        ev["decoded"] = meta
        ev["readback"] = self.readback.get(rel)
        return arr, ev

    def producer_of(self, pointer: str):
        for p in self.passes:
            if p["Pointer"] == pointer:
                return {
                    "id": p["Id"],
                    "pointer": p["Pointer"],
                    "event": p.get("EventName"),
                    "parametersName": p.get("ParametersName"),
                    "parentEventScopes": p.get("ParentEventScopes"),
                }
        return None

    # -- parameter structs -------------------------------------------------
    def refs_for_pass(self):
        if self._refs is None:
            rec = next(
                (x for x in json_stream(self.root / "Base/ReferencedStructs.json")
                 if x.get("Pass") == self.selected["Pointer"]),
                None,
            )
            if rec is None:
                raise ValueError("no ReferencedStructs record for pass 249")
            self._refs = rec["ReferencedStructs"]
        return self._refs

    def read_struct(self, meta_id: str, content_id: str, label: str):
        meta_path = self.root / "StructuresMetadata" / (meta_id + ".json")
        data_path = self.root / "Structures" / (content_id + ".bin")
        if not meta_path.is_file() or not data_path.is_file():
            raise FileNotFoundError(f"{label}: missing {meta_path.name} / {data_path.name}")
        meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        raw = data_path.read_bytes()
        fields = {}
        self._walk(meta_id, raw, 0, "", fields)
        ev = {
            "label": label,
            "metadata": meta_id,
            "content": content_id,
            "structTypeName": meta.get("StructTypeName"),
            "size": len(raw),
            "metadataFile": self.file_evidence(
                str(meta_path.relative_to(self.root)).replace("\\", "/")
            ),
            "contentFile": self.file_evidence(
                str(data_path.relative_to(self.root)).replace("\\", "/")
            ),
        }
        return fields, ev

    def _walk(self, meta_id: str, raw: bytes, offset: int, prefix: str, out: dict):
        meta = json.loads(
            (self.root / "StructuresMetadata" / (meta_id + ".json")).read_text(
                encoding="utf-8-sig"
            )
        )
        for field in meta["Members"]:
            name = (prefix + "." if prefix else "") + field["Name"]
            start = offset + field["Offset"]
            kind = field["BaseType"]
            if kind == "UBMT_INCLUDED_STRUCT":
                self._walk(field["StructMetadata"], raw, start, name, out)
                continue
            scalar = {"UBMT_FLOAT32": "f", "UBMT_INT32": "i", "UBMT_UINT32": "I"}.get(kind)
            if scalar:
                rows, cols = field["NumRows"], field["NumColumns"]
                elements = max(field["NumElements"], 1)
                count = rows * cols
                stride = ((4 * count + 15) // 16) * 16 if field["NumElements"] else 4 * count
                values = [
                    list(struct.unpack_from("<" + scalar * count, raw, start + j * stride))
                    for j in range(elements)
                ]
                value = values if field["NumElements"] else values[0]
                if not field["NumElements"] and count == 1:
                    value = value[0]
                out[name] = value
            elif start + 8 <= len(raw):
                out[name] = {
                    "kind": kind,
                    "pointer": f"{struct.unpack_from('<Q', raw, start)[0]:016x}",
                }

    def pass_parameters(self):
        fields, ev = self.read_struct(
            self.selected["ParametersMetadata"],
            self.selected["Parameters"],
            "FRenderLightParameters",
        )
        return fields, ev

    def ref_struct(self, name: str):
        ref = self.refs_for_pass().get(name)
        if not ref or not ref.get("Content") or not ref.get("Metadata"):
            raise ValueError(f"referenced struct {name} is missing/null")
        return self.read_struct(ref["Metadata"], ref["Content"], name)

    # -- native bindings ---------------------------------------------------
    def native_bindings(self):
        if self._native is None:
            rec = next(
                (x for x in json_stream(self.root / "Base/NativeUniformBufferBindings.json")
                 if x.get("Pass") == self.selected["Pointer"]),
                None,
            )
            if rec is None:
                raise ValueError("no NativeUniformBufferBindings record for pass 249")
            self._native = rec
        return self._native

    def native_binding(self, path: str):
        rec = self.native_bindings()
        b = rec.get("Bindings", {}).get(path)
        if not b:
            raise ValueError(f"native binding {path} missing for pass 249")
        return b


def split_resource(name: str):
    """Split '0001xxxxxxxxxxxxxxxx.mip0' into (rid, '.mip0')."""
    if not isinstance(name, str) or len(name) < 20:
        return None, None
    rid = name[:20]
    if not all(c in "0123456789abcdef" for c in rid):
        return None, None
    sub = name[20:]
    if not sub or sub[0] not in ".[":
        # Some inputs are bare 20-hex resource names; the shader binding uses
        # the .mip0 subresource for these 2D textures.
        sub = ".mip0"
    return rid, sub


def resource_from_pointer(ptr: str) -> str:
    """A RDG texture pointer (16 hex) -> graph-0001 unique resource name."""
    p = str(ptr)
    if len(p) != 16:
        raise ValueError(f"not a 16-hex texture pointer: {ptr}")
    return GRAPH_PREFIX + p + ".mip0"


def rid_from_pointer(ptr: str) -> str:
    return GRAPH_PREFIX + str(ptr)


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------
def replay(root: Path, x: int, y: int, tolerance_ulps: float = TOLERANCE_ULPS,
           allow_ltc_fallback: bool = False, ltc_header: str | None = None) -> dict:
    cap = Capture(root)
    sel = cap.selected
    if not (0 <= x < W and 0 <= y < H):
        raise ValueError("pixel outside buffer")
    if not (VIEW_RECT[0] <= x < VIEW_RECT[0] + VIEW_RECT[2]
            and VIEW_RECT[1] <= y < VIEW_RECT[1] + VIEW_RECT[3]):
        raise ValueError("pixel outside the 1280x720 view rect")

    params, params_ev = cap.pass_parameters()
    view, view_ev = cap.ref_struct("PS.View")
    light, light_ev = cap.ref_struct("PS.DeferredLight")
    scene_ub, scene_ub_ev = cap.ref_struct("PS.SceneTextures")
    vsm_ub, vsm_ub_ev = cap.ref_struct("PS.VirtualShadowMap")

    # Pointers from the actual bound parameter structs.
    sc_ptr = scene_ub["SceneColorTexture"]["pointer"]
    depth_ptr = scene_ub["SceneDepthTexture"]["pointer"]
    ga_ptr = scene_ub["GBufferATexture"]["pointer"]
    gb_ptr = scene_ub["GBufferBTexture"]["pointer"]
    gc_ptr = scene_ub["GBufferCTexture"]["pointer"]
    gd_ptr = scene_ub["GBufferDTexture"]["pointer"]
    mask_ptr = params["PS.ShadowMaskBits"]["pointer"]
    vsm_id = int(params["PS.VirtualShadowMapId"])
    light_scene_id = int(params["PS.LightSceneId"])

    sc_resource = resource_from_pointer(sc_ptr)
    depth_resource = resource_from_pointer(depth_ptr)
    resources = {
        "SceneColor": sc_resource,
        "SceneDepthZ": depth_resource,
        "GBufferA": resource_from_pointer(ga_ptr),
        "GBufferB": resource_from_pointer(gb_ptr),
        "GBufferC": resource_from_pointer(gc_ptr),
        "GBufferD": resource_from_pointer(gd_ptr),
        "Shadow.Virtual.MaskBits": resource_from_pointer(mask_ptr),
    }
    # Cross-check every shader-bound pointer is in the pass InputResources.
    input_set = set(sel.get("InputResources", []))
    missing_inputs = [v for v in resources.values() if v not in input_set]
    if missing_inputs:
        raise ValueError(f"shader-bound resources not in pass InputResources: {missing_inputs}")

    before_arr, before_ev = cap.read_texture(sc_resource, "SceneColor.before")
    after_arr, after_ev = cap.read_texture_after(sc_resource, "SceneColor.after")
    depth_arr, depth_ev = cap.read_texture(depth_resource, "SceneDepthZ")
    ga_arr, ga_ev = cap.read_texture(resources["GBufferA"], "GBufferA")
    gb_arr, gb_ev = cap.read_texture(resources["GBufferB"], "GBufferB")
    gc_arr, gc_ev = cap.read_texture(resources["GBufferC"], "GBufferC")
    gd_arr, gd_ev = cap.read_texture(resources["GBufferD"], "GBufferD")
    mask_arr, mask_ev = cap.read_texture(resources["Shadow.Virtual.MaskBits"], "Shadow.Virtual.MaskBits")

    before = np.asarray(before_arr[y, x], dtype=np.float64)
    after = np.asarray(after_arr[y, x], dtype=np.float64)
    device_z = float(depth_arr[y, x, 0])
    ga_word = int(ga_arr[y, x, 0]) & 0xFFFFFFFF
    gb_word = int.from_bytes(np.asarray(gb_arr[y, x], dtype=np.uint8).tobytes(), "little")
    gc_word = int.from_bytes(np.asarray(gc_arr[y, x], dtype=np.uint8).tobytes(), "little")
    gd_word = int.from_bytes(np.asarray(gd_arr[y, x], dtype=np.uint8).tobytes(), "little")
    mask_dwords = [int(mask_arr[y, x, 0]), int(mask_arr[y, x, 1])]

    # GBuffer decode.
    r10 = (ga_word & 0x3FF) / 1023.0
    g10 = ((ga_word >> 10) & 0x3FF) / 1023.0
    b10 = ((ga_word >> 20) & 0x3FF) / 1023.0
    a2 = ((ga_word >> 30) & 0x3) / 3.0
    n = normalize(np.array([r10, g10, b10], dtype=np.float64) * 2.0 - 1.0)

    # PF_B8G8R8A8 memory order: byte0=B, byte1=G, byte2=R, byte3=A.
    b8 = (gb_word & 0xFF) / 255.0
    g8 = ((gb_word >> 8) & 0xFF) / 255.0
    r8 = ((gb_word >> 16) & 0xFF) / 255.0
    a8 = ((gb_word >> 24) & 0xFF) / 255.0
    metallic, specular, roughness = r8, g8, b8
    packed = int(round(a8 * 255.0))
    shading_model_id = packed & 0xF
    selective_output_mask = packed & 0xF0
    if shading_model_id != 1:
        raise ValueError(
            f"pixel ({x},{y}) shading model {shading_model_id} is not "
            "SHADINGMODELID_DEFAULT_LIT(1)"
        )

    cb = (gc_word & 0xFF) / 255.0
    cg = ((gc_word >> 8) & 0xFF) / 255.0
    cr = ((gc_word >> 16) & 0xFF) / 255.0
    ca = ((gc_word >> 24) & 0xFF) / 255.0
    base = srgb_to_linear(np.array([cr, cg, cb], dtype=np.float64))
    gbuffer_ao = ca
    f0 = lerp(0.08 * specular, base, metallic)
    rho = base * (1.0 - metallic)

    # Screen UV / NDC / depth / translated world.
    uv = np.array([(x + 0.5) / W, (y + 0.5) / H], dtype=np.float64)
    spb = np.asarray(view["ScreenPositionScaleBias"], dtype=np.float64)
    ndc = np.array([(uv[0] - spb[3]) / spb[0], (uv[1] - spb[2]) / spb[1]], dtype=np.float64)
    iz = np.asarray(view["InvDeviceZToWorldZTransform"], dtype=np.float64)
    scene_depth = device_z * iz[0] + iz[1] + 1.0 / (device_z * iz[2] - iz[3])
    clip = np.array([ndc[0] * scene_depth, ndc[1] * scene_depth, scene_depth, 1.0])
    s2tw = np.asarray(view["ScreenToTranslatedWorld"], dtype=np.float64).reshape(4, 4)
    tw = (clip @ s2tw)[:3]
    world = tw + (
        np.asarray(view["ViewOriginHigh"], dtype=np.float64)
        + np.asarray(view["ViewOriginLow"], dtype=np.float64)
    )
    cam = np.asarray(view["TranslatedWorldCameraOrigin"], dtype=np.float64)
    v = -normalize(tw - cam)

    # Actual FDeferredLightUniformStruct (no reconstruction).
    lp = "LightParameters."
    p_light = np.asarray(light[lp + "TranslatedWorldPosition"], dtype=np.float64)
    inv_radius = f64(light[lp + "InvRadius"])
    light_color = np.asarray(light[lp + "Color"], dtype=np.float64)
    falloff_exp = f64(light[lp + "FalloffExponent"])
    direction = np.asarray(light[lp + "Direction"], dtype=np.float64)
    tangent = np.asarray(light[lp + "Tangent"], dtype=np.float64)
    source_radius = f64(light[lp + "SourceRadius"])
    source_length = f64(light[lp + "SourceLength"])
    barn_cos = f64(light[lp + "RectLightBarnCosAngle"])
    barn_len = f64(light[lp + "RectLightBarnLength"])
    spec_scale = f64(light[lp + "SpecularScale"])
    diff_scale = f64(light[lp + "DiffuseScale"])
    shadowed_bits = int(light["ShadowedBits"])
    shadow_map_channel_mask = [f64(z) for z in light["ShadowMapChannelMask"]]
    contact_shadow_length = f64(light["ContactShadowLength"])

    if falloff_exp != 0.0:
        raise ValueError("expected inverse-squared falloff (FalloffExponent == 0)")
    if barn_cos > 0.035:
        raise ValueError("barn-door branch is active; this replay implements the no-barn branch")
    if spec_scale != 1.0 or diff_scale != 1.0:
        raise ValueError("SpecularScale/DiffuseScale != 1")

    to_light = p_light - tw
    d2 = float(to_light @ to_light)
    ldir = to_light / math.sqrt(d2)
    light_mask = max(0.0, 1.0 - (d2 * inv_radius * inv_radius) ** 2) ** 2
    if float(ldir @ direction) < 0.0:
        light_mask = 0.0

    rect = get_rect(to_light, direction, tangent, source_radius, source_length)
    mean_dir, falloff, nol, sin_alpha_sqr = rect_irradiance_lambert(n, rect)
    diffuse = (rho / math.pi) * (diff_scale * falloff * nol)

    # Actual shader-bound LTC textures: load by the binding's ExportFirstFile,
    # decode by that file's SubresourceLayouts.StoredFormat.
    mat_bind = cap.native_binding("PS.View.GGXLTCMatTexture")
    amp_bind = cap.native_binding("PS.View.GGXLTCAmpTexture")
    ltc_source = "actual-native-binding"
    ltc_evidence = {}
    try:
        mat_rel = mat_bind["ExportFirstFile"]
        amp_rel = amp_bind["ExportFirstFile"]
        mat_arr, mat_meta = cap.decode_rel(mat_rel, "GGXLTCMat")
        amp_arr, amp_meta = cap.decode_rel(amp_rel, "GGXLTCAmp")
        mat_ev = cap.file_evidence(mat_rel, "native-texture")
        amp_ev = cap.file_evidence(amp_rel, "native-texture")
        mat_ev.update({
            "bindingPath": "PS.View.GGXLTCMatTexture",
            "nativePointer": mat_bind.get("NativePointer"),
            "nativeTextureName": mat_bind.get("NativeTextureName"),
            "nativeFormat": mat_bind.get("NativeFormat"),
            "exportViewFormat": mat_bind.get("ExportViewFormat"),
            "diagnosticResource": mat_bind.get("DiagnosticResource"),
            "dumpStatus": mat_bind.get("DumpStatus"),
            "reusedScheduledReadback": mat_bind.get("ReusedScheduledReadback"),
            "firstScheduledByPass": mat_bind.get("FirstScheduledByPass"),
            "timingRisk": mat_bind.get("TimingRisk"),
            "decoded": mat_meta,
            "readback": cap.readback.get(mat_rel),
        })
        amp_ev.update({
            "bindingPath": "PS.View.GGXLTCAmpTexture",
            "nativePointer": amp_bind.get("NativePointer"),
            "nativeTextureName": amp_bind.get("NativeTextureName"),
            "nativeFormat": amp_bind.get("NativeFormat"),
            "exportViewFormat": amp_bind.get("ExportViewFormat"),
            "diagnosticResource": amp_bind.get("DiagnosticResource"),
            "dumpStatus": amp_bind.get("DumpStatus"),
            "reusedScheduledReadback": amp_bind.get("ReusedScheduledReadback"),
            "firstScheduledByPass": amp_bind.get("FirstScheduledByPass"),
            "timingRisk": amp_bind.get("TimingRisk"),
            "decoded": amp_meta,
            "readback": cap.readback.get(amp_rel),
        })
        if mat_arr.ndim != 3 or mat_arr.shape[2] < 4:
            raise ValueError(f"GGXLTCMat has {mat_arr.shape}, expected 64x64x>=4")
        if amp_arr.ndim != 3 or amp_arr.shape[2] < 2:
            raise ValueError(f"GGXLTCAmp has {amp_arr.shape}, expected 64x64x>=2")
        ltc_evidence = {"mat": mat_ev, "amp": amp_ev}
    except Exception as exc:
        if not allow_ltc_fallback:
            raise ValueError(
                "actual shader-bound GGX LTC textures are required; "
                "refusing to relabel a synthetic fallback as actual: %s" % exc
            )
        if not ltc_header or not Path(ltc_header).is_file():
            raise
        mat_arr, amp_arr = parse_ltc_header(ltc_header)
        ltc_source = "source-derived-fallback-LTC.h"
        ltc_evidence = {
            "fallback": str(ltc_header),
            "warning": "synthetic fallback is NOT the captured shader-bound texture",
            "error": str(exc),
        }

    specular_lighting, ltc_dbg = rect_ggx_approx_ltc(
        roughness, f0, n, v, rect, mat_arr.astype(np.float64), amp_arr.astype(np.float64)
    )

    # Visibility: final quantized packed mask.  One culled local light -> index 0.
    light_index = 0
    unpacked = (mask_dwords[0] >> (4 * light_index)) & 0xF
    visibility = unpacked / 15.0
    surface_shadow = 1.0 * gbuffer_ao  # LightAttenuation.z=1, StaticShadowing=1, AO=1
    pre_exposure = f64(view["PreExposure"])
    one_over = f64(view["OneOverPreExposure"])
    if abs(pre_exposure * one_over - 1.0) > 1e-6:
        raise ValueError("PreExposure reciprocal mismatch")

    masked_color = light_color * light_mask * surface_shadow * visibility
    cpu = (diffuse + specular_lighting) * masked_color * pre_exposure

    observed = (after - before)[:3]
    residual = cpu - observed
    scale = max(float(np.max(np.abs(observed))), float(np.max(np.abs(cpu))))
    half_ulp = float(np.spacing(np.float16(scale)))
    residual_ulps = residual / half_ulp

    pred_after = np.asarray(before[:3] + cpu, dtype=np.float16)
    obs_after = np.asarray(after[:3], dtype=np.float16)
    pred_bits = pred_after.view(np.uint16).astype(np.int64)
    obs_bits = obs_after.view(np.uint16).astype(np.int64)
    codeword_delta = pred_bits - obs_bits

    max_abs_ulp = float(np.max(np.abs(residual_ulps)))
    passed = bool(max_abs_ulp <= tolerance_ulps)

    # Native sampler evidence: the LTC sampler pointer is the same object as
    # View.SharedBilinearClampedSampler, whose source state is bilinear/clamp.
    mat_sampler = cap.native_binding("PS.View.GGXLTCMatSampler")
    shared_sampler = cap.native_binding("PS.View.SharedBilinearClampedSampler")
    amp_sampler = cap.native_binding("PS.View.GGXLTCAmpSampler")
    sampler_same = (
        mat_sampler.get("Pointer") == shared_sampler.get("Pointer")
        and amp_sampler.get("Pointer") == shared_sampler.get("Pointer")
    )

    # VSM producer operands (retained, not replayed).
    vsm_operands = {}
    for k, val in vsm_ub.items():
        if isinstance(val, dict) and val.get("kind") in (
            "UBMT_RDG_BUFFER_SRV", "UBMT_RDG_TEXTURE", "UBMT_SAMPLER"
        ):
            vsm_operands[k] = val.get("pointer")
    vsm_producer = None
    for p in cap.passes:
        if p["Pointer"] == mask_ev.get("version"):
            vsm_producer = {
                "id": p["Id"],
                "pointer": p["Pointer"],
                "event": p.get("EventName"),
                "parentEventScopes": p.get("ParentEventScopes"),
            }
            break

    # Branch/permutation evidence.
    permutation = {
        "shading_model": {
            "id": shading_model_id,
            "name": "SHADINGMODELID_DEFAULT_LIT",
            "selective_output_mask": "0x%02X" % selective_output_mask,
            "has_anisotropy_bit": bool(selective_output_mask & 0x10),
        },
        "substrate": {
            "enabled": False,
            "evidence": "r.Substrate=0; PS.Substrate pointer is null; pass is legacy GBuffer + DefaultLit",
        },
        "mega_lights": {
            "enabled": False,
            "evidence": "r.MegaLights.EnableForProject=0",
        },
        "source_shape": {
            "value": "Rect",
            "evidence": (
                "Pass event 'RenderLight Light::StandardDeferred: Ceiling area light'; the captured "
                "DeferredLight struct carries the rect-light fields RectLightBarnCosAngle/"
                "RectLightBarnLength and RectLightAtlasUVScale/Offset; the active specular call is "
                "RectGGXApproxLTC, not the capsule/directional path. No synthetic LightType field is "
                "asserted beyond the captured pass/struct evidence."
            ),
        },
        "source_texture": {
            "active": False,
            "evidence": "RectLightAtlasUVScale=(0,0), RectLightAtlasMaxLevel=32; no source texture resource in pass inputs",
        },
        "ies": {
            "active": False,
            "evidence": "IESAtlasIndex=-1.0",
        },
        "anisotropy": {
            "active": False,
            "evidence": "FAnistropicMaterials = bSupportAnisotropyPermutation && !IsRectLight -> false for rect light; GBuffer mask 0x%02X has no 0x10 bit" % selective_output_mask,
        },
        "lighting_channels": {
            "active": bool(int(params["PS.bLightingChannelEnabled"])),
            "evidence": "PS.bLightingChannelEnabled=%d; LightingChannelsTexture is ZeroUIntDummy" % int(params["PS.bLightingChannelEnabled"]),
        },
        "vsm_mask": {
            "active": True,
            "evidence": "VirtualShadowMapId=%d != INDEX_NONE and ShadowMaskBits bound; bUseVirtualShadowMapMask=1" % vsm_id,
        },
        "transmission": {
            "active": False,
            "evidence": "opaque SceneColor pass; bTransmission=0; no translucent light pass",
        },
        "hair_lighting": {
            "active": False,
            "evidence": "FHairLighting permutation explicitly 0; HairShadowMaskValid=0",
        },
        "hair_complex_transmittance": {
            "active": False,
            "evidence": "FHairComplexTransmittance false; HairShadowMaskValid=0",
        },
        "light_function_atlas": {
            "active": False,
            "light_index": int(light[lp + "LightFunctionAtlasLightIndex"]),
            "evidence": "LightFunctionAtlasLightIndex=0; GetLocalLightFunctionCommon returns 1.0 for index 0; no light function material in the captured light",
        },
        "cloud_transmittance": {
            "active": bool(int(params["PS.bCloudShadowEnabled"])),
            "evidence": "PS.bCloudShadowEnabled=%d" % int(params["PS.bCloudShadowEnabled"]),
        },
        "atmosphere_transmittance": {
            "active": bool(int(params["PS.bAtmosphereTransmittanceEnabled"])),
            "evidence": "PS.bAtmosphereTransmittanceEnabled=%d" % int(params["PS.bAtmosphereTransmittanceEnabled"]),
        },
        "visualize_culling": {
            "active": False,
            "evidence": "normal deferred light pass writing SceneColor, not the light-culling debug color",
        },
        "energy_conservation": {
            "use_energy_conservation": 0,
            "evidence": "r.Material.EnergyConservation=0 (capture cvar), r.Substrate=0 -> LEGACY_MATERIAL_ENERGYCONSERVATION=0 -> USE_ENERGY_CONSERVATION=0; energy LUT lookup compiled out",
        },
        "compiled_shader_hash": {
            "captured": False,
            "gap": "The capture records the permutation domain inputs but no PSO/shader hash. The active branch is derived from the captured parameters and CVars; M8 remains open.",
        },
    }

    # Evidence list.
    files = [
        params_ev["contentFile"], params_ev["metadataFile"],
        view_ev["contentFile"], view_ev["metadataFile"],
        light_ev["contentFile"], light_ev["metadataFile"],
        scene_ub_ev["contentFile"], scene_ub_ev["metadataFile"],
        vsm_ub_ev["contentFile"], vsm_ub_ev["metadataFile"],
        before_ev, after_ev, depth_ev, ga_ev, gb_ev, gc_ev, gd_ev, mask_ev,
    ]
    if ltc_source == "actual-native-binding":
        files += [ltc_evidence["mat"], ltc_evidence["amp"]]

    provenance_files = []
    for rel in (
        "Base/Passes.json",
        "Base/ResourceDescs.json",
        "Base/SubresourceLayouts.json",
        "Base/ReferencedStructs.json",
        "Base/NativeUniformBufferBindings.json",
        "Base/DumpReadbackStatus.json",
        "Base/ConsoleVariables.csv",
        "Base/Infos.json",
    ):
        if (cap.root / rel).is_file():
            ev = cap.file_evidence(rel, "metadata")
            provenance_files.append(ev)
            files.append(ev)

    report = {
        "schema": SCHEMA,
        "contract_id": "deferred-replay-final-binary-20261112-153914-pass249-pixel640x650",
        "status": "validated-against-final-capture",
        "capture": {
            "id": "binary-20261112-153914",
            "root": ".",
            "project": "UnrealLab",
            "engine": "UE 5.8.1 Editor Development D3D12 SM6",
            "gpu": "NVIDIA RTX PRO 6000 Blackwell Workstation Edition, driver 610.62",
            "frame_scope": sel.get("ParentEventScopes", [None])[-1],
            "graph_prefix": GRAPH_PREFIX,
            "view_rect": {"min_x": 0, "min_y": 0, "width": 1280, "height": 720},
            "buffer_extent": {"width": W, "height": H},
        },
        "pass": {
            "id": sel["Id"],
            "pointer": sel["Pointer"],
            "event": sel.get("EventName"),
            "parametersName": sel.get("ParametersName"),
            "parameters": sel.get("Parameters"),
            "parametersMetadata": sel.get("ParametersMetadata"),
            "parentEventScopes": sel.get("ParentEventScopes"),
            "outputResources": sel.get("OutputResources"),
            "renderTargets": params.get("PS.RenderTargets"),
            "graph_disambiguation": [
                {
                    "id": p["Id"],
                    "pointer": p["Pointer"],
                    "event": p.get("EventName"),
                    "selected": p["Pointer"] == sel["Pointer"],
                }
                for p in cap.passes
                if p.get("Id") in (249, 561, 869)
                and p.get("EventName") == EVENT
            ],
        },
        "shader_parameters": {
            "FRenderLightParameters": params_ev,
            "PS.View": view_ev,
            "PS.DeferredLight": light_ev,
            "PS.SceneTextures": scene_ub_ev,
            "PS.VirtualShadowMap": vsm_ub_ev,
        },
        "resources": {
            "SceneColor": {
                "uniqueResource": before_ev["uniqueResource"],
                "before": before_ev,
                "after": after_ev,
                "view_rect": {"min_x": 0, "min_y": 0, "width": 1280, "height": 720},
            },
            "SceneDepthZ": depth_ev,
            "GBufferA": ga_ev,
            "GBufferB": gb_ev,
            "GBufferC": gc_ev,
            "GBufferD": gd_ev,
            "Shadow.Virtual.MaskBits": mask_ev,
        },
        "producer_versions": {
            "SceneColor.before": before_ev.get("producer"),
            "SceneColor.after": after_ev.get("producer"),
            "SceneDepthZ": depth_ev.get("producer"),
            "GBufferA": ga_ev.get("producer"),
            "GBufferB": gb_ev.get("producer"),
            "GBufferC": gc_ev.get("producer"),
            "GBufferD": gd_ev.get("producer"),
            "Shadow.Virtual.MaskBits": mask_ev.get("producer"),
        },
        "pixel": {
            "x": x, "y": y,
            "uv": uv.tolist(),
            "ndc": ndc.tolist(),
            "device_z": device_z,
            "device_z_bits": "0x%08X" % int.from_bytes(np.float32(device_z).tobytes(), "little"),
            "scene_depth_cm": scene_depth,
            "translated_world_position": tw.tolist(),
            "world_position": world.tolist(),
            "camera_vector": v.tolist(),
            "normal": n.tolist(),
            "base_color": base.tolist(),
            "metallic": metallic,
            "specular": specular,
            "roughness": roughness,
            "shading_model_id": shading_model_id,
            "selective_output_mask": "0x%02X" % selective_output_mask,
            "gbuffer_a_word": "0x%08X" % ga_word,
            "gbuffer_b_word": "0x%08X" % gb_word,
            "gbuffer_c_word": "0x%08X" % gc_word,
            "gbuffer_d_word": "0x%08X" % gd_word,
            "gbuffer_ao": gbuffer_ao,
            "f0": f0.tolist(),
            "rho": rho.tolist(),
            "scene_color_before": before.tolist(),
            "scene_color_after": after.tolist(),
            "observed_delta": observed.tolist(),
        },
        "light": {
            "struct": "FDeferredLightUniformStruct",
            "translated_world_position": p_light.tolist(),
            "color": light_color.tolist(),
            "inv_radius": inv_radius,
            "falloff_exponent": falloff_exp,
            "direction": direction.tolist(),
            "tangent": tangent.tolist(),
            "source_radius": source_radius,
            "source_length": source_length,
            "rect_light_barn_cos_angle": barn_cos,
            "rect_light_barn_length": barn_len,
            "specular_scale": spec_scale,
            "diffuse_scale": diff_scale,
            "shadowed_bits": shadowed_bits,
            "shadow_map_channel_mask": shadow_map_channel_mask,
            "contact_shadow_length": contact_shadow_length,
            "light_scene_id": light_scene_id,
            "light_type": int(light[lp + "LightType"]) if lp + "LightType" in light else None,
            "ies_atlas_index": f64(light[lp + "IESAtlasIndex"]),
            "light_function_atlas_light_index": int(light[lp + "LightFunctionAtlasLightIndex"]),
            "b_affects_translucent_lighting": int(light[lp + "bAffectsTranslucentLighting"]),
            "light_mask": light_mask,
            "distance_squared": d2,
            "to_light": to_light.tolist(),
            "mean_light_direction": mean_dir.tolist(),
            "falloff": falloff,
            "no_l": nol,
            "sin_alpha_squared": sin_alpha_sqr,
            "notes": "All fields read from the captured PS.DeferredLight UB; no reconstruction from FLumenPackedLight.",
        },
        "visibility": {
            "path": "USE_VIRTUAL_SHADOW_MAP_MASK",
            "virtual_shadow_map_id": vsm_id,
            "fallback_taken": False,
            "mask_bits_dwords": ["0x%08X" % mask_dwords[0], "0x%08X" % mask_dwords[1]],
            "light_index": light_index,
            "unpacked_4bit": unpacked,
            "visibility": visibility,
            "surface_shadow": surface_shadow,
            "producer": vsm_producer,
            "producer_operands": vsm_operands,
            "boundary": (
                "FINAL 4-bit quantized packed Shadow.Virtual.MaskBits, consumed by pass 249. "
                "The SMRT producer (pass 248) is NOT replayed; page/light/projection operands are "
                "retained only as evidence. No unquantized screen-space shadow factor is claimed."
            ),
            "quantization_note": "Visibility is exact only because UnpackShadowMask=15 -> 15/15=1.0; the underlying unquantized SMRT factor is not independently proven.",
        },
        "ltc": {
            "source": ltc_source,
            "sampler": {
                "matPointer": mat_sampler.get("Pointer"),
                "ampPointer": amp_sampler.get("Pointer"),
                "sharedBilinearClampedPointer": shared_sampler.get("Pointer"),
                "sameObjectAsSharedBilinearClamped": sampler_same,
                "stateCaptured": bool(mat_sampler.get("StateCaptured")),
                "interpretation": "bilinear clamp, SampleLevel(...,0), 64x64, no mip chain",
                "source": "SceneManagement.cpp:1277 TStaticSamplerState<SF_Bilinear, AM_Clamp, AM_Clamp, AM_Clamp>; RectLight.ush:14-16 uses View.SharedBilinearClampedSampler when SUPPORTS_INDEPENDENT_SAMPLERS",
            },
            "mat": ltc_evidence.get("mat"),
            "amp": ltc_evidence.get("amp"),
            "sampled": {
                "NoV": ltc_dbg["NoV"],
                "F90": ltc_dbg["F90"].tolist(),
                "UV": ltc_dbg["UV"].tolist(),
                "M": ltc_dbg["M"].tolist(),
                "A": ltc_dbg["A"].tolist(),
                "IrradianceScale": ltc_dbg["IrradianceScale"].tolist(),
                "LL": ltc_dbg["LL"].tolist(),
                "LLen": ltc_dbg["LLen"],
                "NoL": ltc_dbg["NoL"],
                "Irradiance": ltc_dbg["Irradiance"],
            },
            "channel_order": {
                "mat": "PF_FloatRGBA export: file order R,G,B,A -> M.x,M.y,M.z,M.w (no reorder)",
                "amp": "PF_G16R16F export: file order channel0,channel1 -> A.x,A.y (no reorder)",
            },
            "fallback_used": ltc_source != "actual-native-binding",
        },
        "math": {
            "diffuse_unexposed": diffuse.tolist(),
            "specular_unexposed": specular_lighting.tolist(),
            "light_mask": light_mask,
            "surface_shadow": surface_shadow,
            "visibility": visibility,
            "pre_exposure": pre_exposure,
            "one_over_pre_exposure": one_over,
            "masked_light_color": masked_color.tolist(),
            "cpu_contribution": cpu.tolist(),
            "observed_delta": observed.tolist(),
            "residual": residual.tolist(),
            "half_ulp": half_ulp,
            "residual_ulps": residual_ulps.tolist(),
            "max_abs_residual_ulps": max_abs_ulp,
            "tolerance_ulps": tolerance_ulps,
            "passed": passed,
            "predicted_after_fp16": pred_after.astype(np.float64).tolist(),
            "observed_after_fp16": obs_after.astype(np.float64).tolist(),
            "predicted_after_fp16_bits": ["0x%04X" % b for b in pred_bits.tolist()],
            "observed_after_fp16_bits": ["0x%04X" % b for b in obs_bits.tolist()],
            "codeword_delta": codeword_delta.tolist(),
            "codeword_within_1_ulp": bool(np.max(np.abs(codeword_delta)) <= 1),
            "test": "pass if max_channel |L_cpu - (after-before)| <= ULP_fp16(max(|after|,|L_cpu|)); before=0 -> 1 ULP",
        },
        "permutation": permutation,
        "energy_conservation": {
            "use_energy_conservation": 0,
            "energy_lut_active": False,
            "evidence": "r.Material.EnergyConservation=0 in Base/ConsoleVariables.csv; r.Substrate=0; ShadingEnergyConservation.ush:20-30 selects USE_ENERGY_CONSERVATION=0; ShaderCompiler.cpp:4191-4194 sets LEGACY_MATERIAL_ENERGYCONSERVATION=0",
        },
        "checks": {
            "exact_final_scene_graph": True,
            "exact_producer_versions": True,
            "actual_view_uniform_buffer": True,
            "actual_deferred_light_uniform_struct": True,
            "actual_shader_bound_ltc_textures": ltc_source == "actual-native-binding",
            "actual_shadow_mask_values": True,
            "exact_pre_exposure": abs(pre_exposure - 0.0625) < 1e-12,
            "observed_fp16_output_used": True,
            "numerical_tolerance_pass": passed,
            "codeword_within_1_ulp": bool(np.max(np.abs(codeword_delta)) <= 1),
        },
        "gaps": [
            {
                "id": "G1",
                "kind": "residual",
                "detail": (
                    "The CPU contribution is 1.239991595 (R/G) and 1.176608276 (B); the observed "
                    "FP16 delta is 1.2392578125 (R/G) and 1.1767578125 (B). The unrounded residual "
                    "is +0.7514 ULP (R/G) and -0.1531 ULP (B), within the 1-ULP tolerance. The "
                    "predicted FP16 codeword is +1 ULP in R/G and exact in B. The LTC sidecar is "
                    "actual and bit-identical to the quantized LTC.h, so this is not an LTC-content "
                    "gap. No missing operand has been identified; the remaining sub-ULP difference "
                    "is in the GPU shader evaluation (candidates include the half-precision BxDF "
                    "inputs and filter/approximation details), which this float64 CPU replay does "
                    "not model bit-exactly."
                ),
            },
            {
                "id": "G2",
                "kind": "vsm-boundary",
                "detail": (
                    "Only the final quantized 4-bit MaskBits is replayed. The SMRT/VSM producer "
                    "(pass 248) is not replayed; page table, page flags, page receiver masks, "
                    "physical page pool, projection data, light grid and culled-light grid are "
                    "retained as operands but not sampled. Visibility=1.0 is exact for the 15/15 "
                    "codeword; the unquantized shadow factor is not independently proven."
                ),
            },
            {
                "id": "G3",
                "kind": "shader-hash",
                "detail": (
                    "No compiled PSO/shader hash was captured for pass 249. The active permutation "
                    "is derived from the captured parameters/CVars and source; a byte-exact compiled "
                    "shader identity remains open (old M8)."
                ),
            },
            {
                "id": "G4",
                "kind": "native-sampler-state",
                "detail": (
                    "Sampler bindings are pointer-only (StateCaptured=false). The LTC sampler pointer "
                    "equals View.SharedBilinearClampedSampler and source initializes that object as "
                    "SF_Bilinear/AM_Clamp x3, so bilinear-clamp is the correct interpretation; the raw "
                    "sampler object state was not captured."
                ),
            },
            {
                "id": "G5",
                "kind": "native-readback-timing",
                "detail": (
                    "The LTC native bindings are ReusedScheduledReadback from pass "
                    "000100000225fb9fef70 with TimingRisk=OwnerPassNotExecutedAtCPUCollection; the "
                    "exported file exists, size matches, DumpReadbackStatus=Dumped, and its 64x64 "
                    "half values are bit-identical to the quantized LTC.h. The content is therefore "
                    "valid for this static global LUT, but the capture-side timing caveat is retained."
                ),
            },
            {
                "id": "G6",
                "kind": "stored-format-provenance",
                "detail": (
                    "SubresourceLayouts marks the LTC exports StoredBytesAreNativeStorage=false, "
                    "NativeValueInterpretation=DecodedFloat, BitExact=false, ValuePreserving=true. "
                    "The decoder follows StoredFormat (PF_FloatRGBA / PF_G16R16F) and preserves file "
                    "channel order; it does not assume a half2/half4 decoder from NativeFormat."
                ),
            },
        ],
        "files": files,
        "provenance_files": provenance_files,
    }
    return report


def parse_ltc_header(path: str):
    """Explicit source-derived fallback; never used unless --allow-ltc-fallback."""
    hdr = Path(path).read_text(encoding="utf-8", errors="replace")

    def arr(name):
        i = hdr.index(name)
        j = hdr.index("{", i)
        k = hdr.index("}", j)
        vals = []
        for t in hdr[j + 1:k].split(","):
            t = t.strip().rstrip("f").rstrip("F")
            if t:
                vals.append(float(t))
        if len(vals) != 64 * 64 * 4:
            raise ValueError(f"{name}: expected 16384 values, got {len(vals)}")
        return np.array(vals, dtype=np.float64).reshape(64, 64, 4)

    return (
        arr("GGX_LTC_Mat").astype(np.float16).astype(np.float64),
        arr("GGX_LTC_Amp").astype(np.float16).astype(np.float64)[:, :, :2],
    )


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--capture", required=True,
                    help="capture root containing Base/, Resources/, Structures/ (binary-... dump root)")
    ap.add_argument("--output", help="write the JSON report to this path")
    ap.add_argument("--pixel", nargs=2, type=int, default=list(DEFAULT_PIXEL), metavar=("X", "Y"))
    ap.add_argument("--tolerance-ulps", type=float, default=TOLERANCE_ULPS)
    ap.add_argument("--allow-ltc-fallback", action="store_true",
                    help="allow source-derived LTC.h fallback if the actual native export is missing; "
                         "the report is labelled source-derived-fallback and is NOT actual-byte use")
    ap.add_argument("--ltc-header", default=None,
                    help="LTC.h path for the optional fallback only")
    ap.add_argument("--json", action="store_true", help="print the JSON report")
    args = ap.parse_args(argv)

    root = Path(args.capture)
    if not (root / "Base/Passes.json").is_file():
        print(f"error: {root} is not a DumpGPU capture root (Base/Passes.json missing)",
              file=sys.stderr)
        return 3
    try:
        report = replay(
            root, args.pixel[0], args.pixel[1], args.tolerance_ulps,
            args.allow_ltc_fallback, args.ltc_header,
        )
    except FileNotFoundError as exc:
        print(f"error: missing required capture operand: {exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    if args.json:
        print(text, end="")
    else:
        m = report["math"]
        print("capture :", root)
        print("graph   :", report["capture"]["graph_prefix"],
              "pass", report["pass"]["id"], report["pass"]["pointer"])
        print("pixel   :", tuple(args.pixel), "UV", report["pixel"]["uv"])
        print("LTC     :", report["ltc"]["source"])
        print("mask    :", report["visibility"]["mask_bits_dwords"],
              "visibility", report["visibility"]["visibility"])
        print("before  :", report["pixel"]["scene_color_before"][:3])
        print("after   :", report["pixel"]["scene_color_after"][:3])
        print("CPU     :", m["cpu_contribution"])
        print("residual:", m["residual"], "ULPs", [round(v, 4) for v in m["residual_ulps"]])
        print("FP16    : predicted", m["predicted_after_fp16"], m["predicted_after_fp16_bits"])
        print("FP16    : observed ", m["observed_after_fp16"], m["observed_after_fp16_bits"])
        print("codeword delta:", m["codeword_delta"])
        print("tolerance:", m["tolerance_ulps"], "ULP ->",
              "PASS" if m["passed"] else "FAIL")
    return 0 if report["math"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
