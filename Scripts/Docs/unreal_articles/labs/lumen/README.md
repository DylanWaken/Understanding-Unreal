# Computed room laboratory

This is an educational CPU renderer, not Unreal Engine's Lumen implementation.
The rendered figures carry its output label. The Unreal project was captured
independently and has its own provenance.

From the repository root:

```powershell
python -m pip install -r Scripts/Docs/unreal_articles/labs/lumen/requirements.txt
python Scripts/Docs/unreal_articles/labs/lumen/render.py
```

Verified environment: Python 3.12, NumPy 2.3.5, SciPy 1.18.1,
Matplotlib 3.11.2, Pillow 12.3.0. The script takes several seconds on the
development machine; this is not a renderer benchmark.

The scene is an open-front 4m × 5m × 3m room, with red/green side walls,
two boxes, a rectangular ceiling emitter, and a black exterior. Exact
rectangle intersections provide reference visibility. A separate exact-box
SDF diagnostic compares sphere tracing with the rectangle reference.

`render.py` computes material card arrays, direct-light quadrature, a sparse
sampled diffuse transport matrix and its fixed point, world-space directional
probes, screen-space probes, visibility-aware reconstruction comparisons,
rough GGX reflection, and a camera-movement history validation example.

Outputs in `Docs/Unreal/assets/lumen-lab/` include:

- PNG diagrams generated from computed arrays; one SVG from logged SDF steps.
- `scene.json`: geometry, material, and camera description in meters.
- `metrics.json`: actual counts, residuals, errors, trace checks, and seed.
- `computed-data.npz`: positions, normals, albedo, direct and cached radiance,
  directional probe radiance, irradiance, screen-probe values, and SDF slice.

The experiment has these limits: all cards are resident; material captures
use exact planar parameterization; geometry is static; transport is
Lambertian; rays are exact rectangle queries. The SDF diagnostic is not the
lighting visibility backend. World and screen probes are separate experiments
rather than a composed trace hierarchy. Reflection reads a diffuse cache and
uses full-NDF GGX sampling rather than Unreal's visible-GGX sampler. The
script has no screen-space ray marcher, hardware RT, sparse atlas residency,
large-world clipmaps, stochastic budget scheduler, or complete production
denoiser. Every comparison states its reference.

Assertions check sphere-trace agreement for 256 horizontal rays, spherical
solid-angle normalization, finite/nonnegative radiance, and convergence of
the frozen sampled operator. A small residual is not a proof of unbiased
continuum light transport.

`prepare.py` orders the authored essay and updates evidence hashes from
`D:/UnrealEngine`. It is an authoring helper, not a public build dependency.
