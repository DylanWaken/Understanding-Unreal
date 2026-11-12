#!/usr/bin/env python3
"""Deterministic source-declaration extractor for UE 5.8 Lumen / VSM / deferred.

This tool does NOT infer GPU byte layouts.  It inventories:

  * SHADER_PARAMETER* macro declarations in scoped C++ shader classes,
  * RDG_BUFFER_ACCESS / RDG_TEXTURE_ACCESS (plain, _DYNAMIC and _ARRAY forms)
    and RENDER_TARGET_BINDING_SLOTS resource accesses,
  * BEGIN_SHADER_PARAMETER_STRUCT, BEGIN_GLOBAL_SHADER_PARAMETER_STRUCT and
    BEGIN_UNIFORM_BUFFER_STRUCT parameter structs (owner class qualified),
  * plain `struct` declarations and their fields in scoped shader files,
  * #include edges and IMPLEMENT_GLOBAL_SHADER class -> shader entry bindings.

The result is a *source-only superset*.  A declaration in this index does not
prove that a binding is active for a captured frame; runtime permutations and
the active binding closure select what the GPU actually sees.

Usage:
  python extract_declarations.py [--engine-root D:/UnrealEngine]
                                 [--out declaration-index.json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ENGINE_VERSION = "5.8.1"
ENGINE_COMMIT = "71fe36aac5a8df5ccd66c763ffc902b29b6a9c43"

SHADER_DIRS = [
    "Engine/Shaders/Private/Lumen",
    "Engine/Shaders/Private/VirtualShadowMaps",
]
SHADER_FILES = [
    "Engine/Shaders/Private/Common.ush",
    "Engine/Shaders/Private/DeferredShadingCommon.ush",
    "Engine/Shaders/Private/DeferredLightingCommon.ush",
    "Engine/Shaders/Private/DeferredLightPixelShaders.usf",
    "Engine/Shaders/Private/DiffuseIndirectComposite.usf",
    "Engine/Shaders/Private/BasePassPixelShader.usf",
    "Engine/Shaders/Private/ShadingCommon.ush",
    "Engine/Shaders/Private/SHCommon.ush",
    "Engine/Shaders/Private/BRDF.ush",
    "Engine/Shaders/Private/Quantization.ush",
    "Engine/Shaders/Private/RandomPCG.ush",
    "Engine/Shaders/Private/OctahedralCommon.ush",
    "Engine/Shaders/Shared/VirtualShadowMapDefinitions.h",
]
CPP_DIRS = [
    "Engine/Source/Runtime/Renderer/Private/Lumen",
    "Engine/Source/Runtime/Renderer/Private/VirtualShadowMaps",
]
CPP_FILES = [
    "Engine/Source/Runtime/Renderer/Private/BasePassRendering.cpp",
    "Engine/Source/Runtime/Renderer/Private/DeferredShadingRenderer.cpp",
    "Engine/Source/Runtime/Renderer/Private/IndirectLightRendering.cpp",
    "Engine/Source/Runtime/Renderer/Private/LightRendering.cpp",
    "Engine/Source/Runtime/Renderer/Private/LightRendering.h",
    "Engine/Source/Runtime/Renderer/Private/ShadowDepthRendering.cpp",
    "Engine/Source/Runtime/Renderer/Private/ShadowRendering.h",
]
SHADER_EXTS = {".usf", ".ush"}
CPP_EXTS = {".h", ".cpp", ".inl"}

MACRO_RE = re.compile(
    r"\b("
    r"SHADER_PARAMETER[A-Z0-9_]*"
    r"|DECLARE_SCALAR_ARRAY"
    r"|RDG_BUFFER_ACCESS(?:_DYNAMIC|_ARRAY)?"
    r"|RDG_TEXTURE_ACCESS(?:_DYNAMIC|_ARRAY)?"
    r"|RENDER_TARGET_BINDING_SLOTS"
    r")\s*\("
)
BSS_RE = re.compile(
    r"\b("
    r"BEGIN_SHADER_PARAMETER_STRUCT"
    r"|BEGIN_GLOBAL_SHADER_PARAMETER_STRUCT(?:_WITH_CONSTRUCTOR)?"
    r"|BEGIN_UNIFORM_BUFFER_STRUCT(?:_WITH_CONSTRUCTOR)?"
    r")\s*\("
)
BSS_KINDS = {
    "BEGIN_SHADER_PARAMETER_STRUCT": (
        "END_SHADER_PARAMETER_STRUCT",
        "shader-parameter-struct",
    ),
    "BEGIN_GLOBAL_SHADER_PARAMETER_STRUCT": (
        "END_GLOBAL_SHADER_PARAMETER_STRUCT",
        "global-shader-parameter-struct",
    ),
    "BEGIN_GLOBAL_SHADER_PARAMETER_STRUCT_WITH_CONSTRUCTOR": (
        "END_GLOBAL_SHADER_PARAMETER_STRUCT",
        "global-shader-parameter-struct-with-constructor",
    ),
    "BEGIN_UNIFORM_BUFFER_STRUCT": (
        "END_UNIFORM_BUFFER_STRUCT",
        "uniform-buffer-struct",
    ),
    "BEGIN_UNIFORM_BUFFER_STRUCT_WITH_CONSTRUCTOR": (
        "END_UNIFORM_BUFFER_STRUCT",
        "uniform-buffer-struct-with-constructor",
    ),
}
STRUCT_RE = re.compile(r"\bstruct\s+([A-Za-z_]\w*)\s*\{")
CLASS_RE = re.compile(
    r"\b(class|struct)\s+(?:(?:alignas|ALIGNAS)\s*\([^)]*\)\s*)?"
    r"([A-Za-z_]\w*)\s*(?::[^;{]*)?\{"
)
GLOBAL_SHADER_RE = re.compile(
    r"\bIMPLEMENT_GLOBAL_SHADER\s*\(\s*([A-Za-z_][\w:]*)\s*,\s*"
    r'"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*([A-Za-z_][\w:]*)\s*\)'
)
INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.MULTILINE)

CLASSIFICATION = {
    "SHADER_PARAMETER": "value",
    "SHADER_PARAMETER_ARRAY": "array",
    "SHADER_PARAMETER_SCALAR_ARRAY": "scalar_array",
    "SHADER_PARAMETER_STRUCT": "struct",
    "SHADER_PARAMETER_STRUCT_ARRAY": "struct_array",
    "SHADER_PARAMETER_STRUCT_REF": "struct_ref",
    "SHADER_PARAMETER_STRUCT_INCLUDE": "struct_include",
    "SHADER_PARAMETER_RDG_UNIFORM_BUFFER": "rdg_uniform_buffer",
    "SHADER_PARAMETER_RDG_BUFFER_SRV": "rdg_buffer_srv",
    "SHADER_PARAMETER_RDG_BUFFER_SRV_ARRAY": "rdg_buffer_srv_array",
    "SHADER_PARAMETER_RDG_BUFFER_UAV": "rdg_buffer_uav",
    "SHADER_PARAMETER_RDG_BUFFER_UAV_ARRAY": "rdg_buffer_uav_array",
    "SHADER_PARAMETER_RDG_TEXTURE": "rdg_texture",
    "SHADER_PARAMETER_RDG_TEXTURE_ARRAY": "rdg_texture_array",
    "SHADER_PARAMETER_RDG_TEXTURE_SRV": "rdg_texture_srv",
    "SHADER_PARAMETER_RDG_TEXTURE_UAV": "rdg_texture_uav",
    "SHADER_PARAMETER_RDG_TEXTURE_UAV_ARRAY": "rdg_texture_uav_array",
    "SHADER_PARAMETER_SAMPLER": "sampler",
    "SHADER_PARAMETER_SRV": "srv",
    "SHADER_PARAMETER_TEXTURE": "texture",
    "SHADER_PARAMETER_UAV": "uav",
    "DECLARE_SCALAR_ARRAY": "scalar_array",
    # RDG resource-access macros do not carry a type argument: the first
    # argument is the member name and the second (when present) is the RHI
    # access mask.  The C++ type is supplied by the macro implementation.
    "RDG_BUFFER_ACCESS": "rdg_buffer_access",
    "RDG_BUFFER_ACCESS_DYNAMIC": "rdg_buffer_access_dynamic",
    "RDG_BUFFER_ACCESS_ARRAY": "rdg_buffer_access_array",
    "RDG_TEXTURE_ACCESS": "rdg_texture_access",
    "RDG_TEXTURE_ACCESS_DYNAMIC": "rdg_texture_access_dynamic",
    "RDG_TEXTURE_ACCESS_ARRAY": "rdg_texture_access_array",
    "RENDER_TARGET_BINDING_SLOTS": "render_target_binding_slots",
}
TEXTURE_LIKE = {
    "rdg_texture",
    "rdg_texture_array",
    "rdg_texture_srv",
    "rdg_texture_uav",
    "rdg_texture_uav_array",
    "texture",
    "srv",
    "uav",
}
BUFFER_LIKE = {
    "rdg_buffer_srv",
    "rdg_buffer_srv_array",
    "rdg_buffer_uav",
    "rdg_buffer_uav_array",
}


class LineIndex:
    def __init__(self, text: str):
        self.starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                self.starts.append(i + 1)

    def line(self, pos: int) -> int:
        import bisect

        return bisect.bisect_right(self.starts, pos)


def strip_comments(text: str) -> str:
    """Replace // and /* */ comments with spaces, preserving lines and strings."""
    out = list(text)
    i, n = 0, len(text)
    state = "code"
    quote = ""
    while i < n:
        c = text[i]
        if state == "code":
            if c == "/" and i + 1 < n and text[i + 1] == "/":
                state = "line"
                out[i] = out[i + 1] = " "
                i += 2
                continue
            if c == "/" and i + 1 < n and text[i + 1] == "*":
                state = "block"
                out[i] = out[i + 1] = " "
                i += 2
                continue
            if c in ('"', "'"):
                state = "string"
                quote = c
        elif state == "line":
            if c == "\n":
                state = "code"
            else:
                out[i] = " "
        elif state == "block":
            if c == "*" and i + 1 < n and text[i + 1] == "/":
                state = "code"
                out[i] = out[i + 1] = " "
                i += 2
                continue
            if c != "\n":
                out[i] = " "
        elif state == "string":
            if c == "\\":
                i += 1
            elif c == quote:
                state = "code"
        i += 1
    return "".join(out)


def find_matching(text: str, open_pos: int, opener: str = "(", closer: str = ")") -> int:
    depth = 0
    i, n = open_pos, len(text)
    state = "code"
    quote = ""
    while i < n:
        c = text[i]
        if state == "code":
            if c in ('"', "'"):
                state = "string"
                quote = c
            elif c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    return i
        elif state == "string":
            if c == "\\":
                i += 1
            elif c == quote:
                state = "code"
        i += 1
    return -1


def split_args(arg_text: str) -> list[str]:
    args: list[str] = []
    buf: list[str] = []
    depth = 0
    state = "code"
    quote = ""
    for c in arg_text:
        if state == "code":
            if c in ('"', "'"):
                state = "string"
                quote = c
            elif c in "([{<":
                depth += 1
            elif c in ")]}>":
                depth = max(0, depth - 1)
            elif c == "," and depth == 0:
                args.append("".join(buf).strip())
                buf = []
                continue
        elif state == "string":
            if c == quote:
                state = "code"
        buf.append(c)
    tail = "".join(buf).strip()
    if tail or args:
        args.append(tail)
    return args


def normalize(text: str) -> str:
    return " ".join(text.split())


def strip_outer_brackets(text: str) -> str:
    t = text.strip()
    if t.startswith("[") and t.endswith("]"):
        return t[1:-1].strip()
    return t


def build_class_ranges(text: str):
    ranges = []
    for m in CLASS_RE.finditer(text):
        open_pos = text.find("{", m.end() - 1)
        if open_pos < 0:
            continue
        close_pos = find_matching(text, open_pos, "{", "}")
        if close_pos < 0:
            continue
        ranges.append(
            {
                "kind": m.group(1),
                "name": m.group(2),
                "decl_pos": m.start(),
                "open": open_pos,
                "close": close_pos,
            }
        )
    return ranges


def owner_for(pos: int, class_ranges):
    best = None
    for r in class_ranges:
        if r["open"] < pos < r["close"]:
            span = r["close"] - r["open"]
            if best is None or span < best[0]:
                best = (span, r)
    return best[1] if best else None


def build_guards(text: str):
    """Return per-1-based-line active preprocessor condition stack.

    Each entry is a list of dicts {line, directive, condition}. Conditional
    declarations are source-only alternates; runtime activation must evaluate
    these guards (e.g. a literal `&& 0` means compiled out).
    """
    guards = {}
    stack = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        guards[lineno] = [dict(x) for x in stack]
        if not stripped.startswith("#"):
            continue
        directive = stripped[1:].strip()
        head = re.match(r"([A-Za-z]+)\b(.*)", directive)
        if not head:
            continue
        kind = head.group(1)
        rest = head.group(2).strip()
        if kind in ("if", "ifdef", "ifndef"):
            stack.append({"line": lineno, "directive": "#" + kind, "condition": rest})
        elif kind in ("elif", "else"):
            if stack:
                prior = stack[-1].get("condition", "")
                if kind == "elif":
                    stack[-1] = {
                        "line": lineno,
                        "directive": "#elif",
                        "condition": f"!({prior}) && ({rest})" if prior else rest,
                        "priorCondition": prior,
                    }
                else:
                    stack[-1] = {
                        "line": lineno,
                        "directive": "#else",
                        "condition": f"!({prior})" if prior else "1",
                        "priorCondition": prior,
                    }
        elif kind == "endif":
            if stack:
                stack.pop()
    return guards


def guards_for(guards, line):
    return guards.get(line, [])


def parse_macros(text: str, line_index: LineIndex):
    macros = []
    skipped_defines = 0
    for m in MACRO_RE.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        if text[line_start : m.start()].lstrip().startswith("#"):
            skipped_defines += 1
            continue
        open_pos = m.end() - 1
        close_pos = find_matching(text, open_pos)
        if close_pos < 0:
            continue
        args = split_args(text[open_pos + 1 : close_pos])
        macro_name = m.group(1)
        kind = CLASSIFICATION.get(macro_name)
        if kind is None:
            kind = "unknown"
        entry = {
            "macro": macro_name,
            "kind": kind,
            "line": line_index.line(m.start()),
            "column": m.start() - line_start + 1,
            "pos": m.start(),
            "args": args,
            "text": normalize(text[m.start() : close_pos + 1]),
            "parameterType": None,
            "parameterName": None,
            "count": None,
            "packedStruct": None,
            "accessMask": None,
        }
        if kind in ("rdg_buffer_access", "rdg_texture_access"):
            # RDG_BUFFER_ACCESS(MemberName, Access) /
            # RDG_TEXTURE_ACCESS(MemberName, Access): args[0] is the NAME and
            # args[1] is the RHI access mask.  There is no type argument.
            if args:
                entry["parameterName"] = args[0].strip()
            if len(args) >= 2:
                entry["accessMask"] = normalize(args[1])
            base = (
                "TRDGBufferAccess"
                if kind == "rdg_buffer_access"
                else "TRDGTextureAccess"
            )
            entry["parameterType"] = (
                f"{base}<{entry['accessMask']}>" if entry["accessMask"] else base
            )
        elif kind in ("rdg_buffer_access_dynamic", "rdg_texture_access_dynamic"):
            if args:
                entry["parameterName"] = args[0].strip()
            entry["parameterType"] = (
                "FRDGBufferAccess"
                if kind == "rdg_buffer_access_dynamic"
                else "FRDGTextureAccess"
            )
            entry["accessMask"] = "dynamic (runtime array)"
        elif kind in ("rdg_buffer_access_array", "rdg_texture_access_array"):
            if args:
                entry["parameterName"] = args[0].strip()
            entry["parameterType"] = (
                "FRDGBufferAccessArray"
                if kind == "rdg_buffer_access_array"
                else "FRDGTextureAccessArray"
            )
            entry["accessMask"] = "array (runtime length)"
        elif kind == "render_target_binding_slots":
            # RENDER_TARGET_BINDING_SLOTS() has no arguments; the macro expands
            # to a member named RenderTargets of type FRenderTargetBindingSlots.
            entry["parameterType"] = "FRenderTargetBindingSlots"
            entry["parameterName"] = "RenderTargets"
        else:
            if args:
                entry["parameterType"] = normalize(args[0])
            if len(args) >= 2:
                entry["parameterName"] = args[1].strip()
            if kind in ("array", "scalar_array", "struct_array", "rdg_buffer_srv_array",
                        "rdg_buffer_uav_array", "rdg_texture_array", "rdg_texture_uav_array"):
                if len(args) >= 3:
                    entry["count"] = strip_outer_brackets(args[-1])
            if kind in ("struct", "struct_array", "struct_ref", "struct_include",
                        "rdg_uniform_buffer"):
                entry["packedStruct"] = normalize(args[0]) if args else None
            if kind == "scalar_array" and macro_name == "DECLARE_SCALAR_ARRAY":
                if len(args) >= 3:
                    entry["parameterName"] = args[1].strip()
                    entry["count"] = strip_outer_brackets(args[2])
        macros.append(entry)
    return macros, skipped_defines


def parse_parameter_structs(text: str, line_index: LineIndex, macros):
    structs = []
    for m in BSS_RE.finditer(text):
        begin_macro = m.group(1)
        end_macro, declaration_kind = BSS_KINDS[begin_macro]
        line_start = text.rfind("\n", 0, m.start()) + 1
        if text[line_start : m.start()].lstrip().startswith("#"):
            continue
        open_pos = m.end() - 1
        close_pos = find_matching(text, open_pos)
        if close_pos < 0:
            continue
        args = split_args(text[open_pos + 1 : close_pos])
        if not args:
            continue
        name = args[0].strip()
        # Search the matching END marker from the end of BEGIN(...).  The
        # BEGIN_GLOBAL_* and BEGIN_UNIFORM_BUFFER_* variants have distinct END
        # spellings; BEGIN_SHADER_PARAMETER_STRUCT uses END_SHADER_PARAMETER_STRUCT.
        em = re.search(r"\b" + end_macro + r"\s*\(\s*\)", text[close_pos:])
        end_start = close_pos + (em.start() if em else 0)
        end_pos = close_pos + (em.end() if em else 0)
        body_start = close_pos
        body_end = end_start
        fields = [
            mac for mac in macros if body_start < mac["pos"] < body_end
        ]
        structs.append(
            {
                "structName": name,
                "line": line_index.line(m.start()),
                "column": m.start() - line_start + 1,
                "endLine": line_index.line(end_start) if em else line_index.line(close_pos),
                "pos": m.start(),
                "bodyStart": body_start,
                "bodyEnd": body_end,
                "beginMacro": begin_macro,
                "endMacro": end_macro,
                "declarationKind": declaration_kind,
                "fields": fields,
            }
        )
    return structs


def strip_preprocessor(body: str) -> str:
    out = []
    for line in body.splitlines(keepends=True):
        if line.lstrip().startswith("#"):
            out.append("\n" if line.endswith("\n") else "")
        else:
            out.append(line)
    return "".join(out)


def split_statements(body: str):
    """Yield (offset, statement) for top-level ';' terminated chunks."""
    prepared = strip_preprocessor(body)
    buf = []
    depth = 0
    start = 0
    i, n = 0, len(prepared)
    while i < n:
        c = prepared[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth = max(0, depth - 1)
        if c == ";" and depth == 0:
            stmt = "".join(buf)
            yield start, stmt
            buf = []
            start = i + 1
        else:
            buf.append(c)
        i += 1


def parse_field_statement(stmt: str):
    t = normalize(stmt)
    if not t:
        return None
    if t.startswith("#") or "(" in t or ")" in t or "operator" in t:
        return None
    if t.startswith("struct ") or t.startswith("union ") or t.startswith("class "):
        return None
    # Drop a default initializer at the top level (struct fields rarely use one).
    default_text = None
    eq = t.find("=")
    if eq >= 0:
        default_text = t[eq + 1 :].strip()
        t = t[:eq].strip()
    bit_width = None
    bm = re.search(r":\s*(\d+)\s*$", t)
    if bm:
        bit_width = int(bm.group(1))
        t = t[: bm.start()].strip()
    fm = re.match(
        r"^(?P<type>.+?)\s+(?P<name>[A-Za-z_]\w*)\s*"
        r"(?:\[(?P<count>[^\]]*)\])?\s*$",
        t,
    )
    if not fm:
        return None
    type_text = normalize(fm.group("type"))
    if not type_text or type_text in ("return", "typedef"):
        return None
    if "," in type_text:
        return None
    return {
        "name": fm.group("name"),
        "type": type_text,
        "count": normalize(fm.group("count")) if fm.group("count") is not None else None,
        "bitWidth": bit_width,
        "default": default_text,
        "text": t,
    }


def parse_plain_structs(text: str, line_index: LineIndex):
    structs = []
    for m in STRUCT_RE.finditer(text):
        open_pos = m.end() - 1
        close_pos = find_matching(text, open_pos, "{", "}")
        if close_pos < 0:
            continue
        body = text[open_pos + 1 : close_pos]
        body_line = line_index.line(open_pos)
        fields = []
        unparsed = []
        for offset, stmt in split_statements(body):
            field = parse_field_statement(stmt)
            # Point at the first non-whitespace character of the declaration, not
            # at the end of the previous ';' (which sits on the preceding line).
            trimmed = len(stmt) - len(stmt.lstrip())
            stmt_line = body_line + body.count("\n", 0, offset + trimmed)
            if field is None:
                raw = normalize(stmt)
                if raw:
                    unparsed.append({"line": stmt_line, "text": raw[:400]})
                continue
            field["line"] = stmt_line
            fields.append(field)
        structs.append(
            {
                "name": m.group(1),
                "line": line_index.line(m.start()),
                "endLine": line_index.line(close_pos),
                "open": open_pos,
                "close": close_pos,
                "fields": fields,
                "unparsedFields": unparsed,
            }
        )
    return structs


def collect_files(engine_root: Path):
    """Return (coverage, parsed_source) for all considered files."""
    considered = []

    def add_path(rel: str, discovery: str):
        considered.append((rel, discovery))

    for d in SHADER_DIRS:
        base = engine_root / d
        if not base.is_dir():
            add_path(d, "directory-missing")
            continue
        for root, dirnames, filenames in os.walk(base):
            dirnames.sort()
            for fn in sorted(filenames):
                full = Path(root) / fn
                rel = full.relative_to(engine_root).as_posix()
                add_path(rel, f"recursive:{d}")
    for rel in SHADER_FILES:
        add_path(rel, "explicit-shader")
    for d in CPP_DIRS:
        base = engine_root / d
        if not base.is_dir():
            add_path(d, "directory-missing")
            continue
        for root, dirnames, filenames in os.walk(base):
            dirnames.sort()
            for fn in sorted(filenames):
                full = Path(root) / fn
                rel = full.relative_to(engine_root).as_posix()
                add_path(rel, f"recursive:{d}")
    for rel in CPP_FILES:
        add_path(rel, "explicit-cpp")

    # De-duplicate, preserving first discovery marker.
    seen = set()
    unique = []
    for rel, discovery in considered:
        if rel in seen:
            continue
        seen.add(rel)
        unique.append((rel, discovery))
    return unique


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-root", default="D:/UnrealEngine")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent / "declaration-index.json"),
    )
    args = parser.parse_args(argv)

    engine_root = Path(args.engine_root).resolve()
    if not engine_root.is_dir():
        print(f"engine root not found: {engine_root}", file=sys.stderr)
        return 2

    coverage = []
    include_graph = {}
    shader_bindings = []
    class_entries = {}
    parameter_structs = []
    plain_structs = []
    all_macros = []
    parsed_files = 0
    skipped_files = 0
    total_fields = 0

    for rel, discovery in collect_files(engine_root):
        full = engine_root / rel
        ext = Path(rel).suffix.lower()
        in_shader_scope = rel.startswith(tuple(SHADER_DIRS)) or rel in SHADER_FILES
        in_cpp_scope = rel.startswith(tuple(CPP_DIRS)) or rel in CPP_FILES
        shared_hlsl_header = rel in SHADER_FILES and ext == ".h"
        allowed = (
            (in_shader_scope and ext in SHADER_EXTS)
            or shared_hlsl_header
            or (in_cpp_scope and ext in CPP_EXTS)
        )
        if not full.is_file():
            skipped_files += 1
            coverage.append(
                {
                    "path": rel,
                    "discovery": discovery,
                    "status": "skipped",
                    "reason": "file-not-found",
                    "language": "unknown",
                    "bytes": 0,
                    "lines": 0,
                    "macroDeclarations": 0,
                    "parameterStructs": 0,
                    "plainStructs": 0,
                    "fields": 0,
                }
            )
            continue
        if not allowed:
            skipped_files += 1
            coverage.append(
                {
                    "path": rel,
                    "discovery": discovery,
                    "status": "skipped",
                    "reason": f"extension {ext or '(none)'} not parsed in this scope",
                    "language": "unknown",
                    "bytes": full.stat().st_size,
                    "lines": 0,
                    "macroDeclarations": 0,
                    "parameterStructs": 0,
                    "plainStructs": 0,
                    "fields": 0,
                }
            )
            continue

        raw = full.read_text(encoding="utf-8", errors="replace")
        text = strip_comments(raw)
        lines = raw.count("\n") + 1
        language = (
            "hlsl"
            if ext in SHADER_EXTS
            else (
                "hlsl-shared-header"
                if shared_hlsl_header
                else ("cpp" if ext in (".cpp", ".inl") else "c-header")
            )
        )
        line_index = LineIndex(text)
        guards = build_guards(text)
        macros, skipped_defines = parse_macros(text, line_index)
        pstructs = parse_parameter_structs(text, line_index, macros)
        sstructs = (
            parse_plain_structs(text, line_index)
            if language in ("hlsl", "hlsl-shared-header")
            else []
        )
        bindings = []
        if ext in (".cpp", ".h", ".inl"):
            for bm in GLOBAL_SHADER_RE.finditer(text):
                bindings.append(
                    {
                        "className": bm.group(1),
                        "shaderPath": bm.group(2),
                        "entryPoint": bm.group(3),
                        "stage": bm.group(4),
                        "sourcePath": rel,
                        "line": line_index.line(bm.start()),
                    }
                )
        class_ranges = build_class_ranges(text) if language != "hlsl" else []

        include_graph[rel] = sorted(set(INCLUDE_RE.findall(text)))

        macros_for_file = []
        file_macro_fields = 0
        for mac in macros:
            owner = owner_for(mac["pos"], class_ranges) if class_ranges else None
            macro_id = (
                f"sp:{rel}:{mac['line']}:{mac['column']}:"
                f"{mac['parameterName'] or '?'}"
            )
            entry = {
                "id": macro_id,
                "macro": mac["macro"],
                "kind": mac["kind"],
                "parameterType": mac["parameterType"],
                "parameterName": mac["parameterName"],
                "accessMask": mac["accessMask"],
                "count": mac["count"],
                "packedStruct": mac["packedStruct"],
                "ownerClass": owner["name"] if owner else None,
                "sourcePath": rel,
                "line": mac["line"],
                "column": mac["column"],
                "declarationText": mac["text"],
                "preprocessorGuards": guards_for(guards, mac["line"]),
            }
            macros_for_file.append(entry)
            all_macros.append(entry)

        for ps in pstructs:
            owner = owner_for(ps["pos"], class_ranges) if class_ranges else None
            owner_name = owner["name"] if owner else None
            qualified = f"{owner_name}::{ps['structName']}" if owner_name else ps["structName"]
            field_entries = []
            for mac in ps["fields"]:
                field_entries.append(
                    {
                        "name": mac["parameterName"],
                        "type": mac["parameterType"],
                        "accessMask": mac["accessMask"],
                        "count": mac["count"],
                        "macro": mac["macro"],
                        "kind": mac["kind"],
                        "packedStruct": mac["packedStruct"],
                        "line": mac["line"],
                        "column": mac["column"],
                        "preprocessorGuards": guards_for(guards, mac["line"]),
                    }
                )
                file_macro_fields += 1
            if language == "hlsl":
                struct_kind = "shader-parameter-struct"
            else:
                struct_kind = "cpp-" + ps["declarationKind"]
            parameter_structs.append(
                {
                    "id": f"ps:{qualified}@{rel}:{ps['line']}:{ps['column']}",
                    "structName": ps["structName"],
                    "qualifiedName": qualified,
                    "ownerClass": owner_name,
                    "language": language,
                    "kind": struct_kind,
                    "declarationKind": ps["declarationKind"],
                    "beginMacro": ps["beginMacro"],
                    "endMacro": ps["endMacro"],
                    "sourcePath": rel,
                    "line": ps["line"],
                    "column": ps["column"],
                    "endLine": ps["endLine"],
                    "fields": sorted(field_entries, key=lambda f: (f["line"], f["name"] or "")),
                    "preprocessorGuards": guards_for(guards, ps["line"]),
                    "decodingStatus": "source-only",
                    "gaps": [
                        "Macro declaration inventory only; no semantic decode or byte offset is asserted here.",
                        "Runtime permutation/active binding closure is not proven by this declaration.",
                    ],
                }
            )

        plain_for_file = []
        for ss in sstructs:
            field_entries = []
            for f in ss["fields"]:
                field_entries.append(
                    {
                        "name": f["name"],
                        "type": f["type"],
                        "count": f["count"],
                        "bitWidth": f["bitWidth"],
                        "default": f["default"],
                        "line": f["line"],
                        "preprocessorGuards": guards_for(guards, f["line"]),
                    }
                )
            plain_for_file.append(
                {
                    "id": f"st:{ss['name']}@{rel}:{ss['line']}",
                    "name": ss["name"],
                    "sourcePath": rel,
                    "line": ss["line"],
                    "endLine": ss["endLine"],
                    "fields": sorted(field_entries, key=lambda f: (f["line"], f["name"])),
                    "unparsedFields": ss["unparsedFields"],
                    "preprocessorGuards": guards_for(guards, ss["line"]),
                    "decodingStatus": "source-only",
                    "gaps": [
                        "Field order/type text only; byte offsets are not inferred from this declaration.",
                    ],
                }
            )
            total_fields += len(field_entries)
        plain_structs.extend(plain_for_file)

        for b in bindings:
            shader_bindings.append(b)
            ce = class_entries.setdefault(
                b["className"],
                {
                    "className": b["className"],
                    "sourcePath": b["sourcePath"],
                    "line": b["line"],
                    "shaderPath": b["shaderPath"],
                    "entryPoint": b["entryPoint"],
                    "stage": b["stage"],
                },
            )

        parsed_files += 1
        coverage.append(
            {
                "path": rel,
                "discovery": discovery,
                "status": "parsed",
                "reason": "parsed",
                "language": language,
                "bytes": full.stat().st_size,
                "lines": lines,
                "macroDeclarations": len(macros),
                "parameterStructs": len(pstructs),
                "plainStructs": len(sstructs),
                "fields": file_macro_fields
                + sum(len(s["fields"]) for s in sstructs),
                "skippedPreprocessorMacroDefinitions": skipped_defines,
                "shaderBindings": len(bindings),
            }
        )

    parameter_structs.sort(key=lambda s: (s["sourcePath"], s["line"], s["qualifiedName"]))
    plain_structs.sort(key=lambda s: (s["sourcePath"], s["line"], s["name"]))
    all_macros.sort(key=lambda s: (s["sourcePath"], s["line"], s["parameterName"] or ""))
    shader_bindings.sort(key=lambda s: (s["sourcePath"], s["line"], s["className"]))

    # Attach shader binding info to owner classes of parameter structs.
    binding_by_class = {}
    for b in shader_bindings:
        binding_by_class.setdefault(b["className"], b)
    for ps in parameter_structs:
        b = binding_by_class.get(ps["ownerClass"]) if ps["ownerClass"] else None
        ps["shader"] = (
            {
                "className": b["className"],
                "shaderPath": b["shaderPath"],
                "entryPoint": b["entryPoint"],
                "stage": b["stage"],
                "sourcePath": b["sourcePath"],
                "line": b["line"],
            }
            if b
            else None
        )

    shader_classes = sorted(
        class_entries.values(), key=lambda c: (c["sourcePath"], c["line"], c["className"])
    )

    counts = {
        "filesConsidered": len(coverage),
        "filesParsed": parsed_files,
        "filesSkipped": skipped_files,
        "macroDeclarations": len(all_macros),
        "shaderParameterMacroDeclarations": sum(
            1 for m in all_macros if m["macro"].startswith("SHADER_PARAMETER")
        ),
        "rdgAccessDeclarations": sum(
            1 for m in all_macros if "access" in m["kind"] and m["kind"].startswith("rdg_")
        ),
        "rdgBufferAccessDeclarations": sum(
            1 for m in all_macros if m["kind"].startswith("rdg_buffer_access")
        ),
        "rdgTextureAccessDeclarations": sum(
            1 for m in all_macros if m["kind"].startswith("rdg_texture_access")
        ),
        "renderTargetBindingSlots": sum(
            1 for m in all_macros if m["kind"] == "render_target_binding_slots"
        ),
        "parameterStructs": len(parameter_structs),
        "globalParameterStructs": sum(
            1 for s in parameter_structs
            if s["declarationKind"].startswith("global-shader-parameter-struct")
        ),
        "uniformBufferStructs": sum(
            1 for s in parameter_structs
            if s["declarationKind"].startswith("uniform-buffer-struct")
        ),
        "parameterStructFields": sum(len(s["fields"]) for s in parameter_structs),
        "plainStructs": len(plain_structs),
        "plainStructFields": total_fields,
        "shaderBindings": len(shader_bindings),
        "shaderClasses": len(shader_classes),
        "includeEdges": sum(len(v) for v in include_graph.values()),
    }
    all_ids = (
        [m["id"] for m in all_macros]
        + [s["id"] for s in parameter_structs]
        + [s["id"] for s in plain_structs]
    )
    counts["duplicateDeclarationIds"] = len(all_ids) - len(set(all_ids))

    index = {
        "schemaVersion": 1,
        "kind": "source-declaration-index",
        "engine": {
            "version": ENGINE_VERSION,
            "commit": ENGINE_COMMIT,
            "root": engine_root.as_posix(),
        },
        "scope": {
            "shaderDirectories": SHADER_DIRS,
            "shaderFiles": SHADER_FILES,
            "cppDirectories": CPP_DIRS,
            "cppFiles": CPP_FILES,
        },
        "notes": [
            "Source-only superset: declarations here do not prove a runtime binding is active.",
            "Runtime active bindings and shader permutations select the actual closure.",
            "No byte offsets are inferred from C++ struct layout; GPU layouts are decoded in source-schema.json only where shader pack/unpack code is explicit.",
            "Anonymous FParameters are qualified with their surrounding shader class.",
            "Parameter structs include BEGIN_SHADER_PARAMETER_STRUCT, BEGIN_GLOBAL_SHADER_PARAMETER_STRUCT(_WITH_CONSTRUCTOR) and BEGIN_UNIFORM_BUFFER_STRUCT(_WITH_CONSTRUCTOR) bodies, each matched to its own END macro.",
            "RDG_BUFFER_ACCESS / RDG_TEXTURE_ACCESS store the first macro argument as the member name and the second as accessMask; the macro, not the call site, supplies the C++ type. _DYNAMIC and _ARRAY forms are recognized even when a scoped file has none.",
            "RENDER_TARGET_BINDING_SLOTS() expands to a field named RenderTargets of type FRenderTargetBindingSlots.",
            "Declarations and fields carry preprocessorGuards (active #if/#ifdef stack at their line); a literal && 0 guard is compiled out and must not be treated as active.",
            "Comment text is stripped before matching and macro-definition lines (a leading #) are skipped, so #define and commented examples are not counted as declarations.",
        ],
        "counts": counts,
        "extractionCoverage": sorted(coverage, key=lambda c: c["path"]),
        "includeGraph": {k: include_graph[k] for k in sorted(include_graph)},
        "shaderClasses": shader_classes,
        "shaderBindings": shader_bindings,
        "parameterStructs": parameter_structs,
        "plainStructs": plain_structs,
        "macroDeclarations": all_macros,
    }

    out_path = Path(args.out)
    out_path.write_text(
        json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(counts, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
