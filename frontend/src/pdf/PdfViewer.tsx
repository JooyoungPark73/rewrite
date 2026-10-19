// PdfViewer: renders the compiled PDF with pdf.js, all pages stacked
// vertically on <canvas> elements.
//
// - The PDF is fetched from GET /api/pdf?v=<version>. The store bumps
//   `version` after every compile, which triggers a refetch. A 404 means
//   nothing has been built yet and shows "No PDF yet — compile".
// - While a new version loads, the old pages stay on screen, so the scroll
//   position survives a recompile. Zoom changes keep the same relative spot.
// - Pages render lazily, the first time they come near the viewport, so long
//   papers do not render every page up front.
// - Zoom: +/- buttons, or "Fit" to fill the pane width (the default).
//
// SyncTeX hooks (phase 2; App.tsx wires them to the store):
// - `onPageDoubleClick(page, xPt, yPt)` reports a double-click in PDF points
//   (1/72 inch) measured from the page's top-left corner, the coordinate
//   system `synctex edit` expects. See pdf/coords.ts for the conversion.
// - `renderPageOverlay(page, scale)` draws on top of each page, for example
//   SyncTeX highlight boxes. Multiply PDF points by `scale` to get CSS pixels.
// - `reveal` asks the viewer to scroll a rectangle (PDF points) into view:
//   it is centred unless it is already fully visible. A new `seq` means a
//   new request. If the pane is hidden (narrow screens, Editor tab) the
//   request waits until the pane is shown.
// - `onDocumentChange(pageCount)` reports when a PDF appears (page count) or
//   disappears (null), so SyncTeX is only attempted while a PDF is on screen.
// - `status` is a short text shown in the toolbar (e.g. "synctex: no match").
import * as pdfjs from 'pdfjs-dist'
import type { PDFDocumentProxy, RenderTask } from 'pdfjs-dist'
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { errorMessage, fetchPdf } from '../api/client'
import { clientToPdfPoint } from './coords'

// pdf.js parses PDFs in a Web Worker. Vite turns this `new URL(...,
// import.meta.url)` into the URL of the worker file: served from node_modules
// in dev, copied into dist/assets with a hashed name by `npm run build`.
pdfjs.GlobalWorkerOptions.workerSrc = new URL('pdfjs-dist/build/pdf.worker.min.mjs', import.meta.url).toString()

/** Page size in PDF points (scale 1). */
interface PageSize {
  width: number
  height: number
}

interface LoadedPdf {
  pdf: PDFDocumentProxy
  sizes: PageSize[]
}

/** A rectangle to scroll into view, in PDF points from the page's top-left. */
export interface PdfRevealTarget {
  page: number
  x: number
  y: number
  w: number
  h: number
  /** Unique per request, so asking twice for the same box scrolls twice. */
  seq: number
}

export interface PdfViewerProps {
  /** Version counter from the store; null when no repo is open. */
  version: number | null
  /** Phase 2 (reverse SyncTeX): double-click position in PDF points from the page's top-left. */
  onPageDoubleClick?: (page: number, xPt: number, yPt: number) => void
  /** Phase 2 (forward SyncTeX): extra content drawn over a page, e.g. highlight boxes. */
  renderPageOverlay?: (page: number, scale: number) => ReactNode
  /** Phase 2 (forward SyncTeX): scroll this rectangle into view when `seq` changes. */
  reveal?: PdfRevealTarget | null
  /** Called with the page count when a PDF is shown, and null when none is. */
  onDocumentChange?: (pageCount: number | null) => void
  /** Short status text for the toolbar (e.g. SyncTeX "no match"). */
  status?: ReactNode
  /** Tooltip for the toolbar, e.g. explaining the double-click gesture. */
  toolbarTitle?: string
}

type Zoom = 'fit' | number

const MIN_SCALE = 0.25
const MAX_SCALE = 4
const PAGE_GAP_PX = 12
const SIDE_PADDING_PX = 16

const clampScale = (s: number) => Math.min(MAX_SCALE, Math.max(MIN_SCALE, s))

/** Free a document and its worker. In pdf.js 6 this goes through the loading task. */
function destroyPdf(pdf: PDFDocumentProxy | null): void {
  if (pdf) void pdf.loadingTask.destroy()
}

export default function PdfViewer({
  version,
  onPageDoubleClick,
  renderPageOverlay,
  reveal,
  onDocumentChange,
  status,
  toolbarTitle,
}: PdfViewerProps) {
  // The last document that loaded successfully. It stays on screen while a
  // newer version loads.
  const [loaded, setLoaded] = useState<LoadedPdf | null>(null)
  // How the most recent fetch ended. While `outcome.version` lags behind
  // `version`, a fetch is in flight ("loading" is derived, not stored).
  const [outcome, setOutcome] = useState<{ version: number; kind: 'ready' | 'missing' | 'error'; error?: string } | null>(
    null,
  )
  const [zoom, setZoom] = useState<Zoom>('fit')
  // Pane width for "fit width". Only updated while the pane is visible, so a
  // hidden pane (width 0) keeps its last scale.
  const [paneWidth, setPaneWidth] = useState(0)

  const scrollRef = useRef<HTMLDivElement>(null)
  // The document currently shown; destroyed when replaced or on unmount.
  const docRef = useRef<PDFDocumentProxy | null>(null)
  // Last scroll position and the scale it was taken at.
  const scrollPosRef = useRef<{ top: number; left: number; scale: number } | null>(null)
  // A reveal request that could not be carried out yet (pane hidden or no
  // pages laid out), and the seq of the last request seen.
  const pendingRevealRef = useRef<PdfRevealTarget | null>(null)
  const lastRevealSeqRef = useRef<number | null>(null)
  // The current scale, for callbacks created once (ResizeObserver).
  const scaleRef = useRef(1)

  // ---- Load the PDF whenever the version changes -------------------------
  useEffect(() => {
    if (version === null) return
    let cancelled = false
    ;(async () => {
      try {
        const data = await fetchPdf(version)
        if (cancelled) return
        if (!data) {
          destroyPdf(docRef.current)
          docRef.current = null
          setLoaded(null)
          setOutcome({ version, kind: 'missing' })
          return
        }
        const pdf = await pdfjs.getDocument({ data }).promise
        // Read every page's size up front so the page boxes have their final
        // size before rendering, which is what keeps scroll positions stable.
        const sizes: PageSize[] = []
        for (let n = 1; n <= pdf.numPages; n++) {
          const viewport = (await pdf.getPage(n)).getViewport({ scale: 1 })
          sizes.push({ width: viewport.width, height: viewport.height })
        }
        if (cancelled) {
          destroyPdf(pdf)
          return
        }
        const previous = docRef.current
        docRef.current = pdf
        setLoaded({ pdf, sizes })
        setOutcome({ version, kind: 'ready' })
        // Old canvases keep their pixels until the new pages draw over them.
        destroyPdf(previous)
      } catch (err) {
        if (!cancelled) setOutcome({ version, kind: 'error', error: errorMessage(err) })
      }
    })()
    return () => {
      cancelled = true
    }
  }, [version])

  // Destroy the last document when the viewer unmounts.
  useEffect(
    () => () => {
      destroyPdf(docRef.current)
      docRef.current = null
    },
    [],
  )

  // ---- Track the pane width for "fit width" -------------------------------
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const observer = new ResizeObserver(() => {
      if (el.clientWidth === 0) return // hidden (display:none); keep the last width
      setPaneWidth(el.clientWidth)
      // A SyncTeX box requested while the pane was hidden wins over the old
      // scroll position.
      const pending = pendingRevealRef.current
      if (pending && scrollRectIntoView(el, pending, scaleRef.current)) {
        pendingRevealRef.current = null
        scrollPosRef.current = { top: el.scrollTop, left: el.scrollLeft, scale: scaleRef.current }
        return
      }
      // A pane hidden with display:none (the PDF tab on narrow screens) loses
      // its scroll offset; put it back when the pane becomes visible again.
      const saved = scrollPosRef.current
      if (saved && el.scrollTop === 0 && saved.top > 0) {
        el.scrollTop = saved.top
        el.scrollLeft = saved.left
      }
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  const maxPageWidth = loaded ? Math.max(...loaded.sizes.map((s) => s.width)) : 0
  let scale = 1
  if (zoom !== 'fit') scale = zoom
  else if (paneWidth > 0 && maxPageWidth > 0) scale = clampScale((paneWidth - 2 * SIDE_PADDING_PX) / maxPageWidth)
  useLayoutEffect(() => {
    scaleRef.current = scale
  }, [scale])

  // ---- Keep the scroll position across zoom changes and reloads ----------
  // Runs before paint; page boxes already have their new sizes, so the old
  // offset can be rescaled to the new scale. (A reload at the same scale
  // keeps the same DOM, so the browser keeps the offset by itself.)
  useLayoutEffect(() => {
    const el = scrollRef.current
    const saved = scrollPosRef.current
    if (!el || !saved || saved.scale === scale) return
    el.scrollTop = (saved.top * scale) / saved.scale
    el.scrollLeft = (saved.left * scale) / saved.scale
    scrollPosRef.current = { top: el.scrollTop, left: el.scrollLeft, scale }
  }, [scale, loaded])

  // ---- Scroll a SyncTeX box into view -------------------------------------
  // Declared after the effect above so that, when both run, the box wins.
  useLayoutEffect(() => {
    if (!reveal) {
      // Withdrawn (e.g. the PDF was rebuilt): forget a request still waiting
      // for the pane to be shown.
      pendingRevealRef.current = null
    } else if (reveal.seq !== lastRevealSeqRef.current) {
      lastRevealSeqRef.current = reveal.seq
      pendingRevealRef.current = reveal
    }
    const el = scrollRef.current
    const pending = pendingRevealRef.current
    if (!el || !pending) return
    if (scrollRectIntoView(el, pending, scale)) {
      pendingRevealRef.current = null
      scrollPosRef.current = { top: el.scrollTop, left: el.scrollLeft, scale }
    }
  }, [reveal, loaded, scale])

  // ---- Tell the parent whether a PDF is on screen --------------------------
  const onDocumentChangeRef = useRef(onDocumentChange)
  useEffect(() => {
    onDocumentChangeRef.current = onDocumentChange
  }, [onDocumentChange])
  const shownPageCount = version !== null && loaded ? loaded.sizes.length : null
  useEffect(() => {
    onDocumentChangeRef.current?.(shownPageCount)
  }, [shownPageCount])

  const onScroll = () => {
    const el = scrollRef.current
    if (el) scrollPosRef.current = { top: el.scrollTop, left: el.scrollLeft, scale }
  }

  const zoomBy = (factor: number) => setZoom(clampScale(Math.round(scale * factor * 100) / 100))

  // ---- Render -------------------------------------------------------------
  const loading = version !== null && outcome?.version !== version
  const shown = version === null ? null : loaded
  let placeholder: string | null = null
  if (version === null) placeholder = 'Open a repo to see its PDF'
  else if (!shown) {
    if (loading) placeholder = 'Loading PDF…'
    else if (outcome?.kind === 'error') placeholder = `Could not load the PDF: ${outcome.error}`
    else placeholder = 'No PDF yet — compile'
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-base-300" data-testid="pdf-viewer">
      <div
        className="flex items-center gap-1 border-b border-base-300 bg-base-100 px-2 py-1 text-sm"
        title={toolbarTitle}
        data-testid="pdf-toolbar"
      >
        <button type="button" className="btn btn-ghost btn-xs" onClick={() => zoomBy(1 / 1.2)} title="Zoom out" disabled={!shown}>
          −
        </button>
        <span className="w-12 text-center tabular-nums">{Math.round(scale * 100)}%</span>
        <button type="button" className="btn btn-ghost btn-xs" onClick={() => zoomBy(1.2)} title="Zoom in" disabled={!shown}>
          +
        </button>
        <button
          type="button"
          className={`btn btn-xs ${zoom === 'fit' ? 'btn-active' : 'btn-ghost'}`}
          onClick={() => setZoom('fit')}
          title="Fit page width"
          disabled={!shown}
        >
          Fit
        </button>
        {status}
        <span className="ml-auto text-base-content/60" data-testid="pdf-page-count">
          {shown ? `${shown.sizes.length} page${shown.sizes.length === 1 ? '' : 's'}` : ''}
        </span>
        {shown && loading && <span className="loading loading-spinner loading-xs" title="Updating" />}
        {shown && !loading && outcome?.kind === 'error' && (
          <span className="text-error" title={outcome.error}>
            reload failed
          </span>
        )}
      </div>

      <div ref={scrollRef} onScroll={onScroll} className="min-h-0 flex-1 overflow-auto" data-testid="pdf-pages">
        {placeholder !== null || !shown ? (
          <div className="flex h-full items-center justify-center p-4 text-center text-base-content/60">{placeholder}</div>
        ) : (
          <div
            className="flex w-max min-w-full flex-col items-center"
            style={{ gap: PAGE_GAP_PX, padding: `${PAGE_GAP_PX}px ${SIDE_PADDING_PX}px` }}
          >
            {shown.sizes.map((size, i) => (
              <PdfPage
                key={i + 1}
                pdf={shown.pdf}
                pageNumber={i + 1}
                size={size}
                scale={scale}
                rootRef={scrollRef}
                onDoubleClick={onPageDoubleClick}
                overlay={renderPageOverlay?.(i + 1, scale)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// One page: a sized box holding a canvas plus an overlay layer.
// ---------------------------------------------------------------------------
interface PdfPageProps {
  pdf: PDFDocumentProxy
  pageNumber: number
  size: PageSize
  scale: number
  /** Scroll container, used as the IntersectionObserver root. */
  rootRef: RefObject<HTMLDivElement | null>
  onDoubleClick?: (page: number, xPt: number, yPt: number) => void
  overlay?: ReactNode
}

function PdfPage({ pdf, pageNumber, size, scale, rootRef, onDoubleClick, overlay }: PdfPageProps) {
  const boxRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [nearViewport, setNearViewport] = useState(false)

  // Start rendering when the page is within one screen of the visible area.
  // Once rendered it stays rendered.
  useEffect(() => {
    const box = boxRef.current
    const root = rootRef.current
    if (!box || !root || nearViewport) return
    const observer = new IntersectionObserver((entries) => entries.some((e) => e.isIntersecting) && setNearViewport(true), {
      root,
      rootMargin: '100% 0px',
    })
    observer.observe(box)
    return () => observer.disconnect()
  }, [rootRef, nearViewport])

  // Draw the page. The canvas bitmap is devicePixelRatio times larger than its
  // CSS size so text stays sharp on HiDPI screens; pdf.js scales its drawing
  // with `transform`. Drawing goes to an off-screen canvas first and is copied
  // over in one step, so the visible page never flashes blank on recompile.
  useEffect(() => {
    if (!nearViewport) return
    let cancelled = false
    let task: RenderTask | null = null
    ;(async () => {
      try {
        const page = await pdf.getPage(pageNumber)
        if (cancelled) return
        const viewport = page.getViewport({ scale })
        const dpr = window.devicePixelRatio || 1
        const offscreen = document.createElement('canvas')
        offscreen.width = Math.floor(viewport.width * dpr)
        offscreen.height = Math.floor(viewport.height * dpr)
        task = page.render({
          canvas: offscreen,
          viewport,
          transform: dpr === 1 ? undefined : [dpr, 0, 0, dpr, 0, 0],
        })
        await task.promise
        const canvas = canvasRef.current
        if (cancelled || !canvas) return
        canvas.width = offscreen.width
        canvas.height = offscreen.height
        canvas.getContext('2d')?.drawImage(offscreen, 0, 0)
      } catch {
        // Cancelled render, or the document was replaced by a newer compile
        // and destroyed mid-render: nothing to do, the newer render wins.
      }
    })()
    return () => {
      cancelled = true
      task?.cancel()
    }
  }, [pdf, pageNumber, scale, nearViewport])

  const cssWidth = size.width * scale
  const cssHeight = size.height * scale

  return (
    <div
      ref={boxRef}
      // select-none: a double-click should not select anything on the page.
      className="relative shrink-0 bg-white shadow-md select-none"
      style={{ width: cssWidth, height: cssHeight }}
      data-page={pageNumber}
      onDoubleClick={(e) => {
        if (!onDoubleClick) return
        // Mouse position -> PDF points from the page's top-left (y down, as
        // `synctex edit` expects). The box is exactly cssWidth x cssHeight CSS
        // pixels, so this is the CSS offset divided by `scale`;
        // devicePixelRatio cancels out (see pdf/coords.ts).
        const point = clientToPdfPoint(e.clientX, e.clientY, e.currentTarget.getBoundingClientRect(), scale)
        onDoubleClick(pageNumber, point.x, point.y)
      }}
    >
      <canvas ref={canvasRef} className="block" style={{ width: cssWidth, height: cssHeight }} />
      {/* Overlay layer for phase 2 highlight boxes; ignores the mouse so
          double-clicks still reach the page box. */}
      <div className="pointer-events-none absolute inset-0">{overlay}</div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Scrolling a rectangle into view
// ---------------------------------------------------------------------------

/**
 * Scroll `el` (the pages container) so the rectangle `target` (PDF points on
 * page `target.page`) is visible: centred along an axis where it is not
 * already fully visible, untouched along an axis where it is. Returns false
 * when it cannot be done yet (pane hidden, or the page is not laid out).
 */
function scrollRectIntoView(el: HTMLElement, target: PdfRevealTarget, scale: number): boolean {
  if (el.clientHeight === 0 || el.clientWidth === 0) return false
  const pageEl = el.querySelector<HTMLElement>(`[data-page="${target.page}"]`)
  if (!pageEl) return false
  // Page position inside the scrollable content, in CSS pixels.
  const pageRect = pageEl.getBoundingClientRect()
  const elRect = el.getBoundingClientRect()
  const pageTop = pageRect.top - elRect.top - el.clientTop + el.scrollTop
  const pageLeft = pageRect.left - elRect.left - el.clientLeft + el.scrollLeft
  // The rectangle in the same coordinates.
  const top = pageTop + target.y * scale
  const bottom = top + Math.max(target.h, 0) * scale
  const left = pageLeft + target.x * scale
  const right = left + Math.max(target.w, 0) * scale

  const viewTop = el.scrollTop
  const viewLeft = el.scrollLeft
  if (top < viewTop || bottom > viewTop + el.clientHeight) {
    el.scrollTop = (top + bottom) / 2 - el.clientHeight / 2
  }
  if (left < viewLeft || right > viewLeft + el.clientWidth) {
    // A box wider than the pane is aligned to its left edge instead.
    el.scrollLeft = right - left > el.clientWidth ? left - 8 : (left + right) / 2 - el.clientWidth / 2
  }
  return true
}
