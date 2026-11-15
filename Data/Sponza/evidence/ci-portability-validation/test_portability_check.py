#!/usr/bin/env python3
"""Positive and negative checks for the portable lumen-reading comparison."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
GENERATOR = ROOT / 'Scripts' / 'Docs' / 'unreal_articles' / 'labs' / 'lumen-reading' / 'generate.py'
OUT = ROOT / 'Docs' / 'Unreal' / 'assets' / 'lumen-reading'


def load():
    spec = importlib.util.spec_from_file_location('lumen_reading_generate_test', GENERATOR)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def png_chunks(data: bytes):
    pos = 8
    chunks = []
    while pos < len(data):
        length = struct.unpack('>I', data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        chunks.append((tag, chunk))
        if tag == b'IEND':
            break
    return chunks


def rebuild_png(data: bytes, level: int, flip_index: int | None = None) -> bytes:
    chunks = png_chunks(data)
    _, raw = load().png_payload_identity(data)
    raw = bytearray(raw)
    if flip_index is not None:
        raw[flip_index] ^= 0xFF
    out = bytearray(b'\x89PNG\r\n\x1a\n')
    for tag, chunk in chunks:
        payload = zlib.compress(bytes(raw), level) if tag == b'IDAT' else chunk
        out.extend(struct.pack('>I', len(payload)))
        out.extend(tag)
        out.extend(payload)
        out.extend(struct.pack('>I', zlib.crc32(tag + payload) & 0xFFFFFFFF))
    return bytes(out)


def replace_image_payload(svg_text: str, png: bytes) -> str:
    marker = 'data:image/png;base64,'
    start = svg_text.index(marker) + len(marker)
    stop = svg_text.index('"', start)
    encoded = base64.b64encode(png).decode('ascii')
    return svg_text[:start] + encoded + svg_text[stop:]


def main():
    gen = load()
    checked_png = (OUT / 'cpu-reference-render.png').read_bytes()
    checked_svg = (OUT / 'cpu-reference.svg').read_text(encoding='utf-8')
    checked_manifest = json.loads((OUT / 'manifest.json').read_text(encoding='utf-8'))

    level9 = rebuild_png(checked_png, 9)
    level1 = rebuild_png(checked_png, 1)
    assert hashlib.sha256(level9).digest() != hashlib.sha256(level1).digest(), \
        'test setup: compression levels produced identical bytes'
    assert gen.png_payload_identity(level9) == gen.png_payload_identity(level1), \
        'PNG identity must ignore the deflate stream'
    assert gen.png_payload_sha256(level9) == gen.png_payload_sha256(level1), \
        'PNG identity digest must ignore the deflate stream'

    changed_pixel = rebuild_png(checked_png, 9, flip_index=100)
    assert gen.png_payload_identity(level9) != gen.png_payload_identity(changed_pixel), \
        'PNG identity must catch a changed pixel'

    recompressed_svg = replace_image_payload(checked_svg, level1)
    assert gen.canonical_cpu_svg(recompressed_svg) == gen.canonical_cpu_svg(checked_svg), \
        'CPU SVG comparison must ignore base64 compression differences'
    changed_caption = checked_svg.replace('Educational CPU reference', 'Educational CPU reference!', 1)
    assert gen.canonical_cpu_svg(changed_caption) != gen.canonical_cpu_svg(checked_svg), \
        'CPU SVG comparison must catch caption drift'
    changed_svg_image = replace_image_payload(checked_svg, changed_pixel)
    assert gen.canonical_cpu_svg(changed_svg_image) != gen.canonical_cpu_svg(checked_svg), \
        'CPU SVG comparison must catch embedded image drift'

    manifest_with_new_self_hash = json.loads(json.dumps(checked_manifest))
    manifest_with_new_self_hash['generator']['sha256'] = '0' * 64
    assert gen.portable_manifest(manifest_with_new_self_hash) != gen.portable_manifest(checked_manifest), \
        'manifest comparison must catch generator source hash drift'
    manifest_with_changed_width = json.loads(json.dumps(checked_manifest))
    manifest_with_changed_width['diagrams'][0]['width'] += 1
    assert gen.portable_manifest(manifest_with_changed_width) != gen.portable_manifest(checked_manifest), \
        'manifest comparison must catch diagram metadata drift'
    manifest_with_changed_pfm = json.loads(json.dumps(checked_manifest))
    manifest_with_changed_pfm['cpu_reference']['pfm_sha256'] = '0' * 64
    assert gen.portable_manifest(manifest_with_changed_pfm) != gen.portable_manifest(checked_manifest), \
        'manifest comparison must catch PFM provenance drift'

    for bad_png in (checked_png[:-1], checked_png + b'extra',
                    checked_png[:29] + bytes([checked_png[29] ^ 1]) + checked_png[30:]):
        try:
            gen.png_payload_identity(bad_png)
        except (ValueError, zlib.error):
            pass
        else:
            raise AssertionError('PNG corruption or trailing data was accepted')

    print('PASS: portable comparison ignores only deflate encoding;')
    print('      pixels, captions, metadata, source hashes and PNG integrity remain checked.')


if __name__ == '__main__':
    main()
