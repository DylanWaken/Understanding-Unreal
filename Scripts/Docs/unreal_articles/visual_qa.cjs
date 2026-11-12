/* Inspect transformed SVG labels and save contact sheets for human review. */
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
(async () => {
  const base = process.env.ARDA_ARTICLE_BASE || 'http://127.0.0.1:8766/';
  const root = path.resolve(__dirname, '../../../Docs/Unreal/assets');
  const out = path.join(os.tmpdir(), 'arda-unreal-articles', 'visuals');
  fs.mkdirSync(out, {recursive:true});
  const browser = await chromium.launch({headless:true, ...(process.env.ARDA_BROWSER ? {executablePath:process.env.ARDA_BROWSER} : {})});
  const page = await browser.newPage({viewport:{width:1100,height:850}});
  const errors = [];
  let count = 0;
  for (const family of fs.readdirSync(root)) {
    const folder = path.join(root, family);
    if (!fs.statSync(folder).isDirectory()) continue;
    for (const name of fs.readdirSync(folder).filter(n => n.endsWith('.svg'))) {
      const file = family + '/' + name;
      await page.goto(base + 'Docs/Unreal/assets/' + file);
      const issues = await page.evaluate(() => {
        const svg = document.documentElement;
        const frame = svg.getBoundingClientRect();
        const labels = [...svg.querySelectorAll('text')].map(t => {
          const r = t.getBoundingClientRect();
          return {text:t.textContent, x:r.x, y:r.y, right:r.right, bottom:r.bottom};
        });
        const result = [];
        for (const a of labels) {
          if (a.x < frame.x-1 || a.y < frame.y-1 || a.right > frame.right+1 || a.bottom > frame.bottom+1)
            result.push('Outside drawing: ' + a.text);
        }
        for (let i=0; i<labels.length; i++) for (let j=i+1; j<labels.length; j++) {
          const a=labels[i], b=labels[j];
          if (Math.min(a.right,b.right)-Math.max(a.x,b.x)>2 && Math.min(a.bottom,b.bottom)-Math.max(a.y,b.y)>2)
            result.push('Overlapping labels: ' + a.text + ' / ' + b.text);
        }
        return result;
      });
      if (issues.length) errors.push({file, issues});
      if (family === 'walkthrough' || family === 'compatibility' || family === 'scene')
        await page.locator('svg').screenshot({path:path.join(out, family + '-' + name + '.png')});
      count++;
    }
  }
  await browser.close();
  console.log(JSON.stringify({count, errors, screenshots:out}, null, 2));
  if (errors.length) process.exitCode = 1;
})().catch(e => {console.error(e); process.exit(1);});
