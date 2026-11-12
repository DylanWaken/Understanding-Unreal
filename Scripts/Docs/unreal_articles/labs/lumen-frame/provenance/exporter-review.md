# Review — final Unreal diagnostic capture provenance

Capture audited: `D:/UnrealEngine/UnrealUnderstandingCapture/capture/buffers/gpudump/binary-20261112-153914`
(UE 5.8.1, D3D12, RTX PRO 6000, `r.DumpGPU.Stream=0`, `Texture=2`, `Buffer=2`,
`NativeUniformBufferTextures=1`, `Buffer.SkipTracking=1`; `Base/Status.txt` = `ok`).
Source audited: `Engine/Source/Runtime/RenderCore/Private/DumpGPU.cpp`,
`Engine/Shaders/Private/Tools/DumpTexture.usf`,
`Engine/Shaders/Private/Tools/DumpBuffer.usf`, plus the owners named below.
No GPU/build/editor/source/repo edits were made. This review only reads and writes `review/`.

## Verdict

The native-readback evidence is internally consistent and correctly bounded. There are no
blocking defects in the assigned scope, but two limits must be stated rather than smoothed
over: the GDF replay is **conditional**, and `DumpReadbackStatus.json` is **not a complete
inventory** of all resource binaries.

## 1. Registry lifetime and traversal phase

- `AddPassInternal()` inserts the owner pass (`Passes.Insert(Pass)`) **before**
  `SetupParameterPass()` (`RenderGraphBuilder.inl:260-269`). `SetupAuxiliaryPasses()` then
  calls `DumpResourcePassOutputs()` at `RenderGraphBuilder.cpp:2629-2630`, outside the
  `IsImmediateMode()` execution block at `2568-2625`. In this non-immediate capture the
  native collection therefore happens **during graph build**, after the owner is in the RDG
  queue but before GPU execution. The readback passes added during that traversal are
  consequently queued after the owner.
- All **17,622** scheduled native rows report
  `SourceStateProvenAtScheduling=false` and
  `OwnerPassExecutionDependencyClaimed=false`, with
  `TimingRisk=OwnerPassNotExecutedAtCPUCollection;NativeResourceHasNoRDGExecutionDependency`
  (`DumpGPU.cpp:3298-3308`, `3246-3249`, `3082-3086`). That is the honest description of the
  graph-build collection phase. The queue placement is not an execution dependency on the
  owner.
- Diagnostic wrapper lifetime: `GetOrRegisterDiagnosticNativeTexture()` wraps the RHI
  texture with `CreateRenderTarget()` and `RegisterExternalTexture(..., SkipTracking)`
  (`DumpGPU.cpp:2389-2402`). `RegisterExternalTexture()` stores the pooled-RT reference on
  the graph-owned `FRDGTexture` via `SetExternalPooledRenderTargetRHI()`
  (`RenderGraphBuilder.cpp:1113`; `FRDGTexture::Allocation` at
  `RenderGraphResources.h:663-664`). `DumpResourcePassOutputs()` removes only RHI lookup
  entries whose map value is the exact diagnostic wrapper, then clears the pending list
  (`DumpGPU.cpp:5172-5183`). Removal from `ExternalTextures` therefore does not free the
  graph-owned wrapper or the native texture, and a later renderer registration cannot be
  removed by a stale entry (the list is emptied per traversal and a wrapper swapped to a
  renderer registration is not re-added). The per-graph memo is reset in
  `DumpNewGraphBuilder()` (`DumpGPU.cpp:4934-4935`).
- Caveat: `RegistryMutation` is hard-coded `false`
  (`DumpGPU.cpp:3178/3251/3272`). It is not evidence that the registry was untouched; the
  registry is in fact mutated by diagnostic insertion/removal. `FileVerified=false` at
  schedule time is correct: only the completion ledger may claim a file.

## 2. Exact view range / format / SRGB / cube mapping

Source path is coherent: for `UBMT_SRV` the code reads the actual
`FRHIViewDesc::FTextureSRV::FViewInfo` (`DumpGPU.cpp:2951`), carries `MipRange`,
`ArrayRange`, `Format`, `bSRGB`, and `Dimension` into `FRDGTextureSRVDesc`
(`2522-2537`), and maps cube array ranges in cube units to face slices (`*6`,
`2496-2519`). This matches the RHI semantics used by D3D12
(`D3D12SRV.cpp:198-233`: cube-array `First2DArrayFace = ArrayRange.First * 6`,
`NumCubes = ArrayRange.Num`) and the narrowing rule that turns a one-slice 2D view of an
array/cube into a 2D-array view (`RHICore.cpp:50-61`). Non-array and 3D views are given
`FirstArraySlice=0, NumArraySlices=0` (`2527-2533`).

Native buffer views use `DumpBuffer.usf`: four source bytes per `uint` texel, explicit
`ElementIndex < NumElements`, and `SourceByteOffset` to skip the 16-byte-aligned SRV
prefix. `DumpRawBufferViewReadback()` bounds-checks
`AlignedOffset + RawByteSize <= Buffer->Desc.Size` and writes exactly `CopyByteSize` bytes
(`DumpGPU.cpp:3442-3567`); no format conversion or zero fabrication is applied.

Capture coverage is narrower than the code:
- **14,223** native texture export rows, **72** distinct native textures/files; every row is
  `WholeNativeResource` (`mip 0/1`, `array 0/1`, `face 0/1`), `ViewFormat == NativeFormat`,
  and only 309 rows are `ExportSRGB=true` (`PreintegratedSkinBRDF`). There are **zero**
  narrowed native SRV views, zero cube views, and zero format reinterpretations in this
  capture. The exact-view path is therefore source-checked but not exercised by this data.
- The four GDF bindings at `graph0001/pass167` are whole `Texture3D` views with
  `PF_R8, PF_R8, PF_R32_UINT, PF_R8`, one mip, SRGB off, and `ExportRisk=BindIsWholeTexture`.
  Their first export is `graph0001/pass28` `DepthPassParallel`; pass167 reuses those files
  (`ScheduledByEarlierPass`, `ReusedScheduledReadback=true`, same `DiagnosticResource` and
  file). No duplicate wrapper for those native pointers appears within the graph.

## 3. Stable versus mutable native inputs; GDF

Source-proven stable (read-only LUTs):
- BlueNoise: `FastBlueNoise_scalar_128x128x64` / `FastBlueNoise_vec2_128x128x64`, loaded
  once as engine textures (`UnrealEngine.cpp:3849-3867`).
- LTC: `GGX.LTCMat`, `GGX.LTCAmp`, `Sheen.LTC`, created once from static tables
  (`SystemTextures.cpp:982-992`).
These are safe to describe as source-proven stable for any consumer in the captured graphs.

Mutable native inputs:
- GDF atlases are persistent `GlobalDistanceFieldData` resources created at
  `GlobalDistanceField.cpp:922-943`, registered per graph at `2049-2095`, and updated by
  `UpdateGlobalDistanceFieldVolume()`/`UpdateGlobalDistanceFieldCache()` (`3186+`), with an
  ephemeral `PageStatsBuffer` cleared at `3213-3214`.
- Native buffers (e.g. `WhiteVertexBuffer`, `VTFeedbackBuffer`) are RHI-owned/pooled and
  are not RDG resources.

`graph0001` GDF evidence:
- First export: pass 28 `DepthPassParallel` for all four GDF textures.
- Consumer: pass 167 `TraceGlobalSDFAndApplyRadianceCache`, four
  `TracingParameters.View.GlobalDistanceField*` bindings, all `ScheduledByEarlierPass`.
- No pass writes the exact tracked wrapper IDs `000100000228d576b738/ba00/bdf8/c0c0`.
  The only GDF write in graph1 is pass 8
  `ClearBuffer(GlobalDistanceField.PageStatsBuffer Size=256bytes)`, a different resource.
  The only passes under the parent scope `"GlobalDistanceFieldUpdate"` are the PageStats
  clears at passes 8/320/628; no atlas producer pass is present in the capture.
- The same native GDF pointers appear in graph1/3/4 (`…228ec2dbd00`, etc.; `PtrToString()`
  prefixes only the graph index), confirming persistent ownership rather than a fresh
  graph resource.

Conclusion for the selected GDF replay: **conditional, not proven**. Within graph1 the
tracked RDG writer set between pass28 and pass167 is empty (the only GDF update-scope work
is the PageStats clear), so the bytes cannot change through an RDG pass in this graph. But
the capture samples at pass28, not at pass167; no pass in the capture produces the atlas
contents, so the persistent owner's last producer/version is outside the captured data;
`SourceStateProvenAtScheduling=false` means there is no execution dependency on the owner;
and untracked native writes are not observable. The replay must say "same bytes if no
untracked write between pass28 and pass167" rather than "exact pass167 consumer-time
readback".

## 4. VT feedback: skipped UAVs versus the final SRV sample

- **309** skipped native UAV rows, **103 per graph**, all keyed `…VTFeedbackBuffer`
  (`View.View.VTFeedbackBuffer`, `PS.View…`, `TracingParameters…`, etc.), all
  `UBMT_UAV` / `SkippedNativeUAVBindingStateNotObservable`. They are writable UAV bindings
  and must not be read as inputs; the skip is correct.
- The real final feedback sample is separate and exists: RDG
  `VirtualTexture_FeedbackBuffer` (`000100000228e994e5a0` in graph1), file
  `Resources/000100000228e994e5a0.v00000000000000000000.bin`, 35,840 bytes,
  `Kind=SkipTrackingBuffer`, `Status=Dumped`.
- Source: `VirtualTextureFeedbackResource.cpp:97-100` allocates the pooled buffer and UAV;
  `112-141` transitions `UAVMask -> SRVCompute`, registers the buffer with
  `ERDGBufferFlags::SkipTracking`, creates `FeedbackBufferSRV`, and passes 294 `Hash table
  indirect arguments`, 295 `Build feedback hash table`, 296 `Compact feedback hash table`
  read it as an SRV; pass 297 `VirtualTextureFeedbackCopy` reads the compacted result.
  The capture confirms these pass inputs and the 35,840-byte sample.
- Size caveat: the UB binding reports a 65,536-byte native allocation while the RDG
  readback is the 35,840-byte logical feedback range. The sample is the compaction SRV
  range, not a whole-allocation dump; do not label the 65,536-byte UAV allocation as dumped.

## 5. Null uniforms

`ReferencedStructs.json` contains **377** `"Null": true` entries across **29** paths and
**278** passes: **312** `RDGUniformBufferUnbound` (`UBMT_RDG_UNIFORM_BUFFER`) and **65**
`RHIUniformBufferUnbound` (`UBMT_REFERENCED_STRUCT`). These are explicit unbound slots;
`DumpReferencedStructs()` records them without inventing a payload
(`DumpGPU.cpp:4695-4708` and the RHI branch at `4840-4845`). They are not missing coverage.
`ParameterBindings.json` separately has 4,104 null pointer bindings; those are also explicit
nulls, not omissions.

## 6. Completion ledger and residual omissions

- **4,919** completion rows, **4,610** distinct filenames. All rows are `Status=Dumped`,
  `ExpectedBytes == ActualBytes`, `FileExists=true`, `FileSizeMatches=true`; 0 missing files
  and 0 size mismatches. This verifies all **1,496** texture subresources: the 1,496
  `SubresourceLayouts.json` rows all have matching completion records and matching
  `ByteSize`.
- **309** extra rows are duplicate completions for 309 one-per-pass 16-byte
  `NativeBuffer.*` files (the `WhiteVertexBuffer` view reached through two UB members,
  `View.View.LandscapeIndirection` and `View.View.LandscapePerComponentData`). Both
  readbacks write the same bytes to the same file; this is 309 redundant tiny readbacks,
  not a status mismatch.
- **1,183 distinct regular RDG buffer files** (653,187,484 bytes) have **no**
  `DumpReadbackStatus` row at all. This explains the log: 6,102 resource-binary writes =
  4,919 recorded readbacks + 1,183 unrecorded regular RDG buffer dumps; disk has 5,793
  distinct files because 309 of the 6,102 writes overwrite the same 16-byte file. The
  regular `AddDumpBufferPass()` non-stream path writes and closes the file
  (`DumpGPU.cpp:3822-3868`) without calling `RecordReadbackCompletion()`, unlike the native
  buffer/SkipTracking/texture paths (`3349`, `3767`, `2064`, `2109`). The ledger is complete
  for native readbacks and textures, but it is not a complete resource inventory. Any claim
  of "all dumped files verified" must exclude these 1,183 buffers.

## 7. Unresolved states / strict unsupported scopes

- GDF same-version at pass167: conditional (see §3); no direct pass167 sample.
- Native cube/cube-array and narrowed SRV views: no capture sample. `TextureCubeArray` is
  explicitly skipped (`NativeCubeArrayViewNotExpanded`); multi-mip stencil views are skipped
  (`NativeStencilViewMultiMipNotSupported`); the source path is unexercised here.
- All 309 native UAV bindings are unread by design; the final VT feedback value is available
  only through the separate RDG SRV sample.
- `RegistryMutation=false` is constant and must not be used as evidence.
- 1,183 regular RDG buffer binaries lack completion records (see §6).
- `SourceStateProvenAtScheduling=false` for all scheduled native rows; no GPU execution or
  source-state proof was performed in this step (no hardware lease).

## Acceptance checks

| Check | Result | Evidence |
| --- | --- | --- |
| Registry lifetime and exact views checked against final source | **yes** | `DumpGPU.cpp:2389-2402,5172-5183,4934-4935`; `RenderGraphBuilder.inl:260-269`; `RenderGraphBuilder.cpp:2629-2630,1113`; view mapping at `DumpGPU.cpp:2951,2496-2537`; 14,223 whole-view export rows / 72 files; 0 cube or narrowed rows in capture |
| Stable and mutable native timing boundaries identified | **yes** | BlueNoise/LTC source and capture names; GDF persistent owner `GlobalDistanceField.cpp:922-943,2049-2095,3186-3214`; 17,622 scheduled rows all `SourceStateProvenAtScheduling=false` / `OwnerPassExecutionDependencyClaimed=false`; GDF replay labeled conditional |
| Actual completion and residual omissions verified | **yes** | 4,919 rows / 4,610 distinct; all `Dumped`; 0 missing/size mismatch; 1,496/1,496 texture layouts verified; 309 duplicate 16-byte alias rows; 1,183 unrecorded regular RDG buffer files (653,187,484 B) identified; 377 explicit null entries; 309 VT UAV skips tied to the 35,840-byte RDG SRV sample |

Verdict: **pass within the assigned scope**, with the GDF replay conditional and the
completion-ledger omission documented rather than claimed away.
