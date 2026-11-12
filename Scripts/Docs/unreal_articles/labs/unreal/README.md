# Actual Unreal renderer laboratory

These scripts generated the Nanite bunny, Lumen room, scene-representation screenshots, and measured GPU atlas displays used by the essays. They run the Unreal editor renderer on a real GPU. The separate `../nanite` and `../lumen` Python laboratories are readable teaching implementations; their arrays are not Unreal renderer internals.

The recorded run used the locally built Unreal **5.8.1-0+UE5**, Direct3D 12, `PCD3D_SM6`, and an NVIDIA RTX PRO 6000 Blackwell Workstation Edition. `capture-manifest.json` preserves the engine build fields, figure hashes, and source hashes. This checkout reports changelist zero, so that number does not identify a released build.

## Reproduce

Prerequisites: this Unreal source build and its Windows C++ toolchain, Python for downloading the Stanford data, and NumPy/Pillow for decoding the optional GPU readback. No engine source edits are required. The project-local `ArdaLabCapture` editor module exposes the small API gap between Python and actual viewport rendering.

From the repository root in PowerShell:

```powershell
# Downloads the attributed Stanford source and exports the 100 cm OBJ.
python Scripts/Docs/unreal_articles/labs/nanite/download.py

# Build the project-local editor module once, then make the indoor room.
./Scripts/Docs/unreal_articles/labs/unreal/run.ps1 -Build -Script capture.py

# Import the OBJ, run the real Nanite asset builder, and render its debug modes.
./Scripts/Docs/unreal_articles/labs/unreal/run.ps1 -Script capture_bunny.py

# Read back real Lumen textures from the saved baseline room.
./Scripts/Docs/unreal_articles/labs/unreal/run.ps1 -Script capture_gpu.py

python Scripts/Docs/unreal_articles/labs/unreal/export_atlases.py `
  "$env:LOCALAPPDATA/Temp/arda-unreal-lab/gpu-dump-final" `
  Docs/Unreal/assets/unreal-lab
python Scripts/Docs/unreal_articles/labs/unreal/publish_captures.py `
  "$env:LOCALAPPDATA/Temp/arda-unreal-lab" `
  Docs/Unreal/assets/unreal-lab
```

`run.ps1` accepts `-EngineRoot` and `-LabRoot`. The Stanford downloader uses the default lab root; copy its generated `shared/bunny.obj` if choosing another root. Run the room script before the bunny script to provide the shared neutral floor material, and before the GPU dump to save `/Game/Labs/LumenRoom`. Run the GPU dump once in a fresh output directory if comparing multiple executions. Each run opens a hidden, offscreen, unattended editor process and exits after the script completes. First-run shader compilation and asset generation take longer.

The reusable project is created outside the repository at `%LOCALAPPDATA%/Temp/arda-unreal-lab/UnrealLab`. It contains `/Game/Labs/LumenRoom`, `/Game/Labs/NaniteBunny`, generated materials and the actual Nanite asset. Engine binaries, Derived Data Cache, raw GPU dumps, and Stanford source geometry are not committed. Stanford provenance and source usage terms are recorded in `../nanite/provenance.json`.

## What is measured

`capture.py` builds seven `StaticMeshActor` instances sharing `/Engine/BasicShapes/Cube.Cube`: five room surfaces and two blocks. `scene-inventory.json` exports actual actor/component types and paths, per-component transforms and bounds, asset/material references, and light parameters. Room coordinates match the analytic teaching lab through **UE centimeters = (-100 x, 100 y, 100 z)**. Reflecting X keeps the red wall on screen-left in both coordinate conventions.

The open-front room spans x=[-2,2], y=[0,5], z=[0,3] meters. Separate walls are 10 cm thick. The ceiling `RectLightComponent` is 130×120 cm, 2400 lumens, at UE (0,270,298), pointing down. The camera is UE (0,-400,165), looking at (0,280,140), with vertical FOV 43 degrees. Fixed exposure is EV100=4. Debug mesh/global SDF captures use EV100=-1 because this engine's SDF visualization contains directional shading. Black debug faces are not necessarily missing geometry. Strong nonuniform scales on the shared cube make its transformed distance field approximate; this room is a compact demonstration, not a claim of exact analytic-box SDFs.

The moved-block image changes the matte block from UE (77.5,285,52.5) to (-22.5,205,52.5) cm. The next image retains that move and changes the light to 7200 lumens with linear color (1,.55,.25). This is an updated renderer state, not a timing or temporal-convergence experiment.

`capture_bunny.py` imports the 69,451-triangle Stanford bunny, enables its real static-mesh Nanite build, and queries the resulting `Nanite::FResources`. This run produced **1,133 clusters across all built LODs, 8 pages including 1 root page, and 23 hierarchy nodes**. These are asset totals, not visible-cluster counts. The OBJ declares 35,947 vertices; the built resource records 34,834 input vertices. A queried zero-sized streamable bulk field does not measure the encoded stream or indicate that no streaming data exists.

Near and far camera distances differ by a factor of four. `r.Nanite.MaxPixelsPerEdge=1` is held constant. Triangles, Clusters, Groups, Pages, Hierarchy, RasterMode, RasterBins, ShadingBins, and Overdraw are native Nanite visualization modes. Their colors are engine diagnostics, not artist-authored partitions. A separate material enables actual Nanite tessellation using

```text
h(x) = 0.5 + 0.5 sin(2 pi * 0.05 * x_cm)
displacement = magnitude * (h - center)
magnitude = 2 cm; center = 0.5
```

The period is 20 cm and signed displacement ranges from -1 to +1 cm along the surface normal. The material uses world position excluding material offsets. The Patches, Triangles, and Tessellation captures are produced after compiling and applying that real material. Baseline bunny captures use an ordinary clay material without displacement.

## Capture semantics

The editor's `FSceneViewport` is explicitly drawn at 1280×720, using a persistent piloted camera. `FScreenshotRequest` saves the ordinary viewport without UI. There are 200 initial draw requests and 90 requests after each camera, material, or mode change. These counts are warm-up instructions, not evidence of complete convergence. Stochastic lighting noise remains. No screenshot pixels are composited, repainted, or synthesized.

BaseColor, WorldNormal, Roughness and Metallic use native `VMI_VisualizeBuffer` targets. Wireframe and bounds are native viewport modes. `scene-depth.png` instead uses a native postprocess material evaluating **SceneDepth in centimeters divided by 1000**, after tonemapping; white corresponds to 10 meters. It is a readable linear-distance display, not raw hardware device-Z. The manifest and report preserve that distinction.

`lumen-capture-report.json` records commands in execution order. Commands are incremental, so a later capture inherits the recorded earlier settings unless explicitly reset. The hardware-tracing image has screen traces disabled by the preceding diagnostic; it is not a default-quality benchmark against the first lit image. `lumen-screen-probes.png` requires all three of `r.ShaderPrint.Enable=1`, `r.Lumen.ScreenProbeGather.Debug=1`, and `r.Lumen.ScreenProbeGather.Debug.ProbePlacement=1`. Its native counters show 3600 uniform probes and zero adaptive probes for this view; it does not demonstrate adaptive placement. `lumen-probe-traces.png`, `lumen-radiance-cache.png`, and `lumen-radiosity-probes.png` visualize different probe families and should not be interchanged.

## Actual GPU readback displays

`capture_gpu.py` invokes Unreal's local `DumpGPU` with upload and Explorer launch disabled. It temporarily disables surface-cache compression to obtain simple uncompressed formats, warms the saved baseline room, and reads selected GPU textures. The raw dump can be hundreds of megabytes and stays outside the repository. `export_atlases.py` decodes the measured arrays; it does not reconstruct or simulate the caches.

The seven exported images are the surface albedo, packed surface normal, card-space depth, final surface lighting, radiance-cache final radiance, screen-probe trace radiance, and filtered screen-probe radiance. `gpu-atlas-report.json` gives each engine resource name, pixel format, full texture extent, active-area crop, selected raw resource/version, raw SHA-256, and display transform. All arrays use mip zero. When duplicate resource names represent different cache families, the exporter chooses the largest texture. It follows RDG pass order to select the last written version; hexadecimal pass addresses are not treated as chronological timestamps.

Stored 8-bit channels are shown directly. The packed normal atlas displays RG with blue zero; it is not decoded into world normals. Card depth is stored normalized depth, not camera distance. Radiance uses Unreal's R11G11B10 unsigned-float decoding, a per-texture exposure placing the positive-channel 95th percentile at one, Reinhard `x/(1+x)`, and the IEC sRGB transfer. Brightness therefore cannot be compared numerically between different atlas PNGs. Empty margins are cropped with a four-pixel border; full extents and crop coordinates remain in the report. These figures are labeled **measured GPU array displays**, distinct from camera screenshots.

## Source landmarks

- `Engine/Source/Runtime/Renderer/Private/Lumen/LumenVisualize.cpp`: visualization numbers and renderer labels.
- `Engine/Shaders/Private/DistanceFieldVisualization.usf`: the current mesh/global SDF debug shading.
- `Engine/Source/Runtime/Renderer/Private/Lumen/LumenScreenProbeGather.cpp` and associated shaders: screen-probe debug controls.
- `Engine/Source/Runtime/Engine/Public/Rendering/NaniteResources.h`: queried Nanite resource fields.
- `Engine/Source/Runtime/Engine/Private/NaniteVisualizationData.cpp`: native Nanite debug modes.
- `Engine/Source/Runtime/RenderCore/Private/RenderGraphTrace.cpp`: GPU resource dumping.

All figure-specific commands are in the generated reports and executable scripts. Engine debug modes, Python APIs, and texture formats can change between revisions; validate them against the source version used for any new capture.
