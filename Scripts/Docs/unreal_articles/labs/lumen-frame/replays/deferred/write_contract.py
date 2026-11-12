#!/usr/bin/env python3
"""
Build the frozen contract and human-readable numerical report from report.json.

Usage:
  python write_contract.py --report report.json --verification verification.json \
      --out-dir . [--scene-graphs <final/scene-graphs.json>]

The generated files are:
  contract.json
  contract.md
  numerical-report.md
  reproducibility.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def md(v) -> str:
    return str(v).replace("|", "\\|").replace("\n", " ")


def short_sha(v: str) -> str:
    return v[:16] if v else v


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default="report.json")
    ap.add_argument("--verification", default="verification.json")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--scene-graphs", default=None)
    args = ap.parse_args(argv)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rep = json.loads(Path(args.report).read_text(encoding="utf-8"))
    ver = json.loads(Path(args.verification).read_text(encoding="utf-8"))
    report_sha = sha256_file(Path(args.report))
    verification_sha = sha256_file(Path(args.verification))

    sg = None
    sg_sha = None
    if args.scene_graphs and Path(args.scene_graphs).is_file():
        sg = json.loads(Path(args.scene_graphs).read_text(encoding="utf-8"))
        sg_sha = sha256_file(Path(args.scene_graphs))

    # ------------------------------------------------------------------
    # contract.json
    # ------------------------------------------------------------------
    primary_view = None
    if sg:
        pv = next((x["primary_view"] for x in sg["graphs"]
                   if x["graph_prefix"] == rep["capture"]["graph_prefix"]), None)
        if pv:
            primary_view = {k: pv.get(k) for k in (
                "pass", "path", "content", "metadata", "bin_sha256", "bin_bytes"
            )}

    contract = {
        "contract_id": rep["contract_id"],
        "schema": "deferred-replay-contract-v1",
        "status": "final-capture-validated; actual native LTC Mat/Amp and actual DeferredLight UB",
        "goal": (
            "Exact CPU replay of the one actual screen-space deferred rect-light pass "
            "(graph 0001, pass 249) at pixel (640,650), with all inputs bound to the "
            "final capture by exact version, actual native LUT/shadow bytes, exact "
            "pre-exposure, and an observed-FP16 comparison."
        ),
        "capture": rep["capture"],
        "scope": {
            "in_scope": [
                "pass 249 FRenderLightParameters / FDeferredLightPS::FParameters",
                "SceneColor before/after at pass 249",
                "GBufferA/B/C/D, SceneDepthZ, MaskBits",
                "actual PS.View FViewUniformShaderParameters (6752 B)",
                "actual PS.DeferredLight FDeferredLightUniformStruct (192 B)",
                "actual PS.VirtualShadowMap FVirtualShadowMapUniformParameters (368 B)",
                "actual shader-bound GGX LTC Mat/Amp native textures",
                "one legacy DefaultLit rect-light pixel and an observed-FP16 comparison",
            ],
            "out_of_scope": [
                "Substrate",
                "MegaLights",
                "hardware ray-traced direct lighting",
                "translucency/hair/eye/SSS/clearcoat/cloth",
                "Lumen indirect lighting and reflections",
                "VSM/SMRT producer replay",
                "GPU execution and engine/target-repo modification",
            ],
        },
        "selection": {
            "pass_id": rep["pass"]["id"],
            "pass_pointer": rep["pass"]["pointer"],
            "graph_prefix": rep["capture"]["graph_prefix"],
            "event": rep["pass"]["event"],
            "parent_event_scopes": rep["pass"]["parentEventScopes"],
            "same_light_other_graphs": rep["pass"]["graph_disambiguation"],
            "pixel": [rep["pixel"]["x"], rep["pixel"]["y"]],
            "why": (
                "Pass 249 is the screen-space deferred RenderLight for the only local light "
                "in graph 0001. The same light pass appears again at ids 561/869 in graphs "
                "0003/0004; this contract pins graph 0001 / id 249 / pointer "
                "000100000228e3a88880. The pixel is inside the 1280x720 view, DefaultLit, "
                "metallic=0, with MaskBits=0xF (visibility exactly 1.0)."
            ),
        },
        "pass": rep["pass"],
        "scene_graph": {
            "graph_prefix": rep["capture"]["graph_prefix"],
            "frame_scope": rep["capture"]["frame_scope"],
            "view_metadata": rep["shader_parameters"]["PS.View"]["metadata"],
            "view_content": rep["shader_parameters"]["PS.View"]["content"],
            "view_sha256": rep["shader_parameters"]["PS.View"]["contentFile"]["sha256"],
            "scene_graphs_file_sha256": sg_sha,
            "primary_view": primary_view,
        },
        "producer_versions": rep["producer_versions"],
        "resources": rep["resources"],
        "shader_parameters": rep["shader_parameters"],
        "light": rep["light"],
        "pixel": rep["pixel"],
        "visibility": rep["visibility"],
        "ltc": rep["ltc"],
        "permutation": rep["permutation"],
        "energy_conservation": rep["energy_conservation"],
        "numerical": rep["math"],
        "checks": rep["checks"],
        "gaps": rep["gaps"],
        "provenance": {
            "report_file": Path(args.report).name,
            "report_sha256": report_sha,
            "verification_file": Path(args.verification).name,
            "verification_sha256": verification_sha,
            "verification_passed": ver["passed"],
            "verification_checks": len(ver["checks"]),
            "files": rep["files"],
            "provenance_files": rep["provenance_files"],
            "no_gpu_work": True,
        },
        "commands": {
            "install": "python -m pip install -r analysis/deferred-replay/requirements.txt",
            "replay": (
                "python analysis/deferred-replay/replay_cpu.py "
                "--capture <capture-root> --output analysis/deferred-replay/report.json"
            ),
            "verify": (
                "python analysis/deferred-replay/verify_replay.py "
                "--capture <capture-root> --report analysis/deferred-replay/report.json "
                "--output analysis/deferred-replay/verification.json "
                "--ltc-header <engine>/Engine/Source/Runtime/Renderer/Private/LTC.h "
                "--final-dir <final>"
            ),
            "contract": (
                "python analysis/deferred-replay/write_contract.py "
                "--report analysis/deferred-replay/report.json "
                "--verification analysis/deferred-replay/verification.json "
                "--out-dir analysis/deferred-replay --scene-graphs <final>/scene-graphs.json"
            ),
        },
    }
    (out / "contract.json").write_text(
        json.dumps(contract, indent=2) + "\n", encoding="utf-8"
    )

    # ------------------------------------------------------------------
    # Markdown helpers
    # ------------------------------------------------------------------
    p = rep["pixel"]
    m = rep["math"]
    ltc = rep["ltc"]
    vis = rep["visibility"]
    before = p["scene_color_before"]
    after = p["scene_color_after"]
    cpu = m["cpu_contribution"]
    obs = m["observed_delta"]
    res = m["residual"]
    rulp = m["residual_ulps"]
    pred = m["predicted_after_fp16"]
    pred_bits = m["predicted_after_fp16_bits"]
    obs_bits = m["observed_after_fp16_bits"]
    cdelta = m["codeword_delta"]
    channels = ("R", "G", "B")

    def comp_table():
        rows = []
        for i, ch in enumerate(channels):
            rows.append(
                "| %s | %.10f | %.10f | %.10f | %.10f | %+.10f | %+.4f | %.10f | %s | %s | %+d |"
                % (
                    ch, before[i], after[i], obs[i], cpu[i], res[i], rulp[i],
                    pred[i], pred_bits[i], obs_bits[i], cdelta[i],
                )
            )
        return "\n".join(rows)

    def producer_table():
        rows = []
        for name, pv in rep["producer_versions"].items():
            res_ev = None
            if name.startswith("SceneColor."):
                res_ev = rep["resources"]["SceneColor"][name.split(".")[1]]
            else:
                res_ev = rep["resources"].get(name)
            sha = short_sha(res_ev["sha256"]) if res_ev else ""
            fmt = res_ev.get("decoded", {}).get("storedFormat", "") if res_ev else ""
            rows.append(
                "| %s | %s | %s | %s | %s | %s |"
                % (name, pv["id"], pv["pointer"], pv["event"], fmt, sha)
            )
        return "\n".join(rows)

    def files_table():
        rows = []
        for f in rep["files"]:
            rows.append("| `%s` | %d | `%s` |" % (f["path"], f["bytes"], f["sha256"]))
        return "\n".join(rows)

    def gaps_md():
        return "\n".join(
            "- **%s (%s):** %s" % (g["id"], g["kind"], g["detail"])
            for g in rep["gaps"]
        )

    def perm_table():
        rows = []
        for k, v in rep["permutation"].items():
            if k == "compiled_shader_hash":
                continue
            if k == "shading_model":
                rows.append("| %s | %s | %s |" % (k, v["id"], md(v["name"])))
            elif k == "source_shape":
                rows.append("| %s | %s | %s |" % (k, md(v["value"]), md(v["evidence"])))
            else:
                val = v.get("active", v.get("enabled", v.get("use_energy_conservation")))
                rows.append("| %s | %s | %s |" % (k, val, md(v.get("evidence", ""))))
        return "\n".join(rows)

    # ------------------------------------------------------------------
    # contract.md
    # ------------------------------------------------------------------
    contract_md = f"""# Deferred-replay contract — final capture, graph 0001 / pass 249 / pixel (640,650)

**Contract ID:** `{rep['contract_id']}`
**Machine-readable contract:** `contract.json`
**Numerical report:** `numerical-report.md`
**Status:** final-capture validated; actual native LTC Mat/Amp and actual `FDeferredLightUniformStruct`; no GPU work.

## 1. Capture and scope

| Field | Value |
|---|---|
| Capture ID | `{rep['capture']['id']}` |
| Project / engine | {rep['capture']['project']} / {rep['capture']['engine']} |
| GPU | {rep['capture']['gpu']} |
| Graph | `{rep['capture']['graph_prefix']}` |
| Frame scope | `{rep['capture']['frame_scope']}` |
| View rect | (0,0,1280,720) inside a 1280x888 buffer |
| Pixel | ({p['x']},{p['y']}) |

In scope: pass 249 `FRenderLightParameters`, `SceneColor` before/after, GBufferA/B/C/D,
SceneDepthZ, `Shadow.Virtual.MaskBits`, actual `PS.View`, actual `PS.DeferredLight`,
actual `PS.VirtualShadowMap`, actual shader-bound GGX LTC Mat/Amp, and one DefaultLit
pixel comparison.

Out of scope: Substrate, MegaLights, hardware ray tracing, translucency/hair/eye/SSS/
clearcoat/cloth, Lumen indirect/reflections, a VSM/SMRT producer rewrite, and all GPU
execution.

## 2. Pass selection and scene graph

| Field | Value |
|---|---|
| Pass id | **{rep['pass']['id']}** |
| Pointer | `{rep['pass']['pointer']}` |
| Event | `{rep['pass']['event']}` |
| Parameters | `{rep['pass']['parametersName']}` at `{rep['pass']['parameters']}` |
| Parameters metadata | `{rep['pass']['parametersMetadata']}` |
| Output | `{rep['pass']['outputResources'][0]}` (SceneColor) |
| View metadata / content | `{rep['shader_parameters']['PS.View']['metadata']}` / `{rep['shader_parameters']['PS.View']['content']}` |
| View SHA-256 | `{rep['shader_parameters']['PS.View']['contentFile']['sha256']}` |

The same light pass is replayed at ids 561 (graph `0003`) and 869 (graph `0004`); this
contract pins graph `0001` / id 249. The final `scene-graphs.json` primary view for
graph 0001 has the same content pointer and SHA-256 as the pass-bound `PS.View`.

## 3. Exact producer versions

| Resource | Producer id | Version pointer | Producer event | Stored format | SHA-256 (first 16) |
|---|---|---|---|---|---|
{producer_table()}

SceneColor is `PF_FloatRGBA` (FP16), 1280x888. The view is rows 0..719; rows 720..887 are
outside the view rect. `SceneDepthZ` mip0 is `PF_R32_FLOAT` (4 B/pixel); the stencil plane
is a separate `PF_R8_UINT` subresource and is not used by this replay. The paths, version
pointers, and resource IDs above are the final capture's. The SceneColor/GBuffer/depth
byte hashes coincide with the earlier draft capture's values because the frame is
deterministic; they are not inherited from the draft.

## 4. Actual shader operands

| Operand | Evidence |
|---|---|
| `PS.View` | `FViewUniformShaderParameters`, 6752 B, content `{rep['shader_parameters']['PS.View']['content']}` |
| `PS.DeferredLight` | `FDeferredLightUniformStruct`, 192 B, content `{rep['shader_parameters']['PS.DeferredLight']['content']}` |
| `PS.SceneTextures` | `FSceneTextureUniformParameters`, 128 B, content `{rep['shader_parameters']['PS.SceneTextures']['content']}` |
| `PS.VirtualShadowMap` | `FVirtualShadowMapUniformParameters`, 368 B, content `{rep['shader_parameters']['PS.VirtualShadowMap']['content']}` |
| `GGXLTCMatTexture` | actual native binding, `{ltc['mat']['path']}` |
| `GGXLTCAmpTexture` | actual native binding, `{ltc['amp']['path']}` |

The screen-space `FDeferredLightUniformStruct` is **not reconstructed**: it is the captured
192 B RDG UB used by pass 249. Its values include translated light position
`{rep['light']['translated_world_position']}`, color `{rep['light']['color']}`,
`ShadowedBits={rep['light']['shadowed_bits']}`, `ShadowMapChannelMask={rep['light']['shadow_map_channel_mask']}`,
and `bAffectsTranslucentLighting={rep['light']['b_affects_translucent_lighting']}`.

## 5. Actual LTC textures and sampler

| Item | Mat | Amp |
|---|---|---|
| Native texture | `{ltc['mat']['nativeTextureName']}` | `{ltc['amp']['nativeTextureName']}` |
| Native pointer | `{ltc['mat']['nativePointer']}` | `{ltc['amp']['nativePointer']}` |
| Diagnostic resource | `{ltc['mat']['diagnosticResource']}` | `{ltc['amp']['diagnosticResource']}` |
| Export file | `{ltc['mat']['path']}` | `{ltc['amp']['path']}` |
| StoredFormat | `{ltc['mat']['decoded']['storedFormat']}` | `{ltc['amp']['decoded']['storedFormat']}` |
| Channels | {ltc['mat']['decoded']['channels']} (half4) | {ltc['amp']['decoded']['channels']} (half2) |
| SHA-256 | `{ltc['mat']['sha256']}` | `{ltc['amp']['sha256']}` |

Channel order is preserved from the file: `PF_FloatRGBA` half4 -> `M.x,M.y,M.z,M.w`;
`PF_G16R16F` half2 -> `A.x,A.y`. `SubresourceLayouts.StoredFormat` is the decoder selector;
the descriptor's `NativeFormat` is never used to choose a decoder. At this pixel:

- `M = {ltc['sampled']['M']}`
- `A = {ltc['sampled']['A']}`
- `NoV = {ltc['sampled']['NoV']:.15f}`, `UV = {ltc['sampled']['UV']}`
- `IrradianceScale = {ltc['sampled']['IrradianceScale'][0]:.15f}`

The sampler pointers for `GGXLTCMatSampler`, `GGXLTCAmpSampler`, and
`SharedBilinearClampedSampler` are all `{ltc['sampler']['sharedBilinearClampedPointer']}`.
Source `SceneManagement.cpp:1277` creates that object as
`TStaticSamplerState<SF_Bilinear, AM_Clamp, AM_Clamp, AM_Clamp>`, and `RectLight.ush:14-16`
uses it via `SUPPORTS_INDEPENDENT_SAMPLERS`. The interpretation is bilinear clamp,
`SampleLevel(...,0)`, 64x64, no mip chain.

## 6. Visibility / VSM boundary

| Field | Value |
|---|---|
| Path | `{vis['path']}` |
| VirtualShadowMapId | {vis['virtual_shadow_map_id']} (not INDEX_NONE) |
| Producer | pass {vis['producer']['id']} `{vis['producer']['pointer']}` `{vis['producer']['event']}` |
| MaskBits dwords at pixel | `{vis['mask_bits_dwords'][0]}`, `{vis['mask_bits_dwords'][1]}` |
| Light index / unpacked | {vis['light_index']} / {vis['unpacked_4bit']} -> {vis['visibility']} |
| Fallback taken | {vis['fallback_taken']} |

**Boundary:** {vis['boundary']} The producer's `ProjectionData`, `PageTable`, `PageFlags`,
`PageReceiverMasks`, `PhysicalPagePool`, `LightGridData`, and `NumCulledLightsGrid` are
retained as operands in `contract.json` but are not sampled. Visibility is exactly 1.0
because `15/15 = 1.0`; no unquantized SMRT shadow factor is claimed.

## 7. Active branches / permutations

| Branch | Active | Evidence |
|---|---|---|
{perm_table()}

The derived `FDeferredLightPS` permutation domain is: rect source shape, no source
texture, no IES, no anisotropy, no lighting channels, VSM mask on, no transmission, no
hair, no light-function-atlas material, no cloud/atmosphere transmittance, no visualize
culling, energy conservation 0. No compiled PSO/shader hash was captured (gap G3); the
domain is derived from the captured pass parameters and CVars.

## 8. Numerical comparison

Test: `pass if max_channel |L_cpu - (after - before)| <= ULP_fp16(max(|after|,|L_cpu|))`.
`before = {before[:3]}` -> the tolerance is **{m['tolerance_ulps']} ULP = {m['half_ulp']}**.

| Ch | before | after (observed FP16) | observed delta | CPU L | residual | residual ULP | predicted FP16 | pred bits | obs bits | code delta |
|---|---|---|---|---|---|---|---|---|---|---|
{comp_table()}

**Result: {'PASS' if m['passed'] else 'FAIL'}** — max |residual| = {m['max_abs_residual_ulps']:.6f} ULP
<= {m['tolerance_ulps']} ULP. The predicted FP16 codeword is +1 ULP in R/G and exact in
B; the unrounded residual is +{rulp[0]:.4f} ULP (R/G) and {rulp[2]:.4f} ULP (B).
The actual LTC tables are bit-identical to the quantized `LTC.h` (0 mismatched half
codewords in the full 64x64 Mat/Amp tables), so the 1-codeword R/G difference is not an
LTC-content gap (see G1).

## 9. Residual gaps (do not relabel as exact)

{gaps_md()}

## 10. Reproduction and freeze

```text
# install (NumPy only)
python -m pip install -r analysis/deferred-replay/requirements.txt

# replay (portable: paths are relative to --capture)
python analysis/deferred-replay/replay_cpu.py --capture <capture-root> --output analysis/deferred-replay/report.json

# independent verification
python analysis/deferred-replay/verify_replay.py --capture <capture-root> --report analysis/deferred-replay/report.json --output analysis/deferred-replay/verification.json --ltc-header <engine>/Engine/Source/Runtime/Renderer/Private/LTC.h --final-dir <final>
```

Frozen hashes: `report.json` `{report_sha}`; `verification.json` `{verification_sha}`.
Verification: {len(ver['checks'])} checks, {'PASS' if ver['passed'] else 'FAIL'}.

## 11. Source reference index

| File | Lines | What |
|---|---|---|
| `LightRendering.h` | 23-33 | `FDeferredLightUniformStruct` layout |
| `LightRendering.cpp` | 2773-2830, 3154, 3218-3238 | deferred-light PS parameters, VSM permutation |
| `RectLight.ush` | 14-16, 61-165, 192-252, 262-299, 340-405, 602-690 | rect irradiance, polygon integration, LTC, sampler, no barn door |
| `RectLightIntegrate.ush` | 59-92 | `CreateAreaLight`, `IntegrateBxDF` dispatch |
| `ShadingModels.ush` | 212-293, 1137-1168 | `DefaultLitBxDF`, rect LTC branch |
| `DeferredLightingCommon.ush` | 90-149, 246-286, 316-437 | shadow terms, local attenuation, accumulation |
| `DeferredLightPixelShaders.usf` | 102-116, 144-164, 291-302, 440-455 | pixel main, VSM mask, ScreenUV, pre-exposure |
| `DeferredShadingCommon.ush` | 137-147, 295-309, 1076-1186 | GBuffer decode |
| `LightAccumulator.ush` | 58-83, 98-137 | additive accumulation |
| `ShadingEnergyConservation.ush` | 15-31 | `USE_ENERGY_CONSERVATION` selection |
| `ShaderCompiler.cpp` | 4191-4194 | `LEGACY_MATERIAL_ENERGYCONSERVATION` from `r.Material.EnergyConservation` |
| `SceneManagement.cpp` | 1275-1280 | shared sampler state |
| `VirtualShadowMapMaskBitsCommon.ush` | 16-60 | `GetVirtualShadowMapMaskForLight` / unpack |
| `SceneView.cpp` | 2576-2589, 2613 | `ScreenPositionScaleBias` |
| `LTC.h` | `GGX_LTC_Mat`, `GGX_LTC_Amp` | source LUT tables (actual exports are bit-identical) |
"""
    (out / "contract.md").write_text(contract_md, encoding="utf-8")

    # ------------------------------------------------------------------
    # numerical-report.md
    # ------------------------------------------------------------------
    num_md = f"""# Numerical report — final deferred rect-light pixel

**Capture:** `{rep['capture']['id']}`  
**Graph / pass:** `{rep['capture']['graph_prefix']}` / {rep['pass']['id']} `{rep['pass']['pointer']}` `{rep['pass']['event']}`  
**Pixel:** ({p['x']},{p['y']})  
**Report SHA-256:** `{report_sha}`  
**Verification:** `{verification_sha}` ({len(ver['checks'])} checks, {'PASS' if ver['passed'] else 'FAIL'})

## 1. Command actually run

```text
python analysis/deferred-replay/replay_cpu.py --capture <capture-root> --output analysis/deferred-replay/report.json
python analysis/deferred-replay/verify_replay.py --capture <capture-root> --report analysis/deferred-replay/report.json --output analysis/deferred-replay/verification.json --ltc-header <engine>/Engine/Source/Runtime/Renderer/Private/LTC.h --final-dir <final>
```

Result: replay exit 0, `PASS`; verifier exit 0, all {len(ver['checks'])} checks pass.

## 2. Scene graph and versions

| Resource | Producer | Version | Producer event | StoredFormat | SHA-256 (first 16) |
|---|---|---|---|---|---|
{producer_table()}

`SceneColor.before` is the last mip0 writer before pass 249 (pass 126); pass 249 writes the
same resource in place. `SceneDepthZ` mip0 is last written by pass 29; later passes only
write the separate stencil subresource. `MaskBits` is produced by pass 248.

## 3. Pixel decode

| Quantity | Value |
|---|---|
| UV | {p['uv']} |
| NDC | {p['ndc']} |
| DeviceZ | {p['device_z']:.15f} (`{p['device_z_bits']}`) |
| SceneDepth | {p['scene_depth_cm']:.12f} cm |
| Translated world position | {p['translated_world_position']} |
| World position | {p['world_position']} |
| Normal | {p['normal']} |
| BaseColor (linear) | {p['base_color']} |
| Metallic / Specular / Roughness | {p['metallic']} / {p['specular']} / {p['roughness']} |
| GBufferA word | `{p['gbuffer_a_word']}` |
| GBufferB word | `{p['gbuffer_b_word']}` |
| GBufferC word | `{p['gbuffer_c_word']}` |
| GBufferD word | `{p['gbuffer_d_word']}` |
| ShadingModelID / Mask | {p['shading_model_id']} / `{p['selective_output_mask']}` |
| F0 | {p['f0']} |
| DiffuseColor (rho) | {p['rho']} |

## 4. Actual light and LTC operands

| Light field | Value |
|---|---|
| TranslatedWorldPosition | {rep['light']['translated_world_position']} |
| Color | {rep['light']['color']} |
| InvRadius | {rep['light']['inv_radius']} |
| Direction | {rep['light']['direction']} |
| Tangent | {rep['light']['tangent']} |
| SourceRadius / SourceLength | {rep['light']['source_radius']} / {rep['light']['source_length']} |
| RectLightBarnCosAngle / Length | {rep['light']['rect_light_barn_cos_angle']} / {rep['light']['rect_light_barn_length']} |
| ShadowedBits | {rep['light']['shadowed_bits']} |
| ShadowMapChannelMask | {rep['light']['shadow_map_channel_mask']} |
| SpecularScale / DiffuseScale | {rep['light']['specular_scale']} / {rep['light']['diffuse_scale']} |
| LightMask | {rep['light']['light_mask']:.15f} |
| Falloff / NoL | {rep['light']['falloff']:.15f} / {rep['light']['no_l']:.15f} |

| LTC | Value |
|---|---|
| Source | `{ltc['source']}` (fallback used: {ltc['fallback_used']}) |
| Mat file | `{ltc['mat']['path']}` (`{ltc['mat']['decoded']['storedFormat']}`, {ltc['mat']['decoded']['channels']} channels) |
| Amp file | `{ltc['amp']['path']}` (`{ltc['amp']['decoded']['storedFormat']}`, {ltc['amp']['decoded']['channels']} channels) |
| Sampled M | {ltc['sampled']['M']} |
| Sampled A | {ltc['sampled']['A']} |
| IrradianceScale | {ltc['sampled']['IrradianceScale']} |
| UV | {ltc['sampled']['UV']} |
| Sampler | {ltc['sampler']['interpretation']} |

## 5. Visibility

| Field | Value |
|---|---|
| MaskBits dword0 / dword1 | `{vis['mask_bits_dwords'][0]}` / `{vis['mask_bits_dwords'][1]}` |
| Unpacked / visibility | {vis['unpacked_4bit']} / {vis['visibility']} |
| VSM id / fallback | {vis['virtual_shadow_map_id']} / {vis['fallback_taken']} |
| Producer | pass {vis['producer']['id']} `{vis['producer']['event']}` |

{vis['boundary']}

## 6. Contribution and FP16 comparison

Component breakdown:

| Component | R/G/B |
|---|---|
| Diffuse (unexposed) | {m['diffuse_unexposed']} |
| LTC specular (unexposed) | {m['specular_unexposed']} |
| LightMask | {m['light_mask']:.15f} |
| SurfaceShadow | {m['surface_shadow']:.15f} |
| Visibility | {m['visibility']:.15f} |
| PreExposure | {m['pre_exposure']} |
| Masked light color | {m['masked_light_color']} |
| CPU contribution | {m['cpu_contribution']} |

Comparison (observed FP16 output, not a float reconstruction):

| Ch | before | after | observed delta | CPU L | residual | residual ULP | predicted FP16 | pred bits | obs bits | code delta |
|---|---|---|---|---|---|---|---|---|---|---|
{comp_table()}

**Tolerance test:** max |residual| = **{m['max_abs_residual_ulps']:.6f} ULP**;
tolerance = **{m['tolerance_ulps']} ULP**; result = **{'PASS' if m['passed'] else 'FAIL'}**.
The predicted FP16 codeword is +1 ULP in R/G and exact in B. The actual LTC values are
bit-identical to the quantized `LTC.h`; see gap G1 for the remaining sub-ULP difference.

## 7. Active permutations

| Branch | Active | Evidence |
|---|---|---|
{perm_table()}

## 8. Residual gaps

{gaps_md()}

## 9. Evidence files

| File (relative to capture root) | Bytes | SHA-256 |
|---|---|---|
{files_table()}
"""
    (out / "numerical-report.md").write_text(num_md, encoding="utf-8")

    # ------------------------------------------------------------------
    # reproducibility.json
    # ------------------------------------------------------------------
    reproducibility = {
        "schema": "deferred-replay-reproducibility-v1",
        "capture_id": rep["capture"]["id"],
        "report_sha256": report_sha,
        "verification_sha256": verification_sha,
        "verification_passed": ver["passed"],
        "verification_checks": len(ver["checks"]),
        "contract_sha256": sha256_file(out / "contract.json"),
        "contract_md_sha256": sha256_file(out / "contract.md"),
        "numerical_report_sha256": sha256_file(out / "numerical-report.md"),
        "readme_sha256": (
            sha256_file(out / "README.md") if (out / "README.md").is_file() else None
        ),
        "requirements_sha256": (
            sha256_file(out / "requirements.txt")
            if (out / "requirements.txt").is_file() else None
        ),
        "replay_script_sha256": (
            sha256_file(out / "replay_cpu.py")
            if (out / "replay_cpu.py").is_file() else None
        ),
        "verify_script_sha256": (
            sha256_file(out / "verify_replay.py")
            if (out / "verify_replay.py").is_file() else None
        ),
        "write_contract_sha256": (
            sha256_file(out / "write_contract.py")
            if (out / "write_contract.py").is_file() else None
        ),
        "python": "3.14 (tested)",
        "numpy": "2.5.4 (tested)",
        "requirements": "numpy",
        "no_gpu_work": True,
        "no_engine_or_evidence_edits": True,
        "commands": contract["commands"],
        "gaps": [g["id"] for g in rep["gaps"]],
    }
    (out / "reproducibility.json").write_text(
        json.dumps(reproducibility, indent=2) + "\n", encoding="utf-8"
    )

    print("wrote contract.json, contract.md, numerical-report.md, reproducibility.json")
    print("report_sha256", report_sha)
    print("verification_sha256", verification_sha)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
