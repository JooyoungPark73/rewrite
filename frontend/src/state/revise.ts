// Revise (phase 8): author comments on the text, and rewrite proposals on findings.
//
// Two dialogs share this store. CommentBox: the editor's selection menu → "Comment…" →
// a comment typed by the author becomes a finding (POST /api/findings/comment) with the
// usual card, highlight and states. RevisionDialog: "Rewrite…" on any finding card → a
// model proposes a replacement for the quoted span or the whole paragraph; the dialog
// shows a word diff and the editable text; "Approve" writes it as \aigen{...} with the
// original kept on a "% aigen-was:" line, marks the finding addressed, and the paragraph
// turns stale for re-reading. Co-authors accept by removing the wrapper (in Overleaf or
// here); the header counts the wrappers still in the paper.
import { create } from 'zustand'
import * as api from '../api/client'
import type { Finding, Outstanding, Revision, RevisionDetail, RevisionSpan, ServerEvent } from '../api/client'
import { useRunsStore } from './runs'
import { useSpecStore } from './spec'
import { useStore } from './store'

export interface CommentTarget {
  file: string
  lineStart: number
  lineEnd: number
  quote: string
  nodeId: string | null
}

export interface ReviseState {
  /** The comment box (null = closed). */
  commentTarget: CommentTarget | null
  commentBusy: boolean
  commentError: string | null
  /** The rewrite dialog's finding (null = closed). */
  finding: Finding | null
  detail: RevisionDetail | null
  text: string
  history: Revision[]
  starting: boolean
  applying: boolean
  error: string | null
  outstanding: Outstanding

  openComment: (target: CommentTarget) => void
  closeComment: () => void
  submitComment: (problem: string, why: string) => Promise<Finding | null>

  openFor: (finding: Finding) => void
  close: () => void
  start: (request: { span: RevisionSpan; runner: string | null; model: string | null; thinking: string | null }) => Promise<Revision | null>
  setText: (text: string) => void
  show: (id: string) => Promise<void>
  approve: () => Promise<boolean>
  loadHistory: () => Promise<void>
  refreshOutstanding: () => Promise<void>
  handleServerEvent: (event: ServerEvent) => void
}

const POLL_MS = 3000

export function isPending(r: Revision | null | undefined): boolean {
  return !!r && (r.status === 'queued' || r.status === 'running')
}

let detailToken = 0
let pollTimer: ReturnType<typeof setTimeout> | undefined
let outstandingTimer: ReturnType<typeof setTimeout> | undefined

export const useReviseStore = create<ReviseState>()((set, get) => {
  const fetchDetail = async (id: string) => {
    const token = ++detailToken
    try {
      const detail = await api.getRevision(id)
      if (token !== detailToken || get().detail?.revision.id !== id) return
      const previous = get().detail
      set({
        detail,
        text: previous?.revision.text == null && detail.revision.text != null ? detail.revision.text : get().text,
        history: upsert(get().history, detail.revision),
      })
    } catch (err) {
      if (token === detailToken) set({ error: `Could not load the rewrite: ${api.errorMessage(err)}` })
    }
    schedulePoll()
  }
  const schedulePoll = () => {
    clearTimeout(pollTimer)
    const r = get().detail?.revision
    if (!get().finding || !isPending(r)) return
    pollTimer = setTimeout(() => r && void fetchDetail(r.id), POLL_MS)
  }

  return {
    commentTarget: null,
    commentBusy: false,
    commentError: null,
    finding: null,
    detail: null,
    text: '',
    history: [],
    starting: false,
    applying: false,
    error: null,
    outstanding: { count: 0, by_file: {} },

    openComment: (target) => set({ commentTarget: target, commentError: null }),
    closeComment: () => set({ commentTarget: null, commentError: null }),
    submitComment: async (problem, why) => {
      const target = get().commentTarget
      if (!target) return null
      if (!problem.trim()) {
        set({ commentError: 'Write the comment first.' })
        return null
      }
      set({ commentBusy: true, commentError: null })
      try {
        const finding = await api.addComment({
          file: target.file,
          line_start: target.lineStart,
          line_end: target.lineEnd,
          quote: target.quote,
          problem: problem.trim(),
          why: why.trim(),
          json_ref: target.nodeId,
        })
        set({ commentTarget: null })
        // Show the findings column so the new card is in view (it may have been closed).
        useRunsStore.getState().setSidebarOpen(true)
        void useRunsStore.getState().loadFindings()
        void useRunsStore.getState().loadRuns()
        useSpecStore.getState().showToast(`Comment added on ${target.file}:${target.lineStart}; "Rewrite…" on its card proposes a change.`, 'info')
        return finding
      } catch (err) {
        set({ commentError: api.errorMessage(err) })
        return null
      } finally {
        set({ commentBusy: false })
      }
    },

    openFor: (finding) => {
      detailToken++
      clearTimeout(pollTimer)
      set({ finding, detail: null, text: '', history: [], error: null, starting: false, applying: false })
      void get().loadHistory()
    },
    close: () => {
      detailToken++
      clearTimeout(pollTimer)
      set({ finding: null, detail: null, text: '', error: null })
    },
    loadHistory: async () => {
      const finding = get().finding
      if (!finding) return
      try {
        const history = await api.listRevisions(finding.id)
        if (get().finding?.id === finding.id) set({ history })
      } catch {
        // informational
      }
    },
    start: async (request) => {
      const finding = get().finding
      if (!finding) return null
      set({ starting: true, error: null })
      try {
        const revision = await api.startRevision({ finding_id: finding.id, ...request })
        set((s) => ({ detail: { revision, run: null, finding }, text: '', history: upsert(s.history, revision) }))
        void fetchDetail(revision.id)
        return revision
      } catch (err) {
        set({ error: api.errorMessage(err) })
        return null
      } finally {
        set({ starting: false })
      }
    },
    setText: (text) => set({ text }),
    show: async (id) => {
      const known = get().history.find((r) => r.id === id)
      if (known) set({ detail: { revision: known, run: null, finding: get().finding }, text: known.text ?? '', error: null })
      try {
        const detail = await api.getRevision(id)
        if (get().detail?.revision.id === id) set({ detail, text: detail.revision.text ?? '' })
      } catch (err) {
        set({ error: api.errorMessage(err) })
      }
    },
    approve: async () => {
      const finding = get().finding
      const revision = get().detail?.revision
      if (!finding || !revision) return false
      set({ applying: true, error: null })
      try {
        // The backend edits the .tex (and main.tex) on disk: unsaved editor text is saved
        // first, so the span is located in what the author sees and the editor's next save
        // does not conflict with (or overwrite) the rewrite.
        const saved = await useStore.getState().save({ compileAfter: false })
        if (!saved.ok) {
          set({ error: `Not approved: the open file could not be saved first (${saved.error}).` })
          return false
        }
        const applied = await api.applyRevision(revision.id, get().text)
        set((s) => ({ history: upsert(s.history, applied.revision), outstanding: { ...s.outstanding, count: applied.outstanding } }))
        void get().refreshOutstanding()
        void useRunsStore.getState().loadFindings()
        void useSpecStore.getState().refreshStatus()
        await useStore.getState().goToLocation(applied.file, applied.line_start)
        useSpecStore
          .getState()
          .showToast(
            `Wrote the rewrite as \\aigen{…} at ${applied.file}:${applied.line_start}; the original is on the "% aigen-was:" line above. ${applied.outstanding} AI edit${applied.outstanding === 1 ? '' : 's'} awaiting co-authors.`,
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
    refreshOutstanding: async () => {
      try {
        set({ outstanding: await api.getOutstanding() })
      } catch {
        // header counter only
      }
    },
    handleServerEvent: (event) => {
      const current = get().detail?.revision
      switch (event.type) {
        case 'revision.finished':
          if (!event.revision?.id) break
          set((s) => ({ history: upsert(s.history, event.revision) }))
          if (event.revision.id === current?.id) void fetchDetail(event.revision.id)
          break
        case 'run.started':
        case 'run.failed':
          if (current?.run_id && event.run?.id === current.run_id) void fetchDetail(current.id)
          break
        case 'file.changed':
          // A pull or an edit may have removed wrappers: recount, debounced.
          clearTimeout(outstandingTimer)
          outstandingTimer = setTimeout(() => void get().refreshOutstanding(), 500)
          break
      }
    },
  }
})

function upsert(list: Revision[], r: Revision): Revision[] {
  const i = list.findIndex((x) => x.id === r.id)
  if (i < 0) return [r, ...list]
  const next = [...list]
  next[i] = r
  return next
}
