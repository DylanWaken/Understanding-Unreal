"""Verify the recorded replay against the capture bytes and its own arrays.

Checks, with no GPU work:
  * every resource/parameter file still has the recorded size and SHA-256;
  * every texture operand has an exact SubresourceLayouts entry and the recorded
    StoredFormat agrees with it, and the file size matches the stored decode;
  * every operand version is still the most recent same-graph writer before the
    pass, and the output version is the pass pointer;
  * the captured LumenCardScene payload still decodes to the recorded atlas size;
  * per-pixel comparison arrays reproduce the aggregate match counts and error
    metrics;
  * JSON/MD/SVG artifact hashes are recorded for publication.
Exit code is nonzero on any mismatch.
"""
from pathlib import Path
import argparse, hashlib, json, math, struct, sys

HERE=Path(__file__).resolve().parent
J=HERE/'cache-texel-replay.json'

def sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(4*1024*1024),b''):h.update(b)
    return h.hexdigest()

def stream(path):
    text=Path(path).read_text(encoding='utf-8-sig');d=json.JSONDecoder();p=0;o=[]
    while p<len(text):
        while p<len(text) and text[p].isspace():p+=1
        if p==len(text):break
        x,p=d.raw_decode(text,p);o.append(x)
    return o

def decode_ufloat(bits,m):
    e=bits>>m;a=bits&((1<<m)-1)
    if e==0:return math.ldexp(a,-14-m)
    if e==31:return float('inf') if a==0 else float('nan')
    return math.ldexp(1+a/(1<<m),e-15)

def decode_rgb11(v):return [decode_ufloat(v&2047,6),decode_ufloat((v>>11)&2047,6),decode_ufloat(v>>22,5)]

def read_field(meta,data,name):
    m=next(x for x in meta['Members'] if x['Name']==name)
    n=m['NumRows']*m['NumColumns']*max(m['NumElements'],1)
    fmt={'UBMT_FLOAT32':'f','UBMT_UINT32':'I','UBMT_INT32':'i'}[m['BaseType']]
    return struct.unpack_from('<'+fmt*n,data,m['Offset'])[0:n]

fails=[];checks=[]
def check(name,ok,detail=''):
    checks.append({'check':name,'ok':bool(ok),'detail':detail})
    if not ok:fails.append(name)
    print(('PASS ' if ok else 'FAIL ')+name+((' :: '+detail) if detail else ''))

parser = argparse.ArgumentParser(description="Verify the frozen Surface Cache replay against DumpGPU bytes.")
parser.add_argument("--capture", type=Path, help="reconstructed DumpGPU directory")
parser.add_argument("--output", type=Path, default=HERE / "verification.json", help="verification JSON file")
args = parser.parse_args()
x=json.loads(J.read_text())
root=args.capture or Path(x['capture'])
check('capture directory exists',root.is_dir(),str(root))
if not root.is_dir():
    print(json.dumps({'checks':checks,'fails':fails},indent=1));sys.exit(1)

# 1. Recorded input bytes.
for name,o in x['resources'].items():
    f=root/o['path']
    ok=f.is_file() and f.stat().st_size==o['bytes'] and sha(f)==o['sha256']
    check(f'resource bytes/hash {name}',ok,f"{o['path']} {o['bytes']} bytes")
for o in x['parameterPayloads']:
    f=root/o['path']
    ok=f.is_file() and f.stat().st_size==o['bytes'] and sha(f)==o['sha256']
    check(f'payload bytes/hash {o["path"]}',ok,f"{o['bytes']} bytes")

# 2. Exact layout and stored decode size.
layouts=stream(root/'Base/SubresourceLayouts.json')
byfile={Path(e['File']).name:e for e in layouts}
bytes_per_pixel={'PF_FloatR11G11B10':4,'PF_FloatRGBA':8,'PF_A32B32G32R32F':16,'PF_R32_FLOAT':4,'PF_R32_UINT':4,'PF_R32_SINT':4}
for name,o in x['resources'].items():
    if not o.get('storedFormat'):continue
    e=byfile.get(Path(o['path']).name)
    check(f'exact layout entry {name}',e is not None,Path(o['path']).name)
    if e is None:continue
    check(f'stored format {name}',e.get('StoredFormat')==o['storedFormat'],f"recorded {o['storedFormat']} layout {e.get('StoredFormat')}")
    expected=e['ExtentX']*e['ExtentY']*bytes_per_pixel[o['storedFormat']]
    check(f'stored decode size {name}',expected==o['bytes'],f'{expected} bytes expected, {o["bytes"]} recorded')

# 3. Same-run version resolution.
passes=stream(root/'Base/Passes.json');p=x['pass'];prefix=p['Pointer'][:4];vers={}
for q in passes:
    if q['Pointer']==p['Pointer']:break
    if q['Pointer'][:4]!=prefix:continue
    for rid in q['OutputResources']:vers[rid]=q['Pointer']
for name,o in x['resources'].items():
    expected=p['Pointer'] if name==x['outputShaderRoute']['outputName'] else vers.get(o['uniqueResource'])
    check(f'same-run version {name}',expected==o['version'],f"recorded {o['version']} expected {expected}")
check('output version equals pass pointer',x['resources'][x['outputShaderRoute']['outputName']]['version']==p['Pointer'])
# Every producer must actually list the operand and execute before the consumer.
order={q['Pointer']:i for i,q in enumerate(passes)}
for name,o in x['resources'].items():
    prod=next((q for q in passes if q['Pointer']==o['version']),None)
    check(f'producer pass exists {name}',prod is not None,o['version'])
    if prod is None:continue
    check(f'producer lists operand {name}',o['uniqueResource'] in prod['OutputResources'],f"pass {prod['Id']} {prod['EventName']}")
    check(f'producer precedes consumer {name}',order[prod['Pointer']]<=order[p['Pointer']],f"{prod['Pointer']} before {p['Pointer']}")

# 4. Captured LumenCardScene payload and atlas consistency.
rdg=x['lumenCardScene']
if rdg.get('status')=='captured':
    cm=root/'StructuresMetadata'/(rdg['metadata']+'.json');cb=root/'Structures'/(rdg['content']+'.bin')
    meta=json.loads(cm.read_text());data=cb.read_bytes()
    check('LumenCardScene payload size',len(data)==rdg['byteSize'],f"{len(data)} bytes")
    physical=read_field(meta,data,'PhysicalAtlasSize');invc=read_field(meta,data,'InvPhysicalAtlasSize')
    check('LumenCardScene PhysicalAtlasSize',list(physical)==rdg['fields']['PhysicalAtlasSize']['value'],str(list(physical)))
    check('LumenCardScene InvPhysicalAtlasSize',list(invc)==rdg['fields']['InvPhysicalAtlasSize']['value'],str(list(invc)))
    direct=x['resources']['Lumen.SceneDirectLighting']
    check('captured atlas size matches direct atlas extent',list(physical)==direct['layoutExtent'],f'{list(physical)} vs {direct["layoutExtent"]}')
else:
    check('LumenCardScene captured',False,rdg.get('status',''))

# 5. Per-pixel arrays reproduce aggregates.
val=x['validation'];comps=x['comparisons'];tile=x['tileValidation']
check('comparison count',len(comps)==val['examinedTexels'],f"{len(comps)} arrays vs {val['examinedTexels']} declared")
check('truncation matches from arrays',sum(c['truncatedMatch'] for c in comps)==val['truncationMatches'],str(val['truncationMatches']))
check('nearest matches from arrays',sum(c['nearestMatch'] for c in comps)==val['nearestMatches'],str(val['nearestMatches']))
check('tile pixel count',len(tile['pixels'])==tile['examinedPixels'],f"{len(tile['pixels'])} vs {tile['examinedPixels']}")
check('tile truncation matches from arrays',sum(c['truncatedMatch'] for c in tile['pixels'])==tile['truncationMatches'],str(tile['truncationMatches']))
check('tile nearest matches from arrays',sum(c['nearestMatch'] for c in tile['pixels'])==tile['nearestMatches'],str(tile['nearestMatches']))
terr=[v for c in comps for v in c['absoluteFloatError']]
check('error metric channel count',len(terr)==val['errorMetrics']['truncatingModelVsActualRGB']['channelCount'],str(len(terr)))
check('error metric max',abs(max(terr)-val['errorMetrics']['truncatingModelVsActualRGB']['maxAbsChannelError'])<1e-15,f'{max(terr)}')
check('error metric mean',abs(sum(terr)/len(terr)-val['errorMetrics']['truncatingModelVsActualRGB']['meanAbsChannelError'])<1e-15,f'{sum(terr)/len(terr)}')
nerr=[abs(a-b) for c in comps for a,b in zip(decode_rgb11(c['predictedNearestPacked']),decode_rgb11(c['outputPacked']))]
check('nearest error metric max',abs(max(nerr)-val['errorMetrics']['nearestModelVsActualRGB']['maxAbsChannelError'])<1e-15,f'{max(nerr)}')
check('nearest error metric mean',abs(sum(nerr)/len(nerr)-val['errorMetrics']['nearestModelVsActualRGB']['meanAbsChannelError'])<1e-15,f'{sum(nerr)/len(nerr)}')

# 6. Sample consistency and artifact hashes.
s=x['sample']
check('sample truncated match',(s['predictedTruncatedPacked']==s['outputPacked'])==s['truncatedMatch'],str(s['truncatedMatch']))
check('sample nearest match',(s['predictedNearestPacked']==s['outputPacked'])==s['nearestMatch'],str(s['nearestMatch']))
check('sample is brightest comparison',s['score']==max(c['score'] for c in comps),f"score {s['score']}")
artifacts={}
for name in ['cache-texel-replay.json','cache-texel-replay.md','cache-texel-flow.svg']:
    f=HERE/name
    artifacts[name]={'bytes':f.stat().st_size,'sha256':sha(f)} if f.exists() else None
    check(f'artifact exists {name}',f.exists())
check('artifacts distinct hashes',len({a['sha256'] for a in artifacts.values() if a})==sum(1 for a in artifacts.values() if a))
verification={'replay':str(J),'capture':str(root),'checks':checks,'fails':fails,'artifacts':artifacts,'resourceHashes':{n:o['sha256'] for n,o in x['resources'].items()},'payloadHashes':{o['path']:o['sha256'] for o in x['parameterPayloads']}}
args.output.write_text(json.dumps(verification,indent=2),encoding='utf-8')
print(json.dumps({'checks':len(checks),'fails':fails,'verification':str(args.output)}))
sys.exit(1 if fails else 0)
