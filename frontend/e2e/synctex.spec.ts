// Headless tests of the phase 2 UI against the mock backend: SyncTeX in both
// directions and the Settings dialog (compiler kind, TeX Live year, docker pull).
//
// The mock's SyncTeX is a formula, not a real .synctex.gz (see the header of
// mock/server.mjs): line L of a file maps to one box at
//   x = 72, y = 100 + 14 * (L - 1), w = 468, h = 10   (PDF points, top-left origin)
// on page 2 for sections/method.tex and page 1 otherwise; the reverse
// direction inverts it (page 1 -> intro.tex, page 2 -> method.tex). The tests
// check positions in CSS pixels, so they also check the point <-> pixel
// conversion at whatever scale "fit width" picked.
import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test'
import { MOCK_URL } from '../playwright.config.ts'

// Must match mock/server.mjs.
const SYNC_TOP = 100
const SYNC_STEP = 14
const SYNC_X = 72
const SYNC_W = 468
const SYNC_H = 10
const PAGE_WIDTH_PT = 612

/** Allowed error, in CSS pixels, between where a box is and where the formula puts it. */
const TOLERANCE_PX = 1.5

type SyncRequest =
  | { kind: 'forward'; file: string; line: number; col: number }
  | { kind: 'reverse'; page: number; x: number; y: number }

async function syncRequests(request: APIRequestContext): Promise<SyncRequest[]> {
  return (await (await request.get(`${MOCK_URL}/__mock/synctex`)).json()) as SyncRequest[]
}

async function forwardRequests(request: APIRequestContext) {
  return (await syncRequests(request)).filter((r) => r.kind === 'forward')
}

/** Console errors other than expected 404s (SyncTeX "no match", PDF before compile). */
function collectConsoleErrors(page: Page): string[] {
  const errors: string[] = []
  page.on('console', (msg) => {
    if (msg.type() === 'error' && !/404 \(Not Found\)/.test(msg.text())) errors.push(msg.text())
  })
  page.on('pageerror', (err) => errors.push(err.message))
  return errors
}

/** Load the app and compile, so a PDF (and the mock's SyncTeX data) exists. */
async function openAppWithPdf(page: Page) {
  await page.goto('/')
  await expect(page.getByTestId('open-file-name')).toHaveText('main.tex')
  await page.getByRole('button', { name: 'Compile' }).click()
  await expect(page.getByTestId('pdf-page-count')).toHaveText('2 pages')
  await expect(page.getByTestId('sync-forward')).toBeEnabled()
}

async function openFile(page: Page, path: string) {
  await page.getByTestId('file-tree').locator(`[data-path="${path}"]`).click()
  await expect(page.getByTestId('open-file-name')).toHaveText(path)
}

/** Click on line `n` (1-based) of the editor. */
async function clickLine(page: Page, n: number) {
  await page.locator('.cm-content .cm-line').nth(n - 1).click()
}

/** Bounding box of an element; fails the test if it is not rendered. */
async function rect(locator: Locator) {
  const box = await locator.boundingBox()
  expect(box, 'element should be rendered').not.toBeNull()
  return box!
}

/** The PDF page box and the scale (CSS px per point) it is drawn at. */
async function pageGeometry(page: Page, pageNumber: number) {
  const box = await rect(page.locator(`[data-page="${pageNumber}"]`))
  return { box, scale: box.width / PAGE_WIDTH_PT }
}

/** Assert the highlight box for `line` sits where the mock formula puts it. */
async function expectBoxAtLine(page: Page, pageNumber: number, line: number) {
  const highlight = page.getByTestId('sync-box')
  await expect(highlight).toHaveCount(1)
  await expect(highlight).toHaveAttribute('data-sync-page', String(pageNumber))
  await expect(highlight).toHaveAttribute('data-y', String(SYNC_TOP + SYNC_STEP * (line - 1)))
  const { box: pageBox, scale } = await pageGeometry(page, pageNumber)
  const box = await rect(highlight)
  expect(Math.abs(box.x - pageBox.x - SYNC_X * scale)).toBeLessThan(TOLERANCE_PX)
  expect(Math.abs(box.y - pageBox.y - (SYNC_TOP + SYNC_STEP * (line - 1)) * scale)).toBeLessThan(TOLERANCE_PX)
  expect(Math.abs(box.width - SYNC_W * scale)).toBeLessThan(TOLERANCE_PX)
  expect(Math.abs(box.height - SYNC_H * scale)).toBeLessThan(TOLERANCE_PX)
  return box
}

const pdfScrollTop = (page: Page) => page.getByTestId('pdf-pages').evaluate((el) => el.scrollTop)

/** Scroll the PDF pane so the top edge of page `n` is at the top of the pane. */
async function scrollPdfToPageTop(page: Page, n: number) {
  await page.getByTestId('pdf-pages').evaluate((el, pageNumber) => {
    const pageEl = el.querySelector(`[data-page="${pageNumber}"]`)!
    el.scrollTop += pageEl.getBoundingClientRect().top - el.getBoundingClientRect().top - 4
  }, n)
}

/** True when `inner` lies fully inside the visible part of the PDF pane. */
async function isInPdfView(page: Page, inner: { x: number; y: number; width: number; height: number }) {
  const pane = await page.getByTestId('pdf-pages').evaluate((el) => {
    const r = el.getBoundingClientRect()
    return { top: r.top + el.clientTop, left: r.left + el.clientLeft, height: el.clientHeight, width: el.clientWidth }
  })
  return (
    inner.y >= pane.top - 0.5 &&
    inner.y + inner.height <= pane.top + pane.height + 0.5 &&
    inner.x >= pane.left - 0.5 &&
    inner.x + inner.width <= pane.left + pane.width + 0.5
  )
}

test.beforeEach(async ({ request }) => {
  await request.post(`${MOCK_URL}/__mock/reset`)
})

// ---------------------------------------------------------------------------
// Forward sync
// ---------------------------------------------------------------------------

test('resting the cursor on a line highlights it in the PDF at the right spot', async ({ page, request }) => {
  const consoleErrors = collectConsoleErrors(page)
  await openAppWithPdf(page)
  await openFile(page, 'sections/intro.tex')
  // Opening a file alone does not sync; only cursor movement does.
  await expect(page.getByTestId('sync-box')).toHaveCount(0)

  await clickLine(page, 2)
  const first = await expectBoxAtLine(page, 1, 2)
  // Already visible near the top of page 1, so the PDF did not scroll.
  expect(await pdfScrollTop(page)).toBe(0)

  // Moving down one line moves the box down by one mock line.
  await clickLine(page, 3)
  await expect(page.getByTestId('sync-box')).toHaveAttribute('data-y', String(SYNC_TOP + SYNC_STEP * 2))
  const second = await expectBoxAtLine(page, 1, 3)
  const { scale } = await pageGeometry(page, 1)
  expect(Math.abs(second.y - first.y - SYNC_STEP * scale)).toBeLessThan(TOLERANCE_PX)

  // At another zoom level the box follows the new scale. ("Fit" is close to
  // 100% in this viewport, so this is what really checks the scaling.)
  await page.getByTitle('Zoom in').click()
  await page.getByTitle('Zoom in').click()
  await expect.poll(async () => (await pageGeometry(page, 1)).scale).toBeGreaterThan(1.3)
  await expectBoxAtLine(page, 1, 3)

  // Requests carry the repo-relative file, the 1-based line and col 0.
  expect(await forwardRequests(request)).toEqual([
    { kind: 'forward', file: 'sections/intro.tex', line: 2, col: 0 },
    { kind: 'forward', file: 'sections/intro.tex', line: 3, col: 0 },
  ])

  // A blank line has no SyncTeX record: the box goes and a quiet status shows.
  await clickLine(page, 4)
  await expect(page.getByTestId('sync-status')).toHaveText('synctex: no match')
  await expect(page.getByTestId('sync-box')).toHaveCount(0)
  await expect(page.getByRole('alert')).toHaveCount(0) // no error toast
  expect(consoleErrors).toEqual([])
})

test('no forward sync while typing or with unsaved edits; Sync button forces it', async ({ page, request }) => {
  await openAppWithPdf(page)
  await openFile(page, 'sections/intro.tex')
  await clickLine(page, 2)
  await expectBoxAtLine(page, 1, 2)
  expect(await forwardRequests(request)).toHaveLength(1)

  // Typing (including Enter, which moves the cursor to a new line) leaves the
  // file dirty, so no automatic sync fires.
  await page.keyboard.type(' more words')
  await page.keyboard.press('Enter')
  await page.keyboard.type('a new line')
  await expect(page.getByTestId('dirty-marker')).toBeVisible()
  await page.waitForTimeout(900)
  await clickLine(page, 3)
  await page.waitForTimeout(700)
  expect(await forwardRequests(request)).toHaveLength(1)

  // The explicit Sync button works even with unsaved edits.
  await page.getByTestId('sync-forward').click()
  await expect.poll(async () => (await forwardRequests(request)).length).toBe(2)
  expect((await forwardRequests(request))[1]).toMatchObject({ file: 'sections/intro.tex', line: 3 })

  // After saving, cursor movement syncs again.
  await page.keyboard.press('Control+s')
  await expect(page.getByTestId('dirty-marker')).toHaveCount(0)
  await clickLine(page, 1)
  await expect
    .poll(async () => (await forwardRequests(request)).map((r) => (r.kind === 'forward' ? r.line : 0)))
    .toContain(1)
})

test('Sync button and Ctrl-Alt-J scroll the PDF so a box on page 2 is centred', async ({ page, request }) => {
  await openAppWithPdf(page)
  await openFile(page, 'sections/method.tex')
  expect(await pdfScrollTop(page)).toBe(0)

  // Explicit sync for line 1 (the cursor's line after opening the file).
  await page.getByTestId('sync-forward').click()
  const box = await expectBoxAtLine(page, 2, 1)
  expect(await pdfScrollTop(page)).toBeGreaterThan(0)
  expect(await isInPdfView(page, box)).toBe(true)
  // Centred vertically in the pane.
  const pane = await rect(page.getByTestId('pdf-pages'))
  expect(Math.abs(box.y + box.height / 2 - (pane.y + pane.height / 2))).toBeLessThan(3)

  // Moving the cursor to line 4 syncs by itself (and scrolls, since the PDF
  // was scrolled away first).
  await page.getByTestId('pdf-pages').evaluate((el) => (el.scrollTop = 0))
  expect(await isInPdfView(page, await rect(page.getByTestId('sync-box')))).toBe(false)
  await clickLine(page, 4)
  await expectBoxAtLine(page, 2, 4)
  await expect.poll(async () => isInPdfView(page, await rect(page.getByTestId('sync-box')))).toBe(true)
  const afterAuto = (await forwardRequests(request)).length

  // Resting on the same line does not refetch, but Ctrl-Alt-J (focus is in
  // the editor) forces a sync and scrolls the box back into view.
  await page.getByTestId('pdf-pages').evaluate((el) => (el.scrollTop = 0))
  await page.waitForTimeout(500)
  expect((await forwardRequests(request)).length).toBe(afterAuto)
  await page.keyboard.press('Control+Alt+j')
  await expect.poll(async () => (await forwardRequests(request)).length).toBe(afterAuto + 1)
  expect((await forwardRequests(request)).at(-1)).toMatchObject({ file: 'sections/method.tex', line: 4 })
  await expect.poll(async () => isInPdfView(page, await rect(page.getByTestId('sync-box')))).toBe(true)
  await expectBoxAtLine(page, 2, 4)
})

// ---------------------------------------------------------------------------
// Reverse sync
// ---------------------------------------------------------------------------

test('double-clicking the PDF opens the file at the line under the mouse', async ({ page, request }) => {
  const consoleErrors = collectConsoleErrors(page)
  await openAppWithPdf(page)
  await expect(page.getByTestId('pdf-toolbar')).toHaveAttribute('title', /Double-click/)
  // Zoom in so the scale is clearly not 1 ("Fit" is close to 100% here).
  await page.getByTitle('Zoom in').click()
  await page.getByTitle('Zoom in').click()
  await expect.poll(async () => (await pageGeometry(page, 1)).scale).toBeGreaterThan(1.3)

  // A spot inside mock line 3 of page 1 (= sections/intro.tex:3).
  const xPt = 200
  const yPt = SYNC_TOP + SYNC_STEP * 2 + 5
  const { box: page1, scale } = await pageGeometry(page, 1)
  await page.mouse.dblclick(page1.x + xPt * scale, page1.y + yPt * scale)

  await expect(page.getByTestId('open-file-name')).toHaveText('sections/intro.tex')
  await expect(page.locator('.cm-activeLineGutter')).toHaveText('3')
  await expect(page.locator('.cm-activeLine')).toContainText('This sentence cites a missing key')

  // The request carried PDF points from the page's top-left corner.
  const reverse = (await syncRequests(request)).filter((r) => r.kind === 'reverse')
  expect(reverse).toHaveLength(1)
  const sent = reverse[0] as { page: number; x: number; y: number }
  expect(sent.page).toBe(1)
  expect(Math.abs(sent.x - xPt)).toBeLessThan(1 / scale + 0.01)
  expect(Math.abs(sent.y - yPt)).toBeLessThan(1 / scale + 0.01)

  // The jump's own cursor move then highlights that line, without scrolling
  // the PDF away from where the user double-clicked.
  await expectBoxAtLine(page, 1, 3)
  expect(await pdfScrollTop(page)).toBe(0)

  // Page 2 maps to sections/method.tex; line 4 has \undefinedmacro.
  await scrollPdfToPageTop(page, 2)
  const { box: page2 } = await pageGeometry(page, 2)
  await page.mouse.dblclick(page2.x + 300 * scale, page2.y + (SYNC_TOP + SYNC_STEP * 3 + 2) * scale)
  await expect(page.getByTestId('open-file-name')).toHaveText('sections/method.tex')
  await expect(page.locator('.cm-activeLineGutter')).toHaveText('4')
  await expect(page.locator('.cm-activeLine')).toContainText('\\undefinedmacro')

  // The top margin has no record: quiet status, editor unchanged, no toast.
  await scrollPdfToPageTop(page, 1)
  const { box: page1Again } = await pageGeometry(page, 1)
  await page.mouse.dblclick(page1Again.x + 200 * scale, page1Again.y + 40 * scale)
  await expect(page.getByTestId('sync-status')).toHaveText('synctex: no match')
  await expect(page.getByTestId('open-file-name')).toHaveText('sections/method.tex')
  await expect(page.getByRole('alert')).toHaveCount(0)
  expect(consoleErrors).toEqual([])
})

// ---------------------------------------------------------------------------
// Settings dialog
// ---------------------------------------------------------------------------

test('settings dialog round-trips a compiler change', async ({ page, request }) => {
  await page.goto('/')
  await page.getByTestId('settings-button').click()
  const dialog = page.getByTestId('settings-dialog')
  await expect(dialog).toBeVisible()
  await expect(dialog.getByTestId('docker-available')).toHaveText('docker available')
  await expect(dialog.getByTestId('docker-image')).toHaveText('texlive/texlive:TL2024-historic')
  await expect(dialog.getByTestId('docker-image-status')).toHaveText('not pulled')
  await expect(dialog.getByRole('radio', { name: /Native/ })).toBeChecked()
  const year = dialog.getByRole('spinbutton', { name: 'TeX Live year' })
  await expect(year).toHaveValue('2024')
  const save = dialog.getByRole('button', { name: 'Save' })
  await expect(save).toBeDisabled() // nothing changed yet

  // Out-of-range years are refused before reaching the backend.
  await year.fill('2031')
  await expect(save).toBeDisabled()
  await expect(dialog).toContainText('Enter a year from 2018 to 2030')

  await dialog.getByRole('radio', { name: /Docker/ }).check()
  await year.fill('2023')
  const put = page.waitForRequest((r) => r.url().endsWith('/api/config') && r.method() === 'PUT')
  await save.click()
  expect((await put).postDataJSON()).toEqual({ compiler: { kind: 'docker', texlive_year: 2023 } })
  await expect(dialog.getByTestId('settings-save-message')).toContainText('Saved')
  await expect(dialog.getByTestId('docker-image')).toHaveText('texlive/texlive:TL2023-historic')

  // The backend kept it ...
  const stored = await (await request.get(`${MOCK_URL}/api/config`)).json()
  expect(stored.compiler).toMatchObject({ kind: 'docker', texlive_year: 2023 })

  // ... and the dialog shows it when opened again.
  await dialog.getByRole('button', { name: 'Close' }).click()
  await expect(dialog).toHaveCount(0)
  await page.getByTestId('settings-button').click()
  await expect(dialog.getByRole('radio', { name: /Docker/ })).toBeChecked()
  await expect(dialog.getByRole('spinbutton', { name: 'TeX Live year' })).toHaveValue('2023')
})

test('Pull image streams progress lines, then done; a missing image fails', async ({ page }) => {
  await page.goto('/')
  await page.getByTestId('settings-button').click()
  const dialog = page.getByTestId('settings-dialog')
  const pull = dialog.getByRole('button', { name: 'Pull image' })
  await expect(pull).toBeEnabled()

  await pull.click()
  await expect(dialog.getByTestId('docker-pull-status')).toHaveText('pulling…')
  await expect(pull).toBeDisabled()
  const log = dialog.getByTestId('docker-pull-log')
  await expect(log).toContainText('Pulling fs layer')
  await expect(dialog.getByTestId('docker-pull-status')).toHaveText('done')
  await expect(log).toContainText('Status: Downloaded newer image for texlive/texlive:TL2024-historic')
  // The config is re-read, so the image now shows as present.
  await expect(dialog.getByTestId('docker-image-status')).toHaveText('present')

  // Pull uses the saved year: disabled while the form has unsaved changes.
  const year = dialog.getByRole('spinbutton', { name: 'TeX Live year' })
  await year.fill('2030')
  await expect(pull).toBeDisabled()
  await dialog.getByRole('button', { name: 'Save' }).click()
  await expect(dialog.getByTestId('docker-image')).toHaveText('texlive/texlive:TL2030-historic')
  await expect(pull).toBeEnabled()
  await pull.click()
  await expect(dialog.getByTestId('docker-pull-status')).toContainText('failed')
  await expect(dialog.getByTestId('docker-pull-status')).toContainText('not found')
  await expect(log).toContainText('manifest unknown')
  await expect(dialog.getByTestId('docker-image-status')).toHaveText('not pulled')
})

test('the Docker option is disabled with a hint when docker is unavailable', async ({ page, request }) => {
  await request.post(`${MOCK_URL}/__mock/docker`, { data: { available: false } })
  await page.goto('/')
  await page.getByTestId('settings-button').click()
  const dialog = page.getByTestId('settings-dialog')
  await expect(dialog.getByTestId('docker-available')).toHaveText('docker not available')
  await expect(dialog.getByRole('radio', { name: /Docker/ })).toBeDisabled()
  await expect(dialog.getByTestId('docker-unavailable-hint')).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Pull image' })).toBeDisabled()
  // Escape closes the dialog.
  await dialog.getByRole('radio', { name: /Native/ }).focus()
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
})
