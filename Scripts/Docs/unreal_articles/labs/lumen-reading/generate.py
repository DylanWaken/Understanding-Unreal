#!/usr/bin/env python3
"""Generate the Lumen reading diagrams and the clean CPU-reference presentation.

The SVGs in this directory are explanatory schematics, not captured Unreal
geometry, not stochastic GPU sample dumps, and not screenshots. Every diagram
uses native SVG text and a <title>/<desc> pair. The one raster payload is the
educational CPU path-traced reference, reconstructed from the checked-in linear
PFM with the original path_tracer.py tone map at exposure 0.6. Its burned-in
footer is deliberately not reproduced; the SVG supplies a crisp native caption.

Run:
    python Scripts/Docs/unreal_articles/labs/lumen-reading/generate.py
    python Scripts/Docs/unreal_articles/labs/lumen-reading/generate.py --check

No third-party packages, no GPU, no browser.
"""
from __future__ import annotations

from html import escape
from pathlib import Path
import argparse
import base64
import hashlib
import importlib.util
import json
import math
import struct
import zlib
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[5]
OUT = ROOT / 'Docs' / 'Unreal' / 'assets' / 'lumen-reading'
DIAGRAMS = OUT / 'diagrams'
PATH_TRACER = ROOT / 'Scripts' / 'Docs' / 'unreal_articles' / 'labs' / 'lumen-pathtracer' / 'reference' / 'path_tracer.py'
PFM = ROOT / 'Docs' / 'Unreal' / 'assets' / 'lumen-v2' / 'reference_linear.pfm'
ORIGINAL_PNG = ROOT / 'Docs' / 'Unreal' / 'assets' / 'lumen-v2' / 'reference_render.png'
CLEAN_PNG = OUT / 'cpu-reference-render.png'
CPU_SVG = OUT / 'cpu-reference.svg'
EXPOSURE = 0.6
SEED = 20240601
SPP = 64
MAX_DEPTH = 8

PAPER, INK, MUTED, RULE = '#fffdf8', '#22312d', '#586760', '#c4cec7'
PALE, LIGHT, WASH = '#f8f8f2', '#edf3ee', '#f0f1e9'
GREEN, BLUE, GOLD, RED = '#286c56', '#286788', '#93661e', '#aa5343'
PALETTE = {GREEN: 'green', BLUE: 'blue', GOLD: 'gold', RED: 'red', MUTED: 'muted', INK: 'ink'}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='\n') as stream:
        stream.write(text)


class SVG:
    def __init__(self, title: str, desc: str, width: int = 1100, height: int = 760,
                 badge: bool = True, header: bool = True, subtitle: str | None = None,
                 schematic: bool = True):
        self.width, self.height = width, height
        schematic_attr = 'true' if schematic else 'false'
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" role="img" aria-labelledby="title desc" data-schematic="{schematic_attr}">',
            f'<title id="title">{escape(title)}</title>',
            f'<desc id="desc">{escape(desc)}</desc>',
            '<defs>',
        ]
        for color, name in PALETTE.items():
            self.parts.append(
                f'<marker id="arrow-{name}" viewBox="0 0 10 10" refX="9" refY="5" '
                f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
                f'<path d="M0 0 L10 5 L0 10 Z" fill="{color}"/></marker>'
            )
        self.parts.extend([
            '</defs>',
            f'<rect width="{width}" height="{height}" fill="{PAPER}"/>',
            '<g font-family="Segoe UI, Arial, sans-serif">',
        ])
        if badge:
            self.badge(36, 2)
        if header:
            self.text(36, 54, title, 28, INK, True)
            if subtitle:
                self.text(36, 82, subtitle, 15, MUTED)
        self._next_y = 104

    # -- primitives ---------------------------------------------------------
    def text(self, x, y, value, size=19, color=INK, bold=False, anchor='start', mono=False, italic=False):
        weight = '600' if bold else '400'
        family = 'Cascadia Code, Consolas, monospace' if mono else 'Segoe UI, Arial, sans-serif'
        style = ' font-style="italic"' if italic else ''
        for i, line in enumerate(str(value).split('\n')):
            self.parts.append(
                f'<text x="{x}" y="{y + i * (size + 6)}" font-family="{family}" '
                f'font-size="{size}" fill="{color}" font-weight="{weight}"{style} '
                f'text-anchor="{anchor}">{escape(line)}</text>'
            )

    def badge(self, x, y):
        self.rect(x, y, 252, 22, fill=WASH, stroke=GOLD, rx=4)
        self.text(x + 10, y + 16, 'SCHEMATIC · NOT CAPTURED GEOMETRY', 11, GOLD, True)

    def footer(self, note='Schematic diagram. Not captured geometry and not a dump of GPU sample directions.'):
        self.line(36, self.height - 44, self.width - 36, self.height - 44, RULE, 1)
        self.text(36, self.height - 20, note, 13, MUTED)
        self.parts.append('</g></svg>')

    def render(self) -> str:
        return '\n'.join(self.parts) + '\n'

    def rect(self, x, y, w, h, fill=PALE, stroke=RULE, rx=8, width=1.5, dash=False, opacity=1):
        dash_attr = ' stroke-dasharray="8 6"' if dash else ''
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="{width}" opacity="{opacity}"{dash_attr}/>'
        )

    def panel(self, x, y, w, h, label, color=GREEN, note=None, fill=PALE):
        self.rect(x, y, w, h, fill=fill)
        self.parts.append(f'<path d="M{x + 10} {y + 1}H{x + w - 10}" stroke="{color}" stroke-width="4"/>')
        self.text(x + 16, y + 30, label, 17, color, True)
        if note:
            self.text(x + 16, y + 51, note, 14, MUTED)

    def line(self, x1, y1, x2, y2, color=MUTED, width=2, dash=False, arrow=False):
        name = PALETTE.get(color, 'muted')
        dash_attr = ' stroke-dasharray="8 6"' if dash else ''
        marker = f' marker-end="url(#arrow-{name})"' if arrow else ''
        self.parts.append(
            f'<path d="M{x1} {y1}L{x2} {y2}" fill="none" stroke="{color}" '
            f'stroke-width="{width}"{dash_attr}{marker}/>'
        )

    def polyline(self, points, color=MUTED, width=2, dash=False, arrow=False, fill='none'):
        name = PALETTE.get(color, 'muted')
        dash_attr = ' stroke-dasharray="8 6"' if dash else ''
        marker = f' marker-end="url(#arrow-{name})"' if arrow else ''
        path = 'M' + 'L'.join(f'{x} {y}' for x, y in points)
        self.parts.append(
            f'<path d="{path}" fill="{fill}" stroke="{color}" stroke-width="{width}"{dash_attr}{marker}/>'
        )

    def poly(self, points, fill=LIGHT, stroke=RULE, width=1.5, dash=False, opacity=1):
        dash_attr = ' stroke-dasharray="8 6"' if dash else ''
        path = 'M' + 'L'.join(f'{x} {y}' for x, y in points) + 'Z'
        self.parts.append(
            f'<path d="{path}" fill="{fill}" stroke="{stroke}" stroke-width="{width}" '
            f'opacity="{opacity}"{dash_attr}/>'
        )

    def circle(self, x, y, r=5, color=GREEN, fill=None, width=1.5):
        self.parts.append(
            f'<circle cx="{x}" cy="{y}" r="{r}" fill="{fill or color}" '
            f'stroke="{color}" stroke-width="{width}"/>'
        )

    def ellipse(self, x, y, rx, ry, color=RULE, fill='none', width=1.5, dash=False):
        dash_attr = ' stroke-dasharray="8 6"' if dash else ''
        self.parts.append(
            f'<ellipse cx="{x}" cy="{y}" rx="{rx}" ry="{ry}" fill="{fill}" '
            f'stroke="{color}" stroke-width="{width}"{dash_attr}/>'
        )

    def arc(self, cx, cy, r, a0, a1, color=GOLD, width=2):
        # Angles in degrees, screen coordinates (0 = right, 90 = down).
        x0, y0 = cx + r * math.cos(math.radians(a0)), cy + r * math.sin(math.radians(a0))
        x1, y1 = cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1))
        large = 1 if abs(a1 - a0) > 180 else 0
        sweep = 1 if a1 > a0 else 0
        self.parts.append(
            f'<path d="M{x0:.2f} {y0:.2f}A{r} {r} 0 {large} {sweep} {x1:.2f} {y1:.2f}" '
            f'fill="none" stroke="{color}" stroke-width="{width}"/>'
        )

    def chip(self, x, y, label, color=GREEN, width=None, size=14, fill=None):
        w = width or int(len(label) * size * 0.58 + 18)
        self.rect(x, y, w, size + 12, fill=fill or PAPER, stroke=color, rx=4, width=1.2)
        self.text(x + 9, y + size + 2, label, size, color, True)

    def formula(self, x, y, value, w, h, color=INK, fill=WASH, size=15, mono=True):
        self.rect(x, y, w, h, fill=fill, stroke=RULE, rx=5, width=1)
        self.text(x + 12, y + 24, value, size, color, mono=mono)

    # -- 3D helpers (oblique projection: x right, y down, z up) -------------
    @staticmethod
    def ob(ox, oy, s, x, y, z):
        return ox + x * s + z * 0.48 * s, oy + y * s - z * 0.58 * s

    def box3(self, ox, oy, s, x0, y0, z0, dx, dy, dz, color=GREEN, fill=LIGHT, opacity=0.42, dash=False):
        p = lambda x, y, z: self.ob(ox, oy, s, x, y, z)
        c000, c100 = p(x0, y0, z0), p(x0 + dx, y0, z0)
        c010, c110 = p(x0, y0 + dy, z0), p(x0 + dx, y0 + dy, z0)
        c001, c101 = p(x0, y0, z0 + dz), p(x0 + dx, y0, z0 + dz)
        c011, c111 = p(x0, y0 + dy, z0 + dz), p(x0 + dx, y0 + dy, z0 + dz)
        # top, left, front faces
        self.poly([c001, c101, c111, c011], fill=fill, stroke=color, width=1.2, opacity=opacity, dash=dash)
        self.poly([c000, c001, c011, c010], fill=fill, stroke=color, width=1.2, opacity=opacity, dash=dash)
        self.poly([c010, c011, c111, c110], fill=fill, stroke=color, width=1.2, opacity=opacity, dash=dash)
        for a, b in [(c000, c100), (c100, c110), (c110, c010), (c010, c000),
                     (c001, c101), (c101, c111), (c111, c011), (c011, c001),
                     (c000, c001), (c100, c101), (c010, c011), (c110, c111)]:
            self.line(a[0], a[1], b[0], b[1], color, 1.4, dash=dash)
        return {k: v for k, v in dict(c000=c000, c100=c100, c010=c010, c110=c110,
                                      c001=c001, c101=c101, c011=c011, c111=c111).items()}


def surface_patch(s, x, y, w, h, label, color=GREEN):
    s.panel(x, y, w, h, label, color)


# ---------------------------------------------------------------------------
# 1. Area -> solid angle and cosines
# ---------------------------------------------------------------------------
def solid_angle() -> SVG:
    s = SVG(
        'Projected area becomes solid angle; a sample is a cone',
        'A 2D side view maps a small patch at y to a projected solid angle at x. A 3D view shows the same patch as a cone on the sphere. Area measure, solid-angle measure, and the two cosine factors stay distinct. Schematic; one patch and one direction, not captured geometry.',
        1100, 760, subtitle='One patch, two measures: area dA and projected solid angle dσ. The cosines belong to different endpoints.',
    )
    surface_patch(s, 36, 104, 500, 404, '2D side view · one differential patch')
    surface_patch(s, 560, 104, 504, 404, '3D view · the same patch is a cone on the sphere', BLUE)

    # Left: surface, target patch, ray, cosines.
    x_recv, y_recv = 280, 448
    y_tgt = (392, 286)
    s.line(74, y_recv, 500, y_recv, INK, 3)
    s.line(x_recv, y_recv, x_recv, y_recv - 126, INK, 2, arrow=True)
    s.text(x_recv + 8, y_recv - 132, 'n_x', 16, INK, True)
    # patch, tilted so its normal is not parallel to the incoming ray
    s.line(352, 274, 436, 312, GOLD, 7)
    s.line(392, 286, 344, 236, GOLD, 2, arrow=True)
    s.text(330, 228, 'n_y', 16, GOLD, True)
    s.circle(*y_tgt, 5, GOLD)
    s.text(y_tgt[0] + 10, y_tgt[1] + 24, 'y', 18, GOLD, True)
    s.line(x_recv, y_recv, *y_tgt, GREEN, 3, arrow=True)
    s.text(314, 362, 'r', 17, GREEN, True)
    s.line(300, 366, 372, 344, GREEN, 1, dash=True)
    # cone outline and projected area
    s.poly([(x_recv, y_recv), (352, 274), (436, 312)], fill=GOLD, stroke=GOLD, width=1, opacity=0.12)
    s.line(356, 276, 428, 306, GOLD, 2, dash=True)
    # angle at x
    s.arc(x_recv, y_recv, 38, -90, -53, GOLD, 2)
    s.text(x_recv + 18, y_recv - 48, 'θ_x', 16, GOLD, True)
    # angle at y between the target normal and the direction back to x
    s.arc(y_tgt[0], y_tgt[1], 34, 126, 151, GOLD, 2)
    s.text(y_tgt[0] - 24, y_tgt[1] - 10, 'θ_y', 16, GOLD, True)
    s.text(70, 494, 'dA(y) is the target area; the cone it subtends is dσ at x.', 15, MUTED)
    s.formula(70, 150, 'dσ = |n_y · (−ω)| dA / r²', 270, 34, size=14)
    s.formula(70, 186, 'dσ_x⊥ = |n_x · ω| dσ', 270, 34, size=14)
    # projected area annotation
    s.line(392, 286, 448, 328, MUTED, 1.5, dash=True)
    s.text(404, 342, '|n_y · (−ω)| dA', 14, MUTED)

    # Right: sphere and cone.
    cx, cy = 812, 310
    s.ellipse(cx, cy, 118, 82, RULE, PAPER, 2)
    s.parts.append(f'<path d="M694 310A118 82 0 0 1 930 310" fill="{WASH}" stroke="{RULE}" stroke-width="1.5"/>')
    s.ellipse(cx, cy, 118, 34, RULE, 'none', 1.2, True)
    s.ellipse(cx, cy, 60, 82, RULE, 'none', 1.2, True)
    s.line(cx, cy, cx, cy - 106, INK, 2, arrow=True)
    s.text(cx + 8, cy - 112, 'n_x', 15, INK, True)
    s.circle(cx, cy, 4, INK)
    s.text(cx - 10, cy + 22, 'x', 17, INK, True)
    # patch and cone on the sphere
    p1, p2, p3 = (886, 242), (925, 262), (900, 286)
    s.poly([(cx, cy), p1, p2, p3], fill=GOLD, stroke=GOLD, width=1, opacity=0.12)
    for pt in (p1, p2, p3):
        s.line(cx, cy, *pt, GOLD, 1.6, dash=True)
    s.poly([p1, p2, p3], fill=GOLD, stroke=GOLD, width=2, opacity=0.25)
    s.text(915, 232, 'solid-angle cell dσ', 14, GOLD, True)
    # nearer same-area patch with a wider cone
    q1, q2, q3 = (760, 250), (800, 270), (778, 294)
    s.poly([q1, q2, q3], fill=BLUE, stroke=BLUE, width=1.5, opacity=0.16)
    s.text(706, 232, 'same dA, larger r → smaller dσ', 14, BLUE)
    s.line(cx, cy, q1[0], q1[1], BLUE, 1.5, dash=True)
    s.line(cx, cy, q3[0], q3[1], BLUE, 1.5, dash=True)
    # hemisphere labels
    s.text(640, 420, 'The cone is a set of directions, not a surface point.', 15, MUTED)
    s.formula(640, 441, 'projected solid angle weights by |n_x · ω|', 390, 34, size=14)

    # Bottom: three distinct measures.
    boxes = [
        (36, 'Area measure', ['dA(y) at the target', 'used by the area-product path density', 'contains no cosine by itself']),
        (390, 'Solid-angle measure', ['dσ around ω at x', 'sampler density p_σ(ω)', 'the estimator divides by p_σ']),
        (744, 'Why 1/r² cancels', ['p_A(y|x) = p_σ(ω)|n_y·(−ω)|/r²', 'G contains the same 1/r²', 'f/p leaves one receiving cosine']),
    ]
    for x, title, lines in boxes:
        s.rect(x, 532, 320, 152, fill=PALE)
        s.text(x + 16, 560, title, 18, GREEN, True)
        for i, line in enumerate(lines):
            s.text(x + 16, 590 + i * 24, line, 14, INK)
    s.footer('Schematic. One patch, one direction. The matched Lumen/path-traced pair is a convergence comparison, not a sample dump.')
    return s


# ---------------------------------------------------------------------------
# 2. Light sampling vs BSDF sampling, and MIS
# ---------------------------------------------------------------------------
def sampling_mis() -> SVG:
    s = SVG(
        'Light sampling and BSDF sampling propose the same path family',
        'A receiver x and one emitter y are reached by a light-sampling connection and by a BSDF continuation. A small 3D hemisphere inset shows the same two directions, and the directional densities overlap; MIS partitions the contribution with weights that sum to one. Schematic; no captured geometry or GPU samples.',
        1100, 760, subtitle='Two proposals for the same direct path: light sampling and BSDF sampling. MIS only partitions where both can generate it.',
    )
    surface_patch(s, 36, 104, 520, 404, '2D plan view · two techniques, one receiver')
    # emitter
    s.rect(92, 208, 54, 190, fill=WASH, stroke=GOLD, rx=2, width=2)
    s.text(84, 198, 'emitter area', 14, GOLD, True)

    # camera
    s.circle(500, 304, 9, INK)
    s.poly([(500, 304), (522, 292), (522, 316)], fill=INK, stroke=INK)
    s.text(528, 310, 'camera', 14, INK, True)
    # receiver
    s.circle(310, 304, 6, GREEN)
    s.text(318, 330, 'x', 18, GREEN, True)
    s.line(310, 304, 500, 304, INK, 2, arrow=True)
    s.text(390, 292, 'fixed ω_o', 14, MUTED)
    # light sample to upper emitter point
    s.line(310, 304, 118, 250, GREEN, 3, arrow=True)
    s.circle(118, 250, 4, GREEN)
    s.text(176, 242, 'light sample · p_L', 14, GREEN, True)
    # BSDF sample to lower emitter point
    s.line(310, 304, 118, 350, BLUE, 3, arrow=True)
    s.circle(118, 350, 4, BLUE)
    s.text(176, 366, 'BSDF sample · p_B', 14, BLUE, True)
    s.text(70, 486, 'Both arrows estimate the same direct contribution F at x;', 14, MUTED)
    s.text(70, 505, 'the two proposals are not two different physical paths.', 14, MUTED)
    s.formula(70, 418, 'C = β f_s(x;ω→ω_o) L_e(y→x) V |n_x·ω| / p', 436, 34, size=13)
    # 3D inset: the same two directions on the hemisphere at x.
    cx, cy = 482, 194
    s.ellipse(cx, cy, 42, 27, RULE, PAPER, 1.5)
    s.parts.append(f'<path d="M440 194A42 27 0 0 1 524 194" fill="{WASH}" stroke="{RULE}" stroke-width="1.2"/>')
    s.ellipse(cx, cy, 20, 27, RULE, 'none', 1, True)
    s.circle(cx, cy, 4, GREEN)
    s.line(cx, cy, 452, 164, GREEN, 2, arrow=True)
    s.line(cx, cy, 458, 226, BLUE, 2, arrow=True)
    s.arc(cx, cy, 30, 198, 252, GOLD, 2)
    s.text(430, 150, '3D at x', 12, MUTED, True)
    s.text(528, 176, 'ω_L', 11, GREEN, True)
    s.text(528, 218, 'ω_B', 11, BLUE, True)
    s.text(390, 248, 'same hemisphere, two proposals', 10, MUTED)

    # Density panel
    surface_patch(s, 580, 104, 484, 244, 'Directional densities over the same ω', BLUE)
    base_y = 306
    s.line(620, base_y, 1040, base_y, INK, 1.5, arrow=True)
    s.text(1024, base_y + 22, 'ω', 15, INK, True)
    # p_L hump
    s.parts.append(
        f'<path d="M640 {base_y}C700 {base_y - 120} 760 {base_y - 120} 812 {base_y}'
        f'L824 {base_y}C878 {base_y - 70} 932 {base_y - 70} 984 {base_y}" '
        f'fill="none" stroke="{GREEN}" stroke-width="3"/>'
    )
    s.parts.append(
        f'<path d="M640 {base_y}C700 {base_y - 120} 760 {base_y - 120} 812 {base_y}'
        f'L824 {base_y}C878 {base_y - 70} 932 {base_y - 70} 984 {base_y}" '
        f'fill="{GOLD}" opacity="0.10"/>'
    )
    s.parts.append(
        f'<path d="M752 {base_y}C790 {base_y - 92} 846 {base_y - 92} 886 {base_y}" '
        f'fill="none" stroke="{BLUE}" stroke-width="3" stroke-dasharray="7 5"/>'
    )
    s.text(648, base_y - 92, 'p_L', 17, GREEN, True)
    s.text(858, base_y - 66, 'p_B', 17, BLUE, True)
    s.text(792, base_y - 14, 'overlap', 13, GOLD, True)
    s.formula(620, 152, 'w_L = p_L²/(p_L²+p_B²)', 200, 30, size=13)
    s.formula(826, 152, 'w_B = p_B²/(p_L²+p_B²)', 200, 30, size=13)


    # Estimator panel
    s.panel(580, 366, 484, 142, 'The weights partition one integral', GOLD)
    s.chip(604, 414, 'light sample · w_L', GREEN, size=13)
    s.chip(604, 456, 'BSDF sample · w_B', BLUE, size=13)
    s.line(790, 432, 852, 432, GREEN, 2, arrow=True)
    s.line(790, 474, 852, 474, BLUE, 2, arrow=True)
    s.formula(858, 414, '∫ F dσ', 170, 34, size=16)
    s.text(858, 472, 'w_L + w_B = 1', 15, GOLD, True)
    s.text(604, 500, 'zero support ⇒ zero weight; a delta branch carries mass, not a density.', 12, MUTED)

    # Bottom notes
    rows = [
        (36, 'Common measure', ['p_L and p_B must both be solid-angle densities', 'at the same receiver and for the same emitter']),
        (390, 'Support', ['more samples cannot repair a technique with zero support', 'MIS only combines what each sampler can generate']),
        (744, 'Delta events', ['a mirror direction is not a finite density', 'use its branch mass, not a huge p_B value']),
    ]
    for x, title, lines in rows:
        s.rect(x, 532, 320, 152, fill=PALE)
        s.text(x + 16, 560, title, 18, GREEN, True)
        for i, line in enumerate(lines):
            s.text(x + 16, 590 + i * 24, line, 13, INK)
    s.footer('Schematic. The real convergence comparison is the matched Lumen path-traced pair; these arrows are not captured sample directions.')
    return s


# ---------------------------------------------------------------------------
# 3. Surface cache: world point -> card UV -> page table -> atlas
# ---------------------------------------------------------------------------
def surface_cache_lookup() -> SVG:
    s = SVG(
        'A hit becomes card UV, a page-table entry, and an atlas texel',
        'A world hit and normal select directional cards; card-local coordinates map to UV, the virtual page table resolves a mapped page and bias, and four atlas taps are weighted by bilinear and depth-validity terms. Schematic; the captured replay resolves one real texel.',
        1100, 780, subtitle='World point → card space → virtual page → physical atlas → four weighted taps. One real texel is resolved in the replay.',
    )
    surface_patch(s, 36, 104, 560, 448, '3D · a geometric hit selects a directional card', GREEN)

    # Object box and hit.
    box = s.box3(102, 330, 96, 0, 0, 0, 1.6, 1.4, 1.25, INK, LIGHT, 0.35)
    hit = s.ob(102, 330, 96, 1.1, 1.4, 0.72)
    s.circle(hit[0], hit[1], 6, RED)
    s.text(hit[0] + 10, hit[1] - 10, 'x', 18, RED, True)
    s.line(hit[0], hit[1], hit[0] + 44, hit[1] - 56, RED, 2, arrow=True)
    s.text(hit[0] + 48, hit[1] - 58, 'n', 16, RED, True)
    s.text(80, 510, 'object · six directional card sets', 14, MUTED)

    # One card drawn as a parallelogram.
    card = [(385, 238), (486, 208), (514, 296), (413, 326)]
    s.poly(card, fill=WASH, stroke=GOLD, width=3)
    s.text(392, 220, 'candidate card', 14, GOLD, True)
    s.line(hit[0] + 6, hit[1] - 6, 402, 272, GOLD, 2, dash=True, arrow=True)
    s.text(404, 206, 'bounds + facing test', 13, GOLD)
    # local axes
    s.line(424, 312, 472, 298, GOLD, 2, arrow=True)
    s.line(424, 312, 438, 264, GOLD, 2, arrow=True)
    s.text(476, 300, 'u', 15, GOLD, True)
    s.text(440, 254, 'v', 15, GOLD, True)
    s.circle(424, 312, 4, GOLD)
    s.text(388, 342, 'p = card-local hit', 13, MUTED)
    s.formula(80, 366, 'u = ½ + ½ p_x/a_x', 264, 30, size=13)
    s.formula(80, 402, 'v = ½ − ½ p_y/a_y', 264, 30, size=13)

    # Virtual pages + atlas.
    surface_patch(s, 620, 104, 444, 264, '2D · virtual page table resolves a mapped page', GOLD)
    # card UV square
    sx, sy, sw = 646, 150, 128
    s.rect(sx, sy, sw, sw, fill=PAPER, stroke=INK, rx=2)
    for i in range(1, 4):
        s.line(sx + i * sw / 4, sy, sx + i * sw / 4, sy + sw, RULE, 1)
        s.line(sx, sy + i * sw / 4, sx + sw, sy + i * sw / 4, RULE, 1)
    s.rect(sx + 3 * sw / 4, sy, sw / 4, sw / 4, fill=GOLD, stroke=GOLD, rx=1, opacity=0.25)
    s.text(sx, sy + sw + 18, 'requested page', 13, MUTED)
    # page table entry
    s.formula(820, 150, 'page entry (8 bytes)', 220, 30, size=13)
    s.formula(820, 188, 'bias X/Y · res X/Y · page', 220, 30, size=12)
    s.line(sx + sw + 4, sy + 46, 812, 166, MUTED, 2, arrow=True)
    # physical atlas grid
    ax, ay, aw = 820, 244, 96
    s.rect(ax, ay, aw, aw, fill=PAPER, stroke=BLUE, rx=2)
    for i in range(1, 6):
        s.line(ax + i * aw / 6, ay, ax + i * aw / 6, ay + aw, RULE, 1)
        s.line(ax, ay + i * aw / 6, ax + aw, ay + i * aw / 6, RULE, 1)
    s.rect(ax + 1, ay + 1, aw / 6 - 2, aw / 6 - 2, fill=BLUE, stroke=BLUE, rx=1, opacity=0.35)
    s.text(ax, ay - 8, 'physical atlas (one page shown)', 13, BLUE, True)
    s.line(930, 212, 904, 244, BLUE, 2, arrow=True)
    s.text(178, 246, 'mapped fallback', 12, MUTED)
    s.text(646, 330, 'A zero mapped X resolution is invalid, not black lighting.', 12, MUTED)

    # Taps + formula.
    surface_patch(s, 620, 386, 444, 166, '2D · four taps, depth validity, one value', BLUE)
    tx, ty = 650, 436
    weights = [('1.0', GREEN), ('0.0', MUTED), ('0.0', MUTED), ('0.0', MUTED)]
    for i, (w, color) in enumerate(weights):
        x = tx + (i % 2) * 54
        y = ty + (i // 2) * 54
        s.rect(x, y, 46, 46, fill=(WASH if color == GREEN else PAPER), stroke=color, rx=2, width=1.5)
        s.text(x + 23, y + 28, w, 15, color, True, anchor='middle', mono=True)
    s.text(tx + 128, ty + 20, 'β_i · depth validity v_i', 13, INK, True)
    s.text(tx + 128, ty + 42, 'invalid texels contribute 0 to w_i,', 12, MUTED)
    s.text(tx + 128, ty + 60, 'not a valid black radiance.', 12, MUTED)
    s.formula(128 + tx, ty + 76, 'L = Σ w_i L_i / Σ w_i / e_c', 238, 30, size=12)

    # Chain across bottom.
    s.rect(36, 572, 1028, 144, fill=WASH, stroke=RULE, rx=8)
    s.text(54, 600, 'The lookup chain', 18, GREEN, True)
    steps = ['world hit x, n', 'mesh-card record', 'card-local p', '(u,v)', 'page table', 'physical UV', '4 taps + depth', 'L / e_c']
    x = 54
    for i, step in enumerate(steps):
        w = max(72, int(len(step) * 7.2 + 18))
        s.rect(x, 620, w, 34, fill=PAPER, stroke=GREEN if i in (0, 7) else RULE, rx=4, width=1.2)
        s.text(x + w / 2, 643, step, 11, INK, anchor='middle')
        if i < len(steps) - 1:
            s.line(x + w + 2, 637, x + w + 12, 637, MUTED, 1.5, arrow=True)
        x += w + 14
    s.text(54, 688, 'Schematic simplifies to one card and four taps. The replay resolves card page 35 at 8×8 texels.', 12, MUTED)
    s.text(54, 706, 'Atlas coordinate (1067, 4); bilinear weights (1, 0, 0, 0); unrelated pages are omitted.', 12, MUTED)
    s.footer('Schematic. Real atlas is 4096² and sparse; the diagram draws one page and omits unrelated pages. Not captured geometry.')
    return s


# ---------------------------------------------------------------------------
# 4. Screen probes: angular map and compaction
# ---------------------------------------------------------------------------
def screen_probe_angular() -> SVG:
    s = SVG(
        'Screen probes: a square direction map and a compacted ray list',
        'A screen-space probe grid is reconstructed at pixels; each probe traces an equal-area square direction map and fallback stages run on a compacted list of remaining misses. Schematic; the real probe, PDF and trace buffers are captured separately.',
        1100, 780, subtitle='The trace atlas keeps every probe; fallback reads only the compacted misses. A cell is a cone, not a blur.',
    )
    surface_patch(s, 36, 104, 330, 404, '2D · screen grid and nearest probes')
    # screen rectangle and probe grid
    s.rect(62, 154, 278, 176, fill=WASH, stroke=INK, rx=2)
    s.rect(218, 154, 122, 176, fill=LIGHT, stroke=RULE, rx=0, dash=True)
    for gx in range(62, 341, 28):
        for gy in range(154, 331, 28):
            s.circle(gx, gy, 3, GREEN)
    # highlighted pixel + four nearest probes
    px, py = 246, 266
    s.rect(px - 7, py - 7, 14, 14, fill=RED, stroke=RED, rx=1, opacity=0.35)
    s.circle(px, py, 3, RED)
    for nx, ny in [(240, 250), (268, 250), (240, 278), (268, 278)]:
        s.circle(nx, ny, 5, GREEN, fill=PAPER, width=2)
        s.line(nx, ny, px, py, GREEN, 1, dash=True)
    s.line(px, py, px + 20, py - 16, GOLD, 2, arrow=True)
    s.text(px + 24, py - 16, 'tile jitter', 12, GOLD)
    s.text(62, 352, 'Wp = ceil(W / D), Hp = ceil(H / D)', 12, MUTED)
    s.text(62, 374, 'Four probes reconstruct a pixel.', 12, MUTED)
    s.text(62, 396, 'Adaptive probes cover discontinuities.', 12, MUTED)
    s.text(62, 432, 'Default spacing D = 16 px; atlas = Wp × Ha', 12, MUTED)
    s.text(62, 454, 'Ha = Hp + floor(Hp · 0.5)', 12, MUTED)

    surface_patch(s, 390, 104, 330, 404, '3D · square → sphere directions', BLUE)
    # octahedral square
    ox, oy, side = 456, 274, 150
    s.rect(ox, oy, side, side, fill=PAPER, stroke=BLUE, rx=2)
    s.line(ox, oy + side / 2, ox + side, oy + side / 2, RULE, 1)
    s.line(ox + side / 2, oy, ox + side / 2, oy + side, RULE, 1)
    s.poly([(ox, oy), (ox + side, oy), (ox + side, oy + side), (ox, oy + side)], fill='none', stroke=RULE, width=1)
    # folds
    s.line(ox + side / 4, oy, ox + side / 2, oy + side / 2, RULE, 1, dash=True)
    s.line(ox + 3 * side / 4, oy, ox + side / 2, oy + side / 2, RULE, 1, dash=True)
    s.line(ox + side / 4, oy + side, ox + side / 2, oy + side / 2, RULE, 1, dash=True)
    s.line(ox + 3 * side / 4, oy + side, ox + side / 2, oy + side / 2, RULE, 1, dash=True)
    # cells and highlight
    for i in range(1, 6):
        s.line(ox + i * side / 6, oy, ox + i * side / 6, oy + side, RULE, 0.8)
        s.line(ox, oy + i * side / 6, ox + side, oy + i * side / 6, RULE, 0.8)
    s.rect(ox + 4 * side / 6, oy + side / 6, side / 6, side / 6, fill=GOLD, stroke=GOLD, rx=1, opacity=0.35)
    s.text(ox, oy - 10, 'direction map P_oct', 13, BLUE, True)
    # sphere
    cx, cy = 555, 190
    s.ellipse(cx, cy, 72, 50, BLUE, PAPER, 1.5)
    s.ellipse(cx, cy, 72, 18, RULE, 'none', 1, True)
    s.ellipse(cx, cy, 34, 50, RULE, 'none', 1, True)
    hitx, hity = 604, 176
    s.circle(hitx, hity, 4, GOLD)
    s.line(cx, cy, hitx, hity, GOLD, 2, arrow=True)
    s.text(hitx + 8, hity - 8, 'ω', 14, GOLD, True)
    s.line(ox + side + 4, oy + side / 2, cx + 76, cy + 20, MUTED, 2, arrow=True)
    s.text(ox + 10, oy + side + 22, 'each cell has solid angle 4π/N²', 12, MUTED)
    s.text(ox + 10, oy + side + 42, 'a jittered cell is a cone, not one exact direction', 12, MUTED)

    surface_patch(s, 744, 104, 320, 404, '2D · compacted misses feed fallback', GOLD)
    # trace atlas 8x8 with H/M
    gx0, gy0, cell = 776, 158, 22
    states = [
        'HHMMHHMM', 'HMMMHHMM', 'HHHHMMMM', 'MMHHHHMM',
        'MMMHHMMM', 'HHMMMMHH', 'HMMHHHMM', 'MMHHMMMM',
    ]
    for row in range(8):
        for col in range(8):
            st = states[row][col]
            color = GREEN if st == 'H' else GOLD
            s.rect(gx0 + col * cell, gy0 + row * cell, cell - 1, cell - 1,
                   fill=color, stroke=color, rx=1, opacity=0.35)
            s.text(gx0 + col * cell + cell / 2, gy0 + row * cell + 15, st, 10, color,
                   True, anchor='middle', mono=True)
    s.text(776, 148, 'trace atlas · H = hit, M = miss', 13, INK, True)
    s.line(778, 350, 778, 382, MUTED, 2, arrow=True)
    s.text(786, 370, 'compact', 12, MUTED)
    # compact list
    s.rect(776, 388, 250, 70, fill=PAPER, stroke=GOLD, rx=4)
    s.text(790, 412, 'compacted miss list', 14, GOLD, True)
    for i in range(10):
        s.rect(790 + i * 21, 422, 16, 22, fill=GOLD, stroke=GOLD, rx=2, opacity=0.25)
    s.text(776, 470, 'only !bHit lanes continue; hits stay in the atlas', 11, MUTED)
    s.text(790, 488, 'fallback dispatch length = compacted count', 11, MUTED)

    # Bottom: formulas and cautions.
    items = [
        (36, 'Direction', ['ω = P_oct⁻¹(u, v)', 'square cell → sphere direction', 'one direction per trace texel']),
        (286, 'Cone', ['α = acos(1 − 1/N²)', 'used by SDF cone tracing', 'not a screen-space blur radius']),
        (536, 'PDF', ['jittered cell: N²/(4π) inside', 'uniform cell choice: 1/(4π)', 'cell-center: quadrature weight 4π/N²']),
        (786, 'What to inspect', ['trace hit flag and distance', 'PDF is a separate normalized buffer', 'radiance atlas is sampled directions']),
    ]
    for x, title, lines in items:
        s.rect(x, 532, 222, 152, fill=PALE)
        s.text(x + 14, 560, title, 16, GREEN, True)
        for i, line in enumerate(lines):
            s.text(x + 14, 588 + i * 23, line, 12, INK)
    s.footer('Schematic. Real 8×8 trace directions and 6×6 irradiance maps are captured buffers; this diagram does not draw their stochastic samples.')
    return s


# ---------------------------------------------------------------------------
# 5. World radiance cache: clipmap cell, eight corners, octahedral directions
# ---------------------------------------------------------------------------
def radiance_cache() -> SVG:
    s = SVG(
        'Sparse clipmap cell, eight probe corners, octahedral directions',
        'A query point lies inside one clipmap cell; its eight allocated probe corners are interpolated with trilinear weights. Each probe stores directional radiance in an equal-area octahedral map. Schematic; the captured replay resolves one real query and eight real neighbors.',
        1100, 800, subtitle='Trilinear space and octahedral direction are two different interpolations; one query uses both.',
    )
    surface_patch(s, 36, 104, 600, 464, '3D · one clipmap cell and eight probe corners', BLUE)
    # context cells behind
    for i in range(3):
        for j in range(3):
            s.box3(86, 330, 54, i * 1.2, j * 1.2, 0, 1.2, 1.2, 1.2, RULE, PALE, 0.10)
    corners = s.box3(140, 350, 96, 0, 0, 0, 1.2, 1.2, 1.2, BLUE, LIGHT, 0.20)
    q = s.ob(140, 350, 96, 0.45, 0.55, 0.62)
    s.circle(q[0], q[1], 7, RED)
    s.text(q[0] + 10, q[1] + 22, 'x', 18, RED, True)
    # weight labels at corners
    labels = [
        ('c000', '0.027'), ('c001', '0.197'), ('c010', '0.051'), ('c011', '0.376'),
        ('c100', '0.014'), ('c101', '0.105'), ('c110', '0.028'), ('c111', '0.202'),
    ]
    offsets = {
        'c000': (-42, 28), 'c001': (-42, -14), 'c010': (-42, 46), 'c011': (-42, -2),
        'c100': (16, 28), 'c101': (16, -14), 'c110': (16, 46), 'c111': (16, -2),
    }
    for key, weight in labels:
        pt = corners[key]
        s.circle(pt[0], pt[1], 5, BLUE)
        s.line(q[0], q[1], pt[0], pt[1], BLUE, 1, dash=True)
        dx, dy = offsets[key]
        s.chip(pt[0] + dx, pt[1] + dy, weight, BLUE, width=58, size=11)
    s.text(86, 540, 'ProbeCoordFloat = (worldPos − clipmapCorner) / cellSize', 13, MUTED)
    s.text(86, 560, 'lower corner = floor(ProbeCoordFloat − 0.5); alphas = frac(...)', 13, MUTED)
    s.text(552, 510, 'cell [24, 30, 25]', 13, BLUE, True)
    s.text(552, 532, 'clipmap 0', 12, MUTED)

    # Octahedral map.
    surface_patch(s, 660, 104, 404, 260, '2D · octahedral directions per probe', GOLD)
    ox, oy, side = 706, 158, 142
    s.rect(ox, oy, side, side, fill=PAPER, stroke=GOLD, rx=2)
    s.line(ox, oy + side / 2, ox + side, oy + side / 2, RULE, 1)
    s.line(ox + side / 2, oy, ox + side / 2, oy + side, RULE, 1)
    s.line(ox + side / 2, oy, ox, oy + side / 2, RULE, 1, dash=True)
    s.line(ox + side / 2, oy, ox + side, oy + side / 2, RULE, 1, dash=True)
    s.line(ox + side / 2, oy + side, ox, oy + side / 2, RULE, 1, dash=True)
    s.line(ox + side / 2, oy + side, ox + side, oy + side / 2, RULE, 1, dash=True)
    for i in range(1, 8):
        s.line(ox + i * side / 8, oy, ox + i * side / 8, oy + side, RULE, 0.7)
        s.line(ox, oy + i * side / 8, ox + side, oy + i * side / 8, RULE, 0.7)
    s.rect(ox + 5 * side / 8, oy + 2 * side / 8, side / 8, side / 8, fill=GOLD, stroke=GOLD, rx=1, opacity=0.38)
    s.line(ox + side + 12, oy + side / 2, 986, oy + side / 2 - 20, GOLD, 2, arrow=True)
    s.text(880, oy - 10, '32×32 radiance directions', 13, GOLD, True)
    s.text(880, oy + 18, 'final map 34×34 with border', 12, MUTED)
    s.text(880, oy + 48, 'one cell → a cone', 12, MUTED)
    s.text(880, oy + 70, 'cone α = acos(1 − 1/N²)', 12, MUTED)
    s.text(706, oy + side + 22, 'Sampling is not one exact ray at the cell center.', 12, MUTED)

    # Eight probes -> radiance.
    surface_patch(s, 660, 384, 404, 184, '2D · eight neighbors become one value', GOLD)
    ids = [54, 66, 56, 68, 55, 67, 57, 69]
    for i, pid in enumerate(ids):
        x = 680 + (i % 4) * 92
        y = 430 + (i // 4) * 40
        s.chip(x, y, f'p{pid}', GOLD, width=76, size=11)
    s.line(1042, 452, 1042, 486, GOLD, 2)
    s.text(676, 530, 'Σ w_p = 1.000000008 in the replay; parallax is applied per probe.', 11, MUTED)
    s.formula(676, 540, 'L(x,ω) = Σ w_p c_p L_p', 370, 28, size=12)

    # Bottom notes.
    rows = [
        (36, 'Sparse occupancy', ['indirection word 0xFFFFFFFF is invalid', 'a used cell without a probe allocates one', 'cache keeps probes for 8 frames']),
        (390, 'Trilinear eight corners', ['lower cell + eight offsets', 'weights from frac(ProbeCoordFloat − 0.5)', 'invalid neighbor contributes zero']),
        (744, 'Directional footprint', ['32×32 radiance directions per probe', 'cone half-angle ≈0.0884 rad in replay', '32 radiance taps for the captured query']),
    ]
    for x, title, lines in rows:
        s.rect(x, 604, 320, 148, fill=PALE)
        s.text(x + 16, 632, title, 16, GREEN, True)
        for i, line in enumerate(lines):
            s.text(x + 16, 660 + i * 23, line, 12, INK)
    s.footer('Schematic. The real capture selects cell [24,30,25], probes 54,66,56,68,55,67,57,69; atlas radiance (11.7408, 14.5248, 10.6913).')
    return s


# ---------------------------------------------------------------------------
# 6. Temporal reprojection and history rejection
# ---------------------------------------------------------------------------
def temporal_history() -> SVG:
    s = SVG(
        'Reproject the previous sample; reject what does not correspond',
        'A current pixel is reprojected into the previous frame and tested against a 2x2 depth footprint and normal. Accepted history is exposure-corrected and blended by a count-dependent alpha; rejected history is not used. A 3D inset shows a disocclusion. Schematic; the real history buffers and validity masks are separate captures.',
        1100, 760, subtitle='Reprojection gives a candidate; depth, normal and validity decide whether it is usable history.',
    )
    surface_patch(s, 36, 104, 430, 424, '2D · reproject into the previous frame', BLUE)
    # previous grid
    s.rect(72, 154, 158, 118, fill=WASH, stroke=RULE, rx=2)
    s.text(76, 146, 'previous frame', 13, INK, True)
    for i in range(1, 4):
        s.line(72 + i * 39.5, 154, 72 + i * 39.5, 272, RULE, 0.8)
        s.line(72, 154 + i * 29.5, 230, 154 + i * 29.5, RULE, 0.8)
    # current grid
    s.rect(258, 258, 158, 118, fill=WASH, stroke=RULE, rx=2)
    s.text(262, 250, 'current frame', 13, INK, True)
    for i in range(1, 4):
        s.line(258 + i * 39.5, 258, 258 + i * 39.5, 376, RULE, 0.8)
        s.line(258, 258 + i * 29.5, 416, 258 + i * 29.5, RULE, 0.8)
    # motion and reprojection
    s.line(150, 214, 338, 318, RED, 2.5, arrow=True)
    s.text(196, 236, 'motion vector', 13, RED)
    s.circle(338, 318, 5, RED)
    s.text(346, 340, 'current pixel', 12, RED)
    # reprojected 2x2 footprint
    for dx in (-17, 17):
        for dy in (-15, 15):
            s.rect(150 + dx - 14, 214 + dy - 13, 28, 26, fill=GREEN, stroke=GREEN, rx=2, opacity=0.22)
    s.text(90, 300, '2×2 depth taps', 12, GREEN, True)
    s.text(90, 320, 'depth Δ ≤ threshold', 12, MUTED)
    s.text(90, 340, 'normal dot ≥ cos(45°)', 12, MUTED)
    s.text(90, 374, 'Accepted taps are exposure-corrected before blending.', 12, MUTED)
    s.text(90, 394, 'A rejected tap is not zero radiance; it is no usable history.', 12, RED)

    surface_patch(s, 480, 104, 300, 424, '2D · count-dependent blend', GOLD)
    steps = [
        ('L(t−1) history', 'reprojected + validity', 154),
        ('exposure conversion', 'PrevSceneColorPreExposureCorrection', 228),
        ('current L(t)', 'new sample + validity', 302),
        ('L(out)', 'write history + new count', 436),
    ]
    for title, note, y in steps:
        s.rect(506, y, 248, 58, fill=PAPER, stroke=GOLD if 'current' not in title else GREEN, rx=6)
        s.text(520, y + 24, title, 14, GOLD if 'current' not in title else GREEN, True)
        s.text(520, y + 44, note, 11, MUTED)
    s.line(630, 212, 630, 226, GOLD, 2, arrow=True)
    s.line(630, 286, 630, 300, GOLD, 2, arrow=True)
    s.line(630, 360, 630, 434, GOLD, 2, arrow=True)
    s.formula(506, 378, 'N′ = min(N + 1, N_max)', 248, 30, size=12)
    s.formula(506, 506, 'α = 1/(1 + N′)  ·  invalid current: 1/(1 + 4N′)', 248, 30, size=10)

    surface_patch(s, 794, 104, 270, 424, '3D · disocclusion rejection', RED)
    # camera
    s.circle(1018, 300, 8, INK)
    s.poly([(1018, 300), (1038, 288), (1038, 312)], fill=INK, stroke=INK)
    s.text(992, 334, 'camera', 12, MUTED)
    # back wall and foreground edge
    s.rect(842, 180, 82, 210, fill=WASH, stroke=RULE, rx=1)
    s.text(850, 172, 'back wall', 12, MUTED)
    s.rect(916, 250, 22, 170, fill=LIGHT, stroke=RULE, rx=1)
    s.text(918, 242, 'near edge', 12, MUTED)
    # previous sample on back wall -> reprojects to near edge
    s.circle(882, 280, 5, GOLD)
    s.text(860, 300, 'old sample', 11, GOLD)
    s.line(882, 280, 928, 320, RED, 2, dash=True, arrow=True)
    s.circle(928, 320, 5, RED)
    s.text(890, 456, 'depth mismatch → reject', 11, RED)
    # same-plane sample accepted
    s.circle(882, 232, 5, GREEN)
    s.text(858, 222, 'same plane', 11, GREEN)
    s.line(882, 232, 920, 232, GREEN, 2, dash=True, arrow=True)
    s.text(900, 212, 'accept', 11, GREEN)
    s.text(806, 398, 'History is correlated reconstruction state,', 12, MUTED)
    s.text(806, 418, 'not another independent Monte Carlo sample.', 12, MUTED)

    # Bottom notes.
    rows = [
        (36, 'What is rejected', ['depth difference above the relative threshold', 'normal dot below the normal threshold', 'invalid current sample changes the blend']),
        (390, 'What is retained', ['the reprojected coordinate and 2×2 taps', 'the previous count and exposure ratio', 'the validity mask, not just a color']),
        (744, 'What to inspect', ['validity mask and previous count', 'exposure correction and clamp bounds', 'alpha and the effective history length']),
    ]
    for x, title, lines in rows:
        s.rect(x, 552, 320, 152, fill=PALE)
        s.text(x + 16, 580, title, 16, GREEN, True)
        for i, line in enumerate(lines):
            s.text(x + 16, 608 + i * 23, line, 12, INK)
    s.footer('Schematic. Real Lumen screen-probe history uses at most 10 frames; distance threshold 0.01 (0.03 foliage), normal threshold 45°.')
    return s


# ---------------------------------------------------------------------------
# 7. Deferred direct light vs cached indirect light
# ---------------------------------------------------------------------------
def deferred_direct_cached() -> SVG:
    s = SVG(
        'One visible point, two lighting owners',
        'A deferred G-buffer supplies the primary surface; direct lights evaluate one path family and Lumen cached lighting supplies another. A 3D view separates direct light at x from light bounced through a secondary point y. Schematic; not a captured G-buffer or a measured pass cost.',
        1100, 760, subtitle='Direct light at x and cached indirect light through y are different path families, not duplicate terms.',
    )
    surface_patch(s, 36, 104, 520, 424, '2D · primary G-buffer and two owners', GREEN)
    layers = [
        ('depth', 'primary geometric x', GREEN),
        ('normal', 'n_g, n_s', BLUE),
        ('base color', 'ρ_d', GOLD),
        ('metallic / roughness / specular', 'f_s parameters', RED),
    ]
    for i, (title, note, color) in enumerate(layers):
        y = 158 + i * 62
        s.rect(66, y, 262, 52, fill=PAPER, stroke=color, rx=5, width=1.5)
        s.text(80, y + 22, title, 13, color, True)
        s.text(80, y + 40, note, 10, MUTED)
    s.line(328, 250, 378, 220, BLUE, 2, arrow=True)
    s.line(328, 250, 378, 300, GOLD, 2, arrow=True)
    s.text(384, 210, 'deferred direct', 14, BLUE, True)
    s.text(384, 228, 'V(x↔light)', 11, MUTED)
    s.text(384, 316, 'Lumen indirect', 14, GOLD, True)
    s.text(384, 334, 'cached secondary', 11, MUTED)
    s.formula(66, 430, 'L_o(x) = L_e + (ρ/π)(E_direct(x) + E_indirect(x))', 466, 30, size=12)
    s.formula(66, 466, 'E_indirect(x) = ∫ (ρ(y)/π) E_direct(y) G dA(y)', 466, 30, size=12)
    s.text(66, 504, 'Different path families: do not subtract cached direct at y', 12, RED)
    s.text(66, 520, 'as if it duplicated the direct pass at x.', 12, RED)

    surface_patch(s, 580, 104, 484, 424, '3D · direct at x versus indirect through y', BLUE)
    # room-ish 3D: floor, back wall
    s.poly([(640, 430), (940, 470), (1010, 400), (700, 360)], fill=WASH, stroke=RULE, width=1)
    s.poly([(700, 360), (1010, 400), (1010, 190), (700, 150)], fill=LIGHT, stroke=RULE, width=1, opacity=0.6)
    s.text(720, 176, 'secondary wall', 12, MUTED)
    # direct light and x
    x = (860, 360)
    y = (770, 300)
    s.circle(*x, 7, RED)
    s.text(x[0] + 12, x[1] + 22, 'x', 17, RED, True)
    s.circle(*y, 6, GOLD)
    s.text(y[0] - 24, y[1] - 10, 'y', 17, GOLD, True)
    # local light
    s.circle(900, 148, 8, GOLD)
    s.text(914, 152, 'L', 17, GOLD, True)
    s.line(898, 158, 872, 340, BLUE, 3, arrow=True)
    s.text(900, 264, 'direct path', 13, BLUE, True)
    s.text(900, 282, 'V, shadow map', 11, MUTED)
    # indirect path via y
    s.polyline([(898, 158), (780, 286), (850, 352)], GOLD, 2.5, dash=True, arrow=True)
    s.text(712, 246, 'indirect path', 13, GOLD, True)
    s.text(712, 264, 'L → y → x', 11, MUTED)
    s.text(636, 390, 'G(x↔y) includes both cosines and 1/r².', 11, MUTED)
    s.text(636, 410, 'The tracer supplies an approximate visibility.', 11, MUTED)

    # Bottom GBuffer triplet note.
    s.rect(36, 552, 1028, 152, fill=WASH, stroke=RULE, rx=8)
    s.text(54, 580, 'The GBuffer triplet is an input, not the lighting result', 17, GREEN, True)
    items = [
        ('metallic', 'selects F0 / metal workflow'),
        ('roughness', 'shapes the specular lobe width'),
        ('specular', 'F0 for the legacy default-lit path'),
    ]
    for i, (title, note) in enumerate(items):
        x = 66 + i * 330
        s.chip(x, 600, title, RED, width=142, size=12)
        s.text(x, 646, note, 12, INK)
    s.text(54, 686, 'Inspect the captured metallic, roughness and specular channel views together; they parameterize f_s, while the lighting integral is evaluated later.', 12, MUTED)
    s.footer('Schematic. The actual channel views and pass identities are in the captured GBuffer figures; this is not a captured G-buffer.')
    return s


# ---------------------------------------------------------------------------
# Educational CPU reference: clean image from the PFM, native SVG caption
# ---------------------------------------------------------------------------
def load_path_tracer():
    spec = importlib.util.spec_from_file_location('lumen_path_tracer_reference', PATH_TRACER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Cannot load original path tracer: {PATH_TRACER}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_pfm(path: Path):
    with path.open('rb') as stream:
        magic = stream.readline().strip()
        if magic != b'PF':
            raise ValueError(f'Expected binary PFM, got {magic!r}')
        width, height = (int(value) for value in stream.readline().split())
        scale = float(stream.readline())
        payload = stream.read()
    count = width * height * 3
    if len(payload) != count * 4:
        raise ValueError(f'PFM payload is {len(payload)} bytes, expected {count * 4}')
    endian = '<' if scale < 0 else '>'
    values = struct.unpack(endian + str(count) + 'f', payload)
    rows = [values[row * width * 3:(row + 1) * width * 3] for row in range(height)]
    # PFM stores rows bottom-up; the image and PNG are top-down.
    rows.reverse()
    return width, height, rows


def decode_png_rgb(path: Path):
    data = path.read_bytes()
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('Not a PNG')
    pos = 8
    width = height = bit_depth = color_type = interlace = None
    idat = bytearray()
    while pos < len(data):
        length = struct.unpack('>I', data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if tag == b'IHDR':
            width, height, bit_depth, color_type, _comp, _filt, interlace = struct.unpack('>IIBBBBB', chunk)
        elif tag == b'IDAT':
            idat.extend(chunk)
        elif tag == b'IEND':
            break
    if (bit_depth, color_type, interlace) != (8, 2, 0):
        raise ValueError('Expected 8-bit truecolor, non-interlaced PNG')
    raw = zlib.decompress(bytes(idat))
    stride = width * 3
    rows = []
    previous = bytearray(stride)
    for y in range(height):
        base = y * (stride + 1)
        filter_type = raw[base]
        row = bytearray(raw[base + 1:base + 1 + stride])
        if filter_type == 1:
            for i in range(stride):
                row[i] = (row[i] + (row[i - 3] if i >= 3 else 0)) & 255
        elif filter_type == 2:
            for i in range(stride):
                row[i] = (row[i] + previous[i]) & 255
        elif filter_type == 3:
            for i in range(stride):
                left = row[i - 3] if i >= 3 else 0
                row[i] = (row[i] + ((left + previous[i]) // 2)) & 255
        elif filter_type == 4:
            for i in range(stride):
                left = row[i - 3] if i >= 3 else 0
                up = previous[i]
                upper_left = previous[i - 3] if i >= 3 else 0
                p = left + up - upper_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - upper_left)
                predictor = left if pa <= pb and pa <= pc else (up if pb <= pc else upper_left)
                row[i] = (row[i] + predictor) & 255
        elif filter_type != 0:
            raise ValueError(f'Unsupported PNG filter {filter_type}')
        rows.append(bytes(row))
        previous = row
    return width, height, rows


def build_cpu_reference():
    module = load_path_tracer()
    width, height, pfm_rows = read_pfm(PFM)
    image = [[(row[i], row[i + 1], row[i + 2]) for i in range(0, len(row), 3)] for row in pfm_rows]
    # This is the original path_tracer.py display transform at exposure 0.6.
    with tempfile.TemporaryDirectory(prefix='lumen-reference-') as temporary:
        output = Path(temporary) / 'reference.png'
        module.write_png(str(output), image, EXPOSURE, label_lines=())
        clean_png = output.read_bytes()

    png_width, png_height, original_rows = decode_png_rgb(ORIGINAL_PNG)
    if (png_width, png_height) != (width, height):
        raise ValueError('Reference PNG and PFM dimensions differ')
    scale = max(1, width // 400)
    footer_rows = 3 * 9 * scale + 2 * 4 * scale
    compared = height - footer_rows
    mismatches = 0
    max_error = 0
    for y in range(compared):
        expected = pfm_rows[y]
        actual = original_rows[y]
        for x in range(width):
            tri = module.tone_map((expected[3 * x], expected[3 * x + 1], expected[3 * x + 2]), EXPOSURE)
            got = (actual[3 * x], actual[3 * x + 1], actual[3 * x + 2])
            for a, b in zip(tri, got):
                if a != b:
                    mismatches += 1
                    max_error = max(max_error, abs(a - b))
    if mismatches:
        raise ValueError(f'CPU reference image region differs from original PNG: {mismatches} channel mismatches, max error {max_error}')

    payload = base64.b64encode(clean_png).decode('ascii')
    s = SVG(
        'Educational CPU reference · not an Unreal capture',
        'This is the original educational CPU path tracer rendered from the checked-in linear PFM. The burned-in bitmap footer is removed; the caption below is native SVG text. It is NOT an Unreal scene, NOT an Unreal capture, and NOT the matched path-traced reference.',
        760, 640, badge=False, header=False, schematic=False,
    )
    s.rect(24, 20, 342, 24, fill='#f8e9e5', stroke=RED, rx=4)
    s.text(34, 37, 'EDUCATIONAL CPU REFERENCE', 12, RED, True)
    s.rect(378, 20, 358, 24, fill='#f8e9e5', stroke=RED, rx=4)
    s.text(388, 37, 'NOT AN UNREAL CAPTURE', 12, RED, True)
    s.text(24, 78, 'Educational CPU reference', 26, INK, True)
    s.text(24, 104, f'seed {SEED} · {SPP} spp · max depth {MAX_DEPTH} · exposure {EXPOSURE}', 16, MUTED)
    s.rect(92, 122, 576, 412, fill=WASH, stroke=RULE, rx=6, width=1.5)
    s.parts.append(
        f'<image x="100" y="130" width="560" height="400" preserveAspectRatio="none" '
        f'image-rendering="pixelated" href="data:image/png;base64,{payload}"/>'
    )
    s.rect(100, 130, 560, 400, fill='none', stroke=INK, rx=0, width=1)
    s.text(24, 552, 'Image: reference_linear.pfm → path_tracer.py tone map at exposure 0.6.', 11, MUTED)
    s.text(24, 570, 'PFM rows reversed bottom-up → top-down; the original PNG footer is not included.', 11, MUTED)
    s.text(24, 588, f'Image region checked against original PNG rows 0–{compared - 1}; footer rows {compared}–{height - 1}.', 11, MUTED)
    s.text(24, 606, 'Lambertian-only CPU scene; the tall right block is deliberately diffuse, not the Unreal glossy block.', 11, MUTED)
    s.text(24, 624, 'NOT Unreal. NOT a substitute for the matched Lumen / path-traced reference pair.', 11, RED, True)
    s.parts.append('</g></svg>')
    return s.render(), {
        'pfm_sha256': sha256_file(PFM),
        'original_png_sha256': sha256_file(ORIGINAL_PNG),
        'clean_png_sha256': sha256_bytes(clean_png),
        'clean_png_bytes': len(clean_png),
        'raster_width': width,
        'raster_height': height,
        'compared_rows': compared,
        'footer_rows': footer_rows,
        'channel_mismatches': mismatches,
        'max_channel_error': max_error,
    }, clean_png


CONCEPTS = [
    dict(slug='solid-angle-sampling', builder=solid_angle,
         placement='lumen.html, #path-geometry, after the M09–M10 projected-solid-angle paragraph',
         evidence='Adjacent matched Lumen/path-traced image pair (also discussed in §08) is a convergence/path comparison, not a sample-direction dump.'),
    dict(slug='sampling-mis', builder=sampling_mis,
         placement='lumen.html, #sampling-and-mis, after the M29 MIS partition proof',
         evidence='M27 selected NEE sample (0.159154943) and M28 power-heuristic weights; no new numeric claim.'),
    dict(slug='surface-cache-lookup', builder=surface_cache_lookup,
         placement='lumen-engineering-v2.html, #tracing-representations, after the hit-to-card lookup equation',
         evidence='replays/surface-cache/cache-texel-replay.json: page 35, 8x8 texels, atlas (1067,4), weights (1,0,0,0).'),
    dict(slug='screen-probe-angular', builder=screen_probe_angular,
         placement='lumen-engineering-v2.html, #screen-probes, before the probe buffer triplet',
         evidence='figure-manifest.json and the captured probe radiance / lighting-PDF / trace-hit buffers.'),
    dict(slug='radiance-cache', builder=radiance_cache,
         placement='lumen-engineering-v2.html, #radiance-cache, after the trilinear/parallax equations',
         evidence='replays/world-cache/world-cache-replay.json: cell [24,30,25], 8 probes, weights sum 1.000000008, radiance (11.7408,14.5248,10.6913).'),
    dict(slug='temporal-history', builder=temporal_history,
         placement='lumen-engineering-v2.html, #time-and-invalidation, after the history-rejection paragraph',
         evidence='pixel temporal alpha and history rejection: max 10 frames, distance 0.01 (0.03 foliage), normal 45 degrees; source-referenced.'),
    dict(slug='deferred-direct-cached', builder=deferred_direct_cached,
         placement='lumen-deferred.html, #material-evaluation, after the default-lit GBuffer explanation',
         evidence='figure-manifest.json: captured GBufferB group; the metallic/roughness/specular triplet plus selective-mask and shading-model metadata.'),
]


def build_manifest(diagram_records, cpu_record, cpu_svg_sha):
    return {
        'schema': 'lumen-reading-visuals/1',
        'engine_commit': '71fe36aac5a8df5ccd66c763ffc902b29b6a9c43',
        'generator': {
            'path': 'Scripts/Docs/unreal_articles/labs/lumen-reading/generate.py',
            'sha256': sha256_file(Path(__file__)),
        },
        'not_runtime_images': True,
        'diagrams': diagram_records,
        'cpu_reference': dict(
            file='assets/lumen-reading/cpu-reference.svg',
            sha256=cpu_svg_sha,
            width=760,
            height=640,
            **cpu_record,
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='verify checked-in outputs instead of writing')
    args = parser.parse_args(argv)

    texts = {}
    records = []
    for concept in CONCEPTS:
        svg = concept['builder']()
        text = svg.render()
        slug = concept['slug']
        texts[f'diagrams/{slug}.svg'] = text
        records.append({
            'slug': slug,
            'file': f'assets/lumen-reading/diagrams/{slug}.svg',
            'sha256': sha256_bytes(text.encode('utf-8')),
            'width': svg.width,
            'height': svg.height,
            'title': svg.parts[1].split('>', 1)[1].split('<', 1)[0],
            'placement': concept['placement'],
            'evidence': concept['evidence'],
        })

    cpu_text, cpu_record, clean_png = build_cpu_reference()
    texts['cpu-reference.svg'] = cpu_text
    manifest = build_manifest(records, cpu_record, sha256_bytes(cpu_text.encode('utf-8')))
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + '\n'
    texts['manifest.json'] = manifest_text

    if args.check:
        failures = []
        for relative, expected in texts.items():
            path = OUT / relative
            if not path.is_file():
                failures.append(f'missing {path}')
            elif path.read_text(encoding='utf-8') != expected:
                failures.append(f'stale {path}')
        # The clean PNG is deterministic; compare bytes, not only presence.
        if not CLEAN_PNG.is_file():
            failures.append(f'missing {CLEAN_PNG}')
        elif CLEAN_PNG.read_bytes() != clean_png:
            failures.append(f'stale {CLEAN_PNG}')
        for record in records:
            source = texts[f'diagrams/{record["slug"]}.svg']
            root = ET.fromstring(source)
            ns = '{http://www.w3.org/2000/svg}'
            title = root.find(ns + 'title')
            desc = root.find(ns + 'desc')
            body = ''.join(root.itertext())
            if title is None or desc is None:
                failures.append(f'{record["slug"]}: missing title/desc')
            if root.get('data-schematic') != 'true':
                failures.append(f'{record["slug"]}: not marked schematic')
            if len(root.findall('.//' + ns + 'text')) < 20:
                failures.append(f'{record["slug"]}: too few native text elements')
            if '2D' not in body or '3D' not in body:
                failures.append(f'{record["slug"]}: missing 2D/3D pair')
        cpu_root = ET.fromstring(texts['cpu-reference.svg'])
        if cpu_root.get('data-schematic') != 'false':
            failures.append('cpu-reference: wrong schematic flag')
        if len(cpu_root.findall('.//{http://www.w3.org/2000/svg}image')) != 1:
            failures.append('cpu-reference: expected exactly one embedded image')
        if failures:
            print('\n'.join(failures))
            return 1
        print(f'Verified {len(records)} diagrams, CPU reference, clean PNG, and manifest.')
        return 0

    CLEAN_PNG.write_bytes(clean_png)
    for relative, text in texts.items():
        write_lf(OUT / relative, text)
    print(f'Wrote {len(records)} diagrams and {CPU_SVG.relative_to(ROOT)}')
    for record in records:
        print(f'  {record["slug"]}: {record["width"]}x{record["height"]}')
    print(f'  cpu-reference: 760x640, compared rows {cpu_record["compared_rows"]}, mismatches {cpu_record["channel_mismatches"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
