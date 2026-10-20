// Headless tests of the phase 8 UI (Revise) against the mock backend: a comment on a
// selection becomes a finding; "Rewrite…" on a finding proposes a change, shows the diff,
// and Approve writes \aigen{...} with the original kept on a "% aigen-was:" line.
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import { MOCK_URL } from '../playwright.config.ts'

const INTRO = 'sections/intro.tex'
const SENTENCE = 'Mock papers are useful for testing user interfaces.'

async function fileText(request: APIRequestContext, path: string): Promise<string> {
  return (await (await request.get(`${MOCK_URL}/api/file?path=${encodeURIComponent(path)}`)).json()).content
}

/** Select the whole line that holds `needle` in the editor and open the selection menu. */
async function selectLineAndOpenMenu(page: Page, needle: string) {
  const line = page.locator('.cm-content .cm-line').filter({ hasText: needle }).first()
  await line.click()
  await page.keyboard.press('Home')
  await page.keyboard.press('Shift+End')
  await line.click({ button: 'right' })
  await expect(page.getByTestId('editor-context-menu')).toBeVisible()
}

test.beforeEach(async ({ request }) => {
  await request.post(`${MOCK_URL}/__mock/reset`)
  await request.post(`${MOCK_URL}/__mock/revise-config`, { data: { duration_ms: 300 } })
})

test('Comment… on a selection makes a finding with a comment badge; Rewrite… proposes, diffs, and Approve writes \\aigen', async ({ page, request }) => {
  await page.goto('/')
  await page.getByTestId('file-tree').locator(`[data-path="${INTRO}"]`).click()
  await expect(page.getByTestId('open-file-name')).toHaveText(INTRO)
  await selectLineAndOpenMenu(page, 'Mock papers are useful')
  await page.getByTestId('ctx-comment').click()
  const box = page.getByTestId('comment-box')
  await expect(box).toBeVisible()
  await expect(box.getByTestId('comment-quote')).toContainText('Mock papers are useful')
  await box.getByTestId('comment-problem').fill('Too verbose; say it in one clause.')
  await box.getByTestId('comment-why').fill('The reader knows this.')
  await box.getByTestId('comment-submit').click()
  await expect(box).toHaveCount(0)
  await expect(page.getByTestId('spec-toast')).toContainText('Comment added')
  // The comment is a finding: a card with the comment badge in the editor's findings column.
  const card = page.locator('[data-testid="finding-card"]').filter({ hasText: 'Too verbose' }).first()
  await expect(card).toBeVisible()
  await expect(card.getByTestId('comment-badge')).toHaveText('comment')
  await expect(card.getByTestId('finding-problem')).toContainText('Too verbose; say it in one clause.')
  const log = await (await request.get(`${MOCK_URL}/__mock/revise-log`)).json()
  expect(log[0]).toMatchObject({ path: '/api/findings/comment', body: { file: INTRO, problem: 'Too verbose; say it in one clause.', why: 'The reader knows this.' } })

  // Rewrite… → propose → diff + editable text → approve.
  await card.getByTestId('finding-rewrite').click()
  const dialog = page.getByTestId('revision-dialog')
  await expect(dialog).toBeVisible()
  await expect(dialog.getByTestId('revision-comment')).toContainText('Too verbose')
  await dialog.getByTestId('revision-propose').click()
  await expect(dialog.getByTestId('revision-live')).toBeVisible()
  await expect(dialog.getByTestId('revision-text')).toBeVisible({ timeout: 10_000 })
  await expect(dialog.getByTestId('revision-text')).toHaveValue(/^Rewritten span after/)
  await expect(dialog.getByTestId('revision-diff').getByTestId('diff-removed').first()).toBeVisible()
  await expect(dialog.getByTestId('revision-diff').getByTestId('diff-added').first()).toContainText('Rewritten')
  await expect(dialog.getByTestId('revision-notes')).toContainText('changed only the span')
  expect(await fileText(request, INTRO)).not.toContain('aigen')
  await dialog.getByTestId('revision-text').fill('Mock papers help test UIs~\\cite{nobody}.')
  await dialog.getByTestId('revision-approve').click()
  await expect(dialog).toHaveCount(0)
  const after = await fileText(request, INTRO)
  const lines = after.split('\n')
  const was = lines.findIndex((l) => l.startsWith('% aigen-was: '))
  expect(lines[was]).toBe(`% aigen-was: ${SENTENCE}`)
  expect(lines[was + 1]).toContain('\\aigen{Mock papers help test UIs~\\cite{nobody}.}')
  expect(after.split(SENTENCE).length - 1).toBe(1) // only on the was-line
  // main.tex got the macro; the header counts the pending AI edit; the finding is addressed.
  const main = await fileText(request, 'main.tex')
  expect(main).toContain('\\providecommand{\\aigen}')
  expect(main.indexOf('\\providecommand{\\aigen}')).toBeLessThan(main.indexOf('\\begin{document}'))
  await expect(page.getByTestId('outstanding-badge')).toHaveText('1 AI edit pending')
  await expect(page.getByTestId('spec-toast')).toContainText('aigen')
  // The finding is addressed (the column's default filter shows open findings only, so ask the API).
  const findings = await (await request.get(`${MOCK_URL}/api/findings?file=${encodeURIComponent(INTRO)}`)).json()
  expect(findings.findings.find((f: { problem: string }) => f.problem.startsWith('Too verbose')).state).toBe('addressed')
})

test('Rewrite… on a model finding, the whole paragraph, and the drop case', async ({ page, request }) => {
  // A review run needs a linked section in the spec; its findings are on intro.tex:2 and :3.
  const message = { id: 'message', level: 'message', says: 'Mocks help', children: [], claims: [], fields: { problem: '', stakes: '', thesis: '' }, anchor: null, notes: '' }
  const section = { id: 'introduction', level: 'section', says: 'Why mocks', children: [], claims: [], fields: { figure_story: '' }, anchor: { file: INTRO, marker: '\\section{Introduction}' }, notes: '' }
  await request.post(`${MOCK_URL}/__mock/spec-raw`, { data: { raw: JSON.stringify({ version: 1, message, claims: [], tree: { ...message, says: '', fields: {}, children: [section] } }, null, 2) + '\n' } })
  await page.goto('/')
  await page.getByTestId('tab-findings').click()
  await page.getByTestId('pass-select').selectOption('paragraph-function')
  await page.getByTestId('run-button').click()
  const card = page.locator('[data-testid="finding-card"]').first()
  await expect(card).toBeVisible({ timeout: 10_000 })
  await card.getByTestId('finding-rewrite').click()
  const dialog = page.getByTestId('revision-dialog')
  await dialog.getByTestId('span-paragraph').check()
  await dialog.getByTestId('revision-propose').click()
  await expect(dialog.getByTestId('revision-text')).toBeVisible({ timeout: 10_000 })
  await expect(dialog.getByTestId('revision-text')).toHaveValue(/Second part of the rewritten paragraph/)
  // Clearing the text turns Approve into "drop the span".
  await dialog.getByTestId('revision-text').fill('')
  await expect(dialog.getByTestId('revision-approve')).toHaveText(/drop the span/)
  await dialog.getByTestId('revision-text').fill('One.\n\nTwo~\\cite{nobody}.')
  await dialog.getByTestId('revision-approve').click()
  await expect(dialog).toHaveCount(0)
  const after = await fileText(request, INTRO)
  expect(after).toContain('\\aigen{One.}\n\n\\aigen{Two~\\cite{nobody}.}')
  expect(after).toMatch(/% aigen-was: .*Mock papers are useful/)
  await expect(page.getByTestId('outstanding-badge')).toHaveText('2 AI edits pending')
})
