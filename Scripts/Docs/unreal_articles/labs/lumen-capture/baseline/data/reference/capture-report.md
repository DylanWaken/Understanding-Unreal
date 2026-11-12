# Reference capture report: existing Lumen laboratory room

Status: **OK** (24/24 checks passed)

This step captured the already-existing `/Game/Labs/LumenRoom` scene in an isolated
copy of `UnrealLab`, once with Lumen and once with Unreal's path tracer. The room,
camera, materials, light and histogram exposure are the original ones. No geometry or
camera was redesigned, and no pixel was repainted or composited.

## Images

| Image | Renderer | Resolution | Samples / bounces | Denoiser | Engine frame | Viewport draw | SHA-256 |
|---|---|---|---|---|---|---|---|
| `captures/lumen-lit.png` | Lumen software tracing (`VMI_Lit`) | 1280x720 | not applicable | not applicable | 291 | 291 | `6b9c1132fc882d65812f4c769be0e14dc5ce82af0bb5e3b61178a7d5448e5076` |
| `captures/pathtracer-256spp-8bounces.png` | Unreal path tracer (`VMI_PathTracing`) | 1280x720 | 256 spp / 8 | off (`r.PathTracing.Denoiser 0`) | 442 | 443 | `1d73c08b47f05df95894c4b506528cc2d31ab86f0cd0080dbd37b4d6b39de051` |

Both are ordinary `FScreenshotRequest` viewport captures with UI disabled. The
path tracer reached sample index **256** in a reported sample count of **256**;
the sample log contains `256/256`. The engine `GetPathTracingSampleIndex()` is
the authoritative sample counter, and it is **not** equal to the number of
explicit viewport draws: 256 samples accumulated across roughly **86 draw
requests** (sample reset/first sample at draw 357, nonzero progress logged from
draw 358, capture draw 443). The viewport draw number counts explicit
`FSceneViewport::Draw` calls, not path-traced samples.

## Scene and camera preservation

The isolated project loads a byte-identical copy of the saved map
(`LumenRoom.umap`, SHA-256 `f7255018f916af33894b1d0bd811b21585dc0151fca33fddc3a538851337e818`). Runtime inventory matching
against the original `scene-inventory.json`: **True**
(10/10 actors, no differences).

- Camera: UE `(0, -400, 165)` cm looking at `(0, 280, 140)` cm, 43 deg vertical FOV
  (70.006 deg horizontal component FOV), 16:9.
- Effective exposure: histogram metering (`AEM_HISTOGRAM`) with EV100 min = max = 4
  and bias 0 on the original unbound PostProcessVolume. This is **not** manual
  exposure mode; `auto_exposure_method` remains `AEM_HISTOGRAM` and the equal
  min/max values pin the histogram-derived exposure to EV100 4. Same volume and
  exposure for both images.
- Light: original ceiling `RectLight`, 2400 lumens, 130x120 cm, at `(0, 270, 298)` cm,
  pitch -90 deg. The saved map's attenuation radius is 1000 cm; that recorded value
  was preserved rather than re-authored.
- Lumen: software tracing, hardware RT off, screen probes and reflection screen
  traces on, surface-cache atlas 4096. Temporal settings were not overridden:
  screen-probe gather max 10 frames, radiosity max 4, reflections max 12.
- Warm-up: 200 initial viewport draws plus 90 settle draws before the Lumen
  screenshot, matching the original `capture.py` procedure.

## Actual runtime renderer route (measured, not inferred from the ini)

These are capture-time console-variable values and pass-name evidence from the
same saved scene, camera, light and render configuration recorded in
`buffers/runtime-actual.json`:

| Item | Actual value |
|---|---|
| RHI / shader platform | D3D12 / PCD3D_SM6, SM6 |
| `r.Substrate` | 0 (legacy GBuffer path; Substrate passes absent) |
| `r.MegaLights.EnableForProject` | 0 (disabled; no MegaLights shading/trace pass) |
| `r.Lumen.HardwareRayTracing` | 0 (software Lumen tracing) |
| `r.Shadow.Virtual.Enable` | 1 (Virtual Shadow Maps enabled) |
| `r.RayTracing` | 1 (project capability on; `r.RayTracing.Shadows=0`) |
| `r.PathTracing` | 1 (path tracer available; it is the active view only for the PT image) |
| Exposure method | histogram `AEM_HISTOGRAM`, min = max = EV100 4, bias 0 |
| Pre-exposure | 0.0625 (`OneOverPreExposure` 16.0) from the captured view uniform buffer |
| Eye-adaptation scale | 0.0625 result / 0.0625 target, average scene luminance 8.37 |

The captured active frame contains software Lumen passes (`TraceScreen`,
`TraceVoxels`, `ScreenProbeIntegrate`, `LumenReflections`, `ReflectionsResolve`)
and Virtual Shadow Map passes. It contains no Substrate pass and no hardware
ray-tracing pass. Three `MegaLights.LightPowerHistoryRatio` clear-buffer passes
remain even though MegaLights is disabled; there is no MegaLights shading pass.
`r.RayTracing=1` alone is not evidence of hardware Lumen: the hardware Lumen
CVar is 0, and the pass list confirms the software path.

## Why this is a path-traced image

The viewport was switched to `VMI_PathTracing` and the engine reported
`is_path_tracing_view_mode: true`, `view_mode_index: 27`. The screenshot was taken
only after `GetPathTracingSampleIndex()` reached **256** and
`GetPathTracingSampleCount()` reported **256**. Settings were applied as logged:

```text
r.PathTracing.MaxBounces = "8"
r.PathTracing.Denoiser = "0"
r.PathTracing.ProgressDisplay = "false"
r.PathTracing.FilterWidth = "3"
r.PathTracing.MISMode = "2"
r.PathTracing.EnableEmissive = "1"
r.PathTracing.EnableReferenceDOF = "0"
r.PathTracing.VisibleLights = "0"
r.PathTracing.SamplesPerPixel = "255"
r.PathTracing.SamplesPerPixel = "256"
```

The first path-tracing frames logged `Ray Tracing PSO is not ready yet, using
fallback` while the RTPSO compiled (engine frame 295). Those frames
are not part of the reference: 64 warm-up draws were discarded, then
`r.PathTracing.SamplesPerPixel` was changed 255 -> 256 to force a sample reset.
No PSO fallback was logged after that reset (reset engine frame 356;
first counted sample draw 357). The 256 samples
therefore accumulated on a ready RTPSO.

## Numerical image checks

| Metric | Lumen | Path tracer | Published original Lumen |
|---|---|---|---|
| Mean luminance (0-255) | 128.29 | 129.10 | 128.35 |
| Luminance std | 93.63 | 93.79 | 93.59 |
| Unique RGB colors | 67211 | 110317 | 67527 |
| Nonblack fraction (lum > 8) | 0.6982 | 0.6872 | 0.6995 |
| Left wall RGB | (231.1, 137.8, 118.0) | (230.8, 140.7, 119.1) | (231.1, 137.8, 118.0) |
| Right wall RGB | (142.9, 215.1, 150.5) | (145.1, 214.1, 149.8) | (142.9, 215.2, 150.5) |

- New Lumen vs published original Lumen: luminance correlation
  **0.999911**, mean absolute RGB
  difference **0.439/255**.
  This confirms the same room, camera and capture procedure.
- New Lumen vs path tracer: correlation
  **0.993135**, mean absolute RGB
  difference **4.693/255**.
  They are clearly the same view but not identical renders.
- Rejection rules applied: exact 1280x720 size, luminance std > 5, nonblack
  fraction > 0.2, >1000 unique colors, red wall on the left and green wall on the
  right, high correlation with the published original, and non-identical PT/Lumen.
  All passed. Full data: `verification.json`.

Visible differences are expected: Lumen shows temporal/screen-probe dithering and
a brighter glossy-block reflection; the path tracer resolves the same geometry and
lighting with Monte Carlo noise and a darker metallic reflection. Neither image is
blank, UI-only, black, or a different camera.

## Limitations

- Editor viewport capture, not Movie Render Queue, a packaged build, or a multi-GPU run.
- The hidden editor viewport is SDR (`GetSceneHDREnabled()` false), so this existing
  screenshot procedure writes display-referred 8-bit PNG. It does **not** produce a
  linear EXR; no EXR is claimed. A linear EXR would require a Movie Render Queue or
  HDR viewport path, which is outside this step.
- The path tracer's 256 spp still contains Monte Carlo noise; no convergence or
  performance claim is made.
- The NNEDenoiser plugin was instantiated by project defaults, but
  `r.PathTracing.Denoiser 0` selected no denoiser. No denoised image is claimed.
- Original repository scripts, module, maps, materials and published images were
  only read/copied; their hashes are recorded in `capture-manifest.json`.

## Artifacts

`scene.json`, `capture-manifest.json`, `verification.json`, `progress.json`,
`run-metadata.json`, `captures/lumen-lit.png`,
`captures/pathtracer-256spp-8bounces.png`, `captures/capture-session.json`,
`logs/capture.stdout.log`, `logs/capture.stderr.log`, `scripts/`, and the isolated
`UnrealLab/` project. Reproduce with `scripts/run_reference.ps1 -Build`.
