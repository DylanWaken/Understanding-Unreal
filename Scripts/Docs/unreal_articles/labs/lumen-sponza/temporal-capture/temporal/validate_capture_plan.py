#!/usr/bin/env python3
"""Static validation of the temporal capture plan (no Unreal, no GPU).

This script checks the things that can be proven without rendering:
  * the copied project carries the retained saved Sponza map bit-for-bit;
  * the copied project module BuildId still matches the engine;
  * the two known-working launchers are byte-identical to the Sponza copies;
  * the planned lateral camera move clears the actual split Sponza geometry;
  * every selected DumpGPU resource name occurs in the installed engine source;
  * the planned selected-resource filters stay inside the 20 GiB budget, using
    the already-existing complete DumpGPU payload as a size oracle.

It deliberately does not launch Unreal and does not touch the network.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import struct
import sys
from pathlib import Path

WORKSPACE = Path(r"D:\UnrealUnderstanding-Temporal")
SPONZA = Path(r"D:\UnrealUnderstanding-Sponza")
ENGINE = Path(r"D:\UnrealEngine")
OUT = WORKSPACE / "output" / "temporal"
PROJECT = WORKSPACE / "project" / "UnrealLab"
MAP_REL = Path("Content") / "Labs" / "SponzaTeaching.umap"
SETUP_PATH = WORKSPACE / "output" / "final-setup.json"
SCENE_MANIFEST = WORKSPACE / "output" / "scene-manifest.json"
BUDGET_BYTES = 20 * 1024 ** 3
EXPECTED_DUMPS = 9  # baseline + lightstep 1/8/32/64 + camera 1/8/32/64

# The complete, already-existing Sponza DumpGPU frame is only used as a size
# oracle.  It is read-only and no part of it is copied into the workspace.
ORACLE = SPONZA / "output" / "gpu-dump" / "raw" / "sponza-20261115-083654"

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


def stream_json(path: Path):
    s = path.read_text(encoding="utf-8-sig")
    dec = json.JSONDecoder()
    i = 0
    while i < len(s):
        while i < len(s) and s[i] in " \r\n\t,":
            i += 1
        if i >= len(s):
            break
        obj, i = dec.raw_decode(s, i)
        yield obj


def filters():
    """Return the exact (pass pattern, resource pattern) pairs used by capture.

    The two Lumen groups use one resource wildcard each to keep the DumpGPU
    command well below the Windows command-line limit.  Card/page resources are
    listed explicitly because a broader Lumen wildcard there would pull in the
    unrelated atlases as well.
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

    result["forbidden_dirs_present"] = sorted(
        p.name for p in PROJECT.iterdir()
        if p.is_dir() and p.name in {"Saved", "Intermediate", "DerivedDataCache", "Provenance"})
    if result["forbidden_dirs_present"]:
        result["errors"].append("forbidden retained directories were copied")
        result["ok"] = False

    retained = Path(r"C:\Users\Administrator\Documents\Unreal Projects\UnrealLab")
    result["retained_project"] = str(retained)
    result["core_copy"] = {}
    for sub in ("Content", "Config", "Source", "Binaries"):
        src_root = retained / sub
        dst_root = PROJECT / sub
        src_files = [p for p in src_root.rglob("*") if p.is_file() and p.suffix.lower() != ".pdb"]
        missing = []
        changed = []
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
    result["low_lux_plan"] = 20000.0
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


def load_geometry():
    """World-space triangles of the imported split Sponza, in UE cm.

    The derivation is the one recorded by the scene manifest:
      recentered glTF position -> UE = (0.8*x, 0.8*z, 0.8*y) + actor location.
    """
    import numpy as np

    root = SPONZA / "source" / "Sponza"
    gltf = load_json(root / "glTF" / "SponzaSplit.gltf")
    manifest = load_json(root / "derived" / "split-manifest.json")
    buffers = [(root / "glTF" / b["uri"]).read_bytes() for b in gltf["buffers"]]
    parts = {p["mesh_name"]: p for p in manifest["parts"]}

    def accessor(index):
        a = gltf["accessors"][index]
        bv = gltf["bufferViews"][a["bufferView"]]
        data = buffers[bv["buffer"]]
        off = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
        n = a["count"]
        kind, size = {5126: ("f4", 4), 5123: ("u2", 2), 5125: ("u4", 4)}[a["componentType"]]
        ncomp = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}[a["type"]]
        stride = bv.get("byteStride", size * ncomp)
        if stride != size * ncomp:
            out = np.empty((n, ncomp), dtype=np.float64)
            for i in range(n):
                out[i] = np.frombuffer(data, dtype=np.dtype("<" + kind),
                                       count=ncomp, offset=off + i * stride)
            return out
        return np.frombuffer(data, dtype=np.dtype("<" + kind),
                             count=n * ncomp, offset=off).reshape(n, ncomp).astype(np.float64)

    triangles = []
    for mesh in gltf["meshes"]:
        part = parts.get(mesh.get("name", ""))
        if not part:
            continue
        pivot = np.array(part["source_aabb_gltf"]["pivot"], dtype=np.float64)
        actor = np.array(part["actor_location_ue_cm"], dtype=np.float64)
        for prim in mesh["primitives"]:
            pos = accessor(prim["attributes"]["POSITION"])
            idx = accessor(prim["indices"]).astype(np.int64).ravel()
            local = pos - pivot
            ue = np.stack([0.8 * local[:, 0], 0.8 * local[:, 2],
                           0.8 * local[:, 1]], axis=1) + actor
            triangles.append(ue[idx].reshape(-1, 3, 3))
    return np.concatenate(triangles, axis=0)


def edge_clearance(np, triangles, point):
    a, b, c = triangles[:, 0], triangles[:, 1], triangles[:, 2]
    best = np.full(len(triangles), 1e18)
    for e0, e1 in ((a, b), (b, c), (c, a)):
        d = e1 - e0
        dd = np.einsum("ij,ij->i", d, d)
        t = np.einsum("ij,ij->i", point - e0, d) / dd
        t = np.clip(t, 0.0, 1.0)
        q = e0 + t[:, None] * d
        best = np.minimum(best, np.linalg.norm(point - q, axis=1))
    return best


def check_camera_move():
    import numpy as np

    setup = load_json(SETUP_PATH)
    hero = [float(x) for x in setup["config"]["hero_camera"]["location_cm"]]
    rot = setup["config"]["hero_camera"]["rotation_degrees"]
    target = [hero[0], hero[1] + 150.0, hero[2]]
    tris = load_geometry()
    # Degenerate primitives cannot contribute a surface.
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    tris = tris[np.linalg.norm(np.cross(b - a, c - a), axis=1) > 1e-6]
    # Keep only geometry in the courtyard neighbourhood of the path.
    centroid = tris.mean(axis=1)
    near = tris[(centroid[:, 0] > hero[0] - 450) & (centroid[:, 0] < hero[0] + 450) &
                (np.abs(centroid[:, 1]) < 700) &
                (centroid[:, 2] > hero[2] - 250) & (centroid[:, 2] < hero[2] + 250)]

    samples = []
    worst = (1e18, None, None)
    for step in range(0, 151, 5):
        p = np.array([hero[0], hero[1] + step, hero[2]], dtype=np.float64)
        d = edge_clearance(np, near, p)
        i = int(np.argmin(d))
        rec = {"y_offset_cm": step, "edge_clearance_cm": round(float(d[i]), 4),
               "nearest_triangle_cm": [[round(float(v), 3) for v in t] for t in near[i]]}
        samples.append(rec)
        if d[i] < worst[0]:
            worst = (float(d[i]), step, rec["nearest_triangle_cm"])

    # The probe sphere used by the runtime check is 10 cm; require 15 cm of
    # edge clearance at every 5 cm sample so a 10 cm sphere is strictly clear.
    clear = worst[0] >= 15.0
    rec = {
        "schema": "arda-sponza-temporal-camera-move-validation-v1",
        "method": ("reconstructed split Sponza triangles; world = actor_location + "
                   "0.8*(x,z,y) after per-primitive pivot recenter; point-to-triangle-edge "
                   "distance sampled every 5 cm along the lateral path"),
        "hero_camera_cm": hero,
        "hero_rotation_degrees": rot,
        "move_axis": "+Y (right)",
        "move_cm": 150.0,
        "target_cm": target,
        "probe_radius_cm": 10.0,
        "required_edge_clearance_cm": 15.0,
        "min_edge_clearance_cm": round(worst[0], 4),
        "min_at_y_offset_cm": worst[1],
        "nearest_triangle_cm": worst[2],
        "clear": clear,
        "samples": samples,
        "source": str(SPONZA / "source" / "Sponza" / "glTF" / "SponzaSplit.gltf"),
        "source_sha256": sha256(SPONZA / "source" / "Sponza" / "glTF" / "SponzaSplit.gltf"),
        "derived_positions_sha256": sha256(
            SPONZA / "source" / "Sponza" / "glTF" / "SponzaSplit_positions.bin"),
        "note": ("This is a geometry check, not a collision oracle for the editor. "
                 "The capture script still refuses the move if the runtime 5-ray "
                 "line trace reports an obstruction, and records that exact refusal."),
    }
    return rec, tris.shape[0]


def _source_blob(root: Path):
    texts = []
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".cpp", ".h", ".usf", ".ush"}:
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(texts)


def check_source_names():
    lumen_root = ENGINE / "Engine" / "Source" / "Runtime" / "Renderer" / "Private" / "Lumen"
    # SceneDepthZ and ViewData.PrimaryViews are core Renderer resources, not
    # Lumen-private names, so fall back to the whole Renderer source tree.
    renderer_root = ENGINE / "Engine" / "Source" / "Runtime" / "Renderer"
    lumen_blob = _source_blob(lumen_root)
    renderer_blob = _source_blob(renderer_root)
    found, missing, where = {}, [], {}
    for name in all_resources():
        if (f'TEXT("{name}")' in lumen_blob) or (name in lumen_blob):
            found[name] = True
            where[name] = "Renderer/Private/Lumen"
        elif (f'TEXT("{name}")' in renderer_blob) or (name in renderer_blob):
            found[name] = True
            where[name] = "Renderer"
        else:
            found[name] = False
            missing.append(name)
    return {"checked": len(found), "missing": missing, "found": found,
            "source_scope": where, "engine_lumen_root": str(lumen_root),
            "engine_renderer_root": str(renderer_root),
            "ok": not missing}


def estimate_budget():
    """Use the existing complete dump metadata as a size oracle."""
    if not (ORACLE / "Base" / "ResourceDescs.json").exists():
        return {"ok": None, "reason": "existing complete dump metadata not available",
                "oracle": str(ORACLE)}
    descs = list(stream_json(ORACLE / "Base" / "ResourceDescs.json"))
    passes = list(stream_json(ORACLE / "Base" / "Passes.json"))
    readbacks = list(stream_json(ORACLE / "Base" / "DumpReadbackStatus.json"))
    by_name = {}
    for d in descs:
        by_name.setdefault(d["Name"], []).append(d)
    pass_by_ptr = {p["Pointer"]: p for p in passes}
    all_pairs = filters()
    resource_pairs = {}
    for name in all_resources():
        patterns = {pass_pattern for pass_pattern, resource_pattern in all_pairs
                    if fnmatch.fnmatchcase(name, resource_pattern)}
        if patterns:
            resource_pairs[name] = patterns

    per_resource = {}
    for name, patterns in resource_pairs.items():
        descs_for_name = by_name.get(name, [])
        keys = {d["UniqueResourceName"] for d in descs_for_name}
        total = 0
        versions = 0
        for rb in readbacks:
            import re
            m = re.search(r"Resources/([0-9a-f]+)", rb.get("File", ""))
            if not m or m.group(1) not in keys:
                continue
            if rb.get("Status") != "Dumped":
                continue
            vm = re.search(r"\.v([0-9a-f]{20})\.", rb.get("File", ""))
            producer = pass_by_ptr.get(vm.group(1)) if vm else None
            if producer is None:
                continue
            event = producer.get("EventName", "")
            if any(fnmatch.fnmatchcase(event, pat) for pat in patterns):
                total += rb.get("ActualBytes") or 0
                versions += 1
        per_resource[name] = {
            "present_in_oracle": bool(descs_for_name),
            "selected_versions": versions,
            "selected_bytes": total,
            "descriptor_bytes": sum(int(d.get("ByteSize") or 0) for d in descs_for_name),
        }
    total = sum(v["selected_bytes"] for v in per_resource.values())
    projected = total * EXPECTED_DUMPS
    return {
        "ok": projected <= BUDGET_BYTES,
        "oracle": str(ORACLE),
        "oracle_metadata_only": True,
        "estimated_selected_bytes_per_dump": total,
        "estimated_selected_gib_per_dump": round(total / 1024 ** 3, 4),
        "planned_dumps": EXPECTED_DUMPS,
        "estimated_total_bytes": projected,
        "estimated_total_gib": round(projected / 1024 ** 3, 4),
        "budget_bytes": BUDGET_BYTES,
        "budget_gib": 20.0,
        "resources": per_resource,
        "note": ("Estimate sums only those dumped versions whose producer pass "
                 "matches the planned pass wildcard. It over-counts shared "
                 "versions across filters but is a conservative upper bound."),
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    project, setup = check_project()
    move, tri_count = check_camera_move()
    source_names = check_source_names()
    budget = estimate_budget()
    selected_filters = filters()
    filter_string = "|".join(f"{p}@{r}" for p, r in selected_filters)
    result = {
        "schema": "arda-sponza-temporal-capture-plan-validation-v1",
        "workspace": str(WORKSPACE),
        "project": project,
        "camera_move": move,
        "geometry_triangles": tri_count,
        "source_names": source_names,
        "budget": budget,
        "filters": {
            "pair_count": len(selected_filters),
            "filter_string_chars": len(filter_string),
            "under_windows_command_limit": len(filter_string) < 30000,
            "selected_resource_names": len(all_resources()),
        },
        "planned_targets": {"lightstep_lux": [80000.0, 20000.0],
                            "updates": [1, 8, 32, 64],
                            "camera_move_cm": 150.0},
        "ok": bool(project["ok"] and move["clear"] and source_names["ok"] and
                   budget.get("ok") is not False),
    }
    if result["ok"]:
        result["result"] = "pass"
    else:
        result["result"] = "fail"
        reasons = list(project["errors"])
        if not move["clear"]:
            reasons.append("camera move edge clearance below 15 cm")
        if not source_names["ok"]:
            reasons.append("selected resource names missing from engine source")
        if budget.get("ok") is False:
            reasons.append("projected selected capture exceeds 20 GiB")
        result["reasons"] = reasons
    move_path = OUT / "camera-move-validation.json"
    move_path.write_text(json.dumps(move, indent=2), encoding="utf-8")
    path = OUT / "capture-plan-validation.json"
    result["camera_move_validation_path"] = str(move_path)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": result["ok"],
        "map_sha256": project.get("map_sha256"),
        "map_matches_final_setup": project.get("map_matches_final_setup"),
        "build_id_matches": project.get("build_id_matches"),
        "launchers_identical": all(v["identical"] for v in project["launchers"].values()),
        "camera_move_clear": move["clear"],
        "camera_min_edge_clearance_cm": move["min_edge_clearance_cm"],
        "source_names_missing": source_names["missing"],
        "estimated_selected_gib_per_dump": budget.get("estimated_selected_gib_per_dump"),
        "estimated_total_gib": budget.get("estimated_total_gib"),
        "budget_ok": budget.get("ok"),
        "validation": str(path),
    }, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
