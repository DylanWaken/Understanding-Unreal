# Direct lighting and reflections: evidence notes

Read-only review for the two dedicated chapters extracted by root. No repository, engine, shader, or prose edits were made. Only this `notes/` directory was written.

## Snapshot

| Item | Value |
|---|---|
| Chapters read | `D:/UnrealUnderstanding/Scripts/Docs/unreal_articles/chapters/lumen-course-system.html` (58,549 bytes, SHA-256 `E8A557F3D968BB6F5B407076F321FF696A44C4290E5167AFE53288F1C3D43FDC`) |
| | `.../lumen-course-reconstruction.html` (45,688 bytes, SHA-256 `D126F8C985DB48B88992BCC145FC3AED4105450A48C874EA46CCA2FC8122C587`) |
| Engine root | `D:/UnrealEngine` |
| Engine HEAD | `71fe36aac5a8df5ccd66c763ffc902b29b6a9c43` (`Engine/Build/Build.version`: 5.8.1, UE5) |
| Source status | Every file cited below is **clean** against that base: `git status --porcelain -- <file>` is empty and `git rev-parse HEAD:<file>` is the blob SHA in `anchors.json`. |
| Caveat | The engine worktree has 191 unrelated staged changes (mostly Flutter/config deletions). None of the cited files is among them. |
| Capture data | `reflection.json` SHA-256 `9C8D95...DF78D0`; `receivers.json` `8AB539...B0CA63`; `run-lumen.json` `D8F25C...061C26` (full values in `anchors.json`). |

The chapter files changed several times during this review. The current snapshot has a dedicated `#direct-lighting` chapter (`lumen-course-system.html:103-168`) and a dedicated `#reflections` chapter (`lumen-course-reconstruction.html:63-131`); the composition chapter follows at `:133-154`. **No structural plan drift remains.** Root also added inline source anchors while the review was running: `system:128` (direct accumulator), `system:134` (VSM mark/allocate/render and projection), `system:160` (sqrt encode/decode), `reconstruction:70` (tile classification), `:93` (ray generation), `:97` (compaction modes), `:102` (resolve + `EnvBRDF` composition), `:103` (temporal conditional compilation), and `:107` (hit-lighting completion). Those anchors match the ranges below. These notes add the scalar facts, captured boundaries, and residual precision checks; they are not a second competing anchor set.

## 1. Direct lighting

### A1. `direct.deferred_material_eval`
`Engine/Shaders/Private/DeferredLightingCommon.ush:316-416` (blob `c196830d416f`)

The legacy Default Lit direct path owns four distinct operations. Keep them separate; the capture uses this path (`r.Substrate=0`, `GBufferA/B/C/D/F`).

- **Light geometry/attenuation.** `GetLocalLightAttenuation` (`:246-286`) computes `ToLight`, normalizes `L`, applies inverse-square or `RadialAttenuation`, then spot and rect backface support. `LightMask` is a scalar.
- **Light color is attenuated, not the BRDF.** `MaskedLightColor *= LightMask` at `:337-338`. `IntegrateBxDF` receives the unattenuated material/light data.
- **Visibility is a packed, per-light term.** `GetShadowTerms` (`:90-149`) consumes `LightAttenuation`: X = whole-scene directional shadow, Y = directional SSS shadow, Z = light function + per-object, W = per-object SSS. Static shadowing is folded in. This is not a scalar visibility buffer.
- **BRDF plus finite-source integral.** `IntegrateBxDF(GBuffer, N, V, Rect/Capsule, Shadow, SourceTexture)` at `:392`/`:402`/`:404` returns `FDirectLighting` (diffuse, specular, transmission). For a rect light it integrates the source; for a capsule it uses the inverse-square form. The specular helper returns `(D * Vis) * F` (`Engine/Shaders/Private/ShadingModels.ush:164-190`), with the `1/(4 NoL NoV)` folded into `Vis`; a second textbook denominator would be a different BRDF.
- **Scales and add.** `LightData.DiffuseScale`/`SpecularScale` are applied after the integral (`:408-410`); the split accumulator multiplies the result by `MaskedLightColor * Shadow.SurfaceShadow` (`:416`).

```text
331  float3 L = LightData.Direction;                    // normalized light direction
337  LightMask = GetLocalLightAttenuation(TranslatedWorldPosition, LightData, ToLight, L);
338  MaskedLightColor *= LightMask;
352  GetShadowTerms(..., LightData, ..., LightAttenuation, Dither, Shadow);
392  Lighting = IntegrateBxDF(GBuffer, N, V, Rect, Shadow, SourceTexture, SVPos);
408  Lighting.Specular *= LightData.SpecularScale;
409  Lighting.Diffuse  *= LightData.DiffuseScale;
410  Lighting.Transmission *= LightData.DiffuseScale;
416  LightAccumulator_AddSplit(LightAccumulator, Lighting.Diffuse, Lighting.Specular,
         Lighting.Diffuse, MaskedLightColor * Shadow.SurfaceShadow, ...);
```

Header note: `AccumulateDynamicLighting` and `GetDynamicLighting` are marked "UE_DEPRECATED 5.7 - Deprecated by Substrate". For the pinned capture's legacy GBuffer route they are still the inspected implementation; do not describe them as the Substrate path.

### A2. `direct.shadowmask_sqrt`
`Engine/Shaders/Private/Common.ush:1194-1218` (blob `17732032502e`); call site `Common.ush:1321-1324`

- `EncodeLightAttenuation` is `sqrt(InColor)` (`:1195-1206`); `DecodeLightAttenuation` is `Square(InColor)` (`:1209-1218`).
- The deferred consumer is `GetPerPixelLightAttenuation` -> `DecodeLightAttenuation(Texture2DSampleLevel(LightAttenuationTexture, ...))` (`:1321-1323`).
- The VSM composite writes the encoded value: `OutShadowMask = ... EncodeLightAttenuation(float4(ShadowFactor.x, ShadowFactor.y, ...))` at `Engine/Shaders/Private/VirtualShadowMaps/VirtualShadowMapProjectionComposite.usf:36-41`.
- The deferred light pass binds the produced mask as `Out.LightAttenuationTexture = ShadowMaskTexture ? ShadowMaskTexture : WhiteDummy` (`Engine/Source/Runtime/Renderer/Private/LightRendering.cpp:2810-2811`).

```text
1195 MaterialFloat EncodeLightAttenuation(MaterialFloat InColor)
1199     return sqrt(InColor);
1209 MaterialFloat DecodeLightAttenuation(MaterialFloat InColor)
1211     return Square(InColor);
1321 float4 GetPerPixelLightAttenuation(float2 UV)
1323     return DecodeLightAttenuation(Texture2DSampleLevel(LightAttenuationTexture, LightAttenuationTextureSampler, UV, 0));
```

Consequence: the chapter's `(c/255)^2` is exact **for this captured directional sun**, whose red channel carries the whole-scene shadow and whose green agrees. Production must still decode the packed X/Y/Z/W channels according to light type; B/A are the clear-white channels here, not additional visibility.

### A3. `vsm.pass_orchestration`
`Engine/Source/Runtime/Renderer/Private/Shadows/ShadowSceneRenderer.cpp:866-905` (blob `1153a341929e`)

`FShadowSceneRenderer` is the orchestration point, not the page algorithm itself:

- `BeginMarkVirtualShadowMapPages` -> `VirtualShadowMapArray.BeginMarkPages` (`:879`).
- `RenderVirtualShadowMaps` -> `BuildPageAllocations` (`:898`), then `RenderVirtualShadowMaps` (`:904`).
- The render body calls Nanite (`:780`) or non-Nanite (`:791`) rasterization, then `PostRender` (`:794`).
- `BuildPageAllocations` requires the mark context: it early-outs when there are no shadow maps (`VirtualShadowMapArray.cpp:3235-3239`) and `ensure(VirtualShadowMapSetupContext)` with the comment "BeginMarkPages was not run, this indicates a systemic error" (`:3240-3244`). Implementation entries are `FVirtualShadowMapArray::BeginMarkPages` (`:2516`), `RenderVirtualShadowMapsNanite` (`:4218`), `RenderVirtualShadowMapsNonNanite` (`:4389`).

```text
866  void FShadowSceneRenderer::BeginMarkVirtualShadowMapPages(...)
879      VirtualShadowMapArray.BeginMarkPages(...);
889  void FShadowSceneRenderer::RenderVirtualShadowMaps(...)
898      VirtualShadowMapArray.BuildPageAllocations(...);
904      RenderVirtualShadowMaps(GraphBuilder, bNaniteEnabled, ...);
780          VirtualShadowMapArray.RenderVirtualShadowMapsNanite(...);
791      VirtualShadowMapArray.RenderVirtualShadowMapsNonNanite(...);
794      VirtualShadowMapArray.PostRender(GraphBuilder);
```

This supports the chapter's order (mark -> allocate -> render -> consume). It does **not** establish per-page residency, timing, or that a page is sampleable after allocation alone; the `PostRender`/page-table handoff is a separate contract.

### A4. `vsm.projection_entry`
`Engine/Source/Runtime/Renderer/Private/VirtualShadowMaps/VirtualShadowMapProjection.cpp:574-651` (blob `bc54c15d5204`)

Two projection entry points share the same body:

- Local light: `RenderVirtualShadowMapProjection(..., const FLightSceneInfo& LightSceneInfo, int32 VirtualShadowMapId, ...)` at `:574-610`; common projection gets `LightSceneInfo.Proxy`, then composite with `bDirectionalLight=false`, `bModulateRGB=false`.
- Directional clipmap: `RenderVirtualShadowMapProjection(..., const TSharedPtr<FVirtualShadowMapClipmap>& Clipmap, bool bModulateRGB, ...)` at `:612-651`; common projection gets `Clipmap->GetLightSceneInfo().Proxy` and `Clipmap->GetVirtualShadowMapId()`, then composite with `bDirectionalLight=true` and `bModulateRGB`.
- `RenderVirtualShadowMapProjectionCommon` chooses one-pass vs per-light: `bOnePassProjection = LightProxy == nullptr` (`:365`); one-pass writes `OutShadowMaskBits` (`:373`), per-light writes `OutShadowFactor` (`:383`). The composite then applies the sqrt encode (A2).

```text
574  void RenderVirtualShadowMapProjection(
581      const FLightSceneInfo& LightSceneInfo,
585      FRDGTextureRef VirtualShadowMaskTexture = CreateShadowMaskTexture(...);
587      RenderVirtualShadowMapProjectionCommon(..., LightSceneInfo.Proxy, VirtualShadowMapId, nullptr, ...);
601      CompositeVirtualShadowMapMask(..., false/*bDirectionalLight*/, false/*bModulateRGB*/, ...);
612  void RenderVirtualShadowMapProjection(
619      const TSharedPtr<FVirtualShadowMapClipmap>& Clipmap,
625      FRDGTextureRef VirtualShadowMaskTexture = CreateShadowMaskTexture(...);
627      RenderVirtualShadowMapProjectionCommon(..., Clipmap->GetLightSceneInfo().Proxy,
         Clipmap->GetVirtualShadowMapId(), ...);
642      CompositeVirtualShadowMapMask(..., true/*bDirectionalLight*/, bModulateRGB, ...);
```

## 2. Reflections

### A5. `reflection.classification_gating`
`Engine/Shaders/Private/StochasticLighting/StochasticLightingTileClassification.usf:572-596` (blob `2bc2dc479863`)

- Classification is per 8x8 Lumen tile (`TILE_SIZE = 8`, `:21`), written to `RWLumenTileBitmask` at `:755`; `LUMEN_TILE_BITMASK_REFLECTIONS = 0x08` (`LumenScreenProbeTileClassication.ush:10`).
- The gate is `NeedRayTracedReflections(Material.Roughness, Material)` (`:590`), which calls `NeedsLumenReflections` -> `LumenCombineReflectionsAlpha` (`LumenReflectionCommon.ush:325-343`; `LumenReflectionsCombine.ush:12-16`):
  `alpha = saturate((MaxRoughnessToTrace - r) * InvRoughnessFadeLength)`.
- `Material = ApplySmoothBias(Material, false)` before the test (`:589`). Default smooth bias is 0; default `LumenMaxRoughnessToTraceReflections = 0.4` (`Engine/Source/Runtime/Engine/Private/Scene.cpp:658`); `r.Lumen.Reflections.RoughnessFadeLength = 0.1` (`LumenReflections.cpp:129`); `r.Lumen.Reflections.MaxRayIntensity = 40` (`LumenReflections.cpp:179-184`).
- `r.Lumen.Reflections.MaxRoughnessToTrace` itself defaults to `-1` (override disabled), but the capture explicitly sets it to `0.4` (`run-lumen.json`).
- The downstream `ReflectionTileClassificationBuildListsCS` scans the bitmask and allocates the indirect tile lists (`LumenReflections.usf:84-105`, `:141-177`).

```text
572  uint TileBitmask = 0;
574  const float DiffuseLerp = RoughReflectionsDiffuseLerp(Material);
589  Material = ApplySmoothBias(Material, false /*bTopLayerRoughness*/);
590  if (NeedRayTracedReflections(Material.Roughness, Material))
591  {
592      TileBitmask |= LUMEN_TILE_BITMASK_REFLECTIONS;
593  }
596  WriteSharedTileBitmask(LinearThreadIndex, TileBitmask, TILE_BITMASK_INDEX_LUMEN);
```

The `0.4`/`0.1` arithmetic in the chapter is correct: 0.2 -> 1, 0.35 -> 0.5, 0.5 -> 0. It is a representation/workload transition, not a material law.

### A6. `reflection.generate_route`
`Engine/Shaders/Private/Lumen/LumenReflections.usf:301-424` (blob `ba3b6fb3a16d`)

What `ReflectionGenerateRaysCS` actually writes:

- Read the material with `MaxRoughnessToTrace` and `ApplySmoothBias` (`:317`); gate again at `:321`.
- Mirror branch: `RayDirection = reflect(CameraVector, Material.WorldNormal)` (`:366-369`).
- Rough branch: `Alpha = Pow2(Roughness).xx`, optional anisotropic alpha, `ImportanceSampleVisibleGGX(E, Alpha, TangentV)`, `RayDirection = reflect(CameraVector, WorldH)`; `ConeAngle = 1/max(GGXSample.w, 0.0001)` (`:377-388`).
- Writes **only** `RWRayBuffer[coord] = float4(RayDirection, PackedConeAngle)` (`:394`) and `RWRayTraceDistance` (`:416`). Invalid receivers are marked by negating the stored depth (`:420-422`).
- `GenerateRandom` applies `E.y *= 1 - GGXSamplingBias` (`:292`); default `GGXSamplingBias = 0.1` (`LumenReflections.cpp:137-143`). `ImportanceSampleVisibleGGX` maps `E.y` into the VNDF `Z` and returns `VisibleGGXPDF_aniso(V,H,Alpha)` (`MonteCarlo.ush:442-471`); the biased sample density is therefore not the returned PDF, and the code does not apply a separate correction for it.
- `RWTraceThroughput` is initialized to 1 by `ReflectionClearTracesCS` (`LumenReflectionTracing.usf:52-54`) and is **not written** by generate. The BRDF/PDF weight is not produced here.
- `GetRayData` reconstructs `PDF = 1/abs(PackedRayData.w)` from the same cone-angle field (`LumenReflectionCommon.ush:135-145`). The cone/PDF round trip is therefore lossy and clamped (PDF floor `1e-4`, cone floor `1e-5`).

```text
317  const FLumenMaterialData Material = ApplySmoothBias(ReadMaterialData(Coord, MaxRoughnessToTrace), true);
321  if (NeedRayTracedReflections(Material.TopLayerRoughness, Material))
362      ConeAngle = 1.0f / max(Sample.PDF, 0.0001f);
384      float4 GGXSample = ImportanceSampleVisibleGGX(E, Alpha, TangentV);
386      RayDirection = reflect(CameraVector, WorldH);
387      ConeAngle = 1.0f / max(GGXSample.w, 0.0001f);
394      RWRayBuffer[ReflectionTracingCoord] = float4(RayDirection, PackedConeAngle);
420      // Store invalid ray in sign bit
421      DownsampledDepth *= -1.0f;
```

Routing after generation:

- Screen first: `ReflectionTraceScreenTexturesCS` traces the HZB and samples **previous** `SceneColor` at the hit, clamps to `MaxRayIntensity`, and writes `TraceHit` with a hit bit (`LumenReflectionTracing.usf:70-229`).
- Compaction is **not** only unresolved rays. `ReflectionCompactTracesCS` has modes (`:289-344`): default/far-field accept `!bHit` (misses); `TRACE_COMPACTION_MODE_HIT_LIGHTING` accepts `bHit && MaterialId.bNeedsHitLightingPass` (hits that still need shading). Indirect args are built at `:412-430`.
- World routing in C++: `Lumen::UseHardwareRayTracedReflections` -> `RenderLumenHardwareRayTracingReflections` (`LumenReflectionTracing.cpp:1258-1275`); otherwise MeshSDF/heightfields (`:1301-1343`) then voxels/global SDF (`:1349-1407`). Shader entry points: `ReflectionTraceMeshSDFsCS` (`LumenReflectionTracing.usf:693-707`) and `ReflectionTraceVoxelsCS` (`:888-902`).
- Hardware has a separate far-field pass over compacted misses (`LumenReflectionHardwareRayTracing.cpp:660-696`). Far field is off in the captured frame.

### A7. `reflection.hit_lighting_env`
`Engine/Shaders/Private/Lumen/LumenReflectionHardwareRayTracing.usf:285-402` (blob `873ab1633330`)

Cache-hit vs material-hit lighting is a **deferred decision**:

- Default/far-field pass traces the Surface Cache (`TraceSurfaceCacheRay` or `TraceSurfaceCacheFarFieldRay`, `:238`/`:242`). After the trace, `bNeedsHitLightingPass = Result.bIsHit && (HitLightingForceEnabled || !Result.bIsRadianceCompleted)` (`:289-292`).
- If `SampleSceneColorAtHit` succeeds, the pass cancels hit lighting (`:294-302`). If a near-field miss has valid radiance-cache coverage, it samples the cache and sets `Result.bIsHit = true; bNeedsHitLightingPass = false` (`:357-366`).
- Environment policy: sky is applied **only on a miss** and only when `ApplySkyLight != 0` (`:370-373`). `GetSkylightLeakingForReflections` is added unconditionally (`:375`), so it is a leak/approximation term even for hits, not physical sky visibility.
- `MaxRayIntensity` clamps the final pre-exposed lighting (`:378-388`). It is a firefly control, not a radiometric cap.
- The hit-lighting pass uses `TraceAndCalculateRayTracedLighting` with `bHitLightingDirectLighting`, `bHitLightingSkylight`, `bUseReflectionCaptures`, force-opaque and shadow modes (`:216-234`). It is a second pass over the compacted hits flagged by `PackTraceMaterialId(bNeedsHitLightingPass, ...)` (`:397-401`).
- Mode selection: `EHitLightingMode::SurfaceCache = 0`, `HitLighting = 1`, `HitLightingForReflections = 2` (`LumenHardwareRayTracingCommon.h:32-39`); `GetHitLightingMode` clamps and returns mode 0 when ray-gen is unavailable or unsupported (`LumenHardwareRayTracingCommon.cpp:241-277`). The captured frame sets `r.Lumen.HardwareRayTracing.LightingMode=0` and `r.Lumen.Reflections.HardwareRayTracing.HitLighting=0`, so C is a **SurfaceCache** result, not a hit-lighting result.

```text
289  if (Result.bIsHit && (HitLightingForceEnabled != 0 || !Result.bIsRadianceCompleted))
291      bNeedsHitLightingPass = true;
299      bNeedsHitLightingPass = false;
357  if (!Result.bIsHit && RayData.bUseRadianceCache)
364      Result.bIsHit = true;
365      bNeedsHitLightingPass = false;
370  if ((ApplySkyLight != 0) && !Result.bIsHit)
372      ApplySkylightToTraceResult(RayData.Direction, TraceResult);
375  TraceResult.Lighting += GetSkylightLeakingForReflections(...);
385  if (MaxLighting > MaxRayIntensity)
387      TraceResult.Lighting *= MaxRayIntensity / MaxLighting;
399  RWTraceMaterialId[...] = PackTraceMaterialId(bNeedsHitLightingPass, Result.MaterialShaderIndex);
```

### A8. `reflection.resolve_spatial_temporal`
Primary: `Engine/Shaders/Private/Lumen/LumenReflectionResolve.usf:57-73` (blob `386aeb8e4867`)

Where the weight is actually applied:

- `GetSampleWeight` returns `D_GGX(a2, H.z) / RayPDF` (`:57-73`). The comment says "BRDF(Ray) / RayPDF", but the code returns the NDF/PDF ratio, not the full `D*Vis*F` BRDF.
- It is applied to the center sample (`:466-476`) and to each neighbor sample (`:542-575`), then normalized by `WeightSum` (`:620-630`).
- The result is written to `RWSpecularIndirect` (radiance) and `RWSpecularIndirectDepth` (`:649-653`). It is view-pre-exposed and still missing the receiver's `EnvBRDF`.
- `EnvBRDF` and the rough/traced mix are applied later: `MixSpecularAndRoughReflections` does `Rough*(1-alpha) + Traced*alpha` (`Engine/Shaders/Private/DiffuseIndirectComposite.usf:227-244`), then `CombineRoughSpecular` multiplies by `EnvBRDF` (`:246-258`).

Spatial filter (`LumenReflectionDenoiserSpatial.usf:92-143`, blob `e09359031ef5`):

- Kernel radius = `SpatialFilterKernelRadius * saturate(roughness * 8)` (`:92`); lobe half-angle = `GetSpecularLobeHalfAngle(roughness)` (`:109`).
- Weights = depth Gaussian on plane distance / relative depth (`:125-129`) * normal weight `1 - angle/LobeHalfAngle` (`:131-133`) * luminance weight (`:138-142`). There is no explicit roughness-delta weight; roughness enters through kernel radius and the normal acceptance cone.

Temporal filter (`LumenReflectionDenoiserTemporal.usf:126-227`, `:405-413`, `:420-439`, `:532-546`, blob `1de80a8a0d34`):

- Surface-history path uses a relative depth threshold `HistoryDistanceThreshold * lerp(0.5,1.5,noise)` (`:178`, `:224-225`) and widens it at grazing angles (`:217-220`). The hit-history plane test exists but is compiled out by `REFLECT_HIT_DEPTH_TEST 0` (`:183`).
- **The normal/roughness lobe test is also compiled out:** `#define LOBE_CONFIDENCE 0` (`:422`), so the `AngleBetweenLobes`/`LobeHalfAngleA/B` block (`:425-439`) is dead in this build. Do not claim an active normal-dot or roughness-delta rejection.
- Active reuse reduction: history validity / `IsLightingValid` (`:243-248`), depth/distance rejection, neighborhood clamp (`:492-493`), hit-vs-surface history choice by neighborhood error (`:495-507`), and confidence floor `Confidence = 0.75*Confidence + 0.25` (`:534`).
- Roughness does still select the max frame count: `MaxFramesAccumulated = lerp(2, MaxFramesAccumulated, saturate(roughness/0.05))` for 1x downsample (`:410-413`), and final alpha is `1/(N + 4 if reconstructed)` (`:543-545`).

```text
57   // Weight reflection rays by BRDF(Ray) / RayPDF
58   float GetSampleWeight(...)
69       SampleNDF = D_GGX(a2, H.z);
72       return SampleNDF / RayPDF;
469  float CenterSampleWeight = GetSampleWeight(CenterRayData.Direction, CenterRayData.PDF, ...);
474  Center.Lighting *= CenterSampleWeight;
475  Center.WeightSum = CenterSampleWeight;
178  float DisocclusionDistanceThreshold = HistoryDistanceThreshold * lerp(0.5f, 1.5f, Noise);
224  const float4 DistanceToHistoryValue = abs(HistorySampleSceneDepth4 - ReprojectedSceneDepth);
225  float4 DepthWeights = select(DistanceToHistoryValue >= ReprojectedSceneDepth * DisocclusionDistanceThreshold, 0.0f, 1.0f);
422  #define LOBE_CONFIDENCE 0
```

(Resolve lines are from `LumenReflectionResolve.usf`; the last four lines are from `LumenReflectionDenoiserTemporal.usf`.)

Output meaning: `RWSpecularIndirect` is a **partially weighted radiance average**, not the final BRDF result. The captured value is therefore not directly comparable to a path-traced final specular RGB; the receiver's `EnvBRDF` and the rough/traced blend are still pending at the composition stage.

## 3. Captured examples vs source-derived operations

| Statement | Kind | Exact evidence |
|---|---|---|
| A is sunlit; mask red byte 255, decoded attenuation 1; B/C red byte 0, attenuation 0 | captured | `receivers.json` -> `receivers.A/B/C.shadow` (`memory_bgra`, `shader_r_code`, `shadow_factor_r_linear`) |
| Sun pass adds `(1.9306641,1.3037109,0.7900391)` at A, `(0,0,0)` at B/C | captured | `receivers.json` -> `receivers.A/B/C.direct` (delta across passes 840/842) |
| Mask is `ShadowMaskTexture` from pass 841, consumed as `PS.LightAttenuationTexture` at 842 | captured | `receivers.json` -> `vsm_shadow_mask`; `receivers.report.md` lines 141, 153, 169, 285 |
| C resolved reflection `(0.0180664,0.0135498,0.0087891)`; final `(0.0166016,0.0144043,0.0085449)`; hit depth 273.75 cm | captured | `reflection.json` -> `/receivers/C` |
| C raw frame byte 96 -> `96/255*32 = 12.0470588` effective frames | captured | `reflection.json` -> `/receivers/C`, `/frame_count_encoding` |
| C trace route: screen traces on, hardware RT on, lighting mode 0 (SurfaceCache), hit lighting 0, far field 0 | captured config | `run-lumen.json` -> `r.Lumen.Reflections.ScreenTraces=1`, `r.Lumen.Reflections.HardwareRayTracing=1`, `r.Lumen.HardwareRayTracing.LightingMode=0`, `r.Lumen.Reflections.HardwareRayTracing.HitLighting=0`, `r.LumenScene.FarField=0` |
| A/B reflection records are invalid; not reflection evidence | captured | `reflection.json` -> `/receivers/A`, `/receivers/B` (`valid_reflection_signal=false`, raw 0) |
| VSM mark -> allocate -> render -> post order | source-derived | `ShadowSceneRenderer.cpp:879,898,904,780,791,794` |
| Projection writes a mask, then composites with sqrt encode | source-derived | `VirtualShadowMapProjection.cpp:574-651`; `VirtualShadowMapProjectionComposite.usf:36-41` |
| Reflection gate: `saturate((0.4-r)/0.1)` | source-derived | `StochasticLightingTileClassification.usf:590`; `LumenReflectionsCombine.ush:12-16`; `Scene.cpp:658`; `LumenReflections.cpp:129` |
| Generate writes direction+cone, not BRDF throughput | source-derived | `LumenReflections.usf:394`; `LumenReflectionTracing.usf:52-54` |
| Compaction modes: misses (default/far-field) vs hits needing hit lighting | source-derived | `LumenReflectionTracing.usf:328-344`; `LumenReflectionTracing.cpp:662-672,702-713` |
| Hit lighting is a second pass; sky only on miss; leak always added | source-derived | `LumenReflectionHardwareRayTracing.usf:289-302,357-375,397-401` |
| Resolve weight is `D_GGX/RayPDF`; EnvBRDF later | source-derived | `LumenReflectionResolve.usf:57-73,469-475`; `DiffuseIndirectComposite.usf:227-258` |
| Temporal normal/roughness lobe test is disabled | source-derived | `LumenReflectionDenoiserTemporal.usf:422` (and dead block `:425-439`) |
| Per-ray SMRT choices, per-pixel resolve weights, unquantized VSM factor | **not captured** | `reflection.json` `/gaps`; `receivers.report.md` line 285; `system.html:162-164` (VSM caveat) |

## 4. Approximation limits (do not overclaim)

1. **The captured reflection is SurfaceCache, not hit lighting.** C is valid evidence for the resolved spatial signal and history count, not for direct/skylight evaluation at the hit. Hit-lighting/environment statements are source-derived from `LumenReflectionHardwareRayTracing.usf`.
2. **The captured sun is one directional light.** The red-channel decode is correct for it; the production `LightAttenuation` is a packed X/Y/Z/W term with light-function/per-object contributions. Do not generalize `(c/255)^2` to all four channels.
3. **The mask is 8-bit sqrt-encoded.** The decoded factor has 1/255 code quantization; the unquantized receiver factor is not recovered. The observed codes `[0,90,127,156,180,202,221,239,255]` are consistent with eighth-step visibility after squaring; this is a record of quantization, not a high-precision visibility measurement.
4. **No per-thread SMRT rays or per-pixel reflection weights were exported.** The dump contains resolved resources and versions, not shader-local operands. A "replay" must label its own selected samples separately.
5. **The reflection weight is not the full BRDF.** Resolve applies `D_GGX/RayPDF`, then composition applies `EnvBRDF`. Calling the reflection buffer "already BRDF-weighted" is too strong.
6. **The PDF is reconstructed from a clamped cone angle.** `ConeAngle = 1/max(PDF,1e-4)`, floor `1e-5`; `GetRayData` recovers `PDF = 1/ConeAngle`. Very smooth surfaces are therefore not exact delta sampling.
7. **`GGXSamplingBias = 0.1` biases the random y coordinate before VNDF sampling.** This is a quality/variance tradeoff; the code does not add a separate correction for it.
8. **Temporal `LOBE_CONFIDENCE` is off.** Normal/roughness mismatch is not an active rejection path in this build. Roughness affects spatial kernel width and history cap; depth/distance and neighborhood consistency do the rejection.
9. **`MaxRayIntensity = 40` clamps pre-exposed lighting after the sky leak.** It removes fireflies at the cost of energy; it is not a physical radiance bound.
10. **VSM entry points are orchestration, not a timing measurement.** Nanite vs non-Nanite, one-pass vs per-light, and async scheduling change the actual GPU work.

## 5. Residual precision checks after the chapter revision

The chapter was revised while this review was running, and the earlier risks were addressed in the text:

- The ray-generation paragraph now asks for "information needed to interpret their sampling density" (`reconstruction.html:68`), and `:93` explicitly says the reference pseudocode's complete BSDF/PDF product is not written here, that throughput is initialized separately, and that `GGXSamplingBias` is a deliberate bias. This resolves the earlier "with their throughput" mismatch.
- `reconstruction.html:97` now explicitly distinguishes miss compaction from hit-lighting compaction. This resolves the earlier "unresolved rays only" mismatch.
- `reconstruction.html:102` anchors both `LumenReflectionResolve.usf:57-73` and `DiffuseIndirectComposite.usf:227-258`, and says the resolve is partially weighted and still needs `EnvBRDF` at composition. This resolves the earlier "includes the specular response" ambiguity.
- `reconstruction.html:103` explicitly states that `LOBE_CONFIDENCE=0` and the hit-history plane test are disabled, and separates the active depth/neighborhood logic from the spatial filter's roughness-dependent weights. This resolves the earlier temporal-rejection mismatch.
- `system.html:128/134/160` now carry the direct accumulator, VSM mark/allocate/render + projection, and sqrt encode/decode anchors. This closes the earlier direct/VSM anchor gap.

Residual checks are consistency nits, not blockers:

| Chapter line | Current wording | Check | Suggested polish |
|---|---|---|---|
| `lumen-course-system.html:215` | "Compaction gives the GPU a dense list of unresolved rays ... avoids running the expensive stage for already-resolved rays" | The dedicated reflections chapter (`:97`) correctly says hit-lighting compaction selects hits that still need a lighting pass. The tracing chapter's generic sentence conflicts with that. | Scope it to "misses selected for world tracing" and cross-reference the reflections chapter's second compaction kind. |
| `lumen-course-reconstruction.html:99` | "disocclusions and mismatched hits must reduce reuse" | The next paragraph (`:103`) correctly disables the normal/roughness lobe test. The generic sentence is acceptable only when read with `:103`; it should not be used alone to claim an active lobe rejection. | Add "see the conditional-compilation note below" or rephrase as "depth, neighborhood, and hit-history mismatch." |
| `lumen-course-reconstruction.html:130` | "The ordinary blend is in `LumenReflectionsCombine.ush`" | That file defines `LumenCombineReflectionsAlpha` and the `Needs...` predicates; the actual `Rough*(1-alpha)+Traced*alpha` and `EnvBRDF` multiply are in `DiffuseIndirectComposite.usf:227-258`, already linked at `:102`. | Repeat the `DiffuseIndirectComposite` link here or say "the blend factor is defined here; the composition multiply is linked above." |
| `lumen-course-reconstruction.html:137` | reflected scene specular "includes the corresponding specular response" | True for the composed signal after `EnvBRDF`; the resolve buffer at `:102` is only partially weighted. | Optional: "after composition's EnvBRDF multiply." |
| `lumen-course-system.html:159` | "Decode its red byte c as (c/255)^2" | Correct for this directional sun; `:160` immediately generalizes to the packed X/Y/Z/W channels. | No change needed; the capture-specific sentence and the production caveat are now adjacent. |

No blocking implementation defect was found in the inspected ranges. The remaining items are wording/anchor consistency, not missing source facts.

## 6. Acceptance evidence

- **Direct-light and reflection stage contracts have exact inspected source anchors:** A1-A4 cover light attenuation/shadow/sqrt decode/BRDF ownership and VSM mark/allocate/render/projection; A5-A8 cover classification/gating, ray generation, routing/compaction, hit lighting/environment, resolve/spatial/temporal weighting and output meaning. Anchors are file+line ranges with blob SHAs in `anchors.json`. The current chapter's inline anchors (`system:128/134/160`; `reconstruction:70/93/97/102/103/107`) match these ranges.
- **Captured examples are distinguished from source-derived operations:** Section 3 labels each statement. Captured values are JSON-pointer references into `reflection.json`, `receivers.json`, and `run-lumen.json`; source-derived operations cite pinned engine ranges. The "not captured" row records the per-ray/per-pixel gap.
