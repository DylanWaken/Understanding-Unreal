"""Original explanatory diagrams and a computed two-patch convergence plot.

These SVGs are diagrams, never Unreal screenshots. Runtime images and buffers
are published separately with capture provenance.
"""
from pathlib import Path
from html import escape
import math
import json
import hashlib

ROOT = Path(__file__).resolve().parents[5]
OUT = ROOT / 'Docs/Unreal/assets/lumen-v2/diagrams'
PAPER, INK, MUTED, RULE = '#fffdf8', '#22312d', '#586760', '#c4cec7'
GREEN, BLUE, GOLD, RED = '#286c56', '#286788', '#93661e', '#aa5343'

class SVG:
    def __init__(self, title, desc, width=1200, height=720):
        self.width, self.height = width, height
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
          f'<title id="title">{escape(title)}</title><desc id="desc">{escape(desc)}</desc>',
          '<defs>'+''.join(f'<marker id="arrow-{i}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 Z" fill="{c}"/></marker>' for i,c in enumerate([GREEN,BLUE,GOLD,RED,MUTED]))+'</defs>',
          f'<rect width="{width}" height="{height}" fill="{PAPER}"/>',
          '<g font-family="Segoe UI, Arial, sans-serif">']
        self.text(36,48,title,30,bold=True)
    def text(self,x,y,value,size=21,color=INK,bold=False,anchor='start'):
        for i,line in enumerate(value.split('\n')):
            self.parts.append(f'<text x="{x}" y="{y+i*(size+9)}" font-size="{size}" fill="{color}" font-weight="{600 if bold else 400}" text-anchor="{anchor}">{escape(line)}</text>')
    def rect(self,x,y,w,h,fill=PAPER,stroke=RULE,rx=8):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')
    def box(self,x,y,w,h,title,lines='',color=GREEN):
        self.rect(x,y,w,h,stroke=color)
        self.parts.append(f'<path d="M{x+12} {y+1}H{x+w-12}" stroke="{color}" stroke-width="4"/>')
        title_size=23
        body_size=19
        self.text(x+18,y+35,title,title_size,color,True)
        if lines:self.text(x+18,y+69,lines,body_size)
    def line(self,x1,y1,x2,y2,color=MUTED,width=2,dash=False,arrow=False):
        i=[GREEN,BLUE,GOLD,RED,MUTED].index(color)
        self.parts.append(f'<path d="M{x1} {y1}L{x2} {y2}" fill="none" stroke="{color}" stroke-width="{width}"'+(' stroke-dasharray="8 6"' if dash else '')+(f' marker-end="url(#arrow-{i})"' if arrow else '')+'/>')
    def circle(self,x,y,r=6,color=GREEN):
        self.parts.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{color}"/>')
    def write(self,name,note='Original explanatory diagram; positions and colors are schematic.'):
        self.text(36,self.height-24,note,16,MUTED)
        self.parts.append('</g></svg>')
        p=OUT/name;p.write_text('\n'.join(self.parts)+'\n',encoding='utf-8')
        return p

def path_space():
    s=SVG('One path, two directions of reasoning','A physical path goes from emitter x0 through scattering surfaces x1 and x2 to sensor x3. Camera sampling constructs the same path in reverse. Geometry, scattering, emission, and sensor response remain distinct.',height=650)
    points=[(130,'x₀','Emitter'),(440,'x₁','Secondary surface'),(750,'x₂','Visible receiver'),(1060,'x₃','Sensor')]
    for x,v,label in points:
        s.circle(x,205,12);s.text(x,164,v,32,anchor='middle',bold=True);s.text(x,247,label,21,anchor='middle')
    for i in range(3):
        a,b=points[i][0],points[i+1][0];s.line(a+17,205,b-17,205,GREEN,4,arrow=True)
        s.text((a+b)/2,296,f'G(x{i} ↔ x{i+1})',21,anchor='middle')
    s.text(565,110,'PHYSICAL LIGHT FLOW',18,GREEN,True,anchor='middle')
    s.box(40,340,210,118,'Emission','Lₑ(x₀ → x₁)')
    s.box(285,340,280,118,'Scattering at x₁','fₛ(x₀ → x₁ → x₂)')
    s.box(600,340,280,118,'Scattering at x₂','fₛ(x₁ → x₂ → x₃)')
    s.box(915,340,245,118,'Sensor response','Wₑ⁽ʲ⁾(x₂ → x₃)')
    s.line(1060,520,130,520,BLUE,3,True,True)
    s.text(595,557,'Camera sampling begins at the sensor; the notation still follows light.',21,BLUE,anchor='middle')
    return s.write('path-space.svg')

def partition():
    s=SVG('Partition contributions before assigning passes','The exact source and lobe partition has emission, selected analytical direct illumination, remaining diffuse, and remaining glossy terms. Lumen may include sky and emissive source connections, not only multibounce paths.',height=690)
    s.box(390,90,420,92,'Outgoing radiance toward camera','Lₒ = emission + direct + remaining',GREEN)
    labels=[('Emission','Lₑ','Visible emitting material','Base-pass contribution'),('Analytical direct','∫ (f_d + f_g) Lᵢ,ᴬ dσ⊥','Selected local light set','Deferred lights + visibility'),('Remaining diffuse','∫ f_d Lᵢ,ᴿ dσ⊥','Sky, emitters, reflected light','Probe gathering + material'),('Remaining glossy','∫ f_g Lᵢ,ᴿ dσ⊥','Same remaining source field','Rough/dedicated reflections')]
    for i,(title,equation,source,route) in enumerate(labels):
        x=28+i*296;s.box(x,270,272,195,title,equation+'\n'+source+'\n'+route,[GREEN,BLUE,GOLD,RED][i]);s.line(x+136,260,600,196,[GREEN,BLUE,GOLD,RED][i],2,arrow=True)
    s.rect(55,515,1090,105,fill='#edf3ee')
    s.text(80,552,'A light → wall → receiver path is different from a light → receiver path.',23,GREEN,True)
    s.text(80,589,'Keep secondary cached direct light. Blend overlapping glossy representations instead of adding them.',19)
    return s.write('transport-partition.svg')

def dataflow():
    s=SVG('From surface state to the composed image','A dependency diagram connects raster material state, card irradiance and final radiance, selected visibility, screen and world tracing, probe integration, dedicated reflections, and composition. Arrows describe data dependencies, not a single forced scheduling order.',width=1280,height=1060)
    cols=[40,460,880];w=360
    s.box(cols[0],100,w,130,'Rasterized primary surface','Depth + normal + material\nGBuffer: no incoming-light integral')
    s.box(cols[1],100,w,130,'Captured card material','Albedo, normal, emission, depth\nPage table locates persistent data')
    s.box(cols[2],100,w,130,'Selected light sources','Primary visibility: VSM / selected path\nCard shadows use their own tracer',BLUE)
    s.box(cols[0],330,w,125,'Screen tracing','HZB geometry + selected color\nHistory is separately validated',BLUE)
    s.box(cols[1],330,w,125,'Card lighting updates','Direct irradiance + radiosity\nE_direct and E_indirect',GOLD)
    s.box(cols[2],330,w,125,'Primary direct lighting','Material BRDF × light contribution\nVisibility belongs to the connection',BLUE)
    s.box(cols[0],560,w,130,'Unresolved geometry / far field','Configured SDF or HWRT queries\nWorld radiance-cache lookup',BLUE)
    s.box(cols[1],560,w,130,'Final card lighting','L_cache = Lₑ + (ρ/π)(E_D + E_I)\nDiffuse outgoing radiance',GOLD)
    s.box(cols[2],560,w,130,'Dedicated reflections','Glossy ray → selected hit shading\nResolve + temporal/spatial filtering',RED)
    s.box(cols[0],820,w,135,'Screen-probe integration','Reconstructed incoming radiance\nOutputs: E/π and rough specular')
    s.box(cols[2],820,w,135,'Material and composition','ρ × diffuse response + glossy blend\nAdd owned emission/direct terms',RED)
    for col in [0,1,2]:s.line(cols[col]+w/2,235,cols[col]+w/2,320,arrow=True)
    s.line(880,215,815,330,BLUE,arrow=True)
    s.line(640,460,640,550,GOLD,arrow=True)
    s.line(220,460,220,550,BLUE,arrow=True)
    s.line(460,625,410,625,GOLD,arrow=True)
    s.line(820,625,870,625,GOLD,arrow=True)
    s.line(220,695,220,810,BLUE,arrow=True)
    s.line(460,690,350,810,GOLD,arrow=True)
    s.line(1060,695,1060,810,RED,arrow=True)
    s.line(400,885,870,885,GREEN,arrow=True)
    s.text(625,867,'probe diffuse + rough specular',17,GREEN,anchor='middle')
    s.line(1240,430,1258,430,BLUE);s.line(1258,430,1258,795,BLUE);s.line(1258,795,1220,810,BLUE,arrow=True);s.text(1090,770,'direct contribution',17,BLUE)
    s.text(40,995,'History feeds cache updates and reconstruction; it must be reprojected, validated and exposure-corrected.',18,MUTED)
    return s.write('lumen-dataflow.svg','Original dependency diagram. Optional branches and asynchronous overlap are explained in the text.')

def probes():
    s=SVG('Three probe domains; different data and different jobs','Card radiosity probes sit on parameterized secondary surfaces. Screen probes sit on selected camera-visible surfaces. World radiance-cache probes occupy sparse clipmaps and store directional radiance.',height=760)
    cards=[(30,'Card radiosity',GOLD),(425,'Screen probes',GREEN),(820,'World radiance cache',BLUE)]
    for x,title,color in cards:s.box(x,100,350,535,title,color=color)
    for iy in range(5):
        for ix in range(6):
            x=80+ix*40+iy*6;y=220+iy*35;s.circle(x,y,5,GOLD)
    s.line(60,190,320,190,GOLD);s.line(60,405,320,405,GOLD)
    s.text(55,455,'Positions on captured cards\nTrace hemisphere → SH moments\nCosine transfer → irradiance\nFeeds secondary diffuse radiance',19)
    for iy in range(5):
        for ix in range(7):s.circle(465+ix*42,215+iy*37,5,GREEN)
    for x,y in [(565,245),(577,250),(590,275),(610,320)]:s.circle(x,y,4,RED)
    s.text(450,455,'Uniform grid + adaptive positions\nDirectional radiance per probe\nFiltered → E/π and rough specular\nReconstructed at visible pixels',19)
    for j in range(3):s.rect(853+20*j,190+22*j,275-40*j,215-44*j,stroke=BLUE)
    for x,y in [(905,270),(960,255),(1015,270),(960,305),(905,325),(1015,325),(960,350),(960,290)]:s.circle(x,y,5,BLUE)
    s.text(845,455,'Sparse positions in world clipmaps\nIndirection + offsets + age/budget\nEight-neighbor radiance lookup\nParallax proxy, not exact visibility',19)
    s.text(50,690,'Each directional atlas cell also needs its direction, angular footprint, hit state, format, and exposure convention.',21)
    return s.write('probe-domains.svg')

def vsm():
    s=SVG('Virtual Shadow Map: from a receiver to visibility','A receiver is projected into a light clipmap or mip. Its virtual page maps to a resident physical page containing depth. SMRT approximates finite-source visibility. Page allocation and invalidation are separate from BRDF evaluation.',height=820)
    s.box(35,115,330,130,'Receiver and light','World position → light projection\nSelect clipmap / mip',BLUE)
    s.box(435,115,330,130,'Virtual address','Map ID + virtual page coordinate\nLocal texel within 128 × 128 page',BLUE)
    s.box(835,115,330,130,'Page-table lookup','Resident physical address + flags\nValidity and cached-state metadata',GOLD)
    s.line(370,180,425,180,BLUE,arrow=True);s.line(770,180,825,180,BLUE,arrow=True)
    s.box(835,350,330,145,'Physical depth','Address physical page and texel\nRead light-space depth\nBias / slope / validity conventions',GOLD)
    s.box(435,350,330,145,'SMRT evaluation','Samples a finite source footprint\nMarches approximate shadow rays\nCombines visible-ray estimates',RED)
    s.box(35,350,330,145,'Direct-light contribution','Visibility × light × material response\nVSM changes the connection term\nIt does not replace the BRDF',GREEN)
    s.line(1000,250,1000,340,GOLD,arrow=True);s.line(825,420,775,420,GOLD,arrow=True);s.line(425,420,375,420,RED,arrow=True)
    s.rect(35,565,1130,150,fill='#edf3ee')
    s.text(58,605,'Before projection: maintain the virtual representation',23,GREEN,True)
    s.text(58,648,'Mark required pages → allocate or reuse → invalidate changed geometry → rasterize depth → build hierarchy',20)
    s.text(58,687,'A missing or invalid page is state to handle; it is not automatically a physically unoccluded light connection.',18,MUTED)
    return s.write('vsm-addressing.svg')

def history():
    s=SVG('A history sample is a conditional reconstruction','Current lighting is combined with previous lighting only after reprojection, per-tap validity, exposure conversion and optional clamping. History count and moving-light state determine the blend; invalid current samples use a different rule.',height=780)
    s.box(35,110,340,150,'Current frame','Lighting sample + validity\nMoving-light fraction\nCurrent normal / depth',GREEN)
    s.box(825,110,340,150,'Previous frame','Lighting + count + fast-update state\nPrevious normal / depth\nPrevious pre-exposure',BLUE)
    s.box(425,335,350,145,'Reproject and validate','Previous coordinates and 2 × 2 taps\nDepth / normal tests + weights\nReject invalid taps',BLUE)
    s.line(990,270,775,325,BLUE,arrow=True)
    s.box(825,540,340,145,'Convert, clamp, blend','Exposure ratio applies to history\nOptional neighborhood clamp\nα = 1 / (1 + N′)',RED)
    s.box(35,540,340,145,'Store the next history','Lighting + effective count\nFast-update state + validity\nQuantize to the actual format',GREEN)
    s.line(775,440,990,530,BLUE,arrow=True)
    s.line(205,270,205,505,GREEN);s.line(205,505,800,505,GREEN);s.line(800,505,835,530,GREEN,arrow=True)
    s.line(815,615,385,615,RED,arrow=True)
    s.text(425,520,'N′ includes age cap and fast-update limits.',18)
    s.text(425,718,'No valid history: N′ = 0. Invalid current sample: the source may use α = 1 / (1 + 4N′).',18)
    return s.write('history-reconstruction.svg')

def convergence():
    s=SVG('Iteration removes residual; it cannot remove model error','Computed two-patch example from equations M38–M42. One iteration uses the exact transport coefficients, the other changes one coupling from0.3 to0.25. Error relative to the exact scene approaches zero only for the exact model.',height=750)
    x0,y0,w,h=110,145,960,430
    s.text(110,100,'Maximum absolute radiance error relative to the exact two-patch scene',19)
    exact=[1/.94,.3/.94];a=[1.,0.];b=a[:];rows=[]
    for n in range(13):
        rows.append({'iteration':n,'exact_model_error':max(abs(a[i]-exact[i]) for i in range(2)),'changed_model_error':max(abs(b[i]-exact[i]) for i in range(2))})
        a=[1+.2*a[1],.3*a[0]];b=[1+.2*b[1],.25*b[0]]
    def xy(n,e):return x0+w*n/12,y0+h*(-math.log10(max(e,1e-8)))/8
    for p in range(9):
        y=y0+h*p/8;s.line(x0,y,x0+w,y,MUTED,1,dash=True);s.text(x0-18,y+6,'1' if p==0 else '1e−'+str(p),17,MUTED,anchor='end')
    for n in range(0,13,2):s.text(x0+w*n/12,y0+h+32,str(n),18,anchor='middle')
    s.text(x0+w/2,y0+h+67,'Iteration count',20,anchor='middle')
    for key,color in [('exact_model_error',GREEN),('changed_model_error',RED)]:
        points=[xy(r['iteration'],r[key]) for r in rows]
        for p,q in zip(points,points[1:]):s.line(*p,*q,color,3)
        for p in points:s.circle(*p,4,color)
    s.text(610,190,'Changed coupling: converges to the wrong solution',20,RED)
    s.text(155,490,'Exact coupling: converges to the reference solution',20,GREEN)
    s.text(110,690,'T = [[0, 0.2], [0.3, 0]]; changed model uses 0.25. Source vector b = [1, 0].',20)
    (OUT/'operator-convergence.json').write_text(json.dumps({'kind':'original computed mathematical example, not Unreal measurement','exact_solution':exact,'iterations':rows},indent=2)+'\n',encoding='utf-8')
    return s.write('operator-convergence.svg','Computed from the stated two-patch equations. Logarithmic vertical axis; this is not a performance measurement.')

def sdf_addressing():
    s=SVG('Global distance field: clipmaps and physical pages','Source-derived addressing diagram. The coarse mip is the fallback. A valid page within the mip transition band provides distance and optional coverage. Page-table z includes the clipmap index; physical page atlases use different distance and coverage resolutions.',width=1280,height=1000)
    s.box(35,100,350,135,'Translated-world query','x_t = x_w + PreViewTranslation\nClipmap center / extent and UV map\nR: voxel resolution; P = ceil(R / 7)',BLUE)
    s.box(465,100,350,135,'Coarse mip sample','u_mip → signed distance\nRetain this value if no usable page\nRange is larger than a fine page',BLUE)
    s.box(895,100,350,135,'Virtual page address','local = saturate(uv) × P\naddress = int3(local + (0, 0, iP))\nRead flags and physical atlas ID',GOLD)
    s.line(390,167,455,167,BLUE,arrow=True)
    s.line(210,240,210,270,BLUE);s.line(210,270,1070,270,BLUE);s.line(1070,270,1070,245,BLUE,arrow=True)
    s.box(35,330,350,175,'Page-use decision','page valid AND u_mip < transition\nNo: keep mip distance; coverage = 1\nYes: use page distance and\nconfigured coverage branch',GREEN)
    s.box(465,330,350,175,'Fine distance page','8³ stored texels per physical page\nuv_a = (8a + 7f + 0.5) / atlasSize\nd = (2u − 1) × 8h\nh = clipmap half-extent / R',GOLD)
    s.box(895,330,350,175,'Optional coverage page','4³ stored texels per physical page\nuv_c = (4a + 3f + 0.5) / atlasSize\nMissing / disabled coverage: C = 1\nC modifies expansion and min step',RED)
    s.line(640,240,640,300,BLUE);s.line(640,300,210,300,BLUE);s.line(210,300,210,320,BLUE,arrow=True)
    s.line(1070,280,1070,300,GOLD);s.line(1070,300,400,300,GOLD);s.line(400,300,400,388,GOLD);s.line(400,388,390,388,GOLD,arrow=True)
    s.line(390,425,455,425,GREEN,arrow=True)
    s.line(210,510,210,535,GREEN);s.line(210,535,1070,535,GREEN);s.line(1070,535,1070,510,GREEN,arrow=True)
    s.text(35,570,'Page-table z slices: source-default example R = 252, P = 36',22,GREEN,True)
    colors=[BLUE,GREEN,GOLD,RED]
    for i in range(4):
        y=600+i*58;s.rect(35,y,530,48,fill='#edf3ee',stroke=colors[i])
        s.text(52,y+31,f'Clipmap {i}:  z = {i*36} … {i*36+35}     (local z + {i*36})',20,colors[i])
    s.text(35,880,'Leaving out iP samples another clipmap.\nPacked object-grid value 0 is valid; 0xFFFFFFFF is invalid.',20)
    s.text(650,570,'One physical-page slice',22,GOLD,True)
    for iy in range(8):
        for ix in range(8):s.rect(650+ix*27,600+iy*27,26,26,fill='#f3e8d0' if ix==7 or iy==7 else '#fffaf0',stroke=RULE,rx=0)
    for iy in range(4):
        for ix in range(4):s.rect(990+ix*48,600+iy*48,47,47,fill='#f4e5df' if ix==3 or iy==3 else '#fff8f5',stroke=RULE,rx=0)
    s.text(650,850,'Distance: 8 × 8 texels\n7 unique intervals; overlap border\nUNORM8 distance step = 16h / 255',19)
    s.text(990,850,'Coverage: 4 × 4 texels\n3 unique intervals\nA separate scalar field',19)
    return s.write('sdf-addressing.svg','Source-derived schematic, not a captured atlas. i: clipmap index; a: physical page coordinate; f: fractional local page coordinate.')

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    paths=[fn() for fn in [path_space,partition,dataflow,probes,vsm,history,convergence,sdf_addressing]]
    numerical=OUT/'operator-convergence.json'
    manifest={'kind':'original authored scientific diagrams','engine_baseline':'71fe36aac5a8df5ccd66c763ffc902b29b6a9c43','not_runtime_images':True,'generator':{'path':Path(__file__).relative_to(ROOT).as_posix(),'sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},'files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in paths+[numerical]}}
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    print(f'Generated {len(paths)} SVG diagrams and a computed convergence series.')

if __name__=='__main__':main()
