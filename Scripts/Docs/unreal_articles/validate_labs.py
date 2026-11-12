"""Check the frozen rendering evidence without a GPU or scientific dependencies.

--freeze records source/output hashes after experiments and reviews have passed.
This is an artifact-consistency check, not a replacement for executing the labs.
Use the individual lab reproduction commands to rerun numerical validation.
"""
from pathlib import Path
import argparse
import hashlib
import json
import struct
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
MANIFEST = HERE / 'laboratory-manifest.json'
TEXT = {'.py', '.ps1', '.cpp', '.h', '.cs', '.ini', '.uproject', '.txt', '.md', '.json', '.svg', '.gitignore'}


def digest(path):
    data = path.read_bytes()
    if path.suffix in TEXT or path.name == '.gitignore':
        data = data.replace(b'\r\n', b'\n')
    return hashlib.sha256(data).hexdigest()


def files():
    selected = []
    for family in ('nanite', 'lumen', 'unreal'):
        for path in (HERE / 'labs' / family).rglob('*'):
            if not path.is_file() or any(p in {'.venv', '__pycache__', 'cache', 'data'} for p in path.relative_to(HERE / 'labs' / family).parts):
                continue
            if path.suffix in TEXT or path.name == '.gitignore': selected.append(path)
        selected.extend(path for path in (ROOT / 'Docs/Unreal/assets' / (family + '-lab')).iterdir() if path.is_file())
    selected.extend(path for path in (ROOT / 'Docs/Unreal/assets/scene-room-captures').iterdir() if path.is_file())
    return sorted(selected)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--freeze', action='store_true')
    args = ap.parse_args()
    paths = files()
    measured = {path.relative_to(ROOT).as_posix(): digest(path) for path in paths}
    if args.freeze:
        MANIFEST.write_text(json.dumps({'engine_commit': '71fe36aac5a8df5ccd66c763ffc902b29b6a9c43',
                                       'hash_note': 'Text uses LF normalization; binary image/array files are hashed verbatim.',
                                       'files': measured}, indent=2) + '\n', encoding='utf-8')
    saved = json.loads(MANIFEST.read_text(encoding='utf-8'))['files']
    assert saved.keys() == measured.keys(), 'Laboratory file inventory changed; verify and refreeze evidence'
    for path, expected in saved.items():
        assert measured[path] == expected, 'Changed laboratory source/output: ' + path
    image_count = 0
    for path in paths:
        if path.suffix == '.png':
            data = path.read_bytes()
            assert data[:8] == b'\x89PNG\r\n\x1a\n', path
            w, h = struct.unpack('>II', data[16:24])
            assert w > 0 and h > 0, path
            image_count += 1
    nanite = json.loads((HERE / 'labs/nanite/verification.json').read_text(encoding='utf-8'))
    for name, expected in nanite['asset_sha256'].items():
        assert digest(ROOT / 'Docs/Unreal/assets/nanite-lab' / name) == expected, 'Stale Nanite verification: ' + name
    engine_assets = ROOT / 'Docs/Unreal/assets/unreal-lab'
    captures = json.loads((engine_assets / 'capture-manifest.json').read_text(encoding='utf-8'))
    assert {p.name for p in engine_assets.glob('*.png')} == {r['file'] for r in captures['figures']}
    assert len(captures['figures']) == captures['camera_screenshots'] + captures['gpu_array_displays']
    for record in captures['figures']:
        assert digest(engine_assets / record['file']) == record['sha256'], 'Changed engine capture: ' + record['file']
    subprocess.run([sys.executable, str(HERE / 'labs/lumen/verify.py')], check=True)
    subprocess.run([sys.executable, str(HERE / 'labs/unreal/publish_scene_room.py'),
                    'unused', str(ROOT / 'Docs/Unreal/assets/scene-room-captures'),
                    '--check-published'], check=True)
    print(f'PASS: {len(paths)} laboratory sources/outputs, {image_count} PNGs, frozen hashes and numerical evidence')


if __name__ == '__main__':
    main()
