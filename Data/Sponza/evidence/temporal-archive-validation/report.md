# Temporal archive verification

Status: **PASS**

## Commands

All commands ran with `PYTHONDONTWRITEBYTECODE=1`.

| Step | Command | Exit | Result |
|---|---|---|---|
| Snapshot before | `python results/verifier.py snapshot --out results/source_before.json` | 0 | 800 baseline files, 1,448,004,599 bytes |
| Archive verify | `python D:/UnrealUnderstanding/Scripts/Docs/unreal_articles/labs/lumen-sponza/archive_temporal.py verify --package D:/UnrealUnderstanding/Data/Sponza/temporal-native/package --out D:/UnrealUnderstanding-Review/temporal-validation/results/verification.json` | 0 | PASS, 32.404 s |
| Independent verify | `python results/verifier.py verify --before results/source_before.json --out results/independent_verification.json` | 0 | 9/9 checks PASS |

Archive verify CLI: `[verify] PASS states=13 files=23923 chunks=8468 file_bytes=27141983250 unique_chunk_raw_bytes=9874820751 gzip_bytes=1946607594 failures=0`.

## Published artifact

`results/verification.json`:

- `status`: pass; `failures`: 0
- 12/12 archive checks true
- states 13, files 23,923, chunks 8,468, package files 8,469, unique file hashes 8,431
- file bytes 27,141,983,250; unique chunk raw 9,874,820,751; gzip 1,946,607,594
- max chunk raw/gzip 33,554,432 / 14,298,622 bytes; index 11,274,872 bytes
- every chunk gzip MTIME=0, gzip SHA-256, raw SHA-256; every file size and SHA-256 verified
- every catalog chunk referenced; all file paths safe; no package file over 90 MiB

## Independent reconstruction

Chosen from `Base/ResourceDescs.json` metadata and actual `Resources/` binaries, not by viewer-file substrings.

| Resource | Metadata | UniqueResourceName | Source file | Bytes | SHA-256 | Exact bytes |
|---|---|---|---|---|---|---|
| View buffer | `ViewData.PrimaryViews` (index 0) | `00010000026046775e60` | `Resources/00010000026046775e60.v00000000000000000000.bin` | 1,056 | `da76c3bf2b1041965d8fb67a3f9c689383591d4658c02fbf3f5157f0e3ef2684` | yes |
| SH texture | `Lumen.Radiosity.ProbeSHRedAtlas` (index 40), `PF_FloatRGBA` 1024x1024 | `0001000002604645cc58` | `Resources/0001000002604645cc58.mip0.v00000000000000000000.bin` | 8,388,608 | `8664ec6625ee35b2d5c4ce09627019a99dfbafac30ea2a7c3e7e22b875c26c1b` | yes |

Both payloads were reconstructed in memory from `index.json` + `chunks/` only, then compared byte-for-byte with `D:/UnrealUnderstanding-Radiosity/output/gpu-dump/raw/20261115-105951/baseline`. Index record SHA-256, reconstructed SHA-256, and source SHA-256 agree for both.

## Arbitrary chunk/index pair

- id: `7dbb3470659df6e9294f483c4dac5128d3631878d5ad03d0dd4fc77720fb0e3e`
- path: `chunks/7d/bb/7dbb3470659df6e9294f483c4dac5128d3631878d5ad03d0dd4fc77720fb0e3e.bin.gz`
- gzip: 408 bytes, SHA-256 `3159a7ce836e5a28b0225bd5d4279e04fbf4679935e19d474e4280a39d664d5d`, MTIME 0
- raw: 1,307 bytes, SHA-256 equals chunk id and index `raw_sha256`
- catalog path canonical; baseline-referenced by 3 file records
- source slice `StructuresMetadata/000100007ffc20edeb80.json` offset 0 length 1307 is byte-equal
- selection: sorted baseline-referenced chunk ids, `sha256(fixed_tag) % 286 = 130`

## Source and repository unchanged

Before/after comparison in `results/independent_verification.json`:

- baseline source: 800 files, 1,448,004,599 bytes, per-file SHA-256 map unchanged
- baseline manifest SHA-256 `9cb9dfe1880070d89bee21a287ccbbf4939ede19cf00b2e9e5e94837d9925b05` unchanged
- chosen source bytes/SHA-256/mtime unchanged
- repo HEAD `05596a39e75e2f25288734a2f9e62e71e6aa6086` unchanged
- repo git status SHA-256 `7ea240da63b0a2a4a2fad2995d7af31d91bd7d18404fc5b89ea70cbb5f8e7e71` (990,527 bytes) unchanged
- package index SHA-256 `eadf3ad8e902e934caf53d6e61696af457a414990b46072c9259eb1d6fba1941` unchanged
- 8,468 chunk file count/size and chunk-tree metadata SHA-256 unchanged

No reconstructed payload was written to disk; no GPU workload or new run was started.

Invocation note: an initial archive-verify invocation was repeated because its PowerShell tee log path resolved inside the repo and the tee failed. That invocation wrote no repo files (repo status hash is identical before/after); the clean rerun above exited 0 and produced `verification.json`.

## Artifacts

`results/verification.json`, `results/independent_verification.json`, `results/source_before.json`, `results/verifier.py`, `results/checks.json`, `results/verify_cli.log`, `results/independent_cli.log`.
