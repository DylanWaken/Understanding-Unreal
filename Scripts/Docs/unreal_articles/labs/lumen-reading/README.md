# Lumen reading diagrams

This lab generates the explanatory SVGs used by the Lumen reading revision. The
output is published under `Docs/Unreal/assets/lumen-reading/`.

## Run

```powershell
python Scripts/Docs/unreal_articles/labs/lumen-reading/generate.py
python Scripts/Docs/unreal_articles/labs/lumen-reading/generate.py --check
```

`--check` verifies the checked-in SVG text, the clean CPU-reference PNG, and the
manifest byte-for-byte. The script uses only the Python standard library and the
original `labs/lumen-pathtracer/reference/path_tracer.py` module for the CPU
reference tone map.

## What is generated

| file | concept | real evidence paired in the prose |
| --- | --- | --- |
| `diagrams/solid-angle-sampling.svg` | area measure, solid angle, projected solid angle, two cosines | matched Lumen / path-traced pair (convergence comparison, not sample directions) |
| `diagrams/sampling-mis.svg` | light sampling vs BSDF sampling, MIS partition and support | M27 / M28 numeric example; no new numeric claim |
| `diagrams/surface-cache-lookup.svg` | hit -> card space -> card UV -> page table -> physical atlas -> four taps | `replays/surface-cache/cache-texel-replay.json` page 35 |
| `diagrams/screen-probe-angular.svg` | 16-pixel probe grid, equal-area square map, cone, compacted fallback dispatch | captured probe radiance / lighting-PDF / trace-hit buffers |
| `diagrams/radiance-cache.svg` | clipmap cell, eight trilinear corners, octahedral direction map | `replays/world-cache/world-cache-replay.json` cell `[24,30,25]` |
| `diagrams/temporal-history.svg` | reprojection, 2x2 depth/normal tests, count-dependent blend, disocclusion | source-backed temporal constants and the captured probe output |
| `diagrams/deferred-direct-cached.svg` | G-buffer parameters vs direct and cached-indirect path families | captured metallic / roughness / specular GBuffer triplet |
| `cpu-reference.svg` | clean educational CPU render reconstructed from the linear PFM | original PFM, original PNG check, path-tracer manifest |

Every schematic includes `data-schematic="true"`, a `<title>`, a `<desc>`, a
visible `SCHEMATIC · NOT CAPTURED GEOMETRY` badge, and a footer saying it is not
captured geometry or a dump of GPU sample directions. The CPU-reference SVG is
the exception: `data-schematic="false"` because it is a real educational CPU
render, but it is explicitly labelled **NOT AN UNREAL CAPTURE**.

## CPU reference

The original PNG has a burned-in 5x7 bitmap footer in rows 165–199. This lab
reads the checked-in `reference_linear.pfm` bottom-up (PFM row order), reverses
it to top-down, and applies the original `path_tracer.py` `tone_map` at exposure
0.6. It writes a clean 280x200 PNG, embeds that lossless PNG in the SVG, and
draws the caption with native SVG text.

The generator verifies that the reconstructed image pixels match the original
PNG rows 0–164 exactly. It does not modify the frozen PFM, PNG, path tracer, or
their hashes.

## Determinism

The manifest records the SHA-256 of the generator and of every published file.
There is no randomness, clock, locale, or font rasterization in the output. SVG
text is real text; only the educational CPU render is a raster payload.
