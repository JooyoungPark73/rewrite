// Headless tests of the phase 7 UI against the mock backend: "Draft from spec…" on a
// paragraph row, the dialog's Generate / Replace paragraph / Insert at cursor, and the
// writes they make (mock/drafts.mjs edits the fake .tex the way the backend does).
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import { MOCK_URL } from '../playwright.config.ts'

const INTRO = 'sections/intro.tex'

async function seedSpec(request: APIRequestContext, spec: unknown) {
  await request.post(`${MOCK_URL}/__mock/spec-raw`, { data: { raw: JSON.stringify(spec, null, 2) + '\n' } })
}

function node(id: string, level: string, says: string, extra: Record<string, unknown> = {}) {
  const fields = level === 'section' ? { figure_story: '' } : level === 'paragraph' ? { function: '' } : {}
  return { id, level, says, children: [], claims: [], fields, anchor: null, notes: '', ...extra }
}

function paperSpec(sections: unknown[]) {
  const message = { id: 'message', level: 'message', says: 'Mocks help', children: [], claims: [], fields: { problem: '', stakes: '', thesis: '' }, anchor: null, notes: '' }
  return { version: 1, message, claims: [], tree: { ...message, says: '', fields: {}, children: sections } }
}

const row = (page: Page, id: string) => page.locator(`[data-testid="spec-row"][data-node-id="${id}"]`)
const dot = (page: Page, id: string) => row(page, id).getByTestId('status-dot').first()

async function fileText(request: APIRequestContext, path: string): Promise<string> {
  return (await (await request.get(`${MOCK_URL}/api/file?path=${encodeURIComponent(path)}`)).json()).content
}

test.beforeEach(async ({ request }) => {
  await request.post(`${MOCK_URL}/__mock/reset`)
  await request.post(`${MOCK_URL}/__mock/drafts-config`, { data: { duration_ms: 300 } })
})

test('Draft from spec: Generate, edit, Replace paragraph writes under the marker and the node turns stale', async ({ page, request }) => {
  await seedSpec(request, paperSpec([node('intro-1', 'section', 'Intro', { anchor: { file: INTRO, marker: '\\section{Introduction}' } })]))
  // Adopt the first intro paragraph so p-1 has a marker in the fake file.
  const skeleton = await (await request.get(`${MOCK_URL}/api/spec/skeleton`)).json()
  const para = skeleton.paragraphs.find((p: { file: string; section_heading: string | null }) => p.file === INTRO && p.section_heading)
  const adopted = await (await request.post(`${MOCK_URL}/api/spec/adopt`, { data: { kind: 'paragraph', file: INTRO, line_start: para.line_start, says: 'Mock papers are useful' } })).json()
  const p1 = adopted.new_id as string

  await page.goto('/')
  await page.getByTestId('tab-spec').click()
  await expect(dot(page, p1)).toHaveAttribute('data-status', 'linked')
  await row(page, p1).getByTestId('row-menu-button').click()
  await page.getByTestId('menu-draft').click()
  const dialog = page.getByTestId('draft-dialog')
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText(`Draft from spec: ${p1}`)
  await expect(dialog).toContainText('says: Mock papers are useful')
  await expect(dialog.getByTestId('draft-replace')).toHaveCount(0) // nothing drafted yet

  await dialog.getByTestId('draft-generate').click()
  await expect(dialog.getByTestId('draft-live')).toBeVisible()
  const text = dialog.getByTestId('draft-text')
  await expect(text).toBeVisible({ timeout: 10_000 })
  await expect(text).toHaveValue(/^Drafted: Mock papers are useful, as \\textsc\{Mock\} shows~\\cite\{nobody\}\.$/)
  await expect(dialog.getByTestId('draft-notes')).toContainText('nothing left open')
  // The draft went through POST /api/drafts with the chosen settings; nothing was written yet.
  const log = await (await request.get(`${MOCK_URL}/__mock/drafts-log`)).json()
  expect(log[0]).toMatchObject({ method: 'POST', path: '/api/drafts', body: { node_id: p1 } })
  expect(await fileText(request, INTRO)).not.toContain('drafted by')
  // Insert is for planned nodes; Replace applies here. Edit, then replace.
  await expect(dialog.getByTestId('draft-insert')).toBeDisabled()
  await text.fill('Drafted and edited by the author~\\cite{nobody}.')
  await dialog.getByTestId('draft-replace').click()
  await expect(dialog).toHaveCount(0)
  const after = await fileText(request, INTRO)
  const lines = after.split('\n')
  const marker = lines.findIndex((l) => l.trim() === `% @p ${p1}`)
  expect(lines[marker + 1]).toMatch(/^% drafted by claude, draft drf\d+: review before keeping$/)
  expect(lines[marker + 2]).toBe('Drafted and edited by the author~\\cite{nobody}.')
  expect(after).not.toContain('Mock papers are useful for testing user interfaces.')
  // The editor opened the file at the new text and the node is stale (re-read it).
  await expect(page.getByTestId('open-file-name')).toHaveText(INTRO)
  await expect(page.getByTestId('spec-toast')).toContainText('marked stale')
  await page.getByTestId('tab-spec').click()
  await expect(dot(page, p1)).toHaveAttribute('data-status', 'stale')
})

test('Draft from spec: a planned paragraph is inserted at the editor cursor with its marker and linked', async ({ page, request }) => {
  await seedSpec(
    request,
    paperSpec([
      node('intro-1', 'section', 'Intro', {
        anchor: { file: INTRO, marker: '\\section{Introduction}' },
        children: [node('p-9', 'paragraph', 'A closing remark for the intro')],
      }),
    ]),
  )
  await page.goto('/')
  await page.getByTestId('file-tree').locator(`[data-path="${INTRO}"]`).click()
  await expect(page.getByTestId('open-file-name')).toHaveText(INTRO)
  // Put the cursor on the last line of the file.
  await page.locator('[data-testid="editor"] .cm-content').click()
  await page.keyboard.press('ControlOrMeta+End')
  const lineCount = (await fileText(request, INTRO)).split('\n').length
  await page.getByTestId('tab-spec').click()
  await expect(dot(page, 'p-9')).toHaveAttribute('data-status', 'planned')
  await row(page, 'p-9').getByTestId('row-menu-button').click()
  await page.getByTestId('menu-draft').click()
  const dialog = page.getByTestId('draft-dialog')
  await dialog.getByTestId('draft-generate').click()
  await expect(dialog.getByTestId('draft-text')).toBeVisible({ timeout: 10_000 })
  await expect(dialog.getByTestId('draft-replace')).toBeDisabled()
  await expect(dialog.getByTestId('draft-insert')).toContainText(`intro.tex:${lineCount}`)
  await dialog.getByTestId('draft-insert').click()
  await expect(dialog).toHaveCount(0)
  const after = await fileText(request, INTRO)
  const lines = after.split('\n')
  const marker = lines.findIndex((l) => l.trim() === '% @p p-9')
  expect(marker).toBeGreaterThan(0)
  expect(lines[marker + 1]).toContain('% drafted by claude')
  expect(lines[marker + 2]).toBe('Drafted: A closing remark for the intro, as \\textsc{Mock} shows~\\cite{nobody}.')
  // Re-linked through adopt {id}: the node is linked and there is exactly one marker.
  await page.getByTestId('tab-spec').click()
  await expect(dot(page, 'p-9')).toHaveAttribute('data-status', 'linked')
  expect(after.split('% @p p-9').length - 1).toBe(1)
})

test('Draft from spec: a failed or cancelled draft is reported and nothing is written', async ({ page, request }) => {
  await seedSpec(request, paperSpec([node('intro-1', 'section', 'Intro', { children: [node('p-9', 'paragraph', 'Remark')] })]))
  await request.post(`${MOCK_URL}/__mock/drafts-config`, { data: { duration_ms: 5000 } })
  await page.goto('/')
  await page.getByTestId('tab-spec').click()
  await row(page, 'p-9').getByTestId('row-menu-button').click()
  await page.getByTestId('menu-draft').click()
  const dialog = page.getByTestId('draft-dialog')
  await dialog.getByTestId('draft-generate').click()
  await expect(dialog.getByTestId('draft-live')).toBeVisible()
  // Cancel the run from the API (the run list offers the same button).
  const drafts = await (await request.get(`${MOCK_URL}/api/drafts`)).json()
  await request.post(`${MOCK_URL}/api/runs/${drafts.drafts[0].run_id}/cancel`)
  await expect(dialog.getByTestId('draft-failed')).toContainText('cancelled', { timeout: 10_000 })
  await expect(dialog.getByTestId('draft-text')).toHaveCount(0)
  expect(await fileText(request, INTRO)).not.toContain('drafted by')
})
