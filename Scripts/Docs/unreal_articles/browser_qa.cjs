/* Serve the repository at ARDA_ARTICLE_BASE (default http://127.0.0.1:8766/).
   NODE_PATH supplies Playwright; ARDA_BROWSER may select an installed browser. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
(async () => {
  const base = process.env.ARDA_ARTICLE_BASE || 'http://127.0.0.1:8766/';
  const pages = ['scene-representation', 'nanite', 'lumen', 'material-compiler'];
  const out = path.join(os.tmpdir(), 'arda-unreal-articles'); fs.mkdirSync(out, {recursive:true});
  const browser = await chromium.launch({headless:true, ...(process.env.ARDA_BROWSER ? {executablePath:process.env.ARDA_BROWSER} : {})});
  const context = await browser.newContext({reducedMotion:'reduce'});
  await context.route('**/*', r => r.request().url().startsWith(base) ? r.continue() : r.abort());
  const page = await context.newPage(); const exceptions = [];
  page.on('pageerror', e => exceptions.push(e.message));
  for (const slug of pages) {
    const url = base + 'Docs/Unreal/' + slug + '.html';
    for (const width of [1440, 768, 390, 640]) {
      await page.setViewportSize({width, height:1000});
      assert.equal((await page.goto(url)).status(), 200);
      await page.evaluate(() => localStorage.removeItem('arda-essay-theme'));
      assert.equal(await page.locator('h1').count(), 1);
      assert(await page.locator('.chapter').count() >= 10);
      const dimensions = await page.evaluate(() => ({width:innerWidth, full:document.documentElement.scrollWidth}));
      assert(dimensions.full <= dimensions.width + 1, `${slug} horizontal page overflow at ${width}: ${JSON.stringify(dimensions)}`);
      assert.equal(await page.locator('.reference-table[open]').count(), 0);
      if (await page.locator('.reference-table').count()) {
        const table = page.locator('.reference-table').first();
        await table.locator('summary').click();
        assert(await table.locator('table').isVisible());
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Expanded table causes page overflow');
        await table.locator('summary').click();
      }
      if (slug === 'nanite' || slug === 'lumen') {
        assert(await page.locator('.katex').count() > 30);
        assert.equal(await page.locator('.katex-error').count(), 0);
        assert(await page.locator('math annotation[encoding="application/x-tex"]').count() > 30);
      }
      await page.locator('.contents summary').click();
      const destination = page.locator('.contents a').nth(4);
      const hash = await destination.getAttribute('href');
      await destination.click();
      assert.equal(new URL(page.url()).hash, hash);
      assert.equal(await page.locator('.contents').getAttribute('open'), null);
      await page.goto(url);
      await page.screenshot({path:path.join(out, `${slug}-${width}.png`)});
    }
    await page.setViewportSize({width:1440, height:1000});
    await page.goto(url);
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.textContent), 'Skip to article');
    await page.keyboard.press('Enter');
    assert.equal(new URL(page.url()).hash, '#article');
    await page.locator('.contents summary').click();
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.contents').getAttribute('open'), null);
    assert.equal(await page.evaluate(() => document.activeElement.tagName), 'SUMMARY');
    const term = page.locator('a[data-term]').first();
    await term.focus();
    assert.equal(await term.getAttribute('aria-describedby'), 'term-definition');
    assert(await page.locator('#term-definition').isVisible());
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#term-definition').isVisible(), false);
    await page.locator('.theme-button').click();
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
    await page.screenshot({path:path.join(out, `${slug}-dark.png`)});
    await page.locator('.theme-button').click();
    const media = await page.locator('article img').evaluateAll(imgs => imgs.map(i => i.src));
    for (const src of media) {
      const response = await context.request.get(src); assert.equal(response.status(), 200, src);
    }
    const svgPage = await context.newPage();
    for (const src of media.filter(src => src.endsWith('.svg'))) {
      await svgPage.goto(src);
      const outside = await svgPage.evaluate(() => {
        const vb = document.documentElement.viewBox.baseVal;
        return [...document.querySelectorAll('text')].filter(t => {
          const b = t.getBBox(); return b.x < -1 || b.y < -1 || b.x + b.width > vb.width + 1 || b.y + b.height > vb.height + 1;
        }).map(t => t.textContent);
      });
      assert.deepEqual(outside, [], 'SVG text outside viewBox: ' + src);
    }
    await svgPage.close();
    console.log('PASS browser: ' + slug);
  }
  assert.deepEqual(exceptions, []);
  const nojs = await browser.newContext({javaScriptEnabled:false, viewport:{width:390,height:844}});
  const staticPage = await nojs.newPage();
  for (const slug of pages) {
    await staticPage.goto(base + 'Docs/Unreal/' + slug + '.html');
    assert(await staticPage.locator('.chapter').count() >= 10);
    assert.equal(await staticPage.locator('.theme-button').isVisible(), false);
    if (slug === 'nanite' || slug === 'lumen') {
      assert(await staticPage.locator('.katex').first().isVisible());
      await staticPage.locator('.equation-source summary').first().click();
      assert(await staticPage.locator('.equation-source[open] code').isVisible());
    }
    await staticPage.locator('.contents summary').click();
    await staticPage.locator('.contents a').last().click();
    assert(new URL(staticPage.url()).hash.length > 1);
  }
  await page.goto(base + 'Docs/Unreal/nanite-and-lumen.html#surface-cache');
  await page.waitForURL('**/lumen.html#surface-cache');
  await page.goto(base + 'Docs/Unreal/nanite-and-lumen.html#dag-and-cut');
  await page.waitForURL('**/nanite.html#dag-and-cut');
  await browser.close();
  console.log('PASS: all four standalone articles, non-root hosting, offline reading, narrow layouts, navigation, themes, SVG bounds, and no-JS reading. Screenshots: ' + out);
})().catch(error => { console.error(error); process.exit(1); });
