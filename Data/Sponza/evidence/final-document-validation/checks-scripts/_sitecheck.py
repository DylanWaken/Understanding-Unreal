from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urlsplit, unquote
root = Path(r"D:\UnrealUnderstanding\Docs").resolve()
pages = sorted(root.rglob("*.html"))
class P(HTMLParser):
    def __init__(self): super().__init__(convert_charrefs=True); self.links=[]
    def handle_starttag(self, tag, attrs):
        for k,v in attrs:
            if k in ("href","src") and v is not None: self.links.append(v)
    handle_startendtag=handle_starttag
missing=[]; checked=0; placeholder=[]
for page in pages:
    text=page.read_text(encoding="utf-8",errors="replace")
    for tok in ("ROOT_INSERT","TODO-new-shadow","PLACEHOLDER","lorem ipsum"):
        if tok.lower() in text.lower(): placeholder.append((page,tok))
    p=P(); p.feed(text)
    for url in p.links:
        s=url.strip()
        if not s or s.startswith(("#","http:","https:","mailto:","data:","javascript:","tel:","//")): continue
        parts=urlsplit(s)
        if parts.scheme or parts.netloc: continue
        path=unquote(parts.path)
        if not path: continue
        target=(root/path.lstrip("/")).resolve() if path.startswith("/") else (page.parent/path).resolve()
        checked+=1
        if not target.exists(): missing.append((page.relative_to(root).as_posix(),url,str(target)))
print(f"pages={len(pages)} local_links_checked={checked} missing={len(missing)} placeholder_hits={len(placeholder)}")
for m in missing[:40]: print("MISSING",m)
for m in placeholder[:20]: print("PLACEHOLDER",m)
