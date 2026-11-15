# Static final checks: integrated Lumen revision

Scope: read-only validation of `D:\UnrealUnderstanding` and the raw Sponza dump
`D:\UnrealUnderstanding-Sponza\output\gpu-dump\raw\sponza-20261115-083654`.
No GPU work, commits, or source edits were performed. Python 3.14.6, Node v24.22.1.
`PYTHONDONTWRITEBYTECODE=1` was set for the stable rerun and for every independent check.

## Result

- Stable rerun on the current revision: **19/19 commands exit 0**.
- Exact expected context total: **252/252 checks pass, 0 failed**.
- Receiver verify: **27/27 checks pass**.
- Independent raw/arithmetic spot checks: **7/7 pass**.
- Raw capture tree: **0 differences** in 12,458 files.
- Validation content: **pass**. Process/write scope: **fail** on one gitignored Python cache write
  during the first pass (details in "Write-scope caveat"). No source, published artifact, or raw
  file changed.

## Target revision and concurrent root edits

The first pass started while root was still regenerating the integrated site. The first pass ran
18 commands successfully and failed only command 04:

```
04-validate-lumen-v2  exit 1
ERROR: Artifact size mismatch for Scripts/Docs/unreal_articles/labs/lumen-frame/README.md: 24734 != 24649.
```

Root then updated `artifact-manifest.json` at `2026-11-15T10:09:08Z` and regenerated the Docs pages
at `2026-11-15T10:06:56Z`. The stable rerun ran
`2026-11-15T18:10:16+08:00` to `2026-11-15T18:11:19+08:00`
(`10:10:16Z` to `10:11:19Z`) and had **0 file changes during the run**.
All 19 commands passed on that stable rerun; command 04 now reports
`PASS artifact hashes: 236 files / 47,440,343 bytes.` and the independent audit of the current
manifest reports 236 entries with 0 size/SHA mismatches.

After the stable rerun, root added `temp/temporal-mobile-preview.html` (315 bytes,
`2026-11-15T10:12:03Z`), a gitignored temp preview consistent with the separate temporal work.
It is outside the published site and none of the validated commands read it; the raw tree stayed
at 0 differences. The stable-rerun result therefore describes the site revision immediately
before that temp-file addition.

At the stable-rerun snapshot, baseline-to-final working-tree differences are 10 added and 10
removed entries: 9 tracked files edited concurrently by root, plus the one ignored bytecode cache
described below. Root's edits:

| Path | Baseline | Final |
|---|---:|---:|
| `Docs/Unreal/assets/lumen-course/course.css` | 5,226 | 5,514 |
| `Docs/Unreal/lumen.html` | 683,187 | 683,226 |
| `Docs/Unreal/lumen-reference.html` | 2,805,826 | 2,805,826 (mtime) |
| `Docs/Unreal/material-compiler.html` | 109,424 | 109,424 (mtime) |
| `Docs/Unreal/nanite.html` | 400,171 | 400,171 (mtime) |
| `Docs/Unreal/nanite-and-lumen.html` | 1,783 | 1,783 (mtime) |
| `Docs/Unreal/scene-representation.html` | 336,886 | 336,886 (mtime) |
| `Scripts/Docs/unreal_articles/chapters/lumen-course-caches.html` | 54,274 | 54,516 |
| `Scripts/Docs/unreal_articles/labs/lumen-frame/artifact-manifest.json` | 49,767 | 49,767 (mtime) |

## Commands and return codes

Commands 1-6 and 8 ran in `D:\UnrealUnderstanding\Scripts\Docs\unreal_articles`; commands 7 and
9-18 ran in `D:\UnrealUnderstanding`. Stable rerun results:

| # | Command | Exit | Result |
|---|---|---:|---|
| 01 | `python build.py --check` | 0 | 5 articles verified: scene-representation 29,463 words/18 chapters; nanite 15,369/20; lumen 24,605/15; material-compiler 12,214/16; lumen-reference 65,579/44 |
| 02 | `python math_typesetting.py --check --node node` | 0 | `Verified 1010 LaTeX expressions` |
| 03 | `python validate_labs.py` | 0 | `PASS: 129 laboratory sources/outputs, 84 PNGs, frozen hashes and numerical evidence` |
| 04 | `python validate_lumen_v2.py` | 0 | 245 resource layouts; 7,024 payloads; 888 native files; 236 artifacts / 47,440,343 bytes; 6,001 portable capture checks |
| 05 | `python labs/lumen-reading/generate.py --check` | 0 | 7 diagrams, CPU reference, clean PNG, manifest |
| 06 | `python labs/lumen-course/generate.py --check` | 0 | 28 original SVGs and 15 numerical checks |
| 07a | `node --check Docs/Unreal/assets/lumen-course/course.js` | 0 | syntax accepted |
| 07b | `node --check Docs/Unreal/assets/lumen-course/temporal.js` | 0 | syntax accepted |
| 08 | `python validate.py` | 0 | `PASS: article structure, links, figures`; 154 accessible SVGs; 278 recorded source ranges |
| 09 | `python Scripts/Docs/check_site_independence.py` | 0 | `Site independence check passed: 11 HTML file(s)` |
| 10 | `python -m unittest discover -s Scripts/Docs/unreal_articles/labs/lumen-frame/tests -q` | 0 | `Ran 39 tests ... OK` |
| 11 | `python -m unittest discover -s Scripts/Docs/unreal_articles/labs/lumen-frame -p test_verify_frame_evidence.py -q` | 0 | `Ran 15 tests ... OK` |
| 12 | `node Scripts/Docs/unreal_articles/labs/lumen-frame/tests/test_metadata_browser.cjs` | 0 | 6 `ok` checks; `metadata browser tests passed` |
| 13 | `node Scripts/Docs/unreal_articles/labs/lumen-frame/tests/test_read_path.cjs` | 0 | 15 `ok` checks; `read-path tests passed` |
| 14 | `node Scripts/Docs/unreal_articles/labs/lumen-frame/tests/test_codec.cjs` | 0 | `19 codec tests passed.` |
| 15 | `python Scripts/Docs/unreal_articles/labs/lumen-sponza/verify_package_copy.py` | 0 | 6,852 files / 657,904,432 bytes; all SHA-256 match the reconstruction audit |
| 16 | `python Scripts/Docs/unreal_articles/labs/lumen-sponza/decode_receivers.py --out D:\UnrealUnderstanding-Review\static-final-checks\copies\receivers --stage verify` | 0 | `ok: true`, 27 checks |
| 17 | `python Scripts/Docs/unreal_articles/labs/lumen-sponza/cache-replay/verify_queries.py` | 0 | surface pass=694; world pass=747; all query invariants hold |
| 18 | `python Scripts/Docs/unreal_articles/labs/lumen-sponza/decode_context.py --dump D:\UnrealUnderstanding-Sponza\output\gpu-dump\raw\sponza-20261115-083654 --out D:\UnrealUnderstanding-Review\static-final-checks\copies\context --stage verify` | 0 | `total 252, passed 252, failed 0, ok true` |

Commands 16 and 18 used owned copies of the published `receivers` and `context` trees so that
`--stage verify` rewrote reports only under `static-final-checks/copies/`. The context copy has
57 files; 56 are byte-identical to the repository and the only difference is the verify output
`data/decode-verify.json` (both versions report 252/252). The receiver copy is 15/15
byte-identical.

## Independent raw and arithmetic checks

Run as `python D:\UnrealUnderstanding-Review\static-final-checks\verify_spot_checks.py`; exit 0,
7/7 checks. Evidence: `spot-checks.json`.

### Raw payload SHA and modification time

Requested file:
`Resources/00010000020c12495520.mip4.v0001000002090ec60010.bin`

- Size 27,648 bytes; SHA-256 `b9fb3984f9f01cb1bbb47cc199345120bf635bfefd5d6a30a9ebbe8b71b27972`.
- Inventory payload `payload-01891`: 27,648 bytes, same SHA-256; its single 27,648-byte chunk also
  has the same SHA-256.
- Expected value `b9fb3984f9f01cb1bbb47cc199345120bf635bfefd5d6a30a9ebbe8b71b27972`: exact match.
- LastWriteTime UTC `2026-11-15T08:37:24.562226Z`, about 30 seconds after the run-directory
  timestamp `2026-11-15-083654`; consistent with capture time, not a later content rewrite.

The earlier "dirty raw file" flag is not reproduced by the content-hash and mtime checks requested
here. No raw file was edited; the pre/post raw tree has 0 differences.

### Pass 762 structure, offset 460

- Pass 762 is `TemporalReprojection(1280x720)`, `Parameters = 000400000208f36dfaa0`,
  `ParametersMetadata = 000400007ffc1e2ef870`.
- The metadata is `FParameters` from `LumenScreenProbeGather.cpp:1212`; field
  `MaxFramesAccumulated` is at offset 460.
- `Structures/000400000208f36dfaa0.bin` is 576 bytes; bytes 460-463 are `00 00 20 41`, and the
  little-endian float32 is **10.0**.

### Adaptive allocator, pass 706

- Pass 706 is `AdaptivePlacementSpawn`, pointer version `00040000020bd35c1b10`.
- Count buffer `0004000002090c6ba5c0.v00040000020bd35c1b10.bin` is 4 bytes; raw R32_UINT =
  **117**.
- Header `0004000002090c6ba8d0.mip0.v00040000020bd35c1b10.bin` (80x56) has sum **117** over
  exactly **117** nonzero tiles.
- Placement data: 3,809 finite-storage probes = 3,569 uniform + **240** adaptive; active total
  **3,686** = 3,569 + **117** active adaptive; **123** adaptive entries are stale, with adaptive
  indices exactly **117..239**, while active adaptive indices are exactly **0..116**. The 117
  active adaptive tiles map bijectively to the 117 header tiles. The 123 stale entries are
  therefore excluded from active counts and plotting.

### Corrected SH comparison at pixel (498,680)

The checked claim is `Scripts/Docs/unreal_articles/chapters/lumen-course-reconstruction.html:24`
(line SHA-256 `1625e5e64d3b95f15be2ffe7e87d8a2ac8c3a6d98e680673799308ff0b23eadd`).

- Probe A (uniform index 3391) independent projection using the captured `TranslatedWorldToClip`
  matrix and 1280x720 view rect: `(498.500016644, 680.500045420)`, i.e. the center of pixel
  **(498,680)**. The decoded `DiffuseIndirect` pixel index is `[498,680]`.
- Independent SH3 evaluation with the Unreal real-SH basis and Lambert convolution
  (`A0=pi`, `A1=2pi/3`, `A2=pi/4`), AO=1, at the probe normal gives raw
  `(0.3157182, 0.4199811, 0.6449488)` and unit-albedo Lambert response
  `4*raw = (1.2628728, 1.6799242, 2.5797952)`. The chapter's
  `(1.26287, 1.67993, 2.57980)` agrees within `5e-6`. Using the shader/package's rounded basis
  constants gives `1.6799257`, which is the basis of the chapter's `1.67993` at the fifth
  decimal; the difference from exact constants is `1.5e-6` and does not affect the percentages.
- Captured `DiffuseIndirect` at that pixel: R11G11B10 word `0x819f23cd`, decoded
  **(1.203125, 1.5625, 2.375)**, exactly the chapter's captured field.
- Relative differences: **4.966053%**, **7.515151%**, **8.622955%**, i.e. the chapter's
  approximately **5.0%, 7.5%, 8.6%**.
- Physical irradiance computed as `4*pi*raw = pi*(4*raw)` is
  `(3.967432, 5.277638, 8.104666)`, confirming the chapter's warning that omitting `4*pi` would
  be a large systematic error.

### Reflection history at receiver C

- Receiver C pixel `(626,587)`.
- Current `NumFramesAccumulated` byte and imported-history byte are both **96**.
- Effective frames = `96/255*32 = 12.047058823529412`, not 96. The JSON record reports the same
  raw byte and effective value.

## Genuine unresolved issues

1. **Write-scope caveat (process, not course evidence).** The first harness invocation ran before
   `PYTHONDONTWRITEBYTECODE` was inherited by its child processes and refreshed the pre-existing,
   gitignored file
   `Scripts/Docs/unreal_articles/__pycache__/build.cpython-314.pyc`
   (51,944 -> 52,052 bytes, mtime `2026-11-15T10:05:54.635Z`). This is outside the permitted
   `static-final-checks/` write scope. It is a derived bytecode cache, not source, a tracked
   artifact, or raw data. It cannot be restored byte-for-byte because the pre-existing cache was
   overwritten; the stable rerun itself made 0 tree changes. No tracked source/published artifact
   and no raw file changed.

No unresolved course, test, arithmetic, package, or raw-evidence failure remains on the stable
revision. The first-pass command 04 failure was a mid-update manifest race and is resolved by the
current manifest (236/236 entries match).

## Verdict

- Validation content: **pass** — all 19 commands, 252 context checks, 27 receiver checks, and 7
  independent spot checks pass on the stable revision.
- Repository/write scope: **fail** — one gitignored bytecode cache was refreshed in the read-only
  repository during the first pass; disclosed above.
- Overall: **fail** on the write-scope acceptance check only; no course, test, package, arithmetic,
  or raw-evidence failure remains.

Evidence files: `checks.json`, `spot-checks.json`, `aux-audit.json`,
`artifact-manifest-audit.json`, `run-results.json`, `run-results-first.json`,
`tree-before.txt`, `tree-post-rerun.txt`, `raw-tree-before.txt`, `raw-tree-after.txt`,
`logs/`, `logs-first/`.
