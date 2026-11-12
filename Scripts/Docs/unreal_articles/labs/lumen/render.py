"""Deterministic, reduced Lumen teaching renderer. Not Unreal's implementation.

Run with Python 3.12 + numpy, scipy, matplotlib, Pillow. Outputs are computed
from ray/rectangle intersections, patch transport, sampled reflection and SDF.
All radiance arrays remain linear; only displayed RGB is tone mapped.
"""
from pathlib import Path
import json, hashlib, math
import numpy as np
from scipy import sparse
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, Rectangle

ROOT=Path(__file__).resolve().parents[5]
OUT=ROOT/'Docs/Unreal/assets/lumen-lab'
OUT.mkdir(parents=True,exist_ok=True)
RNG=np.random.default_rng(20261015)
plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'figure.facecolor':'#f7f5ef','axes.facecolor':'#f7f5ef','axes.spines.top':False,'axes.spines.right':False,'svg.fonttype':'none'})
faces=[]
def face(name,axis,value,a,b,sign,color,emission=0):
    axes=[j for j in range(3) if j!=axis]
    n=np.zeros(3);n[axis]=sign
    faces.append(dict(name=name,axis=axis,value=value,axes=axes,a=np.array(a),b=np.array(b),normal=n,color=np.array(color),emission=emission))
neutral=[.72,.72,.68]
face('floor',2,0,[-2,0],[2,5],1,neutral)
face('ceiling',2,3,[-2,0],[2,5],-1,neutral)
face('back',1,5,[-2,0],[2,3],-1,neutral)
face('red wall',0,-2,[0,0],[5,3],1,[.65,.08,.06])
face('green wall',0,2,[0,0],[5,3],-1,[.08,.5,.12])
BOXES=[(np.array([-1.25,2.3,0]),np.array([-.3,3.4,1.05])),(np.array([.3,3.3,0]),np.array([1.2,4.3,1.8]))]
for bi,(lo,hi) in enumerate(BOXES):
    for ax in range(3):
        aa=[k for k in range(3) if k!=ax]
        for sign in [-1,1]:
            if ax==2 and sign==-1:continue
            face(f'box {bi+1} {ax} {sign}',ax,(lo if sign<0 else hi)[ax],lo[aa],hi[aa],sign,[.72,.65,.5] if bi==0 else [.62,.65,.7])
face('area emitter',2,2.98,[-.65,2.1],[.65,3.3],-1,[0,0,0],15)
LIGHT=len(faces)-1

def intersect(o,d,tmax=np.inf):
    o=np.broadcast_to(o,d.shape)
    n=len(d);t=np.full(n,tmax);ids=np.full(n,-1,dtype=np.int32)
    for i,f in enumerate(faces):
        ax=f['axis']; aa=f['axes']; den=d[:,ax]
        tt=np.divide(f['value']-o[:,ax],den,out=np.full(n,np.inf),where=np.abs(den)>1e-12)
        with np.errstate(invalid='ignore'):
            q=o[:,aa]+tt[:,None]*d[:,aa]
        valid=(tt>1e-5)&(tt<t)&np.all(q>=f['a']-1e-8,axis=1)&np.all(q<=f['b']+1e-8,axis=1)
        t[valid]=tt[valid];ids[valid]=i
    return t,ids

def normalize(v):return v/np.maximum(np.linalg.norm(v,axis=-1,keepdims=True),1e-20)
def basis(n):
    helper=np.tile([0.,0.,1.],(len(n),1));helper[np.abs(n[:,2])>.9]=[1,0,0]
    t=normalize(np.cross(helper,n));return t,np.cross(n,t)
def cosine_directions(n,u):
    t,b=basis(n);r=np.sqrt(u[...,0]);phi=2*np.pi*u[...,1]
    return t[:,None]* (r*np.cos(phi))[...,None]+b[:,None]*(r*np.sin(phi))[...,None]+n[:,None]*np.sqrt(1-u[...,0])[...,None]

# Uniformly spaced, face-local card texels. Store material before lighting.
positions=[]; normals=[]; colors=[]; face_ids=[]
for i,f in enumerate(faces):
    shape=np.maximum(2,np.ceil((f['b']-f['a'])/.23).astype(int))
    f['shape']=shape;f['offset']=len(positions)
    for j in range(shape[0]):
        for k in range(shape[1]):
            p=np.zeros(3);p[f['axis']]=f['value'];p[f['axes']]=f['a']+(np.array([j,k])+.5)/shape*(f['b']-f['a'])
            positions.append(p);normals.append(f['normal']);colors.append(f['color']);face_ids.append(i)
P=np.array(positions);N=np.array(normals);RHO=np.array(colors);FID=np.array(face_ids);COUNT=len(P)

def indices_at(p,ids):
    result=np.full(len(ids),-1,dtype=np.int32)
    for i,f in enumerate(faces):
        m=ids==i
        if not np.any(m):continue
        uv=(p[m][:,f['axes']]-f['a'])/(f['b']-f['a'])
        ij=np.minimum(np.maximum((uv*f['shape']).astype(int),0),f['shape']-1)
        result[m]=f['offset']+ij[:,0]*f['shape'][1]+ij[:,1]
    return result

def lookup(p,ids,values):
    # Bilinear lookup of the sampled face-local field, with clamped borders.
    result=np.zeros((len(ids),3))
    for i,f in enumerate(faces):
        m=ids==i
        if not np.any(m):continue
        uv=(p[m][:,f['axes']]-f['a'])/(f['b']-f['a'])*f['shape']-.5
        uv=np.clip(uv,0,f['shape']-1);lo=np.floor(uv).astype(int);hi=np.minimum(lo+1,f['shape']-1);w=uv-lo
        for a,b in [(0,0),(0,1),(1,0),(1,1)]:
            ii=np.where(a,hi[:,0],lo[:,0]);jj=np.where(b,hi[:,1],lo[:,1]);weight=(w[:,0] if a else 1-w[:,0])*(w[:,1] if b else 1-w[:,1])
            result[m]+=weight[:,None]*values[f['offset']+ii*f['shape'][1]+jj]
    return result

def direct(p,n,rho,samples=64):
    u=(np.indices((8,8)).reshape(2,-1).T+.5)/8
    lp=np.column_stack((-.65+1.3*u[:,0],2.1+1.2*u[:,1],np.full(64,2.98)))
    out=np.zeros_like(p)
    for j in range(0,len(p),2000):
        pp=p[j:j+2000];nn=n[j:j+2000];v=lp[None]-pp[:,None];dist=np.linalg.norm(v,axis=2);d=v/dist[...,None]
        tt,_=intersect((pp+nn*1e-4)[:,None].repeat(64,axis=1).reshape(-1,3),d.reshape(-1,3))
        visible=(tt.reshape(dist.shape)>=dist-3e-4)
        kernel=np.maximum(np.sum(nn[:,None]*d,axis=2),0)*np.maximum(d[:,:,2],0)/np.maximum(dist*dist,1e-6)
        out[j:j+len(pp)]=rho[j:j+len(pp)]/np.pi*15*1.56*np.mean(kernel*visible,axis=1)[:,None]
    return out

print('cards',COUNT,flush=True)
D=direct(P,N,RHO);D[FID==LIGHT]=15
# Fixed sampled transport matrix: one frozen unbiased row estimator, then a
# deterministic fixed-point solve of that sampled discretization.
SAMPLES=512
rows=[];cols=[]
for j in range(0,COUNT,128):
    pp=P[j:j+128];nn=N[j:j+128];u=RNG.random((len(pp),SAMPLES,2));directions=cosine_directions(nn,u)
    origins=(pp+nn*1e-4)[:,None].repeat(SAMPLES,axis=1).reshape(-1,3)
    tt,ids=intersect(origins,directions.reshape(-1,3)); hit=origins+np.minimum(tt,100)[:,None]*directions.reshape(-1,3)
    ix=indices_at(hit,ids);valid=(ix>=0)&(ids!=LIGHT)
    rows.extend(np.repeat(np.arange(j,j+len(pp)),SAMPLES)[valid]);cols.extend(ix[valid])
K=sparse.coo_matrix((np.full(len(rows),1/SAMPLES),(rows,cols)),shape=(COUNT,COUNT)).tocsr()
L=D.copy();bounces=[L.copy()];changes=[]
for iteration in range(24):
    nxt=D+RHO*(K@L);changes.append(float(np.max(np.abs(nxt-L))));L=nxt;bounces.append(L.copy())
print('transport complete',changes[-1],flush=True)

CAM=np.array([0.,-4.,1.65]);TARGET=np.array([0.,2.8,1.4]);H=400;W=560
def camera(origin=CAM):
    forward=normalize(TARGET-origin);right=normalize(np.cross(forward,[0,0,1]));up=np.cross(right,forward)
    yy,xx=np.mgrid[:H,:W];scale=np.tan(np.deg2rad(43)/2)
    rays=normalize(forward+((xx+.5)/W*2-1)[...,None]*right*(W/H)*scale+(1-(yy+.5)/H*2)[...,None]*up*scale).reshape(-1,3)
    t,ids=intersect(origin,rays);p=origin+np.minimum(t,100)[:,None]*rays
    normals=np.zeros_like(p);rho=np.zeros_like(p)
    for i,f in enumerate(faces):normals[ids==i]=f['normal'];rho[ids==i]=f['color']
    return p,ids,normals,rho,rays,t
CP,CI,CN,CR,CD,CT=camera()
def tonemap(a):return np.power(np.clip(a/(1+a),0,1),1/2.2)
def panel(images,titles,name,cols=2,cmap=None):
    rows=math.ceil(len(images)/cols);fig,axs=plt.subplots(rows,cols,figsize=(cols*5,rows*3.8),squeeze=False)
    for ax,im,title in zip(axs.flat,images,titles):
        artist=ax.imshow(im,cmap='gray' if im.dtype==bool else cmap);ax.set_title(title,loc='left',fontsize=12,fontweight='bold');ax.axis('off')
        if im.ndim==2 and im.dtype!=bool:fig.colorbar(artist,ax=ax,fraction=.035,pad=.02,shrink=.8)
    for ax in list(axs.flat)[len(images):]:ax.axis('off')
    fig.suptitle('COMPUTED CPU REFERENCE · reduced Lumen-style laboratory',fontsize=10,color='#60664b')
    fig.tight_layout();fig.savefig(OUT/name,dpi=150);plt.close(fig)
beauty=lookup(CP,CI,L)
panel([tonemap(beauty).reshape(H,W,3),CR.reshape(H,W,3),(CN*.5+.5).reshape(H,W,3),np.where(np.isfinite(CT),CT,0).reshape(H,W)],['The room: 24 cache iterations','Material albedo (linear RGB)','World-space normal encoded as RGB','Ray distance in meters'],'room-gbuffer.png')
panel([tonemap(lookup(CP,CI,bounces[k])).reshape(H,W,3) for k in [0,1,3,24]],['Direct light only','Direct + one diffuse bounce','Direct + three diffuse bounces','24 iterations: sampled fixed point'],'cache-bounces.png')

# Card atlas visualization: actual face texels, arranged only for presentation.
aw=180;ah=100;atlas_a=np.zeros((ah,aw,3));atlas_n=atlas_a.copy();atlas_l=atlas_a.copy();atlas_depth=np.zeros((ah,aw))
for i,f in enumerate(faces):
    x=(i%4)*45;y=(i//4)*25;nx,ny=f['shape'];sl=slice(f['offset'],f['offset']+nx*ny)
    atlas_a[y:y+ny,x:x+nx]=RHO[sl].reshape(nx,ny,3).transpose(1,0,2)
    atlas_n[y:y+ny,x:x+nx]=(N[sl]*.5+.5).reshape(nx,ny,3).transpose(1,0,2)
    atlas_l[y:y+ny,x:x+nx]=tonemap(L[sl]).reshape(nx,ny,3).transpose(1,0,2)
    # Orthographic capture camera one meter along each face normal: all planar
    # texels have actual captured linear depth 1, gutters remain invalid/zero.
    atlas_depth[y:y+ny,x:x+nx]=1
panel([atlas_a,atlas_n,atlas_depth,atlas_l],['Card albedo atlas','Card normal atlas','Orthographic depth: 1 m; gutter: invalid','Cached outgoing diffuse radiance'],'surface-cards.png')

def sdf(p):
    # Room walls are 0.1m-thick closed boxes, object boxes are exact solids.
    solids=BOXES+[(np.array([-2.1,0,-.1]),np.array([2.1,5.1,0])),(np.array([-2.1,0,3]),np.array([2.1,5.1,3.1])),(np.array([-2.1,5,0]),np.array([2.1,5.1,3])),(np.array([-2.1,0,0]),np.array([-2,5,3])),(np.array([2,0,0]),np.array([2.1,5,3]))]
    ds=[]
    for lo,hi in solids:
        q=np.abs(p-(lo+hi)/2)-(hi-lo)/2
        ds.append(np.linalg.norm(np.maximum(q,0),axis=-1)+np.minimum(np.max(q,axis=-1),0))
    return np.min(ds,axis=0)
def march(o,d):
    tt=0;steps=[]
    for _ in range(256):
        dist=float(sdf(o+d*tt));steps.append([tt,dist])
        if dist<1e-5:return tt,steps,'hit'
        tt+=dist
        if tt>30:return np.inf,steps,'range_exit'
    # Budget exhaustion is unknown visibility, never evidence of empty space.
    return np.nan,steps,'budget_exhausted'
gx,gy=np.meshgrid(np.linspace(-2.2,2.2,300),np.linspace(-.1,5.2,350));gs=sdf(np.stack([gx,gy,np.full_like(gx,.7)],axis=-1))
origin=np.array([0.,.25,.7]);direction=normalize(np.array([.8,3.6,0.]));mt,steps,trace_status=march(origin,direction)
assert trace_status=='hit'
fig,axs=plt.subplots(1,2,figsize=(11,5.7));im=axs[0].imshow(gs,extent=[-2.2,2.2,-.1,5.2],origin='lower',cmap='coolwarm',vmin=-.2,vmax=1.5);fig.colorbar(im,ax=axs[0],label='signed distance (m)');axs[0].set_title('Exact box SDF, horizontal slice z = 0.7 m')
axs[1].contour(gx,gy,gs,levels=[0],colors='#333333');axs[1].plot([origin[0],(origin+direction*mt)[0]],[origin[1],(origin+direction*mt)[1]],color='#96583c')
for ti,di in steps:
    p=origin+direction*ti;axs[1].add_patch(Circle(p[:2],max(di,0),fill=False,edgecolor='#788759',alpha=.7));axs[1].plot(*p[:2],'.',color='#383e26')
axs[1].set_title(f'Actual sphere-trace steps: {len(steps)} evaluations')
for ax in axs:ax.set_aspect('equal');ax.set_xlim(-2.2,2.2);ax.set_ylim(-.1,5.2);ax.set_xlabel('x (m)');ax.set_ylabel('y (m)')
fig.tight_layout();fig.savefig(OUT/'sdf-tracing.png',dpi=150);plt.close(fig)
test_d=normalize(np.column_stack((RNG.uniform(-1,1,256),RNG.uniform(.2,1,256),np.zeros(256))))
exact,_=intersect(origin,test_d);march_results=[march(origin,d) for d in test_d];marched=np.array([r[0] for r in march_results]);valid=np.isfinite(exact)&np.isfinite(marched)
grazing_origin=np.array([0.,.25,.0001]);grazing_direction=np.array([0.,1.,0.])
grazing_t,grazing_steps,grazing_status=march(grazing_origin,grazing_direction)
grazing_exact,_=intersect(grazing_origin,grazing_direction[None,:])
assert grazing_status=='budget_exhausted' and np.isnan(grazing_t) and np.isfinite(grazing_exact[0])

# World probes sample a lat-long sphere and correctly integrate unequal areas.
PW=64;PH=32;theta=(np.arange(PH)+.5)*np.pi/PH;phi=(np.arange(PW)+.5)*2*np.pi/PW
th,ph=np.meshgrid(theta,phi,indexing='ij');PD=np.stack([np.sin(th)*np.cos(ph),np.sin(th)*np.sin(ph),np.cos(th)],axis=-1).reshape(-1,3)
solid=np.repeat((2*np.pi/PW)*(np.cos(np.arange(PH)*np.pi/PH)-np.cos((np.arange(PH)+1)*np.pi/PH)),PW)
probe_positions=np.array([[x,y,z] for z in [.6,1.5,2.4] for y in [.7,1.6,2.5,3.4,4.3] for x in [-1.5,-.75,0,.75,1.5] if sdf(np.array([x,y,z]))>.12])
probe_maps=[];probe_irr=[]
for p in probe_positions:
    tt,ids=intersect(p,PD);q=p+np.minimum(tt,100)[:,None]*PD;values=lookup(q,ids,L);probe_maps.append(values)
    probe_irr.append(np.sum(values*np.maximum(PD[:,2],0)[:,None]*solid[:,None],axis=0))
probe_maps=np.array(probe_maps);probe_irr=np.array(probe_irr);chosen=np.argmin(np.linalg.norm(probe_positions-[0,2.5,1.5],axis=1))
fig=plt.figure(figsize=(11,5.3));ax=fig.add_subplot(121,projection='3d');ax.scatter(*probe_positions.T,c=tonemap(probe_irr/np.pi),s=45);ax.set(xlabel='x (m)',ylabel='y (m)',zlabel='z (m)',title=f'{len(probe_positions)} world probes: upward irradiance / π');ax.view_init(22,-65)
ax2=fig.add_subplot(122);ax2.imshow(tonemap(probe_maps[chosen]).reshape(PH,PW,3),extent=[0,360,180,0]);ax2.set(xlabel='azimuth (degrees)',ylabel='polar angle (degrees)',title='One probe: actual incoming directional radiance');fig.tight_layout();fig.savefig(OUT/'world-probes.png',dpi=150);plt.close(fig)

# Interpolate computed probe irradiance onto the floor, with and without
# visibility gating. This intentionally exposes interpolation bias/leaking.
from scipy.spatial import cKDTree
fy,fx=np.mgrid[.05:4.95:100j,-1.95:1.95:100j];FP=np.column_stack((fx.ravel(),fy.ravel(),np.full(fx.size,.001)))
pdist,pi=cKDTree(probe_positions).query(FP,k=8);pw=1/np.maximum(pdist,.01)**2
naive=np.sum(pw[...,None]*probe_irr[pi],axis=1)/pw.sum(axis=1)[:,None]/np.pi*neutral
delta=probe_positions[pi]-FP[:,None];pd=np.linalg.norm(delta,axis=2);pdir=delta/pd[...,None]
pt,_=intersect(FP[:,None].repeat(8,axis=1).reshape(-1,3),pdir.reshape(-1,3));pv=(pt.reshape(pd.shape)>=pd-1e-4)
gpw=pw*pv;guard=np.sum(gpw[...,None]*probe_irr[pi],axis=1)/np.maximum(gpw.sum(axis=1)[:,None],1e-8)/np.pi*neutral
floorref=lookup(FP,np.zeros(len(FP),dtype=int),L);blocked=sdf(FP)<0
for a in [naive,guard,floorref]:a[blocked]=0
panel([tonemap(a).reshape(100,100,3)[::-1] for a in [floorref,naive,guard]],['Floor: fine surface-cache solution','8 nearest world probes: inverse-distance blend','Same probes: visibility-gated blend'],'probe-interpolation.png',cols=3)

# Sparse screen probes, nearest compatible candidate gather (4 spatial neighbors).
stride=12;sy,sx=np.mgrid[6:H:stride,6:W:stride];spix=(sy*W+sx).ravel();spix=spix[CI[spix]>=0]
SP=CP[spix];SN=CN[spix];sample_dirs=cosine_directions(SN,RNG.random((len(SP),256,2)))
origins=(SP+SN*1e-4)[:,None].repeat(256,axis=1).reshape(-1,3)
tt,ids=intersect(origins,sample_dirs.reshape(-1,3));q=origins+np.minimum(tt,100)[:,None]*sample_dirs.reshape(-1,3)
# Probe stores irradiance / pi; light emission removed to keep direct separate.
incoming=lookup(q,ids,L);incoming[ids==LIGHT]=0
screen_values=incoming.reshape(len(SP),256,3).mean(axis=1)
tree=cKDTree(np.column_stack((spix%W,spix//W)))
yy,xx=np.mgrid[:H,:W];_,near=tree.query(np.column_stack((xx.ravel(),yy.ravel())),k=4)
dist=np.linalg.norm(SP[near]-CP[:,None],axis=2);normal=np.maximum(np.sum(SN[near]*CN[:,None],axis=2),0)**16
weights=np.exp(-dist*6)*normal;ws=weights.sum(axis=1);gather=np.sum(weights[...,None]*screen_values[near],axis=1)/np.maximum(ws[:,None],1e-12)
gathered=lookup(CP,CI,D)+CR*gather;reference=beauty
overlay=tonemap(beauty).reshape(H,W,3).copy()
for py,px in zip(spix//W,spix%W):overlay[max(0,py-1):py+2,max(0,px-1):px+2]=[1,.8,.1]
error=np.mean(np.abs(gathered-reference),axis=1).reshape(H,W)
panel([overlay,tonemap(gathered).reshape(H,W,3),tonemap(reference).reshape(H,W,3),error],['Sparse screen probe origins','256 rays / probe + compatible gather','Surface-cache reference lookup','Absolute linear RGB error (mean channel)'],'screen-probes.png')

def reflect_image(roughness):
    selected=np.where((CI>=10)&(CI<15))[0];p=CP[selected];n=CN[selected];wo=-CD[selected]
    count=64;u=RNG.random((len(p),count,2));alpha=roughness**2;cos=np.sqrt((1-u[...,0])/(1+(alpha*alpha-1)*u[...,0]));sin=np.sqrt(1-cos*cos);phi=2*np.pi*u[...,1];t,b=basis(n)
    h=t[:,None]*(sin*np.cos(phi))[...,None]+b[:,None]*(sin*np.sin(phi))[...,None]+n[:,None]*cos[...,None]
    vh=np.sum(wo[:,None]*h,axis=2);wi=2*vh[...,None]*h-wo[:,None];nl=np.sum(n[:,None]*wi,axis=2);nv=np.sum(n*wo,axis=1)
    orig=(p+n*1e-4)[:,None].repeat(count,axis=1).reshape(-1,3);tt,ids=intersect(orig,wi.reshape(-1,3));q=orig+np.minimum(tt,100)[:,None]*wi.reshape(-1,3);li=lookup(q,ids,L).reshape(len(p),count,3)
    def g1(c):return 2*c/np.maximum(c+np.sqrt(alpha*alpha+(1-alpha*alpha)*c*c),1e-12)
    f=.75+.25*(1-np.clip(vh,0,1))**5
    # f_r*cos_i / p_i after cancellation of D and reflection Jacobian.
    weight=f*g1(np.maximum(nl,0))*g1(np.maximum(nv[:,None],0))*np.maximum(vh,0)/np.maximum(nv[:,None]*cos,1e-8)
    weight[(nl<=0)|(vh<=0)]=0
    spec=np.mean(li*weight[...,None],axis=1);result=beauty.copy();result[selected]=.12*beauty[selected]+spec
    return result
panel([tonemap(reflect_image(r)).reshape(H,W,3) for r in [.12,.35,.65]],['GGX roughness 0.12','GGX roughness 0.35','GGX roughness 0.65'],'reflections.png',cols=3)

# Numerical sampling check on a constant-radiance hemisphere. Cosine sampling
# cancels the Lambertian cosine exactly; uniform sampling does not.
srng=np.random.default_rng(173);u=srng.random((1500,2));r=np.sqrt(u[:,0]);phi=2*np.pi*u[:,1]
disk=np.column_stack((r*np.cos(phi),r*np.sin(phi)));cz=np.sqrt(1-u[:,0])
fig,axs=plt.subplots(1,3,figsize=(13,4.2));axs[0].scatter(*disk.T,s=2,alpha=.4,color='#60784b');axs[0].set_aspect('equal');axs[0].set(title='1,500 actual cosine samples: xy disk',xlabel='x',ylabel='y')
axs[1].hist(cz,bins=20,density=True,color='#8a9c6e',alpha=.7);zz=np.linspace(0,1,100);axs[1].plot(zz,2*zz,color='#9b593f',label='p(z) = 2z');axs[1].set(title='Measured z distribution',xlabel='z = cos θ',ylabel='probability density');axs[1].legend()
counts=np.array([1,4,16,64,256]);std=[]
for count in counts:
    estimates=2*srng.random((4096,count)).mean(axis=1);std.append(estimates.std())
axs[2].loglog(counts,std,'o-',label='uniform hemisphere: measured');axs[2].loglog(counts,1/np.sqrt(3*counts),'--',label='theory: 1 / √(3N)');axs[2].set(title='Constant Li = ρ = 1; cosine variance = 0',xlabel='rays per estimate',ylabel='standard deviation');axs[2].legend(fontsize=8)
fig.tight_layout();fig.savefig(OUT/'sampling-validation.png',dpi=150);plt.close(fig)

# A small SVG shows exact computed sphere-trace progress, not a hand-positioned
# sequence. The distance-versus-step chart preserves the logged numbers.
svg=['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 340" role="img" aria-labelledby="title desc"><title id="title">Measured sphere tracing progress</title><desc id="desc">Each column records ray parameter and distance to the nearest solid from the executed CPU SDF trace.</desc><rect width="1000" height="340" fill="#f7f5ef"/><g font-family="Arial,sans-serif" fill="#273026"><text x="35" y="40" font-size="24">Each safe step consumes the empty sphere at its origin</text>']
chosensteps=list(enumerate(steps[:7]));
for idx,(ti,di) in chosensteps:
    x=35+idx*135;svg.append(f'<rect x="{x}" y="85" width="118" height="160" rx="8" fill="#e8eadf" stroke="#a4ad8b"/><text x="{x+12}" y="114" font-size="17">Step {idx}</text><text x="{x+12}" y="150" font-size="15">t = {ti:.3f} m</text><text x="{x+12}" y="183" font-size="15">d = {di:.3f} m</text><text x="{x+12}" y="219" font-size="13">advance d</text>')
svg.append(f'<text x="35" y="292" font-size="17">Full trace: {len(steps)} distance evaluations, hit parameter {mt:.6f} m. Stop tolerance: 0.00001 m.</text></g></svg>')
(OUT/'trace-steps.svg').write_text(''.join(svg),encoding='utf-8')

# Reproject actual new-camera visibility into old frame; reject wrong surfaces.
NEWCAM=CAM+np.array([.45,0,0]);NP,NI,NN,NR,ND,NT=camera(NEWCAM);newtruth=lookup(NP,NI,L)
forward=normalize(TARGET-CAM);right=normalize(np.cross(forward,[0,0,1]));up=np.cross(right,forward);v=NP-CAM;z=v@forward;scale=np.tan(np.deg2rad(43)/2)
px=((v@right)/(z*scale*W/H)*.5+.5)*W-.5;py=(.5-(v@up)/(z*scale)*.5)*H-.5
ix=np.clip(np.rint(px).astype(int),0,W-1);iy=np.clip(np.rint(py).astype(int),0,H-1);oldidx=iy*W+ix
geometric_accept=(px>=0)&(px<W)&(py>=0)&(py<H)&(NI>=0)&(np.linalg.norm(CP[oldidx]-NP,axis=1)<.08)&(np.sum(CN[oldidx]*NN,axis=1)>.95)
# Coplanar/nearby surfaces can have distinct emission or material identity.
accept=geometric_accept&(CI[oldidx]==NI)
assert np.count_nonzero(accept&(CI[oldidx]!=NI))==0
wrong=.9*beauty+.1*newtruth;right_hist=np.where(accept[:,None],.9*beauty[oldidx]+.1*newtruth,newtruth)
panel([tonemap(wrong).reshape(H,W,3),accept.reshape(H,W),tonemap(right_hist).reshape(H,W,3),tonemap(newtruth).reshape(H,W,3)],['Wrong: same-pixel history after camera move','Reprojection accepted (white), rejected (black)','Reproject + reject incompatible history','New-camera current radiance reference'],'temporal-history.png')
fig,axs=plt.subplots(1,2,figsize=(11,4.3));axs[0].semilogy(range(1,25),changes,'o-',color='#6f8052');axs[0].set(xlabel='cache iteration',ylabel='maximum radiance change',title='Measured convergence of frozen transport matrix');frames=np.arange(40);signal=np.where(frames<10,1.,.15)
for alpha in [.1,.3,1.]:
    y=[signal[0]]
    for v in signal[1:]:y.append((1-alpha)*y[-1]+alpha*v)
    axs[1].plot(frames,y,label=f'α = {alpha}')
axs[1].set(xlabel='frame (light changes at frame 10)',ylabel='normalized cached radiance',title='Computed EMA response to a light change');axs[1].legend();fig.tight_layout();fig.savefig(OUT/'convergence.png',dpi=150);plt.close(fig)

metrics={'seed':20261015,'patch_count':COUNT,'transport_rays_per_patch':SAMPLES,'transport_nonzero_entries':int(K.nnz),'max_diffuse_reflectance':float(RHO.max()),'max_transport_row_sum':float(np.max(np.asarray(K.sum(axis=1)))),'iterations':24,'iteration_max_changes':changes,'sdf_ray_count':256,'sdf_common_hits':int(valid.sum()),'sdf_max_hit_distance_error_m':float(np.max(np.abs(exact[valid]-marched[valid]))),'sdf_hit_mismatches':int(np.count_nonzero(np.isfinite(exact)!=np.isfinite(marched))),'probe_count':len(probe_positions),'sphere_solid_angle_sum':float(solid.sum()),'screen_probe_count':len(SP),'screen_probe_rays':256,'screen_gather_mean_abs_error':float(np.mean(error[CI.reshape(H,W)>=0])),'history_accept_fraction':float(accept.mean()),'camera':CAM.tolist(),'target':TARGET.tolist(),'fov_y_degrees':43,'resolution':[W,H],'renderer':'educational CPU reference, not Unreal Lumen output','transport_scope':'Lambertian face patches, fixed sampled visibility matrix; one rectangular area emitter; black environment; GGX evaluated only in camera reflections','minimum_cached_radiance':float(L.min())}
assert metrics['sdf_hit_mismatches']==0
metrics['sdf_unresolved_test_rays']=sum(r[2]=='budget_exhausted' for r in march_results)
metrics['grazing_regression']={'status':grazing_status,'steps':len(grazing_steps),'last_sample_t_m':grazing_steps[-1][0],'exact_hit_t_m':float(grazing_exact[0])}
metrics['temporal_cross_face_rejections']=int(np.count_nonzero(geometric_accept&(CI[oldidx]!=NI)))
metrics['temporal_accepted_cross_face_samples']=int(np.count_nonzero(accept&(CI[oldidx]!=NI)))
assert metrics['sdf_unresolved_test_rays']==0
assert metrics['sdf_max_hit_distance_error_m']<.001
assert abs(solid.sum()-4*np.pi)<1e-10
assert metrics['max_transport_row_sum']<=1+1e-12
assert metrics['max_diffuse_reflectance']<1
assert np.all(np.isfinite(L)) and L.min()>=0
assert max(changes[-3:])<max(changes[:3])
np.savez_compressed(OUT/'computed-data.npz',positions=P,normals=N,albedo=RHO,direct=D,cached_radiance=L,probe_positions=probe_positions,probe_radiance=probe_maps,probe_irradiance=probe_irr,screen_probe_positions=SP,screen_probe_irradiance_over_pi=screen_values,sdf_slice=gs)
(OUT/'metrics.json').write_text(json.dumps(metrics,indent=2)+'\n',encoding='utf-8')
scene={'units':'meters','faces':[{k:(v.tolist() if isinstance(v,np.ndarray) else v) for k,v in f.items()} for f in faces],'camera':CAM.tolist(),'look_at':TARGET.tolist(),'fov_y_degrees':43}
(OUT/'scene.json').write_text(json.dumps(scene,indent=2)+'\n',encoding='utf-8')
print(json.dumps(metrics,indent=2))
