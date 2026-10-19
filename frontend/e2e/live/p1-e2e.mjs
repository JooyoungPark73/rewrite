// Drives the REAL backend (serving frontend/dist) through phase-1 manual checks 3-6:
//   3  the file tree shows main.tex and every \input / \include file of main.tex
//   4  editing a file and pressing Ctrl-S writes it to disk; the dirty marker clears
//   5  Compile produces .rewrite/build/main.pdf and the PDF pane shows it
//   6  a deliberate \undefinedmacro appears in the error list with file and line;
//      clicking it opens the file at that line
//
//   node e2e/live/p1-e2e.mjs <repo> <screenshot dir>
//
// Assumes `rewrite serve --repo <repo> --port 8765` is running (REWRITE_URL
// overrides the address) on an initialised copy of examples/sample-paper, with a
// current `npm run build` in frontend/dist. Prints one PASS/FAIL line per check
// and exits non-zero if any check failed (audit 2026-10-18: a clean exit alone
// is not a pass). The two .tex files it edits are restored on disk at the end.
//
// Allowed HTTP errors: one 404 for GET /api/pdf before the first compile (no PDF
// has been built yet) and 404s from /api/synctex/* (SyncTeX's "no match"
// answer). Any other HTTP error, page error or console error fails a check.
import { chromium } from '@playwright/test';
import fs from 'node:fs';
const REPO = process.argv[2], OUT = process.argv[3];
const BASE = process.env.REWRITE_URL ?? 'http://127.0.0.1:8765';
const EDIT_FILE = 'sections/intro.tex';
const ERROR_FILE = 'sections/method.tex';
const ERROR_LINE = 3; // \undefinedmacro is typed at the start of this line
const MARK = `% edited by p1-e2e ${Date.now()}`;
if (!REPO || !OUT) { console.log('usage: node e2e/live/p1-e2e.mjs <repo> <screenshot dir>'); process.exit(2); }
fs.mkdirSync(OUT, { recursive: true });

const results = [];
function check(name, ok, info = '') {
  results.push({ name, ok });
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${info ? ' | ' + info : ''}`);
  return ok;
}

const readFile = (path) => fs.readFileSync(`${REPO}/${path}`, 'utf8');
const originals = { [EDIT_FILE]: readFile(EDIT_FILE), [ERROR_FILE]: readFile(ERROR_FILE) };

/** Wait until fn() is truthy (polling), or the timeout passes; returns the last value. */
async function waitFor(fn, timeout = 8000) {
  const end = Date.now() + timeout;
  let value;
  while (Date.now() < end) {
    value = await fn();
    if (value) return value;
    await new Promise(r => setTimeout(r, 200));
  }
  return value;
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });

// Page errors and console errors. Console lines for failed HTTP requests
// ("404 (Not Found)" etc.) are judged through `httpErrors` below instead.
const jsErrors = [];
page.on('pageerror', e => jsErrors.push('pageerror: ' + e.message));
page.on('console', m => {
  if (m.type() === 'error' && !/the server responded with a status of \d+/.test(m.text())) jsErrors.push('console: ' + m.text());
});
// Every HTTP error response, and whether a compile had been requested before it.
let compileRequested = false;
const httpErrors = [];
page.on('request', r => {
  if (r.method() === 'POST' && new URL(r.url()).pathname === '/api/compile') compileRequested = true;
});
page.on('response', r => {
  if (r.status() < 400) return;
  httpErrors.push({ method: r.request().method(), path: new URL(r.url()).pathname, status: r.status(), afterCompile: compileRequested });
});

async function finish() {
  // The expected 404s (see the header); everything else is a failure.
  const pdfBeforeCompile = httpErrors.filter(e => e.path === '/api/pdf' && e.status === 404 && !e.afterCompile);
  const synctexNoMatch = httpErrors.filter(e => e.path.startsWith('/api/synctex/') && e.status === 404);
  const unexpected = httpErrors.filter(e => !pdfBeforeCompile.includes(e) && !synctexNoMatch.includes(e));
  check('at most one 404 for /api/pdf, and only before the first compile', pdfBeforeCompile.length <= 1,
    `${pdfBeforeCompile.length} before compile`);
  check('no other HTTP errors', unexpected.length === 0,
    unexpected.map(e => `${e.status} ${e.method} ${e.path}`).join(', ') || `none (${synctexNoMatch.length} synctex no-match 404s)`);
  check('no page or console errors', jsErrors.length === 0, jsErrors.join(' || ') || 'none');
  await browser.close();
  // Put the two edited files back so the repo can be reused by the other live scripts.
  for (const [path, text] of Object.entries(originals)) fs.writeFileSync(`${REPO}/${path}`, text);
  const failed = results.filter(r => !r.ok);
  console.log(failed.length ? `RESULT: FAIL (${failed.length} of ${results.length} checks failed)` : `RESULT: PASS (${results.length} checks)`);
  process.exit(failed.length ? 1 : 0);
}

const tree = page.getByTestId('file-tree');
const openName = page.getByTestId('open-file-name');
const editor = page.locator('[data-testid=editor] .cm-content');

async function openInTree(path) {
  await tree.locator(`[data-path="${path}"]`).click();
  return openName.filter({ hasText: new RegExp(`^${path.replace(/[.]/g, '\\.')}$`) }).waitFor({ timeout: 5000 }).then(() => true, () => false);
}

await page.goto(BASE);

// ---- Manual check 3: file tree --------------------------------------------
// Expected files come from main.tex on disk (top-level \input / \include,
// comments ignored), independently of the backend's resolver.
const mainTex = readFile('main.tex').split('\n').map(l => l.replace(/(^|[^\\])%.*$/, '$1')).join('\n');
const inputs = [...mainTex.matchAll(/\\(?:input|include)\{([^}]+)\}/g)].map(m => (m[1].endsWith('.tex') ? m[1] : `${m[1]}.tex`));
const treeShown = await tree.locator('[data-path="main.tex"]').waitFor({ timeout: 10000 }).then(() => true, () => false);
if (!check('file tree shows main.tex', treeShown)) await finish();
const listed = await tree.locator('[data-path]').evaluateAll(els => els.map(e => e.getAttribute('data-path')));
const missing = inputs.filter(p => !listed.includes(p));
check(`file tree lists all ${inputs.length} \\input/\\include files of main.tex`, inputs.length > 0 && missing.length === 0,
  missing.length ? `missing: ${missing.join(', ')}` : listed.join(' | '));
check('main.tex is open in the editor', await openName.filter({ hasText: /^main\.tex$/ }).waitFor({ timeout: 5000 }).then(() => true, () => false));

// ---- Manual check 4: edit + Ctrl-S ----------------------------------------
if (!check(`opened ${EDIT_FILE}`, await openInTree(EDIT_FILE))) await finish();
await editor.click();
await page.keyboard.press('Control+End');
await page.keyboard.type(`\n${MARK}\n`);
check('dirty marker shows after typing', await page.getByTestId('dirty-marker').waitFor({ timeout: 3000 }).then(() => true, () => false));
await page.keyboard.press('Control+s');
check('Ctrl-S wrote the edit to disk', !!(await waitFor(() => readFile(EDIT_FILE).includes(MARK), 5000)));
check('dirty marker cleared after the save', await page.getByTestId('dirty-marker').waitFor({ state: 'detached', timeout: 5000 }).then(() => true, () => false));

// ---- Manual check 5: compile + PDF ------------------------------------------
await page.getByRole('button', { name: 'Compile' }).click();
const pageCount = page.getByTestId('pdf-page-count').filter({ hasText: /\d+ pages?/ });
const pdfShown = await pageCount.waitFor({ timeout: 90000 }).then(() => true, () => false);
check('compile produced .rewrite/build/main.pdf', fs.existsSync(`${REPO}/.rewrite/build/main.pdf`));
check('PDF pane shows the pages', pdfShown, await page.getByTestId('pdf-page-count').innerText().catch(() => '?'));
const canvas = page.locator('[data-testid=pdf-viewer] canvas').first();
const canvasWidth = await waitFor(() => canvas.evaluate(c => c.width).catch(() => 0), 10000);
const box = await canvas.boundingBox().catch(() => null);
check('first PDF page is drawn on a canvas', canvasWidth > 0 && !!box && box.width > 0 && box.height > 0,
  `bitmap width ${canvasWidth}, box ${JSON.stringify(box)}`);
await page.screenshot({ path: OUT + '/p1-after-compile.png' });

// ---- Manual check 6: error list + click to the line -------------------------
if (!check(`opened ${ERROR_FILE}`, await openInTree(ERROR_FILE))) await finish();
await editor.click();
await page.keyboard.press('Control+Home');
for (let i = 1; i < ERROR_LINE; i++) await page.keyboard.press('ArrowDown');
await page.keyboard.press('Home');
await page.keyboard.type('\\undefinedmacro\n');
await page.keyboard.press('Control+s');
check('the \\undefinedmacro line was saved', !!(await waitFor(() => readFile(ERROR_FILE).split('\n')[ERROR_LINE - 1] === '\\undefinedmacro', 5000)));
await page.getByRole('button', { name: 'Compile' }).click();
const errorRow = page.getByTestId('error-row').filter({ hasText: 'Undefined control sequence' }).filter({ hasText: `${ERROR_FILE}:${ERROR_LINE}` });
const listedError = await errorRow.first().waitFor({ timeout: 90000 }).then(() => true, () => false);
const rows = await page.getByTestId('error-row').allInnerTexts().catch(() => []);
check(`error list shows ${ERROR_FILE}:${ERROR_LINE} "Undefined control sequence"`, listedError,
  rows.map(r => r.replace(/\s+/g, ' ').slice(0, 90)).join(' || ') || 'no error rows');
// Switch to another file first so the click has to navigate.
await openInTree(EDIT_FILE);
if (listedError) {
  await errorRow.first().click();
  const navigated = await openName.filter({ hasText: ERROR_FILE }).waitFor({ timeout: 5000 }).then(() => true, () => false);
  check(`clicking the error opened ${ERROR_FILE}`, navigated, `editor shows ${await openName.innerText().catch(() => '?')}`);
  await page.waitForTimeout(500);
  const gutter = await page.locator('.cm-activeLineGutter').first().innerText().catch(() => '?');
  const lineText = await page.locator('.cm-activeLine').first().innerText().catch(() => '?');
  check(`cursor is on line ${ERROR_LINE}, the \\undefinedmacro line`, gutter.trim() === String(ERROR_LINE) && lineText.includes('\\undefinedmacro'),
    `gutter ${gutter.trim()}, text "${lineText.slice(0, 60)}"`);
}
await page.screenshot({ path: OUT + '/p1-error.png' });

await finish();
