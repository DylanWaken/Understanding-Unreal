"""Fetch the original research dataset and export the shared Unreal import mesh.

Stanford's research/noncommercial terms are recorded in provenance.json. The
downloaded archive and geometry stay ignored; published assets are study figures.
"""
from pathlib import Path
import hashlib, json, os, subprocess, tarfile, urllib.request

HERE = Path(__file__).resolve().parent
DATA = HERE / 'data'
DATA.mkdir(exist_ok=True)
URL = 'https://graphics.stanford.edu/pub/3Dscanrep/bunny.tar.gz'
EXPECTED_ARCHIVE_SHA256 = 'a5720bd96d158df403d153381b8411a727a1d73cff2f33dc9b212d6f75455b84'
ARCHIVE = DATA / 'bunny.tar.gz'

def ensure_data():
    """Fetch only when explicitly running the lab, never while importing tests."""
    if not ARCHIVE.exists():
        try:
            urllib.request.urlretrieve(URL, ARCHIVE)
        except urllib.error.URLError:
            if os.name != 'nt': raise
            # Use the Windows trust store when a standalone Python installation
            # lacks root certificates; never disable HTTPS certificate validation.
            quoted_path=str(ARCHIVE).replace("'","''")
            subprocess.run(['powershell','-NoProfile','-Command',f"Invoke-WebRequest -Uri '{URL}' -OutFile '{quoted_path}'"],check=True)
    if hashlib.sha256(ARCHIVE.read_bytes()).hexdigest() != EXPECTED_ARCHIVE_SHA256:
        raise ValueError('The downloaded Stanford archive differs from the reviewed input hash.')
    with tarfile.open(ARCHIVE, 'r:gz') as archive:
        raw = archive.extractfile('bunny/reconstruction/bun_zipper.ply').read()
    (DATA / 'bun_zipper.ply').write_bytes(raw)
    return raw

def read_ply(raw):
    lines = raw.decode('ascii').splitlines()
    nv = int(next(s for s in lines if s.startswith('element vertex ')).split()[-1])
    nf = int(next(s for s in lines if s.startswith('element face ')).split()[-1])
    start = lines.index('end_header') + 1
    vertices = [[float(x) for x in s.split()[:3]] for s in lines[start:start+nv]]
    faces = [[int(x) for x in s.split()[1:4]] for s in lines[start+nv:start+nv+nf]]
    return vertices, faces

if __name__ == '__main__':
    raw = ensure_data()
    vertices, faces = read_ply(raw)
    # Source is Y-up. Output has Z-up, ground Z=0, height=100 Unreal cm.
    mins = [min(v[k] for v in vertices) for k in range(3)]
    maxs = [max(v[k] for v in vertices) for k in range(3)]
    scale = 100 / (maxs[1]-mins[1])
    cx, cz = (mins[0]+maxs[0])/2, (mins[2]+maxs[2])/2
    out = Path.home()/'AppData/Local/Temp/arda-unreal-lab/shared/bunny.obj'
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('w', encoding='ascii', newline='\n') as f:
        f.write('# Stanford University Computer Graphics Laboratory; research visualization\n')
        for x,y,z in vertices:
            f.write(f'v {(x-cx)*scale:.9g} {- (z-cz)*scale:.9g} {(y-mins[1])*scale:.9g}\n')
        for a,b,c in faces:
            f.write(f'f {a+1} {b+1} {c+1}\n')
    manifest = {
        'source_page':'https://graphics.stanford.edu/data/3Dscanrep/',
        'download_url':URL,'archive_sha256':hashlib.sha256(ARCHIVE.read_bytes()).hexdigest(),
        'member':'bunny/reconstruction/bun_zipper.ply',
        'member_sha256':hashlib.sha256(raw).hexdigest(),
        'source_vertices':len(vertices),'source_triangles':len(faces),
        'credit':'Stanford University Computer Graphics Laboratory',
        'terms':'Research use and free redistribution permitted with acknowledgment; commercial use requires Stanford permission. This study publishes attributed rendered figures, not the source model.',
        'terms_checked_date':'2026-10-15',
        'source_bounds':{'min':mins,'max':maxs},
        'unreal_transform':'X=(source.x-center.x)*s; Y=-(source.z-center.z)*s; Z=(source.y-min.y)*s; s=100/source_height',
        'unreal_height_cm':100,'obj_sha256':hashlib.sha256(out.read_bytes()).hexdigest()
    }
    (HERE/'provenance.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'obj':str(out),'vertices':len(vertices),'triangles':len(faces),'height_cm':100}))
