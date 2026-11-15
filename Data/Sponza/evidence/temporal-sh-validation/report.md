# Temporal screen-probe SH review

Scope: independent review of `Docs/Unreal/assets/lumen-sponza/temporal/{screen-sh,motion-screen-sh}` and the reusable decoder. No GPU work, no source edits, no published-metadata edits. All writes are under `temporal-sh-review/`.

## Method

- Parsed each raw DumpGPU record directly (`Passes.json`, `ReferencedStructs.json`, `ResourceDescs.json`, `SubresourceLayouts.json`, `StructuresMetadata/*.json`) without importing or executing the published decoder.
- Read `ScreenProbeRadianceSHAmbient` as packed R11G11B10 and `ScreenProbeRadianceSHDirectional` as half4 texels. For each active placement, read L0 from the ambient texel and the eight directional coefficients from the two RGBA16F texels per RGB channel, giving all 27 signed values.
- Read `ScreenProbeTranslatedWorldPosition`, `ScreenProbeSceneDepth`, `ScreenTileAdaptiveProbeHeader`, `NumAdaptiveScreenProbes` and `AdaptiveScreenProbeData`; rebuilt the active set from the captured allocator bound and depth, then projected positions with the captured `TranslatedWorldToClip`, `ViewRectMin` and `ViewSizeAndInvSize`.
- Rebuilt all active sets for baseline (light), lightstep-032 (light) and camera-064 (motion); compared every 27-value record with the published state JSON.
- Independently checked published file hashes, the fixed lobe scales/clamps, the matching identity flags, the actual lobe images, the root UI manifest references, and the reusable CLI.

## Independent raw result

| state | active | uniform | adaptive | stale finite | 27-value comparisons | max abs diff | >1e-6 | uniform center max px | adaptive center max px |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline (light) | 3,706 | 3,569 | 137 | 103 | 100,062 | 5.0e-7 | 0 | 1.0085e-4 | 8.2721e-5 |
| lightstep-032 (light) | 3,686 | 3,569 | 117 | 138 | 99,522 | 5.0e-7 | 0 | 1.1324e-4 | 8.1581e-5 |
| camera-064 (motion) | 3,682 | 3,572 | 110 | 130 | 99,414 | 5.0e-7 | 0 | 1.1347e-4 | 7.8757e-5 |

All 298,998 compared coefficients agree with the published values to the 6-decimal publication rounding. The identity `active + stale + inactive_unlit = finite nonzero storage` holds exactly: 3706+103+31=3840, 3686+138+31=3855, 3682+130+28=3840. Active adaptive count equals the allocator bound in every checked state, so stale finite entries beyond the bound are excluded from the active SH set.

Six probes per state were enumerated with all 27 values: floor uniform atlas [27,42], arcade uniforms [7,16] and [60,16], and adaptive first/middle/last. Their raw/published coefficients, projected screen positions and source identities are in `raw-spotcheck.json`. Source operands are named and distinct per state (SH passes 530, 97, 553; ambient resources `...f16b10`, `...37bdb0`, `...452eb0`), so no id from another dump is reused for the SH data.

## Scales, signs, and display clamp

- screen-sh: single response scale 1.4454, radius 60.0 cm, rosette radius 55 px; motion-screen-sh: single response scale 1.5239, same radius/rosette units.
- `lobes.npz` has shape 5x10x162x3 (light) and 3x10x162x3 (motion), one `lobe_max` value per experiment, and the same value is passed to every state/probe in the drawing code.
- Signed minima are negative (light -0.03150945, motion -0.03988460); `clamped` is exactly `max(signed,0)` and nonnegative; the manifest declares `negative_display_clamp = max(0, signed response)` and that stored coefficients themselves are not clamped.
- Negative signed fractions (0.02580247 / 0.03580247) and display-saturated fractions (0.00975309 / 0.00967078) match `summary.json`.
- The light/motion scales differ, but each is fixed within its own time series and declared in the UI caption; they are not directly comparable across experiments.

## Matching flags

Recomputed the match geometry for every focused probe: matched kind, atlas/world/screen/SH, distance, normal dot, tile footprint, incompatibility threshold, `same_atlas_coord`, `physical_sampling_position_identical`, `geometric_incompatible`, and both SH deltas.

- screen-sh: 50 matches; 18 atlas changes, 22 same-atlas-but-moved, 10 identical, 24 incompatible; 0 flag failures.
- motion-screen-sh: 30 matches; 20 atlas changes, 0 same-atlas-but-moved, 10 identical, 14 incompatible; 0 flag failures.
- No atlas change is labelled physically identical; every distance above the recorded threshold or normal dot below 0.90 is flagged incompatible. The reference atlas, world position and SH all resolve to the published active record.

## Figures

Inspected `lobes_baseline.png`, `lobes_lightstep-001.png`, `lobes_camera-064.png`, both `lobes_rosettes.png` and `active_baseline.png`. The lobe images are anchored on the floor and side arcades of the real Sponza screenshot, remain readable across states, and use magenta x-ray outlines for depth-occluded parts. The lobe images contain only the ten focus lobes and green/magenta focus markers; the all-active dot grid appears only in the separate `active_*` image. Rosettes use one fixed ring radius and the declared per-experiment scale.

## Hashes and provenance

- screen-sh: 21 publish/report files plus the renamed `decoder.py` entry; all 22 SHA-256 values and byte counts match. `decoder.py` is the renamed `decode_temporal_sh.py` (87,099 bytes, `cb76422d...`).
- motion-screen-sh: all 15 listed publish/report files match SHA-256 and byte counts. This manifest has no decoder hash entry.
- 8/8 published screenshot paths exist and match the SHA-256 recorded in `summary.json`.
- No published metadata was mutated. Absolute `D:\...` paths in the decode manifests are historical capture provenance, not browser URLs.

## Reuse

- `--state baseline=<light baseline> --stage all` and `--state camera-064=<motion camera-064> --stage all` both exit PASS. Recomputing one state returns the same active keys, counts, view exposure/frame fields, screen positions and all 27 coefficients as the published records: maximum difference 0.0 for 3,706 light and 3,682 motion active probes.
- Caveat: `--stage data` writes byte-identical state JSON but exits 2 because `finite_nonempty_outputs` requires `publish/figures/lobes_<state>.png`, which that stage does not render. A collector should use `--stage all` or not treat that exit status as a data failure.
- The motion report's reuse example still names the light `lightstep-...` states; the explicit `--state NAME=PATH` form works for motion.

## Root UI manifest

`Docs/Unreal/assets/lumen-sponza/temporal/manifest.json` has 61 local image/record references across `renderer-light-step`, `light-buffers` and `camera-buffers`; all exist, none are URLs. No `fps`, frame-rate, 60 Hz or frame-time claims were found in the manifest or `build_temporal_manifest.py`; the light/camera facts explicitly say updates are discrete and playback spacing does not represent elapsed rendering time. The screen-sh captions use the matching per-experiment scale and 60 cm radius.

## Acceptance checks

- active placements and nine RGB SH coefficients decoded with source identities: **yes** — 11,074 active placements, 298,998 coefficients, max diff 5.0e-7, 0 failures; named per-state operands.
- projected real-scene lobes use fixed declared units and scales over time: **yes** — one 60 cm radius and one 1.4454/1.5239 scale per experiment, negatives retained and display clamp declared, images anchored and readable.
- output and decoder reusable by the main temporal collector: **yes** — single-state `--stage all` PASS and exact coefficient reproduction for light and motion; caveat: `--stage data` exits 2 after writing byte-identical data due the figure-presence self-check.

Overall: **pass**. The non-blocking findings are the data-only exit status, the motion report's copied light-state reuse example, and the motion manifest's missing decoder hash entry.
