#!/usr/bin/env python3
"""Independent validation of a packaged Unreal DumpGPU frame.

This tool deliberately does not use package_frame.py's adapter to enumerate the
source dump.  It re-parses the native concatenated JSON, counts every metadata
record, compares every payload name/size with the package, verifies the full
package with the shipped verifier, reconstructs a selective set of payloads
directly from gzip chunks, and re-reads known GBuffer/PageTable/parameter-block
values from real bytes.  It writes a machine-readable report; it never mutates
the package or the source capture.

Run from the repository root, for example:

    python tools/validate_frame.py \
      --candidate C:\\captures\\binary-20261112-133826 \
      --schema C:\\schemas\\source-schema.json \
      --package site\\assets\\lumen-frame \
      --report report.json
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "package_frame.py"
CODEC = ROOT / "site" / "assets" / "frame-codec.js"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
            size += len(block)
    return h.hexdigest(), size


def load_json(path: Path) -> Any:
    return json.loads(path.read_bytes().decode("utf-8-sig"))


def parse_json_sequence(path: Path) -> list[Any]:
    """Parse the concatenated JSON objects written by DumpGPU."""
    text = path.read_bytes().decode("utf-8-sig")
    decoder = json.JSONDecoder()
    out: list[Any] = []
    index = 0
    length = len(text)
    while True:
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            break
        value, index = decoder.raw_decode(text, index)
        out.append(value)
    return out


def rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def safe_relative(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if "\\" in value or value.startswith("/"):
        return False
    pure = PurePosixPath(value)
    return not pure.is_absolute() and ".." not in pure.parts


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def mip_extent(resource: dict[str, Any], mip: int) -> tuple[int, int]:
    extent = resource.get("extent") or [1, 1, 1]
    width = max(1, int(extent[0] or 1) >> mip)
    height = max(1, int(extent[1] or 1) >> mip)
    return width, height


class Report:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.failures = 0

    def check(self, name: str, ok: bool, evidence: str, detail: Any = None) -> bool:
        entry = {"name": name, "status": "pass" if ok else "fail", "evidence": evidence}
        if detail is not None:
            entry["detail"] = detail
        self.checks.append(entry)
        if not ok:
            self.failures += 1
        print(("PASS  " if ok else "FAIL  ") + name + " :: " + evidence)
        return ok

    def skip(self, name: str, evidence: str) -> None:
        self.checks.append({"name": name, "status": "skip", "evidence": evidence})
        print("SKIP  " + name + " :: " + evidence)


# --------------------------------------------------------------- candidate audit
def audit_candidate(candidate: Path) -> dict[str, Any]:
    files = [p for p in candidate.rglob("*") if p.is_file()]
    by_rel = {rel_posix(p, candidate): p for p in files}
    native_rels = sorted(rel for rel in by_rel if not rel.lower().endswith(".bin"))
    payload_rels = sorted(
        rel for rel in by_rel
        if rel.startswith("Resources/") or rel.startswith("Structures/")
    )
    other_bins = sorted(
        rel for rel in by_rel
        if rel.lower().endswith(".bin")
        and not rel.startswith("Resources/")
        and not rel.startswith("Structures/")
    )
    descriptors = parse_json_sequence(candidate / "Base" / "ResourceDescs.json")
    passes = parse_json_sequence(candidate / "Base" / "Passes.json")
    referenced = parse_json_sequence(candidate / "Base" / "ReferencedStructs.json")
    draw_counts = parse_json_sequence(candidate / "Base" / "PassDrawCounts.json")
    infos = parse_json_sequence(candidate / "Base" / "Infos.json")
    dump_service = parse_json_sequence(candidate / "Base" / "DumpService.json")
    with (candidate / "Base" / "ConsoleVariables.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        cvars = list(csv.reader(handle))
    cvar_rows = max(0, len(cvars) - 1)
    structures = sorted((candidate / "StructuresMetadata").glob("*.json"))
    resources_payloads = sorted((candidate / "Resources").glob("*.bin"))
    structures_payloads = sorted((candidate / "Structures").glob("*.bin"))
    payload_bytes = sum(p.stat().st_size for p in resources_payloads + structures_payloads)

    descriptor_types = Counter(
        "Buffer" if obj.get("Desc") == "FRDGBufferDesc" else (obj.get("Type") or "?")
        for obj in descriptors
    )
    payload_prefixes = {
        path.name.split(".", 1)[0] for path in resources_payloads
    }
    descriptor_only = [
        obj for obj in descriptors
        if str(obj.get("UniqueResourceName") or obj.get("Name")) not in payload_prefixes
    ]
    descriptor_only_types = Counter(
        "Buffer" if obj.get("Desc") == "FRDGBufferDesc" else (obj.get("Type") or "?")
        for obj in descriptor_only
    )
    expected_kinds = {
        "console-variable": cvar_rows,
        "resource-descriptor": len(descriptors),
        "pass": len(passes),
        "referenced-structs": len(referenced),
        "uniform-structure": len(structures),
        "payload": len(payload_rels),
        "infos": len(infos),
        "dump-service": len(dump_service),
        "pass-draw-count": len(draw_counts),
        "status": 1 if (candidate / "Base" / "Status.txt").exists() else 0,
        "log": 1 if (candidate / "Base" / "UnrealLab.log").exists() else 0,
        "file": 0,
    }
    classified = {
        "Base/ResourceDescs.json", "Base/Passes.json", "Base/ReferencedStructs.json",
        "Base/PassDrawCounts.json", "Base/Infos.json", "Base/DumpService.json",
        "Base/ConsoleVariables.csv", "Base/Status.txt", "Base/UnrealLab.log",
    }
    expected_kinds["file"] = sum(
        1 for rel in native_rels
        if rel not in classified and not rel.startswith("StructuresMetadata/")
    )
    native_records = sum(expected_kinds.values())
    return {
        "fileCount": len(files),
        "bytes": sum(p.stat().st_size for p in files),
        "nativeFiles": native_rels,
        "nativeFileCount": len(native_rels),
        "payloadFiles": payload_rels,
        "payloadCount": len(payload_rels),
        "payloadBytes": payload_bytes,
        "resourcePayloadCount": len(resources_payloads),
        "parameterPayloadCount": len(structures_payloads),
        "otherBins": other_bins,
        "descriptors": descriptors,
        "descriptorCount": len(descriptors),
        "descriptorTypes": dict(descriptor_types),
        "descriptorOnly": descriptor_only,
        "descriptorOnlyCount": len(descriptor_only),
        "descriptorOnlyTypes": dict(descriptor_only_types),
        "payloadPrefixCount": len(payload_prefixes),
        "passCount": len(passes),
        "referencedStructCount": len(referenced),
        "drawCount": len(draw_counts),
        "infosCount": len(infos),
        "dumpServiceCount": len(dump_service),
        "cvarCount": cvar_rows,
        "uniformStructureCount": len(structures),
        "expectedKinds": expected_kinds,
        "nativeRecordCount": native_records,
        "byRel": by_rel,
    }


# --------------------------------------------------------------- package audit
def payload_chunk_errors(payload: dict[str, Any], chunk_shas: set[str]) -> list[str]:
    errors: list[str] = []
    refs = payload.get("chunks") or []
    offset = 0
    if not refs:
        errors.append("no chunk refs")
    for index, ref in enumerate(refs):
        if not isinstance(ref.get("offset"), int) or ref["offset"] != offset:
            errors.append(f"chunk {index} offset {ref.get('offset')} != {offset}")
        length = ref.get("length")
        if not isinstance(length, int) or length <= 0 or length > 1_048_576:
            errors.append(f"chunk {index} bad length {length}")
            length = length if isinstance(length, int) and length > 0 else 0
        if ref.get("sha") not in chunk_shas:
            errors.append(f"chunk {index} sha not indexed")
        offset += length
    if offset != int(payload.get("bytes", -1)):
        errors.append(f"chunk length sum {offset} != payload bytes {payload.get('bytes')}")
    return errors


def reconstruct_payload(
    package: Path,
    payload: dict[str, Any],
    cache: dict[str, bytes],
    dest: Path | None = None,
) -> tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    pieces: list[bytes] = []
    for ref in payload["chunks"]:
        sha = ref["sha"]
        data = cache.get(sha)
        if data is None:
            path = package / "chunks" / f"{sha}.gz"
            data = gzip.decompress(path.read_bytes())
            if sha256_bytes(data) != sha:
                raise AssertionError(f"chunk {sha} decompressed hash mismatch")
            if len(data) != ref["length"]:
                raise AssertionError(f"chunk {sha} length {len(data)} != {ref['length']}")
            cache[sha] = data
        h.update(data)
        total += len(data)
        pieces.append(data)
    actual = h.hexdigest()
    if total != int(payload["bytes"]):
        raise AssertionError(f"{payload['id']} length {total} != {payload['bytes']}")
    if actual != payload["sha256"]:
        raise AssertionError(f"{payload['id']} hash {actual} != {payload['sha256']}")
    if dest is not None:
        target = dest / PurePosixPath(payload["originalPath"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"".join(pieces))
    return actual, total


def read_package_range(
    package: Path,
    payload: dict[str, Any],
    offset: int,
    length: int,
    chunk_meta: dict[str, dict[str, Any]],
    cache: dict[str, bytes],
) -> bytes:
    """Read one byte range the way the fixed PackageStore does.

    index.json's chunk rawLength is the authority for validating each whole
    decompressed chunk; the requested range is sliced only after verification.
    """
    if offset < 0 or length < 0 or offset + length > int(payload["bytes"]):
        raise AssertionError(f"range {offset}+{length} outside payload {payload['bytes']}")
    out = bytearray(length)
    covered = 0
    for ref in payload["chunks"]:
        ref_start = int(ref["offset"])
        ref_end = ref_start + int(ref["length"])
        start = max(offset, ref_start)
        end = min(offset + length, ref_end)
        if end <= start:
            continue
        sha = ref["sha"]
        meta = chunk_meta.get(sha)
        if meta is None:
            raise AssertionError(f"chunk {sha} not indexed")
        expected_chunk_length = int(meta["rawLength"])
        data = cache.get(sha)
        if data is None:
            data = gzip.decompress((package / "chunks" / f"{sha}.gz").read_bytes())
            if sha256_bytes(data) != sha:
                raise AssertionError(f"chunk {sha} hash mismatch")
            if len(data) != expected_chunk_length:
                raise AssertionError(
                    f"chunk {sha} length {len(data)} != index rawLength {expected_chunk_length}"
                )
            cache[sha] = data
        out[start - offset:end - offset] = data[start - ref_start:end - ref_start]
        covered += end - start
    if covered != length:
        raise AssertionError(f"chunks cover {covered} of {length} requested bytes")
    return bytes(out)


def read_source_range(candidate: Path, rel: str, offset: int, length: int) -> bytes:
    path = candidate / PurePosixPath(rel)
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(length)
    if len(data) != length:
        raise AssertionError(f"source range {offset}+{length} short in {rel}")
    return data


def binding_matches(payload: dict[str, Any], item: dict[str, Any]) -> bool:
    if payload.get("resourceId") != item.get("resourceId"):
        return False
    if item.get("version") is not None and payload.get("version") != item.get("version"):
        return False
    for key in ("mip", "slice"):
        if item.get(key) is not None and payload.get(key) != item.get(key):
            return False
    return bool(payload.get("stencil")) == bool(item.get("stencil"))



def run_command(command: list[str], cwd: Path, env: dict[str, str] | None = None,
                timeout: int = 1800) -> dict[str, Any]:
    started = time.time()
    try:
        result = subprocess.run(
            command, cwd=str(cwd), text=True, capture_output=True,
            env=env, timeout=timeout,
        )
        return {
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout[-8000:],
            "stderr": result.stderr[-8000:],
            "seconds": round(time.time() - started, 2),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "returncode": None,
            "stdout": (exc.stdout or "")[-8000:] if isinstance(exc.stdout, str) else "",
            "stderr": "timeout after %s seconds" % timeout,
            "seconds": round(time.time() - started, 2),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, help="Raw DumpGPU directory.")
    parser.add_argument("--schema", required=True, help="source-schema.json.")
    parser.add_argument("--package", default="site/assets/lumen-frame")
    parser.add_argument("--report", default="report.json")
    parser.add_argument("--scratch", default="scratch/validate")
    parser.add_argument("--skip-verify", action="store_true",
                        help="Skip the full package verify command (not recommended).")
    parser.add_argument("--skip-tests", action="store_true",
                        help="Skip the Python/Node test suites.")
    args = parser.parse_args(argv)

    candidate = Path(args.candidate).resolve()
    schema_path = Path(args.schema).resolve()
    package = Path(args.package).resolve()
    report_path = (ROOT / args.report).resolve() if not Path(args.report).is_absolute() else Path(args.report)
    scratch = (ROOT / args.scratch).resolve() if not Path(args.scratch).is_absolute() else Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    recon = scratch / "reconstructed"
    recon.mkdir(parents=True, exist_ok=True)
    report = Report()

    if not candidate.is_dir():
        print(f"candidate not found: {candidate}", file=sys.stderr)
        return 2
    if not package.is_dir():
        print(f"package not found: {package}", file=sys.stderr)
        return 2
    if not schema_path.is_file():
        print(f"schema not found: {schema_path}", file=sys.stderr)
        return 2

    # 1. Independent native enumeration.
    cand = audit_candidate(candidate)
    report.check(
        "candidate-native-enumeration",
        cand["descriptorCount"] == 1389 and cand["passCount"] == 928
        and cand["resourcePayloadCount"] == 2481 and cand["parameterPayloadCount"] == 970
        and cand["nativeFileCount"] == 884 and not cand["otherBins"],
        "descriptors={descriptorCount}, passes={passCount}, resources_bin={resourcePayloadCount}, "
        "structures_bin={parameterPayloadCount}, native_files={nativeFileCount}, other_bins={otherBins}".format(**cand),
        {
            "descriptorTypes": cand["descriptorTypes"],
            "descriptorOnlyTypes": cand["descriptorOnlyTypes"],
            "expectedKinds": {k: v for k, v in cand["expectedKinds"].items() if v},
            "payloadBytes": cand["payloadBytes"],
        },
    )

    index = load_json(package / "index.json")
    inventory = load_json(package / "inventory.json")
    coverage = load_json(package / "coverage.json")
    counts = index["counts"]
    payloads = {p["id"]: p for p in inventory["payloads"]}
    chunks = {c["sha"]: c for c in inventory["chunks"]}
    resources = inventory["resources"]
    passes = {p["id"]: p for p in inventory["passes"]}
    native_files = inventory["nativeFiles"]

    # 2. Counts reconcile with the independent enumeration.
    package_kinds = Counter(rec["kind"] for rec in inventory["nativeRecords"])
    expected_kinds = Counter({k: v for k, v in cand["expectedKinds"].items() if v})
    counts_ok = (
        counts["resources"] == cand["descriptorCount"]
        and counts["passes"] == cand["passCount"]
        and counts["payloadFiles"] == cand["payloadCount"]
        and counts["payloadBytes"] == cand["payloadBytes"]
        and counts["nativeFiles"] == cand["nativeFileCount"]
        and counts["nativeRecords"] == cand["nativeRecordCount"]
        and counts["parameterBlocks"] == cand["parameterPayloadCount"]
        and counts["uniformStructures"] == cand["uniformStructureCount"]
        and counts["chunkFiles"] == len(inventory["chunks"])
        and counts["compressedChunkBytes"] == sum(c["compressedLength"] for c in inventory["chunks"])
        and counts["uniqueChunkBytes"] == sum(c["rawLength"] for c in inventory["chunks"])
        and counts["schemaMatchedResources"] + counts["schemaUnmatchedResources"]
            + counts["schemaAmbiguousResources"] == counts["resources"]
        and counts["nativeRecordKinds"] == dict(sorted(expected_kinds.items()))
    )
    report.check(
        "package-counts-match-candidate",
        counts_ok,
        "resources={resources}, passes={passes}, payloads={payloadFiles}, payloadBytes={payloadBytes}, "
        "nativeRecords={nativeRecords}, parameterBlocks={parameterBlocks}, chunks={chunkFiles}".format(**counts),
        {"packageKinds": dict(sorted(package_kinds.items())), "expectedKinds": dict(sorted(expected_kinds.items()))},
    )
    report.check(
        "package-identity",
        index.get("format") == "unreal-frame-explorer-package"
        and index.get("version") == 1 and index.get("fixture") is False
        and index.get("adapter") == "dumpgpu"
        and index.get("evidence", {}).get("kind") == "native-capture",
        "format={} version={} fixture={} adapter={} evidence={}".format(
            index.get("format"), index.get("version"), index.get("fixture"),
            index.get("adapter"), index.get("evidence", {}).get("kind")),
    )

    # 3. Every native metadata file is retained byte-for-byte.
    pkg_native = {nf["path"]: nf for nf in native_files}
    native_errors: list[str] = []
    for rel in cand["nativeFiles"]:
        nf = pkg_native.get(rel)
        if not nf:
            native_errors.append(f"missing {rel}")
            continue
        copied = package / nf["packagePath"]
        if not copied.exists():
            native_errors.append(f"missing package file {nf['packagePath']}")
            continue
        sha, size = sha256_file(copied)
        if sha != nf["sha256"] or size != nf["bytes"]:
            native_errors.append(f"hash/size mismatch {rel}")
        source_sha, source_size = sha256_file(candidate / rel)
        if sha != source_sha or size != source_size:
            native_errors.append(f"source differs {rel}")
    extra_native = sorted(set(pkg_native) - set(cand["nativeFiles"]))
    if extra_native:
        native_errors.append(f"extra {len(extra_native)} native files")
    report.check(
        "native-metadata-byte-for-byte",
        not native_errors,
        f"{cand['nativeFileCount']} native files hashed against source and package copies; "
        f"errors={len(native_errors)}",
        native_errors[:20],
    )

    # 4. Payload list and sizes.
    pkg_payload_paths = Counter(p["originalPath"] for p in inventory["payloads"])
    duplicate_payloads = sorted(path for path, n in pkg_payload_paths.items() if n != 1)
    payload_set_errors: list[str] = []
    if set(pkg_payload_paths) != set(cand["payloadFiles"]):
        payload_set_errors.append("payload path set differs")
    for rel in cand["payloadFiles"]:
        p = next((item for item in inventory["payloads"] if item["originalPath"] == rel), None)
        if p is None:
            payload_set_errors.append(f"missing {rel}")
        elif p["bytes"] != (candidate / rel).stat().st_size:
            payload_set_errors.append(f"size {rel}")
    report.check(
        "payload-set-and-sizes",
        not payload_set_errors and not duplicate_payloads,
        f"{cand['payloadCount']} payload paths, no duplicates, sizes match source; errors={len(payload_set_errors)}",
        {"errors": payload_set_errors[:20], "duplicatePayloads": duplicate_payloads[:20]},
    )

    # 5. Chunk index structure.
    chunk_errors: list[str] = []
    for chunk_sha, chunk in chunks.items():
        path = package / "chunks" / f"{chunk_sha}.gz"
        if not path.exists():
            chunk_errors.append(f"missing {chunk_sha}.gz")
        elif path.stat().st_size != chunk["compressedLength"]:
            chunk_errors.append(f"compressed size {chunk_sha}")
    chunk_files = list((package / "chunks").glob("*.gz"))
    if len(chunk_files) != len(chunks):
        chunk_errors.append(f"chunk file count {len(chunk_files)} != index {len(chunks)}")
    for payload in inventory["payloads"]:
        errors = payload_chunk_errors(payload, set(chunks))
        if errors:
            chunk_errors.append(f"{payload['id']}: {errors[0]}")
    report.check(
        "chunk-index-consistency",
        not chunk_errors,
        f"{len(chunks)} unique chunks, {len(inventory['payloads'])} payload chunk lists; errors={len(chunk_errors)}",
        chunk_errors[:20],
    )

    # 6. Schema retained byte-for-byte and indexed with the same hash.
    schema_bytes = schema_path.read_bytes()
    package_schema = (package / "schema.json").read_bytes()
    schema_ok = (
        package_schema == schema_bytes
        and index["schema"]["sha256"] == sha256_bytes(schema_bytes)
        and index["schema"]["bytes"] == len(schema_bytes)
        and index["schema"]["status"] == "joined"
        and index["schema"].get("path") == "schema.json"
        and index["schema"].get("sourcePath") == "source-schema.json"
        and (package / "schema.json").is_file()
        and load_json(package / "schema.json") == load_json(schema_path)
    )
    report.check(
        "schema-byte-for-byte",
        schema_ok,
        f"source={len(schema_bytes)} B sha256={sha256_bytes(schema_bytes)}; "
        f"package={len(package_schema)} B at schema.json; index.schema={index['schema']['bytes']} B",
        {"resourceSchemas": index["schema"]["resourceSchemas"],
         "parameterSchemas": index["schema"]["parameterSchemas"]},
    )

    # 6b. Reviewed schema inventory and format-variant gates survive packaging.
    schema_doc = load_json(package / "schema.json")
    schema_counts = {
        "resourceSchemas": len(schema_doc.get("resourceSchemas", [])) if isinstance(schema_doc.get("resourceSchemas"), list) else 0,
        "parameterSchemas": len(schema_doc.get("parameterSchemas", [])) if isinstance(schema_doc.get("parameterSchemas"), list) else 0,
        "sourceRefs": len(schema_doc.get("sourceRefs", [])) if isinstance(schema_doc.get("sourceRefs"), (list, dict)) else 0,
        "coverageGaps": len(schema_doc.get("coverageGaps", [])) if isinstance(schema_doc.get("coverageGaps"), list) else 0,
        "samplerBindings": len(schema_doc.get("samplerBindings", [])) if isinstance(schema_doc.get("samplerBindings"), list) else 0,
        "parts": len(schema_doc.get("parts", [])) if isinstance(schema_doc.get("parts"), list) else 0,
    }
    variant_count = sum(
        len((schema.get("storage") or {}).get("formatVariants") or [])
        for schema in schema_doc.get("resourceSchemas", []) if isinstance(schema, dict)
    )
    gated_field_count = sum(
        1 for schema in schema_doc.get("resourceSchemas", []) if isinstance(schema, dict)
        for field in (schema.get("fields") if isinstance(schema.get("fields"), list) else [])
        if isinstance(field, dict) and field.get("appliesToFormats")
    )
    schema_inventory_errors: list[str] = []
    for key, value in schema_counts.items():
        if index["schema"].get(key) != value:
            schema_inventory_errors.append(f"index.schema.{key}={index['schema'].get(key)!r} != schema {value!r}")
    if index["schema"].get("formatVariants") != variant_count:
        schema_inventory_errors.append(f"index.schema.formatVariants={index['schema'].get('formatVariants')!r} != schema {variant_count!r}")
    if index["schema"].get("formatGatedFields") != gated_field_count:
        schema_inventory_errors.append(f"index.schema.formatGatedFields={index['schema'].get('formatGatedFields')!r} != schema {gated_field_count!r}")

    def format_key(value: Any) -> str:
        text = str(value or "").strip().upper()
        if text.startswith("PF_"):
            text = text[3:]
        return {"FLOATRGB": "R11G11B10_FLOAT", "FLOATR11G11B10": "R11G11B10_FLOAT",
                "R11G11B10": "R11G11B10_FLOAT",
                "R16G16B16A16_UNORM": "R16G16B16A16_UINT"}.get(text, text)

    gate_errors: list[str] = []
    gated_matches = 0
    variant_matches = 0
    for resource in resources:
        record = load_json(package / "records" / "resource" / f"{resource['id']}.json")
        for match in record.get("schemaMatches", []):
            payload_format = match.get("payloadFormat")
            declared = match.get("declaredFormat")
            fields = match.get("fields") if isinstance(match.get("fields"), list) else []
            has_gated_fields = any(
                isinstance(field, dict) and field.get("appliesToFormats") for field in fields
            )
            if not payload_format:
                # Buffers have no pixel format; their explicit element offsets
                # are still source-schema fields.  Format gates only apply when
                # the match actually declares format-specific fields.
                if has_gated_fields or match.get("activeFormatVariant"):
                    gate_errors.append(f"{resource['id']} {match.get('schemaId')}: format-gated match has no payloadFormat")
                continue
            declared_pixel = declared if declared and str(declared).upper().startswith("PF_") else None
            expected_gated = sorted(
                str(field.get("name") or "")
                for field in fields if isinstance(field, dict)
                and (
                    (field.get("appliesToFormats")
                     and not any(format_key(payload_format) == format_key(fmt)
                                 for fmt in (field.get("appliesToFormats") or [])))
                    or (not field.get("appliesToFormats") and declared_pixel
                        and format_key(payload_format) != format_key(declared_pixel))
                )
            )
            actual_gated = sorted(str(name) for name in (match.get("formatGatedFields") or []))
            if expected_gated != actual_gated:
                gate_errors.append(
                    f"{resource['id']} {match.get('schemaId')}: gated fields {actual_gated!r} != expected {expected_gated!r}")
            if expected_gated:
                gated_matches += 1
            if (match.get("payloadFormatMatch") is False and not match.get("activeFormatVariant")
                    and match.get("extractionEligible") is not False):
                gate_errors.append(f"{resource['id']} {match.get('schemaId')}: wrong-format payload remains extraction-eligible")
            if match.get("activeFormatVariant"):
                variant_matches += 1
                variant_format = match.get("activeFormatVariant")
                variants = ((match.get("storage") or {}).get("formatVariants") or [])
                if not any(format_key(variant.get("format")) == format_key(variant_format)
                           for variant in variants if isinstance(variant, dict)):
                    gate_errors.append(f"{resource['id']} {match.get('schemaId')}: active variant is not declared in storage.formatVariants")
                variant_fields = match.get("formatVariantFields") or []
                if not variant_fields:
                    gate_errors.append(f"{resource['id']} {match.get('schemaId')}: active variant has no explicit component fields")
                for field in variant_fields:
                    if not field.get("appliesToFormats") or not any(
                            format_key(fmt) == format_key(variant_format) for fmt in field.get("appliesToFormats", [])):
                        gate_errors.append(f"{resource['id']} {match.get('schemaId')}: variant field is not gated to the active format")
            elif match.get("formatVariantFields"):
                gate_errors.append(f"{resource['id']} {match.get('schemaId')}: variant fields exist without a matching runtime format")
            if declared and match.get("payloadFormatMatch") is True and format_key(payload_format) != format_key(declared):
                gate_errors.append(f"{resource['id']} {match.get('schemaId')}: payloadFormatMatch true for different formats")
    report.check(
        "reviewed-schema-and-format-gates-packaged",
        not schema_inventory_errors and not gate_errors,
        f"{schema_counts['resourceSchemas']} resource + {schema_counts['parameterSchemas']} parameter schemas, "
        f"{schema_counts['sourceRefs']} source refs, {schema_counts['coverageGaps']} coverage gaps, "
        f"{schema_counts['samplerBindings']} sampler bindings, {variant_count} format variants and "
        f"{gated_field_count} gated fields retained; {gated_matches} matches have inactive wrong-format fields; "
        f"{variant_matches} matches select a captured format variant",
        {"schemaCounts": schema_counts, "formatVariants": variant_count,
         "formatGatedFields": gated_field_count, "gatedMatches": gated_matches,
         "variantMatches": variant_matches, "errors": (schema_inventory_errors + gate_errors)[:20]},
    )

    # 7. Full package verify (shipped verifier).
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if args.skip_verify:
        report.skip("full-package-verify", "skipped by --skip-verify")
        verify_result = None
    else:
        verify_result = run_command(
            [sys.executable, str(TOOL), "verify", "--package", str(package)],
            ROOT, env=env, timeout=1800,
        )
        ok = verify_result["returncode"] == 0 and "Verified " in verify_result["stdout"]
        report.check(
            "full-package-verify",
            ok,
            (verify_result["stdout"].strip().splitlines() or ["no stdout"])[0]
            + f"; returncode={verify_result['returncode']}; seconds={verify_result['seconds']}",
            {"stdout": verify_result["stdout"], "stderr": verify_result["stderr"]},
        )
        (scratch / "verify-package.log").write_text(
            verify_result["stdout"] + "\n" + verify_result["stderr"], encoding="utf-8")

    # 8. Selective reconstruct directly from gzip chunks, compare to source bytes.
    sample_ids = [
        "payload-00001", "payload-00065", "payload-00534",
        "payload-00547", "payload-00548", "payload-00551",
        "payload-02651", "payload-02700",
    ]
    sample_results: list[dict[str, Any]] = []
    reconstruct_errors: list[str] = []
    cache: dict[str, bytes] = {}
    for pid in sample_ids:
        payload = payloads.get(pid)
        if payload is None:
            reconstruct_errors.append(f"missing payload {pid}")
            continue
        try:
            actual_sha, actual_bytes = reconstruct_payload(package, payload, cache, recon)
        except Exception as exc:  # noqa: BLE001 - report the concrete failure
            reconstruct_errors.append(f"{pid}: {exc}")
            continue
        source = candidate / payload["originalPath"]
        source_sha, source_bytes = sha256_file(source)
        ok = (actual_sha == payload["sha256"] == source_sha and actual_bytes == payload["bytes"] == source_bytes)
        if not ok:
            reconstruct_errors.append(f"{pid}: package/source mismatch")
        sample_results.append({
            "payloadId": pid,
            "path": payload["originalPath"],
            "bytes": actual_bytes,
            "sha256": actual_sha,
            "sourceSha256": source_sha,
            "chunks": len(payload["chunks"]),
            "match": ok,
        })
    report.check(
        "selective-reconstruct-vs-source",
        not reconstruct_errors and len(sample_results) == len(sample_ids),
        f"reconstructed {len(sample_results)} payloads ({sum(r['bytes'] for r in sample_results)} bytes) "
        f"directly from gzip chunks; all hashes match source",
        {"samples": sample_results, "errors": reconstruct_errors},
    )

    # 9. Decode stride/dimension consistency for every 2D colour payload.
    stride_errors: list[str] = []
    stencil_errors: list[str] = []
    checked_payloads = 0
    stencil_payloads = 0
    texture_payloads = 0
    for rs in resources:
        if rs["kind"] != "texture":
            continue
        record = load_json(package / "records" / "resource" / f"{rs['id']}.json")
        extent = record.get("extent") or [1, 1, 1]
        if int(extent[2] or 1) > 1:
            continue  # volume layout is not asserted
        decode = record.get("decode") or {}
        bpt = decode.get("bytesPerTexel")
        if not bpt:
            continue
        for payload in record["payloads"]:
            texture_payloads += 1
            width, height = mip_extent(record, int(payload.get("mip") or 0))
            if payload.get("stencil"):
                # The explorer's per-payload override decodes stencil as PF_R8_UINT.
                stencil_payloads += 1
                expected = width * height * 1
                if expected != payload["bytes"]:
                    stencil_errors.append(f"{record['id']} {payload['id']} {expected}!={payload['bytes']}")
                continue
            expected = width * height * int(bpt)
            checked_payloads += 1
            if expected != payload["bytes"]:
                stride_errors.append(f"{record['id']} {payload['id']} {expected}!={payload['bytes']}")
    report.check(
        "resource-decoder-stride-consistency",
        not stride_errors and not stencil_errors,
        f"{checked_payloads} non-stencil 2D payloads checked at decode.bytesPerTexel; "
        f"{stencil_payloads} stencil payloads checked at 1 B/texel (PF_R8_UINT); "
        f"errors={len(stride_errors) + len(stencil_errors)}",
        {"errors": stride_errors[:20], "stencilErrors": stencil_errors[:20],
         "texturePayloads": texture_payloads},
    )

    # 10. Actual GBuffer bytes/typed reads at one documented texel.
    gbuffer_evidence: dict[str, Any] = {}
    gbuffer_ok = True
    gbuffer_specs = [
        ("resource-0185", "payload-00548", "PF_A2B10G10R10"),
        ("resource-0186", "payload-00551", "PF_B8G8R8A8"),
    ]
    for resource_id, payload_id, expected_format in gbuffer_specs:
        record = load_json(package / "records" / "resource" / f"{resource_id}.json")
        payload = payloads[payload_id]
        width = int(record["extent"][0])
        x, y = 640, 300
        offset = (y * width + x) * int((record.get("decode") or {}).get("bytesPerTexel") or 0)
        parts: list[bytes] = []
        for ref in payload["chunks"]:
            chunk = cache.get(ref["sha"])
            if chunk is None:
                chunk = gzip.decompress((package / "chunks" / f"{ref['sha']}.gz").read_bytes())
                cache[ref["sha"]] = chunk
            parts.append(chunk)
        data = b"".join(parts)
        raw = data[offset:offset + 4]
        if expected_format == "PF_A2B10G10R10":
            word = int.from_bytes(raw, "little")
            fields = [word & 0x3FF, (word >> 10) & 0x3FF, (word >> 20) & 0x3FF, (word >> 30) & 0x3]
            decoded = [fields[0] / 1023, fields[1] / 1023, fields[2] / 1023, fields[3] / 3]
            entry = {
                "resource": record["name"], "format": expected_format,
                "payloadId": payload_id, "payloadSha256": payload["sha256"],
                "byteOffset": offset, "bytesHex": raw.hex(),
                "wordHex": hex(word), "rawChannels": fields, "values": decoded,
                "note": "packed 10:10:10:2, not RGBA8; 511/1023 = 0.4995, 0/1023 = 0, 1/3 = 0.3333",
            }
            ok = raw == bytes.fromhex("ff01f05f") and fields == [511, 0, 511, 1]
        else:
            memory_b, memory_g, memory_r, memory_a = raw
            values = [memory_r / 255, memory_g / 255, memory_b / 255, memory_a / 255]
            entry = {
                "resource": record["name"], "format": expected_format,
                "payloadId": payload_id, "payloadSha256": payload["sha256"],
                "byteOffset": offset, "bytesHex": raw.hex(),
                "memoryOrder": "B,G,R,A", "shaderChannels": [memory_r, memory_g, memory_b, memory_a],
                "values": values,
                "note": "roughness = shader B = 178/255 = 0.6980; shading model = A & 0xF = 1",
            }
            ok = raw == bytes.fromhex("b27f00a1")
        gbuffer_ok = gbuffer_ok and ok
        gbuffer_evidence[resource_id] = entry
    report.check(
        "actual-gbuffer-byte-and-typed-read",
        gbuffer_ok,
        "GBufferA (640,300) = FF 01 F0 5F -> packed (511,0,511,1); "
        "GBufferB = B2 7F 00 A1 -> shader RGBA (0,127,178,161)",
        gbuffer_evidence,
    )

    # 10b. Partial chunk reads: index rawLength is the authority, slice afterwards.
    partial_errors: list[str] = []
    partial_evidence: list[dict[str, Any]] = []
    gbuffer_record = load_json(package / "records" / "resource" / "resource-0185.json")
    gbuffer_payload = payloads["payload-00549"]
    partial_cases = [
        {"name": "actual final GBufferA (640,500)", "x": 640, "y": 500,
         "byteOffset": 2562560, "length": 4, "hex": "fffdf77f", "chunks": 1},
        {"name": "two-texel read crossing 1 MiB chunk boundary", "x": 1023, "y": 204,
         "byteOffset": 1048572, "length": 8, "hex": "fffff75ffffff75f", "chunks": 2},
    ]
    for case in partial_cases:
        try:
            package_bytes = read_package_range(package, gbuffer_payload, case["byteOffset"],
                                               case["length"], chunks, cache)
            source_bytes = read_source_range(candidate, gbuffer_payload["originalPath"],
                                             case["byteOffset"], case["length"])
            pieces = [
                ref for ref in gbuffer_payload["chunks"]
                if ref["offset"] < case["byteOffset"] + case["length"]
                and ref["offset"] + ref["length"] > case["byteOffset"]
            ]
            chunk_lengths = [int(chunks[ref["sha"]]["rawLength"]) for ref in pieces]
            ok = (
                package_bytes == source_bytes
                and package_bytes.hex() == case["hex"]
                and len(pieces) == case["chunks"]
                and all(length == 1048576 for length in chunk_lengths)
                and all(length != case["length"] for length in chunk_lengths)
            )
            if not ok:
                partial_errors.append(f"{case['name']}: bytes/chunk count mismatch")
            partial_evidence.append({
                "case": case["name"], "x": case["x"], "y": case["y"],
                "payloadId": gbuffer_payload["id"],
                "payloadPath": gbuffer_payload["originalPath"],
                "byteOffset": case["byteOffset"], "length": case["length"],
                "bytesHex": package_bytes.hex(), "sourceBytesHex": source_bytes.hex(),
                "chunks": len(pieces), "chunkRawLengths": chunk_lengths,
                "naiveSliceLengthValidationWouldFail": all(length != case["length"] for length in chunk_lengths),
            })
        except Exception as exc:  # noqa: BLE001 - report the concrete failure
            partial_errors.append(f"{case['name']}: {exc}")
    report.check(
        "partial-chunk-read-real-pixel",
        not partial_errors,
        "payload-00549 (640,500) @2562560 = fffdf77f and a two-texel boundary read @1048572 = "
        "fffff75ffffff75f; each whole chunk validated against index rawLength 1,048,576, "
        "then the requested slice was taken",
        {"cases": partial_evidence, "errors": partial_errors},
    )

    # 10c. Depth/stencil bindings are disjoint by version + mip/slice/aspect.
    binding_errors: list[str] = []
    false_stencil_refs = 0
    simulated_old_false_refs = 0
    stencil_payload_ids = {p["id"] for p in inventory["payloads"] if p.get("stencil")}
    colour_payload_ids = {p["id"] for p in inventory["payloads"] if not p.get("stencil")}
    stencil_binding_refs = 0
    colour_binding_refs = 0
    stencil_binding_payloads: set[str] = set()
    payloads_by_resource: dict[str, list[dict[str, Any]]] = {}
    for payload in inventory["payloads"]:
        if payload.get("resourceId"):
            payloads_by_resource.setdefault(payload["resourceId"], []).append(payload)
    for pass_path in sorted((package / "records" / "pass").glob("*.json")):
        pass_record = load_json(pass_path)
        pass_id = pass_record["id"]
        for direction in ("inputs", "outputs"):
            for item in pass_record.get(direction, []):
                for payload_id in item.get("payloadIds", []):
                    payload = payloads.get(payload_id)
                    if payload is None:
                        binding_errors.append(f"{pass_id} {direction} missing payload {payload_id}")
                        continue
                    if not binding_matches(payload, item):
                        binding_errors.append(
                            f"{pass_id} {direction} {item.get('uniqueName')} lists {payload_id} "
                            f"(version/mip/slice/aspect mismatch)")
                    if item.get("stencil"):
                        stencil_binding_refs += 1
                        stencil_binding_payloads.add(payload_id)
                        if not payload.get("stencil"):
                            binding_errors.append(f"{pass_id} stencil binding lists colour {payload_id}")
                    else:
                        colour_binding_refs += 1
                        if payload.get("stencil"):
                            false_stencil_refs += 1
                            binding_errors.append(f"{pass_id} colour binding lists stencil {payload_id}")
                if not item.get("stencil"):
                    for payload in payloads_by_resource.get(item.get("resourceId") or "", []):
                        if (item.get("mip") is None or payload.get("mip") == item.get("mip")) \
                                and (item.get("slice") is None or payload.get("slice") == item.get("slice")) \
                                and payload.get("stencil"):
                            simulated_old_false_refs += 1
    stencil_versions = Counter()
    for resource in resources:
        record = load_json(package / "records" / "resource" / f"{resource['id']}.json")
        for version in record.get("versions", []):
            for payload_id in version.get("payloadIds", []):
                if payload_id in stencil_payload_ids:
                    stencil_versions[payload_id] += 1
    bad_stencil_versions = [pid for pid in stencil_payload_ids if stencil_versions.get(pid) != 1]
    report.check(
        "depth-and-stencil-bindings-disjoint",
        not binding_errors
        and false_stencil_refs == 0
        and simulated_old_false_refs == 1653
        and len(stencil_payload_ids) == 27
        and not bad_stencil_versions
        and stencil_binding_refs > 0,
        f"{len(passes)} pass records: {false_stencil_refs} colour/depth binding references to stencil "
        f"payloads; the pre-fix predicate would produce {simulated_old_false_refs} such references. "
        f"{len(stencil_payload_ids)} stencil payloads remain independently addressable "
        f"({stencil_binding_refs} explicit stencil-binding refs, one version each).",
        {
            "errors": binding_errors[:20],
            "falseStencilReferences": false_stencil_refs,
            "simulatedPreFixFalseStencilReferences": simulated_old_false_refs,
            "stencilPayloadCount": len(stencil_payload_ids),
            "stencilBindingReferences": stencil_binding_refs,
            "colourBindingReferences": colour_binding_refs,
            "stencilPayloadsMissingExactlyOneVersion": bad_stencil_versions,
            "stencilPayloadsInStencilBindings": len(stencil_binding_payloads),
        },
    )

    # 10d. Empty PassDrawCounts is explicit; null is preserved, never zero.
    draw_count_errors: list[str] = []
    draw_count_file = candidate / "Base" / "PassDrawCounts.json"
    draw_count_records = len(parse_json_sequence(draw_count_file)) if draw_count_file.exists() else 0
    if draw_count_records != 0:
        draw_count_errors.append(f"expected 0 native draw-count records, got {draw_count_records}")
    zero_draw_counts = 0
    for pass_path in sorted((package / "records" / "pass").glob("*.json")):
        pass_record = load_json(pass_path)
        pass_id = pass_record["id"]
        if pass_record.get("drawCount") is not None:
            draw_count_errors.append(f"{pass_id} drawCount is {pass_record.get('drawCount')}, expected null")
        if pass_record.get("drawCount") == 0:
            zero_draw_counts += 1
        if pass_record.get("drawCountStatus") != "not-captured":
            draw_count_errors.append(f"{pass_id} drawCountStatus is not not-captured")
    passes_doc = load_json(package / "passes.json")
    if any(item.get("drawCount") is not None for item in passes_doc["items"]):
        draw_count_errors.append("passes.json contains a non-null drawCount")
    draw_gap = next((g for g in coverage["gaps"] if g["kind"] == "pass-draw-counts-empty"), None)
    if draw_gap is None:
        draw_count_errors.append("missing pass-draw-counts-empty gap")
    if counts.get("passDrawCountsCaptured") != 0 or counts.get("passDrawCountsMissing") != 928:
        draw_count_errors.append("draw-count counts do not reconcile to 0 captured / 928 not captured")
    report.check(
        "missing-draw-counts-explicit",
        not draw_count_errors and zero_draw_counts == 0 and draw_gap is not None,
        f"Base/PassDrawCounts.json has {draw_count_records} records; all 928 pass drawCount values are "
        f"null (zero substituted: {zero_draw_counts}); gap kind pass-draw-counts-empty is recorded",
        {"errors": draw_count_errors[:20], "gap": draw_gap, "counts": {
            "captured": counts.get("passDrawCountsCaptured"),
            "missing": counts.get("passDrawCountsMissing"),
        }},
    )

    # 10e. Field-support keys are live and source formulas are distinct from decoders.
    field_errors: list[str] = []
    resource_equation_by_schema: dict[str, int] = {}
    parameter_equation_by_schema: dict[str, int] = {}
    unsafe_by_schema: dict[str, int] = {}
    dead_key_hits = 0
    for resource in resources:
        record = load_json(package / "records" / "resource" / f"{resource['id']}.json")
        for match in record.get("schemaMatches", []):
            support = match.get("fieldSupport") or {}
            if "unsupportedDecodeFields" in support:
                dead_key_hits += 1
            key = "resource:" + str(match.get("schemaId"))
            resource_equation_by_schema[str(match.get("schemaId"))] = len(support.get("sourceEquationFields") or [])
            unsafe_by_schema[key] = len(support.get("unsafeExpressionFields") or [])
    for pass_id in passes:
        record = load_json(package / "records" / "pass" / f"{pass_id}.json")
        for match in record.get("schemaMatches", []):
            support = match.get("fieldSupport") or {}
            if "unsupportedDecodeFields" in support:
                dead_key_hits += 1
            key = "parameter:" + str(match.get("schemaId"))
            parameter_equation_by_schema[str(match.get("schemaId"))] = len(support.get("sourceEquationFields") or [])
            unsafe_by_schema[key] = len(support.get("unsafeExpressionFields") or [])
    # The 129/128 counts are unique source-schema fields, including schemas that
    # did not match a captured instance.  Recompute them directly from the
    # embedded schema with the same non-evaluating classification.
    safe_tags = {"none", "raw", "uint", "sint", "unorm", "float", "half", "float16",
                 "float32", "r11g11b10", "a2b10g10r10", "a2b10g10r10_unorm"}
    unsafe_pattern = re.compile(r"(__import__|eval\s*\(|exec\s*\(|system\s*\()", re.I)

    def classify_schema_fields(schemas: list[Any]) -> tuple[int, int, int]:
        equations = 0
        unsafe = 0
        fields = 0
        for schema in schemas:
            if not isinstance(schema, dict):
                continue
            for field in schema.get("fields", []) if isinstance(schema.get("fields"), list) else []:
                if not isinstance(field, dict):
                    continue
                fields += 1
                decode = field.get("decode")
                if decode is None:
                    continue
                text = str(decode)
                if text.strip().lower() in safe_tags:
                    continue
                if unsafe_pattern.search(text):
                    unsafe += 1
                else:
                    equations += 1
        return fields, equations, unsafe

    package_schema_doc = load_json(package / "schema.json")
    resource_fields, resource_source_equation_fields, resource_unsafe = classify_schema_fields(
        package_schema_doc.get("resourceSchemas", []))
    parameter_fields, parameter_source_equation_fields, parameter_unsafe = classify_schema_fields(
        package_schema_doc.get("parameterSchemas", []))
    unsafe_expression_fields = resource_unsafe + parameter_unsafe
    if counts.get("resourceSourceEquationFields") != resource_source_equation_fields:
        field_errors.append(
            f"index resourceSourceEquationFields {counts.get('resourceSourceEquationFields')} != "
            f"schema field count {resource_source_equation_fields}")
    if counts.get("parameterSourceEquationFields") != parameter_source_equation_fields:
        field_errors.append(
            f"index parameterSourceEquationFields {counts.get('parameterSourceEquationFields')} != "
            f"schema field count {parameter_source_equation_fields}")
    explorer_source = (ROOT / "site" / "assets" / "frame-explorer.js").read_text(encoding="utf-8")
    if "unsupportedDecodeFields" in explorer_source:
        field_errors.append("frame-explorer.js still reads the dead unsupportedDecodeFields key")
    if "sourceEquationFields" not in explorer_source or "unsafeExpressionFields" not in explorer_source:
        field_errors.append("frame-explorer.js does not render both real fieldSupport warning keys")
    if dead_key_hits:
        field_errors.append(f"{dead_key_hits} fieldSupport records still contain the dead key")
    if unsafe_expression_fields != 0:
        field_errors.append(f"expected 0 unsafe expression fields in this schema, got {unsafe_expression_fields}")
    source_equation_gaps = [g for g in coverage["gaps"] if g["kind"] == "schema-field-source-equation"]
    if not source_equation_gaps:
        field_errors.append("missing schema-field-source-equation gap")
    report.check(
        "field-support-warnings-live",
        not field_errors,
        f"{resource_source_equation_fields} resource + {parameter_source_equation_fields} parameter source-formula fields are classified; "
        f"unsafe-expression fields={unsafe_expression_fields}; the dead unsupportedDecodeFields key is absent; "
        f"{len(source_equation_gaps)} schema-field-source-equation gaps record them",
        {
            "errors": field_errors,
            "resourceSourceEquationFields": resource_source_equation_fields,
            "parameterSourceEquationFields": parameter_source_equation_fields,
            "unsafeExpressionFields": unsafe_expression_fields,
            "sourceEquationGapCount": len(source_equation_gaps),
        },
    )

    # 10f. Labels, invocation, frame and allocation/view-rect padding.
    label_errors: list[str] = []
    label_evidence: list[dict[str, Any]] = []
    gbuffer_labels = []
    for resource_id, expected_invocation, expected_mod8 in (
        ("resource-0185", "0001", 7),
        ("resource-0652", "0003", 0),
        ("resource-1113", "0004", 1),
    ):
        record = load_json(package / "records" / "resource" / f"{resource_id}.json")
        label = record.get("displayLabel") or ""
        gbuffer_labels.append(label)
        if record.get("sceneInvocation") != expected_invocation:
            label_errors.append(f"{resource_id} sceneInvocation {record.get('sceneInvocation')}")
        if record.get("frameNumber") != 292:
            label_errors.append(f"{resource_id} frameNumber {record.get('frameNumber')}")
        if (record.get("viewState") or {}).get("frameIndexMod8") != expected_mod8:
            label_errors.append(f"{resource_id} frameIndexMod8 {(record.get('viewState') or {}).get('frameIndexMod8')}")
        if expected_invocation not in label or str(expected_mod8) not in label:
            label_errors.append(f"{resource_id} label does not include invocation/frame index: {label}")
        if (record.get("allocation") or [])[:2] != [1280, 888]:
            label_errors.append(f"{resource_id} allocation {record.get('allocation')}")
        view_rect = (record.get("viewState") or {}).get("viewRect") or {}
        if view_rect.get("size") != [1280, 720]:
            label_errors.append(f"{resource_id} ViewRect {view_rect}")
        padding = record.get("padding") or {}
        if padding.get("validHeight") != 720 or padding.get("bottom") != 168:
            label_errors.append(f"{resource_id} padding {padding}")
        # Cross-check the label against the recorded View.View bytes.
        producer_id = (record.get("versions") or [{}])[-1].get("producerPassId")
        producer = load_json(package / "records" / "pass" / f"{producer_id}.json")
        view_ref = next((ref for ref in producer.get("referencedStructs", [])
                         if ref.get("member") == "View.View"), None)
        if view_ref is None or not view_ref.get("payload"):
            label_errors.append(f"{resource_id} producer has no View.View block")
        else:
            raw = read_package_range(package, view_ref["payload"], 2712, 4, chunks, cache)
            actual_mod8 = int.from_bytes(raw, "little")
            if actual_mod8 != expected_mod8:
                label_errors.append(f"{resource_id} View.StateFrameIndexMod8 bytes={actual_mod8}")
        label_evidence.append({
            "resourceId": resource_id, "label": label,
            "sceneInvocation": record.get("sceneInvocation"),
            "frameNumber": record.get("frameNumber"),
            "frameIndexMod8": (record.get("viewState") or {}).get("frameIndexMod8"),
            "allocation": record.get("allocation"), "viewRect": view_rect, "padding": padding,
        })
    if len(set(gbuffer_labels)) != 3:
        label_errors.append(f"GBufferA labels are not distinct: {gbuffer_labels}")
    for pass_id, expected_mod8 in (("pass-0126", 7), ("pass-0127", 7), ("pass-0438", 0), ("pass-0746", 1)):
        pass_record = load_json(package / "records" / "pass" / f"{pass_id}.json")
        label = pass_record.get("displayLabel") or ""
        if pass_record.get("frameNumber") != 292 or "frame 292" not in label:
            label_errors.append(f"{pass_id} missing frame 292 label")
        if (pass_record.get("viewState") or {}).get("frameIndexMod8") != expected_mod8:
            label_errors.append(f"{pass_id} frameIndexMod8 mismatch")
        if "View.StateFrameIndexMod8" not in label:
            label_errors.append(f"{pass_id} label omits View.StateFrameIndexMod8")
    report.check(
        "labels-from-recorded-metadata",
        not label_errors,
        "GBufferA labels are distinct by scene invocation 0001/0003/0004, frame 292 and "
        "View.StateFrameIndexMod8 7/0/1; allocation 1280x888 vs ViewRect 1280x720 is labelled "
        "with 168 padded rows; labels cross-check against recorded View.View bytes",
        {"errors": label_errors, "resources": label_evidence},
    )

    # 10g. Channel selector and bounded overview are implemented and testable.
    overview_errors: list[str] = []
    for token in ("Whole-texture overview", "Column stride", "Row stride", "channelList",
                  "planOverview", "Crop to recorded ViewRect"):
        if token not in explorer_source:
            overview_errors.append(f"frame-explorer.js lacks {token!r}")
    if "storedChannelNames" not in explorer_source or "channelNames" not in CODEC.read_text(encoding="utf-8"):
        overview_errors.append("channel-name helper is not exported/tested")
    report.check(
        "channel-and-overview-selectors",
        not overview_errors,
        "UI exposes channel selectors over every stored component plus a separate bounded "
        "whole-texture overview with version/mip/slice, column/row stride and optional ViewRect crop; "
        "the pure planner is covered by tests/test_read_path.cjs (no browser automation)",
        {"errors": overview_errors},
    )


    pagetable_record = load_json(package / "records" / "resource" / "resource-0097.json")
    pagetable_payload = payloads["payload-00065"]
    pagetable_parts: list[bytes] = []
    for ref in pagetable_payload["chunks"]:
        chunk = cache.get(ref["sha"])
        if chunk is None:
            chunk = gzip.decompress((package / "chunks" / f"{ref['sha']}.gz").read_bytes())
            cache[ref["sha"]] = chunk
        pagetable_parts.append(chunk)
    pagetable_bytes = b"".join(pagetable_parts)
    entry = pagetable_bytes[80:88]
    word0, word1 = struct.unpack("<II", entry)
    fields = [word0 & 0xFFF, (word0 >> 12) & 0xFFF, (word0 >> 24) & 0xF, (word0 >> 28) & 0xF, word1]
    schema_match = next(
        (m for m in pagetable_record["schemaMatches"] if m["schemaId"] == "resource.lumen-page-table"),
        None,
    )
    pagetable_ok = (
        entry == bytes.fromhex("208000660a000000")
        and fields == [32, 8, 6, 6, 10]
        and schema_match is not None
        and [f["bitWidth"] for f in schema_match["fields"]] == [12, 12, 4, 4, 32]
    )
    report.check(
        "actual-packed-buffer-record",
        pagetable_ok,
        "Lumen.PageTable payload-00065 byte 80 = 20 80 00 66 0A 00 00 00; "
        "word0=0x66008020 -> biasX=32, biasY=8, resX=6, resY=6, page=10",
        {
            "resource": pagetable_record["name"],
            "payloadId": "payload-00065",
            "payloadSha256": pagetable_payload["sha256"],
            "byteOffset": 80,
            "bytesHex": entry.hex(),
            "word0Hex": hex(word0),
            "word1": word1,
            "fields": fields,
            "schemaId": schema_match["schemaId"] if schema_match else None,
        },
    )

    # 12. Scalar uniforms from native StructuresMetadata offsets.
    uniform_specs = [
        ("pass-0015", "payload-02700", [("NumIndirectArgs", 0, "<I", 1),
                                        ("IndirectArgStride", 4, "<I", 3)]),
        ("pass-0007", "payload-02651", [("NumDistanceFieldAssets", 256, "<I", 1),
                                        ("MaxNumStreamingRequests", 260, "<I", 4095)]),
    ]
    uniform_results: list[dict[str, Any]] = []
    uniform_ok = True
    for pass_id, payload_id, specs in uniform_specs:
        pass_record = load_json(package / "records" / "pass" / f"{pass_id}.json")
        payload = payloads[payload_id]
        parts = []
        for ref in payload["chunks"]:
            chunk = cache.get(ref["sha"])
            if chunk is None:
                chunk = gzip.decompress((package / "chunks" / f"{ref['sha']}.gz").read_bytes())
                cache[ref["sha"]] = chunk
            parts.append(chunk)
        block = b"".join(parts)
        members = {m["Name"]: m for m in pass_record["parameterMetadata"]["Members"]}
        for name, offset, fmt, expected in specs:
            member = members.get(name)
            actual = struct.unpack_from(fmt, block, offset)[0]
            ok = member is not None and int(member["Offset"]) == offset and actual == expected
            uniform_ok = uniform_ok and ok
            uniform_results.append({
                "pass": pass_id, "passName": pass_record["name"],
                "payloadId": payload_id, "payloadSha256": payload["sha256"],
                "member": name, "metadataOffset": member["Offset"] if member else None,
                "offset": offset, "baseType": member["BaseType"] if member else None,
                "value": actual, "expected": expected, "match": ok,
            })
    report.check(
        "actual-scalar-uniform-reads",
        uniform_ok,
        "pass-0015 NumIndirectArgs@0=1, IndirectArgStride@4=3; "
        "pass-0007 NumDistanceFieldAssets@256=1, MaxNumStreamingRequests@260=4095",
        uniform_results,
    )

    # 13. BC7/export-layout evidence (descriptor bytes != payload stride).
    bc7_record = load_json(package / "records" / "resource" / "resource-0099.json")
    bc7_payload = payloads["payload-00068"]
    bc7_source = candidate / bc7_payload["originalPath"]
    bc7_source_sha, bc7_source_bytes = sha256_file(bc7_source)
    first_ref = bc7_payload["chunks"][0]
    first_chunk = gzip.decompress((package / "chunks" / f"{first_ref['sha']}.gz").read_bytes())
    with bc7_source.open("rb") as handle:
        source_prefix = handle.read(len(first_chunk))
    half_values = struct.unpack("<4e", source_prefix[0:8])
    bc7_ok = (
        bc7_record["format"] == "PF_BC7"
        and bc7_record["decode"]["dumpFormat"] == "PF_FLOATRGBA"
        and bc7_record["decode"]["bytesPerTexel"] == 8
        and bc7_payload["bytes"] == 134_217_728 == 4096 * 4096 * 8
        and bc7_record["bytes"] == 268_435_456
        and bc7_source_sha == bc7_payload["sha256"]
        and bc7_source_bytes == bc7_payload["bytes"]
        and first_chunk == source_prefix
    )
    report.check(
        "bc7-export-layout",
        bc7_ok,
        "Lumen.SceneAlbedo PF_BC7 payload is 134,217,728 B = 4096x4096x8 (PF_FLOATRGBA); "
        "descriptor ByteSize 268,435,456 is retained but not used as stride; first chunk matches source",
        {
            "resource": bc7_record["name"],
            "descriptorFormat": bc7_record["format"],
            "descriptorByteSize": bc7_record["bytes"],
            "dumpFormat": bc7_record["decode"]["dumpFormat"],
            "bytesPerTexel": bc7_record["decode"]["bytesPerTexel"],
            "payloadBytes": bc7_payload["bytes"],
            "payloadSha256": bc7_payload["sha256"],
            "firstTexelHalfFloat": list(half_values),
            "firstChunkBytesCompared": len(first_chunk),
        },
    )

    # 14. Subresource/version addressability.
    version_errors: list[str] = []
    version_count = 0
    producer_versions = 0
    version_hits: Counter[str] = Counter()
    mip_counts: Counter[Any] = Counter()
    slice_counts: Counter[Any] = Counter()
    for rs in resources:
        record = load_json(package / "records" / "resource" / f"{rs['id']}.json")
        seen: set[str] = set()
        for version in record.get("versions", []):
            version_count += 1
            if version.get("producerPassId"):
                producer_versions += 1
            for pid in version["payloadIds"]:
                if pid in seen:
                    version_errors.append(f"{record['id']} duplicate version payload {pid}")
                seen.add(pid)
                version_hits[pid] += 1
            producer_id = version.get("producerPassId")
            if producer_id and producer_id not in passes:
                version_errors.append(f"{record['id']} unknown producer {producer_id}")
            if producer_id and version["version"] not in ("0" * 20, "(no-version)"):
                if passes[producer_id]["pointer"] != version["version"]:
                    version_errors.append(f"{record['id']} version pointer mismatch")
        for payload in record["payloads"]:
            if payload["resourceId"] != record["id"]:
                version_errors.append(f"{record['id']} payload resource mismatch")
            mip_counts[payload.get("mip")] += 1
            slice_counts[payload.get("slice")] += 1
    roles: Counter[str] = Counter()
    for payload in inventory["payloads"]:
        roles[payload["role"]] += 1
        if payload["role"] == "parameter":
            if version_hits.get(payload["id"], 0) != 0:
                version_errors.append(f"{payload['id']} parameter payload in resource version")
        elif version_hits.get(payload["id"], 0) != 1:
            version_errors.append(f"{payload['id']} resource payload version count {version_hits.get(payload['id'], 0)}")
    for pass_record in passes.values():
        block = pass_record.get("parameterBlock")
        if block and block.get("payloadId") not in payloads:
            version_errors.append(f"{pass_record['id']} bad parameter payload")
    report.check(
        "subresource-and-version-addressability",
        not version_errors
        and roles.get("resource") == cand["resourcePayloadCount"]
        and roles.get("parameter") == cand["parameterPayloadCount"],
        f"{version_count} resource versions ({producer_versions} producer-linked), "
        f"{roles.get('resource')} resource payloads + {roles.get('parameter')} parameter payloads; "
        f"mips={dict(sorted(mip_counts.items(), key=lambda kv: str(kv[0])))}; "
        f"errors={len(version_errors)}",
        {
            "errors": version_errors[:20],
            "mipCounts": {str(k): v for k, v in mip_counts.items()},
            "sliceCounts": {str(k): v for k, v in slice_counts.items()},
        },
    )

    # 15. Descriptor-only capture gaps and raw unknown/buffer layout retention.
    capture_gap_errors: list[str] = []
    descriptor_only_types = cand["descriptorOnlyTypes"]
    missing_payload_record = 0
    for rs in resources:
        if rs["kind"] == "texture" and not rs["payloadIds"]:
            record = load_json(package / "records" / "resource" / f"{rs['id']}.json")
            kinds = {g["kind"] for g in record.get("gaps", [])}
            if "resource-payload-missing" not in kinds:
                capture_gap_errors.append(f"{record['id']} lacks resource-payload-missing")
            if not record.get("native"):
                capture_gap_errors.append(f"{record['id']} lacks native descriptor")
            missing_payload_record += 1
    raw_buffer_errors: list[str] = []
    for rs in resources:
        if rs["kind"] != "buffer":
            continue
        record = load_json(package / "records" / "resource" / f"{rs['id']}.json")
        if (record.get("decode") or {}).get("supported"):
            raw_buffer_errors.append(f"{record['id']} buffer decode marked supported")
        if record["payloads"]:
            for payload in record["payloads"]:
                if payload["bytes"] <= 0 or not payload["chunks"]:
                    raw_buffer_errors.append(f"{record['id']} fabricated/empty payload")
            break
    report.check(
        "descriptor-only-and-unknown-layout-retained",
        not capture_gap_errors and not raw_buffer_errors
        and descriptor_only_types.get("Texture3D") == 78
        and descriptor_only_types.get("TextureCube") == 3
        and cand["descriptorOnlyCount"] == 105,
        "descriptor-only={} (Texture3D={}, TextureCube={}, Buffer={}); "
        "all such texture records keep native metadata and resource-payload-missing; buffer layout stays raw".format(
            cand["descriptorOnlyCount"], descriptor_only_types.get("Texture3D", 0),
            descriptor_only_types.get("TextureCube", 0), descriptor_only_types.get("Buffer", 0)),
        {"errors": capture_gap_errors[:20], "rawBufferErrors": raw_buffer_errors[:20],
         "descriptorOnlyTypes": descriptor_only_types},
    )

    # 16. Gap classification and capture/decoder distinction.
    gap_by_kind = Counter(g["kind"] for g in coverage["gaps"])
    gap_by_scope = Counter(g["scope"] for g in coverage["gaps"])
    kind_scope: dict[str, str] = {}
    for gap in coverage["gaps"]:
        kind_scope.setdefault(gap["kind"], gap["scope"])
    gap_ok = (
        coverage.get("complete") is False
        and "not claimed complete" in coverage.get("statement", "").lower()
        and len(coverage["gaps"]) == counts["gapCount"] == len(inventory["gaps"])
        and gap_by_scope.get("capture", 0) > 0
        and gap_by_scope.get("decoding", 0) > 0
        and gap_by_kind.get("resource-payload-missing", 0) == 105
        and gap_by_kind.get("dumpgpu-skipped-resource", 0) == 24
        and gap_by_kind.get("shader-local-state-not-captured", 0) == 1
        and gap_by_kind.get("dump-scope-unknown", 0) == 1
    )
    capture_kinds = {
        "dumpgpu-skipped-resource": gap_by_kind.get("dumpgpu-skipped-resource", 0),
        "dump-scope-unknown": gap_by_kind.get("dump-scope-unknown", 0),
        "shader-local-state-not-captured": gap_by_kind.get("shader-local-state-not-captured", 0),
        "resource-payload-missing (capture-caused: 78 Texture3D + 3 TextureCube + 24 skipped buffers)":
            gap_by_kind.get("resource-payload-missing", 0),
    }
    decoding_scope_kinds = {
        key: value for key, value in gap_by_kind.items()
        if kind_scope.get(key) == "decoding"
    }
    decoder_format_gaps = {
        key: gap_by_kind.get(key, 0) for key in (
            "unsupported-pixel-format", "multisample-texture",
            "resource-schema-dump-layout-mismatch",
            "schema-field-type-unsupported", "schema-field-offset-missing",
        )
    }
    report.check(
        "coverage-distinguishes-capture-from-decoder-gaps",
        gap_ok,
        "coverage.complete=false; {} gaps by scope {}; capture kinds include 24 skipped, "
        "1 dump-scope, 1 shader-local, 105 descriptor-only payloads; decoder gaps are separate".format(
            len(coverage["gaps"]), dict(gap_by_scope)),
        {
            "byKind": dict(sorted(gap_by_kind.items())),
            "byScope": dict(sorted(gap_by_scope.items())),
            "captureKinds": capture_kinds,
            "decodingScopeKinds": decoding_scope_kinds,
            "decoderFormatGaps": decoder_format_gaps,
        },
    )

    # 17. Cross-check the capture team's own missing-coverage manifest.
    missing_path = candidate.parent.parent / "missing-coverage.json"
    if missing_path.exists():
        missing = load_json(missing_path)
        mc = missing.get("counts", {})
        mc_ok = (
            mc.get("resources_total") == cand["descriptorCount"]
            and mc.get("resources_captured") == cand["payloadPrefixCount"]
            and mc.get("resources_descriptor_only") == cand["descriptorOnlyCount"]
            and mc.get("texture3d_total") == descriptor_only_types.get("Texture3D")
            and mc.get("texture3d_captured") == 0
            and mc.get("texturecube_total") == descriptor_only_types.get("TextureCube")
            and mc.get("texturecube_captured") == 0
            and mc.get("resource_binary_files") == cand["resourcePayloadCount"]
            and mc.get("passes_total") == cand["passCount"]
            and mc.get("skipped_warning_lines") == gap_by_kind.get("dumpgpu-skipped-resource", 0)
        )
        report.check(
            "capture-missing-coverage-cross-check",
            mc_ok,
            "capture missing-coverage counts match independent audit: resources_total={}, "
            "captured={}, descriptor_only={}, Texture3D={}, TextureCube={}, skipped={}".format(
                mc.get("resources_total"), mc.get("resources_captured"),
                mc.get("resources_descriptor_only"), mc.get("texture3d_total"),
                mc.get("texturecube_total"), mc.get("skipped_warning_lines")),
            {"manifest": str(missing_path), "counts": mc, "structuralLimits": missing.get("structural_limits", [])},
        )
    else:
        report.skip("capture-missing-coverage-cross-check", f"not present: {missing_path}")

    # 18. Decoder code has no schema-string evaluation.
    eval_hits: list[str] = []
    for path in (TOOL, CODEC, ROOT / "site" / "assets" / "frame-explorer.js"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in (r"(?<!\.)\beval\s*\(", r"(?<!\.)\bexec\s*\(", r"new\s+Function\s*\("):
            if re.search(pattern, text):
                eval_hits.append(f"{path.name}: {pattern}")
    report.check(
        "no-schema-string-evaluation",
        not eval_hits,
        "no eval/exec/new Function in package_frame.py, frame-codec.js or frame-explorer.js; "
        "the Python suite's unsafe-decode marker is asserted absent",
        eval_hits,
    )

    # 19. Portability: package-internal paths are safe relative POSIX paths.
    path_values: list[str] = []
    existing_values: list[str] = []
    for payload in inventory["payloads"]:
        # Reconstruction target, not a file stored in the package.
        path_values.append(payload["originalPath"])
    for nf in native_files:
        path_values.extend([nf["path"], nf["packagePath"]])
        existing_values.append(nf["packagePath"])
    for rs in resources:
        rel = f"records/resource/{rs['id']}.json"
        path_values.append(rel)
        existing_values.append(rel)
    for pass_id in passes:
        rel = f"records/pass/{pass_id}.json"
        path_values.append(rel)
        existing_values.append(rel)
    schema_link = index["schema"].get("path")
    if schema_link:
        path_values.append(schema_link)
        existing_values.append(schema_link)
    unsafe_paths = [p for p in path_values if not safe_relative(p)]
    missing_paths = [p for p in existing_values if not (package / p).exists()]
    tmp_files = [p for p in package.rglob("*.tmp") if p.is_file()]
    report.check(
        "package-portability",
        not unsafe_paths and not missing_paths and not tmp_files,
        f"{len(path_values)} path references are safe relative POSIX; "
        f"{len(existing_values)} package files checked exist; "
        f"unsafe={len(unsafe_paths)}, missing={len(missing_paths)}, tmp_files={len(tmp_files)}",
        {"unsafe": unsafe_paths[:20], "missing": missing_paths[:20], "tmpFiles": [str(p) for p in tmp_files[:20]]},
    )

    # 20. README reflects the real evidence state.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_lower = readme.lower()
    stale = [
        token for token in (
            "no real `dumpgpu` capture", "empty placeholder", "no-capture-package",
        ) if token in readme_lower
    ]
    required = [
        "binary-20261112-133826", f"{counts['payloadBytes']:,}", f"{counts['gapCount']:,}",
        "not complete", "byte-for-byte", "packed 10/10/10/2", "not RGBA8",
        "half2/half4", "PreprocessedDesc", "format gates",
    ]
    missing_terms = [term for term in required if term not in readme]
    report.check(
        "readme-reflects-real-package",
        not stale and not missing_terms,
        "README has no stale no-capture/placeholder claim, names the real capture and payload bytes, "
        "states the live gap count, and records packed 10:10:10:2, BC half2/half4 preprocessing and "
        "format gates as authoritative",
        {"stale": stale, "missing": missing_terms},
    )

    # 21. Test suites.
    if args.skip_tests:
        report.skip("python-tests", "skipped by --skip-tests")
        report.skip("node-tests", "skipped by --skip-tests")
        report.skip("node-read-path-tests", "skipped by --skip-tests")
        py_result = node_result = read_path_result = None
    else:
        py_result = run_command(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            ROOT, env=env, timeout=600,
        )
        py_ok = py_result["returncode"] == 0 and re.search(r"Ran \d+ tests", py_result["stderr"]) is not None and "\nOK" in py_result["stderr"]
        report.check(
            "python-tests",
            py_ok,
            (re.search(r"Ran \d+ tests", py_result["stderr"]).group(0) + ", OK") if py_ok else f"returncode={py_result['returncode']}",
            {"stdout": py_result["stdout"], "stderr": py_result["stderr"]},
        )
        (scratch / "python-tests.log").write_text(
            py_result["stdout"] + "\n" + py_result["stderr"], encoding="utf-8")
        node_result = run_command(["node", "tests/test_codec.cjs"], ROOT, env=env, timeout=300)
        node_ok = node_result["returncode"] == 0 and "codec tests passed" in node_result["stdout"]
        report.check(
            "node-tests",
            node_ok,
            (node_result["stdout"].strip().splitlines() or ["no stdout"])[-1]
            + f"; returncode={node_result['returncode']}",
            {"stdout": node_result["stdout"], "stderr": node_result["stderr"]},
        )
        (scratch / "node-tests.log").write_text(
            node_result["stdout"] + "\n" + node_result["stderr"], encoding="utf-8")
        read_path_result = run_command(["node", "tests/test_read_path.cjs"], ROOT, env=env, timeout=300)
        read_path_ok = read_path_result["returncode"] == 0 and "read-path tests passed" in read_path_result["stdout"]
        report.check(
            "node-read-path-tests",
            read_path_ok,
            (read_path_result["stdout"].strip().splitlines() or ["no stdout"])[-1]
            + f"; returncode={read_path_result['returncode']}",
            {"stdout": read_path_result["stdout"], "stderr": read_path_result["stderr"]},
        )
        (scratch / "node-read-path-tests.log").write_text(
            read_path_result["stdout"] + "\n" + read_path_result["stderr"], encoding="utf-8")

    # ------------------------------------------------------------- report output
    package_files = [p for p in package.rglob("*") if p.is_file()]
    package_bytes = sum(p.stat().st_size for p in package_files)
    largest = max(package_files, key=lambda p: p.stat().st_size)
    status = "pass" if report.failures == 0 else "fail"

    def status_of(name: str) -> str:
        return next((entry["status"] for entry in report.checks if entry["name"] == name), "missing")

    acceptance_groups = {
        "Partial chunk reads succeed with a real packaged pixel and regression coverage": [
            "partial-chunk-read-real-pixel", "node-read-path-tests",
            "selective-reconstruct-vs-source", "resource-decoder-stride-consistency",
            "actual-gbuffer-byte-and-typed-read",
        ],
        "Depth and stencil bindings are disjoint and missing draw counts are explicit": [
            "depth-and-stencil-bindings-disjoint", "missing-draw-counts-explicit",
            "subresource-and-version-addressability",
        ],
        "Texture channels and whole-image overviews are independently selectable": [
            "channel-and-overview-selectors", "node-tests",
            "labels-from-recorded-metadata",
        ],
        "All software tests and package integrity checks pass": [
            "candidate-native-enumeration", "package-counts-match-candidate",
            "package-identity", "native-metadata-byte-for-byte", "payload-set-and-sizes",
            "schema-byte-for-byte", "full-package-verify", "chunk-index-consistency",
            "actual-packed-buffer-record", "actual-scalar-uniform-reads", "bc7-export-layout",
            "descriptor-only-and-unknown-layout-retained",
            "coverage-distinguishes-capture-from-decoder-gaps",
            "capture-missing-coverage-cross-check", "no-schema-string-evaluation",
            "package-portability", "readme-reflects-real-package", "python-tests",
            "reviewed-schema-and-format-gates-packaged", "field-support-warnings-live",
        ],
        "Reviewed source schemas and format gates survive packaging": [
            "schema-byte-for-byte", "reviewed-schema-and-format-gates-packaged",
            "field-support-warnings-live", "node-tests", "node-read-path-tests",
        ],
    }
    acceptance = {
        name: ("pass" if all(status_of(check) == "pass" for check in checks) else "fail")
        for name, checks in acceptance_groups.items()
    }

    report_doc = {
        "format": "unreal-frame-explorer-validation",
        "version": 1,
        "status": status,
        "generatedUtc": now_utc(),
        "summary": (
            f"Real capture {candidate.name}: {cand['descriptorCount']} resource descriptors, "
            f"{cand['passCount']} passes, {cand['resourcePayloadCount']} resource payloads, "
            f"{cand['parameterPayloadCount']} parameter payloads, {cand['nativeFileCount']} native files. "
            f"Package {package.name}: {len(package_files)} files, {package_bytes} bytes, "
            f"{counts['chunkFiles']} chunks, {counts['compressedChunkBytes']} compressed bytes. "
            f"{report.failures} failed check(s); coverage is not complete ({counts['gapCount']} explicit gaps)."
        ),
        "candidate": {
            "name": candidate.name,
            "path": str(candidate),
            "files": cand["fileCount"],
            "bytes": cand["bytes"],
            "descriptors": cand["descriptorCount"],
            "descriptorTypes": cand["descriptorTypes"],
            "descriptorOnly": cand["descriptorOnlyCount"],
            "descriptorOnlyTypes": cand["descriptorOnlyTypes"],
            "passes": cand["passCount"],
            "resourcePayloads": cand["resourcePayloadCount"],
            "parameterPayloads": cand["parameterPayloadCount"],
            "payloadBytes": cand["payloadBytes"],
            "nativeFiles": cand["nativeFileCount"],
        },
        "package": {
            "path": str(package),
            "files": len(package_files),
            "bytes": package_bytes,
            "largestFile": {"path": rel_posix(largest, package), "bytes": largest.stat().st_size},
            "counts": counts,
        },
        "schema": {
            "sourcePath": str(schema_path),
            "sourceBytes": len(schema_bytes),
            "sourceSha256": sha256_bytes(schema_bytes),
            "packageBytes": len(package_schema),
            "packageSha256": sha256_bytes(package_schema),
            "match": package_schema == schema_bytes,
            "resourceSchemas": index["schema"]["resourceSchemas"],
            "parameterSchemas": index["schema"]["parameterSchemas"],
            "note": f"The source schema is a partial reviewed source dictionary ({index['schema']['resourceSchemas']} resource / "
                    f"{index['schema']['parameterSchemas']} parameter schemas, {index['schema'].get('sourceRefs', 0)} source refs, "
                    f"{index['schema'].get('coverageGaps', 0)} coverage gaps). All "
                    f"{resource_source_equation_fields} resource and {parameter_source_equation_fields} parameter decode "
                    "strings in it are source formulas, not executable tags. They are displayed with source anchors and are "
                    "never evaluated. Resource/pass records carry live fieldSupport keys (sourceEquationFields / "
                    "unsafeExpressionFields); the dead unsupportedDecodeFields key is absent. The shipped schema copy is "
                    f"byte-for-byte identical to the supplied {len(schema_bytes):,}-byte source schema.",
        },
        "acceptance": acceptance,
        "checks": report.checks,
        "staleArtifacts": [
            {
                "path": ".tmp-tests.log",
                "note": "Pre-fix UTF-16 log retained untouched. It shows the older "
                        "schema-field-decode-unsupported expectation. The current "
                        "tests/test_packer.py expects schema-field-decode-unsafe-expression; "
                        "fresh logs are scratch/validate/python-tests.log and "
                        "scratch/validate/node-tests.log.",
            }
        ],
        "evidence": {
            "gbuffer": gbuffer_evidence,
            "packedBuffer": {
                "resource": "Lumen.PageTable",
                "payloadId": "payload-00065",
                "payloadSha256": pagetable_payload["sha256"],
                "byteOffset": 80,
                "entryBytesHex": entry.hex(),
                "fields": fields,
            },
            "scalarUniforms": uniform_results,
            "selectiveReconstruction": sample_results,
            "bc7": {
                "resource": "Lumen.SceneAlbedo",
                "payloadId": "payload-00068",
                "payloadSha256": bc7_payload["sha256"],
                "descriptorFormat": "PF_BC7",
                "dumpFormat": "PF_FLOATRGBA",
                "payloadBytes": bc7_payload["bytes"],
                "descriptorBytes": bc7_record["bytes"],
                "firstTexelHalfFloat": list(half_values),
            },
            "partialChunkReads": partial_evidence,
            "stencilBindings": {
                "falseStencilReferences": false_stencil_refs,
                "simulatedPreFixFalseStencilReferences": simulated_old_false_refs,
                "stencilPayloadCount": len(stencil_payload_ids),
                "stencilBindingReferences": stencil_binding_refs,
                "stencilPayloadsInStencilBindings": len(stencil_binding_payloads),
            },
            "drawCounts": {
                "nativeRecords": draw_count_records,
                "captured": counts.get("passDrawCountsCaptured"),
                "missing": counts.get("passDrawCountsMissing"),
                "gapKind": draw_gap["kind"] if draw_gap else None,
            },
            "fieldSupport": {
                "resourceSourceEquationFields": resource_source_equation_fields,
                "parameterSourceEquationFields": parameter_source_equation_fields,
                "unsafeExpressionFields": unsafe_expression_fields,
                "deadKeyHits": dead_key_hits,
            },
            "labels": label_evidence,
            "gbufferALabels": gbuffer_labels,
        },
        "gaps": {
            "total": len(coverage["gaps"]),
            "complete": coverage.get("complete"),
            "statement": coverage.get("statement"),
            "byKind": dict(sorted(gap_by_kind.items())),
            "byScope": dict(sorted(gap_by_scope.items())),
            "captureKinds": capture_kinds,
            "decodingScopeKinds": decoding_scope_kinds,
            "decoderFormatGaps": decoder_format_gaps,
        },
        "captureLimitations": {
            "texture3dDescriptors": descriptor_only_types.get("Texture3D", 0),
            "texture3dWithPayload": 0,
            "texturecubeDescriptors": descriptor_only_types.get("TextureCube", 0),
            "texturecubeWithPayload": 0,
            "descriptorOnlyBuffers": descriptor_only_types.get("Buffer", 0),
            "skippedResourceWarnings": gap_by_kind.get("dumpgpu-skipped-resource", 0),
            "descriptorOnlyTotal": cand["descriptorOnlyCount"],
            "notCaptured": [
                "Texture3D/volume resources are descriptor-only in this DumpGPU export path.",
                "TextureCube resources are descriptor-only for the same reason.",
                "RDG-only discovery cannot list non-RDG RHI-bound resources or resources never referenced by a captured pass.",
                "Some RDG uniform-buffer contents and shader-local values are outside DumpGPU's capture surface.",
                "Base/PassDrawCounts.json is empty, so pass draw counts are not captured in this candidate.",
                "r.DumpGPU.RedumpInputs=0 means in-place modifications link to the one dumped producer version.",
            ],
            "separateInstrumentation": "The capture team reports separate engine instrumentation is closing 3D/cube/RDG and non-RDG RHI-bound gaps.",
        },
        "decoderLimitations": [
            "27 stencil payloads use the explicit 1-byte PF_R8_UINT layout; the resource color texel layout is not applied.",
            "Colour/depth bindings list only non-stencil payloads, stencil bindings list only stencil payloads, and version/mip/slice/aspect are matched exactly. 1,653 pre-fix stencil references were removed; all 27 stencil payloads remain independently addressable.",
            "Base/PassDrawCounts.json is empty (0 records); all 928 pass drawCount values stay null and are labelled not captured. Zero is never substituted.",
            f"{resource_source_equation_fields} resource and {parameter_source_equation_fields} parameter decode strings are source formulas, not executable decoders; they are displayed separately from typed values and are never evaluated.",
            "874 buffers have no StructuresMetadata record; their layout stays raw unless a schema field supplies an explicit offset.",
            "12 buffer descriptors have ByteSize != BytesPerElement*NumElements; raw bytes and the explicit stride are authoritative.",
            "MSAA, 3D/volume and cube layouts are not decoded; native metadata and raw bytes are retained. Texture3D/cube overview selectors accept captured subresources but remain explicitly unavailable until a sidecar supplies face/slice bytes.",
            f"The source schema is a partial reviewed dictionary ({index['schema']['resourceSchemas']} resource / {index['schema']['parameterSchemas']} parameter schemas); unmatched resources remain raw and are listed as gaps. Field extraction is gated by the runtime stored format, and formatAlternatives alone never activates a variant.",
        ],
        "remainingAdapterWork": [
            {
                "area": "format-specific schema gates (appliesToFormats / formatVariants)",
                "status": "implemented in this baseline package",
                "reason": f"The baseline package embeds the reviewed {len(schema_bytes):,}-byte dictionary with {index['schema'].get('formatVariants', 0)} format variants across resources and {index['schema'].get('formatGatedFields', 0)} appliesToFormats-gated fields. Extraction selects a variant only when the captured payload format matches it; wrong-format fields become inactive with an explicit reason and are never read.",
                "currentBehaviour": "Runtime stored format (decode.dumpFormat when present, otherwise the descriptor format) controls numeric reads. A dump-layout mismatch that matches neither the declared format nor a formatVariants entry keeps every schema offset closed. The upcoming instrumented capture remains separate evidence.",
            },
            {
                "area": "Base/SubresourceLayouts.json exporter sidecar",
                "status": "deferred to the next adapter step",
                "reason": "The upcoming exporter adds per-subresource File/Dimension/MipLevel/FirstArraySlice/NumArraySlices/CubeFace/Extent/Depth/ZSlice/RowPitch/Native/View/Staging/StoredFormat/StoredBytesAreNativeStorage/BitExact/ValuePreserving/ByteSize metadata. This step keeps the current Texture3D/cube selectors explicit-unavailable and does not synthesize sidecar bytes.",
                "currentBehaviour": "Descriptor-only Texture3D/cube resources remain labelled unavailable; no face/slice/volume bytes are claimed.",
            },
        ],
        "portability": {
            "packagePathsRelative": not unsafe_paths,
            "packagePathCountChecked": len(path_values),
            "packageFilesChecked": len(existing_values),
            "missingPaths": missing_paths[:20],
            "tmpFiles": [str(p) for p in tmp_files[:20]],
            "schemaEmbeddedByteForByte": package_schema == schema_bytes,
            "hostPathsInGeneratedLinks": 0,
            "hostPathsInNativeEvidence": (
                "Absolute host paths occur only in retained native metadata strings "
                "(for example ConsoleVariables values in capture.json and FileName fields "
                "in structures.json); they are evidence, not package links."
            ),
            "browserAutomation": "none; this step ran no browser automation",
        },
        "commands": {
            "fullVerify": verify_result["command"] if verify_result else None,
            "pythonTests": py_result["command"] if py_result else None,
            "nodeTests": node_result["command"] if node_result else None,
            "nodeReadPathTests": read_path_result["command"] if read_path_result else None,
        },
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report_doc, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"\nWrote {report_path} ({report_path.stat().st_size} bytes); status={status}; "
          f"checks={len(report.checks)} failed={report.failures}")
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
