"""Verify saved assets/evidence; optionally recompute twice to check determinism."""
from pathlib import Path
import argparse,hashlib,json,subprocess,sys,xml.etree.ElementTree as ET
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[4]
OUT=ROOT/'Docs/Unreal/assets/lumen-lab'
parser=argparse.ArgumentParser()
parser.add_argument('--rerun',action='store_true')
parser.add_argument('--source-root',type=Path)
args=parser.parse_args()
if args.rerun:
    digests=[]
    for run in range(2):
        subprocess.run([sys.executable,str(HERE/'render.py')],check=True,stdout=subprocess.DEVNULL)
        digests.append({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.iterdir() if p.suffix in {'.png','.svg','.json','.npz'}})
    assert digests[0]==digests[1],'Rendered assets differ between identical seeded runs'
    print('Two renders: every PNG/SVG/JSON/NPZ is byte-identical')
for p in OUT.glob('*.svg'):
    root=ET.parse(p).getroot();ns='{http://www.w3.org/2000/svg}'
    assert root.get('viewBox') and root.find(ns+'title') is not None and root.find(ns+'desc') is not None,p
metrics=json.loads((OUT/'metrics.json').read_text())
assert metrics['sdf_hit_mismatches']==0
assert metrics['sdf_unresolved_test_rays']==0
assert metrics['grazing_regression']['status']=='budget_exhausted'
assert metrics['grazing_regression']['exact_hit_t_m']>metrics['grazing_regression']['last_sample_t_m']
assert metrics['temporal_accepted_cross_face_samples']==0
assert metrics['temporal_cross_face_rejections']>0
assert metrics['sdf_max_hit_distance_error_m']<.001
assert metrics['max_diffuse_reflectance']*metrics['max_transport_row_sum']<1
assert metrics['iteration_max_changes'][-1]<1e-6
if args.source_root:
    records=json.loads((HERE.parents[1]/'evidence/lumen-lab.json').read_text())
    for r in records:
        lines=(args.source_root/r['path']).read_text(encoding='utf-8-sig').splitlines()
        actual=hashlib.sha256('\n'.join(lines[r['line']-1:r['end_line']]).encode()).hexdigest()
        assert actual==r['sha256'],r['id']
    print(f'{len(records)} Unreal source ranges verified')
print('Saved metrics, contraction bound, and accessible SVG metadata verified')
