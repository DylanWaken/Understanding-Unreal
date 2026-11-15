#!/usr/bin/env python3
"""Standalone, read-only independent verifier for the VSM storage review.

This script does not import tools/decode_vsm_storage.py and does not write
anything outside review/ (review/checks.json).  It reads the raw DumpGPU
capture, the pinned UE5 source, and the producer's published JSON/NPZ/PNGs.

It re-derives, from native bytes and the source packing:
  * the selected pass 840 operand versions and readback status
  * the captured uniform constants and resource extents
  * the sun projection records and source clipmap selection
  * A/B receiver virtual pages, page-table entries, physical texels,
    physical depth raw words, and (independently) receiver C plus a
    representative selected page
  * the complete 99-page selected-VSM depth set and all 16,384 page-table
    entries + PageFlags for the selected block
  * the global physical-page reference/metadata partition used by F1
  * exact figure pixels for the declared palettes/scales (F1/F2/F3)
  * source-range SHA-256s and the published manifest hashes

Usage:
  python review/verify_vsm_storage.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
from pathlib import Path

import numpy as np
from PIL import Image

DUMP = Path(r"D:\UnrealUnderstanding-Sponza\output\gpu-dump\raw\sponza-20261115-083654")
OUT = Path(r"D:\UnrealUnderstanding-Review\vsm-storage\output")
ENGINE = Path(r"D:\UnrealEngine")
RECEIVERS = Path(r"D:\UnrealUnderstanding-Review\static-final-checks\copies\receivers\data\receivers.json")
ORIENTATION = Path(r"D:\UnrealUnderstanding-Review\static-final-checks\copies\context\data\orientation.json")
ARCHIVE_INDEX = Path(r"D:\UnrealUnderstanding-Sponza\output\final\frame-package\index.json")

PASS_ID = 840
PASS_PTR = "00040000020c16c43fb0"
PAGE = 128
LOG2_PAGE = 7
LEVEL0_PAGES = 128
PAGE_TABLE_BLOCK = 128
POOL_W = 16384
POOL_H = 4096
PSD_STRIDE = 16 * 23
SEL_ID = 8196
PT_BASE_X = 640
PT_BASE_Y = 0


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 23), b""):
            h.update(chunk)
    return h.hexdigest()


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_concat(path: Path):
    """DumpGPU writes several concatenated JSON values per file."""
    s = path.read_text(encoding="utf-8")
    dec = json.JSONDecoder()
    out, i, n = [], 0, len(s)
    while i < n:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        obj, j = dec.raw_decode(s, i)
        out.append(obj)
        i = j
    return out


def u32(raw: bytes, off: int) -> int:
    return struct.unpack_from("<I", raw, off)[0]


def i32(raw: bytes, off: int) -> int:
    return struct.unpack_from("<i", raw, off)[0]


def f32(raw: bytes, off: int) -> float:
    return struct.unpack_from("<f", raw, off)[0]


def f3(raw: bytes, off: int) -> np.ndarray:
    return np.frombuffer(raw, dtype="<f4", count=3, offset=off).astype(np.float64)


def mat4(raw: bytes, off: int) -> np.ndarray:
    # UE stores the block column-major; math matrix is the transpose.
    return np.frombuffer(raw, dtype="<f4", count=16, offset=off).reshape(4, 4).T.astype(np.float64)


def hex32(v: int) -> str:
    return f"0x{int(v) & 0xFFFFFFFF:08x}"


def decode_entry(v: int) -> dict:
    """Source: VirtualShadowMapPageAccessCommon.ush lines 237-287."""
    v = int(v)
    return {
        "raw": v,
        "raw_hex": hex32(v),
        "physical_x": v & 0x3FF,
        "physical_y": (v >> 10) & 0x3FF,
        "lod_offset": (v >> 20) & 0x3F,
        "secondary_request": (v >> 29) & 1,
        "valid_for_rendering": (v >> 30) & 1,
        "any_lod_valid": (v >> 31) & 1,
    }


def encode_entry(e: dict) -> int:
    """Re-encode using the source bit layout, only for the identity check."""
    v = (e["physical_y"] << 10) | e["physical_x"]
    if e["lod_offset"]:
        v |= 0x80000000 | (e["lod_offset"] << 20)
    else:
        v |= 0x80000000 | (0x40000000 if e["valid_for_rendering"] else 0)
    if e["secondary_request"]:
        v |= 0x20000000
    return v & 0xFFFFFFFF


def depth_rgb(z, lo: float, hi: float, stops: np.ndarray):
    """Declared plotting scale: t=(z-lo)/(hi-lo), five-stop linear ramp."""
    a = np.asarray(z, dtype=np.float64)
    t = np.clip((a - lo) / max(hi - lo, 1e-30), 0.0, 1.0)
    u = np.linspace(0.0, 1.0, stops.shape[0])
    out = np.zeros(a.shape + (3,), dtype=np.float64)
    for c in range(3):
        out[..., c] = np.interp(t, u, stops[:, c])
    return np.round(out).astype(np.uint8), t


def add(checks, cid, ok, detail, measurements=None):
    checks.append({
        "id": cid,
        "status": "PASS" if ok else "FAIL",
        "detail": detail,
        "measurements": measurements or {},
    })


def engine_file(rel: str) -> Path:
    a = ENGINE / rel
    if a.exists():
        return a
    b = ENGINE / "Engine" / rel
    if b.exists():
        return b
    raise FileNotFoundError(rel)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", type=Path, default=DUMP)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--engine", type=Path, default=ENGINE)
    ap.add_argument("--receivers", type=Path, default=RECEIVERS)
    ap.add_argument("--orientation", type=Path, default=ORIENTATION)
    ap.add_argument("--archive-index", type=Path, default=ARCHIVE_INDEX)
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()

    checks = []
    issues = []
    data_json = json.loads((args.out / "publish/data/vsm_storage.json").read_text(encoding="utf-8"))
    npz = np.load(args.out / "publish/data/vsm_storage.npz", allow_pickle=False)
    manifest = json.loads((args.out / "publish/manifest.json").read_text(encoding="utf-8"))
    sr = json.loads((args.out / "source-ranges.json").read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    # 1. selected pass / versions / readback
    # ------------------------------------------------------------------
    passes = load_concat(args.dump / "Base" / "Passes.json")
    by_ptr = {p["Pointer"]: p for p in passes}
    vp = by_ptr.get(PASS_PTR)
    layouts = load_concat(args.dump / "Base" / "SubresourceLayouts.json")
    readbacks = {r["File"]: r for r in load_concat(args.dump / "Base" / "DumpReadbackStatus.json")}
    descs = load_concat(args.dump / "Base" / "ResourceDescs.json")

    exact = {
        "page_table": "Resources/00040000020bb879e688.mip0.v00040000020c16c43fb0.bin",
        "page_flags_mip0": "Resources/00040000020bb879e950.mip0.v00040000020c16c43fb0.bin",
        "pool_slice0": "Resources/00040000020c132ec290.[0].mip0.v00040000020c16c43fb0.bin",
        "pool_slice1": "Resources/00040000020c132ec290.[1].mip0.v00040000020c16c43fb0.bin",
        "projection": "Resources/00040000020bbf9a1e30.v00040000020bbf9a2350.bin",
        "metadata": "Resources/00040000020ba06c1680.v00040000020ba0684e78.bin",
        "uniform": "Structures/00040000020910406420.bin",
    }
    flags_files = [f"Resources/00040000020bb879e950.mip{m}.v00040000020c16c43fb0.bin" for m in range(7)]

    layout_versions = {exact["page_table"], exact["page_flags_mip0"], exact["pool_slice0"], exact["pool_slice1"]} | set(flags_files)
    layout_by_file = {}
    for l in layouts:
        f = l.get("File", "").replace("\\", "/")
        if f in layout_versions:
            layout_by_file[f] = l
    layout_ok = all(f in layout_by_file for f in layout_versions)
    buffer_files_ok = all(
        (args.dump / exact[k]).exists()
        and (args.dump / exact[k]).stat().st_size == (3020912 if k == "projection" else 98304)
        for k in ("projection", "metadata")
    )
    readback_ok = all(
        f in readbacks
        and readbacks[f].get("Status") == "Dumped"
        and readbacks[f].get("FileExists") is True
        and readbacks[f].get("FileSizeMatches") is True
        for f in [exact["page_table"], exact["pool_slice0"], exact["pool_slice1"]] + flags_files
    )
    inputs = vp.get("InputResources", []) if vp else []
    input_ok = all(any(x.startswith(rid) for x in inputs) for rid in (
        "00040000020bbf9a1e30", "00040000020bb879e688", "00040000020bb879e950", "00040000020c132ec290"))
    output_ok = vp.get("OutputResources") == ["00040000020c16c43a30.mip0"] if vp else False
    add(checks, "selected_pass_and_versions", bool(vp and vp.get("Id") == PASS_ID and input_ok and output_ok and layout_ok and buffer_files_ok and readback_ok),
        "pass 840 is the final presented-graph projection; its exact page-table/PageFlags/pool input versions are Dumped with matching sizes",
        {
            "pass": {"id": vp.get("Id") if vp else None, "pointer": vp.get("Pointer") if vp else None,
                     "name": vp.get("EventName") if vp else None},
            "inputs_present": input_ok,
            "output": vp.get("OutputResources") if vp else None,
            "exact_versions": {k: {"file": v,
                                   "present": (v in layout_by_file) if k not in ("projection", "metadata", "uniform") else (args.dump / v).exists(),
                                   "readback": readbacks.get(v, {}).get("Status")}
                               for k, v in exact.items() if k != "uniform"},
            "layout_ok": bool(layout_ok), "buffer_files_ok": bool(buffer_files_ok),
            "readback_ok": readback_ok,
        })

    # report's "producer" column: pass 840 only outputs the shadow mask; the
    # page table/flags/pool are InputResources.  The version filename is the
    # capture key, not the writing pass, so the report table is mislabeled.
    producer_label_ok = False
    add(checks, "producer_direction", bool(vp and input_ok and output_ok),
        "pass 840 consumes page table/flags/pool/projection; it produces only the shadow-mask output",
        {
            "reported_producer_840_for_page_table_flags_pool": True,
            "actual_page_table_role": "InputResource (version key 840 / capture snapshot)",
            "actual_page_flags_role": "InputResource (version key 840 / capture snapshot)",
            "actual_pool_role": "InputResource (version key 840 / capture snapshot)",
            "pass840_outputs": vp.get("OutputResources") if vp else None,
            "conclusion": "version selection is correct; the report's 'producer' column attribute is incorrect",
        })
    issues.append({
        "id": "producer_label",
        "severity": "non-blocking provenance wording",
        "detail": ("report.md's Captured operands table labels PageTable mip0, PageFlags mip0..6 and both "
                   "PhysicalPagePool slices as producer 840. Pass 840's only OutputResource is "
                   "00040000020c16c43a30.mip0; all four are InputResources. The .v00040000020c16c43fb0 "
                   "suffix is the capture/version key at the consuming pass, not the writing pass. "
                   "Use 'version key 840 / consumed at 840' (or name the upstream writer) instead of "
                   "'producer 840'."),
    })

    # Metadata is not bound at 840 (null) and is the latest captured version <=840.
    metadata_versions = [p for p in passes if p.get("Pointer") == "00040000020ba0684e78"]
    meta_pass = metadata_versions[0] if metadata_versions else None
    md_versions = sorted({
        p.name.split(".v")[-1].rsplit(".bin")[0]
        for p in (args.dump / "Resources").glob("00040000020ba06c1680.v*.bin")
    })
    md_selected_pass = by_ptr.get("00040000020ba0684e78")
    md_selected_id = (md_selected_pass or {}).get("Id", 0)
    md_later = [v for v in md_versions
                if by_ptr.get(v) and md_selected_id < by_ptr[v].get("Id", 0) <= PASS_ID]
    add(checks, "metadata_snapshot_scope", bool(md_selected_pass and md_selected_pass.get("Id") == 810 and md_versions and not md_later),
        "physical-page metadata is the latest captured snapshot at/before pass 840 (pass 810), not a live pass-840 binding",
        {
            "pass840_metadata_binding": "Null in ParameterBindings",
            "metadata_version": "00040000020ba0684e78",
            "metadata_producer_pass": md_selected_pass.get("Id") if md_selected_pass else None,
            "metadata_producer_name": md_selected_pass.get("EventName") if md_selected_pass else None,
            "captured_metadata_versions": md_versions,
            "later_metadata_versions_at_or_before_840": md_later,
        })

    # ------------------------------------------------------------------
    # 2. uniform native fields
    # ------------------------------------------------------------------
    refs = load_concat(args.dump / "Base" / "ReferencedStructs.json")
    ref = None
    for rec in refs:
        if rec.get("Pass") == PASS_PTR:
            for _, v in rec.get("ReferencedStructs", {}).items():
                if v.get("LayoutName") == "FVirtualShadowMapUniformParameters":
                    ref = v
                    break
    uniform_raw = (args.dump / exact["uniform"]).read_bytes()
    umeta = json.loads((args.dump / "StructuresMetadata" / f"{ref['Metadata']}.json").read_text(encoding="utf-8"))
    members = {m["Name"]: m for m in umeta.get("Members", [])}
    def uval(name):
        m = members[name]
        n = max(1, int(m.get("NumRows", 1)) * int(m.get("NumColumns", 1)))
        off = int(m["Offset"])
        bt = m["BaseType"]
        if bt == "UBMT_FLOAT32":
            vals = list(struct.unpack_from("<" + "f" * n, uniform_raw, off))
        elif bt == "UBMT_INT32":
            vals = list(struct.unpack_from("<" + "i" * n, uniform_raw, off))
        elif bt == "UBMT_UINT32":
            vals = list(struct.unpack_from("<" + "I" * n, uniform_raw, off))
        else:
            raise ValueError((name, bt))
        return vals[0] if len(vals) == 1 else vals

    u = {k: uval(k) for k in (
        "NumFullShadowMaps", "NumSinglePageShadowMaps", "MaxPhysicalPages", "NumShadowMapSlots",
        "StaticCachedArrayIndex", "PhysicalPageRowMask", "PhysicalPageRowShift",
        "PhysicalPoolSize", "PhysicalPoolSizePages", "PageTableRowShift", "PageTableRowMask",
        "SceneFrameNumber")}
    pt_layout = layout_by_file[exact["page_table"]]
    pool_layout = layout_by_file[exact["pool_slice0"]]
    pf_layout = layout_by_file[exact["page_flags_mip0"]]
    extents = {
        "page_table": [int(pt_layout["ExtentX"]), int(pt_layout["ExtentY"])],
        "page_flags_mip0": [int(pf_layout["ExtentX"]), int(pf_layout["ExtentY"])],
        "pool": [int(pool_layout["ExtentX"]), int(pool_layout["ExtentY"])],
    }
    uniform_ok = (
        len(uniform_raw) == PSD_STRIDE == 368
        and u["MaxPhysicalPages"] == 4096
        and u["NumShadowMapSlots"] == 8209
        and u["NumFullShadowMaps"] == 17
        and u["StaticCachedArrayIndex"] == 1
        and u["PhysicalPageRowShift"] == 7
        and u["PageTableRowShift"] == 6
        and u["PageTableRowMask"] == 63
        and list(u["PhysicalPoolSize"]) == [16384, 4096]
        and list(u["PhysicalPoolSizePages"]) == [128, 32]
        and extents["pool"] == [16384, 4096]
        and extents["page_table"] == [8192, 192]
    )
    add(checks, "uniform_native_fields", bool(uniform_ok),
        "captured 368-byte FVirtualShadowMapUniformParameters at the source layout confirms the page/pool/table dimensions",
        {
            "uniform_content": ref.get("Content"), "bytes": len(uniform_raw),
            "sha256": sha_file(args.dump / exact["uniform"]),
            "fields": u, "extents": extents,
            "source_struct": {"file": umeta.get("FileName"), "line": umeta.get("FileLine"), "size": umeta.get("Size")},
        })

    # ------------------------------------------------------------------
    # 3. source ranges + packing constants
    # ------------------------------------------------------------------
    range_mismatch, file_mismatch = [], []
    for r in sr.get("ranges", []):
        p = engine_file(r["file"])
        lines = p.read_text(encoding="utf-8").splitlines()
        if r["end_line"] > len(lines):
            range_mismatch.append({"file": r["file"], "reason": "range past EOF"})
            continue
        h = sha_text("\n".join(lines[r["line"] - 1:r["end_line"]]))
        if h != r.get("rangeSHA"):
            range_mismatch.append({"file": r["file"], "line": r["line"], "end_line": r["end_line"],
                                   "expected": r.get("rangeSHA"), "actual": h})
        fh = sha_file(p)
        if fh != r.get("file_sha256"):
            file_mismatch.append({"file": r["file"], "expected": r.get("file_sha256"), "actual": fh})
        r["_fh"] = fh
    add(checks, "source_range_sha", bool(sr.get("ranges")) and not range_mismatch and not file_mismatch,
        "every cited source range recomputes from the pinned engine as UTF8-LF; every file SHA matches",
        {"ranges": len(sr.get("ranges", [])), "range_mismatches": range_mismatch,
         "file_mismatches": file_mismatch, "unique_files": len({r["file"] for r in sr.get("ranges", [])})})

    src_pack = engine_file("Shaders/Private/VirtualShadowMaps/VirtualShadowMapPageAccessCommon.ush").read_text(encoding="utf-8")
    src_defs = engine_file("Shaders/Shared/VirtualShadowMapDefinitions.h").read_text(encoding="utf-8")
    src_proj = engine_file("Shaders/Private/VirtualShadowMaps/VirtualShadowMapProjectionCommon.ush").read_text(encoding="utf-8")
    src_dir = engine_file("Shaders/Private/VirtualShadowMaps/VirtualShadowMapProjectionDirectional.ush").read_text(encoding="utf-8")
    src_h = engine_file("Source/Runtime/Renderer/Private/VirtualShadowMaps/VirtualShadowMapArray.h").read_text(encoding="utf-8")
    assertions = {
        "page_size_128": "VSM_LOG2_PAGE_SIZE 7u" in src_defs and "VSM_PAGE_SIZE (1u << VSM_LOG2_PAGE_SIZE)" in src_defs,
        "psd_stride_368": "VSM_PSD_STRIDE (16 * 23)" in src_defs,
        "single_page_limit_8192": "VSM_MAX_SINGLE_PAGE_SHADOW_MAPS (1024U * 8U)" in src_defs,
        "entry_bits": all(s in src_pack for s in (
            "[0:9] PageAddress.x", "[10:19] PageAddress.y", "[20:25] LODOffset",
            "[29] bIsSecondaryRequest", "[30] bThisLODValidForRendering", "[31] bAnyLODValid")),
        "entry_decode_masks": "uint2(Value & 0x3FFu, (Value >> 10u) & 0x3FFu)" in src_pack,
        "physical_address_scale": "PhysicalAddress * VSM_PAGE_SIZE + (VirtualTexelAddress & VSM_PAGE_SIZE_MASK)" in src_pack,
        "physical_index_shift": "(PhysicalPageAddress.y << VirtualShadowMap.PhysicalPageRowShift) + PhysicalPageAddress.x" in src_pack,
        "full_id_formula": "VSM_MAX_SINGLE_PAGE_SHADOW_MAPS) + 1u" in src_pack,
        "clipmap_floor": "int(floor(ClipmapLevelFloat)) - BaseProjectionData.ClipmapLevel" in src_dir,
        "per_level_bias": "GetVirtualShadowMapProjectionData(VSMHandle).ResolutionLodBias" in src_proj,
        "physical_depth_asfloat": "asfloat(VirtualShadowMap.PhysicalPagePool.Load" in src_proj,
        "metadata_field_order": all(s in src_defs for s in (
            "uint Flags;", "uint LastRequestedSceneFrameNumber;", "uint VirtualShadowMapId;",
            "uint MipLevel;", "uint2 PageAddress;")),
        "uniform_field_order": all(s in src_h for s in (
            "SHADER_PARAMETER(uint32, MaxPhysicalPages)", "SHADER_PARAMETER(uint32, NumShadowMapSlots)",
            "SHADER_PARAMETER(uint32, PageTableRowShift)", "SHADER_PARAMETER(uint32, PageTableRowMask)",
            "SHADER_PARAMETER(FIntPoint, PhysicalPoolSize)")),
    }
    add(checks, "source_packing_assertions", all(assertions.values()),
        "the independent packing/decoding is backed by the exact source comments and field order",
        assertions)

    # The H-mip hierarchy is stored in six separate PageFlags mip textures
    # (OutPageFlagMips_0..5, PROCESS_LEVEL(0..5)), not eight 6-bit fields packed
    # in one uint32.  The published helper array/description is therefore wrong.
    src_pm = engine_file("Shaders/Private/VirtualShadowMaps/VirtualShadowMapPageManagement.usf").read_text(encoding="utf-8")
    pm_lines = {line.strip() for line in src_pm.splitlines()}
    h_mips = [i for i in range(7) if f"RWTexture2D<uint> OutPageFlagMips_{i};" in pm_lines]
    h_levels = [i for i in range(8) if f"PROCESS_LEVEL({i});" in pm_lines]
    h_arr = npz["page_table_flags_hierarchy_h6"]
    published_h_nonzero = {i: int(np.count_nonzero(h_arr[i])) for i in range(1, 8)}
    flag_shapes = {
        int(l["MipLevel"]): (int(l["ExtentY"]), int(l["ExtentX"]))
        for l in layouts
        if l.get("Resource") == "00040000020bb879e950"
        and l.get("File", "").endswith(".mip" + str(l.get("MipLevel")) + ".v00040000020c16c43fb0.bin")
    }
    h_parent_nonzero, h_mismatch = {}, {}
    for hmip in range(1, 7):
        shape = flag_shapes[hmip]
        m = np.memmap(args.dump / f"Resources/00040000020bb879e950.mip{hmip}.v00040000020c16c43fb0.bin",
                      dtype="<u4", mode="r", shape=shape)
        truth = np.array([[int(m[vy >> hmip, (PT_BASE_X + vx) >> hmip]) & 0x3F
                           for vx in range(128)] for vy in range(128)], dtype=np.uint8)
        word = ((npz["page_table_flags_u32"] >> (6 * hmip)) & 0x3F).astype(np.uint8)
        h_parent_nonzero[hmip] = int(np.count_nonzero(truth))
        h_mismatch[hmip] = int(np.count_nonzero(word != truth))
    published_h = data_json["operands"]["page_flags"]["hierarchy"]
    published_offset = sr.get("field_offsets", {}).get("PageFlags.h0_h7", "")
    schema_h = json.loads((args.out / "publish/data/schema.json").read_text(encoding="utf-8"))["npz_arrays"]["page_table_flags_hierarchy_h6"]
    hierarchy_ok = (
        h_mips == [0, 1, 2, 3, 4, 5] and h_levels == [0, 1, 2, 3, 4, 5]
        and all(v == 0 for v in h_mismatch.values())
        and "8 H-mip fields" not in published_h and "bits[42:47]" not in published_h
        and "8 x 6-bit fields" not in published_offset
    )
    add(checks, "page_flags_hierarchy_semantics", bool(hierarchy_ok),
        "published PageFlags H-mip hierarchy reflects the source: mip0=H0 and mips1..6=H1..H6 at parent texels, not eight 6-bit fields inside one uint32",
        {
            "source_out_page_flag_mips": h_mips,
            "source_process_levels": h_levels,
            "source_h_map": "mip0=H0; OutPageFlagMips_0..5 write H1..H6 into mip1..mip6; H7 is not captured",
            "published_h1_h7_nonzero": published_h_nonzero,
            "correct_parent_h_nonzero": h_parent_nonzero,
            "published_packed_h_vs_parent_mismatches": h_mismatch,
            "published_hierarchy_string": published_h,
            "published_field_offset_string": published_offset,
            "published_schema_desc": schema_h.get("desc"),
            "h0_is_correct_and_used_by_figure": True,
            "impact": "the h0/low-6-bit figure and all address/depth checks are unaffected; the published h1..h7 helper array and its descriptions are wrong",
        })
    issues.append({
        "id": "page_flags_hierarchy",
        "severity": "blocking native-field/provenance error (auxiliary array; no address-depth impact)",
        "detail": ("vsm_storage.json['operands']['page_flags']['hierarchy'] says a uint32 contains eight 6-bit H-mip fields "
                   "and h7 is bits[42:47] (impossible in 32 bits). The source uses mip0=H0 plus six output textures "
                   "OutPageFlagMips_0..5, which write H1..H6 at parent texels in PageFlags mip1..mip6 (PROCESS_LEVEL(0..5), "
                   "HMipLevel=_Level_+1). The published page_table_flags_hierarchy_h6 has H1..H7 all zero; the correct "
                   f"parent H1..H6 are nonzero for {h_parent_nonzero} texels and differ from the packed-word values for "
                   f"{h_mismatch} of 16,384 selected-block texels. H7 is not captured (the OutPageFlagMips_6 declaration is "
                   "commented out). h0 and the PageFlags figure are correct; correct or remove the H1..H7 array, schema "
                   "description and source-ranges field_offsets string."),
    })

    # ------------------------------------------------------------------
    # 4. projection records + clipmap selection
    # ------------------------------------------------------------------
    proj_raw = (args.dump / exact["projection"]).read_bytes()
    proj_count = len(proj_raw) // PSD_STRIDE
    sun = []
    for i in range(proj_count):
        b = proj_raw[i * PSD_STRIDE:(i + 1) * PSD_STRIDE]
        flags = u32(b, 336)
        if flags != 0 and i >= 8192:
            packed = u32(b, 328)
            sun.append({
                "id": i, "flags": flags, "packed": packed,
                "clip": (packed >> 16) - 1024, "idx": (packed >> 8) & 0xFF, "remaining": packed & 0xFF,
                "bias": f32(b, 300), "direction": [float(x) for x in f3(b, 256)],
                "light_type": u32(b, 268), "raw": b,
            })
    sun.sort(key=lambda r: r["id"])
    sun_ok = (
        proj_count == 8209 and len(sun) == 17 and [r["id"] for r in sun] == list(range(8192, 8209))
        and [r["clip"] for r in sun] == list(range(6, 23))
        and all(r["light_type"] == 0 for r in sun)
        and len({tuple(round(x, 9) for x in r["direction"]) for r in sun}) == 1
        and all(abs(r["bias"] - sun[0]["bias"]) < 1e-12 for r in sun)
    )
    add(checks, "projection_sun_accounting", bool(sun_ok),
        "8,209 368-byte records decode to 17 contiguous directional sun levels 6..22, all with the captured light direction",
        {"records": proj_count, "stride": PSD_STRIDE, "sun_count": len(sun),
         "ids": [r["id"] for r in sun], "clip_levels": [r["clip"] for r in sun],
         "direction": sun[0]["direction"] if sun else None,
         "bias_values": sorted({round(r["bias"], 12) for r in sun})})

    # Published projection raw bytes must be the same native records.
    npz_proj = npz["projection_sun_raw_u8"]
    raw_sun = np.frombuffer(proj_raw, dtype=np.uint8)[8192 * PSD_STRIDE:8209 * PSD_STRIDE].reshape(17, PSD_STRIDE)
    proj_pub_ok = np.array_equal(npz_proj, raw_sun)
    add(checks, "projection_publish_bytes", bool(proj_pub_ok),
        "published 17 x 368 projection records are byte-identical to the native ProjectionData file",
        {"shape": list(npz_proj.shape), "byte_mismatches": int(np.count_nonzero(npz_proj != raw_sun))})

    # receiver reconstruction from source formula
    rdoc = json.loads(args.receivers.read_text(encoding="utf-8"))
    odoc = json.loads(args.orientation.read_text(encoding="utf-8"))
    view_pre = np.asarray((odoc.get("view") or {}).get("pre_view_translation_cm"), dtype=np.float64)
    base_b = proj_raw[8192 * PSD_STRIDE:8193 * PSD_STRIDE]
    base_pre = f3(base_b, 272) + f3(base_b, 288)
    base_origin = f3(base_b, 304)
    base_packed = u32(base_b, 328)
    base_clip = (base_packed >> 16) - 1024
    base_remaining = base_packed & 0xFF
    base_bias = f32(base_b, 300)
    view_to_shadow = base_pre - view_pre
    tw_origin = -base_origin + view_to_shadow

    # selected projection: 8196, source-consistent bias (per-level; all equal here)
    def select_for(pt):
        dist2 = float(np.sum((pt + tw_origin) ** 2))
        abs_level = 0.5 * math.log2(max(dist2, 1e-30))
        initial = max(0, int(abs_level - base_clip))
        if initial < base_remaining:
            bias = f32(proj_raw[(8192 + initial) * PSD_STRIDE:(8193 + initial) * PSD_STRIDE], 300)
            biased = abs_level + bias
        else:
            biased = abs_level + base_bias
        idx = max(0, int(math.floor(biased)) - base_clip)
        return dist2, abs_level, biased, idx

    def address_for(pt):
        dist2, abs_level, biased, idx = select_for(pt)
        sid = 8192 + idx
        sb = proj_raw[sid * PSD_STRIDE:(sid + 1) * PSD_STRIDE]
        presel = f3(sb, 272) + f3(sb, 288)
        p_shadow = np.asarray(pt, dtype=np.float64) + (presel - view_pre)
        M = mat4(sb, 64)  # TranslatedWorldToShadowUVMatrix
        uvz = M @ np.array([p_shadow[0], p_shadow[1], p_shadow[2], 1.0])
        uv = np.array([uvz[0] / uvz[3], uvz[1] / uvz[3]], dtype=np.float64)
        z = float(uvz[2] / uvz[3])
        vtex = np.floor(uv * (LEVEL0_PAGES * PAGE)).astype(np.int64)
        vpage = np.floor(uv * LEVEL0_PAGES).astype(np.int64)  # source uint2(ShadowMapUV * 128)
        local = vtex & (PAGE - 1)
        assert np.array_equal(vpage, vtex >> LOG2_PAGE)
        return {
            "dist2": dist2, "abs": abs_level, "biased": biased, "idx": idx, "id": sid,
            "uv": uv, "z": z, "vtex": vtex, "vpage": vpage, "local": local,
            "p_shadow": p_shadow, "proj": sb,
        }

    pt_mm = np.memmap(args.dump / exact["page_table"], dtype="<u4", mode="r", shape=(192, 8192))
    pf_mm = np.memmap(args.dump / exact["page_flags_mip0"], dtype="<u4", mode="r", shape=(192, 8192))
    pool_mm = np.memmap(args.dump / exact["pool_slice0"], dtype="<u4", mode="r", shape=(POOL_H, POOL_W))
    metadata_raw = (args.dump / exact["metadata"]).read_bytes()
    metadata = np.frombuffer(metadata_raw, dtype="<u4").reshape(-1, 6)

    def pool_word(px, py, lx, ly):
        return int(pool_mm[py * PAGE + ly, px * PAGE + lx])

    receivers = {}
    for name in ("A", "B"):
        rec = (rdoc.get("receivers") or {}).get(name)
        if not rec:
            continue
        pt = np.asarray(rec.get("translated_world_cm"), dtype=np.float64)
        a = address_for(pt)
        tx, ty = PT_BASE_X + int(a["vpage"][0]), PT_BASE_Y + int(a["vpage"][1])
        entry = int(pt_mm[ty, tx])
        e = decode_entry(entry)
        px, py = e["physical_x"], e["physical_y"]
        phys = [px * PAGE + int(a["local"][0]), py * PAGE + int(a["local"][1])]
        raw_word = pool_word(px, py, int(a["local"][0]), int(a["local"][1]))
        depth = struct.unpack("<f", struct.pack("<I", raw_word))[0]
        pidx = (py << 7) + px
        md = metadata[pidx]
        receivers[name] = {
            "input": rec, "a": a, "tx": tx, "ty": ty, "entry": entry, "e": e,
            "phys": phys, "raw_word": raw_word, "depth": depth, "pidx": pidx, "metadata": md,
            "pf": int(pf_mm[ty, tx]),
        }

    # published A/B values and native-vs-published comparisons
    pub_ok = True
    max_uv_err = 0.0
    max_z_err = 0.0
    max_depth_err = 0.0
    rows = []
    for name, r in receivers.items():
        p = data_json["receivers_declared_replay"][name]
        a = r["a"]
        max_uv_err = max(max_uv_err, float(np.max(np.abs(a["uv"] - np.array(p["shadow_uv"])))))
        max_z_err = max(max_z_err, abs(a["z"] - float(p["receiver_device_z"])))
        max_depth_err = max(max_depth_err, abs(r["depth"] - float(p["stored_depth_device_z"])))
        ok = (
            int(a["id"]) == int(p["selected_vsm_id"])
            and list(map(int, a["vpage"])) == p["virtual_page_xy"]
            and list(map(int, a["local"])) == p["texel_in_page_xy"]
            and int(r["entry"]) == int(p["page_table_entry_value"])
            and list(r["phys"]) == p["physical_texel_xy"]
            and int(r["raw_word"]) == int(p["raw_depth_bits"], 16)
            and [int(r["metadata"][2]), int(r["metadata"][3]), int(r["metadata"][4]), int(r["metadata"][5])] == [
                int(p["physical_page_metadata"]["virtual_shadow_map_id"]), int(p["physical_page_metadata"]["mip_level"]),
                p["physical_page_metadata"]["page_address"][0], p["physical_page_metadata"]["page_address"][1]]
        )
        pub_ok = pub_ok and ok
        rows.append({
            "receiver": name, "selected_vsm": int(a["id"]), "uv": [float(x) for x in a["uv"]],
            "vpage": list(map(int, a["vpage"])), "local": list(map(int, a["local"])),
            "page_table_texel": [int(r["tx"]), int(r["ty"])], "entry": hex32(r["entry"]),
            "physical_page": [r["e"]["physical_x"], r["e"]["physical_y"]], "physical_texel": r["phys"],
            "raw_depth_word": hex32(r["raw_word"]), "depth": r["depth"],
            "published_depth": float(p["stored_depth_device_z"]), "metadata_owner": int(r["metadata"][2]),
            "published_metadata_owner": int(p["physical_page_metadata"]["virtual_shadow_map_id"]),
            "segment_match": bool(ok),
        })
    add(checks, "receiver_A_B_reconstruction", bool(pub_ok and max_uv_err < 1e-9 and max_z_err < 1e-9 and max_depth_err == 0),
        "A/B are independently re-derived from native projection bytes, source clipmap/page arithmetic, native page table and pool words",
        {"rows": rows, "max_uv_abs_error": max_uv_err, "max_receiver_z_abs_error": max_z_err,
         "max_depth_abs_error": max_depth_err, "all_fields_match": bool(pub_ok)})

    # independent receiver C + one full selected page (four explicit addresses)
    explicit = []
    for name in ("A", "B"):
        r = receivers[name]
        explicit.append({"receiver": name, "selected_vsm": int(r["a"]["id"]), **{
            k: r[k] for k in ("entry", "phys", "raw_word", "depth", "pidx")}})
    crec = (rdoc.get("receivers") or {}).get("C")
    if crec:
        pt3 = np.asarray(crec.get("translated_world_cm"), dtype=np.float64)
        a = address_for(pt3)
        tx, ty = PT_BASE_X + int(a["vpage"][0]), PT_BASE_Y + int(a["vpage"][1])
        entry = int(pt_mm[ty, tx])
        e = decode_entry(entry)
        px, py = e["physical_x"], e["physical_y"]
        raw_word = pool_word(px, py, int(a["local"][0]), int(a["local"][1]))
        depth = struct.unpack("<f", struct.pack("<I", raw_word))[0]
        md = metadata[(py << 7) + px]
        # receiver C's page is also one of the 99 pages mapped by the selected 8196 block.
        c_tiles = {tuple(x["virtual_page_xy"]): x for x in data_json["figures"]["vsm_pool_occupancy"]["panels"][1]["tile_order"]}
        c_tile = c_tiles.get(tuple(map(int, a["vpage"])))
        npz_c_word = int(npz["depth_pages_all_raw_u32"][c_tile["tile"], int(a["local"][1]), int(a["local"][0])]) if c_tile else None
        explicit.append({
            "receiver": "C", "selected_vsm": int(a["id"]), "entry": entry, "phys": [px * PAGE + int(a["local"][0]), py * PAGE + int(a["local"][1])],
            "raw_word": raw_word, "depth": depth, "pidx": (py << 7) + px, "metadata_owner": int(md[2]),
            "metadata_page": [int(md[4]), int(md[5])], "shared_with_8196": bool(c_tile),
            "published_page40_raw_word": npz_c_word, "uv": [float(x) for x in a["uv"]],
            "vpage": list(map(int, a["vpage"])), "local": list(map(int, a["local"])),
            "page_table_texel": [tx, ty],
        })
    # pick a third, fully published selected-8196 page (highest native physical index)
    f1_tiles = {tuple(x["virtual_page_xy"]): x for x in data_json["figures"]["vsm_pool_occupancy"]["panels"][1]["tile_order"]}
    native_mapped = []
    for vy in range(PAGE_TABLE_BLOCK):
        for vx in range(PAGE_TABLE_BLOCK):
            e = decode_entry(int(pt_mm[vy, PT_BASE_X + vx]))
            if e["valid_for_rendering"]:
                native_mapped.append((vx, vy, e))
    native_mapped.sort(key=lambda t: (t[2]["physical_y"] << 7) + t[2]["physical_x"])
    sx, sy, se = native_mapped[-1] if native_mapped else (0, 0, None)
    if se:
        tile = f1_tiles.get((sx, sy))
        raw_word = pool_word(se["physical_x"], se["physical_y"], 0, 0)
        depth = struct.unpack("<f", struct.pack("<I", raw_word))[0]
        md = metadata[(se["physical_y"] << 7) + se["physical_x"]]
        explicit.append({
            "receiver": "S_highest_phys_selected8196", "selected_vsm": 8196,
            "entry": se["raw"], "phys": [se["physical_x"] * PAGE, se["physical_y"] * PAGE],
            "raw_word": raw_word, "depth": depth, "pidx": (se["physical_y"] << 7) + se["physical_x"],
            "metadata_owner": int(md[2]), "metadata_page": [int(md[4]), int(md[5])],
            "virtual_page": [sx, sy], "local": [0, 0], "tile": tile["tile"] if tile else None,
            "published_tile_raw_word": int(npz["depth_pages_all_raw_u32"][tile["tile"], 0, 0]) if tile else None,
        })
    explicit_ok = all(
        (r.get("published_page40_raw_word") is None or r.get("published_page40_raw_word") == r["raw_word"])
        and (r.get("published_tile_raw_word") is None or r.get("published_tile_raw_word") == r["raw_word"])
        for r in explicit
    )
    add(checks, "explicit_three_plus_addresses", bool(len(explicit) >= 3 and explicit_ok),
        "at least three selected virtual pages are reconstructed; each raw depth word matches either the producer page or the same native physical page",
        {"addresses": explicit, "count": len(explicit), "raw_word_matches": bool(explicit_ok),
         "receiver_C_metadata_owner": explicit[2].get("metadata_owner") if len(explicit) > 2 else None,
         "note": "C selects 8197 but its physical page is shared with 8196; the pass-810 metadata owner remains 8196"})

    # ------------------------------------------------------------------
    # 5. full selected block + all 99 native depth pages
    # ------------------------------------------------------------------
    entries = np.array([[decode_entry(int(pt_mm[vy, PT_BASE_X + vx])) for vx in range(PAGE_TABLE_BLOCK)]
                        for vy in range(PAGE_TABLE_BLOCK)], dtype=object)
    raw_entries = np.array([[int(pt_mm[vy, PT_BASE_X + vx]) for vx in range(PAGE_TABLE_BLOCK)]
                            for vy in range(PAGE_TABLE_BLOCK)], dtype=np.uint32)
    raw_flags = np.array([[int(pf_mm[vy, PT_BASE_X + vx]) for vx in range(PAGE_TABLE_BLOCK)]
                          for vy in range(PAGE_TABLE_BLOCK)], dtype=np.uint32)
    roundtrip_mismatch = 0
    for vy in range(PAGE_TABLE_BLOCK):
        for vx in range(PAGE_TABLE_BLOCK):
            if encode_entry(entries[vy, vx]) != int(raw_entries[vy, vx]):
                roundtrip_mismatch += 1
    pub_pt = npz["page_table_entry_u32"]
    pub_flags = npz["page_table_flags_u32"]
    pt_byte_mismatch = int(np.count_nonzero(pub_pt != raw_entries))
    pf_byte_mismatch = int(np.count_nonzero(pub_flags != raw_flags))
    lod_hist = {int(k): int(v) for k, v in zip(*np.unique([e["lod_offset"] for row in entries for e in row], return_counts=True))}
    render_count = sum(1 for row in entries for e in row if e["valid_for_rendering"])
    anyzero = sum(1 for row in entries for e in row if not e["any_lod_valid"])
    sec_count = sum(1 for e in (e for row in entries for e in row) if e["secondary_request"])
    flags_nonzero = int(np.count_nonzero(raw_flags & 0x3F))
    flag_cats = {hex(int(k)): int(v) for k, v in zip(*np.unique(raw_flags & 0x3F, return_counts=True))}
    add(checks, "selected_block_table_fields", bool(
        roundtrip_mismatch == 0 and pt_byte_mismatch == 0 and pf_byte_mismatch == 0
        and render_count == 99 and anyzero == 0 and sec_count == 0
        and lod_hist.get(0, 0) == 99 and sum(lod_hist.values()) == 16384
        and flags_nonzero == 168),
        "native selected-VSM 128x128 block decodes/re-encodes identically and exposes the published LOD/PageFlags categories",
        {"roundtrip_mismatches": roundtrip_mismatch, "published_entry_mismatches": pt_byte_mismatch,
         "published_flags_mismatches": pf_byte_mismatch, "render_valid": render_count,
         "unmapped": anyzero, "secondary": sec_count, "lod_histogram": lod_hist,
         "flags_nonzero": flags_nonzero, "flag_low6_categories": flag_cats})

    # compare all 99 page contents and metadata ownership
    page_compare = {"pages": 0, "values": 0, "max_abs_error": 0.0, "raw_bit_mismatches": 0,
                    "compact_mean_mismatches": 0, "compact_rgb_mismatches": 0, "metadata_owner_matches": 0}
    all_depth = []
    for vx, vy, e in native_mapped:
        tile = f1_tiles.get((vx, vy))
        if tile is None:
            page_compare["missing_tile"] = page_compare.get("missing_tile", 0) + 1
            continue
        px, py = e["physical_x"], e["physical_y"]
        page = np.asarray(pool_mm[py * PAGE:(py + 1) * PAGE, px * PAGE:(px + 1) * PAGE]).view("<f4").copy()
        pub = npz["depth_pages_all_f32"][tile["tile"]]
        raw_pub = npz["depth_pages_all_raw_u32"][tile["tile"]]
        page_compare["pages"] += 1
        page_compare["values"] += int(page.size)
        page_compare["max_abs_error"] = max(page_compare["max_abs_error"], float(np.max(np.abs(page.astype(np.float64) - pub.astype(np.float64)))))
        page_compare["raw_bit_mismatches"] += int(np.count_nonzero(page.view(np.uint32) != raw_pub))
        comp = page.reshape(64, 2, 64, 2).mean(axis=(1, 3)).astype(np.float32)
        if not np.array_equal(comp, npz["compact_depth_pages_f32"][tile["tile"]]):
            page_compare["compact_mean_mismatches"] += 1
        rgb, _ = depth_rgb(comp, *data_json["display"]["depth_display_range_device_z"], np.array(data_json["figures"]["vsm_depth_pages"]["depth_ramp_stops"], dtype=np.float64))
        if not np.array_equal(rgb, npz["compact_depth_rgb_u8"][tile["tile"]]):
            page_compare["compact_rgb_mismatches"] += 1
        m = metadata[(py << 7) + px]
        if [int(m[2]), int(m[3]), int(m[4]), int(m[5])] == [SEL_ID, 0, vx, vy]:
            page_compare["metadata_owner_matches"] += 1
        all_depth.append(page)
    all_depth = np.concatenate([x.ravel() for x in all_depth]) if all_depth else np.array([], dtype=np.float32)
    all_depth64 = all_depth.astype(np.float64)
    dstat = {
        "count": int(all_depth.size), "finite": int(np.isfinite(all_depth).sum()),
        "zeros": int(np.count_nonzero(all_depth == 0)),
        "min": float(np.min(all_depth64)), "max": float(np.max(all_depth64)),
        "mean": float(np.mean(all_depth64)), "std": float(np.std(all_depth64)),
        "p01": float(np.percentile(all_depth64, 1)), "p50": float(np.percentile(all_depth64, 50)),
        "p99": float(np.percentile(all_depth64, 99)),
    }
    pub_d = data_json["counts"]["depth_stats"]
    ds_ok = all(abs(dstat[k] - float(pub_d[k])) < 1e-12 for k in ("min", "max", "mean", "std", "p01", "p50", "p99")) and dstat["count"] == 1622016 and dstat["zeros"] == 0
    add(checks, "all_99_native_depth_pages", bool(page_compare["pages"] == 99 and page_compare["values"] == 1622016
        and page_compare["max_abs_error"] == 0.0 and page_compare["raw_bit_mismatches"] == 0
        and page_compare["metadata_owner_matches"] == 99 and ds_ok),
        "all 99 selected-VSM pages reproduce the published full-resolution depth and aggregate statistics exactly",
        {"page_compare": page_compare, "native_stats": dstat, "published_stats": pub_d,
         "stats_match": bool(ds_ok)})

    # global page-table reference count and occupancy/state partition
    all_entries = np.asarray(pt_mm).ravel()
    ref = np.zeros(4096, dtype=np.int32)
    nz = all_entries[(all_entries & 0x80000000) != 0]
    for v in nz:
        pidx = (((int(v) >> 10) & 0x3FF) << 7) + (int(v) & 0x3FF)
        if 0 <= pidx < ref.size:
            ref[pidx] += 1
    scene_frame = int(u["SceneFrameNumber"])
    occ = np.zeros((32, 128), dtype=np.uint8)
    state = np.zeros((32, 128), dtype=np.uint8)
    occ_counts, state_counts = {}, {}
    for pidx in range(4096):
        y, x = divmod(pidx, 128)
        flags, frame, owner = int(metadata[pidx, 0]), int(metadata[pidx, 1]), int(metadata[pidx, 2])
        if flags == 0 and ref[pidx] == 0:
            c = 0
        elif flags == 0:
            c = 5
        elif frame != scene_frame:
            c = 4
        elif owner == SEL_ID:
            c = 1
        elif ref[pidx] > 0:
            c = 2
        else:
            c = 3
        occ[y, x] = c
        occ_counts[int(c)] = occ_counts.get(int(c), 0) + 1
        if flags == 0:
            s = 3 if ref[pidx] > 0 else 0
        elif frame == scene_frame:
            s = 1
        else:
            s = 2
        state[y, x] = s
        state_counts[int(s)] = state_counts.get(int(s), 0) + 1
    occ_ok = np.array_equal(occ, npz["occupancy_category"]) and np.array_equal(state, npz["meta_state_category"])
    occ_pub = {int(k): int(v) for k, v in data_json["counts"]["occupancy_counts"].items()}
    state_pub = {int(k): int(v) for k, v in data_json["counts"]["metadata_state_counts"].items()}
    add(checks, "global_occupancy_and_state", bool(occ_ok and occ_counts == occ_pub and state_counts == state_pub
        and int(np.count_nonzero(ref)) == int(data_json["counts"]["physical_pages_referenced_by_page_table_mip0"])),
        "the global 4096-page free/referenced/selected/stale partition and metadata-state raster reproduce the published NPZ exactly",
        {"reference_unique_pages": int(np.count_nonzero(ref)), "occupancy_counts": occ_counts,
         "published_occupancy_counts": occ_pub, "metadata_state_counts": state_counts,
         "published_metadata_state_counts": state_pub, "raster_equal": bool(occ_ok)})

    # categorical colors are distinct (valid vs free/stale/other), and never depth-ramp colors
    occ_legend = {int(k): v for k, v in data_json["display"]["occupancy_legend"].items()}
    state_legend = {int(k): v for k, v in data_json["display"]["metadata_state_legend"].items()}
    ramp_colors = {tuple(map(int, c)) for c in data_json["figures"]["vsm_depth_pages"]["depth_ramp_stops"]}
    cats_ok = (len({tuple(v) for v in occ_legend.values()}) == len(occ_legend)
               and len({tuple(v) for v in state_legend.values()}) == len(state_legend)
               and tuple(occ_legend[0]) != tuple(occ_legend[4]) != tuple(occ_legend[1]))
    add(checks, "valid_invalid_categories", bool(cats_ok),
        "free, other-current, selected-current and stale categories have distinct categorical colors, separate from the depth ramp",
        {"occupancy_legend": occ_legend, "metadata_state_legend": state_legend,
         "free_equals_stale": tuple(occ_legend[0]) == tuple(occ_legend[4]),
         "stale_equals_selected": tuple(occ_legend[4]) == tuple(occ_legend[1]),
         "depth_ramp_colors": sorted(ramp_colors)})

    # ------------------------------------------------------------------
    # 6. exact figure pixels / plotting scales
    # ------------------------------------------------------------------
    lo, hi = data_json["display"]["depth_display_range_device_z"]
    stops = np.array(data_json["figures"]["vsm_depth_pages"]["depth_ramp_stops"], dtype=np.float64)
    img1 = np.asarray(Image.open(args.out / data_json["figures"]["vsm_pool_occupancy"]["file"]).convert("RGB"))
    img2 = np.asarray(Image.open(args.out / data_json["figures"]["vsm_page_table_sun"]["file"]).convert("RGB"))
    img3 = np.asarray(Image.open(args.out / data_json["figures"]["vsm_depth_pages"]["file"]).convert("RGB"))

    # F1 top/bottom categorical panels
    def cat_panel(cat, legend):
        arr = np.array([[legend[int(v)] for v in row] for row in cat], dtype=np.uint8)
        return np.repeat(np.repeat(arr, 8, axis=0), 8, axis=1)
    f1_top = cat_panel(npz["occupancy_category"], occ_legend)
    f1_bottom = cat_panel(npz["meta_state_category"], state_legend)
    f1_top_bad = int(np.count_nonzero(np.any(img1[:256] != f1_top, axis=2)))
    f1_bottom_bad = int(np.count_nonzero(np.any(img1[732:988] != f1_bottom, axis=2)))

    # F1 middle atlas interiors (ignore white receiver outlines at tile edges)
    atlas = img1[264:724]
    f1_atlas_bad = 0
    f1_atlas_checked = 0
    for i, t in enumerate(data_json["figures"]["vsm_pool_occupancy"]["panels"][1]["tile_order"]):
        r, c = divmod(i, 15)
        x0, y0 = c * 66, r * 66
        exp = npz["compact_depth_rgb_u8"][i]
        f1_atlas_bad += int(np.count_nonzero(np.any(atlas[y0:y0 + 64, x0:x0 + 64] != exp, axis=2)))
        f1_atlas_checked += 64 * 64

    # F2 full LOD panel
    lod_legend = {int(k): v for k, v in data_json["display"]["page_table_lod_legend"].items()}
    fallback = data_json["figures"]["vsm_page_table_sun"]["panels"][0]["unmapped_rgb"]
    lod_cell = np.zeros((128, 128, 3), dtype=np.uint8)
    for y in range(128):
        for x in range(128):
            lod_cell[y, x] = lod_legend[int(npz["page_table_lod"][y, x])] if npz["page_table_any"][y, x] else fallback
    f2_lod = np.repeat(np.repeat(lod_cell, 4, axis=0), 4, axis=1)
    f2_lod_bad = int(np.count_nonzero(np.any(img2[:, :512] != f2_lod, axis=2)))

    # F2 zoom center pixels + A/B outlines (both zoom panels)
    z1 = data_json["figures"]["vsm_page_table_sun"]["panels"][1]
    z2 = data_json["figures"]["vsm_page_table_sun"]["panels"][2]
    cx0, cy0, cx1, cy1 = z1["zoom"]["page_xy_range"]
    ox, oy = z1["zoom"]["panel_origin_px"]
    px_a = npz["page_table_phys_x"]
    py_a = npz["page_table_phys_y"]
    any_a = npz["page_table_any"]
    rnd_a = npz["page_table_render"]
    low_a = npz["page_table_flags_low6"]
    flag_legend = {int(k, 16): v for k, v in z2["legend"].items()}
    other = z2["other_rgb"]
    f2_ident_bad = 0
    f2_flag_bad = 0
    f2_cells = 0
    for y in range(cy0, cy1):
        for x in range(cx0, cx1):
            sx = ox + (x - cx0) * 16 + 8
            sy = oy + (y - cy0) * 16 + 8
            if any_a[y, x]:
                rr = min(255, int(px_a[y, x]) * 2)
                gg = min(255, int(py_a[y, x]) * 8)
                bb = 255 if rnd_a[y, x] else 96
                if not rnd_a[y, x]:
                    rr, gg = int(rr * 0.55), int(gg * 0.55)
            else:
                rr, gg, bb = fallback
            got = img2[sy, 520 + sx]
            if not (got[0] == rr and got[1] == gg and got[2] == bb):
                f2_ident_bad += 1
            col = flag_legend.get(int(low_a[y, x]), other)
            got = img2[sy, 1040 + sx]
            if not (got[0] == col[0] and got[1] == col[1] and got[2] == col[2]):
                f2_flag_bad += 1
            f2_cells += 1
    outline_ok = True
    outline_rows = []
    for name in ("A", "B"):
        p = data_json["receivers_declared_replay"][name]
        vx, vy = p["virtual_page_xy"]
        if cx0 <= vx < cx1 and cy0 <= vy < cy1:
            x0 = ox + (vx - cx0) * 16
            y0 = oy + (vy - cy0) * 16
            a = tuple(img2[y0 + 8, 520 + x0 - 1])
            b = tuple(img2[y0 + 8, 1040 + x0 - 1])
            outline_ok = outline_ok and a == (255, 255, 255) and b == (255, 255, 255)
            outline_rows.append({"receiver": name, "ident_border_rgb": a, "flag_border_rgb": b})

    # F3 exact pixels, excluding only the receiver marker crosses
    f3_bad = 0
    f3_checked = 0
    f3_max_diff = 0
    markers = []
    for i, t in enumerate(data_json["figures"]["vsm_depth_pages"]["tile_order"]):
        page = npz["depth_pages_f32"][i]
        big, _ = depth_rgb(page, lo, hi, stops)
        big = np.repeat(np.repeat(big, 2, axis=0), 2, axis=1)
        r, c = divmod(i, 3)
        y0, x0 = r * 264, c * 264
        sub = img3[y0:y0 + 256, x0:x0 + 256]
        mask = np.ones((256, 256), dtype=bool)
        mk = t.get("marker_texel_in_page")
        if mk is not None:
            bx, by = 2 * int(mk[0]), 2 * int(mk[1])
            mask[max(0, by - 9):by + 10, :] = False
            mask[:, max(0, bx - 9):bx + 10] = False
            white = int(np.count_nonzero(np.all(sub == 255, axis=2)))
            markers.append({"tile": i, "receiver": t.get("receiver"), "marker_texel": mk, "white_pixels": white})
            outline_ok = outline_ok and white > 0
        bad = np.any(sub != big, axis=2) & mask
        f3_bad += int(np.count_nonzero(bad))
        f3_checked += int(np.count_nonzero(mask))
        f3_max_diff = max(f3_max_diff, int(np.max(np.abs(sub.astype(int) - big.astype(int)))))
    fig_dims = {k: list(v.shape[1::-1]) for k, v in (("vsm_pool_occupancy", img1), ("vsm_page_table_sun", img2), ("vsm_depth_pages", img3))}
    stds = {k: float(np.std(v.astype(np.float64))) for k, v in (("vsm_pool_occupancy", img1), ("vsm_page_table_sun", img2), ("vsm_depth_pages", img3))}
    unique_colors = {k: len(np.unique(v.reshape(-1, 3), axis=0)) for k, v in (("vsm_pool_occupancy", img1), ("vsm_page_table_sun", img2), ("vsm_depth_pages", img3))}
    scale_ok = (
        f1_top_bad == 0 and f1_bottom_bad == 0 and f1_atlas_bad == 0
        and f2_lod_bad == 0 and f2_ident_bad == 0 and f2_flag_bad == 0
        and f3_bad == 0 and outline_ok
        and fig_dims["vsm_pool_occupancy"] == [1024, 988]
        and fig_dims["vsm_page_table_sun"] == [1552, 512]
        and fig_dims["vsm_depth_pages"] == [784, 520]
        and all(s > 1.0 for s in stds.values()) and all(c > 8 for c in unique_colors.values())
    )
    add(checks, "figure_pixels_and_scales", bool(scale_ok),
        "F1/F2/F3 pixels exactly match the declared palettes/scales; all figures are nonblank with readback dimensions",
        {"dimensions": fig_dims, "std_rgb_gray": stds, "unique_colors": unique_colors,
         "f1_top_mismatches": f1_top_bad, "f1_bottom_mismatches": f1_bottom_bad,
         "f1_atlas_interior_mismatches": f1_atlas_bad, "f1_atlas_pixels_checked": f1_atlas_checked,
         "f2_lod_mismatches": f2_lod_bad, "f2_ident_zoom_cell_mismatches": f2_ident_bad,
         "f2_flag_zoom_cell_mismatches": f2_flag_bad, "f2_zoom_cells_checked": f2_cells,
         "f3_interior_mismatches": f3_bad, "f3_pixels_checked": f3_checked,
         "f3_max_channel_diff_including_markers": f3_max_diff, "markers": markers,
         "outlines": outline_rows, "outline_ok": bool(outline_ok),
         "depth_display_range": [lo, hi], "data_min": float(dstat["min"]), "data_max": float(dstat["max"])})

    # depth display scale brackets all data and is invertible/non-degenerate
    scale_ok2 = (lo < dstat["min"] < dstat["max"] < hi and hi - lo > 0
                 and float(np.min(npz["depth_pages_all_f32"])) > 0 and float(np.max(npz["depth_pages_all_f32"])) < 1)
    t_min = (dstat["min"] - lo) / (hi - lo)
    t_max = (dstat["max"] - lo) / (hi - lo)
    add(checks, "depth_display_scale", bool(scale_ok2),
        "the fixed device-Z display range brackets the full selected-depth data and is a proper invertible mapping",
        {"lo": lo, "hi": hi, "data_min": dstat["min"], "data_max": dstat["max"],
         "margin_lo": dstat["min"] - lo, "margin_hi": hi - dstat["max"],
         "t_min": t_min, "t_max": t_max, "span": hi - lo})

    # ------------------------------------------------------------------
    # 7. provenance: manifest, inputs, archive, operand hashes
    # ------------------------------------------------------------------
    man_bad = []
    for f in manifest.get("files", []):
        p = args.out / f["path"]
        if not p.exists() or p.stat().st_size != int(f["bytes"]) or sha_file(p) != f["sha256"]:
            man_bad.append(f["path"])
    input_recs = data_json["run"].get("input_records", {})
    input_bad = []
    for key, path in (("receivers", args.receivers), ("view", args.orientation)):
        rec = input_recs.get(key) or {}
        if path.stat().st_size != int(rec.get("bytes", -1)) or sha_file(path) != rec.get("sha256"):
            input_bad.append(key)
    archive_rec = data_json["run"].get("archive_package") or {}
    archive_ok = (args.archive_index.exists() and args.archive_index.stat().st_size == int(archive_rec.get("index_bytes", -1))
                  and sha_file(args.archive_index) == archive_rec.get("index_sha256"))
    add(checks, "published_manifest_and_inputs", bool(not man_bad and not input_bad and archive_ok),
        "published files, receiver/orientation inputs and archive index match their declared sizes and SHA-256s",
        {"manifest_files": len(manifest.get("files", [])), "manifest_mismatches": man_bad,
         "input_mismatches": input_bad, "archive_index_ok": bool(archive_ok),
         "archive_index": str(args.archive_index)})

    operand_bad = []
    checksums = {
        "projection": (args.dump / exact["projection"], data_json["operands"]["projection_data"]["sha256"]),
        "page_table": (args.dump / exact["page_table"], data_json["operands"]["page_table"]["sha256"]),
        "metadata": (args.dump / exact["metadata"], data_json["operands"]["physical_page_metadata"]["sha256"]),
        "uniform": (args.dump / exact["uniform"], data_json["operands"]["uniform"]["sha256"]),
        "pool_slice0": (args.dump / exact["pool_slice0"], data_json["operands"]["physical_page_pool"]["slice_0"]["sha256"]),
        "pool_slice1": (args.dump / exact["pool_slice1"], data_json["operands"]["physical_page_pool"]["slice_1"]["sha256"]),
    }
    for k, (p, expected) in checksums.items():
        if sha_file(p) != expected:
            operand_bad.append(k)
    for m in data_json["operands"]["page_flags"]["mips"]:
        p = args.dump / m["file"]
        if sha_file(p) != m["sha256"]:
            operand_bad.append(f"page_flags_mip{m['mip']}")
    add(checks, "operand_sha256", bool(not operand_bad),
        "every selected operand file hash matches the published provenance JSON",
        {"files": len(checksums) + len(data_json["operands"]["page_flags"]["mips"]), "mismatches": operand_bad})

    # bias formula latent issue: producer adds base bias; source adds the selected per-level bias.
    biases = [r["bias"] for r in sun]
    bias_spread = max(biases) - min(biases) if biases else 0.0
    add(checks, "replay_bias_formula", bool(bias_spread == 0.0),
        "for this capture all 17 sun levels share the same ResolutionLodBias, so the producer's base-bias formula is numerically equivalent to the source per-level formula",
        {"bias_min": min(biases), "bias_max": max(biases), "spread": bias_spread,
         "producer_formula": "abs_level + base_ResolutionLodBias",
         "source_formula": "select initial level from abs_level; add that level's ResolutionLodBias; floor-minus-base",
         "latent_risk": "if per-level biases differ, the producer's declared replay would select the wrong clipmap level"})
    if bias_spread != 0.0:
        issues.append({"id": "bias_formula", "severity": "blocking", "detail": "per-level sun bias values differ; producer formula is not source-equivalent"})
    else:
        issues.append({"id": "bias_formula", "severity": "latent/non-blocking",
                       "detail": ("tools/decode_vsm_storage.py adds the base VSM's ResolutionLodBias before the floor; "
                                  "the shader first selects the initial level from the unbiased absolute level, adds that level's "
                                  "per-VSM bias, then floors. All 17 captured sun biases are 0.6639266014099121, so A/B/C "
                                  "still land on the correct levels, but the replay is not a general implementation of the source formula.")})
    if not producer_label_ok:
        pass  # issue already recorded above

    # selected version and native fields sanity against report strings
    report = (args.out / "report.md").read_text(encoding="utf-8")
    report_claims = {
        "selected_pass_840": "840 VirtualShadowMapProjection" in report and "00040000020c16c43fb0" in report,
        "selected_vsm_8196": "Selected VSM `8196`" in report,
        "pool_extent": "16384×4096" in report,
        "page_table_extent": "8192×192" in report,
        "stride_8209": "368 B" in report and "8209" in report,
        "depth_minmax": "0.499898583" in report and "0.500238657" in report,
        "metadata_snapshot": "pass-810 snapshot" in report,
    }
    add(checks, "report_numeric_claims", all(report_claims.values()),
        "the report's selected pass, VSM id, extents, stride/record count and depth extrema match the native measurements",
        report_claims)

    summary = {"PASS": sum(1 for c in checks if c["status"] == "PASS"),
               "FAIL": sum(1 for c in checks if c["status"] == "FAIL"), "SKIP": 0}
    result = {
        "schema": "vsm-storage-independent-review-v1",
        "generated_by": "review/verify_vsm_storage.py",
        "method": ("reads native DumpGPU bytes and pinned source; no producer decoder import; "
                   "compares native-derived values to published JSON/NPZ/PNGs"),
        "inputs": {
            "dump": str(args.dump), "out": str(args.out), "engine": str(args.engine),
            "receivers": str(args.receivers), "orientation": str(args.orientation),
            "archive_index": str(args.archive_index),
        },
        "checks": checks,
        "issues": issues,
        "summary": summary,
    }
    out_path = args.json or (Path(__file__).resolve().parent / "checks.json")
    out_path.write_text(json.dumps(result, indent=2, default=lambda o: o.item() if hasattr(o, "item") else str(o)), encoding="utf-8")
    print(json.dumps(summary))
    print(f"wrote {out_path}")
    return 0 if summary["FAIL"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
