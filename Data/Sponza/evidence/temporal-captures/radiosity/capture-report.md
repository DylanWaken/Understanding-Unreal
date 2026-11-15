# Capture report - same-run narrow radiosity surface states

Status: **ok**  
Run: `20261115-105951`  
Workspace: `D:\UnrealUnderstanding-Radiosity`  
Map: `/Game/Labs/SponzaTeaching`  
Resolution: `1280 x 720`, screen percentage 100  
Mode: `radiosity-surface`; one Unreal process; five selected dumps.  

## 1. Scene identity and restoration

| Item | Evidence |
|---|---|
| Map SHA256 before / after | `3ef49eddd474fc16e6129dac3bcbb16bb4937529d9213296a865d368ede72c64` / `3ef49eddd474fc16e6129dac3bcbb16bb4937529d9213296a865d368ede72c64` |
| Map equals expected `3ef49edd...72c64` | `True` |
| Map saved? | **no**; the script has no save call; restored in memory only |
| Camera restored | `True` at `[-850.0, 0.0, 90.0]`, pitch 13, yaw 0, HFOV 70 |
| Sun restored | `True` at `80000` lux |
| Exposure restored | `True` EV100 11.0 min=max, bias 0 |
| Launchers unchanged | `True` |
| Engine/project BuildId match | `True` |

## 2. States, actual frames and paths

| Label | Stage | Target | Actual | Dump frame | Path | Files | Bytes | Resources | Status |
|---|---|---:|---:|---:|---|---:|---:|---|---|
| `baseline` | baseline | None | 1 | 124 | `D:\UnrealUnderstanding-Radiosity\output\gpu-dump\raw\20261115-105951\baseline` | 800 | 1,448,004,599 | 16/16 | ok |
| `light-001` | light | 1 | 1 | 128 | `D:\UnrealUnderstanding-Radiosity\output\gpu-dump\raw\20261115-105951\light-001` | 800 | 1,447,958,622 | 16/16 | ok |
| `light-064` | light | 64 | 63 | 192 | `D:\UnrealUnderstanding-Radiosity\output\gpu-dump\raw\20261115-105951\light-064` | 800 | 1,447,633,834 | 16/16 | ok |
| `camera-001` | camera | 1 | 1 | 262 | `D:\UnrealUnderstanding-Radiosity\output\gpu-dump\raw\20261115-105951\camera-001` | 800 | 1,447,986,939 | 16/16 | ok |
| `camera-064` | camera | 64 | 63 | 326 | `D:\UnrealUnderstanding-Radiosity\output\gpu-dump\raw\20261115-105951\camera-064` | 800 | 1,448,099,144 | 16/16 | ok |

The dump frame is the filler render after arming; the actual update is recovered from `Base/Passes.json` `Frame N` scopes matched to the bridge timeline. Requested 64 is reported as actual 63 where that is what the dump contains; nothing is relabelled.

## 3. Raw coverage and formats

Required exact resources per state:

`Lumen.SceneDepth`, `Lumen.SceneNormal`, `Lumen.Cards`, `Lumen.MeshCards`, `Lumen.PageBuffer`, `Lumen.PageTable`, `Lumen.CardTiles`, `Lumen.Radiosity.ProbeSHRedAtlas`, `Lumen.Radiosity.ProbeSHGreenAtlas`, `Lumen.Radiosity.ProbeSHBlueAtlas`, `Lumen.Radiosity.NumFramesAccumulatedAtlas`, `Lumen.Radiosity.SceneIndirectLighting`, `Lumen.Radiosity.FilteredTraceRadianceAtlas`, `Lumen.CardCaptureRects`, `ViewData.PrimaryViews`, `SceneDepthZ`

| State | SceneDepth | SceneNormal | SH R/G/B | Cards/pages | FilteredTrace | View/Depth | Missing |
|---|---|---|---|---|---|---|---|
| `baseline` | True | True | True | True | True | True | - |
| `light-001` | True | True | True | True | True | True | - |
| `light-064` | True | True | True | True | True | True | - |
| `camera-001` | True | True | True | True | True | True | - |
| `camera-064` | True | True | True | True | True | True | - |

Source-confirmed raw formats:

* `Lumen.SceneDepth`: 4096x4096, `PF_G16` native/stored, 33,554,432 bytes, `NativeFormatExpanded=False`, `StoredBytesAreNativeStorage=True`.
* `Lumen.SceneNormal`: 4096x4096, `PF_BC5` native, exporter-expanded `PF_FloatRGBA` readback (`NativeFormatExpanded=True`, `StoredBytesAreNativeStorage=False`); the per-version layout is in the manifest and the raw bytes are preserved unchanged.
* Radiosity SH atlases: 1024x1024 `PF_FloatRGBA`; `NumFramesAccumulatedAtlas` 512x512 `PF_R8`; `SceneIndirectLighting`/`FilteredTraceRadianceAtlas` 4096x4096 `PF_FloatR11G11B10`.

Every dumped version in the manifest carries its path, byte count, SHA-256, native/stored format, graph pointer prefix, pass ids/names and frame numbers. Base `Passes.json`, `ParameterBindings.json`, `NativeUniformBufferBindings.json`, `ReferencedStructs.json`, `SubresourceLayouts.json`, `ResourceDescs.json`, `DumpReadbackStatus.json` and `ConsoleVariables.csv` are hashed per state.

Source parameter blocks and native uniform buffers:

* Selected pass families: `BasePassParallel`, `Batched lights`, `BuildRadiosityTiles`, `ConvertToSH`, `CopyCardCaptureLightingToAtlas`, `DepthPassParallel`, `Integrate`.
* `Batched lights` is selected with `Lumen.CardTiles` so its captured parameter payload includes `CachedLightingPreExposure` (`FParameters` offset 204 in the captured metadata).
* `BuildRadiosityTiles` contributes the radiosity placement block with `FixedJitterIndex`; `Integrate`/`ConvertToSH` contribute `FLumenRadiosityCommonParameters` and the SH/trace parameters.
* `r.DumpGPU.PassParameters=1` and `r.DumpGPU.NativeUniformBufferTextures=1` are applied before each dump; the launcher startup ini requests `r.RHI.UniformBufferContentMap.Enable=1`, and the captured readback in every state is `found=true, value=true, set_by=SystemSettingsIni` (not assumed).
* Decoded from the captured bytes: `CachedLightingPreExposure` = `0.0625` in all five states and all three graph versions; `FixedJitterIndex` is present in the nested `FLumenRadiosityTexelTraceParameters` metadata for every state. Verification: `output/radiosity/parameter-verification.json`.

| State | Pass payloads | CachedLightingPreExposure | FixedJitterIndex | ParameterBindings | NativeUniformBufferBindings |
|---|---|---|---|---|---|
| `baseline` | 21 | True | True | 1,475,094 | 5,004,138 |
| `light-001` | 21 | True | True | 1,475,094 | 5,004,138 |
| `light-064` | 21 | True | True | 1,475,094 | 5,004,138 |
| `camera-001` | 21 | True | True | 1,475,094 | 5,004,138 |
| `camera-064` | 21 | True | True | 1,475,094 | 5,004,138 |

## 4. Sun, camera and exposure sequence

* Baseline: 80000 lux, camera `[-850.0, 0.0, 90.0]`, pitch 13, yaw 0, HFOV 70.
* Light step: 80000 -> 20000 lux; dumps at requested updates 1 and 64.
* Restore: 20000 -> 80000 lux and 64 warm draws (no dump), then the camera move.
* Camera move: direct `-150 cm` Y to `[-850.0, -150.0, 90.0]`, same rotation/FOV; dumps at requested updates 1 and 64.
* Exposure: fixed EV100 `11.0` min=max, bias `0.0` in every state.

## 5. Native clean images

| State | Native image | Bytes | SHA-256 | Mean / std | Valid |
|---|---|---:|---|---|---|
| `baseline` | `D:\UnrealUnderstanding-Radiosity\output\radiosity\raw-screenshots-20261115-105951\baseline-rawframe.png` | 1,884,167 | `8f0309c7a8b1699c6cbe93248cc790e90609acffd32eb6a39f0a2cc25884e217` | 70.71 / 69.415 | True |
| `light-001` | `D:\UnrealUnderstanding-Radiosity\output\radiosity\raw-screenshots-20261115-105951\light-001-rawframe.png` | 1,819,069 | `72e71562fa1f599af564537a02f9fd4eba035208620233e6428b261a78b7682c` | 55.666 / 59.288 | True |
| `light-064` | `D:\UnrealUnderstanding-Radiosity\output\radiosity\raw-screenshots-20261115-105951\light-064-rawframe.png` | 1,473,363 | `cbeddc76b13f88599ce72687fac40d7a29f28dff9a94b63c9fde5c5aca7ecfac` | 30.679 / 47.451 | True |
| `camera-001` | `D:\UnrealUnderstanding-Radiosity\output\radiosity\raw-screenshots-20261115-105951\camera-001-rawframe.png` | 1,807,028 | `43f3b24334a28b45adc8e1626bcfd65a8dec9bf0c19c8ec70a3f30eee990fffd` | 54.825 / 65.851 | True |
| `camera-064` | `D:\UnrealUnderstanding-Radiosity\output\radiosity\raw-screenshots-20261115-105951\camera-064-rawframe.png` | 1,898,306 | `1e60ea113d69e1d860a57dd9cecbf53438ba8752accb149a386408adabde7705` | 58.777 / 64.45 | True |

`Base/Screenshot.png` is the native same-frame image. Post-dump lit PNGs are separate later observations and are labelled as such; no depth-mode PNG is used as a raw surface atlas.

## 6. Superseded attempt(s)

| Run | Reason | Raw disposition | Retained records |
|---|---|---|---|
| `20261115-105305` | Validation failed after baseline: the light phase was recorded as 'light-20000' while the dump phase was 'light', so light-001's actual update could not be matched to the timeline (reported actual=None). The capture stopped instead of claiming a pass. | raw removed after the replacement run; superseded session, timeline, manifest and images retained under output/radiosity | output/radiosity/run-20261115-105305.json, timeline-20261115-105305.json, manifest-20261115-105305.json, images-20261115-105305/, raw-screenshots-20261115-105305/ |
| `20261115-105420` | Technically valid five-state run, but superseded to add the source-confirmable 'Batched lights' pass parameter block carrying CachedLightingPreExposure. | raw removed after the replacement run; superseded session, timeline, manifest and images retained under output/radiosity | output/radiosity/run-20261115-105420.json, timeline-20261115-105420.json, manifest-20261115-105420.json, images-20261115-105420/, raw-screenshots-20261115-105420/ |
| `20261115-105838` | Five-state capture with the Batched lights parameter pass was complete, but the evaluator compared the captured r.RHI.UniformBufferContentMap.Enable value 'true' against '1' and wrongly marked uniform_buffer_content_map_enabled false. The data show found=true, value=true, set_by=SystemSettingsIni; the predicate was corrected and the run repeated. | raw removed after the replacement run; superseded session, timeline, manifest and images retained under output/radiosity | output/radiosity/run-20261115-105838.json, timeline-20261115-105838.json, manifest-20261115-105838.json, images-20261115-105838/, raw-screenshots-20261115-105838/ |

## 7. Budget

| Quantity | Value |
|---|---:|
| New raw in this workspace | 7,239,683,138 bytes (6.7425 GiB) |
| Workspace cap | 8,589,934,592 bytes (8 GiB) |
| Existing Motion + Temporal raw | 29,967,682,705 bytes (27.9096 GiB) |
| Cumulative | 37,207,365,843 bytes (34.6521 GiB) |
| Cumulative cap | 48,318,382,080 bytes (45 GiB) |

## 8. Checks and hand-off

| Check | Result |
|---|---|
| `same_process_run` | `True` |
| `required_states_present` | `True` |
| `all_dumps_ok` | `True` |
| `actual_updates_recorded` | `True` |
| `target_updates_within_one` | `True` |
| `baseline_depth_present` | `True` |
| `baseline_normal_present` | `True` |
| `baseline_radiosity_sh_present` | `True` |
| `all_states_depth_normal_present` | `True` |
| `all_states_radiosity_sh_present` | `True` |
| `all_states_card_state_present` | `True` |
| `all_states_view_depth_present` | `True` |
| `all_states_formats_ok` | `True` |
| `native_screenshots_valid` | `True` |
| `native_same_frame_as_dump` | `True` |
| `native_exposure_continuous` | `True` |
| `exposure_fixed_ev100` | `True` |
| `sun_sequence_expected` | `True` |
| `camera_move_applied` | `True` |
| `camera_restored` | `True` |
| `sun_restored` | `True` |
| `exposure_restored` | `True` |
| `map_unchanged` | `True` |
| `map_equals_expected` | `True` |
| `workspace_budget_ok` | `True` |
| `cumulative_budget_ok` | `True` |
| `launchers_identical` | `True` |
| `build_ids_match` | `True` |
| `no_depth_png_as_raw_atlas` | `True` |
| `raw_payload_bytes_preserved` | `True` |
| `pass_parameter_blocks_present` | `True` |
| `cached_lighting_exposure_parameter_present` | `True` |
| `placement_jitter_parameter_present` | `True` |
| `radiosity_parameter_blocks_present` | `True` |
| `parameter_bindings_present` | `True` |
| `native_uniform_buffer_bindings_present` | `True` |
| `uniform_buffer_content_map_enabled` | `True` |
| `parameter_blocks_and_native_uniform_buffers_requested` | `True` |

Machine manifest: `D:\UnrealUnderstanding-Radiosity\output\capture-manifest.json`  
Session: `D:\UnrealUnderstanding-Radiosity\output\radiosity\run-20261115-105951.json`  
Timeline: `D:\UnrealUnderstanding-Radiosity\output\radiosity\timeline-20261115-105951.json`

