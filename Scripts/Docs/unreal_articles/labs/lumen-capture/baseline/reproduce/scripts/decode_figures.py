"""Decode representative raw DumpGPU binaries into readable PNG figures.

Usage:
  python decode_figures.py

Reads buffers/inventory.json and the full dump, writes buffers/decoded/*.png
and buffers/figure-manifest.json.  Every figure records its exact source
resource id, raw file, SHA-256, format, native extent, mip/slice, producing
pass and the channel mapping/normalization that was applied.

No figure is a re-render and no display PNG is used for radiometric comparison.
"""
import hashlib
import json
import math
import os
import struct
import zlib
from pathlib import Path

ROOT = Path(os.environ.get('ARDA_BUFFERS_ROOT', str(Path(__file__).resolve().parents[1])))
INV = json.loads((ROOT / 'inventory.json').read_text(encoding='utf-8'))
DUMP = Path(os.environ.get('ARDA_DUMP_DIR', INV['dump_directory']))
OUT = Path(os.environ.get('ARDA_DECODED_DIR', str(ROOT / 'decoded')))
OUT.mkdir(parents=True, exist_ok=True)
RESOURCES = {r['resource_id']: r for r in INV['resources']}
FRAME = INV['engine_frame']


def write_png(path, width, height, rows, channels=3):
    color_type = {1: 0, 3: 2, 4: 6}[channels]
    raw = bytearray()
    for row in rows:
        raw.append(0)
        raw.extend(row)
    def chunk(kind, payload):
        return (struct.pack('>I', len(payload)) + kind + payload +
                struct.pack('>I', zlib.crc32(kind + payload) & 0xFFFFFFFF))
    png = b'\x89PNG\r\n\x1a\n'
    png += chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, color_type, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(bytes(raw), 6))
    png += chunk(b'IEND', b'')
    Path(path).write_bytes(png)


def read_bytes(entry):
    return (DUMP / entry['file']).read_bytes()


def pick(name, prefer=(), slice=None, mip=0, stencil=False, id_prefix='0001'):
    candidates = [r for r in RESOURCES.values()
                  if r['name'] == name and (id_prefix is None or r['resource_id'].startswith(id_prefix))]
    if not candidates:
        candidates = [r for r in RESOURCES.values() if r['name'] == name]
    matches = []
    for r in candidates:
        for f in r['binary_files']:
            if f.get('stencil') != stencil:
                continue
            if mip is not None and f.get('mip') != mip:
                continue
            if slice is not None and f.get('slice') != slice:
                continue
            matches.append((r, f))
    if not matches:
        raise RuntimeError(f'no dumped file for {name} slice={slice} mip={mip} stencil={stencil}')
    for keyword in prefer:
        for r, f in matches:
            if keyword.lower() in (f.get('version_pass_event') or '').lower():
                return r, f
    return max(matches, key=lambda rf: (rf[1].get('version_pass_id') or -1))


def half_values(data):
    n = len(data) // 2
    return struct.unpack('<%de' % n, data)


def u11f(bits):
    exp = (bits >> 6) & 0x1F
    mant = bits & 0x3F
    if exp == 0:
        return (mant / 64.0) * (2.0 ** -14)
    if exp == 31:
        return math.inf if mant else math.nan
    return (1.0 + mant / 64.0) * (2.0 ** (exp - 15))


def u10f(bits):
    exp = (bits >> 5) & 0x1F
    mant = bits & 0x1F
    if exp == 0:
        return (mant / 32.0) * (2.0 ** -14)
    if exp == 31:
        return math.inf if mant else math.nan
    return (1.0 + mant / 32.0) * (2.0 ** (exp - 15))


def decode_r11g11b10(data, width, height):
    vals = struct.unpack('<%dI' % (width * height), data)
    rows = []
    for y in range(height):
        row = bytearray(width * 3)
        base = y * width
        for x in range(width):
            v = vals[base + x]
            r = u11f(v & 0x7FF)
            g = u11f((v >> 11) & 0x7FF)
            b = u10f((v >> 22) & 0x3FF)
            if not math.isfinite(r): r = 0.0
            if not math.isfinite(g): g = 0.0
            if not math.isfinite(b): b = 0.0
            i = x * 3
            row[i] = max(0, min(255, int(round(r * 255))))
            row[i + 1] = max(0, min(255, int(round(g * 255))))
            row[i + 2] = max(0, min(255, int(round(b * 255))))
        rows.append(bytes(row))
    return rows


def tonemap_rows(values, width, height, channels=4, exposure_scale=None):
    """Reinhard display normalization. Scale chosen from the 99th percentile."""
    lum = []
    for i in range(0, len(values), channels):
        r, g, b = values[i], values[i + 1], values[i + 2]
        if math.isfinite(r) and math.isfinite(g) and math.isfinite(b):
            lum.append(0.2126 * r + 0.7152 * g + 0.0722 * b)
    positives = sorted(v for v in lum if v > 0)
    p99 = positives[int(len(positives) * 0.99)] if positives else 1.0
    scale = exposure_scale if exposure_scale is not None else (0.8 / p99 if p99 > 0 else 1.0)
    rows = []
    for y in range(height):
        row = bytearray(width * 3)
        base = y * width * channels
        for x in range(width):
            i = base + x * channels
            r = values[i] * scale
            g = values[i + 1] * scale
            b = values[i + 2] * scale
            if not math.isfinite(r): r = 0.0
            if not math.isfinite(g): g = 0.0
            if not math.isfinite(b): b = 0.0
            row[x * 3] = max(0, min(255, int(round(255 * (r / (1 + r))))))
            row[x * 3 + 1] = max(0, min(255, int(round(255 * (g / (1 + g))))))
            row[x * 3 + 2] = max(0, min(255, int(round(255 * (b / (1 + b))))))
        rows.append(bytes(row))
    return rows, {'p99_luminance_linear': p99, 'linear_scale': scale,
                  'tone_curve': 'reinhard c/(1+c) after uniform linear scale'}


def stats(rows, width, height, channels=3):
    lums = []
    colors = set()
    nonblack = 0
    for row in rows:
        for x in range(width):
            i = x * channels
            r, g, b = row[i], row[i + 1], row[i + 2]
            l = 0.2126 * r + 0.7152 * g + 0.0722 * b
            lums.append(l)
            colors.add((r, g, b))
            if l > 8:
                nonblack += 1
    mean = sum(lums) / len(lums)
    std = (sum((v - mean) ** 2 for v in lums) / len(lums)) ** 0.5
    return {
        'mean_luminance': mean,
        'std_luminance': std,
        'min_luminance': min(lums),
        'max_luminance': max(lums),
        'unique_rgb_colors': len(colors),
        'nonblack_fraction_lum_gt_8': nonblack / len(lums),
        'blank_rejected': not (std > 1.0 and len(colors) > 1 and nonblack / len(lums) > 0.005),
    }


def make_figure(slug, name, mapping, prefer=(), slice=None, mip=0, decoder='rgb',
                stencil=False, note=''):
    r, f = pick(name, prefer=prefer, slice=slice, mip=mip, stencil=stencil)
    d = r['descriptor']
    width, height = int(d.get('ExtentX', 1)), int(d.get('ExtentY', 1))
    data = read_bytes(f)
    fmt = d.get('Format')
    extra = {}
    if decoder == 'gbuffer_normal':
        if fmt != 'PF_A2B10G10R10':
            raise RuntimeError(f'GBufferA expected PF_A2B10G10R10, got {fmt}')
        vals = struct.unpack('<%dI' % (width * height), data)
        rows = []
        lens = []
        lens_nonblack = []
        for y in range(height):
            row = bytearray(width * 3)
            base = y * width
            for x in range(width):
                v = vals[base + x]
                rr = (v & 0x3FF) / 1023.0
                gg = ((v >> 10) & 0x3FF) / 1023.0
                bb = ((v >> 20) & 0x3FF) / 1023.0
                i = x * 3
                row[i] = int(round(rr * 255))
                row[i + 1] = int(round(gg * 255))
                row[i + 2] = int(round(bb * 255))
                nx, ny, nz = rr * 2 - 1, gg * 2 - 1, bb * 2 - 1
                length = math.sqrt(nx * nx + ny * ny + nz * nz)
                lens.append(length)
                if v != 0:
                    lens_nonblack.append(length)
            rows.append(bytes(row))
        extra = {'normal_length_mean': sum(lens) / len(lens),
                 'normal_length_fraction_within_0.1_of_1': sum(1 for v in lens if abs(v - 1) < 0.1) / len(lens),
                 'normal_length_mean_nonblack': (sum(lens_nonblack) / len(lens_nonblack)) if lens_nonblack else None,
                 'normal_length_fraction_nonblack_within_0.1_of_1':
                     (sum(1 for v in lens_nonblack if abs(v - 1) < 0.1) / len(lens_nonblack)) if lens_nonblack else None,
                 'nonblack_pixel_fraction': len(lens_nonblack) / len(lens),
                 'unique_packed_values': len(set(vals)),
                 'channel_mapping': 'R=10-bit UNORM low bits, G=next 10, B=next 10, A=top 2; '
                                    'RGB stored as world normal N*0.5+0.5 (legacy GBufferA). '
                                    'A value of 0 is the cleared/background pixel, not a normal.'}
    elif decoder == 'bgra_channel':
        if fmt != 'PF_B8G8R8A8':
            raise RuntimeError(f'expected PF_B8G8R8A8, got {fmt}')
        channel = extra_channel = None
        # memory byte order is B,G,R,A
        byte_index = {'B': 0, 'G': 1, 'R': 2, 'A': 3}[mapping]
        rows = []
        mins = 255
        maxs = 0
        total = 0
        for y in range(height):
            row = bytearray(width * 3)
            for x in range(width):
                v = data[(y * width + x) * 4 + byte_index]
                row[x * 3] = v
                row[x * 3 + 1] = v
                row[x * 3 + 2] = v
                mins = min(mins, v)
                maxs = max(maxs, v)
                total += v
            rows.append(bytes(row))
        extra = {'channel': mapping, 'memory_byte_index': byte_index,
                 'value_min': mins, 'value_max': maxs, 'value_mean': total / (width * height),
                 'channel_mapping': 'PF_B8G8R8A8 memory order B,G,R,A; '
                                    'shader .z (Roughness) lands in byte 0 (B), .x Metallic in byte 2 (R)'}
    elif decoder == 'bgra_material':
        if fmt != 'PF_B8G8R8A8':
            raise RuntimeError(f'expected PF_B8G8R8A8, got {fmt}')
        rows = []
        for y in range(height):
            row = bytearray(width * 3)
            for x in range(width):
                i = (y * width + x) * 4
                bch, gch, rch, ach = data[i], data[i + 1], data[i + 2], data[i + 3]
                row[x * 3] = rch      # Metallic (.x, byte 2)
                row[x * 3 + 1] = bch  # Roughness (.z, byte 0)
                row[x * 3 + 2] = gch  # Specular (.y, byte 1)
            rows.append(bytes(row))
        extra = {'channel_mapping': 'RGB display = (Metallic=R byte2, Roughness=B byte0, Specular=G byte1); '
                                    'A=ShadingModel byte3 omitted'}
    elif decoder == 'depth':
        if fmt not in ('PF_DepthStencil', 'PF_R32_FLOAT'):
            raise RuntimeError(f'expected depth format, got {fmt}')
        if len(data) != width * height * 4:
            raise RuntimeError(f'depth size {len(data)} != {width}x{height}x4')
        vals = struct.unpack('<%df' % (width * height), data)
        finite = [v for v in vals if math.isfinite(v)]
        vmax_raw = max(finite) if finite else 0.0
        scale = (1.0 / vmax_raw) if vmax_raw > 0 else 0.0
        rows = []
        vmin, vmax = 1.0, 0.0
        vsum = 0.0
        for y in range(height):
            row = bytearray(width * 3)
            base = y * width
            for x in range(width):
                v = vals[base + x]
                if not math.isfinite(v):
                    v = 0.0
                v = max(0.0, min(1.0, v))
                vmin = min(vmin, v)
                vmax = max(vmax, v)
                vsum += v
                g = int(round(min(1.0, v * scale) * 255))
                row[x * 3] = g
                row[x * 3 + 1] = g
                row[x * 3 + 2] = g
            rows.append(bytes(row))
        extra = {'device_z_min': vmin, 'device_z_max': vmax,
                 'device_z_mean': vsum / (width * height),
                 'device_z_scale_to_255': scale,
                 'channel_mapping': 'PF_DepthStencil copied through R32_FLOAT: grayscale = raw device Z '
                                    '(UE reversed-Z; larger=nearer), scaled so the image maximum maps to 255. '
                                    'Not linearized and not exposure-scaled.',
                 'note': 'PF_X24_G8 stencil is a separate .stencil subresource when present.'}
    elif decoder == 'maskbits':
        if fmt != 'PF_R32G32_UINT':
            raise RuntimeError(f'expected PF_R32G32_UINT, got {fmt}')
        vals = struct.unpack('<%dI' % (width * height * 2), data)
        rows = []
        counts = []
        for y in range(height):
            row = bytearray(width * 3)
            base = y * width
            for x in range(width):
                lo = vals[(base + x) * 2]
                hi = vals[(base + x) * 2 + 1]
                nibbles = [(lo >> (4 * i)) & 0xF for i in range(8)] + [(hi >> (4 * i)) & 0xF for i in range(8)]
                nz = sum(1 for n in nibbles if n)
                counts.append(nz)
                v = int(round(nibbles[0] / 15.0 * 255))
                row[x * 3] = v
                row[x * 3 + 1] = v
                row[x * 3 + 2] = v
            rows.append(bytes(row))
        extra = {'nonzero_nibbles_mean': sum(counts) / len(counts),
                 'nonzero_nibbles_max': max(counts),
                 'channel_mapping': 'R32G32_UINT holds 16 packed 4-bit shadow-mask values (one per packed '
                                    'light-grid index, low nibble=first). Figure shows nibble 0 / 15 as grayscale; '
                                    'all 16 nibbles are retained in the raw file.'
                 }
    elif decoder == 'uint_low8':
        if fmt not in ('PF_R32_UINT', 'PF_R32G32_UINT'):
            raise RuntimeError(f'expected uint, got {fmt}')
        stride = 4 if fmt == 'PF_R32_UINT' else 8
        vals = struct.unpack('<%dI' % (len(data) // 4), data)
        per = stride // 4
        rows = []
        for y in range(height):
            row = bytearray(width * 3)
            base = y * width
            for x in range(width):
                v = vals[(base + x) * per] & 0xFF
                row[x * 3] = v
                row[x * 3 + 1] = v
                row[x * 3 + 2] = v
            rows.append(bytes(row))
        extra = {'channel_mapping': f'low 8 bits of the first 32-bit word ({fmt}); '
                                    'packed page-table/page-flag fields are not decoded further'}
    elif decoder == 'hdr':
        if fmt not in ('PF_FloatRGBA', 'PF_FloatRGB', 'PF_FloatR11G11B10'):
            raise RuntimeError(f'expected float format, got {fmt}')
        if fmt == 'PF_FloatRGBA':
            vals = half_values(data)
            rows, extra = tonemap_rows(vals, width, height, channels=4)
        elif fmt in ('PF_FloatRGB', 'PF_FloatR11G11B10'):
            vals = struct.unpack('<%dI' % (width * height), data)
            fv = []
            for v in vals:
                fv.extend([u11f(v & 0x7FF), u11f((v >> 11) & 0x7FF), u10f((v >> 22) & 0x3FF)])
            rows, extra = tonemap_rows(fv, width, height, channels=3)
        else:
            raise RuntimeError('unreachable')
        extra['channel_mapping'] = ('linear HDR RGB; R/G/B decoded from the stored float format. '
                                    'Display only: uniform linear scale from the image 99th percentile '
                                    'then Reinhard c/(1+c).')
        extra['radiance_note'] = ('This is a display normalization of HDR radiance, not the reference '
                                  'PNG tone curve and not a radiometric comparison.')
    elif decoder == 'hdr_sentinel':
        if fmt not in ('PF_FloatRGB', 'PF_FloatR11G11B10'):
            raise RuntimeError(f'expected FloatRGB for sentinel decode, got {fmt}')
        vals = struct.unpack('<%dI' % (width * height), data)
        fv = []
        sentinel_count = 0
        finite_count = 0
        for v in vals:
            rr = u11f(v & 0x7FF)
            gg = u11f((v >> 11) & 0x7FF)
            bb = u10f((v >> 22) & 0x3FF)
            if (not math.isfinite(rr) or rr >= 60000.0 or
                    not math.isfinite(gg) or gg >= 60000.0 or
                    not math.isfinite(bb) or bb >= 60000.0):
                fv.extend([0.0, 0.0, 0.0])
                sentinel_count += 1
            else:
                fv.extend([rr, gg, bb])
                finite_count += 1
        rows, extra = tonemap_rows(fv, width, height, channels=3)
        extra['sentinel_threshold'] = 60000.0
        extra['sentinel_pixel_fraction'] = sentinel_count / (width * height)
        extra['active_pixel_fraction'] = finite_count / (width * height)
        extra['channel_mapping'] = ('linear HDR RGB; 11/11/10 decode. Values at or above the 60000 '
                                    'sentinel (R11G11B10 finite maximum ~65024) are masked to black as '
                                    'empty tile entries; remaining values use the same percentile/Reinhard '
                                    'display normalization as other HDR figures.')
    else:
        raise RuntimeError('unknown decoder ' + decoder)

    st = stats(rows, width, height)
    st.update(extra)
    if st['blank_rejected']:
        raise RuntimeError(f'decoded figure {slug} rejected: {json.dumps(st)}')
    path = OUT / f'{slug}.png'
    write_png(path, width, height, rows)
    png_bytes = path.read_bytes()
    entry = {
        'slug': slug,
        'file': str(path),
        'png_bytes': len(png_bytes),
        'png_sha256': hashlib.sha256(png_bytes).hexdigest(),
        'source_resource_id': r['resource_id'],
        'source_name': r['name'],
        'source_format': fmt,
        'source_type': d.get('Type'),
        'source_extent': [width, height],
        'source_mip': f.get('mip'),
        'source_slice': f.get('slice'),
        'source_stencil': f.get('stencil'),
        'source_raw_file': f['file'],
        'source_raw_bytes': f['bytes'],
        'source_sha256': f['sha256'],
        'version_pass_id': f.get('version_pass_id'),
        'version_pass_event': f.get('version_pass_event'),
        'engine_frame': FRAME,
        'decoder': decoder,
        'channel_mapping': extra.get('channel_mapping', mapping),
        'display_normalization': extra.get('tone_curve', 'identity / direct channel value'),
        'stats': st,
        'note': note,
    }
    print(f'{slug}: {width}x{height} {fmt} {f["file"]}  std={st["std_luminance"]:.1f} '
          f'unique={st["unique_rgb_colors"]}')
    return entry


def main():
    figs = []
    figs.append(make_figure(
        'gbuffer-a-normal', 'GBufferA',
        'legacy GBufferA world normal, RGB = N*0.5+0.5',
        prefer=('BasePassParallel',), decoder='gbuffer_normal',
        note='Full native 1280x888 render target. Not resized to the 1280x720 screenshot.'))
    figs.append(make_figure(
        'gbuffer-b-roughness', 'GBufferB', 'B',
        prefer=('BasePassParallel',), decoder='bgra_channel',
        note='Grayscale = GBufferB shader .z (Roughness), stored in memory byte 0 of B8G8R8A8.'))
    figs.append(make_figure(
        'gbuffer-b-material', 'GBufferB', 'material',
        prefer=('BasePassParallel',), decoder='bgra_material',
        note='RGB display = (Metallic, Roughness, Specular); A/ShadingModel is retained in the raw file only.'))
    figs.append(make_figure(
        'scene-depth-device-z', 'SceneDepthZ', 'raw device Z',
        prefer=('DepthPassParallel', 'DepthPass', 'BasePassParallel'), decoder='depth',
        note='Depth is the raw reversed-Z device value; no linearization is claimed.'))
    figs.append(make_figure(
        'vsm-maskbits-nibble0', 'Shadow.Virtual.MaskBits', 'nibble 0',
        prefer=('VirtualShadowMapProjection',), decoder='maskbits',
        note='Final packed shadow-mask bits from the VSM projection pass; 16 4-bit nibbles are retained.'))
    figs.append(make_figure(
        'vsm-page-table-low8', 'Shadow.Virtual.PageTable', 'low 8 bits',
        prefer=('PropagateMappedPages',), decoder='uint_low8',
        note='Packed virtual page table; only the low 8 bits are displayed, the full words are raw.'))
    figs.append(make_figure(
        'scene-color-before-indirect', 'SceneColor', 'linear HDR',
        prefer=('RenderLight',), decoder='hdr',
        note='SceneColor version produced by the Ceiling area light pass, before DiffuseIndirectComposite.'))
    figs.append(make_figure(
        'scene-color-after-indirect', 'SceneColor', 'linear HDR',
        prefer=('DiffuseIndirectComposite',), decoder='hdr',
        note='SceneColor version produced by DiffuseIndirectComposite (screen-probe indirect applied).'))
    figs.append(make_figure(
        'screen-probe-diffuse', 'Lumen.ScreenProbeGather.DiffuseIndirect', 'linear HDR',
        prefer=('SupportAll',), slice=0, decoder='hdr',
        note='Final screen-probe integrate output for diffuse indirect (pass 181 SupportAll).'))
    figs.append(make_figure(
        'screen-probe-rough-specular', 'Lumen.ScreenProbeGather.RoughSpecularIndirect', 'linear HDR',
        prefer=('SupportAll',), slice=0, decoder='hdr',
        note='Final screen-probe integrate output for rough specular indirect (pass 181 SupportAll).'))
    figs.append(make_figure(
        'screen-probe-history', 'Lumen.ScreenProbeGather.ScreenProbeFilteredRadiance', 'linear HDR',
        prefer=('FilterRadianceWithGather',), decoder='hdr',
        note='Filtered radiance history buffer (640x672 probe grid), latest FilterRadianceWithGather pass.'))
    figs.append(make_figure(
        'reflection-trace', 'Lumen.Reflections.TraceRadiance', 'linear HDR',
        prefer=('TraceVoxel', 'TraceScreen'), slice=0, decoder='hdr',
        note='Reflection trace radiance before resolve; linear HDR array slice 0.'))
    figs.append(make_figure(
        'reflection-resolved', 'Lumen.Reflections.ResolvedSpecularIndirect', 'linear HDR, sentinel-masked',
        prefer=('ReflectionResolve',), slice=0, decoder='hdr_sentinel',
        note='Resolved specular indirect reflection output, array slice 0. Empty-tile sentinel values are '
             'masked to black and counted in the manifest; the raw file keeps them unchanged.'))

    for f in figs:
        if f['source_extent'] == [1280, 888]:
            f['native_content_rect_xywh'] = [0, 0, 1280, 720]
            f['native_padding'] = ('bottom 168 rows of the 1280x888 render target are black padding; '
                                   'the published viewport screenshot is the 1280x720 content region')

    vsm_session = None
    vsm_files = sorted((ROOT / 'runs').glob('vsm-*.json'), key=lambda p: p.stat().st_mtime)
    if vsm_files:
        vsm_session = json.loads(vsm_files[-1].read_text(encoding='utf-8'))
    pt_session = None
    pt_files = sorted((ROOT / 'runs').glob('pt1024-*.json'), key=lambda p: p.stat().st_mtime)
    if pt_files:
        pt_session = json.loads(pt_files[-1].read_text(encoding='utf-8'))

    manifest = {
        'schema': 'unreal-active-frame-figure-manifest-v1',
        'dump_directory': str(DUMP),
        'engine_frame': FRAME,
        'source_screenshot': 'Base/Screenshot.png (1280x720, display-referred, not used for radiometry)',
        'decoded_figure_count': len(figs),
        'figures': figs,
        'vsm_visualizations': vsm_session.get('vsm') if vsm_session else [],
        'vsm_lit_baseline': vsm_session.get('vsm_baseline') if vsm_session else None,
        'vsm_session': vsm_session.get('session_file') if vsm_session else None,
        'path_traced_1024': pt_session.get('pt_result') if pt_session else None,
        'path_traced_1024_session': pt_session.get('session_file') if pt_session else None,
        'limitations': [
            'All decoded figures are display normalizations of raw GPU data, not re-renders.',
            'HDR figures use a per-image 99th-percentile linear scale plus Reinhard c/(1+c); they are not '
            'comparable radiometrically with the SDR reference PNG or with each other without the raw files.',
            'Scene render targets are natively 1280x888 with 168 black padding rows; the content region is '
            'the top 1280x720. Figures are written at native 1280x888 and are not resized.',
            'GBuffer figures are valid only because r.Substrate=0 (legacy GBuffer path) was the runtime value.',
            'Lumen reflection trace/resolve resources are tile/ray buffers, not screen-space images: '
            'TraceRadiance contains only traced texels and ResolvedSpecularIndirect contains packed tiles '
            'with a finite-max sentinel for empty entries. The decoded figures preserve that layout; the '
            'composed result is visible in scene-color-after-indirect.',
        ],
    }
    (ROOT / 'figure-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print('Wrote', ROOT / 'figure-manifest.json')


if __name__ == '__main__':
    main()
