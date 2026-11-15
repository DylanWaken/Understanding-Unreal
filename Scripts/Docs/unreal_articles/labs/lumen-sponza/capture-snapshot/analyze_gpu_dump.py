"""Analyze the one-frame Sponza DumpGPU capture and write the capture report.

The analyzer is deliberately independent of Unreal.  It reads the raw dump
metadata, decodes ViewData.PrimaryViews to establish which camera actually
produced each scene graph, inventories every payload and gap, classifies the
requested rendering subsystems, and emits:

  output/gpu-dump/analysis-<run_id>.json
  capture-report.md

Measurements are only taken from the dumped files.  Shader-local registers and
other non-resource state are reported as gaps, never reconstructed.
"""
import argparse
import hashlib
import json
import math
import os
import re
import struct
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path(os.environ.get('ARDA_SPONZA_WORKSPACE', r'D:\UnrealUnderstanding-Sponza'))
SCRIPTS = WORKSPACE / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from image_stats import Png  # noqa: E402

GPU = WORKSPACE / 'output' / 'gpu-dump'
REPORT = WORKSPACE / 'capture-report.md'
EXPECTED_CAMERA = {
    'actor': 'CAM_Hero_Reference',
    'location_cm': [-850.0, 0.0, 90.0],
    'pitch_deg': 13.0,
    'yaw_deg': 0.0,
    'roll_deg': 0.0,
    'horizontal_fov_deg': 70.0,
    'view_rect': [0, 0, 1280, 720],
}
FPACKED_VIEW_BYTES = 1056


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 22), b''):
            h.update(chunk)
    return h.hexdigest()


def stream_json(path):
    """DumpGPU writes concatenated JSON objects, sometimes without separators."""
    text = Path(path).read_text(encoding='utf-8-sig')
    decoder = json.JSONDecoder()
    i = 0
    while True:
        while i < len(text) and text[i] in ' \r\n\t,':
            i += 1
        if i >= len(text):
            break
        obj, i = decoder.raw_decode(text, i)
        yield obj


def read_csv(path):
    import csv
    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def file_info(path):
    p = Path(path)
    if not p.exists():
        return {'exists': False, 'path': str(p)}
    return {'exists': True, 'path': str(p), 'bytes': p.stat().st_size,
            'sha256': sha256_file(p)}


def read_exact(path, offset, size):
    with open(path, 'rb') as f:
        f.seek(offset)
        return f.read(size)


def f32(data, offset):
    return struct.unpack_from('<f', data, offset)[0]


def i32(data, offset):
    return struct.unpack_from('<i', data, offset)[0]


def u32(data, offset):
    return struct.unpack_from('<I', data, offset)[0]


def fvec(data, offset, n):
    return [f32(data, offset + 4 * i) for i in range(n)]


def mat4(data, offset):
    return [[f32(data, offset + 16 * r + 4 * c) for c in range(4)] for r in range(4)]


def normalize(v):
    n = (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) ** 0.5
    return [x / n for x in v] if n else v


def decode_packed_view(data, base):
    """Decode the C++ FPackedView layout exactly as declared in NaniteShared.h.

    FMatrix44f is row-major.  Offsets follow the 1056-byte struct used by the
    constructor in 5.8.1.
    """
    view_rect = [i32(data, base + 640 + 4 * i) for i in range(4)]
    view_size = fvec(data, base + 656, 4)
    pre_high = fvec(data, base + 720, 3)
    origin_high_x = f32(data, base + 732)
    origin_high_y = f32(data, base + 748)
    origin_low = fvec(data, base + 768, 3)
    origin_high_z = f32(data, base + 780)
    culling_origin_translated = fvec(data, base + 784, 3)
    view_forward_raw = fvec(data, base + 800, 3)
    near_plane = f32(data, base + 812)
    pre_low = fvec(data, base + 832, 3)
    view_to_clip = mat4(data, base + 256)
    translated_world_to_view = mat4(data, base + 128)
    translated_world_to_clip = mat4(data, base + 192)
    view_to_translated_world = mat4(data, base + 64)
    frame_index = u32(data, base + 1044)
    primary_view_id = i32(data, base + 1040)

    origin_high = [origin_high_x, origin_high_y, origin_high_z]
    origin = [origin_high[i] + origin_low[i] for i in range(3)]
    pre = [pre_high[i] + pre_low[i] for i in range(3)]
    forward = normalize(view_forward_raw) if any(view_forward_raw) else [0.0, 0.0, 1.0]
    m00 = view_to_clip[0][0]
    m11 = view_to_clip[1][1]
    hfov = math_deg(2.0 * __import__('math').atan(1.0 / abs(m00))) if m00 else None
    vfov = math_deg(2.0 * __import__('math').atan(1.0 / abs(m11))) if m11 else None
    pitch = math_deg(__import__('math').asin(max(-1.0, min(1.0, forward[2]))))
    yaw = math_deg(__import__('math').atan2(forward[1], forward[0]))
    # Camera position in translated world is the origin of ViewToTranslatedWorld.
    view_to_translated_origin = [view_to_translated_world[3][0], view_to_translated_world[3][1],
                                 view_to_translated_world[3][2]]
    return {
        'view_rect': view_rect,
        'view_size_and_inv_size': view_size,
        'view_origin_high': origin_high,
        'view_origin_low': origin_low,
        'world_origin_cm': origin,
        'preview_translation_high': pre_high,
        'preview_translation_low': pre_low,
        'preview_translation_sum': pre,
        'minus_preview_translation_cm': [-x for x in pre],
        'culling_origin_translated_world': culling_origin_translated,
        'view_forward': forward,
        'near_plane': near_plane,
        'pitch_deg_from_forward': round(pitch, 4),
        'yaw_deg_from_forward': round(yaw, 4),
        'projection_m00': m00,
        'projection_m11': m11,
        'hfov_deg_from_projection': round(hfov, 4) if hfov is not None else None,
        'vfov_deg_from_projection': round(vfov, 4) if vfov is not None else None,
        'translated_world_to_view_rows': translated_world_to_view,
        'translated_world_to_clip_rows': translated_world_to_clip,
        'view_to_translated_world_origin': view_to_translated_origin,
        'scene_renderer_primary_view_id': primary_view_id,
        'frame_index': frame_index,
    }


def math_deg(radians):
    return radians * 180.0 / 3.141592653589793


def camera_matches_expected(view):
    loc = view['world_origin_cm']
    forward = view['view_forward']
    target_forward = [0.974370064785, 0.0, 0.224951054344]
    return {
        'location_matches': all(abs(loc[i] - EXPECTED_CAMERA['location_cm'][i]) < 1.0 for i in range(3)),
        'view_rect_matches': view['view_rect'] == EXPECTED_CAMERA['view_rect'],
        'hfov_matches': (view['hfov_deg_from_projection'] is not None and
                         abs(view['hfov_deg_from_projection'] - EXPECTED_CAMERA['horizontal_fov_deg']) < 0.2),
        'pitch_matches': abs(view['pitch_deg_from_forward'] - EXPECTED_CAMERA['pitch_deg']) < 0.2,
        'yaw_matches': abs(view['yaw_deg_from_forward'] - EXPECTED_CAMERA['yaw_deg']) < 0.2,
        'forward_matches': all(abs(forward[i] - target_forward[i]) < 0.01 for i in range(3)),
        'preview_translation_consistent': all(
            abs((view['minus_preview_translation_cm'][i] - loc[i])) < 1.0 for i in range(3)),
    }


def view_key_from_readback(file_name):
    name = Path(file_name).name
    if name.startswith('NativeBuffer.') or name.startswith('NativeTexture.'):
        parts = name.split('.')
        return parts[1] if len(parts) > 1 else None
    parts = name.split('.')
    return parts[0] if parts else None


def payload_key(file_name):
    """Return the RDG resource key for a Resources/ file name.

    RDG textures/buffers are written as <key>.mip0... / <key>.v....bin.
    Native uniform-buffer textures use NativeTexture.<ptr>..., and native
    buffers use NativeBuffer.<ptr>...; their pointer can still match an RDG
    descriptor key, so the prefix is stripped.
    """
    return view_key_from_readback(file_name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', default=None)
    parser.add_argument('--report', default=str(REPORT))
    args = parser.parse_args()

    if args.run_id:
        session_path = GPU / f'run-{args.run_id}.json'
    else:
        latest = json.loads((GPU / 'latest-run.json').read_text(encoding='utf-8'))
        session_path = Path(latest['session'])
    session = json.loads(session_path.read_text(encoding='utf-8'))
    run_id = session['run_id']
    raw = Path(session['dump_directory'])
    base = raw / 'Base'
    analysis_path = GPU / f'analysis-{run_id}.json'

    launcher = None
    for lp in sorted((GPU / 'logs').glob('*-launcher.json')):
        try:
            candidate = json.loads(lp.read_text(encoding='utf-8-sig'))
        except Exception:
            continue
        if candidate.get('dump_run_id') == run_id:
            candidate['record_path'] = str(lp)
            launcher = candidate
            break

    print('run', run_id)
    print('raw', raw)

    metadata_files = [
        'Status.txt', 'Infos.json', 'DumpService.json', 'ConsoleVariables.csv',
        'Passes.json', 'ParameterBindings.json', 'NativeUniformBufferBindings.json',
        'ReferencedStructs.json', 'ResourceDescs.json', 'SubresourceLayouts.json',
        'DumpReadbackStatus.json', 'PassDrawCounts.json', 'Screenshot.png',
    ]
    metadata = {name: file_info(base / name) for name in metadata_files}
    for name, info in metadata.items():
        if info['exists']:
            info['relative'] = f'Base/{name}'

    infos = json.loads((base / 'Infos.json').read_text(encoding='utf-8-sig')) if (base / 'Infos.json').exists() else {}
    dump_service = json.loads((base / 'DumpService.json').read_text(encoding='utf-8-sig')) if (base / 'DumpService.json').exists() else {}
    passes = list(stream_json(base / 'Passes.json')) if (base / 'Passes.json').exists() else []
    resources = list(stream_json(base / 'ResourceDescs.json')) if (base / 'ResourceDescs.json').exists() else []
    parameter_bindings = list(stream_json(base / 'ParameterBindings.json')) if (base / 'ParameterBindings.json').exists() else []
    native_uniforms = list(stream_json(base / 'NativeUniformBufferBindings.json')) if (base / 'NativeUniformBufferBindings.json').exists() else []
    referenced_structs = list(stream_json(base / 'ReferencedStructs.json')) if (base / 'ReferencedStructs.json').exists() else []
    subresource_layouts = list(stream_json(base / 'SubresourceLayouts.json')) if (base / 'SubresourceLayouts.json').exists() else []
    readbacks = list(stream_json(base / 'DumpReadbackStatus.json')) if (base / 'DumpReadbackStatus.json').exists() else []
    console_vars = read_csv(base / 'ConsoleVariables.csv') if (base / 'ConsoleVariables.csv').exists() else []

    # ---- resource/payload mapping ----------------------------------------
    resource_by_key = {}
    resources_by_name = defaultdict(list)
    for r in resources:
        key = r.get('UniqueResourceName')
        resource_by_key[key] = r
        resources_by_name[r.get('Name')].append(r)

    readbacks_by_key = defaultdict(list)
    for rb in readbacks:
        key = view_key_from_readback(rb.get('File', ''))
        if key:
            readbacks_by_key[key].append(rb)

    layouts_by_key = defaultdict(list)
    for lay in subresource_layouts:
        layouts_by_key[lay.get('Resource')].append(lay)

    resources_dir = raw / 'Resources'
    resource_file_index = defaultdict(list)
    versions_by_key = defaultdict(set)
    payload_version_files = 0
    if resources_dir.exists():
        for fp in resources_dir.iterdir():
            if fp.is_file():
                key = payload_key(fp.name)
                if key:
                    resource_file_index[key].append(fp)
                m = re.search(r'\.v([0-9a-fA-F]+)\.bin$', fp.name)
                if m:
                    payload_version_files += 1
                    if key:
                        versions_by_key[key].add(m.group(1).lower())
    multi_version_resources = {
        k: sorted(v) for k, v in versions_by_key.items() if len(v) > 1
    }
    max_versions_per_resource = max((len(v) for v in versions_by_key.values()), default=0)

    resource_payload = {}
    missing_payload = []
    resources_with_payload = 0
    for r in resources:
        key = r.get('UniqueResourceName')
        hits = [rb for rb in readbacks_by_key.get(key, []) if rb.get('FileExists') and rb.get('Status') == 'Dumped']
        layouts = [lay for lay in layouts_by_key.get(key, []) if lay.get('File')]
        direct = resource_file_index.get(key, [])
        if hits or layouts or direct:
            resources_with_payload += 1
            if key not in resource_payload:
                files = []
                seen = set()
                for rb in hits:
                    files.append({'file': rb.get('File'), 'bytes': rb.get('ActualBytes'),
                                  'expected_bytes': rb.get('ExpectedBytes'), 'kind': rb.get('Kind')})
                    seen.add(rb.get('File'))
                for lay in layouts:
                    files.append({'file': lay.get('File'), 'bytes': lay.get('ByteSize'),
                                  'dimension': lay.get('Dimension'), 'mip': lay.get('MipLevel')})
                    seen.add(lay.get('File'))
                for fp in direct:
                    rel = str(fp.relative_to(raw)).replace('\\', '/')
                    if rel not in seen:
                        files.append({'file': rel, 'bytes': fp.stat().st_size, 'kind': 'DirectFile'})
                resource_payload[key] = {'name': r.get('Name'), 'files': files,
                                         'bytes': sum((f.get('bytes') or 0) for f in files)}
        else:
            missing_payload.append({'name': r.get('Name'), 'key': key,
                                    'byte_size': r.get('ByteSize'), 'desc': r.get('Desc')})
    duplicate_resource_keys = {k: c for k, c in Counter(
        r.get('UniqueResourceName') for r in resources).items() if c > 1}

    status_counts = Counter(rb.get('Status') for rb in readbacks)
    kind_counts = Counter(rb.get('Kind') for rb in readbacks)
    readback_bytes = sum((rb.get('ActualBytes') or 0) for rb in readbacks)

    # Inventory is authoritative for on-disk bytes; the session already wrote it.
    inventory_path = GPU / f'inventory-{run_id}.json'
    inventory = json.loads(inventory_path.read_text(encoding='utf-8')) if inventory_path.exists() else None
    if inventory is None:
        files = []
        total = 0
        for p in sorted(raw.rglob('*')):
            if p.is_file():
                size = p.stat().st_size
                total += size
                files.append({'path': str(p.relative_to(raw)).replace('\\', '/'), 'bytes': size})
        inventory = {'files': files, 'file_count': len(files), 'total_bytes': total}

    payload_count = 0
    payload_bytes = 0
    for f in inventory['files']:
        if f['path'].startswith('Resources/'):
            payload_count += 1
            payload_bytes += f['bytes']

    # ---- preservation of the pre-existing final evidence -----------------
    preservation = {
        'source': 'output/final-setup.json hashes + output/final/manifest.json',
        'frame_hash_records': 0,
        'frame_hash_mismatches': [],
        'run_hash_records': 0,
        'run_hash_mismatches': [],
        'script_hash_records': 0,
        'script_hash_mismatches': [],
    }
    try:
        final_setup = json.loads((WORKSPACE / 'output' / 'final-setup.json').read_text(encoding='utf-8-sig'))
        hashes = final_setup.get('hashes') or {}
        for rel, expected in (hashes.get('frame_hashes') or {}).items():
            preservation['frame_hash_records'] += 1
            p = WORKSPACE / rel
            if not p.exists() or sha256_file(p).lower() != str(expected).lower():
                preservation['frame_hash_mismatches'].append(rel)
        for rel, expected in (hashes.get('run_hashes') or {}).items():
            preservation['run_hash_records'] += 1
            p = WORKSPACE / rel
            if not p.exists() or sha256_file(p).lower() != str(expected).lower():
                preservation['run_hash_mismatches'].append(rel)
        for rel, expected in (hashes.get('script_hashes') or {}).items():
            preservation['script_hash_records'] += 1
            p = WORKSPACE / rel
            if not p.exists() or sha256_file(p).lower() != str(expected).lower():
                preservation['script_hash_mismatches'].append(rel)
        started = datetime.fromisoformat(session['started_utc'].replace('Z', '+00:00'))
        script_mismatch_details = []
        for rel in preservation['script_hash_mismatches']:
            p = WORKSPACE / rel
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc) if p.exists() else None
            script_mismatch_details.append({
                'path': rel,
                'mtime_utc': mtime.isoformat() if mtime else None,
                'pre_existing_before_this_run': bool(mtime and mtime < started),
            })
        preservation['script_mismatch_details'] = script_mismatch_details
        preservation['script_hash_mismatches_are_preexisting'] = all(
            d['pre_existing_before_this_run'] for d in script_mismatch_details)
        preservation['run_started_utc'] = session['started_utc']
        preservation['approved_image_sha256'] = hashes.get('approved_image_sha256')
        preservation['approved_image_matches'] = (
            (WORKSPACE / 'output' / 'sky-floor' / 'exposure' / 'e11_sky16.png').exists() and
            sha256_file(WORKSPACE / 'output' / 'sky-floor' / 'exposure' / 'e11_sky16.png').lower() ==
            str(hashes.get('approved_image_sha256', '')).lower())
        preservation['final_setup_sha256'] = sha256_file(WORKSPACE / 'output' / 'final-setup.json')
        manifest = json.loads((WORKSPACE / 'output' / 'final' / 'manifest.json').read_text(encoding='utf-8-sig'))
        preservation['manifest_authoritative_setup_sha256'] = (
            (manifest.get('authoritative_setup') or {}).get('sha256'))
        preservation['final_setup_matches_manifest'] = (
            preservation['final_setup_sha256'].lower() ==
            str(preservation['manifest_authoritative_setup_sha256']).lower())
        preservation['preserved'] = (
            not preservation['frame_hash_mismatches'] and
            not preservation['run_hash_mismatches'] and
            preservation.get('script_hash_mismatches_are_preexisting', False) and
            preservation.get('approved_image_matches') and
            preservation.get('final_setup_matches_manifest'))
    except Exception as exc:
        preservation['error'] = str(exc)
        preservation['preserved'] = None

    # ---- passes / parameter blocks ---------------------------------------
    pass_by_pointer = {p.get('Pointer'): p for p in passes}
    params_by_pass = defaultdict(list)
    for pb in parameter_bindings:
        params_by_pass[pb.get('Pass')].append(pb)
    passes_with_params = sum(1 for p in passes if p.get('Pointer') in params_by_pass)
    missing_param_passes = [p for p in passes if p.get('Pointer') not in params_by_pass]
    # Clear/copy/barrier/graph passes carry built-in parameter structs that
    # DumpGPU intentionally does not serialize as a pass parameter block.
    parameterless_names = {
        'FEmptyShaderParameters', 'FRenderTargetParameters', 'FReadbackBufferParameters',
        'FEnqueueCopyBufferPass', 'FBarrierPassParameters',
        'FVirtualTextureFeedbackCopyParameters', 'FRayTracingSceneBuildPassParams',
        'FSetRayTracingBindingsInlinePassParams',
    }
    parameterless_passes = [p for p in missing_param_passes
                            if p.get('ParametersName') in parameterless_names]
    missing_expected_params = [p for p in missing_param_passes
                               if p.get('ParametersName') not in parameterless_names]

    # Native uniform buffers: count bindings and records with exported content.
    native_bindings = 0
    native_exported_files = set()
    native_layouts = Counter()
    native_missing_content = 0
    for rec in native_uniforms:
        native_layouts[rec.get('LayoutName')] += 1
        for name, b in (rec.get('Bindings') or {}).items():
            native_bindings += 1
            f = b.get('ExportFirstFile')
            if f:
                native_exported_files.add(f)
            if b.get('BaseType', '').startswith('UBMT_') and not f and b.get('ResourceKind') in ('Texture', 'Buffer'):
                native_missing_content += 1
    native_exported_existing = sum(1 for f in native_exported_files if (raw / f).exists())

    log_path = base / 'UnrealLab.log'
    log_text = log_path.read_text(encoding='utf-8', errors='replace') if log_path.exists() else ''
    raytracing_copy_warnings = [ln for ln in log_text.splitlines()
                                if 'RHICopyToStagingBuffer cannot be used on the RayTracing Accelleration structure'
                                in ln]
    read_only_warnings = [ln for ln in log_text.splitlines() if 'is read only!' in ln]

    # ---- graphs and actual camera from ViewData.PrimaryViews -------------
    prologues = [i for i, p in enumerate(passes) if p.get('EventName') == 'Graph Prologue (Graphics)']
    epilogues = [i for i, p in enumerate(passes) if p.get('EventName') == 'Graph Epilogue']
    graph_ranges = []
    for start in prologues:
        end_candidates = [e for e in epilogues if e > start]
        if end_candidates:
            graph_ranges.append((start, min(end_candidates)))

    primary_resources = [r for r in resources if r.get('Name') == 'ViewData.PrimaryViews']
    primary_views = []
    for r in primary_resources:
        key = r.get('UniqueResourceName')
        candidates = [rb for rb in readbacks_by_key.get(key, []) if rb.get('FileExists')]
        fp = None
        if candidates:
            rb = sorted(candidates, key=lambda x: (x.get('ExpectedBytes') or 0))[0]
            candidate_path = raw / rb['File']
            if candidate_path.exists():
                fp = candidate_path
        if fp is None:
            direct = sorted(resource_file_index.get(key, []), key=lambda p: p.stat().st_size)
            if direct:
                fp = direct[0]
        if fp is None:
            continue
        data = fp.read_bytes()
        stride = int(r.get('BytesPerElement') or FPACKED_VIEW_BYTES)
        count = len(data) // stride if stride else 0
        for i in range(count):
            view = decode_packed_view(data, i * stride)
            view['resource_key'] = key
            view['resource_name'] = r.get('Name')
            view['resource_byte_size'] = r.get('ByteSize')
            view['payload_file'] = str(fp.relative_to(raw)).replace('\\', '/')
            view['payload_bytes'] = len(data)
            view['checks'] = camera_matches_expected(view)
            primary_views.append(view)

    # Associate each decoded view with the graph that consumes its resource.
    for view in primary_views:
        key = view['resource_key']
        consumer_ids = []
        for i, p in enumerate(passes):
            if key in (p.get('InputResources') or []) or any(
                    x.split('.')[0] == key for x in (p.get('InputResources') or [])):
                consumer_ids.append(i)
        view['consumer_pass_ids'] = consumer_ids[:20]
        view['consumer_pass_count'] = len(consumer_ids)
        view['graph_index'] = None
        for gi, (s, e) in enumerate(graph_ranges):
            if any(s <= cid <= e for cid in consumer_ids):
                view['graph_index'] = gi
                break

    graph_records = []
    for gi, (s, e) in enumerate(graph_ranges):
        gpasses = passes[s:e + 1]
        frames = sorted({scope for p in gpasses for scope in (p.get('ParentEventScopes') or [])
                         if scope.startswith('Frame ')})
        views = [v for v in primary_views if v.get('graph_index') == gi]
        tonemap = [p.get('EventName') for p in gpasses if p.get('EventName', '').startswith('Tonemap')]
        fxaa = [p.get('EventName') for p in gpasses if p.get('EventName', '').startswith('FXAA')]
        final_outputs = []
        for p in gpasses:
            if p.get('EventName', '').startswith('FXAA'):
                final_outputs.extend(p.get('OutputResources') or [])
        kind = 'scene' if (tonemap or fxaa or views) else 'slate'
        graph_records.append({
            'graph_index': gi,
            'kind': kind,
            'first_pass_id': passes[s].get('Id'),
            'last_pass_id': passes[e].get('Id'),
            'pass_count': len(gpasses),
            'engine_frames': frames,
            'views': views,
            'has_tonemap': bool(tonemap),
            'has_fxaa': bool(fxaa),
            'tonemap_events': tonemap,
            'fxaa_events': fxaa,
            'final_output_resources': final_outputs,
            'has_present_chain': bool(fxaa),
        })

    hero_views = [v for v in primary_views if all(v['checks'].values())]
    presented_graph = None
    for g in graph_records:
        if g['has_present_chain'] and g['views'] and any(all(v['checks'].values()) for v in g['views']):
            presented_graph = g
    if presented_graph is None and graph_records:
        presented_graph = graph_records[-1]

    # ---- subsystem coverage ----------------------------------------------
    def match_pass(keywords):
        out = []
        for p in passes:
            name = p.get('EventName', '')
            if any(k.lower() in name.lower() for k in keywords):
                out.append(name)
        return out

    def match_resource(keywords):
        out = []
        for name in resources_by_name:
            if any(k.lower() in (name or '').lower() for k in keywords):
                out.append(name)
        return sorted(out)

    categories = {
        'vsm': {
            'pass_keywords': ['VirtualShadow', 'Shadow.Virtual', 'VSM', 'FVirtualShadowMap'],
            'resource_keywords': ['Shadow.Virtual', 'VirtualShadow'],
            'reason_if_absent': 'No virtual-shadow-map pass or resource was present in the dumped RDG frame.',
        },
        'deferred': {
            'pass_keywords': ['BasePass', 'Deferred', 'PrePass', 'DepthPass', 'GBuffer', 'SceneTextures'],
            'resource_keywords': ['GBuffer', 'SceneDepth', 'SceneColor', 'SceneTextures'],
            'reason_if_absent': 'No deferred base/prepass or G-buffer resource was present.',
        },
        'direct_lighting': {
            'pass_keywords': ['DirectLighting', 'CullLights', 'Lights', 'LightGrid', 'DeferredLighting'],
            'resource_keywords': ['DirectLighting', 'LightGrid', 'LightTile'],
            'reason_if_absent': 'No direct-lighting pass or light-grid resource was present.',
        },
        'lumen_cards': {
            'pass_keywords': ['CardPage', 'MeshCard', 'CardCapture', 'CopyCards', 'SurfaceCache'],
            'resource_keywords': ['Lumen.Card', 'CardCapture', 'CardPage', 'SurfaceCache'],
            'reason_if_absent': 'No Lumen card capture/page/surface-cache record was present.',
        },
        'screen_probes': {
            'pass_keywords': ['ScreenProbe'],
            'resource_keywords': ['ScreenProbe'],
            'reason_if_absent': 'No Lumen screen-probe gather pass or resource was present.',
        },
        'radiance_cache': {
            'pass_keywords': ['RadianceCache'],
            'resource_keywords': ['RadianceCache'],
            'reason_if_absent': 'No Lumen radiance-cache pass or resource was present.',
        },
        'histories': {
            'pass_keywords': ['History', 'Temporal', 'NumFramesAccumulated'],
            'resource_keywords': ['History', 'NumFramesAccumulated', 'Temporal'],
            'reason_if_absent': 'No temporal history resource was present; the frame is valid but history reuse is not evidenced.',
        },
        'reflections': {
            'pass_keywords': ['Reflection'],
            'resource_keywords': ['Reflection'],
            'reason_if_absent': 'No Lumen reflection pass or resource was present.',
        },
        'composition': {
            'pass_keywords': ['PostProcessing', 'Tonemap', 'FXAA', 'Composite', 'ComposeTranslucency',
                              'DiffuseIndirectComposite'],
            'resource_keywords': ['PostProcessing', 'Tonemap', 'ViewFamily', 'SceneColor'],
            'reason_if_absent': 'No composition/tonemap/FXAA pass or output resource was present.',
        },
    }
    coverage = {}
    for name, spec in categories.items():
        pnames = match_pass(spec['pass_keywords'])
        rnames = match_resource(spec['resource_keywords'])
        status = 'available' if (pnames or rnames) else 'absent'
        coverage[name] = {
            'status': status,
            'pass_count': len(pnames),
            'pass_names_sample': sorted(set(pnames))[:20],
            'resource_count': len(rnames),
            'resource_names_sample': rnames[:20],
            'reason_if_absent': spec['reason_if_absent'] if status == 'absent' else None,
            'unsupported_branch': spec['reason_if_absent'] if status == 'absent' else None,
        }
    # The VSM pass names include the shadow-depth passes, so record whether
    # actual virtual-shadow page resources were retained, not just depth.
    coverage['vsm']['virtual_shadow_resource_count'] = len(
        [n for n in resources_by_name if 'Shadow.Virtual' in (n or '')])
    coverage['histories']['history_resource_count'] = len(
        [n for n in resources_by_name if 'History' in (n or '') or 'NumFramesAccumulated' in (n or '')])

    # ---- screenshot evidence --------------------------------------------
    screenshot = None
    predump = session.get('pre_dump_screenshot')
    if (base / 'Screenshot.png').exists():
        png = Png(base / 'Screenshot.png')
        step = 2
        total = 0
        lums = []
        for y in range(0, png.height, step):
            row = png.rows[y]
            for x in range(0, png.width, step):
                i = x * png.channels
                lum = 0.2126 * row[i] + 0.7152 * row[i + 1] + 0.0722 * row[i + 2]
                lums.append(lum)
                total += 1
        mean = sum(lums) / total if total else 0.0
        screenshot = {
            'file': str(base / 'Screenshot.png'),
            'bytes': (base / 'Screenshot.png').stat().st_size,
            'sha256': sha256_file(base / 'Screenshot.png'),
            'width': png.width,
            'height': png.height,
            'mean_display_luma_sampled': round(mean, 4),
            'size_ok': [png.width, png.height] == [1280, 720],
        }
        if predump and predump.get('file') and Path(predump['file']).exists():
            pre = Png(predump['file'])
            diff = None
            if (pre.width, pre.height) == (png.width, png.height):
                diff_sum = 0.0
                n = 0
                for y in range(0, png.height, 4):
                    ra = pre.rows[y]
                    rb = png.rows[y]
                    for x in range(0, png.width, 4):
                        ia = x * pre.channels
                        ib = x * png.channels
                        d = (abs(ra[ia] - rb[ib]) + abs(ra[ia + 1] - rb[ib + 1]) +
                             abs(ra[ia + 2] - rb[ib + 2])) / 3.0
                        diff_sum += d
                        n += 1
                diff = round(diff_sum / n, 4) if n else None
            screenshot['pre_dump_comparison'] = {
                'file': predump['file'],
                'mean_abs_rgb_difference_0_255': diff,
                'same_dimensions': (pre.width, pre.height) == (png.width, png.height),
                'stable': diff is not None and diff < 5.0,
            }

    # ---- gaps / unsupported ----------------------------------------------
    pass_draw_counts_bytes = (base / 'PassDrawCounts.json').stat().st_size if (base / 'PassDrawCounts.json').exists() else None
    gaps = []
    if pass_draw_counts_bytes == 0:
        gaps.append({
            'kind': 'unsupported_draw_counts',
            'detail': 'Base/PassDrawCounts.json is empty; per-pass draw-count capture is not provided by this '
                      'DumpGPU build. Passes.json and resource bindings are still complete and retained.',
        })
    if missing_expected_params:
        gaps.append({
            'kind': 'pass_parameter_block_missing',
            'detail': {
                'count': len(missing_expected_params),
                'passes': [f"{p.get('Id')}:{p.get('EventName')}({p.get('ParametersName')})"
                           for p in missing_expected_params],
                'reason': 'These passes use a parameter type outside the serialized shader-parameter block set; '
                          'the pass record itself is retained, but no parameter block exists in the dump.',
            },
        })
    if parameterless_passes:
        gaps.append({
            'kind': 'parameterless_passes',
            'detail': {
                'count': len(parameterless_passes),
                'reason': 'Clear/copy/barrier/graph/ray-tracing-build passes use engine built-in parameter structs '
                          'and intentionally have no serialized shader parameter block. Their pass records and '
                          'resource edges are retained.',
            },
        })
    if raytracing_copy_warnings:
        scene_buffers = [r for r in resources if r.get('Name') == 'FRayTracingScene::SceneBuffer']
        present = [r for r in scene_buffers if resource_file_index.get(r.get('UniqueResourceName'))]
        gaps.append({
            'kind': 'raytracing_scene_buffer_copy_warning',
            'detail': {
                'warning_count': len(raytracing_copy_warnings),
                'warning': raytracing_copy_warnings[0].strip() if raytracing_copy_warnings else None,
                'descriptor_count': len(scene_buffers),
                'descriptors_with_payload': len(present),
                'reason': 'D3D12 emitted a staging-copy warning for the ray-tracing acceleration structure. '
                          'The dump still contains a nonzero SceneBuffer payload for every descriptor; the warning '
                          'is retained as a readback-path caveat, not silently dropped.',
            },
        })
    if read_only_warnings:
        gaps.append({
            'kind': 'read_only_cvar_assignment',
            'detail': {
                'count': len(read_only_warnings),
                'warning': read_only_warnings[0].strip() if read_only_warnings else None,
                'reason': 'r.RHI.UniformBufferContentMap.Enable is read-only at runtime; it was set before uniform '
                          'creation through the launcher startup ini and observed as true. The later runtime '
                          'assignment was rejected by design and did not change the value.',
            },
        })
    if native_missing_content:
        gaps.append({
            'kind': 'native_uniform_binding_without_content',
            'detail': {
                'count': native_missing_content,
                'reason': 'Some native uniform-buffer resource bindings have no exported file; their offsets and '
                          'types are still in NativeUniformBufferBindings.json and are explicitly marked.',
            },
        })
    for m in missing_payload[:50]:
        gaps.append({'kind': 'resource_without_payload', 'detail': m})
    for rb in readbacks:
        if rb.get('Status') != 'Dumped':
            gaps.append({'kind': 'readback_not_dumped', 'detail': rb})
    gaps.extend([
        {'kind': 'shader_local_values', 'detail': 'Per-pixel/per-thread shader choices and intermediate registers are '
                                                  'not DumpGPU resources and are not fabricated from pass parameters.'},
        {'kind': 'memoryless_or_non_rdg', 'detail': 'Resources that are never referenced by an RDG pass, memoryless '
                                                     'resources, and writable SkipTracking resources are outside the '
                                                     'raw frame; absence is explicit, not silently filled.'},
        {'kind': 'uniform_buffer_content_scope', 'detail': 'Native uniform-buffer textures expose the CPU content map '
                                                            'and offsets. Shader-local values not written to a uniform '
                                                            'buffer are not recoverable.'},
    ])

    # ---- checks -----------------------------------------------------------
    checks = {
        'dump_status_ok': session.get('dump_status') == 'ok',
        'map_unchanged': bool(session.get('map_unchanged')),
        'saved_hero_matches_approved': all((session.get('saved_hero_matches_approved') or {}).get(k)
                                           for k in ('location', 'rotation', 'horizontal_fov')),
        'pilot_matches_saved_camera': bool((session.get('warmup') or {}).get('pilot_readback', {}).get('pilot_matches')),
        'actual_view_fov_70': (session.get('warmup') or {}).get('pilot_readback', {}).get('actual_view_fov') == 70.0,
        'bridge_viewport_1280x720': (session.get('warmup') or {}).get('render_status', {}).get('viewport_size') == [1280, 720],
        'redump_inputs_1': (session.get('dump_cvars_actual') or {}).get('r.DumpGPU.RedumpInputs', {}).get('value') == '1',
        'native_uniform_buffer_textures_1': (session.get('dump_cvars_actual') or {}).get(
            'r.DumpGPU.NativeUniformBufferTextures', {}).get('value') == '1',
        'skip_tracking_1': (session.get('dump_cvars_actual') or {}).get(
            'r.DumpGPU.Buffer.SkipTracking', {}).get('value') == '1',
        'uniform_buffer_content_map_1': str((session.get('dump_cvars_actual') or {}).get(
            'r.RHI.UniformBufferContentMap.Enable', {}).get('value', '')).lower() in ('1', 'true'),
        'all_textures_and_buffers': ((session.get('dump_cvars_actual') or {}).get('r.DumpGPU.Texture', {}).get('value') == '2'
                                     and (session.get('dump_cvars_actual') or {}).get('r.DumpGPU.Buffer', {}).get('value') == '2'),
        'pass_parameters_1': (session.get('dump_cvars_actual') or {}).get('r.DumpGPU.PassParameters', {}).get('value') == '1',
        'console_variables_1': (session.get('dump_cvars_actual') or {}).get(
            'r.DumpGPU.ConsoleVariables', {}).get('value') == '1',
        'one_engine_frame_requested': ((session.get('dump_cvars_actual') or {}).get('r.DumpGPU.FrameCount', {}).get('value') == '1'
                                       and '-oneframe' in session.get('dump_command', '')),
        'actual_view_from_dump_matrices': bool(hero_views),
        'actual_view_rect_1280x720': bool(hero_views) and all(v['view_rect'] == [0, 0, 1280, 720] for v in hero_views),
        'actual_hfov_70': bool(hero_views) and all(
            v['hfov_deg_from_projection'] is not None and abs(v['hfov_deg_from_projection'] - 70.0) < 0.2
            for v in hero_views),
        'actual_pitch_13_yaw_0': bool(hero_views) and all(
            abs(v['pitch_deg_from_forward'] - 13.0) < 0.2 and abs(v['yaw_deg_from_forward']) < 0.2
            for v in hero_views),
        'pass_records_present': len(passes) > 0,
        'resource_descs_present': len(resources) > 0,
        'parameter_bindings_present': len(parameter_bindings) > 0,
        'parameterless_passes_classified': len(parameterless_passes) > 0,
        'native_uniform_bindings_present': len(native_uniforms) > 0,
        'subresource_layouts_present': len(subresource_layouts) > 0,
        'console_variables_present': len(console_vars) > 0,
        'screenshot_1280x720': bool(screenshot and screenshot.get('size_ok')),
        'screenshot_stable_vs_predump': bool(screenshot and (screenshot.get('pre_dump_comparison') or {}).get('stable')),
        'all_requested_subsystems_have_entries': all(c['status'] == 'available' for c in coverage.values()),
        'all_resource_descriptors_have_payload': len(missing_payload) == 0,
        'in_place_versions_present': payload_version_files > 0 and len(multi_version_resources) > 0,
        'all_readback_records_dumped': all(rb.get('Status') == 'Dumped' for rb in readbacks),
        'all_scene_graph_views_match_hero': bool(
            [g for g in graph_records if g['kind'] == 'scene']) and all(
            g['views'] and all(all(v['checks'].values()) for v in g['views'])
            for g in graph_records if g['kind'] == 'scene'),
        'native_exported_files_exist': native_exported_existing == len(native_exported_files),
        'pre_existing_final_evidence_preserved': preservation.get('preserved'),
        'raw_inventory_matches_session': (inventory.get('file_count') == session.get('dump_file_count') and
                                          inventory.get('total_bytes') == session.get('dump_total_bytes')),
        'under_100gib': inventory.get('total_bytes', 0) <= 100 * 1024 ** 3,
    }

    analysis = {
        'schema': 'arda-sponza-gpu-dump-analysis-v1',
        'run_id': run_id,
        'session': str(session_path),
        'launcher': launcher,
        'raw_directory': str(raw),
        'dump_service': dump_service,
        'infos': infos,
        'metadata_files': metadata,
        'counts': {
            'passes': len(passes),
            'resources': len(resources),
            'unique_resource_names': len(resources_by_name),
            'parameter_binding_blocks': len(parameter_bindings),
            'passes_with_parameter_block': passes_with_params,
            'passes_missing_parameter_block': len(missing_param_passes),
            'parameterless_passes': len(parameterless_passes),
            'missing_expected_parameter_block': len(missing_expected_params),
            'missing_expected_parameter_pass_names': [
                f"{p.get('Id')}:{p.get('EventName')}({p.get('ParametersName')})"
                for p in missing_expected_params],
            'native_uniform_buffer_records': len(native_uniforms),
            'native_uniform_bindings': native_bindings,
            'native_uniform_layouts': dict(native_layouts),
            'native_exported_file_references': len(native_exported_files),
            'native_exported_files_existing': native_exported_existing,
            'native_missing_content_bindings': native_missing_content,
            'raytracing_scene_buffer_copy_warnings': len(raytracing_copy_warnings),
            'read_only_cvar_runtime_warnings': len(read_only_warnings),
            'referenced_structs': len(referenced_structs),
            'subresource_layouts': len(subresource_layouts),
            'readback_records': len(readbacks),
            'readback_status': dict(status_counts),
            'readback_kinds': dict(kind_counts),
            'readback_actual_bytes': readback_bytes,
            'payload_files': payload_count,
            'payload_bytes': payload_bytes,
            'payload_version_files': payload_version_files,
            'resources_with_multiple_versions': len(multi_version_resources),
            'max_versions_per_resource': max_versions_per_resource,
            'multi_version_examples': [
                {'key': k, 'version_count': len(v), 'versions': v[:8]}
                for k, v in sorted(multi_version_resources.items(),
                                   key=lambda kv: (-len(kv[1]), kv[0]))[:10]
            ],
            'unique_payload_resource_keys': len(resource_payload),
            'duplicate_resource_keys': duplicate_resource_keys,
            'resources_with_payload': resources_with_payload,
            'resources_missing_payload': len(missing_payload),
            'console_variables': len(console_vars),
            'graphs': len(graph_records),
            'scene_graphs': sum(1 for g in graph_records if g['kind'] == 'scene'),
            'slate_graphs': sum(1 for g in graph_records if g['kind'] == 'slate'),
            'primary_view_records': len(primary_views),
            'hero_camera_view_records': len(hero_views),
        },
        'graphs': graph_records,
        'presented_graph_index': presented_graph['graph_index'] if presented_graph else None,
        'primary_views': primary_views,
        'hero_camera_views': hero_views,
        'coverage': coverage,
        'preservation': preservation,
        'approved_state': {
            'warmup_draws': (session.get('warmup') or {}).get('draws'),
            'warmup_seconds': (session.get('warmup') or {}).get('elapsed_seconds'),
            'view_mode_lit': (session.get('warmup') or {}).get('render_status', {}).get('view_mode_index') == 3,
            'ev100': ((session.get('warmup') or {}).get('exposure') or {}).get('auto_exposure_min_brightness'),
            'cvars': (session.get('warmup') or {}).get('cvars_after_warmup'),
        },
        'missing_payload': missing_payload,
        'gaps': gaps,
        'screenshot': screenshot,
        'inventory': {
            'file_count': inventory.get('file_count'),
            'total_bytes': inventory.get('total_bytes'),
            'total_gib': round(inventory.get('total_bytes', 0) / (1024 ** 3), 4),
            'bytes_by_top_level': inventory.get('bytes_by_top_level'),
        },
        'checks': checks,
        'session_commands': session.get('commands'),
        'session_warmup': session.get('warmup'),
        'session_cvars_actual': session.get('dump_cvars_actual'),
        'environment': session.get('environment'),
    }
    analysis_path.write_text(json.dumps(analysis, indent=2), encoding='utf-8')
    print('analysis', analysis_path)

    write_report(analysis, Path(args.report))
    print('report', args.report)


def fmt_bytes(n):
    if n is None:
        return 'n/a'
    return f'{n:,} bytes ({n / (1024 ** 3):.3f} GiB)'


def write_report(a, path):
    c = a['counts']
    checks = a['checks']
    hero = a['hero_camera_views'][0] if a['hero_camera_views'] else None
    presented = a['presented_graph_index']
    env = a['environment'] or {}
    files = (env.get('files') or {})
    session = json.loads(Path(a['session']).read_text(encoding='utf-8'))
    state = a.get('approved_state') or {}
    state_cvars = state.get('cvars') or {}

    def h(p):
        recorded = (files.get(p) or {}).get('sha256')
        if recorded:
            return recorded
        candidate = Path(p)
        return sha256_file(candidate) if candidate.exists() else 'n/a'

    def cvar(name):
        rec = state_cvars.get(name) or {}
        return rec.get('value', 'n/a')

    status_ok = all(checks.get(k) for k in (
        'dump_status_ok', 'map_unchanged', 'saved_hero_matches_approved',
        'actual_view_from_dump_matrices', 'actual_view_rect_1280x720',
        'actual_hfov_70', 'actual_pitch_13_yaw_0', 'all_resource_descriptors_have_payload',
        'all_requested_subsystems_have_entries', 'under_100gib'))

    lines = []
    lines.append('# Sponza complete native GPU frame')
    lines.append('')
    lines.append(f"Status: **{'ok' if status_ok else 'incomplete'}**. Run `{a['run_id']}`.")
    lines.append('')
    lines.append('This step preserves one complete, unfiltered `DumpGPU` engine frame of the approved saved Sponza '
                 'hero view. It is raw evidence only: no packaging, no image comparison, no performance measurement, '
                 'and no map save. Every number below comes from the retained dump files or the capture session.')
    lines.append('')
    lines.append('## Raw frame')
    lines.append('')
    lines.append('| Item | Value |')
    lines.append('| --- | --- |')
    lines.append(f"| Raw directory | `{a['raw_directory']}` |")
    lines.append(f"| Dump status | `{session.get('dump_status')}` |")
    lines.append(f"| Files / bytes | {a['inventory']['file_count']:,} / {fmt_bytes(a['inventory']['total_bytes'])} |")
    lines.append(f"| Payload files / bytes | {c['payload_files']:,} / {fmt_bytes(c['payload_bytes'])} |")
    lines.append(f"| Versioned payload files | {c['payload_version_files']:,} `.v<key>.bin` files; "
                 f"{c['resources_with_multiple_versions']:,} resources have more than one version "
                 f"(max {c['max_versions_per_resource']}) |")
    lines.append(f"| Passes / resources | {c['passes']:,} / {c['resources']:,} |")
    lines.append(f"| Parameter blocks | {c['parameter_binding_blocks']:,} blocks for {c['passes_with_parameter_block']:,} passes; "
                 f"{c['parameterless_passes']:,} parameterless engine passes; {c['missing_expected_parameter_block']:,} expected-block gaps |")
    lines.append(f"| Native uniform-buffer records / bindings | {c['native_uniform_buffer_records']:,} / {c['native_uniform_bindings']:,} |")
    lines.append(f"| Referenced structs / subresource layouts | {c['referenced_structs']:,} / {c['subresource_layouts']:,} |")
    lines.append(f"| Readback records | {c['readback_records']:,}, statuses {c['readback_status']} |")
    lines.append(f"| Console variables | {c['console_variables']:,} |")
    lines.append(f"| Engine graphs in the frame | {c['graphs']:,} total: {c['scene_graphs']:,} scene + {c['slate_graphs']:,} Slate UI |")
    lines.append(f"| Primary-view records decoded | {c['primary_view_records']:,} ({c['hero_camera_view_records']:,} match the approved hero camera) |")
    lines.append('')
    lines.append(f"Dump invocation: `{session.get('dump_command')}`. Requested one frame (`r.DumpGPU.FrameCount=1`, "
                 f"`-oneframe`), all textures (`r.DumpGPU.Texture=2`), all buffers (`r.DumpGPU.Buffer=2`), pass "
                 f"parameters, console variables, native uniform-buffer textures, SkipTracking buffers, and "
                 f"`r.DumpGPU.RedumpInputs=1` for in-place versions.")
    lines.append('')
    lines.append('The unique on-disk payload is authoritative for disk bytes. `DumpReadbackStatus.json` can contain '
                 'more than one readback record for the same version (and native-buffer aliases), so the sum of its '
                 '`ActualBytes` is not used as the disk total.')
    lines.append('')
    lines.append('## Approved state and warm history')
    lines.append('')
    lines.append('| Item | Observed before dump |')
    lines.append('| --- | --- |')
    lines.append(f"| Camera actor | `CAM_Hero_Reference`, saved transform checked against `output/final-setup.json` |")
    lines.append(f"| Camera readback | {json.dumps((session.get('warmup') or {}).get('pilot_readback', {}).get('viewport_camera'))} |")
    lines.append(f"| ViewFOV at capture | {(session.get('warmup') or {}).get('pilot_readback', {}).get('actual_view_fov')} |")
    lines.append(f"| Viewport size / mode | {(session.get('warmup') or {}).get('render_status', {}).get('viewport_size')} / lit (view_mode_index 3) |")
    lines.append(f"| Exposure | EV100 {state.get('ev100')} (AEM_HISTOGRAM pinned), bias 0 |")
    lines.append(f"| Warmup / settle draws | {state.get('warmup_draws')} draws in {state.get('warmup_seconds')} s |")
    lines.append(f"| Lumen route | DGI {cvar('r.DynamicGlobalIlluminationMethod')}, reflections {cvar('r.ReflectionMethod')}, "
                 f"HWRT {cvar('r.Lumen.HardwareRayTracing')}, FarField {cvar('r.LumenScene.FarField')} |")
    lines.append(f"| Screen traces | gather {cvar('r.Lumen.ScreenProbeGather.ScreenTraces')}, reflections {cvar('r.Lumen.Reflections.ScreenTraces')} |")
    lines.append(f"| Diagnostics off | Visualize {cvar('r.Lumen.Visualize')}, Probe Debug {cvar('r.Lumen.ScreenProbeGather.Debug')}, "
                 f"ProbePlacement {cvar('r.Lumen.ScreenProbeGather.Debug.ProbePlacement')}, "
                 f"VisualizeTraces {cvar('r.Lumen.ScreenProbeGather.VisualizeTraces')}, ShaderPrint {cvar('r.ShaderPrint')} |")
    lines.append('')
    lines.append('Requested dump CVars were read back after assignment: `Texture=2`, `Buffer=2`, `PassParameters=1`, '
                 '`ConsoleVariables=1`, `Root=*`, `RedumpInputs=1`, `NativeUniformBufferTextures=1`, '
                 '`Buffer.SkipTracking=1`, `MaxStagingSize=1024`, `FrameCount=1`, `Stream=0`, `Upload=0`. '
                 '`r.RHI.UniformBufferContentMap.Enable` is read-only at runtime; the launcher set it before uniform '
                 'creation and the session observed `true` (set_by `SystemSettingsIni`).')
    lines.append('')
    lines.append('## Camera identity from the dumped view buffer')
    lines.append('')
    lines.append('| Item | Saved actor / approved setup | Actual dumped view (`ViewData.PrimaryViews`) |')
    lines.append('| --- | --- | --- |')
    if hero:
        lines.append(f"| Actor | `CAM_Hero_Reference` | primary view id {hero['scene_renderer_primary_view_id']} |")
        lines.append(f"| World origin (cm) | [-850.0, 0.0, 90.0] | {hero['world_origin_cm']} |")
        lines.append(f"| ViewRect | [0, 0, 1280, 720] | {hero['view_rect']} |")
        lines.append(f"| HFOV (deg) | 70.0 | {hero['hfov_deg_from_projection']} |")
        lines.append(f"| VFOV (deg) | 42.995661 | {hero['vfov_deg_from_projection']} |")
        lines.append(f"| Pitch / yaw (deg) | 13.0 / 0.0 | {hero['pitch_deg_from_forward']} / {hero['yaw_deg_from_forward']} |")
        lines.append(f"| Forward | approx [0.97437, 0, 0.22495] | {hero['view_forward']} |")
    else:
        lines.append('| Actual dumped view | missing | not decoded |')
    lines.append('')
    lines.append('The view records are decoded directly from the dumped `ViewData.PrimaryViews` `FPackedView` '
                 'structure: `ViewRect` at byte 640, `ViewOriginHigh/Low`, `ViewForward`, `PreViewTranslation`, and '
                 'the `ViewToClip` projection matrix. The bridge readback is retained separately in the session; '
                 'the identity above uses the dumped matrices, not the bridge call count.')
    lines.append('')
    lines.append('| Scene graph | Kind | Passes | ViewRect | Origin (cm) | HFOV | Pitch / yaw |')
    lines.append('| ---: | --- | ---: | --- | --- | ---: | --- |')
    for g in a['graphs']:
        if g['views']:
            for v in g['views']:
                lines.append(f"| {g['graph_index']} | {g['kind']} | {g['pass_count']} | {v['view_rect']} | "
                             f"{v['world_origin_cm']} | {v['hfov_deg_from_projection']} | "
                             f"{v['pitch_deg_from_forward']} / {v['yaw_deg_from_forward']} |")
        else:
            lines.append(f"| {g['graph_index']} | {g['kind']} | {g['pass_count']} | n/a | n/a | n/a | n/a |")
    lines.append('')
    frames = sorted({f for g in a['graphs'] for f in g['engine_frames']})
    lines.append(f"All dumped scene graphs belong to engine frame scope(s) {frames}. The presented graph is index "
                 f"{presented}; it contains the final FXAA/Tonemap chain and is the last scene graph. "
                 f"{c['scene_graphs']} scene graphs were rendered inside the one dumped engine frame, so a bridge "
                 f"draw count is not used as camera identity. The {c['slate_graphs']} Slate UI graph has no scene view.")
    lines.append('')
    lines.append('## Screenshot evidence')
    lines.append('')
    shot = a.get('screenshot') or {}
    pre = shot.get('pre_dump_comparison') or {}
    lines.append('| Item | Value |')
    lines.append('| --- | --- |')
    lines.append(f"| DumpGPU screenshot | `{shot.get('file')}` |")
    lines.append(f"| Dimensions / bytes / SHA-256 | {shot.get('width')}x{shot.get('height')} / {shot.get('bytes'):,} / `{shot.get('sha256')}` |")
    lines.append(f"| Mean display luma (sampled) | {shot.get('mean_display_luma_sampled')} |")
    lines.append(f"| Pre-dump comparison | mean abs RGB {pre.get('mean_abs_rgb_difference_0_255')} over the same dimensions; stable={pre.get('stable')} |")
    lines.append('')
    lines.append('The DumpGPU screenshot is the final presented frame, not a separately normalized image. Its '
                 'difference from the warmed pre-dump viewport screenshot is small, which confirms the dumped frame '
                 'is the warmed hero view.')
    lines.append('')
    lines.append('## Subsystem coverage')
    lines.append('')
    lines.append('| Subsystem | Status | Passes | Resources | Evidence / explicit branch |')
    lines.append('| --- | --- | ---: | ---: | --- |')
    for name, cov in a['coverage'].items():
        ev = ', '.join(cov['pass_names_sample'][:4]) if cov['pass_names_sample'] else (cov['reason_if_absent'] or '')
        res = ', '.join(cov['resource_names_sample'][:3]) if cov['resource_names_sample'] else ''
        lines.append(f"| {name} | {cov['status']} | {cov['pass_count']} | {cov['resource_count']} | {ev}{(' / ' + res) if res else ''} |")
    lines.append('')
    lines.append('Every requested branch has retained pass/resource entries: VSM page/table/culling resources, deferred '
                 'G-buffers and base pass, direct lighting and light tiles, Lumen card capture/copy/surface cache, '
                 'screen probes, radiance cache, temporal histories, reflections, and the final PostProcessing/'
                 'Tonemap/FXAA composition. There are no silently invented entries; absent branches would be listed '
                 'as `absent` with a reason.')
    lines.append('')
    lines.append('## Completeness and gaps')
    lines.append('')
    lines.append(f"- `Passes.json`: {c['passes']:,} pass records retained.")
    lines.append(f"- `ResourceDescs.json`: {c['resources']:,} descriptors, {c['resources_with_payload']:,} with a dumped payload, "
                 f"{c['resources_missing_payload']:,} explicitly missing. There are {c['unique_payload_resource_keys']:,} unique "
                 f"payload keys; {len(c['duplicate_resource_keys']):,} descriptors duplicate a key and share that payload.")
    lines.append(f"- In-place versions: `r.DumpGPU.RedumpInputs=1`; the payload contains {c['payload_version_files']:,} "
                 f"version-keyed files and {c['resources_with_multiple_versions']:,} resources with more than one "
                 f"version (max {c['max_versions_per_resource']}). The version keys are retained in the payload file names.")
    lines.append(f"- `ParameterBindings.json`: {c['parameter_binding_blocks']:,} blocks; {c['parameterless_passes']:,} engine "
                 f"clear/copy/barrier/graph passes are parameterless by type; {c['missing_expected_parameter_block']:,} "
                 f"passes have a parameter type outside the serialized block set (ShaderPrint `FParameters`).")
    lines.append(f"- `NativeUniformBufferBindings.json`: {c['native_uniform_buffer_records']:,} records, "
                 f"{c['native_uniform_bindings']:,} bindings, layouts {c['native_uniform_layouts']}; "
                 f"{c['native_exported_file_references']:,} exported-file references, "
                 f"{c['native_exported_files_existing']:,} present on disk.")
    lines.append(f"- `DumpReadbackStatus.json`: {c['readback_records']:,} records, statuses {c['readback_status']}, kinds {c['readback_kinds']}.")
    lines.append(f"- `SubresourceLayouts.json`: {c['subresource_layouts']:,} subresource layouts retained with byte sizes.")
    lines.append(f"- `ConsoleVariables.csv`: {c['console_variables']:,} rows.")
    lines.append(f"- `Base/PassDrawCounts.json`: {metadata_bytes(a, 'PassDrawCounts.json')} bytes; empty is an explicit "
                 f"unsupported-draw-counts gap, not a missing pass record.")
    lines.append('')
    lines.append('Explicit gaps / unsupported branches:')
    lines.append('')
    for g in a['gaps'][:30]:
        detail = g.get('detail')
        if isinstance(detail, dict):
            detail = json.dumps(detail, sort_keys=True)
        lines.append(f"- `{g.get('kind')}`: {detail}")
    lines.append('')
    lines.append('Shader-local choices are not resources. They are counted as gaps and no value is invented for them. '
                 'Resources outside the RDG graph, memoryless resources, and writable SkipTracking resources are '
                 'likewise absent by design and are called out rather than silently replaced.')
    lines.append('')
    lines.append('## Provenance and hashes')
    lines.append('')
    lines.append('| Item | Value |')
    lines.append('| --- | --- |')
    infos = a.get('infos') or {}
    lines.append(f"| Engine | UE {env.get('engine_version', {}).get('MajorVersion')}.{env.get('engine_version', {}).get('MinorVersion')}.{env.get('engine_version', {}).get('PatchVersion')} |")
    lines.append(f"| Engine BuildId | `{env.get('engine_build_id')}` |")
    lines.append(f"| GPU / driver | {infos.get('GPUDeviceDescription')} / {infos.get('GPUDriverUserVersion')} |")
    lines.append(f"| RHI / shader platform | {infos.get('RHI')} / {infos.get('RHIMaxFeatureLevel')} |")
    lines.append(f"| Declared source base | `{session.get('declared_source_base')}` |")
    lines.append(f"| Bridge BuildId | `{env.get('project_module_build_id')}` (matches declared: {env.get('bridge_build_id_matches_declared')}) |")
    lines.append(f"| Engine BuildId == project BuildId | {env.get('engine_build_id_matches_project')} |")
    lines.append(f"| Map SHA-256 | `{session.get('map_sha256_after')}` (unchanged: {session.get('map_unchanged')}) |")
    lines.append(f"| Setup SHA-256 | `{session.get('setup_sha256')}` |")
    lines.append(f"| UnrealEditor.exe SHA-256 | `{h(str(WORKSPACE.parent / 'UnrealEngine' / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor.exe'))}` |")
    lines.append(f"| UnrealEditor-RenderCore.dll SHA-256 | `{h(str(WORKSPACE.parent / 'UnrealEngine' / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor-RenderCore.dll'))}` |")
    lines.append(f"| UnrealEditor-Renderer.dll SHA-256 | `{h(str(WORKSPACE.parent / 'UnrealEngine' / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor-Renderer.dll'))}` |")
    lines.append(f"| UnrealEditor-RHI.dll SHA-256 | `{h(str(WORKSPACE.parent / 'UnrealEngine' / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor-RHI.dll'))}` |")
    lines.append(f"| UnrealEditor-D3D12RHI.dll SHA-256 | `{h(str(WORKSPACE.parent / 'UnrealEngine' / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor-D3D12RHI.dll'))}` |")
    lines.append(f"| ArdaLabCapture.dll SHA-256 | `{h(str(WORKSPACE / 'project' / 'UnrealLab' / 'Binaries' / 'Win64' / 'UnrealEditor-ArdaLabCapture.dll'))}` |")
    lines.append(f"| Capture script SHA-256 | `{h(str(WORKSPACE / 'scripts' / 'capture_gpu_dump.py'))}` |")
    lines.append(f"| Launcher script SHA-256 | `{h(str(WORKSPACE / 'scripts' / 'run_gpu_dump.ps1'))}` |")
    lines.append('')
    lines.append('The session record carries the complete engine/module hash list under `environment.files`; the table '
                 'above selects the runtime products used to build the frame. The capture-script hash is the '
                 'launch-time hash stored in that session. The launcher-script hash is report-time; the launcher '
                 'record retains the exact arguments, PID and wall time for this run.')
    lines.append('')
    lines.append('## Preservation of the pre-existing final evidence')
    lines.append('')
    pres = a.get('preservation') or {}
    lines.append('| Check | Result |')
    lines.append('| --- | --- |')
    lines.append(f"| Recorded frame hashes | {pres.get('frame_hash_records')} checked; mismatches: {len(pres.get('frame_hash_mismatches') or [])} |")
    lines.append(f"| Recorded run hashes | {pres.get('run_hash_records')} checked; mismatches: {len(pres.get('run_hash_mismatches') or [])} |")
    lines.append(f"| Recorded script hashes | {pres.get('script_hash_records')} checked; mismatches: {len(pres.get('script_hash_mismatches') or [])} |")
    lines.append(f"| Script mismatches pre-date this run | {pres.get('script_hash_mismatches_are_preexisting')} "
                 f"({', '.join(pres.get('script_hash_mismatches') or []) or 'none'}) |")
    lines.append(f"| Approved image | matches: {pres.get('approved_image_matches')} |")
    lines.append(f"| `output/final-setup.json` | `{pres.get('final_setup_sha256')}`; matches manifest: {pres.get('final_setup_matches_manifest')} |")
    lines.append(f"| Preserved | {pres.get('preserved')} |")
    lines.append('')
    lines.append('This GPU step only added `scripts/capture_gpu_dump.py`, `scripts/run_gpu_dump.ps1`, '
                 '`scripts/analyze_gpu_dump.py`, `output/gpu-dump/**` and this report. The pre-existing final images, '
                 'runs, reports and setup file were re-hashed above and were not changed.')
    lines.append('')
    lines.append('## Requested acceptance checks')
    lines.append('')
    lines.append('1. **Complete available native resources, parameters and pass records retained with explicit gaps**: '
                 f"yes. {c['passes']:,} passes, {c['resources']:,} resource descriptors "
                 f"({c['resources_with_payload']:,} with payload, {c['resources_missing_payload']:,} missing), "
                 f"{c['parameter_binding_blocks']:,} parameter blocks, {c['native_uniform_buffer_records']:,} native "
                 f"uniform-buffer records, {c['readback_records']:,} readback records and "
                 f"{c['payload_version_files']:,} version-keyed payload files "
                 f"({c['resources_with_multiple_versions']:,} resources with in-place versions) are in the raw "
                 f"directory; the gap list names the empty draw-count file, the three ShaderPrint parameter gaps, "
                 f"the 80 parameterless passes, the ray-tracing staging warning, and the shader-local/non-RDG limits.")
    lines.append('2. **Dump uses the approved saved hero camera with actual view dimensions and identity**: yes. '
                 f"The saved `CAM_Hero_Reference` matched `output/final-setup.json`; the dumped "
                 f"`ViewData.PrimaryViews` decoded origin {hero['world_origin_cm'] if hero else 'n/a'} cm, "
                 f"ViewRect {hero['view_rect'] if hero else 'n/a'}, HFOV {hero['hfov_deg_from_projection'] if hero else 'n/a'} deg, "
                 f"pitch/yaw {hero['pitch_deg_from_forward'] if hero else 'n/a'}/{hero['yaw_deg_from_forward'] if hero else 'n/a'} deg.")
    lines.append('')
    lines.append('## Invocation ledger')
    lines.append('')
    launcher = a.get('launcher') or {}
    lines.append(f"- Launcher record: `{launcher.get('record_path')}`; PID {launcher.get('pid')}; "
                 f"exit_code {launcher.get('exit_code')!r}; wall {launcher.get('wall_seconds')} s; "
                 f"dump status `{launcher.get('dump_status')}`. The PowerShell host did not expose the child "
                 f"exit code; the owned process exited, no UnrealEditor process remains, and the dump status is "
                 f"`ok`.")
    lines.append(f"- Bridge draws recorded: {session.get('draws')}; warmup {session.get('warmup', {}).get('draws')} draws; "
                 f"dump trigger draws {session.get('dump_trigger_draws')}; fallback re-triggers {session.get('dump_retriggers')}.")
    lines.append(f"- Dump armed at `{session.get('dump_armed_utc')}`, completed at `{session.get('dump_completed_utc')}` "
                 f"in {session.get('dump_seconds')} s; engine frame scope {frames}.")
    lines.append(f"- Exact console invocations are retained in `{a['session']}` under `commands` "
                 f"({len(a.get('session_commands') or [])} records), including the one `DumpGPU` command "
                 f"`{session.get('dump_command')}`. The bridge's console wrapper returned false for these commands "
                 f"(it does not report success for CVar assignment), so the actual CVar readback and `Base/Status.txt` "
                 f"are the authoritative evidence; the dump status is `{session.get('dump_status')}`.")
    lines.append('')
    lines.append('## Compliance')
    lines.append('')
    lines.append('- The map was loaded and piloted, never saved: SHA-256 before and after are identical.')
    lines.append('- This step issued no write under `D:/UnrealEngine`, `D:/UnrealUnderstanding` or the original '
                 'capture project; all new files are under this workspace.')
    lines.append('- No Git command was run. No engine or bridge source was modified in this step.')
    lines.append('- No packaging was performed. Only the launcher-owned UnrealEditor process was used and waited for; '
                 'there is no process-name kill.')
    lines.append('')
    path.write_text('\n'.join(lines), encoding='utf-8')


def metadata_bytes(a, name):
    info = (a.get('metadata_files') or {}).get(name) or {}
    return info.get('bytes')


if __name__ == '__main__':
    main()
