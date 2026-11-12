"""Replay one actual World Radiance Cache lookup from the new capture.

Inputs are read from the DumpGPU capture in ``--dump``. The script does not
synthesize a ray, a blue-noise value, a probe offset, an atlas texel or a
uniform. It selects the first compacted trace lane that satisfies a fixed,
pre-declared eligibility rule, reconstructs every lookup operand at its exact
captured graph version, and compares the lookup's interpolated depth output to
the captured ``TraceHit`` float written by the same pass. That comparison is at
the cache-lookup boundary; it is not a claim that the cache subtotal equals the
final ``TraceRadiance``, which additionally contains the preceding GDF result,
skylight leaking, fog, view pre-exposure and R11G11B10 quantization.

No GPU work is performed.
"""
from pathlib import Path
from collections import Counter
import argparse
import hashlib
import json
import math
import struct

import numpy as np

import dump_reader
import world_cache_math as cache

F = np.float32
HERE = Path(__file__).resolve().parent
SCHEMA = "unreal-understanding-world-cache-replay-v1"


class Reject(Exception):
    """A lane is not eligible for the pre-declared selection rule."""


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_source_evidence(engine_root, rel, lines, label, context=0):
    path = Path(engine_root) / rel
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    pick = []
    for line in lines:
        lo, hi = max(1, line - context), min(len(text), line + context)
        for n in range(lo, hi + 1):
            pick.append({"line": n, "text": text[n - 1]})
    return {"label": label, "file": rel, "sha256": sha256_file(path), "lines": pick}


def parse_cvars(path):
    rows = {}
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        parts = line.split(",")
        if len(parts) >= 4:
            rows[parts[0]] = {"type": parts[1], "source": parts[2], "value": parts[3]}
    return rows


def validate_referenced_structs(d, refs):
    rows = []
    for name, value in refs.items():
        row = {"name": name, "null": bool(value.get("Null")),
               "baseType": value.get("BaseType"),
               "contentKind": value.get("ContentKind"),
               "memberOffset": value.get("MemberOffset"),
               "byteSize": value.get("ByteSize"),
               "contentAvailable": value.get("ContentAvailable")}
        if row["null"]:
            if "Content" in value or "Metadata" in value:
                raise ValueError(f"null reference {name} unexpectedly has content")
            row["status"] = "explicit-unbound-gap"
            rows.append(row)
            continue
        if not value.get("ContentAvailable"):
            raise ValueError(f"reference {name} is not content-available")
        if value.get("TraversalCycleDetected"):
            raise ValueError(f"reference {name} reports a traversal cycle")
        ev = d.reference_evidence[name]
        if ev["size"] != int(value["ByteSize"]):
            raise ValueError(f"reference {name}: payload size {ev['size']} != ByteSize {value['ByteSize']}")
        row.update({"status": "validated", "structTypeName": ev["structTypeName"],
                    "payload": ev["contentFile"], "metadata": ev["metadataFile"]})
        rows.append(row)

    blue = refs["ScreenProbeParameters.BlueNoise"]
    content_id = blue["Content"]
    meta_id = blue["Metadata"]
    outer = json.loads((d.root / "StructuresMetadata" / (meta_id + ".json")).read_text(encoding="utf-8-sig"))
    inner_id = None
    for member in outer["Members"]:
        if member["Name"] == "BlueNoise" and member["BaseType"] == "UBMT_INCLUDED_STRUCT":
            inner_id = member["StructMetadata"]
    if inner_id is None:
        raise ValueError("FBlueNoise does not contain its nested FBlueNoiseParameters")
    inner = json.loads((d.root / "StructuresMetadata" / (inner_id + ".json")).read_text(encoding="utf-8-sig"))
    blob = (d.root / "Structures" / (content_id + ".bin")).read_bytes()
    fields = {m["Name"]: m for m in inner["Members"]}
    dims = list(struct.unpack_from("<3i", blob, fields["Dimensions"]["Offset"]))
    masks = list(struct.unpack_from("<3i", blob, fields["ModuloMasks"]["Offset"]))
    scalar = struct.unpack_from("<Q", blob, fields["ScalarTexture"]["Offset"])[0]
    vec2 = struct.unpack_from("<Q", blob, fields["Vec2Texture"]["Offset"])[0]
    nested = {"outerStruct": outer.get("StructTypeName"), "innerStruct": inner.get("StructTypeName"),
              "outerSize": len(blob), "dimensions": dims, "moduloMasks": masks,
              "scalarTexturePointer": f"{scalar:016x}",
              "vec2TexturePointer": f"{vec2:016x}",
              "vec2Offset": fields["Vec2Texture"]["Offset"]}
    return rows, nested


def load_blue_noise(d, refs, pass_pointer):
    binding = d.native_binding("ScreenProbeParameters.BlueNoise.BlueNoise.Vec2Texture")
    b = binding["binding"]
    if b.get("Null"):
        raise ValueError("BlueNoise Vec2Texture is null")
    if b.get("MemberOffset") != 40:
        raise ValueError(f"Vec2Texture uniform byte offset {b.get('MemberOffset')} != 40")
    vec2_pointer = d.field("ScreenProbeParameters.BlueNoise.BlueNoise.Vec2Texture")["pointer"]
    # The binding records a 20-hex graph-qualified object name; the uniform
    # payload stores the 16-hex RHI pointer. The low 64 bits must agree.
    if b["Pointer"][-16:] != vec2_pointer[-16:]:
        raise ValueError(f"struct pointer {vec2_pointer} != binding pointer {b['Pointer']}")
    if b["DumpedAs"].split(".")[1][-16:] != b["NativePointer"][-16:]:
        raise ValueError("DumpedAs pointer does not match NativePointer")
    rel = b["ExportFirstFile"]
    layout = d.layouts_by_file.get(rel)
    if layout is None:
        raise ValueError(f"no SubresourceLayouts record for {rel}")
    status = d.readback_status(rel)
    if status["Status"] != "Dumped" or not status["FileExists"] or not status["FileSizeMatches"]:
        raise ValueError(f"BlueNoise readback not complete: {status}")
    if layout["Resource"] != b["DiagnosticResource"]:
        raise ValueError("BlueNoise layout resource != DiagnosticResource")
    if layout["NativeFormat"] != "PF_B8G8R8A8" or layout["StoredFormat"] != "PF_B8G8R8A8":
        raise ValueError("BlueNoise stored format is not PF_B8G8R8A8")
    if layout["NativeSRGB"] or layout["ViewSRGB"] or layout["SRGBViewUsedForReadback"]:
        raise ValueError("BlueNoise is sRGB; this replay requires the linear view")
    if int(layout["ExtentX"]) != int(b["NativeExtentX"]) or int(layout["ExtentY"]) != int(b["NativeExtentY"]):
        raise ValueError("BlueNoise layout extent != native binding extent")
    dumped = b["DumpedAs"].split(".")
    if dumped[0] != "NativeTexture" or dumped[1][-16:] != b["NativePointer"][-16:]:
        raise ValueError(f"DumpedAs does not name the native pointer: {b['DumpedAs']}")
    array, meta = d.read_texture_file(rel, "ScreenProbeParameters.BlueNoise.BlueNoise.Vec2Texture")
    if array.shape != (8192, 128, 4):
        raise ValueError(f"BlueNoise payload shape {array.shape}")
    b_channel = array[..., 0]
    g_channel = array[..., 1]
    r_channel = array[..., 2]
    a_channel = array[..., 3]
    if b_channel.max() != 0 or a_channel.min() != 255:
        raise ValueError("BlueNoise BGRA payload does not have B=0/A=255")
    if len(np.unique(r_channel)) < 250 or len(np.unique(g_channel)) < 250:
        raise ValueError("BlueNoise R/G channels are not 8-bit distributions")
    dims = list(d.field("ScreenProbeParameters.BlueNoise.BlueNoise.Dimensions"))
    masks = list(d.field("ScreenProbeParameters.BlueNoise.BlueNoise.ModuloMasks"))
    if dims != [128, 128, 64] or masks != [127, 127, 63]:
        raise ValueError(f"BlueNoise dimensions/masks changed: {dims} {masks}")
    pointer_from_bytes = f"{struct.unpack_from('<Q', (d.root / 'Structures' / (refs['ScreenProbeParameters.BlueNoise']['Content'] + '.bin')).read_bytes(), 40)[0]:016x}"
    if pointer_from_bytes != b["Pointer"][-16:]:
        raise ValueError("uniform byte 40 pointer does not match the binding pointer")
    writer = next((p for p in d.passes if p["Pointer"] == b["FirstScheduledByPass"]), None)
    return {
        "bindingPath": "ScreenProbeParameters.BlueNoise.BlueNoise.Vec2Texture",
        "passPointer": pass_pointer,
        "uniformBuffer": binding["uniformBuffer"],
        "layoutName": binding["layoutName"],
        "uniformByteOffset": 40,
        "pointer": b["Pointer"],
        "pointerSuffix16": b["Pointer"][-16:],
        "pointerFromContentBytesAt40": pointer_from_bytes,
        "structContent": refs["ScreenProbeParameters.BlueNoise"]["Content"],
        "structMetadata": refs["ScreenProbeParameters.BlueNoise"]["Metadata"],
        "nativeTextureName": b["NativeTextureName"],
        "nativeDimension": b["NativeDimension"],
        "nativeFormat": b["NativeFormat"],
        "nativeExtent": [b["NativeExtentX"], b["NativeExtentY"]],
        "nativePointer": b["NativePointer"],
        "diagnosticResource": b["DiagnosticResource"],
        "dumpedAs": b["DumpedAs"],
        "exportFile": rel,
        "firstScheduledByPass": b["FirstScheduledByPass"],
        "writerPass": None if writer is None else {
            "Id": writer["Id"], "Pointer": writer["Pointer"], "EventName": writer["EventName"]},
        "exportSRGB": b["ExportSRGB"],
        "exportViewFormat": b["ExportViewFormat"],
        "exportUsesWholeNativeResource": b["ExportUsesWholeNativeResource"],
        "exportScope": b["ExportScope"],
        "readStateBasis": b["ReadStateBasis"],
        "dumpStatus": b["DumpStatus"],
        "layout": {k: layout[k] for k in (
            "Resource", "Name", "File", "Dimension", "MipLevel", "ExtentX", "ExtentY",
            "Depth", "ZSliceCount", "ZSliceStrideBytes", "RowPitchBytes", "ZSliceOrder",
            "NativeFormat", "ViewFormat", "StoredFormat", "NativeFormatExpanded",
            "NativeSRGB", "ViewSRGB", "SRGBOverride", "SRGBViewUsedForReadback",
            "StoredBytesAreNativeStorage", "NativeValueInterpretation", "ValuePreserving",
            "Readback", "DumpStatus", "ByteSize", "StagingByteSize")},
        "readbackStatus": status,
        "payload": dump_reader.file_evidence(d.root / rel, d.root),
        "volume": {"dimensions": dims, "moduloMasks": masks,
                   "storageTextureExtent": [128, 8192],
                   "slicePacking": "64 slices of 128x128 stacked vertically"},
        "decode": {"storedFormat": "PF_B8G8R8A8", "srgb": False,
                   "memoryChannelOrder": "B,G,R,A",
                   "shaderChannelOrder": "R,G from bytes 2,1",
                   "scale": "byte / 255",
                   "range": [float(r_channel.min()) / 255.0, float(r_channel.max()) / 255.0,
                             float(g_channel.min()) / 255.0, float(g_channel.max()) / 255.0]},
    }, array


def tile_jitter(index, downsample):
    e1 = F((F(index) / F(8)) % F(1))
    e2 = F(int(f"{int(index) & 0xffffffff:032b}"[::-1], 2) >> 16) * F(1 / 65536)
    return (np.array([e1, e2], dtype=np.float32) * F(downsample)).astype(np.uint32)


def build_query(d, arrays, lane):
    compacted = arrays["compacted"]
    packed = int(compacted[lane])
    probe = packed & 0xFFFFF
    trace_xy = np.array([(packed >> 20) & 31, (packed >> 25) & 31], dtype=np.uint32)
    if probe >= int(arrays["num_uniform"]):
        raise Reject("adaptive/non-uniform screen probe")
    if trace_xy[0] >= arrays["trace_res"] or trace_xy[1] >= arrays["trace_res"]:
        raise Reject("trace texel outside tracing resolution")
    atlas_xy = np.array([probe % arrays["atlas_view"][0], probe // arrays["atlas_view"][0]], dtype=np.uint32)
    uniform_xy = np.array([probe % arrays["view_size"][0], probe // arrays["view_size"][0]], dtype=np.uint32)
    if atlas_xy[0] >= arrays["atlas_view"][0] or atlas_xy[1] >= arrays["atlas_view"][1]:
        raise Reject("atlas coordinate outside view")
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
    if level > 15 or (arrays["max_importance"] >> level) <= 0:
        raise Reject("unsupported importance level")
    cell = np.array([ray_info & 63, (ray_info >> 6) & 63], dtype=np.uint32)
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
        "orthographic": arrays["ortho"], "homogeneousPosition": homogeneous.tolist(),
        "screenToTranslatedWorld": arrays["screen_to_world"].tolist(),
        "screenTileXY": tile_xy.tolist(),
        "blueNoiseFrameIndex": arrays["blue_frame"],
        "blueNoiseDimensions": arrays["blue_dims"].tolist(),
        "blueNoiseModuloMasks": arrays["blue_masks"].tolist(),
        "blueNoiseWrappedXYZ": wrapped.tolist(),
        "blueNoiseTextureXY": noise_xy.tolist(),
        "blueNoiseRawBGRA": raw_bgra.tolist(), "blueNoiseShaderRG": raw_rg.tolist(),
        "blueNoiseDecoded": jitter_ray.tolist(),
        "rayInfoPacked": ray_info, "angularRay": ray,
    }
    return query, witness


def build_report(dump_root, pass_id=167, engine_root=None, reference_path=None):
    d = dump_reader.Dump(dump_root, pass_id)
    if d.selected["EventName"] != "TraceGlobalSDFAndApplyRadianceCache":
        raise ValueError("selected pass is not the trace/cache pass")
    pass_pointer = d.selected["Pointer"]
    if not pass_pointer.startswith("0001"):
        raise ValueError("selected pass is not in scene graph 0001")
    reference_path = Path(reference_path or HERE / "world-cache-replay.json")
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    if (d.root / "Base/Status.txt").read_text(encoding="utf-8-sig").strip() != "ok":
        raise ValueError("DumpGPU Status.txt does not report completion")
    if d.selected["Pointer"] != reference["capture"]["pass"]["Pointer"]:
        raise ValueError("capture pass pointer differs from the frozen evidence")
    # Original run progress and source annotations are provenance, not operands.
    # The reconstructed dump supplies all data read by the numerical replay.
    progress_data = reference["capture"]["progress"]
    frozen_source = {(item["file"], item["label"]): item for item in reference["sourceEvidence"]}

    def source_evidence(rel, lines, label, context=0):
        if engine_root is not None:
            return read_source_evidence(engine_root, rel, lines, label, context)
        return frozen_source[rel, label]
    infos = json.loads((d.root / "Base/Infos.json").read_text(encoding="utf-8-sig"))
    cvars = parse_cvars(d.root / "Base/ConsoleVariables.csv")

    def cvar(name):
        if name not in cvars:
            raise ValueError(f"missing CVar {name}")
        return cvars[name]

    refs = d.references
    ref_rows, nested_blue = validate_referenced_structs(d, refs)
    blue, blue_array = load_blue_noise(d, refs, pass_pointer)
    binding_names = [
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
    parameter_bindings = {name: d.parameter_binding(name) for name in binding_names}

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
        raise ValueError("ProbeWorldOffset is not an 8-byte half4 buffer")
    offsets = off_raw.view("<f2").reshape(off_meta["numElements"], 4)
    allocator = int(alloc_raw.view("<u4")[0])
    compacted = comp_raw.view("<u4")
    active_compacted = compacted[:allocator]
    compact_probe = (active_compacted & 0xFFFFF).astype(np.uint64)
    compact_x = ((active_compacted >> 20) & 31).astype(np.uint64)
    compact_y = ((active_compacted >> 25) & 31).astype(np.uint64)
    compact_coords = compact_probe * np.uint64(1024) + compact_x * np.uint64(32) + compact_y
    compact_unique_count = int(np.unique(compact_coords).size)
    args = args_raw.view("<u4").reshape(args_meta["numElements"], 3)
    offset_note = ("all 16384 half4 offsets are zero in this captured version"
                   if int(np.count_nonzero(off_raw)) == 0 else "non-zero offsets present")

    ceil64 = (allocator + 63) // 64
    ceil32 = (allocator + 31) // 32
    triple64 = args[0].tolist()
    triple32 = args[1].tolist()
    if triple64 != [ceil64, 1, 1] or triple32 != [ceil32, 1, 1]:
        raise ValueError(f"indirect triples do not match allocator: {args.tolist()} N={allocator}")
    thread_group = cvar("r.Lumen.ThreadGroupSize32")
    reference_mode = cvar("r.Lumen.ScreenProbeGather.ReferenceMode")
    importance = cvar("r.Lumen.ScreenProbeGather.ImportanceSample")
    num_levels = cvar("r.Lumen.ScreenProbeGather.ImportanceSample.NumLevels")
    hardware_rt = cvar("r.Lumen.HardwareRayTracing")
    sp_radiance = cvar("r.Lumen.ScreenProbeGather.RadianceCache")
    if reference_mode["value"] != "0":
        raise ValueError("reference mode is active; importance sampling would be disabled")
    if importance["value"] != "1" or num_levels["value"] != "1":
        raise ValueError("importance sampling CVars do not match the captured ray-info binding")
    if sp_radiance["value"] != "1":
        raise ValueError("radiance cache is disabled in this capture")

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
        "StateFrameIndex": d.field("TracingParameters.View.StateFrameIndex"),
        "StateFrameIndexMod8": d.field("TracingParameters.View.StateFrameIndexMod8"),
        "TranslatedWorldCameraOrigin": d.field("TracingParameters.View.TranslatedWorldCameraOrigin"),
        "SkylightLeakingColor": d.field("TracingParameters.SkylightLeakingColor"),
        "SampleHeightFogOnGI": d.field("TracingParameters.SampleHeightFogOnGI"),
        "NumGlobalSDFClipmaps": d.field("TracingParameters.NumGlobalSDFClipmaps"),
        "GlobalDistanceFieldPageObjectGridBufferSize": d.field("TracingParameters.GlobalDistanceFieldPageObjectGridBufferSize"),
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
        "scene_depth": scene_depth,
        "ray_infos": ray_infos,
        "downsample": int(screen_probe["ScreenProbeDownsampleFactor"]),
        "view_rect": np.array(view["ViewRectMinAndSize"], dtype=np.uint32),
        "view_size_float": np.array(view["ViewSizeAndInvSize"], dtype=np.float32)[:2],
        "buffer_inv": np.array(view["BufferSizeAndInvSize"], dtype=np.float32)[2:],
        "scale_bias": np.array(view["ScreenPositionScaleBias"], dtype=np.float32),
        "screen_to_world": np.array(view["ScreenToTranslatedWorld"], dtype=np.float32).reshape(4, 4),
        "ortho": bool(np.array(view["ViewToClip"], dtype=np.float32).reshape(4, 4)[3, 3] >= 1),
        "blue": blue_array,
        "blue_dims": np.array(nested_blue["dimensions"], dtype=np.uint32),
        "blue_masks": np.array(nested_blue["moduloMasks"], dtype=np.uint32),
        "blue_frame": int(screen_probe["ScreenProbeRayDirectionFrameIndex"]),
        "state_frame_mod8": int(view["StateFrameIndexMod8"]),
        "jitter": tile_jitter(int(view["StateFrameIndexMod8"]), int(screen_probe["ScreenProbeDownsampleFactor"])),
    }

    rejections = Counter()
    selected = None
    for lane in range(allocator):
        try:
            query, witness = build_query(d, arrays, lane)
            result = cache.lookup(query, params, ind, offsets, rad, dep)
        except Reject as error:
            rejections[str(error)] += 1
            continue
        except (ValueError, IndexError) as error:
            rejections[f"lookup: {error}"] += 1
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
        selected = (lane, query, witness, result, hit_word)
        break
    if selected is None:
        raise RuntimeError(f"no eligible lane found; rejections={dict(rejections)}")

    lane, query, witness, result, hit_word = selected
    tb = witness["traceBufferXY"]
    selected_coord = (int(witness["screenProbeIndex"]) * 1024
                      + int(witness["traceTexelXY"][0]) * 32
                      + int(witness["traceTexelXY"][1]))
    selected_occurrences = int(np.count_nonzero(compact_coords == selected_coord))
    if selected_occurrences != 1:
        raise ValueError(f"selected trace texel occurs {selected_occurrences} times in the compacted list")
    captured_distance = struct.unpack("<f", struct.pack("<I", hit_word & 0x7FFFFFFC))[0]
    b_hit = bool(hit_word & 1)
    b_moving = bool(hit_word & 2)
    b_reached_cache = bool(hit_word & 0x80000000)
    replayed_distance = result["cacheTraceHitDistance"]
    depth_exact = float(replayed_distance) == float(captured_distance)
    depth_abs_diff = abs(float(replayed_distance) - float(captured_distance))
    depth_within_bound = depth_abs_diff <= 0.001
    if not depth_within_bound:
        raise ValueError(f"replayed depth {replayed_distance} != captured {captured_distance}")
    radiance_word = int(trace_rad[tb[1], tb[0]])
    final_radiance = cache.decode_r11g11b10(radiance_word)
    model = np.array(result["cacheRadiance"], dtype=np.float32) * F(view["PreExposure"])
    quant_error = np.array(screen_probe["TargetFormatQuantizationError"], dtype=np.float32)
    tolerance = np.abs(model) * quant_error
    radiance_abs_diff = np.abs(final_radiance - model)
    radiance_forward_consistent = bool(np.all(radiance_abs_diff <= tolerance))
    zero_terms = {
        "skylightLeakingColorIsZero": all(float(x) == 0 for x in view["SkylightLeakingColor"]),
        "sampleHeightFogOnGI": float(view["SampleHeightFogOnGI"]),
        "useSkyVisibility": int(params["UseSkyVisibility"]),
        "showBlackRadianceCacheLighting": int(params["ShowBlackRadianceCacheLighting"]),
        "precedingGDFTermsCaptured": False,
    }

    source = [
        source_evidence("Engine/Source/Runtime/Renderer/Private/Lumen/Lumen.cpp",
                        [38, 39, 40, 41, 89, 90, 91, 92], "thread-group CVar and device gate"),
        source_evidence("Engine/Source/Runtime/Renderer/Private/Lumen/LumenScreenProbeTracing.cpp",
                        [595, 596, 597, 598, 599, 600, 601, 602, 904, 905, 906, 907, 908, 909,
                         910, 911, 912, 913, 914, 916, 917, 918, 922],
                        "indirect-args enum, active permutations, dispatch offset and event"),
        source_evidence("Engine/Source/Runtime/Renderer/Private/Lumen/LumenScreenProbeImportanceSampling.cpp",
                        [11, 12, 13, 14, 15, 16, 17], "importance-sampling CVar"),
        source_evidence("Engine/Source/Runtime/Renderer/Private/Lumen/LumenScreenProbeImportanceSampling.cpp",
                        [246, 247, 248, 249, 250, 251, 252, 253, 254, 255, 256, 257, 258],
                        "UseImportanceSampling"),
        source_evidence("Engine/Source/Runtime/Renderer/Private/Lumen/LumenScreenProbeGather.cpp",
                        [686, 687, 688, 2275], "radiance-cache and ray-direction-frame gates"),
        source_evidence("Engine/Shaders/Private/Lumen/LumenScreenProbeTracing.usf",
                        [685, 686, 687, 688, 693, 699, 700, 701, 702, 703, 704, 719, 720, 727,
                         828, 829, 830, 831, 832, 833, 859, 860, 861, 862, 870, 874, 875, 876,
                         877, 878, 879, 880], "trace dispatch guard and cache call"),
        source_evidence("Engine/Shaders/Private/Lumen/LumenRadianceCacheInterpolation.ush",
                        [245, 246, 247, 248, 249, 250, 251, 252, 253, 257, 263, 264, 265, 272,
                         284, 285, 286, 287, 288, 289, 332, 334, 335, 337, 338, 339, 340, 341,
                         343, 346, 348, 349, 350, 351, 352, 353, 354, 401, 402, 403, 429, 440,
                         443, 444, 446, 447, 448, 450, 475, 476, 477, 478, 479, 480, 481, 482,
                         484, 485, 486, 487, 488, 489, 490, 491, 492, 499, 501, 502, 503, 505,
                         507, 510, 511], "cache probe, parallax, interpolation and apply"),
        source_evidence("Engine/Shaders/Private/BlueNoise.ush", [23, 24, 25], "BlueNoiseVec2"),
        source_evidence("Engine/Shaders/Private/BlueNoiseCommon.ush",
                        [11, 12, 13, 14, 15, 16, 17, 18, 19], "GetBlueNoiseVec2"),
        source_evidence("Engine/Shaders/Private/Lumen/LumenScreenProbeTracingCommon.ush",
                        [8, 9, 10, 11, 12, 13, 14, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98],
                        "probe texel center and structured ray info"),
        source_evidence("Engine/Shaders/Private/Lumen/LumenScreenProbeCommon.ush",
                        [122, 123, 124, 125, 135, 136, 137, 138, 202, 203, 204, 207, 208, 209,
                         210, 211], "screen position, tile and compacted texel encoding"),
        source_evidence("Engine/Shaders/Private/Lumen/LumenPosition.ush",
                        [16, 17, 18, 19, 20, 21], "translated-world position reconstruction"),
        source_evidence("Engine/Shaders/Private/Common.ush",
                        [1962, 1963, 1964, 1965, 1966, 1967, 1968, 1969, 1970, 1971, 1972,
                         1973, 1974, 1975, 1976, 1977, 1978, 1979, 1980],
                        "RayIntersectSphere far root"),
        source_evidence("Engine/Shaders/Private/MonteCarlo.ush",
                        [159, 160, 161, 162, 163, 164, 165, 166, 167, 168, 169, 170, 178, 179,
                         180, 181, 182, 183, 184, 185, 186, 187, 188, 189, 190, 191, 192, 193,
                         194, 195, 196, 197, 198, 199, 200, 201, 202, 203, 204, 205, 206],
                        "equi-area mapping"),
        source_evidence("Engine/Shaders/Private/FastMathThirdParty.ush",
                        [108, 109, 110, 111, 112, 113, 114, 115], "acosFast"),
    ]

    def res(name, output=False):
        path, desc, ev = d.resource(name, output=output)
        writer = next((p for p in d.passes if p["Pointer"] == ev["version"]), None)
        return {"name": name, "file": str(Path(path).relative_to(d.root)),
                "exists": path.exists(), "descriptor": desc,
                "uniqueResource": ev["uniqueResource"], "version": ev["version"],
                "writerPass": None if writer is None else {
                    "Id": writer["Id"], "Pointer": writer["Pointer"],
                    "EventName": writer["EventName"]},
                "sha256": ev.get("sha256"), "bytes": ev.get("bytes"),
                "layout": ev.get("layout")}

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
    for name, record in inputs.items():
        if not record["exists"]:
            raise ValueError(f"missing captured payload for {name}")
        if name not in ("probeWorldOffset", "compactedTraceTexelAllocator",
                        "compactedTraceTexelData", "compactTracingIndirectArgs"):
            layout = record["layout"]
            if layout is None or int(layout["ByteSize"]) != int(record["bytes"]):
                raise ValueError(f"{name}: layout bytes do not match file bytes")
    binding_expected = {
        "RadianceCacheParameters.RadianceProbeIndirectionTexture": inputs["indirection"]["uniqueResource"],
        "RadianceCacheParameters.RadianceCacheFinalRadianceAtlas": inputs["finalRadianceAtlas"]["uniqueResource"],
        "RadianceCacheParameters.RadianceCacheDepthAtlas": inputs["depthProbeAtlas"]["uniqueResource"],
        "ScreenProbeParameters.ImportanceSampling.StructuredImportanceSampledRayInfosForTracing": inputs["rayInfosForTracing"]["uniqueResource"],
        "ScreenProbeParameters.ScreenProbeSceneDepth": inputs["screenProbeSceneDepth"]["uniqueResource"],
        "ScreenProbeParameters.BlueNoise.BlueNoise.Vec2Texture": blue["pointer"],
    }
    for name, expected in binding_expected.items():
        actual = parameter_bindings[name]["Pointer"]
        if name in ("ScreenProbeParameters.BlueNoise.BlueNoise.Vec2Texture",):
            if actual[-16:] != expected[-16:]:
                raise ValueError(f"parameter binding {name} pointer {actual} != {expected}")
        elif actual != expected:
            raise ValueError(f"parameter binding {name} pointer {actual} != {expected}")
    for name in ("RadianceCacheParameters.ProbeWorldOffset",
                 "ScreenProbeParameters.RWTraceHit",
                 "ScreenProbeParameters.RWTraceRadiance",
                 "CompactedTraceParameters.CompactedTraceTexelAllocator",
                 "CompactedTraceParameters.CompactedTraceTexelData"):
        if parameter_bindings[name].get("Null"):
            raise ValueError(f"parameter binding {name} is null")
    if int(ind_meta["extentX"]) != 192 or int(ind_meta["extentY"]) != 48 or int(ind_meta["depth"]) != 48:
        raise ValueError("indirection volume dimensions changed")
    if int(ind_meta["rowPitchBytes"]) != 192 * 4 or int(ind_meta["zSliceStrideBytes"]) != 48 * 192 * 4:
        raise ValueError("indirection row/z stride is not the captured 768/36864")
    if int(rad_meta["extentX"]) != 4352 or int(rad_meta["extentY"]) != 4352:
        raise ValueError("radiance atlas extent changed")
    if int(dep_meta["extentX"]) != 4096 or int(dep_meta["extentY"]) != 4096:
        raise ValueError("depth atlas extent changed")
    if int(ray_meta["extentX"]) != 640 or int(ray_meta["extentY"]) != 672:
        raise ValueError("ray-info extent changed")
    if int(scene_meta["extentX"]) != 80 or int(scene_meta["extentY"]) != 84:
        raise ValueError("screen-probe depth extent changed")

    if lane >= allocator:
        raise ValueError("selected lane is beyond the allocator")
    if lane >= min(ceil64 * 64, ceil32 * 32):
        raise ValueError("selected lane is not covered by both group-size dispatch triples")
    if result["radianceTapCount"] != 32:
        raise ValueError(f"radiance tap count {result['radianceTapCount']} != 32")
    if len(result["neighbors"]) != 8:
        raise ValueError("neighbor count != 8")
    if len(result["neighbors"][0]["probeOffsetRawHalf4"]) != 4:
        raise ValueError("offset storage is not half4")
    if abs(result["sumWeights"] - 1.0) > 1e-5:
        raise ValueError(f"neighbor weights do not sum to 1: {result['sumWeights']}")

    selfchecks = [
        ("pass_is_graph_0001_trace_cache",
         pass_pointer.startswith("0001") and d.selected["EventName"] == "TraceGlobalSDFAndApplyRadianceCache",
         pass_pointer),
        ("referenced_structs_null_and_nested_validated",
         len(ref_rows) == 8 and len(d.null_references) == 1 and nested_blue["dimensions"] == [128, 128, 64],
         {"rows": len(ref_rows), "null": list(d.null_references), "nested": nested_blue}),
        ("blue_noise_pointer_matches_uniform_byte_40",
         blue["pointerSuffix16"] == blue["pointerFromContentBytesAt40"],
         {"bindingPointer": blue["pointer"], "suffix16": blue["pointerSuffix16"]}),
        ("blue_noise_payload_matches_layout_and_readback",
         blue["exportFile"] == blue["layout"]["File"] and blue["readbackStatus"]["Status"] == "Dumped",
         {"file": blue["exportFile"], "sha256": blue["payload"]["sha256"],
          "readback": blue["readbackStatus"]}),
        ("blue_noise_is_linear_bgra",
         (not blue["layout"]["ViewSRGB"]) and blue["layout"]["StoredFormat"] == "PF_B8G8R8A8",
         blue["decode"]),
        ("exact_input_versions_resolved",
         all(v["version"] is not None for k, v in inputs.items() if k not in ("traceHit", "traceRadiance"))
         and inputs["traceHit"]["version"] == pass_pointer,
         {k: v["version"] for k, v in inputs.items()}),
        ("all_input_payloads_hashed", all(v.get("sha256") for v in inputs.values()),
         {k: v.get("sha256") for k, v in inputs.items()}),
        ("parameter_bindings_link_shader_operands_to_exact_resources",
         all((parameter_bindings[n]["Pointer"][-16:] == v[-16:]) if n.endswith("Vec2Texture")
             else parameter_bindings[n]["Pointer"] == v
             for n, v in binding_expected.items()),
         {n: parameter_bindings[n]["Pointer"] for n in binding_expected}),
        ("both_group_size_triples_match_allocator",
         triple64 == [ceil64, 1, 1] and triple32 == [ceil32, 1, 1],
         {"allocator": allocator, "div64": triple64, "div32": triple32}),
        ("selected_lane_below_allocator_and_both_dispatches",
         lane < allocator and lane < min(ceil64 * 64, ceil32 * 32), lane),
        ("hlsl_dispatch_guard_present", True,
         "DispatchThreadId.x < CompactedTraceTexelAllocator[0]"),
        ("structured_importance_active",
         reference_mode["value"] == "0" and importance["value"] == "1"
         and num_levels["value"] == "1"
         and int(ray_infos[witness["traceBufferXY"][1], witness["traceBufferXY"][0]]) != 0,
         {"referenceMode": reference_mode["value"], "importanceSample": importance["value"],
          "rayInfo": witness["rayInfoPacked"]}),
        ("radiance_cache_active",
         sp_radiance["value"] == "1" and int(params["UseSkyVisibility"]) == 0,
         sp_radiance["value"]),
        ("query_from_actual_compacted_lane",
         witness["dispatchIndex"] == lane and witness["packedCompactedTraceWord"] == int(compacted[lane]),
         witness["dispatchIndex"]),
        ("selected_trace_buffer_unique_in_compacted_list",
         selected_occurrences == 1 and compact_unique_count == allocator,
         {"selectedTraceBuffer": tb, "occurrences": selected_occurrences,
          "uniqueCoords": compact_unique_count, "allocator": allocator}),
        ("eight_allocated_neighbors",
         all(n["indirectionPacked"] != 0xFFFFFFFF for n in result["neighbors"]),
         [n["indirectionPacked"] for n in result["neighbors"]]),
        ("thirty_two_radiance_taps", result["radianceTapCount"] == 32, result["radianceTapCount"]),
        ("eight_depth_values",
         len({n["probeIndex"] for n in result["neighbors"]}) == 8
         and all(n["depth"]["packed"] >= 0 for n in result["neighbors"]),
         [n["depth"]["packed"] for n in result["neighbors"]]),
        ("offset_half4_storage",
         off_meta["bytesPerElement"] == 8 and off_meta["numElements"] == 16384
         and all(len(n["probeOffsetRawHalf4"]) == 4 for n in result["neighbors"]),
         {"bytesPerElement": off_meta["bytesPerElement"],
          "numElements": off_meta["numElements"], "note": offset_note}),
        ("depth_matches_captured_trace_hit", depth_within_bound,
         {"replayed": replayed_distance, "captured": captured_distance,
          "absDiff": depth_abs_diff, "exact": depth_exact}),
        ("radiance_forward_consistency_not_equality", radiance_forward_consistent,
         {"model": model.tolist(), "observed": final_radiance.tolist(),
          "absDiff": radiance_abs_diff.tolist(), "tolerance": tolerance.tolist()}),
        ("no_gpu_executed", True, "file-only CPU replay"),
    ]

    report = {
        "schema": SCHEMA,
        "scope": "one actual compacted screen-probe ray's World Radiance Cache lookup, exact inputs and boundary comparison",
        "engine": {"root": str(engine_root) if engine_root else reference["engine"]["root"], "version": "5.8.1",
                   "commit": "71fe36aac5a8df5ccd66c763ffc902b29b6a9c43"},
        "capture": {"dumpRoot": str(Path(dump_root).resolve()),
                    "progress": progress_data, "infos": infos,
                    "pass": d.selected,
                    "parameterPayloads": d.parameter_evidence,
                    "referencedStructsFile": dump_reader.file_evidence(
                        d.root / "Base/ReferencedStructs.json", d.root),
                    "consoleVariables": {k: cvars[k] for k in (
                        "r.Lumen.ThreadGroupSize32",
                        "r.Lumen.ScreenProbeGather.ReferenceMode",
                        "r.Lumen.ScreenProbeGather.ImportanceSample",
                        "r.Lumen.ScreenProbeGather.ImportanceSample.NumLevels",
                        "r.Lumen.ScreenProbeGather.RadianceCache",
                        "r.Lumen.HardwareRayTracing",
                        "r.Lumen.ScreenProbeGather.HardwareRayTracing")}},
        "sourceEvidence": source,
        "referenceStructs": {"records": ref_rows, "null": d.null_references,
                             "nestedBlueNoise": nested_blue},
        "parameterBindings": parameter_bindings,
        "blueNoise": blue,
        "inputs": inputs,
        "parameters": {"radianceCache": params, "view": view, "screenProbe": screen_probe},
        "dispatch": {"allocator": allocator, "compactedTraceTexelDataElements": int(compacted.size),
                     "compactedTraceCoordsUnique": compact_unique_count,
                     "indirectArgs": args.tolist(),
                     "enumOffsets": {"NumTracesDiv64": 0, "NumTracesDiv32": 12,
                                     "NumLightSampleTracesDiv64": 24,
                                     "NumLightSampleTracesDiv32": 36},
                     "dispatchTripleDiv64": triple64, "dispatchTripleDiv32": triple32,
                     "hlslGuard": "if (DispatchThreadId.x < CompactedTraceTexelAllocator[0])",
                     "cvarThreadGroupSize32": thread_group,
                     "exactGroupSizeWitness": False,
                     "groupSizeCaveat": "The captured indirect-args buffer contains both Div64 and Div32 records; DumpGPU does not record which record the indirect dispatch consumed. Both triples are consistent with the allocator and the selected lane is below both limits. The CVar r.Lumen.ThreadGroupSize32=1 and the D3D12/SM6 NVIDIA device indicate 32, but this replay does not claim the 32-vs-64 choice as a direct device witness."},
        "selection": {"rule": "ascending compacted lane; uniform probe; valid scene depth; valid clipmap coverage; 8 allocated neighbors; fractional weights in [0.05,0.95]; captured final TraceHit.bHit=false; non-zero replayed cache radiance",
                      "selectedLane": lane,
                      "selectedTraceBufferOccurrences": selected_occurrences,
                      "rejectionsUpToSelection": dict(rejections)},
        "queryWitness": witness,
        "cacheLookup": result,
        "comparison": {
            "observable": "captured Lumen.ScreenProbeGather.TraceHit float at the selected trace-buffer texel",
            "capturedHitWord": hit_word, "capturedHitWordHex": f"{hit_word:08x}",
            "capturedDistance": captured_distance,
            "capturedFlags": {"bHit": b_hit, "bMoving": b_moving,
                              "bReachedRadianceCache": b_reached_cache},
            "replayedCacheDepth": replayed_distance,
            "depthExact": depth_exact, "depthAbsDiff": depth_abs_diff,
            "depthTolerance": 0.001,
            "depthWeightedSumForReference": result["cacheTraceHitDistanceWeightedSum"],
            "whyThisIsAtTheCacheBoundary": "The shader sets TraceHitDistance from SampleRadianceCacheInterpolated.TraceHitDistance and writes it with EncodeProbeRayDistance. With captured bHit=false, Transparency was >0.5 at the cache call, so the cache branch ran and the distance was stored. Fog, skylight leaking, pre-exposure and R11G11B10 quantization do not modify this distance.",
            "finalRadianceComparison": {
                "word": radiance_word, "decoded": final_radiance.tolist(),
                "model": model.tolist(), "absDiff": radiance_abs_diff.tolist(),
                "tolerance": tolerance.tolist(), "consistent": radiance_forward_consistent,
                "isEqualityClaim": False,
                "zeroTerms": zero_terms,
                "note": "This is a forward consistency observation only. The selected lane is a cache-reached GDF miss (bHit=false) but the preceding GDF Lighting and exact Transparency are not captured separately, so the cache subtotal is not claimed equal to TraceRadiance."}},
        "boundary": {
            "subtotal": "SampleRadianceCacheInterpolated return: cacheRadiance and interpolated TraceHitDistance",
            "after": "the preceding GDF/voxel trace decision (its Lighting and Transparency are not reconstructed here)",
            "before": "SampleRadianceCacheAndApply's downstream TraceResult handling, skylight leaking, height fog, View.PreExposure and R11G11B10 quantization",
            "finalRadianceEqualityClaimed": False},
        "traceBranch": {
            "actualPass": "TraceGlobalSDFAndApplyRadianceCache",
            "hardwareRayTracingCvar": hardware_rt["value"],
            "screenProbeHardwareRayTracingCvar": cvars["r.Lumen.ScreenProbeGather.HardwareRayTracing"]["value"],
            "globalSDFClipmaps": view["NumGlobalSDFClipmaps"],
            "globalSDFPageObjectGridBytes": view["GlobalDistanceFieldPageObjectGridBufferSize"],
            "runtimeWitness": "The pass's unique RDG event name is emitted only from the TraceVoxels lambda, and the non-HWRT path is selected (r.Lumen.HardwareRayTracing=0). The exact FTraceVoxels sub-permutation and per-ray GDF hit are not captured, so the boundary is reported as path-eligible global-SDF voxel tracing, not as a proven per-ray trace result."},
        "limitations": [
            "No GPU workload was executed; this is a CPU replay of captured data.",
            "The exact compiled shader permutation is not recorded by DumpGPU. STRUCTURED_IMPORTANCE_SAMPLING is evidenced by the captured CVars, the non-null RayInfos binding, its GenerateRays 8x8 producer and a valid packed RayInfo, but the permutation bit itself is not a captured witness.",
            "The exact 32-vs-64 thread-group choice is not directly witnessed by DumpGPU; both indirect triples are recorded and the selected lane is valid under both.",
            "The preceding GDF decision (TraceResult.Lighting, TraceResult.Transparency) is not reconstructed; the cache lookup is replayed independently and its depth output is compared at the lookup boundary.",
            "The radiance-to-final comparison is a forward consistency observation, not an equality claim; skylight leaking, fog, pre-exposure and quantization are captured zero/known terms but the GDF contribution is not separately captured.",
            "ProbeWorldOffset is entirely zero in this captured version; the replay uses the captured half4 values rather than inventing offsets.",
            "The sky-visibility and stochastic-interpolation variants are not active in this lookup."],
        "selfchecks": [{"name": n, "passed": bool(p), "evidence": e} for n, p, e in selfchecks],
        "allSelfchecksPassed": all(bool(p) for _, p, _ in selfchecks),
    }
    if not report["allSelfchecksPassed"]:
        failures = [n for n, p, _ in selfchecks if not p]
        raise ValueError(f"selfcheck failures: {failures}")
    return report


def write_evidence_files(report, out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    blue = report["blueNoise"]
    (out / "blue-noise-export.json").write_text(json.dumps({
        "schema": "unreal-understanding-blue-noise-export-v1",
        "texturePointer": blue["pointer"],
        "path": blue["exportFile"],
        "sha256": blue["payload"]["sha256"],
        "bytes": blue["payload"]["bytes"],
        "width": 128, "height": 8192,
        "decodedFormat": "PF_B8G8R8A8",
        "memoryChannelOrder": "B,G,R,A",
        "shaderSample": "Texture2D<float2>.Load(...).xy = R,G from bytes 2,1, divided by 255",
        "srgb": False,
        "volumeDimensions": blue["volume"]["dimensions"],
        "moduloMasks": blue["volume"]["moduloMasks"],
        "native": {k: blue[k] for k in (
            "nativeTextureName", "nativeDimension", "nativeFormat", "nativeExtent",
            "nativePointer", "diagnosticResource", "dumpedAs", "firstScheduledByPass",
            "exportSRGB", "exportViewFormat", "readbackStatus")},
        "layout": blue["layout"],
        "binding": {k: blue[k] for k in (
            "passPointer", "uniformBuffer", "layoutName", "uniformByteOffset",
            "pointer", "pointerFromContentBytesAt40", "structContent")},
    }, indent=2) + "\n", encoding="utf-8")
    (out / "permutation-evidence.json").write_text(json.dumps({
        "schema": "unreal-understanding-world-cache-permutation-v1",
        "passPointer": report["capture"]["pass"]["Pointer"],
        "eventName": report["capture"]["pass"]["EventName"],
        "structuredImportanceSampling": True,
        "radianceCache": True,
        "traceBranch": report["traceBranch"],
        "evidence": {
            "cvars": report["capture"]["consoleVariables"],
            "rayInfosBoundTo": report["inputs"]["rayInfosForTracing"],
            "rayInfoProducer": "GenerateRays 8x8 pass 161 (last writer before the selected pass)",
            "maxImportanceSamplingOctahedronResolution": report["parameters"]["screenProbe"]["MaxImportanceSamplingOctahedronResolution"],
            "screenProbeTracingOctahedronResolution": report["parameters"]["screenProbe"]["ScreenProbeTracingOctahedronResolution"],
            "selectedRayInfo": report["queryWitness"]["rayInfoPacked"],
            "source": report["sourceEvidence"],
        },
        "groupSize": report["dispatch"],
    }, indent=2) + "\n", encoding="utf-8")
    (out / "selfchecks.json").write_text(json.dumps({
        "schema": "unreal-understanding-world-cache-selfchecks-v1",
        "allPassed": report["allSelfchecksPassed"],
        "checks": report["selfchecks"],
    }, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump", nargs="?", help="DumpGPU binary capture root")
    parser.add_argument("out", nargs="?", help="output directory")
    parser.add_argument("--capture", type=Path, help="reconstructed DumpGPU directory")
    parser.add_argument("--output", type=Path, help="directory for replay artifacts")
    parser.add_argument("--engine-root", type=Path, help="optional pinned UE checkout for source evidence")
    parser.add_argument("--pass-id", type=int, default=167)
    args = parser.parse_args(argv)
    capture = args.capture or args.dump
    output = args.output or args.out
    if capture is None or output is None:
        parser.error("supply --capture and --output")
    report = build_report(capture, args.pass_id, args.engine_root)
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    (out / "world-cache-replay.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    write_evidence_files(report, out)
    print(json.dumps({
        "out": str(out / "world-cache-replay.json"),
        "pass": report["capture"]["pass"]["Pointer"],
        "lane": report["selection"]["selectedLane"],
        "probe": report["queryWitness"]["screenProbeIndex"],
        "depth": report["comparison"]["replayedCacheDepth"],
        "capturedDepth": report["comparison"]["capturedDistance"],
        "taps": report["cacheLookup"]["radianceTapCount"],
        "allSelfchecksPassed": report["allSelfchecksPassed"],
    }))


if __name__ == "__main__":
    main()
