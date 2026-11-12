# Actual Unreal experiment

`baseline/` preserves the reference capture session, its minimal Unreal project,
portable capture scripts and the original DumpGPU metadata. The verified Lumen
and 256/1024-sample path-traced images use the same saved room and camera.
Follow [the reproduction instructions](baseline/reproduction.md).

The baseline dump has explicitly recorded missing resource classes. Its figures
and pointer IDs describe that capture only; they must not be combined with
binaries from another capture. The extended capture and frame explorer have
their own provenance and coverage records.

The baseline package is frozen byte for byte, including its redaction record.
Verification of this copied package passed all 6,001 checks. These checks establish
artifact consistency; the explicit coverage records describe what was captured.
