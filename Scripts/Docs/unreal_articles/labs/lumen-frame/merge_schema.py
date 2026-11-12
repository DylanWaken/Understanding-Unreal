"""Merge reviewed source dictionaries without discarding their evidence or gaps.

The resource layout is a source interpretation. Captured resource descriptors,
subresource layouts and native parameter metadata determine runtime storage.
"""
from pathlib import Path
import argparse
import copy
import hashlib
import json
import re


def merge(paths):
    documents = [(path, json.loads(path.read_text(encoding='utf-8-sig'))) for path in paths]
    base = documents[0][1]
    result = {
        'schemaVersion': base['schemaVersion'],
        'engine': copy.deepcopy(base.get('engine', {})),
        'resourceSchemas': [], 'parameterSchemas': [], 'samplerBindings': [],
        'sourceRefs': [], 'coverageGaps': [], 'parts': [],
        'coverage': {
            'activeFrameComplete': False,
            'scope': 'Reviewed source dictionary; runtime binding and payload coverage are audited separately.',
            'parts': [],
        },
    }
    ids = set()
    for index, (path, document) in enumerate(documents):
        prefix = f'part{index}.'
        refs = document.get('sourceRefs', [])
        assert isinstance(refs, list), f'{path}: expected a sourceRefs array'
        assert len({ref['id'] for ref in refs}) == len(refs), f'{path}: duplicate source reference'
        ref_map = {ref['id']: prefix + ref['id'] for ref in refs}

        def remap(value):
            if isinstance(value, str):
                return ref_map.get(value, value)
            if isinstance(value, list):
                return [remap(item) for item in value]
            if isinstance(value, dict):
                return {key: remap(item) for key, item in value.items()}
            return value

        part = remap(copy.deepcopy(document))
        for key in ['resourceSchemas', 'parameterSchemas']:
            for entry in part.get(key, []):
                assert entry['id'] not in ids, f'duplicate schema ID: {entry["id"]}'
                ids.add(entry['id'])
                for selector in entry.get('selectors', []):
                    for field in ['resourceNameRegex', 'passNameRegex']:
                        if field in selector:
                            re.compile(selector[field])
                result[key].append(entry)
        for key in ['sourceRefs', 'samplerBindings', 'coverageGaps']:
            result[key].extend(part.get(key, []))
        result['parts'].append({
            'path': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'referenceNamespace': prefix,
            'resourceSchemas': len(part.get('resourceSchemas', [])),
            'parameterSchemas': len(part.get('parameterSchemas', [])),
            'sourceRefs': len(refs),
        })
        result['coverage']['parts'].append({
            'path': path.name, 'coverage': part.get('coverage', {}),
            'nameCoverage': part.get('nameCoverage', {}),
        })
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parts', nargs='+', type=Path, required=True,
                        help='Reviewed base first, then reviewed extensions.')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = merge(args.parts)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: len(result[key]) for key in
                      ['resourceSchemas', 'parameterSchemas', 'sourceRefs', 'coverageGaps']}))


if __name__ == '__main__':
    main()
