// Drives the REAL backend (serving frontend/dist) through phase 6: "Run both"
// (claude + codex on the same pass and section, merged by anchor), a triage call
// on the claude member run, and a per-section fan-out with claude haiku.
//
//   node e2e/live/p6-e2e.mjs <repo> <screenshot dir> [--model sonnet]
//
// Assumes `rewrite serve --repo <repo> --port 8765` is running (REWRITE_URL
// overrides http://127.0.0.1:8765) with a current `npm run build` in frontend/dist,
// on a `rewrite init` of examples/sample-paper with ALL sections adopted in the
// Spec tab (so every \section is a linked spec section), and with both claude and
// codex installed and logged in. Steps:
//   1. Run both: paragraph-function on the Introduction, claude (--model, thinking
//      low) + codex (default model, thinking low); waits up to 5 minutes; checks the
//      group ends "done", the group row says 2/2, and the merged list on screen
//      matches GET /api/groups/{id} entry by entry (agreement badge, runners).
//   2. Triage the claude member (claude, same model, thinking low); waits up to 4
//      minutes; checks the verdict counts on screen, on the cards and in the API
//      agree; then "Dismiss all flagged" and checks the API states.
//   3. Fan out paragraph-function with claude haiku, thinking low; waits up to 8
//      minutes; checks N + 1 member runs (N linked sections + message-drift over
//      scope "ends"), at most 3 running at once (from the run.* events), the
//      group ends done or partial, and the merged view shows one block per member.
// Without --model the claude runs use the backend's default model. Re-running on the
// same repo answers unchanged runs from the cache (fast, no cost; the concurrency
// check is then skipped). Prints one PASS/FAIL line per check (SKIP lines do not
// count) and exits non-zero if any check failed.
import { chromium } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';

const args = process.argv.slice(2);
const flag = (name) => { const i = args.indexOf(name); if (i < 0) return null; const v = args[i + 1]; args.splice(i, 2); return v ?? null; };
const MODEL = flag('--model');
const [REPO, OUT] = args;
const BASE = process.env.REWRITE_URL ?? 'http://127.0.0.1:8765';
const PASS = 'paragraph-function';
const FANOUT_MODEL = 'haiku';
const BOTH_TIMEOUT_MS = 5 * 60_000;
const TRIAGE_TIMEOUT_MS = 4 * 60_000;
const FANOUT_TIMEOUT_MS = 8 * 60_000;
if (!REPO || !OUT) {
  console.log('usage: node e2e/live/p6-e2e.mjs <repo> <screenshot dir> [--model sonnet]');
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
const active = (status) => status === 'queued' || status === 'running';
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
/** Poll GET /api/groups/{id} until the group has ended (or the timeout); returns the last detail. */
async function waitForGroup(id, timeout) {
  let last = null;
  await waitFor(async () => { last = await api(`/api/groups/${id}`); return !active(last.group.status); }, timeout, 3000);
  return last;
}
/** Number of `claude -p` processes right now (informational: other sessions may run some too). */
function claudeProcesses() {
  try { return execFileSync('pgrep', ['-fc', 'claude -p'], { encoding: 'utf8' }).trim() * 1; } catch { return 0; }
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const errors = [];
page.on('pageerror', e => errors.push('pageerror: ' + e.message));
page.on('console', m => { if (m.type() === 'error' && !/404 \(Not Found\)/.test(m.text())) errors.push('console: ' + m.text()); });
// Every phase 6 write with its status, to compare against the API contract.
page.on('response', async r => {
  const u = new URL(r.url());
  if (!/^\/api\/(groups|runs\/[^/]+\/triage|findings\/dismiss-flagged)/.test(u.pathname) || r.request().method() === 'GET') return;
  console.log('API', r.request().method(), u.pathname, (r.request().postData() ?? '').slice(0, 300), '->', r.status());
});
// WebSocket frames: run.*, group.updated, triage.finished.
const wsEvents = [];
page.on('websocket', ws => ws.on('framereceived', f => {
  try { const e = JSON.parse(f.payload); if (/^(run|group|triage)\./.test(e.type)) wsEvents.push({ ...e, at: Date.now() }); } catch { /* not JSON */ }
}));

// ---- Preconditions -------------------------------------------------------------------
const runners = await api('/api/runners');
const available = Object.fromEntries(runners.runners.map(r => [r.name, r.available]));
console.log('RUNNERS:', JSON.stringify(runners.runners.map(r => ({ name: r.name, available: r.available }))), 'DEFAULTS:', JSON.stringify(runners.defaults.paragraph));
if (!check('claude and codex are both available', available.claude && available.codex, JSON.stringify(available))) await finish(browser);
const { spec } = await api('/api/spec');
const status = (await api('/api/spec/status')).nodes;
const skeleton = await api('/api/spec/skeleton');
const sectionsInSpec = spec?.tree.children ?? [];
const linked = sectionsInSpec.filter(n => status[n.id]?.status === 'linked');
const unadopted = skeleton.sections.filter(s => !s.adopted_id);
console.log('SECTIONS:', sectionsInSpec.map(n => `${n.id}[${status[n.id]?.status}] ${n.anchor?.marker ?? ''}`).join(' | '));
check('every heading is adopted and linked', unadopted.length === 0 && linked.length === sectionsInSpec.length && linked.length > 0,
  `${linked.length} linked of ${sectionsInSpec.length} spec sections; unadopted headings: ${unadopted.map(s => s.heading).join(', ') || 'none'}`);
const intro = linked.find(n => /Introduction/.test(n.anchor?.marker ?? ''));
if (!check('the Introduction section is linked', !!intro, intro?.id ?? 'not found')) await finish(browser);

await page.goto(BASE);
await page.locator('[data-testid=file-tree] [data-path="main.tex"]').waitFor({ timeout: 10000 });
await page.getByTestId('tab-findings').click();
const ready = await page.getByTestId('pass-select').locator(`option[value="${PASS}"]`).waitFor({ state: 'attached', timeout: 10000 }).then(() => true, () => false);
if (!check(`launcher lists ${PASS}`, ready)) { await page.screenshot({ path: OUT + '/p6-no-passes.png' }); await finish(browser); }
const passes = (await api('/api/passes')).passes;
const shown = await page.getByTestId('pass-select').locator('option').evaluateAll(els => els.map(o => o.value));
const hiddenKinds = passes.filter(p => (p.kind ?? 'review') !== 'review').map(p => `${p.name}(${p.kind})`);
check('passes whose kind is not "review" are not in the select', passes.filter(p => (p.kind ?? 'review') !== 'review').every(p => !shown.includes(p.name)) && hiddenKinds.length > 0,
  `hidden: ${hiddenKinds.join(', ') || 'none (the backend reports no kinds)'}`);

// ---- 1. Run both ------------------------------------------------------------------------
await page.getByTestId('pass-select').selectOption(PASS);
await page.getByTestId('scope-section').check();
await page.getByTestId('scope-section-select').selectOption(intro.id);
await page.getByTestId('run-both-button').click();
const panel = page.getByTestId('run-both-button-panel');
await panel.waitFor({ timeout: 5000 });
if (MODEL) await panel.getByTestId('both-claude-model-input').fill(MODEL);
await panel.getByTestId('both-claude-thinking-select').selectOption('low');
await panel.getByTestId('both-codex-thinking-select').selectOption('low');
await page.screenshot({ path: OUT + '/p6-01-run-both-popover.png' });
const bothStarted = Date.now();
const postBoth = page.waitForResponse(r => new URL(r.url()).pathname === '/api/groups/both' && r.request().method() === 'POST', { timeout: 15000 });
await panel.getByTestId('run-both-start').click();
const bothResponse = await postBoth.catch(() => null);
const bothBody = bothResponse ? await bothResponse.json().catch(() => null) : null;
const bothId = bothBody?.group?.id;
if (!check('POST /api/groups/both starts a group of 2 runs', bothResponse?.status() === 200 && !!bothId && bothBody.group.run_ids?.length === 2,
  `status ${bothResponse?.status()} ${JSON.stringify(bothBody).slice(0, 300)}`)) {
  await page.screenshot({ path: OUT + '/p6-both-refused.png' }); await finish(browser);
}
const bothRow = page.locator(`[data-testid="group-row"][data-group-id="${bothId}"]`);
check('the group row appears, selected, with kind "both"', await bothRow.waitFor({ timeout: 10000 }).then(() => true, () => false)
  && (await bothRow.getAttribute('data-selected')) === 'true' && (await bothRow.getByTestId('group-kind').innerText()) === 'both');
await page.waitForTimeout(5000);
await page.screenshot({ path: OUT + '/p6-02-both-running.png' });

const both = await waitForGroup(bothId, BOTH_TIMEOUT_MS);
const bothSecs = ((Date.now() - bothStarted) / 1000).toFixed(1);
for (const r of both.runs) console.log(`MEMBER ${r.id} ${r.runner} model=${r.model ?? '-'} used=${r.model_used ?? '-'} thinking=${r.thinking} ${r.status} findings=${r.findings_count} rejected=${r.rejected_count} cost=${r.cost_usd} ${r.error ?? ''}`);
console.log('GROUP:', JSON.stringify(both.group));
check('the "both" group ended "done" within 5 minutes', both.group.status === 'done', `${both.group.status} after ${bothSecs}s`);
check('group.updated events arrived', wsEvents.some(e => e.type === 'group.updated' && e.group?.id === bothId), `${wsEvents.filter(e => e.type === 'group.updated' && e.group?.id === bothId).length} events`);
const rowStatus = await waitFor(async () => { const s = await bothRow.getAttribute('data-status'); return active(s) ? null : s; }, 15000);
const progress = await bothRow.getByTestId('group-progress').innerText().catch(() => '?');
check('the group row shows the final status and 2/2 done', rowStatus === both.group.status && progress.trim() === '2/2 done', `${rowStatus}, "${progress.trim()}"`);
const agreementsApi = both.merged.filter(e => e.agreement > 1).length;
check('agreements_count matches the merged list', both.group.agreements_count === agreementsApi, `${both.group.agreements_count} vs ${agreementsApi}`);

// The merged view, compared entry by entry with the API (filters: all states, all levels).
await bothRow.click();
await page.getByTestId('group-detail').getByTestId('state-filter-all').click();
const entries = page.getByTestId('merged-list').getByTestId('merged-entry');
await waitFor(async () => (await entries.count()) === both.merged.length, 10000);
const onScreen = await entries.evaluateAll(els => els.map(e => ({
  agreement: Number(e.getAttribute('data-agreement')),
  file: e.getAttribute('data-file'),
  badge: e.querySelector('[data-testid="agreement-badge"]')?.textContent?.trim() ?? null,
  runners: [...e.querySelectorAll('[data-testid="runner-chip"]')].map(c => c.textContent.trim()),
})));
for (const e of both.merged) console.log(`MERGED ${e.agreement}x [${e.runners.join('+')}] ${e.severity} ${e.level}/${e.category} ${e.file}:${e.line_start}-${e.line_end} (${e.findings.length} findings)`);
check('the merged list renders every entry of GET /api/groups/{id}', onScreen.length === both.merged.length, `${onScreen.length} on screen, ${both.merged.length} in the API`);
const mismatches = both.merged.map((e, i) => {
  const s = onScreen[i];
  const badge = e.agreement > 1 ? `${e.agreement}/2 agree` : `${e.agreement}/2`;
  const ok = s && s.agreement === e.agreement && s.file === e.file && s.badge === badge && JSON.stringify(s.runners) === JSON.stringify(e.runners);
  return ok ? null : `#${i}: api ${e.agreement} ${badge} [${e.runners}] ${e.file} vs screen ${JSON.stringify(s)}`;
}).filter(Boolean);
check('agreement badges and runner chips match the API, in the same order', mismatches.length === 0, mismatches.join('; ') || `${both.merged.length} entries, ${agreementsApi} agreements`);
const sorted = both.merged.every((e, i) => i === 0 || both.merged[i - 1].agreement >= e.agreement);
check('agreements are listed first', sorted, both.merged.map(e => e.agreement).join(','));
if (agreementsApi > 0) {
  await entries.first().getByTestId('merged-expand').click();
  const perRunner = await entries.first().getByTestId('merged-runner-finding').evaluateAll(els => els.map(e => e.getAttribute('data-runner')));
  check('an agreement unfolds into each runner\'s finding', perRunner.length >= 2 && new Set(perRunner).size >= 2, perRunner.join(', '));
} else {
  skip('agreement unfolding', 'the two runners agreed on nothing this time');
}
await page.screenshot({ path: OUT + '/p6-03-both-merged.png' });
if (both.merged.length > 0) {
  const first = both.merged[0].findings[0];
  await entries.first().getByTestId('merged-problem').click();
  const inEditor = await page.getByTestId('open-file-name').filter({ hasText: first.file }).waitFor({ timeout: 8000 }).then(() => true, () => false);
  const highlighted = await page.locator('.cm-line.cm-finding-active').first().waitFor({ timeout: 5000 }).then(() => true, () => false);
  check('clicking an entry highlights its first finding in the editor', inEditor && highlighted, `${first.file}:${first.anchor_line_start}-${first.anchor_line_end}`);
  await page.screenshot({ path: OUT + '/p6-04-entry-in-editor.png' });
  await page.getByTestId('tab-findings').click();
}

// ---- 2. Triage the claude member ------------------------------------------------------------
const claudeRun = both.runs.find(r => r.runner === 'claude');
const claudeDetail = claudeRun ? await api(`/api/runs/${claudeRun.id}`) : null;
const openBefore = (claudeDetail?.findings ?? []).filter(f => f.state === 'open');
if (!claudeRun || claudeRun.status === 'failed' || openBefore.length === 0) {
  skip('triage', `the claude member ${claudeRun ? `${claudeRun.status} with ${openBefore.length} open findings` : 'is missing'}`);
} else {
  await page.locator(`[data-testid="run-row"][data-run-id="${claudeRun.id}"]`).click();
  await page.locator(`[data-testid="run-detail"][data-run-id="${claudeRun.id}"]`).waitFor({ timeout: 10000 });
  await page.getByTestId('triage-button').click();
  const tpanel = page.getByTestId('triage-button-panel');
  await tpanel.getByTestId('triage-runner-select').selectOption('claude');
  if (MODEL) await tpanel.getByTestId('triage-model-input').fill(MODEL);
  await tpanel.getByTestId('triage-thinking-select').selectOption('low');
  const triageStarted = Date.now();
  const postTriage = page.waitForResponse(r => new URL(r.url()).pathname === `/api/runs/${claudeRun.id}/triage` && r.request().method() === 'POST', { timeout: 15000 });
  await tpanel.getByTestId('triage-start').click();
  const triageResponse = await postTriage.catch(() => null);
  const triageBody = triageResponse ? await triageResponse.json().catch(() => null) : null;
  check('POST /api/runs/{id}/triage starts a triage', triageResponse?.status() === 200 && !!triageBody?.triage?.id, `status ${triageResponse?.status()} ${JSON.stringify(triageBody).slice(0, 300)}`);
  const running = await page.getByTestId('triage-status').getAttribute('data-status').catch(() => '?');
  console.log('TRIAGE STATUS AFTER START:', running);
  const finished = await waitFor(async () => {
    if (wsEvents.some(e => e.type === 'triage.finished' && e.triage?.run_id === claudeRun.id)) return true;
    const t = (await api(`/api/runs/${claudeRun.id}/triage`)).triage;
    return t && t.id === triageBody?.triage?.id && !active(t.status);
  }, TRIAGE_TIMEOUT_MS, 2000);
  const triageSecs = ((Date.now() - triageStarted) / 1000).toFixed(1);
  const result = await api(`/api/runs/${claudeRun.id}/triage`);
  console.log('TRIAGE:', JSON.stringify(result.triage));
  check('triage.finished arrived and the triage is done within 4 minutes', !!finished && result.triage?.status === 'done', `${result.triage?.status} ${result.triage?.error ?? ''} after ${triageSecs}s`);
  check('the triage ran as a fresh run of the "triage" pass', !!result.triage?.triage_run_id && (await api(`/api/runs/${result.triage.triage_run_id}`)).run?.pass === 'triage', result.triage?.triage_run_id ?? 'no run id');
  const tally = { keep: 0, wrong: 0, trivial: 0 };
  for (const f of result.findings) if (f.triage_verdict) tally[f.triage_verdict]++;
  for (const f of result.findings) console.log(`VERDICT ${f.id} ${f.severity} ${f.triage_verdict ?? '-'}: ${String(f.triage_reason ?? '').slice(0, 100)}`);
  const counts = result.triage?.counts ?? {};
  check('the triage counts match the verdicts on the findings', ['keep', 'wrong', 'trivial'].every(v => (counts[v] ?? 0) === tally[v]), `counts ${JSON.stringify(counts)}, findings ${JSON.stringify(tally)}`);
  check('every finding that was open got a verdict', openBefore.every(f => result.findings.find(x => x.id === f.id)?.triage_verdict), `${openBefore.length} open before`);
  // On screen: the status line and the badges on the cards (all states shown, nothing hidden).
  await page.getByTestId('triage-status').and(page.locator('[data-status="done"]')).waitFor({ timeout: 15000 }).catch(() => {});
  await page.getByTestId('run-detail').getByTestId('state-filter-all').click();
  const shownCounts = {};
  for (const v of ['keep', 'wrong', 'trivial']) shownCounts[v] = Number((await page.getByTestId(`triage-count-${v}`).innerText().catch(() => 'NaN')).split(' ')[0]);
  check('the triage status line shows the API counts', ['keep', 'wrong', 'trivial'].every(v => shownCounts[v] === (counts[v] ?? 0)), JSON.stringify(shownCounts));
  const cardVerdicts = await page.getByTestId('run-findings').getByTestId('finding-card').evaluateAll(els => els.map(e => e.getAttribute('data-verdict')));
  const cardTally = { keep: 0, wrong: 0, trivial: 0 };
  for (const v of cardVerdicts) if (v) cardTally[v]++;
  check('the verdict badges on the cards match the API', JSON.stringify(cardTally) === JSON.stringify(tally), `cards ${JSON.stringify(cardTally)} of ${cardVerdicts.length}`);
  await page.screenshot({ path: OUT + '/p6-05-triaged.png' });
  // Hide wrong / trivial, then dismiss them.
  await page.getByTestId('hide-flagged').check();
  const visibleAfterHide = await page.getByTestId('run-findings').getByTestId('finding-card').count();
  check('"hide wrong/trivial" leaves only the other cards', visibleAfterHide === cardVerdicts.length - tally.wrong - tally.trivial, `${visibleAfterHide} cards`);
  await page.getByTestId('hide-flagged').uncheck();
  const flagged = result.findings.filter(f => f.state === 'open' && (f.triage_verdict === 'wrong' || f.triage_verdict === 'trivial'));
  if (flagged.length > 0) {
    await page.getByTestId('dismiss-flagged').click();
    const toast = await page.getByTestId('runs-toast').innerText({ timeout: 8000 }).catch(() => '');
    const after = (await api(`/api/runs/${claudeRun.id}`)).findings;
    const dismissed = flagged.every(f => after.find(x => x.id === f.id)?.state === 'dismissed');
    const keptOpen = after.filter(f => !flagged.some(x => x.id === f.id)).every(f => f.state === (claudeDetail.findings.find(x => x.id === f.id)?.state ?? f.state));
    check('"Dismiss all flagged" dismisses exactly the flagged findings', dismissed && keptOpen && toast.includes(`Dismissed ${flagged.length}`), `toast "${toast}"`);
  } else {
    skip('Dismiss all flagged', 'triage flagged nothing as wrong or trivial');
  }
  await page.screenshot({ path: OUT + '/p6-06-dismissed.png' });
}

// ---- 3. Fan out ---------------------------------------------------------------------------------
await page.getByTestId('pass-select').selectOption(PASS);
await page.getByTestId('scope-paper').check();
await page.getByTestId('runner-select').selectOption('claude');
await page.getByTestId('model-input').fill(FANOUT_MODEL);
await page.getByTestId('thinking-select').selectOption('low');
const fanoutTitle = await page.getByTestId('fanout-button').getAttribute('title').catch(() => null);
check('"Fan out" appears for scope paper and explains itself', !!fanoutTitle && /one run per linked section/.test(fanoutTitle), fanoutTitle ?? 'no button');
const fanoutStarted = Date.now();
const postFanout = page.waitForResponse(r => new URL(r.url()).pathname === '/api/groups/fanout' && r.request().method() === 'POST', { timeout: 15000 });
await page.getByTestId('fanout-button').click();
const fanoutResponse = await postFanout.catch(() => null);
const fanoutBody = fanoutResponse ? await fanoutResponse.json().catch(() => null) : null;
const fanoutId = fanoutBody?.group?.id;
if (!check('POST /api/groups/fanout starts a group', fanoutResponse?.status() === 200 && !!fanoutId, `status ${fanoutResponse?.status()} ${JSON.stringify(fanoutBody).slice(0, 300)}`)) {
  await page.screenshot({ path: OUT + '/p6-fanout-refused.png' }); await finish(browser);
}
const memberIds = new Set(fanoutBody.group.run_ids);
check(`the fan-out has N + 1 = ${linked.length + 1} member runs`, memberIds.size === linked.length + 1, `${memberIds.size} run ids`);
// Sample the process count while it runs (informational).
let maxProcesses = 0;
const sampler = setInterval(() => { maxProcesses = Math.max(maxProcesses, claudeProcesses()); }, 500);
await page.waitForTimeout(6000);
await page.screenshot({ path: OUT + '/p6-07-fanout-running.png' });
const fanout = await waitForGroup(fanoutId, FANOUT_TIMEOUT_MS);
clearInterval(sampler);
const fanoutSecs = ((Date.now() - fanoutStarted) / 1000).toFixed(1);
console.log('GROUP:', JSON.stringify(fanout.group));
for (const r of fanout.runs) console.log(`MEMBER ${r.id} ${r.pass} [${r.scope.kind} ${r.scope.ref ?? ''}] "${r.scope_name}" ${r.runner} ${r.model ?? '-'} ${r.status} findings=${r.findings_count} ${r.error ?? ''}`);
check('the fan-out ended done or partial within 8 minutes', ['done', 'partial'].includes(fanout.group.status), `${fanout.group.status} after ${fanoutSecs}s`);
const sectionMembers = fanout.runs.filter(r => r.pass === PASS && r.scope.kind === 'section');
const driftMembers = fanout.runs.filter(r => r.pass === 'message-drift' && r.scope.kind === 'ends');
check('one section run per linked section plus one message-drift run over "ends"',
  sectionMembers.length === linked.length && driftMembers.length === 1 && linked.every(n => sectionMembers.some(r => r.scope.ref === n.id)),
  `${sectionMembers.length} section runs, ${driftMembers.length} message-drift runs`);
// Concurrency from the run.* events of the members (cached members never start).
let runningNow = 0;
let maxRunning = 0;
for (const e of wsEvents) {
  const id = e.run?.id;
  if (!id || !memberIds.has(id)) continue;
  if (e.type === 'run.started') maxRunning = Math.max(maxRunning, ++runningNow);
  else if (e.type === 'run.finished' || e.type === 'run.failed') runningNow = Math.max(0, runningNow - 1);
}
const started = wsEvents.filter(e => e.type === 'run.started' && memberIds.has(e.run?.id)).length;
if (started > 0) check('at most 3 members ran at the same time', maxRunning <= 3, `max ${maxRunning} at once (${started} started); max "claude -p" processes seen: ${maxProcesses}`);
else skip('concurrency', 'every member came from the cache');
const frow = page.locator(`[data-testid="group-row"][data-group-id="${fanoutId}"]`);
const frowStatus = await waitFor(async () => { const s = await frow.getAttribute('data-status'); return active(s) ? null : s; }, 15000);
const fprogress = (await frow.getByTestId('group-progress').innerText().catch(() => '?')).trim();
check('the fan-out row shows the final status and N+1/N+1 done', frowStatus === fanout.group.status && fprogress === `${memberIds.size}/${memberIds.size} done`, `${frowStatus}, "${fprogress}"`);
await frow.click();
const blocks = page.getByTestId('merged-section');
await waitFor(async () => (await blocks.count()) === fanout.group.run_ids.length, 10000);
const blockInfo = await blocks.evaluateAll(els => els.map(e => ({ section: e.getAttribute('data-section'), run: e.getAttribute('data-run-id'), entries: e.querySelectorAll('[data-testid="merged-entry"]').length })));
const expectedBlocks = fanout.group.run_ids.map(id => {
  const r = fanout.runs.find(x => x.id === id);
  return { section: r?.scope_name ?? id, run: id, entries: fanout.merged.filter(e => e.findings[0]?.run_id === id).length };
});
for (const b of blockInfo) console.log(`BLOCK ${b.run} "${b.section}" ${b.entries} entries`);
check('the merged view shows one block per member, titled by its section, with its findings',
  JSON.stringify(blockInfo) === JSON.stringify(expectedBlocks), `screen ${JSON.stringify(blockInfo)} vs api ${JSON.stringify(expectedBlocks)}`);
check('a fan-out shows no agreement badges', (await page.getByTestId('agreement-badge').count()) === 0);
await page.screenshot({ path: OUT + '/p6-08-fanout-done.png', fullPage: true });

await finish(browser);
