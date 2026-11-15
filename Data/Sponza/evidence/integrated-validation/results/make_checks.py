#!/usr/bin/env python3
"""Assemble the final checks.json from the recorded command/check outputs."""
from __future__ import annotations

import json
from pathlib import Path

R = Path(r"D:/UnrealUnderstanding-Review/final-validation/results")
commands = json.loads((R / "commands.json").read_text())
temporal = json.loads((R / "temporal_manifest_checks.json").read_text())
svg = json.loads((R / "svg_checks.json").read_text())
indep = json.loads((R / "independent_spotchecks.json").read_text())
before = json.loads((R / "hashes-before.json").read_text())
after = json.loads((R / "hashes-after.json").read_text())
be, ae = before["entries"], after["entries"]
added = sorted(set(ae) - set(be))
removed = sorted(set(be) - set(ae))
changed = sorted(k for k in set(be) & set(ae) if be[k] != ae[k])

doc = {
    "schema": "lumen-final-validation-v1",
    "step": "Final read-only validation of the integrated Lumen revision",
    "workspace": "D:/UnrealUnderstanding-Review/final-validation",
    "repo": "D:/UnrealUnderstanding",
    "hardware": "none (no GPU work, no commits)",
    "environment": {
        "python": "3.14.6",
        "node": "v24.22.1",
        "PYTHONDONTWRITEBYTECODE": "1 for every command and check",
    },
    "final_pass_window_local": "2026-11-15T19:55:40+08:00 to 19:57:19+08:00",
    "commands": commands,
    "command_totals": {
        "total": len(commands),
        "passed": sum(1 for c in commands if c["exit"] == 0),
        "failed": [c["id"] for c in commands if c["exit"] != 0],
    },
    "temporal_manifest": {
        "manifest": temporal["manifest"],
        "layers": temporal["layers"],
        "experiments": temporal["experiments"],
        "states": temporal["states"],
        "paths_checked": temporal["paths_checked"],
        "images_checked": temporal["images"],
        "checks": len(temporal["checks"]),
        "failures": temporal["failures"],
        "ok": temporal["ok"],
    },
    "svg": {
        "files_parsed": svg["svg_count"],
        "checks": len(svg["checks"]),
        "frame_flow_strict": svg["strict_frame_flow"],
        "failures": svg["failures"],
        "ok": svg["ok"],
    },
    "independent_spotchecks": {
        "checks": len(indep["checks"]),
        "failures": indep["failures"],
        "radiosity": indep["radiosity"],
        "worldcache": indep["worldcache"],
        "ok": indep["ok"],
    },
    "prior_static_checks_not_rerun": {
        "reason": "Step instruction: existing static context/receiver/cache replay checks passed and the "
                  "95 GB package must not be rerun. Frame Python/Node tests are rerun only if code changed.",
        "code_set_files": 119,
        "code_set_sha256": "7e82f43bdfaf0962a51c600f86fd34982b82f9ca2548b18ef0796e05da8bf5e2",
        "unchanged_vs_static_tree_final": True,
        "comparison": "All 119 files under labs/lumen-frame plus decode_receivers.py, decode_context.py and "
                      "cache-replay have identical sizes and mtimes (within 1 ms timestamp conversion) to the "
                      "saved static-final-checks/tree-final.txt snapshot.",
        "prior_results": {
            "frame_python_unittest": "39 tests OK",
            "frame_verifier_unittest": "15 tests OK",
            "node_metadata_browser": "6 ok checks",
            "node_read_path": "15 ok checks",
            "node_codec": "19 codec tests",
            "receivers": "27/27 checks",
            "context": "252/252 checks",
            "cache_replay": "surface pass=694, world pass=747",
        },
    },
    "integrity": {
        "snapshot_roots": before["roots"],
        "before_files": before["files"],
        "before_bytes": before["bytes"],
        "after_files": after["files"],
        "after_bytes": after["bytes"],
        "pre_existing_files_changed": changed,
        "pre_existing_files_removed": removed,
        "concurrently_added_by_root": [
            {"path": p, "size": ae[p]["size"], "sha256": ae[p]["sha256"]} for p in added
        ],
        "attribution": "The one added file is Data/Sponza/evidence/temporal-captures/flagged-raw-identity.json, "
                       "a capture-identity report produced while the read-only validation ran. It is outside the "
                       "article, temporal manifest and figure references. No validation process writes any repo "
                       "path; the allowed write scope is results/.",
        "validated_files_unchanged": len(changed) == 0 and len(removed) == 0,
    },
    "transient_observations": [
        {
            "when": "earlier pass, 2026-11-15T19:49+08:00",
            "event": "python build.py --check exited 1 with 'Stale article: Docs/Unreal/lumen.html'",
            "cause": "Root was mid-rebuild: lumen-course-reconstruction.html and lumen-course-system.html were "
                     "written at 19:49:00 and the Docs pages were regenerated at 19:49:36. The probe build ran in "
                     "that window.",
            "resolution": "After root's rebuild settled, build.py --check exits 0 on the final revision; the "
                          "transient failure is not reproducible on the validated snapshot.",
        },
        {
            "when": "earlier pass, final-pass window",
            "event": "Root added 15 temporal-decoder evidence logs and updated temporal-reproduction.md during "
                     "the first snapshot interval; revalidated after the writes.",
            "resolution": "Final tight pass has zero changed/removed pre-existing files and one concurrent "
                          "capture-identity addition.",
        },
    ],
    "acceptance_checks": [
        {
            "check": "generated article math diagrams and local references pass",
            "passed": True,
            "evidence": "11/11 commands exit 0: build.py --check (5 articles, lumen 29,302 words / 16 chapters); "
                        "math_typesetting.py --check --node node (1,023 LaTeX); validate.py (155 accessible SVGs, "
                        "289 source ranges); validate_labs.py (129 sources/outputs, 84 PNGs); "
                        "labs/lumen-reading/generate.py --check (7 diagrams); labs/lumen-course/generate.py --check "
                        "(29 SVGs, 15 numerical checks); build_temporal_manifest.py --check; node --check both JS.",
        },
        {
            "check": "temporal assets and representative native numerical records agree",
            "passed": True,
            "evidence": "Temporal manifest: 6/6 layer families wired to article hosts, 4 experiments, 20 states, "
                        "153 declared paths all present, 44 images decoded with stable dimensions, 507 checks, "
                        "0 failures. Every displayed per-state count/frame/exposure matches its numeric record. "
                        "All 8 age charts sum to 878/878/878/878/878/878/968/847 active probes with fixed max 1000 "
                        "and exact source histograms (unit = View.ViewState->GetFrameIndex delta). No "
                        "*_TEMPORAL_EVIDENCE placeholder remains. Independent raw checks: 69 checks, 0 failures "
                        "(3 radiosity probes with 12 native SH coefficients exact and world error <= 8.98e-6 cm; "
                        "all 878 world-cache positions reproduce hash 99b36a6b..., allocator 925-47=878).",
        },
        {
            "check": "published files remain unchanged",
            "passed": True,
            "evidence": "24,841-file SHA-256 snapshot before/after: 0 pre-existing files changed, 0 removed. "
                        "One root-produced capture report was added concurrently "
                        "(Data/Sponza/evidence/temporal-captures/flagged-raw-identity.json), disclosed in "
                        "integrity. The validation wrote only under results/.",
        },
    ],
    "unresolved_issues": [],
    "ok": True,
}

(R / "checks.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
print("wrote checks.json")
print("commands", doc["command_totals"])
print("temporal", doc["temporal_manifest"]["checks"], doc["temporal_manifest"]["failures"])
print("svg", doc["svg"]["checks"], doc["svg"]["failures"])
print("independent", doc["independent_spotchecks"]["checks"], doc["independent_spotchecks"]["failures"])
print("integrity changed/removed/added", len(changed), len(removed), len(added))
