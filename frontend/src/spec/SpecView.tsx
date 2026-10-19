// SpecView: the "Spec" tab, the editor for paper.json (phase 3).
//
//   [Outliner | Raw JSON]                                   ↶ undo  ↷ redo
//   ┌ ClaimBank ─┐┌ Outliner ───────────────────────────┐┌ Coverage ──────┐
//   │ claim cards││ message card, sections, paragraphs, ││ claims×sections│
//   │            ││ skeleton from the .tex               ││ matrix (ph. 5) │
//   └────────────┘└──────────────────────────────────────┘└────────────────┘
//
// The view stays mounted (hidden) while other tabs show, like the editor, so
// half-typed text and scroll positions survive tab switches, and drags can
// cross into the Editor tab (see SpecDnd.tsx).
//
// It loads the spec whenever a repo is opened. Ctrl/Cmd-Z and Ctrl/Cmd-Shift-Z
// (or Ctrl-Y) undo and redo spec changes while this tab is showing, except
// when the focus is in a CodeMirror editor (the Raw JSON tab), which has its
// own text undo. Pending typing is sent first, so undo reverts it as one step.
import { useEffect, useState } from 'react'
import { flushPendingEdits, useSpecStore } from '../state/spec'
import { useStore } from '../state/store'
import ClaimBank from './ClaimBank'
import Coverage from './Coverage'
import Outliner from './Outliner'
import RawJson from './RawJson'

type SpecTab = 'outliner' | 'raw'

export default function SpecView({ active }: { active: boolean }) {
  const repoPath = useStore((s) => s.repo?.path ?? null)
  const [tab, setTab] = useState<SpecTab>('outliner')
  const undoCount = useSpecStore((s) => s.undoStack.length)
  const redoCount = useSpecStore((s) => s.redoStack.length)
  const writing = useSpecStore((s) => s.writing)
  const undo = useSpecStore((s) => s.undo)
  const redo = useSpecStore((s) => s.redo)

  // Load (or reload) the spec when a repo is opened.
  useEffect(() => {
    if (repoPath) void useSpecStore.getState().load()
  }, [repoPath])

  // Global undo/redo keys while the Spec tab shows.
  useEffect(() => {
    if (!active) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.defaultPrevented || !(e.ctrlKey || e.metaKey) || e.altKey) return
      const key = e.key.toLowerCase()
      const isUndo = key === 'z' && !e.shiftKey
      const isRedo = (key === 'z' && e.shiftKey) || (key === 'y' && !e.shiftKey)
      if (!isUndo && !isRedo) return
      const target = e.target as HTMLElement | null
      // CodeMirror undoes its own text; so do the text boxes of panels laid
      // over the Spec view (git drawer, compare dialog), marked data-native-undo.
      if (target?.closest?.('.cm-editor, [data-native-undo]')) return
      e.preventDefault()
      flushPendingEdits()
      const store = useSpecStore.getState()
      void (isUndo ? store.undo() : store.redo())
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [active])

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col" data-testid="spec-view">
      <div className="flex items-center gap-2 border-b border-base-300 px-3 py-1">
        <div role="tablist" className="tabs-border tabs tabs-sm">
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'outliner'}
            className={`tab ${tab === 'outliner' ? 'tab-active' : ''}`}
            onClick={() => setTab('outliner')}
          >
            Outliner
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'raw'}
            className={`tab ${tab === 'raw' ? 'tab-active' : ''}`}
            onClick={() => setTab('raw')}
          >
            Raw JSON
          </button>
        </div>
        <div className="ml-auto flex items-center gap-1">
          {writing > 0 && (
            <span className="loading loading-xs loading-spinner text-base-content/40" title="Saving paper.json…" />
          )}
          <button
            type="button"
            className="btn btn-ghost btn-xs"
            onClick={() => {
              flushPendingEdits()
              void undo()
            }}
            disabled={undoCount === 0}
            title={`Undo the last spec change (Ctrl/Cmd-Z) — ${undoCount} step(s)`}
            data-testid="spec-undo"
          >
            ↶ Undo
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-xs"
            onClick={() => void redo()}
            disabled={redoCount === 0}
            title={`Redo (Ctrl/Cmd-Shift-Z) — ${redoCount} step(s)`}
            data-testid="spec-redo"
          >
            ↷ Redo
          </button>
        </div>
      </div>

      <div className={`${tab === 'outliner' ? 'flex' : 'hidden'} min-h-0 flex-1`}>
        <ClaimBank />
        <Outliner />
        <Coverage />
      </div>
      {/* Mounted only while visible; RawJson keeps unsaved text itself. */}
      {tab === 'raw' && active && <RawJson />}
    </div>
  )
}

/** Short messages from the spec editor ("cannot demote ..."), bottom right; shown on every tab. */
export function SpecToast() {
  const toast = useSpecStore((s) => s.toast)
  const dismiss = useSpecStore((s) => s.dismissToast)
  if (!toast) return null
  return (
    <div className="toast z-50 toast-end toast-bottom">
      <div
        role={toast.kind === 'error' ? 'alert' : 'status'}
        className={`alert alert-soft py-2 text-sm ${toast.kind === 'error' ? 'alert-warning' : 'alert-info'}`}
        data-testid="spec-toast"
      >
        <span className="max-w-md">{toast.text}</span>
        {toast.action && (
          <button
            type="button"
            className="btn btn-outline btn-xs"
            onClick={() => {
              dismiss()
              toast.action?.run()
            }}
            data-testid="spec-toast-action"
          >
            {toast.action.label}
          </button>
        )}
        <button type="button" className="btn btn-ghost btn-xs" onClick={dismiss} aria-label="Dismiss">
          ✕
        </button>
      </div>
    </div>
  )
}
