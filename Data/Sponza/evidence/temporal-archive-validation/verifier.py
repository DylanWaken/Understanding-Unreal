#!/usr/bin/env python3
"""Independent stdlib verifier for the published temporal archive.

This file intentionally does not import or execute archive_temporal.py.  It
reads the published index/chunks directly, and compares reconstructed bytes to
the accepted radiosity baseline source tree.

Commands:
  snapshot --out FILE
  verify   --before FILE --out FILE
"""

from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path("D:/UnrealUnderstanding")
PKG = REPO / "Data/Sponza/temporal-native/package"
SOURCE = Path(
    "D:/UnrealUnderstanding-Radiosity/output/gpu-dump/raw/20261115-105951/baseline"
)
STATE_NAME = "radiosity/baseline"
VIEW_NAME = "ViewData.PrimaryViews"
PROBE_PREFIX = "Lumen.Radiosity.ProbeSH"
ARBITRARY_TAG = b"deepcluster-temporal-validation-arbitrary-chunk"


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


def load_json(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=True, sort_keys=True, indent=2)
        f.write("\n")


def decode_json_stream(path):
    """Decode the concatenated JSON objects used by DumpGPU Base/*.json."""
    text = Path(path).read_text(encoding="utf-8-sig")
    dec = json.JSONDecoder()
    out = []
    i = 0
    while i < len(text):
        while i < len(text) and text[i] in " \r\n\t,":
            i += 1
        if i >= len(text):
            break
        obj, i = dec.raw_decode(text, i)
        out.append(obj)
    return out


def canonical_chunk_rel(chunk_id):
    return "chunks/%s/%s/%s.bin.gz" % (chunk_id[:2], chunk_id[2:4], chunk_id)


def repo_snapshot():
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    head_proc = subprocess.run(
        ["git", "-C", str(REPO), "--no-optional-locks", "rev-parse", "HEAD"],
        capture_output=True, env=env, check=False,
    )
    status_proc = subprocess.run(
        ["git", "-C", str(REPO), "--no-optional-locks", "status",
         "--porcelain=v1", "-z"],
        capture_output=True, env=env, check=False,
    )
    status = status_proc.stdout
    return {
        "head": head_proc.stdout.decode("ascii", "replace").strip(),
        "head_exit": head_proc.returncode,
        "status_sha256": sha256_bytes(status),
        "status_bytes": len(status),
        "status_terminators": status.count(b"\x00"),
        "status_exit": status_proc.returncode,
        "status_stderr": status_proc.stderr.decode("utf-8", "replace")[:500],
        "git_env_optional_locks": env["GIT_OPTIONAL_LOCKS"],
    }


def package_snapshot():
    index_path = PKG / "index.json"
    chunk_meta = []
    chunk_root = PKG / "chunks"
    for p in sorted(chunk_root.rglob("*")):
        if p.is_file():
            st = p.stat()
            rel = p.relative_to(PKG).as_posix()
            chunk_meta.append([rel, st.st_size, st.st_mtime_ns])
    meta_hash = sha256_bytes(
        json.dumps(chunk_meta, separators=(",", ":")).encode("ascii")
    )
    return {
        "index_sha256": sha256_file(index_path),
        "index_bytes": index_path.stat().st_size,
        "chunk_file_count": len(chunk_meta),
        "chunk_total_bytes": sum(x[1] for x in chunk_meta),
        "chunk_tree_meta_sha256": meta_hash,
    }


def select_resources(resources_dir):
    """Select actual Resources binaries through ResourceDescs metadata."""
    descs = decode_json_stream(SOURCE / "Base" / "ResourceDescs.json")
    view_candidates = []
    probe_candidates = []
    for pos, desc in enumerate(descs):
        name = desc.get("Name")
        if name != VIEW_NAME and not str(name).startswith(PROBE_PREFIX):
            continue
        rid = desc.get("UniqueResourceName")
        files = sorted(
            p for p in resources_dir.iterdir()
            if p.is_file() and p.name.startswith(str(rid) + ".")
        )
        entry = {
            "metadata_index": pos,
            "name": name,
            "unique_resource_name": rid,
            "byte_size": desc.get("ByteSize"),
            "desc": desc.get("Desc"),
            "type": desc.get("Type"),
            "format": desc.get("Format"),
            "extent": [desc.get("ExtentX"), desc.get("ExtentY")],
            "candidate_files": [p.name for p in files],
            "candidate_sizes": [p.stat().st_size for p in files],
            "metadata": desc,
        }
        if name == VIEW_NAME:
            view_candidates.append(entry)
        else:
            probe_candidates.append(entry)

    def pick(candidates):
        for entry in candidates:
            if (len(entry["candidate_files"]) == 1
                    and entry["candidate_sizes"][0] == entry["byte_size"]):
                return entry
        return None

    view = pick(view_candidates)
    probe = pick(probe_candidates)
    if view is None:
        raise RuntimeError("no actual ViewData.PrimaryViews resource file found")
    if probe is None:
        raise RuntimeError("no actual ProbeSH texture resource file found")
    return {
        "view": view,
        "probe": probe,
        "view_candidates": view_candidates,
        "probe_candidates": probe_candidates,
    }


def source_snapshot():
    files = sorted(p for p in SOURCE.rglob("*") if p.is_file())
    per_file = {}
    total = 0
    meta = []
    for p in files:
        rel = p.relative_to(SOURCE).as_posix()
        st = p.stat()
        total += st.st_size
        per_file[rel] = sha256_file(p)
        meta.append([rel, st.st_size, st.st_mtime_ns])
    manifest_hash = sha256_bytes(
        json.dumps(sorted(per_file.items()), separators=(",", ":")).encode("ascii")
    )
    meta_hash = sha256_bytes(json.dumps(meta, separators=(",", ":")).encode("ascii"))
    selection = select_resources(SOURCE / "Resources")
    chosen = {}
    for key in ("view", "probe"):
        entry = selection[key]
        rel = "Resources/" + entry["candidate_files"][0]
        p = SOURCE / rel
        chosen[rel] = {
            "bytes": p.stat().st_size,
            "sha256": sha256_file(p),
            "mtime_ns": p.stat().st_mtime_ns,
            "metadata_name": entry["name"],
            "unique_resource_name": entry["unique_resource_name"],
        }
    return {
        "created_utc": utc_now(),
        "source_root": str(SOURCE),
        "state": STATE_NAME,
        "baseline_file_count": len(files),
        "baseline_total_bytes": total,
        "baseline_files_sha256": per_file,
        "baseline_manifest_sha256": manifest_hash,
        "baseline_meta_sha256": meta_hash,
        "selection": selection,
        "chosen_sources": chosen,
        "repo": repo_snapshot(),
        "package": package_snapshot(),
    }


def verify_chunk(catalog_rec, package):
    cid = catalog_rec["id"]
    expected_rel = canonical_chunk_rel(cid)
    result = {
        "id": cid,
        "path": catalog_rec.get("path"),
        "canonical_path": catalog_rec.get("path") == expected_rel,
        "file_exists": False,
        "gzip_bytes_match": False,
        "gzip_sha256_match": False,
        "gzip_mtime_zero": False,
        "raw_bytes_match": False,
        "raw_sha256_match": False,
        "ok": False,
    }
    if catalog_rec.get("path") != expected_rel:
        return result, None
    path = package / expected_rel
    result["file_exists"] = path.is_file()
    if not path.is_file():
        return result, None
    data = path.read_bytes()
    result["gzip_bytes_actual"] = len(data)
    result["gzip_bytes_match"] = len(data) == catalog_rec.get("gzip_bytes")
    result["gzip_sha256_actual"] = sha256_bytes(data)
    result["gzip_sha256_match"] = (
        result["gzip_sha256_actual"] == catalog_rec.get("gzip_sha256")
    )
    result["gzip_mtime_zero"] = (
        len(data) >= 8 and data[:2] == b"\x1f\x8b" and data[4:8] == b"\x00\x00\x00\x00"
    )
    try:
        raw = gzip.decompress(data)
    except Exception as exc:
        result["gzip_error"] = "%s: %s" % (type(exc).__name__, exc)
        return result, None
    result["raw_bytes_actual"] = len(raw)
    result["raw_bytes_match"] = len(raw) == catalog_rec.get("raw_bytes")
    result["raw_sha256_actual"] = sha256_bytes(raw)
    result["raw_sha256_match"] = (
        result["raw_sha256_actual"] == catalog_rec.get("raw_sha256") == cid
    )
    result["ok"] = all(
        result[k] for k in (
            "canonical_path", "file_exists", "gzip_bytes_match",
            "gzip_sha256_match", "gzip_mtime_zero", "raw_bytes_match",
            "raw_sha256_match",
        )
    )
    return result, raw


def reconstruct_file(package, catalog, state, rel):
    records = [f for f in state["files"] if f.get("path") == rel]
    if len(records) != 1:
        raise RuntimeError("expected exactly one file record for %s, got %d"
                           % (rel, len(records)))
    record = records[0]
    chunks = []
    raws = []
    for cid in record["chunks"]:
        if cid not in catalog:
            raise RuntimeError("file references unknown chunk: %s" % cid)
        chunk_result, raw = verify_chunk(catalog[cid], package)
        chunks.append(chunk_result)
        if raw is None:
            raise RuntimeError("chunk failed: %s" % cid)
        raws.append(raw)
    data = b"".join(raws)
    digest = sha256_bytes(data)
    source_path = SOURCE / rel
    source = source_path.read_bytes()
    return {
        "path": rel,
        "record": {
            "bytes": record.get("bytes"),
            "sha256": record.get("sha256"),
            "chunks": record.get("chunks"),
        },
        "chunk_count": len(record["chunks"]),
        "chunks": chunks,
        "reconstructed_bytes": len(data),
        "reconstructed_sha256": digest,
        "record_bytes_match": len(data) == record.get("bytes"),
        "record_sha256_match": digest == record.get("sha256"),
        "source_path": str(source_path),
        "source_bytes": len(source),
        "source_sha256": sha256_bytes(source),
        "source_sha256_match": digest == sha256_bytes(source),
        "exact_bytes_equal": data == source,
        "ok": (
            len(data) == record.get("bytes")
            and digest == record.get("sha256")
            and data == source
            and all(c["ok"] for c in chunks)
        ),
    }


def verify_arbitrary_chunk(package, catalog, state):
    eligible = sorted({
        cid for f in state["files"]
        for cid in (f.get("chunks") or [])
        if cid in catalog
    })
    index = int(hashlib.sha256(ARBITRARY_TAG).hexdigest(), 16) % len(eligible)
    cid = eligible[index]
    result, raw = verify_chunk(catalog[cid], package)
    refs = [
        f for f in state["files"]
        if isinstance(f.get("chunks"), list) and cid in f["chunks"]
    ]
    result["selection"] = {
        "method": (
            "sorted(unique chunk ids referenced by radiosity/baseline)"
            "[sha256(fixed_tag) % count]"
        ),
        "tag": ARBITRARY_TAG.decode("ascii"),
        "index": index,
        "eligible_count": len(eligible),
        "catalog_count": len(catalog),
    }
    result["baseline_referencing_files"] = len(refs)
    if refs and raw is not None:
        f = refs[0]
        pos = f["chunks"].index(cid)
        offset = sum(catalog[c]["raw_bytes"] for c in f["chunks"][:pos])
        source_path = SOURCE / f["path"]
        with open(source_path, "rb") as fh:
            fh.seek(offset)
            source_slice = fh.read(len(raw))
        result["source_slice"] = {
            "path": f["path"],
            "chunk_position": pos,
            "offset": offset,
            "length": len(raw),
            "sha256": sha256_bytes(source_slice),
            "exact_bytes_equal": source_slice == raw,
        }
        result["source_slice_match"] = source_slice == raw
    else:
        result["source_slice_match"] = None
    result["ok"] = result["ok"] and (
        result["source_slice_match"] is True if result["source_slice_match"] is not None
        else False
    )
    return {"chunk": result, "raw": raw}


def compare_snapshots(before, after):
    bsel = before.get("chosen_sources", {})
    asel = after.get("chosen_sources", {})
    chosen_match = True
    chosen_detail = {}
    for rel, b in bsel.items():
        a = asel.get(rel, {})
        same = (
            a.get("sha256") == b.get("sha256")
            and a.get("bytes") == b.get("bytes")
            and a.get("mtime_ns") == b.get("mtime_ns")
        )
        chosen_detail[rel] = {
            "before_sha256": b.get("sha256"),
            "after_sha256": a.get("sha256"),
            "before_bytes": b.get("bytes"),
            "after_bytes": a.get("bytes"),
            "before_mtime_ns": b.get("mtime_ns"),
            "after_mtime_ns": a.get("mtime_ns"),
            "same": same,
        }
        chosen_match = chosen_match and same
    return {
        "baseline_file_count_match": (
            before.get("baseline_file_count") == after.get("baseline_file_count")
        ),
        "baseline_total_bytes_match": (
            before.get("baseline_total_bytes") == after.get("baseline_total_bytes")
        ),
        "baseline_files_sha256_match": (
            before.get("baseline_files_sha256") == after.get("baseline_files_sha256")
        ),
        "baseline_manifest_sha256_match": (
            before.get("baseline_manifest_sha256")
            == after.get("baseline_manifest_sha256")
        ),
        "baseline_meta_sha256_match": (
            before.get("baseline_meta_sha256") == after.get("baseline_meta_sha256")
        ),
        "chosen_source_hashes_match": chosen_match,
        "chosen_sources": chosen_detail,
        "repo_head_match": before["repo"].get("head") == after["repo"].get("head"),
        "repo_status_sha256_match": (
            before["repo"].get("status_sha256")
            == after["repo"].get("status_sha256")
        ),
        "repo_status_bytes_match": (
            before["repo"].get("status_bytes")
            == after["repo"].get("status_bytes")
        ),
        "package_index_sha256_match": (
            before["package"].get("index_sha256")
            == after["package"].get("index_sha256")
        ),
        "package_chunk_tree_meta_sha256_match": (
            before["package"].get("chunk_tree_meta_sha256")
            == after["package"].get("chunk_tree_meta_sha256")
        ),
        "package_chunk_count_match": (
            before["package"].get("chunk_file_count")
            == after["package"].get("chunk_file_count")
        ),
        "package_chunk_total_bytes_match": (
            before["package"].get("chunk_total_bytes")
            == after["package"].get("chunk_total_bytes")
        ),
    }


def official_summary(results_dir):
    path = results_dir / "verification.json"
    if not path.is_file():
        return {"present": False}
    data = load_json(path)
    checks = data.get("checks") or {}
    counts = data.get("counts") or {}
    return {
        "present": True,
        "path": str(path),
        "status": data.get("status"),
        "all_checks_true": bool(checks) and all(v is True for v in checks.values()),
        "checks": checks,
        "counts": counts,
        "bytes": data.get("bytes"),
        "totals": {
            "states": len(data.get("states") or []),
            "failures": len(data.get("failures") or []),
        },
        "failures_head": (data.get("failures") or [])[:5],
        "ok": (
            data.get("status") == "pass"
            and counts.get("files") == 23923
            and counts.get("chunks") == 8468
            and not (data.get("failures") or [])
            and bool(checks) and all(v is True for v in checks.values())
        ),
    }


def cmd_snapshot(args):
    snap = source_snapshot()
    write_json(args.out, snap)
    print("[snapshot] files=%d bytes=%d view=%s probe=%s repo_status=%d chunk_files=%d"
          % (snap["baseline_file_count"], snap["baseline_total_bytes"],
             snap["selection"]["view"]["unique_resource_name"],
             snap["selection"]["probe"]["unique_resource_name"],
             snap["repo"]["status_bytes"], snap["package"]["chunk_file_count"]))
    return 0


def cmd_verify(args):
    before = load_json(args.before)
    index = load_json(PKG / "index.json")
    state = next((s for s in index.get("states") or []
                  if s.get("name") == STATE_NAME), None)
    if state is None:
        raise RuntimeError("state not found: %s" % STATE_NAME)
    catalog = {rec["id"]: rec for rec in index.get("chunk_catalog") or []}
    selection = select_resources(SOURCE / "Resources")
    checks = []

    chosen = {}
    for key, entry in (("view", selection["view"]), ("probe", selection["probe"])):
        rel = "Resources/" + entry["candidate_files"][0]
        recon = reconstruct_file(PKG, catalog, state, rel)
        recon["metadata_index"] = entry["metadata_index"]
        recon["metadata_name"] = entry["name"]
        recon["unique_resource_name"] = entry["unique_resource_name"]
        recon["metadata_desc"] = entry["desc"]
        recon["metadata_format"] = entry["format"]
        chosen[key] = recon

    arbitrary = verify_arbitrary_chunk(PKG, catalog, state)
    after = source_snapshot()
    unchanged = compare_snapshots(before, after)
    official = official_summary(Path(args.out).parent)

    selection_match = True
    if before.get("selection"):
        before_ids = {
            "view": before["selection"]["view"]["unique_resource_name"],
            "probe": before["selection"]["probe"]["unique_resource_name"],
        }
        after_ids = {
            "view": selection["view"]["unique_resource_name"],
            "probe": selection["probe"]["unique_resource_name"],
        }
        selection_match = before_ids == after_ids

    unchanged_ok = all(
        unchanged[k] for k in (
            "baseline_file_count_match",
            "baseline_total_bytes_match",
            "baseline_files_sha256_match",
            "baseline_manifest_sha256_match",
            "baseline_meta_sha256_match",
            "chosen_source_hashes_match",
            "repo_head_match",
            "repo_status_sha256_match",
            "repo_status_bytes_match",
            "package_index_sha256_match",
            "package_chunk_tree_meta_sha256_match",
            "package_chunk_count_match",
            "package_chunk_total_bytes_match",
        )
    )
    recon_ok = chosen["view"]["ok"] and chosen["probe"]["ok"]
    arbitrary_ok = arbitrary["chunk"]["ok"] is True
    result = {
        "schema": "dc-temporal-independent-verification/v1",
        "verified_utc": utc_now(),
        "package": str(PKG),
        "source_root": str(SOURCE),
        "state": STATE_NAME,
        "selection": {
            "view": {
                "metadata_index": selection["view"]["metadata_index"],
                "metadata_name": selection["view"]["name"],
                "unique_resource_name": selection["view"]["unique_resource_name"],
                "candidate_files": selection["view"]["candidate_files"],
                "candidates": selection["view_candidates"],
            },
            "probe": {
                "metadata_index": selection["probe"]["metadata_index"],
                "metadata_name": selection["probe"]["name"],
                "unique_resource_name": selection["probe"]["unique_resource_name"],
                "candidate_files": selection["probe"]["candidate_files"],
                "candidates": selection["probe_candidates"],
            },
            "selection_match_before_after": selection_match,
        },
        "reconstruction": chosen,
        "arbitrary_chunk_index": arbitrary["chunk"],
        "official_verification": official,
        "unchanged": unchanged,
        "checks": {
            "chosen_by_actual_resource_metadata": (
                selection["view"]["metadata_index"] >= 0
                and selection["probe"]["metadata_index"] >= 0
                and selection["view"]["candidate_files"][0].startswith(
                    selection["view"]["unique_resource_name"] + ".")
                and selection["probe"]["candidate_files"][0].startswith(
                    selection["probe"]["unique_resource_name"] + ".")
            ),
            "view_exact_bytes_match": chosen["view"]["exact_bytes_equal"],
            "probe_sh_exact_bytes_match": chosen["probe"]["exact_bytes_equal"],
            "view_record_and_source_match": (
                chosen["view"]["record_sha256_match"]
                and chosen["view"]["source_sha256_match"]
            ),
            "probe_sh_record_and_source_match": (
                chosen["probe"]["record_sha256_match"]
                and chosen["probe"]["source_sha256_match"]
            ),
            "arbitrary_chunk_and_index_match": arbitrary_ok,
            "arbitrary_chunk_source_slice_match": (
                arbitrary["chunk"].get("source_slice_match") is True
            ),
            "official_verification_pass": bool(official.get("ok")),
            "source_and_repository_unchanged": unchanged_ok,
        },
        "ok": False,
    }
    result["ok"] = all(result["checks"].values())
    write_json(args.out, result)
    for key, val in result["checks"].items():
        print("[independent] %-42s %s" % (key, "PASS" if val else "FAIL"))
    print("[independent] %s view=%s probe=%s arbitrary_chunk=%s"
          % ("PASS" if result["ok"] else "FAIL",
             chosen["view"]["reconstructed_sha256"],
             chosen["probe"]["reconstructed_sha256"],
             arbitrary["chunk"]["id"]))
    return 0 if result["ok"] else 1


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_snap = sub.add_parser("snapshot")
    p_snap.add_argument("--out", required=True)
    p_snap.set_defaults(func=cmd_snapshot)
    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--before", required=True)
    p_verify.add_argument("--out", required=True)
    p_verify.set_defaults(func=cmd_verify)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
