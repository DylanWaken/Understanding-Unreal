# World Radiance Cache lookup replay - capture `binary-20261112-153914`

One **actual dispatched** screen-probe ray from pass 167 (`TraceGlobalSDFAndApplyRadianceCache`, graph 0001, pointer `000100000228d58f3800`). The replay reconstructs only the `SampleRadianceCache` lookup; no GPU workload was executed.

Capture root: `D:\UnrealEngine\UnrealUnderstandingCapture\capture\buffers\gpudump\binary-20261112-153914`. Engine 5.8.1 `71fe36aac5a8df5ccd66c763ffc902b29b6a9c43`. The capture progress file records `raw_dump_status=ok` for this path.

## Result

| Measurement | Value |
| --- | --- |
| Selected compacted lane | **190** of 82102 |
| Compacted trace texels | 82102 unique; selected [290, 66] occurs 1 time |
| Screen probe / trace texel | 676 / (2, 2) |
| Trace-buffer texel | (290, 66) |
| Scene depth | 671.328491 |
| Query origin (translated world) | [36.360939, 676.745117, 134.995438] |
| Ray direction | [-0.531528, -0.432203, -0.728477] |
| Cone half-angle | 0.088427 |
| Clipmap / cell / fractional weights | 0 / [24, 30, 25] / [0.349064, 0.656754, 0.879955] |
| 8 corner probe indices | [54, 66, 56, 68, 55, 67, 57, 69] |
| 8 depth values (half decoded) | [265.5, 352.5, 368.5, 405.0, 354.0, 405.0, 399.5, 436.0] |
| 32 radiance taps | 32 (8 probes x 4 bilinear taps) |
| Replayed cache radiance | [11.740781, 14.52484, 10.691309] |
| Replayed cache depth | **394.431884765625** |
| Captured `TraceHit` distance | **394.431884765625** |
| Difference | **0** (bit-exact) |
| Final `TraceRadiance` decoded | [0.734375, 0.914062, 0.671875] |
| Cache x `View.PreExposure` | [0.733799, 0.907803, 0.668207] |
| Forward-consistency worst channel ratio | 0.441328 (must be < 1) |

The depth result is the lookup's `TraceHitDistance` output and is compared to the decoded `TraceHit` distance written by the same pass, after masking the flag bits. The values are exactly equal. The radiance row is a **forward-consistency observation only**, not a claim that the cache subtotal equals `TraceRadiance`.

## Actual query

- Compacted word `69206692` (0x042002a4) decodes to probe 676, trace texel [2, 2].
- Screen tile [36, 8] is the uniform probe tile; temporal index 7 gives tile jitter [14, 14].
- Screen depth word `1143461126` (0x4427d506) = 671.328491; screen UV [0.461328, 0.160473]; screen position [-0.077344, 0.604167].
- The homogeneous input [-51.923073, 405.594299, 671.328491, 1] transforms through `View.ScreenToTranslatedWorld` to translated-world origin [36.360939, 676.745117, 134.995438].
- BlueNoise frame 7, volume [128, 128, 64], masks [127, 127, 63]; wrapped xyz [36, 8, 7], texture xy [36, 904].
- BlueNoise raw BGRA [0, 91, 207, 255]; shader `.xy` is R,G = [207.0, 91.0], decoded [0.811765, 0.356863] (linear, byte/255).
- RayInfo `129` (0x0081): cell [1, 2], level 0, size 16; UV [0.113235, 0.147304].

## BlueNoise version link

- Pass `000100000228d58f3800`; uniform buffer `000100000228805a8e80` (`FBlueNoise`), byte offset **40**, pointer `0001000002299302b980` (16-hex payload pointer `000002299302b980`).
- Native `FastBlueNoise_vec2_128x128x64`, `PF_B8G8R8A8`, [128, 8192]x1, DumpedAs `NativeTexture.0001000002299302b980.mip0_1.a0_1.f0_1.dim1.plane0`; diagnostic resource `000100000225e8ce9630`.
- Exact export `Resources/000100000225e8ce9630.linear.mip0.v000100000225e8ce68d0.bin`; `StoredFormat=PF_B8G8R8A8`, ViewSRGB=False, readback `Dumped` (4194304 bytes). SHA-256 `cd7286ea0fd5374282ac76deb74e47be42a0a175991a293468b506c4c5a19ad0`.
- Volume dimensions `[128, 128, 64]`; 64 slices of 128x128 are stacked vertically in the 128x8192 texture, matching `GetBlueNoiseVec2`.

## Cache lookup

Selected clipmap 0, cell size 104.166664, corner [-2552.083252, -2568.75, -2612.916504]; minimum local trace distance 360.843903; mip 0 (`FinalRadianceAtlasMaxMip=0`).

| eps | probe | cell | weight | t (far) | correction | depth word | depth |
| --- | --- | --- | --- | --- | --- | --- | --- |
| [0, 0, 0] | 54 | [24, 30, 25] | 0.026822 | 384.400726 | 2.031738 | 56358 | 265.5 |
| [0, 0, 1] | 66 | [24, 30, 26] | 0.19661 | 301.828125 | 1.284596 | 56706 | 352.5 |
| [0, 1, 0] | 56 | [24, 31, 25] | 0.05132 | 329.936066 | 1.551297 | 56770 | 368.5 |
| [0, 1, 1] | 68 | [24, 31, 26] | 0.376185 | 260.306488 | 0.942878 | 56916 | 405 |
| [1, 0, 0] | 55 | [25, 30, 25] | 0.014383 | 303.60849 | 1.399882 | 56712 | 354 |
| [1, 0, 1] | 67 | [25, 30, 26] | 0.105432 | 237.500473 | 0.823542 | 56916 | 405 |
| [1, 1, 0] | 57 | [25, 31, 25] | 0.02752 | 258.585388 | 1.015488 | 56894 | 399.5 |
| [1, 1, 1] | 69 | [25, 31, 26] | 0.201729 | 205.63446 | 0.586869 | 57040 | 436 |

All 8 probes are allocated (no `0xFFFFFFFF`); their weights sum to 1.000000008. Each probe contributes four bilinear radiance taps: the JSON records all 32 taps with atlas XY, packed word, decoded radiance and effective weight. The probe offsets are a captured **half4** buffer (8 bytes/element, 16,384 elements); every captured half4 is zero, so the sphere centres are unperturbed for this version.

The parallax correction is the shipped `RayIntersectSphere` far root followed by `CorrectionFactor = T^2 / (R * dot(ReprojectedDirection, Direction))`; each row above retains `(A,B,C)`, discriminant, far `T`, reprojected direction, denominator and UV. The eight samples are combined in the shader's nested Z/Y/X `lerp` order for both radiance and depth.

## Exact versions and provenance

| Operand | Version | Writer | File | SHA-256 (first 16) |
| --- | --- | --- | --- | --- |
| indirection | `000100000225e8ccc458` | AllocateUsedProbes (142) | `Resources\000100000228dcc20010.mip0.v000100000225e8ccc458.bin` | `97ed5cb5f632b473` |
| finalRadianceAtlas | `000100000228ba9e7db0` | FixupBordersAndGenerateMips (158) | `Resources\000100000228ec1e8450.mip0.v000100000228ba9e7db0.bin` | `a0216270fef13b0b` |
| depthProbeAtlas | `000100000225e8adc160` | TraceFromProbes Res=32x32 SkyVisibility:0 (154) | `Resources\000100000228ec1e8190.mip0.v000100000225e8adc160.bin` | `6a5d6d0735e3a21b` |
| probeWorldOffset | `000100000225e8ccc458` | AllocateUsedProbes (142) | `Resources\000100000228ec1eedb0.v000100000225e8ccc458.bin` | `fa43239bcee7b97c` |
| rayInfosForTracing | `000100000225fb910fc0` | GenerateRays 8x8 (161) | `Resources\000100000225fb910b30.mip0.v000100000225fb910fc0.bin` | `62c0a7fd04c899e6` |
| screenProbeSceneDepth | `000100000228ec1e0010` | AdaptivePlacementSpawn (132) | `Resources\000100000228e8df7cc8.mip0.v000100000228ec1e0010.bin` | `045eb8064c0d4e22` |
| compactedTraceTexelAllocator | `000100000228d58f0310` | CompactTraces WaveOps:1 (165) | `Resources\000100000225fb91f930.v000100000228d58f0310.bin` | `d2cd5ec8c1378341` |
| compactedTraceTexelData | `000100000228d58f0310` | CompactTraces WaveOps:1 (165) | `Resources\000100000228d58f0010.v000100000228d58f0310.bin` | `deec641ed53b6036` |
| compactTracingIndirectArgs | `000100000228d58f2a88` | SetupCompactedTracesIndirectArgs (166) | `Resources\000100000228d58f2760.v000100000228d58f2a88.bin` | `94e63f7bb8ecb651` |
| traceHit | `000100000228d58f3800` | TraceGlobalSDFAndApplyRadianceCache (167) | `Resources\000100000225fb913620.mip0.v000100000228d58f3800.bin` | `b68ef5956b568f73` |
| traceRadiance | `000100000228d58f3800` | TraceGlobalSDFAndApplyRadianceCache (167) | `Resources\000100000225fb913310.mip0.v000100000228d58f3800.bin` | `4064e3d92c2cc336` |

Every shader parameter that reads these resources is linked in `parameterBindings`: indirection, final radiance atlas, depth atlas, ray infos, screen-probe depth and BlueNoise pointers match the unique resource IDs. The `ProbeWorldOffset` binding is non-null and its only selected-pass input is the captured half4 buffer above.

## ReferenceStructs

Eight records: **7 validated**, **1 explicit null** (`HairStrandsVoxel`, `ContentKind=RDGUniformBufferUnbound`). Every non-null record has `ContentAvailable=true`, no traversal cycle, and a payload file whose size equals its declared `ByteSize`. Nested `FBlueNoise` -> `FBlueNoiseParameters` was parsed: dimensions `[128, 128, 64]`, masks `[127, 127, 63]`, `Vec2Texture` at offset 40.

The null record is the hair-voxel uniform for a permutation that is not active in this pass (the captured event name has no `HairStrands` suffix); it is not an operand of the cache lookup.

## Dispatch and group size

Compacted allocator `N=82102`. Indirect args are Div64 `[1283, 1, 1]` and Div32 `[2566, 1, 1]`; both equal `ceil(N/group)` and are consistent. The HLSL guard is `if (DispatchThreadId.x < CompactedTraceTexelAllocator[0])`. The selected lane is below both limits.

`r.Lumen.ThreadGroupSize32=1` (Constructor) and the D3D12/SM6 NVIDIA device indicate 32, but DumpGPU does not record which indirect record the dispatch consumed. The replay therefore keeps both triples and the **group-size caveat**; it does not claim 32-vs-64 as a direct device witness.

## Boundary

Subtotal = `SampleRadianceCacheInterpolated` return (cache radiance and interpolated `TraceHitDistance`). It is after the preceding GDF/voxel decision and before `SampleRadianceCacheAndApply`'s downstream handling, skylight leaking, height fog, `View.PreExposure` and R11G11B10 quantization. The captured `TraceHit` distance is set from this lookup; with captured `bHit=false`, transparency was >0.5 at the cache call, so the cache branch ran. Fog/leak/quantization do not modify that distance.

The final `TraceRadiance` is **not** claimed equal to the cache subtotal. The lane has `bHit=false`, which requires transparency greater than 0.5 at the trace branch. The preceding GDF `Lighting` and exact `Transparency` are not captured separately. The observed final radiance is within the captured R11G11B10 quantization tolerance of `cache x PreExposure`; that is a forward-consistency observation, not an equality claim.

## Trace branch

- Pass: `TraceGlobalSDFAndApplyRadianceCache`; `r.Lumen.HardwareRayTracing=0`.
- Global SDF clipmaps 4, page-object grid 7340032 bytes.
- The pass event is emitted only from the `TraceVoxels` lambda and the non-HWRT path is selected. The exact `FTraceVoxels` sub-permutation and the per-ray GDF hit are not captured; this is an explicit path-eligibility boundary, not a per-ray trace witness.

## Reproduce

```text
python -m pip install -r requirements.txt
python replay_world_cache.py --capture <DumpGPU-directory> --output ./rerun --pass-id 167
python write_world_chapter.py --output ./rerun
python draw_world_flow.py --report ./rerun/world-cache-replay.json --output ./rerun/world-cache-flow.svg
python verify_world_replay.py --capture <DumpGPU-directory> --output ./rerun/verification.json
```

Checked with NumPy 2.5.4 and Python 3.14.6. The reconstructed capture supplies every numerical input. The frozen report preserves the original run progress and pinned source annotations. Pass `--engine-root` to read those source annotations directly from the pinned checkout.

## Files

- `world-cache-replay.json` - complete query, 8 neighbors, 32 taps, 8 depths, offset/parallax records, versions, hashes and selfchecks.
- `world-cache-replay.md` - this short chapter.
- `world-cache-flow.svg` - diagram of the recorded query, neighbors, interpolation and output boundaries.
- `blue-noise-export.json` - exact native binding, layout and payload descriptor.
- `permutation-evidence.json` - CVars, RayInfos producer, trace branch and group-size caveat.
- `selfchecks.json` / `verification.json` - replay checks and independent re-run verification.
- `replay_world_cache.py`, `world_cache_math.py`, `dump_reader.py`, `write_world_chapter.py`, `verify_world_replay.py` - CPU replay and checks.
- `reproducibility.json` - byte-identical second-run hashes.

## Selfchecks

All 22 checks pass (`selfchecks.json`).

| Check | Result |
| --- | --- |
| `pass_is_graph_0001_trace_cache` | pass |
| `referenced_structs_null_and_nested_validated` | pass |
| `blue_noise_pointer_matches_uniform_byte_40` | pass |
| `blue_noise_payload_matches_layout_and_readback` | pass |
| `blue_noise_is_linear_bgra` | pass |
| `exact_input_versions_resolved` | pass |
| `all_input_payloads_hashed` | pass |
| `parameter_bindings_link_shader_operands_to_exact_resources` | pass |
| `both_group_size_triples_match_allocator` | pass |
| `selected_lane_below_allocator_and_both_dispatches` | pass |
| `hlsl_dispatch_guard_present` | pass |
| `structured_importance_active` | pass |
| `radiance_cache_active` | pass |
| `query_from_actual_compacted_lane` | pass |
| `selected_trace_buffer_unique_in_compacted_list` | pass |
| `eight_allocated_neighbors` | pass |
| `thirty_two_radiance_taps` | pass |
| `eight_depth_values` | pass |
| `offset_half4_storage` | pass |
| `depth_matches_captured_trace_hit` | pass |
| `radiance_forward_consistency_not_equality` | pass |
| `no_gpu_executed` | pass |

## Limits

- No GPU workload was executed; this is a CPU replay of captured data.
- The exact compiled shader permutation is not recorded by DumpGPU. STRUCTURED_IMPORTANCE_SAMPLING is evidenced by the captured CVars, the non-null RayInfos binding, its GenerateRays 8x8 producer and a valid packed RayInfo, but the permutation bit itself is not a captured witness.
- The exact 32-vs-64 thread-group choice is not directly witnessed by DumpGPU; both indirect triples are recorded and the selected lane is valid under both.
- The preceding GDF decision (TraceResult.Lighting, TraceResult.Transparency) is not reconstructed; the cache lookup is replayed independently and its depth output is compared at the lookup boundary.
- The radiance-to-final comparison is a forward consistency observation, not an equality claim; skylight leaking, fog, pre-exposure and quantization are captured zero/known terms but the GDF contribution is not separately captured.
- ProbeWorldOffset is entirely zero in this captured version; the replay uses the captured half4 values rather than inventing offsets.
- The sky-visibility and stochastic-interpolation variants are not active in this lookup.

