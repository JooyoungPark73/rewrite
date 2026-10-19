// RawJson: paper.json as text, for when the outliner gets in the way.
//
// - A CodeMirror editor (JSON highlighting) showing the file exactly as the
//   backend serialized it (sorted keys, one node per line).
// - Save (or Ctrl/Cmd-S inside the editor) sends the text to PUT
//   /api/spec/raw. The backend parses, validates and re-serializes it; the
//   editor then shows the canonical text. If it is invalid (422) nothing is
//   written and the errors are listed above the editor.
// - Reload throws away unsaved edits and shows the file on disk again.
// - When paper.json changes elsewhere (an outliner edit, another tab) and
//   there are no unsaved edits here, the new text replaces the old.
// - Errors of the file on disk itself (GET /api/spec) are listed too, e.g.
//   after a hand edit outside the app broke it.
// Ctrl/Cmd-Z inside this editor undoes typing, not the spec (SpecView skips
// its global undo when the focus is in a CodeMirror editor).
//
// The component is mounted only while the Raw JSON tab is on screen (a second,
// hidden CodeMirror would confuse page-wide `.cm-content` lookups). Unsaved
// text is kept in `drafts` below when it unmounts and restored next time.
//
// Drafts belong to a repo (audit fixes 2026-10-18). Unsaved text is filed
// under the repo whose paper.json it was edited from, and is only ever shown
// and saved for that repo:
//   - opening another repo while this tab is on screen files the text under
//     the old repo and shows the new repo's paper.json, with a fresh editor
//     history (so Ctrl-Z cannot bring the old repo's text back);
//   - reopening the old repo brings its draft back, still unsaved;
//   - Save is disabled while the text on screen is not the open repo's, and
//     the spec store refuses such a write anyway (saveRaw takes the repo).
// Typing while a save is in flight is kept: the answer's canonical text
// replaces the editor text only if nothing was typed since the request left;
// otherwise it just becomes the "saved" baseline and the text stays unsaved.
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands'
import { json } from '@codemirror/lang-json'
import { bracketMatching, defaultHighlightStyle, syntaxHighlighting } from '@codemirror/language'
import { EditorState, type Extension } from '@codemirror/state'
import { EditorView, keymap, lineNumbers } from '@codemirror/view'
import { useEffect, useRef, useState } from 'react'
import type { SpecError } from '../api/client'
import { useSpecStore } from '../state/spec'
import { useStore } from '../state/store'

/** Unsaved text from earlier visits, by repo path (only repos that had some). */
const drafts = new Map<string, string>()

/** The draft kept for `repo`, or null when there is none or it equals the file. */
function draftFor(repo: string | null, raw: string): string | null {
  if (repo === null) return null
  const text = drafts.get(repo)
  return text !== undefined && text !== raw ? text : null
}

/**
 * The CodeMirror extensions of the Raw JSON editor: line numbers, undo
 * history, JSON highlighting, Ctrl/Cmd-S calling `onSave`, and
 * `onDocChanged` after every change of the text.
 */
function rawJsonExtensions(onSave: () => void, onDocChanged: (doc: string) => void): Extension[] {
  return [
    lineNumbers(),
    history(),
    bracketMatching(),
    json(),
    syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
    keymap.of([
      {
        key: 'Mod-s',
        preventDefault: true,
        run: () => {
          onSave()
          return true
        },
      },
      ...defaultKeymap,
      ...historyKeymap,
    ]),
    EditorView.updateListener.of((update) => {
      if (update.docChanged) onDocChanged(update.state.doc.toString())
    }),
  ]
}

export default function RawJson() {
  const raw = useSpecStore((s) => s.raw)
  // The repo whose paper.json the spec store holds, and the repo the backend
  // has open. They differ for a moment after another repo is opened.
  const specRepo = useSpecStore((s) => s.repoPath)
  const openRepo = useStore((s) => s.repo?.path ?? null)
  const switchingRepo = useStore((s) => s.switchingRepo)
  const fileErrors = useSpecStore((s) => s.errors)
  const hostRef = useRef<HTMLDivElement>(null)
  const viewRef = useRef<EditorView | null>(null)
  /** The repo whose text the editor shows; its unsaved text is filed under it. */
  const shownRepoRef = useRef(specRepo)
  /** The text the editor was last synced to (what "unsaved" compares against). */
  const baseRef = useRef(raw)
  /** Bumped on every change of the editor text, so a save can tell whether the user typed meanwhile. */
  const revisionRef = useRef(0)
  // Unsaved text from the last visit, read once on the first render.
  const [restored] = useState(() => draftFor(specRepo, useSpecStore.getState().raw))
  const [dirty, setDirty] = useState(restored !== null)
  const dirtyRef = useRef(restored !== null)
  const [saving, setSaving] = useState(false)
  const savingRef = useRef(false)
  const [saveError, setSaveError] = useState<{ detail: string; errors: SpecError[] } | null>(null)
  const [savedNote, setSavedNote] = useState(false)

  const markDirty = (value: boolean) => {
    dirtyRef.current = value
    setDirty(value)
  }

  /** The editor's extensions (made when it mounts); resetEditor() reuses them. */
  const extensionsRef = useRef<Extension[]>([])

  /** Replace the editor text (keeps the editor's own undo history). */
  const showText = (text: string) => {
    const view = viewRef.current
    baseRef.current = text
    markDirty(false)
    if (!view || view.state.doc.toString() === text) return
    view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } })
  }

  /** Start over with `text` and an empty undo history; unsaved when it differs from `base`. */
  const resetEditor = (text: string, base: string) => {
    baseRef.current = base
    revisionRef.current++
    markDirty(text !== base)
    viewRef.current?.setState(EditorState.create({ doc: text, extensions: extensionsRef.current }))
  }

  const save = async () => {
    const view = viewRef.current
    if (!view || savingRef.current) return // one save at a time (button and Ctrl/Cmd-S)
    const repo = shownRepoRef.current
    const submittedRevision = revisionRef.current
    savingRef.current = true
    setSaving(true)
    setSavedNote(false)
    const result = await useSpecStore.getState().saveRaw(view.state.doc.toString(), repo)
    savingRef.current = false
    setSaving(false)
    // Another repo was opened meanwhile: this editor now shows that repo, and
    // the old text has been filed under its own repo.
    if (shownRepoRef.current !== repo || viewRef.current !== view) return
    if (!result.ok) {
      setSaveError({ detail: result.detail, errors: result.errors })
      return
    }
    setSaveError(null)
    const canonical = useSpecStore.getState().raw
    if (revisionRef.current === submittedRevision) {
      showText(canonical) // nothing typed since: show the canonical serialization
      setSavedNote(true)
    } else {
      // Typed while the request was pending: keep that text; it is unsaved
      // relative to what has just been written.
      baseRef.current = canonical
      markDirty(view.state.doc.toString() !== canonical)
    }
  }
  const saveRef = useRef(save)
  useEffect(() => {
    saveRef.current = save
  })

  const reload = async () => {
    await useSpecStore.getState().reload()
    setSaveError(null)
    setSavedNote(false)
    showText(useSpecStore.getState().raw)
  }

  // Create the CodeMirror view once (with the unsaved draft, if any).
  useEffect(() => {
    const saved = useSpecStore.getState().raw
    extensionsRef.current = rawJsonExtensions(
      () => void saveRef.current(),
      (doc) => {
        revisionRef.current++
        const changed = doc !== baseRef.current
        if (changed !== dirtyRef.current) markDirty(changed)
      },
    )
    const view = new EditorView({
      parent: hostRef.current!,
      state: EditorState.create({ doc: restored ?? saved, extensions: extensionsRef.current }),
    })
    viewRef.current = view
    baseRef.current = saved
    const repo = shownRepoRef.current
    if (repo !== null) drafts.delete(repo) // the draft is live in the editor now
    return () => {
      // Keep unsaved text for the next time the tab opens on this repo.
      const shown = shownRepoRef.current
      if (shown !== null) {
        if (dirtyRef.current) drafts.set(shown, view.state.doc.toString())
        else drafts.delete(shown)
      }
      view.destroy()
      viewRef.current = null
    }
  }, [restored]) // `restored` never changes: this runs once per mount

  useEffect(() => {
    const view = viewRef.current
    if (specRepo !== shownRepoRef.current) {
      // Another repo's paper.json arrived: file the unsaved text under the repo
      // it was written for, then show this repo's file (or its own draft).
      const previous = shownRepoRef.current
      if (previous !== null && view) {
        if (dirtyRef.current) drafts.set(previous, view.state.doc.toString())
        else drafts.delete(previous)
      }
      shownRepoRef.current = specRepo
      const draft = draftFor(specRepo, raw)
      if (specRepo !== null) drafts.delete(specRepo)
      setSaveError(null)
      setSavedNote(false)
      resetEditor(draft ?? raw, raw)
      return
    }
    // Follow paper.json while there are no unsaved edits here.
    if (!dirtyRef.current) showText(raw)
  }, [specRepo, raw])

  // Save only sends text to the repo it came from (the store checks this too).
  const canSave = specRepo !== null && specRepo === openRepo && !switchingRepo

  const errors = saveError ? saveError.errors : fileErrors
  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="raw-json">
      <div className="flex items-center gap-2 border-b border-base-300 px-3 py-1 text-sm">
        <span className="font-mono">paper.json</span>
        {dirty && (
          <span className="text-warning" data-testid="raw-dirty">
            ● unsaved
          </span>
        )}
        {savedNote && !dirty && <span className="text-success">saved</span>}
        <button
          type="button"
          className="btn ml-auto btn-ghost btn-xs"
          onClick={() => void reload()}
          title="Discard unsaved edits and show paper.json as it is on disk"
          data-testid="raw-reload"
        >
          Reload
        </button>
        <button
          type="button"
          className="btn btn-xs btn-primary"
          onClick={() => void save()}
          disabled={!dirty || saving || !canSave}
          title={canSave ? 'Validate and write paper.json (Ctrl/Cmd-S)' : 'Loading the open repo\'s paper.json…'}
          data-testid="raw-save"
        >
          {saving && <span className="loading loading-xs loading-spinner" />}
          Save
        </button>
      </div>
      {(saveError || errors.length > 0) && (
        <div role="alert" className="alert rounded-none alert-soft py-2 text-sm alert-error" data-testid="raw-errors">
          <div className="flex flex-col gap-0.5">
            <b>{saveError ? `Not saved: ${saveError.detail}` : 'paper.json on disk is invalid:'}</b>
            <ul className="list-disc pl-5 font-mono text-xs">
              {errors.map((e, i) => (
                <li key={i} data-testid="raw-error">
                  {e.line ? `line ${e.line}${e.column ? `:${e.column}` : ''} ` : ''}
                  {e.path ? `${e.path}: ` : ''}
                  {e.message}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
      <div ref={hostRef} className="min-h-0 flex-1 overflow-hidden" />
    </div>
  )
}
