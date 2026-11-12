/* Render the JSON equation list on stdin with the pinned, vendored KaTeX. */
const fs = require('node:fs');
const katex = require('./vendor/katex/katex.cjs');
const equations = JSON.parse(fs.readFileSync(0, 'utf8'));
const rendered = {};
for (const item of equations) {
  rendered[item.key] = katex.renderToString(item.tex, {
    displayMode: item.display, output: 'htmlAndMathml', throwOnError: true,
    strict: 'error', trust: false,
  });
}
process.stdout.write(JSON.stringify({version: katex.version, equations: rendered}));
