"""Validate source dictionary joins; optionally verify against a local UE checkout."""
from pathlib import Path
import argparse
import hashlib
import json
import re
import subprocess
from merge_schema import merge

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine-root', type=Path)
    parser.add_argument('--write-provenance', action='store_true')
    args = parser.parse_args()
    schema_path = HERE / 'schemas/source-schema.json'
    schema = json.loads(schema_path.read_text(encoding='utf-8'))
    parts = [HERE / 'schemas/parts' / entry['path'] for entry in schema['parts']]
    assert merge(parts) == schema, 'Merged dictionary differs from its reviewed parts'
    known = {entry['id'] for entry in schema['sourceRefs']}
    assert len(known) == len(schema['sourceRefs'])

    def inspect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ('sourceRefs', 'producerSources', 'consumerSources'):
                    assert isinstance(child, list), key
                    for ref in child:
                        assert ref in known, f'Unknown source reference: {ref}'
                else:
                    inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    for key in ('resourceSchemas', 'parameterSchemas', 'samplerBindings', 'coverageGaps'):
        inspect(schema[key])
    if args.engine_root:
        files = {}
        mismatches = []
        for ref in schema['sourceRefs']:
            path = args.engine_root / ref['path']
            if ref['path'] not in files:
                raw = path.read_bytes()
                files[ref['path']] = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
            lines = path.read_text(encoding='utf-8-sig').splitlines()
            assert 1 <= ref['line'] <= len(lines), ref
            symbol = re.sub(r'\s+', '', ref['symbol'])
            line = re.sub(r'\s+', '', lines[ref['line'] - 1])
            pattern = re.escape(symbol).replace(r'\*', '.*')
            if not re.search(pattern, line):
                mismatches.append({'reference': ref, 'actualLine': lines[ref['line'] - 1].strip()})
        # `symbol` is an editorial label in the visibility dictionary. For
        # example CreateBuffer(Shadow.Virtual.ProjectionData) abbreviates a
        # longer C++ call. Record this separately from literal symbol matches;
        # line bounds and file hashes apply to every reference.
        revision = subprocess.check_output(['git', '-C', str(args.engine_root), 'rev-parse', 'HEAD'], text=True).strip()
        dirty = subprocess.check_output(['git', '-C', str(args.engine_root), 'diff', 'HEAD', '--name-only', '--', *files], text=True).splitlines()
        provenance = {'engineCommit': revision, 'files': files, 'modifiedCitedFiles': dirty,
                      'sourceLineBoundsVerified': len(known),
                      'literalOrWildcardSymbolMatches': len(known) - len(mismatches),
                      'descriptiveSymbolLabels': [item['reference']['id'] for item in mismatches],
                      'verificationNote': 'Line/file integrity and reference joins are checked mechanically; descriptive labels and algorithmic semantics require source review.',
                      'schemaSha256': hashlib.sha256(schema_path.read_bytes()).hexdigest()}
        if args.write_provenance:
            (HERE / 'schemas/source-provenance.json').write_text(json.dumps(provenance, indent=2) + '\n', encoding='utf-8')
        print(f'Verified {len(known)} source line bounds in {len(files)} files; '
              f'{len(mismatches)} descriptive labels; {len(dirty)} cited files differ from HEAD.')
    else:
        assert not args.write_provenance, '--write-provenance requires --engine-root'
    print(f'PASS: {len(schema["resourceSchemas"])} resource layouts, '
          f'{len(schema["parameterSchemas"])} parameter layouts, {len(known)} source references.')


if __name__ == '__main__':
    main()
