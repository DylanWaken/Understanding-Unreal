"""Validate and publish measured extended-room captures, without image editing."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import struct
import subprocess

HERE = Path(__file__).resolve().parent

def sha(path, text=False):
    data=path.read_bytes()
    return hashlib.sha256(data.replace(b'\r\n',b'\n') if text else data).hexdigest()

def require(value,message):
    if not value:
        raise ValueError(message)

def verify(source, published=False):
    report=json.loads((source/'scene-room-capture-report.json').read_text(encoding='utf-8'))
    inventory=json.loads((source/'scene-room-inventory.json').read_text(encoding='utf-8'))
    require(len(report['captures'])==12,'Expected all 12 captures')
    for capture in report['captures']:
        path=source/(capture['name']+'.png')
        data=path.read_bytes()
        require(data[:8]==b'\x89PNG\r\n\x1a\n',f'Invalid PNG: {path}')
        require(struct.unpack('>II',data[16:24])==(1280,720),f'Wrong dimensions: {path}')
        require(sha(path)==report['image_sha256'][path.name],f'Changed screenshot: {path}')
        require(capture['hinge_location_cm']==[-150.,200.,0.],'Door hinge moved')
        expected=[-90.,200.,150.] if capture['door_yaw_degrees']==0 else [-150.,260.,150.]
        require(all(abs(a-b)<1e-5 for a,b in zip(expected,capture['indicator_world_location_cm'])),'Indicator did not follow hinge transform')
        require(capture['character_world_location_cm']==[65.,110.,0.],'Character root unexpectedly translated')
    instances=[c['instance_count'] for a in inventory['actors'] for c in a['components'] if 'instance_count' in c]
    require(sorted(instances)==[3,3,24],'Wrong instance inventory')
    skeletal=[c for a in inventory['actors'] for c in a['components'] if 'skeletal_mesh' in c]
    require(len(skeletal)==1 and skeletal[0]['bone_count']==68,'Wrong skeletal inventory')
    if published:
        require(report['source_script_sha256_lf_normalized']==sha(HERE/'capture_scene_room.py',True),'Published capture script changed')
    else:
        # Capture records exact bytes. Validate those bytes before deriving a
        # checkout-portable hash; never reinterpret the historical raw digest.
        require(report['source_script_sha256']==sha(HERE/'capture_scene_room.py'),'Capture script bytes changed after recorded run')
    return report,inventory

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('lab_root',type=Path)
    parser.add_argument('output',type=Path)
    parser.add_argument('--engine-root',type=Path,default=Path('D:/UnrealEngine'))
    parser.add_argument('--check-published',action='store_true')
    args=parser.parse_args()
    if args.check_published:
        report,inventory=verify(args.output,published=True)
        provenance=json.loads((args.output/'scene-room-provenance.json').read_text())
        for relative,digest in provenance['source_sha256_lf_normalized'].items():
            require(sha(HERE/relative,True)==digest,f'Published source changed: {relative}')
        print('Validated 12 Unreal screenshots, attachment transforms, 30 instances, 68-bone skeleton, and reproduction source hashes.')
        return
    source=args.lab_root/'scene-room-captures'
    report,inventory=verify(source)
    report['source_script_sha256_encoding']='Exact capture-time file bytes; original line endings preserved.'
    report['source_script_sha256_lf_normalized']=sha(HERE/'capture_scene_room.py',True)
    report['source_script_canonical_encoding']='UTF-8 source bytes with CRLF replaced by LF; used for published-checkout verification.'
    baseline=json.loads((args.lab_root/'captures/scene-inventory.json').read_text(encoding='utf-8'))
    old={a['label']:a for a in baseline['actors'] if a['class']=='StaticMeshActor'}
    new={a['label']:a for a in inventory['actors'] if a['class']=='StaticMeshActor'}
    require(len(old)==7 and set(old)==set(new),'Original room geometry changed')
    for label,a in old.items():
        b=new[label]
        require(a['location_cm']==b['location_cm'] and a['rotation_degrees']==b['rotation_degrees'],f'Original transform changed: {label}')
        ac=next(c for c in a['components'] if c['class']=='StaticMeshComponent')
        bc=next(c for c in b['components'] if c['class']=='StaticMeshComponent')
        require(ac['scale']==bc['scale'] and ac['mesh']==bc['mesh'] and ac['materials']==bc['materials'],f'Original geometry/material changed: {label}')
    log=(args.lab_root/'capture_scene_room.py.stdout.log').read_text(encoding='utf-8',errors='replace')
    require('LogPython: Error:' not in log,'Python errors in capture log')
    gpu=re.search(r'LogRHI:\s+Name:\s+([^\r\n]+)',log)
    provenance={
        'engine_git_commit':subprocess.check_output(['git','-C',str(args.engine_root),'rev-parse','HEAD'],text=True).strip(),
        'gpu':gpu.group(1).strip() if gpu else 'See Unreal capture log',
        'rhi':'Direct3D 12, SM6',
        'capture_report':'scene-room-capture-report.json',
        'scene_inventory':'scene-room-inventory.json',
        'baseline_verification':'All seven original StaticMeshActor transforms, mesh references, and materials match the recorded LumenRoom inventory.',
        'source_sha256_lf_normalized':{str(path.relative_to(HERE)).replace('\\','/'):sha(path,True) for path in [HERE/'capture_scene_room.py',HERE/'publish_scene_room.py',HERE/'run.ps1',HERE/'Source/ArdaLabCapture/ArdaLabCapture.h',HERE/'Source/ArdaLabCapture/ArdaLabCapture.cpp']},
        'template_asset_sha256':{},
        'publication':'Screenshots are unchanged Unreal viewport output. Epic mannequin template assets are used locally and are not redistributed. Procedural door and foliage are created by the published script.',
        'limitations':['Frozen animation samples, not an Animation Blueprint/controller, cloth, or physics demonstration.','Three instancing components contain a total of 30 instances; instance count is not draw-call count.','Leaf opacity masks are analytic material masks on planar triangles. This scene does not demonstrate WPO wind.','The attached indicator is emissive geometry, not a LightComponent.','Stochastic lighting noise remains; warm-up counts do not establish convergence.']
    }
    for relative in ['Character/Mesh/SK_Mannequin.uasset','Character/Mesh/SK_Mannequin_Skeleton.uasset','Animations/ThirdPersonWalk.uasset']:
        path=args.engine_root/'Templates/TemplateResources/Standard/Mannequin/Content'/relative
        provenance['template_asset_sha256'][relative]=sha(path)
    args.output.mkdir(parents=True,exist_ok=True)
    for name in ['scene-room-inventory.json',*report['image_sha256']]:
        shutil.copyfile(source/name,args.output/name)
    (args.output/'scene-room-capture-report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    (args.output/'scene-room-provenance.json').write_text(json.dumps(provenance,indent=2)+'\n',encoding='utf-8')
    print(f'Published 12 verified Unreal screenshots and 3 metadata files to {args.output}')

if __name__=='__main__':
    main()
