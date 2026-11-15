# Final validation — integrated Lumen revision

**Status: PASS** (one genuine concurrent root addition is disclosed, not attributed to
validation).

Scope: read-only validation of `D:\UnrealUnderstanding` and the two accepted native
capture trees named by the step. No GPU work, no commits, no source/prose rewrites. The
only write scope used was `results/` in this workspace. Python 3.14.6, Node v24.22.1,
`PYTHONDONTWRITEBYTECODE=1` for every command and check.

The final tight pass ran `2026-11-15T19:55:40+08:00` to `19:57:19+08:00`. Root was still
integrating during earlier passes; those passes caught root rebuilds and were re-run. The
final pass was performed on the settled revision: 11/11 commands, 507 temporal checks,
158 SVG checks and 69 independent numerical checks, all with 0 failures.

## Commands (exact commands and outcomes)

All commands ran from the stated working directory with `PYTHONDONTWRITEBYTECODE=1`.
Machine-readable copy: `results/commands.json`. Full stdout/stderr: `results/logs/`.

| # | Command | cwd | Exit | Outcome |
|---|---|---|---:|---|
| 01 | `python build.py --check` | `Scripts/Docs/unreal_articles` | 0 | 5 articles verified: scene-representation 29,463 words/18 chapters; nanite 15,369/20; lumen 29,302/16; material-compiler 12,214/16; lumen-reference 65,579/44 |
| 02 | `python math_typesetting.py --check --node node` | `Scripts/Docs/unreal_articles` | 0 | `Verified 1023 LaTeX expressions` |
| 03 | `python validate.py` | `Scripts/Docs/unreal_articles` | 0 | `PASS: article structure, links, figures`; 155 accessible SVGs; 289 recorded source ranges; lumen 29,030 words/16 chapters/71 figures/280 links |
| 04 | `python validate_labs.py` | `Scripts/Docs/unreal_articles` | 0 | `PASS: 129 laboratory sources/outputs, 84 PNGs, frozen hashes and numerical evidence` |
| 05 | `python validate_lumen_v2.py` | `Scripts/Docs/unreal_articles` | 0 | `PASS all requested evidence checks`; 245 resource / 15 parameter layouts; 7,024 payloads, 888 native files, 3,136 chunks, 18,502,060,278 payload bytes; 236 artifact files / 47,440,343 bytes; 6,001 portable capture checks; 8 diagrams |
| 06 | `python Scripts/Docs/check_site_independence.py` | repo root | 0 | `Site independence check passed: 11 HTML file(s)` |
| 07 | `python labs/lumen-reading/generate.py --check` | `Scripts/Docs/unreal_articles` | 0 | `Verified 7 diagrams, CPU reference, clean PNG, and manifest` |
| 08 | `python labs/lumen-course/generate.py --check` | `Scripts/Docs/unreal_articles` | 0 | `Verified 29 original SVG diagrams and 15 numerical checks` |
| 09 | `node --check Docs/Unreal/assets/lumen-course/course.js` | repo root | 0 | syntax accepted |
| 10 | `node --check Docs/Unreal/assets/lumen-course/temporal.js` | repo root | 0 | syntax accepted |
| 11 | `python Scripts/Docs/unreal_articles/labs/lumen-sponza/build_temporal_manifest.py --check` | repo root | 0 | `Verified Docs\Unreal\assets\lumen-sponza\temporal\manifest.json` |

No GPU workload, package re-verification or commit was run. The prior static
context/receiver/cache-replay evidence was not re-run; the frame Python/Node test code was
not changed (below).

### Frame / receiver / context / cache-replay prior evidence

The step says to rerun the frame Python/Node tests only if the code changed since the
saved static checks. It did not change.

- Code set: all 119 files under `labs/lumen-frame`, plus `decode_receivers.py`,
  `decode_context.py` and `cache-replay/`.
- Combined code-set SHA-256:
  `7e82f43bdfaf0962a51c600f86fd34982b82f9ca2548b18ef0796e05da8bf5e2`.
- Compared against `D:\UnrealUnderstanding-Review\static-final-checks\tree-final.txt`:
  0 size differences and 0 mtime differences beyond 1 ms timestamp-conversion noise.
- Prior results therefore remain the evidence: frame Python 39 tests OK; verifier 15 tests
  OK; Node metadata browser 6 checks, read path 15 checks, codec 19 tests; receivers
  27/27; context 252/252; cache replay surface pass=694 / world pass=747.

## Temporal manifest and assets

`Docs/Unreal/assets/lumen-sponza/temporal/manifest.json` was checked against its numeric
sources (`results/temporal_manifest_check.py` → `temporal_manifest_checks.json`):
**507 checks, 0 failures**.

- Layer families: exactly the six controls `renderer`, `screen-probes`, `screen-sh`,
  `cards`, `world-cache`, `radiosity`; each has a legend, at least one experiment state,
  and an article `<div class="scene-observations" data-layer=...>` host. All six hosts
  resolve `assets/lumen-sponza/temporal/manifest.json` to the checked manifest.
- Coverage: 4 experiments, 20 states, 153 declared image/record/download paths. Every path
  exists. Every one of the 44 referenced images decodes as PNG and image dimensions are
  stable within each experiment/layer (renderer 1280×720; placement and radiosity figures
  at their declared native sizes).
- Per-state display agreement: for the 7 renderer states, 8 screen-probe states and 5
  radiosity states, every displayed engine frame, view/state index, exposure, camera
  position, active/uniform/adaptive count, stale count, jitter, retained sample count,
  nonzero-SH count, visible/occluded split and pre-exposure equals the corresponding
  numeric JSON (and, for radiosity, the NPZ cardinalities).
- World-cache charts: all 8 charts use source histogram
  `activity["0"].traced_age_histogram`; bins sum exactly to 878/878/878/878/878/878/968/847
  active probes and the fixed scale is 1000. The age title (“captured view-state updates”)
  matches the source unit: `traced_age_frames = FPackedView.FrameIndex − ProbeLastTracedFrame`,
  where `FrameNumber = View.ViewState->GetFrameIndex()`
  (`LumenRadianceCache.cpp:1904`, `SceneRendering.cpp:1926-1936`); it is not an engine-frame
  or captured-state count.
- Placeholder scan: no `TEMPORAL_EVIDENCE` or `*_TEMPORAL_EVIDENCE` text remains in
  `Docs/Unreal` or the chapter sources.

## Independent numerical spot checks

`results/independent_spotchecks.py` (no decoder module imported) → 
`independent_spotchecks.json`: **69 checks, 0 failures**. Raw operands were checked first
against their published SHA-256.

### Radiosity — `D:/UnrealUnderstanding-Radiosity/output/gpu-dump/raw/20261115-105951/baseline`

Three valid probes selected from the published `probes-baseline.npz` (indices 8815, 46721,
9750). For each, the page/card records, temporal jitter, card-page coordinate, atlas UV,
depth texel and byte offset, card UV, normal address and SH address were re-derived from
native bytes using the engine layout (`MonteCarlo.ush` Hammersley16,
`LumenRadiosity.ush` GetRadiosityTexel/GetProbeJitter, `LumenCardCommon.ush`
GetCardLocalPosition).

| Probe | World error (cm) | 12 signed SH coefficients | Depth u16 | E(normal) error |
|---:|---:|---|---:|---:|
| 8815 | 8.975e-06 | exact (half values) | 27613 | 5.615e-08 |
| 46721 | 6.104e-06 | exact | 11239 | 1.173e-08 |
| 9750 | 6.127e-06 | exact | 35711 | 8.760e-09 |

All page→card ownership, jitter, atlas coordinate, depth byte offset, SH byte offset and
normal checks pass. Declared publication is float32; the 1e-3 cm comparison threshold is
far above the observed ≤9e-6 cm.

### Main world cache — `D:/UnrealUnderstanding-Temporal/output/gpu-dump/raw/20261115-101057/baseline`

- Selected invocation is `0004`; mark pass is `MarkRadianceProbes(ScreenProbes)` res 48,
  4 clipmaps. The main indirection is 192×48×48 = 48³×4 = 442,368 words / 1,769,472 bytes.
  The rejected translucency group is res 24, 3 clipmaps, extent 72×24×24 = 24³×3.
- Raw allocator = 925, raw free-list allocator = 47, 925 − 47 = 878 active entries; all 47
  free ids are unique and disjoint from the 878 active ids.
- Corners were read from the native `RadianceCacheMarkParameters` block (offset 352), and
  `PreViewTranslation` from the native FPackedView as high(720)+low(832) = (850, 0, −90).
- All 878 positions were independently recomputed as
  `corner + (logical+0.5)·cell + ProbeWorldOffset − PreViewTranslation`; the resulting
  position hash is exactly the published
  `99b36a6b4cd3c12ff373ecdcf1f8dad2f77f29a2995f03add1221dbb65ff6483`.
  Five published probes (pids 0, 310, 608, 460, 924) differ by 0.0 cm.
  `ProbeWorldOffset` is exactly zero for all active probes.

## SVG XML and frame-flow geometry

`results/svg_check.py` → `svg_checks.json`: **157 SVG files parsed, 158 checks, 0
failures**. Every published SVG under `Docs/Unreal/assets` and the lab SVG sources is
well-formed XML with a positive viewBox/width-height.

`frame-flow.svg` was additionally bounds-checked (149 text/shape elements against its
1200×2500 viewBox, stroke widths and Arial text metrics included): 0 elements outside the
viewBox. The same diagram is regenerated by `labs/lumen-course/generate.py --check`, which
passed.

## Integrity and concurrent root activity

Two full SHA-256/size snapshots covered `Docs/Unreal`,
`Scripts/Docs/unreal_articles`, `Data/Sponza/evidence`,
`Data/Sponza/temporal-native` and `Data/Sponza/sponza-20261115-083654`
(`results/hashes-before.json`, `results/hashes-after.json`).

- Before: 24,841 files / 3,577,116,934 bytes.
- After: 24,842 files / 3,577,118,085 bytes.
- Pre-existing files changed: **0**. Pre-existing files removed: **0**.
- One file was added concurrently by root during the run:
  `Data/Sponza/evidence/temporal-captures/flagged-raw-identity.json` (1,151 bytes,
  SHA-256 `8a5e099855b7382a…`). It is a capture-identity report for two flagged raw
  operands (`ok: true`), outside the article, temporal manifest and figure references.
  This is the genuine producer-finished update anticipated by the step. No validation
  process writes repository paths.
- Earlier in the session root was rebuilding the integrated site. That produced one
  transient `build.py --check` failure: `Stale article: Docs/Unreal/lumen.html` at 19:49,
  because two chapters were written before the Docs pages were regenerated. After root's
  rebuild the command passes; the failure is not reproducible on the validated snapshot.
  It is recorded in `checks.json` under `transient_observations`.

## Result

- Article math, diagrams, generated pages and local references: **pass**.
- Temporal manifest/assets and representative native numerical records: **pass**.
- Pre-existing published files: **unchanged** by this validation; one concurrent root
  addition disclosed.
- Unresolved course/test/arithmetic failure: **none**.

Evidence: `checks.json`, `commands.json`, `logs/`, `temporal_manifest_checks.json`,
`svg_checks.json`, `independent_spotchecks.json`, `independent_spotchecks.py`,
`temporal_manifest_check.py`, `svg_check.py`, `hashes-before.json`, `hashes-after.json`,
`concurrent-root-update.json` (earlier 15-file root update), `snapshot_hashes.py`.
