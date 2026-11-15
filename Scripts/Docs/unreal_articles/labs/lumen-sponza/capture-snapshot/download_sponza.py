"""Download the Khronos glTF-Sample-Assets Sponza model and record hashes.

Writes only under this workspace's source/ folder.  The upstream tree is not a
git checkout; every downloaded file is verified by size and SHA-256 and listed
in source/Sponza/download-manifest.json.  Attribution files (README.md,
LICENSE.md, metadata.json) are kept next to the model, not inside the model's
glTF folder.

Enumeration does not use the GitHub contents API (shared-IP rate limits): the
glTF document itself lists every buffer and image URI, and adding Sponza.gltf
accounts for all 71 upstream files.  The revision is a pinned main commit,
recorded in the manifest.
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'source' / 'Sponza'
GLTF_DIR = SOURCE / 'glTF'
ATTR_DIR = SOURCE / 'attribution'
REPO = 'KhronosGroup/glTF-Sample-Assets'
REVISION = os.environ.get('ARDA_SPONZA_REVISION', '8f492390ddc7a2ae221a1369ef560589cf8c70fb')
MODEL_DIR = 'Models/Sponza'
RAW = 'https://raw.githubusercontent.com'
EXPECTED_FILES = 71
EXPECTED_BYTES = 52_686_624
EXPECTED_GLTF_BYTES = 167_176
EXPECTED_BIN_BYTES = 9_528_220


def fetch(url, attempts=5):
    last = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'arda-sponza-prep/1.0'})
            with urllib.request.urlopen(req, timeout=180) as response:
                return response.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last = exc
            print(f'retry {attempt + 1}/{attempts} {url}: {exc}', flush=True)
            time.sleep(2 + 4 * attempt)
    raise RuntimeError(f'failed: {url}: {last}')


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def write_atomic(path, data):
    tmp = Path(str(path) + '.part')
    tmp.write_bytes(data)
    os.replace(tmp, path)


def main():
    GLTF_DIR.mkdir(parents=True, exist_ok=True)
    ATTR_DIR.mkdir(parents=True, exist_ok=True)

    base = f'{RAW}/{REPO}/{REVISION}/{MODEL_DIR}/glTF'
    gltf_bytes = fetch(f'{base}/Sponza.gltf')
    if len(gltf_bytes) != EXPECTED_GLTF_BYTES:
        raise RuntimeError(f'Sponza.gltf is {len(gltf_bytes)} bytes, expected {EXPECTED_GLTF_BYTES}')
    document = json.loads(gltf_bytes)
    uris = set()
    for buffer in document.get('buffers', []):
        if 'uri' in buffer:
            uris.add(buffer['uri'])
    for image in document.get('images', []):
        if 'uri' in image:
            uris.add(image['uri'])
    names = ['Sponza.gltf'] + sorted(uris)
    if len(names) != EXPECTED_FILES:
        raise RuntimeError(f'enumerated {len(names)} files from the glTF document, expected {EXPECTED_FILES}')
    if any('/' in name or '\\' in name for name in names):
        raise RuntimeError('unexpected nested URI in Sponza.gltf: ' + repr(names))

    records = []
    total = 0
    for index, name in enumerate(names, 1):
        data = gltf_bytes if name == 'Sponza.gltf' else fetch(f'{base}/{name}')
        target = GLTF_DIR / name
        write_atomic(target, data)
        total += len(data)
        records.append({
            'name': name,
            'path': str(target),
            'bytes': len(data),
            'sha256': sha256_bytes(data),
            'source_url': f'{base}/{name}',
        })
        if index % 10 == 0 or index == len(names):
            print(f'{index}/{len(names)} files, {total} bytes', flush=True)

    if total != EXPECTED_BYTES:
        raise RuntimeError(f'total bytes {total} != expected {EXPECTED_BYTES}')
    gltf_record = next(r for r in records if r['name'] == 'Sponza.gltf')
    bin_record = next(r for r in records if r['name'] == 'Sponza.bin')
    if bin_record['bytes'] != EXPECTED_BIN_BYTES:
        raise RuntimeError('Sponza.bin unexpected size')

    attribution = {}
    for name in ('README.md', 'LICENSE.md', 'metadata.json'):
        data = fetch(f'{RAW}/{REPO}/{REVISION}/{MODEL_DIR}/{name}')
        target = ATTR_DIR / name
        write_atomic(target, data)
        attribution[name] = {
            'path': str(target),
            'bytes': len(data),
            'sha256': sha256_bytes(data),
            'source_url': f'{RAW}/{REPO}/{REVISION}/{MODEL_DIR}/{name}',
        }
        print('attribution', name, len(data), flush=True)

    manifest = {
        'schema': 'arda-sponza-source-manifest-v1',
        'asset': 'Sponza',
        'upstream_repository': f'https://github.com/{REPO}',
        'upstream_revision': REVISION,
        'upstream_revision_kind': 'pinned commit SHA of refs/heads/main at download time',
        'upstream_path': f'{MODEL_DIR}/glTF',
        'upstream_page': f'https://github.com/{REPO}/tree/{REVISION}/{MODEL_DIR}',
        'retrieved_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'license_summary': 'Model: CRYENGINE Limited License Agreement (see attribution/LICENSE.md); '
                           'metadata/documentation: CC BY 4.0. Local educational use only; '
                           'raw assets are not redistributed.',
        'expected': {'files': EXPECTED_FILES, 'bytes': EXPECTED_BYTES},
        'measured': {'files': len(records), 'bytes': total},
        'files': records,
        'attribution': attribution,
        'gltf_sha256': gltf_record['sha256'],
        'bin_sha256': bin_record['sha256'],
    }
    write_atomic(SOURCE / 'download-manifest.json', json.dumps(manifest, indent=2).encode('utf-8'))
    print('OK', len(records), 'files', total, 'bytes', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
