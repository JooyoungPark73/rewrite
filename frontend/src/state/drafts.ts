// Drafts from the spec (phase 7): the dialog's state.
//
// A draft realizes one paragraph node (its says, function and linked claims) as LaTeX,
// through POST /api/drafts. The model call writes nothing: the answer lands here, the
// author edits it in the dialog and chooses "Replace paragraph" (the node has a marker in
// the source) or "Insert at cursor" (a planned node: the marker line is written with the
// text and the node is then re-linked through the spec API's adopt). Separate from the
// review runs on purpose (PLAN.md, 2026-10-20); the run itself is an ordinary run, so the
// runs store streams its live output and can cancel it.
import { create } from 'zustand'
import * as api from '../api/client'
import type { Draft, DraftDetail, DraftRequest, ServerEvent } from '../api/client'
import { useSpecStore } from './spec'
import { useStore } from './store'

export interface DraftTarget {
  nodeId: string
  says: string
  /** The node has a marker in the source, so "Replace paragraph" applies. */
  anchored: boolean
  file: string | null
}

export interface DraftState {
  target: DraftTarget | null
  /** The draft on screen (the newest started or chosen from the history). */
  detail: DraftDetail | null
  /** The author's working copy of the draft text (starts as the model's). */
  text: string
  history: Draft[]
  starting: boolean
  applying: boolean
  error: string | null

  openFor: (target: DraftTarget) => void
  close: () => void
  start: (request: Omit<DraftRequest, 'node_id'>) => Promise<Draft | null>
  setText: (text: string) => void
  show: (id: string) => Promise<void>
  /** Write the draft: replace under the marker, or insert at file:line (then re-link). */
  apply: (mode: 'replace' | 'insert', place?: { file: string; line: number }) => Promise<boolean>
  loadHistory: () => Promise<void>
  handleServerEvent: (event: ServerEvent) => void
}

const POLL_MS = 3000

export function isPending(d: Draft | null | undefined): boolean {
  return !!d && (d.status === 'queued' || d.status === 'running')
}

let detailToken = 0
let pollTimer: ReturnType<typeof setTimeout> | undefined

export const useDraftStore = create<DraftState>()((set, get) => {
  const fetchDetail = async (id: string) => {
    const token = ++detailToken
    try {
      const detail = await api.getDraft(id)
      if (token !== detailToken || get().detail?.draft.id !== id) return
      const previous = get().detail
      set({
        detail,
        // Fill the working copy when the text arrives (the author has not typed yet).
        text: previous?.draft.text == null && detail.draft.text != null ? detail.draft.text : get().text,
        history: upsert(get().history, detail.draft),
      })
    } catch (err) {
      if (token === detailToken) set({ error: `Could not load the draft: ${api.errorMessage(err)}` })
    }
    schedulePoll()
  }
  const schedulePoll = () => {
    clearTimeout(pollTimer)
    const d = get().detail?.draft
    if (!get().target || !isPending(d)) return
    pollTimer = setTimeout(() => d && void fetchDetail(d.id), POLL_MS)
  }

  return {
    target: null,
    detail: null,
    text: '',
    history: [],
    starting: false,
    applying: false,
    error: null,

    openFor: (target) => {
      detailToken++
      clearTimeout(pollTimer)
      set({ target, detail: null, text: '', history: [], error: null, starting: false, applying: false })
      void get().loadHistory()
    },
    close: () => {
      detailToken++
      clearTimeout(pollTimer)
      set({ target: null, detail: null, text: '', error: null })
    },

    loadHistory: async () => {
      const target = get().target
      if (!target) return
      try {
        const history = await api.listDrafts(target.nodeId)
        if (get().target?.nodeId === target.nodeId) set({ history })
      } catch {
        // The history is a convenience; the form works without it.
      }
    },

    start: async (request) => {
      const target = get().target
      if (!target) return null
      set({ starting: true, error: null })
      try {
        const draft = await api.startDraft({ node_id: target.nodeId, ...request })
        set((s) => ({ detail: { draft, run: null }, text: '', history: upsert(s.history, draft) }))
        void fetchDetail(draft.id)
        return draft
      } catch (err) {
        set({ error: api.errorMessage(err) })
        return null
      } finally {
        set({ starting: false })
      }
    },

    setText: (text) => set({ text }),

    show: async (id) => {
      set({ detail: { draft: { ...(get().history.find((d) => d.id === id) ?? ({ id } as Draft)) }, run: null }, text: '', error: null })
      const detail = await api.getDraft(id).catch((err: unknown) => {
        set({ error: api.errorMessage(err) })
        return null
      })
      if (detail && get().detail?.draft.id === id) set({ detail, text: detail.draft.text ?? '' })
    },

    apply: async (mode, place) => {
      const target = get().target
      const draft = get().detail?.draft
      if (!target || !draft) return false
      set({ applying: true, error: null })
      try {
        const applied = await api.applyDraft(draft.id, {
          mode,
          text: get().text,
          ...(mode === 'insert' && place ? { file: place.file, line: place.line } : {}),
        })
        if (mode === 'insert' && !target.anchored) {
          // The marker line was written with the text; link the node to it (idempotent).
          await useSpecStore.getState().adopt({ kind: 'paragraph', id: target.nodeId, file: applied.file, line_start: applied.line_start })
        } else {
          void useSpecStore.getState().refreshStatus()
        }
        set((s) => ({ detail: s.detail ? { ...s.detail, draft: applied.draft } : s.detail, history: upsert(s.history, applied.draft) }))
        await useStore.getState().goToLocation(applied.file, applied.line_start)
        useSpecStore
          .getState()
          .showToast(
            mode === 'replace'
              ? `Replaced the paragraph of ${target.nodeId} with the draft; it is marked stale until you re-read it.`
              : `Inserted the draft of ${target.nodeId} at ${applied.file}:${applied.line_start}.`,
            'info',
          )
        get().close()
        return true
      } catch (err) {
        set({ error: api.errorMessage(err) })
        return false
      } finally {
        set({ applying: false })
      }
    },

    handleServerEvent: (event) => {
      const current = get().detail?.draft
      switch (event.type) {
        case 'draft.finished':
          if (!event.draft?.id) break
          set((s) => ({ history: upsert(s.history, event.draft) }))
          if (event.draft.id === current?.id) void fetchDetail(event.draft.id)
          break
        case 'run.started':
        case 'run.failed':
          if (current?.run_id && event.run?.id === current.run_id) void fetchDetail(current.id)
          break
      }
    },
  }
})

function upsert(list: Draft[], d: Draft): Draft[] {
  const i = list.findIndex((x) => x.id === d.id)
  if (i < 0) return [d, ...list]
  const next = [...list]
  next[i] = d
  return next
}
