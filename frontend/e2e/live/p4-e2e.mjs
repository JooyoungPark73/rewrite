// Drives the REAL backend (serving frontend/dist) through the phase-4 pass runner:
// a real `paragraph-function` run on the Introduction section, streamed events,
// findings with cost/tokens, the editor highlight on the quoted text, j / k,
// "Addressed", a cached re-run, and "Show prompt" against the stored prompt.
//
//   node e2e/live/p4-e2e.mjs <repo> <screenshot dir> [--runner claude|codex] [--model sonnet]
//
// Assumes `rewrite serve --repo <repo> --port 8765` is running on a FRESH
// `rewrite init` of examples/sample-paper with the Introduction section
// adopted in the Spec tab, and a current `npm run build` in frontend/dist.
// The run uses thinking "low"; without --runner / --model the backend's
// defaults for the paragraph level apply. A live LLM run can take minutes
// (this waits up to 4). Prints one PASS/FAIL line per check (SKIP lines do not
// count) and exits non-zero if any check failed.
import { chromium } from '@playwright/test';
import fs from 'node:fs';

const args = process.argv.slice(2);
const flag = (name) => { const i = args.indexOf(name); if (i < 0) return null; const v = args[i + 1]; args.splice(i, 2); return v ?? null; };
const RUNNER = flag('--runner');
const MODEL = flag('--model');
const [REPO, OUT] = args;
const BASE = 'http://127.0.0.1:8765';
const PASS = 'paragraph-function';
const RUN_TIMEOUT_MS = 4 * 60_000;
if (!REPO || !OUT || (RUNNER && !['claude', 'codex'].includes(RUNNER))) {
  console.log('usage: node e2e/live/p4-e2e.mjs <repo> <screenshot dir> [--runner claude|codex] [--model sonnet]');
  process.exit(2);
}
fs.mkdirSync(OUT, { recursive: true });

const results = [];
function check(name, ok, info = '') {
  results.push({ name, ok });
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${info ? ' | ' + info : ''}`);
  return ok;
}
const skip = (name, why) => console.log(`SKIP ${name} | ${why}`);
async function finish(browser) {
  const failed = results.filter(r => !r.ok);
  console.log('JS ERRORS:', errors.length ? errors : 'none');
  console.log(failed.length ? `RESULT: FAIL (${failed.length} of ${results.length} checks failed)` : `RESULT: PASS (${results.length} checks)`);
  await browser.close();
  process.exit(failed.length ? 1 : 0);
}
const api = async (path) => (await fetch(BASE + path)).json();
/** Collapse whitespace, for comparing quotes with editor text. */
const squash = (s) => String(s ?? '').replace(/\s+/g, ' ').trim();
/** Wait until fn() is truthy (polling), or the timeout passes; returns the last value. */
async function waitFor(fn, timeout = 8000, every = 250) {
  const end = Date.now() + timeout;
  let value;
  while (Date.now() < end) {
    try { value = await fn(); } catch { value = undefined; }
    if (value) return value;
    await new Promise(r => setTimeout(r, every));
  }
  return value;
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const errors = [];
page.on('pageerror', e => errors.push('pageerror: ' + e.message));
page.on('console', m => { if (m.type() === 'error' && !/404 \(Not Found\)/.test(m.text())) errors.push('console: ' + m.text()); });
// Every runs/findings write with its status, to compare against the API contract.
page.on('response', async r => {
  const u = new URL(r.url());
  if (!/^\/api\/(runs|findings)/.test(u.pathname) || r.request().method() === 'GET') return;
  console.log('API', r.request().method(), u.pathname, (r.request().postData() ?? '').slice(0, 200), '->', r.status());
});
// WebSocket frames: count run.* events per type.
const wsEvents = [];
page.on('websocket', ws => ws.on('framereceived', f => {
  try { const e = JSON.parse(f.payload); if (/^(run|findings)\./.test(e.type)) wsEvents.push(e); } catch { /* not JSON */ }
}));

const runCards = page.getByTestId('run-findings').getByTestId('finding-card');
const sidebar = page.getByTestId('findings-sidebar');
const sidebarCards = sidebar.getByTestId('finding-card');

await page.goto(BASE);
await page.locator('[data-testid=file-tree] [data-path="main.tex"]').waitFor({ timeout: 10000 });

// ---- Launcher ------------------------------------------------------------------
await page.getByTestId('tab-findings').click();
const launcherReady = await page.getByTestId('pass-select').locator(`option[value="${PASS}"]`).waitFor({ state: 'attached', timeout: 10000 }).then(() => true, () => false);
if (!check(`launcher lists ${PASS}`, launcherReady)) { await page.screenshot({ path: OUT + '/p4-no-passes.png' }); await finish(browser); }
const passes = await api('/api/passes');
console.log('PASSES:', passes.passes.map(p => `${p.name}(${p.level}${p.error ? ', ERROR ' + p.error : ''})`).join(' '));
const runners = await api('/api/runners');
console.log('RUNNERS:', JSON.stringify(runners.runners), 'DEFAULTS:', JSON.stringify(runners.defaults.paragraph), 'GLOBAL:', JSON.stringify(runners.global_instructions));

await page.getByTestId('pass-select').selectOption(PASS);
await page.getByTestId('scope-section').check().catch(() => {});
const sectionSelect = page.getByTestId('scope-section-select');
const introValue = await waitFor(async () => {
  const opts = await sectionSelect.locator('option').evaluateAll(els => els.map(o => ({ value: o.value, text: o.textContent, disabled: o.disabled })));
  return opts.find(o => /Introduction/.test(o.text) && !o.disabled)?.value;
}, 10000);
if (!check('section dropdown offers the adopted Introduction section', !!introValue, `value ${introValue}`)) {
  await page.screenshot({ path: OUT + '/p4-no-section.png' }); await finish(browser);
}
await sectionSelect.selectOption(introValue);
await page.getByTestId('scope-section').check();
if (RUNNER) {
  const disabled = await page.getByTestId('runner-select').locator(`option[value="${RUNNER}"]`).isDisabled();
  if (!check(`runner ${RUNNER} is available`, !disabled)) await finish(browser);
  await page.getByTestId('runner-select').selectOption(RUNNER);
}
if (MODEL) await page.getByTestId('model-input').fill(MODEL);
await page.getByTestId('thinking-select').selectOption('low');
await page.screenshot({ path: OUT + '/p4-01-launcher.png' });

// ---- Run --------------------------------------------------------------------------
const started = Date.now();
const postRun = page.waitForResponse(r => new URL(r.url()).pathname === '/api/runs' && r.request().method() === 'POST', { timeout: 15000 });
await page.getByTestId('run-button').click();
const postResponse = await postRun.catch(() => null);
const posted = postResponse ? await postResponse.json().catch(() => null) : null;
const runId = posted?.run?.id;
if (!check('POST /api/runs starts a run', postResponse?.status() === 200 && !!runId, `status ${postResponse?.status()} ${JSON.stringify(posted).slice(0, 300)}`)) {
  await page.screenshot({ path: OUT + '/p4-run-refused.png' }); await finish(browser);
}
const row = page.locator(`[data-testid="run-row"][data-run-id="${runId}"]`);
await row.waitFor({ timeout: 10000 });
await page.waitForTimeout(4000);
await page.screenshot({ path: OUT + '/p4-02-running.png' });
const ended = await waitFor(async () => wsEvents.some(e => (e.type === 'run.finished' || e.type === 'run.failed') && e.run?.id === runId), RUN_TIMEOUT_MS, 1000);
const elapsed = ((Date.now() - started) / 1000).toFixed(1);
const textEvents = wsEvents.filter(e => e.type === 'run.event' && e.run_id === runId);
console.log(`EVENTS: run.started=${wsEvents.filter(e => e.type === 'run.started' && e.run?.id === runId).length} run.event=${textEvents.length} (${textEvents.filter(e => e.kind === 'text').length} text) after ${elapsed}s`);
check(`run.finished (or run.failed) arrived within 4 minutes`, !!ended, `${elapsed}s`);
const status = await waitFor(() => row.getAttribute('data-status').then(s => (s === 'queued' || s === 'running' ? null : s)), 10000);
const detail = await api(`/api/runs/${runId}`);
check('the run ended "done"', status === 'done', `status ${status}; error ${detail.run?.error ?? '-'}`);
check('live output was streamed (run.event)', textEvents.length > 0, `${textEvents.length} events`);
console.log('RUN:', JSON.stringify({ ...detail.run, scope: undefined }));
await page.screenshot({ path: OUT + '/p4-03-finished.png' });
if (status !== 'done') await finish(browser);

// ---- Findings, cost, tokens --------------------------------------------------------
const cost = await row.getByTestId('run-cost').innerText();
const tokens = await row.getByTestId('run-tokens').innerText();
const duration = await row.getByTestId('run-duration').innerText();
const runner = detail.run.runner;
check('run row shows the cost', runner === 'codex' ? cost === '—' || /^\$\d+\.\d{4}$/.test(cost) : /^\$\d+\.\d{4}$/.test(cost), `${cost} (runner ${runner}; codex reports no cost)`);
check('run row shows tokens in / out', /^\d[\d,]* in \/ \d[\d,]* out$/.test(tokens), `${tokens}, ${duration}`);
const cardCount = await runCards.count();
check('findings rendered (>= 0) match the API', cardCount === detail.findings.length, `${cardCount} cards, ${detail.findings.length} findings, ${detail.rejected.length} rejected`);
for (const f of detail.findings) console.log(`FINDING ${f.id} ${f.severity} ${f.level}/${f.category} ${f.file}:${f.anchor_line_start}-${f.anchor_line_end} [${f.anchor_state}] "${squash(f.quote).slice(0, 70)}"`);
for (const r of detail.rejected) console.log(`REJECTED #${r.index} ${r.reason}: ${JSON.stringify(r.raw).slice(0, 160)}`);

if (cardCount > 0) {
  // ---- Click -> editor highlight on the quote -------------------------------------
  const anchored = detail.findings.find(f => f.anchor_state === 'ok') ?? null;
  const target = anchored ?? detail.findings[0];
  await page.locator(`[data-testid="run-findings"] [data-testid="finding-card"][data-finding-id="${target.id}"]`).click();
  const inEditor = await page.getByTestId('open-file-name').filter({ hasText: target.file }).waitFor({ timeout: 8000 }).then(() => true, () => false);
  check('clicking a finding opens its file in the Editor tab', inEditor, target.file);
  const active = page.locator('.cm-line.cm-finding-active');
  await active.first().waitFor({ timeout: 5000 }).catch(() => {});
  const activeText = squash((await active.allInnerTexts()).join(' '));
  const firstWords = squash(target.quote).split(' ').slice(0, 5).join(' ');
  const gutter = await page.locator('.cm-activeLineGutter').first().innerText().catch(() => '?');
  if (anchored) {
    check('the highlight lands on the quoted text (first 5 words)', activeText.includes(firstWords),
      `"${firstWords}" in lines ${target.anchor_line_start}-${target.anchor_line_end}: "${activeText.slice(0, 120)}"; cursor line ${gutter}`);
  } else {
    skip('highlight on the quoted text', `no finding has a verified anchor (first is ${target.anchor_state}); highlight at the model's lines: "${activeText.slice(0, 80)}"`);
  }
  check('the findings column shows the file\'s findings', (await sidebarCards.count()) > 0, `${await sidebarCards.count()} cards`);
  await page.screenshot({ path: OUT + '/p4-04-highlight.png' });

  // ---- j / k --------------------------------------------------------------------------
  const ids = await sidebarCards.evaluateAll(els => els.map(e => e.getAttribute('data-finding-id')));
  const selectedId = () => sidebar.locator('[data-selected="true"]').getAttribute('data-finding-id').catch(() => null);
  const start = ids.indexOf(await selectedId());
  const afterJ = ids[Math.min(start + 1, ids.length - 1)];
  const afterK = ids[Math.max(Math.min(start + 1, ids.length - 1) - 1, 0)];
  await page.keyboard.press('j');
  const gotJ = await waitFor(async () => (await selectedId()) === afterJ, 3000);
  await page.keyboard.press('k');
  const gotK = await waitFor(async () => (await selectedId()) === afterK, 3000);
  check('j / k move the selection through the column', start >= 0 && !!gotJ && !!gotK, `${ids.length} cards; start ${start}, j -> ${afterJ}, k -> ${afterK}`);
  check('j / k typed nothing into the editor', (await page.getByTestId('dirty-marker').count()) === 0);

  // ---- Addressed ------------------------------------------------------------------------
  const sel = await selectedId();
  const before = await sidebarCards.count();
  await sidebar.locator(`[data-finding-id="${sel}"]`).getByTestId('finding-addressed').click();
  const gone = await waitFor(async () => (await sidebarCards.count()) === before - 1, 5000);
  const stored = (await api(`/api/findings?run_id=${runId}`)).findings.find(f => f.id === sel);
  check('"Addressed" removes the card from the open list and is stored', !!gone && stored?.state === 'addressed', `state ${stored?.state}`);
  await page.screenshot({ path: OUT + '/p4-05-addressed.png' });
  await page.getByTestId('tab-findings').click();
} else {
  skip('highlight, j / k, Addressed', 'the run returned no findings');
}

// ---- Re-run -> cached ---------------------------------------------------------------
const rerun = page.waitForResponse(r => new URL(r.url()).pathname === '/api/runs' && r.request().method() === 'POST', { timeout: 15000 });
await page.getByTestId('run-button').click();
const rerunBody = await (await rerun.catch(() => null))?.json().catch(() => null);
const cachedId = rerunBody?.run?.id;
const cachedRow = page.locator(`[data-testid="run-row"][data-run-id="${cachedId}"]`);
const cachedOk = await cachedRow.getByTestId('cached-badge').waitFor({ timeout: 10000 }).then(() => true, () => false);
check('re-running the unchanged scope shows the cached badge', cachedOk && rerunBody?.run?.status === 'cached',
  `run ${cachedId} status ${rerunBody?.run?.status} cached_from ${rerunBody?.run?.cached_from}`);
await page.screenshot({ path: OUT + '/p4-06-cached.png' });

// ---- Show prompt == stored prompt -----------------------------------------------------
await page.getByTestId('show-prompt').click();
const promptBox = page.getByTestId('prompt-text');
await promptBox.waitFor({ timeout: 10000 }).catch(() => {});
const shown = (await promptBox.textContent().catch(() => '')) ?? '';
const stored = await api(`/api/runs/${runId}/prompt`);
let diffAt = -1;
for (let i = 0; i < Math.max(shown.length, stored.prompt.length); i++) if (shown[i] !== stored.prompt[i]) { diffAt = i; break; }
check('Show prompt is non-empty', shown.length > 0, `${shown.length} characters`);
check('Show prompt equals GET /api/runs/{id}/prompt', shown === stored.prompt,
  diffAt < 0 ? 'identical' : `first difference at ${diffAt}: shown "${shown.slice(diffAt, diffAt + 40)}" vs stored "${stored.prompt.slice(diffAt, diffAt + 40)}"`);
console.log('COMMAND:', JSON.stringify(stored.command));
console.log('PREVIEW CACHE BADGE:', await page.getByTestId('prompt-cache-badge').innerText().catch(() => '?'));
await page.screenshot({ path: OUT + '/p4-07-prompt.png' });
await page.keyboard.press('Escape');

await finish(browser);
