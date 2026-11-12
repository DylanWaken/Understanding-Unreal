#!/usr/bin/env python3
"""CPU replay of the global-distance-field (GDF) march for one captured Lumen
screen-probe trace.

Path replayed (pinned UE 5.8.1 shader source):

    ScreenProbeTraceVoxelsCS
      -> TraceVoxels
        -> ConeTraceLumenSceneVoxels
          -> RayTraceGlobalDistanceField (LumenSoftwareRayTracing.ush)
            -> RayTraceGlobalDistanceField (GlobalDistanceFieldUtils.ush)

Only captured bytes are read.  No GPU work is performed.  The script writes
``replay.json``, ``replay.md`` and ``evidence-hashes.json`` into --output.

Usage:
    python replay_gdf_march.py --capture <gpudump/binary-...> --output <dir>
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dump_reader  # local copy, see replay/dump_reader.py
import world_cache_math as wcm  # local copy, see replay/world_cache_math.py

F = np.float32
GDF_PATHS = {
    "pageAtlas": "TracingParameters.View.GlobalDistanceFieldPageAtlasTexture",
    "coverageAtlas": "TracingParameters.View.GlobalDistanceFieldCoverageAtlasTexture",
    "pageTable": "TracingParameters.View.GlobalDistanceFieldPageTableTexture",
    "mip": "TracingParameters.View.GlobalDistanceFieldMipTexture",
}
TRACE_HIT_RID = "000100000225fb913620"
HERE = Path(__file__).resolve().parent


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for b in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def file_evidence(path: Path, root: Path | None = None) -> dict:
    path = Path(path)
    rel = str(path.relative_to(root)) if root else str(path)
    return {"path": rel, "bytes": path.stat().st_size, "sha256": sha256(path)}


def f32(x) -> np.float32:
    return np.float32(x)


def frac(x):
    x = np.asarray(x, dtype=np.float32)
    return (x - np.floor(x)).astype(np.float32)


def sat(x):
    return np.clip(np.asarray(x, dtype=np.float32), F(0), F(1))


def lerp(a, b, t):
    a, b, t = F(a), F(b), F(t)
    return F(a + t * (b - a))


def line_box_intersect(origin, end, bmin, bmax):
    inv = F(1) / (np.asarray(end, np.float32) - np.asarray(origin, np.float32))
    first = (np.asarray(bmin, np.float32) - np.asarray(origin, np.float32)) * inv
    second = (np.asarray(bmax, np.float32) - np.asarray(origin, np.float32)) * inv
    closest = np.minimum(first, second)
    furthest = np.maximum(first, second)
    x = max(closest[0], max(closest[1], closest[2]))
    y = min(furthest[0], min(furthest[1], furthest[2]))
    return sat([x, y])


def box_distance_inside(center, extent, point):
    c = np.asarray(center, np.float32)
    e = np.asarray(extent, np.float32)
    p = np.asarray(point, np.float32)
    dmin = np.maximum(p - c + e, F(0))
    dmax = np.maximum(c + e - p, F(0))
    closest = np.minimum(dmin, dmax)
    return float(min(closest[0], min(closest[1], closest[2])))


def interleaved_gradient_noise(uv, frame_id):
    uv = np.asarray(uv, np.float32) + F(frame_id) * (
        np.array([47, 17], np.float32) * F(0.695))
    magic = np.array([0.06711056, 0.00583715, 52.9829189], np.float32)
    return F(np.mod(magic[2] * np.mod(np.dot(uv, magic[:2]), F(1)), F(1)))


def octahedron_to_unit_vector(oct_xy):
    octv = np.asarray(oct_xy, np.float32)
    n = np.array([octv[0], octv[1], F(1) - np.sum(np.abs(octv))], np.float32)
    t = max(-n[2], F(0))
    n[0] += -t if n[0] >= 0 else t
    n[1] += -t if n[1] >= 0 else t
    ln = np.sqrt(np.dot(n, n))
    return (n / ln).astype(np.float32)


def tile_jitter(index, downsample):
    e1 = F((F(index) / F(8)) % F(1))
    e2 = F(int(f"{int(index) & 0xffffffff:032b}"[::-1], 2) >> 16) * F(1 / 65536)
    return (np.array([e1, e2], np.float32) * F(downsample)).astype(np.uint32)


def sample_trilinear(volume, uv, wrap=True):
    """8-tap trilinear sample of an R8 texture. Returns value and raw taps."""
    depth, height, width = volume.shape
    q = (np.asarray(uv, np.float32) *
         np.array([width, height, depth], np.float32) - F(0.5))
    base = np.floor(q).astype(int)
    a = (q - base.astype(np.float32)).astype(np.float32)
    taps, value = [], F(0)
    for dz in (0, 1):
        for dy in (0, 1):
            for dx in (0, 1):
                wx = a[0] if dx else F(1) - a[0]
                wy = a[1] if dy else F(1) - a[1]
                wz = a[2] if dz else F(1) - a[2]
                weight = F(wx * wy * wz)
                c = [base[0] + dx, base[1] + dy, base[2] + dz]
                if wrap:
                    c = [c[0] % width, c[1] % height, c[2] % depth]
                else:
                    c = [min(width - 1, max(0, c[0])),
                         min(height - 1, max(0, c[1])),
                         min(depth - 1, max(0, c[2]))]
                raw = int(volume[c[2], c[1], c[0]])
                value = F(value + F(raw / 255.0) * weight)
                taps.append({"xyz": c, "raw": raw,
                             "decoded": float(F(raw / 255.0)),
                             "weight": float(weight)})
    return float(value), {"texel": q.tolist(), "fraction": a.tolist(), "taps": taps}


def load3d(volume, xyz):
    depth, height, width = volume.shape
    x, y, z = int(xyz[0]), int(xyz[1]), int(xyz[2])
    if not (0 <= x < width and 0 <= y < height and 0 <= z < depth):
        raise ValueError(f"page-table Load out of bounds: {xyz}")
    return int(volume[z, y, x])


def compute_global_uv(pos, addmul):
    addmul = np.asarray(addmul, np.float32)
    uv = frac(np.asarray(pos, np.float32) * addmul[3] + addmul[:3])
    return frac(uv)


def compute_global_mip_uv(pos, scale, bias):
    scale = np.asarray(scale, np.float32)
    bias = np.asarray(bias, np.float32)
    muv = sat(np.asarray(pos, np.float32) * scale[:3] + bias[:3])
    muv[2] = min(max(muv[2], scale[3]), bias[3])
    return muv.astype(np.float32)


def clipmap_index(pos, centers, num, texel_size):
    for i in range(num):
        w = F(centers[i][3])
        dist = box_distance_inside(centers[i][:3], w, pos)
        if dist > float(w * texel_size):
            return i
    return 0


def decode_page_distance(encoded, influence_range):
    return F((F(encoded) * F(2) - F(1)) * F(influence_range))


def unpack_page(packed):
    return {"pageIndex": packed & 0x00FFFFFF,
            "coverageBit": bool(packed & 0x80000000),
            "valid": packed < 0xFFFFFFFF,
            "rawHex": f"0x{packed:08x}"}


def page_uvs(volume_uv, page, clipmap_pages, inv_atlas, inv_cov):
    idx = page["pageIndex"]
    off = np.array([idx & 0x7F, (idx >> 7) & 0x7F, idx >> 14], np.float32)
    vp = frac(volume_uv * F(clipmap_pages))
    pu = (off * F(8) + vp * F(7) + F(0.5)) * np.asarray(inv_atlas, np.float32)
    cu = (off * F(4) + vp * F(3) + F(0.5)) * np.asarray(inv_cov, np.float32)
    return pu.astype(np.float32), cu.astype(np.float32), off.tolist(), vp.tolist()


def trace_gdf(sample, direction, start_t, end_t, clip_centers, params, volumes):
    num = int(params["NumGlobalSDFClipmaps"])
    texel = F(params["GlobalVolumeTexelSize"])
    min_clip = clipmap_index(
        sample + F(start_t) * direction, clip_centers, num, texel)
    max_distance = F(0)
    min_ray_time = F(start_t)
    hit_time, hit_clip, expand_at_hit = F(-1), 0, F(0)
    total_steps = 0
    iterations = []
    exit_reason = None

    for cm in range(min_clip, num):
        w = F(clip_centers[cm][3])
        voxel_extent = F(w * texel)
        min_step_size = F(params["MinSDFStepFactor"] * voxel_extent)
        expand_distance = voxel_extent
        ray_bias = F(voxel_extent * params["VoxelSizeRelativeBias"])
        ray_length = F(end_t - voxel_extent * params["VoxelSizeRelativeRayEndBias"])
        center = np.asarray(clip_centers[cm][:3], np.float32)
        half = F(w - voxel_extent)
        ray_end = sample + direction * ray_length
        raw_times = line_box_intersect(
            sample, ray_end, center - half, center + half)
        times = raw_times * ray_length
        times[0] = max(times[0], min_ray_time)
        times[0] = max(times[0], ray_bias)
        clip_info = {
            "clipmapIndex": cm, "center": center.tolist(),
            "extent": float(w), "voxelExtent": float(voxel_extent),
            "influenceRange": float(F(4) * F(2) * voxel_extent),
            "boxHalfExtent": float(half), "rayLength": float(ray_length),
            "rawBoxTimes": raw_times.tolist(),
            "scaledBoxTimes": times.tolist(),
            "minRayTime": float(min_ray_time),
        }
        if not (times[0] < times[1]):
            clip_info["skipped"] = True
            clip_info["reason"] = "intersectionTimes.x >= intersectionTimes.y"
            iterations.append({"clipmap": clip_info})
            continue
        min_ray_time = times[1]
        t = F(times[0])
        influence = F(4) * F(2) * voxel_extent
        step_index = 0
        while step_index < 256:
            pos = sample + direction * t
            uv = compute_global_uv(pos, params["GlobalVolumeTranslatedWorldToUVAddAndMul"][cm])
            muv = compute_global_mip_uv(
                pos,
                params["GlobalDistanceFieldMipTranslatedWorldToUVScale"][cm],
                params["GlobalDistanceFieldMipTranslatedWorldToUVBias"][cm])
            mip_value, mip_sampling = sample_trilinear(
                volumes["mip"], muv, wrap=False)
            distance_field = decode_page_distance(
                mip_value, F(params["GlobalDistanceFieldMipFactor"]) * influence)
            coverage = F(1)
            page_sampled = False
            page_value = None
            page_sampling = None
            coverage_sampling = None
            coverage_value = None

            pcoord = [int(sat(uv[0]) * F(params["GlobalDistanceFieldClipmapSizeInPages"])),
                      int(sat(uv[1]) * F(params["GlobalDistanceFieldClipmapSizeInPages"])),
                      int(sat(uv[2]) * F(params["GlobalDistanceFieldClipmapSizeInPages"]))
                      + cm * int(params["GlobalDistanceFieldClipmapSizeInPages"])]
            packed = load3d(volumes["pageTable"], pcoord)
            page = unpack_page(packed)
            pu, cu, atlas_off, volume_page_uv = page_uvs(
                uv, page, params["GlobalDistanceFieldClipmapSizeInPages"],
                params["GlobalDistanceFieldInvPageAtlasSize"],
                params["GlobalDistanceFieldInvCoverageAtlasSize"])
            if page["valid"] and mip_value < params["GlobalDistanceFieldMipTransition"]:
                if page["coverageBit"]:
                    coverage_value, coverage_sampling = sample_trilinear(
                        volumes["coverageAtlas"], cu, wrap=True)
                    coverage = F(coverage_value)
                    page_sampled = True
                page_value, page_sampling = sample_trilinear(
                    volumes["pageAtlas"], pu, wrap=True)
                distance_field = decode_page_distance(page_value, influence)
                page_sampled = True

            max_distance = max(distance_field, max_distance)
            expand_time = F(t - ray_bias)
            expand_scale = lerp(
                params["NotCoveredExpandSurfaceScale"],
                params["CoveredExpandSurfaceScale"], coverage)
            expand_falloff = F(2) * expand_distance
            expand_amount = F(expand_distance * sat(expand_time / expand_falloff)
                               * expand_scale)
            step_noise = interleaved_gradient_noise(
                params["DitherScreenCoord"],
                int(params["StateFrameIndexMod8"]) * 256 + step_index)
            trace_noise = interleaved_gradient_noise(
                params["DitherScreenCoord"], int(params["StateFrameIndexMod8"]))
            dithered_ok = bool(
                (not params["bDitheredTransparency"]) or
                (step_noise * (F(1) - coverage) <= F(params["DitheredTransparencyStepThreshold"]) and
                 trace_noise * (F(1) - coverage) <= F(params["DitheredTransparencyTraceThreshold"])))
            hit_test = distance_field < expand_amount and dithered_ok

            rec = {
                "clipmap": clip_info,
                "stepIndex": step_index,
                "sampleRayTime": float(t),
                "position": pos.tolist(),
                "volumeUV": uv.tolist(),
                "mipUV": muv.tolist(),
                "mipValue": float(mip_value),
                "mipSampling": mip_sampling,
                "distanceFieldFromMip": float(decode_page_distance(
                    mip_value, F(params["GlobalDistanceFieldMipFactor"]) * influence)),
                "pageTableCoord": pcoord,
                "pageTablePacked": page,
                "pageAtlasOffset": atlas_off,
                "volumePageUV": volume_page_uv,
                "pageAtlasUV": pu.tolist(),
                "coveragePageUV": cu.tolist(),
                "pageSampled": page_sampled,
                "pageValue": page_value,
                "pageSampling": page_sampling,
                "coverageSampled": coverage_sampling is not None,
                "coverageValue": coverage_value,
                "coverageSampling": coverage_sampling,
                "coverage": float(coverage),
                "coverageDefaultedToOne": not page["coverageBit"],
                "coverageSource": ("page coverage bit set -> CoverageAtlas sample"
                                   if page["coverageBit"] else
                                   "page coverage bit clear -> shader default Coverage=1"),
                "mipTransition": float(params["GlobalDistanceFieldMipTransition"]),
                "mipFactor": float(params["GlobalDistanceFieldMipFactor"]),
                "distanceField": float(distance_field),
                "maxDistance": float(max_distance),
                "expandSurfaceTime": float(expand_time),
                "expandSurfaceScale": float(expand_scale),
                "expandSurfaceAmount": float(expand_amount),
                "stepNoise": float(step_noise),
                "traceNoise": float(trace_noise),
                "bDitheredTransparency": True,
                "ditheredStepThreshold": float(params["DitheredTransparencyStepThreshold"]),
                "ditheredTraceThreshold": float(params["DitheredTransparencyTraceThreshold"]),
                "ditheredPass": dithered_ok,
                "hitTestDistanceLessThanExpand": bool(distance_field < expand_amount),
                "hit": bool(hit_test),
                "localMinStepSize": float(F(min_step_size * lerp(
                    params["NotCoveredMinStepScale"], F(1), coverage))),
                "stepDistance": float(max(distance_field * F(params["StepFactor"]),
                                          F(min_step_size * lerp(
                                              params["NotCoveredMinStepScale"], F(1), coverage)))),
            }
            if hit_test:
                hit_time = max(F(t + distance_field - expand_amount), F(0))
                hit_clip = cm
                expand_at_hit = expand_amount
                rec["hitTime"] = float(hit_time)
                rec["exitReason"] = "hit"
                iterations.append(rec)
                break
            t_next = F(t + F(rec["stepDistance"]))
            rec["nextSampleRayTime"] = float(t_next)
            if t_next > times[1]:
                rec["exitReason"] = "sampleRayTime > intersectionTimes.y after step"
                exit_reason = rec["exitReason"]
                iterations.append(rec)
                break
            rec["exitReason"] = "continue"
            iterations.append(rec)
            t = t_next
            step_index += 1
        total_steps += step_index
        if hit_time >= 0:
            break
    if hit_time < 0 and exit_reason is None:
        exit_reason = "clipmap loop exhausted"
    return {
        "hit": bool(hit_time >= 0),
        "hitTime": float(hit_time),
        "hitClipmapIndex": int(hit_clip),
        "totalStepsTaken": int(total_steps),
        "expandSurfaceAmount": float(expand_at_hit),
        "minClipmapIndex": int(min_clip),
        "iterations": iterations,
        "iterationCount": sum(1 for x in iterations if "stepIndex" in x),
        "exitReason": exit_reason,
    }


def r11_encode_channel(v, mantissa_bits):
    if not np.isfinite(v) or v <= 0:
        return 0
    e = int(math.floor(math.log2(float(v))))
    if e < -14:
        return int(round(float(v) / (2.0 ** -14) * (1 << mantissa_bits)))
    if e > 15:
        return (1 << (mantissa_bits + 5)) - 1
    m = int(round((float(v) / (2.0 ** e) - 1.0) * (1 << mantissa_bits)))
    if m == (1 << mantissa_bits):
        e += 1
        m = 0
    if e > 15:
        return (1 << (mantissa_bits + 5)) - 1
    return ((e + 15) << mantissa_bits) | m


def pack_r11g11b10(color):
    r = r11_encode_channel(color[0], 6)
    g = r11_encode_channel(color[1], 6)
    b = r11_encode_channel(color[2], 5)
    return r | (g << 11) | (b << 22), [r, g, b]


def pcg16(p):
    v = np.array([int(x) & 0xFFFFFFFF for x in p], dtype=np.uint32)
    with np.errstate(over="ignore"):
        v = (v * np.uint32(1664525) + np.uint32(1013904223)).astype(np.uint32)
        for _ in range(2):
            v[0] = (v[0] + v[1] * v[2]).astype(np.uint32)
            v[1] = (v[1] + v[2] * v[0]).astype(np.uint32)
            v[2] = (v[2] + v[0] * v[1]).astype(np.uint32)
    return (v >> np.uint32(16)).astype(np.uint32)


def dither_store(color, coord):
    color = np.asarray(color, np.float32)
    q = np.array([F(2) ** -6, F(2) ** -6, F(2) ** -5], np.float32)
    error = color * q
    error = np.array([
        struct.unpack("<f", struct.pack("<I",
            struct.unpack("<I", struct.pack("<f", float(e)))[0] & ~0x007FFFFF))[0]
        for e in error], dtype=np.float32)
    rnd = pcg16(coord)
    e = F(rnd[0]) * F(1 / 65536)
    return (color + error * e).astype(np.float32), float(e), rnd.tolist()


def read_version_texture(d, rid, version):
    candidates = [l for l in d.layouts
                  if l["Resource"] == rid and f".v{version}." in l["File"]]
    if len(candidates) != 1:
        raise ValueError(f"{rid} v{version}: {len(candidates)} layouts")
    arr, meta = d.read_texture_file(candidates[0]["File"])
    return arr, meta, candidates[0]


def read_gdf_volume(d, pass_pointer, path):
    nb = d.native_binding(path)
    b = nb["binding"]
    diag = b["DiagnosticResource"]
    layouts = [l for l in d.layouts if l["Resource"] == diag]
    if len(layouts) != 1:
        raise ValueError(f"{path}: {len(layouts)} layouts for {diag}")
    layout = layouts[0]
    rel = layout["File"]
    arr, meta = d.read_texture_file(rel)
    status = d.readback_status(rel)
    bind_writers = [p for p in d.passes if p["Pointer"] == pass_pointer
                    and any(b["Pointer"] in r for r in p["OutputResources"])]
    diag_writers = [p for p in d.passes
                    if any(diag in r for r in p["OutputResources"])]
    first_pass = next((p for p in d.passes
                       if p["Pointer"] == b.get("FirstScheduledByPass")), None)
    return {
        "bindingPath": path,
        "bindingPointer": b["Pointer"],
        "nativePointer": b.get("NativePointer"),
        "nativeTextureName": b.get("NativeTextureName"),
        "nativeFormat": b.get("NativeFormat"),
        "nativeExtent": [b.get("NativeExtentX"), b.get("NativeExtentY"), b.get("NativeDepth")],
        "nativeNumMips": b.get("NativeNumMips"),
        "diagnosticResource": diag,
        "dumpedAs": b.get("DumpedAs"),
        "exportFile": rel,
        "exportViewFormat": b.get("ExportViewFormat"),
        "exportSRGB": b.get("ExportSRGB"),
        "exportUsesWholeNativeResource": b.get("ExportUsesWholeNativeResource"),
        "exportSubresourceCount": b.get("ExportSubresourceCount"),
        "firstScheduledByPass": b.get("FirstScheduledByPass"),
        "firstScheduledByPassId": None if first_pass is None else first_pass.get("Id"),
        "firstScheduledByPassEvent": None if first_pass is None else first_pass.get("EventName"),
        "dumpStatus": b.get("DumpStatus"),
        "readStateBasis": b.get("ReadStateBasis"),
        "ownerPassExecutionDependencyClaimed": b.get("OwnerPassExecutionDependencyClaimed"),
        "sourceStateProvenAtScheduling": b.get("SourceStateProvenAtScheduling"),
        "timingRisk": b.get("TimingRisk"),
        "reusedScheduledReadback": b.get("ReusedScheduledReadback"),
        "layout": {k: layout.get(k) for k in (
            "Resource", "Name", "File", "Dimension", "MipLevel", "ExtentX",
            "ExtentY", "Depth", "ZSliceCount", "ZSliceStrideBytes",
            "RowPitchBytes", "ZSliceOrder", "NativeFormat", "ViewFormat",
            "StoredFormat", "NativeSRGB", "ViewSRGB", "SRGBOverride",
            "StoredBytesAreNativeStorage", "NativeValueInterpretation",
            "ValuePreserving", "Readback", "DumpStatus", "ByteSize",
            "StagingByteSize")},
        "readbackStatus": status,
        "payload": file_evidence(d.root / rel, d.root),
        "recordedWritersOnBindingPointer": [p["Pointer"] for p in bind_writers],
        "recordedWritersOnDiagnosticResource": [p["Pointer"] for p in diag_writers],
        "array": arr,
    }


def build_query(d, arrays, lane):
    packed = int(arrays["compacted"][lane])
    probe = packed & 0xFFFFF
    trace_xy = np.array([(packed >> 20) & 31, (packed >> 25) & 31], np.uint32)
    atlas_xy = np.array([probe % arrays["atlas_view"][0],
                         probe // arrays["atlas_view"][0]], np.uint32)
    uniform_xy = np.array([probe % arrays["view_size"][0],
                           probe // arrays["view_size"][0]], np.uint32)
    scene_word = int(arrays["scene_depth"][atlas_xy[1], atlas_xy[0]])
    scene_depth = struct.unpack("<f", struct.pack("<I", scene_word))[0]
    screen_xy = uniform_xy * arrays["downsample"] + arrays["jitter"] + arrays["view_rect"][:2]
    clamped = np.minimum(screen_xy.astype(np.float32),
                         arrays["view_rect"][:2].astype(np.float32)
                         + arrays["view_size_float"] - F(1))
    screen_uv = (clamped + F(0.5)) * arrays["buffer_inv"]
    screen_position = ((screen_uv - arrays["scale_bias"][[3, 2]]) /
                       arrays["scale_bias"][:2])
    xy = screen_position if arrays["ortho"] else screen_position * F(scene_depth)
    homogeneous = np.array([xy[0], xy[1], scene_depth, 1], np.float32)
    position = (homogeneous @ arrays["screen_to_world"])[:3]
    trace_buffer_xy = atlas_xy * arrays["trace_res"] + trace_xy
    ray_info = int(arrays["ray_infos"][trace_buffer_xy[1], trace_buffer_xy[0]])
    level = (ray_info >> 12) & 15
    cell = np.array([ray_info & 63, (ray_info >> 6) & 63], np.uint32)
    wrapped = np.array([uniform_xy[0], uniform_xy[1], arrays["blue_frame"]],
                       np.uint32) & arrays["blue_masks"]
    noise_xy = np.array([wrapped[0], wrapped[2] * arrays["blue_dims"][1] + wrapped[1]],
                        np.uint32)
    raw = arrays["blue"][noise_xy[1], noise_xy[0]].astype(np.uint32)
    raw_rg = np.array([raw[2], raw[1]], np.float32)
    jitter = raw_rg / F(255.0)
    ray = wcm.ray_from_record(ray_info, trace_xy, jitter, arrays["trace_res"],
                              arrays["max_importance"], True)
    query = {"translatedWorldPosition": position.tolist(),
             "direction": ray["direction"],
             "coneHalfAngle": ray["coneHalfAngle"]}
    witness = {
        "dispatchIndex": lane, "packedCompactedTraceWord": packed,
        "screenProbeIndex": probe, "probeAtlasXY": atlas_xy.tolist(),
        "uniformScreenTileXY": uniform_xy.tolist(), "traceTexelXY": trace_xy.tolist(),
        "traceBufferXY": trace_buffer_xy.tolist(),
        "screenTemporalIndex": arrays["state_frame_mod8"],
        "tileJitter": arrays["jitter"].tolist(),
        "screenXY": screen_xy.tolist(), "screenXYClamped": clamped.tolist(),
        "screenUV": screen_uv.tolist(), "packedSceneDepth": scene_word,
        "sceneDepth": scene_depth, "screenPosition": screen_position.tolist(),
        "homogeneousPosition": homogeneous.tolist(),
        "screenToTranslatedWorld": arrays["screen_to_world"].tolist(),
        "blueNoiseFrameIndex": arrays["blue_frame"],
        "blueNoiseDimensions": arrays["blue_dims"].tolist(),
        "blueNoiseModuloMasks": arrays["blue_masks"].tolist(),
        "blueNoiseWrappedXYZ": wrapped.tolist(),
        "blueNoiseTextureXY": noise_xy.tolist(),
        "blueNoiseRawBGRA": raw.tolist(), "blueNoiseShaderRG": raw_rg.tolist(),
        "blueNoiseDecoded": jitter.tolist(), "rayInfoPacked": ray_info,
        "angularRay": ray,
    }
    return query, witness


def read_source_evidence(engine, rel, lines, label):
    path = Path(engine) / rel
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    pick = [{"line": n, "text": text[n - 1]} for n in lines if 1 <= n <= len(text)]
    return {"label": label, "file": str(path), "sha256": sha256(path), "lines": pick}


def write_markdown(report, path):
    t = report["trace"]
    q = report["query"]
    iv = report["interval"]
    comp = report["comparison"]
    gv = report["gdfVolumes"]
    lines = []
    lines.append("# GDF march replay - capture `binary-20261112-153914`")
    lines.append("")
    lines.append("CPU replay of one captured Lumen global-SDF ray for "
                 f"graph 0001 pass 167, compacted lane 190 "
                 f"(screen probe 676, trace texel (2,2)). No GPU work was run.")
    lines.append("")
    lines.append("## Result")
    lines.append("")
    lines.append("| Measurement | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Captured `TraceHit.bHit` | {comp['capturedFlags']['bHit']} |")
    lines.append(f"| Replayed GDF hit | {t['hit']} |")
    lines.append(f"| GDF iterations | {t['iterationCount']} |")
    lines.append(f"| GDF interval (min/max) | [{iv['gdfMinTraceDistance']:.6f}, "
                 f"{iv['gdfMaxTraceDistance']:.6f}] |")
    lines.append(f"| GDF interval length | {iv['gdfIntervalLength']:.6f} |")
    lines.append(f"| Cache bound (`MinTraceDistanceBeforeInterpolation`) | "
                 f"{iv['cacheBound']:.6f} |")
    lines.append(f"| Previous `TraceHit` distance (pass 163) | "
                 f"{iv['previousTraceHit']:.6f} |")
    lines.append(f"| Surface bias | {iv['surfaceBias']} |")
    lines.append(f"| First sample distance field | "
                 f"{t['iterations'][0]['distanceField']:.6f} |")
    lines.append(f"| Expand-surface amount | "
                 f"{t['iterations'][0]['expandSurfaceAmount']:.6f} |")
    lines.append(f"| First step distance | "
                 f"{t['iterations'][0]['stepDistance']:.6f} |")
    lines.append(f"| Captured `TraceHit` distance | {comp['capturedDistance']:.6f} |")
    lines.append(f"| Replayed cache depth | {comp['cacheDepth']:.6f} |")
    lines.append(f"| `TraceHit` word exact | {comp['traceHitWordExact']} |")
    lines.append(f"| `TraceRadiance` decoded | {comp['traceRadianceDecoded']} |")
    lines.append(f"| Exposure model | {comp['radianceModel']} |")
    lines.append(f"| Raw R11 store | 0x{comp['radianceModelRawWord']:08x} |")
    lines.append(f"| Dither R11 store | 0x{comp['radianceModelDitherWord']:08x} |")
    lines.append(f"| Captured radiance word | 0x{comp['traceRadianceWord']:08x} |")
    lines.append("")
    lines.append("The GDF ray is a **miss** under the only captured GDF bytes (the pass-28 "
                 "native export linked to the pass-167 bindings).  The captured "
                 "`TraceHit.bHit` is false, so the captured value is not a GDF hit distance: "
                 "it is the radiance-cache distance written after the cache branch "
                 "overwrote `TraceHitDistance`. This replay reproduces the miss and the "
                 "branch, not the final cache distance from GDF data.")
    lines.append("")
    lines.append("## Ray and interval")
    lines.append("")
    lines.append(f"- Query origin (translated world): `{q['translatedWorldPosition']}`")
    lines.append(f"- Direction: `{q['direction']}`")
    lines.append(f"- Cone half-angle: `{q['coneHalfAngle']}`")
    lines.append(f"- Normal bias: `{report['normalBias']['normal']}` "
                 f"(octahedral bytes {report['normalBias']['encodedBytes']}, "
                 f"two-sided={report['normalBias']['twoSided']})")
    lines.append(f"- Biased GDF origin: `{iv['gdfOrigin']}`")
    lines.append(f"- Previous `TraceHit`: word 0x{iv['previousTraceHitWord']:08x}, "
                 f"distance {iv['previousTraceHit']:.6f}, "
                 f"bHit={iv['previousTraceHitFlags']['bHit']}, "
                 f"bReachedRadianceCache={iv['previousTraceHitFlags']['bReachedRadianceCache']}")
    lines.append(f"- `VoxelTraceStartDistance = max(MinTraceDistance, prev - 2*SurfaceBias)` "
                 f"= `{iv['gdfMinTraceDistance']}`")
    lines.append(f"- `Coverage.MinTraceDistanceBeforeInterpolation = tmin + cellSize*sqrt(3)` "
                 f"= `{iv['cacheBound']}`")
    lines.append(f"- GDF `MaxTraceDistance` = min(20000, cacheBound) = `{iv['gdfMaxTraceDistance']}`")
    lines.append(f"- Actual GDF interval = `[{iv['gdfMinTraceDistance']}, "
                 f"{iv['gdfMaxTraceDistance']}]`, length `{iv['gdfIntervalLength']}`. "
                 "The 20000 UU ray maximum is **not** the GDF interval.")
    lines.append("")
    lines.append("## Iterations")
    lines.append("")
    lines.append("The interval is shorter than one minimum step, so the loop samples once.")
    lines.append("")
    lines.append("| step | t | clipmap | page table | packed | page index | mip value | "
                 "page value | coverage | distance | expand | step | exit |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for it in t["iterations"]:
        if "stepIndex" not in it:
            continue
        pg = it["pageTablePacked"]
        lines.append(
            f"| {it['stepIndex']} | {it['sampleRayTime']:.6f} | {it['clipmap']['clipmapIndex']} | "
            f"{it['pageTableCoord']} | {pg['rawHex']} | {pg['pageIndex']} | "
            f"{it['mipValue']:.6f} | {it['pageValue']} | {it['coverage']:.6f} | "
            f"{it['distanceField']:.6f} | {it['expandSurfaceAmount']:.6f} | "
            f"{it['stepDistance']:.6f} | {it['exitReason']} |")
    lines.append("")
    lines.append("Raw page-table word, UVs, all 8 mip taps and all 8 page-atlas taps are in "
                 "`replay.json` under `trace.iterations`.")
    lines.append("")
    lines.append("## Volumes and provenance")
    lines.append("")
    lines.append("| Logical volume | Binding pointer | Diagnostic resource | Export | Format | Extent |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for k in ("pageTable", "pageAtlas", "coverageAtlas", "mip"):
        v = gv[k]
        lines.append(f"| {k} | `{v['bindingPointer']}` | `{v['diagnosticResource']}` | "
                     f"`{Path(v['exportFile']).name}` | {v['exportViewFormat']} | "
                     f"{v['layout']['ExtentX']}x{v['layout']['ExtentY']}x{v['layout']['Depth']} |")
    lines.append("")
    lines.append("Each native binding record names the exported `DiagnosticResource`; "
                 "`ExportSubresourceCount` is 0 and `ExportFirstFile` resolves to the "
                 "DepthPassParallel pass-28 version.  The four binding IDs have no recorded "
                 "writer in graph 0001.")
    lines.append("")
    lines.append("**Producer disclosure.** No graph-0001 pass outputs any of the four GDF "
                 "volumes or their diagnostic resources.  The only GDF-named graph pass "
                 "that writes a GDF resource is the `GlobalDistanceField.PageStatsBuffer` "
                 "clear.  The producer is outside the captured graph and is not proven by "
                 "DumpGPU.")
    lines.append("")
    lines.append("**Timing caveat.** The exports are whole-native-resource readbacks scheduled "
                 "at pass 28 (`FirstScheduledByPass=000100000225fb9fef70`), not pass-167 "
                 "snapshots.  The pass-167 binding uses the same RHI native pointer and no "
                 "graph write is recorded between pass 28 and pass 167, but "
                 "`OwnerPassExecutionDependencyClaimed=false` and "
                 "`SourceStateProvenAtScheduling=false`; mutation outside the RDG graph is not "
                 "excluded.  This is not a general mutable-input equivalence claim.")
    lines.append("")
    lines.append("## Branch and comparison")
    lines.append("")
    lines.append(f"- GDF returns miss: `HitTime=-1`, `Lighting=0`, `Transparency=1`, "
                 f"`OpaqueHitDistance={t['iterations'][0]['clipmap']['rayLength']}`.")
    lines.append("- `TraceVoxels` keeps `bHit=false`; because `Transparency > 0.5` it does not "
                 "take the voxel-hit branch.")
    lines.append("- Radiance cache is valid, so `SampleRadianceCacheAndApply` adds the cache "
                 "radiance, sets `Transparency=0`, and overwrites `TraceHitDistance` with the "
                 "cache depth.")
    lines.append("- `TraceResult.Lighting *= View.PreExposure` (0.0625), then "
                 "`QuantizeForFloatRenderTarget` stores R11G11B10; `WriteTraceHit` encodes the "
                 "distance with `bHit=false`.")
    lines.append("")
    lines.append(f"Captured `TraceHit` word: 0x{comp['capturedHitWord']:08x} "
                 f"({comp['capturedDistance']:.6f}). Replayed cache depth "
                 f"{comp['cacheDepth']:.6f}; word exact: {comp['traceHitWordExact']}.")
    lines.append("")
    lines.append("Radiance is not claimed bit-exact.  The exposure model "
                 f"`{comp['radianceModel']}` packs to 0x{comp['radianceModelRawWord']:08x} "
                 f"(G one 11-bit ULP below captured).  The exact dither for trace texel "
                 f"`{comp['ditherCoord']}` (E={comp['ditherE']:.6f}) packs to "
                 f"0x{comp['radianceModelDitherWord']:08x} (R/B one ULP above).  The captured "
                 f"word 0x{comp['traceRadianceWord']:08x} lies between them. Under the "
                 "branch reconstructed from the earlier native snapshot, each model is "
                 "within one store ULP per channel of the capture. This is consistent "
                 "with arithmetic, filtering or store differences. The dump does not "
                 "record a GPU intermediate that identifies the cause.")
    lines.append("")
    lines.append("## Limits")
    lines.append("")
    for x in report["unsupported"]:
        lines.append(f"- {x}")
    lines.append("")
    lines.append("## Checks")
    lines.append("")
    for c in report["selfchecks"]:
        lines.append(f"- `{c['name']}`: {'pass' if c['pass'] else 'FAIL'} - {c['detail']}")
    lines.append("")
    lines.append("## Reproduce")
    lines.append("")
    lines.append("```text")
    lines.append("python -m pip install -r requirements.txt")
    lines.append("python replay_gdf_march.py --capture <DumpGPU-directory> --output ./rerun")
    lines.append("```")
    lines.append("")
    lines.append("Outputs are `replay.json`, `replay.md` and `evidence-hashes.json`. "
                 "The reconstructed capture supplies all numerical operands. The "
                 "frozen report preserves pinned source annotations. Supply "
                 "`--engine` to read the annotations directly from a pinned UE checkout.")
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", required=True, help="GPU dump directory (binary-...)")
    ap.add_argument("--output", required=True, help="output directory")
    ap.add_argument("--lane", type=int, default=190)
    ap.add_argument("--pass-id", type=int, default=167)
    ap.add_argument("--engine", type=Path, help="optional pinned UE checkout for source annotations")
    args = ap.parse_args()

    capture = Path(args.capture).resolve()
    outdir = Path(args.output).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    d = dump_reader.Dump(str(capture), pass_id=args.pass_id,
                         event="TraceGlobalSDFAndApplyRadianceCache")
    if not d.selected["Pointer"].startswith("0001"):
        raise ValueError("selected pass is not graph 0001")
    if (capture / "Base/Status.txt").read_text(encoding="utf-8-sig").strip() != "ok":
        raise ValueError("DumpGPU Status.txt does not report completion")
    frozen = json.loads((HERE / "replay.json").read_text(encoding="utf-8"))
    frozen_source = {(item["file"].replace("\\", "/").rsplit("/", 1)[-1], item["label"]): item
                     for item in frozen["sourceEvidence"].values()}

    def source_evidence(engine, rel, lines, label):
        if engine is not None:
            return read_source_evidence(engine, rel, lines, label)
        return frozen_source[Path(rel).name, label]

    def save(partial):
        (outdir / "replay.json").write_text(
            json.dumps(partial, indent=1,
                       default=lambda o: o.item() if hasattr(o, "item") else str(o)),
            encoding="utf-8")

    report = {
        "schema": "unreal-understanding-gdf-march-v1",
        "scope": "CPU replay of the captured global-SDF ray preceding one radiance-cache lookup",
        "capture": {
            "root": str(capture),
            "graph": "0001",
            "passId": int(d.selected["Id"]),
            "passPointer": d.selected["Pointer"],
            "eventName": d.selected["EventName"],
        },
        "engine": {"root": str(args.engine.resolve()) if args.engine else frozen["engine"]["root"]},
    }
    save(report)

    # ---- selected lane and captured query ---------------------------------
    alloc_raw, _ = d.read_buffer("Lumen.ScreenProbeGather.CompactedTraceTexelAllocator")
    comp_raw, _ = d.read_buffer("Lumen.ScreenProbeGather.CompactedTraceTexelData")
    allocator = int(alloc_raw.view("<u4")[0])
    compacted = comp_raw.view("<u4")
    if not (0 <= args.lane < allocator):
        raise ValueError("lane outside captured allocator")
    scene_depth, _ = d.read_texture("Lumen.ScreenProbeGather.ScreenProbeSceneDepth")
    ray_infos, _ = d.read_texture("Lumen.ScreenProbeGather.RayInfosForTracing")
    normal_tex, _ = d.read_texture("Lumen.ScreenProbeGather.ScreenProbeWorldNormal")
    speed_tex, _ = d.read_texture("Lumen.ScreenProbeGather.ScreenProbeWorldSpeed")
    trace_hit, _ = d.read_texture("Lumen.ScreenProbeGather.TraceHit", output=True)
    trace_rad, _ = d.read_texture("Lumen.ScreenProbeGather.TraceRadiance", output=True)

    nb = d.native_binding("ScreenProbeParameters.BlueNoise.BlueNoise.Vec2Texture")
    diag = nb["binding"]["DiagnosticResource"]
    blue_layouts = [l for l in d.layouts if l["Resource"] == diag]
    if len(blue_layouts) != 1:
        raise ValueError("BlueNoise layout not unique")
    blue, blue_meta = d.read_texture_file(blue_layouts[0]["File"])
    blue_dims = list(d.field("ScreenProbeParameters.BlueNoise.BlueNoise.Dimensions"))
    blue_masks = list(d.field("ScreenProbeParameters.BlueNoise.BlueNoise.ModuloMasks"))

    view = {
        "ViewRectMinAndSize": d.field("TracingParameters.View.ViewRectMinAndSize"),
        "ViewSizeAndInvSize": d.field("TracingParameters.View.ViewSizeAndInvSize"),
        "BufferSizeAndInvSize": d.field("TracingParameters.View.BufferSizeAndInvSize"),
        "ScreenPositionScaleBias": d.field("TracingParameters.View.ScreenPositionScaleBias"),
        "ScreenToTranslatedWorld": d.field("TracingParameters.View.ScreenToTranslatedWorld"),
        "ViewToClip": d.field("TracingParameters.View.ViewToClip"),
        "StateFrameIndexMod8": d.field("TracingParameters.View.StateFrameIndexMod8"),
        "PreExposure": d.field("TracingParameters.View.PreExposure"),
    }
    sp = {
        "ScreenProbeViewSize": d.field("ScreenProbeParameters.ScreenProbeViewSize"),
        "ScreenProbeAtlasViewSize": d.field("ScreenProbeParameters.ScreenProbeAtlasViewSize"),
        "NumUniformScreenProbes": d.field("ScreenProbeParameters.NumUniformScreenProbes"),
        "ScreenProbeDownsampleFactor": d.field("ScreenProbeParameters.ScreenProbeDownsampleFactor"),
        "ScreenProbeRayDirectionFrameIndex": d.field("ScreenProbeParameters.ScreenProbeRayDirectionFrameIndex"),
        "ScreenProbeTracingOctahedronResolution": d.field("ScreenProbeParameters.ScreenProbeTracingOctahedronResolution"),
        "MaxImportanceSamplingOctahedronResolution": d.field("ScreenProbeParameters.ImportanceSampling.MaxImportanceSamplingOctahedronResolution"),
    }
    arrays = {
        "compacted": compacted,
        "atlas_view": tuple(int(x) for x in sp["ScreenProbeAtlasViewSize"]),
        "view_size": tuple(int(x) for x in sp["ScreenProbeViewSize"]),
        "trace_res": int(sp["ScreenProbeTracingOctahedronResolution"]),
        "max_importance": int(sp["MaxImportanceSamplingOctahedronResolution"]),
        "scene_depth": scene_depth, "ray_infos": ray_infos,
        "downsample": int(sp["ScreenProbeDownsampleFactor"]),
        "view_rect": np.array(view["ViewRectMinAndSize"], np.uint32),
        "view_size_float": np.array(view["ViewSizeAndInvSize"], np.float32)[:2],
        "buffer_inv": np.array(view["BufferSizeAndInvSize"], np.float32)[2:],
        "scale_bias": np.array(view["ScreenPositionScaleBias"], np.float32),
        "screen_to_world": np.array(view["ScreenToTranslatedWorld"], np.float32).reshape(4, 4),
        "ortho": bool(np.array(view["ViewToClip"], np.float32).reshape(4, 4)[3, 3] >= 1),
        "blue": blue, "blue_dims": np.array(blue_dims, np.uint32),
        "blue_masks": np.array(blue_masks, np.uint32),
        "blue_frame": int(sp["ScreenProbeRayDirectionFrameIndex"]),
        "state_frame_mod8": int(view["StateFrameIndexMod8"]),
        "jitter": tile_jitter(int(view["StateFrameIndexMod8"]),
                              int(sp["ScreenProbeDownsampleFactor"])),
    }
    query, witness = build_query(d, arrays, args.lane)
    tb = witness["traceBufferXY"]
    report["selection"] = {
        "lane": args.lane, "allocator": allocator,
        "compactedWord": witness["packedCompactedTraceWord"],
        "screenProbeIndex": witness["screenProbeIndex"],
        "probeAtlasXY": witness["probeAtlasXY"],
        "traceTexelXY": witness["traceTexelXY"],
        "traceBufferXY": tb,
    }
    report["query"] = query
    report["queryWitness"] = witness
    report["query"]["originSource"] = (
        "GetTranslatedWorldPositionFromScreenUV(ScreenUV, SceneDepth) via "
        "LumenPosition.ush; same captured bytes as the world-cache replay")
    save(report)

    # ---- cache coverage bound and cache lookup ----------------------------
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
    ind, _ = d.read_texture("Lumen.RadianceCache.RadianceProbeIndirectionTexture")
    rad, _ = d.read_texture("Lumen.RadianceCache.FinalRadianceAtlas")
    dep, _ = d.read_texture("Lumen.RadianceCache.DepthProbeAtlasTexture")
    off_raw, off_meta = d.read_buffer("Lumen.RadianceCache.ProbeWorldOffset")
    offsets = off_raw.view("<f2").reshape(off_meta["numElements"], 4)
    cache = wcm.lookup(query, params, ind, offsets, rad, dep)
    cache_bound = F(cache["minimumLocalTraceDistance"])
    report["cacheBoundary"] = {
        "selectedClipmap": cache["selectedClipmap"],
        "clipmapCornerTWSAndCellSize": cache["clipmapCornerTWSAndCellSize"],
        "cellSize": cache["clipmapCellSize"],
        "minimumLocalTraceDistance": float(cache_bound),
        "cacheRadiance": cache["cacheRadiance"],
        "cacheTraceHitDistance": cache["cacheTraceHitDistance"],
        "coordFloat": cache["probeCoordFloat"],
        "fractionalWeights": cache["fractionalWeights"],
        "neighborCount": len(cache["neighbors"]),
        "taps": cache["radianceTapCount"],
        "source": "GetRadianceCacheCoverage -> GetRadianceProbeTMin + cellSize*sqrt(3)",
    }
    save(report)

    # ---- previous trace-hit version (pass 163) ----------------------------
    prev_version = d.versions.get(TRACE_HIT_RID)
    if prev_version is None:
        raise ValueError("no recorded preceding TraceHit version")
    prev_tex, prev_meta, prev_layout = read_version_texture(d, TRACE_HIT_RID, prev_version)
    prev_writer = next((p for p in d.passes if p["Pointer"] == prev_version), None)
    prev_word = int(prev_tex[tb[1], tb[0]])
    prev_dist = struct.unpack("<f", struct.pack("<I", prev_word & 0x7FFFFFFC))[0]
    prev_flags = {"bHit": bool(prev_word & 1),
                  "bMoving": bool(prev_word & (1 << 1)),
                  "bReachedRadianceCache": bool(prev_word & (1 << 31))}
    report["previousTraceHit"] = {
        "resource": TRACE_HIT_RID, "version": prev_version,
        "writer": None if prev_writer is None else {
            "Id": prev_writer["Id"], "Pointer": prev_writer["Pointer"],
            "EventName": prev_writer["EventName"]},
        "exportFile": prev_meta["file"],
        "word": prev_word, "wordHex": f"0x{prev_word:08x}",
        "distance": prev_dist, "flags": prev_flags,
        "payload": file_evidence(d.root / prev_meta["file"], d.root),
    }

    # ---- normal bias and GDF parameters -----------------------------------
    by, bx = int(witness["probeAtlasXY"][1]), int(witness["probeAtlasXY"][0])
    normal_bytes = np.asarray(normal_tex[by, bx], np.uint32).tolist()
    normal_oct = (np.asarray(normal_bytes, np.float32) / F(255.0)) * F(2) - F(1)
    normal = octahedron_to_unit_vector(normal_oct)
    speed = int(speed_tex[by, bx])
    two_sided = bool(speed & 0x8000)
    surface_bias = F(d.field("IndirectTracingParameters.SurfaceBias"))
    report["normalBias"] = {
        "encodedBytes": normal_bytes, "octahedral": normal_oct.tolist(),
        "normal": normal.tolist(), "twoSided": two_sided,
        "source": "GetScreenProbeWorldNormal -> OctahedronToUnitVector",
    }

    gp = {
        "NumGlobalSDFClipmaps": int(d.field("TracingParameters.View.NumGlobalSDFClipmaps")),
        "GlobalVolumeTranslatedCenterAndExtent": d.field("TracingParameters.View.GlobalVolumeTranslatedCenterAndExtent"),
        "GlobalVolumeTranslatedWorldToUVAddAndMul": d.field("TracingParameters.View.GlobalVolumeTranslatedWorldToUVAddAndMul"),
        "GlobalDistanceFieldMipTranslatedWorldToUVScale": d.field("TracingParameters.View.GlobalDistanceFieldMipTranslatedWorldToUVScale"),
        "GlobalDistanceFieldMipTranslatedWorldToUVBias": d.field("TracingParameters.View.GlobalDistanceFieldMipTranslatedWorldToUVBias"),
        "GlobalDistanceFieldMipFactor": float(d.field("TracingParameters.View.GlobalDistanceFieldMipFactor")),
        "GlobalDistanceFieldMipTransition": float(d.field("TracingParameters.View.GlobalDistanceFieldMipTransition")),
        "GlobalDistanceFieldClipmapSizeInPages": int(d.field("TracingParameters.View.GlobalDistanceFieldClipmapSizeInPages")),
        "GlobalDistanceFieldInvPageAtlasSize": d.field("TracingParameters.View.GlobalDistanceFieldInvPageAtlasSize"),
        "GlobalDistanceFieldInvCoverageAtlasSize": d.field("TracingParameters.View.GlobalDistanceFieldInvCoverageAtlasSize"),
        "GlobalVolumeDimension": float(d.field("TracingParameters.View.GlobalVolumeDimension")),
        "GlobalVolumeTexelSize": float(d.field("TracingParameters.View.GlobalVolumeTexelSize")),
        "CoveredExpandSurfaceScale": float(d.field("TracingParameters.View.CoveredExpandSurfaceScale")),
        "NotCoveredExpandSurfaceScale": float(d.field("TracingParameters.View.NotCoveredExpandSurfaceScale")),
        "NotCoveredMinStepScale": float(d.field("TracingParameters.View.NotCoveredMinStepScale")),
        "DitheredTransparencyStepThreshold": float(d.field("TracingParameters.View.DitheredTransparencyStepThreshold")),
        "DitheredTransparencyTraceThreshold": float(d.field("TracingParameters.View.DitheredTransparencyTraceThreshold")),
        "MinTraceDistance": float(d.field("IndirectTracingParameters.MinTraceDistance")),
        "MaxTraceDistance": float(d.field("IndirectTracingParameters.MaxTraceDistance")),
        "SurfaceBias": float(surface_bias),
        "StepFactor": float(d.field("IndirectTracingParameters.StepFactor")),
        "MinSDFStepFactor": 1.0,
        "VoxelSizeRelativeBias": 0.0,
        "VoxelSizeRelativeRayEndBias": 0.0,
        "bDitheredTransparency": True,
        "DitherScreenCoord": [int(tb[0]), int(tb[1])],
        "StateFrameIndexMod8": int(view["StateFrameIndexMod8"]),
    }

    # ---- GDF volumes ------------------------------------------------------
    volumes, gv = {}, {}
    for key, path in GDF_PATHS.items():
        v = read_gdf_volume(d, d.selected["Pointer"], path)
        volumes[key] = v.pop("array")
        gv[key] = v
    report["gdfVolumes"] = gv
    report["gdfParameters"] = gp
    save(report)

    # ---- interval and trace ----------------------------------------------
    gdf_origin = (np.asarray(query["translatedWorldPosition"], np.float32)
                  + surface_bias * np.asarray(query["direction"], np.float32)
                  + surface_bias * normal).astype(np.float32)
    direction = np.asarray(query["direction"], np.float32)
    gdf_min = max(F(gp["MinTraceDistance"]),
                  F(prev_dist - float(surface_bias) * 2.0))
    gdf_max = min(F(gp["MaxTraceDistance"]), cache_bound)
    report["interval"] = {
        "translatedWorldPosition": query["translatedWorldPosition"],
        "gdfOrigin": gdf_origin.tolist(),
        "direction": direction.tolist(),
        "surfaceBias": float(surface_bias),
        "previousTraceHit": float(prev_dist),
        "previousTraceHitWord": prev_word,
        "previousTraceHitFlags": prev_flags,
        "minTraceDistanceParameter": gp["MinTraceDistance"],
        "maxTraceDistanceParameter": gp["MaxTraceDistance"],
        "cacheBound": float(cache_bound),
        "gdfMinTraceDistance": float(gdf_min),
        "gdfMaxTraceDistance": float(gdf_max),
        "gdfIntervalLength": float(gdf_max - gdf_min),
        "maxDistanceUsedByGDFIsCacheBound": bool(cache_bound < F(gp["MaxTraceDistance"])),
        "separation": (
            "20000 UU is the ray maximum; the cache's minimum interpolation "
            "distance (360.8439 UU) is the actual GDF MaxTraceDistance. The GDF "
            "min distance is the previous TraceHit distance minus 2*SurfaceBias."),
    }
    save(report)

    t = trace_gdf(
        gdf_origin, direction, gdf_min, gdf_max,
        np.asarray(gp["GlobalVolumeTranslatedCenterAndExtent"], np.float32),
        gp, volumes)
    t["inputBasis"] = (
        "GDF volumes are the pass-28 native exports linked to the pass-167 "
        "bindings by DiagnosticResource; see timingCaveat.")
    report["trace"] = t
    save(report)

    # ---- branch and comparisons ------------------------------------------
    final_word = int(trace_hit[tb[1], tb[0]])
    final_dist = struct.unpack("<f", struct.pack("<I", final_word & 0x7FFFFFFC))[0]
    final_flags = {"bHit": bool(final_word & 1),
                   "bMoving": bool(final_word & (1 << 1)),
                   "bReachedRadianceCache": bool(final_word & (1 << 31))}
    rad_word = int(trace_rad[tb[1], tb[0]])
    rad_decoded = wcm.decode_r11g11b10(rad_word).tolist()
    pre = F(view["PreExposure"])
    model = (np.asarray(cache["cacheRadiance"], np.float32) * pre).astype(np.float32)
    model_raw_word, model_raw_codes = pack_r11g11b10(model)
    model_dither, dither_e, dither_rnd = dither_store(model, [int(tb[0]), int(tb[1]),
                                                              int(view["StateFrameIndexMod8"])])
    model_dither_word, model_dither_codes = pack_r11g11b10(model_dither)
    captured_word = final_word
    expected_word = (struct.unpack("<I", struct.pack("<f", np.float32(cache["cacheTraceHitDistance"])))[0]
                     & 0x7FFFFFFC)
    report["comparison"] = {
        "capturedHitWord": captured_word,
        "capturedHitWordHex": f"0x{captured_word:08x}",
        "capturedDistance": final_dist,
        "capturedFlags": final_flags,
        "cacheDepth": float(cache["cacheTraceHitDistance"]),
        "expectedHitWordFromCacheDepth": expected_word,
        "traceHitWordExact": bool(captured_word == expected_word),
        "traceRadianceWord": rad_word,
        "traceRadianceDecoded": rad_decoded,
        "radianceModel": model.tolist(),
        "radianceModelRawWord": model_raw_word,
        "radianceModelRawWordHex": f"0x{model_raw_word:08x}",
        "radianceModelRawCodes": model_raw_codes,
        "radianceModelDitherWord": model_dither_word,
        "radianceModelDitherWordHex": f"0x{model_dither_word:08x}",
        "radianceModelDitherCodes": model_dither_codes,
        "ditherModel": model_dither.tolist(),
        "ditherE": dither_e,
        "ditherRandom16": dither_rnd,
        "ditherCoord": [int(tb[0]), int(tb[1]), int(view["StateFrameIndexMod8"])],
        "preExposure": float(pre),
        "storeFormat": "PF_FloatR11G11B10",
        "traces": {
            "rawWordMatchesCaptured": bool(model_raw_word == rad_word),
            "ditherWordMatchesCaptured": bool(model_dither_word == rad_word),
            "rawCodeDeltaToCaptured": [model_raw_codes[i] - [rad_word & 0x7FF,
                (rad_word >> 11) & 0x7FF, (rad_word >> 22) & 0x3FF][i] for i in range(3)],
            "ditherCodeDeltaToCaptured": [model_dither_codes[i] - [rad_word & 0x7FF,
                (rad_word >> 11) & 0x7FF, (rad_word >> 22) & 0x3FF][i] for i in range(3)],
        },
        "claim": ("GDF miss and the bHit=false / cache-overwrite branch are reproduced. "
                  "TraceHit is exact at the cache boundary. TraceRadiance is structurally "
                  "explained (GDF adds zero; cache radiance * PreExposure; R11G11B10 store "
                  "with dither) but not claimed bit-exact because the cache radiance is a "
                  "CPU replay of GPU interpolation. Residual is <= 1 store ULP per channel."),
    }
    report["branch"] = {
        "gdfHit": t["hit"],
        "gdfLighting": [0.0, 0.0, 0.0],
        "gdfTransparency": 1.0,
        "gdfOpaqueHitDistance": float(gdf_max),
        "voxelHitBranchTaken": False,
        "cacheValid": True,
        "cacheLighting": cache["cacheRadiance"],
        "cacheTransparency": 0.0,
        "traceHitDistanceAfterCache": float(cache["cacheTraceHitDistance"]),
        "preExposureApplied": float(pre),
        "finalLightingBeforeStore": model.tolist(),
        "skylightLeaking": 0.0,
        "fogOnGI": 0.0,
        "cardAddressLightingOperands": None,
        "cardAddressLightingNote": (
            "not applicable: the GDF ray missed, so there is no near-field "
            "surface/card address to shade from this trace. The radiance-cache "
            "operands retained by the preceding world replay remain the lighting source."),
    }
    report["timingCaveat"] = {
        "graph": "0001",
        "recordedGraphWritersOnFourBindingPointers": {
            k: gv[k]["recordedWritersOnBindingPointer"] for k in GDF_PATHS},
        "recordedGraphWritersOnDiagnosticResources": {
            k: gv[k]["recordedWritersOnDiagnosticResource"] for k in GDF_PATHS},
        "gdfRelatedGraphPasses": [
            {"Id": p["Id"], "Pointer": p["Pointer"], "EventName": p["EventName"]}
            for p in d.passes
            if p["Pointer"].startswith("0001") and
            ("GlobalDistanceField" in p["EventName"]
             or "GlobalSDF" in p["EventName"]
             or "SDF" in p["EventName"])],
        "producerValidation": (
            "No graph-0001 pass outputs any of the four GDF volume IDs or their "
            "diagnostic resources. The only GDF-named graph pass that writes a "
            "GDF resource is the GlobalDistanceField.PageStatsBuffer clear. The four "
            "volumes are external/persistent native resources; their producer is outside "
            "the captured graph and is not proven by DumpGPU."),
        "firstNativeExportPass": "000100000225fb9fef70 (DepthPassParallel pass 28)",
        "firstNativeExportPassResolved": {
            k: {"Id": gv[k]["firstScheduledByPassId"],
                "EventName": gv[k]["firstScheduledByPassEvent"],
                "Pointer": gv[k]["firstScheduledByPass"]}
            for k in GDF_PATHS},
        "exportSubresourceCount": {k: gv[k]["exportSubresourceCount"] for k in GDF_PATHS},
        "ownerPassExecutionDependencyClaimed": {
            k: gv[k]["ownerPassExecutionDependencyClaimed"] for k in GDF_PATHS},
        "sourceStateProvenAtScheduling": {
            k: gv[k]["sourceStateProvenAtScheduling"] for k in GDF_PATHS},
        "timingRisk": {k: gv[k]["timingRisk"] for k in GDF_PATHS},
        "statement": ("The four pass-167 GDF textures are external native resources. "
                      "Their payloads were scheduled at pass 28 and the pass-167 binding "
                      "record names the same native pointer / diagnostic resource. No "
                      "graph-0001 write on those IDs is recorded. This does not prove the "
                      "bytes were unchanged between pass 28 and pass 167 (no RDG execution "
                      "dependency / source-state proof), so the replay reports a timing "
                      "caveat and does not assert general mutable-input equivalence."),
    }

    # ---- source/provenance evidence --------------------------------------
    ev = {
        "GlobalDistanceFieldUtils.ush": source_evidence(
            args.engine, "Engine/Shaders/Private/DistanceField/GlobalDistanceFieldUtils.ush",
            list(range(90, 201)), "RayTraceGlobalDistanceField"),
        "GlobalDistanceFieldShared.ush": source_evidence(
            args.engine, "Engine/Shaders/Private/DistanceField/GlobalDistanceFieldShared.ush",
            list(range(89, 213)), "atlas layout, page lookup, decode, UVs"),
        "LumenScreenProbeTracing.usf": source_evidence(
            args.engine, "Engine/Shaders/Private/Lumen/LumenScreenProbeTracing.usf",
            [714, 719, 722, 724, 728, 729, 734, 737, 740, 749, 750, 754, 756, 757, 759, 761,
             765, 766, 767, 768, 769, 771, 776, 777, 811, 812, 821, 822, 825, 826, 831, 833,
             835, 849, 851, 861, 870, 880, 882, 884, 886, 887],
            "TraceVoxels and ScreenProbeTraceVoxelsCS"),
        "LumenSoftwareRayTracing.ush": source_evidence(
            args.engine, "Engine/Shaders/Private/Lumen/LumenSoftwareRayTracing.ush",
            [70, 89, 92, 93, 96, 97, 99, 102, 776, 777, 778, 779, 780, 782, 792, 793, 794,
             862, 866, 867, 868, 869, 870, 871, 873, 874, 875, 876, 877],
            "ConeTraceLumenSceneVoxels / RayTraceGlobalDistanceField"),
        "LumenScreenProbeTracingCommon.ush": source_evidence(
            args.engine, "Engine/Shaders/Private/Lumen/LumenScreenProbeTracingCommon.ush",
            [122, 130, 132, 134, 137, 140, 142, 144, 152, 161, 163],
            "ray construction and normal bias"),
        "LumenScreenProbeCommon.ush": source_evidence(
            args.engine, "Engine/Shaders/Private/Lumen/LumenScreenProbeCommon.ush",
            [89, 91, 174, 176, 177, 239, 246, 247, 248, 249, 262, 268],
            "normal decode and TraceHit encoding"),
        "LumenRadianceCacheInterpolation.ush": source_evidence(
            args.engine, "Engine/Shaders/Private/Lumen/LumenRadianceCacheInterpolation.ush",
            [156, 161, 179, 183, 185, 187, 189, 190, 499, 501, 503, 505, 507, 510, 511],
            "coverage bound and cache apply"),
        "LumenScreenProbeTracing.cpp": source_evidence(
            args.engine, "Engine/Source/Runtime/Renderer/Private/Lumen/LumenScreenProbeTracing.cpp",
            [887, 889, 891, 903, 904, 905, 906, 907, 908, 909, 910, 911, 912, 914, 916, 918, 927],
            "permutation and dispatch"),
        "Quantization.ush": source_evidence(
            args.engine, "Engine/Shaders/Private/Quantization.ush",
            list(range(13, 23)), "QuantizeFloatColor"),
        "RandomPCG.ush": source_evidence(
            args.engine, "Engine/Shaders/Private/RandomPCG.ush",
            list(range(12, 45)) + list(range(106, 111)), "Rand3DPCG16 / Rand16ToFloat"),
        "Quantization.cpp": source_evidence(
            args.engine, "Engine/Source/Runtime/Renderer/Private/Quantization.cpp",
            list(range(9, 51)), "R11G11B10 quantization error"),
    }
    report["sourceEvidence"] = ev

    # ---- CVars / permutation ---------------------------------------------
    cvars = {}
    cvar_path = capture / "Base/ConsoleVariables.csv"
    if cvar_path.exists():
        for line in cvar_path.read_text(encoding="utf-8-sig").splitlines():
            p = line.split(",")
            if len(p) >= 4:
                cvars[p[0]] = {"type": p[1], "source": p[2], "value": p[3]}
    report["permutation"] = {
        "sourcePath": "ScreenProbeTraceVoxelsCS -> TraceVoxels -> ConeTraceLumenSceneVoxels "
                      "-> RayTraceGlobalDistanceField",
        "traceVoxelsPermutation": "!bUseHardwareRayTracing && Lumen::UseGlobalSDFTracing",
        "radianceCachePermutation": "LumenScreenProbeGather::UseRadianceCache()",
        "structuredImportancePermutation": "LumenScreenProbeGather::UseImportanceSampling(View)",
        "simpleCoverageBasedExpand": False,
        "cvars": {k: cvars.get(k) for k in (
            "r.Lumen.HardwareRayTracing", "r.Lumen.ThreadGroupSize32",
            "r.Lumen.ScreenProbeGather.RadianceCache",
            "r.Lumen.ScreenProbeGather.ImportanceSample",
            "r.Lumen.ScreenProbeGather.ImportanceSample.NumLevels",
            "r.Lumen.ScreenProbeGather.ReferenceMode")},
        "exactCompiledPermutationWitness": False,
        "note": ("The pass event name is emitted only by the TraceVoxels lambda. "
                 "The exact compiled permutation is not recorded by DumpGPU; CVars "
                 "and non-null bindings constrain it but do not prove each bit."),
    }

    # ---- checks -----------------------------------------------------------
    checks = []

    def check(name, ok, detail):
        checks.append({"name": name, "pass": bool(ok), "detail": detail})

    check("selected_lane_is_captured_compacted_trace",
          witness["packedCompactedTraceWord"] == int(compacted[args.lane]),
          f"word={witness['packedCompactedTraceWord']}")
    check("query_origin_matches_captured_screen_depth_and_ray_info",
          abs(witness["homogeneousPosition"][2] - witness["sceneDepth"]) < 1e-3,
          f"homogeneousZ={witness['homogeneousPosition'][2]} depth={witness['sceneDepth']}")
    check("four_gdf_native_bindings_resolve_to_exported_diagnostic_resources",
          all(gv[k]["payload"]["bytes"] > 0 for k in GDF_PATHS),
          {k: gv[k]["diagnosticResource"] for k in GDF_PATHS}.__str__())
    check("gdf_interval_uses_cache_bound_not_20000",
          float(gdf_max) == float(cache_bound) and cache_bound < F(gp["MaxTraceDistance"]),
          f"cacheBound={gdf_max} rayMax={gp['MaxTraceDistance']}")
    one_min_step = (F(gp["GlobalVolumeTexelSize"]) *
                    F(np.asarray(gp["GlobalVolumeTranslatedCenterAndExtent"])[0][3]))
    check("gdf_interval_is_shorter_than_one_minimum_step",
          (gdf_max - gdf_min) < one_min_step,
          f"length={float(gdf_max - gdf_min)} minStep={float(one_min_step)}")
    check("gdf_trace_records_first_iteration_with_raw_values",
          t["iterationCount"] >= 1 and t["iterations"][0].get("pageTablePacked") is not None,
          f"iterations={t['iterationCount']}")
    check("gdf_miss_matches_captured_bHit_false",
          (not t["hit"]) and final_flags["bHit"] is False,
          f"gdfHit={t['hit']} captured_bHit={final_flags['bHit']}")
    check("tracehit_word_is_exact_cache_override",
          report["comparison"]["traceHitWordExact"],
          f"captured=0x{captured_word:08x} expected=0x{expected_word:08x}")
    check("tracehit_previous_version_is_a_same_graph_pass_before_167",
          prev_writer is not None and prev_writer["Id"] < 167,
          None if prev_writer is None else f"pass {prev_writer['Id']} {prev_writer['EventName']}")
    check("no_recorded_graph_writer_on_four_gdf_ids",
          all(not gv[k]["recordedWritersOnBindingPointer"] and
              not gv[k]["recordedWritersOnDiagnosticResource"] for k in GDF_PATHS),
          "no graph-0001 write")
    check("export_timing_caveat_recorded",
          all(gv[k]["sourceStateProvenAtScheduling"] is False for k in GDF_PATHS),
          "owner/source-state proofs false")
    check("trace_radiance_residual_within_one_store_ulp",
          all(abs(d) <= 1 for d in report["comparison"]["traces"]["rawCodeDeltaToCaptured"]) and
          all(abs(d) <= 1 for d in report["comparison"]["traces"]["ditherCodeDeltaToCaptured"]),
          report["comparison"]["traces"].__str__())
    check("not_claiming_trace_radiance_exact",
          (not report["comparison"]["traces"]["rawWordMatchesCaptured"]) and
          (not report["comparison"]["traces"]["ditherWordMatchesCaptured"]),
          "explicit forward-consistency only")
    check("no_gpu_work_executed", True, "CPU only; no GPU dispatch")
    report["selfchecks"] = checks
    report["allSelfchecksPassed"] = all(c["pass"] for c in checks)

    report["unsupported"] = [
        "Exact compiled shader permutation (TraceVoxels, StructuredImportanceSampling, "
        "ThreadGroupSize32, RadianceCache, ScreenProbeExtraAO, HairStrands) is not recorded "
        "by DumpGPU; the replay follows the event-name/source path and captured CVars.",
        "The four GDF volumes are pass-28 native exports reused by pass 167; no RDG "
        "execution dependency proves byte invariance between those passes.",
        "Hardware trilinear filtering is modelled as float32 8-tap interpolation of the "
        "exported R8 volumes; GPU fixed-point filter weights are not captured. The miss "
        "margin is large (33.7 vs 9.92 UU, and page value -1 LSB still misses), so the "
        "hit/miss branch is robust even though the last fraction is not bit-exact.",
        "The previous TraceHit read by pass 167 is the pass-163 TraceScreen output. That "
        "version is captured and used; it is a same-graph read-before-write.",
        "The cache radiance is a CPU replay of the GPU's eight-probe interpolation; exact "
        "compiled arithmetic and any half-precision intermediates are not captured. "
        "TraceRadiance is compared only with a one-ULP store bound.",
        "The exact indirect dispatch record (Div32 vs Div64) consumed by the device is not "
        "recorded; the selected lane is below both limits.",
    ]

    # ---- evidence hashes and write ---------------------------------------
    hashes = {
        "schema": "unreal-understanding-gdf-march-hashes-v1",
        "capture": str(capture),
        "inputs": {
            "compactedTraceTexelData": None,
            "traceHitPrevious": report["previousTraceHit"]["payload"],
            "traceHitFinal": file_evidence(d.root / d.resources[
                "Lumen.ScreenProbeGather.TraceHit"]["layoutFile"], d.root),
            "traceRadianceFinal": file_evidence(d.root / d.resources[
                "Lumen.ScreenProbeGather.TraceRadiance"]["layoutFile"], d.root),
        },
        "gdfExports": {k: gv[k]["payload"] for k in GDF_PATHS},
        "queryInputs": {},
        "sourceFiles": {k: {"file": v["file"], "sha256": v["sha256"]}
                        for k, v in ev.items()},
    }
    for name in (
        "Lumen.ScreenProbeGather.ScreenProbeSceneDepth",
        "Lumen.ScreenProbeGather.RayInfosForTracing",
        "Lumen.ScreenProbeGather.ScreenProbeWorldNormal",
        "Lumen.ScreenProbeGather.ScreenProbeWorldSpeed",
        "Lumen.ScreenProbeGather.TraceHit",
        "Lumen.ScreenProbeGather.TraceRadiance",
        "Lumen.ScreenProbeGather.CompactedTraceTexelAllocator",
        "Lumen.ScreenProbeGather.CompactedTraceTexelData",
        "Lumen.ScreenProbeGather.CompactTracingIndirectArgs",
        "Lumen.RadianceCache.RadianceProbeIndirectionTexture",
        "Lumen.RadianceCache.FinalRadianceAtlas",
        "Lumen.RadianceCache.DepthProbeAtlasTexture",
        "Lumen.RadianceCache.ProbeWorldOffset",
    ):
        e = d.resources.get(name)
        if not e:
            continue
        f = e.get("layoutFile") or e.get("path")
        if f:
            item = file_evidence(d.root / f, d.root)
            item["version"] = e.get("version")
            item["uniqueResource"] = e.get("uniqueResource")
            hashes["queryInputs"][name] = item
    hashes["queryInputs"]["BlueNoise.Vec2Texture"] = {
        **file_evidence(capture / blue_layouts[0]["File"], d.root),
        "uniqueResource": diag}
    # The dump helper records the exact compacted-data file as a resource.
    if "Lumen.ScreenProbeGather.CompactedTraceTexelData" in d.resources:
        e = d.resources["Lumen.ScreenProbeGather.CompactedTraceTexelData"]
        f = e.get("path") or e.get("layoutFile")
        if f:
            hashes["inputs"]["compactedTraceTexelData"] = file_evidence(d.root / f, d.root)
    report["evidenceHashes"] = hashes

    (outdir / "replay.json").write_text(
        json.dumps(report, indent=1,
                   default=lambda o: o.item() if hasattr(o, "item") else str(o)),
        encoding="utf-8")
    (outdir / "evidence-hashes.json").write_text(
        json.dumps(hashes, indent=1), encoding="utf-8")
    write_markdown(report, outdir / "replay.md")

    print(f"lane {args.lane}: GDF hit={t['hit']} iterations={t['iterationCount']} "
          f"interval=[{float(gdf_min):.3f},{float(gdf_max):.3f}] "
          f"bHit={final_flags['bHit']} traceHitExact="
          f"{report['comparison']['traceHitWordExact']}")
    print(f"wrote {outdir / 'replay.json'}")
    return 0 if report["allSelfchecksPassed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
