/* Opt-in real-browser rendering/security check. Requires Playwright and an installed browser. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '../..');
const fixturePath = process.argv[2];
const fixture = fixturePath ? JSON.parse(fs.readFileSync(fixturePath, 'utf8')) : null;
const screenshotPath = process.argv[3];

async function main() {
  const browser = await chromium.launch({headless:true, ...(process.env.PLAYWRIGHT_CHANNEL ? {channel:process.env.PLAYWRIGHT_CHANNEL} : {})});
  try {
    const page = await browser.newPage({viewport:{width:1280,height:920}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.hostname !== 'answer.test') return route.abort();
      if (url.pathname === '/') return route.fulfill({path:path.join(root,'api/static/index.html'),contentType:'text/html'});
      if (['/static/run-events.js','/static/app.js','/static/vendor/marked.umd.js','/static/vendor/purify.min.js'].includes(url.pathname)) {
        return route.fulfill({path:path.join(root,'api',url.pathname),contentType:'application/javascript'});
      }
      if (url.pathname.endsWith('/history') && fixture) return route.fulfill({json:fixture});
      return route.fulfill({json:{files:[]}});
    });
    if (fixture) {
      await page.addInitScript(data => localStorage.setItem('multiagent.runs.v1', JSON.stringify({active:data.run.run_id,
        runs:[{runId:data.run.run_id,threadId:data.run.thread_id,query:data.run.question}]})),fixture);
    }
    await page.goto('http://answer.test/');
    if (fixture) {
      await page.waitForSelector('#answer h2');
      assert.match(await page.locator('#answer h2').innerText(),/武汉/);
      assert.equal(await page.locator('#answer table').count(),1);
      assert.equal(await page.locator('#answer del').count(),0);
      assert.match(await page.locator('#answer').innerText(),/22~23/);
      assert.equal(await page.locator('#answer tbody tr').count(),3);
      assert.ok(await page.locator('#answer strong').count() >= 3);
      assert.equal(await page.locator('#answer').evaluate(el=>getComputedStyle(el).whiteSpace),'normal');
      if (screenshotPath) await page.screenshot({path:screenshotPath,fullPage:true});
      await page.reload();
      await page.waitForSelector('#answer table');
      assert.match(await page.locator('#answer h2').innerText(),/武汉/);
    }
    await page.evaluate(() => renderAnswer(document.querySelector('#answer'),
      '## Title\n\n22~23℃, 16~17℃ and ~~removed~~\n\n**bold** and `code`\n\n- first\n- second\n\n> quote\n\n```js\nconst x = 1;\n```\n\n' +
      '[source](https://example.test/source) [bad](javascript:alert(1)) [data](data:text/html,evil) [file](file:///C:/Windows/) ' +
      '<script>window.renderAttack=1</script><img src=x onerror="window.renderAttack=1">' +
      '<svg onload="window.renderAttack=1"></svg><iframe srcdoc="evil"></iframe><form id="queryInput"></form>'));
    assert.equal(await page.locator('#answer h2').innerText(),'Title');
    assert.equal(await page.locator('#answer del').count(),1);
    assert.equal(await page.locator('#answer del').innerText(),'removed');
    assert.match(await page.locator('#answer').innerText(),/22~23℃, 16~17℃/);
    assert.equal(await page.locator('#answer ul li').count(),2);
    assert.equal(await page.locator('#answer blockquote').innerText(),'quote');
    assert.match(await page.locator('#answer pre code').innerText(),/const x = 1/);
    assert.equal(await page.locator('#answer script,#answer img,#answer svg,#answer iframe,#answer form').count(),0);
    assert.equal(await page.locator('#answer [onclick],#answer [onerror],#answer [id]').count(),0);
    const link = page.locator('#answer a[href]');
    assert.equal(await link.count(),1);
    assert.equal(await link.getAttribute('href'),'https://example.test/source');
    assert.equal(await link.getAttribute('target'),'_blank');
    assert.equal(await link.getAttribute('rel'),'noopener noreferrer');
    assert.equal(await page.evaluate(()=>window.renderAttack),undefined);
    assert.deepEqual(errors,[]);
    console.log('PASS: actual app Markdown rendering, history refresh, tables, lists, code, links and XSS sanitization');
  } finally { await browser.close(); }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
