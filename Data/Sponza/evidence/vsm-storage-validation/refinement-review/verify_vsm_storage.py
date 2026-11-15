#!/usr/bin/env python3
"""Independent review verifier for the Sponza VSM storage artifact.

This script deliberately does NOT import the producer decoder.  It reconstructs
the page-table arithmetic, logical mip layout, PageFlags hierarchy, receiver
replay, native-depth samples and virtual-address mosaic directly from the raw
DumpGPU package and from the published JSON/NPZ/PNGs.

Usage:
    python review/verify_vsm_storage.py

Inputs (read only):
    raw capture: D:\\UnrealUnderstanding-Sponza\\output\\gpu-dump\\raw\\sponza-20261115-083654
    published:   output/
    pinned UE:   D:\\UnrealEngine\\Engine

Output:
    review/checks.json
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image

WORK = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get("VSM_OUT", WORK / "output"))
RAW = Path(os.environ.get("VSM_RAW", r"D:\UnrealUnderstanding-Sponza\output\gpu-dump\raw\sponza-20261115-083654"))
ENGINE = Path(os.environ.get("VSM_ENGINE", r"D:\UnrealEngine\Engine"))
RECEIVERS = Path(os.environ.get(
    "VSM_RECEIVERS",
    r"D:\UnrealUnderstanding\Docs\Unreal\assets\lumen-sponza\receivers\data\receivers.json"))
ORIENTATION = Path(os.environ.get(
    "VSM_ORIENTATION", r"D:\UnrealUnderstanding-Sponza\output\final\data\orientation.json"))
INDEX = Path(os.environ.get(
    "VSM_INDEX", r"D:\UnrealUnderstanding-Sponza\output\final\frame-package\index.json"))
ORIGINAL_JSON = Path(os.environ.get(
    "VSM_ORIGINAL_JSON",
    r"D:\UnrealUnderstanding-Review\vsm-storage\output\publish\data\vsm_storage.json"))

# Source constants, pinned from VirtualShadowMapDefinitions.h and
# VirtualShadowMapArray.h.  These are constants, not producer methods.
PAGE_LOG2 = 7
PAGE = 1 << PAGE_LOG2
DIM0 = 1 << 7
MAX_MIPS = PAGE_LOG2 + 1
PT_W, PT_H = 128, 192
MAX_SINGLE = 1024 * 8
PSD_STRIDE = 368
PACKED_CLIP_BIAS = 1024

VER = "v00040000020c16c43fb0"
PT_ID = "00040000020bb879e688"
PF_ID = "00040000020bb879e950"
POOL_ID = "00040000020c132ec290"
MD_ID = "00040000020ba06c1680"
PROJ_ID = "00040000020bbf9a1e30"
PROJ_VER = "v00040000020bbf9a2350"
MD_VER = "v00040000020ba0684e78"
SELECTED_ID = 8196
BASE_ID = 8192
BASE_CLIP = 6

LO, HI = 0.4998, 0.5003
RAMP = np.array([[0.00, 10, 12, 40], [0.25, 25, 60, 140], [0.50, 20, 150, 150],
                 [0.75, 220, 180, 60], [1.00, 255, 255, 235]], dtype=np.float64)
FALLBACK_RGB = (110, 92, 145)
UNMAPPED_RGB = (22, 22, 28)
GUTTER_RGB = (12, 12, 16)

CHECKS: list[dict] = []


def add(check_id: str, ok: bool, detail: str, measurements: dict | None = None):
    CHECKS.append({"id": check_id, "status": "PASS" if ok else "FAIL",
                   "detail": detail, "measurements": measurements or {}})


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 23), b""):
            h.update(chunk)
    return h.hexdigest()


def load_concat_json(path: Path) -> list:
    text = path.read_text(encoding="utf-8-sig")
    dec = json.JSONDecoder()
    out, i = [], 0
    while i < len(text):
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i >= len(text):
            break
        obj, i = dec.raw_decode(text, i)
        out.append(obj)
    return out


def f32(raw: bytes, off: int) -> float:
    return struct.unpack_from("<f", raw, off)[0]


def u32(raw: bytes, off: int) -> int:
    return struct.unpack_from("<I", raw, off)[0]


def mat4(raw: bytes, off: int) -> np.ndarray:
    # The captured FMatrix block is column-major in memory; the shader's
    # mul(float4, M) uses the mathematical row-major matrix.
    return np.frombuffer(raw, dtype="<f4", count=16, offset=off).reshape(4, 4).T.astype(np.float64)


def vec3(raw: bytes, off: int) -> np.ndarray:
    return np.frombuffer(raw, dtype="<f4", count=3, offset=off).astype(np.float64)


def level_offset(mip: int) -> tuple[int, int]:
    if mip <= 0:
        return 0, 0
    max_mask = (1 << (MAX_MIPS - 1)) - 1
    start_bit = MAX_MIPS - mip
    return (max_mask & (max_mask << start_bit)), DIM0


def block_base(vsm_id: int, mip: int) -> tuple[int, int, int]:
    full_id = vsm_id - MAX_SINGLE + 1
    bx = (full_id & 63) * PT_W + level_offset(mip)[0]
    by = (full_id >> 6) * PT_H + level_offset(mip)[1]
    return bx, by, DIM0 >> mip


def decode_entry(v: int) -> dict:
    return {
        "raw": int(v), "physical_x": int(v) & 0x3FF, "physical_y": (int(v) >> 10) & 0x3FF,
        "lod": (int(v) >> 20) & 0x3F, "secondary": bool(int(v) & 0x20000000),
        "render": bool(int(v) & 0x40000000), "any": bool(int(v) & 0x80000000),
    }


def encode_entry(e: dict) -> int:
    v = (e["physical_y"] << 10) | e["physical_x"]
    if e["lod"]:
        v |= 0x80000000 | (e["lod"] << 20)
    else:
        v |= 0x80000000 | (0x40000000 if e["render"] else 0)
    if e["secondary"]:
        v |= 0x20000000
    return v & 0xFFFFFFFF


def ramp_rgb(z) -> np.ndarray:
    a = np.asarray(z, dtype=np.float64)
    t = np.clip((a - LO) / (HI - LO), 0.0, 1.0)
    out = np.zeros(a.shape + (3,), dtype=np.float64)
    for c in range(3):
        out[..., c] = np.interp(t, RAMP[:, 0], RAMP[:, c + 1])
    return np.round(out).astype(np.uint8)


def page_word(pool: np.ndarray, px: int, py: int) -> np.ndarray:
    return np.array(pool[py * PAGE:(py + 1) * PAGE, px * PAGE:(px + 1) * PAGE], dtype="<u4")


def main() -> int:
    if not RAW.is_dir():
        print(f"missing raw capture {RAW}", file=sys.stderr)
        return 2
    js = json.loads((OUT / "publish/data/vsm_storage.json").read_text(encoding="utf-8"))
    npz = np.load(OUT / "publish/data/vsm_storage.npz")
    passes = load_concat_json(RAW / "Base/Passes.json")
    descs = load_concat_json(RAW / "Base/ResourceDescs.json")
    by_id = {p["Id"]: p for p in passes}
    desc_by_id = {d["UniqueResourceName"]: d for d in descs if "UniqueResourceName" in d}

    # ------------------------------------------------------------------ inputs
    input_records = js["run"]["input_records"]
    input_ok = (
        sha256(RECEIVERS) == input_records["receivers"]["sha256"]
        and sha256(ORIENTATION) == input_records["view"]["sha256"]
        and sha256(INDEX) == js["run"]["archive_package"]["index_sha256"]
        and RECEIVERS.stat().st_size == input_records["receivers"]["bytes"]
        and ORIENTATION.stat().st_size == input_records["view"]["bytes"]
        and INDEX.stat().st_size == js["run"]["archive_package"]["index_bytes"])
    add("input_records", input_ok, "receivers.json / orientation.json / frame-package index.json bytes and SHA-256 match the published run records",
        {"receivers_sha256": sha256(RECEIVERS), "orientation_sha256": sha256(ORIENTATION),
         "index_sha256": sha256(INDEX)})

    p840 = by_id[840]
    out840 = p840.get("OutputResources") or []
    inputs840 = p840.get("InputResources") or []
    pt_file = f"{PT_ID}.mip0"
    pf_files = [f"{PF_ID}.mip{m}" for m in range(7)]
    pool_files = [f"{POOL_ID}.[{s}].mip0" for s in (0, 1)]
    pass_ok = (
        p840["EventName"] == "VirtualShadowMapProjection(Input:GBuffer)"
        and out840 == [f"00040000020c16c43a30.mip0"]
        and all(x in inputs840 for x in [pt_file] + pf_files + pool_files))
    add("selected_pass_840", pass_ok,
        "pass 840 is the presented VirtualShadowMapProjection(Input:GBuffer) and its only output is the shadow mask; the page table, all PageFlags mips and both pool slices are inputs",
        {"pass": 840, "event": p840["EventName"], "outputs": out840,
         "input_count": len(inputs840)})

    def last_output_before(pid: int, target: str) -> int | None:
        hits = [p["Id"] for p in passes
                if p["Id"] < pid and target in (p.get("OutputResources") or [])]
        return max(hits) if hits else None

    writer_pt = last_output_before(840, pt_file)
    writer_pf = [last_output_before(840, f) for f in pf_files]
    writer_pool0 = last_output_before(840, pool_files[0])
    writer_pool1 = last_output_before(840, pool_files[1])
    writer_md = last_output_before(840, f"{MD_ID}")
    snap = js["snapshot_lineage"]
    snap_ok = (
        writer_pt == 792
        and writer_pf == [790, 791, 791, 791, 791, 791, 791]
        and writer_pool0 == 811 and writer_pool1 == 811
        and writer_md == 810
        and snap["page_table"]["last_recorded_writer"]["pass_id"] == 792
        and snap["physical_page_pool"]["last_recorded_writer"]["pass_id"] == 811
        and snap["physical_page_metadata"]["last_recorded_writer"]["pass_id"] == 810
        and [w["pass_id"] for w in snap["page_flags"]["last_recorded_writers"]] == [790, 791, 791, 791, 791, 791, 791])
    add("recorded_writer_lineage", snap_ok,
        "Passes.json records page table 792, PageFlags mip0 790 / mips1..6 791, pool resource 811 and metadata 810 as the last outputs before pass 840",
        {"page_table": writer_pt, "page_flags": writer_pf, "pool_slice0": writer_pool0,
         "pool_slice1": writer_pool1, "metadata": writer_md})

    # -------------------------------------------------------------- descriptors
    pt_desc = desc_by_id[PT_ID]
    pf_desc = desc_by_id[PF_ID]
    pool_desc = desc_by_id[POOL_ID]
    desc_ok = (pt_desc.get("NumMips") == 1 and pf_desc.get("NumMips") == 7
               and pt_desc.get("ExtentX") == 8192 and pt_desc.get("ExtentY") == 192
               and pf_desc.get("ExtentX") == 8192 and pf_desc.get("ExtentY") == 192
               and pool_desc.get("ArraySize") == 2 and pool_desc.get("NumMips") == 1)
    add("resource_descriptors", desc_ok,
        "PageTable is 8192x192 with NumMips=1; PageFlags is 8192x192 with NumMips=7; PhysicalPagePool is a 2-slice Texture2DArray with NumMips=1",
        {"page_table_num_mips": pt_desc.get("NumMips"), "page_flags_num_mips": pf_desc.get("NumMips"),
         "pool_array_size": pool_desc.get("ArraySize")})

    # ------------------------------------------------------------------- raw
    pt = np.fromfile(RAW / "Resources" / f"{PT_ID}.mip0.{VER}.bin", dtype="<u4").reshape(PT_H, 8192)
    pfs = []
    for m in range(7):
        ext = {0: (8192, 192), 1: (4096, 96), 2: (2048, 48), 3: (1024, 24),
               4: (512, 12), 5: (256, 6), 6: (128, 3)}[m]
        pfs.append(np.fromfile(RAW / "Resources" / f"{PF_ID}.mip{m}.{VER}.bin",
                               dtype="<u4").reshape(ext[1], ext[0]))
    md = np.fromfile(RAW / "Resources" / f"{MD_ID}.{MD_VER}.bin", dtype="<u4").reshape(4096, 6)
    pool = np.memmap(RAW / "Resources" / f"{POOL_ID}.[0].mip0.{VER}.bin",
                     dtype="<u4", mode="r").reshape(4096, 16384)

    # ---------------------------------------------------------- logical mips
    base0 = block_base(SELECTED_ID, 0)
    ptb = pt[0:128, base0[0]:base0[0] + 128]
    raw_render = {(x, y) for y in range(128) for x in range(128)
                  if (int(ptb[y, x]) >> 30) & 1}
    logical = []
    cover = np.zeros((PT_H, 8192), dtype=np.uint8)
    overlap = 0
    for m in range(8):
        bx, by, dim = block_base(SELECTED_ID, m)
        reg = pt[by:by + dim, bx:bx + dim]
        cover[by:by + dim, bx:bx + dim] += 1
        ents = [decode_entry(int(v)) for v in reg.ravel()]
        lodh = {int(k): int(v) for k, v in zip(*np.unique([e["lod"] for e in ents], return_counts=True))}
        logical.append({
            "mip": m, "base_texel": [bx, by], "dim_pages": dim, "entries": int(dim * dim),
            "render": int(sum(e["render"] for e in ents)),
            "any": int(sum(e["any"] for e in ents)),
            "secondary": int(sum(e["secondary"] for e in ents)),
            "lod_hist": lodh,
            "nonzero_entries": int(np.count_nonzero(reg)),
        })
    overlap = int(np.count_nonzero(cover > 1))
    lm_json = js["operands"]["page_table"]["logical_mips"]
    lm_ok = len(lm_json) == 8 and overlap == 0
    for m, want in enumerate(logical):
        got = lm_json[m]
        lm_ok &= (got["base_texel"] == want["base_texel"] and got["dim_pages"] == want["dim_pages"]
                  and got["entries"] == want["entries"] and got["valid_for_rendering"] == want["render"]
                  and got["any_lod_valid"] == want["any"]
                  and got["secondary_request"] == want["secondary"]
                  and got["lod_histogram"] == {str(k): v for k, v in want["lod_hist"].items()})
    lm_ok &= logical[0]["render"] == 99 and logical[0]["entries"] == 16384
    lm_ok &= all(x["any"] == 0 and x["render"] == 0 and x["nonzero_entries"] == 0 for x in logical[1:])
    lm_ok &= pt_desc.get("NumMips") == 1
    lm_ok &= np.array_equal(npz["page_table_logical_mip_base_texel"], np.array([x["base_texel"] for x in logical], dtype=np.int32))
    lm_ok &= np.array_equal(npz["page_table_logical_mip_dim"], np.array([x["dim_pages"] for x in logical], dtype=np.uint16))
    for m, x in enumerate(logical):
        bx, by, dim = x["base_texel"][0], x["base_texel"][1], x["dim_pages"]
        lm_ok &= np.array_equal(npz["page_table_logical_mip_entry_u32"][m, :dim, :dim],
                                pt[by:by + dim, bx:bx + dim])
        lm_ok &= int(npz["page_table_logical_mip_render"][m].sum()) == x["render"]
    add("logical_mip_packing", bool(lm_ok),
        "independent CalcLevelOffsets reconstruction: all eight logical mips 0..7 of VSM 8196 occupy non-overlapping regions in the one 128x192 PageTable hardware mip0; mip0 has 99 render-valid / 16384 entries and mips 1..7 are all-zero",
        {"block_base": list(base0), "logical_mips": logical, "overlap_texels": overlap})

    # -------------------------------------------------------- page table block
    ents = [decode_entry(int(v)) for v in ptb.ravel()]
    reencode = sum(1 for e in ents if encode_entry(e) != e["raw"])
    counts = {
        "total": int(ptb.size),
        "render": int(sum(e["render"] for e in ents)),
        "any": int(sum(e["any"] for e in ents)),
        "secondary": int(sum(e["secondary"] for e in ents)),
        "unmapped": int(sum(not e["any"] for e in ents)),
    }
    lod_hist = {int(k): int(v) for k, v in zip(*np.unique([e["lod"] for e in ents], return_counts=True))}
    pfb = pfs[0][0:128, base0[0]:base0[0] + 128]
    low = (pfb & 0x3F).astype(np.uint8)
    pf_cat = {hex(int(k)): int(v) for k, v in zip(*np.unique(low, return_counts=True))}
    jc = js["operands"]["page_table"]["counts"]
    pt_ok = (
        reencode == 0 and counts["render"] == 99 and counts["any"] == 16384
        and counts["secondary"] == 0 and counts["unmapped"] == 0
        and lod_hist == {0: 99, 1: 116, 5: 3881, 6: 12288}
        and pf_cat == {"0x0": 16216, "0x1": 69, "0x1b": 99}
        and jc["valid_for_rendering"] == 99 and jc["any_lod_valid"] == 16384
        and jc["lod_histogram"] == {str(k): v for k, v in lod_hist.items()}
        and np.array_equal(npz["page_table_entry_u32"], ptb))
    add("page_table_block", bool(pt_ok),
        "all 16384 mip0 entries decode and re-encode to the raw uint32; counts 99 render-valid, 16285 LOD fallback, 0 unmapped, LOD histogram and PageFlags low-6 categories match",
        {"counts": counts, "lod_histogram": lod_hist, "page_flags_low6": pf_cat,
         "reencode_mismatches": reencode})

    # ---------------------------------------------------- PageFlags hierarchy
    h = np.zeros((7, 128, 128), dtype=np.uint8)
    for level in range(7):
        xs = (base0[0] + np.arange(128)) >> level
        ys = np.arange(128) >> level
        h[level] = (pfs[level][np.ix_(ys, xs)] & 0x3F).astype(np.uint8)
    h_counts = {f"H{m}": int(np.count_nonzero(h[m])) for m in range(7)}
    parent_bad = 0
    for level in range(1, 7):
        prev = pfs[level - 1]
        cur = pfs[level]
        x0, w = base0[0] >> level, 128 >> level
        hgt = 192 >> level
        cx0, cw = base0[0] >> (level - 1), 128 >> (level - 1)
        ch = 192 >> (level - 1)
        child = prev[0:ch, cx0:cx0 + cw]
        pad = np.zeros(((ch + 1) // 2 * 2, (cw + 1) // 2 * 2), dtype=np.uint32)
        pad[:ch, :cw] = child
        orv = (pad[0::2, 0::2] | pad[1::2, 0::2] | pad[0::2, 1::2] | pad[1::2, 1::2]) & 0x3F
        cur_region = cur[0:hgt, x0:x0 + w] & 0x3F
        parent_bad += int(np.count_nonzero(orv[:hgt, :w] != cur_region))
    hierarchy_ok = (
        pf_desc.get("NumMips") == 7 and h_counts == {"H0": 168, "H1": 212, "H2": 320,
                                                     "H3": 512, "H4": 1024, "H5": 4096, "H6": 16384}
        and parent_bad == 0
        and np.array_equal(npz["page_table_flags_hierarchy_h0_h6"], h)
        and {"0x0": 16216, "0x1": 69, "0x1b": 99}
        == {hex(int(k)): int(v) for k, v in zip(*np.unique(h[0], return_counts=True))})
    forbidden = ("h0..h7", "h1..h7", "8 x 6-bit", "8 x 6 bit", "bits[42:47]", "bits[42:47]")
    text_blob = ((OUT / "publish/data/vsm_storage.json").read_text(encoding="utf-8")
                 + (OUT / "publish/data/schema.json").read_text(encoding="utf-8")
                 + (OUT / "source-ranges.json").read_text(encoding="utf-8")).lower()
    no_packed_claim = not any(s in text_blob for s in forbidden)
    hierarchy_ok &= no_packed_claim
    add("page_flags_hierarchy", bool(hierarchy_ok),
        "PageFlags NumMips=7 is the separate H0..H6 hierarchy: H1..H6 are the OR of the 2x2 child texels, H7 is absent, and no artifact claims eight 6-bit fields in one uint32",
        {"nonzero": h_counts, "parent_or_mismatches": parent_bad,
         "forbidden_claim_found": not no_packed_claim})

    # ------------------------------------------------------ all 99 page records
    mapped = js["operands"]["page_table"]["mapped_pages"]
    raw_pages = {(x, y) for y in range(128) for x in range(128) if (int(ptb[y, x]) >> 30) & 1}
    got_pages = {tuple(r["virtual_page_xy"]) for r in mapped}
    rec_bad = []
    for r in mapped:
        vx, vy = r["virtual_page_xy"]
        tx, ty = r["page_table_texel_xy"]
        ent = int(ptb[vy, vx])
        pword = int(pfs[0][ty, tx])
        mrec = md[r["physical_page_index"]]
        checks = {
            "entry": r["page_table_entry_hex"] == f"0x{ent:08x}",
            "entry_value": r["page_table_entry_value"] == ent,
            "ptexel": (tx, ty) == (base0[0] + vx, vy),
            "physical": r["physical_page_xy"] == [ent & 0x3FF, (ent >> 10) & 0x3FF],
            "physical_index": r["physical_page_index"] == (((ent >> 10) & 0x3FF) << 7) + (ent & 0x3FF),
            "page_flags": r["page_flags_hex"] == f"0x{pword:08x}" and r["page_flags_h0"] == (pword & 0x3F),
            "metadata_flags": r["metadata"]["flags"] == int(mrec[0]),
            "metadata_frame": r["metadata"]["last_requested_scene_frame_number"] == int(mrec[1]),
            "metadata_owner": r["metadata"]["virtual_shadow_map_id"] == int(mrec[2])
                              and r["metadata"]["mip_level"] == int(mrec[3])
                              and r["metadata"]["page_address"] == [int(mrec[4]), int(mrec[5])],
            "owner_match": r["metadata_owner_matches"] is True,
        }
        px, py = r["physical_page_xy"]
        word = page_word(pool, px, py)
        depth = word.view("<f4")
        st = r["depth_stats"]
        checks["depth_count"] = st["count"] == depth.size and st["finite"] == int(np.isfinite(depth).sum())
        checks["depth_zeros"] = st["zeros"] == int((depth == 0).sum())
        checks["depth_minmax"] = abs(st["min"] - float(depth.min())) < 1e-12 and abs(st["max"] - float(depth.max())) < 1e-12
        checks["depth_mean"] = abs(st["mean"] - float(depth.mean())) < 1e-7
        if not all(checks.values()):
            rec_bad.append({"page": [vx, vy], "failed": [k for k, v in checks.items() if not v]})
    compact = sorted(mapped, key=lambda r: r["physical_page_index"])
    npz_bad = 0
    for i, r in enumerate(compact):
        px, py = r["physical_page_xy"]
        raw = page_word(pool, px, py)
        npz_bad += int(not np.array_equal(raw, npz["depth_pages_all_raw_u32"][i]))
        npz_bad += int(not np.array_equal(raw.view("<f4"), npz["depth_pages_all_f32"][i]))
    pages_ok = (len(mapped) == 99 and raw_pages == got_pages and not rec_bad and npz_bad == 0)
    add("selected_99_page_records", bool(pages_ok),
        "every published selected page record reproduces the raw page-table entry, PageFlags H0 word, pass-810 metadata owner, physical word and depth statistics; the 99-page set equals the raw render-valid set and all 99 NPZ pages are bit-exact",
        {"records": len(mapped), "raw_pages": len(raw_pages), "missing": sorted(raw_pages - got_pages),
         "extra": sorted(got_pages - raw_pages), "record_failures": rec_bad[:5],
         "npz_page_mismatches": npz_bad})

    # --------------------------------------------------------- F3 native tiles
    fig3 = js["figures"]["vsm_depth_pages"]
    tiles = fig3["tile_order"]
    img3 = np.array(Image.open(OUT / "publish/figures/vsm_depth_pages.png").convert("RGB"))
    f3_bad = 0
    f3_samples = []
    marker_pixels = 0
    f3_npz_bad = 0
    for i, t in enumerate(tiles):
        px, py = t["physical_page_xy"]
        raw = page_word(pool, px, py)
        f3_npz_bad += int(not np.array_equal(raw, npz["depth_pages_raw_u32"][i]))
        f3_npz_bad += int(not np.array_equal(raw.view("<f4"), npz["depth_pages_f32"][i]))
        big = np.repeat(np.repeat(ramp_rgb(raw.view("<f4")), 2, 0), 2, 1)
        x0, y0, x1, y1 = t["native_px_box"]
        sub = img3[y0:y1, x0:x1]
        white = np.all(sub == 255, axis=-1)
        f3_bad += int(np.count_nonzero(np.any(sub[~white] != big[~white], axis=-1)))
        marker_pixels += int(white.sum())
        lx, ly = 30 + 7 * i, 40 + 11 * i
        z = float(np.array(pool[py * PAGE + ly, px * PAGE + lx], dtype="<u4").view("<f4"))
        got = sub[2 * ly, 2 * lx].tolist()
        want = ramp_rgb(np.array(z)).tolist()
        f3_samples.append({"tile": i, "page": t["virtual_page_xy"], "local": [lx, ly],
                           "raw_depth": z, "image_rgb": got, "ramp_rgb": want, "match": got == want})
    f3_ok = (len(tiles) == 6 and img3.shape == (520, 784, 3) and f3_bad == 0
             and all(s["match"] for s in f3_samples) and f3_npz_bad == 0
             and fig3["depth_display_range_device_z"] == [LO, HI])
    add("native_depth_figure", bool(f3_ok),
        "all six F3 native pages are bit-exact against pool slice 0; every non-marker pixel reproduces the declared [0.4998,0.5003] ramp at 2x nearest scale; four independent sample points match; white crosses are the only non-ramp pixels",
        {"tiles": len(tiles), "image": list(img3.shape), "nonmarker_mismatches": f3_bad,
         "white_marker_pixels": marker_pixels, "npz_mismatches": f3_npz_bad, "samples": f3_samples})

    # -------------------------------------------------- F4 virtual-depth mosaic
    meta = js["figures"]["vsm_virtual_depth"]
    vx0, vy0, vx1, vy1 = meta["bbox_virtual_page"]
    cw, ch = vx1 - vx0 + 1, vy1 - vy0 + 1
    exp = np.full((ch * PAGE, cw * PAGE, 3), UNMAPPED_RGB, dtype=np.uint8)
    exp_mask = np.full((ch * PAGE, cw * PAGE), 2, dtype=np.uint8)
    page_order = meta["page_order"]
    fallback_cells = 0
    for rec in page_order:
        vx, vy = rec["virtual_page_xy"]
        xx, yy = vx - vx0, vy - vy0
        px, py = rec["physical_page_xy"]
        exp[yy * PAGE:(yy + 1) * PAGE, xx * PAGE:(xx + 1) * PAGE] = ramp_rgb(
            page_word(pool, px, py).view("<f4"))
        exp_mask[yy * PAGE:(yy + 1) * PAGE, xx * PAGE:(xx + 1) * PAGE] = 0
    for yy in range(ch):
        for xx in range(cw):
            if exp_mask[yy * PAGE, xx * PAGE] == 0:
                continue
            vx, vy = vx0 + xx, vy0 + yy
            ent = decode_entry(int(ptb[vy, vx]))
            if ent["any"]:
                exp[yy * PAGE:(yy + 1) * PAGE, xx * PAGE:(xx + 1) * PAGE] = FALLBACK_RGB
                exp_mask[yy * PAGE:(yy + 1) * PAGE, xx * PAGE:(xx + 1) * PAGE] = 1
                fallback_cells += 1
    img4 = np.array(Image.open(OUT / "publish/figures/vsm_virtual_depth.png").convert("RGB"))
    mask = npz["virtual_depth_mask_u8"]
    pcat = npz["virtual_depth_page_category_u8"]
    img_bad = int(np.count_nonzero(np.any(img4 != exp, axis=-1)))
    mask_bad = int(np.count_nonzero(mask != exp_mask))
    depth_all = np.concatenate([page_word(pool, *r["physical_page_xy"]).view("<f4").ravel()
                                for r in page_order])
    tmin = float((depth_all.min() - LO) / (HI - LO))
    tmax = float((depth_all.max() - LO) / (HI - LO))
    vo = [r["virtual_address"] for r in page_order]
    mosaic_ok = (
        img4.shape == (ch * PAGE, cw * PAGE, 3) and [cw, ch] == [14, 12]
        and [vx0, vy0, vx1, vy1] == [55, 58, 68, 69]
        and meta["selected_pages"] == 99 and len(page_order) == 99
        and len({tuple(r["virtual_page_xy"]) for r in page_order}) == 99
        and all(vo[i] < vo[i + 1] for i in range(len(vo) - 1))
        and [r["virtual_page_xy"][1] * DIM0 + r["virtual_page_xy"][0] for r in page_order] == vo
        and img_bad == 0 and mask_bad == 0
        and fallback_cells == 69 and meta["fallback_cells"] == 69 and meta["unmapped_cells"] == 0
        and int(np.count_nonzero(pcat == 0)) == 99 and int(np.count_nonzero(pcat == 1)) == 69
        and int(np.count_nonzero(pcat == 2)) == 0
        and int(np.count_nonzero(mask == 0)) == 99 * PAGE * PAGE
        and int(np.count_nonzero(mask == 1)) == 69 * PAGE * PAGE
        and int(np.count_nonzero(mask == 2)) == 0
        and -1e-12 <= tmin <= 1.0 and 0.0 <= tmax <= 1.0
        and meta["scale"] == 1.0 and meta["native_px"] == [1792, 1536])
    samples = []
    for idx in (0, 10, 49, 98):
        rec = page_order[idx]
        vx, vy = rec["virtual_page_xy"]
        px, py = rec["physical_page_xy"]
        lx, ly = 26, 91
        z = float(np.array(pool[py * PAGE + ly, px * PAGE + lx], dtype="<u4").view("<f4"))
        got = img4[(vy - vy0) * PAGE + ly, (vx - vx0) * PAGE + lx].tolist()
        samples.append({"virtual_address": rec["virtual_address"], "virtual_page": [vx, vy],
                        "physical_page": [px, py], "local": [lx, ly], "raw_depth": z,
                        "image_rgb": got, "ramp_rgb": ramp_rgb(np.array(z)).tolist(),
                        "match": got == ramp_rgb(np.array(z)).tolist()})
    mosaic_ok &= all(s["match"] for s in samples)
    add("virtual_depth_mosaic", bool(mosaic_ok),
        "F4 is a 1792x1536 contiguous bbox [55,58,68,69] of 14x12 pages at scale 1.0; all 99 selected pages are placed at their virtual page coordinates in ascending virtual-address order, 69 fallback cells and 0 unmapped cells; every one of 2,752,512 pixels and the NPZ mask/category arrays match an independent raw-pool reconstruction; four samples match",
        {"image": list(img4.shape), "bbox": [vx0, vy0, vx1, vy1], "pixels": int(img4.shape[0] * img4.shape[1]),
         "pixel_mismatches": img_bad, "mask_mismatches": mask_bad, "fallback_cells": fallback_cells,
         "t_range": [tmin, tmax], "samples": samples})

    # ------------------------------------------------------- receiver replay
    proj_raw = (RAW / "Resources" / f"{PROJ_ID}.{PROJ_VER}.bin").read_bytes()
    rec_doc = json.loads(RECEIVERS.read_text(encoding="utf-8"))
    view_pre = (np.array(rec_doc["receivers"]["A"]["translated_world_cm"], dtype=np.float64)
                - np.array(rec_doc["receivers"]["A"]["world_cm"], dtype=np.float64))

    def proj(rid: int):
        raw = proj_raw[rid * PSD_STRIDE:(rid + 1) * PSD_STRIDE]
        packed = u32(raw, 328)
        return {
            "raw": raw,
            "view_clip": mat4(raw, 0),
            "tw2uv": mat4(raw, 64),
            "pre": vec3(raw, 288) + vec3(raw, 272),
            "origin": vec3(raw, 304),
            "bias": f32(raw, 300),
            "level": (packed >> 16) - PACKED_CLIP_BIAS,
            "count": packed & 0xFF,
            "index": (packed >> 8) & 0xFF,
        }

    basep = proj(BASE_ID)
    all_biases = {f32(proj_raw, rid * PSD_STRIDE + 300) for rid in range(BASE_ID, BASE_ID + 17)}
    rec_results = {}
    rec_ok = True
    for name in ("A", "B"):
        rec = rec_doc["receivers"][name]
        pt = np.array(rec["translated_world_cm"], dtype=np.float64)
        v2s = basep["pre"] - view_pre
        dist2 = float(np.sum((pt + (-basep["origin"] + v2s)) ** 2))
        abs_level = 0.5 * math.log2(max(dist2, 1e-30))
        # Source formula: pick the unbiased initial level, use that level's bias,
        # then floor the biased level to pick the final clipmap.
        initial = max(0, int(abs_level - basep["level"]))
        initial = min(initial, basep["count"] - 1)
        initial_bias = proj(BASE_ID + initial)["bias"]
        final_index = max(0, int(math.floor(abs_level + initial_bias)) - basep["level"])
        sel = proj(BASE_ID + final_index)
        p_shadow = pt + (sel["pre"] - view_pre)
        uvz = sel["tw2uv"] @ np.array([p_shadow[0], p_shadow[1], p_shadow[2], 1.0])
        uv = uvz[:2] / uvz[3]
        z = float(uvz[2] / uvz[3])
        vtex = np.floor(uv * DIM0 * PAGE).astype(np.int64)
        vtex = np.clip(vtex, 0, DIM0 * PAGE - 1)
        vpage = vtex >> PAGE_LOG2
        local = vtex & (PAGE - 1)
        ent = decode_entry(int(ptb[vpage[1], vpage[0]]))
        px, py = ent["physical_x"], ent["physical_y"]
        raw_word = int(pool[py * PAGE + local[1], px * PAGE + local[0]])
        d = struct.unpack("<f", struct.pack("<I", raw_word))[0]
        m33 = float(sel["view_clip"][2, 2])
        m43 = float(sel["view_clip"][3, 2])
        delta = d - z
        occ_raw = (z - d) / m33
        occ = max(1e-6, occ_raw)
        got = {
            "clipmap_index": final_index, "selected_vsm_id": BASE_ID + final_index,
            "shadow_uv": uv.tolist(), "receiver_device_z": z,
            "virtual_page_xy": vpage.tolist(), "texel_in_page_xy": local.tolist(),
            "page_table_entry_hex": f"0x{ent['raw']:08x}",
            "physical_page_xy": [px, py], "physical_page_index": (py << 7) + px,
            "physical_texel_xy": [int(px * PAGE + local[0]), int(py * PAGE + local[1])],
            "raw_depth_bits": f"0x{raw_word:08x}", "stored_depth_device_z": d,
            "stored_depth_minus_receiver_z": delta,
            "occluder_distance_ortho_raw_cm": occ_raw,
            "occluder_distance_ortho_cm": occ,
            "captured_shadow_mask_linear_r": rec["shadow"]["shadow_factor_r_linear"],
        }
        pub = js["receivers_declared_replay"][name]
        keymap = {"clipmap_index": "clipmap_index", "selected_vsm_id": "selected_vsm_id",
                  "physical_page_index": "physical_page_index", "raw_depth_bits": "raw_depth_bits"}
        for k, pk in keymap.items():
            if got[k] != pub[pk]:
                rec_ok = False
        for k in ("shadow_uv", "virtual_page_xy", "texel_in_page_xy", "physical_page_xy",
                  "physical_texel_xy"):
            if not np.allclose(got[k], pub[k], atol=0, rtol=0):
                rec_ok = False
        if got["page_table_entry_hex"] != pub["page_table_entry_hex"]:
            rec_ok = False
        for k in ("receiver_device_z", "stored_depth_device_z", "stored_depth_minus_receiver_z",
                  "occluder_distance_ortho_raw_cm", "occluder_distance_ortho_cm"):
            if got[k] != pub[k]:
                rec_ok = False
        if abs(got["captured_shadow_mask_linear_r"] - pub["captured_shadow_mask_linear_r"]) > 1e-12:
            rec_ok = False
        rec_results[name] = got
    # Compare the same fields with the earlier independent publication.
    orig_ok = True
    orig_meas = {}
    if ORIGINAL_JSON.is_file():
        old = json.loads(ORIGINAL_JSON.read_text(encoding="utf-8"))
        for name in ("A", "B"):
            for k in ("stored_depth_device_z", "receiver_device_z", "stored_depth_minus_receiver_z",
                      "shadow_uv", "virtual_texel_xy", "virtual_page_xy", "texel_in_page_xy",
                      "page_table_texel_xy", "page_table_entry_hex", "physical_page_xy",
                      "physical_texel_xy", "raw_depth_bits"):
                if old["receivers_declared_replay"][name].get(k) != js["receivers_declared_replay"][name].get(k):
                    orig_ok = False
        for k, v in old["counts"].items():
            if js["counts"].get(k) != v:
                orig_ok = False
        orig_meas = {"original_present": True,
                     "old_common_counts": len(old["counts"]),
                     "all_common_counts_equal": orig_ok}
    else:
        orig_meas = {"original_present": False}
    add("receiver_A_B_mapping", bool(rec_ok),
        "A and B reproduce from raw projection/PageTable/pool bytes with the source clipmap selection (initial-level bias), including UV, receiver Z, page table entry, physical texel, raw depth word, delta and occluder distance",
        {"all_sun_biases": sorted(all_biases), "selected_bias": basep["bias"],
         "A": rec_results["A"], "B": rec_results["B"]})
    add("original_A_B_preserved", bool(orig_ok),
        "A/B stored depth, receiver Z, delta, UV, addresses and every shared prior count are identical to the earlier independent publication",
        orig_meas)

    # -------------------------------------------------- latent bias semantics
    src = (WORK / "tools/decode_vsm_storage.py").read_text(encoding="utf-8")
    uses_base_bias = (
        'biased = abs_level + float(base["resolution_lod_bias"])' in src
        and 'base = sun[0]' in src)
    bias_ok = not uses_base_bias
    add("receiver_bias_formula", bool(bias_ok),
        "the receiver replay must use the source formula: choose the unbiased initial clipmap, add that level's ResolutionLodBias, then floor. The current tool still adds sun[0]'s base bias before selecting the level. This is latent for this capture because all 17 sun biases are identical, but the code/formula claim is not source-correct",
        {"uses_base_bias_at_806_825": uses_base_bias, "sun_ids": [BASE_ID, BASE_ID + 16],
         "unique_biases": sorted(all_biases), "numerically_latent_here": len(all_biases) == 1})

    # ------------------------------------------------ pool slice1 writer claim
    merge_src = (ENGINE / "Shaders/Private/VirtualShadowMaps/VirtualShadowMapPhysicalPageManagement.usf").read_text(encoding="utf-8")
    merge_body = merge_src[merge_src.find("void MergePhysicalPixel"): merge_src.find("void MergePhysicalPixel") + 500]
    writes_slice0_only = "OutPhysicalPagePool[uint3(PixelCoord, 0)]" in merge_body
    p811 = by_id[811]
    p811_outputs = p811.get("OutputResources") or []
    pool_snap_out = snap["physical_page_pool"]["last_recorded_writer"].get("output")
    slice1_claim = ("| PhysicalPagePool slice1 |" in (OUT / "report.md").read_text(encoding="utf-8")
                    and "last writer 811" in (OUT / "report.md").read_text(encoding="utf-8").split("| PhysicalPagePool slice1 |")[1].split("\n")[0])
    slice1_ok = not (writes_slice0_only and slice1_claim)
    add("pool_slice1_writer_claim", bool(slice1_ok),
        "MergeStaticPhysicalPagesIndirect's shader writes only PhysicalPagePool slice 0 (MergePhysicalPixel uses uint3(PixelCoord, 0)); pass 811 is recorded at resource level and its recorded output is .[0].mip0. The report's slice1 row nevertheless says 'last writer 811', which cannot be inferred as a slice-1 write",
        {"shader_writes_slice0_only": writes_slice0_only,
         "pass_811_recorded_outputs": p811_outputs,
         "snapshot_writer_output": pool_snap_out,
         "report_slice1_claim_811": slice1_claim})

    # ------------------------------------------------------- stale/missing claims
    flags_bits = {"ALLOCATED": 1, "UNREFERENCED": 1 << 13}
    unref = md[(md[:, 0] != 0) & (md[:, 1] != 904)]
    unref_ok = (len(unref) == 96 and set(int(x) for x in unref[:, 0]) == {0x285B}
                and set(int(x) for x in unref[:, 1]) == {3}
                and all((int(f) & flags_bits["ALLOCATED"]) and (int(f) & flags_bits["UNREFERENCED"])
                        for f in unref[:, 0]))
    no_stale = ("stale_records" not in js
                and "stale_records" not in js["operands"]["physical_page_metadata"]
                and js["operands"]["physical_page_metadata"].get("unreferenced_not_proven_invalid") is True
                and "not referenced in this snapshot" in (OUT / "report.md").read_text(encoding="utf-8")
                and "does not prove them stale or invalid" in (OUT / "report.md").read_text(encoding="utf-8"))
    missing_ok = all(x["any"] == 0 for x in logical[1:]) and "no logical page-table mip is missing" in (OUT / "report.md").read_text(encoding="utf-8")
    add("stale_and_missing_mip_claims", bool(unref_ok and no_stale and missing_ok),
        "the 96 earlier-frame metadata records are published as unreferenced (flags 0x285b include ALLOCATED|UNREFERENCED, frame 3), not proven stale from age; all eight logical PageTable mips are present and mips 1..7 are empty",
        {"unreferenced_records": len(unref), "unreferenced_flags": sorted({hex(int(x)) for x in unref[:, 0]}),
         "unreferenced_frames": sorted({int(x) for x in unref[:, 1]}),
         "published_not_proven_invalid": bool(js["operands"]["physical_page_metadata"].get("unreferenced_not_proven_invalid")),
         "logical_mips_present": len(logical)})

    # ------------------------------------------------------- source range hashes
    sr = json.loads((OUT / "source-ranges.json").read_text(encoding="utf-8"))
    range_bad = []
    for r in sr["ranges"]:
        if not r.get("source_available", True):
            continue
        text = (ENGINE / r["file"]).read_text(encoding="utf-8").splitlines()
        body = "\n".join(text[r["line"] - 1:r["end_line"]])
        if hashlib.sha256(body.encode("utf-8")).hexdigest() != r["rangeSHA"]:
            range_bad.append([r["file"], r["line"]])
    add("source_range_hashes", len(range_bad) == 0,
        "all cited UTF8-LF source ranges in source-ranges.json recompute against the pinned UE source",
        {"ranges": len(sr["ranges"]), "mismatches": range_bad})

    # ------------------------------------------------------------ summary/write
    summary = {"PASS": sum(1 for c in CHECKS if c["status"] == "PASS"),
               "FAIL": sum(1 for c in CHECKS if c["status"] == "FAIL"),
               "SKIP": 0}
    result = {"schema": "vsm-independent-review-v1", "verdict": "fail" if summary["FAIL"] else "pass",
              "summary": summary, "checks": CHECKS}
    (Path(__file__).resolve().parent / "checks.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8")
    for c in CHECKS:
        print(f"{c['status']:4s} {c['id']}: {c['detail']}")
    print(json.dumps(summary))
    return 0 if summary["FAIL"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

