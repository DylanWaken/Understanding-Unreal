# Final Lumen course validation — Tester

Date: 2026-11-15 · Repo: `D:/UnrealUnderstanding` · Workspace: `D:/UnrealUnderstanding-Review/final-doc-check`

All commands were run from the repository with `PYTHONDONTWRITEBYTECODE=1`; stdout/stderr/exit codes are in `results/logs/`. Only `results/` was written.

## Assigned commands

| Command | Exit | Result |
|---|---|---|
| `python Scripts/Docs/unreal_articles/build.py --check` | 0 | All 5 pages byte-identical to source, incl. `lumen.html` (854,158 B) |
| `python Scripts/Docs/unreal_articles/math_typesetting.py --check --node node` | 0 | 1028 LaTeX expressions verified |
| `python Scripts/Docs/unreal_articles/labs/lumen-course/generate.py --check` | 0 | 31 deterministic SVG diagrams + 15 numerical checks verified |
| `python Scripts/Docs/unreal_articles/validate.py` | 0 | Structure, links, figures pass; 157 SVGs, 313 source ranges |
| `python Scripts/Docs/check_site_independence.py` | 0 | 11 HTML pages, no links to Ardashir |
| `python Scripts/Docs/unreal_articles/validate.py --article lumen --unreal-root 'D:\UnrealEngine'` | 0 | 97 lumen source ranges match pinned-source sha256 |

## Published VSM verification

Copied **only** `Docs/Unreal/assets/lumen-sponza/vsm-storage/` to `results/vsm-copy/` (initial copy: 11 files, 3,060,800 B), then:

```
python Scripts/Docs/unreal_articles/labs/lumen-sponza/decode_vsm_storage.py \
  --out D:\UnrealUnderstanding-Review\final-doc-check\results\vsm-copy --verify
```

Result: `{"PASS": 33, "FAIL": 0, "SKIP": 0}` (15 preserved decode checks + 18 verify checks). No `--dump`, no `--engine`, no raw capture. Verify checks include manifest byte+sha256 equality, NPZ array re-derivation, page-table re-encoding, all 4096 metadata records, 99 full-resolution depth pages, and an explicit assertion that only published artifacts were used. The repository copy and its `checks.json` were not the verifier target. `results/vsm-copy/checks.json` is the verifier's rewritten result (37,661 B), so the final copy is 11 files / 3,048,250 B.

Operational note: the first invocation ran against an empty destination because the initial `Copy-Item -LiteralPath` did not expand the wildcard; it reported 1 FAIL. After re-copying the intended 11 files, the verifier ran once and passed 33/33. This was a setup error, not a product failure.

## Content and presentation checks

- **31/31 course SVGs** present; all parse as XML, contain **411 native `<text>` elements**, non-empty `<title>` and `<desc>`, a `viewBox`, and no raster `<image>` embeds.
- **All 4 linked VSM PNGs** exist: `vsm_pool_occupancy.png` (105,964 B), `vsm_page_table_sun.png` (4,685 B), `vsm_virtual_depth.png` (209,940 B), `vsm_depth_pages.png` (10,626 B); each is referenced by `lumen.html`.
- **Placeholders**: 0 `ROOT_INSERT` / `TODO-new-shadow` in the five `lumen-course*.html` sources or `lumen.html`. Site-wide, the only "placeholder" strings are HTML input attributes and an engine resource named `*.Placeholder`.
- **Anchors**: `#direct-lighting`, `#reflections`, `#shadow-views`, `#offscreen-shadows`, `#vsm-storage` each appear **exactly once** in the published page and exactly once collectively across the source chapters; 27 page ids, 0 duplicates.
- **Tables**: 10 tables in `lumen.html`, **10/10 inside `<details>`**; 78 balanced `<details>` elements.
- **Links/images**: 0 missing of **1,100 local href/src targets** across all 11 HTML pages; 77/77 images on the Lumen page resolve.
- **Cross-site**: site-independence pass; external hosts limited to the project GitHub, Epic, and cited graphics/reference sources.
- **Pinned source**: engine HEAD `71fe36aac5a8df5ccd66c763ffc902b29b6a9c43` matches the `base_commit` recorded in the evidence registry.
- **Scratch/cache**: no `.tmp`/`.bak`/`scratch` outputs under `Docs`, `Scripts`, or `Data`. Ignored entries are Python `__pycache__` (gitignored, not published), named evidence/package logs, and the preserved temporal-reproduction-check; untracked `Data` additions are provenance-only review records. New untracked `Docs` assets are the intended final publication.

## Earlier validations not rerun

Raw capture decoding, archive reconstruction/hash loops, GPU profiling, and the temporal/radiosity/placement reproduction suites were **not** rerun here; they passed in separate preserved reviews. Their evidence remains under `Data/Sponza/evidence/` and `Scripts/.../lumen-sponza/temporal-reproduction-check/`.

## Verdict

All three acceptance checks pass; see `results/checks.json` for machine-readable evidence and `results/vsm-copy/checks.json` for the 33 VSM checks.
