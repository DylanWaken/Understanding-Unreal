"""Reproduce the scene article's room diagrams with Python's standard library.

Math diagrams are computed teaching examples; runtime panels embed unchanged, attributed Unreal PNGs.
No generated figure is a performance measurement. Run from any directory.
"""
from pathlib import Path
from html import escape
import base64
import json
import math

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
OUT = ROOT / 'Docs/Unreal/assets/walkthrough'
OUT.mkdir(parents=True, exist_ok=True)
FIGURES = []
ANCHORS = {
    'indexed-slab': 'A vertex record usually contains more than position.',
    'coordinate-projection': 'Now place a floor tile inside the room.',
    'ownership-and-draw-counts': 'Now count the illustrative draw submissions.',
    'thread-snapshots': 'The add path illustrates the handoff precisely.',
    'removal-fence': 'Identity has to survive these transitions.',
    'gpu-byte-address': 'The physical buffer layout is an implementation decision',
    'raster-depth-winners': 'Before giving Unreal',
    'alpha-and-volume': 'A ray marcher approximates this integral',
    'gate-attachment': 'For the indoor door, imagine an actor containing',
    'weighted-skinning': 'Skinning introduces transforms inside the object.',
    'deformation-bounds': 'Bounds must follow the same physical reality.',
    'material-response': 'Material instances reuse a parent material',
    'one-pixel-through-the-pipeline': 'Rasterization first answers',
    'courtyard-gbuffer': "Unreal's <code>FGBufferData</code>",
    'off-camera-shadow': 'Shadows add another visibility question.',
    'intersection-versus-cache': 'A ray intersection is only part of a lighting answer.',
    'point-provider-revisions': 'Now edit one scanned point.',
}
INK, MUTED, PAPER = '#263a36', '#53665f', '#fcfaf5'
GREEN, BLUE, RUST, GOLD = '#237665', '#326f96', '#a74834', '#ac7420'
VERT = ['#bd4939', '#287c6b', '#3d72b0', '#aa7925']
CH = ['a-triangle-becomes-an-object', 'the-world-is-not-one-tree', 'crossing-the-thread-boundary',
      'changes-have-lifetimes', 'the-scene-becomes-gpu-data', 'from-a-proxy-to-a-draw',
      'the-courtyard-needs-more-than-static-meshes', 'motion-is-more-than-a-matrix',
      'a-material-is-a-surface-program', 'a-visible-surface-becomes-a-lit-pixel',
      'the-camera-does-not-see-the-whole-lighting-problem', 'deriving-an-arda-scene-from-the-courtyard']


class SVG:
    def __init__(self, title, desc, height=620):
        self.height = height
        self.data = [f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 1040 {height}" role="img" aria-labelledby="title desc"><title id="title">{escape(title)}</title><desc id="desc">{escape(desc)}</desc>',
                     f'<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="{GREEN}"/></marker></defs>',
                     f'<rect width="1040" height="{height}" fill="{PAPER}"/>']
        self.text(30, 40, title, 26, weight='bold')

    def text(self, x, y, value, size=22, color=INK, anchor='start', weight='normal'):
        self.data.append(f'<text x="{x}" y="{y}" font-family="Arial, sans-serif" font-size="{size}" fill="{color}" text-anchor="{anchor}" font-weight="{weight}">{escape(str(value))}</text>')

    def lines(self, x, y, values, size=22, color=INK, anchor='start', gap=29):
        for i, value in enumerate(values): self.text(x, y+i*gap, value, size, color, anchor)

    def rect(self, x, y, width, height, fill='none', stroke=MUTED, sw=2, rx=0, dash=''):
        self.data.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"' + (f' stroke-dasharray="{dash}"' if dash else '') + '/>')

    def line(self, x1, y1, x2, y2, color=GREEN, sw=2, arrow=False, dash=''):
        self.data.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{sw}"' + (' marker-end="url(#arrow)"' if arrow else '') + (f' stroke-dasharray="{dash}"' if dash else '') + '/>')

    def path(self, path, stroke=GREEN, fill='none', sw=2, arrow=False):
        self.data.append(f'<path d="{path}" stroke="{stroke}" fill="{fill}" stroke-width="{sw}"' + (' marker-end="url(#arrow)"' if arrow else '') + '/>')

    def poly(self, points, fill, stroke=INK, sw=2):
        self.data.append('<polygon points="'+' '.join(f'{x:.2f},{y:.2f}' for x,y in points)+f'" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')

    def circle(self, x, y, r, fill=GREEN, stroke=None, sw=2):
        self.data.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{fill}" stroke="{stroke or fill}" stroke-width="{sw}"/>')

    def image(self, x, y, width, height, png):
        encoded=base64.b64encode(png).decode()
        self.data.append(f'<image x="{x}" y="{y}" width="{width}" height="{height}" href="data:image/png;base64,{encoded}"/>')

    def save(self, name, chapter, alt, caption):
        (OUT/(name+'.svg')).write_text('\n'.join(self.data)+'\n</svg>\n', encoding='utf-8')
        FIGURES.append({'chapter': CH[chapter-1], 'after_contains': ANCHORS[name], 'html': f'<figure><img src="assets/walkthrough/{name}.svg" alt="{escape(alt, quote=True)}" width="1040" height="{self.height}" loading="lazy"><figcaption>{caption}</figcaption></figure>'})
        (HERE/'walkthrough.json').write_text(json.dumps(FIGURES, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')


def slab(s,x,y,w=90,h=24,fill='#d8d5bf'):
    s.poly([(x,y),(x+w,y),(x+w+17,y-h/2),(x+17,y-h/2)], fill, MUTED, 1)
    s.poly([(x,y),(x+w,y),(x+w,y+12),(x,y+12)], '#a9ac99', MUTED, 1)
    s.poly([(x+w,y),(x+w+17,y-h/2),(x+w+17,y),(x+w,y+12)], '#bec0aa', MUTED, 1)


def indexed_slab():
    s=SVG('Room floor closeup: four vertices and two triangles', 'Colored vertex positions match colored rows in the vertex buffer. Two index triples reuse vertices zero and two. Texture coordinates give the surface a separate two-dimensional address.',620)
    pts=[(80,280),(295,350),(392,167),(180,100)]
    s.poly([pts[i] for i in [0,1,2]],'#e4ede2',GREEN)
    s.poly([pts[i] for i in [0,2,3]],'#e6dcc5',GOLD)
    for i,(x,y) in enumerate(pts):
        s.circle(x,y,9,VERT[i]);s.text(x-12,y-17,f'v{i}',24,VERT[i])
    s.text(214,296,'triangle 0',21,GREEN,'middle');s.text(235,173,'triangle 1',21,GOLD,'middle')
    s.text(52,417,'Index buffer',24,weight='bold')
    for i,indices in enumerate([[0,1,2],[0,2,3]]):
        s.text(53,461+i*50,f't{i}',23)
        for j,n in enumerate(indices):
            s.rect(100+j*76,433+i*50,65,38,'#f4f0e6',VERT[n],2)
            s.text(132+j*76,460+i*50,n,24,VERT[n],'middle')
    s.text(510,95,'Vertex buffer — teaching layout',24,weight='bold')
    s.text(514,144,'id',21);s.text(578,144,'position (x,y,z)',21);s.text(842,144,'UV (u,v)',21)
    rows=[('0','(0, 0, 0)','(0, 0)'),('1','(100, 0, 0)','(1, 0)'),('2','(100, 100, 0)','(1, 1)'),('3','(0, 100, 0)','(0, 1)')]
    for i,(a,b,c) in enumerate(rows):
        y=190+i*53;s.line(505,y+17,996,y+17,'#ccd2c7',1)
        s.circle(522,y-7,6,VERT[i]);s.text(542,y,a,22,VERT[i]);s.text(579,y,b,22);s.text(851,y,c,22)
    s.lines(510,428,['Each row also supplies a surface normal.','Here all four normals are (0, 0, 1).','The two triangles share v0 and v2.'],21)
    s.lines(31,572,['A vertex index selects a whole attribute row; a UV selects a place in a texture.'],22)
    s.save('indexed-slab',1,'A two-triangle room floor patch with four colored vertices, matching position and UV rows, and the index triples 0,1,2 and 0,2,3.','Read the colors across the geometry and the arrays: the green row is always vertex 1, whether a triangle references it once or many times. The diagonal is shared through indices 0 and 2. Position values use the chapter’s 100-centimeter square room floor patch. This is an illustrative mesh layout, not the binary layout of a particular Unreal vertex factory.')


def transforms():
    s=SVG('The room floor patch in local, world, and image space', 'A mathematical one-meter square patch of the room floor is translated to minus fifty,two hundred,zero centimeters. A teaching overhead camera projects it by dividing by positive forward depth. This extracted patch is not the room Cube mesh topology.',680)
    local=[(0,0,0),(100,0,0),(100,100,0)]
    world=[(x-50,y+200,z) for x,y,z in local]
    camera=[(x+100,y-200,400-z) for x,y,z in world]
    pixel=[(160+240*x/z,120-240*y/z) for x,y,z in camera]
    assert pixel == [(190.0,120.0),(250.0,120.0),(250.0,60.0)]
    s.lines(173,86,['Local patch','100 × 100 cm'],22,anchor='middle')
    lp=[(80+1.7*x,321-1.7*y) for x,y,z in local]
    s.line(55,321,300,321,MUTED,2,True);s.line(80,347,80,135,MUTED,2,True)
    s.poly(lp,'#e3eadd',GREEN)
    s.lines(509,86,['World floor','translation (−50,200,0)'],22,anchor='middle')
    # Room floor extents: X [-200,200], Y [0,500]. Plan view, +Y up.
    wx,wy,k=419,365,.44
    s.rect(wx,wy-500*k,400*k,500*k,'#efeee6',MUTED,1)
    wp=[(wx+(x+200)*k,wy-y*k) for x,y,z in world]
    s.poly(wp,'#e4dcc5',GOLD)
    s.text(507,397,'inside 4 m × 5 m room',18,anchor='middle')
    s.lines(853,86,['Teaching image','x = cx + f X / Z'],22,anchor='middle')
    s.rect(711,158,301,231,'#e7ece7',MUTED,1)
    pp=[(711+px*301/320,158+py*231/240) for px,py in pixel];s.poly(pp,'#d5e4ee',BLUE)
    for points in [lp,wp,pp]:
        for i,(x,y) in enumerate(points):s.circle(x,y,7,VERT[i])
    s.lines(32,441,['Track the red vertex v0:'],24)
    s.lines(50,481,['Local: (0,0,0)','World: (−50,200,0) cm'],21)
    s.lines(362,467,['Camera coordinates: (50,0,400)','positive forward depth = 400 cm','f = 240 px; center = (160,120)'],20)
    s.lines(763,481,['Image: (190,120)','240 × 50 / 400 = 30 px'],20)
    s.lines(32,582,['Overhead teaching camera at (−100,200,400) cm, looking down along −Z.','The ceiling is omitted for this mathematical closeup; this is not the Unreal capture camera.','Image Y increases downward. The same patch returns in the one-pixel worked example.'],20)
    s.save('coordinate-projection',1,'A one-meter mathematical room floor patch is translated to world minus fifty,two hundred,zero centimeters and viewed by an overhead teaching camera. The red vertex projects to 190,120 pixels.','Follow the red vertex: local (0,0,0) becomes world (−50,200,0) cm. Relative to the overhead camera at (−100,200,400), it has camera coordinates (50,0,400), so it lands 30 pixels right of image center. The green local vertex (100,0,0) becomes world (50,200,0). Every plotted point is computed. This is an extracted floor-patch calculation with the ceiling omitted, not the original Cube mesh topology, an Unreal screenshot, or Unreal’s matrix storage convention. The baseline runtime camera remains at (0,−400,165) cm.')


def actors_instances():
    s=SVG('Room tile kit: 3 primitives, 12 placements, 12 submissions', 'Two ordinary mesh components and one instanced component share one two-section mesh asset. The instanced component has ten placements. Two passes produce eight ordinary submissions plus four instanced submissions.',660)
    s.text(260,90,'Two ordinary tile components',24,anchor='middle')
    s.circle(92,164,29,'#e8dfcb',GOLD);s.text(92,172,'A',24,anchor='middle')
    s.line(122,164,209,140,arrow=True);s.line(122,164,209,231,arrow=True)
    slab(s,222,146,95);slab(s,222,237,95)
    s.text(365,149,'placement 1',20);s.text(365,240,'placement 2',20)
    s.text(758,90,'One tile ISM: ten placements',22,anchor='middle')
    for i in range(10):slab(s,570+(i%5)*80,153+(i//5)*86,57)
    s.lines(260,323,['2 render primitives','2 placements'],22,anchor='middle')
    s.lines(754,323,['1 render primitive','10 placements'],22,anchor='middle')
    s.line(30,384,1010,384,'#c6d0c5',2)
    s.text(31,429,'Same shared mesh: 2 sections; depth + base pass = 2 passes',23,weight='bold')
    s.text(40,482,'Ordinary components',22);s.text(552,482,'2 × 2 × 2 = 8 submissions',23,RUST)
    s.text(40,528,'Instanced group (10 placements)',22);s.text(552,528,'1 × 2 × 2 = 4 submissions',23,GREEN)
    s.lines(31,595,['Total: 12 submissions cover 12 × 2 × 2 = 48 placement-section-pass combinations.','Explicit example: all visible, conventional opaque path, preserve sections, no draw merging.'],20)
    s.save('ownership-and-draw-counts',6,'One room tile-kit Actor has two ordinary mesh components and one ten-instance component. Their three primitives describe twelve placements. Two sections and two passes produce eight ordinary and four instanced submissions.','In the room tile-kit thought experiment, count the visible placements first: two ordinary placements plus ten instanced placements share the mesh asset. Then apply the prose’s two-section, two-pass assumptions: 8 ordinary submissions plus 4 instanced submissions, each instanced submission processing ten placements. The total is 12 submissions for 48 placement-section-pass combinations. This optional tile kit is a schematic extension of the room; the baseline capture uses one floor Cube component. These are controlled conventional-path assumptions, not an empirical Unreal draw count.')


def thread_snapshots():
    s=SVG('The indoor door reaches three timelines at different moments', 'Game, render, and GPU lanes show the same indoor door at successively newer angles. A submitted frame retains its snapshot while later updates are prepared.',590)
    times=[286,496,706,916]
    for x,n in zip(times,['t0','t1','t2','t3']):s.text(x,97,n,22,anchor='middle');s.line(x,113,x,473,'#d4dad0',1)
    for y,name in [(169,'Game state'),(306,'Render snapshot'),(443,'GPU in flight')]:
        s.text(30,y,name,22);s.line(239,y+17,985,y+17,MUTED,2,True)
    def gate(x,y,angle,label,color):
        r=math.radians(angle);s.circle(x,y,5,color);s.line(x,y,x+57*math.cos(r),y-57*math.sin(r),color,6);s.text(x+28,y+42,label,18,color,'middle')
    for x,a in zip(times,[0,30,60,90]):gate(x-26,154,a,str(a)+'°',RUST)
    for x,a in zip(times,[0,0,30,60]):gate(x-26,291,a,str(a)+'°',GREEN)
    for x,a in zip(times,[0,0,0,30]):gate(x-26,428,a,str(a)+'°',BLUE)
    s.path('M496 195 L496 213 L671 256',GREEN,'none',2,True)
    s.path('M706 332 L706 350 L881 393',GREEN,'none',2,True)
    s.lines(31,529,['An update publishes a value; it does not mutate a buffer already being read.','This timing sketch is illustrative. Unreal frame overlap varies by configuration and workload.'],21)
    s.save('thread-snapshots',3,'Three timeline lanes show the indoor door angle advancing first on the game side, then in the render snapshot, and later in a submitted GPU frame.','Read vertically at t2: gameplay has reached 60°, the prepared render snapshot shows 30°, and the illustrated GPU work still reads 0°. The angles visualize separate snapshots of the same object, not three gameplay doors. Publishing a change preserves the data already referenced by an in-flight frame; the exact amount of frame overlap is configuration-dependent.')


def fence_lifetime():
    s=SVG('Removing the door indicator does not finish old GPU readers', 'A buffer allocation remains occupied after object removal while an older frame can still read it. A completion fence makes reclamation safe; a new generation identifies later reuse.',600)
    s.text(35,90,'CPU object',22);s.text(35,197,'Render record',22);s.text(35,308,'GPU allocation',22)
    start,end=249,988
    for y in [111,219,330]:s.line(start,y,end,y,MUTED,2,True)
    s.rect(252,78,291,40,'#e6d5c9',RUST,2);s.text(396,106,'Indicator exists',21,anchor='middle')
    s.rect(252,186,389,40,'#dce8db',GREEN,2);s.text(447,214,'Record accepts draws',21,anchor='middle')
    s.rect(252,297,560,54,'#d9e5ee',BLUE,2);s.text(527,333,'Occupied: old frame still reads this range',21,anchor='middle')
    s.line(545,69,545,360,RUST,2,False,'7 5');s.text(545,392,'Remove',21,RUST,'middle')
    s.line(643,158,643,360,GREEN,2,False,'7 5');s.lines(643,430,['No new','references'],20,GREEN,'middle')
    s.line(815,260,815,360,BLUE,3);s.lines(815,392,['Fence completes','safe to reclaim'],20,BLUE,'middle')
    s.rect(828,297,155,54,'#eee5cc',GOLD,2);s.text(905,333,'Reuse: gen 8',19,anchor='middle')
    s.rect(44,433,418,83,'#f4f0e5',MUTED,1);s.lines(62,465,['Old handle = (slot 42, generation 7)','New handle = (slot 42, generation 8)'],21)
    s.text(31,565,'A removed object and a reusable byte range are not the same lifetime event.',23)
    s.save('removal-fence',4,'A removal timeline shows CPU object removal, cessation of new render references, continued GPU buffer occupancy, a completion fence, and later slot reuse with an incremented generation.','In this schematic removal of the room door’s indicator, the blue allocation remains occupied after the red removal line because previously submitted GPU work may still read it. The fence, rather than the gameplay deletion time, permits physical reuse. Reusing slot 42 with generation 8 prevents an old logical handle for generation 7 from naming the new occupant. This is an illustrative lifetime contract, not a claim that Unreal uses this exact handle encoding.')


def gpu_addresses():
    s=SVG('Room planter instances: bytes plus an indexing rule', 'Four 64-byte planter instance records occupy a contiguous buffer. Record three has three 16-byte transform rows followed by a 16-byte tint field. Arithmetic computes the exact tint address.',650)
    s.lines(31,88,['Teaching planter instance layout: stride = 64 bytes, base address = 0x1000'],22)
    for i in range(4):
        x=50+i*242;s.rect(x,141,230,86,'#dbe7de' if i!=3 else '#e5d5bd',GREEN if i!=3 else GOLD,2)
        s.text(x+115,177,f'instance[{i}]',23,anchor='middle');s.text(x+115,210,hex(0x1000+i*64),21,anchor='middle')
    s.text(50,277,'Zoom into instance[3]',23,weight='bold')
    names=['transform row 0','transform row 1','transform row 2','tint RGBA']
    for i,n in enumerate(names):
        x=50+i*242;s.rect(x,309,230,99,['#cddfd9','#d7e3ce','#d4e1eb','#eed598'][i],MUTED,1)
        s.lines(x+115,348,[n,f'offset +{i*16} … +{i*16+15}'],20,anchor='middle')
    s.path('M891 230 L891 263 L899 300',GREEN,'none',3,True)
    s.lines(50,467,['address(tint of instance 3)','= base + instanceID × stride + fieldOffset','= 0x1000 + 3 × 64 + 48 = 0x10F0'],25,gap=39)
    s.text(31,610,'The shader and uploader must agree on stride, field offsets, alignment, and meaning.',22)
    s.save('gpu-byte-address',5,'A contiguous instance buffer is expanded into a 64-byte teaching record with three transform rows and tint; arithmetic obtains address 0x10F0 for instance 3 tint.','Add a hypothetical fourth pot to the captured three-pot group, then follow its highlighted instance record, then its yellow final field. A logical instance ID becomes an address only after applying the stride and field offset. The CPU packing code and GPU decoding code must share that contract. This 64-byte example is deliberately invented for arithmetic clarity; Unreal GPU Scene has its own versioned layouts and additional tables.')


def raster_pixels():
    s=SVG('Room crop: door and back-wall triangles compete for samples', 'Schematic projections of a room door triangle and a back-wall triangle overlap a ten-by-eight sample grid. Green and blue cells mark covered sample centers; blue wins overlap because it has a smaller depth in this explicitly forward-depth toy example.',660)
    A=[(0.6,0.8),(8.8,2.1),(3.2,7.6)];B=[(4.0,0.5),(9.3,6.6),(1.9,5.1)]
    def hit(p,tri):
        e=[]
        for i in range(3):
            a,b=tri[i],tri[(i+1)%3];e.append((b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0]))
        return all(v>=0 for v in e) or all(v<=0 for v in e)
    x0,y0,c=45,132,46
    for y in range(8):
        for x in range(10):
            a,b=hit((x+.5,y+.5),A),hit((x+.5,y+.5),B)
            col='#c8dce9' if b else '#d6e4ce' if a else '#f3f1e9'
            s.rect(x0+x*c,y0+y*c,c,c,col,'#b6c1b5',1);s.circle(x0+(x+.5)*c,y0+(y+.5)*c,2.5,MUTED)
    s.poly([(x0+x*c,y0+y*c) for x,y in A],'none',GREEN,3)
    s.poly([(x0+x*c,y0+y*c) for x,y in B],'none',BLUE,3)
    s.text(45,96,'Actual sample-center coverage',23,weight='bold')
    s.lines(576,143,['Back wall A: depth 0.60','Door B: depth 0.35','Here, smaller depth is nearer.'],23,gap=36)
    s.rect(576,289,395,122,'#f2eee3',MUTED,1)
    s.lines(595,324,['Sample (4.5, 3.5): inside A and B','0.35 < 0.60 → B wins','Shade B; A is hidden at this sample.'],21,gap=33)
    s.rect(x0+4*c,y0+3*c,c,c,'none',RUST,4)
    s.lines(32,558,['Coverage and depth are separate tests. A triangle may touch a pixel square','without containing its sample center. Unreal commonly uses reversed depth;','the printed comparison belongs only to this forward-depth illustration.'],21)
    s.save('raster-depth-winners',6,'A numerically rasterized sample grid shows two overlapping triangles, one highlighted shared sample, and the depth comparison that selects the blue triangle.','Treat the outlines as a closeup of the room door in front of the back wall. This is a schematic projection with chosen depths, not triangle coordinates extracted from the capture. The colored cells are computed from triangle edge tests at their centers; the outlines show the continuous triangles. At sample (4.5,3.5), both cover the sample, then 0.35 wins over 0.60 in this explicitly forward-depth example. The depth convention is stated because copying that inequality into Unreal’s reversed-depth paths would be wrong.')


def alpha_volume():
    s=SVG('Room glass panes and mist accumulate differently', 'Two room display-case panes apply alpha compositing in order. The room humidifier mist uses exponential transmittance along distance, with plotted attenuation and discrete segment values.',650)
    s.text(263,91,'Room display-case panes',24,anchor='middle',weight='bold')
    for x,swap in [(45,False),(265,True)]:
        s.rect(x,139,197,195,'#ffffff',MUTED,1)
        colors=['#0000ff','#ff0000'] if not swap else ['#ff0000','#0000ff']
        s.data.append(f'<rect x="{x+20}" y="158" width="124" height="124" fill="{colors[0]}" fill-opacity="0.5"/>')
        s.data.append(f'<rect x="{x+66}" y="198" width="114" height="115" fill="{colors[1]}" fill-opacity="0.5"/>')
        s.text(x+99,366,'Red in front' if not swap else 'Blue in front',20,anchor='middle')
    s.lines(41,412,['Both α = 0.5, over white (1,1,1)','Red over blue: (0.75, 0.25, 0.50)','Blue over red: (0.50, 0.25, 0.75)'],20,gap=34)
    s.text(795,91,'Room humidifier mist',24,anchor='middle',weight='bold')
    x0,y0,w,h=603,142,367,205
    s.line(x0,y0+h,x0+w,y0+h,MUTED,2,True);s.line(x0,y0+h,x0,y0,MUTED,2,True)
    pts=[(x0+w*i/100,y0+h*(1-math.exp(-2*i/100))) for i in range(101)]
    s.path('M'+' L'.join(f'{x:.2f} {y:.2f}' for x,y in pts),GREEN,'none',3)
    for d in [0,1,2,3,4]:
        x=x0+d*w/4;y=y0+h*(1-math.exp(-.5*d));s.circle(x,y,5,GREEN);s.text(x,y0+h+31,d,19,anchor='middle')
    for v in [0,0.5,1]:
        y=y0+h*(1-v);s.line(x0-5,y,x0,y,MUTED,1);s.text(x0-11,y+6,str(v),18,anchor='end')
    s.text(979,399,'distance (m)',18,anchor='end');s.text(577,123,'T',21)
    s.lines(594,445,['T(d) = exp(−σt d), σt = 0.5 /m','1 m: 0.607    2 m: 0.368    4 m: 0.135'],20,gap=34)
    s.lines(32,574,['Fog also adds in-scattered and emitted light along the ray; transmittance alone is only extinction.','Illustrative equations and colors, not Unreal’s complete translucency or volumetric pipeline.'],20)
    s.save('alpha-and-volume',7,'Red and blue half-transparent panes are composited in both orders over white, yielding different overlap colors. A computed fog transmittance curve falls from one toward zero as distance increases.','For a room display-case extension, read the overlap patches: red over blue gives (0.75,0.25,0.50), while swapping their order gives (0.50,0.25,0.75), using the shown unit-RGB compositing arithmetic over white. A pane is a surface event with an order. A humidifier’s mist instead attenuates continuously along distance: the plotted curve is T(d)=exp(−0.5d). Full volume rendering additionally integrates light scattered and emitted into the ray. Displayed colors illustrate the arithmetic; this is not a complete linear-light display-management example.')


def gate_attachment():
    s=SVG('The room door carries its attached indicator through the same turn', 'A top-down room door rotates ninety degrees around a hinge at world −150,200 centimeters. The door endpoint moves from −50,200 to −150,300. An indicator sixty centimeters from the hinge moves from −90,200 to −150,260.',620)
    ox,oy,scale=112,450,2.25
    def xy(x,y):return ox+(x+190)*scale,oy-(y-170)*scale
    for x in range(-190,-9,20):
        a,b=xy(x,170),xy(x,320);s.line(*a,*b,'#dce1d5',1)
    for y in range(180,321,20):
        a,b=xy(-190,y),xy(-10,y);s.line(*a,*b,'#dce1d5',1)
    H=xy(-150,200);E0=xy(-50,200);E1=xy(-150,300);L0=xy(-90,200);L1=xy(-150,260)
    s.line(*H,*E0,RUST,10);s.line(*H,*E1,GREEN,10);s.circle(*H,10,INK)
    s.circle(*L0,13,'#edcf83',GOLD,2);s.circle(*L1,13,'#edcf83',GOLD,2)
    s.path(f'M{H[0]+130} {H[1]} A130 130 0 0 0 {H[0]} {H[1]-130}',GREEN,'none',2,True)
    s.text(H[0]+80,H[1]-88,'90°',23,GREEN)
    s.text(E0[0],E0[1]+35,'(−50,200)',20,RUST,'middle')
    s.text(E1[0],E1[1]-22,'(−150,300)',20,GREEN,'middle')
    s.text(H[0]-20,H[1]+39,'hinge (−150,200)',20,INK,'middle')
    s.lines(593,139,['Door endpoint, local (100,0):','Rz(90°) × (100,0) = (0,100)','world = (−150,200) + (0,100)','= (−150,300) cm'],22,gap=34)
    s.lines(593,337,['Indicator offset, local (60,0):','Rz(90°) × (60,0) = (0,60)','world indicator = (−150,260) cm'],22,gap=34)
    s.lines(32,551,['Top-down attachment schematic: XY only; indicator height is omitted.','The GPU can read resolved world transforms; it need not traverse this attachment chain.'],21)
    s.save('gate-attachment',2,'A top-down room door swings from zero to ninety degrees about a hinge at −150,200 centimeters. Its indicator is marked at a sixty-centimeter local offset before and after rotation.','The brown door becomes the green door while the hinge stays fixed. Both yellow indicator positions are computed from the same parent rotation applied to the indicator’s 60-centimeter local offset. The native room setup attaches DoorPanel and AttachedIndicator as siblings beneath TeachingRoot. Both inherit the hinge transform; neither requires the other as a parent. The emissive indicator mesh has its own material and no LightComponent.')


def weighted_skinning():
    s=SVG('Room mannequin closeup: one sleeve vertex, two bones', 'The room mannequin’s schematic sleeve strip is skinned with two bone transforms. For the tracked vertex, weights are 0.75 and 0.25. The current bone results 2,0 and 2,4 produce 2,1; the previous second-bone result 2,2 produced 2,0.5.',670)
    def weight(x):return min(1,max(0,x/8))
    def deform(x,y,shift):return x,y+weight(x)*shift
    for col,(shift,label) in enumerate([(0,'Rest sleeve'),(2,'Previous pose'),(4,'Current pose')]):
        x0=48+col*339;y0=338;scale=60
        s.text(x0+128,97,label,23,anchor='middle',weight='bold')
        for i in range(4):
            points=[(i,-.25),(i+1,-.25),(i+1,.25),(i,.25)]
            screen=[(x0+scale*deform(x,y,shift)[0],y0-scale*deform(x,y,shift)[1]) for x,y in points]
            s.poly([screen[j] for j in [0,1,2]],'#d6e4dc',GREEN,1);s.poly([screen[j] for j in [0,2,3]],'#d1dfe7',BLUE,1)
        p=deform(2,0,shift);s.circle(x0+scale*p[0],y0-scale*p[1],8,RUST)
        s.text(x0+128,397,'v = (2, '+str(p[1])+')',21,RUST,'middle')
        s.line(x0,y0+28,x0+263,y0+28,MUTED,1,True)
    s.line(45,430,995,430,'#ccd5c9',2)
    s.lines(44,475,['Current: 0.75 × (2,0) + 0.25 × (2,4) = (2,1)','Previous: 0.75 × (2,0) + 0.25 × (2,2) = (2,0.5)','Local motion of tracked vertex: (0,0.5) before component and camera transforms'],22,gap=37)
    s.lines(32,614,['Original computed skinning example; translations make the weighted arithmetic visible.','Weights vary along the strip. Current and previous shape share vertex identity and connectivity.'],20)
    s.save('weighted-skinning',8,'Three computed triangulated sleeve strips show rest, previous, and current deformation. The tracked red vertex moves from 2,0 to 2,0.5 to 2,1 under the displayed two-bone weighted calculation.','This computed sleeve closeup isolates the arithmetic used by the room’s captured 68-bone <code>SK_Mannequin</code>, posed by <code>ThirdPersonWalk</code>. Its invented two-bone weights do not reproduce the asset’s stored bone influences or its captured poses at 0.10 s and 0.45 s. The red vertex is the prose’s 0.75/0.25 example. Its two current transformed positions are (2,0) and (2,4), so the result is (2,1); the previous pose instead used (2,2) for the second result, producing (2,0.5). The strip is actually computed with weights varying along its length. This is an illustrative software deformation using simple bone translations, not an Unreal skeletal-mesh capture. Its retained previous result is the information motion reconstruction needs.')


def deformation_bounds():
    s=SVG('Room mannequin closeup: a stale bound loses a moving hand', 'The room mannequin stands outside the illustrated view region, but the animated arm reaches into it. The small rest bound misses the view; an updated bound intersects the view and preserves visibility.',620)
    s.rect(30,118,968,336,'#f4f0e5',MUTED,1)
    s.rect(568,119,429,334,'#dce9df',GREEN,2);s.text(784,151,'Visible region of this view',22,GREEN,'middle')
    s.rect(237,200,186,235,'none',RUST,3,dash='8 6');s.text(250,187,'stale rest bound',21,RUST)
    s.circle(329,239,27,'#c7d9e4',BLUE,2)
    s.path('M329 268 L329 359 M329 292 L277 338 M329 294 L418 255 L642 226 M329 357 L291 426 M329 357 L364 426',BLUE,'none',17)
    s.circle(644,226,14,'#d9af83',RUST,2)
    s.rect(235,198,435,239,'none',GREEN,4);s.text(596,418,'current bound',20,GREEN,'middle')
    s.line(568,108,568,471,GREEN,3)
    s.lines(37,508,['Old bound: no overlap with the view → object incorrectly discarded.','Updated bound: overlaps the view → animated hand can be drawn.','The bound must enclose the actual pose, including material displacement.'],22,gap=33)
    s.save('deformation-bounds',8,'A schematic room mannequin outside the camera region extends a hand into it. A stale red dashed bound excludes the hand and fails the visibility test; a larger green current-pose bound intersects the region.','In this schematic closeup of the same room mannequin, the red dashed rest bound lies entirely left of the view boundary, so culling it is internally consistent but visually wrong: the hand has moved into the image. The current green bound includes the hand and survives. A larger correct bound may cost more culling work; a smaller incorrect bound loses visible geometry.')


def runtime_panel(s, name, x, y, width, height):
    """Embed unchanged captured PNG bytes: standalone SVG <img> cannot load sibling images."""
    s.image(x, y, width, height, (OUT.parent / 'unreal-lab' / (name + '.png')).read_bytes())


def material_response():
    s=SVG('Room materials: separate surface values from incoming light', 'Four unchanged Unreal viewport captures of the baseline room show base color, roughness, metallic and lit appearance at the same camera. The matte and glossy blocks have different material values. This is not a controlled roughness-only experiment.',870)
    specs=[('scene-base-color','Base color','The red and green walls store surface color.'),
           ('scene-roughness','Roughness','Bright matte block; darker glossy block.'),
           ('scene-metallic','Metallic','The glossy block also changes metal response.'),
           ('scene-lit','Lit room','These values respond to the ceiling light and GI.')]
    for i,(name,title,subtitle) in enumerate(specs):
        x=30+(i%2)*505;y=96+(i//2)*345
        s.text(x+239,y,title,24,anchor='middle',weight='bold')
        runtime_panel(s,name,x,y+14,478,269)
        s.text(x+239,y+308,subtitle,18,anchor='middle')
    s.lines(30,819,['Actual Unreal captures, unchanged camera and baseline room.','The two blocks differ in multiple parameters; this comparison does not isolate roughness alone.'],20)
    s.save('material-response',9,'Four real Unreal captures of the same indoor room separate base color, roughness and metallic from the final lit result.','Track the short matte block at screen left and the tall glossy block at screen right through all four frames. Roughness and metallic are material inputs; neither is an image of illumination. The lit frame adds direct and indirect illumination, visibility and display processing. The blocks differ in base color, roughness and metallic, so their appearance is not evidence for a roughness-only causal comparison. These panels embed unchanged images from the <a href="assets/unreal-lab/capture-manifest.json">native Unreal capture run</a>.')


def pixel_pipeline():
    s=SVG('Follow one room floor pixel from triangle to logical GBuffer', 'The room floor triangle from chapter one is projected to 190,120; 250,120; and 250,60. Pixel center 230.5,100.5 has barycentric weights 0.325,0.35,0.325. Its interpolated position and UV feed a depth comparison and material sample, producing explicit logical GBuffer values.',980)
    s.text(30,77,'One continuous numeric example; lengths in cm, image coordinates in pixels.',21)
    vertices=[(190,120),(250,120),(250,60)]
    point=(230.5,100.5)
    weights=(.325,.35,.325)
    # Every vertex has the same depth, so perspective-correct and affine weights coincide.
    assert abs(sum(weights)-1)<1e-12
    assert all(abs(sum(weights[i]*vertices[i][j] for i in range(3))-point[j])<1e-12 for j in range(2))
    loc=(100*(weights[1]+weights[2]),100*weights[2],0)
    world=(loc[0]-50,loc[1]+200,0)
    camera=(world[0]+100,world[1]-200,400)
    assert loc==(67.5,32.5,0) and world==(17.5,232.5,0) and camera==(117.5,32.5,400)
    assert (160+240*camera[0]/camera[2],120-240*camera[1]/camera[2])==point
    project=lambda p:(90+(p[0]-190)*4.4,130+(p[1]-60)*4.4)
    pts=[project(p) for p in vertices]
    s.poly(pts,'#e1eadf',GREEN,2)
    for k in range(1,6):
        yy=130+k*44;xx=90+k*44
        s.line(354-k*44,yy,354,yy,'#bccabb',1)
        s.line(xx,394-k*44,xx,394,'#bccabb',1)
    for i,(x,y) in enumerate(pts):s.circle(x,y,7,VERT[i])
    s.text(52,424,'v0 (190,120)',20,VERT[0]);s.text(304,424,'v1 (250,120)',20,VERT[1])
    s.text(286,111,'v2 (250,60)',20,VERT[2])
    px,py=project(point);s.circle(px,py,6,INK)
    s.line(px-14,py,px+14,py,INK,1);s.line(px,py-14,px,py+14,INK,1)
    s.text(119,290,'P',24,weight='bold');s.line(142,287,px-11,py-4,INK,1)
    s.text(75,464,'P = (230.5,100.5), enlarged view',20)
    s.text(529,122,'Barycentric interpolation',24,weight='bold')
    for y,i,weight,position in [(163,0,'.325','(0,0,0)'),(201,1,'.350','(100,0,0)'),(239,2,'.325','(100,100,0)')]:
        s.circle(540,y-7,6,VERT[i]);s.text(555,y,f'v{i}: {weight} × {position}',22,VERT[i])
    s.text(529,288,'Local = (67.5,32.5,0)',22,weight='bold')
    s.text(529,323,'World = (17.5,232.5,0)',22)
    s.text(529,358,'Camera = (117.5,32.5,400)',22)
    s.text(529,389,'400 is positive forward depth.',20)
    s.text(529,429,'UV = (.675,.325)',22,BLUE)
    s.text(529,462,'World normal = (0,0,1) at all vertices',20)
    s.line(30,491,1010,491,'#c5cec0',2)
    s.text(30,532,'The room floor wins the depth comparison',23,weight='bold')
    s.text(30,569,'Compare positive view depth in cm:',20)
    s.line(72,642,441,642,MUTED,2,True)
    s.line(72,642,272,642,GREEN,7)
    s.circle(72,642,9,BLUE);s.text(46,610,'Camera',19,BLUE)
    s.line(272,597,272,688,GREEN,6)
    s.line(372,597,372,688,RUST,6)
    s.text(272,721,'400 cm',21,GREEN,'middle');s.text(372,721,'600 cm',21,RUST,'middle')
    s.text(272,758,'room floor',20,GREEN,'middle');s.text(372,758,'behind it',20,RUST,'middle')
    s.lines(30,812,['400 < 600: keep the floor’s sample.','This uses view depth, not hardware Z.','Depth-test encoding can differ by renderer.'],20,gap=30)
    s.text(529,532,'Sample material, then write surface data',22,weight='bold')
    s.lines(529,573,['Texture at UV: (.4,.3,.2)','Tint: (.8,1,.8)','Multiply → (.32,.30,.16)'],21,gap=31)
    s.rect(529,675,80,80,'#99956f','none',0)
    s.lines(631,700,['Base color patch','sRGB encoded for display'],19,gap=27)
    s.text(529,799,'Logical GBuffer fields',22,weight='bold')
    s.lines(529,835,['BaseColor = (.32,.30,.16)','WorldNormal = (0,0,1); Roughness = .8','Floor patch: Metallic = 0'],20,gap=30)
    s.lines(30,936,['All triangle vertices share 400 cm depth, so perspective-correct weights equal screen weights.'],20)
    s.save('one-pixel-through-the-pipeline',10,'An enlarged projected triangle marks pixel center 230.5,100.5. Three colored barycentric weights interpolate local and world positions, UV and normal. The floor patch at 400 cm defeats a competing surface at 600 cm; its sampled and tinted material writes base color 0.32,0.30,0.16, world normal 0,0,1 and roughness 0.8.','Return to <a href="#a-triangle-becomes-an-object">the 100 cm room floor patch and teaching camera</a>: the same vertices now produce one complete surface sample. The chosen pixel is separate from the earlier small raster-grid exercise. Since all three vertices have the same camera depth, the displayed barycentric weights also give the perspective-correct attributes. Positive view depth selects the nearer sample; this does not describe a hardware depth-buffer encoding. The base-color arithmetic uses linear values, while the color swatch is encoded for display. These are illustrative logical GBuffer fields, before lighting, rather than Unreal’s packed texture layout; depth is a separate visibility result.')


def gbuffer():
    s=SVG('One indoor room, six views of the same camera-visible surfaces', 'Six actual Unreal viewport captures show base color, world normal, linear scene depth, roughness, metallic and lit color for the same baseline indoor room. The three middle diagnostic values must not be confused with illumination.',650)
    specs=[('scene-base-color','Base color','surface color without incoming lighting'),
           ('scene-world-normal','World normal','normal visualization, not a light image'),
           ('scene-depth','Linear scene depth','white = 10 m; black = near'),
           ('scene-roughness','Roughness','white = rough; dark = smooth'),
           ('scene-metallic','Metallic','white = metal; black = dielectric'),
           ('scene-lit','Lit result','material + direct and indirect light')]
    for i,(name,title,subtitle) in enumerate(specs):
        x=29+(i%3)*343;y=96+(i//3)*245
        s.text(x+154,y,title,23,anchor='middle',weight='bold')
        runtime_panel(s,name,x,y+15,309,174)
        s.text(x+154,y+218,subtitle,17,anchor='middle')
    s.lines(31,601,['Actual native Unreal captures: same room, geometry, material assignments and camera.','Depth is a custom SceneDepth(cm)/1000 display. These views do not expose physical GBuffer packing.'],19)
    s.save('courtyard-gbuffer',10,'Six real Unreal captures of the same indoor room show base color, world normal, linear scene depth, roughness, metallic and final lit appearance.','Choose the short matte block, then locate its front face in all six panels. Its normal is constant across that flat face; its base color and roughness are material properties; its depth depends on the camera. The Lit view also includes illumination and display processing, so it cannot replace any one of those inputs. These are unchanged native <a href="assets/unreal-lab/capture-manifest.json">Unreal captures</a>, not a second software-rendered scene. The depth panel uses the documented custom linear display, SceneDepth(cm)/1000, with white at 10 m. Buffer visualizations show decoded logical attributes rather than the engine’s physical texture packing.')


def shadow_view():
    s=SVG('A room block can leave a view while still shadowing its floor', 'An XZ section through the actual room at Y equals 270 centimeters uses the baseline matte block bounds, a point on the ceiling area light and a floor sample at X155. A teaching camera crops the floor, excluding the block; the light-to-floor segment still intersects the block.',710)
    def xz(x,z):return 80+(x+40)*2,500-z*1.1
    light=xz(0,298);point=xz(155,0);camera=xz(180,130)
    s.text(32,88,'Section at Y = 270 cm; X →, Z ↑',21)
    s.line(*xz(-40,0),*xz(220,0),MUTED,3)
    s.poly([camera,xz(140,0),xz(190,0)],'#e3eee4',GREEN,2)
    a=xz(30,105);s.rect(*a,190,115.5,'#d6c7a7',RUST,3)
    s.text(317,448,'Matte block',21,RUST,'middle')
    s.circle(*light,11,'#eacf78',GOLD);s.text(155,139,'Ceiling source sample',21,GOLD,'middle')
    s.circle(*camera,10,BLUE);s.text(548,334,'Crop camera',19,BLUE,'middle')
    s.line(*light,*point,GOLD,3);s.line(*camera,*point,BLUE,2,True)
    # At block top Z=105, the light segment reaches X=155*(298-105)/298.
    crossing_x=155*(298-105)/298
    assert 30 < crossing_x < 125
    s.circle(*xz(crossing_x,105),9,'none',RUST,3)
    s.circle(*point,7,RUST);s.text(480,538,'P: floor sample',20,RUST,'middle')
    s.lines(650,145,['Actual room coordinates (cm):','Source sample L = (0,270,298)','Floor point P = (155,270,0)'],20,gap=32)
    s.lines(650,272,['Matte block at this section:','X ∈ [30,125]; Z ∈ [0,105]','L → P crosses its top at','X = 155 × 193 / 298 = 100.39'],20,gap=32)
    s.lines(650,435,['The crop sees P, not the block.','The light query must retain it.'],20,gap=32)
    s.lines(32,596,['Teaching camera and section drawing; no claim that this crop was captured from Unreal.','The ceiling RectLight is extended: this line tests one source sample, not the complete soft shadow.','Main-view culling can discard draw work without deleting the primitive from the persistent scene.'],20,gap=29)
    s.save('off-camera-shadow',10,'An XZ section of the room shows a tight floor camera excluding the actual matte block while a ceiling-source ray intersects the block before reaching the visible floor point.','This schematic reuses the recorded matte block bounds and ceiling RectLight position. At Y=270 cm, the segment from the source center (0,270,298) to floor point (155,270,0) reaches the block’s top at X≈100.39 cm, inside its X range [30,125]. The teaching camera sees the floor but omits the block. Deleting the block from the persistent scene would change that light query. An extended source requires many such visibility evaluations or an appropriate shadow approximation; the single segment does not predict the full soft-shadow intensity.')


def ray_cache():
    s=SVG('A hit on the room’s back wall still needs a valid cache lookup', 'A ray hits the baseline back wall at minus one hundred,five hundred,seventy five centimeters. The wall capture spans X minus two hundred to two hundred and Z zero to three hundred. Its normalized card coordinate is one quarter,one quarter, independently of material UVs.',675)
    pts=[(63,382),(410,382),(410,121),(63,121)];s.poly(pts,'#dcd4bf',GOLD,2)
    p=(63+347/4,382-261/4);s.circle(*p,9,RUST);s.line(353,148,*p,BLUE,3,True)
    s.text(260,97,'Back wall: Y = 500 cm',22,anchor='middle')
    s.text(198,425,'P = (−100,500,75) cm',21,RUST,'middle')
    s.lines(35,478,['Intersection supplies P and surface identity.','Cache lookup is a separate operation.'],20)
    x0,y0,size=605,129,55
    for y in range(5):
        for x in range(6):
            col=['#c9bd9c','#a8b6a0','#e5d7b6','#9eb8a6'][(x+2*y)%4]
            if x==5 and y in [1,2,3]:col='#db39c8'
            s.rect(x0+x*size,y0+y*size,size,size,col,PAPER,2)
    s.rect(x0+size,y0+size,3*size,3*size,'none',GREEN,4)
    s.circle(x0+size+3*size*.25,y0+size+3*size*.25,9,RUST)
    s.text(770,99,'Schematic capture atlas',24,anchor='middle',weight='bold')
    s.lines(591,450,['u = (X + 200) / 400 = 0.25','v = Z / 300 = 0.25','Map to resident atlas page; sample lighting.'],20,gap=31)
    s.path('M415 321 L512 321 L512 225 L633 225',GREEN,'none',3,True)
    s.lines(32,587,['The wall projection is a teaching parameterization, not a measured Lumen card assignment.','Pink cells illustrate missing coverage; they do not assert missing coverage in this room capture.','Card coordinates need not equal the wall material’s authored UV coordinates.'],19,gap=27)
    s.save('intersection-versus-cache',11,'A ray hits the actual room back-wall plane at minus one hundred,five hundred,seventy five centimeters; a teaching capture projection maps it to quarter,quarter and a schematic atlas page.','The world point belongs to our room’s back wall at Y=500 cm. With a teaching capture covering X∈[−200,200] and Z∈[0,300], its coordinate is ((−100+200)/400,75/300)=(0.25,0.25). This separate parameterization retrieves cached information after intersection. The colored atlas is a schematic, not a readback of this wall’s actual Lumen cards; pink illustrates the failure mode of missing coverage. The room’s native card and atlas captures elsewhere show the real implementation data.')


def point_provider():
    s=SVG('Room scan: one point edit exposes the provider contract', 'A room scan point changes position while its color remains fixed. The raster point buffer and conservative bounds advance to revision eight, an existing reconstructed triangle product becomes stale, and no ray product exists to update. Missing normal and UV attributes require a declared material fallback.',710)
    for x,y,c in [(71,169,VERT[0]),(119,153,VERT[1]),(160,206,VERT[2]),(200,180,VERT[3]),(83,254,VERT[2]),(135,298,VERT[0]),(238,269,VERT[1]),(284,228,VERT[3])]:s.circle(x,y,9,c)
    s.circle(185,311,9,RUST);s.circle(307,325,11,RUST);s.line(197,312,292,323,RUST,3,True)
    s.rect(51,134,214,194,'none',RUST,2,dash='6 5');s.rect(45,128,281,217,'none',GREEN,3)
    s.text(156,99,'Room scan samples',24,anchor='middle',weight='bold')
    s.lines(45,395,['Point 17: x = 0.2 → 0.3 m','y = 2.2 m, z = 0.6 m; color fixed','Missing: normal, tangent, UV'],21,gap=34)
    s.text(658,100,'Selected material asks for UV + tangent',22,anchor='middle')
    s.lines(435,153,['Those attributes cannot be fetched from this record.','Declared fallback: display the supplied point color.','Do not silently claim a normal-mapped mesh exists.'],20,gap=34)
    s.line(432,255,984,255,'#c6d0c3',2)
    for i,(name,status,col) in enumerate([('Raster buffer','revision 8',GREEN),('Conservative bounds','revision 8',GREEN),('Derived triangles, if built','revision 7 → stale',RUST),('Ray representation','absent; no update invented',MUTED)]):
        y=305+i*49;s.text(430,y,name,20);s.text(736,y,status,20,col);s.line(431,y+13,995,y+13,'#d6dccf',1)
    s.lines(31,566,['GPU frame N keeps the revision-7 allocation alive until its completion fence.','New draws can publish revision 8 without rewriting memory still being read.','A color-only edit can invalidate less work if no derived product depends on color.'],21,gap=34)
    s.save('point-provider-revisions',12,'A point cloud sample moves beyond its old bound. A revision table advances the point buffer and bounds, marks optional derived triangles stale, and leaves an absent ray product absent; missing UV and tangent data force an explicit material fallback.','For a hypothetical scan of this room, point 17 moves from (0.2,2.2,0.6) to (0.3,2.2,0.6) meters. The red point moves, so both the point positions and the enclosing bound change. The right-hand entries distinguish an existing stale derived product from a representation that never existed. A material requiring UVs and a tangent basis cannot be satisfied by position-and-color records alone; a declared point-color fallback is honest and testable. The earlier lifetime rule still applies to the old allocation used by an in-flight frame.')


def room_study_map():
    s=SVG('One room is the coordinate reference for the whole essay', 'A top-down coordinate map of the four by five meter indoor room. Positive Unreal X is screen-left, positive Y points to the back wall. Solid objects are the captured baseline; dashed objects extend that same room. A tinted one-meter floor patch supports the computed geometry examples.',760)
    s.data[0]=s.data[0].replace('0 0 1040 760','0 0 1120 760')
    s.data[2]=s.data[2].replace('width="1040"','width="1120"')
    def xy(x,y):return 300-1.1*x,640-y
    def box(xlo,xhi,ylo,yhi,color,stroke=MUTED,dash=''):
        x,y=xy(xhi,yhi);s.rect(x,y,(xhi-xlo)*1.1,yhi-ylo,color,stroke,2,dash=dash)
    # Actual interior wall positions; wall thickness is schematic.
    s.rect(80,140,440,500,'#f0eee4',MUTED,2)
    s.line(80,140,80,640,RUST,8);s.line(520,140,520,640,GREEN,8);s.line(80,140,520,140,MUTED,8)
    s.text(300,111,'Back wall: Y = 500 cm',21,anchor='middle')
    s.text(80,77,'+X = screen-left',21,RUST)
    s.text(520,77,'−X = screen-right',21,GREEN,'end')
    s.text(300,674,'Open front: Y = 0',21,anchor='middle')
    box(-50,50,200,300,'#e6d29d',GOLD)
    box(30,125,230,340,'#c5b895',RUST)
    box(-120,-30,330,430,'#b9cbd0',BLUE)
    s.lines(214,346,['Matte','block'],20,anchor='middle',gap=25)
    s.lines(383,250,['Glossy','block'],20,anchor='middle',gap=25)
    s.text(300,426,'F',22,GOLD,'middle','bold')
    # Ceiling source is projected onto this plan; its height is recorded in the legend.
    light=xy(0,270);s.circle(*light,11,'#f6da78',GOLD,2);s.text(light[0]+18,light[1]+7,'L',22,GOLD)
    hinge=xy(-150,200);end0=xy(-50,200);end1=xy(-150,300)
    s.line(*hinge,*end0,RUST,4,False,'7 4');s.line(*hinge,*end1,RUST,4,False,'7 4')
    s.circle(*hinge,6,RUST);s.text(490,465,'H',21,RUST,'middle','bold')
    for x,y in [(-90,200),(-150,260)]:s.circle(*xy(x,y),6,'#eacf78',GOLD,1)
    # Native capture extensions retain the original room and seven cube transforms.
    mx,my=xy(65,110);s.circle(mx,my,22,'none',BLUE,2);s.rect(mx-28,my-31,56,62,'none',BLUE,2,dash='5 4');s.text(mx,my+8,'M',22,BLUE,'middle','bold')
    for i,x in enumerate([-145,-105,-65]):
        px,py=xy(x,80);s.circle(px,py,15,'#d8e2ce',GREEN,2);s.rect(px-19,py-19,38,38,'none',GREEN,1,dash='5 4');s.text(px,py+6,str(i),17,GREEN,'middle')
    s.text(417,606,'three pot instances',19,GREEN,'middle')
    s.line(300,704,300,686,BLUE,3,True)
    s.text(24,739,'Camera (0,−400,165) cm: off-map; arrow is not to scale',20,BLUE)
    # Fixed legend, keeping all additions explicit rather than inventing capture claims.
    s.text(620,105,'Read this plan before every closeup',24,weight='bold')
    s.line(622,141,681,141,MUTED,4);s.text(700,148,'Solid: baseline room',21)
    s.line(622,180,681,180,BLUE,3,False,'7 5');s.text(700,187,'Dashed: added teaching props',21)
    s.rect(622,207,59,25,'#e6d29d',GOLD,2);s.text(700,228,'F: computed floor patch',21)
    s.lines(620,280,['Room interior: X ∈ [−200,200] cm','Y ∈ [0,500] cm; Z ∈ [0,300] cm','L: ceiling source at (0,270,298) cm'],20,gap=30)
    s.lines(620,398,['H: hinge at (−150,200,0) cm','The two dashed segments are two door poses.','Yellow marks show its attached indicator.'],20,gap=30)
    s.lines(620,515,['M: skeletal mannequin at (65,110,0) cm','Pots 0–2: X = −145, −105, −65; Y = 80 cm','F: X ∈ [−50,50], Y ∈ [200,300], Z = 0'],20,gap=30)
    s.lines(620,647,['A coordinate schematic, not a rendered screenshot.','Solid block footprints use the captured bounds.','The floor patch is an analytic teaching surface.'],19,gap=28)
    (OUT.parent / 'scene' / 'room-study-map.svg').write_text('\n'.join(s.data)+'\n</svg>\n',encoding='utf-8')


def main():
    room_study_map()
    indexed_slab();transforms();actors_instances();thread_snapshots();fence_lifetime();gpu_addresses();raster_pixels();alpha_volume()
    gate_attachment();weighted_skinning();deformation_bounds();material_response();pixel_pipeline();gbuffer();shadow_view();ray_cache();point_provider()
    print(f'Wrote {len(FIGURES)} scene walkthrough figures.')


if __name__ == '__main__':
    main()
