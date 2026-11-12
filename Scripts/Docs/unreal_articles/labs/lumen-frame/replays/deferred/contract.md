# Deferred-replay contract — final capture, graph 0001 / pass 249 / pixel (640,650)

**Contract ID:** `deferred-replay-final-binary-20261112-153914-pass249-pixel640x650`
**Machine-readable contract:** `contract.json`
**Numerical report:** `numerical-report.md`
**Status:** final-capture validated; actual native LTC Mat/Amp and actual `FDeferredLightUniformStruct`; no GPU work.

## 1. Capture and scope

| Field | Value |
|---|---|
| Capture ID | `binary-20261112-153914` |
| Project / engine | UnrealLab / UE 5.8.1 Editor Development D3D12 SM6 |
| GPU | NVIDIA RTX PRO 6000 Blackwell Workstation Edition, driver 610.62 |
| Graph | `0001` |
| Frame scope | `Frame 292 (Delta=0.010717s)` |
| View rect | (0,0,1280,720) inside a 1280x888 buffer |
| Pixel | (640,650) |

In scope: pass 249 `FRenderLightParameters`, `SceneColor` before/after, GBufferA/B/C/D,
SceneDepthZ, `Shadow.Virtual.MaskBits`, actual `PS.View`, actual `PS.DeferredLight`,
actual `PS.VirtualShadowMap`, actual shader-bound GGX LTC Mat/Amp, and one DefaultLit
pixel comparison.

Out of scope: Substrate, MegaLights, hardware ray tracing, translucency/hair/eye/SSS/
clearcoat/cloth, Lumen indirect/reflections, a VSM/SMRT producer rewrite, and all GPU
execution.

## 2. Pass selection and scene graph

| Field | Value |
|---|---|
| Pass id | **249** |
| Pointer | `000100000228e3a88880` |
| Event | `RenderLight Light::StandardDeferred: Ceiling area light` |
| Parameters | `FRenderLightParameters` at `000100000228e3a88380` |
| Parameters metadata | `000100007ffb89248ea0` |
| Output | `000100000228d3652960.mip0` (SceneColor) |
| View metadata / content | `000100007ffb9ac979f0` / `00010000022880566180` |
| View SHA-256 | `0ce9aefbd724dd7861e95e81a389b12d19a5a62ca0ae534b72d06b2b288c6944` |

The same light pass is replayed at ids 561 (graph `0003`) and 869 (graph `0004`); this
contract pins graph `0001` / id 249. The final `scene-graphs.json` primary view for
graph 0001 has the same content pointer and SHA-256 as the pass-bound `PS.View`.

## 3. Exact producer versions

| Resource | Producer id | Version pointer | Producer event | Stored format | SHA-256 (first 16) |
|---|---|---|---|---|---|
| SceneColor.before | 126 | 000100000228dcc2d760 | EditorPrimitives: SDPG_World | PF_FloatRGBA | c40276d3c4b8fda2 |
| SceneColor.after | 249 | 000100000228e3a88880 | RenderLight Light::StandardDeferred: Ceiling area light | PF_FloatRGBA | 1aca84e6ef4616fd |
| SceneDepthZ | 29 | 000100000228d23fc770 | EditorPrimitives | PF_R32_FLOAT | 06688174ae7ec87a |
| GBufferA | 126 | 000100000228dcc2d760 | EditorPrimitives: SDPG_World | PF_A2B10G10R10 | cbfd73f11e271437 |
| GBufferB | 126 | 000100000228dcc2d760 | EditorPrimitives: SDPG_World | PF_B8G8R8A8 | 6ea8806364ea8a1c |
| GBufferC | 126 | 000100000228dcc2d760 | EditorPrimitives: SDPG_World | PF_B8G8R8A8 | 3d34b2c48c508ff1 |
| GBufferD | 126 | 000100000228dcc2d760 | EditorPrimitives: SDPG_World | PF_B8G8R8A8 | 46c5e9fa3a41c9d8 |
| Shadow.Virtual.MaskBits | 248 | 000100000228e3a85210 | VirtualShadowMapProjection(Input:GBuffer) | PF_R32G32_UINT | d8f1c55e2e830d58 |

SceneColor is `PF_FloatRGBA` (FP16), 1280x888. The view is rows 0..719; rows 720..887 are
outside the view rect. `SceneDepthZ` mip0 is `PF_R32_FLOAT` (4 B/pixel); the stencil plane
is a separate `PF_R8_UINT` subresource and is not used by this replay. The paths, version
pointers, and resource IDs above are the final capture's. The SceneColor/GBuffer/depth
byte hashes coincide with the earlier draft capture's values because the frame is
deterministic; they are not inherited from the draft.

## 4. Actual shader operands

| Operand | Evidence |
|---|---|
| `PS.View` | `FViewUniformShaderParameters`, 6752 B, content `00010000022880566180` |
| `PS.DeferredLight` | `FDeferredLightUniformStruct`, 192 B, content `000100000228e3a88830` |
| `PS.SceneTextures` | `FSceneTextureUniformParameters`, 128 B, content `000100000228bbbe92a0` |
| `PS.VirtualShadowMap` | `FVirtualShadowMapUniformParameters`, 368 B, content `000100000228b830ae00` |
| `GGXLTCMatTexture` | actual native binding, `Resources/000100000228d23f80d0.linear.mip0.v000100000225fb9fef70.bin` |
| `GGXLTCAmpTexture` | actual native binding, `Resources/000100000228d23f8ad0.linear.mip0.v000100000225fb9fef70.bin` |

The screen-space `FDeferredLightUniformStruct` is **not reconstructed**: it is the captured
192 B RDG UB used by pass 249. Its values include translated light position
`[0.0, 670.0, 133.0]`, color `[979.4150390625, 979.4150390625, 979.4150390625]`,
`ShadowedBits=3`, `ShadowMapChannelMask=[0.0, 0.0, 0.0, 1.0]`,
and `bAffectsTranslucentLighting=1`.

## 5. Actual LTC textures and sampler

| Item | Mat | Amp |
|---|---|---|
| Native texture | `GGX.LTCMat` | `GGX.LTCAmp` |
| Native pointer | `000100000228ec2da800` | `000100000228ec2dab80` |
| Diagnostic resource | `000100000228d23f80d0` | `000100000228d23f8ad0` |
| Export file | `Resources/000100000228d23f80d0.linear.mip0.v000100000225fb9fef70.bin` | `Resources/000100000228d23f8ad0.linear.mip0.v000100000225fb9fef70.bin` |
| StoredFormat | `PF_FloatRGBA` | `PF_G16R16F` |
| Channels | 4 (half4) | 2 (half2) |
| SHA-256 | `a4b5de62685647a60ef388b9a8084a9de3a8621f75d952c80463bf3aefba7590` | `86a414395cad51e0bc5ff8018254ce3c48ba124089bb0a717cf85c68fa9f5b24` |

Channel order is preserved from the file: `PF_FloatRGBA` half4 -> `M.x,M.y,M.z,M.w`;
`PF_G16R16F` half2 -> `A.x,A.y`. `SubresourceLayouts.StoredFormat` is the decoder selector;
the descriptor's `NativeFormat` is never used to choose a decoder. At this pixel:

- `M = [0.5856864200368056, -0.28026741197201943, 0.5386638240830689, 0.5205275583148652]`
- `A = [0.7437171867090612, 0.02350480188822511]`
- `NoV = 0.338666764361958`, `UV = [0.6949448529411765, 0.8083298516756743]`
- `IrradianceScale = 0.052200322789322`

The sampler pointers for `GGXLTCMatSampler`, `GGXLTCAmpSampler`, and
`SharedBilinearClampedSampler` are all `00010000022615ec2b20`.
Source `SceneManagement.cpp:1277` creates that object as
`TStaticSamplerState<SF_Bilinear, AM_Clamp, AM_Clamp, AM_Clamp>`, and `RectLight.ush:14-16`
uses it via `SUPPORTS_INDEPENDENT_SAMPLERS`. The interpretation is bilinear clamp,
`SampleLevel(...,0)`, 64x64, no mip chain.

## 6. Visibility / VSM boundary

| Field | Value |
|---|---|
| Path | `USE_VIRTUAL_SHADOW_MAP_MASK` |
| VirtualShadowMapId | 8192 (not INDEX_NONE) |
| Producer | pass 248 `000100000228e3a85210` `VirtualShadowMapProjection(Input:GBuffer)` |
| MaskBits dwords at pixel | `0x0000000F`, `0x00000000` |
| Light index / unpacked | 0 / 15 -> 1.0 |
| Fallback taken | False |

**Boundary:** FINAL 4-bit quantized packed Shadow.Virtual.MaskBits, consumed by pass 249. The SMRT producer (pass 248) is NOT replayed; page/light/projection operands are retained only as evidence. No unquantized screen-space shadow factor is claimed. The producer's `ProjectionData`, `PageTable`, `PageFlags`,
`PageReceiverMasks`, `PhysicalPagePool`, `LightGridData`, and `NumCulledLightsGrid` are
retained as operands in `contract.json` but are not sampled. Visibility is exactly 1.0
because `15/15 = 1.0`; no unquantized SMRT shadow factor is claimed.

## 7. Active branches / permutations

| Branch | Active | Evidence |
|---|---|---|
| shading_model | 1 | SHADINGMODELID_DEFAULT_LIT |
| substrate | False | r.Substrate=0; PS.Substrate pointer is null; pass is legacy GBuffer + DefaultLit |
| mega_lights | False | r.MegaLights.EnableForProject=0 |
| source_shape | Rect | Pass event 'RenderLight Light::StandardDeferred: Ceiling area light'; the captured DeferredLight struct carries the rect-light fields RectLightBarnCosAngle/RectLightBarnLength and RectLightAtlasUVScale/Offset; the active specular call is RectGGXApproxLTC, not the capsule/directional path. No synthetic LightType field is asserted beyond the captured pass/struct evidence. |
| source_texture | False | RectLightAtlasUVScale=(0,0), RectLightAtlasMaxLevel=32; no source texture resource in pass inputs |
| ies | False | IESAtlasIndex=-1.0 |
| anisotropy | False | FAnistropicMaterials = bSupportAnisotropyPermutation && !IsRectLight -> false for rect light; GBuffer mask 0xA0 has no 0x10 bit |
| lighting_channels | False | PS.bLightingChannelEnabled=0; LightingChannelsTexture is ZeroUIntDummy |
| vsm_mask | True | VirtualShadowMapId=8192 != INDEX_NONE and ShadowMaskBits bound; bUseVirtualShadowMapMask=1 |
| transmission | False | opaque SceneColor pass; bTransmission=0; no translucent light pass |
| hair_lighting | False | FHairLighting permutation explicitly 0; HairShadowMaskValid=0 |
| hair_complex_transmittance | False | FHairComplexTransmittance false; HairShadowMaskValid=0 |
| light_function_atlas | False | LightFunctionAtlasLightIndex=0; GetLocalLightFunctionCommon returns 1.0 for index 0; no light function material in the captured light |
| cloud_transmittance | False | PS.bCloudShadowEnabled=0 |
| atmosphere_transmittance | False | PS.bAtmosphereTransmittanceEnabled=0 |
| visualize_culling | False | normal deferred light pass writing SceneColor, not the light-culling debug color |
| energy_conservation | 0 | r.Material.EnergyConservation=0 (capture cvar), r.Substrate=0 -> LEGACY_MATERIAL_ENERGYCONSERVATION=0 -> USE_ENERGY_CONSERVATION=0; energy LUT lookup compiled out |

The derived `FDeferredLightPS` permutation domain is: rect source shape, no source
texture, no IES, no anisotropy, no lighting channels, VSM mask on, no transmission, no
hair, no light-function-atlas material, no cloud/atmosphere transmittance, no visualize
culling, energy conservation 0. No compiled PSO/shader hash was captured (gap G3); the
domain is derived from the captured pass parameters and CVars.

## 8. Numerical comparison

Test: `pass if max_channel |L_cpu - (after - before)| <= ULP_fp16(max(|after|,|L_cpu|))`.
`before = [0.0, 0.0, 0.0]` -> the tolerance is **1.0 ULP = 0.0009765625**.

| Ch | before | after (observed FP16) | observed delta | CPU L | residual | residual ULP | predicted FP16 | pred bits | obs bits | code delta |
|---|---|---|---|---|---|---|---|---|---|---|
| R | 0.0000000000 | 1.2392578125 | 1.2392578125 | 1.2399915953 | +0.0007337828 | +0.7514 | 1.2402343750 | 0x3CF6 | 0x3CF5 | +1 |
| G | 0.0000000000 | 1.2392578125 | 1.2392578125 | 1.2399915953 | +0.0007337828 | +0.7514 | 1.2402343750 | 0x3CF6 | 0x3CF5 | +1 |
| B | 0.0000000000 | 1.1767578125 | 1.1767578125 | 1.1766082763 | -0.0001495362 | -0.1531 | 1.1767578125 | 0x3CB5 | 0x3CB5 | +0 |

**Result: PASS** — max |residual| = 0.751394 ULP
<= 1.0 ULP. The predicted FP16 codeword is +1 ULP in R/G and exact in
B; the unrounded residual is +0.7514 ULP (R/G) and -0.1531 ULP (B).
The actual LTC tables are bit-identical to the quantized `LTC.h` (0 mismatched half
codewords in the full 64x64 Mat/Amp tables), so the 1-codeword R/G difference is not an
LTC-content gap (see G1).

## 9. Residual gaps (do not relabel as exact)

- **G1 (residual):** The CPU contribution is 1.239991595 (R/G) and 1.176608276 (B); the observed FP16 delta is 1.2392578125 (R/G) and 1.1767578125 (B). The unrounded residual is +0.7514 ULP (R/G) and -0.1531 ULP (B), within the 1-ULP tolerance. The predicted FP16 codeword is +1 ULP in R/G and exact in B. The LTC sidecar is actual and bit-identical to the quantized LTC.h, so this is not an LTC-content gap. No missing operand has been identified; the remaining sub-ULP difference is in the GPU shader evaluation (candidates include the half-precision BxDF inputs and filter/approximation details), which this float64 CPU replay does not model bit-exactly.
- **G2 (vsm-boundary):** Only the final quantized 4-bit MaskBits is replayed. The SMRT/VSM producer (pass 248) is not replayed; page table, page flags, page receiver masks, physical page pool, projection data, light grid and culled-light grid are retained as operands but not sampled. Visibility=1.0 is exact for the 15/15 codeword; the unquantized shadow factor is not independently proven.
- **G3 (shader-hash):** No compiled PSO/shader hash was captured for pass 249. The active permutation is derived from the captured parameters/CVars and source; a byte-exact compiled shader identity remains open (old M8).
- **G4 (native-sampler-state):** Sampler bindings are pointer-only (StateCaptured=false). The LTC sampler pointer equals View.SharedBilinearClampedSampler and source initializes that object as SF_Bilinear/AM_Clamp x3, so bilinear-clamp is the correct interpretation; the raw sampler object state was not captured.
- **G5 (native-readback-timing):** The LTC native bindings are ReusedScheduledReadback from pass 000100000225fb9fef70 with TimingRisk=OwnerPassNotExecutedAtCPUCollection; the exported file exists, size matches, DumpReadbackStatus=Dumped, and its 64x64 half values are bit-identical to the quantized LTC.h. The content is therefore valid for this static global LUT, but the capture-side timing caveat is retained.
- **G6 (stored-format-provenance):** SubresourceLayouts marks the LTC exports StoredBytesAreNativeStorage=false, NativeValueInterpretation=DecodedFloat, BitExact=false, ValuePreserving=true. The decoder follows StoredFormat (PF_FloatRGBA / PF_G16R16F) and preserves file channel order; it does not assume a half2/half4 decoder from NativeFormat.

## 10. Reproduction and freeze

```text
# install (NumPy only)
python -m pip install -r analysis/deferred-replay/requirements.txt

# replay (portable: paths are relative to --capture)
python analysis/deferred-replay/replay_cpu.py --capture <capture-root> --output analysis/deferred-replay/report.json

# independent verification
python analysis/deferred-replay/verify_replay.py --capture <capture-root> --report analysis/deferred-replay/report.json --output analysis/deferred-replay/verification.json --ltc-header <engine>/Engine/Source/Runtime/Renderer/Private/LTC.h --final-dir <final>
```

Frozen hashes: `report.json` `4dacc34ac987e31ab212108d4e94eacd996a9de66c101a8badce2b3d38674fde`; `verification.json` `abddacbdbc66e0d9d92dc120c3987ae3cd3e1846f48d02dd651fb4515fa00246`.
Verification: 17 checks, PASS.

## 11. Source reference index

| File | Lines | What |
|---|---|---|
| `LightRendering.h` | 23-33 | `FDeferredLightUniformStruct` layout |
| `LightRendering.cpp` | 2773-2830, 3154, 3218-3238 | deferred-light PS parameters, VSM permutation |
| `RectLight.ush` | 14-16, 61-165, 192-252, 262-299, 340-405, 602-690 | rect irradiance, polygon integration, LTC, sampler, no barn door |
| `RectLightIntegrate.ush` | 59-92 | `CreateAreaLight`, `IntegrateBxDF` dispatch |
| `ShadingModels.ush` | 212-293, 1137-1168 | `DefaultLitBxDF`, rect LTC branch |
| `DeferredLightingCommon.ush` | 90-149, 246-286, 316-437 | shadow terms, local attenuation, accumulation |
| `DeferredLightPixelShaders.usf` | 102-116, 144-164, 291-302, 440-455 | pixel main, VSM mask, ScreenUV, pre-exposure |
| `DeferredShadingCommon.ush` | 137-147, 295-309, 1076-1186 | GBuffer decode |
| `LightAccumulator.ush` | 58-83, 98-137 | additive accumulation |
| `ShadingEnergyConservation.ush` | 15-31 | `USE_ENERGY_CONSERVATION` selection |
| `ShaderCompiler.cpp` | 4191-4194 | `LEGACY_MATERIAL_ENERGYCONSERVATION` from `r.Material.EnergyConservation` |
| `SceneManagement.cpp` | 1275-1280 | shared sampler state |
| `VirtualShadowMapMaskBitsCommon.ush` | 16-60 | `GetVirtualShadowMapMaskForLight` / unpack |
| `SceneView.cpp` | 2576-2589, 2613 | `ScreenPositionScaleBias` |
| `LTC.h` | `GGX_LTC_Mat`, `GGX_LTC_Amp` | source LUT tables (actual exports are bit-identical) |
