from pathlib import Path
import json,html,sys
p=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path(__file__).parent;x=json.loads((p/'cache-texel-replay.json').read_text());s=x['sample'];v=x['validation']
def fmt(a):return ' / '.join(f'{x:.5g}' for x in a)
svg=['<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="620" viewBox="0 0 1200 620" role="img" aria-labelledby="title desc">',f'<title id="title">Recorded Lumen cache texel arithmetic</title><desc id="desc">Actual captured operands and independently replayed stored output, pass {x["pass"]["Id"]}, atlas coordinate {s["atlasCoord"]}.</desc>','<defs><marker id="a" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8" fill="#718198"/></marker></defs>','<rect width="1200" height="620" fill="#f5f8fc"/>','<style>text{font-family:Arial,sans-serif;fill:#182c46} .small{font-size:14px;fill:#4c6079}.label{font-size:17px;font-weight:bold}.value{font-size:17px;font-family:Consolas,monospace}.arrow{stroke:#718198;stroke-width:2;fill:none;marker-end:url(#a)}</style>']
def text(xx,yy,t,cls='label'):svg.append(f'<text x="{xx}" y="{yy}" class="{cls}">{html.escape(t)}</text>')
def box(xx,yy,ww,hh,label,value,color='#ffffff'):
 svg.append(f'<rect x="{xx}" y="{yy}" width="{ww}" height="{hh}" rx="10" fill="{color}" stroke="#c7d3e3"/>');text(xx+16,yy+28,label);text(xx+16,yy+55,value,'value')
def arrow(path):svg.append(f'<path class="arrow" d="{path}"/>')
text(32,36,'A measured Lumen surface-cache computation')
text(32,61,f'Capture {Path(x["capture"]).name}  |  pass {x["pass"]["Id"]} ({x["pass"]["Pointer"]})  |  atlas texel {tuple(s["atlasCoord"])}','small')
box(32,86,1136,75,'Address, dispatch and captured RDG payload',f'Group {s["groupId"][0]}, local thread (3,4) → packed tile {s["packedTileWord"]} → page {s["cardPageIndex"]}, byte {s["pageRecordByteOffset"]} → UV ({s["atlasUV"][0]:.8f}, {s["atlasUV"][1]:.8f})','#e9f0fb')
text(32,176,f'LumenCardScene RDGUB: {x["lumenCardScene"]["status"]}, PhysicalAtlasSize {x["lumenCardScene"]["fields"]["PhysicalAtlasSize"]["value"]}','small')
box(32,191,355,78,'Decoded cache albedo ρ',fmt(s['decodedAlbedo']))
box(423,191,355,78,'Direct irradiance E_direct',fmt(s['samples']['direct']['values'][0]))
box(814,191,354,78,'Indirect irradiance E_indirect',fmt(s['samples']['indirect']['values'][0]))
arrow('M210 269 L210 305 L410 305 L410 336');arrow('M600 269 L600 336');arrow('M990 269 L990 305 L790 305 L790 336')
box(270,337,660,82,'ρ / π × (E_direct + E_indirect) + emissive',fmt(s['finalBeforeQuantization']),'#e1f3ee')
text(32,337,'Bilinear weights: '+', '.join(f'{w:g}' for w in s['samples']['albedo']['weights']),'small');text(32,361,'Emissive: '+fmt(s['samples']['emissive']['values'][0]),'small');text(952,341,'Same cache exposure','small');text(952,365,'No second exposure','small')
arrow('M600 419 L600 435 L309 435 L309 451')
box(32,452,553,80,'PCG dither and formatted store',f'u = {s["pcg16"][0]} / 65536 = {s["ditherUniform"]:.8f}')
box(615,452,553,80,'Observed R11G11B10 output',fmt(s['outputRGB']),'#e6e8fc')
arrow('M585 493 L615 493')
text(32,565,f'Packed GPU word: {s["outputPacked"]}  |  CPU truncation model: {s["predictedTruncatedPacked"]}  |  samples: {v["truncationMatches"]}/{v["examinedTexels"]}  |  selected tile pixels: {v["selectedTile"]["truncationMatches"]}/{v["selectedTile"]["examinedPixels"]}','small')
text(32,591,'Diagram of captured values, not a rendered scene. Stored formats, versions, raw records, per-pixel comparisons and hashes are in the replay JSON.','small')
svg.append('</svg>');(p/'cache-texel-flow.svg').write_text('\n'.join(svg),encoding='utf-8');print(p/'cache-texel-flow.svg')
