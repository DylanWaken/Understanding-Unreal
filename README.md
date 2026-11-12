# Understanding Unreal

Independent, source-backed essays on Unreal Engine rendering. The site source lives in [`Docs/`](Docs/); the standalone Pages workflow is configured to publish it at `https://dylanwaken.github.io/Understanding-Unreal/` once GitHub Pages is enabled.

The collection contains four standalone long-form articles:

- **Scene Representation in Unreal Engine** — one room from shared assets and components through render proxies, GPU Scene, animation, raster passes and lighting representations.
- **Nanite: From a Scanned Bunny to Visible Pixels** — a measured replacement hierarchy, streaming, visibility, rasterization and shading, with a computed Stanford bunny laboratory and real engine captures.
- **Lumen: Following Light Through a Room** — surface caches, distance-field tracing, probes, diffuse GI, reflections and temporal reuse, with computed intermediate states and real Lumen captures.
- **How Unreal Compiles a Material Graph** — graph analysis, HLSL translation, shader compilation, permutations, caching and GPU material programs.

A shared [graphics glossary](Docs/glossary.html), the site shell and the old combined-essay redirect are published alongside them. The legacy `nanite-and-lumen.html` page preserves old chapter bookmarks by forwarding to the independent essays.

## Repository layout

| Path | Purpose |
| --- | --- |
| `Docs/index.html` | Understanding Unreal homepage and essay index. |
| `Docs/Unreal/` | Built/published essay pages, diagrams, math assets, lab evidence and progressive-enhancement scripts. |
| `Docs/glossary.html` | Shared glossary; Arda-specific entries are retained as comparative notes and their source links point at the companion Ardashir site. |
| `Docs/assets/` | Site shell (`site.css`, `site.js`) and glossary data used by the homepage and glossary. |
| `Scripts/Docs/unreal_articles/` | Authored chapter fragments, figure placements, evidence records, generators, validators, math cache and reproducible labs. |
| `.github/workflows/pages.yml` | Validates the standalone site and deploys `Docs/` to GitHub Pages. |

The chapter HTML under `Scripts/Docs/unreal_articles/chapters/` is the prose source. `build.py` composes each article, inserts placed chapters and walkthrough figures, renders the frozen equation cache, and writes the four pages plus the legacy redirect into `Docs/Unreal/`. Do not hand-edit the generated essay pages.

## Prerequisites

- Python 3.12 or newer.
- Node.js on `PATH` for the offline equation check and for regenerating the math cache.
- No third-party Python packages are needed for the build and validation checks; the labs list their own requirements in `labs/*/requirements.txt`.

## Build and check

Run from the repository root.

```powershell
# Rebuild all four essays and the legacy redirect.
python Scripts/Docs/unreal_articles/build.py

# Verify the committed pages exactly match the authored sources.
python Scripts/Docs/unreal_articles/build.py --check

# Verify the frozen, pre-rendered KaTeX cache without network access.
python Scripts/Docs/unreal_articles/math_typesetting.py --check --node node

# Validate article structure, headings, internal links, figure alt text, SVG accessibility and evidence records.
python Scripts/Docs/unreal_articles/validate.py

# Validate frozen laboratory outputs, hashes, PNG integrity and numerical evidence without a GPU.
python Scripts/Docs/unreal_articles/validate_labs.py
```

The same checks run in CI before the `Docs/` artifact is uploaded:

```powershell
python Scripts/Docs/unreal_articles/build.py --check
python Scripts/Docs/unreal_articles/math_typesetting.py --check --node node
python Scripts/Docs/unreal_articles/validate_labs.py
python Scripts/Docs/unreal_articles/validate.py
```

To regenerate the equation cache after changing LaTeX (writes `math-cache.json`):

```powershell
python Scripts/Docs/unreal_articles/math_typesetting.py --node node
```

The vendored KaTeX build and fonts live in `Scripts/Docs/unreal_articles/vendor/katex/` and `Docs/Unreal/assets/math/`. The published site uses only those local assets; it does not load a CDN.

### Editing one article

```powershell
python Scripts/Docs/unreal_articles/build.py --article nanite
python Scripts/Docs/unreal_articles/build.py --check --article nanite
python Scripts/Docs/unreal_articles/validate.py --article nanite
```

## Capture reproducibility

The essays cite Unreal Engine 5.8.1 source ranges at commit `71fe36aac5a8df5ccd66c763ffc902b29b6a9c43`. The recorded captures came from a local Unreal source build reporting **5.8.1-0+UE5**, Direct3D 12, Shader Model 6, on an NVIDIA RTX PRO 6000 Blackwell Workstation Edition. The checkout reported changelist zero, so the changelist is not a released-build identifier.

Published evidence is frozen and auditable:

- `Scripts/Docs/unreal_articles/evidence/` records the cited source ranges and their SHA-256 hashes.
- `Scripts/Docs/unreal_articles/laboratory-manifest.json` freezes the laboratory source/output inventory.
- `Docs/Unreal/assets/unreal-lab/capture-manifest.json` records engine build fields, figure names and image hashes.
- `Docs/Unreal/assets/scene-room-captures/scene-room-provenance.json` records capture environment, source hashes and scene inventory.
- `Docs/Unreal/assets/lumen-lab/metrics.json`, `Docs/Unreal/assets/nanite-lab/metrics.json` and their provenance files contain the computed measurements and limitations.

`validate_labs.py` verifies these frozen artifacts, image formats, source hashes and the numerical assertions without a GPU. Regenerating the measurements requires the matching environments:

```powershell
# Computed Lumen room (CPU; install labs/lumen/requirements.txt first).
python Scripts/Docs/unreal_articles/labs/lumen/render.py

# Computed Nanite bunny (downloads attributed Stanford data; install labs/nanite/requirements.txt first).
python Scripts/Docs/unreal_articles/labs/nanite/download.py
python Scripts/Docs/unreal_articles/labs/nanite/run.py --rebuild

# Actual Unreal captures (Windows, matching Unreal source build and GPU).
./Scripts/Docs/unreal_articles/labs/unreal/run.ps1 -Script capture.py
./Scripts/Docs/unreal_articles/labs/unreal/run.ps1 -Script capture_bunny.py
./Scripts/Docs/unreal_articles/labs/unreal/run.ps1 -Script capture_gpu.py
```

Raw Stanford downloads, virtual environments, cached hierarchies and in-repository Unreal build output are covered by `.gitignore`; raw GPU dumps stay in the local temporary directory. Only reviewable, hashed figures and evidence records are published. The small project-local Unreal editor bridge under `labs/unreal/Source/` is authored laboratory code, not Unreal Engine source. No engine source tree or compiled Unreal binaries are included.

The exact engine-capture scripts, modes, camera settings and limitations are documented in `Scripts/Docs/unreal_articles/labs/unreal/README.md` and `scene-room-README.md`. The visible figures are not color-corrected or composited; the GPU atlas displays are labelled as measured array displays and their display transform is recorded in `gpu-atlas-report.json`.

## Deployment

`.github/workflows/pages.yml` runs on pushes to `main` and on `workflow_dispatch`. It checks the generated essays, the offline equation cache, the frozen laboratory evidence and the published links, then uploads `Docs/` with `actions/upload-pages-artifact` and deploys it with `actions/deploy-pages`. `.nojekyll` is included so Pages serves the asset paths directly.

The site is live at [dylanwaken.github.io/Understanding-Unreal](https://dylanwaken.github.io/Understanding-Unreal/). The first publication on 12 November 2026 used the `gh-pages` branch, containing exactly the `Docs/` subtree. GitHub's Pages build and deployment completed successfully.

The same publication can be reproduced from a validated commit without changing the local `main` checkout:

```powershell
$siteCommit = git subtree split --prefix=Docs HEAD
git push origin "${siteCommit}:refs/heads/gh-pages"
```

This preserves the source and authoring tools on `main`, while the publication branch contains only the static site. On a new repository, enabling a custom Actions publishing source through Settings requires repository administration access; pushing source code alone does not grant the workflow that access.

## Migration record

This repository was migrated from the Ardashader working tree at commit `4f55fb953f65ca9db3014e76ffcff2ccd1d89297`; that checkout contained uncommitted authored essay and asset changes, so the source state is a dirty working tree rather than that commit alone. See [`MIGRATION.md`](MIGRATION.md) for the exact source/destination mapping, counts, aggregate hashes, excluded files and scope decisions.
