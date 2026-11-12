## Replaying a captured surface-cache texel

This example recomputes an actual GPU output from capture `binary-20261112-153914`, `CombineLighting CS` pass 113, unique pass `000100000225e8ce2600`. It follows one dispatched card texel from packed scheduling data to the stored outgoing-radiance bits, and then checks every thread pixel of the same 8x8 dispatch tile. It is a replay of cache production; the ray-hit card interpolation in the preceding section is a separate consumer operation. The accompanying `cache-texel-replay.json` records every selected resource ID, producer version, exact stored format, file hash, raw page record, interpolation neighborhood, scalar value, per-pixel comparison and arithmetic intermediate. `replay_cache_texel.py` is the executable CPU calculation.

This rerun uses the new instrumented capture, not the earlier native frame. Every input is resolved by its unique resource ID and most recent preceding writer in the same render graph; the output is the pass pointer version. No baseline pass or resource ID is reused. A name-only join could silently combine a new direct-lighting atlas with a later radiosity atlas and reconstruct a computation that never happened.

### Captured inputs, exact stored formats, and hashes

The replay reads `SubresourceLayouts.StoredFormat` for the exact `<resource>.v<version>.bin` file before decoding. Native BC formats are exporter-decoded, not compressed blocks, and the stored container may differ from the native format.

| Operand | Resource / version | Native → stored | Bytes | SHA-256 (prefix) | Producer |
|---|---|---|---|---|---|
| `Lumen.CardTiles` | `000100000228d4dbb2f8` / `000100000228d4dbbb10` | buffer → raw bytes | 33124 | `52da233736d66588` | 99 SpliceCardPagesIntoTiles |
| `Lumen.PageBuffer` | `00010000022925049708` / `000100000228d4db7290` | buffer → raw bytes | 81920 | `c25e4ac49a70d884` | 96 Build cards update list |
| `Lumen.DispatchCardTilesIndirectArgs` | `000100000228d4dbcca8` / `000100000228d4dbce98` | buffer → raw bytes | 24 | `73204376c2f7021c` | 100 InitializeCardTileIndirectArgs |
| `Lumen.SceneAlbedo` | `00010000022925049890.mip0` / `000100000228bb0f28a0` | PF_BC7 → PF_FloatRGBA | 134217728 | `22161b24be7b48ac` | 87 CompressToSurfaceCache Albedo |
| `Lumen.SceneEmissive` | `00010000022925049e10.mip0` / `000100000228bb0f74c0` | PF_BC6H → PF_FloatRGBA | 134217728 | `6fe629d8f6f61dc9` | 89 CompressToSurfaceCache Emissive |
| `Lumen.SceneDirectLighting` | `0001000002292504a390.mip0` / `0001000002292507f1f0` | PF_FloatR11G11B10 → PF_FloatR11G11B10 | 67108864 | `3395ad2b61a76803` | 112 Batched lights |
| `Lumen.Radiosity.SceneIndirectLighting` | `0001000002292504a650.mip0` / `000100000228bb0f9aa8` | PF_FloatR11G11B10 → PF_FloatR11G11B10 | 67108864 | `a1d016fd3b56712e` | 90 CopyCardCaptureLightingToAtlas |
| `Lumen.SceneFinalLighting` | `0001000002292504abd0.mip0` / `000100000225e8ce2600` | PF_FloatR11G11B10 → PF_FloatR11G11B10 | 67108864 | `dc6bff613318168e` | 113 CombineLighting CS |


The pass payload, `View` payload and captured `LumenCardScene` RDG uniform payload are also hashed in the JSON (`parameterPayloads`). Decode sizes are asserted against the exact stored format and the on-disk file size, so a half/float32 mismatch or a truncated readback fails immediately instead of producing plausible numbers.

### Captured RDG uniform payload, not allocation inference

`LumenCardScene` is the captured `RDGUniformBufferParameters` payload `000100000228bb0fe610` (metadata `000100007ffb89269ff0`, 144 bytes, layout `FLumenCardScene`) linked to pass 113. `PhysicalAtlasSize` at offset 24 is `(4096, 4096)` and `InvPhysicalAtlasSize` at offset 32 is `(0.000244140625, 0.000244140625)`; both were checked against the actual 4096×4096 lighting atlases. No allocation-inferred reciprocal is used. `IndirectLightingAtlasDownsampleFactor` is 1.0, so the equal-resolution branch is the recorded one.

### Dispatch, expected shader route, page record, and texture address

The pass event is `CombineLighting CS` with parameter struct `FParameters`, and the unique output descriptor is `Lumen.SceneFinalLighting`. All required input edges were found: `Lumen.CardTiles`, `Lumen.PageBuffer`, `Lumen.SceneAlbedo`, `Lumen.SceneEmissive`, `Lumen.SceneDirectLighting`, `Lumen.Radiosity.SceneIndirectLighting`, `Lumen.DispatchCardTilesIndirectArgs`. The indirect-argument buffer contains `[9, 1, 1, 548, 1, 1]`; this kernel reads the second three-word record at byte offset 12, giving dispatch groups `[548, 1, 1]` against 8281 available tile words. The selected group is `[0, 0, 0]` and its local thread is `[3, 4, 0]`. The `CardTiles` word is `35`: low 24 bits select page 35, and the two high nibbles select tile `[0, 0]`. `Lumen.PageBuffer` supplies the 80-byte page record at byte offset 2800. Integer fields are bitcast from their float4 storage, not converted from tiny floating-point values.

| Decoded field | Captured value |
|---|---|
| `CardIndex` | `35` |
| `ResLevelPageTableOffset` | `35` |
| `SizeInTexels` | `[8.0, 8.0]` |
| `CardUVRect` | `[0.0, 0.0, 1.0, 1.0]` |
| `PhysicalAtlasUVRect` | `[0.259765625, 0.0, 0.26171875, 0.001953125]` |
| `CardUVTexelScale` | `[0.125, 0.125]` |
| `ResLevelSizeInTiles` | `[1, 1]` |
| `LastDirectLightingUpdateFrameIndex` | `871` |
| `LastIndirectLightingUpdateFrameIndex` | `871` |
| `IndirectLightingTemporalIndex` | `870` |
| `DirectLightingTemporalIndex` | `870` |

The physical page origin comes from the page UV rectangle times atlas extent, rounded as the shader specifies. Adding `8 * tileCoord + localThread.xy` gives atlas texel `[1067, 4]`. Adding a half-texel center and dividing by atlas extent gives UV `[0.2606201171875, 0.0010986328125]`. All four texture reads use that UV in this equal-resolution case. Their bilinear neighborhood weights are `[1.0, 0.0, 0.0, 0.0]`: the coordinate lands exactly at the center of a stored texel. The zero-weight neighbors are still retained in the replay record so the sampling operation is inspectable.

The captured sampler pointer is tied to the pinned C++ assignment `TStaticSamplerState<SF_Bilinear, AM_Clamp, AM_Clamp, AM_Clamp>`. Its source initializer specifies bilinear filtering, clamp on U/V/W, mip bias 0, anisotropy 1, border color 0, comparison `SCF_Never`, minimum LOD 0 and maximum LOD `FLT_MAX`. This initializer is established from the executed source path; the native pointer bytes alone do not encode it. It is recorded separately from the captured pointer, not inferred from texture bytes.

### Decode the operands and apply the transport equation

In this capture the sampled albedo is native `PF_BC7` stored as `PF_FloatRGBA`; the emissive is native `PF_BC6H` stored as `PF_FloatRGBA`. Direct, indirect and output lighting are `PF_FloatR11G11B10` stored as `PF_FloatR11G11B10`. The decoder branches on the recorded stored format, so a BC view exported as float32 would not be misread as half, and vice versa. For BC7 the expanded half channels are rounded back to the exact 8-bit UNORM decoded value; BC6H remains HDR float. No compressed block is sampled directly.

| Operand | Sampled RGB | Meaning |
|---|---|---|
| Encoded albedo | `(0.854901969, 0.815686285, 0.713725507)` | Square-root encoding of effective cache albedo |
| DirectLighting | `(16.75, 16.75, 17)` | Cached pre-exposed irradiance |
| IndirectLighting | `(2.375, 1.609375, 1.1875)` | Cached pre-exposed irradiance |
| Emissive | `(0, 0, 0)` | Cached pre-exposed emitted radiance |


`DiffuseColorBoost` is 1.0 and `View.DiffuseOverrideParameter` is `[0.0, 0.0, 0.0, 1.0]`, so this sample uses the squared encoded RGB without an additional boost or override. Its effective albedo is `(0.730857372, 0.665344119, 0.509404123)`. The shader computes

$$
L_\mathrm{cache}^{(\mathrm{pre},c)}=\frac{\rho_\mathrm{cache}}{\pi}(E_\mathrm{direct}^{(\mathrm{pre},c)}+E_\mathrm{indirect}^{(\mathrm{pre},c)})+L_e^{(\mathrm{pre},c)},
$$

which gives `(4.44922304, 3.8882513, 2.94907331)` before output quantization. Albedo is dimensionless, $\rho/\pi$ is the Lambertian BRDF in $\mathrm{sr}^{-1}$, irradiance becomes radiance through that factor, and the radiometric atlas operands use the cache scale $X^{(\mathrm{pre},c)}=e_c X$. The captured direct-light producer (pass 112) supplies `CachedLightingPreExposure = 0.0625`. Pass 167 also records that scale and its reciprocal `16`. The two parameter files and byte offsets are in `cache-exposure-evidence.json`. `View.PreExposure` is 0.0625, but this kernel does not multiply by it again or apply a display tone curve.

### Follow the random seed and output quantization

The view's recorded `StateFrameIndexMod8` is 7; use this uniform rather than deriving it from the render-graph frame label. The shader adds 2 and hashes `[1067, 4, 9]` with its 32-bit wrapping PCG-style permutation. The resulting first 16-bit component is 23960, so $u=23960/65536=0.365600585938$. The replay includes every integer state of that permutation.

The recorded target relative precision vector is `(0.015625, 0.015625, 0.03125)`. Multiply the prequantized radiance by that vector, then clear the FP32 mantissa bits to obtain the exponent-dependent step `(0.0625, 0.03125, 0.0625)`. Adding $u$ times that step gives `(4.47207308, 3.89967632, 2.97192335)` before the formatted UAV store. The measured stored packed word is **2212619335**, decoding to **`(4.4375, 3.875, 2.9375)`**.

The CPU truncation model predicts that exact packed word for the selected texel. Across 512 sampled dispatched tiles it matches 512/512 outputs; a nearest-rounding model matches 304/512. The whole selected 8×8 dispatch tile was then replayed thread by thread: 64/64 pixels match the truncating store model, against 11/64 for nearest rounding. Every per-pixel actual word, predicted word, atlas coordinate and float error is in the JSON `comparisons` and `tileValidation` arrays, and the aggregate counts are recomputed from those arrays by `verify_replay.py`.

The truncating model's channel error against the stored RGB has maximum 0.0489578247 and mean 0.00177827899 over 1536 channels. The corresponding nearest-rounding error is maximum 0.0625 and mean 0.00186833491. The nonzero truncating error is the expected quantization residual, not a packed-word mismatch. This is evidence for the observed store path and finite values, not a claim about every GPU format conversion. The positive dither followed by truncation behaves like stochastic rounding; it is not symmetric zero-mean noise added to the image. Quantized output can lie above or below the original undithered value. The discrete random grid and extreme floating-point cases need separate analysis.

### Provenance gaps

No operand or uniform payload needed by this replay is missing: the captured `LumenCardScene` RDG payload was present and consistent, and all seven input resource payloads and the output payload resolve to exact same-run versions. Other capture-wide coverage gaps (for example capture-report parameter gaps) are outside this texel's operand set and are not silently substituted here.

### Reproduction

The replay needs only Python 3, NumPy, the capture directory, and this directory's scripts. NumPy was supplied by the reviewed replay's local `deps` directory on `PYTHONPATH`; no binary is packaged here. The command used was:

```text
python -m pip install -r requirements.txt
python replay_cache_texel.py --capture <DumpGPU-directory> --output . --pass-id 113
```

`write_replay_chapter.py` and `draw_replay_flow.py` regenerate this section and the SVG from `cache-texel-replay.json`; `verify_replay.py` re-hashes every recorded input and output, checks decode sizes against stored formats, recomputes match counts and error metrics from the per-pixel arrays, and writes a machine-readable verification record.

The relevant source chain is `LumenSceneLighting.cpp:181–206` (bindings and dispatch), `LumenCardTile.ush:19–25` (packed tile), `LumenCardCommon.ush:170–199` (page data), `LumenSceneLighting.usf:477–506` (addresses and kernel), `Engine/Shaders/Private/Lumen/SurfaceCache/LumenSurfaceCache.ush:44–60` (albedo and transport), `LumenFloatQuantization.ush:24–28`, `RandomPCG.ush:12–43,107–110`, and `Quantization.ush:13–22` (dither). These anchors refer to the pinned engine source; the capture instrumentation is recorded separately.
