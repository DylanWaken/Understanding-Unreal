# Captured Lumen cache-texel replay

`replay_cache_texel.py` reads a native or instrumented UE DumpGPU capture and independently recomputes `CombineLumenSceneLightingCS`. It writes a complete selected-texel arithmetic trace and compares packed output words for one texel from each of up to 512 dispatched tiles.

Requirements: Python 3 and NumPy. Keep dependencies in a virtual environment. The checked run used Python 3.14 and NumPy 2.5.4 on Windows; this is CPU analysis and does not launch Unreal or modify the engine.

```text
python -m venv .venv
.venv/Scripts/python -m pip install numpy
.venv/Scripts/python replay_cache_texel.py PATH_TO_CAPTURE OUTPUT_DIRECTORY
```

Optional `--pass-id N` selects a specific CombineLighting pass. The default chooses the first such pass; names can recur in different graphs, so all resource references are resolved by capture IDs and producer versions. The brightest sampled candidate is selected independently of whether its computed output matches. An unmatched selected texel fails explicitly after saving the diagnostic JSON.

To regenerate the prose, place `write_replay_chapter.py` beside the generated `cache-texel-replay.json` and run it. It creates `cache-texel-replay.md` from the actual recorded results.

The script supports the captured equal-resolution lighting atlases, boost=1 branch, BC7/BC6H decoded readbacks and finite R11G11B10 values. It asserts its branch assumptions. It is not a general implementation of all UE shader permutations or all GPU floating-point conversion corner cases. Its output preserves missing capture operands as explicit provenance gaps; source-derived sampler construction is recorded separately from captured pointer bytes.

No Unreal shader source is copied into this replay. Original CPU expressions implement the documented algorithms and name source anchors for comparison.
