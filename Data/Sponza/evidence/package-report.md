# Sponza frame package report

Status: **complete**. Source capture run `20261115-083654`; package `output/final/frame-package`; no GPU workload, no Git command and no article were produced in this step.

This step takes the preserved raw DumpGPU directory as read-only input and builds one static, lossless frame-explorer package. Every available raw file is either stored as a compressed payload chunk (by SHA-256) or copied byte-for-byte as a native file. The exact package size is **657,904,432 bytes in 6,852 files**.

## Result at a glance

| Item | Value |
| --- | ---: |
| Source raw files / bytes | 12,458 / 95,239,807,151 |
| Source payload files / bytes | 11,446 / 95,161,036,158 |
| Source native (non-payload) files / bytes | 1,012 / 78,770,993 |
| Static package files | 6,852 |
| Static package bytes | 657,904,432 |
| Unique chunks (compressed) | 3,416 |
| Compressed unique-chunk bytes | 551,246,399 |
| Unique raw chunk bytes | 1,745,497,466 |
| Metadata files (gzip) | 2,423 |
| Metadata stored / raw bytes | 26,896,113 / 475,373,835 |
| Native inputs | 99 resources (69 textures, 30 buffers) |
| Reconstructed payload size | 95,161,036,158 bytes, 11,446 files |
| Largest package file | `native/Base/NativeUniformBufferBindings.json`, 43,256,400 bytes |
| Coverage gaps | 2,913; `coverage.complete=false` |
| Missing bytes | 0 |

The 11,446 payload files are the 10,318 version-keyed `Resources/*.bin` files from the capture plus the 1,128 `Structures/*.bin` parameter/structure payloads; together with the 1,012 native files they account for all 12,458 raw files.

### Missing-evidence summary

| Evidence | Status |
| --- | --- |
| Payload bytes missing | **0** of 11,446 payload files; all 1,541 native descriptors have a payload |
| Native copies missing or mismatched | **0** of 1,012 |
| Chunk / reconstructed-payload mismatches | **0** of 3,416 chunks / 11,446 payloads |
| Pass draw counts | **886 not captured** (`Base/PassDrawCounts.json` is empty) |
| Shader parameter-binding records | **83 passes have none** (80 parameterless engine passes + 3 ShaderPrint); raw structure payloads are retained separately |
| RDG buffer-version completion | **1,297 versions have no readback completion record**; bytes retained and hashed, owner-time identity unproven |
| Acceleration-structure usability | **Unproven for the 9 ray-tracing SceneBuffer descriptors**; D3D12 staging-copy warning retained, nonzero bytes are not treated as a usability proof |
| Coverage | **`complete=false`**, 2,913 explicit gaps; full ledger in `frame-package-gaps.tsv` |

## Source provenance

| Item | Value |
| --- | --- |
| Raw directory | `output/gpu-dump/raw/sponza-20261115-083654` |
| Capture report / status | `capture-report.md` / `ok` |
| Dump run | `20261115-083654`, one unfiltered `-oneframe` DumpGPU frame, `RedumpInputs=1` |
| Raw files / bytes | 12,458 / 95,239,807,151 |
| Raw payload files / bytes | 11,446 / 95,161,036,158 |
| Raw native files / bytes | 1,012 / 78,770,993 |
| Screenshot SHA-256 | `faa77025dcfd82b4982a28a2a155205ae6347fc03989923b6dc2d9423faaa6eb` |
| Map SHA-256 | `3ef49eddd474fc16e6129dac3bcbb16bb4937529d9213296a865d368ede72c64` |
| Engine | UE 5.8.1, BuildId `b4effda9-1b7d-4e62-b9a6-cb99e41a70c9` |
| Joined schema | `source-schema.json`, 1,286,023 bytes, SHA-256 `d2bd0620e9250f29d9788944be6a73ff80d007a9d5bf86a45fe52b2eefc81d2a` |
| Package `index.json` | 990,927 bytes, SHA-256 `cab06a753e1380a4ffca0f1c2810572f005beaba6e66d2605eb13259f4365088` |
| Package generated UTC | `2026-11-15T08:54:37Z` |

The package tool and schema were read from the shared, read-only implementation tree at `D:/UnrealUnderstanding/Scripts/Docs/unreal_articles/labs/lumen-frame/`. No file under that tree was modified. The raw Sponza capture and every pre-existing workspace file were not changed.

Separate analysis note: the root reported that `scripts/analyze_gpu_dump.py` reads the `FPackedView` `TranslatedWorldToView` matrix at offset 192 instead of 128, so the three `translated_world_to_view_rows` records in the preserved `output/gpu-dump/analysis-20261115-083654.json` currently contain the clip/projection-containing matrix. That file and decoder are preserved dump-step evidence and were not modified by this package step; the offset finding was forwarded to the `decode` task. The package's own camera/pass mapping uses the captured `ViewRect` and pass output bindings, and the capture report's origin/FOV/forward identity checks use the other decoded fields; neither uses the affected matrix field.

## Exact package composition

| Component | Files | Bytes | Share of package |
| --- | ---: | ---: | ---: |
| Compressed unique chunks (`chunks/<sha>.gz`) | 3,416 | 551,246,399 | 83.79% |
| Native non-payload copies (`native/<original path>`) | 1,012 | 78,770,993 | 11.97% |
| Metadata records and summaries (`.json.gz`) | 2,423 | 26,896,113 | 4.09% |
| Raw bootstrap `index.json` | 1 | 990,927 | 0.15% |
| **Total** | **6,852** | **657,904,432** | **100%** |

Payload storage:

- 95,161,036,158 payload bytes from 11,446 payload files become 1,745,497,466 unique raw bytes in 3,416 chunks: a **54.52:1** deduplication of payload bytes before compression.
- Those 1,745,497,466 unique raw bytes compress to **551,246,399 bytes** (31.58% of unique raw; 3.17:1).
- Largest chunk: `chunks/b135dd62c73cdee1920885d28baf2b71643a0577934a77cda6d584c35162de0b.gz`, 1,045,243 bytes compressed from 1,048,576 raw bytes.
- Generation is deterministic: every chunk is keyed by the SHA-256 of its raw bytes; gzip uses `mtime=0`. No payload was pruned, sampled, rewritten or synthesized.

Metadata storage:

- 2,423 generated metadata files store 475,373,835 raw bytes as 26,896,113 gzip bytes (5.66%; 17.67:1).
- `index.json` deliberately remains raw so the browser can bootstrap; its `files` map names the actual `.json.gz` paths. `schema.json.gz` decompresses to the exact 1,286,023-byte source schema.
- The largest non-chunk file is the native, uncompressed `native/Base/NativeUniformBufferBindings.json` (43,256,400 bytes), larger than the largest metadata summary.

Logical inventory retained in the package:

| Item | Count |
| --- | ---: |
| Native descriptors (all kinds) | 1,541 |
| Normalized resources | 1,529 |
| Passes | 886 |
| Parameter/structure payloads | 1,128 |
| Structures / uniform structures | 487 referenced records, 995 uniform structures |
| Subresource layouts | 6,675 |
| Readback status records | 10,404 (all `Dumped`) |
| Native uniform-buffer records / bindings | 521 / 35,648 |
| Parameter-binding records | 803 |
| Console variables | 9,262 |
| Native files copied | 1,012 |
| Camera-mapped screen resources | 18 (`1280x720` inside `1280x888`; crop is display-only) |

The resource count is 1,529 rather than 1,541 because 12 duplicate native descriptors share a payload key; all 1,541 descriptor records are retained and the 12 duplicates are recorded as explicit `duplicate-resource-descriptor` gaps.

## Native inputs

`native-inputs.json.gz` retains **99** native RHI resources outside `ResourceDescs.json`:

| Kind | Count |
| --- | ---: |
| Native textures | 69 |
| Native buffers | 30 |

Their captured bindings are retained in full: **35,648** bindings, **19,182 with captured bytes** and **16,466 without bytes**; **15,417** bindings are reused native views. The package also carries the native uniform-buffer records (521) and all 35,648 binding entries. It does not claim owner-pass value identity for the 521 records collected during graph-build CPU traversal; those records are flagged with the `owner-pass-execution-not-claimed` gap rather than silently promoted to execution-time values. The capture report records 69 exported native-file references, all 69 present on disk and represented in the package.

## Lossless verification

All commands ran in the foreground and completed with exit code 0. Logs are retained next to the package.

### 1. Estimate first

```text
python tools/package_frame.py build --input output/gpu-dump/raw/sponza-20261115-083654 \
  --schema .../schemas/source-schema.json --estimate --metadata-gzip
```

Result (`output/final/package-estimate.log`, 174.4 s): 11,446 payload files / 95,161,036,158 bytes; 3,416 unique chunks / 1,745,497,466 unique raw bytes / 551,246,399 compressed bytes; 2,913 gaps; no files written.

### 2. Full package build

```text
python tools/package_frame.py build --input output/gpu-dump/raw/sponza-20261115-083654 \
  --output output/final/frame-package --schema .../schemas/source-schema.json --metadata-gzip
```

Result (`output/final/package-build.log`, 184.3 s): **6,852 files / 657,904,432 bytes**; metadata 2,423 files, 475,373,835 raw bytes, 26,896,113 stored bytes at gzip level 6; largest output `native/Base/NativeUniformBufferBindings.json` (43,256,400 bytes).

### 3. Package verification

```text
python tools/package_frame.py verify --package output/final/frame-package
```

Result (`output/final/package-verify.log`, 128.9 s): verified **3,416 unique chunks, 11,446 payloads and 1,012 native files**; raw payload bytes 95,161,036,158; 2,913 gaps; no mismatch.

### 4. Full reconstruction

```text
python tools/package_frame.py reconstruct --package output/final/frame-package \
  --dest output/final/frame-package-reconstruct
```

Result (`output/final/package-reconstruct.log`, 256.3 s): the invocation verified the package (3,416 chunks, 11,446 payloads, 1,012 native files) and reconstructed **11,446 payloads / 95,161,036,158 bytes**; each payload was hashed against its recorded SHA-256 while it was written. The reconstruction directory holds 11,447 files: the 11,446 payload paths plus `reconstruction-manifest.json`. The independent audit below then re-read every written payload and compared it with the original source file.

### 5. Independent source-vs-package hash audit

`scripts/package_frame_audit.py` was added to hash the original source files and the reconstructed files themselves, rather than trusting the package's recorded hashes. It also hashed every package file.

```text
python scripts/package_frame_audit.py \
  --package output/final/frame-package \
  --source output/gpu-dump/raw/sponza-20261115-083654 \
  --reconstructed output/final/frame-package-reconstruct \
  --output output/final/frame-package-audit.json
```

Result (`output/final/package-audit.log`, 68.4 s; `output/final/frame-package-audit.json`, SHA-256 `eb3a926dcd2f587ad7671f2a04a923942fce75c0c98f745165ab9ff7e95cfbb4`):

| Check | Result |
| --- | --- |
| Source files enumerated | 12,458 |
| Source files accounted for by package payload/native sets | 12,458 |
| Missing from package / extra package paths | 0 / 0 |
| Payloads compared source-vs-reconstructed | 11,446 |
| Payload SHA-256/length mismatches | **0** |
| Native copies compared source-vs-package | 1,012 |
| Native SHA-256/length mismatches | **0** |
| Chunk gzip/decompressed-SHA/length mismatches | **0** (3,416 chunks) |
| Package files hashed and sized | 6,852 / 657,904,432 bytes |
| Promised metadata links missing | 0 |
| Reconstructed payload bytes checked | 95,161,036,158 |

The independent audit therefore establishes the acceptance condition directly: every available raw file is accounted for, and every reconstructed payload byte and every native copy matches its source byte-for-byte.

## Coverage and all gaps

`coverage.complete` is **false**. The package records **2,913** explicit gaps: **2,834 info** and **79 warning**, with **0 missing-bytes** and **0 unsupported-decode** entries. Every one of the 2,913 records, not a sample, is enumerated in `output/final/frame-package-gaps.tsv` (2,913 data rows, 723,990 bytes, SHA-256 `d088afa6a54d6bf0953411e384464c46f318ee33e986b2940fde418b42d8167b`) and is retained in the package as `coverage.json.gz` (SHA-256 `06175043b804d66e24be18f3dd75a3051c29d14434d8eb847916797e0c0c0f61`).

Gap scope totals: capture 3, pass 441, resource 25, schema 626, decoding 1,297, timing 521; these sum to 2,913.

### All gaps by kind

| Gap kind | Count | Severity | Scope | Evidence |
| --- | ---: | --- | --- | --- |
| `buffer-metadata-missing` | 970 | info | decoding | Buffer has no captured structure metadata; raw bytes retained, typed fields not inferred. |
| `resource-schema-unmatched` | 598 | info | schema | No source resource-schema selector matched; raw metadata remains authoritative. |
| `owner-pass-execution-not-claimed` | 521 | info | timing | Native uniform bindings were collected during graph-build CPU traversal, not after owner-pass execution; no owner-time value-identity claim. |
| `referenced-struct-null` | 406 | info | pass | Referenced struct is explicitly null (`RHIUniformBufferUnbound`); no block fabricated. |
| `schema-field-source-equation` | 260 | info | decoding | Source equation retained as description; typed raw value shown; equation not evaluated. |
| `pass-without-bindings` | 35 | info | pass | Pass has no captured input/output resources. |
| `schema-field-type-unsupported` | 33 | warning | decoding | Field type has no numeric extractor; descriptive only, bytes retained. |
| `resource-schema-dump-layout-mismatch` | 15 | warning | decoding | Schema layout differs from captured dump layout; offsets not applied to converted payload. |
| `parameter-schema-source-fields` | 15 | info | schema | Parameter schema has source-level fields without captured byte offsets. |
| `duplicate-resource-descriptor` | 12 | warning | resource | Duplicate native descriptor key; all records retained, first used for normalization. |
| `buffer-size-mismatch` | 12 | info | resource | Declared size differs from element count × stride; raw bytes authoritative. |
| `parameter-schema-descriptive-only` | 10 | info | schema | Descriptive parameter schema not joined to a captured pass; no values inferred. |
| `resource-schema-format-mismatch` | 9 | warning | decoding | Schema format differs from captured descriptor format; extraction disabled where not proven. |
| `resource-schema-format-proof-missing` | 6 | warning | decoding | Descriptive storage format; exact-format proof absent. |
| `schema-field-offset-missing` | 4 | info | decoding | Field has no byte/bit offset; descriptive only. |
| `resource-schema-ambiguous` | 3 | warning | schema | More than one schema matched; all shown, none silently selected. |
| `dump-scope-unknown` | 1 | info | capture | DumpGPU filters/settings can omit data; completeness relative to the live frame cannot be proven from the dump alone. |
| `shader-local-state-not-captured` | 1 | info | capture | Shader-local registers and intermediates are not RDG resources and are not synthesized. |
| `declared-subresources-not-all-observed` | 1 | info | resource | 6 subresource payloads observed against 9 declared slots; missing slots not inferred. |
| `pass-draw-counts-empty` | 1 | warning | capture | `Base/PassDrawCounts.json` is empty; all 886 pass draw counts are `null` (`not-captured`). |

### Coverage categories

The package's six coverage categories overlap; their counts must not be added to reproduce 2,913. `omitted` is the number of category items left out of the category's preview list only, never out of the package.

| Category | Count | Omitted from preview |
| --- | ---: | ---: |
| `unsupported-decode` | 0 | 0 |
| `missing-bytes` | 0 | 0 |
| `unproven-native-timing` | 1,818 | 1,618 |
| `inactive-unused-capacity` | 12 | 0 |
| `source-only-formula` | 1,002 | 802 |
| `missing-semantic-metadata` | 1,170 | 970 |

### Explicit capture-level limits carried into this report

These are gaps in what the capture can prove; they are not packaging defects and are not hidden by the successful reconstruction.

- **Ray-tracing acceleration-structure staging warning.** D3D12 emitted `RHICopyToStagingBuffer cannot be used on the RayTracing Accelleration structure FRayTracingScene::SceneBuffer` 9 times. The dump has 9 SceneBuffer descriptors with payload and a generic `Dumped` status. The package preserves and hashes those bytes, but a nonzero SceneBuffer payload plus `Dumped` **does not establish that the acceleration-structure copy is usable**. No ray-tracing-structure usability claim is made. This warning comes from the DumpGPU log; the package's gap ledger books the same resources under `buffer-metadata-missing` / `resource-schema-unmatched`, so it must be read together with this provenance note.
- **Pass draw counts.** `Base/PassDrawCounts.json` is 0 bytes. All 886 `drawCount` values are `null` with status `not-captured`; zero is never substituted (`pass-draw-counts-empty`).
- **Parameter-binding gaps.** The capture report's `pass_parameter_block_missing` gap covers the 3 `ShaderPrint::UploadParameters` passes: no `ParameterBindings` block was serialized for them. The package does attach the retained 16-byte `Structures/<parameters pointer>.bin` structure payload for each, with `parameterBindingCount=0`. That raw 16-byte structure is not a full shader parameter-binding block.
- **Parameterless passes.** 80 clear/copy/barrier/graph/ray-tracing-build passes are parameterless by engine type. In total, 83 of 886 passes have zero `ParameterBindings` records (the 80 plus the 3 ShaderPrint passes). The package's headline "886 passes with a parameter block" counts a retained raw structure payload for every pass; only **803** passes have captured `ParameterBindings` records. Do not read 886 as 886 binding blocks.
- **Read-only CVar.** `r.RHI.UniformBufferContentMap.Enable` is read-only at runtime; the launcher set it before uniform creation and it was observed `true`. The rejected runtime assignment changed nothing; the final CVar table is retained.
- **Shader-local and non-RDG state.** Shader-local registers, per-pixel/per-thread choices, memoryless resources, resources never referenced by an RDG pass, and writable SkipTracking resources are outside DumpGPU's captured resource set. Absence is explicit, never filled.
- **Uniform-buffer content scope.** The 521 native uniform-buffer records expose the captured CPU content map and offsets (35,648 bindings); 16,466 bindings have no captured bytes. Shader-local values not written to a uniform buffer are not recoverable.
- **Subresource completeness.** One resource has 6 observed subresource payloads against 9 declared mip/array slots; missing slots are not inferred. In addition, 1,297 RDG buffer-version payloads have no readback completion record: their bytes are retained and hashed, but completion and owner-time value identity are unproven.

## Browser package URL

No publication or Pages placement was performed; no Sponza source asset bundle was published. The root owns the Pages budget decision after the exact **657,904,432-byte** package size.

The existing static explorer supports a second package through its `?package=` query parameter. `frame-explorer.js` reads `URLSearchParams(...).get('package')`, resolves it against the document, appends a trailing slash if needed, and fetches `index.json` from that directory (see `Docs/Unreal/assets/frame-explorer.js`, `PackageStore.prototype.load`, lines 372–382). Serve `output/final/frame-package` at a stable URL and point the explorer at the directory, for example:

```text
frame-explorer.html?package=<URL-to-frame-package-directory>/
```

The package is self-contained: `index.json` is raw; all subsequent metadata is fetched through `index.files`, including `.json.gz` records and `chunks/<sha>.gz`. No explorer change was needed. Cross-origin serving must allow the explorer to fetch the package (normal CORS rules apply).

## Checkpoint and reproduction

`output/final/frame-package-checkpoint.json` records every completed phase and its result:

| Phase | Status | Wall time | Log / result |
| --- | --- | ---: | --- |
| Estimate | ok | 174.4 s | `output/final/package-estimate.log` |
| Build | ok | 184.3 s | `output/final/package-build.log` |
| Verify | ok | 128.9 s | `output/final/package-verify.log` |
| Reconstruct | ok | 256.3 s | `output/final/package-reconstruct.log` |
| Independent audit | ok | 68.4 s | `output/final/frame-package-audit.json` |

Reproduction is the five commands in “Lossless verification”. The only step-specific code added is `scripts/package_frame_audit.py`; the shared packer, schema and explorer were used read-only. The package is **read-only with respect to the raw dump**, and the raw directory still contains its original 12,458 files / 95,239,807,151 bytes.

## Requested acceptance checks

1. **Full static package reconstructs every available file exactly: yes.** The tool reconstructed 11,446 payloads / 95,161,036,158 bytes and re-verified 3,416 chunks, 11,446 payloads and 1,012 native files; the independent audit then compared all 11,446 reconstructed payloads and all 1,012 native copies against the original source files, with 0 hash/length mismatches, and accounted for all 12,458 source files with 0 missing and 0 extra.
2. **File count, compressed bytes, metadata bytes and missing evidence are reported: yes.** 6,852 package files / 657,904,432 bytes; 551,246,399 compressed unique-chunk bytes; 26,896,113 metadata stored bytes (475,373,835 raw); 2,913 explicit gaps and `coverage.complete=false`, with the complete gap ledger in `output/final/frame-package-gaps.tsv` and `output/final/frame-package/coverage.json.gz`.
