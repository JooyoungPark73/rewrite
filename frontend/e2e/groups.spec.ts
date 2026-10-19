// Headless tests of the phase 6 UI against the mock backend: "Run both" and
// its merged view, "Fan out" with its per-section grouping, group cancel, the
// triage call (verdict badges, "hide wrong/trivial", "Dismiss all flagged"),
// and the pass select hiding non-review passes.
//
// The mock (mock/groups.mjs on top of mock/runs.mjs):
// - codex counts as installed only after POST /__mock/runs-config
//   {codex_available: true}. A codex run answers with ONE finding on the same
//   sentence as claude's first (sections/intro.tex:2), so "Run both" merges
//   into one agreement entry (claude + codex) and claude's two other findings
//   (intro.tex:3 critical, method.tex:3 minor). codex takes 1.5x as long.
// - "Fan out" with the two seeded sections: Section: Introduction (2
//   findings), Section: Method (1), and message-drift over "Abstract, first and
//   last section" (1); members finish one after the other over
//   POST /__mock/groups-config {fanout_ms} (default 2 s).
// - Triage marks open findings by severity: critical keep, major wrong, minor
//   trivial (after {triage_ms}, default 1 s).
// GET /__mock/groups-log lists every write request to the phase 6 endpoints.
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import { MOCK_URL } from '../playwright.config.ts'

// ---------------------------------------------------------------------------
// Mock helpers
// ---------------------------------------------------------------------------

function section(id: string, file: string, heading: string, says = '') {
  return {
    id,
    level: 'section',
    says,
    children: [],
    claims: [],
    fields: { figure_story: '' },
    anchor: { file, marker: `\\section{${heading}}` },
    notes: '',
  }
}

/** paper.json with two linked sections (Introduction, Method). */
async function seedSpec(request: APIRequestContext) {
  const message = { id: 'message', level: 'message', says: 'Mocks help', children: [], claims: [], fields: { problem: '', stakes: '', thesis: '' }, anchor: null, notes: '' }
  const spec = {
    version: 1,
    message,
    claims: [],
    tree: {
      ...message,
      says: '',
      fields: {},
      children: [section('introduction', 'sections/intro.tex', 'Introduction', 'Why mocks'), section('method', 'sections/method.tex', 'Method')],
    },
  }
  await request.post(`${MOCK_URL}/__mock/spec-raw`, { data: { raw: JSON.stringify(spec, null, 2) + '\n' } })
}

interface LogEntry {
  method: string
  path: string
  body: Record<string, unknown> | null
}

async function groupsLog(request: APIRequestContext): Promise<LogEntry[]> {
  return (await (await request.get(`${MOCK_URL}/__mock/groups-log`)).json()) as LogEntry[]
}

async function enableCodex(request: APIRequestContext) {
  await request.post(`${MOCK_URL}/__mock/runs-config`, { data: { codex_available: true } })
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

const groupRows = (page: Page) => page.getByTestId('run-list').getByTestId('group-row')
const entries = (page: Page) => page.getByTestId('merged-list').getByTestId('merged-entry')
const runCards = (page: Page) => page.getByTestId('run-findings').getByTestId('finding-card')
const runRows = (page: Page) => page.getByTestId('run-list').getByTestId('run-row')

async function openFindingsTab(page: Page) {
  await page.goto('/')
  await expect(page.getByTestId('open-file-name')).toHaveText('main.tex')
  await page.getByTestId('tab-findings').click()
  await expect(page.getByTestId('run-launcher')).toBeVisible()
  await expect(page.getByTestId('pass-select').locator('option[value="paragraph-function"]')).toHaveCount(1)
}

test.beforeEach(async ({ request }) => {
  await request.post(`${MOCK_URL}/__mock/reset`)
  await seedSpec(request)
})

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test('non-review passes are hidden from the pass select; Run both needs codex', async ({ page, request }) => {
  await openFindingsTab(page)
  const values = await page.getByTestId('pass-select').locator('option').evaluateAll((els) => els.map((o) => (o as HTMLOptionElement).value))
  expect(values).not.toContain('ab-compare')
  expect(values).not.toContain('triage')
  expect(values).toContain('message-drift')
  // The API still lists them (the compare dialog and the triage call use them).
  const passes = (await (await request.get(`${MOCK_URL}/api/passes`)).json()).passes as { name: string; kind: string }[]
  expect(passes.find((p) => p.name === 'ab-compare')?.kind).toBe('compare')
  expect(passes.find((p) => p.name === 'triage')?.kind).toBe('triage')

  // codex is not installed in the mock by default: "Run both" is disabled and says why.
  const both = page.getByTestId('run-both-button')
  await expect(both).toBeDisabled()
  await expect(both).toHaveAttribute('title', /codex is not installed/)
  // "Fan out" only appears with scope = paper.
  await page.getByTestId('pass-select').selectOption('paragraph-function')
  await expect(page.getByTestId('scope-section')).toBeChecked()
  await expect(page.getByTestId('fanout-button')).toHaveCount(0)
  await page.getByTestId('scope-paper').check()
  await expect(page.getByTestId('fanout-button')).toBeEnabled()
  await expect(page.getByTestId('fanout-button')).toHaveAttribute('title', /one run per linked section \(2\).*message-drift.*at most 3 at once/)
})

test('Run both: 2/2 runs, merged view with the agreement first, j / k, click highlights in the editor', async ({ page, request }) => {
  const errors = collectConsoleErrors(page)
  await enableCodex(request)
  await request.post(`${MOCK_URL}/__mock/runs-config`, { data: { duration_ms: 2000 } })
  await openFindingsTab(page)
  await page.getByTestId('pass-select').selectOption('paragraph-function')
  await expect(page.getByTestId('scope-section-select')).toHaveValue('introduction')

  // The popover: model / thinking per runner.
  await page.getByTestId('run-both-button').click()
  const panel = page.getByTestId('run-both-button-panel')
  await expect(panel).toBeVisible()
  await expect(panel.getByTestId('both-claude-model-input')).toHaveAttribute('placeholder', 'default (sonnet)')
  await expect(panel.getByTestId('both-codex-model-input')).toHaveAttribute('placeholder', 'default (CLI default)')
  await expect(panel.getByTestId('both-claude-runner-select')).toHaveCount(0) // the runner is fixed per row
  await panel.getByTestId('both-claude-model-input').fill('haiku')
  await panel.getByTestId('both-codex-thinking-select').selectOption('low')
  await panel.getByTestId('run-both-start').click()
  await expect(panel).toHaveCount(0)

  const row = groupRows(page).first()
  await expect(row).toHaveAttribute('data-kind', 'both')
  await expect(row.getByTestId('group-kind')).toHaveText('both')
  await expect(row).toHaveAttribute('data-selected', 'true')
  await expect(row.getByTestId('group-cancel')).toBeVisible()
  // claude ends first (2 s), codex later (3 s).
  await expect(row.getByTestId('group-progress')).toHaveText('1/2 done', { timeout: 10_000 })
  await expect(row.getByTestId('group-progress')).toHaveText('2/2 done', { timeout: 10_000 })
  await expect(row).toHaveAttribute('data-status', 'done')
  await expect(row.getByTestId('group-cancel')).toHaveCount(0)
  await expect(row.getByTestId('group-cost')).toHaveText('$0.0123') // codex reports no cost
  await expect(row.getByTestId('group-counts')).toHaveText('4 findings · 1 agreement')
  // The member runs sit under the group row (not as separate top-level rows).
  const members = page.getByTestId('group-item').first().locator('[data-testid="run-row"][data-nested="true"]')
  expect(await members.evaluateAll((els) => els.map((e) => e.getAttribute('data-runner')))).toEqual(['claude', 'codex'])
  await expect(runRows(page).and(page.locator(':not([data-nested])'))).toHaveCount(0)

  // Merged view: the agreement first, then claude's two other findings.
  await expect(page.getByTestId('group-detail-status')).toHaveText('done')
  await expect(entries(page)).toHaveCount(3)
  expect(await entries(page).evaluateAll((els) => els.map((e) => e.getAttribute('data-agreement')))).toEqual(['2', '1', '1'])
  const agreed = entries(page).first()
  await expect(agreed.getByTestId('agreement-badge')).toHaveText('2/2 agree')
  await expect(agreed.getByTestId('agreement-badge')).toHaveClass(/badge-success/)
  await expect(entries(page).nth(1).getByTestId('agreement-badge')).toHaveText('1/2')
  expect(await agreed.getByTestId('runner-chip').allInnerTexts()).toEqual(['claude', 'codex'])
  expect(await entries(page).nth(1).getByTestId('runner-chip').allInnerTexts()).toEqual(['claude'])
  await expect(agreed).toHaveAttribute('data-severity', 'major') // the highest of major + minor
  await expect(page.getByTestId('merged-summary')).toHaveText('3 merged entries · 1 found by both runners')

  // ▸ unfolds each runner's own problem / why.
  await agreed.getByTestId('merged-expand').click()
  const perRunner = agreed.getByTestId('merged-runner-finding')
  await expect(perRunner).toHaveCount(2)
  await expect(perRunner.nth(0)).toHaveAttribute('data-runner', 'claude')
  await expect(perRunner.nth(1)).toHaveAttribute('data-runner', 'codex')
  await expect(perRunner.nth(1)).toContainText('states a general remark instead of the paragraph topic')
  await expect(page.getByTestId('tab-findings')).toHaveAttribute('aria-selected', 'true') // unfolding did not open the editor

  // j / k step through the merged entries.
  await page.getByTestId('merged-summary').click() // focus outside any text box
  await page.keyboard.press('j')
  await expect(entries(page).first()).toHaveAttribute('data-selected', 'true')
  await page.keyboard.press('j')
  await expect(entries(page).nth(1)).toHaveAttribute('data-selected', 'true')
  await page.keyboard.press('j')
  await page.keyboard.press('j')
  await expect(entries(page).nth(2)).toHaveAttribute('data-selected', 'true')
  await page.keyboard.press('k')
  await expect(entries(page).nth(1)).toHaveAttribute('data-selected', 'true')

  // The request carried the per-runner options and nothing else.
  const posts = (await groupsLog(request)).filter((e) => e.path === '/api/groups/both')
  expect(posts).toHaveLength(1)
  expect(posts[0].body).toEqual({
    pass: 'paragraph-function',
    scope: { kind: 'section', ref: 'introduction', file: null, line_start: null, line_end: null },
    claude: { model: 'haiku' },
    codex: { thinking: 'low' },
  })

  // Clicking the agreement highlights its (claude) finding in the editor like a normal finding.
  await agreed.locator('[data-testid="merged-problem"]').click()
  await expect(page.getByTestId('tab-editor')).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByTestId('open-file-name')).toHaveText('sections/intro.tex')
  const active = page.locator('.cm-line.cm-finding-active')
  await expect(active).toHaveCount(1)
  await expect(active).toContainText('Mock papers are useful for testing user interfaces.')
  // Both members' findings stay in the editor column: the two runs of one group do not replace each other.
  const column = page.getByTestId('findings-sidebar')
  await expect(column.getByTestId('finding-card')).toHaveCount(3) // claude intro:2 + intro:3, codex intro:2
  expect(errors).toEqual([])
})

test('Fan out: one run per linked section plus message-drift, progress to 3/3, entries grouped by section', async ({ page, request }) => {
  const errors = collectConsoleErrors(page)
  await request.post(`${MOCK_URL}/__mock/groups-config`, { data: { fanout_ms: 3000 } })
  await openFindingsTab(page)
  await page.getByTestId('pass-select').selectOption('paragraph-function')
  await page.getByTestId('scope-paper').check()
  await page.getByTestId('thinking-select').selectOption('low')
  await page.getByTestId('fanout-button').click()

  const row = groupRows(page).first()
  await expect(row).toHaveAttribute('data-kind', 'fanout')
  await expect(row.getByTestId('group-kind')).toHaveText('fan-out')
  await expect(row).toContainText('whole paper, per section')
  // Members end one after the other (1 s apart): the progress passes through 1/3 or 2/3.
  await expect(row.getByTestId('group-progress')).toHaveText(/^[12]\/3 done$/, { timeout: 10_000 })
  await expect(row.getByTestId('group-progress')).toHaveText('3/3 done', { timeout: 10_000 })
  await expect(row).toHaveAttribute('data-status', 'done')
  await expect(row.getByTestId('group-counts')).toHaveText('4 findings')
  await expect(row.getByTestId('group-cost')).toHaveText('$0.0369')

  // Members: two section runs of the pass and one message-drift run over the ends.
  const members = page.getByTestId('group-item').first().locator('[data-testid="run-row"][data-nested="true"]')
  await expect(members).toHaveCount(3)
  await expect(members.nth(0)).toContainText('paragraph-function')
  await expect(members.nth(0)).toContainText('Section: Introduction')
  await expect(members.nth(2)).toContainText('message-drift')
  await expect(members.nth(2)).toContainText('Abstract, first and last section')

  // The merged view groups the findings under each member's section.
  const sections = page.getByTestId('merged-section')
  await expect(sections).toHaveCount(3)
  expect(await sections.evaluateAll((els) => els.map((e) => e.getAttribute('data-section')))).toEqual([
    'Section: Introduction',
    'Section: Method',
    'Abstract, first and last section',
  ])
  await expect(sections.nth(0).getByTestId('merged-entry')).toHaveCount(2)
  await expect(sections.nth(1).getByTestId('merged-entry')).toHaveCount(1)
  await expect(sections.nth(2).getByTestId('merged-entry')).toHaveCount(1)
  await expect(sections.nth(2)).toContainText('(message-drift)')
  // No agreement badges in a fan-out (every entry has one runner).
  await expect(page.getByTestId('agreement-badge')).toHaveCount(0)
  await expect(page.getByTestId('merged-summary')).toHaveText('4 findings from 3 runs')

  // j / k follow the on-screen order across the sections.
  await page.getByTestId('merged-summary').click()
  for (let i = 0; i < 3; i++) await page.keyboard.press('j')
  await expect(sections.nth(1).getByTestId('merged-entry')).toHaveAttribute('data-selected', 'true')
  await page.keyboard.press('j')
  await expect(sections.nth(2).getByTestId('merged-entry')).toHaveAttribute('data-selected', 'true')

  const posts = (await groupsLog(request)).filter((e) => e.path === '/api/groups/fanout')
  expect(posts.map((e) => e.body)).toEqual([{ pass: 'paragraph-function', thinking: 'low' }])

  // A member chip opens that run on its own.
  await page.getByTestId('group-member').nth(1).click()
  await expect(page.getByTestId('run-detail')).toHaveAttribute('data-run-id', /run-/)
  await expect(page.getByTestId('run-detail')).toContainText('Section: Method')
  await expect(runCards(page)).toHaveCount(1)
  expect(errors).toEqual([])
})

test('Cancel on a running group stops every member; the group says cancelled', async ({ page, request }) => {
  await request.post(`${MOCK_URL}/__mock/groups-config`, { data: { fanout_ms: 20_000 } })
  await openFindingsTab(page)
  await page.getByTestId('pass-select').selectOption('section-promises') // default scope: paper
  await expect(page.getByTestId('scope-paper')).toBeChecked()
  await page.getByTestId('fanout-button').click()

  const row = groupRows(page).first()
  await expect(row).toHaveAttribute('data-status', 'running')
  await expect(row.getByTestId('group-progress')).toHaveText('0/3 done')
  await row.getByTestId('group-cancel').click()
  await expect(row).toHaveAttribute('data-status', 'cancelled')
  await expect(row.getByTestId('group-status')).toHaveText('cancelled')
  await expect(row.getByTestId('group-cancel')).toHaveCount(0)
  await expect(row.getByTestId('group-progress')).toHaveText('3/3 done')
  await expect(page.getByTestId('group-detail-status')).toHaveText('cancelled')
  // Every member ended "cancelled" (listed under the group while it is selected).
  const members = page.getByTestId('group-item').first().locator('[data-testid="run-row"][data-nested="true"]')
  await expect(members).toHaveCount(3)
  expect(await members.evaluateAll((els) => els.map((e) => e.getAttribute('data-status')))).toEqual(['cancelled', 'cancelled', 'cancelled'])
  const cancels = (await groupsLog(request)).filter((e) => e.path.endsWith('/cancel'))
  expect(cancels.map((e) => e.path)).toEqual(['/api/groups/group-1/cancel'])
})

test('Triage: verdict badges, "hide wrong/trivial", and Dismiss all flagged', async ({ page, request }) => {
  const errors = collectConsoleErrors(page)
  await openFindingsTab(page)
  await page.getByTestId('pass-select').selectOption('paragraph-function')
  await page.getByTestId('run-button').click()
  await expect(runRows(page).first()).toHaveAttribute('data-status', 'done', { timeout: 10_000 })
  await expect(runCards(page)).toHaveCount(3)
  await expect(page.getByTestId('triage-status')).toHaveText('not triaged')
  await expect(page.getByTestId('verdict-badge')).toHaveCount(0)

  await page.getByTestId('triage-button').click()
  const panel = page.getByTestId('triage-button-panel')
  await expect(panel).toContainText('3 open findings')
  await panel.getByTestId('triage-thinking-select').selectOption('low')
  await panel.getByTestId('triage-start').click()
  await expect(page.getByTestId('triage-status')).toHaveAttribute('data-status', /queued|running/)
  await expect(page.getByTestId('triage-status')).toContainText('Triage running')
  await expect(page.getByTestId('triage-button')).toBeDisabled()

  // Done: a verdict on every card (critical keep, major wrong, minor trivial) and the counts.
  await expect(page.getByTestId('triage-status')).toHaveAttribute('data-status', 'done', { timeout: 10_000 })
  await expect(page.getByTestId('triage-count-keep')).toHaveText('1 keep')
  await expect(page.getByTestId('triage-count-wrong')).toHaveText('1 wrong')
  await expect(page.getByTestId('triage-count-trivial')).toHaveText('1 trivial')
  const verdicts = await runCards(page).evaluateAll((els) => els.map((e) => [e.getAttribute('data-severity'), e.getAttribute('data-verdict')]))
  expect(verdicts).toEqual([
    ['major', 'wrong'],
    ['critical', 'keep'],
    ['minor', 'trivial'],
  ])
  const keep = runCards(page).nth(1).getByTestId('verdict-badge')
  await expect(keep).toHaveClass(/badge-success/)
  await expect(runCards(page).nth(0).getByTestId('verdict-badge')).toHaveClass(/badge-error/)
  await expect(runCards(page).nth(2).getByTestId('verdict-badge')).toHaveClass(/bg-slate-400/) // grey
  // The reason: tooltip, and unfolded by a click (which does not open the editor).
  await expect(keep).toHaveAttribute('title', /The cited key really is missing/)
  await keep.click()
  await expect(runCards(page).nth(1).getByTestId('verdict-reason')).toContainText('The cited key really is missing')
  await expect(page.getByTestId('tab-findings')).toHaveAttribute('aria-selected', 'true')
  // Verdicts never change the state by themselves.
  expect(await runCards(page).evaluateAll((els) => els.map((e) => e.getAttribute('data-state')))).toEqual(['open', 'open', 'open'])

  // The filter hides wrong / trivial; off again shows them.
  await page.getByTestId('hide-flagged').check()
  await expect(runCards(page)).toHaveCount(1)
  await expect(runCards(page).first()).toHaveAttribute('data-verdict', 'keep')
  await page.getByTestId('hide-flagged').uncheck()
  await expect(runCards(page)).toHaveCount(3)

  // Dismiss all flagged: the two flagged findings leave the open list.
  const dismiss = page.getByTestId('dismiss-flagged')
  await expect(dismiss).toHaveText('Dismiss all flagged (2)')
  await dismiss.click()
  await expect(page.getByTestId('runs-toast')).toContainText('Dismissed 2 flagged findings (1 wrong, 1 trivial).')
  await expect(runCards(page)).toHaveCount(1)
  await expect(runCards(page).first()).toHaveAttribute('data-verdict', 'keep')
  await expect(dismiss).toHaveText('Dismiss all flagged (0)')
  await expect(dismiss).toBeDisabled()
  await page.getByTestId('run-detail').getByTestId('state-filter-all').click()
  expect(await runCards(page).evaluateAll((els) => els.map((e) => e.getAttribute('data-state')))).toEqual(['dismissed', 'open', 'dismissed'])

  // The editor column shows the verdicts too.
  await runCards(page).nth(1).click()
  await expect(page.getByTestId('findings-sidebar').locator('[data-testid="finding-card"][data-verdict="keep"]')).toHaveCount(1)

  const log = await groupsLog(request)
  expect(log).toEqual([
    { method: 'POST', path: '/api/runs/run-1/triage', body: { thinking: 'low' } },
    { method: 'POST', path: '/api/findings/dismiss-flagged', body: { run_id: 'run-1' } },
  ])
  expect(errors).toEqual([])
})

test('Triage of a "Run both" member: open the claude run from the group and triage it', async ({ page, request }) => {
  await enableCodex(request)
  await openFindingsTab(page)
  await page.getByTestId('pass-select').selectOption('paragraph-function')
  await page.getByTestId('run-both-button').click()
  await page.getByTestId('run-both-start').click()
  const row = groupRows(page).first()
  await expect(row).toHaveAttribute('data-status', 'done', { timeout: 10_000 })

  // Open the claude member, triage it with claude.
  await page.locator('[data-testid="run-row"][data-nested="true"][data-runner="claude"]').click()
  await expect(page.getByTestId('run-detail')).toBeVisible()
  await expect(runCards(page)).toHaveCount(3)
  await page.getByTestId('triage-button').click()
  await page.getByTestId('triage-runner-select').selectOption('claude')
  await page.getByTestId('triage-start').click()
  await expect(page.getByTestId('triage-status')).toHaveAttribute('data-status', 'done', { timeout: 10_000 })
  await expect(page.getByTestId('verdict-badge')).toHaveCount(3)

  // Back in the merged view, the expanded entry shows the claude finding's verdict.
  await row.click()
  await expect(entries(page)).toHaveCount(3)
  await entries(page).first().getByTestId('merged-expand').click()
  await expect(entries(page).first().locator('[data-runner="claude"]').getByTestId('verdict-badge')).toHaveText('wrong')
  // "hide wrong/trivial" hides entries whose findings are all flagged: the agreement keeps codex's
  // un-triaged finding, the critical one is "keep", only the minor (trivial) one goes.
  await page.getByTestId('hide-flagged').check()
  await expect(entries(page)).toHaveCount(2)
})
