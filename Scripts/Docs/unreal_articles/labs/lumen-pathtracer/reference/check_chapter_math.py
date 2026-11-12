"""Reproduce the numerical examples in the supervisor-reviewed math chapter.

Standard library only; this is algebra/quadrature, not an Unreal capture.
Run: python check_chapter_math.py
"""

import json
import math


def main():
    cone_area = 0.5
    cone_axis_cosine = 0.8
    cos_alpha = 1.0 - cone_area / (2.0 * math.pi)
    cone_exact = 0.02 * 10.0 * cone_axis_cosine * cone_area * (
        1.0 - cone_area / (4.0 * math.pi)
    )
    # Independent angular quadrature: sample cos(theta) uniformly over the
    # spherical cap and azimuth uniformly around its tilted central axis.
    n = 128
    cosine_integral = 0.0
    axis_sine = math.sqrt(1.0 - cone_axis_cosine**2)
    for i in range(n):
        z = cos_alpha + (1.0 - cos_alpha) * (i + 0.5) / n
        radius = math.sqrt(1.0 - z * z)
        for j in range(n):
            phi = 2.0 * math.pi * (j + 0.5) / n
            dot = cone_axis_cosine * z + axis_sine * radius * math.cos(phi)
            assert dot > 0.0  # Entire chosen cap is above the emitting horizon.
            cosine_integral += dot * cone_area / (n * n)
    cone_quadrature = 0.02 * 10.0 * cosine_integral
    assert abs(cone_quadrature - cone_exact) < 1e-13

    exact = (1.0 / 0.94, 0.3 / 0.94)
    approximate = (1.0 / 0.95, 0.25 / 0.95)
    iteration = (1.0, 0.0)
    for _ in range(100):
        iteration = (1.0 + 0.2 * iteration[1], 0.3 * iteration[0])
    assert max(abs(a - b) for a, b in zip(iteration, exact)) < 1e-14
    error = tuple(a - b for a, b in zip(exact, approximate))
    lhs = (error[0] - 0.2 * error[1], error[1] - 0.3 * error[0])
    rhs = (0.0, (0.3 - 0.25) * approximate[0])
    identity_error = max(abs(a - b) for a, b in zip(lhs, rhs))
    assert identity_error < 1e-15

    sensor_irradiance = 10.0 * math.pi * 0.005**2 / 0.05**2
    result = {
        "kind": "original mathematical examples; not Unreal measurements",
        "M02_hemisphere_power_W": 0.02 * 10.0 * math.pi,
        "M03_cone_approx_power_W": 0.02 * 10.0 * 0.8 * 0.5,
        "M03_cone_exact_power_W": cone_exact,
        "M03_cone_quadrature_power_W": cone_quadrature,
        "M03_quadrature_absolute_error_W": abs(cone_quadrature - cone_exact),
        "M07_constant_environment_radiance": 0.8 * 2.0,
        "M13_received_patch_power_W": 5.0 * 0.25 * 1e-8,
        "M13_received_patch_irradiance_W_m2": 5.0 * 0.25 * 1e-4,
        "M13_outgoing_patch_radiance_W_m2_sr": 0.2 * 5.0 * 0.25 * 1e-4,
        "M24_sensor_irradiance_W_m2": sensor_irradiance,
        "M24_pixel_power_W": sensor_irradiance * (5e-6)**2,
        "M27_nee_sample_radiance": (0.8 / math.pi) * 10.0 / 16.0,
        "M30_roulette_mean": 0.25 * 0.8,
        "M30_roulette_variance": 0.25 * (0.8 - 0.2)**2 + 0.75 * 0.2**2,
        "M42_exact_radiance": exact,
        "M42_approximate_radiance": approximate,
        "M42_iterated_radiance": iteration,
        "M40_operator_identity_max_error": identity_error,
        "checks": "pass",
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
