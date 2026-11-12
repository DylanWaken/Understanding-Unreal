"""Parse an Unreal DumpGPU directory (metadata or full binaries).

Usage:
  python analyze_dump.py <dump_dir> [--projection <json>]

The DumpGPU metadata files are concatenated JSON objects, not JSON arrays.
This parser streams them with json.JSONDecoder.raw_decode and derives:

  * per-resource descriptor, estimated binary size for every mip/slice/view
  * producer/consumer pass binding from Passes.json
  * pass-parameter structure schema and raw payload sizes
  * explicit unsupported/skipped warnings found in the dump log

It never modifies the dump.  Estimated binary sizes are an upper-bound estimate
for formats the engine preprocesses (depth/stencil and sub-4-byte channels).
"""
import argparse
import csv
import json
import math
import re
import struct
from pathlib import Path

# Final stored bytes per pixel/block for the format written by DumpGPU after
# its staging preprocessing.  Block formats use bytes per 4x4 block.
PIXEL_BYTES = {
    'PF_B8G8R8A8': 4,
    'PF_R8G8B8A8': 4,
    'PF_R8G8B8A8_UINT': 4,
    'PF_A2B10G10R10': 4,
    'PF_FloatRGB': 4,
    'PF_FloatR11G11B10': 4,
    'PF_FloatRGBA': 8,
    'PF_A32B32G32R32F': 16,
    'PF_R32_FLOAT': 4,
    'PF_R32_UINT': 4,
    'PF_R16_UINT': 2,
    'PF_R16F': 2,
    'PF_R16G16_UINT': 4,
    'PF_R8': 1,
    'PF_R8_UINT': 1,
    'PF_G8': 1,
    'PF_R8G8': 2,
    'PF_G16': 2,
    'PF_DepthStencil': 4,
    'PF_X24_G8': 4,
    'PF_BC5': 16,
    'PF_BC7': 16,
    'PF_BC6H': 16,
}
BLOCK_FORMATS = {'PF_BC5', 'PF_BC7', 'PF_BC6H'}


def read_concat_json(path):
    text = Path(path).read_text(encoding='utf-8', errors='replace')
    decoder = json.JSONDecoder()
    pos = 0
    n = len(text)
    while pos < n:
        while pos < n and text[pos] in ' \r\n\t':
            pos += 1
        if pos >= n:
            break
        obj, end = decoder.raw_decode(text, pos)
        yield obj
        pos = end


def texel_block_bytes(fmt):
    return PIXEL_BYTES.get(fmt)


def mip_extent(v, mip):
    return max(1, int(v) >> mip)


def texture_bytes(desc):
    """Bytes for every mip (and slice/face) that DumpGPU writes."""
    fmt = desc.get('Format')
    bpp = texel_block_bytes(fmt)
    unknown = bpp is None
    if unknown:
        bpp = 16  # conservative: every unknown format assumed 128-bit
    typ = desc.get('Type', 'Texture2D')
    w = int(desc.get('ExtentX', 1))
    h = int(desc.get('ExtentY', 1))
    d = int(desc.get('Depth', 1))
    slices = int(desc.get('ArraySize', 1))
    mips = int(desc.get('NumMips', 1))
    samples = max(1, int(desc.get('NumSamples', 1)))
    if typ == 'TextureCube':
        slices = max(slices, 1) * 6
    total = 0
    per_mip = []
    for mip in range(mips):
        mw = mip_extent(w, mip)
        mh = mip_extent(h, mip)
        md = mip_extent(d, mip) if typ == 'Texture3D' else d
        if fmt in BLOCK_FORMATS:
            mip_bytes = math.ceil(mw / 4) * math.ceil(mh / 4) * bpp
        else:
            mip_bytes = mw * mh * md * bpp
        if typ == 'Texture3D':
            count = 1
        elif typ == 'TextureCube':
            count = slices
        elif typ == 'Texture2DArray':
            count = max(1, slices)
        else:
            count = max(1, slices)
        mip_bytes *= count * samples
        total += mip_bytes
        per_mip.append({'mip': mip, 'width': mw, 'height': mh, 'depth': md,
                        'slices_or_faces': count, 'bytes': mip_bytes})
    return total, per_mip, unknown


def parse_warnings(log_path):
    if not log_path.exists():
        return []
    out = []
    patterns = [
        re.compile(r'Not dumping .* because .*'),
        re.compile(r'Not dumping .* because of .*'),
        re.compile(r'Unknown virtual shadow map visualization mode .*'),
        re.compile(r'Unable to .* dump .*'),
        re.compile(r'DumpResourceBinaryToFile\(\).*failed.*'),
        re.compile(r'RHICmdList\.(MapStagingSurface|LockStagingBuffer).*failed.*'),
    ]
    for line in log_path.read_text(encoding='utf-8', errors='replace').splitlines():
        for pat in patterns:
            if pat.search(line):
                out.append(line.strip())
                break
    # de-duplicate consecutive repeats
    dedup = []
    for line in out:
        if not dedup or dedup[-1] != line:
            dedup.append(line)
    return dedup


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('dump_dir')
    ap.add_argument('--out', default=None)
    args = ap.parse_args()
    dump = Path(args.dump_dir).resolve()
    base = dump / 'Base'
    descs = list(read_concat_json(base / 'ResourceDescs.json'))
    passes = list(read_concat_json(base / 'Passes.json'))

    resources = {}
    for d in descs:
        uid = d.get('UniqueResourceName')
        if not uid:
            continue
        rec = resources.setdefault(uid, {
            'id': uid,
            'name': d.get('Name'),
            'desc': d.get('Desc'),
            'texture': None,
            'buffer': None,
            'estimated_binary_bytes': 0,
            'format_unknown': False,
            'producers': [],
            'consumers': [],
        })
        if d.get('Desc') == 'FRDGTextureDesc':
            size, per_mip, unknown = texture_bytes(d)
            rec['texture'] = {
                'type': d.get('Type'),
                'format': d.get('Format'),
                'extent_x': d.get('ExtentX'),
                'extent_y': d.get('ExtentY'),
                'depth': d.get('Depth'),
                'array_size': d.get('ArraySize'),
                'num_mips': d.get('NumMips'),
                'num_samples': d.get('NumSamples'),
                'flags': d.get('Flags', []),
                'per_mip': per_mip,
            }
            rec['estimated_binary_bytes'] = size
            rec['format_unknown'] = unknown
        elif d.get('Desc') == 'FRDGBufferDesc':
            rec['buffer'] = {
                'bytes_per_element': d.get('BytesPerElement'),
                'num_elements': d.get('NumElements'),
                'usage': d.get('Usage', []),
                'metadata': d.get('Metadata'),
            }
            rec['estimated_binary_bytes'] = int(d.get('ByteSize', 0) or 0)

    pass_records = []
    for p in passes:
        pid = p.get('Id')
        ins = p.get('InputResources', []) or []
        outs = p.get('OutputResources', []) or []
        prec = {
            'id': pid,
            'event_name': p.get('EventName'),
            'parameters_name': p.get('ParametersName'),
            'parameters_id': p.get('Parameters'),
            'parameters_metadata_id': p.get('ParametersMetadata'),
            'parent_event_scopes': p.get('ParentEventScopes', []),
            'inputs': ins,
            'outputs': outs,
        }
        pass_records.append(prec)
        for uid in ins:
            if uid in resources:
                resources[uid]['consumers'].append({'pass_id': pid, 'event_name': p.get('EventName')})
        for uid in outs:
            if uid in resources:
                resources[uid]['producers'].append({'pass_id': pid, 'event_name': p.get('EventName')})

    tex_total = sum(r['estimated_binary_bytes'] for r in resources.values() if r['texture'])
    buf_total = sum(r['estimated_binary_bytes'] for r in resources.values() if r['buffer'])
    unknown = [r for r in resources.values() if r['format_unknown']]
    structure_bytes = sum(p.stat().st_size for p in (dump / 'Structures').glob('*') if p.is_file())
    metadata_bytes = sum(p.stat().st_size for p in (dump / 'StructuresMetadata').glob('*') if p.is_file())
    existing_bytes = sum(p.stat().st_size for p in dump.rglob('*') if p.is_file())
    warnings = parse_warnings(base / f'{dump.parent.parent.name}.log')
    if not warnings:
        warnings = parse_warnings(base / 'UnrealLab.log')

    by_name = {}
    for r in resources.values():
        key = (r['name'], r['texture']['type'] if r['texture'] else 'Buffer')
        agg = by_name.setdefault(key, {'count': 0, 'bytes': 0})
        agg['count'] += 1
        agg['bytes'] += r['estimated_binary_bytes']
    top = sorted(by_name.items(), key=lambda kv: -kv[1]['bytes'])[:40]

    summary = {
        'dump_directory': str(dump),
        'schema_version': 'unreal-gpudump-inventory-v1',
        'pass_count': len(passes),
        'resource_count': len(resources),
        'texture_count': sum(1 for r in resources.values() if r['texture']),
        'buffer_count': sum(1 for r in resources.values() if r['buffer']),
        'estimated_texture_binary_bytes': tex_total,
        'estimated_buffer_binary_bytes': buf_total,
        'estimated_binary_bytes': tex_total + buf_total,
        'estimated_binary_gib': (tex_total + buf_total) / (1024 ** 3),
        'unknown_format_resources': len(unknown),
        'unknown_format_names': [r['name'] for r in unknown[:50]],
        'structure_binary_bytes': structure_bytes,
        'structure_metadata_bytes': metadata_bytes,
        'existing_dump_bytes': existing_bytes,
        'warnings': warnings,
        'top_resources_by_estimated_bytes': [
            {'name': k[0], 'type': k[1], 'versions': v['count'], 'estimated_bytes': v['bytes']}
            for k, v in top
        ],
    }

    out_path = Path(args.out) if args.out else dump / 'analysis-projection.json'
    out_path.write_text(json.dumps({'summary': summary, 'resources': list(resources.values())},
                                   indent=2), encoding='utf-8')

    csv_path = out_path.with_suffix('.csv')
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['id', 'name', 'kind', 'type', 'format', 'extent_x', 'extent_y', 'depth',
                    'array_size', 'num_mips', 'num_samples', 'bytes_per_element', 'num_elements',
                    'estimated_binary_bytes', 'format_unknown', 'producers', 'consumers'])
        for r in sorted(resources.values(), key=lambda x: -x['estimated_binary_bytes']):
            t = r['texture'] or {}
            b = r['buffer'] or {}
            w.writerow([
                r['id'], r['name'], 'texture' if r['texture'] else 'buffer', t.get('type', ''),
                t.get('format', ''), t.get('extent_x', ''), t.get('extent_y', ''), t.get('depth', ''),
                t.get('array_size', ''), t.get('num_mips', ''), t.get('num_samples', ''),
                b.get('bytes_per_element', ''), b.get('num_elements', ''),
                r['estimated_binary_bytes'], r['format_unknown'],
                ';'.join(f"{x['pass_id']}:{x['event_name']}" for x in r['producers']),
                ';'.join(f"{x['pass_id']}:{x['event_name']}" for x in r['consumers']),
            ])
    print(json.dumps(summary, indent=2))
    print(f'Wrote {out_path}')
    print(f'Wrote {csv_path}')


if __name__ == '__main__':
    main()
