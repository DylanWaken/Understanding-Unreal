# VSM storage verification

The initial review found a real PageFlags hierarchy interpretation error despite exact selected depth values. It is retained under `initial-review/` as a failed, superseded review, not a passing validation of the final artifact.

The refinement replaces the invented packed hierarchy with captured PageFlags hardware mips H0–H6, distinguishes the eight logical shadow mips packed in PageTable hardware mip0, and adds the contiguous virtual-depth view. Its independent review under `refinement-review/` passed the full numerical reconstruction, including every mosaic pixel, but identified two remaining reusable-code/provenance defects in that candidate. Its failed status is preserved.

`root-integration-check.json` records the graph-selection, per-level bias, UV bounds, and subresource-provenance corrections applied to the final repository. Regeneration preserved all 45 NPZ arrays exactly. The separate final delta review checks those corrections against the pinned source. The published decoder and outputs are under `Scripts/Docs/unreal_articles/labs/lumen-sponza/` and `Docs/Unreal/assets/lumen-sponza/vsm-storage/`.

Raw address/depth verification is distinct from reproducing shader-local SMRT rays, bias decisions, and the final mask.

The completed final delta review is retained in `final-delta-review/`. It passes all three focused checks: source-correct level/bounds selection, slice-specific writer provenance, and byte-identical preservation of all 45 verified NPZ arrays. The final decoder verification records 33 PASS and no failures.
