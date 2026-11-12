"""Replay one recorded CombineLumenSceneLightingCS dispatch on CPU.

Independent explanatory implementation; no Unreal source redistributed.
Input is an unmodified DumpGPU directory. Every chosen resource version is
resolved by unique ID and preceding writer, never by resource name alone.

This run is against the instrumented new capture: texture decoding follows the
exact per-file SubresourceLayouts.StoredFormat (BC views may be expanded to
float32 rather than half), and the LumenCardScene RDG uniform-buffer payload is
read from the captured RDGUniformBufferParameters mapping rather than inferred.
"""
from pathlib import Path
import argparse, hashlib, json, math, struct
import numpy as np

F=np.float32
ZERO='00000000000000000000'

def stream(path):
    text=Path(path).read_text(encoding='utf-8-sig'); dec=json.JSONDecoder(); pos=0; out=[]
    while pos<len(text):
        while pos<len(text) and text[pos].isspace(): pos+=1
        if pos==len(text): break
        obj,pos=dec.raw_decode(text,pos); out.append(obj)
    return out

def sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(4*1024*1024),b''): h.update(block)
    return h.hexdigest()

def pointer(data,offset,prefix):
    v=struct.unpack_from('<Q',data,offset)[0]
    return prefix+f'{v:016x}' if v else ZERO

def read_field(meta,data,name):
    """Return the scalar/list value of a captured UB member, or raise."""
    m=next(x for x in meta['Members'] if x['Name']==name)
    n=m['NumRows']*m['NumColumns']*max(m['NumElements'],1)
    fmt={'UBMT_FLOAT32':'f','UBMT_UINT32':'I','UBMT_INT32':'i'}[m['BaseType']]
    v=struct.unpack_from('<'+fmt*n,data,m['Offset'])
    return v[0] if n==1 else list(v)

def decode_ufloat(bits,m):
    e=bits>>m; a=bits&((1<<m)-1)
    if e==0:return math.ldexp(a,-14-m)
    if e==31:return float('inf') if a==0 else float('nan')
    return math.ldexp(1+a/(1<<m),e-15)

def decode_rgb11(v):
    return np.array([decode_ufloat(v&2047,6),decode_ufloat((v>>11)&2047,6),decode_ufloat(v>>22,5)],dtype=np.float32)

def encode_ufloat(v,m,mode):
    if v<=0:return 0
    b=struct.unpack('<I',struct.pack('<f',float(v)))[0]
    e=(b>>23)&255
    if e<113:
        q=float(v)*2**(14+m);return int(round(q) if mode=='nearest' else math.floor(q))
    mant=b&0x7fffff;shift=23-m
    if mode=='nearest':mant += (1<<(shift-1))-1+((mant>>shift)&1)
    return ((e-112)<<m)+(mant>>shift)

def encode_rgb11(v,mode='nearest'):
    return encode_ufloat(v[0],6,mode)|(encode_ufloat(v[1],6,mode)<<11)|(encode_ufloat(v[2],5,mode)<<22)

def pcg16(p):
    v=[(int(x)*1664525+1013904223)&0xffffffff for x in p]
    trace=[list(v)]
    for i in [0,1,2,0,1,2]:
        v[i]=(v[i]+v[(i+1)%3]*v[(i+2)%3])&0xffffffff;trace.append(list(v))
    return [x>>16 for x in v],trace

# StoredFormat -> (numpy dtype, channel count). These are the exact formats the
# exporter wrote, not the native storage format.
STORED_DTYPES={
    'PF_FloatR11G11B10':('<u4',1),
    'PF_FloatRGBA':('<f2',4),
    'PF_A32B32G32R32F':('<f4',4),
    'PF_R32_FLOAT':('<f4',1),
    'PF_R32_UINT':('<u4',1),
    'PF_R32_SINT':('<i4',1),
}

class Texture:
    def __init__(self,path,layout):
        self.path=path;self.layout=layout
        self.w=int(layout['ExtentX']);self.h=int(layout['ExtentY'])
        self.stored=layout['StoredFormat'];self.native=layout['NativeFormat']
        if self.stored not in STORED_DTYPES:
            raise ValueError('unsupported stored format '+self.stored)
        dt,ch=STORED_DTYPES[self.stored]
        expected=self.w*self.h*ch*np.dtype(dt).itemsize
        actual=Path(path).stat().st_size
        if expected!=actual:
            raise ValueError(f'{path}: stored format {self.stored} implies {expected} bytes, file has {actual}')
        shape=(self.h,self.w) if ch==1 else (self.h,self.w,ch)
        self.data=np.memmap(path,dtype=dt,mode='r',shape=shape)
        self.channels=ch
    def pixel(self,x,y):
        x=max(0,min(self.w-1,x));y=max(0,min(self.h-1,y))
        if self.stored=='PF_FloatR11G11B10':
            return decode_rgb11(int(self.data[y,x]))
        v=np.array(self.data[y,x],dtype=np.float32)
        if self.channels==1:return np.repeat(v,3)
        v=v[:3]
        if self.native=='PF_BC7' and self.layout.get('NativeFormatExpanded'):
            # The decoder produced expanded float channels from 8-bit UNORM BC7.
            # Recover the exact 8-bit decoded value before using it as the shader's
            # float sample. BC6H is HDR and must not be rounded to 8 bits.
            v=np.round(v*F(255)).astype(np.float32)/F(255)
        return v
    def sample(self,uv):
        q=np.array(uv,dtype=np.float32)*np.array([self.w,self.h],dtype=np.float32)-F(.5)
        lo=np.floor(q).astype(int);t=q-lo
        weights=[(1-t[0])*(1-t[1]),t[0]*(1-t[1]),(1-t[0])*t[1],t[0]*t[1]]
        coords=[lo,lo+[1,0],lo+[0,1],lo+[1,1]]
        values=[self.pixel(*xy) for xy in coords]
        result=sum((F(w)*v for w,v in zip(weights,values)),np.zeros(3,dtype=np.float32))
        return result,{'uv':list(map(float,uv)),'texelSpace':q.tolist(),'coordinates':[x.tolist() for x in coords],'weights':list(map(float,weights)),'values':[v.tolist() for v in values]}

def replay(root,out,pass_id=None):
    root=Path(root);out=Path(out);out.mkdir(parents=True,exist_ok=True)
    passes=stream(root/'Base/Passes.json')
    desc={x['UniqueResourceName']:x for x in stream(root/'Base/ResourceDescs.json')}
    layouts=stream(root/'Base/SubresourceLayouts.json')
    layouts_by_file={Path(e['File']).name:e for e in layouts}
    p=next(x for x in passes if x['EventName']=='CombineLighting CS' and (pass_id is None or x['Id']==pass_id))
    prefix=p['Pointer'][:4];versions={}
    for prior in passes:
        if prior['Pointer']==p['Pointer']:break
        if prior['Pointer'][:4]!=prefix:continue
        for rid in prior['OutputResources']:versions[rid]=prior['Pointer']
    operands={}
    def base_rid(rid):return rid.split('.mip')[0].split('.[')[0]
    def resource(name,output=False):
        options=[]
        for rid in dict.fromkeys(p['OutputResources' if output else 'InputResources']):
            d=desc.get(base_rid(rid))
            if d and d['Name']==name:options.append((rid,d))
        assert len(options)==1,(name,options)
        rid,d=options[0];version=p['Pointer'] if output else versions.get(rid,ZERO)
        path=root/'Resources'/(rid+'.v'+version+'.bin')
        assert path.exists(),path
        is_texture=d.get('Desc')!='FRDGBufferDesc'
        layout=layouts_by_file.get(path.name)
        if is_texture:
            assert layout is not None,('no SubresourceLayouts entry for exact file',path.name)
        operands[name]={'uniqueResource':rid,'version':version,'path':str(path.relative_to(root)),'sourceFormat':d.get('Format'),'bytes':path.stat().st_size,'sha256':sha(path),'producerPass':next(({'id':q['Id'],'name':q['EventName'],'pointer':q['Pointer']} for q in passes if q['Pointer']==version),None)}
        if is_texture:
            operands[name].update({'nativeFormat':layout.get('NativeFormat'),'storedFormat':layout.get('StoredFormat'),'stagingFormat':layout.get('StagingFormat'),'nativeValueInterpretation':layout.get('NativeValueInterpretation'),'bitExact':layout.get('BitExact'),'storedBytesAreNativeStorage':layout.get('StoredBytesAreNativeStorage'),'layoutExtent':[layout.get('ExtentX'),layout.get('ExtentY')],'layoutFile':layout['File'],'dumpStatus':layout.get('DumpStatus')})
        else:
            operands[name].update({'desc':d.get('Desc'),'bytesPerElement':d.get('BytesPerElement'),'numElements':d.get('NumElements')})
            assert path.stat().st_size==d.get('ByteSize'),('buffer byte size disagrees with descriptor',path.name,path.stat().st_size,d.get('ByteSize'))
        return path,layout
    mp=root/'StructuresMetadata'/(p['ParametersMetadata']+'.json');bp=root/'Structures'/(p['Parameters']+'.bin')
    meta=json.loads(mp.read_text());params=bp.read_bytes()
    ref=next(x for x in stream(root/'Base/ReferencedStructs.json') if x['Pass']==p['Pointer'])['ReferencedStructs']
    vm=root/'StructuresMetadata'/(ref['View']['Metadata']+'.json');vb=root/'Structures'/(ref['View']['Content']+'.bin')
    vmeta=json.loads(vm.read_text());view=vb.read_bytes()
    values={n:read_field(meta,params,n) for n in ['DiffuseColorBoost','IndirectLightingAtlasHalfTexelSize','TargetFormatQuantizationError']}
    for n in ['StateFrameIndexMod8','StateFrameIndex','DiffuseOverrideParameter','PreExposure']:
        values['View.'+n]=read_field(vmeta,view,n)
    assert values['DiffuseColorBoost']==1,'This recorded-case replay supports boost1 only; do not silently change the equation.'
    gaps=[]
    payloadfiles=[mp,bp,vm,vb]
    sampler_member=next(x for x in meta['Members'] if x['Name']=='BilinearClampedSampler')
    sampler={'capturedPointer':pointer(params,sampler_member['Offset'],prefix),'parameterOffset':sampler_member['Offset'],'provenance':'pinned-source-construction','filter':'SF_Bilinear','addressU':'AM_Clamp','addressV':'AM_Clamp','addressW':'AM_Clamp','mipBias':0,'maxAnisotropy':1,'borderColor':0,'comparison':'SCF_Never','minLOD':0,'maxLOD':3.4028234663852886e+38,'source':'Engine/Source/Runtime/Renderer/Private/Lumen/LumenSceneLighting.cpp:190; Engine/Source/Runtime/RenderCore/Public/RHIStaticStates.h:59-78'}
    tilepath,_=resource('Lumen.CardTiles');tiles=np.memmap(tilepath,dtype='<u4',mode='r')
    pagepath,_=resource('Lumen.PageBuffer');pagebytes=pagepath.read_bytes()
    argpath,_=resource('Lumen.DispatchCardTilesIndirectArgs');args=list(struct.unpack('<6I',argpath.read_bytes()))
    tex={}
    for name,resource_name in [('albedo','Lumen.SceneAlbedo'),('emissive','Lumen.SceneEmissive'),('direct','Lumen.SceneDirectLighting'),('indirect','Lumen.Radiosity.SceneIndirectLighting')]:
        tex[name]=Texture(*resource(resource_name))
    # The output resource name is discovered from the actual unique output descriptor.
    outname=desc[base_rid(p['OutputResources'][0])]['Name']
    tex['output']=Texture(*resource(outname,True))
    atlas=np.array([tex['direct'].w,tex['direct'].h],dtype=np.float32)
    assert tex['albedo'].w==tex['direct'].w and tex['emissive'].w==tex['direct'].w and tex['indirect'].w==tex['direct'].w,'All sampled atlases must share the dispatch extent.'
    assert tex['output'].w==tex['direct'].w and tex['output'].h==tex['direct'].h,'Output atlas must match the dispatch extent.'
    # LumenCardScene: prefer the captured RDGUniformBufferParameters payload.
    cardub=ref.get('LumenCardScene')
    rdgub={'operand':'LumenCardScene','present':cardub is not None}
    if cardub is None:
        rdgub.update({'status':'mapping-missing'})
        gaps.append({'operand':'LumenCardScene','status':'mapping-missing','detail':'ReferencedStructs has no LumenCardScene entry for this pass; no captured payload can be read.'})
        inv=F(1)/atlas
        rdgub['fields']={'PhysicalAtlasSize':{'status':'missing','source':'allocation-inference'},'InvPhysicalAtlasSize':{'status':'missing','source':'allocation-inference'}}
    elif cardub.get('Null'):
        rdgub.update({'status':'captured-null-binding','content':None,'metadata':None})
        gaps.append({'operand':'LumenCardScene','status':'captured-null-binding','detail':'The pass bound an explicitly null RDG uniform buffer; no payload exists. Allocation inference is recorded as a labelled fallback.'})
        inv=F(1)/atlas
        rdgub['fields']={'PhysicalAtlasSize':{'status':'null-binding'},'InvPhysicalAtlasSize':{'status':'null-binding'}}
    elif not cardub.get('ContentAvailable'):
        rdgub.update({'status':'content-unavailable','content':cardub.get('Content'),'metadata':cardub.get('Metadata')})
        gaps.append({'operand':'LumenCardScene','status':'content-unavailable','detail':f"Captured mapping has ContentAvailable={cardub.get('ContentAvailable')}; no payload bytes exist."})
        inv=F(1)/atlas
        rdgub['fields']={'PhysicalAtlasSize':{'status':'content-unavailable'},'InvPhysicalAtlasSize':{'status':'content-unavailable'}}
    else:
        cb=root/'Structures'/(cardub['Content']+'.bin');cm=root/'StructuresMetadata'/(cardub['Metadata']+'.json')
        cmeta=json.loads(cm.read_text());cbytes=cb.read_bytes();payloadfiles.extend([cm,cb])
        missing=[n for n in ('PhysicalAtlasSize','InvPhysicalAtlasSize') if not any(x['Name']==n for x in cmeta['Members'])]
        assert not missing,('captured LumenCardScene payload is missing required members',missing)
        physical=read_field(cmeta,cbytes,'PhysicalAtlasSize')
        invc=read_field(cmeta,cbytes,'InvPhysicalAtlasSize')
        assert np.all(np.array(physical,dtype=np.float32)==atlas),('captured PhysicalAtlasSize disagrees with the actual atlas',physical,atlas.tolist())
        inv=np.array(invc,dtype=np.float32)
        assert np.allclose(inv,F(1)/atlas,rtol=0,atol=1e-7),('captured InvPhysicalAtlasSize inconsistent',inv.tolist())
        rdgub.update({'status':'captured','content':cardub['Content'],'metadata':cardub['Metadata'],'byteSize':len(cbytes),'contentKind':cardub.get('ContentKind'),'layoutName':cardub.get('LayoutName'),'fields':{'PhysicalAtlasSize':{'value':list(physical),'offset':next(x['Offset'] for x in cmeta['Members'] if x['Name']=='PhysicalAtlasSize')},'InvPhysicalAtlasSize':{'value':list(invc),'offset':next(x['Offset'] for x in cmeta['Members'] if x['Name']=='InvPhysicalAtlasSize')}}})
        values['LumenCardScene.PhysicalAtlasSize']=physical
        values['LumenCardScene.InvPhysicalAtlasSize']=invc
        if any(x['Name']=='IndirectLightingAtlasDownsampleFactor' for x in cmeta['Members']):
            values['LumenCardScene.IndirectLightingAtlasDownsampleFactor']=read_field(cmeta,cbytes,'IndirectLightingAtlasDownsampleFactor')
    assert tex['indirect'].w==tex['direct'].w and tex['indirect'].h==tex['direct'].h,'Downsampled permutation needs additional recorded compile permutation.'
    # Expected output shader route and dispatch edges.
    route={'eventName':p['EventName'],'parameterStruct':'FParameters','parametersMetadata':p['ParametersMetadata'],'parameters':p['Parameters'],'parentEventScopes':p.get('ParentEventScopes',[]),'outputName':outname,'expectedKernel':'CombineLumenSceneLightingCS','eventNameMatches':p['EventName']=='CombineLighting CS','outputMatches':outname=='Lumen.SceneFinalLighting','inputRequires':{n:any(desc.get(base_rid(r),{}).get('Name')==n for r in p['InputResources']) for n in ['Lumen.CardTiles','Lumen.PageBuffer','Lumen.SceneAlbedo','Lumen.SceneEmissive','Lumen.SceneDirectLighting','Lumen.Radiosity.SceneIndirectLighting','Lumen.DispatchCardTilesIndirectArgs']}}
    assert route['eventNameMatches'] and route['outputMatches'],route
    assert all(route['inputRequires'].values()),route
    assert len(args)==6 and args[3]>0 and args[4]==1 and args[5]==1,('unexpected indirect args',args)
    dispatch={'argsUint32':args,'argsByteCount':len(args)*4,'directGroupCountOffset':12,'groups':args[3:6],'tilesAvailable':int(len(tiles))}
    candidates=[]
    for group in range(min(args[3],len(tiles))):
        packed=int(tiles[group]);pageidx=packed&0xffffff;tile=np.array([(packed>>24)&15,(packed>>28)&15])
        off=80*pageidx;vectors=np.array(struct.unpack_from('<20f',pagebytes,off),dtype=np.float32).reshape(5,4)
        words=struct.unpack_from('<20I',pagebytes,off)
        if vectors[0,2]<=0:continue
        pagecoord=(vectors[2,:2]*atlas+F(.5)).astype(int)
        local=tile*8+np.array([3,4]);xy=pagecoord+local
        if np.any(xy<0) or xy[0]>=tex['direct'].w or xy[1]>=tex['direct'].h:continue
        uv=vectors[2,:2]+inv*(local.astype(np.float32)+F(.5))
        samples={};vals={}
        for key in ['albedo','emissive','direct','indirect']:vals[key],samples[key]=tex[key].sample(uv)
        rho=vals['albedo']*vals['albedo'];override=np.array(values['View.DiffuseOverrideParameter'],dtype=np.float32)
        rho=rho*override[3]+override[:3]
        lambert=rho*F(1/math.pi);irr=vals['direct']+vals['indirect'];L=irr*lambert+vals['emissive'];L=np.maximum(np.nan_to_num(L,nan=0,posinf=0,neginf=0),F(0))
        seed=[int(xy[0]),int(xy[1]),int(values['View.StateFrameIndexMod8'])+2];rnd,state=pcg16(seed);E=F(rnd[0]/65536)
        error=L*np.array(values['TargetFormatQuantizationError'],dtype=np.float32);error=(error.view(np.uint32)&np.uint32(0xff800000)).view(np.float32)
        dithered=L+error*E
        actualword=int(tex['output'].data[xy[1],xy[0]]) if tex['output'].stored=='PF_FloatR11G11B10' else None
        actualrgb=tex['output'].pixel(xy[0],xy[1])
        nearest=encode_rgb11(dithered);truncated=encode_rgb11(dithered,'truncate')
        score=float(np.min(vals['direct'])+np.min(vals['indirect']))
        candidates.append({'groupId':[group,0,0],'groupThreadId':[3,4,0],'packedTileWord':packed,'cardPageIndex':pageidx,'tileCoord':tile.tolist(),'cardIndex':words[0],'pageRecordByteOffset':off,'pageRecordHex':pagebytes[off:off+80].hex(),'pageDecoded':{'CardIndex':words[0],'ResLevelPageTableOffset':words[1],'SizeInTexels':vectors[0,2:].tolist(),'CardUVRect':vectors[1].tolist(),'PhysicalAtlasUVRect':vectors[2].tolist(),'CardUVTexelScale':vectors[3,:2].tolist(),'ResLevelSizeInTiles':list(words[14:16]),'LastDirectLightingUpdateFrameIndex':words[16],'LastIndirectLightingUpdateFrameIndex':words[17],'IndirectLightingTemporalIndex':words[18],'DirectLightingTemporalIndex':words[19]},'pageVectors':vectors.tolist(),'atlasCoord':xy.tolist(),'atlasUV':uv.tolist(),'samples':samples,'decodedAlbedo':rho.tolist(),'diffuseLambert':lambert.tolist(),'sumIrradiance':irr.tolist(),'finalBeforeQuantization':L.tolist(),'pcgInput':seed,'pcgStatesUint32':state,'pcg16':rnd,'ditherUniform':float(E),'quantizationStep':error.tolist(),'ditheredBeforeStore':dithered.tolist(),'outputPacked':actualword,'outputRGB':actualrgb.tolist(),'predictedNearestPacked':nearest,'predictedTruncatedPacked':truncated,'nearestMatch':nearest==actualword,'truncatedMatch':truncated==actualword,'absoluteFloatError':np.abs(actualrgb-dithered).tolist(),'score':score})
        if len(candidates)>=512:break
    assert candidates,'No dispatched tile produced a sampled texel.'
    candidates.sort(key=lambda x:x['score'],reverse=True)
    chosen=next((x for x in candidates if x['score']>0),candidates[0])
    # Per-pixel check of the whole selected 8x8 dispatch tile: every thread of
    # the group uses the same captured page record but its own local offset.
    g=chosen['groupId'][0];packed=int(tiles[g]);pageidx=packed&0xffffff
    tile=np.array([(packed>>24)&15,(packed>>28)&15]);off=80*pageidx
    pv=np.array(struct.unpack_from('<20f',pagebytes,off),dtype=np.float32).reshape(5,4)
    size=np.rint(pv[0,2:]).astype(int) if np.all(np.isfinite(pv[0,2:])) else np.array([8,8])
    size=np.clip(size,1,8)
    pagecoord=(pv[2,:2]*atlas+F(.5)).astype(int)
    tilepix=[]
    for ly in range(int(size[1])):
        for lx in range(int(size[0])):
            local=tile*8+np.array([lx,ly]);xy=pagecoord+local
            if np.any(xy<0) or xy[0]>=tex['direct'].w or xy[1]>=tex['direct'].h:continue
            uv=pv[2,:2]+inv*(local.astype(np.float32)+F(.5))
            vals={}
            for key in ['albedo','emissive','direct','indirect']:vals[key],_=tex[key].sample(uv)
            rho=vals['albedo']*vals['albedo'];override=np.array(values['View.DiffuseOverrideParameter'],dtype=np.float32)
            rho=rho*override[3]+override[:3]
            irr=vals['direct']+vals['indirect'];L=np.maximum(np.nan_to_num(irr*(rho*F(1/math.pi))+vals['emissive'],nan=0,posinf=0,neginf=0),F(0))
            seed=[int(xy[0]),int(xy[1]),int(values['View.StateFrameIndexMod8'])+2];rnd,_=pcg16(seed);E=F(rnd[0]/65536)
            error=L*np.array(values['TargetFormatQuantizationError'],dtype=np.float32);error=(error.view(np.uint32)&np.uint32(0xff800000)).view(np.float32)
            dithered=L+error*E
            actual=int(tex['output'].data[xy[1],xy[0]]) if tex['output'].stored=='PF_FloatR11G11B10' else None
            trunc_match=encode_rgb11(dithered,'truncate')==actual
            near_match=encode_rgb11(dithered)==actual
            tilepix.append({'localThread':[lx,ly],'atlasCoord':xy.tolist(),'outputPacked':actual,'predictedTruncatedPacked':encode_rgb11(dithered,'truncate'),'truncatedMatch':trunc_match,'nearestMatch':near_match})
    tile_validation={'group':g,'tileCoord':tile.tolist(),'pageSizeInTexels':size.tolist(),'examinedPixels':len(tilepix),'truncationMatches':sum(x['truncatedMatch'] for x in tilepix),'nearestMatches':sum(x['nearestMatch'] for x in tilepix),'pixels':tilepix}
    comparisons=[{'groupId':x['groupId'],'atlasCoord':x['atlasCoord'],'packedTileWord':x['packedTileWord'],'cardPageIndex':x['cardPageIndex'],'outputPacked':x['outputPacked'],'predictedTruncatedPacked':x['predictedTruncatedPacked'],'predictedNearestPacked':x['predictedNearestPacked'],'truncatedMatch':x['truncatedMatch'],'nearestMatch':x['nearestMatch'],'absoluteFloatError':x['absoluteFloatError'],'score':x['score']} for x in sorted(candidates,key=lambda y:y['groupId'][0])]
    terr=np.array([x['absoluteFloatError'] for x in candidates],dtype=np.float64)
    ner=np.array([np.abs(np.array(decode_rgb11(x['predictedNearestPacked']))-np.array(decode_rgb11(x['outputPacked']))) for x in candidates],dtype=np.float64)
    def errstats(a):return {'maxAbsChannelError':float(a.max()),'meanAbsChannelError':float(a.mean()),'channelCount':int(a.size)}
    report={'schemaVersion':1,'capture':str(root),'engineVersion':'5.8.1','engineCommit':'71fe36aac5a8df5ccd66c763ffc902b29b6a9c43','kernel':'CombineLumenSceneLightingCS','pass':p,'parameters':values,'lumenCardScene':rdgub,'parameterPayloads':[{'path':str(f.relative_to(root)),'sha256':sha(f),'bytes':f.stat().st_size} for f in payloadfiles],'sampler':sampler,'resources':operands,'outputShaderRoute':route,'dispatch':dispatch,'dispatchArgsUint32':args,'dispatchArgsByteOffset':12,'dispatchGroups':args[3:6],'coverageGaps':gaps,'sample':chosen,'tileValidation':tile_validation,'comparisons':comparisons,'validation':{'examinedTexels':len(candidates),'nearestMatches':sum(x['nearestMatch'] for x in candidates),'truncationMatches':sum(x['truncatedMatch'] for x in candidates),'comparisonCountMatchesCandidates':len(comparisons)==len(candidates),'selectedTile':{k:v for k,v in tile_validation.items() if k!='pixels'},'errorMetrics':{'truncatingModelVsActualRGB':errstats(terr),'nearestModelVsActualRGB':errstats(ner)},'scope':'One texel from each of up to 512 dispatched tiles, plus every thread pixel of the selected 8x8 tile; the actual GPU output packed word is compared per pixel with the CPU replay. Not a proof of every shader path.'},'notes':['Texture bytes are decoded from the exact per-file SubresourceLayouts.StoredFormat; BC7/BC6H views are exporter-decoded and are not treated as compressed blocks. BC7 is recovered as 8-bit UNORM before sampling; BC6H remains float.','The LumenCardScene payload is read from the captured RDGUniformBufferParameters mapping for this pass; allocation-derived atlas size is not used when the payload is present.','GPU store format is R11G11B10 unsigned float; nearest and truncated predictions are both reported rather than assuming unverified hardware rounding.','Direct/indirect irradiance, emissive radiance and final radiance share the cache pre-exposure scale; albedo and interpolation/quantization multipliers are dimensionless. rho/pi is the Lambertian BRDF (sr^-1). This combine kernel does not apply scene exposure or tone mapping.','The compact float encoder is validated only for these finite captured values; it is not a general NaN/overflow/subnormal GPU format-conversion emulator.','Card candidate here is the selected dispatch tile, not a geometric ray hit. Surface-cache ray-hit interpolation is a separate operation.']}
    report['sourceRefs']=[{'path':p,'line':line,'role':role} for p,line,role in [('Engine/Shaders/Private/Lumen/LumenSceneLighting.usf',477,'dispatch/address/sample/combine/write'),('Engine/Source/Runtime/Renderer/Private/Lumen/LumenSceneLighting.cpp',190,'static sampler'),('Engine/Source/Runtime/Renderer/Private/Lumen/LumenSceneLighting.h',168,'indirect dispatch offset12'),('Engine/Shaders/Private/Lumen/LumenCardTile.ush',19,'tile word decode'),('Engine/Shaders/Private/Lumen/LumenCardCommon.ush',170,'page buffer decode'),('Engine/Shaders/Private/Lumen/SurfaceCache/LumenSurfaceCache.ush',44,'albedo/transport combine'),('Engine/Shaders/Private/Lumen/LumenDiffuseColorBoost.ush',6,'view diffuse override'),('Engine/Shaders/Private/Lumen/LumenFloatQuantization.ush',24,'noise seed'),('Engine/Shaders/Private/RandomPCG.ush',12,'PCG16'),('Engine/Shaders/Private/Quantization.ush',13,'mantissa-clear quantization dither'),('Engine/Source/Runtime/RenderCore/Private/DumpGPU.cpp',912,'BC decoded readback format')]]
    (out/'cache-texel-replay.json').write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
    if not (chosen['nearestMatch'] or chosen['truncatedMatch']):
        raise RuntimeError('Brightest selected texel matches neither explicit float-store model; inspect JSON before publishing.')
    print(json.dumps({'pass':p['Id'],'pointer':p['Pointer'],'sample':chosen['atlasCoord'],'validation':report['validation'],'lumenCardScene':rdgub['status'],'gaps':gaps,'output':str(out/'cache-texel-replay.json')}))
if __name__=='__main__':
    parser = argparse.ArgumentParser(description="Replay one captured Lumen Surface Cache dispatch.")
    parser.add_argument("dump", nargs="?")
    parser.add_argument("out", nargs="?")
    parser.add_argument("--capture", type=Path, help="reconstructed DumpGPU directory")
    parser.add_argument("--output", type=Path, help="directory for the replay JSON")
    parser.add_argument("--pass-id", type=int)
    args = parser.parse_args()
    capture = args.capture or args.dump
    output = args.output or args.out
    if capture is None or output is None:
        parser.error("supply --capture and --output")
    replay(capture, output, args.pass_id)
