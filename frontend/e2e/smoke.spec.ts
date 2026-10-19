// Headless smoke test of the phase 1 UI against the mock backend.
//
// Covers: the file tree, opening a file, edit + Ctrl-S (PUT /api/file, dirty
// marker clears, debounced compile follows), Compile -> error list -> click
// opens the right file at the right line, PDF rendering, the Open dialog's
// inline error, and the narrow-screen PDF tab.
import { expect, test, type Page } from '@playwright/test'
import { MOCK_URL } from '../playwright.config.ts'

// Console errors other than the expected 404 for /api/pdf before the first
// compile mean something is broken (e.g. the pdf.js worker failed to load).
function collectConsoleErrors(page: Page): string[] {
  const errors: string[] = []
  page.on('console', (msg) => {
    if (msg.type() === 'error' && !/404 \(Not Found\)/.test(msg.text())) errors.push(msg.text())
  })
  page.on('pageerror', (err) => errors.push(err.message))
  return errors
}

test.beforeEach(async ({ request }) => {
  await request.post(`${MOCK_URL}/__mock/reset`)
})

test('file tree lists main.tex and both \\input files', async ({ page }) => {
  await page.goto('/')
  const tree = page.getByTestId('file-tree')
  await expect(tree.locator('[data-path="main.tex"]')).toBeVisible()
  await expect(tree.locator('[data-path="sections/intro.tex"]')).toBeVisible()
  await expect(tree.locator('[data-path="sections/method.tex"]')).toBeVisible()
  // main.tex is marked, and opened by default.
  await expect(tree.locator('[data-path="main.tex"]')).toContainText('main')
  await expect(page.getByTestId('open-file-name')).toHaveText('main.tex')
  // Health chips render from GET /api/health.
  await expect(page.getByTestId('health-chips')).toContainText('latexmk')
  await expect(page.getByTestId('socket-status')).toHaveAttribute('title', 'Live updates: open')
})

test('open a file, edit, Ctrl-S saves, dirty marker clears, compile follows', async ({ page, request }) => {
  const consoleErrors = collectConsoleErrors(page)
  await page.goto('/')
  await page.getByTestId('file-tree').locator('[data-path="sections/intro.tex"]').click()
  await expect(page.getByTestId('open-file-name')).toHaveText('sections/intro.tex')
  const content = page.locator('.cm-content')
  await expect(content).toContainText('\\section{Introduction}')

  await content.click()
  await page.keyboard.press('ControlOrMeta+End')
  await page.keyboard.type('Added by the smoke test.')
  await expect(page.getByTestId('dirty-marker')).toBeVisible()

  const put = page.waitForRequest((r) => r.url().endsWith('/api/file') && r.method() === 'PUT')
  // Save should be followed by a compile after the 800 ms debounce, with no click.
  const autoCompile = page.waitForRequest((r) => r.url().endsWith('/api/compile') && r.method() === 'POST')
  await page.keyboard.press('ControlOrMeta+s')
  await put
  await expect(page.getByTestId('dirty-marker')).toHaveCount(0)

  const saves = (await (await request.get(`${MOCK_URL}/__mock/saves`)).json()) as { path: string; content: string }[]
  expect(saves).toHaveLength(1)
  expect(saves[0].path).toBe('sections/intro.tex')
  expect(saves[0].content).toContain('Added by the smoke test.')

  await autoCompile
  await expect(page.getByTestId('error-count')).toHaveText('1')
  expect(consoleErrors).toEqual([])
})

test('Compile shows the error; clicking it opens the file at the line', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByTestId('open-file-name')).toHaveText('main.tex')
  await page.getByRole('button', { name: 'Compile' }).click()

  const row = page.getByTestId('error-row').filter({ hasText: 'Undefined control sequence' })
  await expect(row).toContainText('sections/method.tex:4')
  await row.click()

  await expect(page.getByTestId('open-file-name')).toHaveText('sections/method.tex')
  // The cursor line is highlighted in both the gutter and the text.
  await expect(page.locator('.cm-activeLineGutter')).toHaveText('4')
  await expect(page.locator('.cm-activeLine')).toContainText('\\undefinedmacro')
})

test('PDF pane shows a placeholder before compiling and pages after', async ({ page }) => {
  const consoleErrors = collectConsoleErrors(page)
  await page.goto('/')
  const viewer = page.getByTestId('pdf-viewer')
  await expect(viewer).toContainText('No PDF yet — compile')

  await page.getByRole('button', { name: 'Compile' }).click()
  await expect(page.getByTestId('pdf-page-count')).toHaveText('2 pages')
  const canvas = viewer.locator('canvas').first()
  await expect(canvas).toBeVisible()
  const box = await canvas.boundingBox()
  expect(box?.width ?? 0).toBeGreaterThan(0)
  expect(box?.height ?? 0).toBeGreaterThan(0)
  // The bitmap is drawn (width set by the renderer) once pdf.js finishes.
  await expect.poll(() => canvas.evaluate((c) => (c as HTMLCanvasElement).width)).toBeGreaterThan(0)
  // And it is not blank: the mock PDF has a blue bar near the top.
  const hasInk = await canvas.evaluate((c) => {
    const el = c as HTMLCanvasElement
    const data = el.getContext('2d')!.getImageData(0, 0, el.width, el.height).data
    for (let i = 0; i < data.length; i += 4) if (data[i] < 200 || data[i + 1] < 200) return true
    return false
  })
  expect(hasInk).toBe(true)
  expect(consoleErrors).toEqual([])
})

test('Open dialog shows the backend detail error inline', async ({ page }) => {
  await page.goto('/')
  await page.getByTestId('repo-path').click()
  const input = page.getByLabel('Repo path')
  await input.fill('/no/such/repo')
  await page.getByRole('button', { name: 'Open', exact: true }).click()
  await expect(page.getByTestId('open-repo-error')).toContainText('has no .rewrite/ directory')

  // Init + Open on the same path succeeds and closes the dialog.
  await page.getByRole('button', { name: 'Init + Open' }).click()
  await expect(page.getByTestId('open-repo-error')).toHaveCount(0)
  await expect(page.getByTestId('repo-path')).toHaveText('/no/such/repo')
})

test('narrow screens show the PDF only in the PDF tab', async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 800 })
  await page.goto('/')
  await expect(page.getByTestId('editor')).toBeVisible()
  await expect(page.getByTestId('pdf-viewer')).toBeHidden()
  await page.getByRole('tab', { name: 'PDF' }).click()
  await expect(page.getByTestId('pdf-viewer')).toBeVisible()
  await expect(page.getByTestId('editor')).toBeHidden()
})
