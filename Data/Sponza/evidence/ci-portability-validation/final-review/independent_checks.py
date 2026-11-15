#!/usr/bin/env python3
"""Focused negative probes for the final portability review.

This script is read-only with respect to the repository. It imports the two
generators, copies the checked-in lumen-reading outputs into temporary
directories for full ``--check`` negative runs, and writes only ``probes.json``
next to itself.
"""
from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
READING_GEN = ROOT / 'Scripts' / 'Docs' / 'unreal_articles' / 'labs' / 'lumen-reading' / 'generate.py'
COURSE_GEN = ROOT / 'Scripts' / 'Docs' / 'unreal_articles' / 'labs' / 'lumen-course' / 'generate.py'
READING_OUT = ROOT / 'Docs' / 'Unreal' / 'assets' / 'lumen-reading'
COURSE_OUT = ROOT / 'Docs' / 'Unreal' / 'assets' / 'lumen-course'
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


def load_module(name: str, path: Path):
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def parse_chunks(data: bytes):
    assert data[:8] == PNG_SIGNATURE, 'not a PNG'
    pos = 8
    chunks = []
    while pos < len(data):
        length = struct.unpack('>I', data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        end = pos + 8 + length
        assert end + 4 <= len(data), 'truncated chunk'
        chunks.append((tag, data[pos + 8:end]))
        pos = end + 4
        if tag == b'IEND':
            break
    return chunks


def build_png(chunks, idat=None):
    out = bytearray(PNG_SIGNATURE)
    wrote_idat = False
    for tag, payload in chunks:
        if tag == b'IDAT' and idat is not None:
            if wrote_idat:
                continue
            payload = idat
            wrote_idat = True
        out.extend(struct.pack('>I', len(payload)))
        out.extend(tag)
        out.extend(payload)
        out.extend(struct.pack('>I', zlib.crc32(tag + payload) & 0xFFFFFFFF))
    return bytes(out)


def replace_svg_payload(text: str, png: bytes) -> str:
    marker = 'data:image/png;base64,'
    start = text.index(marker) + len(marker)
    stop = text.index('"', start)
    return text[:start] + base64.b64encode(png).decode('ascii') + text[stop:]


def step_ulps(value: float, count: int) -> float:
    direction = math.inf if count > 0 else -math.inf
    result = value
    for _ in range(abs(count)):
        result = math.nextafter(result, direction)
    return result


def record_accept(results, name, detail):
    results.append(dict(name=name, expected='accepted', observed='accepted', ok=True, detail=detail))


def record_reject(results, name, detail, message):
    results.append(dict(name=name, expected='rejected', observed='rejected', ok=True,
                        detail=detail, message=message))


def record_difference(results, name, detail):
    results.append(dict(name=name, expected='difference detected', observed='difference detected',
                        ok=True, detail=detail))


def expect_reject(results, name, fn, detail):
    try:
        fn()
    except (AssertionError, ValueError, zlib.error) as exc:
        record_reject(results, name, detail, f'{type(exc).__name__}: {exc}')
        return
    raise AssertionError(f'{name}: expected rejection, call returned')


def expect_difference(results, name, left, right, detail):
    assert left != right, f'{name}: expected a difference'
    record_difference(results, name, detail)


def run_reading_check(gen, out_dir: Path):
    old = (gen.OUT, gen.CLEAN_PNG, gen.CPU_SVG)
    gen.OUT = out_dir
    gen.CLEAN_PNG = out_dir / 'cpu-reference-render.png'
    gen.CPU_SVG = out_dir / 'cpu-reference.svg'
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            code = gen.main(['--check'])
    finally:
        gen.OUT, gen.CLEAN_PNG, gen.CPU_SVG = old
    return code, buffer.getvalue()


def temp_reading_copy():
    holder = tempfile.TemporaryDirectory(prefix='lumen-final-review-')
    out = Path(holder.name) / 'lumen-reading'
    shutil.copytree(READING_OUT, out)
    return holder, out


def write_manifest(path: Path, mutate):
    manifest = json.loads(path.read_text(encoding='utf-8'))
    mutate(manifest)
    path.write_bytes((json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode('utf-8'))


def course_probes(course, results):
    saved = json.loads((COURSE_OUT / 'experiments.json').read_text(encoding='utf-8'))
    computed = course.experiments()
    course.check_experiment_snapshot(saved, computed)
    record_accept(results, 'course_snapshot_baseline',
                  'checked-in experiments.json compared with a fresh experiments() run')

    diffs = []
    for old, new in zip(saved['checks'], computed['checks']):
        for field in ('value', 'expected'):
            a, b = old[field], new[field]
            if isinstance(a, float) and isinstance(b, float) and a != b:
                ulps = abs(a - b) / max(math.ulp(a), math.ulp(b))
                diffs.append(dict(field=field, name=old['name'], saved=a, computed=b, ulps=ulps))
    observed_max_ulps = max((item['ulps'] for item in diffs), default=0.0)
    assert observed_max_ulps <= 16
    record_accept(results, 'course_snapshot_observed_platform_delta',
                  f'{observed_max_ulps:g} ULP maximum saved-vs-computed delta on this host')

    row_index = next(i for i, row in enumerate(saved['checks'])
                     if row['name'] == 'diffuse_uniform_estimator_mean')
    base_value = saved['checks'][row_index]['value']
    base_expected = saved['checks'][1]['expected']

    def check_with(mutate):
        snapshot = copy.deepcopy(saved)
        mutate(snapshot)
        course.check_experiment_snapshot(saved, snapshot)

    one_ulp = step_ulps(base_value, 1)
    assert one_ulp != base_value, '1 ULP setup did not change the value'
    assert abs(one_ulp - base_value) == math.ulp(base_value), '1 ULP setup was not exactly one ULP'
    check_with(lambda s: s['checks'][row_index].__setitem__('value', one_ulp))
    record_accept(results, 'course_one_ulp_value_allowed', f'{base_value!r} -> {one_ulp!r} (1 ULP)')

    sixteen_ulp = step_ulps(base_value, 16)
    assert abs(sixteen_ulp - base_value) == 16 * math.ulp(base_value), '16 ULP setup crossed a binade'
    check_with(lambda s: s['checks'][row_index].__setitem__('value', sixteen_ulp))
    record_accept(results, 'course_sixteen_ulp_value_allowed',
                  f'{base_value!r} -> {sixteen_ulp!r} (16 ULP)')

    seventeen_ulp = step_ulps(base_value, 17)
    assert abs(seventeen_ulp - base_value) > 16 * math.ulp(base_value)
    expect_reject(results, 'course_seventeen_ulp_value_rejected',
                  lambda: check_with(lambda s: s['checks'][row_index].__setitem__('value', seventeen_ulp)),
                  f'{base_value!r} -> {seventeen_ulp!r} (17 ULP)')

    expect_reject(results, 'course_one_e_minus_eight_value_rejected',
                  lambda: check_with(lambda s: s['checks'][row_index].__setitem__('value', base_value + 1e-8)),
                  f'{base_value!r} -> {base_value + 1e-8!r} (+1e-8)')
    expect_reject(results, 'course_one_e_minus_eight_expected_rejected',
                  lambda: check_with(lambda s: s['checks'][1].__setitem__('expected', base_expected + 1e-8)),
                  f'{base_expected!r} -> {base_expected + 1e-8!r} (+1e-8)')

    expect_reject(results, 'course_changed_tolerance_rejected',
                  lambda: check_with(lambda s: s['checks'][0].__setitem__(
                      'tolerance', math.nextafter(s['checks'][0]['tolerance'], math.inf))),
                  'tolerance changed by 1 ULP; tolerance must compare exactly')
    expect_reject(results, 'course_changed_input_rejected',
                  lambda: check_with(lambda s: s['ggx_example'].__setitem__(
                      'alpha', math.nextafter(s['ggx_example']['alpha'], math.inf))),
                  'ggx_example.alpha changed by 1 ULP; inputs must compare exactly')
    expect_reject(results, 'course_changed_metadata_rejected',
                  lambda: check_with(lambda s: s.__setitem__('seed', s['seed'] + 1)),
                  'top-level seed changed; metadata must compare exactly')
    expect_reject(results, 'course_changed_type_rejected',
                  lambda: check_with(lambda s: s['checks'][row_index].__setitem__('value', str(base_value))),
                  'value float -> string')
    expect_reject(results, 'course_changed_pass_type_rejected',
                  lambda: check_with(lambda s: s['checks'][0].__setitem__('passed', 1)),
                  'passed bool -> int')


def png_probes(reading, png: bytes, svg: str, results):
    chunks = parse_chunks(png)
    raw = reading.png_payload_identity(png)[1]
    original_identity = reading.png_payload_identity(png)
    original_digest = reading.png_payload_sha256(png)
    idat = b''.join(payload for tag, payload in chunks if tag == b'IDAT')

    level1 = build_png(chunks, idat=zlib.compress(raw, 1))
    level9 = build_png(chunks, idat=zlib.compress(raw, 9))
    assert level1 != level9, 'test setup: compression levels produced identical PNG bytes'
    assert reading.png_payload_identity(level1) == original_identity
    assert reading.png_payload_sha256(level1) == original_digest
    record_accept(results, 'png_recompressed_identity_allowed',
                  'different deflate streams, same non-IDAT chunks and scanlines')

    split_chunks = []
    wrote = False
    for tag, payload in chunks:
        if tag == b'IDAT':
            if wrote:
                continue
            split_chunks.append((b'IDAT', idat[:len(idat) // 2]))
            split_chunks.append((b'IDAT', idat[len(idat) // 2:]))
            wrote = True
        else:
            split_chunks.append((tag, payload))
    split_png = build_png(split_chunks)
    assert reading.png_payload_identity(split_png) == original_identity
    record_accept(results, 'png_split_idat_allowed',
                  'same zlib stream split across two IDAT chunks')

    changed_raw = bytearray(raw)
    changed_raw[100] ^= 0xFF
    changed_pixel = build_png(chunks, idat=zlib.compress(bytes(changed_raw), 9))
    expect_difference(results, 'changed_embedded_pixel_rejected',
                      reading.png_payload_identity(changed_pixel), original_identity,
                      'one decompressed scanline byte changed and CRCs rebuilt')
    assert reading.png_payload_sha256(changed_pixel) != original_digest
    assert reading.canonical_cpu_svg(replace_svg_payload(svg, changed_pixel)) != \
        reading.canonical_cpu_svg(svg), 'canonical SVG must catch an embedded pixel change'

    metadata_chunks = list(chunks)
    text_index = next(i for i, (tag, _) in enumerate(metadata_chunks) if tag == b'tEXt')
    metadata_chunks[text_index] = (b'tEXt', metadata_chunks[text_index][1] + b'-changed')
    changed_metadata = build_png(metadata_chunks)
    expect_difference(results, 'changed_png_metadata_rejected',
                      reading.png_payload_identity(changed_metadata), original_identity,
                      'tEXt payload changed and CRC rebuilt')
    assert reading.canonical_cpu_svg(replace_svg_payload(svg, changed_metadata)) != \
        reading.canonical_cpu_svg(svg), 'canonical SVG must catch changed PNG metadata'

    changed_caption = svg.replace('Educational CPU reference', 'Educational CPU reference!', 1)
    assert changed_caption != svg
    expect_difference(results, 'changed_svg_caption_rejected',
                      reading.canonical_cpu_svg(changed_caption), reading.canonical_cpu_svg(svg),
                      'native SVG caption changed')

    bad_crc = bytearray(png)
    bad_crc[16] ^= 0x01
    expect_reject(results, 'png_crc_corrupt_rejected',
                  lambda: reading.png_payload_identity(bytes(bad_crc)),
                  'IHDR data byte flipped without updating its CRC')
    expect_reject(results, 'png_truncated_rejected',
                  lambda: reading.png_payload_identity(png[:-1]),
                  'last byte removed (truncated IEND CRC)')
    expect_reject(results, 'png_trailing_after_iend_rejected',
                  lambda: reading.png_payload_identity(png + b'extra'),
                  'uncompressed bytes after IEND')
    expect_reject(results, 'png_trailing_zlib_junk_rejected',
                  lambda: reading.png_payload_identity(build_png(chunks, idat=idat + b'junk')),
                  'compressed bytes after the zlib stream inside IDAT')
    expect_reject(results, 'png_trailing_second_zlib_stream_rejected',
                  lambda: reading.png_payload_identity(build_png(chunks, idat=idat + zlib.compress(b'extra'))),
                  'a complete second zlib stream after the first inside IDAT')
    expect_reject(results, 'png_truncated_zlib_rejected',
                  lambda: reading.png_payload_identity(build_png(chunks, idat=idat[:-4])),
                  'zlib Adler-32 trailer removed')


def manifest_probes(reading, manifest: dict, results):
    source_hash = hashlib.sha256(READING_GEN.read_bytes()).hexdigest()
    assert manifest['generator']['sha256'] == source_hash, 'manifest generator hash is stale'
    record_accept(results, 'manifest_generator_hash_matches_source', source_hash)

    wrong_generator = copy.deepcopy(manifest)
    wrong_generator['generator']['sha256'] = '0' * 64
    assert reading.portable_manifest(wrong_generator) != reading.portable_manifest(manifest)
    record_reject(results, 'wrong_generator_hash_rejected_by_normalizer',
                  'portable_manifest keeps generator.sha256 exact', '')

    wrong_cpu_svg = copy.deepcopy(manifest)
    wrong_cpu_svg['cpu_reference']['sha256'] = '0' * 64
    assert reading.portable_manifest(wrong_cpu_svg) == reading.portable_manifest(manifest)
    record_accept(results, 'cpu_svg_hash_dropped_from_normalizer',
                  'verified separately against the checked-in CPU SVG; full --check probe below')

    wrong_pfm = copy.deepcopy(manifest)
    wrong_pfm['cpu_reference']['pfm_sha256'] = '0' * 64
    assert reading.portable_manifest(wrong_pfm) != reading.portable_manifest(manifest)
    record_reject(results, 'wrong_pfm_source_hash_rejected',
                  'pfm_sha256 remains in the exact manifest comparison', '')


def corrupt_png(path: Path):
    png_path = path / 'cpu-reference-render.png'
    data = bytearray(png_path.read_bytes())
    data[16] ^= 0x01
    png_path.write_bytes(bytes(data))


def change_svg(path: Path):
    svg_path = path / 'cpu-reference.svg'
    data = svg_path.read_bytes()
    changed = data.replace(b'Educational CPU reference', b'Educational CPU reference!', 1)
    assert changed != data
    svg_path.write_bytes(changed)


def full_check_probes(reading, results):
    holder, out = temp_reading_copy()
    try:
        code, output = run_reading_check(reading, out)
        assert code == 0, output
        record_accept(results, 'full_reading_check_copied_baseline',
                      'temporary copy of Docs/Unreal/assets/lumen-reading')
    finally:
        holder.cleanup()

    scenarios = [
        ('full_check_wrong_generator_hash_rejected',
         lambda path: write_manifest(path / 'manifest.json',
                                     lambda m: m['generator'].__setitem__('sha256', '0' * 64)),
         ['stale', 'manifest.json']),
        ('full_check_wrong_cpu_svg_hash_rejected',
         lambda path: write_manifest(path / 'manifest.json',
                                     lambda m: m['cpu_reference'].__setitem__('sha256', '0' * 64)),
         ['cpu-reference sha256 does not match checked-in SVG']),
        ('full_check_wrong_clean_png_hash_rejected',
         lambda path: write_manifest(path / 'manifest.json',
                                     lambda m: m['cpu_reference'].__setitem__('clean_png_sha256', '0' * 64)),
         ['clean_png_sha256 does not match checked-in PNG']),
        ('full_check_corrupt_png_rejected', corrupt_png, ['Invalid PNG CRC']),
        ('full_check_changed_svg_rejected', change_svg, ['stale', 'cpu-reference.svg']),
    ]
    for name, mutate, needles in scenarios:
        holder, out = temp_reading_copy()
        try:
            mutate(out)
            code, output = run_reading_check(reading, out)
            assert code == 1, f'{name}: expected exit 1, got {code}\n{output}'
            for needle in needles:
                assert needle in output, f'{name}: missing {needle!r} in output:\n{output}'
            record_reject(results, name, '; '.join(needles), output.strip())
        finally:
            holder.cleanup()


def byte_probes(results):
    head = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                          capture_output=True, text=True, check=True).stdout.strip()
    reviewed_dirs = ['Docs/Unreal/assets/lumen-reading', 'Docs/Unreal/assets/lumen-course']
    diff = subprocess.run(['git', '-C', str(ROOT), 'diff', '--name-status', 'HEAD', '--', *reviewed_dirs],
                          capture_output=True, text=True, check=True).stdout.strip()
    png_svg_diff = [line for line in diff.splitlines() if line.lower().endswith(('.png', '.svg'))]
    untracked = subprocess.run(['git', '-C', str(ROOT), 'status', '--porcelain=v1', '--untracked-files=all',
                                '--', *reviewed_dirs],
                               capture_output=True, text=True, check=True).stdout.strip()
    png_svg_untracked = [line for line in untracked.splitlines() if line.lower().endswith(('.png', '.svg'))]
    tracked = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '--', *reviewed_dirs],
                             capture_output=True, text=True, check=True).stdout.splitlines()
    figures = [path for path in tracked if path.lower().endswith(('.png', '.svg'))]
    mismatches = []
    for path in figures:
        current = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        head_bytes = subprocess.run(['git', '-C', str(ROOT), 'cat-file', 'blob', f'HEAD:{path}'],
                                    capture_output=True, check=True).stdout
        if current != hashlib.sha256(head_bytes).hexdigest():
            mismatches.append(path)
    assert not png_svg_diff, f'PNG/SVG diff vs HEAD in reviewed dirs: {png_svg_diff}'
    assert not png_svg_untracked, f'untracked PNG/SVG in reviewed dirs: {png_svg_untracked}'
    assert not mismatches, f'PNG/SVG bytes changed vs HEAD: {mismatches}'
    record_accept(results, 'figure_png_svg_bytes_unchanged_vs_head',
                  f'{len(figures)} tracked PNG/SVG files under lumen-reading and lumen-course compared '
                  f'with HEAD {head}; scoped diff and untracked status empty')


def main():
    reading = load_module('final_review_reading_generate', READING_GEN)
    course = load_module('final_review_course_generate', COURSE_GEN)

    png = (READING_OUT / 'cpu-reference-render.png').read_bytes()
    svg = (READING_OUT / 'cpu-reference.svg').read_text(encoding='utf-8')
    manifest = json.loads((READING_OUT / 'manifest.json').read_text(encoding='utf-8'))

    results = []
    course_probes(course, results)
    png_probes(reading, png, svg, results)
    manifest_probes(reading, manifest, results)
    full_check_probes(reading, results)
    byte_probes(results)

    head = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                          capture_output=True, text=True, check=True).stdout.strip()
    payload = dict(host=sys.platform, python=sys.version.split()[0],
                   zlib=zlib.ZLIB_RUNTIME_VERSION, head=head, results=results)
    output = Path(__file__).with_name('probes.json')
    output.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    print(f'{len(results)} probes passed on {sys.platform} Python {payload["python"]} '
          f'(zlib {payload["zlib"]}); wrote {output.name}')


if __name__ == '__main__':
    main()
