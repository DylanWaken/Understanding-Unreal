# VSM storage decode: sponza-20261115-083654 (presented graph, native0004)

Selected pass: **840 VirtualShadowMapProjection(Input:GBuffer)** (`00040000020c16c43fb0`), graph prefix `0004`. Earlier scene invocations are not used.

## Captured operands

| operand | resource | version | capture snapshot / last resource output binding | file | bytes | sha256 |
|---|---|---|---|---|---|---|
| ProjectionData | `00040000020bbf9a1e30` | `00040000020bbf9a2350` | 768 ScatterUpload (Resource: Shadow.Virtual.ProjectionData) | `Resources/00040000020bbf9a1e30.v00040000020bbf9a2350.bin` | 3020912 | `1b55828b5387b5aa…` |
| PageTable hardware mip0 (logical mips 0..7) | `00040000020bb879e688` | `00040000020c16c43fb0` | pass 840 consumer snapshot; last resource output binding 792 PropagateMappedMips | `Resources/00040000020bb879e688.mip0.v00040000020c16c43fb0.bin` | 6291456 | `0c2831883c84084d…` |
| PageFlags hardware mips 0..6 (H0..H6) | `00040000020bb879e950` | `00040000020c16c43fb0` | pass 840 consumer snapshot; mip0 writer 790 AllocateNewPageMappings, mips1..6 writer 791 GenerateHierarchicalPageFlags | 7 files | 8388096 | see JSON |
| PhysicalPagePool slice0 | `00040000020c132ec290` | `00040000020c16c43fb0` | pass 840 consumer snapshot; last resource output binding 811 MergeStaticPhysicalPagesIndirect | `Resources/00040000020c132ec290.[0].mip0.v00040000020c16c43fb0.bin` | 268435456 | `2620936937482b39…` |
| PhysicalPagePool slice1 | `00040000020c132ec290` | `00040000020c16c43fb0` | pass 840 consumer snapshot; last resource output binding 811 MergeStaticPhysicalPagesIndirect | `Resources/00040000020c132ec290.[1].mip0.v00040000020c16c43fb0.bin` | 268435456 | `dd12e922cefe0c1c…` |
| PhysicalPageMetaData | `00040000020ba06c1680` | `00040000020ba0684e78` | 810 SelectPagesToPostProcess (snapshot before pass 840) | `Resources/00040000020ba06c1680.v00040000020ba0684e78.bin` | 98304 | `d06fc26a1d1ebbda…` |
| Uniform block | `00040000020910406420` | — | captured at pass 840 | `Structures/00040000020910406420.bin` | 368 | `8dadf8b4398bc1d5…` |

Pass 840 writes only its shadow-mask output; the page table, PageFlags and pool are its inputs, versioned at the pass-840 capture boundary. The table names the last recorded resource-level output bindings; a whole-resource UAV binding does not prove a write to every slice. In particular, pass 811 merges static depth into slice 0 and does not overwrite the static slice 1.

- input `receivers`: `receivers.json` 64940 B sha256 `8ab5394b94f96df215b452f2427c1fce0596e81c86750bf74cb880f897b0ca63`
- input `view`: `orientation.json` 59786 B sha256 `4c89765ecc9c06a2bf58974ec299469e8e9f6144095b242c6d93d33095c2f77f`
- archive package `sponza-20261115-083654`: index.json 990927 B sha256 `cab06a753e1380a4ffca0f1c2810572f005beaba6e66d2605eb13259f4365088`

## Captured constants (uniform block, pass 840)

| field | value |
|---|---|
| NumFullShadowMaps | `17` |
| NumSinglePageShadowMaps | `0` |
| MaxPhysicalPages | `4096` |
| NumShadowMapSlots | `8209` |
| StaticCachedArrayIndex | `1` |
| PhysicalPageRowMask | `127` |
| PhysicalPageRowShift | `7` |
| PhysicalPoolSize | `[16384, 4096]` |
| PhysicalPoolSizePages | `[128, 32]` |
| PageTableRowShift | `6` |
| PageTableRowMask | `63` |
| SceneFrameNumber | `904` |

Page size **128×128**; physical pool **16384×4096** texels = **128×32** pages; page table **8192×192**; projection stride **368 B**, **8209** records.

## Sun VSMs

Base id `8192` (clip level 6), 17 contiguous levels 6..22, direction `[-0.2120121568441391, 0.1484525054693222, 0.9659258127212524]`. Selected VSM `8196` (clip level 10, level index 4), page-table block base texel `[640, 0]`.

## Selected-VSM page table (hardware mip0, logical mips 0..7)

- page-table hardware descriptor NumMips = `1`; that one hardware mip packs all eight logical shadow-map mips in the VSM 128×192 block
- hardware mip0 exact-page entries 16384: render-valid 99, coarser-clipmap LOD fallback 16285, unmapped 0, secondary 0

| logical mip | base texel | dim pages | entries | render-valid | any-valid | Empty | PageFlags H0 nonzero |
|---|---|---|---|---|---|---|---|
| 0 | [640, 0] | 128 | 16384 | 99 | 16384 | no | 168 |
| 1 | [640, 128] | 64 | 4096 | 0 | 0 | yes | 0 |
| 2 | [704, 128] | 32 | 1024 | 0 | 0 | yes | 0 |
| 3 | [736, 128] | 16 | 256 | 0 | 0 | yes | 0 |
| 4 | [752, 128] | 8 | 64 | 0 | 0 | yes | 0 |
| 5 | [760, 128] | 4 | 16 | 0 | 0 | yes | 0 |
| 6 | [764, 128] | 2 | 4 | 0 | 0 | yes | 0 |
| 7 | [766, 128] | 1 | 1 | 0 | 0 | yes | 0 |

- LODOffset semantics: for this directional clipmap the field selects a coarser clipmap level and the sampled mip stays 0; for local lights the same field can select a logical page-table mip.
- LODOffset histogram (logical mip0): `{0: 99, 1: 116, 5: 3881, 6: 12288}`
- PageFlags H0 nonzero 168; low6 categories `{'0x0': 16216, '0x1': 69, '0x1b': 99}`
- PageFlags hardware mips 0..6 are the separate H0..H6 hierarchy (mip m parent at texel >> m); H7 is not generated. They are not the eight logical mips packed in PageTable hardware mip0.
- Occupancy categories (4096 physical pages): `{'0': 3757, '2': 144, '4': 96, '1': 99}`
- Metadata state categories: `{'0': 3757, '1': 243, '2': 96}`
- Physical pages referenced by page-table mip0: 243
- Metadata records allocated but unreferenced in this snapshot: 96 (earlier last-requested frame 3); age alone does not prove them stale or invalid, and free/unallocated slots stay neutral gray.

Depth range over all 1622016 values of the 99 render-valid pages: min 0.499898583, max 0.500238657, mean 0.500076860, p01 0.499905556, p99 0.500231206, zeros 0.

Display range for the selected projection: device Z **[0.49980000, 0.50030000]**, ramp navy→blue→teal→amber→white; t = (z−lo)/(hi−lo) is the invertible display coordinate. Invalid and masked texels are categorical, not depth.

## Receiver declared replay

| rcvr | mask | clipmap | UV | vpage | entry | phys page | phys texel | raw depth | depth | receiver Z | Δ | occ dist cm |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 1.0 | 8196 | (0.49383,0.51341) | [63, 65] | `0xc000504f` | [79, 20] | [10138, 2651] | `0x3efff6f3` | 0.499930948 | 0.499930967 | -1.856e-08 | 0.000 |
| B | 0.0 | 8196 | (0.50666,0.49444) | [64, 63] | `0xc0001017` | [23, 4] | [3053, 548] | `0x3f000db7` | 0.500209272 | 0.499937844 | +2.714e-04 | 1111.770 |

The replay applies the captured clipmap-selection formula and page-table arithmetic only. SMRT ray directions, slope/normal bias, texel dither and per-lane hits are shader-local and are not replayed, and the captured mask is reported but not reproduced. B's stored depth is greater than its receiver depth (occluder closer to the light), consistent with the captured shadowed mask. A's stored depth nearly equals its receiver projection (Δ = -1.856e-08, float32 roundoff; not exactly equal). The replay is a storage-address check, not a full shadow-mask explanation. The occluder-distance column applies the source clamp max(1e-6 cm, (receiverZ-storedZ)/m33); A's unclamped value is slightly negative (no occluder), B's is positive.

## Figures

- `publish/figures/vsm_pool_occupancy.png` (1024×988): physical_pool_occupancy_and_depth
- `publish/figures/vsm_page_table_sun.png` (1552×512): sun_virtual_page_table_mip0
- `publish/figures/vsm_depth_pages.png` (784×520): native_depth_page_closeups
- `publish/figures/vsm_virtual_depth.png` (1792×1536): contiguous_virtual_depth_mosaic

Figure 3 tile order is listed in `publish/data/vsm_storage.json` → `figures.vsm_depth_pages.tile_order` (row-major, 3 columns × 2 rows, 256 px tiles, 8 px gutters, 2× nearest scale, white cross = replay texel on A/B).
Figure 4 (`vsm_virtual_depth.png`) reassembles all 99 selected VSM-8196 mip0 pages at their virtual page coordinates, cropped to the active bbox [55, 58, 68, 69] (14×12 pages, 1792×1536 px, uniform scale 1.000000). It uses the same fixed device-Z range [0.4998, 0.5003]; coarser-clipmap fallback (69 page cells) and unmapped (0 page cells) positions are categorical, and no text is burned in. Page order, bbox, scale and mask are stored in JSON/NPZ.

## Limits

- PageTable hardware mip0 packs all eight logical shadow-map mips 0..7 in each VSM 128×192 block. For selected VSM 8196, logical mip0 holds 99 render-valid pages and logical mips 1..7 are present but empty (all-zero entries); no logical page-table mip is missing from the capture.
- PageFlags hardware mips 0..6 are a separate H0..H6 hierarchy (mip m parent at texel >> m); H7 is not generated. This hierarchy is not the eight logical mips packed in PageTable hardware mip0.
- The receiver chain is a declared replay, not an executed shader-local SMRT trace.
- The page-table, PageFlags and pool files are pass-840 consumer snapshots versioned at the capture boundary; their last resource-level output bindings are earlier passes (page table 792, pool 811, PageFlags mip0 790 / mips1-6 791). Physical-page metadata is the pass-810 snapshot before the pass-840 consumer.
- The 96 metadata records with flags 0x285b and LastRequestedSceneFrameNumber 3 are allocated slots not referenced in this snapshot; age alone does not prove them stale or invalid. Free/unallocated slots remain neutral gray. The 99 selected pages have no zero-valued depths; the compact atlas masks nonpositive values, while the native-page and mosaic views apply the declared ramp directly.
- Full pool bytes (2 × 256 MiB) stay in the raw archive; all 99 current selected pages are duplicated as full 128×128 float32 + raw bits, plus the 64×64 compact thumbnails and the virtual-address mosaic.
- Depth across different projections/clipmap levels is not globally comparable; one fixed range is used for selected VSM 8196.
- AllocatedPageRectBounds/PageRectBounds, PageReceiverMasks, HZBPhysicalPagePool, NextVirtualShadowMapData and StatsBuffer are captured but not used for these point storage queries.
