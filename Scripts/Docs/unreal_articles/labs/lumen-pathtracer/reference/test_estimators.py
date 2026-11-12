#!/usr/bin/env python3
"""Numerical estimator checks for the educational CPU reference renderer.

These tests are deliberately small and deterministic.  They check the pieces
of the transport estimator that are easiest to get subtly wrong:

  1. cosine-weighted sampling and its pdf (Lambertian constant environment),
  2. the area -> solid-angle Jacobian used by next-event estimation,
  3. the power-heuristic MIS partition (weights sum to one; combined direct
     lighting matches a deterministic Gauss-Legendre reference),
  4. Russian roulette with throughput compensation against the exact finite
     geometric-series expectation, plus an uncompensated control that must be
     biased low.

Everything is standard-library Python.  Run:

    python reference/test_estimators.py --json reference/out/checks.json

The reference renderer itself is in reference/path_tracer.py.  This is an
educational CPU reference, not Unreal Engine and not an Unreal capture.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import path_tracer as pt  # noqa: E402


# ---------------------------------------------------------------------------
# Deterministic reference integration
# ---------------------------------------------------------------------------


def gauss_legendre(n: int) -> tuple[list[float], list[float]]:
    """Nodes and weights for n-point Gauss-Legendre quadrature on [-1, 1]."""
    nodes: list[float] = []
    weights: list[float] = []
    for i in range(n):
        x = math.cos(math.pi * (i + 0.75) / (n + 0.5))
        dp = 1.0
        for _ in range(30):
            p0 = 1.0
            p1 = x
            for k in range(2, n + 1):
                p0, p1 = p1, ((2 * k - 1) * x * p1 - (k - 1) * p0) / k
            dp = n * (x * p1 - p0) / (x * x - 1.0)
            dx = p1 / dp
            x -= dx
            if abs(dx) < 1.0e-15:
                break
        nodes.append(x)
        weights.append(2.0 / ((1.0 - x * x) * dp * dp))
    return nodes, weights


def projected_solid_angle(light: pt.Rect, p: pt.Vec3, n: pt.Vec3, order: int) -> float:
    """Deterministic integral of cos_x * cos_light / d^2 over the light area."""
    nodes, weights = gauss_legendre(order)
    total = 0.0
    for i, a in enumerate(nodes):
        u = 0.5 * (a + 1.0)
        wu = 0.5 * weights[i]
        for j, b in enumerate(nodes):
            v = 0.5 * (b + 1.0)
            wv = 0.5 * weights[j]
            y = light.point(u, v)
            delta = pt.vsub(y, p)
            dist2 = pt.vdot(delta, delta)
            dist = math.sqrt(dist2)
            wi = pt.vmul(delta, 1.0 / dist)
            cos_x = pt.vdot(n, wi)
            cos_light = -pt.vdot(light.n, wi)
            if cos_x > 0.0 and cos_light > 0.0:
                total += cos_x * cos_light / dist2 * wu * wv
    return total * light.area


def direct_reference(light: pt.Rect, p: pt.Vec3, n: pt.Vec3,
                     albedo: float, order: int = 64) -> float:
    """Lambertian direct radiance from a uniform rectangle, deterministic."""
    omega_proj = projected_solid_angle(light, p, n, order)
    return albedo * light.emission[0] / math.pi * omega_proj


# ---------------------------------------------------------------------------
# Monte Carlo helpers
# ---------------------------------------------------------------------------


def mean_se(values: list[float]) -> tuple[float, float]:
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return mean, math.inf
    var = sum((x - mean) ** 2 for x in values) / (n - 1)
    return mean, math.sqrt(var / n)


def cosine_pdf_test(seed: int, n_samples: int) -> dict[str, float]:
    rng = random.Random(seed)
    n = pt.vnorm((0.3, 1.0, -0.2))
    ratio_sum = 0.0
    for _ in range(n_samples):
        d, pdf = pt.cosine_sample(rng, n)
        cos_theta = pt.vdot(d, n)
        assert abs(pt.vlen(d) - 1.0) < 1.0e-12
        assert cos_theta >= -1.0e-12
        assert abs(pdf - max(0.0, cos_theta) / math.pi) < 1.0e-12
        ratio_sum += cos_theta / pdf
    ratio_mean = ratio_sum / n_samples
    # Integrating the pdf over the hemisphere with uniform-hemisphere MC.
    integral_sum = 0.0
    for _ in range(n_samples):
        u1 = rng.random()
        u2 = rng.random()
        z = u1
        r = math.sqrt(max(0.0, 1.0 - z * z))
        phi = 2.0 * math.pi * u2
        b1, b2 = pt.onb(n)
        d = (b1[0] * r * math.cos(phi) + b2[0] * r * math.sin(phi) + n[0] * z,
             b1[1] * r * math.cos(phi) + b2[1] * r * math.sin(phi) + n[1] * z,
             b1[2] * r * math.cos(phi) + b2[2] * r * math.sin(phi) + n[2] * z)
        integral_sum += pt.vdot(d, n) / math.pi * 2.0 * math.pi
    pdf_integral = integral_sum / n_samples
    return {
        "cos_over_pdf_mean": ratio_mean,
        "cos_over_pdf_exact": math.pi,
        "pdf_integral_uniform_mc": pdf_integral,
        "samples": n_samples,
    }


def constant_environment_test(seed: int, n_samples: int, max_depth: int = 2) -> dict[str, float]:
    """Lambertian plane under a constant environment: E[L_o] = albedo * L_env."""
    albedo = 0.6
    env = 2.0
    floor = pt.Rect((-20.0, 0.0, -20.0), (40.0, 0.0, 0.0), (0.0, 0.0, 40.0),
                    (albedo, albedo, albedo), pt.BLACK, "test-floor",
                    normal_hint=(0.0, 1.0, 0.0))
    scene = pt.Scene([floor])
    rng = random.Random(seed)
    opts = pt.TraceOptions(max_depth=max_depth, rr_start=99, rr_min=0.5)
    total = 0.0
    for _ in range(n_samples):
        c = pt.trace_path(scene, (0.0, 1.0, 0.0), (0.0, -1.0, 0.0), rng, opts,
                          environment=(env, env, env))
        total += c[0]
    mean = total / n_samples
    return {
        "mean": mean,
        "expected": albedo * env,
        "albedo": albedo,
        "environment": env,
        "samples": n_samples,
    }


def roulette_tracer_test(seed: int, n_samples: int) -> dict[str, float]:
    """Exercise the tracer's RR branch on the constant-environment plane.

    Throughput after the Lambertian bounce is 0.6, so the tracer's
    q = max(rr_min, min(0.95, max(T))) is 0.6.  Surviving paths divide by 0.6
    (throughput 1.0) and then escape into the constant environment 2.0.
    E[L] = 0.6 * (1/0.6) * 0.6 * 2.0 = 1.2, the same bound as without RR.
    """
    albedo = 0.6
    env = 2.0
    floor = pt.Rect((-20.0, 0.0, -20.0), (40.0, 0.0, 0.0), (0.0, 0.0, 40.0),
                    (albedo, albedo, albedo), pt.BLACK, "test-floor",
                    normal_hint=(0.0, 1.0, 0.0))
    scene = pt.Scene([floor])
    rng = random.Random(seed)
    opts = pt.TraceOptions(max_depth=2, rr_start=0, rr_min=0.05)
    values: list[float] = []
    for _ in range(n_samples):
        c = pt.trace_path(scene, (0.0, 1.0, 0.0), (0.0, -1.0, 0.0), rng, opts,
                          environment=(env, env, env))
        values.append(c[0])
    mean, se = mean_se(values)
    return {
        "mean": mean,
        "se": se,
        "expected": albedo * env,
        "survival_q": albedo,
        "samples": n_samples,
    }


def camera_orientation_test(seed: int) -> dict[str, object]:
    """Camera basis, pixel mapping, and non-mirrored room orientation."""
    cam = (0.0, -4.0, 1.65)
    target = (0.0, 2.8, 1.4)
    forward = pt.vnorm(pt.vsub(target, cam))
    right = pt.vnorm(pt.vcross(forward, (0.0, 0.0, 1.0)))
    up = pt.vcross(right, forward)
    tan_half = math.tan(0.5 * math.radians(43.0))
    aspect = 280.0 / 200.0
    center = pt.camera_ray(cam, forward, right, up, tan_half, aspect, 0.5, 0.5)
    right_ray = pt.camera_ray(cam, forward, right, up, tan_half, aspect, 0.9, 0.5)
    left_ray = pt.camera_ray(cam, forward, right, up, tan_half, aspect, 0.1, 0.5)
    up_ray = pt.camera_ray(cam, forward, right, up, tan_half, aspect, 0.5, 0.1)
    down_ray = pt.camera_ray(cam, forward, right, up, tan_half, aspect, 0.5, 0.9)
    near_ray = pt.camera_ray(cam, forward, right, up, tan_half, aspect, 0.5001, 0.5)
    scene = pt.cornell_scene()
    hit_left = pt.intersect_scene(scene, cam, left_ray)
    hit_right = pt.intersect_scene(scene, cam, right_ray)
    return {
        "center_dot_forward": pt.vdot(center, forward),
        "right_dot_right": pt.vdot(right_ray, right),
        "left_dot_right": pt.vdot(left_ray, right),
        "up_dot_up": pt.vdot(up_ray, up),
        "down_dot_up": pt.vdot(down_ray, up),
        "adjacent_ray_separation": pt.vlen(pt.vsub(center, near_ray)),
        "left_hit": None if hit_left is None else hit_left[1].name,
        "right_hit": None if hit_right is None else hit_right[1].name,
        "seed": seed,
    }


def terminal_nee_test(seed: int, n_samples: int) -> dict[str, float]:
    """At max_depth=1 only NEE exists: it must not be MIS-weighted.

    Before the terminal-depth fix, NEE at the last vertex carried w_light < 1
    while the competing BSDF ray was never traced, biasing direct light low.
    This test would fail by roughly 1.5% (many standard errors).
    """
    albedo = 0.7
    floor = pt.Rect((-20.0, 0.0, -20.0), (40.0, 0.0, 0.0), (0.0, 0.0, 40.0),
                    (albedo, albedo, albedo), pt.BLACK, "test-floor",
                    normal_hint=(0.0, 1.0, 0.0))
    light = pt.Rect((-0.5, 1.5, -0.5), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0),
                    pt.BLACK, (1.0, 1.0, 1.0), "test-light",
                    normal_hint=(0.0, -1.0, 0.0))
    scene = pt.Scene([floor, light])
    rng = random.Random(seed)
    opts = pt.TraceOptions(max_depth=1, rr_start=99, rr_min=0.5)
    values: list[float] = []
    for _ in range(n_samples):
        c = pt.trace_path(scene, (0.0, 1.0, 0.0), (0.0, -1.0, 0.0), rng, opts)
        values.append(c[0])
    mean, se = mean_se(values)
    ref = direct_reference(light, (0.0, 0.0, 0.0), (0.0, 1.0, 0.0), albedo, order=64)
    return {
        "mean": mean,
        "se": se,
        "reference": ref,
        "abs_error": abs(mean - ref),
        "samples": n_samples,
    }


def occlusion_target_light_test(seed: int) -> dict[str, object]:
    """Shadow rays must skip only the target light, not every emitter."""
    target = pt.Rect((-0.5, 1.5, -0.5), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0),
                     pt.BLACK, (1.0, 1.0, 1.0), "target-light",
                     normal_hint=(0.0, -1.0, 0.0))
    blocker = pt.Rect((-0.5, 0.75, -0.5), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0),
                      pt.BLACK, (1.0, 1.0, 1.0), "blocking-light",
                      normal_hint=(0.0, -1.0, 0.0))
    scene = pt.Scene([target, blocker])
    p = (0.0, 0.0, 0.0)
    q = (0.0, 1.5, 0.0)
    blocked = pt.occluded(scene, p, q, ignore_light=target)
    ignored_both = pt.occluded(scene, p, q, ignore_light=blocker)
    return {
        "blocked_by_other_emitter": blocked,
        "not_blocked_when_other_ignored": ignored_both,
        "seed": seed,
    }


def jacobian_test(seed: int, grid: int) -> dict[str, float]:
    """Verify p_omega = p_A d^2 / cos_light against dOmega = cos/d^2 dA."""
    light = pt.Rect((-0.5, 1.5, -0.5), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0),
                    pt.BLACK, (1.0, 1.0, 1.0), "test-light",
                    normal_hint=(0.0, -1.0, 0.0))
    pmf_area = 1.0 / light.area
    total = 0.0
    for i in range(grid):
        u = (i + 0.5) / grid
        for j in range(grid):
            v = (j + 0.5) / grid
            y = light.point(u, v)
            delta = pt.vsub(y, (0.0, 0.0, 0.0))
            dist2 = pt.vdot(delta, delta)
            dist = math.sqrt(dist2)
            wi = pt.vmul(delta, 1.0 / dist)
            cos_light = -pt.vdot(light.n, wi)
            pdf_omega = pt.area_to_solid_angle_pdf(pmf_area, dist2, cos_light)
            d_omega = cos_light / dist2 * light.area / (grid * grid)
            total += pdf_omega * d_omega
    # Also confirm the deterministic projected-solid-angle reference converges.
    omega_12 = projected_solid_angle(light, (0.0, 0.0, 0.0), (0.0, 1.0, 0.0), 12)
    omega_32 = projected_solid_angle(light, (0.0, 0.0, 0.0), (0.0, 1.0, 0.0), 32)
    omega_64 = projected_solid_angle(light, (0.0, 0.0, 0.0), (0.0, 1.0, 0.0), 64)
    return {
        "pdf_integral": total,
        "grid": grid,
        "omega_proj_12": omega_12,
        "omega_proj_32": omega_32,
        "omega_proj_64": omega_64,
        "omega_conv_12_32": abs(omega_12 - omega_32),
        "omega_conv_32_64": abs(omega_32 - omega_64),
    }


def direct_mis_test(seed: int, n_samples: int) -> dict[str, float]:
    """Direct lighting: NEE, BSDF-only, and MIS vs Gauss-Legendre reference."""
    light = pt.Rect((-0.5, 1.5, -0.5), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0),
                    pt.BLACK, (1.0, 1.0, 1.0), "test-light",
                    normal_hint=(0.0, -1.0, 0.0))
    scene = pt.Scene([light])
    p = (0.0, 0.0, 0.0)
    n = (0.0, 1.0, 0.0)
    albedo = 0.7
    pmf_area = 1.0 / (light.area * len(scene.lights))
    rng = random.Random(seed)

    nee_values: list[float] = []
    bsdf_values: list[float] = []
    mis_values: list[float] = []
    weight_partition_max = 0.0

    for _ in range(n_samples):
        # Light-sampling strategy.
        c_nee = 0.0
        y = light.sample_point(rng)
        delta = pt.vsub(y, p)
        dist2 = pt.vdot(delta, delta)
        dist = math.sqrt(dist2)
        wi = pt.vmul(delta, 1.0 / dist)
        cos_x = pt.vdot(n, wi)
        cos_light = -pt.vdot(light.n, wi)
        if cos_x > 0.0 and cos_light > 0.0 and not pt.occluded(scene, p, y, ignore_light=light):
            pdf_light = pt.area_to_solid_angle_pdf(pmf_area, dist2, cos_light)
            pdf_bsdf = cos_x / math.pi
            w_light = pt.power_heuristic(pdf_light, pdf_bsdf)
            weight_partition_max = max(weight_partition_max,
                                       abs(w_light + pt.power_heuristic(pdf_bsdf, pdf_light) - 1.0))
            c_nee = (albedo / math.pi) * light.emission[0] * cos_x / pdf_light * w_light

        # BSDF-sampling strategy.
        c_bsdf = 0.0
        wi2, pdf_bsdf2 = pt.cosine_sample(rng, n)
        hit = pt.intersect_scene(scene, p, wi2)
        if hit is not None and hit[1] is light:
            t, prim = hit
            yhit = pt.vadd(p, pt.vmul(wi2, t))
            delta2 = pt.vsub(yhit, p)
            dist22 = pt.vdot(delta2, delta2)
            cos_light2 = -pt.vdot(light.n, wi2)
            if cos_light2 > 0.0:
                pdf_light2 = pt.area_to_solid_angle_pdf(pmf_area, dist22, cos_light2)
                w_bsdf = pt.power_heuristic(pdf_bsdf2, pdf_light2)
                weight_partition_max = max(weight_partition_max,
                                           abs(w_bsdf + pt.power_heuristic(pdf_light2, pdf_bsdf2) - 1.0))
                # f*cos/pdf = albedo for cosine sampling, so throughput = albedo.
                c_bsdf = albedo * light.emission[0] * w_bsdf

        nee_values.append(c_nee)
        bsdf_values.append(c_bsdf)
        # Both strategies are evaluated for every sample (no strategy-selection
        # probability), so the MIS estimator is their sum, not their average.
        mis_values.append(c_nee + c_bsdf)

    ref = direct_reference(light, p, n, albedo, order=64)
    mean_nee, se_nee = mean_se(nee_values)
    mean_bsdf, se_bsdf = mean_se(bsdf_values)
    mean_mis, se_mis = mean_se(mis_values)
    return {
        "reference": ref,
        "nee_mean": mean_nee,
        "nee_se": se_nee,
        "bsdf_mean": mean_bsdf,
        "bsdf_se": se_bsdf,
        "component_sum": mean_nee + mean_bsdf,
        "mis_mean": mean_mis,
        "mis_se": se_mis,
        "mis_abs_error": abs(mean_mis - ref),
        "mis_rel_error": abs(mean_mis - ref) / ref,
        "weight_partition_max_error": weight_partition_max,
        "samples": n_samples,
        "albedo": albedo,
        "light_emission": light.emission[0],
    }


def mis_weight_partition_test(seed: int, n_pairs: int) -> dict[str, float]:
    rng = random.Random(seed)
    max_error = 0.0
    min_weight = math.inf
    max_weight = -math.inf
    for _ in range(n_pairs):
        a = 10.0 ** rng.uniform(-9.0, 9.0)
        b = 10.0 ** rng.uniform(-9.0, 9.0)
        wa = pt.power_heuristic(a, b)
        wb = pt.power_heuristic(b, a)
        max_error = max(max_error, abs(wa + wb - 1.0))
        min_weight = min(min_weight, wa, wb)
        max_weight = max(max_weight, wa, wb)
    return {
        "pairs": n_pairs,
        "max_partition_error": max_error,
        "min_weight": min_weight,
        "max_weight": max_weight,
    }


def roulette_test(seed: int, trials: int, a: float = 0.6, q: float = 0.8,
                  max_depth: int = 40) -> dict[str, float]:
    """Simulate the tracer's geometric throughput with compensated RR."""
    rng = random.Random(seed)
    compensated: list[float] = []
    uncompensated: list[float] = []
    for _ in range(trials):
        t = 1.0
        x = 0.0
        for k in range(max_depth + 1):
            x += t
            if k == max_depth:
                break
            t *= a
            if rng.random() < q:
                t /= q
            else:
                break
        compensated.append(x)

        t = 1.0
        x = 0.0
        for k in range(max_depth + 1):
            x += t
            if k == max_depth:
                break
            t *= a
            if rng.random() >= q:
                break
        uncompensated.append(x)

    exactly = sum(a ** k for k in range(max_depth + 1))
    mean_c, se_c = mean_se(compensated)
    mean_u, se_u = mean_se(uncompensated)
    mean_c2 = sum(v * v for v in compensated) / trials
    return {
        "a": a,
        "q": q,
        "max_depth": max_depth,
        "trials": trials,
        "exact_compensated": exactly,
        "mean_compensated": mean_c,
        "se_compensated": se_c,
        "abs_error": abs(mean_c - exactly),
        "tolerance_4se": 4.0 * se_c,
        "mean_compensated_square": mean_c2,
        "exact_uncompensated": sum((a * q) ** k for k in range(max_depth + 1)),
        "mean_uncompensated": mean_u,
        "se_uncompensated": se_u,
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class Recorder:
    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.checks: list[dict[str, object]] = []

    def record(self, name: str, ok: bool, detail: str, measured: object,
               expected: object, tolerance: object, extra: object = None,
               *, sampling_seed: int) -> None:
        entry = {
            "name": name,
            "status": "pass" if ok else "fail",
            "base_seed": self.seed,
            "sampling_seed": sampling_seed,
            "measured": measured,
            "expected": expected,
            "tolerance": tolerance,
            "detail": detail,
        }
        if extra is not None:
            entry["extra"] = extra
        self.checks.append(entry)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        for key, value in entry.items():
            if key in ("name", "status", "detail"):
                continue
            print(f"        {key}: {value}")

    @property
    def ok(self) -> bool:
        return all(c["status"] == "pass" for c in self.checks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Numerical checks for the educational CPU reference path tracer.")
    parser.add_argument("--seed", type=int, default=20240601)
    parser.add_argument("--samples", type=int, default=200_000,
                        help="samples for cosine/MIS checks")
    parser.add_argument("--json", default=os.path.join(HERE, "out", "checks.json"))
    args = parser.parse_args(argv)

    started = time.perf_counter()
    rec = Recorder(args.seed)

    cos = cosine_pdf_test(args.seed + 1, args.samples)
    ok = (abs(cos["cos_over_pdf_mean"] - math.pi) < 1.0e-9
          and abs(cos["pdf_integral_uniform_mc"] - 1.0) < 0.01)
    rec.record(
        "cosine_pdf_lambertian",
        ok,
        f"E[cos/pdf]={cos['cos_over_pdf_mean']:.15f} (exact pi={math.pi:.15f}); "
        f"uniform-MC integral of pdf={cos['pdf_integral_uniform_mc']:.6f} (exact 1)",
        cos["cos_over_pdf_mean"], math.pi, 1.0e-9, cos, sampling_seed=args.seed + 1,
    )

    const = constant_environment_test(args.seed + 2, min(args.samples, 50_000))
    ok = abs(const["mean"] - const["expected"]) < 1.0e-9
    rec.record(
        "constant_environment_cosine_sampling",
        ok,
        f"mean={const['mean']:.12f}, albedo*L_env={const['expected']:.12f}",
        const["mean"], const["expected"], 1.0e-9, const, sampling_seed=args.seed + 2,
    )

    terminal_env = constant_environment_test(args.seed + 12, 5000, max_depth=1)
    rec.record(
        "terminal_depth_retains_direct_environment",
        abs(terminal_env["mean"] - terminal_env["expected"]) < 1.0e-9,
        f"max_depth=1 mean={terminal_env['mean']:.12f}, expected rho*L_env={terminal_env['expected']:.12f}",
        terminal_env["mean"], terminal_env["expected"], 1.0e-9, terminal_env,
        sampling_seed=args.seed + 12,
    )

    rr_trace = roulette_tracer_test(args.seed + 8, min(args.samples, 100_000))
    rr_tol = max(4.0 * rr_trace["se"], 1.0e-9)
    ok = abs(rr_trace["mean"] - rr_trace["expected"]) <= rr_tol
    rec.record(
        "russian_roulette_in_tracer",
        ok,
        f"mean={rr_trace['mean']:.9f}, albedo*L_env={rr_trace['expected']:.9f} "
        f"(|err|={abs(rr_trace['mean'] - rr_trace['expected']):.2e}, "
        f"4SE={rr_tol:.2e}, q={rr_trace['survival_q']:.2f})",
        rr_trace["mean"], rr_trace["expected"], rr_tol, rr_trace, sampling_seed=args.seed + 8,
    )

    cam = camera_orientation_test(args.seed + 9)
    ok = (abs(cam["center_dot_forward"] - 1.0) < 1.0e-12
          and cam["right_dot_right"] > 0.05
          and cam["left_dot_right"] < -0.05
          and cam["up_dot_up"] > 0.05
          and cam["down_dot_up"] < -0.05
          and cam["adjacent_ray_separation"] > 1.0e-6
          and cam["left_hit"] == "left-red"
          and cam["right_hit"] == "right-green")
    rec.record(
        "camera_basis_and_unmirrored_room",
        ok,
        f"center.fwd={cam['center_dot_forward']:.12f}; "
        f"right.dir={cam['right_dot_right']:+.4f}; left.dir={cam['left_dot_right']:+.4f}; "
        f"up.dir={cam['up_dot_up']:+.4f}; down.dir={cam['down_dot_up']:+.4f}; "
        f"left hit={cam['left_hit']}, right hit={cam['right_hit']}",
        1.0, 1.0, 1.0e-12, cam, sampling_seed=args.seed + 9,
    )

    terminal = terminal_nee_test(args.seed + 10, min(args.samples, 100_000))
    terminal_tol = max(4.0 * terminal["se"], 1.0e-9)
    ok = terminal["abs_error"] <= terminal_tol
    rec.record(
        "terminal_depth_nee_not_mis_weighted",
        ok,
        f"max_depth=1 NEE mean={terminal['mean']:.9f}, reference={terminal['reference']:.9f} "
        f"(|err|={terminal['abs_error']:.2e}, 4SE={terminal_tol:.2e})",
        terminal["mean"], terminal["reference"], terminal_tol, terminal,
        sampling_seed=args.seed + 10,
    )

    occ = occlusion_target_light_test(args.seed + 11)
    ok = bool(occ["blocked_by_other_emitter"]) and not bool(occ["not_blocked_when_other_ignored"])
    rec.record(
        "shadow_ray_skips_only_target_light",
        ok,
        f"blocked by other emitter={occ['blocked_by_other_emitter']}; "
        f"blocked when other emitter ignored={occ['not_blocked_when_other_ignored']}",
        occ["blocked_by_other_emitter"], True, 0, occ, sampling_seed=args.seed + 11,
    )

    jac = jacobian_test(args.seed + 3, 256)
    ok = (abs(jac["pdf_integral"] - 1.0) < 1.0e-9
          and jac["omega_conv_32_64"] < 5.0e-7)
    rec.record(
        "area_to_solid_angle_jacobian",
        ok,
        f"integral(p_omega dOmega)={jac['pdf_integral']:.15f} (exact 1); "
        f"Omega_proj converged to {jac['omega_proj_64']:.12f} "
        f"(|32-64|={jac['omega_conv_32_64']:.2e})",
        jac["pdf_integral"], 1.0, 1.0e-9, jac, sampling_seed=args.seed + 3,
    )

    mis = direct_mis_test(args.seed + 4, args.samples)
    tol_mis = max(4.0 * mis["mis_se"], 1.0e-5)
    ok = mis["mis_abs_error"] <= tol_mis
    rec.record(
        "direct_lighting_mis_vs_reference",
        ok,
        f"reference={mis['reference']:.8f}; MIS sum={mis['mis_mean']:.8f} "
        f"(err {mis['mis_abs_error']:.2e}, tol {tol_mis:.2e}); "
        f"components NEE={mis['nee_mean']:.8f} + BSDF={mis['bsdf_mean']:.8f} "
        f"= {mis['component_sum']:.8f}",
        mis["mis_mean"], mis["reference"], tol_mis, mis, sampling_seed=args.seed + 4,
    )

    part = mis_weight_partition_test(args.seed + 5, 50_000)
    ok = (part["max_partition_error"] < 1.0e-12
          and part["min_weight"] >= 0.0 and part["max_weight"] <= 1.0)
    rec.record(
        "mis_power_heuristic_partition",
        ok,
        f"max |w_light+w_bsdf-1|={part['max_partition_error']:.2e} over {part['pairs']} random pairs; "
        f"weights in [{part['min_weight']:.3e}, {part['max_weight']:.3e}]",
        part["max_partition_error"], 0.0, 1.0e-12, part, sampling_seed=args.seed + 5,
    )

    rr = roulette_test(args.seed + 6, 200_000)
    ok = rr["abs_error"] <= rr["tolerance_4se"]
    rec.record(
        "russian_roulette_compensated_expectation",
        ok,
        f"mean={rr['mean_compensated']:.6f} vs exact={rr['exact_compensated']:.6f} "
        f"(|err|={rr['abs_error']:.2e} <= 4SE={rr['tolerance_4se']:.2e}); "
        f"uncompensated control={rr['mean_uncompensated']:.6f} "
        f"(exact uncompensated={rr['exact_uncompensated']:.6f})",
        rr["mean_compensated"], rr["exact_compensated"], rr["tolerance_4se"], rr,
        sampling_seed=args.seed + 6,
    )

    # Emitter hit at depth zero must not be counted twice or MIS-weighted.
    light = pt.Rect((-0.5, 1.5, -0.5), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0),
                    pt.BLACK, (3.0, 2.0, 1.0), "single-light",
                    normal_hint=(0.0, -1.0, 0.0))
    scene = pt.Scene([light])
    rng = random.Random(args.seed + 7)
    c = pt.trace_path(scene, (0.0, 0.0, 0.0), (0.0, 1.0, 0.0), rng,
                      pt.TraceOptions(max_depth=4))
    ok = c == (3.0, 2.0, 1.0)
    rec.record(
        "camera_ray_emitter_not_double_counted",
        ok,
        f"radiance={c}, expected emission=(3.0, 2.0, 1.0)",
        list(c), [3.0, 2.0, 1.0], 0.0, sampling_seed=args.seed + 7,
    )

    elapsed = time.perf_counter() - started
    payload = {
        "schema": "lumen-educational-reference-checks/1",
        "artifact": "educational CPU reference, NOT Unreal",
        "seed": args.seed,
        "command_line": "python " + " ".join(sys.argv),
        "elapsed_seconds": round(elapsed, 6),
        "tolerances": "absolute unless stated; 4 standard errors for MC checks",
        "overall": "pass" if rec.ok else "fail",
        "checks": rec.checks,
    }
    os.makedirs(os.path.dirname(args.json) or HERE, exist_ok=True)
    with open(args.json, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    print(f"\noverall: {payload['overall']}  ({elapsed:.2f} s)")
    print(f"wrote {args.json}")
    return 0 if rec.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
