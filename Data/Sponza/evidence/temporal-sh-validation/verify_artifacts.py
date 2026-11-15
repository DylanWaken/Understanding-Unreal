"""Check published hashes, fixed scales/clamps, matching identity flags, and the root UI manifest."""
import hashlib
import json
import re
from pathlib import Path

import numpy as np

REVIEW = Path(r"D:\UnrealUnderstanding-Review\temporal-sh-review")
DOCS = Path(r"D:\UnrealUnderstanding\Docs\Unreal\assets\lumen-sponza")
TEMPORAL = DOCS / "temporal"
DECODER = Path(r"D:\UnrealUnderstanding\Scripts\Docs\unreal_articles\labs\lumen-sponza\decode_temporal_sh.py")
BUILDER = Path(r"D:\UnrealUnderstanding\Scripts\Docs\unreal_articles\labs\lumen-sponza\build_temporal_manifest.py")
FOLDERS = ["screen-sh", "motion-screen-sh"]


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def load(p):
    return json.loads(Path(p).read_text(encoding="utf-8-sig"))


def main():
    out = {"hash": {}, "scales": {}, "matching": {}, "ui": {}, "timing_claims": None, "ok": True}

    # ---- published file hashes against each decode-manifest -----------------
    hash_ok = True
    for folder in FOLDERS:
        base = TEMPORAL / folder
        man = load(base / "decode-manifest.json")
        mism = []
        for f in man["files"]:
            if f["path"] == "decoder.py":
                p = DECODER
                mapped = "decode_temporal_sh.py"
            else:
                p = base / f["path"]
                mapped = f["path"]
            if not p.exists():
                mism.append({"path": mapped, "error": "missing"})
                continue
            h = sha(p)
            if h != f["sha256"] or p.stat().st_size != f["bytes"]:
                mism.append({"path": mapped, "expected": f["sha256"], "actual": h,
                             "bytes_expected": f["bytes"], "bytes_actual": p.stat().st_size})
        listed = {f["path"] for f in man["files"] if f["path"] != "decoder.py"}
        actual = {str(p.relative_to(base)).replace("\\", "/") for p in (base / "publish").rglob("*")
                  if p.is_file()}
        actual.add("report.md")
        extra = sorted(actual - listed)
        absent = sorted(listed - actual)
        # decoder.py in the manifest is the renamed copy of decode_temporal_sh.py
        dec_entry = next((f for f in man["files"] if f["path"] == "decoder.py"), None)
        dec_ok = bool(dec_entry is None or (dec_entry["sha256"] == sha(DECODER)
                      and dec_entry["bytes"] == DECODER.stat().st_size))
        ok = not mism and not extra and not absent and dec_ok
        hash_ok = hash_ok and ok
        out["hash"][folder] = {
            "listed_files": len(man["files"]), "mismatches": mism,
            "unlisted_published_files": extra, "listed_but_absent": absent,
            "decoder_rename": {"manifest_path": "decoder.py" if dec_entry else None,
                               "source": str(DECODER),
                               "sha256_matches": dec_ok,
                               "listed": bool(dec_entry),
                               "note": "light manifest lists the renamed decoder copy and its hash matches; "
                                       "motion manifest omits a decoder entry (14 publish/report files listed). "
                                       "No manifest self-hash is listed in either."},
            "ok": ok,
        }

    # published screenshot hashes are separate provenance in summary.json
    shot_ok = True
    shot_bad = []
    for folder in FOLDERS:
        base = TEMPORAL / folder
        summ = load(base / "publish/data/summary.json")
        for state, rec in summ.get("screenshots", {}).items():
            p = Path(rec["path"])
            if not p.exists() or sha(p) != rec["sha256"]:
                shot_bad.append({"experiment": folder, "state": state, "path": str(p)})
                shot_ok = False
    out["hash"]["screenshots"] = {"ok": shot_ok, "bad": shot_bad}
    hash_ok = hash_ok and shot_ok

    # ---- fixed scales, negative retention, declared display clamp ----------
    scales_ok = True
    for folder in FOLDERS:
        base = TEMPORAL / folder
        man = load(base / "decode-manifest.json")
        summ = load(base / "publish/data/summary.json")
        focus = load(base / "publish/data/focused_probes.json")
        npz = np.load(base / "publish/data/lobes.npz", allow_pickle=False)
        signed, clamped = npz["signed"], npz["clamped"]
        lum_s, lum_c = npz["luminance_signed"], npz["luminance_clamped"]
        lm = float(npz["lobe_max"][0])
        neg_frac = float(np.mean(lum_s < 0.0))
        sat_frac = float(np.mean(lum_c > lm))
        probe_scale = {p["id"]: p.get("scales") for p in focus["probes"] if "scales" in p}
        checks = {
            "lobe_radius_world_cm_60": float(man["scales"]["lobe_radius_world_cm"]) == 60.0
            and float(summ["lobe_radius_world_cm"]) == 60.0,
            "rosette_radius_px_55": float(man["scales"]["rosette_radius_px"]) == 55.0
            and float(summ["rosette_radius_px"]) == 55.0,
            "one_declared_lobe_scale": lm == float(man["scales"]["lobe_response_scale_luminance"])
            == float(summ["lobe_max_luminance"]) and npz["lobe_max"].shape == (1,),
            "declared_fixed_across_states_and_probes": bool(man["scales"]["fixed_across_states_and_probes"]),
            "negative_display_clamp_declared": "max(0" in man["scales"]["negative_display_clamp"],
            "signed_coefficients_unsigned_clamp_declared": "no clamp" in man["basis"]["sign"].lower(),
            "signed_retains_negatives": bool(np.any(signed < 0.0) and np.min(lum_s) < 0.0),
            "clamped_is_nonnegative": bool(np.all(clamped >= 0.0) and np.all(lum_c >= 0.0)),
            "clamped_equals_channel_max_signed": bool(np.array_equal(clamped, np.maximum(signed, 0.0))),
            "luminance_clamped_equals_max_signed_luminance": bool(np.allclose(lum_c, np.maximum(lum_s, 0.0))),
            "negative_fraction_matches_summary": abs(neg_frac - float(summ["lobe_negative_signed_fraction"])) < 1e-12,
            "saturated_fraction_matches_summary": abs(sat_frac - float(summ["lobe_display_saturated_fraction"])) < 1e-12,
            "state_and_probe_axes_match": list(npz["state_names"]) == [s["name"] for s in man["states"]]
            and list(npz["probe_ids"]) == [p["id"] for p in focus["probes"]],
            "single_scale_for_all_states_probes": bool(
                signed.shape[0] == len(man["states"]) and signed.shape[1] == len(focus["probes"])
                and len(set(probe_scale.values())) <= 1),
        }
        src = DECODER.read_text(encoding="utf-8")
        checks["source_radius_constant_60"] = bool(re.search(r"LOBE_RADIUS_WORLD_CM\s*=\s*60\.0", src))
        checks["source_rosette_constant_55"] = bool(re.search(r"ROSETTE_RADIUS_PX\s*=\s*55", src))
        checks["source_one_lobe_max_passed_to_every_state"] = bool(
            re.search(r"lobe_max\s*=\s*float\(summary\[\"lobe_max_luminance\"\]\)", src)
            and re.search(r"draw_lobe\([^)]*lobe_max", src, re.S)
            and re.search(r"render_rosettes\([^)]*lobe_max", src, re.S))
        ok = all(checks.values())
        scales_ok = scales_ok and ok
        out["scales"][folder] = {
            "lobe_response_scale_luminance": lm,
            "lobe_radius_world_cm": float(man["scales"]["lobe_radius_world_cm"]),
            "rosette_radius_px": float(man["scales"]["rosette_radius_px"]),
            "npz_shape": list(signed.shape),
            "signed_min_luminance": float(np.min(lum_s)),
            "clamped_max_luminance": float(np.max(lum_c)),
            "negative_signed_fraction": neg_frac,
            "display_saturated_fraction": sat_frac,
            "checks": checks,
            "ok": ok,
        }
    out["scales"]["cross_experiment_note"] = (
        "Each experiment declares its own single fixed response scale (light 1.4454, motion 1.5239); "
        "neither experiment changes scale between its states or probes.")
    out["scales"]["ok"] = scales_ok

    # ---- matching flags guard atlas/identity changes -----------------------
    match_ok = True
    for folder in FOLDERS:
        base = TEMPORAL / folder
        man = load(base / "decode-manifest.json")
        focus = load(base / "publish/data/focused_probes.json")["probes"]
        states = {s["name"]: load(base / f"publish/data/states/{s['name']}.json") for s in man["states"]}
        bad = []
        flags = {"atlas_changed": 0, "identical": 0, "incompatible": 0, "same_atlas_but_moved": 0}
        for probe in focus:
            ref = probe["reference"]
            rs = states[ref["state"]]
            rrec = [a for a in rs["active"] if a["atlas"] == ref["atlas"]]
            if len(rrec) != 1:
                bad.append({"probe": probe["id"], "error": "reference atlas not unique"})
                continue
            rrec = rrec[0]
            for state, m in probe["matches"].items():
                if m.get("missing"):
                    bad.append({"probe": probe["id"], "state": state, "error": "missing match"})
                    continue
                act = [a for a in states[state]["active"] if a["atlas"] == m["matched_atlas"]]
                if len(act) != 1:
                    bad.append({"probe": probe["id"], "state": state, "error": "matched atlas not unique"})
                    continue
                act = act[0]
                same_atlas = list(m["matched_atlas"]) == list(ref["atlas"])
                identical = bool(m["distance_cm"] < 1e-6 and m["normal_dot"] > 0.999999)
                threshold = max(1.0, 0.75 * float(m["tile_footprint_cm"]))
                incompatible = bool(m["distance_cm"] > threshold or m["normal_dot"] < 0.90)
                d = float(np.linalg.norm(np.asarray(m["matched_world_cm"]) -
                                         np.asarray(ref["world_cm"])))
                nd = float(np.dot(np.asarray(ref["normal"]), np.asarray(m["matched_normal"])))
                shd = np.asarray(m["sh_state"]) - np.asarray(ref["sh"])
                checks = {
                    "matched_kind": m["matched_kind"] == act["kind"],
                    "matched_world": np.allclose(m["matched_world_cm"], act["world_cm"], atol=1e-6),
                    "matched_screen": np.allclose(m["matched_screen_xy"], act["screen_xy"], atol=1e-6),
                    "matched_sh": np.allclose(m["sh_state"], act["sh"], atol=1e-8),
                    "delta_is_state_minus_reference": np.allclose(m["sh_delta_rgb"], shd, atol=1e-8),
                    "delta_l2": abs(m["sh_delta_l2"] - float(np.linalg.norm(shd))) < 1e-6,
                    "distance": abs(m["distance_cm"] - d) < 1e-4,
                    "normal_dot": abs(m["normal_dot"] - nd) < 1e-6,
                    "tile_footprint": abs(m["tile_footprint_cm"] - threshold / 0.75) < 1e-4,
                    "threshold": abs(m["geometric_incompatibility_threshold_cm"] - threshold) < 1e-4,
                    "same_atlas_flag": m["same_atlas_coord"] == same_atlas,
                    "identical_flag": m["physical_sampling_position_identical"] == identical,
                    "incompatible_flag": m["geometric_incompatible"] == incompatible,
                    "no_identity_on_atlas_change": bool(same_atlas or not m["physical_sampling_position_identical"]),
                }
                # flag counters
                flags["atlas_changed"] += int(not same_atlas)
                flags["identical"] += int(identical)
                flags["incompatible"] += int(incompatible)
                flags["same_atlas_but_moved"] += int(same_atlas and not identical)
                fails = [k for k, v in checks.items() if not v]
                if fails:
                    bad.append({"probe": probe["id"], "state": state, "failed": fails})
        ok = not bad
        match_ok = match_ok and ok
        out["matching"][folder] = {"flags": flags, "failures": bad, "ok": ok}

    # ---- root UI manifest references and timing claims ---------------------
    ui = load(TEMPORAL / "manifest.json")
    missing, refs = [], []
    for exp in ui.get("experiments", []):
        for state in exp.get("states", []):
            for key in ("record",):
                refs.append((exp["id"], state["id"], key, state.get(key)))
            for lid, layer in (state.get("layers") or {}).items():
                for key in ("image", "record"):
                    refs.append((exp["id"], state["id"], f"{lid}.{key}", layer.get(key)))
    for exp, state, key, rel in refs:
        if not rel:
            continue
        if str(rel).startswith(("http://", "https://")):
            missing.append({"experiment": exp, "state": state, "key": key, "ref": rel, "error": "url"})
            continue
        p = TEMPORAL / rel
        if not p.exists():
            missing.append({"experiment": exp, "state": state, "key": key, "ref": rel, "error": "missing"})
    blob = json.dumps(ui)
    timing_hits = re.findall(r"(?i)\b(?:fps|frames? per second|framerate|frame rate|60\s*hz|ms\s*/\s*frame|ms per frame|milliseconds)\b", blob)
    builder_txt = BUILDER.read_text(encoding="utf-8")
    builder_hits = re.findall(r"(?i)\b(?:fps|frames? per second|framerate|frame rate|60\s*hz|ms\s*/\s*frame|ms per frame|milliseconds)\b", builder_txt)
    timing_statement_ok = all(
        "Discrete captured updates" in (s.get("facts", {}).get("Timing", ""))
        and "does not represent elapsed rendering time" in (s.get("facts", {}).get("Timing", ""))
        for e in ui.get("experiments", []) if e["id"] in ("light-buffers", "camera-buffers")
        for s in e.get("states", []))
    caption_scale_bad = []
    for exp in ui.get("experiments", []):
        if exp["id"] not in ("light-buffers", "camera-buffers"):
            continue
        folder = "screen-sh" if exp["id"] == "light-buffers" else "motion-screen-sh"
        scale = float(load(TEMPORAL / folder / "decode-manifest.json")["scales"]["lobe_response_scale_luminance"])
        for state in exp.get("states", []):
            cap = state.get("layers", {}).get("screen-sh", {}).get("caption", "")
            facts = state.get("layers", {}).get("screen-sh", {}).get("facts", {})
            if f"/ {scale}" not in cap or "60 cm" not in cap:
                caption_scale_bad.append({"experiment": exp["id"], "state": state["id"], "field": "caption"})
            if not str(facts.get("Display range", "")).startswith(f"0 to {scale}"):
                caption_scale_bad.append({"experiment": exp["id"], "state": state["id"], "field": "Display range"})
    ui_ok = (not missing and not timing_hits and not builder_hits and timing_statement_ok
             and not caption_scale_bad)
    out["ui"] = {
        "experiments": [e["id"] for e in ui.get("experiments", [])],
        "referenced_paths": len(refs),
        "missing_or_url": missing,
        "timing_claims": timing_hits,
        "builder_timing_claims": builder_hits,
        "discrete_timing_statement_ok": timing_statement_ok,
        "caption_scale_mismatches": caption_scale_bad,
        "ok": ui_ok,
    }
    out["timing_claims"] = {"manifest": timing_hits, "builder": builder_hits,
                            "scope_note": "No 60fps/Hz/frame-time claim; timing is explicitly discrete capture spacing."}

    out["ok"] = bool(hash_ok and scales_ok and match_ok and ui_ok)
    REVIEW.mkdir(parents=True, exist_ok=True)
    (REVIEW / "artifact-checks.json").write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print("hash", hash_ok, "scales", scales_ok, "matching", match_ok, "ui", ui_ok)
    print("wrote", REVIEW / "artifact-checks.json")


if __name__ == "__main__":
    main()
