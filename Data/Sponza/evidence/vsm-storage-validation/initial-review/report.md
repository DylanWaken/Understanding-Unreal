# Independent review — Sponza VSM storage, pass 840

**Verdict: fail (one blocking published native-field/provenance error; all selected-address, depth, and figure-pixel checks pass).**

I read the raw DumpGPU package, the pinned UE5 source, and the published JSON/NPZ/PNGs. I did **not** import or execute `tools/decode_vsm_storage.py`, did not run a GPU workload, and wrote only this `review/` directory. The standalone verifier is `review/verify_vsm_storage.py`; its machine-readable output is `review/checks.json` (20 PASS, 1 FAIL).

## Inputs and selected pass

- Raw capture: `D:\UnrealUnderstanding-Sponza\output\gpu-dump\raw\sponza-20261115-083654` (read-only).
- Presented graph: prefix `0004`; selected pass **840** `VirtualShadowMapProjection(Input:GBuffer)`, pointer `00040000020c16c43fb0`.
- Selected page-table/flags/pool version key: `00040000020c16c43fb0`; projection data version `00040000020bbf9a2350` (pass 768 ScatterUpload); metadata version `00040000020ba0684e78` (pass 810 SelectPagesToPostProcess, latest captured ≤ 840).
- Inputs checked against the report: `receivers.json` 64,940 B / `8ab5394b…0ca63`, `orientation.json` 59,786 B / `4c89765e…f77f`; archive `index.json` 990,927 B / `cab06a75…365088`.
- Pass 840 has exactly one output, `00040000020c16c43a30.mip0`. Page table, PageFlags, pool, and projection data are its **InputResources**; the version suffix `…c16c43fb0` is the capture/version key at the consuming pass, not proof that pass 840 wrote them.

## Acceptance check 1 — selected page mappings and depth bytes reproduce: yes

### Independently reconstructed addresses

All values below come from native projection bytes, the source clipmap/page arithmetic, the native page table, and a seek/read of the native pool word—not from producer decoder functions.

| case | selected VSM | shadow UV | virtual page / local | page-table texel | page entry | physical page | physical texel | raw depth word | float depth | metadata (pass-810 snapshot) |
|---|---|---|---|---|---|---|---|---|---|---|
| A | 8196 | (0.4938309314, 0.5134112716) | (63,65) / (26,91) | (703,65) | `0xc000504f` | (79,20) | (10138,2651) | `0x3efff6f3` | 0.4999309480190277 | VSM 8196, mip 0, page (63,65) |
| B | 8196 | (0.5066635216, 0.4944383004) | (64,63) / (109,36) | (704,63) | `0xc0001017` | (23,4) | (3053,548) | `0x3f000db7` | 0.5002092719078064 | VSM 8196, mip 0, page (64,63) |
| C | 8197 | (0.4582092990, 0.4722011310) | (58,60) / (83,56) | (698,60) | `0xc0005033` | (51,20) | (6611,2616) | `0x3f000b8c` | 0.500176191329956 | VSM 8196, mip 0, page (58,60) |
| S | 8196 | — | (59,60) / (0,0) | (699,60) | `0xc0006c32` | (50,27) | (6400,3456) | `0x3f000bce` | 0.5001801252365112 | VSM 8196, mip 0, page (59,60) |

- A/B reproduce the producer record exactly: max shadow-UV error **0.0**, max receiver-Z error **0.0**, max depth error **0.0**; the page entry, physical texel, raw word, and metadata owner all match.
- C is an independent extra selection. Its page is shared with the selected-8196 set: the published compact-tile-40 raw word at the same local texel is also `0x3f000b8c` (1,056,967,564). This is a real cross-level page sharing, and the pass-810 metadata still names 8196.
- S is the highest-physical-index page among the 99 native render-valid 8196 entries; its native raw word equals the published NPZ word for tile 98.

Source basis: `VirtualShadowMapProjectionDirectional.ush:38-70` (clipmap index), `VirtualShadowMapProjectionCommon.ush:262-303` (page lookup), `VirtualShadowMapPageAccessCommon.ush:140-145` (FullId/block), `:223-226` (physical index), `:237-287` (page-entry bits), `:305-330` (physical texel and `& 127` local).

### All 99 selected pages and the whole selected block

- 16,384 page-table entries: **0** decode/re-encode mismatches; **0** mismatches against the published NPZ entries; **0** mismatches against the published PageFlags words.
- Counts: 99 render-valid, 16,285 LOD fallback, 0 unmapped, 0 secondary. LOD histogram `{0:99, 1:116, 5:3881, 6:12288}`; PageFlags low-6 categories `{0x0:16216, 0x1:69, 0x1b:99}` (168 nonzero).
- All 99 published 128×128 pages reproduce the native pool exactly: 1,622,016 values, **max absolute depth error 0.0**, **0** raw-bit mismatches, **0** compact-mean mismatches, **0** compact-RGB mismatches, **99/99** metadata owner/mip/page matches against the pass-810 snapshot.
- Native aggregate: count 1,622,016, finite 1,622,016, zeros 0, min 0.4998985826969147, max 0.5002386569976807, mean 0.5000768603328992, std 0.00012547811487161463, p01 0.4999055564403534, p50 0.5001034140586853, p99 0.5002312064170837. These are bit-for-bit the published `counts.depth_stats`.
- Global page table: 243 unique referenced physical pages; occupancy partition `{free:3757, other-current:144, selected-current:99, stale:96}`; metadata state `{free:3757, current:243, stale:96}`. The exact 4096-entry rasters match the published NPZ; both selected A/B `PageFlags` words are `0x1b`.

## Acceptance check 2 — figures and valid/invalid provenance: figures yes, provenance no

### Figures (all exact-pixel checks pass)

- **F1** `vsm_pool_occupancy.png` 1024×988: top occupancy panel 0 mismatches; bottom metadata-state panel 0 mismatches; middle 99-tile depth atlas 405,504 interior pixels checked, 0 mismatches. Free `(62,62,74)`, other-current `(55,115,220)`, selected-current `(40,205,255)`, stale `(215,78,62)` are distinct categorical colors and are not depth-ramp colors.
- **F2** `vsm_page_table_sun.png` 1552×512: full LOD panel 262,144 pixels, 0 mismatches; 840 zoom cells in each of the identity and PageFlags panels, 0 mismatches; A/B white outlines present on both zoom panels. LOD fallback is dark, active 99 pages are the declared LOD colors, and the PageFlags panel uses the correct low-6-bit `h0` word.
- **F3** `vsm_depth_pages.png` 784×520: 374,482 non-marker pixels checked across the six 256×256 tiles, 0 mismatches. The only non-ramp pixels are the intentional 81-pixel white A/B crosses (`f3_max_channel_diff_including_markers=230`). Tiles are 3 columns × 2 rows, 256 px, 2× nearest scale, 8 px gutters.
- Display scale: fixed device-Z `[0.4998, 0.5003]` brackets the data (margins 9.86e-5 / 6.13e-5); `t=(z-lo)/(hi-lo)` maps the data to t∈[0.19717, 0.87731]. Recomputing the declared ramp at that t reproduces every non-marker pixel.
- All figures nonblank: stds 58.05 / 72.61 / 74.20 and 530 / 143 / 73 unique RGB colors (pool/table/depth figures).

### Provenance

- 29/29 source ranges recompute to the declared UTF8-LF `rangeSHA` against the pinned engine; all 12 unique source-file SHA-256s match. This includes `VirtualShadowMapDefinitions.h:12-38,147-155`, the page-entry packing comments in `VirtualShadowMapPageAccessCommon.ush:237-287`, and the uniform field order in `VirtualShadowMapArray.h:157-228`.
- Published manifest: 8/8 files match byte size and SHA-256. All 13 selected operand files match the JSON hashes. The 10 texture readbacks are `Dumped` with `FileExists` and `FileSizeMatches` true.
- The metadata caveat is real and is documented in the report: pass 840's `PhysicalPageMetaData` binding is `Null`; the selected metadata is the latest captured version at/before 840 (pass 810). Six captured metadata versions exist; none is later than 810 and ≤840.

## Blocking issue

### Published PageFlags h1..h7 hierarchy is wrong

`vsm_storage.json` says:

> `"uint32 contains 8 H-mip fields of 6 bits; h0 = bits[0:5] = exact page flags, h7 = bits[42:47]"`

A 32-bit word cannot contain 8 × 6 = 48 bits. The source uses one 6-bit flag per PageFlags mip texture:

- `VirtualShadowMapPageAccessCommon.ush:28-30`: 6 bits per H-mip.
- `VirtualShadowMapPageManagement.usf:80-86`: `OutPageFlagMips_0` … `OutPageFlagMips_5`; the `OutPageFlagMips_6` declaration is commented out.
- `VirtualShadowMapPageManagement.usf:225-235`: `PROCESS_LEVEL(0)` … `PROCESS_LEVEL(5)` call `ProcessMipLevel(..., HMipLevel = _Level_ + 1)`, so these six outputs write **H1..H6** into PageFlags mip1..mip6. PageFlags mip0 is H0.

Consequences in the published artifact:

- `page_table_flags_hierarchy_h6` is `[8,128,128]`; H1..H7 are **all zero** (the selected mip0 words only carry `h0`).
- The correct parent H1..H6 values live in mip1..mip6 at `(tx>>k, ty>>k)`. They are nonzero for 212 / 320 / 512 / 1,024 / 4,096 / 16,384 selected-block texels respectively, and the published packed-word h1..h6 differs from them for exactly those counts.
- `publish/data/schema.json` describes the array as `"H0..H7 6-bit flag fields"`, and `source-ranges.json` says `"PageFlags.h0_h7": "8 x 6-bit fields; h0 = bits[0:5]"`; both are wrong. The captured chain is H0..H6; H7 is not captured.

The **h0 word and the PageFlags figure are correct**; all address/depth checks and the visible flag panel are unaffected. This is a published native-field/provenance error, not a depth or mapping error. Fix by removing the h1..h7 array/description, or publishing a 7-level H0..H6 array populated from mip1..mip6 parent texels and dropping the nonexistent H7.

## Non-blocking findings

1. **Producer column misattributes pass 840.** `report.md` lines 10-13 list PageTable mip0, PageFlags mip0..6, and both pool slices as “producer 840”. Pass 840's only output is `00040000020c16c43a30.mip0`; all four are inputs. The version key `…c16c43fb0` means “captured at pass 840”, not “written by pass 840”. Pass 790 `AllocateNewPageMappings` outputs the page table/flags/metadata, and pass 810 `SelectPagesToPostProcess` outputs metadata. Use “version key 840 / consumed at 840” or name the upstream writer.
2. **Declared replay bias formula is not the source formula (latent).** The producer computes `abs_level + base_ResolutionLodBias`, then floors. The shader first selects the initial level from the unbiased absolute level, then adds **that level's** `ResolutionLodBias`, then floors (`VirtualShadowMapProjectionCommon.ush:62-72`, `VirtualShadowMapProjectionDirectional.ush:43`). All 17 captured sun biases are exactly `0.6639266014099121`, so A/B/C are correct here; the replay would select the wrong level if a future capture has differing per-level biases.
3. **Metadata is a snapshot, not a live binding.** Pass 840 binds `PhysicalPageMetaData` to `Null`. The pass-810 snapshot is used for ownership/staleness categories. C shows why this matters: C selects level 8197, but its shared physical page (51,20) is owned by 8196 in the snapshot. The report's “owner identity was verified for all 99” is true against the snapshot, but should not be read as a live/unique current owner for arbitrary levels. The report's “pass-810 snapshot” limit is the right wording; keep it.

## How to re-run

```powershell
python review/verify_vsm_storage.py
```

The verifier reads the raw dump, engine, and producer outputs read-only and rewrites `review/checks.json`. It uses no producer decoder function, no GPU, and no repository/index mutation.

## Residual limits

- Only page-table mip 0 is used for the selected VSM; PageFlags mip1..6 are used here only to disprove the published packed-hierarchy claim. The chapter should describe H0..H6 across the mip chain, not h1..h7 packed in one word.
- The A/B/C selection replay covers clipmap selection and page-table/depth addressing; it does not execute SMRT ray directions, cone/bias terms, or per-lane shadow tests. C is an extra address check, not a third published receiver replay.
- The metadata partition is valid for the pass-810 snapshot and the captured frame; it is not a live pass-840 ownership proof.
