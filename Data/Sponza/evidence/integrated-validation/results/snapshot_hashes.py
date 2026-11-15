#!/usr/bin/env python3
"""Snapshot SHA-256/size/mtime for the published review target.

Read-only over the repo: writes only the requested output file under results/.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

REPO = Path(r"D:/UnrealUnderstanding")
ROOTS = [
    "Docs/Unreal",
    "Scripts/Docs/unreal_articles",
    "Data/Sponza/evidence",
    "Data/Sponza/temporal-native",
    "Data/Sponza/sponza-20261115-083654",
]
SKIP_DIRS = {"__pycache__"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(4 << 20), b""):
            h.update(block)
    return h.hexdigest()


def iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and d != ".git")
        for name in sorted(filenames):
            yield Path(dirpath) / name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    entries = {}
    total = 0
    for rel_root in ROOTS:
        root = REPO / rel_root
        if not root.exists():
            raise SystemExit(f"missing root {root}")
        for p in iter_files(root):
            if p.suffix == ".pyc":
                continue
            rel = p.relative_to(REPO).as_posix()
            size = p.stat().st_size
            entries[rel] = {"size": size, "sha256": sha256_file(p)}
            total += size
    doc = {
        "roots": ROOTS,
        "files": len(entries),
        "bytes": total,
        "entries": entries,
    }
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=0, sort_keys=True), encoding="utf-8")
    tmp.replace(out)
    print(f"snapshot files={len(entries)} bytes={total} out={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
