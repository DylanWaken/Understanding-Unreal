# Sponza cache queries — surface cache and world radiance cache

Two CPU replays over the raw hero capture
`D:\UnrealUnderstanding-Sponza\output\gpu-dump\raw\sponza-20261115-083654`.
Both use the **presented scene graph**: 3 scene graphs + 1 Slate graph, presented
graph index 3, pass pointers prefixed `0004` (passes 593–885). Camera
`(-850, 0, 90) cm`, pitch 13°, HFOV 70°, ViewRect 1280×720. No GPU work, no
engine changes, no repository writes outside this directory.

Both reports separate **captured state** from the **declared query replay**:
DumpGPU does not record a shader-local per-thread query, so each report names the
selection rule it used and never claims that a dispatched thread chose that query.

## Files

| File | Contents |
| --- | --- |
| `surface_cache_query.py` | Surface Cache replay (presented pass 694) |
| `world_cache_query.py` | World Radiance Cache replay (presented pass 747) |
| `dump_reader.py` | Version-aware reader adapted for this capture |
| `world_cache_math.py` | Reused read-only world-cache interpolation math |
| `surface-cache-query.json` | Full surface operand/tap/validation JSON |
| `world-cache-query.json` | Full world operand/tap/validation JSON |
| `verify_queries.py` | Re-checks the saved JSON invariants |
| `figures/surface-cache-atlas-crop.png` | Encoded albedo / encoded normal / final crop at stored×View.PreExposure gain (no text) |
| `figures/surface-cache-hero-projection.png` | Captured hero frame with projected page and 8×8 tile |
| `figures/world-cache-neighborhood.png` | Eight 34×34 stored-atlas probe tiles, 2×2 per z-plane |
| `figures/world-cache-taps.png` | 8 neighbors × 4 bilinear taps, stored-atlas preview |
| `figures/world-cache-probe-grid.png` | Two z-planes of the selected 2×2×2 cell neighborhood |
| `figures/*-preview-metrics.json` | Named preview transforms, gains, scales, finite/non-empty stats |

## Surface Cache query

Pass **694** `CombineLighting CS` (`Radiosity / LumenSceneLighting / Scene /
Frame 302`), pointer `00040000020bef0630a0`, parameters
`00040000020bef062f70` / metadata `000400007ffc1e2e7fb0`. This is the presented
Radiosity combine that reads both lighting atlases and writes
`Lumen.SceneFinalLighting`.

Selected declared query (rule: mapped page, tile inside page, atlas inside
4096², non-zero encoded albedo, non-emissive-dominated cached-pre-exposed
final; prefer a floor-like card visible in the hero view, then a visible valid
card with non-zero captured direct lighting, then lower-half visible cards, then
luminance):

| Operand | Value |
| --- | --- |
| Dispatch | `args=(62,1,1,3953,1,1)` (two captured triples); replay uses the second = 3953 card-tile groups, 8×8 threads; which triple the dispatch consumed is not recorded |
| Card / page | card **107**, page **323**, mesh-cards **15**, scene instance **76** |
| Card OBB | origin `(-52.2497, -189.759, 400.317) cm`, extent `(738.184, 6.926, 24.606) cm`, world normal `(0,1,0)` |
| Page | 128×128 texels, `PhysicalAtlasUVRect=(0.53125,0.03125,0.5625,0.0625)`, physical coord `(2176,128)` |
| Tile / texel | tile `(13,1)`, local texel `(108,12)`, atlas coord `(2284,140)`, atlas UV `(0.557739,0.034302)` |
| Page UV / card UV | `(0.84375,0.09375)` / `(0.921570,0.09375)` |
| World point | `(570.143, -191.739, 405.944) cm`, screen `(519.54, 367.30)` px |
| Depth | raw `35404` of `PF_G16`, normalized `0.54023041` |
| Stored formats | albedo `PF_BC7→PF_FloatRGBA`, normal `PF_BC5→PF_FloatRGBA`, emissive `PF_BC6H→PF_FloatRGBA`, depth `PF_G16`, direct/indirect/output `PF_FloatR11G11B10`, all 4096×4096 |
| Albedo (BC7 encoded) | `(0.400000, 0.376471, 0.337255)` = sqrt(linear ρ); raw expanded float16 `(0.3999023, 0.3764648, 0.3371582)` snaps to `(102,96,86)/255` |
| Emissive | `(0, 0, 0)` cached-lighting pre-exposed |
| Direct | `(4288.0, 3296.0, 2496.0)` cached-lighting pre-exposed (×`CachedLightingPreExposure` = 1/16) |
| Indirect | `(412.0, 568.0, 880.0)` cached-lighting pre-exposed |
| ρ = encodedAlbedo² | `(0.160000, 0.141730, 0.113740)` linear |
| Final before store | `(239.369, 174.321, 122.228)` cached-lighting pre-exposed |
| Captured output | packed `2943139256`, `(240, 174, 122)` |
| Final before quantized store, un-pre-exposed | `(3829.905, 2789.134, 1955.641)` = predicted pre-store value ×16 |
| Final before quantized store, view-pre-exposed | `(1.870071, 1.361882, 0.954903)` = predicted pre-store value ×16×`View.PreExposure` |
| `View.PreExposure` | `0.00048828125` (1/2048; separate from the cached-lighting exposure) |

The store is **not** view-pre-exposed. `CombineLumenSceneLightingCS` reads
direct/indirect/emissive atlases already multiplied by
`CachedLightingPreExposure` (captured pass 684 `Batched lights`: `0.0625`), so
the stored final is linear radiance / 16.
`LumenSurfaceCacheSampling.ush:312-314` multiplies by 16 on read;
`View.PreExposure` (1/2048) is a separate camera pre-exposure. The old
`stored × View.PreExposure` = `(0.116879, 0.085118, 0.059681)` is a partial
transform that omits the cached-exposure inverse and is not display-linear.

The selected card is a thin stone ledge/cornice at z ≈ 400 cm; its geometric
normal is `(0,1,0)` and the captured BC5 normal at the texel decodes to
`(0.0118, 0.7412, 0.6712)`. The point projects to the middle of the hero frame.

All four bilinear taps are retained for albedo, emissive, direct and indirect in
`surface-cache-query.json → taps`; at this exact texel-centre UV the weights are
`(1,0,0,0)` and the other three taps are the neighbouring captured texels.

Validation on the whole 8×8 selected tile: **64/64 texels** match the shader's
float-store truncation model; 16/64 also match the nearest-rounding variant.
The tile check uses the captured `TargetFormatQuantizationError`
`(0.015625, 0.015625, 0.03125)`, PCG16 seed `(atlasX, atlasY, StateFrameIndexMod8+2)`,
and the captured output words.

Independence from the primary-floor receiver decode in
`D:\UnrealUnderstanding-Review\sponza-receivers`: receiver A at `(500,680)`
decodes to world `(-19.718, -120.314, -2.005)`, while this card texel is
`(570.143, -191.739, 405.944)` — a different surface, 590 cm away in X and 408 cm
higher. The surface operands are Lumen card/page/atlas records, not the GBuffer
or SceneDepth at `(500,680)`/`(720,680)`.

## World Radiance Cache query

Pass **747** `TraceGlobalSDFAndApplyRadianceCache` (`LumenScreenProbeGather /
DiffuseIndirectAndAO / Scene / Frame 302`), pointer `00040000020c1328ca80`.
Selected lane **11** of 104,483 compacted traces (ascending-lane declared rule;
uniform probe, valid depth, valid clipmap, eight allocated neighbors, weights in
`[0.05,0.95]`, captured `TraceHit.bHit=false`, non-zero cache radiance, depth
match within 0.001 cm).

| Operand | Value |
| --- | --- |
| Compacted word | `135266999`; probe **695**, trace texel `(1,4)`, trace buffer `(441,68)` |
| Screen probe | atlas `(55,8)`, screen `(882,136)`, scene depth `543.1459 cm` |
| Query position | translated world `(499.349, 144.104, 251.590) cm` |
| Ray direction | `(0.916132, -0.394151, 0.073122)` from equal-area UV `(0.856863,0.375490)` |
| Cone half-angle | `0.1770825` rad (cell 6,3; level 1; 8×8) |
| Clipmap | level **0**, corner `(-2639.583, -2552.083, -2642.083)`, cell `104.1667 cm` |
| Position − clipmap corner | `(3138.932, 2696.187, 2893.674) cm` |
| Cell coordinate | `(30.133747, 25.883396, 27.779266)` cells (`CellSize` 104.1667 cm) |
| Base cell / fractions | `(29,25,27)` / `(0.633747,0.383396,0.279266)` |
| Indirection volume | 192×48×48 `PF_R32_UINT`, row pitch 768, z stride 36864 |
| Probe indices | `38, 715, 42, 719, 39, 716, 45, 720` (all allocated, all distinct) |
| Probe offsets | all 16,384 half4 offsets are zero in this captured version |
| Radiance atlas | 4352×4352 `PF_FloatR11G11B10`, final-probe resolution 34, max mip 0 |
| Depth atlas | 4096×4096 `PF_R16_UINT` |
| Neighbor weights | `0.162765, 0.0630675, 0.101205, 0.0392146, 0.281642, 0.109129, 0.175121, 0.0678551` (sum `1.0`) |
| Taps | 8 neighbors × 4 bilinear taps = **32** retained |
| Cache result | `(457.921, 406.295, 344.265)` un-pre-exposed cached-lighting radiance (stored atlas ×16) |
| Forward model (view-pre-exposed) | `cache * View.PreExposure(1/2048)` = `(0.223594, 0.198386, 0.168098)` |
| Captured `TraceRadiance` | `(0.224609, 0.199219, 0.167969)` in the same view-pre-exposed space |
| Depth | replayed `1039.793091` vs captured `1039.792969` cm, abs diff `1.221e-4` cm |

Radiance scaling: each stored tap is decoded R11G11B10, multiplied by
`RadianceCacheOneOverCachedLightingPreExposure = 16` and the sphere-parallax
correction `t²/(radius·dot(reprojectedDirection, omega))`; the eight neighbor
values are combined in the shader's nested Z/Y/X `lerp` order. The ×16 cancels
the cache atlas's `CachedLightingPreExposure` (1/16), so `cacheRadiance` is
**un-pre-exposed** cached-lighting radiance. `LumenScreenProbeTracing.usf:851`
then multiplies the trace result by `View.PreExposure` (1/2048) before the
`TraceRadiance` store, so the forward model is **view-pre-exposed scene-linear**,
not display-linear. The three spaces are kept distinct: stored cache atlas
(cache-pre-exposed) → `cacheRadiance` (un-pre-exposed) → `TraceRadiance`
(view-pre-exposed). The forward model is within the captured quantization
tolerance on all three channels (`0.00102 ≤ 0.00349`, `0.00083 ≤ 0.00310`,
`0.00013 ≤ 0.00525`). This is a forward consistency check, not an equality
claim: the preceding GDF/voxel result, skylight leaking, height fog and final
store are not reconstructed.

The transform from clipmap cell to probe position is
`center = ClipmapCornerTWS + (cell + 0.5) * CellSize + ProbeWorldOffset`, and the
atlas tile is `tile = (probeIndex & 127, probeIndex >> 7)`.

## Captured state vs declared replay

- **Captured:** pass records, parameter blocks, referenced View/CardScene
  payloads, all input resource versions and byte contents, output atlas words,
  compacted trace word/allocator, ray info, blue-noise bytes, `TraceHit` and
  `TraceRadiance`.
- **Declared:** which card tile and which compacted trace lane are evaluated.
  The selection rules are written in each JSON (`selection.rule`) and in this
  report. No shader-local query, compiled permutation bit, or per-thread dispatch
  ID is claimed.
- **Not invented:** every captured numerical operand in the JSON is read from a
  named captured file/version; derived values are recomputed from those operands
  and named formulas. Missing fields are listed under `coverageGaps`.

## Gaps

- DumpGPU has no mesh asset or material name for a Lumen card; the captured
  identity is CardIndex / MeshCardsIndex / SceneInstanceIndex plus the world
  OBB. The surface report does not invent a name.
- BC7/BC5 readback is exporter-expanded float (`NativeFormatExpanded=True`,
  `StoredBytesAreNativeStorage=False`), not native BC block bits. The replay
  snaps those channels to the 1/255 UNORM8 grid to model hardware BC7/BC5
  sampling; this is a modeling approximation, not a bit-exact block decode.
  About 11% of albedo texels are off the 1/255 grid (max 0.123/255), so the snap
  is not a lossless recovery. The selected albedo texel's raw expanded float16
  is `(0.3999023, 0.3764648, 0.3371582)` and snaps to `(102,96,86)/255`.
  BC6H remains float.
- The captured surface `DispatchCardTilesIndirectArgs` holds two triples,
  `(62,1,1)` and `(3953,1,1)`; DumpGPU does not record which one the dispatch
  consumed. The replay evaluates the second (3953 groups).
- `NumGlobalSDFClipmaps` is 0 in the captured view; the world report replays the
  cache lookup, not the preceding voxel trace.
- The world pass binds both Div64 and Div32 indirect triples; DumpGPU does not
  record which one the dispatch consumed. The selected lane is below both limits.
- The surface card-tile selection is a declared rule over the captured tile
  list; GPU scheduling order is not captured per thread.
- The selected surface is a ledge/cornice rather than the floor: the presented
  graph's Radiosity dispatch contains no floor-like card (0 of 3953 candidates
  pass the `|normal.z|>0.7, origin.z<300 cm` floor test). 49 candidates have
  non-zero captured direct lighting; the rule selects the brightest of them.

## Reproduction

The published world-neighborhood preview places the two z planes in separate panels (564×276 pixels), retaining all eight tiles. The original exploratory preview overwrote one z plane with the other. The corrected generator records all eight distinct tile rectangles in `figures/world-cache-preview-metrics.json`, and the verifier checks their uniqueness, corner coverage, and PNG dimensions. Captured operands and replay results are unchanged. The captured quantized final texel (240,174,122), unlike the pre-store estimate above, converts to view-pre-exposed (1.875,1.359375,0.953125).

```powershell
cd D:/UnrealUnderstanding-Tools/sponza-cache-queries
python surface_cache_query.py
python world_cache_query.py
python verify_queries.py
```

`verify_queries.py` re-checks the saved JSON only and prints:

```
surface: pass=694 card=107 page=323 atlas=[2284, 140] tileMatches=64/64 screen=[519.54, 367.3] OK
world: pass=747 lane=11 probe=695 clipmap=0 neighbors=8 taps=32 depth=1039.7930908203125 captured=1039.79296875 absDiff=0.0001220703125 forwardConsistent=True OK
all query invariants hold
```

Engine source is the declared pinned base
`71fe36aac5a8df5ccd66c763ffc902b29b6a9c43` under `D:/UnrealEngine`; exact source
lines and SHA-256 values are in each JSON (`sourceRefs`, `sourceEvidence`).
