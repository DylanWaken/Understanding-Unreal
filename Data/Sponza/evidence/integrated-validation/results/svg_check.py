#!/usr/bin/env python3
"""XML + geometry validation for every published SVG, with strict bounds for
the new frame-flow diagram (both the source and published copies)."""
from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import ImageFont

REPO = Path(r"D:/UnrealUnderstanding")
RESULTS = Path(r"D:/UnrealUnderstanding-Review/final-validation/results")
SVG_ROOTS = [
    REPO / "Docs/Unreal/assets",
    REPO / "Scripts/Docs/unreal_articles/labs",
]
NS = "{http://www.w3.org/2000/svg}"
NUM = re.compile(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?")
FONT = None
failures: list[str] = []
checks: list[dict] = []


def ok(name, detail, **kw):
    checks.append({"name": name, "ok": True, "detail": detail, **kw})


def fail(name, detail, **kw):
    checks.append({"name": name, "ok": False, "detail": detail, **kw})
    failures.append(f"{name}: {detail}")


def font(size):
    global FONT
    if FONT is None:
        try:
            FONT = ImageFont.truetype("arial.ttf", 100)
        except OSError:
            FONT = None
    if FONT is None:
        return None
    return FONT


def text_width(text: str, size: float) -> float:
    f = font(size)
    if f is not None:
        return float(f.getlength(text)) * (size / 100.0)
    return 0.62 * size * len(text)


def num_attr(el, name, default=0.0):
    raw = el.get(name)
    if raw is None:
        return default
    m = NUM.search(raw)
    return float(m.group(0)) if m else default


def path_points(d: str):
    """Conservative point set for an SVG path (absolute control points)."""
    tokens = re.findall(r"[A-Za-z]|" + NUM.pattern, d)
    pts = []
    i = 0
    cur = (0.0, 0.0)
    start = (0.0, 0.0)
    cmd = None
    last = None
    while i < len(tokens):
        t = tokens[i]
        if t.isalpha():
            cmd = t
            i += 1
            if cmd in "Zz":
                cur = start
                continue
            if i >= len(tokens):
                break
        rel = cmd.islower()
        c = cmd.upper()
        if c == "M":
            x, y = float(tokens[i]), float(tokens[i + 1]); i += 2
            cur = (cur[0] + x, cur[1] + y) if rel else (x, y)
            start = cur
            pts.append(cur)
            cmd = "l" if rel else "L"  # subsequent pairs are line-to
            last = cur
        elif c == "L":
            x, y = float(tokens[i]), float(tokens[i + 1]); i += 2
            cur = (cur[0] + x, cur[1] + y) if rel else (x, y)
            pts.append(cur); last = cur
        elif c == "H":
            x = float(tokens[i]); i += 1
            cur = (cur[0] + x, cur[1]) if rel else (x, cur[1])
            pts.append(cur); last = cur
        elif c == "V":
            y = float(tokens[i]); i += 1
            cur = (cur[0], cur[1] + y) if rel else (cur[0], y)
            pts.append(cur); last = cur
        elif c in ("C", "S", "Q", "T"):
            n = {"C": 6, "S": 4, "Q": 4, "T": 2}[c]
            vals = [float(v) for v in tokens[i:i + n]]
            i += n
            for j in range(0, n, 2):
                if rel:
                    pts.append((cur[0] + vals[j], cur[1] + vals[j + 1]))
                else:
                    pts.append((vals[j], vals[j + 1]))
            cur = pts[-1]; last = cur
        elif c == "A":
            rx, ry, rot, laf, sf, x, y = tokens[i:i + 7]; i += 7
            x, y = float(x), float(y)
            cur = (cur[0] + x, cur[1] + y) if rel else (x, y)
            pts.append(cur); last = cur
        else:
            i += 1
    return pts


def element_bounds(el):
    tag = el.tag.replace(NS, "")
    if tag in ("rect", "image"):
        x, y = num_attr(el, "x"), num_attr(el, "y")
        w, h = num_attr(el, "width"), num_attr(el, "height")
        return [(x, y), (x + w, y + h)]
    if tag in ("circle", "ellipse"):
        cx, cy = num_attr(el, "cx"), num_attr(el, "cy")
        if tag == "circle":
            r = num_attr(el, "r")
            return [(cx - r, cy - r), (cx + r, cy + r)]
        rx, ry = num_attr(el, "rx"), num_attr(el, "ry")
        return [(cx - rx, cy - ry), (cx + rx, cy + ry)]
    if tag == "line":
        return [(num_attr(el, "x1"), num_attr(el, "y1")), (num_attr(el, "x2"), num_attr(el, "y2"))]
    if tag in ("polygon", "polyline"):
        vals = [float(v) for v in NUM.findall(el.get("points", ""))]
        return list(zip(vals[0::2], vals[1::2]))
    if tag == "path":
        return path_points(el.get("d", ""))
    return []


def check_svg(path: Path, strict: bool):
    rel = path.relative_to(REPO)
    try:
        root = ET.parse(path).getroot()
    except Exception as exc:  # noqa: BLE001
        fail(f"svg.xml.{rel}", f"XML parse error: {exc}")
        return None
    ok(f"svg.xml.{rel}", "well-formed XML")

    vb = root.get("viewBox")
    if vb:
        vals = [float(v) for v in NUM.findall(vb)]
        if len(vals) != 4 or vals[2] <= 0 or vals[3] <= 0:
            fail(f"svg.viewbox.{rel}", f"invalid viewBox {vb!r}")
            return None
        minx, miny, width, height = vals
    else:
        width, height = num_attr(root, "width"), num_attr(root, "height")
        minx = miny = 0.0
        if width <= 0 or height <= 0:
            fail(f"svg.viewbox.{rel}", "no viewBox and no positive width/height")
            return None

    if not strict:
        return {"width": width, "height": height}

    bad = []
    elements = 0
    for el in root.iter():
        tag = el.tag.replace(NS, "")
        if tag in ("svg", "defs", "marker", "title", "desc", "g", "linearGradient",
                   "radialGradient", "stop", "clipPath", "filter", "style", "metadata"):
            continue
        if tag == "text":
            elements += 1
            size = num_attr(el, "font-size", 16.0)
            x, y = num_attr(el, "x"), num_attr(el, "y")
            anchor = el.get("text-anchor", "start")
            text = "".join(el.itertext())
            w = text_width(text, size)
            if anchor == "middle":
                x0 = x - w / 2
            elif anchor == "end":
                x0 = x - w
            else:
                x0 = x
            x1 = x0 + w
            y0, y1 = y - size * 0.85, y + size * 0.25
            if x0 < minx - 0.5 or x1 > minx + width + 0.5 or y0 < miny - 0.5 or y1 > miny + height + 0.5:
                bad.append(f"text {text[:60]!r} bbox=({x0:.1f},{y0:.1f})-({x1:.1f},{y1:.1f})")
            continue
        pts = element_bounds(el)
        if not pts:
            continue
        elements += 1
        stroke = num_attr(el, "stroke-width", 0.0) / 2.0 if el.get("stroke") not in (None, "none") else 0.0
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x0, x1 = min(xs) - stroke, max(xs) + stroke
        y0, y1 = min(ys) - stroke, max(ys) + stroke
        if x0 < minx - 0.5 or x1 > minx + width + 0.5 or y0 < miny - 0.5 or y1 > miny + height + 0.5:
            bad.append(f"{tag} bbox=({x0:.1f},{y0:.1f})-({x1:.1f},{y1:.1f})")
    if bad:
        fail(f"svg.bounds.{rel}", f"{len(bad)} element(s) outside viewBox {vb}: " + "; ".join(bad[:8]))
    else:
        ok(f"svg.bounds.{rel}", f"{elements} text/shape elements inside viewBox {vb}")
    return {"width": width, "height": height, "elements": elements}


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    seen = set()
    counts = {"svg": 0, "strict": 0}
    svg_info = {}
    for root_dir in SVG_ROOTS:
        for path in sorted(root_dir.rglob("*.svg")):
            if path in seen:
                continue
            seen.add(path)
            counts["svg"] += 1
            strict = path.name == "frame-flow.svg"
            info = check_svg(path, strict)
            if strict:
                counts["strict"] += 1
                svg_info[str(path.relative_to(REPO))] = info
    result = {
        "schema": "lumen-svg-geometry-check-v1",
        "svg_count": counts["svg"],
        "strict_frame_flow": svg_info,
        "checks": checks,
        "failures": failures,
        "ok": not failures,
    }
    (RESULTS / "svg_checks.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"SVG check: {counts['svg']} files parsed, {counts['strict']} frame-flow strict, "
          f"{len(checks)} checks, {len(failures)} failures")
    for f in failures:
        print("FAIL:", f)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
