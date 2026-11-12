# Surface-cache tile replay against the new capture

This directory reruns the reviewed cache-texel replay against the **new instrumented
capture**, not the earlier native frame and not the old baseline IDs.

- Capture: `binary-20261112-153914`
- Pass: `CombineLighting CS`, Id 113, unique pointer `000100000225e8ce2600`
- Kernel: `CombineLumenSceneLightingCS`
- Scope: one texel per dispatched 8×8 tile for up to 512 tiles, plus every thread
  pixel of the selected 8x8 tile.

## Result

| Measurement | Result |
|---|---|
| Sampled texels (one per dispatched tile) | 512 |
| Exact R11G11B10 packed writes, truncating model | **512 / 512** |
| Exact R11G11B10 packed writes, nearest-rounding model | 304 / 512 |
| Selected 8x8 tile pixels, truncating model | **64 / 64** |
| Selected 8x8 tile pixels, nearest-rounding model | 11 / 64 |
| Truncating-model channel error (max / mean) | 0.0489578247 / 0.0017782790 |
| Nearest-model channel error (max / mean) | 0.0625 / 0.0018683349 |
| Missing operands / RDG payload gaps for this texel | none |

The selected texel is group 0, atlas texel `(1067, 4)`, page 35 at byte 2800,
stored word `2212619335` decoding to `(4.4375, 3.875, 2.9375)`; both the truncating
prediction and the actual packed word are `2212619335`.

## Provenance decisions in this rerun

- **Exact stored format.** Every texture operand is decoded from the
  `SubresourceLayouts.json` entry for the exact `<resource>.v<version>.bin` file.
  In this capture `Lumen.SceneAlbedo` (native `PF_BC7`) and `Lumen.SceneEmissive`
  (native `PF_BC6H`) are stored as `PF_FloatRGBA`; the lighting atlases and the
  output are stored as `PF_FloatR11G11B10`. The decoder also accepts
  `PF_A32B32G32R32F`, `PF_R32_FLOAT`, `PF_R32_UINT` and `PF_R32_SINT`, so a BC view
  exported as float32 would not be silently read as half.
- **Captured RDG uniform payload.** `LumenCardScene` is read from the
  `RDGUniformBufferParameters` mapping for the pass: content
  `000100000228bb0fe610`, metadata `000100007ffb89269ff0`, 144 bytes, layout
  `FLumenCardScene`. `PhysicalAtlasSize` = `(4096, 4096)` and
  `InvPhysicalAtlasSize` = `(0.000244140625, 0.000244140625)` were checked against
  the actual direct-lighting atlas. No allocation-inferred reciprocal is used.
  Null, missing and `ContentAvailable=false` mappings are handled explicitly and
  recorded as gaps if ever encountered; this pass has the captured payload.
- **Same-run versions.** Each input is the most recent preceding writer of the same
  render graph, resolved by unique resource ID; the output version is the pass
  pointer. `Lumen.SceneDirectLighting` deliberately resolves to the pass-112
  `Batched lights` version `0001000002292507f1f0`, not the later pass-122 version.
- **Output route and indirect args.** Event `CombineLighting CS`, parameter struct
  `FParameters`, output `Lumen.SceneFinalLighting`, all required input edges present.
  Indirect args are `[9, 1, 1, 548, 1, 1]`; the kernel reads the second record at
  byte offset 12, giving 548 groups against 8281 tile words.
- **Per-pixel comparisons.** `comparisons` records each sampled texel's actual and
  predicted packed words, match flags and channel errors. `tileValidation.pixels`
  records the same for all 64 threads of the selected tile. Aggregate counts and
  error metrics are recomputed from those arrays by `verify_replay.py`.

## Files

- `replay_cache_texel.py` - executable CPU replay (adapted from
  `UnrealUnderstanding-Engineering-Reviewed/replay`, no Unreal source copied).
- `write_replay_chapter.py` - regenerates `cache-texel-replay.md` from the JSON.
- `draw_replay_flow.py` - regenerates `cache-texel-flow.svg` from the JSON.
- `verify_replay.py` - re-hashes every input and artifact, checks exact layouts and
  decode sizes, re-resolves versions, re-decodes the captured RDG payload, and
  recomputes all aggregate metrics; writes `verification.json`.
- `cache-texel-replay.json` - complete trace, inputs, per-pixel arrays and hashes.
- `cache-texel-replay.md` - prose chapter.
- `cache-texel-flow.svg` - one-page arithmetic diagram.
- `reproducibility.json` - byte-identical rerun from a clean working directory.
- `README-provenance.md` - original reviewed-script README, kept for attribution.

## Reproduce

Use Python 3 and NumPy. The checked run used Python 3.14.6 and NumPy 2.5.4. From this directory, install dependencies into a virtual environment and pass the reconstructed capture directory and an output directory explicitly.

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python replay_cache_texel.py --capture <DumpGPU-directory> --output ./rerun --pass-id 113
.venv/Scripts/python write_replay_chapter.py ./rerun
.venv/Scripts/python draw_replay_flow.py ./rerun
.venv/Scripts/python verify_replay.py --capture <DumpGPU-directory> --output ./rerun/verification.json
```

The verifier re-hashes the captured operands and checks the frozen replay. The frozen JSON preserves the original capture path in its provenance field. Its numeric results and input hashes can be compared with a rerun made from a reconstructed copy at a different path.

## Capture boundaries

All seven input resource payloads and the output payload are present and hashed. The pass, View and LumenCardScene uniform payloads are also available. The producer and consumer exposure fields are recorded in `cache-exposure-evidence.json`.

The capture records View.VTFeedbackBuffer as an unexported native UAV binding for pass 113. The combine equation has no read of that feedback buffer. The later VT feedback compaction SRV is a separate captured version.

## Limits

This is CPU analysis; no GPU workload was executed. It samples one texel from each
of 512 dispatched tiles and all 64 threads of one selected tile, not every pixel of
every 4096x4096 atlas. The compact float encoder is validated only on the finite
captured values here; it is not a general NaN/overflow/subnormal GPU conversion
emulator. Capture-wide coverage gaps outside this texel's operand set are not
substituted or claimed closed.
