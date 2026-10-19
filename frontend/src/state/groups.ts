// Pure helpers for run groups, merged findings and triage (phase 6).
//
// No store lives here: the state (groups list, the selected group and its
// merged findings, triage per run) is part of the runs store in
// state/runs.ts, because selecting a group or a run, `j` / `k` and the editor
// highlights all have to agree on one selection. This file only holds the
// calculations, so they can be read (and tested) on their own:
//
//   groupOfRun        run id -> id of the group that owns it
//   buildRunListItems the Findings tab's left column: groups (with their
//                     member runs) and standalone runs, newest first
//   groupProgress     "2/5 done" for a group row
//   mergedSections    the merged view's blocks: one block for "Run both",
//                     one block per member run (section) for a fan-out
//   filterMerged      the level / open-all / hide-flagged filters for entries
//   isFlagged, flaggedCounts   triage verdicts "wrong" and "trivial"
import type { Finding, GroupDetail, GroupStatus, MergedFinding, Run, RunGroup, TriageVerdict } from '../api/client'

/** Group statuses that mean "still going" (spinner, Cancel button). */
export function isGroupActive(group: RunGroup): boolean {
  return group.status === 'queued' || group.status === 'running'
}

/** Run statuses that mean the run has ended, whatever the outcome. */
function hasEnded(run: Run): boolean {
  return run.status !== 'queued' && run.status !== 'running'
}

/** Map from member run id to the id of its group. */
export function groupOfRun(groups: RunGroup[]): Map<string, string> {
  const map = new Map<string, string>()
  for (const group of groups) for (const id of group.run_ids) map.set(id, group.id)
  return map
}

/** One row of the runs column: a group (with the member runs we know of) or a single run. */
export type RunListItem = { kind: 'group'; group: RunGroup; members: Run[] } | { kind: 'run'; run: Run }

/** When a run was created, for interleaving with groups (unknown = newest). */
function runTime(run: Run): number {
  return run.created_at ?? run.started_at ?? Number.POSITIVE_INFINITY
}

/**
 * The runs column, newest first. Member runs are not listed on their own:
 * they sit under their group's row. `runs` keeps the backend's order; each
 * group is slotted in before the first standalone run older than it.
 */
export function buildRunListItems(runs: Run[], groups: RunGroup[]): RunListItem[] {
  const owner = groupOfRun(groups)
  const byId = new Map(runs.map((r) => [r.id, r]))
  const pendingGroups = [...groups].sort((a, b) => b.created_at - a.created_at)
  const items: RunListItem[] = []
  const pushGroupsNewerThan = (time: number) => {
    while (pendingGroups.length > 0 && pendingGroups[0].created_at >= time) {
      const group = pendingGroups.shift()!
      const members = group.run_ids.map((id) => byId.get(id)).filter((r): r is Run => !!r)
      items.push({ kind: 'group', group, members })
    }
  }
  for (const run of runs) {
    if (owner.has(run.id)) continue
    pushGroupsNewerThan(runTime(run))
    items.push({ kind: 'run', run })
  }
  pushGroupsNewerThan(Number.NEGATIVE_INFINITY)
  return items
}

/** Members that have ended, out of all members ("2/5 done"). */
export function groupProgress(group: RunGroup, members: Run[]): { ended: number; total: number } {
  const total = group.run_ids.length
  // Once the group itself has ended, every member has.
  if (!isGroupActive(group)) return { ended: total, total }
  return { ended: Math.min(total, members.filter(hasEnded).length), total }
}

/** "Section: Introduction" for a "both" group (from a member run), "the whole paper" for a fan-out. */
export function groupScopeName(group: RunGroup, members: Run[]): string {
  if (group.kind === 'fanout') return 'whole paper, per section'
  const named = members.find((r) => r.scope_name)?.scope_name
  if (named) return named
  const s = group.scope
  return s.kind === 'section' ? `Section: ${s.ref}` : s.kind === 'paper' ? 'the whole paper' : (s.file ?? s.kind)
}

/** How many runners a "both" group compares (the "2" of "2/2 agree"). */
export function runnerCount(detail: GroupDetail): number {
  const runners = new Set(detail.runs.map((r) => r.runner))
  return Math.max(runners.size, ...detail.merged.map((e) => e.runners.length), 1)
}

/** True when triage marked the finding wrong or trivial. */
export function isFlagged(finding: Finding): boolean {
  return finding.triage_verdict === 'wrong' || finding.triage_verdict === 'trivial'
}

/** Open findings that triage flagged, by verdict (what "Dismiss all flagged" would dismiss). */
export function flaggedCounts(findings: Finding[]): { wrong: number; trivial: number; total: number } {
  let wrong = 0
  let trivial = 0
  for (const f of findings) {
    if (f.state !== 'open') continue
    if (f.triage_verdict === 'wrong') wrong++
    else if (f.triage_verdict === 'trivial') trivial++
  }
  return { wrong, trivial, total: wrong + trivial }
}

/** How many findings carry each verdict (for the triage status line). */
export function verdictCounts(findings: Finding[]): Record<TriageVerdict, number> {
  const counts: Record<TriageVerdict, number> = { keep: 0, wrong: 0, trivial: 0 }
  for (const f of findings) if (f.triage_verdict) counts[f.triage_verdict]++
  return counts
}

/** The filters of the Findings tab, as they apply to merged entries. */
export interface MergedFilters {
  stateFilter: 'open' | 'all'
  hiddenLevels: Record<string, boolean>
  hideFlagged: boolean
}

/**
 * An entry stays when its level is shown, at least one of its findings is
 * open (with the "open" filter), and not all of its findings were flagged by
 * triage (with "hide wrong/trivial").
 */
export function filterMerged(entries: MergedFinding[], filters: MergedFilters): MergedFinding[] {
  return entries.filter(
    (e) =>
      !filters.hiddenLevels[e.level] &&
      (filters.stateFilter === 'all' || e.findings.some((f) => f.state === 'open')) &&
      !(filters.hideFlagged && e.findings.length > 0 && e.findings.every(isFlagged)),
  )
}

/** One block of the merged view. */
export interface MergedSection {
  /** Stable key: the member run id (fan-out) or "all" ("Run both"). */
  key: string
  /** Heading: the member run's scope name, e.g. "Section: Method"; "" for "Run both". */
  title: string
  /** The member run of a fan-out block (status badge, click to open it). */
  run: Run | null
  entries: MergedFinding[]
}

/**
 * Split (already filtered) entries into the blocks shown on screen. "Run
 * both": one block, entries in the order the backend sent (agreements
 * first). Fan-out: one block per member run, in member order, holding the
 * entries whose finding came from that run, so each section's problems sit
 * under its heading; members without findings still get a (empty) block.
 */
export function mergedSections(detail: GroupDetail, entries: MergedFinding[]): MergedSection[] {
  if (detail.group.kind !== 'fanout') return [{ key: 'all', title: '', run: null, entries }]
  const byId = new Map(detail.runs.map((r) => [r.id, r]))
  const order = detail.group.run_ids.length > 0 ? detail.group.run_ids : detail.runs.map((r) => r.id)
  const sections: MergedSection[] = order.map((id) => ({
    key: id,
    title: byId.get(id)?.scope_name ?? id,
    run: byId.get(id) ?? null,
    entries: [],
  }))
  const index = new Map(sections.map((s, i) => [s.key, i]))
  for (const entry of entries) {
    const runId = entry.findings[0]?.run_id
    const i = runId !== undefined ? index.get(runId) : undefined
    if (i !== undefined) sections[i].entries.push(entry)
    else {
      // A finding of a run the group does not list (should not happen): keep it visible.
      if (!index.has('other')) {
        index.set('other', sections.length)
        sections.push({ key: 'other', title: 'Other', run: null, entries: [] })
      }
      sections[index.get('other')!].entries.push(entry)
    }
  }
  return sections
}

/** The entries in on-screen order (blocks one after the other), for `j` / `k`. */
export function orderedMerged(detail: GroupDetail, filters: MergedFilters): MergedFinding[] {
  return mergedSections(detail, filterMerged(detail.merged, filters)).flatMap((s) => s.entries)
}

/** The entry that holds finding `id`, or null. */
export function entryOfFinding(entries: MergedFinding[], id: string | null): MergedFinding | null {
  if (!id) return null
  return entries.find((e) => e.findings.some((f) => f.id === id)) ?? null
}

/** Badge color per group status (spelled out so Tailwind keeps the classes). */
export const GROUP_STATUS_BADGE: Record<GroupStatus, string> = {
  queued: 'badge-ghost',
  running: 'badge-info',
  done: 'badge-success',
  partial: 'badge-warning',
  failed: 'badge-error',
  cancelled: 'badge-neutral',
}

/** Badge color and label per triage verdict: keep green, wrong red, trivial grey. */
export const VERDICT_STYLE: Record<TriageVerdict, { badge: string; label: string }> = {
  keep: { badge: 'badge-success', label: 'keep' },
  wrong: { badge: 'badge-error', label: 'wrong' },
  trivial: { badge: 'border-slate-400 bg-slate-400 text-white', label: 'trivial' },
}
