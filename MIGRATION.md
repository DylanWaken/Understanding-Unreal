# Migration record: Ardashader → Understanding Unreal

This records the initial migration snapshot. Later Lumen rewrites and capture additions are separate changes; the counts below describe the migration, not every later revision.

After that copy was verified, the migrated essays, authoring files, and assets were removed from Ardashir in commit `061db67e95360fa3af07f051f254372bd375e4f9`. Commit `256b2fd` also removed the homepage's relocation notice and external Unreal navigation. No redirect pages remain in Ardashir. The user's unrelated working-tree changes were preserved. The read-only-copy statements below describe the initial migration stage, before this requested cleanup.

## Source snapshot

- **Source checkout:** `D:/Projects/Ardashader`
- **Source HEAD:** `4f55fb953f65ca9db3014e76ffcff2ccd1d89297`
- **Destination:** `D:/UnrealUnderstanding` (standalone repository; no source Git metadata was modified and no Git state was changed)
- **Target repository:** `https://github.com/DylanWaken/Understanding-Unreal` (remote `origin`; the local folder name differs)
- **Target Pages URL:** `https://dylanwaken.github.io/Understanding-Unreal/` (Pages must be enabled in repository settings; this record does not claim a completed deployment)
- **Migration method:** read-only copy of the current working tree, followed by standalone build and validation in the destination.

### Dirty-working-tree caveat

The source commit alone does **not** reproduce this migration. The working tree was dirty:

- `git status --porcelain=v1 --untracked-files=all` reported **1,076** entries repository-wide.
- **369** of those entries were under the selected paths (`Docs/Unreal`, `Scripts/Docs/unreal_articles`, `Docs/glossary.html` and the three shared `Docs/assets` files).
- The four essay authoring trees, most essay assets and the authoring/lab scripts were **untracked** at the time of migration.
- `Docs/Unreal/material-compiler.html` and `Docs/Unreal/scene-representation.html` had **modified** built pages.
- `Docs/glossary.html`, `Docs/assets/site.css` and `Docs/assets/site.js` had **modified** shared-site files.
- The tracked `Docs/Unreal` tree still contained deleted Arda-era scene pages and assets; those deletions were not resurrected.

This migration therefore captures the working tree as inspected, not merely the commit. The current source checkout was not changed: HEAD remains `4f55fb953f65ca9db3014e76ffcff2ccd1d89297`, and a post-migration status check still reports 1,076 entries (369 under the selected paths). No source-writing command was run.

## Scope and essay discovery

`Docs/` was inventoried for Unreal analysis material.

Migrated:

- The four independent essays under `Docs/Unreal`:
  - `scene-representation.html`
  - `nanite.html`
  - `lumen.html`
  - `material-compiler.html`
- The legacy combined-essay route `Docs/Unreal/nanite-and-lumen.html`, `legacy-routing.js` and its preserved chapter anchors.
- All essay figures, diagrams, math assets/fonts, lab evidence and site dependencies.

Also present under `Docs/`, but intentionally **not** migrated as independent Unreal essays:

- `Docs/ArdaBackend/unreal-rhi-audit.html` and `Docs/ArdaBackend/unreal-rhi-parity-plan.html` are ArdaBackend module documents about that backend's Unreal RHI integration audit and parity plan. They are not independent Unreal rendering analyses and were excluded with the unrelated Arda module documentation.
- `Docs/Essays/arda-graph-and-inductor.html` is an Arda render-graph essay, not an Unreal analysis.

No other independent Unreal analysis essay was found under `Docs/`. The four migrated essays are therefore the complete applicable set. Comparative Arda material embedded in the essay prose is preserved verbatim; only the shared shell and the two Nanite laboratory source links were changed.

## Source → destination mapping

| Source | Destination | Source files | Destination files | Notes |
| --- | --- | ---: | ---: | --- |
| `Docs/Unreal/**` | `Docs/Unreal/**` | 252 | 252 | All 244 assets are byte-identical; the four built pages and the legacy redirect were regenerated from authored sources with the new shell branding. |
| `Scripts/Docs/unreal_articles/**` (excluding caches) | `Scripts/Docs/unreal_articles/**` | 67 | 67 | 64 authored files are byte-identical. `build.py` was rebranded; `chapters/nanite-lab.html` and `nanite-runtime.json` had their Nanite lab repository URLs redirected. |
| `Docs/glossary.html` | `Docs/glossary.html` | 1 | 1 | Rebranded shell; glossary content and all 146 term anchors preserved; Arda `Read more` links redirected to the published Ardashir site. |
| `Docs/assets/{glossary-data.js,site.css,site.js}` | `Docs/assets/` | 3 | 3 | Byte-identical. |
| `Docs/.nojekyll` | `Docs/.nojekyll` | 1 | 1 | Byte-identical. |
| Source `Docs/index.html` (Arda portal) | `Docs/index.html` | — | 1 | Not copied; a new Understanding Unreal homepage was authored for the standalone scope. |
| Source `.github/workflows/pages.yml` | `.github/workflows/pages.yml` | — | 1 | Not copied; a standalone `main`/`workflow_dispatch` Pages workflow was authored. |
| — | `README.md`, `MIGRATION.md`, `.gitignore` | — | 3 | Repository documentation and exclusions. |

The authoring layout remains `Scripts/Docs/unreal_articles/chapters`, `evidence`, `labs` and `vendor`, and the output layout remains `Docs/Unreal`. `build.py --article <key>` still works for single-article iteration.

## Manifest

Hashes are SHA-256 over a deterministic stream of `repo-relative path`, a NUL byte, the SHA-256 of the file, and a newline, with paths sorted. The manifest below was computed after the final build and cache cleanup. It covers every file currently present in the named trees.

| Tree | Files | Bytes | Aggregate SHA-256 |
| --- | ---: | ---: | --- |
| `Docs/` (published site) | 258 | 48,896,488 | `679e1828f11e97cf6943ae9a4c603a6ccffbe0aaa0bae538004bb172d7cf57f5` |
| `Docs/Unreal/` | 252 | 48,603,992 | `71b2d28349ec87fb6795b5386b471699a322fd0494b616854a71795bed99b414` |
| `Docs/Unreal/assets/` | 244 | 47,124,471 | `42fb1ad9d77476f3d6184eb533456c2bee0ac9671e19eac2667c595ff18d5680` |
| `Scripts/Docs/unreal_articles/` | 67 | 2,434,034 | `b5f683e53a054ad6aec1ed61c1df330cdd85a4cad41f8d66f7ced9ab90ca89e2` |
| `Docs/` shell and glossary (6 files) | 6 | 292,496 | `577b9f84907c1646c68962531e336901df414d0065fcea011e32387fc0fd517b` |

The six shell/glossary files are `Docs/index.html`, `Docs/glossary.html`, `Docs/.nojekyll`, `Docs/assets/glossary-data.js`, `Docs/assets/site.css` and `Docs/assets/site.js`.

**Published site size:** 258 files, 48,896,488 bytes (46.63 MiB). The full working tree is about 51.4 MB; the difference is repository documentation, workflow and `.gitignore`. The largest published file is `Docs/Unreal/assets/lumen-lab/computed-data.npz` at 3,014,845 bytes. **No file is 100 MB or larger.** All 60 KaTeX font files, `katex.min.css`, `math.css` and the KaTeX license are under `Docs/Unreal/assets/math/`; the vendored renderer remains under `Scripts/Docs/unreal_articles/vendor/katex/`.

## Excluded files and directories

Unrelated Arda documentation and applications:

- `Docs/ArdaBackend/`, `Docs/ArdaRDG/`, `Docs/ArdaScene/`, `Docs/ArdaTrace/`, `Docs/Essays/`
- `Docs/assets/arda-rdg-api.js`, `Docs/assets/backend-api.js`, `Docs/assets/operation-batching.svg`
- `Scripts/Docs/arda_graph_essay/`, `Scripts/Docs/HtmlDocumentation.py`, `Scripts/Docs/SyncGraphNodeRecipes.py`, `Scripts/Docs/__pycache__/`
- The source `Docs/index.html` Arda portal (replaced by the Understanding Unreal homepage)

Temporary, local or generated material:

- `Scripts/Docs/unreal_articles/__pycache__/` and every Python cache
- `Scripts/Docs/unreal_articles/labs/nanite/.venv/` (local virtual environment)
- `Scripts/Docs/unreal_articles/labs/nanite/cache/` (generated hierarchy, arrays and run log)
- `Scripts/Docs/unreal_articles/labs/nanite/data/` (downloaded `bunny.tar.gz`, `bun_zipper.ply` and other raw Stanford downloads)
- Unreal project `Binaries/`, `Build/`, `Intermediate/`, `Saved/` and `DerivedDataCache/` output
- Raw GPU captures, temporary logs and temporary browser-QA output

No full Unreal Engine source tree and no compiled Unreal project binaries are included. The only Unreal project code is the small authored `labs/unreal/Source/ArdaLabCapture` editor bridge (a C++ header/implementation plus its build rule), the `UnrealLab.uproject` descriptor, `DefaultEngine.ini`, and the capture scripts. These are laboratory source, not engine source or a compiled product.

## Rebranding and link policy

- `Scripts/Docs/unreal_articles/build.py` now emits the **Understanding Unreal** masthead, page titles, navigation labels, footer and legacy-redirect page. Article prose is otherwise untouched.
- `Docs/index.html` is a new branded homepage linking every independent essay, its scope, the glossary, reproducibility material and the companion project.
- `Docs/glossary.html` retains all glossary entries and anchors, including comparative “In Ardashir” notes, but its shell is branded Understanding Unreal and its navigation no longer links to Arda module pages.
- Cross-project glossary links to `ArdaBackend/…` and `ArdaRDG/…` now target `https://dylanwaken.github.io/Ardashir/` with the original relative paths and fragments preserved.
- The two Nanite laboratory source links now target `https://github.com/DylanWaken/Understanding-Unreal` on `main`, where the migrated authoring tree lives.
- Epic Games documentation/source links, Stanford references, paper citations and all other external provenance links are unchanged.
- All `href`/`src` values in the chapter fragments, placements and runtime JSON were inventoried. Aside from the migrated laboratory and documentation links described above, the remaining URLs are external citations or source repositories; no other local Arda page was a dependency.
- No Lumen chapter, placement, runtime, evidence or asset file was rewritten. The Lumen page was regenerated only to apply the shared shell branding.
- Historical records such as `Scripts/Docs/unreal_articles/verification.json` are preserved unchanged. Their source-checkout-wide checks (including the excluded Arda graph essay) describe the source worktree, not this standalone site; the checks listed below are the ones run here.

## Standalone build and validation

No Arda source tree, GPU or Unreal checkout is needed for the checks. The following were run from `D:/UnrealUnderstanding`:

```text
python Scripts/Docs/unreal_articles/build.py                 # built all 4 essays + redirect
python Scripts/Docs/unreal_articles/build.py --check          # Verified 4 essays
python Scripts/Docs/unreal_articles/math_typesetting.py --check --node node
                                                              # Verified 263 LaTeX expressions
python Scripts/Docs/unreal_articles/validate.py               # PASS
python Scripts/Docs/unreal_articles/validate_labs.py          # PASS
```

Validation results:

- Four essays: 29,463 + 15,369 + 16,599 + 12,214 words, with 18/20/23/16 chapters.
- 74 accessible SVGs and 268 recorded source ranges.
- 129 laboratory sources/outputs, 84 PNGs, frozen hashes and numerical evidence.
- 12 Unreal room screenshots, attachment transforms, 30 instances, 68-bone skeleton and reproduction source hashes.
- An additional destination-wide HTML link/fragment check found no broken internal links across the seven published HTML files.

`validate.py` was deliberately run without `--unreal-root`: the Unreal source is not published here. The source-hash path remains available for a local checkout. `validate_labs.py` checks the frozen laboratory inventory and numerical evidence, not a GPU rerun. Nothing was removed from the checks to compensate for missing assets.

The Pages workflow in `.github/workflows/pages.yml`:

- runs on pushes to `main` and on `workflow_dispatch`;
- uses `actions/checkout@v4`, `actions/configure-pages@v5`, `actions/upload-pages-artifact@v4` and `actions/deploy-pages@v4`;
- runs `build.py --check`, the offline math-cache check, `validate_labs.py` and `validate.py`;
- deploys only `Docs/`, including `.nojekyll`, with the standard Pages permissions and environment.

## Capture reproduction

The source snapshot and published evidence identify Unreal Engine **5.8.1-0+UE5** at engine commit `71fe36aac5a8df5ccd66c763ffc902b29b6a9c43`, Direct3D 12, Shader Model 6, on an NVIDIA RTX PRO 6000 Blackwell Workstation Edition. The local build reported changelist zero, which is not a released-build identifier. The full reproduction environment, commands, capture semantics and limitations remain in:

- `Scripts/Docs/unreal_articles/labs/unreal/README.md`
- `Scripts/Docs/unreal_articles/labs/unreal/scene-room-README.md`
- `Docs/Unreal/assets/unreal-lab/capture-manifest.json`
- `Docs/Unreal/assets/scene-room-captures/scene-room-provenance.json`
- `Docs/Unreal/assets/lumen-lab/metrics.json`
- `Docs/Unreal/assets/nanite-lab/provenance.json`

Raw downloads, virtual environments, caches and logs are excluded by `.gitignore`; raw GPU dumps stay in the local temporary directory. The checked-in figures and evidence records are the reviewable, hashed artifacts; see `README.md` for the regeneration commands.
