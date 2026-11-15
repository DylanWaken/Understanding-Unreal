#!/usr/bin/env python3
"""Diagnose why the lumen-reading --check result is platform dependent.

Read-only: imports the generator, rebuilds the expected artifacts, and reports
which bytes differ. It never writes generated artifacts into Docs/.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import platform
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
GENERATOR = ROOT / 'Scripts' / 'Docs' / 'unreal_articles' / 'labs' / 'lumen-reading' / 'generate.py'
OUT = ROOT / 'Docs' / 'Unreal' / 'assets' / 'lumen-reading'


def load():
    spec = importlib.util.spec_from_file_location('lumen_reading_generate_diag', GENERATOR)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def png_parts(data: bytes):
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    pos = 8
    idat = bytearray()
    chunks = []
    while pos < len(data):
        length = struct.unpack('>I', data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        chunks.append((tag.decode('latin-1'), length, sha(chunk)))
        if tag == b'IDAT':
            idat.extend(chunk)
        if tag == b'IEND':
            break
    return chunks, bytes(idat), zlib.decompress(bytes(idat))


def manifest_diff(a, b):
    """Return dotted paths whose JSON values differ."""
    diffs = []

    def walk(x, y, prefix=''):
        if type(x) is not type(y):
            diffs.append(f'{prefix} (type {type(x).__name__} != {type(y).__name__})')
            return
        if isinstance(x, dict):
            for key in sorted(set(x) | set(y)):
                if key not in x or key not in y:
                    diffs.append(f'{prefix}.{key} (missing one side)')
                else:
                    walk(x[key], y[key], f'{prefix}.{key}' if prefix else key)
        elif isinstance(x, list):
            if len(x) != len(y):
                diffs.append(f'{prefix} (length {len(x)} != {len(y)})')
            else:
                for index, (left, right) in enumerate(zip(x, y)):
                    walk(left, right, f'{prefix}[{index}]')
        elif x != y:
            diffs.append(f'{prefix}: {x!r} != {y!r}')

    walk(a, b)
    return diffs


def main():
    gen = load()
    report = {
        'platform': {
            'system': platform.system(),
            'release': platform.release(),
            'python': sys.version,
            'zlib_compile': zlib.ZLIB_VERSION,
            'zlib_runtime': zlib.ZLIB_RUNTIME_VERSION,
        },
        'artifact_sha256': {},
        'texts': {},
        'png': {},
        'manifest': {},
    }

    expected_texts = {}
    records = []
    for concept in gen.CONCEPTS:
        svg = concept['builder']()
        text = svg.render()
        expected_texts[f'diagrams/{concept["slug"]}.svg'] = text
        records.append(concept['slug'])

    cpu_text, cpu_record, clean_png = gen.build_cpu_reference()
    expected_texts['cpu-reference.svg'] = cpu_text
    # Rebuild the manifest from the same records the generator uses.
    manifest = {
        'schema': 'lumen-reading-visuals/1',
        'engine_commit': '71fe36aac5a8df5ccd66c763ffc902b29b6a9c43',
        'generator': {'path': 'Scripts/Docs/unreal_articles/labs/lumen-reading/generate.py',
                      'sha256': sha(GENERATOR.read_bytes())},
        'not_runtime_images': True,
        'diagrams': [
            {
                'slug': concept['slug'],
                'file': f'assets/lumen-reading/diagrams/{concept["slug"]}.svg',
                'sha256': sha(expected_texts[f'diagrams/{concept["slug"]}.svg'].encode('utf-8')),
                'width': None,
                'height': None,
                'title': None,
                'placement': concept['placement'],
                'evidence': concept['evidence'],
            }
            for concept in gen.CONCEPTS
        ],
        'cpu_reference': dict(
            file='assets/lumen-reading/cpu-reference.svg',
            sha256=sha(cpu_text.encode('utf-8')),
            width=760,
            height=640,
            **cpu_record,
        ),
    }
    for record, concept in zip(manifest['diagrams'], gen.CONCEPTS):
        svg = concept['builder']()
        record['width'] = svg.width
        record['height'] = svg.height
        record['title'] = svg.parts[1].split('>', 1)[1].split('<', 1)[0]
    expected_texts['manifest.json'] = json.dumps(manifest, indent=2, sort_keys=True) + '\n'

    for relative, expected in expected_texts.items():
        path = OUT / relative
        actual = path.read_text(encoding='utf-8')
        report['texts'][relative] = {
            'checked_in_sha256': sha(actual.encode('utf-8')),
            'generated_sha256': sha(expected.encode('utf-8')),
            'equal': actual == expected,
        }

    checked_png = (OUT / 'cpu-reference-render.png').read_bytes()
    report['artifact_sha256'] = {
        'checked_in_png': sha(checked_png),
        'generated_png': sha(clean_png),
        'checked_in_png_bytes': len(checked_png),
        'generated_png_bytes': len(clean_png),
    }
    checked_chunks, checked_idat, checked_raw = png_parts(checked_png)
    generated_chunks, generated_idat, generated_raw = png_parts(clean_png)
    report['png'] = {
        'checked_in_chunks': checked_chunks,
        'generated_chunks': generated_chunks,
        'checked_in_idat_sha256': sha(checked_idat),
        'generated_idat_sha256': sha(generated_idat),
        'checked_in_raw_sha256': sha(checked_raw),
        'generated_raw_sha256': sha(generated_raw),
        'raw_equal': checked_raw == generated_raw,
        'idat_equal': checked_idat == generated_idat,
        'recompress_checked_raw_level9_sha256': sha(zlib.compress(checked_raw, 9)),
        'recompress_checked_raw_level9_equals_checked_idat': zlib.compress(checked_raw, 9) == checked_idat,
    }

    checked_manifest = json.loads((OUT / 'manifest.json').read_text(encoding='utf-8'))
    report['manifest'] = {
        'equal': checked_manifest == manifest,
        'checked_in_cpu_reference': checked_manifest.get('cpu_reference'),
        'generated_cpu_reference': manifest.get('cpu_reference'),
        'checked_in_vs_checked_png_sha': checked_manifest.get('cpu_reference', {}).get('clean_png_sha256') == sha(checked_png),
        'checked_in_vs_checked_png_bytes': checked_manifest.get('cpu_reference', {}).get('clean_png_bytes') == len(checked_png),
        'checked_in_cpu_svg_sha_matches_file': checked_manifest.get('cpu_reference', {}).get('sha256') == sha((OUT / 'cpu-reference.svg').read_bytes()),
        'diffs_after_dropping_png_encoding_fields': manifest_diff(
            {k: v for k, v in checked_manifest.items() if k != 'cpu_reference'},
            {k: v for k, v in manifest.items() if k != 'cpu_reference'},
        ) + manifest_diff(
            {k: v for k, v in checked_manifest['cpu_reference'].items()
             if k not in ('sha256', 'clean_png_sha256', 'clean_png_bytes')},
            {k: v for k, v in manifest['cpu_reference'].items()
             if k not in ('sha256', 'clean_png_sha256', 'clean_png_bytes')},
        ),
    }

    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
