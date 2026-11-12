"""Executable, deliberately small Nanite-inspired geometry laboratory.

This is a CPU replacement-tree experiment, not Unreal's Nanite builder, DAG,
compressed stream format, GPU culler, or GPU software rasterizer. All published
mesh pictures and counts are calculated here from Stanford's scanned bunny.
"""
from pathlib import Path
from collections import defaultdict, deque
import argparse, colorsys, hashlib, heapq, json, math, pickle, platform, sys, time
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from numba import njit
from download import read_ply, ensure_data, DATA, HERE

ROOT = HERE.parents[4]
OUT = ROOT/'Docs/Unreal/assets/nanite-lab'
CACHE = HERE/'cache'
OUT.mkdir(parents=True, exist_ok=True)
CACHE.mkdir(exist_ok=True)
W, H = 640, 640
PALETTE = np.array([[.77,.32,.24],[.22,.50,.49],[.58,.42,.66],[.78,.58,.18],[.30,.57,.30],[.26,.43,.67]])

def edge_faces(f):
    edges = defaultdict(list)
    for i,(a,b,c) in enumerate(f):
        for u,v in ((a,b),(b,c),(c,a)):
            edges[tuple(sorted((int(u),int(v))))].append(i)
    return edges

def clusters(v,f,limit=128):
    """Connected greedy face growth; deterministic IDs and centroid tie breaks."""
    ef = edge_faces(f)
    adj = [set() for _ in f]
    for fs in ef.values():
        for a in fs:
            adj[a].update(b for b in fs if b != a)
    centers = v[f].mean(axis=1)
    unused = np.ones(len(f), bool)
    result = []
    for seed in range(len(f)):
        if not unused[seed]: continue
        picked,frontier,verts = [],[(0.,seed)],set()
        while frontier and len(picked)<limit:
            _,t=heapq.heappop(frontier)
            if not unused[t]: continue
            newverts=verts|set(f[t])
            if len(newverts)>256: continue
            unused[t]=False; picked.append(t); verts=newverts
            for n in sorted(adj[t]):
                if unused[n]:
                    heapq.heappush(frontier,(float(np.sum((centers[n]-centers[seed])**2)),n))
        result.append(np.array(picked,dtype=np.int32))
    assert sum(map(len,result)) == len(f)
    return result

def compact(v,f,ids):
    used,inv=np.unique(f, return_inverse=True)
    return v[used].copy(),inv.reshape(-1,3),ids[used].copy()

def boundaries(f,ids):
    return {tuple(sorted((int(ids[a]),int(ids[b])))) for (a,b),fs in edge_faces(f).items() if len(fs)==1}

def simplify(v,f,ids,target):
    """Area-weighted QEM with locked exterior, manifold link, and normal checks.

    radius stores a conservative accumulated *vertex relocation* envelope. It is
    intentionally more pessimistic than Nanite's geometric error metric.
    """
    v=v.copy(); f=f.copy()
    nv=len(v); alive=np.ones(len(f),bool); valid=np.ones(nv,bool)
    incident=[set() for _ in v]
    for t,tri in enumerate(f):
        for a in tri: incident[a].add(t)
    ef=edge_faces(f)
    locked=np.zeros(nv,bool)
    for (a,b),ts in ef.items():
        if len(ts)==1: locked[[a,b]]=True
    before={int(ids[a]):v[a].copy() for a in np.flatnonzero(locked)}
    p=v[f]; cross=np.cross(p[:,1]-p[:,0],p[:,2]-p[:,0]); lengths=np.linalg.norm(cross,axis=1)
    normals=cross/np.maximum(lengths[:,None],1e-30)
    planes=np.column_stack((normals,-np.einsum('ij,ij->i',normals,p[:,0])))
    quads=np.einsum('ti,tj,t->tij',planes,planes,lengths/2)
    q=np.zeros((nv,4,4)); radius=np.zeros(nv); version=np.zeros(nv,np.int64)
    for k in range(3): np.add.at(q,f[:,k],quads)
    heap=[]; collapses=0; rejected=0
    def neighbors(a):
        return {int(b) for t in incident[a] for b in f[t] if b!=a}
    def link_edges(a):
        # The link of a vertex in a triangle complex contains edges as well as
        # vertices. Checking common neighbor vertices alone misses tetrahedra:
        # their opposite link edge would collapse to duplicate coincident faces.
        return {tuple(sorted(int(b) for b in f[t] if b!=a)) for t in incident[a]}
    def push(a,b):
        if a>b: a,b=b,a
        if not valid[a] or not valid[b] or (locked[a] and locked[b]): return
        quad=q[a]+q[b]
        if locked[a]: pt=v[a].copy()
        elif locked[b]: pt=v[b].copy()
        else:
            choices=[v[a],v[b],(v[a]+v[b])*.5]
            if abs(np.linalg.det(quad[:3,:3]))>1e-18:
                solved=np.linalg.solve(quad[:3,:3],-quad[:3,3])
                if np.linalg.norm(solved-(v[a]+v[b])*.5)<2*np.linalg.norm(v[a]-v[b]): choices.append(solved)
            pt=min(choices,key=lambda p: float(np.r_[p,1]@quad@np.r_[p,1])).copy()
        cost=max(0.,float(np.r_[pt,1]@quad@np.r_[pt,1]))
        heapq.heappush(heap,(cost,a,b,int(version[a]),int(version[b]),tuple(pt)))
    for a,b in ef: push(a,b)
    count=len(f)
    while count>target and heap:
        _,a,b,va,vb,pos=heapq.heappop(heap)
        if not valid[a] or not valid[b] or version[a]!=va or version[b]!=vb: continue
        shared=incident[a]&incident[b]
        if len(shared)!=2: rejected+=1; continue
        opposite={int(c) for t in shared for c in f[t] if c!=a and c!=b}
        if neighbors(a)&neighbors(b) != opposite: rejected+=1; continue
        # link(a) intersect link(b) must equal link(edge(a,b)). An interior edge
        # has only opposite vertices in its link, never a link edge.
        if link_edges(a)&link_edges(b): rejected+=1; continue
        pt=np.array(pos)
        affected=incident[a]|incident[b]
        remains=sorted(affected-shared)
        old=v[f[remains]]
        new=old.copy()
        fm=f[remains]
        new[(fm==a)|(fm==b)]=pt
        n0=np.cross(old[:,1]-old[:,0],old[:,2]-old[:,0])
        n1=np.cross(new[:,1]-new[:,0],new[:,2]-new[:,0])
        if np.any(np.einsum('ij,ij->i',n0,n1)<=.1*np.linalg.norm(n0,axis=1)*np.linalg.norm(n1,axis=1)) or np.any(np.linalg.norm(n1,axis=1)<1e-15):
            rejected+=1; continue
        # Preserve locked vertex's identity if it is the second endpoint.
        if locked[b]: a,b=b,a
        rad=max(radius[a]+np.linalg.norm(v[a]-pt),radius[b]+np.linalg.norm(v[b]-pt))
        for t in shared:
            alive[t]=False
            for c in f[t]: incident[c].discard(t)
        for t in remains:
            f[t][f[t]==b]=a
        incident[a]=set(remains); incident[b].clear()
        valid[b]=False; version[a]+=1; version[b]+=1
        v[a]=pt; q[a]+=q[b]; radius[a]=rad
        count-=len(shared); collapses+=1
        for c in sorted(neighbors(a)): push(a,c)
    result=compact(v,f[alive],ids)
    rv,rf,ri=result
    canonical_faces={tuple(sorted(map(int,tri))) for tri in rf}
    assert len(canonical_faces)==len(rf), 'Collapse created duplicate unoriented faces.'
    lookup={int(k):p for k,p in zip(ri,rv)}
    locked_ok=all(k in lookup and np.array_equal(p,lookup[k]) for k,p in before.items())
    assert locked_ok
    return (*result,float(radius[valid].max(initial=0)),{'collapses':collapses,'rejected_candidates':rejected,'locked_vertices':len(before),'locked_positions_unchanged':locked_ok,'duplicate_unoriented_faces':0})

def build_tree(v,f):
    nodes=[]; leaf_lists=clusters(v,f)
    for ci,ts in enumerate(leaf_lists):
        cv,cf,ids=compact(v,f[ts],np.arange(len(v)))
        nodes.append(dict(id=ci,v=cv,f=cf,ids=ids,children=[],error=0.,level=0,leaf_ids=[ci],stats={}))
    levels=[list(range(len(nodes)))]; level=0
    while len(levels[-1])>1 or sum(len(nodes[i]['f']) for i in levels[-1])>128:
        current=levels[-1]; edge_owners=defaultdict(list)
        for i in current:
            n=nodes[i]
            for edge in boundaries(n['f'],n['ids']): edge_owners[edge].append(i)
        adjacency=defaultdict(lambda:defaultdict(int))
        for owners in edge_owners.values():
            for a in owners:
                for b in owners:
                    if a!=b: adjacency[a][b]+=1
        left=set(current); groups=[]
        while left:
            start=min(left); group=[start]; left.remove(start)
            while len(group)<4 and left:
                scores=defaultdict(int)
                for a in group:
                    for b,w in adjacency[a].items():
                        if b in left: scores[b]+=w
                if scores: nxt=min(scores,key=lambda k:(-scores[k],k))
                else:
                    center=np.mean([nodes[a]['v'].mean(axis=0) for a in group],axis=0)
                    nxt=min(left,key=lambda k:float(np.sum((nodes[k]['v'].mean(axis=0)-center)**2)))
                group.append(nxt); left.remove(nxt)
            groups.append(group)
        following=[]; level+=1; started=time.time()
        for group in groups:
            pos={}; merged=[]; idx={}; ids=[]; fs=[]
            for child in group:
                n=nodes[child]; mapping=[]
                for gid,p in zip(n['ids'],n['v']):
                    gid=int(gid)
                    if gid not in idx: idx[gid]=len(merged); merged.append(p); ids.append(gid)
                    else: assert np.allclose(merged[idx[gid]],p,atol=1e-14)
                    mapping.append(idx[gid])
                fs.extend(np.array(mapping)[n['f']])
            mv,mf,mi=np.array(merged),np.array(fs),np.array(ids)
            oldboundary=boundaries(mf,mi)
            sv,sf,si,delta,stats=simplify(mv,mf,mi,max(24,len(mf)//2))
            assert boundaries(sf,si)==oldboundary
            stats['exterior_edge_set_preserved']=True
            nid=len(nodes)
            nodes.append(dict(id=nid,v=sv,f=sf,ids=si,children=group,error=max(nodes[a]['error'] for a in group)+delta,level=level,leaf_ids=sum((nodes[a]['leaf_ids'] for a in group),[]),stats=stats))
            following.append(nid)
        levels.append(following)
        print(f'level {level}: {len(following)} replacement regions, {sum(len(nodes[i]["f"]) for i in following)} triangles, {time.time()-started:.1f}s',flush=True)
        if len(following)==1 and len(current)==1 and len(nodes[following[0]]['f'])>=len(nodes[current[0]]['f']):
            # Original scan holes constrain the absolute floor; never delete a
            # boundary just to hit a triangle budget. Drop the redundant parent.
            nodes.pop(); levels.pop(); break
    for n in nodes:
        n['cluster_faces']=clusters(n['v'],n['f'])
        n['center']=(n['v'].min(axis=0)+n['v'].max(axis=0))/2
        n['radius']=float(np.linalg.norm(n['v']-n['center'],axis=1).max())
    return nodes,levels,leaf_lists

def camera(distance=2.5):
    target=np.array([0.,.46,0.]); direction=np.array([.25,.12,1.]); direction/=np.linalg.norm(direction)
    eye=target+direction*distance
    forward=target-eye; forward/=np.linalg.norm(forward)
    right=np.cross(forward,[0.,1.,0.]); right/=np.linalg.norm(right)
    up=np.cross(right,forward)
    return eye,np.stack((right,up,forward),axis=1)

def select(nodes,distance,tau=1.,height=H):
    eye,basis=camera(distance); focal=height/(2*math.tan(math.radians(40)/2))
    picked=[]; tests=[]
    def visit(i):
        n=nodes[i]; cam=(n['center']-eye)@basis
        z=cam[2]-n['radius']-n['error']
        # Bounds touching the near plane must descend, never accept an artificial
        # clamped denominator as if it were a conservative projection bound.
        if z<=.05: projected=math.inf
        else:
            radial=(np.linalg.norm(cam[:2])+n['radius']+n['error'])/z
            projected=focal*n['error']/z*math.sqrt(1+radial*radial)
        accepted=projected<=tau or not n['children']
        tests.append({'node':i,'projected_envelope_px':projected,'accepted':accepted})
        if accepted: picked.append(i)
        else:
            for j in n['children']: visit(j)
    visit(len(nodes)-1)
    covered=sum((nodes[i]['leaf_ids'] for i in picked),[])
    leaves=sum(not n['children'] for n in nodes)
    assert len(covered)==len(set(covered))==leaves
    return picked,tests

def assemble(nodes,picked,colour='cluster'):
    vertices=[]; faces=[]; values=[]; localids=[]; offset=0
    for i in picked:
        n=nodes[i]; vertices.extend(n['v']); faces.extend(n['f']+offset); offset+=len(n['v'])
        labels=np.empty(len(n['f']),int)
        for k,ts in enumerate(n['cluster_faces']): labels[ts]=(i*13+k)
        if colour=='level': labels[:]=n['level']
        if colour=='group': labels[:]=i
        values.extend(labels); localids.extend(np.arange(len(n['f'])))
    return np.array(vertices),np.array(faces),np.array(values),np.array(localids)

@njit(cache=True)
def raster_core(screen,depths,faces,width,height):
    depth=np.full((height,width),np.inf); ids=np.full((height,width),-1,np.int32)
    bary=np.zeros((height,width,3)); overdraw=np.zeros((height,width),np.int32)
    for t in range(len(faces)):
        a,b,c=faces[t]; x0,y0=screen[a]; x1,y1=screen[b]; x2,y2=screen[c]
        area=(x1-x0)*(y2-y0)-(y1-y0)*(x2-x0)
        if abs(area)<1e-12 or min(depths[a],depths[b],depths[c])<=.01: continue
        xmin=max(0,int(math.floor(min(x0,x1,x2)))); xmax=min(width-1,int(math.ceil(max(x0,x1,x2))))
        ymin=max(0,int(math.floor(min(y0,y1,y2)))); ymax=min(height-1,int(math.ceil(max(y0,y1,y2))))
        for y in range(ymin,ymax+1):
            for x in range(xmin,xmax+1):
                px=x+.5; py=y+.5
                l0=((x1-px)*(y2-py)-(y1-py)*(x2-px))/area
                l1=((x2-px)*(y0-py)-(y2-py)*(x0-px))/area
                l2=1-l0-l1
                if l0<0 or l1<0 or l2<0: continue
                overdraw[y,x]+=1
                denom=l0/depths[a]+l1/depths[b]+l2/depths[c]
                z=1/denom
                if z<depth[y,x]:
                    depth[y,x]=z; ids[y,x]=t
                    bary[y,x,0]=(l0/depths[a])/denom; bary[y,x,1]=(l1/depths[b])/denom; bary[y,x,2]=(l2/depths[c])/denom
    return depth,ids,bary,overdraw

def render(v,f,labels=None,distance=2.5,width=W,height=H):
    eye,basis=camera(distance); p=(v-eye)@basis
    focal=height/(2*math.tan(math.radians(40)/2))
    screen=np.column_stack((width/2+focal*p[:,0]/p[:,2],height/2-focal*p[:,1]/p[:,2]))
    depth,ids,bary,overdraw=raster_core(screen,p[:,2],f,width,height)
    mask=ids>=0; tid=ids[mask]; weights=bary[mask]
    points=np.sum(v[f[tid]]*weights[:,:,None],axis=1)
    cross=np.cross(v[f[:,1]]-v[f[:,0]],v[f[:,2]]-v[f[:,0]])
    # The scan has no authored normal seam: weld equal locked boundary positions
    # for normal reconstruction even when bounded clusters duplicate vertices.
    unique,remap=np.unique(v,axis=0,return_inverse=True)
    welded=np.zeros_like(unique)
    for k in range(3): np.add.at(welded,remap[f[:,k]],cross)
    welded/=np.maximum(np.linalg.norm(welded,axis=1,keepdims=True),1e-30)
    normals=welded[remap]
    n=np.sum(normals[f[tid]]*weights[:,:,None],axis=1); n/=np.maximum(np.linalg.norm(n,axis=1,keepdims=True),1e-30)
    view=eye-points; view/=np.linalg.norm(view,axis=1,keepdims=True)
    # Bunny's published winding is used. Two-sided display makes scan holes explicit.
    n*=np.where(np.einsum('ij,ij->i',n,view)<0,-1,1)[:,None]
    light=np.array([-.45,.75,.6]); light/=np.linalg.norm(light)
    ndotl=np.maximum(0,n@light)
    base=np.tile(np.array([.52,.29,.13]),(len(points),1))
    # An explicitly authored procedural material, not a captured texture.
    stripe=(np.floor(points[:,1]*16).astype(int)%2)==0
    base[stripe]*=.75
    # Lambertian radiance plus a small environment fill; linear→display at the end.
    lit=base*(.13+ndotl[:,None]*1.3)
    bg=np.array([.958,.952,.929]); rgb=np.tile(bg,(height,width,1)); rgb[mask]=np.clip(lit,0,1)**(1/2.2)
    outputs={'lit':rgb}
    if labels is not None:
        lab=np.asarray(labels)[tid]
        color=np.array([colorsys.hsv_to_rgb((int(k)*.61803398875)%1,.55,.85) for k in lab])
        arr=np.tile(bg,(height,width,1)); arr[mask]=color*(.45+.55*ndotl[:,None]); outputs['labels']=arr
    norm=np.tile(bg,(height,width,1)); norm[mask]=n*.5+.5; outputs['normals']=norm
    alb=np.tile(bg,(height,width,1)); alb[mask]=base**(1/2.2); outputs['albedo']=alb
    dep=np.tile(bg,(height,width,1)); d=depth[mask]; val=(d-d.min())/max(1e-10,d.max()-d.min()); dep[mask]=np.stack([.2+.6*val,.3+.45*val,.55+.3*val],axis=1); outputs['depth']=dep
    vis=np.tile(bg,(height,width,1)); vis[mask]=np.array([colorsys.hsv_to_rgb((int(k)*.61803398875)%1,.65,.9) for k in tid]); outputs['visibility']=vis
    wires=rgb.copy()
    edge=np.zeros(mask.shape,bool); edge[mask]=np.min(weights,axis=1)<.05
    wires[edge]=.18; outputs['wire']=wires
    return outputs,{'depth':depth,'triangle_id':ids,'barycentric':bary,'overdraw':overdraw,'screen':screen,'mask':mask,'positions':points,'normals':n,'base_color':base}

def font(size):
    for name in ['C:/Windows/Fonts/segoeui.ttf','C:/Windows/Fonts/arial.ttf']:
        if Path(name).exists(): return ImageFont.truetype(name,size)
    return ImageFont.load_default(size=size)

def image8(rgb): return Image.fromarray((np.clip(rgb,0,1)*255+.5).astype(np.uint8))

def panel(filename,items,title,subtitle='',cols=3,size=420):
    rows=math.ceil(len(items)/cols); pad=26; top=104
    canvas=Image.new('RGB',(cols*size+2*pad,rows*(size+66)+top+pad),(246,244,237)); draw=ImageDraw.Draw(canvas)
    draw.text((pad,20),title,font=font(29),fill=(36,43,36))
    draw.text((pad,62),subtitle,font=font(17),fill=(83,91,82))
    for k,(img,heading,detail) in enumerate(items):
        x=pad+(k%cols)*size; y=top+(k//cols)*(size+66)
        im=image8(img).resize((size-12,size-12),Image.Resampling.LANCZOS)
        canvas.paste(im,(x,y)); draw.text((x,y+size),heading,font=font(21),fill=(36,43,36)); draw.text((x,y+size+30),detail,font=font(16),fill=(83,91,82))
    canvas.save(OUT/filename)

def svg_chart(levelstats,selections):
    w,h=1280,max(650,190+len(levelstats)*46); parts=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" role="img" aria-labelledby="t d"><title id="t">Measured bunny replacement hierarchy and camera selection</title><desc id="d">Bars plot triangle counts computed by the CPU lab, not Unreal statistics.</desc><rect width="{w}" height="{h}" fill="#f6f4ed"/><g font-family="Segoe UI,Arial,sans-serif" fill="#242b24"><text x="40" y="50" font-size="28">What the build retained; what each view requested</text><text x="40" y="84" font-size="17">Measured on Stanford bun_zipper.ply. CPU replacement tree; 640px view; threshold 1px.</text>']
    maximum=max(s['triangles'] for s in levelstats)
    for k,s in enumerate(levelstats):
        y=140+k*46; bw=330*s['triangles']/maximum
        parts.append(f'<text x="40" y="{y+18}" font-size="17">Level {k}</text><rect x="122" y="{y}" width="{bw:.2f}" height="25" fill="#648465"/><text x="{136+bw:.2f}" y="{y+19}" font-size="16">{s["triangles"]:,} triangles · {s["regions"]} regions</text>')
    for k,s in enumerate(selections):
        y=140+k*86; bw=320*s['triangles']/maximum
        parts.append(f'<text x="760" y="{y-6}" font-size="17">Camera {s["distance"]} heights away</text><rect x="760" y="{y+7}" width="{bw:.2f}" height="22" fill="#ad6a46"/><text x="760" y="{y+50}" font-size="16">{s["triangles"]:,} triangles · {s["clusters"]} bounded clusters</text>')
    parts.append(f'<text x="40" y="{h-30}" font-size="16">Geometry is stored at every level; each cut covers every leaf exactly once.</text></g></svg>')
    (OUT/'measured-hierarchy.svg').write_text(''.join(parts),encoding='utf-8')

def tessellation():
    """A real material displacement demo; not Unreal's adaptive tessellator."""
    images=[]; records=[]
    # A square patch rotated slightly, using the same raster path as the bunny.
    for n in [2,16,64]:
        x,y=np.meshgrid(np.linspace(-.5,.5,n+1),np.linspace(0,1,n+1))
        z=.07*np.sin(2*np.pi*3*x)*np.sin(2*np.pi*3*y)
        v=np.column_stack((x.ravel(),y.ravel(),z.ravel())); f=[]
        for j in range(n):
            for i in range(n):
                a=j*(n+1)+i; f.extend([(a,a+1,a+n+2),(a,a+n+2,a+n+1)])
        f=np.array(f); ims,_=render(v,f,distance=2.0)
        images.append((ims['wire'],f'{len(f):,} displaced triangles',f'{n} segments per side · amplitude 0.07'))
        records.append({'segments':n,'triangles':len(f),'amplitude':.07})
    panel('displacement-tessellation.png',images,'Tessellation adds samples before displacement','Computed patch h(x,y)=0.07 sin(6πx) sin(6πy); this is a lab, not a UE capture.')
    return records

def boundary_detail(nodes,levels):
    candidates=[i for i in levels[1] if len(nodes[i]['f'])>150]
    index=min(candidates,key=lambda i:np.linalg.norm(nodes[i]['center']-[.13,.45,.3]))
    n=nodes[index]
    bv,bf,bl,_=assemble(nodes,n['children'])
    av,af,al,_=assemble(nodes,[index])
    center=(bv.min(axis=0)+bv.max(axis=0))/2
    scale=np.max(np.ptp(bv,axis=0))
    transform=lambda vv:(vv-center)/scale+np.array([0.,.46,0.])
    bims,braw=render(transform(bv),bf,bl,distance=2.)
    aims,araw=render(transform(av),af,al,distance=2.)
    exterior=boundaries(n['f'],n['ids']); locked=set(sum(([a,b] for a,b in exterior),[]))
    images=[]
    for ims,raw,vv,ff,gids,label in [(bims,braw,bv,bf,np.concatenate([nodes[i]['ids'] for i in n['children']]),'Before reduction'),(aims,araw,av,af,n['ids'],'After boundary-locked QEM')]:
        im=image8(ims['wire']); d=ImageDraw.Draw(im)
        for k,gid in enumerate(gids):
            if gid not in locked: continue
            x,y=raw['screen'][k]
            d.ellipse((x-3,y-3,x+3,y+3),fill=(203,58,31))
        images.append((np.asarray(im)/255.,label,f'{len(ff)} triangles · {len(locked)} exterior vertices fixed'))
    panel('bunny-boundary-detail.png',images,'A real replacement region, enlarged','Red vertices are fixed. Old cluster boundaries inside the group may disappear.',cols=2,size=560)
    return {'node':index,'child_nodes':n['children'],'before_triangles':len(bf),'after_triangles':len(af),'locked_vertices':len(locked),'error_envelope':n['error']}

def occlusion_experiment(v,f):
    """Run conservative max-depth HZB culling and current-frame recovery.

    This lab uses positive camera depth, +infinity as empty, and MAX reduction.
    Unreal's reversed-Z comparisons must not be copied from these CPU operators.
    """
    cs=clusters(v,f)
    full,rr=render(v,f,distance=2.)
    old_depth=rr['depth'].copy(); new_ref=rr['depth'].copy()
    old_depth[:,:350]=1.25; new_ref[:,:245]=1.25
    def hzb(depth):
        mips=[depth]
        while max(mips[-1].shape)>1:
            a=mips[-1]; hh,ww=a.shape
            a=np.pad(a,((0,hh%2),(0,ww%2)),constant_values=np.inf)
            mips.append(a.reshape(a.shape[0]//2,2,a.shape[1]//2,2).max(axis=(1,3)))
        return mips
    oldmips=hzb(old_depth)
    eye,basis=camera(2.); z=((v-eye)@basis)[:,2]; screens=rr['screen']
    def hidden(ts,mips):
        verts=np.unique(f[ts]); s=screens[verts]
        # Full rectangle, including partly off-screen pixels, must be covered.
        lo=np.floor(s.min(axis=0)).astype(int); hi=np.ceil(s.max(axis=0)).astype(int)
        if np.any(lo<0) or hi[0]>=W or hi[1]>=H: return False
        span=int(max(hi-lo)+1); level=max(0,int(math.ceil(math.log2(max(1,span)))))
        level=min(level,len(mips)-1); step=2**level
        a=lo//step; b=hi//step
        depth=mips[level][a[1]:b[1]+1,a[0]:b[0]+1]
        return z[verts].min()>depth.max()+1e-8
    pending=[i for i,ts in enumerate(cs) if hidden(ts,oldmips)]
    initial=[i for i in range(len(cs)) if i not in set(pending)]
    mainf=f[np.concatenate([cs[i] for i in initial])]
    main,mr=render(v,mainf,distance=2.)
    current=mr['depth'].copy(); current[:,:245]=1.25
    currentmips=hzb(current)
    recovered=[i for i in pending if not hidden(cs[i],currentmips)]
    finalf=f[np.concatenate([cs[i] for i in initial+recovered])]
    final,fr=render(v,finalf,distance=2.)
    finald=fr['depth'].copy(); finald[:,:245]=1.25
    def compose(ims,edge):
        out=ims['lit'].copy(); out[:,:edge]=[.32,.40,.43]; return out
    # HZB mips visualized as finite occupied depth versus empty coverage.
    mip_images=[]
    for k in [0,3,6]:
        dep=currentmips[k]; a=np.ones((*dep.shape,3))*.96
        finite=np.isfinite(dep); val=np.clip((dep[finite]-1.2)/1.2,0,1)
        a[finite]=np.stack((.2+.5*val,.35+.4*val,.45+.3*val),axis=1)
        mip_images.append((a,f'Current HZB mip {k}',f'{dep.shape[1]} × {dep.shape[0]} · max depth'))
    panel('bunny-hzb-recovery.png',[(compose(full,350),'Previous view','Occluder covers 350 screen columns'),(compose(main,245),'Main pass after movement',f'{len(pending)} history-rejected clusters deferred'),(compose(final,245),'Post pass recovers disocclusion',f'{len(recovered)} deferred clusters retested visible')],'History predicts; current depth repairs visibility','Computed CPU two-pass occlusion with a moving foreground plane. Same camera and bunny.',cols=3,size=420)
    panel('bunny-hzb-pyramid.png',mip_images,'The actual current-frame depth hierarchy','Positive camera depth: each parent keeps the farthest depth; any empty child makes the parent empty.',cols=3,size=420)
    same=np.isclose(finald,new_ref,atol=1e-10,rtol=0) | (np.isinf(finald)&np.isinf(new_ref))
    main_same=np.isclose(current,new_ref,atol=1e-10,rtol=0) | (np.isinf(current)&np.isinf(new_ref))
    return {'frozen_cut_selection_distance':64.,'raster_camera_distance':2.,'clusters':len(cs),'history_rejected':len(pending),'main_clusters':len(initial),'post_recovered':len(recovered),'main_pass_mismatching_depth_samples':int((~main_same).sum()),'final_depth_matches_unculled_reference':bool(np.all(same)),'mismatching_samples':int((~same).sum())}

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--rebuild',action='store_true'); args=parser.parse_args()
    source_v,source_f=read_ply(ensure_data()); v=np.array(source_v); f=np.array(source_f)
    mins=v.min(axis=0); maxs=v.max(axis=0); scale=maxs[1]-mins[1]
    v[:,0]-=(mins[0]+maxs[0])/2; v[:,2]-=(mins[2]+maxs[2])/2; v[:,1]-=mins[1]; v/=scale
    cache=CACHE/'hierarchy.pkl'
    if args.rebuild or not cache.exists():
        nodes,levels,leaves=build_tree(v,f)
        cache.write_bytes(pickle.dumps((nodes,levels,leaves),protocol=5))
    else: nodes,levels,leaves=pickle.loads(cache.read_bytes())
    print('Rendering computed hierarchy',flush=True)
    clusterlabels=np.empty(len(f),int)
    for k,ts in enumerate(leaves): clusterlabels[ts]=k
    source,raw=render(v,f,clusterlabels,distance=2.)
    np.savez_compressed(CACHE/'source-raster.npz',**raw)
    group_labels=np.empty(len(f),int)
    for gid in levels[1]:
        for leaf in nodes[gid]['leaf_ids']: group_labels[leaves[leaf]]=gid
    groups,_=render(v,f,group_labels,distance=2.)
    panel('bunny-clusters.png',[(source['lit'],'Source mesh','69,451 triangles · direct-light material'),(source['labels'],f'{len(leaves):,} connected leaf clusters','Each has ≤128 triangles / ≤256 vertices'),(groups['labels'],f'{len(levels[1])} adjacent replacement groups','Fine boundaries inside a group may simplify')],'A scanned bunny becomes bounded work','Every colored region comes from the computed adjacency partition. Stanford Computer Graphics Laboratory.')
    levelstats=[]; items=[]
    for k,inds in enumerate(levels):
        tri=sum(len(nodes[i]['f']) for i in inds); cs=sum(len(nodes[i]['cluster_faces']) for i in inds)
        levelstats.append({'level':k,'regions':len(inds),'triangles':tri,'clusters':cs,'max_vertex_relocation_envelope':max(nodes[i]['error'] for i in inds)})
        if k in [0,2,4,6,8,len(levels)-1]:
            lv,lf,ll,_=assemble(nodes,inds,'group'); ims,_=render(lv,lf,ll,distance=2.)
            items.append((ims['wire'],f'Level {k} · {tri:,} triangles',f'{len(inds)} replacement regions · {cs} clusters'))
    panel('bunny-hierarchy.png',items,'Build several compatible replacements','Locked exterior vertices remain exact. Coarser groups unlock former interior boundaries.',cols=3,size=420)
    selections=[]; items=[]; selection_masks=[]
    for dist in [2.,16.,32.,64.,128.,512.]:
        picked,tests=select(nodes,dist)
        sv,sf,sl,_=assemble(nodes,picked,'level')
        ims,rr=render(sv,sf,sl,distance=dist)
        shown,_=render(sv,sf,sl,distance=2.)
        stat={'distance':dist,'threshold_px':1.,'triangles':len(sf),'regions':len(picked),'clusters':sum(len(nodes[i]['cluster_faces']) for i in picked),'selected_nodes':picked,'tree_tests':len(tests),'covered_samples':int(rr['mask'].sum()),'selected_level_counts':{str(k):sum(nodes[i]['level']==k for i in picked) for k in range(len(levels))}}
        selections.append(stat); selection_masks.append(rr['mask'])
        if dist in [16.,64.,512.]: items.append((shown['labels'],f'Selected from distance {dist:g}',f'{len(sf):,} triangles · enlarged to same view'))
    panel('bunny-runtime-selection.png',items,'A runtime cut changes with projected error','Colors identify hierarchy levels. Images are enlarged after selection so geometry differences remain visible.')
    picked,_=select(nodes,64.)
    sv,sf,sl,_=assemble(nodes,picked)
    ims,rr=render(sv,sf,sl,distance=2.)
    np.savez_compressed(CACHE/'selected-raster.npz',**rr)
    panel('bunny-visibility-shading.png',[(ims['depth'],'1. Depth','Nearest camera-space z per covered sample'),(ims['visibility'],'2. Triangle identity','Color is a deterministic hash of the winning ID'),(ims['normals'],'3. Reconstructed normals','Perspective-correct vertex interpolation'),(ims['albedo'],'4. Material base color','Procedural clay bands from reconstructed position'),(ims['lit'],'5. Direct illumination','Lambert cosine and an explicit ambient fill'),(ims['labels'],'6. Cluster identity','Recovered from the visible triangle record')],'Visibility is an address, not a shaded photograph','All six panels use the same computed winning-triangle buffer. CPU lab; no indirect-light claim.',cols=3,size=420)
    svg_chart(levelstats,selections)
    # Show the actual 16x16 identity grid around a visible sample, and metadata.
    ys,xs=np.nonzero(rr['mask'] & (rr['barycentric'].min(axis=2)>.2)); chosen=np.argmin((xs-W*.5)**2+(ys-H*.56)**2); px=int(xs[chosen]); py=int(ys[chosen]); ti=int(rr['triangle_id'][py,px]); b=rr['barycentric'][py,px]
    pixel={'x':px,'y':py,'triangle':ti,'vertex_indices':[int(i) for i in sf[ti]],'barycentric':b.tolist(),'depth':float(rr['depth'][py,px]),'position':np.sum(sv[sf[ti]]*b[:,None],axis=0).tolist(),'candidate_coverage_count':int(rr['overdraw'][py,px])}
    metrics={'experiment':'CPU boundary-locked QEM replacement tree and visibility rasterizer; not Unreal Nanite', 'source_triangles':len(f),'source_vertices':len(v),'used_vertices':len(np.unique(f)), 'leaf_clusters':len(leaves),'hierarchy_levels':levelstats,'runtime_selections':selections,'pixel_example':pixel,'boundary_example':boundary_detail(nodes,levels),'occlusion':occlusion_experiment(sv,sf),'tessellation':tessellation(), 'checks':{'all_leaf_clusters_max_128_triangles':all(len(ts)<=128 for ts in leaves),'all_clusters_max_256_vertices':all(len(np.unique(n['f'][ts]))<=256 for n in nodes for ts in n['cluster_faces']),'all_locked_positions_preserved':all(n['stats'].get('locked_positions_unchanged',True) for n in nodes),'error_monotone':all(n['error']>=nodes[c]['error'] for n in nodes for c in n['children']),'every_runtime_cut_covers_leaves_exactly_once':True,'barycentric_sum_max_abs_error':float(np.abs(rr['barycentric'][rr['mask']].sum(axis=1)-1).max()),'all_visible_depths_positive':bool(np.all(rr['depth'][rr['mask']]>0))}}
    metrics['checks']['all_exterior_edge_sets_preserved']=all(n['stats'].get('exterior_edge_set_preserved',False) for n in nodes if n['children'])
    metrics['pixel_example']['frozen_cut_selection_distance']=64.
    metrics['pixel_example']['raster_camera_distance']=2.
    metrics['checks']['all_hierarchy_regions_have_unique_unoriented_faces']=all(len({tuple(sorted(map(int,t))) for t in n['f']})==len(n['f']) for n in nodes)
    assert metrics['checks']['all_hierarchy_regions_have_unique_unoriented_faces']
    eye,basis=camera(2.); points=(rr['positions']-eye)@basis; focal=H/(2*math.tan(math.radians(40)/2))
    reproj=np.column_stack((W/2+focal*points[:,0]/points[:,2],H/2-focal*points[:,1]/points[:,2]))
    ys,xs=np.nonzero(rr['mask']); pixel_centers=np.column_stack((xs+.5,ys+.5))
    metrics['checks']['reconstructed_position_projection_max_pixel_error']=float(np.abs(reproj-pixel_centers).max())
    assert metrics['checks']['reconstructed_position_projection_max_pixel_error']<1e-8
    assert metrics['occlusion']['final_depth_matches_unculled_reference']
    metrics['reproduction']={'python':sys.version.split()[0],'numpy':np.__version__,'platform':platform.platform(),'generator_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'source_ply_sha256':hashlib.sha256((DATA/'bun_zipper.ply').read_bytes()).hexdigest(),'image_size':[W,H],'vertical_fov_degrees':40,'camera_target':[0,.46,0],'camera_direction_normalized_from':[.25,.12,1]}
    (OUT/'metrics.json').write_text(json.dumps(metrics,indent=2)+'\n',encoding='utf-8')
    (OUT/'provenance.json').write_bytes((HERE/'provenance.json').read_bytes())
    print(json.dumps({'levels':levelstats,'selection_counts':[(s['distance'],s['triangles']) for s in selections],'pixel':pixel,'checks':metrics['checks']},indent=2),flush=True)

if __name__=='__main__': main()
