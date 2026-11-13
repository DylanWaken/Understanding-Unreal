"""Deterministic verifier for the final-capture figure package.

Usage:
  python figures/verify_figures.py
  python figures/verify_figures.py --dump D:\\...\\binary-... --report D:\\...\\report

The verifier is intentionally independent of figure rendering: it checks the
manifest against the raw dump, the report manifest hashes, the selected View
bytes, exact pass/payload versions, image hashes and the SceneColor shared
transform. It prints a stable summary and exits non-zero on the first class of
failure (all failures are collected and printed).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

FIG_DIR = Path(__file__).resolve().parent
ROOT = FIG_DIR.parent
MANIFEST = FIG_DIR / "figure-manifest.json"
INV = FIG_DIR / "inventory"
PNG = FIG_DIR / "png"
SVG = FIG_DIR / "svg"
SVG_NS = "{http://www.w3.org/2000/svg}"


def load(path: Path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def sha256(path: Path, chunk=8 * 1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def iter_json_objects(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    dec = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break
        obj, end = dec.raw_decode(text, i)
        yield obj
        i = end


def main():
    global FIG_DIR, MANIFEST, INV, PNG, SVG
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", "--dump", dest="dump", default=None)
    ap.add_argument("--report", default=None)
    ap.add_argument("--output", type=Path, default=FIG_DIR, help="directory of decoded figures")
    args = ap.parse_args()
    FIG_DIR = args.output
    MANIFEST = FIG_DIR / "figure-manifest.json"
    INV = FIG_DIR / "inventory"
    PNG = FIG_DIR / "png"
    SVG = FIG_DIR / "svg"
    failures = []
    checks = []

    def check(name, cond, detail=""):
        checks.append((name, bool(cond), detail))
        if not cond:
            failures.append((name, detail))

    def decode_view(content, metadata):
        vb = dump / "Structures" / (str(content) + ".bin")
        mb = dump / "StructuresMetadata" / (str(metadata) + ".json")
        if not vb.exists() or not mb.exists():
            return None, None, None
        meta = load(mb)
        members = {m["Name"]: m for m in meta.get("Members", [])}
        b = vb.read_bytes()
        def dec(name):
            m = members.get(name)
            if not m:
                return None
            n = max(1, int(m.get("NumColumns") or 1))
            return list(struct.unpack_from("<" + "f" * n, b, int(m["Offset"])))
        return dec("ViewRectMin"), dec("ViewSizeAndInvSize"), sha256(vb)

    def page_union(path, atlas, crop):
        b = path.read_bytes()
        x0, y0, w, h = crop
        mask = bytearray(w * h)
        pages = 0
        for i in range(len(b) // 80):
            o = i * 80
            sx, sy = struct.unpack_from("<2f", b, o + 8)
            uv = struct.unpack_from("<4f", b, o + 32)
            if sx <= 0.0 or sy <= 0.0:
                continue
            px0 = int(uv[0] * atlas + 0.5); py0 = int(uv[1] * atlas + 0.5)
            px1 = int(uv[2] * atlas + 0.5); py1 = int(uv[3] * atlas + 0.5)
            if px1 <= px0 or py1 <= py0:
                continue
            pages += 1
            ix0, iy0 = max(x0, px0), max(y0, py0)
            ix1, iy1 = min(x0 + w, px1), min(y0 + h, py1)
            for yy in range(iy0, iy1):
                row = (yy - y0) * w
                for xx in range(ix0, ix1):
                    mask[row + (xx - x0)] = 1
        return pages, sum(mask), mask

    if not MANIFEST.exists():
        print("FAIL: missing figures/figure-manifest.json")
        return 2
    man = load(MANIFEST)
    dump = Path(args.dump or man["capture"]["dump"])
    if not dump.is_absolute():
        dump = (ROOT / dump).resolve()
    report = Path(args.report or man["capture"]["report"])
    if not report.is_absolute():
        report = (FIG_DIR / report).resolve()
    report_manifest = load(report / "manifest.json")
    report_files = {f["path"]: f for f in report_manifest.get("files", [])}

    check("manifest.schema", man.get("schema") == "unreal-understanding-final-figure-manifest-v2",
          str(man.get("schema")))
    check("manifest.figure_count", man.get("decoded_figure_count") == len(man.get("figures", [])),
          f"{man.get('decoded_figure_count')} vs {len(man.get('figures', []))}")
    check("manifest.figure_count_nonzero", len(man.get("figures", [])) > 0, str(len(man.get("figures", []))))

    selected = man["selection"]["selected_graph"]
    selected_view = man["selection"]["selected_view"]
    check("selection.graph_prefix", re.fullmatch(r"[0-9a-f]{4}", selected) is not None, selected)

    # 1. Re-decode the selected View bytes and prove the rect/size.
    view_bin = dump / "Structures" / (selected_view["content"] + ".bin")
    meta_bin = dump / "StructuresMetadata" / (selected_view["metadata"] + ".json")
    check("selection.view_bin_exists", view_bin.exists(), str(view_bin))
    check("selection.view_metadata_exists", meta_bin.exists(), str(meta_bin))
    if view_bin.exists() and meta_bin.exists():
        b = view_bin.read_bytes()
        check("selection.view_sha256", sha256(view_bin) == selected_view["bin_sha256"],
              selected_view["bin_sha256"])
        meta = load(meta_bin)
        members = {m["Name"]: m for m in meta.get("Members", [])}
        def dec(name):
            m = members[name]
            n = max(1, int(m.get("NumColumns") or 1))
            return list(struct.unpack_from("<" + "f" * n, b, int(m["Offset"])))
        rect = dec("ViewRectMin")
        size = dec("ViewSizeAndInvSize")
        check("selection.view_rect_min", [round(x, 6) for x in rect] == [0.0, 0.0, 0.0, 0.0], str(rect))
        check("selection.view_size_1280x720",
              [round(x, 6) for x in size[:2]] == [1280.0, 720.0], str(size[:2]))
        check("selection.view_size_matches_manifest",
              [round(x, 6) for x in size] == [round(x, 6) for x in selected_view["view_size_and_inv_size"]],
              f"{size} vs {selected_view['view_size_and_inv_size']}")

    # 2. Every figure's exact graph/payload/pass/image identity.
    required = {
        "gbuffer-a-world-normal", "gbuffer-a-perobject-2bit",
        "gbuffer-b-metallic", "gbuffer-b-specular", "gbuffer-b-roughness",
        "gbuffer-b-shading-model", "gbuffer-b-selective-mask", "gbuffer-c-basecolor",
        "scene-depth-device-z", "scene-depth-linear-view", "scene-stencil",
        "scene-color-before-indirect", "scene-color-after-indirect", "scene-color-before-after",
        "surface-cache-material-layers", "card-capture-layers", "card-capture-atlas", "surface-cache-lighting",
        "world-cache-radiance-indirection-slices",
        "reflection-trace-radiance", "reflection-trace-hit",
        "reflection-resolved-radiance", "reflection-resolved-depth",
        "screen-probe-scene-depth", "screen-probe-world-normal",
        "screen-probe-lighting-pdf", "screen-probe-trace-hit",
        "screen-probe-hit-distance", "screen-probe-radiance",
        "screen-probe-filtered-radiance", "screen-probe-diffuse-indirect",
        "screen-probe-rough-specular",
        "vsm-maskbits-nibbles", "vsm-page-table-state",
    }
    slugs = {f["slug"] for f in man["figures"]}
    check("figures.required_set", required <= slugs, "missing=" + ",".join(sorted(required - slugs)))
    check("figures.no_duplicate_slugs", len(slugs) == len(man["figures"]), str(len(slugs)))

    passed_figures = 0
    vector_figures = 0
    for f in man["figures"]:
        ok = True
        def figcheck(cond, name, detail=""):
            nonlocal ok
            if not cond:
                ok = False
                failures.append((f["slug"] + "." + name, detail))
        figcheck(f["graph"]["prefix"] == selected, "graph_prefix", f["graph"]["prefix"])
        figcheck(f["graph"]["view_content"] == selected_view["content"], "graph_view_content")
        figcheck(f["graph"]["view_bin_sha256"] == selected_view["bin_sha256"], "graph_view_sha")
        figcheck(f["source"]["resource_id"].startswith(selected), "resource_prefix", f["source"]["resource_id"])
        figcheck(bool(f["quantity"]["name"]), "quantity_name")
        figcheck(bool(f["quantity"]["units"]), "quantity_units")
        figcheck(bool(f["quantity"]["domain"]), "quantity_domain")
        figcheck(bool(f["channel_mapping"]), "channel_mapping")
        figcheck(bool(f["invalid"].get("source")), "invalid_source", str(f["invalid"].get("source")))
        figcheck(bool(f["display"].get("transform")), "display_transform")
        figcheck(f["payload_version"]["version_pointer"] is not None, "version_pointer")
        figcheck(f["pass"]["pointer"] is not None, "pass_pointer")
        figcheck(f["pass"]["id"] == f["payload_version"]["pass_id"], "pass_id_consistency",
                f"{f['pass']['id']} vs {f['payload_version']['pass_id']}")
        figcheck(f["payload_version"]["pass_event"] == f["pass"]["event"], "pass_event_consistency")
        # Payload bytes and SHA against the report manifest and the actual file.
        rel = f["payload"]["file"]
        p = dump / rel
        figcheck(p.exists(), "payload_exists", str(p))
        figcheck(rel in report_files, "payload_in_report_manifest", rel)
        if p.exists():
            figcheck(p.stat().st_size == f["payload"]["bytes"], "payload_size",
                    f"{p.stat().st_size} vs {f['payload']['bytes']}")
            figcheck(sha256(p) == f["payload"]["sha256"], "payload_sha256", rel)
        if rel in report_files:
            figcheck(report_files[rel]["sha256"] == f["payload"]["sha256"], "payload_report_sha", rel)
            figcheck(report_files[rel]["bytes"] == f["payload"]["bytes"], "payload_report_bytes", rel)
        # Image hash.
        img = FIG_DIR / f["image"]["file"]
        figcheck(img.exists(), "image_exists", str(img))
        if img.exists():
            figcheck(sha256(img) == f["image"]["sha256"], "image_sha256", str(img))
            figcheck(img.stat().st_size == f["image"]["bytes"], "image_bytes", str(img))
        # Vector record: caption text is SVG <text>, data is the embedded PNG.
        vec = f.get("vector_image")
        if vec:
            vector_figures += 1
            vfile = FIG_DIR / vec["file"]
            figcheck(str(vec["file"]).endswith(".svg"), "vector_suffix", str(vec["file"]))
            figcheck(vfile.exists(), "vector_exists", str(vfile))
            figcheck(vec.get("backing_image") == f["image"]["file"], "vector_backing",
                     f"{vec.get('backing_image')} vs {f['image']['file']}")
            figcheck(vec.get("width") == f["image"]["width"]
                     and vec.get("height") == f["image"]["height"], "vector_dimensions",
                     f"{vec.get('width')}x{vec.get('height')} vs "
                     f"{f['image']['width']}x{f['image']['height']}")
            if vfile.exists():
                figcheck(sha256(vfile) == vec["sha256"], "vector_sha256", str(vfile))
                figcheck(vfile.stat().st_size == vec["bytes"], "vector_bytes", str(vfile))
                try:
                    root = ET.parse(vfile).getroot()
                except Exception as e:  # malformed SVG is a hard failure
                    root = None
                    figcheck(False, "vector_xml", repr(e))
                if root is not None:
                    vt = vec.get("text") or {}
                    figcheck(root.tag == SVG_NS + "svg", "vector_root", root.tag)
                    figcheck(root.get("width") == str(vec["width"])
                             and root.get("height") == str(vec["height"])
                             and root.get("viewBox") == "0 0 %d %d" % (vec["width"], vec["height"]),
                             "vector_canvas",
                             f"{root.get('width')}x{root.get('height')} viewBox={root.get('viewBox')}")
                    images = root.findall(".//" + SVG_NS + "image")
                    figcheck(len(images) == 1, "vector_embedded_png_count", str(len(images)))
                    if images:
                        href = (images[0].get("href")
                                or images[0].get("{http://www.w3.org/1999/xlink}href") or "")
                        embedded_ok = False
                        if href.startswith("data:image/png;base64,"):
                            try:
                                raw = base64.b64decode(href.split(",", 1)[1], validate=True)
                                embedded_ok = (
                                    len(raw) == f["image"]["bytes"]
                                    and hashlib.sha256(raw).hexdigest() == f["image"]["sha256"])
                            except Exception:
                                embedded_ok = False
                        figcheck(embedded_ok, "vector_embedded_png",
                                 "embedded bytes/hash must equal png backing")
                    texts = [t for t in root.findall(".//" + SVG_NS + "text")
                             if (t.text or "").strip()]
                    figcheck(vt.get("is_vector") is True, "vector_text_flag", str(vt.get("is_vector")))
                    figcheck(len(texts) == int(vt.get("elements") or -1),
                             "vector_text_count", f"{len(texts)} vs {vt.get('elements')}")
                    elem_sizes = [float(t.get("font-size") or 0) for t in texts]
                    figcheck(bool(elem_sizes) and min(elem_sizes) >= 12
                             and int(vt.get("min_font_px") or 0) >= 12, "vector_min_font",
                             f"elements={min(elem_sizes) if elem_sizes else None} "
                             f"record={vt.get('min_font_px')}")
                    figcheck(bool(elem_sizes) and max(elem_sizes) >= 20
                             and int(vt.get("max_font_px") or 0) >= 20, "vector_title_font",
                             f"elements={max(elem_sizes) if elem_sizes else None} "
                             f"record={vt.get('max_font_px')}")
                    text_set = {(t.text or "").strip() for t in texts}
                    title_lines = [str(x).strip() for x in (vt.get("title_lines") or []) if str(x).strip()]
                    figcheck(bool(title_lines), "vector_title_lines", str(title_lines))
                    figcheck(all(line in text_set for line in title_lines),
                             "vector_title_is_text", str(title_lines))
        # Source pass must be the exact pass that owns the version pointer.
        if f["pass"]["pointer"] is not None:
            vp = f["payload_version"]["version_pointer"]
            figcheck(vp is None or f["pass"]["pointer"] == vp, "version_pass_pointer",
                     f"{vp} vs {f['pass']['pointer']}")
        if ok:
            passed_figures += 1
    check("figures.identity_checks", passed_figures == len(man["figures"]),
          f"{passed_figures}/{len(man['figures'])}")
    check("vectors.present_for_all", vector_figures == len(man["figures"]),
          f"{vector_figures}/{len(man['figures'])}")
    check("vectors.all_34", vector_figures == 34, str(vector_figures))

    # 2b. Exact-pass card capture proof: one pass rect AND one Target1.a mask.
    figs = {f["slug"]: f for f in man["figures"]}
    card = figs.get("card-capture-layers")
    check("card.figure_present", card is not None, "card-capture-layers")
    if card:
        cc = card.get("card_capture") or {}
        rect = cc.get("exact_rect_xywh") or []
        check("card.exact_rect_xywh", len(rect) == 4 and rect[2] > 0 and rect[3] > 0, str(rect))
        check("card.mask_rule_target1a", "Target1.a" in str(cc.get("mask_rule", "")),
              str(cc.get("mask_rule")))
        common = cc.get("common_valid_pixels")
        check("card.common_count_is_rect", bool(rect) and common == rect[2] * rect[3],
              f"{common} vs {rect[2] * rect[3] if len(rect) == 4 else '?'}")
        counts = cc.get("panel_common_counts") or {}
        check("card.panel_common_counts_equal",
              len(counts) == 4 and all(v == common for v in counts.values()), str(counts))
        check("card.no_prior_rect_overlap", cc.get("prior_rect_overlap_pixels") == 0,
              str(cc.get("prior_rect_overlap_pixels")))
        versions = cc.get("same_pass_versions") or []
        check("card.four_same_pass_versions",
              len(versions) == 4
              and all(v.get("version_pointer") == cc.get("pass_pointer")
                      and v.get("version_pass_id") == cc.get("pass_id") for v in versions),
              str([(v.get("name"), v.get("version_pass_id"), v.get("version_pointer")) for v in versions]))
        check("card.source_resources",
              {v.get("name") for v in versions} == {
                  "Lumen.CardCaptureAlbedoAtlas", "Lumen.CardCaptureNormalAtlas",
                  "Lumen.CardCaptureEmissiveAtlas", "Lumen.CardCaptureDepthStencilAtlas"},
              str(sorted(v.get("name") for v in versions)))
        vr, vs, vsha = decode_view(cc.get("view_content"), cc.get("view_metadata"))
        check("card.view_rect_decodes", vr is not None and vs is not None, str(vr))
        if vr is not None and vs is not None and len(rect) == 4:
            check("card.view_rect_matches_exact_rect",
                  [int(round(v)) for v in vr[:2]] == rect[:2]
                  and [int(round(v)) for v in vs[:2]] == rect[2:],
                  f"{vr[:2]} {vs[:2]} vs {rect}")
            check("card.view_sha256", vsha == cc.get("view_bin_sha256"), str(vsha))
        wp = (cc.get("write_proof") or {}).get("Lumen.CardCaptureAlbedoAtlas") or {}
        check("card.write_proof_inside", bool(rect) and wp.get("changed_inside_rect") == rect[2] * rect[3],
              str(wp))
        check("card.write_proof_outside", wp.get("changed_outside_rect") == 0, str(wp))
        alb = next((v for v in versions if v.get("name") == "Lumen.CardCaptureAlbedoAtlas"), None)
        if alb and rect and wp.get("previous_version_pointer"):
            cur_p = dump / alb["payload"]["file"]
            prev_p = dump / "Resources" / (
                alb["resource_id"] + ".mip0.v" + wp["previous_version_pointer"] + ".bin")
            if cur_p.exists() and prev_p.exists():
                ab, bb = cur_p.read_bytes(), prev_p.read_bytes()
                bpp = len(ab) // (512 * 512)
                inside = outside = 0
                for yy in range(512):
                    for xx in range(512):
                        i = (yy * 512 + xx) * bpp
                        if ab[i:i + bpp] != bb[i:i + bpp]:
                            if rect[0] <= xx < rect[0] + rect[2] and rect[1] <= yy < rect[1] + rect[3]:
                                inside += 1
                            else:
                                outside += 1
                check("card.write_proof_recomputed", inside == rect[2] * rect[3] and outside == 0,
                      f"inside={inside} outside={outside}")
            else:
                check("card.write_proof_recomputed", False, "previous payload missing")
    atlas = figs.get("card-capture-atlas")
    check("card.atlas_explorer_present", atlas is not None, "card-capture-atlas")
    if atlas and card:
        cca = atlas.get("card_capture_atlas") or {}
        check("card.atlas_union_matches", cca.get("union_pixels") == (card.get("card_capture") or {}).get("current_frame_union_pixels"),
              f"{cca.get('union_pixels')} vs {(card.get('card_capture') or {}).get('current_frame_union_pixels')}")
        check("card.atlas_masks_stale",
              (cca.get("raw_nonzero_alpha_outside_union") or 0) > 0
              and cca.get("stale_alpha_outside_union") == 0, str(cca))

    # 2c. Surface-cache occupancy: mapped Lumen.PageBuffer rectangles, shared panels.
    for slug, common_key in (("surface-cache-material-layers", "common_valid_pixels"),
                             ("surface-cache-lighting", "scene_common_valid_pixels")):
        f = figs.get(slug)
        check("surface.%s.present" % slug, f is not None, slug)
        if not f:
            continue
        po = f.get("page_occupancy") or {}
        check("surface.%s.page_buffer_id" % slug, po.get("resource_id") == "00010000022925049708",
              str(po.get("resource_id")))
        check("surface.%s.mapped_pages" % slug, po.get("mapped_page_count") == 42,
              str(po.get("mapped_page_count")))
        check("surface.%s.bbox_crop" % slug,
              po.get("crop_xywh") == [0, 0, po.get("mapped_bbox_xyxy", [0, 0, 0, 0])[2] + 16,
                                      po.get("mapped_bbox_xyxy", [0, 0, 0, 0])[3] + 16],
              f"{po.get('crop_xywh')} vs {po.get('mapped_bbox_xyxy')}")
        try:
            pages, union, _ = page_union(dump / po["payload"]["file"], po["atlas_size"], po["crop_xywh"])
        except Exception as e:
            pages, union = -1, -1
            check("surface.%s.recompute" % slug, False, repr(e))
        check("surface.%s.mapped_pages_recomputed" % slug, pages == po.get("mapped_page_count"),
              f"{pages} vs {po.get('mapped_page_count')}")
        check("surface.%s.union_recomputed" % slug, union == po.get("union_pixels_in_crop"),
              f"{union} vs {po.get('union_pixels_in_crop')}")
        check("surface.%s.common_matches_union" % slug, f["stats"].get(common_key) == union,
              f"{f['stats'].get(common_key)} vs {union}")
        if slug == "surface-cache-material-layers":
            counts = f["stats"].get("panel_common_counts") or {}
            check("surface.material.panels_share_mask",
                  len(counts) == 4 and all(v == union for v in counts.values()), str(counts))
        else:
            cap = f["stats"].get("capture_union_valid_pixels")
            check("surface.lighting.capture_union_matches_card",
                  card is not None and cap == (card.get("card_capture") or {}).get("current_frame_union_pixels"),
                  f"{cap} vs {(card.get('card_capture') or {}).get('current_frame_union_pixels')}")
            cc = f.get("card_capture_union") or {}
            check("surface.lighting.capture_union_evidence",
                  cc.get("union_pixels") == cap and cc.get("rect_count") == 37,
                  str(cc))

    # 3. SceneColor: one and only one shared transform.
    sc = [f for f in man["figures"] if f["slug"].startswith("scene-color-")]
    check("scenecolor.three_entries", len(sc) == 3, str([f["slug"] for f in sc]))
    transforms = [json.dumps(f["display"]["transform"], sort_keys=True) for f in sc]
    check("scenecolor.one_transform", len(set(transforms)) == 1, f"{len(set(transforms))} distinct")
    if transforms:
        t = json.loads(transforms[0])
        check("scenecolor.per_image_scale_false", t.get("per_image_scale") is False, str(t.get("per_image_scale")))
        check("scenecolor.has_oetf", "sRGB" in str(t.get("oetf")), str(t.get("oetf")))
        check("scenecolor.has_exposure", float(t.get("linear_exposure_scale", 0)) > 0,
              str(t.get("linear_exposure_scale")))
        check("scenecolor.has_tone_curve", bool(t.get("tone_curve")), str(t.get("tone_curve")))

    # 4. Unavailable items: concrete cause.
    check("unavailable.present", len(man.get("unavailable", [])) > 0, str(len(man.get("unavailable", []))))
    for u in man.get("unavailable", []):
        check("unavailable." + str(u.get("item", "?")),
              bool(u.get("item")) and bool(u.get("reason")) and bool(u.get("evidence")),
              json.dumps(u, sort_keys=True)[:160])

    # 5. Inventory completeness and hashes.
    for name, filename in (("passes", "passes.json"), ("resources", "resources.json"),
                           ("payloads", "payloads.json"), ("graph_index", "graph-index.json"),
                           ("summary", "summary.json")):
        check("inventory." + name + ".exists", (INV / filename).exists(), filename)
    if (INV / "passes.json").exists():
        passes = load(INV / "passes.json")
        check("inventory.passes.count", passes["count"] == len(passes["items"]), str(passes["count"]))
        check("inventory.passes.928", passes["count"] == 928, str(passes["count"]))
    if (INV / "resources.json").exists():
        res = load(INV / "resources.json")
        check("inventory.resources.count", res["count"] == len(res["items"]), str(res["count"]))
    if (INV / "payloads.json").exists():
        pay = load(INV / "payloads.json")
        check("inventory.payloads.count", pay["count"] == len(pay["items"]), str(pay["count"]))
        bad = 0
        for row in pay["items"]:
            if row.get("file") is None or row.get("sha256") is None:
                bad += 1
        check("inventory.payloads.rows_have_sha", bad == 0, f"{bad} missing")
    if (INV / "graph-index.json").exists():
        gi = load(INV / "graph-index.json")
        check("inventory.graph_index.selected", gi.get("selected_graph") == selected, str(gi.get("selected_graph")))
        check("inventory.graph_index.graphs", len(gi.get("graphs", [])) >= 2, str(len(gi.get("graphs", []))))
        check("inventory.graph_index.selected_present",
              any(g["prefix"] == selected and g["selected"] for g in gi.get("graphs", [])))

    # 6. Root explorer / HTML present and contains every graph prefix.
    index = FIG_DIR / "index.html"
    check("html.exists", index.exists(), str(index))
    if index.exists():
        html = index.read_text(encoding="utf-8", errors="replace")
        check("html.has_selected_graph", selected in html, selected)
        check("html.has_all_graphs", all(g["prefix"] in html for g in man["selection"]["graphs"]),
              ",".join(g["prefix"] for g in man["selection"]["graphs"]))
        imgs = re.findall(r"<img[^>]+src='([^']+)'", html)
        check("html.image_sources_are_svg",
              len(imgs) == len(man["figures"]) and all(src.endswith(".svg") for src in imgs),
              f"{len(imgs)} images; {sorted(set(src.rsplit('.', 1)[-1] for src in imgs))}")
        check("html.vector_files_linked",
              all(f["vector_image"]["file"] in html for f in man["figures"]
                  if f.get("vector_image")),
              "missing vector href")
        check("html.figure_anchors",
              all(("id='%s'" % f["slug"]) in html for f in man["figures"]),
              "missing per-figure slug anchor")
        check("html.raw_explorer_links",
              all(("#resource/" + f["source"]["resource_id"]) in html for f in man["figures"]),
              "missing raw explorer link")
        check("html.manifest_link", "figure-manifest.json" in html, "manifest link")

    # Print deterministic result.
    print("final figure verifier")
    print(f"figures: {len(man['figures'])}")
    print(f"selected graph: {selected}")
    print(f"selected view: {selected_view['content']} sha256 {selected_view['bin_sha256']}")
    print(f"checks: {sum(1 for _, ok, _ in checks if ok)}/{len(checks)}")
    if failures:
        print(f"FAIL: {len(failures)} failure(s)")
        for name, detail in failures[:60]:
            print(f"  - {name}: {detail}")
        return 1
    print("OK: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
