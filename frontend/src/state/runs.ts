// Review runs and findings (phase 4): a third zustand store, beside store.ts
// (editor, files, compile) and spec.ts (paper.json).
//
// What it holds
//   passes, runners   GET /api/passes and GET /api/runners (for the launcher)
//   runs              GET /api/runs, newest first, kept current by WebSocket events
//   selectedRunId     the run shown in the Findings tab; `detail` is its
//                     GET /api/runs/{id} (findings of every file, rejected, raw output)
//   fileFindings      GET /api/findings?file=<file open in the editor>, every state
//   live              streamed `run.event` text per run (capped), for "Live output"
//   hiddenLevels, stateFilter   the filter chips (shared by the sidebar and the tab)
//   selectedFindingId the finding highlighted in the editor and marked in the lists
//                     (its json_ref node is also selected in the Spec view)
//   reveal            a request for the Editor to scroll to a finding (consumed there)
//
// Which findings are "visible"
//   Editor tab:   fileFindings, minus findings of runs superseded by a newer run of
//                 the same pass on the same scope (so a re-run does not list every
//                 problem twice), filtered by state and level.
//   Findings tab: the selected run's findings, filtered by state and level.
//   Both lists are ordered by file (include order) then anchor line. `j` / `k`
//   (nextFinding / prevFinding) step through whichever list is on screen.
//
// Flow of a run
//   startRun(req) -> POST /api/runs -> { run } (queued / running / cached) is added to
//   `runs` and selected. The backend then broadcasts run.started, run.event (text
//   deltas), and run.finished or run.failed; findings.updated follows whenever
//   findings change (a run finished, a state was set, a file edit re-anchored them).
//   Each event updates `runs`, `live`, and refetches the detail / file findings.
//
// Phase 6: run groups and triage
//   groups            GET /api/groups, newest first, kept current by `group.updated`
//   selectedGroupId   the group shown in the Findings tab INSTEAD of a run (selecting
//                     one clears the other); `groupDetail` is its GET /api/groups/{id}:
//                     the member runs and the merged findings
//   triages           the latest triage per run id (GET /api/runs/{id}/triage)
//   hideFlagged       "hide wrong/trivial" in the Findings tab
//   - Member runs are ordinary runs: they are in `runs`, stream as usual, and a
//     merged entry's `findings` are ordinary findings (click, highlight, state).
//   - The editor column's "newest run per pass + scope" rule treats a group as
//     one unit: both members of "Run both" stay listed together, and a newer run
//     or group on the same pass + scope replaces them (and the other way round).
//   - In the Findings tab with a group selected, `j` / `k` step through the
//     merged entries (selecting each entry's first finding).
//   - startBoth / startFanout -> POST /api/groups/both|fanout -> the group is
//     selected; `group.updated` and the member runs' events refetch its detail.
//   - startTriage -> POST /api/runs/{id}/triage; `triage.finished` reloads the
//     run's verdicts. dismissFlagged -> POST /api/findings/dismiss-flagged.
import { useMemo } from 'react'
import { create } from 'zustand'
import * as api from '../api/client'
import type {
  BothRequest,
  FanoutRequest,
  Finding,
  FindingState,
  GroupDetail,
  MergedFinding,
  Pass,
  PasteRequest,
  PromptPreview,
  Run,
  RunDetail,
  RunGroup,
  RunnersInfo,
  RunRequest,
  Scope,
  ServerEvent,
  Triage,
  TriageRequest,
} from '../api/client'
import { entryOfFinding, flaggedCounts, groupOfRun, isFlagged, isGroupActive, orderedMerged } from './groups'
import { useSpecStore } from './spec'
import { useStore } from './store'

/** Which states the lists show: only open findings, or every state. */
export type StateFilter = 'open' | 'all'

/** One piece of a run's live output: model text, or a progress/status line. */
export interface LiveChunk {
  kind: 'text' | 'status'
  text: string
}

/** Ask the Editor to scroll to a finding's lines (and optionally put the cursor there). */
export interface FindingReveal {
  file: string
  line_start: number
  line_end: number
  /** Move the cursor to the first line too (explicit clicks and j/k; not auto-advance). */
  moveCursor: boolean
  /** Unique per request, so revealing the same finding twice still scrolls. */
  seq: number
}

/** A short message at the bottom left (e.g. "already running"). */
export interface RunsToast {
  text: string
  kind: 'info' | 'error'
  seq: number
}

export interface RunsState {
  passes: Pass[]
  /** Set when GET /api/passes failed. */
  passesError: string | null
  runners: RunnersInfo | null
  runs: Run[]
  selectedRunId: string | null
  /** GET /api/runs/{selectedRunId}; null while loading or when nothing is selected. */
  detail: RunDetail | null
  /** Every finding (any state) in `fileFindingsPath`, from GET /api/findings?file=. */
  fileFindings: Finding[]
  fileFindingsPath: string | null
  /** Live output per run id (only runs that streamed something while this page was open). */
  live: Record<string, LiveChunk[]>
  /** Levels switched off with the filter chips. */
  hiddenLevels: Record<string, boolean>
  stateFilter: StateFilter
  selectedFindingId: string | null
  reveal: FindingReveal | null
  /** The findings column in the Editor tab (wide screens). Remembered in localStorage. */
  sidebarOpen: boolean
  toast: RunsToast | null
  /** Phase 6: run groups ("Run both", "Fan out"), newest first. */
  groups: RunGroup[]
  /** The group shown in the Findings tab instead of a run (null when a run or nothing is shown). */
  selectedGroupId: string | null
  /** GET /api/groups/{selectedGroupId}: member runs and merged findings; null while loading. */
  groupDetail: GroupDetail | null
  /** Latest triage per run id: null = never triaged; a missing key = not loaded yet. */
  triages: Record<string, Triage | null>
  /** "hide wrong/trivial": hide findings that triage flagged, in the Findings tab. */
  hideFlagged: boolean

  /** Load everything for a newly opened repo (forgets the previous repo's state). */
  loadAll: () => Promise<void>
  loadPasses: () => Promise<void>
  loadRunners: () => Promise<void>
  loadRuns: () => Promise<void>
  /** Start a run; resolves to the new run, or null when refused (a toast says why). */
  startRun: (request: RunRequest) => Promise<Run | null>
  cancelRun: (id: string) => Promise<void>
  /** Show a run in the Findings tab (fetches its detail). */
  selectRun: (id: string | null) => Promise<void>
  /** Re-read the findings of the file open in the editor. */
  loadFindings: () => Promise<void>
  setFindingState: (id: string, state: FindingState) => Promise<void>
  /** The prompt a run would send ("Show prompt"); throws ApiError on failure. */
  preview: (request: RunRequest) => Promise<PromptPreview>
  /** Turn pasted findings JSON into a run; resolves to it, or null on failure (toast). */
  paste: (request: PasteRequest) => Promise<Run | null>
  /**
   * Mark a finding as selected. With `reveal`, also open its file in the editor
   * (if needed) and scroll to its lines.
   */
  selectFinding: (id: string | null, options?: { reveal?: boolean; moveCursor?: boolean }) => void
  /** Click on a finding card: switch to the Editor tab, open the file, highlight the lines. */
  openFinding: (finding: Finding) => void
  /** `j`: the next finding of the list on screen. */
  nextFinding: () => void
  /** `k`: the previous finding of the list on screen. */
  prevFinding: () => void
  clearReveal: (seq: number) => void
  toggleLevel: (level: string) => void
  setStateFilter: (filter: StateFilter) => void
  setSidebarOpen: (open: boolean) => void
  showToast: (text: string, kind?: RunsToast['kind']) => void
  dismissToast: () => void
  handleServerEvent: (event: ServerEvent) => void

  // ---- Phase 6 ----
  loadGroups: () => Promise<void>
  /** "Run both": resolves to the new group (and selects it), or null when refused (toast). */
  startBoth: (request: BothRequest) => Promise<RunGroup | null>
  /** "Fan out": resolves to the new group (and selects it), or null when refused (toast). */
  startFanout: (request: FanoutRequest) => Promise<RunGroup | null>
  cancelGroup: (id: string) => Promise<void>
  /** Show a group's merged view in the Findings tab (clears the selected run). */
  selectGroup: (id: string | null) => Promise<void>
  /** Start a triage call over a run's open findings; null when refused (toast). */
  startTriage: (runId: string, request: TriageRequest) => Promise<Triage | null>
  /** Re-read a run's latest triage and the verdicts on its findings. */
  loadTriage: (runId: string) => Promise<void>
  /** Dismiss the run's open findings that triage marked wrong or trivial (toast with the counts). */
  dismissFlagged: (runId: string) => Promise<void>
  setHideFlagged: (on: boolean) => void
}

/** Characters of live output kept per run; older text is dropped. */
export const LIVE_MAX_CHARS = 20_000
const RELOAD_DEBOUNCE_MS = 150
const TOAST_MS = 5000
const SIDEBAR_KEY = 'rewrite.findingsSidebar'

// ---------------------------------------------------------------------------
// Pure helpers (exported for the components)
// ---------------------------------------------------------------------------

/** Statuses that mean "still going" (show a spinner and a Cancel button). */
export function isActive(run: Run): boolean {
  return run.status === 'queued' || run.status === 'running'
}

/** Statuses whose findings count (a failed or cancelled run has none). */
function hasResults(run: Run): boolean {
  return run.status === 'done' || run.status === 'cached'
}

/** Same pass on the same scope: a newer run of the group replaces an older one in the sidebar. */
function groupKey(pass: string, scope: Scope | null | undefined): string {
  const s = scope ?? ({} as Partial<Scope>)
  return [pass, s.kind ?? '', s.ref ?? '', s.file ?? '', s.line_start ?? '', s.line_end ?? ''].join('|')
}

/**
 * Ids of runs whose findings are hidden from the sidebar because a newer run
 * with results exists for the same pass and scope. `runs` is newest first.
 * Phase 6: the members of one run group count as one unit, so the claude and
 * codex runs of "Run both" (same pass, same scope) do not hide each other.
 */
function supersededRunIds(runs: Run[], groups: RunGroup[] = []): Set<string> {
  const owner = groupOfRun(groups)
  /** pass + scope -> the run or group that has it (the newest one seen). */
  const claimedBy = new Map<string, string>()
  const superseded = new Set<string>()
  for (const run of runs) {
    if (!hasResults(run)) continue
    const key = groupKey(run.pass, run.scope)
    const claimant = owner.get(run.id) ?? run.id
    const holder = claimedBy.get(key)
    if (holder === undefined) claimedBy.set(key, claimant)
    else if (holder !== claimant) superseded.add(run.id)
  }
  return superseded
}

const SEVERITY_RANK: Record<string, number> = { critical: 0, major: 1, minor: 2 }

/** Order: file (include order, then name), anchor line, severity (critical first), id. */
export function sortFindings(findings: Finding[], fileOrder: string[]): Finding[] {
  const index = new Map(fileOrder.map((path, i) => [path, i]))
  const fileRank = (path: string) => index.get(path) ?? Number.MAX_SAFE_INTEGER
  return [...findings].sort(
    (a, b) =>
      fileRank(a.file) - fileRank(b.file) ||
      a.file.localeCompare(b.file) ||
      a.anchor_line_start - b.anchor_line_start ||
      (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9) ||
      a.id.localeCompare(b.id),
  )
}

/** Apply the state and level filters (and, in the Findings tab, "hide wrong/trivial"). */
function filterFindings(
  findings: Finding[],
  stateFilter: StateFilter,
  hiddenLevels: Record<string, boolean>,
  hideFlagged = false,
): Finding[] {
  return findings.filter(
    (f) => (stateFilter === 'all' || f.state === 'open') && !hiddenLevels[f.level] && !(hideFlagged && isFlagged(f)),
  )
}

/** Paths in include order, from the editor store's file list. */
function fileOrder(): string[] {
  return useStore.getState().files.map((f) => f.path)
}

type ListInputs = Pick<RunsState, 'fileFindings' | 'runs' | 'groups' | 'stateFilter' | 'hiddenLevels'>

/** The sidebar list: findings in the open file `path`, filtered and ordered. */
export function visibleFileFindings(s: ListInputs, order: string[], path: string | null): Finding[] {
  const superseded = supersededRunIds(s.runs, s.groups)
  const current = s.fileFindings.filter((f) => f.file === path && !superseded.has(f.run_id))
  return sortFindings(filterFindings(current, s.stateFilter, s.hiddenLevels), order)
}

/** The Findings tab list: the selected run's findings (every file), filtered and ordered. */
export function visibleRunFindings(
  s: Pick<RunsState, 'detail' | 'stateFilter' | 'hiddenLevels' | 'hideFlagged'>,
  order: string[],
): Finding[] {
  return sortFindings(filterFindings(s.detail?.findings ?? [], s.stateFilter, s.hiddenLevels, s.hideFlagged), order)
}

/** Phase 6: the selected group's merged entries, filtered, in on-screen order. */
export function visibleMergedEntries(
  s: Pick<RunsState, 'groupDetail' | 'selectedGroupId' | 'stateFilter' | 'hiddenLevels' | 'hideFlagged'>,
): MergedFinding[] {
  if (!s.groupDetail || s.groupDetail.group.id !== s.selectedGroupId) return []
  return orderedMerged(s.groupDetail, s)
}

/**
 * The list `j` / `k` move through: the one on screen. With a group selected
 * in the Findings tab, that is each merged entry's first finding.
 */
function visibleList(s: RunsState): Finding[] {
  const { activeTab, activeFile } = useStore.getState()
  if (activeTab === 'findings' && s.selectedGroupId) return visibleMergedEntries(s).map((e) => e.findings[0]).filter(Boolean)
  return activeTab === 'findings'
    ? visibleRunFindings(s, fileOrder())
    : visibleFileFindings(s, fileOrder(), activeFile?.path ?? null)
}

/** Look a finding up in the loaded lists (the file's, the selected run's, the selected group's). */
export function findFinding(
  s: Pick<RunsState, 'fileFindings' | 'detail' | 'groupDetail'>,
  id: string | null,
): Finding | null {
  if (!id) return null
  return (
    s.fileFindings.find((f) => f.id === id) ??
    s.detail?.findings.find((f) => f.id === id) ??
    entryOfFinding(s.groupDetail?.merged ?? [], id)?.findings.find((f) => f.id === id) ??
    null
  )
}

/** Merge a run reported by an event or a response into the list (newest first). */
function upsertRun(runs: Run[], run: Run): Run[] {
  const i = runs.findIndex((r) => r.id === run.id)
  if (i < 0) return [run, ...runs]
  const old = runs[i]
  // Events and HTTP answers can arrive out of order: never let a "running"
  // snapshot overwrite a run that is already known to have ended.
  if (!isActive(old) && isActive(run)) return runs
  const next = [...runs]
  next[i] = run
  return next
}

/** Append streamed output to a run's buffer, merging text deltas and capping its size. */
function appendLive(chunks: LiveChunk[] | undefined, chunk: LiveChunk): LiveChunk[] {
  const next = [...(chunks ?? [])]
  const last = next[next.length - 1]
  if (chunk.kind === 'text' && last?.kind === 'text') next[next.length - 1] = { kind: 'text', text: last.text + chunk.text }
  else next.push(chunk)
  let total = next.reduce((n, c) => n + c.text.length, 0)
  while (total > LIVE_MAX_CHARS && next.length > 0) {
    const first = next[0]
    const excess = total - LIVE_MAX_CHARS
    if (first.text.length > excess && next.length === 1) {
      next[0] = { ...first, text: first.text.slice(excess) }
      total = LIVE_MAX_CHARS
    } else {
      next.shift()
      total -= first.text.length
    }
  }
  return next
}

function readSidebarPref(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_KEY) === 'open'
  } catch {
    return false
  }
}

/** Replace one finding (after PATCH) in a list, if it is there. */
function replaceFinding(list: Finding[], finding: Finding): Finding[] {
  return list.some((f) => f.id === finding.id) ? list.map((f) => (f.id === finding.id ? finding : f)) : list
}

/** Replace findings by id with fresher copies (e.g. with triage verdicts), keeping the rest. */
function replaceFindings(list: Finding[], fresh: Map<string, Finding>): Finding[] {
  return list.some((f) => fresh.has(f.id)) ? list.map((f) => fresh.get(f.id) ?? f) : list
}

/** The same for the findings inside a group's merged entries. */
function replaceInGroup(detail: GroupDetail | null, fresh: Map<string, Finding>): GroupDetail | null {
  if (!detail) return detail
  return { ...detail, merged: detail.merged.map((e) => ({ ...e, findings: replaceFindings(e.findings, fresh) })) }
}

/** Merge a group reported by an event or a response into the list (newest first). */
function upsertGroup(groups: RunGroup[], group: RunGroup): RunGroup[] {
  const i = groups.findIndex((g) => g.id === group.id)
  if (i < 0) return [group, ...groups].sort((a, b) => b.created_at - a.created_at)
  // As for runs: an "active" snapshot never overwrites a group known to have ended.
  if (!isGroupActive(groups[i]) && isGroupActive(group)) return groups
  const next = [...groups]
  next[i] = group
  return next
}

// ---------------------------------------------------------------------------
// Module-level bookkeeping (never rendered)
// ---------------------------------------------------------------------------
let detailToken = 0 // last detail fetch wins
let groupToken = 0 // last group-detail fetch wins
let groupTimer: ReturnType<typeof setTimeout> | undefined
let findingsToken = 0 // last file-findings fetch wins
let runsToken = 0
let revealCounter = 0
let toastCounter = 0
let toastTimer: ReturnType<typeof setTimeout> | undefined
let detailTimer: ReturnType<typeof setTimeout> | undefined
let findingsTimer: ReturnType<typeof setTimeout> | undefined

export const useRunsStore = create<RunsState>()((set, get) => {
  /** Fetch the selected run's detail (drops answers for a run no longer selected). */
  const fetchDetail = async () => {
    const id = get().selectedRunId
    const token = ++detailToken
    if (!id) return
    try {
      const detail = await api.getRun(id)
      if (token !== detailToken || get().selectedRunId !== id) return
      set((s) => ({ detail, runs: upsertRun(s.runs, detail.run) }))
    } catch (err) {
      if (token === detailToken) get().showToast(`Could not load run ${id}: ${api.errorMessage(err)}`, 'error')
    }
  }

  /** Coalesce bursts of events into one fetch each. */
  const detailSoon = () => {
    clearTimeout(detailTimer)
    detailTimer = setTimeout(() => void fetchDetail(), RELOAD_DEBOUNCE_MS)
  }
  const findingsSoon = () => {
    clearTimeout(findingsTimer)
    findingsTimer = setTimeout(() => void get().loadFindings(), RELOAD_DEBOUNCE_MS)
  }

  /** Phase 6: fetch the selected group's detail (drops answers for a group no longer selected). */
  const fetchGroup = async () => {
    const id = get().selectedGroupId
    const token = ++groupToken
    if (!id) return
    try {
      const detail = await api.getGroup(id)
      if (token !== groupToken || get().selectedGroupId !== id) return
      set((s) => {
        let runs = s.runs
        for (const run of detail.runs) runs = upsertRun(runs, run)
        return { groupDetail: detail, groups: upsertGroup(s.groups, detail.group), runs }
      })
    } catch (err) {
      if (token === groupToken) get().showToast(`Could not load group ${id}: ${api.errorMessage(err)}`, 'error')
    }
  }
  const groupSoon = () => {
    clearTimeout(groupTimer)
    groupTimer = setTimeout(() => void fetchGroup(), RELOAD_DEBOUNCE_MS)
  }
  /** True when run `id` belongs to the selected group. */
  const inSelectedGroup = (id: string | null | undefined) => {
    const groupId = get().selectedGroupId
    if (!groupId || !id) return false
    const group = get().groups.find((g) => g.id === groupId) ?? get().groupDetail?.group
    return !!group?.run_ids.includes(id)
  }

  /** Move the selection by `step` through the list on screen. */
  const step = (delta: 1 | -1) => {
    const s = get()
    const list = visibleList(s)
    if (list.length === 0) return
    // In a merged view the selected finding may be any finding of an entry: use its entry's first.
    const selected =
      useStore.getState().activeTab === 'findings' && s.selectedGroupId
        ? (entryOfFinding(visibleMergedEntries(s), s.selectedFindingId)?.findings[0]?.id ?? s.selectedFindingId)
        : s.selectedFindingId
    const i = list.findIndex((f) => f.id === selected)
    const next = i < 0 ? (delta === 1 ? 0 : list.length - 1) : Math.min(list.length - 1, Math.max(0, i + delta))
    // In the Findings tab the editor is hidden: only mark the card (it scrolls itself into view).
    const inEditor = useStore.getState().activeTab !== 'findings'
    get().selectFinding(list[next].id, { reveal: inEditor, moveCursor: true })
  }

  return {
    passes: [],
    passesError: null,
    runners: null,
    runs: [],
    selectedRunId: null,
    detail: null,
    fileFindings: [],
    fileFindingsPath: null,
    live: {},
    hiddenLevels: {},
    stateFilter: 'open',
    selectedFindingId: null,
    reveal: null,
    sidebarOpen: readSidebarPref(),
    toast: null,
    groups: [],
    selectedGroupId: null,
    groupDetail: null,
    triages: {},
    hideFlagged: false,

    loadAll: async () => {
      detailToken++
      findingsToken++
      groupToken++
      set({
        groups: [],
        selectedGroupId: null,
        groupDetail: null,
        triages: {},
        passes: [],
        passesError: null,
        runners: null,
        runs: [],
        selectedRunId: null,
        detail: null,
        fileFindings: [],
        fileFindingsPath: null,
        live: {},
        selectedFindingId: null,
        reveal: null,
      })
      await Promise.all([get().loadPasses(), get().loadRunners(), get().loadRuns(), get().loadFindings(), get().loadGroups()])
    },

    loadPasses: async () => {
      try {
        set({ passes: await api.getPasses(), passesError: null })
      } catch (err) {
        set({ passesError: api.errorMessage(err) })
      }
    },

    loadRunners: async () => {
      try {
        set({ runners: await api.getRunners() })
      } catch {
        // The launcher shows "no runners"; the next repo open retries.
      }
    },

    loadRuns: async () => {
      const token = ++runsToken
      try {
        const runs = await api.listRuns(50)
        if (token !== runsToken) return
        // Keep runs that events added meanwhile but the list does not have yet.
        set((s) => {
          let merged = runs
          for (const r of s.runs) if (!runs.some((x) => x.id === r.id)) merged = [r, ...merged]
          return { runs: merged }
        })
      } catch {
        // Keep what is on screen.
      }
    },

    startRun: async (request) => {
      try {
        const run = await api.startRun(request)
        set((s) => ({ runs: upsertRun(s.runs, run), live: { ...s.live, [run.id]: s.live[run.id] ?? [] } }))
        await get().selectRun(run.id)
        if (!isActive(run)) findingsSoon() // a cache hit already has findings
        return run
      } catch (err) {
        const prefix = err instanceof api.ApiError && err.status === 409 ? 'Already running' : 'Could not start the run'
        get().showToast(`${prefix}: ${api.errorMessage(err)}`, 'error')
        return null
      }
    },

    cancelRun: async (id) => {
      try {
        const run = await api.cancelRun(id)
        set((s) => ({ runs: upsertRun(s.runs, run) }))
      } catch (err) {
        get().showToast(`Cancel failed: ${api.errorMessage(err)}`, 'error')
      }
    },

    selectRun: async (id) => {
      if (get().selectedRunId !== id) set({ selectedRunId: id, detail: null })
      // A run and a group are never shown at the same time.
      if (id && get().selectedGroupId) {
        groupToken++
        set({ selectedGroupId: null, groupDetail: null })
      }
      if (!id) {
        detailToken++
        return
      }
      // The triage status line needs the run's latest triage (phase 6).
      await Promise.all([fetchDetail(), get().loadTriage(id)])
    },

    loadFindings: async () => {
      const path = useStore.getState().activeFile?.path ?? null
      const token = ++findingsToken
      if (!path) {
        set({ fileFindings: [], fileFindingsPath: null })
        return
      }
      try {
        const findings = await api.getFindings({ file: path })
        if (token !== findingsToken) return
        set({ fileFindings: findings, fileFindingsPath: path })
      } catch {
        // No repo or the backend is down: keep what is on screen.
      }
    },

    setFindingState: async (id, state) => {
      const before = visibleList(get())
      const wasSelected = get().selectedFindingId === id
      try {
        const finding = await api.setFindingState(id, state)
        set((s) => ({
          fileFindings: replaceFinding(s.fileFindings, finding),
          detail: s.detail ? { ...s.detail, findings: replaceFinding(s.detail.findings, finding) } : s.detail,
          groupDetail: replaceInGroup(s.groupDetail, new Map([[finding.id, finding]])),
        }))
        // When the selected finding drops out of the list (e.g. "Addressed" with the
        // "open" filter), select the one that took its place so work can continue.
        const after = visibleList(get())
        if (wasSelected && !after.some((f) => f.id === id)) {
          const index = before.findIndex((f) => f.id === id)
          const replacement = after[Math.min(index, after.length - 1)] ?? null
          const inEditor = useStore.getState().activeTab !== 'findings'
          get().selectFinding(replacement?.id ?? null, { reveal: inEditor && !!replacement, moveCursor: false })
        }
      } catch (err) {
        get().showToast(`Could not update the finding: ${api.errorMessage(err)}`, 'error')
      }
    },

    preview: (request) => api.previewRun(request),

    paste: async (request) => {
      try {
        const run = await api.pasteRun(request)
        set((s) => ({ runs: upsertRun(s.runs, run) }))
        await get().selectRun(run.id)
        findingsSoon()
        return run
      } catch (err) {
        get().showToast(`Paste-back failed: ${api.errorMessage(err)}`, 'error')
        return null
      }
    },

    selectFinding: (id, { reveal = false, moveCursor = true } = {}) => {
      set({ selectedFindingId: id })
      const finding = findFinding(get(), id)
      if (!finding) return
      // Also mark the spec node the finding refers to, so the Spec tab shows it selected.
      if (finding.json_ref) useSpecStore.getState().select(finding.json_ref)
      if (!reveal) return
      const editor = useStore.getState()
      // Opening another file saves unsaved edits first (store.openFile).
      if (editor.activeFile?.path !== finding.file) void editor.openFile(finding.file)
      set({
        reveal: {
          file: finding.file,
          line_start: finding.anchor_line_start,
          line_end: finding.anchor_line_end,
          moveCursor,
          seq: ++revealCounter,
        },
      })
    },

    openFinding: (finding) => {
      useStore.getState().setActiveTab('editor')
      get().setSidebarOpen(true)
      get().selectFinding(finding.id, { reveal: true, moveCursor: true })
    },

    nextFinding: () => step(1),
    prevFinding: () => step(-1),

    clearReveal: (seq) => {
      if (get().reveal?.seq === seq) set({ reveal: null })
    },

    toggleLevel: (level) => set((s) => ({ hiddenLevels: { ...s.hiddenLevels, [level]: !s.hiddenLevels[level] } })),

    setStateFilter: (filter) => set({ stateFilter: filter }),

    setSidebarOpen: (open) => {
      try {
        localStorage.setItem(SIDEBAR_KEY, open ? 'open' : 'closed')
      } catch {
        // Remembering the choice is optional.
      }
      set({ sidebarOpen: open })
    },

    showToast: (text, kind = 'info') => {
      clearTimeout(toastTimer)
      set({ toast: { text, kind, seq: ++toastCounter } })
      toastTimer = setTimeout(() => set({ toast: null }), TOAST_MS)
    },

    dismissToast: () => {
      clearTimeout(toastTimer)
      set({ toast: null })
    },

    handleServerEvent: (event) => {
      switch (event.type) {
        case 'run.started':
          if (!event.run?.id) break
          set((s) => ({
            runs: upsertRun(s.runs, event.run),
            live: { ...s.live, [event.run.id]: s.live[event.run.id] ?? [] },
          }))
          if (get().selectedRunId === event.run.id) detailSoon()
          if (inSelectedGroup(event.run.id)) groupSoon()
          break
        case 'run.event': {
          if (typeof event.run_id !== 'string' || typeof event.text !== 'string') break
          const kind = event.kind === 'status' ? 'status' : 'text'
          set((s) => ({ live: { ...s.live, [event.run_id]: appendLive(s.live[event.run_id], { kind, text: event.text }) } }))
          break
        }
        case 'run.finished':
        case 'run.failed': {
          const run = event.run
          if (!run?.id) break
          set((s) => ({ runs: upsertRun(s.runs, run) }))
          if (get().selectedRunId === run.id) detailSoon()
          if (inSelectedGroup(run.id)) groupSoon()
          findingsSoon()
          if (run.status === 'failed') {
            get().showToast(`Run ${run.pass} (${run.scope_name}) failed: ${run.error ?? 'unknown error'}`, 'error')
          }
          break
        }
        case 'findings.updated': {
          const path = get().fileFindingsPath ?? useStore.getState().activeFile?.path ?? null
          if (!event.file || event.file === path) findingsSoon()
          if (get().selectedRunId && (!event.run_id || event.run_id === get().selectedRunId)) detailSoon()
          if (get().selectedGroupId && (!event.run_id || inSelectedGroup(event.run_id))) groupSoon()
          break
        }
        case 'group.updated': {
          const group = event.group
          if (!group?.id) break
          set((s) => ({ groups: upsertGroup(s.groups, group) }))
          if (get().selectedGroupId === group.id) groupSoon()
          break
        }
        case 'triage.finished': {
          const triage = event.triage
          if (!triage?.run_id) break
          set((s) => ({ triages: { ...s.triages, [triage.run_id]: triage } }))
          if (triage.status === 'failed') {
            get().showToast(`Triage of ${triage.run_id} failed: ${triage.error ?? 'unknown error'}`, 'error')
          }
          // The verdicts are on the findings now: refresh every list that may show them.
          if (get().selectedRunId === triage.run_id) {
            void get().loadTriage(triage.run_id)
            detailSoon()
          }
          if (inSelectedGroup(triage.run_id)) groupSoon()
          findingsSoon()
          break
        }
      }
    },

    // ---- Phase 6: run groups and triage ---------------------------------------

    loadGroups: async () => {
      try {
        const groups = await api.listGroups(50)
        // Keep groups that events added meanwhile but the list does not have yet.
        set((s) => {
          let merged = groups
          for (const g of s.groups) if (!groups.some((x) => x.id === g.id)) merged = upsertGroup(merged, g)
          return { groups: merged }
        })
      } catch {
        // An older backend without groups (404), or the backend is down: keep what is on screen.
      }
    },

    startBoth: async (request) => {
      try {
        const group = await api.startBothGroup(request)
        set((s) => ({ groups: upsertGroup(s.groups, group) }))
        await get().selectGroup(group.id)
        return group
      } catch (err) {
        get().showToast(`Could not start "Run both": ${api.errorMessage(err)}`, 'error')
        return null
      }
    },

    startFanout: async (request) => {
      try {
        const group = await api.startFanoutGroup(request)
        set((s) => ({ groups: upsertGroup(s.groups, group) }))
        await get().selectGroup(group.id)
        return group
      } catch (err) {
        get().showToast(`Could not start the fan-out: ${api.errorMessage(err)}`, 'error')
        return null
      }
    },

    cancelGroup: async (id) => {
      try {
        const group = await api.cancelGroup(id)
        set((s) => ({ groups: upsertGroup(s.groups, group) }))
        if (get().selectedGroupId === id) groupSoon()
      } catch (err) {
        get().showToast(`Cancel failed: ${api.errorMessage(err)}`, 'error')
      }
    },

    selectGroup: async (id) => {
      if (get().selectedGroupId !== id) set({ selectedGroupId: id, groupDetail: null })
      if (id && get().selectedRunId) {
        detailToken++
        set({ selectedRunId: null, detail: null })
      }
      if (!id) {
        groupToken++
        return
      }
      await fetchGroup()
    },

    startTriage: async (runId, request) => {
      try {
        const triage = await api.startTriage(runId, request)
        set((s) => ({ triages: { ...s.triages, [runId]: triage } }))
        return triage
      } catch (err) {
        const prefix = err instanceof api.ApiError && err.status === 409 ? 'Triage already running' : 'Could not start the triage'
        get().showToast(`${prefix}: ${api.errorMessage(err)}`, 'error')
        return null
      }
    },

    loadTriage: async (runId) => {
      try {
        const result = await api.getTriage(runId)
        const fresh = new Map(result.findings.map((f) => [f.id, f]))
        set((s) => ({
          triages: { ...s.triages, [runId]: result.triage },
          detail:
            s.detail && s.detail.run.id === runId ? { ...s.detail, findings: replaceFindings(s.detail.findings, fresh) } : s.detail,
          fileFindings: replaceFindings(s.fileFindings, fresh),
          groupDetail: replaceInGroup(s.groupDetail, fresh),
        }))
      } catch (err) {
        // 404 from an older backend means "no triage support": treat as never triaged.
        if (err instanceof api.ApiError && err.status === 404) set((s) => ({ triages: { ...s.triages, [runId]: null } }))
      }
    },

    dismissFlagged: async (runId) => {
      const s = get()
      const findings = s.detail?.run.id === runId ? s.detail.findings : s.fileFindings.filter((f) => f.run_id === runId)
      const counts = flaggedCounts(findings)
      try {
        const updated = await api.dismissFlagged(runId)
        get().showToast(
          `Dismissed ${updated} flagged finding${updated === 1 ? '' : 's'} (${counts.wrong} wrong, ${counts.trivial} trivial).`,
        )
        // Refresh now rather than waiting for findings.updated, so the list updates at once.
        if (get().selectedRunId === runId) await fetchDetail()
        if (get().selectedGroupId) groupSoon()
        findingsSoon()
      } catch (err) {
        get().showToast(`Could not dismiss the flagged findings: ${api.errorMessage(err)}`, 'error')
      }
    },

    setHideFlagged: (on) => set({ hideFlagged: on }),
  }
})

// ---------------------------------------------------------------------------
// React hooks
// ---------------------------------------------------------------------------

/** The sidebar list for the file open in the editor (filtered, ordered). */
export function useVisibleFileFindings(): Finding[] {
  const fileFindings = useRunsStore((s) => s.fileFindings)
  const runs = useRunsStore((s) => s.runs)
  const groups = useRunsStore((s) => s.groups)
  const stateFilter = useRunsStore((s) => s.stateFilter)
  const hiddenLevels = useRunsStore((s) => s.hiddenLevels)
  const path = useStore((s) => s.activeFile?.path ?? null)
  const files = useStore((s) => s.files)
  return useMemo(() => {
    const order = files.map((f) => f.path)
    return visibleFileFindings({ fileFindings, runs, groups, stateFilter, hiddenLevels }, order, path)
  }, [fileFindings, runs, groups, stateFilter, hiddenLevels, path, files])
}

/** The selected run's findings for the Findings tab (filtered, ordered). */
export function useVisibleRunFindings(): Finding[] {
  const detail = useRunsStore((s) => s.detail)
  const stateFilter = useRunsStore((s) => s.stateFilter)
  const hiddenLevels = useRunsStore((s) => s.hiddenLevels)
  const hideFlagged = useRunsStore((s) => s.hideFlagged)
  const files = useStore((s) => s.files)
  return useMemo(
    () => visibleRunFindings({ detail, stateFilter, hiddenLevels, hideFlagged }, files.map((f) => f.path)),
    [detail, stateFilter, hiddenLevels, hideFlagged, files],
  )
}

/** Phase 6: the selected group's merged entries (filtered, in on-screen order). */
export function useVisibleMergedEntries(): MergedFinding[] {
  const groupDetail = useRunsStore((s) => s.groupDetail)
  const selectedGroupId = useRunsStore((s) => s.selectedGroupId)
  const stateFilter = useRunsStore((s) => s.stateFilter)
  const hiddenLevels = useRunsStore((s) => s.hiddenLevels)
  const hideFlagged = useRunsStore((s) => s.hideFlagged)
  return useMemo(
    () => visibleMergedEntries({ groupDetail, selectedGroupId, stateFilter, hiddenLevels, hideFlagged }),
    [groupDetail, selectedGroupId, stateFilter, hiddenLevels, hideFlagged],
  )
}

/** The selected finding object (from either list), or null. */
export function useSelectedFinding(): Finding | null {
  return useRunsStore((s) => findFinding(s, s.selectedFindingId))
}
