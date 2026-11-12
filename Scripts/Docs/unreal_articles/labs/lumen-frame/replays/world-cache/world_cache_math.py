"""CPU reconstruction of the pinned world-radiance-cache lookup.

This module does not synthesize GPU evidence. A caller must supply arrays and
uniform values decoded from a particular captured resource version, and retain
their provenance. It evaluates only the cache lookup, not preceding SDF/hair
tracing or subsequent leaking, fog, exposure and output quantization.

The arithmetic order follows the shipped UE 5.8.1 shader:

* eight corner probes are sampled with ``SampleRadianceCacheProbeWithParallaxCorrection``;
* each probe sample uses the 2x2 bilinear footprint of the final radiance atlas at
  the clamped mip (this capture has ``FinalRadianceAtlasMaxMip == 0``);
* the eight samples are combined with the shader's nested Z/Y/X ``lerp`` order,
  not with a reassociated weighted sum;
* the same nested order is used for ``TraceHitDistance``, which is the lookup's
  depth output and the captured GPU observable used for validation.

Each bilinear footprint contributes four radiance taps, so the selected lookup
contains 8 x 4 = 32 taps, 8 depth reads and 8 sphere-parallax corrections.
"""
import math
import struct

import numpy as np

F = np.float32


def equal_area(uv):
    a, b = np.asarray(uv, dtype=np.float32) * F(2) - F(1)
    d = F(1) - abs(a) - abs(b)
    r = F(1) - abs(d)
    phi = F(0) if r == 0 else F(math.pi / 4) * ((abs(b) - abs(a)) / r + F(1))
    radial = r * np.sqrt(F(2) - r * r)
    return np.array([radial * np.sign(a) * abs(np.cos(phi)),
                     radial * np.sign(b) * abs(np.sin(phi)),
                     np.sign(d) * (F(1) - r * r)], dtype=np.float32)


def inverse_equal_area(direction):
    v = np.asarray(direction, dtype=np.float32)
    v = v / np.sqrt(np.dot(v, v))
    a = np.abs(v)
    r = np.sqrt(F(1) - a[2])
    x = min(a[0], a[1]) / (max(a[0], a[1]) + F(2.0**-64))
    # Mathematical coefficients of the source's minimax atan approximation.
    coeff = np.array([0.000004067585662467885, 0.6362265452740161,
                      0.006157201789828021, -0.24733373328126895,
                      0.08817706647753163, 0.04190388180291657,
                      -0.02513909723434835], dtype=np.float32)
    phi = coeff[-1]
    for c in coeff[-2::-1]:
        phi = F(c + phi * x)
    if a[0] < a[1]:
        phi = F(1) - phi
    uv = np.array([r - phi * r, phi * r], dtype=np.float32)
    if v[2] < 0:
        uv = F(1) - uv[::-1]
    # copysign preserves the original signed zero convention.
    uv = np.copysign(uv, v[:2])
    return uv * F(0.5) + F(0.5)


def acos_fast(v):
    v = F(v)
    x = abs(v)
    y = (F(math.pi / 2) - F(0.156583) * x) * np.sqrt(F(1) - x)
    return y if v >= 0 else F(math.pi) - y


def ray_from_record(ray_info, trace_xy, jitter, trace_resolution,
                    max_importance_resolution, structured_importance):
    """Return the actual cell/jitter direction and its source cone footprint."""
    if structured_importance:
        cell = np.array([int(ray_info) & 63, (int(ray_info) >> 6) & 63])
        level = (int(ray_info) >> 12) & 15
        size = int(max_importance_resolution) >> level
    else:
        cell = np.asarray(trace_xy, dtype=int)
        level = None
        size = int(trace_resolution)
    if size <= 0:
        raise ValueError("invalid angular cell resolution")
    uv = (cell.astype(np.float32) + np.asarray(jitter, dtype=np.float32)) / F(size)
    alpha = acos_fast(F(1) - F(1) / F(size * size))
    return {"packedRayInfo": int(ray_info), "structuredImportance": bool(structured_importance),
            "cell": cell.tolist(), "level": level, "cellResolution": size,
            "jitter": list(map(float, jitter)), "uv": uv.tolist(),
            "direction": equal_area(uv).tolist(), "coneHalfAngle": float(alpha),
            "angularCellSolidAngle": 4 * math.pi / (size * size),
            "coneSolidAngleFromApproximateAngle": 2 * math.pi * (1 - math.cos(float(alpha)))}


def decode_unsigned_float(bits, mantissa_bits):
    exponent = bits >> mantissa_bits
    mantissa = bits & ((1 << mantissa_bits) - 1)
    if exponent == 0:
        return math.ldexp(mantissa, -14 - mantissa_bits)
    if exponent == 31:
        return float("inf") if mantissa == 0 else float("nan")
    return math.ldexp(1 + mantissa / (1 << mantissa_bits), exponent - 15)


def decode_r11g11b10(value):
    value = int(value)
    return np.array([decode_unsigned_float(value & 2047, 6),
                     decode_unsigned_float((value >> 11) & 2047, 6),
                     decode_unsigned_float((value >> 22) & 1023, 5)], dtype=np.float32)


def decode_probe_depth(value):
    value = int(value)
    hit = value != 65535
    distance = struct.unpack("<e", struct.pack("<H", value & 0x7FFE))[0] if hit else 65503.0
    return {"packed": value, "hit": hit, "frontface": bool(value & 0x8000),
            "twoSided": bool(value & 1), "distance": float(distance)}


def lerp(a, b, t):
    """HLSL lerp: a + t*(b-a), evaluated in float32."""
    return F(a) + F(t) * (F(b) - F(a))


def bilinear_packed_radiance(packed, uv):
    """Sample one mip's packed R11G11B10 array and retain all four operands."""
    height, width = packed.shape
    q = np.asarray(uv, dtype=np.float32) * np.array([width, height], dtype=np.float32) - F(0.5)
    lo = np.floor(q).astype(int)
    a = q - lo.astype(np.float32)
    taps, result = [], np.zeros(3, dtype=np.float32)
    for dx, dy in [(0, 0), (1, 0), (0, 1), (1, 1)]:
        x = min(width - 1, max(0, int(lo[0] + dx)))
        y = min(height - 1, max(0, int(lo[1] + dy)))
        weight = (a[0] if dx else F(1) - a[0]) * (a[1] if dy else F(1) - a[1])
        word = int(packed[y, x])
        color = decode_r11g11b10(word)
        result += color * weight
        taps.append({"xy": [x, y], "weight": float(weight), "packed": word,
                     "decodedRadiance": color.tolist()})
    return result, {"dimensions": [width, height], "uv": list(map(float, uv)),
                    "texelCoordinate": q.tolist(), "taps": taps, "value": result.tolist()}


def _nested_lookup(values, weights):
    """The shader's Z/Y/X lerp order over the eight corner probes."""
    f000, f001, f010, f011, f100, f101, f110, f111 = values
    az, ay, ax = F(weights[2]), F(weights[1]), F(weights[0])
    z00 = lerp(f000, f001, az)
    z01 = lerp(f010, f011, az)
    z10 = lerp(f100, f101, az)
    z11 = lerp(f110, f111, az)
    y0 = lerp(z00, z01, ay)
    y1 = lerp(z10, z11, ay)
    return lerp(y0, y1, ax)


def lookup(query, params, indirection_zyx, offsets, radiance, depth_yx):
    """Replay a valid eight-neighbor screen-probe cache lookup.

    Required params use the actual shader uniform names. ``offsets`` is the
    captured half4 buffer decoded as float16 in shape (NumElements, 4); the
    typed storage is recorded in the caller. ``radiance`` is the packed
    R11G11B10 atlas at mip 0. ``depth_yx`` is the R16_UINT depth atlas.
    Invalid neighbors or sphere geometry stop rather than fabricating samples.
    Sky visibility and stochastic reflection variants are explicitly excluded.
    """
    if params.get("UseSkyVisibility", 0):
        raise ValueError("sky-visibility resolve requires its separate captured operands")
    if int(params["FinalRadianceAtlasMaxMip"]) != 0:
        raise ValueError("this replay is pinned to the captured max-mip 0 atlas")
    x = np.asarray(query["translatedWorldPosition"], dtype=np.float32)
    omega = np.asarray(query["direction"], dtype=np.float32)
    alpha = F(query["coneHalfAngle"])
    clipmaps = np.asarray(params["ClipmapCornerTWSAndCellSize"], dtype=np.float32)
    settings = np.asarray(params["RadianceProbeSettings"], dtype=np.float32)
    resolution = int(params["RadianceProbeClipmapResolution"])
    fade_scale = F(params["InvClipmapFadeSize"])
    candidates = []
    selected = None
    for level in range(int(params["NumRadianceProbeClipmaps"])):
        c, size = clipmaps[level, :3], clipmaps[level, 3]
        q = (x - c) / size
        bottom = np.clip((q - F(0.5)) * fade_scale, F(0), F(1))
        top = np.clip((F(resolution) - F(0.5) - q) * fade_scale, F(0), F(1))
        edge = min(float(np.min(bottom)), float(np.min(top)))
        candidates.append({"level": level, "coordinate": q.tolist(),
                           "bottomFades": bottom.tolist(), "topFades": top.tolist(),
                           "edgeConfidence": edge})
        if edge > 0:
            selected = level
            break
    if selected is None:
        raise ValueError("actual query has no valid clipmap coverage")
    level = selected
    c, size = clipmaps[level, :3], clipmaps[level, 3]
    q = (x - c) / size - F(0.5)
    base = np.floor(q).astype(int)
    a = q - base.astype(np.float32)
    trace_resolution = int(params["RadianceProbeResolution"])
    final_resolution = int(params["FinalProbeResolution"])
    max_mip = int(params["FinalRadianceAtlasMaxMip"])
    footprint = np.sqrt(F(1) - np.cos(alpha)) * F(trace_resolution)
    mip = F(np.clip(np.log2(footprint), F(0), F(max_mip)))
    if float(mip) != 0.0:
        raise ValueError("captured maxMip is 0 but computed mip is not 0")
    raw_tmin = settings[level, 0]
    tmin = F(1000) if raw_tmin == 0 else raw_tmin
    radius = F(params["ReprojectionRadiusScale"]) * tmin
    inv_atlas = np.asarray(params["InvProbeFinalRadianceAtlasResolution"], dtype=np.float32)
    exposure_inverse = F(params["RadianceCacheOneOverCachedLightingPreExposure"])
    neighbors = []
    radiance_values = []
    depth_values = []
    taps_flat = []
    total_distance_weighted = F(0)
    for ex, ey, ez in [(i, j, k) for i in range(2) for j in range(2) for k in range(2)]:
        epsilon = np.array([ex, ey, ez], dtype=int)
        coord = base + epsilon
        icoord = coord + np.array([level * resolution, 0, 0])
        if np.any(icoord < 0) or any(icoord[k] >= indirection_zyx.shape[2-k] for k in range(3)):
            raise ValueError("indirection coordinate outside captured volume")
        word = int(indirection_zyx[icoord[2], icoord[1], icoord[0]])
        if word == 0xFFFFFFFF:
            raise ValueError("unallocated neighbor; no source-supported renormalization")
        if word & 0xFFF80000:
            raise ValueError(f"illegal indirection encoding {word:#010x}")
        index = word & 0xFFFF
        if index >= offsets.shape[0]:
            raise ValueError("probe index outside captured offset buffer")
        weight = F(1)
        for axis in range(3):
            weight *= a[axis] if epsilon[axis] else F(1) - a[axis]
        offset = np.asarray(offsets[index, :3], dtype=np.float32)
        center = c + (coord.astype(np.float32) + F(0.5)) * size + offset
        v = x - center
        A = np.dot(omega, omega)
        B = F(2) * np.dot(omega, v)
        C = np.dot(v, v) - radius * radius
        discriminant = B * B - F(4) * A * C
        if discriminant < 0:
            raise ValueError("sphere proxy has no intersection; do not invent fallback")
        t = (-B + np.sqrt(discriminant)) / (F(2) * A)
        reprojected = x + t * omega - center
        denominator = radius * np.dot(reprojected, omega)
        if denominator <= 0:
            raise ValueError("invalid sphere-parallax denominator")
        correction = t * t / denominator
        uv = inverse_equal_area(reprojected)
        tile = np.array([index & int(params["ProbeAtlasResolutionModuloMask"]),
                         index >> int(params["ProbeAtlasResolutionDivideShift"])])
        atlas_uv = (F(final_resolution) * tile.astype(np.float32)
                    + F(trace_resolution) * uv + F(1 << max_mip)) * inv_atlas
        stored, sampling = bilinear_packed_radiance(radiance, atlas_uv)
        value = stored * exposure_inverse * correction
        depth_coord = (tile * trace_resolution + uv * F(trace_resolution)).astype(int)
        if (depth_coord[0] < 0 or depth_coord[1] < 0
                or depth_coord[0] >= depth_yx.shape[1] or depth_coord[1] >= depth_yx.shape[0]):
            raise ValueError("depth atlas coordinate outside captured texture")
        depth = decode_probe_depth(depth_yx[depth_coord[1], depth_coord[0]])
        total_distance_weighted += F(depth["distance"]) * weight
        for tap in sampling["taps"]:
            taps_flat.append({"neighborIndex": len(neighbors),
                              "offsetBits": epsilon.tolist(), "probeIndex": index,
                              "atlasXY": tap["xy"], "bilinearWeight": tap["weight"],
                              "neighborWeight": float(weight),
                              "effectiveWeight": float(weight) * tap["weight"],
                              "packed": tap["packed"],
                              "decodedRadiance": tap["decodedRadiance"]})
        radiance_values.append(value)
        depth_values.append(F(depth["distance"]))
        neighbors.append({"offsetBits": epsilon.tolist(), "cell": coord.tolist(),
            "indirectionXYZ": icoord.tolist(), "indirectionPacked": word,
            "probeIndex": index, "adaptiveCount": (word >> 16) & 7,
            "probeOffsetRawHalf4": np.asarray(offsets[index], dtype=np.float32).tolist(),
            "trilinearWeight": float(weight), "probeOffset": offset.tolist(),
            "probeTranslatedWorldPosition": center.tolist(),
            "sphereRadius": float(radius), "quadraticABC": [float(A), float(B), float(C)],
            "discriminant": float(discriminant), "farIntersection": float(t),
            "reprojectedDirection": reprojected.tolist(),
            "correctionDenominator": float(denominator),
            "correctionFactor": float(correction), "probeUV": uv.tolist(),
            "atlasTile": tile.tolist(), "radianceSampling": sampling,
            "storedRadiance": stored.tolist(),
            "exposureInverse": float(exposure_inverse),
            "radianceBeforeWeight": value.tolist(),
            "depthAtlasXY": depth_coord.tolist(), "depth": depth})
    cache_radiance = _nested_lookup(radiance_values, a)
    cache_depth = _nested_lookup(depth_values, a)
    return {"scope": "source-derived actual-query cache lookup; not the complete TraceVoxels result",
            "query": query, "clipmapCandidates": candidates, "selectedClipmap": level,
            "clipmapCornerTWSAndCellSize": c.tolist(), "clipmapCellSize": float(size),
            "minimumLocalTraceDistance": float(raw_tmin + size * F(math.sqrt(3))),
            "probeCoordFloat": (x - c).tolist(), "bottomCell": base.tolist(),
            "fractionalWeights": a.tolist(), "angularFootprintTexels": float(footprint),
            "mipLevel": float(mip), "radianceTaps": taps_flat,
            "radianceTapCount": len(taps_flat), "neighbors": neighbors,
            "sumWeights": float(sum(n["trilinearWeight"] for n in neighbors)),
            "cacheRadiance": cache_radiance.tolist(),
            "cacheTraceHitDistance": float(cache_depth),
            "cacheTraceHitDistanceWeightedSum": float(total_distance_weighted),
            "limitations": ["eight-neighbor screen-probe route only; sky visibility disabled",
                "CPU float32 expressions can differ from compiled GPU FMA/half behavior",
                "GPU cache-only shader-local subtotal is not independently captured"]}
