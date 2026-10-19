// Spec editor state: a second zustand store, kept apart from the editor store
// in store.ts so each file stays readable.
//
// It holds paper.json as last returned by the backend, plus what the Spec view
// needs around it: the status dot of every node, the skeleton parsed from the
// .tex files, the selected row, undo/redo, and small UI state (collapsed
// sections, rows whose fields are expanded).
//
// Write path. Every change is ONE request; the backend applies it to
// paper.json on disk and answers with the whole new spec:
//   applyOp(op)    POST /api/spec/ops     every outliner and claim-bank edit
//   adopt(req)     POST /api/spec/adopt   skeleton "Adopt", marker drops
//   saveRaw(text)  PUT  /api/spec/raw     Raw JSON tab
//   undo / redo    PUT  /api/spec         a full earlier snapshot
// Writes run one at a time in the order they were asked for (a promise
// chain), so quick edits can never overtake each other.
//
// Undo. Before each write the current spec is pushed onto `undoStack` (at most
// 100 snapshots); undo PUTs the top snapshot back and moves the current spec to
// `redoStack`; any new edit clears `redoStack`. Undo restores paper.json only:
// a `% @p` marker that adopt wrote into a .tex file stays there (the editor's
// own undo removes it).
//
// Live updates. The backend broadcasts `spec.changed` after every write to
// paper.json (ours included) and `file.changed` when a file changes on disk.
// A spec reload is skipped while one of our writes is queued or in flight, and
// its answer is dropped if a write started meanwhile (`writeSeq`, the "my last
// write" counter); a reload that returns the text we already have changes
// nothing. So our own echoes never put an older spec on screen. A changed
// .tex file only refreshes the status dots and the skeleton.
//
// Typing. `says` and field inputs keep their own text while an edit is pending
// (spec/useSyncedText.ts) and send it as an `update` op 400 ms after the last
// keystroke, or on blur. Pending edits register here so that undo and the
// structural keys (Enter, Tab, ...) can send them first: flushPendingEdits().
//
// External changes and undo (audit fixes 2026-10-18). The undo/redo stacks
// hold whole earlier documents, so restoring one after paper.json changed
// elsewhere (another tab, git pull, a hand edit) would silently drop that
// change. Therefore:
//   - a reload that brings in different content clears both stacks and says
//     so ("History cleared: paper.json changed outside the editor");
//   - undo/redo first re-read paper.json and refuse (clearing the history and
//     showing the file as it is) if it differs from the text the history was
//     built on, i.e. the `raw` this store last saw. This catches changes whose
//     spec.changed event was missed, e.g. while the socket was down.
//
// Repo identity. `repoPath` is the repo whose paper.json the store holds.
// Opening another repo replaces it via load(); until then the old repo's
// content is still here, so no write is sent: saveRaw() takes the repo its
// text came from and refuses to send it anywhere else, ops and adoptions
// queued for the old repo are dropped with a toast, and undo/redo do nothing.
import { useMemo } from 'react'
import { create } from 'zustand'
import * as api from '../api/client'
import type {
  AdoptRequest,
  LabelInfo,
  NodeStatus,
  PaperSpec,
  ServerEvent,
  Skeleton,
  SpecError,
  SpecOp,
  SpecResponse,
} from '../api/client'
import { findNode, locate } from '../spec/tree'
import { useStore } from './store'

/** Most snapshots kept for undo (and for redo). */
export const UNDO_LIMIT = 100
/** Typing in a `says` or field input is sent this long after the last keystroke. */
export const EDIT_DEBOUNCE_MS = 400
const RELOAD_DEBOUNCE_MS = 150
const TOAST_MS = 4000
/** Toasts with a button (Undo) stay longer, so there is time to press it. */
const ACTION_TOAST_MS = 8000

/** A short message at the bottom right of the Spec view. */
export interface SpecToast {
  text: string
  kind: 'info' | 'error'
  seq: number
  /** Optional button in the toast, e.g. "Undo" after an editor context-menu action (phase 5). */
  action?: ToastAction
}

export interface ToastAction {
  label: string
  run: () => void
}

/** Ask the row (or claim card) with this id to focus its text input. */
export interface FocusRequest {
  id: string
  seq: number
}

/** Result of saveRaw: ok, or the backend's validation errors (422). */
export type RawSaveResult = { ok: true } | { ok: false; detail: string; errors: SpecError[] }

export interface SpecState {
  /** True once the first GET /api/spec finished (successfully or not). */
  loaded: boolean
  /** Set when GET /api/spec itself failed (backend down, no repo). */
  loadError: string | null
  /** The parsed paper.json; null when the file is missing or invalid (see `errors`). */
  spec: PaperSpec | null
  /** paper.json's text as the backend serialized it. */
  raw: string
  /** The repo `spec` and `raw` belong to (null before the first load). */
  repoPath: string | null
  /** Validation errors of the file on disk (from GET /api/spec). */
  errors: SpecError[]
  /** Status dot per section/paragraph id. */
  status: Record<string, NodeStatus>
  skeleton: Skeleton | null
  /** The paper's \label targets (GET /api/spec/labels), refreshed with the skeleton. */
  labels: LabelInfo[]
  /** Row last focused or clicked in the outliner. */
  selectedId: string | null
  undoStack: PaperSpec[]
  redoStack: PaperSpec[]
  /** Number of writes queued or in flight (for a small "saving" spinner). */
  writing: number
  toast: SpecToast | null
  focusRequest: FocusRequest | null
  /** Sections whose paragraphs are hidden. */
  collapsed: Record<string, boolean>
  /** Rows (and claim cards) whose fields + notes editor is open. */
  expanded: Record<string, boolean>
  /** Write the node's `says` as a LaTeX comment next to markers inserted from the spec. */
  markerWithSays: boolean

  /** Load the spec for a newly opened repo (clears undo/redo). */
  load: () => Promise<void>
  /**
   * Re-read paper.json; resolves true if it changed. A change made outside
   * this editor clears undo/redo (see "External changes and undo" above).
   */
  reload: () => Promise<boolean>
  /** Apply one edit. Resolves to the backend's answer, or null if it failed (a toast says why). */
  applyOp: (op: SpecOp) => Promise<SpecResponse | null>
  undo: () => Promise<void>
  redo: () => Promise<void>
  /**
   * Write paper.json from the Raw JSON tab. `repoPath` is the repo the text
   * was edited for; the write is refused unless that repo is the one open.
   */
  saveRaw: (raw: string, repoPath: string | null) => Promise<RawSaveResult>
  refreshStatus: () => Promise<void>
  refreshSkeleton: () => Promise<void>
  refreshLabels: () => Promise<void>
  /** Adopt a heading or paragraph of the .tex (inserts a marker for paragraphs). */
  adopt: (request: AdoptRequest) => Promise<SpecResponse | null>
  /**
   * Insert node `id`'s `% @p` marker above the paragraph starting at
   * file:lineStart and show it in the editor. With `markerWithSays` on, a
   * `% says: ...` comment line is also written right below the marker.
   */
  insertMarker: (id: string, file: string, lineStart: number) => Promise<boolean>
  /** "The prose is right": clear `stale` without editing. */
  reconcile: (id: string) => Promise<void>

  select: (id: string | null) => void
  requestFocus: (id: string) => void
  clearFocusRequest: (seq: number) => void
  /** Show a toast; with `action` it gets a button and stays a little longer. */
  showToast: (text: string, kind?: SpecToast['kind'], action?: ToastAction) => void
  dismissToast: () => void
  toggleCollapsed: (id: string) => void
  toggleExpanded: (id: string) => void
  setMarkerWithSays: (on: boolean) => void
  handleServerEvent: (event: ServerEvent) => void
}

// ---------------------------------------------------------------------------
// Module-level bookkeeping (never rendered, so kept out of the store)
// ---------------------------------------------------------------------------
let chain: Promise<unknown> = Promise.resolve() // serializes writes
let queuedWrites = 0 // writes queued or in flight
let writeSeq = 0 // bumped for every write requested; reloads compare it
let loadToken = 0 // last load() wins (a quick switch A -> B must not end on A)
let statusToken = 0 // last refreshStatus() wins
let skeletonToken = 0 // last refreshSkeleton() wins
let labelsToken = 0 // last refreshLabels() wins
let reloadTimer: ReturnType<typeof setTimeout> | undefined
let refreshTimer: ReturnType<typeof setTimeout> | undefined
let toastTimer: ReturnType<typeof setTimeout> | undefined
let focusCounter = 0
let toastCounter = 0

/** Flush functions of inputs whose text has not been sent yet, by input key. */
const pendingEdits = new Map<string, () => void>()

/** Called by useSyncedText when an input has unsent text. */
export function registerPendingEdit(key: string, flush: () => void): void {
  pendingEdits.set(key, flush)
}

export function unregisterPendingEdit(key: string): void {
  pendingEdits.delete(key)
}

/** Send every pending text edit now (before undo, Enter, Tab, ...). */
export function flushPendingEdits(): void {
  for (const flush of [...pendingEdits.values()]) flush()
}

/** Ops that only change text or links: the skeleton (adopted headings) cannot change. */
const TEXT_ONLY_OPS = new Set<SpecOp['type']>([
  'update',
  'update_claim',
  'add_claim',
  'link_claim',
  'unlink_claim',
  'lift_to_claim',
])

/** Toast for a write dropped because another repo is being opened. */
const OTHER_REPO = 'another repo is being opened; paper.json of the old one was not changed'

/** The text of the history-cleared notice (also checked by the e2e tests). */
const EXTERNAL_CHANGE = 'paper.json changed outside the editor'

/**
 * True when the spec on screen belongs to the repo the backend has open and
 * no repo switch is under way, i.e. a write would land where its text came from.
 */
function specRepoIsOpen(repoPath: string | null): boolean {
  const editor = useStore.getState()
  return repoPath !== null && !editor.switchingRepo && editor.repo?.path === repoPath
}

/** One-line description of a failed write, including the first validation error. */
function failureText(prefix: string, err: unknown): string {
  const first = api.specErrorsOf(err)[0]
  const detail = api.errorMessage(err)
  return first ? `${prefix}: ${detail} (${first.path}: ${first.message})` : `${prefix}: ${detail}`
}

export const useSpecStore = create<SpecState>()((set, get) => {
  /** Queue a write behind all earlier ones. */
  const enqueueWrite = <T>(task: () => Promise<T>): Promise<T> => {
    queuedWrites++
    writeSeq++
    set((s) => ({ writing: s.writing + 1 }))
    const run = chain.then(task).finally(() => {
      queuedWrites--
      set((s) => ({ writing: s.writing - 1 }))
    })
    chain = run.catch(() => undefined)
    return run
  }

  /** State after a successful write: new spec, `previous` pushed for undo, redo cleared. */
  const committed = (s: SpecState, res: SpecResponse, previous: PaperSpec | null): Partial<SpecState> => {
    // A write that changed nothing (same text) adds no undo step.
    if (res.raw === s.raw) return { spec: res.spec, errors: [] }
    return {
      spec: res.spec,
      raw: res.raw,
      errors: [],
      undoStack: previous ? [...s.undoStack, previous].slice(-UNDO_LIMIT) : s.undoStack,
      redoStack: [],
    }
  }

  /** Refresh status (and the skeleton) after a write, without waiting. */
  const afterWrite = (skeletonToo: boolean) => {
    void get().refreshStatus()
    if (skeletonToo) {
      void get().refreshSkeleton()
      void get().refreshLabels()
    }
  }

  /** Shared body of undo and redo. */
  const restore = (from: 'undoStack' | 'redoStack') =>
    enqueueWrite(async () => {
      const state = get()
      const stack = state[from]
      const target = stack[stack.length - 1]
      const current = state.spec
      if (!target || !current) return
      // The history belongs to the repo it was made in; another repo is being opened.
      if (!specRepoIsOpen(state.repoPath)) return
      const label = from === 'undoStack' ? 'Undo' : 'Redo'
      try {
        // Precondition: PUT replaces the whole file, so only do it if the file
        // is still the text this history was built on (`raw`). Otherwise the
        // restore would wipe out whatever changed it.
        const disk = await api.getSpec()
        if ((disk.raw ?? '') !== state.raw) {
          set({
            spec: disk.spec,
            raw: disk.raw ?? '',
            errors: disk.errors ?? [],
            loadError: null,
            undoStack: [],
            redoStack: [],
          })
          get().showToast(`${label} refused: ${EXTERNAL_CHANGE}. History cleared; showing the file as it is now.`, 'error')
          afterWrite(true)
          return
        }
        const res = await api.putSpec(target)
        set((s) =>
          from === 'undoStack'
            ? {
                spec: res.spec,
                raw: res.raw,
                errors: [],
                undoStack: s.undoStack.slice(0, -1),
                redoStack: [...s.redoStack, current].slice(-UNDO_LIMIT),
              }
            : {
                spec: res.spec,
                raw: res.raw,
                errors: [],
                redoStack: s.redoStack.slice(0, -1),
                undoStack: [...s.undoStack, current].slice(-UNDO_LIMIT),
              },
        )
        afterWrite(true)
      } catch (err) {
        get().showToast(failureText(from === 'undoStack' ? 'Undo failed' : 'Redo failed', err), 'error')
      }
    })

  return {
    loaded: false,
    loadError: null,
    spec: null,
    raw: '',
    repoPath: null,
    errors: [],
    status: {},
    skeleton: null,
    labels: [],
    selectedId: null,
    undoStack: [],
    redoStack: [],
    writing: 0,
    toast: null,
    focusRequest: null,
    collapsed: {},
    expanded: {},
    markerWithSays: false,

    load: async () => {
      writeSeq++ // drop any reload still running for a previously opened repo
      const token = ++loadToken
      // The repo this answer belongs to (the backend serves the open repo).
      const repoPath = useStore.getState().repo?.path ?? null
      try {
        const file = await api.getSpec()
        if (token !== loadToken) return // a later load (another repo) superseded this one
        set({
          loaded: true,
          loadError: null,
          spec: file.spec,
          raw: file.raw ?? '',
          repoPath,
          errors: file.errors ?? [],
          undoStack: [],
          redoStack: [],
          selectedId: null,
        })
      } catch (err) {
        if (token !== loadToken) return
        set({
          loaded: true,
          loadError: api.errorMessage(err),
          spec: null,
          raw: '',
          repoPath,
          errors: [],
          undoStack: [],
          redoStack: [],
        })
      }
      void get().refreshStatus()
      void get().refreshSkeleton()
      void get().refreshLabels()
    },

    reload: async () => {
      // Our own queued write will bring the newest spec anyway.
      if (queuedWrites > 0) return false
      // Another repo was opened and its load() is on the way; a reload now
      // would put that repo's file under the old repo's name.
      if (get().repoPath !== (useStore.getState().repo?.path ?? null)) return false
      const seq = writeSeq
      try {
        const file = await api.getSpec()
        if (seq !== writeSeq || queuedWrites > 0) return false // a write started meanwhile
        if ((file.raw ?? '') === get().raw && !!file.spec === !!get().spec) return false // our own echo
        // Content we did not write: the undo/redo snapshots predate it, and
        // restoring one would silently remove it, so the history goes.
        const hadHistory = get().undoStack.length > 0 || get().redoStack.length > 0
        set({
          spec: file.spec,
          raw: file.raw ?? '',
          errors: file.errors ?? [],
          loadError: null,
          undoStack: [],
          redoStack: [],
        })
        if (hadHistory) get().showToast(`History cleared: ${EXTERNAL_CHANGE}`)
        return true
      } catch {
        return false // keep what is on screen; the next event retries
      }
    },

    applyOp: (op) =>
      enqueueWrite(async () => {
        if (!specRepoIsOpen(get().repoPath)) {
          get().showToast(`Edit not sent: ${OTHER_REPO}`, 'error')
          return null
        }
        const previous = get().spec
        try {
          const res = await api.applySpecOp(op)
          set((s) => committed(s, res, previous))
          afterWrite(!TEXT_ONLY_OPS.has(op.type))
          return res
        } catch (err) {
          get().showToast(failureText('Edit failed', err), 'error')
          return null
        }
      }),

    undo: () => restore('undoStack'),
    redo: () => restore('redoStack'),

    saveRaw: (raw, repoPath) =>
      enqueueWrite(async (): Promise<RawSaveResult> => {
        // Never send one repo's text to another: the backend writes whatever
        // repo it has open, which may no longer be the one this text is for.
        if (!specRepoIsOpen(repoPath) || get().repoPath !== repoPath) {
          return {
            ok: false,
            detail: 'this text belongs to a repo that is not open; it was kept as a draft for that repo',
            errors: [],
          }
        }
        const previous = get().spec
        try {
          const res = await api.putSpecRaw(raw)
          set((s) => committed(s, res, previous))
          afterWrite(true)
          return { ok: true }
        } catch (err) {
          return { ok: false, detail: api.errorMessage(err), errors: api.specErrorsOf(err) }
        }
      }),

    refreshStatus: async () => {
      const token = ++statusToken
      try {
        const res = await api.getSpecStatus()
        if (token === statusToken) set({ status: res.nodes ?? {} })
      } catch {
        // No repo or no spec yet: keep the dots we have.
      }
    },

    refreshSkeleton: async () => {
      const token = ++skeletonToken
      try {
        const res = await api.getSkeleton()
        if (token === skeletonToken) set({ skeleton: res })
      } catch {
        // Keep the old skeleton; the next file change retries.
      }
    },

    refreshLabels: async () => {
      const token = ++labelsToken
      try {
        const res = await api.getLabels()
        if (token === labelsToken) set({ labels: res.labels })
      } catch {
        // Keep the old labels; the next file change retries.
      }
    },

    adopt: async (request) => {
      // The backend edits the .tex on disk, so unsaved editor text in that
      // file is saved first (otherwise line numbers would not match).
      const editor = useStore.getState()
      if (editor.activeFile?.dirty && editor.activeFile.path === request.file) {
        await editor.save({ compileAfter: false })
      }
      return enqueueWrite(async () => {
        if (!specRepoIsOpen(get().repoPath)) {
          get().showToast(`Adopt not sent: ${OTHER_REPO}`, 'error')
          return null
        }
        const previous = get().spec
        try {
          const res = await api.adopt(request)
          set((s) => committed(s, res, previous))
          afterWrite(true)
          // Show the marker in the editor before anyone jumps to it.
          if (request.kind === 'paragraph') await useStore.getState().reloadFile(res.file ?? request.file)
          return res
        } catch (err) {
          get().showToast(failureText('Adopt failed', err), 'error')
          return null
        }
      })
    },

    insertMarker: async (id, file, lineStart) => {
      const { spec, markerWithSays } = get()
      const node = spec ? findNode(spec, id) : null
      if (!node || node.level !== 'paragraph') {
        get().showToast('Only paragraph nodes get a % @p marker.', 'error')
        return false
      }
      const res = await get().adopt({ kind: 'paragraph', id, file, line_start: lineStart })
      if (!res) return false
      const where = res.file ?? file
      const markerLine = res.line ?? lineStart
      const says = node.says.replace(/\s+/g, ' ').trim()
      if (markerWithSays && says) await writeSaysComment(where, markerLine, id, says)
      await useStore.getState().goToLocation(where, markerLine)
      return true
    },

    reconcile: async (id) => {
      try {
        const res = await api.reconcileNode(id)
        set((s) => {
          const before: NodeStatus = s.status[id] ?? { status: res.status, file: null, line: null }
          return { status: { ...s.status, [id]: { ...before, status: res.status } } }
        })
      } catch (err) {
        get().showToast(failureText('Reconcile failed', err), 'error')
      }
      void get().refreshStatus()
    },

    select: (id) => {
      if (get().selectedId !== id) set({ selectedId: id })
    },

    requestFocus: (id) => {
      // Open a collapsed section so the row can actually receive focus.
      const spec = get().spec
      const loc = spec ? locate(spec, id) : null
      const parentId = loc && loc.parent.id !== spec?.tree.id ? loc.parent.id : null
      set((s) => ({
        focusRequest: { id, seq: ++focusCounter },
        selectedId: id,
        collapsed: parentId && s.collapsed[parentId] ? { ...s.collapsed, [parentId]: false } : s.collapsed,
      }))
    },

    clearFocusRequest: (seq) => {
      if (get().focusRequest?.seq === seq) set({ focusRequest: null })
    },

    showToast: (text, kind = 'info', action) => {
      clearTimeout(toastTimer)
      set({ toast: { text, kind, seq: ++toastCounter, action } })
      toastTimer = setTimeout(() => set({ toast: null }), action ? ACTION_TOAST_MS : TOAST_MS)
    },

    dismissToast: () => {
      clearTimeout(toastTimer)
      set({ toast: null })
    },

    toggleCollapsed: (id) => set((s) => ({ collapsed: { ...s.collapsed, [id]: !s.collapsed[id] } })),
    toggleExpanded: (id) => set((s) => ({ expanded: { ...s.expanded, [id]: !s.expanded[id] } })),
    setMarkerWithSays: (on) => set({ markerWithSays: on }),

    handleServerEvent: (event) => {
      const reloadSoon = () => {
        clearTimeout(reloadTimer)
        reloadTimer = setTimeout(() => {
          void get()
            .reload()
            .then((changed) => {
              if (changed) afterWrite(true)
            })
        }, RELOAD_DEBOUNCE_MS)
      }
      const refreshSoon = () => {
        clearTimeout(refreshTimer)
        refreshTimer = setTimeout(() => afterWrite(true), RELOAD_DEBOUNCE_MS)
      }
      if (event.type === 'spec.changed') {
        reloadSoon()
      } else if (event.type === 'file.changed' && typeof event.path === 'string') {
        if (event.path.endsWith('paper.json')) reloadSoon()
        else if (event.path.endsWith('.tex')) refreshSoon()
      }
    },
  }
})

// ---------------------------------------------------------------------------
// "says" as a LaTeX comment next to a marker
// ---------------------------------------------------------------------------

/**
 * Write `% says: <says>` on the line below the `% @p <id>` marker at
 * file:markerLine (unless that exact line is already there), then reload the
 * editor. The backend ignores comment lines inside a paragraph, so this
 * neither breaks the anchor nor makes the paragraph stale. This is a plain
 * file write (PUT /api/file): the adopt endpoint has no option for it.
 */
async function writeSaysComment(file: string, markerLine: number, id: string, says: string): Promise<void> {
  const editor = useStore.getState()
  try {
    const open = editor.activeFile
    // The text to edit and the revision it is (sent as base_sha256, so a file
    // changed in between is refused rather than overwritten).
    const source = open && open.path === file && !open.dirty ? open : await api.getFile(file)
    const text = source.content
    const lines = text.split('\n')
    const marker = lines[markerLine - 1] ?? ''
    if (!new RegExp(`^\\s*%\\s*@p\\s+${id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*$`).test(marker)) return
    const indent = marker.slice(0, marker.length - marker.trimStart().length)
    const comment = `${indent}% says: ${says}`
    if (lines[markerLine] === comment) return
    lines.splice(markerLine, 0, comment)
    await api.putFile(file, lines.join('\n'), source.sha256 || undefined)
    await useStore.getState().reloadFile(file)
  } catch (err) {
    useSpecStore.getState().showToast(`Could not write the says comment: ${api.errorMessage(err)}`, 'error')
  }
}

// ---------------------------------------------------------------------------
// Editor cursor -> spec row
// ---------------------------------------------------------------------------

/**
 * The node whose marker (or heading) is the nearest one at or above `line`
 * in `path`, among nodes that are linked or stale. That is the paragraph (or
 * section) the editor cursor is in.
 */
export function activeNodeIdFor(status: Record<string, NodeStatus>, path: string | null, line: number): string | null {
  if (!path) return null
  let best: string | null = null
  let bestLine = -1
  for (const [id, st] of Object.entries(status)) {
    if (st.file !== path || st.line == null) continue
    if (st.status !== 'linked' && st.status !== 'stale') continue
    if (st.line <= line && st.line > bestLine) {
      best = id
      bestLine = st.line
    }
  }
  return best
}

/** React hook: the node the editor's cursor is in (highlighted in the outliner). */
export function useActiveNodeId(): string | null {
  const status = useSpecStore((s) => s.status)
  const path = useStore((s) => s.activeFile?.path ?? null)
  const line = useStore((s) => s.cursorLine)
  return useMemo(() => activeNodeIdFor(status, path, line), [status, path, line])
}
