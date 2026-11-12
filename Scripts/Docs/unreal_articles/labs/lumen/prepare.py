"""Order authored sections and refresh source-evidence hashes from local UE."""
from pathlib import Path
import re,json,hashlib
HERE=Path(__file__).resolve().parent
ARTICLES=HERE.parents[1]
chapter=ARTICLES/'chapters/lumen-lab.html'
text=chapter.read_text(encoding='utf-8')
intro=text.split('<section class="chapter"',1)[0]
sections=dict(re.findall(r'(<section class="chapter" id="([^"]+)">.*?</section>)',text,re.S))
byid={id:html for html,id in sections.items()}
order=['room','equation','cards','distance','sampling','direct','radiosity','world-probes','screen-probes','reflections','temporal','implementation','verification']
assert len(byid)==len(order)
out=intro+'\n\n'.join(byid['lumen-lab-'+id] for id in order)+'\n'
out=out.replace('class="data-table"','class="reference-table"').replace('class="table-scroll"','class="table-wrap"')
chapter.write_text(out,encoding='utf-8',newline='\n')
UE=Path('D:/UnrealEngine')
R='Engine/Source/Runtime/Renderer/Private/Lumen/'
S='Engine/Shaders/Private/Lumen/'
items=[
 ('ll-card',R+'LumenSceneData.h',307,350,'FLumenCard stores local/world capture placement, allocated levels, mip mappings, and ownership indices.'),
 ('ll-frame-temporaries',R+'LumenSceneData.h',996,1024,'Frame temporaries expose captured material/depth atlases, separate lighting atlases, temporal statistics, and addressing resources.'),
 ('ll-capture-targets',R+'LumenSceneCardCapture.cpp',537,582,'Card capture allocates material targets and a depth-stencil atlas.'),
 ('ll-radiosity',R+'LumenRadiosity.cpp',20,48,'Radiosity is a Surface Cache indirect gather providing multibounce diffuse; sampling and spatial filtering have explicit quality tradeoffs.'),
 ('ll-radiance-update',R+'LumenRadianceCache.h',107,122,'Radiance cache update places or reuses marked probes, traces a subset, and provides interpolation resources.'),
 ('ll-radiance-parameters',R+'LumenRadianceCacheInterpolation.h',44,69,'Radiance-cache interpolation carries indirection, radiance/irradiance, validity, occlusion/depth, clipmap transforms, and pre-exposure.'),
 ('ll-screen-parameters',R+'LumenScreenProbeGather.h',30,120,'Screen-probe declarations distinguish irradiance formats, tracing/gathering dimensions, reconstruction policy, and structured importance samples.'),
 ('ll-screen-trace',S+'LumenScreenProbeTracing.usf',54,79,'The shader provides a distinct screen-texture trace entry point for probe rays.'),
 ('ll-compact',S+'LumenScreenProbeTracing.usf',421,439,'Trace compaction tests unresolved trace state and radiance-cache completion.'),
 ('ll-mesh-sdf',S+'LumenScreenProbeTracing.usf',549,565,'Mesh distance field tracing has a separate screen-probe implementation path.'),
 ('ll-voxel-trace',S+'LumenScreenProbeTracing.usf',754,785,'Valid radiance-cache coverage limits trace distance before cone tracing scene voxels.'),
 ('ll-lighting-pdf',S+'LumenScreenProbeImportanceSampling.usf',139,190,'Lighting luminance is reduced and normalized into a directional sampling PDF.'),
 ('ll-reflection-vndf',S+'LumenReflections.usf',370,394,'The inspected reflection path uses material roughness, optional anisotropy, visible-GGX half-vector sampling, and reflected ray direction.'),
 ('ll-temporal',S+'LumenScreenProbeFiltering.usf',190,253,'Temporal probe filtering has prior depth, radiance, world position, history projection, and bounds checks.'),
]
records=[]
for id,path,a,b,claim in items:
    lines=(UE/path).read_text(encoding='utf-8-sig').splitlines()
    digest=hashlib.sha256('\n'.join(lines[a-1:b]).encode()).hexdigest()
    records.append(dict(id=id,path=path,line=a,end_line=b,claim=claim,sha256=digest,repository='unreal'))
(ARTICLES/'evidence/lumen-lab.json').write_text(json.dumps(records,indent=2)+'\n',encoding='utf-8')
print('chapters',len(order),'words',len(re.findall(r'\b\w+\b',re.sub('<[^>]+>',' ',out))))
