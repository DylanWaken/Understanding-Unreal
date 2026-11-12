"""Copy verified camera outputs and write a small reproducibility manifest.

Atlas images are exported separately by export_atlases.py. No image pixels are
changed here. Only generated lab outputs are copied into the documentation.
"""
import argparse
import hashlib
import json
import re
import shutil
import struct
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('lab_root', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--engine-root', type=Path, default=Path('D:/UnrealEngine'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    source = args.lab_root / 'captures'
    figures = []
    reports = [('capture-report.json', 'lumen-capture-report.json', 28),
               ('bunny-report.json', 'nanite-capture-report.json', 18)]
    for input_name, output_name, expected in reports:
        report = json.loads((source / input_name).read_text(encoding='utf-8'))
        assert len(report['captures']) == expected, input_name
        for capture in report['captures']:
            name = capture['name'] + '.png'
            raw = (source / name).read_bytes()
            assert raw[:8] == b'\x89PNG\r\n\x1a\n', name
            assert struct.unpack('>II', raw[16:24]) == (1280, 720), name
            shutil.copy2(source / name, args.output / name)
            figures.append({'file': name, 'kind': 'Unreal editor viewport screenshot',
                            'size': [1280, 720], 'sha256': sha(args.output / name)})
        shutil.copy2(source / input_name, args.output / output_name)
    shutil.copy2(source / 'scene-inventory.json', args.output / 'scene-inventory.json')
    atlas = args.output / 'gpu-atlas-report.json'
    if atlas.exists():
        for image in json.loads(atlas.read_text(encoding='utf-8'))['images']:
            path = args.output / image['file']
            assert path.exists(), path
            figures.append({'file': path.name, 'kind': 'Measured GPU array display; see gpu-atlas-report.json',
                            'size': list(struct.unpack('>II', path.read_bytes()[16:24])), 'sha256': sha(path)})
    here = Path(__file__).resolve().parent
    sources = [p for p in here.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc']
    logs = []
    for name in ('capture.py.stdout.log', 'capture_bunny.py.stdout.log'):
        log = (args.lab_root / name).read_text(encoding='utf-8', errors='replace')
        assert 'LogExit: Exiting.' in log, 'Capture process has not completed: '+name
        assert 'Failed to compile Material' not in log, 'Material compilation failed: '+name
        assert 'LogPython: Error:' not in log, 'Python failed: '+name
        logs.append(log)
    excerpt = '\n'.join(line for log in logs for line in log.splitlines()
                        if re.search(r'Chosen D3D12 Adapter|LogRHI:.*Name:|shaderplatform=|ARDA_COMMAND|ARDA_CAPTURE|LogExit: Exiting', line))
    (args.output / 'capture-log-excerpt.txt').write_text(excerpt+'\n', encoding='utf-8')
    gpu = re.search(r'LogRHI:\s+Name:\s+([^\r\n]+)', logs[0]).group(1).strip()
    date = re.search(r'\[(\d{4})\.(\d\d)\.(\d\d)-', logs[0])
    engine_sources = [
        'Engine/Source/Runtime/Renderer/Private/Lumen/LumenVisualize.cpp',
        'Engine/Shaders/Private/DistanceFieldVisualization.usf',
        'Engine/Source/Runtime/Renderer/Private/Lumen/LumenScreenProbeGather.cpp',
        'Engine/Source/Runtime/Engine/Public/Rendering/NaniteResources.h',
        'Engine/Source/Runtime/Engine/Private/NaniteVisualizationData.cpp',
        'Engine/Source/Runtime/RenderCore/Private/RenderGraphTrace.cpp',
    ]
    manifest = {
        'engine_build': json.loads((args.engine_root / 'Engine/Build/Build.version').read_text()),
        'render_api': 'Direct3D 12', 'shader_platform': 'PCD3D_SM6',
        'gpu': gpu,
        'capture_date_utc': '-'.join(date.groups()),
        'camera_screenshots': 46, 'gpu_array_displays': len(figures)-46,
        'origin': 'Locally executed source-built Unreal editor renderer. No generative imagery. Raw GPU array displays use documented decoding, crop and tone mapping.',
        'limitations': [
            'Editor captures, not packaged gameplay or performance benchmarks.',
            'Draw-request warm-up does not prove temporal convergence; stochastic lighting noise remains.',
            'One asset shared by seven strongly scaled cube components; mesh-distance fields approximate nonuniform scales.',
            'The room has only uniform screen probes in the captured placement view (3600 uniform; 0 adaptive).',
            'Surface-cache compression was disabled only for GPU atlas readback; each radiance atlas has an independent display exposure.',
            'The source-built checkout reports changelist 0; do not infer a released engine revision from that value.'
        ],
        'figures': figures,
        'engine_source_landmarks': [{'file': p, 'sha256': sha(args.engine_root / p)} for p in engine_sources],
        'reproduction_sources': [{'file': p.relative_to(here).as_posix(), 'sha256': sha(p)} for p in sorted(sources)]
    }
    (args.output / 'capture-manifest.json').write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'camera_screenshots': 46, 'atlas_displays': len(figures)-46, 'output': str(args.output)}))


if __name__ == '__main__':
    main()
