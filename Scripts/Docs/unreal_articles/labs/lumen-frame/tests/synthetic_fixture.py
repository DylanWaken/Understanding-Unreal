#!/usr/bin/env python3
"""Generate a deterministic, clearly-labelled synthetic DumpGPU fixture.

The fixture mirrors the on-disk contract verified in
Engine/Source/Runtime/RenderCore/Private/DumpGPU.cpp:

    Base/Infos.json, Base/Passes.json, Base/ResourceDescs.json,
    Base/PassDrawCounts.json, Base/ReferencedStructs.json,
    Base/ConsoleVariables.csv, Base/Status.txt,
    Resources/<unique>[.mipN|.[slice].mipN|.stencil].v<pass>.bin,
    Structures/<pointer>.bin, StructuresMetadata/<pointer>.json

It contains synthetic bytes only.  It must never be presented as runtime evidence.
"""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

CHUNK = 1024 * 1024
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z2S8AAAAASUVORK5CYII="
)


def _jsonl(items: list[dict]) -> str:
    return "\n".join(json.dumps(item, indent=2) for item in items) + "\n"


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def build_fixture(root: Path) -> Path:
    if root.exists():
        import shutil
        shutil.rmtree(root)
    base = root / "Base"
    resources = root / "Resources"
    structures = root / "Structures"
    structures_meta = root / "StructuresMetadata"
    for p in (base, resources, structures, structures_meta):
        p.mkdir(parents=True, exist_ok=True)

    p1 = "00010000000000000001"
    p2 = "00010000000000000002"
    p3 = "00010000000000000003"
    tex_a = "0001000000000000000a"
    buf_b = "0001000000000000000b"
    buf_large = "0001000000000000000c"
    buf_dupe = "0001000000000000000d"
    tex_unknown = "0001000000000000000e"
    tex_missing = "0001000000000000000f"

    infos = {
        "Project": "SyntheticFrameFixture",
        "EngineMajorVersion": 5,
        "EngineMinorVersion": 8,
        "EnginePatchVersion": 1,
        "BuildBranch": "synthetic-test",
        "BuildVersion": "5.8.1-synthetic",
        "BuildTarget": "Editor",
        "BuildConfiguration": "Development",
        "Platform": "Windows",
        "OS": "Synthetic fixture",
        "DeviceName": "not-a-real-device",
        "GPUVendor": "Synthetic",
        "GPUDeviceDescription": "Synthetic fixture GPU (no capture)",
        "RHI": "SyntheticRHI",
        "RHIMaxFeatureLevel": "SM6",
        "DumpTime": "2000.01.01-00.00.00",
        "DumpContext": "Synthetic test fixture (not runtime evidence)",
        "LogFilename": "SyntheticFixture.log",
    }
    _write(base / "Infos.json", json.dumps(infos, indent=2).encode())
    _write(base / "Status.txt", b"ok\n")
    _write(base / "ConsoleVariables.csv",
           b"Name,Type,SetBy,Value\n"
           b"r.DumpGPU.Texture,int,console,2\n"
           b"r.DumpGPU.Buffer,int,console,2\n"
           b"r.DumpGPU.PassParameters,int,console,1\n")
    _write(base / "Screenshot.png", PNG_1X1)
    _write(base / "ExtraRecords.json", _jsonl([
        {"Note": "synthetic extra record one", "Value": 1},
        {"Note": "synthetic extra record two", "Value": 2},
    ]).encode())

    resource_descs = [
        {
            "Name": "Lumen.Synthetic.DirectLighting",
            "UniqueResourceName": tex_a,
            "ByteSize": 32,
            "Desc": "FRDGTextureDesc",
            "Type": "Texture2D",
            "Format": "PF_FloatRGBA",
            "ExtentX": 2, "ExtentY": 2, "Depth": 1, "ArraySize": 1,
            "NumMips": 1, "NumSamples": 1,
            "Flags": ["ShaderResource", "RenderTargetable"],
        },
        {
            "Name": "Lumen.Synthetic.TileData",
            "UniqueResourceName": buf_b,
            "ByteSize": 64,
            "Desc": "FRDGBufferDesc",
            "BytesPerElement": 16,
            "NumElements": 4,
            "Metadata": "00000000000000000301",
        },
        {
            "Name": "Lumen.Synthetic.RadianceCacheProbeData",
            "UniqueResourceName": buf_large,
            "ByteSize": 2 * CHUNK + 124,
            "Desc": "FRDGBufferDesc",
            "BytesPerElement": 4,
            "NumElements": (2 * CHUNK + 124) // 4,
            "Usage": ["ShaderResource", "UnorderedAccess"],
        },
        {
            "Name": "Lumen.Synthetic.DuplicateProbeData",
            "UniqueResourceName": buf_dupe,
            "ByteSize": 2 * CHUNK + 124,
            "Desc": "FRDGBufferDesc",
            "BytesPerElement": 4,
            "NumElements": (2 * CHUNK + 124) // 4,
            "Usage": ["ShaderResource", "UnorderedAccess"],
        },
        {
            "Name": "Lumen.Synthetic.AmbiguousFormat",
            "UniqueResourceName": tex_unknown,
            "ByteSize": 8,
            "Desc": "FRDGTextureDesc",
            "Type": "Texture2D",
            "Format": "PF_R16G16B16A16_UINT",
            "ExtentX": 1, "ExtentY": 1, "Depth": 1, "ArraySize": 1,
            "NumMips": 1, "NumSamples": 1,
            "Flags": ["ShaderResource"],
        },
        {
            "Name": "Lumen.Synthetic.MissingPayload",
            "UniqueResourceName": tex_missing,
            "ByteSize": 16,
            "Desc": "FRDGTextureDesc",
            "Type": "Texture2D",
            "Format": "PF_FloatRGBA",
            "ExtentX": 1, "ExtentY": 1, "Depth": 1, "ArraySize": 1,
            "NumMips": 1, "NumSamples": 1,
            "Flags": ["ShaderResource"],
        },
    ]
    _write(base / "ResourceDescs.json", _jsonl(resource_descs).encode())

    passes = [
        {
            "EventName": "Lumen.Synthetic.DirectLighting",
            "ParametersName": "FLumenSyntheticDirectLightingCS",
            "Parameters": "00000000000000000101",
            "ParametersMetadata": "00000000000000000201",
            "Pointer": p1,
            "Id": 0,
            "ParentEventScopes": ["Frame 42 (Delta=0.016000s)"],
            "InputResources": [f"{tex_a}.mip0"],
            "OutputResources": [f"{tex_a}.mip0"],
        },
        {
            "EventName": "Lumen.Synthetic.CompositeLighting CS",
            "ParametersName": "FCompositeLightingCS",
            "Parameters": "00000000000000000102",
            "ParametersMetadata": "00000000000000000202",
            "Pointer": p2,
            "Id": 1,
            "ParentEventScopes": ["Frame 42 (Delta=0.016000s)"],
            "InputResources": [f"{tex_a}.mip0"],
            "OutputResources": [buf_b],
        },
        {
            "EventName": "Lumen.Synthetic.PostProcess",
            "ParametersName": "FPostProcessPS",
            "Parameters": "00000000000000000103",
            "ParametersMetadata": "00000000000000000203",
            "Pointer": p3,
            "Id": 2,
            "ParentEventScopes": ["Frame 42 (Delta=0.016000s)"],
            "InputResources": [buf_b],
            "OutputResources": [f"{tex_unknown}.mip0", f"{buf_large}", f"{buf_dupe}"],
        },
    ]
    _write(base / "Passes.json", _jsonl(passes).encode())
    _write(base / "PassDrawCounts.json", _jsonl([
        {"Pointer": p1, "DrawCount": 1},
        {"Pointer": p2, "DrawCount": 2},
    ]).encode())
    _write(base / "ReferencedStructs.json", _jsonl([
        {
            "Pass": p1,
            "ReferencedStructs": {
                "Pass.LightData": {
                    "Content": "00000000000000000302",
                    "Metadata": "00000000000000000203",
                }
            },
        }
    ]).encode())

    # Parameter metadata: float, uint, and a three-component vector.
    struct_201 = {
        "StructTypeName": "FLumenSyntheticDirectLightingCS",
        "ShaderVariableName": "LumenSyntheticDirectLighting",
        "FileName": "SyntheticShaders.usf",
        "FileLine": 42,
        "Size": 32,
        "Members": [
            {"Name": "LightingMode", "ShaderType": "float", "FileLine": 43, "Offset": 0,
             "BaseType": "UBMT_FLOAT32", "Precision": 1, "NumRows": 1, "NumColumns": 1,
             "NumElements": 1, "StructMetadata": ""},
            {"Name": "FrameIndex", "ShaderType": "uint", "FileLine": 44, "Offset": 4,
             "BaseType": "UBMT_UINT32", "Precision": 1, "NumRows": 1, "NumColumns": 1,
             "NumElements": 1, "StructMetadata": ""},
            {"Name": "LightDirection", "ShaderType": "float3", "FileLine": 45, "Offset": 8,
             "BaseType": "UBMT_FLOAT32", "Precision": 1, "NumRows": 3, "NumColumns": 1,
             "NumElements": 1, "StructMetadata": ""},
            {"Name": "bEnabled", "ShaderType": "bool", "FileLine": 46, "Offset": 20,
             "BaseType": "UBMT_BOOL", "Precision": 1, "NumRows": 1, "NumColumns": 1,
             "NumElements": 1, "StructMetadata": ""},
        ],
    }
    struct_202 = {
        "StructTypeName": "FCompositeLightingCS",
        "ShaderVariableName": "CompositeLighting",
        "FileName": "SyntheticShaders.usf",
        "FileLine": 60,
        "Size": 16,
        "Members": [
            {"Name": "Scale", "ShaderType": "float", "FileLine": 61, "Offset": 0,
             "BaseType": "UBMT_FLOAT32", "Precision": 1, "NumRows": 1, "NumColumns": 1,
             "NumElements": 1, "StructMetadata": ""},
            {"Name": "Mode", "ShaderType": "uint", "FileLine": 62, "Offset": 4,
             "BaseType": "UBMT_UINT32", "Precision": 1, "NumRows": 1, "NumColumns": 1,
             "NumElements": 1, "StructMetadata": ""},
        ],
    }
    struct_203 = {
        "StructTypeName": "FReferencedLightData",
        "ShaderVariableName": "LightData",
        "FileName": "SyntheticShaders.usf",
        "FileLine": 70,
        "Size": 16,
        "Members": [
            {"Name": "LightColor", "ShaderType": "float3", "FileLine": 71, "Offset": 0,
             "BaseType": "UBMT_FLOAT32", "Precision": 1, "NumRows": 3, "NumColumns": 1,
             "NumElements": 1, "StructMetadata": ""},
            {"Name": "Intensity", "ShaderType": "float", "FileLine": 72, "Offset": 12,
             "BaseType": "UBMT_FLOAT32", "Precision": 1, "NumRows": 1, "NumColumns": 1,
             "NumElements": 1, "StructMetadata": ""},
        ],
    }
    struct_301 = {
        "StructTypeName": "FSyntheticTileData",
        "ShaderVariableName": "TileData",
        "FileName": "SyntheticShaders.usf",
        "FileLine": 80,
        "Size": 16,
        "Members": [
            {"Name": "TileRadiance", "ShaderType": "float4", "FileLine": 81, "Offset": 0,
             "BaseType": "UBMT_FLOAT32", "Precision": 1, "NumRows": 1, "NumColumns": 1,
             "NumElements": 4, "StructMetadata": ""},
        ],
    }
    for pointer, value in {
        "00000000000000000201": struct_201,
        "00000000000000000202": struct_202,
        "00000000000000000203": struct_203,
        "00000000000000000301": struct_301,
    }.items():
        _write(structures_meta / f"{pointer}.json", json.dumps(value, indent=2).encode())

    _write(structures / "00000000000000000101.bin",
           struct.pack("<fI3fI", 2.0, 42, 0.25, -0.5, 0.75, 1))
    _write(structures / "00000000000000000102.bin", struct.pack("<fI", 1.5, 7))
    _write(structures / "00000000000000000103.bin", b"\x00" * 32)
    _write(structures / "00000000000000000302.bin",
           struct.pack("<3fI", 0.9, 0.8, 0.7, 2400))

    # 2x2 half-float RGBA texels.  Values deliberately include exact halves.
    texel_bytes = b"".join(struct.pack("<e", v) for v in (
        1.0, 0.5, 2.0, 1.0,
        0.25, 0.75, 0.0, 1.0,
        2.0, 1.0, 0.5, 0.25,
        0.125, 0.375, 4.0, 1.0,
    ))
    _write(resources / f"{tex_a}.mip0.v{p1}.bin", texel_bytes)
    _write(resources / f"{tex_unknown}.mip0.v{p3}.bin", b"\x11\x22\x33\x44\x55\x66\x77\x88")

    buffer_b = bytearray()
    for i in range(4):
        buffer_b += struct.pack("<4f", float(i), float(i + 1), float(i + 2), float(i + 3))
    assert len(buffer_b) == 64
    _write(resources / f"{buf_b}.v{p2}.bin", bytes(buffer_b))

    # Two identical large buffers exercise dedup; a half float at the 1 MiB
    # boundary exercises cross-chunk reads (offset CHUNK-2..CHUNK).
    large = bytearray(((i * 37 + 11) & 0xFF) for i in range(2 * CHUNK + 124))
    large[CHUNK - 2:CHUNK] = struct.pack("<H", 0x3C00)  # 1.0 crossing chunk 0/1
    large[2 * CHUNK - 2:2 * CHUNK] = struct.pack("<H", 0xC000)  # -2.0 crossing chunk 1/2
    large_bytes = bytes(large)
    _write(resources / f"{buf_large}.v{p3}.bin", large_bytes)
    _write(resources / f"{buf_dupe}.v{p3}.bin", large_bytes)

    # An unreferenced bin must be retained and reported, not silently dropped.
    _write(resources / "orphan.bin", b"orphan-bytes-not-in-any-pass")

    return root


def add_instrumented_sidecars(root: Path) -> Path:
    """Add the instrumented exporter sidecars to the synthetic fixture.

    The fixture is still synthetic, but the records have the exact shape of the
    final capture: per-file SubresourceLayouts, DumpReadbackStatus completion,
    NativeUniformBufferBindings with typed native-buffer subranges and a reused
    native view, ParameterBindings, and expanded ReferencedStructs.  It also
    contains a descriptive (non-PF) schema layout, unknown future fields, and a
    scheduled readback with no completion record so scheduling is not mistaken
    for success.
    """
    base = root / "Base"
    resources = root / "Resources"
    p1 = "00010000000000000001"
    p2 = "00010000000000000002"
    tex_3d = "00010000000000000020"
    tex_cube = "00010000000000000021"
    tex_array = "00010000000000000022"
    tex_bc7 = "00010000000000000023"
    tex_no_completion = "00010000000000000024"
    native_buf = "00050000000000000030"
    skip_buf = "00050000000000000031"

    def append_jsonl(path: Path, items: list[dict]) -> None:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        path.write_text(existing + _jsonl(items), encoding="utf-8")

    append_jsonl(base / "ResourceDescs.json", [
        {
            "Name": "Lumen.Synthetic.Volume3D",
            "UniqueResourceName": tex_3d, "ByteSize": 32,
            "Desc": "FRDGTextureDesc", "Type": "Texture3D",
            "Format": "PF_R32_FLOAT", "ExtentX": 2, "ExtentY": 2, "Depth": 2,
            "ArraySize": 1, "NumMips": 1, "NumSamples": 1, "Flags": ["ShaderResource"],
        },
        {
            "Name": "Lumen.Synthetic.CubeFaces",
            "UniqueResourceName": tex_cube, "ByteSize": 24,
            "Desc": "FRDGTextureDesc", "Type": "TextureCube",
            "Format": "PF_R32_FLOAT", "ExtentX": 1, "ExtentY": 1, "Depth": 1,
            "ArraySize": 6, "NumMips": 1, "NumSamples": 1, "Flags": ["ShaderResource"],
        },
        {
            "Name": "Lumen.Synthetic.ArrayMips",
            "UniqueResourceName": tex_array, "ByteSize": 40,
            "Desc": "FRDGTextureDesc", "Type": "Texture2DArray",
            "Format": "PF_R32_FLOAT", "ExtentX": 2, "ExtentY": 2, "Depth": 1,
            "ArraySize": 2, "NumMips": 2, "NumSamples": 1, "Flags": ["ShaderResource"],
        },
        {
            "Name": "Lumen.Synthetic.ConvertedBC7",
            "UniqueResourceName": tex_bc7, "ByteSize": 16,
            "Desc": "FRDGTextureDesc", "Type": "Texture2D",
            "Format": "PF_BC7", "ExtentX": 1, "ExtentY": 1, "Depth": 1,
            "ArraySize": 1, "NumMips": 1, "NumSamples": 1, "Flags": ["ShaderResource"],
        },
        {
            "Name": "Lumen.Synthetic.NoCompletion",
            "UniqueResourceName": tex_no_completion, "ByteSize": 4,
            "Desc": "FRDGTextureDesc", "Type": "Texture2D",
            "Format": "PF_R32_UINT", "ExtentX": 1, "ExtentY": 1, "Depth": 1,
            "ArraySize": 1, "NumMips": 1, "NumSamples": 1, "Flags": ["ShaderResource"],
        },
        {
            "Name": "Synthetic.NativeTypedBuffer",
            "UniqueResourceName": native_buf, "ByteSize": 16,
            "Desc": "FRDGBufferDesc", "BytesPerElement": 4, "NumElements": 4,
            "Metadata": "00000000000000000000",
            "Usage": ["ShaderResource", "StructuredBuffer"],
        },
        {
            "Name": "Synthetic.SkipTrackingBuffer",
            "UniqueResourceName": skip_buf, "ByteSize": 4,
            "Desc": "FRDGBufferDesc", "BytesPerElement": 4, "NumElements": 1,
            "Metadata": "00000000000000000000", "Usage": ["ShaderResource"],
        },
    ])
    append_jsonl(base / "Passes.json", [
        {
            "EventName": "Lumen.Synthetic.InstrumentedVolume",
            "ParametersName": "FSyntheticInstrumented",
            "Parameters": "00000000000000000106",
            "ParametersMetadata": "00000000000000000206",
            "Pointer": "00010000000000000006",
            "Id": 6,
            "ParentEventScopes": ["Frame 42 (Delta=0.016000s)"],
            "InputResources": [f"{tex_3d}.mip0", f"{tex_cube}.[0].mip0", f"{tex_array}.[0].mip0"],
            "OutputResources": [f"{tex_bc7}.mip0", f"{tex_no_completion}.mip0"],
        },
    ])
    append_jsonl(base / "PassDrawCounts.json", [{"Pointer": "00010000000000000006", "DrawCount": 3}])
    _write(root / "Structures" / "00000000000000000106.bin", struct.pack("<4f", 3.0, 4.0, 5.0, 6.0))
    _write(root / "StructuresMetadata" / "00000000000000000206.json", json.dumps({
        "StructTypeName": "FSyntheticInstrumented", "ShaderVariableName": "SyntheticInstrumented",
        "FileName": "SyntheticShaders.usf", "FileLine": 100, "Size": 16,
        "Members": [
            {"Name": "Scale", "ShaderType": "float", "FileLine": 101, "Offset": 0,
             "BaseType": "UBMT_FLOAT32", "Precision": 1, "NumRows": 1, "NumColumns": 1,
             "NumElements": 1, "StructMetadata": ""},
        ],
    }, indent=2).encode())

    # 3D volume: 2x2x2 float, one file holding both z slices.
    xyz = struct.pack("<8f", 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0)
    _write(resources / f"{tex_3d}.mip0.v{p1}.bin", xyz)
    # Cube: six 1x1 faces with distinct values.
    cube_files = []
    for face in range(6):
        face_file = f"Resources/{tex_cube}.[{face}].mip0.v{p1}.bin"
        _write(resources / face_file.split("/", 1)[1], struct.pack("<f", float(10 + face)))
        cube_files.append(face_file)
    # Array with two slices and two mips.
    array_files = []
    for slice_index in range(2):
        for mip, extent in ((0, 2), (1, 1)):
            array_file = f"Resources/{tex_array}.[{slice_index}].mip{mip}.v{p1}.bin"
            values = [float(slice_index * 10 + mip * 100 + i) for i in range(extent * extent)]
            _write(resources / array_file.split("/", 1)[1], struct.pack("<%df" % len(values), *values))
            array_files.append((array_file, slice_index, mip, extent))
    # BC7 native descriptor is exported as half4 FLOATRGBA (8 B/texel).
    _write(resources / f"{tex_bc7}.mip0.v{p1}.bin", struct.pack("<4e", 1.0, 0.5, 2.0, 1.0))
    # Scheduled but no completion record.
    _write(resources / f"{tex_no_completion}.mip0.v{p1}.bin", struct.pack("<I", 0xDEADBEEF))
    # Native typed buffer subrange: elements 1..2 of a 4-element uint buffer.
    _write(resources / f"NativeBuffer.{native_buf}.o4.n2.v{p1}.bin", struct.pack("<2I", 111, 222))
    # Skip-tracking buffer version file.
    _write(resources / f"{skip_buf}.v{'0' * 20}.bin", struct.pack("<I", 777))

    layouts = [
        {
            "Resource": tex_3d, "Name": "Lumen.Synthetic.Volume3D",
            "File": f"Resources/{tex_3d}.mip0.v{p1}.bin",
            "Dimension": "Texture3D", "MipLevel": 0, "FirstArraySlice": 0, "NumArraySlices": 0,
            "ExtentX": 2, "ExtentY": 2, "Depth": 2, "ZSliceCount": 2,
            "ZSliceStrideBytes": 16, "RowPitchBytes": 8, "ZSliceOrder": "ascending z, tightly packed",
            "NativeFormat": "PF_R32_FLOAT", "ViewFormat": "PF_R32_FLOAT", "ViewFormatMatchesNativeFormat": True,
            "StagingFormat": "PF_R32_FLOAT", "StoredFormat": "PF_R32_FLOAT",
            "NativeFormatExpanded": False, "NativeSRGB": False, "ViewSRGB": False, "SRGBOverride": "Default",
            "SRGBViewUsedForReadback": False, "StoredBytesAreNativeStorage": True,
            "NativeValueInterpretation": "DecodedFloat", "BitExact": True, "ValuePreserving": True,
            "BitExactCondition": "synthetic exact float copy", "MipExtentClampedToMinimum": False,
            "StencilSubresource": False, "Readback": "SynchronousComputePerSlice",
            "DumpStatus": "ScheduledForReadback", "FileVerifiedAtSchedule": False,
            "ByteSize": 32, "StagingByteSize": 32,
            "FutureExporterField": {"nested": ["retained", 7], "unknown": True},
        },
    ]
    for face, face_file in enumerate(cube_files):
        layouts.append({
            "Resource": tex_cube, "Name": "Lumen.Synthetic.CubeFaces",
            "File": face_file, "Dimension": "TextureCube", "MipLevel": 0,
            "FirstArraySlice": face, "NumArraySlices": 1, "CubeFace": face,
            "ExtentX": 1, "ExtentY": 1, "Depth": 1, "ZSliceCount": 1,
            "ZSliceStrideBytes": 4, "RowPitchBytes": 4, "ZSliceOrder": "single slice",
            "NativeFormat": "PF_R32_FLOAT", "ViewFormat": "PF_R32_FLOAT", "ViewFormatMatchesNativeFormat": True,
            "StagingFormat": "PF_R32_FLOAT", "StoredFormat": "PF_R32_FLOAT",
            "NativeFormatExpanded": False, "NativeSRGB": False, "ViewSRGB": False, "SRGBOverride": "Default",
            "SRGBViewUsedForReadback": False, "StoredBytesAreNativeStorage": False,
            "NativeValueInterpretation": "DecodedFloat", "BitExact": True, "ValuePreserving": True,
            "BitExactCondition": "synthetic exact float copy", "MipExtentClampedToMinimum": False,
            "StencilSubresource": False, "Readback": "ComputeThenCopy",
            "DumpStatus": "ScheduledForReadback", "FileVerifiedAtSchedule": False,
            "ByteSize": 4, "StagingByteSize": 4,
        })
    for array_file, slice_index, mip, extent in array_files:
        layouts.append({
            "Resource": tex_array, "Name": "Lumen.Synthetic.ArrayMips",
            "File": array_file, "Dimension": "Texture2DArray", "MipLevel": mip,
            "FirstArraySlice": slice_index, "NumArraySlices": 1,
            "ExtentX": extent, "ExtentY": extent, "Depth": 1, "ZSliceCount": 1,
            "ZSliceStrideBytes": extent * extent * 4, "RowPitchBytes": extent * 4,
            "ZSliceOrder": "single slice",
            "NativeFormat": "PF_R32_FLOAT", "ViewFormat": "PF_R32_FLOAT", "ViewFormatMatchesNativeFormat": True,
            "StagingFormat": "PF_R32_FLOAT", "StoredFormat": "PF_R32_FLOAT",
            "NativeFormatExpanded": False, "NativeSRGB": False, "ViewSRGB": False, "SRGBOverride": "Default",
            "SRGBViewUsedForReadback": False, "StoredBytesAreNativeStorage": False,
            "NativeValueInterpretation": "DecodedFloat", "BitExact": True, "ValuePreserving": True,
            "BitExactCondition": "synthetic exact float copy", "MipExtentClampedToMinimum": mip == 1,
            "StencilSubresource": False, "Readback": "ComputeThenCopy",
            "DumpStatus": "ScheduledForReadback", "FileVerifiedAtSchedule": False,
            "ByteSize": extent * extent * 4, "StagingByteSize": extent * extent * 4,
        })
    layouts.append({
        "Resource": tex_bc7, "Name": "Lumen.Synthetic.ConvertedBC7",
        "File": f"Resources/{tex_bc7}.mip0.v{p1}.bin",
        "Dimension": "Texture2D", "MipLevel": 0, "FirstArraySlice": 0, "NumArraySlices": 0,
        "ExtentX": 1, "ExtentY": 1, "Depth": 1, "ZSliceCount": 1,
        "ZSliceStrideBytes": 8, "RowPitchBytes": 8, "ZSliceOrder": "single slice",
        "NativeFormat": "PF_BC7", "ViewFormat": "PF_BC7", "ViewFormatMatchesNativeFormat": True,
        "StagingFormat": "PF_FloatRGBA", "StoredFormat": "PF_FloatRGBA",
        "NativeFormatExpanded": False, "NativeSRGB": False, "ViewSRGB": False, "SRGBOverride": "Default",
        "SRGBViewUsedForReadback": False, "StoredBytesAreNativeStorage": False,
        "NativeValueInterpretation": "DecodedFloatExpanded", "BitExact": False, "ValuePreserving": False,
        "BitExactCondition": "BC7 is decoded and re-encoded as half4; native BC7 bits are not preserved.",
        "MipExtentClampedToMinimum": False, "StencilSubresource": False, "Readback": "ComputeThenCopy",
        "DumpStatus": "ScheduledForReadback", "FileVerifiedAtSchedule": False,
        "ByteSize": 8, "StagingByteSize": 8,
    })
    layouts.append({
        "Resource": tex_no_completion, "Name": "Lumen.Synthetic.NoCompletion",
        "File": f"Resources/{tex_no_completion}.mip0.v{p1}.bin",
        "Dimension": "Texture2D", "MipLevel": 0, "FirstArraySlice": 0, "NumArraySlices": 0,
        "ExtentX": 1, "ExtentY": 1, "Depth": 1, "ZSliceCount": 1,
        "ZSliceStrideBytes": 4, "RowPitchBytes": 4, "ZSliceOrder": "single slice",
        "NativeFormat": "PF_R32_UINT", "ViewFormat": "PF_R32_UINT", "ViewFormatMatchesNativeFormat": True,
        "StagingFormat": "PF_R32_UINT", "StoredFormat": "PF_R32_UINT",
        "NativeFormatExpanded": False, "NativeSRGB": False, "ViewSRGB": False, "SRGBOverride": "Default",
        "SRGBViewUsedForReadback": False, "StoredBytesAreNativeStorage": True,
        "NativeValueInterpretation": "UnsignedIntegerBits", "BitExact": True, "ValuePreserving": True,
        "BitExactCondition": "synthetic exact integer copy", "MipExtentClampedToMinimum": False,
        "StencilSubresource": False, "Readback": "ComputeThenCopy",
        "DumpStatus": "ScheduledForReadback", "FileVerifiedAtSchedule": False,
        "ByteSize": 4, "StagingByteSize": 4,
    })
    append_jsonl(base / "SubresourceLayouts.json", layouts)

    statuses = [{
        "File": layout["File"], "Kind": "Texture" if layout["Dimension"] != "Texture3D" else "Texture3D",
        "ExpectedBytes": layout["ByteSize"], "ActualBytes": layout["ByteSize"],
        "FileExists": True, "FileSizeMatches": True,
        "Status": "Dumped", "Timing": "GPUReadbackCompleted",
        "FutureStatusField": "retained-completion-field",
    } for layout in layouts if layout["Resource"] != tex_no_completion]
    statuses.extend([
        {
            "File": f"Resources/NativeBuffer.{native_buf}.o4.n2.v{p1}.bin",
            "Kind": "Buffer", "ExpectedBytes": 8, "ActualBytes": 8,
            "FileExists": True, "FileSizeMatches": True,
            "Status": "Dumped", "Timing": "GPUReadbackCompleted",
        },
        {
            "File": f"Resources/{skip_buf}.v{'0' * 20}.bin",
            "Kind": "SkipTrackingBuffer", "ExpectedBytes": 4, "ActualBytes": 4,
            "FileExists": True, "FileSizeMatches": True,
            "Status": "Dumped", "Timing": "GPUReadbackCompleted",
        },
    ])
    append_jsonl(base / "DumpReadbackStatus.json", statuses)

    native_binding_base = {
        "CPUCollectionTiming": "GraphBuildParameterTraversal",
        "GPUReadbackOrdering": "InsertedAfterOwnerInRDGQueue",
        "CollectionTiming": "GraphBuildParameterTraversal",
        "OwnerPassExecutionDependencyClaimed": False,
        "SourceStateProvenAtScheduling": False,
        "TimingRisk": "OwnerPassNotExecutedAtCPUCollection;NativeResourceHasNoRDGExecutionDependency",
        "ReadbackCompletionFile": "Base/DumpReadbackStatus.json",
        "ScheduledStatus": "ScheduledForReadback",
        "DumpedStatusSource": "Base/DumpReadbackStatus.json after file verification",
        "ReadOnlyBindingPolicy": "NoTransitionPrivateStagingOutput",
        "MutableBindingPolicy": "NoBytesWhenWritableAliasOrUAVNotObservable",
        "FutureBindingRecordField": {"retained": True},
    }

    def record(pas: str, uniform_buffer: str, layout_name: str, bindings: dict) -> dict:
        item = dict(native_binding_base)
        item.update({"Pass": pas, "UniformBuffer": uniform_buffer, "LayoutName": layout_name, "Bindings": bindings})
        return item

    volume_binding = {
        "BaseType": "UBMT_TEXTURE", "MemberBaseOffset": 0, "MemberOffset": 0,
        "ElementStrideBytes": 8, "ArrayNumElements": 1, "StateCaptured": False,
        "Source": "ReferencedUniformBufferContentMap", "ResourceKind": "Texture",
        "Pointer": "00060000000000000040", "Null": False,
        "NativeTextureName": "Lumen.Synthetic.Volume3D", "NativeDimension": "Texture3D",
        "NativeFormat": "PF_R32_FLOAT", "NativeExtentX": 2, "NativeExtentY": 2,
        "NativeDepth": 2, "NativeArraySize": 1, "NativeNumMips": 1,
        "DumpedAs": f"NativeTexture.00060000000000000040.mip0_1.a0_1.f0_1.dim5.plane0",
        "DiagnosticResource": tex_3d, "NativePointer": "00060000000000000040",
        "NativeBindingPath": "View.View.SyntheticVolume",
        "ExportSubresourceCount": 1,
        "ExportFirstFile": f"Resources/{tex_3d}.mip0.v{p1}.bin",
        "ExportSRGB": False, "ExportUsesWholeNativeResource": True,
        "ExportScope": "WholeNativeResource", "ExportRisk": "BindIsWholeTexture",
        "ExportMipFirst": 0, "ExportMipCount": 1, "ExportArrayFirstInViewUnits": 0,
        "ExportArrayCountInViewUnits": 1, "ExportFaceFirst": 0, "ExportFaceCount": 1,
        "ExportViewFormat": "PF_R32_FLOAT", "ReadStateBasis": "OwnerPassSRVBinding",
        "OwnerPassExecutionDependencyClaimed": False, "SourceStateProvenAtScheduling": False,
        "TimingRisk": "OwnerPassNotExecutedAtCPUCollection;NativeResourceHasNoRDGExecutionDependency",
        "RegistryMutation": False, "FileVerified": False,
        "ReadbackStatusFile": "Base/DumpReadbackStatus.json",
        "CrossUniformBufferAliasObservable": False, "ReusedScheduledReadback": False,
        "DumpStatus": "ScheduledForReadback",
    }
    buffer_binding = {
        "BaseType": "UBMT_SRV", "MemberBaseOffset": 8, "MemberOffset": 8,
        "ElementStrideBytes": 8, "ArrayNumElements": 1, "StateCaptured": False,
        "Source": "ReferencedUniformBufferContentMap", "ResourceKind": "BufferShaderResourceView",
        "Pointer": "00060000000000000041", "Null": False,
        "UnderlyingBuffer": native_buf, "UnderlyingBufferName": "Synthetic.NativeTypedBuffer",
        "NativeBufferByteSize": 16, "BufferViewType": "Typed",
        "ViewOffsetInBytes": 4, "ViewSizeInBytes": 8, "ViewStrideInBytes": 4,
        "ViewNumElements": 2, "ViewFormat": "PF_R32_UINT", "ViewIsNull": False,
        "ExactViewByteRange": True, "ViewFormatBlockBytes": 4, "ViewStrideMatchesFormat": True,
        "DumpedAs": f"NativeBuffer.{native_buf}.o4.n2",
        "DumpFile": f"Resources/NativeBuffer.{native_buf}.o4.n2.v{p1}.bin",
        "ExportByteSize": 8, "ExportFidelity": "ExactBytesThroughRawSRV",
        "ReadStateBasis": "ReadOnlyUniformBufferSRVBinding", "TransitionIssued": False,
        "OwnerPassExecutionDependencyClaimed": False, "SourceStateProvenAtScheduling": False,
        "TimingRisk": "OwnerPassNotExecutedAtCPUCollection;NativeResourceHasNoRDGExecutionDependency",
        "CrossUniformBufferAliasObservable": False, "RawViewOffsetPolicy": "AlignDown16WithShaderPrefix",
        "RawViewAlignedBaseOffset": 0, "RawViewPrefixBytes": 4, "FileVerified": False,
        "ReadbackStatusFile": "Base/DumpReadbackStatus.json", "DumpStatus": "ScheduledForReadback",
    }
    sampler_binding = {
        "BaseType": "UBMT_SAMPLER", "MemberBaseOffset": 16, "MemberOffset": 16,
        "ElementStrideBytes": 8, "ArrayNumElements": 1, "StateCaptured": False,
        "Source": "ReferencedUniformBufferContentMap", "ResourceKind": "Sampler",
        "Pointer": "00060000000000000042", "Null": False, "StaticInitializerCaptured": False,
    }
    append_jsonl(base / "NativeUniformBufferBindings.json", [
        record("00010000000000000006", "00000000000000000106", "FSyntheticInstrumented",
               {"View.View.SyntheticVolume": volume_binding,
                "View.View.SyntheticTypedBuffer": buffer_binding,
                "View.View.SyntheticSampler": sampler_binding}),
        # Second pass reuses the same scheduled volume readback.  Reuse is an
        # explicit link, not a second independent completion.
        record(p1, "00000000000000000101", "FLumenSyntheticDirectLightingCS",
               {"View.View.SyntheticVolume": dict(volume_binding, ReusedScheduledReadback=True,
                                                  DumpStatus="ScheduledByEarlierPass",
                                                  FirstScheduledByPass="00010000000000000006")}),
    ])
    append_jsonl(base / "ParameterBindings.json", [{
        "Pass": p1,
        "Parameters": "00000000000000000101",
        "Bindings": {
            "SyntheticBufferUAV": {
                "BaseType": "UBMT_RDG_BUFFER_UAV", "Offset": 0, "NumElements": 1,
                "ElementStrideBytes": 8, "Null": False,
                "Pointer": "0000000000000000000b", "StateCaptured": False,
                "Source": "PassParameterBlock",
                "FutureParameterBindingField": "retained",
            }
        },
    }])
    append_jsonl(base / "ReferencedStructs.json", [{
        "Pass": "00010000000000000006",
        "ReferencedStructs": {
            "Instrumented.VolumeView": {
                "Content": "00000000000000000303", "Metadata": "00000000000000000207",
                "ContentKind": "RDGUniformBufferParameters", "BaseType": "UBMT_RDG_UNIFORM_BUFFER",
                "MemberOffset": 0, "ElementStrideBytes": 16, "ArrayNumElements": 2,
                "BindingFlags": "Static", "LayoutName": "FSyntheticVolumeView",
                "ByteSize": 16, "Null": False, "ContentAvailable": True,
                "TraversalCycleDetected": False, "CollectedDuringGraphBuildParameterTraversal": True,
                "FutureReferencedStructField": ["retained", 1, 2],
            }
        },
    }])
    _write(root / "Structures" / "00000000000000000303.bin", struct.pack("<4f", 1.5, 2.5, 3.5, 4.5))
    _write(root / "StructuresMetadata" / "00000000000000000207.json", json.dumps({
        "StructTypeName": "FSyntheticVolumeView", "Size": 16,
        "Members": [
            {"Name": "WorldToVolume", "ShaderType": "float4", "Offset": 0,
             "BaseType": "UBMT_FLOAT32", "NumRows": 1, "NumColumns": 1,
             "NumElements": 4, "StructMetadata": ""},
        ],
    }, indent=2).encode())
    return root


def add_viewport_fixture(root: Path) -> Path:
    """Append a synthetic camera pass and domain-specific resources.

    The fixture is still synthetic.  It exercises the viewport applicability
    rule: only a camera pass output whose active mip-0 sidecar bounds match the
    descriptor may be cropped.  A 4096 card atlas, a 3D indirection volume, a
    cube and a virtual-shadow-map array are present as inputs to the same pass
    but must keep their own domain and full subresource.
    """
    base = root / "Base"
    resources = root / "Resources"
    structures = root / "Structures"
    structures_meta = root / "StructuresMetadata"
    p_depth = "00010000000000000110"
    p_base = "00010000000000000111"
    p_light = "00010000000000000112"
    tex_scene = "00010000000000000120"
    tex_gbuf = "00010000000000000121"
    tex_depth = "00010000000000000122"
    tex_card = "00010000000000000123"
    tex_volume = "00010000000000000124"
    tex_cube = "00010000000000000125"
    tex_vsm = "00010000000000000126"
    tex_atlas = "00010000000000000127"
    tex_history = "00010000000000000128"

    def append_jsonl(path: Path, items: list[dict]) -> None:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        path.write_text(existing + _jsonl(items), encoding="utf-8")

    descs = [
        {"Name": "SceneColor", "UniqueResourceName": tex_scene, "ByteSize": 128 * 96 * 4,
         "Desc": "FRDGTextureDesc", "Type": "Texture2D", "Format": "PF_A2B10G10R10",
         "ExtentX": 128, "ExtentY": 96, "Depth": 1, "ArraySize": 1, "NumMips": 1,
         "NumSamples": 1, "Flags": ["RenderTargetable", "ShaderResource"]},
        {"Name": "GBufferA", "UniqueResourceName": tex_gbuf, "ByteSize": 128 * 96 * 4,
         "Desc": "FRDGTextureDesc", "Type": "Texture2D", "Format": "PF_A2B10G10R10",
         "ExtentX": 128, "ExtentY": 96, "Depth": 1, "ArraySize": 1, "NumMips": 1,
         "NumSamples": 1, "Flags": ["RenderTargetable", "ShaderResource"]},
        {"Name": "SceneDepthZ", "UniqueResourceName": tex_depth, "ByteSize": 128 * 96 * 4,
         "Desc": "FRDGTextureDesc", "Type": "Texture2D", "Format": "PF_DepthStencil",
         "ExtentX": 128, "ExtentY": 96, "Depth": 1, "ArraySize": 1, "NumMips": 1,
         "NumSamples": 1, "Flags": ["DepthStencil", "ShaderResource"]},
        {"Name": "Lumen.SceneAlbedo", "UniqueResourceName": tex_card, "ByteSize": 16,
         "Desc": "FRDGTextureDesc", "Type": "Texture2D", "Format": "PF_BC7",
         "ExtentX": 4096, "ExtentY": 4096, "Depth": 1, "ArraySize": 1, "NumMips": 1,
         "NumSamples": 1, "Flags": ["ShaderResource"]},
        {"Name": "Lumen.RadianceCache.RadianceProbeIndirectionTexture",
         "UniqueResourceName": tex_volume, "ByteSize": 8 * 4 * 4 * 4,
         "Desc": "FRDGTextureDesc", "Type": "Texture3D", "Format": "PF_R32_UINT",
         "ExtentX": 8, "ExtentY": 4, "Depth": 4, "ArraySize": 1, "NumMips": 1,
         "NumSamples": 1, "Flags": ["ShaderResource"]},
        {"Name": "CubeBlackDummy", "UniqueResourceName": tex_cube, "ByteSize": 24,
         "Desc": "FRDGTextureDesc", "Type": "TextureCube", "Format": "PF_B8G8R8A8",
         "ExtentX": 1, "ExtentY": 1, "Depth": 1, "ArraySize": 6, "NumMips": 1,
         "NumSamples": 1, "Flags": ["ShaderResource"]},
        {"Name": "Shadow.Virtual.PhysicalPagePool", "UniqueResourceName": tex_vsm, "ByteSize": 512,
         "Desc": "FRDGTextureDesc", "Type": "Texture2DArray", "Format": "PF_R32_UINT",
         "ExtentX": 256, "ExtentY": 64, "Depth": 1, "ArraySize": 2, "NumMips": 1,
         "NumSamples": 1, "Flags": ["ShaderResource"]},
        {"Name": "LightFunction.Atlas", "UniqueResourceName": tex_atlas, "ByteSize": 1024,
         "Desc": "FRDGTextureDesc", "Type": "Texture2D", "Format": "PF_R8",
         "ExtentX": 32, "ExtentY": 32, "Depth": 1, "ArraySize": 1, "NumMips": 1,
         "NumSamples": 1, "Flags": ["RenderTargetable"]},
        {"Name": "StochasticLighting.DepthHistory", "UniqueResourceName": tex_history,
         "ByteSize": 128 * 96 * 4, "Desc": "FRDGTextureDesc", "Type": "Texture2D",
         "Format": "PF_R32_FLOAT", "ExtentX": 128, "ExtentY": 96, "Depth": 1,
         "ArraySize": 1, "NumMips": 1, "NumSamples": 1, "Flags": ["ShaderResource"]},
    ]
    append_jsonl(base / "ResourceDescs.json", descs)

    view_specs = [(p_depth, "view-depth"), (p_base, "view-base"), (p_light, "view-light")]
    passes = [
        {"EventName": "DepthPassParallel", "ParametersName": "FDepthPassParameters",
         "Parameters": "00010000000000000130", "ParametersMetadata": "00010000000000000140",
         "Pointer": p_depth, "Id": 10, "ParentEventScopes": ["Frame 7 (Delta=0.016000s)", "Scene"],
         "InputResources": [tex_volume], "OutputResources": [f"{tex_depth}.mip0"]},
        {"EventName": "BasePassParallel", "ParametersName": "FOpaqueBasePassParameters",
         "Parameters": "00010000000000000131", "ParametersMetadata": "00010000000000000141",
         "Pointer": p_base, "Id": 11, "ParentEventScopes": ["Frame 7 (Delta=0.016000s)", "BasePass", "Scene"],
         "InputResources": [tex_card, tex_volume, tex_cube, tex_vsm, tex_history],
         "OutputResources": [f"{tex_scene}.mip0", f"{tex_gbuf}.mip0", f"{tex_depth}.mip0"]},
        {"EventName": "LightFunctionAtlas Generation", "ParametersName": "FLightFunctionAtlasRenderParameters",
         "Parameters": "00010000000000000132", "ParametersMetadata": "00010000000000000142",
         "Pointer": p_light, "Id": 12, "ParentEventScopes": ["Frame 7 (Delta=0.016000s)", "LightFunctionAtlasGeneration", "Scene"],
         "InputResources": [tex_volume, tex_cube], "OutputResources": [f"{tex_atlas}.mip0"]},
    ]
    # The pass records are written after the View.View parameter pointers below
    # are attached, so each pass's Parameters block is the same captured struct.

    # View.View structs: a captured 128x72 camera ViewRect at member offset 16
    # and StateFrameIndexMod8 at offset 32.  The pass parameter block is the
    # same captured struct so no parameter bytes are fabricated.
    refs = []
    for pointer, stem in view_specs:
        content = "00010000000000000150" if stem == "view-depth" else (
            "00010000000000000151" if stem == "view-base" else "00010000000000000152")
        meta = "00010000000000000160" if stem == "view-depth" else (
            "00010000000000000161" if stem == "view-base" else "00010000000000000162")
        # Keep the pass's Parameters pointer equal to the View.View content so
        # the same captured block is both the parameter block and View state.
        for item in passes:
            if item["Pointer"] == pointer:
                item["Parameters"] = content
                item["ParametersMetadata"] = meta
        data = bytearray(40)
        struct.pack_into("<4I", data, 16, 0, 0, 128, 72)
        struct.pack_into("<I", data, 32, 3)
        struct.pack_into("<I", data, 36, 7)
        _write(structures / f"{content}.bin", bytes(data))
        _write(structures_meta / f"{meta}.json", json.dumps({
            "StructTypeName": "FViewState",
            "ShaderVariableName": "View",
            "FileName": "SyntheticView.ush",
            "FileLine": 1,
            "Size": 40,
            "Members": [
                {"Name": "StateFrameIndex", "ShaderType": "uint", "Offset": 36,
                 "BaseType": "UBMT_UINT32", "Precision": 1, "NumRows": 1,
                 "NumColumns": 1, "NumElements": 0, "StructMetadata": ""},
                {"Name": "StateFrameIndexMod8", "ShaderType": "uint", "Offset": 32,
                 "BaseType": "UBMT_UINT32", "Precision": 1, "NumRows": 1,
                 "NumColumns": 1, "NumElements": 0, "StructMetadata": ""},
                {"Name": "ViewRectMinAndSize", "ShaderType": "", "Offset": 16,
                 "BaseType": "UBMT_UINT32", "Precision": 0, "NumRows": 1,
                 "NumColumns": 4, "NumElements": 0, "StructMetadata": "00000000000000000000"},
            ],
        }, indent=2).encode())
        refs.append({"Pass": pointer, "ReferencedStructs": {"View.View": {
            "Content": content, "Metadata": meta, "ContentKind": "RHIUniformBufferContentMap",
            "BaseType": "UBMT_REFERENCED_STRUCT", "Null": False, "ContentAvailable": True,
            "ByteSize": 40, "ElementStrideBytes": 16, "ArrayNumElements": 1,
        }}})
    append_jsonl(base / "ReferencedStructs.json", refs)
    append_jsonl(base / "Passes.json", passes)

    # Screen target payloads, versioned by the camera pass pointer.  Only these
    # three get a SubresourceLayouts record with mip 0 bounds matching the
    # descriptor; the atlas/volume/cube/VSM/history keep their own domains.
    for unique, pass_pointer in ((tex_scene, p_base), (tex_gbuf, p_base), (tex_depth, p_base)):
        _write(resources / f"{unique}.mip0.v{pass_pointer}.bin", b"\x00" * (128 * 96 * 4))
    for unique, pass_pointer in ((tex_card, p_base), (tex_volume, p_base), (tex_cube, p_base),
                                 (tex_vsm, p_base), (tex_history, p_base)):
        _write(resources / f"{unique}.v{pass_pointer}.bin", b"\x00" * 16)
    _write(resources / f"{tex_atlas}.mip0.v{p_light}.bin", b"\x00" * 1024)

    layouts = []
    for unique, name in ((tex_scene, "SceneColor"), (tex_gbuf, "GBufferA"), (tex_depth, "SceneDepthZ")):
        layouts.append({
            "Resource": unique, "Name": name,
            "File": f"Resources/{unique}.mip0.v{p_base}.bin",
            "Dimension": "Texture2D", "MipLevel": 0, "FirstArraySlice": 0,
            "NumArraySlices": 0, "CubeFace": None,
            "ExtentX": 128, "ExtentY": 96, "Depth": 1, "ZSliceCount": 1,
            "ZSliceStrideBytes": 128 * 96 * 4, "RowPitchBytes": 128 * 4,
            "ZSliceOrder": "single slice",
            "NativeFormat": "PF_A2B10G10R10" if unique != tex_depth else "PF_DepthStencil",
            "ViewFormat": "PF_A2B10G10R10" if unique != tex_depth else "PF_DepthStencil",
            "ViewFormatMatchesNativeFormat": True,
            "StagingFormat": "PF_A2B10G10R10" if unique != tex_depth else "PF_R32_FLOAT",
            "StoredFormat": "PF_A2B10G10R10" if unique != tex_depth else "PF_R32_FLOAT",
            "NativeFormatExpanded": False, "NativeSRGB": False, "ViewSRGB": False,
            "SRGBOverride": "Default", "SRGBViewUsedForReadback": False,
            "StoredBytesAreNativeStorage": True,
            "NativeValueInterpretation": "UnsignedIntegerBits" if unique != tex_depth else "DecodedFloat",
            "BitExact": True, "ValuePreserving": True,
            "BitExactCondition": "synthetic exact copy",
            "MipExtentClampedToMinimum": False, "StencilSubresource": False,
            "Readback": "SynchronousComputePerSlice", "DumpStatus": "ScheduledForReadback",
            "FileVerifiedAtSchedule": False,
            "ByteSize": 128 * 96 * 4, "StagingByteSize": 128 * 96 * 4,
            "FutureViewportLayoutField": {"retained": True},
        })
    append_jsonl(base / "SubresourceLayouts.json", layouts)
    return root


def add_depth_stencil_subresources(root: Path) -> Path:
    """Append a synthetic depth texture with colour and stencil payload versions.

    This exercises the binding rule that a colour/depth subresource must never
    list stencil payloads (and vice versa) and that an explicit version suffix
    selects exactly one payload version.
    """
    base = root / "Base"
    resources = root / "Resources"
    p4 = "00010000000000000004"
    p5 = "00010000000000000005"
    tex_depth = "00010000000000000010"

    def append_jsonl(path: Path, items: list[dict]) -> None:
        existing = path.read_text(encoding="utf-8")
        if existing and not existing.endswith("\n"):
            existing += "\n"
        path.write_text(existing + _jsonl(items), encoding="utf-8")

    append_jsonl(base / "ResourceDescs.json", [{
        "Name": "Lumen.Synthetic.SceneDepthZ",
        "UniqueResourceName": tex_depth,
        "ByteSize": 16,
        "Desc": "FRDGTextureDesc",
        "Type": "Texture2D",
        "Format": "PF_DepthStencil",
        "ExtentX": 2, "ExtentY": 2, "Depth": 1, "ArraySize": 1,
        "NumMips": 1, "NumSamples": 1,
        "Flags": ["DepthStencil", "ShaderResource"],
    }])
    append_jsonl(base / "Passes.json", [
        {
            "EventName": "Lumen.Synthetic.ClearDepthStencil",
            "ParametersName": "FClearDepthStencil",
            "Parameters": "00000000000000000104",
            "ParametersMetadata": "00000000000000000201",
            "Pointer": p4,
            "Id": 3,
            "ParentEventScopes": ["Frame 42 (Delta=0.016000s)"],
            "InputResources": [f"{tex_depth}.mip0.v{p4}"],
            "OutputResources": [f"{tex_depth}.mip0.v{p4}", f"{tex_depth}.stencil.v{p4}"],
        },
        {
            "EventName": "Lumen.Synthetic.DepthPass",
            "ParametersName": "FDepthPass",
            "Parameters": "00000000000000000105",
            "ParametersMetadata": "00000000000000000201",
            "Pointer": p5,
            "Id": 4,
            "ParentEventScopes": ["Frame 42 (Delta=0.016000s)"],
            "InputResources": [f"{tex_depth}.mip0.v{p5}"],
            "OutputResources": [f"{tex_depth}.mip0.v{p5}", f"{tex_depth}.stencil.v{p5}"],
        },
    ])
    append_jsonl(base / "PassDrawCounts.json", [
        {"Pointer": p4, "DrawCount": 1},
        {"Pointer": p5, "DrawCount": 0},
    ])
    # Colour/depth versions are 2x2 R32_FLOAT (16 B); stencil versions are
    # 2x2 PF_R8_UINT (4 B) and must remain independently addressable.
    _write(resources / f"{tex_depth}.mip0.v{p4}.bin", struct.pack("<4f", 0.25, 0.5, 0.75, 1.0))
    _write(resources / f"{tex_depth}.mip0.v{p5}.bin", struct.pack("<4f", 0.1, 0.2, 0.3, 0.4))
    _write(resources / f"{tex_depth}.stencil.v{p4}.bin", b"\x01\x02\x03\x04")
    _write(resources / f"{tex_depth}.stencil.v{p5}.bin", b"\x05\x06\x07\x08")
    # Referenced parameter/block payloads for the two synthetic passes.
    _write(root / "Structures" / "00000000000000000104.bin", b"\x00" * 16)
    _write(root / "Structures" / "00000000000000000105.bin", b"\x00" * 16)
    return root


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Build a labelled synthetic DumpGPU fixture.")
    parser.add_argument("output")
    args = parser.parse_args()
    print(build_fixture(Path(args.output)))
