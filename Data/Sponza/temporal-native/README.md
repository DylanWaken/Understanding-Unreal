# Native temporal observations

These thirteen selected-resource DumpGPU states support the scene-anchored card, probe, SH and cache observations in the Lumen course. Every file beneath each selected state is preserved byte-for-byte. They are selected-resource captures, not complete GPU frames or shader-local traces.

| Sequence | Run | States |
|---|---|---|
| Fixed-camera light change | 20261115-101057 | baseline, lightstep-001, lightstep-008, lightstep-032, lightstep-064 |
| Camera translation | 20261115-104035 | baseline, camera-001, camera-064 |
| Surface radiosity | 20261115-105951 | baseline, light-001, light-064, camera-001, camera-064 |

The filenames retain capture targets. Actual updates for target 8, 32 and 64 were 7, 31 and 63. The index retains engine-frame counters, view observations, settings and capture manifests; playback does not imply evenly spaced simulation time.

The archive preserves 23,923 files totaling 27,141,983,250 native bytes. SHA-256 deduplication and gzip reduce them to 8,468 chunks plus an index, totaling 1,957,882,466 bytes. Chunks contain at most 32 MiB of raw bytes. The largest stored chunk is 14,298,622 bytes. This package is outside the Pages site; the article loads derived figures and records on demand.

From the repository root, verify without access to Unreal or the original scratch directories:

```powershell
python Scripts/Docs/unreal_articles/labs/lumen-sponza/archive_temporal.py verify --package Data/Sponza/temporal-native/package --out temporal-verification.json
python Scripts/Docs/unreal_articles/labs/lumen-sponza/archive_temporal.py reconstruct --help
```

Verification checks compressed and decompressed chunk hashes, all file sizes and SHA-256 identities, inventory digests, bounds and safe relative paths. Reconstruction accepts one selected state and optional relative file paths; choose a new output directory. All thirteen states require about 27.2 GB if fully reconstructed; inspecting one operand does not require that allocation. Historical workstation paths in the index serve as provenance and do not affect decoding.

`verification.json` records the complete source-free verification. `source-compare.json` records a comparison of all source files after packaging: zero missing, extra, size-mismatched or hash-mismatched files. `report.md` preserves the detailed inventory and capture coverage.

The radiosity sequence contains card depth, expanded normals, four SH coefficients per channel, history counters and the captured cache-exposure parameter. The exact radiosity dispatch list and independent unfiltered ray atlas were not included. Expanded compressed-texture readbacks do not recover original BC block bits. The archive preserves only the bytes that were captured, so unobserved operands are absent.
