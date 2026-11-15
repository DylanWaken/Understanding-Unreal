"""Replay one World Radiance Cache lookup from the Sponza hero capture.

The query is reconstructed from a captured compacted screen-probe trace lane:
screen-probe depth, ray info and blue-noise jitter give the translated-world
position and direction; the captured radiance-cache clipmaps, indirection
volume, probe offsets and atlases give the eight-neighbor lookup.  The lookup's
interpolated distance is compared with the captured ``TraceHit`` word written by
the same pass.  This is a declared query replay over captured state, not a
record of a shader-local register value, and it does not claim that a dispatched
ray chose this query.

No GPU work is performed.
"""
from pathlib import Path
from collections import Counter
import argparse
import hashlib
import json
import math
import struct
import sys

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dump_reader
import world_cache_math as cache

F = np.float32
DEFAULT_ROOT = r"D:/UnrealUnderstanding-Sponza/output/gpu-dump/raw/sponza-20261115-083654"
# Presented scene graph of the captured frame: pass pointers prefixed 0004.
WORLD_PASS_ID = 747


class Reject(Exception):
    pass


def sha_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def parse_cvars(path):
    rows = {}
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        parts = line.split(",")
        if len(parts) >= 4:
            rows[parts[0]] = {"type": parts[1], "source": parts[2], "value": parts[3]}
    return rows


def read_source_evidence(engine_root, rel, lines, label):
    path = Path(engine_root) / rel
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    pick = []
    for line in lines:
        if 1 <= line <= len(text):
            pick.append({"line": line, "text": text[line - 1]})
    return {"label": label, "file": rel, "sha256": sha_file(path), "lines": pick}


def validate_references(d):
    rows = []
    for name, value in d.references.items():
        row = {"name": name, "null": bool(value.get("Null")),
               "contentAvailable": value.get("ContentAvailable"),
               "byteSize": value.get("ByteSize"), "contentKind": value.get("ContentKind")}
        if row["null"]:
            row["status"] = "explicit-unbound-gap"
            rows.append(row)
            continue
        ev = d.reference_evidence[name]
        if ev["size"] != int(value["ByteSize"]):
            raise ValueError(f"{name}: payload {ev['size']} != declared {value['ByteSize']}")
        row.update({"status": "validated", "structTypeName": ev["structTypeName"],
                    "payload": ev["contentFile"]["path"], "metadata": ev["metadataFile"]["path"]})
        rows.append(row)
    return rows


def decode_blue_noise(d):
    binding = d.native_binding("ScreenProbeParameters.BlueNoise.BlueNoise.Vec2Texture")["binding"]
    if binding.get("Null"):
        raise ValueError("BlueNoise Vec2Texture is null")
    if binding.get("MemberOffset") != 40:
        raise ValueError("Vec2Texture uniform offset is not 40")
    field = d.field("ScreenProbeParameters.BlueNoise.BlueNoise.Vec2Texture")
    if binding["Pointer"][-16:] != field["pointer"][-16:]:
        raise ValueError("binding pointer does not match the uniform byte-40 pointer")
    rel = binding["ExportFirstFile"]
    layout = d.layouts_by_file[rel]
    if layout["NativeFormat"] != "PF_B8G8R8A8" or layout["StoredFormat"] != "PF_B8G8R8A8":
        raise ValueError("BlueNoise is not stored B8G8R8A8")
    if layout.get("ViewSRGB") or layout.get("NativeSRGB"):
        raise ValueError("BlueNoise is sRGB; a linear replay is required")
    array, meta = d.read_texture_file(rel, "BlueNoise.Vec2")
    if array.shape != (8192, 128, 4):
        raise ValueError(f"BlueNoise shape {array.shape}")
    if array[..., 0].max() != 0 or array[..., 3].min() != 255:
        raise ValueError("BlueNoise BGRA packing changed")
    return array, {"binding": binding, "exportFile": rel, "layout": layout,
                   "payload": dump_reader.file_evidence(d.root / rel, d.root),
                   "decode": "memory B,G,R,A; shader R,G = bytes 2,1; jitter = byte/255",
                   "dimensions": d.field("ScreenProbeParameters.BlueNoise.BlueNoise.Dimensions"),
                   "moduloMasks": d.field("ScreenProbeParameters.BlueNoise.BlueNoise.ModuloMasks")}


def tile_jitter(index, downsample):
    e1 = F((F(index) / F(8)) % F(1))
    e2 = F(int(f"{int(index) & 0xffffffff:032b}"[::-1], 2) >> 16) * F(1 / 65536)
    return (np.array([e1, e2], dtype=np.float32) * F(downsample)).astype(np.uint32)


def build_query(arrays, lane):
    packed = int(arrays["compacted"][lane])
    probe = packed & 0xFFFFF
    trace_xy = np.array([(packed >> 20) & 31, (packed >> 25) & 31], dtype=np.uint32)
    if probe >= arrays["num_uniform"]:
        raise Reject("adaptive/non-uniform screen probe")
    if trace_xy[0] >= arrays["trace_res"] or trace_xy[1] >= arrays["trace_res"]:
        raise Reject("trace texel outside tracing resolution")
    atlas_xy = np.array([probe % arrays["atlas_view"][0], probe // arrays["atlas_view"][0]], dtype=np.uint32)
    uniform_xy = np.array([probe % arrays["view_size"][0], probe // arrays["view_size"][0]], dtype=np.uint32)
    scene_word = int(arrays["scene_depth"][atlas_xy[1], atlas_xy[0]])
    scene_depth = struct.unpack("<f", struct.pack("<I", scene_word))[0]
    if not math.isfinite(scene_depth) or scene_depth <= 0:
        raise Reject("invalid screen-probe scene depth")
    screen_xy = uniform_xy * arrays["downsample"] + arrays["jitter"] + arrays["view_rect"][:2]
    tile_xy = (screen_xy - arrays["jitter"] - arrays["view_rect"][:2]) // arrays["downsample"]
    if not np.array_equal(tile_xy, uniform_xy):
        raise Reject("screen tile != uniform probe tile")
    clamped = np.minimum(screen_xy.astype(np.float32),
                         arrays["view_rect"][:2].astype(np.float32) + arrays["view_size_float"] - F(1))
    screen_uv = (clamped + F(0.5)) * arrays["buffer_inv"]
    screen_position = (screen_uv - arrays["scale_bias"][[3, 2]]) / arrays["scale_bias"][:2]
    xy = screen_position if arrays["ortho"] else screen_position * F(scene_depth)
    homogeneous = np.array([xy[0], xy[1], scene_depth, 1], dtype=np.float32)
    position = (homogeneous @ arrays["screen_to_world"])[:3]
    trace_buffer_xy = atlas_xy * arrays["trace_res"] + trace_xy
    ray_info = int(arrays["ray_infos"][trace_buffer_xy[1], trace_buffer_xy[0]])
    level = (ray_info >> 12) & 15
    cell = np.array([ray_info & 63, (ray_info >> 6) & 63], dtype=np.uint32)
    if level > 15 or (arrays["max_importance"] >> level) <= 0:
        raise Reject("unsupported importance level")
    if np.any(cell >= (arrays["max_importance"] >> level)):
        raise Reject("importance cell outside level")
    wrapped = np.array([tile_xy[0], tile_xy[1], arrays["blue_frame"]], dtype=np.uint32) & arrays["blue_masks"]
    noise_xy = np.array([wrapped[0], wrapped[2] * arrays["blue_dims"][1] + wrapped[1]], dtype=np.uint32)
    raw_bgra = arrays["blue"][noise_xy[1], noise_xy[0]].astype(np.uint32)
    raw_rg = np.array([raw_bgra[2], raw_bgra[1]], dtype=np.float32)
    jitter_ray = raw_rg / F(255.0)
    ray = cache.ray_from_record(ray_info, trace_xy, jitter_ray,
                                arrays["trace_res"], arrays["max_importance"], True)
    query = {"translatedWorldPosition": position.tolist(),
             "direction": ray["direction"], "coneHalfAngle": ray["coneHalfAngle"]}
    witness = {"dispatchIndex": lane, "packedCompactedTraceWord": packed,
               "screenProbeIndex": probe, "probeAtlasXY": atlas_xy.tolist(),
               "uniformScreenTileXY": uniform_xy.tolist(), "traceTexelXY": trace_xy.tolist(),
               "traceBufferXY": trace_buffer_xy.tolist(),
               "screenTemporalIndex": arrays["state_frame_mod8"],
               "tileJitter": arrays["jitter"].tolist(),
               "screenXY": screen_xy.tolist(), "screenXYClamped": clamped.tolist(),
               "screenUV": screen_uv.tolist(), "packedSceneDepth": scene_word,
               "sceneDepth": scene_depth, "sceneDepthCM": scene_depth,
               "sceneDepthUnits": "cm (translated-world depth)", "screenPosition": screen_position.tolist(),
               "orthographic": arrays["ortho"], "homogeneousPosition": homogeneous.tolist(),
               "screenToTranslatedWorld": arrays["screen_to_world"].tolist(),
               "screenTileXY": tile_xy.tolist(),
               "blueNoiseFrameIndex": arrays["blue_frame"],
               "blueNoiseTextureXY": noise_xy.tolist(),
               "blueNoiseRawBGRA": raw_bgra.tolist(),
               "blueNoiseShaderRG": raw_rg.tolist(),
               "blueNoiseDecoded": jitter_ray.tolist(),
               "rayInfoPacked": ray_info, "angularRay": ray}
    return query, witness


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--engine", default=r"D:/UnrealEngine")
    ap.add_argument("--max-lanes", type=int, default=20000)
    args = ap.parse_args()
    root, out = Path(args.root), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fig = out / "figures"
    fig.mkdir(exist_ok=True)

    d = dump_reader.Dump(root, WORLD_PASS_ID)
    p = d.selected
    if p["EventName"] != "TraceGlobalSDFAndApplyRadianceCache" or not p["Pointer"].startswith("0004"):
        raise ValueError("selected pass is not presented-graph-0004 TraceGlobalSDFAndApplyRadianceCache")
    if p["Id"] != WORLD_PASS_ID:
        raise ValueError("selected pass id is not the presented-graph trace pass")
    if (root / "Base/Status.txt").read_text(encoding="utf-8-sig").strip() != "ok":
        raise ValueError("DumpGPU status is not ok")
    infos = json.loads((root / "Base/Infos.json").read_text(encoding="utf-8-sig"))
    cvars = parse_cvars(root / "Base/ConsoleVariables.csv")
    refs = validate_references(d)
    blue, blue_meta = decode_blue_noise(d)

    bindings_names = [
        "RadianceCacheParameters.RadianceProbeIndirectionTexture",
        "RadianceCacheParameters.RadianceCacheFinalRadianceAtlas",
        "RadianceCacheParameters.RadianceCacheDepthAtlas",
        "RadianceCacheParameters.ProbeWorldOffset",
        "ScreenProbeParameters.ImportanceSampling.StructuredImportanceSampledRayInfosForTracing",
        "ScreenProbeParameters.ScreenProbeSceneDepth",
        "ScreenProbeParameters.BlueNoise.BlueNoise.Vec2Texture",
        "ScreenProbeParameters.RWTraceHit",
        "ScreenProbeParameters.RWTraceRadiance",
        "CompactedTraceParameters.CompactedTraceTexelAllocator",
        "CompactedTraceParameters.CompactedTraceTexelData",
    ]
    parameter_bindings = {n: d.parameter_binding(n) for n in bindings_names}

    ind, ind_meta = d.read_texture("Lumen.RadianceCache.RadianceProbeIndirectionTexture")
    rad, rad_meta = d.read_texture("Lumen.RadianceCache.FinalRadianceAtlas")
    dep, dep_meta = d.read_texture("Lumen.RadianceCache.DepthProbeAtlasTexture")
    ray_infos, ray_meta = d.read_texture("Lumen.ScreenProbeGather.RayInfosForTracing")
    scene_depth, scene_meta = d.read_texture("Lumen.ScreenProbeGather.ScreenProbeSceneDepth")
    hit, hit_meta = d.read_texture("Lumen.ScreenProbeGather.TraceHit", output=True)
    trace_rad, trace_meta = d.read_texture("Lumen.ScreenProbeGather.TraceRadiance", output=True)
    off_raw, off_meta = d.read_buffer("Lumen.RadianceCache.ProbeWorldOffset")
    alloc_raw, alloc_meta = d.read_buffer("Lumen.ScreenProbeGather.CompactedTraceTexelAllocator")
    comp_raw, comp_meta = d.read_buffer("Lumen.ScreenProbeGather.CompactedTraceTexelData")
    args_raw, args_meta = d.read_buffer("Lumen.ScreenProbeGather.CompactTracingIndirectArgs")
    if off_meta["bytesPerElement"] != 8:
        raise ValueError("ProbeWorldOffset is not half4")
    offsets = off_raw.view("<f2").reshape(off_meta["numElements"], 4)
    allocator = int(alloc_raw.view("<u4")[0])
    compacted = comp_raw.view("<u4")
    indirect_args = args_raw.view("<u4").reshape(args_meta["numElements"], 3)
    active = compacted[:allocator]
    coords = ((active & 0xFFFFF).astype(np.uint64) * np.uint64(1024)
              + ((active >> 20) & 31).astype(np.uint64) * np.uint64(32)
              + ((active >> 25) & 31).astype(np.uint64))
    unique = int(np.unique(coords).size)

    if int(ind_meta["extentX"]) != 192 or int(ind_meta["extentY"]) != 48 or int(ind_meta["depth"]) != 48:
        raise ValueError("indirection volume dimensions changed")
    if int(ind_meta["rowPitchBytes"]) != 768 or int(ind_meta["zSliceStrideBytes"]) != 36864:
        raise ValueError("indirection strides changed")
    if int(rad_meta["extentX"]) != 4352 or int(rad_meta["extentY"]) != 4352:
        raise ValueError("radiance atlas extent changed")
    if int(dep_meta["extentX"]) != 4096 or int(dep_meta["extentY"]) != 4096:
        raise ValueError("depth atlas extent changed")
    if int(ray_meta["extentX"]) != 640 or int(ray_meta["extentY"]) != 672:
        raise ValueError("ray-info extent changed")
    if int(scene_meta["extentX"]) != 80 or int(scene_meta["extentY"]) != 84:
        raise ValueError("screen-probe depth extent changed")
    if allocator <= 0 or allocator > active.size:
        raise ValueError("invalid compacted allocator")

    params = {
        "ClipmapCornerTWSAndCellSize": d.field("RadianceCacheParameters.ClipmapCornerTWSAndCellSize"),
        "RadianceProbeSettings": d.field("RadianceCacheParameters.RadianceProbeSettings"),
        "NumRadianceProbeClipmaps": d.field("RadianceCacheParameters.RadianceCacheInputs.NumRadianceProbeClipmaps"),
        "RadianceProbeClipmapResolution": d.field("RadianceCacheParameters.RadianceCacheInputs.RadianceProbeClipmapResolution"),
        "RadianceProbeResolution": d.field("RadianceCacheParameters.RadianceCacheInputs.RadianceProbeResolution"),
        "FinalProbeResolution": d.field("RadianceCacheParameters.RadianceCacheInputs.FinalProbeResolution"),
        "FinalRadianceAtlasMaxMip": d.field("RadianceCacheParameters.RadianceCacheInputs.FinalRadianceAtlasMaxMip"),
        "ReprojectionRadiusScale": d.field("RadianceCacheParameters.RadianceCacheInputs.ReprojectionRadiusScale"),
        "InvClipmapFadeSize": d.field("RadianceCacheParameters.RadianceCacheInputs.InvClipmapFadeSize"),
        "InvProbeFinalRadianceAtlasResolution": d.field("RadianceCacheParameters.InvProbeFinalRadianceAtlasResolution"),
        "RadianceCacheOneOverCachedLightingPreExposure": d.field("RadianceCacheParameters.RadianceCacheOneOverCachedLightingPreExposure"),
        "ProbeAtlasResolutionModuloMask": d.field("RadianceCacheParameters.ProbeAtlasResolutionModuloMask"),
        "ProbeAtlasResolutionDivideShift": d.field("RadianceCacheParameters.ProbeAtlasResolutionDivideShift"),
        "UseSkyVisibility": d.field("RadianceCacheParameters.RadianceCacheInputs.UseSkyVisibility"),
        "ShowBlackRadianceCacheLighting": d.field("RadianceCacheParameters.ShowBlackRadianceCacheLighting"),
    }
    view = {
        "ViewRectMinAndSize": d.field("TracingParameters.View.ViewRectMinAndSize"),
        "ViewSizeAndInvSize": d.field("TracingParameters.View.ViewSizeAndInvSize"),
        "BufferSizeAndInvSize": d.field("TracingParameters.View.BufferSizeAndInvSize"),
        "ScreenPositionScaleBias": d.field("TracingParameters.View.ScreenPositionScaleBias"),
        "ScreenToTranslatedWorld": d.field("TracingParameters.View.ScreenToTranslatedWorld"),
        "ViewToClip": d.field("TracingParameters.View.ViewToClip"),
        "PreExposure": d.field("TracingParameters.View.PreExposure"),
        "PreExposureNote": "View.PreExposure = 0.00048828125 (1/2048) is the camera pre-exposure applied after the cache lookup at LumenScreenProbeTracing.usf:851; it is not the cache's CachedLightingPreExposure (1/16).",
        "StateFrameIndex": d.field("TracingParameters.View.StateFrameIndex"),
        "StateFrameIndexMod8": d.field("TracingParameters.View.StateFrameIndexMod8"),
        "TranslatedWorldCameraOrigin": d.field("TracingParameters.View.TranslatedWorldCameraOrigin"),
        "SkylightLeakingColor": d.field("TracingParameters.SkylightLeakingColor"),
        "SampleHeightFogOnGI": d.field("TracingParameters.SampleHeightFogOnGI"),
        "NumGlobalSDFClipmaps": d.field("TracingParameters.NumGlobalSDFClipmaps"),
    }
    screen_probe = {
        "ScreenProbeViewSize": d.field("ScreenProbeParameters.ScreenProbeViewSize"),
        "ScreenProbeAtlasViewSize": d.field("ScreenProbeParameters.ScreenProbeAtlasViewSize"),
        "ScreenProbeAtlasBufferSize": d.field("ScreenProbeParameters.ScreenProbeAtlasBufferSize"),
        "NumUniformScreenProbes": d.field("ScreenProbeParameters.NumUniformScreenProbes"),
        "ScreenProbeDownsampleFactor": d.field("ScreenProbeParameters.ScreenProbeDownsampleFactor"),
        "FixedJitterIndex": d.field("ScreenProbeParameters.FixedJitterIndex"),
        "ScreenProbeRayDirectionFrameIndex": d.field("ScreenProbeParameters.ScreenProbeRayDirectionFrameIndex"),
        "ScreenProbeTracingOctahedronResolution": d.field("ScreenProbeParameters.ScreenProbeTracingOctahedronResolution"),
        "MaxImportanceSamplingOctahedronResolution": d.field("ScreenProbeParameters.ImportanceSampling.MaxImportanceSamplingOctahedronResolution"),
        "TargetFormatQuantizationError": d.field("ScreenProbeParameters.TargetFormatQuantizationError"),
    }
    if int(screen_probe["ScreenProbeTracingOctahedronResolution"]) != 8:
        raise ValueError("unexpected tracing octahedron resolution")
    if int(screen_probe["MaxImportanceSamplingOctahedronResolution"]) != 16:
        raise ValueError("unexpected importance octahedron resolution")

    arrays = {
        "compacted": compacted,
        "num_uniform": int(screen_probe["NumUniformScreenProbes"]),
        "atlas_view": tuple(int(x) for x in screen_probe["ScreenProbeAtlasViewSize"]),
        "view_size": tuple(int(x) for x in screen_probe["ScreenProbeViewSize"]),
        "trace_res": int(screen_probe["ScreenProbeTracingOctahedronResolution"]),
        "max_importance": int(screen_probe["MaxImportanceSamplingOctahedronResolution"]),
        "scene_depth": scene_depth, "ray_infos": ray_infos,
        "downsample": int(screen_probe["ScreenProbeDownsampleFactor"]),
        "view_rect": np.array(view["ViewRectMinAndSize"], dtype=np.uint32),
        "view_size_float": np.array(view["ViewSizeAndInvSize"], dtype=np.float32)[:2],
        "buffer_inv": np.array(view["BufferSizeAndInvSize"], dtype=np.float32)[2:],
        "scale_bias": np.array(view["ScreenPositionScaleBias"], dtype=np.float32),
        "screen_to_world": np.array(view["ScreenToTranslatedWorld"], dtype=np.float32).reshape(4, 4),
        "ortho": bool(np.array(view["ViewToClip"], dtype=np.float32).reshape(4, 4)[3, 3] >= 1),
        "blue": blue,
        "blue_dims": np.array(blue_meta["dimensions"], dtype=np.uint32),
        "blue_masks": np.array(blue_meta["moduloMasks"], dtype=np.uint32),
        "blue_frame": int(screen_probe["ScreenProbeRayDirectionFrameIndex"]),
        "state_frame_mod8": int(view["StateFrameIndexMod8"]),
        "jitter": tile_jitter(int(view["StateFrameIndexMod8"]), int(screen_probe["ScreenProbeDownsampleFactor"])),
    }

    rejections = Counter()
    selected = None
    for lane in range(min(allocator, args.max_lanes)):
        try:
            query, witness = build_query(arrays, lane)
            result = cache.lookup(query, params, ind, offsets, rad, dep)
        except Reject as exc:
            rejections[str(exc)] += 1
            continue
        except (ValueError, IndexError) as exc:
            rejections[f"lookup: {exc}"] += 1
            continue
        fw = result["fractionalWeights"]
        if min(fw) < 0.05 or max(fw) > 0.95:
            rejections["fractional weights too close to a cell boundary"] += 1
            continue
        tb = witness["traceBufferXY"]
        hit_word = int(hit[tb[1], tb[0]])
        if hit_word & 1:
            rejections["final TraceHit.bHit is true"] += 1
            continue
        if not any(abs(x) > 0 for x in result["cacheRadiance"]):
            rejections["zero replayed cache radiance"] += 1
            continue
        captured_distance = struct.unpack("<f", struct.pack("<I", hit_word & 0x7FFFFFFC))[0]
        if abs(float(result["cacheTraceHitDistance"]) - captured_distance) > 0.001:
            rejections["depth does not match captured TraceHit"] += 1
            continue
        selected = (lane, query, witness, result, hit_word)
        break
    if selected is None:
        raise RuntimeError(f"no eligible lane; rejections={dict(rejections)}")

    lane, query, witness, result, hit_word = selected
    tb = witness["traceBufferXY"]
    coord = (int(witness["screenProbeIndex"]) * 1024 + int(witness["traceTexelXY"][0]) * 32
             + int(witness["traceTexelXY"][1]))
    occurrences = int(np.count_nonzero(coords == coord))
    if occurrences != 1 or unique != allocator:
        raise ValueError("selected trace texel is not unique in the compacted list")
    captured_distance = struct.unpack("<f", struct.pack("<I", hit_word & 0x7FFFFFFC))[0]
    replayed = float(result["cacheTraceHitDistance"])
    radiance_word = int(trace_rad[tb[1], tb[0]])
    final_radiance = cache.decode_r11g11b10(radiance_word)
    model = np.array(result["cacheRadiance"], dtype=np.float32) * F(view["PreExposure"])
    quant_error = np.array(screen_probe["TargetFormatQuantizationError"], dtype=np.float32)
    tolerance = np.abs(model) * quant_error
    radiance_diff = np.abs(final_radiance - model)
    radiance_consistent = bool(np.all(radiance_diff <= tolerance))

    if len(result["neighbors"]) != 8 or result["radianceTapCount"] != 32:
        raise ValueError("lookup did not produce 8 neighbors x 4 taps")
    if abs(result["sumWeights"] - 1.0) > 1e-5:
        raise ValueError("neighbor weights do not sum to 1")

    # --- previews ---------------------------------------------------------
    # Neighborhood: eight 34x34 atlas tiles arranged as the shader's 2x2x2
    # corner order (x fastest, then y, then z).
    tile_px = int(params["FinalProbeResolution"])
    scale = 4
    gain = 4.0
    tile_width = tile_px * scale
    plane_width = 2 * tile_width + 4
    canvas = np.zeros((plane_width, 2 * plane_width + 12, 3), dtype=np.float32)
    drawn_tiles = []
    for i, n in enumerate(result["neighbors"]):
        ex, ey, ez = n["offsetBits"]
        tile = np.array(n["atlasTile"])
        x0, y0 = tile[0] * tile_px, tile[1] * tile_px
        sub = rad[y0:y0 + tile_px, x0:x0 + tile_px]
        rgb = np.stack([cache.decode_r11g11b10(int(v)) for v in sub.ravel()]).reshape(tile_px, tile_px, 3)
        rgb = np.clip(rgb * F(view["PreExposure"]) * F(gain), 0, 1)
        img = Image.fromarray((rgb * 255 + 0.5).astype(np.uint8), "RGB")
        img = np.array(img.resize((tile_px * scale, tile_px * scale), Image.NEAREST), dtype=np.float32) / 255
        ox = ez * (plane_width + 12) + ex * (tile_width + 4)
        oy = (1 - ey) * (tile_width + 4)
        drawn_tiles.append({"neighbor": i, "offsetBits": [ex, ey, ez], "pixelOrigin": [ox, oy]})
        canvas[oy:oy + tile_px * scale, ox:ox + tile_px * scale] = img
    assert len(drawn_tiles) == 8 and len({tuple(t["pixelOrigin"]) for t in drawn_tiles}) == 8
    Image.fromarray((canvas * 255 + 0.5).astype(np.uint8), "RGB").save(fig / "world-cache-neighborhood.png")

    # Taps: 8 neighbor rows x 4 bilinear taps, colour chip with a white border.
    # A named gain of 4 keeps the dim taps visible; the metrics sidecar records it.
    tap_gain = 16.0
    chip = 28
    tap_canvas = np.zeros((8 * chip + 9, 4 * chip + 5, 3), dtype=np.float32)
    for i in range(8):
        for j in range(4):
            tap = result["radianceTaps"][i * 4 + j]
            rgb = np.clip(np.array(tap["decodedRadiance"], dtype=np.float32)
                          * F(view["PreExposure"]) * F(tap_gain), 0, 1)
            tap_canvas[i * chip + 1:i * chip + chip, j * chip + 1:j * chip + chip] = rgb
    Image.fromarray((tap_canvas * 255 + 0.5).astype(np.uint8), "RGB").save(fig / "world-cache-taps.png")

    # Two z-plane zoom of the selected 2x2x2 neighborhood. Fill brightness is
    # proportional to the captured trilinear weight; the red dot is the query's
    # fractional x/y position in each plane.
    grid = int(params["RadianceProbeClipmapResolution"])
    base = np.array(result["bottomCell"])
    cell = 56
    gap = 10
    img = Image.new("RGB", (2 * (2 * cell + gap) + gap, 2 * cell + 2 * gap), (10, 10, 10))
    draw = ImageDraw.Draw(img)
    weight_by_offset = {tuple(n["offsetBits"]): n["trilinearWeight"] for n in result["neighbors"]}
    max_weight = max(weight_by_offset.values()) or 1.0
    for zi, zoff in enumerate((0, 1)):
        ox = gap + zi * (2 * cell + gap + gap)
        oy = gap
        for ex in (0, 1):
            for ey in (0, 1):
                w = weight_by_offset.get((ex, ey, zoff), 0.0)
                level = int(50 + 170 * (w / max_weight))
                x0 = ox + ex * cell
                y0 = oy + ey * cell
                draw.rectangle([x0, y0, x0 + cell - 2, y0 + cell - 2], fill=(0, level, 30))
        qx = ox + (float(result["fractionalWeights"][0])) * cell
        qy = oy + (float(result["fractionalWeights"][1])) * cell
        draw.ellipse([qx - 5, qy - 5, qx + 5, qy + 5], fill=(255, 60, 60))
    img.save(fig / "world-cache-probe-grid.png")

    # --- report -----------------------------------------------------------
    def res(name, output=False):
        path, desc, ev = d.resource(name, output=output)
        writer = next((q for q in d.passes if q["Pointer"] == ev["version"]), None)
        return {"name": name, "file": str(path.relative_to(d.root)), "bytes": path.stat().st_size,
                "sha256": sha_file(path), "uniqueResource": ev["uniqueResource"],
                "version": ev["version"],
                "producerPass": None if writer is None else {
                    "id": writer["Id"], "eventName": writer["EventName"], "pointer": writer["Pointer"]},
                "descriptor": desc, "layout": ev.get("layout")}

    inputs = {
        "indirection": res("Lumen.RadianceCache.RadianceProbeIndirectionTexture"),
        "finalRadianceAtlas": res("Lumen.RadianceCache.FinalRadianceAtlas"),
        "depthProbeAtlas": res("Lumen.RadianceCache.DepthProbeAtlasTexture"),
        "probeWorldOffset": res("Lumen.RadianceCache.ProbeWorldOffset"),
        "rayInfosForTracing": res("Lumen.ScreenProbeGather.RayInfosForTracing"),
        "screenProbeSceneDepth": res("Lumen.ScreenProbeGather.ScreenProbeSceneDepth"),
        "compactedTraceTexelAllocator": res("Lumen.ScreenProbeGather.CompactedTraceTexelAllocator"),
        "compactedTraceTexelData": res("Lumen.ScreenProbeGather.CompactedTraceTexelData"),
        "compactTracingIndirectArgs": res("Lumen.ScreenProbeGather.CompactTracingIndirectArgs"),
        "traceHit": res("Lumen.ScreenProbeGather.TraceHit", output=True),
        "traceRadiance": res("Lumen.ScreenProbeGather.TraceRadiance", output=True),
    }
    binding_expected = {
        "RadianceCacheParameters.RadianceProbeIndirectionTexture": inputs["indirection"]["uniqueResource"],
        "RadianceCacheParameters.RadianceCacheFinalRadianceAtlas": inputs["finalRadianceAtlas"]["uniqueResource"],
        "RadianceCacheParameters.RadianceCacheDepthAtlas": inputs["depthProbeAtlas"]["uniqueResource"],
        "ScreenProbeParameters.ImportanceSampling.StructuredImportanceSampledRayInfosForTracing": inputs["rayInfosForTracing"]["uniqueResource"],
        "ScreenProbeParameters.ScreenProbeSceneDepth": inputs["screenProbeSceneDepth"]["uniqueResource"],
    }
    binding_checks = {}
    for name, expected in binding_expected.items():
        actual = parameter_bindings[name]["Pointer"]
        binding_checks[name] = {"binding": actual, "resource": expected,
                                "matches": actual == expected}
        if actual != expected:
            raise ValueError(f"binding {name} pointer != captured resource")
    for name in ("RadianceCacheParameters.ProbeWorldOffset",
                 "ScreenProbeParameters.RWTraceHit",
                 "ScreenProbeParameters.RWTraceRadiance",
                 "CompactedTraceParameters.CompactedTraceTexelAllocator",
                 "CompactedTraceParameters.CompactedTraceTexelData"):
        if parameter_bindings[name].get("Null"):
            raise ValueError(f"binding {name} is null")
    if inputs["traceHit"]["version"] != p["Pointer"] or inputs["traceRadiance"]["version"] != p["Pointer"]:
        raise ValueError("TraceHit/TraceRadiance are not written by the selected pass")
    non_zero_offsets = int(np.count_nonzero(off_raw))

    source = [
        read_source_evidence(args.engine, "Engine/Shaders/Private/Lumen/LumenRadianceCacheInterpolation.ush",
                             [245, 246, 247, 248, 249, 250, 251, 252, 253, 257, 263, 264, 265, 272,
                              284, 285, 286, 287, 288, 289, 332, 334, 335, 337, 338, 339, 340, 341,
                              343, 346, 348, 349, 350, 351, 352, 353, 354, 401, 402, 403, 429, 440,
                              443, 444, 446, 447, 448, 450, 475, 476, 477, 478, 479, 480, 481, 482,
                              484, 485, 486, 487, 488, 489, 490, 491, 492, 499, 501, 502, 503, 505,
                              507, 510, 511], "cache probe, parallax, interpolation and apply"),
        read_source_evidence(args.engine, "Engine/Shaders/Private/Lumen/LumenScreenProbeTracing.usf",
                             [685, 686, 687, 688, 693, 699, 700, 701, 702, 703, 704, 719, 720, 727,
                              828, 829, 830, 831, 832, 833, 849, 851, 859, 860, 861, 862, 870, 874, 875, 876,
                              877, 878, 879, 880], "trace dispatch guard and cache call"),
        read_source_evidence(args.engine, "Engine/Shaders/Private/MonteCarlo.ush",
                             list(range(159, 207)), "equi-area mapping"),
        read_source_evidence(args.engine, "Engine/Shaders/Private/Common.ush",
                             list(range(1962, 1981)), "RayIntersectSphere far root"),
        read_source_evidence(args.engine, "Engine/Shaders/Private/BlueNoiseCommon.ush",
                             list(range(11, 20)), "GetBlueNoiseVec2"),
        read_source_evidence(args.engine, "Engine/Shaders/Private/Lumen/LumenScreenProbeCommon.ush",
                             [122, 123, 124, 125, 135, 136, 137, 138, 202, 203, 204, 207, 208, 209,
                              210, 211], "screen position, tile and compacted texel encoding"),
    ]

    preview_metrics = {
        "neighborhood": {"image": "figures/world-cache-neighborhood.png",
                         "layout": "Left panel z=base.z; right panel z=base.z+1. Each panel is 2x2: x increases right, y increases upward. Top-left is offset (0,1,z); bottom-left (0,0,z).",
                         "drawnTiles": drawn_tiles,
                         "imageDimensions": [int(canvas.shape[1]), int(canvas.shape[0])],
                         "tilePixels": tile_px, "upscale": scale, "displayGain": gain,
                         "radianceScale": f"stored cache-pre-exposed R11G11B10 atlas * View.PreExposure ({view['PreExposure']}) * gain ({gain}) clip[0,1]; the *16 cache-exposure inverse is not applied, so this previews the stored atlas, not the un-pre-exposed lookup radiance",
                         "neighborWeightMin": float(min(n["trilinearWeight"] for n in result["neighbors"])),
                         "neighborWeightMax": float(max(n["trilinearWeight"] for n in result["neighbors"]))},
        "taps": {"image": "figures/world-cache-taps.png", "rows": 8, "columns": 4,
                 "tapCount": result["radianceTapCount"], "displayGain": tap_gain,
                 "effectiveWeightSum": float(sum(t["effectiveWeight"] for t in result["radianceTaps"])),
                 "radianceScale": f"stored cache-pre-exposed R11G11B10 atlas * View.PreExposure ({view['PreExposure']}) * gain ({tap_gain}) clip[0,1]; the *16 cache-exposure inverse is not applied"},
        "probeGrid": {"image": "figures/world-cache-probe-grid.png", "clipmapResolution": grid,
                      "pixelsPerCell": cell, "panels": "left z=base.z, right z=base.z+1",
                      "cellFill": "brightness proportional to captured trilinear weight",
                      "red": "query fractional x/y position",
                      "baseCell": base.tolist(), "fractionalWeights": result["fractionalWeights"],
                      "cellSizeCM": float(result["clipmapCellSize"]),
                      "positionMinusCornerCM": result["positionMinusClipmapCornerCM"],
                      "cellCoordinateFloat": result["cellCoordinateFloat"],
                      "units": "baseCell/fractionalWeights are clipmap cells; positionMinusCornerCM is cm; cellCoordinateFloat = positionMinusCornerCM/cellSizeCM",
                      "weightsByZPlane": {str(z): {f"{ex},{ey}": weight_by_offset[(ex, ey, z)]
                                                   for ex in (0, 1) for ey in (0, 1)}
                                          for z in (0, 1)}},
    }
    (fig / "world-cache-preview-metrics.json").write_text(
        json.dumps(preview_metrics, indent=2, allow_nan=False), encoding="utf-8")

    report = {
        "schema": "sponza-world-radiance-cache-query-v1",
        "scope": "one declared World Radiance Cache lookup reconstructed from a captured presented-graph compacted screen-probe trace lane; not a recorded shader-local query and not proof that a dispatched ray chose it",
        "engine": {"root": args.engine, "version": "5.8.1",
                   "commit": "71fe36aac5a8df5ccd66c763ffc902b29b6a9c43"},
        "capture": {"root": str(root), "infos": infos, "pass": p,
                    "parameterPayloads": d.parameter_evidence,
                    "consoleVariables": {k: cvars.get(k) for k in (
                        "r.Lumen.ScreenProbeGather.RadianceCache",
                        "r.Lumen.ScreenProbeGather.ImportanceSample",
                        "r.Lumen.ScreenProbeGather.ImportanceSample.NumLevels",
                        "r.Lumen.HardwareRayTracing",
                        "r.Lumen.ScreenProbeGather.HardwareRayTracing")}},
        "referencedStructs": refs,
        "parameterBindings": parameter_bindings,
        "bindingChecks": binding_checks,
        "blueNoise": blue_meta,
        "inputs": inputs,
        "parameters": {"radianceCache": params, "view": view, "screenProbe": screen_probe},
        "dispatch": {"allocator": allocator, "compactedTraceTexelDataElements": int(compacted.size),
                     "compactedTraceCoordsUnique": unique,
                     "indirectArgs": indirect_args.tolist(),
                     "dispatchNote": "The pass binds both Div64 and Div32 indirect-arg triples; DumpGPU does not record which triple the dispatch consumed. The selected lane is below the allocator."},
        "selection": {"rule": "ascending compacted lane; uniform screen probe; valid scene depth; valid clipmap coverage; eight allocated neighbors; fractional weights in [0.05,0.95]; captured final TraceHit.bHit=false; non-zero replayed cache radiance; replayed depth matches captured TraceHit within 0.001 cm",
                      "selectedLane": lane, "selectedTraceBufferOccurrences": occurrences,
                      "rejectionsUpToSelection": dict(rejections)},
        "queryWitness": witness,
        "directionalEncoding": {
            "rayInfoField": "ScreenProbeParameters.ImportanceSampling.StructuredImportanceSampledRayInfosForTracing (PF_R16_UINT)",
            "cell": witness["angularRay"]["cell"], "level": witness["angularRay"]["level"],
            "cellResolution": witness["angularRay"]["cellResolution"],
            "jitter": witness["angularRay"]["jitter"],
            "equalAreaUV": witness["angularRay"]["uv"],
            "direction": witness["angularRay"]["direction"],
            "coneHalfAngle": witness["angularRay"]["coneHalfAngle"],
            "formula": "cell=(rayInfo&63,(rayInfo>>6)&63); level=(rayInfo>>12)&15; size=16>>level; uv=(cell+jitter)/size; direction=EqualAreaToSphere(uv); alpha=acosFast(1-1/size^2)",
        },
        "formulas": {
            "clipmapSelection": "for level: q=(x-ClipmapCornerTWS)/CellSize; bottom=clip((q-0.5)*InvClipmapFadeSize,0,1); top=clip((Res-0.5-q)*InvClipmapFadeSize,0,1); first level with min(bottom,top)>0",
            "grid": "cellCoordinateFloat = (translatedWorldPosition - ClipmapCornerTWS)/CellSize (cell units); base=floor(cellCoordinateFloat-0.5); a=frac(cellCoordinateFloat-0.5). The raw (x-corner) values are cm.",
            "indirection": "word=Indirection[base+offset+(level*Res,0,0)]; index=word&0xFFFF; 0xFFFFFFFF is unallocated",
            "probeCenter": "center = corner + (cell + 0.5)*CellSize + ProbeWorldOffset[index]",
            "atlasUV": "(FinalProbeResolution*tile + RadianceProbeResolution*uv + (1<<FinalRadianceAtlasMaxMip)) * InvProbeFinalRadianceAtlasResolution",
            "bilinear": "q=atlasUV*tileExtent-0.5; lo=floor(q); t=q-lo; four taps at lo+(0,0),(1,0),(0,1),(1,1)",
            "parallax": "t=(-B+sqrt(B^2-4AC))/(2A) with A=dot(omega,omega), B=2*dot(omega,v), C=dot(v,v)-radius^2; correction=t^2/(radius*dot(reprojected,omega))",
            "combine": "nested Z/Y/X lerp of eight neighborRadiance values with weights a",
            "radianceScale": "neighborRadiance = storedBilinear * RadianceCacheOneOverCachedLightingPreExposure * correction (un-pre-exposed); forwardModel = cacheRadiance * View.PreExposure (view-pre-exposed TraceRadiance space)",
        },
        "cacheLookup": result,
        "validity": {"allEightIndirectionAllocated": all(n["indirectionPacked"] != 0xFFFFFFFF for n in result["neighbors"]),
                     "neighborCount": len(result["neighbors"]),
                     "uniqueProbeIndices": len({n["probeIndex"] for n in result["neighbors"]}),
                     "radianceTapCount": result["radianceTapCount"],
                     "neighborWeightsSum": result["sumWeights"],
                     "fractionalWeights": result["fractionalWeights"],
                     "mipLevel": result["mipLevel"],
                     "probeWorldOffsetNonZeroBytes": non_zero_offsets,
                     "probeWorldOffsetNote": ("all 16384 half4 offsets are zero in this captured version"
                                              if non_zero_offsets == 0 else "non-zero offsets present")},
        "radianceScale": {
            "storedTap": "R11G11B10 word decoded to linear float; this is the cache-pre-exposed atlas value (linear radiance * CachedLightingPreExposure)",
            "parallaxCorrection": "t*t/(radius*dot(reprojectedDirection,omega))",
            "cachedLightingPreExposureInverse": float(params["RadianceCacheOneOverCachedLightingPreExposure"]),
            "cachedLightingPreExposure": 1.0 / float(params["RadianceCacheOneOverCachedLightingPreExposure"]),
            "cachedLightingPreExposureNote": "inverse of the captured RadianceCacheOneOverCachedLightingPreExposure=16.0; the cache atlas stores radiance*1/16",
            "neighborRadiance": "storedBilinear * cachedLightingPreExposureInverse * parallaxCorrection",
            "cacheRadiance": "nested Z/Y/X lerp of the eight neighborRadiance values after the *16 cache-exposure inverse; un-pre-exposed cached-lighting radiance",
            "viewPreExposure": float(view["PreExposure"]),
            "forwardTraceRadianceModel": "cacheRadiance * View.PreExposure; this is the view-pre-exposed TraceRadiance buffer space, not display-linear",
            "note": "cacheRadiance is un-pre-exposed cached-lighting radiance: the stored atlas is multiplied by RadianceCacheOneOverCachedLightingPreExposure (16) at LumenRadianceCacheInterpolation.ush:272, cancelling CachedLightingPreExposure. The consumer then multiplies by View.PreExposure at LumenScreenProbeTracing.usf:851 before storing TraceRadiance. Therefore forwardModel = cacheRadiance*View.PreExposure is view-pre-exposed scene-linear, not display-linear."},
        "comparison": {"capturedHitWord": hit_word, "capturedHitWordHex": f"{hit_word:08x}",
                       "capturedDistance": captured_distance, "replayedCacheDepth": replayed,
                       "depthAbsDiff": abs(replayed - captured_distance),
                       "capturedDistanceUnits": "cm", "replayedCacheDepthUnits": "cm",
                       "depthAbsDiffUnits": "cm",
                       "finalRadianceWord": radiance_word, "finalRadiance": final_radiance.tolist(),
                       "forwardModel": model.tolist(), "absDiff": radiance_diff.tolist(),
                       "tolerance": tolerance.tolist(), "forwardConsistent": radiance_consistent,
                       "equalityClaimed": False,
                       "forwardModelSpace": "view-pre-exposed scene-linear (after *View.PreExposure, matching LumenScreenProbeTracing.usf:851); cacheRadiance remains un-pre-exposed",
                       "capturedTraceRadianceSpace": "view-pre-exposed scene-linear R11G11B10 store",
                       "boundary": "cache-subtotal boundary; the preceding GDF/voxel result, skylight leaking, height fog and final R11G11B10 quantization are not asserted equal"},
        "sourceEvidence": source,
        "coverageGaps": [
            "The exact compiled shader permutation is not recorded by DumpGPU; structured importance sampling is evidenced by captured CVars, the non-null ray-info binding and a valid packed RayInfo, not by a permutation bit.",
            "NumGlobalSDFClipmaps is 0 in the captured view; the cache call is replayed, not the preceding voxel trace.",
            "The GPU cache-only shader-local subtotal is not independently captured; the depth output is compared with the captured TraceHit word.",
            "The selected query is reconstructed from captured compacted-trace state by a declared rule, not read from a per-thread shader register.",
        ],
        "previews": {"neighborhood": "figures/world-cache-neighborhood.png",
                     "taps": "figures/world-cache-taps.png",
                     "probeGrid": "figures/world-cache-probe-grid.png",
                     "metrics": "figures/world-cache-preview-metrics.json"},
    }
    (out / "world-cache-query.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"lane": lane, "probe": witness["screenProbeIndex"],
                      "clipmap": result["selectedClipmap"], "mip": result["mipLevel"],
                      "taps": result["radianceTapCount"], "neighbors": len(result["neighbors"]),
                      "weights": result["fractionalWeights"],
                      "depth": replayed, "captured": captured_distance,
                      "radiance": result["cacheRadiance"],
                      "out": str(out / "world-cache-query.json")}, indent=1))


if __name__ == "__main__":
    main()
