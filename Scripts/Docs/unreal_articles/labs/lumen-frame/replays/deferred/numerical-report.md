# Numerical report — final deferred rect-light pixel

**Capture:** `binary-20261112-153914`  
**Graph / pass:** `0001` / 249 `000100000228e3a88880` `RenderLight Light::StandardDeferred: Ceiling area light`  
**Pixel:** (640,650)  
**Report SHA-256:** `4dacc34ac987e31ab212108d4e94eacd996a9de66c101a8badce2b3d38674fde`  
**Verification:** `abddacbdbc66e0d9d92dc120c3987ae3cd3e1846f48d02dd651fb4515fa00246` (17 checks, PASS)

## 1. Command actually run

```text
python analysis/deferred-replay/replay_cpu.py --capture <capture-root> --output analysis/deferred-replay/report.json
python analysis/deferred-replay/verify_replay.py --capture <capture-root> --report analysis/deferred-replay/report.json --output analysis/deferred-replay/verification.json --ltc-header <engine>/Engine/Source/Runtime/Renderer/Private/LTC.h --final-dir <final>
```

Result: replay exit 0, `PASS`; verifier exit 0, all 17 checks pass.

## 2. Scene graph and versions

| Resource | Producer | Version | Producer event | StoredFormat | SHA-256 (first 16) |
|---|---|---|---|---|---|
| SceneColor.before | 126 | 000100000228dcc2d760 | EditorPrimitives: SDPG_World | PF_FloatRGBA | c40276d3c4b8fda2 |
| SceneColor.after | 249 | 000100000228e3a88880 | RenderLight Light::StandardDeferred: Ceiling area light | PF_FloatRGBA | 1aca84e6ef4616fd |
| SceneDepthZ | 29 | 000100000228d23fc770 | EditorPrimitives | PF_R32_FLOAT | 06688174ae7ec87a |
| GBufferA | 126 | 000100000228dcc2d760 | EditorPrimitives: SDPG_World | PF_A2B10G10R10 | cbfd73f11e271437 |
| GBufferB | 126 | 000100000228dcc2d760 | EditorPrimitives: SDPG_World | PF_B8G8R8A8 | 6ea8806364ea8a1c |
| GBufferC | 126 | 000100000228dcc2d760 | EditorPrimitives: SDPG_World | PF_B8G8R8A8 | 3d34b2c48c508ff1 |
| GBufferD | 126 | 000100000228dcc2d760 | EditorPrimitives: SDPG_World | PF_B8G8R8A8 | 46c5e9fa3a41c9d8 |
| Shadow.Virtual.MaskBits | 248 | 000100000228e3a85210 | VirtualShadowMapProjection(Input:GBuffer) | PF_R32G32_UINT | d8f1c55e2e830d58 |

`SceneColor.before` is the last mip0 writer before pass 249 (pass 126); pass 249 writes the
same resource in place. `SceneDepthZ` mip0 is last written by pass 29; later passes only
write the separate stencil subresource. `MaskBits` is produced by pass 248.

## 3. Pixel decode

| Quantity | Value |
|---|---|
| UV | [0.500390625, 0.732545045045045] |
| NDC | [0.0007812499999999556, -0.8069444588047487] |
| DeviceZ | 0.021478749811649 (`0x3CAFF434`) |
| SceneDepth | 465.576438955454 cm |
| Translated world position | [-0.25473594843049613, 459.8249760783056, -164.99523532450635] |
| World position | [-0.25473594843049613, 59.82497607830561, 0.004764675493646564] |
| Normal | [-0.000977516172494306, -0.000977516172494306, 0.999999044461676] |
| BaseColor (linear) | [0.7230551289219693, 0.7230551289219693, 0.6795424696330938] |
| Metallic / Specular / Roughness | 0.0 / 0.4980392156862745 / 0.6980392156862745 |
| GBufferA word | `0x7FF7FDFF` |
| GBufferB word | `0xA1007FB2` |
| GBufferC word | `0xFFDDDDD7` |
| GBufferD word | `0x00000000` |
| ShadingModelID / Mask | 1 / `0xA0` |
| F0 | [0.03984313725490196, 0.03984313725490196, 0.03984313725490196] |
| DiffuseColor (rho) | [0.7230551289219693, 0.7230551289219693, 0.6795424696330938] |

## 4. Actual light and LTC operands

| Light field | Value |
|---|---|
| TranslatedWorldPosition | [0.0, 670.0, 133.0] |
| Color | [979.4150390625, 979.4150390625, 979.4150390625] |
| InvRadius | 0.0010000000474974513 |
| Direction | [2.220446049250313e-16, 0.0, 1.0] |
| Tangent | [1.0, 0.0, -2.220446049250313e-16] |
| SourceRadius / SourceLength | 65.0 / 60.0 |
| RectLightBarnCosAngle / Length | 0.03489949554204941 / 20.0 |
| ShadowedBits | 3 |
| ShadowMapChannelMask | [0.0, 0.0, 0.0, 1.0] |
| SpecularScale / DiffuseScale | 1.0 / 1.0 |
| LightMask | 0.964948080017135 |
| Falloff / NoL | 0.093696366520267 / 0.826867156667550 |

| LTC | Value |
|---|---|
| Source | `actual-native-binding` (fallback used: False) |
| Mat file | `Resources/000100000228d23f80d0.linear.mip0.v000100000225fb9fef70.bin` (`PF_FloatRGBA`, 4 channels) |
| Amp file | `Resources/000100000228d23f8ad0.linear.mip0.v000100000225fb9fef70.bin` (`PF_G16R16F`, 2 channels) |
| Sampled M | [0.5856864200368056, -0.28026741197201943, 0.5386638240830689, 0.5205275583148652] |
| Sampled A | [0.7437171867090612, 0.02350480188822511] |
| IrradianceScale | [0.05220032278932195, 0.05220032278932195, 0.05220032278932195] |
| UV | [0.6949448529411765, 0.8083298516756743] |
| Sampler | bilinear clamp, SampleLevel(...,0), 64x64, no mip chain |

## 5. Visibility

| Field | Value |
|---|---|
| MaskBits dword0 / dword1 | `0x0000000F` / `0x00000000` |
| Unpacked / visibility | 15 / 1.0 |
| VSM id / fallback | 8192 / False |
| Producer | pass 248 `VirtualShadowMapProjection(Input:GBuffer)` |

FINAL 4-bit quantized packed Shadow.Virtual.MaskBits, consumed by pass 249. The SMRT producer (pass 248) is NOT replayed; page/light/projection operands are retained only as evidence. No unquantized screen-space shadow factor is claimed.

## 6. Contribution and FP16 comparison

Component breakdown:

| Component | R/G/B |
|---|---|
| Diffuse (unexposed) | [0.01783117777828439, 0.01783117777828439, 0.016758117188087555] |
| LTC specular (unexposed) | [0.003161508203498746, 0.003161508203498746, 0.003161508203498746] |
| LightMask | 0.964948080017135 |
| SurfaceShadow | 1.000000000000000 |
| Visibility | 1.000000000000000 |
| PreExposure | 0.0625 |
| Masked light color | [945.0846614832664, 945.0846614832664, 945.0846614832664] |
| CPU contribution | [1.2399915952948766, 1.2399915952948766, 1.1766082762550512] |

Comparison (observed FP16 output, not a float reconstruction):

| Ch | before | after | observed delta | CPU L | residual | residual ULP | predicted FP16 | pred bits | obs bits | code delta |
|---|---|---|---|---|---|---|---|---|---|---|
| R | 0.0000000000 | 1.2392578125 | 1.2392578125 | 1.2399915953 | +0.0007337828 | +0.7514 | 1.2402343750 | 0x3CF6 | 0x3CF5 | +1 |
| G | 0.0000000000 | 1.2392578125 | 1.2392578125 | 1.2399915953 | +0.0007337828 | +0.7514 | 1.2402343750 | 0x3CF6 | 0x3CF5 | +1 |
| B | 0.0000000000 | 1.1767578125 | 1.1767578125 | 1.1766082763 | -0.0001495362 | -0.1531 | 1.1767578125 | 0x3CB5 | 0x3CB5 | +0 |

**Tolerance test:** max |residual| = **0.751394 ULP**;
tolerance = **1.0 ULP**; result = **PASS**.
The predicted FP16 codeword is +1 ULP in R/G and exact in B. The actual LTC values are
bit-identical to the quantized `LTC.h`; see gap G1 for the remaining sub-ULP difference.

## 7. Active permutations

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

## 8. Residual gaps

- **G1 (residual):** The CPU contribution is 1.239991595 (R/G) and 1.176608276 (B); the observed FP16 delta is 1.2392578125 (R/G) and 1.1767578125 (B). The unrounded residual is +0.7514 ULP (R/G) and -0.1531 ULP (B), within the 1-ULP tolerance. The predicted FP16 codeword is +1 ULP in R/G and exact in B. The LTC sidecar is actual and bit-identical to the quantized LTC.h, so this is not an LTC-content gap. No missing operand has been identified; the remaining sub-ULP difference is in the GPU shader evaluation (candidates include the half-precision BxDF inputs and filter/approximation details), which this float64 CPU replay does not model bit-exactly.
- **G2 (vsm-boundary):** Only the final quantized 4-bit MaskBits is replayed. The SMRT/VSM producer (pass 248) is not replayed; page table, page flags, page receiver masks, physical page pool, projection data, light grid and culled-light grid are retained as operands but not sampled. Visibility=1.0 is exact for the 15/15 codeword; the unquantized shadow factor is not independently proven.
- **G3 (shader-hash):** No compiled PSO/shader hash was captured for pass 249. The active permutation is derived from the captured parameters/CVars and source; a byte-exact compiled shader identity remains open (old M8).
- **G4 (native-sampler-state):** Sampler bindings are pointer-only (StateCaptured=false). The LTC sampler pointer equals View.SharedBilinearClampedSampler and source initializes that object as SF_Bilinear/AM_Clamp x3, so bilinear-clamp is the correct interpretation; the raw sampler object state was not captured.
- **G5 (native-readback-timing):** The LTC native bindings are ReusedScheduledReadback from pass 000100000225fb9fef70 with TimingRisk=OwnerPassNotExecutedAtCPUCollection; the exported file exists, size matches, DumpReadbackStatus=Dumped, and its 64x64 half values are bit-identical to the quantized LTC.h. The content is therefore valid for this static global LUT, but the capture-side timing caveat is retained.
- **G6 (stored-format-provenance):** SubresourceLayouts marks the LTC exports StoredBytesAreNativeStorage=false, NativeValueInterpretation=DecodedFloat, BitExact=false, ValuePreserving=true. The decoder follows StoredFormat (PF_FloatRGBA / PF_G16R16F) and preserves file channel order; it does not assume a half2/half4 decoder from NativeFormat.

## 9. Evidence files

| File (relative to capture root) | Bytes | SHA-256 |
|---|---|---|
| `Structures/000100000228e3a88380.bin` | 912 | `3f55d882eba6520849aa8485db064b6bbdecf005db3b8dc2b67713064280e58c` |
| `StructuresMetadata/000100007ffb89248ea0.json` | 2003 | `d53a4208f96e81f5d5f9ac2dbbd675d9d5e058075d7d48d329638b21fc9b0197` |
| `Structures/00010000022880566180.bin` | 6752 | `0ce9aefbd724dd7861e95e81a389b12d19a5a62ca0ae534b72d06b2b288c6944` |
| `StructuresMetadata/000100007ffb9ac979f0.json` | 102382 | `a8967122baefb6a1ab9778fd87a65d4bda8b34f3ca2ab7de5cd1c2006ac5b100` |
| `Structures/000100000228e3a88830.bin` | 192 | `9378465b8ee730c068912c300545f01a976fc7d95795800985fb70e7a9e02019` |
| `StructuresMetadata/000100007ffb89247ae0.json` | 2673 | `8c42e611453c4db19f9adfbda9f5a058595c15093c4710a21526366c43867b7c` |
| `Structures/000100000228bbbe92a0.bin` | 128 | `607296a9117d0b0fe4c1aa1b1eb55a900881f027085a12e09392953105fbb7c5` |
| `StructuresMetadata/000100007ffb9ac975f0.json` | 4397 | `5286c0f6ee629c86f740302ab1050ddc582464391aacbf64359b1eef5b97b6d1` |
| `Structures/000100000228b830ae00.bin` | 368 | `585f524dd3ea820ba7bf9a5d041348248442b6d4d0864445e258058fc19b3d9a` |
| `StructuresMetadata/000100007ffb893329a0.json` | 13902 | `0da0eb520d3649babba0202125deaf0c7b537266c3bc4508802173f414f39182` |
| `Resources/000100000228d3652960.mip0.v000100000228dcc2d760.bin` | 9093120 | `c40276d3c4b8fda259c126bc1200aec43ad0d5bb4460b6796e4006f9c6780ea9` |
| `Resources/000100000228d3652960.mip0.v000100000228e3a88880.bin` | 9093120 | `1aca84e6ef4616fd9693c9559198efd47b20a9c2b38f2f650dc5aac56f0b90e2` |
| `Resources/000100000228d36523c0.mip0.v000100000228d23fc770.bin` | 4546560 | `06688174ae7ec87a2857123c92aa6f9f016be2a57348f48b80d8e64be06bad77` |
| `Resources/000100000228d3653720.mip0.v000100000228dcc2d760.bin` | 4546560 | `cbfd73f11e271437cc61dc6a7bec9fee37b192106e7e3945e3554fb8ed780c44` |
| `Resources/000100000228d36539e0.mip0.v000100000228dcc2d760.bin` | 4546560 | `6ea8806364ea8a1cfe2ceb037572b8eeb0115457516988ccd880097851e2b226` |
| `Resources/000100000228d3653ca0.mip0.v000100000228dcc2d760.bin` | 4546560 | `3d34b2c48c508ff126f7beed0e68353bac0f2980abf8ca58753aeb3258220a66` |
| `Resources/000100000228d3653f60.mip0.v000100000228dcc2d760.bin` | 4546560 | `46c5e9fa3a41c9d86ff844c63383eae7b303e9315e64c19f4f780c6c4cb6b6df` |
| `Resources/000100000228e3a849d0.mip0.v000100000228e3a85210.bin` | 9093120 | `d8f1c55e2e830d58f042ac245c96094570ce236f866796275e33d3d0e28ef15c` |
| `Resources/000100000228d23f80d0.linear.mip0.v000100000225fb9fef70.bin` | 32768 | `a4b5de62685647a60ef388b9a8084a9de3a8621f75d952c80463bf3aefba7590` |
| `Resources/000100000228d23f8ad0.linear.mip0.v000100000225fb9fef70.bin` | 16384 | `86a414395cad51e0bc5ff8018254ce3c48ba124089bb0a717cf85c68fa9f5b24` |
| `Base/Passes.json` | 1595138 | `a7740c0a54294a27e28d15c7d9b0b9842402955c3639ec99d0756cda2f915585` |
| `Base/ResourceDescs.json` | 499915 | `9e94fb4ed98e321a01f7401182188fcf312a183511c6b0af5e32b737b593b792` |
| `Base/SubresourceLayouts.json` | 2023203 | `6b4110050c2b9af9b2f0792c0f05c932ba8c4495b7d001791fd24a791675853d` |
| `Base/ReferencedStructs.json` | 766482 | `48e65cd47d400c9afe2772dbaf521505fb969f1bbe7ac2bcd406d15251dd78d0` |
| `Base/NativeUniformBufferBindings.json` | 39687945 | `8701cfd579775e312942eba2ec003c43732ea5fea62e22e498c89ba43b55c030` |
| `Base/DumpReadbackStatus.json` | 1293442 | `b9b55eec55f553b43c235ec3dadb170fee80a8b6b2cd4b9831631f972169bc58` |
| `Base/ConsoleVariables.csv` | 536873 | `1bb9e59367c5356d6755cfe8efc4905c88ebb0ab3c32210ed5e49e12f5914126` |
| `Base/Infos.json` | 987 | `d60dfcdddc9cbd4e492e8f29b38aff24d88adf3e0631339485692968df1abe07` |
