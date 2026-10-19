// Editor: a CodeMirror 6 editor for the file currently open in the store.
//
// - LaTeX highlighting comes from the legacy CodeMirror 5 `stex` mode wrapped
//   with StreamLanguage (there is no maintained CM6 LaTeX grammar).
// - Ctrl-S (Cmd-S on macOS) saves through the store, which then schedules a
//   compile.
// - One EditorView lives for the component's lifetime. Each file gets its own
//   EditorState, cached by path, so switching files and back keeps that file's
//   undo history, cursor and scroll position.
// - Cursor-line changes are reported to the store after 300 ms of quiet; the
//   store then runs a SyncTeX forward search (phase 2) unless the file has
//   unsaved edits.
// - Ctrl-Alt-J (also Cmd-Alt-J on macOS) forces a forward search for the
//   cursor line right away, even with unsaved edits.
// - `goToLine(line)` moves the cursor, scrolls the line to the middle and
//   flashes it. The store's `goToLocation` (used by the error list) reaches it
//   through `pendingJump`; parents can also call it via a ref (EditorHandle).
// - `lineAtCoords` / `lineTop` (also on the handle) let the Spec view's
//   drag-and-drop find the line under the mouse when a spec node is dropped on
//   the editor to insert its `% @p` marker (phase 3, spec/SpecDnd.tsx).
// - Phase 4, review findings (state/runs.ts):
//   * every finding listed in the sidebar for this file tints its lines by
//     severity (`setFindingHighlights`), and the selected finding gets a
//     stronger tint plus a bar in the left margin (`setActiveFinding`). These
//     stay until the selection or the list changes, unlike the 1.4 s flash of
//     "go to line". The quote itself is underlined when it is found verbatim.
//   * a `reveal` request from the runs store scrolls the finding's lines into
//     view WITHOUT focusing the editor, so `j` / `k` keep working.
//   * the selected lines are reported to the store (`editorSelection`) for
//     the run launcher's "selection" scope.
// - Phase 5: right-click on selected text, or Ctrl/Cmd-. with text selected,
//   opens the selection menu (editor/ContextMenu.tsx, logic in
//   editor/contextActions.ts): set as this paragraph's message, lift to claim,
//   set as thesis, compare with an earlier version. With nothing selected the
//   browser's own menu shows.
// - While the backend switches to another repo (store `switchingRepo`), typing
//   is ignored: the old paper's text has just been saved, and anything typed
//   into it now could no longer be saved there. The Open dialog covers the
//   editor at that moment anyway; this is a safety net.
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands'
import { bracketMatching, defaultHighlightStyle, StreamLanguage, syntaxHighlighting } from '@codemirror/language'
import { stex } from '@codemirror/legacy-modes/mode/stex'
import { EditorState, StateEffect, StateField, type Extension, type Range, type Text } from '@codemirror/state'
import {
  Decoration,
  type DecorationSet,
  drawSelection,
  EditorView,
  highlightActiveLine,
  highlightActiveLineGutter,
  keymap,
  lineNumbers,
} from '@codemirror/view'
import { useEffect, useImperativeHandle, useRef, type Ref } from 'react'
import type { Finding } from '../api/client'
import { useRunsStore, useSelectedFinding, useVisibleFileFindings } from '../state/runs'
import { useStore } from '../state/store'
import { collapseWhitespace, enclosingNodeId, useEditorMenuStore } from './contextActions'

/** Imperative API exposed through `ref`. */
export interface EditorHandle {
  goToLine: (line: number) => void
  focus: () => void
  /** 1-based line of the cursor right now (not debounced), or null when no file is open. */
  cursorLine: () => number | null
  /** 1-based line under a point given in client (viewport) pixels, or null outside the text. */
  lineAtCoords: (x: number, y: number) => number | null
  /** Top edge of a 1-based line in client pixels, or null when it is not on screen. */
  lineTop: (line: number) => number | null
}

const CURSOR_DEBOUNCE_MS = 300
const FLASH_MS = 1400

// ---------------------------------------------------------------------------
// "Flash a line" decoration
// ---------------------------------------------------------------------------
// CodeMirror draws extra styling through decorations stored in a StateField.
// `flashLine.of(pos)` adds the CSS class `cm-flash-line` (see index.css) to
// the line containing `pos`; `flashLine.of(null)` removes it.
const flashLine = StateEffect.define<number | null>()

const flashField = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update(decorations, tr) {
    let next = decorations.map(tr.changes) // keep it on the same line while typing
    for (const effect of tr.effects) {
      if (effect.is(flashLine)) {
        next =
          effect.value === null
            ? Decoration.none
            : Decoration.set([Decoration.line({ class: 'cm-flash-line' }).range(effect.value)])
      }
    }
    return next
  },
  provide: (field) => EditorView.decorations.from(field),
})

function goToLine(view: EditorView, line: number): void {
  const doc = view.state.doc
  const target = Math.min(Math.max(1, Math.floor(line)), doc.lines)
  const pos = doc.line(target).from
  view.dispatch({
    selection: { anchor: pos },
    effects: [EditorView.scrollIntoView(pos, { y: 'center' }), flashLine.of(pos)],
    userEvent: 'select',
  })
  view.focus()
  setTimeout(() => {
    // The view may have been destroyed (component unmounted) in the meantime.
    if (view.dom.isConnected) view.dispatch({ effects: flashLine.of(null) })
  }, FLASH_MS)
}

// ---------------------------------------------------------------------------
// Review findings: persistent line highlights (phase 4)
// ---------------------------------------------------------------------------
// The component tells the editor which line ranges to tint with two effects:
//   setFindingHighlights.of(ranges)  every finding listed for this file (faint tint)
//   setActiveFinding.of(range|null)  the selected finding (strong tint + margin bar)
// The field remembers both lists and rebuilds its decorations whenever either
// changes; while the user types, the decorations simply move with the text.
// CSS classes (index.css): cm-finding, cm-finding-<severity>, cm-finding-active,
// cm-finding-quote.

/** A finding's lines (1-based, inclusive) as the editor needs them. */
export interface FindingRange {
  id: string
  lineStart: number
  lineEnd: number
  severity: string
  quote: string
}

const setFindingHighlights = StateEffect.define<FindingRange[]>()
const setActiveFinding = StateEffect.define<FindingRange | null>()

interface FindingDecorations {
  ranges: FindingRange[]
  active: FindingRange | null
  decorations: DecorationSet
}

/** Clamp a 1-based line number into the document. */
function clampLine(doc: Text, line: number): number {
  return Math.min(Math.max(1, Math.floor(line) || 1), doc.lines)
}

function buildFindingDecorations(doc: Text, ranges: FindingRange[], active: FindingRange | null): DecorationSet {
  const decos: Range<Decoration>[] = []
  const lineDecos = (range: FindingRange, attrs: { class: string; attributes?: Record<string, string> }) => {
    const first = clampLine(doc, range.lineStart)
    const last = Math.max(first, clampLine(doc, range.lineEnd))
    for (let n = first; n <= last; n++) decos.push(Decoration.line(attrs).range(doc.line(n).from))
  }
  for (const range of ranges) lineDecos(range, { class: `cm-finding cm-finding-${range.severity}` })
  if (active) {
    lineDecos(active, {
      class: `cm-finding-active cm-finding-active-${active.severity}`,
      attributes: { 'data-finding-id': active.id },
    })
    // Underline the quote when it appears verbatim inside the range.
    const from = doc.line(clampLine(doc, active.lineStart)).from
    const to = doc.line(Math.max(clampLine(doc, active.lineStart), clampLine(doc, active.lineEnd))).to
    const at = active.quote ? doc.sliceString(from, to).indexOf(active.quote) : -1
    if (at >= 0) decos.push(Decoration.mark({ class: 'cm-finding-quote' }).range(from + at, from + at + active.quote.length))
  }
  return Decoration.set(decos, true) // true = sort them (required by CodeMirror)
}

const findingField = StateField.define<FindingDecorations>({
  create: () => ({ ranges: [], active: null, decorations: Decoration.none }),
  update(value, tr) {
    let { ranges, active } = value
    let changed = false
    for (const effect of tr.effects) {
      if (effect.is(setFindingHighlights)) {
        ranges = effect.value
        changed = true
      } else if (effect.is(setActiveFinding)) {
        active = effect.value
        changed = true
      }
    }
    if (changed) return { ranges, active, decorations: buildFindingDecorations(tr.state.doc, ranges, active) }
    if (tr.docChanged) return { ...value, decorations: value.decorations.map(tr.changes) }
    return value
  },
  provide: (field) => EditorView.decorations.from(field, (value) => value.decorations),
})

function toRange(f: Finding): FindingRange {
  return { id: f.id, lineStart: f.anchor_line_start, lineEnd: f.anchor_line_end, severity: f.severity, quote: f.quote }
}

/**
 * Scroll a finding's lines into view without focusing the editor (so `j`/`k`
 * keep working), optionally putting the cursor on its first line.
 */
function revealLines(view: EditorView, lineStart: number, moveCursor: boolean): void {
  const pos = view.state.doc.line(clampLine(view.state.doc, lineStart)).from
  view.dispatch({
    selection: moveCursor ? { anchor: pos } : undefined,
    effects: EditorView.scrollIntoView(pos, { y: 'center' }),
    userEvent: moveCursor ? 'select' : undefined,
  })
}

// ---------------------------------------------------------------------------
// Extensions shared by every file's EditorState
// ---------------------------------------------------------------------------
// They only talk to the store through useStore.getState(), never through
// component state, so cached EditorStates stay valid across re-mounts.
let cursorTimer: ReturnType<typeof setTimeout> | undefined

/** 1-based line number of the main cursor. */
function lineOfCursor(state: EditorState): number {
  return state.doc.lineAt(state.selection.main.head).number
}

/** The selected lines, or null when the selection is empty. */
function selectedLines(state: EditorState): { line_start: number; line_end: number } | null {
  const sel = state.selection.main
  if (sel.empty) return null
  const first = state.doc.lineAt(sel.from).number
  // A selection ending at the very start of a line does not include that line.
  let last = state.doc.lineAt(sel.to).number
  if (last > first && state.doc.line(last).from === sel.to) last--
  return { line_start: first, line_end: last }
}

const reportChanges = EditorView.updateListener.of((update) => {
  if (update.docChanged) {
    // Mirror the text into the store, which derives the dirty flag from it.
    useStore.getState().setContent(update.state.doc.toString())
  }
  if (update.docChanged || update.selectionSet) {
    const store = useStore.getState()
    const lines = selectedLines(update.state)
    const path = store.activeFile?.path
    store.setEditorSelection(lines && path ? { path, ...lines } : null)
  }
  if (update.docChanged || update.selectionSet) {
    const line = lineOfCursor(update.state)
    // Remember which file this line belongs to: if another file is opened
    // before the timer fires, the report is stale and must be dropped.
    const path = useStore.getState().activeFile?.path
    clearTimeout(cursorTimer)
    cursorTimer = setTimeout(() => {
      const store = useStore.getState()
      if (store.activeFile?.path === path) store.setCursorLine(line)
    }, CURSOR_DEBOUNCE_MS)
  }
})

/**
 * Open the selection menu (phase 5) for the current selection, at `at`
 * (viewport pixels) or just below the selection. Returns false when nothing
 * is selected, so the key or the browser's own menu does its usual thing.
 */
function openSelectionMenu(view: EditorView, at: { x: number; y: number } | null): boolean {
  const sel = view.state.selection.main
  const path = useStore.getState().activeFile?.path
  if (sel.empty || !path) return false
  const doc = view.state.doc
  const lineStart = doc.lineAt(sel.from).number
  let lineEnd = doc.lineAt(sel.to).number
  if (lineEnd > lineStart && doc.line(lineEnd).from === sel.to) lineEnd--
  const coords = at ?? (() => {
    const c = view.coordsAtPos(sel.head)
    return c ? { x: c.left, y: c.bottom + 4 } : { x: 200, y: 200 }
  })()
  useEditorMenuStore.getState().open({
    ...coords,
    file: path,
    text: collapseWhitespace(view.state.sliceDoc(sel.from, sel.to)),
    lineStart,
    lineEnd,
    nodeId: enclosingNodeId(doc.toString().split('\n'), lineStart),
    restoreFocus: () => view.focus(),
  })
  return true
}

/** Right-click with text selected opens the selection menu instead of the browser's. */
const selectionMenuOnRightClick = EditorView.domEventHandlers({
  contextmenu: (event, view) => {
    if (!openSelectionMenu(view, { x: event.clientX, y: event.clientY })) return false
    event.preventDefault()
    return true
  },
})

/**
 * Drop text changes while the backend switches repos (see the header). A
 * transaction filter sees every change before it is applied; returning []
 * cancels it. Loading another file (view.setState) is not a transaction, so
 * the new repo's text still arrives.
 */
const freezeWhileSwitchingRepo = EditorState.transactionFilter.of((tr) =>
  tr.docChanged && useStore.getState().switchingRepo ? [] : tr,
)

/** Ctrl/Cmd-Alt-J: forward SyncTeX for the cursor line now. */
function syncCursorLine(view: EditorView): boolean {
  void useStore.getState().syncForward({ line: lineOfCursor(view.state), explicit: true })
  return true
}

const fileExtensions: Extension[] = [
  lineNumbers(),
  highlightActiveLineGutter(),
  highlightActiveLine(),
  history(),
  drawSelection(),
  bracketMatching(),
  EditorView.lineWrapping,
  StreamLanguage.define(stex),
  syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
  keymap.of([
    // Listed first so it wins over any default binding; preventDefault stops
    // the browser's own "Save page" dialog.
    {
      key: 'Mod-s',
      preventDefault: true,
      run: () => {
        void useStore.getState().save()
        return true
      },
    },
    // Show the cursor line in the PDF. Mod is Cmd on macOS, where Chrome may
    // keep Cmd-Alt-J for its developer console, so Ctrl-Alt-J works there too.
    { key: 'Mod-Alt-j', preventDefault: true, run: syncCursorLine },
    { key: 'Ctrl-Alt-j', preventDefault: true, run: syncCursorLine },
    // Phase 5: the selection menu from the keyboard (only with text selected).
    { key: 'Mod-.', run: (view) => openSelectionMenu(view, null) },
    ...defaultKeymap,
    ...historyKeymap,
  ]),
  flashField,
  findingField,
  reportChanges,
  selectionMenuOnRightClick,
  freezeWhileSwitchingRepo,
]

/** Shown when no file is open. */
function emptyState(): EditorState {
  return EditorState.create({ doc: '', extensions: [EditorState.readOnly.of(true), EditorView.editable.of(false)] })
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------
export default function Editor({ ref }: { ref?: Ref<EditorHandle> }) {
  const hostRef = useRef<HTMLDivElement>(null)
  const viewRef = useRef<EditorView | null>(null)
  /** EditorState per file path (undo history, selection, scroll). */
  const cacheRef = useRef(new Map<string, EditorState>())
  /** Which file version the view currently shows. */
  const shownRef = useRef<{ path: string; loadId: number } | null>(null)

  const path = useStore((s) => s.activeFile?.path ?? null)
  const loadId = useStore((s) => s.activeFile?.loadId ?? 0)
  const pendingJump = useStore((s) => s.pendingJump)
  const clearPendingJump = useStore((s) => s.clearPendingJump)
  // Phase 4: findings to highlight, the selected one, and scroll requests.
  const visibleFindings = useVisibleFileFindings()
  const selectedFinding = useSelectedFinding()
  const reveal = useRunsStore((s) => s.reveal)

  // Create the EditorView once; destroy it on unmount.
  useEffect(() => {
    const view = new EditorView({ parent: hostRef.current!, state: emptyState() })
    viewRef.current = view
    const cache = cacheRef.current
    return () => {
      if (shownRef.current) cache.set(shownRef.current.path, view.state)
      shownRef.current = null
      view.destroy()
      viewRef.current = null
    }
  }, [])

  // Swap in the right EditorState whenever a different file (or a fresh copy
  // of the same file from disk) is loaded into the store.
  useEffect(() => {
    const view = viewRef.current
    if (!view) return
    const file = useStore.getState().activeFile
    const shown = shownRef.current
    if (!file) {
      if (shown) cacheRef.current.set(shown.path, view.state)
      shownRef.current = null
      view.setState(emptyState())
      return
    }
    if (shown && shown.path === file.path && shown.loadId === file.loadId) return
    if (shown) cacheRef.current.set(shown.path, view.state)

    // Reuse the cached state (and its undo history) only if it still matches
    // what is on disk; otherwise start fresh from the loaded text.
    const cached = cacheRef.current.get(file.path)
    const state =
      cached && cached.doc.toString() === file.content
        ? cached
        : EditorState.create({ doc: file.content, extensions: fileExtensions })
    view.setState(state)
    shownRef.current = { path: file.path, loadId: file.loadId }
  }, [path, loadId])

  // Carry out a jump requested through the store once its file is showing.
  // Declared after the swap effect so it runs after it in the same commit.
  useEffect(() => {
    const view = viewRef.current
    if (!view || !pendingJump || shownRef.current?.path !== pendingJump.path) return
    goToLine(view, pendingJump.line)
    clearPendingJump(pendingJump.seq)
  }, [pendingJump, path, loadId, clearPendingJump])

  // Keep the finding highlights in sync with the sidebar list and the
  // selection. Runs after every file swap too, because each file's
  // EditorState carries its own (possibly outdated) decorations.
  useEffect(() => {
    const view = viewRef.current
    const shownPath = shownRef.current?.path
    if (!view || !shownPath) return
    const ranges = visibleFindings.filter((f) => f.file === shownPath).map(toRange)
    const active = selectedFinding && selectedFinding.file === shownPath ? toRange(selectedFinding) : null
    view.dispatch({ effects: [setFindingHighlights.of(ranges), setActiveFinding.of(active)] })
  }, [visibleFindings, selectedFinding, path, loadId])

  // Scroll to a finding once its file is showing (requested by state/runs.ts).
  useEffect(() => {
    const view = viewRef.current
    if (!view || !reveal || shownRef.current?.path !== reveal.file) return
    revealLines(view, reveal.line_start, reveal.moveCursor)
    useRunsStore.getState().clearReveal(reveal.seq)
  }, [reveal, path, loadId])

  useImperativeHandle(
    ref,
    () => ({
      goToLine: (line: number) => {
        if (viewRef.current) goToLine(viewRef.current, line)
      },
      focus: () => viewRef.current?.focus(),
      cursorLine: () => (viewRef.current && shownRef.current ? lineOfCursor(viewRef.current.state) : null),
      lineAtCoords: (x: number, y: number) => {
        const view = viewRef.current
        if (!view || !shownRef.current) return null
        const box = view.scrollDOM.getBoundingClientRect()
        if (x < box.left || x > box.right || y < box.top || y > box.bottom) return null
        // `false` = estimate a position even below the last line.
        const pos = view.posAtCoords({ x, y }, false)
        return view.state.doc.lineAt(pos).number
      },
      lineTop: (line: number) => {
        const view = viewRef.current
        if (!view || !shownRef.current) return null
        const doc = view.state.doc
        const target = Math.min(Math.max(1, Math.floor(line)), doc.lines)
        return view.coordsAtPos(doc.line(target).from)?.top ?? null
      },
    }),
    [],
  )

  return (
    <div className="relative h-full min-h-0 bg-base-100" data-testid="editor">
      <div ref={hostRef} className="h-full" />
      {!path && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-base-content/40">
          Select a file in the tree
        </div>
      )}
    </div>
  )
}
