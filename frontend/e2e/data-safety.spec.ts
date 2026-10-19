// Regression tests for the data-loss defects found by the external audit of
// 2026-10-18 (.agents/scratch/codex-audit-2026-10-18.md, items 1-4 and 6).
//
// Each test drives the real UI against the mock backend and uses
// `page.route()` to delay or reject one kind of request, so the timing that
// lost text in the audit (a save still in flight, a failed save, a repo switch,
// an external change to paper.json) happens on cue:
//   item 1  Raw JSON draft of paper A must never be written into paper B
//   item 2  opening another repo saves (or keeps) the open file's edits first
//   item 3  switching files after a failed save, or while typing during the
//           save, keeps every edit
//   item 4  Raw JSON: typing while a save is in flight survives its response
//   item 6  undo after an external change to paper.json never removes it
//   item 5  (frontend half) a save is based on the revision the editor loaded
//           (`base_sha256`); a file changed on disk meanwhile gives 409, which
//           keeps the buffer and offers a Reload instead of overwriting
//
// The mock keeps separate files and paper.json per repo path (see "Several
// repos" in mock/server.mjs). Repo A is the mock's default /tmp/mock-paper;
// repo B is created per test with POST /__mock/repo.
import { expect, test, type APIRequestContext, type Page, type Request, type Route } from '@playwright/test'
import { MOCK_URL } from '../playwright.config.ts'

const REPO_A = '/tmp/mock-paper'
const REPO_B = '/tmp/mock-paper-b'

// ---------------------------------------------------------------------------
// Spec builders (same shape as e2e/spec.spec.ts)
// ---------------------------------------------------------------------------

type Level = 'message' | 'section'

function node(id: string, level: Level, says = '') {
  const fields: Record<string, string> = level === 'message' ? { problem: '', stakes: '', thesis: '' } : { figure_story: '' }
  return { id, level, says, children: [], claims: [], fields, anchor: null, notes: '' }
}

/** A valid paper.json text with one section per `says`. */
function specText(...sections: { id: string; says: string }[]): string {
  const spec = {
    version: 1,
    message: node('message', 'message'),
    claims: [],
    tree: { ...node('message', 'message'), fields: {}, children: sections.map((s) => node(s.id, 'section', s.says)) },
  }
  return JSON.stringify(spec, null, 2) + '\n'
}

// ---------------------------------------------------------------------------
// Mock helpers
// ---------------------------------------------------------------------------

/** Replace the open repo's paper.json; `silent` skips the spec.changed event. */
async function seedSpec(request: APIRequestContext, raw: string, silent = false) {
  await request.post(`${MOCK_URL}/__mock/spec-raw`, { data: { raw, silent } })
}

/** Create repo B with its own main.tex and paper.json. */
async function createRepoB(request: APIRequestContext, specRaw = specText({ id: 'b-1', says: 'B promise' })) {
  await request.post(`${MOCK_URL}/__mock/repo`, {
    data: {
      path: REPO_B,
      files: { 'main.tex': '\\documentclass{article}\n\\begin{document}\nPaper B body.\n\\end{document}\n' },
      spec_raw: specRaw,
    },
  })
}

interface Save {
  repo: string
  path: string
  content: string
}

async function saves(request: APIRequestContext): Promise<Save[]> {
  return (await (await request.get(`${MOCK_URL}/__mock/saves`)).json()) as Save[]
}

interface SpecWrite {
  repo: string
  method: string
  path: string
  body: { raw?: string; spec?: unknown }
}

async function specWrites(request: APIRequestContext): Promise<SpecWrite[]> {
  return (await (await request.get(`${MOCK_URL}/__mock/spec-log`)).json()) as SpecWrite[]
}

/** paper.json of the repo the mock has open. */
async function diskSpec(request: APIRequestContext): Promise<string> {
  return ((await (await request.get(`${MOCK_URL}/api/spec`)).json()) as { raw: string }).raw
}

async function diskFile(request: APIRequestContext, path: string): Promise<string> {
  return ((await (await request.get(`${MOCK_URL}/api/file?path=${encodeURIComponent(path)}`)).json()) as { content: string })
    .content
}

/**
 * Hold the next request matching `method` + `url` until `release()` is called.
 * `sent` resolves once that request reached the route (i.e. was sent).
 */
async function holdNext(page: Page, method: string, url: string) {
  let release!: () => void
  const released = new Promise<void>((resolve) => (release = resolve))
  let markSent!: () => void
  const sent = new Promise<void>((resolve) => (markSent = resolve))
  let holding = true
  await page.route(url, async (route: Route) => {
    if (holding && route.request().method() === method) {
      holding = false
      markSent()
      await released
    }
    await route.continue()
  })
  return { sent, release }
}

/** Answer every `method` request to `url` with HTTP 500 and this detail. */
async function failAll(page: Page, method: string, url: string, detail: string) {
  await page.route(url, async (route: Route) => {
    if (route.request().method() !== method) return route.continue()
    await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail }) })
  })
}

/** "METHOD /path" of every API request the page sends, in order. */
function recordRequests(page: Page): string[] {
  const seen: string[] = []
  page.on('request', (r: Request) => {
    const url = new URL(r.url())
    if (url.pathname.startsWith('/api/')) seen.push(`${r.method()} ${url.pathname}`)
  })
  return seen
}

// ---------------------------------------------------------------------------
// Page helpers
// ---------------------------------------------------------------------------

async function openApp(page: Page) {
  await page.goto('/')
  await expect(page.getByTestId('open-file-name')).toHaveText('main.tex')
}

/** Open another repo through the header's Open dialog. */
async function openRepoViaDialog(page: Page, path: string) {
  await page.getByTestId('repo-path').click()
  await page.getByLabel('Repo path').fill(path)
  await page.getByRole('button', { name: 'Open', exact: true }).click()
}

const editorText = (page: Page) => page.locator('[data-testid="editor"] .cm-content')

/** Type at the end of the source editor. */
async function typeAtEnd(page: Page, text: string) {
  await editorText(page).click()
  await page.keyboard.press('Control+End')
  await page.keyboard.type(text)
}

async function openRawJson(page: Page) {
  await page.getByTestId('tab-spec').click()
  await page.getByRole('tab', { name: 'Raw JSON' }).click()
  await expect(page.getByTestId('raw-json')).toBeVisible()
}

const rawEditor = (page: Page) => page.getByTestId('raw-json').locator('.cm-content')

/** Replace the whole Raw JSON text. */
async function replaceRaw(page: Page, text: string) {
  await rawEditor(page).click()
  await page.keyboard.press('Control+a')
  await page.keyboard.insertText(text)
}

const saysInput = (page: Page, id: string) => page.locator(`[data-testid="says-input"][data-node-id="${id}"]`)

test.beforeEach(async ({ request }) => {
  await request.post(`${MOCK_URL}/__mock/reset`)
})

// ---------------------------------------------------------------------------
// Item 1: a Raw JSON draft belongs to its repo
// ---------------------------------------------------------------------------

test('item 1: a dirty Raw JSON draft of repo A never reaches repo B, and comes back with A', async ({ page, request }) => {
  await seedSpec(request, specText({ id: 'a-1', says: 'A promise' }))
  await createRepoB(request)
  await openApp(page)
  await openRawJson(page)
  await expect(rawEditor(page)).toContainText('A promise')

  // An unsaved, valid edit of A's spec.
  await replaceRaw(page, specText({ id: 'a-1', says: 'A DRAFT' }))
  await expect(page.getByTestId('raw-dirty')).toBeVisible()

  // Open B while the Raw JSON editor stays on screen.
  await openRepoViaDialog(page, REPO_B)
  await expect(page.getByTestId('repo-path')).toHaveText(REPO_B)
  await expect(rawEditor(page)).toContainText('B promise')
  await expect(rawEditor(page)).not.toContainText('A DRAFT')
  await expect(page.getByTestId('raw-dirty')).toHaveCount(0)
  await expect(page.getByTestId('raw-save')).toBeDisabled()

  // Saving in B writes B's own text.
  await replaceRaw(page, specText({ id: 'b-1', says: 'B edited' }))
  await page.getByTestId('raw-save').click()
  await expect(page.getByTestId('raw-dirty')).toHaveCount(0)
  await expect.poll(() => diskSpec(request)).toContain('B edited')

  // Back to A: the draft is restored, still unsaved, and saves into A.
  await openRepoViaDialog(page, REPO_A)
  await expect(page.getByTestId('repo-path')).toHaveText(REPO_A)
  await expect(rawEditor(page)).toContainText('A DRAFT')
  await expect(page.getByTestId('raw-dirty')).toBeVisible()
  await page.getByTestId('raw-save').click()
  await expect(page.getByTestId('raw-dirty')).toHaveCount(0)
  await expect.poll(() => diskSpec(request)).toContain('A DRAFT')

  // Every raw write went to the repo its text came from.
  const rawWrites = (await specWrites(request)).filter((w) => w.path === '/api/spec/raw')
  expect(rawWrites.map((w) => [w.repo, w.body.raw?.includes('A DRAFT') ? 'A DRAFT' : 'B edited'])).toEqual([
    [REPO_B, 'B edited'],
    [REPO_A, 'A DRAFT'],
  ])
})

test('item 1: a draft kept while the Raw JSON tab is closed is not shown for another repo', async ({ page, request }) => {
  await seedSpec(request, specText({ id: 'a-1', says: 'A promise' }))
  await createRepoB(request)
  await openApp(page)
  await openRawJson(page)
  await replaceRaw(page, specText({ id: 'a-1', says: 'A DRAFT' }))
  await expect(page.getByTestId('raw-dirty')).toBeVisible()

  // Leave the Raw JSON tab (it unmounts and keeps the draft), then open B.
  await page.getByRole('tab', { name: 'Outliner' }).click()
  await openRepoViaDialog(page, REPO_B)
  await expect(page.getByTestId('repo-path')).toHaveText(REPO_B)
  await expect(saysInput(page, 'b-1')).toHaveValue('B promise')
  await page.getByRole('tab', { name: 'Raw JSON' }).click()
  await expect(rawEditor(page)).toContainText('B promise')
  await expect(rawEditor(page)).not.toContainText('A DRAFT')
  await expect(page.getByTestId('raw-dirty')).toHaveCount(0)
  await expect(page.getByTestId('raw-save')).toBeDisabled()

  // Reopening A (tab closed meanwhile) brings the draft back.
  await page.getByRole('tab', { name: 'Outliner' }).click()
  await openRepoViaDialog(page, REPO_A)
  await expect(saysInput(page, 'a-1')).toHaveValue('A promise')
  await page.getByRole('tab', { name: 'Raw JSON' }).click()
  await expect(rawEditor(page)).toContainText('A DRAFT')
  await expect(page.getByTestId('raw-dirty')).toBeVisible()
  expect((await specWrites(request)).filter((w) => w.path === '/api/spec/raw')).toEqual([])
})

test('item 1: an outliner edit made for repo A while repo B loads is not written into B', async ({ page, request }) => {
  // Both repos have a section with the same id, so a misdirected op would succeed.
  await seedSpec(request, specText({ id: 'intro-1', says: 'A promise' }))
  await createRepoB(request, specText({ id: 'intro-1', says: 'B promise' }))
  await openApp(page)
  await page.getByTestId('tab-spec').click()
  await expect(saysInput(page, 'intro-1')).toHaveValue('A promise')

  // B's paper.json is slow to arrive, so A's outliner stays on screen meanwhile.
  const held = await holdNext(page, 'GET', '**/api/spec')
  await openRepoViaDialog(page, REPO_B)
  await held.sent
  await expect(page.getByTestId('repo-path')).toHaveText(REPO_B)
  await saysInput(page, 'intro-1').fill('TYPED FOR A')
  await saysInput(page, 'intro-1').blur()
  await page.waitForTimeout(300) // the edit would have been sent by now
  held.release()

  await expect(saysInput(page, 'intro-1')).toHaveValue('B promise')
  expect((await specWrites(request)).filter((w) => w.path === '/api/spec/ops')).toEqual([])
  expect(await diskSpec(request)).toContain('B promise')
  await expect(page.getByTestId('spec-toast')).toContainText('Edit not sent')
})

// ---------------------------------------------------------------------------
// Item 2: opening another repo with unsaved source
// ---------------------------------------------------------------------------

test('item 2: unsaved source is saved to repo A before the backend opens repo B', async ({ page, request }) => {
  await createRepoB(request)
  await openApp(page)
  const requests = recordRequests(page)
  await typeAtEnd(page, '% typed in A')
  await expect(page.getByTestId('dirty-marker')).toBeVisible()

  await openRepoViaDialog(page, REPO_B)
  await expect(page.getByTestId('repo-path')).toHaveText(REPO_B)
  await expect(editorText(page)).toContainText('Paper B body.')

  // Exactly one save, of A's main.tex with the typed text, sent before the open.
  const all = await saves(request)
  expect(all).toHaveLength(1)
  expect(all[0]).toMatchObject({ repo: REPO_A, path: 'main.tex' })
  expect(all[0].content).toContain('% typed in A')
  const writes = requests.filter((r) => r === 'PUT /api/file' || r === 'POST /api/repo/open')
  expect(writes).toEqual(['PUT /api/file', 'POST /api/repo/open'])
})

test('item 2: when that save fails, repo A stays open with its edits and an error', async ({ page, request }) => {
  await createRepoB(request)
  await openApp(page)
  const requests = recordRequests(page)
  await failAll(page, 'PUT', '**/api/file', 'disk is full')
  await typeAtEnd(page, '% typed in A')

  await openRepoViaDialog(page, REPO_B)
  await expect(page.getByTestId('open-repo-error')).toContainText('disk is full')
  await expect(page.getByTestId('repo-path')).toHaveText(REPO_A)
  expect(requests).not.toContain('POST /api/repo/open')

  // Close the dialog: A's buffer is intact and still marked unsaved.
  await page.getByRole('button', { name: 'Cancel' }).click()
  await expect(page.getByTestId('open-file-name')).toHaveText('main.tex')
  await expect(editorText(page)).toContainText('% typed in A')
  await expect(page.getByTestId('dirty-marker')).toBeVisible()
  expect(await saves(request)).toEqual([])
  expect((await (await request.get(`${MOCK_URL}/api/health`)).json()).repo).toBe(REPO_A)
})

// ---------------------------------------------------------------------------
// Item 3: switching files keeps edits
// ---------------------------------------------------------------------------

test('item 3: a failed save keeps the file open with its edits instead of switching', async ({ page }) => {
  await openApp(page)
  await page.getByTestId('file-tree').locator('[data-path="sections/intro.tex"]').click()
  await expect(page.getByTestId('open-file-name')).toHaveText('sections/intro.tex')
  await failAll(page, 'PUT', '**/api/file', 'permission denied')
  await typeAtEnd(page, 'UNSAVED WORDS')

  await page.getByTestId('file-tree').locator('[data-path="sections/method.tex"]').click()
  await expect(page.getByRole('alert').filter({ hasText: 'permission denied' })).toBeVisible()
  await page.waitForTimeout(300) // give a wrong switch time to happen
  await expect(page.getByTestId('open-file-name')).toHaveText('sections/intro.tex')
  await expect(editorText(page)).toContainText('UNSAVED WORDS')
  await expect(page.getByTestId('dirty-marker')).toBeVisible()
})

test('item 3: text typed while the switch-save is in flight is saved before the switch', async ({ page, request }) => {
  await openApp(page)
  await page.getByTestId('file-tree').locator('[data-path="sections/intro.tex"]').click()
  await expect(page.getByTestId('open-file-name')).toHaveText('sections/intro.tex')
  await typeAtEnd(page, 'FIRST')
  const held = await holdNext(page, 'PUT', '**/api/file')

  await page.getByTestId('file-tree').locator('[data-path="sections/method.tex"]').click()
  await held.sent
  // Keep typing in intro.tex while its save is pending.
  await typeAtEnd(page, ' SECOND')
  held.release()

  await expect(page.getByTestId('open-file-name')).toHaveText('sections/method.tex')
  await expect.poll(() => diskFile(request, 'sections/intro.tex')).toContain('FIRST SECOND')
  // Coming back shows the text from disk, with both edits.
  await page.getByTestId('file-tree').locator('[data-path="sections/intro.tex"]').click()
  await expect(editorText(page)).toContainText('FIRST SECOND')
  await expect(page.getByTestId('dirty-marker')).toHaveCount(0)
})

// ---------------------------------------------------------------------------
// Item 4: Raw JSON typing during a save
// ---------------------------------------------------------------------------

for (const how of ['button', 'keyboard'] as const) {
  test(`item 4: Raw JSON text typed while a save is in flight survives its response (${how})`, async ({ page, request }) => {
    await seedSpec(request, specText({ id: 'a-1', says: 'start' }))
    await openApp(page)
    await openRawJson(page)
    await replaceRaw(page, specText({ id: 'a-1', says: 'FIRST SAVE' }))
    const held = await holdNext(page, 'PUT', '**/api/spec/raw')

    if (how === 'button') await page.getByTestId('raw-save').click()
    else await page.keyboard.press('Control+s')
    await held.sent
    await replaceRaw(page, specText({ id: 'a-1', says: 'SECOND UNSAVED EDIT WHILE SAVING' }))
    held.release()

    await expect.poll(() => diskSpec(request)).toContain('FIRST SAVE')
    await page.waitForTimeout(300) // let the response reach the editor
    await expect(rawEditor(page)).toContainText('SECOND UNSAVED EDIT WHILE SAVING')
    await expect(page.getByTestId('raw-dirty')).toBeVisible()

    // The kept text saves normally afterwards.
    await page.getByTestId('raw-save').click()
    await expect(page.getByTestId('raw-dirty')).toHaveCount(0)
    await expect.poll(() => diskSpec(request)).toContain('SECOND UNSAVED EDIT WHILE SAVING')
  })
}

// ---------------------------------------------------------------------------
// Item 5 (frontend half): saving over a file that changed on disk
// ---------------------------------------------------------------------------

test('item 5: a save over a file changed on disk is refused (409); the buffer stays until Reload', async ({ page, request }) => {
  await openApp(page)
  const tree = page.getByTestId('file-tree')
  await tree.locator('[data-path="sections/intro.tex"]').click()
  await expect(page.getByTestId('open-file-name')).toHaveText('sections/intro.tex')
  await typeAtEnd(page, 'MY UNSAVED EDIT')
  await expect(page.getByTestId('dirty-marker')).toBeVisible()

  // Someone else (another editor, git pull) rewrites the file meanwhile.
  const external = '\\section{Introduction}\nEXTERNAL VERSION\n'
  await request.put(`${MOCK_URL}/api/file`, { data: { path: 'sections/intro.tex', content: external } })

  const put = page.waitForRequest((r) => r.url().endsWith('/api/file') && r.method() === 'PUT')
  await page.keyboard.press('Control+s')
  const body = (await put).postDataJSON()
  await page.waitForTimeout(300) // let the answer arrive
  // Nothing was overwritten and nothing was lost.
  expect(await diskFile(request, 'sections/intro.tex')).toBe(external)
  expect(body).toMatchObject({ path: 'sections/intro.tex', base_sha256: expect.any(String) })
  const conflict = page.getByTestId('file-conflict')
  await expect(conflict).toContainText('File changed on disk')
  await expect(editorText(page)).toContainText('MY UNSAVED EDIT')
  await expect(page.getByTestId('dirty-marker')).toBeVisible()
  // Leaving the file would drop the edits, so it is refused too.
  await tree.locator('[data-path="sections/method.tex"]').click()
  await page.waitForTimeout(300)
  await expect(page.getByTestId('open-file-name')).toHaveText('sections/intro.tex')
  await expect(editorText(page)).toContainText('MY UNSAVED EDIT')

  // Reload is the user's choice: it shows the file on disk and clears the conflict.
  await conflict.getByTestId('conflict-reload').click()
  await expect(conflict).toHaveCount(0)
  await expect(editorText(page)).toContainText('EXTERNAL VERSION')
  await expect(editorText(page)).not.toContainText('MY UNSAVED EDIT')
  await expect(page.getByTestId('dirty-marker')).toHaveCount(0)

  // Saves are based on the reloaded revision from now on.
  await typeAtEnd(page, 'AFTER RELOAD')
  await page.keyboard.press('Control+s')
  await expect(page.getByTestId('dirty-marker')).toHaveCount(0)
  expect(await diskFile(request, 'sections/intro.tex')).toContain('EXTERNAL VERSION\nAFTER RELOAD')
})

// ---------------------------------------------------------------------------
// Item 6: undo after an external change to paper.json
// ---------------------------------------------------------------------------

const TWO_SECTIONS = specText({ id: 'intro-1', says: 'old promise' }, { id: 'method-1', says: 'method' })
const EXTERNAL = specText({ id: 'intro-1', says: 'new promise' }, { id: 'method-1', says: 'EXTERNAL CHANGE' })

async function editThenChangeExternally(page: Page, request: APIRequestContext, silent: boolean) {
  await seedSpec(request, TWO_SECTIONS)
  await openApp(page)
  await page.getByTestId('tab-spec').click()
  await saysInput(page, 'intro-1').fill('new promise')
  await expect
    .poll(async () => (await specWrites(request)).filter((w) => w.path === '/api/spec/ops').length)
    .toBe(1)
  await expect(page.getByTestId('spec-undo')).toBeEnabled()
  // Another editor (or git pull) changes a different field.
  await seedSpec(request, EXTERNAL, silent)
}

test('item 6: an external change clears undo history, so undo cannot remove it', async ({ page, request }) => {
  await editThenChangeExternally(page, request, false)
  await expect(saysInput(page, 'method-1')).toHaveValue('EXTERNAL CHANGE')

  // Ctrl-Z with the focus still in the edited row (the global spec undo key).
  await page.keyboard.press('Control+z')
  await page.waitForTimeout(300)
  expect((await specWrites(request)).filter((w) => w.path === '/api/spec')).toEqual([])
  expect(await diskSpec(request)).toContain('EXTERNAL CHANGE')
  await expect(saysInput(page, 'method-1')).toHaveValue('EXTERNAL CHANGE')
  await expect(page.getByTestId('spec-undo')).toBeDisabled()
  await expect(page.getByTestId('spec-toast')).toContainText('History cleared: paper.json changed outside the editor')
})

test('item 6: undo re-checks paper.json first and refuses when it changed unnoticed', async ({ page, request }) => {
  // `silent`: no spec.changed event, as if the socket had been down.
  await editThenChangeExternally(page, request, true)
  await page.waitForTimeout(300)
  await expect(page.getByTestId('spec-undo')).toBeEnabled() // the UI has not noticed yet

  await page.getByTestId('spec-undo').click()
  await page.waitForTimeout(300)
  expect((await specWrites(request)).filter((w) => w.path === '/api/spec')).toEqual([])
  expect(await diskSpec(request)).toContain('EXTERNAL CHANGE')
  await expect(page.getByTestId('spec-toast')).toContainText('paper.json changed outside the editor')
  // The view now shows the file as it is, with no history to undo into.
  await expect(saysInput(page, 'method-1')).toHaveValue('EXTERNAL CHANGE')
  await expect(page.getByTestId('spec-undo')).toBeDisabled()
})
