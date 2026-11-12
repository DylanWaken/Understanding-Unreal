# Reproduce the captured arithmetic

Run these commands from the repository root. Python 3.14.6 and NumPy 2.5.4 were used for the numerical replays. Package verification, reconstruction and the static site do not require NumPy or a GPU.

## Inspect or reconstruct the frame

```powershell
python -m http.server 8768 --directory Docs
```

Open `http://localhost:8768/Unreal/frame-explorer.html`. The explorer reads compressed chunks on demand. It shows exported storage values separately from the source-defined physical interpretation.

In another terminal, verify the complete package and reconstruct its original file layout:

```powershell
python Scripts/Docs/unreal_articles/labs/lumen-frame/tools/package_frame.py verify --package Docs/Unreal/assets/lumen-frame
python Scripts/Docs/unreal_articles/labs/lumen-frame/tools/package_frame.py reconstruct --package Docs/Unreal/assets/lumen-frame --dest Captures/reconstructed-frame
```

The complete reconstruction contains 18,570,711,246 bytes across 7,912 files. The package preserves every exported binary and native metadata file; completion and native consumer-time identity remain separate evidence states.

## Recompute the four worked examples

```powershell
python -m pip install -r Scripts/Docs/unreal_articles/labs/lumen-frame/replays/world-cache/requirements.txt

python Scripts/Docs/unreal_articles/labs/lumen-frame/replays/deferred/replay_cpu.py --capture Captures/reconstructed-frame --output Captures/replays/deferred/report.json
python Scripts/Docs/unreal_articles/labs/lumen-frame/replays/deferred/verify_replay.py --capture Captures/reconstructed-frame --report Captures/replays/deferred/report.json --output Captures/replays/deferred/verification.json

python Scripts/Docs/unreal_articles/labs/lumen-frame/replays/surface-cache/replay_cache_texel.py --capture Captures/reconstructed-frame --output Captures/replays/surface-cache --pass-id 113
python Scripts/Docs/unreal_articles/labs/lumen-frame/replays/surface-cache/verify_replay.py --capture Captures/reconstructed-frame --output Captures/replays/surface-cache/verification.json

python Scripts/Docs/unreal_articles/labs/lumen-frame/replays/world-cache/replay_world_cache.py --capture Captures/reconstructed-frame --output Captures/replays/world-cache --pass-id 167
python Scripts/Docs/unreal_articles/labs/lumen-frame/replays/world-cache/verify_world_replay.py --capture Captures/reconstructed-frame --output Captures/replays/world-cache/verification.json

python Scripts/Docs/unreal_articles/labs/lumen-frame/replays/gdf/replay_gdf_march.py --capture Captures/reconstructed-frame --output Captures/replays/gdf
```

Each script reads numerical inputs from the reconstructed capture. Frozen records provide source annotations and historical run metadata, not replacement numerical operands. An optional pinned engine checkout enables fresh source checks; the portable numerical replay uses the captured LTC textures directly.

| Example | Compared boundary |
|---|---|
| Deferred area light | One DefaultLit pixel, actual native LTC values and the captured final VSM mask; the floating-point contribution is within one FP16 store ULP of the observed delta. This does not rerun the SMRT mask producer. |
| Surface cache | Dispatch, card-page addressing, material/lighting samples, pre-exposed combine, dither and packed output. The selected packed word matches exactly. |
| World cache | Actual compacted ray, BlueNoise direction, eight neighbors, 32 radiance taps, parallax correction and nested interpolation; the decoded cache hit distance matches exactly. |
| Global distance field | The preceding march, interval, page address and all interpolation taps under the recorded earlier native snapshot. The final hit word matches; radiance is compared within one store ULP. Native snapshot timing remains conditional. |

The portable aggregate check reconstructs only the operand files needed by each replay into temporary directories, while verifying every payload in the full package:

```powershell
python Scripts/Docs/unreal_articles/labs/lumen-frame/verify_frame_evidence.py --replay
```

See the essay and each replay's JSON for validity rules, source-derived sampler state, inactive permutations, tolerances and unresolved timing. A passing arithmetic check does not erase those boundaries.

## Capture another frame

Use the [portable room project](../lumen-capture/baseline/reproduce/), the [diagnostic patch and build provenance](provenance/), and the [instrumented capture recipe](capture/). A new engine run has new resource identities and may select different probe updates. Generate a new package and new replay records together; do not combine its payloads with the frozen producer IDs in this publication.
