# Source schema dictionary notes — UE 5.8.1 Lumen / deferred / VSM

Engine: `D:/UnrealEngine`, `Engine/Build/Build.version` = 5.8.1, `HEAD` = `71fe36aac5a8df5ccd66c763ffc902b29b6a9c43`.
All paths below are engine-relative and 1-based. Every scoped engine file (shader, shared header, and the explicit Renderer sources) is clean in `git status`; no engine file was modified.
No GPU capture was performed (no hardware lease). These notes are source-verified, not capture-verified.

Machine-readable outputs:

| file | content |
|---|---|
| `declaration-index.json` | exhaustive source-only declaration inventory (macros, parameter structs, plain shader structs, includes, shader bindings) |
| `source-schema.json` | 39 resource schemas + 15 parameter/layout schemas + 8 sampler bindings + 345 source refs |
| `validation.json` | JSON parse, path/line/symbol checks, extraction counts, determinism hash |
| `extract_declarations.py` | deterministic extractor (`python extract_declarations.py --engine-root D:/UnrealEngine`) |

---

## 1. Source layout vs runtime permutation

The schema is deliberately split into two layers:

1. **Declarations** (`declaration-index.json`) are a source-only superset. A `SHADER_PARAMETER` declaration, or a plain `struct` field, proves the declaration exists at a path/line; it does **not** prove the binding is active in a captured frame.
2. **Resolution** (`source-schema.json`) only asserts a decode where explicit shader pack/unpack or a format contract exists. Runtime-selected formats and permutations are named in `storage.formatAlternatives` and in `gaps`.

Rules applied:

- No byte offsets are inferred from C++ `sizeof`/alignment. The only offsets present are explicit in shader pack code (`GetLumenCardData`, `GetLumenCardPageData`, `page-table packing`, VSM page format).
- `FLumenPageTableEntry` (CPU) is **not** the GPU page table. CPU fields `PhysicalPageCoord`, `PhysicalAtlasRect`, `CardIndex`, `ResLevel`, `CardUVRect`, `SubAllocationSize` are absent from `Lumen.PageTable`; only `SampleAtlasBiasX/Y`, `SampleCardResLevelX/Y`, `SamplePageIndex` are uploaded (LumenScene.cpp:752-757).
- `FLumenCardPageData` is a 5×float4 GPU stream (80-byte stride), not a native struct layout (LumenCardCommon.ush:76,169-199; LumenScene.cpp:321-361).
- `FVirtualShadowMapProjectionShaderData` is a byte-address record (`VSM_PSD_STRIDE = 16*23`), decoded by sequential `LoadAndIncrementOffset`; only two explicit offsets are annotated (`LightType` 220, `ResolutionLodBias` 252). Other offsets are not guessed.
- `FPhysicalPageMetaData` field order is source-ordered; its C++ stride is `sizeof(UE::HLSL::FPhysicalPageMetaData)` and is not asserted.

---

### 1.1 DumpGPU export contract (what the dump actually contains)

Source: `Engine/Source/Runtime/RenderCore/Private/DumpGPU.cpp`. `r.DumpGPU.PassParameters` defaults to 1 (`:88-91`); the pass dump path is `:2834-2895`, metadata/binary serialization `:708-834,2516-2592`.

| Export item | Serialized? | Evidence / limit |
|---|---|---|
| Pass scalar members and inline structs | **yes**, bytes in `Structures/<Parameters-pointer>.bin` | `Pass->GetParameters().GetContents()` + `Metadata->GetSize()` (`:2854-2882`); offsets/types in `StructuresMetadata/<Metadata-pointer>.json` (`:708-744`) |
| Pass parameter metadata | **yes**, JSON per metadata pointer | `Name`, `ShaderType`, `FileLine`, `Offset`, `BaseType`, `Precision`, `NumRows`, `NumColumns`, `NumElements`, `StructMetadata` |
| `SHADER_PARAMETER_STRUCT_REF` (referenced UB, e.g. View/BlueNoise/DeferredLight/ReflectionCapture) | **only if** the RHI uniform-buffer content map has an entry | `DumpReferencedStructs` handles only `UBMT_REFERENCED_STRUCT` (`:2516-2592`); resolves `FUniformBufferBinding::GetUniformBuffer()` and `UE::RHI::UniformBufferContentMap::Get`, writes a separate `Structures/<UB-pointer>.bin`; missing content logs a warning and requires `r.RHI.UniformBufferContentMap.Enable=1` |
| `SHADER_PARAMETER_RDG_UNIFORM_BUFFER` (e.g. `LumenCardScene`) | **not guaranteed** by this path | `DumpReferencedStructs` skips anything that is not `UBMT_REFERENCED_STRUCT` (`:2535-2540`); an RDG UB member is a binding. Treat `PhysicalAtlasSize`, card/page pointers in `LumenCardScene` as unresolved unless a separate capture provides them |
| Resource pointers inside a UB (textures/SRVs/UAVs/buffers, e.g. BlueNoise texture arrays, LTC textures, system textures) | **no** — only binding handles, never the pointed-to resource bytes | The referenced-struct binary copies the UB scalar/struct members; resource members are handles. Join to the dump resource inventory for those |
| Shader locals / computed values (`AtlasUV`, `Albedo_linear`, `FinalLighting`, sampler object internals) | **no** | Not members of the pass parameter struct; must be reconstructed by the equations in §3 |
| Sampler RHI object | **no initializer** | `BilinearClampedSampler` is a pointer; the static `TStaticSamplerState` construction must be derived from source (§6.1) |
| Buffers with `ERDGBufferFlags::SkipTracking` | **no binary** | `DumpGPU.cpp:1709-1713` logs “Not dumping ... because has ERDGBufferFlags::SkipTracking.” Default/Sentinel buffers (`DefaultBuffer`, `DefaultByteAddressBuffer`, `DefaultStructuredBuffer`, `VirtualTexture_FeedbackBuffer`, etc.) may be omitted; do not assert binding closure from metadata alone |
| Textures with `ERDGTextureFlags::SkipTracking` | read through a preprocess shader rather than skipped | `:940-942`; presence does not imply the same transition state as a normal resource |
| Staging cap | per-resource size cap | `r.DumpGPU.MaxStagingSize` default 64 MB; oversized resources are skipped with a warning (source map / `:1698`) |
| `Passes.json` shape | concatenated JSON objects, not a JSON array | `DumpGPU.cpp:2845` appends each pass object to the file; readers must stream/split multiple RDG graph instances and pass IDs |

Consequences for replay:

- Pass bytes can supply `CachedLightingPreExposure`, `TargetFormatQuantizationError`, `DiffuseColorBoost`, dispatch sizes, and inline structs **when those members are in the parameter struct**.
- `View.StateFrameIndexMod8` / `View.PreExposure` are only available if the View referenced-UB content is actually captured; the metadata record alone is not a value.
- `LumenCardScene` physical atlas size and card/page buffer handles are not proven by the pass binary; the source schema names them but marks them as gaps for dump-only replay.
- Sampler state is not serialized as bytes; derive it from the static construction in §6.1 and keep any runtime-created/dynamic sampler unresolved.
- SkipTracking/sentinel resources and resource pointers inside UBs mean **do not assert active-frame closure from parameter metadata alone**.

### 1.2 Active-frame coverage claim

`source-schema.json.coverage` is `semantic-subset`, `activeFrameComplete: false`. The 39 resource schemas are a source-verified semantic subset, not a claim that every active binding in the final capture is decoded. Active-frame closure comes from the capture's pass/resource/binding/UB inventory joined to this dictionary; unmatched bindings remain `source-only` in `declaration-index.json`.

## 2. Declaration extraction

`extract_declarations.py` parses, deterministically:

- `SHADER_PARAMETER*`, `SHADER_PARAMETER_STRUCT_*`, `SHADER_PARAMETER_RDG_*`, `SHADER_PARAMETER_ARRAY`, `SHADER_PARAMETER_SCALAR_ARRAY` in scoped C++ (`Engine/Source/Runtime/Renderer/Private/Lumen`, `.../VirtualShadowMaps`, plus explicit renderer files);
- `RDG_BUFFER_ACCESS` / `RDG_BUFFER_ACCESS_DYNAMIC` / `RDG_BUFFER_ACCESS_ARRAY` and `RDG_TEXTURE_ACCESS` / `RDG_TEXTURE_ACCESS_DYNAMIC` / `RDG_TEXTURE_ACCESS_ARRAY`, decoded with the macro-specific argument shape (first argument = member name, second = RHI access mask; no type argument);
- `RENDER_TARGET_BINDING_SLOTS()` (field `RenderTargets`, type `FRenderTargetBindingSlots`);
- `BEGIN_SHADER_PARAMETER_STRUCT`, all nine `BEGIN_GLOBAL_SHADER_PARAMETER_STRUCT` bodies with matching `END_GLOBAL_SHADER_PARAMETER_STRUCT`, and the two `BEGIN_UNIFORM_BUFFER_STRUCT` bodies with matching `END_UNIFORM_BUFFER_STRUCT`; anonymous `FParameters` are qualified by the surrounding shader class;
- plain `struct` declarations/fields in scoped `.ush/.usf` and the shared `VirtualShadowMapDefinitions.h`;
- `#include` edges and `IMPLEMENT_GLOBAL_SHADER(class,path,entry,stage)` bindings.

Parser features: comment stripping that preserves lines/strings, balanced nested parentheses, multi-line macros, arrays (`[N]`), bitfields, nested structs, active preprocessor guards (`preprocessorGuards`), and unparsed-field recording (text only, no invented names or offsets). Macro-definition lines (a leading `#`) are skipped, so `#define` examples and deprecated aliases are not counted as declarations.

Current extraction counts:

| metric | count |
|---|---|
| files considered / parsed / skipped | 225 / 225 / 0 |
| `SHADER_PARAMETER*` declarations | 2859 |
| `DECLARE_SCALAR_ARRAY` declarations | 1 |
| `RDG_BUFFER_ACCESS*` declarations (80 base + 0 dynamic/array) | 80 |
| `RDG_TEXTURE_ACCESS*` declarations (2 base + 0 dynamic/array) | 2 |
| `RENDER_TARGET_BINDING_SLOTS` declarations | 33 |
| shader parameter structs | 343 (332 local + 9 global + 2 uniform-buffer) |
| parameter-struct fields | 2974 |
| plain shader structs | 151 |
| plain struct fields | 725 |
| shader bindings / classes | 250 / 250 |
| include edges | 2485 |

Source-only status: every entry in the index is `decodingStatus: "source-only"` with gaps. Do not promote an unknown binding to a semantic resource merely because it is declared.

---

## 3. `CombineLumenSceneLightingCS` numerical reconstruction contract

Source: `Engine/Shaders/Private/Lumen/LumenSceneLighting.usf:471-507`; C++ pass setup `Engine/Source/Runtime/Renderer/Private/Lumen/LumenSceneLighting.cpp:138-207`; combine equation `Engine/Shaders/Private/Lumen/SurfaceCache/LumenSurfaceCache.ush:44-61`.

### 3.1 Inputs (every operand)

| Input | Source / value | Notes |
|---|---|---|
| dispatch group | `SV_GroupID.x` = card-tile index | indirect args: 1 group per card tile (`LumenSceneLighting.cpp:205-206`) |
| `IndirectArgsBuffer` | `RDG_BUFFER_ACCESS(IndirectArgsBuffer, ERHIAccess::IndirectArgs)` | `FLumenCardCombineLightingCS::FParameters` field 1 of 13; dispatch offset **12 bytes** = `ELumenDispatchCardTilesIndirectArgsOffset::OneGroupPerCardTile = 1 * sizeof(FRHIDispatchIndirectParameters)` (`LumenSceneLighting.h:168-173`, `LumenSceneLighting.cpp:144,205-206`). `FRHIDispatchIndirectParameters` is three `uint32` counts (`RHI.h:542-556`); only a platform with `PLATFORM_DISPATCH_INDIRECT_ARGUMENT_BOUNDARY_SIZE` 64/128 adds padding and changes the stride. |
| `CardTiles` | `StructuredBuffer<uint>`, `Lumen.CardTiles` | packed tile; only `[0, dispatchCount)` valid |
| `LumenCardScene` | uniform buffer, 19 fields | `FLumenCardScene` (`LumenSceneData.h:45-65`): 8 scalar/vector values, 7 RDG buffer SRVs and 4 atlas textures. Field count is 19, not 14. Byte offsets are C++ ABI and are not asserted; RDG UB payloads are not serialized by DumpGPU. |
| `CardPageData` | 5×float4 per card page | `GetLumenCardPageData` (LumenCardCommon.ush:169-199) |
| `AlbedoAtlas` | `Lumen.SceneAlbedo` | sqrt-encoded RGB, 8-bit; BC7 path optional |
| `EmissiveAtlas` | `Lumen.SceneEmissive` | pre-exposed radiance, R11G11B10; BC6H path optional |
| `DirectLightingAtlas` | `Lumen.SceneDirectLighting` | pre-exposed **irradiance** E_d |
| `IndirectLightingAtlas` | `Lumen.Radiosity.SceneIndirectLighting` | pre-exposed **irradiance** E_i; null → black |
| `BilinearClampedSampler` | `SF_Bilinear`, `AM_Clamp` ×3 | `TStaticSamplerState<SF_Bilinear, AM_Clamp, AM_Clamp, AM_Clamp>` (LumenSceneLighting.cpp:190) |
| `DiffuseColorBoost` | `1/max(View.FinalPostProcessSettings.LumenDiffuseColorBoost, 1)` | default post-process value is 1.0 (Scene.cpp:598), so parameter = 1.0 |
| `View.DiffuseOverrideParameter` | default `(0,0,0,1)` | SceneView.cpp:905,2816 |
| `IndirectLightingAtlasHalfTexelSize` | `0.5 / GetRadiosityAtlasSize()` per axis | LumenSceneLighting.cpp:193-194 |
| `TargetFormatQuantizationError` | `Lumen::GetLightingQuantizationError()` | default R11G11B10: `(2^-6,2^-6,2^-5)` |
| frame seed | `View.StateFrameIndexMod8 + 2` | `StateFrameIndexMod8 = FrameIndex % 8` (SceneRendering.cpp:1935) |

Default lighting format: `PF_FloatR11G11B10` unless `r.Lumen.LightingDataFormat` selects `PF_FloatRGBA` (1) or `PF_A32B32G32R32F` (2) (`Lumen.cpp:94-107`).
Quantization error per format: `2^-mantissa` per channel; R11G11B10 = 6/6/5 mantissa bits (`Quantization.cpp:9-50`).

### 3.2 Decode steps

1. `Packed = CardTiles[GroupId.x]`
   - `CardPageIndex = Packed & 0xFFFFFF`
   - `TileCoord = uint2((Packed>>24)&0xF, (Packed>>28)&0xF)`
   - `0xFFFFFFFF` is the null tile (`LumenCardTile.ush:11-34`).
2. `Page = CardPageData[CardPageIndex*5 .. +4]`:
   - `CardIndex = asuint(Page[0].x)`, `ResLevelPageTableOffset = asuint(Page[0].y)`, `SizeInTexels = Page[0].zw`
   - `CardUVRect = Page[1]`, `PhysicalAtlasUVRect = Page[2]`
   - `CardUVTexelScale = Page[3].xy`, `ResLevelSizeInTiles = asuint(Page[3].zw)`
   - `LastDirect/Indirect`, `IndirectLightingTemporalIndex`, `DirectLightingTemporalIndex = asuint(Page[4].xyzw)`
   - Derived: `PhysicalAtlasCoord = uint2(PhysicalAtlasUVRect.xy * PhysicalAtlasSize + 0.5)`
   - Derived: `PhysicalAtlasUVTexelScale = InvPhysicalAtlasSize`
3. `CoordInCardPage = 8*TileCoord + SV_GroupThreadID.xy`
4. `AtlasCoord = PhysicalAtlasCoord + CoordInCardPage`
5. `AtlasUV = PhysicalAtlasUVRect.xy + InvPhysicalAtlasSize * (CoordInCardPage + 0.5)`
6. Indirect UV:
   - default `RADIOSITY_ATLAS_DOWNSAMPLE_FACTOR == 1` → `IndirectLightingAtlasUV = AtlasUV`
   - else clamp `AtlasUV` to `[PhysicalAtlasUVRect.xy + halfTexel, PhysicalAtlasUVRect.zw - halfTexel]`; `halfTexel = 0.5/IndirectLightingAtlasSize`
   (`LumenSceneLighting.usf:493-498`; permutation constant `LumenRadiosity.ush:13`)
7. Four bilinear-mip0 reads, all with the same clamped sampler:
   - `Albedo = AlbedoAtlas.SampleLevel(AtlasUV)`
   - `Emissive = EmissiveAtlas.SampleLevel(AtlasUV)`
   - `Direct = DirectLightingAtlas.SampleLevel(AtlasUV)`
   - `Indirect = IndirectLightingAtlas.SampleLevel(IndirectLightingAtlasUV)`
8. `AlbedoLin = ApplyDiffuseColorBoost(Albedo*Albedo, DiffuseColorBoost)`:
   - if `DiffuseColorBoost < 1`: `lum = Luminance(C)`, `C = min(saturate(C * pow(lum, boost) / max(lum,1e-8)), 0.99)`
   - `C = C * View.DiffuseOverrideParameter.w + View.DiffuseOverrideParameter.xyz`
9. `L = max(MakeFinite(AlbedoLin * (1/PI) * (Direct + Indirect) + Emissive), 0)`
   - `PI = 3.1415926535897932f` (Common.ush:133)
   - `MakeFinite(x) = 0` when `(asuint(x) & 0x7F800000) == 0x7F800000` (NaN/Inf), else `x` (Common.ush:2260-2276)
10. `stored = QuantizeFloatColor(L, qerr, E)`:
    - `E = Rand16ToFloat(Rand3DPCG16(int3(AtlasCoord, View.StateFrameIndexMod8 + 2)).x)`
    - `Rand3DPCG16`: `v = uint3(p) * 1664525 + 1013904223`; two Feistel rounds `v.x+=v.y*v.z; v.y+=v.z*v.x; v.z+=v.x*v.y;` (twice); return `v >> 16` (`RandomPCG.ush:12-44`)
    - `Rand16ToFloat(r) = r / 65536` (`RandomPCG.ush:107-110`)
    - `err = L * qerr`; each channel `err = asfloat(asuint(err) & ~0x007FFFFF)` (clears low 23 mantissa bits); `stored = L + err*E` (`Quantization.ush:13-21`; `LumenFloatQuantization.ush:24-28`)
11. `RWFinalLightingAtlas[AtlasCoord] = stored` (R11G11B10 write; hardware rounds to the format).

### 3.3 Exposure transforms

- Surface-cache direct/emissive/final/radiosity are stored **pre-exposed** with `CachedLightingPreExposure`, a fixed-EV value independent of eye adaptation:
  `CachedLightingPreExposure = 1 / 2^clamp(r.EyeAdaptation.CachedLightingPreExposure, -16, 16)`; default CVar = 4 EV → **1/16 = 0.0625** (`PostProcessEyeAdaptation.cpp:201-243`).
- Store sites: direct `Irradiance * CachedLightingPreExposure` (LumenSceneDirectLighting.usf:163); card emissive `Emissive * CachedLightingPreExposure` (LumenCardBasePass.ush:147); radiosity trace radiance `Radiance * CachedLightingPreExposure` (LumenRadiosity.usf:161).
- Decode sites: `* OneOverCachedLightingPreExposure` when sampling direct/indirect/final/emissive (LumenSurfaceCacheSampling.ush:312-314,340; radiance cache LumenRadianceCacheInterpolation.ush:272).
- Screen-probe trace radiance uses **View.PreExposure**, not CachedLightingPreExposure: current trace lighting is multiplied by `View.PreExposure` (LumenScreenProbeTracing.usf:663), and history is converted with `PrevInvPreExposure * View.PreExposure` (LumenScreenProbeFiltering.usf:260). Thus `Lumen.ScreenProbeGather.DiffuseIndirect` is pre-exposed with View.PreExposure.
- GBufferC indirect irradiance uses `View.PreExposure` (`EncodeIndirectIrradiance`, DeferredShadingCommon.ush:221-235).
- To convert a stored surface-cache value to physical radiance/irradiance, multiply by `1/CachedLightingPreExposure = 2^EV` (default 16). For screen-probe/GBuffer values, multiply by `1/View.PreExposure`.

### 3.4 Undecodable / missing inputs (gaps)

These are required for byte-exact reconstruction but are not present in the raw atlas bytes:

1. `View.StateFrameIndexMod8` (or `FrameIndex`) — needed for the dither seed. Without it, `E` is unknown and the quantized value cannot be reproduced exactly.
2. `CachedLightingPreExposure` (or the EyeAdaptation EV/CVar) — needed to de-preexpose.
3. `View.DiffuseOverrideParameter`, `DiffuseColorBoost` — needed for albedo reconstruction.
4. `PhysicalAtlasSize` / `InvPhysicalAtlasSize` — not stored in the card page stream; from `LumenCardScene` uniform buffer.
5. Exact `BilinearClampedSampler` state and hardware bilinear/edge behavior; including sub-texel precision.
6. Actual `FinalLightingAtlas` format (`PF_FloatR11G11B10` vs `PF_FloatRGBA` vs `PF_A32B32G32R32F`) and therefore `TargetFormatQuantizationError`.
7. Compressed surface-cache permutation (BC7/BC5/BC6H): block layout is not expanded; use the uncompressed path only when the capture provides uncompressed layers.
8. `RADIOSITY_ATLAS_DOWNSAMPLE_FACTOR` runtime permutation (default 1) and `IndirectLightingAtlasSize`; the clamp branch is compiled out at 1.
9. Stale/persistent atlas state: `SessionFinalLightingAtlas` is a persistent feedback atlas; the capture must include its previous contents for frame-to-frame history, although the combine itself is stateless for one texel.
10. The indirect lighting atlas may be null (black) on the first frame / when radiosity is disabled; treat as 0.

No raw numbers are fabricated here: the equations are source text, the constants are stated CVar/format defaults.

---

## 4. High-value decodes (source-verified)

### 4.1 GBuffer

| Quantity | Storage | Decode |
|---|---|---|
| GBufferA RGB | `PF_A2B10G10R10` default, 10:10:10:2 (`GBufferInfo.cpp:154-161,344`) | raw affine `N_raw = 2*code - 1`, **not** octahedral (`EncodeNormal: return N*0.5+0.5` / `DecodeNormal: return N*2-1`, DeferredShadingCommon.ush:137-147). Normalization is a **separate consumer step**: `DecodeGBufferData` calls `DecodeNormal(InGBufferA.xyz)` and only applies `normalize()` when `bGetNormalizedNormal=true` (lines 1092-1096). Do not fold the normalize into the storage decode. |
| GBufferA A | 2 bits | per-object payload, material-defined; unknown generic decode |
| GBufferB | `PF_B8G8R8A8`; shader .r/.g/.b/.a (DeferredShadingCommon.ush:1007-1010) | metallic=.r, specular=.g, roughness=.b; A = `(ShadingModelID & 0xF) | SelectiveOutputMask`, stored `Value/255` (lines 295-309) |
| GBufferC | `PF_B8G8R8A8`, sRGB target (`GBufferInfo.cpp:367`) | base color sRGB→linear; A is one of diffuse-sample occlusion / indirect irradiance / AO depending on `GBUFFER_HAS_DIFFUSE_SAMPLE_OCCLUSION` and `ALLOW_STATIC_LIGHTING` (lines 1014-1021). Default `r.GBufferDiffuseSampleOcclusion=0` (RenderUtils.cpp:747-750). Indirect irradiance: `E = OneOverPreExposure*(exp2(16*A-8)-2^-8)` (lines 221-235). |
| GBufferD/E | `PF_B8G8R8A8` | custom data (unknown per material) / optional precomputed shadow factors |

MRT targets are named GBufferA..E but target indices shift when Velocity/Tangent are enabled (`GBufferInfo.cpp:369-405`); selectors use names, not indices.

### 4.2 Lumen surface cache material layers

| Layer | Format | Encode → Decode |
|---|---|---|
| `Lumen.SceneAlbedo` | `PF_R8G8B8A8` (BC7 optional) | `Target0 = sqrt(DiffuseColor)` (LumenCardBasePass.ush:145). Decode `A^2`, then `ApplyDiffuseColorBoost`. Alpha = capture opacity, not used by combine. |
| `Lumen.SceneNormal` | `PF_R8G8` (BC5 optional) | `Target1 = CardSpaceNormal.xy*0.5+0.5` (146). `z=+sqrt(max(1-x²-y²,1e-4))`; world = `mul(Card.WorldToLocalRotation, N_card)` (LumenSurfaceCache.ush:26-39). |
| `Lumen.SceneDepth` | `PF_G16` | card ortho depth; `EncodeSurfaceCacheDepth` clamps to `(0xFFFF-1-0.5)/0xFFFF` and reserves 1.0 invalid (LumenSurfaceCache.ush:13-24). `LocalZ=-(2D-1)*Extent.z` (LumenCardCommon.ush:315-324). |
| `Lumen.SceneEmissive` | `PF_FloatR11G11B10` (BC6H optional) | `Emissive * CachedLightingPreExposure` (LumenCardBasePass.ush:147); decode `* OneOverCachedLightingPreExposure`. |

Card capture targets are RGBA8 albedo/normal and R11G11B10 emissive (`LumenSceneCardCapture.cpp:486-535`). The capture view axes are the card OBB axes (`LumenSceneCardCapture.cpp:720-742`), which is why the view-space normal written at `LumenCardBasePass.ush:141` equals card space; the comment says “local card space”. The final on-disk capture uses BC7/BC5/BC6H for these layers (§6.5), so `Lumen.SceneNormal` and `Lumen.SceneEmissive` are `partial` until those blocks are decoded.

### 4.3 Direct/Indirect/Final units

- `Lumen.SceneDirectLighting` = **irradiance** E_d, pre-exposed with CachedLightingPreExposure. Accumulated `Σ GetIrradianceForLight(...) * ShadowFactor` (LumenSceneDirectLighting.usf:146), quantized with `CardPage.DirectLightingTemporalIndex` (line 163).
- `Lumen.Radiosity.SceneIndirectLighting` = **irradiance** E_i from cosine-convolved SH, pre-exposed (Radiosity integrate; see 4.4).
- `Lumen.SceneFinalLighting` = **outgoing radiance** `rho/π*(E_d+E_i)+E_m`, pre-exposed (LumenSurfaceCache.ush:50-61). It is not another irradiance.

Lighting storage format is runtime-selected by `r.Lumen.LightingDataFormat`: 0 = `PF_FloatR11G11B10` (default), 1 = `PF_FloatRGBA`, 2 = `PF_A32B32G32R32F` (`Lumen.cpp:45-53,94-108`). The `bitOffset`/`bitWidth` fields in the seven lighting resource schemas carry `appliesToFormats: ["PF_FloatR11G11B10"]`; the alternative layouts are recorded explicitly in `storage.formatVariants` (4×16-bit at byte offsets 0/2/4/6, stride 8; 4×32-bit at 0/4/8/12, stride 16; `PixelFormat.cpp:35,44,60`). Those resources are therefore `partial`, not `complete`, because format-specific hardware rounding and alpha use are not asserted.

### 4.4 Radiosity SH is radiance moments, not irradiance SH

`LumenRadiosityConvertToSH` stores:

`SH = (1/N) * Σ AddSH(SHBasisFunction(dir), TraceRadiance / PDF)` (`LumenRadiosity.usf:358-383`).

The local name `IrradianceSH` is a misnomer: the atlas contains **pre-exposed radiance moments**. The cosine transfer happens at integration:

`E(n) = max(0, DotSH(Σ w_i SH_i, CalcDiffuseTransferSH(n,1))) / WeightSum` (`LumenRadiosity.usf:540-546`; `CalcDiffuseTransferSH` at SHCommon.ush:275-287).

The three `Lumen.Radiosity.ProbeSH{Red,Green,Blue}Atlas` are `PF_FloatRGBA` (4 two-band coefficients per RGB channel), allocated at LumenRadiosity.cpp:683-705.

### 4.5 Screen probe diffuse indirect is E/π

`DiffuseIndirectOutput = DiffuseLighting * Diffuse_Lambert(float3(1,1,1)) = DiffuseLighting / PI` (LumenScreenProbeGather.usf:1459; Diffuse_Lambert BRDF.ush:157-160).

It is stored quantized with `BlueNoiseScalar(IntegrateCoord.xy, View.StateFrameIndexMod8)` (LumenScreenProbeGather.usf:1492-1495). Composite multiplies by material diffuse color: `IndirectLighting.Diffuse = (DiffuseIndirectLighting * DiffuseColor + Backface...) * Occlusion * EnergyPreservationFactor + ShortRangeGI * DiffuseColor` (DiffuseIndirectComposite.usf:646). So the stored value is **E/π**, not final radiance; final diffuse radiance = albedo * stored (albedo applied in composite).

`LightIsMoving` is an R8 history alpha; 0 disables history and valid values are clamped to ≥0.004 (`LumenScreenProbeGather.usf:1450-1459`).

### 4.6 Structured importance `SampleWeight` is not BSDF PDF

In `LumenScreenProbeFiltering.usf:65-85`:

```
MipSize = MaxImportanceSamplingOctahedronResolution >> RayLevel
SampleWeight = (ScreenProbeGatherOctahedronResolution / MipSize)^2
Lighting = TraceRadiance.Load(...) * SampleWeight
if max(Lighting) > MaxRayIntensity: Lighting *= MaxRayIntensity / max(Lighting)
```

`SampleWeight` is the mip/solid-angle resampling prior (`(GatherRes/MipSize)^2`) and `MaxRayIntensity` is applied **after** it (`:82-85`). It is not the BSDF PDF (`BRDFProbabilityDensityFunctionSH` is a separate input, `LumenScreenProbeImportanceSamplingShared.ush:5-8`).

### 4.7 Virtual Shadow Maps are depth pages, not variance maps

- `Shadow.Virtual.PhysicalPagePool` is `PF_R32_UINT`; depth is read as `asfloat(VirtualShadowMap.PhysicalPagePool.Load(uint4(PhysicalTexelAddress,0,0)))` (`VirtualShadowMapProjectionCommon.ush:160-163`; pool format `VirtualShadowMapCacheManager.cpp:1118`).
- The array slices are: `0` = merged/dynamic depth, `1` = static cached depth, `2` = visualization overdraw count (init copy `VirtualShadowMapPhysicalPageManagement.usf:1000-1016`; slice-2 clear `VirtualShadowMapArray.cpp:3466-3473`; overdraw read `VirtualShadowMapProjection.usf:904`).
- There is no variance/moment pair in the VSM page pool. Receiver masks are 4×4 visibility submasks (2×2 uint texels per page, `VirtualShadowMapPageMarking.ush:62-80`, lookup `VirtualShadowMapProjectionCommon.ush:731-739`).
- Page table entry: bits 0-9 physical x, 10-19 physical y, 20-25 LOD offset, 26-28 unused, 29 secondary request, 30 valid for rendering, 31 any LOD valid (`VirtualShadowMapPageAccessCommon.ush:237-287`).
- Page flags: bits 0-5 = allocated / dynamic uncached / static uncached / detail geometry / primary request / secondary request, OR-propagated hierarchically (`VirtualShadowMapPageAccessCommon.ush:13-39`). Extended flags bits 6-15 live in physical metadata, not page flags.

---

## 5. Packed bit reference

| Structure / buffer | Bits |
|---|---|
| `CardTiles` (`Lumen.CardTiles`) | `[0:23]` CardPageIndex, `[24:27]` TileCoord.x, `[28:31]` TileCoord.y; `0xFFFFFFFF` null (`LumenCardTile.ush:11-34`) |
| `FLumenCardData` packed0 | `[0:7]` ResLevelToResLevelXYBias.x, `[8:15]` .y, `[16:19]` AxisAlignedDirection, `[20:23]` LightingChannelMask, `[24]` bVisible, `[25]` bHeightfield, `[26]` bFarField (`LumenCardCommon.ush:104-111`) |
| `Lumen.PageTable` | word0: `[0:11]` SampleAtlasBiasX, `[12:23]` SampleAtlasBiasY, `[24:27]` SampleCardResLevelX, `[28:31]` SampleCardResLevelY; word1: SamplePageIndex (`LumenScene.cpp:725-760`; `LumenSurfaceCacheSampling.ush:116-128`) |
| `Lumen.PageBuffer` | 5 float4 per page; vector order documented in §3.2 (`LumenCardCommon.ush:169-199`; `LumenScene.cpp:321-361`) |
| Card-tile shadow downsample | 8 dwords per physical tile; bit index `lightIndex & 255`; dword = `8*tile + (bit/32)` (`LumenCardTileShadowDownsampleFactor.ush:11-22,93-106`) |
| Lumen direct shadow mask | 8-bit path: 7-bit factor + 1 complete; 16-bit colored path: 5+5+5 RGB + 1 complete; bit offset `SHADOW_MASK_RAY_BITS*(x+8*y)` (8 times the index for the 8-bit path, 16 times for the 16-bit colored path) (`LumenSceneDirectLighting.ush:41-66,101-155`) |
| Screen probe ray info | `[0:5]` x, `[6:11]` y, `[12:15]` level (`LumenScreenProbeTracingCommon.ush:67-78`) |
| Screen probe trace hit | `[0]` hit, `[1]` moving, `[2:30]` 29-bit float hit-distance field, `[31]` reached radiance cache (`LumenScreenProbeCommon.ush:239-269`) |
| Screen probe trace texel (compaction) | `[0:19]` probe index, `[20:24]` x, `[25:29]` y (`LumenScreenProbeCommon.ush:202-212`) |
| Adaptive placement mask | `R16_UINT`; bit i = sample i needs an adaptive probe; `NumSamplesPerUniformProbe` ∈ {4,8,16} (`LumenScreenProbeGather.usf:1744-1806`; cpp `LumenScreenProbeGather.cpp:2335-2345`) |
| Screen tile adaptive header / indices | header `R32_UINT` count per tile; indices `R16_UINT`, address `(index%factor, index/factor)*ScreenProbeViewSize + tile`; data `R32_UINT` = x low16, y high16 (`LumenScreenProbeCommon.ush:99-119`; `LumenScreenProbeGather.usf:1921-1943`) |
| Radiance-cache probe depth | fp16 hit distance in bits `[1:14]` (`f32tof16(d)&0x7FFE`), bit15 frontface, bit0 two-sided, `0xFFFF` miss (`LumenRadianceCacheInterpolation.ush:196-235`) |
| Radiance-cache indirection | `[0:15]` ProbeIndex, `[16:18]` NumAdaptiveProbes, `0xFFFFFFFF` invalid (`LumenRadianceCacheInterpolation.ush:295-318`) |
| VSM page table | see §4.7 |
| VSM receiver mask | 2×2 uint texels per page; each quadrant 16 bits; bit `(sub.y*4+sub.x)`; quadrant `(vx>>6&1, vy>>6&1)`, sub `((vx>>4)&3,(vy>>4)&3)` (`VirtualShadowMapPageMarking.ush:71-80`; `VirtualShadowMapProjectionCommon.ush:731-739`) |
| Screen-probe history count | `R8`: `[3:0]` NumFramesAccumulated, `[7:4]` fast-update amount (`LumenScreenProbeGatherTemporal.ush:71-90`) |
| Screen-probe scene depth | `R32_UINT` holding `asfloat`; negative sign = invalid/unlit (`LumenScreenProbeGather.usf:83-95`; `LumenScreenProbeCommon.ush:78-92`) |

---

## 6. Preexposure / history / sampler dependencies

- **Sampler:** combine uses `SF_Bilinear` with clamp addressing on all three axes and mip 0 (`LumenSceneLighting.cpp:190; LumenSceneLighting.usf:500-503`). Border/edge sampling must use the same clamp and half-texel convention; the indirect downsample branch adds a half-texel clamp inside the physical rect.
- **History validity:** screen-probe history is valid only if previous diffuse-indirect and depth textures exist (`LumenScreenProbeGather.cpp:1797-1801,1879`). `LightIsMoving` and `HistoryFastUpdateMode_NumFramesAccumulated` carry the per-pixel confidence/count. Radiosity history requires a valid final-lighting atlas and matching atlas size/format (`LumenRadiosity.cpp:646-650`). Direct-lighting stochastic history is allocated only when stochastic lighting is enabled (`LumenSurfaceCache.cpp:268-285`).
- **Moments:** direct-lighting stochastic history A = `luminance(diffuse)^2` (`LumenSceneDirectLightingStochastic.usf:890,929`); reflection specular history A = `luminance(specular)^2` (`LumenReflectionDenoiserTemporal.usf:344,583`), RGB stored in a denoiser tonemap space (`LumenReflectionDenoiserCommon.ush:14-40`).
- **Temporal pre-exposure correction:** on reuse, stored radiance is multiplied by `PrevSceneColorPreExposureCorrection`, and second moments by its square (`LumenSceneDirectLightingStochastic.usf:899-900`; `LumenReflectionDenoiserTemporal.usf:259-260`; screen-probe history uses `PrevInvPreExposure * View.PreExposure`, `LumenScreenProbeFiltering.usf:260`).
- **Input versions needed by the explorer:**
  1. resource descriptor: name, format, extent, array size, mip count, flags;
  2. pass metadata: pass name, dispatch group count, active permutation defines (especially `RADIOSITY_ATLAS_DOWNSAMPLE_FACTOR`, GBuffer layout, Substrate, `GBUFFER_HAS_DIFFUSE_SAMPLE_OCCLUSION`);
  3. view/frame uniforms: `View.StateFrameIndexMod8`, `View.PreExposure`, `View.OneOverPreExposure`, `View.DiffuseOverrideParameter`;
  4. Lumen uniforms: `PhysicalAtlasSize`, `InvPhysicalAtlasSize`, `CachedLightingPreExposure`, `TargetFormatQuantizationError`, cardinality of `CardTiles` dispatch;
  5. CVars: `r.Lumen.LightingDataFormat`, `r.EyeAdaptation.CachedLightingPreExposure`, `r.Lumen.DiffuseColorBoost` (post-process), `r.GBufferDiffuseSampleOcclusion`, surface-cache compression mode, `r.Substrate`.

Without items 3-5 the byte-exact value is underdetermined; these are recorded as gaps rather than guessed.

---

### 6.1 Sampler bindings (static construction only)

`TStaticSamplerState` defaults (`RenderCore/Public/RHIStaticStates.h:59-79`): `Filter=SF_Point`, `AddressU/V/W=AM_Clamp`, `MipBias=0`, `MaxAnisotropy=1`, `BorderColor=0`, `SamplerComparisonFunction=SCF_Never`; `FSamplerStateInitializerRHI` is constructed with `MinLOD=0, MaxLOD=FLT_MAX`.

| Binding | Construction source | State |
|---|---|---|
| `FLumenCardCombineLightingCS::BilinearClampedSampler` | `LumenSceneLighting.cpp:190` `TStaticSamplerState<SF_Bilinear, AM_Clamp, AM_Clamp, AM_Clamp>` | bilinear, clamp U/V/W, mipBias 0, maxAniso 1, border 0, no comparison, LOD [0,FLT_MAX] |
| `FDiffuseIndirectCompositePS::DiffuseIndirectSampler` | `IndirectLightRendering.cpp:1500` `TStaticSamplerState<SF_Point>` | point, clamp, defaults |
| VSM `PageTableSampler` | `VirtualShadowMapArray.cpp:983` | bilinear, clamp, defaults |
| VSM `BilinearClampedSampler` | `VirtualShadowMapArray.cpp:995` | bilinear, clamp, defaults |
| VSM `PhysicalPagePoolSampler` | `VirtualShadowMapArray.cpp:4876` | point, clamp, defaults |
| stochastic direct `BilinearClampedSampler` | `LumenSceneDirectLightingStochastic.inl:648` | bilinear, clamp, defaults |
| `ShadowDepthSampler` | `ShadowDepthRendering.cpp:1014` | point, clamp, defaults |
| `LightAttenuationTextureSampler` | `LightRendering.cpp:2811` | point, wrap U/V/W, defaults |

These are **static construction derivations**, not serialized sampler objects. `TStaticSamplerState::GetRHI()` returns an RHI pointer; DumpGPU pass bytes contain the pointer, not filter/address fields. Runtime-created or platform-adjusted sampler states are unresolved and must stay explicit gaps. This inventory is bounded to scoped source and is not an exhaustive list of every sampler in the engine.

### 6.2 Selector policy and bit-width corrections

- `selectors` use anchored, exact resource names. `passNameRegex` is retained only where the pass identity is essential: GBufferA-E (`^BasePass`) and `Lumen.SceneFinalLighting` (`^CombineLighting CS$`). For uniquely named resources the pass restriction was removed so a child RDG event name does not create a false negative. In particular, `Lumen.CardTiles` now has only `resourceNameRegex: "^Lumen\\.CardTiles$"`; the direct-lighting pass is named `Batched lights` (`LumenSceneDirectLighting.cpp:1270`), which the old `^(CombineLighting CS|DirectLighting|Integrate)$` regex missed.
- Screen-probe `TraceHit.HitDistance` is **float29 at bitOffset 2**: mask `0x7FFFFFFC` occupies bits 2..30 (29 bits); bit 31 is `bReachedRadianceCache`. The previous 30-bit description was wrong.
- Radiance-cache `DepthProbeAtlasTexture.HitDistance` is **float14 at bitOffset 1**: mask `0x7FFE` occupies bits 1..14; bit 0 is `bTwoSided` and must not be included. The previous offset-0/width-15 description overlapped the flag.
- `Lumen.PageTable` res-level sizing is the piecewise `ResLevelXYToSizeInPages`: `select(ResLevelXY > 7, uint2(1u << (ResLevelXY - 7)), uint2(1))` (`LumenSurfaceCacheSampling.ush:88-91`). There is no negative-shift formula; the previous `1<<(resY-7)` wording was incomplete.

### 6.3 Radiance-cache parallax correction: active vs compiled-out

`SampleRadianceCacheProbeWithParallaxCorrection` (`LumenRadianceCacheInterpolation.ush:332-404`):

- Active (`SIMPLE_SPHERE_PARALLAX 1`, `:343`): ray-sphere intersect with the probe-centered sphere; the code takes the **far** root `.y` (`:348-350`). `q = intersection - probePosition` (`:351`); `CorrectionFactor = T^2 / (R * dot(q, omega))` (`:354`); applied to the sampled radiance at `:402`.
- **Disabled**: distant-depth parallax test is inside `#if RADIANCE_CACHE_DEPTH_TEST_SPHERE_PARALLAX && 0` (`:358-372`). Although code exists, it is compiled out and must not be listed as active.
- **Disabled alternate**: `#elif TRACE_THROUGH_PROBE_DEPTHS_REFERENCE` (`:374-399`), with `TRACE_THROUGH_PROBE_DEPTHS_REFERENCE 0` at `:344`.

`declaration-index.json` now records `preprocessorGuards` on macros, parameter structs, plain structs and fields from the active `#if/#ifdef/#ifndef/#elif/#else` stack; a guard containing a literal `&& 0` is source-only. Function-body branches such as the one above are not declarations, so they are recorded here explicitly.

### 6.4 Radiance-cache probe depth bit widths

`EncodeProbeDepth` (`:215-225`) writes `Encoded = (f32tof16(d) & 0x7FFE) | (bFrontface ? 0x8000 : 0) | (bTwoSided ? 1 : 0)`. Therefore the **distance field is bits [1:14] (14 bits, bitOffset 1, float14)**, bit 0 is `bTwoSided`, bit 15 is `bFrontface`, and `0xFFFF` is the miss sentinel. Similarly, screen-probe `TraceHit` distance uses mask `0x7FFFFFFC`, i.e. **29 bits [2:30]**; bit 31 is `bReachedRadianceCache`.

### 6.5 Final-capture cross-check (metadata only; no GPU run by this step)

Metadata read from `C:/Users/Administrator/AppData/Local/Temp/UnrealUnderstanding-Evidence/buffers/gpudump/binary-20261112-133826/Base/ResourceDescs.json` (concatenated JSON objects):

| Finding | Consequence |
|---|---|
| `Lumen.SceneAlbedo` = `PF_BC7`, `Lumen.SceneNormal` = `PF_BC5`, `Lumen.SceneEmissive` = `PF_BC6H` | the uncompressed byte offsets are source-path only; these three schemas are `partial` for the final capture until a BC7/BC5/BC6H block decoder is applied |
| `SceneDepthZ` = `PF_DepthStencil` | selector fixed to `^SceneDepthZ$` (not `SceneDepth`) |
| `Shadow.Virtual.PhysicalPagePool` = `PF_R32_UINT`, `ArraySize=2` | only slices 0/1 exist; the slice-2 overdraw field is optional/absent in this capture |
| `Shadow.Virtual.PageFlags` `NumMips=7`, `PageReceiverMasks` `NumMips=8` | per-mip hierarchy; one-entry decode is not the whole hierarchy |
| `Lumen.RadianceCache.FinalIrradianceAtlas` and `ProbeOcclusionAtlas` absent | optional paths; `CalculateIrradiance` was false for that cache |
| `Lumen.PageTable` = 4 B/element, 2048 elements; `Lumen.PageBuffer` = 16 B/element, 5120 elements | 2 elements per page-table entry (8 B/page), 5 elements per card page (80 B/page); schemas carry both element stride and page stride |
| `GBufferA/B/C/D` present; `GBufferE` absent | E is optional precomputed-shadow-factor target |
| `Lumen.ScreenProbeGather.DiffuseIndirect` `ArraySize=1` | ClosureCount was 1 in this capture; field array-slice note is runtime |

This cross-check does not assert active-frame closure. `source-schema.json.coverage.activeFrameComplete` remains `false`; pass/resource/UB joins, SkipTracking omissions, and compressed blocks still determine what can be replayed exactly.

## 7. Unknown semantics / blockers

The following are explicitly `unknown` or `partial` in `source-schema.json` (see each schema's `gaps`):

- GBufferD custom data: per-material semantics; no generic decode.
- GBufferE precomputed shadow factors: optional target, channel-to-light mapping not stored.
- GBufferC alpha: three runtime permutations; quantization bias is not recoverable from the stored byte alone.
- SceneDepth exact pixel format is platform/feature selected; stencil not decoded.
- `Lumen.SceneAlbedo` compressed BC7 block layout and alpha role; `Lumen.SceneNormal` BC5; `Lumen.SceneEmissive` BC6H.
- `FLumenCardData` LWC `DFHackToFloat/MakeDFVector3` decode, `PackedMeshCardsAxes` shuffle semantics, and the exact `AxisAlignedDirection` enum meaning are not expanded; fields are recorded with source pack offsets only.
- Radiance cache probe atlas indexing/interpolation, octahedral layout, clipmap addressing; only texel decode is asserted.
- VSM physical page pool slice semantics (partly inferred from init/read sites) and the platform-dependent array size; VSM physical metadata stride (C++ ABI).
- Reflection history denoiser-space validity sentinel and tonemap range are runtime inputs; only the storage transform is asserted.
- `FVirtualShadowMapProjectionShaderData` most byte offsets are not annotated in source; not guessed.
- Screen-probe/radiance-cache values are pre-exposed; exact de-exposure requires captured View uniforms.
- Compressed surface-cache/BC paths require either the uncompressed dump or a block decoder; offsets in the schema are for the uncompressed path unless noted.

No semantic status is promoted merely because a declaration exists: `declaration-index.json` entries are `source-only`; `source-schema.json` statuses are `complete` only where the encode/decode is explicit in shader source or an explicit format contract.

---

## 8. Validation

`validation.json` records:

- JSON parse of all five artifacts;
- existence and line range of every `sourceRefs` path/line, and whether the declared `symbol` substring is present on that exact line;
- declaration-index coverage/count consistency and existence of every parsed path;
- an independent comment-stripped macro audit over the same 225 files: `SHADER_PARAMETER*` 2859, `DECLARE_SCALAR_ARRAY` 1, `RDG_BUFFER_ACCESS*` 80, `RDG_TEXTURE_ACCESS*` 2, `RENDER_TARGET_BINDING_SLOTS` 33, `BEGIN/END_GLOBAL_SHADER_PARAMETER_STRUCT` 9/9, `BEGIN/END_UNIFORM_BUFFER_STRUCT` 2/2; `#define` and commented examples contribute zero entries;
- all nine global parameter structs present with matching source field counts (including `FLumenCardScene` 19 and `FShadowDepthPassUniformParameters` 13);
- all 82 RDG access declarations and all 33 `RENDER_TARGET_BINDING_SLOTS` declarations present in the index and bound as fields of a parameter struct;
- reviewer-defect checks: radiosity `/WeightSum`, 8-bit 7+1 and 16-bit 5+5+5+1 shadow-mask variants with no overlap, R11-scoped lighting fields plus explicit format variants and `partial` status, CardTiles selector without `passNameRegex`, all `requires` arrays, `FLumenCardScene`/`IndirectArgsBuffer` 12-byte dispatch contract, and GBufferA raw affine decode vs consumer-gated normalize;
- regression checks: screen-probe trace-hit distance 29 bits at bit offset 2, radiance-cache depth 14 bits at bit offset 1, `SHADER_PARAMETER*` count 2859, 225/225 files;
- unique source-anchor and declaration IDs, selector regex compilation, and `coverage.activeFrameComplete=false`;
- representative verified symbols (EncodeNormal, DecodeSurfaceCacheAlbedo, CombineFinalLighting, QuantizeForFloatRenderTarget, Radiosity AddSH/WeightSum, SampleWeight, page-table decode, VSM asfloat, format table anchors, FLumenCardScene fields, etc.);
- extractor determinism: rerunning `extract_declarations.py` reproduces the same SHA-256.
