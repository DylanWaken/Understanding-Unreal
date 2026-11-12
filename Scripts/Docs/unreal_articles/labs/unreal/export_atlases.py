"""Decode selected UE DumpGPU readbacks into documented scientific displays.
Requires NumPy and Pillow. This renders measured arrays, not synthetic caches.
"""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
from PIL import Image

def objects(path):
    s=path.read_text(encoding='utf-8-sig'); d=json.JSONDecoder(); i=0
    while i<len(s):
        if s[i].isspace():i+=1;continue
        v,i=d.raw_decode(s,i)
        yield v

def small_float(bits,mantissa):
    exponent=bits>>mantissa; fraction=bits&((1<<mantissa)-1)
    return np.where(exponent==0,np.ldexp(fraction.astype(np.float32),1-15-mantissa),np.ldexp(1+fraction.astype(np.float32)/(1<<mantissa),exponent.astype(np.int32)-15))

def decode(path,desc):
    h,w=desc['ExtentY'],desc['ExtentX']; fmt=desc['Format']
    if fmt=='PF_R8G8B8A8':return np.fromfile(path,np.uint8).reshape(h,w,4)[...,:3]/255
    if fmt=='PF_R8G8':
        a=np.fromfile(path,np.uint8).reshape(h,w,2)/255
        return np.concatenate((a,np.zeros((h,w,1))),axis=-1)
    if fmt=='PF_R32_FLOAT':
        a=np.fromfile(path,np.float32).reshape(h,w)
        return np.repeat(a[...,None],3,axis=2)
    if fmt=='PF_G16':
        a=np.fromfile(path,np.uint16).reshape(h,w)/65535
        return np.repeat(a[...,None],3,axis=2)
    if fmt=='PF_FloatR11G11B10':
        a=np.fromfile(path,np.uint32).reshape(h,w)
        return np.stack((small_float(a&2047,6),small_float((a>>11)&2047,6),small_float((a>>22)&1023,5)),axis=-1)
    raise ValueError(fmt)

def main():
    p=argparse.ArgumentParser();p.add_argument('dump',type=Path);p.add_argument('output',type=Path);args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    requested={
        'Lumen.SceneAlbedo':'gpu-surface-albedo',
        'Lumen.SceneNormal':'gpu-surface-normal',
        'Lumen.SceneDepth':'gpu-surface-depth',
        'Lumen.SceneFinalLighting':'gpu-surface-lighting',
        'Lumen.RadianceCache.FinalRadianceAtlas':'gpu-radiance-cache',
        'Lumen.ScreenProbeGather.TraceRadiance':'gpu-screen-trace-radiance',
        'Lumen.ScreenProbeGather.ScreenProbeFilteredRadiance':'gpu-screen-filtered-radiance',
    }
    descriptors=list(objects(args.dump/'Base/ResourceDescs.json'))
    report={'origin':'Unreal Engine 5.8.1 DumpGPU actual GPU readback','surface_cache_compression':0,'note':'Compression disabled for this diagnostic dump only; no post-hoc reconstruction of cache contents. Display transforms and crop coordinates are recorded per image.','images':[]}
    for name,stem in requested.items():
        choices=[d for d in descriptors if d['Name']==name]
        if not choices:continue
        # Select the highest-resolution resource where multiple cache families
        # have the same debug name (screen-gather vs translucency cache).
        desc=max(choices,key=lambda d:d['ExtentX']*d['ExtentY'])
        files=sorted((args.dump/'Resources').glob(desc['UniqueResourceName']+'.mip0.*.bin'))
        if not files:continue
        # Dump versions encode pass addresses, not chronological order. Select
        # the last version referenced by the recorded RDG pass sequence.
        version=files[-1]
        for event in objects(args.dump/'Base/Passes.json'):
            for resource in event.get('OutputResources',[]):
                if resource==desc['UniqueResourceName']+'.mip0':
                    candidate=args.dump/'Resources'/(resource+'.v'+event['Pointer']+'.bin')
                    if candidate.exists():version=candidate
        a=decode(version,desc);a=np.nan_to_num(a,nan=0,posinf=0,neginf=0)
        active=(np.max(a,axis=-1)<.99999) if desc['Format']=='PF_G16' else (np.max(a,axis=-1)>1e-6)
        yy,xx=np.where(active)
        if len(xx):
            x0,x1=max(0,int(xx.min())-4),min(a.shape[1],int(xx.max())+5)
            y0,y1=max(0,int(yy.min())-4),min(a.shape[0],int(yy.max())+5)
        else:x0,y0,x1,y1=0,0,a.shape[1],a.shape[0]
        crop=a[y0:y1,x0:x1]
        exposure=1.0
        if desc['Format']=='PF_FloatR11G11B10':
            positive=crop[crop>1e-6]
            exposure=1/max(float(np.percentile(positive,95)) if positive.size else 1,1e-9)
            b=crop*exposure; b=b/(1+b)
            display=np.where(b<=.0031308,12.92*b,1.055*np.power(b,1/2.4)-.055)
            transform='per-texture exposure times linear RGB, Reinhard x/(1+x), then IEC sRGB transfer; not comparable brightness across atlases'
        elif desc['Format'] in ('PF_R32_FLOAT','PF_G16'):
            display=np.clip(crop,0,1)
            transform='stored normalized depth shown as grayscale; not linear world distance'
        else:
            display=np.clip(crop,0,1)
            transform='stored channels shown directly as RGB; packed normal RG has display blue=0'
        im=Image.fromarray(np.uint8(np.clip(display,0,1)*255+.5))
        im.save(args.output/(stem+'.png'),optimize=True)
        report['images'].append({'file':stem+'.png','resource':name,'format':desc['Format'],'extent':[desc['ExtentX'],desc['ExtentY']],'crop_xyxy':[x0,y0,x1,y1],'display_transform':transform,'exposure':exposure,'raw_file':version.name,'raw_sha256':hashlib.sha256(version.read_bytes()).hexdigest(),'active_texels':int(active.sum())})
    (args.output/'gpu-atlas-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
