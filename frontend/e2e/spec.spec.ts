// Headless tests of the phase 3 Spec view against the mock backend.
//
// The mock keeps paper.json in memory (mock/spec.mjs) and logs every spec
// write request; GET /__mock/spec-log returns that log, so the tests can
// check exactly which ops the UI sent. Specs are seeded through
// POST /__mock/spec-raw before the page loads.
//
// Mock .tex files (see mock/server.mjs):
//   sections/intro.tex   1 \section{Introduction}
//                        2 Mock papers are useful for testing user interfaces.
//                        3 This sentence cites a missing key~\cite{nobody}.
//   sections/method.tex  1 \section{Method}
//                        2-5 one paragraph of four lines
//
// Drags use real mouse events in small steps, which is what dnd-kit's pointer
// sensor needs (it starts a drag only after the pointer moved 5 px).
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

const INTRO_ANCHOR = { file: 'sections/intro.tex', marker: '\\section{Introduction}' }

async function seedSpec(request: APIRequestContext, spec: unknown) {
  await request.post(`${MOCK_URL}/__mock/spec-raw`, { data: { raw: JSON.stringify(spec, null, 2) + '\n' } })
}

interface LogEntry {
  method: string
  path: string
  body: { op?: { type: string; [key: string]: unknown }; [key: string]: unknown }
}

async function specLog(request: APIRequestContext): Promise<LogEntry[]> {
  return (await (await request.get(`${MOCK_URL}/__mock/spec-log`)).json()) as LogEntry[]
}

/** The ops sent to POST /api/spec/ops, optionally only one type. */
async function ops(request: APIRequestContext, type?: string) {
  return (await specLog(request))
    .filter((e) => e.path === '/api/spec/ops' && (!type || e.body.op?.type === type))
    .map((e) => e.body.op!)
}

async function getSpecFile(request: APIRequestContext) {
  return (await (await request.get(`${MOCK_URL}/api/spec`)).json()) as { spec: ReturnType<typeof paperSpec>; raw: string }
}

async function getTex(request: APIRequestContext, path: string): Promise<string> {
  return ((await (await request.get(`${MOCK_URL}/api/file?path=${encodeURIComponent(path)}`)).json()) as { content: string })
    .content
}

async function putTex(request: APIRequestContext, path: string, content: string) {
  await request.put(`${MOCK_URL}/api/file`, { data: { path, content } })
}

/** Console errors other than expected 404s. */
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

async function openSpecTab(page: Page) {
  await page.goto('/')
  await expect(page.getByTestId('open-file-name')).toHaveText('main.tex')
  await page.getByTestId('tab-spec').click()
  await expect(page.getByTestId('spec-view')).toBeVisible()
  await expect(page.getByTestId('message-card')).toBeVisible()
}

const row = (page: Page, id: string) => page.locator(`[data-testid="spec-row"][data-node-id="${id}"]`)
const saysInput = (page: Page, id: string) => page.locator(`[data-testid="says-input"][data-node-id="${id}"]`)
const dot = (page: Page, id: string) => row(page, id).getByTestId('status-dot').first()
const sectionBlock = (page: Page, id: string) => page.locator(`[data-testid="section-block"][data-node-id="${id}"]`)

/** Ids of the rows on screen, in order. */
async function rowIds(page: Page): Promise<string[]> {
  return page.getByTestId('spec-row').evaluateAll((els) => els.map((e) => e.getAttribute('data-node-id') ?? ''))
}

/** Ids of the paragraph rows inside a section block, in order. */
async function paragraphIds(page: Page, sectionId: string): Promise<string[]> {
  return sectionBlock(page, sectionId)
    .locator('[data-testid="spec-row"][data-level="paragraph"]')
    .evaluateAll((els) => els.map((e) => e.getAttribute('data-node-id') ?? ''))
}

async function center(locator: Locator) {
  const box = await locator.boundingBox()
  expect(box, 'element should be on screen').not.toBeNull()
  return { x: box!.x + box!.width / 2, y: box!.y + box!.height / 2 }
}

/** Press the mouse on `from`, move past dnd-kit's 5 px threshold, then in steps to `to`. */
async function startDrag(page: Page, from: Locator) {
  const a = await center(from)
  await page.mouse.move(a.x, a.y)
  await page.mouse.down()
  await page.mouse.move(a.x + 6, a.y + 6, { steps: 3 })
  await page.mouse.move(a.x + 12, a.y + 12, { steps: 3 })
}

async function moveTo(page: Page, point: { x: number; y: number }) {
  await page.mouse.move(point.x, point.y, { steps: 15 })
  // One more small move so the collision check runs at the final spot.
  await page.mouse.move(point.x + 1, point.y + 1)
}

async function dragTo(page: Page, from: Locator, to: Locator) {
  await startDrag(page, from)
  await moveTo(page, await center(to))
  await page.mouse.up()
}

test.beforeEach(async ({ request }) => {
  await request.post(`${MOCK_URL}/__mock/reset`)
})

// ---------------------------------------------------------------------------
// Skeleton adoption, markers, status dots, editor <-> spec
// ---------------------------------------------------------------------------

test('adopt a heading and a paragraph from the skeleton; the marker shows in the editor', async ({ page, request }) => {
  const consoleErrors = collectConsoleErrors(page)
  await openSpecTab(page)
  const panel = page.getByTestId('skeleton-panel')
  await expect(panel.getByTestId('skeleton-count')).toHaveText('4 not adopted')

  // Heading -> section row with an empty says, linked to the heading.
  const heading = panel.locator('[data-testid="skeleton-heading"][data-heading="Introduction"]')
  await heading.getByTestId('adopt-heading').click()
  await expect(row(page, 'introduction-1')).toBeVisible()
  await expect(saysInput(page, 'introduction-1')).toHaveValue('')
  await expect(row(page, 'introduction-1').getByTestId('row-heading')).toHaveText('Introduction')
  await expect(dot(page, 'introduction-1')).toHaveAttribute('data-status', 'linked')
  await expect(dot(page, 'introduction-1')).toHaveAttribute('title', /linked — sections\/intro\.tex:1/)
  // Still listed (its paragraph is not adopted yet), now marked as adopted.
  await expect(heading).toHaveAttribute('data-adopted', 'introduction-1')

  // Paragraph -> paragraph row under that section, marker written into the .tex.
  const paragraph = panel.locator('[data-testid="skeleton-paragraph"][data-file="sections/intro.tex"]')
  await paragraph.getByTestId('adopt-paragraph').click()
  await expect(sectionBlock(page, 'introduction-1').locator('[data-testid="spec-row"][data-node-id="p-1"]')).toBeVisible()
  await expect(dot(page, 'p-1')).toHaveAttribute('data-status', 'linked')
  const adopts = (await specLog(request)).filter((e) => e.path === '/api/spec/adopt').map((e) => e.body)
  expect(adopts).toEqual([
    { kind: 'section', file: 'sections/intro.tex', line: 1 },
    { kind: 'paragraph', file: 'sections/intro.tex', line_start: 2, parent_id: 'introduction-1' },
  ])
  expect((await getTex(request, 'sections/intro.tex')).split('\n').slice(0, 3)).toEqual([
    '\\section{Introduction}',
    '% @p p-1',
    'Mock papers are useful for testing user interfaces.',
  ])
  // Adopted entries are hidden unless "show adopted" is on.
  await expect(panel.getByTestId('skeleton-count')).toHaveText('2 not adopted')
  await expect(heading).toHaveCount(0)
  await panel.getByTestId('skeleton-show-adopted').check()
  await expect(heading).toBeVisible()
  await expect(paragraph).toHaveAttribute('data-marker', 'p-1')

  // The editor shows the marker; clicking the dot jumps to it.
  await dot(page, 'p-1').click()
  await expect(page.getByTestId('open-file-name')).toHaveText('sections/intro.tex')
  await expect(page.locator('.cm-activeLineGutter')).toHaveText('2')
  await expect(page.locator('.cm-activeLine')).toHaveText('% @p p-1')

  // The cursor in the paragraph highlights its row in the Spec view ...
  await page.locator('.cm-content .cm-line').nth(3).click() // line 4, inside p-1
  await page.waitForTimeout(500) // the editor reports the cursor after 300 ms
  await page.getByTestId('tab-spec').click()
  await expect(row(page, 'p-1')).toHaveAttribute('data-active', 'true')
  await expect(row(page, 'introduction-1')).not.toHaveAttribute('data-active', 'true')
  // ... and on the heading line, the section's row.
  await page.getByTestId('tab-editor').click()
  await page.locator('.cm-content .cm-line').nth(0).click()
  await page.waitForTimeout(500)
  await page.getByTestId('tab-spec').click()
  await expect(row(page, 'introduction-1')).toHaveAttribute('data-active', 'true')
  expect(consoleErrors).toEqual([])
})

test('status: stale after the paragraph changes on disk, linked after reconcile or a says edit, orphaned without its marker', async ({
  page,
  request,
}) => {
  await putTex(
    request,
    'sections/intro.tex',
    '\\section{Introduction}\n% @p p-1\nMock papers are useful for testing user interfaces.\n',
  )
  await seedSpec(
    request,
    paperSpec([
      node('intro-1', 'section', 'Why mock papers', {
        anchor: INTRO_ANCHOR,
        children: [node('p-1', 'paragraph', 'Mocks help', { anchor: { file: 'sections/intro.tex', marker: '@p p-1' } })],
      }),
      node('plan-1', 'section', 'Not written yet'),
    ]),
  )
  await openSpecTab(page)
  await expect(dot(page, 'p-1')).toHaveAttribute('data-status', 'linked')
  await expect(dot(page, 'intro-1')).toHaveAttribute('data-status', 'linked')
  await expect(dot(page, 'plan-1')).toHaveAttribute('data-status', 'planned')
  await expect(dot(page, 'plan-1')).toHaveAttribute('title', /^planned/)

  // The prose changes (file.changed over the WebSocket): stale.
  await putTex(
    request,
    'sections/intro.tex',
    '\\section{Introduction}\n% @p p-1\nMock papers are useful for testing user interfaces, and cheap.\n',
  )
  await expect(dot(page, 'p-1')).toHaveAttribute('data-status', 'stale')
  await expect(dot(page, 'p-1')).toHaveAttribute('title', /stale — sections\/intro\.tex:2/)

  // ⋯ -> Reconcile: linked again, without touching the spec.
  await row(page, 'p-1').getByTestId('row-menu-button').click()
  await row(page, 'p-1').getByTestId('menu-reconcile').click()
  await expect(dot(page, 'p-1')).toHaveAttribute('data-status', 'linked')
  expect((await specLog(request)).at(-1)).toMatchObject({ method: 'POST', path: '/api/spec/nodes/p-1/reconcile' })

  // Changed again, then `says` edited: linked (the new says is the new baseline).
  await putTex(request, 'sections/intro.tex', '\\section{Introduction}\n% @p p-1\nMock papers are cheap.\n')
  await expect(dot(page, 'p-1')).toHaveAttribute('data-status', 'stale')
  await row(page, 'p-1').getByTestId('row-menu-button').click()
  await expect(row(page, 'p-1').getByTestId('menu-reconcile')).toBeVisible()
  await page.keyboard.press('Escape') // closes the menu
  await expect(row(page, 'p-1').getByTestId('row-menu')).toHaveCount(0)
  await saysInput(page, 'p-1').fill('Mocks are cheap')
  await saysInput(page, 'p-1').blur()
  await expect(dot(page, 'p-1')).toHaveAttribute('data-status', 'linked')

  // The marker line is deleted: orphaned.
  await putTex(request, 'sections/intro.tex', '\\section{Introduction}\nMock papers are cheap.\n')
  await expect(dot(page, 'p-1')).toHaveAttribute('data-status', 'orphaned')
  await expect(dot(page, 'p-1')).toHaveAttribute('title', /orphaned — @p p-1 not found in sections\/intro\.tex/)
})

test('dragging a paragraph row onto the editor inserts its marker there (via the Editor tab)', async ({ page, request }) => {
  await seedSpec(
    request,
    paperSpec([
      node('intro-1', 'section', '', {
        anchor: INTRO_ANCHOR,
        children: [node('p-1', 'paragraph', 'Mock papers are handy')],
      }),
    ]),
  )
  await page.goto('/')
  await page.getByTestId('file-tree').locator('[data-path="sections/intro.tex"]').click()
  await expect(page.getByTestId('open-file-name')).toHaveText('sections/intro.tex')
  await page.getByTestId('tab-spec').click()
  await expect(dot(page, 'p-1')).toHaveAttribute('data-status', 'planned')

  // Pick up the row, hover the Editor tab until it opens, drop on line 3.
  await startDrag(page, row(page, 'p-1').getByTestId('drag-handle'))
  await expect(page.getByTestId('drag-preview')).toContainText('Mock papers are handy')
  await moveTo(page, await center(page.getByTestId('tab-editor')))
  await expect(page.getByTestId('editor')).toBeVisible()
  await moveTo(page, await center(page.locator('.cm-content .cm-line').nth(2)))
  // Line 3 is inside the paragraph that starts on line 2: the marker goes above line 2.
  await expect(page.getByTestId('editor-drop-indicator')).toHaveAttribute('data-line', '2')
  await page.mouse.up()

  await expect(page.locator('.cm-content .cm-line').nth(1)).toHaveText('% @p p-1')
  const adopt = (await specLog(request)).filter((e) => e.path === '/api/spec/adopt').map((e) => e.body)
  expect(adopt).toEqual([{ kind: 'paragraph', file: 'sections/intro.tex', line_start: 2, id: 'p-1' }])
  await expect(page.locator('.cm-activeLineGutter')).toHaveText('2') // the editor shows the new marker
  await page.getByTestId('tab-spec').click()
  await expect(dot(page, 'p-1')).toHaveAttribute('data-status', 'linked')
})

test('⋯ → Insert marker at the editor cursor, with says as a comment', async ({ page, request }) => {
  await seedSpec(
    request,
    paperSpec([node('method-1', 'section', '', { children: [node('p-1', 'paragraph', 'The method in four steps')] })]),
  )
  await page.goto('/')
  await page.getByTestId('file-tree').locator('[data-path="sections/method.tex"]').click()
  await page.locator('.cm-content .cm-line').nth(3).click() // line 4, inside the paragraph of lines 2-5
  await page.waitForTimeout(500)
  await page.getByTestId('tab-spec').click()
  await row(page, 'p-1').getByTestId('row-menu-button').click()
  await row(page, 'p-1').getByTestId('menu-marker-says').check()
  await row(page, 'p-1').getByTestId('menu-insert-marker').click()

  await expect(page.getByTestId('open-file-name')).toHaveText('sections/method.tex')
  await expect(page.locator('.cm-content .cm-line').nth(1)).toHaveText('% @p p-1')
  await expect(page.locator('.cm-content .cm-line').nth(2)).toHaveText('% says: The method in four steps')
  await expect(page.locator('.cm-content .cm-line').nth(3)).toHaveText('We describe the method here.')
  expect((await getTex(request, 'sections/method.tex')).split('\n').slice(0, 4)).toEqual([
    '\\section{Method}',
    '% @p p-1',
    '% says: The method in four steps',
    'We describe the method here.',
  ])
  // The comment does not make the paragraph stale.
  await page.getByTestId('tab-spec').click()
  await expect(dot(page, 'p-1')).toHaveAttribute('data-status', 'linked')
})

// ---------------------------------------------------------------------------
// Editing in the outliner
// ---------------------------------------------------------------------------

test('typing in says sends one update op after the pause; paper.json follows', async ({ page, request }) => {
  await seedSpec(request, paperSpec([node('intro-1', 'section')]))
  await openSpecTab(page)
  await saysInput(page, 'intro-1').click()
  await page.keyboard.type('Cold starts are frequent', { delay: 25 })
  await expect.poll(async () => (await ops(request, 'update')).length).toBe(1)
  await page.waitForTimeout(700)
  expect(await ops(request)).toEqual([{ type: 'update', id: 'intro-1', says: 'Cold starts are frequent' }])
  // Leaving the field sends nothing more (the text is already saved).
  await page.keyboard.press('Escape')
  await expect(saysInput(page, 'intro-1')).not.toBeFocused()
  await page.waitForTimeout(500)
  expect(await ops(request)).toHaveLength(1)
  expect((await getSpecFile(request)).spec.tree.children[0].says).toBe('Cold starts are frequent')

  // Message and fields edit the same way.
  await page.getByTestId('message-says').fill('Prefetching beats warm pools')
  await page.getByTestId('message-field-thesis').fill('Predict the pages')
  await page.getByTestId('message-field-thesis').blur()
  await expect.poll(async () => (await getSpecFile(request)).spec.message.fields.thesis).toBe('Predict the pages')
  expect((await getSpecFile(request)).spec.message.says).toBe('Prefetching beats warm pools')
})

test('Enter adds a sibling after the row and focuses it; Backspace on an empty row deletes it', async ({ page, request }) => {
  await seedSpec(
    request,
    paperSpec([
      node('intro-1', 'section', 'Intro', {
        children: [node('p-1', 'paragraph', 'first'), node('p-2', 'paragraph', 'third')],
      }),
    ]),
  )
  await openSpecTab(page)
  await saysInput(page, 'p-1').click()
  await page.keyboard.press('End')
  await page.keyboard.press('Enter')
  await expect(saysInput(page, 'p-3')).toBeFocused()
  expect(await ops(request)).toEqual([{ type: 'insert', parent_id: 'intro-1', index: 1, level: 'paragraph' }])
  expect(await paragraphIds(page, 'intro-1')).toEqual(['p-1', 'p-3', 'p-2'])
  await page.keyboard.type('second')
  await page.keyboard.press('Escape')
  await expect.poll(async () => (await ops(request, 'update')).at(-1)).toEqual({ type: 'update', id: 'p-3', says: 'second' })

  // Enter on a section adds a section after it.
  await saysInput(page, 'intro-1').click()
  await page.keyboard.press('Enter')
  await expect(saysInput(page, 'section-1')).toBeFocused()
  expect((await ops(request)).at(-1)).toEqual({ type: 'insert', parent_id: 'message', index: 1, level: 'section' })

  // Backspace on the new empty section deletes it and focuses the row above.
  await page.keyboard.press('Backspace')
  await expect(row(page, 'section-1')).toHaveCount(0)
  expect((await ops(request)).at(-1)).toEqual({ type: 'delete', id: 'section-1' })
  await expect(saysInput(page, 'p-2')).toBeFocused()

  // Alt-Up moves a paragraph up among its siblings; Arrow keys move the focus.
  await page.keyboard.press('Alt+ArrowUp')
  await expect.poll(() => paragraphIds(page, 'intro-1')).toEqual(['p-1', 'p-2', 'p-3'])
  expect((await ops(request)).at(-1)).toEqual({ type: 'move', id: 'p-2', parent_id: 'intro-1', index: 1 })
  await expect(saysInput(page, 'p-2')).toBeFocused()
  await page.keyboard.press('Alt+ArrowDown')
  await expect.poll(() => paragraphIds(page, 'intro-1')).toEqual(['p-1', 'p-3', 'p-2'])
  await page.keyboard.press('ArrowUp')
  await expect(saysInput(page, 'p-3')).toBeFocused()
})

test('Tab / Shift-Tab change the level; notes keep the displaced fields', async ({ page, request }) => {
  await seedSpec(
    request,
    paperSpec([
      node('intro-1', 'section', 'Intro', {
        children: [node('p-1', 'paragraph', 'Cold starts hurt', { fields: { function: 'motivates the problem' } })],
      }),
      node('method-1', 'section', 'Method', { fields: { figure_story: 'Fig. 2 shows the pipeline' } }),
    ]),
  )
  await openSpecTab(page)

  // Shift-Tab: paragraph -> section, placed right after its old section.
  await saysInput(page, 'p-1').click()
  await page.keyboard.press('Shift+Tab')
  await expect(row(page, 'p-1')).toHaveAttribute('data-level', 'section')
  expect((await ops(request)).at(-1)).toEqual({ type: 'set_level', id: 'p-1', level: 'section' })
  expect(await rowIds(page)).toEqual(['intro-1', 'p-1', 'method-1'])
  await expect(saysInput(page, 'p-1')).toBeFocused()
  await expect(saysInput(page, 'p-1')).toHaveValue('Cold starts hurt') // says is kept
  await row(page, 'p-1').getByTestId('row-expander').click()
  await expect(row(page, 'p-1').getByTestId('notes-input')).toHaveValue('function: motivates the problem')
  await expect(row(page, 'p-1').getByTestId('field-figure_story')).toHaveValue('')

  // Tab: back to a paragraph, at the end of the section before it.
  await saysInput(page, 'p-1').click()
  await page.keyboard.press('Tab')
  await expect(row(page, 'p-1')).toHaveAttribute('data-level', 'paragraph')
  expect(await paragraphIds(page, 'intro-1')).toEqual(['p-1'])
  await expect(saysInput(page, 'p-1')).toBeFocused()
  await expect(row(page, 'p-1').getByTestId('notes-input')).toHaveValue('function: motivates the problem')

  // A section with paragraphs cannot be demoted: a toast, no op.
  const before = (await ops(request)).length
  await saysInput(page, 'intro-1').click()
  await page.keyboard.press('Tab')
  await expect(page.getByTestId('spec-toast')).toContainText('move or delete them')
  expect(await ops(request)).toHaveLength(before)

  // An empty section can: its figure_story moves to notes.
  await saysInput(page, 'method-1').click()
  await page.keyboard.press('Tab')
  await expect(row(page, 'method-1')).toHaveAttribute('data-level', 'paragraph')
  expect(await paragraphIds(page, 'intro-1')).toEqual(['p-1', 'method-1'])
  await row(page, 'method-1').getByTestId('row-expander').click()
  await expect(row(page, 'method-1').getByTestId('notes-input')).toHaveValue('figure_story: Fig. 2 shows the pipeline')
  await expect(row(page, 'method-1').getByTestId('field-function')).toHaveValue('')
})

test('Ctrl-Z undoes the last spec change by PUTting the previous spec; Ctrl-Shift-Z redoes it', async ({ page, request }) => {
  await seedSpec(request, paperSpec([node('intro-1', 'section', 'old promise')]))
  await openSpecTab(page)
  await saysInput(page, 'intro-1').fill('new promise')
  await expect.poll(async () => (await ops(request, 'update')).length).toBe(1)

  // Undo while the input still has focus: the spec, not the input's text, is undone.
  await page.keyboard.press('Control+z')
  await expect(saysInput(page, 'intro-1')).toHaveValue('old promise')
  const undo = (await specLog(request)).at(-1)!
  expect(undo).toMatchObject({ method: 'PUT', path: '/api/spec' })
  expect((undo.body.spec as ReturnType<typeof paperSpec>).tree.children[0].says).toBe('old promise')
  expect((await getSpecFile(request)).spec.tree.children[0].says).toBe('old promise')

  await page.keyboard.press('Control+Shift+z')
  await expect(saysInput(page, 'intro-1')).toHaveValue('new promise')
  expect((await getSpecFile(request)).spec.tree.children[0].says).toBe('new promise')

  // Structural changes undo too; text typed but not yet sent is sent first, so
  // one Ctrl-Z reverts exactly that typing.
  await saysInput(page, 'intro-1').press('Enter')
  await expect(row(page, 'section-1')).toBeVisible()
  await page.keyboard.type('typed then undone')
  await page.keyboard.press('Control+z')
  await expect(saysInput(page, 'section-1')).toHaveValue('')
  await page.keyboard.press('Control+z')
  await expect(row(page, 'section-1')).toHaveCount(0)
  await expect(page.getByTestId('spec-redo')).toBeEnabled()
})

// ---------------------------------------------------------------------------
// Claims and drag and drop
// ---------------------------------------------------------------------------

test('drag a claim chip onto a section and a paragraph to link it; × unlinks', async ({ page, request }) => {
  await seedSpec(
    request,
    paperSpec(
      [node('intro-1', 'section', 'Intro', { children: [node('p-1', 'paragraph', 'Warm pools idle')] })],
      [node('c1', 'claim', 'Warm pools waste memory')],
    ),
  )
  await openSpecTab(page)
  const chip = page.locator('[data-testid="claim-card"][data-claim-id="c1"]').getByTestId('claim-chip')
  await expect(chip).toHaveText('c1')

  await dragTo(page, chip, sectionBlock(page, 'intro-1').getByTestId('section-header'))
  await expect(row(page, 'intro-1').locator('[data-testid="linked-claim"][data-claim-id="c1"]')).toBeVisible()
  expect(await ops(request)).toEqual([{ type: 'link_claim', id: 'intro-1', claim_id: 'c1' }])

  await dragTo(page, chip, row(page, 'p-1'))
  await expect(row(page, 'p-1').locator('[data-testid="linked-claim"][data-claim-id="c1"]')).toBeVisible()
  expect((await ops(request)).at(-1)).toEqual({ type: 'link_claim', id: 'p-1', claim_id: 'c1' })
  await expect(page.locator('[data-testid="claim-card"][data-claim-id="c1"]')).toContainText('linked ×2')

  await row(page, 'intro-1').getByRole('button', { name: 'Unlink c1 from intro-1' }).click()
  await expect(row(page, 'intro-1').getByTestId('linked-claim')).toHaveCount(0)
  expect((await ops(request)).at(-1)).toEqual({ type: 'unlink_claim', id: 'intro-1', claim_id: 'c1' })
})

test('claim bank: add, edit, lift a row to a claim, delete with confirm', async ({ page, request }) => {
  await seedSpec(request, paperSpec([node('intro-1', 'section', 'Snapshots fault pages lazily')]))
  await openSpecTab(page)
  await page.getByTestId('add-claim').click()
  const card = page.locator('[data-testid="claim-card"][data-claim-id="c1"]')
  await expect(card.getByTestId('claim-says')).toBeFocused()
  await page.keyboard.type('Page sets are stable')
  await card.getByTestId('claim-field-evidence').fill('Fig. 3')
  await card.getByTestId('claim-field-evidence').blur()
  await expect.poll(async () => (await getSpecFile(request)).spec.claims[0]).toMatchObject({
    says: 'Page sets are stable',
    fields: { evidence: 'Fig. 3', scope: '' },
  })

  // Lift to claim copies says into a new claim and links it.
  await row(page, 'intro-1').getByTestId('row-menu-button').click()
  await row(page, 'intro-1').getByTestId('menu-lift').click()
  const lifted = page.locator('[data-testid="claim-card"][data-claim-id="c2"]')
  await expect(lifted.getByTestId('claim-says')).toHaveValue('Snapshots fault pages lazily')
  await expect(row(page, 'intro-1').locator('[data-testid="linked-claim"][data-claim-id="c2"]')).toBeVisible()

  // Delete needs a second click, and unlinks the claim everywhere.
  await lifted.getByTestId('claim-delete').click()
  await expect(lifted).toBeVisible()
  await lifted.getByTestId('claim-delete-confirm').click()
  await expect(lifted).toHaveCount(0)
  await expect(row(page, 'intro-1').getByTestId('linked-claim')).toHaveCount(0)
  expect((await ops(request)).at(-1)).toEqual({ type: 'delete_claim', id: 'c2' })
})

test('drag rows: reorder within a section, move to another section, reorder sections', async ({ page, request }) => {
  await seedSpec(
    request,
    paperSpec([
      node('intro-1', 'section', 'Intro', {
        children: [node('p-1', 'paragraph', 'one'), node('p-2', 'paragraph', 'two'), node('p-3', 'paragraph', 'three')],
      }),
      node('method-1', 'section', 'Method', { children: [node('p-4', 'paragraph', 'four')] }),
    ]),
  )
  await openSpecTab(page)

  // p-1 dropped on p-3: takes its place (last).
  await dragTo(page, row(page, 'p-1').getByTestId('drag-handle'), row(page, 'p-3'))
  await expect.poll(() => paragraphIds(page, 'intro-1')).toEqual(['p-2', 'p-3', 'p-1'])
  expect((await ops(request)).at(-1)).toEqual({ type: 'move', id: 'p-1', parent_id: 'intro-1', index: 2 })

  // p-2 dropped on the Method header: appended to Method.
  await dragTo(page, row(page, 'p-2').getByTestId('drag-handle'), sectionBlock(page, 'method-1').getByTestId('section-header'))
  await expect.poll(() => paragraphIds(page, 'method-1')).toEqual(['p-4', 'p-2'])
  expect((await ops(request)).at(-1)).toEqual({ type: 'move', id: 'p-2', parent_id: 'method-1', index: 1 })

  // Method dragged onto Intro: sections swap.
  await dragTo(page, row(page, 'method-1').getByTestId('drag-handle'), row(page, 'intro-1'))
  await expect.poll(() => rowIds(page)).toEqual(['method-1', 'p-4', 'p-2', 'intro-1', 'p-3', 'p-1'])
  expect((await ops(request)).at(-1)).toEqual({ type: 'move', id: 'method-1', parent_id: 'message', index: 0 })
})

// ---------------------------------------------------------------------------
// Raw JSON tab
// ---------------------------------------------------------------------------

test('Raw JSON: an invalid document shows the 422 errors and writes nothing; a valid one saves', async ({ page, request }) => {
  await seedSpec(request, paperSpec([node('intro-1', 'section', 'Intro')]))
  await openSpecTab(page)
  await page.getByRole('tab', { name: 'Raw JSON' }).click()
  const raw = page.getByTestId('raw-json')
  await expect(raw.locator('.cm-content')).toContainText('"version": 1')
  const before = (await getSpecFile(request)).raw

  await raw.locator('.cm-content').click()
  await page.keyboard.press('Control+a')
  await page.keyboard.insertText('{"version": 2, "message": {"id": "message", "level": "paragraph", "says": ""}}')
  await expect(raw.getByTestId('raw-dirty')).toBeVisible()
  await raw.getByTestId('raw-save').click()
  const errors = raw.getByTestId('raw-errors')
  await expect(errors).toContainText('Not saved')
  await expect(errors).toContainText('version: must be 1')
  await expect(errors).toContainText('message.level')
  expect((await getSpecFile(request)).raw).toBe(before)
  await expect(raw.getByTestId('raw-dirty')).toBeVisible() // the text is kept for fixing

  // A valid edit saves; the editor then shows the canonical text and the outliner follows.
  const fixed = paperSpec([node('intro-1', 'section', 'Intro, edited by hand')])
  await raw.locator('.cm-content').click()
  await page.keyboard.press('Control+a')
  await page.keyboard.insertText(JSON.stringify(fixed))
  await raw.getByTestId('raw-save').click()
  await expect(errors).toHaveCount(0)
  await expect(raw.getByTestId('raw-dirty')).toHaveCount(0)
  await expect(raw.locator('.cm-content')).toContainText('"says": "Intro, edited by hand"')
  expect((await getSpecFile(request)).spec.tree.children[0].says).toBe('Intro, edited by hand')
  await page.getByRole('tab', { name: 'Outliner' }).click()
  await expect(saysInput(page, 'intro-1')).toHaveValue('Intro, edited by hand')
})

test('an invalid paper.json on disk: the outliner says so and Raw JSON lists the errors', async ({ page, request }) => {
  await request.post(`${MOCK_URL}/__mock/spec-raw`, { data: { raw: '{"version": 1, "claims": [' } })
  await page.goto('/')
  await page.getByTestId('tab-spec').click()
  await expect(page.getByTestId('outliner')).toContainText('paper.json is missing or invalid')
  await expect(page.getByTestId('message-card')).toHaveCount(0)
  await page.getByRole('tab', { name: 'Raw JSON' }).click()
  await expect(page.getByTestId('raw-errors')).toContainText('Invalid JSON')
})
