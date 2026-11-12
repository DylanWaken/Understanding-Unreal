# Verification result - graph 0001

Commands run:

```powershell
python figures\decode_figures.py --capture D:\UnrealEngine\UnrealUnderstandingCapture\capture\buffers\gpudump\binary-20261112-153914 --graph 0001 --output figures
python figures\verify_figures.py --capture D:\UnrealEngine\UnrealUnderstandingCapture\capture\buffers\gpudump\binary-20261112-153914 --output figures
```

Verifier result: **79/79 checks passed** (34 figures). Selected graph `0001`,
camera View `00010000022880566180`, sha256
`0ce9aefbd724dd7861e95e81a389b12d19a5a62ca0ae534b72d06b2b288c6944`.
The verifier resolves `capture.report = evidence` relative to `figures/` and
re-hashes every displayed payload.

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
