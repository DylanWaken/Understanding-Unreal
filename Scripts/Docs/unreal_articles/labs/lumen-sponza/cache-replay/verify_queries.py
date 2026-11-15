"""Invariant checks over the two saved query reports.

Run:  python verify_queries.py
No GPU and no capture reads; this only re-checks the retained JSON evidence.
"""
import json
import math
import argparse
import struct
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--data', type=Path, default=Path(__file__).resolve().parents[6] / 'Docs/Unreal/assets/lumen-sponza/caches')
HERE = parser.parse_args().data
S = json.loads((HERE / "surface-cache-query.json").read_text(encoding="utf-8"))
W = json.loads((HERE / "world-cache-query.json").read_text(encoding="utf-8"))
preview = json.loads((HERE / 'figures/world-cache-preview-metrics.json').read_text())['neighborhood']
assert len(preview['drawnTiles']) == 8
assert len({tuple(t['pixelOrigin']) for t in preview['drawnTiles']}) == 8
assert {tuple(t['offsetBits']) for t in preview['drawnTiles']} == {(x,y,z) for x in range(2) for y in range(2) for z in range(2)}
png = (HERE / 'figures/world-cache-neighborhood.png').read_bytes()
assert list(struct.unpack_from('>II', png, 16)) == preview['imageDimensions'] == [564,276]


def finite(values):
    return all(math.isfinite(float(x)) for x in values)


def check_surface():
    assert S["capture"]["pass"]["Id"] == 694
    assert S["capture"]["pass"]["EventName"] == "CombineLighting CS"
    assert S["capture"]["pass"]["Pointer"].startswith("0004")
    assert S["lumenCardScene"]["PhysicalAtlasSize"] == [4096.0, 4096.0]
    assert S["page"]["bMapped"] is True
    assert S["page"]["sizeInTexels"] == [128.0, 128.0]
    assert all(0 <= x < 4096 for x in S["query"]["atlasCoord"])
    assert S["card"]["cardIndex"] == S["page"]["cardIndex"]
    assert S["meshCards"]["cardOffset"] <= S["card"]["cardIndex"] < (
        S["meshCards"]["cardOffset"] + S["meshCards"]["numCards"])
    assert S["sceneInstances"]["count"] >= 1
    assert finite(S["query"]["worldPoint"])
    v = S["values"]
    assert finite(v["encodedAlbedo"])
    assert finite(v["cachedPreExposedFinalBeforeQuantization"])
    assert finite(v["cachedPreExposedDirect"])
    assert finite(v["cachedPreExposedIndirect"])
    assert finite(v["cachedPreExposedEmissive"])
    # Encoded albedo squares to the decoded linear albedo (DiffuseOverride).
    ov = S["view"]["DiffuseOverrideParameter"]
    decoded = [a * a * ov[3] + ov[i] for i, a in enumerate(v["encodedAlbedo"])]
    assert max(abs(a - b) for a, b in zip(decoded, v["diffuseAlbedoRho"])) < 1e-6
    # The stored final is the shader combine of the cached-pre-exposed operands.
    recon = [(d + i) * l + e for d, i, l, e in zip(
        v["cachedPreExposedDirect"], v["cachedPreExposedIndirect"],
        v["lambertRhoOverPi"], v["cachedPreExposedEmissive"])]
    assert max(abs(a - b) for a, b in zip(
        recon, v["cachedPreExposedFinalBeforeQuantization"])) < 1e-3
    # Cached-lighting pre-exposure and View.PreExposure are separate spaces.
    inv = v["cachedLightingPreExposureInverse"]
    assert abs(v["cachedLightingPreExposure"] * inv - 1.0) < 1e-12
    for f, u, p in zip(v["cachedPreExposedFinalBeforeQuantization"],
                       v["unPreExposedFinal"], v["viewPreExposedFinal"]):
        assert abs(u - f * inv) < 1e-3
        assert abs(p - f * inv * v["viewPreExposure"]) < 1e-6
    assert any("LumenSceneDirectLighting.usf" in r["file"] for r in S["sourceRefs"])
    for key in ("albedo", "emissive", "direct", "indirect"):
        assert len(S["taps"][key]["weights"]) == 4
        assert len(S["taps"][key]["coordinates"]) == 4
        assert len(S["taps"][key]["values"]) == 4
        assert abs(sum(S["taps"][key]["weights"]) - 1.0) < 1e-6
    assert S["values"]["tileTexelsExamined"] == 64
    assert S["values"]["truncationMatchesTileTexels"] >= 60
    nearest = sum(p["nearestMatch"] for p in S["tileValidation"]["pixels"])
    trunc = sum(p["truncationMatch"] for p in S["tileValidation"]["pixels"])
    assert nearest == S["values"]["nearestMatchesTileTexels"]
    assert trunc == S["values"]["truncationMatchesTileTexels"]
    assert S["values"]["outputPacked"] != 0
    assert math.isfinite(v["viewPreExposure"]) and v["viewPreExposure"] > 0
    print(f"surface: pass={S['capture']['pass']['Id']} card={S['card']['cardIndex']} "
          f"page={S['page']['pageIndex']} atlas={S['query']['atlasCoord']} "
          f"tileMatches={S['values']['truncationMatchesTileTexels']}/64 "
          f"screen={[round(x, 2) for x in S['query']['projection']['screen']]} OK")


def check_world():
    assert W["capture"]["pass"]["Id"] == 747
    assert W["capture"]["pass"]["EventName"] == "TraceGlobalSDFAndApplyRadianceCache"
    assert W["capture"]["pass"]["Pointer"].startswith("0004")
    v = W["validity"]
    assert v["neighborCount"] == 8 and len(W["cacheLookup"]["neighbors"]) == 8
    assert v["allEightIndirectionAllocated"] is True
    assert v["uniqueProbeIndices"] == 8
    assert v["radianceTapCount"] == 32 and len(W["cacheLookup"]["radianceTaps"]) == 32
    assert abs(v["neighborWeightsSum"] - 1.0) < 1e-5
    assert W["cacheLookup"]["mipLevel"] == 0.0
    assert finite(W["cacheLookup"]["cacheRadiance"])
    assert all(n["indirectionPacked"] != 0xFFFFFFFF for n in W["cacheLookup"]["neighbors"])
    assert all(len(n["radianceSampling"]["taps"]) == 4 for n in W["cacheLookup"]["neighbors"])
    assert all(abs(sum(t["weight"] for t in n["radianceSampling"]["taps"]) - 1.0) < 1e-5
               for n in W["cacheLookup"]["neighbors"])
    assert all(n["depth"]["packed"] >= 0 for n in W["cacheLookup"]["neighbors"])
    assert all(math.isfinite(n["correctionFactor"]) for n in W["cacheLookup"]["neighbors"])
    c = W["cacheLookup"]
    # The grid coordinate is in cells; the raw position-corner vector is cm.
    assert c["positionMinusClipmapCornerUnits"].startswith("cm")
    assert "cell" in c["cellCoordinateUnits"]
    for delta, cell in zip(c["positionMinusClipmapCornerCM"], c["cellCoordinateFloat"]):
        assert abs(delta / c["clipmapCellSize"] - cell) < 1e-6
    for cell, base, frac in zip(c["cellCoordinateFloat"], c["bottomCell"],
                                c["fractionalWeights"]):
        assert base == math.floor(cell - 0.5)
        assert abs((cell - 0.5) - base - frac) < 1e-6
    # Screen-probe scene depth is captured in cm, not normalized or meters.
    assert W["queryWitness"]["sceneDepthUnits"].startswith("cm")
    assert W["queryWitness"]["sceneDepthCM"] == W["queryWitness"]["sceneDepth"]
    # cacheRadiance is un-pre-exposed; TraceRadiance is view-pre-exposed.
    pe = W["parameters"]["view"]["PreExposure"]
    for model, cache in zip(W["comparison"]["forwardModel"], c["cacheRadiance"]):
        assert abs(model - cache * pe) < 1e-9
    assert W["comparison"]["forwardModelSpace"].startswith("view-pre-exposed")
    assert any(line["line"] == 851 for ev in W["sourceEvidence"]
               if ev["file"].endswith("LumenScreenProbeTracing.usf")
               for line in ev["lines"])
    comp = W["comparison"]
    assert comp["depthAbsDiff"] <= 0.001
    assert comp["forwardConsistent"] is True
    assert comp["equalityClaimed"] is False
    print(f"world: pass={W['capture']['pass']['Id']} lane={W['selection']['selectedLane']} "
          f"probe={W['queryWitness']['screenProbeIndex']} clipmap={W['cacheLookup']['selectedClipmap']} "
          f"neighbors=8 taps=32 depth={comp['replayedCacheDepth']} captured={comp['capturedDistance']} "
          f"absDiff={comp['depthAbsDiff']} forwardConsistent={comp['forwardConsistent']} OK")


if __name__ == "__main__":
    check_surface()
    check_world()
    print("all query invariants hold")
