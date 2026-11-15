# Sponza static context decoder — decode report

Run: `sponza-20261115-083654` (frame 302). Raw dump: `output/gpu-dump/raw/sponza-20261115-083654` (read-only).
Decoder: `scripts/decode_context.py`; rerun with `python scripts/decode_context.py --stage all`.
All native values below are captured bytes/words from the named resource versions or shader parameter payloads. Display rasters are regenerated from the same native values; no value in this report is estimated from an image.

## Acceptance checks

| # | Correction | Captured identity / field | Corrected value | Validation |
|---|---|---|---|---|
| 1 | ShadingModelID is the alpha low nibble | GBufferB `00040000020b9d701710` (pass 697), alpha byte | A/B alpha `0xB1` -> model 1; C alpha `0xA1` -> model 1; high nibble 11/11/10 is SelectiveOutputMask | `gbuffer_shading_model_low_nibble` in `data/decode-verify.json`; raw alpha and both nibbles are in `data/orientation.json` |
| 2 | `history_minus_current` sign | `DiffuseIndirect` current `000400000208f36d6770` / imported history v0 | A history 1.296875 - current 1.28125 = +0.015625; C 0.609375 - 0.515625 = +0.09375 | `history_minus_current_is_history_minus_current`; definition field says `history - current` |
| 3 | Packed temporal frame count | pass 762 `FParameters.MaxFramesAccumulated`, `Structures/000400000208f36dfaa0.bin`, offset 460 | float32 = 10; raw byte 15 has high nibble 0 and low nibble 15 -> decoded 10 frames, not 15 | `diffuse_frames_raw15_decoded10`; raw byte, both nibbles, decoded value and MaxFrames are retained per receiver and in `data/figure-native/diffuse_frames_A.npz` |
| 4 | Reflection frame UNORM packing | pass 826 `NumFramesAccumulated` `00040000020b9dcd8030`, `LumenReflectionDenoiserCommon.ush:51-59` | raw 96 -> 96/255*32 = 12.0470588 effective frames, not 96; A/B raw 0 | `reflection_C_raw96_effective_12_0470588`; figure `reflection_frames_C.png` uses raw/255*32 units |
| 5 | Adaptive placement bounded by allocator | pass 706 count buffer `0004000002090c6ba5c0` v`00040000020bd35c1b10`; header `0004000002090c6ba8d0` | count = 117; header sum = 117 over 117 tiles; finite storage = 3809 (3569 uniform + 240 adaptive); active = 3686 (3569 + 117); stale adaptive = 123 | `adaptive_allocation_bounded`; first 117 adaptive indices map bijectively to the 117 header tiles; rayinfo is never used as activity evidence |
| 6 | Structured trace slot vs regular reconstructed field | trace `TraceRadiance` `00040000020c1328ca80` (pass 747); filtered `ScreenProbeRadiance` `0004000002094be0c130` (pass 748) | `screenprobe_rays_A.png` now colours by `radiance_trace_native`; filtered radiance is shown only in the separately labelled regular-field panel | `rays_figure_uses_structured_trace_radiance`; `screenprobe_rays_A.npz` keeps `ray_trace_radiance_native` and `ray_filtered_radiance_same_integer_slot_native` separately |
| 7 | SH units | SH ambient `000400000208f3733bf0` / directional `000400000208f3733eb0` (pass 753) | stored SH3 are sphere-average radiance; raw R = EvaluateSHIrradiance; physical irradiance = 4*pi*R; unit-albedo Lambert response = 4*R. Figure displays 4*R with fixed white point 2.8, not physical irradiance | `probe_A/B/C_sh_units` and `sh_preview_units_explicit`; `screenprobe_sh_A.npz` stores raw, 4*R and 4*pi*R |
| 8 | Inverse display mapping order | all tone-mapped figure mappings | inverse is first `sRGB_EOTF(code/255) -> t`, then `L = t/(1-t)`; the old note had the order reversed | `tone_inverse_formula_order`; identical string is in every `TONE_NOTE` mapping and in `data/figure-manifest.json` |
| 9 | Raster text and empty-figure policy | figure generator and manifest | no `ImageDraw.text`/`.text(` call; every emitted figure is non-empty and has native NPZ or is the captured screenshot; `material_C.png` and the dark-grey invalid reflection pixels are retained | `no_raster_text_calls`, per-figure existence/dimension/NPZ checks; `decode-verify.json` |

## Captured values used by the checks

- Temporal frame raw bytes at A: history `0x0f` (high 0, low 15) -> `10`; new history `0x0f` -> `10`.
- MaxFramesAccumulated parameter: `10` from pass 762; shader source `D:\UnrealEngine\Engine\Shaders\Private\Lumen\LumenScreenProbeGatherTemporal.ush` lines 72-91.
- Reflection at C: raw byte `96`, UNORM `0.376470588`, effective `12.0470588`; raw history byte `96`.
- Adaptive count buffer: `Resources/0004000002090c6ba5c0.v00040000020bd35c1b10.bin`, bytes `4`, raw word `117`, sha256 `229ba167c2752f2f401b2f6b862630b27302bf137b95b5ec114e6f38bfa66b47`.
- Adaptive header: `Resources/0004000002090c6ba8d0.mip0.v00040000020bd35c1b10.bin`, extent [80, 56], nonzero 117, sum 117, sha256 `5cb958b9d8c7ee0d206e2506793059e48e1d24ce7f1dffc3dfd9ad7522f66f77`.
- Structured ray example (probe A, trace cell [0,0]): TraceRadiance `[9.875, 12.625, 19.5]`, filtered regular-field value at the same integer slot `[0.0, 0.0, 0.0]`. These are different layouts and must not be treated as the same ray.

## Figure inventory

22 figures are listed in `data/figure-manifest.json`. Each entry carries `source`, `mapping`, `native_units`, `native_stats`, `finite`, `nonempty`, and a `native_npz` (except the captured screenshot). No raster image contains embedded text.

## Truthful unavailable evidence / gaps

- No shader-local per-thread adaptive-placement dispatch is captured. Activity is bounded by the captured `NumAdaptiveScreenProbes` count and independently corroborated by the pass-706 header tile map; per-index tile membership is validated as a bijection, not read from a captured per-index assignment table.
- The pass-706 `NumAdaptiveScreenProbes` 4-byte buffer has no `DumpReadbackStatus` record; the payload file exists in `Resources`, is hashed, and agrees with the header sum. The pass-707 six dispatch groups are captured as raw words but are not individually labelled, so they are not used to infer activity.
- The regular reconstructed field (`ScreenProbeRadiance`) does not encode which structured trace slot produced each filtered value. Filtered radiance is therefore reported in its own layout and is not mapped back to individual rays.
- No per-pixel screen-probe interpolation weights, temporal blend factors, disocclusion decisions, or reflection BRDF/ray choices are captured; those calculations remain shader-local.
- `EvaluateSHIrradiance` output is a replay from captured SH3 coefficients. No captured per-pixel physical irradiance texture exists, and no pre-tonemap linear frame is captured; physical irradiance and 4*R are derived only from the stored SH coefficients.
- Reflection `NumFramesAccumulated` is an encoded UNORM byte; only the decoded effective value is recovered. The encoder cannot distinguish values that quantize to the same byte.
- The raw GPU capture/package was not modified. No GPU work was executed in this step.

Observed-vs-replay: all ray directions, SH evaluations and cache queries in these records are declared-query replays from captured inputs; only the named captured resource words and shader parameters are observed values.


## Machine verification


`data/decode-verify.json`: 252/252 checks passed, 0 failed, overall ok = `True`.

All machine checks passed.
