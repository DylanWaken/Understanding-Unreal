# Reproduce and verify the publication package

## 1. What this package proves

- The existing Lumen laboratory room was loaded from the saved
  `/Game/Labs/LumenRoom` map (SHA-256
  `f7255018f916af33894b1d0bd811b21585dc0151fca33fddc3a538851337e818`), not rebuilt.
- The reference Lumen image, the 256 spp path-traced image and the 1024 spp
  path-traced image are byte-preserved copies. Their SHA-256 values are in
  `data/reference/reference-image-map.json` and `file-list.json`.
- The VSM screenshots and all 13 decoded native figures are byte-preserved copies.
  Their sizes, hashes, source resource ids, pass ids/events and native frame are in
  `data/native/figure-manifest.json` and `data/native/figure-to-native-map.json`.
- The native resource/pass inventories are retained in full:
  1389 resources, 928 passes, 2481 resource files, 14,517,078,510 resource bytes.
  No rendering operand or resource record was removed to shrink the package.
- The native raw binaries are **not** in the package. The raw dump remains local and
  is referenced only as an external, non-packaged object.

## 2. Layout

```text
publication/
  README.md
  reproduction.md
  package-report.md / package-report.json
  file-list.json
  figures/reference/           lumen + 256/1024 spp reference PNGs
  figures/native/decoded/      13 decoded native figures
  figures/native/vsm/          VSM mask/vpage/cache + lit baseline
  figures/native/native-screenshot.png
  data/reference/              reference scene/manifest/session/verification/report
  data/native/                 inventory/pass/resource-file/gap/runtime/scene data
  data/native/dump-metadata/   Base JSON/CSV + Structures/ + StructuresMetadata/
  reproduce/UnrealLab/         minimal project: .uproject, Config, Source, map/materials
  reproduce/scripts/           portable capture, inventory, decode, runtime, verifier
```

Excluded on purpose: `Binaries/`, `Intermediate/`, `DerivedDataCache/`, `Saved/`,
all full stdout/stderr logs, the native `Base/UnrealLab.log`, the 14.5 GB
`Resources/` binaries, and the empty/cache-only project directories. The only edit to
the copied project config (and the embedded ini copy in `data/reference/scene.json`)
is the non-rendering AndroidFileServer `SecurityToken`, which is set to
`REDACTED_FOR_PUBLICATION`; every renderer/RHI line is unchanged.

## 3. Verify the package without Unreal

Run from the package root (the directory containing this file):

```powershell
python reproduce/scripts/verify_package.py
```

The verifier does not import Unreal and does not need the raw dump. It checks:

- every `file-list.json` entry exists with the listed size and SHA-256;
- every packaged file except `file-list.json` is listed;
- all 21 PNGs are byte-identical to their manifest entries, including the 13
  decoded figures, the 3 VSM visualizations, the VSM lit baseline and the
  3 reference images;
- the scene light values, exposure wording, 256/1024 sample indices, draw-vs-sample
  distinction and native coverage counts;
- the absence of raw `Resources/`, build products, private logs and absolute host
  paths.

Optional original-image comparison (only if the original published image and
scene inventory are available outside this package):

```powershell
python reproduce/scripts/verify_package.py `
  --published-lumen <path-to-published-lumen-lit.png> `
  --scene-inventory <path-to-scene-inventory.json>
```

That comparison is optional; the package itself does not require the original
source-reference tree.

## 4. Rebuild and run the project

Use an Unreal Engine 5.8 installation. `EngineRoot` is mandatory in the portable
scripts, so no machine-specific engine path is baked into them.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  reproduce/scripts/run_reference.ps1 `
  -EngineRoot <path-to-UnrealEngine> -Build
```

Without `-Build`, the same runner starts the hidden editor directly. It writes
captures, logs and `run-metadata.json` below `reproduce/output/reference/` by
default; override with `-OutputRoot`. The Python capture script also honors
`ARDA_REFERENCE_ROOT`, `ARDA_PROJECT_ROOT`, `ARDA_REFERENCE_OUTPUT` and
`ARDA_SCENE_INVENTORY`.

The capture sequence is the recorded one:

1. Load the saved room and compare it with the original scene inventory when that
   optional inventory is supplied (`ARDA_REQUIRE_SCENE_MATCH=1` makes it mandatory).
2. Apply the original camera `(0,-400,165)` -> `(0,280,140)` cm, 43 deg vertical FOV.
3. Apply exposure by histogram min = max = EV100 4 with bias 0. This is **not**
   manual exposure mode; `AEM_HISTOGRAM` remains active and the equal min/max values
   pin the histogram-derived exposure.
4. Lumen warm-up: 200 initial draws plus 90 settle draws; capture `lumen-lit.png`.
5. Path tracer: 256 spp, 8 bounces, denoiser 0, filter width 3, MIS 2; discard 64
   PSO warm-up draws; force a reset with `r.PathTracing.SamplesPerPixel 255` -> `256`;
   wait until the engine sample index and reported sample count are both 256.
6. Capture. The engine `GetPathTracingSampleIndex()` proves 256 samples. 256 samples
   accumulated across about 86 explicit viewport draw requests; **draw requests are
   not path-tracer samples**.

The 1024 spp image was captured by the `pt1024` mode; its engine sample index and
reported count are both 1024.

## 5. Capture and analyze a native DumpGPU frame

Raw capture (needs the GPU and the built editor):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  reproduce/scripts/run_gpudump.ps1 `
  -Mode binary -EngineRoot <path-to-UnrealEngine> -UniformBufferMap
```

`ARDA_BUFFERS_ROOT`, `ARDA_PROJECT_ROOT`, `ARDA_DUMP_MODE` and
`ARDA_REFERENCE_CAPTURES` are set by the runner; the Python script reads them.
`-Mode metadata`, `-Mode vsm` and `-Mode pt1024` are also supported. The 1024 spp
capture is written to `reproduce/output/reference/captures/` by default.

Inventory and decoding (no Unreal import):

```powershell
python reproduce/scripts/analyze_dump.py <raw-dump-dir> --out <output-json>
python reproduce/scripts/build_inventory.py <raw-dump-dir> --out-dir <inventory-dir>
$env:ARDA_BUFFERS_ROOT = "<inventory-dir>"
$env:ARDA_DUMP_DIR = "<raw-dump-dir>"
python reproduce/scripts/decode_figures.py
python reproduce/scripts/extract_runtime.py
```

`decode_figures.py` writes decoded PNGs and `figure-manifest.json`; it needs
`inventory.json` and the raw dump. `extract_runtime.py` reads the binary session JSON,
`inventory.json`, `pass-inventory.json` and the raw `Base/`/`Structures*/` metadata.
The copied scripts never import Unreal in these offline paths.

## 6. Scene and exposure facts (corrected wording)

- Saved map light: `Ceiling area light`, 2400 lumens, 130 x 120 cm source,
  attenuation radius **1000 cm**. The original room builder once requested 2000 cm,
  but the saved map's recorded 1000 cm value was preserved; the room was not rebuilt.
- Exposure: histogram (`AEM_HISTOGRAM`) pinned to EV100 4 by
  `min_brightness == max_brightness == 4` and bias 0. The auto-exposure method was
  not overridden to manual mode.
- The render targets are natively 1280x888; the top 1280x720 region is the visible
  view and the bottom 168 rows are black padding. Reference screenshots are 1280x720.
- Lumen warm-up draw counts are draw requests, not proof of temporal convergence.

## 7. Native omissions (honest scope)

The native attempt has Status `ok`, 1389 resources, 1284 captured resources,
105 descriptor-only resources, 928 passes and 2481 resource files. Known gaps:

- **Texture3D:** 78/78 descriptor-only. DumpGPU fills subresource extents only for
  Texture2D, so 3D texture bytes are absent; descriptors remain in
  `data/native/inventory.json`.
- **TextureCube:** 3/3 descriptor-only for the same reason.
- **SkipTracking:** 24 warning lines for `DefaultStructuredBuffer`,
  `DefaultByteAddressBuffer`, `DefaultBuffer` and `VirtualTexture_FeedbackBuffer`.
  Exact warnings are retained in `data/native/missing-coverage.json`.
- **RDG uniform-buffer payload mappings (LumenCardScene):** pass parameter payloads
  and `StructuresMetadata` are retained, but this export does not provide an exact
  mapping from every RDG uniform-buffer payload to the corresponding RDG resource.
  Source instrumentation is expected to close this in later data. No exhaustive
  mapping is claimed here.
- Non-RDG/never-viewed resources, shader-local registers, full RHI descriptor views,
  in-place modified versions (`RedumpInputs=0`) and driver-private state are outside
  this capture. Depth/stencil bytes are postprocessed staging bytes, not native RHI
  layout.

These omissions mean the package is a complete record of what this DumpGPU run
produced, **not** a claim of exhaustive computational coverage of the frame.

## 8. Reference limitations and interpretation

- The path-traced images are **finite references**, not ground truth. Their
  8-bounce truncation and the unoverridden engine path-intensity clamp
  (`PathTracing.MaxPathIntensity = 24.0`, visible in the recorded post-process
  defaults) can bias the result. No path-integral error metric is claimed.
- The editor viewport is SDR, so the captures are display-referred 8-bit PNGs.
  They are **not** linear EXR and not suitable for a transport-error comparison.
- The 256 spp image still contains Monte Carlo noise. 1024 spp reduces noise; it
  does not remove truncation or clamp bias.
- Decoded native figures are display normalizations of raw GPU data, not re-renders.
  HDR figures use a per-image 99th-percentile linear scale plus Reinhard
  `c/(1+c)` and are not radiometrically comparable to the SDR PNGs without the raw
  files. The raw files are external to this package.
- Lumen temporal state was warmed by draw requests, not proven converged.

## 9. Hash and size checks

`file-list.json` records `source`, portable `destination`, `sha256` and `size` for
every packaged file. `file-list.json` itself cannot list its own hash (self-reference);
the verifier checks that every other file is accounted for. To check a single figure
against its manifest:

```powershell
Get-FileHash figures/reference/lumen-lit.png -Algorithm SHA256
```

Expected value: `6b9c1132fc882d65812f4c769be0e14dc5ce82af0bb5e3b61178a7d5448e5076`.
