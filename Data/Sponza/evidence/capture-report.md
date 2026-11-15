# Sponza complete native GPU frame

Status: **ok**. Run `20261115-083654`.

This step preserves one complete, unfiltered `DumpGPU` engine frame of the approved saved Sponza hero view. It is raw evidence only: no packaging, no image comparison, no performance measurement, and no map save. Every number below comes from the retained dump files or the capture session.

## Raw frame

| Item | Value |
| --- | --- |
| Raw directory | `D:\UnrealUnderstanding-Sponza\output\gpu-dump\raw\sponza-20261115-083654` |
| Dump status | `ok` |
| Files / bytes | 12,458 / 95,239,807,151 bytes (88.699 GiB) |
| Payload files / bytes | 10,318 / 95,160,502,078 bytes (88.625 GiB) |
| Versioned payload files | 10,318 `.v<key>.bin` files; 705 resources have more than one version (max 110) |
| Passes / resources | 886 / 1,541 |
| Parameter blocks | 803 blocks for 803 passes; 80 parameterless engine passes; 3 expected-block gaps |
| Native uniform-buffer records / bindings | 521 / 35,648 |
| Referenced structs / subresource layouts | 487 / 6,675 |
| Readback records | 10,404, statuses {'Dumped': 10404} |
| Console variables | 9,262 |
| Engine graphs in the frame | 4 total: 3 scene + 1 Slate UI |
| Primary-view records decoded | 3 (3 match the approved hero camera) |

Dump invocation: `DumpGPU -path="D:\UnrealUnderstanding-Sponza\output\gpu-dump\raw\sponza-20261115-083654" -noexplorer -oneframe`. Requested one frame (`r.DumpGPU.FrameCount=1`, `-oneframe`), all textures (`r.DumpGPU.Texture=2`), all buffers (`r.DumpGPU.Buffer=2`), pass parameters, console variables, native uniform-buffer textures, SkipTracking buffers, and `r.DumpGPU.RedumpInputs=1` for in-place versions.

The unique on-disk payload is authoritative for disk bytes. `DumpReadbackStatus.json` can contain more than one readback record for the same version (and native-buffer aliases), so the sum of its `ActualBytes` is not used as the disk total.

## Approved state and warm history

| Item | Observed before dump |
| --- | --- |
| Camera actor | `CAM_Hero_Reference`, saved transform checked against `output/final-setup.json` |
| Camera readback | {"location_cm": [-850.0, 0.0, 90.0], "rotation": {"pitch": 13.0, "yaw": 0.0, "roll": -0.0}} |
| ViewFOV at capture | 70.0 |
| Viewport size / mode | [1280, 720] / lit (view_mode_index 3) |
| Exposure | EV100 11.0 (AEM_HISTOGRAM pinned), bias 0 |
| Warmup / settle draws | 300 draws in 3.906 s |
| Lumen route | DGI 1, reflections 1, HWRT 1, FarField 0 |
| Screen traces | gather 1, reflections 1 |
| Diagnostics off | Visualize 0, Probe Debug 0, ProbePlacement 0, VisualizeTraces 0, ShaderPrint 0 |

Requested dump CVars were read back after assignment: `Texture=2`, `Buffer=2`, `PassParameters=1`, `ConsoleVariables=1`, `Root=*`, `RedumpInputs=1`, `NativeUniformBufferTextures=1`, `Buffer.SkipTracking=1`, `MaxStagingSize=1024`, `FrameCount=1`, `Stream=0`, `Upload=0`. `r.RHI.UniformBufferContentMap.Enable` is read-only at runtime; the launcher set it before uniform creation and the session observed `true` (set_by `SystemSettingsIni`).

## Camera identity from the dumped view buffer

| Item | Saved actor / approved setup | Actual dumped view (`ViewData.PrimaryViews`) |
| --- | --- | --- |
| Actor | `CAM_Hero_Reference` | primary view id 0 |
| World origin (cm) | [-850.0, 0.0, 90.0] | [-850.0, 0.0, 90.0] |
| ViewRect | [0, 0, 1280, 720] | [0, 0, 1280, 720] |
| HFOV (deg) | 70.0 | 70.0 |
| VFOV (deg) | 42.995661 | 42.9957 |
| Pitch / yaw (deg) | 13.0 / 0.0 | 13.0 / 0.0 |
| Forward | approx [0.97437, 0, 0.22495] | [0.9743700637234466, 0.0, 0.22495105894297684] |

The view records are decoded directly from the dumped `ViewData.PrimaryViews` `FPackedView` structure: `ViewRect` at byte 640, `ViewOriginHigh/Low`, `ViewForward`, `PreViewTranslation`, and the `ViewToClip` projection matrix. The bridge readback is retained separately in the session; the identity above uses the dumped matrices, not the bridge call count.

| Scene graph | Kind | Passes | ViewRect | Origin (cm) | HFOV | Pitch / yaw |
| ---: | --- | ---: | --- | --- | ---: | --- |
| 0 | scene | 294 | [0, 0, 1280, 720] | [-850.0, 0.0, 90.0] | 70.0 | 13.0 / 0.0 |
| 1 | slate | 4 | n/a | n/a | n/a | n/a |
| 2 | scene | 295 | [0, 0, 1280, 720] | [-850.0, 0.0, 90.0] | 70.0 | 13.0 / 0.0 |
| 3 | scene | 293 | [0, 0, 1280, 720] | [-850.0, 0.0, 90.0] | 70.0 | 13.0 / 0.0 |

All dumped scene graphs belong to engine frame scope(s) ['Frame 302 (Delta=0.013059s)']. The presented graph is index 3; it contains the final FXAA/Tonemap chain and is the last scene graph. 3 scene graphs were rendered inside the one dumped engine frame, so a bridge draw count is not used as camera identity. The 1 Slate UI graph has no scene view.

## Screenshot evidence

| Item | Value |
| --- | --- |
| DumpGPU screenshot | `D:\UnrealUnderstanding-Sponza\output\gpu-dump\raw\sponza-20261115-083654\Base\Screenshot.png` |
| Dimensions / bytes / SHA-256 | 1280x720 / 1,892,042 / `faa77025dcfd82b4982a28a2a155205ae6347fc03989923b6dc2d9423faaa6eb` |
| Mean display luma (sampled) | 71.8852 |
| Pre-dump comparison | mean abs RGB 0.9154 over the same dimensions; stable=True |

The DumpGPU screenshot is the final presented frame, not a separately normalized image. Its difference from the warmed pre-dump viewport screenshot is small, which confirms the dumped frame is the warmed hero view.

## Subsystem coverage

| Subsystem | Status | Passes | Resources | Evidence / explicit branch |
| --- | --- | ---: | ---: | --- |
| vsm | available | 51 | 49 | ClearBuffer(Shadow.Virtual.CacheInstanceAsDynamic Size=40bytes), ClearBuffer(Shadow.Virtual.DirtyPageFlags Size=98308bytes), ClearBuffer(Shadow.Virtual.OutputOffsetBuffer Size=4bytes), ClearBuffer(Shadow.Virtual.StatsBuffer Size=360bytes) / Shadow.Virtual.AllocatedPageRectBounds, Shadow.Virtual.BatchInds, Shadow.Virtual.BatchInfos |
| deferred | available | 27 | 11 | BasePassParallel, ClearRenderTarget(GBufferF, slice 0, mip 0) 1280x888 ClearAction, DepthPassParallel, GBufferClear / GBufferA, GBufferB, GBufferC |
| direct_lighting | available | 48 | 32 | Batched lights, BuildLightTiles MaxLightsPerTile=8 GroupPerTile=1, ClearBuffer(Lumen.DirectLighting.CompactedLightTileAllocatorPerLight Size=16bytes), ClearBuffer(Lumen.DirectLighting.LightTileAllocator Size=4bytes) / Lumen.DirectLighting.CompactedLightTileAllocatorPerLight, Lumen.DirectLighting.CompactedLightTiles, Lumen.DirectLighting.CompactedShadowTraceIndirectArgs |
| lumen_cards | available | 35 | 26 | CardPage Commands:1, ClearCardCapture, CompressToSurfaceCache Albedo, CompressToSurfaceCache Emissive / Lumen.CardCaptureAlbedoAtlas, Lumen.CardCaptureDepthStencilAtlas, Lumen.CardCaptureEmissiveAtlas |
| screen_probes | available | 42 | 38 | ClearBuffer(Lumen.ScreenProbeGather.CompactedTraceTexelAllocator Size=4bytes), ClearBuffer(Lumen.ScreenProbeGather.NumAdaptiveScreenProbes Size=4bytes), ClearTextureUint(Lumen.ScreenProbeGather.ScreenTileAdaptiveProbeHeader R32_UINT 80x56 Mip=0), DiffuseIndirectComposite(DiffuseIndirect=ScreenProbeGather) 1280x720 / Lumen.ScreenProbeGather.AdaptiveProbePlacementMask, Lumen.ScreenProbeGather.AdaptiveScreenProbeData, Lumen.ScreenProbeGather.BRDFProbabilityDensityFunctionSH |
| radiance_cache | available | 9 | 33 | ClearRadianceCacheUpdateResources, TraceGlobalSDFAndApplyRadianceCache / Lumen.RadianceCache.ClearProbePDFsIndirectArgs, Lumen.RadianceCache.DepthProbeAtlasTexture, Lumen.RadianceCache.FilterProbesIndirectArgs |
| histories | available | 12 | 10 | ClearBuffer(MegaLights.LightPowerHistoryRatio Size=4bytes), ResampleLightingHistoryToCardCaptureAtlas, TemporalAccumulation, TemporalReprojection(1280x720) / Lumen.Radiosity.NumFramesAccumulatedAtlas, Lumen.Reflections.NumFramesAccumulated, Lumen.ResampledCardCaptureNumFramesAccumulated |
| reflections | available | 12 | 22 | InitReflectionIndirectArgs, ReflectionCompactRaysIndirectArgs, ReflectionHardwareRayTracingCS default, ReflectionsResolve DownsampleFactor:1x1 SpatialReconstruction:1 Aniso:1 / Lumen.Reflection.CompactTracingIndirectArgs, Lumen.Reflections.ClearTileIndirectArgs, Lumen.Reflections.ClearUnusedTracingTileIndirectArgs |
| composition | available | 24 | 5 | ComposeTranslucencyToNewSceneColor(AfterDOF) 1280x720 -> 1280x720, CompositeTraces, CompositeTracesIntoAtlas, CompositeVirtualShadowMapMask / PostDOFTranslucency.SceneColor, PostProcessing.SceneColor.HalfRes, SceneColor |

Every requested branch has retained pass/resource entries: VSM page/table/culling resources, deferred G-buffers and base pass, direct lighting and light tiles, Lumen card capture/copy/surface cache, screen probes, radiance cache, temporal histories, reflections, and the final PostProcessing/Tonemap/FXAA composition. There are no silently invented entries; absent branches would be listed as `absent` with a reason.

## Completeness and gaps

- `Passes.json`: 886 pass records retained.
- `ResourceDescs.json`: 1,541 descriptors, 1,541 with a dumped payload, 0 explicitly missing. There are 1,529 unique payload keys; 12 descriptors duplicate a key and share that payload.
- In-place versions: `r.DumpGPU.RedumpInputs=1`; the payload contains 10,318 version-keyed files and 705 resources with more than one version (max 110). The version keys are retained in the payload file names.
- `ParameterBindings.json`: 803 blocks; 80 engine clear/copy/barrier/graph passes are parameterless by type; 3 passes have a parameter type outside the serialized block set (ShaderPrint `FParameters`).
- `NativeUniformBufferBindings.json`: 521 records, 35,648 bindings, layouts {'FViewUniformShaderParameters': 336, 'FSkyAtmosphereInternalCommonParameters': 15, 'FBlueNoise': 168, 'FSceneTextureUniformParameters': 2}; 69 exported-file references, 69 present on disk.
- `DumpReadbackStatus.json`: 10,404 records, statuses {'Dumped': 10404}, kinds {'SkipTrackingBuffer': 33, 'Texture': 6462, 'Texture3D': 213, 'Buffer': 3696}.
- `SubresourceLayouts.json`: 6,675 subresource layouts retained with byte sizes.
- `ConsoleVariables.csv`: 9,262 rows.
- `Base/PassDrawCounts.json`: 0 bytes; empty is an explicit unsupported-draw-counts gap, not a missing pass record.

Explicit gaps / unsupported branches:

- `unsupported_draw_counts`: Base/PassDrawCounts.json is empty; per-pass draw-count capture is not provided by this DumpGPU build. Passes.json and resource bindings are still complete and retained.
- `pass_parameter_block_missing`: {"count": 3, "passes": ["3:ShaderPrint::UploadParameters(FParameters)", "301:ShaderPrint::UploadParameters(FParameters)", "596:ShaderPrint::UploadParameters(FParameters)"], "reason": "These passes use a parameter type outside the serialized shader-parameter block set; the pass record itself is retained, but no parameter block exists in the dump."}
- `parameterless_passes`: {"count": 80, "reason": "Clear/copy/barrier/graph/ray-tracing-build passes use engine built-in parameter structs and intentionally have no serialized shader parameter block. Their pass records and resource edges are retained."}
- `raytracing_scene_buffer_copy_warning`: {"descriptor_count": 9, "descriptors_with_payload": 9, "reason": "D3D12 emitted a staging-copy warning for the ray-tracing acceleration structure. The dump still contains a nonzero SceneBuffer payload for every descriptor; the warning is retained as a readback-path caveat, not silently dropped.", "warning": "[2026.11.15-08.37.02:317][302]LogD3D12RHI: Warning: RHICopyToStagingBuffer cannot be used on the RayTracing Accelleration structure FRayTracingScene::SceneBuffer", "warning_count": 9}
- `read_only_cvar_assignment`: {"count": 1, "reason": "r.RHI.UniformBufferContentMap.Enable is read-only at runtime; it was set before uniform creation through the launcher startup ini and observed as true. The later runtime assignment was rejected by design and did not change the value.", "warning": "[2026.11.15-08.36.59:375][301]Error: r.RHI.UniformBufferContentMap.Enable is read only!"}
- `shader_local_values`: Per-pixel/per-thread shader choices and intermediate registers are not DumpGPU resources and are not fabricated from pass parameters.
- `memoryless_or_non_rdg`: Resources that are never referenced by an RDG pass, memoryless resources, and writable SkipTracking resources are outside the raw frame; absence is explicit, not silently filled.
- `uniform_buffer_content_scope`: Native uniform-buffer textures expose the CPU content map and offsets. Shader-local values not written to a uniform buffer are not recoverable.

Shader-local choices are not resources. They are counted as gaps and no value is invented for them. Resources outside the RDG graph, memoryless resources, and writable SkipTracking resources are likewise absent by design and are called out rather than silently replaced.

## Provenance and hashes

| Item | Value |
| --- | --- |
| Engine | UE 5.8.1 |
| Engine BuildId | `b4effda9-1b7d-4e62-b9a6-cb99e41a70c9` |
| GPU / driver | NVIDIA RTX PRO 6000 Blackwell Workstation Edition / 610.62 |
| RHI / shader platform | D3D12 / SM6 |
| Declared source base | `71fe36aac5a8df5ccd66c763ffc902b29b6a9c43` |
| Bridge BuildId | `b4effda9-1b7d-4e62-b9a6-cb99e41a70c9` (matches declared: True) |
| Engine BuildId == project BuildId | True |
| Map SHA-256 | `3ef49eddd474fc16e6129dac3bcbb16bb4937529d9213296a865d368ede72c64` (unchanged: True) |
| Setup SHA-256 | `b31b8f8d4186b38cc45101b1eb6d72c5cdbb00b351adf21fb744868dc2fafd55` |
| UnrealEditor.exe SHA-256 | `dd2a31e9a3babe48b56c49c54d85661f50d07a1254d09fac64f50b85244d4833` |
| UnrealEditor-RenderCore.dll SHA-256 | `6f1bf96fab9841f0d4793cb65abc4dfbf459aa41ee4a8ee90d06d737cf32bf1d` |
| UnrealEditor-Renderer.dll SHA-256 | `5508eb82f1ea84d9fbb8dea620a44fbc4f7119231281dd2eea7ae1d5a8a5fc09` |
| UnrealEditor-RHI.dll SHA-256 | `7d697f9bb4b6d0d9b5b750bc10e2e259b1f346121b42857f0b3df65255953a46` |
| UnrealEditor-D3D12RHI.dll SHA-256 | `3434e257fcd64d93c8ad7058508dc55f83e51d67fc4cbb64537f3b0cdf64c96e` |
| ArdaLabCapture.dll SHA-256 | `9d313d997fa704165b5c0b411250ab0a7bebd8ea4f126f9be85840bb2e7d4923` |
| Capture script SHA-256 | `1edb19f34c86a16027db6112bffb53e0ead9011e60d44b50c7eb8b730a0b061d` |
| Launcher script SHA-256 | `15f79b790986779727f3c18f57ff5eb4e8cf90536b815a22da478a9c32634df5` |

The session record carries the complete engine/module hash list under `environment.files`; the table above selects the runtime products used to build the frame. The capture-script hash is the launch-time hash stored in that session. The launcher-script hash is report-time; the launcher record retains the exact arguments, PID and wall time for this run.

## Preservation of the pre-existing final evidence

| Check | Result |
| --- | --- |
| Recorded frame hashes | 70 checked; mismatches: 0 |
| Recorded run hashes | 3 checked; mismatches: 0 |
| Recorded script hashes | 7 checked; mismatches: 2 |
| Script mismatches pre-date this run | True (scripts/capture_lumen.py, scripts/analyze_final.py) |
| Approved image | matches: True |
| `output/final-setup.json` | `b31b8f8d4186b38cc45101b1eb6d72c5cdbb00b351adf21fb744868dc2fafd55`; matches manifest: True |
| Preserved | True |

This GPU step only added `scripts/capture_gpu_dump.py`, `scripts/run_gpu_dump.ps1`, `scripts/analyze_gpu_dump.py`, `output/gpu-dump/**` and this report. The pre-existing final images, runs, reports and setup file were re-hashed above and were not changed.

## Requested acceptance checks

1. **Complete available native resources, parameters and pass records retained with explicit gaps**: yes. 886 passes, 1,541 resource descriptors (1,541 with payload, 0 missing), 803 parameter blocks, 521 native uniform-buffer records, 10,404 readback records and 10,318 version-keyed payload files (705 resources with in-place versions) are in the raw directory; the gap list names the empty draw-count file, the three ShaderPrint parameter gaps, the 80 parameterless passes, the ray-tracing staging warning, and the shader-local/non-RDG limits.
2. **Dump uses the approved saved hero camera with actual view dimensions and identity**: yes. The saved `CAM_Hero_Reference` matched `output/final-setup.json`; the dumped `ViewData.PrimaryViews` decoded origin [-850.0, 0.0, 90.0] cm, ViewRect [0, 0, 1280, 720], HFOV 70.0 deg, pitch/yaw 13.0/0.0 deg.

## Invocation ledger

- Launcher record: `D:\UnrealUnderstanding-Sponza\output\gpu-dump\logs\gpu-dump-20261115-083640-launcher.json`; PID 38576; exit_code None; wall 122.7 s; dump status `ok`. The PowerShell host did not expose the child exit code; the owned process exited, no UnrealEditor process remains, and the dump status is `ok`.
- Bridge draws recorded: 303; warmup 300 draws; dump trigger draws 2; fallback re-triggers 0.
- Dump armed at `2026-11-15T08:36:59Z`, completed at `2026-11-15T08:38:39Z` in 99.671 s; engine frame scope ['Frame 302 (Delta=0.013059s)'].
- Exact console invocations are retained in `D:\UnrealUnderstanding-Sponza\output\gpu-dump\run-20261115-083654.json` under `commands` (63 records), including the one `DumpGPU` command `DumpGPU -path="D:\UnrealUnderstanding-Sponza\output\gpu-dump\raw\sponza-20261115-083654" -noexplorer -oneframe`. The bridge's console wrapper returned false for these commands (it does not report success for CVar assignment), so the actual CVar readback and `Base/Status.txt` are the authoritative evidence; the dump status is `ok`.

## Compliance

- The map was loaded and piloted, never saved: SHA-256 before and after are identical.
- This step issued no write under `D:/UnrealEngine`, `D:/UnrealUnderstanding` or the original capture project; all new files are under this workspace.
- No Git command was run. No engine or bridge source was modified in this step.
- No packaging was performed. Only the launcher-owned UnrealEditor process was used and waited for; there is no process-name kill.
