"""Render the recorded world-cache lookup as a deterministic standalone SVG."""

import argparse
from html import escape
import json
from pathlib import Path


def vector(values, places=5):
    return "(" + ", ".join(f"{value:.{places}g}" for value in values) + ")"


def draw(report):
    witness = report["queryWitness"]
    lookup = report["cacheLookup"]
    comparison = report["comparison"]
    query = lookup["query"]
    svg = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1140" height="862" viewBox="0 0 1140 862" role="img" aria-labelledby="title description">',
        '<title id="title">World Radiance Cache lookup for captured lane 190</title>',
        '<desc id="description">One compacted screen-probe ray selects eight world-cache probes. Thirty-two radiance taps and eight depth values feed nested trilinear interpolation.</desc>',
        '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0L8 4L0 8" fill="#617589"/></marker></defs>',
        '<style>text{font-family:Arial,sans-serif;fill:#172f42;font-size:17px}.small{font-size:15px}.value{font:17px Consolas,monospace}.heading{font-weight:700;font-size:21px}.arrow{fill:none;stroke:#617589;stroke-width:2;marker-end:url(#arrow)}</style>',
        '<rect width="1140" height="862" fill="#f5f8fa"/>',
    ]

    def text(x, y, value, style=""):
        svg.append(f'<text x="{x}" y="{y}" class="{style}">{escape(str(value))}</text>')

    def panel(x, y, width, height, color):
        svg.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="10" fill="{color}" stroke="#bacbd5"/>')

    def arrow(path):
        svg.append(f'<path class="arrow" d="{path}"/>')

    text(32, 36, "A captured World Radiance Cache lookup", "heading")
    text(32, 63, "binary-20261112-153914 · graph 0001 · pass 167 · compacted lane 190", "small")
    panel(32, 88, 1076, 116, "#e8f0f5")
    text(50, 115, f"Screen probe {witness['screenProbeIndex']}, ray texel {tuple(witness['traceTexelXY'])}, BlueNoise R/G {tuple(map(int, witness['blueNoiseShaderRG']))}", "heading")
    text(50, 146, f"Translated-world origin {vector(query['translatedWorldPosition'])}", "value")
    text(50, 173, f"Direction {vector(query['direction'])}, cone half-angle {query['coneHalfAngle']:.6f} rad", "value")
    arrow("M570 205 L570 226")
    panel(32, 228, 1076, 282, "#ffffff")
    text(50, 255, f"Clipmap {lookup['selectedClipmap']}, bottom cell {tuple(lookup['bottomCell'])}, fractions {vector(lookup['fractionalWeights'])}", "heading")
    for x, name in ((56, "Corner"), (245, "Probe index"), (480, "Trilinear weight"), (748, "Depth"), (920, "Radiance taps")):
        text(x, 285, name, "small")
    for index, neighbor in enumerate(lookup["neighbors"]):
        y = 311 + index * 25
        if index % 2 == 0:
            svg.append(f'<rect x="46" y="{y - 17}" width="1048" height="24" fill="#edf4f6"/>')
        for x, value in (
            (56, str(tuple(neighbor["offsetBits"]))),
            (245, str(neighbor["probeIndex"])),
            (480, f"{neighbor['trilinearWeight']:.6f}"),
            (748, f"{neighbor['depth']['distance']:g}"),
            (920, "4"),
        ):
            text(x, y, value, "value")
    arrow("M570 511 L570 532")
    panel(32, 534, 1076, 106, "#e1f2ed")
    text(50, 562, "Bilinear radiance sampling, then nested Z → Y → X lerps", "heading")
    text(50, 591, f"{lookup['radianceTapCount']} radiance taps and {len(lookup['neighbors'])} depth values; sum of corner weights {lookup['sumWeights']:.9f}", "value")
    text(50, 616, f"Cache radiance {vector(lookup['cacheRadiance'], 7)} · cache depth {lookup['cacheTraceHitDistance']:.9f}", "value")
    arrow("M570 641 L570 655 L304 655 L304 678")
    arrow("M570 655 L836 655 L836 678")
    panel(32, 680, 535, 136, "#e7eff8")
    panel(583, 680, 525, 136, "#eef0f7")
    text(50, 708, "Depth written to TraceHit", "heading")
    text(50, 737, f"Decoded GPU depth {comparison['capturedDistance']:.9f}", "value")
    text(50, 765, "CPU and recorded decoded distance are equal.", "small")
    text(601, 708, "Radiance continues through the trace", "heading")
    text(601, 737, "Exposure, dither and the R11G11B10 store follow.", "small")
    text(601, 765, "The cache subtotal is compared at its own boundary.", "small")
    text(32, 842, "Exact atlas coordinates, input hashes, tap values and depth flags are in world-cache-replay.json.", "small")
    svg.append("</svg>")
    return "\n".join(svg) + "\n"


def main():
    directory = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=directory / "world-cache-replay.json")
    parser.add_argument("--output", type=Path, default=directory / "world-cache-flow.svg")
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    args.output.write_text(draw(report), encoding="utf-8")


if __name__ == "__main__":
    main()
