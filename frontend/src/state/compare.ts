// Blind A/B compare state (phase 5, PLAN.md "Blind A/B compare"): a small
// zustand store behind compare/CompareDialog.tsx.
//
// The workflow: pick a paragraph and a "before" (a snapshot tag or a commit);
// the backend sends both versions, in random order and labelled only A and B,
// to a fresh model call that is never told either one is an edit. The
// verdict ("A", "B" or "tie") and its reason come back first; which letter
// was the earlier version stays hidden until the user presses "Reveal"
// (GET /api/compare/{id}?reveal=true). That keeps the user's own reading of
// the verdict blind too.
//
// What it holds
//   target       the paragraph the dialog was opened for (null = dialog closed)
//   view         "compare" (form, or the comparison on screen) or "history"
//   comparisons  GET /api/compare, newest first (never with a mapping)
//   currentId    the comparison on screen; `detail` is its GET /api/compare/{id}
//   revealed     whether the user pressed Reveal for it (then `detail` has the mapping)
//
// Live updates: a comparison carries a phase 4 run (`run_id`). Its
// `run.started` / `run.failed` events and `compare.finished` refetch the
// detail; while it is queued or running a slow poll (every 3 s) covers a
// missed event. Cancel goes through the run's own cancel endpoint.
import { create } from 'zustand'
import * as api from '../api/client'
import type { Comparison, ComparisonDetail, CompareRequest, ServerEvent } from '../api/client'

/** The paragraph a comparison is about. */
export interface CompareTarget {
  file: string
  /** Paragraph node with a `% @p` marker (the usual case). */
  node_id: string | null
  /** Alternative to node_id: a line range in `file`. */
  line_start?: number
  line_end?: number
  /** The node's `says`, shown in the dialog header. */
  says?: string
}

export type CompareView = 'compare' | 'history'

export interface CompareState {
  target: CompareTarget | null
  view: CompareView
  comparisons: Comparison[]
  historyError: string | null
  currentId: string | null
  detail: ComparisonDetail | null
  revealed: boolean
  starting: boolean
  /** Why the last start / cancel / reveal failed (shown in the dialog). */
  error: string | null

  /** Open the dialog for a paragraph (shows the form, loads the history). */
  openFor: (target: CompareTarget) => void
  close: () => void
  setView: (view: CompareView) => void
  loadHistory: () => Promise<void>
  /** POST /api/compare; resolves to the new comparison, or null (error in `error`). */
  start: (request: CompareRequest) => Promise<Comparison | null>
  cancel: () => Promise<void>
  /** Fetch the mapping (only once the comparison is done). */
  reveal: () => Promise<void>
  /** Show a past comparison (from the History list), unrevealed. */
  show: (id: string) => Promise<void>
  /** Back to the form for the current target. */
  newComparison: () => void
  handleServerEvent: (event: ServerEvent) => void
}

const POLL_MS = 3000

/** True while the model call is still going. */
export function isPending(c: Comparison | null | undefined): boolean {
  return !!c && (c.status === 'queued' || c.status === 'running')
}

/** Merge a comparison into the newest-first list. */
function upsert(list: Comparison[], c: Comparison): Comparison[] {
  const blind = { ...c, mapping: null }
  const i = list.findIndex((x) => x.id === c.id)
  if (i < 0) return [blind, ...list]
  const next = [...list]
  next[i] = blind
  return next
}

// Module-level bookkeeping (never rendered).
let detailToken = 0
let pollTimer: ReturnType<typeof setTimeout> | undefined

export const useCompareStore = create<CompareState>()((set, get) => {
  /** Re-read the comparison on screen (with the mapping once revealed). */
  const fetchDetail = async () => {
    const id = get().currentId
    if (!id) return
    const token = ++detailToken
    try {
      const detail = await api.getComparison(id, get().revealed)
      if (token !== detailToken || get().currentId !== id) return
      set((s) => ({ detail, comparisons: upsert(s.comparisons, detail.comparison) }))
    } catch (err) {
      if (token === detailToken) set({ error: `Could not load comparison ${id}: ${api.errorMessage(err)}` })
    }
    schedulePoll()
  }

  /** While the comparison on screen is pending, re-read it every few seconds. */
  const schedulePoll = () => {
    clearTimeout(pollTimer)
    if (!get().target || !isPending(get().detail?.comparison)) return
    pollTimer = setTimeout(() => void fetchDetail(), POLL_MS)
  }

  return {
    target: null,
    view: 'compare',
    comparisons: [],
    historyError: null,
    currentId: null,
    detail: null,
    revealed: false,
    starting: false,
    error: null,

    openFor: (target) => {
      detailToken++
      clearTimeout(pollTimer)
      set({ target, view: 'compare', currentId: null, detail: null, revealed: false, error: null })
      void get().loadHistory()
    },

    close: () => {
      detailToken++
      clearTimeout(pollTimer)
      set({ target: null, currentId: null, detail: null, revealed: false, error: null })
    },

    setView: (view) => {
      set({ view })
      if (view === 'history') void get().loadHistory()
    },

    loadHistory: async () => {
      try {
        set({ comparisons: await api.listComparisons(50), historyError: null })
      } catch (err) {
        set({ historyError: api.errorMessage(err) })
      }
    },

    start: async (request) => {
      if (get().starting) return null
      set({ starting: true, error: null })
      try {
        const comparison = await api.startCompare(request)
        detailToken++
        set((s) => ({
          currentId: comparison.id,
          revealed: false,
          // Texts arrive with the detail fetch just below.
          detail: { comparison, before_text: '', after_text: '', run: null },
          comparisons: upsert(s.comparisons, comparison),
        }))
        await fetchDetail()
        return comparison
      } catch (err) {
        set({ error: `Could not start the comparison: ${api.errorMessage(err)}` })
        return null
      } finally {
        set({ starting: false })
      }
    },

    cancel: async () => {
      const runId = get().detail?.comparison.run_id
      if (!runId) return
      try {
        await api.cancelRun(runId)
      } catch (err) {
        set({ error: `Cancel failed: ${api.errorMessage(err)}` })
      }
      await fetchDetail()
    },

    reveal: async () => {
      if (get().detail?.comparison.status !== 'done') return
      set({ revealed: true })
      await fetchDetail()
    },

    show: async (id) => {
      detailToken++
      clearTimeout(pollTimer)
      const known = get().comparisons.find((c) => c.id === id) ?? null
      set({
        view: 'compare',
        currentId: id,
        revealed: false,
        error: null,
        detail: known ? { comparison: known, before_text: '', after_text: '', run: null } : null,
      })
      await fetchDetail()
    },

    newComparison: () => {
      detailToken++
      clearTimeout(pollTimer)
      set({ view: 'compare', currentId: null, detail: null, revealed: false, error: null })
    },

    handleServerEvent: (event) => {
      const current = get().detail?.comparison
      switch (event.type) {
        case 'compare.finished':
          if (!event.comparison?.id) break
          set((s) => ({ comparisons: upsert(s.comparisons, event.comparison) }))
          if (event.comparison.id === get().currentId) void fetchDetail()
          break
        case 'run.started':
        case 'run.failed':
          // The comparison's run moved (queued -> running, or cancelled / failed).
          if (current?.run_id && event.run?.id === current.run_id) void fetchDetail()
          break
      }
    },
  }
})
