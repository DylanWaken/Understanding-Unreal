# Integrated course validation snapshot

These results validate the 29,302-word, 16-chapter course with dedicated Direct Lighting and Reflections chapters, before the subsequent VSM storage-visualization addition. The exact inspected file hashes are retained under `results/`.

The checks completed successfully: 11 commands, 507 temporal-manifest checks, 158 SVG checks and 69 independent native-data checks. The DeepCluster worker wrote its report and numerical results, but its orchestration step subsequently timed out. `deepcluster-report.json` preserves that **failed orchestration status**; the report's PASS describes the recorded checks, not successful completion of the worker lifecycle. The supervising author inspected the checks and independently checked representative native values and hashes. Concurrent root edits and the transient mid-build failure are recorded rather than attributed to the reviewer.

The separate `chapter-source-validation/` evidence records the completed source review. Later VSM storage evidence has its own decoder and independent checks.
