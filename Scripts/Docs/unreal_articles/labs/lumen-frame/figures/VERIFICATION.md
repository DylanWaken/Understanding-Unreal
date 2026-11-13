# Verification result - graph 0001

Commands run (temporary package; published outputs copied to
`Docs/Unreal/assets/lumen-v2/buffers/`):

```powershell
$out = "$env:TEMP\Lumen-Reading-Revision\build"
New-Item -ItemType Directory -Force "$out\evidence" | Out-Null
Copy-Item figures\evidence\* "$out\evidence" -Recurse
python figures\decode_figures.py --capture D:\UnrealEngine\UnrealUnderstandingCapture\capture\buffers\gpudump\binary-20261112-153914 --report "$out\evidence" --run "$out\evidence\run.json" --graph 0001 --output $out
python figures\verify_figures.py --capture D:\UnrealEngine\UnrealUnderstandingCapture\capture\buffers\gpudump\binary-20261112-153914 --output $out
python figures\verify_figures.py --capture D:\UnrealEngine\UnrealUnderstandingCapture\capture\buffers\gpudump\binary-20261112-153914 --output Docs\Unreal\assets\lumen-v2\buffers
```

Verifier result: **86/86 checks passed** (34 figures) on both the temporary
package and the published package. Selected graph `0001`, camera View
`00010000022880566180`, sha256
`0ce9aefbd724dd7861e95e81a389b12d19a5a62ca0ae534b72d06b2b288c6944`.
The verifier resolves `capture.report = evidence` relative to the output
directory and re-hashes every displayed payload.

## Vector typography

- all 34 figures publish `svg/<slug>.svg` with the same slug as `png/<slug>.png`;
- each SVG embeds exactly one lossless PNG data image and contains real SVG
  `<text>` (400 text elements across the set; 14 px body, 22 px titles);
- the verifier parses every SVG, checks the embedded `data:image/png;base64`
  image bytes against the retained `image` record's size and sha256, the
  `<text>` element count, the minimum/maximum font size, and that each wrapped
  title line exists as vector text;
- the gallery's 34 `<img>` sources are SVG, every per-figure slug anchor remains,
  and every figure retains a `#resource/<id>` link to the full frame explorer;
- an independent system-font measurement of all 400 text elements reported zero
  out-of-canvas lines and zero overlapping line boxes.

Scientific fields are unchanged: a recursive comparison of the previous and new
`figure-manifest.json` found no changes outside `image`, the new `vector_image`
record, and the reserved display-band heights (`display.content_rect_xywh`),
which are presentation-only.

## Card capture

- selected exact pass: **85**, pointer `000100000225fbd7d6e0`, event
  `CardPage Commands:1`;
- own View: content `000100000225fbd7d690`, rect `[320,64,64,64]`, size
  `[64,64,1/64,1/64]`;
- four card atlases all exact version `000100000225fbd7d6e0` / pass 85;
- mask: exact rectangle AND `Target1.a > 0.5`; common count **4096/4096**;
- byte diff vs pass 84: albedo/normal/depth **4096 changed inside, 0 outside**;
  emissive unchanged (all valid emissive texels are zero);
- full atlas explorer: 37 current-frame rects, union **31424** texels; 148166 raw
  non-zero Target1.a bytes outside that union are stale/previous-frame data,
  **0** exceed the 0.5 validity threshold and all are masked navy;
- clear provenance: `ClearCardCapture` pass 48.

## Surface cache

- `Lumen.PageBuffer` pass 96, 1024 slots, **42 mapped pages**;
- mapped test `SizeInTexels.x/y > 0`; physical rect from
  `PhysicalAtlasUVRect * 4096 + 0.5`;
- bbox `[0,0,1088,128]`, crop `[0,0,1104,144]`, union **35072** mapped texels
  (123904 off-page texels masked);
- material panels all report 35072 common texels and share the exact 1:1 mask;
- scene lighting panels use the same 35072 mask; resampled card panels use the
  exact 37-rect union **31424**, excluding green clear/stale content.

## Preservation

The 30 other graph-0001 PNGs, including the exact pass-167
`screen-probe-trace-hit`, are byte-identical to the prior
`Temp/UnrealUnderstanding-Figures-Graph0001/png` output. Only the three
intended figures changed (`card-capture-layers`, `surface-cache-material-layers`,
`surface-cache-lighting`); `card-capture-atlas` is the new full-atlas explorer.
