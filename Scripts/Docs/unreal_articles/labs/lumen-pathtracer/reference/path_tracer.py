#!/usr/bin/env python3
"""A small, original, dependency-free CPU path tracer for teaching transport.

This file is an *educational reproduction* written from scratch for the Lumen
math essay.  It is not Unreal Engine code, it is not a capture from Unreal, and
it does not attempt to reproduce any particular Unreal scene.  It renders the
essay's 4 x 5 x 3 m CPU reference room in a smaller form: a red wall, a green
wall, a neutral floor/ceiling/back wall, an open black front, one ceiling area
emitter, and two blocks.  All surfaces are Lambertian; the tall right block
that Unreal renders with a glossy/metallic material is deliberately reduced to
a neutral diffuse albedo and labelled as a pedagogical approximation.  The
renderer implements:

  * cosine-weighted Lambertian BSDF sampling,
  * next-event estimation (NEE) with an explicit area -> solid-angle Jacobian,
  * the power heuristic (beta = 2) for multiple importance sampling between
    light sampling and BSDF sampling,
  * Russian roulette with throughput compensation,
  * deterministic per-pixel random streams from an explicit seed,
  * raw linear HDR output as PFM plus an 8-bit display PNG written with the
    Python standard library only.

Only the standard library is used, so the renderer runs on any CPU with
CPython >= 3.9.  No GPU, no third-party renderer, no copied engine code.

The estimator is unbiased for the transport it actually solves (finite
Lambertian interreflection) *except* for the deliberately finite maximum path
depth, which is a truncation bias documented in the report.  Emitters terminate
paths: the scene's lights are purely emissive and not reflective.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import shlex
import struct
import sys
import time
import zlib
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

Vec3 = tuple[float, float, float]
BLACK: Vec3 = (0.0, 0.0, 0.0)
WHITE: Vec3 = (1.0, 1.0, 1.0)
EPS = 1.0e-5

# ---------------------------------------------------------------------------
# Small vector helpers.  Plain tuples keep the hot loops allocation-light.
# ---------------------------------------------------------------------------


def vadd(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def vsub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def vmul(a: Vec3, s: float) -> Vec3:
    return (a[0] * s, a[1] * s, a[2] * s)


def vhad(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] * b[0], a[1] * b[1], a[2] * b[2])


def vdot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def vcross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def vlen(a: Vec3) -> float:
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def vnorm(a: Vec3) -> Vec3:
    n = vlen(a)
    if n == 0.0:
        raise ValueError("cannot normalize the zero vector")
    inv = 1.0 / n
    return (a[0] * inv, a[1] * inv, a[2] * inv)


def vneg(a: Vec3) -> Vec3:
    return (-a[0], -a[1], -a[2])


# ---------------------------------------------------------------------------
# Geometry: axis-aligned rectangles and axis-aligned boxes made of rectangles.
# Both sides of a rectangle shade identically (Lambertian); emission is only
# returned from the front face, i.e. the side its normal points toward.
# ---------------------------------------------------------------------------


class Rect:
    """A planar parallelogram with orthogonal edge vectors u and v."""

    __slots__ = (
        "name",
        "o",
        "u",
        "v",
        "n",
        "area",
        "albedo",
        "emission",
        "is_light",
        "u2",
        "v2",
    )

    def __init__(
        self,
        origin: Vec3,
        u: Vec3,
        v: Vec3,
        albedo: Vec3 = (0.75, 0.75, 0.75),
        emission: Vec3 = BLACK,
        name: str = "rect",
        normal_hint: Optional[Vec3] = None,
    ) -> None:
        cross = vcross(u, v)
        if vlen(cross) == 0.0:
            raise ValueError(f"{name}: degenerate rectangle")
        if abs(vdot(u, v)) > 1.0e-12:
            raise ValueError(f"{name}: u and v must be orthogonal")
        n = vnorm(cross)
        if normal_hint is not None and vdot(n, normal_hint) < 0.0:
            n = vneg(n)
        self.name = name
        self.o = origin
        self.u = u
        self.v = v
        self.n = n
        self.u2 = vdot(u, u)
        self.v2 = vdot(v, v)
        self.area = math.sqrt(self.u2 * self.v2)
        self.albedo = albedo
        self.emission = emission
        self.is_light = emission[0] > 0.0 or emission[1] > 0.0 or emission[2] > 0.0

    def point(self, a: float, b: float) -> Vec3:
        return (
            self.o[0] + a * self.u[0] + b * self.v[0],
            self.o[1] + a * self.u[1] + b * self.v[1],
            self.o[2] + a * self.u[2] + b * self.v[2],
        )

    def sample_point(self, rng: random.Random) -> Vec3:
        return self.point(rng.random(), rng.random())


class Box:
    """Axis-aligned box: a quick AABB test gates six outward-facing rectangles."""

    __slots__ = ("name", "lo", "hi", "faces", "is_light", "albedo")

    def __init__(self, lo: Vec3, hi: Vec3, albedo: Vec3, name: str = "box") -> None:
        self.name = name
        self.lo = lo
        self.hi = hi
        self.is_light = False
        self.albedo = albedo
        x0, y0, z0 = lo
        x1, y1, z1 = hi
        dx, dy, dz = x1 - x0, y1 - y0, z1 - z0
        faces = [
            Rect((x0, y0, z0), (0, dy, 0), (0, 0, dz), albedo, BLACK, name + "-x", (-1, 0, 0)),
            Rect((x1, y0, z0), (0, 0, dz), (0, dy, 0), albedo, BLACK, name + "+x", (1, 0, 0)),
            Rect((x0, y0, z0), (dx, 0, 0), (0, 0, dz), albedo, BLACK, name + "-y", (0, -1, 0)),
            Rect((x0, y1, z0), (0, 0, dz), (dx, 0, 0), albedo, BLACK, name + "+y", (0, 1, 0)),
            Rect((x0, y0, z0), (0, dy, 0), (dx, 0, 0), albedo, BLACK, name + "-z", (0, 0, -1)),
            Rect((x0, y0, z1), (dx, 0, 0), (0, dy, 0), albedo, BLACK, name + "+z", (0, 0, 1)),
        ]
        self.faces = faces


def intersect_rect(o: Vec3, d: Vec3, rect: Rect, t_min: float = 1.0e-6,
                   t_max: float = math.inf) -> Optional[float]:
    """Two-sided ray/rectangle intersection in the rectangle's parameter plane."""
    den = vdot(rect.n, d)
    if -1.0e-12 < den < 1.0e-12:
        return None
    t = vdot(rect.n, vsub(rect.o, o)) / den
    if t < t_min or t > t_max:
        return None
    px = o[0] + t * d[0] - rect.o[0]
    py = o[1] + t * d[1] - rect.o[1]
    pz = o[2] + t * d[2] - rect.o[2]
    ux, uy, uz = rect.u
    vx, vy, vz = rect.v
    a = (px * ux + py * uy + pz * uz) / rect.u2
    if a < 0.0 or a > 1.0:
        return None
    b = (px * vx + py * vy + pz * vz) / rect.v2
    if b < 0.0 or b > 1.0:
        return None
    return t


def intersect_box(o: Vec3, d: Vec3, box: Box, t_min: float = 1.0e-6,
                  t_max: float = math.inf) -> Optional[tuple[float, Rect]]:
    """Slab test, then the six faces; returns the nearest (t, face)."""
    lo, hi = box.lo, box.hi
    t_near = t_min
    t_far = t_max
    for axis in range(3):
        oa = o[axis]
        da = d[axis]
        la = lo[axis]
        ha = hi[axis]
        if abs(da) < 1.0e-15:
            if oa < la or oa > ha:
                return None
        else:
            inv = 1.0 / da
            t1 = (la - oa) * inv
            t2 = (ha - oa) * inv
            if t1 > t2:
                t1, t2 = t2, t1
            if t1 > t_near:
                t_near = t1
            if t2 < t_far:
                t_far = t2
            if t_near > t_far:
                return None
    if t_far < t_min:
        return None
    best: Optional[tuple[float, Rect]] = None
    for face in box.faces:
        t = intersect_rect(o, d, face, t_min, t_far + 1.0e-9)
        if t is not None and (best is None or t < best[0]):
            best = (t, face)
    return best


class Scene:
    __slots__ = ("prims", "lights")

    def __init__(self, prims: Sequence[object]) -> None:
        self.prims = list(prims)
        self.lights = [p for p in self.prims if isinstance(p, Rect) and p.is_light]


def intersect_scene(scene: Scene, o: Vec3, d: Vec3,
                    skip_lights: bool = False) -> Optional[tuple[float, object]]:
    best_t = math.inf
    best_p = None
    for prim in scene.prims:
        if isinstance(prim, Rect):
            if skip_lights and prim.is_light:
                continue
            t = intersect_rect(o, d, prim)
            hit_prim: object = prim
        else:
            box_hit = intersect_box(o, d, prim)
            if box_hit is None:
                continue
            t, hit_prim = box_hit
        if t is not None and t < best_t:
            best_t = t
            best_p = hit_prim
    if best_p is None:
        return None
    return best_t, best_p


def occluded(scene: Scene, p0: Vec3, p1: Vec3,
             ignore_light: Optional[Rect] = None) -> bool:
    """True if opaque geometry (any light except the target) blocks p0 -> p1."""
    delta = vsub(p1, p0)
    dist = vlen(delta)
    if dist <= 2.0 * EPS:
        return True
    d = vmul(delta, 1.0 / dist)
    t_max = dist - EPS
    for prim in scene.prims:
        if isinstance(prim, Rect):
            if prim is ignore_light:
                continue
            t = intersect_rect(p0, d, prim, EPS, t_max)
        else:
            t = intersect_box(p0, d, prim, EPS, t_max)
        if t is not None:
            return True
    return False


# ---------------------------------------------------------------------------
# Sampling and estimator building blocks
# ---------------------------------------------------------------------------


def onb(n: Vec3) -> tuple[Vec3, Vec3]:
    """Return two unit vectors orthogonal to unit n and to each other."""
    if abs(n[0]) > 0.9:
        helper = (0.0, 1.0, 0.0)
    else:
        helper = (1.0, 0.0, 0.0)
    b1 = vnorm(vcross(helper, n))
    b2 = vcross(n, b1)
    return b1, b2


def cosine_sample(rng: random.Random, n: Vec3) -> tuple[Vec3, float]:
    """Cosine-weighted hemisphere sample around n; returns (direction, pdf).

    pdf(w) = cos(theta) / pi, with theta measured from n.
    """
    b1, b2 = onb(n)
    u1 = rng.random()
    u2 = rng.random()
    r = math.sqrt(u1)
    phi = 2.0 * math.pi * u2
    x = r * math.cos(phi)
    y = r * math.sin(phi)
    z = math.sqrt(max(0.0, 1.0 - u1))
    d = (
        b1[0] * x + b2[0] * y + n[0] * z,
        b1[1] * x + b2[1] * y + n[1] * z,
        b1[2] * x + b2[2] * y + n[2] * z,
    )
    return d, z / math.pi


def area_to_solid_angle_pdf(pmf_area: float, dist2: float, cos_light: float) -> float:
    """p_omega = p_A * d^2 / cos(theta_light).

    Uses the Jacobian dA = (d^2 / cos(theta_light)) dOmega.  Returns 0 for
    directions below the light's emitting hemisphere.
    """
    if cos_light <= 0.0 or dist2 <= 0.0:
        return 0.0
    return pmf_area * dist2 / cos_light


def power_heuristic(pdf_a: float, pdf_b: float) -> float:
    """Power heuristic (beta = 2): p_a^2 / (p_a^2 + p_b^2)."""
    a = pdf_a * pdf_a
    b = pdf_b * pdf_b
    denom = a + b
    if denom <= 0.0:
        return 0.0
    return a / denom


def mis_weight_bsdf(pdf_bsdf: float, pdf_light: float) -> float:
    """Weight for a BSDF-sampled emitter hit; complement of the light weight."""
    return power_heuristic(pdf_bsdf, pdf_light)


# ---------------------------------------------------------------------------
# Path tracer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceOptions:
    max_depth: int = 8
    rr_start: int = 3
    rr_min: float = 0.05


def trace_path(
    scene: Scene,
    origin: Vec3,
    direction: Vec3,
    rng: random.Random,
    options: TraceOptions,
    environment: Vec3 = BLACK,
) -> Vec3:
    """Estimate outgoing radiance along one camera ray.

    Emission is added with a MIS weight when the path reaches a light through
    BSDF sampling after at least one scattering event.  NEE is performed at
    every non-emissive diffuse vertex.  Russian roulette follows the BSDF
    sample at depth >= rr_start and divides throughput by the survival
    probability, so it compensates and remains unbiased in expectation.
    """
    radiance = [0.0, 0.0, 0.0]
    throughput = [1.0, 1.0, 1.0]
    prev_point: Optional[Vec3] = None
    prev_bsdf_pdf: Optional[float] = None
    o = origin
    d = direction
    n_lights = len(scene.lights)

    for depth in range(options.max_depth):
        hit = intersect_scene(scene, o, d)
        if hit is None:
            radiance[0] += throughput[0] * environment[0]
            radiance[1] += throughput[1] * environment[1]
            radiance[2] += throughput[2] * environment[2]
            break

        t, prim = hit
        p = (o[0] + t * d[0], o[1] + t * d[1], o[2] + t * d[2])

        # --- Emitter: add radiance with the BSDF-sampling MIS weight. ------
        if prim.is_light:  # type: ignore[attr-defined]
            if vdot(prim.n, d) < 0.0:  # type: ignore[attr-defined]
                if prev_point is None or prev_bsdf_pdf is None:
                    weight = 1.0
                else:
                    delta = vsub(p, prev_point)
                    dist2 = vdot(delta, delta)
                    dist = math.sqrt(dist2)
                    cos_light = -vdot(prim.n, delta) / dist  # type: ignore[attr-defined]
                    pmf_area = 1.0 / (prim.area * n_lights)  # type: ignore[attr-defined]
                    pdf_light = area_to_solid_angle_pdf(pmf_area, dist2, cos_light)
                    weight = (1.0 if pdf_light <= 0.0
                              else mis_weight_bsdf(prev_bsdf_pdf, pdf_light))
                emission = prim.emission  # type: ignore[attr-defined]
                radiance[0] += throughput[0] * emission[0] * weight
                radiance[1] += throughput[1] * emission[1] * weight
                radiance[2] += throughput[2] * emission[2] * weight
            break

        albedo = prim.albedo  # type: ignore[attr-defined]
        if albedo[0] <= 0.0 and albedo[1] <= 0.0 and albedo[2] <= 0.0:
            break

        nd = vdot(prim.n, d)  # type: ignore[attr-defined]
        n = prim.n if nd < 0.0 else vneg(prim.n)  # type: ignore[attr-defined]
        terminal = (depth + 1 >= options.max_depth)

        # --- Next-event estimation (always on for non-emissive vertices) ---
        if scene.lights:
            light = scene.lights[rng.randrange(n_lights)]
            y = light.sample_point(rng)
            delta = vsub(y, p)
            dist2 = vdot(delta, delta)
            if dist2 > 1.0e-14:
                dist = math.sqrt(dist2)
                wi = vmul(delta, 1.0 / dist)
                cos_x = vdot(n, wi)
                cos_light = -vdot(light.n, wi)
                if (cos_x > 0.0 and cos_light > 0.0
                        and not occluded(scene, vadd(p, vmul(n, EPS)), y,
                                         ignore_light=light)):
                    pmf_area = 1.0 / (light.area * n_lights)
                    pdf_light = area_to_solid_angle_pdf(pmf_area, dist2, cos_light)
                    pdf_bsdf = cos_x / math.pi
                    # At the last vertex no BSDF ray is traced, so the BSDF
                    # strategy is absent.  Do not MIS-weight against it.
                    weight = 1.0 if terminal else power_heuristic(pdf_light, pdf_bsdf)
                    # f * L * cos_x / p_omega, with p_omega = p_A d^2 / cos_light.
                    scale = (cos_x * cos_light) / (pmf_area * dist2) * weight
                    fr = 1.0 / math.pi
                    radiance[0] += throughput[0] * albedo[0] * fr * light.emission[0] * scale
                    radiance[1] += throughput[1] * albedo[1] * fr * light.emission[1] * scale
                    radiance[2] += throughput[2] * albedo[2] * fr * light.emission[2] * scale

        if terminal:
            # Area-emitter direct lighting is complete through unweighted NEE.
            # A nonblack environment is not in that light distribution, so
            # estimate its direct contribution separately even at the cap.
            # Only a miss contributes: area-emitter hits were already handled
            # by NEE and further reflected surface transport is truncated.
            if environment != BLACK:
                env_direction, _ = cosine_sample(rng, n)
                env_origin = vadd(p, vmul(n, EPS))
                if intersect_scene(scene, env_origin, env_direction) is None:
                    radiance[0] += throughput[0] * albedo[0] * environment[0]
                    radiance[1] += throughput[1] * albedo[1] * environment[1]
                    radiance[2] += throughput[2] * albedo[2] * environment[2]
            break

        # --- BSDF sampling (cosine-weighted Lambertian) --------------------
        wi, pdf_bsdf = cosine_sample(rng, n)
        cos_x = vdot(n, wi)
        if pdf_bsdf <= 0.0 or cos_x <= 0.0:
            break
        # For Lambertian f = albedo/pi and pdf = cos/pi, so f*cos/pdf = albedo.
        throughput[0] *= albedo[0]
        throughput[1] *= albedo[1]
        throughput[2] *= albedo[2]
        if max(throughput) <= 0.0:
            break

        prev_point = p
        prev_bsdf_pdf = pdf_bsdf

        if depth >= options.rr_start:
            q = max(options.rr_min, min(0.95, max(throughput)))
            if rng.random() >= q:
                break
            inv_q = 1.0 / q
            throughput[0] *= inv_q
            throughput[1] *= inv_q
            throughput[2] *= inv_q

        o = vadd(p, vmul(n, EPS))
        d = wi

    return (radiance[0], radiance[1], radiance[2])


# ---------------------------------------------------------------------------
# Camera and scene
# ---------------------------------------------------------------------------


def camera_ray(cam_pos: Vec3, forward: Vec3, right: Vec3, up: Vec3,
               tan_half_fov: float, aspect: float, u: float, v: float) -> Vec3:
    """Pinhole camera ray; u,v in [0,1] with v measured from the image top."""
    sx = (2.0 * u - 1.0) * aspect * tan_half_fov
    sy = (1.0 - 2.0 * v) * tan_half_fov
    return vnorm((
        forward[0] + sx * right[0] + sy * up[0],
        forward[1] + sx * right[1] + sy * up[1],
        forward[2] + sx * right[2] + sy * up[2],
    ))


def cornell_scene() -> Scene:
    """The essay's CPU reference room, in metres with z up.

    Layout follows the Lumen essay's CPU lighting lab: x in [-2, 2] (red wall
    at x = -2, green at x = +2), y in [0, 5] (open front at y = 0, back wall at
    y = 5), z in [0, 3] (floor at 0, ceiling at 3), black environment through
    the open front.  A 1.3 x 1.2 m emitter sits at z = 2.98 and emits down.
    The left short block is matte; the right tall block is included with the
    same geometry but a Lambertian albedo only.  This renderer has no
    microfacet/GGX lobe and therefore does NOT reproduce the Unreal glossy
    block's BRDF: that block is an explicitly documented pedagogical
    approximation, not an identical Unreal material.
    """
    prims: list[object] = []

    # Room: five rectangles, open at y = 0.  Normals point into the room.
    prims.append(Rect((-2, 0, 0), (4, 0, 0), (0, 5, 0), (0.74, 0.74, 0.71),
                      name="floor", normal_hint=(0, 0, 1)))
    prims.append(Rect((-2, 0, 3), (4, 0, 0), (0, 5, 0), (0.78, 0.78, 0.75),
                      name="ceiling", normal_hint=(0, 0, -1)))
    prims.append(Rect((-2, 0, 0), (0, 5, 0), (0, 0, 3), (0.72, 0.09, 0.07),
                      name="left-red", normal_hint=(1, 0, 0)))
    prims.append(Rect((2, 0, 0), (0, 0, 3), (0, 5, 0), (0.12, 0.62, 0.12),
                      name="right-green", normal_hint=(-1, 0, 0)))
    prims.append(Rect((-2, 5, 0), (4, 0, 0), (0, 0, 3), (0.76, 0.76, 0.73),
                      name="back", normal_hint=(0, -1, 0)))

    # Area emitter: CPU-lab placement (0, 2.7, 2.98), 1.3 m along x and 1.2 m
    # along y, front face downward.  Radiance is arbitrary linear units.
    prims.append(Rect((-0.65, 2.1, 2.98), (1.3, 0, 0), (0, 1.2, 0), BLACK,
                      (16.0, 14.0, 11.0), name="emitter", normal_hint=(0, 0, -1)))

    # Blocks from the essay room's CPU bounds.  The right block's "glossy"
    # material is reduced to a neutral Lambertian albedo and labelled as such.
    prims.append(Box((-1.25, 2.3, 0.0), (-0.30, 3.4, 1.05),
                     (0.66, 0.63, 0.58), name="matte-block-left"))
    prims.append(Box((0.30, 3.3, 0.0), (1.20, 4.3, 1.80),
                     (0.52, 0.55, 0.60), name="glossy-block-right-lambertian"))
    return Scene(prims)


# ---------------------------------------------------------------------------
# Rendering, linear PFM output, and a stdlib-only PNG display writer
# ---------------------------------------------------------------------------


def render(
    scene: Scene,
    width: int,
    height: int,
    samples: int,
    seed: int,
    options: TraceOptions,
    cam_pos: Vec3 = (0.0, -4.0, 1.65),
    look_at: Vec3 = (0.0, 2.8, 1.4),
    fov_degrees: float = 43.0,
    world_up: Vec3 = (0.0, 0.0, 1.0),
    quiet: bool = False,
) -> tuple[list[list[Vec3]], dict[str, float]]:
    """Render with jittered primary samples and per-pixel deterministic RNGs."""
    forward = vnorm(vsub(look_at, cam_pos))
    right = vnorm(vcross(forward, world_up))
    up = vcross(right, forward)
    aspect = width / height
    tan_half = math.tan(0.5 * math.radians(fov_degrees))

    image: list[list[Vec3]] = []
    start = time.perf_counter()
    for y in range(height):
        row: list[Vec3] = []
        for x in range(width):
            # Deterministic, independent stream per pixel.
            pixel_seed = (seed * 0x9E3779B97F4A7C15) ^ ((y * width + x) * 0xBF58476D1CE4E5B9)
            rng = random.Random(pixel_seed & 0xFFFFFFFFFFFFFFFF)
            acc0 = acc1 = acc2 = 0.0
            for s in range(samples):
                # Stratify the x dimension inside the pixel; each strip is
                # sampled uniformly and the strips partition the pixel, so the
                # estimator stays unbiased.  Pixel coordinates are then mapped
                # to full-image normalized coordinates.
                px = (s + rng.random()) / samples
                py = rng.random()
                u = (x + px) / width
                v = (y + py) / height
                d = camera_ray(cam_pos, forward, right, up, tan_half, aspect, u, v)
                c = trace_path(scene, cam_pos, d, rng, options)
                acc0 += c[0]
                acc1 += c[1]
                acc2 += c[2]
            inv = 1.0 / samples
            row.append((acc0 * inv, acc1 * inv, acc2 * inv))
        image.append(row)
        if not quiet and (y + 1) % max(1, height // 8) == 0:
            print(f"  rows {y + 1}/{height}", flush=True)
    elapsed = time.perf_counter() - start
    return image, {"render_seconds": elapsed}


def tone_map(rgb: Vec3, exposure: float) -> tuple[int, int, int]:
    out = []
    for c in rgb:
        x = max(0.0, min(1.0, c * exposure))
        s = 12.92 * x if x <= 0.0031308 else 1.055 * (x ** (1.0 / 2.4)) - 0.055
        out.append(int(round(max(0.0, min(1.0, s)) * 255.0)))
    return out[0], out[1], out[2]


def write_pfm(path: str, image: Sequence[Sequence[Vec3]]) -> None:
    """Write bottom-up little-endian float32 RGB PFM (raw linear radiance)."""
    height = len(image)
    width = len(image[0])
    with open(path, "wb") as f:
        f.write(b"PF\n")
        f.write(f"{width} {height}\n".encode("ascii"))
        f.write(b"-1.0\n")
        for y in range(height - 1, -1, -1):
            row = image[y]
            for rgb in row:
                f.write(struct.pack("<fff", rgb[0], rgb[1], rgb[2]))


# A compact 5x7 bitmap font, enough for the educational label and metadata.
_FONT_ROWS = {
    "A": "01110/10001/10001/11111/10001/10001/10001",
    "B": "11110/10001/10001/11110/10001/10001/11110",
    "C": "01111/10000/10000/10000/10000/10000/01111",
    "D": "11110/10001/10001/10001/10001/10001/11110",
    "E": "11111/10000/10000/11110/10000/10000/11111",
    "F": "11111/10000/10000/11110/10000/10000/10000",
    "G": "01111/10000/10000/10111/10001/10001/01111",
    "H": "10001/10001/10001/11111/10001/10001/10001",
    "I": "11111/00100/00100/00100/00100/00100/11111",
    "J": "00111/00010/00010/00010/00010/10010/01100",
    "K": "10001/10010/10100/11000/10100/10010/10001",
    "L": "10000/10000/10000/10000/10000/10000/11111",
    "M": "10001/11011/10101/10101/10001/10001/10001",
    "N": "10001/11001/10101/10011/10001/10001/10001",
    "O": "01110/10001/10001/10001/10001/10001/01110",
    "P": "11110/10001/10001/11110/10000/10000/10000",
    "Q": "01110/10001/10001/10001/10101/10010/01101",
    "R": "11110/10001/10001/11110/10100/10010/10001",
    "S": "01111/10000/10000/01110/00001/00001/11110",
    "T": "11111/00100/00100/00100/00100/00100/00100",
    "U": "10001/10001/10001/10001/10001/10001/01110",
    "V": "10001/10001/10001/10001/10001/01010/00100",
    "W": "10001/10001/10001/10101/10101/10101/01010",
    "X": "10001/10001/01010/00100/01010/10001/10001",
    "Y": "10001/10001/01010/00100/00100/00100/00100",
    "Z": "11111/00001/00010/00100/01000/10000/11111",
    "0": "01110/10001/10011/10101/11001/10001/01110",
    "1": "00100/01100/00100/00100/00100/00100/01110",
    "2": "01110/10001/00001/00010/00100/01000/11111",
    "3": "11111/00010/00100/00010/00001/10001/01110",
    "4": "00010/00110/01010/10010/11111/00010/00010",
    "5": "11111/10000/11110/00001/00001/10001/01110",
    "6": "00110/01000/10000/11110/10001/10001/01110",
    "7": "11111/00001/00010/00100/01000/01000/01000",
    "8": "01110/10001/10001/01110/10001/10001/01110",
    "9": "01110/10001/10001/01111/00001/00010/01100",
    " ": "00000/00000/00000/00000/00000/00000/00000",
    "-": "00000/00000/00000/11111/00000/00000/00000",
    ".": "00000/00000/00000/00000/00000/01100/01100",
    ",": "00000/00000/00000/00000/01100/01100/11000",
    "(": "00010/00100/01000/01000/01000/00100/00010",
    ")": "01000/00100/00010/00010/00010/00100/01000",
    ":": "00000/01100/01100/00000/01100/01100/00000",
    "/": "00001/00010/00010/00100/01000/01000/10000",
}


def _draw_text(pixels: bytearray, width: int, height: int, x0: int, y0: int,
               text: str, scale: int, color: tuple[int, int, int]) -> None:
    """Draw a label into a 3-byte RGB bytearray (top-left origin)."""
    cx = x0
    for ch in text.upper():
        glyph = _FONT_ROWS.get(ch, _FONT_ROWS[" "])
        for gy, line in enumerate(glyph.split("/")):
            for gx, bit in enumerate(line):
                if bit != "1":
                    continue
                for sy in range(scale):
                    py = y0 + gy * scale + sy
                    if py < 0 or py >= height:
                        continue
                    for sx in range(scale):
                        px = cx + gx * scale + sx
                        if px < 0 or px >= width:
                            continue
                        idx = (py * width + px) * 3
                        pixels[idx] = color[0]
                        pixels[idx + 1] = color[1]
                        pixels[idx + 2] = color[2]
        cx += 6 * scale


def write_png(path: str, image: Sequence[Sequence[Vec3]], exposure: float,
              label_lines: Sequence[str] = (), text_chunks: Optional[dict[str, str]] = None) -> None:
    """Write an 8-bit sRGB PNG using only zlib/struct.

    The optional label is burned into the image so the artifact cannot be
    mistaken for an Unreal scene capture when viewed out of context.
    """
    height = len(image)
    width = len(image[0])
    pixels = bytearray(width * height * 3)
    for y in range(height):
        row = image[y]
        for x in range(width):
            r, g, b = tone_map(row[x], exposure)
            idx = (y * width + x) * 3
            pixels[idx] = r
            pixels[idx + 1] = g
            pixels[idx + 2] = b

    if label_lines:
        scale = max(1, width // 400)
        line_h = 9 * scale
        block_pad = 4 * scale
        block_h = len(label_lines) * line_h + 2 * block_pad
        block_top = height - block_h
        # Dark translucent-looking plate (opaque here for simplicity).
        for y in range(max(0, block_top), height):
            for x in range(width):
                idx = (y * width + x) * 3
                pixels[idx] = pixels[idx] // 4
                pixels[idx + 1] = pixels[idx + 1] // 4
                pixels[idx + 2] = pixels[idx + 2] // 4
        ty = block_top + block_pad
        for line in label_lines:
            _draw_text(pixels, width, height, block_pad, ty, line, scale, (245, 240, 230))
            ty += line_h

    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)  # filter type 0 (None)
        raw.extend(pixels[y * stride:(y + 1) * stride])

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    out = bytearray(b"\x89PNG\r\n\x1a\n")
    out.extend(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
    texts = {
        "Title": "Educational CPU path tracer reference (not Unreal)",
        "Software": "Lumen math essay reference/path_tracer.py (original, stdlib only)",
        "Description": "Essay-room CPU reconstruction (Lambertian-only); linear PFM companion.",
        "Disclaimer": "Educational CPU reference render. NOT an Unreal scene, NOT an Unreal capture.",
    }
    if text_chunks:
        texts.update(text_chunks)
    for key, value in texts.items():
        out.extend(chunk(b"tEXt", key.encode("latin-1") + b"\x00" + value.encode("latin-1")))
    out.extend(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
    out.extend(chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(out)


# ---------------------------------------------------------------------------
# Provenance helpers
# ---------------------------------------------------------------------------


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def file_info(path: str, root: str) -> dict[str, object]:
    full = os.path.abspath(path)
    return {
        "path": os.path.relpath(full, root).replace(os.sep, "/"),
        "bytes": os.path.getsize(full),
        "sha256": sha256_file(full),
    }


def cpu_identity() -> dict[str, object]:
    uname = platform.uname()
    return {
        "machine": uname.machine,
        "processor": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "unknown"),
        "processor_identifier": os.environ.get("PROCESSOR_IDENTIFIER", "unavailable"),
        "system": uname.system,
        "release": uname.release,
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "logical_cores": os.cpu_count(),
    }


def build_manifest(
    args: argparse.Namespace,
    root: str,
    elapsed_render: float,
    elapsed_total: float,
    output_paths: dict[str, str],
    started_utc: str,
    finished_utc: str,
) -> dict[str, object]:
    files = {}
    for key, path in output_paths.items():
        if path and os.path.exists(path):
            files[key] = file_info(path, root)
    manifest: dict[str, object] = {
        "schema": "lumen-educational-reference-manifest/1",
        "artifact_kind": "educational CPU reference render",
        "disclaimer": (
            "This is an original educational CPU path-traced image that "
            "reconstructs the essay's 4 x 5 x 3 m reference room with "
            "Lambertian-only materials. It is NOT an Unreal Engine scene, NOT "
            "an Unreal Engine capture, and is not a substitute for the user's "
            "requested identical Unreal scene. The tall right block's Unreal "
            "glossy/metallic BRDF is not reproduced."
        ),
        "scene": {
            "description": (
                "CPU-lab reconstruction of the essay's 4 x 5 x 3 m room: red wall "
                "at x=-2, green wall at x=+2, neutral floor at z=0, ceiling at "
                "z=3, back wall at y=5, open front at y=0 with black environment. "
                "A 1.3 x 1.2 m emitter at (0,2.7,2.98) emits downward. A short "
                "matte block is on the left; the tall right block uses the same "
                "placement as the essay's glossy block but a neutral Lambertian "
                "albedo, because this renderer has no metallic/GGX lobe."
            ),
            "coordinate_system": (
                "CPU-lab metres, x right, y depth (front to back), z up; "
                "Unreal centimetres map as (x_u,y_u,z_u)=(-100x,100y,100z)"
            ),
            "geometry": "five room rectangles (open front) + two axis-aligned boxes; Lambertian two-sided BSDF",
            "units": "metres (radiance and emission in arbitrary linear units)",
            "walls": {
                "left_red": "x=-2, albedo (0.72,0.09,0.07)",
                "right_green": "x=+2, albedo (0.12,0.62,0.12)",
                "floor": "z=0, albedo (0.74,0.74,0.71)",
                "ceiling": "z=3, albedo (0.78,0.78,0.75)",
                "back": "y=5, albedo (0.76,0.76,0.73)",
                "front": "open at y=0; rays escape to the black environment",
            },
            "emitter": {
                "center": [0.0, 2.7, 2.98],
                "width_x": 1.3,
                "height_y": 1.2,
                "normal": [0.0, 0.0, -1.0],
                "radiance_rgb": [16.0, 14.0, 11.0],
            },
            "blocks": [
                {
                    "name": "matte-block-left",
                    "bounds_min": [-1.25, 2.3, 0.0],
                    "bounds_max": [-0.30, 3.4, 1.05],
                    "albedo_rgb": [0.66, 0.63, 0.58],
                    "note": "short matte block; Lambertian",
                },
                {
                    "name": "glossy-block-right-lambertian",
                    "bounds_min": [0.30, 3.3, 0.0],
                    "bounds_max": [1.20, 4.3, 1.80],
                    "albedo_rgb": [0.52, 0.55, 0.60],
                    "note": (
                        "essay's tall glossy block placement; rendered here as "
                        "neutral Lambertian only. The Unreal metallic/roughness "
                        "0.85/0.12 BRDF is NOT reproduced."
                    ),
                },
            ],
            "camera": {
                "position": [0.0, -4.0, 1.65],
                "look_at": [0.0, 2.8, 1.4],
                "vertical_fov_degrees": 43.0,
                "aspect": "1.4 (560x400 essay reference; CPU artifact uses a smaller same-aspect image)",
            },
            "alignment_note": (
                "Room dimensions, camera, emitter and block placements are aligned "
                "with the essay's CPU lab. This is still a separate educational "
                "CPU render with Lambertian-only materials, not an identical Unreal "
                "scene, not an Unreal capture, and not a reproduction of the "
                "glossy block's Unreal BRDF."
            ),
        },
        "command_line": "python " + " ".join(shlex.quote(a) for a in sys.argv),
        "argv": list(sys.argv),
        "executable": sys.executable,
        "options": {
            "width": args.width,
            "height": args.height,
            "samples": args.samples,
            "seed": args.seed,
            "max_depth": args.max_depth,
            "rr_start": args.rr_start,
            "rr_min": args.rr_min,
            "exposure": args.exposure,
            "nee": "every non-emissive diffuse vertex",
            "bsdf": "cosine-weighted Lambertian",
            "mis": "power heuristic beta=2 (light vs BSDF strategy)",
            "russian_roulette": "throughput-compensated, survival=max(rr_min,min(0.95,max(T)))",
            "environment": "black; the front at y=0 is open",
            "outputs": "PFM raw linear radiance (bottom-up float32) + 8-bit sRGB PNG",
        },
        "timing": {
            "started_utc": started_utc,
            "finished_utc": finished_utc,
            "render_seconds": round(elapsed_render, 6),
            "total_seconds": round(elapsed_total, 6),
            "primary_paths": args.width * args.height * args.samples,
        },
        "cpu": cpu_identity(),
        "files": files,
        "labels": {
            "png_overlay": [
                "EDUCATIONAL CPU REFERENCE",
                "NOT AN UNREAL CAPTURE",
                f"SEED {args.seed}  SPP {args.samples}",
            ],
            "report": "reports/reference-report.md",
            "notes": "reference/README.md",
        },
    }
    checks_path = os.path.join(root, "reference", "out", "checks.json")
    if os.path.exists(checks_path):
        manifest["estimator_checks"] = {
            "file": file_info(checks_path, root),
            "status": "see reference/test_estimators.py output",
        }
    return manifest


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Educational dependency-free CPU path tracer (not Unreal).",
    )
    p.add_argument("--width", type=int, default=280, help="image width in pixels")
    p.add_argument("--height", type=int, default=200, help="image height in pixels")
    p.add_argument("--samples", "--spp", dest="samples", type=int, default=48,
                   help="samples per pixel (default 48)")
    p.add_argument("--seed", type=int, default=20240601, help="explicit RNG seed")
    p.add_argument("--max-depth", type=int, default=8,
                   help="maximum surface vertices per path (finite-depth bias documented)")
    p.add_argument("--rr-start", type=int, default=3, help="first depth for roulette")
    p.add_argument("--rr-min", type=float, default=0.05, help="minimum survival probability")
    p.add_argument("--exposure", type=float, default=0.6, help="display exposure for the PNG")
    p.add_argument("--out-pfm", default=None, help="linear PFM path")
    p.add_argument("--out-png", default=None, help="display PNG path")
    p.add_argument("--manifest", default=None, help="manifest JSON path")
    p.add_argument("--quiet", action="store_true", help="suppress row progress")
    args = p.parse_args(argv)

    if args.width < 1 or args.height < 1:
        p.error("width and height must be positive")
    if args.samples < 1:
        p.error("samples must be positive")
    if args.max_depth < 1:
        p.error("max_depth must be positive")
    if args.rr_start < 0:
        p.error("rr_start must be non-negative")
    if not (0.0 < args.rr_min <= 1.0):
        p.error("rr_min must be in (0, 1]")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    args.root = root
    args.out_pfm = args.out_pfm or os.path.join(root, "reference", "out", "reference_linear.pfm")
    args.out_png = args.out_png or os.path.join(root, "reference", "out", "reference_render.png")
    args.manifest = args.manifest or os.path.join(root, "reference", "manifest.json")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = args.root
    os.makedirs(os.path.dirname(args.out_pfm), exist_ok=True)
    os.makedirs(os.path.dirname(args.out_png), exist_ok=True)
    os.makedirs(os.path.dirname(args.manifest) or root, exist_ok=True)

    started = time.perf_counter()
    started_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print("Educational CPU path tracer (original, stdlib only). "
          "NOT an Unreal scene and NOT an Unreal capture.")
    print(f"scene=essay-room-cpu-lambertian resolution={args.width}x{args.height} "
          f"samples={args.samples} seed={args.seed} max_depth={args.max_depth}")
    scene = cornell_scene()
    options = TraceOptions(max_depth=args.max_depth, rr_start=args.rr_start, rr_min=args.rr_min)
    image, timing = render(scene, args.width, args.height, args.samples, args.seed,
                           options, quiet=args.quiet)
    write_pfm(args.out_pfm, image)
    label_lines = [
        "EDUCATIONAL CPU REFERENCE",
        "NOT AN UNREAL CAPTURE",
        f"SEED {args.seed}  SPP {args.samples}",
    ]
    write_png(args.out_png, image, args.exposure, label_lines,
              {"Parameters": f"seed={args.seed};spp={args.samples};"
                             f"max_depth={args.max_depth};resolution={args.width}x{args.height}"})
    finish_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    elapsed_total = time.perf_counter() - started
    output_paths = {
        "source": os.path.join(root, "reference", "path_tracer.py"),
        "linear_pfm": args.out_pfm,
        "display_png": args.out_png,
        "readme": os.path.join(root, "reference", "README.md"),
    }
    manifest = build_manifest(args, root, timing["render_seconds"], elapsed_total,
                              output_paths, started_utc, finish_utc)
    with open(args.manifest, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    print(f"wrote {args.out_pfm}")
    print(f"wrote {args.out_png}")
    print(f"wrote {args.manifest}")
    print(f"render time {timing['render_seconds']:.2f} s; total {elapsed_total:.2f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
