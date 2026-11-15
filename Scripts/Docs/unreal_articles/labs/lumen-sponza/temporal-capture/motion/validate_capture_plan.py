#!/usr/bin/env python3
"""Static validation of the Motion camera-only capture plan (no Unreal, no GPU).

This copy is deliberately bounded to this workspace.  It checks only what the
capture step needs and nothing that requires an expensive whole-engine scan:

  * the copied project carries the retained saved Sponza map bit-for-bit;
  * the copied project module BuildId still matches the engine;
  * the two known-working launchers are byte-identical to the Sponza copies;
  * the retained project core is still the same copy (no scene edit);
  * the four planned full-filter dumps fit the 10 GiB workspace budget and the
    approved 35 GiB cumulative budget with the existing Temporal 19.5536 GiB;
  * the -150 cm camera target is the one already validated by the Temporal
    rendered-view retry (run 20261115-101923).

It does not read the 88 GiB size-oracle dump, does not reconstruct geometry and
does not scan the engine source tree.  It deliberately does not launch Unreal.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
SPONZA = Path(r"D:\UnrealUnderstanding-Sponza")
TEMPORAL = Path(r"D:\UnrealUnderstanding-Temporal")
ENGINE = Path(r"D:\UnrealEngine")
OUT = WORKSPACE / "output" / "temporal"
PROJECT = WORKSPACE / "project" / "UnrealLab"
MAP_REL = Path("Content") / "Labs" / "SponzaTeaching.umap"
SETUP_PATH = WORKSPACE / "output" / "final-setup.json"
BUDGET_BYTES = 10 * 1024 ** 3          # this workspace's raw cap
CUMULATIVE_BUDGET_BYTES = 35 * 1024 ** 3
EXPECTED_DUMPS = 3                     # baseline + required early 1 + settled 64
CAMERA_TARGET_CM = [-850.0, -150.0, 90.0]
CAMERA_ROTATION = {"pitch": 13.0, "yaw": 0.0, "roll": 0.0}
CAMERA_HFOV = 70.0
PRIOR_RETRY_RUN = TEMPORAL / "output" / "temporal" / "run-20261115-101923.json"
MOTION_ORACLE = WORKSPACE / "output" / "temporal" / "run-20261115-103742.json"

# DumpGPU's unified filter matches the full RDG scope path, not just the leaf
# event name.  Every pattern is therefore wildcard-bounded on both sides.
PROBE_PASSES = [
    "*UniformPlacement*", "*AdaptivePlacement*", "*GenerateRays*",
    "*ScreenProbeGatherScreenData*", "*ScreenProbeConvertToIrradiance*",
    "*ScreenProbeIntegrate*", "*TemporalReprojection*",
    "*TraceGlobalSDFAndApplyRadianceCache*", "*CompositeTraces*",
    "*ComputeLightingPDF*", "*MarkRadianceProbes*",
    "*DiffuseIndirectComposite*", "*ScreenProbeGatherDebug*",
]
RADIANCE_PASSES = [
    "*ClearProbeIndirectionCS*", "*UpdateCacheForUsedProbes*",
    "*AllocateUsedProbes*", "*SetupProbeIndirectArgsCS*",
    "*CompositeTracesIntoAtlas*", "*FilterProbeRadiance*",
    "*TraceFromProbes*", "*FixupBordersAndGenerateMips*",
    "*CalculateProbeIrradiance*", "*PrepareProbeOcclusion*",
    "*TraceGlobalSDFAndApplyRadianceCache*", "*ComputeLightingPDF*",
]
CARD_PASSES = [
    "*Build*cards*", "*BuildPageUpdatePriorityHistogram*",
    "*SpliceCardPagesIntoTiles*", "*CalculateCardTileDepthRanges*", "*CardPage*",
    "*CopyToSurfaceCache*", "*CompressToSurfaceCache*", "*ClearCardCapture*",
    "*CopyCardCaptureLightingToAtlas*",
]
CARD_RESOURCES = [
    "Lumen.Cards", "Lumen.PageTable", "Lumen.PageBuffer", "Lumen.MeshCards",
    "Lumen.PrimitiveGroup", "Lumen.CardPageLastUsedBuffer",
    "Lumen.CardPageHighResLastUsedBuffer", "Lumen.CardPageTileAllocator",
    "Lumen.CardTiles", "Lumen.CardTileDepthRanges", "Lumen.CardCaptureRects",
]
PROBE_RESOURCES = [
    "Lumen.ScreenProbeGather.ScreenProbeTranslatedWorldPosition",
    "Lumen.ScreenProbeGather.ScreenProbeWorldNormal",
    "Lumen.ScreenProbeGather.ScreenProbeSceneDepth",
    "Lumen.ScreenProbeGather.ScreenProbeWorldSpeed",
    "Lumen.ScreenProbeGather.ScreenProbeRadiance",
    "Lumen.ScreenProbeGather.ScreenProbeRadianceSHAmbient",
    "Lumen.ScreenProbeGather.ScreenProbeRadianceSHDirectional",
    "Lumen.ScreenProbeGather.ScreenProbeHitDistance",
    "Lumen.ScreenProbeGather.ScreenProbeTraceMoving",
    "Lumen.ScreenProbeGather.ScreenProbeMoving",
    "Lumen.ScreenProbeGather.ScreenProbeDisocclusion",
    "Lumen.ScreenProbeGather.ScreenProbeFilteredRadiance",
    "Lumen.ScreenProbeGather.ScreenProbeFilteredRadianceWithBorder",
    "Lumen.ScreenProbeGather.ScreenProbeIrradianceWithBorder",
    "Lumen.ScreenProbeGather.ScreenProbeExtraAOWithBorder",
    "Lumen.ScreenProbeGather.RayInfosForTracing",
    "Lumen.ScreenProbeGather.TraceRadiance",
    "Lumen.ScreenProbeGather.TraceHit",
    "Lumen.ScreenProbeGather.DiffuseIndirect",
    "Lumen.ScreenProbeGather.LightIsMoving",
    "Lumen.ScreenProbeGather.HistoryFastUpdateMode_NumFramesAccumulated",
    "Lumen.ScreenProbeGather.AdaptiveScreenProbeData",
    "Lumen.ScreenProbeGather.NumAdaptiveScreenProbes",
    "Lumen.ScreenProbeGather.ScreenTileAdaptiveProbeHeader",
    "Lumen.ScreenProbeGather.ScreenTileAdaptiveProbeIndices",
    "Lumen.ScreenProbeGather.AdaptiveProbePlacementMask",
    "Lumen.ScreenProbeGather.BRDFProbabilityDensityFunctionSH",
    "Lumen.ScreenProbeGather.LightingProbabilityDensityFunction",
    "Lumen.ScreenProbeGather.CompactedTraceTexelAllocator",
    "Lumen.ScreenProbeGather.CompactedTraceTexelData",
    "Lumen.ScreenProbeGather.ScreenProbeIndirectArgs",
    "Lumen.ScreenProbeGather.IntegrateIndirectArgs",
    "Lumen.ScreenProbeGather.IntegrateTileData",
]
RADIOSITY_RESOURCES = [
    "Lumen.Radiosity.ProbeSHRedAtlas",
    "Lumen.Radiosity.ProbeSHGreenAtlas",
    "Lumen.Radiosity.ProbeSHBlueAtlas",
    "Lumen.Radiosity.NumFramesAccumulatedAtlas",
    "Lumen.Radiosity.TraceRadianceAtlas",
    "Lumen.Radiosity.TraceHitDistanceAtlas",
    "Lumen.Radiosity.FilteredTraceRadianceAtlas",
    "Lumen.Radiosity.SceneIndirectLighting",
    "Lumen.RadiosityTileAllocator",
    "Lumen.RadiosityTiles",
    "Lumen.RadiosityIndirectArgs",
    "Lumen.RadiosityVisualizeProbeAllocator",
    "Lumen.RadiosityVisualizeProbeData",
]
RADIOSITY_PASSES = [
    "*BuildRadiosityTiles*", "*ConvertToSH*", "*Integrate*",
    "*BuildVisualizeProbes*",
]
RADIANCE_RESOURCES = [
    "Lumen.RadianceCache.RadianceProbeIndirectionTexture",
    "Lumen.RadianceCache.ProbeWorldOffset",
    "Lumen.RadianceCache.ProbeAllocator",
    "Lumen.RadianceCache.ProbeFreeList",
    "Lumen.RadianceCache.ProbeFreeListAllocator",
    "Lumen.RadianceCache.ProbeLastUsedFrame",
    "Lumen.RadianceCache.ProbeLastTracedFrame",
    "Lumen.RadianceCache.ProbeValid",
    "Lumen.RadianceCache.FinalRadianceAtlas",
    "Lumen.RadianceCache.FinalIrradianceAtlas",
    "Lumen.RadianceCache.ProbeOcclusionAtlas",
    "Lumen.RadianceCache.DepthProbeAtlasTexture",
    "Lumen.RadianceCache.FinalSkyVisibilityAtlas",
    "Lumen.RadianceCache.RadianceProbeSH_PDF",
    "Lumen.RadianceCache.ProbeTraceData",
    "Lumen.RadianceCache.ProbeTraceAllocator",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def filters():
    """Return the exact (pass pattern, resource pattern) pairs used by capture.

    This is copied unchanged from the Temporal reference: the same 136 pairs
    that produced the working full-filter dumps.  Camera-only selection is a
    subset of what is captured, never a smaller filter.
    """
    pairs = [(p, "Lumen.ScreenProbeGather.*") for p in PROBE_PASSES]
    pairs += [(p, "Lumen.RadianceCache.*") for p in RADIANCE_PASSES]
    pairs += [(p, "Lumen.Radiosity.*") for p in RADIOSITY_PASSES]
    pairs += [("*BuildVisualizeProbes*", res) for res in
              ("Lumen.RadiosityVisualizeProbeAllocator",
               "Lumen.RadiosityVisualizeProbeData")]
    pairs += [("*BuildRadiosityTiles*", res) for res in
              ("Lumen.CardTileAllocator", "Lumen.CardTiles")]
    pairs += [(p, res) for p in CARD_PASSES for res in CARD_RESOURCES]
    pairs += [
        ("*DepthPassParallel*", "SceneDepthZ"),
        ("*BasePassParallel*", "SceneDepthZ"),
        ("*DepthPassParallel*", "ViewData.PrimaryViews"),
        ("*BasePassParallel*", "ViewData.PrimaryViews"),
    ]
    return pairs


def all_resources():
    return (PROBE_RESOURCES + RADIANCE_RESOURCES + RADIOSITY_RESOURCES +
            CARD_RESOURCES + ["SceneDepthZ", "ViewData.PrimaryViews"])


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

    # Unreal creates these on first load.  They are reported, not treated as a
    # copy error: this step must not archive them until the run is complete.
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
    """Read the already-executed -150 cm rendered-view retry as read-only proof."""
    rec = {"source": str(PRIOR_RETRY_RUN), "available": PRIOR_RETRY_RUN.exists()}
    if not PRIOR_RETRY_RUN.exists():
        rec["ok"] = False
        return rec
    run = load_json(PRIOR_RETRY_RUN)
    move = run.get("camera_move") or {}
    probe = run.get("camera_probe") or {}
    chosen = probe.get("chosen") or {}
    readback = move.get("readback") or {}
    rec.update({
        "applied": bool(move.get("applied")),
        "offset_cm": move.get("offset_cm"),
        "target_cm": move.get("target_cm"),
        "readback": readback,
        "chosen_view_valid": chosen.get("view_valid"),
        "chosen_empty_view": chosen.get("empty_view"),
        "map_sha256_after": run.get("map_sha256_after"),
        "ok": bool(
            move.get("applied") and
            [round(float(x), 3) for x in (move.get("target_cm") or [])] == CAMERA_TARGET_CM and
            readback.get("rotation_degrees") == CAMERA_ROTATION and
            abs(float(readback.get("horizontal_fov_deg", -1)) - CAMERA_HFOV) < 1e-6 and
            chosen.get("view_valid") is True and
            chosen.get("empty_view") is False),
    })
    return rec


def estimate_budget():
    """Use this workspace's first-attempt sizes plus a 2.99 GB settled estimate.

    The first attempt proved that a valid -150 cm view produces ~2.99 GB per
    full-filter dump (not the ~1.98 GB empty +150 cm view used by the earlier
    Temporal estimate).  Four such dumps would exceed 10 GiB, so target 8 is
    optional and the required set is baseline + early 1 + settled 64.
    """
    fallback = {
        "baseline": 2990617601,
        "camera-001": 2990384313,
        "camera-064": 2990000000,
    }
    per_dump = {}
    source = "first-attempt baseline/camera-001 plus 2.99 GB settled estimate"
    if MOTION_ORACLE.exists():
        try:
            run = load_json(MOTION_ORACLE)
            labels = {d.get("label"): d for d in run.get("dumps") or []}
            for label in fallback:
                if label in labels and labels[label].get("total_bytes"):
                    per_dump[label] = int(labels[label]["total_bytes"])
            if per_dump:
                source = str(MOTION_ORACLE)
        except Exception:
            per_dump = {}
    if not per_dump:
        per_dump = dict(fallback)
    if "camera-064" not in per_dump:
        per_dump["camera-064"] = fallback["camera-064"]
    projected = sum(per_dump.values())
    prior_raw = dir_bytes(TEMPORAL / "output" / "gpu-dump" / "raw")
    cumulative = prior_raw + projected
    return {
        "ok": bool(projected <= BUDGET_BYTES and cumulative <= CUMULATIVE_BUDGET_BYTES),
        "source": source,
        "planned_dumps": EXPECTED_DUMPS,
        "required_labels": ["baseline", "camera-001", "camera-064"],
        "optional_targets": [8],
        "per_dump_bytes": per_dump,
        "estimated_total_bytes": projected,
        "estimated_total_gib": round(projected / 1024 ** 3, 4),
        "workspace_budget_bytes": BUDGET_BYTES,
        "workspace_budget_gib": 10.0,
        "workspace_ok": projected <= BUDGET_BYTES,
        "existing_temporal_raw_bytes": prior_raw,
        "existing_temporal_raw_gib": round(prior_raw / 1024 ** 3, 4),
        "cumulative_bytes": cumulative,
        "cumulative_gib": round(cumulative / 1024 ** 3, 4),
        "cumulative_budget_bytes": CUMULATIVE_BUDGET_BYTES,
        "cumulative_budget_gib": 35.0,
        "cumulative_ok": cumulative <= CUMULATIVE_BUDGET_BYTES,
        "note": ("Estimate uses the four equivalent full-filter dumps from the "
                 "Temporal main run; it does not read the 88 GiB complete dump."),
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    project, _setup = check_project()
    prior = check_prior_camera_validation()
    budget = estimate_budget()
    selected_filters = filters()
    filter_string = "|".join(f"{p}@{r}" for p, r in selected_filters)
    result = {
        "schema": "arda-motion-camera-capture-plan-validation-v1",
        "workspace": str(WORKSPACE),
        "project": project,
        "prior_camera_validation": prior,
        "budget": budget,
        "filters": {
            "pair_count": len(selected_filters),
            "filter_string_chars": len(filter_string),
            "under_windows_command_limit": len(filter_string) < 30000,
            "selected_resource_names": len(all_resources()),
        },
        "planned_targets": {
            "baseline_lux": 80000.0,
            "exposure_ev100": 11.0,
            "camera_start_cm": [-850.0, 0.0, 90.0],
            "camera_target_cm": CAMERA_TARGET_CM,
            "camera_rotation": CAMERA_ROTATION,
            "camera_hfov_deg": CAMERA_HFOV,
            "required_updates": [1, 64],
            "optional_updates": [8],
        },
        "ok": bool(project["ok"] and prior.get("ok") and budget.get("ok")),
    }
    result["result"] = "pass" if result["ok"] else "fail"
    if not result["ok"]:
        reasons = list(project["errors"])
        if not prior.get("ok"):
            reasons.append("prior -150 cm rendered-view validation is not available")
        if not budget.get("ok"):
            reasons.append("projected full-filter capture exceeds the 10 GiB / 35 GiB budget")
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
        "cumulative_gib": budget.get("cumulative_gib"),
        "cumulative_ok": budget.get("cumulative_ok"),
        "validation": str(path),
    }, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
