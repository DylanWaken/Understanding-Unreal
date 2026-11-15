#!/usr/bin/env python3
"""Shared metadata container helpers for frame-explorer packages.

A package may keep its generated JSON raw (the historical layout) or store the
same bytes as ``<path>.json.gz``.  ``index.json`` is always the raw bootstrap;
``native/`` and ``chunks/`` are always exact/raw and are never decompressed by
these helpers.

The functions in this module are deliberately independent of
``package_frame.py`` so that readers and validators can share one hardened
implementation:

* ``read_package_bytes(package, rel)`` / ``read_package_json(package, rel)``
  resolve a logical metadata path (raw or ``.gz``), reject ambiguity, unsafe
  paths and corrupt gzip, and return the original uncompressed bytes/JSON.
* ``compress_package_in_place(package)`` converts a freshly built raw package.
* ``convert_package(source, dest)`` copies a package to a *separate* directory
  and then compresses the copy; the source is never modified.

Compression is deterministic: gzip level 6 with ``mtime=0``.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import sys
import zlib
from pathlib import Path
from typing import Any

PACKAGE_FORMAT = "unreal-frame-explorer-package"
PACKAGE_VERSION = 1
METADATA_GZIP_LEVEL = 6
GZIP_SUFFIX = ".gz"
METADATA_COMPRESSION = "gzip"
METADATA_BOOTSTRAP = "index.json"
METADATA_NATIVE_COMPRESSION = "none"
# Generated metadata is exactly the top-level JSON documents (except the raw
# index bootstrap) plus lazy JSON records.  Native sidecars and chunk payloads
# are outside this contract.
_TOP_LEVEL_SKIP = frozenset({"index.json"})
_RECORD_PREFIX = "records/"
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)],
    *[f"LPT{i}" for i in range(1, 10)],
}


class MetadataError(RuntimeError):
    """Raised when package metadata is missing, unsafe, ambiguous or corrupt."""


# --------------------------------------------------------------------- paths
def safe_relpath(value: str) -> str:
    """Normalize a relative path and refuse anything that can escape a root."""
    if value is None:
        raise MetadataError("Empty path is not allowed.")
    raw = str(value)
    if "\x00" in raw:
        raise MetadataError("NUL byte in path.")
    raw = raw.replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise MetadataError(f"Absolute path is not allowed: {value!r}")
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise MetadataError(f"Path traversal is not allowed: {value!r}")
        if any(ord(ch) < 32 for ch in part):
            raise MetadataError(f"Control character in path: {value!r}")
        if ":" in part:
            raise MetadataError(f"Drive/ADS separator in path: {value!r}")
        stem = part.split(".")[0].upper()
        if stem in WINDOWS_RESERVED:
            raise MetadataError(f"Windows reserved filename in path: {value!r}")
        if part.endswith(" ") or part.endswith("."):
            raise MetadataError(f"Windows-incompatible trailing character in path: {value!r}")
        parts.append(part)
    if not parts:
        raise MetadataError(f"Path resolves to nothing: {value!r}")
    return "/".join(parts)


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def is_metadata_rel(rel: str) -> bool:
    """True for generated JSON metadata, never for native/chunk payloads."""
    rel = safe_relpath(rel)
    base = rel[:-len(GZIP_SUFFIX)] if rel.endswith(GZIP_SUFFIX) else rel
    if not base.endswith(".json"):
        return False
    if "/" not in base:
        return True
    return base.startswith(_RECORD_PREFIX)


def metadata_rel(rel: str, compressed: bool = True) -> str:
    """Return the actual package path for a logical ``.json`` metadata path."""
    rel = safe_relpath(rel)
    if rel.endswith(GZIP_SUFFIX):
        rel = rel[:-len(GZIP_SUFFIX)]
    if not rel.endswith(".json"):
        raise MetadataError(f"Not a JSON metadata path: {rel!r}")
    if not is_metadata_rel(rel):
        raise MetadataError(f"Refusing non-metadata path: {rel!r}")
    if rel == METADATA_BOOTSTRAP and compressed:
        raise MetadataError("index.json is the raw bootstrap and must not be compressed.")
    return rel + GZIP_SUFFIX if compressed else rel


def package_path(package: Path, rel: str) -> Path:
    package = Path(package).resolve()
    candidate = (package / safe_relpath(rel)).resolve()
    if not is_within(candidate, package):
        raise MetadataError(f"Path escapes package root: {rel!r}")
    return candidate


def resolve_metadata_path(package: Path, rel: str, *, require: bool = True) -> Path | None:
    """Resolve a logical metadata path to its actual raw or gzip file."""
    rel = safe_relpath(rel)
    if not is_metadata_rel(rel):
        raise MetadataError(f"Refusing non-metadata path: {rel!r}")
    if rel.endswith(GZIP_SUFFIX):
        actual = package_path(package, rel)
        if actual.is_file():
            return actual
        if require:
            raise MetadataError(f"Missing metadata file: {rel}")
        return None
    raw = package_path(package, rel)
    compressed = package_path(package, rel + GZIP_SUFFIX)
    raw_exists = raw.is_file()
    compressed_exists = compressed.is_file()
    if raw_exists and compressed_exists:
        raise MetadataError(
            f"Ambiguous metadata layout for {rel}: both {rel} and {rel + GZIP_SUFFIX} exist."
        )
    if raw_exists:
        return raw
    if compressed_exists:
        return compressed
    if require:
        raise MetadataError(f"Missing metadata file: {rel}")
    return None


# ------------------------------------------------------------------- reading
def read_package_bytes(package: Path, rel: str) -> bytes:
    """Return the original uncompressed bytes for one metadata document."""
    path = resolve_metadata_path(package, rel)
    assert path is not None
    data = path.read_bytes()
    if path.name.endswith(GZIP_SUFFIX):
        try:
            return gzip.decompress(data)
        except (OSError, EOFError, zlib.error) as exc:
            raise MetadataError(
                f"Corrupt gzip metadata {path.relative_to(Path(package).resolve())}: {exc}"
            ) from exc
    return data


def read_package_json(package: Path, rel: str) -> Any:
    """Return parsed JSON for one metadata document, raw or gzip."""
    data = read_package_bytes(package, rel)
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MetadataError(f"Invalid JSON metadata {rel}: {exc}") from exc


# ------------------------------------------------------------------- writing
def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def gzip_bytes(data: bytes, level: int = METADATA_GZIP_LEVEL) -> bytes:
    """Deterministic gzip with an in-memory lossless round-trip check."""
    if not 1 <= int(level) <= 9:
        raise MetadataError(f"gzip level must be in 1..9, got {level!r}")
    compressed = gzip.compress(data, compresslevel=int(level), mtime=0)
    try:
        restored = gzip.decompress(compressed)
    except (OSError, EOFError, zlib.error) as exc:  # pragma: no cover - defensive
        raise MetadataError(f"gzip round-trip failed: {exc}") from exc
    if restored != data:
        raise MetadataError("gzip round-trip did not preserve the source bytes.")
    return compressed


def metadata_block(level: int = METADATA_GZIP_LEVEL) -> dict[str, Any]:
    return {
        "compression": METADATA_COMPRESSION,
        "level": int(level),
        "bootstrap": METADATA_BOOTSTRAP,
        "nativeCompression": METADATA_NATIVE_COMPRESSION,
    }


def metadata_files(package: Path) -> list[str]:
    """Logical paths of every generated metadata candidate in the package."""
    package = Path(package).resolve()
    rels: list[str] = []
    for path in sorted(package.glob("*.json")):
        rel = path.relative_to(package).as_posix()
        if rel not in _TOP_LEVEL_SKIP:
            rels.append(rel)
    records = package / "records"
    if records.is_dir():
        for path in sorted(records.rglob("*.json")):
            rels.append(path.relative_to(package).as_posix())
    return rels


def package_file_stats(package: Path) -> tuple[int, dict[str, Any]]:
    package = Path(package).resolve()
    count = 0
    largest = {"path": "", "bytes": 0}
    for path in package.rglob("*"):
        if not path.is_file():
            continue
        size = path.stat().st_size
        count += 1
        if size > largest["bytes"]:
            largest = {"path": path.relative_to(package).as_posix(), "bytes": size}
    return count, largest


def _write_raw(package: Path, rel: str, data: bytes) -> None:
    path = package_path(package, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _update_records_document(package: Path, rel: str) -> None:
    doc = read_package_json(package, rel)
    if not isinstance(doc, dict):
        raise MetadataError(f"{rel}: expected one JSON object.")
    items = doc.get("items")
    if not isinstance(items, list):
        raise MetadataError(f"{rel}: missing items array.")
    for item in items:
        if not isinstance(item, dict):
            raise MetadataError(f"{rel}: item is not an object.")
        record = item.get("record")
        if not isinstance(record, str) or not record:
            raise MetadataError(f"{rel}: item has no record path.")
        item["record"] = metadata_rel(record, True)
    _write_raw(package, rel, json_bytes(doc))


def _update_index(index: dict[str, Any], level: int) -> dict[str, Any]:
    if index.get("format") != PACKAGE_FORMAT:
        raise MetadataError(f"Unexpected package format: {index.get('format')!r}")
    files = index.get("files")
    if not isinstance(files, dict):
        raise MetadataError("index.json has no files map.")
    for key, logical in list(files.items()):
        if not isinstance(logical, str):
            continue
        base = logical[:-len(GZIP_SUFFIX)] if logical.endswith(GZIP_SUFFIX) else logical
        if not base.endswith(".json"):
            continue
        if not is_metadata_rel(base):
            raise MetadataError(f"index.files[{key!r}] is not a metadata path: {logical!r}")
        if base == METADATA_BOOTSTRAP:
            files[key] = METADATA_BOOTSTRAP
        else:
            files[key] = metadata_rel(base, True)
    files["index"] = METADATA_BOOTSTRAP
    schema = index.get("schema")
    if not isinstance(schema, dict):
        raise MetadataError("index.json has no schema block.")
    schema_path = schema.get("path")
    if not isinstance(schema_path, str) or not schema_path:
        raise MetadataError("index.json has no schema.path.")
    schema["path"] = metadata_rel(schema_path, True)
    files["schema"] = schema["path"]
    index["metadata"] = metadata_block(level)
    return index


def compress_package_in_place(package: Path, level: int = METADATA_GZIP_LEVEL) -> dict[str, Any]:
    """Compress the generated metadata of an already-built raw package.

    Record/schema/top-level bytes are gzipped deterministically.  Native files
    and chunks are never opened.  ``index.json`` is rewritten raw last with the
    actual compressed paths, the metadata block and the final container size.
    """
    package = Path(package).resolve()
    if not 1 <= int(level) <= 9:
        raise MetadataError(f"gzip level must be in 1..9, got {level!r}")
    index_path = package / METADATA_BOOTSTRAP
    if not index_path.is_file():
        raise MetadataError(f"Not a frame-explorer package (missing raw index.json): {package}")
    if (package / (METADATA_BOOTSTRAP + GZIP_SUFFIX)).exists():
        raise MetadataError("index.json must remain the raw bootstrap; found index.json.gz.")
    index = read_package_json(package, METADATA_BOOTSTRAP)
    if not isinstance(index, dict):
        raise MetadataError("index.json must contain one JSON object.")

    for rel in ("resources.json", "passes.json"):
        if resolve_metadata_path(package, rel, require=False) is not None:
            _update_records_document(package, rel)

    index = _update_index(index, int(level))

    candidates = metadata_files(package)
    if not candidates:
        raise MetadataError("Package has no generated metadata to compress.")
    raw_total = 0
    stored_total = 0
    for rel in candidates:
        raw_path = package_path(package, rel)
        compressed_path = package_path(package, rel + GZIP_SUFFIX)
        if compressed_path.exists():
            raise MetadataError(f"Ambiguous metadata layout for {rel}: both raw and gzip exist.")
        raw = raw_path.read_bytes()
        try:
            json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MetadataError(f"{rel}: invalid JSON metadata: {exc}") from exc
        compressed = gzip_bytes(raw, int(level))
        compressed_path.write_bytes(compressed)
        raw_path.unlink()
        raw_total += len(raw)
        stored_total += len(compressed)
        if read_package_bytes(package, rel) != raw:
            raise MetadataError(f"{rel}: on-disk gzip round-trip mismatch.")
    if resolve_metadata_path(package, METADATA_BOOTSTRAP, require=False) is None:
        raise MetadataError("index.json disappeared during conversion.")

    # Write the raw bootstrap with final container metadata.  Iterate to a
    # fixed point because the index itself can be the largest package file.
    _write_raw(package, METADATA_BOOTSTRAP, json_bytes(index))
    count = 0
    largest = {"path": "", "bytes": 0}
    for _ in range(4):
        count, largest = package_file_stats(package)
        counts = index.setdefault("counts", {})
        counts["packageFiles"] = count
        counts["largestOutputBytes"] = largest["bytes"] if largest else None
        index["largestOutput"] = largest
        data = json_bytes(index)
        if (package / METADATA_BOOTSTRAP).read_bytes() == data:
            break
        (package / METADATA_BOOTSTRAP).write_bytes(data)
    return {
        "metadataFiles": len(candidates),
        "metadataRawBytes": raw_total,
        "metadataGzipBytes": stored_total,
        "packageFiles": count,
        "largestOutput": largest,
        "level": int(level),
    }


def convert_package(source: Path, destination: Path, level: int = METADATA_GZIP_LEVEL,
                    force: bool = False) -> dict[str, Any]:
    """Copy a package to a separate directory, then gzip the copy's metadata."""
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if source == destination:
        raise MetadataError("Conversion destination must differ from the source package.")
    if is_within(destination, source) or is_within(source, destination):
        raise MetadataError("Conversion destination must not be inside or contain the source package.")
    if not source.is_dir():
        raise MetadataError(f"Source package does not exist: {source}")
    if not (source / METADATA_BOOTSTRAP).is_file():
        raise MetadataError(f"Not a frame-explorer package (missing index.json): {source}")
    if destination.exists():
        if not destination.is_dir():
            raise MetadataError(f"Conversion destination is not a directory: {destination}")
        if any(destination.iterdir()):
            if not force:
                raise MetadataError(
                    f"Conversion destination is not empty: {destination}. Use --force."
                )
            shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True)
    info = compress_package_in_place(destination, level)
    info["source"] = str(source)
    info["output"] = str(destination)
    return info


# ----------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read or convert gzip-compressed frame-package metadata.")
    sub = parser.add_subparsers(dest="command", required=True)
    convert = sub.add_parser(
        "convert", help="Copy a raw package to a separate directory and gzip its generated metadata.")
    convert.add_argument("--input", required=True, help="Existing raw package directory (never modified).")
    convert.add_argument("--output", required=True, help="Separate directory for the converted copy.")
    convert.add_argument("--level", type=int, default=METADATA_GZIP_LEVEL, choices=range(1, 10))
    convert.add_argument("--force", action="store_true", help="Replace a non-empty output directory.")
    read = sub.add_parser("read", help="Print one metadata document (raw or gzip) as JSON.")
    read.add_argument("--package", required=True)
    read.add_argument("--path", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "convert":
            info = convert_package(Path(args.input), Path(args.output), args.level, args.force)
            print(
                f"converted {info['metadataFiles']} metadata file(s): "
                f"{info['metadataRawBytes']} raw bytes -> {info['metadataGzipBytes']} gzip bytes"
            )
            print(f"output: {info['output']}")
        else:
            value = read_package_json(Path(args.package), args.path)
            print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except MetadataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
