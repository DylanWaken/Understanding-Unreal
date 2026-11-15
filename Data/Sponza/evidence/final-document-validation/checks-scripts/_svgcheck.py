import sys, xml.etree.ElementTree as ET
from pathlib import Path
d = Path(r"D:\UnrealUnderstanding\Docs\Unreal\assets\lumen-course")
ns = "{http://www.w3.org/2000/svg}"
files = sorted(d.glob("*.svg"))
bad=[]; total_text=0; total_tspans=0
for f in files:
    try:
        root=ET.parse(f).getroot()
    except Exception as e:
        bad.append(f"{f.name}: parse error {e}"); continue
    texts=root.findall(f".//{ns}text")
    tspans=root.findall(f".//{ns}tspan")
    title=root.find(f"{ns}title"); desc=root.find(f"{ns}desc")
    raster=root.findall(f".//{ns}image")
    content=[]
    for t in texts:
        s="".join(t.itertext()).strip()
        if s: content.append(s)
    total_text += len(texts); total_tspans += len(tspans)
    if not texts: bad.append(f"{f.name}: no <text>")
    if not content: bad.append(f"{f.name}: empty text content")
    if title is None or not "".join(title.itertext()).strip(): bad.append(f"{f.name}: missing/empty title")
    if desc is None or not "".join(desc.itertext()).strip(): bad.append(f"{f.name}: missing/empty desc")
    if not root.get("viewBox"): bad.append(f"{f.name}: missing viewBox")
    if raster: bad.append(f"{f.name}: raster <image> x{len(raster)}")
print(f"files={len(files)} text_elements={total_text} tspans={total_tspans} bad={len(bad)}")
for b in bad: print("BAD", b)
