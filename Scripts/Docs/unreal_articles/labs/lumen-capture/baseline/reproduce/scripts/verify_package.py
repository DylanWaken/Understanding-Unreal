#!/usr/bin/env python
"""Verify the portable publication package without Unreal, a GPU or the raw dump.

Checks:
  * file-list.json completeness, sizes and SHA-256;
  * every figure against its manifest (bytes, hash, dimensions);
  * retained native inventory counts and explicit known gaps;
  * scene/light/exposure/sample-index facts;
  * absence of raw Resources/, build products and private logs;
  * Python syntax of all copied scripts.

The optional original-image comparison only runs when both external inputs are
supplied; it is never required by the package.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import py_compile
import re
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from image_stats import Png, compare  # noqa: E402

FORBIDDEN_DIRS = {"Binaries", "Intermediate", "DerivedDataCache", "Saved"}
FORBIDDEN_SUFFIXES = {".log", ".pdb", ".dll", ".exe", ".lib", ".obj", ".exp", ".sarif"}
TEXT_SUFFIXES = {".json", ".md", ".py", ".ps1", ".txt", ".ini", ".uproject", ".csv"}
ABSOLUTE_PATTERNS = [
    re.compile(r"[A-Za-z]:\\"),
    re.compile(r"C:/Users/", re.IGNORECASE),
    re.compile(r"UnrealUnderstanding-Evidence", re.IGNORECASE),
    re.compile(r"Users[\\/]Administrator", re.IGNORECASE),
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None)
    ap.add_argument("--published-lumen", default=None)
    ap.add_argument("--scene-inventory", default=None)
    args = ap.parse_args()
    root = Path(args.root).resolve() if args.root else HERE.parents[1]
    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    required = [
        "README.md", "reproduction.md", "file-list.json", "package-report.json",
        "package-report.md",
        "figures/reference/lumen-lit.png",
        "figures/reference/pathtracer-256spp-8bounces.png",
        "figures/reference/pathtracer-1024spp-8bounces.png",
        "figures/native/decoded/gbuffer-a-normal.png",
        "figures/native/vsm/vsm-mask.png",
        "figures/native/native-screenshot.png",
        "data/reference/scene.json",
        "data/reference/capture-manifest.json",
        "data/reference/capture-session.json",
        "data/reference/reference-image-map.json",
        "data/native/inventory.json",
        "data/native/pass-inventory.json",
        "data/native/resource-files.json",
        "data/native/missing-coverage.json",
        "data/native/runtime-actual.json",
        "data/native/figure-manifest.json",
        "data/native/figure-to-native-map.json",
        "data/native/native-dump-summary.json",
        "data/native/dump-metadata/Base/Passes.json",
        "data/native/dump-metadata/Base/ResourceDescs.json",
        "data/native/dump-metadata/Base/ConsoleVariables.csv",
        "reproduce/UnrealLab/UnrealLab.uproject",
        "reproduce/UnrealLab/Content/Labs/LumenRoom.umap",
        "reproduce/scripts/verify_package.py",
    ]
    for rel in required:
        check("required:" + rel, (root / rel).is_file(), rel)

    # ---------------------------------------------------------------- file list
    file_list_path = root / "file-list.json"
    listed = {}
    if file_list_path.exists():
        fl = load_json(file_list_path)
        check("file_list_schema", fl.get("schema") == "publication-file-list-v1", fl.get("schema"))
        for entry in fl.get("files", []):
            dest = entry.get("destination", "")
            listed[dest] = entry
            path = root / dest
            exists = path.is_file()
            check("file_list:exists:" + dest, exists, dest)
            if exists:
                size_ok = path.stat().st_size == entry.get("size")
                hash_ok = sha256(path) == entry.get("sha256")
                check("file_list:size:" + dest, size_ok,
                      f"actual={path.stat().st_size} listed={entry.get('size')}")
                check("file_list:sha256:" + dest, hash_ok, entry.get("sha256", ""))
        actual = {
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file()
            and p.relative_to(root).as_posix() != "file-list.json"
            and not p.relative_to(root).as_posix().startswith(".build/")
        }
        missing = sorted(actual - set(listed))
        extra = sorted(set(listed) - actual)
        check("file_list:complete", not missing and not extra,
              f"unlisted={missing[:8]} missing={extra[:8]} files={len(actual)} listed={len(listed)}")
        check("file_list:count", len(fl.get("files", [])) == len(actual),
              f"{len(fl.get('files', []))} vs {len(actual)}")
    else:
        check("file_list_schema", False, "file-list.json missing")

    # ---------------------------------------------------------------- figures
    fm = load_json(root / "data/native/figure-manifest.json")
    check("figure_manifest:decoded_count", len(fm["figures"]) == 13, len(fm["figures"]))
    check("figure_manifest:vsm_count", len(fm["vsm_visualizations"]) == 3,
          len(fm["vsm_visualizations"]))
    for fig in fm["figures"]:
        rel = str(fig["file"]).replace("\\", "/")
        path = root / rel
        exists = path.is_file()
        check("figure:exists:" + rel, exists, rel)
        if not exists:
            continue
        check("figure:size:" + rel, path.stat().st_size == fig["png_bytes"],
              f"actual={path.stat().st_size} manifest={fig['png_bytes']}")
        check("figure:sha256:" + rel, sha256(path) == fig["png_sha256"], fig["png_sha256"])
        png = Png(path)
        check("figure:extent:" + rel, [png.width, png.height] == list(fig["source_extent"]),
              f"png={png.width}x{png.height} source={fig['source_extent']}")
        check("figure:native_mapping:" + rel,
              bool(fig.get("source_resource_id") and fig.get("version_pass_id") is not None),
              f"resource={fig.get('source_resource_id')} pass={fig.get('version_pass_id')}")
    for item in fm["vsm_visualizations"]:
        rel = str(item["image"]["file"]).replace("\\", "/")
        path = root / rel
        exists = path.is_file()
        check("vsm:exists:" + rel, exists, rel)
        if exists:
            check("vsm:size:" + rel, path.stat().st_size == item["image"]["bytes"])
            check("vsm:sha256:" + rel, sha256(path) == item["image"]["sha256"])
            png = Png(path)
            check("vsm:extent:" + rel, (png.width, png.height) == (1280, 720),
                  f"{png.width}x{png.height}")
    baseline = fm["vsm_lit_baseline"]
    path = root / str(baseline["file"]).replace("\\", "/")
    check("vsm_baseline:exists", path.is_file(), path)
    if path.is_file():
        check("vsm_baseline:sha256", sha256(path) == baseline["sha256"])
        check("vsm_baseline:size", path.stat().st_size == baseline["bytes"])
    pt1024 = fm["path_traced_1024"]
    pt_path = root / str(pt1024["image"]["file"]).replace("\\", "/")
    check("pt1024:exists", pt_path.is_file(), pt_path)
    if pt_path.is_file():
        check("pt1024:sha256", sha256(pt_path) == pt1024["image"]["sha256"])
        check("pt1024:sample_index", pt1024["sample_index_before_screenshot"] == 1024,
              pt1024["sample_index_before_screenshot"])
        check("pt1024:sample_count", pt1024["sample_count_reported"] == 1024,
              pt1024["sample_count_reported"])
        check("pt1024:bounces", pt1024["max_bounces"] == 8, pt1024["max_bounces"])

    ref_map = load_json(root / "data/reference/reference-image-map.json")
    check("reference:image_count", len(ref_map["images"]) == 3, len(ref_map["images"]))
    for item in ref_map["images"]:
        rel = str(item["figure"]).replace("\\", "/")
        path = root / rel
        exists = path.is_file()
        check("reference:exists:" + rel, exists, rel)
        if not exists:
            continue
        check("reference:size:" + rel, path.stat().st_size == item["bytes"])
        check("reference:sha256:" + rel, sha256(path) == item["sha256"], item["sha256"])
        png = Png(path)
        check("reference:extent:" + rel, (png.width, png.height) == (1280, 720),
              f"{png.width}x{png.height}")
    check("reference:pt256_samples",
          ref_map["images"][1]["samples_per_pixel_engine_index"] == 256,
          ref_map["images"][1]["samples_per_pixel_engine_index"])
    check("reference:pt1024_samples",
          ref_map["images"][2]["samples_per_pixel_engine_index"] == 1024,
          ref_map["images"][2]["samples_per_pixel_engine_index"])

    # ---------------------------------------------------------------- scene/exposure
    ref_scene = load_json(root / "data/reference/scene.json")
    native_scene = load_json(root / "data/native/scene.json")
    for label, scene in (("reference", ref_scene), ("native", native_scene)):
        light = scene["lighting"]["rect_light"]
        check(f"scene:{label}:map_sha256",
              scene["map_sha256"] == "f7255018f916af33894b1d0bd811b21585dc0151fca33fddc3a538851337e818",
              scene["map_sha256"])
        check(f"scene:{label}:attenuation_1000", light["attenuation_radius_cm"] == 1000.0,
              light["attenuation_radius_cm"])
        check(f"scene:{label}:intensity_2400", light["intensity_lumens"] == 2400.0,
              light["intensity_lumens"])
        exp = scene["exposure"]
        check(f"scene:{label}:exposure_histogram",
              "AEM_HISTOGRAM" in str(exp.get("auto_exposure_method", "")),
              exp.get("auto_exposure_method"))
        check(f"scene:{label}:exposure_min_max_4",
              exp.get("min_brightness") == 4.0 and exp.get("max_brightness") == 4.0 and
              exp.get("bias") == 0.0,
              f"min={exp.get('min_brightness')} max={exp.get('max_brightness')} bias={exp.get('bias')}")
        check(f"scene:{label}:not_manual",
              "not manual" in str(exp.get("effective_method", "")).lower() or
              "not manual" in str(exp.get("note", "")).lower(),
              exp.get("effective_method"))

    ref_manifest = load_json(root / "data/reference/capture-manifest.json")
    check("capture:pt256_samples", ref_manifest["path_tracer"]["sample_index_at_capture"] == 256,
          ref_manifest["path_tracer"]["sample_index_at_capture"])
    check("capture:pt256_count", ref_manifest["path_tracer"]["sample_count_reported"] == 256)
    check("capture:pt256_bounces", ref_manifest["path_tracer"]["max_bounces"] == 8)
    check("capture:draw_not_sample",
          ref_manifest["frame_and_warmup"]["one_sample_per_viewport_draw"] is False,
          ref_manifest["frame_and_warmup"].get("one_sample_per_viewport_draw_note"))
    session = load_json(root / "data/reference/capture-session.json")
    pt = session["path_tracer"]
    check("session:pt256_samples", pt["sample_index_before_screenshot"] == 256)
    check("session:pt256_count", pt["sample_count_reported"] == 256)
    check("session:draws_for_256", pt.get("draw_requests_for_256_samples") == 86,
          pt.get("draw_requests_for_256_samples"))
    check("session:sample_count_is_not_draw_count",
          pt.get("sample_count_is_not_draw_count") is True)

    # ---------------------------------------------------------------- inventories
    inv = load_json(root / "data/native/inventory.json")
    passes = load_json(root / "data/native/pass-inventory.json")
    files = load_json(root / "data/native/resource-files.json")
    missing = load_json(root / "data/native/missing-coverage.json")
    check("inventory:pass_count", inv["pass_count"] == 928, inv["pass_count"])
    check("inventory:resource_count", inv["resource_count"] == 1389, inv["resource_count"])
    check("inventory:resource_file_count", inv["resource_file_count"] == 2481,
          inv["resource_file_count"])
    check("inventory:resource_bytes_total", inv["resource_bytes_total"] == 14517078510,
          inv["resource_bytes_total"])
    check("inventory:resources_len", len(inv["resources"]) == 1389, len(inv["resources"]))
    check("passes:len", len(passes["passes"]) == 928, len(passes["passes"]))
    check("resource_files:len", len(files["files"]) == 2481, len(files["files"]))
    check("resource_files:bytes_total", files["bytes_total"] == 14517078510,
          files["bytes_total"])
    coverage = {}
    for resource in inv["resources"]:
        coverage[resource["coverage"]] = coverage.get(resource["coverage"], 0) + 1
    check("inventory:captured", coverage.get("captured") == 1284, coverage)
    check("inventory:descriptor_only", coverage.get("descriptor_only") == 105, coverage)
    check("missing:resources_total", missing["counts"]["resources_total"] == 1389)
    check("missing:resources_captured", missing["counts"]["resources_captured"] == 1284)
    check("missing:descriptor_only", missing["counts"]["resources_descriptor_only"] == 105)
    check("missing:partial", missing["counts"]["resources_partial"] == 0)
    check("missing:texture3d", missing["counts"]["texture3d_total"] == 78 and
          missing["counts"]["texture3d_captured"] == 0, missing["counts"])
    check("missing:texturecube", missing["counts"]["texturecube_total"] == 3 and
          missing["counts"]["texturecube_captured"] == 0, missing["counts"])
    check("missing:skipped_warnings", len(missing["skipped_resource_warnings"]) == 24,
          len(missing["skipped_resource_warnings"]))
    check("missing:passes_payload", missing["counts"]["passes_without_parameter_payload"] == 0)
    check("missing:passes_metadata", len(missing["passes_without_parameter_metadata"]) == 0,
          len(missing["passes_without_parameter_metadata"]))
    limits = {item["limit"]: item for item in missing["structural_limits"]}
    check("missing:texture3d_limit", "texture3d_unsupported" in limits)
    check("missing:skip_tracking_limit", "skip_tracking_defaults" in limits)
    check("missing:descriptor_records", len(missing["resources_descriptor_only"]) == 105,
          len(missing["resources_descriptor_only"]))

    summary = load_json(root / "data/native/native-dump-summary.json")
    check("summary:status", summary["status"] == "ok", summary["status"])
    check("summary:raw_not_included", summary["raw_dump_included"] is False)
    check("summary:counts", summary["counts"]["resources_total"] == 1389 and
          summary["counts"]["passes_total"] == 928 and
          summary["counts"]["resource_binary_files"] == 2481)
    gaps = " ".join(str(item) for item in summary["known_gaps"])
    check("summary:lumen_card_scene_gap", "LumenCardScene" in gaps)
    check("summary:texture3d_gap", "Texture3D" in gaps)
    check("summary:skip_tracking_gap", "SkipTracking" in gaps)

    # CSV row counts must match JSON inventories.
    def csv_rows(rel):
        with open(root / rel, "r", encoding="utf-8-sig", newline="") as f:
            return sum(1 for _ in csv.reader(f)) - 1
    check("csv:inventory_rows", csv_rows("data/native/inventory.csv") == 1389,
          csv_rows("data/native/inventory.csv"))
    check("csv:pass_rows", csv_rows("data/native/pass-inventory.csv") == 928,
          csv_rows("data/native/pass-inventory.csv"))
    check("csv:resource_file_rows", csv_rows("data/native/resource-files.csv") == 2481,
          csv_rows("data/native/resource-files.csv"))

    # ---------------------------------------------------------------- hygiene
    all_files = [
        p for p in root.rglob("*")
        if p.is_file() and not p.relative_to(root).as_posix().startswith(".build/")
    ]
    bad_dirs = [p for p in all_files if any(part in FORBIDDEN_DIRS for part in p.parts)]
    check("hygiene:no_build_dirs", not bad_dirs, [str(p.relative_to(root)) for p in bad_dirs[:5]])
    bad_suffix = [p for p in all_files if p.suffix.lower() in FORBIDDEN_SUFFIXES]
    check("hygiene:no_logs_or_binaries", not bad_suffix,
          [str(p.relative_to(root)) for p in bad_suffix[:5]])
    resources_dirs = [p for p in root.rglob("*") if p.is_dir() and p.name.lower() == "resources"]
    check("hygiene:no_raw_resources_dir", not resources_dirs)
    check("hygiene:no_unreal_log", not (root / "data/native/dump-metadata/Base/UnrealLab.log").exists())
    total = sum(p.stat().st_size for p in all_files)
    check("hygiene:package_under_100mb", total < 100 * 1024 * 1024, f"{total} bytes")
    oversized = [p for p in all_files if p.stat().st_size > 20 * 1024 * 1024]
    check("hygiene:no_file_over_20mb", not oversized,
          [str(p.relative_to(root)) for p in oversized])

    text_violations = []
    for path in all_files:
        if path.name == "verify_package.py":
            continue
        if path.suffix.lower() in TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8", errors="ignore")
            scan_text = text
            if "StructuresMetadata" in path.parts:
                # These files are byte-identical evidence; their engine-source FileName
                # values necessarily retain the external engine root.
                scan_text = scan_text.replace("D:\\\\UnrealEngine", "EXTERNAL_ENGINE_ROOT")
                scan_text = scan_text.replace("D:\\UnrealEngine", "EXTERNAL_ENGINE_ROOT")
            for pattern in ABSOLUTE_PATTERNS:
                match = pattern.search(scan_text)
                if match:
                    text_violations.append(f"{path.relative_to(root)}: {match.group(0)}")
                    break
    check("hygiene:no_absolute_host_paths", not text_violations, text_violations[:8])

    # ---------------------------------------------------------------- scripts
    py_files = sorted((root / "reproduce/scripts").glob("*.py"))
    compile_errors = []
    with tempfile.TemporaryDirectory() as tmp:
        for path in py_files:
            try:
                py_compile.compile(str(path), cfile=str(Path(tmp) / (path.stem + ".pyc")),
                                   doraise=True)
            except Exception as exc:  # noqa: BLE001
                compile_errors.append(f"{path.name}: {exc}")
    check("scripts:py_compile", not compile_errors, compile_errors)
    check("scripts:count", len(py_files) >= 9, len(py_files))
    script_abs = []
    for path in py_files:
        if path.name == "verify_package.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"[A-Za-z]:\\|C:/Users/|D:/Unreal", text):
            script_abs.append(path.name)
    check("scripts:no_absolute_paths", not script_abs, script_abs)

    # ---------------------------------------------------------------- docs wording
    docs = "\n".join(
        (root / rel).read_text(encoding="utf-8", errors="ignore")
        for rel in ("reproduction.md", "package-report.md", "README.md")
        if (root / rel).exists()
    )
    for phrase, name in (
        ("not manual", "docs:not_manual_exposure"),
        ("1000 cm", "docs:1000cm_light"),
        ("2400", "docs:2400_lumens"),
        ("8-bounce", "docs:8bounce_truncation"),
        ("MaxPathIntensity", "docs:path_intensity_clamp"),
        ("linear EXR", "docs:not_linear_exr"),
        ("draw requests", "docs:draw_not_samples"),
        ("LumenCardScene", "docs:lumen_card_scene_gap"),
        ("not ground truth", "docs:not_ground_truth"),
    ):
        check(name, phrase.lower() in docs.lower(), phrase)

    # ---------------------------------------------------------------- optional comparison
    if args.published_lumen and args.scene_inventory:
        try:
            lumen = Png(root / "figures/reference/lumen-lit.png")
            published = Png(args.published_lumen)
            comparison = compare(lumen, published)
            check("optional:correlation",
                  comparison["luminance_pearson_correlation"] > 0.98,
                  comparison["luminance_pearson_correlation"])
            check("optional:mean_abs_diff",
                  comparison["mean_absolute_rgb_difference_0_255"] < 10,
                  comparison["mean_absolute_rgb_difference_0_255"])
            inventory = load_json(Path(args.scene_inventory))
            check("optional:scene_inventory_actors", len(inventory.get("actors", [])) == 10,
                  len(inventory.get("actors", [])))
        except Exception as exc:  # noqa: BLE001
            check("optional:original_comparison", False, str(exc))
    else:
        checks.append({
            "name": "optional:original_comparison",
            "ok": True,
            "skipped": True,
            "detail": "supply --published-lumen and --scene-inventory to run it",
        })

    failures = [c for c in checks if not c["ok"]]
    result = {
        "schema": "publication-verification-v1",
        "root": ".",
        "status": "ok" if not failures else "failed",
        "checks_total": len(checks),
        "checks_passed": len(checks) - len(failures),
        "failed_checks": [c["name"] for c in failures],
        "checks": checks,
    }
    print(json.dumps(result, indent=2))
    if failures:
        print("VERIFICATION FAILED: " + ", ".join(c["name"] for c in failures), file=sys.stderr)
        return 1
    print(f"VERIFICATION OK: all {len(checks)} checks passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
