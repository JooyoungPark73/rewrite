// Drives the REAL backend (serving frontend/dist) through the phase-3 spec editor:
// skeleton adoption, the `% @p` marker on disk, says -> paper.json, stale after a
// prose edit, Reconcile, and a Tab / Shift-Tab round trip (plus re-linking the
// marker afterwards, since a level change drops the anchor).
//
//   node e2e/live/p3-e2e.mjs <repo> <screenshot dir>
//
// Assumes `rewrite serve --repo <repo> --port 8765` is running on a FRESH
// `rewrite init` of examples/sample-paper (empty paper.json) with a current
// `npm run build` in frontend/dist. Prints one PASS/FAIL line per check and
// exits non-zero if any check failed.
import { chromium } from '@playwright/test';
import fs from 'node:fs';
const REPO = process.argv[2], OUT = process.argv[3], BASE = 'http://127.0.0.1:8765';
const FILE = 'sections/intro.tex';
const SAYS = 'E2E: cold starts are frequent and costly';
const FUNCTION = 'motivates the problem';
const APPENDED = ' Extra words added by p3-e2e.';
if (!REPO || !OUT) { console.log('usage: node e2e/live/p3-e2e.mjs <repo> <screenshot dir>'); process.exit(2); }
fs.mkdirSync(OUT, { recursive: true });

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
const readTex = () => fs.readFileSync(`${REPO}/${FILE}`, 'utf8');
const readSpec = () => fs.readFileSync(`${REPO}/paper.json`, 'utf8');
/** Wait until fn() is truthy (polling), or the timeout passes; returns the last value. */
async function waitFor(fn, timeout = 8000) {
  const end = Date.now() + timeout;
  let value;
  while (Date.now() < end) {
    try { value = await fn(); } catch { value = undefined; }
    if (value) return value;
    await new Promise(r => setTimeout(r, 200));
  }
  return value;
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const errors = [];
page.on('pageerror', e => errors.push('pageerror: ' + e.message));
page.on('console', m => { if (m.type() === 'error' && !/404 \(Not Found\)/.test(m.text())) errors.push('console: ' + m.text()); });
// Log every spec write with its status, to compare against the API contract.
page.on('response', async r => {
  const u = new URL(r.url());
  if (!u.pathname.startsWith('/api/spec') || r.request().method() === 'GET') return;
  console.log('SPEC', r.request().method(), u.pathname, r.request().postData() ?? '', '->', r.status());
});

const row = id => page.locator(`[data-testid="spec-row"][data-node-id="${id}"]`);
const dotOf = id => row(id).getByTestId('status-dot').first();
const statusOf = id => dotOf(id).getAttribute('data-status').catch(() => '?');

await page.goto(BASE);
await page.locator('[data-testid=file-tree] [data-path="main.tex"]').waitFor({ timeout: 10000 });

// ---- Spec tab --------------------------------------------------------------
await page.getByTestId('tab-spec').click();
const opened = await page.getByTestId('message-card').waitFor({ timeout: 10000 }).then(() => true, () => false);
if (!check('Spec tab shows the outliner', opened)) await finish(browser);
const panel = page.getByTestId('skeleton-panel');
const heading = panel.locator('[data-testid="skeleton-heading"][data-heading="Introduction"]');
const gotSkeleton = await heading.waitFor({ timeout: 10000 }).then(() => true, () => false);
check('skeleton lists \\section{Introduction}', gotSkeleton, await panel.getByTestId('skeleton-count').innerText().catch(() => '?'));
if (!gotSkeleton) { await page.screenshot({ path: OUT + '/p3-no-skeleton.png' }); await finish(browser); }
await page.screenshot({ path: OUT + '/p3-01-empty-spec.png' });

// ---- Manual check 1: adopt the heading -------------------------------------
await heading.getByTestId('adopt-heading').click();
const sectionId = await waitFor(async () => (await heading.getAttribute('data-adopted')) || null);
check('heading adopted as a section node', !!sectionId, `id ${sectionId}`);
if (!sectionId) await finish(browser);
await row(sectionId).waitFor({ timeout: 5000 });
const sectionSays = await page.locator(`[data-testid="says-input"][data-node-id="${sectionId}"]`).inputValue();
check('section row has an empty says', sectionSays === '', JSON.stringify(sectionSays));
check('section row is linked', await waitFor(async () => (await statusOf(sectionId)) === 'linked'), `status ${await statusOf(sectionId)}`);

// ---- Manual check 2: adopt the first Introduction paragraph ----------------
const firstPara = panel.locator(`[data-testid="skeleton-paragraph"][data-file="${FILE}"]`).first();
const paraLine = Number(await firstPara.getAttribute('data-line'));
console.log(`FIRST INTRO PARAGRAPH: ${FILE}:${paraLine} "${(await firstPara.innerText()).slice(0, 60)}"`);
await firstPara.getByTestId('adopt-paragraph').click();
const section = page.locator(`[data-testid="section-block"][data-node-id="${sectionId}"]`);
const paraRow = section.locator('[data-testid="spec-row"][data-level="paragraph"]').first();
await paraRow.waitFor({ timeout: 5000 }).catch(() => {});
const pid = await paraRow.getAttribute('data-node-id').catch(() => null);
check('paragraph row appears under the section', !!pid, `id ${pid}`);
if (!pid) { await page.screenshot({ path: OUT + '/p3-adopt-fail.png' }); await finish(browser); }
check('paragraph row is linked', await waitFor(async () => (await statusOf(pid)) === 'linked'), `status ${await statusOf(pid)}`);
const texLines = readTex().split('\n');
const markerIdx = texLines.findIndex(l => l.trim() === `% @p ${pid}`);
check(`file on disk contains "% @p ${pid}" above the paragraph`, markerIdx === paraLine - 1,
  `marker on line ${markerIdx + 1}, paragraph was at ${paraLine}; next line: "${(texLines[markerIdx + 1] ?? '').slice(0, 50)}"`);
await page.screenshot({ path: OUT + '/p3-02-adopted.png' });

// ---- says -> paper.json ------------------------------------------------------
const saysBox = page.locator(`[data-testid="says-input"][data-node-id="${pid}"]`);
await saysBox.fill(SAYS);
await saysBox.press('Escape'); // blur: sends the edit at once
check('paper.json on disk contains the edited says', !!(await waitFor(() => readSpec().includes(SAYS))));

// ---- Manual check 3: edit the prose -> stale ---------------------------------
await dotOf(pid).click(); // jumps to the marker line in the editor
const inEditor = await page.getByTestId('open-file-name').filter({ hasText: FILE }).waitFor({ timeout: 5000 }).then(() => true, () => false);
const gutter = await page.locator('.cm-activeLineGutter').first().innerText().catch(() => '?');
check('status dot opens the marker line in the editor', inEditor && Number(gutter) === markerIdx + 1, `editor on ${FILE}:${gutter}`);
await page.keyboard.press('ArrowDown'); // first line of the paragraph
await page.keyboard.press('End');
await page.keyboard.type(APPENDED);
await page.keyboard.press('ControlOrMeta+s');
check('appended text saved to disk', !!(await waitFor(() => readTex().includes(APPENDED.trim()))));
await page.waitForTimeout(500); // let the cursor report settle (the active-row highlight)
await page.getByTestId('tab-spec').click();
check('row turns stale after the prose edit', await waitFor(async () => (await statusOf(pid)) === 'stale', 10000), `status ${await statusOf(pid)}`);
check('the row the editor cursor is in is highlighted', (await row(pid).getAttribute('data-active')) === 'true');
await page.screenshot({ path: OUT + '/p3-03-stale.png' });

// ---- Reconcile -> linked -------------------------------------------------------
await row(pid).getByTestId('row-menu-button').click();
const canReconcile = await row(pid).getByTestId('menu-reconcile').waitFor({ timeout: 3000 }).then(() => true, () => false);
check('⋯ menu offers Reconcile while stale', canReconcile);
if (canReconcile) await row(pid).getByTestId('menu-reconcile').click();
check('row is linked after Reconcile', await waitFor(async () => (await statusOf(pid)) === 'linked'), `status ${await statusOf(pid)}`);

// ---- Manual check 5: Tab / Shift-Tab round trip --------------------------------
await row(pid).getByTestId('row-expander').click();
await row(pid).getByTestId('field-function').fill(FUNCTION);
await row(pid).getByTestId('field-function').blur();
await waitFor(() => readSpec().includes(FUNCTION));
await saysBox.click();
await page.keyboard.press('Shift+Tab');
check('Shift-Tab: the paragraph becomes a section', await waitFor(async () => (await row(pid).getAttribute('data-level')) === 'section'));
const notesAfterPromote = await row(pid).getByTestId('notes-input').inputValue().catch(() => '?');
check('notes keep the displaced "function" field', notesAfterPromote.includes(`function: ${FUNCTION}`), JSON.stringify(notesAfterPromote));
check('promoted node keeps its says', (await saysBox.inputValue().catch(() => '?')) === SAYS);
await page.screenshot({ path: OUT + '/p3-04-promoted.png' });
await saysBox.click();
await page.keyboard.press('Tab');
check('Tab: back to a paragraph in the section', await waitFor(async () =>
  (await section.locator(`[data-testid="spec-row"][data-node-id="${pid}"][data-level="paragraph"]`).count()) === 1));
const notesAfterDemote = await row(pid).getByTestId('notes-input').inputValue().catch(() => '?');
check('notes still hold the old field after the round trip', notesAfterDemote.includes(`function: ${FUNCTION}`), JSON.stringify(notesAfterDemote));
const afterRoundTrip = await statusOf(pid);
console.log(`STATUS AFTER ROUND TRIP: ${afterRoundTrip} (the backend drops the anchor on a level change)`);

// Re-link: the marker is still in the .tex; ⋯ -> Insert marker (editor cursor is in the paragraph).
if (afterRoundTrip !== 'linked') {
  await row(pid).getByTestId('row-menu-button').click();
  await row(pid).getByTestId('menu-insert-marker').click();
  await page.getByTestId('open-file-name').filter({ hasText: FILE }).waitFor({ timeout: 5000 }).catch(() => {});
  await page.getByTestId('tab-spec').click();
  check('re-linked via ⋯ → Insert marker (no duplicate marker)', await waitFor(async () => (await statusOf(pid)) === 'linked'),
    `status ${await statusOf(pid)}; markers in file: ${readTex().split('\n').filter(l => l.includes(`@p ${pid}`)).length}`);
  check('exactly one marker line for the node', readTex().split('\n').filter(l => l.trim() === `% @p ${pid}`).length === 1);
}

// ---- Undo: Ctrl-Z restores the previous paper.json -----------------------------
const beforeUndo = readSpec();
await page.locator('body').click({ position: { x: 5, y: 5 } }).catch(() => {});
await page.keyboard.press('ControlOrMeta+z');
check('Ctrl-Z rewrites paper.json (undo)', !!(await waitFor(() => readSpec() !== beforeUndo)));
await page.keyboard.press('ControlOrMeta+Shift+z');
check('Ctrl-Shift-Z restores it (redo)', !!(await waitFor(() => readSpec() === beforeUndo)));

await page.screenshot({ path: OUT + '/p3-05-final.png' });
await finish(browser);
