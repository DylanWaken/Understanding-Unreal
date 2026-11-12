# Final capture figures (graph 0001)

This directory is a self-contained, deterministic decode of the final DumpGPU
capture at:

```
D:\UnrealEngine\UnrealUnderstandingCapture\capture\buffers\gpudump\binary-20261112-153914
```

The published package is the **graph 0001** decode. No GPU, engine or source
work is performed. The raw dump is read-only.

## Run

```powershell
python figures\decode_figures.py --capture D:\UnrealEngine\UnrealUnderstandingCapture\capture\buffers\gpudump\binary-20261112-153914 --graph 0001 --output figures
python figures\verify_figures.py --capture D:\UnrealEngine\UnrealUnderstandingCapture\capture\buffers\gpudump\binary-20261112-153914 --output figures
```

`decode_figures.py` writes `png/*.png`, `figure-manifest.json`, `index.html` and
`inventory/{passes,resources,payloads,graph-index,summary}.json`.
`verify_figures.py` re-checks the manifest against the raw dump, the packaged
evidence manifest and the exact payload bytes; the final graph 0001 run reports
**79/79 checks passed**. Both scripts are deterministic: running the decoder
twice produces byte-identical figures and manifests.

The portable form is `--capture <dump> --graph 0001 --output <dir>`: by default
`--report` is `figures/evidence` and `--run` is `figures/evidence/run.json`, so
no external report/run tree is required. Relative report/run paths in the
manifest are resolved against `figures/` by the verifier.

## Packaged evidence (`figures/evidence/`)

Only the reports the decoder needs are packaged, copied from the same capture:

- `manifest.json` - payload relative paths and SHA-256 values (top-level dump
  path reduced to the dump basename);
- `scene-graphs.json` - pass-pointer -> View payload mapping and ViewRectMin /
  ViewSizeAndInvSize, used to recover exact capture rectangles;
- `report-summary.json` - capture status/frame/coverage summary;
- `run.json` - engine/RHI/frame metadata; absolute capture/engine/docs roots are
  replaced with `<capture>`, `<engine>`, `<docs>`, and `dump_directory` is the
  basename.

A scan of these four files finds no `C:\`, `D:\`, `Users`, `Administrator`,
`token`, `secret`, `password`, `api_key`, `authorization` or `Bearer` strings.
The payload hashes in `manifest.json` are unchanged, so the verifier still
checks every decoded payload byte-for-byte.

## Scene graph selection

The three final-capture graphs (`0001`, `0003`, `0004`) all contain a 1280x720
View payload. This package is explicitly generated with `--graph 0001`. Its
camera View content is `00010000022880566180` (sha256
`0ce9aefbd724dd7861e95e81a389b12d19a5a62ca0ae534b72d06b2b288c6944`, rect
`(0,0,0,0)`, size `(1280,720,...)`, max pass 288).

Every figure records its graph/view content and hash, exact resource id, payload
file/hash, version pass pointer, pass id and parent scopes.
`inventory/graph-index.json` and the "Root explorer" section of `index.html`
retain all three graphs with their resource and pass inventories.

## Format policy

The decoder is driven by the exact `StoredFormat` and actual bytes per texel from
`Base/SubresourceLayouts.json`, not by `NativeFormat` or an assumed half layout.

- `PF_FloatRGBA` is RGBA16F (8 B/texel) for the surface-cache atlases.
- `PF_FloatRGB` is `PF_FloatR11G11B10` (4 B/texel), decoded 11/11/10.
- `PF_A2B10G10R10` is decoded as native 10/10/10/2 with A preserved.
- `PF_B8G8R8A8` memory order B,G,R,A is respected; GBufferA/B/C/D fields are
  decoded per shader channel.
- Compound fields (GBuffer A/B, VSM mask nibbles, packed page table) are decoded
  per field; a combined colour is never used to hide a field.

## Card capture: one exact live pass

`card-capture-layers` no longer reads the whole 512x512 atlas. For each
`CardPage Commands:1` pass in graph 0001 it recovers that pass's own View
`ViewRectMin` and `ViewSizeAndInvSize` from the pass-pointer -> View payload
mapping, and requires the four card atlases to have a payload version whose
version pointer is exactly that pass pointer.

The selected pass is the final live card of the frame:

- pass **85**, pointer `000100000225fbd7d6e0`, event `CardPage Commands:1`;
- View content `000100000225fbd7d690`, rect **`[320,64,64,64]`**, size
  `[64,64,1/64,1/64]`;
- the four resources all resolve to version `000100000225fbd7d6e0` / pass 85:
  `Lumen.CardCaptureAlbedoAtlas`, `Lumen.CardCaptureNormalAtlas`,
  `Lumen.CardCaptureEmissiveAtlas`, `Lumen.CardCaptureDepthStencilAtlas`.

The shared mask is exactly `rect membership AND Target1.a > 0.5`, the
source-proved card validity flag (`LumenCardBasePass.ush:145-147`,
`LumenSurfaceCache.usf:64`). The figure uses an 80x80 crop (8-texel border) at
5x nearest display, so the border is categorical navy and is never decoded as
this pass's material. The common count is **4096/6400** (the 4096-texel card;
2304 border texels).

A byte diff against the immediately preceding atlas version (pass 84) proves the
written update rectangle: albedo 4096, normal 4096 and depth 4096 changed texels
inside the rect, and **0/0/0 outside**. Emissive is unchanged because every valid
card emissive texel in this capture is exactly zero.

The full 512x512 atlas is retained as `card-capture-atlas`. Its mask is the union
of all **37** exact current-frame CardPage rectangles (**31424** texels), never a
future rectangle. Outside that union the raw atlas still contains **148166**
non-zero Target1.a bytes from stale/previous-frame content, but **0** of them
exceed the 0.5 validity threshold; all are categorical navy in the explorer
figure and are excluded from the material figure. Clear provenance is
`ClearCardCapture` pass 48, pointer `000100000228e8dd3730`.

## Surface cache: proven mapped page occupancy

The material and lighting figures use the GPU `Lumen.PageBuffer`
(`resource.lumen-card-page-data`) at pass 96 (`Build cards update list`):

- 1024 page slots, **42 mapped pages**;
- mapped test `SizeInTexels.x > 0 && SizeInTexels.y > 0`;
- `PhysicalAtlasCoord = uint2(PhysicalAtlasUVRect.xy * 4096 + 0.5)`, max corner
  from `.zw`; page rects are non-overlapping and cover a bbox of
  `[0,0,1088,128]`;
- the shared crop is the bbox + 16 texels, `[0,0,1104,144]`, and the union of
  mapped page rectangles is **35072** texels (123904 off-page texels masked).

All four material panels use that one exact 1:1 mask. No panel has its own
nonzero test. Emissive is exactly zero on every mapped texel; surface depth
value `1.0` is the `EncodeSurfaceCacheDepth` invalid sentinel and is categorical
blue.

`surface-cache-lighting` uses the same 35072-texel mapped-page mask for the two
4096x4096 Scene panels. The two 512x512 resampled card-capture lighting panels
are a different domain: they use the union of the 37 exact current-frame CardPage
rectangles (**31424** texels), which excludes the green clear sentinel and stale
atlas content. Per-panel log10(1+c) display uses each panel's p99; it is not a
cross-panel radiometric comparison.

## Figure families

- GBufferA world normal + 2-bit per-object field
- GBufferB metallic/specular/roughness + shading-model and selective-mask nibbles
- GBufferC sRGB base colour
- SceneDepthZ raw reversed-Z and source-transform linear view depth; stencil
- SceneColor before/after with one shared exposure, Reinhard and sRGB OETF
- Lumen surface-cache material layers and direct/indirect/final lighting on
  proven mapped-page occupancy
- Lumen exact-pass card-capture material/depth layers plus full-atlas explorer
- World radiance-cache indirection representative slices (pass 167 trace input)
- Reflection trace radiance, hit/miss distance, resolved radiance and depth
- Screen-probe depth, octahedral world normal, PDF, hit distance/ray flags,
  radiance, filtered radiance, diffuse and rough specular
- VSM 16 packed mask nibbles and packed page-table state

## Invalid values

Invalid values are categorical and source-proved, never folded into a valid
zero:

- card atlas: `Target1.a <= 0.5` or outside the exact pass rectangle = navy;
- surface cache: unmapped PageBuffer page = navy; depth `1.0` = blue invalid;
- reflection tiles: exact `INVALID_LIGHTING = (65024,65024,64512)` from
  `StochasticLightingCommon.ush:189-193`;
- reflection trace tiles: signed `EncodeRayDistance` from
  `LumenReflectionCommon.ush:89-99`;
- world radiance-cache pages: `0xFFFFFFFF` from
  `LumenRadianceCacheInterpolation.ush:302-317`;
- VSM page table: `0` invalid / `0xFFFFFFFF` dummy from
  `VirtualShadowMapPageAccessCommon.ush:247-286`;
- screen-probe depth/normal: invalid probe on non-positive `asfloat(uint)` depth;
- PDF: normalized `[0,1]`; out-of-range halfs are categorical invalid.

Each figure's `invalid.rule`, `invalid.semantics`, `invalid.source`, legend and
the HTML details state this explicitly.

## Unavailable / not decoded

`figure-manifest.json.unavailable` enumerates concrete omissions with evidence,
including GBufferC.A ambiguity, GBufferD all-zero custom data, absent
GBufferE/velocity, Lumen.Cards / Lumen.MeshCards card table semantics, VSM
PageFlags, the SH BRDF PDF buffer, screen-probe interpolation weights, absolute
photometric calibration, and the baseline VSM debug screenshots.

## Notes on display

HDR figures use a declared per-figure transform in `display.transform` (except
the SceneColor pair, which shares one transform). They are displays of raw
payloads, not re-renders and not radiometric comparisons. Screen-sized targets
are natively 1280x888; screen figures crop the exact 1280x720 view rect. Atlases
are shown at their own extents/crops and record those crops.
