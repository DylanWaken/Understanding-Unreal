"""Root-authored dependency map for the complete deferred/Lumen frame."""
from html import escape

BG = '#101b2d'
FG = '#eef4ff'
MUTED = '#a7b9d3'
BLUE = '#65baff'
GOLD = '#ffd36f'
GREEN = '#6ce1b5'
PURPLE = '#c4a0ff'


def frame_flow():
    shapes, labels = [], []
    def text(x, y, value, size=23, color=FG, anchor='start'):
        labels.append(f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}" text-anchor="{anchor}">{escape(value)}</text>')
    def box(key, x, y, w, h, title, lines, color=BLUE):
        h = max(h, 70 + 29 * (len(lines) - 1) + 23)
        shapes.append(f'<rect data-node="{key}" x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="#18283e" stroke="{color}" stroke-width="2"/>')
        text(x+18,y+35,title,24,color)
        for i, line in enumerate(lines):
            text(x+18,y+70+29*i,line,21)
    def path(d, color=MUTED, dash=False):
        shapes.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="3" marker-end="url(#flow-arrow)"'+(' stroke-dasharray="9 7"' if dash else '')+'/>')
    def down(x,y1,y2,color=MUTED):
        path(f'M{x} {y1}V{y2}',color)
    text(40,45,'One deferred frame: direct light, shadows, and Lumen',30)
    text(40,88,'A / B / C / H identify shared inputs; arrows show dependencies, not GPU timing.',21,MUTED)
    box('input',40,120,1120,125,'Frame inputs + retained state H',
        ['Camera, scene transforms, material programs, lights, and renderer settings',
         'H: cached lighting and allocation state; probe, diffuse, reflection, and upscaling histories'],PURPLE)

    shapes.append('<rect x="40" y="290" width="1120" height="665" rx="12" fill="none" stroke="#435874" stroke-width="2"/>')
    text(65,328,'Secondary scene lighting',27)
    box('scene',65,355,320,165,'B · Secondary scene',
        ['Mesh / global distance fields','or hardware RT geometry','Cards: depth + materials'],BLUE)
    box('card-direct',440,355,320,165,'Direct card lighting',
        ['Cull lights into card tiles','DF / HW shadow queries','Integrate direct irradiance'],GOLD)
    box('first-combine',815,355,320,165,'First lighting combine',
        ['Emission + direct + retained','indirect lighting from H','Apply card material once'],GREEN)
    path('M385 435H440',BLUE)
    path('M760 435H815',GOLD)
    path('M40 215H35V435H65',BLUE)
    path('M600 245V355',GOLD)
    path('M990 245V355',PURPLE,True)
    path('M975 520V570H225V625',GREEN)
    box('radiosity-rays',65,625,320,195,'Card radiosity probes',
        ['Lift samples through card','depth; trace directions in B','Read available cached light','Gather indirect illumination'],GREEN)
    box('radiosity-sh',440,625,320,195,'Filter and integrate',
        ['Spatial / temporal filtering','Four-coefficient SH on cards','Reconstruct new indirect','irradiance over card texels'],GREEN)
    box('second-combine',815,625,320,195,'C · Final cached light',
        ['Combine emission, direct,','and updated indirect again','Effective card albedo / pi','gives outgoing radiance'],GREEN)
    path('M385 720H440',GREEN)
    path('M760 720H815',GREEN)
    text(65,868,'Captured schedule: card direct → first combine → radiosity → second combine.',21,MUTED)
    text(65,908,'C shades secondary hits. Card shadow queries are separate from primary-view VSM projection.',20,MUTED)

    box('gbuffer',40,1010,1120,125,'A · Primary visibility and material evaluation',
        ['Visibility / Nanite / depth → base-pass material shader → GBuffer, velocity, depth and HZB',
         'The material program supplies normals, roughness, metallic / specular, base color and emission.'],BLUE)
    path('M40 183H20V1070H40',BLUE)
    path('M600 1135V1175H210V1235',BLUE)
    down(600,1135,1235,BLUE)
    path('M600 1175H990V1235',BLUE)
    text(50,1208,'Direct lights',20,GOLD)
    text(440,1208,'Diffuse GI',20,GREEN)
    text(830,1208,'Reflections',20,BLUE)

    box('vsm-pages',40,1235,340,165,'Virtual Shadow Maps',
        ['Mark demand; allocate pages','Invalidate / reuse cached pages','Rasterize dirty shadow depth','Inputs: lights, geometry, A'],GOLD)
    box('vsm-project',40,1450,340,135,'Project shadow visibility',
        ['Sample / filter VSM depth','Combine enabled shadow paths','Produce primary light masks'],GOLD)
    box('deferred',40,1635,340,165,'Deferred PBR lighting',
        ['Read the material in A','Evaluate diffuse + specular','BRDF × light × visibility','Accumulate direct SceneColor'],GOLD)
    down(210,1415,1450,GOLD)
    down(210,1601,1635,GOLD)

    box('place',430,1235,340,135,'Place screen probes',
        ['Uniform + adaptive samples','Depth / normals from A','Reuse guidance history H'],GREEN)
    box('world-cache',430,1420,340,165,'World radiance cache',
        ['Mark demand from probes','Allocate / reuse clipmap cells','Budget tracing in B, using C','Filter stored directional light'],GREEN)
    box('screen-trace',430,1635,340,165,'Trace guided probe rays',
        ['Sample BRDF / lighting PDFs','HZB → compact unresolved','SDF or HW RT in scene B','Hit C / cache / sky fallback'],GREEN)
    box('diffuse-reconstruct',430,1850,340,165,'Reconstruct diffuse',
        ['Filter radiance; SH or OCT','Integrate unit-albedo D = E / pi','Reproject / validate history H','Apply owned short-range AO'],GREEN)
    down(600,1386,1420,GREEN)
    down(600,1600,1635,GREEN)
    down(600,1815,1850,GREEN)

    box('reflection-sample',820,1235,340,135,'Classify and sample',
        ['Read roughness / normals in A','Schedule reflection tiles','Sample the glossy BSDF'],BLUE)
    box('reflection-trace',820,1420,340,165,'Trace reflection rays',
        ['HZB → compact unresolved','SDF or HW RT in scene B','Use cached C or hit lighting','Resolve miss / sky policy'],BLUE)
    box('reflection-reconstruct',820,1635,340,165,'Reconstruct reflections',
        ['Temporal / spatial filtering','Reject invalid history H','Reconstruct glossy response','Apply selected BRDF weights'],BLUE)
    down(990,1386,1420,BLUE)
    down(990,1600,1635,BLUE)

    box('translucency',40,1850,340,165,'Translucency lighting',
        ['Separate volume / cache path','Scene B, lighting and C','Direct-light volume injection','Shades transparent materials'],PURPLE)

    box('composition',430,2080,730,135,'Opaque linear SceneColor',
        ['Base-pass emission + explicit direct lighting',
         '+ diffuse color × D + material-weighted reflections'],FG)
    path('M380 1730H405V2145H430',GOLD)
    down(600,2030,2080,GREEN)
    down(990,1815,2080,BLUE)
    box('output',40,2280,1120,135,'Complete the displayed frame',
        ['Compose fog / translucency in the selected pipeline; apply AA / temporal upscaling when enabled',
         'Exposure and tone mapping → display image. Retain the applicable histories and cache state H.'],PURPLE)
    path('M210 2030V2280',PURPLE)
    path('M795 2215V2280',FG)
    path('M1160 2345H1185V183H1160',PURPLE,True)
    text(40,2460,'SCHEMATIC · the captured pass list supplies exact events; enabled features select the branches.',20,MUTED)

    return '\n'.join([
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 2500" role="img" aria-labelledby="title desc">',
        '<title id="title">Complete deferred lighting and Lumen frame dependency flow</title>',
        '<desc id="desc">Scene representations and card lighting feed Lumen diffuse and reflection paths. The primary GBuffer feeds VSM shadows, deferred PBR direct lighting, screen-probe GI and reflections. Their owned contributions combine into linear scene color, followed by translucency, post processing and retained state for later frames. The card cache is combined both before and after radiosity in the captured schedule.</desc>',
        '<defs><marker id="flow-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="context-stroke"/></marker></defs>',
        f'<rect width="1200" height="2500" rx="12" fill="{BG}"/>',
        '<g font-family="Inter, Segoe UI, Arial, sans-serif">',
        *shapes,*labels,'</g></svg>',''])
