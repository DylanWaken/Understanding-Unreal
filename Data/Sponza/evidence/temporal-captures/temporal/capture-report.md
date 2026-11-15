# Capture report — real Sponza placement and temporal evidence

Status: **pass with a recorded camera-plan correction**. The main nine-dump run
completed and captured the requested selected resources, but every clean camera
view at the planned `+150 cm` offset rendered empty. A second, bounded retry
tested the rendered view, rejected `+150 cm`, accepted `-150 cm`, and produced
valid camera states. The failed views are listed in §6 rather than presented as
valid evidence.

Workspace: `D:\UnrealUnderstanding-Temporal`
Map: `/Game/Labs/SponzaTeaching`
Resolution: `1280 x 720`, screen percentage 100
GPU: NVIDIA RTX PRO 6000 Blackwell Workstation Edition (97,887 MiB)

## 1. Scene identity

| Item | Evidence |
|---|---|
| Map SHA256 before/after main run | `3ef49eddd474fc16e6129dac3bcbb16bb4937529d9213296a865d368ede72c64` / same |
| Map SHA256 before/after retry | same / same |
| `final-setup.json` saved hash | `3ef49edd…72c64` (matches) |
| Engine BuildId | `b4effda9-1b7d-4e62-b9a6-cb99e41a70c9` |
| Project module BuildId | `b4effda9-1b7d-4e62-b9a6-cb99e41a70c9` (matches engine) |
| Launchers | `run_gpu_dump.ps1` and `run_unreal_hidden.ps1` byte-identical to the Sponza copies |
| Map saved? | no; hash unchanged and `map_unchanged=true` in both sessions |
| Restored camera/sun/exposure | `camera_restored=true`, `sun_restored=true`, `exposure_restored=true` in both sessions |

The editor created `Saved`, `Intermediate` and `DerivedDataCache` in the copied
project during the runs. Those runtime directories were archived to
`output/logs/project-runtime-20261115-101923/` and removed from `project/`, so
the prepared core-only copy is restored. `python scripts\validate_capture_plan.py`
then returned `ok=true` (map hash, BuildId, launchers, source names and budget
all still valid). The map itself was never rewritten or saved.

## 2. Runs, timing and budget

| Run | Purpose | In-script elapsed | Launcher wall | Bridge draws | Dumps | Files | Raw bytes | Raw GiB |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `20261115-101057` | baseline, light step, planned camera | 81.891 s | 132.6 s | 428 | 9 | 18,321 | 19,857,586,378 | 18.4938 |
| `20261115-101923` | camera retry, narrow dump | 24.500 s | 50.9 s | 188 | 1 | 1,904 | 1,137,879,723 | 1.0597 |
| **total** | | | | | **10** | **20,225** | **20,995,466,101** | **19.5536** |

The 20 GiB cap is `21,474,836,480` bytes. Total selected raw is
`20,995,466,101` bytes, i.e. `19.5536 GiB`, under the cap. The main estimate was
`16.0461 GiB` for nine full-filter dumps; actual main raw was `18.4938 GiB` and
still fit. The retry's narrow filter was chosen specifically to fit the
remaining `1.4464 GiB`.

Both launcher records report `python_error_detected=false`, `dump_status=ok`,
and matching engine/project BuildIds. The launcher's `exit_code` field is `null`
(observed PowerShell `Start-Process` behaviour); no Unreal process remained
after either run. Run status is taken from the session/audit evidence, not from
that field.

Per-dump `seconds` is `0.0` in both sessions: `wait_dump()` found `Status.txt`
already `ok` on its first poll. This is not a measurement of DumpGPU's duration;
no per-dump duration is claimed. Only the whole-run wall times above are timing
claims.

## 3. States and actual update indices

Targets are bridge viewport draws after the light change or camera move. `actual`
is recovered after each dump from the `Frame N` scopes in `Base/Passes.json`
matched to the bridge/engine-frame timeline. The dump is armed two draws before
the requested target; DumpGPU captures the following frame, which in this build
was the filler draw. Thus for targets 8/32/64 the actual dumped update is one
draw **before** the requested target (`7/31/63`). The lit screenshot is still
taken at the requested draw. Nothing is relabelled: the session and manifest
carry both `target_update` and `actual_update`. The script's `target_matched`
flag uses its ±1 rule; the raw offset is visible in the table.

### Main run `20261115-101057`

| Dump | Phase | Requested | Actual | Offset | Engine frame | Dump bytes | Present/Missing | Status |
|---|---|---:|---:|---:|---:|---:|---|---|
| `baseline` | baseline | — | 3 | — | 127 | 2,989,801,808 | 59/16 | ok |
| `lightstep-001` | light step | 1 | 1 | 0 | 169 | 1,998,063,689 | 59/16 | ok |
| `lightstep-008` | light step | 8 | 7 | −1 | 178 | 1,981,110,588 | 59/16 | ok |
| `lightstep-032` | light step | 32 | 31 | −1 | 205 | 1,980,570,491 | 59/16 | ok |
| `lightstep-064` | light step | 64 | 63 | −1 | 240 | 1,980,536,932 | 59/16 | ok |
| `camera-001` | camera (+150) | 1 | 1 | 0 | 350 | 2,988,406,915 | 59/16 | ok; view empty |
| `camera-008` | camera (+150) | 8 | 7 | −1 | 359 | 1,979,772,732 | 59/16 | ok; view empty |
| `camera-032` | camera (+150) | 32 | 31 | −1 | 386 | 1,979,685,830 | 59/16 | ok; view empty |
| `camera-064` | camera (+150) | 64 | 63 | −1 | 421 | 1,979,637,393 | 59/16 | ok; view empty |

The main run also recorded a `restored` clean state and a `final` clean state
after restoring the original camera, sun and exposure. No skips and no script
errors were recorded; `session.errors=[]`.

### Camera retry `20261115-101923`

| State | Requested | Actual | Offset | Engine frame | Evidence |
|---|---:|---:|---:|---:|---|
| `camera-001` | 1 | 1 | 0 | clean record | valid lit + depth |
| `camera-008` | 8 | 8 | 0 | clean record | valid lit + depth |
| `camera-032` | 32 | 32 | 0 | clean record | valid lit + depth |
| `camera-064` (narrow dump) | 64 | 63 | −1 | 137 | valid lit + depth; 32/43 resources |

The retry used `-150 cm` (actual readback `[-850, -150, 90]`, pitch `13`, yaw
`0`, HFOV `70`). Its 12 clean records and 9 debug observations contain **no**
empty views.

## 4. Selected DumpGPU data

### 4.1 Evidence rule

A resource counts as present when either (a) `DumpReadbackStatus.json` has a
`Dumped` row with bytes, or (b) a real `Resources/<UniqueResourceName>…bin` file
exists. DumpGPU's `SkipTracking` diagnostic path writes the buffer file with a
zero producer pointer and does not append a readback-status row; the file is
still a real readback. The audit records which evidence applies per version.
`selected_resources.missing` means *no descriptor and no file* or *descriptor
without either payload*; it is not a claim that the engine has no such concept.

### 4.2 Coverage by family

Main full-filter dumps (all nine have the same 59/75 coverage):

| Family | Present | Missing |
|---|---:|---|
| Screen probes | **31/33** | `ScreenProbeIrradianceWithBorder`, `ScreenProbeExtraAOWithBorder` |
| World cache | **11/16** | `ProbeValid`, `FinalIrradianceAtlas`, `ProbeOcclusionAtlas`, `FinalSkyVisibilityAtlas`, `RadianceProbeSH_PDF` |
| Card radiosity | **6/13** | `TraceRadianceAtlas`, `TraceHitDistanceAtlas`, `RadiosityTileAllocator`, `RadiosityTiles`, `RadiosityIndirectArgs`, `RadiosityVisualizeProbeAllocator`, `RadiosityVisualizeProbeData` |
| Cards/pages | **9/11** | `PrimitiveGroup`, `CardPageLastUsedBuffer` |
| View + depth | **2/2** | — |

Critical files present in every main dump:

* `Lumen.ScreenProbeGather.ScreenProbeTranslatedWorldPosition`,
  `ScreenProbeWorldNormal`, `ScreenProbeRadianceSHAmbient`,
  `ScreenProbeRadianceSHDirectional`, `ScreenProbeDisocclusion`,
  `HistoryFastUpdateMode_NumFramesAccumulated`;
* `Lumen.RadianceCache.RadianceProbeIndirectionTexture`, `ProbeWorldOffset`,
  `ProbeAllocator`, `ProbeFreeList`, `ProbeFreeListAllocator`,
  `ProbeLastUsedFrame`, `ProbeLastTracedFrame`, `FinalRadianceAtlas`,
  `DepthProbeAtlasTexture`;
* `Lumen.Radiosity.ProbeSHRedAtlas`, `ProbeSHGreenAtlas`, `ProbeSHBlueAtlas`,
  `NumFramesAccumulatedAtlas`, `FilteredTraceRadianceAtlas`,
  `SceneIndirectLighting`;
* `Lumen.Cards`, `Lumen.PageTable`, `Lumen.PageBuffer`, `Lumen.MeshCards`,
  `Lumen.CardPageHighResLastUsedBuffer`, `Lumen.CardPageTileAllocator`,
  `Lumen.CardTiles`, `Lumen.CardTileDepthRanges`, `Lumen.CardCaptureRects`;
* `ViewData.PrimaryViews` (3 versions × 1056 bytes) and `SceneDepthZ`.

The absent resources are reported by exact name in
`output/temporal/resource-inventory-20261115-101057.json`. The radiosity
trace/tile resources and `RadiosityVisualizeProbeData` were not created in these
clean frames (the probe visualizer was off during the dumps; radiosity was in a
steady/integrate state rather than a per-frame trace). That is an observed frame
state, not a readback failure. The card-radiosity SH family itself is captured.

### 4.3 Retry narrow dump

The retry dump covers 32/75 names, including the camera-critical:

* `ViewData.PrimaryViews` (3 × 1056 bytes), `SceneDepthZ`;
* `Lumen.Cards` (3 versions), `Lumen.PageBuffer` (3 versions);
* `ScreenProbeRadianceSHAmbient`, `ScreenProbeRadianceSHDirectional`,
  `ScreenProbeSceneDepth`, `ScreenProbeDisocclusion`, `ScreenProbeMoving`,
  `HistoryFastUpdateMode_NumFramesAccumulated`, `DiffuseIndirect`;
* world-cache `ProbeWorldOffset`, `ProbeAllocator`, `ProbeFreeList`,
  `ProbeFreeListAllocator`, `ProbeLastUsedFrame`, `ProbeLastTracedFrame`,
  `RadianceProbeIndirectionTexture`;
* `Lumen.Radiosity.ProbeSHRedAtlas`, `ProbeSHGreenAtlas`, `ProbeSHBlueAtlas`,
  `FilteredTraceRadianceAtlas`.

It deliberately omits screen-probe positions/normals and the page-table/card
demand buffers to stay inside the remaining budget
(`resource-inventory-20261115-101923.json` lists every absent name). It is a
camera-state data point, not a replacement for the full-filter light-step
sequence. The four main-run camera dumps do contain
`ScreenProbeTranslatedWorldPosition`, `ScreenProbeWorldNormal` and the rest of
the 59-resource set, but their rendered views are empty (§6); they may be used
as data-only camera-state probes, never as visual evidence, and their camera
offset is the rejected `+150 cm`.

## 5. Images and debug observations

Valid clean lit views: main `baseline`, all four `lightstep-*`, `restored`,
`final`; retry `retry-baseline`, `camera-001/008/032/064`, `retry-final`.
Valid depth views: main `restored` and `final`, and the `debug/*-depth` views
captured with an explicit depth-mode switch; all retry camera states and the
retry final are valid.

Representative metrics (display luma 0–255):

| View | Mean | Std | Bytes | Verdict |
|---|---:|---:|---:|---|
| `images/baseline-lit.png` | 60.186 | 70.118 | 1,800,908 | valid scene |
| `images/lightstep-064-lit.png` | 31.210 | 47.612 | 1,485,381 | valid low-light scene |
| `images/camera-001-lit.png` (retry) | 52.153 | 65.267 | 1,767,356 | valid −150 cm view |
| `images/camera-064-dump-lit.png` (retry) | 55.980 | 64.627 | 1,879,935 | valid −150 cm view |
| `debug/camera-settled-radiosity-probes.png` | 53.906 | 69.867 | 1,703,633 | real probe spheres |
| `debug/camera-settled-worldcache-probes.png` | 44.672 | 64.180 | 1,794,723 | real probe spheres |
| `debug/camera-settled-card-placement.png` | 27.455 | 57.597 | 475,846 | card/Lumen-scene view |
| `debug/camera-settled-depth.png` | 16.158 | 13.655 | 57,740 | valid depth |

The retry radiosity-probe and world-cache-probe images show scene-anchored probe
spheres across the actual card surfaces; they are separate display
observations, not substitutes for the raw buffers.

## 6. Explicitly failed or superseded views

These are **not** presented as valid states. Metrics are from
`audit-20261115-101057.json`; a display-luma std of essentially zero is an empty
frame.

| View | Mean | Std | Reason/replacement |
|---|---:|---:|---|
| `images/camera-001-lit.png` (main +150) | 0.000 | 0.000 | camera inside/behind geometry; superseded by retry `camera-001-lit.png` |
| `images/camera-008-lit.png` (main) | 0.016 | 0.113 | empty; superseded by retry |
| `images/camera-032-lit.png` (main) | 0.676 | 0.551 | empty; superseded by retry |
| `images/camera-064-lit.png` (main) | 1.002 | 0.533 | empty; retry uses `camera-064-dump-lit.png` |
| `debug/camera-settled-probe-history.png` (main) | 43.377 | 0.290 | uniform display, no history signal; superseded by retry |
| `debug/camera-settled-radiosity-probes.png` (main) | 0.000 | 0.000 | empty; superseded by retry |
| `debug/camera-settled-worldcache-probes.png` (main) | 0.000 | 0.000 | empty; superseded by retry |
| `debug/camera-settled-card-placement.png` (main) | 0.000 | 0.000 | empty; superseded by retry |
| `debug/camera-settled-lumen-scene.png` (main) | 0.000 | 0.000 | empty; superseded by retry |
| `debug/camera-settled-surface-cache.png` (main) | 0.289 | 7.155 | effectively empty; superseded by retry |

Two output-path collisions are also recorded:

* `images/baseline-lit.png` was written by the clean baseline capture and then
  again by the baseline dump's lit capture (two different hashes).
* `images/baseline-depth.png` was written by the clean baseline depth capture
  and then overwritten by the baseline dump's lit capture. The valid baseline
  depth view is `debug/baseline-depth.png` (hash `d8a75069…`).

The same pre-fix dump path did not switch to `SceneDepth` mode, so
`images/lightstep-001/008/032/064-depth.png` and `images/baseline-depth.png`
are lit captures, not depth. They are marked
`depth_view_valid=null` / `unverified_pre_fix_capture` in the manifest. Raw
`SceneDepthZ` for every one of those states is present in the corresponding
selected dump. The retry uses the corrected path (`camera-*-depth.png` and
`camera-064-dump-depth.png`), which were verified with a depth-mode switch.

No empty view is accepted silently: the audit recomputes the emptiness test from
the recorded metrics and the manifest carries `lit_view_valid`,
`depth_view_valid`, and `depth_provenance` per state.

## 7. Camera-move correction

The planned move was `+Y 150 cm`. The offline check in
`validate_capture_plan.py` measures **edge** clearance and reported
`22.8023 cm` minimum, so it accepted the move. The runtime five-ray trace also
reported no blocking. Nevertheless the rendered frames at the target were empty:
the camera was inside/behind the right-side geometry; edge distance does not
test whether a point is inside a surface.

The retry therefore tested candidate offsets against the actual rendered frame:

| Candidate | Trace clear | Applied | Mean | Std | Accepted |
|---|---|---:|---:|---:|---|
| `+150 cm` | yes | yes | 0.000 | 0.000 | **no — empty view** |
| `−150 cm` | yes | yes | 54.754 | 64.281 | **yes** |

`−150 cm` was then used for the retry camera states. Its actor and viewport
camera readbacks both report `[-850, -150, 90]`, pitch `13`, yaw `0`, HFOV `70`;
the view is a real shifted view of the same courtyard. This is a plan change:
the original `+150 cm` offset is not usable as visual evidence.

## 8. Counters, matching and limitations

* Bridge draws and engine frames are recorded separately in
  `timeline-20261115-101057.json` and `timeline-20261115-101923.json`.
* `Frame N` scopes in `Base/Passes.json` were matched to the bridge timeline;
  the actual update indices are in §3. Dumps at 8/32/64 are one draw before the
  requested target; this is recorded, not relabelled.
* `graph_count=0` / `has_graph_prologues=false`: the filtered `Passes.json`
  contains only matching passes, so the graph prologue/epilogue scopes are not
  present. Graph identity is therefore taken from the dumped `Frame N` and the
  per-state `ViewData.PrimaryViews`, not from prologue ranges.
* The main run's `dump_status=ok` is DumpGPU's readback status, not a claim that
  the camera view is valid. The empty camera views are listed in §6.
* The 16 absent main-run resources and 43 absent retry resources are exact-name
  records; no resource is invented or substituted.
* `RadiosityVisualizeProbeData` was not captured in the clean dumps (visualizer
  off). The card-radiosity SH atlases, `NumFramesAccumulatedAtlas`,
  `FilteredTraceRadianceAtlas` and `SceneIndirectLighting` are captured. Per
  `capture-spec.md` §7.4 the probe grid can be reconstructed from the captured
  card transforms, but that is a decode-step action and is not claimed here.
* No causal chain is claimed from a single frame; the evidence is the observed
  state sequence with actual update indices and separate debug observations.

## 9. Hand-off files

| Path | Content |
|---|---|
| `output/temporal/run-20261115-101057.json` | main run: 428 draws, 9 dumps, timeline, phases, camera/sun/exposure, map identity |
| `output/temporal/run-20261115-101923.json` | camera retry: probes, chosen `−150 cm`, 1 narrow dump, corrected depth |
| `output/temporal/timeline-*.json` | every bridge draw with both counters |
| `output/temporal/manifest.json` | merged scrubber hand-off; camera states are retry states, original camera states retained as `original_states` with `status=rejected` |
| `output/temporal/manifest-camera-retry-20261115-101923.json` | retry manifest |
| `output/temporal/manifest-20261115-101057.json` | original main manifest (preserved) |
| `output/temporal/audit-20261115-101057.json` | independent resource/image/identity audit |
| `output/temporal/audit-20261115-101923.json` | retry audit (0 empty views) |
| `output/temporal/resource-inventory-*.json` | per-dump present/missing resources with file, bytes and evidence |
| `output/temporal/evidence-hashes.json` | map, inputs, sessions, manifests, per-dump metadata and key image hashes |
| `output/gpu-dump/raw/20261115-101057/` | 9 selected dump directories (18,321 files, 18.4938 GiB) |
| `output/gpu-dump/raw/20261115-101923/camera-064/` | narrow camera dump (1,904 files, 1.0597 GiB) |
| `output/gpu-dump/latest-main-run.json` | preserved launcher record for the nine-dump run |
| `output/gpu-dump/latest-run.json` | retry launcher-compatible latest record |

The merged `manifest.json` has `status=partial` with an explicit `status_note`:
the original camera experiment was rejected and replaced by the retry. Baseline
and light-step states retain their main-run data. Decode should use the retry
camera states and the `depth_provenance` field when selecting depth images.

## 10. Acceptance checks

| Check | Result | Evidence |
|---|---|---|
| Real scene views and selected temporal data captured | **yes** | 9 full-filter dumps over baseline/light step plus 1 narrow camera dump; §3/§4 tables; selected probe SH, world-cache atlases, radiosity SH, cards and View/depth present |
| No empty or clipped diagnostic view silently accepted | **yes** | 10 empty/effectively-empty main camera views listed with metrics in §6; retry rejects `+150` and accepts `−150` with 0 empty views; audit recomputes the test and manifest carries per-state validity |
| Source scene remains unchanged with recorded identity | **yes** | map SHA256 `3ef49edd…72c64` before and after both runs, equals `final-setup.json`; `map_unchanged=true`; no map save |

## 11. Reproduction

```powershell
# main run (already executed; launcher unchanged)
powershell -ExecutionPolicy Bypass -File scripts\run_gpu_dump.ps1 -Workspace D:\UnrealUnderstanding-Temporal
# camera retry (already executed; mode is supplied by environment, launcher unchanged)
$env:ARDA_TEMPORAL_MODE='camera-retry'
powershell -ExecutionPolicy Bypass -File scripts\run_gpu_dump.ps1 -Workspace D:\UnrealUnderstanding-Temporal -Tag camera-retry
# independent audit + manifest count correction
python scripts\audit_capture.py --update-manifest
# static validation
python scripts\validate_capture_plan.py
```

The launcher was not modified. `-ExecutionPolicy Bypass` is a per-process
invocation flag because this host disables script execution by default.
