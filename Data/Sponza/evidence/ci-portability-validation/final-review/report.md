# Final portability integration review

**Scope:** the integrated diff at HEAD `9f8b8c974614d592752606b29227363bce2c1d20` for
`labs/lumen-reading/generate.py`, `labs/lumen-course/generate.py`, and
`Docs/Unreal/assets/lumen-reading/manifest.json`.

**Verdict: pass.** The integration preserves meaningful validation on both tested
platforms. No repository file outside `final-review/` was modified by this review.

## Platform runs

| Host | Python / zlib | reading `--check` | course `--check` | `test_portability_check.py` |
|---|---|---|---|---|
| Windows | 3.14.6 / zlib-ng 1.3.1 | exit 0, "Verified 7 diagrams, CPU reference, clean PNG, and manifest." | exit 0, "Verified 31 original SVG diagrams and 15 numerical checks" | exit 0, PASS |
| WSL Linux | 3.10.12 / zlib 1.2.11 | exit 0, same output | exit 0, same output | exit 0, PASS |

Commands:

```text
python Scripts/Docs/unreal_articles/labs/lumen-reading/generate.py --check
python Scripts/Docs/unreal_articles/labs/lumen-course/generate.py --check
python Data/Sponza/evidence/ci-portability-validation/test_portability_check.py
wsl.exe --exec python3 /mnt/d/UnrealUnderstanding/Scripts/Docs/unreal_articles/labs/lumen-reading/generate.py --check
wsl.exe --exec python3 /mnt/d/UnrealUnderstanding/Scripts/Docs/unreal_articles/labs/lumen-course/generate.py --check
wsl.exe --exec python3 /mnt/d/UnrealUnderstanding/Data/Sponza/evidence/ci-portability-validation/test_portability_check.py
```

## Independent negative probes

`independent_checks.py` ran 34 probes on each host; all passed. Raw results are in
`probes-windows.json` and `probes-wsl.json`. Key results:

- **Course snapshot:** saved-vs-fresh delta was 0 ULP on Windows and 8 ULP on WSL
  Linux, under the 16-ULP cap. A 1-ULP and a 16-ULP value change were accepted. A
  17-ULP change and a 1e-8 change in `value` or `expected` were rejected. A 1-ULP
  change to `tolerance`, `ggx_example.alpha`, or `seed`, a float-to-string type
  change, and a bool-to-int `passed` change were all rejected.
- **PNG encoding:** different zlib levels produced identical identities and digests;
  a zlib stream split across two IDAT chunks produced an identical identity. A
  changed pixel with rebuilt CRCs and a changed `tEXt` payload with rebuilt CRC
  both changed the identity and the canonical CPU SVG.
- **PNG integrity:** an IHDR byte changed without updating its CRC was rejected
  with `Invalid PNG CRC for b'IHDR'`. Truncation, bytes after IEND, junk after the
  zlib stream, a second complete zlib stream inside IDAT, and a removed zlib
  Adler-32 trailer were all rejected with the expected `ValueError`.
- **SVG and source hashes:** a changed native caption and changed embedded PNG
  pixels changed the canonical SVG. In temporary copies, the real `--check`
  rejected a changed CPU SVG (`stale .../cpu-reference.svg`), a wrong generator
  hash (`stale .../manifest.json`), a wrong `cpu_reference.sha256`, a wrong
  `clean_png_sha256`, and a corrupt PNG CRC. A wrong `pfm_sha256` was rejected by
  `portable_manifest`.
- **Byte preservation:** all 40 tracked PNG/SVG files under
  `Docs/Unreal/assets/lumen-reading` and `Docs/Unreal/assets/lumen-course` match
  HEAD raw bytes. The scoped `git diff` and untracked PNG/SVG status are empty on
  both hosts.

## Design assessment

- **Plan conformance:** no drift. PNG identity ignores only the deflate stream
  (and IDAT chunk splitting), while non-IDAT chunks, decompressed scanlines, and
  all SVG text remain exact. The manifest keeps `generator.sha256` exact and
  separately verifies the dropped CPU-SVG/clean-PNG byte fields against the
  checked-in files. The course comparator allows 16 ULPs only in `value` and
  `expected` floats; schema, inputs, tolerances, metadata, types, and pass flags
  remain exact.
- **Hidden-cost/risk review:** the additional work is a few decompressions of the
  117 KB PNG plus one extra JSON round-trip, which is negligible for CI. Temp
  directories are cleaned up. The code is CPU-only and makes no GPU, engine,
  capture, or timing assumptions. No races or resource leaks were found.
- **Residual observation:** a repo-wide WSL `git diff` over all `*.svg` reports
  47 pre-existing CRLF-only worktree differences under `material/`,
  `nanite-lumen/`, `scene/`, and `walkthrough/`. Replacing CRLF with LF makes each
  identical to HEAD, and Windows git (`core.autocrlf=true`) reports none. They are
  outside the reviewed output directories and were not modified, so they do not
  affect this review or a fresh Linux CI checkout.

## Evidence files

- `checks.json` — consolidated acceptance results.
- `probes-windows.json`, `probes-wsl.json` — raw 34-probe results per host.
- `independent_checks.py` — read-only probe harness; full `--check` negative runs
  use temporary copies of the checked-in assets.
