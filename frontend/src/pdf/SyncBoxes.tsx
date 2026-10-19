// SyncBoxes: the forward-SyncTeX highlight drawn over one PDF page.
//
// PdfViewer calls `renderPageOverlay(page, scale)` for every page and puts the
// result in a layer exactly on top of the page's canvas; App.tsx returns
// <SyncBoxes page={page} scale={scale} /> from it. This component reads the
// boxes of the last forward search from the store and draws the ones on its
// page as semi-transparent yellow rectangles.
//
// - Boxes stay until the next sync (or until the PDF is rebuilt, which clears
//   them in the store). Each new sync replays a short fade-in so a repeated
//   sync to the same spot is still noticeable.
// - pointer-events: none, so double-clicks reach the page underneath.
// - data-sync-page / data-x / data-y / data-w / data-h carry the values the backend
//   sent, for tests and for debugging the coordinate convention.
import { useStore } from '../state/store'
import { pdfRectToCss } from './coords'

export default function SyncBoxes({ page, scale }: { page: number; scale: number }) {
  const boxes = useStore((s) => s.syncBoxes)
  const seq = useStore((s) => s.syncSeq)
  const onPage = boxes.filter((b) => b.page === page)
  if (onPage.length === 0) return null
  return (
    <>
      {onPage.map((box, i) => {
        const css = pdfRectToCss(box, scale)
        return (
          <div
            // A new key per sync remounts the element, which restarts the CSS animation.
            key={`${seq}-${i}`}
            className="sync-box pointer-events-none absolute rounded-sm bg-yellow-300/40 ring-1 ring-yellow-500/70"
            style={{ left: css.left, top: css.top, width: css.width, height: css.height }}
            data-testid="sync-box"
            data-sync-page={page}
            data-x={box.x}
            data-y={box.y}
            data-w={box.w}
            data-h={box.h}
          />
        )
      })}
    </>
  )
}
