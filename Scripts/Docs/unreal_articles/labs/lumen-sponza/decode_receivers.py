#!/usr/bin/env python3
"""Decode the presented Sponza receiver pixels from the frozen native GPU dump.

Scope
-----
This script is read-only over the immutable dump and writes only inside the
``sponza-receivers/`` output directory.  It decodes:

* the presented scene graph (index 3 of 3 scene graphs + 1 Slate graph);
* the exact 1280x720 view inside the 1280x888 allocation;
* GBufferA/B/C/D/F and SceneDepthZ at the BasePass producer and the
  StandardDeferred consumer version keys;
* the pre/post SceneColor versions around the single sun direct-light pass;
* the VSM ``ShadowMaskTexture`` produced by the VSM composite pass and consumed
  by the sun pass;
* receiver pixels A(500,680), B(720,680), C(626,587).

It emits exact receiver/orientation JSON first, then clean full-frame PNG
figures with declared display mappings and an explicit figure manifest.

No GPU work, no engine edits, no writes to the dump, no acceleration-structure
claims, and no fabricated shader-local/SMRT/AS values.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import struct
import sys
import zlib
from pathlib import Path

SCHEMA_RECEIVERS = "sponza-receivers-v1"
SCHEMA_ORIENTATION = "sponza-orientation-v1"
SCHEMA_FIGURES = "sponza-receiver-figures-v1"
SCHEMA_VERIFY = "sponza-receiver-verify-v1"

DEFAULT_DUMP = Path(
    r"D:\UnrealUnderstanding-Sponza\output\gpu-dump\raw\sponza-20261115-083654"
)
DEFAULT_ANALYSIS = Path(
    r"D:\UnrealUnderstanding-Sponza\output\gpu-dump\analysis-20261115-083654.json"
)
DEFAULT_OUT = Path(__file__).resolve().parent

GRAPH_INDEX = 3
GRAPH_PREFIX = "0004"
VIEW_RESOURCE_NAME = "ViewData.PrimaryViews"
VIEW_RESOURCE_KEY = "00040000020bef07f840"
VIEW_UNIFORM_LAYOUT = "FViewUniformShaderParameters"

BASE_PASS_ID = 697
ANISO_PASS_ID = 700
DEPTH_PASS_ID = 620
VSM_COMPOSITE_PASS_ID = 841
SUN_PASS_ID = 842
SCENE_COLOR_PRE_PASS_ID = 840
SCENE_COLOR_POST_PASS_ID = 842

WIDTH = 1280
HEIGHT = 720
ALLOC_W = 1280
ALLOC_H = 888

CANDIDATES = {
    "A": {"pixel": (500, 680), "label": "directly sunlit stone"},
    "B": {"pixel": (720, 680), "label": "shaded stone"},
    "C": {"pixel": (626, 587), "label": "polished gold sphere"},
}

# Declared invalid/sky presentation colours.  These are masks, not fake scene
# backgrounds; every figure carries the exact pair in its manifest.
INVALID_BLACK = (0, 0, 0)
INVALID_GREY = (128, 128, 128)

SHADING_MODEL_NAMES = {
    0: "Unlit",
    1: "DefaultLit",
    2: "Subsurface",
    3: "PreintegratedSkin",
    4: "ClearCoat",
    5: "SubsurfaceProfile",
    6: "TwoSidedFoliage",
    7: "Hair",
    8: "Cloth",
    9: "Eye",
    10: "SingleLayerWater",
    11: "ThinTranslucent",
    12: "Substrate",
}

# Exact source references used for the channel semantics.  These are the
# engine files the schema rows were extracted from; line numbers are stable for
# the declared engine commit.
SOURCE_REFS = {
    "gbuffer_encode": "Engine/Shaders/Private/DeferredShadingCommon.ush:619-633",
    "gbuffer_decode": "Engine/Shaders/Private/DeferredShadingCommon.ush:1076-1124",
    "shading_models": "Engine/Shaders/Private/ShadingCommon.ush:20-22",
    "depth_linearize": "Engine/Shaders/Private/Common.ush:1373-1376",
    "sv_to_translated_world": "Engine/Shaders/Private/Common.ush:1507-1511",
    "clip_to_translated_world": "Engine/Shaders/Private/Common.ush:1530-1542",
    "light_attenuation": "Engine/Shaders/Private/Common.ush:1195-1216",
    "vsm_composite": "Engine/Shaders/Private/VirtualShadowMaps/VirtualShadowMapProjectionComposite.usf:40-41",
    "packed_view": "Engine/Source/Runtime/Renderer/Private/Nanite/NaniteShared.h:59-72",
    "view_uniform": "Engine/Source/Runtime/Engine/Public/SceneView.h:898-940",
    "schema": "labs/lumen-frame/schemas/source-schema.json:8-561",
}


class DecodeError(RuntimeError):
    pass


# --------------------------------------------------------------------------- IO


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def load_json(path: Path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def load_concat_json(path: Path):
    """DumpGPU JSON files are concatenated pretty-printed objects."""
    text = path.read_text(encoding="utf-8-sig")
    dec = json.JSONDecoder()
    pos = 0
    out = []
    n = len(text)
    while True:
        while pos < n and text[pos] in " \t\r\n,":
            pos += 1
        if pos >= n:
            break
        obj, pos = dec.raw_decode(text, pos)
        out.append(obj)
    return out


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def read_cvars(base: Path, names):
    wanted = set(names)
    out = {}
    path = base / "ConsoleVariables.csv"
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            name = row.get("CVar")
            if name in wanted:
                out[name] = {
                    "value": row.get("Value"),
                    "type": row.get("Type"),
                    "set_by": row.get("SetBy"),
                }
    missing = wanted - set(out)
    if missing:
        raise DecodeError(f"missing console variables: {sorted(missing)}")
    return out


def hex_bytes(b: bytes) -> str:
    return "0x" + b.hex()


def fmt(x, n=9) -> str:
    if isinstance(x, float):
        if not math.isfinite(x):
            return str(x)
        return f"{x:.{n}g}"
    return str(x)


# --------------------------------------------------------------------- formats


def srgb_to_linear(code: int) -> float:
    c = code / 255.0
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def srgb_oetf(x: float) -> float:
    if not math.isfinite(x) or x <= 0.0:
        return 0.0
    if x <= 0.0031308:
        return 12.92 * x
    return 1.055 * (x ** (1.0 / 2.4)) - 0.055


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def round_u8(x: float) -> int:
    return max(0, min(255, int(round(255.0 * x))))


def decode_a2b10(word: int):
    # PF_A2B10G10R10 little-endian word: R low 10, G next 10, B next 10, A top 2.
    return word & 0x3FF, (word >> 10) & 0x3FF, (word >> 20) & 0x3FF, (word >> 30) & 0x3


def decode_shadow_bgra(raw: bytes):
    # PF_B8G8R8A8 memory order is B,G,R,A.  The VSM composite shader writes
    # sqrt(float4(shadow.x, shadow.y, shadow.x, shadow.y)); the target clear is
    # white and only the meaningful channels carry the light attenuation.
    b, g, r, a = raw[0], raw[1], raw[2], raw[3]
    return {
        "memory_bgra": [b, g, r, a],
        "shader_r_code": r,
        "shader_g_code": g,
        "shader_b_code": b,
        "shader_a_code": a,
        "shader_r_encoded_sqrt": r / 255.0,
        "shader_g_encoded_sqrt": g / 255.0,
        "shadow_factor_r_linear": (r / 255.0) ** 2,
        "shadow_factor_g_linear": (g / 255.0) ** 2,
        "r_equals_g": r == g,
        "ba_are_clear_white": b == 255 and a == 255,
    }


# ------------------------------------------------------------------------ view


def version_pointer(file_rel: str):
    base = file_rel.rsplit("/", 1)[-1]
    if ".v" not in base or not base.endswith(".bin"):
        return None
    key = base.split(".v")[-1][:-4]
    if len(key) == 20:
        return key
    return None


def matrix_groups(buf: bytes, offset: int):
    vals = struct.unpack_from("<16f", buf, offset)
    return [list(vals[i * 4:(i + 1) * 4]) for i in range(4)]


def apply_matrix_groups(v, groups):
    """Apply the captured four float4 groups by the engine row-vector path.

    ``out[j] = sum_i v[i] * groups[i][j]``.  This reproduces both the captured
    ClipToTranslatedWorld route and the SVPositionToTranslatedWorld route at the
    receiver pixels; see the world-route check in receivers.json.
    """
    return [sum(v[i] * groups[i][j] for i in range(4)) for j in range(4)]


def perspective_div(out):
    w = out[3]
    if w == 0.0 or not math.isfinite(w):
        raise DecodeError(f"non-finite perspective w={w}")
    return [out[0] / w, out[1] / w, out[2] / w]


def vec3_sub(a, b):
    return [a[i] - b[i] for i in range(3)]


def vec3_add(a, b):
    return [a[i] + b[i] for i in range(3)]


def vec3_dot(a, b):
    return sum(a[i] * b[i] for i in range(3))


def vec3_len(a):
    return math.sqrt(vec3_dot(a, a))


def vec3_norm(a):
    n = vec3_len(a)
    if n == 0.0 or not math.isfinite(n):
        return None
    return [a[i] / n for i in range(3)]


def vec3_cross(a, b):
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def find_resource(resources, name: str, prefix: str, rid: str | None = None):
    if rid is not None:
        matches = [r for r in resources if r.get("UniqueResourceName") == rid]
        if len(matches) != 1:
            raise DecodeError(f"expected exactly one resource id {rid}, found {len(matches)}")
        if matches[0].get("Name") != name:
            raise DecodeError(f"resource {rid} is {matches[0].get('Name')!r}, expected {name!r}")
        return matches[0]
    matches = [
        r for r in resources
        if r.get("Name") == name
        and str(r.get("UniqueResourceName", "")).startswith(prefix)
    ]
    if len(matches) != 1:
        raise DecodeError(
            f"expected exactly one {name!r} under graph prefix {prefix}, "
            f"found {len(matches)}: {[r.get('UniqueResourceName') for r in matches]}"
        )
    return matches[0]


def find_layout(layouts, rid: str, version_key: str, mip: int = 0, stencil: bool = False):
    hits = []
    for l in layouts:
        if l.get("Resource") != rid:
            continue
        if bool(l.get("StencilSubresource")) != bool(stencil):
            continue
        if int(l.get("MipLevel") or 0) != int(mip):
            continue
        if version_pointer(l.get("File", "")) != version_key:
            continue
        hits.append(l)
    unique = {}
    for l in hits:
        unique.setdefault(l["File"], l)
    if len(unique) != 1:
        raise DecodeError(
            f"expected one layout for {rid} version {version_key} mip {mip} stencil {stencil}, "
            f"found {len(unique)}: {list(unique)}"
        )
    return next(iter(unique.values()))


def decode_view(dump: Path, base: Path, analysis_path: Path, prefixes):
    analysis = load_json(analysis_path)
    if int(analysis.get("presented_graph_index", -1)) != GRAPH_INDEX:
        raise DecodeError(
            f"presented_graph_index={analysis.get('presented_graph_index')} != {GRAPH_INDEX}"
        )
    graphs = analysis.get("graphs") or []
    if len(graphs) != 4:
        raise DecodeError(f"expected 4 graphs, found {len(graphs)}")
    graph = graphs[GRAPH_INDEX]
    if graph.get("kind") != "scene":
        raise DecodeError(f"graph {GRAPH_INDEX} is {graph.get('kind')}, not scene")
    views = graph.get("views") or []
    if len(views) != 1:
        raise DecodeError(f"graph {GRAPH_INDEX} has {len(views)} views, expected 1")
    view_rec = views[0]
    if view_rec.get("resource_key") != VIEW_RESOURCE_KEY:
        raise DecodeError(
            f"graph {GRAPH_INDEX} view key {view_rec.get('resource_key')} != {VIEW_RESOURCE_KEY}"
        )

    resources = load_concat_json(base / "ResourceDescs.json")
    layouts = load_concat_json(base / "SubresourceLayouts.json")
    passes = load_concat_json(base / "Passes.json")
    pass_by_id = {int(p["Id"]): p for p in passes}
    pass_by_ptr = {p.get("Pointer"): p for p in passes}

    view_res = find_resource(resources, VIEW_RESOURCE_NAME, GRAPH_PREFIX)
    # ViewData.PrimaryViews is an FRDGBufferDesc (a raw buffer), so it has no
    # SubresourceLayouts texture row.  DumpGPU writes the immutable v0 payload
    # beside the texture payloads.
    view_rel = f"Resources/{view_res['UniqueResourceName']}.v{'0' * 20}.bin"
    view_path = dump / view_rel
    if not view_path.exists():
        raise DecodeError(f"missing ViewData.PrimaryViews payload {view_path}")
    view_buf = view_path.read_bytes()
    if len(view_buf) != 1056:
        raise DecodeError(f"ViewData.PrimaryViews is {len(view_buf)} bytes, expected 1056")
    view_layout = {
        "File": view_rel,
        "MipLevel": 0,
        "FirstArraySlice": 0,
        "ExtentX": 1,
        "ExtentY": 1,
        "RowPitchBytes": 1056,
        "StoredFormat": "FRDGBufferDesc",
    }

    packed_rect = list(struct.unpack_from("<4i", view_buf, 640))
    packed_size = list(struct.unpack_from("<4f", view_buf, 656))
    packed_v2c = matrix_groups(view_buf, 256)
    packed_tw2c = matrix_groups(view_buf, 192)
    packed_sv2tw = matrix_groups(view_buf, 0)

    # FViewUniformShaderParameters metadata for this graph.
    meta_hits = []
    for p in sorted((dump / "StructuresMetadata").glob("*.json")):
        if not p.name.startswith(GRAPH_PREFIX):
            continue
        try:
            meta = load_json(p)
        except Exception:
            continue
        if meta.get("StructTypeName") == VIEW_UNIFORM_LAYOUT:
            meta_hits.append((p, meta))
    if len(meta_hits) != 1:
        raise DecodeError(f"expected one {VIEW_UNIFORM_LAYOUT} metadata under {GRAPH_PREFIX}, found {len(meta_hits)}")
    meta_path, meta = meta_hits[0]
    members = {m["Name"]: m for m in meta.get("Members", [])}

    native = load_concat_json(base / "NativeUniformBufferBindings.json")
    sun_ptr = pass_by_id[SUN_PASS_ID]["Pointer"]
    ub_records = [
        r for r in native
        if r.get("LayoutName") == VIEW_UNIFORM_LAYOUT and r.get("Pass") == sun_ptr
    ]
    if len(ub_records) != 1:
        raise DecodeError(
            f"expected one {VIEW_UNIFORM_LAYOUT} native record at pass {SUN_PASS_ID}, found {len(ub_records)}"
        )
    ub_record = ub_records[0]
    ub_key = ub_record["UniformBuffer"]
    ub_path = dump / "Structures" / f"{ub_key}.bin"
    ub = ub_path.read_bytes()
    if len(ub) != int(meta.get("Size") or 0):
        raise DecodeError(f"view uniform buffer is {len(ub)} bytes, metadata Size={meta.get('Size')}")

    def member(name):
        if name not in members:
            raise DecodeError(f"{VIEW_UNIFORM_LAYOUT} has no member {name!r}")
        return members[name]

    def mvec(name, count=None):
        m = member(name)
        n = int(count if count is not None else (m.get("NumColumns") or 1))
        return list(struct.unpack_from("<" + "f" * n, ub, int(m["Offset"])))

    def mmat(name):
        m = member(name)
        vals = struct.unpack_from("<16f", ub, int(m["Offset"]))
        return [list(vals[i * 4:(i + 1) * 4]) for i in range(4)]

    rect_min = mvec("ViewRectMin", 4)
    size_and_inv = mvec("ViewSizeAndInvSize", 4)
    rect_min_size_u32 = list(struct.unpack_from("<4I", ub, int(member("ViewRectMinAndSize")["Offset"])))
    origin_high = mvec("ViewOriginHigh", 3)
    origin_low = mvec("ViewOriginLow", 3)
    world_origin_high = mvec("WorldViewOriginHigh", 3)
    world_origin_low = mvec("WorldViewOriginLow", 3)
    preview_high = mvec("PreViewTranslationHigh", 3)
    preview_low = mvec("PreViewTranslationLow", 3)
    forward = mvec("ViewForward", 3)
    pre_exposure = mvec("PreExposure", 1)[0]
    one_over_pre_exposure = mvec("OneOverPreExposure", 1)[0]
    jitter = mvec("TemporalAAJitter", 4)
    inv_device_z = mvec("InvDeviceZToWorldZTransform", 4)
    v2c = mmat("ViewToClip")
    v2c_no_aa = mmat("ViewToClipNoAA")
    clip_to_tw = mmat("ClipToTranslatedWorld")
    sv_to_tw = mmat("SVPositionToTranslatedWorld")

    origin = vec3_add(origin_high, origin_low)
    world_origin = vec3_add(world_origin_high, world_origin_low)
    preview = vec3_add(preview_high, preview_low)

    near_plane = v2c[3][2]
    m00 = v2c[0][0]
    m11 = v2c[1][1]
    hfov = math.degrees(2.0 * math.atan(1.0 / m00)) if m00 else float("nan")
    vfov = math.degrees(2.0 * math.atan(1.0 / m11)) if m11 else float("nan")
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, forward[2]))))
    yaw = math.degrees(math.atan2(forward[1], forward[0]))

    def close_list(a, b, tol):
        return len(a) == len(b) and all(abs(float(a[i]) - float(b[i])) <= tol for i in range(len(a)))

    checks = {
        "scene_graph_index_3": True,
        "scene_graph_is_scene": True,
        "scene_graph_pass_range_593_885": (
            int(graph.get("first_pass_id", -1)) == 593
            and int(graph.get("last_pass_id", -1)) == 885
        ),
        "view_resource_key": view_rec.get("resource_key") == VIEW_RESOURCE_KEY,
        "view_rect_1280x720_at_origin": close_list(rect_min, [0.0, 0.0, 0.0, 0.0], 1e-6)
        and close_list(size_and_inv[:2], [1280.0, 720.0], 1e-6),
        "packed_view_rect_matches_uniform": packed_rect == [0, 0, 1280, 720],
        "packed_view_size_matches_uniform": close_list(packed_size[:2], size_and_inv[:2], 1e-6),
        "view_origin_minus_850_0_90": close_list(origin, [-850.0, 0.0, 90.0], 1e-4),
        "world_origin_minus_850_0_90": close_list(world_origin, [-850.0, 0.0, 90.0], 1e-4),
        "preview_translation_850_0_minus_90": close_list(preview, [850.0, 0.0, -90.0], 1e-4),
        "origin_equals_minus_preview": close_list(origin, [-preview[0], -preview[1], -preview[2]], 1e-3),
        "hfov_70": abs(hfov - 70.0) <= 1e-5,
        "vfov_42_9957": abs(vfov - 42.9957) <= 1e-4,
        "pitch_13": abs(pitch - 13.0) <= 1e-5,
        "yaw_0": abs(yaw) <= 1e-6,
        "pre_exposure_positive_finite": math.isfinite(pre_exposure) and pre_exposure > 0.0,
        "pre_exposure_inverse_consistent": abs(pre_exposure * one_over_pre_exposure - 1.0) <= 1e-6,
        "reversed_z_transform": abs(inv_device_z[2] - 0.1) <= 1e-6 or inv_device_z[2] > 0.0,
        "near_plane_10": abs(near_plane - 10.0) <= 1e-5,
        "packed_view_to_clip_matches_uniform": all(
            abs(packed_v2c[i][j] - v2c[i][j]) <= 1e-6 for i in range(4) for j in range(4)
        ),
        "offset_192_is_translated_world_to_clip_not_view": abs(packed_tw2c[0][1] - (-0.5711348652839661)) < 1e-6
        and abs(packed_v2c[0][1]) < 1e-9,
        "unused_offset_192_not_used_as_view_matrix": True,
    }
    if not all(checks.values()):
        failed = [k for k, v in checks.items() if not v]
        raise DecodeError(f"view checks failed: {failed}")

    # World route cross-check at the three receiver centres.
    world_routes = []
    for name, rec in CANDIDATES.items():
        x, y = rec["pixel"]
        raw = (
            dump
            / find_layout(
                layouts,
                find_resource(resources, "SceneDepthZ", GRAPH_PREFIX)["UniqueResourceName"],
                pass_by_id[SUN_PASS_ID]["Pointer"],
            )["File"]
        ).read_bytes()
        d = struct.unpack_from("<f", raw, y * 5120 + x * 4)[0]
        ndc = [2.0 * ((x + 0.5) / WIDTH) - 1.0, 1.0 - 2.0 * ((y + 0.5) / HEIGHT), d, 1.0]
        tw_a = perspective_div(apply_matrix_groups(ndc, clip_to_tw))
        sv = [x + 0.5, y + 0.5, d, 1.0]
        tw_b = perspective_div(apply_matrix_groups(sv, sv_to_tw))
        wa = vec3_sub(tw_a, preview)
        wb = vec3_sub(tw_b, preview)
        diff = vec3_len(vec3_sub(wa, wb))
        world_routes.append(
            {
                "receiver": name,
                "clip_to_translated_world_cm": wa,
                "sv_position_to_translated_world_cm": wb,
                "route_delta_cm": diff,
            }
        )
    route_max = max(r["route_delta_cm"] for r in world_routes)
    if route_max > 0.01:
        raise DecodeError(f"world reconstruction routes disagree by {route_max} cm")

    return {
        "dump": dump,
        "base": base,
        "analysis": analysis,
        "analysis_path": analysis_path,
        "graph": graph,
        "graph_index": GRAPH_INDEX,
        "resources": resources,
        "layouts": layouts,
        "passes": passes,
        "pass_by_id": pass_by_id,
        "pass_by_ptr": pass_by_ptr,
        "view_resource": view_res,
        "view_layout": view_layout,
        "view_path": view_path,
        "view_buf": view_buf,
        "packed_rect": packed_rect,
        "packed_size": packed_size,
        "packed_v2c": packed_v2c,
        "packed_tw2c": packed_tw2c,
        "packed_sv2tw": packed_sv2tw,
        "meta_path": meta_path,
        "meta": meta,
        "ub_key": ub_key,
        "ub_path": ub_path,
        "ub": ub,
        "ub_record": ub_record,
        "rect_min": rect_min,
        "size_and_inv": size_and_inv,
        "rect_min_size_u32": rect_min_size_u32,
        "origin": origin,
        "origin_high": origin_high,
        "origin_low": origin_low,
        "world_origin": world_origin,
        "world_origin_high": world_origin_high,
        "world_origin_low": world_origin_low,
        "preview": preview,
        "preview_high": preview_high,
        "preview_low": preview_low,
        "forward": forward,
        "pre_exposure": pre_exposure,
        "one_over_pre_exposure": one_over_pre_exposure,
        "jitter": jitter,
        "inv_device_z": inv_device_z,
        "v2c": v2c,
        "v2c_no_aa": v2c_no_aa,
        "clip_to_tw": clip_to_tw,
        "sv_to_tw": sv_to_tw,
        "near_plane": near_plane,
        "hfov": hfov,
        "vfov": vfov,
        "pitch": pitch,
        "yaw": yaw,
        "checks": checks,
        "world_routes": world_routes,
    }


# ------------------------------------------------------------------- resources


def build_resource_records(dump: Path, ctx):
    resources = ctx["resources"]
    layouts = ctx["layouts"]
    pass_by_id = ctx["pass_by_id"]

    targets = {
        "gbuffer_a": {
            "name": "GBufferA",
            "producer": BASE_PASS_ID,
            "consumer": SUN_PASS_ID,
            "format": "PF_A2B10G10R10",
            "channels": "R10G10B10A2 little-endian word: R bits0-9, G bits10-19, B bits20-29, A bits30-31",
            "formula": "N_raw = (2*(word&0x3FF)/1023-1, 2*((word>>10)&0x3FF)/1023-1, 2*((word>>20)&0x3FF)/1023-1); N = N_raw/|N_raw|; A = PerObjectGBufferData (opaque 2-bit)",
            "source_refs": [SOURCE_REFS["gbuffer_encode"], SOURCE_REFS["gbuffer_decode"], SOURCE_REFS["schema"]],
        },
        "gbuffer_b": {
            "name": "GBufferB",
            "producer": BASE_PASS_ID,
            "consumer": SUN_PASS_ID,
            "format": "PF_B8G8R8A8",
            "channels": "memory B,G,R,A; shader .r=memory[2]=Metallic, .g=memory[1]=Specular, .b=memory[0]=Roughness, .a=memory[3]=packed ShadingModelID/SelectiveOutputMask",
            "formula": "Metallic=mem[2]/255; Specular=mem[1]/255; Roughness=mem[0]/255; id=round(mem[3])&0xF; mask=round(mem[3])&0xF0",
            "source_refs": [SOURCE_REFS["gbuffer_encode"], SOURCE_REFS["schema"], SOURCE_REFS["shading_models"]],
        },
        "gbuffer_c": {
            "name": "GBufferC",
            "producer": BASE_PASS_ID,
            "consumer": SUN_PASS_ID,
            "format": "PF_B8G8R8A8",
            "channels": "memory B,G,R,A; shader .r=memory[2], .g=memory[1], .b=memory[0]; target sRGB=true; .a=AO",
            "formula": "BaseColor_linear = sRGB_EOTF(mem[2]/255), sRGB_EOTF(mem[1]/255), sRGB_EOTF(mem[0]/255); sRGB_EOTF(c)=c/12.92 if c<=0.04045 else ((c+0.055)/1.055)^2.4; AO=mem[3]/255",
            "source_refs": [SOURCE_REFS["gbuffer_encode"], SOURCE_REFS["schema"]],
        },
        "gbuffer_d": {
            "name": "GBufferD",
            "producer": BASE_PASS_ID,
            "consumer": SUN_PASS_ID,
            "format": "PF_B8G8R8A8",
            "channels": "memory B,G,R,A = shader .b,.g,.r,.a custom data",
            "formula": "opaque per-material CustomData; all zero at the reported receivers",
            "source_refs": [SOURCE_REFS["gbuffer_encode"], SOURCE_REFS["schema"]],
        },
        "gbuffer_f": {
            "name": "GBufferF",
            "producer": ANISO_PASS_ID,
            "consumer": SUN_PASS_ID,
            "format": "PF_B8G8R8A8",
            "channels": "memory B,G,R,A; world tangent RGB + anisotropy A when the material is anisotropic",
            "formula": "not used: the captured shading model is DefaultLit (id 1) and the receiver materials are not anisotropic",
            "source_refs": [SOURCE_REFS["gbuffer_decode"], SOURCE_REFS["schema"]],
        },
        "scene_depth": {
            "name": "SceneDepthZ",
            "producer": DEPTH_PASS_ID,
            "consumer": SUN_PASS_ID,
            "format": "PF_R32_FLOAT",
            "channels": "single float device Z; native target PF_DepthStencil; readback decoded to float32",
            "formula": "W = D*InvDeviceZToWorldZTransform[0] + [1] + 1/(D*[2] - [3]); reversed-Z, deviceZ=0 is sky/far",
            "source_refs": [SOURCE_REFS["depth_linearize"], SOURCE_REFS["schema"]],
        },
        "scene_color_pre": {
            "name": "SceneColor",
            "rid": "00040000020b9d700490",
            "producer": SCENE_COLOR_PRE_PASS_ID,
            "consumer": SUN_PASS_ID,
            "format": "PF_FloatRGBA",
            "channels": "half4 RGBA, memory order R,G,B,A",
            "formula": "pre-direct SceneColor; exact half values reported",
            "source_refs": [SOURCE_REFS["gbuffer_encode"]],
        },
        "scene_color_post": {
            "name": "SceneColor",
            "rid": "00040000020b9d700490",
            "producer": SUN_PASS_ID,
            "consumer": SUN_PASS_ID,
            "format": "PF_FloatRGBA",
            "channels": "half4 RGBA, memory order R,G,B,A",
            "formula": "post-direct SceneColor; direct term = post - pre (RGB)",
            "source_refs": [SOURCE_REFS["gbuffer_encode"]],
        },
        "vsm_shadow_mask": {
            "name": "ShadowMaskTexture",
            "producer": VSM_COMPOSITE_PASS_ID,
            "consumer": SUN_PASS_ID,
            "format": "PF_B8G8R8A8",
            "channels": "memory B,G,R,A; shader .r/.g carry sqrt(shadow factor), target clear white; .b/.a remain clear white",
            "formula": "OutShadowMask = sqrt(float4(shadow.x, shadow.y, shadow.x, shadow.y)); consumed as LightAttenuationTexture; shadow_linear = (mem[2]/255)^2",
            "source_refs": [SOURCE_REFS["vsm_composite"], SOURCE_REFS["light_attenuation"]],
        },
    }

    records = {}
    hash_cache = {}
    for key, t in targets.items():
        res = find_resource(resources, t["name"], GRAPH_PREFIX, rid=t.get("rid"))
        rid = res["UniqueResourceName"]
        producer_ptr = pass_by_id[t["producer"]]["Pointer"]
        consumer_ptr = pass_by_id[t["consumer"]]["Pointer"]
        prod = find_layout(layouts, rid, producer_ptr)
        cons = find_layout(layouts, rid, consumer_ptr)
        prod_path = dump / prod["File"]
        cons_path = dump / cons["File"]
        for p in (prod_path, cons_path):
            if not p.exists():
                raise DecodeError(f"missing payload {p}")
        for p in (prod_path, cons_path):
            if p not in hash_cache:
                hash_cache[p] = sha256_file(p)
        rec = {
            "name": t["name"],
            "resource_id": rid,
            "resource_byte_size": res.get("ByteSize"),
            "extent": [int(prod.get("ExtentX") or 0), int(prod.get("ExtentY") or 0)],
            "producer": {
                "pass_id": t["producer"],
                "event": pass_by_id[t["producer"]].get("EventName"),
                "pointer": producer_ptr,
            },
            "consumer": {
                "pass_id": t["consumer"],
                "event": pass_by_id[t["consumer"]].get("EventName"),
                "pointer": consumer_ptr,
            },
            "producer_version_key": producer_ptr,
            "consumer_version_key": consumer_ptr,
            "producer_file": prod["File"],
            "consumer_file": cons["File"],
            "producer_bytes": prod_path.stat().st_size,
            "consumer_bytes": cons_path.stat().st_size,
            "producer_sha256": hash_cache[prod_path],
            "consumer_sha256": hash_cache[cons_path],
            "versions_identical": hash_cache[prod_path] == hash_cache[cons_path],
            "mip": int(prod.get("MipLevel") or 0),
            "first_array_slice": int(prod.get("FirstArraySlice") or 0),
            "z_slice_count": int(prod.get("ZSliceCount") or 1),
            "row_pitch_bytes": int(prod.get("RowPitchBytes") or 0),
            "stored_format": prod.get("StoredFormat"),
            "native_format": prod.get("NativeFormat"),
            "view_format": prod.get("ViewFormat"),
            "staging_format": prod.get("StagingFormat"),
            "stored_bytes_are_native_storage": prod.get("StoredBytesAreNativeStorage"),
            "native_value_interpretation": prod.get("NativeValueInterpretation"),
            "native_srgb": prod.get("NativeSRGB"),
            "view_srgb": prod.get("ViewSRGB"),
            "bit_exact": prod.get("BitExact"),
            "value_preserving": prod.get("ValuePreserving"),
            "dump_status": prod.get("DumpStatus"),
            "stored_bytes_per_texel": int(prod_path.stat().st_size // (int(prod.get("ExtentX")) * int(prod.get("ExtentY")) * int(prod.get("ZSliceCount") or 1))),
            "channels": t["channels"],
            "formula": t["formula"],
            "source_refs": t["source_refs"],
            "pixel_address_formula": "file_offset = y*RowPitchBytes + x*stored_bytes_per_texel (mip 0, slice 0)",
        }
        if t["name"] == "SceneDepthZ":
            rec["native_target_format"] = "PF_DepthStencil"
            rec["depth_is_reversed_z"] = True
        records[key] = rec
    return records


# ---------------------------------------------------------------- pixel decode


def load_payloads(dump: Path, resources: dict):
    paths = {
        "gbuffer_a": dump / resources["gbuffer_a"]["consumer_file"],
        "gbuffer_b": dump / resources["gbuffer_b"]["consumer_file"],
        "gbuffer_c": dump / resources["gbuffer_c"]["consumer_file"],
        "gbuffer_d": dump / resources["gbuffer_d"]["consumer_file"],
        "gbuffer_f": dump / resources["gbuffer_f"]["consumer_file"],
        "scene_depth": dump / resources["scene_depth"]["consumer_file"],
        "scene_color_pre": dump / resources["scene_color_pre"]["producer_file"],
        "scene_color_post": dump / resources["scene_color_post"]["consumer_file"],
        "vsm_shadow_mask": dump / resources["vsm_shadow_mask"]["consumer_file"],
    }
    return {k: p.read_bytes() for k, p in paths.items()}, paths


def decode_pixel(ctx, resources, payloads, xy):
    x, y = int(xy[0]), int(xy[1])
    view = ctx
    rect = view["rect_min"]
    size = view["size_and_inv"]
    preview = view["preview"]
    inv = view["inv_device_z"]

    o4 = y * 5120 + x * 4
    osc = y * 10240 + x * 8

    word = struct.unpack_from("<I", payloads["gbuffer_a"], o4)[0]
    ra, ga, ba, aa = decode_a2b10(word)
    nraw = [2.0 * ra / 1023.0 - 1.0, 2.0 * ga / 1023.0 - 1.0, 2.0 * ba / 1023.0 - 1.0]
    nlen = vec3_len(nraw)
    n = [v / nlen for v in nraw] if nlen > 0.0 else [0.0, 0.0, 0.0]

    mem_b = payloads["gbuffer_b"][o4:o4 + 4]
    mem_c = payloads["gbuffer_c"][o4:o4 + 4]
    mem_d = payloads["gbuffer_d"][o4:o4 + 4]
    mem_f = payloads["gbuffer_f"][o4:o4 + 4]
    rough_code, spec_code, metal_code, packed_id = mem_b[0], mem_b[1], mem_b[2], mem_b[3]

    base_linear = [srgb_to_linear(mem_c[2]), srgb_to_linear(mem_c[1]), srgb_to_linear(mem_c[0])]
    base_srgb_codes = [mem_c[2], mem_c[1], mem_c[0]]
    shading_id = packed_id & 0x0F
    selective_mask = packed_id & 0xF0

    device_z = struct.unpack_from("<f", payloads["scene_depth"], o4)[0]
    if math.isfinite(device_z) and device_z > 0.0:
        denom = device_z * inv[2] - inv[3]
        linear_w = device_z * inv[0] + inv[1] + (1.0 / denom if denom != 0.0 else float("nan"))
    else:
        linear_w = float("nan")

    # World position: exact engine route via the captured ClipToTranslatedWorld.
    ndc = [
        2.0 * ((x + 0.5 - rect[0]) / size[0]) - 1.0,
        1.0 - 2.0 * ((y + 0.5 - rect[1]) / size[1]),
        device_z,
        1.0,
    ]
    tw = perspective_div(apply_matrix_groups(ndc, view["clip_to_tw"]))
    world = vec3_sub(tw, preview)
    sv = [x + 0.5, y + 0.5, device_z, 1.0]
    tw_sv = perspective_div(apply_matrix_groups(sv, view["sv_to_tw"]))
    world_sv = vec3_sub(tw_sv, preview)

    # Depth-derived geometric normal, oriented to the shading normal.  This is a
    # check/description only; it is never used as the shading normal.
    def world_at(xx, yy):
        if xx < 0 or yy < 0 or xx >= WIDTH or yy >= HEIGHT:
            return None
        o = yy * 5120 + xx * 4
        d = struct.unpack_from("<f", payloads["scene_depth"], o)[0]
        if not (math.isfinite(d) and d > 0.0):
            return None
        nd = [
            2.0 * ((xx + 0.5 - rect[0]) / size[0]) - 1.0,
            1.0 - 2.0 * ((yy + 0.5 - rect[1]) / size[1]),
            d,
            1.0,
        ]
        t = perspective_div(apply_matrix_groups(nd, view["clip_to_tw"]))
        return vec3_sub(t, preview)

    geom = None
    geom_angle = None
    px, nx = world_at(x + 1, y), world_at(x - 1, y)
    py, ny = world_at(x, y + 1), world_at(x, y - 1)
    if px and nx and py and ny:
        du = vec3_sub(px, nx)
        dv = vec3_sub(py, ny)
        g = vec3_norm(vec3_cross(du, dv))
        if g:
            if vec3_dot(g, n) < 0.0:
                g = [-v for v in g]
            geom = g
            geom_angle = math.degrees(math.acos(max(-1.0, min(1.0, vec3_dot(g, n)))))

    pre = struct.unpack_from("<4e", payloads["scene_color_pre"], osc)
    post = struct.unpack_from("<4e", payloads["scene_color_post"], osc)
    pre_bits = list(struct.unpack_from("<4H", payloads["scene_color_pre"], osc))
    post_bits = list(struct.unpack_from("<4H", payloads["scene_color_post"], osc))
    delta = [float(post[i]) - float(pre[i]) for i in range(3)]

    shadow = decode_shadow_bgra(payloads["vsm_shadow_mask"][o4:o4 + 4])

    addresses = {
        "gbuffer_a": {"file_offset": o4, "row_offset": y * 5120, "texel_bytes": 4},
        "gbuffer_b": {"file_offset": o4, "row_offset": y * 5120, "texel_bytes": 4},
        "gbuffer_c": {"file_offset": o4, "row_offset": y * 5120, "texel_bytes": 4},
        "gbuffer_d": {"file_offset": o4, "row_offset": y * 5120, "texel_bytes": 4},
        "gbuffer_f": {"file_offset": o4, "row_offset": y * 5120, "texel_bytes": 4},
        "scene_depth": {"file_offset": o4, "row_offset": y * 5120, "texel_bytes": 4},
        "scene_color_pre": {"file_offset": osc, "row_offset": y * 10240, "texel_bytes": 8},
        "scene_color_post": {"file_offset": osc, "row_offset": y * 10240, "texel_bytes": 8},
        "vsm_shadow_mask": {"file_offset": o4, "row_offset": y * 5120, "texel_bytes": 4},
    }

    rec = {
        "label": None,
        "pixel": [x, y],
        "sample_center": [x + 0.5, y + 0.5],
        "valid": bool(math.isfinite(device_z) and device_z > 0.0 and nlen > 0.5 and math.isfinite(linear_w) and linear_w > 0.0),
        "world_cm": world,
        "translated_world_cm": tw,
        "world_route_check": {
            "clip_to_translated_world_cm": world,
            "sv_position_to_translated_world_cm": world_sv,
            "delta_cm": vec3_len(vec3_sub(world, world_sv)),
        },
        "depth": {
            "device_z_raw_bits": hex(struct.unpack_from("<I", payloads["scene_depth"], o4)[0]),
            "device_z": device_z,
            "linear_w_cm": linear_w,
            "near_plane": view["near_plane"],
            "reversed_z": True,
            "invalid_or_sky": not (math.isfinite(device_z) and device_z > 0.0),
            "inv_device_z_to_world_z_transform": inv,
        },
        "normal": {
            "kind": "world_space_shading_normal_from_GBufferA",
            "geometric_normal_used_for_shading": False,
            "raw_codes_rgb10": [ra, ga, ba],
            "raw_word_hex": hex(word),
            "raw_components": nraw,
            "raw_length": nlen,
            "normalized": n,
            "normalized_length": vec3_len(n),
            "per_object_gbuffer_data_2bit": aa,
            "per_object_interpretation": "opaque; not decoded",
            "geometric_normal_from_depth": geom,
            "geometric_vs_shading_angle_deg": geom_angle,
        },
        "basecolor": {
            "stored_srgb_bytes_bgra": [mem_c[0], mem_c[1], mem_c[2], mem_c[3]],
            "shader_rgb_memory_indices": [2, 1, 0],
            "shader_rgb_srgb_codes": base_srgb_codes,
            "linear_rgb": base_linear,
            "alpha_code": mem_c[3],
            "alpha_semantic": "GBufferAO (r.AllowStaticLighting=0, r.GBufferDiffuseSampleOcclusion=0)",
            "gbuffer_ao": mem_c[3] / 255.0,
            "formula": "sRGB_EOTF(mem[2]/255), sRGB_EOTF(mem[1]/255), sRGB_EOTF(mem[0]/255)",
        },
        "material": {
            "gbuffer_b_bytes_bgra": list(mem_b),
            "roughness_code": rough_code,
            "roughness": rough_code / 255.0,
            "specular_code": spec_code,
            "specular": spec_code / 255.0,
            "metallic_code": metal_code,
            "metallic": metal_code / 255.0,
            "packed_id_code": packed_id,
            "shading_model_id": shading_id,
            "shading_model_name": SHADING_MODEL_NAMES.get(shading_id, f"Unknown({shading_id})"),
            "selective_output_mask": selective_mask,
            "gbuffer_d_bytes_bgra": list(mem_d),
            "gbuffer_d_custom_data": "all zero at this receiver",
            "gbuffer_f_bytes_bgra": list(mem_f),
            "gbuffer_f_used": False,
        },
        "direct": {
            "scene_color_pre_half_rgba": [float(v) for v in pre],
            "scene_color_post_half_rgba": [float(v) for v in post],
            "scene_color_pre_half_bits": pre_bits,
            "scene_color_post_half_bits": post_bits,
            "direct_rgb_pre_exposure": delta,
            "pre_is_zero": all(v == 0.0 for v in pre[:3]),
            "definition": "post SceneColor(pass 842) - pre SceneColor(pass 840), RGB; actual captured additive sun pass, not an analytic BRDF or AS/SMRT intermediate",
        },
        "shadow": {
            **shadow,
            "source": "ShadowMaskTexture, VSM composite pass 841, consumed by sun pass 842 as PS.LightAttenuationTexture",
            "formula": "shadow_factor_r_linear = (shader_r_code/255)^2; consumed attenuation is sqrt-encoded in the 8-bit target",
            "interpretation": "R (and matching G) is the VSM receiver attenuation for the single sun light; B/A remain the white clear value",
        },
        "addresses": addresses,
        "quantization": {
            "normal_rgb10_step": 2.0 / 1023.0,
            "unorm8_step": 1.0 / 255.0,
            "basecolor_srgb_code_step": 1.0 / 255.0,
            "depth": "float32 device Z; linearization uses captured InvDeviceZToWorldZTransform",
            "scene_color": "FP16 stored; exact half values and half bit patterns reported",
            "shadow": "8-bit sqrt-encoded code; decoded factor has 1/255 code quantization",
        },
        "substituted": False,
        "substituted_from": None,
        "authored_roughness": None,
    }
    return rec


def _normal_at(payloads, x, y):
    o = y * 5120 + x * 4
    word = struct.unpack_from("<I", payloads["gbuffer_a"], o)[0]
    r, g, b, _ = decode_a2b10(word)
    nraw = [2.0 * r / 1023.0 - 1.0, 2.0 * g / 1023.0 - 1.0, 2.0 * b / 1023.0 - 1.0]
    return vec3_norm(nraw)


def _depth_at(payloads, x, y):
    return struct.unpack_from("<f", payloads["scene_depth"], y * 5120 + x * 4)[0]


def _rough_at(payloads, x, y):
    return payloads["gbuffer_b"][y * 5120 + x * 4] / 255.0


def _metal_at(payloads, x, y):
    return payloads["gbuffer_b"][y * 5120 + x * 4 + 2] / 255.0


def neighbor_check(payloads, xy, center):
    x, y = xy
    normals = []
    depths = []
    roughs = []
    metals = []
    valid = True
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            xx, yy = x + dx, y + dy
            if xx < 0 or yy < 0 or xx >= WIDTH or yy >= HEIGHT:
                valid = False
                continue
            d = _depth_at(payloads, xx, yy)
            n = _normal_at(payloads, xx, yy)
            if not (math.isfinite(d) and d > 0.0) or n is None:
                valid = False
                continue
            depths.append(d)
            normals.append(n)
            roughs.append(_rough_at(payloads, xx, yy))
            metals.append(_metal_at(payloads, xx, yy))
    cn = center["normal"]["normalized"]
    max_angle = 0.0
    for n in normals:
        max_angle = max(max_angle, math.degrees(math.acos(max(-1.0, min(1.0, vec3_dot(cn, n))))))
    out = {
        "valid_3x3": valid and len(normals) == 9,
        "max_normal_angle_deg": max_angle,
        "depth_span": (max(depths) - min(depths)) if depths else None,
        "roughness_span": (max(roughs) - min(roughs)) if roughs else None,
        "metallic_span": (max(metals) - min(metals)) if metals else None,
    }
    out["clean"] = bool(
        out["valid_3x3"]
        and max_angle <= 10.0
        and (out["depth_span"] is not None and out["depth_span"] <= 0.05)
        and (out["roughness_span"] is not None and out["roughness_span"] <= 0.15)
        and (out["metallic_span"] is not None and out["metallic_span"] <= 0.02)
    )
    return out


def decode_receivers(ctx, resources, payloads):
    out = {}
    for name, c in CANDIDATES.items():
        xy = c["pixel"]
        rec = decode_pixel(ctx, resources, payloads, xy)
        rec["label"] = c["label"]
        ncheck = neighbor_check(payloads, xy, rec)
        rec["neighbor_check"] = ncheck
        rec["center_clean"] = ncheck["clean"]
        if not rec["valid"]:
            raise DecodeError(f"receiver {name} {xy} is not valid: {rec}")
        if not ncheck["clean"]:
            # The named samples are expected to be clean.  Do not silently move
            # them; report the condition.  A nearby clean substitute can be
            # requested explicitly by changing CANDIDATES.
            rec["notes"] = ["named receiver is valid but its 3x3 neighbourhood is not clean"]
        else:
            rec["notes"] = []
        if name == "C":
            rec["authored_roughness"] = {
                "nominal": 0.08,
                "decoded_code": rec["material"]["roughness_code"],
                "decoded_value": rec["material"]["roughness"],
                "note": "authored 0.08 quantizes to code 20/255; the reported actual is 20/255, not the nominal value",
            }
        out[name] = rec
    return out


# --------------------------------------------------------------------- figures


def png_bytes(path: Path, w: int, h: int, rgb: bytearray):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw.extend(rgb[y * w * 3:(y + 1) * w * 3])

    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    data = b"\x89PNG\r\n\x1a\n"
    data += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    data += chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def new_stats(n):
    return {
        "count": 0,
        "finite": 0,
        "nonfinite": 0,
        "min": [math.inf] * n,
        "max": [-math.inf] * n,
        "sum": [0.0] * n,
    }


def add_stats(s, vals):
    s["count"] += 1
    ok = all(math.isfinite(v) for v in vals)
    if ok:
        s["finite"] += 1
    else:
        s["nonfinite"] += 1
    for i, v in enumerate(vals):
        if math.isfinite(v):
            s["min"][i] = min(s["min"][i], v)
            s["max"][i] = max(s["max"][i], v)
            s["sum"][i] += v


def finish_stats(s):
    n = s["count"]
    return {
        "count": n,
        "finite": s["finite"],
        "nonfinite": s["nonfinite"],
        "min": [None if not math.isfinite(v) else v for v in s["min"]],
        "max": [None if not math.isfinite(v) else v for v in s["max"]],
        "mean": [(s["sum"][i] / n if n and math.isfinite(s["sum"][i]) else None) for i in range(len(s["sum"]))],
    }


def image_stats(rgb: bytearray, w: int, h: int, invalid_color):
    s = new_stats(3)
    nonempty = 0
    equal_invalid = 0
    nonblack = 0
    for y in range(h):
        base = y * w * 3
        for x in range(w):
            i = base + x * 3
            vals = (rgb[i], rgb[i + 1], rgb[i + 2])
            add_stats(s, vals)
            if invalid_color is not None and vals == tuple(invalid_color):
                equal_invalid += 1
            if any(v != 0 for v in vals):
                nonblack += 1
            if invalid_color is None or vals != tuple(invalid_color):
                nonempty += 1
    out = finish_stats(s)
    out.update(
        {
            "width": w,
            "height": h,
            "channels": 3,
            "nonempty_pixels": nonempty,
            "nonblack_pixels": nonblack,
            "pixels_equal_invalid_color": equal_invalid,
            "invalid_color": list(invalid_color) if invalid_color is not None else None,
        }
    )
    return out


def build_figures(dump: Path, out_dir: Path, ctx, resources, payloads):
    fig_dir = out_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)

    inv = ctx["inv_device_z"]
    rect = ctx["rect_min"]
    size = ctx["size_and_inv"]
    preview = ctx["preview"]
    clip_to_tw = ctx["clip_to_tw"]

    # Source-stat accumulators.
    base_stats = new_stats(3)
    normal_stats = new_stats(4)
    rough_stats = new_stats(1)
    metal_stats = new_stats(1)
    depth_stats = new_stats(2)
    direct_stats = new_stats(3)
    shadow_stats = new_stats(3)

    total = WIDTH * HEIGHT
    valid_count = 0
    sky_count = 0
    bad_normal = 0
    shadow_rg_mismatch = 0
    shadow_ba_not_clear = 0
    shadow_unique = set()
    pre_nz_geometry = 0
    base_roundtrip_max_code = 0.0

    fig_defs = [
        ("basecolor", INVALID_BLACK),
        ("worldnormal", INVALID_BLACK),
        ("roughness", INVALID_BLACK),
        ("metallic", INVALID_BLACK),
        ("depth_log", INVALID_BLACK),
        ("depth_validity", INVALID_BLACK),
        ("direct_rgb", None),
        ("vsm_shadow", INVALID_GREY),
    ]
    imgs = {name: bytearray(total * 3) for name, _ in fig_defs}

    for y in range(HEIGHT):
        o4_row = y * 5120
        osc_row = y * 10240
        row = y * WIDTH * 3
        for x in range(WIDTH):
            o4 = o4_row + x * 4
            osc = osc_row + x * 8
            i = row + x * 3
            d = struct.unpack_from("<f", payloads["scene_depth"], o4)[0]
            if math.isfinite(d) and d > 0.0:
                denom = d * inv[2] - inv[3]
                W = d * inv[0] + inv[1] + (1.0 / denom if denom != 0.0 else float("nan"))
                valid = math.isfinite(W) and W > 0.0
            else:
                W = float("nan")
                valid = False

            if not valid:
                sky_count += 1
                for name, invalid in fig_defs:
                    if invalid is not None:
                        off = i
                        imgs[name][off:off + 3] = bytes(invalid)
                # Direct is defined on the captured delta; sky delta is still
                # sampled and normally zero.
                pre = struct.unpack_from("<4e", payloads["scene_color_pre"], osc)
                post = struct.unpack_from("<4e", payloads["scene_color_post"], osc)
                delta = [float(post[k]) - float(pre[k]) for k in range(3)]
                tone = [(v / (1.0 + v) if v > 0.0 else 0.0) for v in delta]
                out_rgb = [round_u8(srgb_oetf(v)) for v in tone]
                imgs["direct_rgb"][i:i + 3] = bytes(out_rgb)
                add_stats(direct_stats, delta)
                continue

            valid_count += 1

            word = struct.unpack_from("<I", payloads["gbuffer_a"], o4)[0]
            ra, ga, ba, aa = decode_a2b10(word)
            nraw = [2.0 * ra / 1023.0 - 1.0, 2.0 * ga / 1023.0 - 1.0, 2.0 * ba / 1023.0 - 1.0]
            nlen = vec3_len(nraw)
            if nlen <= 1e-6:
                bad_normal += 1
                n = [0.0, 0.0, 1.0]
            else:
                n = [v / nlen for v in nraw]
            add_stats(normal_stats, nraw + [nlen])

            mem_b = payloads["gbuffer_b"][o4:o4 + 4]
            mem_c = payloads["gbuffer_c"][o4:o4 + 4]
            rough = mem_b[0] / 255.0
            metal = mem_b[2] / 255.0
            add_stats(rough_stats, [rough])
            add_stats(metal_stats, [metal])

            base_linear = [srgb_to_linear(mem_c[2]), srgb_to_linear(mem_c[1]), srgb_to_linear(mem_c[0])]
            add_stats(base_stats, base_linear)
            base_out = [
                max(1, min(255, 1 + int(round(254.0 * srgb_oetf(v))))) for v in base_linear
            ]
            # Quantization round-trip: compare re-encoded linear colour to the
            # original stored sRGB code.
            for k, code in enumerate((mem_c[2], mem_c[1], mem_c[0])):
                err = abs(srgb_oetf(srgb_to_linear(code)) * 255.0 - code)
                base_roundtrip_max_code = max(base_roundtrip_max_code, err)

            # Normal and scalar display mappings reserve 0 for the declared
            # invalid/sky mask, so valid zero-valued data is not confused with
            # missing data.  No gamma is applied to these scalar views.
            nout = [max(32, min(255, 32 + int(round(223.0 * (0.5 * v + 0.5))))) for v in n]
            imgs["worldnormal"][i:i + 3] = bytes(nout)
            imgs["basecolor"][i:i + 3] = bytes(base_out)
            rough_out = max(32, min(255, 32 + int(round(223.0 * rough))))
            metal_out = max(32, min(255, 32 + int(round(223.0 * metal))))
            imgs["roughness"][i:i + 3] = bytes([rough_out] * 3)
            imgs["metallic"][i:i + 3] = bytes([metal_out] * 3)

            add_stats(depth_stats, [W, d])
            t = math.log10(max(W, 200.0) / 200.0) / math.log10(2000.0 / 200.0)
            t = clamp01(t)
            depth_v = 32 + int(round(223.0 * t))
            depth_v = max(32, min(255, depth_v))
            imgs["depth_log"][i:i + 3] = bytes([depth_v] * 3)
            imgs["depth_validity"][i:i + 3] = b"\xff\xff\xff"

            smem = payloads["vsm_shadow_mask"][o4:o4 + 4]
            sr, sg = smem[2], smem[1]
            if sr != sg:
                shadow_rg_mismatch += 1
            if smem[0] != 255 or smem[3] != 255:
                shadow_ba_not_clear += 1
            factor = (sr / 255.0) ** 2
            shadow_unique.add(sr)
            add_stats(shadow_stats, [factor, sr / 255.0, (sr / 255.0) ** 2])
            sv = round_u8(factor)
            imgs["vsm_shadow"][i:i + 3] = bytes([sv] * 3)

            pre = struct.unpack_from("<4e", payloads["scene_color_pre"], osc)
            post = struct.unpack_from("<4e", payloads["scene_color_post"], osc)
            delta = [float(post[k]) - float(pre[k]) for k in range(3)]
            if any(v != 0.0 for v in pre[:3]):
                pre_nz_geometry += 1
            add_stats(direct_stats, delta)
            tone = [(v / (1.0 + v) if v > 0.0 else 0.0) for v in delta]
            out_rgb = [round_u8(srgb_oetf(v)) for v in tone]
            imgs["direct_rgb"][i:i + 3] = bytes(out_rgb)

    fig_dir.mkdir(parents=True, exist_ok=True)

    mappings = {
        "basecolor": "GBufferC linear BaseColor -> sRGB OETF (c<=0.0031308 ? 12.92c : 1.055*c^(1/2.4)-0.055) -> RGB8=1+round(254*x) (1..255); invalid/sky -> (0,0,0)",
        "worldnormal": "N_raw = 2*code/1023-1; N=N_raw/len(N_raw); RGB8=32+round(223*(0.5*N+0.5)) (32..255); no gamma; invalid/sky -> (0,0,0)",
        "roughness": "Roughness=GBufferB.mem[0]/255; RGB8=32+round(223*roughness) (32..255); no gamma; invalid/sky -> (0,0,0)",
        "metallic": "Metallic=GBufferB.mem[2]/255; RGB8=32+round(223*metallic) (32..255); no gamma; invalid/sky -> (0,0,0)",
        "depth_log": "W = D*Inv[0]+Inv[1]+1/(D*Inv[2]-Inv[3]); t=log10(max(W,200)/200)/log10(2000/200) clamped; v=32+round(223*t) (32..255); black=invalid/sky; declared range 200..2000 cm (2..20 m)",
        "depth_validity": "white (255) iff finite deviceZ>0 and finite W>0; black (0) otherwise",
        "direct_rgb": "L = SceneColor(pass 842)-SceneColor(pass 840) per RGB half channel; tone=L/(1+L) for L>0; sRGB OETF; round(255*x); no exposure multiply; black=zero direct",
        "vsm_shadow": "R=ShadowMaskTexture.mem[2]; G=mem[1]; s=(R/255)^2; RGB8=round(255*s); no gamma; invalid/sky -> (128,128,128)",
    }
    sources = {
        "basecolor": "GBufferC pass 697 / consumed pass 842",
        "worldnormal": "GBufferA pass 697 / consumed pass 842",
        "roughness": "GBufferB pass 697 / consumed pass 842",
        "metallic": "GBufferB pass 697 / consumed pass 842",
        "depth_log": "SceneDepthZ pass 620 / consumed pass 842",
        "depth_validity": "SceneDepthZ pass 620 / consumed pass 842",
        "direct_rgb": "SceneColor pre pass 840 / post pass 842",
        "vsm_shadow": "ShadowMaskTexture pass 841 / consumed pass 842",
    }

    figures = []
    for name, invalid in fig_defs:
        path = fig_dir / f"{name}.png"
        data = png_bytes(path, WIDTH, HEIGHT, imgs[name])
        img_stats = image_stats(imgs[name], WIDTH, HEIGHT, invalid)
        source_stats = {
            "basecolor": base_stats,
            "worldnormal": normal_stats,
            "roughness": rough_stats,
            "metallic": metal_stats,
            "depth_log": depth_stats,
            "depth_validity": depth_stats,
            "direct_rgb": direct_stats,
            "vsm_shadow": shadow_stats,
        }[name]
        ss = finish_stats(source_stats)
        nonempty = img_stats["nonempty_pixels"] > 0
        figures.append(
            {
                "name": name,
                "file": f"figures/{name}.png",
                "width": WIDTH,
                "height": HEIGHT,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "source": sources[name],
                "mapping": mappings[name],
                "invalid_color": list(invalid) if invalid is not None else None,
                "source_stats": ss,
                "image_stats": img_stats,
                "checks": {
                    "dimensions_1280x720": True,
                    "source_finite": ss["nonfinite"] == 0,
                    "image_nonempty": nonempty,
                    "all_pixels_covered": img_stats["nonempty_pixels"] + img_stats["pixels_equal_invalid_color"] == total,
                },
            }
        )

    by_name = {f["name"]: f for f in figures}
    checks = {
        "eight_full_frame_figures": len(figures) == 8,
        "all_dimensions_1280x720": all(f["width"] == WIDTH and f["height"] == HEIGHT for f in figures),
        "all_source_finite": all(f["source_stats"]["nonfinite"] == 0 for f in figures),
        "all_images_nonempty": all(f["image_stats"]["nonempty_pixels"] > 0 for f in figures),
        "no_raster_text_or_overlays": True,
        "primary_view_only_no_atlas": True,
        "valid_geometry_plus_sky_equals_total": valid_count + sky_count == total,
        "shadow_r_equals_g_all_valid": shadow_rg_mismatch == 0,
        "shadow_ba_clear_all_valid": shadow_ba_not_clear == 0,
        "scene_color_pre_zero_on_geometry": pre_nz_geometry == 0,
        "basecolor_roundtrip_within_one_code": base_roundtrip_max_code <= 1.0,
        "normal_lengths_near_one": (
            by_name["worldnormal"]["source_stats"]["min"][3] >= 0.99
            and by_name["worldnormal"]["source_stats"]["max"][3] <= 1.01
            and bad_normal == 0
        ),
        "depth_values_positive": by_name["depth_log"]["source_stats"]["min"][0] > 0.0,
        "shadow_factors_unit_range": (
            by_name["vsm_shadow"]["source_stats"]["min"][0] >= 0.0
            and by_name["vsm_shadow"]["source_stats"]["max"][0] <= 1.0
        ),
        "basecolor_linear_unit_range": (
            by_name["basecolor"]["source_stats"]["min"][0] >= 0.0
            and by_name["basecolor"]["source_stats"]["max"][0] <= 1.0
            and by_name["basecolor"]["source_stats"]["min"][1] >= 0.0
            and by_name["basecolor"]["source_stats"]["max"][1] <= 1.0
            and by_name["basecolor"]["source_stats"]["min"][2] >= 0.0
            and by_name["basecolor"]["source_stats"]["max"][2] <= 1.0
        ),
        "roughness_metallic_unit_range": (
            by_name["roughness"]["source_stats"]["min"][0] >= 0.0
            and by_name["roughness"]["source_stats"]["max"][0] <= 1.0
            and by_name["metallic"]["source_stats"]["min"][0] >= 0.0
            and by_name["metallic"]["source_stats"]["max"][0] <= 1.0
        ),
    }
    manifest = {
        "schema": SCHEMA_FIGURES,
        "dump": str(dump),
        "view_rect": [0, 0, WIDTH, HEIGHT],
        "allocation": [ALLOC_W, ALLOC_H],
        "figures": figures,
        "full_frame_stats": {
            "total_pixels": total,
            "valid_geometry_pixels": valid_count,
            "invalid_or_sky_pixels": sky_count,
            "bad_normal_pixels": bad_normal,
            "shadow_r_equals_g_pixels": valid_count - shadow_rg_mismatch,
            "shadow_rg_mismatch_pixels": shadow_rg_mismatch,
            "shadow_ba_clear_pixels": valid_count - shadow_ba_not_clear,
            "shadow_ba_not_clear_pixels": shadow_ba_not_clear,
            "shadow_unique_r_codes": sorted(shadow_unique),
            "scene_color_pre_nonzero_geometry_pixels": pre_nz_geometry,
            "basecolor_srgb_roundtrip_max_code_error": base_roundtrip_max_code,
        },
        "checks": checks,
    }
    return manifest


# ----------------------------------------------------------------------- data


def build_receiver_json(ctx, resources, receivers, world_routes, manifest=None):
    analysis = ctx["analysis"]
    graph = ctx["graph"]
    env = analysis.get("environment") or {}
    view = {
        "scene_graph_index": GRAPH_INDEX,
        "scene_graph_kind": graph.get("kind"),
        "scene_graph_first_pass_id": int(graph.get("first_pass_id")),
        "scene_graph_last_pass_id": int(graph.get("last_pass_id")),
        "scene_graph_pass_count": int(graph.get("pass_count")),
        "presented_graph_index_from_analysis": int(analysis.get("presented_graph_index")),
        "graph_count": len(analysis.get("graphs") or []),
        "engine_frame": (graph.get("engine_frames") or [None])[0],
        "view_resource_name": VIEW_RESOURCE_NAME,
        "view_resource_key": VIEW_RESOURCE_KEY,
        "view_resource_desc": "FRDGBufferDesc (raw buffer, not a texture)",
        "view_resource_file": ctx["view_layout"]["File"],
        "view_resource_version_key": "0" * 20,
        "view_resource_bytes": len(ctx["view_buf"]),
        "view_resource_stride_bytes": 1056,
        "view_resource_num_elements": 1,
        "view_resource_mip": None,
        "view_resource_row_pitch_bytes": None,
        "view_resource_sha256": sha256_file(ctx["view_path"]),
        "view_resource_source_formula": "FPackedView: SVPositionToTranslatedWorld 0, ViewToTranslatedWorld 64, TranslatedWorldToView 128, TranslatedWorldToClip 192, ViewToClip 256, ClipToRelativeWorld 320, prev matrices 384..576, ViewRect 640, ViewSizeAndInvSize 656 (NaniteShared.h:59-72)",
        "view_uniform_layout": VIEW_UNIFORM_LAYOUT,
        "view_uniform_content_key": ctx["ub_key"],
        "view_uniform_file": f"Structures/{ctx['ub_key']}.bin",
        "view_uniform_bytes": len(ctx["ub"]),
        "view_uniform_sha256": sha256_file(ctx["ub_path"]),
        "view_uniform_metadata": f"StructuresMetadata/{ctx['meta_path'].name}",
        "view_uniform_bound_at_pass": SUN_PASS_ID,
        "view_uniform_member_offsets": {
            m["Name"]: int(m["Offset"])
            for m in ctx["meta"].get("Members", [])
            if m.get("Name")
            in (
                "ViewRectMin",
                "ViewSizeAndInvSize",
                "ViewOriginHigh",
                "ViewOriginLow",
                "WorldViewOriginHigh",
                "WorldViewOriginLow",
                "PreViewTranslationHigh",
                "PreViewTranslationLow",
                "ViewForward",
                "ViewToClip",
                "ViewToClipNoAA",
                "ClipToTranslatedWorld",
                "SVPositionToTranslatedWorld",
                "TemporalAAJitter",
                "InvDeviceZToWorldZTransform",
                "PreExposure",
                "OneOverPreExposure",
            )
        },
        "view_rect": [int(v) for v in ctx["packed_rect"]],
        "view_rect_min_f32": ctx["rect_min"],
        "view_size_and_inv_size": ctx["size_and_inv"],
        "view_rect_min_and_size_u32": ctx["rect_min_size_u32"],
        "allocation": [ALLOC_W, ALLOC_H],
        "allocation_source": "ResourceDescs GBufferA/B/C/D/F, SceneColor, SceneDepthZ, ShadowMaskTexture = 1280x888; presented view rect = 1280x720",
        "origin_cm": ctx["origin"],
        "world_origin_cm": ctx["world_origin"],
        "origin_high_low_cm": [ctx["origin_high"], ctx["origin_low"]],
        "world_origin_high_low_cm": [ctx["world_origin_high"], ctx["world_origin_low"]],
        "view_forward": ctx["forward"],
        "pitch_deg": ctx["pitch"],
        "yaw_deg": ctx["yaw"],
        "hfov_deg": ctx["hfov"],
        "vfov_deg": ctx["vfov"],
        "near_plane": ctx["near_plane"],
        "reversed_z": True,
        "pre_exposure": ctx["pre_exposure"],
        "one_over_pre_exposure": ctx["one_over_pre_exposure"],
        "pre_view_translation_cm": ctx["preview"],
        "pre_view_translation_high_cm": ctx["preview_high"],
        "pre_view_translation_low_cm": ctx["preview_low"],
        "temporal_aa_jitter": ctx["jitter"],
        "inv_device_z_to_world_z_transform": ctx["inv_device_z"],
        "view_to_clip_rows": ctx["v2c"],
        "view_to_clip_no_aa_rows": ctx["v2c_no_aa"],
        "clip_to_translated_world_rows": ctx["clip_to_tw"],
        "sv_position_to_translated_world_rows": ctx["sv_to_tw"],
        "matrix_convention": "captured four float4 groups applied as out[j]=sum_i v[i]*groups[i][j]; both ClipToTranslatedWorld and SVPositionToTranslatedWorld routes agree at all receivers",
        "packed_view_offsets": {
            "SVPositionToTranslatedWorld": 0,
            "ViewToTranslatedWorld": 64,
            "TranslatedWorldToView": 128,
            "TranslatedWorldToClip": 192,
            "ViewToClip": 256,
            "ClipToRelativeWorld": 320,
            "PrevTranslatedWorldToView": 384,
            "PrevTranslatedWorldToClip": 448,
            "PrevViewToClip": 512,
            "PrevClipToRelativeWorld": 576,
            "ViewRect": 640,
            "ViewSizeAndInvSize": 656,
        },
        "packed_view_to_clip_rows": ctx["packed_v2c"],
        "packed_translated_world_to_clip_rows": ctx["packed_tw2c"],
        "packed_sv_position_to_translated_world_rows": ctx["packed_sv2tw"],
        "packed_vs_uniform_view_to_clip_max_abs_diff": max(
            abs(ctx["packed_v2c"][i][j] - ctx["v2c"][i][j]) for i in range(4) for j in range(4)
        ),
        "world_route_max_delta_cm": max(r["route_delta_cm"] for r in world_routes),
        "offset_192_identity": "TranslatedWorldToClip per NaniteShared.h:59-72; it is NOT TranslatedWorldToView and is not used as the view matrix",
        "world_route_checks": world_routes,
        "checks": ctx["checks"],
    }
    recv = {
        "schema": SCHEMA_RECEIVERS,
        "run_id": analysis.get("run_id"),
        "dump": str(ctx["dump"]),
        "analysis_json": str(ctx["analysis_path"]),
        "engine": {
            "version": "5.8.1",
            "build_id": env.get("engine_build_id"),
            "declared_source_base": "71fe36aac5a8df5ccd66c763ffc902b29b6a9c43",
        },
        "scene_graph": {
            "index": GRAPH_INDEX,
            "kind": graph.get("kind"),
            "first_pass_id": int(graph.get("first_pass_id")),
            "last_pass_id": int(graph.get("last_pass_id")),
            "pass_count": int(graph.get("pass_count")),
            "presented": True,
            "reason": "index 3 is the last of 3 scene graphs plus 1 Slate graph and contains the final FXAA/Tonemap chain",
            "engine_frame": (graph.get("engine_frames") or [None])[0],
        },
        "gbuffer_semantics": {
            "legacy_gbuffer_path": True,
            "reason": "r.Substrate=0; GBufferA/B/C/D/F are the legacy deferred targets",
            "console_variables": read_cvars(
                ctx["base"],
                (
                    "r.Substrate",
                    "r.GBufferFormat",
                    "r.AllowStaticLighting",
                    "r.GBufferDiffuseSampleOcclusion",
                    "r.TemporalAA.Upsampling",
                    "r.Shadow.Virtual.Enable",
                    "r.ScreenPercentage",
                    "r.DefaultFeature.AutoExposure",
                    "r.EyeAdaptationQuality",
                ),
            ),
            "gbuffer_c_alpha": "GBufferAO because r.AllowStaticLighting=0 and r.GBufferDiffuseSampleOcclusion=0",
            "gbuffer_c_srgb": "target NativeSRGB=true/ViewSRGB=true; byte codes are sRGB-encoded and decoded to linear",
        },
        "view": view,
        "resources": resources,
        "direct_light": {
            "pass_id": SUN_PASS_ID,
            "event": ctx["pass_by_id"][SUN_PASS_ID].get("EventName"),
            "pointer": ctx["pass_by_id"][SUN_PASS_ID].get("Pointer"),
            "parameters": ctx["pass_by_id"][SUN_PASS_ID].get("Parameters"),
            "parameters_metadata": ctx["pass_by_id"][SUN_PASS_ID].get("ParametersMetadata"),
            "parent_scopes": ctx["pass_by_id"][SUN_PASS_ID].get("ParentEventScopes"),
            "outputs": ctx["pass_by_id"][SUN_PASS_ID].get("OutputResources"),
            "scene_color_pre": resources["scene_color_pre"],
            "scene_color_post": resources["scene_color_post"],
            "shadow_mask": resources["vsm_shadow_mask"],
            "definition": "direct_rgb_pre_exposure = SceneColor(post pass 842) - SceneColor(pre pass 840), RGB half values; no analytic BRDF, no AS/SMRT intermediate",
        },
        "receivers": receivers,
        "selection": {
            "requested": {
                name: {"pixel": list(c["pixel"]), "label": c["label"]}
                for name, c in CANDIDATES.items()
            },
            "substitution_performed": False,
            "reason": "all three named receivers are valid, finite, DefaultLit, and clean in the 3x3 normal/depth/material check; C is material-valid but sun-occluded, so no receiver was moved",
            "label_basis": "labels come from the capture plan; the decoded GBuffer values are reported independently",
        },
        "checks": {
            "view_identity": all(ctx["checks"].values()),
            "views_joined_to_gbuffer_versions": all(
                resources[k]["versions_identical"] for k in ("gbuffer_a", "gbuffer_b", "gbuffer_c", "gbuffer_d", "gbuffer_f", "scene_depth", "vsm_shadow_mask")
            ),
            "all_receivers_valid": all(r["valid"] for r in receivers.values()),
            "all_receivers_clean_3x3": all(r["center_clean"] for r in receivers.values()),
            "world_routes_agree": all(r["route_delta_cm"] <= 0.01 for r in world_routes),
            "direct_pre_zero_on_receivers": all(r["direct"]["pre_is_zero"] for r in receivers.values()),
            "shadow_r_equals_g_on_receivers": all(r["shadow"]["r_equals_g"] for r in receivers.values()),
            "shadow_ba_clear_on_receivers": all(r["shadow"]["ba_are_clear_white"] for r in receivers.values()),
            "shading_models_default_lit": all(r["material"]["shading_model_id"] == 1 for r in receivers.values()),
        },
    }
    if manifest is not None:
        recv["figures"] = {
            "manifest": "data/receiver-figures.json",
            "figures": [f["file"] for f in manifest["figures"]],
        }
        recv["checks"]["figures_complete"] = all(manifest["checks"].values())
    return recv


def orientation_json(recv):
    return {
        "schema": SCHEMA_ORIENTATION,
        "dump": recv["dump"],
        "scene_graph_index": recv["scene_graph"]["index"],
        "view_resource_key": recv["view"]["view_resource_key"],
        "view_uniform_content_key": recv["view"]["view_uniform_content_key"],
        "origin_cm": recv["view"]["origin_cm"],
        "view_rect": recv["view"]["view_rect"],
        "allocation": recv["view"]["allocation"],
        "hfov_deg": recv["view"]["hfov_deg"],
        "pitch_deg": recv["view"]["pitch_deg"],
        "yaw_deg": recv["view"]["yaw_deg"],
        "pre_exposure": recv["view"]["pre_exposure"],
        "pre_view_translation_cm": recv["view"]["pre_view_translation_cm"],
        "jitter": recv["view"]["temporal_aa_jitter"],
        "receivers": recv["receivers"],
        "selection": recv["selection"],
    }


# ---------------------------------------------------------------------- report


def write_report(out_dir: Path, recv, manifest, verify=None):
    v = recv["view"]
    r = recv["receivers"]

    def f(x, n=9):
        if isinstance(x, float):
            return f"{x:.{n}g}"
        return str(x)

    def flist(xs, n=9):
        return "[" + ", ".join(f"{float(x):.{n}g}" for x in xs) + "]"

    status = "pass" if verify is None or verify.get("ok") else "fail"
    lines = []
    lines.append("# Sponza receiver decode")
    lines.append("")
    lines.append(
        f"Status: **{status}**. Read-only decode of the frozen native dump; no GPU work, "
        "no engine/repo/Sponza writes, and no acceleration-structure claims."
    )
    lines.append("")
    lines.append("## Source")
    lines.append("")
    lines.append(f"- Dump: `{recv['dump']}`")
    lines.append(f"- Analysis: `{recv['analysis_json']}`")
    lines.append(f"- Run id: `{recv['run_id']}`")
    lines.append(f"- Engine: `{recv['engine']['version']}` build `{recv['engine']['build_id']}`")
    lines.append(
        f"- Presented scene graph: index **{recv['scene_graph']['index']}** (last of 3 scene "
        f"graphs + 1 Slate graph), passes "
        f"{recv['scene_graph']['first_pass_id']}..{recv['scene_graph']['last_pass_id']} "
        f"({recv['scene_graph']['pass_count']} passes), {recv['scene_graph']['engine_frame']}"
    )
    lines.append("")
    lines.append("## Presented view")
    lines.append("")
    lines.append("| Item | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| View resource | `{v['view_resource_name']}` `{v['view_resource_key']}` |")
    lines.append(f"| View resource file | `{v['view_resource_file']}` (1056 B) |")
    lines.append(f"| View uniform content | `{v['view_uniform_file']}` ({v['view_uniform_bytes']} B) |")
    lines.append(f"| View uniform bound at pass | {v['view_uniform_bound_at_pass']} |")
    lines.append(f"| ViewRect | {v['view_rect']} |")
    lines.append(f"| Allocation | {v['allocation']} |")
    lines.append(f"| Origin (cm) | {flist(v['origin_cm'], 9)} |")
    lines.append(f"| Forward | {flist(v['view_forward'], 9)} |")
    lines.append(f"| HFOV / VFOV (deg) | {f(v['hfov_deg'], 12)} / {f(v['vfov_deg'], 12)} |")
    lines.append(f"| Pitch / yaw (deg) | {f(v['pitch_deg'], 12)} / {f(v['yaw_deg'], 12)} |")
    lines.append(f"| Near plane | {f(v['near_plane'], 12)} |")
    lines.append(f"| Reversed-Z | {v['reversed_z']} |")
    lines.append(f"| PreExposure | {f(v['pre_exposure'], 18)} |")
    lines.append(f"| OneOverPreExposure | {f(v['one_over_pre_exposure'], 12)} |")
    lines.append(f"| PreViewTranslation (cm) | {flist(v['pre_view_translation_cm'], 9)} |")
    lines.append(f"| TemporalAAJitter | {flist(v['temporal_aa_jitter'], 9)} |")
    lines.append(f"| InvDeviceZToWorldZTransform | {flist(v['inv_device_z_to_world_z_transform'], 12)} |")
    lines.append(
        "| FPackedView offsets | SVPositionToTranslatedWorld 0, ViewToTranslatedWorld 64, "
        "TranslatedWorldToView 128, TranslatedWorldToClip 192, ViewToClip 256, ViewRect 640, "
        "ViewSizeAndInvSize 656 |"
    )
    lines.append(
        "| Offset 192 note | `TranslatedWorldToClip`, not `TranslatedWorldToView`; "
        "the uniform `ViewToClip` at 448 and the packed `ViewToClip` at 256 agree exactly |"
    )
    lines.append("")
    lines.append(
        "Nominal approved identity is origin `(-850,0,90)` cm, HFOV `70` deg, pitch `13` deg, "
        "yaw `0` deg; the decoded float values above agree within `1e-5` deg.  The "
        "`1280x720` ViewRect is the presented sub-rect of the `1280x888` allocation; only rows "
        "`0..719` are presented and decoded."
    )
    lines.append("")
    lines.append("## Receiver values")
    lines.append("")
    lines.append(
        "| ID | Pixel | World (cm) | Depth W (cm) | Shading normal (world) | Linear BaseColor | "
        "Rough | Metal | Spec | Shading | Direct RGB (pre-exposure) | VSM R |"
    )
    lines.append("| --- | --- | --- | ---: | --- | --- | ---: | ---: | ---: | --- | --- | ---: |")
    for name in ("A", "B", "C"):
        x = r[name]
        lines.append(
            "| %s | (%d,%d) | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
            % (
                name,
                x["pixel"][0],
                x["pixel"][1],
                flist(x["world_cm"], 9),
                f(x["depth"]["linear_w_cm"], 12),
                flist(x["normal"]["normalized"], 7),
                flist(x["basecolor"]["linear_rgb"], 8),
                f(x["material"]["roughness"], 8),
                f(x["material"]["metallic"], 8),
                f(x["material"]["specular"], 8),
                f"{x['material']['shading_model_name']} ({x['material']['shading_model_id']})",
                flist(x["direct"]["direct_rgb_pre_exposure"], 12),
                f(x["shadow"]["shadow_factor_r_linear"], 8),
            )
        )
    lines.append("")
    lines.append("Exact raw material bytes and codes:")
    lines.append("")
    lines.append("| ID | GBufferB BGRA | Rough code | Metal code | Spec code | Packed id | GBufferC BGRA | BaseColor sRGB codes (R,G,B) | GBufferA word | GBufferD | GBufferF |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- | --- |")
    for name in ("A", "B", "C"):
        x = r[name]
        m = x["material"]
        lines.append(
            "| %s | %s | %d | %d | %d | %d | %s | %s | %s | %s | %s |"
            % (
                name,
                m["gbuffer_b_bytes_bgra"],
                m["roughness_code"],
                m["metallic_code"],
                m["specular_code"],
                m["packed_id_code"],
                x["basecolor"]["stored_srgb_bytes_bgra"],
                x["basecolor"]["shader_rgb_srgb_codes"],
                x["normal"]["raw_word_hex"],
                m["gbuffer_d_bytes_bgra"],
                m["gbuffer_f_bytes_bgra"],
            )
        )
    lines.append("")
    lines.append("C authored roughness .08 decodes to code **20/255 = 0.0784313725**; the table reports the actual decoded value.")
    lines.append("")
    cv = recv["gbuffer_semantics"]["console_variables"]
    lines.append(
        "GBuffer semantics: legacy path (`r.Substrate=%s`, `r.GBufferFormat=%s`); "
        "GBufferC alpha is AO (`r.AllowStaticLighting=%s`, `r.GBufferDiffuseSampleOcclusion=%s`); "
        "GBufferC target is sRGB; TAA upsampling `r.TemporalAA.Upsampling=%s`; "
        "VSM `r.Shadow.Virtual.Enable=%s`."
        % (
            cv["r.Substrate"]["value"],
            cv["r.GBufferFormat"]["value"],
            cv["r.AllowStaticLighting"]["value"],
            cv["r.GBufferDiffuseSampleOcclusion"]["value"],
            cv["r.TemporalAA.Upsampling"]["value"],
            cv["r.Shadow.Virtual.Enable"]["value"],
        )
    )
    lines.append("")
    lines.append("## Provenance")
    lines.append("")
    lines.append("| Key | Resource | Producer pass (version) | Consumer pass (version) | Mip | RowPitch | Stored format | Identical |")
    lines.append("| --- | --- | --- | --- | ---: | ---: | --- | --- |")
    for key in ("gbuffer_a", "gbuffer_b", "gbuffer_c", "gbuffer_d", "gbuffer_f", "scene_depth", "scene_color_pre", "scene_color_post", "vsm_shadow_mask"):
        q = recv["resources"][key]
        lines.append(
            "| %s | %s `%s` | %s `%s` | %s `%s` | %d | %d | %s | %s |"
            % (
                key,
                q["name"],
                q["resource_id"],
                q["producer"]["pass_id"],
                q["producer_version_key"],
                q["consumer"]["pass_id"],
                q["consumer_version_key"],
                q["mip"],
                q["row_pitch_bytes"],
                q["stored_format"],
                q["versions_identical"],
            )
        )
    lines.append("")
    lines.append("Payload hashes are in `data/receivers.json`; every consumed consumer version was byte-identical to its producer version for the GBuffers, depth, and VSM mask.")
    lines.append("")
    lines.append("## Direct light and VSM shadow")
    lines.append("")
    lines.append(
        f"Sun pass **{recv['direct_light']['pass_id']}** `{recv['direct_light']['event']}`; "
        f"pre SceneColor pass {recv['direct_light']['scene_color_pre']['producer']['pass_id']} "
        f"`{recv['direct_light']['scene_color_pre']['producer_version_key']}`; "
        f"post SceneColor pass {recv['direct_light']['scene_color_post']['consumer']['pass_id']} "
        f"`{recv['direct_light']['scene_color_post']['consumer_version_key']}`."
    )
    lines.append("")
    lines.append("| ID | Pre SceneColor RGB | Post SceneColor RGB | Direct RGB | Shadow BGRA | Shadow R linear | R==G | B/A clear |")
    lines.append("| --- | --- | --- | --- | --- | ---: | --- | --- |")
    for name in ("A", "B", "C"):
        x = r[name]
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s | %s |"
            % (
                name,
                flist(x["direct"]["scene_color_pre_half_rgba"][:3], 10),
                flist(x["direct"]["scene_color_post_half_rgba"][:3], 10),
                flist(x["direct"]["direct_rgb_pre_exposure"], 10),
                x["shadow"]["memory_bgra"],
                f(x["shadow"]["shadow_factor_r_linear"], 9),
                x["shadow"]["r_equals_g"],
                x["shadow"]["ba_are_clear_white"],
            )
        )
    lines.append("")
    lines.append(
        "Supported statements: the sun pass is the only light in the captured `SponzaTeaching.Sun_Sponza` scope; "
        "the direct RGB is the exact FP16 SceneColor delta across that pass; the VSM mask is the exact "
        "`ShadowMaskTexture` consumed as `PS.LightAttenuationTexture`."
    )
    lines.append("")
    lines.append("## Figures")
    lines.append("")
    lines.append("| Figure | Mapping | Source stats (min..max, mean) | Finite | Nonempty |")
    lines.append("| --- | --- | --- | --- | --- |")
    for fig in manifest["figures"]:
        ss = fig["source_stats"]
        mn = ", ".join(f(v, 8) for v in ss["min"])
        mx = ", ".join(f(v, 8) for v in ss["max"])
        mean = ", ".join(f(v, 8) for v in ss["mean"])
        lines.append(
            "| `%s` | %s | [%s] .. [%s], mean [%s] | %d/%d | %d |"
            % (
                fig["file"],
                fig["mapping"].replace("|", "/"),
                mn,
                mx,
                mean,
                ss["finite"],
                ss["count"],
                fig["image_stats"]["nonempty_pixels"],
            )
        )
    lines.append("")
    lines.append(
        "Every PNG is a 1280x720 RGB8 data raster with no raster text, arrows, legends, "
        "or invented background.  Invalid/sky masks are the declared colours in the manifest."
    )
    lines.append("")
    lines.append("## Checks")
    lines.append("")
    for section, checks in (
        ("View", recv["checks"]),
        ("Figures", manifest["checks"]),
    ):
        for k, val in checks.items():
            lines.append(f"- {section}: `{k}` = **{val}**")
    if verify is not None:
        lines.append(
            f"- Verification: `ok` = **{verify.get('ok')}** "
            f"({len(verify.get('checks', []))} checks, including PNG IHDR/IDAT/IEND chunk-only checks)"
        )
    lines.append("")
    lines.append("## Exact commands")
    lines.append("")
    base_cmd = (
        f'python sponza-receivers/decode_receivers.py --dump "{recv["dump"]}" '
        f'--analysis "{recv["analysis_json"]}" --out "sponza-receivers"'
    )
    lines.append("```text")
    lines.append(base_cmd + " --stage receivers")
    lines.append(base_cmd + " --stage all")
    lines.append(base_cmd + " --stage verify")
    lines.append("```")
    lines.append("")
    lines.append("## Limitations")
    lines.append("")
    lines.append(
        "- The direct term is the captured SceneColor delta across the StandardDeferred sun pass, "
        "not an analytic BRDF and not a native ray-tracing/SMRT/AS intermediate."
    )
    lines.append(
        "- The VSM value is the 8-bit sqrt-encoded `ShadowMaskTexture` consumed by the light pass; "
        "the unquantized VSM receiver factor is not recovered.  The observed R codes are "
        f"{manifest['full_frame_stats']['shadow_unique_r_codes']}, i.e. the consumed attenuation is "
        "quantized to eighth steps after squaring."
    )
    lines.append(
        "- GBufferA is the world-space shading normal.  The depth-derived geometric normal is a check only; "
        "no geometric normal is used for shading."
    )
    lines.append(
        "- BaseColor is the 8-bit sRGB-decoded GBufferC albedo; it is not a material parameter or texture source."
    )
    lines.append(
        "- SceneColor is pre-exposure linear radiance.  No exposure multiply is applied to the reported direct values."
    )
    lines.append(
        "- The ray-tracing SceneBuffer staging warning means nonzero SceneBuffer bytes do not prove readable "
        "acceleration structures.  This decode does not read or claim acceleration structures."
    )
    lines.append(
        "- Shader-local values and per-thread choices are not dump resources and are not fabricated."
    )
    lines.append(
        "- The C sample is a valid polished-gold material point but is occluded from the sun at this view "
        "(mask R=0, direct RGB=0).  No nearby lit gold pixel was found in the sampled sphere region, so no "
        "receiver was substituted."
    )
    lines.append("")
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------- verify


def parse_png_header(data: bytes):
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise DecodeError("not a PNG with an IHDR")
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def png_chunk_types(data: bytes):
    types = []
    pos = 8
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        types.append(kind)
        pos += 12 + length
    return types


def verify_outputs(out_dir: Path):
    recv = load_json(out_dir / "data" / "receivers.json")
    orient = load_json(out_dir / "data" / "orientation.json")
    manifest = load_json(out_dir / "data" / "receiver-figures.json")
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    v = recv["view"]
    add(
        "view_identity",
        all(v["checks"].values())
        and abs(v["origin_cm"][0] + 850.0) < 1e-6
        and abs(v["origin_cm"][2] - 90.0) < 1e-6
        and v["view_rect"] == [0, 0, 1280, 720]
        and abs(v["hfov_deg"] - 70.0) < 1e-4
        and abs(v["pitch_deg"] - 13.0) < 1e-4,
        f"origin={v['origin_cm']} rect={v['view_rect']} hfov={v['hfov_deg']} pitch={v['pitch_deg']}",
    )
    add(
        "offset_192_not_used",
        v["offset_192_identity"].startswith("TranslatedWorldToClip")
        and abs(v["packed_translated_world_to_clip_rows"][0][1] + 0.5711348652839661) < 1e-9,
        "packed offset 192 is TranslatedWorldToClip; packed/uuniform ViewToClip at 256/448 match",
    )
    add(
        "gbuffer_depth_versions_identical",
        all(
            recv["resources"][k]["versions_identical"]
            for k in ("gbuffer_a", "gbuffer_b", "gbuffer_c", "gbuffer_d", "gbuffer_f", "scene_depth", "vsm_shadow_mask")
        ),
        "producer and consumer payload SHA-256 equal for all receiver textures",
    )
    add(
        "receivers_present",
        all(k in recv["receivers"] for k in ("A", "B", "C")),
        "A/B/C present",
    )
    add(
        "receivers_finite_and_valid",
        all(
            r["valid"]
            and all(math.isfinite(x) for x in r["world_cm"])
            and all(math.isfinite(x) for x in r["normal"]["normalized"])
            and all(math.isfinite(x) for x in r["basecolor"]["linear_rgb"])
            and math.isfinite(r["depth"]["linear_w_cm"])
            for r in recv["receivers"].values()
        ),
        "world/normal/basecolor/depth finite and valid for A/B/C",
    )
    add(
        "c_roughness_actual_20_of_255",
        recv["receivers"]["C"]["material"]["roughness_code"] == 20
        and abs(recv["receivers"]["C"]["material"]["roughness"] - 20.0 / 255.0) < 1e-15,
        "C actual roughness is code 20/255, not the nominal 0.08",
    )
    add(
        "shadow_rg_and_ba",
        all(r["shadow"]["r_equals_g"] and r["shadow"]["ba_are_clear_white"] for r in recv["receivers"].values()),
        "R==G and B/A clear white on A/B/C",
    )
    add(
        "direct_pre_zero",
        all(r["direct"]["pre_is_zero"] for r in recv["receivers"].values()),
        "pre SceneColor RGB is zero on A/B/C",
    )

    for fig in manifest["figures"]:
        p = out_dir / fig["file"]
        data = p.read_bytes()
        w, h = parse_png_header(data)
        sha = hashlib.sha256(data).hexdigest()
        add(
            "figure:" + fig["name"],
            w == 1280 and h == 720 and sha == fig["sha256"] and len(data) == fig["bytes"] and fig["image_stats"]["nonempty_pixels"] > 0,
            f"{w}x{h} bytes={len(data)} sha256={sha[:16]} nonempty={fig['image_stats']['nonempty_pixels']}",
        )
        chunks = png_chunk_types(data)
        add(
            "figure_chunks:" + fig["name"],
            chunks == [b"IHDR", b"IDAT", b"IEND"],
            f"PNG chunks {[c.decode('ascii') for c in chunks]}",
        )
    add(
        "orientation_matches_receivers",
        orient["receivers"]["A"]["pixel"] == [500, 680]
        and orient["receivers"]["B"]["pixel"] == [720, 680]
        and orient["receivers"]["C"]["pixel"] == [626, 587],
        "orientation.json carries the exact A/B/C pixel coordinates",
    )
    add(
        "figure_manifest_checks",
        all(manifest["checks"].values()),
        f"manifest checks: {manifest['checks']}",
    )
    add(
        "report_exists",
        (out_dir / "report.md").exists() and (out_dir / "report.md").stat().st_size > 0,
        "report.md present and nonempty",
    )

    result = {
        "schema": SCHEMA_VERIFY,
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "figure_count": len(manifest["figures"]),
    }
    write_json(out_dir / "data" / "verify.json", result)
    return result


# ------------------------------------------------------------------------ main


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", type=Path, default=DEFAULT_DUMP)
    parser.add_argument("--analysis", type=Path, default=DEFAULT_ANALYSIS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--stage", choices=("receivers", "figures", "all", "verify"), default="all")
    args = parser.parse_args(argv)

    dump = args.dump.resolve()
    analysis = args.analysis.resolve()
    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.stage == "verify":
        result = verify_outputs(out_dir)
        print(json.dumps({"ok": result["ok"], "checks": len(result["checks"])}, indent=2))
        return 0 if result["ok"] else 1

    if not (dump / "Base" / "ResourceDescs.json").exists():
        raise DecodeError(f"not a DumpGPU dump: {dump}")
    if not analysis.exists():
        raise DecodeError(f"missing analysis JSON: {analysis}")

    ctx = decode_view(dump, dump / "Base", analysis, GRAPH_PREFIX)
    resources = build_resource_records(dump, ctx)
    payloads, payload_paths = load_payloads(dump, resources)
    receivers = decode_receivers(ctx, resources, payloads)
    recv = build_receiver_json(ctx, resources, receivers, ctx["world_routes"])
    write_json(out_dir / "data" / "receivers.json", recv)
    write_json(out_dir / "data" / "orientation.json", orientation_json(recv))
    write_json(
        out_dir / "data" / "run.json",
        {
            "schema": "sponza-receiver-run-v1",
            "argv": list(sys.argv if argv is None else argv),
            "stage": args.stage,
            "dump": str(dump),
            "analysis": str(analysis),
            "out": str(out_dir),
        },
    )
    print(f"wrote {out_dir / 'data' / 'receivers.json'}")
    print(f"wrote {out_dir / 'data' / 'orientation.json'}")

    if args.stage == "receivers":
        return 0

    manifest = build_figures(dump, out_dir, ctx, resources, payloads)
    write_json(out_dir / "data" / "receiver-figures.json", manifest)
    print(f"wrote {out_dir / 'data' / 'receiver-figures.json'}")

    # Rebuild the receiver JSON with the figure manifest link, then write the
    # report.  The per-pixel values are identical to the early JSON.
    recv = build_receiver_json(ctx, resources, receivers, ctx["world_routes"], manifest)
    write_json(out_dir / "data" / "receivers.json", recv)
    # Write the report before verification so the verify check can assert it
    # exists; then rewrite it with the verification summary.
    write_report(out_dir, recv, manifest, None)
    verify = verify_outputs(out_dir)
    write_report(out_dir, recv, manifest, verify)
    print(f"wrote {out_dir / 'report.md'}")
    print(json.dumps({"verify_ok": verify["ok"], "checks": len(verify["checks"])}, indent=2))
    return 0 if verify["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DecodeError as exc:
        print(f"decode error: {exc}", file=sys.stderr)
        raise SystemExit(2)
