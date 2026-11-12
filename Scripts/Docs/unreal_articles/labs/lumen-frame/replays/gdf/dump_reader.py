"""Version-aware reader for native DumpGPU structured metadata.

Only inputs of the selected pass are eligible. Repeated resource names across
different frames are never treated as identity. All reads retain file hashes.

This copy is adapted for the world-radiance-cache replay. In addition to the
original parameter traversal it:

* tolerates the legitimate ``{"Null": true}`` ReferencedStructs records that an
  unbound RDG uniform buffer produces, and keeps them as explicit gaps;
* normalises native-binding suffixes such as ``.linear.mip0`` before looking up
  the unique resource descriptor;
* resolves the exact graph version of each input (last preceding writer) and the
  exact SubresourceLayouts record for that file;
* decodes a texture from its recorded ``StoredFormat`` instead of assuming half
  or a particular channel order. This is important because a BC view can be
  exported as float32 and because B8G8R8A8 is stored BGRA in memory.
"""
from pathlib import Path
import hashlib
import json
import re
import struct
import numpy as np

ZERO = "00000000000000000000"
RESOURCE_ID = re.compile(r"^(?P<id>[0-9a-f]{20})")


def json_stream(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    decoder, i = json.JSONDecoder(), 0
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i == len(text):
            break
        item, i = decoder.raw_decode(text, i)
        yield item


def file_evidence(path, root=None):
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return {"path": str(path.relative_to(root)) if root else str(path),
            "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def unique_resource_id(name):
    text = str(name)
    if text.startswith("NativeBuffer."):
        text = text[len("NativeBuffer."):]
    match = RESOURCE_ID.match(text)
    if not match:
        raise ValueError(f"not a native unique resource name: {name}")
    return match.group("id")


class Dump:
    def __init__(self, root, pass_id=None, event="TraceGlobalSDFAndApplyRadianceCache"):
        self.root = Path(root)
        self.passes = list(json_stream(self.root / "Base/Passes.json"))
        self.descriptors = {x["UniqueResourceName"]: x for x in json_stream(self.root / "Base/ResourceDescs.json")}
        self.layouts = list(json_stream(self.root / "Base/SubresourceLayouts.json"))
        self.layouts_by_file = {x["File"]: x for x in self.layouts}
        self.selected = next(p for p in self.passes
                             if p["EventName"] == event
                             and (pass_id is None or p["Id"] == pass_id))
        self.prefix = self.selected["Pointer"][:4]
        self.versions = {}
        for p in self.passes:
            if p["Pointer"] == self.selected["Pointer"]:
                break
            for resource in p["OutputResources"]:
                self.versions[unique_resource_id(resource)] = p["Pointer"]
        self.fields = {}
        self.parameter_evidence = []
        self.resources = {}
        self._read_struct(self.selected["ParametersMetadata"], self.selected["Parameters"])
        self.reference_record = next(p for p in json_stream(self.root / "Base/ReferencedStructs.json")
                                     if p["Pass"] == self.selected["Pointer"])
        refs = self.reference_record["ReferencedStructs"]
        self.references = refs
        # A pass can legitimately reference an unbound RDG uniform buffer. Those
        # records carry {"Null": true} and no Content/Metadata: they are recorded
        # as explicit gaps instead of being dereferenced (which would fail).
        self.null_references = {}
        self.reference_evidence = {}
        for name, value in refs.items():
            if value.get("Null") or "Metadata" not in value or "Content" not in value:
                self.null_references[name] = value
                continue
            self.reference_evidence[name] = self._read_struct(
                value["Metadata"], value["Content"], name)
        self.native_bindings_path = self.root / "Base/NativeUniformBufferBindings.json"
        self._native_bindings = None
        self.parameter_bindings_path = self.root / "Base/ParameterBindings.json"
        self._parameter_bindings = None

    def _read_struct(self, meta_id, data_id, prefix=""):
        meta_path = self.root / "StructuresMetadata" / (meta_id + ".json")
        data_path = self.root / "Structures" / (data_id + ".bin")
        data = data_path.read_bytes()
        for p in (meta_path, data_path):
            self.parameter_evidence.append(file_evidence(p, self.root))
        meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        self._walk(meta_id, data, 0, prefix)
        return {"metadata": meta_id, "content": data_id,
                "structTypeName": meta.get("StructTypeName"),
                "size": len(data),
                "metadataFile": file_evidence(meta_path, self.root),
                "contentFile": file_evidence(data_path, self.root)}

    def _walk(self, meta_id, data, offset, prefix):
        meta = json.loads((self.root / "StructuresMetadata" / (meta_id + ".json")).read_text(encoding="utf-8-sig"))
        for field in meta["Members"]:
            name = (prefix + "." if prefix else "") + field["Name"]
            start = offset + field["Offset"]
            kind = field["BaseType"]
            if kind == "UBMT_INCLUDED_STRUCT":
                self._walk(field["StructMetadata"], data, start, name)
                continue
            scalar = {"UBMT_FLOAT32": "f", "UBMT_INT32": "i", "UBMT_UINT32": "I"}.get(kind)
            if scalar:
                rows, columns = field["NumRows"], field["NumColumns"]
                elements = max(field["NumElements"], 1)
                # Shader arrays are 16-byte aligned. This task uses float4
                # arrays; preserve a correct stride for smaller vector arrays.
                count = rows * columns
                stride = ((4 * count + 15) // 16) * 16 if field["NumElements"] else 4 * count
                values = [list(struct.unpack_from("<" + scalar * count, data, start + j * stride))
                          for j in range(elements)]
                value = values if field["NumElements"] else values[0]
                if not field["NumElements"] and count == 1:
                    value = value[0]
                self.fields[name] = value
            elif start + 8 <= len(data):
                self.fields[name] = {"kind": kind,
                                     "pointer": f"{struct.unpack_from('<Q', data, start)[0]:016x}"}

    def suffix(self, name):
        matches = [(k, v) for k, v in self.fields.items() if k == name or k.endswith("." + name)]
        if len(matches) != 1:
            raise ValueError(f"ambiguous/missing field {name}: {[k for k, v in matches]}")
        return matches[0][1]

    def field(self, *names):
        """Return the unique field whose path ends with one of the given names."""
        for name in names:
            matches = [(k, v) for k, v in self.fields.items()
                       if k == name or k.endswith("." + name)]
            if len(matches) == 1:
                return matches[0][1]
            if len(matches) > 1:
                raise ValueError(f"ambiguous field {name}: {[k for k, v in matches]}")
        raise ValueError(f"missing field(s) {names}")

    def _resource_match(self, name, output=False):
        key = "OutputResources" if output else "InputResources"
        options = []
        for resource in dict.fromkeys(self.selected[key]):
            rid = unique_resource_id(resource)
            descriptor = self.descriptors.get(rid)
            if descriptor is not None and descriptor.get("Name") == name:
                options.append((rid, resource, descriptor))
        if len(options) != 1:
            raise ValueError(f"ambiguous/missing selected-pass resource {name}: {options}")
        return options[0]

    def resource(self, name, output=False):
        rid, resource, descriptor = self._resource_match(name, output)
        suffix = resource[len(rid):]
        version = self.selected["Pointer"] if output else self.versions.get(rid)
        rel = None
        if version is not None:
            rel = f"Resources/{rid}{suffix}.v{version}.bin"
        if rel is None or (rel not in self.layouts_by_file and not (self.root / rel).exists()):
            # Native bindings (e.g. .linear.mip0) are not RDG outputs. Their
            # exported version is recorded in SubresourceLayouts.
            candidates = [l for l in self.layouts
                          if l["Resource"] == rid and l["File"].startswith(f"Resources/{rid}{suffix}.v")]
            if len(candidates) != 1:
                raise ValueError(f"cannot resolve payload for {name}: {candidates}")
            rel = candidates[0]["File"]
        path = self.root / rel
        evidence = {"uniqueResource": rid, "version": version,
                    "descriptor": descriptor, "layoutFile": rel}
        if path.exists():
            evidence.update(file_evidence(path, self.root))
            if rel in self.layouts_by_file:
                evidence["layout"] = self.layouts_by_file[rel]
        else:
            evidence.update({"path": rel, "status": "missing-native-payload"})
        self.resources[name] = evidence
        return path, descriptor, evidence

    def _read_rows(self, path, height, width, channels, dtype, row_pitch, z_stride, depth):
        itemsize = np.dtype(dtype).itemsize
        texel = itemsize * channels
        if row_pitch < width * texel:
            raise ValueError(f"row pitch {row_pitch} smaller than {width}x{texel} texels")
        if depth > 1:
            if z_stride < height * row_pitch:
                raise ValueError(f"z stride {z_stride} smaller than {height}x{row_pitch}")
            if path.stat().st_size != z_stride * depth:
                raise ValueError(f"file size {path.stat().st_size} != z stride {z_stride} x depth {depth}")
        else:
            if path.stat().st_size != z_stride:
                raise ValueError(f"file size {path.stat().st_size} != z stride {z_stride}")
        contiguous = (row_pitch == width * texel and
                      (depth == 1 or z_stride == height * row_pitch))
        if contiguous:
            shape = (height, width, channels) if channels > 1 else (height, width)
            if depth > 1:
                shape = (depth,) + shape
            return np.memmap(path, dtype=dtype, mode="r", shape=shape)
        out = np.empty((depth, height, width, channels) if channels > 1 else (depth, height, width),
                       dtype=dtype)
        raw = np.fromfile(path, dtype=np.uint8)
        for z in range(depth):
            base = z * z_stride
            for y in range(height):
                row = np.frombuffer(raw, dtype=dtype, count=width * channels,
                                    offset=base + y * row_pitch)
                out[z, y] = row.reshape(width, channels) if channels > 1 else row
        return out[0] if depth == 1 else out

    def read_texture(self, name, output=False):
        path, descriptor, evidence = self.resource(name, output)
        layout = evidence.get("layout")
        if not layout:
            raise ValueError(f"{name}: no SubresourceLayouts record")
        return self._decode_layout(path, layout, name)

    def read_texture_file(self, rel, name=None):
        """Decode a texture directly from its SubresourceLayouts file path."""
        layout = self.layouts_by_file.get(rel)
        if layout is None:
            raise ValueError(f"{rel}: no SubresourceLayouts record")
        return self._decode_layout(self.root / rel, layout, name or layout.get("Name"))

    def _decode_layout(self, path, layout, name):
        fmt = layout["StoredFormat"]
        width, height = int(layout["ExtentX"]), int(layout["ExtentY"])
        depth = int(layout.get("Depth", 1))
        row_pitch = int(layout["RowPitchBytes"])
        z_stride = int(layout["ZSliceStrideBytes"])
        srgb = bool(layout.get("ViewSRGB"))
        decoders = {
            "PF_R8": ("u1", 1),
            "PF_R8G8": ("u1", 2),
            "PF_R32_UINT": ("<u4", 1),
            "PF_R32_SINT": ("<i4", 1),
            "PF_R32_FLOAT": ("<f4", 1),
            "PF_R16_UINT": ("<u2", 1),
            "PF_R16_SINT": ("<i2", 1),
            "PF_G16": ("<u2", 1),
            "PF_FloatR11G11B10": ("<u4", 1),
            "PF_B8G8R8A8": ("u1", 4),
            "PF_FloatRGBA": ("<f2", 4),
            "PF_A32B32G32R32F": ("<f4", 4),
        }
        if fmt not in decoders:
            raise ValueError(f"{name}: no exact stored-format decoder for {fmt}")
        dtype, channels = decoders[fmt]
        array = self._read_rows(path, height, width, channels, dtype, row_pitch, z_stride, depth)
        meta = {"name": name, "storedFormat": fmt, "viewSRGB": srgb,
                "nativeFormat": layout.get("NativeFormat"),
                "nativeSRGB": layout.get("NativeSRGB"),
                "storedBytesAreNativeStorage": layout.get("StoredBytesAreNativeStorage"),
                "nativeValueInterpretation": layout.get("NativeValueInterpretation"),
                "valuePreserving": layout.get("ValuePreserving"),
                "extentX": width, "extentY": height, "depth": depth,
                "rowPitchBytes": row_pitch, "zSliceStrideBytes": z_stride,
                "zSliceCount": layout.get("ZSliceCount"),
                "byteSize": layout.get("ByteSize"),
                "file": str(Path(path).relative_to(self.root)),
                "layout": layout}
        return array, meta

    def read_buffer(self, name, output=False):
        path, descriptor, evidence = self.resource(name, output)
        bpe = int(descriptor["BytesPerElement"])
        count = int(descriptor["NumElements"])
        raw = np.fromfile(path, dtype=np.uint8) if path.exists() else np.empty(0, dtype=np.uint8)
        if raw.size != bpe * count:
            raise ValueError(f"{name}: expected {bpe * count} bytes, got {raw.size}")
        return raw, {"name": name, "bytesPerElement": bpe, "numElements": count,
                     "descriptor": descriptor, "resource": evidence}

    def native_bindings(self):
        if self._native_bindings is None:
            self._native_bindings = list(json_stream(self.native_bindings_path))
        return self._native_bindings

    def native_binding(self, binding_path):
        """Find exactly one binding for this pass and path, plus its UB entry."""
        hits = []
        for entry in self.native_bindings():
            if entry.get("Pass") != self.selected["Pointer"]:
                continue
            if binding_path in entry.get("Bindings", {}):
                hits.append((entry, entry["Bindings"][binding_path]))
        if len(hits) != 1:
            raise ValueError(f"native binding {binding_path} not unique for pass: {len(hits)}")
        entry, binding = hits[0]
        return {"uniformBuffer": entry["UniformBuffer"], "layoutName": entry.get("LayoutName"),
                "timingRisk": entry.get("TimingRisk"),
                "sourceStateProvenAtScheduling": entry.get("SourceStateProvenAtScheduling"),
                "binding": binding}

    def parameter_binding_set(self):
        if self._parameter_bindings is None:
            self._parameter_bindings = next(
                p for p in json_stream(self.parameter_bindings_path)
                if p.get("Pass") == self.selected["Pointer"])
        return self._parameter_bindings

    def parameter_binding(self, name):
        bindings = self.parameter_binding_set()["Bindings"]
        if name not in bindings:
            raise ValueError(f"parameter binding {name} missing")
        return bindings[name]

    def readback_status(self, rel):
        hits = [x for x in json_stream(self.root / "Base/DumpReadbackStatus.json")
                if x.get("File") == rel]
        if len(hits) != 1:
            raise ValueError(f"readback status for {rel}: {len(hits)} records")
        return hits[0]
