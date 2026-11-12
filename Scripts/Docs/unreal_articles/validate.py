"""Validate the four articles, local links, original SVGs, and optional source hashes."""
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urlsplit, unquote
import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from build import ROOT, OUT, HERE, ARTICLES, Headings, article_source


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.duplicates = []
        self.links = []
        self.images = []
        self.headings = []
        self.title = False
        self.language = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if 'id' in a:
            if a['id'] in self.ids: self.duplicates.append(a['id'])
            self.ids.add(a['id'])
        if tag == 'html': self.language = a.get('lang')
        if tag == 'title': self.title = True
        if tag in ['h1', 'h2', 'h3', 'h4']: self.headings.append(int(tag[1]))
        for key in ['href', 'src']:
            if key in a: self.links.append(a[key])
        if tag == 'img': self.images.append(a)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--unreal-root', type=Path)
    ap.add_argument('--article', choices=[a['key'] for a in ARTICLES])
    args = ap.parse_args()
    parsed, errors = {}, []

    def parse(path):
        if path not in parsed:
            p = Page(); p.feed(path.read_text(encoding='utf-8-sig')); parsed[path] = p
        return parsed[path]

    for article in ARTICLES:
        if args.article and article['key'] != args.article: continue
        path = OUT / (article['slug'] + '.html')
        text = path.read_text(encoding='utf-8')
        p = parse(path)
        if p.duplicates: errors.append(f'{path}: duplicate IDs {p.duplicates}')
        if p.language != 'en' or not p.title: errors.append(f'{path}: missing document metadata')
        if p.headings.count(1) != 1: errors.append(f'{path}: expected one article title')
        for before, after in zip(p.headings, p.headings[1:]):
            if after > before + 1: errors.append(f'{path}: heading level jumps {before} to {after}')
        if re.search(r'\[\[|\bTODO\b|\bTBD\b', text): errors.append(f'{path}: unresolved authoring marker')
        if re.search(r'class="[^"]*(?:api-entry|card-grid|checkpoint|learning-objectives)', text):
            errors.append(f'{path}: directory-oriented presentation in article')
        for image in p.images:
            if not image.get('alt'): errors.append(f'{path}: image lacks meaningful alternative text')
        for url in p.links:
            parts = urlsplit(url)
            if parts.scheme or parts.netloc:
                if parts.scheme not in ('http', 'https', 'mailto', 'data'):
                    errors.append(f'{path}: unresolved or unsupported link {url}')
                continue
            if parts.path.startswith('/'): errors.append(f'{path}: root-relative URL {url}'); continue
            target = (path.parent / unquote(parts.path)).resolve() if parts.path else path
            if not target.exists(): errors.append(f'{path}: missing link {url}'); continue
            if parts.fragment and target.suffix == '.html' and unquote(parts.fragment) not in parse(target).ids:
                errors.append(f'{path}: missing fragment {url}')
        source = article_source(article)
        headings = Headings(); headings.feed(source)
        words = len(re.findall(r"\b[\w’'-]+\b", ' '.join(headings.words)))
        print(f'{article["short"]}: {words:,} words; {len(headings.items)} chapters; {len(p.images)} figures; {len(p.links)} links')

    svg_count = 0
    for path in OUT.rglob('*.svg'):
        doc = ET.parse(path).getroot(); svg_count += 1
        ns = '{http://www.w3.org/2000/svg}'
        if not doc.get('viewBox') or doc.find(ns+'title') is None or doc.find(ns+'desc') is None:
            errors.append(f'{path}: missing accessible SVG metadata')

    evidence_count = 0
    for path in (HERE / 'evidence').glob('*.json'):
        related = {'scene': {'scene', 'compatibility', 'unreal-lab'},
                   'nanite': {'nanite', 'nanite-lumen', 'nanite-lab', 'unreal-lab'},
                   'lumen': {'lumen', 'nanite-lumen', 'lumen-lab', 'unreal-lab'},
                   'material': {'material'}}
        if args.article and path.stem not in related[args.article]: continue
        records = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(records, dict): records = records.get('evidence', records.get('sources', []))
        seen = set()
        for e in records:
            evidence_count += 1
            if e['id'] in seen: errors.append(f'{path}: duplicate evidence {e["id"]}')
            seen.add(e['id'])
            if args.unreal_root:
                root = ROOT if e.get('repository') == 'arda' else args.unreal_root
                source = root / e['path']
                if not source.exists(): errors.append(f'{path}: missing source {source}'); continue
                lines = source.read_text(encoding='utf-8-sig').splitlines()
                if not 1 <= e['line'] <= e['end_line'] <= len(lines):
                    errors.append(f'{path}: invalid source range {e["id"]}'); continue
                digest = hashlib.sha256('\n'.join(lines[e['line']-1:e['end_line']]).encode()).hexdigest()
                if digest != e['sha256']: errors.append(f'{path}: changed source range {e["id"]}')
    print(f'{svg_count} accessible SVGs; {evidence_count} recorded source ranges')
    if errors:
        print('\n'.join(errors)); raise SystemExit(1)
    print('PASS: article structure, links, figures' + (', and source hashes' if args.unreal_root else ''))


if __name__ == '__main__':
    main()
