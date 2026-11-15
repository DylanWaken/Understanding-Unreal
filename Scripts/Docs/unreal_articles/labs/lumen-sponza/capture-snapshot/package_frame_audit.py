#!/usr/bin/env python3
"""Independently audit a frame-explorer package against its source capture.

Unlike ``package_frame.py verify`` (which checks the package against the hashes
recorded inside it), this audit hashes the original source files and the
reconstructed payload files themselves.  It proves that every raw file is
accounted for, that every reconstructed byte matches the source, that every
native copy matches its source, and that every package file is listed with its
actual size and SHA-256.

Standard library only.  Read-only with respect to the source and the package.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import gzip
import hashlib
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    with path.open("rb") as f:
        while True:
            block = f.read(1024 * 1024)
            if not block:
                break
            h.update(block)
            total += len(block)
    return h.hexdigest(), total


def load_gz_json(path: Path) -> Any:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def rel_files(root: Path) -> list[str]:
    out = []
    for path in root.rglob("*"):
        if path.is_file():
            out.append(path.relative_to(root).as_posix())
    return sorted(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--package", required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--reconstructed", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)

    package = Path(args.package).resolve()
    source = Path(args.source).resolve()
    recon = Path(args.reconstructed).resolve()
    output = Path(args.output).resolve()
    errors: list[str] = []

    index = json.loads((package / "index.json").read_text(encoding="utf-8"))
    inventory = load_gz_json(package / "inventory.json.gz")
    coverage = load_gz_json(package / "coverage.json.gz")
    native_inputs = load_gz_json(package / "native-inputs.json.gz")

    # ---------------------------------------------------------- package files
    package_paths = rel_files(package)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        package_hashes = list(pool.map(lambda r: (r, *sha256_file(package / r)), package_paths))
    package_files = [{"path": r, "bytes": n, "sha256": s} for r, s, n in package_hashes]
    package_bytes = sum(f["bytes"] for f in package_files)
    largest = max(package_files, key=lambda f: f["bytes"], default={"path": None, "bytes": 0})
    chunk_files = [f for f in package_files if f["path"].startswith("chunks/")]
    native_files = [f for f in package_files if f["path"].startswith("native/")]
    metadata_files = [f for f in package_files
                      if not f["path"].startswith("chunks/")
                      and not f["path"].startswith("native/")
                      and f["path"] != "index.json"]
    metadata_bytes = sum(f["bytes"] for f in metadata_files)
    index_bytes = (package / "index.json").stat().st_size
    native_bytes = sum(f["bytes"] for f in native_files)

    counts = index.get("counts") or {}
    if len(package_files) != counts.get("packageFiles"):
        errors.append(f"package file count {len(package_files)} != index {counts.get('packageFiles')}")
    if largest["bytes"] != counts.get("largestOutputBytes"):
        errors.append(f"largest file {largest['bytes']} != index {counts.get('largestOutputBytes')}")
    if len(chunk_files) != counts.get("chunkFiles"):
        errors.append(f"chunk file count {len(chunk_files)} != index {counts.get('chunkFiles')}")
    if sum(f["bytes"] for f in chunk_files) != counts.get("compressedChunkBytes"):
        errors.append("compressed chunk bytes != index compressedChunkBytes")
    if len(native_files) != counts.get("nativeFiles"):
        errors.append(f"native file count {len(native_files)} != index {counts.get('nativeFiles')}")

    # ------------------------------------------------------------- chunk check
    chunk_mismatches = []
    chunk_raw_bytes = 0
    for chunk in index.get("chunks", []):
        path = package / chunk["file"]
        if not path.is_file():
            chunk_mismatches.append({"sha": chunk["sha"], "error": "missing file"})
            continue
        try:
            with gzip.open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            chunk_mismatches.append({"sha": chunk["sha"], "error": f"gzip: {exc}"})
            continue
        actual = hashlib.sha256(data).hexdigest()
        chunk_raw_bytes += len(data)
        if actual != chunk["sha"] or len(data) != chunk["rawLength"]:
            chunk_mismatches.append({
                "sha": chunk["sha"], "actual_sha": actual,
                "bytes": len(data), "expected_bytes": chunk["rawLength"]})
    if chunk_mismatches:
        errors.append(f"{len(chunk_mismatches)} chunk mismatch(es)")

    # ------------------------------------------------- source/payload coverage
    payloads = inventory.get("payloads") or []
    native_records = inventory.get("nativeFiles") or []
    payload_paths = [p["originalPath"] for p in payloads]
    native_paths = [n["path"] for n in native_records]
    accounted = set(payload_paths) | set(native_paths)
    source_paths = set(rel_files(source))
    missing_from_package = sorted(source_paths - accounted)
    extra_in_package = sorted(accounted - source_paths)
    if missing_from_package:
        errors.append(f"{len(missing_from_package)} source file(s) not accounted for")
    if extra_in_package:
        errors.append(f"{len(extra_in_package)} package path(s) absent from source")
    if len(payload_paths) != len(set(payload_paths)):
        errors.append("duplicate payload originalPath in inventory")

    # ------------------------------------------------------- native byte check
    def check_native(rec: dict[str, Any]) -> dict[str, Any]:
        pkg_path = package / rec["packagePath"]
        src_path = source / rec["path"]
        if not pkg_path.is_file():
            return {"path": rec["path"], "error": "missing package copy"}
        if not src_path.is_file():
            return {"path": rec["path"], "error": "missing source file"}
        pkg_sha, pkg_size = sha256_file(pkg_path)
        src_sha, src_size = sha256_file(src_path)
        ok = pkg_sha == src_sha == rec.get("sha256") and pkg_size == src_size == rec.get("bytes")
        return {"path": rec["path"], "ok": ok, "packageSha256": pkg_sha,
                "sourceSha256": src_sha, "bytes": src_size}

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        native_results = list(pool.map(check_native, native_records))
    native_bad = [r for r in native_results if not r.get("ok")]
    if native_bad:
        errors.append(f"{len(native_bad)} native copy mismatch(es)")

    # ----------------------------------------------------- payload byte check
    def check_payload(payload: dict[str, Any]) -> dict[str, Any]:
        rel = payload["originalPath"]
        src_path = source / rel
        rec_path = recon / rel
        if not src_path.is_file():
            return {"path": rel, "error": "missing source file"}
        if not rec_path.is_file():
            return {"path": rel, "error": "missing reconstructed file"}
        src_sha, src_size = sha256_file(src_path)
        rec_sha, rec_size = sha256_file(rec_path)
        ok = (src_sha == rec_sha == payload.get("sha256")
              and src_size == rec_size == payload.get("bytes"))
        return {"path": rel, "ok": ok, "sourceSha256": src_sha,
                "reconstructedSha256": rec_sha, "bytes": src_size}

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        payload_results = list(pool.map(check_payload, payloads))
    payload_bad = [r for r in payload_results if not r.get("ok")]
    payload_bytes = sum(r["bytes"] for r in payload_results if "bytes" in r)
    if payload_bad:
        errors.append(f"{len(payload_bad)} payload mismatch(es)")
    if payload_bytes != counts.get("payloadBytes"):
        errors.append(f"reconstructed payload bytes {payload_bytes} != index {counts.get('payloadBytes')}")

    # ---------------------------------------------------------- reconstruct log
    recon_manifest_path = recon / "reconstruction-manifest.json"
    recon_manifest = None
    if recon_manifest_path.is_file():
        recon_manifest = json.loads(recon_manifest_path.read_text(encoding="utf-8"))
        if len(recon_manifest.get("payloads") or []) != len(payloads):
            errors.append("reconstruction manifest payload count mismatch")

    # ------------------------------------------------------------------ gaps
    gaps = coverage.get("gaps") or []
    if len(gaps) != (coverage.get("counts") or {}).get("gapCount"):
        errors.append("coverage gap count mismatch")
    gap_kinds = Counter(g["kind"] for g in gaps)
    categories = [{"id": c["id"], "count": c["count"], "omitted": c.get("omitted")}
                  for c in coverage.get("categories") or []]
    native_kinds = Counter(i.get("kind") for i in native_inputs.get("items") or [])

    # ------------------------------------------------------------ package links
    promised = index.get("files") or {}
    broken_links = []
    for key, rel in promised.items():
        if key in ("chunks", "records", "native", "reconstruct"):
            continue
        if not (package / rel).is_file():
            broken_links.append({"key": key, "path": rel})
    if broken_links:
        errors.append(f"{len(broken_links)} promised metadata file(s) missing")

    report = {
        "format": "sponza-frame-package-audit",
        "version": 1,
        "generatedUtc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "ok": not errors,
        "errors": errors,
        "package": {
            "path": str(package),
            "files": len(package_files),
            "bytes": package_bytes,
            "largest": largest,
            "chunkFiles": len(chunk_files),
            "compressedChunkBytes": sum(f["bytes"] for f in chunk_files),
            "uniqueChunkRawBytes": chunk_raw_bytes,
            "uniqueChunks": len(index.get("chunks") or []),
            "chunkMismatches": chunk_mismatches,
            "nativeFiles": len(native_files),
            "nativeBytes": native_bytes,
            "metadataFiles": len(metadata_files),
            "metadataStoredBytes": metadata_bytes,
            "indexBytes": index_bytes,
            "countsMatch": not any(e for e in errors if "index" in e),
            "brokenMetadataLinks": broken_links,
        },
        "source": {
            "path": str(source),
            "files": len(source_paths),
            "payloadFiles": len(payloads),
            "nativeFiles": len(native_records),
            "accountedFiles": len(accounted),
            "missingFromPackage": missing_from_package[:20],
            "missingCount": len(missing_from_package),
            "extraInPackage": extra_in_package[:20],
            "extraCount": len(extra_in_package),
        },
        "reconstruction": {
            "path": str(recon),
            "payloadFiles": len(payloads),
            "payloadBytes": payload_bytes,
            "payloadsCompared": len(payload_results),
            "payloadMismatches": len(payload_bad),
            "mismatchSample": payload_bad[:20],
            "manifestPayloads": len((recon_manifest or {}).get("payloads") or []),
        },
        "nativeCopies": {
            "compared": len(native_results),
            "mismatches": len(native_bad),
            "mismatchSample": native_bad[:20],
        },
        "coverage": {
            "complete": coverage.get("complete"),
            "gapCount": len(gaps),
            "bySeverity": coverage.get("bySeverity"),
            "byScope": coverage.get("byScope"),
            "byKind": dict(sorted(gap_kinds.items())),
            "categories": categories,
        },
        "nativeInputs": {
            "count": native_inputs.get("count"),
            "byKind": dict(sorted(native_kinds.items())),
        },
        "files": package_files,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"package files: {len(package_files)}; bytes: {package_bytes}; "
          f"largest: {largest['path']} ({largest['bytes']} bytes)")
    print(f"chunks: {len(chunk_files)} ({report['package']['compressedChunkBytes']} compressed, "
          f"{chunk_raw_bytes} raw); mismatches: {len(chunk_mismatches)}")
    print(f"source files: {len(source_paths)}; accounted: {len(accounted)}; "
          f"missing: {len(missing_from_package)}; extra: {len(extra_in_package)}")
    print(f"payloads compared: {len(payload_results)} ({payload_bytes} bytes); "
          f"mismatches: {len(payload_bad)}")
    print(f"native copies compared: {len(native_results)}; mismatches: {len(native_bad)}")
    print(f"coverage gaps: {len(gaps)}; complete: {coverage.get('complete')}")
    print(f"wrote {output}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
