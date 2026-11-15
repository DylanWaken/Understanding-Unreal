# Independent review — VSM storage, pass 840

**Verdict: fail (two non-numerical correctness/provenance defects; the two acceptance checks below pass).**

I read the raw DumpGPU package, the pinned UE 5.8 source, and the published JSON/NPZ/PNGs. I did **not** import or execute `tools/decode_vsm_storage.py`, did not run a GPU workload, and wrote only this `review/` directory. The standalone verifier is `review/verify_vsm_storage.py`; its machine-readable output is `review/checks.json` (14 PASS, 2 FAIL).

- Raw capture: `D:\UnrealUnderstanding-Sponza\output\gpu-dump\raw\sponza-20261115-083654` (read-only).
- Published artifact: `output/` (report, JSON, NPZ, four PNGs).
- Pinned engine: `D:\UnrealEngine\Engine`.
- Inputs checked: `receivers.json` 64,940 B / `8ab5394b…0ca63`, `orientation.json` 59,786 B / `4c89765e…f77f`, frame-package `index.json` 990,927 B / `cab06a75…365088`.

## Acceptance check 1 — logical mip packing and terminology match pinned source: yes

Independently reconstructed from the source formula, not from producer methods:

| logical mip | base texel | dim pages | entries | render-valid | any-valid |
|---|---|---|---|---|---|
| 0 | [640, 0] | 128 | 16384 | 99 | 16384 |
| 1 | [640, 128] | 64 | 4096 | 0 | 0 |
| 2 | [704, 128] | 32 | 1024 | 0 | 0 |
| 3 | [736, 128] | 16 | 256 | 0 | 0 |
| 4 | [752, 128] | 8 | 64 | 0 | 0 |
| 5 | [760, 128] | 4 | 16 | 0 | 0 |
| 6 | [764, 128] | 2 | 4 | 0 | 0 |
| 7 | [766, 128] | 1 | 1 | 0 | 0 |

- `CalcLevelOffsets` independently reproduces the eight bases; all eight regions are inside the one 128×192 VSM block, with **0 overlapping texels**. Mips 1–7 contain **0 nonzero entries**; mip0 has 99 render-valid entries and 16,285 LOD-fallback entries. The PageTable descriptor is **NumMips=1**, so “one hardware mip containing logical mips 0–7” matches the source.
- PageFlags is a **separate 7-mip hierarchy**, not eight 6-bit fields in one uint32. The descriptor is NumMips=7; `OutPageFlagMips_0..5` write H1..H6 at `texel >> (m+1)`, and H7 is not generated. Independently OR-ing each 2×2 child group gives **0 mismatches** for H1..H6. Published nonzero counts `H0..H6 = 168, 212, 320, 512, 1024, 4096, 16384` match; H0 low-6 categories are `{0x0: 16216, 0x1: 69, 0x1b: 99}`. The old “bits[42:47]” / “8 × 6-bit fields” claim is absent from JSON, schema and source-ranges.
- All **31** cited source ranges recompute to their declared UTF8-LF `rangeSHA` against the pinned engine.

## Acceptance check 2 — mosaic native samples and original A/B mappings remain correct: yes

- **F4 mosaic:** `vsm_virtual_depth.png` is **1792×1536**, bbox **[55, 58, 68, 69]** (14×12 pages), scale **1.0**. All 99 selected pages are placed at their virtual page coordinates in ascending `virtual_address` order; 69 cells are categorical coarser-clipmap fallback and 0 are unmapped. An independent reconstruction from pool slice 0 reproduces **all 2,752,512 pixels (0 mismatches)** and the NPZ mask/page-category arrays exactly. Mask counts are 99×128² mapped, 69×128² fallback, 0 unmapped. The fixed range [0.4998, 0.5003] maps the data to t ∈ [0.197172, 0.877321], and the image is not vertically flipped.
- Four independent mosaic samples (all at local (26,91)):

| virtual addr | virtual page | physical page | raw depth | image RGB | ramp RGB |
|---|---|---|---|---|---|
| 7482 | [58,58] | [96,24] | 0.5001794696 | (221,183,66) | (221,183,66) |
| 7739 | [59,60] | [50,27] | 0.5001800060 | (221,183,67) | (221,183,67) |
| 8132 | [68,63] | [48,6] | 0.5002306700 | (236,213,138) | (236,213,138) |
| 8897 | [65,69] | [72,13] | 0.5000423193 | (20,144,149) | (20,144,149) |

- **F3 native pages:** all six tiles are bit-exact against pool slice 0; every non-marker pixel reproduces the declared [0.4998,0.5003] ramp at 2× nearest scale (0 mismatches). Four independent samples (tiles 0–3) match exactly. The only non-ramp pixels are the 162 white A/B cross pixels.
- **All 99 page records:** every published page reproduces the raw page-table entry, PageFlags H0 word, pass-810 metadata owner (VSM 8196, mip 0, page address), physical index/texel and 128×128 depth statistics. The published 99-page set equals the raw 99 render-valid entries; all 99 NPZ full pages are bit-exact.
- **A/B mapping:** independently recomputed from raw projection bytes, source clipmap selection, page table and pool:

| rcvr | index | UV | vpage/local | entry | phys page | raw depth | receiver Z | Δ | mask |
|---|---|---|---|---|---|---|---|---|---|
| A | 4 (8196) | (0.4938309314, 0.5134112716) | (63,65)/(26,91) | `0xc000504f` | (79,20) | `0x3efff6f3` = 0.4999309480 | 0.4999309666 | −1.8563333e−08 | 1.0 |
| B | 4 (8196) | (0.5066635216, 0.4944383004) | (64,63)/(109,36) | `0xc0001017` | (23,4) | `0x3f000db7` = 0.5002092719 | 0.4999378436 | +2.7142831e−04 | 0.0 |

  A/B stored depth, receiver Z, delta, UV, page-table texel/entry, physical texel, raw word and every shared prior count are identical to the earlier independent publication (`vsm-storage/output/.../vsm_storage.json`).

## Blocking findings

### 1. Receiver replay still uses the base level's `ResolutionLodBias`, not the source formula

`tools/decode_vsm_storage.py:806-808` computes

```python
biased = abs_level + float(base["resolution_lod_bias"])
base_clip = int(base["clipmap_level"])
```

and `:1838` sets `base = sun[0]` (VSM 8192). The shader first chooses the unbiased initial clipmap, then adds **that level's** `ResolutionLodBias`, then floors (`VirtualShadowMapProjectionCommon.ush:62-72`, `VirtualShadowMapProjectionDirectional.ush:42-43`). The report's “replay applies the captured clipmap-selection formula” is therefore not source-correct as written.

This is **latent for this capture**: all 17 sun biases are exactly `0.6639266014099121`, and A/B still resolve to index 4 / VSM 8196 with the correct bytes. It is nevertheless a real reusable-code defect and a false formula claim.

### 2. Pool slice1's “last writer 811” is a resource-level record, not a slice-1 write

The report table says:

> `PhysicalPagePool slice1 … last writer 811 MergeStaticPhysicalPagesIndirect`

The shader's `MergePhysicalPixel` writes only `OutPhysicalPagePool[uint3(PixelCoord, 0)]` and reads `OutPhysicalPagePool[uint3(PixelCoord, GetVirtualShadowMapStaticArrayIndex())]` (`VirtualShadowMapPhysicalPageManagement.usf:805-811`). Pass 811 is recorded as an output writer of both slices (`Passes.json`), but the recorded writer output used by the artifact is `00040000020c132ec290.[0].mip0`; the actual shader writes slice 0 only. The report's slice1 row therefore cannot be read as a slice-1 producer claim. Qualify it as “resource-level recorded writer 811; shader writes slice 0 only; no per-slice writer is recorded for slice1.”

## Non-blocking notes

- The metadata caveat is correct: 96 records with flags `0x285b` (ALLOCATED|UNREFERENCED|…) and `LastRequestedSceneFrameNumber 3` are published as *unreferenced*, not proven stale; free slots remain neutral gray.
- The decoder's `GetMappedClipmap` post-selection fallback (`SampleVirtualShadowMapClipmap` LODOffset walk) is not replayed. For A/B both entries have LODOffset 0, so this does not affect the published numbers; it remains a residual limitation for other pages.
- The report states “zero-valued depth pixels are categorically masked.” F1's compact atlas masks non-positive values, but F3/F4 use the ramp directly. The captured 99 pages contain 0 zero-valued pixels, so no visible pixel is affected.

## How to re-run

```powershell
python review/verify_vsm_storage.py
```

The verifier reads the raw dump, pinned engine and published artifact read-only, writes `review/checks.json`, uses no producer decoder function and no GPU. Current result: **14 PASS, 2 FAIL**.
