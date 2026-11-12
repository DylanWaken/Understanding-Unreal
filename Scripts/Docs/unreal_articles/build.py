"""Compose four independent, authored long-form Unreal articles.

python Scripts/Docs/unreal_articles/build.py [--check]
The chapter HTML is the prose source. This shell adds only reading navigation.
"""
from pathlib import Path
from html import escape
from html.parser import HTMLParser
import argparse
import json
import math
import re
from math_typesetting import typeset, PATTERN as MATH_PATTERN

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
OUT = ROOT / 'Docs' / 'Unreal'
ARTICLES = [
    dict(key='scene', slug='scene-representation', title='Scene Representation in Unreal Engine',
         short='Scene representation',
         deck='Follow one indoor room from shared geometry and placed components through animation, GPU records, visible surfaces, and lighting representations—with real Unreal captures.'),
    dict(key='nanite', slug='nanite', title='Nanite: From a Scanned Bunny to Visible Pixels',
         short='Nanite',
         deck='Build, select, stream, and rasterize virtual geometry: a source-guided essay with a computed Stanford bunny experiment and real Unreal Engine captures.'),
    dict(key='lumen', slug='lumen', title='Lumen: Following Light Through a Room',
         short='Lumen',
         deck='Derive and implement the machinery of real-time indirect lighting: surface caches, distance fields, probes, reflections, and temporal reuse, demonstrated in one indoor scene.'),
    dict(key='material', slug='material-compiler', title='How Unreal Compiles a Material Graph',
         short='Material graph compilation',
         deck='Follow an expression graph from the Material Editor through analysis, shader generation, caching, compilation, and the GPU programs used to shade a surface.'),
]


class Headings(HTMLParser):
    def __init__(self):
        super().__init__()
        self.chapter = None
        self.heading = None
        self.items = []
        self.words = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'section' and 'chapter' in attrs.get('class', '').split():
            self.chapter = attrs.get('id')
        if tag == 'h2':
            self.heading = []

    def handle_data(self, data):
        self.words.append(data)
        if self.heading is not None:
            self.heading.append(data)

    def handle_endtag(self, tag):
        if tag == 'h2' and self.heading is not None:
            assert self.chapter, 'Every chapter heading needs a section ID'
            self.items.append((self.chapter, ''.join(self.heading)))
            self.heading = None


class TermLinks(HTMLParser):
    """Link a small first-use vocabulary without interrupting the authored prose."""
    terms = {
        'surface normal': 'surface-normal', 'UV coordinates': 'uv-coordinates',
        'rasterization': 'rasterization', 'transform': 'transform', 'matrix': 'matrix',
        'render proxy': 'render-proxy', 'GPU Scene': 'gpu-scene',
        'vertex factory': 'vertex-factory', 'heightfield': 'heightfield',
        'skinning': 'skinning', 'GBuffer': 'gbuffer',
        'global illumination': 'global-illumination', 'Surface Cache': 'surface-cache',
        'BRDF': 'brdf', 'generational handle': 'generational-handle',
    }

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.out, self.stack, self.seen = [], [], set()
        self.pattern = re.compile(r'\b(' + '|'.join(re.escape(t) for t in self.terms) + r')\b', re.I)
        self.slugs = {k.lower(): v for k, v in self.terms.items()}

    def handle_starttag(self, tag, attrs):
        self.out.append(self.get_starttag_text())
        if tag not in ('img', 'br', 'hr', 'input', 'meta', 'link', 'wbr'): self.stack.append(tag)

    def handle_startendtag(self, tag, attrs): self.out.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        self.out.append(f'</{tag}>')
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]

    def handle_data(self, data):
        if 'p' in self.stack and not set(self.stack).intersection(('a', 'code', 'pre', 'script', 'style')):
            def term(m):
                slug = self.slugs[m[0].lower()]
                if slug in self.seen: return m[0]
                self.seen.add(slug)
                return f'<a class="term" href="../glossary.html#term-{slug}" data-term="{slug}">{m[0]}</a>'
            data = self.pattern.sub(term, data)
        self.out.append(data)

    def handle_entityref(self, name): self.out.append('&' + name + ';')
    def handle_charref(self, name): self.out.append('&#' + name + ';')
    def handle_comment(self, text): self.out.append('<!--' + text + '-->')


def render(article, body):
    equations = []
    def protect_math(match):
        equations.append(match[0])
        return f'MATHPLACEHOLDER{len(equations)-1}END'
    body = MATH_PATTERN.sub(protect_math, body)
    terms = TermLinks(); terms.feed(body); body = ''.join(terms.out)
    body = re.sub(r'MATHPLACEHOLDER(\d+)END', lambda m: equations[int(m[1])], body)
    def collapse_reference(match):
        wrapper, table, title_id, title, rows = match.groups()
        return (f'<details class="reference-table"><summary id="{title_id}">{title}</summary>'
                f'{wrapper}{table[:-1]} aria-labelledby="{title_id}">{rows}</table></div></details>')
    body = re.sub(r'(<div class="table-scroll"[^>]*>)\s*(<table class="data-table">)<caption id="([^"]+)">(.*?)</caption>(.*?)</table>\s*</div>',
                  collapse_reference, body, flags=re.S)
    body = re.sub(r'(<h2>)\s*\d+[.)]\s+', r'\1', body)
    # Wide diagrams remain readable on phones; the surrounding article never scrolls sideways.
    def diagram(match):
        tag, image = match.groups()
        alt = re.search(r'alt="([^"]*)"', image)
        label = alt.group(1) if alt else 'Technical diagram'
        return f'{tag}<div class="figure-scroll" tabindex="0" role="region" aria-label="{label}">{image}</div>'
    body = re.sub(r'(<figure[^>]*>)\s*(<img\b[^>]*src="[^"]+\.svg"[^>]*>)', diagram, body)
    body = re.sub(r'(<figure[^>]*>\s*<div class="figure-scroll"[^>]*><img\b[^>]*src="([^"]+\.svg)"[^>]*></div>\s*<figcaption>)(.*?)(</figcaption>)',
                  lambda m: m[1] + m[3] + f' <a href="{m[2]}">Open diagram at full size</a>.' + m[4], body, flags=re.S)
    body = re.sub(r'(<figure[^>]*>\s*<img\b[^>]*src="([^"]+\.(?:png|jpg|webp))"[^>]*>\s*<figcaption>)(.*?)(</figcaption>)',
                  lambda m: m[1] + m[3] + f' <a href="{m[2]}">Open image at full size</a>.' + m[4], body, flags=re.S)
    parsed = Headings()
    parsed.feed(body)
    assert parsed.items, article['key'] + ': no chapters'
    count = len(re.findall(r"\b[\w’'-]+\b", ' '.join(parsed.words)))
    body = typeset(body)
    minutes = math.ceil(count / 200)
    nav = ''.join(f'<li><a href="#{escape(id)}"><span>{i:02}</span>{escape(label)}</a></li>'
                  for i, (id, label) in enumerate(parsed.items, 1))
    siblings = ''.join(f'<a href="{a["slug"]}.html"' +
                       (' aria-current="page"' if a == article else '') +
                       f'>{escape(a["short"])}</a>' for a in ARTICLES)
    title = escape(article['title'])
    # Numbering belongs to the reading aid; the prose remains independently authored.
    chapter_number = iter(range(1, len(parsed.items) + 1))
    body = re.sub(r'<h2>', lambda _: '<h2><span class="chapter-number" aria-hidden="true">'
                  f'{next(chapter_number):02}</span>', body)
    page = f'''<!doctype html>
<!-- Generated by Scripts/Docs/unreal_articles/build.py from authored chapter fragments and figure placements. -->
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{escape(article['deck'])}">
<title>{title} · Understanding Unreal</title>
<link rel="stylesheet" href="articles.css">
<link rel="stylesheet" href="assets/math/katex.min.css">
<link rel="stylesheet" href="assets/math/math.css">
<script src="../assets/glossary-data.js" defer></script>
<script src="articles.js" defer></script>
</head><body>
<a class="skip-link" href="#article">Skip to article</a>
<header class="masthead" id="top"><a class="wordmark" href="../index.html">Understanding Unreal</a><span class="publication">Technical essays</span><a class="docs-link" href="../index.html">All essays ↗</a></header>
<nav class="essay-links" aria-label="Independent articles">{siblings}</nav>
<header class="article-heading"><p class="kicker">Rendering engineering</p><h1>{title}</h1>
<p class="deck">{escape(article['deck'])}</p>
<p class="byline">Source study · Unreal Engine 5.8.1 · <time datetime="2026-10-15">15 October 2026</time><br>{count:,} words · approximately {minutes} minutes</p>
</header>
<div class="reading-bar"><div class="reading-bar-inner">
<details class="contents"><summary>Chapters <span aria-hidden="true">↓</span></summary><nav aria-label="Article chapters"><ol>{nav}</ol></nav></details>
<span class="reading-position" aria-live="off">{escape(article['short'])}</span>
<a class="top-link" href="#top" aria-label="Back to the beginning">↑</a>
<button type="button" class="theme-button" aria-label="Switch to dark appearance" hidden>Dark</button>
</div><div class="progress" aria-hidden="true"><span></span></div></div>
<main id="article" tabindex="-1"><article class="prose">
{body}
</article></main>
<footer class="article-footer"><p>Written against the local Unreal 5.8.1 source snapshot <code>71fe36aac5a8</code>. Historical screenshots and older presentations are identified in their captions. Engine source links require access to Epic’s Unreal Engine repository.</p><p><a href="#top">Back to the beginning</a> · <a href="../glossary.html">Graphics glossary</a> · <a href="../index.html">All essays</a></p></footer>
</body></html>'''
    return '\n'.join(line.rstrip() for line in page.splitlines()) + '\n', count, len(parsed.items)


def article_source(article):
    body = (HERE / 'chapters' / (article['key'] + '.html')).read_text(encoding='utf-8')
    placements = HERE / (article['key'] + '-placements.json')
    if placements.exists():
        for item in json.loads(placements.read_text(encoding='utf-8')):
            fragment = (HERE / 'chapters' / item['source']).read_text(encoding='utf-8')
            if item.get('id'):
                matches = re.findall(r'<section\b[^>]*id="' + re.escape(item['id']) + r'"[^>]*>.*?</section>', fragment, re.S)
                assert len(matches) == 1, f'Unknown inserted chapter: {item["id"]}'
                fragment = matches[0]
            anchor = item.get('after') or item.get('before')
            if anchor:
                pattern = r'(<section\b[^>]*id="' + re.escape(anchor) + r'"[^>]*>.*?</section>)'
                assert len(re.findall(pattern, body, re.S)) == 1, f'Unknown placement: {anchor}'
                body = re.sub(pattern, lambda m: (m[0] + '\n' + fragment) if item.get('after') else (fragment + '\n' + m[0]), body, flags=re.S)
            else:
                body += '\n' + fragment
    visuals = HERE / ('walkthrough.json' if article['key'] == 'scene' else article['key'] + '-runtime.json')
    if visuals.exists():
        for item in json.loads(visuals.read_text(encoding='utf-8')):
            pattern = re.compile(r'(<section\b[^>]*id="' + re.escape(item['chapter']) + r'"[^>]*>)(.*?)(</section>)', re.S)
            matches = list(pattern.finditer(body))
            assert len(matches) == 1, f'Unknown walkthrough chapter: {item["chapter"]}'
            def insert(m):
                chapter = m[2]
                phrase = item.get('after_contains')
                if phrase:
                    paragraphs = list(re.finditer(r'<p\b[^>]*>.*?</p>', chapter, re.S))
                    target = [p for p in paragraphs if phrase in p[0]]
                    assert len(target) == 1, f'Walkthrough placement is not unique: {phrase}'
                    end = target[0].end()
                    chapter = chapter[:end] + '\n' + item['html'] + '\n' + chapter[end:]
                else:
                    chapter += '\n' + item['html'] + '\n'
                return m[1] + chapter + m[3]
            body = pattern.sub(insert, body)
    if article['key'] == 'scene':
        compatibility = HERE / 'chapters' / 'scene-compatibility.html'
        if compatibility.exists(): body += '\n' + compatibility.read_text(encoding='utf-8')
    if article['key'] in ('nanite', 'lumen'):
        evidence = HERE / 'evidence' / (article['key'] + '-lab.json')
        if evidence.exists():
            records = json.loads(evidence.read_text(encoding='utf-8'))
            if isinstance(records, dict): records = records.get('evidence', records.get('sources', []))
            body += '\n<section class="chapter" id="source-lab-notes"><h2>Source evidence and reproducibility</h2><p>The source ranges below were inspected in the local Unreal 5.8.1 checkout. Their exact hashes are recorded beside the laboratory source. These are implementation anchors for the worked examples, not a claim that the reduced reference code reproduces every engine path.</p><ol class="source-list">'
            for record in records:
                url = 'https://github.com/EpicGames/UnrealEngine/blob/71fe36aac5a8df5ccd66c763ffc902b29b6a9c43/' + record['path'] + f'#L{record["line"]}-L{record["end_line"]}'
                body += f'<li id="source-{escape(record["id"])}"><a href="{url}">{escape(record["path"])}, lines {record["line"]}–{record["end_line"]}</a><p>{escape(record.get("claim", record.get("note", "Inspected implementation range.")))}</p></li>'
            body += '</ol></section>'
    return body


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--article', choices=[a['key'] for a in ARTICLES])
    args = ap.parse_args()
    for article in ARTICLES:
        if args.article and article['key'] != args.article:
            continue
        content, count, chapters = render(article, article_source(article))
        target = OUT / (article['slug'] + '.html')
        if args.check:
            assert target.read_text(encoding='utf-8') == content, f'Stale article: {target}'
        else:
            target.write_text(content, encoding='utf-8')
        print(f'{"Verified" if args.check else "Built"} {article["slug"]}: {count:,} words, {chapters} chapters')
    if not args.article:
        target = OUT / 'nanite-and-lumen.html'
        content = migration_page()
        if args.check:
            assert target.read_text(encoding='utf-8') == content, f'Stale migration page: {target}'
        else:
            target.write_text(content, encoding='utf-8')


def migration_page():
    anchors = ['pixels-and-triangles', 'clusters-and-boundaries', 'dag-and-cut', 'streaming-residency',
               'gpu-visibility', 'visibility-rasterization', 'programmable-materials', 'shadow-views',
               'light-transport', 'tracing-representations', 'surface-cache', 'screen-probes',
               'reflections-and-hit-lighting', 'time-and-invalidation', 'moving-and-large-worlds',
               'courtyard-frame', 'diagnosing-the-picture', 'designing-the-contracts']
    marker = ''.join(f'<span id="{id}"></span>' for id in anchors)
    return '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Nanite and Lumen are now separate essays · Understanding Unreal</title>
<link rel="stylesheet" href="articles.css"><script src="legacy-routing.js" defer></script></head><body>
<header class="masthead"><a class="wordmark" href="../index.html">Understanding Unreal</a></header>
<main class="prose"><h1>Two independent essays</h1>
<p>The former combined article has been replaced by two expanded, independently readable studies.</p>
<p><a href="nanite.html">Nanite: From a Scanned Bunny to Visible Pixels</a> follows an actual mesh through building, LOD selection, rasterization, tessellation and shading.</p>
<p><a href="lumen.html">Lumen: Following Light Through a Room</a> develops surface caches, tracing, probes, reflections and temporal reuse with computed experiments and real Unreal captures.</p>
<p><a href="../index.html">Return to the Understanding Unreal homepage</a>.</p>''' + marker + '\n</main></body></html>\n'


if __name__ == '__main__':
    main()
