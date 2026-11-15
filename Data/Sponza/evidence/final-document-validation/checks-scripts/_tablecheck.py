import sys
from html.parser import HTMLParser
p = r"D:\UnrealUnderstanding\Docs\Unreal\lumen.html"
html = open(p, encoding="utf-8").read()
VOID = {"area","base","br","col","embed","hr","img","input","link","meta","param","source","track","wbr"}
class P(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack=[]; self.tables=[]; self.ids={}; self.details_bad=[]; self.in_summary=0
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag=="table":
            anc=[t for t,_,_ in self.stack]
            self.tables.append((a.get("id"), "details" in anc, self.getpos()))
        if "id" in a:
            self.ids.setdefault(a["id"], []).append(self.getpos())
        if tag=="details":
            pass
        if tag not in VOID:
            self.stack.append((tag, a, self.getpos()))
    def handle_startendtag(self, tag, attrs):
        a=dict(attrs)
        if "id" in a: self.ids.setdefault(a["id"], []).append(self.getpos())
    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0]==tag:
                del self.stack[i:]
                return
        self.details_bad.append((tag,self.getpos()))
parser=P(); parser.feed(html)
inside=[t for t in parser.tables if t[1]]
outside=[t for t in parser.tables if not t[1]]
print(f"tables={len(parser.tables)} inside_details={len(inside)} outside_details={len(outside)}")
for t in outside[:10]: print("OUTSIDE", t)
dupes={k:v for k,v in parser.ids.items() if len(v)>1}
print(f"ids_total={len(parser.ids)} duplicate_ids={len(dupes)}")
for k,v in list(dupes.items())[:20]: print("DUP", k, v)
for a in ["direct-lighting","reflections","shadow-views","offscreen-shadows","vsm-storage"]:
    print(a, len(parser.ids.get(a,[])))
