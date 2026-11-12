# Publication package report

Status: **OK**

This package is a portable copy of the verified existing reference and native
DumpGPU artifacts. It retains the full native resource/pass inventory and makes
every known native omission explicit. Raw 14.5 GB `Resources/` binaries, build
products and private logs are not included.

## Package size and contents

| Item | Value |
|---|---:|
| Payload files (excluding `file-list.json` and `package-report.*`) | 1930 |
| Payload bytes (excluding `file-list.json` and `package-report.*`) | 24,197,023 |
| PNG figures | 21 |
| Reference PNGs | 3 |
| Decoded native PNGs | 13 |
| VSM PNGs (mask/vpage/cache + lit baseline) | 4 |
| Native screenshot | 1 |
| Raw binaries included | false |
| Build products included | false |
| Private/full logs included | false |

## Acceptance checks

| Check | Status | Evidence |
|---|---|---|
| Existing reference, VSM and decoded images copied with verified hashes | pass | 3 reference PNGs, 13 decoded native PNGs, 3 VSM visualizations + lit baseline and 1 native screenshot; all sizes and SHA-256 values match their manifests. |
| Portable project and capture scripts packaged without build products or private logs | pass | Minimal UnrealLab .uproject/Config/Source/map/materials plus 9 Python and 2 PowerShell portable scripts; no Binaries, Intermediate, DerivedDataCache, Saved, stdout/stderr or UnrealLab.log; EngineRoot is a mandatory parameter. |
| Native resource inventory and known gaps retained honestly | pass | All 1389 resources, 928 passes and 2481 resource-file records retained (14,517,078,510 bytes); 105 descriptor-only records, 78 Texture3D, 3 TextureCube, 24 SkipTracking warnings and the LumenCardScene RDG UB mapping gap are explicit. |

## Native attempt counts

| Measure | Value |
|---|---:|
| Dump status | `ok` |
| Engine frame | 292 |
| Resources | 1389 |
| Captured resources | 1284 |
| Descriptor-only resources | 105 |
| Partial resources | 0 |
| Passes | 928 |
| Passes without parameter payload | 0 |
| Resource binary files | 2481 |
| Resource binary bytes | 14,517,078,510 |
| Texture3D total / captured | 78 / 0 |
| TextureCube total / captured | 3 / 0 |

## Known native omissions

- **Texture3D payloads absent** — All 78 Texture3D resources are descriptor-only. DumpGPU's TranslateSubresourceDumpDesc fills extents only for Texture2D, so no 3D texture bytes exist in Resources/. Descriptors remain in inventory.json.
- **TextureCube payloads absent** — All 3 TextureCube resources are descriptor-only for the same IsTexture2D gate. Descriptors remain in inventory.json; face bytes are not present.
- **SkipTracking default/dummy buffers** — 24 warning lines record DefaultStructuredBuffer, DefaultByteAddressBuffer, DefaultBuffer and VirtualTexture_FeedbackBuffer skipped because of ERDGBufferFlags::SkipTracking. Exact warning lines are retained in missing-coverage.json.
- **RDG uniform-buffer payload mappings (LumenCardScene)** — This export retains pass parameter payloads and StructuresMetadata for shader structures including LumenCardScene, but it does not provide an exact mapping from every RDG uniform-buffer payload to the corresponding RDG resource. Source instrumentation is expected to close this in later data. No such mapping is claimed here.
- **Non-RDG / never-viewed resources** — Only resources referenced by an RDG pass are discoverable. RHI-external resources, never-viewed resources, swapchain/backbuffer state and driver-private allocations are absent.
- **Shader-local registers** — DumpGPU captures resources and pass parameter payloads, not registers or values local to a shader invocation. Shader source mapping is evidence, not a memory capture.
- **RHI descriptor views** — Passes.json names resource versions bound to passes; it is not a full descriptor-heap or SRV/UAV view dump.
- **RedumpInputs=0** — When a resource is modified in place, later passes link to the one dumped producer version rather than per-read duplicates. Passes.json preserves the version linkage.
- **Depth/stencil preprocessing** — Depth/stencil and sub-4-byte textures are copied through a staging preprocess; stored bytes are the postprocessed format, not native RHI layout.
- **Upload/Explore disabled** — r.DumpGPU.Upload=0 and r.DumpGPU.Explore=0; nothing was uploaded or opened. Raw dump path is external and not included.

No exhaustive computational coverage is claimed for this native attempt.

## Reference facts and corrected wording

- Reference images: lumen-lit.png, pathtracer-256spp-8bounces.png and pathtracer-1024spp-8bounces.png, all 1280x720 display-referred PNGs with manifest hashes.
- Path-tracer sample indices: 256 spp / 8 bounces / denoiser 0 and 1024 spp / 8 bounces / denoiser 0; engine sample index and reported count both equal 256 or 1024. 256 samples accumulated across about 86 draw requests.
- Scene light: Ceiling area light, 2400 lumens, 130x120 cm, attenuation radius 1000 cm (saved map value; the old 2000 cm request was not rebuilt).
- Exposure: histogram AEM_HISTOGRAM pinned by min_brightness == max_brightness == EV100 4 and bias 0; not manual exposure mode.

The reference is a finite render, not ground truth. 8-bounce truncation and the
unoverridden engine path-intensity clamp (`PathTracing.MaxPathIntensity = 24.0`)
can bias it. The SDR PNGs are display-referred 8-bit captures, not linear EXR and
not a transport-error metric. A draw request is not a path-tracing sample; the
engine sample index proves 256 and 1024 samples. The saved map keeps its recorded
RectLight attenuation radius of 1000 cm and 2400 lumens; the old requested 2000 cm
value was not rebuilt.

## Verification

- Command: `python reproduce/scripts/verify_package.py`
- Status: **OK**
- Checks: 6001/6001 passed
- Failed checks: none

The verifier checks every file-list hash and size, every PNG against its
manifest, inventory row/byte counts, scene/light/exposure/sample-index facts,
absence of raw resources/build products/private logs, and Python syntax.

## Exclusions

`Binaries/`, `Intermediate/`, `DerivedDataCache/`, `Saved/`, stdout/stderr logs,
the native `Base/UnrealLab.log`, the raw `Resources/` trees and the 14.5 GB raw
dump are not in this package. The non-rendering AndroidFileServer SecurityToken
is redacted in the public Config and embedded ini; renderer/RHI lines are unchanged.

## Wording corrections carried into this package

- Exposure is histogram `AEM_HISTOGRAM` pinned by min = max = EV100 4, not manual exposure mode.
- Draw requests are not PT samples; 256 samples accumulated across about 86 draws, proven by the engine sample index.
- The 8-bounce path tracer and path-intensity clamp may bias the finite reference; it is not ground truth.
- The SDR PNG is not linear EXR and is not a transport-error metric.
- Native Texture3D, TextureCube, SkipTracking and RDG UB payload mappings (LumenCardScene) gaps are explicit.
