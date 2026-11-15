#!/usr/bin/env python3
"""Read-only verification of the captured radiosity parameter payloads.

Reads the machine manifest and the raw `Structures`/`StructuresMetadata` files
from the finished capture.  It decodes only values that were actually captured:
CachedLightingPreExposure from the `Batched lights` FParameters block and the
presence of FixedJitterIndex in the nested radiosity texel-trace metadata.
No Unreal launch, no GPU work, no shader-local invention.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
MANIFEST = WORKSPACE / 'output' / 'capture-manifest.json'
OUT = WORKSPACE / 'output' / 'radiosity' / 'parameter-verification.json'


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def member_offset(metadata, name):
    for m in metadata.get('Members') or []:
        if m.get('Name') == name:
            return int(m.get('Offset')), str(m.get('BaseType'))
    return None, None


def main():
    manifest = load(MANIFEST)
    per_state = (manifest.get('parameter_capture') or {}).get('per_state') or {}
    states = {}
    ok = True
    for label, params in per_state.items():
        params = params or []
        state = {'cached_lighting_pre_exposure': [], 'placement_jitter': []}
        for p in params:
            if p.get('event_name') == 'Batched lights':
                meta = load(p['metadata_file'])
                offset, base_type = member_offset(meta, 'CachedLightingPreExposure')
                raw = Path(p['parameters_file']).read_bytes()
                value = (struct.unpack_from('<f', raw, offset)[0]
                         if offset is not None else None)
                rec = {
                    'pass_id': p.get('pass_id'),
                    'pointer_prefix': p.get('pointer_prefix'),
                    'parameters_file': p.get('parameters_file'),
                    'parameters_bytes': p.get('parameters_bytes'),
                    'parameters_sha256': p.get('parameters_sha256'),
                    'metadata_file': p.get('metadata_file'),
                    'metadata_sha256': p.get('metadata_sha256'),
                    'offset': offset,
                    'base_type': base_type,
                    'value': value,
                    'value_matches_default_4_ev': bool(value is not None and
                                                       abs(value - 0.0625) < 1e-12),
                }
                state['cached_lighting_pre_exposure'].append(rec)
                if not rec['value_matches_default_4_ev']:
                    ok = False
            if p.get('event_name') == 'BuildRadiosityTiles':
                found = False
                for dep in p.get('metadata_dependencies') or []:
                    try:
                        meta = load(dep['file'])
                    except Exception:
                        continue
                    if 'FixedJitterIndex' in [m.get('Name') for m in
                                              (meta.get('Members') or [])]:
                        off, base = member_offset(meta, 'FixedJitterIndex')
                        found = True
                        state['placement_jitter'].append({
                            'pass_id': p.get('pass_id'),
                            'pointer_prefix': p.get('pointer_prefix'),
                            'parameters_file': p.get('parameters_file'),
                            'parameters_sha256': p.get('parameters_sha256'),
                            'nested_struct_type': meta.get('StructTypeName'),
                            'nested_metadata_file': dep['file'],
                            'nested_metadata_sha256': dep.get('sha256'),
                            'offset': off,
                            'base_type': base,
                        })
                        break
                if not found:
                    ok = False
        states[label] = state
    result = {
        'schema': 'arda-radiosity-parameter-verification-v1',
        'source_manifest': str(MANIFEST),
        'run_id': manifest.get('run_id'),
        'ok': ok,
        'states': states,
        'note': ('Values read from captured Structures/*.bin using offsets in the '
                 'captured StructuresMetadata; no value is inferred from a shader.'),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'ok': ok, 'path': str(OUT),
                      'values': {k: [r['value'] for r in v['cached_lighting_pre_exposure']]
                                 for k, v in states.items()}}, indent=2))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
