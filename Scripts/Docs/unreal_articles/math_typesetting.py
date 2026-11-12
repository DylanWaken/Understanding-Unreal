"""Pre-render authored LaTeX as static HTML/MathML; no client JS or CDN required.

Run with --node PATH when Node is not on PATH. --check verifies the frozen cache.
The regular article builder consumes that cache without needing Node installed.
"""
from pathlib import Path
from html import unescape, escape
import argparse
import hashlib
import json
import re
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
CACHE = HERE / 'math-cache.json'
PATTERN = re.compile(r'\\\[(.*?)\\\]|\\\((.*?)\\\)', re.S)


def equation(match):
    display = match[1] is not None
    tex = unescape(match[1] if display else match[2]).strip()
    key = hashlib.sha256(('display:' if display else 'inline:').encode() + tex.encode()).hexdigest()[:20]
    return dict(key=key, tex=tex, display=display)


def typeset(body):
    if not PATTERN.search(body):
        return body
    cache = json.loads(CACHE.read_text(encoding='utf-8'))['equations']
    def replace(match):
        item = equation(match)
        assert item['key'] in cache, 'Run math_typesetting.py for new or changed LaTeX: ' + item['tex'][:100]
        output = cache[item['key']]
        if not item['display']:
            return '<span class="inline-math">' + output + '</span>'
        return ('<div class="equation-block"><div class="equation-scroll" tabindex="0" role="region" aria-label="Mathematical derivation">'
                + output + '</div><details class="equation-source"><summary>LaTeX source</summary><pre><code>'
                + escape(item['tex']) + '</code></pre></details></div>')
    return PATTERN.sub(replace, body)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--node', default=shutil.which('node'))
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()
    assert args.node, 'Pass --node with the Node.js executable path'
    equations = {}
    for source in sorted((HERE/'chapters').glob('*.html')):
        for match in PATTERN.finditer(source.read_text(encoding='utf-8')):
            item = equation(match); equations[item['key']] = item
    result = subprocess.run([args.node, str(HERE/'render_math.cjs')], input=json.dumps(list(equations.values())),
                            text=True, encoding='utf-8', stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    content = json.dumps(json.loads(result.stdout), ensure_ascii=False, indent=2) + '\n'
    if args.check:
        assert CACHE.read_text(encoding='utf-8') == content, 'Stale math cache'
    else:
        CACHE.write_text(content, encoding='utf-8')
    print(f'{"Verified" if args.check else "Typeset"} {len(equations)} LaTeX expressions')


if __name__ == '__main__':
    main()
