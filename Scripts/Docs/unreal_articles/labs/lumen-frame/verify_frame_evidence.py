#!/usr/bin/env python3
"""Portable offline verifier for the frozen Lumen frame package and replays.

The verifier never reads the original DumpGPU capture.  It:

* imports the production ``tools/package_frame.py`` and calls
  ``verify_package`` so every chunk, every payload byte and every native file
  copy is checked against the package inventory;
* compares the package's source schema byte-for-byte with the lab schema and
  fixes the published resource/parameter/reference counts;
* verifies the root-provided artifact manifest (file sizes and SHA-256 values)
  without accepting absolute, traversing or duplicate paths;
* with ``--replay`` builds a fresh temporary DumpGPU-shaped capture from
  package chunks only: all native metadata, all small ``Structures/`` binaries
  and the exact resource operands named by the frozen replay evidence.  The
  published replay verify scripts are then run against that temporary capture.

Only small final counts are printed.  The large ``verify_package`` report is
never dumped.  Completion metadata is not treated as owner-time identity.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

EXPECTED_PAYLOAD_FILES = 7024
EXPECTED_NATIVE_FILES = 888
EXPECTED_INPUT_FILES = 7912
EXPECTED_CHUNK_FILES = 3136
EXPECTED_SCHEMA_RESOURCES = 245
EXPECTED_SCHEMA_PARAMETERS = 15
EXPECTED_SCHEMA_REFS = 1547
PACKAGE_FORMAT = "unreal-frame-explorer-package"


class EvidenceError(RuntimeError):
    """A concrete evidence failure, reported without weakening a check."""


@dataclass(frozen=True)
class ReplaySpec:
    """An explicitly registered frozen replay interface."""

    name: str
    directory: str
    script: str
    expected_checks: int
    evidence_files: tuple[str, ...]
    # Extra fixed arguments; ``{replay_dir}`` expands to the installed replay
    # directory.  Optional source comparisons must remain absent so their
    # verifier checks are reported as skips, not as new direct proof.
    extra_args: tuple[str, ...] = ()
    expected_skips: tuple[str, ...] = ()
    # ``verify`` runs an independent verify script.  ``gdf`` runs the replay
    # itself (there is no separate verifier) and compares its frozen output.
    mode: str = "verify"
    compare_output: str | None = None


# Explicit registry.  Future replay directories are added here together with
# their published verify script, expected check count and authoritative JSON
# evidence.  Nothing is auto-discovered as a replay.
REPLAY_REGISTRY: tuple[ReplaySpec, ...] = (
    ReplaySpec(
        name="surface-cache",
        directory="surface-cache",
        script="verify_replay.py",
        expected_checks=85,
        evidence_files=(
            "cache-texel-replay.json",
            "cache-exposure-evidence.json",
        ),
    ),
    ReplaySpec(
        name="world-cache",
        directory="world-cache",
        script="verify_world_replay.py",
        expected_checks=25,
        evidence_files=(
            "world-cache-replay.json",
            "blue-noise-export.json",
            "permutation-evidence.json",
            "selfchecks.json",
        ),
    ),
    ReplaySpec(
        name="deferred",
        directory="deferred",
        script="verify_replay.py",
        expected_checks=17,
        evidence_files=(
            "report.json",
            "contract.json",
            "reproducibility.json",
        ),
        # The published verifier needs its frozen report.  --ltc-header is
        # deliberately absent: the LTC.h comparison must be a skip, never
        # promoted to direct proof. The published scene-graph evidence is
        # supplied separately and must pass its real primary-view check.
        extra_args=(
            "--report",
            "{replay_dir}/report.json",
            "--final-dir",
            "{final_dir}",
        ),
        expected_skips=("ltc_matches_ltc_h",),
    ),
    ReplaySpec(
        name="gdf",
        directory="gdf",
        script="replay_gdf_march.py",
        expected_checks=14,
        evidence_files=(
            "replay.json",
            "evidence-hashes.json",
        ),
        mode="gdf",
        compare_output="replay.json",
    ),
)


# --------------------------------------------------------------------- helpers
def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
            total += len(block)
    return digest.hexdigest(), total


def safe_relative(value: Any) -> str:
    """Normalize a repository-relative path and reject every escape form."""
    if not isinstance(value, str) or not value:
        raise EvidenceError("Manifest path must be a non-empty string.")
    if "\x00" in value:
        raise EvidenceError("Manifest path contains a NUL byte.")
    raw = value.replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise EvidenceError(f"Absolute manifest path is not allowed: {value!r}")
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise EvidenceError(f"Path traversal is not allowed: {value!r}")
        if ":" in part:
            raise EvidenceError(f"Drive/ADS separator is not allowed: {value!r}")
        if any(ord(ch) < 32 for ch in part):
            raise EvidenceError(f"Control character in manifest path: {value!r}")
        parts.append(part)
    if not parts:
        raise EvidenceError(f"Manifest path resolves to nothing: {value!r}")
    return "/".join(parts)


def safe_join(root: Path, relative: str) -> Path:
    relative = safe_relative(relative)
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise EvidenceError(f"Path escapes root: {relative!r}") from exc
    return candidate


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def derive_repo_root(lab: Path) -> Path:
    """Find the repository root from the installed lab path."""
    for candidate in (lab, *lab.parents):
        if (candidate / "Docs").is_dir() and (candidate / "Scripts").is_dir():
            return candidate
        if (candidate / "Docs/Unreal/assets/lumen-frame").is_dir():
            return candidate
        if (candidate / ".git").exists():  # Git is a layout hint only; never edited.
            return candidate
    if len(lab.parents) > 4:
        return lab.parents[4]
    return lab


def default_package(lab: Path) -> Path:
    return derive_repo_root(lab) / "Docs/Unreal/assets/lumen-frame"


def find_package_frame(lab: Path, package: Path, repo_root: Path) -> Path:
    """Locate the production packer without importing anything else by name."""
    candidates: list[Path] = []
    for variable in ("LUMEN_FRAME_PACKAGE_TOOL", "FRAME_PACKAGE_TOOL"):
        value = os.environ.get(variable)
        if value:
            candidates.append(Path(value))
    candidates.extend(
        [
            repo_root / "tools/package_frame.py",
            lab / "tools/package_frame.py",
            Path.cwd() / "tools/package_frame.py",
            package.parent / "tools/package_frame.py",
        ]
    )
    # The lab can live at Scripts/Docs/...; accept a tools/ directory at any
    # enclosing publication level without hard-coding this checkout's layout.
    for ancestor in (lab.parent, *lab.parents[1:5]):
        candidates.append(ancestor / "tools/package_frame.py")
    try:
        installed = importlib.util.find_spec("package_frame")
    except (ImportError, ValueError):
        installed = None
    if installed is not None and installed.origin:
        candidates.append(Path(installed.origin))
    seen: set[Path] = set()
    searched: list[str] = []
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        searched.append(str(resolved))
        if resolved.is_file():
            return resolved
    raise EvidenceError(
        "Production package_frame.py was not found. Put it at "
        f"{repo_root / 'tools/package_frame.py'} or set LUMEN_FRAME_PACKAGE_TOOL. "
        "Searched: " + "; ".join(searched)
    )


def import_package_frame(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("_lumen_frame_package_frame", path)
    if spec is None or spec.loader is None:
        raise EvidenceError(f"Cannot import production packer from {path}")
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their defining module through sys.modules; registering
    # the production module is required before executing it.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - report the concrete import failure
        raise EvidenceError(f"Cannot import production packer {path}: {exc}") from exc
    for name in ("verify_package", "reconstruct_payload", "safe_relpath", "sha256_file"):
        if not hasattr(module, name):
            raise EvidenceError(f"Production packer {path} lacks required API {name}()")
    return module


# ------------------------------------------------------------------- package
def verify_package_bytes(package: Path, packer: Any) -> dict[str, Any]:
    try:
        result = packer.verify_package(package)
    except Exception as exc:  # noqa: BLE001 - retain the packer's exact message
        raise EvidenceError(f"Package verification failed: {exc}") from exc
    if not isinstance(result, tuple) or len(result) != 4:
        raise EvidenceError("Production verify_package() returned an unexpected result shape.")
    index, payloads, chunks, inventory = result
    if not isinstance(index, dict) or not isinstance(inventory, dict):
        raise EvidenceError("Package index/inventory is not a JSON object.")
    payload_records = inventory.get("payloads")
    native_records = inventory.get("nativeFiles")
    if not isinstance(payload_records, list) or not isinstance(native_records, list):
        raise EvidenceError("Package inventory lacks payload/native file lists.")
    if len(payloads) != len(payload_records):
        raise EvidenceError(
            f"Verified payload count {len(payloads)} != inventory count {len(payload_records)}."
        )
    if len(payload_records) != EXPECTED_PAYLOAD_FILES:
        raise EvidenceError(
            f"Payload file count {len(payload_records)} != required {EXPECTED_PAYLOAD_FILES}."
        )
    if len(native_records) != EXPECTED_NATIVE_FILES:
        raise EvidenceError(
            f"Native file count {len(native_records)} != required {EXPECTED_NATIVE_FILES}."
        )
    if len(payload_records) + len(native_records) != EXPECTED_INPUT_FILES:
        raise EvidenceError("Payload plus native file counts do not total 7,912.")
    chunk_records = inventory.get("chunks")
    if not isinstance(chunk_records, list) or len(chunk_records) != EXPECTED_CHUNK_FILES:
        got = len(chunk_records) if isinstance(chunk_records, list) else "missing"
        raise EvidenceError(f"Chunk file count {got} != required {EXPECTED_CHUNK_FILES}.")
    if len(chunks) != EXPECTED_CHUNK_FILES:
        raise EvidenceError(
            f"Verified chunk count {len(chunks)} != required {EXPECTED_CHUNK_FILES}."
        )

    payload_paths: set[str] = set()
    total_payload_bytes = 0
    for record in payload_records:
        try:
            rel = packer.safe_relpath(record["originalPath"])
            size = int(record["bytes"])
        except Exception as exc:  # noqa: BLE001 - retain the production path error
            raise EvidenceError(f"Malformed payload record: {exc}") from exc
        if not rel.lower().endswith(".bin"):
            raise EvidenceError(f"Payload is not a binary file: {rel}")
        if rel in payload_paths:
            raise EvidenceError(f"Duplicate payload path in inventory: {rel}")
        payload_paths.add(rel)
        total_payload_bytes += size

    native_paths: set[str] = set()
    for record in native_records:
        try:
            rel = packer.safe_relpath(record["path"])
        except Exception as exc:  # noqa: BLE001 - retain the production path error
            raise EvidenceError(f"Malformed native file record: {exc}") from exc
        if rel.lower().endswith(".bin"):
            raise EvidenceError(f"Native metadata record is a .bin payload: {rel}")
        if rel in native_paths:
            raise EvidenceError(f"Duplicate native file path in inventory: {rel}")
        native_paths.add(rel)

    source = index.get("source") or {}
    if source:
        if int(source.get("nativeFileCount", -1)) != EXPECTED_NATIVE_FILES:
            raise EvidenceError("Package index nativeFileCount does not match 888.")
        if int(source.get("payloadFileCount", -1)) != EXPECTED_PAYLOAD_FILES:
            raise EvidenceError("Package index payloadFileCount does not match 7,024.")

    counts = inventory.get("counts") or {}
    if counts:
        if int(counts.get("payloadFiles", -1)) != EXPECTED_PAYLOAD_FILES:
            raise EvidenceError("Package inventory counts.payloadFiles does not match 7,024.")
        if int(counts.get("nativeFiles", -1)) != EXPECTED_NATIVE_FILES:
            raise EvidenceError("Package inventory counts.nativeFiles does not match 888.")

    return {
        "index": index,
        "inventory": inventory,
        "payloads": payload_records,
        "nativeFiles": native_records,
        "chunks": chunk_records,
        "payloadCount": len(payload_records),
        "nativeCount": len(native_records),
        "chunkCount": len(chunk_records),
        "inputCount": len(payload_records) + len(native_records),
        "payloadBytes": total_payload_bytes,
        "ownerTimeIdentity": "not-claimed",
    }


# -------------------------------------------------------------------- schema
def verify_schema(package: Path, lab: Path, package_result: dict[str, Any]) -> dict[str, Any]:
    package_schema = package / "schema.json"
    lab_schema = lab / "schemas/source-schema.json"
    if not package_schema.is_file():
        raise EvidenceError(f"Package has no schema.json: {package_schema}")
    if not lab_schema.is_file():
        raise EvidenceError(f"Lab has no schemas/source-schema.json: {lab_schema}")
    package_bytes = package_schema.read_bytes()
    lab_bytes = lab_schema.read_bytes()
    package_sha, package_size = sha256_file(package_schema)
    lab_sha, lab_size = sha256_file(lab_schema)
    if package_bytes != lab_bytes or package_sha != lab_sha or package_size != lab_size:
        raise EvidenceError(
            "Package schema.json does not match the lab source-schema.json byte-for-byte: "
            f"package {package_size} bytes {package_sha}, lab {lab_size} bytes {lab_sha}."
        )
    try:
        schema = json.loads(package_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"Package schema.json is not valid JSON: {exc}") from exc
    resources = schema.get("resourceSchemas")
    parameters = schema.get("parameterSchemas")
    refs = schema.get("sourceRefs")
    if not isinstance(resources, list) or len(resources) != EXPECTED_SCHEMA_RESOURCES:
        raise EvidenceError(
            f"Schema resource count {len(resources) if isinstance(resources, list) else 'missing'} "
            f"!= required {EXPECTED_SCHEMA_RESOURCES}."
        )
    if not isinstance(parameters, list) or len(parameters) != EXPECTED_SCHEMA_PARAMETERS:
        raise EvidenceError(
            f"Schema parameter count {len(parameters) if isinstance(parameters, list) else 'missing'} "
            f"!= required {EXPECTED_SCHEMA_PARAMETERS}."
        )
    if not isinstance(refs, list) or len(refs) != EXPECTED_SCHEMA_REFS:
        raise EvidenceError(
            f"Schema source reference count {len(refs) if isinstance(refs, list) else 'missing'} "
            f"!= required {EXPECTED_SCHEMA_REFS}."
        )

    index = package_result["index"]
    index_schema = index.get("schema") or {}
    if index_schema:
        if index_schema.get("sha256") != package_sha:
            raise EvidenceError("Package index schema SHA-256 does not match schema.json.")
        if int(index_schema.get("bytes", -1)) != package_size:
            raise EvidenceError("Package index schema byte count does not match schema.json.")
        if int(index_schema.get("resourceSchemas", -1)) != EXPECTED_SCHEMA_RESOURCES:
            raise EvidenceError("Package index resourceSchemas does not match 245.")
        if int(index_schema.get("parameterSchemas", -1)) != EXPECTED_SCHEMA_PARAMETERS:
            raise EvidenceError("Package index parameterSchemas does not match 15.")
        if int(index_schema.get("sourceRefs", -1)) != EXPECTED_SCHEMA_REFS:
            raise EvidenceError("Package index sourceRefs does not match 1,547.")

    return {
        "bytes": package_size,
        "sha256": package_sha,
        "resources": len(resources),
        "parameters": len(parameters),
        "sourceRefs": len(refs),
    }


# ----------------------------------------------------------------- artifacts
def verify_artifacts(manifest_path: Path, repo_root: Path) -> dict[str, Any]:
    if not manifest_path.is_file():
        raise EvidenceError(f"Artifact manifest does not exist: {manifest_path}")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"Artifact manifest is not valid JSON: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("files"), list):
        raise EvidenceError("Artifact manifest must be an object with a 'files' list.")
    records = document["files"]
    if not records:
        raise EvidenceError("Artifact manifest contains no files.")
    root = repo_root.resolve()
    seen: dict[str, str] = {}
    total_bytes = 0
    manifest_resolved = manifest_path.resolve()
    for position, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise EvidenceError(f"Artifact entry #{position} is not an object.")
        try:
            rel = safe_relative(record["path"])
            expected_bytes = record["bytes"]
            expected_sha = str(record["sha256"]).lower()
        except (KeyError, TypeError) as exc:
            raise EvidenceError(f"Malformed artifact entry #{position}: {exc}") from exc
        if not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool) or expected_bytes < 0:
            raise EvidenceError(f"Artifact {rel}: 'bytes' must be a non-negative integer.")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            raise EvidenceError(f"Artifact {rel}: 'sha256' is not a 64-hex digest.")
        key = rel.casefold()
        if key in seen:
            raise EvidenceError(f"Duplicate artifact path: {seen[key]!r} and {rel!r}.")
        seen[key] = rel
        path = safe_join(root, rel)
        if path == manifest_resolved:
            raise EvidenceError("Artifact manifest must not list itself (self-referential hash).")
        if not path.is_file():
            raise EvidenceError(f"Artifact is missing: {rel}")
        actual_sha, actual_bytes = sha256_file(path)
        if actual_bytes != expected_bytes:
            raise EvidenceError(
                f"Artifact size mismatch for {rel}: {actual_bytes} != {expected_bytes}."
            )
        if actual_sha != expected_sha:
            raise EvidenceError(
                f"Artifact SHA-256 mismatch for {rel}: {actual_sha} != {expected_sha}."
            )
        total_bytes += actual_bytes
    return {"files": len(records), "bytes": total_bytes}


# -------------------------------------------------------------------- replay
def _walk_json_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _walk_json_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_strings(child)


def payload_path_from_string(value: str) -> str | None:
    """Return a normalized Resources/ or Structures/ .bin path from a string."""
    normalized = value.replace("\\", "/")
    for marker in ("Resources/", "Structures/"):
        index = normalized.find(marker)
        if index >= 0:
            candidate = normalized[index:]
            break
    else:
        return None
    if not candidate.lower().endswith(".bin"):
        return None
    return safe_relative(candidate)


def discover_payload_paths(
    replay_dir: Path,
    spec: ReplaySpec,
    known_paths: set[str] | None = None,
) -> set[str]:
    """Discover every Resources/ or Structures/ operand named by the evidence.

    A few nested exports carry only a basename.  Those are resolved against the
    package inventory only when exactly one payload has that basename; an
    ambiguous basename is an error rather than a guess about the producer.
    """
    basenames: dict[str, list[str]] = {}
    for path in known_paths or ():
        basenames.setdefault(path.rsplit("/", 1)[-1].casefold(), []).append(path)
    paths: set[str] = set()
    for name in spec.evidence_files:
        evidence = replay_dir / name
        if not evidence.is_file():
            raise EvidenceError(f"{spec.name}: missing replay evidence file {evidence}")
        try:
            document = json.loads(evidence.read_text(encoding="utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceError(f"{spec.name}: invalid replay evidence {name}: {exc}") from exc
        for value in _walk_json_strings(document):
            try:
                candidate = payload_path_from_string(value)
            except EvidenceError:
                continue
            if candidate is None:
                # Basename-only export.  Only a pure filename is considered,
                # so an absolute or unrelated path can never select a payload
                # produced by a different pass.
                normalized = value.replace("\\", "/")
                if known_paths is None or "/" in normalized:
                    continue
                if not normalized.lower().endswith(".bin"):
                    continue
                matches = basenames.get(normalized.casefold(), [])
                if len(matches) > 1:
                    raise EvidenceError(
                        f"{spec.name}: ambiguous capture filename {normalized!r} matches "
                        + ", ".join(sorted(matches))
                    )
                if matches:
                    candidate = matches[0]
                else:
                    continue
            paths.add(candidate)
    if not paths:
        raise EvidenceError(f"{spec.name}: no Resources/ or Structures/ .bin operands discovered.")
    return paths


def require_known_payloads(spec: ReplaySpec, discovered: set[str], known: set[str]) -> None:
    missing = sorted(discovered - known)
    if missing:
        raise EvidenceError(
            f"{spec.name}: frozen evidence names operands absent from the package: "
            + ", ".join(missing)
        )


def _copy_native_files(
    package: Path, capture: Path, native_records: list[dict[str, Any]], packer: Any
) -> int:
    copied = 0
    for record in native_records:
        rel = packer.safe_relpath(record["path"])
        source = safe_join(package, record["packagePath"])
        target = safe_join(capture, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied += 1
    return copied


def _reconstruct(
    package: Path,
    capture: Path,
    payload: dict[str, Any],
    cache: dict[str, bytes],
    packer: Any,
) -> dict[str, Any]:
    try:
        return packer.reconstruct_payload(package, payload, cache, capture)
    except Exception as exc:  # noqa: BLE001 - preserve the production error
        raise EvidenceError(
            f"Cannot reconstruct {payload.get('originalPath', payload.get('id'))}: {exc}"
        ) from exc


def _run_script(
    script: Path,
    capture: Path,
    output: Path,
    extra_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(script),
        "--capture",
        str(capture),
        "--output",
        str(output),
        *extra_args,
    ]
    environment = os.environ.copy()
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        return subprocess.run(
            command,
            cwd=str(script.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise EvidenceError(f"{script.name} timed out after 1800 seconds.") from exc
    except OSError as exc:
        raise EvidenceError(f"Cannot run {script}: {exc}") from exc


def _check_passed(check: Any) -> bool:
    if not isinstance(check, dict):
        return False
    for key in ("ok", "passed", "pass"):
        if key in check:
            return check.get(key) is True
    return False


def _is_skipped(check: Any) -> bool:
    if not isinstance(check, dict):
        return False
    evidence = check.get("evidence")
    return isinstance(evidence, str) and evidence.lstrip().lower().startswith("skipped")


def _split_checks(checks: list[dict[str, Any]]) -> tuple[int, list[str]]:
    """Return the direct-proof count and the names of explicitly skipped checks."""
    skipped: list[str] = []
    for check in checks:
        if _is_skipped(check):
            skipped.append(str(check.get("name")))
    return len(checks) - len(skipped), skipped


def _read_verification(path: Path, spec: ReplaySpec) -> tuple[list[dict[str, Any]], bool]:
    if not path.is_file():
        raise EvidenceError(f"{spec.name}: verify script did not write {path.name}.")
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{spec.name}: invalid verification JSON: {exc}") from exc
    checks = document.get("checks")
    if not isinstance(checks, list):
        raise EvidenceError(f"{spec.name}: verification JSON has no checks list.")
    all_ok = all(_check_passed(check) for check in checks)
    if "fails" in document and document.get("fails"):
        all_ok = False
    if "passed" in document and document.get("passed") is not True:
        all_ok = False
    return checks, all_ok


def _failure_text(completed: subprocess.CompletedProcess[str]) -> str:
    text = (completed.stdout or "") + (completed.stderr or "")
    text = text.strip()
    return text[-2500:] if text else "(no output)"


# ---------------------------------------------------------------- GDF replay
GDF_REQUIRED_CHECKS = (
    "selected_lane_is_captured_compacted_trace",
    "query_origin_matches_captured_screen_depth_and_ray_info",
    "four_gdf_native_bindings_resolve_to_exported_diagnostic_resources",
    "gdf_interval_uses_cache_bound_not_20000",
    "gdf_interval_is_shorter_than_one_minimum_step",
    "gdf_trace_records_first_iteration_with_raw_values",
    "gdf_miss_matches_captured_bHit_false",
    "tracehit_word_is_exact_cache_override",
    "tracehit_previous_version_is_a_same_graph_pass_before_167",
    "no_recorded_graph_writer_on_four_gdf_ids",
    "export_timing_caveat_recorded",
    "trace_radiance_residual_within_one_store_ulp",
    "not_claiming_trace_radiance_exact",
    "no_gpu_work_executed",
)
GDF_VOLUME_KEYS = ("pageAtlas", "coverageAtlas", "pageTable", "mip")


def _read_gdf_result(path: Path, spec: ReplaySpec) -> tuple[list[dict[str, Any]], bool]:
    if not path.is_file():
        raise EvidenceError(f"{spec.name}: replay did not write {path.name}.")
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{spec.name}: invalid replay JSON: {exc}") from exc
    checks = document.get("selfchecks")
    if not isinstance(checks, list):
        raise EvidenceError(f"{spec.name}: replay JSON has no selfchecks list.")
    all_ok = document.get("allSelfchecksPassed") is True and all(
        _check_passed(check) for check in checks
    )
    return checks, all_ok


def _first_difference(left: bytes, right: bytes) -> str:
    limit = min(len(left), len(right))
    offset = next((i for i in range(limit) if left[i] != right[i]), limit)
    return (
        f"first difference at byte {offset}: fresh {left[offset:offset + 24]!r} "
        f"frozen {right[offset:offset + 24]!r}"
    )


def compare_gdf_replay(fresh_path: Path, frozen_path: Path) -> dict[str, Any]:
    """Byte-compare a fresh replay with the frozen one.

    The only permitted difference is the absolute temporary capture root that
    the replay records (both in ``capture.root`` and ``evidenceHashes.capture``).
    It is replaced by the frozen public capture root before the byte comparison;
    the substitution is reported explicitly rather than hidden.
    """
    if not frozen_path.is_file():
        raise EvidenceError(f"Frozen GDF replay is missing: {frozen_path}")
    fresh = fresh_path.read_bytes()
    frozen = frozen_path.read_bytes()
    fresh_document = json.loads(fresh.decode("utf-8"))
    frozen_document = json.loads(frozen.decode("utf-8"))
    fresh_root = (fresh_document.get("capture") or {}).get("root")
    frozen_root = (frozen_document.get("capture") or {}).get("root")
    if not isinstance(fresh_root, str) or not isinstance(frozen_root, str):
        raise EvidenceError("GDF replay capture.root is missing from fresh or frozen JSON.")
    normalized = fresh
    replaced = False
    if fresh != frozen:
        if fresh_root != frozen_root:
            # Replacing the path in the serialized bytes is exact, but first
            # prove that no semantic field other than the two capture-root
            # records differs.  This prevents a global substitution from
            # hiding an unrelated change.
            fresh_document["capture"]["root"] = frozen_root
            evidence_hashes = fresh_document.get("evidenceHashes")
            if isinstance(evidence_hashes, dict) and "capture" in evidence_hashes:
                evidence_hashes["capture"] = frozen_root
            if fresh_document != frozen_document:
                raise EvidenceError(
                    "Fresh GDF replay.json is not byte-identical to the frozen replay "
                    "beyond the temporary capture root."
                )
            fresh_encoded = json.dumps(fresh_root)[1:-1].encode("utf-8")
            frozen_encoded = json.dumps(frozen_root)[1:-1].encode("utf-8")
            if fresh_encoded not in fresh:
                raise EvidenceError(
                    "Fresh GDF replay does not record its expected temporary capture root."
                )
            normalized = fresh.replace(fresh_encoded, frozen_encoded)
            replaced = True
        if normalized != frozen:
            raise EvidenceError(
                "Fresh GDF replay.json is not byte-identical to the frozen replay after "
                f"capture-root normalization; {_first_difference(normalized, frozen)}"
            )
    return {
        "byteIdentical": True,
        "byteIdenticalSha256": hashlib.sha256(frozen).hexdigest(),
        "freshRawSha256": hashlib.sha256(fresh).hexdigest(),
        "captureRootNormalized": replaced,
        "freshCaptureRoot": fresh_root,
        "frozenCaptureRoot": frozen_root,
    }


def _validate_gdf_report(document: dict[str, Any], spec: ReplaySpec) -> dict[str, Any]:
    """Assert the exact pass-28 boundary and the non-claims around radiance."""
    checks = document.get("selfchecks")
    if not isinstance(checks, list):
        raise EvidenceError(f"{spec.name}: replay JSON has no selfchecks list.")
    names = [check.get("name") for check in checks if isinstance(check, dict)]
    if len(checks) != len(GDF_REQUIRED_CHECKS) or set(names) != set(GDF_REQUIRED_CHECKS):
        raise EvidenceError(
            f"{spec.name}: expected the 14 registered selfchecks, got {len(checks)}: {names}"
        )
    if not all(_check_passed(check) for check in checks):
        raise EvidenceError(f"{spec.name}: one or more GDF selfchecks are not passing.")
    if document.get("allSelfchecksPassed") is not True:
        raise EvidenceError(f"{spec.name}: replay does not declare all selfchecks passed.")

    capture = document.get("capture") or {}
    if (
        capture.get("graph") != "0001"
        or capture.get("passId") != 167
        or not str(capture.get("passPointer", "")).startswith("0001")
    ):
        raise EvidenceError(f"{spec.name}: unexpected captured pass identity: {capture}")

    volumes = document.get("gdfVolumes") or {}
    if set(volumes) != set(GDF_VOLUME_KEYS):
        raise EvidenceError(f"{spec.name}: expected four GDF volumes, got {sorted(volumes)}")
    for key in GDF_VOLUME_KEYS:
        volume = volumes[key]
        if volume.get("firstScheduledByPassId") != 28:
            raise EvidenceError(f"{spec.name}: {key} is not a pass-28 native export.")
        if volume.get("ownerPassExecutionDependencyClaimed") is not False:
            raise EvidenceError(f"{spec.name}: {key} improperly claims an owner dependency.")
        if volume.get("sourceStateProvenAtScheduling") is not False:
            raise EvidenceError(f"{spec.name}: {key} improperly claims source-state proof.")
        if volume.get("reusedScheduledReadback") is not True:
            raise EvidenceError(f"{spec.name}: {key} does not record a reused readback.")
        if int((volume.get("payload") or {}).get("bytes", 0)) <= 0:
            raise EvidenceError(f"{spec.name}: {key} has no exported payload bytes.")

    trace = document.get("trace") or {}
    if "pass-28" not in str(trace.get("inputBasis", "")):
        raise EvidenceError(f"{spec.name}: trace does not state its pass-28 native basis.")
    comparison = document.get("comparison") or {}
    traces = comparison.get("traces") or {}
    if comparison.get("traceHitWordExact") is not True:
        raise EvidenceError(f"{spec.name}: TraceHit is not the exact cache override.")
    if traces.get("rawWordMatchesCaptured") is not False:
        raise EvidenceError(f"{spec.name}: replay improperly claims exact raw radiance.")
    if traces.get("ditherWordMatchesCaptured") is not False:
        raise EvidenceError(f"{spec.name}: replay improperly claims exact dithered radiance.")
    for field in ("rawCodeDeltaToCaptured", "ditherCodeDeltaToCaptured"):
        deltas = traces.get(field)
        if not isinstance(deltas, list) or len(deltas) != 3 or any(
            not isinstance(delta, int) or abs(delta) > 1 for delta in deltas
        ):
            raise EvidenceError(f"{spec.name}: {field} is not within one store ULP.")
    if not any(
        check.get("name") == "trace_radiance_residual_within_one_store_ulp"
        and _check_passed(check)
        for check in checks
    ):
        raise EvidenceError(f"{spec.name}: one-ULP radiance boundary was not verified.")

    return {
        "selfchecks": len(checks),
        "oneUlpBoundary": True,
        "pass28NativeSnapshot": True,
        "traceHitExact": True,
        "radianceExact": False,
        "provenance": "pass-28 native exports reused by pass-167; not owner-time identity",
    }


def run_replay(
    spec: ReplaySpec,
    lab: Path,
    package: Path,
    package_result: dict[str, Any],
    packer: Any,
) -> dict[str, Any]:
    replay_dir = lab / "replays" / spec.directory
    script = replay_dir / spec.script
    if not replay_dir.is_dir():
        raise EvidenceError(f"{spec.name}: replay directory is missing: {replay_dir}")
    if not script.is_file():
        raise EvidenceError(f"{spec.name}: replay script is missing: {script}")

    payload_by_path: dict[str, dict[str, Any]] = {}
    for record in package_result["payloads"]:
        rel = packer.safe_relpath(record["originalPath"])
        payload_by_path[rel] = record
    discovered = discover_payload_paths(replay_dir, spec, set(payload_by_path))
    require_known_payloads(spec, discovered, set(payload_by_path))

    structure_paths = sorted(
        path for path in payload_by_path if path.startswith("Structures/")
    )
    resource_paths = sorted(
        path for path in discovered if path.startswith("Resources/")
    )
    if not structure_paths:
        raise EvidenceError(f"{spec.name}: package contains no Structures/ binaries.")

    # The temporary capture is deliberately outside the workspace and is the
    # only place this verifier writes reconstructed bytes.
    with tempfile.TemporaryDirectory(prefix=f"lumen-{spec.name}-") as temporary:
        capture = Path(temporary).resolve()
        if is_within(capture, Path.cwd().resolve()):
            raise EvidenceError("Refusing to reconstruct inside the workspace directory.")
        capture.mkdir(parents=True, exist_ok=True)
        extra_args = tuple(
            argument.replace("{replay_dir}", str(replay_dir)).replace(
                "{final_dir}", str(lab / "provenance")
            )
            for argument in spec.extra_args
        )
        native_count = _copy_native_files(
            package, capture, package_result["nativeFiles"], packer
        )

        cache: dict[str, bytes] = {}
        reconstructed: dict[str, dict[str, Any]] = {}
        for rel in structure_paths:
            reconstructed[rel] = _reconstruct(
                package, capture, payload_by_path[rel], cache, packer
            )
        for rel in resource_paths:
            reconstructed[rel] = _reconstruct(
                package, capture, payload_by_path[rel], cache, packer
            )

        output = capture / "gdf-output" if spec.mode == "gdf" else capture / "verification.json"
        result_path = output / spec.compare_output if spec.mode == "gdf" and spec.compare_output else output
        attempts = 0
        completed: subprocess.CompletedProcess[str] | None = None
        checks: list[dict[str, Any]] = []
        all_ok = False
        while attempts < 3:
            attempts += 1
            completed = _run_script(script, capture, output, extra_args)
            try:
                if spec.mode == "gdf":
                    checks, all_ok = _read_gdf_result(result_path, spec)
                else:
                    checks, all_ok = _read_verification(result_path, spec)
            except EvidenceError:
                checks, all_ok = [], False
            if completed.returncode == 0 and all_ok:
                break
            # A future replay verifier may scan an additional resource/hash
            # list.  Reconstruct every newly named operand and retry rather
            # than silently accepting a missing file.  Only package-known
            # Resources/ paths can be added here; Structures/ were all built.
            output_text = (completed.stdout or "") + (completed.stderr or "")
            new_paths: set[str] = set()
            for token in re.findall(r"[^\s\"'<>]+", output_text):
                try:
                    candidate = payload_path_from_string(token)
                except EvidenceError:
                    continue
                if (
                    candidate is not None
                    and candidate.startswith("Resources/")
                    and candidate in payload_by_path
                    and candidate not in reconstructed
                ):
                    new_paths.add(candidate)
            if not new_paths:
                break
            for rel in sorted(new_paths):
                reconstructed[rel] = _reconstruct(
                    package, capture, payload_by_path[rel], cache, packer
                )
            resource_paths = sorted(set(resource_paths) | new_paths)

        if completed is None or completed.returncode != 0 or not all_ok:
            detail = _failure_text(completed) if completed is not None else "(not run)"
            raise EvidenceError(
                f"{spec.name}: replay verification failed after {attempts} attempt(s); "
                f"{len(checks)} checks read. Output tail:\n{detail}"
            )
        if len(checks) != spec.expected_checks:
            raise EvidenceError(
                f"{spec.name}: expected {spec.expected_checks} checks, got {len(checks)}."
            )
        if any(not _check_passed(check) for check in checks):
            raise EvidenceError(f"{spec.name}: one or more replay checks are not passing.")
        direct_checks, skipped_checks = _split_checks(checks)
        if len(skipped_checks) != len(spec.expected_skips) or set(skipped_checks) != set(
            spec.expected_skips
        ):
            raise EvidenceError(
                f"{spec.name}: optional checks must be reported as skips, not as direct "
                f"proof; expected {list(spec.expected_skips)}, got {skipped_checks}."
            )
        if direct_checks != spec.expected_checks - len(spec.expected_skips):
            raise EvidenceError(
                f"{spec.name}: expected {spec.expected_checks - len(spec.expected_skips)} "
                f"direct checks, got {direct_checks}."
            )

        gdf_result: dict[str, Any] | None = None
        if spec.mode == "gdf":
            if not spec.compare_output:
                raise EvidenceError(f"{spec.name}: no frozen replay output is registered.")
            if not result_path.is_file():
                raise EvidenceError(f"{spec.name}: replay did not write {result_path.name}.")
            gdf_result = compare_gdf_replay(result_path, replay_dir / spec.compare_output)
            try:
                gdf_document = json.loads(result_path.read_text(encoding="utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise EvidenceError(f"{spec.name}: invalid fresh replay JSON: {exc}") from exc
            gdf_result.update(_validate_gdf_report(gdf_document, spec))

        reconstructed_bytes = sum(int(record.get("bytes", 0)) for record in reconstructed.values())
        structure_bytes = sum(
            int(payload_by_path[path].get("bytes", 0)) for path in structure_paths
        )
        resource_bytes = sum(
            int(payload_by_path[path].get("bytes", 0)) for path in resource_paths
        )
        provenance = hashlib.sha256()
        for path in sorted(reconstructed):
            record = payload_by_path[path]
            provenance.update(
                f"{path}\0{record['sha256']}\0{int(record['bytes'])}\n".encode("utf-8")
            )
        result = {
            "name": spec.name,
            "checks": len(checks),
            "expectedChecks": spec.expected_checks,
            "directChecks": direct_checks,
            "skippedChecks": skipped_checks,
            "attempts": attempts,
            "nativeFilesCopied": native_count,
            "structureCount": len(structure_paths),
            "structureBytes": structure_bytes,
            "resourceCount": len(resource_paths),
            "resourceBytes": resource_bytes,
            "reconstructedCount": len(reconstructed),
            "reconstructedBytes": reconstructed_bytes,
            "provenanceSha256": provenance.hexdigest(),
        }
        if gdf_result is not None:
            result["gdf"] = gdf_result
        return result


def verify_replays(
    lab: Path, package: Path, package_result: dict[str, Any], packer: Any
) -> tuple[dict[str, Any], str]:
    try:
        import numpy  # noqa: PLC0415 - intentionally lazy: only --replay needs it
    except ImportError as exc:
        raise EvidenceError(
            "NumPy is required only for --replay. Install the replay requirements first."
        ) from exc
    results: dict[str, Any] = {}
    for spec in REPLAY_REGISTRY:
        results[spec.name] = run_replay(spec, lab, package, package_result, packer)
    return results, str(numpy.__version__)


# ---------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the frozen Lumen frame package, schema and replays offline."
    )
    parser.add_argument(
        "--package",
        help="Extracted frame package directory (default: repo Docs/Unreal/assets/lumen-frame).",
    )
    parser.add_argument(
        "--lab",
        help="Lumen frame lab directory (default: directory containing this script).",
    )
    parser.add_argument(
        "--artifact-manifest",
        help="Optional {files:[{path,bytes,sha256}]} manifest of repository artifacts.",
    )
    parser.add_argument(
        "--repo-root",
        help="Repository root for artifact manifest paths (default: derived from --lab).",
    )
    parser.add_argument(
        "--replay",
        action="store_true",
        help="Reconstruct a minimal temporary capture and run the frozen replay verifiers.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        lab = Path(args.lab).expanduser().resolve() if args.lab else Path(__file__).resolve().parent
        if not lab.is_dir():
            raise EvidenceError(f"Lab directory does not exist: {lab}")
        package = (
            Path(args.package).expanduser().resolve() if args.package else default_package(lab)
        )
        if not package.is_dir():
            raise EvidenceError(f"Package directory does not exist: {package}")
        repo_root = (
            Path(args.repo_root).expanduser().resolve()
            if args.repo_root
            else derive_repo_root(lab)
        )

        packer_path = find_package_frame(lab, package, repo_root)
        packer = import_package_frame(packer_path)
        package_result = verify_package_bytes(package, packer)
        print(
            "PASS package bytes: "
            f"{package_result['payloadCount']:,} payloads / "
            f"{package_result['nativeCount']:,} native files / "
            f"{package_result['inputCount']:,} input files / "
            f"{package_result['chunkCount']:,} chunks / "
            f"{package_result['payloadBytes']:,} payload bytes "
            "(byte identity only; owner-time identity is not claimed).",
            flush=True,
        )

        schema_result = verify_schema(package, lab, package_result)
        print(
            "PASS source schema: "
            f"{schema_result['bytes']:,} bytes sha256={schema_result['sha256']}; "
            f"{schema_result['resources']} resources / "
            f"{schema_result['parameters']} parameters / "
            f"{schema_result['sourceRefs']:,} source refs.",
            flush=True,
        )

        artifact_result: dict[str, Any] | None = None
        if args.artifact_manifest:
            manifest = Path(args.artifact_manifest).expanduser().resolve()
            artifact_result = verify_artifacts(manifest, repo_root)
            print(
                "PASS artifact hashes: "
                f"{artifact_result['files']:,} files / {artifact_result['bytes']:,} bytes.",
                flush=True,
            )

        replay_results: dict[str, Any] = {}
        numpy_version = ""
        if args.replay:
            replay_results, numpy_version = verify_replays(
                lab, package, package_result, packer
            )
            for name, result in replay_results.items():
                skipped = result["skippedChecks"]
                skip_note = (
                    f" + {len(skipped)} skips ({', '.join(skipped)})" if skipped else ""
                )
                print(
                    f"PASS replay {name}: {result['checks']}/{result['expectedChecks']} checks "
                    f"({result['directChecks']} direct{skip_note}); "
                    f"reconstructed {result['reconstructedCount']:,} payloads "
                    f"({result['reconstructedBytes']:,} bytes) = "
                    f"{result['structureCount']:,} Structures + "
                    f"{result['resourceCount']:,} resource operands; "
                    f"{result['nativeFilesCopied']:,} native files copied; "
                    f"provenance sha256={result['provenanceSha256']}.",
                    flush=True,
                )
                if "gdf" in result:
                    gdf = result["gdf"]
                    print(
                        "PASS replay gdf boundaries: replay.json byte-identical "
                        f"sha256={gdf['byteIdenticalSha256']} (capture root normalized: "
                        f"{str(gdf['captureRootNormalized']).lower()}); "
                        f"{gdf['selfchecks']} selfchecks, 4 pass-28 native volumes, "
                        f"TraceHit exact, radiance exact={str(gdf['radianceExact']).lower()}, "
                        "one-ULP boundary verified.",
                        flush=True,
                    )
            print(f"NumPy {numpy_version}", flush=True)

        print("PASS all requested evidence checks.", flush=True)
        result = {
            "status": "pass",
            "package": {
                "path": str(package),
                "payloads": package_result["payloadCount"],
                "nativeFiles": package_result["nativeCount"],
                "chunks": package_result["chunkCount"],
                "payloadBytes": package_result["payloadBytes"],
                "ownerTimeIdentity": package_result["ownerTimeIdentity"],
            },
            "schema": schema_result,
            "artifacts": artifact_result,
            "replays": replay_results,
            "numpy": numpy_version,
            "packer": str(packer_path),
        }
        return 0
    except EvidenceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    except Exception as exc:  # noqa: BLE001 - no failure may pass silently
        print(f"ERROR: unexpected {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
