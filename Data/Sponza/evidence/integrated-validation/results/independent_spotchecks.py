#!/usr/bin/env python3
"""Independent numerical spot checks for the integrated Lumen revision.

No decoder module is imported.  The script reads native GPU-dump bytes and the
engine's declared data layout/math, then compares against the published NPZ /
JSON records of the corresponding accepted captures.

Radiosity (D:/UnrealUnderstanding-Radiosity/.../20261115-105951/baseline):
  * three valid published probes are selected deterministically;
  * their page/card/depth/normal/SH addresses are re-decoded from raw bytes;
  * CardUV -> GetCardLocalPosition -> world position and the 12 signed SH
    components are reconstructed independently and compared.

Main world cache (D:/UnrealUnderstanding-Temporal/.../20261115-101057/baseline):
  * allocator/free/indirection bytes, the selected 0004 invocation, and the
    48^3 x 4 clipmap layout (vs the rejected 24^3 x 3 translucency group);
  * every published position is recomputed from
    corner + (logical+0.5)*cell + ProbeWorldOffset - PreViewTranslation and
    checked against the published position hash + per-probe records.
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
import sys
from pathlib import Path

import numpy as np

REPO = Path(r"D:/UnrealUnderstanding")
RESULTS = Path(r"D:/UnrealUnderstanding-Review/final-validation/results")
RAD_ROOT = Path(r"D:/UnrealUnderstanding-Radiosity/output/gpu-dump/raw/20261115-105951/baseline")
RAD_JSON = REPO / "Docs/Unreal/assets/lumen-sponza/temporal/radiosity/publish/data/probes-baseline.json"
RAD_NPZ = REPO / "Docs/Unreal/assets/lumen-sponza/temporal/radiosity/publish/data/probes-baseline.npz"
PLACE_ROOT = Path(r"D:/UnrealUnderstanding-Temporal/output/gpu-dump/raw/20261115-101057/baseline")
PLACE_DIR = REPO / "Docs/Unreal/assets/lumen-sponza/temporal/placements/light/data/baseline"

FAILS: list[str] = []
CHECKS: list[dict] = []


def ok(name, detail, **kw):
    CHECKS.append({"name": name, "ok": True, "detail": detail, **kw})


def fail(name, detail, **kw):
    CHECKS.append({"name": name, "ok": False, "detail": detail, **kw})
    FAILS.append(f"{name}: {detail}")


def check(name, cond, detail, **kw):
    (ok if cond else fail)(name, detail, **kw)
    return cond


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def raw(path: Path):
    return RAD_ROOT / path if path.parts[0] == "Resources" or path.parts[0] == "Structures" else PLACE_ROOT / path


# ---------------------------------------------------------------------------
# shared engine-layout helpers (data layout, not a decoder import)
# ---------------------------------------------------------------------------

def decode_half4(path: Path, ax: int, ay: int, width: int = 1024) -> np.ndarray:
    with open(path, "rb") as fh:
        fh.seek(((ay * width) + ax) * 8)
        return np.frombuffer(fh.read(8), dtype="<f2").astype(np.float64)


def shuffled_extent(mesh_extent: np.ndarray, packed: int) -> np.ndarray:
    op = packed & ((3 << 7) | (3 << 4) | (3 << 1))
    cases = {
        ((2 << 7) | (1 << 4) | (0 << 1)): (0, 1, 2),
        ((1 << 7) | (2 << 4) | (0 << 1)): (0, 2, 1),
        ((2 << 7) | (0 << 4) | (1 << 1)): (1, 0, 2),
        ((0 << 7) | (2 << 4) | (1 << 1)): (2, 0, 1),
        ((1 << 7) | (0 << 4) | (2 << 1)): (1, 2, 0),
        ((0 << 7) | (1 << 4) | (2 << 1)): (2, 1, 0),
    }
    order = cases.get(op)
    if order is None:
        raise ValueError(f"unknown ShuffleAxesNoFlip packing {op:#x}")
    return np.array([mesh_extent[order[0]], mesh_extent[order[1]], mesh_extent[order[2]]], dtype=np.float64)


def reverse_bits32(x: int) -> int:
    x &= 0xFFFFFFFF
    x = ((x & 0x55555555) << 1) | ((x & 0xAAAAAAAA) >> 1)
    x = ((x & 0x33333333) << 2) | ((x & 0xCCCCCCCC) >> 2)
    x = ((x & 0x0F0F0F0F) << 4) | ((x & 0xF0F0F0F0) >> 4)
    x = ((x & 0x00FF00FF) << 8) | ((x & 0xFF00FF00) >> 8)
    return ((x << 16) | (x >> 16)) & 0xFFFFFFFF


def probe_jitter(temporal_index: int, fixed_jitter: int, max_frames: int, spacing: int):
    # MonteCarlo.ush Hammersley16(Index, NumSamples, uint2(0x4ae4, 0x9bdb))
    index = fixed_jitter if fixed_jitter >= 0 else temporal_index
    jidx = index % max_frames
    e1 = (jidx / float(max_frames) + 0x4AE4 / 65536.0) % 1.0
    e2 = ((reverse_bits32(jidx) >> 16) ^ 0x9BDB) / 65536.0
    return int(e1 * spacing), int(e2 * spacing)


# ---------------------------------------------------------------------------
# radiosity
# ---------------------------------------------------------------------------

def radiosity_check():
    meta = load(RAD_JSON)
    npz = np.load(RAD_NPZ)
    resources = meta["resources"]
    params = meta["params"]["BuildRadiosityTiles"]
    spacing = int(params["ProbeSpacingInCardTexels"])
    shift = int(params["ProbeSpacingInCardTexelsDivideShift"])
    max_frames = int(params["MaxFramesAccumulated"])
    fixed_jitter = int(params["FixedJitterIndex"])
    atlas_size = int(params["RadiosityAtlasSize"][0])
    probe_atlas = atlas_size >> shift

    # every raw operand is checked against its published SHA-256 before use
    used_roles = ["Lumen.Cards", "Lumen.PageBuffer", "Lumen.SceneDepth", "Lumen.SceneNormal",
                  "Lumen.Radiosity.ProbeSHRedAtlas", "Lumen.Radiosity.ProbeSHGreenAtlas",
                  "Lumen.Radiosity.ProbeSHBlueAtlas", "ViewData.PrimaryViews"]

    def rpath(role):
        rel = resources[role]["file"]
        return RAD_ROOT / rel if rel.startswith("Resources/") else RAD_ROOT / "Resources" / rel

    def rad_path(rec):
        rel = rec["file"]
        return RAD_ROOT / rel if rel.startswith("Resources/") else RAD_ROOT / "Resources" / rel

    sha_ok = True
    for role in used_roles:
        rec = resources[role]
        p = rad_path(rec)
        digest = sha256(p)
        if digest != rec["sha256"]:
            sha_ok = False
            fail(f"rad.raw_sha.{role}", f"{digest} != {rec['sha256']}")
    check("rad.raw_sha_all", sha_ok, f"{len(used_roles)} native operands SHA-256 match publication")

    cards_raw = np.fromfile(rpath("Lumen.Cards"), dtype="<f4")
    ncards = cards_raw.size // 28
    pages_raw = np.fromfile(rpath("Lumen.PageBuffer"), dtype="<f4")
    npages = pages_raw.size // 20
    depth = np.fromfile(rpath("Lumen.SceneDepth"), dtype="<u2").reshape(4096, 4096)
    normal = np.fromfile(rpath("Lumen.SceneNormal"), dtype="<f2").reshape(4096, 4096, 4)
    sh_files = {ch: rpath(f"Lumen.Radiosity.ProbeSH{ch}Atlas") for ch in ("Red", "Green", "Blue")}
    view_raw = np.fromfile(rpath("ViewData.PrimaryViews"), dtype="<f4")

    def card(i):
        c = cards_raw[i * 28:(i + 1) * 28].reshape(7, 4).astype(np.float64)
        origin = c[0, :3] + np.array([c[1, 3], c[2, 3], c[3, 3]])
        axes = np.array([[c[1, 0], c[2, 0], c[3, 0]],
                         [c[1, 1], c[2, 1], c[3, 1]],
                         [c[1, 2], c[2, 2], c[3, 2]]], dtype=np.float64)
        packed0 = int(np.asarray(c[0, 3], dtype=np.float32).view(np.uint32))
        packed_axes = int(np.asarray(c[5, 3], dtype=np.float32).view(np.uint32))
        extent = shuffled_extent(c[6, :3], packed_axes)
        extent[2] = max(extent[2], 1.0)
        return {"origin": origin, "axes": axes, "extent": extent, "visible": bool((packed0 >> 24) & 1)}

    def page(i):
        p = pages_raw[i * 20:(i + 1) * 20].reshape(5, 4).astype(np.float64)
        return {
            "card_index": int(np.asarray(p[0, 0], dtype=np.float32).view(np.int32)),
            "size": (float(p[0, 2]), float(p[0, 3])),
            "mapped": float(p[0, 2]) > 0.0,
            "card_uv_rect": p[1, :4].tolist(),
            "physical_uv": (float(p[2, 0]), float(p[2, 1])),
            "card_uv_scale": (float(p[3, 0]), float(p[3, 1])),
            "last_indirect": int(np.asarray(p[4, 1], dtype=np.float32).view(np.uint32)),
            "temporal_index": int(np.asarray(p[4, 2], dtype=np.float32).view(np.uint32)),
        }

    def select(indices, cond, prefer):
        cand = [i for i in indices if cond(i)]
        if not cand:
            raise RuntimeError("sample selection found no candidate")
        return prefer(cand)

    n = int(npz["position_world_cm"].shape[0])
    all_idx = list(range(n))
    picks = []
    used = set()

    def pick(cond, prefer):
        cand = [i for i in all_idx if cond(i) and (int(npz['page_index'][i]), int(npz['card_index'][i])) not in used]
        if not cand:
            return None
        i = prefer(cand)
        used.add((int(npz['page_index'][i]), int(npz['card_index'][i])))
        picks.append(i)
        return i

    pick(lambda i: bool(npz["visible_primary_depth"][i]) and bool(npz["card_tile_in_capture"][i]),
         lambda c: c[len(c) // 2])
    pick(lambda i: bool(npz["visible_primary_depth"][i]) and not bool(npz["card_tile_in_capture"][i]),
         lambda c: c[len(c) // 4])
    pick(lambda i: bool(npz["occluded_primary_depth"][i]) and bool(npz["card_tile_in_capture"][i]),
         lambda c: c[len(c) // 2])
    while len(picks) < 3 and len(picks) < n:
        pick(lambda i: True, lambda c: c[len(picks) * len(c) // 4])

    def dec_r11g11b10_unused(*_):
        raise RuntimeError("r11g11b10 is not part of the radiosity path")

    for k, i in enumerate(picks[:3]):
        pid = int(npz["page_index"][i])
        cid = int(npz["card_index"][i])
        gx, gy = int(npz["grid_ix"][i]), int(npz["grid_iy"][i])
        pg = page(pid)
        cd = card(cid)
        check(f"rad.sample{k}.page_mapped", pg["mapped"] and pg["last_indirect"] != 0,
              f"probe[{i}] page={pid} card={cid} mapped={pg['mapped']} last_indirect={pg['last_indirect']}")
        check(f"rad.sample{k}.page_card", pg["card_index"] == cid,
              f"page.card_index={pg['card_index']} published card={cid}")

        jx, jy = probe_jitter(pg["temporal_index"], fixed_jitter, max_frames, spacing)
        check(f"rad.sample{k}.jitter", (jx, jy) == (int(npz["jitter_x"][i]), int(npz["jitter_y"][i])),
              f"recomputed jitter {(jx, jy)} published {(int(npz['jitter_x'][i]), int(npz['jitter_y'][i]))}")
        cx, cy = gx * spacing + jx, gy * spacing + jy
        check(f"rad.sample{k}.coord_in_page", cx < pg["size"][0] and cy < pg["size"][1],
              f"CoordInCardPage=({cx},{cy}) page size={pg['size']}")

        phu, phv = pg["physical_uv"]
        cuv = pg["card_uv_scale"]
        au = phu + (cx + 0.5) / atlas_size
        av = phv + (cy + 0.5) / atlas_size
        tx = min(4095, max(0, int(au * 4096)))
        ty = min(4095, max(0, int(av * 4096)))
        check(f"rad.sample{k}.atlas_uv", abs(au - float(npz["atlas_uv"][i][0])) <= 2e-9
              and abs(av - float(npz["atlas_uv"][i][1])) <= 2e-9,
              f"recomputed ({au:.12f},{av:.12f}) published {tuple(float(v) for v in npz['atlas_uv'][i])}")
        check(f"rad.sample{k}.depth_texel", (tx, ty) == tuple(int(v) for v in npz["depth_texel_xy"][i]),
              f"recomputed {(tx, ty)} published {tuple(int(v) for v in npz['depth_texel_xy'][i])}")

        raw_d = int(depth[ty, tx])
        byte_off = (ty * 4096 + tx) * 2
        check(f"rad.sample{k}.depth_raw", raw_d == int(npz["depth_raw_u16"][i]),
              f"native u16={raw_d} published={int(npz['depth_raw_u16'][i])}")
        check(f"rad.sample{k}.depth_offset", byte_off == int(npz["depth_byte_offset"][i]),
              f"recomputed byte offset={byte_off} published={int(npz['depth_byte_offset'][i])}")
        dep = raw_d / 65535.0

        cu = pg["card_uv_rect"][0] + cuv[0] * (cx + 0.5)
        cv = pg["card_uv_rect"][1] + cuv[1] * (cy + 0.5)
        check(f"rad.sample{k}.card_uv", abs(cu - float(npz["card_uv"][i][0])) <= 2e-9
              and abs(cv - float(npz["card_uv"][i][1])) <= 2e-9,
              f"recomputed ({cu:.12f},{cv:.12f}) published {tuple(float(v) for v in npz['card_uv'][i])}")

        # LumenCardCommon.ush GetCardLocalPosition: x flip first
        u = 1.0 - cu
        local = np.array([cd["extent"][0] * (1.0 - 2.0 * u),
                          cd["extent"][1] * (1.0 - 2.0 * cv),
                          -(2.0 * dep - 1.0) * cd["extent"][2]], dtype=np.float64)
        world = cd["axes"][0] * local[0] + cd["axes"][1] * local[1] + cd["axes"][2] * local[2] + cd["origin"]
        published_world = np.asarray(npz["position_world_cm"][i], dtype=np.float64)
        err_cm = float(np.max(np.abs(world - published_world)))
        check(f"rad.sample{k}.world_position", err_cm <= 1e-3,
              f"max |reconstructed-published| = {err_cm:.3e} cm (declared float32 publication precision 1e-3 cm)",
              reconstructed=[float(x) for x in world], published=[float(x) for x in published_world], error_cm=err_cm)

        nr, ng = float(normal[ty, tx, 0]), float(normal[ty, tx, 1])
        csn = np.array([nr * 2.0 - 1.0, ng * 2.0 - 1.0, 0.0], dtype=np.float64)
        csn[2] = math.sqrt(max(1.0 - csn[0] ** 2 - csn[1] ** 2, 0.0001))
        wn = cd["axes"][0] * csn[0] + cd["axes"][1] * csn[1] + cd["axes"][2] * csn[2]
        wn /= np.linalg.norm(wn)
        nerr = float(np.max(np.abs(wn - np.asarray(npz["normal_world"][i], dtype=np.float64))))
        check(f"rad.sample{k}.normal", nerr <= 1e-6,
              f"max |normal diff| = {nerr:.3e}")

        phys_x = int(round(phu * atlas_size))
        phys_y = int(round(phv * atlas_size))
        ax = (phys_x + cx) >> shift
        ay = (phys_y + cy) >> shift
        check(f"rad.sample{k}.sh_address", (ax, ay) == tuple(int(v) for v in npz["probe_atlas_xy"][i]),
              f"recomputed {(ax, ay)} published {tuple(int(v) for v in npz['probe_atlas_xy'][i])}")
        check(f"rad.sample{k}.sh_atlas_bounds", 0 <= ax < probe_atlas and 0 <= ay < probe_atlas,
              f"({ax},{ay}) inside {probe_atlas}x{probe_atlas}")
        sh_pub = np.asarray(npz["sh_coeffs_raw"][i], dtype=np.float64).reshape(3, 4)
        sh_val = np.stack([decode_half4(sh_files[ch], ax, ay) for ch in ("Red", "Green", "Blue")], axis=0)
        sh_err = float(np.max(np.abs(sh_val - sh_pub)))
        check(f"rad.sample{k}.sh12", sh_err == 0.0,
              f"12 native half coefficients identical; max diff = {sh_err:.3e}",
              native=[float(x) for x in sh_val.reshape(-1)], published=[float(x) for x in sh_pub.reshape(-1)])
        check(f"rad.sample{k}.sh_offset", ((ay * 1024 + ax) * 8) == int(npz["sh_byte_offset_r"][i]),
              f"recomputed {(ay * 1024 + ax) * 8} published {int(npz['sh_byte_offset_r'][i])}")
        # Hemisphere response, engine formula: E = 4 * DotSH(coefficients, transfer)
        # transfer = [pi, 2pi/3, 2pi/3, 2pi/3]; sample normal is the probe normal.
        x, y, z = wn
        basis = np.array([0.282095, -0.488603 * y, 0.488603 * z, -0.488603 * x])
        transfer = basis * np.array([math.pi, 2 * math.pi / 3, 2 * math.pi / 3, 2 * math.pi / 3])
        e_raw = sh_val @ transfer
        published_e = np.asarray(npz["e_normal_raw"][i], dtype=np.float64)
        e_err = float(np.max(np.abs(e_raw - published_e)))
        check(f"rad.sample{k}.e_normal", e_err <= 1e-4,
              f"max |E_raw diff| = {e_err:.3e} (published e_normal_raw)")

    return {"samples": [int(i) for i in picks[:3]],
            "probe_count": n,
            "raw_sha_ok": sha_ok}


# ---------------------------------------------------------------------------
# main world cache
# ---------------------------------------------------------------------------

def worldcache_check():
    wc = load(PLACE_DIR / "worldcache.json")
    view = load(PLACE_DIR / "view.json")
    npz = np.load(PLACE_DIR / "worldcache.npz")
    sources = wc["sources"]
    sel = wc["selection"]
    counts = wc["counts"]

    check("wc.selection_0004", sel.get("view_graph_prefix") == "0004"
          and all(sources[k].get("graph_prefix") == "0004" for k in ("indirection", "world_offset")),
          f"view prefix={sel.get('view_graph_prefix')}; indirection prefix={sources['indirection']['graph_prefix']}")
    check("wc.invocation_consistent", sel.get("invocation_consistent") is True,
          "selection.invocation_consistent=True")
    check("wc.mark_48x4", sel["mark_pass"]["res"] == 48 and sel["mark_pass"]["num"] == 4,
          "MarkRadianceProbes(ScreenProbes) res=48 num=4")

    ind_rec = sources["indirection"]
    ind_path = PLACE_ROOT / ind_rec["file"]
    ind = np.fromfile(ind_path, dtype="<u4").astype(np.uint32).reshape(48, 48, 192)
    check("wc.indirection_extent", ind.size == 48 * 48 * 48 * 4,
          f"indirection words={ind.size} == 48^3*4={48 ** 3 * 4}")
    check("wc.indirection_bytes", ind_path.stat().st_size == 48 * 48 * 48 * 4 * 4,
          f"{ind_path.stat().st_size} bytes == 192*48*48*4")
    tr = sel.get("rejected_translucency_volume", {})
    check("wc.translucency_24x3", tr.get("mark", {}).get("res") == 24
          and tr.get("mark", {}).get("num") == 3
          and tr.get("indirection_extent", [None])[0] == 72,
          f"rejected mark res/num={tr.get('mark', {}).get('res')}/{tr.get('mark', {}).get('num')} "
          f"indirection extent={tr.get('indirection_extent')} = (24*3)x24x24")

    check("wc.indirection_sha", sha256(ind_path) == ind_rec["file_sha256"],
          "indirection SHA-256 matches publication")

    alloc = int(np.fromfile(PLACE_ROOT / sources["allocator"]["file"], dtype="<u4")[0])
    free_count = int(np.fromfile(PLACE_ROOT / sources["free_list_allocator"]["file"], dtype="<u4")[0])
    freelist = np.fromfile(PLACE_ROOT / sources["free_list"]["file"], dtype="<u4")
    check("wc.allocator_bits", alloc == 925 and free_count == 47 and alloc - free_count == 878,
          f"allocator={alloc} free={free_count} alloc-free={alloc - free_count}")
    check("wc.published_counts", counts["allocator_high_water"] == alloc
          and counts["free_count"] == free_count and counts["allocator_minus_free"] == alloc - free_count,
          f"published {counts['allocator_high_water']}/{counts['free_count']}/{counts['allocator_minus_free']}")

    valid = ind != 0xFFFFFFFF
    z, y, x = np.nonzero(valid)
    enc = ind[z, y, x].astype(np.uint32)
    pids = (enc & 0xFFFF).astype(np.int64)
    order = np.argsort(pids, kind="stable")
    pids_s = pids[order]
    unique, first_s, ref_counts = np.unique(pids_s, return_index=True, return_counts=True)
    check("wc.valid_count", int(valid.sum()) == 878 and unique.size == 878,
          f"valid entries={int(valid.sum())} unique pids={unique.size} marks={int(np.count_nonzero(enc == 0xFFFFFFFE))}")
    check("wc.pid_capacity", bool(unique.max() == alloc - 1),
          f"max physical id={int(unique.max())} == allocator-1={alloc - 1}")
    active_set = set(int(v) for v in unique)
    free_ids = [int(v) for v in freelist[:free_count]]
    check("wc.free_list", len(set(free_ids)) == free_count and not (set(free_ids) & active_set),
          f"{free_count} unique free ids, no overlap with {len(active_set)} active ids")

    # --- raw corner/pre-transform operands ---
    mark = sel["mark_pass"]
    passes = []
    dec = json.JSONDecoder()
    ptext = (PLACE_ROOT / "Base/Passes.json").read_text(encoding="utf-8")
    pos = 0
    while pos < len(ptext):
        while pos < len(ptext) and ptext[pos].isspace():
            pos += 1
        if pos >= len(ptext):
            break
        obj, pos = dec.raw_decode(ptext, pos)
        passes.extend(obj if isinstance(obj, list) else [obj])
    mark_pass = next(p for p in passes if p["Pointer"] == mark["pass"]["pointer"])
    top_meta = load(PLACE_ROOT / "StructuresMetadata" / f"{mark_pass['ParametersMetadata']}.json")
    top_field = next(m for m in top_meta["Members"] if m["Name"] == "RadianceCacheMarkParameters")
    nested_meta = load(PLACE_ROOT / "StructuresMetadata" / f"{top_field['StructMetadata']}.json")
    corner_off = next(m["Offset"] for m in nested_meta["Members"]
                      if m["Name"] == "ClipmapCornerTWSAndCellSizeForMark")
    corners_off = int(top_field["Offset"]) + int(corner_off)
    check("wc.mark_parameter_offset", corners_off == 352,
          f"ClipmapCornerTWSAndCellSizeForMark at {corners_off} "
          f"(RadianceCacheMarkParameters base {int(top_field['Offset'])} + field {corner_off})")
    mark_bytes = (PLACE_ROOT / mark["parameter_block"]).read_bytes()
    corners = np.frombuffer(mark_bytes, dtype="<f4", count=24, offset=corners_off).reshape(6, 4).astype(np.float64)
    check("wc.corners_raw", np.allclose(corners, np.asarray(mark["corners"], dtype=np.float64), atol=0, rtol=0),
          f"raw corners at {mark['parameter_block']}+{corners_off} equal published corners")
    check("wc.corners_48_4", [float(corners[i][3]) for i in range(4)] ==
          [104.16666412353516, 208.3333282470703, 416.6666564941406, 833.3333129882812],
          "four captured clipmap corners with cell sizes")

    view_rec = view["view"]["resource"]
    view_bytes = (PLACE_ROOT / view_rec["file"]).read_bytes()
    pre_high = np.frombuffer(view_bytes, dtype="<f4", count=3, offset=720).astype(np.float64)
    pre_low = np.frombuffer(view_bytes, dtype="<f4", count=3, offset=832).astype(np.float64)
    pre = pre_high + pre_low
    check("wc.pre_view_translation_raw",
          np.allclose(pre, np.asarray(view["view"]["pre_view_translation_cm"]), atol=0, rtol=0)
          and np.allclose(pre_high, np.asarray(view["view"]["pre_view_translation_high_cm"]), atol=0, rtol=0)
          and np.allclose(pre_low, np.asarray(view["view"]["pre_view_translation_low_cm"]), atol=0, rtol=0),
          f"raw FPackedView PreViewTranslation = high(720)+low(832) = {pre.tolist()}")

    off_rec = sources["world_offset"]
    check("wc.world_offset_sha", sha256(PLACE_ROOT / off_rec["file"]) == off_rec["file_sha256"],
          "ProbeWorldOffset SHA-256 matches publication")
    off_arr = np.fromfile(PLACE_ROOT / off_rec["file"], dtype="<f2").reshape(-1, 4).astype(np.float64)
    check("wc.world_offset_zero", np.all(off_arr[unique, :3] == 0.0) and wc.get("probe_world_offset_zero") is True,
          f"all {unique.size} active ProbeWorldOffset xyz are exactly zero")

    # --- recompute every active position from the raw indirection + operands ---
    first = order[first_s]
    c = (x[first] // 48).astype(np.int64)
    lx = (x[first] % 48).astype(np.int64)
    coord = np.stack([lx, y[first].astype(np.int64), z[first].astype(np.int64)], axis=1)
    base_tw = corners[c, :3] + (coord + 0.5) * corners[c, 3:4]
    offset3 = off_arr[pids_s, :3].astype(np.float64)
    pos_tw = base_tw + offset3
    pos_world = pos_tw - pre[None, :]
    corner_world = corners[:4, :3] - pre[None, :]
    world_cell = np.floor(corner_world[c] / corners[c, 3:4] + coord + 0.5 + 1e-6).astype(np.int64)

    hash_arr = np.concatenate([
        pids_s.astype(np.float64)[:, None], world_cell.astype(np.float64), pos_world.astype("<f4").astype(np.float64)
    ], axis=1).astype("<f4")
    digest = hashlib.sha256(hash_arr.tobytes()).hexdigest()
    check("wc.positions_sha256", digest == wc["positions_sha256"],
          f"recomputed all-878 position hash {digest[:16]}… == published {wc['positions_sha256'][:16]}…")

    published = {int(p["physical_id"]): p for p in wc["probes"]}
    sample_pids = [int(pids_s[0]), int(pids_s[len(pids_s) // 3]), int(pids_s[2 * len(pids_s) // 3]),
                   int(pids_s[len(pids_s) // 2]), int(pids_s[-1])]
    max_err = 0.0
    mismatched = []
    for pid in sample_pids:
        j = int(np.searchsorted(pids_s, pid))
        rec = published.get(pid)
        if rec is None:
            mismatched.append(f"pid {pid} absent from publication")
            continue
        err = float(np.max(np.abs(pos_world[j] - np.asarray(rec["position_world_cm"], dtype=np.float64))))
        max_err = max(max_err, err)
        if err > 1e-5:
            mismatched.append(f"pid {pid} err {err:.3e}")
    check("wc.sample_positions", not mismatched,
          f"{len(sample_pids)} published probes recomputed; max coordinate error {max_err:.3e} cm; "
          f"pids={sample_pids}")
    return {
        "allocator": alloc, "free": free_count, "active": int(unique.size),
        "positions_sha256": digest,
        "sample_pids": sample_pids,
        "sample_max_error_cm": max_err,
        "indirection_extent": [192, 48, 48],
        "translucency_extent": tr.get("indirection_extent"),
    }


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    rad = radiosity_check()
    wc = worldcache_check()
    result = {
        "schema": "lumen-independent-spotchecks-v1",
        "radiosity": rad,
        "worldcache": wc,
        "checks": CHECKS,
        "failures": FAILS,
        "ok": not FAILS,
    }
    out = RESULTS / "independent_spotchecks.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"independent spot checks: {len(CHECKS)} checks, {len(FAILS)} failures")
    for f in FAILS:
        print("FAIL:", f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
