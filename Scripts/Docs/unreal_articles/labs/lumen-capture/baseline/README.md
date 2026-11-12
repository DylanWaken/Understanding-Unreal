# Unreal Lumen and path-tracer evidence package

This is a portable, copy-ready evidence package for the existing `/Game/Labs/LumenRoom`
experiment. It contains the verified reference images, the VSM and decoded native
figures, the complete native DumpGPU resource/pass inventories and known-gap records,
and a minimal Unreal project plus capture/analysis scripts. It does **not** contain
the 14.5 GB raw `Resources/` binaries.

Start with:

- `reproduction.md` — package scope, exact reproduction commands and limitations.
- `package-report.md` — verification result, copied-artifact counts and native omissions.
- `file-list.json` — SHA-256 and size for every packaged file (except the list itself).
- `data/native/native-dump-summary.json` — native attempt counts and known gaps.
- `data/native/figure-to-native-map.json` — figure-to-resource/pass/frame mapping.
- `data/reference/reference-image-map.json` — reference image/scene/sample mapping.

Quick check (no Unreal, no GPU, no raw dump needed):

```powershell
python reproduce/scripts/verify_package.py
```

The package is evidence, not a claim of exhaustive computational coverage. The native
attempt is missing all Texture3D/TextureCube payloads, SkipTracking default buffers and
an exact RDG uniform-buffer payload mapping (including LumenCardScene). The path-traced
images are finite references: 8-bounce truncation and the unoverridden path-intensity
clamp can bias them. The SDR PNGs are display-referred 8-bit captures, not linear EXR
and not a transport-error metric.
