# Radiosity probe reconstruction from captured card depth and two-band SH

Status: **ok**

Run: `20261115-105951`; five same-process states; all paths read from `D:\UnrealUnderstanding-Radiosity\output\gpu-dump\raw\20261115-105951`.

## 1. What was decoded

For every mapped Lumen card page with an indirect-lighting update, the decoder walks the same probe grid as the radiosity shader:

* page `CoordInCardPage = (ix,iy)*ProbeSpacingInCardTexels + GetProbeJitter(page.IndirectLightingTemporalIndex)`;
* surface-atlas UV `AtlasUV = PhysicalAtlasUVRect.xy + InvPhysicalAtlasSize*(CoordInCardPage+0.5)` (`LumenRadiosity.ush` `GetRadiosityTexel`);
* depth from the captured `Lumen.SceneDepth` PF_G16 4096x4096 atlas at `floor(AtlasUV*4096)`; invalid `65535`/1.0 is rejected;
* card UV `CardUVRect.xy + CardUVTexelScale*(CoordInCardPage+0.5)`;
* `GetCardLocalPosition` (x-flip first) then `world = axis0*local.x + axis1*local.y + axis2*local.z + origin`;
* normal from `Lumen.SceneNormal`; because the capture expanded BC5 to `PF_FloatRGBA`, R/G are decoded BC5 values in [0,1], Z is reconstructed as `sqrt(1-|xy|^2)`, then rotated by the card basis and normalised;
* four SH coefficients per channel from `Lumen.Radiosity.ProbeSH{Red,Green,Blue}Atlas` half4 at `(PhysicalAtlasCoord + cx) >> ProbeSpacingInCardTexelsDivideShift`.

Probes with uninitialised `NumFramesAccumulatedAtlas` (raw byte 0), a page with `LastIndirectLightingUpdateFrameIndex == 0`, an invalid depth texel, or an out-of-bounds atlas address are rejected. All rejected counts are in the state records.

Jitter anchor and accumulated history: the captured `FixedJitterIndex` is -1 in all states, so `GetProbeJitter` uses `page.IndirectLightingTemporalIndex % MaxFramesAccumulated` (MaxFramesAccumulated=4) as the jittered grid anchor for the current frame. `LumenRadiosityConvertToSH` writes the four coefficients from that frame's `TraceRadianceAtlas`; the SH atlas is not itself temporally accumulated. Temporal accumulation happens in `LumenRadiosityIntegrateCS`, which lerps `HistoryRadiosity` with the new radiosity by `alpha = 1/(1+NumFramesAccumulated)` and writes the counter atlas. A probe's position and jitter therefore come from the current grid anchor at the page's last update, its four coefficients are the last hemispherical projection written at that atlas texel, and `nf_raw`/`last_indirect` are the recorded counter and update fields. Inactive probes keep the last written SH, which the decoder does not assert equals the current frame's trace.

## 2. Constant-input SH normalisation (no 4, no 4pi)

`LumenRadiosityConvertToSH` builds `IrradianceSH = (1/N) * sum(SHBasisFunction(dir) * TraceRadiance / PDF)` with `PROBE_HEMISPHERE_UNIFORM` and `PDF = 1/(2*pi)` (`MonteCarlo.ush`). `TraceRadiance` was written as `Radiance * CachedLightingPreExposure` (`LumenRadiosity.usf`). Evaluation is `TexelRadiance = max(0, DotSH(IrradianceSH, CalcDiffuseTransferSH(normal, 1)))`, whose band constants are `L0 = pi`, `L1 = 2*pi/3` (`SHCommon.ush`).

Independent constant-hemisphere test: 65536 uniform samples, L=1, exact basis and PDF; reconstructed irradiance `3.141598110876` vs `2*pi^2*(C^2+D^2/3) = 3.141598110878` for the same six-decimal SHCommon.ush literals (relative error `6.460e-13`). The residual versus exact `pi` is `1.737e-06`, attributable to the six-decimal basis literals, not to any 4 or 4*pi factor.

Cache pre-exposure: the captured `Batched lights` payload carries `CachedLightingPreExposure = 0.0625` (FParameters offset 204) in every state. Stored SH is divided by 0.0625 to obtain `sh_coeffs_radiance`; both raw and converted arrays are kept. `e_normal_raw` is `DotSH(raw, transfer)`; `e_normal_radiance = e_normal_raw/0.0625`; `lambert_radiance_unit_albedo = e_normal_radiance/pi`.

## 3. State results

| State | Graph prefix | View frame field | Camera (cm) | Probes | Card tile in capture | Visible | Occluded | Median depth err | p90 | SH nonzero |
|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|
| `baseline` | 0004 | 365 | `[-850.0, 0.0, 90.0]` | 94787 | 3772 | 43815 | 25026 | 0.0088 | 0.1361 | 83428 |
| `light-001` | 0004 | 371 | `[-850.0, 0.0, 90.0]` | 104697 | 11060 | 11354 | 36390 | 0.0149 | 0.1462 | 82207 |
| `light-064` | 0004 | 557 | `[-850.0, 0.0, 90.0]` | 94840 | 4072 | 43983 | 24880 | 0.0088 | 0.1365 | 83527 |
| `camera-001` | 0004 | 757 | `[-850.0, -150.0, 90.0]` | 123316 | 9412 | 48261 | 43710 | 0.0089 | 0.1094 | 107327 |
| `camera-064` | 0004 | 943 | `[-850.0, -150.0, 90.0]` | 104728 | 5532 | 44032 | 38217 | 0.0086 | 0.0692 | 93880 |

`Frame field` is the FPackedView frame counter at byte 1044, not the capture target update. The capture's actual target updates are in the manifest (`baseline` 1, `light-001` 1, `light-064` 63 of target 64, `camera-001` 1, `camera-064` 63 of target 64). Sun 80000 -> 20000 -> 80000 lux (64 warm draws) then camera -Y150 cm, EV100 11.0 fixed, as reported by the capture manifest.

Visible counts vary across states because each state's card/page set and primary-depth visibility differ; the light change, for example, re-captures and updates a different set of pages. The arrays keep every valid probe, and the figures draw only the declared primary-depth-visible subset.

SH corroboration against the captured `SceneIndirectLighting` (surface-texel comparison, not an independent per-probe trace):

| State | samples | Pearson R | Pearson G | Pearson B | median ratio R/G/B |
|---|---:|---:|---:|---:|---|
| `baseline` | 15250 | 0.880 | 0.887 | 0.894 | 1.96 / 1.94 / 1.96 |
| `light-001` | 14560 | 0.983 | 0.989 | 0.992 | 1.00 / 1.00 / 1.00 |
| `light-064` | 14994 | 0.860 | 0.873 | 0.885 | 2.19 / 2.23 / 2.33 |
| `camera-001` | 14714 | 0.884 | 0.890 | 0.896 | 1.55 / 1.53 / 1.52 |
| `camera-064` | 15451 | 0.877 | 0.884 | 0.891 | 1.68 / 1.68 / 1.70 |

These correlations are corroboration only; the captured radiosity atlas is the bilinearly interpolated and weight-normalised surface-texel result, so exact equality is not expected.

Temporal change for stable `(page,card,grid_xy)` keys (descriptive; page allocation is not a persistent identity):

| Pair | common | mean position delta cm | SH mean abs delta | jitter changed | temporal changed | nf changed |
|---|---:|---:|---:|---:|---:|---:|
| baseline vs light-064 | 91809 | 4.44 | 162.60 | 0.66 | 0.91 | 0.03 |
| baseline vs camera-064 | 57773 | 4.04 | 100.82 | 0.85 | 0.93 | 0.02 |
| light-064 vs camera-064 | 57867 | 2.71 | 231.86 | 0.60 | 0.60 | 0.04 |

## 4. Graph/version selection

Each state contains three render-graph prefixes (`0001`, `0003`, `0004`) with three consecutive FPackedView frame fields. The decoder selects the highest-frame prefix and uses only operands from that prefix: `Lumen.Cards`, `Lumen.PageBuffer`, `Lumen.PageTable`, `Lumen.CardTiles` (the 16384-byte CardTileData; the separate 33124-byte static buffer is recorded but not used for placement), `Lumen.SceneDepth`, `Lumen.SceneNormal`, the three SH atlases, `NumFramesAccumulatedAtlas`, `SceneIndirectLighting`, `FilteredTraceRadianceAtlas`, `SceneDepthZ`, `ViewData.PrimaryViews`, and the `BuildRadiosityTiles`/`ConvertToSH`/`Integrate`/`Batched lights` parameter payloads. Every operand comes from that prefix.

The 16 captured required resources do not include `RadiosityTileData`/`RadiosityTileAllocator`, so the decoder cannot know the exact per-frame radiosity tile list. `card_tile_in_capture` marks the probes whose 8x8 card tile appears in the captured `Lumen.CardTiles` buffer; the numeric array itself is the complete set of mapped, indirect-initialised, depth-valid probes in the persistent atlas.

* `baseline`: prefix `0004`; graph variants [{"prefix": "0001", "view_uid": "00010000026046775e60", "view_file": "00010000026046775e60.v00000000000000000000.bin", "frame_index_field": 363, "camera_world_cm": [-850.0, 0.0, 90.0]}, {"prefix": "0003", "view_uid": "0003000002604473e440", "view_file": "0003000002604473e440.v00000000000000000000.bin", "frame_index_field": 364, "camera_world_cm": [-850.0, 0.0, 90.0]}, {"prefix": "0004", "view_uid": "000400000260466ae440", "view_file": "000400000260466ae440.v00000000000000000000.bin", "frame_index_field": 365, "camera_world_cm": [-850.0, 0.0, 90.0]}].
* `light-001`: prefix `0004`; graph variants [{"prefix": "0001", "view_uid": "000100000260656d5e60", "view_file": "000100000260656d5e60.v00000000000000000000.bin", "frame_index_field": 369, "camera_world_cm": [-850.0, 0.0, 90.0]}, {"prefix": "0003", "view_uid": "0003000002604676e440", "view_file": "0003000002604676e440.v00000000000000000000.bin", "frame_index_field": 370, "camera_world_cm": [-850.0, 0.0, 90.0]}, {"prefix": "0004", "view_uid": "0004000002604849e440", "view_file": "0004000002604849e440.v00000000000000000000.bin", "frame_index_field": 371, "camera_world_cm": [-850.0, 0.0, 90.0]}].
* `light-064`: prefix `0004`; graph variants [{"prefix": "0001", "view_uid": "000100000260485d5e60", "view_file": "000100000260485d5e60.v00000000000000000000.bin", "frame_index_field": 555, "camera_world_cm": [-850.0, 0.0, 90.0]}, {"prefix": "0003", "view_uid": "0003000002604448e440", "view_file": "0003000002604448e440.v00000000000000000000.bin", "frame_index_field": 556, "camera_world_cm": [-850.0, 0.0, 90.0]}, {"prefix": "0004", "view_uid": "00040000025d2cf5e440", "view_file": "00040000025d2cf5e440.v00000000000000000000.bin", "frame_index_field": 557, "camera_world_cm": [-850.0, 0.0, 90.0]}].
* `camera-001`: prefix `0004`; graph variants [{"prefix": "0001", "view_uid": "00010000026048495e60", "view_file": "00010000026048495e60.v00000000000000000000.bin", "frame_index_field": 755, "camera_world_cm": [-850.0, -150.0, 90.0]}, {"prefix": "0003", "view_uid": "00030000026048e2e440", "view_file": "00030000026048e2e440.v00000000000000000000.bin", "frame_index_field": 756, "camera_world_cm": [-850.0, -150.0, 90.0]}, {"prefix": "0004", "view_uid": "000400000260467be440", "view_file": "000400000260467be440.v00000000000000000000.bin", "frame_index_field": 757, "camera_world_cm": [-850.0, -150.0, 90.0]}].
* `camera-064`: prefix `0004`; graph variants [{"prefix": "0001", "view_uid": "0001000002604b0a5e60", "view_file": "0001000002604b0a5e60.v00000000000000000000.bin", "frame_index_field": 941, "camera_world_cm": [-850.0, -150.0, 90.0]}, {"prefix": "0003", "view_uid": "0003000002607507e440", "view_file": "0003000002607507e440.v00000000000000000000.bin", "frame_index_field": 942, "camera_world_cm": [-850.0, -150.0, 90.0]}, {"prefix": "0004", "view_uid": "0004000002604646e440", "view_file": "0004000002604646e440.v00000000000000000000.bin", "frame_index_field": 943, "camera_world_cm": [-850.0, -150.0, 90.0]}].

## 5. Figures

Figures are standalone RGB composites with no baked text. Legends, colours, scales and root prose live in `publish/data/figure-legend.json` and this report.

* `placement-<state>.png`: native `Base/Screenshot.png` plus visible probes coloured by normal class (floor/up green, ceiling/down orange, wall X blue, wall Y red), with two fixed-frame orthographic panels (XY and XZ) showing all valid probes. The panels use fixed world bounds and a white camera marker.
* `placement-xray-<state>.png`: visible probes cyan, hidden probes magenta `x`. Hidden here means the probe failed the primary-depth test; it is still in the array.
* `sh-lobes-<state>.png`: native screenshot plus up to 10 visible probes with projected 3D SH4 response lobes. Lobe radius is fixed at 75.0 cm and colour/radius use the global fixed radiance scale below; zero responses are clamped to zero and values above the scale saturate.

Native background: for every state, `Base/Screenshot.png` SHA-256 equals the capture manifest `native_screenshot.sha256` and `same_frame_as_dump` is true. The composites therefore use the native same-frame view, not a post-lit or depth-mode image.

Fixed scales across states: `{"lobe_radiance_max": 20000.0, "radiance_units": "unit-albedo Lambertian outgoing radiance = e_normal_radiance/pi, scene-referred after /CachedLightingPreExposure", "zero_clamp": "evaluated SH response is clamped to >= 0 before colour/radius", "saturation": "values above lobe_radiance_max saturate; no per-state or per-probe normalisation", "lobe_radius_cm": 75.0, "placement_dot_radius_px": 1, "plan_bounds_cm": {"x": [-1536.7568115234376, 1439.9265380859374], "y": [-946.2457153320313, 884.3408325195312], "z": [-101.15400085449215, 1143.5465866088869]}}`.

Depth test: `view_z <= near/SceneDepthZ + max(10.0 cm, 0.02*scene_depth_cm)`, sampled at `floor(projected pixel)` on the captured 1280x888 primary depth. This is the declared tolerance; no per-state fitting.

## 6. Source anchors and limits

* The probes visibly follow walls, floor, arches and curtains in the composite; the numeric arrays cover the whole captured card set, not just the drawn subset.
* `Lumen.SceneNormal` was captured as `PF_BC5` expanded to `PF_FloatRGBA`; the stored values are decoded numeric values, not the original BC bits. X/Y are source-decoded; Z is reconstructed from the unit-length constraint.
* `Lumen.SceneDepth` is the native `PF_G16` storage (`NativeFormatExpanded=False`, `StoredBytesAreNativeStorage=True`, 33,554,432 bytes); `Lumen.SceneNormal` is the exporter-expanded `PF_FloatRGBA` readback (`NativeFormatExpanded=True`, `StoredBytesAreNativeStorage=False`, 134,217,728 bytes). These format contracts are copied unchanged into `manifest.json` under each state's `formats`.
* The capture has no independent per-ray trace atlas for radiosity: `FilteredTraceRadianceAtlas` is filtered, not raw per-hemisphere trace radiance. The SH normalisation is therefore source-derived and constant-input tested. `SceneIndirectLighting` correlation is corroboration only, not equality.
* The decoder knows trace origins only as far as the recorded probe coords: the shader adds `SurfaceBias` along the normal and a second bias along each ray (`LumenRadiosity.usf`), and those offsets are not present in the dump.
* The capture's `r.RHI.UniformBufferContentMap.Enable` readback is true in this run, but the decoder does not rely on uniform-buffer contents: every parameter value comes from a dumped pass parameter payload (`CachedLightingPreExposure` at FParameters offset 204 and the nested `FLumenRadiosityTexelTraceParameters`). If the flag were unset, the decoder would not fabricate uniform contents.
* Raw source SHA-256 before/after is recorded in `checks.json` and `manifest.json`; no raw file was written or modified.

## 7. Files

* `decoder.py` - single reproducible decoder/checker/figure writer.
* `publish/data/schema.json`, `publish/data/probes-<state>.npz`, `publish/data/probes-<state>.json`, `publish/data/probes-sample.json`, `publish/data/figure-legend.json`, `publish/data/checks.json`.
* `publish/figures/placement-<state>.png`, `placement-xray-<state>.png`, `sh-lobes-<state>.png`.
* `checks.json`, `manifest.json`.
