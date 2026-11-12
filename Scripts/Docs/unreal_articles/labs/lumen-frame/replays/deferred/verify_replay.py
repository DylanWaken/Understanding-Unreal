#!/usr/bin/env python3
"""
Independent verifier for the final deferred-rect-light replay.

It does not trust report.json: it re-runs replay_cpu.replay() against the same
capture and compares the frozen report, re-hashes every evidence file, checks
the captured View/DeferredLight/LTC/shadow operands, and (when --ltc-header is
available) proves the actual native LTC exports are bit-identical to the
quantized LTC.h tables.

Usage:
  python verify_replay.py --capture <capture-root> --report report.json \
      [--output verification.json] [--ltc-header <LTC.h>] [--final-dir <final>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np

import replay_cpu as R


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def close(a, b, tol=1e-12):
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(close(x, y, tol) for x, y in zip(a, b))
    return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(a)), abs(float(b)))


def parse_ltc_header(path: Path):
    hdr = path.read_text(encoding="utf-8", errors="replace")

    def arr(name):
        i = hdr.index(name)
        j = hdr.index("{", i)
        k = hdr.index("}", j)
        vals = []
        for t in hdr[j + 1:k].split(","):
            t = t.strip().rstrip("f").rstrip("F")
            if t:
                vals.append(float(t))
        if len(vals) != 64 * 64 * 4:
            raise ValueError(f"{name}: expected 16384 values, got {len(vals)}")
        return np.array(vals, dtype=np.float64).reshape(64, 64, 4)

    return (
        arr("GGX_LTC_Mat").astype(np.float16),
        arr("GGX_LTC_Amp").astype(np.float16),
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--capture", required=True)
    ap.add_argument("--report", default="report.json")
    ap.add_argument("--output", default="verification.json")
    ap.add_argument("--ltc-header", default=None)
    ap.add_argument("--final-dir", default=None)
    args = ap.parse_args(argv)

    root = Path(args.capture)
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    checks = []
    failures = []

    def check(name, ok, evidence):
        checks.append({"name": name, "passed": bool(ok), "evidence": evidence})
        if not ok:
            failures.append(name)
        print(("PASS " if ok else "FAIL ") + name + " :: " + str(evidence))

    # 1. Identity and graph.
    check(
        "pass_identity",
        report.get("schema") == R.SCHEMA
        and report["pass"]["id"] == 249
        and report["pass"]["pointer"] == "000100000228e3a88880"
        and report["capture"]["graph_prefix"] == "0001"
        and report["pass"]["event"] == R.EVENT,
        {
            "schema": report.get("schema"),
            "pass": report["pass"]["id"],
            "pointer": report["pass"]["pointer"],
            "graph": report["capture"]["graph_prefix"],
        },
    )
    check(
        "graph_disambiguation",
        len(report["pass"]["graph_disambiguation"]) == 3
        and sum(1 for x in report["pass"]["graph_disambiguation"] if x["selected"]) == 1
        and report["pass"]["graph_disambiguation"][0]["id"] == 249,
        report["pass"]["graph_disambiguation"],
    )

    # 2. Re-run and compare key numerical fields.
    rerun = R.replay(root, report["pixel"]["x"], report["pixel"]["y"],
                     report["math"]["tolerance_ulps"])
    numeric = [
        ("math.cpu_contribution", report["math"]["cpu_contribution"],
         rerun["math"]["cpu_contribution"]),
        ("math.observed_delta", report["math"]["observed_delta"],
         rerun["math"]["observed_delta"]),
        ("math.residual_ulps", report["math"]["residual_ulps"],
         rerun["math"]["residual_ulps"]),
        ("math.max_abs_residual_ulps", report["math"]["max_abs_residual_ulps"],
         rerun["math"]["max_abs_residual_ulps"]),
        ("ltc.sampled.M", report["ltc"]["sampled"]["M"],
         rerun["ltc"]["sampled"]["M"]),
        ("ltc.sampled.A", report["ltc"]["sampled"]["A"],
         rerun["ltc"]["sampled"]["A"]),
        ("math.codeword_delta", report["math"]["codeword_delta"],
         rerun["math"]["codeword_delta"]),
        ("visibility.visibility", report["visibility"]["visibility"],
         rerun["visibility"]["visibility"]),
    ]
    num_ok = all(close(a, b, 1e-12) for _, a, b in numeric)
    check("independent_rerun", num_ok, [{"field": n, "report": a, "rerun": b}
                                        for n, a, b in numeric])

    # 3. Producer versions and resource evidence.
    pv = report["producer_versions"]
    expected_versions = {
        "SceneColor.before": ("000100000228dcc2d760", 126),
        "SceneColor.after": ("000100000228e3a88880", 249),
        "SceneDepthZ": ("000100000228d23fc770", 29),
        "GBufferA": ("000100000228dcc2d760", 126),
        "GBufferB": ("000100000228dcc2d760", 126),
        "GBufferC": ("000100000228dcc2d760", 126),
        "GBufferD": ("000100000228dcc2d760", 126),
        "Shadow.Virtual.MaskBits": ("000100000228e3a85210", 248),
    }
    ver_ok = True
    for k, (ptr, pid) in expected_versions.items():
        got = pv.get(k) or {}
        ver_ok = ver_ok and got.get("pointer") == ptr and got.get("id") == pid
    check("producer_versions", ver_ok,
          {k: pv.get(k) for k in expected_versions})

    # 4. File hashes.
    hash_fail = []
    for f in report.get("files", []):
        p = root / f["path"]
        if not p.is_file():
            hash_fail.append({"path": f["path"], "error": "missing"})
            continue
        got = sha256_file(p)
        if got != f["sha256"] or p.stat().st_size != f["bytes"]:
            hash_fail.append({"path": f["path"], "expected": f["sha256"],
                              "actual": got})
    check("file_hashes", not hash_fail, {"files": len(report.get("files", [])),
                                         "failures": hash_fail})

    # 5. Actual LTC binding, format, channel order, sampler.
    ltc = report["ltc"]
    ltc_ok = (
        ltc["source"] == "actual-native-binding"
        and ltc["fallback_used"] is False
        and ltc["mat"]["decoded"]["storedFormat"] == "PF_FloatRGBA"
        and ltc["amp"]["decoded"]["storedFormat"] == "PF_G16R16F"
        and ltc["mat"]["decoded"]["channels"] == 4
        and ltc["amp"]["decoded"]["channels"] == 2
        and ltc["mat"]["decoded"]["nativeValueInterpretation"] == "DecodedFloat"
        and ltc["amp"]["decoded"]["nativeValueInterpretation"] == "DecodedFloat"
    )
    check("actual_ltc_formats", ltc_ok, {
        "source": ltc["source"],
        "mat": ltc["mat"]["decoded"],
        "amp": ltc["amp"]["decoded"],
    })
    check("ltc_sampler_interpretation",
          ltc["sampler"]["sameObjectAsSharedBilinearClamped"] is True
          and "bilinear clamp" in ltc["sampler"]["interpretation"],
          ltc["sampler"])

    # 6. LTC actual vs quantized LTC.h (optional but strong).
    ltc_header = args.ltc_header
    if ltc_header and Path(ltc_header).is_file():
        mh, ah = parse_ltc_header(Path(ltc_header))
        mat = np.fromfile(root / ltc["mat"]["path"], dtype="<f2").reshape(64, 64, 4)
        amp = np.fromfile(root / ltc["amp"]["path"], dtype="<f2").reshape(64, 64, 2)
        mat_bits = mat.view(np.uint16)
        amp_bits = amp.view(np.uint16)
        mh_bits = mh.view(np.uint16)
        ah_bits = ah[:, :, :2].view(np.uint16)
        check("ltc_matches_ltc_h",
              np.array_equal(mat_bits, mh_bits) and np.array_equal(amp_bits, ah_bits),
              {
                  "mat_mismatches": int(np.count_nonzero(mat_bits != mh_bits)),
                  "amp_mismatches": int(np.count_nonzero(amp_bits != ah_bits)),
              })
    else:
        check("ltc_matches_ltc_h", True, "skipped: --ltc-header not supplied")

    # 7. View / DeferredLight actual operands.
    sp = report["shader_parameters"]
    check("actual_view_ub",
          sp["PS.View"]["structTypeName"] == "FViewUniformShaderParameters"
          and sp["PS.View"]["size"] == 6752
          and sp["PS.View"]["content"] == "00010000022880566180"
          and sp["PS.View"]["metadata"] == "000100007ffb9ac979f0",
          sp["PS.View"])
    check("actual_deferred_light_ub",
          sp["PS.DeferredLight"]["structTypeName"] == "FDeferredLightUniformStruct"
          and sp["PS.DeferredLight"]["size"] == 192
          and sp["PS.DeferredLight"]["content"] == "000100000228e3a88830",
          sp["PS.DeferredLight"])
    light = report["light"]
    light_ok = (
        close(light["translated_world_position"], [0.0, 670.0, 133.0])
        and close(light["color"], [979.4150390625] * 3)
        and close(light["inv_radius"], 0.0010000000474974513)
        and light["shadowed_bits"] == 3
        and close(light["shadow_map_channel_mask"], [0.0, 0.0, 0.0, 1.0])
        and light["b_affects_translucent_lighting"] == 1
    )
    check("actual_deferred_light_values", light_ok, {
        "translated_world_position": light["translated_world_position"],
        "color": light["color"],
        "inv_radius": light["inv_radius"],
        "shadowed_bits": light["shadowed_bits"],
        "shadow_map_channel_mask": light["shadow_map_channel_mask"],
        "b_affects_translucent_lighting": light["b_affects_translucent_lighting"],
    })
    check("exact_pre_exposure",
          report["math"]["pre_exposure"] == 0.0625
          and report["math"]["one_over_pre_exposure"] == 16.0,
          {"pre_exposure": report["math"]["pre_exposure"],
           "one_over": report["math"]["one_over_pre_exposure"]})

    # 8. VSM boundary.
    vis = report["visibility"]
    vsm_ok = (
        vis["producer"]["id"] == 248
        and vis["producer"]["pointer"] == "000100000228e3a85210"
        and vis["mask_bits_dwords"] == ["0x0000000F", "0x00000000"]
        and vis["unpacked_4bit"] == 15
        and vis["visibility"] == 1.0
        and vis["virtual_shadow_map_id"] == 8192
        and vis["fallback_taken"] is False
        and "NOT replayed" in vis["boundary"]
        and "quantized" in vis["boundary"]
    )
    operands = vis["producer_operands"]
    vsm_ok = vsm_ok and all(k in operands for k in (
        "ProjectionData", "PageTable", "PageReceiverMasks",
        "PhysicalPagePool", "PerViewData.LightGridData",
        "PerViewData.NumCulledLightsGrid",
    ))
    check("vsm_boundary", vsm_ok, {
        "producer": vis["producer"],
        "mask": vis["mask_bits_dwords"],
        "visibility": vis["visibility"],
        "operand_keys": sorted(operands),
        "boundary": vis["boundary"],
    })

    # 9. Observed FP16 comparison.
    m = report["math"]
    obs_ok = (
        close(report["pixel"]["scene_color_before"], [0.0, 0.0, 0.0, 0.0])
        and close(report["pixel"]["scene_color_after"],
                  [1.2392578125, 1.2392578125, 1.1767578125, 0.0])
        and close(m["observed_delta"], [1.2392578125, 1.2392578125, 1.1767578125])
        and m["observed_after_fp16_bits"] == ["0x3CF5", "0x3CF5", "0x3CB5"]
        and m["predicted_after_fp16_bits"] == ["0x3CF6", "0x3CF6", "0x3CB5"]
        and m["codeword_delta"] == [1, 1, 0]
    )
    check("observed_fp16_comparison", obs_ok, {
        "before": report["pixel"]["scene_color_before"],
        "after": report["pixel"]["scene_color_after"],
        "observed_bits": m["observed_after_fp16_bits"],
        "predicted_bits": m["predicted_after_fp16_bits"],
        "codeword_delta": m["codeword_delta"],
    })
    check("tolerance_pass",
          m["passed"] is True
          and m["max_abs_residual_ulps"] <= m["tolerance_ulps"] <= 1.0,
          {"max_abs_residual_ulps": m["max_abs_residual_ulps"],
           "tolerance_ulps": m["tolerance_ulps"],
           "passed": m["passed"]})

    # 10. Every report self-check.
    self_ok = all(bool(v) for v in report["checks"].values())
    check("report_self_checks", self_ok, report["checks"])

    # 11. Final scene-graphs.json cross-check (optional).
    if args.final_dir:
        sgp = Path(args.final_dir) / "scene-graphs.json"
        if sgp.is_file():
            sg = json.loads(sgp.read_text(encoding="utf-8"))
            g = next(x for x in sg["graphs"] if x["graph_prefix"] == "0001")
            pv0 = g["primary_view"]
            check("scene_graph_primary_view",
                  pv0["content"] == "00010000022880566180"
                  and pv0["metadata"] == "000100007ffb9ac979f0"
                  and pv0["bin_sha256"] == sp["PS.View"]["contentFile"]["sha256"],
                  {"graph": g["graph_prefix"], "view_content": pv0["content"],
                   "view_metadata": pv0["metadata"],
                   "bin_sha256": pv0["bin_sha256"]})
        else:
            check("scene_graph_primary_view", True, "skipped: scene-graphs.json missing")

    result = {
        "schema": "deferred-replay-verification-v1",
        "capture_id": report["capture"]["id"],
        "report": str(Path(args.report)),
        "checks": checks,
        "passed": not failures,
        "failures": failures,
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print()
    print("VERIFICATION:", "PASS" if not failures else "FAIL",
          f"({len(checks) - len(failures)}/{len(checks)} checks)")
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
