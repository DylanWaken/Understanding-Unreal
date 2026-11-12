"""Build the machine-readable inventory for a completed DumpGPU directory.

Usage:
  python build_inventory.py <dump_dir> [--out-dir <dir>]

Reads (never modifies) ResourceDescs.json, Passes.json, Structures/,
StructuresMetadata/ and Resources/.  Produces:

  inventory.json           per-resource descriptors, bindings, files, coverage
  inventory.csv            flat per-resource summary
  resource-files.json      per-subresource/version file names, sizes, SHA-256
  resource-files.csv       flat per-subresource summary
  pass-inventory.json      per-pass parameter payload and bindings
  pass-inventory.csv       flat per-pass summary
  missing-coverage.json    explicit omissions, skipped resources, limits

SHA-256 covers every dumped binary and every pass-parameter payload.
"""
import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_dump import read_concat_json, texture_bytes  # noqa: E402


def sha256_file(path, chunk=4 * 1024 * 1024):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


FILE_RE = re.compile(r'^(?P<uid>[0-9a-f]+)(?:\.\[(?P<slice>\d+)\])?(?:\.mip(?P<mip>\d+))?'
                     r'(?:\.(?P<stencil>stencil))?(?:\.v(?P<version>[0-9a-f]+))?\.bin$', re.IGNORECASE)


def parse_resource_filename(name):
    m = FILE_RE.match(name)
    if not m:
        return None
    uid = m.group('uid')
    sl = m.group('slice')
    mip = m.group('mip')
    ver = m.group('version')
    return {
        'resource_id': uid,
        'slice': int(sl) if sl is not None else None,
        'mip': int(mip) if mip is not None else None,
        'stencil': bool(m.group('stencil')),
        'version_id': ver if ver and ver != '0' * 20 else None,
        'raw_version_id': ver,
    }


def expected_subresources(desc):
    if desc.get('Desc') != 'FRDGTextureDesc':
        return [{'key': 'buffer', 'mip': None, 'slice': None}]
    typ = desc.get('Type', 'Texture2D')
    mips = max(1, int(desc.get('NumMips', 1)))
    slices = max(1, int(desc.get('ArraySize', 1)))
    if typ == 'TextureCube':
        slices = max(1, slices) * 6
    out = []
    is_depth_stencil = desc.get('Format') in ('PF_DepthStencil', 'PF_X24_G8')
    for mip in range(mips):
        if typ in ('Texture2DArray', 'TextureCube'):
            for sl in range(slices):
                out.append({'key': f'mip{mip}.slice{sl}', 'mip': mip, 'slice': sl})
                if is_depth_stencil:
                    out.append({'key': f'stencil.mip{mip}.slice{sl}', 'mip': mip, 'slice': sl})
        else:
            out.append({'key': f'mip{mip}', 'mip': mip, 'slice': None})
            if is_depth_stencil:
                out.append({'key': f'stencil.mip{mip}', 'mip': mip, 'slice': None})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('dump_dir')
    ap.add_argument('--out-dir', default=None)
    args = ap.parse_args()
    dump = Path(args.dump_dir).resolve()
    out_dir = Path(args.out_dir).resolve() if args.out_dir else dump
    out_dir.mkdir(parents=True, exist_ok=True)
    base = dump / 'Base'

    print(f'Reading {base / "ResourceDescs.json"}', flush=True)
    descs = list(read_concat_json(base / 'ResourceDescs.json'))
    passes = list(read_concat_json(base / 'Passes.json'))

    # Frame index and delta from the root event scope.
    engine_frame = None
    frame_delta = None
    for p in passes:
        for scope in p.get('ParentEventScopes', []):
            m = re.match(r'Frame (\d+) \(Delta=([0-9.]+)s\)', scope)
            if m:
                engine_frame = int(m.group(1))
                frame_delta = float(m.group(2))
                break
        if engine_frame is not None:
            break

    pass_by_pointer = {}
    for p in passes:
        ptr = p.get('Pointer')
        if ptr:
            pass_by_pointer[ptr.lower()] = p
            pass_by_pointer[ptr.lower()[-16:]] = p

    resources = {}
    for d in descs:
        uid = d.get('UniqueResourceName')
        if not uid:
            continue
        rec = resources.setdefault(uid, {
            'resource_id': uid,
            'name': d.get('Name'),
            'kind': 'texture' if d.get('Desc') == 'FRDGTextureDesc' else 'buffer',
            'descriptor': d,
            'descriptor_estimate_bytes': 0,
            'expected_subresources': [],
            'binary_files': [],
            'coverage': 'descriptor_only',
            'missing_subresources': [],
            'format_unknown': False,
            'producer_passes': [],
            'consumer_passes': [],
        })
        if d.get('Desc') == 'FRDGTextureDesc':
            size, per_mip, unknown = texture_bytes(d)
            rec['descriptor_estimate_bytes'] = size
            rec['format_unknown'] = unknown
            rec['expected_subresources'] = expected_subresources(d)
        else:
            rec['descriptor_estimate_bytes'] = int(d.get('ByteSize', 0) or 0)
            rec['expected_subresources'] = expected_subresources(d)

    for p in passes:
        pid = p.get('Id')
        for bound in p.get('InputResources', []) or []:
            uid = bound.split('.')[0]
            if uid in resources:
                resources[uid]['consumer_passes'].append({'pass_id': pid, 'event_name': p.get('EventName'),
                                                          'binding': bound})
        for bound in p.get('OutputResources', []) or []:
            uid = bound.split('.')[0]
            if uid in resources:
                resources[uid]['producer_passes'].append({'pass_id': pid, 'event_name': p.get('EventName'),
                                                          'binding': bound})

    print('Hashing resource binaries...', flush=True)
    files = []
    total = 0
    for i, path in enumerate(sorted((dump / 'Resources').glob('*.bin'))):
        parsed = parse_resource_filename(path.name)
        if not parsed:
            print('WARNING unparsed resource file: ' + path.name, file=sys.stderr)
            continue
        size = path.stat().st_size
        digest = sha256_file(path)
        version_pass = None
        if parsed['version_id']:
            version_pass = (pass_by_pointer.get(parsed['version_id'])
                            or pass_by_pointer.get(parsed['version_id'][-16:]))
        rec = {
            'file': 'Resources/' + path.name,
            'bytes': size,
            'sha256': digest,
            'resource_id': parsed['resource_id'],
            'mip': parsed['mip'],
            'slice': parsed['slice'],
            'stencil': parsed['stencil'],
            'version_id': parsed['version_id'],
            'version_pass_id': version_pass.get('Id') if version_pass else None,
            'version_pass_event': version_pass.get('EventName') if version_pass else None,
        }
        files.append(rec)
        total += size
        if (i + 1) % 400 == 0:
            print(f'  {i + 1} files, {total / 1024 ** 3:.2f} GiB', flush=True)
        if parsed['resource_id'] in resources:
            resources[parsed['resource_id']]['binary_files'].append(rec)
        else:
            print('WARNING resource file without descriptor: ' + path.name, file=sys.stderr)

    # Coverage: expected subresources present in at least one dumped version.
    for rec in resources.values():
        seen = set()
        for f in rec['binary_files']:
            if rec['kind'] == 'buffer':
                seen.add('buffer')
            elif f.get('stencil'):
                if f['slice'] is None:
                    seen.add(f'stencil.mip{f["mip"] if f["mip"] is not None else 0}')
                else:
                    seen.add(f'stencil.mip{f["mip"] if f["mip"] is not None else 0}.slice{f["slice"]}')
            elif f['slice'] is None:
                seen.add(f'mip{f["mip"] if f["mip"] is not None else 0}')
            else:
                seen.add(f'mip{f["mip"] if f["mip"] is not None else 0}.slice{f["slice"]}')
        expected = {s['key'] for s in rec['expected_subresources']}
        missing = sorted(expected - seen)
        rec['missing_subresources'] = missing
        rec['dumped_bytes'] = sum(f['bytes'] for f in rec['binary_files'])
        rec['dumped_file_count'] = len(rec['binary_files'])
        rec['dumped_subresource_count'] = len(seen)
        flags = rec['descriptor'].get('Flags', []) or []
        if not rec['binary_files']:
            rec['coverage'] = 'descriptor_only'
            name = rec['name'] or ''
            flag_text = ' '.join(str(f) for f in flags)
            if 'Memoryless' in flag_text:
                rec['unsupported_reason'] = 'memoryless resource; DumpGPU cannot read it back'
            elif rec['descriptor'].get('Type') in ('Texture3D', 'TextureCube'):
                rec['unsupported_reason'] = ('DumpGPU only fills subresource extents for Texture2D/2DArray; '
                                             f'{rec["descriptor"].get("Type")} descriptors are emitted but '
                                             'no binary is supported by this implementation')
            elif name.startswith('Default') or 'Dummy' in name or 'Placeholder' in name:
                rec['unsupported_reason'] = ('engine default/dummy resource; no binary emitted '
                                             '(default resources use ERDGBufferFlags::SkipTracking)')
            elif not rec['producer_passes'] and not rec['consumer_passes']:
                rec['unsupported_reason'] = ('descriptor emitted from a pass parameter binding but no tracked '
                                             'RDG InputResources/OutputResources edge and no binary; '
                                             'the binary was not emitted by this DumpGPU implementation')
            else:
                rec['unsupported_reason'] = ('no binary emitted by this DumpGPU implementation '
                                             '(external, untracked, tiled, or never viewed by a tracked RDG pass)')
        elif missing:
            rec['coverage'] = 'partial'
            rec['unsupported_reason'] = 'some expected mip/slice subresources were not emitted'
        else:
            rec['coverage'] = 'captured'
            rec['unsupported_reason'] = None

    print('Hashing pass parameter payloads...', flush=True)
    pass_records = []
    for p in passes:
        pid = p.get('Parameters')
        meta = p.get('ParametersMetadata')
        raw_path = dump / 'Structures' / f'{pid}.bin'
        meta_path = dump / 'StructuresMetadata' / f'{meta}.json'
        raw = None
        if raw_path.exists():
            raw = {'file': f'Structures/{pid}.bin', 'bytes': raw_path.stat().st_size,
                   'sha256': sha256_file(raw_path)}
        metadata = None
        if meta_path.exists():
            metadata = {'file': f'StructuresMetadata/{meta}.json', 'bytes': meta_path.stat().st_size,
                        'sha256': sha256_file(meta_path)}
        pass_records.append({
            'pass_id': p.get('Id'),
            'event_name': p.get('EventName'),
            'parameters_name': p.get('ParametersName'),
            'parameters_id': pid,
            'parameters_metadata_id': meta,
            'parameters_raw': raw,
            'parameters_metadata': metadata,
            'parent_event_scopes': p.get('ParentEventScopes', []),
            'input_resource_ids': p.get('InputResources', []) or [],
            'output_resource_ids': p.get('OutputResources', []) or [],
        })

    # Warnings and explicit omissions.
    log_path = base / 'UnrealLab.log'
    warnings = []
    if log_path.exists():
        for line in log_path.read_text(encoding='utf-8', errors='replace').splitlines():
            if 'Not dumping' in line or 'Unable to dump' in line or 'DumpResourceBinaryToFile()' in line:
                warnings.append(line.strip())
    dedup_warnings = []
    for w in warnings:
        if w not in dedup_warnings:
            dedup_warnings.append(w)

    descriptor_only = [r for r in resources.values() if r['coverage'] == 'descriptor_only']
    partial = [r for r in resources.values() if r['coverage'] == 'partial']
    tex3d = [r for r in resources.values() if r['descriptor'].get('Type') == 'Texture3D']
    cubetex = [r for r in resources.values() if r['descriptor'].get('Type') == 'TextureCube']
    missing_pass_params = [p for p in pass_records if not p['parameters_raw']]
    missing_pass_meta = [p for p in pass_records if not p['parameters_metadata']]

    omissions = {
        'schema': 'unreal-active-frame-missing-coverage-v1',
        'dump_directory': str(dump),
        'engine_frame': engine_frame,
        'frame_delta_seconds': frame_delta,
        'skipped_resource_warnings': dedup_warnings,
        'resources_descriptor_only': [
            {'resource_id': r['resource_id'], 'name': r['name'], 'kind': r['kind'],
             'reason': r['unsupported_reason'],
             'descriptor_estimate_bytes': r['descriptor_estimate_bytes']}
            for r in descriptor_only
        ],
        'resources_partial': [
            {'resource_id': r['resource_id'], 'name': r['name'], 'kind': r['kind'],
             'missing_subresources': r['missing_subresources'],
             'dumped_file_count': r['dumped_file_count'],
             'reason': r['unsupported_reason']}
            for r in partial
        ],
        'passes_without_parameter_payload': missing_pass_params,
        'passes_without_parameter_metadata': missing_pass_meta,
        'structural_limits': [
            {
                'limit': 'texture3d_unsupported',
                'count': len(tex3d),
                'detail': 'All Texture3D resources are descriptor-only in this DumpGPU implementation. '
                          'TranslateSubresourceDumpDesc only fills SubResourceExtent for Desc.IsTexture2D(), '
                          'so Texture3D gets ByteSize==0 and IsDumpSupported() returns false. This includes '
                          'Lumen translucency volumes, radiance-cache indirection and 3D LUTs. Descriptors '
                          'are retained in inventory.json; their bytes are not in Resources/.',
            },
            {
                'limit': 'texturecube_unsupported',
                'count': len(cubetex),
                'detail': 'All TextureCube resources are descriptor-only for the same IsTexture2D() gate. '
                          'The descriptors are retained; cube face bytes are not emitted.',
            },
            {
                'limit': 'shader_local_registers',
                'detail': 'DumpGPU captures resources and pass parameter payloads, not registers or '
                          'values local to a shader invocation. Shader source mapping is evidence only, '
                          'not a memory capture.',
            },
            {
                'limit': 'rhi_descriptor_views',
                'detail': 'This DumpGPU implementation names RDG resources and emits per-mip/per-slice '
                          'subresource binaries, but does not emit RHI descriptor heap entries, SRV/UAV '
                          'objects or the full view descriptor table. Passes.json names resource versions '
                          'bound to each pass; it is not a full descriptor dump.',
            },
            {
                'limit': 'non_rdg_resources',
                'detail': 'Only resources referenced by an RDG pass can be discovered. RHI-external '
                          'resources, resources never viewed by a pass, swapchain/backbuffer state and '
                          'driver-private allocations are not listed.',
            },
            {
                'limit': 'skip_tracking_defaults',
                'detail': 'DefaultByteAddressBuffer, DefaultStructuredBuffer, DefaultBuffer and '
                          'VirtualTexture_FeedbackBuffer are skipped with ERDGBufferFlags::SkipTracking; '
                          'their bytes are not in the dump. Exact warning lines are preserved above.',
            },
            {
                'limit': 'redump_inputs_off',
                'detail': 'r.DumpGPU.RedumpInputs=0. When a resource is modified in place, later passes '
                          'link to the one dumped producer version instead of a per-read duplicate. '
                          'Passes.json preserves the version linkage; the bytes are the recorded version.',
            },
            {
                'limit': 'depth_stencil_preprocessed',
                'detail': 'Depth/stencil and sub-4-byte channel textures are copied through a staging '
                          'preprocess. Stored bytes are the postprocessed format recorded in the '
                          'descriptor/analysis, not the native RHI layout. PF_X24_G8 stencil is emitted '
                          'as a separate subresource when present.',
            },
            {
                'limit': 'uniform_buffer_content_map',
                'detail': 'Referenced uniform-buffer contents are present only because the read-only CVar '
                          'r.RHI.UniformBufferContentMap.Enable=1 was set at process start via an ini '
                          'override. Its capture-time value is in Base/ConsoleVariables.csv.',
            },
            {
                'limit': 'upload_and_explore_disabled',
                'detail': 'r.DumpGPU.Upload=0 and r.DumpGPU.Explore=0; nothing was uploaded and no file '
                          'explorer was opened. The raw directory is local only.',
            },
        ],
        'counts': {
            'resources_total': len(resources),
            'resources_captured': sum(1 for r in resources.values() if r['coverage'] == 'captured'),
            'resources_partial': len(partial),
            'resources_descriptor_only': len(descriptor_only),
            'texture3d_total': len(tex3d),
            'texture3d_captured': sum(1 for r in tex3d if r['coverage'] == 'captured'),
            'texturecube_total': len(cubetex),
            'texturecube_captured': sum(1 for r in cubetex if r['coverage'] == 'captured'),
            'resource_binary_files': len(files),
            'passes_total': len(passes),
            'passes_without_parameter_payload': len(missing_pass_params),
            'skipped_warning_lines': len(dedup_warnings),
        },
    }

    inventory = {
        'schema': 'unreal-active-frame-resource-inventory-v1',
        'dump_directory': str(dump),
        'engine_frame': engine_frame,
        'frame_delta_seconds': frame_delta,
        'pass_count': len(passes),
        'resource_count': len(resources),
        'resource_file_count': len(files),
        'resource_bytes_total': total,
        'resources': sorted(resources.values(), key=lambda r: (r['kind'], r['name'] or '', r['resource_id'])),
    }

    (out_dir / 'inventory.json').write_text(json.dumps(inventory, indent=2, default=str), encoding='utf-8')
    (out_dir / 'resource-files.json').write_text(json.dumps({
        'schema': 'unreal-active-frame-resource-files-v1',
        'dump_directory': str(dump),
        'engine_frame': engine_frame,
        'file_count': len(files),
        'bytes_total': total,
        'files': files,
    }, indent=2), encoding='utf-8')
    (out_dir / 'pass-inventory.json').write_text(json.dumps({
        'schema': 'unreal-active-frame-pass-inventory-v1',
        'dump_directory': str(dump),
        'engine_frame': engine_frame,
        'pass_count': len(pass_records),
        'passes': pass_records,
    }, indent=2), encoding='utf-8')
    (out_dir / 'missing-coverage.json').write_text(json.dumps(omissions, indent=2), encoding='utf-8')

    with open(out_dir / 'inventory.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['resource_id', 'name', 'kind', 'type', 'format', 'extent_x', 'extent_y', 'depth',
                    'array_size', 'num_mips', 'num_samples', 'bytes_per_element', 'num_elements',
                    'descriptor_estimate_bytes', 'dumped_bytes', 'dumped_file_count',
                    'expected_subresources', 'dumped_subresources', 'coverage', 'format_unknown',
                    'missing_subresources', 'producer_passes', 'consumer_passes', 'unsupported_reason'])
        for r in inventory['resources']:
            d = r['descriptor']
            w.writerow([
                r['resource_id'], r['name'], r['kind'], d.get('Type', ''), d.get('Format', ''),
                d.get('ExtentX', ''), d.get('ExtentY', ''), d.get('Depth', ''), d.get('ArraySize', ''),
                d.get('NumMips', ''), d.get('NumSamples', ''), d.get('BytesPerElement', ''),
                d.get('NumElements', ''), r['descriptor_estimate_bytes'], r['dumped_bytes'],
                r['dumped_file_count'], len(r['expected_subresources']), r['dumped_subresource_count'],
                r['coverage'], r['format_unknown'], ';'.join(r['missing_subresources']),
                ';'.join(f"{x['pass_id']}:{x['event_name']}" for x in r['producer_passes']),
                ';'.join(f"{x['pass_id']}:{x['event_name']}" for x in r['consumer_passes']),
                r['unsupported_reason'] or '',
            ])
    with open(out_dir / 'resource-files.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['resource_id', 'file', 'bytes', 'sha256', 'mip', 'slice', 'stencil', 'version_id',
                    'version_pass_id', 'version_pass_event'])
        for r in files:
            w.writerow([r['resource_id'], r['file'], r['bytes'], r['sha256'], r['mip'], r['slice'],
                        r['stencil'], r['version_id'], r['version_pass_id'], r['version_pass_event']])
    with open(out_dir / 'pass-inventory.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['pass_id', 'event_name', 'parameters_name', 'parameters_raw_file',
                    'parameters_raw_bytes', 'parameters_metadata_file', 'input_resource_count',
                    'output_resource_count', 'parent_scopes'])
        for p in pass_records:
            w.writerow([
                p['pass_id'], p['event_name'], p['parameters_name'],
                p['parameters_raw']['file'] if p['parameters_raw'] else '',
                p['parameters_raw']['bytes'] if p['parameters_raw'] else '',
                p['parameters_metadata']['file'] if p['parameters_metadata'] else '',
                len(p['input_resource_ids']), len(p['output_resource_ids']),
                '|'.join(p['parent_event_scopes']),
            ])

    summary = {
        'engine_frame': engine_frame,
        'frame_delta_seconds': frame_delta,
        'resources_total': len(resources),
        'resources_captured': omissions['counts']['resources_captured'],
        'resources_partial': len(partial),
        'resources_descriptor_only': len(descriptor_only),
        'resource_file_count': len(files),
        'resource_bytes_total': total,
        'resource_bytes_gib': round(total / 1024 ** 3, 3),
        'passes_total': len(passes),
        'passes_without_parameter_payload': len(missing_pass_params),
        'skipped_warning_lines': len(dedup_warnings),
    }
    (out_dir / 'metadata' / 'summary-full.json').parent.mkdir(parents=True, exist_ok=True)
    (out_dir / 'metadata' / 'summary-full.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))
    print(f'Wrote inventory to {out_dir}')


if __name__ == '__main__':
    main()
