// Git panel state (phase 5): a fourth zustand store, beside store.ts (editor,
// files, compile), spec.ts (paper.json) and runs.ts (review runs).
//
// What it holds
//   status        GET /api/git/status: branch, upstream, ahead/behind, changed files
//   unchecked     paths the user un-ticked in the file list. Everything else is
//                 ticked, so a file that shows up later starts ticked too, and
//                 "Commit" sends exactly the ticked paths.
//   diffPath/diff GET /api/git/diff?path= for the file clicked in the list
//   commits       GET /api/git/log (newest first, with tags)
//   snapshots     GET /api/snapshots ("flag major revision" tags)
//   remote        the running or last push / pull: its streamed output lines
//                 (`git.output` events) and how it ended (`git.finished`)
//
// When things refresh
//   - status: when a repo opens, after every git action, and (debounced) on
//     every `file.changed` / `spec.changed` event, so the header's "Git" badge
//     always shows how many files changed;
//   - log and snapshots: when the panel opens and after every git action
//     (the compare dialog's "before" picker also loads them).
// The backend shells out to the host's git; this store never guesses what git
// would do, it just re-reads.
import { create } from 'zustand'
import * as api from '../api/client'
import type { Commit, GitStatus, ServerEvent, Snapshot } from '../api/client'

/** A `git push` or `git pull` started from the panel (or another tab). */
export interface RemoteOp {
  op: 'push' | 'pull'
  status: 'running' | 'done' | 'failed'
  /** Output lines, oldest first (capped at REMOTE_LOG_MAX_LINES). */
  lines: string[]
  error: string | null
}

/** A short result line under the commit box / snapshot form. */
export interface GitMessage {
  kind: 'ok' | 'error'
  text: string
}

export interface GitState {
  /** The drawer is open (App.tsx renders it over the right side of the page). */
  open: boolean
  status: GitStatus | null
  /** Set when GET /api/git/status failed (no repo, backend without git support). */
  statusError: string | null
  loadingStatus: boolean
  unchecked: Record<string, boolean>
  diffPath: string | null
  diff: string | null
  diffError: string | null
  diffLoading: boolean
  commits: Commit[]
  snapshots: Snapshot[]
  /** Commit message being typed (kept while the drawer is closed). */
  message: string
  committing: boolean
  commitResult: GitMessage | null
  remote: RemoteOp | null
  snapshotting: boolean
  snapshotResult: GitMessage | null

  setOpen: (open: boolean) => void
  /** Forget everything (a new repo was opened) and load its status. */
  reset: () => void
  loadStatus: () => Promise<void>
  /** Log and snapshots (for the panel and the compare dialog's "before" picker). */
  loadHistory: () => Promise<void>
  /** Status + log + snapshots. */
  refreshAll: () => Promise<void>
  showDiff: (path: string | null) => Promise<void>
  toggleChecked: (path: string) => void
  /** Tick (true) or un-tick (false) every file. */
  setAllChecked: (checked: boolean) => void
  setMessage: (message: string) => void
  /** Commit the ticked files with `message`; resolves true on success. */
  commit: () => Promise<boolean>
  startRemote: (op: 'push' | 'pull') => Promise<void>
  /** "Flag major revision": POST /api/snapshots; resolves to the snapshot, or null on failure. */
  flagRevision: (note: string) => Promise<Snapshot | null>
  handleServerEvent: (event: ServerEvent) => void
}

/** Older push/pull output lines are dropped beyond this many. */
export const REMOTE_LOG_MAX_LINES = 500
const STATUS_DEBOUNCE_MS = 300

/** Paths that will be committed: every changed file that is not un-ticked. */
export function checkedPaths(s: Pick<GitState, 'status' | 'unchecked'>): string[] {
  return (s.status?.files ?? []).filter((f) => !s.unchecked[f.path]).map((f) => f.path)
}

// Module-level bookkeeping (never rendered).
let statusToken = 0 // last loadStatus() wins
let historyToken = 0
let diffToken = 0
let statusTimer: ReturnType<typeof setTimeout> | undefined

export const useGitStore = create<GitState>()((set, get) => ({
  open: false,
  status: null,
  statusError: null,
  loadingStatus: false,
  unchecked: {},
  diffPath: null,
  diff: null,
  diffError: null,
  diffLoading: false,
  commits: [],
  snapshots: [],
  message: '',
  committing: false,
  commitResult: null,
  remote: null,
  snapshotting: false,
  snapshotResult: null,

  setOpen: (open) => {
    set({ open })
    if (open) void get().refreshAll()
  },

  reset: () => {
    statusToken++
    historyToken++
    diffToken++
    set({
      status: null,
      statusError: null,
      unchecked: {},
      diffPath: null,
      diff: null,
      diffError: null,
      commits: [],
      snapshots: [],
      commitResult: null,
      remote: null,
      snapshotResult: null,
    })
    void get().loadStatus()
    if (get().open) void get().loadHistory()
  },

  loadStatus: async () => {
    const token = ++statusToken
    set({ loadingStatus: true })
    try {
      const status = await api.getGitStatus()
      if (token !== statusToken) return
      // Forget un-ticks of files that are no longer changed.
      const present = new Set(status.files.map((f) => f.path))
      const unchecked = Object.fromEntries(Object.entries(get().unchecked).filter(([p, v]) => v && present.has(p)))
      set({ status, statusError: null, unchecked, loadingStatus: false })
      // The diff on screen may be outdated (the file changed or was committed).
      const shown = get().diffPath
      if (shown) {
        if (present.has(shown)) void get().showDiff(shown)
        else set({ diffPath: null, diff: null, diffError: null })
      }
    } catch (err) {
      if (token === statusToken) set({ statusError: api.errorMessage(err), loadingStatus: false })
    }
  },

  loadHistory: async () => {
    const token = ++historyToken
    const [commits, snapshots] = await Promise.all([
      api.getGitLog(30).catch(() => null),
      api.getSnapshots().catch(() => null),
    ])
    if (token !== historyToken) return
    set((s) => ({
      commits: commits ?? s.commits,
      // Newest first, whatever order the backend used.
      snapshots: snapshots ? [...snapshots].sort((a, b) => b.created_at - a.created_at) : s.snapshots,
    }))
  },

  refreshAll: async () => {
    await Promise.all([get().loadStatus(), get().loadHistory()])
  },

  showDiff: async (path) => {
    const token = ++diffToken
    if (!path) {
      set({ diffPath: null, diff: null, diffError: null, diffLoading: false })
      return
    }
    // Keep the old text on screen while the same file reloads (no flicker).
    set((s) => ({ diffPath: path, diffLoading: true, diff: s.diffPath === path ? s.diff : null, diffError: null }))
    try {
      const diff = await api.getGitDiff(path)
      if (token === diffToken) set({ diff, diffLoading: false })
    } catch (err) {
      if (token === diffToken) set({ diff: null, diffError: api.errorMessage(err), diffLoading: false })
    }
  },

  toggleChecked: (path) => set((s) => ({ unchecked: { ...s.unchecked, [path]: !s.unchecked[path] } })),

  setAllChecked: (checked) =>
    set((s) => ({
      unchecked: checked ? {} : Object.fromEntries((s.status?.files ?? []).map((f) => [f.path, true])),
    })),

  setMessage: (message) => set({ message }),

  commit: async () => {
    const message = get().message.trim()
    const paths = checkedPaths(get())
    if (!message || paths.length === 0 || get().committing) return false
    set({ committing: true, commitResult: null })
    try {
      const res = await api.gitCommit(message, paths)
      set({
        message: '',
        commitResult: { kind: 'ok', text: `Committed ${res.sha.slice(0, 7)} “${res.subject}” (${paths.length} file${paths.length === 1 ? '' : 's'}).` },
      })
      return true
    } catch (err) {
      set({ commitResult: { kind: 'error', text: `Commit failed: ${api.errorMessage(err)}` } })
      return false
    } finally {
      set({ committing: false })
      void get().refreshAll()
    }
  },

  startRemote: async (op) => {
    if (get().remote?.status === 'running') return
    set({ remote: { op, status: 'running', lines: [], error: null } })
    try {
      const res = await api[op === 'push' ? 'gitPush' : 'gitPull']()
      if (!res.started) set({ remote: { op, status: 'failed', lines: get().remote?.lines ?? [], error: `The backend did not start git ${op}.` } })
    } catch (err) {
      set({ remote: { op, status: 'failed', lines: get().remote?.lines ?? [], error: api.errorMessage(err) } })
    }
  },

  flagRevision: async (note) => {
    const text = note.trim()
    if (!text || get().snapshotting) return null
    set({ snapshotting: true, snapshotResult: null })
    try {
      const snapshot = await api.createSnapshot(text)
      set({ snapshotResult: { kind: 'ok', text: `Tagged ${snapshot.tag} at ${snapshot.sha.slice(0, 7)}.` } })
      return snapshot
    } catch (err) {
      set({ snapshotResult: { kind: 'error', text: `Could not flag the revision: ${api.errorMessage(err)}` } })
      return null
    } finally {
      set({ snapshotting: false })
      void get().refreshAll()
    }
  },

  handleServerEvent: (event) => {
    switch (event.type) {
      case 'git.output': {
        if (typeof event.line !== 'string') break
        const remote = get().remote
        // A push/pull started from another tab also shows up here.
        const same = remote && remote.status === 'running' && remote.op === event.op
        const lines = same ? [...remote.lines, event.line] : [event.line]
        set({ remote: { op: event.op, status: 'running', lines: lines.slice(-REMOTE_LOG_MAX_LINES), error: null } })
        break
      }
      case 'git.finished': {
        const remote = get().remote
        set({
          remote: {
            op: event.op,
            lines: remote?.op === event.op ? remote.lines : [],
            status: event.ok ? 'done' : 'failed',
            error: event.ok ? null : (event.error ?? `git ${event.op} failed`),
          },
        })
        void get().refreshAll() // ahead/behind changed; a pull may have changed files
        break
      }
      case 'file.changed':
      case 'spec.changed':
        // Only once a repo's status was asked for (reset() runs when a repo opens).
        if (!get().status && !get().statusError) break
        // Coalesce bursts (a save emits several events) into one status read.
        clearTimeout(statusTimer)
        statusTimer = setTimeout(() => void get().loadStatus(), STATUS_DEBOUNCE_MS)
        break
    }
  },
}))
