#!/usr/bin/env python3
"""Package a captured Unreal DumpGPU frame for the static frame explorer.

The packer is intentionally standard-library only.  It:

* retains every native JSON/CSV/TXT file byte-for-byte under ``native/``;
* inventories every native resource, pass, parameter/uniform block and payload;
* splits each raw payload into fixed-size (<= 1 MiB) chunks;
* stores each unique raw chunk once as gzip, keyed by the chunk's SHA-256;
* makes every payload reference an ordered list of chunk offsets/lengths and
  records the original raw length and SHA-256;
* joins an optional source schema by explicit selectors without evaluating any
  schema-provided strings;
* can estimate, verify and reconstruct without modifying the source dump.

No capture is fabricated here.  If metadata or payloads are missing they are
reported as gaps and the package is marked incomplete.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

PACKAGE_FORMAT = "unreal-frame-explorer-package"
PACKAGE_VERSION = 1
CHUNK_SIZE = 1024 * 1024
DEFAULT_GZIP_LEVEL = 6
SAFE_DECODE_TAGS = {
    "none", "raw", "uint", "sint", "unorm", "float", "half", "float16",
    "float32", "r11g11b10", "a2b10g10r10", "a2b10g10r10_unorm",
}
SAFE_FIELD_TYPES = {
    "uint8", "uint16", "uint32", "uint64", "int8", "int16", "int32",
    "sint", "uint", "unorm8", "unorm16", "unorm", "float16", "half",
    "float32", "float", "bool", "float2", "float3", "float4",
}
UNSAFE_DECODE_PATTERN = re.compile(r"(__import__|eval\s*\(|exec\s*\(|system\s*\()", re.I)
# A resource is croppable to a camera ViewRect only when a real camera pass
# (identified by a captured View.View rect AND an output to one of these screen
# targets) produced it.  Allocation size is never used to guess a camera.
CAMERA_SCREEN_TARGET_NAMES = frozenset({
    "SceneColor", "SceneDepthZ", "GBufferA", "GBufferB", "GBufferC", "GBufferD",
})
VIEWPORT_SOURCE = (
    "camera-pass output binding plus active-mip SubresourceLayouts; "
    "maximum allocation area is never used to identify a camera"
)


def is_supported_field_type(ftype: Any) -> bool:
    s = str(ftype or "").strip().lower()
    if s in SAFE_FIELD_TYPES:
        return True
    m = re.fullmatch(r"(unorm|uint|int|float)(\d+)", s)
    if not m:
        return False
    kind, bits = m.group(1), int(m.group(2))
    if kind == "unorm":
        return 1 <= bits <= 32
    if kind in ("uint", "int"):
        return 1 <= bits <= 64
    return bits in {2, 3, 4, 10, 11, 14, 15, 16, 30, 32}
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)],
    *[f"LPT{i}" for i in range(1, 10)],
}


class PackageError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, block_size: int = 1024 * 1024) -> tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    with path.open("rb") as f:
        while True:
            block = f.read(block_size)
            if not block:
                break
            h.update(block)
            total += len(block)
    return h.hexdigest(), total


def _stringify_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k) if k is not None else "_extra": _stringify_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_stringify_keys(v) for v in value]
    return value


def canonical_json_sha(value: Any) -> str:
    data = json.dumps(_stringify_keys(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str).encode("utf-8")
    return sha256_bytes(data)


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def safe_relpath(value: str) -> str:
    """Normalize a relative path and refuse anything that can escape a root."""
    if value is None:
        raise PackageError("Empty path is not allowed.")
    raw = str(value)
    if "\x00" in raw:
        raise PackageError("NUL byte in path.")
    raw = raw.replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise PackageError(f"Absolute path is not allowed: {value!r}")
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise PackageError(f"Path traversal is not allowed: {value!r}")
        if any(ord(ch) < 32 for ch in part):
            raise PackageError(f"Control character in path: {value!r}")
        if ":" in part:
            raise PackageError(f"Drive/ADS separator in path: {value!r}")
        stem = part.split(".")[0].upper()
        if stem in WINDOWS_RESERVED:
            raise PackageError(f"Windows reserved filename in path: {value!r}")
        if part.endswith(" ") or part.endswith("."):
            raise PackageError(f"Windows-incompatible trailing character in path: {value!r}")
        parts.append(part)
    if not parts:
        raise PackageError(f"Path resolves to nothing: {value!r}")
    return "/".join(parts)


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def parse_json_sequence(data: bytes, source: str) -> list[Any]:
    """Parse DumpGPU's append-style JSON files (objects with no array wrapper)."""
    text = data.decode("utf-8-sig")
    decoder = json.JSONDecoder()
    values: list[Any] = []
    index = 0
    length = len(text)
    while True:
        while index < length and text[index] in " \t\r\n,":
            index += 1
        if index >= length:
            break
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            raise PackageError(f"{source}: invalid JSON at character {exc.pos}: {exc.msg}") from exc
        values.append(value)
        index = end
    return values


def load_json_sequence(path: Path, source: str) -> list[Any]:
    return parse_json_sequence(path.read_bytes(), source)


def load_json_object(path: Path, source: str) -> Any:
    values = load_json_sequence(path, source)
    if len(values) != 1:
        raise PackageError(f"{source}: expected one JSON object, found {len(values)}.")
    return values[0]


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig", errors="replace")


def safe_join(root: Path, rel: str) -> Path:
    rel = safe_relpath(rel)
    candidate = (root / rel).resolve()
    if not is_within(candidate, root):
        raise PackageError(f"Path escapes input root: {rel!r}")
    return candidate


def add_gap(gaps: list[dict[str, Any]], kind: str, severity: str, scope: str,
            message: str, source: str | None = None, **extra: Any) -> dict[str, Any]:
    gap = {
        "id": f"gap-{len(gaps) + 1:04d}",
        "kind": kind,
        "severity": severity,
        "scope": scope,
        "message": message,
    }
    if source:
        gap["source"] = source
    gap.update(extra)
    gaps.append(gap)
    return gap


def normalize_offset_basis(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip().lower().replace("_", "-")
    if s in {"texel", "pixel", "texture-texel", "per-texel"}:
        return "texel"
    if s in {"element", "buffer-element", "index", "per-element"}:
        return "element"
    if s in {"block", "uniform", "constant", "parameter-block", "absolute"}:
        return "block"
    return None


def schema_selectors(entry: dict[str, Any]) -> list[dict[str, Any]]:
    raw = entry.get("selectors")
    selectors: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        selectors = [raw]
    elif isinstance(raw, list):
        selectors = [s for s in raw if isinstance(s, dict)]
    if not selectors:
        if entry.get("resourceNameRegex") or entry.get("resourceRegex") or entry.get("passNameRegex") or entry.get("passRegex"):
            selectors = [{
                "resourceNameRegex": entry.get("resourceNameRegex", entry.get("resourceRegex")),
                "passNameRegex": entry.get("passNameRegex", entry.get("passRegex")),
            }]
    cleaned = []
    for sel in selectors:
        cleaned.append({
            "resourceNameRegex": sel.get("resourceNameRegex", sel.get("resourceRegex")),
            "passNameRegex": sel.get("passNameRegex", sel.get("passRegex")),
        })
    return cleaned


def shader_core_tokens(name: Any, strip_stage: bool = True) -> list[str]:
    s = str(name or "")
    s = re.sub(r"\(.*", "", s)
    if strip_stage:
        s = re.sub(r"^F(?=[A-Z])", "", s)
        s = re.sub(r"(CS|PS|VS|GS|MS|AS|RGS)$", "", s)
    tokens = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+", s)
    return [t.lower() for t in tokens if t]


def compile_regex(pattern: Any) -> re.Pattern[str] | None:
    if pattern is None or str(pattern).strip() == "":
        return None
    try:
        return re.compile(str(pattern))
    except re.error:
        return None


def normalize_format_token(value: Any) -> str:
    if value is None:
        return ""
    m = re.search(r"PF_[A-Za-z0-9_]+", str(value))
    return m.group(0) if m else str(value).strip()


def storage_format_text(storage: Any) -> str | None:
    """Return the raw storage.format description without interpreting it."""
    if isinstance(storage, dict):
        fmt = storage.get("format")
        if isinstance(fmt, str) and fmt.strip():
            return fmt
        if isinstance(fmt, list) and fmt:
            return ", ".join(str(v) for v in fmt)
        if fmt is not None:
            return str(fmt)
    if isinstance(storage, str):
        return storage
    return None


def storage_declared_format(storage: Any) -> str | None:
    """Return an explicit, machine-readable PF_* format, or None.

    A descriptive expression such as
    ``Lumen::GetDirectLightingAtlasFormat() (R11G11B10 in this capture)`` is
    source prose, not a packed-layout declaration.  It is preserved separately
    by storage_format_text() and never activates a field offset.
    """
    text = storage_format_text(storage)
    if not text:
        return None
    m = re.search(r"PF_[A-Za-z0-9_]+", text)
    return m.group(0) if m else None


def storage_alternatives(storage: Any) -> list[str]:
    if isinstance(storage, dict) and isinstance(storage.get("formatAlternatives"), list):
        return [str(v) for v in storage["formatAlternatives"]]
    return []


def formats_equivalent(actual: Any, declared: Any) -> bool:
    if actual is None or declared is None:
        return False
    a = normalize_format_token(actual).upper()
    d = normalize_format_token(declared).upper()
    if not a or not d:
        return False
    if a.startswith("PF_"):
        a = a[3:]
    if d.startswith("PF_"):
        d = d[3:]
    aliases = {
        "FLOATRGB": "R11G11B10_FLOAT",
        "FLOATR11G11B10": "R11G11B10_FLOAT",
        "R11G11B10": "R11G11B10_FLOAT",
        "R16G16B16A16_UNORM": "R16G16B16A16_UINT",
    }
    return aliases.get(a, a) == aliases.get(d, d)


def storage_format_match(storage: Any, actual_format: Any) -> bool | None:
    """Whether the captured format proves the declared storage format.

    formatAlternatives are source possibilities, not evidence that this capture
    used them, so they never turn an unproven layout into a match.
    """
    declared = storage_declared_format(storage)
    if actual_format is None:
        return None
    if declared is None:
        return False if storage_format_text(storage) else None
    return formats_equivalent(actual_format, declared)


def storage_format_variants(storage: Any) -> list[dict[str, Any]]:
    if not isinstance(storage, dict):
        return []
    raw = storage.get("formatVariants")
    if not isinstance(raw, list):
        return []
    return [variant for variant in raw if isinstance(variant, dict)]


def active_format_variant(storage: Any, runtime_format: Any) -> dict[str, Any] | None:
    """Select a declared formatVariant only when the runtime stored format matches it.

    storage.formatAlternatives and storage.formatVariants are source-level
    possibilities.  They are not evidence that a captured payload used that
    permutation; the captured payload format decides.
    """
    if not runtime_format:
        return None
    for variant in storage_format_variants(storage):
        if formats_equivalent(runtime_format, variant.get("format")):
            return variant
    return None


def field_applies_to_format(field: Any, runtime_format: Any,
                            declared_format: Any = None) -> bool:
    """Return whether a field may be read from bytes in runtime_format.

    An explicit appliesToFormats list is authoritative.  A field without that
    key belongs to the schema's declared storage format, so it is inactive when
    the captured bytes use a different (non-equivalent) format.  Buffers have no
    pixel format and no declared format, so their explicit offsets still apply.
    """
    if not isinstance(field, dict):
        return False
    raw = field.get("appliesToFormats")
    values = [] if raw is None else (raw if isinstance(raw, list) else [raw])
    values = [value for value in values if value]
    if values:
        if not runtime_format:
            return False
        return any(formats_equivalent(runtime_format, value) for value in values)
    if declared_format:
        if not runtime_format:
            return False
        return formats_equivalent(runtime_format, declared_format)
    return True


def format_variant_fields(variant: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Expose variant components as fields without inventing offsets or decodes."""
    if not isinstance(variant, dict):
        return []
    components = variant.get("components")
    if not isinstance(components, list):
        return []
    defaults = variant.get("sourceRefs") if isinstance(variant.get("sourceRefs"), list) else []
    fields: list[dict[str, Any]] = []
    for component in components:
        if not isinstance(component, dict):
            continue
        field = dict(component)
        field["appliesToFormats"] = [str(variant.get("format"))]
        field["variantFormat"] = str(variant.get("format"))
        field.setdefault("sourceRefs", defaults)
        fields.append(field)
    return fields


def normalize_refs(refs: Any) -> list[Any]:
    if refs is None:
        return []
    if isinstance(refs, list):
        return refs
    return [refs]


def field_support_summary(schema: dict[str, Any], parameter: bool = False) -> dict[str, Any]:
    fields = schema.get("fields") if isinstance(schema.get("fields"), list) else []
    source_equations: list[str] = []
    unsafe_expressions: list[str] = []
    unsupported_type: list[str] = []
    missing_offset: list[str] = []
    for idx, field in enumerate(fields):
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or f"fields[{idx}]")
        decode = field.get("decode")
        if decode is not None:
            decode_text = str(decode)
            if decode_text.strip().lower() not in SAFE_DECODE_TAGS:
                if UNSAFE_DECODE_PATTERN.search(decode_text):
                    unsafe_expressions.append(name)
                else:
                    source_equations.append(name)
        ftype = field.get("type")
        if ftype is not None and not is_supported_field_type(ftype):
            unsupported_type.append(name)
        # Parameter schemas are source-level struct descriptions; their fields
        # intentionally do not carry captured byte offsets.  Their values come
        # from the native `StructuresMetadata` block, not from these offsets.
        if not parameter and field.get("byteOffset") is None and field.get("bitOffset") is None:
            missing_offset.append(name)
    return {
        "fieldCount": len(fields),
        "sourceEquationFields": source_equations,
        "unsafeExpressionFields": unsafe_expressions,
        "unsupportedTypeFields": unsupported_type,
        "missingOffsetFields": missing_offset,
    }


@dataclass
class NativeFile:
    rel: str
    abs_path: Path
    bytes: int
    sha256: str
    kind: str
    package_rel: str | None = None


@dataclass
class Payload:
    id: str
    original_path: str
    abs_path: Path
    role: str
    bytes: int = 0
    sha256: str = ""
    chunks: list[dict[str, Any]] = field(default_factory=list)
    resource_id: str | None = None
    pass_id: str | None = None
    version: str | None = None
    mip: int | None = None
    slice: int | None = None
    stencil: bool = False
    pointer: str | None = None
    native_record_id: str | None = None
    gap: str | None = None
    native_kind: str = "rdg-resource"
    classification: str = "unclassified"
    layout: dict[str, Any] | None = None
    completion: dict[str, Any] | None = None
    identity: dict[str, Any] | None = None
    diagnostic_resource: str | None = None
    face: int | None = None
    subresource: dict[str, Any] | None = None
    decode: dict[str, Any] | None = None


class FramePackage:
    def __init__(self, input_root: Path, output_root: Path | None, adapter: str,
                 schema_path: Path | None, estimate: bool, force: bool,
                 gzip_level: int = DEFAULT_GZIP_LEVEL):
        # Instrumented native sidecars (DumpGPU export extensions).  They are
        # optional: a capture without them keeps the original DumpGPU behaviour.
        self.subresource_layouts: list[dict[str, Any]] = []
        self.readback_status: list[dict[str, Any]] = []
        self.native_uniform_records: list[dict[str, Any]] = []
        self.parameter_binding_records: list[dict[str, Any]] = []
        self.layout_by_file: dict[str, dict[str, Any]] = {}
        self.status_by_file: dict[str, dict[str, Any]] = {}
        self.native_bindings_by_file: dict[str, list[dict[str, Any]]] = {}
        self.native_bindings_by_view: dict[str, list[dict[str, Any]]] = {}
        self.native_uniform_by_pass: dict[str, list[dict[str, Any]]] = {}
        self.parameter_bindings_by_pass: dict[str, list[dict[str, Any]]] = {}
        self.native_resources: dict[str, dict[str, Any]] = {}
        self.layout_by_file_seen: dict[str, bool] = {}
        self.sidecar_integrity: list[dict[str, Any]] = []
        self.input_root = input_root.resolve()
        self.output_root = output_root.resolve() if output_root else None
        self.requested_adapter = adapter
        self.adapter = adapter
        self.schema_path = schema_path
        self.estimate = estimate
        self.force = force
        self.gzip_level = gzip_level
        self.dump_root = self.input_root

        self.native_files: list[NativeFile] = []
        self.native_records: list[dict[str, Any]] = []
        self.records_by_kind: Counter[str] = Counter()
        self.resources: list[dict[str, Any]] = []
        self.passes: list[dict[str, Any]] = []
        self.payloads: list[Payload] = []
        self.payload_by_id: dict[str, Payload] = {}
        self.chunks: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.gaps: list[dict[str, Any]] = []
        self.infos: dict[str, Any] = {}
        self.dump_service: Any = None
        self.cvars: list[dict[str, Any]] = []
        self.status: str | None = None
        self.structures: dict[str, Any] = {}
        self.structure_records: list[dict[str, Any]] = []
        self.draw_counts: dict[str, int] = {}
        self.referenced_structs: list[dict[str, Any]] = []
        self.schema_doc: dict[str, Any] | None = None
        self.schema_status = "missing"
        self.schema_input_path: str | None = None
        self.schema_sha256: str | None = None
        self.schema_bytes: int | None = None
        self.schema_raw: bytes | None = None
        self.schema_resource_matches: dict[str, list[dict[str, Any]]] = {}
        self.schema_pass_matches: dict[str, list[dict[str, Any]]] = {}
        self.fixture = False
        self.raw_payload_bytes = 0
        self.unique_raw_bytes = 0
        self.compressed_bytes = 0
        self.resource_by_unique: dict[str, dict[str, Any]] = {}
        self.pass_by_pointer: dict[str, dict[str, Any]] = {}
        self.camera_view_passes: list[dict[str, Any]] = []
        self.normalized_doc: dict[str, Any] | None = None
        self._desc_objects: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self._pass_objects: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self._payload_specs: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ setup
    def prepare(self) -> None:
        if not self.input_root.exists() or not self.input_root.is_dir():
            raise PackageError(f"Input directory does not exist: {self.input_root}")
        self._detect_dump_root()
        if self.requested_adapter in ("auto", ""):
            if (self.dump_root / "Base" / "Infos.json").exists() or (self.dump_root / "Base" / "Passes.json").exists():
                self.adapter = "dumpgpu"
            elif (self.dump_root / "normalized-frame.json").exists() or (self.dump_root / "frame.json").exists():
                self.adapter = "normalized"
            else:
                raise PackageError("Could not detect an adapter. Expected Base/Infos.json (DumpGPU) or normalized-frame.json.")
        if self.adapter not in ("dumpgpu", "normalized"):
            raise PackageError(f"Unknown adapter: {self.adapter}")
        if self.output_root is not None:
            self._check_output_root()

    def _detect_dump_root(self) -> None:
        if (self.input_root / "Base" / "Infos.json").exists() or (self.input_root / "Base" / "Passes.json").exists():
            self.dump_root = self.input_root
            return
        if (self.input_root / "normalized-frame.json").exists() or (self.input_root / "frame.json").exists():
            self.dump_root = self.input_root
            return
        candidates = []
        for infos in self.input_root.rglob("Base/Infos.json"):
            candidates.append(infos.parent.parent)
        if len(candidates) == 1:
            self.dump_root = candidates[0].resolve()
            return
        self.dump_root = self.input_root

    def _check_output_root(self) -> None:
        out = self.output_root
        assert out is not None
        if out == self.input_root or is_within(out, self.input_root) or is_within(self.input_root, out):
            raise PackageError("Output directory must not be inside the input dump, and the input dump must not be inside the output directory.")
        if out.exists() and any(out.iterdir()):
            if not self.force:
                raise PackageError(f"Output directory is not empty: {out}. Use --force to replace a previous package.")
            shutil.rmtree(out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "chunks").mkdir(exist_ok=True)
        (out / "records" / "resource").mkdir(parents=True, exist_ok=True)
        (out / "records" / "pass").mkdir(parents=True, exist_ok=True)
        (out / "native").mkdir(exist_ok=True)

    # --------------------------------------------------------------- native
    def scan_native(self) -> None:
        """Read metadata, retain every non-bin file, and enumerate bin payloads."""
        root = self.dump_root
        files: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames.sort()
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if path.is_symlink():
                    try:
                        target = path.resolve()
                    except OSError:
                        continue
                    if not is_within(target, root):
                        add_gap(self.gaps, "symlink-outside-root", "warning", "capture",
                                f"Skipped symlink that resolves outside the dump: {path}", str(path.relative_to(root)))
                        continue
                files.append(path)
        for path in files:
            rel = safe_relpath(path.relative_to(root).as_posix())
            if rel.split(".")[-1].lower() == "bin":
                spec = {"rel": rel, "abs": path}
                if self.adapter == "dumpgpu":
                    if rel.startswith("Resources/"):
                        spec.update({"role": "resource"})
                    elif rel.startswith("Structures/"):
                        spec.update({"role": "parameter"})
                    else:
                        spec.update({"role": "other"})
                else:
                    spec.update({"role": "other", "normalized": False})
                self._payload_specs.append(spec)
                continue
            try:
                data = path.read_bytes()
            except OSError as exc:
                add_gap(self.gaps, "native-read-error", "error", "capture",
                        f"Could not read native file: {exc}", rel)
                continue
            sha = sha256_bytes(data)
            kind = self._native_kind(rel)
            nf = NativeFile(rel=rel, abs_path=path, bytes=len(data), sha256=sha, kind=kind,
                            package_rel=f"native/{rel}")
            self.native_files.append(nf)
            self._parse_native_file(nf, data)

    @staticmethod
    def _native_kind(rel: str) -> str:
        low = rel.lower()
        if low.endswith(".json"):
            if low.endswith("resourcedescs.json"):
                return "resource-descriptors"
            if low.endswith("passes.json"):
                return "passes"
            if low.endswith("passdrawcounts.json"):
                return "pass-draw-counts"
            if low.endswith("referencedstructs.json"):
                return "referenced-structs"
            if low.endswith("subresourcelayouts.json"):
                return "subresource-layouts"
            if low.endswith("dumpreadbackstatus.json"):
                return "readback-status"
            if low.endswith("nativeuniformbufferbindings.json"):
                return "native-uniform-bindings"
            if low.endswith("parameterbindings.json"):
                return "parameter-bindings"
            if low.endswith("infos.json"):
                return "infos"
            if low.endswith("dumpservice.json"):
                return "dump-service"
            if low.startswith("structuresmetadata/"):
                return "structure-metadata"
            return "json"
        if low.endswith(".csv"):
            return "csv"
        if low.endswith(".txt"):
            return "text"
        if low.endswith(".log"):
            return "log"
        return "file"

    def _add_native_record(self, kind: str, source: str, index: int, sha: str,
                           value: Any = None, summary: str | None = None,
                           file_sha: str | None = None) -> dict[str, Any]:
        record = {
            "id": f"native-{len(self.native_records) + 1:06d}",
            "kind": kind,
            "sourcePath": source,
            "index": index,
            "sha256": sha,
            "fileSha256": file_sha or sha,
        }
        if summary:
            record["summary"] = summary
        if value is not None:
            record["canonicalType"] = type(value).__name__
        self.native_records.append(record)
        self.records_by_kind[kind] += 1
        return record

    def _parse_native_file(self, nf: NativeFile, data: bytes) -> None:
        rel = nf.rel
        low = rel.lower()
        if low.endswith(".json"):
            try:
                values = parse_json_sequence(data, rel)
            except PackageError as exc:
                self._add_native_record("unparsed-metadata", rel, 0, nf.sha256,
                                        summary=str(exc), file_sha=nf.sha256)
                add_gap(self.gaps, "metadata-parse-error", "error", "capture", str(exc), rel)
                return
            kind = nf.kind
            for index, obj in enumerate(values):
                if not isinstance(obj, dict):
                    self._add_native_record("unknown-json", rel, index, canonical_json_sha(obj),
                                            value=obj, summary=type(obj).__name__, file_sha=nf.sha256)
                    continue
                record = self._add_native_record(self._record_kind(kind), rel, index,
                                                 canonical_json_sha(obj), value=obj,
                                                 summary=self._record_summary(kind, obj),
                                                 file_sha=nf.sha256)
                if kind == "resource-descriptors":
                    self._desc_objects.append((record, obj))
                elif kind == "passes":
                    self._pass_objects.append((record, obj))
                elif kind == "infos":
                    self.infos = obj
                elif kind == "dump-service":
                    self.dump_service = obj
                elif kind == "structure-metadata":
                    pointer = Path(rel).stem
                    self.structures[pointer] = obj
                    self.structure_records.append({"pointer": pointer, "sourcePath": rel,
                                                   "sha256": nf.sha256, "metadata": obj})
                elif kind == "pass-draw-counts":
                    pointer = obj.get("Pointer")
                    # Never coerce an absent count to zero: absent means "not
                    # captured", which is distinct from a captured zero.
                    if pointer is not None and obj.get("DrawCount") is not None:
                        self.draw_counts[str(pointer)] = obj.get("DrawCount")
                elif kind == "referenced-structs":
                    self.referenced_structs.append(obj)
                elif kind == "subresource-layouts":
                    self.subresource_layouts.append(obj)
                elif kind == "readback-status":
                    self.readback_status.append(obj)
                elif kind == "native-uniform-bindings":
                    self.native_uniform_records.append(obj)
                elif kind == "parameter-bindings":
                    self.parameter_binding_records.append(obj)
            return
        if low.endswith("status.txt"):
            self.status = data.decode("utf-8-sig", errors="replace").strip()
            self._add_native_record("status", rel, 0, nf.sha256, summary=self.status, file_sha=nf.sha256)
            return
        if low.endswith(".csv"):
            text = data.decode("utf-8-sig", errors="replace")
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
            header = reader.fieldnames or []
            for index, row in enumerate(rows):
                name = row.get("Name") or row.get(header[0] if header else "") or f"row {index}"
                self.cvars.append(row)
                self._add_native_record("console-variable", rel, index, canonical_json_sha(row),
                                        value=row, summary=str(name), file_sha=nf.sha256)
            if not rows:
                self._add_native_record("csv", rel, 0, nf.sha256, summary="empty CSV", file_sha=nf.sha256)
            return
        self._add_native_record(nf.kind if nf.kind in ("log", "text", "file") else "file",
                                rel, 0, nf.sha256, summary=f"{len(data)} bytes", file_sha=nf.sha256)
        if nf.kind == "log":
            for line_no, line in enumerate(data.decode("utf-8-sig", errors="replace").splitlines(), 1):
                lowline = line.lower()
                if "not dumping" in lowline or "unable to dump" in lowline or "skipped" in lowline and "dump" in lowline:
                    add_gap(self.gaps, "dumpgpu-skipped-resource", "warning", "capture",
                            line.strip(), f"{rel}:{line_no}")

    @staticmethod
    def _record_kind(kind: str) -> str:
        return {
            "resource-descriptors": "resource-descriptor",
            "passes": "pass",
            "pass-draw-counts": "pass-draw-count",
            "referenced-structs": "referenced-structs",
            "subresource-layouts": "subresource-layout",
            "readback-status": "readback-status",
            "native-uniform-bindings": "native-uniform-buffer-bindings",
            "parameter-bindings": "parameter-bindings",
            "structure-metadata": "uniform-structure",
            "infos": "infos",
            "dump-service": "dump-service",
            "json": "unknown-json",
        }.get(kind, "unknown-json")

    @staticmethod
    def _record_summary(kind: str, obj: dict[str, Any]) -> str:
        if kind == "resource-descriptors":
            return str(obj.get("UniqueResourceName") or obj.get("Name") or "(unnamed resource)")
        if kind == "passes":
            return str(obj.get("EventName") or obj.get("Pointer") or "(unnamed pass)")
        if kind == "structure-metadata":
            return str(obj.get("StructTypeName") or "(unnamed structure)")
        if kind == "subresource-layouts":
            return str(obj.get("File") or obj.get("Name") or "(layout)")
        if kind == "readback-status":
            return str(obj.get("File") or "(readback status)")
        if kind == "native-uniform-bindings":
            return str(obj.get("LayoutName") or obj.get("UniformBuffer") or "(native uniform bindings)")
        if kind == "parameter-bindings":
            return str(obj.get("Pass") or "(parameter bindings)")
        if kind == "infos":
            return f"{obj.get('Project', '?')} / {obj.get('RHI', '?')} / frame {obj.get('DumpTime', '?')}"
        return str(obj.get("Pointer") or "(record)")

    # -------------------------------------------------------------- adapter
    def load_adapter(self) -> None:
        if self.adapter == "dumpgpu":
            self._load_dumpgpu()
        else:
            self._load_normalized()

    def _load_dumpgpu(self) -> None:
        if not self.infos:
            add_gap(self.gaps, "infos-missing", "error", "capture",
                    "Base/Infos.json is absent; capture identity cannot be established.",
                    "Base/Infos.json")
        if self.status is None:
            add_gap(self.gaps, "status-missing", "warning", "capture",
                    "Base/Status.txt is absent; dump completion status is unknown.", "Base/Status.txt")
        elif self.status.lower() != "ok":
            add_gap(self.gaps, "dump-not-ok", "error", "capture",
                    f"DumpGPU status is {self.status!r}, not 'ok'.", "Base/Status.txt")
        add_gap(self.gaps, "dump-scope-unknown", "info", "capture",
                "Only records present in the dump are inventoried. DumpGPU pass/resource filters (r.DumpGPU.Root, -unified_filter) and the capture settings can omit data; completeness relative to the live frame cannot be proven from the dump alone.",
                "Base/Infos.json")
        add_gap(self.gaps, "shader-local-state-not-captured", "info", "capture",
                "DumpGPU captures RDG resources and pass parameter blocks. Shader-local registers and intermediate values not exposed as RDG resources are not present and are not synthesized by this explorer.",
                "Base/Passes.json")
        self.convert_fixture_hint()
        self._index_sidecars()
        self._build_resources_from_descs()
        self._build_passes_from_objects()
        self._match_resource_payloads()
        self._associate_parameter_payloads()
        self._associate_referenced_structs()
        self._finish_relationships()
        self._integrate_sidecars()
        self._annotate_scene_metadata()
        self._draw_count_gaps()

    def _load_normalized(self) -> None:
        path = self.dump_root / "normalized-frame.json"
        if not path.exists():
            path = self.dump_root / "frame.json"
        try:
            doc = load_json_object(path, path.name)
        except PackageError as exc:
            raise PackageError(f"Could not read normalized frame manifest: {exc}") from exc
        if not isinstance(doc, dict):
            raise PackageError("Normalized frame manifest must be a JSON object.")
        self.normalized_doc = doc
        self.infos = doc.get("capture", {}) if isinstance(doc.get("capture"), dict) else {}
        self.fixture = bool(doc.get("fixture", False))
        if doc.get("status") is not None:
            self.status = str(doc.get("status"))
        self.structures = doc.get("structures", {}) if isinstance(doc.get("structures"), dict) else {}

        # Native JSON retention: all non-bin files are copied; the manifest itself
        # is retained as well.  Count each normalized record.
        for index, item in enumerate(doc.get("resources", []) if isinstance(doc.get("resources"), list) else []):
            if not isinstance(item, dict):
                continue
            resource = self._normalized_resource(item, index)
            native = item.get("native", item.get("metadata", {}))
            record = self._add_native_record("resource-descriptor", path.name, index, canonical_json_sha(native),
                                             value=native, summary=resource["name"])
            resource["nativeRecordId"] = record["id"]
            self.resources.append(resource)
        for index, item in enumerate(doc.get("passes", []) if isinstance(doc.get("passes"), list) else []):
            if not isinstance(item, dict):
                continue
            pas = self._normalized_pass(item, index)
            native = item.get("native", item.get("metadata", {}))
            record = self._add_native_record("pass", path.name, index, canonical_json_sha(native),
                                             value=native, summary=pas["name"])
            pas["nativeRecordId"] = record["id"]
            self.passes.append(pas)
        for index, item in enumerate(doc.get("payloads", []) if isinstance(doc.get("payloads"), list) else []):
            if not isinstance(item, dict):
                continue
            try:
                rel = safe_relpath(str(item.get("path") or item.get("originalPath") or ""))
            except PackageError as exc:
                raise PackageError(f"Unsafe normalized payload path in {path.name}: {exc}") from exc
            abs_path = safe_join(self.dump_root, rel)
            if not abs_path.exists() or not abs_path.is_file():
                add_gap(self.gaps, "payload-missing", "error", "payload",
                        f"Normalized payload does not exist: {rel}", rel)
                continue
            payload = Payload(
                id=f"payload-{len(self.payloads) + 1:05d}",
                original_path=rel,
                abs_path=abs_path,
                role=str(item.get("role") or "other"),
                resource_id=item.get("resourceId"),
                pass_id=item.get("passId"),
                version=None if item.get("version") is None else str(item.get("version")),
                mip=item.get("mip"),
                slice=item.get("slice"),
                stencil=bool(item.get("stencil", False)),
                pointer=item.get("pointer"),
            )
            self.payloads.append(payload)
            self.payload_by_id[payload.id] = payload
            self._add_native_record("payload", rel, index, item.get("sha256") or sha256_file(abs_path)[0],
                                    value=None, summary=f"{item.get('role', 'other')} payload")
        # Link normalized objects by explicit ids only; never by broad name guesses.
        by_resource_id = {r["id"]: r for r in self.resources}
        by_pass_id = {p["id"]: p for p in self.passes}
        for payload in self.payloads:
            if payload.resource_id and payload.resource_id not in by_resource_id:
                add_gap(self.gaps, "payload-resource-unresolved", "error", "payload",
                        f"Payload {payload.id} names unknown resource {payload.resource_id!r}.",
                        payload.original_path)
            if payload.pass_id and payload.pass_id not in by_pass_id:
                add_gap(self.gaps, "payload-pass-unresolved", "error", "payload",
                        f"Payload {payload.id} names unknown pass {payload.pass_id!r}.",
                        payload.original_path)
        # Parameter metadata/parameter blocks supplied by the normalized manifest.
        for index, item in enumerate(doc.get("parameterBlocks", []) if isinstance(doc.get("parameterBlocks"), list) else []):
            if not isinstance(item, dict):
                continue
            pid = item.get("passId")
            if pid in by_pass_id:
                by_pass_id[pid]["parameterBlockSpec"] = item
        self._finish_relationships()
        self._finish_normalized_payload_links()

    def _normalized_resource(self, item: dict[str, Any], index: int) -> dict[str, Any]:
        native = item.get("native", item.get("metadata", {})) if isinstance(item.get("native", item.get("metadata", {})), dict) else {}
        rid = str(item.get("id") or f"resource-{index + 1:04d}")
        kind = str(item.get("kind") or ("buffer" if native.get("Desc") == "FRDGBufferDesc" else "texture"))
        fmt = item.get("format", native.get("Format"))
        resource = {
            "id": rid,
            "nativeIndex": index,
            "nativeRecordId": None,
            "name": str(item.get("name") or native.get("Name") or rid),
            "uniqueName": str(item.get("uniqueName") or native.get("UniqueResourceName") or rid),
            "kind": kind,
            "format": fmt,
            "bytes": int(item.get("byteSize", native.get("ByteSize", 0)) or 0),
            "extent": item.get("extent") or [native.get("ExtentX"), native.get("ExtentY"), native.get("Depth")],
            "arraySize": int(item.get("arraySize", native.get("ArraySize", 1)) or 1),
            "numMips": int(item.get("numMips", native.get("NumMips", 1)) or 1),
            "numSamples": int(item.get("numSamples", native.get("NumSamples", 1)) or 1),
            "bytesPerElement": item.get("bytesPerElement", native.get("BytesPerElement")),
            "numElements": item.get("numElements", native.get("NumElements")),
            "metadataPointer": item.get("metadataPointer", native.get("Metadata")),
            "native": native,
            "nativePath": None,
            "payloadIds": [],
            "versions": [],
            "producers": [],
            "consumers": [],
            "schemaMatches": [],
            "gaps": [],
            "decode": {},
            "bufferMetadata": None,
        }
        return resource

    def _normalized_pass(self, item: dict[str, Any], index: int) -> dict[str, Any]:
        native = item.get("native", item.get("metadata", {})) if isinstance(item.get("native", item.get("metadata", {})), dict) else {}
        pid = str(item.get("id") or f"pass-{index + 1:04d}")
        inputs = item.get("inputs", native.get("InputResources", [])) or []
        outputs = item.get("outputs", native.get("OutputResources", [])) or []
        return {
            "id": pid,
            "nativeIndex": index,
            "nativeRecordId": None,
            "name": str(item.get("name") or native.get("EventName") or pid),
            "pointer": item.get("pointer", native.get("Pointer")),
            "parametersName": item.get("parametersName", native.get("ParametersName")),
            "parametersPointer": item.get("parametersPointer", native.get("Parameters")),
            "parametersMetadataPointer": item.get("parametersMetadataPointer", native.get("ParametersMetadata")),
            "parentScopes": item.get("parentScopes", native.get("ParentEventScopes", [])) or [],
            "inputsRaw": list(inputs),
            "outputsRaw": list(outputs),
            "inputs": [],
            "outputs": [],
            "drawCount": item.get("drawCount"),
            "drawCountStatus": "captured" if item.get("drawCount") is not None else "not-captured",
            "drawCountSource": "normalized manifest",
            "native": native,
            "parameterBlock": None,
            "parameterBlockSpec": None,
            "referencedStructs": [],
            "schemaMatches": [],
            "gaps": [],
        }

    def convert_fixture_hint(self) -> None:
        context = str(self.infos.get("DumpContext", ""))
        project = str(self.infos.get("Project", ""))
        if "synthetic" in context.lower() or "fixture" in context.lower() or "synthetic" in project.lower():
            self.fixture = True
            add_gap(self.gaps, "synthetic-fixture", "info", "capture",
                    "This package is built from a labelled synthetic test fixture, not a runtime capture.",
                    "Base/Infos.json")

    # ------------------------------------------------------- native sidecars
    def _index_sidecars(self) -> None:
        """Index the instrumented exporter sidecars by their own keys.

        Keys are never inferred from a pointer prefix or a filename when a
        sidecar record states the exact File/Pass/UniformBuffer identity.  A
        duplicate or conflicting record is retained and reported, not silently
        collapsed.
        """
        for record in self.subresource_layouts:
            path = record.get("File")
            if not path:
                add_gap(self.gaps, "subresource-layout-file-missing", "warning", "capture",
                        "SubresourceLayouts record has no File field; it cannot be linked to a payload.",
                        "Base/SubresourceLayouts.json")
                continue
            if path in self.layout_by_file:
                prior = self.layout_by_file[path]
                if canonical_json_sha(prior) != canonical_json_sha(record):
                    add_gap(self.gaps, "subresource-layout-conflict", "warning", "capture",
                            f"Conflicting SubresourceLayouts records for {path}; both are retained and the first is used for addressing.",
                            "Base/SubresourceLayouts.json")
                continue
            self.layout_by_file[path] = record
        for record in self.readback_status:
            path = record.get("File")
            if not path:
                add_gap(self.gaps, "readback-status-file-missing", "warning", "capture",
                        "DumpReadbackStatus record has no File field; it cannot be linked to a payload.",
                        "Base/DumpReadbackStatus.json")
                continue
            if path in self.status_by_file and canonical_json_sha(self.status_by_file[path]) != canonical_json_sha(record):
                add_gap(self.gaps, "readback-status-conflict", "warning", "capture",
                        f"Conflicting DumpReadbackStatus records for {path}; both are retained and the first is used for completion.",
                        "Base/DumpReadbackStatus.json")
                continue
            self.status_by_file[path] = record
        for record in self.native_uniform_records:
            pas = str(record.get("Pass") or "")
            if not pas:
                add_gap(self.gaps, "native-uniform-pass-missing", "warning", "capture",
                        "NativeUniformBufferBindings record has no Pass pointer.", "Base/NativeUniformBufferBindings.json")
                continue
            self.native_uniform_by_pass.setdefault(pas, []).append(record)
            for member_path, entry in (record.get("Bindings") or {}).items():
                if not isinstance(entry, dict):
                    continue
                wrapper = {"record": record, "memberPath": str(member_path), "entry": entry}
                for key in ("DumpFile", "ExportFirstFile"):
                    target = entry.get(key)
                    if target:
                        self.native_bindings_by_file.setdefault(str(target), []).append(wrapper)
                for key in ("Pointer", "UnderlyingBuffer", "DiagnosticResource", "NativePointer"):
                    target = entry.get(key)
                    if target:
                        self.native_bindings_by_view.setdefault(str(target), []).append(wrapper)
        for record in self.parameter_binding_records:
            pas = str(record.get("Pass") or "")
            if not pas:
                add_gap(self.gaps, "parameter-bindings-pass-missing", "warning", "capture",
                        "ParameterBindings record has no Pass pointer.", "Base/ParameterBindings.json")
                continue
            self.parameter_bindings_by_pass.setdefault(pas, []).append(record)

    def _sidecar_completion(self, path: str, layout: dict[str, Any] | None = None) -> dict[str, Any]:
        """Join the completion record to a scheduled readback, without conflating them."""
        status = self.status_by_file.get(path)
        scheduled = layout.get("DumpStatus") if isinstance(layout, dict) else None
        result: dict[str, Any] = {
            "scheduledStatus": scheduled,
            "statusRecord": status,
            "statusSource": "Base/DumpReadbackStatus.json",
            "observed": status is not None,
            "dumped": bool(status and status.get("Status") == "Dumped"),
            "timing": status.get("Timing") if status else None,
            "expectedBytes": status.get("ExpectedBytes") if status else None,
            "actualBytes": status.get("ActualBytes") if status else None,
            "fileExists": status.get("FileExists") if status else None,
            "fileSizeMatches": status.get("FileSizeMatches") if status else None,
            "kind": status.get("Kind") if status else None,
        }
        if status is None:
            result["reason"] = "No DumpReadbackStatus record lists this file; completion is unobserved, and scheduling metadata (if any) is not treated as success."
        elif status.get("Status") != "Dumped":
            result["reason"] = "DumpReadbackStatus lists this file but Status is not Dumped."
        elif status.get("Timing") != "GPUReadbackCompleted":
            result["reason"] = "DumpReadbackStatus does not record GPUReadbackCompleted."
        else:
            result["reason"] = None
        return result

    def _layout_decode(self, layout: dict[str, Any] | None) -> dict[str, Any]:
        """Build a texel decoder description from the recorded stored layout.

        StoredFormat is authoritative.  The native descriptor format is retained
        separately and is never used as the payload stride when the sidecar
        records a different stored format.
        """
        if not isinstance(layout, dict):
            return {"supported": False, "layout": None, "reason": "No SubresourceLayouts record for this payload."}
        stored = layout.get("StoredFormat")
        decode = self._resource_decode("texture", stored, {})
        decode["source"] = "SubresourceLayouts.json StoredFormat"
        decode["nativeFormat"] = layout.get("NativeFormat")
        decode["viewFormat"] = layout.get("ViewFormat")
        decode["storedBytesAreNativeStorage"] = layout.get("StoredBytesAreNativeStorage")
        decode["nativeFormatExpanded"] = layout.get("NativeFormatExpanded")
        decode["nativeValueInterpretation"] = layout.get("NativeValueInterpretation")
        decode["bitExact"] = layout.get("BitExact")
        decode["valuePreserving"] = layout.get("ValuePreserving")
        decode["bitExactCondition"] = layout.get("BitExactCondition")
        decode["srgbViewUsedForReadback"] = layout.get("SRGBViewUsedForReadback")
        if decode.get("supported"):
            expected = int(layout.get("RowPitchBytes") or 0) * int(layout.get("ExtentY") or 0)
            if expected and int(layout.get("ZSliceStrideBytes") or 0) not in (0, expected):
                decode["zSliceStrideNote"] = (
                    f"Recorded ZSliceStrideBytes {layout.get('ZSliceStrideBytes')} differs from RowPitchBytes*ExtentY ({expected}); the sidecar value is authoritative."
                )
        return decode

    def _make_native_resource(self, key: str, value_factory) -> dict[str, Any]:
        if key not in self.native_resources:
            self.native_resources[key] = value_factory()
        return self.native_resources[key]

    def _integrate_sidecars(self) -> None:
        """Attach sidecar facts to payloads, resources and passes.

        Payloads are already linked to resource versions.  This pass adds exact
        per-file layout, completion, native identity and binding provenance
        without changing any native bytes or inventing missing records.
        """
        if not self.layout_by_file and not self.native_uniform_by_pass and not self.parameter_bindings_by_pass:
            return

        # Payload-side enrichment from SubresourceLayouts/DumpReadbackStatus.
        for payload in self.payloads:
            layout = self.layout_by_file.get(payload.original_path)
            if layout is not None:
                payload.native_kind = str(layout.get("Dimension") or "texture")
                payload.classification = "texture-readback-subresource"
                payload.layout = layout
                payload.completion = self._sidecar_completion(payload.original_path, layout)
                payload.mip = int(layout.get("MipLevel") or 0)
                dimension = str(layout.get("Dimension") or "")
                num_array = int(layout.get("NumArraySlices") or 0)
                if dimension in ("Texture2DArray", "TextureCube") or num_array >= 1:
                    payload.slice = int(layout.get("FirstArraySlice") or 0)
                else:
                    payload.slice = None
                if layout.get("CubeFace") is not None:
                    payload.face = int(layout["CubeFace"])
                payload.stencil = bool(layout.get("StencilSubresource"))
                payload.subresource = {
                    "dimension": dimension,
                    "mipLevel": layout.get("MipLevel"),
                    "firstArraySlice": layout.get("FirstArraySlice"),
                    "numArraySlices": layout.get("NumArraySlices"),
                    "cubeFace": layout.get("CubeFace"),
                    "extentX": layout.get("ExtentX"),
                    "extentY": layout.get("ExtentY"),
                    "depth": layout.get("Depth"),
                    "zSliceCount": layout.get("ZSliceCount"),
                    "zSliceStrideBytes": layout.get("ZSliceStrideBytes"),
                    "rowPitchBytes": layout.get("RowPitchBytes"),
                    "storageFormat": layout.get("StoredFormat"),
                    "stencil": bool(layout.get("StencilSubresource")),
                }
                payload.decode = self._layout_decode(layout)
                # Native identity: prefer the exact binding's NativePointer/Name;
                # otherwise retain the sidecar's own Resource/Name identity.
                wrappers = self.native_bindings_by_file.get(payload.original_path, [])
                if wrappers:
                    first = wrappers[0]["entry"]
                    identity = {
                        "source": "Base/NativeUniformBufferBindings.json",
                        "kind": "native-texture",
                        "pointer": first.get("NativePointer") or first.get("Pointer"),
                        "name": first.get("NativeTextureName"),
                        "dimension": first.get("NativeDimension"),
                        "format": first.get("NativeFormat"),
                        "extentX": first.get("NativeExtentX"),
                        "extentY": first.get("NativeExtentY"),
                        "depth": first.get("NativeDepth"),
                        "arraySize": first.get("NativeArraySize"),
                        "numMips": first.get("NativeNumMips"),
                        "diagnosticResource": first.get("DiagnosticResource"),
                        "exportViewFormat": first.get("ExportViewFormat"),
                    }
                    payload.identity = identity
                    payload.diagnostic_resource = str(first.get("DiagnosticResource") or "")
                    key = f"texture:{identity.get('pointer') or payload.original_path}"
                    native = self._make_native_resource(key, lambda: {
                        "id": f"native-texture-{len([r for r in self.native_resources.values() if r.get('kind') == 'native-texture']) + 1:04d}",
                        "kind": "native-texture",
                        "pointer": identity.get("pointer"),
                        "name": identity.get("name"),
                        "dimension": identity.get("dimension"),
                        "format": identity.get("format"),
                        "extent": [identity.get("extentX"), identity.get("extentY"), identity.get("depth")],
                        "arraySize": identity.get("arraySize"),
                        "numMips": identity.get("numMips"),
                        "diagnosticResources": [],
                        "exports": [],
                        "boundPassIds": [],
                        "timingRisks": [],
                        "source": "Base/NativeUniformBufferBindings.json",
                    })
                    if identity.get("diagnosticResource") and identity["diagnosticResource"] not in native["diagnosticResources"]:
                        native["diagnosticResources"].append(identity["diagnosticResource"])
                    native["exports"].append({
                        "file": payload.original_path,
                        "payloadId": payload.id,
                        "layout": layout,
                        "completion": payload.completion,
                        "payload": self.payload_public(payload),
                    })
                else:
                    payload.identity = {
                        "source": "Base/SubresourceLayouts.json",
                        "kind": "rdg-texture",
                        "pointer": layout.get("Resource"),
                        "name": layout.get("Name"),
                        "diagnosticResource": layout.get("Resource"),
                    }

            status = self.status_by_file.get(payload.original_path)
            if status is not None and payload.completion is None:
                payload.completion = self._sidecar_completion(payload.original_path, layout)
                payload.native_kind = str(status.get("Kind") or payload.native_kind)
                if status.get("Kind") == "SkipTrackingBuffer":
                    payload.classification = "skip-tracking-buffer"

            # Native RHI buffer view subranges are identified by the exact
            # NUBB entry that produced the file, not by parsing the filename.
            wrappers = self.native_bindings_by_file.get(payload.original_path, [])
            if payload.original_path.startswith("Resources/NativeBuffer.") or any(
                    w["entry"].get("BaseType") in ("UBMT_SRV", "UBMT_UAV") for w in wrappers):
                entry = wrappers[0]["entry"] if wrappers else {}
                buffer_ptr = str(entry.get("UnderlyingBuffer") or payload.original_path.split(".")[1])
                identity = {
                    "source": "Base/NativeUniformBufferBindings.json" if wrappers else "filename",
                    "kind": "native-buffer",
                    "pointer": buffer_ptr,
                    "name": entry.get("UnderlyingBufferName"),
                    "byteSize": entry.get("NativeBufferByteSize"),
                    "viewOffsetInBytes": entry.get("ViewOffsetInBytes"),
                    "viewSizeInBytes": entry.get("ViewSizeInBytes"),
                    "viewStrideInBytes": entry.get("ViewStrideInBytes"),
                    "viewNumElements": entry.get("ViewNumElements"),
                    "viewFormat": entry.get("ViewFormat"),
                    "bufferViewType": entry.get("BufferViewType"),
                }
                payload.identity = identity
                payload.native_kind = "native-buffer"
                payload.classification = "native-buffer-srv-subrange"
                payload.completion = payload.completion or self._sidecar_completion(payload.original_path, None)
                if payload.bytes and entry.get("ViewSizeInBytes") is not None:
                    if int(entry["ViewSizeInBytes"]) != int(payload.bytes):
                        add_gap(self.gaps, "native-buffer-view-size-mismatch", "error", "payload",
                                f"NUBB ViewSizeInBytes {entry['ViewSizeInBytes']} differs from captured payload bytes {payload.bytes}; raw bytes remain authoritative.",
                                payload.original_path)
                key = f"buffer:{buffer_ptr}"
                native = self._make_native_resource(key, lambda: {
                    "id": f"native-buffer-{len([r for r in self.native_resources.values() if r.get('kind') == 'native-buffer']) + 1:04d}",
                    "kind": "native-buffer",
                    "pointer": buffer_ptr,
                    "name": entry.get("UnderlyingBufferName"),
                    "byteSize": entry.get("NativeBufferByteSize"),
                    "views": [],
                    "boundPassIds": [],
                    "timingRisks": [],
                    "source": "Base/NativeUniformBufferBindings.json",
                })
                if not native.get("name") and entry.get("UnderlyingBufferName"):
                    native["name"] = entry.get("UnderlyingBufferName")
                if native.get("byteSize") is None and entry.get("NativeBufferByteSize") is not None:
                    native["byteSize"] = entry.get("NativeBufferByteSize")
                native["views"].append({
                    "file": payload.original_path,
                    "payloadId": payload.id,
                    "pass": wrappers[0]["record"].get("Pass") if wrappers else None,
                    "bindingPath": wrappers[0]["memberPath"] if wrappers else None,
                    "view": {k: entry.get(k) for k in (
                        "BaseType", "BufferViewType", "ViewOffsetInBytes", "ViewSizeInBytes",
                        "ViewStrideInBytes", "ViewNumElements", "ViewFormat", "DumpedAs",
                        "DumpFile", "ExportByteSize", "ExportFidelity", "ReadStateBasis",
                        "DumpStatus", "TimingRisk", "OwnerPassExecutionDependencyClaimed",
                        "SourceStateProvenAtScheduling", "CrossUniformBufferAliasObservable",
                    ) if k in entry},
                    "completion": payload.completion,
                    "payload": self.payload_public(payload),
                })

            if payload.role == "resource" and payload.layout is None and payload.original_path.startswith("Resources/") \
                    and not payload.original_path.startswith("Resources/NativeBuffer.") and payload.completion is None:
                payload.classification = "rdg-buffer-version"
                payload.native_kind = "rdg-buffer"
                resource = next((r for r in self.resources if r["id"] == payload.resource_id), None)
                payload.identity = {
                    "source": "Base/ResourceDescs.json",
                    "kind": "rdg-buffer",
                    "pointer": resource["uniqueName"] if resource else None,
                    "name": resource["name"] if resource else None,
                    "byteSize": resource.get("bytes") if resource else None,
                }
                payload.completion = {
                    "scheduledStatus": None,
                    "statusRecord": None,
                    "statusSource": "Base/DumpReadbackStatus.json",
                    "observed": False,
                    "dumped": None,
                    "timing": None,
                    "expectedBytes": None,
                    "actualBytes": payload.bytes or None,
                    "fileExists": True,
                    "fileSizeMatches": None,
                    "kind": "RDGBufferVersion",
                    "reason": "No DumpReadbackStatus record lists this RDG buffer version file. Its bytes are present and hashed, but completion and owner-time value identity are unobserved.",
                }

        # Attach layouts, completion and native bindings to resource records.
        for resource in self.resources:
            payloads = [self.payload_by_id[pid] for pid in resource.get("payloadIds", []) if pid in self.payload_by_id]
            layouts = []
            for payload in payloads:
                if payload.layout is None:
                    continue
                layouts.append({
                    "payloadId": payload.id,
                    "file": payload.original_path,
                    "layout": payload.layout,
                    "completion": payload.completion,
                })
            resource["subresourceLayouts"] = layouts
            resource["hasSubresourceLayouts"] = bool(layouts)
            resource["completion"] = [
                {
                    "payloadId": p.id,
                    "file": p.original_path,
                    "completion": p.completion,
                }
                for p in payloads if p.completion is not None
            ]
            bindings = []
            for payload in payloads:
                for wrapper in self.native_bindings_by_file.get(payload.original_path, []):
                    record = wrapper["record"]
                    entry = wrapper["entry"]
                    bindings.append({
                        "payloadId": payload.id,
                        "file": payload.original_path,
                        "passPointer": record.get("Pass"),
                        "passId": self.pass_by_pointer.get(str(record.get("Pass")), {}).get("id"),
                        "uniformBuffer": record.get("UniformBuffer"),
                        "layoutName": record.get("LayoutName"),
                        "memberPath": wrapper["memberPath"],
                        "entry": entry,
                        "recordTiming": {
                            "ownerPassExecutionDependencyClaimed": record.get("OwnerPassExecutionDependencyClaimed"),
                            "sourceStateProvenAtScheduling": record.get("SourceStateProvenAtScheduling"),
                            "timingRisk": record.get("TimingRisk"),
                            "collectionTiming": record.get("CollectionTiming"),
                            "readOnlyBindingPolicy": record.get("ReadOnlyBindingPolicy"),
                            "mutableBindingPolicy": record.get("MutableBindingPolicy"),
                        },
                        "completion": self._sidecar_completion(payload.original_path, payload.layout),
                    })
            resource["nativeUniformBindings"] = bindings
            if resource.get("kind") == "texture":
                missing = [p for p in payloads if p.layout is None]
                if payloads and missing:
                    add_gap(self.gaps, "texture-payload-layout-missing", "warning", "decoding",
                            f"{len(missing)} texture payload(s) have no SubresourceLayouts record. Their stored layout is not assumed; raw bytes remain available.",
                            resource.get("nativePath"), resourceId=resource["id"])
                scheduled = [item for item in layouts if (item.get("layout") or {}).get("DumpStatus") == "ScheduledForReadback"]
                completed = [item for item in scheduled if (item.get("completion") or {}).get("dumped")]
                if scheduled and len(completed) != len(scheduled):
                    add_gap(self.gaps, "readback-scheduled-not-completed", "error", "decoding",
                            f"{len(scheduled) - len(completed)} of {len(scheduled)} scheduled subresource readback(s) lack a Dumped completion record.",
                            resource.get("nativePath"), resourceId=resource["id"])

        # Attach the full native uniform and parameter binding records to passes.
        nubb_id_by_sha = {
            record["sha256"]: record["id"] for record in self.native_records
            if record.get("kind") == "native-uniform-buffer-bindings"
        }
        payload_by_path = {payload.original_path: payload for payload in self.payloads}
        for pas in self.passes:
            pointer = str(pas.get("pointer") or "")
            nubb_records = self.native_uniform_by_pass.get(pointer, [])
            enriched_native = []
            for record in nubb_records:
                bindings = {}
                for member_path, entry in (record.get("Bindings") or {}).items():
                    if not isinstance(entry, dict):
                        bindings[member_path] = entry
                        continue
                    copy = dict(entry)
                    completion = None
                    payload_ids = []
                    for key in ("DumpFile", "ExportFirstFile"):
                        target = entry.get(key)
                        if target and target in self.status_by_file:
                            completion = self._sidecar_completion(str(target), self.layout_by_file.get(str(target)))
                        if target:
                            target_payload = payload_by_path.get(str(target))
                            if target_payload is not None:
                                payload_ids.append(target_payload.id)
                                if completion is None and target_payload.completion is not None:
                                    completion = target_payload.completion
                    copy["completion"] = completion
                    copy["payloadIds"] = sorted(set(payload_ids))
                    bindings[member_path] = copy
                uniform_payload = next((p for p in self.payloads if p.role == "parameter" and p.pointer == str(record.get("UniformBuffer"))), None)
                enriched_native.append({
                    "nativeRecordId": nubb_id_by_sha.get(canonical_json_sha(record)),
                    "raw": record,
                    "pass": record.get("Pass"),
                    "uniformBuffer": record.get("UniformBuffer"),
                    "layoutName": record.get("LayoutName"),
                    "cpuCollectionTiming": record.get("CPUCollectionTiming"),
                    "gpuReadbackOrdering": record.get("GPUReadbackOrdering"),
                    "collectionTiming": record.get("CollectionTiming"),
                    "ownerPassExecutionDependencyClaimed": record.get("OwnerPassExecutionDependencyClaimed"),
                    "sourceStateProvenAtScheduling": record.get("SourceStateProvenAtScheduling"),
                    "timingRisk": record.get("TimingRisk"),
                    "readOnlyBindingPolicy": record.get("ReadOnlyBindingPolicy"),
                    "mutableBindingPolicy": record.get("MutableBindingPolicy"),
                    "readbackCompletionFile": record.get("ReadbackCompletionFile"),
                    "scheduledStatus": record.get("ScheduledStatus"),
                    "dumpedStatusSource": record.get("DumpedStatusSource"),
                    "uniformBufferPayload": self.payload_public(uniform_payload) if uniform_payload else None,
                    "bindings": bindings,
                })
                if uniform_payload is None:
                    add_gap(self.gaps, "native-uniform-bytes-missing", "warning", "pass",
                            f"Native uniform buffer {record.get('UniformBuffer')} has no captured Structures/<pointer>.bin; binding descriptors are retained without bytes.",
                            "Base/NativeUniformBufferBindings.json", passId=pas["id"])
                if record.get("OwnerPassExecutionDependencyClaimed") is False:
                    add_gap(self.gaps, "owner-pass-execution-not-claimed", "info", "timing",
                            "Native uniform bindings were collected during graph-build CPU traversal, not after owner-pass execution. File/hash completion is not evidence of owner-time value identity.",
                            "Base/NativeUniformBufferBindings.json", passId=pas["id"])
            pas["nativeUniformBuffers"] = enriched_native
            pas["parameterBindings"] = self.parameter_bindings_by_pass.get(pointer, [])

        # Resource side of native bindings: link DiagnosticResource to RDG descriptors.
        for resource in self.resources:
            direct = []
            for pointer in (resource["uniqueName"], resource.get("nativePointer")):
                if pointer and pointer in self.native_bindings_by_view:
                    direct.extend(self.native_bindings_by_view[pointer])
            pas_seen = {(b.get("passPointer"), b.get("memberPath")) for b in resource.get("nativeUniformBindings", [])}
            for wrapper in direct:
                record = wrapper["record"]
                key = (record.get("Pass"), wrapper["memberPath"])
                if key in pas_seen:
                    continue
                pas_seen.add(key)
                entry = wrapper["entry"]
                resource.setdefault("nativeUniformBindings", []).append({
                    "payloadId": None,
                    "file": None,
                    "passPointer": record.get("Pass"),
                    "passId": self.pass_by_pointer.get(str(record.get("Pass")), {}).get("id"),
                    "uniformBuffer": record.get("UniformBuffer"),
                    "layoutName": record.get("LayoutName"),
                    "memberPath": wrapper["memberPath"],
                    "entry": entry,
                    "recordTiming": {
                        "ownerPassExecutionDependencyClaimed": record.get("OwnerPassExecutionDependencyClaimed"),
                        "sourceStateProvenAtScheduling": record.get("SourceStateProvenAtScheduling"),
                        "timingRisk": record.get("TimingRisk"),
                        "collectionTiming": record.get("CollectionTiming"),
                        "readOnlyBindingPolicy": record.get("ReadOnlyBindingPolicy"),
                        "mutableBindingPolicy": record.get("MutableBindingPolicy"),
                    },
                    "completion": self._sidecar_completion(str(entry.get("ExportFirstFile") or entry.get("DumpFile") or ""), None),
                })

    def _native_inputs_document(self) -> dict[str, Any]:
        # The document is serialized after payload ingest.  Refresh embedded
        # payload metadata so chunks/hashes/bytes reflect the final capture,
        # not the pre-ingest placeholder created while indexing sidecars.
        for resource in self.native_resources.values():
            for key in ("views", "exports"):
                for item in resource.get(key, []) or []:
                    pid = item.get("payloadId")
                    if pid and pid in self.payload_by_id:
                        item["payload"] = self.payload_public(self.payload_by_id[pid])
        items = sorted(self.native_resources.values(), key=lambda r: (r.get("kind", ""), r.get("id", "")))
        return {
            "format": f"{PACKAGE_FORMAT}-native-inputs",
            "version": PACKAGE_VERSION,
            "count": len(items),
            "items": items,
        }


    # ------------------------------------------------------------- resources
    def _build_resources_from_descs(self) -> None:
        seen: dict[str, dict[str, Any]] = {}
        for index, (record, obj) in enumerate(self._desc_objects):
            unique = str(obj.get("UniqueResourceName") or obj.get("Name") or f"resource-{index + 1}")
            if unique in seen:
                add_gap(self.gaps, "duplicate-resource-descriptor", "warning", "resource",
                        f"Duplicate native resource descriptor for {unique}; all native records are retained but the first descriptor is used for normalization.",
                        record["sourcePath"], nativeRecordId=record["id"])
                continue
            desc = str(obj.get("Desc") or "")
            kind = "texture" if desc == "FRDGTextureDesc" else "buffer" if desc == "FRDGBufferDesc" else "unknown"
            fmt = obj.get("Format")
            decode = self._resource_decode(kind, fmt, obj)
            resource = {
                "id": f"resource-{len(self.resources) + 1:04d}",
                "nativeIndex": index,
                "nativeRecordId": record["id"],
                "name": str(obj.get("Name") or unique),
                "uniqueName": unique,
                "kind": kind,
                "format": fmt,
                "bytes": int(obj.get("ByteSize", 0) or 0),
                "extent": [obj.get("ExtentX"), obj.get("ExtentY"), obj.get("Depth")],
                "arraySize": int(obj.get("ArraySize", 1) or 1),
                "numMips": int(obj.get("NumMips", 1) or 1),
                "numSamples": int(obj.get("NumSamples", 1) or 1),
                "bytesPerElement": obj.get("BytesPerElement"),
                "numElements": obj.get("NumElements"),
                "metadataPointer": obj.get("Metadata"),
                "native": obj,
                "nativePath": record["sourcePath"],
                "payloadIds": [],
                "versions": [],
                "producers": [],
                "consumers": [],
                "schemaMatches": [],
                "gaps": [],
                "decode": decode,
                "bufferMetadata": self.structures.get(str(obj.get("Metadata"))) if kind == "buffer" and obj.get("Metadata") else None,
            }
            self.resources.append(resource)
            self.resource_by_unique[unique] = resource
            seen[unique] = resource
            if kind == "unknown":
                add_gap(self.gaps, "unknown-resource-kind", "warning", "resource",
                        f"Unrecognized resource descriptor type {desc!r}; raw metadata retained.",
                        record["sourcePath"], resourceId=resource["id"])
            if kind == "texture" and not decode.get("supported", False):
                add_gap(self.gaps, "unsupported-pixel-format", "warning", "decoding",
                        f"Resource {resource['name']} format {fmt!r} has no explicit decoder. Raw bytes and native metadata are retained.",
                        record["sourcePath"], resourceId=resource["id"])
            if kind == "buffer" and not resource["bufferMetadata"]:
                add_gap(self.gaps, "buffer-metadata-missing", "info", "decoding",
                        f"Buffer {resource['name']} has no captured structure metadata; typed buffer fields require a source schema or remain raw.",
                        record["sourcePath"], resourceId=resource["id"])

    def _resource_decode(self, kind: str, fmt: Any, obj: dict[str, Any]) -> dict[str, Any]:
        if kind == "buffer":
            return {
                "supported": False,
                "layout": "buffer-element",
                "reason": "Buffers have no pixel format. Typed values require a captured structure metadata pointer or a source schema field with an explicit offset basis.",
                "bytesPerElement": obj.get("BytesPerElement"),
                "numElements": obj.get("NumElements"),
            }
        if kind != "texture":
            return {"supported": False, "layout": None, "reason": "Unknown resource kind."}
        from_site_codec = None  # Python packer deliberately keeps its own small table.
        f = str(fmt or "").upper()
        if f.startswith("PF_"):
            f = f[3:]
        table = {
            "R32_FLOAT": ("FLOAT32", 4), "R32_UINT": ("UINT32", 4), "R32_SINT": ("SINT32", 4),
            "A32B32G32R32F": ("FLOAT32", 16), "R32G32B32A32_FLOAT": ("FLOAT32", 16),
            "FLOATRGBA": ("FLOAT16", 8), "R16G16B16A16_FLOAT": ("FLOAT16", 8),
            "R16F": ("FLOAT16", 2), "R16F_FILTER": ("FLOAT16", 2),
            "G16R16F": ("FLOAT16", 4), "G16R16F_FILTER": ("FLOAT16", 4),
            "B8G8R8A8": ("UNORM8", 4), "R8G8B8A8": ("UNORM8", 4),
            "R8": ("UNORM8", 1), "G8": ("UNORM8", 1), "A8": ("UNORM8", 1), "L8": ("UNORM8", 1),
            "R8G8": ("UNORM8", 2),
            "G16": ("UNORM16", 2), "G16R16": ("UNORM16", 4), "A16B16G16R16": ("UNORM16", 8),
            "R16G16B16A16_UNORM": ("UNORM16", 8),
            "R8_UINT": ("UINT8", 1), "R8_SINT": ("SINT8", 1), "R8G8_UINT": ("UINT8", 2),
            "R8G8B8A8_UINT": ("UINT8", 4), "R16_UINT": ("UINT16", 2), "R16_SINT": ("SINT16", 2),
            "R16G16_UINT": ("UINT16", 4), "R32G32_UINT": ("UINT32", 8),
            "R32G32B32_UINT": ("UINT32", 12), "R32G32B32A32_UINT": ("UINT32", 16),
            "R32G32B32A32_SINT": ("SINT32", 16),
            "A2B10G10R10": ("A2B10G10R10_UNORM", 4), "A2B10G10R10_UNORM": ("A2B10G10R10_UNORM", 4),
            "A2B10G10R10_UINT": ("A2B10G10R10_UINT", 4),
            "FLOATR11G11B10": ("R11G11B10_FLOAT", 4), "R11G11B10_FLOAT": ("R11G11B10_FLOAT", 4),
            "R11G11B10": ("R11G11B10_FLOAT", 4), "FLOATRGB": ("R11G11B10_FLOAT", 4),
        }
        # DumpGPU copies these formats through its uncompressing/preprocessing
        # path; the descriptor retains the original format while the .bin holds
        # the postprocessed format.  Verified in DumpGPU.cpp:851-960.
        dump_format = f
        if f in ("BC6H", "BC7"):
            dump_format = "FLOATRGBA"
        elif f == "BC5":
            dump_format = "G16R16F"
        elif f == "BC4":
            dump_format = "R32_FLOAT"
        elif f in ("DEPTHSTENCIL", "SHADOWDEPTH", "D24"):
            dump_format = "R32_FLOAT"
        elif f == "X24_G8":
            dump_format = "R8_UINT"
        entry = table.get(dump_format)
        if entry:
            return {
                "supported": True,
                "layout": entry[0],
                "bytesPerTexel": entry[1],
                "formatNormalized": f,
                "dumpFormat": f"PF_{dump_format}",
                "convertedByDumpGpu": dump_format != f,
            }
        if f == "R16G16B16A16_UINT":
            return {
                "supported": False, "layout": None,
                "reason": "DumpGPU emits PF_R16G16B16A16_UINT for both the UE UNORM and UINT enum names; numeric interpretation is ambiguous without source metadata.",
                "formatNormalized": f,
            }
        return {
            "supported": False, "layout": None,
            "reason": f"No explicit decoder for format {fmt!r}; raw bytes and native metadata are retained.",
            "formatNormalized": f,
        }

    def _match_resource_payloads(self) -> None:
        if not self.resource_by_unique:
            if self._payload_specs:
                add_gap(self.gaps, "resource-descriptors-missing", "error", "capture",
                        "Binary payload files exist but Base/ResourceDescs.json is missing or unparsable.",
                        "Base/ResourceDescs.json")
        unique_names = sorted(self.resource_by_unique, key=len, reverse=True)
        for spec in self._payload_specs:
            if spec.get("role") != "resource":
                continue
            rel = spec["rel"]
            payload = self._make_resource_payload(spec, unique_names)
            self.payloads.append(payload)
            self.payload_by_id[payload.id] = payload

    def _make_resource_payload(self, spec: dict[str, Any], unique_names: list[str]) -> Payload:
        rel = spec["rel"]
        path = spec["abs"]
        name = rel[len("Resources/"):] if rel.startswith("Resources/") else rel
        stem = name[:-4] if name.lower().endswith(".bin") else name
        version = None
        m = re.search(r"\.v([0-9a-fA-F]+)$", stem)
        if m:
            version = m.group(1)
            stem = stem[:m.start()]
        else:
            add_gap(self.gaps, "payload-version-missing", "warning", "payload",
                    f"Resource payload filename has no .v<version> suffix: {rel}", rel)
        payload = Payload(id=f"payload-{len(self.payloads) + 1:05d}", original_path=rel,
                          abs_path=path, role="resource", version=version,
                          native_record_id=None)

        # Exact sidecar identity wins over filename parsing.  The File field is
        # the exporter's own association and does not depend on a name prefix.
        layout = self.layout_by_file.get(rel)
        if layout is not None:
            resource = self.resource_by_unique.get(str(layout.get("Resource")))
            if resource is None:
                add_gap(self.gaps, "subresource-layout-resource-unresolved", "warning", "payload",
                        f"SubresourceLayouts File {rel} names resource {layout.get('Resource')!r}, which has no native descriptor. The file is retained with its exact sidecar identity.",
                        rel)
                payload.role = "native-material"
            else:
                payload.resource_id = resource["id"]
                resource["payloadIds"].append(payload.id)
            payload.mip = int(layout.get("MipLevel") or 0)
            if str(layout.get("Dimension")) in ("Texture2DArray", "TextureCube") or int(layout.get("NumArraySlices") or 0) >= 1:
                payload.slice = int(layout.get("FirstArraySlice") or 0)
            if layout.get("CubeFace") is not None:
                payload.face = int(layout["CubeFace"])
            payload.stencil = bool(layout.get("StencilSubresource"))
            # Apply the sidecar now so an early gap/coverage pass sees the
            # authoritative subresource, not the filename guess.
            payload.layout = layout
            payload.completion = self._sidecar_completion(rel, layout)
            payload.native_kind = str(layout.get("Dimension") or "texture")
            payload.classification = "texture-readback-subresource"
            payload.decode = self._layout_decode(layout)
            return payload

        if stem.startswith("NativeBuffer."):
            # A native RHI typed-view subrange.  It has its own identity (a
            # native buffer pointer), not an RDG resource descriptor.
            parts = stem.split(".")
            pointer = parts[1] if len(parts) > 1 else None
            payload.role = "native-buffer"
            payload.pointer = pointer
            payload.native_kind = "native-buffer"
            payload.classification = "native-buffer-srv-subrange"
            payload.completion = self._sidecar_completion(rel, None)
            return payload

        matched_unique = None
        rest = None
        for unique in unique_names:
            if stem == unique:
                matched_unique, rest = unique, ""
                break
            if stem.startswith(unique) and len(stem) > len(unique) and stem[len(unique)] in ".[":
                matched_unique, rest = unique, stem[len(unique):]
                break
        if matched_unique is None:
            status = self.status_by_file.get(rel)
            if status is not None and status.get("Kind") == "SkipTrackingBuffer":
                # The filename pointer is the native identity key; the status
                # record is the completion authority.
                payload.role = "skip-tracking"
                payload.pointer = stem
                payload.native_kind = "skip-tracking-buffer"
                payload.classification = "skip-tracking-buffer"
                payload.completion = self._sidecar_completion(rel, None)
                resource = self.resource_by_unique.get(stem)
                if resource is not None:
                    payload.resource_id = resource["id"]
                    resource["payloadIds"].append(payload.id)
                return payload
            add_gap(self.gaps, "orphan-resource-payload", "error", "payload",
                    f"Payload cannot be associated with a native resource descriptor: {rel}", rel)
            payload.role = "orphan"
            return payload
        resource = self.resource_by_unique[matched_unique]
        payload.resource_id = resource["id"]
        mip = None
        slice_index = None
        stencil = False
        if rest == "":
            if resource["kind"] == "texture":
                add_gap(self.gaps, "texture-payload-without-mip", "warning", "payload",
                        f"Texture payload has no .mipN suffix; mip identity is not inferred: {rel}", rel)
        else:
            mm = re.fullmatch(r"\.mip(\d+)", rest)
            ms = re.fullmatch(r"\.\[(\d+)\]\.mip(\d+)", rest)
            mst = re.fullmatch(r"\.stencil", rest)
            mss = re.fullmatch(r"\.\[(\d+)\]\.stencil", rest)
            if mm:
                mip = int(mm.group(1))
            elif ms:
                slice_index = int(ms.group(1))
                mip = int(ms.group(2))
            elif mst:
                stencil = True
                mip = 0
            elif mss:
                slice_index = int(mss.group(1))
                stencil = True
                mip = 0
            else:
                add_gap(self.gaps, "unparsed-subresource-name", "warning", "payload",
                        f"Could not parse subresource suffix {rest!r}; raw payload retained and linked to the resource only.",
                        rel, resourceId=resource["id"])
        payload.mip = mip
        payload.slice = slice_index
        payload.stencil = stencil
        resource["payloadIds"].append(payload.id)
        return payload

    # ----------------------------------------------------------------- passes
    def _build_passes_from_objects(self) -> None:
        for index, (record, obj) in enumerate(self._pass_objects):
            pointer = str(obj.get("Pointer") or f"pass-{index}")
            if pointer in self.pass_by_pointer:
                add_gap(self.gaps, "duplicate-pass-pointer", "warning", "pass",
                        f"Duplicate pass pointer {pointer}; all native records are retained.", record["sourcePath"])
                continue
            pas = {
                "id": f"pass-{len(self.passes) + 1:04d}",
                "nativeIndex": index,
                "nativeRecordId": record["id"],
                "name": str(obj.get("EventName") or "Unnamed pass"),
                "pointer": pointer,
                "parametersName": obj.get("ParametersName"),
                "parametersPointer": obj.get("Parameters"),
                "parametersMetadataPointer": obj.get("ParametersMetadata"),
                "parentScopes": obj.get("ParentEventScopes") or [],
                "inputsRaw": list(obj.get("InputResources") or []),
                "outputsRaw": list(obj.get("OutputResources") or []),
                "inputs": [],
                "outputs": [],
                "drawCount": self.draw_counts.get(pointer),
                "drawCountStatus": "captured" if pointer in self.draw_counts else "not-captured",
                "drawCountSource": "Base/PassDrawCounts.json",
                "native": obj,
                "parameterBlock": None,
                "referencedStructs": [],
                "schemaMatches": [],
                "gaps": [],
            }
            self.passes.append(pas)
            self.pass_by_pointer[pointer] = pas

    def _associate_parameter_payloads(self) -> None:
        # Not every Structure/*.bin is a pass parameter block; referenced structs are
        # linked to passes by Base/ReferencedStructs.json below.
        by_pointer: dict[str, list[Payload]] = {}
        for spec in self._payload_specs:
            if spec.get("role") != "parameter":
                continue
            rel = spec["rel"]
            path = spec["abs"]
            name = rel[len("Structures/"):] if rel.startswith("Structures/") else rel
            pointer = name[:-4] if name.lower().endswith(".bin") else name
            payload = Payload(id=f"payload-{len(self.payloads) + 1:05d}", original_path=rel,
                              abs_path=path, role="parameter", pointer=pointer,
                              native_kind="structure",
                              classification="parameter-or-structure-block")
            self.payloads.append(payload)
            self.payload_by_id[payload.id] = payload
            by_pointer.setdefault(pointer, []).append(payload)
        used: set[str] = set()
        for pas in self.passes:
            pointer = str(pas.get("parametersPointer") or "")
            candidates = by_pointer.get(pointer, [])
            if candidates:
                payload = candidates[0]
                payload.pass_id = pas["id"]
                used.add(payload.id)
                pas["parameterBlock"] = {
                    "payloadId": payload.id,
                    "metadataPointer": pas.get("parametersMetadataPointer"),
                    "bytes": None,
                    "sha256": None,
                }
                if len(candidates) > 1:
                    add_gap(self.gaps, "duplicate-parameter-block", "warning", "pass",
                            f"Multiple Structures/*.bin payloads share parameter pointer {pointer}; first retained as the pass block, all raw files retained.",
                            payload.original_path, passId=pas["id"])
            else:
                if pointer:
                    add_gap(self.gaps, "parameter-block-not-captured", "warning", "pass",
                            f"Pass parameter block {pointer} was not captured. Parameter metadata may still be present; no bytes are fabricated.",
                            "Base/Passes.json", passId=pas["id"])
                else:
                    add_gap(self.gaps, "parameter-pointer-missing", "warning", "pass",
                            "Pass JSON has no Parameters pointer; parameter capture cannot be linked.",
                            "Base/Passes.json", passId=pas["id"])
            if pas.get("parametersMetadataPointer") and str(pas["parametersMetadataPointer"]) not in self.structures:
                add_gap(self.gaps, "parameter-metadata-missing", "warning", "pass",
                        f"Parameter metadata {pas['parametersMetadataPointer']} was not captured; raw block, if present, remains hex only.",
                        "StructuresMetadata/", passId=pas["id"])
        for pointer, candidates in by_pointer.items():
            for payload in candidates:
                if payload.id not in used:
                    add_gap(self.gaps, "orphan-parameter-payload", "warning", "payload",
                            f"Parameter binary {payload.original_path} is not referenced by a captured pass Parameters pointer; raw bytes retained.",
                            payload.original_path)

    def _associate_referenced_structs(self) -> None:
        by_pass: dict[str, list[dict[str, Any]]] = {}
        for entry in self.referenced_structs:
            pointer = str(entry.get("Pass") or "")
            refs = entry.get("ReferencedStructs")
            if isinstance(refs, dict):
                for member_name, ref in refs.items():
                    if not isinstance(ref, dict):
                        continue
                    # Preserve every member of the expanded ReferencedStructs
                    # record.  Content/Metadata are added as explicit link keys,
                    # but the original field names and values are not dropped.
                    item = dict(ref)
                    item.update({
                        "member": member_name,
                        "contentPointer": ref.get("Content"),
                        "metadataPointer": ref.get("Metadata"),
                        "payloadId": None,
                    })
                    by_pass.setdefault(pointer, []).append(item)
        for pas in self.passes:
            refs = by_pass.get(str(pas.get("pointer")), [])
            for ref in refs:
                ptr = str(ref.get("contentPointer") or "")
                for payload in self.payloads:
                    if payload.role == "parameter" and payload.pointer == ptr:
                        ref["payloadId"] = payload.id
                        payload.pass_id = pas["id"]
                        self.gaps = [
                            gap for gap in self.gaps
                            if not (gap.get("kind") == "orphan-parameter-payload"
                                    and gap.get("source") == payload.original_path)
                        ]
                        break
                if ref["payloadId"] is None:
                    if ref.get("Null") is True:
                        # A null binding is a recorded state, not a missing file.
                        # Preserve ContentKind (for example RHIUniformBufferUnbound)
                        # and do not manufacture bytes.
                        ref["status"] = "null-reference"
                        add_gap(self.gaps, "referenced-struct-null", "info", "pass",
                                f"Referenced struct {ref['member']} is recorded Null=true ({ref.get('ContentKind') or 'no content kind'}); there is no captured block because the source binding is explicitly null.",
                                "Base/ReferencedStructs.json", passId=pas["id"])
                    else:
                        add_gap(self.gaps, "referenced-struct-not-captured", "warning", "pass",
                                f"Referenced struct {ref['member']} ({ptr}) has no captured Structures/<pointer>.bin.",
                                "Base/ReferencedStructs.json", passId=pas["id"])
            pas["referencedStructs"] = refs

    def _finish_relationships(self) -> None:
        # Link pass input/output subresource names to resource descriptors and payloads.
        unique_names = sorted(self.resource_by_unique, key=len, reverse=True)
        for pas in self.passes:
            for direction, raw_list in (("input", pas["inputsRaw"]), ("output", pas["outputsRaw"])):
                linked = []
                for raw_name in raw_list:
                    name = str(raw_name)
                    if name.lower().startswith("nativebuffer."):
                        # Native RHI typed-view subranges are not RDG resources and
                        # do not appear in ResourceDescs.json.  Associate them with
                        # their exact captured file list and native pointer instead
                        # of reporting an unresolved RDG resource.
                        native_ids = [
                            payload.id for payload in self.payloads
                            if payload.role == "native-buffer"
                            and (payload.original_path.lower().startswith(f"resources/{name.lower()}.v")
                                 or payload.original_path.lower().startswith(f"resources/{name.lower()}."))
                        ]
                        item = {
                            "uniqueName": name,
                            "resourceId": None,
                            "nativeKind": "native-buffer",
                            "nativePointer": name.split(".")[1] if "." in name else None,
                            "version": None,
                            "mip": None,
                            "slice": None,
                            "stencil": False,
                            "payloadMatch": "exact native DumpedAs/ViewOffset/ViewSize",
                            "payloadIds": native_ids,
                        }
                        if not native_ids:
                            add_gap(self.gaps, "native-buffer-payload-unresolved", "warning", "pass",
                                    f"Pass {pas['name']} references native buffer view {name!r}, but no captured .bin matches that view identity.",
                                    "Base/Passes.json", passId=pas["id"])
                        if direction == "input":
                            pas["inputs"].append(item)
                        else:
                            pas["outputs"].append(item)
                        continue
                    resource, sub = self._parse_subresource_name(name, unique_names)
                    item = {
                        "uniqueName": name,
                        "resourceId": resource["id"] if resource else None,
                        "version": sub.get("version"),
                        "mip": sub.get("mip"),
                        "slice": sub.get("slice"),
                        "stencil": sub.get("stencil"),
                        "payloadMatch": "resource+mip+slice+aspect+version-when-named",
                        "payloadIds": [],
                    }
                    if resource:
                        # Match the full subresource identity.  In particular a
                        # colour (non-stencil) binding must never list stencil
                        # payloads, and a stencil binding must never list colour
                        # bytes.  Version is applied when the native binding
                        # name carries it; otherwise every captured version at
                        # the same mip/slice/aspect is listed explicitly.
                        item["payloadIds"] = [
                            payload.id for payload in self.payloads
                            if payload.resource_id == resource["id"]
                            and payload.role == "resource"
                            and (sub.get("version") is None or payload.version == sub.get("version"))
                            and (sub.get("mip") is None or payload.mip == sub.get("mip"))
                            and (sub.get("slice") is None or payload.slice == sub.get("slice"))
                            and bool(payload.stencil) == bool(sub.get("stencil"))
                        ]
                        if direction == "input":
                            pas["inputs"].append(item)
                            if pas["id"] not in resource["consumers"]:
                                resource["consumers"].append(pas["id"])
                        else:
                            pas["outputs"].append(item)
                            if pas["id"] not in resource["producers"]:
                                resource["producers"].append(pas["id"])
                    else:
                        add_gap(self.gaps, "pass-resource-unresolved", "warning", "pass",
                                f"Pass {pas['name']} references {name!r}, which has no native resource descriptor.",
                                "Base/Passes.json", passId=pas["id"])
                        if direction == "input":
                            pas["inputs"].append(item)
                        else:
                            pas["outputs"].append(item)
            if not pas["inputsRaw"] and not pas["outputsRaw"]:
                add_gap(self.gaps, "pass-without-bindings", "info", "pass",
                        f"Pass {pas['name']} has no captured input/output resources.", "Base/Passes.json",
                        passId=pas["id"])

        # Versions and producer links.
        for resource in self.resources:
            payloads = [p for p in self.payloads if p.resource_id == resource["id"] and p.role == "resource"]
            by_version: dict[str, list[Payload]] = {}
            for payload in payloads:
                key = payload.version or "(no-version)"
                by_version.setdefault(key, []).append(payload)
            versions = []
            for key, group in by_version.items():
                producer = self.pass_by_pointer.get(key) if key not in ("(no-version)",) else None
                versions.append({
                    "version": key,
                    "producerPassId": producer["id"] if producer else None,
                    "producerPassName": producer["name"] if producer else None,
                    "payloadIds": [p.id for p in group],
                })
            versions.sort(key=lambda v: self._pass_order(v.get("producerPassId")))
            for idx, version in enumerate(versions):
                version["previousVersion"] = versions[idx - 1]["version"] if idx > 0 else None
                version["nextVersion"] = versions[idx + 1]["version"] if idx + 1 < len(versions) else None
            resource["versions"] = versions
            for version in versions:
                # DumpGPU uses the all-zero pointer for a resource first observed
                # as an external/initial input (no pass-produced version).
                if version["version"] not in ("(no-version)", "0" * 20) and not version["producerPassId"]:
                    add_gap(self.gaps, "payload-version-pass-unresolved", "info", "resource",
                            f"Payload version {version['version']} does not match a captured pass pointer.",
                            self.resource_by_unique.get(resource["uniqueName"], {}).get("nativePath"),
                            resourceId=resource["id"])
            self._resource_coverage(resource, payloads)
            if resource["bufferMetadata"] is None and resource.get("metadataPointer"):
                pointer = str(resource["metadataPointer"])
                resource["bufferMetadata"] = self.structures.get(pointer)
                if resource["bufferMetadata"] is None and pointer.strip("0"):
                    add_gap(self.gaps, "buffer-structure-metadata-missing", "warning", "decoding",
                            f"Buffer metadata pointer {pointer} has no StructuresMetadata/*.json record; typed fields remain unavailable.",
                            resource.get("nativePath"), resourceId=resource["id"])

    def _view_state_for_pass(self, pas: dict[str, Any]) -> dict[str, Any] | None:
        """Read View.StateFrameIndexMod8 / ViewRect from a captured View.View block.

        The referenced block and its member offsets are recorded metadata
        (Base/ReferencedStructs.json plus StructuresMetadata).  Only those
        explicit offsets are read; no value is inferred from a name.
        """
        for ref in pas.get("referencedStructs", []) or []:
            if str(ref.get("member") or "") != "View.View":
                continue
            meta = self.structures.get(str(ref.get("metadataPointer") or ""))
            payload = self.payload_by_id.get(str(ref.get("payloadId") or ""))
            if not isinstance(meta, dict) or payload is None or not payload.abs_path.exists():
                continue
            members = {
                str(member.get("Name")): member
                for member in (meta.get("Members") or [])
                if isinstance(member, dict)
            }

            def read_uint(member_name: str, index: int = 0) -> int | None:
                member = members.get(member_name)
                if member is None:
                    return None
                offset = int(member.get("Offset") or 0) + index * 4
                try:
                    with payload.abs_path.open("rb") as handle:
                        handle.seek(offset)
                        raw = handle.read(4)
                except OSError:
                    return None
                return int.from_bytes(raw, "little") if len(raw) == 4 else None

            frame_mod8 = read_uint("StateFrameIndexMod8")
            frame_index = read_uint("StateFrameIndex")
            min_and_size = [read_uint("ViewRectMinAndSize", i) for i in range(4)]
            view_rect = None
            if all(v is not None for v in min_and_size):
                view_rect = {
                    "min": [min_and_size[0], min_and_size[1]],
                    "size": [min_and_size[2], min_and_size[3]],
                    "source": "ViewRectMinAndSize (uint32 x 4)",
                }
            if frame_mod8 is None and frame_index is None and view_rect is None:
                continue
            return {
                "member": "View.View",
                "metadataPointer": ref.get("metadataPointer"),
                "contentPointer": ref.get("contentPointer"),
                "payloadId": payload.id,
                "frameIndexMod8": frame_mod8,
                "frameIndex": frame_index,
                "viewRect": view_rect,
                "source": "Base/ReferencedStructs.json View.View content read at StructuresMetadata member offsets",
            }
        return None

    def _annotate_scene_metadata(self) -> None:
        """Derive frame/graph/invocation labels and view-rect padding from metadata."""
        for pas in self.passes:
            scopes = [str(s) for s in (pas.get("parentScopes") or [])]
            frame = None
            for scope in scopes:
                match = re.search(r"Frame\s+(\d+)", scope)
                if match:
                    frame = int(match.group(1))
                    break
            graph_scopes = [s for s in scopes if not re.match(r"Frame\s+\d+", s, re.I)]
            pas["frameNumber"] = frame
            pas["graphPrefix"] = graph_scopes[0] if graph_scopes else None
            pas["viewState"] = self._view_state_for_pass(pas)
            native = pas.get("native") if isinstance(pas.get("native"), dict) else {}
            native_id = native.get("Id")
            label = pas["name"]
            if pas["graphPrefix"] and pas["graphPrefix"].strip().lower() != "scene":
                label += " · " + pas["graphPrefix"]
            if frame is not None:
                label += " · frame " + str(frame)
            if native_id is not None:
                label += " · pass " + str(native_id)
            view = pas["viewState"]
            if view and view.get("frameIndexMod8") is not None:
                label += " · View.StateFrameIndexMod8 " + str(view["frameIndexMod8"])
            pas["displayLabel"] = label

        name_counts = Counter(resource["name"] for resource in self.resources)
        resource_by_id = {resource["id"]: resource for resource in self.resources}
        camera_passes = self._camera_view_passes(resource_by_id)
        self.camera_view_passes = camera_passes
        for resource in self.resources:
            producer_ids = list(resource.get("producers") or [])
            producers = [p for p in self.passes if p["id"] in producer_ids]
            producers.sort(key=lambda p: self._pass_order(p["id"]))
            primary = producers[-1] if producers else None
            if primary is None:
                consumer_ids = list(resource.get("consumers") or [])
                consumers = [p for p in self.passes if p["id"] in consumer_ids]
                consumers.sort(key=lambda p: self._pass_order(p["id"]))
                primary = consumers[0] if consumers else None
            view = primary.get("viewState") if primary else None
            scopes = [str(s) for s in (primary.get("parentScopes") or [])] if primary else []
            in_scene = any("scene" in scope.lower() for scope in scopes)
            unique = str(resource.get("uniqueName") or "")
            prefix = unique[:4] if re.fullmatch(r"[0-9a-fA-F]{4}", unique[:4]) else None
            resource["instancePrefix"] = prefix
            resource["sceneInvocation"] = prefix if (view and in_scene) else None
            resource["frameNumber"] = primary.get("frameNumber") if primary else None
            resource["viewState"] = view
            extent = [int(v or 1) for v in (resource.get("extent") or [1, 1, 1])]
            native_type = str((resource.get("native") or {}).get("Type") or "")
            if resource.get("kind") == "texture" and extent[2] <= 1 and "cube" not in native_type.lower():
                resource["allocation"] = extent[:2]
            else:
                resource["allocation"] = extent
            consumer_ids = list(resource.get("consumers") or [])
            consumers = [p for p in self.passes if p["id"] in consumer_ids]
            consumers.sort(key=lambda p: self._pass_order(p["id"]))
            resource["viewport"], resource["padding"] = self._resource_viewport(
                resource, producers, consumers, camera_passes, resource_by_id)
            native_ids = []
            for producer in producers:
                pid = (producer.get("native") or {}).get("Id") if isinstance(producer.get("native"), dict) else None
                if pid is not None:
                    native_ids.append(int(pid))
            resource["passInterval"] = (
                {"first": min(native_ids), "last": max(native_ids)} if native_ids else None
            )
            label = resource["name"]
            if resource["sceneInvocation"]:
                label += " · scene " + resource["sceneInvocation"]
            elif name_counts[resource["name"]] > 1 and prefix:
                label += " · instance " + prefix
            if resource["frameNumber"] is not None:
                label += " · frame " + str(resource["frameNumber"])
            interval = resource["passInterval"]
            if interval:
                label += " · passes " + str(interval["first"])
                if interval["last"] != interval["first"]:
                    label += ".." + str(interval["last"])
            if view and view.get("frameIndexMod8") is not None:
                label += " · View.StateFrameIndexMod8 " + str(view["frameIndexMod8"])
            resource["displayLabel"] = label

    def _camera_view_passes(self, resource_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """Return passes that really render a camera target with a captured ViewRect.

        A pass qualifies only when it has its own captured View.View rect AND
        writes a screen target (SceneColor/SceneDepthZ/GBuffer*).  Allocation
        area is deliberately not consulted; a 16384-wide shadow page pool can
        never become the camera just because it is the largest allocation.
        """
        cameras: list[dict[str, Any]] = []
        for pas in self.passes:
            view = pas.get("viewState") if isinstance(pas.get("viewState"), dict) else {}
            rect = view.get("viewRect") if isinstance(view.get("viewRect"), dict) else None
            size = rect.get("size") if isinstance(rect, dict) else None
            if not isinstance(size, list) or len(size) < 2:
                continue
            try:
                width, height = int(size[0]), int(size[1])
            except (TypeError, ValueError):
                continue
            if width <= 0 or height <= 0:
                continue
            output_names = set()
            for item in pas.get("outputs") or []:
                resource = resource_by_id.get(item.get("resourceId"))
                if resource:
                    output_names.add(str(resource.get("name") or ""))
            if output_names & CAMERA_SCREEN_TARGET_NAMES:
                cameras.append(pas)
        return cameras

    def _resource_domain(self, resource: dict[str, Any]) -> str:
        if resource.get("kind") != "texture":
            return "non-texture"
        native = resource.get("native") if isinstance(resource.get("native"), dict) else {}
        native_type = str(native.get("Type") or "").lower()
        extent = [int(v or 1) for v in (resource.get("extent") or [1, 1, 1])]
        if "cube" in native_type:
            return "cube"
        if "3d" in native_type or "volume" in native_type or extent[2] > 1:
            return "volume-3d"
        if "array" in native_type or int(resource.get("arraySize") or 1) > 1:
            return "array"
        return "texture2d"

    def _non_screen_classification(self, resource: dict[str, Any]) -> tuple[str | None, str | None]:
        name = str(resource.get("name") or "").lower()
        if "shadow.virtual" in name or re.search(r"(^|[^a-z])vsm([^a-z]|$)", name):
            return "virtual-shadow-map", (
                "Virtual-shadow-map page pools use their own page/tile domain; "
                "the recorded pass ViewRect is context only and no screen padding is inferred.")
        if "history" in name:
            return "history", (
                "Temporal history resources use their own history/view domain; "
                "the recorded pass ViewRect is context only and no screen padding is inferred.")
        if "probe" in name or "downsample" in name or "grid" in name:
            return "probe-grid", (
                "Probe-grid/downsampled resources use their own grid domain; "
                "the recorded pass ViewRect is context only and no screen padding is inferred.")
        if "card" in name:
            return "card-atlas", (
                "Card atlases use their own card-page domain; "
                "the recorded pass ViewRect is context only and no screen padding is inferred.")
        if "atlas" in name or "cache" in name:
            return "atlas", (
                "Atlas resources use their own atlas domain; "
                "the recorded pass ViewRect is context only and no screen padding is inferred.")
        return None, None

    def _screen_viewport(self, resource: dict[str, Any], pas: dict[str, Any],
                         extent: list[int], resource_by_id: dict[str, dict[str, Any]]) -> tuple[
                             dict[str, Any] | None, dict[str, Any] | None, tuple[str, str] | None]:
        """Build a screen mapping only when every same-view fact matches.

        Required positive evidence:
          * the pass is a camera pass (see _camera_view_passes);
          * the resource is an output of that exact pass;
          * the active mip-0 SubresourceLayouts record belongs to that pass
            version and its own extent matches the resource descriptor;
          * the pass ViewRect lies fully inside the allocation.
        """
        view = pas.get("viewState") if isinstance(pas.get("viewState"), dict) else {}
        rect = view.get("viewRect") if isinstance(view.get("viewRect"), dict) else None
        if not isinstance(rect, dict):
            return None, None, ("no-viewrect", "The camera pass has no recorded View.View rect.")
        try:
            min_x, min_y = [int(v) for v in rect.get("min")[:2]]
            size_x, size_y = [int(v) for v in rect.get("size")[:2]]
        except (TypeError, ValueError, IndexError):
            return None, None, ("invalid-viewrect", "The recorded ViewRect min/size is not a four-integer record.")
        if size_x <= 0 or size_y <= 0 or min_x < 0 or min_y < 0:
            return None, None, ("invalid-viewrect", "The recorded ViewRect is empty or starts outside the allocation.")
        if min_x + size_x > extent[0] or min_y + size_y > extent[1]:
            return None, None, (
                "viewrect-outside-allocation",
                f"The camera ViewRect {size_x}x{size_y} at ({min_x},{min_y}) is not fully inside the "
                f"{extent[0]}x{extent[1]} allocation; no padding is inferred.")

        # Known same-size sceneColor: its own bounds must match the GBuffer
        # target produced by the same camera pass.
        if str(resource.get("name") or "") == "SceneColor":
            gbuffer_sizes = set()
            for item in pas.get("outputs") or []:
                other = resource_by_id.get(item.get("resourceId"))
                if other and str(other.get("name") or "").startswith("GBuffer"):
                    other_extent = [int(v or 1) for v in (other.get("extent") or [1, 1, 1])]
                    gbuffer_sizes.add((other_extent[0], other_extent[1]))
            if (extent[0], extent[1]) not in gbuffer_sizes:
                return None, None, (
                    "scene-color-not-same-size",
                    "SceneColor is not the same size as the GBuffer target of the same camera pass; "
                    "screen alignment is not proven.")

        pass_pointer = str(pas.get("pointer") or "")
        matching: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for item in resource.get("subresourceLayouts") or []:
            payload = self.payload_by_id.get(str(item.get("payloadId") or ""))
            if payload is None:
                continue
            if str(payload.version or "") != pass_pointer:
                continue
            layout = item.get("layout") if isinstance(item.get("layout"), dict) else {}
            if str(layout.get("Dimension") or "") != "Texture2D":
                continue
            if int(layout.get("MipLevel") or 0) != 0:
                continue
            matching.append((item, layout))
        if not matching:
            return None, None, (
                "same-view-active-mip-unavailable",
                "No active mip-0 SubresourceLayouts record has a payload version equal to this camera "
                "pass pointer; same-view screen alignment is not proven.")
        for item, layout in matching:
            if int(layout.get("ExtentX") or 0) != extent[0] or int(layout.get("ExtentY") or 0) != extent[1]:
                return None, None, (
                    "active-mip-bounds-mismatch",
                    f"The active mip-0 subresource extent {layout.get('ExtentX')}x{layout.get('ExtentY')} "
                    f"does not match the resource descriptor {extent[0]}x{extent[1]}.")
            if int(layout.get("Depth") or 1) != 1 or int(layout.get("ZSliceCount") or 1) != 1:
                return None, None, (
                    "active-mip-not-2d",
                    "The active mip-0 subresource is not a single 2D slice; its own domain is retained.")
            if layout.get("CubeFace") is not None:
                return None, None, (
                    "active-mip-cube-face",
                    "The active mip-0 subresource is a cube face; a camera ViewRect does not apply.")
        chosen_item, chosen_layout = next(
            ((item, layout) for item, layout in matching if not layout.get("StencilSubresource")),
            matching[0])
        padding = {
            "left": min_x,
            "top": min_y,
            "right": extent[0] - (min_x + size_x),
            "bottom": extent[1] - (min_y + size_y),
            "validWidth": size_x,
            "validHeight": size_y,
            "padded": bool(min_x or min_y or size_x < extent[0] or size_y < extent[1]),
            "note": (
                "Display-only crop to the recorded camera ViewRect. Allocation is the native texture extent; "
                "captured bytes, active-mip bounds and subresource addressing are unchanged. "
                "Bytes outside the rect are labelled, not removed."
            ),
        }
        basis = (
            f"{pas.get('name')} (pass {pas.get('id')}, pointer {pass_pointer}) outputs "
            f"{resource.get('name')}; active mip 0 payload {chosen_item.get('file')} has extent "
            f"{chosen_layout.get('ExtentX')}x{chosen_layout.get('ExtentY')} and version equal to the pass; "
            f"ViewRect {size_x}x{size_y} at ({min_x},{min_y}) lies fully inside the allocation."
        )
        if str(resource.get("name") or "") == "SceneColor":
            basis += " The same pass's GBuffer target has the same extent, confirming the known same-size sceneColor mapping."
        viewport = {
            "applicability": "screen-aligned",
            "screenPadding": True,
            "cropAllowed": True,
            "domain": "screen",
            "allocation": extent[:2] if extent[2] <= 1 else extent,
            "activeMip": 0,
            "mipExtent": [int(chosen_layout.get("ExtentX") or 0), int(chosen_layout.get("ExtentY") or 0)],
            "boundsMatch": True,
            "passViewRect": dict(rect),
            "passId": pas.get("id"),
            "passName": pas.get("name"),
            "passPointer": pass_pointer,
            "passIsCameraPass": True,
            "sameViewPayload": chosen_item.get("file"),
            "storedFormat": chosen_layout.get("StoredFormat"),
            "basis": basis,
            "reason": None,
            "source": VIEWPORT_SOURCE,
        }
        return viewport, padding, None

    def _resource_viewport(self, resource: dict[str, Any],
                           producers: list[dict[str, Any]], consumers: list[dict[str, Any]],
                           camera_passes: list[dict[str, Any]],
                           resource_by_id: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Record explicit viewport applicability and a crop decision per resource."""
        extent = [int(v or 1) for v in (resource.get("extent") or [1, 1, 1])]
        domain = self._resource_domain(resource)
        context = None
        for pas in reversed(producers):
            if (pas.get("viewState") or {}).get("viewRect"):
                context = pas
                break
        if context is None:
            for pas in consumers:
                if (pas.get("viewState") or {}).get("viewRect"):
                    context = pas
                    break
        failure: tuple[str, str] | None = None
        if domain == "texture2d" and str(resource.get("name") or "") in CAMERA_SCREEN_TARGET_NAMES:
            for pas in sorted(camera_passes, key=lambda p: self._pass_order(p["id"]), reverse=True):
                if pas.get("id") not in (resource.get("producers") or []):
                    continue
                if not any(item.get("resourceId") == resource.get("id") for item in (pas.get("outputs") or [])):
                    continue
                viewport, padding, failed = self._screen_viewport(resource, pas, extent, resource_by_id)
                if viewport is not None:
                    return viewport, padding
                if failure is None and failed is not None:
                    failure = failed

        context_view = context.get("viewState") if context and isinstance(context.get("viewState"), dict) else None
        context_rect = context_view.get("viewRect") if context_view else None
        context_camera = bool(context and context.get("id") in {p.get("id") for p in camera_passes})
        classification, classification_reason = self._non_screen_classification(resource)
        if context is None:
            applicability = "no-view-context"
            reason = (
                "No captured View.View rect is associated with this resource's pass interval; "
                "screen padding is not inferred and the viewport crop is disabled.")
            basis = "No pass-associated View.View rect."
        elif domain != "texture2d":
            applicability = "other-domain"
            if domain == "cube":
                reason = ("TextureCube faces have their own per-face domain; the recorded pass ViewRect "
                          "is context only and no screen padding is inferred.")
            elif domain == "volume-3d":
                reason = ("Texture3D/volume atlases have their own x/y/z domain; the recorded pass ViewRect "
                          "is context only and no screen padding is inferred.")
            elif domain == "array":
                reason = ("Texture2DArray slices have their own array domain; the recorded pass ViewRect "
                          "is context only and no screen padding is inferred.")
            else:
                reason = ("Buffers have no 2D screen-texel domain; the recorded pass ViewRect is context "
                          "only and no screen padding is inferred.")
            basis = f"{context.get('name')} (pass {context.get('id')}) ViewRect retained as context only."
        elif classification:
            applicability = "other-domain"
            reason = classification_reason or "This resource has its own domain; the ViewRect is context only."
            basis = f"{context.get('name')} (pass {context.get('id')}) ViewRect retained as context only."
        else:
            applicability = "unknown-mapping"
            reason = failure[1] if failure else (
                "This resource is attached to a pass with a recorded ViewRect, but it is not a positively "
                "identified camera-pass screen-aligned target; padding is not inferred and the viewport "
                "crop is disabled.")
            basis = f"{context.get('name')} (pass {context.get('id')}) ViewRect retained as context only."
        bounds_match = None
        if failure and failure[0] in ("active-mip-bounds-mismatch", "viewrect-outside-allocation"):
            bounds_match = False
        viewport = {
            "applicability": applicability,
            "screenPadding": False,
            "cropAllowed": False,
            "domain": domain,
            "classification": classification,
            "allocation": extent[:2] if extent[2] <= 1 else extent,
            "activeMip": None,
            "mipExtent": None,
            "boundsMatch": bounds_match,
            "passViewRect": dict(context_rect) if isinstance(context_rect, dict) else None,
            "passId": context.get("id") if context else None,
            "passName": context.get("name") if context else None,
            "passPointer": context.get("pointer") if context else None,
            "passIsCameraPass": context_camera,
            "sameViewPayload": None,
            "storedFormat": None,
            "basis": basis,
            "reason": reason,
            "source": VIEWPORT_SOURCE,
        }
        return viewport, None

    def _viewport_summary(self) -> dict[str, Any]:
        applicability = Counter()
        domains = Counter()
        classifications = Counter()
        screen_targets: list[dict[str, Any]] = []
        non_screen_contexts: list[dict[str, Any]] = []
        for resource in self.resources:
            viewport = resource.get("viewport") if isinstance(resource.get("viewport"), dict) else {}
            applicability[str(viewport.get("applicability") or "unrecorded")] += 1
            domains[str(viewport.get("domain") or "unrecorded")] += 1
            classifications[str(viewport.get("classification") or "none")] += 1
            if viewport.get("applicability") == "screen-aligned":
                screen_targets.append({
                    "id": resource["id"], "name": resource["name"],
                    "uniqueName": resource["uniqueName"], "passId": viewport.get("passId"),
                    "passName": viewport.get("passName"), "viewRect": viewport.get("passViewRect"),
                    "allocation": viewport.get("allocation"), "padding": resource.get("padding"),
                })
            elif viewport.get("passViewRect"):
                non_screen_contexts.append({
                    "id": resource["id"], "name": resource["name"],
                    "uniqueName": resource["uniqueName"], "applicability": viewport.get("applicability"),
                    "domain": viewport.get("domain"), "classification": viewport.get("classification"),
                    "passId": viewport.get("passId"), "passName": viewport.get("passName"),
                    "passViewRect": viewport.get("passViewRect"), "reason": viewport.get("reason"),
                })
        cameras = []
        for pas in self.camera_view_passes:
            view = pas.get("viewState") if isinstance(pas.get("viewState"), dict) else {}
            cameras.append({
                "id": pas.get("id"), "name": pas.get("name"), "pointer": pas.get("pointer"),
                "viewRect": view.get("viewRect"),
            })
        return {
            "source": VIEWPORT_SOURCE,
            "cameraPasses": cameras,
            "applicabilityCounts": dict(sorted(applicability.items())),
            "domainCounts": dict(sorted(domains.items())),
            "classificationCounts": dict(sorted(classifications.items())),
            "screenAlignedResources": screen_targets,
            "nonScreenPassViewRectContexts": non_screen_contexts,
        }

    def _draw_count_gaps(self) -> None:
        missing = [pas for pas in self.passes if pas.get("drawCount") is None]
        if not missing:
            return
        path = self.dump_root / "Base" / "PassDrawCounts.json"
        if path.exists() and path.stat().st_size <= 2:
            add_gap(self.gaps, "pass-draw-counts-empty", "warning", "capture",
                    f"Base/PassDrawCounts.json is empty (0 records). All {len(missing)} pass drawCount values remain null (not captured); zero is not substituted.",
                    "Base/PassDrawCounts.json")
        elif path.exists():
            add_gap(self.gaps, "pass-draw-counts-incomplete", "warning", "capture",
                    f"Base/PassDrawCounts.json has {len(self.draw_counts)} record(s); {len(missing)} of {len(self.passes)} passes have no captured draw count. drawCount stays null for those passes; zero is not substituted.",
                    "Base/PassDrawCounts.json")
        else:
            add_gap(self.gaps, "pass-draw-counts-missing", "error", "capture",
                    f"Base/PassDrawCounts.json is absent; all {len(missing)} pass drawCount values remain null (not captured).",
                    "Base/PassDrawCounts.json")

    def _pass_order(self, pass_id: str | None) -> tuple[int, str]:
        if not pass_id:
            return (10**9, "")
        pas = next((p for p in self.passes if p["id"] == pass_id), None)
        return (pas["nativeIndex"] if pas else 10**9, pass_id)

    def _parse_subresource_name(self, name: str, unique_names: list[str]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        version = None
        stem = name
        m = re.search(r"\.v([0-9a-fA-F]+)$", stem)
        if m:
            version = m.group(1)
            stem = stem[:m.start()]
        for unique in unique_names:
            if stem == unique:
                return self.resource_by_unique.get(unique), {"version": version, "mip": None, "slice": None, "stencil": False}
            if stem.startswith(unique) and len(stem) > len(unique) and stem[len(unique)] in ".[":
                rest = stem[len(unique):]
                resource = self.resource_by_unique.get(unique)
                mm = re.fullmatch(r"\.mip(\d+)", rest)
                ms = re.fullmatch(r"\.\[(\d+)\]\.mip(\d+)", rest)
                mst = re.fullmatch(r"\.stencil", rest)
                mss = re.fullmatch(r"\.\[(\d+)\]\.stencil", rest)
                sub = {"version": version, "mip": None, "slice": None, "stencil": False}
                if mm:
                    sub["mip"] = int(mm.group(1))
                elif ms:
                    sub["slice"] = int(ms.group(1))
                    sub["mip"] = int(ms.group(2))
                elif mst:
                    sub["stencil"] = True
                    sub["mip"] = 0
                elif mss:
                    sub["slice"] = int(mss.group(1))
                    sub["stencil"] = True
                    sub["mip"] = 0
                return resource, sub
        return None, {"version": None, "mip": None, "slice": None, "stencil": False}

    def _resource_coverage(self, resource: dict[str, Any], payloads: list[Payload]) -> None:
        if not payloads:
            add_gap(self.gaps, "resource-payload-missing", "warning", "resource",
                    f"Resource {resource['name']} ({resource['kind']}, {resource['bytes']} declared bytes) has no captured binary payload.",
                    resource.get("nativePath"), resourceId=resource["id"])
            return
        if resource["kind"] == "texture":
            observed = len({(p.mip, p.slice, p.stencil) for p in payloads})
            declared = max(1, int(resource.get("numMips") or 1)) * max(1, int(resource.get("arraySize") or 1))
            if observed < declared:
                add_gap(self.gaps, "declared-subresources-not-all-observed", "info", "resource",
                        f"Observed {observed} texture subresource payload(s) against {declared} declared mip/array slots. DumpGPU may dump only referenced subresources; this explorer does not infer missing bytes.",
                        resource.get("nativePath"), resourceId=resource["id"])
            if resource.get("numSamples", 1) > 1:
                add_gap(self.gaps, "multisample-texture", "warning", "decoding",
                        f"Resource has NumSamples={resource.get('numSamples')}; the explorer does not decode multisample storage.",
                        resource.get("nativePath"), resourceId=resource["id"])
        if resource["kind"] == "buffer":
            if resource.get("bytesPerElement") and resource.get("numElements"):
                expected = int(resource["bytesPerElement"]) * int(resource["numElements"])
                if expected != int(resource["bytes"] or 0):
                    add_gap(self.gaps, "buffer-size-mismatch", "info", "resource",
                            f"Declared ByteSize {resource['bytes']} differs from BytesPerElement*NumElements ({expected}). Raw bytes are authoritative.",
                            resource.get("nativePath"), resourceId=resource["id"])

    def _finish_normalized_payload_links(self) -> None:
        by_resource = {r["id"]: r for r in self.resources}
        by_pass = {p["id"]: p for p in self.passes}
        for payload in self.payloads:
            if payload.resource_id in by_resource:
                by_resource[payload.resource_id]["payloadIds"].append(payload.id)
            if payload.pass_id in by_pass:
                pas = by_pass[payload.pass_id]
                if pas.get("parameterBlockSpec") is None:
                    pas["parameterBlock"] = {"payloadId": payload.id, "metadataPointer": None, "bytes": None, "sha256": None}
        for resource in self.resources:
            payloads = [p for p in self.payloads if p.resource_id == resource["id"]]
            by_version: dict[str, list[Payload]] = {}
            for payload in payloads:
                by_version.setdefault(payload.version or "(no-version)", []).append(payload)
            resource["versions"] = [
                {"version": version, "producerPassId": None, "producerPassName": None,
                 "payloadIds": [p.id for p in group], "previousVersion": None, "nextVersion": None}
                for version, group in by_version.items()
            ]
            if not payloads:
                add_gap(self.gaps, "resource-payload-missing", "warning", "resource",
                        f"Resource {resource['name']} has no normalized payload.", "normalized-frame.json",
                        resourceId=resource["id"])

    # ---------------------------------------------------------------- schema
    def load_schema(self) -> None:
        candidate = self.schema_path
        if candidate is None:
            for name in ("source-schema.json", "schema.json"):
                p = self.dump_root / name
                if p.exists():
                    candidate = p
                    break
        if candidate is None:
            self.schema_status = "missing"
            add_gap(self.gaps, "source-schema-missing", "error", "schema",
                    "No source schema was supplied. Raw metadata and bytes remain inspectable; semantic anchors, units and field joins are unavailable.",
                    "source-schema.json")
            self.schema_doc = None
            self._write_schema_placeholder()
            return
        candidate = candidate.resolve()
        try:
            self.schema_input_path = candidate.relative_to(self.input_root).as_posix()
        except ValueError:
            self.schema_input_path = candidate.name
        if not candidate.exists():
            self.schema_status = "missing"
            add_gap(self.gaps, "source-schema-missing", "error", "schema",
                    f"Source schema does not exist: {self.schema_input_path}", self.schema_input_path)
            self.schema_doc = None
            self._write_schema_placeholder()
            return
        try:
            data = candidate.read_bytes()
            doc = json.loads(data.decode("utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            self.schema_status = "invalid"
            add_gap(self.gaps, "source-schema-invalid", "error", "schema",
                    f"Source schema could not be parsed: {exc}", self.schema_input_path)
            self.schema_doc = None
            self._write_schema_placeholder()
            return
        if not isinstance(doc, dict):
            self.schema_status = "invalid"
            add_gap(self.gaps, "source-schema-invalid", "error", "schema",
                    "Source schema root is not a JSON object.", self.schema_input_path)
            self.schema_doc = None
            self._write_schema_placeholder()
            return
        self.schema_doc = doc
        self.schema_raw = data
        self.schema_status = "joined"
        self.schema_sha256 = sha256_bytes(data)
        self.schema_bytes = len(data)
        resources = doc.get("resourceSchemas") if isinstance(doc.get("resourceSchemas"), list) else []
        params = doc.get("parameterSchemas") if isinstance(doc.get("parameterSchemas"), list) else []
        self._join_resource_schemas(resources)
        self._join_parameter_schemas(params)
        self._schema_field_gaps(resources, parameter=False)
        self._schema_field_gaps(params, parameter=True)

    def _write_schema_placeholder(self) -> None:
        self.schema_doc = {
            "format": "unreal-frame-explorer-schema",
            "status": self.schema_status,
            "schemaVersion": None,
            "resourceSchemas": [],
            "parameterSchemas": [],
            "transforms": [],
            "mathTerms": [],
            "sourceRefs": {},
            "gap": "No source schema was available at package time. Nothing was inferred in its place.",
        }

    def _join_resource_schemas(self, schemas: list[Any]) -> None:
        for schema in schemas:
            if not isinstance(schema, dict):
                continue
            schema_id = str(schema.get("id") or f"resource-schema-{len(self.schema_resource_matches) + 1}")
            selectors = schema_selectors(schema)
            if not selectors:
                add_gap(self.gaps, "schema-selector-missing", "warning", "schema",
                        f"Resource schema {schema_id} has no selectors; it cannot be joined.", "source-schema.json",
                        schemaId=schema_id)
                continue
            for resource in self.resources:
                matched_selectors = []
                for sel in selectors:
                    rr = compile_regex(sel.get("resourceNameRegex"))
                    pr = compile_regex(sel.get("passNameRegex"))
                    if rr is None:
                        continue
                    if not (rr.search(resource["name"]) or rr.search(resource["uniqueName"])):
                        continue
                    pass_ids = resource.get("producers", []) + resource.get("consumers", [])
                    if pr is not None:
                        names = [p["name"] for p in self.passes if p["id"] in pass_ids]
                        if not any(pr.search(name) for name in names):
                            continue
                    matched_selectors.append({
                        "resourceNameRegex": sel.get("resourceNameRegex"),
                        "passNameRegex": sel.get("passNameRegex"),
                    })
                if matched_selectors:
                    summary = field_support_summary(schema)
                    storage = schema.get("storage")
                    format_match = storage_format_match(storage, resource.get("format"))
                    declared_format = storage_declared_format(storage)
                    declared_format_text = storage_format_text(storage)
                    format_proof = "explicit-pf" if declared_format else ("descriptive-only" if declared_format_text else "none")
                    if resource.get("kind") != "texture":
                        # Buffers have no packed pixel format.  Their element
                        # layout is proven by the recorded element stride and
                        # explicit field offsets, not by storage.format prose.
                        stride = storage.get("strideBytes") if isinstance(storage, dict) else None
                        format_proof = ("explicit-element-stride"
                                        if stride is not None and resource.get("bytesPerElement") is not None
                                        and int(stride) == int(resource["bytesPerElement"])
                                        else "descriptive-element-layout")
                    # Buffer schemas use storage.format for the element type or
                    # typed-buffer format; they have no runtime pixel format.
                    # Only a captured texture's PF_* format gates field reads.
                    gate_format = (declared_format
                                   if resource.get("kind") == "texture"
                                   and declared_format else None)
                    dump_format = resource.get("decode", {}).get("dumpFormat")
                    dump_format_match = formats_equivalent(dump_format, declared_format) if dump_format and declared_format else None
                    # Field offsets are read from the bytes DumpGPU saved, so the
                    # exported payload format is the runtime authority.  The
                    # descriptor format remains separately visible as actualFormat.
                    payload_format = dump_format or resource.get("format")
                    payload_format_match = (
                        formats_equivalent(payload_format, gate_format)
                        if payload_format and gate_format else None
                    )
                    variant = active_format_variant(storage, payload_format)
                    variant_fields = format_variant_fields(variant)
                    schema_fields = schema.get("fields") if isinstance(schema.get("fields"), list) else []
                    gated_fields = [
                        str(field.get("name") or f"fields[{index}]")
                        for index, field in enumerate(schema_fields)
                        if isinstance(field, dict) and not field_applies_to_format(field, payload_format, gate_format)
                    ]
                    if gated_fields:
                        summary = dict(summary)
                        summary["formatGatedFields"] = gated_fields
                    if resource.get("kind") == "texture":
                        # A numeric field offset is only applied when the packed
                        # storage layout is proven: a machine-readable PF_*
                        # format that matches the captured stored format, or an
                        # explicit formatVariant selected by that runtime format.
                        primary_extractable = bool(gate_format) and payload_format_match is True
                        extraction_eligible = primary_extractable or variant is not None
                        extraction_reason = None
                        if not extraction_eligible:
                            extraction_reason = (
                                "Schema storage.format is descriptive source prose "
                                f"({declared_format_text!r}) with no machine-readable PF_* tag, and no "
                                "formatVariant matches the captured stored format. Field offsets are retained "
                                "for display but numeric extraction is disabled."
                            ) if format_proof == "descriptive-only" else (
                                f"Captured stored format {payload_format!r} does not prove the schema layout "
                                f"{declared_format!r}; numeric extraction is disabled."
                            )
                    else:
                        primary_extractable = True
                        extraction_eligible = True
                        extraction_reason = None
                    match = {
                        "schemaId": schema_id,
                        "method": "explicit-selector",
                        "selectors": matched_selectors,
                        "quantity": schema.get("quantity"),
                        "units": schema.get("units"),
                        "coordinateSpace": schema.get("coordinateSpace"),
                        "storage": storage,
                        "actualFormat": resource.get("format"),
                        "declaredFormat": declared_format,
                        "declaredFormatText": declared_format_text,
                        "formatProof": format_proof,
                        "gateFormat": gate_format,
                        "formatAlternatives": storage_alternatives(storage),
                        "dumpFormat": dump_format,
                        "payloadFormat": payload_format,
                        "formatMatch": format_match,
                        "dumpFormatMatchDeclared": dump_format_match,
                        "payloadFormatMatch": payload_format_match,
                        "activeFormatVariant": variant.get("format") if variant else None,
                        "formatVariantFields": variant_fields,
                        "formatGatedFields": gated_fields,
                        "primaryFieldsExtractable": primary_extractable,
                        "extractionEligible": extraction_eligible,
                        "extractionEligibleReason": extraction_reason,
                        "decodingStatus": schema.get("decodingStatus"),
                        "transforms": schema.get("transforms"),
                        "mathTerms": schema.get("mathTerms"),
                        "producerSources": schema.get("producerSources"),
                        "consumerSources": schema.get("consumerSources"),
                        "gaps": schema.get("gaps"),
                        "fields": schema_fields,
                        "fieldSupport": summary,
                    }
                    self.schema_resource_matches.setdefault(resource["id"], []).append(match)
                    if resource.get("kind") == "texture" and format_proof == "descriptive-only" and variant is None:
                        add_gap(self.gaps, "resource-schema-format-proof-missing", "warning", "decoding",
                                f"Schema {schema_id} has a descriptive storage format ({declared_format_text!r}) but no machine-readable PF_* layout. Its fields remain visible; numeric extraction is disabled because the packed layout is unproven. The raw runtime-format texel inspector remains available.",
                                resource.get("nativePath"), resourceId=resource["id"], schemaId=schema_id)
                    elif format_match is False and variant is None and resource.get("kind") == "texture":
                        add_gap(self.gaps, "resource-schema-format-mismatch", "warning", "decoding",
                                f"Schema {schema_id} declares {match['declaredFormat']!r} for {resource['name']}, but the captured descriptor says {resource.get('format')!r}. Field offsets are shown but extraction is disabled.",
                                resource.get("nativePath"), resourceId=resource["id"], schemaId=schema_id)
                    if dump_format_match is False and variant is None:
                        add_gap(self.gaps, "resource-schema-dump-layout-mismatch", "warning", "decoding",
                                f"Schema {schema_id} describes {declared_format!r}, but DumpGPU saved {resource['name']} as {dump_format!r}. Schema field offsets are not applied to the converted payload; raw bytes and native metadata remain authoritative.",
                                resource.get("nativePath"), resourceId=resource["id"], schemaId=schema_id)
        for resource in self.resources:
            matches = self.schema_resource_matches.get(resource["id"], [])
            resource["schemaMatches"] = matches
            if len(matches) == 0:
                resource["schemaStatus"] = "unmatched"
                add_gap(self.gaps, "resource-schema-unmatched", "info", "schema",
                        f"No source resource schema selector matched {resource['name']} ({resource['uniqueName']}). Raw metadata remains authoritative.",
                        resource.get("nativePath"), resourceId=resource["id"])
            elif len(matches) > 1:
                resource["schemaStatus"] = "ambiguous"
                add_gap(self.gaps, "resource-schema-ambiguous", "warning", "schema",
                        f"{len(matches)} resource schemas matched {resource['name']}; all are shown and none is silently selected.",
                        resource.get("nativePath"), resourceId=resource["id"],
                        schemaIds=[m["schemaId"] for m in matches])
            else:
                resource["schemaStatus"] = "matched"

    def _join_parameter_schemas(self, schemas: list[Any]) -> None:
        for schema in schemas:
            if not isinstance(schema, dict):
                continue
            schema_id = str(schema.get("id") or f"parameter-schema-{len(self.schema_pass_matches) + 1}")
            selectors = schema_selectors(schema)
            qualified = schema.get("qualifiedShader")
            struct_name = schema.get("structName")
            if not selectors and not qualified:
                add_gap(self.gaps, "parameter-schema-descriptive-only", "info", "schema",
                        f"Parameter schema {schema_id} has neither selectors nor a qualified shader name; it is retained as a descriptive shader-struct schema and is not joined to a captured pass. No pass parameter values are inferred from it.",
                        "source-schema.json", schemaId=schema_id)
                continue
            for pas in self.passes:
                matched = []
                method = None
                if selectors:
                    for sel in selectors:
                        pr = compile_regex(sel.get("passNameRegex"))
                        rr = compile_regex(sel.get("resourceNameRegex"))
                        if pr is None and rr is None:
                            continue
                        if pr is not None:
                            pass_names = [pas["name"], str(pas.get("parametersName") or "")]
                            if not any(pr.search(name) for name in pass_names if name):
                                continue
                        if rr is not None:
                            resource_names = []
                            for item in pas.get("inputs", []) + pas.get("outputs", []):
                                if item.get("resourceId"):
                                    res = next((r for r in self.resources if r["id"] == item["resourceId"]), None)
                                    if res:
                                        resource_names.extend([res["name"], res["uniqueName"]])
                            if not any(rr.search(name) for name in resource_names):
                                continue
                        matched.append({"resourceNameRegex": sel.get("resourceNameRegex"),
                                        "passNameRegex": sel.get("passNameRegex")})
                    method = "explicit-selector"
                else:
                    tokens = shader_core_tokens(qualified)
                    event_tokens = shader_core_tokens(pas["name"])
                    if tokens and event_tokens and (event_tokens[:len(tokens)] == tokens
                                                   or (tokens[-len(event_tokens):] == event_tokens and len(event_tokens) >= 2)):
                        matched.append({"qualifiedShader": qualified, "structName": struct_name,
                                        "passEvent": pas["name"]})
                        method = "qualified-shader-token-match"
                if matched:
                    summary = field_support_summary(schema, parameter=True)
                    self.schema_pass_matches.setdefault(pas["id"], []).append({
                        "schemaId": schema_id,
                        "method": method,
                        "selectors": matched,
                        "qualifiedShader": qualified,
                        "structName": struct_name,
                        "sourcePath": schema.get("sourcePath"),
                        "line": schema.get("line"),
                        "quantity": schema.get("quantity"),
                        "units": schema.get("units"),
                        "storage": schema.get("storage"),
                        "decodingStatus": schema.get("decodingStatus"),
                        "transforms": schema.get("transforms"),
                        "mathTerms": schema.get("mathTerms"),
                        "producerSources": schema.get("producerSources"),
                        "consumerSources": schema.get("consumerSources"),
                        "gaps": schema.get("gaps"),
                        "fields": schema.get("fields") if isinstance(schema.get("fields"), list) else [],
                        "fieldSupport": summary,
                    })
        for pas in self.passes:
            matches = self.schema_pass_matches.get(pas["id"], [])
            pas["schemaMatches"] = matches
            if len(matches) > 1:
                add_gap(self.gaps, "parameter-schema-ambiguous", "warning", "schema",
                        f"{len(matches)} parameter schemas matched pass {pas['name']}; all are shown and none is silently selected.",
                        "source-schema.json", passId=pas["id"],
                        schemaIds=[m["schemaId"] for m in matches])

    def _schema_field_gaps(self, schemas: list[Any], parameter: bool = False) -> None:
        for schema in schemas:
            if not isinstance(schema, dict):
                continue
            schema_id = str(schema.get("id") or "(schema without id)")
            summary = field_support_summary(schema, parameter=parameter)
            if summary["unsafeExpressionFields"]:
                add_gap(self.gaps, "schema-field-decode-unsafe-expression", "warning", "decoding",
                        f"Schema {schema_id} has {len(summary['unsafeExpressionFields'])} decode string(s) that look like executable expressions. They were never evaluated; the numeric type/offset is used only if explicitly known, and the text is shown as a source-linked equation.",
                        "source-schema.json", schemaId=schema_id,
                        fields=summary["unsafeExpressionFields"])
            if summary["unsupportedTypeFields"] and not parameter:
                add_gap(self.gaps, "schema-field-type-unsupported", "warning", "decoding",
                        f"Schema {schema_id} has {len(summary['unsupportedTypeFields'])} field type(s) without a numeric extractor. The field remains descriptive and raw bytes are retained.",
                        "source-schema.json", schemaId=schema_id,
                        fields=summary["unsupportedTypeFields"])
            if summary["missingOffsetFields"]:
                add_gap(self.gaps, "schema-field-offset-missing", "info", "decoding",
                        f"Schema {schema_id} has {len(summary['missingOffsetFields'])} field(s) without byteOffset/bitOffset; they remain descriptive only.",
                        "source-schema.json", schemaId=schema_id,
                        fields=summary["missingOffsetFields"])
            if summary["sourceEquationFields"]:
                add_gap(self.gaps, "schema-field-source-equation", "info", "decoding",
                        f"Schema {schema_id} has {len(summary['sourceEquationFields'])} field(s) whose decode is a source-linked equation, not a machine tag. The typed raw value is shown; the equation is displayed and is not applied by the explorer.",
                        "source-schema.json", schemaId=schema_id,
                        fields=summary["sourceEquationFields"][:20],
                        omittedFieldCount=max(0, len(summary["sourceEquationFields"]) - 20))
            if parameter and summary["fieldCount"]:
                add_gap(self.gaps, "parameter-schema-source-fields", "info", "schema",
                        f"Parameter schema {schema_id} has {summary['fieldCount']} source-level field(s) with no captured byte offsets. Native StructuresMetadata and the raw parameter block are authoritative for values; these fields provide source anchors and decode descriptions.",
                        "source-schema.json", schemaId=schema_id)

    # --------------------------------------------------------------- chunks
    def ingest_payloads(self) -> None:
        for payload in self.payloads:
            if not payload.abs_path.exists():
                add_gap(self.gaps, "payload-missing", "error", "payload",
                        f"Payload file is missing: {payload.original_path}", payload.original_path)
                continue
            self._ingest_payload(payload)
        for payload in self.payloads:
            # Find the native payload record by sourcePath and version-specific name.
            for record in self.native_records:
                if record["kind"] == "payload" and record["sourcePath"] == payload.original_path:
                    payload.native_record_id = record["id"]
                    record["bytes"] = payload.bytes
                    record["sha256"] = payload.sha256
                    record["payloadId"] = payload.id
                    break
            if payload.native_record_id is None:
                record = self._add_native_record("payload", payload.original_path, 0, payload.sha256 or "",
                                                 summary=f"{payload.role} payload")
                payload.native_record_id = record["id"]
                record["bytes"] = payload.bytes
                record["payloadId"] = payload.id
        self.raw_payload_bytes = sum(p.bytes for p in self.payloads)
        self.unique_raw_bytes = sum(c["rawLength"] for c in self.chunks.values())
        self.compressed_bytes = sum(c["compressedLength"] for c in self.chunks.values())

    def _ingest_payload(self, payload: Payload) -> None:
        h = hashlib.sha256()
        offset = 0
        refs: list[dict[str, Any]] = []
        try:
            with payload.abs_path.open("rb") as f:
                while True:
                    block = f.read(CHUNK_SIZE)
                    if not block:
                        break
                    h.update(block)
                    chunk_sha = sha256_bytes(block)
                    if chunk_sha not in self.chunks:
                        compressed = gzip.compress(block, compresslevel=self.gzip_level, mtime=0)
                        self.chunks[chunk_sha] = {
                            "sha": chunk_sha,
                            "rawLength": len(block),
                            "compressedLength": len(compressed),
                            "file": f"chunks/{chunk_sha}.gz",
                            "gzip": compressed,
                        }
                        if not self.estimate and self.output_root is not None:
                            chunk_path = self.output_root / "chunks" / f"{chunk_sha}.gz"
                            with chunk_path.open("wb") as out:
                                out.write(compressed)
                            self.chunks[chunk_sha]["gzip"] = None
                    refs.append({"sha": chunk_sha, "offset": offset, "length": len(block)})
                    offset += len(block)
        except OSError as exc:
            add_gap(self.gaps, "payload-read-error", "error", "payload",
                    f"Could not read payload: {exc}", payload.original_path)
            return
        payload.bytes = offset
        payload.sha256 = h.hexdigest()
        payload.chunks = refs

    def chunks_public(self) -> list[dict[str, Any]]:
        out = []
        for chunk in self.chunks.values():
            out.append({
                "sha": chunk["sha"],
                "rawLength": chunk["rawLength"],
                "compressedLength": chunk["compressedLength"],
                "file": chunk["file"],
            })
        return sorted(out, key=lambda c: c["sha"])

    # --------------------------------------------------------------- package
    def finalize(self) -> None:
        for index, gap in enumerate(self.gaps, 1):
            gap["id"] = f"gap-{index:04d}"
        # Add native payload records for any .bin that could not be linked.
        for spec in self._payload_specs:
            if not any(p.original_path == spec["rel"] for p in self.payloads):
                payload = Payload(id=f"payload-{len(self.payloads) + 1:05d}", original_path=spec["rel"],
                                  abs_path=spec["abs"], role="orphan")
                self.payloads.append(payload)
                self.payload_by_id[payload.id] = payload
                add_gap(self.gaps, "orphan-payload", "warning", "payload",
                        f"Raw binary is not referenced by any capture record: {spec['rel']}", spec["rel"])
        # Ingest any late orphan payloads.
        for payload in self.payloads:
            if not payload.sha256 and payload.abs_path.exists():
                self._ingest_payload(payload)
        # The pass-side uniform payload snapshot is attached while sidecars are
        # indexed, before ingest computes bytes/hashes/chunks.  Re-resolve every
        # non-null snapshot by payload id now that ingest is final.  A null
        # snapshot means no captured uniform block exists and must stay null.
        self._refresh_uniform_payloads()
        for payload in self.payloads:
            if payload.native_record_id is None:
                record = self._add_native_record("payload", payload.original_path, 0, payload.sha256 or "",
                                                 summary=f"{payload.role} payload")
                payload.native_record_id = record["id"]
                record["bytes"] = payload.bytes
                record["payloadId"] = payload.id
        self.raw_payload_bytes = sum(p.bytes for p in self.payloads)
        self.unique_raw_bytes = sum(c["rawLength"] for c in self.chunks.values())
        self.compressed_bytes = sum(c["compressedLength"] for c in self.chunks.values())

        for resource in self.resources:
            resource["payloadCount"] = len(resource["payloadIds"])
            resource["payloadBytes"] = sum(self.payload_by_id[pid].bytes for pid in resource["payloadIds"] if pid in self.payload_by_id)
            resource["schemaStatus"] = resource.get("schemaStatus") or ("unmatched" if self.schema_status != "joined" else "unmatched")
            resource["searchText"] = self._resource_search_text(resource)
        for pas in self.passes:
            payload = self.payload_by_id.get((pas.get("parameterBlock") or {}).get("payloadId") or "")
            if payload:
                pas["parameterBlock"]["bytes"] = payload.bytes
                pas["parameterBlock"]["sha256"] = payload.sha256
                pas["parameterBlock"]["chunks"] = payload.chunks
            pas["searchText"] = self._pass_search_text(pas)
        if self.adapter == "normalized":
            for pas in self.passes:
                if not pas.get("parameterBlock"):
                    add_gap(self.gaps, "pass-parameter-block-missing", "warning", "pass",
                            f"Pass {pas['name']} has no normalized parameter block; metadata members, if any, are shown without values.",
                            "normalized-frame.json", passId=pas["id"])
        if not self.payloads:
            add_gap(self.gaps, "no-raw-payloads", "error", "capture",
                    "No raw .bin payloads were found. The explorer will show metadata only; no bytes are synthesized.",
                    "Resources/")
        if self.fixture:
            self.evidence_kind = "synthetic-fixture"
            self.notice = "LABELLED SYNTHETIC FIXTURE: this package demonstrates the explorer and packer; it is not runtime evidence."
        elif not self.payloads:
            self.evidence_kind = "metadata-only"
            self.notice = "No raw payloads were available. The package is metadata-only and explicitly incomplete."
        else:
            self.evidence_kind = "native-capture"
            self.notice = "Captured native metadata and raw payload chunks. Coverage and gaps are reported explicitly."

    def _refresh_uniform_payloads(self) -> None:
        """Replace pre-ingest uniform payload snapshots with final payload records.

        The pass attachment runs during adapter loading, before payload bytes,
        hashes and chunks exist.  Resolving by the recorded payload id (with the
        uniform-buffer pointer as a fallback) keeps the attached record exactly
        equal to the inventory payload; an intentionally unbound record stays
        null rather than being filled or fabricated.
        """
        for pas in self.passes:
            for native in pas.get("nativeUniformBuffers", []) or []:
                current = native.get("uniformBufferPayload")
                if not isinstance(current, dict):
                    continue
                payload = self.payload_by_id.get(str(current.get("id") or ""))
                if payload is None:
                    pointer = str(native.get("uniformBuffer") or "")
                    payload = next((p for p in self.payloads if p.role == "parameter" and p.pointer == pointer), None)
                if payload is not None:
                    native["uniformBufferPayload"] = self.payload_public(payload)

    def _resource_search_text(self, resource: dict[str, Any]) -> str:
        parts = [resource["name"], resource["uniqueName"], resource["kind"], str(resource.get("format") or "")]
        for pass_id in resource.get("producers", []) + resource.get("consumers", []):
            pas = next((p for p in self.passes if p["id"] == pass_id), None)
            if pas:
                parts.append(pas["name"])
        for match in resource.get("schemaMatches", []):
            parts.extend([str(match.get("schemaId")), str(match.get("quantity")), str(match.get("units")),
                          str(match.get("coordinateSpace")), str(match.get("storage")),
                          json.dumps(match.get("mathTerms"), ensure_ascii=False)])
            for fld in match.get("fields", []):
                if isinstance(fld, dict):
                    parts.append(str(fld.get("name") or ""))
                    parts.append(str(fld.get("decode") or ""))
        for gap in self.gaps:
            if gap.get("resourceId") == resource["id"]:
                parts.append(gap["kind"])
                parts.append(gap["message"])
        for item in resource.get("subresourceLayouts", []):
            layout = item.get("layout") or {}
            parts.extend([str(layout.get("Dimension") or ""), str(layout.get("StoredFormat") or ""),
                          str(layout.get("NativeFormat") or ""), str(layout.get("ViewFormat") or ""),
                          str(layout.get("Name") or "")])
        for binding in resource.get("nativeUniformBindings", []):
            parts.extend([str(binding.get("memberPath") or ""), str(binding.get("layoutName") or "")])
        return " ".join(p for p in parts if p).lower()

    def _pass_search_text(self, pas: dict[str, Any]) -> str:
        parts = [pas["name"], str(pas.get("parametersName") or ""), str(pas.get("pointer") or "")]
        parts.extend(str(s) for s in pas.get("parentScopes", []))
        for item in pas.get("inputs", []) + pas.get("outputs", []):
            parts.append(str(item.get("uniqueName")))
            unique_name = str(item.get("uniqueName", ""))
            resource = self.resource_by_unique.get(unique_name)
            if resource is None:
                resource = next((r for u, r in self.resource_by_unique.items()
                                 if unique_name.startswith(u + ".") or unique_name.startswith(u + "[")), None)
            if resource:
                parts.extend([resource["name"], resource["uniqueName"], str(resource.get("format") or "")])
        for match in pas.get("schemaMatches", []):
            parts.extend([str(match.get("schemaId")), str(match.get("quantity")), str(match.get("units")),
                          json.dumps(match.get("mathTerms"), ensure_ascii=False)])
            for fld in match.get("fields", []):
                if isinstance(fld, dict):
                    parts.append(str(fld.get("name") or ""))
        meta_ptr = str(pas.get("parametersMetadataPointer") or "")
        meta = self.structures.get(meta_ptr)
        if isinstance(meta, dict):
            for member in meta.get("Members", []) if isinstance(meta.get("Members"), list) else []:
                if isinstance(member, dict):
                    parts.extend([str(member.get("Name") or ""), str(member.get("ShaderType") or "")])
        for gap in self.gaps:
            if gap.get("passId") == pas["id"]:
                parts.append(gap["kind"])
                parts.append(gap["message"])
        for record in pas.get("nativeUniformBuffers", []):
            parts.extend([str(record.get("layoutName") or ""), str(record.get("uniformBuffer") or "")])
            parts.extend(str(key) for key in (record.get("bindings") or {}))
        for record in pas.get("parameterBindings", []):
            parts.extend(str(key) for key in (record.get("Bindings") or {}))
        return " ".join(p for p in parts if p).lower()

    def counts(self) -> dict[str, Any]:
        # Decode warnings are counted once per source-schema field, not once per
        # matched resource/pass instance.  The review's 129 source-equation
        # fields are unique resource-schema fields (parameter schemas add 128).
        resource_equations = 0
        parameter_equations = 0
        unsafe_expressions = 0
        if isinstance(self.schema_doc, dict):
            for schema in self.schema_doc.get("resourceSchemas", []) or []:
                if not isinstance(schema, dict):
                    continue
                support = field_support_summary(schema, parameter=False)
                resource_equations += len(support["sourceEquationFields"])
                unsafe_expressions += len(support["unsafeExpressionFields"])
            for schema in self.schema_doc.get("parameterSchemas", []) or []:
                if not isinstance(schema, dict):
                    continue
                support = field_support_summary(schema, parameter=True)
                parameter_equations += len(support["sourceEquationFields"])
                unsafe_expressions += len(support["unsafeExpressionFields"])
        return {
            "nativeRecords": len(self.native_records),
            "nativeRecordKinds": dict(sorted(self.records_by_kind.items())),
            "nativeFiles": len(self.native_files),
            "resourceRecords": sum(1 for r in self.native_records if r["kind"] == "resource-descriptor"),
            "passRecords": sum(1 for r in self.native_records if r["kind"] == "pass"),
            "resources": len(self.resources),
            "passes": len(self.passes),
            "parameterBlocks": sum(1 for p in self.payloads if p.role == "parameter"),
            "uniformStructures": len(self.structures),
            "payloadFiles": len(self.payloads),
            "payloadBytes": self.raw_payload_bytes,
            "uniqueChunkBytes": self.unique_raw_bytes,
            "compressedChunkBytes": self.compressed_bytes,
            "chunkFiles": len(self.chunks),
            "resourcesWithPayload": sum(1 for r in self.resources if r["payloadIds"]),
            "resourcesMissingPayload": sum(1 for r in self.resources if not r["payloadIds"]),
            "unsupportedResourceFormats": sum(1 for r in self.resources if r["kind"] == "texture" and not r["decode"].get("supported", False)),
            "schemaMatchedResources": sum(1 for r in self.resources if r.get("schemaStatus") == "matched"),
            "schemaAmbiguousResources": sum(1 for r in self.resources if r.get("schemaStatus") == "ambiguous"),
            "schemaUnmatchedResources": sum(1 for r in self.resources if r.get("schemaStatus") == "unmatched"),
            "passesWithParameterBlock": sum(1 for p in self.passes if p.get("parameterBlock")),
            "passesMissingParameterBlock": sum(1 for p in self.passes if not p.get("parameterBlock")),
            "passDrawCountsCaptured": sum(1 for p in self.passes if p.get("drawCount") is not None),
            "passDrawCountsMissing": sum(1 for p in self.passes if p.get("drawCount") is None),
            "resourceSourceEquationFields": resource_equations,
            "parameterSourceEquationFields": parameter_equations,
            "unsafeDecodeExpressionFields": unsafe_expressions,
            "schemaMatchedPasses": sum(1 for p in self.passes if p.get("schemaMatches")),
            "gapCount": len(self.gaps),
            "subresourceLayouts": len(self.subresource_layouts),
            "readbackStatusRecords": len(self.readback_status),
            "nativeUniformBufferRecords": len(self.native_uniform_records),
            "parameterBindingRecords": len(self.parameter_binding_records),
            "referencedStructRecords": len(self.referenced_structs),
            "nativeInputResources": len(self.native_resources),
            "textureReadbackPayloads": sum(1 for p in self.payloads if p.classification == "texture-readback-subresource"),
            "nativeBufferSubranges": sum(1 for p in self.payloads if p.classification == "native-buffer-srv-subrange"),
            "skipTrackingPayloads": sum(1 for p in self.payloads if p.classification == "skip-tracking-buffer"),
            "rdgBufferVersionsWithoutCompletion": sum(1 for p in self.payloads if p.classification == "rdg-buffer-version"),
            "volumePayloads": sum(1 for p in self.payloads if (p.layout or {}).get("Dimension") == "Texture3D"),
            "cubeFacePayloads": sum(1 for p in self.payloads if (p.layout or {}).get("Dimension") == "TextureCube"),
            "arrayPayloads": sum(1 for p in self.payloads if (p.layout or {}).get("Dimension") == "Texture2DArray"),
            "convertedStoragePayloads": sum(1 for p in self.payloads if p.layout and p.layout.get("NativeFormat") != p.layout.get("StoredFormat")),
            "bitExactPayloads": sum(1 for p in self.payloads if p.layout and p.layout.get("BitExact") is True),
            "valuePreservingPayloads": sum(1 for p in self.payloads if p.layout and p.layout.get("ValuePreserving") is True),
            "nonValuePreservingPayloads": sum(1 for p in self.payloads if p.layout and p.layout.get("ValuePreserving") is False),
            "nativeBindingsExposed": sum(1 for r in self.passes for n in r.get("nativeUniformBuffers", []) for _ in (n.get("bindings") or {})),
            "nativeBindingsWithBytes": sum(1 for r in self.passes for n in r.get("nativeUniformBuffers", []) for b in (n.get("bindings") or {}).values() if isinstance(b, dict) and b.get("payloadIds")),
            "nativeBindingsWithoutBytes": sum(1 for r in self.passes for n in r.get("nativeUniformBuffers", []) for b in (n.get("bindings") or {}).values() if isinstance(b, dict) and not b.get("payloadIds")),
            "reusedNativeBindings": sum(1 for r in self.passes for n in r.get("nativeUniformBuffers", []) for b in (n.get("bindings") or {}).values() if isinstance(b, dict) and b.get("ReusedScheduledReadback") is True),
            "ownerPassExecutionNotClaimed": sum(1 for r in self.passes for n in r.get("nativeUniformBuffers", []) if n.get("ownerPassExecutionDependencyClaimed") is False),
        }

    def index_document(self, package_files: int = 0, largest: dict[str, Any] | None = None) -> dict[str, Any]:
        counts = self.counts()
        counts["packageFiles"] = package_files
        counts["largestOutputBytes"] = largest["bytes"] if largest else None
        schema_doc = self.schema_doc if isinstance(self.schema_doc, dict) else {}
        schema_resources = schema_doc.get("resourceSchemas") if isinstance(schema_doc.get("resourceSchemas"), list) else []
        schema_parameters = schema_doc.get("parameterSchemas") if isinstance(schema_doc.get("parameterSchemas"), list) else []
        source_refs = schema_doc.get("sourceRefs")
        schema_info = {
            "status": self.schema_status,
            # Package-relative path to the embedded schema; sourcePath records
            # where the supplied schema came from without pretending it is a
            # package file.
            "path": "schema.json",
            "sourcePath": self.schema_input_path,
            "sha256": self.schema_sha256,
            "bytes": self.schema_bytes,
            "schemaVersion": schema_doc.get("schemaVersion"),
            "resourceSchemas": len(schema_resources),
            "parameterSchemas": len(schema_parameters),
            "sourceRefs": len(source_refs) if isinstance(source_refs, (list, dict)) else 0,
            "samplerBindings": len(schema_doc.get("samplerBindings")) if isinstance(schema_doc.get("samplerBindings"), list) else 0,
            "coverageGaps": len(schema_doc.get("coverageGaps")) if isinstance(schema_doc.get("coverageGaps"), list) else 0,
            "parts": len(schema_doc.get("parts")) if isinstance(schema_doc.get("parts"), list) else 0,
            "formatVariants": sum(
                len(storage_format_variants(schema.get("storage")))
                for schema in schema_resources if isinstance(schema, dict)
            ),
            "formatGatedFields": sum(
                1 for schema in schema_resources if isinstance(schema, dict)
                for field in (schema.get("fields") if isinstance(schema.get("fields"), list) else [])
                if isinstance(field, dict) and field.get("appliesToFormats")
            ),
        }
        return {
            "format": PACKAGE_FORMAT,
            "version": PACKAGE_VERSION,
            "generatedUtc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "adapter": self.adapter,
            "fixture": self.fixture,
            "evidence": {
                "kind": getattr(self, "evidence_kind", "unknown"),
                "notice": getattr(self, "notice", ""),
            },
            "source": {
                "entry": self.dump_root.name,
                "nativeFileCount": len(self.native_files),
                "payloadFileCount": len(self.payloads),
            },
            "schema": schema_info,
            "counts": counts,
            "viewports": self._viewport_summary(),
            "coverage": {
                "complete": len(self.gaps) == 0,
                "statement": self._coverage_statement(),
            },
            "files": {
                "index": "index.json",
                "resources": "resources.json",
                "passes": "passes.json",
                "coverage": "coverage.json",
                "schema": "schema.json",
                "structures": "structures.json",
                "capture": "capture.json",
                "inventory": "inventory.json",
                "nativeInputs": "native-inputs.json",
                "chunks": "chunks/",
                "records": "records/",
                "native": "native/",
                "reconstruct": "Use tools/package_frame.py reconstruct --package <this directory> --dest <dir>",
            },
            "chunks": self.chunks_public(),
            "largestOutput": largest,
        }

    def _coverage_statement(self) -> str:
        counts = self.counts()
        if self.fixture:
            prefix = "Synthetic fixture, not runtime evidence. "
        elif not self.payloads:
            prefix = "Metadata-only package with no raw payloads. "
        else:
            prefix = ""
        return (
            f"{prefix}Coverage is not claimed complete. {counts['resources']} resources, "
            f"{counts['passes']} passes ({counts['passDrawCountsCaptured']} with a captured draw count; "
            f"{counts['passDrawCountsMissing']} explicitly not captured), "
            f"{counts['parameterBlocks']} parameter payloads and "
            f"{counts['payloadFiles']} raw payload files were inventoried; "
            f"{counts['gapCount']} explicit gaps are listed in coverage.json. "
            "Representative previews never imply complete resource coverage."
        )

    def resources_document(self) -> dict[str, Any]:
        items = []
        for r in self.resources:
            items.append({
                "id": r["id"], "name": r["name"], "uniqueName": r["uniqueName"], "kind": r["kind"],
                "displayLabel": r.get("displayLabel", r["name"]),
                "sceneInvocation": r.get("sceneInvocation"),
                "instancePrefix": r.get("instancePrefix"),
                "frameNumber": r.get("frameNumber"),
                "passInterval": r.get("passInterval"),
                "allocation": r.get("allocation"),
                "viewState": r.get("viewState"),
                "viewport": r.get("viewport"),
                "padding": r.get("padding"),
                "format": r.get("format"), "bytes": r.get("bytes"),
                "extent": r.get("extent"), "arraySize": r.get("arraySize"), "numMips": r.get("numMips"),
                "payloadCount": r.get("payloadCount", 0), "payloadBytes": r.get("payloadBytes", 0),
                "versionCount": len(r.get("versions", [])),
                "producerPassIds": r.get("producers", []), "consumerPassIds": r.get("consumers", []),
                "schemaStatus": r.get("schemaStatus", "unmatched"),
                "decodeSupported": bool(r.get("decode", {}).get("supported")),
                "hasSubresourceLayouts": bool(r.get("subresourceLayouts")),
                "subresourceLayoutCount": len(r.get("subresourceLayouts", [])),
                "nativeBindingCount": len(r.get("nativeUniformBindings", [])),
                "hasUnsafeDecodeExpression": any((m.get("fieldSupport", {}).get("unsafeExpressionFields") or []) for m in r.get("schemaMatches", [])),
                "hasSourceEquationFields": any((m.get("fieldSupport", {}).get("sourceEquationFields") or []) for m in r.get("schemaMatches", [])),
                "sourceEquationFieldCount": sum(len((m.get("fieldSupport", {}).get("sourceEquationFields") or [])) for m in r.get("schemaMatches", [])),
                "gapCount": sum(1 for g in self.gaps if g.get("resourceId") == r["id"]),
                "searchText": r.get("searchText", "")
                    + " " + str(r.get("displayLabel") or "")
                    + (" scene " + str(r.get("sceneInvocation")) if r.get("sceneInvocation") else "")
                    + (" frame " + str(r.get("frameNumber")) if r.get("frameNumber") is not None else ""),
                "record": f"records/resource/{r['id']}.json",
            })
        return {"format": f"{PACKAGE_FORMAT}-resources", "version": PACKAGE_VERSION,
                "total": len(items), "items": items}

    def passes_document(self) -> dict[str, Any]:
        items = []
        for p in self.passes:
            items.append({
                "id": p["id"], "name": p["name"], "displayLabel": p.get("displayLabel", p["name"]),
                "pointer": p.get("pointer"),
                "frameNumber": p.get("frameNumber"), "graphPrefix": p.get("graphPrefix"),
                "viewState": p.get("viewState"),
                "parametersName": p.get("parametersName"),
                "drawCount": p.get("drawCount"), "drawCountStatus": p.get("drawCountStatus", "not-captured"),
                "inputCount": len(p.get("inputs", [])), "outputCount": len(p.get("outputs", [])),
                "hasParameterBlock": bool(p.get("parameterBlock")),
                "parameterBlockBytes": ((p.get("parameterBlock") or {}).get("bytes") or 0),
                "nativeUniformBufferCount": len(p.get("nativeUniformBuffers", [])),
                "parameterBindingCount": len(p.get("parameterBindings", [])),
                "schemaMatchCount": len(p.get("schemaMatches", [])),
                "schemaStatus": "ambiguous" if len(p.get("schemaMatches", [])) > 1 else ("matched" if p.get("schemaMatches") else "unmatched"),
                "hasUnsafeDecodeExpression": any((m.get("fieldSupport", {}).get("unsafeExpressionFields") or []) for m in p.get("schemaMatches", [])),
                "hasSourceEquationFields": any((m.get("fieldSupport", {}).get("sourceEquationFields") or []) for m in p.get("schemaMatches", [])),
                "gapCount": sum(1 for g in self.gaps if g.get("passId") == p["id"]),
                "searchText": p.get("searchText", "") + " " + str(p.get("displayLabel") or ""),
                "record": f"records/pass/{p['id']}.json",
            })
        return {"format": f"{PACKAGE_FORMAT}-passes", "version": PACKAGE_VERSION,
                "total": len(items), "items": items}

    def resource_record(self, resource: dict[str, Any]) -> dict[str, Any]:
        payloads = []
        for pid in resource.get("payloadIds", []):
            payload = self.payload_by_id.get(pid)
            if not payload:
                continue
            payloads.append(self.payload_public(payload))
        return {
            "format": f"{PACKAGE_FORMAT}-resource-record",
            "version": PACKAGE_VERSION,
            "id": resource["id"],
            "nativeRecordId": resource.get("nativeRecordId"),
            "nativePath": resource.get("nativePath"),
            "name": resource["name"],
            "uniqueName": resource["uniqueName"],
            "displayLabel": resource.get("displayLabel", resource["name"]),
            "instancePrefix": resource.get("instancePrefix"),
            "sceneInvocation": resource.get("sceneInvocation"),
            "frameNumber": resource.get("frameNumber"),
            "passInterval": resource.get("passInterval"),
            "allocation": resource.get("allocation"),
            "viewState": resource.get("viewState"),
            "viewport": resource.get("viewport"),
            "padding": resource.get("padding"),
            "kind": resource["kind"],
            "format": resource.get("format"),
            "bytes": resource.get("bytes"),
            "extent": resource.get("extent"),
            "arraySize": resource.get("arraySize"),
            "numMips": resource.get("numMips"),
            "numSamples": resource.get("numSamples"),
            "bytesPerElement": resource.get("bytesPerElement"),
            "numElements": resource.get("numElements"),
            "metadataPointer": resource.get("metadataPointer"),
            "decode": resource.get("decode"),
            "bufferMetadata": resource.get("bufferMetadata"),
            "native": resource.get("native"),
            "payloads": payloads,
            "versions": resource.get("versions", []),
            "bindings": {
                "producers": [self.pass_ref(pid) for pid in resource.get("producers", [])],
                "consumers": [self.pass_ref(pid) for pid in resource.get("consumers", [])],
            },
            "subresourceLayouts": resource.get("subresourceLayouts", []),
            "completion": resource.get("completion", []),
            "nativeUniformBindings": resource.get("nativeUniformBindings", []),
            "schemaMatches": resource.get("schemaMatches", []),
            "gaps": [g for g in self.gaps if g.get("resourceId") == resource["id"]],
        }

    def pass_record(self, pas: dict[str, Any]) -> dict[str, Any]:
        param = dict(pas.get("parameterBlock") or {}) if pas.get("parameterBlock") else None
        if param:
            payload = self.payload_by_id.get(param.get("payloadId") or "")
            if payload:
                param.update({
                    "payloadId": payload.id,
                    "originalPath": payload.original_path,
                    "bytes": payload.bytes,
                    "sha256": payload.sha256,
                    "chunks": payload.chunks,
                })
        metadata = self.structures.get(str(pas.get("parametersMetadataPointer") or ""))
        refs = []
        for ref in pas.get("referencedStructs", []):
            item = dict(ref)
            if item.get("payloadId") and item["payloadId"] in self.payload_by_id:
                p = self.payload_by_id[item["payloadId"]]
                item["payload"] = self.payload_public(p)
            item["metadata"] = self.structures.get(str(item.get("metadataPointer") or ""))
            refs.append(item)
        return {
            "format": f"{PACKAGE_FORMAT}-pass-record",
            "version": PACKAGE_VERSION,
            "id": pas["id"],
            "nativeRecordId": pas.get("nativeRecordId"),
            "name": pas["name"],
            "displayLabel": pas.get("displayLabel", pas["name"]),
            "frameNumber": pas.get("frameNumber"),
            "graphPrefix": pas.get("graphPrefix"),
            "viewState": pas.get("viewState"),
            "pointer": pas.get("pointer"),
            "parametersName": pas.get("parametersName"),
            "parametersPointer": pas.get("parametersPointer"),
            "parametersMetadataPointer": pas.get("parametersMetadataPointer"),
            "parentScopes": pas.get("parentScopes", []),
            "drawCount": pas.get("drawCount"),
            "drawCountStatus": pas.get("drawCountStatus", "not-captured"),
            "drawCountSource": pas.get("drawCountSource", "Base/PassDrawCounts.json"),
            "native": pas.get("native"),
            "inputs": pas.get("inputs", []),
            "outputs": pas.get("outputs", []),
            "parameterBlock": param,
            "parameterMetadata": metadata,
            "referencedStructs": refs,
            "nativeUniformBuffers": pas.get("nativeUniformBuffers", []),
            "parameterBindings": pas.get("parameterBindings", []),
            "schemaMatches": pas.get("schemaMatches", []),
            "gaps": [g for g in self.gaps if g.get("passId") == pas["id"]],
        }

    def payload_public(self, payload: Payload) -> dict[str, Any]:
        return {
            "id": payload.id,
            "role": payload.role,
            "originalPath": payload.original_path,
            "bytes": payload.bytes,
            "sha256": payload.sha256,
            "version": payload.version,
            "mip": payload.mip,
            "slice": payload.slice,
            "face": payload.face,
            "stencil": payload.stencil,
            "resourceId": payload.resource_id,
            "passId": payload.pass_id,
            "chunks": payload.chunks,
            "nativeKind": payload.native_kind,
            "classification": payload.classification,
            "layout": payload.layout,
            "completion": payload.completion,
            "identity": payload.identity,
            "diagnosticResource": payload.diagnostic_resource,
            "subresource": payload.subresource,
            "decode": payload.decode,
        }

    def pass_ref(self, pass_id: str) -> dict[str, Any] | None:
        pas = next((p for p in self.passes if p["id"] == pass_id), None)
        if not pas:
            return None
        return {"id": pas["id"], "name": pas["name"], "displayLabel": pas.get("displayLabel", pas["name"]),
                "record": f"records/pass/{pas['id']}.json"}

    def coverage_categories(self) -> list[dict[str, Any]]:
        """Explicit, non-overlapping coverage categories required by the export contract.

        Each category names the exact evidence state rather than collapsing every
        limitation into one "incomplete" flag.
        """
        unsupported = []
        for resource in self.resources:
            if resource.get("kind") != "texture":
                continue
            payloads = [self.payload_by_id[pid] for pid in resource.get("payloadIds", []) if pid in self.payload_by_id]
            if payloads:
                bad = [p for p in payloads if p.decode is not None and not p.decode.get("supported")]
                if bad:
                    unsupported.append({
                        "resourceId": resource["id"], "name": resource["name"],
                        "formats": sorted({(p.layout or {}).get("StoredFormat") for p in bad}),
                    })
            elif not resource["decode"].get("supported"):
                unsupported.append({"resourceId": resource["id"], "name": resource["name"],
                                    "formats": [resource.get("format")]})
        missing_bytes = []
        for resource in self.resources:
            if not resource.get("payloadIds"):
                missing_bytes.append({"resourceId": resource["id"], "name": resource["name"],
                                      "reason": "no captured payload"})
                continue
            if resource.get("kind") == "texture":
                for item in resource.get("subresourceLayouts", []):
                    comp = item.get("completion") or {}
                    if comp.get("observed") and not comp.get("dumped"):
                        missing_bytes.append({"resourceId": resource["id"], "name": resource["name"],
                                              "reason": comp.get("reason") or "readback not completed",
                                              "file": item.get("file")})
        timing = []
        for pas in self.passes:
            for record in pas.get("nativeUniformBuffers", []):
                if record.get("ownerPassExecutionDependencyClaimed") is False:
                    timing.append({
                        "passId": pas["id"], "passName": pas["name"],
                        "uniformBuffer": record.get("uniformBuffer"),
                        "timingRisk": record.get("timingRisk"),
                        "sourceStateProvenAtScheduling": record.get("sourceStateProvenAtScheduling"),
                    })
        for payload in self.payloads:
            if payload.classification == "rdg-buffer-version":
                timing.append({
                    "payloadId": payload.id, "file": payload.original_path,
                    "timingRisk": "CompletionUnobserved",
                    "reason": (payload.completion or {}).get("reason"),
                })
        inactive = []
        if isinstance(self.schema_doc, dict):
            for schema in self.schema_doc.get("resourceSchemas", []) or []:
                storage = schema.get("storage") if isinstance(schema, dict) else None
                if isinstance(storage, dict) and storage.get("captureObserved") is False:
                    inactive.append({"schemaId": schema.get("id"),
                                     "reason": "schema declares storage.captureObserved=false (inactive/unused capacity in this capture)"})
        for resource in self.resources:
            if resource.get("kind") == "buffer" and resource.get("bytesPerElement") and resource.get("numElements"):
                declared = int(resource["bytesPerElement"]) * int(resource["numElements"])
                captured = sum(self.payload_by_id[pid].bytes for pid in resource.get("payloadIds", []) if pid in self.payload_by_id)
                if captured and declared > captured:
                    inactive.append({"resourceId": resource["id"], "name": resource["name"],
                                     "reason": f"declared capacity {declared} B exceeds captured version bytes {captured} B; the difference is unobserved capacity, not zero data"})
        source_only = []
        if isinstance(self.schema_doc, dict):
            for group, parameter in (("resourceSchemas", False), ("parameterSchemas", True)):
                for schema in self.schema_doc.get(group, []) or []:
                    if not isinstance(schema, dict):
                        continue
                    summary = field_support_summary(schema, parameter=parameter)
                    for name in summary["sourceEquationFields"]:
                        source_only.append({"schemaId": schema.get("id"), "field": name})
        missing_semantic = []
        for resource in self.resources:
            if resource.get("kind") == "buffer" and resource.get("metadataPointer") and not resource.get("bufferMetadata"):
                missing_semantic.append({"resourceId": resource["id"], "name": resource["name"],
                                         "reason": "buffer structure metadata pointer has no captured metadata"})
            if resource.get("kind") == "texture" and not resource.get("schemaMatches"):
                missing_semantic.append({"resourceId": resource["id"], "name": resource["name"],
                                         "reason": "no source resource schema matched"})
        missing_semantic.append({"reason": "Static sampler initializers are source-derived; NUBB records StaticInitializerCaptured=false and only the bound sampler pointer is known.", "count": sum(1 for r in self.passes for n in r.get("nativeUniformBuffers", []) for b in (n.get("bindings") or {}).values() if isinstance(b, dict) and b.get("BaseType") == "UBMT_SAMPLER")})
        return [
            {"id": "unsupported-decode", "label": "Unsupported semantic decode",
             "count": len(unsupported), "items": unsupported[:200], "omitted": max(0, len(unsupported) - 200)},
            {"id": "missing-bytes", "label": "Missing or incomplete bytes",
             "count": len(missing_bytes), "items": missing_bytes[:200], "omitted": max(0, len(missing_bytes) - 200)},
            {"id": "unproven-native-timing", "label": "Unproven native timing / completion",
             "count": len(timing), "items": timing[:200], "omitted": max(0, len(timing) - 200)},
            {"id": "inactive-unused-capacity", "label": "Inactive or unused capacity",
             "count": len(inactive), "items": inactive[:200], "omitted": max(0, len(inactive) - 200)},
            {"id": "source-only-formula", "label": "Source-only formula descriptions",
             "count": len(source_only), "items": source_only[:200], "omitted": max(0, len(source_only) - 200)},
            {"id": "missing-semantic-metadata", "label": "Missing semantic metadata",
             "count": len(missing_semantic), "items": missing_semantic[:200], "omitted": max(0, len(missing_semantic) - 200)},
        ]

    def coverage_document(self) -> dict[str, Any]:
        by_severity = Counter(g["severity"] for g in self.gaps)
        by_scope = Counter(g["scope"] for g in self.gaps)
        unsupported = []
        for r in self.resources:
            if r["kind"] == "texture" and not r["decode"].get("supported", False):
                unsupported.append({"resourceId": r["id"], "name": r["name"], "format": r.get("format"),
                                    "reason": r["decode"].get("reason")})
        return {
            "format": f"{PACKAGE_FORMAT}-coverage",
            "version": PACKAGE_VERSION,
            "fixture": self.fixture,
            "evidenceKind": getattr(self, "evidence_kind", "unknown"),
            "counts": self.counts(),
            "bySeverity": dict(sorted(by_severity.items())),
            "byScope": dict(sorted(by_scope.items())),
            "complete": len(self.gaps) == 0,
            "statement": self._coverage_statement(),
            "categories": self.coverage_categories(),
            "unsupportedFormats": unsupported,
            "gaps": self.gaps,
        }

    def capture_document(self) -> dict[str, Any]:
        screenshot = None
        for nf in self.native_files:
            low = nf.rel.lower()
            if low.endswith("screenshot.png"):
                screenshot = {"nativePath": nf.package_rel, "bytes": nf.bytes, "sha256": nf.sha256}
                break
        return {
            "format": f"{PACKAGE_FORMAT}-capture",
            "version": PACKAGE_VERSION,
            "fixture": self.fixture,
            "evidenceKind": getattr(self, "evidence_kind", "unknown"),
            "notice": getattr(self, "notice", ""),
            "status": self.status,
            "infos": self.infos,
            "dumpService": self.dump_service,
            "consoleVariables": self.cvars,
            "screenshot": screenshot,
            "nativeFiles": [
                {"path": nf.rel, "packagePath": nf.package_rel, "bytes": nf.bytes,
                 "sha256": nf.sha256, "kind": nf.kind}
                for nf in self.native_files
            ],
        }

    def inventory_document(self, package_files: int = 0, largest: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "format": f"{PACKAGE_FORMAT}-inventory",
            "version": PACKAGE_VERSION,
            "adapter": self.adapter,
            "fixture": self.fixture,
            "counts": {**self.counts(), "packageFiles": package_files,
                       "largestOutputBytes": largest["bytes"] if largest else None},
            "nativeRecords": self.native_records,
            "nativeFiles": [
                {"path": nf.rel, "packagePath": nf.package_rel, "bytes": nf.bytes,
                 "sha256": nf.sha256, "kind": nf.kind}
                for nf in self.native_files
            ],
            "resources": [
                {"id": r["id"], "nativeRecordId": r.get("nativeRecordId"), "nativePath": r.get("nativePath"),
                 "uniqueName": r["uniqueName"], "name": r["name"], "kind": r["kind"], "format": r.get("format"),
                 "bytes": r.get("bytes"), "payloadIds": r.get("payloadIds", []),
                 "schemaStatus": r.get("schemaStatus")}
                for r in self.resources
            ],
            "passes": [
                {"id": p["id"], "nativeRecordId": p.get("nativeRecordId"), "name": p["name"],
                 "pointer": p.get("pointer"), "parametersPointer": p.get("parametersPointer"),
                 "parameterPayloadId": (p.get("parameterBlock") or {}).get("payloadId"),
                 "inputCount": len(p.get("inputs", [])), "outputCount": len(p.get("outputs", []))}
                for p in self.passes
            ],
            "payloads": [self.payload_public(p) for p in self.payloads],
            "uniformStructures": [
                {"pointer": ptr, "record": rec} for ptr, rec in sorted(self.structures.items())
            ],
            "chunks": self.chunks_public(),
            "gaps": self.gaps,
        }

    def write(self) -> None:
        if self.estimate or self.output_root is None:
            return
        out = self.output_root
        # Native metadata is retained byte-for-byte.
        for nf in self.native_files:
            dest = out / "native" / Path(nf.rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(nf.abs_path, dest)
        for resource in self.resources:
            path = out / "records" / "resource" / f"{resource['id']}.json"
            path.write_bytes(json_bytes(self.resource_record(resource)))
        for pas in self.passes:
            path = out / "records" / "pass" / f"{pas['id']}.json"
            path.write_bytes(json_bytes(self.pass_record(pas)))
        # The joined source schema is evidence: retain its exact bytes so the
        # package copy matches index.schema.sha256/bytes. Only the synthetic
        # placeholder (no source schema) is serialized here.
        schema_out = self.schema_raw if self.schema_raw is not None else json_bytes(self._schema_placeholder())
        (out / "schema.json").write_bytes(schema_out)
        (out / "structures.json").write_bytes(json_bytes({
            "format": f"{PACKAGE_FORMAT}-structures", "version": PACKAGE_VERSION,
            "count": len(self.structures), "byPointer": self.structures,
        }))
        (out / "capture.json").write_bytes(json_bytes(self.capture_document()))
        (out / "resources.json").write_bytes(json_bytes(self.resources_document()))
        (out / "passes.json").write_bytes(json_bytes(self.passes_document()))
        (out / "coverage.json").write_bytes(json_bytes(self.coverage_document()))
        # File statistics before writing inventory/index (they reference each other's counts).
        package_files, largest = self._package_file_stats(out)
        inventory = self.inventory_document(package_files + 2, largest)
        (out / "inventory.json").write_bytes(json_bytes(inventory))
        (out / "native-inputs.json").write_bytes(json_bytes(self._native_inputs_document()))
        package_files, largest = self._package_file_stats(out)
        index = self.index_document(package_files + 1, largest)
        (out / "index.json").write_bytes(json_bytes(index))
        package_files, largest = self._package_file_stats(out)
        # Rewrite inventory/index once with final file counts (their own sizes can
        # change the largest-file answer, not the count).
        inventory["counts"]["packageFiles"] = package_files
        inventory["counts"]["largestOutputBytes"] = largest["bytes"] if largest else None
        (out / "inventory.json").write_bytes(json_bytes(inventory))
        package_files, largest = self._package_file_stats(out)
        index["counts"]["packageFiles"] = package_files
        index["counts"]["largestOutputBytes"] = largest["bytes"] if largest else None
        index["largestOutput"] = largest
        (out / "index.json").write_bytes(json_bytes(index))

    def _schema_placeholder(self) -> dict[str, Any]:
        return self.schema_doc or {
            "format": "unreal-frame-explorer-schema",
            "status": self.schema_status,
            "resourceSchemas": [], "parameterSchemas": [],
            "transforms": [], "mathTerms": [], "sourceRefs": {},
            "gap": "No source schema was available at package time.",
        }

    def _package_file_stats(self, out: Path) -> tuple[int, dict[str, Any]]:
        count = 0
        largest = {"path": "", "bytes": 0}
        for dirpath, dirnames, filenames in os.walk(out):
            for name in filenames:
                path = Path(dirpath) / name
                size = path.stat().st_size
                count += 1
                if size > largest["bytes"]:
                    largest = {"path": path.relative_to(out).as_posix(), "bytes": size}
        return count, largest


# --------------------------------------------------------------------- verify
def _chunk_cache() -> dict[str, bytes]:
    return {}


def _load_chunk(package: Path, sha: str, expected_length: int, cache: dict[str, bytes]) -> bytes:
    if sha in cache:
        data = cache[sha]
    else:
        path = package / "chunks" / f"{sha}.gz"
        if not path.exists():
            raise PackageError(f"Missing chunk file: {path.relative_to(package)}")
        try:
            with gzip.open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            raise PackageError(f"Chunk {sha} is not valid gzip: {exc}") from exc
        actual = sha256_bytes(data)
        if actual != sha:
            raise PackageError(f"Chunk SHA mismatch for {sha}: decompressed {actual}")
        if len(data) != expected_length:
            raise PackageError(f"Chunk length mismatch for {sha}: {len(data)} != {expected_length}")
        if len(cache) < 64:
            cache[sha] = data
    return data


def reconstruct_payload(package: Path, payload: dict[str, Any], cache: dict[str, bytes],
                        dest: Path | None = None) -> dict[str, Any]:
    h = hashlib.sha256()
    total = 0
    chunks = payload.get("chunks") or []
    target = None
    if dest is not None:
        rel = safe_relpath(payload["originalPath"])
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target_out = target.open("wb")
    else:
        target_out = None
    try:
        for ref in chunks:
            data = _load_chunk(package, ref["sha"], ref["length"], cache)
            if target_out:
                target_out.write(data)
            h.update(data)
            total += len(data)
    finally:
        if target_out:
            target_out.close()
    sha = h.hexdigest()
    if total != int(payload["bytes"]):
        raise PackageError(f"Payload {payload['id']} length mismatch: {total} != {payload['bytes']}")
    if sha != payload["sha256"]:
        raise PackageError(f"Payload {payload['id']} SHA mismatch: {sha} != {payload['sha256']}")
    return {"payloadId": payload["id"], "bytes": total, "sha256": sha, "path": payload["originalPath"],
            "written": str(target) if target else None}


def verify_package(package: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    package = package.resolve()
    index_path = package / "index.json"
    if not index_path.exists():
        raise PackageError(f"Not a frame-explorer package (missing index.json): {package}")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if index.get("format") != PACKAGE_FORMAT:
        raise PackageError(f"Unexpected package format: {index.get('format')!r}")
    inventory_path = package / "inventory.json"
    if not inventory_path.exists():
        raise PackageError("Package has no inventory.json.")
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    cache: dict[str, bytes] = {}
    verified_chunks = set()
    for chunk in inventory.get("chunks", []):
        data = _load_chunk(package, chunk["sha"], chunk["rawLength"], cache)
        if len(data) != chunk["rawLength"]:
            raise PackageError(f"Chunk {chunk['sha']} length mismatch.")
        verified_chunks.add(chunk["sha"])
    payloads = []
    for payload in inventory.get("payloads", []):
        payloads.append(reconstruct_payload(package, payload, cache))
    for nf in inventory.get("nativeFiles", []):
        path = package / nf["packagePath"]
        if not path.exists():
            raise PackageError(f"Missing native file: {nf['packagePath']}")
        sha, size = sha256_file(path)
        if sha != nf["sha256"] or size != nf["bytes"]:
            raise PackageError(f"Native file mismatch: {nf['packagePath']}")
    for record in inventory.get("nativeRecords", []):
        if record.get("kind") == "payload":
            # payload records are covered by the payload hash checks above
            continue
    return index, payloads, verified_chunks, inventory


def verify_and_report(package: Path, write_dest: Path | None = None, force: bool = False) -> int:
    package = package.resolve()
    index, payloads, chunks, inventory = verify_package(package)
    if write_dest is not None:
        dest = write_dest.resolve()
        if dest == package or is_within(dest, package) or is_within(package, dest):
            raise PackageError("Reconstruction destination must not overlap the package directory.")
        if dest.exists() and any(dest.iterdir()):
            if not force:
                raise PackageError(f"Reconstruction destination is not empty: {dest}. Use --force.")
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        cache: dict[str, bytes] = {}
        written = []
        for payload in inventory.get("payloads", []):
            written.append(reconstruct_payload(package, payload, cache, dest))
        manifest = {
            "format": f"{PACKAGE_FORMAT}-reconstruction",
            "version": PACKAGE_VERSION,
            "packageSha256": sha256_file(package / "index.json")[0],
            "payloads": written,
        }
        (dest / "reconstruction-manifest.json").write_bytes(json_bytes(manifest))
        print(f"Reconstructed {len(written)} payload(s) to {dest}")
    print(f"Verified {len(inventory.get('chunks', []))} unique chunk(s), "
          f"{len(payloads)} payload(s), {len(inventory.get('nativeFiles', []))} native file(s).")
    print(f"Raw payload bytes: {sum(p['bytes'] for p in payloads)}; "
          f"gaps recorded: {len(inventory.get('gaps', []))}.")
    return 0


# ----------------------------------------------------------------------- CLI
def build_command(args: argparse.Namespace) -> int:
    input_root = Path(args.input)
    output_root = Path(args.output) if args.output else None
    schema_path = Path(args.schema) if args.schema else None
    if not args.estimate and output_root is None:
        raise PackageError("--output is required unless --estimate is used.")
    pkg = FramePackage(input_root, output_root, args.adapter, schema_path,
                       estimate=args.estimate, force=args.force, gzip_level=args.gzip_level)
    pkg.prepare()
    pkg.scan_native()
    pkg.load_adapter()
    pkg.ingest_payloads()
    pkg.load_schema()
    pkg.finalize()
    if not args.estimate:
        pkg.write()
    counts = pkg.counts()
    output_files = 0
    largest = None
    if args.estimate:
        largest = max(({"path": c["file"], "bytes": c["compressedLength"]} for c in pkg.chunks.values()),
                      key=lambda c: c["bytes"], default=None)
    else:
        assert output_root is not None
        output_files, largest = pkg._package_file_stats(output_root)
    print(("ESTIMATE (no files written)" if args.estimate else "PACKAGE COMPLETE"))
    print(f"adapter={pkg.adapter} fixture={str(pkg.fixture).lower()} schema={pkg.schema_status}")
    print(f"native records: {counts['nativeRecords']} {json.dumps(counts['nativeRecordKinds'], sort_keys=True)}")
    print(f"resources: {counts['resources']} (with payload {counts['resourcesWithPayload']}, missing payload {counts['resourcesMissingPayload']}, unsupported format {counts['unsupportedResourceFormats']})")
    print(f"passes: {counts['passes']} (parameter block captured {counts['passesWithParameterBlock']}, missing {counts['passesMissingParameterBlock']})")
    print(f"parameter payloads: {counts['parameterBlocks']}; uniform structures: {counts['uniformStructures']}")
    print(f"payload files: {counts['payloadFiles']}; raw bytes: {counts['payloadBytes']}")
    print(f"unique chunk raw bytes: {counts['uniqueChunkBytes']}; compressed chunk bytes: {counts['compressedChunkBytes']}; unique chunks: {counts['chunkFiles']}")
    if args.estimate:
        estimated_files = counts['chunkFiles'] + counts['nativeFiles'] + counts['resources'] + counts['passes'] + 8
        print(f"estimated package files: {estimated_files}; largest estimated file: {largest['path'] if largest else '(none)'} ({largest['bytes'] if largest else 0} bytes)")
    print(f"files: {output_files if not args.estimate else counts['chunkFiles']}; largest output: {largest['path'] if largest else '(none)'} ({largest['bytes'] if largest else 0} bytes)")
    print(f"gaps: {counts['gapCount']}; coverage complete: {str(len(pkg.gaps) == 0).lower()}")
    if not args.estimate:
        print(f"output: {output_root}")
    return 0


def estimate_command(args: argparse.Namespace) -> int:
    # Kept as a convenience alias: estimate is a build flag, not a separate mode.
    args.estimate = True
    args.output = None
    return build_command(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Package a native Unreal DumpGPU frame for the static explorer.")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Build a package from a DumpGPU or normalized frame manifest.")
    build.add_argument("--input", required=True, help="DumpGPU root (contains Base/) or normalized frame directory.")
    build.add_argument("--output", help="Package output directory (required unless --estimate).")
    build.add_argument("--adapter", default="auto", choices=["auto", "dumpgpu", "normalized"])
    build.add_argument("--schema", help="Optional source-schema.json to join by explicit selectors.")
    build.add_argument("--estimate", action="store_true", help="Compute and print sizes without writing package files.")
    build.add_argument("--force", action="store_true", help="Replace a previous package output directory.")
    build.add_argument("--gzip-level", type=int, default=DEFAULT_GZIP_LEVEL, choices=range(1, 10))
    build.set_defaults(func=build_command)

    verify = sub.add_parser("verify", help="Verify every chunk, payload hash/length and native file copy.")
    verify.add_argument("--package", required=True)
    verify.set_defaults(func=lambda a: verify_and_report(Path(a.package)))

    reconstruct = sub.add_parser("reconstruct", help="Verify and rebuild the original raw payload paths.")
    reconstruct.add_argument("--package", required=True)
    reconstruct.add_argument("--dest", required=True)
    reconstruct.add_argument("--force", action="store_true")
    reconstruct.set_defaults(func=lambda a: verify_and_report(Path(a.package), Path(a.dest), a.force))

    args = parser.parse_args(argv)
    try:
        if args.command == "build" and args.estimate:
            args.output = None
        return args.func(args)
    except PackageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
