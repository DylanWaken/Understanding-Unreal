"""Independent raw-byte spot check of the published temporal screen-probe SH records.

This does NOT import or execute the published decoder.  It parses the DumpGPU
records, reads the named resource binaries directly, decodes R11G11B10 / half4
storage itself, and projects the stored translated-world positions with the
captured View matrix.
"""
import json
import math
import struct
from pathlib import Path

import numpy as np

REVIEW = Path(r"D:\UnrealUnderstanding-Review\temporal-sh-review")
LIGHT_ROOT = Path(r"D:\UnrealUnderstanding-Temporal\output\gpu-dump\raw\20261115-101057")
MOTION_ROOT = Path(r"D:\UnrealUnderstanding-Motion\output\gpu-dump\raw\20261115-104035")
PUB = Path(r"D:\UnrealUnderstanding\Docs\Unreal\assets\lumen-sponza\temporal")

STATES = [
    ("light", "baseline", LIGHT_ROOT / "baseline", PUB / "screen-sh" / "publish/data/states/baseline.json"),
    ("light", "lightstep-032", LIGHT_ROOT / "lightstep-032", PUB / "screen-sh" / "publish/data/states/lightstep-032.json"),
    ("motion", "camera-064", MOTION_ROOT / "camera-064", PUB / "motion-screen-sh" / "publish/data/states/camera-064.json"),
]


def load_concat(path):
    s = Path(path).read_text(encoding="utf-8-sig")
    dec = json.JSONDecoder()
    out, i = [], 0
    while i < len(s):
        while i < len(s) and s[i].isspace():
            i += 1
        if i >= len(s):
            break
        o, j = dec.raw_decode(s, i)
        out.append(o)
        i = j
    return out


def meta(base, mid):
    return json.loads((base / "StructuresMetadata" / f"{mid}.json").read_text(encoding="utf-8-sig"))


def member(m, name):
    for x in m["Members"]:
        if x["Name"] == name:
            return x
    raise KeyError(name)


def raw_field(buf, m, name):
    x = member(m, name)
    off, n = int(x["Offset"]), int(x["NumRows"]) * int(x["NumColumns"])
    bt = x["BaseType"]
    if bt == "UBMT_FLOAT32":
        return np.frombuffer(buf[off:off + 4 * n], dtype="<f4").reshape(int(x["NumRows"]), int(x["NumColumns"]))
    if bt in ("UBMT_UINT32", "UBMT_INT32"):
        return np.frombuffer(buf[off:off + 4 * n], dtype="<u4" if bt == "UBMT_UINT32" else "<i4").reshape(
            int(x["NumRows"]), int(x["NumColumns"]))
    raise TypeError((name, bt))


def decode_r11g11b10(words):
    w = np.asarray(words, dtype=np.uint32)
    out = []
    for shift, bits, mask in ((0, 6, 0x7FF), (11, 6, 0x7FF), (22, 5, 0x3FF)):
        v = (w >> shift) & mask
        e = (v >> bits) & 0x1F
        m = v & ((1 << bits) - 1)
        scale = float(1 << bits)
        val = np.where(e == 0, (m / scale) * (2.0 ** -14),
                       (1.0 + m / scale) * np.exp2(e.astype(np.int32) - 15))
        out.append(val.astype(np.float64))
    return np.stack(out, axis=-1)


def reverse_bits32(x):
    x = int(x) & 0xFFFFFFFF
    x = ((x & 0x55555555) << 1) | ((x & 0xAAAAAAAA) >> 1)
    x = ((x & 0x33333333) << 2) | ((x & 0xCCCCCCCC) >> 2)
    x = ((x & 0x0F0F0F0F) << 4) | ((x & 0xF0F0F0F0) >> 4)
    x = ((x & 0x00FF00FF) << 8) | ((x & 0xFF00FF00) >> 8)
    return ((x << 16) | (x >> 16)) & 0xFFFFFFFF


def jitter(idx):
    return int((idx / 8.0) % 1.0 * 16.0), int(((reverse_bits32(idx) >> 16) / 65536.0) * 16.0)


def main():
    results = {"states": {}, "ok": True}
    for group, name, base, state_json_path in STATES:
        passes = load_concat(base / "Base/Passes.json")
        refs = load_concat(base / "Base/ReferencedStructs.json")
        descs = {d["UniqueResourceName"]: d for d in load_concat(base / "Base/ResourceDescs.json")}
        subs = {}
        for s in load_concat(base / "Base/SubresourceLayouts.json"):
            if s.get("MipLevel") == 0:
                subs[s["Resource"]] = s
        spawn = [p for p in passes if p.get("EventName") == "AdaptivePlacementSpawn"][-1]
        shp = [p for p in passes if p.get("EventName", "").startswith("ScreenProbeConvertToIrradiance")][-1]

        # nested FScreenProbeParameters block, from raw parameter metadata
        smeta = meta(base, spawn["ParametersMetadata"])
        nested = member(smeta, "ScreenProbeParameters")
        nested_off = int(nested["Offset"])
        nmeta = meta(base, nested["StructMetadata"])
        pbuf = (base / "Structures" / f"{spawn['Parameters']}.bin").read_bytes()
        nbuf = pbuf[nested_off:nested_off + int(nmeta["Size"])]
        num_uniform = int(raw_field(nbuf, nmeta, "NumUniformScreenProbes")[0, 0])
        max_adapt = int(raw_field(nbuf, nmeta, "MaxNumAdaptiveProbes")[0, 0])
        fixed_jitter = int(raw_field(nbuf, nmeta, "FixedJitterIndex")[0, 0])
        downsample = int(raw_field(nbuf, nmeta, "ScreenProbeDownsampleFactor")[0, 0])
        view_size = [int(x) for x in raw_field(nbuf, nmeta, "ScreenProbeAtlasViewSize")[0]]
        buffer_size = [int(x) for x in raw_field(nbuf, nmeta, "ScreenProbeAtlasBufferSize")[0]]

        # captured View, from the SH pass referenced structs
        vref = next(r for r in refs if r.get("Pass") == shp["Pointer"])["ReferencedStructs"]["View"]
        vmeta = meta(base, vref["Metadata"])
        vbuf = (base / "Structures" / f"{vref['Content']}.bin").read_bytes()
        M = np.asarray(raw_field(vbuf, vmeta, "TranslatedWorldToClip"), dtype=np.float64)
        vr = raw_field(vbuf, vmeta, "ViewRectMin")[0, :2].astype(np.float64)
        vs = raw_field(vbuf, vmeta, "ViewSizeAndInvSize")[0, :2].astype(np.float64)
        pre = (raw_field(vbuf, vmeta, "PreViewTranslationHigh")[0, :3]
               + raw_field(vbuf, vmeta, "PreViewTranslationLow")[0, :3]).astype(np.float64)
        preexp = float(raw_field(vbuf, vmeta, "PreExposure")[0, 0])
        near = float(raw_field(vbuf, vmeta, "NearPlane")[0, 0])
        state_frame = int(raw_field(vbuf, vmeta, "StateFrameIndex")[0, 0])
        frame_number = int(raw_field(vbuf, vmeta, "FrameNumber")[0, 0])

        # resolve every operand by resource name, then the raw subresource record
        def operand(p, suffix):
            for out in p.get("OutputResources", []):
                uid = out.split(".")[0]
                nm = (descs.get(uid) or {}).get("Name", "")
                if nm.endswith(suffix):
                    return uid, nm
            raise KeyError(suffix)

        def tex(p, suffix):
            uid, nm = operand(p, suffix)
            s = subs[uid]
            h, w = int(s["ExtentY"]), int(s["ExtentX"])
            data = (base / s["File"]).read_bytes()
            return uid, nm, s, data, w, h

        def buf(uid):
            cands = sorted((base / "Resources").glob(f"{uid}.*.bin"))
            if len(cands) != 1:
                raise RuntimeError((uid, [c.name for c in cands]))
            return cands[0].read_bytes()

        uid_a, _, sa, da, wa, ha = tex(shp, "ScreenProbeRadianceSHAmbient")
        uid_d, _, sd, dd, wd, hd = tex(shp, "ScreenProbeRadianceSHDirectional")
        assert sa["StoredFormat"] == "PF_FloatR11G11B10" and sd["StoredFormat"] == "PF_FloatRGBA"
        assert sd["RowPitchBytes"] == wd * 8 and wd == 6 * wa
        amb = decode_r11g11b10(np.frombuffer(da, dtype="<u4").reshape(ha, wa))
        dir_tex = np.frombuffer(dd, dtype="<f2").reshape(hd, wd, 4).astype(np.float64)

        def sh_at(ax, ay):
            sh = np.zeros((3, 9), dtype=np.float64)
            sh[:, 0] = amb[ay, ax, :]
            for c in range(3):
                sh[c, 1:5] = dir_tex[ay, ax + 2 * c * wa, :]
                sh[c, 5:9] = dir_tex[ay, ax + (2 * c + 1) * wa, :]
            return sh

        _, _, spos, dpos, wp, hp = tex(spawn, "ScreenProbeTranslatedWorldPosition")
        pos = np.frombuffer(dpos, dtype="<f4").reshape(hp, wp, 4).astype(np.float64)
        _, _, sdep, ddep, wd2, hd2 = tex(spawn, "ScreenProbeSceneDepth")
        dep = np.frombuffer(ddep, dtype="<u4").reshape(hd2, wd2).view(np.float32)
        _, _, _, dhdr, wh, hh = tex(spawn, "ScreenTileAdaptiveProbeHeader")
        hdr = np.frombuffer(dhdr, dtype="<u4").reshape(hh, wh)
        uid_n, _ = operand(spawn, "NumAdaptiveScreenProbes")
        uid_ad, _ = operand(spawn, "AdaptiveScreenProbeData")
        count_raw = int(np.frombuffer(buf(uid_n), dtype="<u4")[0])
        adata = np.frombuffer(buf(uid_ad), dtype="<u4")
        active_bound = max(0, min(count_raw, max_adapt, max(0, view_size[0] * view_size[1] - num_uniform)))

        flat = (np.mgrid[0:hd2, 0:wd2][0] * wd2 + np.mgrid[0:hd2, 0:wd2][1]).reshape(-1)
        depf = dep.reshape(-1)
        active_mask = (depf > 0) & ((flat < num_uniform) |
                                    ((flat >= num_uniform) & ((flat - num_uniform) < active_bound)))

        def project(p):
            clip = np.array([p[0], p[1], p[2], 1.0]) @ M
            w = float(clip[3])
            ndc = clip[:2] / w
            return (float(vr[0] + (ndc[0] * 0.5 + 0.5) * vs[0]),
                    float(vr[1] + (0.5 - ndc[1] * 0.5) * vs[1]), w)

        active = []       # (kind, index, atlas, position, sh)
        errors = {"uniform": [], "adaptive": []}
        for k in np.nonzero(active_mask)[0]:
            lin = int(k)
            ax, ay = lin % wd2, lin // wd2
            p = pos[ay, ax, :3]
            if not np.all(np.isfinite(p)):
                continue
            kind = "uniform" if lin < num_uniform else "adaptive"
            idx = lin if kind == "uniform" else lin - num_uniform
            pr = project(p)
            if kind == "uniform":
                jx, jy = jitter(fixed_jitter if fixed_jitter >= 0 else state_frame % 8)
                ex = int(min(vr[0] + ax * downsample + jx, vr[0] + vs[0] - 1)) + 0.5
                ey = int(min(vr[1] + ay * downsample + jy, vr[1] + vs[1] - 1)) + 0.5
            else:
                word = int(adata[idx])
                ex = float(word & 0xFFFF) + 0.5
                ey = float((word >> 16) & 0xFFFF) + 0.5
            errors[kind].append((abs(pr[0] - ex), abs(pr[1] - ey)))
            active.append((kind, idx, [ax, ay], p.copy(), sh_at(ax, ay), pr))

        # published record, keyed by storage identity (kind,index)
        st = json.loads(state_json_path.read_text(encoding="utf-8-sig"))
        pub = {}
        for r in st["active"]:
            key = (r["kind"], r["uniform_index"] if r["kind"] == "uniform" else r["adaptive_index"])
            pub[key] = r

        # independent counts
        stale = (depf != 0) & (flat >= num_uniform + active_bound)
        unlit = (depf < 0) & (flat < num_uniform)
        counts = {
            "num_uniform": num_uniform,
            "max_adapt": max_adapt,
            "atlas_view": view_size,
            "atlas_buffer": buffer_size,
            "captured_num_adaptive": count_raw,
            "active_bound": active_bound,
            "active_uniform": sum(1 for a in active if a[0] == "uniform"),
            "active_adaptive": sum(1 for a in active if a[0] == "adaptive"),
            "active_total": len(active),
            "inactive_unlit": int(np.count_nonzero(unlit)),
            "stale_finite_adaptive": int(np.count_nonzero(stale)),
            "finite_nonzero_storage": int(np.count_nonzero(depf != 0)),
            "header_nonzero": int(np.count_nonzero(hdr)),
            "header_sum": int(hdr.sum()),
            "preexposure": preexp,
            "near": near,
            "frame_number": frame_number,
            "state_frame_index": state_frame,
            "fixed_jitter_index": fixed_jitter,
            "downsample": downsample,
        }
        pub_counts = st["counts"]
        count_pairs = [
            ("captured_num_adaptive", "captured_num_adaptive_screen_probes"),
            ("active_bound", "active_adaptive_bound"),
            ("active_uniform", "active_uniform_count"),
            ("active_adaptive", "active_adaptive_count"),
            ("active_total", "active_total_count"),
            ("inactive_unlit", "inactive_unlit_uniform_count"),
            ("stale_finite_adaptive", "stale_finite_adaptive_count"),
            ("finite_nonzero_storage", "finite_nonzero_storage_count"),
            ("header_nonzero", "adaptive_header_nonzero_tiles"),
            ("header_sum", "adaptive_header_value_sum"),
        ]
        count_diff = {a: counts[a] - int(pub_counts[b]) for a, b in count_pairs}
        count_ok = all(v == 0 for v in count_diff.values())

        # all-active 27-value comparison
        maxdiff = 0.0
        bad = 0
        for kind, idx, atlas, p, sh, pr in active:
            if (kind, idx) not in pub:
                bad += 1
                continue
            pv = np.asarray(pub[(kind, idx)]["sh"], dtype=np.float64)
            d = np.abs(sh.reshape(-1) - pv)
            maxdiff = max(maxdiff, float(d.max()))
            bad += int(np.count_nonzero(d > 1e-6))
        missing = len(set(pub) - {(a[0], a[1]) for a in active})

        # a few named probes, with all 27 values side by side
        selected = []
        picks = [
            ("uniform", num_uniform * 0 + (42 * wd2 + 27)),                 # floor-1 atlas [27,42]
            ("uniform", 16 * wd2 + 7),                                       # arcade-left atlas [7,16]
            ("uniform", 16 * wd2 + 60),                                      # arcade-right [60,16]
            ("adaptive", 0),
            ("adaptive", active_bound // 2),
            ("adaptive", max(0, active_bound - 1)),
        ]
        bykey = {(a[0], a[1]): a for a in active}
        for kind, key in picks:
            if kind == "uniform":
                # keys in the loop are linear indices, uniform index == linear
                a = bykey.get(("uniform", key))
            else:
                a = bykey.get(("adaptive", key))
            if a is None:
                continue
            kind2, idx2, atlas, p, sh, pr = a
            pv = np.asarray(pub[(kind2, idx2)]["sh"], dtype=np.float64)
            selected.append({
                "kind": kind2,
                "index": idx2,
                "atlas": atlas,
                "raw_sh": [round(float(x), 8) for x in sh.reshape(-1)],
                "published_sh": [round(float(x), 8) for x in pv],
                "max_abs_diff": round(float(np.max(np.abs(sh.reshape(-1) - pv))), 9),
                "projected_screen_xy": [round(pr[0], 6), round(pr[1], 6)],
                "published_screen_xy": pub[(kind2, idx2)]["screen_xy"],
                "projection_err_px": [round(abs(pr[0] - pub[(kind2, idx2)]["screen_xy"][0]), 6),
                                      round(abs(pr[1] - pub[(kind2, idx2)]["screen_xy"][1]), 6)],
                "world_cm": [round(float(x), 6) for x in (p - pre)],
            })

        e_u = max((max(x) for x in errors["uniform"]), default=0.0)
        e_a = max((max(x) for x in errors["adaptive"]), default=0.0)
        ok = bool(count_ok and maxdiff <= 1e-6 and bad == 0 and missing == 0
                  and e_u < 1.0 and e_a < 1.0)
        results["states"][name] = {
            "group": group,
            "source_root": str(base),
            "operands": {
                "spawn_pass": {"id": spawn["Id"], "pointer": spawn["Pointer"]},
                "sh_pass": {"id": shp["Id"], "pointer": shp["Pointer"]},
                "sh_ambient": {"resource": uid_a, "file": sa["File"], "format": sa["StoredFormat"],
                               "extent": [wa, ha]},
                "sh_directional": {"resource": uid_d, "file": sd["File"], "format": sd["StoredFormat"],
                                   "extent": [wd, hd], "row_pitch": sd["RowPitchBytes"]},
                "num_adaptive": {"resource": uid_n},
                "adaptive_data": {"resource": uid_ad},
            },
            "counts_raw": counts,
            "count_diff_vs_published": count_diff,
            "count_ok": count_ok,
            "all_active_sh_max_abs_diff": maxdiff,
            "all_active_sh_values_over_1e6": bad,
            "missing_published_keys": missing,
            "selected_probes": selected,
            "max_uniform_center_error_px": round(e_u, 9),
            "max_adaptive_center_error_px": round(e_a, 9),
            "ok": ok,
        }
        if not ok:
            results["ok"] = False
        print(f"{name}: active={counts['active_total']} stale={counts['stale_finite_adaptive']} "
              f"shmax={maxdiff:.3e} bad={bad} centers=({e_u:.2e},{e_a:.2e}) ok={ok}")

    REVIEW.mkdir(parents=True, exist_ok=True)
    (REVIEW / "raw-spotcheck.json").write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
    print("wrote", REVIEW / "raw-spotcheck.json", "ok=", results["ok"])


if __name__ == "__main__":
    main()
