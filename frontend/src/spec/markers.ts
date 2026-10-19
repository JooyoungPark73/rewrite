// Where a `% @p <id>` marker goes when a spec node is dropped on the editor
// (or inserted at the editor's cursor from a row's "⋯" menu).
//
// A marker must sit directly above the FIRST line of its paragraph: the
// backend reads the paragraph as the lines after the marker up to the next
// blank line. So a drop anywhere inside a paragraph snaps to the paragraph's
// first line, and a drop on a blank line, a heading or another marker snaps
// down to the next line of text.

const BLANK = /^\s*$/
const HEADING = /^\s*\\(part|chapter|section|subsection|subsubsection|paragraph)\*?\s*[[{]/
const MARKER = /^\s*%\s*@p\b/
const LABEL_ONLY = /^\s*\\label\{[^}]*\}\s*$/

/** Lines that end a paragraph above or below them. */
function isBoundary(text: string): boolean {
  return BLANK.test(text) || HEADING.test(text) || MARKER.test(text) || LABEL_ONLY.test(text)
}

/**
 * The 1-based line where the paragraph containing `line` starts.
 * `lines` is the file's text split on "\n"; `line` is 1-based.
 */
export function paragraphStartLine(lines: string[], line: number): number {
  if (lines.length === 0) return 1
  let l = Math.min(Math.max(1, Math.floor(line)), lines.length)
  // On a blank line, heading or marker: step down to the next line of text.
  while (l < lines.length && isBoundary(lines[l - 1])) l++
  if (isBoundary(lines[l - 1])) return Math.min(Math.max(1, Math.floor(line)), lines.length)
  // Walk up to the first line of this paragraph.
  while (l > 1 && !isBoundary(lines[l - 2])) l--
  return l
}
