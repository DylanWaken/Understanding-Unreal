#!/usr/bin/env python3
"""Verify the Motion camera-motion capture evidence without a GPU.

Checks the run session and machine manifest against the step's acceptance
criteria: same-run baseline/early/settled states, full selected families, exact
camera and frame identity, nonempty displayed views, unchanged scene/launchers
and the 10 GiB / 35 GiB raw budgets.  Writes output/capture-check.json.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
OUT = WORKSPACE / "output"
MANIFEST = OUT / "capture-manifest.json"
SETUP = OUT / "final-setup.json"
SPONZA = Path(r"D:\UnrealUnderstanding-Sponza")
CUMULATIVE_BUDGET = 35 * 1024 ** 3
EXPECTED_MAP = "3ef49eddd474fc16e6129dac3bcbb16bb4937529d9213296a865d368ede72c64"
REQUIRED = ("baseline", "camera-001", "camera-064")
EXPECTED_ACTUAL = {"baseline": 3, "camera-001": 1, "camera-064": 63}
EXPECTED_FRAMES = {"baseline": 127, "camera-001": 133, "camera-064": 198}
KEY_RESOURCES = [
    "Lumen.ScreenProbeGather.ScreenProbeTranslatedWorldPosition",
    "Lumen.ScreenProbeGather.ScreenProbeWorldNormal",
    "Lumen.ScreenProbeGather.ScreenProbeRadianceSHAmbient",
    "Lumen.ScreenProbeGather.ScreenProbeRadianceSHDirectional",
    "Lumen.ScreenProbeGather.ScreenProbeDisocclusion",
    "Lumen.ScreenProbeGather.HistoryFastUpdateMode_NumFramesAccumulated",
    "Lumen.RadianceCache.ProbeAllocator",
    "Lumen.RadianceCache.ProbeLastUsedFrame",
    "Lumen.RadianceCache.ProbeLastTracedFrame",
    "Lumen.RadianceCache.RadianceProbeIndirectionTexture",
    "Lumen.RadianceCache.ProbeWorldOffset",
    "Lumen.RadianceCache.FinalRadianceAtlas",
    "Lumen.RadianceCache.DepthProbeAtlasTexture",
    "Lumen.Radiosity.ProbeSHRedAtlas",
    "Lumen.Radiosity.ProbeSHGreenAtlas",
    "Lumen.Radiosity.ProbeSHBlueAtlas",
    "Lumen.Radiosity.NumFramesAccumulatedAtlas",
    "Lumen.Radiosity.FilteredTraceRadianceAtlas",
    "Lumen.Cards",
    "Lumen.PageTable",
    "Lumen.PageBuffer",
    "ViewData.PrimaryViews",
    "SceneDepthZ",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    session = json.loads(Path(manifest["session"]).read_text(encoding="utf-8"))
    setup = json.loads(SETUP.read_text(encoding="utf-8"))
    states = {s["label"]: s for s in manifest["states"]}
    checks = {}

    checks["required_states_present"] = all(label in states for label in REQUIRED)
    checks["dump_count_is_three"] = manifest["budget"] is not None and len(states) == 3
    checks["filter_count_136"] = int(session.get("filter_count_used") or 0) == 136
    checks["all_required_dumps_ok"] = all(
        states.get(label, {}).get("dump_status") == "ok" for label in REQUIRED)
    checks["all_required_59_present"] = all(
        states.get(label, {}).get("present_resource_count") == 59 for label in REQUIRED)
    checks["all_required_16_missing"] = all(
        states.get(label, {}).get("missing_resource_count") == 16 for label in REQUIRED)
    checks["all_required_key_resources"] = all(
        not (set(KEY_RESOURCES) & set(states.get(label, {}).get("missing_resources") or []))
        for label in REQUIRED)

    per_state = {}
    for label in REQUIRED:
        s = states.get(label) or {}
        native = s.get("native_screenshot") or {}
        lit = s.get("experimental_lit_offset") or {}
        dep = s.get("experimental_depth_offset") or {}
        cam = (s.get("camera") or {}).get("location_cm")
        expected_cam = [-850.0, 0.0, 90.0] if label == "baseline" else [-850.0, -150.0, 90.0]
        per_state[label] = {
            "actual_update": s.get("actual_update"),
            "actual_update_ok": s.get("actual_update") == EXPECTED_ACTUAL[label],
            "engine_frame": (s.get("raw_frame") or {}).get("engine_frame"),
            "engine_frame_ok": (s.get("raw_frame") or {}).get("engine_frame") == EXPECTED_FRAMES[label],
            "camera_cm": cam,
            "camera_ok": cam == expected_cam,
            "sun_lux": (s.get("sun") or {}).get("intensity_lux"),
            "sun_ok": (s.get("sun") or {}).get("intensity_lux") == 80000.0,
            "native_valid": bool(native.get("view_valid")),
            "native_same_frame": bool(native.get("same_frame_as_dump")),
            "lit_valid": bool(lit.get("view_valid")),
            "depth_valid": bool(dep.get("view_valid")),
            "lit_offset_phase": lit.get("phase_draw_offset_from_dump"),
            "lit_offset_frame": lit.get("engine_frame_offset_from_dump"),
            "depth_offset_phase": dep.get("phase_draw_offset_from_dump"),
            "depth_offset_frame": dep.get("engine_frame_offset_from_dump"),
            "lit_offset_ok": (lit.get("phase_draw_offset_from_dump") == 0 and
                              lit.get("engine_frame_offset_from_dump") == 1),
            "depth_offset_ok": (dep.get("phase_draw_offset_from_dump") == 1 and
                                dep.get("engine_frame_offset_from_dump") == 3),
        }
    checks["per_state_identity_and_images"] = all(
        all(v[k] for k in ("actual_update_ok", "engine_frame_ok", "camera_ok",
                           "sun_ok", "native_valid", "native_same_frame",
                           "lit_valid", "depth_valid", "lit_offset_ok",
                           "depth_offset_ok"))
        for v in per_state.values())

    checks["camera_move_applied"] = bool((session.get("camera_move") or {}).get("applied"))
    checks["camera_restored"] = bool(session.get("camera_restored"))
    checks["sun_restored"] = bool(session.get("sun_restored"))
    checks["exposure_restored"] = bool(session.get("exposure_restored"))
    checks["no_empty_views"] = (int(session.get("empty_capture_count") or 0) == 0 and
                                not (session.get("empty_clean_views") or []) and
                                not (session.get("empty_debug_views") or []) and
                                not (session.get("empty_native_views") or []))
    checks["debug_views_all_valid"] = all(
        bool(r.get("view_valid")) for r in manifest.get("debug_observations") or [])

    checks["map_unchanged"] = bool(session.get("map_unchanged"))
    checks["map_equals_expected"] = (session.get("map_sha256_after") == EXPECTED_MAP and
                                     session.get("map_sha256_before") == EXPECTED_MAP)
    checks["setup_map_hash_matches"] = (
        str(setup["hashes"]["map_sha256"]).lower() == EXPECTED_MAP)
    checks["engine_project_build_ids_match"] = bool(
        (session.get("env_hashes") or {}).get("build_id_matches"))

    checks["launchers_identical"] = True
    for name in ("run_gpu_dump.ps1", "run_unreal_hidden.ps1"):
        a = WORKSPACE / "scripts" / name
        b = SPONZA / "scripts" / name
        if not (a.exists() and b.exists() and sha256(a) == sha256(b)):
            checks["launchers_identical"] = False

    budget = session.get("budget") or {}
    checks["workspace_raw_within_10gib"] = (
        int(budget.get("workspace_used_all_runs_bytes") or 0) <= 10 * 1024 ** 3)
    checks["cumulative_raw_within_35gib"] = (
        int(budget.get("cumulative_used_bytes") or 0) <= CUMULATIVE_BUDGET)
    checks["budget_reported_not_exceeded"] = not bool(budget.get("exceeded"))

    ok = all(checks.values())
    result = {
        "schema": "arda-motion-capture-check-v1",
        "ok": ok,
        "run_id": manifest.get("run_id"),
        "manifest": str(MANIFEST),
        "session": manifest.get("session"),
        "report": manifest.get("report"),
        "checks": checks,
        "per_state": per_state,
        "budget": {
            "workspace_used_bytes": budget.get("workspace_used_all_runs_bytes"),
            "workspace_used_gib": budget.get("workspace_used_all_runs_gib"),
            "workspace_cap_bytes": 10 * 1024 ** 3,
            "cumulative_used_bytes": budget.get("cumulative_used_bytes"),
            "cumulative_used_gib": budget.get("cumulative_used_gib"),
            "cumulative_cap_bytes": CUMULATIVE_BUDGET,
        },
    }
    path = OUT / "capture-check.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": ok,
        "run_id": result["run_id"],
        "failed": [k for k, v in checks.items() if not v],
        "per_state": per_state,
        "budget": result["budget"],
        "check_path": str(path),
    }, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
