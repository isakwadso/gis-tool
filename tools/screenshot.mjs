// Takes phone-sized screenshots of the live map in WebKit (the engine behind every
// iPhone browser) and Chromium. Used by .github/workflows/screenshots.yml.
//
//   node tools/screenshot.mjs <url> <out-dir>
import { chromium, devices, webkit } from 'playwright';
import { mkdirSync, writeFileSync } from 'node:fs';

const url = process.argv[2] || 'https://isakwadso.github.io/gis-tool/';
const out = process.argv[3] || 'shots';
mkdirSync(out, { recursive: true });
const log = [];
const pause = (ms) => new Promise((r) => setTimeout(r, ms));

for (const [name, engine] of Object.entries({ webkit, chromium })) {
  let browser;
  try {
    browser = await engine.launch();
    const context = await browser.newContext({ ...devices['iPhone 13'], serviceWorkers: 'block' });
    const page = await context.newPage();
    page.on('console', (m) => { if (m.type() === 'error') log.push(`${name} console: ${m.text()}`); });
    page.on('pageerror', (e) => log.push(`${name} pageerror: ${e.message}`));
    // Keep a handle on the Leaflet map so the script can move it.
    await page.addInitScript(() => {
      let realL;
      Object.defineProperty(window, 'L', {
        configurable: true,
        get() { return realL; },
        set(v) { const orig = v.map; v.map = function (...a) { const m = orig.apply(this, a); window.__map = m; return m; }; realL = v; },
      });
    });
    await page.goto(`${url}?shot=${Date.now()}`, { waitUntil: 'load' });
    await page.waitForFunction(() => !document.getElementById('status').textContent.startsWith('Loading'), null, { timeout: 30000 });
    await pause(4000);
    await page.screenshot({ path: `${out}/${name}-1-overview.png` });

    await page.evaluate(() => window.__map.setView([55.70, 13.19], 10, { animate: false }));
    await pause(4000);
    await page.screenshot({ path: `${out}/${name}-2-lund-today.png` });

    for (let i = 0; i < 3; i++) await page.click('#next');
    await pause(1500);
    await page.screenshot({ path: `${out}/${name}-3-lund-day3.png` });

    await page.evaluate(() => window.__map.setView([55.738, 13.245], 14, { animate: false }));
    await pause(5000);
    await page.screenshot({ path: `${out}/${name}-4-pastures-day3.png` });

    log.push(`${name}: ${await page.evaluate(() => document.getElementById('status').textContent)}`);
  } catch (err) {
    log.push(`${name} FAILED: ${err && err.stack ? err.stack.split('\n').slice(0, 4).join(' | ') : err}`);
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
}

writeFileSync(`${out}/log.txt`, log.join('\n') + '\n');
console.log(log.join('\n'));
