"""Compose four independent, authored long-form Unreal articles.

python Scripts/Docs/unreal_articles/build.py [--check]
The chapter HTML is the prose source. This shell adds only reading navigation.
"""
from pathlib import Path
from html import escape, unescape
from html.parser import HTMLParser
import argparse
import hashlib
import json
import math
import re
import struct
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit
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
    dict(key='lumen', slug='lumen', title='Lumen: From the Path Integral to a Frame',
         short='Lumen',
         date='2026-11-13', date_label='13 November 2026', append_lab_sources=False,
         deck='Derive light transport, follow its real-time approximation, and inspect the actual deferred shading, shadows, surface caches, probes, and reflections in one Unreal frame.'),
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


VOID_ELEMENTS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
                 'link', 'meta', 'param', 'source', 'track', 'wbr'}
HEADING_TAGS = {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
TABLE_WRAPPERS = {'table-scroll', 'table-wrap'}


class _Element:
    """One parsed element with source offsets, enough for a lossless splice."""
    __slots__ = ('tag', 'attrs', 'start', 'tag_end', 'end', 'parent', 'children')

    def __init__(self, tag, attrs, start, tag_end, end, parent):
        self.tag, self.attrs = tag, attrs
        self.start, self.tag_end, self.end = start, tag_end, end
        self.parent, self.children = parent, []


class TableDisclosures(HTMLParser):
    """Locate top-level tables and splice native disclosures around their wrappers.

    Table markup is kept byte-for-byte. The parser only finds element boundaries,
    so nesting inside a table or an existing details element is never rewritten.
    """
    def __init__(self, text):
        super().__init__(convert_charrefs=False)
        self.source = text
        self.line_offsets = [0]
        for line in text.splitlines(keepends=True):
            self.line_offsets.append(self.line_offsets[-1] + len(line))
        self.root = _Element('__root__', {}, 0, 0, len(text), None)
        self.stack = [self.root]
        self.tables = []
        self.headings = []

    def position(self):
        line, column = self.getpos()
        return self.line_offsets[line - 1] + column

    def add(self, tag, attrs, start, tag_end):
        node = _Element(tag, dict(attrs), start, tag_end, None, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag == 'table':
            self.tables.append(node)
        if tag in HEADING_TAGS:
            self.headings.append(node)
        return node

    def handle_starttag(self, tag, attrs):
        start = self.position()
        raw = self.get_starttag_text() or ''
        node = self.add(tag, attrs, start, start + len(raw))
        if tag in VOID_ELEMENTS:
            node.end = node.tag_end
        else:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        start = self.position()
        raw = self.get_starttag_text() or ''
        node = self.add(tag, attrs, start, start + len(raw))
        node.end = node.tag_end

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            node = self.stack[index]
            if node.tag != tag:
                continue
            close = self.source.find('>', self.position())
            node.end = close + 1 if close >= 0 else node.tag_end
            # Close any unclosed children at the same boundary rather than
            # leaving stale nodes that would confuse wrapper detection.
            for child in self.stack[index + 1:]:
                if child.end is None:
                    child.end = node.end
            del self.stack[index:]
            return


def _descendants(node):
    for child in node.children:
        yield child
        if child.tag != 'table':
            yield from _descendants(child)


def _text_of(node, source):
    """Visible text of one element, ignoring code-like subtrees."""
    if node is None:
        return ''
    class Text(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.parts, self.skip = [], 0
        def handle_starttag(self, tag, attrs):
            if tag in ('script', 'style', 'annotation'):
                self.skip += 1
        def handle_endtag(self, tag):
            if tag in ('script', 'style', 'annotation') and self.skip:
                self.skip -= 1
        def handle_data(self, data):
            if not self.skip:
                self.parts.append(data)
        def handle_entityref(self, name):
            if not self.skip:
                self.parts.append('&' + name + ';')
        def handle_charref(self, name):
            if not self.skip:
                self.parts.append('&#' + name + ';')
    parsed = Text()
    parsed.feed(source[node.start:node.end or node.tag_end])
    parsed.close()
    return re.sub(r'\s+', ' ', unescape(''.join(parsed.parts))).strip()


def _table_columns(table, source):
    heads = [child for child in _descendants(table) if child.tag == 'thead']
    cells = list(_descendants(heads[0])) if heads else []
    if not cells:
        rows = [child for child in _descendants(table) if child.tag == 'tr']
        if rows:
            cells = list(_descendants(rows[0]))
    columns = []
    for cell in cells:
        if cell.tag != 'th':
            continue
        label = _text_of(cell, source)
        if label and label not in columns:
            columns.append(label)
    return columns


def _table_summary(table, heading, source):
    captions = [child for child in table.children if child.tag == 'caption']
    if captions:
        caption = _text_of(captions[0], source)
        if caption:
            return (f'<span class="lumen-table-title">{escape(caption)}</span>',
                    'Table: ' + caption)
    heading_text = _text_of(heading, source) if heading else ''
    heading_text = re.sub(r'^\d+(?:\.\d+)*[.)]\s+', '', heading_text)
    columns = _table_columns(table, source)
    title = heading_text or 'Table'
    parts = [f'<span class="lumen-table-title">{escape(title)}</span>']
    label = title
    if columns:
        shown = columns[:5]
        text = ' · '.join(shown)
        if len(columns) > len(shown):
            text += f' · and {len(columns) - len(shown)} more'
        parts.append(f'<span class="lumen-table-columns">Columns: {escape(text)}</span>')
        label += ': ' + ', '.join(shown)
        if len(columns) > len(shown):
            label += f', and {len(columns) - len(shown)} more'
    return ''.join(parts), 'Table: ' + label


def _with_attributes(tag_text, additions):
    if not additions:
        return tag_text
    extra = ''.join(f' {name}="{escape(str(value), quote=True)}"'
                    for name, value in additions.items())
    if tag_text.endswith('/>'):
        return tag_text[:-2] + extra + '/>'
    return tag_text[:-1] + extra + '>'


def disclose_lumen_tables(body):
    """Wrap every top-level Lumen table in a closed native details/summary."""
    parser = TableDisclosures(body)
    parser.feed(body)
    parser.close()
    edits = []
    for table in parser.tables:
        if table.end is None:
            continue
        if any(ancestor.tag in ('table', 'details') for ancestor in _ancestors(table)):
            continue
        wrapper = table.parent if (table.parent.tag == 'div' and
                                   TABLE_WRAPPERS.intersection(
                                       table.parent.attrs.get('class', '').split())) else None
        outer_start = wrapper.start if wrapper else table.start
        outer_end = wrapper.end if wrapper and wrapper.end is not None else table.end
        heading = max((h for h in parser.headings
                       if h.end is not None and h.end <= table.start),
                      key=lambda h: h.start, default=None)
        summary, label = _table_summary(table, heading, body)
        if wrapper:
            additions = {}
            if 'tabindex' not in wrapper.attrs:
                additions['tabindex'] = '0'
            if 'role' not in wrapper.attrs:
                additions['role'] = 'group'
            if 'aria-label' not in wrapper.attrs:
                additions['aria-label'] = label
            start_tag = _with_attributes(body[wrapper.start:wrapper.tag_end], additions)
            replacement = f'<details class="lumen-table"><summary>{summary}</summary>{start_tag}'
            close = '</details>'
        else:
            start_tag = body[table.start:table.tag_end]
            replacement = (f'<details class="lumen-table"><summary>{summary}</summary>'
                           f'<div class="table-scroll" role="group" aria-label="{escape(label, quote=True)}" tabindex="0">'
                           f'{start_tag}')
            outer_end = table.end
            close = '</div></details>'
        edits.append((outer_start, (wrapper or table).tag_end, replacement))
        edits.append((outer_end, outer_end, close))
    # Apply from the end of the document so earlier offsets stay valid.
    for start, end, replacement in sorted(edits, key=lambda item: (item[0], item[1]), reverse=True):
        body = body[:start] + replacement + body[end:]
    return body


def _ancestors(node):
    while node.parent is not None:
        yield node.parent
        node = node.parent


BUFFER_PATH = re.compile(r'assets/lumen-v2/buffers/png/([^"\s<>]+?)\.png')


def rewrite_lumen_buffer_images(body):
    """Serve the vector buffers and let SVG viewBoxes supply fresh dimensions."""
    def vector(match):
        return 'assets/lumen-v2/buffers/svg/' + match.group(1) + '.svg'

    def image(match):
        tag = match[0]
        rewritten = BUFFER_PATH.sub(vector, tag)
        if rewritten != tag:
            # The PNG dimensions describe the old raster; image_dimensions()
            # reads the local SVG viewBox instead of preserving stale values.
            rewritten = re.sub(r'\s+(?:width|height)="[^"]*"', '', rewritten)
        return rewritten

    body = re.sub(r'<img\b[^>]*>', image, body)
    body = re.sub(r'<a\b[^>]*>', lambda match: BUFFER_PATH.sub(vector, match[0]), body)
    return body


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


def image_dimensions(match):
    """Reserve image layout from the actual local PNG/SVG dimensions."""
    tag = match[0]
    class ImageTag(HTMLParser):
        attributes = None
        def handle_starttag(self, name, attrs):
            if name == 'img': self.attributes = dict(attrs)
    parsed = ImageTag(); parsed.feed(tag)
    attrs = parsed.attributes or {}
    if attrs.get('width') and attrs.get('height'): return tag
    url = urlsplit(attrs.get('src', ''))
    if url.scheme or url.netloc or not url.path: return tag
    path = OUT / unquote(url.path)
    if not path.is_file(): return tag
    if path.suffix == '.png':
        with path.open('rb') as stream: header = stream.read(24)
        if header[:8] != b'\x89PNG\r\n\x1a\n': return tag
        width, height = struct.unpack('>II', header[16:24])
    elif path.suffix == '.svg':
        svg = ET.parse(path).getroot()
        box = [float(v) for v in re.split(r'[ ,]+', svg.get('viewBox', '').strip()) if v]
        if len(box) != 4: return tag
        width, height = box[2:]
    else:
        return tag
    if width <= 0 or height <= 0: return tag
    additions = []
    if not attrs.get('width'):
        value = float(attrs['height']) * width / height if attrs.get('height') else width
        additions.append(f'width="{value:g}"')
    if not attrs.get('height'):
        value = float(attrs['width']) * height / width if attrs.get('width') else height
        additions.append(f'height="{value:g}"')
    return re.sub(r'\s*(/?>)$', lambda m: ' ' + ' '.join(additions) + m[1], tag)


def asset_url(path):
    content = (OUT / path).read_bytes().replace(b'\r\n', b'\n')
    return path + '?v=' + hashlib.sha256(content).hexdigest()[:12]


def render(article, body):
    equations = []
    def protect_math(match):
        equations.append(match[0])
        return f'MATHPLACEHOLDER{len(equations)-1}END'
    body = MATH_PATTERN.sub(protect_math, body)
    terms = TermLinks(); terms.feed(body); body = ''.join(terms.out)
    body = re.sub(r'MATHPLACEHOLDER(\d+)END', lambda m: equations[int(m[1])], body)
    if article['key'] == 'lumen':
        # The Lumen essay owns its table disclosures and vector buffers; other
        # essays keep the reference-table transform below unchanged.
        body = rewrite_lumen_buffer_images(body)
        body = disclose_lumen_tables(body)
    else:
        def collapse_reference(match):
            wrapper, table, title_id, title, rows = match.groups()
            return (f'<details class="reference-table"><summary id="{title_id}">{title}</summary>'
                    f'{wrapper}{table[:-1]} aria-labelledby="{title_id}">{rows}</table></div></details>')
        body = re.sub(r'(<div class="table-scroll"[^>]*>)\s*(<table class="data-table">)<caption id="([^"]+)">(.*?)</caption>(.*?)</table>\s*</div>',
                      collapse_reference, body, flags=re.S)
    body = re.sub(r'(<h2>)\s*\d+[.)]\s+', r'\1', body)
    body = re.sub(r'<img\b[^>]*>', image_dimensions, body)
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
    concepts = (f'\n<link rel="stylesheet" href="{asset_url("assets/lumen-concepts.css")}">'
                if article['key'] == 'lumen' else '')
    page = f'''<!doctype html>
<!-- Generated by Scripts/Docs/unreal_articles/build.py from authored chapter fragments and figure placements. -->
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{escape(article['deck'])}">
<title>{title} · Understanding Unreal</title>
<link rel="stylesheet" href="{asset_url('articles.css')}">
<link rel="stylesheet" href="assets/math/katex.min.css">
<link rel="stylesheet" href="assets/math/math.css">{concepts}
<script src="../assets/glossary-data.js" defer></script>
<script src="{asset_url('articles.js')}" defer></script>
</head><body>
<a class="skip-link" href="#article">Skip to article</a>
<header class="masthead" id="top"><a class="wordmark" href="../index.html">Understanding Unreal</a><span class="publication">Technical essays</span><a class="docs-link" href="../index.html">All essays ↗</a></header>
<nav class="essay-links" aria-label="Independent articles">{siblings}</nav>
<header class="article-heading"><p class="kicker">Rendering engineering</p><h1>{title}</h1>
<p class="deck">{escape(article['deck'])}</p>
<p class="byline">Source study · Unreal Engine 5.8.1 · <time datetime="{article.get('date', '2026-10-15')}">{article.get('date_label', '15 October 2026')}</time><br>{count:,} words · approximately {minutes} minutes</p>
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
    if article['key'] in ('nanite', 'lumen') and article.get('append_lab_sources', True):
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
<p><a href="lumen.html">Lumen: From the Path Integral to a Frame</a> derives path tracing, then follows deferred lighting, shadows, caches, probes and reflections through the real-time approximation.</p>
<p><a href="../index.html">Return to the Understanding Unreal homepage</a>.</p>''' + marker + '\n</main></body></html>\n'


if __name__ == '__main__':
    main()
