#!/usr/bin/env python3
"""Read-only DumpGPU context used by the published temporal decoders.

This module contains only the dump I/O that the placement decoder needs.  It is
a dependency extraction of ``DumpCtx`` (and the concatenated-JSON reader it
uses) from the historical ``scripts/decode_temporal.py`` producer.  The decode
algorithms stay in the calling decoder; nothing here selects or interprets
capture values.

The reader intentionally keeps the historical semantics:

* ``Base/Passes.json``, ``ResourceDescs.json``, ``SubresourceLayouts.json`` and
  ``DumpReadbackStatus.json`` are concatenated JSON objects (or arrays);
* every ``Resources/*.v*.bin`` file is indexed by its 000-prefixed resource UID
  and version pointer, excluding stencil and mip variants exactly as before;
* ``StructuresMetadata/*.json`` is keyed by file stem.

All paths are opened read-only.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path


def load_concat_json(path: Path):
    """Read a dump JSON file containing one or more concatenated values.

    This is the same behaviour as ``decode_context.load_concat_json`` that the
    historical producer imported: arrays are appended as one value, not
    flattened.  It is kept local so ``DumpCtx`` has no undeclared dependency.
    """
    with open(path, encoding="utf-8") as f:
        s = f.read()
    dec = json.JSONDecoder()
    out = []
    i = 0
    n = len(s)
    while i < n:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        o, j = dec.raw_decode(s, i)
        out.append(o)
        i = j
    return out


class DumpCtx:
    """Index one raw DumpGPU state directory.

    ``root`` is a single state (for example ``.../baseline``), not a run root.
    The inherited ``main_cache`` helper is retained verbatim for provenance and
    for callers that explicitly want the old global selection.  The placement
    decoder does not use it for data; it selects the cache from the captured
    mark/allocate passes in its chosen renderer invocation instead.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        base = self.root / "Base"
        self.passes = load_concat_json(base / "Passes.json")
        self.by_ptr = {p["Pointer"]: p for p in self.passes}
        self.descs = load_concat_json(base / "ResourceDescs.json")
        self.desc_by_uid = {d["UniqueResourceName"]: d for d in self.descs}
        self.layouts = load_concat_json(base / "SubresourceLayouts.json")
        self.readback = {
            r["File"].replace("\\", "/"): r
            for r in load_concat_json(base / "DumpReadbackStatus.json")
        }
        self.meta = {}
        for f in (base.parent / "StructuresMetadata").glob("*.json"):
            self.meta[f.stem] = json.load(open(f, encoding="utf-8"))
        # index resource files by unique resource name
        self.files = defaultdict(list)
        for f in (self.root / "Resources").glob("*.bin"):
            fn = f.name
            if "stencil" in fn or ".v" not in fn:
                continue
            uid = fn.split(".")[0]
            if not uid.startswith("000"):
                continue
            ver = fn.split(".v")[-1].replace(".bin", "")
            self.files[uid].append((f, ver))
        self.file_meta = {}
        for uid, entries in self.files.items():
            for f, ver in entries:
                p = self.by_ptr.get(ver)
                self.file_meta[(uid, ver)] = {
                    "path": f,
                    "version": ver,
                    "producer": p,
                    "producer_id": p["Id"] if p else None,
                    "producer_name": p["EventName"] if p else None,
                }

    def passes_named(self, name, prefix=None):
        out = [
            p
            for p in self.passes
            if p["EventName"] == name
            and (prefix is None or p["Pointer"].startswith(prefix))
        ]
        return sorted(out, key=lambda p: p["Id"])

    def graph_prefixes(self):
        return sorted(
            {
                d["UniqueResourceName"][:4]
                for d in self.descs
                if d["Name"] == "ViewData.PrimaryViews"
            }
        )

    def view_descs(self):
        return [d for d in self.descs if d["Name"] == "ViewData.PrimaryViews"]

    def candidates(self, name, prefix=None):
        out = []
        for d in self.descs:
            uid = d["UniqueResourceName"]
            if d["Name"] != name:
                continue
            if prefix and not uid.startswith(prefix):
                continue
            for f, ver in self.files.get(uid, []):
                p = self.by_ptr.get(ver)
                if p is None:
                    continue
                out.append(
                    {"desc": d, "uid": uid, "file": f, "version": ver, "producer": p}
                )
        return out

    def select(self, name, prefix=None, prefer=None, allow_v0=False, extent_x=None):
        cands = self.candidates(name, prefix)
        if extent_x is not None:
            cands = [c for c in cands if c["desc"].get("ExtentX") == extent_x]
        if not cands or allow_v0:
            # version 0 is an imported/uploaded resource with no recorded producer pass
            for d in self.descs:
                uid = d["UniqueResourceName"]
                if d["Name"] != name or (prefix and not uid.startswith(prefix)):
                    continue
                if extent_x is not None and d.get("ExtentX") != extent_x:
                    continue
                for f, ver in self.files.get(uid, []):
                    if ver == "00000000000000000000":
                        cands.append(
                            {
                                "desc": d,
                                "uid": uid,
                                "file": f,
                                "version": ver,
                                "producer": None,
                            }
                        )
        if not cands:
            raise KeyError(f"{name} (prefix {prefix})")
        if prefer:
            pref = [
                c for c in cands if c["producer"] and prefer in c["producer"]["EventName"]
            ]
            if pref:
                cands = pref
        cands.sort(key=lambda c: c["producer"]["Id"] if c["producer"] else -1)
        return cands[-1]

    def main_cache(self, name):
        """Select the main (screen-probe) radiance cache resource.

        Kept verbatim from the historical producer for provenance.  The main
        and translucency-volume caches share names.  Each resource version was
        written by a pass, so the descriptor whose latest producer has the
        lowest pass id belongs to the main chain; exact-size ties (the two depth
        atlases) are broken by the larger extent.  The placement decoder records
        this as a rejected counter-example and uses invoked-pass identity
        instead; do not use this helper for data.
        """
        cands = self.candidates(name)
        if not cands:
            return None
        groups = defaultdict(list)
        for c in cands:
            groups[c["uid"]].append(c)
        ranked = []
        for uid, g in groups.items():
            g.sort(key=lambda c: c["producer"]["Id"])
            last = g[-1]
            ranked.append(
                (
                    last["producer"]["Id"],
                    -int(last["desc"].get("ByteSize") or 0),
                    uid,
                    g,
                )
            )
        ranked.sort()
        _, _, uid, g = ranked[0]
        g.sort(key=lambda c: c["producer"]["Id"])
        last = g[-1]
        return {
            "uid": uid,
            "desc": last["desc"],
            "file": last["file"],
            "version": last["version"],
            "producer": last["producer"],
            "candidates": g,
        }

    def read_bytes(self, entry):
        return entry["file"].read_bytes()

    def readback_status(self, entry):
        rel = "Resources/" + entry["file"].name
        rb = self.readback.get(rel)
        return rb["Status"] if rb else None
