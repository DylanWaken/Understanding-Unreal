# Deferred direct-light replay — final capture, graph 0001 / pass 249 / pixel (640,650)

This directory contains the final-capture CPU replay and its verification for the one
actual screen-space deferred rect-light pass:

- capture `binary-20261112-153914` (project `UnrealLab`, UE 5.8.1 Editor Development, D3D12 SM6, RTX PRO 6000 Blackwell);
- graph `0001`, pass **249**, pointer `000100000228e3a88880`, event
  `RenderLight Light::StandardDeferred: Ceiling area light`;
- pixel **(640, 650)** in the 1280x720 view inside a 1280x888 buffer;
- legacy `DefaultLit` GBuffer path, one rect area light, no Substrate/MegaLights.

**Result:** the CPU contribution is `(1.2399915953, 1.2399915953, 1.1766082763)`;
the observed FP16 delta is `(1.2392578125, 1.2392578125, 1.1767578125)`. The maximum
unrounded residual is **0.7514 ULP** against a **1.0 ULP** tolerance, so the replay
**passes**. The predicted FP16 codeword is +1 ULP in R/G and exact in B; the residual
and the exact gaps are documented in `contract.md` / `numerical-report.md`.

## Files

| File | Purpose |
|---|---|
| `replay_cpu.py` | portable CPU replay; `--capture` is required, `--output` writes the JSON report |
| `verify_replay.py` | independent re-run, file-hash, LTC/LTC.h, VSM-boundary and FP16 checks |
| `write_contract.py` | builds `contract.json`, `contract.md`, `numerical-report.md`, `reproducibility.json` from the frozen report |
| `contract.json` / `contract.md` | frozen machine-readable / human contract |
| `report.json` | frozen numerical report (actual captured values, provenance, gaps) |
| `verification.json` | 17/17 independent checks, PASS |
| `numerical-report.md` | detailed per-channel numbers and evidence |
| `reproducibility.json` | command/hash/environment freeze |
| `requirements.txt` | NumPy only |

## Quick start

```text
python -m pip install -r analysis/deferred-replay/requirements.txt

python analysis/deferred-replay/replay_cpu.py \
  --capture <capture-root> \
  --output analysis/deferred-replay/report.json

python analysis/deferred-replay/verify_replay.py \
  --capture <capture-root> \
  --report analysis/deferred-replay/report.json \
  --output analysis/deferred-replay/verification.json \
  --ltc-header <engine>/Engine/Source/Runtime/Renderer/Private/LTC.h \
  --final-dir <final>
```

`<capture-root>` is any copy of the dump root that contains `Base/`, `Resources/`,
`Structures/`, and `StructuresMetadata/` (the tested root is the
`binary-20261112-153914` dump root; pass a copy of it with `--capture`). All paths in the
report are relative to that root; the script uses `--capture` and does not hardcode an
absolute capture path.

## What is actual, what is derived, what is not replayed

**Actual captured bytes used:**

- `PS.View` — `FViewUniformShaderParameters`, 6752 B, content `00010000022880566180`;
  exact `PreExposure = 0.0625`, `ScreenPositionScaleBias`, `ScreenToTranslatedWorld`,
  `InvDeviceZToWorldZTransform`.
- `PS.DeferredLight` — `FDeferredLightUniformStruct`, 192 B, content
  `000100000228e3a88830`; exact translated light position `(0,670,133)`, color
  `979.4150390625`, `InvRadius`, `SourceRadius/Length`, `ShadowedBits=3`,
  `ShadowMapChannelMask=(0,0,0,1)`.
- Versioned `SceneColor` before/after (FP16), GBufferA/B/C/D, SceneDepthZ mip0, and
  `Shadow.Virtual.MaskBits` (dword0 `0x0000000F` → visibility 1.0).
- Actual shader-bound `GGX.LTCMat` / `GGX.LTCAmp` native textures:
  - `Resources/000100000228d23f80d0.linear.mip0.v000100000225fb9fef70.bin` (`PF_FloatRGBA`, half4);
  - `Resources/000100000228d23f8ad0.linear.mip0.v000100000225fb9fef70.bin` (`PF_G16R16F`, half2).
  Their full 64×64 tables are bit-identical to the quantized `LTC.h` tables (0 mismatched
  half codewords). The sampler object is the same pointer as
  `View.SharedBilinearClampedSampler` (`SF_Bilinear`, clamp), sampled with `SampleLevel(...,0)`.

**Derived, not actual-byte:**

- The active `FDeferredLightPS` permutation domain is derived from the captured pass
  parameters/CVars and source (rect source shape, no source texture/IES/anisotropy/
  lighting channels, VSM mask on, no transmission/hair/light-function material, no
  cloud/atmosphere, no visualize culling, energy conservation 0). No compiled PSO/shader
  hash was captured; this remains gap G3.
- Sampler *state* is pointer-only (`StateCaptured=false`); the bilinear-clamp state is
  proven by pointer equality to `SharedBilinearClampedSampler` plus the source initializer
  (`SceneManagement.cpp:1277`), not by a captured sampler object.

**Explicitly not replayed / not claimed:**

- The VSM/SMRT producer (pass 248) is **not** replayed. Only the final quantized 4-bit
  `MaskBits` is consumed. Page table, page flags, page receiver masks, physical page pool,
  projection data, light grid, and culled-light grid are retained as operands in the
  contract but are not sampled. Visibility is exactly 1.0 only because `15/15 = 1.0`; the
  unquantized shadow factor is not independently proven (gap G2).
- No GPU work, no engine edits, no evidence-repo edits. Capture access is read-only.

## Stored-format / provenance rule

Every texture is decoded from its `SubresourceLayouts.StoredFormat` for the exact version
file, **never** from the descriptor's `NativeFormat`. A BC/other native view that was
exported as float32 is decoded as `PF_A32B32G32R32F`; ordinary captures that retain native
half/float16 are decoded as `PF_FloatRGBA`/`PF_G16R16F`. `PF_B8G8R8A8` is decoded in memory
order `B,G,R,A` and the GBuffer channel order is preserved. The LTC files are decoded as
half4 / half2 with file channel order preserved:

- Mat: `PF_FloatRGBA` half4 → `M.x, M.y, M.z, M.w`;
- Amp: `PF_G16R16F` half2 → `A.x, A.y`.

`SubresourceLayouts` marks the LTC exports `StoredBytesAreNativeStorage=false`,
`NativeValueInterpretation=DecodedFloat`, `BitExact=false`, `ValuePreserving=true`; the
report preserves that decoded-value provenance (gap G6).

## Frozen hashes

The canonical report is `report.json` with SHA-256
`4dacc34ac987e31ab212108d4e94eacd996a9de66c101a8badce2b3d38674fde`; the verification is
`verification.json` with SHA-256
`abddacbdbc66e0d9d92dc120c3987ae3cd3e1846f48d02dd651fb4515fa00246`. The exact hashes are
also in `contract.md` / `reproducibility.json`. Re-running the scripts may change the
report/verification bytes (e.g. formatting) but must not change the numerical result; the
verifier checks the frozen report against a fresh run.
