// Headless tests of the phase 5 UI against the mock backend: the git panel,
// "flag major revision" snapshots, the blind A/B compare dialog, the coverage
// matrix and the editor's selection menu.
//
// The mock's fake git repo (mock/git.mjs) starts with HEAD older than the
// files: sections/intro.tex line 2 and sections/method.tex line 4 differ
// ("M"), and notes/todo.md is untracked ("??"). Branch main tracks
// origin/main and is 1 commit ahead. Push / pull stream three `git.output`
// lines, then `git.finished`. Comparisons (mock/compare.mjs) go queued ->
// running -> done in ~1.5 s, prefer the working tree by default, and reveal
// their A/B mapping only with ?reveal=true.
//
// GET /__mock/p5-log lists every write to /api/git*, /api/snapshots and
// /api/compare; GET /__mock/spec-log every spec write (for "one op" checks).
import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test'
import { MOCK_URL } from '../playwright.config.ts'

// ---------------------------------------------------------------------------
// Spec builders and mock helpers
// ---------------------------------------------------------------------------

type Level = 'message' | 'claim' | 'section' | 'paragraph'
const FIELDS: Record<Level, string[]> = {
  message: ['problem', 'stakes', 'thesis'],
  claim: ['evidence', 'scope'],
  section: ['figure_story'],
  paragraph: ['function'],
}

interface TestNode {
  id: string
  level: Level
  says: string
  children: TestNode[]
  claims: string[]
  fields: Record<string, string>
  anchor: { file: string; marker: string } | null
  notes: string
}

function node(id: string, level: Level, says = '', extra: Partial<TestNode> = {}): TestNode {
  const fields = Object.fromEntries(FIELDS[level].map((k) => [k, '']))
  return { id, level, says, children: [], claims: [], anchor: null, notes: '', ...extra, fields: { ...fields, ...extra.fields } }
}

function paperSpec(sections: TestNode[], claims: TestNode[] = []) {
  return {
    version: 1,
    message: node('message', 'message'),
    claims,
    tree: { ...node('message', 'message'), fields: {}, children: sections },
  }
}

const INTRO_FILE = 'sections/intro.tex'
const introSection = (extra: Partial<TestNode> = {}) =>
  node('introduction', 'section', 'Why mocks', { anchor: { file: INTRO_FILE, marker: '\\section{Introduction}' }, ...extra })
const p1 = (extra: Partial<TestNode> = {}) => node('p-1', 'paragraph', '', { anchor: { file: INTRO_FILE, marker: '@p p-1' }, ...extra })

/** intro.tex with the `% @p p-1` marker on line 2 above the paragraph. */
const INTRO_WITH_MARKER = [
  '\\section{Introduction}',
  '% @p p-1',
  'Mock papers are useful for testing user interfaces.',
  'This sentence cites a missing key~\\cite{nobody}.',
  '',
].join('\n')
const FIRST_SENTENCE = 'Mock papers are useful for testing user interfaces.'

async function seedSpec(request: APIRequestContext, spec: unknown) {
  await request.post(`${MOCK_URL}/__mock/spec-raw`, { data: { raw: JSON.stringify(spec, null, 2) + '\n' } })
}

async function putTex(request: APIRequestContext, path: string, content: string) {
  await request.put(`${MOCK_URL}/api/file`, { data: { path, content } })
}

interface LogEntry {
  method: string
  path: string
  body: Record<string, unknown> & { op?: { type: string; [key: string]: unknown } }
}

async function p5Log(request: APIRequestContext): Promise<LogEntry[]> {
  return (await (await request.get(`${MOCK_URL}/__mock/p5-log`)).json()) as LogEntry[]
}

async function specLog(request: APIRequestContext): Promise<LogEntry[]> {
  return (await (await request.get(`${MOCK_URL}/__mock/spec-log`)).json()) as LogEntry[]
}

/** The ops sent to POST /api/spec/ops, optionally only one type. */
async function ops(request: APIRequestContext, type?: string) {
  return (await specLog(request)).filter((e) => e.path === '/api/spec/ops' && (!type || e.body.op?.type === type)).map((e) => e.body.op!)
}

async function getSpec(request: APIRequestContext) {
  return (await (await request.get(`${MOCK_URL}/api/spec`)).json()) as { spec: ReturnType<typeof paperSpec> }
}

/** Console errors other than expected 404s (no PDF before the first compile). */
function collectConsoleErrors(page: Page): string[] {
  const errors: string[] = []
  page.on('console', (msg) => {
    if (msg.type() === 'error' && !/404 \(Not Found\)/.test(msg.text())) errors.push(msg.text())
  })
  page.on('pageerror', (err) => errors.push(err.message))
  return errors
}

// ---------------------------------------------------------------------------
// Page helpers
// ---------------------------------------------------------------------------

async function openApp(page: Page) {
  await page.goto('/')
  await expect(page.getByTestId('open-file-name')).toHaveText('main.tex')
}

async function openGitPanel(page: Page) {
  await page.getByTestId('git-button').click()
  await expect(page.getByTestId('git-panel')).toBeVisible()
  await expect(page.getByTestId('git-branch')).toBeVisible()
}

const gitFiles = (page: Page) => page.getByTestId('git-file')
const gitFile = (page: Page, path: string) => page.locator(`[data-testid="git-file"][data-path="${path}"]`)
const row = (page: Page, id: string) => page.locator(`[data-testid="spec-row"][data-node-id="${id}"]`)
const cell = (page: Page, claim: string, section: string) =>
  page.locator(`[data-testid="coverage-cell"][data-claim-id="${claim}"][data-section-id="${section}"]`)
const menu = (page: Page) => page.getByTestId('editor-context-menu')

/** Open a file from the tree in the Editor tab. */
async function openFile(page: Page, path: string) {
  await page.getByTestId('tab-editor').click()
  await page.locator(`[data-testid="file-tree"] [data-path="${path}"]`).click()
  await expect(page.getByTestId('open-file-name')).toHaveText(path)
}

/** Select the whole text of the editor line that contains `text` (Home, Shift-End). Returns the line. */
async function selectLine(page: Page, text: string): Promise<Locator> {
  const line = page.locator('.cm-content .cm-line', { hasText: text }).first()
  const box = await line.boundingBox()
  expect(box, 'line should be on screen').not.toBeNull()
  await page.mouse.click(box!.x + 20, box!.y + box!.height / 2)
  await page.keyboard.press('Home')
  await page.keyboard.press('Shift+End')
  return line
}

/** Right-click on the start of a line (inside the selection). */
async function rightClickLine(page: Page, line: Locator) {
  const box = await line.boundingBox()
  await page.mouse.click(box!.x + 20, box!.y + box!.height / 2, { button: 'right' })
}

test.beforeEach(async ({ request }) => {
  await request.post(`${MOCK_URL}/__mock/reset`)
})

// ---------------------------------------------------------------------------
// Git panel
// ---------------------------------------------------------------------------

test('git panel shows the branch, the changed files and a colored diff', async ({ page }) => {
  const errors = collectConsoleErrors(page)
  await openApp(page)
  await expect(page.getByTestId('git-badge')).toHaveText('3')
  await openGitPanel(page)

  await expect(page.getByTestId('git-branch')).toContainText('main')
  await expect(page.getByTestId('git-branch')).toContainText('origin/main')
  await expect(page.getByTestId('git-ahead')).toHaveText('↑1')
  await expect(gitFiles(page)).toHaveCount(3)
  expect(await gitFiles(page).evaluateAll((els) => els.map((e) => `${e.getAttribute('data-status')} ${e.getAttribute('data-path')}`))).toEqual([
    '?? notes/todo.md',
    'M sections/intro.tex',
    'M sections/method.tex',
  ])
  // Every file starts ticked.
  for (const box of await page.getByTestId('git-file-check').all()) await expect(box).toBeChecked()

  // The editor stays mounted (and visible on the left) under the drawer.
  await expect(page.locator('.cm-content')).toBeVisible()

  // Diff of a modified file: - old line in red, + new line in green, @@ hunk header.
  await gitFile(page, INTRO_FILE).getByTestId('git-file-open').click()
  const diff = page.getByTestId('git-diff')
  await expect(diff.locator('[data-kind="del"]')).toHaveText(['-Mock papers help when testing interfaces.'])
  await expect(diff.locator('[data-kind="add"]')).toHaveText([`+${FIRST_SENTENCE}`])
  await expect(diff.locator('[data-kind="hunk"]')).toHaveCount(1)
  const addColor = await diff.locator('[data-kind="add"]').evaluate((el) => getComputedStyle(el).backgroundColor)
  const delColor = await diff.locator('[data-kind="del"]').evaluate((el) => getComputedStyle(el).backgroundColor)
  expect(addColor).not.toBe(delColor)
  expect(addColor).not.toBe('rgba(0, 0, 0, 0)')

  // An untracked file diffs as a full add.
  await gitFile(page, 'notes/todo.md').getByTestId('git-file-open').click()
  await expect(diff).toContainText('new file mode')
  await expect(diff.locator('[data-kind="add"]').first()).toHaveText('+# To do')
  await expect(diff.locator('[data-kind="del"]')).toHaveCount(0)

  // Escape (focus in the drawer) closes it.
  await page.getByTestId('git-panel').focus()
  await page.keyboard.press('Escape')
  await expect(page.getByTestId('git-panel')).toHaveCount(0)
  expect(errors).toEqual([])
})

test('Commit sends the ticked files, clears them from the list and adds a log entry', async ({ page, request }) => {
  await openApp(page)
  await openGitPanel(page)
  await expect(gitFiles(page)).toHaveCount(3)

  // Leave the untracked file out of the commit.
  await gitFile(page, 'notes/todo.md').getByTestId('git-file-check').uncheck()
  await expect(page.getByTestId('git-commit')).toHaveText(/Commit 2 of 3 files/)
  await expect(page.getByTestId('git-commit')).toBeDisabled() // no message yet
  await page.getByTestId('git-message').fill('Tighten the introduction')
  await page.getByTestId('git-commit').click()

  await expect(gitFiles(page)).toHaveCount(1)
  await expect(gitFile(page, 'notes/todo.md')).toBeVisible()
  await expect(page.getByTestId('git-commit-result')).toContainText('Tighten the introduction')
  await expect(page.getByTestId('git-message')).toHaveValue('')
  await expect(page.getByTestId('git-commit-subject').first()).toHaveText('Tighten the introduction')
  await expect(page.getByTestId('git-ahead')).toHaveText('↑2')
  await expect(page.getByTestId('git-badge')).toHaveText('1')

  const commits = (await p5Log(request)).filter((e) => e.path === '/api/git/commit')
  expect(commits.map((e) => e.body)).toEqual([{ message: 'Tighten the introduction', paths: ['sections/intro.tex', 'sections/method.tex'] }])

  // An un-ticked file stays un-ticked while it is still changed; "all" ticks it.
  await expect(gitFile(page, 'notes/todo.md').getByTestId('git-file-check')).not.toBeChecked()
  await page.getByTestId('git-check-all').click()
  // Commit the rest with Ctrl-Enter: the tree is clean.
  await page.getByTestId('git-message').fill('Add notes')
  await page.getByTestId('git-message').press('ControlOrMeta+Enter')
  await expect(page.getByTestId('git-clean')).toBeVisible()
  await expect(page.getByTestId('git-badge')).toHaveText('0')
  await expect(page.getByTestId('git-commit-subject').first()).toHaveText('Add notes')
})

test('Push streams git output lines, then ok; a failing pull shows its error', async ({ page, request }) => {
  const errors = collectConsoleErrors(page)
  await openApp(page)
  await openGitPanel(page)

  await page.getByTestId('git-push').click()
  const log = page.getByTestId('git-remote-log')
  await expect(log).toContainText('Enumerating objects')
  await expect(page.getByTestId('git-remote-result')).toHaveText('git push: ok')
  await expect(page.getByTestId('git-remote-result')).toHaveAttribute('data-status', 'done')
  await expect(log).toHaveText(
    ['Enumerating objects: 7, done.', 'Writing objects: 100% (4/4), 512 bytes | 512.00 KiB/s, done.', 'To /tmp/mock-remote.git'].join('\n'),
  )
  await expect(page.getByTestId('git-ahead')).toHaveText('↑0') // status re-read after git.finished

  await request.post(`${MOCK_URL}/__mock/git-config`, { data: { pull_fails: true } })
  await page.getByTestId('git-pull').click()
  await expect(page.getByTestId('git-remote-result')).toHaveAttribute('data-status', 'failed')
  await expect(page.getByTestId('git-remote-result')).toContainText('Could not read from remote repository.')
  await expect(log).toContainText('From /tmp/mock-remote.git')
  expect((await p5Log(request)).map((e) => e.path)).toEqual(['/api/git/push', '/api/git/pull'])
  expect(errors).toEqual([])
})

test('Flag major revision commits the dirty tree and tags rewrite/rev-1-…', async ({ page, request }) => {
  await openApp(page)
  await openGitPanel(page)
  await expect(page.getByTestId('git-snapshot')).toHaveCount(0)

  await page.getByTestId('git-flag').click()
  await page.getByTestId('git-flag-note').fill('First full draft')
  await page.getByTestId('git-flag-submit').click()

  const snapshot = page.locator('[data-testid="git-snapshot"][data-tag="rewrite/rev-1-first-full-draft"]')
  await expect(snapshot).toBeVisible()
  await expect(snapshot).toContainText('First full draft')
  await expect(page.getByTestId('git-snapshot-result')).toContainText('rewrite/rev-1-first-full-draft')
  // The dirty tree was committed first, and the tag sits on that commit.
  await expect(page.getByTestId('git-clean')).toBeVisible()
  const head = page.getByTestId('git-commit-row').first()
  await expect(head.getByTestId('git-commit-subject')).toHaveText('Snapshot: First full draft')
  await expect(head).toContainText('rewrite/rev-1-first-full-draft')

  // A second revision on a clean tree: tag only, numbered 2.
  await page.getByTestId('git-flag').click()
  await page.getByTestId('git-flag-note').fill('v2')
  await page.getByTestId('git-flag-submit').click()
  await expect(page.locator('[data-testid="git-snapshot"][data-tag="rewrite/rev-2-v2"]')).toBeVisible()
  await expect(page.getByTestId('git-snapshot').first()).toHaveAttribute('data-tag', 'rewrite/rev-2-v2') // newest first
  expect((await p5Log(request)).filter((e) => e.path === '/api/snapshots').map((e) => e.body)).toEqual([{ note: 'First full draft' }, { note: 'v2' }])
})

// ---------------------------------------------------------------------------
// Blind A/B compare
// ---------------------------------------------------------------------------

/** p-1 anchored in intro.tex, snapshot "v1" of it, then a reworded working tree. */
async function seedCompare(request: APIRequestContext) {
  await seedSpec(request, paperSpec([introSection({ children: [p1({ says: 'Mocks make UI tests cheap' })] })]))
  await putTex(request, INTRO_FILE, INTRO_WITH_MARKER)
  await request.post(`${MOCK_URL}/api/snapshots`, { data: { note: 'v1' } })
  await putTex(request, INTRO_FILE, INTRO_WITH_MARKER.replace(FIRST_SENTENCE, 'Mock papers make interface tests fast.'))
}

async function openCompareFromOutliner(page: Page) {
  await page.getByTestId('tab-spec').click()
  await expect(row(page, 'p-1').getByTestId('status-dot')).toHaveAttribute('data-status', 'linked')
  await row(page, 'p-1').getByTestId('row-menu-button').click()
  await page.getByTestId('menu-compare').click()
  await expect(page.getByTestId('compare-dialog')).toBeVisible()
}

test('Compare from the outliner: verdict first, Reveal then labels before / after', async ({ page, request }) => {
  const errors = collectConsoleErrors(page)
  await seedCompare(request)
  await openApp(page)
  await openCompareFromOutliner(page)

  const dialog = page.getByTestId('compare-dialog')
  await expect(dialog.getByTestId('compare-node')).toHaveText('p-1')
  await expect(dialog.getByTestId('compare-file')).toHaveText(INTRO_FILE)
  await expect(dialog.getByTestId('compare-says')).toContainText('Mocks make UI tests cheap')
  // "before" defaults to the newest snapshot; recent commits are offered too.
  const before = dialog.getByTestId('compare-before-select')
  await expect(before).toHaveValue('snapshot:rewrite/rev-1-v1')
  expect(await before.locator('optgroup[label="Recent commits"] option').count()).toBeGreaterThan(0)
  await dialog.getByTestId('compare-thinking-select').selectOption('low')
  await dialog.getByTestId('compare-start').click()

  // Status line with a spinner while it runs, then done.
  const status = dialog.getByTestId('compare-status')
  await expect(status).toHaveAttribute('data-status', /queued|running/)
  await expect(dialog.getByTestId('compare-cancel')).toBeVisible()
  await expect(status).toHaveAttribute('data-status', 'done', { timeout: 10_000 })

  // Verdict and reason only: no texts, no labels until Reveal.
  await expect(dialog.getByTestId('compare-verdict')).toHaveText(/^Version [AB] reads better$/)
  await expect(dialog.getByTestId('compare-reason')).toContainText('local message')
  await expect(dialog.getByTestId('compare-version')).toHaveCount(0)
  await expect(dialog).not.toContainText('working tree')
  await expect(dialog).not.toContainText('Mock papers make interface tests fast.')

  const posted = (await p5Log(request)).filter((e) => e.path === '/api/compare')
  expect(posted.map((e) => e.body)).toEqual([
    { file: INTRO_FILE, node_id: 'p-1', before: { kind: 'snapshot', ref: 'rewrite/rev-1-v1' }, thinking: 'low' },
  ])

  await dialog.getByTestId('compare-reveal').click()
  await expect(dialog.getByTestId('compare-version')).toHaveCount(2)
  const labels = await dialog.getByTestId('compare-version-label').allInnerTexts()
  expect(labels.sort()).toEqual(['after (working tree)', 'before (snapshot rewrite/rev-1-v1)'])
  // The mock prefers the working tree: the current text won.
  await expect(dialog.getByTestId('compare-outcome')).toHaveAttribute('data-winner', 'after')
  await expect(dialog.getByTestId('compare-outcome')).toContainText('The current text won')
  const after = dialog.locator('[data-testid="compare-version"][data-side="after"]')
  await expect(after).toContainText('preferred')
  await expect(after.getByTestId('compare-version-text')).toContainText('Mock papers make interface tests fast.')
  await expect(dialog.locator('[data-testid="compare-version"][data-side="before"]').getByTestId('compare-version-text')).toContainText(FIRST_SENTENCE)
  // The verdict letter is the letter of the "after" box.
  const verdict = await dialog.getByTestId('compare-verdict').getAttribute('data-verdict')
  await expect(after).toHaveAttribute('data-letter', verdict!)

  // History lists it.
  await dialog.getByTestId('compare-tab-history').click()
  await expect(dialog.getByTestId('compare-history-row')).toHaveCount(1)
  await expect(dialog.getByTestId('compare-history-row')).toContainText('better')
  await expect(dialog.getByTestId('compare-history-row')).toContainText('snapshot rewrite/rev-1-v1')
  await dialog.getByTestId('compare-close').click()
  await expect(page.getByTestId('compare-dialog')).toHaveCount(0)

  // The model call is an ordinary run: listed in the Findings tab.
  await page.getByTestId('tab-findings').click()
  await expect(page.getByTestId('run-list').getByTestId('run-row').first()).toContainText('ab-compare')
  expect(errors).toEqual([])
})

test('Cancel stops a running comparison through its run', async ({ page, request }) => {
  await seedCompare(request)
  await request.post(`${MOCK_URL}/__mock/compare-config`, { data: { duration_ms: 8000 } })
  await openApp(page)
  await openCompareFromOutliner(page)
  const dialog = page.getByTestId('compare-dialog')
  await dialog.getByTestId('compare-start').click()
  await expect(dialog.getByTestId('compare-status')).toHaveAttribute('data-status', 'running')
  await dialog.getByTestId('compare-cancel').click()
  await expect(dialog.getByTestId('compare-status')).toHaveAttribute('data-status', 'failed')
  await expect(dialog.getByTestId('compare-failed')).toHaveText('Cancelled.')
  await expect(dialog.getByTestId('compare-reveal')).toHaveCount(0)

  // "New comparison" returns to the form.
  await dialog.getByTestId('compare-new').click()
  await expect(dialog.getByTestId('compare-start')).toBeVisible()
})

// ---------------------------------------------------------------------------
// Coverage matrix
// ---------------------------------------------------------------------------

test('Coverage matrix: counts, empty rows / columns, and a cell click is one link op', async ({ page, request }) => {
  const errors = collectConsoleErrors(page)
  const method = node('method', 'section', 'How it works', { anchor: { file: 'sections/method.tex', marker: '\\section{Method}' } })
  await seedSpec(
    request,
    paperSpec(
      [introSection({ claims: ['c1'], children: [p1({ claims: ['c1'] })] }), method],
      [node('c1', 'claim', 'Mocks are cheap to run'), node('c2', 'claim', 'Nobody tests the tests')],
    ),
  )
  await openApp(page)
  await page.getByTestId('tab-spec').click()
  await expect(page.getByTestId('coverage-matrix')).toBeVisible()

  // c1 is argued twice in the Introduction (the section and p-1), never in Method.
  await expect(cell(page, 'c1', 'introduction')).toHaveText('2')
  await expect(cell(page, 'c1', 'introduction')).toHaveAttribute('data-own', 'true')
  await expect(cell(page, 'c1', 'method')).toHaveText('')
  await expect(cell(page, 'c2', 'introduction')).toHaveText('')
  // c2 is an unreferenced claim (empty row); Method argues nothing (empty column).
  await expect(page.locator('[data-testid="coverage-row"][data-claim-id="c2"]')).toHaveAttribute('data-unreferenced', 'true')
  await expect(page.locator('[data-testid="coverage-row"][data-claim-id="c1"]')).not.toHaveAttribute('data-unreferenced', 'true')
  await expect(page.locator('[data-testid="coverage-section"][data-section-id="method"]')).toHaveAttribute('data-empty', 'true')
  await expect(page.locator('[data-testid="coverage-section"][data-section-id="introduction"]')).toContainText('Introduction')
  // Hover names claim and section in full.
  await expect(cell(page, 'c2', 'method')).toHaveAttribute('title', /c2 “Nobody tests the tests” × Method/)
  await expect(page.getByTestId('coverage-summary')).toContainText('1 unreferenced claim')

  // Click: link c2 to Method, exactly one op.
  const before = (await specLog(request)).length
  await cell(page, 'c2', 'method').click()
  await expect(cell(page, 'c2', 'method')).toHaveText('1')
  await expect(cell(page, 'c2', 'method')).toHaveAttribute('data-own', 'true')
  const after = await specLog(request)
  expect(after.slice(before).map((e) => e.body.op)).toEqual([{ type: 'link_claim', id: 'method', claim_id: 'c2' }])
  await expect(page.locator('[data-testid="coverage-row"][data-claim-id="c2"]')).not.toHaveAttribute('data-unreferenced', 'true')
  await expect(page.locator('[data-testid="coverage-section"][data-section-id="method"]')).not.toHaveAttribute('data-empty', 'true')
  // The outliner row shows the new link too.
  await expect(row(page, 'method').locator('[data-testid="linked-claim"][data-claim-id="c2"]')).toBeVisible()

  // Click again: unlink.
  await cell(page, 'c2', 'method').click()
  await expect(cell(page, 'c2', 'method')).toHaveText('')
  expect(await ops(request, 'unlink_claim')).toEqual([{ type: 'unlink_claim', id: 'method', claim_id: 'c2' }])

  // A change from outside (spec.changed) refreshes the matrix.
  await seedSpec(request, paperSpec([introSection({ children: [p1()] }), method], [node('c1', 'claim', 'Mocks are cheap to run')]))
  await expect(cell(page, 'c1', 'introduction')).toHaveText('')
  await expect(page.locator('[data-testid="coverage-row"]')).toHaveCount(1)
  expect(errors).toEqual([])
})

// ---------------------------------------------------------------------------
// Editor selection menu
// ---------------------------------------------------------------------------

test('Selection menu: lift to claim, set as message, set as argument, and Undo from the toast', async ({ page, request }) => {
  const errors = collectConsoleErrors(page)
  await seedSpec(request, paperSpec([introSection({ children: [p1({ says: 'old message' })] })]))
  await putTex(request, INTRO_FILE, INTRO_WITH_MARKER)
  await openApp(page)
  await openFile(page, INTRO_FILE)

  // Right-click on the selected sentence (line 3, under the p-1 marker).
  let line = await selectLine(page, FIRST_SENTENCE)
  await rightClickLine(page, line)
  await expect(menu(page)).toBeVisible()
  await expect(menu(page).getByTestId('ctx-node')).toHaveText('in p-1')
  await expect(menu(page).getByTestId('ctx-set-message')).toBeVisible()
  await expect(menu(page).getByTestId('ctx-compare')).toBeEnabled()
  await menu(page).getByTestId('ctx-lift-claim').click()
  await expect(menu(page)).toHaveCount(0)

  await expect(page.getByTestId('spec-toast')).toContainText('Lifted to claim c1 and linked it to p-1')
  expect(await ops(request)).toEqual([
    { type: 'add_claim', says: FIRST_SENTENCE },
    { type: 'link_claim', id: 'p-1', claim_id: 'c1' },
  ])
  await page.getByTestId('tab-spec').click()
  await expect(page.locator('[data-testid="claim-card"][data-claim-id="c1"]').getByTestId('claim-says')).toHaveValue(FIRST_SENTENCE)
  await expect(row(page, 'p-1').locator('[data-testid="linked-claim"][data-claim-id="c1"]')).toBeVisible()

  // Ctrl-. opens the same menu from the keyboard: Set as argument (the `thesis` field).
  await openFile(page, INTRO_FILE)
  line = await selectLine(page, FIRST_SENTENCE)
  await page.keyboard.press('ControlOrMeta+Period')
  await expect(menu(page)).toBeVisible()
  await expect(menu(page).getByTestId('ctx-set-message')).toBeFocused()
  await menu(page).getByTestId('ctx-set-thesis').click()
  await expect(page.getByTestId('spec-toast')).toContainText('Argument set')
  await page.getByTestId('tab-spec').click()
  await expect(page.getByTestId('message-field-thesis')).toHaveValue(FIRST_SENTENCE)

  // Set as this paragraph's message, then Undo from the toast.
  await openFile(page, INTRO_FILE)
  line = await selectLine(page, 'This sentence cites')
  await rightClickLine(page, line)
  await menu(page).getByTestId('ctx-set-message').click()
  await expect(page.getByTestId('spec-toast')).toContainText('p-1 now says')
  expect((await ops(request, 'update')).at(-1)).toEqual({ type: 'update', id: 'p-1', says: 'This sentence cites a missing key~\\cite{nobody}.' })
  await page.getByTestId('tab-spec').click()
  await expect(page.locator('[data-testid="says-input"][data-node-id="p-1"]')).toHaveValue('This sentence cites a missing key~\\cite{nobody}.')
  await page.getByTestId('spec-toast-action').click()
  await expect(page.locator('[data-testid="says-input"][data-node-id="p-1"]')).toHaveValue('old message')
  await expect(page.getByTestId('spec-toast')).toContainText('Undone')
  expect((await getSpec(request)).spec.tree.children[0].children[0].says).toBe('old message')
  expect(errors).toEqual([])
})

test('Selection menu outside an adopted paragraph: "Adopt this paragraph first", then lift', async ({ page, request }) => {
  await seedSpec(request, paperSpec([introSection()]))
  await openApp(page)
  await openFile(page, INTRO_FILE)

  // Nothing selected: no custom menu (the browser's own would show).
  await page.locator('.cm-content .cm-line', { hasText: FIRST_SENTENCE }).click()
  await page.keyboard.press('End')
  await page.keyboard.press('ControlOrMeta+Period')
  await expect(menu(page)).toHaveCount(0)

  const line = await selectLine(page, FIRST_SENTENCE)
  await rightClickLine(page, line)
  await expect(menu(page).getByTestId('ctx-node')).toHaveText('not in an adopted paragraph')
  await expect(menu(page).getByTestId('ctx-adopt-title')).toHaveText('Adopt this paragraph first')
  await expect(menu(page).getByTestId('ctx-set-message')).toHaveCount(0)
  await expect(menu(page).getByTestId('ctx-compare')).toBeDisabled()
  await menu(page).getByTestId('ctx-adopt-claim').click()

  await expect(page.getByTestId('spec-toast')).toContainText('Adopted the paragraph as p-1. Lifted to claim c1 and linked it to p-1.')
  const log = await specLog(request)
  expect(log.map((e) => (e.path === '/api/spec/ops' ? e.body.op?.type : e.path))).toEqual(['/api/spec/adopt', 'add_claim', 'link_claim'])
  expect(log[0].body).toEqual({ kind: 'paragraph', file: INTRO_FILE, line_start: 2 })
  // The marker is in the editor above the sentence.
  await expect(page.locator('.cm-content .cm-line').nth(1)).toHaveText('% @p p-1')
  const spec = (await getSpec(request)).spec
  expect(spec.claims.map((c) => c.says)).toEqual([FIRST_SENTENCE])
  expect(spec.tree.children[0].children.map((p) => [p.id, p.claims])).toEqual([['p-1', ['c1']]])

  // The selection menu now finds p-1 for the same sentence (unsaved edits or not).
  const again = await selectLine(page, FIRST_SENTENCE)
  await rightClickLine(page, again)
  await expect(menu(page).getByTestId('ctx-node')).toHaveText('in p-1')
  await page.keyboard.press('Escape')
  await expect(menu(page)).toHaveCount(0)
})
