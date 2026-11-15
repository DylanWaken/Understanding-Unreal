# Final VSM integration review — corrected contracts
**Verdict: pass.** Both prior blocking findings are corrected in the current
repository files, and the published numeric arrays are exactly the independently
verified candidate. Read directly from `D:/UnrealUnderstanding` (not the
superseded `vsm-final` tool). No repository edits, GPU work, raw-capture decode,
99-page audit, whole-file hash loop, or producer-function import. Evidence:
`results/checks.json`.
## Acceptance check 1 — per-level bias and invalid UV: yes
`decode_vsm_storage.py:813-821` now follows the source order:
`unbiased_index = int(max(0, abs_level - base_clip))`; take that projection's
`resolution_lod_bias` (`proj[base.id + index]` when `index < remaining`,
otherwise the base bias); add it; then `index = max(0, floor(biased) - base_clip)`.
This matches `VirtualShadowMapProjectionCommon.ush:60-72` (unbiased index, then
that level's bias; base bias only out of range) and
`VirtualShadowMapProjectionDirectional.ush:38-43` (`floor` plus range check).
The optional DOF term (`Common.ush:54-59`) is explicitly excluded at
`decode_vsm_storage.py:846-855`; records say `not_executed_smrt: true`, so this is
a declared center-address replay, not a full SMRT claim. Invalid UV handling
matches the contract: `:869-870` raises on nonfinite or UV outside `[0,1)` rather
than clamping. Descriptor identity (`:552-563`) requires exactly one descriptor in
the selected graph prefix and raises on missing/ambiguous matches, so no
cross-graph fallback.
All 17 captured biases (decoded from NPZ raw records at offset 300, float32) are
`0.6639266014099121`; A/B still select index 4 / VSM 8196 with
`unbiased_clipmap_index=3`, `bias_projection_id=8195`, so the correction is latent
here. An independent unequal-bias example shows why it matters: `abs=10.3`,
base 6, base bias 0.0, level-4 bias 0.9 gives corrected index 5 (VSM 8197); using
the base bias only would give index 4 (VSM 8196).
## Acceptance check 2 — writer provenance vs slice writes: yes
`report.md:7` labels the column "last resource output binding"; rows 12-13 name
pass 811, and line 17 states that pass 811 merges into slice 0 and "does not
overwrite the static slice 1". `publish/data/vsm_storage.json` ->
`snapshot_lineage.physical_page_pool.writer_scope` says the same: "last recorded
resource output binding; MergeStaticPhysicalPagesIndirect writes slice0 only, so
this is not a slice1 producer identity", recorded output `...ec290.[0].mip0`.
This matches `VirtualShadowMapPhysicalPageManagement.usf:804-811`, where
`MergePhysicalPixel` writes `OutPhysicalPagePool[uint3(PixelCoord, 0)]` and reads
the static slice. The final `checks.json` `snapshot_writer_lineage` check repeats
the qualified wording and passes (33 PASS / 0 FAIL / 0 SKIP overall).
## Acceptance check 3 — numeric arrays preserve the verified candidate: yes
One-shot independent comparison of
`vsm-final/output/publish/data/vsm_storage.npz` vs the current repository NPZ:
45 arrays in both, same shapes/dtypes, every array byte-identical (`tobytes()`),
and both files SHA-256
`40668b394aac1d2cce8f08ec1a08043e3208860904ab5f29e706c3f81c76ed45`.
`depth_mosaic_z_f32` contains 255,808 NaN invalid cells, so `np.array_equal`
falsely reports a difference; exact bytes and the identical file hash show it is
unchanged. This agrees with `root-integration-check.json` (45 listed arrays,
equal; same candidate/final and decoder hashes). A/B selected VSM, clipmap level,
index, bias, counts and sun ids/levels are also equal in candidate/final JSON.
## Zero-depth wording correction
`report.md:100` says the 99 selected pages have no zero-valued depths, the compact
atlas masks nonpositive values, and native-page/mosaic views apply the declared
ramp directly. Code agrees: `:2209` masks `~isfinite | <= 0` only for the compact
atlas, while `depth_rgb` (`:296-302`) is called directly for native pages/mosaic.
NPZ measurement: 0 zeros and 0 nonpositive values among 1,622,016 samples.
## Residual notes (non-blocking)
- Replay is float64 vs GPU float32; insensitive here (all biases equal, A/B not
  near an integer boundary).
- `proj[base.id + unbiased_index]` is guarded only by `unbiased_index < remaining`; safe here (8209 slots, base 8192, remaining 17), inconsistent captures only.
## Exact hashes
| artifact | SHA-256 |
|---|---|
| `decode_vsm_storage.py` | `101e393db566069c747b5e399a28f3eb940a0643d8cde23de0628618ebbcd3c2` |
| final `report.md` | `685cce0611553b4cee8b729c2a8b09d5aba8585f88094cdc1a09f4f34ef6d2c3` |
| candidate/final `vsm_storage.npz` | `40668b394aac1d2cce8f08ec1a08043e3208860904ab5f29e706c3f81c76ed45` |
| final `checks.json` | `d1f49abfbb72bae4a54f014ae4a6e8c3d8c2b4db176b1e1df7c37feddf622757` |
| `root-integration-check.json` | `bfeef30d3b19bca335b82255186916b2f4c63c56ea4660be0a6be70409ff93a4` |
Conclusion applies to the current repository files only.
