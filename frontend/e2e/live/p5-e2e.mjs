// Drives the REAL backend (serving frontend/dist) through phase 5: the git panel
// (status, diff, commit, push), "flag major revision", a blind A/B compare of p-3
// against snapshot v1 (verdict, then Reveal), the coverage matrix after a claim is
// linked with a cell click, and the editor's selection menu (Lift to claim).
//
//   node e2e/live/p5-e2e.mjs <repo> <screenshot dir> [--runner claude|codex] [--model sonnet]
//
// Assumes `rewrite serve --repo <repo> --port 8765` is running (REWRITE_URL
// overrides http://127.0.0.1:8765) with a current
// `npm run build` in frontend/dist, on a repo prepared like this:
//   - a git repo whose branch tracks a bare `origin` (so push works),
//   - examples/sample-paper initialised (`rewrite init`), the Introduction
//     section adopted with its paragraphs (p-1 ... in sections/intro.tex),
//   - one commit, then one snapshot with the note "v1" (rewrite/rev-1-v1),
//   - a reworded sentence in paragraph p-3, left uncommitted in the working tree.
// The compare uses thinking "low" and waits up to 3 minutes for the model. It
// commits and pushes the working tree and adds a tag, so run it on a scratch
// repo. Prints one PASS/FAIL line per check and exits non-zero if any check failed.
import { chromium } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';

const args = process.argv.slice(2);
const flag = (name) => { const i = args.indexOf(name); if (i < 0) return null; const v = args[i + 1]; args.splice(i, 2); return v ?? null; };
const RUNNER = flag('--runner');
const MODEL = flag('--model');
const [REPO, OUT] = args;
const BASE = process.env.REWRITE_URL ?? 'http://127.0.0.1:8765';
const FILE = 'sections/intro.tex';
const NODE = 'p-3';
const COMMIT_MESSAGE = 'p5-e2e: reword p-3';
const COMPARE_TIMEOUT_MS = 3 * 60_000;
const CLAIM_SAYS = 'p5-e2e coverage claim';
if (!REPO || !OUT || (RUNNER && !['claude', 'codex'].includes(RUNNER))) {
  console.log('usage: node e2e/live/p5-e2e.mjs <repo> <screenshot dir> [--runner claude|codex] [--model sonnet]');
  process.exit(2);
}
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
const api = async (path) => (await fetch(BASE + path)).json();
const git = (...a) => { try { return execFileSync('git', ['-C', REPO, ...a], { encoding: 'utf8' }).trim(); } catch (e) { return `ERROR ${e.message.split('\n')[0]}`; } };
const readSpec = () => JSON.parse(fs.readFileSync(`${REPO}/paper.json`, 'utf8'));
/** Collapse whitespace, as the selection menu does. */
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

// ---- The prepared repo, as found -------------------------------------------------
console.log('GIT STATUS:', JSON.stringify(git('status', '--porcelain=v1', '--branch')));
console.log('GIT TAGS:', JSON.stringify(git('tag', '-l', 'rewrite/*')));
console.log('GIT REMOTES:', JSON.stringify(git('remote', '-v')));
const startTags = git('tag', '-l', 'rewrite/*').split('\n').filter(Boolean);
const v1Tag = startTags.find(t => /^rewrite\/rev-\d+-v1$/.test(t)) ?? null;
const nextN = 1 + Math.max(0, ...startTags.map(t => Number(/^rewrite\/rev-(\d+)-/.exec(t)?.[1] ?? 0)));
const v2Tag = `rewrite/rev-${nextN}-v2`;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const errors = [];
page.on('pageerror', e => errors.push('pageerror: ' + e.message));
page.on('console', m => { if (m.type() === 'error' && !/404 \(Not Found\)/.test(m.text())) errors.push('console: ' + m.text()); });
// Every phase 5 write with its status, to compare against the API contract.
page.on('response', async r => {
  const u = new URL(r.url());
  if (!/^\/api\/(git|snapshots|compare|spec\/ops|spec\/adopt)/.test(u.pathname) || r.request().method() === 'GET') return;
  console.log('API', r.request().method(), u.pathname, (r.request().postData() ?? '').slice(0, 200), '->', r.status());
});
// WebSocket frames: git.*, compare.*, run.* events.
const wsEvents = [];
page.on('websocket', ws => ws.on('framereceived', f => {
  try { const e = JSON.parse(f.payload); if (/^(git|compare|run)\./.test(e.type)) wsEvents.push(e); } catch { /* not JSON */ }
}));

await page.goto(BASE);
await page.locator('[data-testid=file-tree] [data-path="main.tex"]').waitFor({ timeout: 10000 });

const status0 = await api('/api/spec/status');
if (!check(`${NODE} is an anchored paragraph`, ['linked', 'stale'].includes(status0.nodes?.[NODE]?.status), JSON.stringify(status0.nodes?.[NODE]))) {
  await finish(browser);
}
check('the repo has a v1 snapshot tag', !!v1Tag, `${v1Tag}; all: ${startTags.join(', ')}`);

// ---- Git panel: status and diff ---------------------------------------------------
await page.getByTestId('git-button').click();
const introRow = page.locator(`[data-testid="git-file"][data-path="${FILE}"]`);
const listed = await introRow.waitFor({ timeout: 10000 }).then(() => true, () => false);
const gitStatus = await api('/api/git/status');
console.log('STATUS:', JSON.stringify({ ...gitStatus, files: gitStatus.files?.map(f => `${f.status} ${f.path}`) }));
check(`the git panel lists ${FILE} as modified`, listed && (await introRow.getAttribute('data-status')) === 'M',
  `${await page.getByTestId('git-file').count()} files; branch ${await page.getByTestId('git-branch').innerText().catch(() => '?')}`);
await introRow.getByTestId('git-file-open').click();
const diff = page.getByTestId('git-diff');
await diff.waitFor({ timeout: 8000 }).catch(() => {});
const adds = await diff.locator('[data-kind="add"]').count();
const dels = await diff.locator('[data-kind="del"]').count();
check('the diff shows the reworded lines (+ and -)', adds > 0 && dels > 0, `${adds} added, ${dels} removed`);
await page.screenshot({ path: OUT + '/p5-01-git-status.png' });

// ---- Commit ------------------------------------------------------------------------
await page.getByTestId('git-message').fill(COMMIT_MESSAGE);
await page.getByTestId('git-commit').click();
const committed = await page.getByTestId('git-commit-result').filter({ hasText: 'Committed' }).waitFor({ timeout: 20000 }).then(() => true, () => false);
const subject = git('log', '-1', '--format=%s');
check('Commit via the panel: `git log -1` shows it', committed && subject === COMMIT_MESSAGE,
  `subject "${subject}"; result "${await page.getByTestId('git-commit-result').innerText().catch(() => '?')}"`);
const porcelain = git('status', '--porcelain=v1');
const goneFromList = await waitFor(async () => (await introRow.count()) === 0, 8000);
check(`${FILE} is committed and gone from the list`, !porcelain.includes(FILE) && !!goneFromList, `porcelain: ${JSON.stringify(porcelain)}`);
await page.screenshot({ path: OUT + '/p5-02-committed.png' });

// ---- Push ----------------------------------------------------------------------------
await page.getByTestId('git-push').click();
const pushed = await waitFor(() => wsEvents.find(e => e.type === 'git.finished' && e.op === 'push'), 120000, 500);
const pushLines = wsEvents.filter(e => e.type === 'git.output' && e.op === 'push').map(e => e.line);
console.log('PUSH OUTPUT:', JSON.stringify(pushLines));
check('push streams git.output and ends with git.finished ok', !!pushed?.ok, `${pushLines.length} lines; ${JSON.stringify(pushed)}`);
const uiResult = await page.getByTestId('git-remote-result').innerText().catch(() => '?');
check('the panel shows "git push: ok"', uiResult === 'git push: ok', uiResult);
const head = git('rev-parse', 'HEAD');
const upstream = git('rev-parse', '@{u}');
check('HEAD equals its upstream after the push', head === upstream, `${head.slice(0, 7)} vs ${upstream.slice(0, 7)}`);
await page.screenshot({ path: OUT + '/p5-03-pushed.png' });

// ---- Flag major revision "v2" ----------------------------------------------------------
await page.getByTestId('git-flag').click();
await page.getByTestId('git-flag-note').fill('v2');
await page.getByTestId('git-flag-submit').click();
const tagShown = await page.locator(`[data-testid="git-snapshot"][data-tag="${v2Tag}"]`).waitFor({ timeout: 20000 }).then(() => true, () => false);
const tagsNow = git('tag', '-l', 'rewrite/*').split('\n').filter(Boolean);
check(`Flag major revision "v2" creates tag ${v2Tag}`, tagShown && tagsNow.includes(v2Tag), `tags: ${tagsNow.join(', ')}; ${await page.getByTestId('git-snapshot-result').innerText().catch(() => '?')}`);
console.log('SNAPSHOTS:', JSON.stringify((await api('/api/snapshots')).snapshots));
await page.screenshot({ path: OUT + '/p5-04-snapshot.png' });
await page.getByTestId('git-close').click();

// ---- Blind A/B compare of p-3 against v1 ------------------------------------------------
await page.getByTestId('tab-spec').click();
const p3Row = page.locator(`[data-testid="spec-row"][data-node-id="${NODE}"]`);
await p3Row.waitFor({ timeout: 10000 });
await p3Row.getByTestId('row-menu-button').click();
const compareItem = await page.getByTestId('menu-compare').waitFor({ timeout: 5000 }).then(() => true, () => false);
if (!check(`${NODE}'s ⋯ menu offers Compare…`, compareItem)) { await page.screenshot({ path: OUT + '/p5-no-compare.png' }); await finish(browser); }
await page.getByTestId('menu-compare').click();
const dialog = page.getByTestId('compare-dialog');
await dialog.waitFor({ timeout: 5000 });
const beforeSelect = dialog.getByTestId('compare-before-select');
const v1Value = `snapshot:${v1Tag}`;
await waitFor(async () => (await beforeSelect.locator(`option[value="${v1Value}"]`).count()) > 0, 8000);
await beforeSelect.selectOption(v1Value).catch(() => {});
check('the "before" picker offers snapshot v1 (and selects it)', (await beforeSelect.inputValue()) === v1Value, await beforeSelect.inputValue());
if (RUNNER) await dialog.getByTestId('compare-runner-select').selectOption(RUNNER);
if (MODEL) await dialog.getByTestId('compare-model-input').fill(MODEL);
await dialog.getByTestId('compare-thinking-select').selectOption('low');
await page.screenshot({ path: OUT + '/p5-05-compare-form.png' });

const started = Date.now();
const postCompare = page.waitForResponse(r => new URL(r.url()).pathname === '/api/compare' && r.request().method() === 'POST', { timeout: 20000 });
await dialog.getByTestId('compare-start').click();
const postResponse = await postCompare.catch(() => null);
const posted = postResponse ? await postResponse.json().catch(() => null) : null;
const comparisonId = posted?.comparison?.id;
if (!check('POST /api/compare starts a comparison', postResponse?.status() === 200 && !!comparisonId, `status ${postResponse?.status()} ${JSON.stringify(posted).slice(0, 300)}`)) {
  await page.screenshot({ path: OUT + '/p5-compare-refused.png' }); await finish(browser);
}
await page.waitForTimeout(3000);
await page.screenshot({ path: OUT + '/p5-06-compare-running.png' });
const statusBadge = dialog.getByTestId('compare-status');
const ended = await waitFor(async () => {
  const s = await statusBadge.getAttribute('data-status');
  return s === 'done' || s === 'failed' ? s : null;
}, COMPARE_TIMEOUT_MS, 1000);
const elapsed = ((Date.now() - started) / 1000).toFixed(1);
const blindDetail = await api(`/api/compare/${comparisonId}`);
console.log('COMPARISON:', JSON.stringify({ ...blindDetail.comparison }), 'RUN:', JSON.stringify({ ...blindDetail.run, scope: undefined }));
check(`the comparison ended "done" within 3 minutes`, ended === 'done', `${ended} after ${elapsed}s; error ${blindDetail.comparison?.error ?? '-'}`);
check('compare.finished arrived over the WebSocket', wsEvents.some(e => e.type === 'compare.finished' && e.comparison?.id === comparisonId));
if (ended !== 'done') { await page.screenshot({ path: OUT + '/p5-compare-failed.png' }); await finish(browser); }
const verdictText = await dialog.getByTestId('compare-verdict').innerText();
const reason = await dialog.getByTestId('compare-reason').innerText().catch(() => '');
check('the verdict and its reason are shown', /^(Version [AB] reads better|Tie: neither version reads better)$/.test(verdictText) && reason.length > 0, `"${verdictText}" — "${reason.slice(0, 160)}"`);
check('before Reveal: no texts on screen, and the API keeps the mapping hidden',
  (await dialog.getByTestId('compare-version').count()) === 0 && blindDetail.comparison.mapping === null);
check('before and after texts differ (the reworded sentence)', squash(blindDetail.before_text) !== squash(blindDetail.after_text),
  `before ${blindDetail.before_text.length} chars, after ${blindDetail.after_text.length} chars`);
await page.screenshot({ path: OUT + '/p5-07-verdict.png' });

await dialog.getByTestId('compare-reveal').click();
await dialog.getByTestId('compare-version').first().waitFor({ timeout: 10000 }).catch(() => {});
const labels = await dialog.getByTestId('compare-version-label').allInnerTexts();
check('Reveal labels the versions before (snapshot v1) / after (working tree)',
  labels.length === 2 && labels.includes(`before (snapshot ${v1Tag})`) && labels.includes('after (working tree)'), JSON.stringify(labels));
const revealed = await api(`/api/compare/${comparisonId}?reveal=true`);
const sideA = await dialog.locator('[data-testid="compare-version"][data-letter="A"]').getAttribute('data-side');
check('the UI mapping matches GET ?reveal=true', revealed.comparison.mapping?.A === sideA, `${JSON.stringify(revealed.comparison.mapping)}; UI A = ${sideA}`);
console.log('OUTCOME:', await dialog.getByTestId('compare-outcome').innerText().catch(() => '?'));
await page.screenshot({ path: OUT + '/p5-08-revealed.png' });
await dialog.getByTestId('compare-close').click();

// ---- Coverage: "+ claim", then link it with a cell click ------------------------------------
const intro = readSpec().tree.children.find(s => /Introduction/.test(s.anchor?.marker ?? ''));
if (!check('paper.json has the Introduction section', !!intro, intro?.id)) await finish(browser);
const addClaim = page.waitForResponse(r => new URL(r.url()).pathname === '/api/spec/ops' && (r.request().postData() ?? '').includes('add_claim'), { timeout: 10000 });
await page.getByTestId('add-claim').click();
const claimId = (await (await addClaim.catch(() => null))?.json().catch(() => null))?.new_id;
if (!check('"+ claim" adds a claim', !!claimId, claimId)) await finish(browser);
// The new card's "says" box has the focus.
await page.locator(`[data-testid="claim-card"][data-claim-id="${claimId}"] [data-testid="claim-says"]`).fill(CLAIM_SAYS);
await waitFor(() => readSpec().claims.find(c => c.id === claimId)?.says === CLAIM_SAYS, 8000);
const coverageCell = page.locator(`[data-testid="coverage-cell"][data-claim-id="${claimId}"][data-section-id="${intro.id}"]`);
const cellShown = await coverageCell.waitFor({ timeout: 10000 }).then(() => true, () => false);
check('the coverage matrix has a row for the new claim (unreferenced)', cellShown &&
  (await page.locator(`[data-testid="coverage-row"][data-claim-id="${claimId}"]`).getAttribute('data-unreferenced')) === 'true');
await coverageCell.click();
const linkedCount = await waitFor(async () => (await coverageCell.getAttribute('data-count')) === '1', 8000);
const onDisk = readSpec().tree.children.find(s => s.id === intro.id)?.claims ?? [];
const cov = await api('/api/spec/coverage');
check('a cell click links the claim to the section (matrix, paper.json, API)', !!linkedCount && onDisk.includes(claimId) && cov.cells?.[claimId]?.[intro.id] === 1,
  `cell ${await coverageCell.getAttribute('data-count')}; ${intro.id}.claims ${JSON.stringify(onDisk)}; api ${cov.cells?.[claimId]?.[intro.id]}`);
await page.screenshot({ path: OUT + '/p5-09-coverage.png' });

// ---- Selection menu: Lift to claim on a line of p-3 -----------------------------------------
const markerLine = (await api('/api/spec/status')).nodes?.[NODE]?.line;
await page.getByTestId('tab-editor').click();
await page.locator(`[data-testid=file-tree] [data-path="${FILE}"]`).click();
await page.getByTestId('open-file-name').filter({ hasText: FILE }).waitFor({ timeout: 8000 });
// The first prose line under the marker (CodeMirror renders every line of this short file).
const line = page.locator('.cm-content .cm-line').nth(markerLine);
const box = await line.boundingBox();
// Click on the FIRST visual row (long lines wrap), then select that visual row.
// Home/End move within the visual row in CodeMirror, so the expected text is
// whatever the browser actually selected, not the whole logical line.
await page.mouse.click(box.x + 20, box.y + 8);
await page.keyboard.press('Home');
await page.keyboard.press('Shift+End');
const lineText = squash(await page.evaluate(() => window.getSelection()?.toString() ?? '')) || squash(await line.innerText());
await page.mouse.click(box.x + 20, box.y + 8, { button: 'right' });
const menu = page.getByTestId('editor-context-menu');
const menuShown = await menu.waitFor({ timeout: 5000 }).then(() => true, () => false);
check(`right-click on a selection in ${NODE} opens the menu for ${NODE}`, menuShown && (await menu.getByTestId('ctx-node').innerText()) === `in ${NODE}`,
  `marker line ${markerLine}; selected "${lineText.slice(0, 80)}"`);
await page.screenshot({ path: OUT + '/p5-10-context-menu.png' });
const claimsBefore = readSpec().claims.map(c => c.id);
await menu.getByTestId('ctx-lift-claim').click();
const toast = await page.getByTestId('spec-toast').filter({ hasText: 'Lifted to claim' }).waitFor({ timeout: 10000 }).then(() => true, () => false);
const lifted = await waitFor(() => readSpec().claims.find(c => !claimsBefore.includes(c.id)), 8000);
const p3 = readSpec().tree.children.flatMap(s => s.children).find(p => p.id === NODE);
check('Lift to claim adds the selection as a claim linked to the paragraph', toast && squash(lifted?.says) === lineText && !!p3?.claims.includes(lifted?.id),
  `claim ${lifted?.id} "${squash(lifted?.says).slice(0, 80)}"; ${NODE}.claims ${JSON.stringify(p3?.claims)}`);
await page.getByTestId('tab-spec').click();
const card = await page.locator(`[data-testid="claim-card"][data-claim-id="${lifted?.id}"]`).waitFor({ timeout: 5000 }).then(() => true, () => false);
check('the claim bank shows the lifted claim', card);
await page.screenshot({ path: OUT + '/p5-11-lifted.png' });

await finish(browser);
