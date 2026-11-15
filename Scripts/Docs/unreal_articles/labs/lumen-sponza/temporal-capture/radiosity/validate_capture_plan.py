#!/usr/bin/env python3
"""Static validation of the Radiosity narrow surface-cache capture plan.

No Unreal launch and no GPU work.  It checks the copied project identity, the
unchanged launchers, the already-executed -150 cm camera validation, and that
the planned five narrow dumps fit the 8 GiB workspace / 45 GiB cumulative cap.

The filter below is intentionally narrow: exact resource names, one pass family
per resource, so the selected scene depth, normal, card/page buffers and
radiosity SH atlases are captured at the same state without broad atlas
duplication.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
SPONZA = Path(r"D:\UnrealUnderstanding-Sponza")
MOTION = Path(r"D:\UnrealUnderstanding-Motion")
TEMPORAL = Path(r"D:\UnrealUnderstanding-Temporal")
ENGINE = Path(r"D:\UnrealEngine")
OUT = WORKSPACE / "output" / "temporal"
PROJECT = WORKSPACE / "project" / "UnrealLab"
MAP_REL = Path("Content") / "Labs" / "SponzaTeaching.umap"
SETUP_PATH = WORKSPACE / "output" / "final-setup.json"

BUDGET_BYTES = 8 * 1024 ** 3            # this workspace's new raw cap
CUMULATIVE_BUDGET_BYTES = 45 * 1024 ** 3
EXPECTED_DUMPS = 5                       # baseline, light 1/64, camera 1/64
CAMERA_TARGET_CM = [-850.0, -150.0, 90.0]
CAMERA_ROTATION = {"pitch": 13.0, "yaw": 0.0, "roll": 0.0}
CAMERA_HFOV = 70.0
PRIOR_VALIDATION_RUN = MOTION / "output" / "temporal" / "run-20261115-104035.json"

# One pass family per resource family.  Integrate binds the complete card/page
# state plus SceneDepth, SceneNormal, the three radiosity SH atlases,
# NumFramesAccumulatedAtlas and SceneIndirectLighting.  ConvertToSH is the only
# selected source of FilteredTraceRadianceAtlas.  CardCaptureRects is bound by
# CopyCardCaptureLightingToAtlas.  BuildRadiosityTiles contributes the
# source-confirmable radiosity placement parameter block (CardTiles only).
CARD_STATE_RESOURCES = [
    "Lumen.SceneDepth",
    "Lumen.SceneNormal",
    "Lumen.Cards",
    "Lumen.MeshCards",
    "Lumen.PageBuffer",
    "Lumen.PageTable",
    "Lumen.CardTiles",
]
RADIOSITY_STATE_RESOURCES = [
    "Lumen.Radiosity.ProbeSHRedAtlas",
    "Lumen.Radiosity.ProbeSHGreenAtlas",
    "Lumen.Radiosity.ProbeSHBlueAtlas",
    "Lumen.Radiosity.NumFramesAccumulatedAtlas",
    "Lumen.Radiosity.SceneIndirectLighting",
]
RADIOSITY_TRACE_RESOURCES = ["Lumen.Radiosity.FilteredTraceRadianceAtlas"]
CARD_CAPTURE_RESOURCES = ["Lumen.CardCaptureRects"]
VIEW_RESOURCES = ["ViewData.PrimaryViews", "SceneDepthZ"]

PROBE_RESOURCES = []      # kept for the import surface used by older helpers
RADIANCE_RESOURCES = []
CARD_RESOURCES = CARD_STATE_RESOURCES + CARD_CAPTURE_RESOURCES
RADIOSITY_RESOURCES = RADIOSITY_STATE_RESOURCES + RADIOSITY_TRACE_RESOURCES


def filters():
    """Return the exact narrow (pass pattern, resource pattern) pairs."""
    pairs = [("*Integrate*", r) for r in
             CARD_STATE_RESOURCES + RADIOSITY_STATE_RESOURCES]
    pairs += [("*ConvertToSH*", r) for r in RADIOSITY_TRACE_RESOURCES]
    pairs += [("*CopyCardCaptureLightingToAtlas*", r) for r in CARD_CAPTURE_RESOURCES]
    pairs += [("*BuildRadiosityTiles*", "Lumen.CardTiles")]
    # Batched lights contributes the source-confirmable CachedLightingPreExposure
    # pass parameter block; CardTiles is already selected, so no extra atlas
    # version is introduced by this pair.
    pairs += [("*Batched lights*", "Lumen.CardTiles")]
    pairs += [("*DepthPassParallel*", r) for r in VIEW_RESOURCES]
    pairs += [("*BasePassParallel*", r) for r in VIEW_RESOURCES]
    return pairs


def all_resources():
    return (CARD_STATE_RESOURCES + RADIOSITY_STATE_RESOURCES +
            RADIOSITY_TRACE_RESOURCES + CARD_CAPTURE_RESOURCES + VIEW_RESOURCES)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def dir_bytes(path: Path) -> int:
    try:
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    except Exception:
        return 0


def check_project():
    setup = load_json(SETUP_PATH)
    result = {"ok": True, "errors": []}
    map_file = PROJECT / MAP_REL
    result["map_file"] = str(map_file)
    result["map_exists"] = map_file.exists()
    if not map_file.exists():
        result["errors"].append("copied map missing")
        result["ok"] = False
        return result, setup
    result["map_sha256"] = sha256(map_file)
    result["map_matches_final_setup"] = (
        result["map_sha256"].lower() ==
        str(setup["hashes"]["map_sha256"]).lower())
    result["map_matches_saved_state"] = (
        result["map_sha256"].lower() ==
        str(setup["hashes"].get("map_sha256_after_save", "")).lower())
    if not result["map_matches_final_setup"]:
        result["errors"].append("copied map hash differs from final-setup")
        result["ok"] = False

    proj_modules = load_json(PROJECT / "Binaries" / "Win64" / "UnrealEditor.modules")
    eng_modules = load_json(ENGINE / "Engine" / "Binaries" / "Win64" / "UnrealEditor.modules")
    result["project_build_id"] = proj_modules.get("BuildId")
    result["engine_build_id"] = eng_modules.get("BuildId")
    result["build_id_matches"] = (
        str(proj_modules.get("BuildId", "")).lower() ==
        str(eng_modules.get("BuildId", "")).lower())
    if not result["build_id_matches"]:
        result["errors"].append("project module BuildId differs from engine")
        result["ok"] = False

    result["runtime_dirs_present"] = sorted(
        p.name for p in PROJECT.iterdir()
        if p.is_dir() and p.name in {"Saved", "Intermediate", "DerivedDataCache", "Provenance"})
    result["runtime_dirs_policy"] = (
        "created by Unreal during a run; root cleans them later; never copied "
        "from the retained scene")

    retained = Path(r"C:\Users\Administrator\Documents\Unreal Projects\UnrealLab")
    result["retained_project"] = str(retained)
    result["core_copy"] = {}
    if retained.exists():
        for sub in ("Content", "Config", "Source", "Binaries"):
            src_root = retained / sub
            dst_root = PROJECT / sub
            src_files = [p for p in src_root.rglob("*")
                         if p.is_file() and p.suffix.lower() != ".pdb"]
            missing, changed = [], []
            for src in src_files:
                rel = src.relative_to(src_root)
                dst = dst_root / rel
                if not dst.exists():
                    missing.append(str(rel))
                elif sha256(src) != sha256(dst):
                    changed.append(str(rel))
            result["core_copy"][sub] = {
                "source_file_count": len(src_files),
                "missing": missing[:20],
                "changed": changed[:20],
                "matches": not missing and not changed,
            }
            if missing or changed:
                result["errors"].append("%s copy does not match retained project" % sub)
                result["ok"] = False
        src_up = retained / "UnrealLab.uproject"
        dst_up = PROJECT / "UnrealLab.uproject"
        result["uproject_matches_retained"] = (
            src_up.exists() and dst_up.exists() and sha256(src_up) == sha256(dst_up))
        if not result["uproject_matches_retained"]:
            result["errors"].append("copied .uproject differs from retained project")
            result["ok"] = False
    else:
        result["core_copy"]["skipped"] = "retained project not present on this host"

    result["launchers"] = {}
    for name in ("run_gpu_dump.ps1", "run_unreal_hidden.ps1"):
        a = WORKSPACE / "scripts" / name
        b = SPONZA / "scripts" / name
        rec = {"workspace_sha256": sha256(a) if a.exists() else None,
               "source_sha256": sha256(b) if b.exists() else None}
        rec["identical"] = bool(rec["workspace_sha256"] and
                                rec["workspace_sha256"] == rec["source_sha256"])
        result["launchers"][name] = rec
        if not rec["identical"]:
            result["errors"].append("launcher changed: " + name)
            result["ok"] = False

    cfg = setup["config"]
    result["setup_schema"] = setup.get("schema")
    result["hero"] = cfg["hero_camera"]
    result["sun"] = cfg["light"]["sun"]
    result["exposure_ev100"] = cfg["light"]["exposure_ev100"]
    result["accepted_hero"] = (
        [round(float(x), 3) for x in cfg["hero_camera"]["location_cm"]] ==
        [-850.0, 0.0, 90.0] and
        abs(float(cfg["hero_camera"]["rotation_degrees"]["pitch"]) - 13.0) < 1e-6 and
        abs(float(cfg["hero_camera"]["rotation_degrees"]["yaw"])) < 1e-6 and
        abs(float(cfg["hero_camera"]["horizontal_fov_deg"]) - 70.0) < 1e-6 and
        abs(float(cfg["light"]["sun"]["rotation_degrees"]["pitch"]) + 75.0) < 1e-6 and
        abs(float(cfg["light"]["sun"]["rotation_degrees"]["yaw"]) + 35.0) < 1e-6 and
        abs(float(cfg["light"]["sun"]["intensity_lux"]) - 80000.0) < 1e-6 and
        abs(float(cfg["light"]["exposure_ev100"]) - 11.0) < 1e-6)
    if not result["accepted_hero"]:
        result["errors"].append("final-setup does not match the accepted hero/light exposure state")
        result["ok"] = False
    return result, setup


def check_prior_camera_validation():
    """Read the already-executed Motion -150 cm move as read-only evidence."""
    rec = {"source": str(PRIOR_VALIDATION_RUN), "available": PRIOR_VALIDATION_RUN.exists()}
    if not PRIOR_VALIDATION_RUN.exists():
        rec["ok"] = False
        return rec
    run = load_json(PRIOR_VALIDATION_RUN)
    move = run.get("camera_move") or {}
    readback = move.get("readback") or {}
    rec.update({
        "run_id": run.get("run_id"),
        "applied": bool(move.get("applied")),
        "offset_cm": move.get("offset_cm"),
        "target_cm": move.get("target_cm"),
        "readback": readback,
        "map_sha256_after": run.get("map_sha256_after"),
        "ok": bool(
            move.get("applied") and
            [round(float(x), 3) for x in (move.get("target_cm") or [])] == CAMERA_TARGET_CM and
            readback.get("rotation_degrees") == CAMERA_ROTATION and
            abs(float(readback.get("horizontal_fov_deg", -1)) - CAMERA_HFOV) < 1e-6 and
            (move.get("pilot") or {}).get("pilot_matches") is True),
    })
    return rec


def estimate_budget():
    """Independent narrow-dump estimate from the source-confirmed formats.

    Each scene graph contributes 32 MiB SceneDepth, 128 MiB expanded SceneNormal,
    three 8 MiB SH atlases, 64 MiB SceneIndirectLighting and 64 MiB
    FilteredTraceRadianceAtlas, i.e. ~360 MiB per graph.  Five dumps with up to
    three RDG scene graphs each are projected below 5.3 GiB; the guard still
    enforces the hard 8 GiB cap.
    """
    per_graph = (32 + 128 + 3 * 8 + 64 + 64) * 1024 ** 2
    projected = EXPECTED_DUMPS * 3 * per_graph
    prior_motion = dir_bytes(MOTION / "output" / "gpu-dump" / "raw")
    prior_temporal = dir_bytes(TEMPORAL / "output" / "gpu-dump" / "raw")
    prior = prior_motion + prior_temporal
    cumulative = prior + projected
    return {
        "ok": bool(projected <= BUDGET_BYTES and cumulative <= CUMULATIVE_BUDGET_BYTES),
        "source": "source-confirmed 4096x4096 formats (SceneDepth PF_G16, SceneNormal expanded BC5, radiosity atlases)",
        "planned_dumps": EXPECTED_DUMPS,
        "required_labels": ["baseline", "light-001", "light-064", "camera-001", "camera-064"],
        "per_graph_bytes": per_graph,
        "estimated_total_bytes": projected,
        "estimated_total_gib": round(projected / 1024 ** 3, 4),
        "workspace_budget_bytes": BUDGET_BYTES,
        "workspace_budget_gib": 8.0,
        "workspace_ok": projected <= BUDGET_BYTES,
        "existing_motion_raw_bytes": prior_motion,
        "existing_temporal_raw_bytes": prior_temporal,
        "existing_raw_bytes": prior,
        "existing_raw_gib": round(prior / 1024 ** 3, 4),
        "cumulative_bytes": cumulative,
        "cumulative_gib": round(cumulative / 1024 ** 3, 4),
        "cumulative_budget_bytes": CUMULATIVE_BUDGET_BYTES,
        "cumulative_budget_gib": 45.0,
        "cumulative_ok": cumulative <= CUMULATIVE_BUDGET_BYTES,
        "note": ("Prior raw is the already-finished Motion + Temporal selected dumps "
                 "(27.91 GiB).  The separate 88 GiB exploratory Sponza package is not "
                 "part of this task's cumulative authorization."),
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    project, _setup = check_project()
    prior = check_prior_camera_validation()
    budget = estimate_budget()
    selected_filters = filters()
    filter_string = "|".join(f"{p}@{r}" for p, r in selected_filters)
    result = {
        "schema": "arda-radiosity-surface-capture-plan-validation-v1",
        "workspace": str(WORKSPACE),
        "project": project,
        "prior_camera_validation": prior,
        "budget": budget,
        "filters": {
            "pair_count": len(selected_filters),
            "filter_string_chars": len(filter_string),
            "under_windows_command_limit": len(filter_string) < 30000,
            "selected_resource_names": len(all_resources()),
            "resources": all_resources(),
        },
        "planned_states": {
            "baseline": {"sun_lux": 80000.0, "target_update": None},
            "light": {"from_lux": 80000.0, "to_lux": 20000.0,
                      "required_updates": [1, 64]},
            "restore_warm": {"sun_lux": 80000.0, "warm_draws": 64},
            "camera": {"start_cm": [-850.0, 0.0, 90.0],
                       "target_cm": CAMERA_TARGET_CM,
                       "rotation": CAMERA_ROTATION,
                       "hfov_deg": CAMERA_HFOV,
                       "required_updates": [1, 64]},
            "exposure_ev100": 11.0,
        },
        "ok": bool(project["ok"] and prior.get("ok") and budget.get("ok")),
    }
    result["result"] = "pass" if result["ok"] else "fail"
    if not result["ok"]:
        reasons = list(project["errors"])
        if not prior.get("ok"):
            reasons.append("prior -150 cm camera validation is not available")
        if not budget.get("ok"):
            reasons.append("projected narrow capture exceeds the 8 GiB / 45 GiB budget")
        result["reasons"] = reasons
    path = OUT / "capture-plan-validation.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": result["ok"],
        "map_sha256": project.get("map_sha256"),
        "map_matches_final_setup": project.get("map_matches_final_setup"),
        "build_id_matches": project.get("build_id_matches"),
        "launchers_identical": all(v["identical"] for v in project["launchers"].values()),
        "core_copy_matches": all(v.get("matches", True) for v in project["core_copy"].values()),
        "prior_camera_valid": prior.get("ok"),
        "filter_pairs": len(selected_filters),
        "selected_resource_names": len(all_resources()),
        "estimated_total_gib": budget.get("estimated_total_gib"),
        "workspace_ok": budget.get("workspace_ok"),
        "existing_raw_gib": budget.get("existing_raw_gib"),
        "cumulative_gib": budget.get("cumulative_gib"),
        "cumulative_ok": budget.get("cumulative_ok"),
        "validation": str(path),
    }, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
