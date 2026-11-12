# Captured Lumen frame and explorer

Start with [reconstruction and the four numerical replays](REPRODUCTION.md). The [capture recipe](capture/) records how to produce a new instrumented frame using the portable room project.

From the repository root, serve `Docs` with `python -m http.server 8768 --directory Docs` and open `http://localhost:8768/Unreal/frame-explorer.html`. The public package is already built. To verify or reconstruct it:

```powershell
python Scripts/Docs/unreal_articles/labs/lumen-frame/tools/package_frame.py verify --package Docs/Unreal/assets/lumen-frame
python Scripts/Docs/unreal_articles/labs/lumen-frame/tools/package_frame.py reconstruct --package Docs/Unreal/assets/lumen-frame --dest Captures/binary-20261112-153914
```

The following implementation record describes the packer and its intermediate archive. The checked-in `publication/archive-manifest.json` verifies every extracted package file; the ZIP itself is not needed.

## Explorer implementation

A portable static explorer and lossless packer for the instrumented resources of
one captured Unreal render frame. The explorer is plain HTML, CSS and
JavaScript: no CDN, no framework, no build step and no server-side code.
`tools/package_frame.py` uses only the Python standard library.

## Final evidence state

The final package was built from the instrumented capture
`binary-20261112-153914` (DumpGPU status `ok`; 7,912 files, 18,570,711,246
bytes) and the reviewed `source-schema.json`. It is a real native capture, not a
placeholder or a synthetic fixture.

The package accounts for every input file:

- **7,024 `.bin` payloads** (18,502,060,278 bytes): 5,793 under `Resources/`
  and 1,231 under `Structures/`.
- **888 non-`.bin` native files**, copied byte-for-byte under `native/`
  (`Base/*`, `StructuresMetadata/*.json`, screenshot, root HTML/help files).
- **1,449 resources** (575 textures, 874 buffers) and **928 passes**.
- **1,496 exact texture readback subresources**: 1,133 Texture2D, 210
  Texture2DArray, 135 Texture3D and 18 TextureCube faces (3 cubes × 6 faces).
- **1,496 scheduled readbacks joined to completion records**: every one says
  `Dumped` with `GPUReadbackCompleted`. Scheduling alone is never treated as
  success.
- **1,183 RDG buffer version payloads with no completion record**. Their bytes
  are preserved and hashed, but completion and owner-time value identity are
  reported as unobserved.
- **3,090 native RHI typed-buffer subranges** and **24 SkipTracking buffers**
  with their exact native pointer/view/status identity.
- **470 NativeUniformBufferBindings records**, **863 ParameterBindings
  records**, **557 ReferencedStructs records** and **1,231 parameter/structure
  blocks**.
- **3,136 unique chunks**, 1,567,676,477 unique raw bytes, 252,014,634
  compressed bytes. The built package has 6,410 files (700,692,456 bytes before
  ZIP). The intermediate publication ZIP is 275,220,096 bytes, SHA-256
  `75cf3c2f1876f2420f7c604cbfed1d6edaa9b62439b96bf9cca632248522412f`.

### Viewport applicability

`ViewRect` is **not** a property of every resource that happens to touch a
camera pass. A resource gets a crop mapping only when all of the following are
positively recorded:

- a camera pass is identified by its own captured `View.View` rect **and** an
  output binding to `SceneColor`, `SceneDepthZ` or `GBufferA`–`GBufferD`;
- the resource is an output of that exact pass (not merely an input);
- the active mip-0 `SubresourceLayouts` record belongs to that pass version and
  its own `ExtentX`/`ExtentY` match the resource descriptor;
- the pass `ViewRect` lies fully inside the allocation. A known same-size
  `SceneColor` must also match the GBuffer extent from the same pass.

Only those 18 resources (3 frames × 6 camera targets) receive screen padding:
1,280 × 720 inside the 1,280 × 888 allocation (168 padded rows below). Every
other resource records an explicit `viewport.applicability` of
`no-view-context`, `other-domain` or `unknown-mapping`; its pass ViewRect is
retained as context only, `padding` is null, and the crop is disabled with a
reason. This covers 4,096 card atlases, world-cache/probe indirection volumes,
cube faces, array and virtual-shadow-map page pools, history resources and
unknown 2D inputs. Allocation area is never used to identify a camera, so the
16,384-wide shadow page pool cannot become one. Cropping is display-only: no
captured byte, layout bound, pitch, stride, component or chunk reference is
changed.

The joined schema is retained byte-for-byte: 1,286,023 bytes, SHA-256
`d2bd0620e9250f29d9788944be6a73ff80d007a9d5bf86a45fe52b2eefc81d2a`,
245 resource schemas, 15 parameter schemas, 1,547 source references, 8 sampler
bindings, 29 declared schema coverage gaps, 14 format variants and 21
`appliesToFormats`-gated fields. Source formulas are displayed with anchors and
are never evaluated.

Coverage is **not complete**: 2,644 explicit gaps remain and
`coverage.json.complete` is false. The capture does not contain pass draw counts
(`Base/PassDrawCounts.json` is empty, so all 928 `drawCount` values stay `null`),
874 buffers have no captured structure metadata, 512 resources have no matching
source schema, and 377 referenced-struct bindings are explicitly null.

## Publication

```
publication/frame-package.zip        complete package-relative archive
publication/archive-manifest.json    every ZIP entry path/bytes/SHA-256 + ZIP SHA
publication/package-report.json      capture/package/verification/regression report
report.json                          the same report at the workspace root
```

The ZIP contains exactly the 6,410 package-relative files (no absolute paths, no
drive letters, no `..` entries). It was verified entry-by-entry against the
built package, then extracted into a fresh system-temp directory and verified
again with `tools/package_frame.py verify`; the temporary extraction was removed
after success. The raw capture and all package chunks/payloads are read-only
inputs; this step does not delete or rewrite them.

The static site at `site/` deliberately ships **without** the raw package. Point
the explorer at an extracted archive with `?package=<url>` or place the package
at `site/assets/lumen-frame/`.

## Package file contract

```
index.json                 manifest, counts, schema status, chunk index
resources.json             lightweight list of every resource, with viewport applicability
passes.json                lightweight list of every pass
coverage.json              counts, explicit categories, every gap
schema.json                the complete source schema, byte-for-byte
structures.json            every StructuresMetadata record, by pointer
capture.json               Infos, CVars, screenshot and the native file inventory
native-inputs.json         native RHI buffers/textures outside ResourceDescs.json
inventory.json             every native record, payload, resource, pass and chunk
records/resource/<id>.json complete resource record, layouts and native bindings
records/pass/<id>.json     complete pass record, native uniform/parameter bindings
chunks/<raw-sha256>.gz     one unique gzip-compressed raw chunk per raw SHA-256
native/<original path>     every native non-.bin file, byte-for-byte
```

All package-generated paths are relative POSIX paths. Absolute host paths that
occur inside native evidence (for example a `FileName` inside captured metadata)
are retained as evidence, not used as package links.

## Exact subresource layout adapter

`Base/SubresourceLayouts.json` is parsed as concatenated JSON objects. Every
record is retained in full, including fields this explorer does not yet
interpret. File identity, not a filename guess, selects the resource and
subresource.

Each payload record exposes the sidecar facts used for addressing:

- `File`, `Resource`, `Name`, `Dimension`;
- `MipLevel`, `FirstArraySlice`, `NumArraySlices`, `CubeFace` when applicable;
- `ExtentX`, `ExtentY`, `Depth`, `ZSliceCount`, `ZSliceOrder`;
- `ZSliceStrideBytes`, `RowPitchBytes`;
- `NativeFormat`, `ViewFormat`, `StagingFormat`, `StoredFormat`;
- `NativeFormatExpanded`, `NativeSRGB`, `ViewSRGB`, `SRGBOverride`,
  `SRGBViewUsedForReadback`, `StoredBytesAreNativeStorage`;
- `NativeValueInterpretation`, `BitExact`, `ValuePreserving`,
  `BitExactCondition`, `StencilSubresource`;
- `Readback`, `DumpStatus`, `FileVerifiedAtSchedule`, `ByteSize`,
  `StagingByteSize` and every unknown field present in the capture.

`StoredFormat` is authoritative for byte addressing. The native descriptor format
is retained separately. This matters for the 168 converted subresources: for
example, BC7/BC5/BC6H native textures were exported with `StoredFormat
PF_FloatRGBA` (8 bytes/texel); the exported bytes are never interpreted with a
BC block size. 303 sidecars are bit-exact integer copies; 1,328 are
value-preserving and 168 are explicitly not value-preserving. The explorer shows
that qualification next to the numeric table.

Completion is joined separately from `Base/DumpReadbackStatus.json` by the exact
`File` key. `DumpStatus: ScheduledForReadback` is shown as scheduling, not
success. A payload with no completion record keeps `observed=false`,
`dumped=false` and an explicit reason. A completion record proves that the file
was written and read back, not that the bytes equal the owner pass's input at
execution time.

### Exact texel addressing

The browser computes:

```
offset = z * ZSliceStrideBytes + y * RowPitchBytes + x * bytesPerTexel(StoredFormat)
```

Bounds are the selected subresource's own `ExtentX`, `ExtentY` and
`ZSliceCount`; descriptor allocation dimensions are not substituted. A row pitch
is never silently "repaired" to `width × bytesPerTexel`; the recorded pitch and
stride are used. Texture2D/2DArray readback rows are tightly packed in this
capture, but the code path remains pitch-aware.

The resource view exposes independent captured-dimension controls for version,
mip, array slice or cube face (and an aspect control when both colour/depth and
stencil payloads exist). The numeric inspector and whole-subresource overview
each have their own z-slice control where `ZSliceCount > 1`. The overview samples
a bounded lattice of whole rows, labels the exact view, and reports an
unavailable result rather than extrapolating when a row or the request exceeds
the 2 MiB cap.

Cube face and array slice are distinct recorded fields. A cube face is a
`CubeFace` view unit; `FirstArraySlice`/`NumArraySlices` are not relabelled as a
face range. Native binding records additionally carry
`ExportArrayFirstInViewUnits`/`ExportArrayCountInViewUnits` and
`ExportFaceFirst`/`ExportFaceCount`, which the UI keeps separate.

Stencil payloads are separate files with `ViewFormat PF_X24_G8` and
`StoredFormat PF_R8_UINT`. The 27 stencil layouts never enter colour/depth
bindings (0 aspect mismatches in the final audit), and a depth binding never
lists stencil bytes.

## Native RHI inputs and typed buffer views

`ResourceDescs.json` describes RDG resources only. Native RHI buffers and
textures bound through uniform buffers are retained as a separate identity set
in `native-inputs.json` (72 native textures and 30 native buffers in this
capture), with their exact native pointer, name, byte size, view offset, view
size, view stride, element count, view format and pass associations.

A native typed subrange file such as
`Resources/NativeBuffer.<pointer>.o<offset>.n<count>.v<pass>.bin` is associated
from the exact `NativeUniformBufferBindings` entry that produced it
(`DumpedAs`/`DumpFile`/`UnderlyingBuffer`), not by parsing the filename. Its
`ViewOffsetInBytes`, `ViewSizeInBytes`, `ViewStrideInBytes`, `ViewNumElements`
and `ViewFormat` are retained in full.

`SkipTrackingBuffer` payloads keep their own completion record and native
identity; they are not merged into ordinary RDG buffer versions.

## Native and RDG uniform bindings

Every `Base/NativeUniformBufferBindings.json` record is exposed on its pass and,
through `DiagnosticResource`/`ExportFirstFile`, from the bound resource side.
All nested member paths, offsets, array counts, strides, pointers, export
fields, skip reasons and unknown fields are preserved and shown as raw JSON in
addition to the summary table.

The top-level temporal facts are preserved, not upgraded:

- `OwnerPassExecutionDependencyClaimed=false`
- `SourceStateProvenAtScheduling=false`
- `TimingRisk=OwnerPassNotExecutedAtCPUCollection;NativeResourceHasNoRDGExecutionDependency`
- `MutableBindingPolicy=NoBytesWhenWritableAliasOrUAVNotObservable`
- `CrossUniformBufferAliasObservable=false`
- `ReadOnlyBindingPolicy=NoTransitionPrivateStagingOutput`

A successful file/hash proves completion only. This app does not claim
owner-time value identity. A separate replay/source review may establish
immutability for static LUT/BlueNoise inputs; that claim is not made here.
Bindings with no captured bytes keep their descriptor, `DumpStatus` and reason
(15,143 of 32,765 binding entries have no captured payload).

`Base/ParameterBindings.json` is also retained per pass with every parameter
pointer member. `Base/ReferencedStructs.json` records are retained in full,
including nested/unknown fields, `Null` state and `ContentKind`; 377 null
bindings are labelled `null-reference` rather than reported as missing files.
The captured `Structures/<UniformBuffer>.bin` bytes are attached to the native
uniform record where present.

Sampler entries record only the bound pointer and
`StaticInitializerCaptured=false`. The sampler initializer remains
source-derived/unknown and is not inferred from the pointer.

### Field format proof

Numeric source-schema fields are extracted only when the packed storage layout is
proven:

- an explicit machine-readable `PF_*` format in `storage.format` that matches the
  captured `StoredFormat`, or
- an explicit `formatVariants` entry selected by the captured `StoredFormat`.

`formatAlternatives` are source possibilities, not evidence of a captured
permutation. A descriptive expression such as
`Lumen::GetDirectLightingAtlasFormat() (R11G11B10 in this capture)` is retained
and displayed, but it is **not** a packed-layout proof. Six texture schema
instances (`cards.resource.resampled-card-capture-direct/indirect`) therefore
keep every field visible with `formatProof: descriptive-only`,
`primaryFieldsExtractable: false`, `extractionEligible: false`, and an explicit
`resource-schema-format-proof-missing` gap. The raw runtime-format texel
inspector remains available for those captured bytes. A descriptive format or an
alternative alone never activates a numeric offset.

Structured-buffer schemas use an explicit element stride (`strideBytes` equal to
the captured `BytesPerElement`) and are labelled
`explicit-element-stride`; they are not treated as a packed pixel format.

## Coverage categories

`coverage.json.categories` keeps six distinct evidence states instead of one
opaque "incomplete" flag:

| Category | Count | Meaning |
| --- | ---: | --- |
| unsupported-decode | 0 | No stored texture format lacks an explicit decoder. |
| missing-bytes | 0 | No captured resource lacks its payload; descriptor-only cases would appear here. |
| unproven-native-timing | 1,653 | 470 owner-pass CPU-collected native bindings plus 1,183 RDG buffer versions with no completion record. |
| inactive-unused-capacity | 12 | Declared buffer capacity exceeds the captured version bytes, or schema `captureObserved=false`. |
| source-only-formula | 1,002 | Source decode equations retained as descriptions, never evaluated. |
| missing-semantic-metadata | 1,087 | Buffers without captured structure metadata, resources without a schema match, and sampler initializers that are source-derived/unknown. |

The 2,644 explicit gaps include the 874 buffer-metadata cases, 512 unmatched
resources, 470 temporal notices, 377 null referenced bindings, 260 source
equations, 33 unsupported field types, 32 passes without bindings, 15 dump-layout
mismatches, 12 buffer-size mismatches, 9 schema-format mismatches, 6
format-proof-missing cases and smaller declared gaps. No gap is hidden by a
successful preview.

Pass draw counts are never fabricated: `Base/PassDrawCounts.json` is empty, so
all 928 `drawCount` values are `null` with status `not-captured`.

## Chunk packing, verification and reconstruction

`tools/package_frame.py build`:

1. hashes every raw payload;
2. splits it into chunks of at most 1,048,576 uncompressed bytes;
3. hashes each raw chunk;
4. gzip-compresses each unique raw chunk once (`mtime=0`);
5. writes `chunks/<raw-sha256>.gz` once;
6. records an ordered `{sha, offset, length}` list for every payload.

`verify` checks every chunk's gzip, decompressed SHA-256 and `index.json`
`rawLength`, reconstructs every payload and checks its length and SHA-256, and
checks every retained native file's bytes. It is a full-package check, not a
sample. `reconstruct` writes every original payload path and verifies the same
hashes.

```powershell
python tools/package_frame.py build `
  --input D:\UnrealEngine\UnrealUnderstandingCapture\capture\buffers\gpudump\binary-20261112-153914 `
  --output scratch\package `
  --schema D:\UnrealUnderstanding\Scripts\Docs\unreal_articles\labs\lumen-frame\schemas\source-schema.json `
  --force
python tools/package_frame.py verify --package scratch\package
python tools/package_frame.py reconstruct --package scratch\package --dest C:\temp\reconstructed-frame
```

The build is reproducible and read-only with respect to the capture. The source
dump is rejected as an output location, and package paths are checked for
absolute paths and `..` traversal.

## Browser behaviour

- Search covers pass/resource names, parameter member names, schema ids,
  quantities/units, math terms and gap messages.
- Both lists are complete; only rendering is paginated.
- Selecting a resource lazily fetches its record, then only the chunks needed for
  the selected probe/page/preview.
- Chunk fetch uses `DecompressionStream('gzip')`, validates the whole
  decompressed chunk against `index.json.rawLength`, verifies SHA-256 when
  `crypto.subtle` is available, and only then slices the requested range. A small
  probe of a 1 MiB chunk is valid; a requested slice length is never mistaken for
  the chunk length.
- A texel probe fetches at most one texel. The hex view fetches one small page.
  A preview fetches a bounded row strip; the overview fetches whole sampled rows
  up to the 2 MiB cap.
- Every stored component remains selectable (R/G/B/A, packed bitfields, or the
  single scalar). The packed bitfield/decode table is a separate exact view and
  is never replaced by a display transform.
- Raw values/bitfields are displayed separately from display transforms.
  Native-vs-stored format, bit-exactness and value-preservation qualifications
  are visible on the layout card.
- Allocation dimensions are shown separately from the recorded `ViewRect`.
  `View.StateFrameIndexMod8`/`ViewRectMinAndSize` come from captured
  `View.View` bytes at native member offsets; a default 1280 × 720 scene size is
  never used as an allocation extent. The resource record carries an explicit
  `viewport` object: a crop is enabled only for a positively identified
  camera-pass screen target whose active mip and bounds match, while every other
  resource keeps the pass ViewRect as context and disables the crop with a
  reason.
- Labels come from recorded metadata: unique-name instance prefix, graph scopes,
  producer-pass interval, frame number and `View.StateFrameIndexMod8`. Every
  resource and pass has a recorded display label.
- Raw downloads are capped at 32 MiB in the browser; larger payloads must be
  reconstructed with the Python command.
- The global-fetch receiver fix is retained: the store calls the current global
  `fetch` with the global receiver, and the Node regression test uses a
  receiver-sensitive fake to exercise the production path.

## Tests

```powershell
python -m unittest discover -s tests -v
node tests/test_codec.cjs
# optional real-package byte comparison:
$env:FRAME_PACKAGE_DIR = "C:\path\to\extracted\frame-package"
node tests/test_read_path.cjs
```

The Python suite covers the synthetic DumpGPU fixture and the instrumented
sidecar shape: actual 3D/cube/array layout records, native typed subranges,
completion-vs-scheduling, converted BC7 → `PF_FloatRGBA`, reused native views,
unknown sidecar fields, depth/stencil disjointness, empty draw counts and
lossless chunk reconstruction. It also covers viewport applicability: a
synthetic camera pass crops a 128 × 96 GBuffer to its 128 × 72 ViewRect while a
4,096 card atlas, a 3D indirection volume, a cube, a VSM page pool and a
history resource keep their own domains. When `FRAME_PACKAGE_DIR` points at an
extracted real package, four additional checks verify the actual 1,280 × 720
mapping from 1,280 × 888, the non-padding classification of those domains,
byte-identical raw subresource layouts/counts, and the six descriptive
resampled-card schemas whose fields stay visibly inactive. The codec suite
covers sidecar pitch/stride addressing, descriptive-format proof, packed
formats, channel lists and cross-chunk reads. The read-path suite reads selected
bytes from the final capture through the production `PackageStore` (7 cases,
including the actual GBufferA payload, a Texture3D z slice, a cube face, an
array slice, a converted BC7 texel and a native typed-buffer subrange), plus
receiver, cache, slice-length, viewport and descriptive-schema checks. When no
package is present it prints a skip for only the package-dependent portion; the
static checks still run.

## Known limitations

- This capture is a DumpGPU/instrumented export, not a complete engine trace.
  Shader-local values, non-RDG RHI-bound resources not surfaced through the
  captured uniform maps, and some RDG uniform contents are outside its scope.
- 1,183 RDG buffer version files have no `DumpReadbackStatus` completion record;
  they are visible and hashed but their completion is unobserved.
- 874 buffers have no captured structure metadata, so typed fields require a
  source schema or remain raw.
- 12 buffer descriptors disagree between `ByteSize` and
  `BytesPerElement × NumElements`; raw bytes and the explicit stride are
  authoritative.
- 6 descriptive `cards.resource.resampled-card-capture-*` texture schemas have no
  machine-readable packed format; their numeric extraction is intentionally
  disabled while every field and the raw texture inspector remain available.
- Pass draw counts are not captured and stay null. MSAA is not decoded.
- The UI requires HTTP(S); `file://` fetch/decompression limitations are not
  papered over.
- The verified package is published as individual files under
  `Docs/Unreal/assets/lumen-frame/`; chunks are fetched on demand. The intermediate
  ZIP is not duplicated in this repository.
