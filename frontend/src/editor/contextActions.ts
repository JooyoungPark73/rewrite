// Editor context menu (phase 5): what the menu knows and what its items do.
// The menu itself is editor/ContextMenu.tsx; Editor.tsx opens it on a
// right-click (or Ctrl/Cmd-.) while text is selected.
//
// PLAN.md "Spec editor": selecting text in the editor offers "set as this
// paragraph's message", "lift to claim" and "set as argument" (the `thesis` field), which is how
// sentences move between levels: across the two panes. Every item is one or
// two phase 3 spec ops (decided in phase-5-ab-git-coverage.md), nothing new
// on the backend:
//
//   Set as this paragraph's message   update {id: <enclosing node>, says: <selection>}
//   Lift to claim                     add_claim {says: <selection>}, then
//                                     link_claim {id: <enclosing node>, claim_id: <new id>}
//   Set as argument                   update {id: <message id>, fields: {thesis: <selection>}}
//   Compare with earlier version…     opens the blind A/B compare dialog for the node
//
// The selection text is whitespace-collapsed first (line breaks and indents
// become single spaces).
//
// The enclosing node: the paragraph node whose `% @p <id>` marker most
// recently precedes the selection start, as long as no paragraph break lies in
// between (a blank line, a heading, or another marker: the same rule the
// backend uses for a marker's paragraph text). It is read from the editor's
// current text, so unsaved edits above the selection do not shift it; once
// the file is saved these are the same marker lines GET /api/spec/status
// reports. The id must be a paragraph node of paper.json. When there is
// none, the first two items become "Adopt this paragraph first, then …":
// POST /api/spec/adopt with the selection's start line (any line of the
// paragraph works), then the action on the new node.
//
// After an action a toast says what changed, with an "Undo" button that runs
// the spec store's undo once per spec write the action made (if nothing else
// was edited in between). Undo restores paper.json only: a marker that
// "Adopt" wrote into the .tex stays, as everywhere else in the app.
import { create } from 'zustand'
import type { SpecOp, SpecResponse } from '../api/client'
import { useCompareStore } from '../state/compare'
import { useSpecStore } from '../state/spec'
import { findNode } from '../spec/tree'

/** What the menu was opened on. */
export interface EditorMenu {
  /** Viewport position of the menu's top-left corner. */
  x: number
  y: number
  /** File open in the editor. */
  file: string
  /** The selection, whitespace-collapsed. */
  text: string
  /** 1-based lines of the selection's start and end. */
  lineStart: number
  lineEnd: number
  /** The enclosing paragraph node, or null (then "Adopt this paragraph first"). */
  nodeId: string | null
  /** Gives the focus back to the editor when the menu closes without acting. */
  restoreFocus?: () => void
}

interface EditorMenuState {
  menu: EditorMenu | null
  open: (menu: EditorMenu) => void
  close: () => void
}

/** The open menu (null = closed). Kept outside the component so CodeMirror's handlers can open it. */
export const useEditorMenuStore = create<EditorMenuState>()((set) => ({
  menu: null,
  open: (menu) => set({ menu }),
  close: () => set({ menu: null }),
}))

/** "a  b\n c" -> "a b c" */
export function collapseWhitespace(text: string): string {
  return text.replace(/\s+/g, ' ').trim()
}

const MARKER = /^\s*%\s*@p\s+(\S+)\s*$/
const BLANK = /^\s*$/
const HEADING = /^\s*\\(part|chapter|section|subsection|subsubsection|paragraph)\*?\s*[[{]/
const END_DOCUMENT = /\\end\{document\}/

/**
 * The id in the `% @p <id>` marker that most recently precedes `startLine`
 * (1-based) within the same paragraph, or null when a blank line, a heading
 * or \end{document} comes first. A selection that starts on the marker line
 * itself belongs to that marker. `lines` is the file's text split on "\n".
 */
export function precedingMarkerId(lines: string[], startLine: number): string | null {
  const first = Math.min(Math.max(1, Math.floor(startLine)), lines.length)
  for (let l = first; l >= 1; l--) {
    const text = lines[l - 1] ?? ''
    const marker = MARKER.exec(text)
    if (marker) return marker[1]
    if (BLANK.test(text) || HEADING.test(text) || END_DOCUMENT.test(text)) return null
  }
  return null
}

/** The enclosing paragraph node's id (see the header), or null. */
export function enclosingNodeId(lines: string[], startLine: number): string | null {
  const id = precedingMarkerId(lines, startLine)
  if (!id) return null
  const spec = useSpecStore.getState().spec
  const node = spec ? findNode(spec, id) : null
  return node && node.level === 'paragraph' ? id : null
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

/** Counts the spec writes that changed paper.json (one undo step each). */
interface Steps {
  count: number
}

/** applyOp that counts a real change as one undo step; resolves null on failure (the store toasts why). */
async function op(steps: Steps, specOp: SpecOp): Promise<SpecResponse | null> {
  const before = useSpecStore.getState().raw
  const res = await useSpecStore.getState().applyOp(specOp)
  if (res && res.raw !== before) steps.count++
  return res
}

/** Adopt the paragraph at file:line (inserts its marker); resolves the new node id, or null. */
async function adoptAt(steps: Steps, file: string, line: number): Promise<string | null> {
  const before = useSpecStore.getState().raw
  const res = await useSpecStore.getState().adopt({ kind: 'paragraph', file, line_start: line })
  if (res && res.raw !== before) steps.count++
  return res?.new_id ?? null
}

/**
 * Run an action and report it: `work` returns the success message (or null
 * when it failed; the store has already shown why). Adds an "Undo" button
 * that undoes exactly the writes the action made.
 */
async function report(work: (steps: Steps) => Promise<string | null>): Promise<void> {
  const steps: Steps = { count: 0 }
  const message = await work(steps)
  const store = useSpecStore.getState()
  if (message === null) return
  if (steps.count === 0) {
    store.showToast(`${message} (nothing changed)`)
    return
  }
  const depth = store.undoStack.length
  store.showToast(message, 'info', {
    label: 'Undo',
    run: () => void undoSteps(steps.count, depth),
  })
}

/** Undo `count` spec writes, unless other edits happened since (`depth` no longer matches). */
async function undoSteps(count: number, depth: number): Promise<void> {
  if (useSpecStore.getState().undoStack.length !== depth) {
    useSpecStore.getState().showToast('Other spec edits happened since; use ↶ Undo in the Spec tab instead.', 'error')
    return
  }
  for (let i = 0; i < count; i++) await useSpecStore.getState().undo()
  useSpecStore.getState().showToast(count > 1 ? `Undone (${count} steps).` : 'Undone.')
}

/** “first words…” for messages. */
function quote(text: string): string {
  return `“${text.length > 60 ? `${text.slice(0, 57)}…` : text}”`
}

export type MenuAction = 'message' | 'claim' | 'thesis' | 'compare'

/** Set the node's `says`. */
async function setMessage(steps: Steps, nodeId: string, text: string): Promise<string | null> {
  const res = await op(steps, { type: 'update', id: nodeId, says: text })
  return res ? `${nodeId} now says ${quote(text)}.` : null
}

/** New claim with the text, linked to the node when there is one. */
async function liftToClaim(steps: Steps, nodeId: string | null, text: string): Promise<string | null> {
  const added = await op(steps, { type: 'add_claim', says: text })
  const claimId = added?.new_id
  if (!claimId) return null
  if (!nodeId) return `Added claim ${claimId} ${quote(text)}.`
  const linked = await op(steps, { type: 'link_claim', id: nodeId, claim_id: claimId })
  return linked ? `Lifted to claim ${claimId} and linked it to ${nodeId}.` : `Added claim ${claimId}, but linking it to ${nodeId} failed.`
}

/** Carry out one menu item for the menu's selection. */
export async function runMenuAction(menu: EditorMenu, action: MenuAction): Promise<void> {
  const { text, nodeId, file } = menu
  if (action === 'compare') {
    if (!nodeId) return
    const spec = useSpecStore.getState().spec
    const node = spec ? findNode(spec, nodeId) : null
    useCompareStore.getState().openFor({ file, node_id: nodeId, says: node?.says })
    return
  }
  if (!text) {
    useSpecStore.getState().showToast('The selection is empty.', 'error')
    return
  }
  if (action === 'thesis') {
    await report(async (steps) => {
      const spec = useSpecStore.getState().spec
      if (!spec) return null
      const res = await op(steps, { type: 'update', id: spec.message.id, fields: { thesis: text } })
      return res ? `Argument set to ${quote(text)}.` : null
    })
    return
  }
  await report(async (steps) => {
    let id = nodeId
    if (!id) {
      // "Adopt this paragraph first": insert its marker, then act on the new node.
      id = await adoptAt(steps, file, menu.lineStart)
      if (!id) return null
    }
    const done = action === 'message' ? await setMessage(steps, id, text) : await liftToClaim(steps, id, text)
    if (done && !nodeId) return `Adopted the paragraph as ${id}. ${done}`
    return done
  })
}
