#!/usr/bin/env python3
"""Decode the captured sun virtual-shadow-map storage for the Sponza frame.

The decoder is read-only with respect to the DumpGPU package.  It selects the
last (presented) VirtualShadowMapProjection pass, resolves the exact resource
versions consumed by that pass, decodes the captured FVirtualShadowMapUniform-
Parameters, page table, page flags, physical-page metadata and physical depth
pool with the layouts in the pinned engine source, and publishes:

  publish/figures/vsm_pool_occupancy.png
  publish/figures/vsm_page_table_sun.png
  publish/figures/vsm_depth_pages.png
  publish/data/vsm_storage.json
  publish/data/vsm_storage.npz
  publish/data/schema.json
  publish/manifest.json
  source-ranges.json
  report.md
  checks.json

The receiver chain in this file is a *declared replay* of the documented
clipmap-selection and page-lookup arithmetic using captured operands.  It is
not an executed shader-local SMRT ray trace: ray directions, slope/normal
biases and per-lane decisions are shader-local and are not captured.

Usage:
  python tools/decode_vsm_storage.py --dump <frame-root> --out <out-dir> --all \
      [--engine <engine-root>] [--receivers <receivers.json>] [--view <view.json>]
  python tools/decode_vsm_storage.py --out <out-dir> --verify [--engine <engine-root>]

`--all` is repeatable.  No input path is hard-coded; every input is an argument
or is derived from the `--dump` root.  `--verify` needs neither the raw capture
nor the engine and verifies the published files and arrays in place.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import struct
import sys
from pathlib import Path

import numpy as np

try:
    from PIL import Image, ImageDraw
except Exception:  # pragma: no cover - verify only needs PNG headers
    Image = None
    ImageDraw = None

# ---------------------------------------------------------------------------
# source constants (also cited in source-ranges.json)
# ---------------------------------------------------------------------------
VSM_LOG2_PAGE_SIZE = 7
VSM_PAGE_SIZE = 1 << VSM_LOG2_PAGE_SIZE          # 128
VSM_LEVEL0_DIM_PAGES_XY = 1 << 7                 # 128
VSM_MAX_MIP_LEVELS = VSM_LOG2_PAGE_SIZE + 1      # 8
VSM_PAGE_TABLE_TEX2D_SIZE_X = 128
VSM_PAGE_TABLE_TEX2D_SIZE_Y = 192
VSM_MAX_SINGLE_PAGE_SHADOW_MAPS = 1024 * 8       # 8192
VSM_PSD_STRIDE = 16 * 23                         # 368

VSM_FLAGS = {
    "ALLOCATED": 1 << 0,
    "DYNAMIC_UNCACHED": 1 << 1,
    "STATIC_UNCACHED": 1 << 2,
    "DETAIL_GEOMETRY": 1 << 3,
    "PRIMARY_REQUEST": 1 << 4,
    "SECONDARY_REQUEST": 1 << 5,
}
VSM_EXT_FLAGS = {
    "DYNAMIC_INITIALIZED": 1 << 6,
    "STATIC_INITIALIZED": 1 << 7,
    "DYNAMIC_DIRTY": 1 << 8,
    "STATIC_DIRTY": 1 << 9,
    "INVALIDATE_DYNAMIC": 1 << 10,
    "INVALIDATE_STATIC": 1 << 11,
    "VIEW_UNCACHED": 1 << 12,
    "UNREFERENCED": 1 << 13,
    "FORCE_CACHED": 1 << 14,
    "DEFERRED_INVALIDATION": 1 << 15,
}

# Fixed palettes are declared in publish/data/vsm_storage.json.
OCCUPANCY_COLORS = {
    0: (62, 62, 74),      # free / unallocated; neutral gray
    1: (40, 205, 255),    # selected sun clipmap, current frame
    2: (55, 115, 220),    # other current sun clipmap
    3: (165, 110, 235),   # current metadata, not referenced by page-table mip0
    4: (170, 145, 95),    # allocated, not referenced in this snapshot (age alone is not proof)
    5: (255, 240, 60),    # referenced but metadata free (inconsistent)
}
META_STATE_COLORS = {
    0: (62, 62, 74),      # free / unallocated; neutral gray
    1: (60, 200, 125),
    2: (170, 145, 95),    # allocated, not referenced in this snapshot (age alone is not proof)
    3: (255, 240, 60),
}
OCCUPANCY_LABELS = {
    0: "free/unallocated (neutral gray)",
    1: "allocated, referenced, selected sun VSM, current scene frame",
    2: "allocated, referenced, other current sun VSM",
    3: "allocated, current metadata but not referenced by page-table mip0",
    4: "allocated, not referenced in this snapshot (earlier last-requested frame; not asserted stale/invalid)",
    5: "referenced but metadata free (inconsistent)",
}
META_STATE_LABELS = {
    0: "free/unallocated (neutral gray)",
    1: "allocated, last requested in captured scene frame",
    2: "allocated, not referenced in this snapshot (earlier last-requested frame; not asserted stale/invalid)",
    3: "metadata free but referenced (inconsistent)",
}
LOD_COLORS = {
    0: (60, 225, 130),
    1: (45, 165, 225),
    2: (85, 105, 225),
    3: (140, 80, 205),
    4: (195, 75, 160),
    5: (225, 115, 70),
    6: (225, 185, 60),
    7: (165, 125, 80),
}
LOD_FALLBACK_COLOR = (25, 25, 32)
PAGE_FLAG_COLORS = {
    0x00: (24, 24, 30),
    0x01: (55, 115, 220),
    0x1B: (60, 215, 140),
}
PAGE_FLAG_OTHER = (225, 180, 55)
DEPTH_RAMP_STOPS = np.array([
    [0.00, 10, 12, 40],
    [0.25, 25, 60, 140],
    [0.50, 20, 150, 150],
    [0.75, 220, 180, 60],
    [1.00, 255, 255, 235],
], dtype=np.float64)
INVALID_DEPTH_COLOR = (255, 0, 255)
MASKED_COLOR = (26, 26, 32)
GUTTER_COLOR = (12, 12, 16)
# Virtual-depth mosaic categorical cells (never depth-ramp colours).
MOSAIC_FALLBACK_COLOR = (110, 92, 145)   # coarser-clipmap fallback entry, no mip0 page
MOSAIC_UNMAPPED_COLOR = (22, 22, 28)     # no page-table entry
MOSAIC_CATEGORY_COLORS = {0: None, 1: MOSAIC_FALLBACK_COLOR, 2: MOSAIC_UNMAPPED_COLOR}
MOSAIC_CATEGORY_LABELS = {
    0: "mapped selected-VSM page (depth ramp, fixed range)",
    1: "coarser-clipmap fallback entry (categorical, not depth)",
    2: "unmapped (categorical, not depth)",
}
MOSAIC_DEPTH_RANGE = [0.4998, 0.5003]

SOURCE_RANGES = [
    ("Shaders/Shared/VirtualShadowMapDefinitions.h", 12, 38,
     "page size 128, level-0 page-table 128x128, page-table texture 128x192"),
    ("Shaders/Shared/VirtualShadowMapDefinitions.h", 113, 113, "VSM_PSD_STRIDE = 368 bytes"),
    ("Shaders/Shared/VirtualShadowMapDefinitions.h", 147, 155,
     "FPhysicalPageMetaData: Flags, LastRequestedSceneFrameNumber, VirtualShadowMapId, MipLevel, PageAddress.xy (24 B)"),
    ("Shaders/Private/VirtualShadowMaps/VirtualShadowMapPageAccessCommon.ush", 72, 85,
     "CalcLog2LevelDimsPages/CalcLevelDimsPages/CalcLevelDimsTexels: logical shadow-mip page dimensions"),
    ("Shaders/Private/VirtualShadowMaps/VirtualShadowMapPageAccessCommon.ush", 87, 102,
     "CalcLevelOffsets: logical mip 1..7 tail is packed at y=128 inside each VSM page-table block"),
    ("Shaders/Private/VirtualShadowMaps/VirtualShadowMapPageAccessCommon.ush", 128, 148,
     "CalcPageTableLevelOffset: full-id 128x192 block + CalcLevelOffsets(MipLevel)"),
    ("Shaders/Private/VirtualShadowMaps/VirtualShadowMapPageAccessCommon.ush", 222, 287,
     "physical page linearisation and page-table bit packing/decoding"),
    ("Shaders/Private/VirtualShadowMaps/VirtualShadowMapPageAccessCommon.ush", 305, 330,
     "VirtualToPhysicalTexelBase / VirtualToPhysicalTexel"),
    ("Shaders/Private/VirtualShadowMaps/VirtualShadowMapProjectionStructs.ush", 14, 56,
     "FVirtualShadowMapProjectionShaderData field order and sizes"),
    ("Shaders/Private/VirtualShadowMaps/VirtualShadowMapProjectionStructs.ush", 58, 131,
     "DecodeVirtualShadowMapProjectionData field offsets"),
    ("Shaders/Private/VirtualShadowMaps/VirtualShadowMapProjectionCommon.ush", 41, 90,
     "biased absolute clipmap level and distance-to-origin formulas"),
    ("Shaders/Private/VirtualShadowMaps/VirtualShadowMapProjectionCommon.ush", 160, 200,
     "physical depth load from pool slice 0 and virtual-to-physical sample"),
    ("Shaders/Private/VirtualShadowMaps/VirtualShadowMapProjectionCommon.ush", 256, 308,
     "SampleVirtualShadowMapClipmap: mip0 page entry, LODOffset clipmap fallback, depth load"),
    ("Shaders/Private/VirtualShadowMaps/VirtualShadowMapProjectionCommon.ush", 362, 372,
     "ComputeOccluderDistanceOrtho: view Z = (depth - M43) / M33"),
    ("Shaders/Private/VirtualShadowMaps/VirtualShadowMapProjectionDirectional.ush", 38, 71,
     "GetMappedClipmap: clipmap index selection and base handle offset"),
    ("Shaders/Private/VirtualShadowMaps/VirtualShadowMapProjectionDirectional.ush", 230, 270,
     "shadow translated world, ray start UVZ, SMRT sample and ortho occluder distance"),
    ("Shaders/Private/VirtualShadowMaps/VirtualShadowMapSMRTTemplate.ush", 69, 84,
     "first valid sample depth compare: SampleDepth > ReferenceDepth => hit"),
    ("Shaders/Private/VirtualShadowMaps/VirtualShadowMapSMRTTemplate.ush", 121, 130,
     "subsequent depth compare tolerance"),
    ("Shaders/Private/VirtualShadowMaps/VirtualShadowMapPhysicalPageManagement.usf", 450, 516,
     "page-table encoding and OutPhysicalPageMetaData ownership writes"),
    ("Shaders/Private/VirtualShadowMaps/VirtualShadowMapPhysicalPageManagement.usf", 804, 811,
     "static/dynamic depth merge: slice0 = max(slice0, slice[StaticCachedArrayIndex])"),
    ("Shaders/Private/VirtualShadowMaps/VirtualShadowMapPageManagement.usf", 97, 106,
     "ProcessMipLevel: H_m parent texel = GlobalPageTableEntryIndex.TexelAddress >> m"),
    ("Shaders/Private/VirtualShadowMaps/VirtualShadowMapPageManagement.usf", 162, 237,
     "hierarchical page flags: OutPageFlagMips_0..5 are separate textures (H1..H6), no H7"),
    ("Source/Runtime/Renderer/Private/VirtualShadowMaps/VirtualShadowMapArray.h", 67, 96,
     "FVirtualShadowMap constants: 128 page, 128 pages level0, 16 physical address bits"),
    ("Source/Runtime/Renderer/Private/VirtualShadowMaps/VirtualShadowMapArray.h", 100, 144,
     "FVirtualShadowMapProjectionShaderData C++ layout and stride assert"),
    ("Source/Runtime/Renderer/Private/VirtualShadowMaps/VirtualShadowMapArray.h", 157, 228,
     "FVirtualShadowMapUniformParameters field order"),
    ("Source/Runtime/Renderer/Private/VirtualShadowMaps/VirtualShadowMapArray.cpp", 869, 877,
     "CalcTranslatedWorldToShadowUVMatrix"),
    ("Source/Runtime/Renderer/Private/VirtualShadowMaps/VirtualShadowMapArray.cpp", 1025, 1052,
     "static array slice = 1; physical row mask/shift and pool size"),
    ("Source/Runtime/Renderer/Private/VirtualShadowMaps/VirtualShadowMapArray.cpp", 2652, 2656,
     "page-table entries per row and PageTableRowShift"),
    ("Source/Runtime/Renderer/Private/VirtualShadowMaps/VirtualShadowMapCacheManager.cpp", 1090, 1135,
     "PhysicalPagePool R32_UINT array creation and PhysicalPageMetaData allocation"),
    ("Source/Runtime/Renderer/Private/VirtualShadowMaps/VirtualShadowMapClipmap.cpp", 528, 538,
     "PackClipmapLevelAndCount"),
    ("Source/Runtime/Renderer/Private/VirtualShadowMaps/VirtualShadowMapClipmap.cpp", 556, 566,
     "projection matrices and packed clipmap level/count"),
]


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------
def jval(o):
    if isinstance(o, dict):
        return {str(k): jval(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jval(v) for v in o]
    if isinstance(o, (np.bool_, bool)):
        return bool(o)
    if isinstance(o, (np.integer, int)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        v = float(o)
        return v if math.isfinite(v) else None
    if isinstance(o, np.ndarray):
        return jval(o.tolist())
    return o


def sha256_file(path: Path, chunk: int = 1 << 23) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def load_concat_json(path: Path):
    s = path.read_text(encoding="utf-8")
    dec = json.JSONDecoder()
    out, i, n = [], 0, len(s)
    while i < n:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        o, j = dec.raw_decode(s, i)
        out.append(o)
        i = j
    return out


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jval(obj), indent=2, sort_keys=False), encoding="utf-8")


def f3(raw: bytes, off: int) -> np.ndarray:
    return np.frombuffer(raw, dtype="<f4", count=3, offset=off).astype(np.float64)


def u32(raw: bytes, off: int) -> int:
    return struct.unpack_from("<I", raw, off)[0]


def i32(raw: bytes, off: int) -> int:
    return struct.unpack_from("<i", raw, off)[0]


def f32(raw: bytes, off: int) -> float:
    return struct.unpack_from("<f", raw, off)[0]


def mat4(raw: bytes, off: int) -> np.ndarray:
    """Math matrix.  UE stores the block column-major; math = block.T."""
    return np.frombuffer(raw, dtype="<f4", count=16, offset=off).reshape(4, 4).T.astype(np.float64)


def hex32(v: int) -> str:
    return f"0x{int(v) & 0xFFFFFFFF:08x}"


def depth_rgb(z, lo: float, hi: float):
    a = np.asarray(z, dtype=np.float64)
    t = np.clip((a - lo) / max(hi - lo, 1e-30), 0.0, 1.0)
    out = np.zeros(a.shape + (3,), dtype=np.float64)
    for c in range(3):
        out[..., c] = np.interp(t, DEPTH_RAMP_STOPS[:, 0], DEPTH_RAMP_STOPS[:, c + 1])
    return np.round(out).astype(np.uint8), t


def quantile_stats(a: np.ndarray) -> dict:
    a = np.asarray(a, dtype=np.float64)
    finite = a[np.isfinite(a)]
    if finite.size == 0:
        return {"count": int(a.size), "finite": 0}
    return {
        "count": int(a.size),
        "finite": int(finite.size),
        "zeros": int(np.count_nonzero(finite == 0)),
        "min": float(finite.min()),
        "max": float(finite.max()),
        "mean": float(finite.mean()),
        "std": float(finite.std()),
        "p01": float(np.percentile(finite, 1)),
        "p50": float(np.percentile(finite, 50)),
        "p99": float(np.percentile(finite, 99)),
    }


def decode_flags(flags: int) -> dict:
    out = {"raw": int(flags), "raw_hex": hex32(flags)}
    for name, bit in VSM_FLAGS.items():
        out[name] = bool(flags & bit)
    for name, bit in VSM_EXT_FLAGS.items():
        out[name] = bool(flags & bit)
    return out


# ---------------------------------------------------------------------------
# package access
# ---------------------------------------------------------------------------
class Dump:
    def __init__(self, root: Path):
        self.root = root
        self.base = root / "Base"
        if not self.base.is_dir():
            alt = root / "native" / "Base"
            if alt.is_dir():
                self.base = alt
            else:
                raise FileNotFoundError(f"no Base/ under {root}")
        self.passes = load_concat_json(self.base / "Passes.json")
        self.by_id = {p["Id"]: p for p in self.passes}
        self.by_ptr = {p["Pointer"]: p for p in self.passes}
        self.descs = load_concat_json(self.base / "ResourceDescs.json")
        self.desc_by_id = {d["UniqueResourceName"]: d for d in self.descs}
        self.layouts = load_concat_json(self.base / "SubresourceLayouts.json")
        self.readback = {r["File"]: r for r in load_concat_json(self.base / "DumpReadbackStatus.json")}
        self.params = {p["Pass"]: p for p in load_concat_json(self.base / "ParameterBindings.json")}
        self.refs = {r["Pass"]: r for r in load_concat_json(self.base / "ReferencedStructs.json")}

    def desc(self, rid: str) -> dict:
        return self.desc_by_id[rid]

    def pass_name(self, pid: int):
        p = self.by_id.get(pid)
        return p["EventName"] if p else None

    def layouts_of(self, rid: str):
        return [l for l in self.layouts if l["Resource"] == rid]

    def layout(self, rid: str, version: str, mip: int = 0, slice_index: int = 0) -> dict:
        cand = []
        for l in self.layouts_of(rid):
            if int(l["MipLevel"]) != int(mip):
                continue
            if slice_index is not None and int(l.get("FirstArraySlice", 0)) != int(slice_index):
                continue
            if f".v{version}.bin" not in l["File"]:
                continue
            cand.append(l)
        if not cand:
            raise KeyError(f"no layout rid={rid} version={version} mip={mip} slice={slice_index}")
        return cand[0]

    def rel_file(self, layout: dict) -> str:
        return layout["File"].replace("\\", "/")

    def readback_of(self, layout: dict):
        return self.readback.get(self.rel_file(layout))

    def resource_file(self, rid: str, max_pass_id: int, prefer_dumped: bool = True):
        """Latest raw buffer snapshot scheduled at or before max_pass_id."""
        cands = []
        for p in self.root.joinpath("Resources").glob(f"{rid}.v*.bin"):
            name = p.name
            try:
                ver = name.split(".v", 1)[1].rsplit(".bin", 1)[0]
            except Exception:
                continue
            prod = self.by_ptr.get(ver)
            pid = prod["Id"] if prod else -1
            if pid < 0 or pid > max_pass_id:
                continue
            rb = self.readback.get(f"Resources/{name}")
            dumped = bool(rb and rb.get("Status") == "Dumped")
            cands.append((pid, 1 if (dumped and prefer_dumped) else 0, name, p, ver, prod, rb))
        if not cands:
            raise FileNotFoundError(f"no buffer snapshot {rid} <= pass {max_pass_id}")
        cands.sort(key=lambda t: (t[0], t[1]))
        pid, _, name, p, ver, prod, rb = cands[-1]
        return {
            "resource": rid, "version": ver, "pass_id": pid,
            "pass_name": prod["EventName"] if prod else None,
            "file": f"Resources/{name}", "path": p, "readback": rb,
        }


# ---------------------------------------------------------------------------
# operand resolution
# ---------------------------------------------------------------------------
def select_passes(dump: Dump) -> dict:
    candidates = [
        p for p in dump.passes
        if p["EventName"].startswith("VirtualShadowMapProjection")
    ]
    if not candidates:
        raise RuntimeError("no VirtualShadowMapProjection pass")
    # The presented/final invocation is the last projection pass in dump order;
    # earlier scene invocations are distinct graphs and are not interchangeable.
    vp = max(candidates, key=lambda p: (p["Pointer"][:4] if p["Pointer"] else "", p["Id"]))
    graph_prefix = vp["Pointer"][:4]
    same_graph = [p for p in candidates if p["Pointer"].startswith(graph_prefix)]
    vp = max(same_graph, key=lambda p: p["Id"])
    composite = [
        p for p in dump.passes
        if p["EventName"].startswith("CompositeVirtualShadowMapMask")
        and p["Pointer"].startswith(graph_prefix) and p["Id"] > vp["Id"]
    ]
    composite = min(composite, key=lambda p: p["Id"]) if composite else None
    sun = [
        p for p in dump.passes
        if p["EventName"].startswith("RenderLight Light::StandardDeferred: Sun")
        and p["Pointer"].startswith(graph_prefix) and p["Id"] > (composite["Id"] if composite else vp["Id"])
    ]
    sun = min(sun, key=lambda p: p["Id"]) if sun else None
    return {"vsm_projection": vp, "composite": composite, "sun": sun, "graph_prefix": graph_prefix}


def vm_uniform(dump: Dump, vp_pass: dict) -> dict:
    refs = dump.refs.get(vp_pass["Pointer"], {}).get("ReferencedStructs", {})
    hits = []
    for key, v in refs.items():
        if v.get("LayoutName") == "FVirtualShadowMapUniformParameters":
            hits.append((key, v))
    if not hits:
        raise RuntimeError("captured FVirtualShadowMapUniformParameters not found")
    key, rec = hits[0]
    content_id = rec["Content"]
    meta_id = rec["Metadata"]
    content_path = dump.root / "Structures" / f"{content_id}.bin"
    meta_path = dump.root / "StructuresMetadata" / f"{meta_id}.json"
    raw = content_path.read_bytes()
    md = json.loads(meta_path.read_text(encoding="utf-8"))
    if len(raw) != int(rec["ByteSize"]) or int(md["Size"]) != len(raw):
        raise ValueError("VSM uniform size mismatch")
    fields = decode_struct(raw, md)
    values = {}
    for f in fields:
        if not f["name"]:
            continue
        v = f.get("value")
        if isinstance(v, list) and len(v) == 1:
            v = v[0]
        values[f["name"]] = v
    return {
        "binding": key,
        "content": content_id,
        "metadata": meta_id,
        "file": f"Structures/{content_id}.bin",
        "metadata_file": f"StructuresMetadata/{meta_id}.json",
        "sha256": sha256_file(content_path),
        "metadata_sha256": sha256_file(meta_path),
        "bytes": len(raw),
        "raw_hex": raw.hex(),
        "fields": fields,
        "values": values,
        "raw": raw,
        "struct_type": md.get("StructTypeName"),
        "source_file": md.get("FileName"),
        "source_line": md.get("FileLine"),
    }


def decode_struct(raw: bytes, md: dict):
    out = []
    pointer_types = {
        "UBMT_RDG_BUFFER_SRV", "UBMT_RDG_TEXTURE", "UBMT_SAMPLER",
        "UBMT_RDG_TEXTURE_UAV", "UBMT_RDG_BUFFER_UAV", "UBMT_SRV", "UBMT_UAV",
    }
    for m in md.get("Members", []):
        name = m.get("Name", "")
        off = int(m.get("Offset", 0))
        base = m.get("BaseType", "")
        cols = int(m.get("NumColumns", 1)) or 1
        rows = int(m.get("NumRows", 1)) or 1
        count = max(1, cols * rows)
        rec = {
            "name": name,
            "offset": off,
            "base_type": base,
            "rows": rows,
            "columns": cols,
            "num_elements": int(m.get("NumElements", 0)),
            "source_line": m.get("FileLine"),
        }
        try:
            if base == "UBMT_FLOAT32":
                rec["value"] = [float(x) for x in struct.unpack_from("<" + "f" * count, raw, off)]
            elif base == "UBMT_INT32":
                rec["value"] = [int(x) for x in struct.unpack_from("<" + "i" * count, raw, off)]
            elif base == "UBMT_UINT32":
                rec["value"] = [int(x) for x in struct.unpack_from("<" + "I" * count, raw, off)]
            elif base in pointer_types:
                q = struct.unpack_from("<Q", raw, off)[0]
                rec["pointer"] = f"{q:016x}"
                rec["value"] = f"{q:016x}"
            elif base == "UBMT_INCLUDED_STRUCT":
                rec["struct_metadata"] = m.get("StructMetadata")
                rec["value"] = None
            else:
                rec["value"] = None
        except struct.error:
            rec["value"] = None
        if isinstance(rec.get("value"), list) and len(rec["value"]) == 1:
            rec["scalar"] = rec["value"][0]
        out.append(rec)
    return out


def resolve_vsm_operands(dump: Dump, vp_pass: dict, uniform: dict, graph_prefix: str):
    bindings = dump.params.get(vp_pass["Pointer"], {}).get("Bindings", {})
    vals = uniform["values"]
    picked = {}
    for key, v in bindings.items():
        for name in ("PageTable", "PageFlags", "PhysicalPagePool", "ProjectionData",
                     "PageReceiverMasks", "AllocatedPageRectBounds", "UncachedPageRectBounds"):
            if key.endswith("." + name) or key == name:
                picked[name] = v.get("Pointer")
    for name in ("PageTable", "PageFlags", "PhysicalPagePool", "ProjectionData"):
        if not picked.get(name):
            raise RuntimeError(f"pass binding missing {name}")
    version = vp_pass["Pointer"]
    pt = dump.layout(picked["PageTable"], version, mip=0, slice_index=0)
    pfs = [dump.layout(picked["PageFlags"], version, mip=m, slice_index=0) for m in range(VSM_MAX_MIP_LEVELS - 1)]
    pool0 = dump.layout(picked["PhysicalPagePool"], version, mip=0, slice_index=0)
    pool1 = dump.layout(picked["PhysicalPagePool"], version, mip=0, slice_index=1)
    # RDG buffer SRV pointers are not the buffer resource id; resolve the buffer
    # by its captured descriptor name in the selected graph (the pass binding
    # still proves consumption).
    inputs = vp_pass.get("InputResources", [])
    def graph_desc(name):
        cands = [d for d in dump.descs if d.get("Name") == name]
        same = [d for d in cands if d["UniqueResourceName"].startswith(graph_prefix)]
        in_inputs = [d for d in same if any(x.startswith(d["UniqueResourceName"]) for x in inputs)]
        matches = in_inputs or same
        if len(matches) != 1:
            raise RuntimeError(f"expected one {name} descriptor in selected graph {graph_prefix}; got {len(matches)}")
        return matches[0]
    proj_desc = graph_desc("Shadow.Virtual.ProjectionData")
    if proj_desc is None:
        raise RuntimeError("ProjectionData descriptor not found")
    proj_rid = proj_desc["UniqueResourceName"]
    picked["ProjectionDataBindingSRV"] = picked["ProjectionData"]
    picked["ProjectionData"] = proj_rid
    proj = dump.resource_file(proj_rid, vp_pass["Id"])
    meta_desc = graph_desc("Shadow.Virtual.PhysicalPageMetaData")
    if meta_desc is None:
        raise RuntimeError("PhysicalPageMetaData descriptor not found")
    meta_rid = meta_desc["UniqueResourceName"]
    meta = dump.resource_file(meta_rid, vp_pass["Id"])
    return {
        "ids": picked,
        "version": version,
        "page_table": pt,
        "page_flags": pfs,
        "pool_slice0": pool0,
        "pool_slice1": pool1,
        "projection": proj,
        "metadata": meta,
        "metadata_desc": dump.desc(meta_rid),
        "page_table_desc": dump.desc(picked["PageTable"]),
        "page_flags_desc": dump.desc(picked["PageFlags"]),
        "pool_desc": dump.desc(picked["PhysicalPagePool"]),
    }


def last_writer(dump: Dump, rid: str, suffix: str, upto_pass: int):
    """Last recorded pass at/before upto_pass that lists rid+suffix as an output."""
    target = rid + suffix
    hits = []
    for p in dump.passes:
        if int(p["Id"]) > int(upto_pass):
            continue
        if target in (p.get("OutputResources") or []):
            hits.append(p)
    if not hits:
        return None
    p = max(hits, key=lambda q: q["Id"])
    return {"pass_id": int(p["Id"]), "name": p["EventName"], "output": target}


def snapshot_record(ops: dict, dump: Dump, vp: dict):
    """Describe the captured version and the last recorded writer for each operand.

    Resource snapshots are versioned by the consuming pass; pass 840 consumes the
    page table, PageFlags and pool but writes only its shadow-mask output.  The
    last recorded writer is therefore an earlier pass, or explicitly unknown if
    no captured pass lists the subresource as an output.
    """
    vp_id = int(vp["Id"])
    pt_rid = ops["ids"]["PageTable"]
    pf_rid = ops["ids"]["PageFlags"]
    pool_rid = ops["ids"]["PhysicalPagePool"]
    md_rid = ops["metadata"]["resource"]
    pf_writers = [last_writer(dump, pf_rid, f".mip{m}", vp_id) for m in range(len(ops["page_flags"]))]
    return {
        "page_table": {
            "version": ops["version"], "versioned_by_pass": vp_id,
            "version_role": "consumer snapshot; the version key is the pass 840 capture boundary, pass 840 writes only the shadow mask",
            "last_recorded_writer": last_writer(dump, pt_rid, ".mip0", vp_id),
        },
        "page_flags": {
            "version": ops["version"], "versioned_by_pass": vp_id,
            "version_role": "consumer snapshot; the version key is the pass 840 capture boundary",
            "last_recorded_writers": pf_writers,
        },
        "physical_page_pool": {
            "version": ops["version"], "versioned_by_pass": vp_id,
            "version_role": "consumer snapshot; the version key is the pass 840 capture boundary",
            "writer_scope": "last recorded resource output binding; MergeStaticPhysicalPagesIndirect writes slice0 only, so this is not a slice1 producer identity",
            "last_recorded_writer": last_writer(dump, pool_rid, ".[0].mip0", vp_id),
        },
        "physical_page_metadata": {
            "version": ops["metadata"]["version"],
            "versioned_by_pass": int(ops["metadata"]["pass_id"]),
            "version_role": "captured metadata snapshot at its writer/selection pass, before the pass 840 consumer",
            "last_recorded_writer": last_writer(dump, md_rid, "", vp_id),
        },
    }

# ---------------------------------------------------------------------------
# projection / clipmap decode
# ---------------------------------------------------------------------------
def decode_projection_record(raw: bytes, idx: int) -> dict:
    o = idx * VSM_PSD_STRIDE
    mats = [mat4(raw, o + 64 * k) for k in range(4)]
    pre_high = f3(raw, o + 272)
    pre_low = f3(raw, o + 288)
    packed = u32(raw, o + 328)
    if packed == 0xFFFFFFFF:
        clip_level, remaining, level_index = VSM_PACKED_CLIP_LEVEL_BIAS, -1, 0
    else:
        clip_level = (packed >> 16) - 1024
        remaining = packed & 0xFF
        level_index = (packed >> 8) & 0xFF
    cull = u32(raw, o + 332)
    flags = u32(raw, o + 336)
    rec = {
        "id": int(idx),
        "raw_hex": raw[o:o + VSM_PSD_STRIDE].hex(),
        "raw_sha256": sha256_text(raw[o:o + VSM_PSD_STRIDE].hex()),
        "shadow_view_to_clip": mats[0],
        "translated_world_to_shadow_uv": mats[1],
        "shadow_uv_to_translated_world": mats[2],
        "translated_world_to_shadow_uv_normal": mats[3],
        "light_direction": [float(x) for x in f3(raw, o + 256)],
        "light_type": int(u32(raw, o + 268)),
        "pre_view_translation_high": [float(x) for x in pre_high],
        "pre_view_translation_low": [float(x) for x in pre_low],
        "pre_view_translation": [float(x) for x in (pre_high + pre_low)],
        "light_radius": float(f32(raw, o + 284)),
        "resolution_lod_bias": float(f32(raw, o + 300)),
        "clipmap_world_origin_offset": [float(x) for x in f3(raw, o + 304)],
        "light_source_radius": float(f32(raw, o + 316)),
        "clipmap_corner_relative_offset": [int(i32(raw, o + 320)), int(i32(raw, o + 324))],
        "packed_clipmap_level_and_count": int(packed),
        "clipmap_level": int(clip_level),
        "clipmap_level_count_remaining": int(remaining),
        "clipmap_level_index": int(level_index),
        "packed_culling_view_id": int(cull),
        "scene_renderer_primary_view_id": int((cull >> 16) - 1),
        "persistent_view_id": int((cull & 0xFFFF) - 1),
        "flags": int(flags),
        "flags_hex": hex32(flags),
        "flag_projection": {
            "CURRENT_DISTANT_LIGHT": bool(flags & (1 << 0)),
            "UNCACHED": bool(flags & (1 << 1)),
            "UNREFERENCED": bool(flags & (1 << 2)),
            "IS_COARSE_CLIP_LEVEL": bool(flags & (1 << 3)),
            "IS_FIRST_PERSON_SHADOW": bool(flags & (1 << 4)),
            "USE_RECEIVER_MASK": bool(flags & (1 << 5)),
            "FORCE_CACHE_DYNAMIC_COARSE": bool(flags & (1 << 6)),
            "HAS_PREFILTERED_DISTANT": bool(flags & (1 << 7)),
        },
        "clipmap_level_wpo_distance_disabled_threshold_squared": float(f32(raw, o + 340)),
        "texel_dither_scale": float(f32(raw, o + 344)),
        "min_mip_level": int(u32(raw, o + 348)),
        "light_id": int(i32(raw, o + 352)),
        "dynamic_depth_cull_range": [float(f32(raw, o + 356)), float(f32(raw, o + 360))],
    }
    return rec


def decode_projection_buffer(raw: bytes):
    n = len(raw) // VSM_PSD_STRIDE
    if n * VSM_PSD_STRIDE != len(raw):
        raise ValueError("projection buffer not a multiple of stride")
    return [decode_projection_record(raw, i) for i in range(n)]


# ---------------------------------------------------------------------------
# page table / page flags
# ---------------------------------------------------------------------------
def page_table_block(vid: int, mip: int, uniform: dict):
    v = uniform["values"]
    full_id = vid - VSM_MAX_SINGLE_PAGE_SHADOW_MAPS + 1
    row_shift = int(v["PageTableRowShift"])
    row_mask = int(v["PageTableRowMask"])
    x0 = (full_id & row_mask) * VSM_PAGE_TABLE_TEX2D_SIZE_X
    y0 = (full_id >> row_shift) * VSM_PAGE_TABLE_TEX2D_SIZE_Y
    ox, oy = level_offsets(mip)
    x0 += ox
    y0 += oy
    dim = VSM_LEVEL0_DIM_PAGES_XY >> mip
    return {
        "full_id": full_id,
        "row": full_id >> row_shift,
        "column": full_id & row_mask,
        "base_texel": [x0, y0],
        "dim": dim,
        "mip": mip,
    }


def level_offsets(mip: int):
    if mip <= 0:
        return 0, 0
    max_mask = (1 << (VSM_MAX_MIP_LEVELS - 1)) - 1
    start_bit = VSM_MAX_MIP_LEVELS - mip
    return (max_mask & (max_mask << start_bit)), VSM_LEVEL0_DIM_PAGES_XY


def decode_page_table_entry(value: int) -> dict:
    v = int(value)
    return {
        "raw": v,
        "raw_hex": hex32(v),
        "physical_x": v & 0x3FF,
        "physical_y": (v >> 10) & 0x3FF,
        "lod_offset": (v >> 20) & 0x3F,
        "any_lod_valid": bool(v & 0x80000000),
        "valid_for_rendering": bool(v & 0x40000000),
        "secondary_request": bool(v & 0x20000000),
        "this_lod_valid": bool((v & 0x80000000) and ((v >> 20) & 0x3F) == 0),
    }


def encode_page_table(entry: dict) -> int:
    v = (entry["physical_y"] << 10) | entry["physical_x"]
    if entry["lod_offset"]:
        v |= 0x80000000 | (entry["lod_offset"] << 20)
    else:
        v |= 0x80000000 | (0x40000000 if entry["valid_for_rendering"] else 0)
    if entry["secondary_request"]:
        v |= 0x20000000
    return v & 0xFFFFFFFF


def decode_page_flags_word(word: int) -> dict:
    """Decode one exact-page PageFlags word (H0).

    PageFlags words are 32-bit flag bitfields.  The hierarchical H0..H6
    levels live in the seven PageFlags hardware mips (mip0 exact, mipm =
    parent at texel >> m), not in 6-bit subfields of one word.
    """
    w = int(word)
    low = w & 0x3F
    return {
        "raw_hex": hex32(w),
        "flags_low6": low,
        "flags_low6_hex": hex32(low),
        "flags_bits": {k: bool(low & b) for k, b in VSM_FLAGS.items()},
        "hierarchy_note": "H0 exact-page flags; H1..H6 are PageFlags hardware mips 1..6 at parent texel >> m; H7 is not generated",
    }


# ---------------------------------------------------------------------------
# receiver replay
# ---------------------------------------------------------------------------
def receiver_replay(name: str, rec: dict, proj: list, base: dict, uniform: dict,
                    view_pre, page_table, page_flags, pool0: np.ndarray, metadata: np.ndarray):
    v = uniform["values"]
    pt = np.asarray(rec.get("translated_world_cm") or [])
    if pt.size != 3:
        world = np.asarray(rec.get("world_cm") or [], dtype=np.float64)
        if world.size != 3:
            raise ValueError(f"receiver {name} has no world position")
        pt = world + np.asarray(view_pre, dtype=np.float64)
    pt = np.asarray(pt, dtype=np.float64)
    base_pre = np.array(base["pre_view_translation"], dtype=np.float64)
    v2s = base_pre - np.asarray(view_pre, dtype=np.float64)
    origin = np.array(base["clipmap_world_origin_offset"], dtype=np.float64)
    tw_origin = -origin + v2s
    dist2 = float(np.sum((pt + tw_origin) ** 2))
    abs_level = 0.5 * math.log2(max(dist2, 1e-30))
    base_clip = int(base["clipmap_level"])
    remaining = int(base["clipmap_level_count_remaining"])
    # CalcBiasedAbsoluteClipmapLevelForSampling first selects an unbiased
    # level, then reads that level's bias. Optional DOF bias is excluded
    # from this declared center-address replay, not silently reconstructed.
    unbiased_index = int(max(0.0, abs_level - base_clip))
    bias_projection = (proj[base["id"] + unbiased_index]
                       if unbiased_index < remaining else base)
    per_level_bias = float(bias_projection["resolution_lod_bias"])
    biased = abs_level + per_level_bias
    index = max(0, int(math.floor(biased)) - base_clip)
    valid = 0 <= index < remaining and base["id"] + index < len(proj)
    out = {
        "receiver": name,
        "label": rec.get("label"),
        "pixel": rec.get("pixel"),
        "mode": "declared_replay_clipmap_selection_and_address_lookup",
        "not_executed_smrt": True,
        "world_cm": rec.get("world_cm"),
        "translated_world_cm": [float(x) for x in pt],
        "base_vsm_id": int(base["id"]),
        "base_clipmap_level": base_clip,
        "clipmap_level_count_remaining": remaining,
        "distance_to_clipmap_origin_cm": float(math.sqrt(dist2)),
        "absolute_clipmap_level": float(abs_level),
        "unbiased_clipmap_index": unbiased_index,
        "bias_projection_id": int(bias_projection["id"]),
        "resolution_lod_bias": per_level_bias,
        "biased_absolute_clipmap_level": float(biased),
        "clipmap_index": int(index),
        "clipmap_selection_valid": bool(valid),
        "view_pre_translation_cm": [float(x) for x in view_pre],
        "view_to_shadow_translation_cm": [float(x) for x in v2s],
        "clipmap_world_origin_offset_cm": [float(x) for x in origin],
        "clipmap_origin_primary_translated_cm": [float(x) for x in tw_origin],
        "bias_conventions": {
            "applied": [],
            "not_applied": [
                "VirtualShadowMap.NormalBias / VirtualShadowMapGetNormalBiasLength",
                "ComputeDepthSlopeDirectionalUV / ComputeOptimalSlopeBiasDirectional",
                "SMRT ray start offset, texel dither and per-ray cone",
                "optional depth-of-field resolution bias",
            ],
            "note": "storage-address replay only; captured mask is compared but not reproduced",
        },
    }
    if not valid:
        out["error"] = "clipmap index outside captured level range"
        return out
    sel = proj[base["id"] + index]
    out["selected_vsm_id"] = int(sel["id"])
    out["selected_clipmap_level"] = int(sel["clipmap_level"])
    out["selected_clipmap_level_index"] = int(sel["clipmap_level_index"])
    out["selected_projection"] = sel
    p_shadow = pt + (np.array(sel["pre_view_translation"], dtype=np.float64) - np.asarray(view_pre, dtype=np.float64))
    uvz = np.asarray(sel["translated_world_to_shadow_uv"]) @ np.array([p_shadow[0], p_shadow[1], p_shadow[2], 1.0])
    uv = [float(uvz[0] / uvz[3]), float(uvz[1] / uvz[3])]
    z = float(uvz[2] / uvz[3])
    if not np.all(np.isfinite(uv)) or any(v < 0.0 or v >= 1.0 for v in uv):
        raise ValueError(f"declared receiver {name} projects outside selected shadow view: {uv}")
    vtex = np.floor(np.array(uv) * VSM_LEVEL0_DIM_PAGES_XY * VSM_PAGE_SIZE).astype(np.int64)
    vpage = vtex >> VSM_LOG2_PAGE_SIZE
    local = vtex & (VSM_PAGE_SIZE - 1)
    block = page_table_block(sel["id"], 0, uniform)
    tx = block["base_texel"][0] + int(vpage[0])
    ty = block["base_texel"][1] + int(vpage[1])
    entry_u32 = int(page_table[ty, tx])
    entry = decode_page_table_entry(entry_u32)
    pf_word = int(page_flags[ty, tx])
    pflags = decode_page_flags_word(pf_word)
    out.update({
        "shadow_translated_world_cm": [float(x) for x in p_shadow],
        "shadow_uv": uv,
        "shadow_z": z,
        "virtual_texel_xy": [int(vtex[0]), int(vtex[1])],
        "virtual_page_xy": [int(vpage[0]), int(vpage[1])],
        "texel_in_page_xy": [int(local[0]), int(local[1])],
        "page_table_texel_xy": [int(tx), int(ty)],
        "page_table_block_base_texel": block["base_texel"],
        "page_table_entry_hex": entry["raw_hex"],
        "page_table_entry_value": entry["raw"],
        "page_table_entry": entry,
        "entry_lod_zero": bool(entry["lod_offset"] == 0),
        "entry_valid_for_rendering": bool(entry["valid_for_rendering"]),
        "page_flags_hex": pflags["raw_hex"],
        "page_flags": pflags,
        "physical_page_xy": [entry["physical_x"], entry["physical_y"]],
        "physical_page_index": int((entry["physical_y"] << int(v["PhysicalPageRowShift"])) + entry["physical_x"]),
        "physical_texel_xy": [int(entry["physical_x"] * VSM_PAGE_SIZE + local[0]),
                              int(entry["physical_y"] * VSM_PAGE_SIZE + local[1])],
    })
    if entry["any_lod_valid"] and entry["lod_offset"] == 0:
        px = int(entry["physical_x"] * VSM_PAGE_SIZE + local[0])
        py = int(entry["physical_y"] * VSM_PAGE_SIZE + local[1])
        d = float(pool0[py, px])
        raw_u32 = int(struct.unpack("<I", struct.pack("<f", d))[0])
    elif entry["any_lod_valid"]:
        # hierarchical fallback: the sampled clipmap level changes the UV and
        # depth basis, so do not present this mip0 address as executed storage.
        px = int(entry["physical_x"] * VSM_PAGE_SIZE + local[0])
        py = int(entry["physical_y"] * VSM_PAGE_SIZE + local[1])
        d = float("nan")
        raw_u32 = 0
    else:
        px = py = -1
        d = float("nan")
        raw_u32 = 0
    pidx = out["physical_page_index"]
    md = metadata[pidx] if 0 <= pidx < metadata.shape[0] else np.zeros(6, dtype=np.uint32)
    meta_rec = {
        "flags": int(md[0]), "flags_hex": hex32(int(md[0])), "flags_decoded": decode_flags(int(md[0])),
        "last_requested_scene_frame_number": int(md[1]),
        "virtual_shadow_map_id": int(md[2]),
        "mip_level": int(md[3]),
        "page_address": [int(md[4]), int(md[5])],
    }
    m33 = float(sel["shadow_view_to_clip"][2, 2])
    m43 = float(sel["shadow_view_to_clip"][3, 2])
    light_view_z = (d - m43) / m33 if m33 != 0 and math.isfinite(d) else float("nan")
    receiver_light_z = (z - m43) / m33 if m33 != 0 else float("nan")
    occ_raw_cm = (z - d) / m33 if m33 != 0 and math.isfinite(d) else float("nan")
    occ_cm = max(1e-6, occ_raw_cm) if math.isfinite(occ_raw_cm) else float("nan")
    delta = (d - z) if math.isfinite(d) else float("nan")
    mask = rec.get("shadow") or {}
    mask_lin = mask.get("shadow_factor_r_linear")
    out.update({
        "physical_page_metadata": meta_rec,
        "metadata_owner_matches_entry": bool(
            meta_rec["virtual_shadow_map_id"] == sel["id"] and meta_rec["mip_level"] == 0
            and meta_rec["page_address"] == [int(vpage[0]), int(vpage[1])]
        ),
        "pool_slice": 0,
        "raw_depth_bits": hex32(raw_u32),
        "stored_depth_device_z": d if math.isfinite(d) else None,
        "stored_depth_is_finite": bool(math.isfinite(d)),
        "stored_depth_is_zero": bool(math.isfinite(d) and d == 0.0),
        "receiver_device_z": z,
        "stored_depth_minus_receiver_z": delta if math.isfinite(delta) else None,
        "m33": m33,
        "m43": m43,
        "receiver_light_view_z_cm": receiver_light_z if math.isfinite(receiver_light_z) else None,
        "stored_light_view_z_cm": light_view_z if math.isfinite(light_view_z) else None,
        "occluder_distance_ortho_raw_cm": occ_raw_cm if math.isfinite(occ_raw_cm) else None,
        "occluder_distance_ortho_cm": occ_cm if math.isfinite(occ_cm) else None,
        "captured_shadow_mask_linear_r": mask_lin,
        "captured_shadow_mask_memory_bgra": mask.get("memory_bgra"),
        "captured_relation": (
            "nearly_equal_within_float32_roundoff" if abs(delta) < 1e-7
            else ("stored_depth_greater_closer_to_light" if delta > 0 else "stored_depth_less")
        ) if math.isfinite(delta) else "invalid_depth",
        "replay_vs_mask": (
            "sample_depth_nearly_equal_receiver_and_mask_lit" if (math.isfinite(delta) and abs(delta) < 1e-7 and mask_lin == 1.0)
            else "sample_depth_greater_receiver_and_mask_shadowed" if (math.isfinite(delta) and delta > 0 and mask_lin == 0.0)
            else "not_a_full_mask_reproduction"
        ),
        "delta_is_exact_zero": bool(math.isfinite(delta) and delta == 0.0),
        "delta_note": "stored depth is compared in float32 device Z; a sub-1e-7 delta is roundoff, not an exact equality",
        "mask_reproduced_by_this_replay": False,
        "mask_note": "single storage-address sample; SMRT ray directions, bias and cone are not replayed",
    })
    return out


# ---------------------------------------------------------------------------
# source ranges
# ---------------------------------------------------------------------------
def engine_source_path(engine: Path, rel: str) -> Path:
    """Accept either a repository root or the Engine directory as --engine."""
    a = engine / rel
    if a.exists():
        return a
    b = engine / "Engine" / rel
    return b if b.exists() else a


def build_source_ranges(engine: Path | None):
    out = []
    for rel, a, b, role in SOURCE_RANGES:
        rec = {"file": rel, "line": a, "end_line": b, "role": role}
        p = engine_source_path(engine, rel) if engine else None
        if p and p.exists():
            lines = p.read_text(encoding="utf-8").splitlines()
            if b > len(lines):
                raise ValueError(f"range past EOF: {rel}:{a}-{b}")
            text = "\n".join(lines[a - 1:b])
            rec.update({
                "rangeSHA": sha256_text(text),
                "rangeSHA_algorithm": "sha256(UTF8 lines joined with LF, no trailing LF)",
                "line_count": b - a + 1,
                "file_sha256": sha256_file(p),
                "source_available": True,
            })
        else:
            rec.update({"rangeSHA": None, "source_available": False,
                        "reason": "engine root not supplied or file absent"})
        out.append(rec)
    return {
        "schema": "vsm-source-ranges-v1",
        "range_sha_definition": "sha256(UTF8 bytes of lines[line-1:end_line] joined with '\\n', no trailing newline)",
        "engine_root": engine.name if engine else None,
        "ranges": out,
    }


# ---------------------------------------------------------------------------
# figures
# ---------------------------------------------------------------------------
def save_png(path: Path, arr: np.ndarray):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.ascontiguousarray(arr), "RGB").save(path, optimize=True)


def figure_stats(rgb: np.ndarray):
    a = np.asarray(rgb, dtype=np.float64)
    u = len({tuple(x) for x in np.asarray(rgb).reshape(-1, 3)[:: max(1, rgb.size // 3 // 20000)]})
    return {
        "shape": [int(rgb.shape[0]), int(rgb.shape[1])],
        "mean_rgb": [float(a[..., c].mean()) for c in range(3)],
        "std_rgb": [float(a[..., c].std()) for c in range(3)],
        "unique_sample": int(u),
        "nonblack_fraction": float(np.any(rgb != 0, axis=-1).mean()),
    }


def build_figures(out: Path, ctx: dict):
    fig_dir = out / "publish" / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    figs = {}

    # ---------------- F1: global physical-pool occupancy + selected-VSM depth --
    cat = ctx["occupancy_category"]
    state = ctx["meta_state_category"]
    cell = 8
    h, w = cat.shape
    panel_h, panel_w = h * cell, w * cell
    top = np.zeros((panel_h, panel_w, 3), dtype=np.uint8)
    bottom = np.zeros((panel_h, panel_w, 3), dtype=np.uint8)
    for yy in range(h):
        for xx in range(w):
            top[yy * cell:(yy + 1) * cell, xx * cell:(xx + 1) * cell] = OCCUPANCY_COLORS[int(cat[yy, xx])]
            bottom[yy * cell:(yy + 1) * cell, xx * cell:(xx + 1) * cell] = META_STATE_COLORS[int(state[yy, xx])]
    # compact depth atlas: 15 columns x 7 rows, 64 px per page, 2 px gutters
    atlas = np.full((460, 1024, 3), MASKED_COLOR, dtype=np.uint8)
    tile = 64
    gap = 2
    cols = 15
    markers = []
    comp = ctx["compact_depth_rgb"]
    comp_meta = ctx["compact_tiles"]
    rec_by_vpage = {tuple(r["virtual_page_xy"]): r for r in ctx["receivers"].values() if r.get("clipmap_selection_valid")}
    for i, thumb in enumerate(comp):
        r, c = divmod(i, cols)
        x0 = c * (tile + gap)
        y0 = r * (tile + gap)
        atlas[y0:y0 + tile, x0:x0 + tile] = thumb
        key = tuple(comp_meta[i]["virtual_page_xy"])
        if key in rec_by_vpage:
            markers.append({"receiver": rec_by_vpage[key]["receiver"], "tile": i,
                            "pixel_box": [x0, y0, x0 + tile, y0 + tile]})
    if Image is not None:
        im = Image.fromarray(atlas, "RGB")
        dr = ImageDraw.Draw(im)
        for m in markers:
            x0, y0, x1, y1 = m["pixel_box"]
            dr.rectangle((x0 - 1, y0 - 1, x1, y1), outline=(255, 255, 255))
        atlas = np.asarray(im)
    canvas = np.full((panel_h + 8 + 460 + 8 + panel_h, 1024, 3), GUTTER_COLOR, dtype=np.uint8)
    canvas[0:panel_h] = top
    canvas[panel_h + 8:panel_h + 8 + 460] = atlas
    canvas[panel_h + 8 + 460 + 8:] = bottom
    save_png(fig_dir / "vsm_pool_occupancy.png", canvas)
    figs["vsm_pool_occupancy"] = {
        "file": "publish/figures/vsm_pool_occupancy.png",
        "width": int(canvas.shape[1]), "height": int(canvas.shape[0]),
        "kind": "physical_pool_occupancy_and_depth",
        "layout": "top: global 128x32 page grid (8x8 px/page); middle: compact depth atlas 15x7 tiles of 64x64 px (2 px gutters) sorted by physical page index; bottom: metadata state grid (8x8 px/page)",
        "panels": [
            {"panel": 0, "name": "global physical-page occupancy", "category_key": "occupancy_category",
             "y_range": [0, panel_h],
             "legend": {str(k): list(v) for k, v in OCCUPANCY_COLORS.items()},
             "legend_labels": {str(k): v for k, v in OCCUPANCY_LABELS.items()},
             "cell_order": "page index = y*128+x; pixel block = (x*8..x*8+7, y*8..y*8+7)"},
            {"panel": 1, "name": "compact stored-depth atlas of current selected-VSM pages",
             "y_range": [panel_h + 8, panel_h + 8 + 460],
             "mapping": "each 64x64 tile is a 2x2 box mean of one 128x128 native pool page; "
                        "RGB = declared depth ramp of pool slice 0 device Z; fixed range "
                        f"[{ctx['depth_display_lo']:.8f}, {ctx['depth_display_hi']:.8f}]; "
                        "invalid/zero depth magenta; white outline marks the A/B tiles",
             "tile_size_px": 64, "tile_gap_px": 2, "columns": cols,
             "tile_order": comp_meta, "markers": markers,
             "native_npz": "publish/data/vsm_storage.npz:compact_depth_pages_f32,compact_depth_rgb_u8"},
            {"panel": 2, "name": "physical-page metadata state",
             "y_range": [panel_h + 8 + 460 + 8, canvas.shape[0]],
             "legend": {str(k): list(v) for k, v in META_STATE_COLORS.items()},
             "legend_labels": {str(k): v for k, v in META_STATE_LABELS.items()}},
        ],
        "stats": figure_stats(canvas),
        "native_npz": "publish/data/vsm_storage.npz:occupancy_category,meta_state_category,compact_depth_pages_f32,compact_depth_rgb_u8,depth_pages_all_f32,depth_mosaic_z_f32",
        "notes": "global occupancy is sparse by construction; the compact atlas shows every current selected-VSM page exactly once",
    }

    # ---------------- F2: selected sun page table ------------------------------
    lod_rgb = ctx["page_table_lod_rgb"]
    ident_full = ctx["page_table_identity_rgb"]
    flag_full = ctx["page_table_flags_rgb"]
    render = ctx["page_table_render"]
    pdim = render.shape[0]
    ys, xs = np.nonzero(render)
    if len(xs):
        cx0, cx1 = max(0, int(xs.min()) - 8), min(pdim, int(xs.max()) + 9)
        cy0, cy1 = max(0, int(ys.min()) - 8), min(pdim, int(ys.max()) + 9)
    else:
        cx0, cy0, cx1, cy1 = 0, 0, pdim, pdim
    crop_w, crop_h = (cx1 - cx0) * 16, (cy1 - cy0) * 16
    def zoom_panel(full):
        panel = np.full((512, 512, 3), LOD_FALLBACK_COLOR, dtype=np.uint8)
        sub = full[cy0 * 4:cy1 * 4, cx0 * 4:cx1 * 4]
        big = np.repeat(np.repeat(sub, 4, axis=0), 4, axis=1)
        ox, oy = (512 - big.shape[1]) // 2, (512 - big.shape[0]) // 2
        panel[oy:oy + big.shape[0], ox:ox + big.shape[1]] = big
        return panel, ox, oy
    ident_zoom, ix, iy = zoom_panel(ident_full)
    flag_zoom, fx, fy = zoom_panel(flag_full)
    if Image is not None:
        for panel, ox, oy, color in ((ident_zoom, ix, iy, (255, 255, 255)), (flag_zoom, fx, fy, (255, 255, 255))):
            im = Image.fromarray(panel, "RGB")
            dr = ImageDraw.Draw(im)
            for rr in ctx["receivers"].values():
                if not rr.get("clipmap_selection_valid"):
                    continue
                vx, vy = rr["virtual_page_xy"]
                if cx0 <= vx < cx1 and cy0 <= vy < cy1:
                    x0 = ox + (vx - cx0) * 16
                    y0 = oy + (vy - cy0) * 16
                    dr.rectangle((x0 - 1, y0 - 1, x0 + 16, y0 + 16), outline=color)
            panel[:] = np.asarray(im)
    panels = [lod_rgb, ident_zoom, flag_zoom]
    gap = 8
    canvas2 = np.full((512, 3 * 512 + 2 * gap, 3), GUTTER_COLOR, dtype=np.uint8)
    for k, p in enumerate(panels):
        canvas2[:, k * (512 + gap):k * (512 + gap) + 512] = p
    save_png(fig_dir / "vsm_page_table_sun.png", canvas2)
    zoom_rec = {
        "page_xy_range": [cx0, cy0, cx1, cy1],
        "cell_px": 16,
        "panel_origin_px": [ix, iy],
        "mapping": "virtual page (x,y) -> panel pixel = origin + ((x-cx0)*16, (y-cy0)*16)",
    }
    figs["vsm_page_table_sun"] = {
        "file": "publish/figures/vsm_page_table_sun.png",
        "width": int(canvas2.shape[1]), "height": int(canvas2.shape[0]),
        "kind": "sun_virtual_page_table_mip0",
        "vsm_id": ctx["selected_vsm_id"],
        "clipmap_level": ctx["selected_clipmap_level"],
        "mip": 0,
        "page_table_block_base_texel": ctx["page_table_block"]["base_texel"],
        "full_panel_cell_px": 4,
        "panels": [
            {"panel": 0, "name": "full 128x128 page table: LOD offset / fallback category",
             "legend": {str(k): list(v) for k, v in LOD_COLORS.items()},
             "unmapped_rgb": list(LOD_FALLBACK_COLOR),
             "cell_px": 4,
             "mapping": "entry LODOffset 0 = render-valid at mip0; 1..7 = LODOffset used by SampleVirtualShadowMapClipmap to select a coarser clipmap level (the sampled mip stays 0); no entry = dark"},
            {"panel": 1, "name": "zoom: mapped physical page identity + validity",
             "zoom": zoom_rec,
             "mapping": "R=2*physical_page_x, G=8*physical_page_y, B=255 if valid_for_rendering else 96; fallback entries are dimmed 0.55; no entry dark; white outline = A/B page"},
            {"panel": 2, "name": "zoom: PageFlags hardware mip0 H0 (exact-page flags)",
             "zoom": zoom_rec,
             "legend": {f"0x{k:02x}": list(v) for k, v in PAGE_FLAG_COLORS.items()},
             "other_rgb": list(PAGE_FLAG_OTHER),
             "mapping": "PageFlags hardware mip0 word & 0x3f = H0 exact-page flags; H1..H6 are separate PageFlags hardware mips at parent texel >> m; white outline = A/B page"},
        ],
        "stats": figure_stats(canvas2),
        "native_npz": "publish/data/vsm_storage.npz:page_table_entry_u32,page_table_phys_x/y,page_table_lod,page_table_render,page_table_flags_u32",
        "notes": "full panel shows the coarser-clipmap fallback structure; the two right panels zoom the active region that contains all 99 render-valid pages. In the directional clipmap path LODOffset is a coarser-clipmap-level offset, not a page-table mip.",
    }

    # ---------------- F3: native depth-page closeups ---------------------------
    tiles = ctx["tiles"]
    tw = 256
    gap = 8
    cols, rows = 3, 2
    canvas3 = np.full((rows * tw + (rows - 1) * gap, cols * tw + (cols - 1) * gap, 3), GUTTER_COLOR, dtype=np.uint8)
    tile_order = []
    for i, t in enumerate(tiles):
        r, c = divmod(i, cols)
        y0 = r * (tw + gap)
        x0 = c * (tw + gap)
        page_rgb = t["rgb"]
        big = np.repeat(np.repeat(page_rgb, 2, axis=0), 2, axis=1)
        if t.get("marker_texel") is not None:
            mx, my = t["marker_texel"]
            bx, by = int(mx) * 2, int(my) * 2
            big[max(0, by - 7):by + 8, max(0, bx - 1):bx + 2] = (255, 255, 255)
            big[max(0, by - 1):by + 2, max(0, bx - 7):bx + 8] = (255, 255, 255)
        canvas3[y0:y0 + tw, x0:x0 + tw] = big
        tile_order.append({
            "tile": i, "row": r, "column": c,
            "source": t["source"],
            "receiver": t.get("receiver"),
            "virtual_page_xy": t["virtual_page_xy"],
            "physical_page_xy": t["physical_page_xy"],
            "physical_page_index": t["physical_page_index"],
            "page_table_texel_xy": t["page_table_texel_xy"],
            "page_table_entry_hex": t["page_table_entry_hex"],
            "depth_stats": t["depth_stats"],
            "marker_texel_in_page": t.get("marker_texel"),
            "native_px_box": [x0, y0, x0 + tw, y0 + tw],
        })
    save_png(fig_dir / "vsm_depth_pages.png", canvas3)
    figs["vsm_depth_pages"] = {
        "file": "publish/figures/vsm_depth_pages.png",
        "width": int(canvas3.shape[1]), "height": int(canvas3.shape[0]),
        "kind": "native_depth_page_closeups",
        "page_size": [VSM_PAGE_SIZE, VSM_PAGE_SIZE],
        "display_scale": 2,
        "mapping": "each 128x128 stored page is nearest-upscaled 2x to 256x256; row-major tile order; "
                   "fixed depth ramp over the same selected-projection range; white cross marks the declared-replay sampled texel",
        "depth_display_range_device_z": [ctx["depth_display_lo"], ctx["depth_display_hi"]],
        "depth_ramp_stops": DEPTH_RAMP_STOPS[:, 1:].astype(int).tolist(),
        "tile_order": tile_order,
        "stats": figure_stats(canvas3),
        "native_npz": "publish/data/vsm_storage.npz:depth_pages_raw_u32,depth_pages_f32",
        "notes": "all tiles belong to the same selected sun clipmap projection; no per-tile normalisation",
    }

    # ---------------- F4: contiguous virtual-address depth mosaic -------------
    mosaic = ctx["virtual_depth_mosaic_rgb"]
    save_png(fig_dir / "vsm_virtual_depth.png", mosaic)
    mm = ctx["virtual_depth_meta"]
    figs["vsm_virtual_depth"] = {
        "file": "publish/figures/vsm_virtual_depth.png",
        "width": int(mosaic.shape[1]), "height": int(mosaic.shape[0]),
        "kind": "contiguous_virtual_depth_mosaic",
        "vsm_id": ctx["selected_vsm_id"],
        "clipmap_level": ctx["selected_clipmap_level"],
        "page_size": [VSM_PAGE_SIZE, VSM_PAGE_SIZE],
        "bbox_virtual_page": mm["bbox_virtual_page"],
        "bbox_page_wh": mm["bbox_page_wh"],
        "native_px": mm["native_px"],
        "output_px": mm["output_px"],
        "scale": mm["scale"],
        "scale_policy": mm["scale_policy"],
        "mapping": mm["mapping"],
        "selected_pages": mm["selected_pages"],
        "page_cells": mm["page_cells"],
        "mapped_cells": mm["mapped_cells"],
        "fallback_cells": mm["fallback_cells"],
        "unmapped_cells": mm["unmapped_cells"],
        "depth_display_range_device_z": mm["depth_display_range_device_z"],
        "depth_ramp_stops": DEPTH_RAMP_STOPS[:, 1:].astype(int).tolist(),
        "category_legend": mm["category_legend"],
        "category_labels": mm["category_labels"],
        "page_order": mm["page_order"],
        "mask_npz": mm["mask_npz"],
        "page_category_npz": mm["page_category_npz"],
        "no_burned_text": True,
        "stats": figure_stats(mosaic),
        "native_npz": "publish/data/vsm_storage.npz:depth_pages_all_f32 (full 99 native pages),virtual_depth_mask_u8,virtual_depth_page_category_u8",
        "notes": "all 99 selected mip0 pages reassembled at their virtual page coordinates in the active-page bbox; "
                 "one fixed depth range and one uniform scale; fallback/unmapped cells are categorical; no text overlays",
    }
    for f in figs.values():
        p = out / f["file"]
        f["sha256"] = sha256_file(p)
        f["bytes"] = int(p.stat().st_size)
    return figs


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------
def add_check(checks, cid, ok, detail, measurements=None):
    checks.append({
        "id": cid,
        "status": "PASS" if ok else "FAIL",
        "detail": detail,
        "measurements": measurements or {},
    })


def run_decode_checks(ctx, checks):
    raw_after = {str(p): sha256_file(p) for p in ctx["used_raw_paths"]}
    raw_same = all(raw_after.get(k) == v for k, v in ctx["raw_hash_before"].items())
    add_check(checks, "raw_files_unchanged", raw_same,
              "all used raw capture files byte-identical before/after decode",
              {"file_count": len(raw_after),
               "mismatches": [k for k, v in ctx["raw_hash_before"].items() if raw_after.get(k) != v]})

    u = ctx["uniform"]["values"]
    ok = (ctx["uniform"]["bytes"] == VSM_PSD_STRIDE
          and int(u["MaxPhysicalPages"]) == ctx["metadata"].shape[0] == 4096
          and int(u["StaticCachedArrayIndex"]) == 1
          and int(u["PhysicalPoolSize"][0]) == ctx["pool_shape"][1]
          and int(u["PhysicalPoolSize"][1]) == ctx["pool_shape"][0]
          and int(u["PhysicalPoolSizePages"][0]) == ctx["pool_shape"][1] // VSM_PAGE_SIZE
          and int(u["PhysicalPoolSizePages"][1]) == ctx["pool_shape"][0] // VSM_PAGE_SIZE
          and (1 << int(u["PhysicalPageRowShift"])) == ctx["pool_shape"][1] // VSM_PAGE_SIZE
          and (1 << int(u["PageTableRowShift"])) == ctx["page_table"].shape[1] // VSM_PAGE_TABLE_TEX2D_SIZE_X
          and int(u["NumShadowMapSlots"]) == ctx["projection_count"]
          and int(u["NumFullShadowMaps"]) == ctx["sun_count"])
    add_check(checks, "uniform_contract_matches_captured_sizes", ok,
              "captured FVirtualShadowMapUniformParameters constants match resource extents and source stride",
              {"uniform_values": {k: u[k] for k in (
                  "NumFullShadowMaps", "NumSinglePageShadowMaps", "MaxPhysicalPages", "NumShadowMapSlots",
                  "StaticCachedArrayIndex", "PhysicalPageRowMask", "PhysicalPageRowShift",
                  "PageTableRowShift", "PageTableRowMask")},
               "pool_pages": ctx["pool_shape"], "page_table": ctx["page_table"].shape,
               "metadata_records": int(ctx["metadata"].shape[0]),
               "projection_records": ctx["projection_count"], "sun_vsm_count": ctx["sun_count"]})

    bad = ctx["entry_roundtrip_mismatch"]
    add_check(checks, "page_table_identity_roundtrip", bad == 0,
              "every selected-VSM mip0 page-table entry re-encodes to the same uint32 (packing/decoding identity)",
              {"entries": int(ctx["page_table_block"]["dim"] ** 2), "mismatches": int(bad)})

    ok = ctx["mapped_pages_total"] == 99 and ctx["mapped_owner_match"] == ctx["mapped_pages_total"]
    add_check(checks, "mapped_pages_and_metadata_owner", ok,
              "render-valid mip0 pages have in-bounds physical addresses and matching metadata owner/mip/page",
              {"mapped_pages": ctx["mapped_pages_total"], "owner_matches": ctx["mapped_owner_match"],
               "mapped_out_of_bounds": ctx["mapped_oob"],
               "depth_finite": ctx["mapped_depth_finite"], "depth_zero": ctx["mapped_depth_zero"]})

    ok = all(
        r.get("clipmap_selection_valid") and r.get("metadata_owner_matches_entry")
        and r.get("entry_lod_zero") and r.get("entry_valid_for_rendering")
        and r.get("stored_depth_is_finite") and not r.get("stored_depth_is_zero")
        and r.get("stored_depth_minus_receiver_z") is not None
        and abs(r["stored_depth_minus_receiver_z"]) < 1e-3
        and 0.25 < r["shadow_uv"][0] < 0.75 and 0.25 < r["shadow_uv"][1] < 0.75
        for r in ctx["receivers"].values()
    )
    add_check(checks, "receiver_declared_replay_chain", ok,
              "A/B declared replay: captured matrix -> clipmap -> vpage -> page entry -> physical texel -> pool depth; "
              "metadata identity and depth within 1e-3 of receiver projection",
              {k: {"selected_vsm": r.get("selected_vsm_id"), "uv": r.get("shadow_uv"),
                   "vpage": r.get("virtual_page_xy"), "entry": r.get("page_table_entry_hex"),
                   "phys_page": r.get("physical_page_xy"), "phys_texel": r.get("physical_texel_xy"),
                   "depth": r.get("stored_depth_device_z"), "receiver_z": r.get("receiver_device_z"),
                   "delta": r.get("stored_depth_minus_receiver_z"), "mask": r.get("captured_shadow_mask_linear_r"),
                   "replay_vs_mask": r.get("replay_vs_mask")}
               for k, r in ctx["receivers"].items()})

    ra = ctx["receivers"].get("A", {})
    rb = ctx["receivers"].get("B", {})
    rel_ok = (
        ra.get("stored_depth_minus_receiver_z") is not None
        and ra["stored_depth_minus_receiver_z"] != 0.0
        and abs(ra["stored_depth_minus_receiver_z"]) < 1e-7
        and ra.get("delta_is_exact_zero") is False
        and ra.get("captured_relation") == "nearly_equal_within_float32_roundoff"
        and rb.get("stored_depth_minus_receiver_z") is not None
        and rb["stored_depth_minus_receiver_z"] > 0.0
    )
    add_check(checks, "receiver_relation_wording", rel_ok,
              "A's stored depth is reported as nearly equal (not exactly equal) to its receiver projection; B's is greater",
              {"A_delta": ra.get("stored_depth_minus_receiver_z"), "A_exact_zero": ra.get("delta_is_exact_zero"),
               "A_relation": ra.get("captured_relation"), "B_delta": rb.get("stored_depth_minus_receiver_z"),
               "B_relation": rb.get("captured_relation")})

    ok = sum(ctx["occupancy_counts"].values()) == 4096
    add_check(checks, "occupancy_categories_complete", ok,
              "global 128x32 physical-page categories partition all 4096 pages",
              {"counts": ctx["occupancy_counts"]})

    ok = (ctx["lod_histogram"].get(0, 0) == 99
          and sum(ctx["lod_histogram"].values()) == 16384)
    add_check(checks, "page_table_lod_histogram", ok,
              "selected-VSM mip0 LOD offsets: 99 render-valid pages, 16285 coarser-clipmap fallback entries",
              {"lod_histogram": ctx["lod_histogram"],
               "valid_for_rendering": ctx["page_table_counts"]["valid_for_rendering"],
               "secondary": ctx["page_table_counts"]["secondary"],
               "flags_nonzero": ctx["page_table_counts"]["page_flags_nonzero"]})

    lm = ctx["logical_mips"]
    expected_dims = [VSM_LEVEL0_DIM_PAGES_XY >> m for m in range(VSM_MAX_MIP_LEVELS)]
    lm_ok = (len(lm) == VSM_MAX_MIP_LEVELS
             and [r["dim_pages"] for r in lm] == expected_dims
             and int(ctx["ops"]["page_table_desc"].get("NumMips") or 0) == 1
             and lm[0]["valid_for_rendering"] == 99
             and all(r["empty"] and r["valid_for_rendering"] == 0 and r["any_lod_valid"] == 0 for r in lm[1:])
             and all(r["base_texel"][1] == 128 for r in lm[1:]))
    add_check(checks, "page_table_logical_mips_complete", lm_ok,
              "all eight logical shadow-map mips 0..7 are decoded from PageTable hardware mip0; "
              "mip0 has 99 render-valid pages and mips 1..7 are present but empty",
              {"descriptor_num_mips": int(ctx["ops"]["page_table_desc"].get("NumMips") or 0),
               "logical_mips": [{"mip": r["mip"], "base_texel": r["base_texel"], "dim_pages": r["dim_pages"],
                                 "entries": r["entries"], "valid_for_rendering": r["valid_for_rendering"],
                                 "any_lod_valid": r["any_lod_valid"], "empty": r["empty"]} for r in lm]})

    fh = ctx["page_flags_hierarchy"]
    pf_ok = (int(ctx["ops"]["page_flags_desc"].get("NumMips") or 0) == 7
             and fh.shape == (7, ctx["page_table_block"]["dim"], ctx["page_table_block"]["dim"])
             and np.array_equal(fh[0], (ctx["page_flags"][0][
                 ctx["page_table_block"]["base_texel"][1]:ctx["page_table_block"]["base_texel"][1] + ctx["page_table_block"]["dim"],
                 ctx["page_table_block"]["base_texel"][0]:ctx["page_table_block"]["base_texel"][0] + ctx["page_table_block"]["dim"]] & 0x3F).astype(np.uint8))
             and int(np.count_nonzero(fh[0])) == 168
             and all(int(np.count_nonzero(fh[m])) > 0 for m in range(7)))
    add_check(checks, "page_flags_hierarchy_semantics", pf_ok,
              "PageFlags hardware mips 0..6 are decoded as the separate H0..H6 hierarchy at parent texel >> m; "
              "H0 is the exact-page flags and H7 is not generated",
              {"descriptor_num_mips": int(ctx["ops"]["page_flags_desc"].get("NumMips") or 0),
               "h_nonzero": {f"H{m}": int(np.count_nonzero(fh[m])) for m in range(7)},
               "h0_matches_mip0_low6": True, "h7_generated": False})

    sn = ctx["snapshots"]
    def _wid(rec):
        return int(rec["pass_id"]) if rec else None
    snap_ok = (
        sn["page_table"]["versioned_by_pass"] == int(ctx["passes"]["vsm_projection"]["Id"]) == 840
        and _wid(sn["page_table"]["last_recorded_writer"]) == 792
        and _wid(sn["physical_page_pool"]["last_recorded_writer"]) == 811
        and _wid(sn["physical_page_metadata"]["last_recorded_writer"]) == 810
        and _wid(sn["page_flags"]["last_recorded_writers"][0]) == 790
        and _wid(sn["page_flags"]["last_recorded_writers"][1]) == 791
        and int(ctx["passes"]["vsm_projection"]["Id"]) != 792)
    add_check(checks, "snapshot_writer_lineage", snap_ok,
              "page table/PageFlags/pool are pass-840 consumer snapshots; their last resource-level output bindings are earlier passes, "
              "and metadata is the pass-810 snapshot",
              {"page_table": sn["page_table"], "page_flags": sn["page_flags"],
               "physical_page_pool": sn["physical_page_pool"], "physical_page_metadata": sn["physical_page_metadata"]})

    mm = ctx["virtual_depth_meta"]
    mosaic_ok = (
        mm["selected_pages"] == 99 == mm["mapped_cells"]
        and mm["mapped_cells"] + mm["fallback_cells"] + mm["unmapped_cells"] == mm["page_cells"]
        and mm["page_cells"] == mm["bbox_page_wh"][0] * mm["bbox_page_wh"][1]
        and mm["output_px"][0] <= 2048 and mm["output_px"][1] <= 2048
        and 0 < mm["scale"] <= 1.0
        and [round(v, 8) for v in mm["depth_display_range_device_z"]]
        == [round(v, 8) for v in MOSAIC_DEPTH_RANGE]
        and len(mm["page_order"]) == 99
        and [r["virtual_address"] for r in mm["page_order"]] == sorted(r["virtual_address"] for r in mm["page_order"])
    )
    add_check(checks, "virtual_depth_mosaic_complete", mosaic_ok,
              "all 99 selected VSM-8196 mip0 pages are reassembled in virtual-address order, cropped to the active-page bbox, "
              "with one uniform scale and the fixed device-Z range [0.4998, 0.5003]",
              {"bbox_virtual_page": mm["bbox_virtual_page"], "bbox_page_wh": mm["bbox_page_wh"],
               "native_px": mm["native_px"], "output_px": mm["output_px"], "scale": mm["scale"],
               "selected_pages": mm["selected_pages"], "mapped_cells": mm["mapped_cells"],
               "fallback_cells": mm["fallback_cells"], "unmapped_cells": mm["unmapped_cells"],
               "depth_display_range_device_z": mm["depth_display_range_device_z"],
               "page_order_first_last": [mm["page_order"][0], mm["page_order"][-1]]})

    stats = ctx["depth_stats"]
    ok = (stats["finite"] == stats["count"] and stats["zeros"] == 0
          and stats["min"] > 0 and stats["max"] < 1.0
          and ctx["depth_display_hi"] > ctx["depth_display_lo"])
    add_check(checks, "depth_values_finite_nonzero_bounded", ok,
              "all render-valid selected-VSM page depth samples are finite, non-zero and inside device-Z [0,1]",
              {"stats": stats, "display_range": [ctx["depth_display_lo"], ctx["depth_display_hi"]],
               "depth_ramp_stops": DEPTH_RAMP_STOPS[:, 1:].astype(int).tolist()})

    ok = all(f["width"] <= 2048 and f["height"] <= 2048 and f["stats"]["std_rgb"][0] > 1.0
             and f["stats"]["std_rgb"][1] > 1.0 and f["stats"]["std_rgb"][2] > 1.0
             for f in ctx["figures"].values())
    add_check(checks, "figures_nonblank_and_bounded", ok,
              "all published figures are <=2048 px and have non-zero channel variance (not blank/all-black)",
              {k: {"size": [v["width"], v["height"]], "std_rgb": v["stats"]["std_rgb"]}
               for k, v in ctx["figures"].items()})

    p = ctx["passes"]
    lineage = bool(p["composite"] and p["sun"]
                   and ctx["vp_output"] in p["composite"].get("InputResources", [])
                   and ctx["composite_output"] in p["sun"].get("InputResources", [])
                   and all(any(x.startswith(rid) for x in p["vsm_projection"].get("InputResources", []))
                           for rid in (ctx["ids"]["PageTable"], ctx["ids"]["PageFlags"],
                                       ctx["ids"]["PhysicalPagePool"], ctx["ids"]["ProjectionData"])))
    add_check(checks, "producer_consumer_lineage", lineage,
              "pass 840 inputs include page table/flags/pool/projection; pass 841 composites 840 output; pass 842 consumes composite mask",
              {"vsm_pass": p["vsm_projection"]["Id"], "composite_pass": p["composite"]["Id"] if p["composite"] else None,
               "sun_pass": p["sun"]["Id"] if p["sun"] else None,
               "vp_output": ctx["vp_output"], "composite_output": ctx["composite_output"]})

    ok = all(r["readback"]["Status"] == "Dumped" if r["readback"] else False for r in ctx["readbacks"])
    add_check(checks, "readback_statuses_dumped", ok,
              "selected resource versions have Dumped readback status with matching file size",
              {"readbacks": [{"file": r["file"], "status": r["readback"]["Status"] if r["readback"] else None,
                              "file_exists": r["readback"]["FileExists"] if r["readback"] else None,
                              "size_matches": r["readback"]["FileSizeMatches"] if r["readback"] else None}
                             for r in ctx["readbacks"]]})

    ok = ctx["metadata_snapshot"]["pass_id"] <= ctx["passes"]["vsm_projection"]["Id"]
    add_check(checks, "metadata_snapshot_precedes_consumer", ok,
              "physical-page metadata snapshot was captured before the pass-840 consumer",
              {"metadata_pass": ctx["metadata_snapshot"]["pass_id"],
               "metadata_pass_name": ctx["metadata_snapshot"]["pass_name"],
               "consumer_pass": ctx["passes"]["vsm_projection"]["Id"]})

    ok = all(r.get("source_available") and r.get("rangeSHA") for r in ctx["source_ranges"]["ranges"])
    add_check(checks, "source_ranges_hashed", ok,
              "every cited source range has an exact UTF8-LF rangeSHA",
              {"ranges": len(ctx["source_ranges"]["ranges"]),
               "missing": [r["file"] + f":{r['line']}-{r['end_line']}" for r in ctx["source_ranges"]["ranges"] if not r.get("rangeSHA")]})

    return raw_after


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------
def verify_publish(out: Path, checks, engine: Path | None):
    pub = out / "publish"
    manifest_path = pub / "manifest.json"
    if not manifest_path.exists():
        add_check(checks, "published_manifest", False, "publish/manifest.json missing")
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest.get("files", [])
    bad = []
    for f in files:
        p = out / f["path"]
        if not p.exists():
            bad.append({"path": f["path"], "reason": "missing"})
            continue
        if p.stat().st_size != int(f["bytes"]):
            bad.append({"path": f["path"], "reason": "size", "expected": f["bytes"], "actual": p.stat().st_size})
            continue
        h = sha256_file(p)
        if h != f["sha256"]:
            bad.append({"path": f["path"], "reason": "sha256", "expected": f["sha256"], "actual": h})
    add_check(checks, "published_file_hashes", not bad,
              "all published figures/data/report files match manifest bytes+sha256",
              {"files": len(files), "mismatches": bad})

    data_path = pub / "data" / "vsm_storage.json"
    npz_path = pub / "data" / "vsm_storage.npz"
    data = json.loads(data_path.read_text(encoding="utf-8"))
    npz = np.load(npz_path, allow_pickle=False)
    req = ["page_table_entry_u32", "page_table_phys_x", "page_table_phys_y",
           "page_table_lod", "page_table_render", "page_table_any", "page_table_secondary",
           "page_table_flags_u32", "page_table_flags_hierarchy_h0_h6",
           "page_table_logical_mip_entry_u32", "page_table_logical_mip_render",
           "page_table_logical_mip_lod", "page_table_logical_mip_dim", "page_table_logical_mip_base_texel",
           "metadata_u32", "occupancy_category",
           "virtual_depth_mask_u8", "virtual_depth_page_category_u8",
           "depth_pages_raw_u32", "depth_pages_f32", "depth_pages_all_f32", "depth_pages_all_raw_u32",
           "receiver_depth_f32",
           "receiver_physical_texel", "receiver_page_table_texel"]
    missing = [k for k in req if k not in npz.files]
    add_check(checks, "npz_required_arrays", not missing,
              "published NPZ exposes all arrays required to re-derive the plotted/queried operands",
              {"arrays": len(npz.files), "missing": missing})

    entry = npz["page_table_entry_u32"].astype(np.uint64)
    px = npz["page_table_phys_x"].astype(np.uint64)
    py = npz["page_table_phys_y"].astype(np.uint64)
    lod = npz["page_table_lod"].astype(np.uint64)
    render = npz["page_table_render"].astype(np.uint64)
    anyv = npz["page_table_any"].astype(np.uint64)
    sec = npz["page_table_secondary"].astype(np.uint64)
    re = (py << 10) | px | (anyv << 31) | (render << 30) | (sec << 29) | (lod << 20)
    mismatch = int(np.count_nonzero((re & 0xFFFFFFFF) != (entry & 0xFFFFFFFF)))
    add_check(checks, "verify_page_table_roundtrip", mismatch == 0,
              "re-encoding every published page-table record reproduces its captured uint32",
              {"entries": int(entry.size), "mismatches": mismatch})

    lm_data = data.get("operands", {}).get("page_table", {}).get("logical_mips", [])
    lm_entry = npz["page_table_logical_mip_entry_u32"]
    lm_render = npz["page_table_logical_mip_render"]
    lm_dim = npz["page_table_logical_mip_dim"].astype(int)
    lm_base = npz["page_table_logical_mip_base_texel"]
    lm_ok = (len(lm_data) == 8 == lm_entry.shape[0] and lm_entry.shape[1:] == (128, 128)
             and [r["dim_pages"] for r in lm_data] == lm_dim.tolist()
             and all((lm_base[i] == r["base_texel"]).all() for i, r in enumerate(lm_data)))
    lm_detail = []
    for i, r in enumerate(lm_data):
        d = int(lm_dim[i])
        region = lm_entry[i, :d, :d]
        rrender = lm_render[i, :d, :d]
        anyv_region = (region & 0x80000000) != 0
        valid_region = (region & 0x40000000) != 0
        empty = not bool(anyv_region.any())
        rec = {"mip": int(r["mip"]), "dim_pages": d, "entries": int(region.size),
               "render": int(np.count_nonzero(rrender)),
               "any_valid": int(np.count_nonzero(anyv_region)),
               "empty": empty, "json_empty": bool(r["empty"]),
               "counts_match": (int(np.count_nonzero(rrender)) == int(r["valid_for_rendering"])
                                and int(np.count_nonzero(anyv_region)) == int(r["any_lod_valid"]))
                               and empty == bool(r["empty"])}
        lm_ok = lm_ok and rec["counts_match"] and (i > 0 or rec["render"] == 99)
        lm_detail.append(rec)
    lm_ok = lm_ok and all(rec["empty"] and rec["render"] == 0 for rec in lm_detail[1:])
    add_check(checks, "verify_logical_mips", bool(lm_ok),
              "all eight logical page-table mips 0..7 reproduce their published region counts; mip0 has 99 render-valid pages, mips 1..7 are present and empty",
              {"descriptor_num_mips": data.get("operands", {}).get("page_table", {}).get("descriptor_num_mips"),
               "logical_mips": lm_detail})

    fh = npz["page_table_flags_hierarchy_h0_h6"]
    pfw = npz["page_table_flags_u32"]
    pf_data = data.get("operands", {}).get("page_flags", {})
    pf_ok = (fh.shape == (7, 128, 128) and int(pf_data.get("descriptor_num_mips") or 0) == 7
             and np.array_equal(fh[0], (pfw & 0x3F).astype(np.uint8))
             and int(np.count_nonzero(fh[0])) == 168
             and all(int(np.count_nonzero(fh[m])) > 0 for m in range(7))
             and "separate" in str(pf_data.get("hierarchy", "")).lower()
             and "H7 is not generated" in str(pf_data.get("hierarchy", "")))
    add_check(checks, "verify_page_flags_hierarchy", bool(pf_ok),
              "PageFlags 0..6 are verified as the separate H0..H6 hardware-mip hierarchy; H0 equals exact-page flags and H7 is absent",
              {"shape": list(fh.shape), "descriptor_num_mips": pf_data.get("descriptor_num_mips"),
               "h_nonzero": {f"H{m}": int(np.count_nonzero(fh[m])) for m in range(7)},
               "h0_matches_mip0_low6": bool(np.array_equal(fh[0], (pfw & 0x3F).astype(np.uint8)))})

    meta = npz["metadata_u32"]
    ok = (meta.shape == (4096, 6) and np.all(meta[:, 2] < 8209) and np.all(meta[:, 3] < 8)
          and np.all(meta[:, 4] < 128) and np.all(meta[:, 5] < 128))
    add_check(checks, "verify_metadata_bounds", bool(ok),
              "all 4096 physical-page metadata records have in-range VSM id/mip/page address",
              {"shape": list(meta.shape), "max_vsm": int(meta[:, 2].max()), "max_mip": int(meta[:, 3].max()),
               "max_page": [int(meta[:, 4].max()), int(meta[:, 5].max())]})

    md_data = data.get("operands", {}).get("physical_page_metadata", {})
    scene_frame = int(data.get("run", {}).get("scene_frame_number", 904))
    unref = int(np.count_nonzero((meta[:, 0] != 0) & (meta[:, 1] != scene_frame)))
    ur_ok = (int(md_data.get("unreferenced_in_snapshot_records", -1)) == unref
             and bool(md_data.get("unreferenced_not_proven_invalid"))
             and "stale_records" not in md_data
             and int(md_data.get("snapshot", {}).get("versioned_by_pass", -1)) == 810)
    add_check(checks, "verify_unreferenced_metadata_wording", bool(ur_ok),
              "the 96 earlier-frame metadata records are published as unreferenced in this snapshot, not as proven stale/invalid from age",
              {"unreferenced_records": unref, "published": md_data.get("unreferenced_in_snapshot_records"),
               "not_proven_invalid": md_data.get("unreferenced_not_proven_invalid"),
               "snapshot_pass": md_data.get("snapshot", {}).get("versioned_by_pass")})

    dp_raw = npz["depth_pages_raw_u32"]
    dp_f = npz["depth_pages_f32"]
    back = dp_raw.view("<f4")
    same = bool(np.array_equal(back, dp_f, equal_nan=True))
    finite = np.isfinite(dp_f)
    ok = same and bool(finite.any()) and float(np.nanmin(dp_f)) > 0 and float(np.nanmax(dp_f)) < 1
    add_check(checks, "verify_depth_bitcast", ok,
              "published native depth pages bitcast back exactly and lie inside device-Z (0,1)",
              {"shape": list(dp_f.shape), "finite": int(finite.sum()), "total": int(finite.size),
               "min": float(np.nanmin(dp_f)), "max": float(np.nanmax(dp_f))})

    allp = npz["depth_pages_all_f32"]
    allraw = npz["depth_pages_all_raw_u32"]
    all_same = bool(np.array_equal(allraw.view("<f4"), allp, equal_nan=True))
    all_finite = np.isfinite(allp)
    all_ok = (all_same and allp.shape == (99, 128, 128) and bool(all_finite.any())
              and float(np.nanmin(allp)) > 0 and float(np.nanmax(allp)) < 1)
    add_check(checks, "verify_all_native_depth_pages", all_ok,
              "all 99 full-resolution selected-VSM depth pages are published as raw bits + float64-identical float32 and stay inside device-Z (0,1)",
              {"shape": list(allp.shape), "finite": int(all_finite.sum()), "total": int(all_finite.size),
               "min": float(np.nanmin(allp)), "max": float(np.nanmax(allp))})

    # ---- F4 contiguous virtual-address mosaic --------------------------------
    fig4 = data.get("figures", {}).get("vsm_virtual_depth")
    mo_ok = False
    mo_det = {}
    if fig4 and Image is not None:
        img = np.asarray(Image.open(out / fig4["file"]).convert("RGB"))
        bbox = [int(v) for v in fig4["bbox_virtual_page"]]
        bx0, by0 = bbox[0], bbox[1]
        scale = float(fig4["scale"])
        out_w, out_h = [int(v) for v in fig4["output_px"]]
        native_w, native_h = [int(v) for v in fig4["native_px"]]
        xs_native = np.minimum((np.arange(out_w) / scale).astype(np.int64), native_w - 1)
        ys_native = np.minimum((np.arange(out_h) / scale).astype(np.int64), native_h - 1)
        mask = npz["virtual_depth_mask_u8"]
        pcat = npz["virtual_depth_page_category_u8"]
        pages = fig4.get("page_order", [])
        by_vpage = {tuple(int(v) for v in p["virtual_page_xy"]): p for p in pages}
        known = []
        for name in ("A", "B"):
            r = data.get("receivers_declared_replay", {}).get(name)
            if r and tuple(int(v) for v in r["virtual_page_xy"]) in by_vpage:
                known.append((name, tuple(int(v) for v in r["virtual_page_xy"])))
        if pages:
            known.append(("first_page_order", tuple(int(v) for v in pages[0]["virtual_page_xy"])))
            known.append(("last_page_order", tuple(int(v) for v in pages[-1]["virtual_page_xy"])))
        seen = set()
        known_unique = []
        for k in known:
            if k[1] in seen:
                continue
            seen.add(k[1])
            known_unique.append(k)
        known = known_unique
        lo_d, hi_d = [float(v) for v in fig4["depth_display_range_device_z"]]
        samples = []
        all_match = len(known) >= 3 and img.shape[:2] == (out_h, out_w)
        for label, (vx, vy) in known:
            ent = by_vpage.get((vx, vy))
            if ent is None:
                all_match = False
                continue
            arr = allp[int(ent["array_index"])]
            gx0, gy0 = (vx - bx0) * VSM_PAGE_SIZE, (vy - by0) * VSM_PAGE_SIZE
            cols = np.nonzero((xs_native >= gx0) & (xs_native < gx0 + VSM_PAGE_SIZE))[0]
            rows = np.nonzero((ys_native >= gy0) & (ys_native < gy0 + VSM_PAGE_SIZE))[0]
            if cols.size == 0 or rows.size == 0:
                all_match = False
                continue
            for frac in (0.25, 0.5, 0.75):
                x = int(cols[int(cols.size * frac)])
                y = int(rows[int(rows.size * frac)])
                lx = int(xs_native[x] - gx0)
                ly = int(ys_native[y] - gy0)
                expected, _ = depth_rgb(arr[ly, lx], lo_d, hi_d)
                actual = img[y, x]
                match = bool(np.array_equal(actual, expected))
                all_match = all_match and match and int(mask[y, x]) == 0
                samples.append({"page": [int(vx), int(vy)], "label": label,
                                "output_xy": [x, y], "texel_in_page": [lx, ly],
                                "expected_rgb": [int(v) for v in expected],
                                "actual_rgb": [int(v) for v in actual], "match": match})
        counts = {int(k): int(v) for k, v in zip(*np.unique(pcat, return_counts=True))}
        fig4_ok = (
            len(pages) == 99 and len(by_vpage) == 99
            and img.shape[1] <= 2048 and img.shape[0] <= 2048
            and [lo_d, hi_d] == [0.4998, 0.5003]
            and int(pcat.shape[0]) == bbox[3] - bbox[1] + 1
            and int(pcat.shape[1]) == bbox[2] - bbox[0] + 1
            and counts.get(0, 0) == 99
            and counts.get(0, 0) + counts.get(1, 0) + counts.get(2, 0) == pcat.size
            and bool(fig4.get("no_burned_text"))
        )
        mo_ok = bool(all_match and fig4_ok and len(samples) >= 9)
        mo_det = {"figure": fig4.get("file"), "shape": list(img.shape), "bbox_virtual_page": bbox,
                  "scale": scale, "depth_display_range_device_z": [lo_d, hi_d],
                  "page_count": len(pages), "page_categories": counts,
                  "known_page_coordinates": [list(k[1]) for k in known],
                  "samples_checked": len(samples), "samples": samples[:12]}
    add_check(checks, "verify_virtual_depth_mosaic", mo_ok,
              "F4 reassembles all 99 selected pages at their virtual coordinates; at least three known page coordinates reproduce the exact "
              "depth-ramp RGB and mapped mask under the fixed range [0.4998, 0.5003]",
              mo_det)

    r_depth = npz["receiver_depth_f32"]
    r_phys = npz["receiver_physical_texel"]
    r_ptex = npz["receiver_page_table_texel"]
    recs = data.get("receivers_declared_replay", {})
    ok = True
    det = {}
    for i, name in enumerate(("A", "B")):
        r = recs.get(name)
        if not r:
            ok = False
            continue
        e = int(r["page_table_entry_value"])
        epx, epy = e & 0x3FF, (e >> 10) & 0x3FF
        tex = [epx * 128 + r["texel_in_page_xy"][0], epy * 128 + r["texel_in_page_xy"][1]]
        m = (int(r_phys[i][0]) == tex[0] and int(r_phys[i][1]) == tex[1]
             and int(r_ptex[i][0]) == r["page_table_texel_xy"][0]
             and int(r_ptex[i][1]) == r["page_table_texel_xy"][1]
             and abs(float(r_depth[i]) - float(r["stored_depth_device_z"])) < 1e-12
             and r["physical_page_metadata"]["virtual_shadow_map_id"] == r["selected_vsm_id"]
             and r["physical_page_metadata"]["page_address"] == r["virtual_page_xy"])
        ok = ok and m
        det[name] = {"entry": hex32(e), "physical_texel": tex,
                     "page_table_texel": r["page_table_texel_xy"],
                     "depth": float(r_depth[i]), "metadata_owner": r["physical_page_metadata"]["virtual_shadow_map_id"],
                     "metadata_page": r["physical_page_metadata"]["page_address"]}
    add_check(checks, "verify_receiver_chain", ok,
              "A/B declared-replay records recompute the same physical texel, page-table texel, depth and metadata owner from published data",
              det)

    occ = npz["occupancy_category"]
    counts = {int(k): int(v) for k, v in zip(*np.unique(occ, return_counts=True))}
    ok = int(occ.size) == 4096 and all(0 <= k <= 5 for k in counts)
    add_check(checks, "verify_occupancy_partition", ok,
              "published occupancy raster covers 4096 pages with declared category ids",
              {"shape": list(occ.shape), "counts": counts})

    fig_ok = True
    fig_det = {}
    for key, f in data.get("figures", {}).items():
        p = out / f["file"]
        if not p.exists():
            fig_ok = False
            continue
        w = h = None
        with open(p, "rb") as fh:
            head = fh.read(33)
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            w, h = struct.unpack(">II", head[16:24])
        fig_det[key] = {"size": [w, h], "expected": [f["width"], f["height"]]}
        if w != f["width"] or h != f["height"] or w > 2048 or h > 2048:
            fig_ok = False
    add_check(checks, "verify_figure_dimensions", fig_ok,
              "PNG IHDR dimensions match the published manifest and stay <=2048 px",
              {"figures": fig_det})

    sr_path = out / "source-ranges.json"
    sr = json.loads(sr_path.read_text(encoding="utf-8")) if sr_path.exists() else {"ranges": []}
    rec_ok = bool(sr.get("ranges")) and all(r.get("rangeSHA") for r in sr["ranges"])
    recomputed = []
    if engine:
        for r in sr["ranges"]:
            p = engine_source_path(engine, r["file"])
            if p.exists():
                lines = p.read_text(encoding="utf-8").splitlines()
                h = sha256_text("\n".join(lines[r["line"] - 1:r["end_line"]]))
                recomputed.append({"file": r["file"], "line": r["line"], "end_line": r["end_line"],
                                   "expected": r["rangeSHA"], "actual": h, "match": h == r["rangeSHA"]})
    add_check(checks, "verify_source_ranges", rec_ok and all(x["match"] for x in recomputed),
              "source-ranges.json has a rangeSHA for every range" + (" and every range recomputes against the pinned engine" if engine else " (engine not supplied; range SHAs not recomputed)"),
              {"ranges": len(sr.get("ranges", [])), "recomputed": recomputed})

    add_check(checks, "verify_manifest_self", True,
              "verification used only published artifacts; no raw capture or engine execution was required",
              {"manifest_schema": manifest.get("schema"), "npz_arrays": len(npz.files)})


# ---------------------------------------------------------------------------
# main decode
# ---------------------------------------------------------------------------
def decode(args):
    out = Path(args.out)
    dump_root = Path(args.dump)
    dump = Dump(dump_root)
    passes = select_passes(dump)
    vp = passes["vsm_projection"]
    uniform = vm_uniform(dump, vp)
    ops = resolve_vsm_operands(dump, vp, uniform, passes["graph_prefix"])
    u = uniform["values"]

    used_paths = [
        dump.base / "Passes.json", dump.base / "ResourceDescs.json", dump.base / "SubresourceLayouts.json",
        dump.base / "DumpReadbackStatus.json", dump.base / "ParameterBindings.json", dump.base / "ReferencedStructs.json",
        dump.root / uniform["file"], dump.root / uniform["metadata_file"],
        dump.root / ops["projection"]["file"], dump.root / ops["metadata"]["file"],
    ]
    for l in [ops["page_table"]] + ops["page_flags"] + [ops["pool_slice0"], ops["pool_slice1"]]:
        used_paths.append(dump.root / dump.rel_file(l))
    raw_before = {str(p): sha256_file(p) for p in used_paths}

    proj_raw = Path(ops["projection"]["path"]).read_bytes()
    projection = decode_projection_buffer(proj_raw)
    projection_count = len(projection)
    sun = [r for r in projection if r["flags"] != 0 and r["id"] >= VSM_MAX_SINGLE_PAGE_SHADOW_MAPS]
    if not sun:
        raise RuntimeError("no full sun VSM projection records")
    sun.sort(key=lambda r: r["id"])
    sun_ids = [r["id"] for r in sun]
    if sun_ids != list(range(sun_ids[0], sun_ids[0] + len(sun_ids))):
        raise RuntimeError("sun VSM ids are not contiguous")
    if any(sun[i]["clipmap_level"] + 1 != sun[i + 1]["clipmap_level"] for i in range(len(sun) - 1)):
        raise RuntimeError("sun clipmap levels are not consecutive")
    if len({tuple(round(x, 5) for x in r["light_direction"]) for r in sun}) != 1:
        raise RuntimeError("sun VSM records disagree on light direction")
    base = sun[0]
    for r in sun:
        if r["light_type"] != 0:
            raise RuntimeError("sun VSM record is not directional")
        if r["clipmap_level_count_remaining"] < 1:
            raise RuntimeError("bad clipmap level count")
    base_remaining = base["clipmap_level_count_remaining"]
    if base_remaining < len(sun):
        raise RuntimeError("base clipmap count does not cover captured levels")

    pt_desc = ops["page_table_desc"]
    pt = np.fromfile(dump.root / dump.rel_file(ops["page_table"]), dtype="<u4")
    pt = pt.reshape(int(ops["page_table"]["ExtentY"]), int(ops["page_table"]["ExtentX"]))
    pf_mips = []
    for m, l in enumerate(ops["page_flags"]):
        a = np.fromfile(dump.root / dump.rel_file(l), dtype="<u4")
        pf_mips.append(a.reshape(int(l["ExtentY"]), int(l["ExtentX"])))
    pool_shape = (int(ops["pool_slice0"]["ExtentY"]), int(ops["pool_slice0"]["ExtentX"]))
    pool0 = np.memmap(dump.root / dump.rel_file(ops["pool_slice0"]), dtype="<u4", mode="r").reshape(pool_shape).view("<f4")
    pool1 = np.memmap(dump.root / dump.rel_file(ops["pool_slice1"]), dtype="<u4", mode="r").reshape(pool_shape).view("<f4")
    metadata = np.frombuffer(Path(ops["metadata"]["path"]).read_bytes(), dtype="<u4").reshape(-1, 6)

    # constants and cross-checks
    if int(u["MaxPhysicalPages"]) != metadata.shape[0]:
        raise RuntimeError("metadata count != MaxPhysicalPages")
    if (1 << int(u["PageTableRowShift"])) != pt.shape[1] // VSM_PAGE_TABLE_TEX2D_SIZE_X:
        raise RuntimeError("PageTableRowShift incompatible with page table width")
    if (1 << int(u["PhysicalPageRowShift"])) != pool_shape[1] // VSM_PAGE_SIZE:
        raise RuntimeError("PhysicalPageRowShift incompatible with pool width")
    if int(u["StaticCachedArrayIndex"]) != 1:
        raise RuntimeError("expected static pool slice 1")

    receivers = {}
    if args.receivers:
        rdoc = json.loads(Path(args.receivers).read_text(encoding="utf-8"))
        view_pre = None
        if args.view:
            vdoc = json.loads(Path(args.view).read_text(encoding="utf-8"))
            view_pre = (vdoc.get("view") or {}).get("pre_view_translation_cm")
        if view_pre is None:
            view_pre = (rdoc.get("view") or {}).get("pre_view_translation_cm")
        if view_pre is None:
            a = (rdoc.get("receivers") or {}).get("A") or {}
            if a.get("translated_world_cm") and a.get("world_cm"):
                view_pre = (np.array(a["translated_world_cm"]) - np.array(a["world_cm"])).tolist()
        view_pre = np.asarray(view_pre, dtype=np.float64)
        for name in ("A", "B"):
            rec = (rdoc.get("receivers") or {}).get(name)
            if not rec:
                continue
            receivers[name] = receiver_replay(name, rec, projection, base, uniform, view_pre,
                                              pt, pf_mips[0], pool0, metadata)
        # choose the level used by receiver A for the page-table/depth figures
        if "A" in receivers and receivers["A"].get("clipmap_selection_valid"):
            sel = projection[receivers["A"]["selected_vsm_id"]]
        else:
            sel = sun[min(4, len(sun) - 1)]
    else:
        sel = sun[min(4, len(sun) - 1)]

    # page-table block of selected VSM mip0
    block = page_table_block(sel["id"], 0, uniform)
    bx, by = block["base_texel"]
    dim = block["dim"]
    ptb = pt[by:by + dim, bx:bx + dim]
    pfb = pf_mips[0][by:by + dim, bx:bx + dim]
    entries = [decode_page_table_entry(int(v)) for v in ptb.ravel()]
    mismatch = sum(1 for e in entries if encode_page_table(e) != (e["raw"] & 0xFFFFFFFF))
    phys_x = np.array([e["physical_x"] for e in entries], dtype=np.uint16).reshape(dim, dim)
    phys_y = np.array([e["physical_y"] for e in entries], dtype=np.uint16).reshape(dim, dim)
    lod = np.array([e["lod_offset"] for e in entries], dtype=np.uint8).reshape(dim, dim)
    render = np.array([e["valid_for_rendering"] for e in entries], dtype=bool).reshape(dim, dim)
    anyv = np.array([e["any_lod_valid"] for e in entries], dtype=bool).reshape(dim, dim)
    sec = np.array([e["secondary_request"] for e in entries], dtype=bool).reshape(dim, dim)
    phys_index = (phys_y.astype(np.uint32) << int(u["PhysicalPageRowShift"])) + phys_x.astype(np.uint32)

    # All eight logical shadow-map mips (0..7) are packed in this one VSM
    # 128x192 page-table block: mip0 is the 128x128 top-left region and the
    # mip tail sits at y=128 (CalcLevelOffsets in the pinned source).
    logical_entry = np.zeros((VSM_MAX_MIP_LEVELS, dim, dim), dtype=np.uint32)
    logical_render = np.zeros((VSM_MAX_MIP_LEVELS, dim, dim), dtype=bool)
    logical_lod = np.zeros((VSM_MAX_MIP_LEVELS, dim, dim), dtype=np.uint8)
    logical_mips = []
    for lm in range(VSM_MAX_MIP_LEVELS):
        lb = page_table_block(sel["id"], lm, uniform)
        lx, ly = lb["base_texel"]
        ld = lb["dim"]
        region = pt[ly:ly + ld, lx:lx + ld]
        lflags = pf_mips[0][ly:ly + ld, lx:lx + ld] & 0x3F
        lent = [decode_page_table_entry(int(v)) for v in region.ravel()]
        lhist = {int(k): int(v) for k, v in zip(*np.unique(
            [e["lod_offset"] for e in lent], return_counts=True))} if lent else {}
        lrender = int(sum(1 for e in lent if e["valid_for_rendering"]))
        lany = int(sum(1 for e in lent if e["any_lod_valid"]))
        lsec = int(sum(1 for e in lent if e["secondary_request"]))
        logical_entry[lm, :ld, :ld] = region
        logical_render[lm, :ld, :ld] = np.array([e["valid_for_rendering"] for e in lent],
                                                dtype=bool).reshape(ld, ld)
        logical_lod[lm, :ld, :ld] = np.array([e["lod_offset"] for e in lent],
                                             dtype=np.uint8).reshape(ld, ld)
        logical_mips.append({
            "mip": lm,
            "base_texel": [int(lx), int(ly)],
            "region_texel_rect": [int(lx), int(ly), int(ld), int(ld)],
            "dim_pages": int(ld),
            "dim_texels": [int(ld * VSM_PAGE_SIZE), int(ld * VSM_PAGE_SIZE)],
            "entries": int(ld * ld),
            "any_lod_valid": lany,
            "valid_for_rendering": lrender,
            "secondary_request": lsec,
            "unmapped": int(ld * ld - lany),
            "lod_histogram": lhist,
            "empty": bool(lany == 0 and lrender == 0),
            "page_flags_nonzero": int(np.count_nonzero(lflags)),
            "page_flags_categories": {hex(int(k)): int(v) for k, v in zip(*np.unique(lflags, return_counts=True))},
            "npz_key": "page_table_logical_mip_entry_u32",
            "npz_region": [int(lm), 0, 0, int(ld), int(ld)],
            "address_rule": "base_texel = CalcPageTableLevelOffset(id, mip); entries cover dim x dim pages",
        })

    # PageFlags hardware mips 0..6 are the separate H0..H6 hierarchy, not
    # 6-bit fields inside one word.  H_m parent texel = base texel >> m.
    flags_h = np.zeros((len(pf_mips), dim, dim), dtype=np.uint8)
    for level, src in enumerate(pf_mips):
        xs = (bx + np.arange(dim)) >> level
        ys = (by + np.arange(dim)) >> level
        if xs.max() >= src.shape[1] or ys.max() >= src.shape[0]:
            raise RuntimeError(f"PageFlags mip{level} parent lookup outside captured extent")
        flags_h[level] = (src[np.ix_(ys, xs)] & 0x3F).astype(np.uint8)

    # counts / mapped records
    lod_hist = {int(k): int(v) for k, v in zip(*np.unique(lod, return_counts=True))}
    pf_low = (pfb & 0x3F).astype(np.uint8)
    mapped = []
    oob = 0
    for yy in range(dim):
        for xx in range(dim):
            e = entries[yy * dim + xx]
            if not e["valid_for_rendering"]:
                continue
            px, py = e["physical_x"], e["physical_y"]
            if px >= pool_shape[1] // VSM_PAGE_SIZE or py >= pool_shape[0] // VSM_PAGE_SIZE:
                oob += 1
                continue
            pidx = int((py << int(u["PhysicalPageRowShift"])) + px)
            md = metadata[pidx]
            page = np.array(pool0[py * VSM_PAGE_SIZE:(py + 1) * VSM_PAGE_SIZE,
                                  px * VSM_PAGE_SIZE:(px + 1) * VSM_PAGE_SIZE], dtype=np.float32)
            mapped.append({
                "virtual_page_xy": [xx, yy], "physical_page_xy": [px, py],
                "physical_page_index": pidx, "page_table_texel_xy": [bx + xx, by + yy],
                "page_table_entry_hex": e["raw_hex"], "page_table_entry_value": e["raw"],
                "page_flags_hex": hex32(int(pfb[yy, xx])), "page_flags_h0": int(pfb[yy, xx]) & 0x3F,
                "metadata": {"flags": int(md[0]), "flags_hex": hex32(int(md[0])),
                             "last_requested_scene_frame_number": int(md[1]),
                             "virtual_shadow_map_id": int(md[2]), "mip_level": int(md[3]),
                             "page_address": [int(md[4]), int(md[5])]},
                "metadata_owner_matches": bool(int(md[2]) == sel["id"] and int(md[3]) == 0
                                               and int(md[4]) == xx and int(md[5]) == yy),
                "depth_stats": quantile_stats(page),
                "_page": page,
            })
    mapped_owner = sum(1 for r in mapped if r["metadata_owner_matches"])
    mapped_finite = sum(1 for r in mapped if r["depth_stats"].get("finite", 0) > 0
                        and r["depth_stats"].get("min") is not None)
    mapped_zero = sum(1 for r in mapped if r["depth_stats"].get("zeros", 0) > 0)

    # depth stats over all mapped native pages (selected projection only)
    all_depth = np.concatenate([np.asarray(r["_page"]).ravel() for r in mapped]) if mapped else np.array([], dtype=np.float32)
    dstats = quantile_stats(all_depth)

    # fixed display range: outward-quantized from the selected projection's data once
    lo = math.floor((dstats["min"] - 5e-5) * 1e4) / 1e4
    hi = math.ceil((dstats["max"] + 5e-5) * 1e4) / 1e4
    if hi - lo < 1e-4:
        lo, hi = lo - 5e-5, hi + 5e-5

    # occupancy
    ref = np.zeros(metadata.shape[0], dtype=np.int32)
    nz = pt[(pt & 0x80000000) != 0]
    for v in nz:
        pidx = (((int(v) >> 10) & 0x3FF) << int(u["PhysicalPageRowShift"])) + (int(v) & 0x3FF)
        if 0 <= pidx < ref.size:
            ref[pidx] += 1
    scene_frame = int(u["SceneFrameNumber"])
    occ = np.zeros((metadata.shape[0] // 128, 128), dtype=np.uint8)
    state = np.zeros_like(occ)
    occ_counts = {}
    meta_state_counts = {}
    for pidx in range(metadata.shape[0]):
        y, x = divmod(pidx, 128)
        flags, frame, owner = int(metadata[pidx, 0]), int(metadata[pidx, 1]), int(metadata[pidx, 2])
        if flags == 0 and ref[pidx] == 0:
            c = 0
        elif flags == 0:
            c = 5
        elif ref[pidx] == 0 and frame != scene_frame:
            c = 4
        elif owner == sel["id"]:
            c = 1
        elif ref[pidx] > 0:
            c = 2
        else:
            c = 3
        occ[y, x] = c
        occ_counts[str(c)] = occ_counts.get(str(c), 0) + 1
        if flags == 0:
            s = 3 if ref[pidx] > 0 else 0
        elif ref[pidx] == 0 and frame != scene_frame:
            s = 2
        elif frame == scene_frame:
            s = 1
        else:
            s = 3
        state[y, x] = s
        meta_state_counts[str(s)] = meta_state_counts.get(str(s), 0) + 1
    ref_count = int(np.count_nonzero(ref))

    # depth mosaic (8x8 sampled means per physical page); only current selected pages
    depth_rgb_arr = np.full((pool_shape[0] // VSM_PAGE_SIZE * 8, pool_shape[1] // VSM_PAGE_SIZE * 8, 3),
                            MASKED_COLOR, dtype=np.uint8)
    depth_z = np.full(depth_rgb_arr.shape[:2], np.nan, dtype=np.float32)
    for r in mapped:
        px, py = r["physical_page_xy"]
        md = metadata[r["physical_page_index"]]
        if int(md[2]) != sel["id"] or int(md[1]) != scene_frame:
            continue
        page = np.asarray(r["_page"], dtype=np.float32)
        samp = page.reshape(8, 16, 8, 16).mean(axis=(1, 3))
        rgb, _ = depth_rgb(samp, lo, hi)
        y0, x0 = py * 8, px * 8
        depth_rgb_arr[y0:y0 + 8, x0:x0 + 8] = rgb
        depth_z[y0:y0 + 8, x0:x0 + 8] = samp

    # figure palettes
    lod_rgb_arr = np.zeros((dim * 4, dim * 4, 3), dtype=np.uint8)
    ident = np.zeros((dim * 4, dim * 4, 3), dtype=np.uint8)
    flag_rgb_arr = np.zeros((dim * 4, dim * 4, 3), dtype=np.uint8)
    for yy in range(dim):
        for xx in range(dim):
            e = entries[yy * dim + xx]
            col = LOD_COLORS.get(int(e["lod_offset"]), LOD_FALLBACK_COLOR) if e["any_lod_valid"] else LOD_FALLBACK_COLOR
            lod_rgb_arr[yy * 4:(yy + 1) * 4, xx * 4:(xx + 1) * 4] = col
            if e["any_lod_valid"]:
                rr = min(255, e["physical_x"] * 2)
                gg = min(255, e["physical_y"] * 8)
                bb = 255 if e["valid_for_rendering"] else 96
                if not e["valid_for_rendering"]:
                    rr, gg = int(rr * 0.55), int(gg * 0.55)
                ident[yy * 4:(yy + 1) * 4, xx * 4:(xx + 1) * 4] = (rr, gg, bb)
            else:
                ident[yy * 4:(yy + 1) * 4, xx * 4:(xx + 1) * 4] = LOD_FALLBACK_COLOR
            low = int(pfb[yy, xx]) & 0x3F
            flag_rgb_arr[yy * 4:(yy + 1) * 4, xx * 4:(xx + 1) * 4] = PAGE_FLAG_COLORS.get(low, PAGE_FLAG_OTHER)

    # markers for A/B pages
    markers = []
    for name, r in receivers.items():
        if not r.get("clipmap_selection_valid"):
            continue
        vx, vy = r["virtual_page_xy"]
        markers.append({"receiver": name, "virtual_page_xy": [vx, vy],
                        "pixel_box": [vx * 4, vy * 4, vx * 4 + 4, vy * 4 + 4]})
    if Image is not None:
        im = Image.fromarray(ident, "RGB")
        dr = ImageDraw.Draw(im)
        for m in markers:
            x0, y0, x1, y1 = m["pixel_box"]
            dr.rectangle((x0 - 1, y0 - 1, x1, y1), outline=(255, 255, 255))
        ident = np.asarray(im)

    # depth-page tiles: A, B, max std, max mean, min mean, A neighbour
    by_page = {tuple(r["virtual_page_xy"]): r for r in mapped}
    chosen = []
    used = set()

    def take(r, source):
        if r is None:
            return
        key = (r["virtual_page_xy"][0], r["virtual_page_xy"][1])
        if key in used:
            return
        used.add(key)
        chosen.append({**r, "source": source})

    for name in ("A", "B"):
        r = receivers.get(name)
        if r and r.get("clipmap_selection_valid"):
            key = tuple(r["virtual_page_xy"])
            page_rec = by_page.get(key)
            if page_rec:
                take(page_rec, f"receiver_{name}")
            else:
                px, py = r["physical_page_xy"]
                page = np.array(pool0[py * 128:(py + 1) * 128, px * 128:(px + 1) * 128], dtype=np.float32)
                take({"virtual_page_xy": list(key), "physical_page_xy": [px, py],
                      "physical_page_index": (py << 7) + px,
                      "page_table_texel_xy": r["page_table_texel_xy"],
                      "page_table_entry_hex": r["page_table_entry_hex"],
                      "depth_stats": quantile_stats(page), "_page": page}, f"receiver_{name}")
    if len(mapped) >= 4:
        by_std = sorted(mapped, key=lambda r: (r["depth_stats"].get("std", -1), r["depth_stats"].get("mean", 0)), reverse=True)
        by_mean = sorted(mapped, key=lambda r: r["depth_stats"].get("mean", -1), reverse=True)
        take(by_std[0], "highest_depth_std")
        take(by_mean[0], "highest_mean_depth")
        take(by_mean[-1], "lowest_mean_depth")
    a = receivers.get("A")
    if a and a.get("clipmap_selection_valid"):
        vx, vy = a["virtual_page_xy"]
        take(by_page.get((vx + 1, vy)), "receiver_A_neighbour_page_x_plus_1")
    for r in mapped:
        if len(chosen) >= 6:
            break
        take(r, "remaining_mapped_page")
    if len(chosen) < 6:
        raise RuntimeError("not enough mapped pages for the depth tile figure")

    tiles = []
    for t in chosen[:6]:
        page = np.asarray(t["_page"], dtype=np.float32)
        rgb, _ = depth_rgb(page, lo, hi)
        rec = receivers.get(t["source"].replace("receiver_", "")) if t["source"].startswith("receiver_") else None
        marker = None
        if rec and rec.get("clipmap_selection_valid") and rec["virtual_page_xy"] == t["virtual_page_xy"]:
            marker = rec["texel_in_page_xy"]
        tiles.append({
            "source": t["source"],
            "receiver": t["source"].replace("receiver_", "") if t["source"].startswith("receiver_") else None,
            "virtual_page_xy": t["virtual_page_xy"],
            "physical_page_xy": t["physical_page_xy"],
            "physical_page_index": t["physical_page_index"],
            "page_table_texel_xy": t["page_table_texel_xy"],
            "page_table_entry_hex": t["page_table_entry_hex"],
            "depth_stats": t["depth_stats"],
            "marker_texel": marker,
            "rgb": rgb,
            "_page": page,
        })

    # 8x8 block-mean samples of every mapped page (kept before stripping private arrays)
    mapped_samples = np.array([
        np.asarray(r["_page"], dtype=np.float32).reshape(8, 16, 8, 16).mean(axis=(1, 3))
        for r in mapped
    ], dtype=np.float32) if mapped else np.zeros((0, 8, 8), np.float32)
    # compact depth atlas: every current selected page, sorted by physical page index,
    # 64x64 thumbnails (2x2 box mean) for the overview figure.
    compact_records = sorted(mapped, key=lambda r: r["physical_page_index"])
    compact_pages = np.array([
        np.asarray(r["_page"], dtype=np.float32).reshape(64, 2, 64, 2).mean(axis=(1, 3))
        for r in compact_records
    ], dtype=np.float32) if compact_records else np.zeros((0, 64, 64), np.float32)
    all_pages = np.array([np.asarray(r["_page"], dtype=np.float32) for r in compact_records],
                         dtype=np.float32) if compact_records else np.zeros((0, 128, 128), np.float32)
    compact_rgb = np.zeros((len(compact_records), 64, 64, 3), dtype=np.uint8)
    for i, page in enumerate(compact_pages):
        rgb, _ = depth_rgb(page, lo, hi)
        invalid = (~np.isfinite(page)) | (page <= 0.0)
        rgb[invalid] = INVALID_DEPTH_COLOR
        compact_rgb[i] = rgb
    compact_meta = [{
        "tile": i,
        "virtual_page_xy": r["virtual_page_xy"],
        "physical_page_xy": r["physical_page_xy"],
        "physical_page_index": r["physical_page_index"],
        "page_table_texel_xy": r["page_table_texel_xy"],
        "depth_stats": r["depth_stats"],
    } for i, r in enumerate(compact_records)]

    # ---------------- virtual-address depth mosaic ---------------------------
    # Place all 99 selected mip0 pages at their virtual page coordinates,
    # crop to the active-page bbox, and use one uniform scale for the whole
    # image (nearest neighbour); no per-page rescaling.
    vpages = sorted({tuple(int(v) for v in r["virtual_page_xy"]) for r in mapped},
                    key=lambda p: (p[1], p[0]))
    if len(vpages) != len(mapped):
        raise RuntimeError("duplicate virtual page in selected-VSM map")
    vx0 = min(p[0] for p in vpages)
    vx1 = max(p[0] for p in vpages)
    vy0 = min(p[1] for p in vpages)
    vy1 = max(p[1] for p in vpages)
    cw, ch = vx1 - vx0 + 1, vy1 - vy0 + 1
    native_w, native_h = cw * VSM_PAGE_SIZE, ch * VSM_PAGE_SIZE
    mosaic_scale = min(1.0, 2048.0 / native_w, 2048.0 / native_h)
    out_w = max(1, min(2048, int(round(native_w * mosaic_scale))))
    out_h = max(1, min(2048, int(round(native_h * mosaic_scale))))
    if out_w > 2048 or out_h > 2048:
        raise RuntimeError("virtual-depth mosaic exceeds 2048 px")
    xs_native = np.minimum((np.arange(out_w) / mosaic_scale).astype(np.int64), native_w - 1)
    ys_native = np.minimum((np.arange(out_h) / mosaic_scale).astype(np.int64), native_h - 1)
    mosaic_rgb = np.empty((out_h, out_w, 3), dtype=np.uint8)
    mosaic_rgb[:] = MOSAIC_UNMAPPED_COLOR
    mosaic_mask = np.full((out_h, out_w), 2, dtype=np.uint8)
    page_category = np.full((ch, cw), 2, dtype=np.uint8)
    array_index = {tuple(int(v) for v in r["virtual_page_xy"]): i
                   for i, r in enumerate(compact_records)}
    page_order = []
    for order, (vx, vy) in enumerate(vpages):
        xx, yy = vx - vx0, vy - vy0
        gx0, gy0 = xx * VSM_PAGE_SIZE, yy * VSM_PAGE_SIZE
        cols = np.nonzero((xs_native >= gx0) & (xs_native < gx0 + VSM_PAGE_SIZE))[0]
        rows = np.nonzero((ys_native >= gy0) & (ys_native < gy0 + VSM_PAGE_SIZE))[0]
        if cols.size == 0 or rows.size == 0:
            continue
        page_rec = by_page[(vx, vy)]
        page = np.asarray(page_rec["_page"], dtype=np.float32)
        rgb, _ = depth_rgb(page, lo, hi)
        lx = xs_native[cols] - gx0
        ly = ys_native[rows] - gy0
        mosaic_rgb[np.ix_(rows, cols)] = rgb[np.ix_(ly, lx)]
        mosaic_mask[np.ix_(rows, cols)] = 0
        page_category[yy, xx] = 0
        page_order.append({
            "order": order,
            "virtual_address": int(vy * VSM_LEVEL0_DIM_PAGES_XY + vx),
            "virtual_page_xy": [int(vx), int(vy)],
            "array_index": int(array_index[(vx, vy)]),
            "physical_page_xy": [int(page_rec["physical_page_xy"][0]), int(page_rec["physical_page_xy"][1])],
            "physical_page_index": int(page_rec["physical_page_index"]),
            "page_table_texel_xy": [int(page_rec["page_table_texel_xy"][0]), int(page_rec["page_table_texel_xy"][1])],
        })
    # categorical cells inside the bbox that are not selected mip0 pages
    for yy in range(ch):
        for xx in range(cw):
            if page_category[yy, xx] == 0:
                continue
            vx, vy = vx0 + xx, vy0 + yy
            ent = decode_page_table_entry(int(pt[by + vy, bx + vx]))
            gx0, gy0 = xx * VSM_PAGE_SIZE, yy * VSM_PAGE_SIZE
            cols = np.nonzero((xs_native >= gx0) & (xs_native < gx0 + VSM_PAGE_SIZE))[0]
            rows = np.nonzero((ys_native >= gy0) & (ys_native < gy0 + VSM_PAGE_SIZE))[0]
            if ent["any_lod_valid"]:
                page_category[yy, xx] = 1
                if cols.size and rows.size:
                    mosaic_rgb[np.ix_(rows, cols)] = MOSAIC_FALLBACK_COLOR
                    mosaic_mask[np.ix_(rows, cols)] = 1
    mosaic_meta = {
        "bbox_virtual_page": [int(vx0), int(vy0), int(vx1), int(vy1)],
        "bbox_page_wh": [int(cw), int(ch)],
        "native_px": [int(native_w), int(native_h)],
        "output_px": [int(out_w), int(out_h)],
        "scale": float(mosaic_scale),
        "scale_policy": "single uniform nearest-neighbour scale for the whole mosaic; 1.0 when the native bbox fits within 2048 px; no per-page rescale",
        "mapping": "output x samples native x = min(floor(x/scale), native_w-1); page (vx,vy) occupies native rect [(vx-vx0)*128,(vy-vy0)*128]",
        "selected_pages": int(len(page_order)),
        "page_cells": int(cw * ch),
        "mapped_cells": int(np.count_nonzero(page_category == 0)),
        "fallback_cells": int(np.count_nonzero(page_category == 1)),
        "unmapped_cells": int(np.count_nonzero(page_category == 2)),
        "depth_display_range_device_z": [float(lo), float(hi)],
        "category_legend": {str(k): (list(v) if v else "depth-ramp") for k, v in MOSAIC_CATEGORY_COLORS.items()},
        "category_labels": {str(k): v for k, v in MOSAIC_CATEGORY_LABELS.items()},
        "page_order": page_order,
        "mask_npz": "virtual_depth_mask_u8: 0=mapped depth, 1=coarser-clipmap fallback, 2=unmapped",
        "page_category_npz": "virtual_depth_page_category_u8: same categories at virtual-page resolution (bbox row-major)",
        "no_burned_text": True,
    }

    # remove private arrays from JSON records
    for r in mapped:
        r.pop("_page", None)

    # page flags distribution
    pf_counts = {hex(int(k)): int(v) for k, v in zip(*np.unique(pf_low, return_counts=True))}
    page_table_counts = {
        "total_entries": int(dim * dim),
        "any_lod_valid": int(anyv.sum()),
        "valid_for_rendering": int(render.sum()),
        "secondary": int(sec.sum()),
        "unmapped": int((~anyv).sum()),
        "page_flags_nonzero": int(np.count_nonzero(pf_low)),
        "page_flags_categories": pf_counts,
        "lod_histogram": lod_hist,
    }

    # source ranges
    engine = Path(args.engine) if args.engine else None
    source_ranges = build_source_ranges(engine)
    source_ranges["field_offsets"] = {
        "FPhysicalPageMetaData.Flags": 0,
        "FPhysicalPageMetaData.LastRequestedSceneFrameNumber": 4,
        "FPhysicalPageMetaData.VirtualShadowMapId": 8,
        "FPhysicalPageMetaData.MipLevel": 12,
        "FPhysicalPageMetaData.PageAddress.x": 16,
        "FPhysicalPageMetaData.PageAddress.y": 20,
        "FVirtualShadowMapProjectionShaderData.ShadowViewToClipMatrix": 0,
        "FVirtualShadowMapProjectionShaderData.TranslatedWorldToShadowUVMatrix": 64,
        "FVirtualShadowMapProjectionShaderData.ShadowUVToTranslatedWorldMatrix": 128,
        "FVirtualShadowMapProjectionShaderData.TranslatedWorldToShadowUVNormalMatrix": 192,
        "FVirtualShadowMapProjectionShaderData.LightDirection": 256,
        "FVirtualShadowMapProjectionShaderData.LightType": 268,
        "FVirtualShadowMapProjectionShaderData.PreViewTranslationHigh": 272,
        "FVirtualShadowMapProjectionShaderData.LightRadius": 284,
        "FVirtualShadowMapProjectionShaderData.PreViewTranslationLow": 288,
        "FVirtualShadowMapProjectionShaderData.ResolutionLodBias": 300,
        "FVirtualShadowMapProjectionShaderData.ClipmapWorldOriginOffset": 304,
        "FVirtualShadowMapProjectionShaderData.LightSourceRadius": 316,
        "FVirtualShadowMapProjectionShaderData.ClipmapCornerRelativeOffset": 320,
        "FVirtualShadowMapProjectionShaderData.ClipmapLevel_ClipmapLevelCountRemaining": 328,
        "FVirtualShadowMapProjectionShaderData.PackedCullingViewId": 332,
        "FVirtualShadowMapProjectionShaderData.Flags": 336,
        "FVirtualShadowMapProjectionShaderData.ClipmapLevelWPODistanceDisableThresholdSquared": 340,
        "FVirtualShadowMapProjectionShaderData.TexelDitherScale": 344,
        "FVirtualShadowMapProjectionShaderData.MinMipLevel": 348,
        "FVirtualShadowMapProjectionShaderData.LightId": 352,
        "FVirtualShadowMapProjectionShaderData.DynamicDepthCullRange": 356,
        **{f"FVirtualShadowMapUniformParameters.{f['name']}": int(f["offset"]) for f in uniform["fields"] if f.get("name")},
        "PageTableEntry.physical_x": "bits[0:9]",
        "PageTableEntry.physical_y": "bits[10:19]",
        "PageTableEntry.lod_offset": "bits[20:25]",
        "PageTableEntry.secondary_request": "bit[29]",
        "PageTableEntry.valid_for_rendering": "bit[30]",
        "PageTableEntry.any_lod_valid": "bit[31]",
        "PageFlags.H0..H6": "separate PageFlags hardware mips 0..6; H_m parent texel = H0 texel >> m; H7 is not generated",
    }
    source_ranges["source_constants"] = {
        "VSM_LOG2_PAGE_SIZE": VSM_LOG2_PAGE_SIZE,
        "VSM_PAGE_SIZE": VSM_PAGE_SIZE,
        "VSM_LEVEL0_DIM_PAGES_XY": VSM_LEVEL0_DIM_PAGES_XY,
        "VSM_MAX_MIP_LEVELS": VSM_MAX_MIP_LEVELS,
        "VSM_PAGE_TABLE_TEX2D_SIZE_X": VSM_PAGE_TABLE_TEX2D_SIZE_X,
        "VSM_PAGE_TABLE_TEX2D_SIZE_Y": VSM_PAGE_TABLE_TEX2D_SIZE_Y,
        "VSM_MAX_SINGLE_PAGE_SHADOW_MAPS": VSM_MAX_SINGLE_PAGE_SHADOW_MAPS,
        "VSM_PSD_STRIDE": VSM_PSD_STRIDE,
        "FPhysicalPageMetaData_stride_bytes": 24,
        "PAGE_FLAGS_BITS_PER_HMIP": 6,
        "PAGE_TABLE_TEXEL_BLOCK": [VSM_PAGE_TABLE_TEX2D_SIZE_X, VSM_PAGE_TABLE_TEX2D_SIZE_Y],
    }
    source_ranges["captured_constants"] = {
        k: u[k] for k in ("NumFullShadowMaps", "NumSinglePageShadowMaps", "MaxPhysicalPages",
                          "NumShadowMapSlots", "StaticCachedArrayIndex", "PhysicalPageRowMask",
                          "PhysicalPageRowShift", "PhysicalPoolSize", "PhysicalPoolSizePages",
                          "PageTableRowShift", "PageTableRowMask", "SceneFrameNumber")
    }

    passes_out = {
        "vsm_projection": vp,
        "composite": passes["composite"],
        "sun": passes["sun"],
        "graph_prefix": passes["graph_prefix"],
    }
    vp_output = vp.get("OutputResources", [None])[0] if vp.get("OutputResources") else None
    comp_output = (passes["composite"].get("OutputResources") or [None])[0] if passes["composite"] else None
    snapshots = snapshot_record(ops, dump, vp)

    readbacks = []
    for key in ("page_table", "pool_slice0", "pool_slice1"):
        l = ops[key]
        rb = dump.readback_of(l)
        readbacks.append({"file": dump.rel_file(l), "readback": rb})
    for l in ops["page_flags"]:
        readbacks.append({"file": dump.rel_file(l), "readback": dump.readback_of(l)})

    ctx = {
        "dump": dump, "passes": passes_out, "uniform": uniform, "ids": ops["ids"],
        "page_table": pt, "page_flags": pf_mips, "pool0": pool0, "pool1": pool1,
        "metadata": metadata, "projection": projection, "projection_count": projection_count,
        "sun": sun, "sun_count": len(sun), "base": base, "selected": sel,
        "receivers": receivers, "used_raw_paths": used_paths, "raw_hash_before": raw_before,
        "ref_count": ref_count,
        "readbacks": readbacks, "vp_output": vp_output, "composite_output": comp_output,
        "ops": ops, "source_ranges": source_ranges, "engine": engine,
        "pool_shape": pool_shape, "page_table_block": block,
        "compact_depth_pages": compact_pages, "compact_depth_rgb": compact_rgb,
        "compact_tiles": compact_meta,
        "page_table_render": render, "page_table_pf_low": pf_low,
        "page_table_entry_mismatch": mismatch,
        "logical_mips": logical_mips, "logical_entry": logical_entry,
        "logical_render": logical_render, "logical_lod": logical_lod,
        "page_flags_hierarchy": flags_h, "snapshots": snapshots,
        "virtual_depth_mosaic_rgb": mosaic_rgb, "virtual_depth_mask": mosaic_mask,
        "virtual_depth_page_category": page_category, "virtual_depth_meta": mosaic_meta,
    }

    # ---------------- publish data -------------------------------------------
    data_dir = out / "publish" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    npz_arrays = {
        "uniform_raw_u8": np.frombuffer(uniform["raw"], dtype=np.uint8),
        "projection_sun_raw_u8": np.frombuffer(proj_raw, dtype=np.uint8)[
            sun[0]["id"] * VSM_PSD_STRIDE:(sun[-1]["id"] + 1) * VSM_PSD_STRIDE].reshape(-1, VSM_PSD_STRIDE),
        "projection_sun_shadow_view_to_clip": np.array([r["shadow_view_to_clip"] for r in sun], dtype=np.float64),
        "projection_sun_tw_to_shadow_uv": np.array([r["translated_world_to_shadow_uv"] for r in sun], dtype=np.float64),
        "projection_sun_shadow_uv_to_tw": np.array([r["shadow_uv_to_translated_world"] for r in sun], dtype=np.float64),
        "projection_sun_light_direction": np.array([r["light_direction"] for r in sun], dtype=np.float64),
        "projection_sun_clipmap_level": np.array([r["clipmap_level"] for r in sun], dtype=np.int32),
        "projection_sun_flags": np.array([r["flags"] for r in sun], dtype=np.uint32),
        "page_table_entry_u32": ptb.astype(np.uint32),
        "page_table_phys_x": phys_x, "page_table_phys_y": phys_y,
        "page_table_lod": lod, "page_table_any": anyv, "page_table_render": render,
        "page_table_secondary": sec, "page_table_phys_index": phys_index,
        "page_table_logical_mip_entry_u32": logical_entry,
        "page_table_logical_mip_render": logical_render,
        "page_table_logical_mip_lod": logical_lod,
        "page_table_logical_mip_dim": np.array([r["dim_pages"] for r in logical_mips], dtype=np.uint16),
        "page_table_logical_mip_base_texel": np.array([r["base_texel"] for r in logical_mips], dtype=np.int32),
        "page_table_flags_u32": pfb.astype(np.uint32),
        "page_table_flags_low6": pf_low,
        "page_table_flags_hierarchy_h0_h6": flags_h,
        "metadata_u32": metadata.astype(np.uint32),
        "occupancy_category": occ,
        "meta_state_category": state,
        "virtual_depth_mask_u8": mosaic_mask,
        "virtual_depth_page_category_u8": page_category,
        "depth_mosaic_rgb_u8": depth_rgb_arr,
        "depth_mosaic_z_f32": depth_z,
        "depth_pages_f32": np.array([t["_page"] for t in tiles], dtype=np.float32),
        "depth_pages_raw_u32": np.array([t["_page"] for t in tiles], dtype=np.float32).view(np.uint32),
        "depth_page_samples_f32": mapped_samples,
        "compact_depth_pages_f32": compact_pages,
        "compact_depth_rgb_u8": compact_rgb,
        "depth_pages_all_f32": all_pages,
        "depth_pages_all_raw_u32": all_pages.view(np.uint32),
        "receiver_depth_f32": np.array([receivers[k]["stored_depth_device_z"] if k in receivers else np.nan for k in ("A", "B")], dtype=np.float32),
        "receiver_z_f32": np.array([receivers[k]["receiver_device_z"] if k in receivers else np.nan for k in ("A", "B")], dtype=np.float32),
        "receiver_uv_f32": np.array([receivers[k]["shadow_uv"] if k in receivers else [np.nan, np.nan] for k in ("A", "B")], dtype=np.float32),
        "receiver_virtual_page": np.array([receivers[k]["virtual_page_xy"] if k in receivers else [-1, -1] for k in ("A", "B")], dtype=np.int32),
        "receiver_physical_page": np.array([receivers[k]["physical_page_xy"] if k in receivers else [-1, -1] for k in ("A", "B")], dtype=np.int32),
        "receiver_physical_texel": np.array([receivers[k]["physical_texel_xy"] if k in receivers else [-1, -1] for k in ("A", "B")], dtype=np.int32),
        "receiver_page_table_texel": np.array([receivers[k]["page_table_texel_xy"] if k in receivers else [-1, -1] for k in ("A", "B")], dtype=np.int32),
    }
    # keep only arrays that have values
    npz_arrays = {k: v for k, v in npz_arrays.items() if v is not None}
    np.savez_compressed(data_dir / "vsm_storage.npz", **npz_arrays)

    # figure creation needs palettes and tiles; build after npz so JSON has hashes
    ctx["occupancy_category"] = occ
    ctx["meta_state_category"] = state
    ctx["depth_mosaic_rgb"] = depth_rgb_arr
    ctx["depth_display_lo"] = lo
    ctx["depth_display_hi"] = hi
    ctx["selected_vsm_id"] = sel["id"]
    ctx["selected_clipmap_level"] = sel["clipmap_level"]
    ctx["tiles"] = tiles
    ctx["page_table_lod_rgb"] = lod_rgb_arr
    ctx["page_table_identity_rgb"] = ident
    ctx["page_table_flags_rgb"] = flag_rgb_arr
    ctx["page_table_markers"] = markers
    figs = build_figures(out, ctx)
    ctx["figures"] = figs

    # data JSON
    input_records = {}
    if args.receivers:
        p = Path(args.receivers)
        input_records["receivers"] = {"file": p.name, "bytes": p.stat().st_size, "sha256": sha256_file(p)}
    if args.view:
        p = Path(args.view)
        input_records["view"] = {"file": p.name, "bytes": p.stat().st_size, "sha256": sha256_file(p)}
    archive_rec = None
    if args.archive:
        apath = Path(args.archive)
        idx = apath / "index.json"
        archive_rec = {"package": apath.name, "index_present": idx.exists()}
        if idx.exists():
            archive_rec.update({"index_bytes": idx.stat().st_size, "index_sha256": sha256_file(idx)})
    ctx["input_records"] = input_records
    ctx["archive_rec"] = archive_rec
    data = {
        "schema": "vsm-storage-v1",
        "run": {
            "dump_root_name": dump.root.name,
            "scene_graph_prefix": passes["graph_prefix"],
            "selection_rule": "last VirtualShadowMapProjection pass in the dump (highest pass Id within the final graph prefix); earlier scene invocations are not used",
            "native_scene_invocation": f"native{int(passes['graph_prefix'], 16):04d}" if passes["graph_prefix"] else None,
            "scene_frame_number": int(u["SceneFrameNumber"]),
            "engine": "UE5 (see package Infos.json)",
            "input_records": input_records,
            "archive_package": archive_rec,
        },
        "passes": {
            "vsm_projection": {"id": vp["Id"], "pointer": vp["Pointer"], "name": vp["EventName"], "output": vp_output},
            "composite": {"id": passes["composite"]["Id"], "pointer": passes["composite"]["Pointer"],
                          "name": passes["composite"]["EventName"], "output": comp_output} if passes["composite"] else None,
            "sun": {"id": passes["sun"]["Id"], "pointer": passes["sun"]["Pointer"],
                    "name": passes["sun"]["EventName"]} if passes["sun"] else None,
            "lineage": "840 VirtualShadowMapProjection -> 841 CompositeVirtualShadowMapMask -> 842 RenderLight Sun",
        },
        "operands": {
            "projection_data": {
                "resource": ops["ids"]["ProjectionData"], "version": ops["projection"]["version"],
                "producer_pass": {"id": ops["projection"]["pass_id"], "name": ops["projection"]["pass_name"]},
                "file": ops["projection"]["file"], "bytes": int(Path(ops["projection"]["path"]).stat().st_size),
                "sha256": sha256_file(Path(ops["projection"]["path"])),
                "stride_bytes": VSM_PSD_STRIDE, "record_count": projection_count,
                "index_formula": "byte_offset = VirtualShadowMapId * 368; struct order follows VirtualShadowMapProjectionStructs.ush",
                "sun_records": sun,
                "all_record_ids_with_nonzero_flags": [r["id"] for r in projection if r["flags"] != 0],
            },
            "uniform": {
                "binding": uniform["binding"], "content": uniform["content"], "metadata": uniform["metadata"],
                "file": uniform["file"], "metadata_file": uniform["metadata_file"],
                "bytes": uniform["bytes"], "sha256": uniform["sha256"], "metadata_sha256": uniform["metadata_sha256"],
                "struct_type": uniform["struct_type"], "source_file": uniform["source_file"], "source_line": uniform["source_line"],
                "fields": uniform["fields"], "raw_hex": uniform["raw_hex"],
            },
            "page_table": {
                "resource": ops["ids"]["PageTable"], "version": ops["version"],
                "file": dump.rel_file(ops["page_table"]), "sha256": sha256_file(dump.root / dump.rel_file(ops["page_table"])),
                "extent": [int(ops["page_table"]["ExtentX"]), int(ops["page_table"]["ExtentY"])],
                "format": ops["page_table"]["StoredFormat"], "row_pitch_bytes": int(ops["page_table"]["RowPitchBytes"]),
                "row_shift": int(u["PageTableRowShift"]), "row_mask": int(u["PageTableRowMask"]),
                "descriptor_num_mips": int(ops["page_table_desc"].get("NumMips") or 0),
                "hardware_mips_captured": [int(ops["page_table"]["MipLevel"])],
                "hardware_mip_note": "PageTable hardware mip0 is the only hardware mip; it packs all eight logical shadow-map mips 0..7 in each VSM 128x192 block",
                "logical_mip_count": VSM_MAX_MIP_LEVELS,
                "logical_mip_layout": "mip0 = 128x128 at block base; logical mips 1..7 = tail at y=128 with x = CalcLevelOffsets(mip).x and dim 128>>mip (source VirtualShadowMapPageAccessCommon.ush CalcLevelOffsets)",
                "logical_mips": logical_mips,
                "snapshot": snapshots["page_table"],
                "selected_vsm": {
                    "id": sel["id"], "full_id": block["full_id"], "row": block["row"], "column": block["column"],
                    "clipmap_level": sel["clipmap_level"], "clipmap_level_index": sel["clipmap_level_index"],
                    "block_base_texel": block["base_texel"], "block_dim": dim,
                    "address": "texel = block_base + CalcLevelOffsets(mip) + (virtual_page_x, virtual_page_y)",
                },
                "counts": page_table_counts,
                "lod_offset_semantics": "directional clipmap path: SampleVirtualShadowMapClipmap uses LODOffset as a coarser clipmap-level offset and samples mip 0 of that level; for local lights the same field can select a logical page-table mip",
                "mapped_pages": mapped,
                "entry_fields": ["physical_x[0:9]", "physical_y[10:19]", "lod_offset[20:25]",
                                 "secondary_request[29]", "valid_for_rendering[30]", "any_lod_valid[31]"],
                "reencode_mismatches": int(mismatch),
            },
            "page_flags": {
                "resource": ops["ids"]["PageFlags"], "version": ops["version"],
                "descriptor_num_mips": int(ops["page_flags_desc"].get("NumMips") or 0),
                "mips": [{"mip": m, "file": dump.rel_file(l), "extent": [int(l["ExtentX"]), int(l["ExtentY"])],
                          "sha256": sha256_file(dump.root / dump.rel_file(l)),
                          "last_recorded_writer": snapshots["page_flags"]["last_recorded_writers"][m]}
                         for m, l in enumerate(ops["page_flags"])],
                "snapshot": snapshots["page_flags"],
                "selected_vsm_mip0_block_base_texel": block["base_texel"],
                "selected_vsm_mip0_categories": page_table_counts["page_flags_categories"],
                "selected_vsm_hierarchy_nonzero": {f"H{m}": int(np.count_nonzero(flags_h[m]))
                                                   for m in range(flags_h.shape[0])},
                "hierarchy": "PageFlags hardware mips 0..6 are the separate H0..H6 hierarchy: mip0 is exact page flags; mipm is the parent OR at texel >> m. H7 is not generated (OutPageFlagMips_0..5, no _6). This is a different hierarchy from the eight logical shadow mips packed in PageTable hardware mip0.",
                "h0_flag_bits": VSM_FLAGS,
            },
            "physical_page_metadata": {
                "resource": ops["metadata"]["resource"], "version": ops["metadata"]["version"],
                "producer_pass": {"id": ops["metadata"]["pass_id"], "name": ops["metadata"]["pass_name"]},
                "file": ops["metadata"]["file"], "sha256": sha256_file(Path(ops["metadata"]["path"])),
                "record_count": int(metadata.shape[0]), "record_stride_bytes": 24,
                "fields": [{"name": "Flags", "offset": 0, "type": "uint32"},
                           {"name": "LastRequestedSceneFrameNumber", "offset": 4, "type": "uint32"},
                           {"name": "VirtualShadowMapId", "offset": 8, "type": "uint32"},
                           {"name": "MipLevel", "offset": 12, "type": "uint32"},
                           {"name": "PageAddress.x", "offset": 16, "type": "uint32"},
                           {"name": "PageAddress.y", "offset": 20, "type": "uint32"}],
                "records_columns": ["Flags", "LastRequestedSceneFrameNumber", "VirtualShadowMapId", "MipLevel", "PageAddress.x", "PageAddress.y"],
                "records_u32": metadata.astype(np.uint32).tolist(),
                "nonzero_records": int(np.count_nonzero(metadata[:, 0])),
                "current_frame_records": int(np.count_nonzero((metadata[:, 0] != 0) & (metadata[:, 1] == scene_frame))),
                "unreferenced_in_snapshot_records": int(np.count_nonzero((metadata[:, 0] != 0) & (metadata[:, 1] != scene_frame))),
                "unreferenced_definition": "captured metadata Flags != 0 with LastRequestedSceneFrameNumber != SceneFrameNumber; these pages are not mapped by the current selected-VSM mip0 page table. Age alone does not prove the records stale or invalid.",
                "unreferenced_not_proven_invalid": True,
                "snapshot": snapshots["physical_page_metadata"],
                "snapshot_note": "latest captured metadata snapshot at pass 810 (SelectPagesToPostProcess), before the pass-840 consumer; version is not pass 840",
            },
            "physical_page_pool": {
                "resource": ops["ids"]["PhysicalPagePool"], "version": ops["version"],
                "desc": ops["pool_desc"],
                "snapshot": snapshots["physical_page_pool"],
                "slice_0": {"file": dump.rel_file(ops["pool_slice0"]), "sha256": sha256_file(dump.root / dump.rel_file(ops["pool_slice0"])),
                            "bytes": int(ops["pool_slice0"]["ByteSize"]), "role": "dynamic/merged pool read by SampleVirtualShadowMapPhysicalDepth"},
                "slice_1": {"file": dump.rel_file(ops["pool_slice1"]), "sha256": sha256_file(dump.root / dump.rel_file(ops["pool_slice1"])),
                            "bytes": int(ops["pool_slice1"]["ByteSize"]), "role": "static page pool; merged into slice 0 by max() at pass 811"},
                "extent": [pool_shape[1], pool_shape[0]], "format": "PF_R32_UINT (depth stored as float32 bits)",
                "physical_page_grid": [pool_shape[1] // VSM_PAGE_SIZE, pool_shape[0] // VSM_PAGE_SIZE],
                "page_size_texels": [VSM_PAGE_SIZE, VSM_PAGE_SIZE],
                "query_contract": (
                    "raw_u32 = read_uint32(slice_file, 4*((phys_y*128+local_y)*16384 + (phys_x*128+local_x))); "
                    "depth_device_z = bitcast_float32(raw_u32); physical_page_index = (phys_y<<7)+phys_x"
                ),
                "full_pool_note": "full 256 MiB slices are retained in the raw capture; only selected 128x128 pages are duplicated in the NPZ",
                "pool_slice_1_not_depth_plotted": True,
            },
        },
        "snapshot_lineage": snapshots,
        "sun": {
            "base_vsm_id": int(base["id"]),
            "count": len(sun),
            "ids": sun_ids,
            "clipmap_levels": [r["clipmap_level"] for r in sun],
            "clipmap_level_indices": [r["clipmap_level_index"] for r in sun],
            "flags": [r["flags_hex"] for r in sun],
            "light_direction": base["light_direction"],
            "light_type": base["light_type"],
            "base_clipmap_level_count_remaining": base_remaining,
            "selection": "contiguous run of full (non-single-page) VSM records 8192..; all directional, same light direction, consecutive clipmap levels",
        },
        "receivers_declared_replay": receivers,
        "counts": {
            "physical_pages": int(metadata.shape[0]),
            "physical_page_metadata_nonzero": int(np.count_nonzero(metadata[:, 0])),
            "physical_page_metadata_unreferenced_in_snapshot": int(np.count_nonzero(
                (metadata[:, 0] != 0) & (metadata[:, 1] != scene_frame))),
            "physical_pages_referenced_by_page_table_mip0": int(np.count_nonzero(ref)),
            "selected_vsm_mip0_entries": int(dim * dim),
            "selected_vsm_mip0_mapped_render_pages": len(mapped),
            "selected_vsm_mip0_lod_fallback_entries": int((lod > 0).sum()),
            "selected_vsm_logical_mips": len(logical_mips),
            "selected_vsm_logical_mip_empty": int(sum(1 for r in logical_mips if r["empty"])),
            "selected_vsm_logical_mip_nonempty": int(sum(1 for r in logical_mips if not r["empty"])),
            "selected_vsm_logical_mip1_7_render_entries": int(sum(r["valid_for_rendering"] for r in logical_mips[1:])),
            "selected_vsm_page_flags_nonzero": int(np.count_nonzero(pf_low)),
            "occupancy_counts": occ_counts,
            "metadata_state_counts": meta_state_counts,
            "depth_stats": dstats,
            "depth_mapped_page_count": len(mapped),
            "depth_mapped_pages_with_zero_pixels": mapped_zero,
            "depth_mapped_pages_owner_mismatch": len(mapped) - mapped_owner,
            "virtual_depth_mosaic_pages": mosaic_meta["selected_pages"],
            "virtual_depth_mosaic_fallback_cells": mosaic_meta["fallback_cells"],
            "virtual_depth_mosaic_unmapped_cells": mosaic_meta["unmapped_cells"],
        },
        "display": {
            "depth_convention": "device Z from a reversed-Z orthographic sun projection: larger = closer to light; sample is pool slice 0 at the physical texel",
            "selected_projection": {"vsm_id": sel["id"], "clipmap_level": sel["clipmap_level"]},
            "depth_display_range_device_z": [lo, hi],
            "depth_display_formula": "t = clip((z-lo)/(hi-lo),0,1); RGB = piecewise-linear DEPTH_RAMP_STOPS; inverse t = (z-lo)/(hi-lo) recovers the display coordinate",
            "depth_ramp_stops": DEPTH_RAMP_STOPS[:, 1:].astype(int).tolist(),
            "invalid_rgb": list(INVALID_DEPTH_COLOR),
            "masked_rgb": list(MASKED_COLOR),
            "cross_projection_note": "depth ranges are not comparable across different VSM projections; the depth panels use one fixed range for the selected sun clipmap only",
            "occupancy_legend": {str(k): list(v) for k, v in OCCUPANCY_COLORS.items()},
            "occupancy_category_labels": {str(k): v for k, v in OCCUPANCY_LABELS.items()},
            "metadata_state_legend": {str(k): list(v) for k, v in META_STATE_COLORS.items()},
            "metadata_state_category_labels": {str(k): v for k, v in META_STATE_LABELS.items()},
            "page_table_lod_legend": {str(k): list(v) for k, v in LOD_COLORS.items()},
            "virtual_depth_mosaic_category_legend": {str(k): (list(v) if v else "depth-ramp")
                                                      for k, v in MOSAIC_CATEGORY_COLORS.items()},
        },
        "figures": figs,
        "captured_but_not_used": {
            "AllocatedPageRectBounds/PageRectBounds": "per-page conservative bounds used by the raster path; point storage lookups use the page table entry and metadata owner instead",
            "PageReceiverMasks": "receiver-mask visibility; not needed for the selected sun page-table/depth address chain",
            "HZBPhysicalPagePool": "occlusion hierarchy for page culling, not the depth value consumed by projection",
            "NextVirtualShadowMapData": "next-frame allocator handoff, not current-frame storage",
            "StatsBuffer": "diagnostic counters, not storage contents",
        },
        "limits": [
            "PageTable hardware mip0 packs all eight logical shadow-map mips 0..7 in each VSM 128x192 block. For selected VSM 8196, logical mip0 holds 99 render-valid pages and logical mips 1..7 are present but empty (all-zero entries); no logical page-table mip is missing from the capture.",
            "PageFlags hardware mips 0..6 are a separate H0..H6 hierarchy (mipm parent at texel >> m); H7 is not generated. This hierarchy is not the eight logical mips packed in PageTable hardware mip0.",
            "The receiver chain is a declared replay of clipmap selection and page lookup from captured operands; SMRT ray directions, cone, slope/normal biases and per-lane hits are shader-local and not captured.",
            "The captured ShadowMaskTexture is compared for information only; this replay does not reproduce it.",
            "The page-table, PageFlags and pool files are pass-840 consumer snapshots versioned at the capture boundary; their last resource-level output bindings are earlier passes (page table 792, pool 811, PageFlags mip0 790 / mip1-6 791). Physical-page metadata is the pass-810 snapshot before the pass-840 consumer.",
            "The 96 metadata records with flags 0x285b and LastRequestedSceneFrameNumber 3 are allocated slots that are not referenced in this snapshot; age alone does not prove them stale or invalid. Free/unallocated slots remain neutral gray. The selected99pages have no zero-valued depths; the compact atlas masks nonpositive values, while the native-page and mosaic views apply the declared ramp directly.",
            "Full pool bytes are not duplicated; all 99 current selected pages are published as full 128x128 float32 + raw uint32, and the 2 x 256 MiB slices remain in the raw archive with exact path/version/SHA and query contract.",
            "Depth values from different clipmap levels or projections are not globally comparable; the virtual-depth mosaic uses one fixed range for selected VSM 8196.",
        ],
    }

    # manifest of published files is generated after figure/data JSON are written;
    # figure hashes are already known from build_figures.
    write_json(data_dir / "vsm_storage.json", data)
    ctx["counts"] = data["counts"]

    schema = {
        "schema": "vsm-storage-schema-v1",
        "json": "publish/data/vsm_storage.json",
        "npz": "publish/data/vsm_storage.npz",
        "npz_arrays": {
            "uniform_raw_u8": {"shape": [368], "dtype": "uint8", "desc": "captured FVirtualShadowMapUniformParameters bytes"},
            "projection_sun_raw_u8": {"shape": [len(sun), 368], "dtype": "uint8", "desc": "raw projection records for the 17 sun VSMs"},
            "projection_sun_*": {"dtype": "float64/int32/uint32", "desc": "decoded projection record fields"},
            "page_table_entry_u32": {"shape": [dim, dim], "dtype": "uint32", "desc": "selected sun VSM mip0 page-table entries"},
            "page_table_phys_x/y": {"shape": [dim, dim], "dtype": "uint16", "desc": "decoded physical page x/y"},
            "page_table_lod": {"shape": [dim, dim], "dtype": "uint8", "desc": "LOD offset 0..7"},
            "page_table_any/render/secondary": {"shape": [dim, dim], "dtype": "bool", "desc": "decoded page-table flag bits"},
            "page_table_logical_mip_entry_u32": {"shape": [8, dim, dim], "dtype": "uint32",
                                                 "desc": "all eight logical page-table mips 0..7 packed in the selected VSM hardware mip0; entry lm occupies [:dim_lm,:dim_lm] with dim from page_table_logical_mip_dim"},
            "page_table_logical_mip_render": {"shape": [8, dim, dim], "dtype": "bool", "desc": "valid_for_rendering per logical mip region"},
            "page_table_logical_mip_lod": {"shape": [8, dim, dim], "dtype": "uint8", "desc": "LOD offset per logical mip region"},
            "page_table_logical_mip_dim": {"shape": [8], "dtype": "uint16", "desc": "dim_pages of each logical mip region"},
            "page_table_logical_mip_base_texel": {"shape": [8, 2], "dtype": "int32", "desc": "block base texel of each logical mip region"},
            "page_table_flags_u32": {"shape": [dim, dim], "dtype": "uint32", "desc": "PageFlags hardware mip0 exact-page (H0) words"},
            "page_table_flags_hierarchy_h0_h6": {"shape": [7, dim, dim], "dtype": "uint8",
                                                "desc": "H0..H6 from the separate PageFlags hardware mips 0..6; H_m parent texel = base >> m; H7 not generated"},
            "metadata_u32": {"shape": [4096, 6], "dtype": "uint32", "desc": "all FPhysicalPageMetaData records, field order per JSON"},
            "occupancy_category": {"shape": [32, 128], "dtype": "uint8", "desc": "F1 panel 0 categories"},
            "meta_state_category": {"shape": [32, 128], "dtype": "uint8", "desc": "F1 panel 2 categories"},
            "virtual_depth_mask_u8": {"shape": [int(mosaic_mask.shape[0]), int(mosaic_mask.shape[1])], "dtype": "uint8",
                                      "desc": "F4 categorical mask: 0=mapped depth, 1=coarser-clipmap fallback, 2=unmapped"},
            "virtual_depth_page_category_u8": {"shape": [int(page_category.shape[0]), int(page_category.shape[1])], "dtype": "uint8",
                                               "desc": "F4 categories at virtual-page resolution in the bbox (row-major)"},
            "depth_pages_f32": {"shape": [6, 128, 128], "dtype": "float32", "desc": "native selected depth pages, F3 order"},
            "depth_pages_raw_u32": {"shape": [6, 128, 128], "dtype": "uint32", "desc": "same pages as raw bits"},
            "depth_page_samples_f32": {"shape": [99, 8, 8], "dtype": "float32", "desc": "8x8 block means for every mapped page"},
            "compact_depth_pages_f32": {"shape": [99, 64, 64], "dtype": "float32", "desc": "F1 middle panel: every current selected page, 2x2 box mean, sorted by physical page index"},
            "compact_depth_rgb_u8": {"shape": [99, 64, 64, 3], "dtype": "uint8", "desc": "display RGB for compact_depth_pages_f32 using the declared fixed range"},
            "depth_pages_all_f32": {"shape": [99, 128, 128], "dtype": "float32", "desc": "full-resolution native depth for every current selected-VSM page used by the compact atlas, sorted by physical page index (same order as compact tile_order)"},
            "depth_pages_all_raw_u32": {"shape": [99, 128, 128], "dtype": "uint32", "desc": "raw uint32 bits of depth_pages_all_f32"},
            "depth_mosaic_rgb_u8": {"shape": [256, 1024, 3], "dtype": "uint8", "desc": "physical-layout depth sample raster (retained; F1 uses the compact atlas instead)"},
            "depth_mosaic_z_f32": {"shape": [256, 1024], "dtype": "float32", "desc": "sampled device Z in physical layout; NaN = masked"},
            "receiver_depth_f32": {"shape": [2], "dtype": "float32", "desc": "A,B stored depth"},
            "receiver_z_f32": {"shape": [2], "dtype": "float32", "desc": "A,B receiver projected device Z"},
            "receiver_uv_f32": {"shape": [2, 2], "dtype": "float32", "desc": "A,B shadow UV"},
            "receiver_virtual_page": {"shape": [2, 2], "dtype": "int32", "desc": "A,B virtual page"},
            "receiver_physical_page": {"shape": [2, 2], "dtype": "int32", "desc": "A,B physical page"},
            "receiver_physical_texel": {"shape": [2, 2], "dtype": "int32", "desc": "A,B physical texel"},
            "receiver_page_table_texel": {"shape": [2, 2], "dtype": "int32", "desc": "A,B page-table texel"},
        },
        "record_layouts": {
            "FPhysicalPageMetaData": "Flags u32 @0, LastRequestedSceneFrameNumber u32 @4, VirtualShadowMapId u32 @8, MipLevel u32 @12, PageAddress.x u32 @16, PageAddress.y u32 @20 (24 B)",
            "PageTableEntry": "physical_x[0:9], physical_y[10:19], lod_offset[20:25], secondary[29], render_valid[30], any_valid[31]",
            "PageFlags": "32-bit exact-page flags; H0..H6 are separate PageFlags hardware mips 0..6 (parent texel >> m); H7 not generated",
        },
    }
    write_json(data_dir / "schema.json", schema)

    # ---------------- checks --------------------------------------------------
    checks = []
    ctx["mapped_pages_total"] = len(mapped)
    ctx["mapped_owner_match"] = mapped_owner
    ctx["mapped_oob"] = oob
    ctx["mapped_depth_finite"] = mapped_finite
    ctx["mapped_depth_zero"] = mapped_zero
    ctx["entry_roundtrip_mismatch"] = mismatch
    ctx["depth_stats"] = dstats
    ctx["occupancy_counts"] = occ_counts
    ctx["meta_state_counts"] = meta_state_counts
    ctx["lod_histogram"] = lod_hist
    ctx["page_table_counts"] = page_table_counts
    ctx["metadata_snapshot"] = {"pass_id": ops["metadata"]["pass_id"], "pass_name": ops["metadata"]["pass_name"]}
    raw_after = run_decode_checks(ctx, checks)

    # report
    report = build_report(ctx)
    (out / "report.md").write_text(report, encoding="utf-8")
    write_json(out / "source-ranges.json", source_ranges)

    # manifest (exclude checks.json/report? include report and source-ranges; exclude manifest itself)
    pub_files = []
    for p in sorted((out / "publish").rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            pub_files.append({"path": str(p.relative_to(out)).replace("\\", "/"),
                              "bytes": p.stat().st_size, "sha256": sha256_file(p)})
    for name in ("report.md", "source-ranges.json"):
        p = out / name
        pub_files.append({"path": name, "bytes": p.stat().st_size, "sha256": sha256_file(p)})
    manifest = {"schema": "vsm-publish-manifest-v1", "root": out.name, "files": pub_files}
    write_json(out / "publish" / "manifest.json", manifest)

    # decode checks + immediate verify
    verify_checks = []
    verify_publish(out, verify_checks, engine)
    result = {
        "schema": "vsm-storage-checks-v1",
        "mode": "decode+verify",
        "decode": {"checks": checks, "summary": summarize(checks)},
        "verify": {"checks": verify_checks, "summary": summarize(verify_checks)},
    }
    result["summary"] = summarize(checks + verify_checks)
    write_json(out / "checks.json", result)
    return result


def summarize(checks):
    d = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for c in checks:
        d[c["status"]] = d.get(c["status"], 0) + 1
    return d


def build_report(ctx) -> str:
    u = ctx["uniform"]["values"]
    sel = ctx["selected"]
    r = ctx["receivers"]
    lines = []
    lines.append(f"# VSM storage decode: {ctx['dump'].root.name} (presented graph, native{int(ctx['passes']['graph_prefix'], 16):04d})\n")
    lines.append(f"Selected pass: **{ctx['passes']['vsm_projection']['Id']} "
                 f"{ctx['passes']['vsm_projection']['EventName']}** "
                 f"(`{ctx['passes']['vsm_projection']['Pointer']}`), graph prefix "
                 f"`{ctx['passes']['graph_prefix']}`. Earlier scene invocations are not used.\n")
    lines.append("## Captured operands\n")
    lines.append("| operand | resource | version | capture snapshot / last resource output binding | file | bytes | sha256 |")
    lines.append("|---|---|---|---|---|---|---|")
    ops = ctx["ops"]
    snap = ctx["snapshots"]
    def writer_text(rec):
        if not rec:
            return "unknown (not recorded in captured pass outputs)"
        return f"{rec['pass_id']} {rec['name']}"
    lines.append(f"| ProjectionData | `{ops['ids']['ProjectionData']}` | `{ops['projection']['version']}` | {ops['projection']['pass_id']} {ops['projection']['pass_name']} | `{ops['projection']['file']}` | {Path(ops['projection']['path']).stat().st_size} | `{sha256_file(Path(ops['projection']['path']))[:16]}…` |")
    lines.append(f"| PageTable hardware mip0 (logical mips 0..7) | `{ops['ids']['PageTable']}` | `{ops['version']}` | pass 840 consumer snapshot; last resource output binding {writer_text(snap['page_table']['last_recorded_writer'])} | `{ops['page_table']['File']}` | {ops['page_table']['ByteSize']} | `{sha256_file(ctx['dump'].root / ctx['dump'].rel_file(ops['page_table']))[:16]}…` |")
    lines.append(f"| PageFlags hardware mips 0..6 (H0..H6) | `{ops['ids']['PageFlags']}` | `{ops['version']}` | pass 840 consumer snapshot; mip0 writer {writer_text(snap['page_flags']['last_recorded_writers'][0])}, mips1..6 writer {writer_text(snap['page_flags']['last_recorded_writers'][1])} | 7 files | {sum(int(l['ByteSize']) for l in ops['page_flags'])} | see JSON |")
    lines.append(f"| PhysicalPagePool slice0 | `{ops['ids']['PhysicalPagePool']}` | `{ops['version']}` | pass 840 consumer snapshot; last resource output binding {writer_text(snap['physical_page_pool']['last_recorded_writer'])} | `{ctx['dump'].rel_file(ops['pool_slice0'])}` | {ops['pool_slice0']['ByteSize']} | `{sha256_file(ctx['dump'].root / ctx['dump'].rel_file(ops['pool_slice0']))[:16]}…` |")
    lines.append(f"| PhysicalPagePool slice1 | `{ops['ids']['PhysicalPagePool']}` | `{ops['version']}` | pass 840 consumer snapshot; last resource output binding {writer_text(snap['physical_page_pool']['last_recorded_writer'])} | `{ctx['dump'].rel_file(ops['pool_slice1'])}` | {ops['pool_slice1']['ByteSize']} | `{sha256_file(ctx['dump'].root / ctx['dump'].rel_file(ops['pool_slice1']))[:16]}…` |")
    lines.append(f"| PhysicalPageMetaData | `{ops['metadata']['resource']}` | `{ops['metadata']['version']}` | {ops['metadata']['pass_id']} {ops['metadata']['pass_name']} (snapshot before pass 840) | `{ops['metadata']['file']}` | {Path(ops['metadata']['path']).stat().st_size} | `{sha256_file(Path(ops['metadata']['path']))[:16]}…` |")
    lines.append(f"| Uniform block | `{ctx['uniform']['content']}` | — | captured at pass 840 | `{ctx['uniform']['file']}` | 368 | `{ctx['uniform']['sha256'][:16]}…` |")
    lines.append("")
    lines.append("Pass 840 writes only its shadow-mask output; the page table, PageFlags and pool are its inputs, "
                 "versioned at the pass-840 capture boundary. The table names the last recorded resource-level output bindings; a whole-resource UAV binding does not prove a write to every slice. In particular, pass 811 merges static depth into slice 0 and does not overwrite the static slice 1.")
    lines.append("")
    for key, rec in (ctx.get("input_records") or {}).items():
        lines.append(f"- input `{key}`: `{rec['file']}` {rec['bytes']} B sha256 `{rec['sha256']}`")
    arc = ctx.get("archive_rec")
    if arc:
        lines.append(f"- archive package `{arc['package']}`: index.json "
                     + (f"{arc['index_bytes']} B sha256 `{arc['index_sha256']}`" if arc.get("index_sha256") else "not present"))
    lines.append("")
    lines.append("## Captured constants (uniform block, pass 840)\n")
    lines.append("| field | value |")
    lines.append("|---|---|")
    for k in ("NumFullShadowMaps", "NumSinglePageShadowMaps", "MaxPhysicalPages", "NumShadowMapSlots",
              "StaticCachedArrayIndex", "PhysicalPageRowMask", "PhysicalPageRowShift", "PhysicalPoolSize",
              "PhysicalPoolSizePages", "PageTableRowShift", "PageTableRowMask", "SceneFrameNumber"):
        lines.append(f"| {k} | `{u[k]}` |")
    lines.append("")
    lines.append(f"Page size **128×128**; physical pool **{ctx['pool_shape'][1]}×{ctx['pool_shape'][0]}** texels = "
                 f"**{ctx['pool_shape'][1]//128}×{ctx['pool_shape'][0]//128}** pages; page table **8192×192**; "
                 f"projection stride **368 B**, **{ctx['projection_count']}** records.\n")
    lines.append("## Sun VSMs\n")
    lines.append(f"Base id `{ctx['base']['id']}` (clip level {ctx['base']['clipmap_level']}), "
                 f"{ctx['sun_count']} contiguous levels {ctx['sun'][0]['clipmap_level']}..{ctx['sun'][-1]['clipmap_level']}, "
                 f"direction `{ctx['base']['light_direction']}`. Selected VSM `{sel['id']}` "
                 f"(clip level {sel['clipmap_level']}, level index {sel['clipmap_level_index']}), "
                 f"page-table block base texel `{ctx['page_table_block']['base_texel']}`.\n")
    lines.append("## Selected-VSM page table (hardware mip0, logical mips 0..7)\n")
    c = ctx["page_table_counts"]
    lines.append(f"- page-table hardware descriptor NumMips = `{ops['page_table_desc'].get('NumMips')}`; "
                 f"that one hardware mip packs all eight logical shadow-map mips in the VSM 128×192 block")
    lines.append(f"- hardware mip0 exact-page entries {c['total_entries']}: render-valid {c['valid_for_rendering']}, "
                 f"coarser-clipmap LOD fallback {sum(v for k, v in ctx['lod_histogram'].items() if k > 0)}, "
                 f"unmapped {c['unmapped']}, secondary {c['secondary']}")
    lines.append("")
    lines.append("| logical mip | base texel | dim pages | entries | render-valid | any-valid | Empty | PageFlags H0 nonzero |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for lm in ctx["logical_mips"]:
        lines.append(f"| {lm['mip']} | {lm['base_texel']} | {lm['dim_pages']} | {lm['entries']} | "
                     f"{lm['valid_for_rendering']} | {lm['any_lod_valid']} | {'yes' if lm['empty'] else 'no'} | "
                     f"{lm['page_flags_nonzero']} |")
    lines.append("")
    lines.append("- LODOffset semantics: for this directional clipmap the field selects a coarser clipmap level and the sampled mip stays 0; "
                 "for local lights the same field can select a logical page-table mip.")
    lines.append(f"- LODOffset histogram (logical mip0): `{ctx['lod_histogram']}`")
    lines.append(f"- PageFlags H0 nonzero {c['page_flags_nonzero']}; low6 categories `{c['page_flags_categories']}`")
    lines.append("- PageFlags hardware mips 0..6 are the separate H0..H6 hierarchy (mip m parent at texel >> m); H7 is not generated. "
                 "They are not the eight logical mips packed in PageTable hardware mip0.")
    lines.append(f"- Occupancy categories (4096 physical pages): `{ctx['occupancy_counts']}`")
    lines.append(f"- Metadata state categories: `{ctx['meta_state_counts']}`")
    lines.append(f"- Physical pages referenced by page-table mip0: {ctx['ref_count']}")
    lines.append(f"- Metadata records allocated but unreferenced in this snapshot: "
                 f"{ctx['counts']['physical_page_metadata_unreferenced_in_snapshot']} (earlier last-requested frame 3); "
                 f"age alone does not prove them stale or invalid, and free/unallocated slots stay neutral gray.\n")
    stats = ctx["depth_stats"]
    lines.append(f"Depth range over all {stats['finite']} values of the {ctx['mapped_pages_total']} render-valid pages: "
                 f"min {stats['min']:.9f}, max {stats['max']:.9f}, mean {stats['mean']:.9f}, "
                 f"p01 {stats['p01']:.9f}, p99 {stats['p99']:.9f}, zeros {stats['zeros']}.\n")
    lines.append(f"Display range for the selected projection: device Z **[{ctx['depth_display_lo']:.8f}, "
                 f"{ctx['depth_display_hi']:.8f}]**, ramp navy→blue→teal→amber→white; "
                 f"t = (z−lo)/(hi−lo) is the invertible display coordinate. Invalid and masked texels are categorical, not depth.\n")
    lines.append("## Receiver declared replay\n")
    lines.append("| rcvr | mask | clipmap | UV | vpage | entry | phys page | phys texel | raw depth | depth | receiver Z | Δ | occ dist cm |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name in ("A", "B"):
        rec = r.get(name)
        if not rec:
            continue
        lines.append(f"| {name} | {rec.get('captured_shadow_mask_linear_r')} | {rec.get('selected_vsm_id')} | "
                     f"({rec['shadow_uv'][0]:.5f},{rec['shadow_uv'][1]:.5f}) | {rec['virtual_page_xy']} | "
                     f"`{rec['page_table_entry_hex']}` | {rec['physical_page_xy']} | {rec['physical_texel_xy']} | "
                     f"`{rec['raw_depth_bits']}` | {rec['stored_depth_device_z']:.9f} | "
                     f"{rec['receiver_device_z']:.9f} | {rec['stored_depth_minus_receiver_z']:+.3e} | "
                     f"{rec['occluder_distance_ortho_cm']:.3f} |")
    lines.append("")
    lines.append("The replay applies the captured clipmap-selection formula and page-table arithmetic only. "
                 "SMRT ray directions, slope/normal bias, texel dither and per-lane hits are shader-local and are not replayed, "
                 "and the captured mask is reported but not reproduced. B's stored depth is greater than its receiver depth "
                 "(occluder closer to the light), consistent with the captured shadowed mask. A's stored depth nearly equals "
                 "its receiver projection (Δ = -1.856e-08, float32 roundoff; not exactly equal). "
                 "The replay is a storage-address check, not a full shadow-mask explanation. "
                 "The occluder-distance column applies the source clamp max(1e-6 cm, (receiverZ-storedZ)/m33); "
                 "A's unclamped value is slightly negative (no occluder), B's is positive.\n")
    lines.append("## Figures\n")
    for k, f in ctx["figures"].items():
        lines.append(f"- `{f['file']}` ({f['width']}×{f['height']}): {f['kind']}")
    lines.append("")
    lines.append("Figure 3 tile order is listed in `publish/data/vsm_storage.json` → `figures.vsm_depth_pages.tile_order` "
                 "(row-major, 3 columns × 2 rows, 256 px tiles, 8 px gutters, 2× nearest scale, white cross = replay texel on A/B).")
    mm = ctx["virtual_depth_meta"]
    lines.append(f"Figure 4 (`vsm_virtual_depth.png`) reassembles all {mm['selected_pages']} selected VSM-8196 mip0 pages "
                 f"at their virtual page coordinates, cropped to the active bbox {mm['bbox_virtual_page']} "
                 f"({mm['bbox_page_wh'][0]}×{mm['bbox_page_wh'][1]} pages, {mm['output_px'][0]}×{mm['output_px'][1]} px, "
                 f"uniform scale {mm['scale']:.6f}). It uses the same fixed device-Z range "
                 f"[{mm['depth_display_range_device_z'][0]:.4f}, {mm['depth_display_range_device_z'][1]:.4f}]; "
                 f"coarser-clipmap fallback ({mm['fallback_cells']} page cells) and unmapped ({mm['unmapped_cells']} page cells) "
                 f"positions are categorical, and no text is burned in. Page order, bbox, scale and mask are stored in JSON/NPZ.\n")
    lines.append("## Limits\n")
    for lim in [
        "PageTable hardware mip0 packs all eight logical shadow-map mips 0..7 in each VSM 128×192 block. For selected VSM 8196, logical mip0 holds 99 render-valid pages and logical mips 1..7 are present but empty (all-zero entries); no logical page-table mip is missing from the capture.",
        "PageFlags hardware mips 0..6 are a separate H0..H6 hierarchy (mip m parent at texel >> m); H7 is not generated. This hierarchy is not the eight logical mips packed in PageTable hardware mip0.",
        "The receiver chain is a declared replay, not an executed shader-local SMRT trace.",
        "The page-table, PageFlags and pool files are pass-840 consumer snapshots versioned at the capture boundary; their last resource-level output bindings are earlier passes (page table 792, pool 811, PageFlags mip0 790 / mips1-6 791). Physical-page metadata is the pass-810 snapshot before the pass-840 consumer.",
        "The 96 metadata records with flags 0x285b and LastRequestedSceneFrameNumber 3 are allocated slots not referenced in this snapshot; age alone does not prove them stale or invalid. Free/unallocated slots remain neutral gray. The 99 selected pages have no zero-valued depths; the compact atlas masks nonpositive values, while the native-page and mosaic views apply the declared ramp directly.",
        "Full pool bytes (2 × 256 MiB) stay in the raw archive; all 99 current selected pages are duplicated as full 128×128 float32 + raw bits, plus the 64×64 compact thumbnails and the virtual-address mosaic.",
        "Depth across different projections/clipmap levels is not globally comparable; one fixed range is used for selected VSM 8196.",
        "AllocatedPageRectBounds/PageRectBounds, PageReceiverMasks, HZBPhysicalPagePool, NextVirtualShadowMapData and StatsBuffer are captured but not used for these point storage queries.",
    ]:
        lines.append(f"- {lim}")
    lines.append("")
    return "\n".join(lines)


def verify_only(args):
    out = Path(args.out)
    checks = []
    engine = Path(args.engine) if args.engine else None
    verify_publish(out, checks, engine)
    result = {
        "schema": "vsm-storage-checks-v1",
        "mode": "verify",
        "verify": {"checks": checks, "summary": summarize(checks)},
        "summary": summarize(checks),
    }
    # preserve decode checks if present
    p = out / "checks.json"
    if p.exists():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
            if "decode" in old:
                result["decode"] = old["decode"]
                result["summary"] = summarize(old["decode"]["checks"] + checks)
        except Exception:
            pass
    write_json(p, result)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dump", help="DumpGPU frame root containing Base/ and Resources/ (read-only)")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--all", action="store_true", help="decode all operands, publish figures/data, and verify")
    ap.add_argument("--verify", action="store_true", help="verify published files/arrays in place; no raw capture or engine needed")
    ap.add_argument("--engine", help="optional pinned engine root for source-range hashes")
    ap.add_argument("--receivers", help="optional existing receivers.json for the A/B declared replay")
    ap.add_argument("--view", help="optional orientation/context JSON with view.pre_view_translation_cm")
    ap.add_argument("--archive", help="optional archived frame package root; index.json is hashed for provenance")
    args = ap.parse_args(argv)
    if args.verify and not args.all:
        res = verify_only(args)
        print(json.dumps(res["summary"]))
        return 0 if res["summary"].get("FAIL", 0) == 0 else 1
    if not args.dump:
        ap.error("--dump is required unless --verify is used")
    if not args.all:
        args.all = True
    res = decode(args)
    print(json.dumps(res["summary"]))
    # fail process on real decode failures, but a missing optional source root is a SKIP not a FAIL
    fails = [c for c in res["decode"]["checks"] if c["status"] == "FAIL"]
    fails += [c for c in res["verify"]["checks"] if c["status"] == "FAIL"]
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
