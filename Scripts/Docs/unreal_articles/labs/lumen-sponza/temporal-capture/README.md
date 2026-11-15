# Temporal capture provenance

These scripts preserve the three native Unreal capture experiments used for the temporal figures. They contain historical workstation paths and depend on the instrumented engine bridge described by the main Sponza laboratory. They are a provenance snapshot, not a portable installer. Supply your engine/project paths and validate the capture plan before launching.

The retained scene is `/Game/Labs/SponzaTeaching` in the UnrealLab project. The publication uses the accepted light run `20261115-101057`, motion run `20261115-104035`, and radiosity run `20261115-105951`. Rejected or superseded observations are not substituted into these sequences. Engine-frame counters, actual update counts, native view state, resource versions and parameter readbacks remain in the archived capture manifests.

The light sequence changes illuminance with a fixed view. The motion sequence changes the camera in one process with illumination and exposure fixed. The radiosity sequence captures the surface atlases and SH operands, restores and warms the original light, then changes the camera. These experiments have separate timelines.

For CPU-only reproduction, reconstruct the selected native state directories with `../archive_temporal.py` and pass the resulting roots to the decoders. Figures are derived from native bytes and matching screenshots. No GPU run is required to verify the archived bytes or inspect the published numeric arrays.
