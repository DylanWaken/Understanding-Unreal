"""Verify the rewritten essay's frozen experiment and diagram evidence offline."""
from pathlib import Path
import hashlib
import json
import math
import statistics
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
ASSETS = ROOT / 'Docs/Unreal/assets/lumen-v2'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    reference = HERE / 'labs/lumen-pathtracer'
    manifest = read(reference / 'reference/manifest.json')
    for record in manifest['files'].values():
        path = reference / record['path']
        assert path.stat().st_size == record['bytes'], path
        assert sha(path) == record['sha256'], path
    for source, published in {
        'reference/out/reference_render.png': 'reference_render.png',
        'reference/out/reference_linear.pfm': 'reference_linear.pfm',
        'reference/manifest.json': 'cpu-reference-manifest.json',
        'reference/out/checks.json': 'estimator-checks.json',
    }.items():
        assert sha(reference / source) == sha(ASSETS / published), published
    checks = read(reference / 'reference/out/checks.json')
    assert checks['overall'] == 'pass'
    assert len(checks['checks']) == 12
    assert all(check['status'] == 'pass' for check in checks['checks'])

    diagrams = ASSETS / 'diagrams'
    inventory = read(diagrams / 'manifest.json')
    assert inventory['not_runtime_images'] is True
    assert sha(ROOT / inventory['generator']['path']) == inventory['generator']['sha256']
    for name, expected in inventory['files'].items():
        assert sha(diagrams / name) == expected, name
    assert {p.name for p in diagrams.iterdir() if p.is_file()} == set(inventory['files']) | {'manifest.json'}
    series = read(diagrams / 'operator-convergence.json')
    exact = (1 / (1 - .2 * .3), .3 / (1 - .2 * .3))
    a = b = (1., 0.)
    for index, row in enumerate(series['iterations']):
        assert row['iteration'] == index
        for key, values in [('exact_model_error', a), ('changed_model_error', b)]:
            expected = max(abs(values[i] - exact[i]) for i in range(2))
            assert math.isclose(row[key], expected, rel_tol=1e-12, abs_tol=1e-14), (index, key)
        a = (1 + .2 * a[1], .3 * a[0])
        b = (1 + .2 * b[1], .25 * b[0])
    assert series['iterations'][-1]['changed_model_error'] > .05

    baseline = HERE / 'labs/lumen-capture/baseline'
    result = subprocess.run([sys.executable, str(baseline / 'reproduce/scripts/verify_package.py')],
                            cwd=baseline, capture_output=True, text=True, encoding='utf-8')
    if result.returncode:
        raise AssertionError(result.stderr[-4000:])
    verified = json.loads(result.stdout)
    assert verified['status'] == 'ok' and not verified['failed_checks']
    image_map = read(baseline / 'data/reference/reference-image-map.json')
    assert sha(ASSETS / 'reference-image-map.json') == sha(baseline / 'data/reference/reference-image-map.json')
    for image in image_map['images']:
        assert sha(ASSETS / Path(image['figure']).name) == image['sha256']
    subprocess.run([sys.executable, str(HERE / 'labs/lumen-frame/validate_schema.py')], check=True)
    profile_dir=ASSETS / 'profile'
    profile=read(profile_dir / 'profile-result.json')
    samples=profile['samples']
    assert len(samples)==7 and len({s['engineFrame'] for s in samples})==7
    for name,record in profile['rawFiles'].items():
        assert (profile_dir/name).stat().st_size==record['bytes']
        assert sha(profile_dir/name)==record['sha256']
    def check_summary(values,actual):
        expected={'n':len(values),'min':min(values),'median':statistics.median(values),'max':max(values)}
        for key,value in expected.items():
            assert math.isclose(actual[key],value,abs_tol=1e-12),(key,actual,expected)
    check_summary([s['graphicsBusyMs'] for s in samples],profile['summary']['graphicsQueueBusyMs'])
    check_summary([s['graphicsBusyAndWaitMs'] for s in samples],profile['summary']['graphicsQueueBusyAndWaitMs'])
    assert all(len(s['sequentialSceneSpansMs'])==3 for s in samples)
    check_summary([v for s in samples for v in s['sequentialSceneSpansMs']],profile['summary']['oneSceneRenderSubtreeMs'])
    for name,summary in profile['summary']['namedMaximumSingleOccurrencePerFrameMs'].items():
        assert all(len(s['namedInclusiveEventSpansMs'][name])==3 for s in samples)
        check_summary([max(s['namedInclusiveEventSpansMs'][name]) for s in samples],summary)
    frame=HERE/'labs/lumen-frame'
    package=ROOT/'Docs/Unreal/assets/lumen-frame'
    package_manifest=read(frame/'publication/archive-manifest.json')
    package_files={p.relative_to(package).as_posix() for p in package.rglob('*') if p.is_file()}
    assert package_files=={r['path'] for r in package_manifest['entries']}
    for row in package_manifest['entries']:
        path=package/row['path']
        assert path.resolve().is_relative_to(package.resolve())
        assert path.stat().st_size==row['bytes'] and sha(path)==row['sha256'],path
    subprocess.run([sys.executable,str(frame/'verify_frame_evidence.py'),
                    '--package',str(package),
                    '--lab',str(frame),'--repo-root',str(ROOT),
                    '--artifact-manifest',str(frame/'artifact-manifest.json')],check=True)
    diagram_count=sum(name.endswith('.svg') for name in inventory['files'])
    print(f'PASS: CPU evidence, {diagram_count} diagrams, convergence arithmetic, reference image provenance; '
          f'{verified["checks_passed"]} portable capture checks.')


if __name__ == '__main__':
    main()
