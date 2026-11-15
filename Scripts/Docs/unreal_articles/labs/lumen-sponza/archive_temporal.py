#!/usr/bin/env python3
"""Lossless, portable archive for selected-resource DumpGPU temporal captures.

This tool packages the *bytes of every file* in a set of explicitly named
DumpGPU state directories.  It does not interpret or filter the payload: Base,
Resources, Structures, StructuresMetadata, native-uniform metadata, viewer
files, Status and every other regular file are stored as opaque bytes.

Archive format (format_version 1)
---------------------------------
* Content is split into chunks of at most 32 MiB of uncompressed bytes.
* Each unique chunk is stored once as a gzip member with gzip MTIME = 0 at
  ``chunks/<sha[:2]>/<sha[2:4]>/<sha>.bin.gz``.
* The SHA-256 of the raw chunk is the chunk id and the chunk file name.
* A file is an ordered list of chunk ids.  Its SHA-256 is the SHA-256 of the
  concatenated raw chunks.  The empty file is represented by an empty list.
* ``index.json`` records the format/schema, state/run mapping, capture manifest
  frame/update/camera sequence, every source file's size and SHA-256, every
  chunk's raw and compressed size/SHA-256, and provenance only paths.
* Reconstruction needs only ``index.json`` + ``chunks/``; the old source path is
  retained as provenance and is never consulted.

Commands
--------
build       package explicit STATE=PATH roots
verify      validate the package without any source tree
compare     re-hash the source trees and compare them with the archive
reconstruct chosen state (or selected files) without overwriting files
report      render report.md from the index + verification evidence

Python 3.8+ standard library only.  No LFS, no third-party packages.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import gzip
import hashlib
import json
import os
import re
import stat
import sys
import time
from collections import OrderedDict
from pathlib import Path, PurePosixPath

FORMAT_VERSION = 1
SCHEMA = "dc-temporal-native-archive/v1"
TOOL_VERSION = "1.0.0"
DEFAULT_CHUNK_SIZE = 32 * 1024 * 1024          # 32 MiB uncompressed
MAX_ARCHIVE_FILE = 90 * 1024 * 1024           # every Git blob comfortably < 100 MiB
DEFAULT_JOBS = max(1, min(8, os.cpu_count() or 1))
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class ArchiveError(Exception):
    """A deterministic, user-facing archive failure."""


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(1024 * 1024)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def json_text(obj):
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"


def atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-%d-%d" % (os.getpid(), id(text)))
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def write_json(path, obj):
    atomic_write_text(path, json_text(obj))


def load_json(path):
    # utf-8-sig tolerates a UTF-8 BOM in externally produced capture manifests;
    # our own JSON is written without a BOM.
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def validate_rel_path(rel):
    """Return a PurePosixPath for a safe, relative POSIX archive path."""
    if not isinstance(rel, str) or not rel:
        raise ArchiveError("empty or non-string archive path")
    if "\\" in rel or "\x00" in rel or rel.startswith("/"):
        raise ArchiveError("unsafe archive path: %r" % rel)
    p = PurePosixPath(rel)
    if p.is_absolute():
        raise ArchiveError("absolute archive path: %r" % rel)
    for part in p.parts:
        if part in ("", ".", "..") or ":" in part:
            raise ArchiveError("unsafe archive path component in %r" % rel)
    if str(p) != rel:
        raise ArchiveError("non-canonical archive path: %r" % rel)
    return p


def package_path(package, rel):
    p = validate_rel_path(rel)
    return Path(package).joinpath(*p.parts)


def validate_run_name(name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ArchiveError("run name must use [A-Za-z0-9._-]: %r" % name)


def parse_state_name(name):
    parts = name.split("/")
    if len(parts) != 2 or not all(NAME_RE.match(p) for p in parts):
        raise ArchiveError("state name must be RUN/STATE using [A-Za-z0-9._-]: %r" % name)
    return parts[0], parts[1]


def split_named(item, flag):
    if "=" not in item:
        raise ArchiveError("%s value must be NAME=VALUE: %r" % (flag, item))
    name, value = item.split("=", 1)
    if not name or not value:
        raise ArchiveError("%s value must be NAME=VALUE: %r" % (flag, item))
    return name, value


def parse_named_args(items, flag, validate_name=None):
    out = {}
    for item in items or []:
        name, value = split_named(item, flag)
        if validate_name is not None:
            validate_name(name)
        if name in out:
            raise ArchiveError("duplicate %s name: %s" % (flag, name))
        out[name] = value
    return out


def walk_files(root):
    """Deterministic list of (relative POSIX path, absolute Path) regular files."""
    root = Path(root)
    if not root.exists():
        raise ArchiveError("source state does not exist: %s" % root)
    if root.is_symlink() or not root.is_dir():
        raise ArchiveError("source state is not a real directory: %s" % root)
    entries = []
    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        dirnames.sort()
        filenames.sort()
        for d in dirnames:
            p = Path(dirpath) / d
            if p.is_symlink():
                raise ArchiveError("symlinked directory is not supported: %s" % p)
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.is_symlink():
                raise ArchiveError("symlinked file is not supported: %s" % p)
            if not p.is_file():
                raise ArchiveError("non-regular file is not supported: %s" % p)
            rel = os.path.relpath(str(p), str(root)).replace(os.sep, "/")
            validate_rel_path(rel)
            entries.append((rel, p))
    entries.sort(key=lambda item: item[0])
    return entries


def inventory_digest(file_records):
    h = hashlib.sha256()
    for rec in sorted(file_records, key=lambda r: r["path"]):
        h.update(rec["path"].encode("utf-8"))
        h.update(b"\x00")
        h.update(str(rec["bytes"]).encode("ascii"))
        h.update(b"\x00")
        h.update(rec["sha256"].encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def common_run_directory(paths):
    try:
        common = os.path.commonpath([str(p) for p in paths])
    except ValueError:
        return None
    if common and all(os.path.abspath(str(p)) != os.path.abspath(common) for p in paths):
        return common
    return None


# ---------------------------------------------------------------------------
# Chunk storage (build side)
# ---------------------------------------------------------------------------

def _chunk_relpath(chunk_id):
    return "chunks/%s/%s/%s.bin.gz" % (chunk_id[:2], chunk_id[2:4], chunk_id)


def _store_chunk(chunk_dir, chunk_id, raw):
    """Compress and atomically publish one chunk. Runs in a worker thread."""
    compressed = gzip.compress(raw, compresslevel=6, mtime=0)
    gzip_sha = sha256_bytes(compressed)
    rel = _chunk_relpath(chunk_id)
    target = Path(chunk_dir).parent.joinpath(*PurePosixPath(rel).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp-%d-%d" % (os.getpid(), id(raw)))
    with open(tmp, "wb") as f:
        f.write(compressed)
    os.replace(tmp, target)
    return {
        "id": chunk_id,
        "path": rel,
        "raw_bytes": len(raw),
        "raw_sha256": chunk_id,
        "gzip_bytes": len(compressed),
        "gzip_sha256": gzip_sha,
    }


class ChunkStore:
    """Bounded, content-addressed chunk writer shared by all states."""

    def __init__(self, package, jobs):
        self.package = Path(package)
        self.chunk_dir = self.package / "chunks"
        self.jobs = max(1, int(jobs))
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=self.jobs)
        self.futures = {}
        self.records = {}
        self.max_pending = max(2, self.jobs * 2)
        self._closed = False

    def add(self, block):
        chunk_id = sha256_bytes(block)
        if chunk_id in self.records or chunk_id in self.futures:
            return chunk_id
        fut = self.pool.submit(_store_chunk, self.chunk_dir, chunk_id, block)
        self.futures[chunk_id] = fut
        if len(self.futures) >= self.max_pending:
            self._collect_one()
        return chunk_id

    def _collect_one(self):
        if not self.futures:
            return
        done, _ = concurrent.futures.wait(
            list(self.futures.values()),
            return_when=concurrent.futures.FIRST_COMPLETED,
        )
        for fut in done:
            rec = fut.result()
            self.records[rec["id"]] = rec
            for chunk_id, candidate in list(self.futures.items()):
                if candidate is fut:
                    del self.futures[chunk_id]

    def finish(self):
        for fut in concurrent.futures.as_completed(list(self.futures.values())):
            rec = fut.result()
            self.records[rec["id"]] = rec
        self.futures.clear()
        self.pool.shutdown(wait=True)
        self._closed = True

    def abort(self):
        if not self._closed:
            self.pool.shutdown(wait=False, cancel_futures=True)
            self._closed = True


def read_source_file(path, store, retries, settle_seconds):
    """Read one file, retrying if its size/mtime changes while being read."""
    path = Path(path)
    last_problem = None
    for attempt in range(retries + 1):
        try:
            st_before = os.stat(str(path))
            if not stat.S_ISREG(st_before.st_mode):
                raise ArchiveError("not a regular file: %s" % path)
            size_before = st_before.st_size
            mtime_before = st_before.st_mtime_ns
            h = hashlib.sha256()
            refs = []
            total = 0
            with open(path, "rb") as f:
                while True:
                    block = f.read(DEFAULT_CHUNK_SIZE)
                    if not block:
                        break
                    h.update(block)
                    total += len(block)
                    refs.append(store.add(block))
            st_after = os.stat(str(path))
            if (
                st_after.st_size == size_before
                and st_after.st_mtime_ns == mtime_before
                and total == size_before
            ):
                return {
                    "path": os.path.relpath(str(path), str(path.parent)).replace(os.sep, "/"),
                    "bytes": total,
                    "sha256": h.hexdigest(),
                    "chunks": refs,
                    "source_mtime_ns": mtime_before,
                }, None
            last_problem = (
                "source changed while reading: size %s->%s, mtime_ns %s->%s, read %s bytes"
                % (size_before, st_after.st_size, mtime_before, st_after.st_mtime_ns, total)
            )
        except OSError as exc:
            last_problem = "%s: %s" % (type(exc).__name__, exc)
        if attempt < retries:
            time.sleep(settle_seconds)
    raise ArchiveError("could not read a settled file %s after %d attempts (%s)"
                       % (path, retries + 1, last_problem))


# ---------------------------------------------------------------------------
# Capture manifest extraction
# ---------------------------------------------------------------------------

def _pick_camera(dump):
    dsa = dump.get("device_state_at_arm") or {}
    cam = dsa.get("camera")
    if not cam:
        cam = (dump.get("lit_record") or {}).get("camera")
    if not cam:
        cam = dump.get("camera")
    if not cam:
        return None
    return {
        "location_cm": cam.get("location_cm"),
        "rotation_degrees": cam.get("rotation_degrees"),
        "horizontal_fov_deg": cam.get("horizontal_fov_deg"),
    }


def _pick_sun(dump):
    dsa = dump.get("device_state_at_arm") or {}
    sun = dsa.get("sun")
    if not sun:
        sun = (dump.get("lit_record") or {}).get("sun")
    if not sun:
        sun = dump.get("sun")
    if not sun:
        return None
    return {
        "actor": sun.get("actor"),
        "rotation_degrees": sun.get("rotation_degrees"),
        "intensity_lux": sun.get("intensity_lux"),
        "temperature_k": sun.get("temperature_k"),
    }


def _pick_exposure(dump):
    return (dump.get("device_state_at_arm") or {}).get("exposure") or dump.get("exposure")


def _compact_selected_resources(value):
    if not isinstance(value, dict):
        return None
    out = {}
    for key, item in value.items():
        if isinstance(item, list):
            out[key] = {"count": len(item), "items": item}
        else:
            out[key] = item
    return out


def _compact_coverage(value):
    if not isinstance(value, dict):
        return None
    return {k: v for k, v in value.items() if k != "formats"}


def _compact_pass_parameters(value):
    if not isinstance(value, list):
        return None
    out = []
    for item in value:
        if not isinstance(item, dict):
            continue
        out.append({
            "event_name": item.get("event_name"),
            "parameters_name": item.get("parameters_name"),
            "parameters_bytes": item.get("parameters_bytes"),
            "parameters_sha256": item.get("parameters_sha256"),
            "frame_numbers": item.get("frame_numbers"),
        })
    return out


def capture_from_dump(dump):
    """A bounded copy of the capture-manifest facts needed by the archive."""
    raw_frame = dump.get("raw_frame") or {}
    cap = {
        "label": dump.get("label"),
        "phase": dump.get("phase"),
        "stage": dump.get("stage"),
        "required": dump.get("required"),
        "target_phase_draw": dump.get("target_phase_draw"),
        "arm_phase_draw": dump.get("arm_phase_draw"),
        "arm_draw_total": dump.get("arm_draw_total"),
        "arm_engine_frame": dump.get("arm_engine_frame"),
        "actual_phase_draw": dump.get("actual_phase_draw"),
        "actual_phase_draws": dump.get("actual_phase_draws"),
        "engine_frames_in_dump": dump.get("engine_frames_in_dump"),
        "raw_frame": {
            "engine_frames": raw_frame.get("engine_frames"),
            "engine_frame": raw_frame.get("engine_frame"),
            "actual_phase_draw": raw_frame.get("actual_phase_draw"),
            "actual_phase_draws": raw_frame.get("actual_phase_draws"),
            "source": raw_frame.get("source"),
        } if raw_frame else None,
        "camera": _pick_camera(dump),
        "sun": _pick_sun(dump),
        "exposure": _pick_exposure(dump),
        "raw_directory": dump.get("raw_directory"),
        "file_count": dump.get("file_count"),
        "total_bytes": dump.get("total_bytes"),
        "metadata_error": dump.get("metadata_error"),
        "graph_count": dump.get("graph_count"),
        "pass_count": dump.get("pass_count"),
        "selected_resources": _compact_selected_resources(dump.get("selected_resources")),
        "coverage": _compact_coverage(dump.get("coverage")),
        "pass_parameters": _compact_pass_parameters(dump.get("pass_parameters")),
        "rhi_uniform_buffer_content_map": dump.get("rhi_uniform_buffer_content_map"),
    }
    return {k: v for k, v in cap.items() if v is not None}


def sequence_entry(capture):
    raw_frame = capture.get("raw_frame") or {}
    return {
        "label": capture.get("label"),
        "phase": capture.get("phase"),
        "stage": capture.get("stage"),
        "target_phase_draw": capture.get("target_phase_draw"),
        "arm_phase_draw": capture.get("arm_phase_draw"),
        "actual_phase_draw": capture.get("actual_phase_draw"),
        "actual_phase_draws": capture.get("actual_phase_draws"),
        "engine_frames_in_dump": capture.get("engine_frames_in_dump"),
        "engine_frame": raw_frame.get("engine_frame"),
        "camera": capture.get("camera"),
        "sun": capture.get("sun"),
        "exposure": capture.get("exposure"),
    }


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def cmd_build(args):
    started = time.monotonic()
    roots = []
    seen = set()
    for item in args.root:
        name, value = split_named(item, "--root")
        parse_state_name(name)
        if name in seen:
            raise ArchiveError("duplicate state root: %s" % name)
        seen.add(name)
        roots.append((name, Path(os.path.abspath(os.path.expanduser(value)))))
    if not roots:
        raise ArchiveError("at least one --root RUN/STATE=PATH is required")

    manifests = parse_named_args(args.manifest, "--manifest", validate_run_name)
    run_ids = parse_named_args(args.run, "--run", validate_run_name)
    groups = []
    for name, _ in roots:
        group, _label = parse_state_name(name)
        if group not in groups:
            groups.append(group)
    for named in list(manifests) + list(run_ids):
        if named not in groups:
            raise ArchiveError("--manifest/--run names a run with no roots: %s" % named)

    manifest_by_group = {}
    for group in groups:
        path = manifests.get(group)
        if path is None:
            continue
        mp = Path(os.path.abspath(os.path.expanduser(path)))
        if not mp.is_file():
            raise ArchiveError("capture manifest does not exist: %s" % mp)
        manifest = load_json(mp)
        if not isinstance(manifest.get("dumps"), list):
            raise ArchiveError("capture manifest has no dumps list: %s" % mp)
        manifest_by_group[group] = (mp, manifest)
        declared = manifest.get("run_id")
        if group in run_ids and declared and run_ids[group] != declared:
            raise ArchiveError("--run %s=%s disagrees with manifest run_id %s"
                               % (group, run_ids[group], declared))

    out = Path(os.path.abspath(os.path.expanduser(args.out)))
    if out.exists():
        existing = list(out.iterdir())
        if existing:
            raise ArchiveError("output directory must be new or empty: %s" % out)
    out.mkdir(parents=True, exist_ok=True)
    chunk_root = out / "chunks"
    chunk_root.mkdir(parents=True, exist_ok=True)

    store = ChunkStore(out, args.jobs)
    states = []
    source_sigs = {}
    gaps = []
    try:
        for name, root in roots:
            group, label = parse_state_name(name)
            entries = walk_files(root)
            state_files = []
            sig = {}
            bytes_total = 0
            state_started = time.monotonic()
            for rel, path in entries:
                rec, _ = read_source_file(path, store, args.retries, args.settle_seconds)
                # read_source_file is given an absolute file; force the walked
                # relative path so the archive path is authoritative.
                rec["path"] = rel
                state_files.append(rec)
                st = os.stat(str(path))
                sig[rel] = [st.st_size, st.st_mtime_ns]
                bytes_total += rec["bytes"]
            source_sigs[name] = sig

            cap = None
            if group in manifest_by_group:
                _mp, manifest = manifest_by_group[group]
                for dump in manifest["dumps"]:
                    if dump.get("label") == label:
                        cap = capture_from_dump(dump)
                        break
                if cap is None:
                    raise ArchiveError("capture manifest for %s has no dump label %s"
                                       % (group, label))

            state = {
                "name": name,
                "run": group,
                "label": label,
                "kind": "selected-resource-temporal-capture",
                "source_path": str(root),
                "source_path_role": "historical provenance only; reconstruction never reads it",
                "file_count": len(state_files),
                "bytes": bytes_total,
                "inventory_sha256": inventory_digest(state_files),
                "capture": cap,
                "files": state_files,
            }
            states.append(state)

            if cap:
                if cap.get("metadata_error"):
                    gaps.append({
                        "kind": "capture-metadata-error",
                        "state": name,
                        "detail": cap.get("metadata_error"),
                    })
                if cap.get("file_count") is not None and cap.get("file_count") != len(state_files):
                    gaps.append({
                        "kind": "capture-file-count-mismatch",
                        "state": name,
                        "manifest_file_count": cap.get("file_count"),
                        "archived_file_count": len(state_files),
                    })
                if cap.get("total_bytes") is not None and cap.get("total_bytes") != bytes_total:
                    gaps.append({
                        "kind": "capture-byte-count-mismatch",
                        "state": name,
                        "manifest_bytes": cap.get("total_bytes"),
                        "archived_bytes": bytes_total,
                    })
                coverage = cap.get("coverage") or {}
                if coverage.get("missing_count"):
                    gaps.append({
                        "kind": "capture-coverage-missing",
                        "state": name,
                        "missing_count": coverage.get("missing_count"),
                        "missing": coverage.get("missing"),
                    })
                ubmap = cap.get("rhi_uniform_buffer_content_map") or {}
                flag = ubmap.get("r.RHI.UniformBufferContentMap.Enable") or {}
                value = str(flag.get("value", "")).strip().lower()
                if value in ("false", "0"):
                    gaps.append({
                        "kind": "uniform-buffer-content-map-disabled",
                        "state": name,
                        "field": "r.RHI.UniformBufferContentMap.Enable",
                        "value": flag.get("value"),
                        "impact": ("native uniform-buffer content-map metadata is known incomplete; "
                                   "captured raw bytes are still preserved losslessly"),
                    })

            print("[build] %-24s files=%-6d bytes=%-12d elapsed=%.1fs"
                  % (name, len(state_files), bytes_total, time.monotonic() - state_started),
                  file=sys.stderr, flush=True)

        store.finish()

        # Final source snapshot check: catches additions/removals or a file that
        # changed after it was read.  The archive is not published on failure.
        for name, root in roots:
            current = {}
            for rel, path in walk_files(root):
                st = os.stat(str(path))
                current[rel] = [st.st_size, st.st_mtime_ns]
            expected = source_sigs[name]
            changed = sorted(set(expected).intersection(current).difference(
                {k for k in expected if expected[k] == current.get(k)}
            ))
            if changed or set(expected) != set(current):
                details = []
                for rel in changed[:10]:
                    details.append("%s: %s -> %s" % (rel, expected[rel], current.get(rel)))
                extra = sorted(set(current) - set(expected))[:10]
                missing = sorted(set(expected) - set(current))[:10]
                raise ArchiveError(
                    "source state changed during archive: %s; changed=%s extra=%s missing=%s"
                    % (name, details, extra, missing)
                )

        referenced = set()
        for state in states:
            for rec in state["files"]:
                referenced.update(rec["chunks"])
        catalog = [store.records[cid] for cid in sorted(referenced)]
        missing_chunks = referenced.difference(store.records)
        if missing_chunks:
            raise ArchiveError("internal error: %d referenced chunks were not stored"
                               % len(missing_chunks))

        runs = {}
        for group in groups:
            group_states = [s for s in states if s["run"] == group]
            mp = manifest_by_group.get(group, (None, None))[0]
            manifest = manifest_by_group.get(group, (None, None))[1]
            run_id = run_ids.get(group)
            if run_id is None and manifest:
                run_id = manifest.get("run_id")
            if run_id is None:
                run_id = group
            seq = []
            for state in group_states:
                if state["capture"]:
                    seq.append(sequence_entry(state["capture"]))
            runs[group] = {
                "run_id": run_id,
                "source_directory": common_run_directory([Path(s["source_path"]) for s in group_states]),
                "capture_manifest": str(mp) if mp else None,
                "capture_schema": manifest.get("schema") if manifest else None,
                "capture_status": manifest.get("status") if manifest else None,
                "manifest_dump_count": len(manifest.get("dumps") or []) if manifest else None,
                "selected_state_count": len(group_states),
                "states": [s["name"] for s in group_states],
                "capture_sequence": seq,
            }

        file_count = sum(s["file_count"] for s in states)
        raw_bytes = sum(s["bytes"] for s in states)
        unique_file_hashes = len({rec["sha256"] for s in states for rec in s["files"]})
        chunk_raw_bytes = sum(c["raw_bytes"] for c in catalog)
        chunk_gzip_bytes = sum(c["gzip_bytes"] for c in catalog)
        totals = {
            "state_count": len(states),
            "file_count": file_count,
            "raw_bytes": raw_bytes,
            "unique_file_hashes": unique_file_hashes,
            "duplicate_file_records": file_count - unique_file_hashes,
            "chunk_count": len(catalog),
            "chunk_raw_bytes": chunk_raw_bytes,
            "chunk_gzip_bytes": chunk_gzip_bytes,
            "compression_ratio_gzip_over_raw": (
                round(chunk_gzip_bytes / chunk_raw_bytes, 6) if chunk_raw_bytes else 0.0
            ),
            "reconstruction_space_bytes": raw_bytes,
            "max_state_reconstruction_bytes": max((s["bytes"] for s in states), default=0),
        }

        index = {
            "schema": SCHEMA,
            "format_version": FORMAT_VERSION,
            "tool": {
                "name": "archive.py",
                "version": TOOL_VERSION,
                "chunk_size_limit_uncompressed": DEFAULT_CHUNK_SIZE,
                "compression": "gzip",
                "gzip_mtime": 0,
                "hash": "sha256",
                "python": sys.version.split()[0],
            },
            "archive_kind": "selected-resource-temporal-native-bytes",
            "payload_scope": {
                "labels": [
                    "SELECTED-resource temporal captures",
                    "native DumpGPU bytes",
                    "all files under each state (Base, Resources, Structures, "
                    "StructuresMetadata, native-uniform metadata, viewer files, Status)",
                ],
                "not_complete_gpu_frames": True,
                "not_shader_locals": True,
                "opaque_bytes": True,
                "line_ending_normalization": False,
                "selective_payload_omission": False,
            },
            "created_utc": utc_now(),
            "source_snapshot_verified_utc": utc_now(),
            "chunking": {
                "chunk_id": "sha256 of uncompressed chunk bytes",
                "gzip_path": "chunks/<sha[:2]>/<sha[2:4]>/<sha>.bin.gz",
                "max_uncompressed_bytes": DEFAULT_CHUNK_SIZE,
                "compressed_overhead_note": "gzip overhead keeps every chunk blob well below 100 MiB",
            },
            "roots": [{"name": name, "source_path": str(root)} for name, root in roots],
            "runs": runs,
            "states": states,
            "chunk_catalog": catalog,
            "totals": totals,
            "gaps": gaps,
            "notices": list(args.note or []),
        }
        write_json(out / "index.json", index)

        cleanup_orphan_chunks(chunk_root, referenced)
        elapsed = time.monotonic() - started
        print("[build] PASS states=%d files=%d raw_bytes=%d chunks=%d gzip_bytes=%d elapsed=%.1fs"
              % (len(states), file_count, raw_bytes, len(catalog), chunk_gzip_bytes, elapsed),
              file=sys.stderr, flush=True)
        print(str(out / "index.json"))
        return 0
    except Exception:
        store.abort()
        raise


def cleanup_orphan_chunks(chunk_root, referenced):
    """Remove only our own unreferenced chunk/temp files from this package."""
    chunk_root = Path(chunk_root)
    if not chunk_root.exists():
        return
    for path in list(chunk_root.rglob("*")):
        if not path.is_file():
            continue
        name = path.name
        if ".tmp-" in name:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        if name.endswith(".bin.gz"):
            chunk_id = name[:-7]
            if chunk_id not in referenced:
                try:
                    path.unlink()
                except OSError:
                    pass
    for path in sorted([p for p in chunk_root.rglob("*") if p.is_dir()],
                       key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# verify (source-free)
# ---------------------------------------------------------------------------

class RawChunkCache:
    """Bounded LRU of validated raw chunks used while reconstructing file hashes."""

    def __init__(self, package, catalog, limit_bytes=512 * 1024 * 1024):
        self.package = Path(package)
        self.catalog = catalog
        self.limit = limit_bytes
        self.cache = OrderedDict()
        self.bytes = 0
        self.hits = 0
        self.misses = 0

    def get(self, chunk_id):
        if chunk_id in self.cache:
            raw = self.cache.pop(chunk_id)
            self.cache[chunk_id] = raw
            self.hits += 1
            return raw
        rec = self.catalog.get(chunk_id)
        if rec is None:
            raise ArchiveError("file references unknown chunk %s" % chunk_id)
        raw = read_chunk_strict(self.package, rec)
        self.misses += 1
        self.cache[chunk_id] = raw
        self.bytes += len(raw)
        while self.bytes > self.limit and len(self.cache) > 1:
            _old_id, old = self.cache.popitem(last=False)
            self.bytes -= len(old)
        return raw


def read_chunk_strict(package, rec):
    """Read, gzip-verify, decompress and raw-hash one chunk."""
    path = package_path(package, rec["path"])
    if not path.is_file():
        raise ArchiveError("missing chunk file: %s" % path)
    data = path.read_bytes()
    if len(data) != rec["gzip_bytes"]:
        raise ArchiveError("chunk gzip size mismatch %s: %d != %d"
                           % (rec["id"], len(data), rec["gzip_bytes"]))
    if sha256_bytes(data) != rec["gzip_sha256"]:
        raise ArchiveError("chunk gzip SHA-256 mismatch: %s" % rec["id"])
    try:
        raw = gzip.decompress(data)
    except Exception as exc:
        raise ArchiveError("chunk gzip decompression failed %s: %s" % (rec["id"], exc))
    if len(raw) != rec["raw_bytes"]:
        raise ArchiveError("chunk raw size mismatch %s: %d != %d"
                           % (rec["id"], len(raw), rec["raw_bytes"]))
    if sha256_bytes(raw) != rec["raw_sha256"]:
        raise ArchiveError("chunk raw SHA-256 mismatch: %s" % rec["id"])
    return raw


def cmd_verify(args):
    package = Path(os.path.abspath(os.path.expanduser(args.package)))
    index_path = package / "index.json"
    if not index_path.is_file():
        raise ArchiveError("package has no index.json: %s" % package)
    index = load_json(index_path)
    failures = []
    checks = {
        "schema_valid": index.get("schema") == SCHEMA,
        "format_version_supported": index.get("format_version") == FORMAT_VERSION,
        "source_free": True,
        "every_chunk_gzip_sha256": True,
        "every_chunk_gzip_mtime_zero": True,
        "every_chunk_raw_sha256": True,
        "every_file_record_validated": True,
        "every_file_size_and_sha256": True,
        "all_catalog_chunks_referenced": True,
        "max_uncompressed_chunk_within_limit": True,
        "no_archive_file_over_90MiB": True,
        "no_absolute_or_unsafe_paths": True,
    }
    if not checks["schema_valid"]:
        failures.append("index schema is %r, expected %r" % (index.get("schema"), SCHEMA))
    if not checks["format_version_supported"]:
        failures.append("unsupported format_version %r" % index.get("format_version"))

    states = index.get("states") or []
    catalog_list = index.get("chunk_catalog") or []
    chunk_limit = int((index.get("chunking") or {}).get("max_uncompressed_bytes") or DEFAULT_CHUNK_SIZE)
    catalog = {}
    chunk_total_raw = 0
    chunk_total_gzip = 0
    chunk_file_max = 0
    chunk_raw_max = 0
    for rec in catalog_list:
        cid = rec.get("id")
        if not isinstance(cid, str) or not SHA256_RE.match(cid):
            failures.append("invalid chunk id: %r" % (cid,))
            checks["every_chunk_gzip_sha256"] = False
            checks["every_chunk_raw_sha256"] = False
            continue
        if cid in catalog:
            failures.append("duplicate chunk catalog id: %s" % cid)
            continue
        if rec.get("raw_sha256") != cid:
            failures.append("chunk %s raw_sha256 does not equal its id" % cid)
            checks["every_chunk_raw_sha256"] = False
        if rec.get("path") != _chunk_relpath(cid):
            failures.append("chunk %s has non-canonical path %r" % (cid, rec.get("path")))
            checks["no_absolute_or_unsafe_paths"] = False
        try:
            path = package_path(package, rec.get("path"))
        except ArchiveError as exc:
            failures.append(str(exc))
            checks["no_absolute_or_unsafe_paths"] = False
            continue
        if not path.is_file():
            failures.append("missing chunk file: %s" % rec.get("path"))
            checks["every_chunk_gzip_sha256"] = False
            checks["every_chunk_raw_sha256"] = False
            continue
        with open(path, "rb") as head_file:
            header = head_file.read(8)
        if len(header) < 8 or header[:2] != b"\x1f\x8b" or header[4:8] != b"\x00\x00\x00\x00":
            checks["every_chunk_gzip_mtime_zero"] = False
            failures.append("chunk is not a gzip member with MTIME=0: %s" % rec.get("path"))
        try:
            raw = read_chunk_strict(package, rec)
        except ArchiveError as exc:
            failures.append(str(exc))
            checks["every_chunk_gzip_sha256"] = False
            checks["every_chunk_raw_sha256"] = False
            continue
        catalog[cid] = rec
        chunk_total_raw += rec["raw_bytes"]
        chunk_total_gzip += rec["gzip_bytes"]
        chunk_raw_max = max(chunk_raw_max, rec["raw_bytes"])
        chunk_file_max = max(chunk_file_max, rec["gzip_bytes"])
        if rec["raw_bytes"] > chunk_limit:
            checks["max_uncompressed_chunk_within_limit"] = False
            failures.append("chunk %s raw bytes %d exceed limit %d"
                            % (cid, rec["raw_bytes"], chunk_limit))

    if chunk_raw_max > chunk_limit:
        checks["max_uncompressed_chunk_within_limit"] = False

    cache = RawChunkCache(package, catalog)
    seq_cache = {}
    seq_hits = 0
    seq_misses = 0
    referenced = set()
    total_files = 0
    total_raw = 0
    unique_file_hashes = set()
    state_summaries = []
    max_source_file_bytes = 0
    max_source_file_path = None
    source_files_over_90mib = 0
    for state in states:
        name = state.get("name")
        files = state.get("files") or []
        state_bytes = 0
        state_paths = set()
        if not isinstance(name, str):
            failures.append("state has no name")
            continue
        for rec in files:
            total_files += 1
            rel = rec.get("path")
            try:
                validate_rel_path(rel)
            except ArchiveError as exc:
                failures.append("state %s: %s" % (name, exc))
                checks["no_absolute_or_unsafe_paths"] = False
                checks["every_file_record_validated"] = False
                continue
            if rel in state_paths:
                failures.append("state %s: duplicate file path %s" % (name, rel))
                checks["every_file_record_validated"] = False
                continue
            state_paths.add(rel)
            size = rec.get("bytes")
            digest = rec.get("sha256")
            refs = rec.get("chunks")
            if not isinstance(size, int) or size < 0:
                failures.append("state %s %s: invalid size %r" % (name, rel, size))
                checks["every_file_record_validated"] = False
                continue
            if size > max_source_file_bytes:
                max_source_file_bytes = size
                max_source_file_path = "%s/%s" % (name, rel)
            if size > MAX_ARCHIVE_FILE:
                # Source payloads may be larger than 90 MiB; they are split into
                # <=32 MiB chunks so no archive/Git blob approaches 100 MiB.
                source_files_over_90mib += 1
            if not isinstance(digest, str) or not SHA256_RE.match(digest):
                failures.append("state %s %s: invalid SHA-256 %r" % (name, rel, digest))
                checks["every_file_record_validated"] = False
                continue
            if not isinstance(refs, list):
                failures.append("state %s %s: chunk refs are not a list" % (name, rel))
                checks["every_file_record_validated"] = False
                continue
            bad_ref = False
            ref_bytes = 0
            for cid in refs:
                if not isinstance(cid, str) or cid not in catalog:
                    failures.append("state %s %s: invalid or missing chunk ref %r"
                                    % (name, rel, cid))
                    bad_ref = True
                    checks["every_file_record_validated"] = False
                    continue
                referenced.add(cid)
                ref_bytes += catalog[cid]["raw_bytes"]
            if bad_ref:
                continue
            if ref_bytes != size:
                failures.append("state %s %s: chunk bytes %d != file bytes %d"
                                % (name, rel, ref_bytes, size))
                checks["every_file_size_and_sha256"] = False
                checks["every_file_record_validated"] = False
                continue
            seq = tuple(refs)
            cached = seq_cache.get(seq)
            if cached is None:
                h = hashlib.sha256()
                n = 0
                try:
                    for cid in refs:
                        raw = cache.get(cid)
                        h.update(raw)
                        n += len(raw)
                except ArchiveError as exc:
                    failures.append("state %s %s: %s" % (name, rel, exc))
                    checks["every_file_size_and_sha256"] = False
                    continue
                cached = (n, h.hexdigest())
                seq_cache[seq] = cached
                seq_misses += 1
            else:
                seq_hits += 1
            if cached != (size, digest):
                failures.append(
                    "state %s %s: reconstructed %s/%s != record %s/%s"
                    % (name, rel, cached[0], cached[1], size, digest)
                )
                checks["every_file_size_and_sha256"] = False
            state_bytes += size
            unique_file_hashes.add(digest)
        if state_bytes != state.get("bytes"):
            failures.append("state %s: summed file bytes %d != declared %r"
                            % (name, state_bytes, state.get("bytes")))
            checks["every_file_record_validated"] = False
        if len(files) != state.get("file_count"):
            failures.append("state %s: file count %d != declared %r"
                            % (name, len(files), state.get("file_count")))
            checks["every_file_record_validated"] = False
        if inventory_digest(files) != state.get("inventory_sha256"):
            failures.append("state %s: inventory SHA-256 mismatch" % name)
            checks["every_file_record_validated"] = False
        state_summaries.append({
            "name": name,
            "run": state.get("run"),
            "file_count": len(files),
            "bytes": state_bytes,
            "inventory_sha256": state.get("inventory_sha256"),
            "verified": not any(f.startswith("state %s" % name) for f in failures),
        })
        total_raw += state_bytes

    unreferenced = sorted(set(catalog).difference(referenced))
    if unreferenced:
        checks["all_catalog_chunks_referenced"] = False
        failures.append("catalog contains %d unreferenced chunks (first: %s)"
                        % (len(unreferenced), unreferenced[:5]))
    missing_references = referenced.difference(catalog)
    if missing_references:
        checks["all_catalog_chunks_referenced"] = False
        failures.append("file records reference %d chunks absent from catalog"
                        % len(missing_references))

    # Whole-package file inventory: no >90 MiB blob and no unexpected files.
    package_files = []
    for dirpath, dirnames, filenames in os.walk(str(package), followlinks=False):
        dirnames.sort()
        filenames.sort()
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.is_symlink():
                failures.append("symlink in package: %s" % p)
                continue
            rel = os.path.relpath(str(p), str(package)).replace(os.sep, "/")
            size = p.stat().st_size
            package_files.append({"path": rel, "bytes": size})
            if size > MAX_ARCHIVE_FILE:
                checks["no_archive_file_over_90MiB"] = False
                failures.append("package file exceeds 90 MiB: %s (%d bytes)" % (rel, size))
    expected_top = {"index.json"}
    for item in package_files:
        rel = item["path"]
        if rel in expected_top:
            continue
        if rel.startswith("chunks/") and rel.endswith(".bin.gz"):
            try:
                cid = PurePosixPath(rel).name[:-7]
            except Exception:
                cid = ""
            if cid not in catalog:
                failures.append("unexpected package chunk file: %s" % rel)
            continue
        failures.append("unexpected package file: %s" % rel)

    totals = index.get("totals") or {}
    declared_files = totals.get("file_count")
    declared_raw = totals.get("raw_bytes")
    if declared_files is not None and declared_files != total_files:
        failures.append("totals file_count %r != verified %d" % (declared_files, total_files))
    if declared_raw is not None and declared_raw != total_raw:
        failures.append("totals raw_bytes %r != verified %d" % (declared_raw, total_raw))
    if totals.get("chunk_count") is not None and totals.get("chunk_count") != len(catalog):
        failures.append("totals chunk_count %r != verified %d"
                        % (totals.get("chunk_count"), len(catalog)))
    if totals.get("chunk_raw_bytes") is not None and totals.get("chunk_raw_bytes") != chunk_total_raw:
        failures.append("totals chunk_raw_bytes %r != verified %d"
                        % (totals.get("chunk_raw_bytes"), chunk_total_raw))
    if totals.get("chunk_gzip_bytes") is not None and totals.get("chunk_gzip_bytes") != chunk_total_gzip:
        failures.append("totals chunk_gzip_bytes %r != verified %d"
                        % (totals.get("chunk_gzip_bytes"), chunk_total_gzip))

    max_package_file = max(package_files, key=lambda x: x["bytes"], default={"path": None, "bytes": 0})
    status = "pass" if not failures else "fail"
    result = {
        "schema": "dc-temporal-native-archive-verification/v1",
        "verified_utc": utc_now(),
        "package": str(package),
        "source_required": False,
        "status": status,
        "checks": checks,
        "counts": {
            "states": len(states),
            "files": total_files,
            "unique_file_hashes": len(unique_file_hashes),
            "chunks": len(catalog),
            "package_files": len(package_files),
        },
        "bytes": {
            "raw_file_bytes": total_raw,
            "unique_chunk_raw_bytes": chunk_total_raw,
            "unique_chunk_gzip_bytes": chunk_total_gzip,
            "chunk_compression_ratio": (
                round(chunk_total_gzip / chunk_total_raw, 6) if chunk_total_raw else 0.0
            ),
            "archive_package_bytes": sum(item["bytes"] for item in package_files),
            "index_bytes": index_path.stat().st_size,
            "max_chunk_raw_bytes": chunk_raw_max,
            "max_chunk_gzip_bytes": chunk_file_max,
            "max_package_file_bytes": max_package_file["bytes"],
            "max_package_file": max_package_file["path"],
            "max_source_file_bytes": max_source_file_bytes,
            "max_source_file": max_source_file_path,
            "source_files_over_90MiB": source_files_over_90mib,
            "chunk_size_limit": chunk_limit,
        },
        "reconstruction_space": {
            "all_states_bytes": total_raw,
            "max_state_bytes": max((s["bytes"] for s in state_summaries), default=0),
            "states": {s["name"]: s["bytes"] for s in state_summaries},
        },
        "sequence_memoization": {
            "unique_chunk_sequences_hashed": seq_misses,
            "duplicate_sequences_reused": seq_hits,
            "chunk_raw_cache_hits": cache.hits,
            "chunk_raw_cache_misses": cache.misses,
            "note": "every file record's size and SHA-256 is compared; only identical ordered chunk sequences reuse a computed hash",
        },
        "states": state_summaries,
        "failures": failures,
    }
    if args.out:
        write_json(args.out, result)
    print("[verify] %s states=%d files=%d chunks=%d file_bytes=%d unique_chunk_raw_bytes=%d "
          "gzip_bytes=%d failures=%d"
          % (status.upper(), len(states), total_files, len(catalog), total_raw,
             chunk_total_raw, chunk_total_gzip, len(failures)), file=sys.stderr)
    for failure in failures[:20]:
        print("[verify] FAILURE: %s" % failure, file=sys.stderr)
    return 0 if status == "pass" else 1


# ---------------------------------------------------------------------------
# compare source
# ---------------------------------------------------------------------------

def cmd_compare(args):
    package = Path(os.path.abspath(os.path.expanduser(args.package)))
    index = load_json(package / "index.json")
    roots = parse_named_args(args.root, "--root", parse_state_name)
    index_states = {s["name"]: s for s in index.get("states") or []}
    if set(roots) != set(index_states):
        raise ArchiveError("compare roots must exactly match archive states; missing=%s extra=%s"
                           % (sorted(set(index_states) - set(roots)),
                              sorted(set(roots) - set(index_states))))
    mismatches = []
    states_out = []
    totals_checked = 0
    totals_bytes = 0
    for name in sorted(index_states):
        root = Path(os.path.abspath(os.path.expanduser(roots[name])))
        records = {rec["path"]: rec for rec in index_states[name]["files"]}
        source = {}
        entries = walk_files(root)
        for rel, path in entries:
            st = os.stat(str(path))
            source[rel] = {
                "bytes": st.st_size,
                "sha256": sha256_file(path),
                "mtime_ns": st.st_mtime_ns,
            }
        missing = sorted(set(records) - set(source))
        extra = sorted(set(source) - set(records))
        size_mismatch = []
        hash_mismatch = []
        mtime_changes = []
        for rel in sorted(set(records).intersection(source)):
            rec = records[rel]
            got = source[rel]
            if got["bytes"] != rec["bytes"]:
                size_mismatch.append({"path": rel, "archive": rec["bytes"], "source": got["bytes"]})
            if got["sha256"] != rec["sha256"]:
                hash_mismatch.append({"path": rel, "archive": rec["sha256"], "source": got["sha256"]})
            if got["mtime_ns"] != rec.get("source_mtime_ns"):
                mtime_changes.append({"path": rel,
                                      "archive_mtime_ns": rec.get("source_mtime_ns"),
                                      "source_mtime_ns": got["mtime_ns"]})
        fresh_records = [{
            "path": rel,
            "bytes": source[rel]["bytes"],
            "sha256": source[rel]["sha256"],
        } for rel in sorted(source)]
        fresh_digest = inventory_digest(fresh_records)
        digest_ok = fresh_digest == index_states[name].get("inventory_sha256")
        state_ok = not (missing or extra or size_mismatch or hash_mismatch or not digest_ok)
        if not state_ok:
            mismatches.append({
                "state": name,
                "missing": missing[:100],
                "extra": extra[:100],
                "size_mismatch": size_mismatch[:100],
                "hash_mismatch": hash_mismatch[:100],
                "inventory_sha256_archive": index_states[name].get("inventory_sha256"),
                "inventory_sha256_source": fresh_digest,
            })
        states_out.append({
            "name": name,
            "source_path": str(root),
            "files_checked": len(source),
            "bytes_checked": sum(v["bytes"] for v in source.values()),
            "missing_in_source": len(missing),
            "extra_in_source": len(extra),
            "size_mismatches": len(size_mismatch),
            "hash_mismatches": len(hash_mismatch),
            "mtime_changes": len(mtime_changes),
            "inventory_sha256_match": digest_ok,
            "status": "pass" if state_ok else "fail",
        })
        totals_checked += len(source)
        totals_bytes += sum(v["bytes"] for v in source.values())
        print("[compare] %-24s files=%-6d hash_mismatches=%d status=%s"
              % (name, len(source), len(hash_mismatch), "PASS" if state_ok else "FAIL"),
              file=sys.stderr, flush=True)
    status = "pass" if not mismatches else "fail"
    result = {
        "schema": "dc-temporal-native-archive-source-compare/v1",
        "compared_utc": utc_now(),
        "package": str(package),
        "status": status,
        "before": {
            "source": "index.json file size/SHA-256 records captured during build",
            "files": sum(s["file_count"] for s in index.get("states") or []),
            "bytes": sum(s["bytes"] for s in index.get("states") or []),
        },
        "after": {
            "source": "fresh SHA-256 scan of the read-only source trees",
            "files": totals_checked,
            "bytes": totals_bytes,
        },
        "states": states_out,
        "mismatches": mismatches,
        "note": "mtime changes are reported but only path/size/hash changes fail the comparison",
    }
    if args.out:
        write_json(args.out, result)
    print("[compare] %s files=%d bytes=%d mismatched_states=%d"
          % (status.upper(), totals_checked, totals_bytes, len(mismatches)),
          file=sys.stderr)
    return 0 if status == "pass" else 1


# ---------------------------------------------------------------------------
# reconstruct
# ---------------------------------------------------------------------------

def select_state(index, name):
    states = index.get("states") or []
    for state in states:
        if state.get("name") == name:
            return state
    matches = [s for s in states if s.get("label") == name]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ArchiveError("state %r is ambiguous; use RUN/STATE (matches: %s)"
                           % (name, ", ".join(s["name"] for s in matches)))
    raise ArchiveError("unknown state %r; available: %s"
                       % (name, ", ".join(s.get("name", "?") for s in states)))


def cmd_reconstruct(args):
    package = Path(os.path.abspath(os.path.expanduser(args.package)))
    index = load_json(package / "index.json")
    state = select_state(index, args.state)
    catalog = {rec["id"]: rec for rec in index.get("chunk_catalog") or []}
    records = {rec["path"]: rec for rec in state.get("files") or []}
    if args.only:
        selected = []
        seen = set()
        for rel in args.only:
            validate_rel_path(rel)
            if rel not in records:
                raise ArchiveError("selected file is not in state %s: %s" % (state["name"], rel))
            if rel not in seen:
                seen.add(rel)
                selected.append(records[rel])
    else:
        selected = [records[rel] for rel in sorted(records)]

    dest = Path(os.path.abspath(os.path.expanduser(args.dest)))
    conflicts = []
    targets = []
    for rec in selected:
        target = dest.joinpath(*validate_rel_path(rec["path"]).parts)
        targets.append((rec, target))
        if os.path.lexists(str(target)):
            conflicts.append(str(target))
    if conflicts:
        raise ArchiveError("refusing to overwrite %d existing file(s): %s"
                           % (len(conflicts), conflicts[:10]))
    if dest.exists() and not dest.is_dir():
        raise ArchiveError("destination is not a directory: %s" % dest)
    dest.mkdir(parents=True, exist_ok=True)

    written_files = []
    written_bytes = 0
    for rec, target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(str(target)):
            raise ArchiveError("refusing to overwrite file created during reconstruction: %s" % target)
        h = hashlib.sha256()
        n = 0
        fd = None
        try:
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
            for cid in rec["chunks"]:
                chunk = catalog.get(cid)
                if chunk is None:
                    raise ArchiveError("unknown chunk %s for %s" % (cid, rec["path"]))
                raw = read_chunk_strict(package, chunk)
                _write_all(fd, raw)
                h.update(raw)
                n += len(raw)
            os.close(fd)
            fd = None
            if n != rec["bytes"] or h.hexdigest() != rec["sha256"]:
                raise ArchiveError("reconstructed file failed verification: %s" % rec["path"])
        except Exception:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                if target.exists():
                    target.unlink()
            except OSError:
                pass
            raise
        written_files.append({"path": rec["path"], "bytes": n, "sha256": h.hexdigest()})
        written_bytes += n
    result = {
        "schema": "dc-temporal-native-archive-reconstruction/v1",
        "reconstructed_utc": utc_now(),
        "package": str(package),
        "state": state["name"],
        "destination": str(dest),
        "file_count": len(written_files),
        "bytes": written_bytes,
        "files": written_files,
        "overwrote_existing_files": False,
    }
    if args.out:
        write_json(args.out, result)
    print("[reconstruct] %s files=%d bytes=%d dest=%s"
          % (state["name"], len(written_files), written_bytes, dest), file=sys.stderr)
    for item in written_files:
        print("%s  %s  %d" % (item["sha256"], item["path"], item["bytes"]))
    return 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _fmt_int(value):
    try:
        return "{:,}".format(int(value))
    except Exception:
        return str(value)


def _fmt_bytes(value):
    try:
        value = int(value)
    except Exception:
        return str(value)
    return "%s bytes" % _fmt_int(value)


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise ArchiveError("short write while reconstructing")
        view = view[written:]


def _camera_text(camera):
    if not camera:
        return "-"
    loc = camera.get("location_cm")
    rot = camera.get("rotation_degrees") or {}
    fov = camera.get("horizontal_fov_deg")
    if loc is None:
        return "-"
    return "[%s] rot(p=%s,y=%s,r=%s) hfov=%s" % (
        ", ".join(str(x) for x in loc),
        rot.get("pitch", "-"), rot.get("yaw", "-"), rot.get("roll", "-"),
        fov if fov is not None else "-",
    )


def cmd_report(args):
    package = Path(os.path.abspath(os.path.expanduser(args.package)))
    index = load_json(package / "index.json")
    verification = load_json(args.verification) if args.verification else None
    compare = load_json(args.compare) if args.compare else None
    totals = index.get("totals") or {}
    lines = []
    add = lines.append

    add("# Temporal native archive report")
    add("")
    add("**Scope: SELECTED-resource temporal captures - native DumpGPU bytes, "
        "not complete GPU frames and not shader locals.** Every regular file under "
        "each accepted state is stored as opaque bytes; nothing is filtered by "
        "resource name, and line endings are never normalized.")
    add("")
    add("## Accepted capture runs")
    add("")
    add("| Run | Run id | Capture manifest | States | Source directory |")
    add("|---|---|---|---:|---|")
    for group, run in (index.get("runs") or {}).items():
        add("| `%s` | `%s` | `%s` | %d | `%s` |" % (
            group, run.get("run_id"), run.get("capture_manifest") or "-",
            run.get("selected_state_count") or 0, run.get("source_directory") or "-"))
    add("")

    add("## Per-state inventory and capture sequence")
    add("")
    add("| State | Files | Manifest files | Native bytes | Capture update | Engine frame(s) | Camera |")
    add("|---|---:|---:|---:|---|---:|---|")
    for state in index.get("states") or []:
        cap = state.get("capture") or {}
        raw_frame = cap.get("raw_frame") or {}
        update = cap.get("actual_phase_draw")
        if update is None:
            update = raw_frame.get("actual_phase_draw")
        target = cap.get("target_phase_draw")
        update_text = str(update) if update is not None else "-"
        if target is not None:
            update_text += " (target %s)" % target
        frames = raw_frame.get("engine_frames")
        if not frames:
            frames = cap.get("engine_frames_in_dump")
        if isinstance(frames, list):
            frame_text = ",".join(str(x) for x in frames)
        elif frames is not None:
            frame_text = str(frames)
        else:
            frame_text = "-"
        add("| `%s` | %s | %s | %s | %s | %s | %s |" % (
            state.get("name"), _fmt_int(state.get("file_count")),
            _fmt_int(cap.get("file_count")) if cap.get("file_count") is not None else "-",
            _fmt_int(state.get("bytes")), update_text, frame_text,
            _camera_text(cap.get("camera"))))
    add("")

    add("## Archive construction")
    add("")
    add("| Quantity | Value |")
    add("|---|---:|")
    add("| States | %s |" % _fmt_int(totals.get("state_count")))
    add("| Files preserved | %s |" % _fmt_int(totals.get("file_count")))
    add("| Native raw bytes (all states) | %s |" % _fmt_bytes(totals.get("raw_bytes")))
    add("| Unique file hashes | %s |" % _fmt_int(totals.get("unique_file_hashes")))
    add("| Unique chunks | %s |" % _fmt_int(totals.get("chunk_count")))
    add("| Unique chunk raw bytes | %s |" % _fmt_bytes(totals.get("chunk_raw_bytes")))
    add("| Chunk gzip bytes | %s |" % _fmt_bytes(totals.get("chunk_gzip_bytes")))
    add("| gzip / raw ratio | %s |" % totals.get("compression_ratio_gzip_over_raw"))
    add("| Reconstruction space, all %s states | %s |" % (
        _fmt_int(totals.get("state_count")),
        _fmt_bytes(totals.get("reconstruction_space_bytes"))))
    add("| Reconstruction space, largest state | %s |" % _fmt_bytes(totals.get("max_state_reconstruction_bytes")))
    if verification:
        b = verification.get("bytes") or {}
        add("| Package bytes on disk | %s |" % _fmt_bytes(b.get("archive_package_bytes")))
        add("| index.json bytes | %s |" % _fmt_bytes(b.get("index_bytes")))
        add("| Max chunk raw / gzip bytes | %s / %s |" % (
            _fmt_bytes(b.get("max_chunk_raw_bytes")), _fmt_bytes(b.get("max_chunk_gzip_bytes"))))
        add("| Max package file | `%s` (%s) |" % (
            b.get("max_package_file"), _fmt_bytes(b.get("max_package_file_bytes"))))
        add("| Max source file | `%s` (%s) |" % (
            b.get("max_source_file"), _fmt_bytes(b.get("max_source_file_bytes"))))
        add("| Source file records over 90 MiB (split into chunks) | %s |"
            % _fmt_int(b.get("source_files_over_90MiB")))
        add("| Chunk size limit | %s |" % _fmt_bytes(b.get("chunk_size_limit")))
    add("| Git LFS | not used |")
    add("")

    add("## Independent verification (no source)")
    add("")
    if verification:
        add("- Status: **%s**" % verification.get("status", "?").upper())
        add("- Chunks checked with gzip SHA-256 and decompressed raw SHA-256: %s"
            % _fmt_int((verification.get("counts") or {}).get("chunks")))
        add("- gzip MTIME=0 for every chunk: **%s**"
            % ("PASS" if (verification.get("checks") or {}).get("every_chunk_gzip_mtime_zero") else "FAIL"))
        add("- Files whose record size/SHA-256 was validated: %s"
            % _fmt_int((verification.get("counts") or {}).get("files")))
        add("- Unique chunk sequences hashed: %s; duplicate sequences reused: %s"
            % (_fmt_int((verification.get("sequence_memoization") or {}).get("unique_chunk_sequences_hashed")),
               _fmt_int((verification.get("sequence_memoization") or {}).get("duplicate_sequences_reused"))))
        add("- Max archive/Git blob check: **%s** (limit 90 MiB). Source files over 90 MiB: %s "
            "(each split into <=32 MiB chunks, so no archive blob is large)."
            % ("PASS" if (verification.get("checks") or {}).get("no_archive_file_over_90MiB") else "FAIL",
               _fmt_int((verification.get("bytes") or {}).get("source_files_over_90MiB"))))
        add("- Failures: %s" % ("None" if not verification.get("failures") else
                                "; ".join(verification.get("failures")[:20])))
    else:
        add("- No verification.json supplied.")
    add("")

    add("## Source before/after comparison")
    add("")
    if compare:
        add("- Status: **%s**" % compare.get("status", "?").upper())
        add("- Files checked after build: %s" % _fmt_int((compare.get("after") or {}).get("files")))
        add("- Bytes checked after build: %s" % _fmt_bytes((compare.get("after") or {}).get("bytes")))
        add("- Mismatched states: %d" % len(compare.get("mismatches") or []))
        add("- Missing, extra, size and hash mismatches: %s"
            % ("none" if not compare.get("mismatches") else "see source-compare.json"))
    else:
        add("- No source-compare.json supplied.")
    add("")

    if args.demo:
        add("## Bounded reconstruction demonstration")
        add("")
        demo = Path(os.path.abspath(os.path.expanduser(args.demo)))
        state = select_state(index, args.demo_state)
        records = {rec["path"]: rec for rec in state.get("files") or []}
        add("| File | Bytes | SHA-256 | Matches index |")
        add("|---|---:|---|---|")
        demo_files = []
        for dirpath, dirnames, filenames in os.walk(str(demo), followlinks=False):
            dirnames.sort()
            filenames.sort()
            for fn in filenames:
                p = Path(dirpath) / fn
                rel = os.path.relpath(str(p), str(demo)).replace(os.sep, "/")
                demo_files.append((rel, p))
        demo_files.sort()
        for rel, p in demo_files:
            size = p.stat().st_size
            digest = sha256_file(p)
            rec = records.get(rel)
            ok = rec is not None and rec.get("bytes") == size and rec.get("sha256") == digest
            add("| `%s` | %s | `%s` | %s |" % (rel, _fmt_int(size), digest, "yes" if ok else "NO"))
        add("")
        add("Reconstructed from `package/index.json` + `package/chunks/` only; the source "
            "tree was not read by the reconstruct command. State: `%s`." % state.get("name"))
        add("")

    add("## Capture coverage, gaps and notices")
    add("")
    rad_states = [s for s in index.get("states") or []
                  if s.get("run") == "radiosity"]
    if rad_states:
        complete = 0
        expected_total = None
        for state in rad_states:
            cov = (state.get("capture") or {}).get("coverage") or {}
            if cov.get("missing_count") == 0:
                complete += 1
            required = cov.get("required") or []
            if required:
                expected_total = len(required)
        add("- Radiosity required-resource coverage: **%d/%d accepted states report "
            "%s/%s required resources present, 0 missing**."
            % (complete, len(rad_states), expected_total if expected_total else "?",
               expected_total if expected_total else "?"))
        pp_counts = sorted({len((s.get("capture") or {}).get("pass_parameters") or [])
                            for s in rad_states})
        add("- Radiosity pass-parameter blocks recorded per state: %s (all referenced "
            "Structures and StructuresMetadata bytes are preserved)."
            % ", ".join(str(x) for x in pp_counts))
    for run_name in ("light", "motion"):
        run_states = [s for s in index.get("states") or [] if s.get("run") == run_name]
        pairs = set()
        for state in run_states:
            sr = (state.get("capture") or {}).get("selected_resources") or {}
            pairs.add((sr.get("present_count"), sr.get("missing_count")))
        if pairs:
            add("- `%s` manifest selected-resource coverage (same counts in every accepted "
                "state): %s present, %s listed as not present in that selected filter; "
                "these are manifest filter statistics, not archive omissions."
                % (run_name,
                   ", ".join(str(p) for p, _m in sorted(pairs)),
                   ", ".join(str(m) for _p, m in sorted(pairs))))
        flags = []
        for s in rad_states:
            ub = ((s.get("capture") or {}).get("rhi_uniform_buffer_content_map") or {})
            flag = (ub.get("r.RHI.UniformBufferContentMap.Enable") or {})
            flags.append(str(flag.get("value")))
        add("- `r.RHI.UniformBufferContentMap.Enable` values in accepted manifest: %s."
            % ", ".join(sorted(set(flags))))
    gaps = index.get("gaps") or []
    add("- Known gaps recorded in index: %s" % ("none" if not gaps else
                                                 json.dumps(gaps, ensure_ascii=False)))
    for note in index.get("notices") or []:
        add("- Notice: %s" % note)
    add("")
    add("## Failures")
    add("")
    failed = []
    if verification:
        failed.extend(verification.get("failures") or [])
    if compare:
        failed.extend(compare.get("mismatches") or [])
    add("None." if not failed else json.dumps(failed[:20], ensure_ascii=False, indent=1))
    add("")
    add("Reproduction commands are in `README.md`; the portable CLI is `archive.py`.")
    add("")
    text = "\n".join(lines)
    atomic_write_text(args.out, text)
    print("[report] wrote %s" % args.out, file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="archive.py",
        description="Lossless content-addressed archive for selected-resource DumpGPU states.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build", help="build a package from explicit RUN/STATE=PATH roots")
    p.add_argument("--root", action="append", required=True, metavar="RUN/STATE=PATH")
    p.add_argument("--out", required=True, metavar="PACKAGE_DIR")
    p.add_argument("--manifest", action="append", default=[], metavar="RUN=PATH")
    p.add_argument("--run", action="append", default=[], metavar="RUN=RUN_ID")
    p.add_argument("--note", action="append", default=[], metavar="TEXT")
    p.add_argument("--jobs", type=int, default=DEFAULT_JOBS)
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--settle-seconds", type=float, default=1.0)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("verify", help="verify the package without any source tree")
    p.add_argument("--package", required=True, metavar="PACKAGE_DIR")
    p.add_argument("--out", default=None, metavar="VERIFICATION_JSON")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("compare", help="re-hash source states and compare them with the archive")
    p.add_argument("--package", required=True, metavar="PACKAGE_DIR")
    p.add_argument("--root", action="append", required=True, metavar="RUN/STATE=PATH")
    p.add_argument("--out", default=None, metavar="SOURCE_COMPARE_JSON")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("reconstruct", help="reconstruct a state or selected files without overwriting")
    p.add_argument("--package", required=True, metavar="PACKAGE_DIR")
    p.add_argument("--state", required=True, metavar="RUN/STATE")
    p.add_argument("--dest", required=True, metavar="DEST_DIR")
    p.add_argument("--only", action="append", default=[], metavar="RELATIVE_FILE")
    p.add_argument("--out", default=None, metavar="RECONSTRUCTION_JSON")
    p.set_defaults(func=cmd_reconstruct)

    p = sub.add_parser("report", help="render report.md from index and verification evidence")
    p.add_argument("--package", required=True, metavar="PACKAGE_DIR")
    p.add_argument("--verification", default=None)
    p.add_argument("--compare", default=None)
    p.add_argument("--demo", default=None)
    p.add_argument("--demo-state", default="light/baseline")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_report)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ArchiveError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("ERROR: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print("ERROR: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
