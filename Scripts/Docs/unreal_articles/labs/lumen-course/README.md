# Forward-design Lumen teaching examples

The primary author wrote the course prose, diagrams, and toy experiments. DeepCluster supplies source research, capture execution, and factual review; it does not write the teaching chapters.

From the repository root:

```powershell
python Scripts/Docs/unreal_articles/labs/lumen-course/generate.py
python Scripts/Docs/unreal_articles/math_typesetting.py --node node
python Scripts/Docs/unreal_articles/build.py --article lumen
python Scripts/Docs/unreal_articles/validate.py --article lumen
```

`generate.py --check` verifies deterministic SVG text and numerical results without changing files. It uses only the Python standard library. All 31 diagrams are marked schematic and contain native SVG text, titles, and descriptions. `experiments.json` records the seed, sample count, expectations, and tolerances. These are mathematical checks, not measured Unreal performance.

`Docs/Unreal/assets/lumen-course/course.js` adds three optional interactive demonstrations: uniform versus cosine hemisphere sampling, two-surface radiosity iteration with an altered transport model, and temporal response after a step change. They use native controls and SVG text. The static article and numerical examples remain readable without JavaScript.

The teaching sources are the five `chapters/lumen-course*.html` files. The older, detailed room experiment is built as `Docs/Unreal/lumen-reference.html`; its captured payload and native-evidence bytes remain unchanged. The published package compresses generated JSON metadata losslessly; the manifest records the actual container paths. Keep scene identities explicit when adding captures: a Sponza screenshot must never be used to imply that a room resource dump came from Sponza.
