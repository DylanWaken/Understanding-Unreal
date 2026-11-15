from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urlsplit
from collections import Counter
root=Path(r"D:\UnrealUnderstanding\Docs")
class P(HTMLParser):
    def __init__(self): super().__init__(convert_charrefs=True); self.links=[]
    def handle_starttag(self,tag,attrs):
        for k,v in attrs:
            if k in ("href","src") and v: self.links.append(v)
    handle_startendtag=handle_starttag
hosts=Counter(); examples={}
for page in sorted(root.rglob("*.html")):
    p=P(); p.feed(page.read_text(encoding="utf-8",errors="replace"))
    for u in p.links:
        s=u.strip()
        if s.startswith("//"): s="https:"+s
        try: parts=urlsplit(s)
        except Exception: continue
        if parts.scheme in ("http","https") and parts.netloc:
            h=parts.netloc.lower()
            hosts[h]+=1; examples.setdefault(h,(page.relative_to(root).as_posix(),u))
print(f"pages={len(list(root.rglob('*.html')))} external_hosts={len(hosts)}")
for h,n in hosts.most_common(30): print(f"{n:5d} {h}  e.g. {examples[h][1][:110]}")
