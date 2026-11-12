"""Independent verification of the world-cache replay publication.

Re-runs the replay from the raw capture, compares the fresh report to the stored
JSON, re-hashes every named input, and recomputes the lookup's depth and tap
arithmetic directly from the stored arrays. Writes ``verification.json``.
"""
from pathlib import Path
import argparse
import hashlib
import json
import sys

import numpy as np

import world_cache_math as cache
import replay_world_cache as replay

HERE = Path(__file__).resolve().parent


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def nested_depth(values, weights):
    az, ay, ax = np.float32(weights[2]), np.float32(weights[1]), np.float32(weights[0])
    l = lambda a, b, t: np.float32(a) + np.float32(t) * (np.float32(b) - np.float32(a))
    z00 = l(values[0], values[1], az)
    z01 = l(values[2], values[3], az)
    z10 = l(values[4], values[5], az)
    z11 = l(values[6], values[7], az)
    return l(l(z00, z01, ay), l(z10, z11, ay), ax)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=HERE / "verification.json")
    args = parser.parse_args()
    checks = []

    def check(name, passed, evidence):
        checks.append({"name": name, "passed": bool(passed), "evidence": evidence})

    stored = json.loads((HERE / "world-cache-replay.json").read_text(encoding="utf-8"))
    selfchecks = json.loads((HERE / "selfchecks.json").read_text(encoding="utf-8"))
    blue_export = json.loads((HERE / "blue-noise-export.json").read_text(encoding="utf-8"))
    permutation = json.loads((HERE / "permutation-evidence.json").read_text(encoding="utf-8"))
    md = (HERE / "world-cache-replay.md").read_text(encoding="utf-8")

    check("stored_json_all_selfchecks", stored["allSelfchecksPassed"] and selfchecks["allPassed"],
          {"report": stored["allSelfchecksPassed"], "selfchecks": selfchecks["allPassed"],
           "count": len(selfchecks["checks"])})
    check("stored_selfcheck_count", len(selfchecks["checks"]) == 22, len(selfchecks["checks"]))

    fresh = replay.build_report(args.capture, 167)
    fresh["capture"]["dumpRoot"] = stored["capture"]["dumpRoot"]
    # Public metadata may redact the host device name. The raw shader inputs
    # and every pass, layout and payload identity remain in the comparison.
    fresh["capture"]["infos"]["DeviceName"] = stored["capture"]["infos"].get("DeviceName")
    check("fresh_replay_matches_stored_report", fresh == stored,
          {"freshSelfchecks": fresh["allSelfchecksPassed"],
           "storedSelfchecks": stored["allSelfchecksPassed"]})
    check("fresh_replay_all_selfchecks", fresh["allSelfchecksPassed"], True)

    q = stored["queryWitness"]
    c = stored["cacheLookup"]
    cmp = stored["comparison"]
    disp = stored["dispatch"]
    check("selected_lane_is_actual_compacted_lane",
          q["dispatchIndex"] == stored["selection"]["selectedLane"] == 190
          and q["screenProbeIndex"] == 676
          and q["traceTexelXY"] == [2, 2]
          and q["traceBufferXY"] == [290, 66],
          {"lane": q["dispatchIndex"], "probe": q["screenProbeIndex"],
           "traceTexel": q["traceTexelXY"], "traceBuffer": q["traceBufferXY"]})
    check("selected_lane_below_allocator", q["dispatchIndex"] < disp["allocator"], disp["allocator"])
    check("both_indirect_triples_are_ceil_of_allocator",
          disp["dispatchTripleDiv64"] == [(disp["allocator"] + 63) // 64, 1, 1]
          and disp["dispatchTripleDiv32"] == [(disp["allocator"] + 31) // 32, 1, 1],
          {"N": disp["allocator"], "div64": disp["dispatchTripleDiv64"],
           "div32": disp["dispatchTripleDiv32"]})
    check("eight_neighbors_thirty_two_taps",
          len(c["neighbors"]) == 8 and c["radianceTapCount"] == 32
          and len(c["radianceTaps"]) == 32,
          {"neighbors": len(c["neighbors"]), "taps": c["radianceTapCount"]})
    check("compacted_trace_texels_are_unique_and_selected_occurs_once",
          disp["compactedTraceCoordsUnique"] == disp["allocator"]
          and stored["selection"]["selectedTraceBufferOccurrences"] == 1,
          {"unique": disp["compactedTraceCoordsUnique"], "allocator": disp["allocator"],
           "selectedOccurrences": stored["selection"]["selectedTraceBufferOccurrences"]})
    tap_weight_sum = sum(t["effectiveWeight"] for t in c["radianceTaps"])
    check("tap_effective_weights_sum_to_one", abs(tap_weight_sum - 1.0) < 1e-5, tap_weight_sum)
    check("accounted_offsets_are_half4",
          all(len(n["probeOffsetRawHalf4"]) == 4 for n in c["neighbors"])
          and stored["inputs"]["probeWorldOffset"]["descriptor"]["BytesPerElement"] == 8,
          {"bytesPerElement": stored["inputs"]["probeWorldOffset"]["descriptor"]["BytesPerElement"],
           "numElements": stored["inputs"]["probeWorldOffset"]["descriptor"]["NumElements"]})

    depths = [n["depth"]["distance"] for n in c["neighbors"]]
    recomputed_depth = nested_depth(depths, c["fractionalWeights"])
    check("stored_cache_depth_recomputes_from_eight_depths",
          float(recomputed_depth) == float(c["cacheTraceHitDistance"]),
          {"recomputed": float(recomputed_depth), "stored": c["cacheTraceHitDistance"]})
    check("cache_depth_matches_captured_trace_hit",
          float(c["cacheTraceHitDistance"]) == float(cmp["capturedDistance"])
          and cmp["depthExact"] and cmp["depthAbsDiff"] == 0.0,
          {"replayed": c["cacheTraceHitDistance"], "captured": cmp["capturedDistance"]})

    model = np.array(c["cacheRadiance"], dtype=np.float32) * np.float32(
        stored["parameters"]["view"]["PreExposure"])
    final = np.array(cmp["finalRadianceComparison"]["decoded"], dtype=np.float32)
    tolerance = np.abs(model) * np.array(
        stored["parameters"]["screenProbe"]["TargetFormatQuantizationError"], dtype=np.float32)
    check("radiance_forward_consistency_is_within_storage_tolerance",
          bool(np.all(np.abs(final - model) <= tolerance)),
          {"absDiff": np.abs(final - model).tolist(), "tolerance": tolerance.tolist()})
    check("no_final_radiance_equality_claim",
          cmp["finalRadianceComparison"]["isEqualityClaim"] is False
          and stored["boundary"]["finalRadianceEqualityClaimed"] is False,
          True)

    blue = stored["blueNoise"]
    check("blue_noise_pointer_suffix_matches_uniform_byte_40",
          blue["pointerSuffix16"] == blue["pointerFromContentBytesAt40"]
          and blue["uniformByteOffset"] == 40,
          {"pointer": blue["pointer"], "suffix16": blue["pointerSuffix16"]})
    check("blue_noise_export_matches_binding_pointer",
          blue_export["texturePointer"] == blue["pointer"]
          and blue_export["path"] == blue["exportFile"]
          and blue_export["srgb"] is False
          and blue_export["memoryChannelOrder"] == "B,G,R,A",
          blue_export["path"])

    rehash_failures = []
    for key, rec in stored["inputs"].items():
        path = args.capture / rec["file"]
        if not path.exists():
            rehash_failures.append({"key": key, "error": "missing"})
            continue
        actual = sha256(path)
        if actual != rec["sha256"]:
            rehash_failures.append({"key": key, "expected": rec["sha256"], "actual": actual})
    check("all_input_files_rehash_to_recorded_sha256", not rehash_failures, rehash_failures)

    blue_path = args.capture / blue["exportFile"]
    check("blue_noise_payload_rehashes",
          blue_path.exists() and sha256(blue_path) == blue["payload"]["sha256"],
          blue["payload"]["sha256"])

    refs = stored["referenceStructs"]
    check("referenced_structs_7_valid_1_null",
          len(refs["records"]) == 8
          and sum(1 for r in refs["records"] if r["status"] == "validated") == 7
          and sum(1 for r in refs["records"] if r["status"] == "explicit-unbound-gap") == 1
          and refs["nestedBlueNoise"]["dimensions"] == [128, 128, 64],
          {"records": len(refs["records"]), "null": refs["null"]})
    check("permutation_evidence_structured_importance_and_cache",
          permutation["passPointer"] == stored["capture"]["pass"]["Pointer"]
          and permutation["structuredImportanceSampling"] is True
          and permutation["radianceCache"] is True,
          {"pass": permutation["passPointer"],
           "cvars": permutation["evidence"]["cvars"]})

    check("md_states_depth_result_and_boundary",
          "394.431884765625" in md
          and "bit-exact" in md
          and "not a claim that the cache subtotal equals" in md
          and "group-size caveat" in md
          and "No GPU" in md,
          {"lines": len(md.splitlines())})

    repro = json.loads((HERE / "reproducibility.json").read_text(encoding="utf-8"))
    check("recorded_report_sha_matches_artifact",
          repro["sha256"]["world-cache-replay.json"].lower() == sha256(HERE / "world-cache-replay.json"),
          repro["sha256"]["world-cache-replay.json"])
    check("recorded_markdown_sha_matches_artifact",
          repro["sha256"]["world-cache-replay.md"].lower() == sha256(HERE / "world-cache-replay.md"),
          repro["sha256"]["world-cache-replay.md"])

    check("source_evidence_has_15_entries", len(stored["sourceEvidence"]) == 15,
          len(stored["sourceEvidence"]))

    passed = all(c["passed"] for c in checks)
    verification = {
        "schema": "unreal-understanding-world-cache-verification-v1",
        "passed": passed,
        "checks": checks,
        "artifacts": {
            "report": {"file": "world-cache-replay.json",
                       "sha256": sha256(HERE / "world-cache-replay.json")},
            "markdown": {"file": "world-cache-replay.md",
                         "sha256": sha256(HERE / "world-cache-replay.md")},
            "blueNoiseExport": {"file": "blue-noise-export.json",
                                "sha256": sha256(HERE / "blue-noise-export.json")},
            "permutationEvidence": {"file": "permutation-evidence.json",
                                    "sha256": sha256(HERE / "permutation-evidence.json")},
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(verification, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": passed, "checks": len(checks),
                      "failed": [c["name"] for c in checks if not c["passed"]]}))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
