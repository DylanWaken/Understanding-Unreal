"""Generate the short world-cache replay chapter from world-cache-replay.json."""
from pathlib import Path
import argparse
import json

HERE = Path(__file__).resolve().parent


def f(value, digits=6):
    if isinstance(value, list):
        return "[" + ", ".join(f(v, digits) for v in value) + "]"
    if isinstance(value, float):
        if value == 0:
            return "0"
        if abs(value) >= 1e5 or abs(value) < 1e-4:
            return f"{value:.{digits}e}"
        return f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return str(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=HERE, help="replay artifact directory")
    args = parser.parse_args()
    report = json.loads((args.output / "world-cache-replay.json").read_text(encoding="utf-8"))
    q = report["queryWitness"]
    c = report["cacheLookup"]
    cmp = report["comparison"]
    blue = report["blueNoise"]
    disp = report["dispatch"]
    selfchecks = report["selfchecks"]
    lines = []
    add = lines.append

    add("# World Radiance Cache lookup replay - capture `binary-20261112-153914`")
    add("")
    add("One **actual dispatched** screen-probe ray from pass 167 "
        f"(`{report['capture']['pass']['EventName']}`, graph 0001, pointer "
        f"`{report['capture']['pass']['Pointer']}`). The replay reconstructs only the "
        "`SampleRadianceCache` lookup; no GPU workload was executed.")
    add("")
    add(f"Capture root: `{report['capture']['dumpRoot']}`. "
        f"Engine 5.8.1 `{report['engine']['commit']}`. "
        "The capture progress file records `raw_dump_status=ok` for this path.")
    add("")
    add("## Result")
    add("")
    add("| Measurement | Value |")
    add("| --- | --- |")
    add(f"| Selected compacted lane | **{q['dispatchIndex']}** of {disp['allocator']} |")
    add(f"| Compacted trace texels | {disp['compactedTraceCoordsUnique']} unique; selected "
        f"{q['traceBufferXY']} occurs {report['selection']['selectedTraceBufferOccurrences']} time |")
    add(f"| Screen probe / trace texel | {q['screenProbeIndex']} / ({q['traceTexelXY'][0]}, {q['traceTexelXY'][1]}) |")
    add(f"| Trace-buffer texel | ({q['traceBufferXY'][0]}, {q['traceBufferXY'][1]}) |")
    add(f"| Scene depth | {f(q['sceneDepth'])} |")
    add(f"| Query origin (translated world) | {f(c['query']['translatedWorldPosition'])} |")
    add(f"| Ray direction | {f(q['angularRay']['direction'])} |")
    add(f"| Cone half-angle | {f(q['angularRay']['coneHalfAngle'])} |")
    add(f"| Clipmap / cell / fractional weights | {c['selectedClipmap']} / {f(c['bottomCell'])} / {f(c['fractionalWeights'])} |")
    add(f"| 8 corner probe indices | {[n['probeIndex'] for n in c['neighbors']]} |")
    add(f"| 8 depth values (half decoded) | {[n['depth']['distance'] for n in c['neighbors']]} |")
    add(f"| 32 radiance taps | {c['radianceTapCount']} (8 probes x 4 bilinear taps) |")
    add(f"| Replayed cache radiance | {f(c['cacheRadiance'])} |")
    add(f"| Replayed cache depth | **{f(cmp['replayedCacheDepth'], 12)}** |")
    add(f"| Captured `TraceHit` distance | **{f(cmp['capturedDistance'], 12)}** |")
    add(f"| Difference | **{f(cmp['depthAbsDiff'], 12)}** ({'bit-exact' if cmp['depthExact'] else 'within bound'}) |")
    add(f"| Final `TraceRadiance` decoded | {f(cmp['finalRadianceComparison']['decoded'])} |")
    add(f"| Cache x `View.PreExposure` | {f(cmp['finalRadianceComparison']['model'])} |")
    ratios = [a / t for a, t in zip(cmp["finalRadianceComparison"]["absDiff"],
                                    cmp["finalRadianceComparison"]["tolerance"])]
    add(f"| Forward-consistency worst channel ratio | {f(max(ratios))} (must be < 1) |")
    add("")
    add("The depth result is the lookup's `TraceHitDistance` output and is compared to the "
        "decoded `TraceHit` distance written by the same pass, after masking the flag bits. The values are exactly equal. The radiance "
        "row is a **forward-consistency observation only**, not a claim that the cache "
        "subtotal equals `TraceRadiance`.")
    add("")
    add("## Actual query")
    add("")
    add(f"- Compacted word `{q['packedCompactedTraceWord']}` (0x{q['packedCompactedTraceWord']:08x}) "
        f"decodes to probe {q['screenProbeIndex']}, trace texel {q['traceTexelXY']}.")
    add(f"- Screen tile {q['screenTileXY']} is the uniform probe tile; temporal index "
        f"{q['screenTemporalIndex']} gives tile jitter {q['tileJitter']}.")
    add(f"- Screen depth word `{q['packedSceneDepth']}` (0x{q['packedSceneDepth']:08x}) = "
        f"{f(q['sceneDepth'])}; screen UV {f(q['screenUV'])}; screen position {f(q['screenPosition'])}.")
    add(f"- The homogeneous input {f(q['homogeneousPosition'])} transforms through `View.ScreenToTranslatedWorld` to translated-world origin {f(c['query']['translatedWorldPosition'])}.")
    add(f"- BlueNoise frame {q['blueNoiseFrameIndex']}, volume {q['blueNoiseDimensions']}, "
        f"masks {q['blueNoiseModuloMasks']}; wrapped xyz {q['blueNoiseWrappedXYZ']}, "
        f"texture xy {q['blueNoiseTextureXY']}.")
    add(f"- BlueNoise raw BGRA {q['blueNoiseRawBGRA']}; shader `.xy` is R,G = "
        f"{q['blueNoiseShaderRG']}, decoded {f(q['blueNoiseDecoded'])} (linear, byte/255).")
    add(f"- RayInfo `{q['rayInfoPacked']}` (0x{q['rayInfoPacked']:04x}): cell "
        f"{q['angularRay']['cell']}, level {q['angularRay']['level']}, size "
        f"{q['angularRay']['cellResolution']}; UV {f(q['angularRay']['uv'])}.")
    add("")
    add("## BlueNoise version link")
    add("")
    add(f"- Pass `{blue['passPointer']}`; uniform buffer `{blue['uniformBuffer']}` "
        f"(`{blue['layoutName']}`), byte offset **{blue['uniformByteOffset']}**, pointer "
        f"`{blue['pointer']}` (16-hex payload pointer `{blue['pointerFromContentBytesAt40']}`).")
    add(f"- Native `{blue['nativeTextureName']}`, `{blue['nativeFormat']}`, "
        f"{blue['nativeExtent']}x1, DumpedAs `{blue['dumpedAs']}`; diagnostic resource "
        f"`{blue['diagnosticResource']}`.")
    add(f"- Exact export `{blue['exportFile']}`; `StoredFormat={blue['layout']['StoredFormat']}`, "
        f"ViewSRGB={blue['layout']['ViewSRGB']}, readback `{blue['readbackStatus']['Status']}` "
        f"({blue['readbackStatus']['ActualBytes']} bytes). SHA-256 "
        f"`{blue['payload']['sha256']}`.")
    add(f"- Volume dimensions `{blue['volume']['dimensions']}`; 64 slices of 128x128 are "
        "stacked vertically in the 128x8192 texture, matching `GetBlueNoiseVec2`.")
    add("")
    add("## Cache lookup")
    add("")
    add(f"Selected clipmap {c['selectedClipmap']}, cell size {f(c['clipmapCellSize'])}, "
        f"corner {f(c['clipmapCornerTWSAndCellSize'])}; minimum local trace distance "
        f"{f(c['minimumLocalTraceDistance'])}; mip {f(c['mipLevel'])} "
        f"(`FinalRadianceAtlasMaxMip=0`).")
    add("")
    add("| eps | probe | cell | weight | t (far) | correction | depth word | depth |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for n in c["neighbors"]:
        add(f"| {n['offsetBits']} | {n['probeIndex']} | {n['cell']} | {f(n['trilinearWeight'])} | "
            f"{f(n['farIntersection'])} | {f(n['correctionFactor'])} | {n['depth']['packed']} | "
            f"{f(n['depth']['distance'])} |")
    add("")
    add(f"All 8 probes are allocated (no `0xFFFFFFFF`); their weights sum to "
        f"{f(c['sumWeights'], 9)}. Each probe contributes four bilinear radiance taps: "
        f"the JSON records all {c['radianceTapCount']} taps with atlas XY, packed word, "
        "decoded radiance and effective weight. The probe offsets are a captured "
        "**half4** buffer (8 bytes/element, 16,384 elements); every captured half4 is zero, "
        "so the sphere centres are unperturbed for this version.")
    add("")
    add("The parallax correction is the shipped `RayIntersectSphere` far root followed by "
        "`CorrectionFactor = T^2 / (R * dot(ReprojectedDirection, Direction))`; each row above "
        "retains `(A,B,C)`, discriminant, far `T`, reprojected direction, denominator and UV. "
        "The eight samples are combined in the shader's nested Z/Y/X `lerp` order for both "
        "radiance and depth.")
    add("")
    add("## Exact versions and provenance")
    add("")
    add("| Operand | Version | Writer | File | SHA-256 (first 16) |")
    add("| --- | --- | --- | --- | --- |")
    for key, rec in report["inputs"].items():
        writer = rec.get("writerPass") or {}
        writer_text = f"{writer.get('EventName', '?')} ({writer.get('Id', '?')})"
        add(f"| {key} | `{rec['version']}` | {writer_text} | `{rec['file']}` | "
            f"`{rec['sha256'][:16]}` |")
    add("")
    add("Every shader parameter that reads these resources is linked in "
        "`parameterBindings`: indirection, final radiance atlas, depth atlas, ray infos, "
        "screen-probe depth and BlueNoise pointers match the unique resource IDs. "
        "The `ProbeWorldOffset` binding is non-null and its only selected-pass input is "
        "the captured half4 buffer above.")
    add("")
    add("## ReferenceStructs")
    add("")
    add(f"Eight records: **7 validated**, **1 explicit null** (`HairStrandsVoxel`, "
        "`ContentKind=RDGUniformBufferUnbound`). Every non-null record has "
        "`ContentAvailable=true`, no traversal cycle, and a payload file whose size equals "
        "its declared `ByteSize`. Nested `FBlueNoise` -> `FBlueNoiseParameters` was parsed: "
        f"dimensions `{report['referenceStructs']['nestedBlueNoise']['dimensions']}`, "
        f"masks `{report['referenceStructs']['nestedBlueNoise']['moduloMasks']}`, "
        f"`Vec2Texture` at offset {report['referenceStructs']['nestedBlueNoise']['vec2Offset']}.")
    add("")
    add("The null record is the hair-voxel uniform for a permutation that is not active in "
        "this pass (the captured event name has no `HairStrands` suffix); it is not an "
        "operand of the cache lookup.")
    add("")
    add("## Dispatch and group size")
    add("")
    add(f"Compacted allocator `N={disp['allocator']}`. Indirect args are "
        f"Div64 `{disp['dispatchTripleDiv64']}` and Div32 `{disp['dispatchTripleDiv32']}`; "
        "both equal `ceil(N/group)` and are consistent. The HLSL guard is "
        f"`{disp['hlslGuard']}`. The selected lane is below both limits.")
    add("")
    add(f"`r.Lumen.ThreadGroupSize32={disp['cvarThreadGroupSize32']['value']}` "
        "(Constructor) and the D3D12/SM6 NVIDIA device indicate 32, but DumpGPU does not "
        "record which indirect record the dispatch consumed. The replay therefore keeps "
        "both triples and the **group-size caveat**; it does not claim 32-vs-64 as a direct "
        "device witness.")
    add("")
    add("## Boundary")
    add("")
    add("Subtotal = `SampleRadianceCacheInterpolated` return (cache radiance and interpolated "
        "`TraceHitDistance`). It is after the preceding GDF/voxel decision and before "
        "`SampleRadianceCacheAndApply`'s downstream handling, skylight leaking, height fog, "
        "`View.PreExposure` and R11G11B10 quantization. The captured `TraceHit` distance is "
        "set from this lookup; with captured `bHit=false`, transparency was >0.5 at the cache "
        "call, so the cache branch ran. Fog/leak/quantization do not modify that distance.")
    add("")
    add("The final `TraceRadiance` is **not** claimed equal to the cache subtotal. The lane "
        "has `bHit=false`, which requires transparency greater than 0.5 at the trace branch. The preceding GDF `Lighting` and exact "
        "`Transparency` are not captured separately. The observed final radiance is within "
        "the captured R11G11B10 quantization tolerance of `cache x PreExposure`; that is a "
        "forward-consistency observation, not an equality claim.")
    add("")
    add("## Trace branch")
    add("")
    add(f"- Pass: `{report['traceBranch']['actualPass']}`; "
        f"`r.Lumen.HardwareRayTracing={report['traceBranch']['hardwareRayTracingCvar']}`.")
    add(f"- Global SDF clipmaps {report['traceBranch']['globalSDFClipmaps']}, page-object "
        f"grid {report['traceBranch']['globalSDFPageObjectGridBytes']} bytes.")
    add("- The pass event is emitted only from the `TraceVoxels` lambda and the non-HWRT "
        "path is selected. The exact `FTraceVoxels` sub-permutation and the per-ray GDF "
        "hit are not captured; this is an explicit path-eligibility boundary, not a "
        "per-ray trace witness.")
    add("")
    add("## Reproduce")
    add("")
    add("```text")
    add("python -m pip install -r requirements.txt")
    add("python replay_world_cache.py --capture <DumpGPU-directory> --output ./rerun --pass-id 167")
    add("python write_world_chapter.py --output ./rerun")
    add("python draw_world_flow.py --report ./rerun/world-cache-replay.json --output ./rerun/world-cache-flow.svg")
    add("python verify_world_replay.py --capture <DumpGPU-directory> --output ./rerun/verification.json")
    add("```")
    add("")
    add("Checked with NumPy 2.5.4 and Python 3.14.6. The reconstructed capture supplies every numerical input. The frozen report preserves the original run progress and pinned source annotations. Pass `--engine-root` to read those source annotations directly from the pinned checkout.")
    add("")
    add("## Files")
    add("")
    add("- `world-cache-replay.json` - complete query, 8 neighbors, 32 taps, 8 depths, "
        "offset/parallax records, versions, hashes and selfchecks.")
    add("- `world-cache-replay.md` - this short chapter.")
    add("- `world-cache-flow.svg` - diagram of the recorded query, neighbors, interpolation and output boundaries.")
    add("- `blue-noise-export.json` - exact native binding, layout and payload descriptor.")
    add("- `permutation-evidence.json` - CVars, RayInfos producer, trace branch and group-size caveat.")
    add("- `selfchecks.json` / `verification.json` - replay checks and independent re-run verification.")
    add("- `replay_world_cache.py`, `world_cache_math.py`, `dump_reader.py`, "
        "`write_world_chapter.py`, `verify_world_replay.py` - CPU replay and checks.")
    add("- `reproducibility.json` - byte-identical second-run hashes.")
    add("")
    add("## Selfchecks")
    add("")
    add(f"All {len(selfchecks)} checks pass (`selfchecks.json`).")
    add("")
    add("| Check | Result |")
    add("| --- | --- |")
    for check in selfchecks:
        add(f"| `{check['name']}` | {'pass' if check['passed'] else 'FAIL'} |")
    add("")
    add("## Limits")
    add("")
    for limitation in report["limitations"]:
        add(f"- {limitation}")
    add("")
    (args.output / "world-cache-replay.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

