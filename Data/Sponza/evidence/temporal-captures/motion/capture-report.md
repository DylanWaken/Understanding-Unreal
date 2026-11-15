# Capture report — same-run Sponza camera-motion states

Status: **pass with one recorded capture-plan reduction**. One Unreal process
captured the accepted warm baseline, the direct `-150 cm` Y camera move, the
first rendered camera update (1) and the settled camera state (actual update 63).
All three selected dumps use the full 136-pair filter and carry the same
59/75 selected resources. Target update 8 was deliberately deferred until after
the settled dump and then skipped because it was already past; four full-filter
dumps at this valid camera view are ~2.99 GB each and would exceed the approved
10 GiB workspace cap.

Workspace: `D:\UnrealUnderstanding-Motion`
Run: `20261115-104035`
Map: `/Game/Labs/SponzaTeaching`
Resolution: `1280 x 720`, screen percentage 100
GPU: NVIDIA RTX PRO 6000 Blackwell Workstation Edition (97,887 MiB)

## 1. Scene identity and restoration

| Item | Evidence |
|---|---|
| Map SHA256 before / after | `3ef49eddd474fc16e6129dac3bcbb16bb4937529d9213296a865d368ede72c64` / same |
| `final-setup.json` saved map hash | same `3ef49edd…72c64` |
| Map saved? | **no**; the script has no save call, `map_unchanged=true` |
| Engine BuildId | `b4effda9-1b7d-4e62-b9a6-cb99e41a70c9` |
| Project module BuildId | `b4effda9-1b7d-4e62-b9a6-cb99e41a70c9` (matches engine) |
| Launchers | `run_gpu_dump.ps1` `15F79B79…634DF5` and `run_unreal_hidden.ps1` `FE2F14A3…A139` byte-identical to Sponza |
| Camera restored in memory | `camera_restored=true`; final readback `[-850, 0, 90]`, pitch 13, yaw 0, HFOV 70 |
| Sun restored in memory | `sun_restored=true`; `80000` lux, pitch `-75`, yaw `-35` |
| Exposure restored in memory | `exposure_restored=true`; EV100 min=max=11.0, bias 0, histogram pinned |
| Retained core copy | 213 Content + 2 Config + 4 Source + 3 Binaries files match the retained project; `.uproject` matches |

Unreal created `Saved`, `Intermediate` and `DerivedDataCache` inside the copied
project during the run. They were left in place as required by this step; root
cleans them later. No runtime folder was archived or copied back over the scene.

## 2. Run and budget

| Quantity | Value |
|---|---:|
| Launcher wall time | `54.2 s` |
| In-script elapsed | `37.078 s` |
| Bridge draws | `243` |
| Timeline entries | `248` |
| Selected dumps | `3` |
| Dump files | `9,626` |
| Dump raw bytes | `8,972,216,604` (`8.3560 GiB`) |
| Workspace raw cap | `10 GiB` (`10,737,418,240` bytes) — respected |
| Existing Temporal raw | `20,995,466,101` (`19.5536 GiB`) |
| Cumulative raw | `29,967,682,705` (`27.9096 GiB`) |
| Cumulative cap | `35 GiB` (`37,580,963,840` bytes) — respected |

Launcher record: `D:\UnrealUnderstanding-Motion\output\gpu-dump\logs\gpu-dump-20261115-104022-launcher.json`
(`python_error_detected=false`, `dump_status=ok`, engine/project BuildId match).
The launcher's `exit_code` is `null`, which is the observed PowerShell
`Start-Process` behaviour; no Unreal process remained after the run.

### First attempt (superseded, no longer counted as raw evidence)

Run `20261115-103742` captured baseline + camera 1 + camera 7, but the budget
guard skipped camera-064 because the valid-view dumps are ~2.99 GB each. Its
session, timeline and manifest are retained as
`output/temporal/run-20261115-103742.json`,
`output/temporal/timeline-20261115-103742.json` and
`output/temporal/manifest-20261115-103742.json`, with a compact summary at
`output/temporal/superseded-attempt-20261115-103742.json`. Its raw directory
(8,971,992,023 bytes) was removed only after the replacement run reported all
required states `ok`, so the workspace stays inside the 10 GiB cap. No source raw
file from the Sponza source tree was touched.

## 3. States, actual frames and exact paths

Targets are bridge viewport draws after the direct camera move. The selected dump
is armed two draws before the requested target; DumpGPU then captures the next
frame. The actual state is recovered from the `Frame N` scopes in
`Base/Passes.json` matched to the bridge timeline.

| Label | Stage | Requested | Actual | Dump engine frame | Dump path | Files | Bytes | Present / Missing | Status |
|---|---|---:|---:|---:|---|---:|---:|---|---|
| `baseline` | baseline | — | 3 | 127 | `output\gpu-dump\raw\20261115-104035\baseline` | 3,199 | 2,990,620,330 | 59 / 16 | ok |
| `camera-001` | early | 1 | 1 | 133 | `output\gpu-dump\raw\20261115-104035\camera-001` | 3,212 | 2,990,386,206 | 59 / 16 | ok |
| `camera-064` | settled | 64 | 63 | 198 | `output\gpu-dump\raw\20261115-104035\camera-064` | 3,215 | 2,991,210,068 | 59 / 16 | ok |

The settled dump is update **63**, the nearest actual dumped draw to the requested
64, exactly as the reference `dump_state` timing predicts. Nothing is relabelled:
the session and manifest carry both `target_update` and `actual_update`.

Target 8 was attempted only after the settled state. By then `phase_draw=65`, so
the script recorded the skip `already past target when reached` rather than
silently omitting it. The required same-run states are baseline, early (1) and
settled (63); the optional target 8 is not claimed.

## 4. Camera, sun and exposure

| Item | Value |
|---|---|
| Start camera | `[-850, 0, 90]` cm, pitch 13, yaw 0, roll 0, HFOV 70 |
| Applied camera | `[-850, -150, 90]` cm, pitch 13, yaw 0, roll 0, HFOV 70 |
| Move | direct `-150 cm` Y; **no candidate sweep or camera exploration** |
| Actor readback | `[-850, -150, 90]`, pitch 13, yaw 0, HFOV 70 |
| Piloted viewport readback | `[-850, -150, 90]`, pitch 13, yaw 0, HFOV 70; `pilot_matches=true` |
| Prior validation | Temporal run `20261115-101923` accepted `-150 cm` after the planned `+150 cm` view rendered empty |
| Sun (all states) | `Sun_Sponza`, `80000.0` lux, pitch `-75`, yaw `-35`, 5500 K, atmosphere sun light |
| Exposure (all states) | EV100 `11.0` min=max, bias `0.0`, histogram pinned; no adaptation |
| Final camera | restored to `[-850, 0, 90]`, pitch 13, yaw 0, HFOV 70 |

The camera readbacks in the baseline and final clean records are the original
pose; the three camera-state records are the moved pose. The map was not saved.

## 5. Images: native raw frame vs later bridge observations

The **native** image is `Base/Screenshot.png` written by DumpGPU for the dumped
frame itself. It is copied to the run-specific
`output\temporal\raw-screenshots-20261115-104035\` for convenient display, with
its original path and hash recorded.

The **experimental** lit and depth PNGs are `BRIDGE.capture_viewport()` calls in
later engine frames. Their offsets from the dump frame are recorded explicitly:

* experimental lit: phase-draw offset `0`, engine-frame offset `+1`;
* experimental depth: phase-draw offset `+1`, engine-frame offset `+3`;
* native screenshot: same dump frame (`same_frame_as_dump=true`).

| State | Native raw-frame mean/std | Native bytes / SHA256 | Experimental lit mean/std | Lit bytes / SHA256 | Experimental depth mean/std | Depth bytes / SHA256 |
|---|---|---:|---|---:|---|---:|
| baseline | 64.089 / 69.861 | 1,831,985 / `4d851a18…541111` | 60.475 / 70.015 | 1,803,370 / `696f3312…d49763` | 17.774 / 12.391 | 65,554 / `d8a75069…a52d29` |
| camera-001 | 39.075 / 65.579 | 1,123,970 / `0ea030c0…407979` | 40.433 / 64.469 | 1,550,903 / `b13d58d0…f2d547` | 16.158 / 13.655 | 57,740 / `510b3ff3…cf1716` |
| camera-064 | 58.744 / 64.436 | 1,898,869 / `4cde4731…093882` | 56.301 / 64.641 | 1,881,655 / `d74e81cb…247725` | 16.158 / 13.655 | 57,740 / `510b3ff3…cf1716` |
| final (restored) | — | — | 69.440 / 69.572 | 1,906,241 / `6331418c…ef2db6` | 17.774 / 12.391 | 65,554 / `d8a75069…a52d29` |

All displayed views are nonempty: `empty_capture_count=0` across native, clean
lit, clean depth and the nine debug views. The camera-001 and camera-064 depth
images have the same hash and metrics because the camera is identical in both
states; their lit/native images differ, which is the temporal signal rather than
a camera change. The baseline and final depth images also match after the
in-memory restore.

The corrected depth path (`capture_state_images` / `dump_state` switching to
`SceneDepth` buffer visualization and back) was used. The raw same-frame depth is
`SceneDepthZ` inside each selected dump; the PNG is a display-encoded view, not
linear depth.

### Post-settled debug observations

These are separate display observations rendered after the settled dump
(phase 68–92, engine frames 205–237). They are **not** same-frame raw buffers and
are not used as projection sources. All are nonempty.

| View | Phase draw | Engine frame | Mean / Std | Bytes | Valid |
|---|---:|---:|---|---:|---|
| `camera-settled-probe-placement` | 68 | 205 | 55.654 / 74.705 | 1,805,459 | yes |
| `camera-settled-probe-history` | 71 | 209 | 50.641 / 11.953 | 579,120 | yes |
| `camera-settled-radiosity-probes` | 74 | 213 | 54.243 / 69.958 | 1,713,975 | yes |
| `camera-settled-radiosity-history` | 77 | 217 | 163.413 / 39.100 | 139,637 | yes |
| `camera-settled-worldcache-probes` | 80 | 221 | 44.491 / 64.318 | 1,789,249 | yes |
| `camera-settled-card-placement` | 83 | 225 | 27.970 / 58.496 | 478,758 | yes |
| `camera-settled-lumen-scene` | 86 | 229 | 26.791 / 57.747 | 474,443 | yes |
| `camera-settled-surface-cache` | 89 | 233 | 39.904 / 64.258 | 485,873 | yes |
| `camera-settled-depth` | 92 | 237 | 16.158 / 13.655 | 57,740 | yes |

Files: `output\temporal\debug-20261115-104035\<state>-<view>.png`.

## 6. Selected buffer coverage

Every state was captured with the full **136** filter pairs and yields **59/75**
selected resource names with **16** honest absences. The absences are exact
names with no RDG descriptor in the selected frame; they are not readback
failures and are not replaced by schematic data.

| Family | Present | Missing |
|---|---:|---|
| Screen probes | **31/33** | `ScreenProbeIrradianceWithBorder`, `ScreenProbeExtraAOWithBorder` |
| World cache | **11/16** | `ProbeValid`, `FinalIrradianceAtlas`, `ProbeOcclusionAtlas`, `FinalSkyVisibilityAtlas`, `RadianceProbeSH_PDF` |
| Card radiosity | **6/13** | `TraceRadianceAtlas`, `TraceHitDistanceAtlas`, `RadiosityTileAllocator`, `RadiosityTiles`, `RadiosityIndirectArgs`, `RadiosityVisualizeProbeAllocator`, `RadiosityVisualizeProbeData` |
| Cards/pages | **9/11** | `PrimitiveGroup`, `CardPageLastUsedBuffer` |
| View + depth | **2/2** | — |

Critical presence in **all three** dumps:

* screen probes: `ScreenProbeTranslatedWorldPosition` (967,680 bytes),
  `ScreenProbeWorldNormal` (80,640), `ScreenProbeSceneDepth`, `ScreenProbeWorldSpeed`,
  `ScreenProbeRadiance`, `ScreenProbeRadianceSHAmbient` (80,640),
  `ScreenProbeRadianceSHDirectional` (967,680), `ScreenProbeHitDistance`,
  `ScreenProbeTraceMoving`, `ScreenProbeMoving`, `ScreenProbeDisocclusion`
  (20,160), `ScreenProbeFilteredRadiance`,
  `ScreenProbeFilteredRadianceWithBorder`, `HistoryFastUpdateMode_NumFramesAccumulated`
  (6,819,840), `RayInfosForTracing`, `TraceRadiance`, `TraceHit`, `DiffuseIndirect`,
  `LightIsMoving`, adaptive-probe data/indices/header/mask, `BRDFProbabilityDensityFunctionSH`,
  `LightingProbabilityDensityFunction`, `CompactedTraceTexelAllocator/Data`,
  `ScreenProbeIndirectArgs`, `IntegrateIndirectArgs`, `IntegrateTileData`.
* world cache: `RadianceProbeIndirectionTexture` (23,224,320 bytes),
  `ProbeWorldOffset` (786,432), `ProbeAllocator` (48), `ProbeFreeList`,
  `ProbeFreeListAllocator`, `ProbeLastUsedFrame` (786,432),
  `ProbeLastTracedFrame` (393,216), `FinalRadianceAtlas` (293,339,136),
  `DepthProbeAtlasTexture` (106,954,752), `ProbeTraceData`, `ProbeTraceAllocator`.
* card radiosity: `ProbeSHRedAtlas`, `ProbeSHGreenAtlas`, `ProbeSHBlueAtlas`
  (25,165,824 each), `NumFramesAccumulatedAtlas` (786,432),
  `FilteredTraceRadianceAtlas` (201,326,592), `SceneIndirectLighting`.
* cards/pages: `Lumen.Cards` (196,608), `Lumen.PageTable` (24,576),
  `Lumen.PageBuffer` (491,520), `Lumen.MeshCards`,
  `Lumen.CardPageHighResLastUsedBuffer`, `Lumen.CardPageTileAllocator`,
  `Lumen.CardTiles`, `Lumen.CardTileDepthRanges`, `Lumen.CardCaptureRects`.
* same-frame projection: `ViewData.PrimaryViews` (3 versions, 3,168 bytes) and
  `SceneDepthZ` (20,459,520 bytes) in every dump.

The card-radiosity SH family is therefore captured; the missing radiosity names
are the trace/tile/visualizer temporaries that the clean frame did not create.
The world-cache indirection, allocator/free-list, last-used/last-traced frames,
final radiance atlas and depth-probe atlas are captured; the missing world-cache
names are the specific atlases/SH-PDF that were not instantiated in this frame.

## 7. Are baseline, early and settled genuinely available?

Yes. They are **full selected raw dumps**, not merely lit screenshots:

* the baseline dump is engine frame 127, actual bridge update 3, with all 59
  present resource names including screen-probe placement/normals/SH,
  disocclusion/history, world-cache indirection/offset/allocator/last-used/
  last-traced/final radiance/depth atlases, card radiosity SH/frames, cards,
  page table, page buffer, `ViewData.PrimaryViews` and `SceneDepthZ`;
* the early camera dump is engine frame 133, actual update 1, with the same 59
  names at the moved camera;
* the settled camera dump is engine frame 198, actual update 63, with the same
  59 names at the moved camera;
* the native `Base/Screenshot.png` of each dump is nonempty and belongs to the
  dump frame; the experimental lit/depth PNGs are nonempty later observations
  with explicit `+1`/`+3` engine-frame offsets.

No resource family was dropped to fit the budget. The only reduction is the
optional target 8 state; the required baseline/early/settled sequence is
complete and same-process.

## 8. Acceptance checks

| Check | Result | Evidence |
|---|---|---|
| Same-run baseline, early and settled have full selected buffer families | **yes** | one process, run `20261115-104035`; baseline/1/63 dumps each 59/75 names, 136 filter pairs, key SH/cache/card/View buffers listed in §6 |
| All displayed scene views nonempty with exact camera and frame identity | **yes** | `empty_capture_count=0`; native/lit/depth/debug metrics in §5; camera readback `[-850,-150,90]`, pitch 13, yaw 0, HFOV 70; dump engine frames 127/133/198 and actual updates 3/1/63 |
| Scene and launchers unchanged with raw budget respected | **yes** | map SHA256 `3ef49edd…72c64` before/after, no save; launchers byte-identical; core copy matches; raw `8.3560 GiB ≤ 10 GiB`, cumulative `27.9096 GiB ≤ 35 GiB` |

## 9. Hand-off files

| Path | Content |
|---|---|
| `output\capture-manifest.json` | canonical machine manifest: exact dump paths, frames, counts, camera/sun/exposure, image hashes/metrics, offsets, missing coverage, acceptance evidence |
| `output\temporal\run-20261115-104035.json` | full session: timeline, dumps, clean/debug records, camera move, restore, map hashes, environment hashes, budget |
| `output\temporal\timeline-20261115-104035.json` | every bridge draw with `phase_draw` and engine frame |
| `output\temporal\manifest-20261115-104035.json` | capture-time manifest |
| `output\temporal\images-20261115-104035\` | experimental lit/depth PNGs |
| `output\temporal\raw-screenshots-20261115-104035\` | copies of the native DumpGPU `Base/Screenshot.png` files |
| `output\temporal\debug-20261115-104035\` | nine post-settled debug views |
| `output\gpu-dump\raw\20261115-104035\` | baseline, camera-001, camera-064 selected dumps |
| `output\gpu-dump\latest-run.json` | launcher-compatible latest record |
| `output\temporal\capture-plan-validation.json` | static validation: map hash, BuildId, launchers, prior camera validation, budget |
| `output\temporal\superseded-attempt-20261115-103742.json` | retained summary of the superseded first attempt |

## 10. Reproduction

```powershell
cd D:\UnrealUnderstanding-Motion
python scripts\validate_capture_plan.py
powershell -ExecutionPolicy Bypass -File scripts\run_gpu_dump.ps1 -Workspace D:\UnrealUnderstanding-Motion
```

The launcher is unchanged and was invoked with the Motion workspace. The capture
script is the workspace copy; it uses the full 136-pair filter, applies the
accepted `-150 cm` camera state directly, and never saves the map.
