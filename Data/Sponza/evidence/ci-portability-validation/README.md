# CI portability correction

GitHub Actions run 43716584373 at 9f8b8c974614d592752606b29227363bce2c1d20 failed the reference-figure generation check. DeepCluster reproduced the discrepancy on Windows Python 3.14 and WSL Linux Python 3.10: all seven schematic SVGs match; the PNG's compressed IDAT differs, while the exact decompressed scanlines and non-IDAT metadata agree. The recorded diagnostic JSON includes hashes and zlib versions.

The initial DeepCluster run was cancelled by the root after collecting this evidence and proposing a patch. It is retained as diagnostic-run.json, not presented as a passing review. Root integration retains generator source-hash enforcement and adds PNG chunk-CRC, stream-completion, and trailing-data rejection. The checked-in PNG and all SVGs remain byte-identical; only the manifest generator hash is refreshed. Canonical comparison removes only compression encoding, while manifest hashes still verify the actual published bytes.

The Linux continuation also exposed two last-bit arithmetic differences in the course snapshot: 1.2011484668447452 versus 1.2011484668447434, and 28.12550570205571 versus 28.125505702055705. Only computed value/expected float fields may differ by at most 16 ULPs; field types, inputs, metadata, pass flags and per-experiment tolerances remain checked. Every experiment still executes and applies its original mathematical tolerance before snapshot comparison. SVG comparison stays exact.

Root verified both generators on Windows and WSL Linux, and the PNG positive/negative tests on both. The separate final review covers the integrated code and negative arithmetic cases. These diagnostics reproduce the platform dependency, not the private CI log (which requires sign-in).
