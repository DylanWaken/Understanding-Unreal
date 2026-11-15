#!/usr/bin/env python3
"""Validate the integrated temporal presentation manifest against its sources.

Checks, without rewriting any artifact:
  * every manifest path (image / record / download) resolves and images decode;
  * image dimensions are stable within a layer/experiment;
  * all six layer families exist, are offered by an experiment, and are wired
    into an article host with the manifest path;
  * no *_TEMPORAL_EVIDENCE placeholder remains;
  * every displayed per-state count / frame / exposure equals the numeric record;
  * chart bins sum to the active probe count, scale is fixed 1000, and the
    histogram is identical to the source; the chart's age unit is compared with
    the source field definition.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from PIL import Image

REPO = Path(r"D:/UnrealUnderstanding")
DOCS = REPO / "Docs/Unreal"
CHAPTERS = REPO / "Scripts/Docs/unreal_articles/chapters"
MANIFEST_PATH = DOCS / "assets/lumen-sponza/temporal/manifest.json"
TEMPORAL = MANIFEST_PATH.parent
RADIOSITY_CAPTURE = Path(r"D:/UnrealUnderstanding-Radiosity/output/capture-manifest.json")

LAYER_IDS = {"renderer", "screen-probes", "screen-sh", "cards", "world-cache", "radiosity"}
failures: list[str] = []
checks: list[dict] = []


def ok(name, detail, **kw):
    checks.append({"name": name, "ok": True, "detail": detail, **kw})


def fail(name, detail, **kw):
    checks.append({"name": name, "ok": False, "detail": detail, **kw})
    failures.append(f"{name}: {detail}")


def assert_eq(name, got, want):
    if got == want:
        ok(name, f"{got!r} == expected {want!r}")
    else:
        fail(name, f"{got!r} != expected {want!r}", got=got, expected=want)


def assert_true(name, cond, detail):
    if cond:
        ok(name, detail)
    else:
        fail(name, detail)


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def fnum(text):
    return float(str(text).replace(",", "").split("/")[0].strip())


def join3(values):
    return ", ".join(str(x) for x in values)


manifest = load(MANIFEST_PATH)

# ---- layer families -------------------------------------------------------
layer_ids = [layer["id"] for layer in manifest["layers"]]
assert_eq("manifest.layer_ids", sorted(layer_ids), sorted(LAYER_IDS))
assert_eq("manifest.layer_unique", len(set(layer_ids)), len(layer_ids))
for layer in manifest["layers"]:
    assert_true(f"manifest.layer_legend.{layer['id']}", bool(layer.get("legend", "").strip()),
                "legend present")

offered = {}
for experiment in manifest["experiments"]:
    for state in experiment["states"]:
        for lid in state["layers"]:
            offered.setdefault(lid, 0)
            offered[lid] += 1
for lid in sorted(LAYER_IDS):
    assert_true(f"manifest.layer_offered.{lid}", offered.get(lid, 0) > 0,
                f"{offered.get(lid,0)} state/layer occurrences")

# ---- article hosts --------------------------------------------------------
host_re = re.compile(r'<div class="scene-observations"[^>]*>')
wired = {}
for chapter in sorted(CHAPTERS.glob("*.html")):
    text = chapter.read_text(encoding="utf-8")
    for tag in host_re.findall(text):
        m_path = re.search(r'data-manifest="([^"]+)"', tag)
        m_layer = re.search(r'data-layer="([^"]+)"', tag)
        if not (m_path and m_layer):
            fail("article.host.attrs", f"{chapter.name}: host missing data-manifest/data-layer: {tag}")
            continue
        rel = m_path.group(1)
        target = (DOCS / rel).resolve()
        assert_true(f"article.host.path.{chapter.stem}.{m_layer.group(1)}",
                    target == MANIFEST_PATH.resolve(), f"{target}")
        wired.setdefault(m_layer.group(1), []).append(chapter.name)
for lid in sorted(LAYER_IDS):
    assert_true(f"article.host.layer.{lid}", len(wired.get(lid, [])) > 0,
                f"wired in {wired.get(lid, [])}")

# ---- placeholder scan -----------------------------------------------------
placeholder_hits = []
scan_roots = [DOCS, REPO / "Scripts/Docs/unreal_articles/chapters"]
for root in scan_roots:
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in {".html", ".js", ".json", ".svg", ".css", ".md", ".py"}:
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat in ("TEMPORAL_EVIDENCE", "_TEMPORAL_EVIDENCE"):
            if pat in txt:
                placeholder_hits.append(f"{p.relative_to(REPO)} contains {pat}")
assert_true("manifest.no_temporal_placeholders", not placeholder_hits,
            "no *_TEMPORAL_EVIDENCE placeholder" if not placeholder_hits else "; ".join(placeholder_hits[:10]))

# ---- path + dimension validation -----------------------------------------
image_records: dict[str, list] = {}
path_count = 0
for experiment in manifest["experiments"]:
    for state in experiment["states"]:
        state_record = state.get("record")
        if state_record:
            p = (TEMPORAL / state_record).resolve()
            path_count += 1
            assert_true(f"path.record.{experiment['id']}.{state['id']}", p.is_file(),
                        str(p.relative_to(REPO)) if p.is_file() else f"missing {p}")
        for lid, layer in state["layers"].items():
            for key in ("image", "record"):
                rel = layer.get(key)
                if not rel:
                    # A layer may omit its own record when the state record covers it.
                    if key == "record" and state_record:
                        continue
                    fail(f"path.missing_{key}.{experiment['id']}.{state['id']}.{lid}",
                         f"layer has no {key}")
                    continue
                p = (TEMPORAL / rel).resolve()
                path_count += 1
                assert_true(f"path.{key}.{experiment['id']}.{state['id']}.{lid}", p.is_file(),
                            str(p.relative_to(REPO)) if p.is_file() else f"missing {p}")
            for dl in layer.get("downloads", []):
                p = (TEMPORAL / dl["path"]).resolve()
                path_count += 1
                assert_true(f"path.download.{experiment['id']}.{state['id']}.{lid}.{dl['label']}",
                            p.is_file(),
                            str(p.relative_to(REPO)) if p.is_file() else f"missing {p}")
            rel = layer.get("image")
            if rel:
                p = (TEMPORAL / rel).resolve()
                if p.is_file():
                    try:
                        with Image.open(p) as im:
                            im.load()
                            dims = im.size
                        assert_true(f"image.decode.{experiment['id']}.{state['id']}.{lid}",
                                    dims[0] > 0 and dims[1] > 0, f"{dims[0]}x{dims[1]} {p.name}")
                        image_records.setdefault(f"{experiment['id']}/{lid}", []).append(
                            (state["id"], dims, p.name))
                    except Exception as exc:  # noqa: BLE001 - report the decode failure
                        fail(f"image.decode.{experiment['id']}.{state['id']}.{lid}", repr(exc))

for key, recs in sorted(image_records.items()):
    dims = {tuple(r[1]) for r in recs}
    assert_true(f"image.dimensions_stable.{key}", len(dims) == 1,
                f"{len(recs)} images, dimensions {sorted(dims)}")

# ---- per-experiment numeric agreement ------------------------------------
screen_summaries = {
    "light-buffers": TEMPORAL / "screen-sh/publish/data/summary.json",
    "camera-buffers": TEMPORAL / "motion-screen-sh/publish/data/summary.json",
}
placements_dir = {"light-buffers": "light", "camera-buffers": "motion"}
sun_by_experiment = {"light-buffers": "80,000 lux", "camera-buffers": "80,000 lux"}
rad_capture = {s["label"]: s for s in load(RADIOSITY_CAPTURE)["states"]}
run_temporal = load(DOCS / "assets/lumen-sponza/data/run-temporal.json")

engine_re = re.compile(r"Frame\s+(\d+)")
for experiment in manifest["experiments"]:
    eid = experiment["id"]
    if eid in screen_summaries:
        summary = load(screen_summaries[eid])
        for state in experiment["states"]:
            sid = state["id"]
            facts = state["facts"]
            probe_facts = state["layers"]["screen-probes"]["facts"]
            src = load(TEMPORAL / state["record"])
            counts, view = src["counts"], src["view"]
            frame = engine_re.search(summary["frame_scopes"][sid]).group(1)
            assert_eq(f"screen.frame.{eid}.{sid}", str(facts["Recorded engine frame"]), frame)
            assert_eq(f"screen.view_frame.{eid}.{sid}", facts["View frame / state index"],
                      f"{view['frame_number']} / {view['state_frame_index']}")
            assert_eq(f"screen.exposure.{eid}.{sid}", facts["Exposure"],
                      f"EV100 11; captured pre-exposure {view['preexposure']}")
            assert_eq(f"screen.camera.{eid}.{sid}", facts["Camera world position (cm)"],
                      join3(view["world_camera_origin_cm"]))
            assert_eq(f"screen.active.{eid}.{sid}", probe_facts["Active probes"],
                      f"{counts['active_total_count']} = {counts['active_uniform_count']} uniform"
                      f" + {counts['active_adaptive_count']} adaptive")
            assert_eq(f"screen.stale.{eid}.{sid}", str(probe_facts["Excluded stale adaptive entries"]),
                      str(counts["stale_finite_adaptive_count"]))
            assert_eq(f"screen.jitter.{eid}.{sid}", probe_facts["Uniform tile jitter (pixels)"],
                      join3(counts["screen_tile_jitter_px"]))
            expected_change = sun_by_experiment[eid]
            if sid != "baseline":
                expected_change = ("Camera translated 150 cm" if eid == "camera-buffers"
                                   else "Sun illuminance 20,000 lux")
            else:
                expected_change = "Sun illuminance 80,000 lux"
            assert_eq(f"screen.change.{eid}.{sid}", facts["Change"], expected_change)

            # cards layer facts against the matching placement record
            cards = load(TEMPORAL / f"placements/{placements_dir[eid]}/data/{sid}/cards.json")
            cc = cards["counts"]
            cl = state["layers"]["cards"]["facts"]
            assert_eq(f"cards.valid.{eid}.{sid}", cl["Valid cards"], cc["valid_cards"])
            assert_eq(f"cards.resident.{eid}.{sid}", cl["Included / resident cards"], cc["page_resident"])
            assert_eq(f"cards.lowres.{eid}.{sid}", cl["Referenced low-resolution pages"], cc["total_low_res_pages"])
            assert_eq(f"cards.highlight.{eid}.{sid}", cl["Highlighted cards"], cc["representative_subset"])
            assert_eq(f"cards.download_pages.{eid}.{sid}",
                      state["layers"]["cards"]["downloads"][0]["path"],
                      f"placements/{placements_dir[eid]}/data/{sid}/pages.json")

            # world-cache facts and chart against the matching placement record
            wc = load(TEMPORAL / f"placements/{placements_dir[eid]}/data/{sid}/worldcache.json")
            wcc = wc["counts"]
            wl = state["layers"]["world-cache"]
            wf = wl["facts"]
            assert_eq(f"wc.active.{eid}.{sid}", wf["Active world probes"], wcc["active_by_indirection"])
            assert_eq(f"wc.alloc.{eid}.{sid}", wf["Allocator high-water / free slots"],
                      f"{wcc['allocator_high_water']} / {wcc['free_count']}")
            assert_eq(f"wc.screen.{eid}.{sid}", wf["On screen / passing depth test"],
                      f"{wcc['on_screen']} / {wcc['visible']}")
            assert_true(f"wc.offset_zero.{eid}.{sid}", wc.get("probe_world_offset_zero") is True,
                        "source reports probe_world_offset_zero=True")
            src_hist = wc["activity"]["0"]["traced_age_histogram"]
            chart = wl["chart"]
            assert_eq(f"wc.chart.maximum.{eid}.{sid}", chart["maximum"], 1000)
            assert_eq(f"wc.chart.unit.{eid}.{sid}", chart["unit"], "active probes")
            got_hist = {re.sub(r"(\d)–(\d)", r"\1_\2", v["label"]).replace("+", "_plus"):
                        v["value"] for v in chart["values"]}
            # labels use en-dash for ranges; map back to source keys
            got_hist = {}
            for v in chart["values"]:
                label = v["label"].replace("+", "_plus").replace("–", "_")
                got_hist[label] = v["value"]
            assert_eq(f"wc.chart.histogram.{eid}.{sid}", got_hist, src_hist)
            assert_eq(f"wc.chart.sum.{eid}.{sid}", sum(v["value"] for v in chart["values"]),
                      wcc["active_by_indirection"])
            age_title = chart["title"]
            source_definition = ("traced_age_frames = FPackedView.FrameIndex - ProbeLastTracedFrame; "
                                 "engine FrameNumber = View.ViewState->GetFrameIndex() "
                                 "(LumenRadianceCache.cpp:1904) and StateFrameIndex is the same "
                                 "view-state frame index (SceneRendering.cpp:1926-1936)")
            if "view-state updates" in age_title:
                ok(f"wc.chart.age_semantics.{eid}.{sid}",
                   f"{age_title!r} matches source unit: {source_definition}")
            else:
                fail(f"wc.chart.age_semantics.{eid}.{sid}",
                     f"chart title {age_title!r} does not name the source unit: {source_definition}")
    elif eid == "radiosity-buffers":
        for state in experiment["states"]:
            sid = state["id"]
            facts = state["facts"]
            src = load(TEMPORAL / state["record"])
            counts, view = src["counts"], src["view"]
            cap = rad_capture[sid]
            assert_eq(f"rad.engine_frame.{sid}", facts["Recorded engine frame"], cap["engine_frame"])
            assert_eq(f"rad.view_index.{sid}", facts["View state index"], view["frame_index_field"])
            assert_eq(f"rad.camera.{sid}", facts["Camera world position (cm)"],
                      join3(view["camera_world_cm"]))
            assert_eq(f"rad.probes.{sid}", facts["Surface samples retained"], counts["probes"])
            assert_eq(f"rad.shnonzero.{sid}", facts["Nonzero SH samples"], counts["sh_nonzero"])
            assert_eq(f"rad.visible.{sid}", facts["Scene-depth visible / occluded samples"],
                      f"{counts['visible']} / {counts['occluded']}")
            assert_eq(f"rad.preexp.{sid}", facts["Cache pre-exposure"], src["preexposure"])
            # NPZ array cardinality and nonzero count, independent of the JSON
            import numpy as np
            npz = np.load(TEMPORAL / f"radiosity/publish/data/probes-{sid}.npz")
            assert_eq(f"rad.npz_probes.{sid}", int(npz["position_world_cm"].shape[0]), counts["probes"])
            assert_eq(f"rad.npz_shnonzero.{sid}", int(npz["sh_nonzero"].sum()), counts["sh_nonzero"])
            assert_eq(f"rad.npz_preexp_camera.{sid}", len(npz["page_index"]), counts["probes"])
            dl_labels = [d["label"] for d in state["layers"]["radiosity"]["downloads"]]
            assert_eq(f"rad.downloads.{sid}", dl_labels,
                      ["Complete numerical arrays (NPZ)", "X-ray placement",
                       "SH response functions", "Figure scales and interpretation"])
    elif eid == "renderer-light-step":
        frames = {fr["name"]: fr for fr in run_temporal["frames"]}
        for state in experiment["states"]:
            sid = state["id"]
            facts = state["facts"]
            img = Path(state["layers"]["renderer"]["image"]).name
            stem = Path(img).stem
            fr = frames.get(stem)
            assert_true(f"renderer.frame_present.{sid}", fr is not None, f"frame record {stem}")
            if fr is None:
                continue
            draw = int(facts["Bridge draw since light change"])
            assert_eq(f"renderer.draw.{sid}", draw, fr["actual_bridge_draw_after_sun_change"])
            assert_eq(f"renderer.engine_frame.{sid}", facts["Engine frame before / after capture"],
                      f"{fr['frame_before']} / {fr['frame_after']}")
            assert_eq(f"renderer.sun.{sid}", facts["Sun illuminance"],
                      f"{int(fr['sun']['intensity_lux']):,} lux")
            got_luma = fnum(facts["Mean display luma"])
            assert_true(f"renderer.luma.{sid}", abs(got_luma - fr["mean_display_luma"]) <= 0.0005,
                        f"{got_luma} vs {fr['mean_display_luma']}")
            assert_true(f"renderer.image.{sid}", (TEMPORAL / state["layers"]["renderer"]["image"]).is_file(),
                        "image exists")

# ---- report ---------------------------------------------------------------
result = {
    "schema": "lumen-temporal-manifest-check-v1",
    "manifest": str(MANIFEST_PATH),
    "layers": sorted(layer_ids),
    "experiments": len(manifest["experiments"]),
    "states": sum(len(e["states"]) for e in manifest["experiments"]),
    "paths_checked": path_count,
    "images": sum(len(v) for v in image_records.values()),
    "checks": checks,
    "failures": failures,
    "ok": not failures,
}
out = Path(r"D:/UnrealUnderstanding-Review/final-validation/results/temporal_manifest_checks.json")
out.write_text(json.dumps(result, indent=2), encoding="utf-8")
print(f"temporal manifest: experiments={result['experiments']} states={result['states']} "
      f"paths={path_count} images={result['images']} checks={len(checks)} failures={len(failures)}")
for f in failures:
    print("FAIL:", f)
sys.exit(0 if not failures else 1)
