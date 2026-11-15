# Sponza receiver decode

Status: **pass**. Read-only decode of the frozen native dump; no GPU work, no engine/repo/Sponza writes, and no acceleration-structure claims.

## Source

- Dump: `D:\UnrealUnderstanding-Sponza\output\gpu-dump\raw\sponza-20261115-083654`
- Analysis: `D:\UnrealUnderstanding-Sponza\output\gpu-dump\analysis-20261115-083654.json`
- Run id: `20261115-083654`
- Engine: `5.8.1` build `b4effda9-1b7d-4e62-b9a6-cb99e41a70c9`
- Presented scene graph: index **3** (last of 3 scene graphs + 1 Slate graph), passes 593..885 (293 passes), Frame 302 (Delta=0.013059s)

## Presented view

| Item | Value |
| --- | --- |
| View resource | `ViewData.PrimaryViews` `00040000020bef07f840` |
| View resource file | `Resources/00040000020bef07f840.v00000000000000000000.bin` (1056 B) |
| View uniform content | `Structures/00040000020bc4c24980.bin` (6752 B) |
| View uniform bound at pass | 842 |
| ViewRect | [0, 0, 1280, 720] |
| Allocation | [1280, 888] |
| Origin (cm) | [-850, 0, 90] |
| Forward | [0.974370062, 0, 0.224951059] |
| HFOV / VFOV (deg) | 70.0000080649 / 42.9956664562 |
| Pitch / yaw (deg) | 13.0000002518 / 0 |
| Near plane | 10 |
| Reversed-Z | True |
| PreExposure | 0.00048828125 |
| OneOverPreExposure | 2048 |
| PreViewTranslation (cm) | [850, 0, -90] |
| TemporalAAJitter | [0, 0, 0, 0] |
| InvDeviceZToWorldZTransform | [0, 0, 0.10000000149, -9.99999982452e-14] |
| FPackedView offsets | SVPositionToTranslatedWorld 0, ViewToTranslatedWorld 64, TranslatedWorldToView 128, TranslatedWorldToClip 192, ViewToClip 256, ViewRect 640, ViewSizeAndInvSize 656 |
| Offset 192 note | `TranslatedWorldToClip`, not `TranslatedWorldToView`; the uniform `ViewToClip` at 448 and the packed `ViewToClip` at 256 agree exactly |

Nominal approved identity is origin `(-850,0,90)` cm, HFOV `70` deg, pitch `13` deg, yaw `0` deg; the decoded float values above agree within `1e-5` deg.  The `1280x720` ViewRect is the presented sub-rect of the `1280x888` allocation; only rows `0..719` are presented and decoded.

## Receiver values

| ID | Pixel | World (cm) | Depth W (cm) | Shading normal (world) | Linear BaseColor | Rough | Metal | Spec | Shading | Direct RGB (pre-exposure) | VSM R |
| --- | --- | --- | ---: | --- | --- | ---: | ---: | ---: | --- | --- | ---: |
| A | (500,680) | [-19.7180953, -120.313805, -2.00517701] | 788.305175297 | [0.01466291, -0.08699994, 0.9961004] | [0.11443537, 0.095307467, 0.06847817] | 0.94117647 | 0 | 1 | DefaultLit (1) | [1.9306640625, 1.3037109375, 0.7900390625] | 1 |
| B | (720,680) | [-19.7198631, 69.4282488, -2.00498113] | 788.303496934 | [0.004887413, 0.006842378, 0.9999646] | [0.24228112, 0.20155625, 0.14412847] | 0.89411765 | 0 | 1 | DefaultLit (1) | [0, 0, 0] | 0 |
| C | (626,587) | [828.425518, -24.0598761, 61.3765201] | 1628.96870486 | [-0.9896113, 0.1299292, 0.06154542] | [1, 0.76815115, 0.33716362] | 0.078431373 | 1 | 0.50196078 | DefaultLit (1) | [0, 0, 0] | 0 |

Exact raw material bytes and codes:

| ID | GBufferB BGRA | Rough code | Metal code | Spec code | Packed id | GBufferC BGRA | BaseColor sRGB codes (R,G,B) | GBufferA word | GBufferD | GBufferF |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- | --- |
| A | [240, 255, 0, 177] | 240 | 0 | 255 | 177 | [74, 87, 95, 255] | [95, 87, 74] | 0x7fd74e07 | [0, 0, 0, 0] | [129, 127, 0, 127] |
| B | [228, 255, 0, 177] | 228 | 0 | 255 | 177 | [106, 124, 135, 255] | [135, 124, 106] | 0x7ff80e02 | [0, 0, 0, 0] | [128, 127, 0, 127] |
| C | [20, 128, 255, 161] | 20 | 255 | 128 | 161 | [157, 227, 255, 255] | [255, 227, 157] | 0x61f90805 | [0, 0, 0, 0] | [127, 127, 127, 127] |

C authored roughness .08 decodes to code **20/255 = 0.0784313725**; the table reports the actual decoded value.

GBuffer semantics: legacy path (`r.Substrate=0`, `r.GBufferFormat=1`); GBufferC alpha is AO (`r.AllowStaticLighting=0`, `r.GBufferDiffuseSampleOcclusion=0`); GBufferC target is sRGB; TAA upsampling `r.TemporalAA.Upsampling=1`; VSM `r.Shadow.Virtual.Enable=1`.

## Provenance

| Key | Resource | Producer pass (version) | Consumer pass (version) | Mip | RowPitch | Stored format | Identical |
| --- | --- | --- | --- | ---: | ---: | --- | --- |
| gbuffer_a | GBufferA `00040000020b9d701250` | 697 `00040000020bef06b8f0` | 842 `00040000020c16c4eef0` | 0 | 5120 | PF_A2B10G10R10 | True |
| gbuffer_b | GBufferB `00040000020b9d701710` | 697 `00040000020bef06b8f0` | 842 `00040000020c16c4eef0` | 0 | 5120 | PF_B8G8R8A8 | True |
| gbuffer_c | GBufferC `00040000020b9d7019d0` | 697 `00040000020bef06b8f0` | 842 `00040000020c16c4eef0` | 0 | 5120 | PF_B8G8R8A8 | True |
| gbuffer_d | GBufferD `00040000020b9d701c90` | 697 `00040000020bef06b8f0` | 842 `00040000020c16c4eef0` | 0 | 5120 | PF_B8G8R8A8 | True |
| gbuffer_f | GBufferF `00040000020b9d701f50` | 700 `0004000002097acf7700` | 842 `00040000020c16c4eef0` | 0 | 5120 | PF_B8G8R8A8 | True |
| scene_depth | SceneDepthZ `00040000020b9d700010` | 620 `00040000020ba06cda50` | 842 `00040000020c16c4eef0` | 0 | 5120 | PF_R32_FLOAT | True |
| scene_color_pre | SceneColor `00040000020b9d700490` | 840 `00040000020c16c43fb0` | 842 `00040000020c16c4eef0` | 0 | 10240 | PF_FloatRGBA | False |
| scene_color_post | SceneColor `00040000020b9d700490` | 842 `00040000020c16c4eef0` | 842 `00040000020c16c4eef0` | 0 | 10240 | PF_FloatRGBA | True |
| vsm_shadow_mask | ShadowMaskTexture `00040000020c948b7fa0` | 841 `00040000020c16c4de70` | 842 `00040000020c16c4eef0` | 0 | 5120 | PF_B8G8R8A8 | True |

Payload hashes are in `data/receivers.json`; every consumed consumer version was byte-identical to its producer version for the GBuffers, depth, and VSM mask.

## Direct light and VSM shadow

Sun pass **842** `RenderLight Light::StandardDeferred: Sun_Sponza`; pre SceneColor pass 840 `00040000020c16c43fb0`; post SceneColor pass 842 `00040000020c16c4eef0`.

| ID | Pre SceneColor RGB | Post SceneColor RGB | Direct RGB | Shadow BGRA | Shadow R linear | R==G | B/A clear |
| --- | --- | --- | --- | --- | ---: | --- | --- |
| A | [0, 0, 0] | [1.930664062, 1.303710938, 0.7900390625] | [1.930664062, 1.303710938, 0.7900390625] | [255, 255, 255, 255] | 1 | True | True |
| B | [0, 0, 0] | [0, 0, 0] | [0, 0, 0] | [255, 0, 0, 255] | 0 | True | True |
| C | [0, 0, 0] | [0, 0, 0] | [0, 0, 0] | [255, 0, 0, 255] | 0 | True | True |

Supported statements: the sun pass is the only light in the captured `SponzaTeaching.Sun_Sponza` scope; the direct RGB is the exact FP16 SceneColor delta across that pass; the VSM mask is the exact `ShadowMaskTexture` consumed as `PS.LightAttenuationTexture`.

## Figures

| Figure | Mapping | Source stats (min..max, mean) | Finite | Nonempty |
| --- | --- | --- | --- | --- |
| `figures/basecolor.png` | GBufferC linear BaseColor -> sRGB OETF (c<=0.0031308 ? 12.92c : 1.055*c^(1/2.4)-0.055) -> RGB8=1+round(254*x) (1..255); invalid/sky -> (0,0,0) | [0, 0, 0] .. [1, 0.79910274, 0.79910274], mean [0.13952701, 0.12312102, 0.092721612] | 913871/913871 | 913871 |
| `figures/worldnormal.png` | N_raw = 2*code/1023-1; N=N_raw/len(N_raw); RGB8=32+round(223*(0.5*N+0.5)) (32..255); no gamma; invalid/sky -> (0,0,0) | [-1, -1, -1, 0.99839436] .. [1, 1, 1, 1.0016851], mean [-0.41581728, -0.020678752, -0.093460234, 1.0000787] | 913871/913871 | 913871 |
| `figures/roughness.png` | Roughness=GBufferB.mem[0]/255; RGB8=32+round(223*roughness) (32..255); no gamma; invalid/sky -> (0,0,0) | [0] .. [1], mean [0.73899971] | 913871/913871 | 913871 |
| `figures/metallic.png` | Metallic=GBufferB.mem[2]/255; RGB8=32+round(223*metallic) (32..255); no gamma; invalid/sky -> (0,0,0) | [0] .. [1], mean [0.064133662] | 913871/913871 | 913871 |
| `figures/depth_log.png` | W = D*Inv[0]+Inv[1]+1/(D*Inv[2]-Inv[3]); t=log10(max(W,200)/200)/log10(2000/200) clamped; v=32+round(223*t) (32..255); black=invalid/sky; declared range 200..2000 cm (2..20 m) | [224.91975, 0.0050571845] .. [1977.3848, 0.0444603], mean [705.87778, 0.020503509] | 913871/913871 | 913871 |
| `figures/depth_validity.png` | white (255) iff finite deviceZ>0 and finite W>0; black (0) otherwise | [224.91975, 0.0050571845] .. [1977.3848, 0.0444603], mean [705.87778, 0.020503509] | 913871/913871 | 913871 |
| `figures/direct_rgb.png` | L = SceneColor(pass 842)-SceneColor(pass 840) per RGB half channel; tone=L/(1+L) for L>0; sRGB OETF; round(255*x); no exposure multiply; black=zero direct | [0, 0, 0] .. [55.90625, 43.59375, 32.375], mean [0.14759347, 0.10186634, 0.065730419] | 921600/921600 | 921600 |
| `figures/vsm_shadow.png` | R=ShadowMaskTexture.mem[2]; G=mem[1]; s=(R/255)^2; RGB8=round(255*s); no gamma; invalid/sky -> (128,128,128) | [0, 0, 0] .. [1, 1, 1], mean [0.54372372, 0.54717442, 0.54372372] | 913871/913871 | 913871 |

Every PNG is a 1280x720 RGB8 data raster with no raster text, arrows, legends, or invented background.  Invalid/sky masks are the declared colours in the manifest.

## Checks

- View: `view_identity` = **True**
- View: `views_joined_to_gbuffer_versions` = **True**
- View: `all_receivers_valid` = **True**
- View: `all_receivers_clean_3x3` = **True**
- View: `world_routes_agree` = **True**
- View: `direct_pre_zero_on_receivers` = **True**
- View: `shadow_r_equals_g_on_receivers` = **True**
- View: `shadow_ba_clear_on_receivers` = **True**
- View: `shading_models_default_lit` = **True**
- View: `figures_complete` = **True**
- Figures: `eight_full_frame_figures` = **True**
- Figures: `all_dimensions_1280x720` = **True**
- Figures: `all_source_finite` = **True**
- Figures: `all_images_nonempty` = **True**
- Figures: `no_raster_text_or_overlays` = **True**
- Figures: `primary_view_only_no_atlas` = **True**
- Figures: `valid_geometry_plus_sky_equals_total` = **True**
- Figures: `shadow_r_equals_g_all_valid` = **True**
- Figures: `shadow_ba_clear_all_valid` = **True**
- Figures: `scene_color_pre_zero_on_geometry` = **True**
- Figures: `basecolor_roundtrip_within_one_code` = **True**
- Figures: `normal_lengths_near_one` = **True**
- Figures: `depth_values_positive` = **True**
- Figures: `shadow_factors_unit_range` = **True**
- Figures: `basecolor_linear_unit_range` = **True**
- Figures: `roughness_metallic_unit_range` = **True**
- Verification: `ok` = **True** (27 checks, including PNG IHDR/IDAT/IEND chunk-only checks)

## Exact commands

```text
python sponza-receivers/decode_receivers.py --dump "D:\UnrealUnderstanding-Sponza\output\gpu-dump\raw\sponza-20261115-083654" --analysis "D:\UnrealUnderstanding-Sponza\output\gpu-dump\analysis-20261115-083654.json" --out "sponza-receivers" --stage receivers
python sponza-receivers/decode_receivers.py --dump "D:\UnrealUnderstanding-Sponza\output\gpu-dump\raw\sponza-20261115-083654" --analysis "D:\UnrealUnderstanding-Sponza\output\gpu-dump\analysis-20261115-083654.json" --out "sponza-receivers" --stage all
python sponza-receivers/decode_receivers.py --dump "D:\UnrealUnderstanding-Sponza\output\gpu-dump\raw\sponza-20261115-083654" --analysis "D:\UnrealUnderstanding-Sponza\output\gpu-dump\analysis-20261115-083654.json" --out "sponza-receivers" --stage verify
```

## Limitations

- The direct term is the captured SceneColor delta across the StandardDeferred sun pass, not an analytic BRDF and not a native ray-tracing/SMRT/AS intermediate.
- The VSM value is the 8-bit sqrt-encoded `ShadowMaskTexture` consumed by the light pass; the unquantized VSM receiver factor is not recovered.  The observed R codes are [0, 90, 127, 156, 180, 202, 221, 239, 255], i.e. the consumed attenuation is quantized to eighth steps after squaring.
- GBufferA is the world-space shading normal.  The depth-derived geometric normal is a check only; no geometric normal is used for shading.
- BaseColor is the 8-bit sRGB-decoded GBufferC albedo; it is not a material parameter or texture source.
- SceneColor is pre-exposure linear radiance.  No exposure multiply is applied to the reported direct values.
- The ray-tracing SceneBuffer staging warning means nonzero SceneBuffer bytes do not prove readable acceleration structures.  This decode does not read or claim acceleration structures.
- Shader-local values and per-thread choices are not dump resources and are not fabricated.
- The C sample is a valid polished-gold material point but is occluded from the sun at this view (mask R=0, direct RGB=0).  No nearby lit gold pixel was found in the sampled sphere region, so no receiver was substituted.

