# GDF march replay - capture `binary-20261112-153914`

CPU replay of one captured Lumen global-SDF ray for graph 0001 pass 167, compacted lane 190 (screen probe 676, trace texel (2,2)). No GPU work was run.

## Result

| Measurement | Value |
| --- | --- |
| Captured `TraceHit.bHit` | False |
| Replayed GDF hit | False |
| GDF iterations | 1 |
| GDF interval (min/max) | [353.379028, 360.843903] |
| GDF interval length | 7.464874 |
| Cache bound (`MinTraceDistanceBeforeInterpolation`) | 360.843903 |
| Previous `TraceHit` distance (pass 163) | 363.379028 |
| Surface bias | 5.0 |
| First sample distance field | 33.746117 |
| Expand-surface amount | 9.920635 |
| First step distance | 33.746117 |
| Captured `TraceHit` distance | 394.431885 |
| Replayed cache depth | 394.431885 |
| `TraceHit` word exact | True |
| `TraceRadiance` decoded | [0.734375, 0.9140625, 0.671875] |
| Exposure model | [0.7337988018989563, 0.9078025221824646, 0.6682068109512329] |
| Raw R11 store | 0x72dda39e |
| Dither R11 store | 0x731dab9f |
| Captured radiance word | 0x72ddab9e |

The GDF ray is a **miss** under the only captured GDF bytes (the pass-28 native export linked to the pass-167 bindings).  The captured `TraceHit.bHit` is false, so the captured value is not a GDF hit distance: it is the radiance-cache distance written after the cache branch overwrote `TraceHitDistance`. This replay reproduces the miss and the branch, not the final cache distance from GDF data.

## Ray and interval

- Query origin (translated world): `[36.360939025878906, 676.7451171875, 134.9954376220703]`
- Direction: `[-0.5315276384353638, -0.4322027862071991, -0.7284772396087646]`
- Cone half-angle: `0.08842656016349792`
- Normal bias: `[0.0, 0.0, -1.0]` (octahedral bytes [0, 0], two-sided=False)
- Biased GDF origin: `[33.70330047607422, 674.5841064453125, 126.35305786132812]`
- Previous `TraceHit`: word 0xc3b5b084, distance 363.379028, bHit=False, bReachedRadianceCache=True
- `VoxelTraceStartDistance = max(MinTraceDistance, prev - 2*SurfaceBias)` = `353.3790283203125`
- `Coverage.MinTraceDistanceBeforeInterpolation = tmin + cellSize*sqrt(3)` = `360.8439025878906`
- GDF `MaxTraceDistance` = min(20000, cacheBound) = `360.8439025878906`
- Actual GDF interval = `[353.3790283203125, 360.8439025878906]`, length `7.464874267578125`. The 20000 UU ray maximum is **not** the GDF interval.

## Iterations

The interval is shorter than one minimum step, so the loop samples once.

| step | t | clipmap | page table | packed | page index | mip value | page value | coverage | distance | expand | step | exit |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | 353.379028 | 0 | [16, 21, 17] | 0x0000005d | 93 | 0.549340 | 0.7126005291938782 | 1.000000 | 33.746117 | 9.920635 | 33.746117 | sampleRayTime > intersectionTimes.y after step |

Raw page-table word, UVs, all 8 mip taps and all 8 page-atlas taps are in `replay.json` under `trace.iterations`.

## Volumes and provenance

| Logical volume | Binding pointer | Diagnostic resource | Export | Format | Extent |
| --- | --- | --- | --- | --- | --- |
| pageTable | `000100000228ec2dc400` | `000100000228d576bdf8` | `000100000228d576bdf8.linear.mip0.v000100000225fb9fef70.bin` | PF_R32_UINT | 36x36x144 |
| pageAtlas | `000100000228ec2dbd00` | `000100000228d576b738` | `000100000228d576b738.linear.mip0.v000100000225fb9fef70.bin` | PF_R8 | 1024x1024x56 |
| coverageAtlas | `000100000228ec2dc080` | `000100000228d576ba00` | `000100000228d576ba00.linear.mip0.v000100000225fb9fef70.bin` | PF_R8 | 512x512x28 |
| mip | `000100000228ec2dc780` | `000100000228d576c0c0` | `000100000228d576c0c0.linear.mip0.v000100000225fb9fef70.bin` | PF_R8 | 63x63x252 |

Each native binding record names the exported `DiagnosticResource`; `ExportSubresourceCount` is 0 and `ExportFirstFile` resolves to the DepthPassParallel pass-28 version.  The four binding IDs have no recorded writer in graph 0001.

**Producer disclosure.** No graph-0001 pass outputs any of the four GDF volumes or their diagnostic resources.  The only GDF-named graph pass that writes a GDF resource is the `GlobalDistanceField.PageStatsBuffer` clear.  The producer is outside the captured graph and is not proven by DumpGPU.

**Timing caveat.** The exports are whole-native-resource readbacks scheduled at pass 28 (`FirstScheduledByPass=000100000225fb9fef70`), not pass-167 snapshots.  The pass-167 binding uses the same RHI native pointer and no graph write is recorded between pass 28 and pass 167, but `OwnerPassExecutionDependencyClaimed=false` and `SourceStateProvenAtScheduling=false`; mutation outside the RDG graph is not excluded.  This is not a general mutable-input equivalence claim.

## Branch and comparison

- GDF returns miss: `HitTime=-1`, `Lighting=0`, `Transparency=1`, `OpaqueHitDistance=360.8439025878906`.
- `TraceVoxels` keeps `bHit=false`; because `Transparency > 0.5` it does not take the voxel-hit branch.
- Radiance cache is valid, so `SampleRadianceCacheAndApply` adds the cache radiance, sets `Transparency=0`, and overwrites `TraceHitDistance` with the cache depth.
- `TraceResult.Lighting *= View.PreExposure` (0.0625), then `QuantizeForFloatRenderTarget` stores R11G11B10; `WriteTraceHit` encodes the distance with `bHit=false`.

Captured `TraceHit` word: 0x43c53748 (394.431885). Replayed cache depth 394.431885; word exact: True.

Radiance is not claimed bit-exact.  The exposure model `[0.7337988018989563, 0.9078025221824646, 0.6682068109512329]` packs to 0x72dda39e (G one 11-bit ULP below captured).  The exact dither for trace texel `[290, 66, 7]` (E=0.843811) packs to 0x731dab9f (R/B one ULP above).  The captured word 0x72ddab9e lies between them. Under the branch reconstructed from the earlier native snapshot, each model is within one store ULP per channel of the capture. This is consistent with arithmetic, filtering or store differences. The dump does not record a GPU intermediate that identifies the cause.

## Limits

- Exact compiled shader permutation (TraceVoxels, StructuredImportanceSampling, ThreadGroupSize32, RadianceCache, ScreenProbeExtraAO, HairStrands) is not recorded by DumpGPU; the replay follows the event-name/source path and captured CVars.
- The four GDF volumes are pass-28 native exports reused by pass 167; no RDG execution dependency proves byte invariance between those passes.
- Hardware trilinear filtering is modelled as float32 8-tap interpolation of the exported R8 volumes; GPU fixed-point filter weights are not captured. The miss margin is large (33.7 vs 9.92 UU, and page value -1 LSB still misses), so the hit/miss branch is robust even though the last fraction is not bit-exact.
- The previous TraceHit read by pass 167 is the pass-163 TraceScreen output. That version is captured and used; it is a same-graph read-before-write.
- The cache radiance is a CPU replay of the GPU's eight-probe interpolation; exact compiled arithmetic and any half-precision intermediates are not captured. TraceRadiance is compared only with a one-ULP store bound.
- The exact indirect dispatch record (Div32 vs Div64) consumed by the device is not recorded; the selected lane is below both limits.

## Checks

- `selected_lane_is_captured_compacted_trace`: pass - word=69206692
- `query_origin_matches_captured_screen_depth_and_ray_info`: pass - homogeneousZ=671.3284912109375 depth=671.3284912109375
- `four_gdf_native_bindings_resolve_to_exported_diagnostic_resources`: pass - {'pageAtlas': '000100000228d576b738', 'coverageAtlas': '000100000228d576ba00', 'pageTable': '000100000228d576bdf8', 'mip': '000100000228d576c0c0'}
- `gdf_interval_uses_cache_bound_not_20000`: pass - cacheBound=360.8439025878906 rayMax=20000.0
- `gdf_interval_is_shorter_than_one_minimum_step`: pass - length=7.464874267578125 minStep=9.920635223388672
- `gdf_trace_records_first_iteration_with_raw_values`: pass - iterations=1
- `gdf_miss_matches_captured_bHit_false`: pass - gdfHit=False captured_bHit=False
- `tracehit_word_is_exact_cache_override`: pass - captured=0x43c53748 expected=0x43c53748
- `tracehit_previous_version_is_a_same_graph_pass_before_167`: pass - pass 163 TraceScreen(Scene)
- `no_recorded_graph_writer_on_four_gdf_ids`: pass - no graph-0001 write
- `export_timing_caveat_recorded`: pass - owner/source-state proofs false
- `trace_radiance_residual_within_one_store_ulp`: pass - {'rawWordMatchesCaptured': False, 'ditherWordMatchesCaptured': False, 'rawCodeDeltaToCaptured': [0, -1, 0], 'ditherCodeDeltaToCaptured': [1, 0, 1]}
- `not_claiming_trace_radiance_exact`: pass - explicit forward-consistency only
- `no_gpu_work_executed`: pass - CPU only; no GPU dispatch

## Reproduce

```text
python -m pip install -r requirements.txt
python replay_gdf_march.py --capture <DumpGPU-directory> --output ./rerun
```

Outputs are `replay.json`, `replay.md` and `evidence-hashes.json`. The reconstructed capture supplies all numerical operands. The frozen report preserves pinned source annotations. Supply `--engine` to read the annotations directly from a pinned UE checkout.

