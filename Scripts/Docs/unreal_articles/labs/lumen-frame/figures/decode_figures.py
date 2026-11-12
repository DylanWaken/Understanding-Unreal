"""Decode final-capture DumpGPU payloads into publication figures.

This script is deliberately self-contained under ``figures/``.  It reads the
final capture dump read-only, selects one scene graph from the actual
``FViewUniformShaderParameters`` bytes, resolves every figure to an exact
resource/pass/file version, decodes the *stored* format recorded in
``Base/SubresourceLayouts.json`` and writes deterministic PNG figures,
``figure-manifest.json`` and compact dump inventories.

No GPU work, no engine work, no writes outside ``figures/``.
"""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import mmap
import os
import re
import struct
import sys
import zlib
from pathlib import Path

FIG_DIR = Path(__file__).resolve().parent
ROOT = FIG_DIR.parent
PNG_DIR = FIG_DIR / "png"
INV_DIR = FIG_DIR / "inventory"

DEFAULT_DUMP = Path(
    r"D:\UnrealEngine\UnrealUnderstandingCapture\capture\buffers\gpudump\binary-20261112-153914"
)
# Portable evidence: sanitized, relative copies of only the reports this decoder
# needs.  Override with --report/--run for a different capture tree.
DEFAULT_REPORT = FIG_DIR / "evidence"
DEFAULT_RUN = FIG_DIR / "evidence" / "run.json"

SCHEMA = "unreal-understanding-final-figure-manifest-v2"

# ---------------------------------------------------------------- colors / font

BG = (10, 10, 10)
WHITE = (245, 245, 245)
GREY = (160, 160, 160)
DARKGREY = (70, 70, 70)
NAVY = (18, 24, 60)          # no geometry / not written
MAGENTA = (255, 0, 255)      # invalid / sentinel
YELLOW = (255, 230, 0)       # non-finite
BLACK = (0, 0, 0)            # valid zero
GREEN = (40, 200, 80)
ORANGE = (255, 150, 20)
BLUE = (60, 130, 255)
LIGHTGREY = (215, 215, 215)

# 3x5 bitmap font.  Every glyph is five rows of three characters.
_FONT = {
    "A": ("111", "101", "111", "101", "101"),
    "B": ("110", "101", "110", "101", "110"),
    "C": ("111", "100", "100", "100", "111"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "110", "100", "111"),
    "F": ("111", "100", "110", "100", "100"),
    "G": ("111", "100", "101", "101", "111"),
    "H": ("101", "101", "111", "101", "101"),
    "I": ("111", "010", "010", "010", "111"),
    "J": ("001", "001", "001", "101", "111"),
    "K": ("101", "101", "110", "101", "101"),
    "L": ("100", "100", "100", "100", "111"),
    "M": ("101", "111", "111", "101", "101"),
    "N": ("101", "111", "111", "111", "101"),
    "O": ("111", "101", "101", "101", "111"),
    "P": ("111", "101", "111", "100", "100"),
    "Q": ("111", "101", "101", "111", "001"),
    "R": ("111", "101", "111", "110", "101"),
    "S": ("111", "100", "111", "001", "111"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"),
    "V": ("101", "101", "101", "101", "010"),
    "W": ("101", "101", "111", "111", "101"),
    "X": ("101", "101", "010", "101", "101"),
    "Y": ("101", "101", "010", "010", "010"),
    "Z": ("111", "001", "010", "100", "111"),
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "001", "001", "001"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    ".": ("000", "000", "000", "000", "010"),
    ",": ("000", "000", "000", "010", "100"),
    ":": ("000", "010", "000", "010", "000"),
    ";": ("000", "010", "000", "010", "100"),
    "-": ("000", "000", "111", "000", "000"),
    "_": ("000", "000", "000", "000", "111"),
    "/": ("001", "001", "010", "100", "100"),
    "(": ("001", "010", "010", "010", "001"),
    ")": ("100", "010", "010", "010", "100"),
    "[": ("011", "010", "010", "010", "011"),
    "]": ("110", "010", "010", "010", "110"),
    "+": ("000", "010", "111", "010", "000"),
    "=": ("000", "111", "000", "111", "000"),
    "%": ("101", "001", "010", "100", "101"),
    "<": ("001", "010", "100", "010", "001"),
    ">": ("100", "010", "001", "010", "100"),
    "#": ("101", "111", "101", "111", "101"),
    "*": ("101", "010", "111", "010", "101"),
    "'": ("010", "010", "000", "000", "000"),
    '"': ("101", "101", "000", "000", "000"),
    "?": ("111", "001", "011", "000", "010"),
    "!": ("010", "010", "010", "000", "010"),
    " ": ("000", "000", "000", "000", "000"),
}


def glyph(ch):
    return _FONT.get(ch.upper(), _FONT["?"])


# ---------------------------------------------------------------- basic IO


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def load_json(path: Path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def iter_json_objects(path: Path):
    """Unreal DumpGPU append-only JSON: concatenated pretty objects."""
    text = path.read_text(encoding="utf-8", errors="replace")
    decoder = json.JSONDecoder()
    pos = 0
    n = len(text)
    while pos < n:
        while pos < n and text[pos] in " \t\r\n,":
            pos += 1
        if pos >= n:
            break
        obj, end = decoder.raw_decode(text, pos)
        yield obj
        pos = end


def fmt_hex(v) -> str:
    return "0x%08X" % (v & 0xFFFFFFFF)


def pct(sorted_values, q):
    if not sorted_values:
        return None
    i = min(len(sorted_values) - 1, max(0, int((len(sorted_values) - 1) * q)))
    return sorted_values[i]


# ---------------------------------------------------------------- raster / PNG


class Raster:
    """Tightly packed RGB8 raster."""

    __slots__ = ("w", "h", "rows")

    def __init__(self, w: int, h: int, fill=BG):
        self.w = w
        self.h = h
        self.rows = [bytearray(bytes(fill) * w) for _ in range(h)]

    def set(self, x, y, color):
        if 0 <= x < self.w and 0 <= y < self.h:
            i = x * 3
            r = self.rows[y]
            r[i] = color[0]
            r[i + 1] = color[1]
            r[i + 2] = color[2]

    def rect(self, x, y, w, h, color):
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.w, x + w), min(self.h, y + h)
        if x0 >= x1 or y0 >= y1:
            return
        buf = bytes(color) * (x1 - x0)
        for yy in range(y0, y1):
            i = x0 * 3
            self.rows[yy][i:i + len(buf)] = buf

    def blit(self, src: "Raster", dx: int, dy: int):
        for y in range(src.h):
            ty = dy + y
            if ty < 0 or ty >= self.h:
                continue
            n = min(src.w, self.w - dx)
            if n <= 0:
                continue
            self.rows[ty][dx * 3:dx * 3 + n * 3] = src.rows[y][:n * 3]

    def scaled(self, w: int, h: int) -> "Raster":
        out = Raster(w, h)
        for y in range(h):
            sy = min(self.h - 1, int(y * self.h / h))
            src = self.rows[sy]
            row = out.rows[y]
            for x in range(w):
                sx = min(self.w - 1, int(x * self.w / w))
                i = sx * 3
                j = x * 3
                row[j:j + 3] = src[i:i + 3]
        return out

    def draw_text(self, x: int, y: int, text: str, color=WHITE, scale: int = 2):
        cx = x
        for ch in text:
            g = glyph(ch)
            for gy, grow in enumerate(g):
                for gx, bit in enumerate(grow):
                    if bit == "1":
                        self.rect(cx + gx * scale, y + gy * scale, scale, scale, color)
            cx += 4 * scale

    def text_width(self, text: str, scale: int = 2) -> int:
        return max(0, len(text) * 4 * scale - scale)


def png_bytes(rast: Raster) -> bytes:
    raw = bytearray()
    for row in rast.rows:
        raw.append(0)
        raw.extend(row)

    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    out = b"\x89PNG\r\n\x1a\n"
    out += chunk(b"IHDR", struct.pack(">IIBBBBB", rast.w, rast.h, 8, 2, 0, 0, 0))
    out += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    out += chunk(b"IEND", b"")
    return out


def write_png(path: Path, rast: Raster) -> dict:
    data = png_bytes(rast)
    path.write_bytes(data)
    return {
        "file": path.relative_to(FIG_DIR).as_posix(),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "width": rast.w,
        "height": rast.h,
        "mode": "RGB8",
    }


def wrap_text(text: str, max_chars: int):
    words = text.split()
    lines = []
    cur = ""
    for w in words:
        cand = (cur + " " + w).strip()
        if len(cand) <= max_chars:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def compose_figure(content: Raster, title: str, subtitle: str, legend, notes):
    """Add a readable title/footer around the decoded data image."""
    width = max(640, min(1280, content.w + 16))
    header = 46
    legend = list(legend)
    max_chars = max(20, (width - 16) // 8)
    note_lines = []
    for n in notes:
        note_lines.extend(wrap_text(n, max_chars))
    cols = 2 if width >= 1000 else 1
    rows = (len(legend) + cols - 1) // cols
    footer = 10 + rows * 18 + (len(note_lines) * 14 if note_lines else 0) + 8
    height = header + content.h + footer
    c = Raster(width, height, BG)
    c.draw_text(8, 8, title, WHITE, 3)
    c.draw_text(8, 30, subtitle, GREY, 2)
    c.blit(content, (width - content.w) // 2, header)
    y = header + content.h + 8
    colw = width // cols
    for i, item in enumerate(legend):
        col = i % cols
        row = i // cols
        x = 8 + col * colw
        yy = y + row * 18
        label, color = item
        c.rect(x, yy + 2, 10, 10, color)
        c.rect(x, yy + 2, 10, 1, GREY)
        c.rect(x, yy + 11, 10, 1, GREY)
        c.draw_text(x + 15, yy + 1, label, WHITE, 2)
    y += rows * 18 + 4
    for line in note_lines:
        c.draw_text(8, y, line, GREY, 2)
        y += 14
    return c


def colorbar(rast: Raster, x: int, y: int, w: int, h: int, colors):
    n = max(1, len(colors))
    for i, col in enumerate(colors):
        x0 = x + int(i * w / n)
        x1 = x + int((i + 1) * w / n)
        rast.rect(x0, y, max(1, x1 - x0), h, col)


def lerp_color(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def srgb_oetf(v: float) -> float:
    if not math.isfinite(v) or v <= 0.0:
        return 0.0
    if v <= 0.0031308:
        return 12.92 * v
    return 1.055 * (v ** (1.0 / 2.4)) - 0.055


def u8(v: float) -> int:
    return max(0, min(255, int(round(v * 255.0))))


# ---------------------------------------------------------------- formats


def r11(bits):
    e = (bits >> 6) & 0x1F
    m = bits & 0x3F
    if e == 0:
        return (m / 64.0) * (2.0 ** -14)
    if e == 31:
        return math.inf if m == 0 else math.nan
    return (1.0 + m / 64.0) * (2.0 ** (e - 15))


def r10(bits):
    e = (bits >> 5) & 0x1F
    m = bits & 0x1F
    if e == 0:
        return (m / 32.0) * (2.0 ** -14)
    if e == 31:
        return math.inf if m == 0 else math.nan
    return (1.0 + m / 32.0) * (2.0 ** (e - 15))


def decode_r11g11b10(word):
    return (r11(word & 0x7FF), r11((word >> 11) & 0x7FF), r10((word >> 22) & 0x3FF))


def decode_a2b10(word):
    return (
        (word & 0x3FF) / 1023.0,
        ((word >> 10) & 0x3FF) / 1023.0,
        ((word >> 20) & 0x3FF) / 1023.0,
        ((word >> 30) & 0x3) / 3.0,
    )


class Texture:
    """Stored-format-driven view over one exact DumpGPU subresource file."""

    def __init__(self, dump: Path, rid: str, desc: dict, layout: dict):
        self.dump = dump
        self.rid = rid
        self.desc = desc
        self.layout = layout
        self.name = desc.get("Name")
        self.path = dump / layout["File"]
        self.stored_format = layout.get("StoredFormat")
        self.native_format = layout.get("NativeFormat")
        self.w = int(layout.get("ExtentX") or desc.get("ExtentX") or 1)
        self.h = int(layout.get("ExtentY") or desc.get("ExtentY") or 1)
        self.depth = int(layout.get("Depth") or 1)
        self.zcount = int(layout.get("ZSliceCount") or 1)
        self.mip = int(layout.get("MipLevel") or 0)
        self.slice = int(layout.get("FirstArraySlice") or 0)
        self.stencil = bool(layout.get("StencilSubresource"))
        self.file_bytes = self.path.stat().st_size
        denom = self.w * self.h * self.zcount
        self.bpp = self.file_bytes // denom if denom else 0
        self.kind = self._classify()
        self.row_bytes = int(layout.get("RowPitchBytes") or (self.w * self.bpp))
        self._mm = None
        self._row_y = None
        self._row = None
        self._row2_y = None
        self._row2 = None

    def _classify(self):
        f = self.stored_format
        b = self.bpp
        if f == "PF_FloatRGBA":
            if b == 8:
                return "half4"
            if b == 16:
                return "float4"
        if f in ("PF_FloatRGB", "PF_FloatR11G11B10"):
            if b == 4:
                return "r11g11b10"
            if b == 8:
                return "half3"
            if b == 12:
                return "float3"
        if f == "PF_A2B10G10R10" and b == 4:
            return "a2b10g10r10"
        if f == "PF_B8G8R8A8" and b == 4:
            return "bgra8"
        if f == "PF_R8G8B8A8" and b == 4:
            return "rgba8"
        if f == "PF_R8G8" and b == 2:
            return "r8g8"
        if f == "PF_R8" and b == 1:
            return "r8"
        if f == "PF_R8_UINT" and b == 1:
            return "r8uint"
        if f == "PF_R16F" and b == 2:
            return "r16f"
        if f == "PF_R16_UINT" and b == 2:
            return "r16ui"
        if f == "PF_G16" and b == 2:
            return "g16"
        if f == "PF_R32_FLOAT" and b == 4:
            return "r32f"
        if f == "PF_R32_UINT" and b == 4:
            return "r32ui"
        if f == "PF_R32G32_UINT" and b == 8:
            return "r32g32ui"
        if f == "PF_A32B32G32R32F" and b == 16:
            return "float4"
        raise RuntimeError(
            f"{self.name} ({self.rid}): no decoder for StoredFormat={f} bpp={b} "
            f"file={self.path.name} bytes={self.file_bytes} extent={self.w}x{self.h}x{self.zcount}"
        )

    @property
    def mm(self):
        if self._mm is None:
            f = open(self.path, "rb")
            self._fh = f
            self._mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        return self._mm

    def _load_row(self, y, z):
        y = max(0, min(self.h - 1, y))
        z = max(0, min(self.zcount - 1, z))
        key = (y, z)
        if self._row_y == key:
            return self._row
        off = (z * self.h + y) * self.row_bytes
        k = self.kind
        if k == "half4":
            n = self.w * 4
            row = struct.unpack_from("<%de" % n, self.mm, off)
        elif k == "float4":
            n = self.w * 4
            row = struct.unpack_from("<%df" % n, self.mm, off)
        elif k == "half3":
            n = self.w * 3
            row = struct.unpack_from("<%de" % n, self.mm, off)
        elif k == "float3":
            n = self.w * 3
            row = struct.unpack_from("<%df" % n, self.mm, off)
        elif k in ("bgra8", "rgba8"):
            row = self.mm[off:off + self.w * 4]
        elif k == "r8g8":
            row = self.mm[off:off + self.w * 2]
        elif k == "r8" or k == "r8uint":
            row = self.mm[off:off + self.w]
        elif k == "r16f":
            row = struct.unpack_from("<%de" % self.w, self.mm, off)
        elif k == "r16ui":
            row = struct.unpack_from("<%dH" % self.w, self.mm, off)
        elif k == "g16":
            row = struct.unpack_from("<%dH" % self.w, self.mm, off)
        elif k == "r32f":
            row = struct.unpack_from("<%df" % self.w, self.mm, off)
        elif k == "r32ui":
            row = struct.unpack_from("<%dI" % self.w, self.mm, off)
        elif k == "r32g32ui":
            row = struct.unpack_from("<%dI" % (self.w * 2), self.mm, off)
        elif k in ("r11g11b10", "a2b10g10r10"):
            row = struct.unpack_from("<%dI" % self.w, self.mm, off)
        else:
            raise RuntimeError("unreachable " + k)
        self._row_y = key
        self._row = row
        return row

    def texel(self, x, y, z=0):
        x = max(0, min(self.w - 1, x))
        row = self._load_row(y, z)
        k = self.kind
        if k == "half4" or k == "float4":
            i = x * 4
            return (float(row[i]), float(row[i + 1]), float(row[i + 2]), float(row[i + 3]))
        if k == "half3" or k == "float3":
            i = x * 3
            return (float(row[i]), float(row[i + 1]), float(row[i + 2]), 1.0)
        if k == "bgra8":
            i = x * 4
            return (row[i + 2] / 255.0, row[i + 1] / 255.0, row[i] / 255.0, row[i + 3] / 255.0)
        if k == "rgba8":
            i = x * 4
            return (row[i] / 255.0, row[i + 1] / 255.0, row[i + 2] / 255.0, row[i + 3] / 255.0)
        if k == "r8g8":
            i = x * 2
            return (row[i] / 255.0, row[i + 1] / 255.0, 0.0, 1.0)
        if k == "r8":
            return (row[x] / 255.0, 0.0, 0.0, 1.0)
        if k == "r8uint":
            return (float(row[x]), 0.0, 0.0, 1.0)
        if k == "r16f":
            return (float(row[x]), 0.0, 0.0, 1.0)
        if k == "r16ui":
            return (float(row[x]), 0.0, 0.0, 1.0)
        if k == "g16":
            return (row[x] / 65535.0, 0.0, 0.0, 1.0)
        if k == "r32f":
            return (float(row[x]), 0.0, 0.0, 1.0)
        if k == "r32ui":
            return (float(row[x]), 0.0, 0.0, 1.0)
        if k == "r32g32ui":
            return (float(row[x * 2]), float(row[x * 2 + 1]), 0.0, 1.0)
        if k == "r11g11b10":
            return decode_r11g11b10(row[x]) + (1.0,)
        if k == "a2b10g10r10":
            return decode_a2b10(row[x])
        raise RuntimeError("unreachable")

    def raw_u32(self, x, y, z=0, word=0):
        x = max(0, min(self.w - 1, x))
        row = self._load_row(y, z)
        if self.kind == "r32ui":
            return int(row[x])
        if self.kind == "r32g32ui":
            return int(row[x * 2 + word])
        if self.kind in ("r11g11b10", "a2b10g10r10"):
            return int(row[x])
        raise RuntimeError(f"raw_u32 not valid for {self.kind}")

    def close(self):
        if self._mm is not None:
            self._mm.close()
            self._mm = None
            self._fh.close()


def stored_format_provenance(layout: dict, tex: Texture) -> dict:
    return {
        "stored_format": layout.get("StoredFormat"),
        "native_format": layout.get("NativeFormat"),
        "view_format": layout.get("ViewFormat"),
        "staging_format": layout.get("StagingFormat"),
        "storage_bytes_per_texel": tex.bpp,
        "decoder_storage_class": tex.kind,
        "native_value_interpretation": layout.get("NativeValueInterpretation"),
        "stored_bytes_are_native_storage": layout.get("StoredBytesAreNativeStorage"),
        "bit_exact": layout.get("BitExact"),
        "value_preserving": layout.get("ValuePreserving"),
        "bit_exact_condition": layout.get("BitExactCondition"),
        "native_srgb": layout.get("NativeSRGB"),
        "view_srgb": layout.get("ViewSRGB"),
        "readback": layout.get("Readback"),
        "dump_status": layout.get("DumpStatus"),
    }


# ---------------------------------------------------------------- dump context

VERSION_RE = re.compile(r"\.v([0-9a-f]{20})\.bin$")


def version_pointer(layout: dict):
    m = VERSION_RE.search(layout.get("File") or "")
    return m.group(1) if m else None


class Context:
    def __init__(self, dump: Path, report: Path, run_path: Path, graph: str | None = None):
        self.dump = dump
        self.report = report
        self.run_path = run_path
        self.base = dump / "Base"
        if not (self.base / "ResourceDescs.json").exists():
            raise RuntimeError(f"not a DumpGPU dump: {dump}")

        self.report_summary = load_json(report / "report-summary.json")
        self.run = load_json(run_path)
        if Path(self.report_summary.get("dump", "")).name != dump.name:
            raise RuntimeError(
                f"report-summary dump mismatch: {self.report_summary.get('dump')} != {dump}"
            )
        if Path(self.run.get("dump_directory", "")).name != dump.name:
            raise RuntimeError(
                f"run dump_directory mismatch: {self.run.get('dump_directory')} != {dump}"
            )

        self.resources = list(iter_json_objects(self.base / "ResourceDescs.json"))
        self.res_by_id = {r["UniqueResourceName"]: r for r in self.resources}
        self.layouts = list(iter_json_objects(self.base / "SubresourceLayouts.json"))
        self.layouts_by_res = {}
        for l in self.layouts:
            self.layouts_by_res.setdefault(l.get("Resource"), []).append(l)
        self.passes = list(iter_json_objects(self.base / "Passes.json"))
        self.passes.sort(key=lambda p: int(p.get("Id") or -1))
        self.pass_by_ptr = {p.get("Pointer"): p for p in self.passes}
        self.pass_by_id = {int(p.get("Id")): p for p in self.passes}
        self.manifest = load_json(report / "manifest.json")
        self.manifest_files = {f["path"]: f for f in self.manifest.get("files", [])}
        self.source_hash_cache = {}
        self.textures = []
        self.figures = []
        self.source_checks = []
        self.unavailable = []
        self.requested_graph = graph
        self._select_graph()
        self._build_inventory()

    # ------------------------------------------------------------ selection

    def _decode_view(self, payload: dict):
        meta_path = self.dump / "StructuresMetadata" / (str(payload["metadata"]) + ".json")
        bin_path = self.dump / "Structures" / (str(payload["content"]) + ".bin")
        meta = load_json(meta_path)
        if meta.get("StructTypeName") != "FViewUniformShaderParameters":
            raise RuntimeError(f"metadata {meta_path} is {meta.get('StructTypeName')}")
        members = {m["Name"]: m for m in meta.get("Members", [])}
        b = bin_path.read_bytes()

        def dec(name):
            m = members.get(name)
            if not m:
                return None
            o = int(m["Offset"])
            n = max(1, int(m.get("NumColumns") or 1))
            t = m.get("BaseType")
            if t == "UBMT_FLOAT32":
                return [float(x) for x in struct.unpack_from("<" + "f" * n, b, o)]
            if t == "UBMT_UINT32":
                return [int(x) for x in struct.unpack_from("<" + "I" * n, b, o)]
            if t == "UBMT_INT32":
                return [int(x) for x in struct.unpack_from("<" + "i" * n, b, o)]
            return None

        return {
            "content": str(payload["content"]),
            "metadata": str(payload["metadata"]),
            "bin_bytes": len(b),
            "bin_sha256": hashlib.sha256(b).hexdigest(),
            "view_rect_min": dec("ViewRectMin"),
            "view_size_and_inv_size": dec("ViewSizeAndInvSize"),
            "state_frame_index_mod8": dec("StateFrameIndexMod8"),
            "inv_device_z_to_world_z_transform": dec("InvDeviceZToWorldZTransform"),
            "near_plane": dec("NearPlane"),
            "view_forward": dec("ViewForward"),
            "view_up": dec("ViewUp"),
            "view_right": dec("ViewRight"),
            "max_pass_id": max(
                [int(p.get("Id") or -1) for p in payload.get("_passes", [])] or [-1]
            ),
        }

    def _select_graph(self):
        sg_path = self.report / "scene-graphs.json"
        if not sg_path.exists():
            raise RuntimeError(f"missing {sg_path}; cannot select a graph from View bytes")
        sg = load_json(sg_path)
        payloads = sg.get("all_view_payloads") or []
        if not payloads:
            raise RuntimeError("scene-graphs.json has no all_view_payloads")
        # Group references by graph prefix and unique View content.
        grouped = {}
        for p in payloads:
            pref = str(p.get("graph_prefix") or p.get("content", "")[:4])
            key = str(p.get("content"))
            g = grouped.setdefault(pref, {})
            item = g.setdefault(
                key,
                {
                    "content": key,
                    "metadata": str(p.get("metadata")),
                    "graph_prefix": pref,
                    "passes": [],
                    "bin_sha256": p.get("bin_sha256"),
                    "bin_bytes": p.get("bin_bytes"),
                },
            )
            item["passes"].append(p)
        candidates = []
        self.view_payloads = {}
        self.pass_view = {}
        for pref, contents in sorted(grouped.items()):
            infos = []
            for content, item in sorted(contents.items()):
                info = self._decode_view(item)
                info["graph_prefix"] = pref
                # `pass` in the report is the pass pointer; resolve it to the numeric id.
                pids = []
                for q in item["passes"]:
                    pid = q.get("pass")
                    # report `pass` is the pass pointer, not the numeric id.
                    pp = self.pass_by_ptr.get(pid)
                    if pp is not None:
                        pids.append(int(pp.get("Id")))
                    if pid:
                        self.pass_view[pid] = info
                info["max_pass_id"] = max(pids) if pids else -1
                info["pass_references"] = len(item["passes"])
                infos.append(info)
            self.view_payloads[pref] = infos
            main = [
                v for v in infos
                if v["view_rect_min"] == [0.0, 0.0, 0.0, 0.0]
                and v["view_size_and_inv_size"]
                and abs(v["view_size_and_inv_size"][0] - 1280.0) < 1e-6
                and abs(v["view_size_and_inv_size"][1] - 720.0) < 1e-6
            ]
            if len(main) == 1:
                candidates.append((pref, main[0]))
        if not candidates:
            raise RuntimeError("no graph has exactly one 1280x720 view payload at rect (0,0)")
        # Final graph = the graph whose 1280x720 View bytes are referenced by the
        # highest-numbered pass in the dump (submission order is pass order).
        candidates.sort(key=lambda c: (c[1]["max_pass_id"], c[0]))
        if self.requested_graph is None:
            self.selected_graph, self.selected_view = candidates[-1]
        else:
            selected = next((candidate for candidate in candidates if candidate[0] == self.requested_graph), None)
            if selected is None:
                raise RuntimeError(f"graph {self.requested_graph} has no matching camera View payload")
            self.selected_graph, self.selected_view = selected
        self.candidates = candidates
        self.view_payloads = {
            pref: sorted(infos, key=lambda v: v["content"]) for pref, infos in self.view_payloads.items()
        }
        if self.selected_view["max_pass_id"] < 0:
            raise RuntimeError("selected View payload has no pass reference")
        if not self.selected_view["view_size_and_inv_size"]:
            raise RuntimeError("ViewSizeAndInvSize could not be decoded")
        if not self.selected_view["inv_device_z_to_world_z_transform"]:
            raise RuntimeError("InvDeviceZToWorldZTransform could not be decoded")

        # Cross-check against the report, and prove the graph prefix is unique.
        for v in self.view_payloads[self.selected_graph]:
            if v["content"] == self.selected_view["content"]:
                if [round(x, 6) for x in v["view_size_and_inv_size"][:2]] != [1280.0, 720.0]:
                    raise RuntimeError("selected view is not 1280x720 after decode")

    def selected_prefix(self) -> str:
        return self.selected_graph

    # ------------------------------------------------------------ resources

    def resource(self, name: str, rid: str | None = None):
        if rid:
            r = self.res_by_id.get(rid)
            if not r:
                raise RuntimeError(f"resource id not found: {rid}")
            return r
        matches = [
            r for r in self.resources
            if r.get("Name") == name
            and r["UniqueResourceName"].startswith(self.selected_graph)
        ]
        if not matches:
            raise RuntimeError(f"no resource named {name!r} in graph {self.selected_graph}")
        return sorted(matches, key=lambda r: r["UniqueResourceName"])[0]

    def layout_for(self, rid: str, event=None, event_prefix=None, latest=False,
                   stencil=False, mip=0, slice_index=None, allow_v0=False, before_pass=None):
        best = None
        matches = 0
        for l in self.layouts_by_res.get(rid, []):
            if bool(l.get("StencilSubresource")) != bool(stencil):
                continue
            if int(l.get("MipLevel") or 0) != int(mip):
                continue
            if slice_index is not None and int(l.get("FirstArraySlice") or 0) != int(slice_index):
                continue
            ptr = version_pointer(l)
            p = self.pass_by_ptr.get(ptr) if ptr else None
            pid = int(p.get("Id")) if p else -1
            if before_pass is not None and pid >= before_pass:
                continue
            ev = (p or {}).get("EventName") or ""
            score = 0
            if event is not None:
                if ev != event:
                    continue
                score += 1000
            if event_prefix is not None:
                if not ev.startswith(event_prefix):
                    continue
                score += 500
            if latest:
                score += pid
            elif event is None and event_prefix is None:
                score += pid
            if ptr and ptr != "0" * 20 and p is None:
                # Version pointer that does not map to any pass is not usable.
                continue
            if ptr == "0" * 20 and not allow_v0:
                continue
            matches += 1
            cand = (score, pid, l)
            if best is None or cand[:2] > best[:2]:
                best = cand
        if best is None:
            raise RuntimeError(
                f"no subresource for {rid} ({self.res_by_id.get(rid, {}).get('Name')}) "
                f"event={event!r} prefix={event_prefix!r} latest={latest} stencil={stencil} mip={mip}"
            )
        return best[2]

    def texture(self, name: str, rid: str | None = None, **kw):
        if rid:
            candidates = [self.resource(name, rid)]
        else:
            candidates = [
                r for r in self.resources
                if r.get("Name") == name and r["UniqueResourceName"].startswith(self.selected_graph)
            ]
        if not candidates:
            raise RuntimeError(f"no resource named {name!r} in graph {self.selected_graph}")
        event = kw.get("event")
        event_prefix = kw.get("event_prefix")
        best = None
        errors = []
        for r in candidates:
            try:
                l = self.layout_for(r["UniqueResourceName"], **kw)
            except RuntimeError as e:
                errors.append(str(e))
                continue
            p = self.pass_of(l)
            pid = int(p.get("Id")) if p else -1
            score = pid
            if event is not None:
                score += 10_000
            if event_prefix is not None:
                score += 5_000
            if best is None or score > best[0] or (score == best[0] and r["UniqueResourceName"] < best[1]["UniqueResourceName"]):
                best = (score, r, l, pid)
        if best is None:
            raise RuntimeError(
                f"no matching subresource across {len(candidates)} resource(s) named {name!r}; "
                + " | ".join(errors[:4])
            )
        _, r, l, _ = best
        t = Texture(self.dump, r["UniqueResourceName"], r, l)
        self.textures.append(t)
        return t, r, l

    def pass_of(self, layout: dict):
        ptr = version_pointer(layout)
        return self.pass_by_ptr.get(ptr) if ptr else None

    def verify_source(self, layout: dict) -> dict:
        rel = layout["File"]
        p = self.dump / rel
        if not p.exists():
            raise RuntimeError(f"missing payload file {p}")
        expected = self.manifest_files.get(rel)
        if expected is None:
            raise RuntimeError(f"payload {rel} is absent from report/manifest.json")
        actual_bytes = p.stat().st_size
        if actual_bytes != int(expected["bytes"]):
            raise RuntimeError(
                f"payload size mismatch {rel}: {actual_bytes} != {expected['bytes']}"
            )
        if rel not in self.source_hash_cache:
            self.source_hash_cache[rel] = sha256_file(p)
        actual_sha = self.source_hash_cache[rel]
        if actual_sha != expected["sha256"]:
            raise RuntimeError(f"payload sha256 mismatch {rel}")
        self.source_checks.append(
            {"file": rel, "bytes": actual_bytes, "sha256": actual_sha, "manifest_match": True}
        )
        return {"file": rel, "bytes": actual_bytes, "sha256": actual_sha, "manifest_match": True}

    def layout_at_version(self, rid: str, version_ptr: str, stencil=False, mip=0):
        """Exact subresource layout whose version pointer is ``version_ptr``."""
        for l in self.layouts_by_res.get(rid, []):
            if version_pointer(l) != version_ptr:
                continue
            if bool(l.get("StencilSubresource")) != bool(stencil):
                continue
            if int(l.get("MipLevel") or 0) != int(mip):
                continue
            return l
        raise RuntimeError(f"no layout for {rid} at exact version {version_ptr}")

    def previous_layout(self, rid: str, before_pass: int, stencil=False, mip=0):
        """Latest layout of ``rid`` whose version pass is strictly before ``before_pass``."""
        best = None
        for l in self.layouts_by_res.get(rid, []):
            if bool(l.get("StencilSubresource")) != bool(stencil):
                continue
            if int(l.get("MipLevel") or 0) != int(mip):
                continue
            vp = version_pointer(l)
            p = self.pass_by_ptr.get(vp) if vp else None
            if p is None:
                continue
            pid = int(p.get("Id"))
            if pid >= before_pass:
                continue
            if best is None or pid > best[0]:
                best = (pid, l)
        return best[1] if best else None

    def buffer_payload(self, name: str, rid: str | None = None, latest=True):
        """Exact dumped buffer payload, selected by version pass id (no GPU work)."""
        res = self.resource(name, rid)
        rid = res["UniqueResourceName"]
        pat = re.compile(
            r"^Resources/" + re.escape(rid)
            + r"(?P<linear>\.linear)?(?:\.mip\d+)?\.v(?P<vp>[0-9a-f]{20})\.bin$"
        )
        cands = []
        for rel, mf in self.manifest_files.items():
            m = pat.match(rel)
            if not m:
                continue
            vp = m.group("vp")
            p = self.pass_by_ptr.get(vp)
            pid = int(p.get("Id")) if p else -1
            cands.append((pid, not bool(m.group("linear")), vp, rel, mf))
        if not cands:
            raise RuntimeError(f"no dumped buffer payload for {name} ({rid})")
        cands.sort(key=lambda c: (c[0] if latest else -c[0], c[1]))
        pid, _, vp, rel, mf = cands[-1]
        path = self.dump / rel
        if not path.exists():
            raise RuntimeError(f"missing buffer payload {path}")
        actual_bytes = path.stat().st_size
        if actual_bytes != int(mf["bytes"]):
            raise RuntimeError(f"buffer size mismatch {rel}: {actual_bytes} != {mf['bytes']}")
        if rel not in self.source_hash_cache:
            self.source_hash_cache[rel] = sha256_file(path)
        actual_sha = self.source_hash_cache[rel]
        if actual_sha != mf["sha256"]:
            raise RuntimeError(f"buffer sha256 mismatch {rel}")
        self.source_checks.append(
            {"file": rel, "bytes": actual_bytes, "sha256": actual_sha, "manifest_match": True}
        )
        return {"resource_id": rid, "name": name, "file": rel, "bytes": actual_bytes,
                "sha256": actual_sha, "version_pointer": vp, "pass_id": pid, "path": path}

    def page_occupancy(self):
        """Decode Lumen.PageBuffer's 5-float4-per-page physical atlas mapping.

        Mapped test is SizeInTexels.x != 0 (and .y != 0), per
        resource.lumen-card-page-data in the Lumen source schema.  The physical
        rectangle is uint2(PhysicalAtlasUVRect.xy * PhysicalAtlasSize + 0.5),
        with the max corner from .zw.  SizeInTexels.x == 0 is unmapped/stale.
        """
        if getattr(self, "_page_occupancy", None) is not None:
            return self._page_occupancy
        alb = self.resource("Lumen.SceneAlbedo")
        atlas = int(alb.get("ExtentX") or 0)
        if atlas != int(alb.get("ExtentY") or 0) or atlas <= 0:
            raise RuntimeError(f"unexpected Lumen.SceneAlbedo extent {alb.get('ExtentX')}x{alb.get('ExtentY')}")
        bp = self.buffer_payload("Lumen.PageBuffer", latest=True)
        b = bp["path"].read_bytes()
        stride = 80
        if len(b) % stride:
            raise RuntimeError(f"PageBuffer byte size {len(b)} is not a multiple of {stride}")
        pages = []
        for i in range(len(b) // stride):
            o = i * stride
            card_index = struct.unpack_from("<I", b, o)[0]
            sx, sy = struct.unpack_from("<2f", b, o + 8)
            uv = struct.unpack_from("<4f", b, o + 32)
            frame = struct.unpack_from("<4I", b, o + 64)
            if not (math.isfinite(sx) and math.isfinite(sy) and all(math.isfinite(v) for v in uv)):
                continue
            if sx <= 0.0 or sy <= 0.0:
                continue
            x0 = int(math.floor(uv[0] * atlas + 0.5))
            y0 = int(math.floor(uv[1] * atlas + 0.5))
            x1 = int(math.floor(uv[2] * atlas + 0.5))
            y1 = int(math.floor(uv[3] * atlas + 0.5))
            if not (0 <= x0 < x1 <= atlas and 0 <= y0 < y1 <= atlas):
                continue
            pages.append({
                "page_index": i,
                "card_index": card_index,
                "size_texels": [round(sx, 6), round(sy, 6)],
                "physical_atlas_uv_rect": [round(v, 9) for v in uv],
                "physical_atlas_rect_xyxy": [x0, y0, x1, y1],
                "size_matches_rect": abs((x1 - x0) - sx) <= 1.0 and abs((y1 - y0) - sy) <= 1.0,
                "last_direct_lighting_update_frame": frame[0],
                "last_indirect_lighting_update_frame": frame[1],
            })
        if not pages:
            raise RuntimeError("Lumen.PageBuffer has no mapped page")
        bbox = [min(p["physical_atlas_rect_xyxy"][0] for p in pages),
                min(p["physical_atlas_rect_xyxy"][1] for p in pages),
                max(p["physical_atlas_rect_xyxy"][2] for p in pages),
                max(p["physical_atlas_rect_xyxy"][3] for p in pages)]
        self._page_occupancy = {
            "resource_id": bp["resource_id"], "name": bp["name"],
            "payload": {k: bp[k] for k in ("file", "bytes", "sha256")},
            "version_pointer": bp["version_pointer"], "pass_id": bp["pass_id"],
            "pass_event": self.pass_by_id.get(bp["pass_id"], {}).get("EventName"),
            "page_stride_bytes": stride, "page_count": len(b) // stride,
            "mapped_page_count": len(pages), "atlas_size": atlas,
            "mapped_bbox_xyxy": bbox, "pages": pages,
            "mapped_test": "SizeInTexels.x>0 and SizeInTexels.y>0; physical rect from PhysicalAtlasUVRect*AtlasSize+0.5",
            "source_schema": "resource.lumen-card-page-data (Lumen.PageBuffer, 5 float4/page, SizeInTexels.x==0 unmapped)",
        }
        return self._page_occupancy

    def occupancy_mask(self, crop):
        """Exact union of mapped Lumen.PageBuffer physical page rectangles."""
        occ = self.page_occupancy()
        x0, y0, w, h = crop
        mask = [False] * (w * h)
        for p in occ["pages"]:
            px0, py0, px1, py1 = p["physical_atlas_rect_xyxy"]
            ix0, iy0 = max(x0, px0), max(y0, py0)
            ix1, iy1 = min(x0 + w, px1), min(y0 + h, py1)
            if ix1 <= ix0 or iy1 <= iy0:
                continue
            for yy in range(iy0, iy1):
                row = (yy - y0) * w
                for xx in range(ix0, ix1):
                    mask[row + (xx - x0)] = True
        return mask

    def card_capture_rects(self):
        """Every CardPage Commands pass in the selected graph with its own View rect."""
        out = []
        for p in self.passes:
            if not str(p.get("Pointer") or "").startswith(self.selected_graph):
                continue
            if p.get("EventName") != "CardPage Commands:1":
                continue
            v = self.pass_view.get(p.get("Pointer"))
            if not v or not v.get("view_rect_min") or not v.get("view_size_and_inv_size"):
                continue
            r, s = v["view_rect_min"], v["view_size_and_inv_size"]
            x0, y0 = int(round(r[0])), int(round(r[1]))
            w, h = int(round(s[0])), int(round(s[1]))
            if w <= 0 or h <= 0:
                continue
            out.append({
                "pass_id": int(p.get("Id")), "pass_pointer": p.get("Pointer"),
                "event": p.get("EventName"), "view": v,
                "rect_xywh": [x0, y0, w, h],
            })
        return out

    def card_capture_choice(self):
        """Pick one exact-pass live card capture; prove its update rect by version diff."""
        if getattr(self, "_card_choice", None) is not None:
            return self._card_choice
        names = ["Lumen.CardCaptureAlbedoAtlas", "Lumen.CardCaptureNormalAtlas",
                 "Lumen.CardCaptureEmissiveAtlas", "Lumen.CardCaptureDepthStencilAtlas"]
        rids = {n: self.resource(n)["UniqueResourceName"] for n in names}
        candidates = []
        for rec in self.card_capture_rects():
            vp = rec["pass_pointer"]
            x0, y0, w, h = rec["rect_xywh"]
            try:
                layouts = {n: self.layout_at_version(rids[n], vp) for n in names}
            except RuntimeError:
                continue
            quality = {"coverage": 0, "finite_emissive": 0, "depth_valid": 0}
            for n, key in (("Lumen.CardCaptureNormalAtlas", "coverage"),
                           ("Lumen.CardCaptureEmissiveAtlas", "finite_emissive"),
                           ("Lumen.CardCaptureDepthStencilAtlas", "depth_valid")):
                rid = rids[n]
                t = Texture(self.dump, rid, self.res_by_id[rid], layouts[n])
                try:
                    if key == "coverage":
                        for yy in range(y0, y0 + h):
                            for xx in range(x0, x0 + w):
                                if t.texel(xx, yy)[3] > 0.5:
                                    quality[key] += 1
                    elif key == "finite_emissive":
                        for yy in range(y0, y0 + h):
                            for xx in range(x0, x0 + w):
                                if all(math.isfinite(c) for c in t.texel(xx, yy)[:3]):
                                    quality[key] += 1
                    else:
                        for yy in range(y0, y0 + h):
                            for xx in range(x0, x0 + w):
                                d = t.texel(xx, yy)[0]
                                if math.isfinite(d) and 0.0 <= d <= 1.0:
                                    quality[key] += 1
                finally:
                    t.close()
            area = w * h
            candidates.append({**rec, "layouts": layouts, "area": area, **quality})
        full = [c for c in candidates
                if c["coverage"] == c["area"] and c["finite_emissive"] == c["area"]
                and c["depth_valid"] == c["area"]]
        pool = full or candidates
        if not pool:
            raise RuntimeError("no CardPage Commands pass with an exact View rect")
        # Largest clean live card, then latest pass id (final card of the frame).
        chosen = max(pool, key=lambda c: (c["area"], c["pass_id"]))
        vp = chosen["pass_pointer"]
        x0, y0, w, h = chosen["rect_xywh"]
        rect = [x0, y0, w, h]

        # Exact same-pass versions and payload hashes for all four resources.
        panels = {}
        for n in names:
            l = self.layout_at_version(rids[n], vp)
            p = self.pass_of(l)
            payload = self.verify_source(l)
            panels[n] = {
                "resource_id": rids[n], "layout": l, "payload": payload,
                "version_pointer": version_pointer(l),
                "version_pass_id": int(p.get("Id")) if p else None,
                "version_pass_event": p.get("EventName") if p else None,
            }
        for n, panel in panels.items():
            if panel["version_pointer"] != vp or panel["version_pass_id"] != chosen["pass_id"]:
                raise RuntimeError(f"{n} is not the same-pass version {vp} for pass {chosen['pass_id']}")

        # Previous version of each resource, for a byte-level written-rect proof.
        write_proof = {}
        for n in names:
            lprev = self.previous_layout(rids[n], chosen["pass_id"])
            if lprev is None:
                continue
            lp = self.pass_of(lprev)
            a = self.dump / panels[n]["layout"]["File"]
            b = self.dump / lprev["File"]
            ab, bb = a.read_bytes(), b.read_bytes()
            if len(ab) != len(bb):
                write_proof[n] = {"previous_version_pointer": version_pointer(lprev),
                                  "error": "payload byte length changed"}
                continue
            bpp = len(ab) // (512 * 512)
            inside = outside = 0
            for yy in range(512):
                for xx in range(512):
                    i = (yy * 512 + xx) * bpp
                    if ab[i:i + bpp] != bb[i:i + bpp]:
                        if x0 <= xx < x0 + w and y0 <= yy < y0 + h:
                            inside += 1
                        else:
                            outside += 1
            write_proof[n] = {
                "previous_version_pointer": version_pointer(lprev),
                "previous_pass_id": int(lp.get("Id")) if lp else None,
                "changed_inside_rect": inside, "changed_outside_rect": outside,
            }

        # Clear pass provenance and the full-frame union of current capture rects.
        clear = next((p for p in self.passes
                      if str(p.get("Pointer") or "").startswith(self.selected_graph)
                      and str(p.get("EventName") or "").startswith("ClearCardCapture")), None)
        all_rects = self.card_capture_rects()
        prior_rects = [r for r in all_rects if r["pass_id"] < chosen["pass_id"]]
        prior_mask = [False] * (512 * 512)
        for rec in prior_rects:
            rx, ry, rw, rh = rec["rect_xywh"]
            for yy in range(ry, ry + rh):
                for xx in range(rx, rx + rw):
                    prior_mask[yy * 512 + xx] = True
        reuse_px = 0
        for yy in range(y0, y0 + h):
            for xx in range(x0, x0 + w):
                if prior_mask[yy * 512 + xx]:
                    reuse_px += 1
        full = [False] * (512 * 512)
        for rec in all_rects:
            rx, ry, rw, rh = rec["rect_xywh"]
            for yy in range(ry, ry + rh):
                for xx in range(rx, rx + rw):
                    full[yy * 512 + xx] = True
        normal = Texture(self.dump, rids["Lumen.CardCaptureNormalAtlas"],
                         self.res_by_id[rids["Lumen.CardCaptureNormalAtlas"]],
                         panels["Lumen.CardCaptureNormalAtlas"]["layout"])
        try:
            stale_alpha = 0
            raw_alpha_outside = 0
            alpha_inside_union = 0
            for yy in range(512):
                for xx in range(512):
                    a = normal.texel(xx, yy)[3]
                    if full[yy * 512 + xx]:
                        if a > 0.5:
                            alpha_inside_union += 1
                    elif a > 0.0:
                        # Raw non-zero alpha outside this frame's rects is stale
                        # (previous-frame/undefined); below 0.5 it is not a valid flag.
                        raw_alpha_outside += 1
                        if a > 0.5:
                            stale_alpha += 1
        finally:
            normal.close()

        # Crop with an 8-texel border so the categorical outside-mask colour is visible.
        margin = 8
        cx0, cy0 = max(0, x0 - margin), max(0, y0 - margin)
        cx1, cy1 = min(512, x0 + w + margin), min(512, y0 + h + margin)
        crop = (cx0, cy0, cx1 - cx0, cy1 - cy0)
        nmask = [False] * (crop[2] * crop[3])
        normal = Texture(self.dump, rids["Lumen.CardCaptureNormalAtlas"],
                         self.res_by_id[rids["Lumen.CardCaptureNormalAtlas"]],
                         panels["Lumen.CardCaptureNormalAtlas"]["layout"])
        try:
            for yy in range(y0, y0 + h):
                for xx in range(x0, x0 + w):
                    out_i = (yy - cy0) * crop[2] + (xx - cx0)
                    # Target1.a > 0.5 is the source-proved card valid flag.
                    nmask[out_i] = normal.texel(xx, yy)[3] > 0.5
        finally:
            normal.close()
        common = sum(1 for v in nmask if v)

        self._card_choice = {
            "pass": chosen, "panels": panels, "rect_xywh": rect, "crop_xywh": list(crop),
            "margin": margin, "mask": nmask, "common_valid_pixels": common,
            "write_proof": write_proof,
            "clear": {"pass_id": int(clear.get("Id")) if clear else None,
                      "pass_pointer": clear.get("Pointer") if clear else None,
                      "event": clear.get("EventName") if clear else None},
            "all_capture_rects": [{k: r[k] for k in ("pass_id", "pass_pointer", "rect_xywh")}
                                  for r in all_rects],
            "current_frame_union_pixels": sum(1 for v in full if v),
            "prior_rect_overlap_pixels": reuse_px,
            "target1a_alpha_inside_union": alpha_inside_union,
            "target1a_alpha_outside_current_frame_union": stale_alpha,
            "target1a_raw_nonzero_outside_current_frame_union": raw_alpha_outside,
            "mask_rule": ("exact pass ViewRectMin/ViewSizeAndInvSize rectangle "
                          "AND Lumen.CardCaptureNormalAtlas.a > 0.5 (Target1.a)"),
            "source_schema": "cards.resource.card-capture-depthstencil gap: validity comes from CardCaptureNormalAtlas.a > 0.5 (LumenSurfaceCache.usf:64)",
        }
        return self._card_choice

    # ------------------------------------------------------------ inventory

    def _build_inventory(self):
        self.inventory_summary = {
            "schema": "unreal-understanding-final-dump-inventory-v1",
            "dump": str(self.dump),
            "run": portable_path(self.run_path),
            "report": portable_path(self.report),
            "dump_status": self.report_summary.get("status"),
            "captured_utc": self.report_summary.get("captured_utc"),
            "engine_frame": 292,
            "passes": len(self.passes),
            "resources": len(self.resources),
            "subresource_layouts": len(self.layouts),
            "resource_binary_files": self.report_summary.get("resource_binary_files"),
            "selected_graph": self.selected_graph,
            "graph_prefixes": sorted(self.view_payloads),
        }
        # Compact pass inventory.
        pass_rows = []
        for p in self.passes:
            ins, outs = [], []
            for nm in p.get("InputResources") or []:
                base = re.split(r"[.\[]", nm)[0]
                if base not in ins:
                    ins.append(base)
            for nm in p.get("OutputResources") or []:
                base = re.split(r"[.\[]", nm)[0]
                if base not in outs:
                    outs.append(base)
            pass_rows.append({
                "id": int(p.get("Id")),
                "graph": str(p.get("Pointer") or "")[:4],
                "event": p.get("EventName"),
                "pointer": p.get("Pointer"),
                "parameters_name": p.get("ParametersName"),
                "parent_scopes": p.get("ParentEventScopes") or [],
                "input_ids": ins,
                "output_ids": outs,
            })
        # Resource + payload inventory.  Include every Resources/*.bin file, not
        # only the files that have a SubresourceLayouts record (buffers do not).
        file_re = re.compile(
            r"^Resources/(?P<rid>[0-9a-f]{20})(?P<stencil>\.stencil)?"
            r"(?P<arr>\.\[\d+\])?(?P<linear>\.linear)?"
            r"(?:\.mip(?P<mip>\d+))?\.v(?P<ptr>[0-9a-f]{20})\.bin$"
        )
        layout_by_file = {l.get("File"): l for l in self.layouts}
        files_by_res = {}
        res_rows = []
        payload_rows = []
        for rel in sorted(p for p in self.manifest_files if p.startswith("Resources/")):
            m = file_re.match(rel)
            if not m:
                continue
            rid = m.group("rid")
            r = self.res_by_id.get(rid, {})
            l = layout_by_file.get(rel)
            ptr = m.group("ptr") or (version_pointer(l) if l else None)
            pp = self.pass_by_ptr.get(ptr) if ptr else None
            arr = m.group("arr")
            arr_idx = int(arr[2:-1]) if arr else (l.get("FirstArraySlice") if l else 0)
            entry = {
                "file": rel,
                "bytes": self.manifest_files[rel].get("bytes"),
                "sha256": self.manifest_files[rel].get("sha256"),
                "mip": int(m.group("mip")) if m.group("mip") is not None else (l.get("MipLevel") if l else 0),
                "first_array_slice": arr_idx,
                "num_array_slices": l.get("NumArraySlices") if l else None,
                "z_slice_count": l.get("ZSliceCount") if l else None,
                "stencil": bool(m.group("stencil")) or bool(l and l.get("StencilSubresource")),
                "linear_variant": bool(m.group("linear")),
                "native_format": l.get("NativeFormat") if l else None,
                "view_format": l.get("ViewFormat") if l else None,
                "stored_format": l.get("StoredFormat") if l else None,
                "version_pointer": ptr,
                "version_pass_id": int(pp.get("Id")) if pp else None,
                "version_pass_event": pp.get("EventName") if pp else None,
                "has_subresource_layout": l is not None,
            }
            files_by_res.setdefault(rid, []).append(entry)
            payload_rows.append({
                "resource": rid,
                "name": r.get("Name"),
                "graph": rid[:4],
                **entry,
            })
        for r in sorted(self.resources, key=lambda x: x["UniqueResourceName"]):
            rid = r["UniqueResourceName"]
            files = files_by_res.get(rid, [])
            res_rows.append({
                "id": rid,
                "graph": rid[:4],
                "name": r.get("Name"),
                "desc": r.get("Desc"),
                "type": r.get("Type"),
                "format": r.get("Format"),
                "extent": [r.get("ExtentX"), r.get("ExtentY"), r.get("Depth")],
                "array_size": r.get("ArraySize"),
                "num_mips": r.get("NumMips"),
                "byte_size": r.get("ByteSize"),
                "file_count": len(files),
                "files": files,
            })
        self.inventory_summary["resource_binary_files"] = len(payload_rows)
        self.inventory_passes = pass_rows
        self.inventory_resources = res_rows
        self.inventory_payloads = payload_rows


# ---------------------------------------------------------------- sampling

def view_crop(ctx: Context, tex: Texture):
    """Exact published view rect for view-sized targets, else full texture."""
    vs = ctx.selected_view
    x, y, _, _ = [int(round(v)) for v in (vs["view_rect_min"] or [0, 0, 0, 0])]
    w, h = [int(round(v)) for v in (vs["view_size_and_inv_size"] or [tex.w, tex.h])[:2]]
    if x >= 0 and y >= 0 and w > 0 and h > 0 and x + w <= tex.w and y + h <= tex.h:
        return (x, y, w, h)
    return (0, 0, tex.w, tex.h)


def iter_rect(tex: Texture, crop, out_w: int, out_h: int, z=0):
    x0, y0, w, h = crop
    for oy in range(out_h):
        sy = y0 + min(h - 1, int(oy * h / out_h))
        for ox in range(out_w):
            sx = x0 + min(w - 1, int(ox * w / out_w))
            yield ox, oy, tex.texel(sx, sy, z)


def gray(v: float):
    g = u8(max(0.0, min(1.0, v)))
    return (g, g, g)


def heat(v: float):
    v = max(0.0, min(1.0, v))
    stops = [
        (0.0, (8, 8, 40)),
        (0.25, (20, 90, 200)),
        (0.5, (30, 200, 190)),
        (0.75, (245, 220, 70)),
        (1.0, (250, 60, 30)),
    ]
    for i in range(len(stops) - 1):
        a, ca = stops[i]
        b, cb = stops[i + 1]
        if v <= b:
            return lerp_color(ca, cb, (v - a) / (b - a) if b > a else 0.0)
    return stops[-1][1]


def cat_color(i: int):
    pal = [
        (25, 25, 25), (235, 60, 60), (60, 200, 80), (70, 130, 255),
        (255, 200, 40), (240, 80, 240), (40, 220, 220), (255, 130, 20),
        (170, 110, 255), (190, 210, 70), (120, 220, 160), (255, 120, 150),
    ]
    return pal[i % len(pal)]


def cat_index(v: int, values=None):
    if values is None:
        return v
    try:
        return values.index(v)
    except ValueError:
        return 0


def color_hex(c):
    return "#%02X%02X%02X" % (c[0], c[1], c[2])


def legend_manifest(items):
    return [{"label": str(l), "color": color_hex(c)} for l, c in items]


# ---------------------------------------------------------------- stats / HDR

INVALID_LIGHTING = (65024.0, 65024.0, 64512.0)


def is_invalid_lighting(r, g, b):
    return (
        abs(r - INVALID_LIGHTING[0]) <= 0.5
        and abs(g - INVALID_LIGHTING[1]) <= 0.5
        and abs(b - INVALID_LIGHTING[2]) <= 0.5
    )


def hdr_is_invalid(r, g, b, sentinel=True):
    if not (math.isfinite(r) and math.isfinite(g) and math.isfinite(b)):
        return "nonfinite"
    if r < 0.0 or g < 0.0 or b < 0.0:
        return "negative"
    if sentinel and is_invalid_lighting(r, g, b):
        return "sentinel"
    return None


def collect_hdr(tex: Texture, crop, out_w: int, out_h: int, sentinel=True, stride=2):
    lums = []
    n = 0
    zero = 0
    invalid = 0
    nonfinite = 0
    negative = 0
    sentinel_count = 0
    vmax = 0.0
    vmin = math.inf
    for oy in range(0, out_h, stride):
        for ox in range(0, out_w, stride):
            x0, y0, w, h = crop
            sx = x0 + min(w - 1, int(ox * w / out_w))
            sy = y0 + min(h - 1, int(oy * h / out_h))
            r, g, b, _ = tex.texel(sx, sy)
            n += 1
            why = hdr_is_invalid(r, g, b, sentinel)
            if why:
                invalid += 1
                if why == "nonfinite":
                    nonfinite += 1
                elif why == "negative":
                    negative += 1
                else:
                    sentinel_count += 1
                continue
            if r == 0.0 and g == 0.0 and b == 0.0:
                zero += 1
                continue
            lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
            if lum > 0.0 and math.isfinite(lum):
                lums.append(lum)
                vmax = max(vmax, lum)
                vmin = min(vmin, lum)
    lums.sort()
    return {
        "samples": n,
        "valid_nonzero_samples": len(lums),
        "valid_zero_samples": zero,
        "invalid_samples": invalid,
        "nonfinite_samples": nonfinite,
        "negative_samples": negative,
        "sentinel_samples": sentinel_count,
        "valid_nonzero_fraction": (len(lums) / n) if n else 0.0,
        "valid_zero_fraction": (zero / n) if n else 0.0,
        "invalid_fraction": (invalid / n) if n else 0.0,
        "luminance_min": (lums[0] if lums else None),
        "luminance_p50": pct(lums, 0.50),
        "luminance_p90": pct(lums, 0.90),
        "luminance_p99": pct(lums, 0.99),
        "luminance_max": (lums[-1] if lums else None),
    }


def hdr_scale_from(stats, fallback=1.0):
    p99 = stats.get("luminance_p99")
    if not p99 or p99 <= 0.0 or not math.isfinite(p99):
        return fallback
    return 1.0 / p99


def plot_hdr(tex: Texture, crop, out_w: int, out_h: int, scale: float,
             sentinel=True, sentinel_color=MAGENTA, nonfinite_color=YELLOW,
             negative_color=BLUE, zero_color=BLACK, oetf="srgb",
             stride_render=1):
    rast = Raster(out_w, out_h, BG)
    for oy in range(0, out_h, stride_render):
        for ox in range(0, out_w, stride_render):
            x0, y0, w, h = crop
            sx = x0 + min(w - 1, int(ox * w / out_w))
            sy = y0 + min(h - 1, int(oy * h / out_h))
            r, g, b, _ = tex.texel(sx, sy)
            why = hdr_is_invalid(r, g, b, sentinel)
            if why == "nonfinite":
                col = nonfinite_color
            elif why == "negative":
                col = negative_color
            elif why == "sentinel":
                col = sentinel_color
            elif r == 0.0 and g == 0.0 and b == 0.0:
                col = zero_color
            else:
                out = []
                for c in (r, g, b):
                    q = c * scale
                    t = q / (1.0 + q) if q >= 0.0 else 0.0
                    if oetf == "srgb":
                        t = srgb_oetf(t)
                    out.append(u8(t))
                col = (out[0], out[1], out[2])
            rast.set(ox, oy, col)
    return rast


def upscale_nearest(rast: Raster, factor: int) -> Raster:
    return rast.scaled(rast.w * factor, rast.h * factor)


def fit_panel(src: Raster, w: int, h: int, fill=(0, 0, 0)) -> Raster:
    """Preserve aspect ratio inside a w*h panel."""
    if src.w <= 0 or src.h <= 0:
        return Raster(w, h, fill)
    s = min(w / src.w, h / src.h)
    nw = max(1, int(round(src.w * s)))
    nh = max(1, int(round(src.h * s)))
    scaled = src.scaled(nw, nh)
    out = Raster(w, h, fill)
    out.blit(scaled, (w - nw) // 2, (h - nh) // 2)
    return out


def panel_label(rast: Raster, text: str):
    rast.rect(0, 0, rast.w, 12, (0, 0, 0))
    rast.draw_text(3, 2, text, WHITE, 1)


def contact_sheet(panels, cols: int, panel_w: int, panel_h: int, labels):
    rows = (len(panels) + cols - 1) // cols
    out = Raster(cols * panel_w, rows * panel_h, (0, 0, 0))
    for i, p in enumerate(panels):
        x = (i % cols) * panel_w
        y = (i // cols) * panel_h
        fit = fit_panel(p, panel_w, panel_h)
        if labels and i < len(labels):
            panel_label(fit, labels[i])
        out.blit(fit, x, y)
    return out


def composite_sheet(placements, width: int, height: int):
    """Place exact-size panels at absolute positions; labels are drawn in place."""
    out = Raster(width, height, (0, 0, 0))
    for x, y, panel, label in placements:
        if label:
            panel_label(panel, label)
        out.blit(panel, x, y)
    return out


# ---------------------------------------------------------------- manifest entry


INVALID_SOURCES = {
    "gbuffer-a-world-normal": "GBufferClear clears GBufferA to 0; EncodeGBuffer writes OutGBufferA=0 for the unlit/background branch (DeferredShadingCommon.ush:984-988). A zero packed word is not a valid normal.",
    "gbuffer-a-perobject-2bit": "GBufferClear clears GBufferA to 0; EncodeGBuffer writes PerObjectGBufferData only for non-unlit pixels (DeferredShadingCommon.ush:1003-1004).",
    "gbuffer-b-metallic": "GBufferB is cleared to 0 by GBufferClear; valid material pixels have a non-zero packed shading-model byte (EncodeGBuffer, DeferredShadingCommon.ush:1007-1010).",
    "gbuffer-b-specular": "GBufferB is cleared to 0 by GBufferClear; valid material pixels have a non-zero packed shading-model byte (EncodeGBuffer, DeferredShadingCommon.ush:1007-1010).",
    "gbuffer-b-roughness": "GBufferB is cleared to 0 by GBufferClear; valid material pixels have a non-zero packed shading-model byte (EncodeGBuffer, DeferredShadingCommon.ush:1007-1010).",
    "gbuffer-b-shading-model": "GBufferB is cleared to 0 by GBufferClear; EncodeShadingModelIdAndSelectiveOutputMask produces 0 only for a zero shading model/mask, which is the cleared background here.",
    "gbuffer-b-selective-mask": "GBufferB is cleared to 0 by GBufferClear; EncodeShadingModelIdAndSelectiveOutputMask produces 0 only for a zero shading model/mask, which is the cleared background here.",
    "gbuffer-c-basecolor": "GBufferC is cleared to 0 by GBufferClear; EncodeGBuffer writes BaseColor only for non-unlit pixels (DeferredShadingCommon.ush:1012).",
    "scene-depth-device-z": "ClearDepthStencil (SceneDepthZ) is the first depth pass; reversed-Z far plane is 0. DeviceZ <= 1e-9 is therefore no geometry/far clear, not a valid near depth.",
    "scene-depth-linear-view": "DeviceZ=0 is the reversed-Z far-plane clear (ClearDepthStencil pass); ConvertFromDeviceZ would divide by zero there, so it is categorical no-geometry.",
    "scene-stencil": "PF_X24_G8 stencil byte has no invalid encoding; 0 is the cleared stencil value (ClearStencil pass). All 1,136,640 bytes are 0 in this capture.",
    "scene-color-before-indirect": "SceneColor is PF_FloatRGBA; only IEEE non-finite values are marked invalid. No value is clamped or treated as sentinel.",
    "scene-color-after-indirect": "SceneColor is PF_FloatRGBA; only IEEE non-finite values are marked invalid. No value is clamped or treated as sentinel.",
    "scene-color-before-after": "SceneColor is PF_FloatRGBA; only IEEE non-finite values are marked invalid. No value is clamped or treated as sentinel.",
    "surface-cache-material-layers": "Unallocated surface-cache pages are cleared to 0; Lumen.SceneAlbedo all-zero is the empty-page marker. Validity is taken from the albedo page, not guessed from normal/emissive alpha.",
    "surface-cache-lighting": "Unallocated surface-cache pages are cleared to 0; validity is taken from the corresponding Lumen.SceneAlbedo page. Navy = no page, black = valid zero lighting.",
    "card-capture-layers": "ClearCardCapture clears the card atlases; Target1.a is the source-proved valid flag (LumenCardBasePass.ush:146). A=0 is no card data, not black albedo.",
    "world-cache-radiance-indirection-slices": "LumenRadianceCacheInterpolation.ush:302-317: 0xFFFFFFFF = INVALID_PROBE_INDEX; valid words carry a low-16-bit probe index and 3 adaptive-probe bits.",
    "reflection-trace-radiance": "LumenReflectionCommon.ush:89-99: EncodeRayDistance = HitDistance * (-1 if hit else +1); +0 is the cleared/unused tile. INVALID_LIGHTING = (65024,65024,64512) from StochasticLightingCommon.ush:189-193. |distance| < 0.1 cm is treated as a cleared/self-intersection trace, not a radiance measurement.",
    "reflection-trace-hit": "LumenReflectionCommon.ush:89-99: negative = hit, positive = miss; +0 is cleared/unused. Sign is preserved bit-exactly; |distance| < 0.1 cm is categorical unused/self-intersection, not a heat value.",
    "reflection-resolved-radiance": "StochasticLightingCommon.ush:189-193 defines INVALID_LIGHTING = half3(65024,65024,64512) and IsLightingValid = !all(V == INVALID_LIGHTING); LumenReflectionResolve.usf:111-112 writes it for missing lighting. Valid zero is a different value.",
    "reflection-resolved-depth": "LumenReflectionResolve.usf:650 writes Center.MinRayHitT; InitReflectionLighting sets FLOAT_32_MAX for no hit (:112). In R16F that saturates to inf; no-hit is categorical, not distance 0.",
    "screen-probe-scene-depth": "LumenScreenProbeCommon.ush:78-86 reads asfloat(uint) and documents negative depth for invalid probes; this figure additionally treats non-finite/zero as invalid.",
    "screen-probe-world-normal": "Probe validity is the same scene-depth test (LumenScreenProbeCommon.ush:92); stale normal bytes of invalid probes are not decoded as a direction.",
    "screen-probe-lighting-pdf": "The PDF is a half-float importance-sampling value; negative and non-finite halves occur in unused/accumulation texels. They are marked invalid, not clamped into the PDF.",
    "screen-probe-trace-hit": "LumenScreenProbeCommon.ush:239-269: bit0=bHit, bit1=bMoving, bit31=bReachedRadianceCache, bits 2..30=float32 distance. bHit=0 and distance=0 is an unused texel, not radiance=0. A bHit texel with non-finite/<=0/>1e7 cm distance is categorical invalid, not a heat value.",
    "screen-probe-hit-distance": "LumenScreenProbeTracingCommon.ush:48-57: decode (enc^2*65504 - 1/255)*255/254; a negative decoded value is the invalid-ray marker, and GetProbeMaxHitDistance() = MaxHalfFloat 65504 (LumenScreenProbeCommon.ush:234-237) encodes no-hit/miss.",
    "screen-probe-radiance": "StochasticLightingCommon.ush:189-193 INVALID_LIGHTING = (65024,65024,64512); exact triple is categorical invalid, not radiance=0 (LumenScreenProbeGatherTemporal.usf:19).",
    "screen-probe-filtered-radiance": "StochasticLightingCommon.ush:189-193 INVALID_LIGHTING = (65024,65024,64512); exact triple is categorical invalid, not radiance=0.",
    "screen-probe-diffuse-indirect": "The screen-probe temporal path uses the same INVALID_LIGHTING triple (LumenScreenProbeGatherTemporal.usf:19); the exact triple is categorical invalid, not zero irradiance.",
    "screen-probe-rough-specular": "The screen-probe temporal path uses the same INVALID_LIGHTING triple (LumenScreenProbeGatherTemporal.usf:19); the exact triple is categorical invalid, not zero radiance.",
    "vsm-maskbits-nibbles": "PF_R32G32_UINT has no invalid encoding; a 0 nibble is a valid all-clear mask (none of the 4 packed bits set).",
    "vsm-page-table-state": "VirtualShadowMapPageAccessCommon.ush:247-286: 0 = VSM_PHYSICAL_PAGE_INVALID; VirtualShadowMapArray.cpp:985 records 0xFFFFFFFF as the dummy page. Both are categorical, not page index 0.",
}


def add_figure(ctx: Context, slug: str, title: str, subtitle: str, content: Raster,
               legend, notes, tex: Texture, layout: dict, stats: dict,
               quantity: dict, channel_mapping, invalid: dict, display: dict,
               kind: str, extra: dict | None = None):
    payload = ctx.verify_source(layout)
    p = ctx.pass_of(layout)
    image = write_png(PNG_DIR / (slug + ".png"), compose_figure(
        content, title, subtitle, legend, notes
    ))
    invalid = dict(invalid or {})
    invalid.setdefault("source", INVALID_SOURCES.get(
        slug, "Semantics are stated in the figure notes and channel mapping; no sentinel is inferred."
    ))
    entry = {
        "slug": slug,
        "kind": kind,
        "title": title,
        "subtitle": subtitle,
        "graph": {
            "prefix": ctx.selected_graph,
            "selection": "exact 1280x720 FViewUniformShaderParameters payload referenced by the highest pass id",
            "view_content": ctx.selected_view["content"],
            "view_metadata": ctx.selected_view["metadata"],
            "view_bin_sha256": ctx.selected_view["bin_sha256"],
            "view_rect_min": ctx.selected_view["view_rect_min"],
            "view_size_and_inv_size": ctx.selected_view["view_size_and_inv_size"],
            "state_frame_index_mod8": ctx.selected_view["state_frame_index_mod8"],
            "inv_device_z_to_world_z_transform": ctx.selected_view["inv_device_z_to_world_z_transform"],
            "max_pass_id": ctx.selected_view["max_pass_id"],
        },
        "source": {
            "resource_id": tex.rid,
            "name": tex.name,
            "type": tex.desc.get("Type"),
            "native_format": tex.native_format,
            "stored_format": tex.stored_format,
            "extent": [tex.w, tex.h, tex.depth],
            "mip": tex.mip,
            "first_array_slice": tex.slice,
            "stencil_subresource": tex.stencil,
            **stored_format_provenance(layout, tex),
        },
        "payload": payload,
        "payload_version": {
            "version_pointer": version_pointer(layout),
            "pass_id": int(p.get("Id")) if p else None,
            "pass_event": p.get("EventName") if p else None,
        },
        "pass": {
            "id": int(p.get("Id")) if p else None,
            "pointer": p.get("Pointer") if p else None,
            "event": p.get("EventName") if p else None,
            "parameters_name": p.get("ParametersName") if p else None,
            "parent_scopes": p.get("ParentEventScopes") if p else None,
        },
        "quantity": quantity,
        "channel_mapping": channel_mapping,
        "invalid": invalid,
        "display": display,
        "stats": stats,
        "legend": legend_manifest(legend),
        "image": image,
        "note": extra.get("note") if extra else None,
    }
    if extra:
        entry.update({k: v for k, v in extra.items() if k != "note"})
    ctx.figures.append(entry)
    print(f"  {slug:42s} {image['width']}x{image['height']}  {image['bytes']:8d} B  "
          f"res={tex.rid}  pass={entry['pass']['id']}")
    return entry


# ---------------------------------------------------------------- GBuffer


def fig_gbuffer(ctx: Context):
    ga, ra, la = ctx.texture("GBufferA", event="BasePassParallel")
    crop = view_crop(ctx, ga)
    vw, vh = crop[2], crop[3]

    # Normal: R/G/B are WorldNormal*0.5+0.5 in native A2B10G10R10.
    rast = Raster(vw, vh, BG)
    lengths = []
    nonblack = 0
    for oy in range(vh):
        sy = crop[1] + oy
        for ox in range(vw):
            sx = crop[0] + ox
            word = ga.raw_u32(sx, sy)
            r, g, b, a = decode_a2b10(word)
            if word == 0:
                rast.set(ox, oy, NAVY)
                continue
            rast.set(ox, oy, (u8(r), u8(g), u8(b)))
            nx, ny, nz = r * 2.0 - 1.0, g * 2.0 - 1.0, b * 2.0 - 1.0
            lengths.append(math.sqrt(nx * nx + ny * ny + nz * nz))
            nonblack += 1
    lengths.sort()
    st = {
        "pixels": vw * vh,
        "nonblack_pixels": nonblack,
        "background_pixels": vw * vh - nonblack,
        "world_normal_length_mean": (sum(lengths) / len(lengths)) if lengths else None,
        "world_normal_length_p01": pct(lengths, 0.01),
        "world_normal_length_p99": pct(lengths, 0.99),
        "world_normal_length_fraction_within_0.02_of_1": (
            sum(1 for v in lengths if abs(v - 1.0) <= 0.02) / len(lengths) if lengths else None
        ),
        "a_per_object_values": [0, 1, 2, 3],
    }
    add_figure(
        ctx, "gbuffer-a-world-normal",
        "GBufferA: world normal (native 10/10/10/2 packed)",
        "graph %s | BasePassParallel pass | RGB = WorldNormal*0.5+0.5 | view rect 0,0,1280,720" % ctx.selected_graph,
        rast,
        [("world normal N*0.5+0.5", (128, 128, 255)),
         ("no geometry (all 32 bits zero)", NAVY)],
        [
            "Source: EncodeGBuffer -> OutGBufferA.rgb = EncodeNormal(WorldNormal) = N*0.5+0.5 (DeferredShadingCommon.ush:1003, EncodeNormal at :137).",
            "A is the 2-bit PerObjectGBufferData field; it is shown separately. Native format PF_A2B10G10R10, read from the stored file without expansion.",
            "World-space normal validity: non-background decoded N lengths are measured and reported in the manifest; A=0 is not treated as a normal.",
        ],
        ga, la, st,
        {"name": "WorldNormal", "units": "unit vector encoded 0..1", "domain": "world space",
         "description": "RGB = normalized world-space normal * 0.5 + 0.5; not octahedral."},
        ["R = 10-bit UNORM low bits", "G = 10-bit UNORM next bits", "B = 10-bit UNORM next bits", "A = top 2-bit PerObjectGBufferData"],
        {"rule": "packed word == 0", "semantics": "cleared/background pixel; not a normal", "color": color_hex(NAVY)},
        {"transform": "identity: decoded 10-bit UNORM to 8-bit RGB", "content_rect_xywh": [0, 0, vw, vh],
         "source_crop_xywh": list(crop), "resample": "none (1:1 crop of native 1280x888 target)"},
        "gbuffer",
    )

    # Per-object 2-bit field.
    rast = Raster(vw, vh, BG)
    counts = {0: 0, 1: 0, 2: 0, 3: 0}
    for oy in range(vh):
        for ox in range(vw):
            word = ga.raw_u32(crop[0] + ox, crop[1] + oy)
            if word == 0:
                rast.set(ox, oy, NAVY)
                continue
            v = (word >> 30) & 3
            counts[v] += 1
            rast.set(ox, oy, cat_color(v + 1))
    add_figure(
        ctx, "gbuffer-a-perobject-2bit",
        "GBufferA alpha: 2-bit per-object data",
        "graph %s | BasePassParallel pass | A = PerObjectGBufferData | categorical" % ctx.selected_graph,
        rast,
        [("0: neither", cat_color(1)), ("1: contact-shadow caster", cat_color(2)),
         ("2: dynamic indirect-shadow caster", cat_color(3)), ("3: both", cat_color(4)),
         ("no geometry", NAVY)],
        [
            "Source: DeferredShadingCommon.ush:1004 stores PerObjectGBufferData in A; CastContactShadow reads bit 0 and HasDynamicIndirectShadowCasterRepresentation reads bit 1 (:441-453).",
            "The 2-bit field is preserved exactly; the categorical colours are display-only.",
        ],
        ga, la,
        {"pixels": vw * vh, "category_counts": {str(k): v for k, v in counts.items()}},
        {"name": "PerObjectGBufferData", "units": "2-bit mask", "domain": "per-pixel",
         "description": "bit0 = CastContactShadow, bit1 = HasDynamicIndirectShadowCasterRepresentation."},
        ["A bit0 = contact shadow caster", "A bit1 = dynamic indirect shadow caster"],
        {"rule": "packed word == 0", "semantics": "cleared/background pixel", "color": color_hex(NAVY)},
        {"transform": "categorical palette", "content_rect_xywh": [0, 0, vw, vh],
         "source_crop_xywh": list(crop), "resample": "none"},
        "gbuffer",
    )

    gb, rb, lb = ctx.texture("GBufferB", event="BasePassParallel")
    # Metallic / specular / roughness from shader .r/.g/.b, memory B/G/R/A.
    fields = [
        ("metallic", 0, "Metallic (shader .r, memory byte 2)"),
        ("specular", 1, "Specular (shader .g, memory byte 1)"),
        ("roughness", 2, "Roughness (shader .b, memory byte 0)"),
    ]
    for slug, idx, label in fields:
        rast = Raster(vw, vh, BG)
        vals = []
        bg = 0
        for oy in range(vh):
            for ox in range(vw):
                t = gb.texel(crop[0] + ox, crop[1] + oy)
                a_byte = int(round(t[3] * 255.0))
                if a_byte == 0:
                    rast.set(ox, oy, NAVY)
                    bg += 1
                    continue
                v = t[idx]
                vals.append(v)
                rast.set(ox, oy, gray(v))
        vals.sort()
        add_figure(
            ctx, f"gbuffer-b-{slug}",
            f"GBufferB: {label}",
            "graph %s | BasePassParallel pass | valid only where GBufferB.A != 0" % ctx.selected_graph,
            rast,
            [("valid 0.0 (black) .. 1.0 (white)", (128, 128, 128)), ("no geometry (A == 0)", NAVY)],
            [
                "Source: DeferredShadingCommon.ush:1007-1009 writes Metallic, Specular, Roughness to B.r/B.g/B.b; PF_B8G8R8A8 memory order is B,G,R,A.",
                "The three fields are decoded independently; no combined colour is used for this quantity.",
                "Black is a valid zero value; navy marks background/cleared pixels where A == 0.",
            ],
            gb, lb,
            {"pixels": vw * vh, "valid_pixels": len(vals), "background_pixels": bg,
             "min": (vals[0] if vals else None), "p50": pct(vals, 0.50), "max": (vals[-1] if vals else None)},
            {"name": label.split(" (")[0], "units": "linear 0..1 UNORM", "domain": "material",
             "description": f"GBufferB shader channel {label}."},
            [f"R/G/B/A decode for PF_B8G8R8A8: byte 0 = B (Roughness), byte 1 = G (Specular), byte 2 = R (Metallic), byte 3 = A (packed shading model/mask)"],
            {"rule": "A byte == 0", "semantics": "background/cleared; not a valid material value", "color": color_hex(NAVY)},
            {"transform": "linear value -> 8-bit grayscale", "content_rect_xywh": [0, 0, vw, vh],
             "source_crop_xywh": list(crop), "resample": "none"},
            "gbuffer",
        )

    # Shading model ID (low 4 bits) and selective mask (high 4 bits).
    models = {}
    masks = {}
    for oy in range(vh):
        for ox in range(vw):
            t = gb.texel(crop[0] + ox, crop[1] + oy)
            ab = int(round(t[3] * 255.0))
            if ab == 0:
                continue
            models.setdefault(ab & 0xF, 0)
            models[ab & 0xF] += 1
            masks.setdefault(ab & 0xF0, 0)
            masks[ab & 0xF0] += 1
    model_values = sorted(models)
    mask_values = sorted(masks)
    for slug, values, shift, title, note in [
        ("shading-model", model_values, 0, "GBufferB: ShadingModelID (low nibble)",
         "EncodeShadingModelIdAndSelectiveOutputMask: Value = (ShadingModelId & 0xF) | SelectiveOutputMask; low nibble = ShadingModelID (ShadingCommon.ush:35 mask 0xF)."),
        ("selective-mask", mask_values, 4, "GBufferB: SelectiveOutputMask (high nibble)",
         "EncodeShadingModelIdAndSelectiveOutputMask: high nibble = SelectiveOutputMask; individual engine mask bits are not re-derived here."),
    ]:
        rast = Raster(vw, vh, BG)
        for oy in range(vh):
            for ox in range(vw):
                t = gb.texel(crop[0] + ox, crop[1] + oy)
                ab = int(round(t[3] * 255.0))
                if ab == 0:
                    rast.set(ox, oy, NAVY)
                else:
                    v = (ab & 0xF) if shift == 0 else (ab & 0xF0)
                    rast.set(ox, oy, cat_color(cat_index(v, values) + 1))
        legend = [(f"0x{v:02X} ({models.get(v, masks.get(v, 0))} px)", cat_color(i + 1)) for i, v in enumerate(values)]
        legend.append(("no geometry", NAVY))
        add_figure(
            ctx, f"gbuffer-b-{slug}", title,
            "graph %s | BasePassParallel pass | categorical; exact byte preserved" % ctx.selected_graph,
            rast, legend,
            [note,
             "The capture contains %d distinct value(s); see the manifest stats for exact counts." % len(values),
             "Categorical colours are display-only and do not imply a numeric ordering."],
            gb, lb,
            {"pixels": vw * vh, "distinct_values": len(values),
             "value_counts": {("0x%02X" % v): (models.get(v, masks.get(v, 0))) for v in values}},
            {"name": "ShadingModelID" if shift == 0 else "SelectiveOutputMask",
             "units": "4-bit enum" if shift == 0 else "4-bit mask",
             "domain": "material", "description": note},
            ["PF_B8G8R8A8 byte 3 = packed shading model (low nibble) + selective output mask (high nibble)"],
            {"rule": "packed byte == 0", "semantics": "background/cleared; not a valid shading-model value", "color": color_hex(NAVY)},
            {"transform": "categorical palette keyed by exact nibble", "content_rect_xywh": [0, 0, vw, vh],
             "source_crop_xywh": list(crop), "resample": "none"},
            "gbuffer",
        )

    gc, rc, lc = ctx.texture("GBufferC", event="BasePassParallel")
    rast = Raster(vw, vh, BG)
    nonbg = 0
    for oy in range(vh):
        for ox in range(vw):
            t = gc.texel(crop[0] + ox, crop[1] + oy)
            if t[0] == 0.0 and t[1] == 0.0 and t[2] == 0.0:
                rast.set(ox, oy, NAVY)
            else:
                nonbg += 1
                rast.set(ox, oy, (u8(t[0]), u8(t[1]), u8(t[2])))
    add_figure(
        ctx, "gbuffer-c-basecolor",
        "GBufferC: base color (sRGB-encoded bytes)",
        "graph %s | BasePassParallel pass | RGB = BaseColor (sRGB target) | no exposure" % ctx.selected_graph,
        rast,
        [("base color: sRGB-encoded RGB bytes", (220, 180, 140)), ("no geometry (RGB == 0)", NAVY)],
        [
            "Source: DeferredShadingCommon.ush:1012 OutGBufferC.rgb = EncodeBaseColor(BaseColor); EncodeBaseColor returns BaseColor unchanged because the render target is sRGB (:192-196).",
            "SubresourceLayouts.json records NativeSRGB=true and ViewSRGB=true for this file; the bytes are the native sRGB encoding and are displayed as-is. No linearization, exposure or tone curve is applied to this figure.",
            "The engine's material base colour is recovered by the sRGB EOTF; the manifest stores this statement, not a second image.",
        ],
        gc, lc,
        {"pixels": vw * vh, "nonbackground_pixels": nonbg, "background_pixels": vw * vh - nonbg,
         "native_srgb": lc.get("NativeSRGB"), "view_srgb": lc.get("ViewSRGB"),
         "srgb_view_used_for_readback": lc.get("SRGBViewUsedForReadback")},
        {"name": "BaseColor", "units": "sRGB-encoded 0..1 per channel", "domain": "material",
         "description": "BaseColor stored in an sRGB render target; displayed as sRGB bytes."},
        ["R = shader .r (memory byte 2)", "G = shader .g (memory byte 1)", "B = shader .b (memory byte 0)", "A = GBufferAO / occlusion byte (not shown)"],
        {"rule": "RGB all zero", "semantics": "cleared/background", "color": color_hex(NAVY)},
        {"transform": "none (sRGB-encoded bytes shown directly)", "content_rect_xywh": [0, 0, vw, vh],
         "source_crop_xywh": list(crop), "resample": "none"},
        "gbuffer",
    )

    ctx.unavailable.append({
        "item": "GBufferC.A as AO vs DiffuseIndirectSampleOcclusion",
        "reason": "AmbiguousChannelMeaningInThisBuild",
        "evidence": "GBufferC.A is exactly 0 (background) or 255 (opaque) in this capture. EncodeGBuffer has two compile-time branches (GBufferAO vs DiffuseIndirectSampleOcclusion bitmask); no shader permutation define is captured, so a radiometric meaning would be a guess. GBufferB.A shading model and A per-object masks are decoded instead.",
    })
    ctx.unavailable.append({
        "item": "GBufferD CustomData",
        "reason": "AllZeroInThisCapture",
        "evidence": "GBufferD is PF_B8G8R8A8 and all 5,683,200 bytes are 0; CustomData is only valid for subsurface/skin shading models (HasCustomGBufferData, DeferredShadingCommon.ush:345-350), none of which are present.",
    })


# ---------------------------------------------------------------- depth


def fig_depth(ctx: Context):
    d, rd, ld = ctx.texture("SceneDepthZ", event="DepthPassParallel", stencil=False)
    crop = view_crop(ctx, d)
    vw, vh = crop[2], crop[3]
    vals = []
    zero = 0
    for oy in range(vh):
        for ox in range(vw):
            v = d.texel(crop[0] + ox, crop[1] + oy)[0]
            if v <= 1e-9 or not math.isfinite(v):
                zero += 1
                vals.append(None)
            else:
                vals.append(v)
    finite = sorted(v for v in vals if v is not None)
    vmax = finite[-1] if finite else 1.0
    # Raw device Z: fixed 0..vmax range, no linearization.
    rast = Raster(vw, vh, BG)
    for oy in range(vh):
        for ox in range(vw):
            v = vals[oy * vw + ox]
            rast.set(ox, oy, NAVY if v is None else gray(v / vmax))
    add_figure(
        ctx, "scene-depth-device-z",
        "SceneDepthZ: raw device Z (reversed-Z)",
        "graph %s | DepthPassParallel pass | range 0..%.8f | 1=near, 0=far" % (ctx.selected_graph, vmax),
        rast,
        [("raw device Z 0 (black) .. %.6f (white)" % vmax, (128, 128, 128)), ("no geometry (Z == 0)", NAVY)],
        [
            "Source payload is the depth subresource of PF_DepthStencil, stored as PF_R32_FLOAT (StoredFormat read from Base/SubresourceLayouts.json).",
            "This figure shows the raw reversed-Z device value; it is not linearized and no world-unit claim is made here. The linear view-depth figure uses the exact InvDeviceZToWorldZTransform from the selected View payload.",
            "The render target is 1280x888; only the exact 1280x720 view rect (0,0) is published.",
        ],
        d, ld,
        {"pixels": vw * vh, "no_geometry_pixels": zero, "valid_pixels": len(finite),
         "device_z_min": (finite[0] if finite else None), "device_z_p50": pct(finite, 0.50),
         "device_z_p99": pct(finite, 0.99), "device_z_max": vmax},
        {"name": "DeviceZ", "units": "normalized device Z (reversed-Z)", "domain": "screen",
         "description": "Raw value stored in the depth buffer; larger = nearer, 0 = far plane/no geometry."},
        ["R = PF_R32_FLOAT value (single channel)"],
        {"rule": "device Z <= 1e-9 or non-finite", "semantics": "no geometry / far-plane clear", "color": color_hex(NAVY)},
        {"transform": "linear scale by 1/max(valid deviceZ); 8-bit grayscale", "range": [0.0, vmax],
         "content_rect_xywh": [0, 0, vw, vh], "source_crop_xywh": list(crop), "resample": "none"},
        "depth",
    )

    # Linear view depth from the exact View payload.
    inv = ctx.selected_view["inv_device_z_to_world_z_transform"]
    lin = []
    for v in vals:
        if v is None:
            lin.append(None)
        else:
            try:
                z = v * inv[0] + inv[1] + 1.0 / (v * inv[2] - inv[3])
            except ZeroDivisionError:
                z = math.inf
            if not math.isfinite(z) or z <= 0.0:
                lin.append(None)
            else:
                lin.append(z)
    linv = sorted(v for v in lin if v is not None)
    dmin, dmax = (linv[0] if linv else 1.0), (linv[-1] if linv else 1.0)
    lo = math.log10(max(1e-6, dmin))
    hi = math.log10(max(1e-6, dmax))
    rast = Raster(vw, vh, BG)
    for oy in range(vh):
        for ox in range(vw):
            z = lin[oy * vw + ox]
            if z is None:
                rast.set(ox, oy, NAVY)
            else:
                t = (math.log10(max(1e-6, z)) - lo) / (hi - lo) if hi > lo else 0.5
                rast.set(ox, oy, heat(t))
    add_figure(
        ctx, "scene-depth-linear-view",
        "SceneDepthZ: linear view depth (reversed-Z reconstruction)",
        "graph %s | DepthPassParallel pass | InvDeviceZToWorldZTransform from View bytes | Unreal units (cm)" % ctx.selected_graph,
        rast,
        [("log10(view depth): %.2f cm (blue) .. %.2f cm (red)" % (dmin, dmax), (200, 120, 60)),
         ("no geometry", NAVY)],
        [
            "Reconstruction uses ConvertFromDeviceZ (Common.ush:1373): z = d*Inv[0] + Inv[1] + 1/(d*Inv[2] - Inv[3]) with the exact transform decoded from the selected View payload.",
            "Selected transform = %s; NearPlane = %s. Units are Unreal units (cm) because the engine view transform is in world units." % (inv, ctx.selected_view.get("near_plane")),
            "The colour scale is log10(view depth) between the valid minimum and maximum; it is a display transform, not a claim of linear precision.",
        ],
        d, ld,
        {"pixels": vw * vh, "valid_pixels": len(linv), "no_geometry_pixels": vw * vh - len(linv),
         "view_depth_min_cm": dmin, "view_depth_p50_cm": pct(linv, 0.50),
         "view_depth_p99_cm": pct(linv, 0.99), "view_depth_max_cm": dmax,
         "inv_device_z_to_world_z_transform": inv, "near_plane": ctx.selected_view.get("near_plane")},
        {"name": "LinearViewDepth", "units": "Unreal units (cm)", "domain": "view space",
         "description": "ConvertFromDeviceZ applied to the raw depth buffer using the captured View transform."},
        ["R = PF_R32_FLOAT device Z -> ConvertFromDeviceZ -> view depth"],
        {"rule": "device Z <= 1e-9 or reconstructed depth non-finite", "semantics": "no geometry", "color": color_hex(NAVY)},
        {"transform": "log10(view depth) normalized to [min,max]; heat palette", "range": [dmin, dmax],
         "content_rect_xywh": [0, 0, vw, vh], "source_crop_xywh": list(crop), "resample": "none"},
        "depth",
    )

    s, rs, ls = ctx.texture("SceneDepthZ", event="DepthPassParallel", stencil=True)
    counts = {}
    rast = Raster(vw, vh, (0, 0, 0))
    for oy in range(vh):
        for ox in range(vw):
            v = int(round(s.texel(crop[0] + ox, crop[1] + oy)[0]))
            counts[v] = counts.get(v, 0) + 1
            rast.set(ox, oy, gray(v / 255.0))
    add_figure(
        ctx, "scene-stencil",
        "SceneDepthZ: stencil subresource",
        "graph %s | DepthPassParallel pass | exact PF_X24_G8 stencil view" % ctx.selected_graph,
        rast,
        [("stencil value 0..255 (grayscale)", (128, 128, 128))],
        [
            "Source is the separate .stencil subresource (PF_X24_G8, StoredFormat PF_R8_UINT); it is not the depth channel.",
            "This capture's stencil is uniformly 0; the figure is intentionally flat and the manifest records the exact histogram. No custom-stencil content is claimed.",
        ],
        s, ls,
        {"pixels": vw * vh, "histogram": {str(k): v for k, v in sorted(counts.items())}},
        {"name": "Stencil", "units": "8-bit integer", "domain": "screen", "description": "Stencil byte from the depth-stencil subresource."},
        ["R = stencil byte / 255"],
        {"rule": "no invalid encoding; all values are stencil bytes", "semantics": "0 is a valid stencil value", "color": None},
        {"transform": "value/255 -> grayscale", "content_rect_xywh": [0, 0, vw, vh],
         "source_crop_xywh": list(crop), "resample": "none"},
        "depth",
    )


# ---------------------------------------------------------------- SceneColor


def fig_scene_color(ctx: Context):
    before, rb, lb = ctx.texture(
        "SceneColor", event_prefix="RenderLight Light::StandardDeferred"
    )
    after, ra, la = ctx.texture(
        "SceneColor", event_prefix="DiffuseIndirectComposite"
    )
    crop_b = view_crop(ctx, before)
    crop_a = view_crop(ctx, after)
    sb = collect_hdr(before, crop_b, crop_b[2], crop_b[3], sentinel=False, stride=3)
    sa = collect_hdr(after, crop_a, crop_a[2], crop_a[3], sentinel=False, stride=3)
    union = sorted(
        [v for v in (sb.get("luminance_p99"), sa.get("luminance_p99")) if v]
    )
    # One shared scale, derived from the union of both versions' valid pixels.
    lums = []
    for tex, crop in ((before, crop_b), (after, crop_a)):
        for oy in range(0, crop[3], 3):
            for ox in range(0, crop[2], 3):
                r, g, b, _ = tex.texel(crop[0] + ox, crop[1] + oy)
                if not (math.isfinite(r) and math.isfinite(g) and math.isfinite(b)):
                    continue
                if r < 0 or g < 0 or b < 0 or (r == 0 and g == 0 and b == 0):
                    continue
                lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
                if lum > 0 and math.isfinite(lum):
                    lums.append(lum)
    lums.sort()
    shared_p99 = pct(lums, 0.99) or 1.0
    scale = 1.0 / shared_p99 if shared_p99 > 0 else 1.0
    transform = {
        "method": "single shared exposure + Reinhard + sRGB OETF",
        "shared_p99_luminance_linear": shared_p99,
        "linear_exposure_scale": scale,
        "tone_curve": "c/(1+c) after exposure",
        "oetf": "sRGB (IEC 61966-2-1)",
        "per_image_scale": False,
        "statement": "Both SceneColor versions use exactly this transform; no independent percentile, exposure or tone curve is applied to either version.",
    }

    def render(tex, crop):
        return plot_hdr(tex, crop, crop[2], crop[3], scale, sentinel=False)

    rb_img = render(before, crop_b)
    ra_img = render(after, crop_a)
    for slug, title, tex, layout, rast, st, ev in [
        ("scene-color-before-indirect", "SceneColor: before diffuse-indirect composite",
         before, lb, rb_img, sb, "RenderLight Light::StandardDeferred: Ceiling area light"),
        ("scene-color-after-indirect", "SceneColor: after diffuse-indirect composite",
         after, la, ra_img, sa, "DiffuseIndirectComposite(DiffuseIndirect=ScreenProbeGather) 1280x720"),
    ]:
        add_figure(
            ctx, slug, title,
            "graph %s | pass %s | one shared SceneColor transform" % (
                ctx.selected_graph, ev),
            rast,
            [("linear radiance: shared exposure %.6f, Reinhard, sRGB" % scale, (200, 170, 120)),
             ("valid zero radiance (black)", BLACK), ("non-finite (yellow)", YELLOW)],
            [
                "Both SceneColor before/after figures use one declared exposure, normalization and OETF: %s." % transform["statement"],
                "exposure = %.12f (1 / shared p99 luminance %.12f), then c/(1+c), then sRGB OETF. The same constants are applied to both versions." % (scale, shared_p99),
                "SceneColor is linear HDR radiance in engine render-target units; no absolute photometric calibration is claimed.",
            ],
            tex, layout, st,
            {"name": "SceneColor", "units": "linear RGB radiance (engine render-target units)",
             "domain": "screen", "description": "HDR scene colour at this pass version."},
            ["R/G/B = half-float linear radiance", "A decoded but not used for display"],
            {"rule": "non-finite", "semantics": "invalid value; shown yellow", "color": color_hex(YELLOW)},
            {"transform": transform, "content_rect_xywh": [0, 0, rast.w, rast.h],
             "source_crop_xywh": list(crop_b if slug.endswith("before-indirect") else crop_a),
             "resample": "none"},
            "scene-color",
        )

    # Comparison: exact same transform, side by side, each downsampled 2x.
    left = rb_img.scaled(640, 360)
    right = ra_img.scaled(640, 360)
    comp = Raster(1280, 360, (0, 0, 0))
    comp.blit(left, 0, 0)
    comp.blit(right, 640, 0)
    comp.rect(639, 0, 2, 360, WHITE)
    panel_label(comp, "BEFORE: RenderLight")
    panel_label(right, "")
    comp.draw_text(650, 2, "AFTER: DiffuseIndirectComposite", WHITE, 1)
    comp.draw_text(6, 346, "BEFORE", WHITE, 1)
    comp.draw_text(646, 346, "AFTER", WHITE, 1)
    add_figure(
        ctx, "scene-color-before-after",
        "SceneColor before/after: one common display transform",
        "graph %s | same exposure %.9f, Reinhard and sRGB OETF for both halves" % (ctx.selected_graph, scale),
        comp,
        [("shared transform only; no per-image percentile", (200, 170, 120))],
        [
            "This comparison is the acceptance-check image: both halves were rendered from raw half-float SceneColor with the identical exposure %.12f, Reinhard c/(1+c) and sRGB OETF." % scale,
            "The before version is the Ceiling-area-light RenderLight output; after is DiffuseIndirectComposite. The difference is the indirect-diffuse/specular composite, not an exposure change.",
            "Each half is a 2x downsample of the exact 1280x720 view rect; the source crop remains 1280x720 for both.",
        ],
        after, la,
        {"before": sb, "after": sa, "shared_p99_luminance_linear": shared_p99,
         "linear_exposure_scale": scale, "before_label": "RenderLight",
         "after_label": "DiffuseIndirectComposite"},
        {"name": "SceneColor before/after comparison", "units": "linear RGB radiance",
         "domain": "screen", "description": "Side-by-side comparison under one common transform."},
        ["left half = before (RenderLight)", "right half = after (DiffuseIndirectComposite)"],
        {"rule": "non-finite", "semantics": "invalid; yellow", "color": color_hex(YELLOW)},
        {"transform": transform, "content_rect_xywh": [0, 0, 1280, 720],
         "display_rect_xywh": [0, 0, 1280, 360], "resample": "each half 2x nearest downsample"},
        "scene-color",
    )


# ---------------------------------------------------------------- atlases / surface cache


def find_bbox(tex: Texture, step: int = 8):
    minx, miny, maxx, maxy = tex.w, tex.h, -1, -1
    for y in range(0, tex.h, step):
        for x in range(0, tex.w, step):
            t = tex.texel(x, y)
            if any(abs(float(v)) > 1e-12 for v in t[:3]) or abs(float(t[3])) > 1e-12:
                minx = min(minx, x)
                miny = min(miny, y)
                maxx = max(maxx, x)
                maxy = max(maxy, y)
    if maxx < 0:
        return None
    return (minx, miny, maxx + step, maxy + step)


def union_crop(ctx: Context, texs, step=8, margin=16, max_side=2048):
    boxes = [b for b in (find_bbox(t, step) for t in texs) if b]
    if not boxes:
        return (0, 0, min(max_side, texs[0].w), min(max_side, texs[0].h))
    minx = max(0, min(b[0] for b in boxes) - margin)
    miny = max(0, min(b[1] for b in boxes) - margin)
    maxx = min(texs[0].w, max(b[2] for b in boxes) + margin)
    maxy = min(texs[0].h, max(b[3] for b in boxes) + margin)
    # Keep the crop bounded and readable.
    if maxx - minx > max_side:
        maxx = minx + max_side
    if maxy - miny > max_side:
        maxy = miny + max_side
    return (minx, miny, maxx - minx, maxy - miny)


def valid_mask_from(tex: Texture, crop, out_w: int, out_h: int):
    mask = []
    for oy in range(out_h):
        sy = crop[1] + min(crop[3] - 1, int(oy * crop[3] / out_h))
        for ox in range(out_w):
            sx = crop[0] + min(crop[2] - 1, int(ox * crop[2] / out_w))
            t = tex.texel(sx, sy)
            mask.append(any(abs(float(v)) > 1e-12 for v in t[:3]))
    return mask


def surface_albedo_panel(tex: Texture, crop, out_w, out_h, mask):
    rast = Raster(out_w, out_h, BG)
    vals = []
    off = nonfinite = 0
    for ox, oy, t in iter_rect(tex, crop, out_w, out_h):
        if not mask[oy * out_w + ox]:
            rast.set(ox, oy, NAVY)
            off += 1
            continue
        if not all(math.isfinite(v) for v in t[:3]):
            nonfinite += 1
            rast.set(ox, oy, YELLOW)
            continue
        rgb = [max(0.0, min(1.0, v * v)) for v in t[:3]]  # DecodeSurfaceCacheAlbedo: square
        vals.extend(rgb)
        rast.set(ox, oy, (u8(srgb_oetf(rgb[0])), u8(srgb_oetf(rgb[1])), u8(srgb_oetf(rgb[2]))))
    vals.sort()
    return rast, {"common_valid_pixels": out_w * out_h - off, "off_page_pixels": off,
                  "nonfinite_pixels": nonfinite,
                  "linear_albedo_p01": pct(vals, 0.01), "linear_albedo_p50": pct(vals, 0.50),
                  "linear_albedo_p99": pct(vals, 0.99)}


def surface_normal_panel(tex: Texture, crop, out_w, out_h, mask):
    rast = Raster(out_w, out_h, BG)
    lens = []
    off = nonfinite = 0
    for ox, oy, t in iter_rect(tex, crop, out_w, out_h):
        if not mask[oy * out_w + ox]:
            rast.set(ox, oy, NAVY)
            off += 1
            continue
        if not all(math.isfinite(v) for v in t[:3]):
            nonfinite += 1
            rast.set(ox, oy, YELLOW)
            continue
        nx = max(-1.0, min(1.0, t[0] * 2.0 - 1.0))
        ny = max(-1.0, min(1.0, t[1] * 2.0 - 1.0))
        nz = math.sqrt(max(1.0 - nx * nx - ny * ny, 0.0001))
        lens.append(math.sqrt(nx * nx + ny * ny + nz * nz))
        rast.set(ox, oy, (u8(nx * 0.5 + 0.5), u8(ny * 0.5 + 0.5), u8(nz * 0.5 + 0.5)))
    lens.sort()
    return rast, {"common_valid_pixels": out_w * out_h - off, "off_page_pixels": off,
                  "nonfinite_pixels": nonfinite,
                  "card_normal_length_mean": (sum(lens) / len(lens)) if lens else None,
                  "card_normal_length_fraction_within_0.01_of_1": (
                      sum(1 for v in lens if abs(v - 1.0) <= 0.01) / len(lens) if lens else None)}


def surface_depth_panel(tex: Texture, crop, out_w, out_h, mask):
    rast = Raster(out_w, out_h, BG)
    vals = []
    off = invalid = 0
    for ox, oy, t in iter_rect(tex, crop, out_w, out_h):
        v = t[0]
        if not mask[oy * out_w + ox]:
            off += 1
            rast.set(ox, oy, NAVY)
        elif not math.isfinite(v) or v >= 1.0:
            invalid += 1
            rast.set(ox, oy, BLUE)       # EncodeSurfaceCacheDepth reserves 1.0 for invalid
        else:
            vals.append(v)
            rast.set(ox, oy, gray(v))
    vals.sort()
    return rast, {"common_valid_pixels": out_w * out_h - off, "off_page_pixels": off,
                  "invalid_depth_pixels": invalid,
                  "depth_min": (vals[0] if vals else None), "depth_p50": pct(vals, 0.5),
                  "depth_p99": pct(vals, 0.99), "depth_max": (vals[-1] if vals else None)}


def surface_hdr_panel(tex: Texture, crop, out_w, out_h, scale, mask=None, sentinel=False):
    rast = Raster(out_w, out_h, BG)
    n = 0
    zero = 0
    invalid = 0
    lums = []
    for ox, oy, t in iter_rect(tex, crop, out_w, out_h):
        if mask is not None and not mask[oy * out_w + ox]:
            rast.set(ox, oy, NAVY)
            continue
        r, g, b = t[0], t[1], t[2]
        n += 1
        why = hdr_is_invalid(r, g, b, sentinel)
        if why:
            invalid += 1
            rast.set(ox, oy, MAGENTA if why == "sentinel" else (YELLOW if why == "nonfinite" else BLUE))
        elif r == 0.0 and g == 0.0 and b == 0.0:
            zero += 1
            rast.set(ox, oy, BLACK)
        else:
            lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
            if lum > 0 and math.isfinite(lum):
                lums.append(lum)
            col = []
            for c in (r, g, b):
                q = max(0.0, c) * scale
                tq = q / (1.0 + q)
                col.append(u8(srgb_oetf(tq)))
            rast.set(ox, oy, (col[0], col[1], col[2]))
    lums.sort()
    return rast, {"pixels": n, "valid_nonzero": len(lums), "valid_zero": zero, "invalid": invalid,
                  "luminance_p50": pct(lums, 0.5), "luminance_p99": pct(lums, 0.99),
                  "luminance_max": (lums[-1] if lums else None)}


def surface_hdr_panel_log(tex: Texture, crop, out_w, out_h, p99, mask=None, sentinel=False):
    """Log10(1+c) display, normalized by a declared per-panel p99."""
    rast = Raster(out_w, out_h, BG)
    denom = math.log10(1.0 + max(1e-12, p99))
    n = 0
    zero = 0
    invalid = 0
    for ox, oy, t in iter_rect(tex, crop, out_w, out_h):
        if mask is not None and not mask[oy * out_w + ox]:
            rast.set(ox, oy, NAVY)
            continue
        r, g, b = t[0], t[1], t[2]
        n += 1
        why = hdr_is_invalid(r, g, b, sentinel)
        if why:
            invalid += 1
            rast.set(ox, oy, MAGENTA if why == "sentinel" else (YELLOW if why == "nonfinite" else BLUE))
        elif r == 0.0 and g == 0.0 and b == 0.0:
            zero += 1
            rast.set(ox, oy, BLACK)
        else:
            col = []
            for c in (r, g, b):
                q = math.log10(1.0 + max(0.0, c)) / denom
                col.append(u8(srgb_oetf(max(0.0, min(1.0, q)))))
            rast.set(ox, oy, (col[0], col[1], col[2]))
    return rast, {"pixels": n, "valid_nonzero": n - zero - invalid, "valid_zero": zero,
                  "invalid": invalid, "display_log_p99": p99}


def card_albedo_panel(tex: Texture, crop, out_w, out_h, mask):
    rast = Raster(out_w, out_h, BG)
    alpha = []
    off = nonfinite = 0
    for ox, oy, t in iter_rect(tex, crop, out_w, out_h):
        if not mask[oy * out_w + ox]:
            rast.set(ox, oy, NAVY)
            off += 1
            continue
        if not all(math.isfinite(v) for v in t[:3]):
            nonfinite += 1
            rast.set(ox, oy, YELLOW)
            continue
        rgb = [max(0.0, min(1.0, v * v)) for v in t[:3]]  # Target0 = sqrt(DiffuseColor)
        alpha.append(t[3])
        rast.set(ox, oy, (u8(srgb_oetf(rgb[0])), u8(srgb_oetf(rgb[1])), u8(srgb_oetf(rgb[2]))))
    alpha.sort()
    return rast, {"common_valid_pixels": out_w * out_h - off, "outside_mask_pixels": off,
                  "nonfinite_pixels": nonfinite,
                  "opacity_min": (alpha[0] if alpha else None),
                  "opacity_p50": pct(alpha, 0.5), "opacity_max": (alpha[-1] if alpha else None)}


def card_normal_panel(tex: Texture, crop, out_w, out_h, mask):
    rast = Raster(out_w, out_h, BG)
    lens = []
    off = nonfinite = 0
    for ox, oy, t in iter_rect(tex, crop, out_w, out_h):
        if not mask[oy * out_w + ox]:
            rast.set(ox, oy, NAVY)
            off += 1
            continue
        if not all(math.isfinite(v) for v in t[:3]):
            nonfinite += 1
            rast.set(ox, oy, YELLOW)
            continue
        nx = max(-1.0, min(1.0, t[0] * 2.0 - 1.0))
        ny = max(-1.0, min(1.0, t[1] * 2.0 - 1.0))
        nz = math.sqrt(max(1.0 - nx * nx - ny * ny, 0.0001))
        lens.append(math.sqrt(nx * nx + ny * ny + nz * nz))
        rast.set(ox, oy, (u8(nx * 0.5 + 0.5), u8(ny * 0.5 + 0.5), u8(nz * 0.5 + 0.5)))
    lens.sort()
    return rast, {"common_valid_pixels": out_w * out_h - off, "outside_mask_pixels": off,
                  "nonfinite_pixels": nonfinite,
                  "card_normal_length_mean": (sum(lens) / len(lens)) if lens else None,
                  "card_normal_length_fraction_within_0.01_of_1": (
                      sum(1 for v in lens if abs(v - 1.0) <= 0.01) / len(lens) if lens else None)}


def card_depth_panel(tex: Texture, crop, out_w, out_h, mask):
    vals = []
    off = invalid = 0
    rast = Raster(out_w, out_h, BG)
    for ox, oy, t in iter_rect(tex, crop, out_w, out_h):
        v = t[0]
        if not mask[oy * out_w + ox]:
            off += 1
            rast.set(ox, oy, NAVY)
        elif not math.isfinite(v) or v < 0.0 or v > 1.0:
            invalid += 1
            rast.set(ox, oy, BLUE)
        else:
            vals.append(v)
            rast.set(ox, oy, gray(v))
    vals.sort()
    return rast, {"common_valid_pixels": out_w * out_h - off, "outside_mask_pixels": off,
                  "invalid_depth_pixels": invalid,
                  "device_z_min": (vals[0] if vals else None),
                  "device_z_p50": pct(vals, 0.5),
                  "device_z_max": (vals[-1] if vals else None)}


def fig_surface_cache(ctx: Context):
    # ---------------------------------------------------------- material layers
    occ = ctx.page_occupancy()
    ob = occ["mapped_bbox_xyxy"]
    crop = (0, 0, min(4096, ob[2] + 16), min(4096, ob[3] + 16))
    w, h = crop[2], crop[3]
    mask = ctx.occupancy_mask(crop)
    common = sum(1 for v in mask if v)
    if common == 0:
        raise RuntimeError("proven mapped-page occupancy is empty")

    alb, ra, la = ctx.texture("Lumen.SceneAlbedo", event_prefix="CompressToSurfaceCache Albedo")
    nrm, rn, ln = ctx.texture("Lumen.SceneNormal", event_prefix="CompressToSurfaceCache Normal")
    emi, re, le = ctx.texture("Lumen.SceneEmissive", event_prefix="CompressToSurfaceCache Emissive")
    dep, rd, ld = ctx.texture("Lumen.SceneDepth", event_prefix="CopyToSurfaceCache Depth")
    for t, expected in ((alb, 4096), (nrm, 4096), (emi, 4096), (dep, 4096)):
        if t.w != expected or t.h != expected:
            raise RuntimeError(f"{t.name} extent {t.w}x{t.h} is not the {expected} physical atlas")

    p_alb, s_alb = surface_albedo_panel(alb, crop, w, h, mask)
    p_nrm, s_nrm = surface_normal_panel(nrm, crop, w, h, mask)
    _, emi_stats = surface_hdr_panel(emi, crop, w, h, 1.0, mask, sentinel=False)
    emi_scale = hdr_scale_from(emi_stats)
    p_emi, _ = surface_hdr_panel(emi, crop, w, h, emi_scale, mask, sentinel=False)
    p_dep, s_dep = surface_depth_panel(dep, crop, w, h, mask)
    emi_stats = dict(emi_stats)
    emi_stats["common_valid_pixels"] = s_alb["common_valid_pixels"]
    sheet = contact_sheet(
        [p_alb, p_nrm, p_emi, p_dep], 1, w, h,
        ["ALBEDO (sqrt-decoded)", "CARD-SPACE NORMAL", "EMISSIVE (HDR)", "SURFACE-CACHE DEPTH"],
    )
    panels = [
        {"name": "albedo", "source": alb.rid, "pass": ctx.pass_of(la).get("Id")},
        {"name": "normal", "source": nrm.rid, "pass": ctx.pass_of(ln).get("Id")},
        {"name": "emissive", "source": emi.rid, "pass": ctx.pass_of(le).get("Id")},
        {"name": "depth", "source": dep.rid, "pass": ctx.pass_of(ld).get("Id")},
    ]
    page_proof = {
        "resource_id": occ["resource_id"], "name": occ["name"],
        "payload": occ["payload"], "version_pointer": occ["version_pointer"],
        "pass_id": occ["pass_id"], "pass_event": occ["pass_event"],
        "page_stride_bytes": occ["page_stride_bytes"], "page_count": occ["page_count"],
        "mapped_page_count": occ["mapped_page_count"], "atlas_size": occ["atlas_size"],
        "mapped_bbox_xyxy": occ["mapped_bbox_xyxy"], "mapped_test": occ["mapped_test"],
        "source_schema": occ["source_schema"], "crop_xywh": list(crop),
        "union_pixels_in_crop": common, "off_page_pixels_in_crop": w * h - common,
        "pages": occ["pages"],
    }
    st = {"albedo": s_alb, "normal": s_nrm, "emissive": emi_stats,
          "emissive_display_scale": emi_scale, "depth": s_dep,
          "common_valid_pixels": common, "off_page_pixels": w * h - common,
          "source_crop_xywh": list(crop), "panel_size": [w, h],
          "panel_common_counts": {p["name"]: common for p in panels}}
    add_figure(
        ctx, "surface-cache-material-layers",
        "Lumen Surface Cache: material layers (albedo / normal / emissive / depth)",
        "graph %s | mapped-page crop %s | %d mapped pages, %d common texels" % (
            ctx.selected_graph, crop, occ["mapped_page_count"], common),
        sheet,
        [("mapped physical page texel", (180, 180, 180)), ("off-page/unmapped (masked)", NAVY),
         ("emissive HDR (shared scale %.3g)" % emi_scale, (220, 160, 100)),
         ("nonfinite (should be 0 here)", YELLOW)],
        [
            "Occupancy is decoded from Lumen.PageBuffer (5 float4/page): mapped test SizeInTexels.x>0 and .y>0; PhysicalAtlasCoord = uint2(PhysicalAtlasUVRect.xy * 4096 + 0.5), max corner from .zw. Unmapped/stale pages are masked navy, not decoded as data (source schema resource.lumen-card-page-data).",
            "The four panels share one exact occupancy mask and one 1:1 crop; the crop is the mapped-page bbox plus 16 texels. No panel uses its own nonzero test.",
            "Lumen.SceneAlbedo: DecodeSurfaceCacheAlbedo squares the stored sqrt(albedo); DiffuseColorBoost is a material parameter not present in the payload and is not applied (LumenSurfaceCache.ush:44-47).",
            "Lumen.SceneNormal: BC5 stores card-space normal.xy*0.5+0.5; the figure reconstructs z=sqrt(1-x^2-y^2) and displays N*0.5+0.5 (card space, not world) (LumenSurfaceCache.ush:25-33).",
            "Lumen.SceneEmissive: linear HDR; every mapped texel is exactly zero in this capture (stats.emissive.valid_nonzero = 0), so the panel is intentionally black. Lumen.SceneDepth: PF_G16 / 65535; 1.0 is the EncodeSurfaceCacheDepth invalid sentinel and is categorical blue.",
        ],
        alb, la, st,
        {"name": "SurfaceCacheMaterialLayers", "units": "albedo 0..1 linear; normal unit vector; emissive radiance; depth 0..1",
         "domain": "surface cache (physical atlas texels)", "description": "Material layers of the Lumen surface cache on proven mapped pages only."},
        ["albedo panel: RGB = decoded linear albedo, then sRGB OETF for display",
         "normal panel: card-space normal reconstructed from BC5 RG",
         "emissive panel: HDR tone map", "depth panel: PF_G16 / 65535"],
        {"rule": "unmapped Lumen.CardPageData page (SizeInTexels.x==0) is masked navy; depth value 1.0 is invalid blue",
         "semantics": "navy is no mapped page, not a valid zero", "color": color_hex(NAVY)},
        {"transform": "per-panel, declared in channel_mapping; one shared 1:1 occupancy mask",
         "content_rect_xywh": [0, 0, sheet.w, sheet.h], "source_crop_xywh": list(crop),
         "panels": panels},
        "surface-cache",
        extra={"atlas_crop_xywh": list(crop), "page_occupancy": page_proof},
    )

    # ---------------------------------------------------------- exact card capture
    choice = ctx.card_capture_choice()
    ccrop = tuple(choice["crop_xywh"])
    cw, ch = ccrop[2], ccrop[3]
    cmask = choice["mask"]
    names = list(choice["panels"].keys())
    card_tex = {}
    for n in names:
        panel = choice["panels"][n]
        card_tex[n] = Texture(ctx.dump, panel["resource_id"], ctx.res_by_id[panel["resource_id"]], panel["layout"])
    try:
        p_ca, s_ca = card_albedo_panel(card_tex["Lumen.CardCaptureAlbedoAtlas"], ccrop, cw, ch, cmask)
        p_cn, s_cn = card_normal_panel(card_tex["Lumen.CardCaptureNormalAtlas"], ccrop, cw, ch, cmask)
        _, ce_stats = surface_hdr_panel(card_tex["Lumen.CardCaptureEmissiveAtlas"], ccrop, cw, ch, 1.0, cmask, sentinel=False)
        ce_scale = hdr_scale_from(ce_stats)
        p_ce, _ = surface_hdr_panel(card_tex["Lumen.CardCaptureEmissiveAtlas"], ccrop, cw, ch, ce_scale, cmask, sentinel=False)
        p_cd, s_cd = card_depth_panel(card_tex["Lumen.CardCaptureDepthStencilAtlas"], ccrop, cw, ch, cmask)
    finally:
        for t in card_tex.values():
            t.close()
    ce_stats = dict(ce_stats)
    ce_stats["common_valid_pixels"] = choice["common_valid_pixels"]
    common = choice["common_valid_pixels"]
    # 5x nearest display of the 80x80 source crop (16x16 card + 8-texel border).
    up = 5
    panels = [upscale_nearest(p, up) for p in (p_ca, p_cn, p_ce, p_cd)]
    sheet = contact_sheet(
        panels, 2, cw * up, ch * up,
        ["CARD ALBEDO (sqrt-decoded)", "TARGET1.a-GATED NORMAL",
         "CARD EMISSIVE (HDR, valid zero)", "CARD DEPTH (device Z)"],
    )
    rect = choice["rect_xywh"]
    same_pass = [
        {"name": n, "resource_id": p["resource_id"], "version_pointer": p["version_pointer"],
         "version_pass_id": p["version_pass_id"], "version_pass_event": p["version_pass_event"],
         "payload": p["payload"]}
        for n, p in choice["panels"].items()
    ]
    card_proof = {
        "pass_id": choice["pass"]["pass_id"], "pass_pointer": choice["pass"]["pass_pointer"],
        "pass_event": choice["pass"]["event"],
        "view_content": choice["pass"]["view"]["content"],
        "view_metadata": choice["pass"]["view"]["metadata"],
        "view_bin_sha256": choice["pass"]["view"]["bin_sha256"],
        "view_rect_min": choice["pass"]["view"]["view_rect_min"],
        "view_size_and_inv_size": choice["pass"]["view"]["view_size_and_inv_size"],
        "exact_rect_xywh": rect, "crop_xywh": list(ccrop), "margin": choice["margin"],
        "common_valid_pixels": common,
        "outside_mask_pixels": cw * ch - common,
        "panel_common_counts": {"albedo": s_ca["common_valid_pixels"], "normal": s_cn["common_valid_pixels"],
                                "emissive": ce_stats.get("common_valid_pixels"), "depth": s_cd["common_valid_pixels"]},
        "same_pass_versions": same_pass, "write_proof": choice["write_proof"],
        "clear": choice["clear"],
        "current_frame_union_pixels": choice["current_frame_union_pixels"],
        "prior_rect_overlap_pixels": choice["prior_rect_overlap_pixels"],
        "target1a_alpha_inside_union": choice["target1a_alpha_inside_union"],
        "target1a_alpha_outside_current_frame_union": choice["target1a_alpha_outside_current_frame_union"],
        "target1a_raw_nonzero_outside_current_frame_union": choice["target1a_raw_nonzero_outside_current_frame_union"],
        "all_capture_rects": choice["all_capture_rects"],
        "mask_rule": choice["mask_rule"], "source_schema": choice["source_schema"],
        "mask_xyxy": [rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3]],
    }
    st = {
        "common_valid_pixels": common, "outside_mask_pixels": cw * ch - common,
        "albedo": s_ca, "normal": s_cn, "emissive": ce_stats,
        "emissive_display_scale": ce_scale, "depth": s_cd,
        "exact_rect_xywh": rect, "crop_xywh": list(ccrop),
        "finite_missing_stats": {
            "albedo_nonfinite": s_ca["nonfinite_pixels"],
            "normal_nonfinite": s_cn["nonfinite_pixels"],
            "emissive_nonfinite": ce_stats.get("invalid_samples", 0),
            "depth_invalid": s_cd["invalid_depth_pixels"],
            "outside_mask": cw * ch - common,
        },
    }
    card_source_tex = Texture(
        ctx.dump, choice["panels"]["Lumen.CardCaptureAlbedoAtlas"]["resource_id"],
        ctx.res_by_id[choice["panels"]["Lumen.CardCaptureAlbedoAtlas"]["resource_id"]],
        choice["panels"]["Lumen.CardCaptureAlbedoAtlas"]["layout"])
    add_figure(
        ctx, "card-capture-layers",
        "Lumen card capture: one exact live CardPage pass",
        "graph %s | pass %s %s | exact rect %s | Target1.a mask | %d/%d common texels" % (
            ctx.selected_graph, choice["pass"]["pass_id"], choice["pass"]["event"],
            rect, common, cw * ch),
        sheet,
        [("valid card texel (Target1.a>0.5)", (180, 180, 180)),
         ("outside selected pass rect (masked)", NAVY),
         ("nonfinite (none in mask)", YELLOW),
         ("emissive valid zero (black)", BLACK)],
        [
            "Exact capture rect is this pass's own ViewRectMin=%s and ViewSizeAndInvSize=%s from the %s View payload; the atlas version pointer is the pass pointer for all four resources." % (
                choice["pass"]["view"]["view_rect_min"], choice["pass"]["view"]["view_size_and_inv_size"],
                choice["pass"]["view"]["content"]),
            "All four panels use the same exact-pass version and the same mask: rectangle membership AND Lumen.CardCaptureNormalAtlas.a>0.5 (Target1.a is the source-proved shader valid flag, LumenCardBasePass.ush:145-147; LumenSurfaceCache.usf:64 uses >0.5).",
            "Byte diff against the immediately preceding atlas version proves the written update: albedo %d, normal %d, depth %d changed texels inside the rect and %d/%d/%d outside; emissive is unchanged because every valid card emissive texel is exactly zero." % (
                choice["write_proof"]["Lumen.CardCaptureAlbedoAtlas"]["changed_inside_rect"],
                choice["write_proof"]["Lumen.CardCaptureNormalAtlas"]["changed_inside_rect"],
                choice["write_proof"]["Lumen.CardCaptureDepthStencilAtlas"]["changed_inside_rect"],
                choice["write_proof"]["Lumen.CardCaptureAlbedoAtlas"]["changed_outside_rect"],
                choice["write_proof"]["Lumen.CardCaptureNormalAtlas"]["changed_outside_rect"],
                choice["write_proof"]["Lumen.CardCaptureDepthStencilAtlas"]["changed_outside_rect"]),
            "The 8-texel border is categorical: it is outside the selected exact-pass rectangle, so stale atlas texels there are never decoded as this pass's material.",
            "The selected rectangle overlaps earlier current-frame CardPage rectangles on %d texels, so it is not a reused rect; even if it were, the 4096 changed-inside / 0 changed-outside diff proves this pass overwrote the full rectangle." % (
                choice["prior_rect_overlap_pixels"]),
        ],
        card_source_tex,
        choice["panels"]["Lumen.CardCaptureAlbedoAtlas"]["layout"], st,
        {"name": "LumenCardCaptureLayers", "units": "albedo linear / normal / emissive radiance / device Z",
         "domain": "card capture atlas (exact pass rect)", "description": "One exact live CardPage capture with a shared Target1.a validity mask."},
        ["albedo: square(Target0.rgb), sRGB OETF for display", "normal: decode Target1.a-gated Target1.xy, reconstruct z",
         "emissive: Target2 HDR (valid zero here)", "depth: PF_R32_FLOAT raw device Z"],
        {"rule": "outside exact pass rect OR Target1.a<=0.5", "semantics": "navy is not this pass's live card data; black is a valid zero", "color": color_hex(NAVY)},
        {"transform": "per-panel; exact 80x80 crop upscaled 5x nearest",
         "content_rect_xywh": [0, 0, sheet.w, sheet.h], "source_crop_xywh": list(ccrop),
         "panels": [{"name": n, "source": p["resource_id"], "pass": p["version_pass_id"]}
                    for n, p in choice["panels"].items()]},
        "surface-cache",
        extra={"card_capture": card_proof, "atlas_crop_xywh": list(ccrop)},
    )
    card_source_tex.close()

    # ---------------------------------------------------------- full atlas explorer
    union = [False] * (512 * 512)
    for rec in choice["all_capture_rects"]:
        rx, ry, rw, rh = rec["rect_xywh"]
        for yy in range(ry, ry + rh):
            for xx in range(rx, rx + rw):
                union[yy * 512 + xx] = True
    union_px = sum(1 for v in union if v)
    # Read the full atlas from the exact selected albedo version.
    atlas_tex = Texture(ctx.dump,
                        choice["panels"]["Lumen.CardCaptureAlbedoAtlas"]["resource_id"],
                        ctx.res_by_id[choice["panels"]["Lumen.CardCaptureAlbedoAtlas"]["resource_id"]],
                        choice["panels"]["Lumen.CardCaptureAlbedoAtlas"]["layout"])
    rast = Raster(512, 512, BG)
    for yy in range(512):
        for xx in range(512):
            if not union[yy * 512 + xx]:
                rast.set(xx, yy, NAVY)
                continue
            t = atlas_tex.texel(xx, yy)
            rgb = [max(0.0, min(1.0, v * v)) for v in t[:3]]
            rast.set(xx, yy, (u8(srgb_oetf(rgb[0])), u8(srgb_oetf(rgb[1])), u8(srgb_oetf(rgb[2]))))
    # Outline the chosen exact pass rectangle.
    for xx in range(rect[0], rect[0] + rect[2]):
        rast.set(xx, rect[1], GREEN)
        rast.set(xx, rect[1] + rect[3] - 1, GREEN)
    for yy in range(rect[1], rect[1] + rect[3]):
        rast.set(rect[0], yy, GREEN)
        rast.set(rect[0] + rect[2] - 1, yy, GREEN)
    atlas_layout = choice["panels"]["Lumen.CardCaptureAlbedoAtlas"]["layout"]
    add_figure(
        ctx, "card-capture-atlas",
        "Lumen card capture: full atlas live-rect union",
        "graph %s | %d exact CardPage rects, %d live texels | green = selected pass %s rect %s" % (
            ctx.selected_graph, len(choice["all_capture_rects"]), union_px,
            choice["pass"]["pass_id"], rect),
        rast,
        [("current-frame live card texel", (180, 180, 180)), ("not written this frame (masked)", NAVY),
         ("selected exact pass rect", GREEN)],
        [
            "Full 512x512 CardCaptureAlbedoAtlas at the selected pass version, retained as the atlas explorer context; only the union of the current frame's exact CardPage View rectangles is decoded.",
            "Outside that union, %d texels carry raw non-zero Target1.a bytes from stale/previous-frame atlas content; only %d exceed the 0.5 validity threshold. All are categorical navy here and are excluded from card-capture-layers." % (
                choice["target1a_raw_nonzero_outside_current_frame_union"],
                choice["target1a_alpha_outside_current_frame_union"]),
            "Inside the union, Target1.a>0.5 on %d texels; the selected pass has %d common valid texels and %d changed-inside-rect texels in the preceding-version byte diff." % (
                choice["target1a_alpha_inside_union"], choice["common_valid_pixels"],
                choice["write_proof"]["Lumen.CardCaptureAlbedoAtlas"]["changed_inside_rect"]),
        ],
        atlas_tex, atlas_layout,
        {"union_pixels": union_px,
         "stale_alpha_outside_union": choice["target1a_alpha_outside_current_frame_union"],
         "raw_nonzero_alpha_outside_union": choice["target1a_raw_nonzero_outside_current_frame_union"],
         "selected_pass_id": choice["pass"]["pass_id"], "selected_rect_xywh": rect,
         "rect_count": len(choice["all_capture_rects"]), "atlas_size": [512, 512]},
        {"name": "LumenCardCaptureAtlasContext", "units": "atlas texel membership",
         "domain": "card capture atlas", "description": "Full atlas context with current-frame live-rect union mask."},
        ["albedo: square(Target0.rgb), sRGB OETF", "navy: not written by any current-frame CardPage pass"],
        {"rule": "outside current-frame CardPage rect union", "semantics": "navy includes stale previous-frame bytes, not live data", "color": color_hex(NAVY)},
        {"transform": "sqrt decode + sRGB OETF", "content_rect_xywh": [0, 0, 512, 512],
         "source_crop_xywh": [0, 0, 512, 512]},
        "surface-cache",
        extra={"card_capture_atlas": {"union_pixels": union_px,
                                      "selected_pass_id": choice["pass"]["pass_id"],
                                      "selected_rect_xywh": rect,
                                      "stale_alpha_outside_union": choice["target1a_alpha_outside_current_frame_union"],
                                      "raw_nonzero_alpha_outside_union": choice["target1a_raw_nonzero_outside_current_frame_union"]}},
    )
    atlas_tex.close()

    # ---------------------------------------------------------- direct / indirect / final lighting
    dl, rdl, ldl = ctx.texture("Lumen.SceneDirectLighting", event_prefix="Batched lights")
    fl, rfl, lfl = ctx.texture("Lumen.SceneFinalLighting", event_prefix="CombineLighting CS", latest=True)
    rd, rrd, lrd = ctx.texture("Lumen.ResampledCardCaptureDirectLighting", latest=True)
    ri, rri, lri = ctx.texture("Lumen.ResampledCardCaptureIndirectLighting", latest=True)

    cap_mask = [False] * (512 * 512)
    for rec in choice["all_capture_rects"]:
        rx, ry, rw, rh = rec["rect_xywh"]
        for yy in range(ry, ry + rh):
            for xx in range(rx, rx + rw):
                cap_mask[yy * 512 + xx] = True
    cap_px = sum(1 for v in cap_mask if v)
    if cap_px != choice["current_frame_union_pixels"]:
        raise RuntimeError("capture-union mask count mismatch")
    scene_common = sum(1 for v in mask if v)

    _, s_dl = surface_hdr_panel(dl, crop, w, h, 1.0, mask, sentinel=False)
    _, s_fl = surface_hdr_panel(fl, crop, w, h, 1.0, mask, sentinel=False)
    _, s_rd = surface_hdr_panel(rd, (0, 0, 512, 512), 512, 512, 1.0, cap_mask, sentinel=False)
    _, s_ri = surface_hdr_panel(ri, (0, 0, 512, 512), 512, 512, 1.0, cap_mask, sentinel=False)
    p99_dl = s_dl.get("luminance_p99") or 1.0
    p99_fl = s_fl.get("luminance_p99") or 1.0
    p99_rd = s_rd.get("luminance_p99") or 1.0
    p99_ri = s_ri.get("luminance_p99") or 1.0
    p_dl, _ = surface_hdr_panel_log(dl, crop, w, h, p99_dl, mask, sentinel=False)
    p_fl, _ = surface_hdr_panel_log(fl, crop, w, h, p99_fl, mask, sentinel=False)
    p_rd, _ = surface_hdr_panel_log(rd, (0, 0, 512, 512), 512, 512, p99_rd, cap_mask, sentinel=False)
    p_ri, _ = surface_hdr_panel_log(ri, (0, 0, 512, 512), 512, 512, p99_ri, cap_mask, sentinel=False)
    scene_w, scene_h = w, h
    sheet = composite_sheet(
        [(0, 0, p_dl, "SCENE DIRECT (irradiance)"),
         (0, scene_h, p_fl, "SCENE FINAL (radiance)"),
         (0, 2 * scene_h, p_rd, "RESAMPLED CARD DIRECT"),
         (512, 2 * scene_h, p_ri, "RESAMPLED CARD INDIRECT")],
        max(scene_w, 1024), 2 * scene_h + 512,
    )
    p99_map = {"scene_direct": p99_dl, "resampled_direct": p99_rd,
               "resampled_indirect": p99_ri, "scene_final": p99_fl}
    all_stats = [s_dl, s_fl, s_rd, s_ri]
    for stx in all_stats:
        stx["common_valid_pixels"] = stx.get("pixels")
    add_figure(
        ctx, "surface-cache-lighting",
        "Lumen Surface Cache: direct / indirect / final lighting on proven occupancy",
        "graph %s | scene panels = mapped-page mask, resampled panels = current-frame card-rect union | log10(1+c) per-panel p99" % ctx.selected_graph,
        sheet,
        [("per-panel log tone (see p99 labels)", (210, 170, 110)),
         ("valid zero (black)", BLACK), ("unmapped/off-page (navy)", NAVY),
         ("nonfinite/sentinel", YELLOW)],
        [
            "Scene panels use the exact Lumen.PageBuffer mapped-page mask and the same 1:1 crop as the material figure (%d mapped texels); off-page/stale atlas regions are categorical navy." % scene_common,
            "Resampled panels are the 512x512 card-capture lighting atlases. They are not in the 4096 physical atlas domain: they use the exact current-frame CardPage View-rect union (%d texels, %d passes). The green clear value outside those rects is not data and is masked navy." % (
                cap_px, len(choice["all_capture_rects"])),
            "Lumen.SceneDirectLighting is surface-cache direct irradiance; Lumen.SceneFinalLighting is final shaded radiance (LumenSurfaceCache.ush:49-55). Lumen.ResampledCardCapture{Direct,Indirect}Lighting are resampled card-capture lighting atlases.",
            "Each panel is displayed with log10(1+c) normalized to its own p99 (direct %.4g, resampled-direct %.4g, resampled-indirect %.4g, final %.4g); this is a display transform, not a cross-panel radiometric comparison." % (
                p99_dl, p99_rd, p99_ri, p99_fl),
        ],
        dl, ldl,
        {"per_panel_log_p99": p99_map, "direct": all_stats[0], "final": all_stats[1],
         "resampled_direct": all_stats[2], "resampled_indirect": all_stats[3],
         "scene_common_valid_pixels": scene_common, "capture_union_valid_pixels": cap_px,
         "source_crop_xywh": list(crop), "resampled_source_crop_xywh": [0, 0, 512, 512]},
        {"name": "SurfaceCacheLighting", "units": "direct = irradiance; final = radiance; resampled = lighting atlases",
         "domain": "surface cache (mapped physical pages) and card capture atlas (exact rect union)", "description": "Direct, indirect and final lighting on proven occupancy."},
        ["panel 1: SceneDirectLighting RGB irradiance on mapped pages", "panel 2: ResampledCardCaptureDirectLighting RGB on exact card rects",
         "panel 3: ResampledCardCaptureIndirectLighting RGB on exact card rects", "panel 4: SceneFinalLighting RGB radiance on mapped pages"],
        {"rule": "scene: unmapped Lumen.PageBuffer page; resampled: outside exact current-frame CardPage rects; black is valid zero",
         "semantics": "navy is not data; black is a valid zero", "color": color_hex(NAVY)},
        {"transform": "per-panel log10(1+c) normalized by p99 + sRGB OETF",
         "per_panel_p99": p99_map,
         "content_rect_xywh": [0, 0, sheet.w, sheet.h], "source_crop_xywh": list(crop),
         "panels": [{"name": "scene_direct", "source": dl.rid, "pass": ctx.pass_of(ldl).get("Id")},
                    {"name": "resampled_direct", "source": rd.rid, "pass": ctx.pass_of(lrd).get("Id")},
                    {"name": "resampled_indirect", "source": ri.rid, "pass": ctx.pass_of(lri).get("Id")},
                    {"name": "scene_final", "source": fl.rid, "pass": ctx.pass_of(lfl).get("Id")}]},
        "surface-cache",
        extra={"atlas_crop_xywh": list(crop), "page_occupancy": page_proof,
               "card_capture_union": {"rect_count": len(choice["all_capture_rects"]),
                                      "union_pixels": cap_px,
                                      "mask_rule": "exact current-frame CardPage ViewRectMin/ViewSizeAndInvSize rectangles"}},
    )


# ---------------------------------------------------------------- world radiance cache


def fig_world_cache(ctx: Context):
    # The same name belongs to multiple cache instances. Use the exact volume
    # input and preceding producer of the selected scene's screen-probe trace.
    name = "Lumen.RadianceCache.RadianceProbeIndirectionTexture"
    consumer = next(p for p in ctx.passes
                    if p["Pointer"].startswith(ctx.selected_graph)
                    and p["EventName"] == "TraceGlobalSDFAndApplyRadianceCache")
    inputs = {ref.split(".")[0] for ref in consumer["InputResources"]}
    matches = [rid for rid in inputs if ctx.res_by_id.get(rid, {}).get("Name") == name]
    if len(matches) != 1:
        raise RuntimeError(f"expected one indirection volume for pass {consumer['Id']}, got {matches}")
    t, _, l = ctx.texture(name, rid=matches[0], latest=True, before_pass=int(consumer["Id"]))
    per_z = []
    for z in range(t.depth):
        cnt = 0
        bbox = None
        for y in range(t.h):
            for x in range(t.w):
                wv = t.raw_u32(x, y, z)
                if wv != 0xFFFFFFFF:
                    cnt += 1
                    if bbox is None:
                        bbox = [x, y, x, y]
                    else:
                        bbox = [min(bbox[0], x), min(bbox[1], y), max(bbox[2], x), max(bbox[3], y)]
        per_z.append({"z": z, "valid": cnt, "bbox": bbox})
    occupied = [p for p in per_z if p["valid"] > 0]
    if occupied:
        pick = [occupied[0], occupied[len(occupied) // 2], occupied[-1]]
    else:
        pick = [per_z[0], per_z[len(per_z) // 2], per_z[-1]]
    zs = [p["z"] for p in pick]
    # Union crop around the occupied probes of the selected slices.
    boxes = [p["bbox"] for p in pick if p["bbox"]]
    if boxes:
        x0 = max(0, min(b[0] for b in boxes) - 2)
        y0 = max(0, min(b[1] for b in boxes) - 2)
        x1 = min(t.w, max(b[2] for b in boxes) + 3)
        y1 = min(t.h, max(b[3] for b in boxes) + 3)
    else:
        x0, y0, x1, y1 = 0, 0, t.w, t.h
    cw, ch = max(1, x1 - x0), max(1, y1 - y0)
    panels = []
    stats = {"slices": [], "invalid_total": 0, "valid_total": 0, "max_probe_index": 0,
             "max_adaptive": 0, "slice_dims": [t.w, t.h, t.depth],
             "consumer_pass": consumer["Id"],
             "consumer_pointer": consumer["Pointer"],
             "selected_volume_valid_words": sum(p["valid"] for p in per_z),
             "selected_volume_reason": "exact TraceGlobalSDFAndApplyRadianceCache input, most recent preceding producer",
             "source_crop_xywh": [x0, y0, cw, ch]}
    for z in zs:
        base = Raster(cw, ch, BG)
        counts = {"invalid": 0, "valid": 0, "zero": 0}
        for y in range(ch):
            for x in range(cw):
                wv = t.raw_u32(x0 + x, y0 + y, z)
                if wv == 0xFFFFFFFF:
                    counts["invalid"] += 1
                    base.set(x, y, MAGENTA)
                    continue
                counts["valid"] += 1
                if wv == 0:
                    counts["zero"] += 1
                idx = wv & 0xFFFF
                adaptive = (wv >> 16) & 0x7
                stats["max_probe_index"] = max(stats["max_probe_index"], idx)
                stats["max_adaptive"] = max(stats["max_adaptive"], adaptive)
                base.set(x, y, cat_color((idx % 11) + 1))
        stats["invalid_total"] += counts["invalid"]
        stats["valid_total"] += counts["valid"]
        stats["slices"].append({"z": z, "source_crop_xywh": [x0, y0, cw, ch], **counts})
        panel = fit_panel(base, 768, 192)
        panels.append(panel)
    sheet = Raster(768, len(panels) * 208, (0, 0, 0))
    for i, p in enumerate(panels):
        y = i * 208
        sheet.blit(p, 0, y)
        sheet.draw_text(4, y + 194, "Z SLICE %d / %d | crop x=%d y=%d w=%d h=%d | magenta = invalid 0xFFFFFFFF" % (
            zs[i], t.depth - 1, x0, y0, cw, ch), WHITE, 1)
    add_figure(
        ctx, "world-cache-radiance-indirection-slices",
        "World radiance cache: representative indirection slices",
        "graph %s | Texture3D %dx%dx%d | occupied slices %s" % (ctx.selected_graph, t.w, t.h, t.depth, zs),
        sheet,
        [("valid probe index (categorical low 16 bits)", cat_color(3)),
         ("invalid page (0xFFFFFFFF)", MAGENTA), ("valid zero word", cat_color(1))],
        [
            "Source: LumenRadianceCacheInterpolation.ush:302-317 defines EncodeProbeIndirection/DecodeProbeIndirection: 0xFFFFFFFF = invalid; low 16 bits = probe index; bits 16..18 = NumAdaptiveProbes.",
            "This is the world-cache indirection volume read by TraceGlobalSDFAndApplyRadianceCache. The first, middle and last occupied z slices use a shared crop around their occupied probes.",
            "Invalid is a categorical magenta page, never radiance=0. A valid zero probe index is a different colour; no page is resampled or interpolated.",
        ],
        t, l,
        stats,
        {"name": "RadianceProbeIndirection", "units": "packed uint32 (probe index + adaptive count)",
         "domain": "world radiance cache clipmaps", "description": "Probe indirection page state, not radiance."},
        ["low 16 bits = ProbeIndex", "bits 16..18 = NumAdaptiveProbes", "0xFFFFFFFF = invalid probe"],
        {"rule": "word == 0xFFFFFFFF", "semantics": "invalid/unmapped probe page", "color": color_hex(MAGENTA)},
        {"transform": "categorical color by probe index; invalid magenta; fit to 768x192 panels",
         "content_rect_xywh": [0, 0, sheet.w, sheet.h], "source_crop_xywh": [x0, y0, cw, ch],
         "slices": zs, "upscale": "fit to 768x192"},
        "world-cache",
    )


# ---------------------------------------------------------------- reflections


def fig_reflections(ctx: Context):
    tr, rtr, ltr = ctx.texture("Lumen.Reflections.TraceRadiance", event_prefix="TraceVoxels")
    th, rth, lth = ctx.texture("Lumen.Reflections.TraceHit", event_prefix="TraceVoxels")
    crop = view_crop(ctx, tr)
    vw, vh = crop[2], crop[3]
    if int(ctx.pass_of(ltr).get("Id")) != int(ctx.pass_of(lth).get("Id")):
        raise RuntimeError("TraceRadiance and TraceHit resolved to different passes")

    # Trace radiance with validity taken from the signed hit distance.
    rast = Raster(vw, vh, BG)
    lums = []
    counts = {"unused": 0, "hit": 0, "miss": 0, "sentinel": 0, "nonfinite": 0, "zero": 0}
    for oy in range(vh):
        for ox in range(vw):
            h = th.texel(crop[0] + ox, crop[1] + oy)[0]
            neg = h < 0.0 or (h == 0.0 and math.copysign(1.0, h) < 0.0)
            unused = abs(h) < 0.1 or not math.isfinite(h)
            r, g, b, _ = tr.texel(crop[0] + ox, crop[1] + oy)
            if unused:
                counts["unused"] += 1
                rast.set(ox, oy, NAVY)
                continue
            counts["hit" if neg else "miss"] += 1
            why = hdr_is_invalid(r, g, b, sentinel=True)
            if why == "sentinel":
                counts["sentinel"] += 1
                rast.set(ox, oy, MAGENTA)
                continue
            if why:
                counts["nonfinite"] += 1
                rast.set(ox, oy, YELLOW)
                continue
            if r == 0.0 and g == 0.0 and b == 0.0:
                counts["zero"] += 1
                rast.set(ox, oy, BLACK)
                continue
            lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
            if lum > 0 and math.isfinite(lum):
                lums.append(lum)
    lums.sort()
    scale = hdr_scale_from({"luminance_p99": pct(lums, 0.99)})
    for oy in range(vh):
        for ox in range(vw):
            h = th.texel(crop[0] + ox, crop[1] + oy)[0]
            neg = h < 0.0 or (h == 0.0 and math.copysign(1.0, h) < 0.0)
            unused = abs(h) < 0.1 or not math.isfinite(h)
            if unused:
                continue
            r, g, b, _ = tr.texel(crop[0] + ox, crop[1] + oy)
            if hdr_is_invalid(r, g, b, sentinel=True):
                continue
            if r == 0.0 and g == 0.0 and b == 0.0:
                continue
            rast.set(ox, oy, (u8(srgb_oetf(r * scale / (1 + r * scale))),
                              u8(srgb_oetf(g * scale / (1 + g * scale))),
                              u8(srgb_oetf(b * scale / (1 + b * scale)))))
    add_figure(
        ctx, "reflection-trace-radiance",
        "Lumen reflections: trace radiance (valid tiles only)",
        "graph %s | TraceVoxels pass | signed hit-distance validity | scale %.4g" % (ctx.selected_graph, scale),
        rast,
        [("valid radiance (shared tonemap)", (200, 170, 120)),
         ("unused trace tile (hit=+0, radiance is not a measurement)", NAVY),
         ("INVALID_LIGHTING sentinel (65024,65024,64512)", MAGENTA),
         ("valid zero radiance", BLACK), ("non-finite", YELLOW)],
        [
            "TraceHit is encoded by LumenReflectionCommon.ush:89-99: EncodeRayDistance = HitDistance * (-1 if hit else +1); negative = hit, positive = miss, +0 = cleared/unused. Unused tiles are not radiance=0.",
            "The trace texture is a 1280x888 array slice 0; the figure is the exact 1280x720 view rect. The sentinel triple is the source INVALID_LIGHTING from StochasticLightingCommon.ush:189-193, not a guessed threshold.",
            "This is pre-resolve trace radiance; valid zero and unused zero are rendered differently (black vs navy).",
        ],
        tr, ltr,
        {"pixels": vw * vh, "counts": counts,
         "valid_radiance_luminance_p50": pct(lums, 0.50), "valid_radiance_luminance_p99": pct(lums, 0.99),
         "valid_radiance_luminance_max": (lums[-1] if lums else None), "display_scale": scale},
        {"name": "TraceRadiance", "units": "linear RGB radiance (pre-resolve trace)", "domain": "reflection ray texels",
         "description": "Radiance returned by the Lumen reflection trace; only valid trace texels are shown."},
        ["R/G/B = PF_FloatRGB (R11G11B10) linear radiance"],
        {"rule": "TraceHit +0 / non-finite", "semantics": "unused trace tile, not zero radiance", "color": color_hex(NAVY)},
        {"transform": "per-figure shared p99 exposure + Reinhard + sRGB OETF", "scale": scale,
         "content_rect_xywh": [0, 0, vw, vh], "source_crop_xywh": list(crop), "resample": "none"},
        "reflection",
    )

    # Hit / miss / distance.
    rast = Raster(vw, vh, BG)
    dists = []
    hc = {"unused": 0, "hit": 0, "miss": 0, "nonfinite": 0}
    for oy in range(vh):
        for ox in range(vw):
            h = th.texel(crop[0] + ox, crop[1] + oy)[0]
            neg = h < 0.0 or (h == 0.0 and math.copysign(1.0, h) < 0.0)
            unused = abs(h) < 0.1 or not math.isfinite(h)
            if unused:
                hc["unused"] += 1
                rast.set(ox, oy, NAVY)
                continue
            d = abs(h)
            if d > 0:
                dists.append(d)
            hc["hit" if neg else "miss"] += 1
            rast.set(ox, oy, heat(0.0) if d <= 0 else (0, 0, 0))
    # second pass for a distance-normalized heat map
    dmax = max(dists) if dists else 1.0
    lo = math.log10(max(1e-6, min(dists))) if dists else 0.0
    hi = math.log10(max(1e-6, dmax))
    for oy in range(vh):
        for ox in range(vw):
            h = th.texel(crop[0] + ox, crop[1] + oy)[0]
            neg = h < 0.0 or (h == 0.0 and math.copysign(1.0, h) < 0.0)
            unused = abs(h) < 0.1 or not math.isfinite(h)
            if unused:
                continue
            d = abs(h)
            if not neg:
                rast.set(ox, oy, BLUE)
            else:
                t = (math.log10(max(1e-6, d)) - lo) / (hi - lo) if hi > lo else 0.5
                rast.set(ox, oy, heat(t))
    add_figure(
        ctx, "reflection-trace-hit",
        "Lumen reflections: hit / miss and hit distance",
        "graph %s | TraceVoxels pass | signed float16 ray distance" % ctx.selected_graph,
        rast,
        [("hit distance (log10, near=blue -> far=red)", (200, 120, 60)),
         ("miss (positive distance, no surface hit)", BLUE),
         ("unused trace tile", NAVY)],
        [
            "EncodeRayDistance stores negative distance for a hit and positive distance for a miss. The figure keeps that sign: blue = miss, heat = hit, navy = cleared/unused.",
            "Hit distance is in Unreal units (cm), the ray's t value. The first decoding pass used raw distance; the manifest records hit/miss counts and min/max.",
            "A miss is still a traced ray (sky/radiance-cache); it is not an invalid tile.",
        ],
        th, lth,
        {"pixels": vw * vh, "counts": hc, "hit_distance_min_cm": (min(dists) if dists else None),
         "hit_distance_p50_cm": pct(sorted(dists), 0.5), "hit_distance_max_cm": dmax},
        {"name": "ReflectionTraceHit", "units": "Unreal units (cm); sign = hit/miss", "domain": "reflection ray texels",
         "description": "Signed ray distance from LumenReflectionTracing; not a depth buffer."},
        ["R = PF_R16F signed ray distance (negative = hit, positive = miss)"],
        {"rule": "value +0 and not negative-zero", "semantics": "unused/cleared trace tile", "color": color_hex(NAVY)},
        {"transform": "categorical miss/unused; log10 hit distance heat palette", "content_rect_xywh": [0, 0, vw, vh],
         "source_crop_xywh": list(crop), "resample": "none"},
        "reflection",
    )

    rr, rrr, lrr = ctx.texture("Lumen.Reflections.ResolvedSpecularIndirect", event_prefix="ReflectionsResolve")
    rdp, rrdp, ldp = ctx.texture("Lumen.Reflections.ResolvedSpecularIndirectDepth", event_prefix="ReflectionsResolve")
    rr_stats = collect_hdr(rr, crop, vw, vh, sentinel=True, stride=2)
    rr_scale = hdr_scale_from(rr_stats)
    rast = plot_hdr(rr, crop, vw, vh, rr_scale, sentinel=True)
    add_figure(
        ctx, "reflection-resolved-radiance",
        "Lumen reflections: resolved specular indirect",
        "graph %s | ReflectionsResolve pass | source-proved INVALID_LIGHTING sentinel" % ctx.selected_graph,
        rast,
        [("valid radiance (shared tonemap)", (200, 170, 120)),
         ("invalid/empty reflection tile (65024,65024,64512)", MAGENTA),
         ("valid zero radiance", BLACK), ("non-finite", YELLOW)],
        [
            "The invalid tile value is not guessed: StochasticLightingCommon.ush:189-193 defines INVALID_LIGHTING = half3(65024.0, 65024.0, 64512.0) and IsLightingValid = !all(V == INVALID_LIGHTING). The resolve writes Center.Lighting = INVALID_LIGHTING for missing lighting (LumenReflectionResolve.usf:111-112).",
            "Empty tiles are drawn magenta; a valid zero radiance is drawn black. They are never merged.",
            "Resolved radiance is linear RGB; the display scale is the 99th percentile of valid positive luminance for this figure, declared in display.transform.",
        ],
        rr, lrr,
        {"pixels": vw * vh, "valid_stats": rr_stats, "display_scale": rr_scale,
         "invalid_lighting_triple": list(INVALID_LIGHTING)},
        {"name": "ResolvedSpecularIndirect", "units": "linear RGB radiance", "domain": "reflection resolve",
         "description": "Resolved specular indirect reflection radiance."},
        ["R/G/B = PF_FloatRGB (R11G11B10) linear radiance", "invalid tile = exact INVALID_LIGHTING triple"],
        {"rule": "exact INVALID_LIGHTING triple", "semantics": "invalid/empty reflection tile, not radiance=0", "color": color_hex(MAGENTA)},
        {"transform": "Reinhard c/(1+c) + sRGB OETF; per-figure scale", "scale": rr_scale,
         "content_rect_xywh": [0, 0, vw, vh], "source_crop_xywh": list(crop), "resample": "none"},
        "reflection",
    )

    rast = Raster(vw, vh, BG)
    dv = []
    dc = {"miss_or_inf": 0, "hit": 0, "zero": 0}
    for oy in range(vh):
        for ox in range(vw):
            v = rdp.texel(crop[0] + ox, crop[1] + oy)[0]
            if not math.isfinite(v) or v >= 60000.0:
                dc["miss_or_inf"] += 1
                rast.set(ox, oy, BLUE)
            elif v <= 0.0:
                dc["zero"] += 1
                rast.set(ox, oy, BLACK)
            else:
                dc["hit"] += 1
                dv.append(v)
    dv.sort()
    lo = math.log10(max(1e-6, dv[0])) if dv else 0.0
    hi = math.log10(max(1e-6, dv[-1])) if dv else 1.0
    for oy in range(vh):
        for ox in range(vw):
            v = rdp.texel(crop[0] + ox, crop[1] + oy)[0]
            if math.isfinite(v) and 0 < v < 60000.0:
                t = (math.log10(max(1e-6, v)) - lo) / (hi - lo) if hi > lo else 0.5
                rast.set(ox, oy, heat(t))
    add_figure(
        ctx, "reflection-resolved-depth",
        "Lumen reflections: resolved hit distance",
        "graph %s | ReflectionsResolve pass | FLOAT_32_MAX -> half inf = no hit" % ctx.selected_graph,
        rast,
        [("hit distance (log10, near=blue -> far=red)", (200, 120, 60)),
         ("no valid hit (inf / >=60000)", BLUE), ("zero distance", BLACK)],
        [
            "LumenReflectionResolve.usf:650 writes Center.MinRayHitT; InitReflectionLighting sets FLOAT_32_MAX for no hit. In the PF_R16F destination that becomes +inf (or a value at the half maximum); it is displayed as a categorical miss, not distance 0.",
            "The figure is the exact 1280x720 view rect of the ResolvedSpecularIndirectDepth array slice 0.",
        ],
        rdp, ldp,
        {"pixels": vw * vh, "counts": dc, "hit_distance_min_cm": (dv[0] if dv else None),
         "hit_distance_p50_cm": pct(dv, 0.5), "hit_distance_max_cm": (dv[-1] if dv else None)},
        {"name": "ResolvedSpecularIndirectDepth", "units": "Unreal units (cm)", "domain": "reflection resolve",
         "description": "Minimum hit distance from the reflection resolve; inf = no hit."},
        ["R = PF_R16F hit distance; FLOAT_32_MAX/no-hit saturates to inf"],
        {"rule": "non-finite or >=60000", "semantics": "no valid hit", "color": color_hex(BLUE)},
        {"transform": "categorical no-hit; log10 hit distance heat palette", "content_rect_xywh": [0, 0, vw, vh],
         "source_crop_xywh": list(crop), "resample": "none"},
        "reflection",
    )


# ---------------------------------------------------------------- screen probes


def fig_screen_probes(ctx: Context):
    spd, rspd, lspd = ctx.texture("Lumen.ScreenProbeGather.ScreenProbeSceneDepth", latest=True)
    spn, rspn, lspn = ctx.texture("Lumen.ScreenProbeGather.ScreenProbeWorldNormal", latest=True)
    # Depth is stored as asfloat(uint), one value per probe.
    depth_vals = {}
    dvals = []
    for y in range(spd.h):
        for x in range(spd.w):
            word = spd.raw_u32(x, y)
            v = struct.unpack("<f", struct.pack("<I", word))[0]
            depth_vals[(x, y)] = v
            if math.isfinite(v) and v > 0.0:
                dvals.append(v)
    dvals.sort()
    valid_pts = [(x, y) for (x, y), v in depth_vals.items() if math.isfinite(v) and v > 0.0]
    if valid_pts:
        x0 = max(0, min(p[0] for p in valid_pts) - 1)
        y0 = max(0, min(p[1] for p in valid_pts) - 1)
        x1 = min(spd.w, max(p[0] for p in valid_pts) + 2)
        y1 = min(spd.h, max(p[1] for p in valid_pts) + 2)
    else:
        x0, y0, x1, y1 = 0, 0, spd.w, spd.h
    cw, ch = max(1, x1 - x0), max(1, y1 - y0)
    lo = math.log10(max(1e-6, dvals[0])) if dvals else 0.0
    hi = math.log10(max(1e-6, dvals[-1])) if dvals else 1.0
    base = Raster(cw, ch, BG)
    for y in range(ch):
        for x in range(cw):
            v = depth_vals.get((x0 + x, y0 + y), -1.0)
            if math.isfinite(v) and v > 0.0:
                t = (math.log10(max(1e-6, v)) - lo) / (hi - lo) if hi > lo else 0.5
                base.set(x, y, heat(t))
            else:
                base.set(x, y, MAGENTA)
    probe_depth = base.scaled(640, max(1, int(round(640 * ch / cw))))
    add_figure(
        ctx, "screen-probe-scene-depth",
        "Lumen screen probes: scene depth per probe",
        "graph %s | occupied probe crop x=%d y=%d w=%d h=%d | asfloat(uint) depth" % (
            ctx.selected_graph, x0, y0, cw, ch),
        probe_depth,
        [("probe scene depth (log10, cm): near=blue -> far=red", (200, 120, 60)),
         ("invalid probe (depth <= 0 or non-finite)", MAGENTA)],
        [
            "Source: ScreenProbeSceneDepth is Texture2D<uint>, read as asfloat (LumenScreenProbeCommon.ush:78-86). Negative depth means an invalid/unlit probe; those are magenta, not depth 0.",
            "The texture is 80x84 probe entries (the probe atlas), not a 1280x720 screen image. The crop covers the occupied probe rows/columns (%d..%d, %d..%d); unused bottom rows are omitted and recorded here." % (x0, x1 - 1, y0, y1 - 1),
            "Depth is linear world-space depth in Unreal units (cm). The occupied crop is scaled to 640 px wide with nearest-neighbour sampling.",
        ],
        spd, lspd,
        {"probe_count": spd.w * spd.h, "valid_count": len(dvals), "invalid_count": spd.w * spd.h - len(dvals),
         "depth_min_cm": (dvals[0] if dvals else None), "depth_p50_cm": pct(dvals, 0.5),
         "depth_max_cm": (dvals[-1] if dvals else None), "source_crop_xywh": [x0, y0, cw, ch]},
        {"name": "ScreenProbeSceneDepth", "units": "Unreal units (cm)", "domain": "screen-probe atlas",
         "description": "Linear scene depth sampled for each screen probe."},
        ["R = uint bits reinterpreted as float32 (asfloat)"],
        {"rule": "depth <= 0 or non-finite", "semantics": "invalid/unlit probe", "color": color_hex(MAGENTA)},
        {"transform": "log10 depth heat palette; scaled to 640 px wide", "content_rect_xywh": [0, 0, probe_depth.w, probe_depth.h],
         "source_crop_xywh": [x0, y0, cw, ch], "resample": "nearest to 640 px wide"},
        "screen-probe",
    )

    # World normal: OctahedronToUnitVector(enc*2-1).
    base = Raster(cw, ch, BG)
    normals = []
    for y in range(ch):
        for x in range(cw):
            v = depth_vals.get((x0 + x, y0 + y), -1.0)
            if not math.isfinite(v) or v <= 0.0:
                base.set(x, y, MAGENTA)
                continue
            t = spn.texel(x0 + x, y0 + y)
            ox, oy = t[0] * 2.0 - 1.0, t[1] * 2.0 - 1.0
            nx = ox
            ny = oy
            nz = 1.0 - abs(ox) - abs(oy)
            tt = max(-nz, 0.0)
            if nx >= 0:
                nx -= tt
            else:
                nx += tt
            if ny >= 0:
                ny -= tt
            else:
                ny += tt
            l = math.sqrt(nx * nx + ny * ny + nz * nz)
            if l <= 0:
                continue
            nx, ny, nz = nx / l, ny / l, nz / l
            normals.append((nx, ny, nz))
            base.set(x, y, (u8(nx * 0.5 + 0.5), u8(ny * 0.5 + 0.5), u8(nz * 0.5 + 0.5)))
    probe_normal = base.scaled(640, max(1, int(round(640 * ch / cw))))
    add_figure(
        ctx, "screen-probe-world-normal",
        "Lumen screen probes: world normal per probe",
        "graph %s | occupied probe crop x=%d y=%d w=%d h=%d | octahedral RG8" % (
            ctx.selected_graph, x0, y0, cw, ch),
        probe_normal,
        [("world normal N*0.5+0.5", (128, 128, 255)), ("invalid probe (depth <= 0)", MAGENTA)],
        [
            "Source: GetScreenProbeNormal = OctahedronToUnitVector(ScreenProbeWorldNormal * 2 - 1) (LumenScreenProbeCommon.ush:89-92; OctahedralCommon.ush:28-34).",
            "The stored RG8 is an octahedral direction, not two independent angles. Probes with non-positive scene depth are invalid and drawn magenta; their stale normal bytes are not decoded as a direction.",
            "The same occupied probe crop as the depth figure is used; unused bottom rows of the 80x84 atlas are omitted and recorded in display.source_crop_xywh.",
        ],
        spn, lspn,
        {"probe_count": spn.w * spn.h, "valid_count": len(normals), "invalid_count": spn.w * spn.h - len(normals),
         "normal_space": "world space", "octahedral_encoding": "RG8 -> oct*2-1",
         "source_crop_xywh": [x0, y0, cw, ch]},
        {"name": "ScreenProbeWorldNormal", "units": "unit vector", "domain": "world space",
         "description": "World normal stored as octahedral RG8 per probe."},
        ["R/G = octahedral coordinates, 8-bit UNORM", "decoded with OctahedronToUnitVector"],
        {"rule": "probe scene depth <= 0", "semantics": "invalid probe; normal is not a direction", "color": color_hex(MAGENTA)},
        {"transform": "normal N*0.5+0.5 -> RGB; scaled to 640 px wide", "content_rect_xywh": [0, 0, probe_normal.w, probe_normal.h],
         "source_crop_xywh": [x0, y0, cw, ch], "resample": "nearest to 640 px wide"},
        "screen-probe",
    )

    # Lighting PDF (R16F, 640x672).  The shader normalizes it to [0,1]
    # (PDF / max(PDFSum, 0.0001)); out-of-range values are invalid, not PDF.
    pdf, rpdf, lpdf = ctx.texture("Lumen.ScreenProbeGather.LightingProbabilityDensityFunction", latest=True)
    good = []
    over = 0
    neg = 0
    nonfinite = 0
    zero = 0
    for y in range(pdf.h):
        for x in range(pdf.w):
            v = pdf.texel(x, y)[0]
            if not math.isfinite(v):
                nonfinite += 1
            elif v < 0.0:
                neg += 1
            elif v > 1.0:
                over += 1
            elif v == 0.0:
                zero += 1
            else:
                good.append(v)
    good.sort()
    p99 = pct(good, 0.99) or 1.0
    base = Raster(pdf.w, pdf.h, BG)
    for y in range(pdf.h):
        for x in range(pdf.w):
            v = pdf.texel(x, y)[0]
            if not math.isfinite(v):
                base.set(x, y, LIGHTGREY)
            elif v < 0.0:
                base.set(x, y, MAGENTA)
            elif v > 1.0:
                base.set(x, y, YELLOW)
            elif v == 0.0:
                base.set(x, y, BLACK)
            else:
                base.set(x, y, heat(min(1.0, v / p99)))
    add_figure(
        ctx, "screen-probe-lighting-pdf",
        "Lumen screen probes: lighting PDF (normalized range)",
        "graph %s | 640x672 octahedral probe atlas | R16F | valid normalized PDF 0..1" % ctx.selected_graph,
        base,
        [("normalized PDF: 0 (black) .. p99 %.6g (red)" % p99, (200, 120, 60)),
         ("PDF > 1: out of normalized range (yellow)", YELLOW),
         ("PDF < 0 (magenta)", MAGENTA), ("non-finite (light grey)", LIGHTGREY),
         ("valid zero PDF", BLACK)],
        [
            "Source: LumenScreenProbeImportanceSampling.usf:190 writes PDF / max(PDFSum, 0.0001), so a valid normalized PDF lies in [0,1]. The stored payload contains 130,774 halfs >1, 48,061 <0 and 1 non-finite sample; those cannot be a normalized PDF and are categorical (yellow/magenta/grey), not silently clamped into the valid map.",
            "The 251,244 valid samples are displayed on value/p99 of the valid [0,1] range (p99 = %.6g). The manifest records every class count. PDF units are the engine's normalized per-texel importance PDF, not radiance." % p99,
            "This is a diagnostic of the PDF payload; the invalid classes are not used in any radiance figure.",
        ],
        pdf, lpdf,
        {"texels": pdf.w * pdf.h, "valid_0_to_1": len(good), "zero": zero,
         "over_one": over, "negative": neg, "nonfinite": nonfinite,
         "pdf_min": (good[0] if good else None), "pdf_p50": pct(good, 0.5),
         "pdf_p99": p99, "pdf_max": (good[-1] if good else None)},
        {"name": "LightingProbabilityDensityFunction", "units": "normalized PDF in [0,1] per texel",
         "domain": "octahedral probe atlas", "description": "Importance-sampling PDF used by screen-probe integration."},
        ["R = PF_R16F normalized PDF"],
        {"rule": "value < 0, > 1, or non-finite", "semantics": "out of the source-proved normalized PDF range; categorical, not clamped", "color": color_hex(MAGENTA)},
        {"transform": "valid 0..1: value/p99 heat palette; zero black; out-of-range categorical",
         "content_rect_xywh": [0, 0, base.w, base.h],
         "source_crop_xywh": [0, 0, pdf.w, pdf.h], "resample": "none"},
        "screen-probe",
    )

    # Trace hit encoding (uint) and filtered hit distance (R8).
    th, rth, lth = ctx.texture("Lumen.ScreenProbeGather.TraceHit", latest=True)
    base = Raster(th.w, th.h, BG)
    hc = {"unused": 0, "miss": 0, "hit": 0, "invalid_distance": 0,
          "radiance_cache": 0, "moving": 0}
    dists = []
    for y in range(th.h):
        for x in range(th.w):
            word = th.raw_u32(x, y)
            dist = struct.unpack("<f", struct.pack("<I", word & 0x7FFFFFFC))[0]
            bhit = (word & 1) != 0
            moving = (word & 2) != 0
            rc = (word >> 31) != 0
            if moving:
                hc["moving"] += 1
            if rc:
                hc["radiance_cache"] += 1
            if not bhit and dist == 0.0:
                hc["unused"] += 1
                base.set(x, y, NAVY)
                continue
            if not bhit:
                hc["miss"] += 1
                base.set(x, y, BLUE)
                continue
            if not math.isfinite(dist) or dist < 1e-3 or dist > 1e7:
                hc["invalid_distance"] += 1
                base.set(x, y, LIGHTGREY)
                continue
            hc["hit"] += 1
            dists.append(dist)
    dists.sort()
    lo = math.log10(max(1e-6, dists[0])) if dists else 0.0
    hi = math.log10(max(1e-6, dists[-1])) if dists else 1.0
    for y in range(th.h):
        for x in range(th.w):
            word = th.raw_u32(x, y)
            dist = struct.unpack("<f", struct.pack("<I", word & 0x7FFFFFFC))[0]
            if (word & 1) == 0:
                continue
            if not math.isfinite(dist) or dist < 1e-3 or dist > 1e7:
                continue
            if (word >> 31) != 0:
                base.set(x, y, ORANGE)
            else:
                t = (math.log10(max(1e-6, dist)) - lo) / (hi - lo) if hi > lo else 0.5
                base.set(x, y, heat(t))
    add_figure(
        ctx, "screen-probe-trace-hit",
        "Lumen screen probes: trace hit distance and ray flags",
        "graph %s | 640x672 octahedral trace atlas | packed uint" % ctx.selected_graph,
        base,
        [("hit distance (log10, near=blue -> far=red)", (200, 120, 60)),
         ("miss (bHit=0, distance>0)", BLUE), ("reached radiance cache (bit31)", ORANGE),
         ("unused (bHit=0, distance=0)", NAVY),
         ("invalid/out-of-range hit distance (<=0 or >1e7)", LIGHTGREY)],
        [
            "Source: LumenScreenProbeCommon.ush:239-269 EncodeProbeRayDistance stores asuint(HitDistance)&0x7FFFFFFC with bit0=bHit, bit1=bMoving, bit31=bReachedRadianceCache; DecodeProbeRayDistance reverses it.",
            "A miss is a traced ray that ended in sky/radiance cache, not an invalid tile. Unused texels are navy.",
        ],
        th, lth,
        {"texels": th.w * th.h, "counts": hc, "hit_distance_min_cm": (dists[0] if dists else None),
         "hit_distance_p50_cm": pct(dists, 0.5), "hit_distance_max_cm": (dists[-1] if dists else None)},
        {"name": "ScreenProbeTraceHit", "units": "Unreal units (cm) + flag bits", "domain": "octahedral trace atlas",
         "description": "Packed hit distance and per-ray flags for screen-probe tracing."},
        ["bits 2..30 = float32 hit distance bits (mask 0x7FFFFFFC)", "bit0 = bHit", "bit1 = bMoving", "bit31 = bReachedRadianceCache"],
        {"rule": "bHit=0 and distance=0", "semantics": "unused trace texel", "color": color_hex(NAVY)},
        {"transform": "categorical miss/unused/radiance-cache; log10 hit distance heat", "content_rect_xywh": [0, 0, base.w, base.h],
         "source_crop_xywh": [0, 0, th.w, th.h], "resample": "none"},
        "screen-probe",
    )

    hd, rhd, lhd = ctx.texture("Lumen.ScreenProbeGather.ScreenProbeHitDistance", latest=True)
    encs = []
    inv = 0
    miss = 0
    for y in range(hd.h):
        for x in range(hd.w):
            enc = hd.texel(x, y)[0]  # PF_R8 -> 0..1
            linear = enc * enc * 65504.0
            d = (linear - 1.0 / 255.0) * 255.0 / 254.0
            if d < 0.0 or not math.isfinite(d):
                inv += 1
            elif d >= 65504.0 * 0.999:
                miss += 1
            else:
                encs.append(d)
    encs.sort()
    lo = math.log10(max(1e-6, encs[0])) if encs else 0.0
    hi = math.log10(max(1e-6, encs[-1])) if encs else 1.0
    base = Raster(hd.w, hd.h, BG)
    for y in range(hd.h):
        for x in range(hd.w):
            enc = hd.texel(x, y)[0]
            linear = enc * enc * 65504.0
            d = (linear - 1.0 / 255.0) * 255.0 / 254.0
            if d < 0.0 or not math.isfinite(d):
                base.set(x, y, MAGENTA)
            elif d >= 65504.0 * 0.999:
                base.set(x, y, BLUE)
            else:
                t = (math.log10(max(1e-6, d)) - lo) / (hi - lo) if hi > lo else 0.5
                base.set(x, y, heat(t))
    add_figure(
        ctx, "screen-probe-hit-distance",
        "Lumen screen probes: filtered hit distance",
        "graph %s | 640x672 R8 UNORM | sqrt-encoded, MaxHalfFloat=65504" % ctx.selected_graph,
        base,
        [("hit distance (log10, near=blue -> far=red)", (200, 120, 60)),
         ("no hit: encoded at MaxHalfFloat (65504)", BLUE),
         ("invalid ray (decoded < 0)", MAGENTA)],
        [
            "Source: LumenScreenProbeTracingCommon.ush:48-57 encodes sqrt(HitDistance/65504)*254/255 + 1/255 into R8 UNORM and decodes with (enc^2*65504 - 1/255)*255/254. The decode is applied exactly.",
            "GetProbeMaxHitDistance() is MaxHalfFloat = 65504 (LumenScreenProbeCommon.ush:234-237); the encoded maximum is a no-hit/miss, drawn blue and excluded from the hit-distance distribution.",
            "The R8 stores 8 bits of precision; the figure is a display of the decoded distance, not a claim of full float precision.",
        ],
        hd, lhd,
        {"texels": hd.w * hd.h, "valid_count": len(encs), "miss_at_max": miss, "invalid_count": inv,
         "distance_min_cm": (encs[0] if encs else None), "distance_p50_cm": pct(encs, 0.5),
         "distance_max_cm": (encs[-1] if encs else None)},
        {"name": "ScreenProbeHitDistance", "units": "Unreal units (cm)", "domain": "octahedral gather atlas",
         "description": "Filtered minimum hit distance per gather texel; max encoding means no hit."},
        ["R = R8 UNORM encoding of sqrt(distance/65504)"],
        {"rule": "decoded distance < 0, or encoded maximum (no hit)", "semantics": "invalid ray / no hit", "color": color_hex(MAGENTA)},
        {"transform": "log10 decoded distance heat palette; no-hit blue", "content_rect_xywh": [0, 0, base.w, base.h],
         "source_crop_xywh": [0, 0, hd.w, hd.h], "resample": "none"},
        "screen-probe",
    )

    # Radiance and filtered radiance atlases.
    for slug, title, name, ev, note in [
        ("screen-probe-radiance", "Lumen screen probes: radiance atlas",
         "Lumen.ScreenProbeGather.ScreenProbeRadiance", None,
         "ScreenProbeRadiance is the 640x672 octahedral probe atlas (80x84 probes x 8x8 texels). It stores radiance, not irradiance."),
        ("screen-probe-filtered-radiance", "Lumen screen probes: filtered radiance atlas",
         "Lumen.ScreenProbeGather.ScreenProbeFilteredRadiance", "FilterRadianceWithGather",
         "ScreenProbeFilteredRadiance is the spatially/temporally filtered octahedral radiance atlas consumed by screen-probe integration."),
    ]:
        st, rst, lst = ctx.texture(name, event_prefix=ev, latest=True)
        cstats = collect_hdr(st, (0, 0, st.w, st.h), st.w, st.h, sentinel=True, stride=2)
        sscale = hdr_scale_from(cstats)
        rast = plot_hdr(st, (0, 0, st.w, st.h), st.w, st.h, sscale, sentinel=True)
        add_figure(
            ctx, slug, title,
            "graph %s | %s | pass %s | linear radiance" % (
                ctx.selected_graph, name, ctx.pass_of(lst).get("Id")),
            rast,
            [("radiance (shared per-figure tonemap)", (200, 170, 120)),
             ("INVALID_LIGHTING sentinel", MAGENTA), ("valid zero", BLACK), ("non-finite", YELLOW)],
            [note,
             "The exact INVALID_LIGHTING triple (65024,65024,64512) is masked as a categorical invalid tile; a valid zero radiance remains black.",
             "The atlas is 640x672 texels, not a screen image; the octahedral 8x8 texel blocks are visible at native resolution."],
            st, lst,
            {"texels": st.w * st.h, "hdr": cstats, "display_scale": sscale},
            {"name": name.split(".")[-1], "units": "linear RGB radiance", "domain": "octahedral probe atlas",
             "description": "Radiance stored per octahedral probe texel."},
            ["R/G/B = PF_FloatR11G11B10 linear radiance"],
            {"rule": "exact INVALID_LIGHTING triple", "semantics": "invalid probe texel, not radiance=0", "color": color_hex(MAGENTA)},
            {"transform": "per-figure shared p99 exposure + Reinhard + sRGB OETF", "scale": sscale,
             "content_rect_xywh": [0, 0, st.w, st.h], "source_crop_xywh": [0, 0, st.w, st.h], "resample": "none"},
            "screen-probe",
        )

    # Screen-space integrated diffuse and rough-specular.
    for slug, title, name, ev, qname, qdesc in [
        ("screen-probe-diffuse-indirect", "Lumen screen probes: diffuse indirect",
         "Lumen.ScreenProbeGather.DiffuseIndirect", "ScreenProbeIntegrate SupportAll",
         "irradiance/pi", "DiffuseIndirectOutput = DiffuseLighting * Diffuse_Lambert(1) = irradiance/pi (LumenScreenProbeGather.usf:1459,1494-1495); the composite later multiplies by diffuse colour."),
        ("screen-probe-rough-specular", "Lumen screen probes: rough specular indirect",
         "Lumen.ScreenProbeGather.RoughSpecularIndirect", "ScreenProbeIntegrate SupportAll",
         "rough-specular radiance estimate", "RoughSpecularIndirect is the screen-probe rough-specular radiance estimate; the composite applies the material specular colour/EnvBRDF."),
    ]:
        st, rst, lst = ctx.texture(name, event_prefix=ev, latest=True)
        crop2 = view_crop(ctx, st)
        cstats = collect_hdr(st, crop2, crop2[2], crop2[3], sentinel=True, stride=3)
        sscale = hdr_scale_from(cstats)
        rast = plot_hdr(st, crop2, crop2[2], crop2[3], sscale, sentinel=True)
        add_figure(
            ctx, slug, title,
            "graph %s | %s | pass %s | view rect 1280x720" % (
                ctx.selected_graph, ev, ctx.pass_of(lst).get("Id")),
            rast,
            [("valid %s (shared tonemap)" % qname, (200, 170, 120)),
             ("INVALID_LIGHTING sentinel", MAGENTA), ("valid zero", BLACK), ("non-finite", YELLOW)],
            [qdesc,
             "The array is a 1280x888 Texture2DArray slice 0; only the exact 1280x720 view rect is shown.",
             "Empty/invalid tiles are categorical magenta; a valid zero is black. The display scale is the 99th percentile of this figure's valid radiance, recorded in display.transform."],
            st, lst,
            {"pixels": crop2[2] * crop2[3], "hdr": cstats, "display_scale": sscale},
            {"name": qname, "units": "linear RGB (irradiance/pi or radiance)", "domain": "screen",
             "description": qdesc},
            ["R/G/B = PF_FloatR11G11B10 linear values"],
            {"rule": "exact INVALID_LIGHTING triple", "semantics": "invalid tile, not zero", "color": color_hex(MAGENTA)},
            {"transform": "per-figure shared p99 exposure + Reinhard + sRGB OETF", "scale": sscale,
             "content_rect_xywh": [0, 0, crop2[2], crop2[3]], "source_crop_xywh": list(crop2), "resample": "none"},
            "screen-probe",
        )

    ctx.unavailable.append({
        "item": "Lumen.ScreenProbeGather.BRDFProbabilityDensityFunctionSH",
        "reason": "SHBufferNotRendered",
        "evidence": "The buffer has 60,480 half values = 80*84 probes * 9 SH coefficients. A direction-resolved SH figure would require applying the engine band weights and octahedral convolution; the per-texel LightingProbabilityDensityFunction figure is included instead. The raw buffer remains in the final dump.",
    })
    ctx.unavailable.append({
        "item": "Lumen screen-probe interpolation weights",
        "reason": "NotStoredAsAResource",
        "evidence": "The 4-probe bilinear/plane weights are computed inside ScreenProbeIntegrateCS from depth/normal/depth weights and are not a dumped RDG resource. The PDF and hit-distance inputs are included.",
    })


# ---------------------------------------------------------------- VSM


def vsm_state_color(v: int):
    if v == 0xFFFFFFFF:
        return LIGHTGREY
    if v == 0:
        return (35, 35, 35)
    any_lod = (v >> 31) & 1
    render = (v >> 30) & 1
    secondary = (v >> 29) & 1
    lod = (v >> 20) & 0x3F
    if secondary:
        return MAGENTA
    if render:
        return ORANGE
    if any_lod and lod > 0:
        return BLUE
    if any_lod and lod == 0:
        return GREEN
    return YELLOW


def fig_vsm(ctx: Context):
    mb, rmb, lmb = ctx.texture("Shadow.Virtual.MaskBits", event_prefix="VirtualShadowMapProjection")
    crop = view_crop(ctx, mb)
    panels = []
    nib_stats = []
    for idx in range(16):
        panel = Raster(320, 180, (0, 0, 0))
        nz = 0
        mx = 0
        for oy in range(180):
            sy = crop[1] + min(crop[3] - 1, int(oy * crop[3] / 180))
            for ox in range(320):
                sx = crop[0] + min(crop[2] - 1, int(ox * crop[2] / 320))
                word = mb.raw_u32(sx, sy, word=0 if idx < 8 else 1)
                nib = (word >> (4 * (idx % 8))) & 0xF
                if nib:
                    nz += 1
                    mx = max(mx, nib)
                panel.set(ox, oy, gray(nib / 15.0))
        nib_stats.append({"nibble": idx, "nonzero_pixels": nz, "max_value": mx})
        panels.append(panel)
    sheet = contact_sheet(panels, 4, 320, 180,
                          [f"NIBBLE {i}" for i in range(16)])
    add_figure(
        ctx, "vsm-maskbits-nibbles",
        "Virtual Shadow Map projection: 16 packed mask nibbles",
        "graph %s | VirtualShadowMapProjection pass | R32G32_UINT, 16 x 4-bit masks" % ctx.selected_graph,
        sheet,
        [("mask nibble 0 (black) .. 15 (white)", (128, 128, 128))],
        [
            "Shadow.Virtual.MaskBits is PF_R32G32_UINT: 16 packed 4-bit mask values per pixel, one per light/grid slot. Nibble 0 is the low nibble of the first uint; nibble 8 is the low nibble of the second uint.",
            "The panels preserve all 16 fields; no combined mask is computed. The 4x4 sheet is a 4x downscale of the 1280x720 view rect.",
            "Nonzero nibbles in this capture: %s. The other nibbles are valid all-clear zero masks, not missing data." % (
                ", ".join(str(x["nibble"]) for x in nib_stats if x["nonzero_pixels"] > 0) or "none"),
        ],
        mb, lmb,
        {"pixels": crop[2] * crop[3], "nibbles": nib_stats},
        {"name": "Shadow.Virtual.MaskBits", "units": "16 x 4-bit shadow mask", "domain": "screen",
         "description": "Packed VSM projection mask bits from the final projection pass."},
        ["R32 low word nibbles 0..7", "G32 high word nibbles 8..15", "each nibble is a 4-bit packed light-grid mask"],
        {"rule": "no invalid encoding; zero is a valid all-clear nibble", "semantics": "0 is a valid mask value", "color": None},
        {"transform": "nibble/15 -> grayscale; each panel 4x nearest downsample", "content_rect_xywh": [0, 0, 1280, 720],
         "source_crop_xywh": list(crop), "panels": 16},
        "vsm",
    )

    pt, rpt, lpt = ctx.texture("Shadow.Virtual.PageTable", latest=True)
    counts = {}
    # Overview: full page table, downsampled to 1280x30.
    overview = Raster(1280, 30, BG)
    for oy in range(30):
        sy = min(pt.h - 1, int(oy * pt.h / 30))
        for ox in range(1280):
            sx = min(pt.w - 1, int(ox * pt.w / 1280))
            v = pt.raw_u32(sx, sy)
            counts[v] = counts.get(v, 0) + 1
            overview.set(ox, oy, vsm_state_color(v))
    # Densest 1280x192 native window.
    best_x, best_score = 0, -1
    for x0 in range(0, max(1, pt.w - 1280 + 1), 64):
        score = 0
        for y in range(0, pt.h, 8):
            for x in range(x0, min(pt.w, x0 + 1280), 8):
                if pt.raw_u32(x, y) != 0:
                    score += 1
        if score > best_score:
            best_x, best_score = x0, score
    zoom = Raster(1280, pt.h, BG)
    for y in range(pt.h):
        for x in range(1280):
            v = pt.raw_u32(min(pt.w - 1, best_x + x), y)
            zoom.set(x, y, vsm_state_color(v))
    sheet = Raster(1280, 30 + 14 + pt.h + 16, (0, 0, 0))
    sheet.blit(overview, 0, 0)
    sheet.draw_text(4, 32, "FULL PAGE TABLE %dx%d (x-downsampled) | invalid=0 | mapped LOD0=green | coarser=blue | renderable=orange | secondary=magenta | dummy=white" % (pt.w, pt.h), WHITE, 1)
    sheet.blit(zoom, 0, 48)
    sheet.draw_text(4, 48 + pt.h + 2, "NATIVE 1280x%d WINDOW at x=%d (densest region)" % (pt.h, best_x), WHITE, 1)
    add_figure(
        ctx, "vsm-page-table-state",
        "Virtual Shadow Map: page table state",
        "graph %s | PropagateMappedMips version | PF_R32_UINT %dx%d" % (ctx.selected_graph, pt.w, pt.h),
        sheet,
        [("invalid (0)", (35, 35, 35)), ("mapped at this LOD", GREEN), ("mapped coarser LOD", BLUE),
         ("valid for rendering", ORANGE), ("secondary request", MAGENTA), ("dummy 0xFFFFFFFF", LIGHTGREY)],
        [
            "Source: ShadowDecodePageTable / ShadowEncodePageTable (VirtualShadowMapPageAccessCommon.ush:247-286): bits 0..9 = physical X, 10..19 = physical Y, 20..25 = LODOffset, bit29 = secondary, bit30 = valid-for-rendering, bit31 = any LOD valid; 0 = invalid; VirtualShadowMapArray.cpp:985 records 0xFFFFFFFF as the dummy page.",
            "The full 8192x192 table is compressed horizontally in the overview; the lower panel is a native 1280x192 window at the densest x offset, so page-state pixels are not resampled there.",
            "This is page state, not VSM depth/radiance. Renderer debug screenshots from the baseline run are not used or relabelled.",
        ],
        pt, lpt,
        {"table_extent": [pt.w, pt.h], "distinct_words": len(counts),
         "invalid_zero_count": sum(v for k, v in counts.items() if k == 0),
         "dummy_count": sum(v for k, v in counts.items() if k == 0xFFFFFFFF),
         "densest_window_x": best_x, "dummy_value": "0xFFFFFFFF"},
        {"name": "Shadow.Virtual.PageTable", "units": "packed virtual page state", "domain": "VSM page table",
         "description": "Packed page table mapping and flags, not physical page contents."},
        ["bits 0..9 physical X", "bits 10..19 physical Y", "bits 20..25 LODOffset",
         "bit29 secondary request", "bit30 valid for rendering", "bit31 any LOD valid", "0 = invalid", "0xFFFFFFFF = dummy"],
        {"rule": "word == 0 (invalid) or 0xFFFFFFFF (dummy)", "semantics": "no mapped page / sentinel dummy", "color": color_hex((35, 35, 35))},
        {"transform": "categorical state colours; overview x-downsample; zoom native", "content_rect_xywh": [0, 0, sheet.w, sheet.h],
         "source_crop_xywh": [0, 0, pt.w, pt.h], "zoom_x": best_x},
        "vsm",
    )


# ---------------------------------------------------------------- manifest / inventory / html


def dump_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def engine_frame(ctx: Context):
    for p in ctx.passes:
        for scope in p.get("ParentEventScopes") or []:
            m = re.search(r"Frame\s+(\d+)", scope)
            if m:
                return int(m.group(1))
    return None


def write_inventory(ctx: Context):
    INV_DIR.mkdir(parents=True, exist_ok=True)
    summary = dict(ctx.inventory_summary)
    summary["engine_frame"] = engine_frame(ctx)
    summary["selected_view"] = {
        "content": ctx.selected_view["content"],
        "metadata": ctx.selected_view["metadata"],
        "sha256": ctx.selected_view["bin_sha256"],
        "view_rect_min": ctx.selected_view["view_rect_min"],
        "view_size_and_inv_size": ctx.selected_view["view_size_and_inv_size"],
        "state_frame_index_mod8": ctx.selected_view["state_frame_index_mod8"],
        "max_pass_id": ctx.selected_view["max_pass_id"],
    }
    summary["inventory_files"] = {
        "passes": "inventory/passes.json",
        "resources": "inventory/resources.json",
        "payloads": "inventory/payloads.json",
        "graph_index": "inventory/graph-index.json",
    }
    dump_json(INV_DIR / "summary.json", summary)
    dump_json(INV_DIR / "passes.json", {
        "schema": "unreal-understanding-final-pass-inventory-v1",
        "dump": str(ctx.dump),
        "count": len(ctx.inventory_passes),
        "items": ctx.inventory_passes,
    })
    dump_json(INV_DIR / "resources.json", {
        "schema": "unreal-understanding-final-resource-inventory-v1",
        "dump": str(ctx.dump),
        "count": len(ctx.inventory_resources),
        "items": ctx.inventory_resources,
    })
    dump_json(INV_DIR / "payloads.json", {
        "schema": "unreal-understanding-final-payload-inventory-v1",
        "dump": str(ctx.dump),
        "count": len(ctx.inventory_payloads),
        "items": ctx.inventory_payloads,
    })
    graphs = []
    for pref in sorted(ctx.view_payloads):
        views = sorted(ctx.view_payloads[pref], key=lambda v: v["content"])
        res = [x for x in ctx.inventory_resources if x["graph"] == pref]
        pas = [x for x in ctx.inventory_passes if x["graph"] == pref]
        graphs.append({
            "prefix": pref,
            "selected": pref == ctx.selected_graph,
            "view_payloads": [
                {
                    "content": v["content"],
                    "metadata": v["metadata"],
                    "sha256": v["bin_sha256"],
                    "view_rect_min": v["view_rect_min"],
                    "view_size_and_inv_size": v["view_size_and_inv_size"],
                    "state_frame_index_mod8": v["state_frame_index_mod8"],
                    "max_pass_id": v["max_pass_id"],
                    "is_1280x720_main": (
                        v["view_rect_min"] == [0.0, 0.0, 0.0, 0.0]
                        and v["view_size_and_inv_size"] is not None
                        and abs(v["view_size_and_inv_size"][0] - 1280.0) < 1e-6
                        and abs(v["view_size_and_inv_size"][1] - 720.0) < 1e-6
                    ),
                }
                for v in views
            ],
            "resource_count": len(res),
            "pass_count": len(pas),
            "resources": [{"id": x["id"], "name": x["name"], "format": x["format"],
                           "type": x["type"], "extent": x["extent"], "files": x["file_count"]}
                          for x in res],
            "passes": [{"id": x["id"], "event": x["event"], "pointer": x["pointer"]} for x in pas],
        })
    dump_json(INV_DIR / "graph-index.json", {
        "schema": "unreal-understanding-final-root-explorer-v1",
        "dump": str(ctx.dump),
        "selected_graph": ctx.selected_graph,
        "selection_rule": "exact 1280x720 View payload at rect (0,0); highest pass id among graphs",
        "graphs": graphs,
    })


def portable_path(p: Path) -> str:
    """Store package-relative paths for files inside figures/; else absolute."""
    try:
        rel = Path(p).resolve().relative_to(Path(FIG_DIR).resolve())
        return str(rel).replace("\\", "/")
    except ValueError:
        return str(p)


def build_manifest(ctx: Context):
    figures = sorted(ctx.figures, key=lambda f: f["slug"])
    sources = {}
    for s in ctx.source_checks:
        sources[s["file"]] = s
    graphs = []
    for pref in sorted(ctx.view_payloads):
        views = sorted(ctx.view_payloads[pref], key=lambda v: v["content"])
        graphs.append({
            "prefix": pref,
            "selected": pref == ctx.selected_graph,
            "view_payloads": [
                {
                    "content": v["content"], "metadata": v["metadata"],
                    "sha256": v["bin_sha256"], "bytes": v["bin_bytes"],
                    "view_rect_min": v["view_rect_min"],
                    "view_size_and_inv_size": v["view_size_and_inv_size"],
                    "state_frame_index_mod8": v["state_frame_index_mod8"],
                    "max_pass_id": v["max_pass_id"],
                    "pass_references": v["pass_references"],
                }
                for v in views
            ],
        })
    scene_transform = None
    for f in figures:
        if f["slug"] == "scene-color-before-after":
            scene_transform = f["display"]["transform"]
    manifest = {
        "schema": SCHEMA,
        "capture": {
            "dump": str(ctx.dump),
            "report": portable_path(ctx.report),
            "run": portable_path(ctx.run_path),
            "run_status": ctx.run.get("status"),
            "dump_status": ctx.report_summary.get("status"),
            "captured_utc": ctx.report_summary.get("captured_utc"),
            "engine_version": ctx.run.get("engine_version"),
            "engine_git_sha": ctx.run.get("engine_git_sha"),
            "engine_git_branch": ctx.run.get("engine_git_branch"),
            "engine_build_id": ctx.run.get("engine_build_id"),
            "rhi": ctx.run.get("rhi"),
            "shader_platform": ctx.run.get("shader_platform"),
            "resolution": ctx.run.get("resolution"),
            "gpu": ctx.run.get("gpu"),
            "project_module_sha256": (ctx.run.get("project_module") or {}).get("sha256"),
            "engine_frame": engine_frame(ctx),
            "report_summary": ctx.report_summary,
        },
        "selection": {
            "selected_graph": ctx.selected_graph,
            "selected_view": {
                "content": ctx.selected_view["content"],
                "metadata": ctx.selected_view["metadata"],
                "bin_sha256": ctx.selected_view["bin_sha256"],
                "bin_bytes": ctx.selected_view["bin_bytes"],
                "view_rect_min": ctx.selected_view["view_rect_min"],
                "view_size_and_inv_size": ctx.selected_view["view_size_and_inv_size"],
                "state_frame_index_mod8": ctx.selected_view["state_frame_index_mod8"],
                "max_pass_id": ctx.selected_view["max_pass_id"],
            },
            "rule": (
                "The explicitly selected graph has exactly one camera View payload with ViewRectMin (0,0,0,0) and ViewSizeAndInvSize (1280,720)."
                if ctx.requested_graph else
                "The selected graph is the candidate with a unique 1280x720 camera View payload referenced by the highest-numbered pass ID."
            ),
            "candidates": [
                {"graph": p, "content": v["content"], "sha256": v["bin_sha256"],
                 "max_pass_id": v["max_pass_id"], "selected": p == ctx.selected_graph}
                for p, v in ctx.candidates
            ],
            "all_graphs_retained_in_root_explorer": True,
            "graphs": graphs,
        },
        "scene_color_transform": scene_transform,
        "format_policy": {
            "decoder_is_stored_format_driven": True,
            "rule": "Every source is decoded from Base/SubresourceLayouts.json StoredFormat plus the actual file bytes per texel, not from the descriptor's NativeFormat or an assumed half2/half4 layout.",
            "rgba16f_note": "PF_FloatRGBA is RGBA16F in this capture (8 bytes/texel), including BC7/BC5/BC6H exports whose stored format is PF_FloatRGBA. The decoder uses bytes_per_texel = ByteSize/(ExtentX*ExtentY*ZSliceCount); if a file is 16 bytes/texel it is decoded as float32 RGBA instead.",
            "packed_formats": "PF_FloatRGB == PF_FloatR11G11B10 (4 bytes/texel); decoded as 11/11/10. PF_A2B10G10R10 is decoded as native 10/10/10/2. R/G/B/A channel order is preserved per compound format.",
            "channel_provenance": "Every figure records source format, stored format, storage bytes per texel, decoder storage class, BitExact/ValuePreserving and StoredBytesAreNativeStorage.",
        },
        "decoded_figure_count": len(figures),
        "figures": figures,
        "unavailable": ctx.unavailable,
        "sources_verified": sorted(sources.values(), key=lambda s: s["file"]),
        "inventory": {
            "files": {
                "summary": "inventory/summary.json",
                "passes": "inventory/passes.json",
                "resources": "inventory/resources.json",
                "payloads": "inventory/payloads.json",
                "graph_index": "inventory/graph-index.json",
            },
            "passes": len(ctx.inventory_passes),
            "resources": len(ctx.inventory_resources),
            "payloads": len(ctx.inventory_payloads),
        },
        "limitations": [
            "All decoded figures are exact displays of stored GPU payloads, not re-renders. No GPU or engine work was executed to produce them.",
            "HDR display transforms are per-figure and declared in each figure's display.transform; they are not radiometric comparisons across figures. SceneColor before/after is the sole figure set with one shared transform.",
            "Source textures are natively 1280x888 with 168 padding rows; screen figures crop to the exact declared 1280x720 view rect. Non-screen atlases are shown at their own extent/crop and are not resized to 1280x720.",
            "VSM renderer debug screenshots from the baseline evidence run (dump binary-20261112-133826) are an earlier run and are deliberately not included or relabelled here.",
            "Absolute photometric calibration (candela/m2) is not available from the DumpGPU payload alone; radiance figures record engine render-target units.",
        ],
    }
    dump_json(FIG_DIR / "figure-manifest.json", manifest)
    return manifest


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def write_html(ctx: Context, manifest: dict):
    graph_index = load_json(INV_DIR / "graph-index.json")
    payload = json.dumps(graph_index, separators=(",", ":"))
    parts = []
    parts.append("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Unreal final capture figures</title>
<style>
:root{color-scheme:dark}body{background:#0b0b0d;color:#ddd;font:13px/1.45 Consolas,monospace;margin:0}
header{padding:18px 22px;border-bottom:1px solid #333;background:#111}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:26px 0 8px;border-bottom:1px solid #333;padding-bottom:4px}
main{padding:16px 22px 40px;max-width:1500px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,390px),1fr));gap:14px}
figure{margin:0;background:#151518;border:1px solid #333;padding:8px}
figure img{width:100%;height:auto;display:block;background:#000}
figcaption{margin-top:6px}figcaption b{color:#fff}
.small{color:#999;font-size:12px}code{color:#e8d9a0}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #2a2a2e;padding:3px 6px;text-align:left;vertical-align:top}
th{color:#fff;position:sticky;top:0;background:#111}
.badge{display:inline-block;padding:1px 6px;border:1px solid #555;margin-right:6px;color:#fff}
.warn{color:#ffb0b0}.ok{color:#9fe3a0}
details{margin:8px 0}summary{cursor:pointer;color:#fff}
#q{background:#111;color:#eee;border:1px solid #444;padding:5px 8px;width:320px}
pre{white-space:pre-wrap;overflow-wrap:anywhere}
</style>
</head><body>
<header><h1>Unreal final capture: decoded GPU figures</h1>
<div class="small">Selected graph <b>__SELECTED__</b> by exact 1280x720 FViewUniformShaderParameters bytes; all graphs remain browsable below.</div>
<p><a href="../../../frame-explorer.html">Open the full frame explorer</a></p>
</header><main>
""".replace("__SELECTED__", esc(ctx.selected_graph)))
    parts.append("<p class='small'>Dump: <code>%s</code><br>Run: <code>%s</code><br>"
                 "Selected View: <code>%s</code> sha256 <code>%s</code> | rect %s | size %s | max pass %s</p>" % (
                     esc(ctx.dump.name), esc(portable_path(ctx.run_path)), esc(ctx.selected_view["content"]),
                     esc(ctx.selected_view["bin_sha256"]), esc(ctx.selected_view["view_rect_min"]),
                     esc(ctx.selected_view["view_size_and_inv_size"]), esc(ctx.selected_view["max_pass_id"])))
    parts.append("<h2>Figures (%d)</h2><div class='grid'>" % len(manifest["figures"]))
    for f in manifest["figures"]:
        parts.append("<figure id='%s'><a href='%s'><img loading='lazy' src='%s' alt='%s' width='%s' height='%s'></a><figcaption><b>%s</b><br>"
                     "<span class='small'>%s</span><br>"
                     "<span class='small'>resource <code>%s</code> (%s) | pass %s %s | stored %s (%s B/texel)</span>"
                     "</figcaption></figure>" % (
                         esc(f["slug"]), esc(f["image"]["file"]), esc(f["image"]["file"]),
                         esc(f["title"]), esc(f["image"]["width"]), esc(f["image"]["height"]), esc(f["title"]),
                         esc(f["subtitle"]), esc(f["source"]["resource_id"]), esc(f["source"]["name"]),
                         esc(f["pass"]["id"]), esc(f["pass"]["event"]), esc(f["source"]["stored_format"]),
                         esc(f["source"]["storage_bytes_per_texel"])))
    parts.append("</div>")
    parts.append("<h2>Figure details</h2>")
    for f in manifest["figures"]:
        parts.append("<details><summary>%s</summary>" % esc(f["slug"]))
        parts.append("<p><b>Quantity:</b> %s — %s (%s; %s)</p>" % (
            esc(f["quantity"]["name"]), esc(f["quantity"]["description"]),
            esc(f["quantity"]["units"]), esc(f["quantity"]["domain"])))
        parts.append("<p><b>Channels:</b> %s</p>" % esc("; ".join(f["channel_mapping"])))
        parts.append("<p><b>Invalid:</b> %s — %s<br><span class='small'>source: %s</span></p>" % (
            esc(f["invalid"]["rule"]), esc(f["invalid"]["semantics"]), esc(f["invalid"].get("source"))))
        parts.append("<p><b>Display:</b> <code>%s</code></p>" % esc(json.dumps(f["display"], sort_keys=True)))
        parts.append("<p><b>Payload:</b> <code>%s</code> %s bytes sha256 <code>%s</code> version pass %s</p>" % (
            esc(f["payload"]["file"]), esc(f["payload"]["bytes"]), esc(f["payload"]["sha256"]),
            esc(f["payload_version"]["pass_id"])))
        parts.append("<p><b>Image:</b> <code>%s</code> %sx%s sha256 <code>%s</code></p>" % (
            esc(f["image"]["file"]), esc(f["image"]["width"]), esc(f["image"]["height"]), esc(f["image"]["sha256"])))
        for key, label in (("card_capture", "Card capture proof"),
                           ("card_capture_atlas", "Card atlas proof"),
                           ("page_occupancy", "Mapped-page occupancy proof"),
                           ("card_capture_union", "Card-rect union proof")):
            if key in f and f[key] is not None:
                parts.append("<p><b>%s:</b> <code>%s</code></p>" % (
                    label, esc(json.dumps(f[key], sort_keys=True))))
        parts.append("<details><summary>Full figure record</summary><pre>%s</pre></details>" %
                     esc(json.dumps(f, indent=2, ensure_ascii=False)))
        parts.append("</details>")
    parts.append("<h2>Root explorer: all scene graphs</h2>")
    parts.append("<p class='small'>Every graph and every resource/pass record from the final dump is listed here. "
                 "Only the selected graph has decoded figures; the other graphs remain addressable by exact resource id/pass id for numerical replay.</p>")
    parts.append("<p><input id='q' placeholder='filter resource name / id / pass event'></p>")
    parts.append("<div id='graphs'></div>")
    parts.append("""<script>
const GI = __GI_PAYLOAD__;
function h(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function render(q){
  q=(q||'').toLowerCase();
  const root=document.getElementById('graphs');
  let out='';
  for(const g of GI.graphs){
    out+='<details '+(g.selected?'open':'')+'><summary>graph '+h(g.prefix)+' '+(g.selected?'[SELECTED]':'')+
      ' — '+g.resource_count+' resources, '+g.pass_count+' passes</summary>';
    for(const v of g.view_payloads){
      if(v.is_1280x720_main || q==='')
        out+='<p class="small">View '+h(v.content)+' sha256 '+h(v.sha256)+' | rect '+h(v.view_rect_min)+' | size '+h(v.view_size_and_inv_size)+' | max pass '+h(v.max_pass_id)+'</p>';
    }
    out+='<table><tr><th>kind</th><th>id</th><th>name / event</th><th>format / pointer</th></tr>';
    for(const r of g.resources){
      const s=(r.id+' '+r.name+' '+r.format).toLowerCase();
      if(q && !s.includes(q)) continue;
      out+='<tr><td>resource</td><td><code>'+h(r.id)+'</code></td><td>'+h(r.name)+'</td><td>'+h(r.format)+' ('+h(r.extent)+', '+r.files+' files)</td></tr>';
    }
    for(const p of g.passes){
      const s=(p.id+' '+p.event+' '+p.pointer).toLowerCase();
      if(q && !s.includes(q)) continue;
      out+='<tr><td>pass</td><td><code>'+h(p.id)+'</code></td><td>'+h(p.event)+'</td><td><code>'+h(p.pointer)+'</code></td></tr>';
    }
    out+='</table></details>';
  }
  root.innerHTML=out;
}
document.getElementById('q').addEventListener('input',e=>render(e.target.value));
render('');
</script>""".replace("__GI_PAYLOAD__", payload))
    parts.append("</main></body></html>")
    (FIG_DIR / "index.html").write_text("\n".join(parts), encoding="utf-8")


def main():
    global FIG_DIR, PNG_DIR, INV_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", "--dump", dest="dump", default=str(DEFAULT_DUMP))
    ap.add_argument("--report", default=str(DEFAULT_REPORT))
    ap.add_argument("--run", default=str(DEFAULT_RUN))
    ap.add_argument("--graph", help="explicit camera render-graph prefix")
    ap.add_argument("--output", type=Path, default=FIG_DIR)
    args = ap.parse_args()
    def _resolve(p, base):
        p = Path(p)
        return p if p.is_absolute() else (base / p).resolve()
    dump = _resolve(args.dump, Path.cwd())
    report = _resolve(args.report, FIG_DIR)
    run = _resolve(args.run, FIG_DIR)
    FIG_DIR = args.output
    PNG_DIR = FIG_DIR / "png"
    INV_DIR = FIG_DIR / "inventory"
    PNG_DIR.mkdir(parents=True, exist_ok=True)
    INV_DIR.mkdir(parents=True, exist_ok=True)
    ctx = Context(dump, report, run, args.graph)
    print(f"dump={dump}")
    print(f"selected graph={ctx.selected_graph} view={ctx.selected_view['content']} "
          f"sha256={ctx.selected_view['bin_sha256'][:16]}...")
    fig_gbuffer(ctx)
    fig_depth(ctx)
    fig_scene_color(ctx)
    fig_surface_cache(ctx)
    fig_world_cache(ctx)
    fig_reflections(ctx)
    fig_screen_probes(ctx)
    fig_vsm(ctx)
    ctx.unavailable.extend([
        {"item": "GBufferE / GBufferVelocity",
         "reason": "ResourcesNotPresentInFinalGraph",
         "evidence": f"Graph {ctx.selected_graph} exposes GBufferA-D. The capture has no GBufferE or GBufferVelocity descriptor for this graph."},
        {"item": "Lumen.Cards / Lumen.MeshCards card table",
         "reason": "StructuredBufferSemanticsNotDecoded",
         "evidence": "The figures show the card-capture atlases. Lumen.Cards and Lumen.MeshCards structured payloads are available through the capture and the source schema."},
        {"item": "VSM renderer debug screenshots",
         "reason": "EarlierRunOnly",
         "evidence": "The baseline VSM mask/vpage/cache PNGs and lit baseline come from dump binary-20261112-133826, an earlier run. They are not included or relabelled as final-run figures. This final-run figure set uses Shadow.Virtual.MaskBits and Shadow.Virtual.PageTable payloads."},
        {"item": "Absolute photometric calibration of SceneColor / Lumen radiance",
         "reason": "NoRadiometricReferenceInDumpGPU",
         "evidence": "DumpGPU stores engine render-target radiance values; no calibrated luminance meter, exposure EXR or reference transport image is part of this dump. Figures record engine units and a declared display transform, not candela/m2."},
        {"item": "VSM PageFlags / hierarchical page flag mips",
         "reason": "NotDecodedInThisStep",
         "evidence": "Shadow.Virtual.PageFlags is present (R32_UINT, 7 mips), but the final figure shows the packed PageTable state and all 16 mask nibbles. PageFlags bit semantics are not re-derived; raw payload remains in the dump."},
    ])
    for t in ctx.textures:
        t.close()
    manifest = build_manifest(ctx)
    write_inventory(ctx)
    write_html(ctx, manifest)
    print(f"wrote {len(manifest['figures'])} figures, figure-manifest.json, index.html, "
          f"{len(ctx.inventory_passes)} passes, {len(ctx.inventory_resources)} resources, "
          f"{len(ctx.inventory_payloads)} payloads")
    print(f"verified source files: {len(set(s['file'] for s in ctx.source_checks))}")


if __name__ == "__main__":
    main()





