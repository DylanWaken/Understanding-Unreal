"""CPU replay of one captured Lumen Surface Cache combine dispatch.

The captured state is the graph-3 ``CombineLighting CS`` pass of the Sponza
hero frame.  A card tile is selected by a declared rule over captured state,
every operand is read at its exact captured graph version, and the shader
arithmetic is evaluated in float32.  The predicted R11G11B10 store is compared
with the captured ``Lumen.SceneFinalLighting`` texel.

This is a declared query replay over captured state.  It is not a record of
which texel a GPU thread chose to shade.  No GPU work is performed and no
shader-local value is invented.
"""
from pathlib import Path
import argparse
import hashlib
import json
import math
import struct
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dump_reader

F = np.float32
DEFAULT_ROOT = r"D:/UnrealUnderstanding-Sponza/output/gpu-dump/raw/sponza-20261115-083654"
ENGINE_ROOT = Path(r"D:/UnrealEngine")
# Presented scene graph of the captured frame: 3 scene graphs + 1 Slate graph,
# presented graph index 3, pass pointers prefixed 0004.
SURFACE_PASS_ID = 694
CARD_TILE = 8
CARD_STRIDE = 7 * 4          # FLumenCardData: 7 float4
PAGE_STRIDE = 5 * 4         # FLumenCardPageData: 5 float4
MESH_STRIDE = 6 * 4         # FLumenMeshCardsData: 6 float4


def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def file_record(path, root):
    return {"path": str(Path(path).relative_to(root)), "bytes": Path(path).stat().st_size,
            "sha256": sha_file(path)}


def decode_ufloat(bits, m):
    e, a = bits >> m, bits & ((1 << m) - 1)
    if e == 0:
        return math.ldexp(a, -14 - m)
    if e == 31:
        return float("inf") if a == 0 else float("nan")
    return math.ldexp(1 + a / (1 << m), e - 15)


def decode_rgb11(v):
    return np.array([decode_ufloat(v & 2047, 6), decode_ufloat((v >> 11) & 2047, 6),
                     decode_ufloat(v >> 22, 5)], dtype=np.float32)


def encode_ufloat(v, m, mode):
    if v <= 0:
        return 0
    bits = struct.unpack("<I", struct.pack("<f", float(max(v, 0))))[0]
    e = (bits >> 23) & 255
    if e < 113:
        q = float(v) * 2 ** (14 + m)
        return int(round(q) if mode == "nearest" else math.floor(q))
    mant = bits & 0x7FFFFF
    shift = 23 - m
    if mode == "nearest":
        mant += (1 << (shift - 1)) - 1 + ((mant >> shift) & 1)
    return ((e - 112) << m) + (mant >> shift)


def encode_rgb11(v, mode="nearest"):
    return (encode_ufloat(v[0], 6, mode) | (encode_ufloat(v[1], 6, mode) << 11)
            | (encode_ufloat(v[2], 5, mode) << 22))


def pcg16(seed):
    v = [(int(x) * 1664525 + 1013904223) & 0xFFFFFFFF for x in seed]
    trace = [list(v)]
    for i in (0, 1, 2, 0, 1, 2):
        v[i] = (v[i] + v[(i + 1) % 3] * v[(i + 2) % 3]) & 0xFFFFFFFF
        trace.append(list(v))
    return [x >> 16 for x in v], trace


STORED = {"PF_FloatRGBA": ("<f2", 4), "PF_FloatR11G11B10": ("<u4", 1),
          "PF_G16": ("<u2", 1), "PF_R32_FLOAT": ("<f4", 1)}


class Texture:
    """Exact per-file readback view, decoded from SubresourceLayouts.StoredFormat."""

    def __init__(self, path, layout):
        self.path = Path(path)
        self.layout = layout
        self.w, self.h = int(layout["ExtentX"]), int(layout["ExtentY"])
        self.stored, self.native = layout["StoredFormat"], layout["NativeFormat"]
        dtype, ch = STORED[self.stored]
        expected = self.w * self.h * ch * np.dtype(dtype).itemsize
        actual = self.path.stat().st_size
        if expected != actual:
            raise ValueError(f"{path}: {self.stored} implies {expected} bytes, file has {actual}")
        shape = (self.h, self.w) if ch == 1 else (self.h, self.w, ch)
        self.data = np.memmap(path, dtype=dtype, mode="r", shape=shape)
        self.channels = ch

    def pixel(self, x, y):
        x, y = max(0, min(self.w - 1, x)), max(0, min(self.h - 1, y))
        if self.stored == "PF_FloatR11G11B10":
            return decode_rgb11(int(self.data[y, x]))
        v = np.array(self.data[y, x], dtype=np.float32)
        if self.channels == 1:
            return np.repeat(v, 3)
        v = v[:3]
        if self.native in ("PF_BC7", "PF_BC5") and self.layout.get("NativeFormatExpanded"):
            # The dump stores exporter-expanded float16, not native BC bits.
            # Snap to the 1/255 UNORM8 grid to model the hardware BC7/BC5
            # sampler. This is an approximation/model of the 8-bit decode, not a
            # bit-exact BC7 block decode; see coverageGaps.
            v = np.round(v * F(255)).astype(np.float32) / F(255)
        return v

    def sample(self, uv):
        q = np.array(uv, dtype=np.float32) * np.array([self.w, self.h], dtype=np.float32) - F(0.5)
        lo = np.floor(q).astype(int)
        t = q - lo
        weights = [(1 - t[0]) * (1 - t[1]), t[0] * (1 - t[1]),
                   (1 - t[0]) * t[1], t[0] * t[1]]
        coords = [lo, lo + [1, 0], lo + [0, 1], lo + [1, 1]]
        values = [self.pixel(*xy) for xy in coords]
        result = sum((F(w) * v for w, v in zip(weights, values)), np.zeros(3, dtype=np.float32))
        return result, {
            "uv": list(map(float, uv)), "texelSpace": q.tolist(),
            "coordinates": [x.tolist() for x in coords],
            "weights": list(map(float, weights)),
            "values": [v.tolist() for v in values],
            "formula": "q = uv * extent - 0.5; lo = floor(q); t = q - lo; bilinear over lo+(0,0),(1,0),(0,1),(1,1)"}


def load_texture(d, name, output=False):
    path, desc, ev = d.resource(name, output=output)
    layout = ev.get("layout")
    if layout is None:
        raise ValueError(f"{name}: no SubresourceLayouts record")
    tex = Texture(path, layout)
    writer = next((q for q in d.passes if q["Pointer"] == ev["version"]), None)
    record = {"name": name, "uniqueResource": ev["uniqueResource"], "version": ev["version"],
              "producerPass": None if writer is None else {
                  "id": writer["Id"], "eventName": writer["EventName"], "pointer": writer["Pointer"]},
              "file": str(path.relative_to(d.root)), "bytes": path.stat().st_size,
              "sha256": sha_file(path), "descriptor": desc, "layout": layout}
    return tex, record


def read_f32_buffer(d, name):
    path, desc, ev = d.resource(name)
    raw = np.fromfile(path, dtype=np.uint8)
    bpe, count = int(desc["BytesPerElement"]), int(desc["NumElements"])
    if raw.size != bpe * count:
        raise ValueError(f"{name}: {raw.size} != {bpe}*{count}")
    writer = next((q for q in d.passes if q["Pointer"] == ev["version"]), None)
    return raw, {"name": name, "uniqueResource": ev["uniqueResource"], "version": ev["version"],
                 "producerPass": None if writer is None else {
                     "id": writer["Id"], "eventName": writer["EventName"], "pointer": writer["Pointer"]},
                 "file": str(path.relative_to(d.root)), "bytes": path.stat().st_size,
                 "sha256": sha_file(path), "bytesPerElement": bpe, "numElements": count,
                 "descriptor": desc}


def make_texture_from_path(path, layout):
    return Texture(path, layout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--debug-candidates", type=int, default=0)
    ap.add_argument("--debug-summary", action="store_true")
    args = ap.parse_args()
    root, out = Path(args.root), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fig = out / "figures"
    fig.mkdir(exist_ok=True)

    d = dump_reader.Dump(root, SURFACE_PASS_ID)
    p = d.selected
    if p["EventName"] != "CombineLighting CS" or p["ParentEventScopes"][0] != "Radiosity":
        raise ValueError(f"unexpected pass {p['EventName']} {p['ParentEventScopes']}")
    if not p["Pointer"].startswith("0004") or p["Id"] != SURFACE_PASS_ID:
        raise ValueError("selected pass is not the presented graph-0004 Radiosity combine")

    # --- parameter block and referenced uniforms -------------------------
    params = d._read_struct(p["ParametersMetadata"], p["Parameters"])  # hashed payloads
    view = d.reference_evidence["View"]
    card_scene = d.reference_evidence["LumenCardScene"]
    view_fields = d.fields
    diff_boost = float(d.field("DiffuseColorBoost"))
    half_texel = np.array(d.field("IndirectLightingAtlasHalfTexelSize"), dtype=np.float32)
    quant_err = np.array(d.field("TargetFormatQuantizationError"), dtype=np.float32)
    pre_exposure = float(d.field("View.PreExposure"))
    # The cached-lighting exposure is a separate, fixed exposure (default 4 EV =
    # 1/16). Read it from the captured direct-lighting producer pass so the
    # exposure-space labels are not inferred from View.PreExposure.
    direct_d = dump_reader.Dump(root, 684)
    cached_pre_exposure = float(direct_d.field("CachedLightingPreExposure"))
    if abs(cached_pre_exposure - 0.0625) > 1e-12:
        raise ValueError(f"unexpected CachedLightingPreExposure {cached_pre_exposure}")
    cached_pre_exposure_inverse = 1.0 / cached_pre_exposure
    override = np.array(d.field("View.DiffuseOverrideParameter"), dtype=np.float32)
    frame_mod8 = int(d.field("View.StateFrameIndexMod8"))
    atlas_size = np.array(d.field("LumenCardScene.PhysicalAtlasSize"), dtype=np.float32)
    inv_atlas = np.array(d.field("LumenCardScene.InvPhysicalAtlasSize"), dtype=np.float32)
    num_cards = int(d.field("LumenCardScene.NumCards"))
    num_pages = int(d.field("LumenCardScene.NumCardPages"))
    num_mesh_cards = int(d.field("LumenCardScene.NumMeshCards"))
    if diff_boost != 1.0:
        raise ValueError("recorded DiffuseColorBoost is not 1; do not reuse this equation")
    if not np.all(atlas_size == 4096):
        raise ValueError(f"unexpected atlas size {atlas_size}")

    # --- exact captured resource versions --------------------------------
    tex = {}
    resources = {}
    for key, name in [("albedo", "Lumen.SceneAlbedo"), ("normal", "Lumen.SceneNormal"),
                      ("emissive", "Lumen.SceneEmissive"), ("depth", "Lumen.SceneDepth"),
                      ("direct", "Lumen.SceneDirectLighting"),
                      ("indirect", "Lumen.Radiosity.SceneIndirectLighting")]:
        tex[key], resources[key] = load_texture(d, name)
    tex["output"], resources["output"] = load_texture(d, "Lumen.SceneFinalLighting", output=True)
    for k, t in tex.items():
        if t.w != 4096 or t.h != 4096:
            raise ValueError(f"{k} atlas is {t.w}x{t.h}, not 4096x4096")

    tiles_raw, tiles_rec = read_f32_buffer(d, "Lumen.CardTiles")
    page_raw, page_rec = read_f32_buffer(d, "Lumen.PageBuffer")
    cards_raw, cards_rec = read_f32_buffer(d, "Lumen.Cards")
    mesh_raw, mesh_rec = read_f32_buffer(d, "Lumen.MeshCards")
    inst_raw, inst_rec = read_f32_buffer(d, "Lumen.SceneInstanceIndexToMeshCardsIndexBuffer")
    args_raw, args_rec = read_f32_buffer(d, "Lumen.DispatchCardTilesIndirectArgs")
    tiles = tiles_raw.view("<u4")
    pages = page_raw.view("<f4").reshape(-1, 4)
    cards = cards_raw.view("<f4").reshape(-1, 4)
    meshes = mesh_raw.view("<f4").reshape(-1, 4)
    instances = inst_raw.view("<u4")
    dispatch = list(struct.unpack("<6I", args_raw.tobytes()))
    group_count = dispatch[3]
    if dispatch[4] != 1 or dispatch[5] != 1 or group_count <= 0:
        raise ValueError(f"unexpected dispatch args {dispatch}")
    if not (len(tiles) >= group_count and len(pages) >= num_pages and len(cards) >= num_cards):
        raise ValueError("captured buffer sizes are smaller than the declared counts")

    # --- view projection --------------------------------------------------
    view_m = np.array(d.field("View.TranslatedWorldToClip"), dtype=np.float64).reshape(4, 4)
    pre = (np.array(d.field("View.PreViewTranslationHigh"), dtype=np.float64)
           + np.array(d.field("View.PreViewTranslationLow"), dtype=np.float64))
    rect = np.array(d.field("View.ViewRectMinAndSize"), dtype=np.float64)

    def project(world):
        h = np.array([world[0] + pre[0], world[1] + pre[1], world[2] + pre[2], 1.0])
        clip = h @ view_m
        w = float(clip[3])
        if w <= 1e-6:
            return {"clip": clip.tolist(), "w": w, "inFront": False, "inView": False,
                    "screen": None, "ndc": None}
        ndc = clip[:3] / w
        sx = rect[0] + (ndc[0] * 0.5 + 0.5) * rect[2]
        sy = rect[1] + (0.5 - ndc[1] * 0.5) * rect[3]
        return {"clip": clip.tolist(), "w": w, "ndc": ndc.tolist(), "screen": [float(sx), float(sy)],
                "inFront": True,
                "inView": bool(0 <= sx < rect[2] and 0 <= sy < rect[3] and 0 <= ndc[2] <= 1)}

    # --- decode card / page / mesh identity ------------------------------
    def decode_page(page_index):
        v = pages[page_index * 5:page_index * 5 + 5]
        card_index = int(np.frombuffer(v[0].tobytes(), dtype="<u4")[0])
        res_off = int(np.frombuffer(v[0].tobytes(), dtype="<u4")[1])
        size = v[0][2:4].astype(np.float32)
        rect_c = v[1].astype(np.float32)
        rect_a = v[2].astype(np.float32)
        scale = v[3][:2].astype(np.float32)
        res_tiles = np.frombuffer(v[3].tobytes(), dtype="<u4")[2:4].astype(np.int64)
        uints = np.frombuffer(v[4].tobytes(), dtype="<u4")
        return {"pageIndex": page_index, "cardIndex": card_index, "bMapped": bool(size[0] > 0),
                "sizeInTexels": size.tolist(), "cardUVRect": rect_c.tolist(),
                "physicalAtlasUVRect": rect_a.tolist(), "cardUVTexelScale": scale.tolist(),
                "physicalAtlasCoord": np.floor(rect_a[:2] * atlas_size + F(0.5)).astype(int).tolist(),
                "resLevelPageTableOffset": res_off, "resLevelSizeInTiles": res_tiles.tolist(),
                "lastDirectLightingUpdateFrameIndex": int(uints[0]),
                "lastIndirectLightingUpdateFrameIndex": int(uints[1]),
                "indirectLightingTemporalIndex": int(uints[2]),
                "directLightingTemporalIndex": int(uints[3]),
                "byteOffset": page_index * 80}

    def decode_card(card_index):
        v = cards[card_index * 7:card_index * 7 + 7]
        high = v[0][:3].astype(np.float64)
        low = np.array([v[1][3], v[2][3], v[3][3]], dtype=np.float64)
        origin = high + low
        w2l = np.array([v[1][:3], v[2][:3], v[3][:3]], dtype=np.float64)
        packed0w = int(np.frombuffer(v[0].tobytes(), dtype="<u4")[3])
        packed_axes = int(np.frombuffer(v[5].tobytes(), dtype="<u4")[3])
        mesh_origin = v[5][:3].astype(np.float64)
        mesh_extent = v[6][:3].astype(np.float64)
        ext = np.array([mesh_extent[(packed_axes >> 1) & 3], mesh_extent[(packed_axes >> 4) & 3],
                        mesh_extent[(packed_axes >> 7) & 3]], dtype=np.float64)
        ext[2] = max(ext[2], 1.0)
        return {"cardIndex": card_index, "packedWorld": [high.tolist(), low.tolist()],
                "origin": origin.tolist(), "worldToLocalRotation": w2l.tolist(),
                "meshCardsOrigin": mesh_origin.tolist(), "meshCardsExtent": mesh_extent.tolist(),
                "packedMeshCardsAxes": packed_axes,
                "localExtent": ext.tolist(), "texelSize": float(v[6][3]),
                "sizeInPages": np.frombuffer(v[4].tobytes(), dtype="<u2")[:2].tolist(),
                "pageTableOffset": int(np.frombuffer(v[4].tobytes(), dtype="<u4")[1]),
                "visible": bool((packed0w >> 24) & 1), "heightfield": bool((packed0w >> 25) & 1),
                "farField": bool((packed0w >> 26) & 1),
                "worldNormal": (w2l.T @ np.array([0.0, 0.0, 1.0])).tolist()}

    def card_uv_to_world(card, card_uv, depth):
        x, y = 1.0 - card_uv[0], card_uv[1]
        ext = card["localExtent"]
        local = np.array([ext[0] * (1 - 2 * x), ext[1] * (1 - 2 * y),
                          -(2 * depth - 1) * ext[2]], dtype=np.float64)
        return np.array(card["origin"]) + np.array(card["worldToLocalRotation"]).T @ local

    def mesh_for_card(card_index):
        for mid in range(num_mesh_cards):
            v4 = np.frombuffer(meshes[mid * 6 + 4].tobytes(), dtype="<u4")
            first, count = int(v4[0]), int(v4[1] & 0xFFFF)
            if first <= card_index < first + count:
                return {"meshCardsIndex": mid, "cardOffset": first, "numCards": count,
                        "worldOrigin": meshes[mid * 6][:3].tolist(),
                        "packedFlags": int(v4[1]), "bHeightfield": bool(v4[1] & 0x10000),
                        "bMostlyTwoSided": bool(v4[1] & 0x20000)}
        return None

    def instances_for_mesh(mesh_index):
        hits = np.nonzero(instances == mesh_index)[0]
        return {"count": int(hits.size), "sceneInstanceIndices": hits[:16].tolist()}

    # --- declared selection over captured card tiles ---------------------
    candidates = []
    rejections = {}
    for group in range(min(group_count, len(tiles))):
        packed = int(tiles[group])
        page_index = packed & 0xFFFFFF
        tile = np.array([(packed >> 24) & 15, (packed >> 28) & 15])
        if page_index >= num_pages:
            rejections["page index out of range"] = rejections.get("page index out of range", 0) + 1
            continue
        page = decode_page(page_index)
        if not page["bMapped"]:
            rejections["unmapped page"] = rejections.get("unmapped page", 0) + 1
            continue
        if page["cardIndex"] >= num_cards:
            rejections["card index out of range"] = rejections.get("card index out of range", 0) + 1
            continue
        size = np.rint(np.array(page["sizeInTexels"])).astype(int)
        if np.any(size <= 0) or np.any(tile * CARD_TILE >= size):
            rejections["tile outside page"] = rejections.get("tile outside page", 0) + 1
            continue
        local = tile * CARD_TILE + np.array([4, 4])
        xy = np.array(page["physicalAtlasCoord"]) + local
        if np.any(xy < 0) or xy[0] >= 4096 or xy[1] >= 4096:
            rejections["atlas coord outside"] = rejections.get("atlas coord outside", 0) + 1
            continue
        uv = np.array(page["physicalAtlasUVRect"][:2], dtype=np.float32) + inv_atlas * (local + F(0.5))
        a, a_s = tex["albedo"].sample(uv)
        e, e_s = tex["emissive"].sample(uv)
        di, di_s = tex["direct"].sample(uv)
        ii, ii_s = tex["indirect"].sample(uv)
        cp, cp_s = tex["indirect"].sample(np.clip(uv, np.array(page["physicalAtlasUVRect"][:2], dtype=np.float32)
                                                  + half_texel,
                                                  np.array(page["physicalAtlasUVRect"][2:], dtype=np.float32)
                                                  - half_texel))
        card = decode_card(page["cardIndex"])
        page_uv = local.astype(np.float64) / np.maximum(np.array(page["sizeInTexels"], dtype=np.float64), 1)
        card_uv = (np.array(page["cardUVRect"][:2], dtype=np.float64)
                   + page_uv * (np.array(page["cardUVRect"][2:], dtype=np.float64)
                                - np.array(page["cardUVRect"][:2], dtype=np.float64)))
        depth_texel = int(tex["depth"].data[min(4095, xy[1]), min(4095, xy[0])])
        depth = depth_texel / 65535.0
        world = card_uv_to_world(card, card_uv, depth)
        proj = project(world)
        rho = a * a * override[3] + override[:3]
        lambert = rho * F(1 / math.pi)
        final = np.maximum(np.nan_to_num((di + ii) * lambert + e, nan=0, posinf=0, neginf=0), F(0))
        actual = int(tex["output"].data[xy[1], xy[0]])
        lum = float(np.dot(final, [0.2126, 0.7152, 0.0722]))
        albedo_lum = float(np.dot(a, [0.2126, 0.7152, 0.0722]))
        emissive_lum = float(np.dot(e, [0.2126, 0.7152, 0.0722]))
        normal = np.array(card["worldNormal"])
        floor_like = abs(normal[2]) > 0.7 and card["origin"][2] < 300.0
        candidates.append({
            "group": group, "packedTile": packed, "pageIndex": page_index, "cardIndex": page["cardIndex"],
            "tileCoord": tile.tolist(), "localTexel": local.tolist(), "atlasCoord": xy.tolist(),
            "atlasUV": uv.tolist(), "page": page, "card": card, "worldPoint": world.tolist(),
            "projection": proj, "depthRaw": depth_texel, "depth": depth,
            "albedo": a.tolist(), "emissive": e.tolist(), "direct": di.tolist(), "indirect": ii.tolist(),
            "final": final.tolist(), "outputPacked": actual,
            "outputRGB": tex["output"].pixel(int(xy[0]), int(xy[1])).tolist(),
            "albedoSample": a_s, "emissiveSample": e_s, "directSample": di_s, "indirectSample": ii_s,
            "floorLike": bool(floor_like), "normal": normal.tolist(),
            "albedoLuminance": albedo_lum, "emissiveLuminance": emissive_lum, "luminance": lum,
            "valid": bool(np.all(np.isfinite(final)) and albedo_lum > 0.005
                          and not (emissive_lum > 0.5 * max(lum, 1e-9)))})

    if not candidates:
        raise RuntimeError(f"no card tile produced a sample; rejections={rejections}")

    def rank(c):
        proj = c["projection"]
        visible = 1 if proj["inView"] else 0
        lower = 1 if proj["screen"] and proj["screen"][1] > rect[3] * 0.5 else 0
        # Prefer a floor-like card visible in the hero view, then a lit stone-like
        # (non-emissive) card, then signal strength.
        return (1 if (visible and c["floorLike"]) else 0,
                1 if (visible and c["valid"]) else 0,
                1 if (visible and c["valid"] and any(abs(x) > 0 for x in c["direct"])) else 0,
                lower,
                c["valid"],
                c["luminance"] if c["valid"] else -1.0)

    candidates.sort(key=rank, reverse=True)
    chosen = candidates[0]
    if not (chosen["valid"] and chosen["projection"]["inView"]):
        raise RuntimeError("no valid candidate is visible in the hero view")
    if args.debug_summary:
        print(json.dumps({
            "total": len(candidates),
            "floorLike": sum(c["floorLike"] for c in candidates),
            "floorVisible": sum(c["floorLike"] and c["projection"]["inView"] for c in candidates),
            "directNonzero": sum(any(abs(x) > 0 for x in c["direct"]) for c in candidates),
            "directNonzeroVisible": sum(any(abs(x) > 0 for x in c["direct"]) and c["projection"]["inView"]
                                        for c in candidates),
            "visible": sum(c["projection"]["inView"] for c in candidates),
        }, indent=1))
    if args.debug_candidates:
        for c in candidates[:args.debug_candidates]:
            print(json.dumps({k: c[k] for k in ("group", "cardIndex", "pageIndex", "atlasCoord",
                                                "worldPoint", "floorLike", "luminance",
                                                "albedo", "final")} | {"screen": c["projection"]["screen"],
                                                                       "inView": c["projection"]["inView"]},
                             default=str)[:500])

    # --- full 8x8 tile validation ---------------------------------------
    page, card = chosen["page"], chosen["card"]
    size = np.rint(np.array(page["sizeInTexels"])).astype(int)
    tile = np.array(chosen["tileCoord"])
    pixels = []
    for ly in range(CARD_TILE):
        for lx in range(CARD_TILE):
            local = tile * CARD_TILE + np.array([lx, ly])
            if np.any(local >= size):
                continue
            xy = np.array(page["physicalAtlasCoord"]) + local
            if np.any(xy < 0) or xy[0] >= 4096 or xy[1] >= 4096:
                continue
            uv = np.array(page["physicalAtlasUVRect"][:2], dtype=np.float32) + inv_atlas * (local + F(0.5))
            a, _ = tex["albedo"].sample(uv)
            e, _ = tex["emissive"].sample(uv)
            di, _ = tex["direct"].sample(uv)
            ii, _ = tex["indirect"].sample(uv)
            if np.any(uv < np.array(page["physicalAtlasUVRect"][:2], dtype=np.float32)) or \
               np.any(uv > np.array(page["physicalAtlasUVRect"][2:], dtype=np.float32)):
                pass
            rho = a * a * override[3] + override[:3]
            L = np.maximum(np.nan_to_num((di + ii) * (rho * F(1 / math.pi)) + e,
                                         nan=0, posinf=0, neginf=0), F(0))
            seed = [int(xy[0]), int(xy[1]), frame_mod8 + 2]
            rnd, _ = pcg16(seed)
            E = F(rnd[0] / 65536)
            err = L * quant_err
            err = (err.view(np.uint32) & np.uint32(0xFF800000)).view(np.float32)
            dithered = L + err * E
            actual = int(tex["output"].data[xy[1], xy[0]])
            near = encode_rgb11(dithered)
            trunc = encode_rgb11(dithered, "truncate")
            pixels.append({"localThread": [lx, ly], "atlasCoord": xy.tolist(),
                           "outputPacked": actual, "predictedNearest": near,
                           "predictedTruncated": trunc, "nearestMatch": near == actual,
                           "truncationMatch": trunc == actual,
                           "ditherUniform": float(E), "final": L.tolist(),
                           "quantizationStep": err.tolist()})
    if not pixels:
        raise RuntimeError("selected tile has no texels")
    nearest = sum(p["nearestMatch"] for p in pixels)
    trunc = sum(p["truncationMatch"] for p in pixels)
    if nearest == 0 and trunc == 0:
        raise RuntimeError("selected tile matches neither float-store model")

    # --- preview images ---------------------------------------------------
    x0, y0 = [int(v) for v in chosen["atlasCoord"]]
    pad = 40
    box = (max(0, x0 - pad), max(0, y0 - pad), min(4096, x0 + pad + 1), min(4096, y0 + pad + 1))

    def crop(tex_obj, mode):
        final_display_gain = 8.0
        x1, y1, x2, y2 = box
        arr = np.array(tex_obj.data[y1:y2, x1:x2])
        if tex_obj.stored == "PF_FloatR11G11B10":
            words = arr.reshape(-1).astype(np.uint32)
            arr = np.stack([decode_rgb11(int(w)) for w in words]).reshape(y2 - y1, x2 - x1, 3)
        else:
            arr = np.array(arr, dtype=np.float32)
            if arr.ndim == 2:
                arr = np.repeat(arr[..., None], 3, axis=2)
            arr = arr[..., :3]
        if tex_obj.native in ("PF_BC7", "PF_BC5") and tex_obj.layout.get("NativeFormatExpanded"):
            arr = np.round(np.clip(arr, 0, 1) * 255) / 255
        if mode == "final":
            # Stored atlas is cached-lighting pre-exposed. This preview applies
            # only the camera View.PreExposure (and a named gain); it does not
            # remove CachedLightingPreExposure, so it is a partial exposure
            # transform, not a display-linear preview.
            arr = arr * F(pre_exposure) * F(final_display_gain)
        return np.clip(arr, 0, 1)

    def upscale(a, k=4):
        img = Image.fromarray((a * 255 + 0.5).astype(np.uint8), "RGB")
        return np.array(img.resize((a.shape[1] * k, a.shape[0] * k), Image.NEAREST), dtype=np.float32) / 255

    alb = upscale(crop(tex["albedo"], "albedo"))
    nrm = upscale(crop(tex["normal"], "normal"))
    # The atlas stores cached-lighting pre-exposed radiance; this preview applies
    # only View.PreExposure for a partial exposure transform. The cached-exposure
    # inverse is intentionally not applied here, and the metrics sidecar says so.
    fin_lin = crop(tex["output"], "final")
    fin = upscale(fin_lin)
    comp = np.concatenate([alb, np.ones((alb.shape[0], 8, 3)), nrm,
                           np.ones((alb.shape[0], 8, 3)), fin], axis=1)
    Image.fromarray((comp * 255 + 0.5).astype(np.uint8), "RGB").save(fig / "surface-cache-atlas-crop.png")

    # Hero-view overlay: the captured screenshot with the projected card page and
    # the exact 8x8 tile footprint. No text is rasterized; the metrics sidecar
    # names the transforms and scales.
    overlay = {"pageQuadScreen": [], "tileQuadScreen": [], "pointScreen": chosen["projection"]["screen"]}
    shot_path = root / "Base/Screenshot.png"
    if shot_path.exists():
        shot = Image.open(shot_path).convert("RGB")
        from PIL import ImageDraw

        def page_uv_to_screen(u, v):
            cuv = (np.array(page["cardUVRect"][:2], dtype=np.float64)
                   + np.array([u, v], dtype=np.float64)
                   * (np.array(page["cardUVRect"][2:], dtype=np.float64)
                      - np.array(page["cardUVRect"][:2], dtype=np.float64)))
            pj = project(card_uv_to_world(card, cuv, chosen["depth"]))
            return pj["screen"] if (pj["inFront"] and pj["screen"]) else None

        page_quad = [page_uv_to_screen(u, v) for u, v in ((0, 0), (1, 0), (1, 1), (0, 1))]
        tile = np.array(chosen["tileCoord"])
        sz = np.maximum(np.array(page["sizeInTexels"], dtype=np.float64), 1)
        tile_quad = [page_uv_to_screen(float((tile[0] + u) * CARD_TILE) / sz[0],
                                       float((tile[1] + v) * CARD_TILE) / sz[1])
                     for u, v in ((0, 0), (1, 0), (1, 1), (0, 1))]
        overlay["pageQuadScreen"] = page_quad
        overlay["tileQuadScreen"] = tile_quad
        draw = ImageDraw.Draw(shot)
        for quad, colour in ((page_quad, (0, 255, 0)), (tile_quad, (255, 0, 255))):
            if all(quad):
                pts = [(max(0, min(1279, x)), max(0, min(719, y))) for x, y in quad]
                draw.line(pts + [pts[0]], fill=colour, width=2)
        sx, sy = chosen["projection"]["screen"]
        draw.ellipse([sx - 4, sy - 4, sx + 4, sy + 4], outline=(255, 255, 0), width=2)
        shot.save(fig / "surface-cache-hero-projection.png")

    def stats(a):
        a = np.asarray(a, dtype=np.float64)
        return {"min": float(a.min()), "max": float(a.max()), "mean": float(a.mean()),
                "finiteCount": int(np.isfinite(a).sum()), "elementCount": int(a.size),
                "nonEmpty": bool(a.size > 0 and np.isfinite(a).all())}

    alb_crop = crop(tex["albedo"], "albedo")
    nrm_crop = crop(tex["normal"], "normal")
    fin_crop = crop(tex["output"], "final")
    preview_metrics = {
        "atlasCrop": {"boxXYXY": list(box), "upscaleFactor": 4, "panels": ["albedo", "normal", "final"],
                      "channelMap": "RGB = stored XYZ",
                      "colorSpace": "linear (no sRGB transfer)",
                      "albedoPanelEncoding": "BC7 encoded albedo = sqrt(linear rho); the panel shows stored RGB, not rho",
                      "normalPreviewTransform": "raw stored RG shown as RGB (B=0); decode xy=2*RG-1, z=sqrt(1-xy^2)",
                      "finalPreviewTransform": "stored_cached_pre_exposed_final * View.PreExposure (%s) * gain (8) then clip[0,1]; the *16 cached-exposure inverse is NOT applied, so this is a partial exposure transform, not a display-linear preview" % pre_exposure,
                      "albedoStats": stats(alb_crop), "normalStats": stats(nrm_crop),
                      "finalStats": stats(fin_crop)},
        "heroProjection": {"image": "Base/Screenshot.png", "viewRect": rect.tolist(),
                           "translationCM": pre.tolist(),
                           "worldPointCM": chosen["worldPoint"],
                           "pointScreenPixels": chosen["projection"]["screen"],
                           "pageQuadScreenPixels": overlay["pageQuadScreen"],
                           "tile8x8QuadScreenPixels": overlay["tileQuadScreen"],
                           "pageTexelScaleCM": (np.array(card["localExtent"])[:2]
                                                * (np.array(page["cardUVRect"][2:]) - np.array(page["cardUVRect"][:2]))
                                                / np.maximum(np.array(page["sizeInTexels"]), 1)).tolist(),
                           "frameMod8": frame_mod8},
    }
    (fig / "surface-cache-preview-metrics.json").write_text(
        json.dumps(preview_metrics, indent=2, allow_nan=False), encoding="utf-8")

    # --- report -----------------------------------------------------------
    mesh = mesh_for_card(card["cardIndex"])
    inst = instances_for_mesh(mesh["meshCardsIndex"]) if mesh else None
    normal_rgb = tex["normal"].pixel(x0, y0)
    nx, ny = normal_rgb[0] * 2.0 - 1.0, normal_rgb[1] * 2.0 - 1.0
    nz = math.sqrt(max(0.0, 1.0 - nx * nx - ny * ny))
    normal_decoded = np.array([nx, ny, nz], dtype=np.float64)
    report = {
        "schema": "sponza-surface-cache-query-v1",
        "scope": "one declared card-tile query replayed over captured presented scene-graph 0004 state; not an observed shader-thread choice",
        "capture": {"root": str(root), "engineVersion": "5.8.1",
                    "engineCommit": "71fe36aac5a8df5ccd66c763ffc902b29b6a9c43",
                    "pass": p, "parameterPayload": params, "referencedStructs": {
                        "View": view, "LumenCardScene": card_scene}},
        "sampler": {"capturedPointer": d.field("BilinearClampedSampler")["pointer"],
                    "capturedOffset": 88, "filter": "SF_Bilinear", "address": "AM_Clamp",
                    "mipBias": 0, "maxAnisotropy": 1,
                    "source": "Engine/Source/Runtime/Renderer/Private/Lumen/LumenSceneLighting.cpp:190; "
                              "Engine/Shaders/Private/Lumen/LumenSceneLighting.usf:471,500-503"},
        "resources": resources,
        "buffers": {"CardTiles": tiles_rec, "PageBuffer": page_rec, "Cards": cards_rec,
                    "MeshCards": mesh_rec, "SceneInstanceIndexToMeshCardsIndexBuffer": inst_rec,
                    "DispatchCardTilesIndirectArgs": args_rec},
        "dispatch": {"argsUint32": dispatch, "groups": dispatch[3:6],
                     "tilesAvailable": int(len(tiles)), "shader": "CombineLumenSceneLightingCS",
                     "numthreads": [8, 8, 1],
                     "argsNote": "The captured buffer holds two indirect triples, (62,1,1) and (3953,1,1); this replay evaluates the second triple (3953 card-tile groups). DumpGPU does not record which triple the dispatch consumed."},
        "lumenCardScene": {"NumCards": num_cards, "NumMeshCards": num_mesh_cards,
                           "NumCardPages": num_pages,
                           "PhysicalAtlasSize": atlas_size.tolist(),
                           "InvPhysicalAtlasSize": inv_atlas.tolist(),
                           "IndirectLightingAtlasDownsampleFactor": float(d.field("LumenCardScene.IndirectLightingAtlasDownsampleFactor"))},
        "view": {"ViewRectMinAndSize": rect.tolist(), "TranslatedWorldToClip": view_m.tolist(),
                 "PreViewTranslation": pre.tolist(), "PreExposure": pre_exposure,
                 "DiffuseOverrideParameter": override.tolist(), "StateFrameIndexMod8": frame_mod8,
                 "projectionFormula": "clip = (world + PreViewTranslation,1) @ TranslatedWorldToClip; ndc = clip.xyz/clip.w; screen.x = rect.x + (ndc.x*0.5+0.5)*rect.w; screen.y = rect.y + (0.5-ndc.y*0.5)*rect.h"},
        "parameters": {"DiffuseColorBoost": diff_boost,
                       "IndirectLightingAtlasHalfTexelSize": half_texel.tolist(),
                       "TargetFormatQuantizationError": quant_err.tolist()},
        "selection": {"rule": "over all dispatched card-tile groups: mapped page, tile inside page, atlas coord inside 4096x4096, non-zero encoded albedo, non-emissive-dominated cached-pre-exposed final; prefer a floor-like card (|world normal Z|>0.7 and origin Z<300 cm) visible in the hero view, then a visible valid card with non-zero captured direct lighting, then lower-half visible cards, then luminance",
                      "candidatesExamined": len(candidates), "rejections": rejections,
                      "selectedGroup": chosen["group"]},
        "card": card, "page": page, "meshCards": mesh, "sceneInstances": inst,
        "material": {"encodedAlbedoRGB": chosen["albedo"],
                     "albedoEncoding": "BC7 stores sqrt(linear diffuse albedo) (encoded albedo). DecodeSurfaceCacheAlbedo squares it, then applies DiffuseOverrideParameter.w/.rgb and DiffuseColorBoost (=1 here).",
                     "albedoNormalization": "PF_BC7 readback is expanded float16, not native BC bits. The replay snaps the decoded channels to the 1/255 UNORM8 grid to model hardware BC7 sampling. Selected texel raw expanded (0.3999023, 0.3764648, 0.3371582) -> snapped (102,96,86)/255 = (0.4,0.37647,0.33725). The snap is a model of the UNORM8 sampler, not a bit-exact BC7 block decode.",
                     "normalAtlasRGBEncoded": normal_rgb.tolist(),
                     "normalDecoded": normal_decoded.tolist(),
                     "normalDecode": "BC5 stores encoded xy; xy=2*storedRG-1, z=sqrt(1-xy^2), sign taken from the card world normal",
                     "depthNormalized": chosen["depth"], "depthRaw": chosen["depthRaw"],
                     "note": "No material asset name is present in DumpGPU; these are the captured material attributes of the selected card texel. The albedo is encoded (sqrt of linear albedo), not linear albedo."},
        "query": {"tileCoord": chosen["tileCoord"], "localTexel": chosen["localTexel"],
                  "atlasCoord": chosen["atlasCoord"], "atlasUV": chosen["atlasUV"],
                  "pageUV": (np.array(chosen["localTexel"]) / np.maximum(np.array(page["sizeInTexels"]), 1)).tolist(),
                  "cardUV": (np.array(page["cardUVRect"][:2])
                             + (np.array(chosen["localTexel"]) / np.maximum(np.array(page["sizeInTexels"]), 1))
                             * (np.array(page["cardUVRect"][2:]) - np.array(page["cardUVRect"][:2]))).tolist(),
                  "worldPoint": chosen["worldPoint"], "projection": chosen["projection"],
                  "depthRaw": chosen["depthRaw"], "depth": chosen["depth"]},
        "taps": {"albedo": {**chosen["albedoSample"],
                            "encoding": "BC7 encoded albedo (sqrt of linear rho); raw expanded selected texel (0.3999023,0.3764648,0.3371582), snapped to (102,96,86)/255. The squared value, not this encoded value, is the linear albedo."},
                 "emissive": chosen["emissiveSample"],
                 "direct": chosen["directSample"], "indirect": chosen["indirectSample"],
                 "filterFootnote": "At an exact texel-centre UV the four bilinear weights are (1,0,0,0); all four captured values are retained."},
        "values": {"encodedAlbedo": chosen["albedo"],
                   "encodedAlbedoNote": "The BC7 albedo atlas stores EncodedAlbedo = sqrt(linear diffuse albedo); CombineFinalLighting decodes it as EncodedAlbedo^2 before the /pi Lambert term. The 0.4 value is therefore encoded albedo, and rho = 0.16, not 0.4.",
                   "cachedPreExposedEmissive": chosen["emissive"],
                   "cachedPreExposedDirect": chosen["direct"],
                   "cachedPreExposedIndirect": chosen["indirect"],
                   "diffuseAlbedoRho": (np.array(chosen["albedo"]) ** 2 * override[3] + override[:3]).tolist(),
                   "lambertRhoOverPi": ((np.array(chosen["albedo"]) ** 2 * override[3] + override[:3]) / math.pi).tolist(),
                   "cachedPreExposedFinalBeforeQuantization": chosen["final"],
                   "storedFinalTimesViewPreExposure": (np.array(chosen["final"]) * F(pre_exposure)).tolist(),
                   "storedFinalTimesViewPreExposureNote": "Partial transform only: cached-pre-exposed final * View.PreExposure. It does not remove CachedLightingPreExposure and is not display-linear.",
                   "unPreExposedFinal": (np.array(chosen["final"]) * F(cached_pre_exposure_inverse)).tolist(),
                   "viewPreExposedFinal": (np.array(chosen["final"]) * F(cached_pre_exposure_inverse) * F(pre_exposure)).tolist(),
                   "cachedPreExposedLuminance": chosen["luminance"],
                   "viewPreExposure": pre_exposure,
                   "cachedLightingPreExposure": cached_pre_exposure,
                   "cachedLightingPreExposureInverse": cached_pre_exposure_inverse,
                   "cachedLightingPreExposureSource": "captured pass 684 'Batched lights' parameter block: CachedLightingPreExposure=0.0625, View.PreExposure=0.00048828125; LumenSceneDirectLighting.usf:163 writes Irradiance*CachedLightingPreExposure. The same view's world pass also records RadianceCacheOneOverCachedLightingPreExposure=16.0.",
                   "exposureNote": "Lumen.SceneFinalLighting stores cached-lighting pre-exposed radiance: linear radiance * CachedLightingPreExposure (1/16). Un-pre-exposed radiance = stored*16; view-pre-exposed scene color = stored*16*View.PreExposure (1/2048). LumenSurfaceCacheSampling.ush:312-314 applies the *16 on read. The store is not view-pre-exposed and the old stored*View.PreExposure value is not display-linear.",
                   "outputPacked": chosen["outputPacked"], "outputRGB": chosen["outputRGB"],
                   "predictedNearestPacked": pixels[0]["predictedNearest"],
                   "nearestMatchesTileTexels": nearest, "truncationMatchesTileTexels": trunc,
                   "tileTexelsExamined": len(pixels)},
        "tileValidation": {"pixels": pixels,
                           "formula": "Final = max((Direct+Indirect)*(Albedo^2*DiffuseOverride.w+DiffuseOverride.rgb)/pi + Emissive, 0); dither=PCG16(atlasX,atlasY,StateFrameIndexMod8+2).x/65536; error=quantizationStep*L; stored=QuantizeForFloatRenderTarget(L+error*dither)"},
        "formulas": {
            "pageDecode": "page = PageBuffer[pageIndex*5 .. +4]; CardIndex=asuint(V0.x); SizeInTexels=V0.zw; CardUVRect=V1; PhysicalAtlasUVRect=V2; PhysicalAtlasCoord=floor(rect.xy*PhysicalAtlasSize+0.5)",
            "atlasUV": "uv = PhysicalAtlasUVRect.xy + InvPhysicalAtlasSize*(CoordInCardPage+0.5)",
            "cardUV": "CardUV = lerp(CardUVRect.xy, CardUVRect.zw, CoordInCardPage/SizeInTexels)",
            "cardLocal": "x=1-CardUV.x; local.xy=LocalExtent.xy*(1-2*CardUV); local.z=-(2*Depth-1)*LocalExtent.z",
            "world": "world = Origin + transpose(WorldToLocalRotation) * local",
            "combine": "Final_cachedPreExposed = max((Direct_cachedPreExposed+Indirect_cachedPreExposed)*(EncodedAlbedo^2*DiffuseOverride.w+DiffuseOverride.rgb)/pi + Emissive_cachedPreExposed, 0); CombineFinalLighting's DecodeSurfaceCacheAlbedo squares the encoded albedo",
            "albedoDecode": "rho = EncodedAlbedo^2 * DiffuseOverrideParameter.w + DiffuseOverrideParameter.xyz (DiffuseColorBoost=1). BC7 stores sqrt(rho).",
            "exposure": "cachedPreExposed = linear_radiance * CachedLightingPreExposure (1/16); unPreExposed = cachedPreExposed*16; viewPreExposed = unPreExposed*View.PreExposure (1/2048).",
            "store": "QuantizeForFloatRenderTarget(Final, int3(AtlasCoord, StateFrameIndexMod8+2)) with TargetFormatQuantizationError",
        },
        "sourceRefs": [
            {"file": "Engine/Shaders/Private/Lumen/LumenSceneLighting.usf", "lines": "477-507", "role": "CombineLumenSceneLightingCS dispatch, page/card decode, bilinear atlas samples, combine and R11G11B10 store"},
            {"file": "Engine/Shaders/Private/Lumen/LumenSceneLighting.usf", "lines": "471-474", "role": "BilinearClampedSampler and CardTiles declarations"},
            {"file": "Engine/Shaders/Private/Lumen/LumenCardCommon.ush", "lines": "83-129", "role": "FLumenCardData decode including PackedMeshCardsAxes and LocalExtent"},
            {"file": "Engine/Shaders/Private/Lumen/LumenCardCommon.ush", "lines": "170-199", "role": "FLumenCardPageData decode and PhysicalAtlasCoord"},
            {"file": "Engine/Shaders/Private/Lumen/LumenCardCommon.ush", "lines": "315-324", "role": "GetCardLocalPosition (CardUV flip and depth to local Z)"},
            {"file": "Engine/Shaders/Private/Lumen/LumenCardTile.ush", "lines": "15-33", "role": "FCardTileData packed word: page index and 4-bit tile coordinates"},
            {"file": "Engine/Source/Runtime/Renderer/Private/Lumen/LumenSceneLighting.cpp", "lines": "152,190", "role": "shader parameter and static bilinear clamped sampler state"},
            {"file": "Engine/Shaders/Private/Lumen/LumenDiffuseColorBoost.ush", "lines": "6-16", "role": "DiffuseColorBoost and DiffuseOverrideParameter albedo combine"},
            {"file": "Engine/Shaders/Private/Lumen/SurfaceCache/LumenSurfaceCache.ush", "lines": "50-60", "role": "CombineFinalLighting"},
            {"file": "Engine/Shaders/Private/Lumen/LumenFloatQuantization.ush", "lines": "14-29", "role": "QuantizeForFloatRenderTarget mantissa-clear quantization dither"},
            {"file": "Engine/Shaders/Private/RandomPCG.ush", "lines": "12-20", "role": "Rand3DPCG16"},
            {"file": "Engine/Shaders/Private/Lumen/LumenSceneDirectLighting.usf", "lines": "163",
             "role": "writes DirectLightingAtlas = Irradiance * CachedLightingPreExposure (cached-lighting pre-exposure, separate from View.PreExposure)"},
            {"file": "Engine/Shaders/Private/Lumen/SurfaceCache/LumenSurfaceCacheSampling.ush", "lines": "312-314",
             "role": "consumer multiplies Direct/Indirect/Final atlases by OneOverCachedLightingPreExposure to recover un-pre-exposed radiance"},
            {"file": "Engine/Source/Runtime/Renderer/Private/PostProcess/PostProcessEyeAdaptation.cpp", "lines": "201-242",
             "role": "CachedLightingPreExposure = 2^(-r.EyeAdaptation.CachedLightingPreExposure), default 4 EV = 1/16; captured pass 684 records 0.0625"},
        ],
        "coverageGaps": [
            "DumpGPU does not record the compiled shader permutation or a per-thread query. The card tile is a declared replay over captured state, not a witnessed dispatched hit.",
            "The capture has no mesh asset or material name for the Lumen card. Captured identity is CardIndex/MeshCardsIndex/SceneInstanceIndex plus the world OBB; the name is not invented.",
            "BC7/BC5 readback is exporter-expanded float (NativeFormatExpanded=True, StoredBytesAreNativeStorage=False), not native BC block bits. The replay snaps the decoded channels to the 1/255 UNORM8 grid to model hardware BC7/BC5 sampling; this is a modeling approximation, not a bit-exact block decode. About 11% of albedo texels are off the 1/255 grid (max 0.123/255), so the snap is not a lossless recovery. The selected albedo texel's raw expanded float16 is (0.3999023,0.3764648,0.3371582) and snaps to (102,96,86)/255. BC6H remains float.",
            "The card/page selection is a declared rule over the captured tile list; the GPU GroupId scheduling order is not captured per thread.",
            "The captured surface DispatchCardTilesIndirectArgs holds two triples, (62,1,1) and (3953,1,1); DumpGPU does not record which one the dispatch consumed. The replay evaluates the second (3953 groups).",
        ],
        "previews": {"atlasCrop": "figures/surface-cache-atlas-crop.png",
                     "heroProjection": "figures/surface-cache-hero-projection.png",
                     "metrics": "figures/surface-cache-preview-metrics.json",
                     "atlasCropBox": list(box)},
    }
    for ref in report["sourceRefs"]:
        src = ENGINE_ROOT / ref["file"]
        ref["sha256"] = sha_file(src) if src.exists() else None
        ref["engineRoot"] = str(ENGINE_ROOT)
    (out / "surface-cache-query.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"selectedGroup": chosen["group"], "card": card["cardIndex"],
                      "page": page["pageIndex"], "atlasCoord": chosen["atlasCoord"],
                      "screen": chosen["projection"]["screen"], "floorLike": chosen["floorLike"],
                      "luminance": chosen["luminance"], "nearestMatches": nearest,
                      "truncationMatches": trunc, "texels": len(pixels),
                      "out": str(out / "surface-cache-query.json")}, indent=1))


if __name__ == "__main__":
    main()
