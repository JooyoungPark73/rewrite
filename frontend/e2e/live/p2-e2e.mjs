// Drives the real backend (serving frontend/dist) through phase-2 manual checks 1-2:
// forward SyncTeX (cursor line -> highlight box on the PDF) and reverse SyncTeX
// (double-click on the PDF -> editor at that line).
//
//   node e2e/live/p2-e2e.mjs <repo> <screenshot dir>
//
// Assumes `rewrite serve --repo <repo> --port 8765` is running with a fresh
// `npm run build` in frontend/dist, and that the sample paper has been compiled.
// Prints one PASS/FAIL line per check and exits non-zero if any check failed.
import { chromium } from '@playwright/test';
import fs from 'node:fs';
const REPO = process.argv[2], OUT = process.argv[3], BASE = 'http://127.0.0.1:8765';
const FILE = 'sections/intro.tex';
const NEEDLE = 'Snapshot restore is the main alternative';
if (!REPO || !OUT) { console.log('usage: node e2e/live/p2-e2e.mjs <repo> <screenshot dir>'); process.exit(2); }

const results = [];
function check(name, ok, info = '') {
  results.push({ name, ok });
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${info ? ' | ' + info : ''}`);
  return ok;
}
async function finish(browser) {
  const failed = results.filter(r => !r.ok);
  console.log('JS ERRORS:', errors.length ? errors : 'none');
  console.log(failed.length ? `RESULT: FAIL (${failed.length} of ${results.length} checks failed)` : `RESULT: PASS (${results.length} checks)`);
  await browser.close();
  process.exit(failed.length ? 1 : 0);
}

// The line we expect, read from disk (earlier live runs may have edited the file).
const source = fs.readFileSync(`${REPO}/${FILE}`, 'utf8').split('\n');
const expectedLine = source.findIndex(l => l.includes(NEEDLE)) + 1;
console.log(`TARGET: ${FILE}:${expectedLine} "${NEEDLE}"`);

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const errors = [];
page.on('pageerror', e => errors.push('pageerror: ' + e.message));
// 404s are expected for SyncTeX "no match"; anything else is reported.
page.on('console', m => { if (m.type() === 'error' && !/404 \(Not Found\)/.test(m.text())) errors.push('console: ' + m.text()); });
// Log every SyncTeX call with its answer, to compare against the backend's convention.
const syncCalls = [];
page.on('response', async r => {
  if (!r.url().includes('/api/synctex/')) return;
  const body = await r.text().catch(() => '?');
  const u = new URL(r.url());
  syncCalls.push({ path: u.pathname, query: Object.fromEntries(u.searchParams), status: r.status(), body });
  console.log('SYNCTEX', r.status(), u.pathname + u.search, '->', body.slice(0, 300));
});

if (!check('target line found in source', expectedLine > 0, `${FILE}:${expectedLine}`)) await finish(browser);

await page.goto(BASE);
await page.locator('[data-testid=file-tree] [data-path="main.tex"]').waitFor({ timeout: 10000 });

// A PDF must be on screen; compile once if the backend has none yet.
const pageCount = page.getByTestId('pdf-page-count');
let havePdf = await pageCount.filter({ hasText: /\d+ pages?/ }).waitFor({ timeout: 10000 }).then(() => true, () => false);
if (!havePdf) {
  console.log('No PDF shown; compiling...');
  await page.getByRole('button', { name: 'Compile' }).click();
  havePdf = await pageCount.filter({ hasText: /\d+ pages?/ }).waitFor({ timeout: 90000 }).then(() => true, () => false);
}
if (!check('PDF is shown', havePdf, await pageCount.innerText().catch(() => '?'))) await finish(browser);

// ---- Manual check 1: forward sync -----------------------------------------
await page.locator(`[data-testid=file-tree] [data-path="${FILE}"]`).click();
await page.getByTestId('open-file-name').filter({ hasText: FILE }).waitFor({ timeout: 5000 });
const targetLine = page.locator('.cm-content .cm-line').filter({ hasText: NEEDLE }).first();
await targetLine.waitFor({ timeout: 5000 });
await targetLine.click({ position: { x: 4, y: 4 } }); // start of the line
await page.waitForTimeout(300);
const cursorAt = await page.locator('.cm-activeLineGutter').first().innerText().catch(() => '?');
check('cursor placed on the target line', Number(cursorAt) === expectedLine, `gutter says ${cursorAt}`);

// The store syncs 300 ms after the cursor rests; allow for a slow synctex call.
const boxes = page.getByTestId('sync-box');
let gotBox = await boxes.first().waitFor({ timeout: 5000 }).then(() => true, () => false);
const status = await page.getByTestId('sync-status').innerText().catch(() => '(none)');
check('automatic forward sync drew a highlight box', gotBox, `sync status: ${status}`);
if (!gotBox) {
  console.log('Retrying with the explicit "Sync" button...');
  await page.getByTestId('sync-forward').click();
  gotBox = await boxes.first().waitFor({ timeout: 5000 }).then(() => true, () => false);
  check('explicit forward sync drew a highlight box', gotBox, `sync status: ${await page.getByTestId('sync-status').innerText().catch(() => '(none)')}`);
  if (!gotBox) { await page.screenshot({ path: OUT + '/p2-forward-fail.png' }); await finish(browser); }
}
await page.waitForTimeout(500); // let the scroll and the fade-in settle

const boxCount = await boxes.count();
const first = boxes.first();
const pdfPage = Number(await first.getAttribute('data-sync-page'));
const pt = Object.fromEntries(await Promise.all(['x', 'y', 'w', 'h'].map(async k => [k, Number(await first.getAttribute('data-' + k))])));
const boxCss = await first.boundingBox();
const pageCss = await page.locator(`[data-page="${pdfPage}"]`).boundingBox();
const scale = boxCss.width / pt.w;
console.log(`BOXES: ${boxCount}; first on page ${pdfPage}`);
console.log(`  PDF points (top-left origin): x=${pt.x} y=${pt.y} w=${pt.w} h=${pt.h}`);
console.log(`  CSS px relative to the page: left=${(boxCss.x - pageCss.x).toFixed(1)} top=${(boxCss.y - pageCss.y).toFixed(1)} w=${boxCss.width.toFixed(1)} h=${boxCss.height.toFixed(1)} (scale ${scale.toFixed(3)} px/pt)`);
console.log(`  page box: ${pageCss.width.toFixed(1)} x ${pageCss.height.toFixed(1)} px = ${(pageCss.width / scale).toFixed(1)} x ${(pageCss.height / scale).toFixed(1)} pt`);
check('box lies inside its page', pt.x >= 0 && pt.y >= 0 && pt.x + pt.w <= pageCss.width / scale + 1 && pt.y + pt.h <= pageCss.height / scale + 1);
const pane = await page.getByTestId('pdf-pages').boundingBox();
const inView = boxCss.y >= pane.y - 1 && boxCss.y + boxCss.height <= pane.y + pane.height + 1;
check('PDF pane scrolled so the box is visible', inView, `box y ${boxCss.y.toFixed(0)}..${(boxCss.y + boxCss.height).toFixed(0)}, pane ${pane.y.toFixed(0)}..${(pane.y + pane.height).toFixed(0)}`);
await page.screenshot({ path: OUT + '/p2-forward.png' });

// ---- Manual check 2: reverse sync ------------------------------------------
// Switch to main.tex first so the double-click has to open intro.tex again.
await page.locator('[data-testid=file-tree] [data-path="main.tex"]').click();
await page.getByTestId('open-file-name').filter({ hasText: /^main\.tex$/ }).waitFor({ timeout: 5000 });
const target = (await first.boundingBox().catch(() => null)) ?? boxCss; // the box stays until the next sync
const cx = target.x + target.width / 2, cy = target.y + target.height / 2;
const expectPt = { x: pt.x + pt.w / 2, y: pt.y + pt.h / 2 };
const callsBefore = syncCalls.length;
await page.mouse.dblclick(cx, cy);
const opened = await page.getByTestId('open-file-name').filter({ hasText: FILE }).waitFor({ timeout: 5000 }).then(() => true, () => false);
const reverse = syncCalls.slice(callsBefore).find(c => c.path.endsWith('/reverse'));
if (reverse) {
  const dx = Number(reverse.query.x) - expectPt.x, dy = Number(reverse.query.y) - expectPt.y;
  check('reverse request sent the box centre in PDF points', Math.abs(dx) < 1 && Math.abs(dy) < 1,
    `page=${reverse.query.page} x=${reverse.query.x} y=${reverse.query.y}, expected ~${expectPt.x.toFixed(2)},${expectPt.y.toFixed(2)}`);
} else {
  check('reverse request sent', false, 'no /api/synctex/reverse call seen');
}
check(`double-click opened ${FILE}`, opened, `editor shows ${await page.getByTestId('open-file-name').innerText().catch(() => '?')}; sync status: ${await page.getByTestId('sync-status').innerText().catch(() => '(none)')}`);
await page.waitForTimeout(800);
const landed = Number(await page.locator('.cm-activeLineGutter').first().innerText().catch(() => 'NaN'));
const landedText = await page.locator('.cm-activeLine').first().innerText().catch(() => '?');
check(`cursor on line ${expectedLine} (+-1)`, Math.abs(landed - expectedLine) <= 1, `landed on ${landed}: "${landedText.slice(0, 70)}"`);
await page.screenshot({ path: OUT + '/p2-reverse.png' });

await finish(browser);
