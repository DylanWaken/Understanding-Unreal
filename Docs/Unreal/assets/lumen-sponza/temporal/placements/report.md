# Scene-anchored Lumen cards and main world-cache probes

This publication is the focused placement decode for the two accepted temporal sequences. It does not decode screen-probe SH, radiosity, or world-cache content; the existing `output/temporal/screen-sh` and `motion-screen-sh` publications remain the screen/SH source.

## Datasets

| sequence | state | run | actual update | frame_index | engine frame | graph prefix | camera world cm |
|---|---|---|---|---|---|---|---|
| light | baseline | 20261115-101057 | baseline | 370 | 127 | 0004 | [-850.0, -0.0, 90.0] |
| light | lightstep-001 | 20261115-101057 | 1 | 459 | 169 | 0003 | [-850.0, -0.0, 90.0] |
| light | lightstep-008 | 20261115-101057 | 7 | 475 | 178 | 0003 | [-850.0, -0.0, 90.0] |
| light | lightstep-032 | 20261115-101057 | 31 | 545 | 205 | 0003 | [-850.0, -0.0, 90.0] |
| light | lightstep-064 | 20261115-101057 | 63 | 638 | 240 | 0003 | [-850.0, -0.0, 90.0] |
| motion | baseline | 20261115-104035 | baseline | 370 | 127 | 0004 | [-850.0, -0.0, 90.0] |
| motion | camera-001 | 20261115-104035 | 1 | 378 | 133 | 0004 | [-850.0, -150.0, 90.0] |
| motion | camera-064 | 20261115-104035 | 63 | 563 | 198 | 0004 | [-850.0, -150.0, 90.0] |

The rejected narrow retry `20261115-101923` and the black `+Y150` states are not read. The light sequence actual updates are baseline / 1 / 7 / 31 / 63 (labels baseline, lightstep-001, lightstep-008, lightstep-032, lightstep-064). The motion sequence uses baseline / 1 / 63 from the same 20261115-104035 process.

`view.json` keeps the frame identity (`frame_index`, engine frame scope, primary view id) separate from the View-state (matrices, PreViewTranslation high/low/sum, camera, projection checks), so the two concepts are never conflated in the numeric data.

## Invocation fix (why this differs from the old decoder)

`scripts/decode_temporal.py` chose each cache buffer by the globally lowest producer pass id. For the baseline dump that picks graph `0001` while the selected camera View and `MarkRadianceProbes(ScreenProbes)` are graph `0004`. That is a mixed-invocation decode. This decoder selects the camera View first and then requires the mark pass, allocator pass, indirection, world-offset buffer, allocator/free-list buffers and timestamp buffers to belong to that same graph prefix.

The main screen-probe cache is not selected by name, size guess, or lowest id. It is identified by:

1. captured `FRadianceCacheMarkParameters` on `MarkRadianceProbes(ScreenProbes)` → resolution 48, 4 clipmaps, exact corners;
2. an `AllocateUsedProbes` pass in the same invocation whose `FRadianceCacheParameters` has the same resolution, clipmap count and corners;
3. the indirection and companion buffers taken from that allocate pass's `OutputResources`;
4. read lineage: the 192×48×48 indirection is consumed by `FilterProbeRadiance Res=32x32`, `ComputeLightingPDF` and `TraceGlobalSDFAndApplyRadianceCache` with screen-probe-gather inputs.

The 72×24×24 group is rejected as the translucency-volume cache: its `MarkRadianceProbesUsedByTranslucencyVolume` parameters are 24/3, its indirection is 72×24×24, its only filter is `FilterProbeRadiance Res=8x8`, and it has no ScreenProbeGather lineage.

| state | view prefix | mark res/num | alloc res/num | indirection extent | probe res | old global-lowest prefix | used |
|---|---|---|---|---|---|---|---|
| baseline | 0004 | 48/4 | 48/4 | 192×48×48 | 32 | 0001 | selected-live only |
| lightstep-001 | 0003 | 48/4 | 48/4 | 192×48×48 | 32 | 0002 | selected-live only |
| lightstep-008 | 0003 | 48/4 | 48/4 | 192×48×48 | 32 | 0002 | selected-live only |
| lightstep-032 | 0003 | 48/4 | 48/4 | 192×48×48 | 32 | 0002 | selected-live only |
| lightstep-064 | 0003 | 48/4 | 48/4 | 192×48×48 | 32 | 0002 | selected-live only |
| baseline | 0004 | 48/4 | 48/4 | 192×48×48 | 32 | 0001 | selected-live only |
| camera-001 | 0004 | 48/4 | 48/4 | 192×48×48 | 32 | 0001 | selected-live only |
| camera-064 | 0004 | 48/4 | 48/4 | 192×48×48 | 32 | 0001 | selected-live only |

`ParameterBindings.json` is also read for the mark/allocate passes; the selected allocate pass binds `RWRadianceProbeIndirectionTexture`, `RWProbeWorldOffset`, `RWProbeAllocator`, `RWProbeFreeListAllocator`, `RWProbeLastUsedFrame` and `RWProbeLastTracedFrame` under the captured parameter block. Imported v0 card/page-table payloads are used only with the identity evidence in `cards.json`/`pages.json`.

## Cards

`FLumenCardGPUData` is 7 float4 per card (112 bytes): position high/low, world OBB axes, packed flags, low/high page spans, mesh-card transform and average texel size. The flags are exactly `bVisible` bit 24, `bHeightfield` bit 25, `bFarField` bit 26, `AxisAlignedDirection` bits 16–19, `LightingChannelMask` bits 20–23, and the two res-level biases in bits 0–7 / 8–15. `bVisible` is `bCardVisible && Card.IsAllocated()`, not a proof of on-screen visibility.

| state | GPU capacity cards | valid cards | included visible | page-resident | low-res pages | static geometry sha256 |
|---|---|---|---|---|---|---|
| baseline | 585 | 522 | 371 | 371 | 385 | `05a046be0619bdcf…` |
| lightstep-001 | 585 | 522 | 371 | 371 | 385 | `05a046be0619bdcf…` |
| lightstep-008 | 585 | 522 | 522 | 522 | 626 | `05a046be0619bdcf…` |
| lightstep-032 | 585 | 522 | 522 | 522 | 626 | `05a046be0619bdcf…` |
| lightstep-064 | 585 | 522 | 522 | 522 | 626 | `05a046be0619bdcf…` |
| baseline | 585 | 522 | 386 | 386 | 400 | `f0423fcf93bd0cb9…` |
| camera-001 | 585 | 522 | 395 | 395 | 408 | `f0423fcf93bd0cb9…` |
| camera-064 | 585 | 522 | 395 | 395 | 408 | `f0423fcf93bd0cb9…` |

The 4096-float4 GPU allocation is 585 full 7-float4 cards plus one trailing float4 that is not a card. The valid range is inferred as the contiguous nonzero uploaded payload 0..521; the tail 63 slots are zero in origin/axes/mesh-extent/flags. The `FLumenCardScene.NumCards` uniform is not exported, so this is structural identity evidence, not a guessed capacity. Capacity is therefore not the card count.

Fixed-camera audit: baseline and lightstep-001 have 371 included cards / 385 low-res pages; lightstep-008/032/064 have 522 included cards / 626 pages. The exact static geometry hash is identical across all five light states, so this is an inclusion/visibility flag plus matching page residency change, not geometry motion. `sequence.json` lists every changed card index.

The two runs contain the same static card geometry set but in a different index order: the exact index-order hash differs between light and motion, while the canonical sorted geometry hash is identical (see `checks.json`). Card indices are never matched across sequences.

## Pages

| state | page-buffer capacity | page-table capacity | mapped payload | referenced by current cards | unreferenced payload | page-table nonzero |
|---|---|---|---|---|---|---|
| baseline | 1024 | 1024 | 626 | 385 | 241 | 626 |
| lightstep-001 | 1024 | 1024 | 626 | 385 | 241 | 626 |
| lightstep-008 | 1024 | 1024 | 626 | 626 | 0 | 626 |
| lightstep-032 | 1024 | 1024 | 626 | 626 | 0 | 626 |
| lightstep-064 | 1024 | 1024 | 626 | 626 | 0 | 626 |
| baseline | 1024 | 1024 | 626 | 400 | 226 | 626 |
| camera-001 | 1024 | 1024 | 626 | 408 | 218 | 626 |
| camera-064 | 1024 | 1024 | 626 | 408 | 218 | 626 |

PageBuffer rows are 5 float4; PageTable entries are 2 uint. The PageTable word0 packs SampleAtlasBiasX/Y and SampleCardResLevelX/Y; word1 is SamplePageIndex. In every current reference, SamplePageIndex equals the page's own index and the PageBuffer CardIndex equals the owner card. Rows outside the current card spans are retained payload, not current page residency. The per-page update fields in float4[4] are decoded as raw uint frame indices.

## World cache

World position is `corner + (logical_coord + 0.5)*cell_size + ProbeWorldOffset - (PreViewTranslationHigh + PreViewTranslationLow)`. `ProbeWorldOffset` is a translated-world offset (PF_FloatRGBA/float16x4 in this capture); in all eight selected states it is exactly zero, so positions are the captured clipmap grid centres. No cache SH or cache content is synthesized.

| state | active by indirection | allocator | free | alloc−free | last-used window | last-traced window | never traced | position sha256 |
|---|---|---|---|---|---|---|---|---|
| baseline | 878 | 925 | 47 | 878 | 878 | 835 | 0 | `99b36a6b4cd3c12f…` |
| lightstep-001 | 878 | 925 | 47 | 878 | 878 | 839 | 0 | `e8259795f57690e0…` |
| lightstep-008 | 878 | 925 | 47 | 878 | 878 | 834 | 0 | `e8259795f57690e0…` |
| lightstep-032 | 878 | 925 | 47 | 878 | 878 | 836 | 0 | `e8259795f57690e0…` |
| lightstep-064 | 878 | 925 | 47 | 878 | 878 | 828 | 0 | `e8259795f57690e0…` |
| baseline | 878 | 925 | 47 | 878 | 878 | 870 | 0 | `a4bc4584a52b5ff6…` |
| camera-001 | 968 | 972 | 4 | 968 | 968 | 918 | 0 | `601430308d24c535…` |
| camera-064 | 847 | 972 | 125 | 847 | 847 | 839 | 0 | `38e5721cb705098e…` |

Physical slots are tracked by `(clipmap, world_cell)` as well as by physical id. A physical id that maps to a different world cell at the same clipmap level is counted as actual slot reassignment; a logical-coordinate change that preserves the world cell is clipmap-origin motion and is not counted as recycle. The motion capture moves the clipmap origin by whole world cells (clipmap 0: −2 cells; clipmaps 1–3: −1 cell), so the raw logical coordinate changes for many unchanged cells. This is separated in `sequence.json`.

| pair | shared pids | same cell same pid | same cell new pid | physical id new cell | coord changed / world cell same | cells only prev | cells only curr |
|---|---|---|---|---|---|---|---|
| light: baseline→lightstep-001 | 878 | 870 | 8 | 8 | 0 | 0 | 0 |
| light: lightstep-001→lightstep-008 | 878 | 878 | 0 | 0 | 0 | 0 | 0 |
| light: lightstep-008→lightstep-032 | 878 | 878 | 0 | 0 | 0 | 0 | 0 |
| light: lightstep-032→lightstep-064 | 878 | 878 | 0 | 0 | 0 | 0 | 0 |
| motion: baseline→camera-001 | 874 | 870 | 0 | 4 | 870 | 8 | 98 |
| motion: camera-001→camera-064 | 847 | 842 | 1 | 5 | 0 | 125 | 4 |

Activity is published as numeric tracks: per-probe `last_used_frame`, `last_traced_frame`, `use_age_frames`, `traced_age_frames` (0 means never traced), and per-state/per-clipmap `traced_age_histogram` in `worldcache.json` and `sequence.json`. Marker colour and radius are fixed per clipmap and are not varied by age.

## Figures

Each state has `base.png` (byte-identical copy of the raw `Base/Screenshot.png`) plus four separate composites: `cards-only.png`, `cards-xray.png`, `worldcache-only.png`, `worldcache-xray.png`. There are no mixed card+worldcache images and no burned text. `figures/legend.json` carries the native colour/radius/depth metadata.

| sequence | state | cards drawn | worldcache drawn | cards xray | worldcache xray |
|---|---|---|---|---|---|
| light | baseline | 24 | 361 | 128 | 408 |
| light | lightstep-001 | 24 | 361 | 128 | 408 |
| light | lightstep-008 | 24 | 361 | 169 | 408 |
| light | lightstep-032 | 24 | 361 | 169 | 408 |
| light | lightstep-064 | 24 | 361 | 169 | 408 |
| motion | baseline | 24 | 361 | 131 | 408 |
| motion | camera-001 | 24 | 316 | 135 | 511 |
| motion | camera-064 | 24 | 315 | 135 | 418 |

The card overlay is the projected convex hull of the 8 OBB corners. The primary image draws a deterministic representative subset (up to 24 typical-size cards with 8×6 screen-cell diversity) thick; all numeric cards are retained in `cards.json`/`cards.npz`. The xray image is a separate low-alpha image of centre-depth-occluded cards. Worldcache markers use fixed per-clipmap colours and radii; xray is separate.

## Projection / depth checks

| state | tw_to_clip product max | pre high+low max | forward delta | camera vs manifest cm | cards on screen | centre-depth visible | probes on screen | probes visible |
|---|---|---|---|---|---|---|---|---|
| baseline | 6.73e-08 | 0.00e+00 | 0.00e+00 | 0.0 | 407 | 175 | 769 | 361 |
| lightstep-001 | 6.73e-08 | 0.00e+00 | 0.00e+00 | 0.0 | 407 | 175 | 769 | 361 |
| lightstep-008 | 6.73e-08 | 0.00e+00 | 0.00e+00 | 0.0 | 407 | 175 | 769 | 361 |
| lightstep-032 | 6.73e-08 | 0.00e+00 | 0.00e+00 | 0.0 | 407 | 175 | 769 | 361 |
| lightstep-064 | 6.73e-08 | 0.00e+00 | 0.00e+00 | 0.0 | 407 | 175 | 769 | 361 |
| baseline | 6.73e-08 | 0.00e+00 | 0.00e+00 | 0.0 | 407 | 175 | 769 | 361 |
| camera-001 | 6.73e-08 | 0.00e+00 | 0.00e+00 | 0.0 | 396 | 162 | 827 | 316 |
| camera-064 | 6.73e-08 | 0.00e+00 | 0.00e+00 | 0.0 | 396 | 162 | 733 | 315 |

Depth uses `view_cm = NearPlane / SceneDepthZ` at the projected pixel with tolerance `max(10 cm, 0.02*scene_depth_cm)`. The card centre is the overlay classifier because OBB corners can overshoot the represented surface; corner counts are still stored per card. The SceneDepthZ allocation is 1280×888 while the FPackedView view rect is 1280×720; the top-left 1280×720 region is sampled and was validated against card-centre depth.

## Integrity and checks

- Raw inputs hashed before/after: **2604** files; changed: **0**.
- NativeBuffer files read: **0**; raw files are never opened for write.
- Overall checks status: **pass** (0 failures of 12).

| check | status |
|---|---|
| cache_resources_and_transforms_same_invocation | pass |
| card_bounds_and_worldcache_positions_both_sequences | pass |
| individual_scene_overlays_and_temporal_provenance | pass |
| projection_and_camera_verified | pass |
| allocator_free_reference_counts | pass |
| static_card_geometry_hash | pass |
| page_index_bounds_and_mapping | pass |
| imported_alias_identity_evidence | pass |
| raw_inputs_hashed_before_and_after_zero_changed | pass |
| no_radiosity_or_worldcache_sh | pass |
| accepted_datasets_only | pass |
| no_burned_text_in_figures | pass |

## CLI

```powershell
python scripts/decode_placements.py
python scripts/decode_placements.py --light-root <raw-run-dir> --motion-root <raw-run-dir> --out <dir>
python scripts/decode_placements.py --sequences light --light-states baseline,lightstep-008
```

Data layout: `light/data/<state>/{view,cards,pages,worldcache,sources}.json` plus `.npz`; `light/data/sequence.json`; `light/figures/<state>/*.png` and `light/figures/legend.json`. The same layout is used for `motion`.

## Limits

- `Lumen.Cards` and `Lumen.PageTable` are exported only as v0 imported pooled payloads; the structural and temporal identity evidence is recorded in `cards.json`, `pages.json` and `checks.json`.
- `ProbeWorldOffset` is zero in all selected states, so no sub-cell probe displacement is visible in this capture. The decoder applies the captured offset generically.
- PageBuffer/PageTable contain retained payload rows outside the current card spans; those are reported as unreferenced payload, not as current page residency.
- No radiosity and no world-cache SH are decoded or drawn. A separate surface-probe capture is required for real SceneDepth/Normal-based probes.
