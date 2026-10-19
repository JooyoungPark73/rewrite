// Conversions between PDF points and CSS pixels on a rendered PDF page.
//
// The coordinate system shared with the backend (phase 2 API contract) is PDF
// points (1/72 inch) with the origin at the TOP-LEFT corner of the page as
// pdf.js renders it, x growing to the right and y growing DOWNWARDS. That is
// also what `synctex view` prints and what `synctex edit` expects, so the
// backend can pass the numbers through.
//
// pdf.js draws a page at `scale` CSS pixels per point: page.getViewport({ scale })
// puts the page's top-left corner at (0, 0) and flips y so it grows downwards.
// Each page is shown in a box (a <div> holding the <canvas>) that is exactly
// widthPt * scale by heightPt * scale CSS pixels, so for a spot on that box:
//
//     xPt = cssX / scale          cssX = xPt * scale
//     yPt = cssY / scale          cssY = yPt * scale
//
// where cssX / cssY are measured from the box's top-left corner.
//
// devicePixelRatio (dpr): the canvas *bitmap* is dpr times larger than its CSS
// size so text stays sharp on HiDPI screens. A bitmap pixel bx is therefore at
// cssX = bx / dpr, i.e. xPt = bx / (scale * dpr). Mouse events and layout are
// already in CSS pixels, so the functions below never need dpr; it cancels
// out. (Going through bitmap pixels instead would only add the rounding of the
// bitmap size.)
//
// Not handled (pdfLaTeX output has neither): pages with a /Rotate entry, and a
// CropBox whose corner differs from the MediaBox's corner.

/** A point in PDF points from the page's top-left corner. */
export interface PdfPoint {
  x: number
  y: number
}

/** A rectangle in CSS pixels relative to the page box's top-left corner. */
export interface CssRect {
  left: number
  top: number
  width: number
  height: number
}

/**
 * Mouse position -> PDF point on a page.
 * `pageRect` is the page box's getBoundingClientRect(); `clientX/Y` come from
 * the mouse event; `scale` is CSS pixels per point.
 */
export function clientToPdfPoint(clientX: number, clientY: number, pageRect: DOMRect, scale: number): PdfPoint {
  return {
    x: (clientX - pageRect.left) / scale,
    y: (clientY - pageRect.top) / scale,
  }
}

/** PDF rectangle (points, top-left origin) -> CSS pixels on the page box. */
export function pdfRectToCss(box: { x: number; y: number; w: number; h: number }, scale: number): CssRect {
  return {
    left: box.x * scale,
    top: box.y * scale,
    width: box.w * scale,
    height: box.h * scale,
  }
}
