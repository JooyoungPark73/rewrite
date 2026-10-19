// Run groups in the Findings tab (phase 6): the group row in the runs column
// and the merged view shown when a group is selected.
//
// GroupRow (runs column):
//   [both] paragraph-function · Section: Introduction             [running]
//   1/2 done ▓▓▓▓░░░░   $0.0123   4 findings · 1 agreement   14:03:21
//   ▾ 2 runs                                                      [Cancel]
//     ↳ paragraph-function · Section: Introduction  (claude member, RunRow.tsx)
//     ↳ paragraph-function · Section: Introduction  (codex member)
// The member rows show while the group is selected or still running (▸ / ▾
// toggles them); clicking one opens that run on its own (e.g. to triage it).
//
// GroupDetailPane (right side, instead of a run's detail):
//   header   kind, pass, scope, status, cost, Cancel
//   members  one chip per member run with its status (click = open the run)
//   entries  MergedEntryCard per merged finding, as the backend sorted them
//            (agreements first). "Run both": an agreement badge ("2/2 agree"
//            highlighted, "1/2" muted) and the runners that found it; ▸ unfolds
//            each runner's own problem / why. Fan-out: the entries are grouped
//            under one heading per member run (its section, or the
//            message-drift run over the paper's ends).
// Clicking an entry opens its first finding in the editor like a normal
// finding card; `j` / `k` step through the entries (state/runs.ts).
import { useEffect, useMemo, useRef, useState } from 'react'
import type { Finding, MergedFinding, Run, RunGroup } from '../api/client'
import {
  filterMerged,
  GROUP_STATUS_BADGE,
  groupProgress,
  groupScopeName,
  isGroupActive,
  mergedSections,
  runnerCount,
  VERDICT_STYLE,
} from '../state/groups'
import { isActive, useRunsStore, useVisibleMergedEntries } from '../state/runs'
import { FindingFilters } from './FindingCard'
import { formatCost, formatLines, formatTime, levelChip, SEVERITY_STYLE, STATUS_BADGE } from './format'
import RunRow from './RunRow'
import { HideFlaggedToggle } from './TriageBar'

/** "both" / "fan-out" as shown on the badge. */
const KIND_LABEL: Record<RunGroup['kind'], string> = { both: 'both', fanout: 'fan-out' }

/** "4 findings · 1 agreement" (agreements only for "Run both"). */
function countsText(group: RunGroup): string {
  const findings = `${group.findings_count} finding${group.findings_count === 1 ? '' : 's'}`
  if (group.kind !== 'both') return findings
  return `${findings} · ${group.agreements_count} agreement${group.agreements_count === 1 ? '' : 's'}`
}

// ---------------------------------------------------------------------------
// Runs column: one group
// ---------------------------------------------------------------------------

export interface GroupRowProps {
  group: RunGroup
  /** The member runs known to the store, in member order. */
  members: Run[]
  selected: boolean
  selectedRunId: string | null
}

export function GroupRow({ group, members, selected, selectedRunId }: GroupRowProps) {
  const selectGroup = useRunsStore((s) => s.selectGroup)
  const selectRun = useRunsStore((s) => s.selectRun)
  const cancelGroup = useRunsStore((s) => s.cancelGroup)
  const active = isGroupActive(group)
  const { ended, total } = groupProgress(group, members)
  // null = automatic: open while selected, running, or showing one of its members.
  const [manualOpen, setManualOpen] = useState<boolean | null>(null)
  const open = manualOpen ?? (selected || active || members.some((m) => m.id === selectedRunId))

  return (
    <div data-testid="group-item" data-group-id={group.id}>
      <div
        role="button"
        tabIndex={0}
        onClick={() => void selectGroup(group.id)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') void selectGroup(group.id)
        }}
        className={`flex cursor-pointer flex-col gap-0.5 border-b border-base-200 px-3 py-1.5 text-sm hover:bg-base-200/60 ${
          selected ? 'bg-primary/10' : ''
        }`}
        data-testid="group-row"
        data-group-id={group.id}
        data-kind={group.kind}
        data-status={group.status}
        data-selected={selected ? 'true' : undefined}
      >
        <div className="flex items-center gap-1">
          <span className="badge badge-xs badge-primary badge-outline" data-testid="group-kind">
            {KIND_LABEL[group.kind]}
          </span>
          <span className="truncate font-medium">{group.pass}</span>
          <span className="truncate text-base-content/60">· {groupScopeName(group, members)}</span>
          <span className={`badge ml-auto badge-xs ${GROUP_STATUS_BADGE[group.status] ?? 'badge-ghost'}`} data-testid="group-status">
            {active && <span className="loading loading-xs loading-spinner" />}
            {group.status}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-x-2 text-xs text-base-content/60">
          <span className="flex items-center gap-1" title="Member runs that have ended" data-testid="group-progress">
            {ended}/{total} done
            <progress className="progress w-14 progress-primary" value={ended} max={Math.max(total, 1)} />
          </span>
          <span title="Total cost of the member runs" data-testid="group-cost">
            {formatCost(group.cost_usd)}
          </span>
          <span data-testid="group-counts">{countsText(group)}</span>
          <span className="ml-auto">{formatTime(group.created_at)}</span>
        </div>
        <div className="flex items-center gap-2 text-xs">
          <button
            type="button"
            className="btn btn-ghost btn-xs"
            onClick={(e) => {
              e.stopPropagation()
              setManualOpen(!open)
            }}
            aria-expanded={open}
            title={open ? 'Hide the member runs' : 'Show the member runs'}
            data-testid="group-toggle"
          >
            {open ? '▾' : '▸'} {total} run{total === 1 ? '' : 's'}
          </button>
          {active && (
            <button
              type="button"
              className="btn ml-auto btn-outline btn-xs btn-error"
              onClick={(e) => {
                e.stopPropagation()
                void cancelGroup(group.id)
              }}
              title="Cancel every member run that is still queued or running"
              data-testid="group-cancel"
            >
              Cancel
            </button>
          )}
        </div>
      </div>
      {open &&
        members.map((run) => (
          <RunRow key={run.id} run={run} nested selected={run.id === selectedRunId} onSelect={() => void selectRun(run.id)} />
        ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Right side: the selected group's merged view
// ---------------------------------------------------------------------------

export function GroupDetailPane() {
  const group = useRunsStore((s) => s.groups.find((g) => g.id === s.selectedGroupId) ?? s.groupDetail?.group ?? null)
  const detail = useRunsStore((s) => (s.groupDetail?.group.id === s.selectedGroupId ? s.groupDetail : null))
  const stateFilter = useRunsStore((s) => s.stateFilter)
  const hiddenLevels = useRunsStore((s) => s.hiddenLevels)
  const hideFlagged = useRunsStore((s) => s.hideFlagged)
  const selectedFindingId = useRunsStore((s) => s.selectedFindingId)
  const openFinding = useRunsStore((s) => s.openFinding)
  const cancelGroup = useRunsStore((s) => s.cancelGroup)
  const selectRun = useRunsStore((s) => s.selectRun)
  // The same filtered list `j` / `k` use; the blocks below are cut from it.
  const visible = useVisibleMergedEntries()

  const sections = useMemo(
    () => (detail ? mergedSections(detail, filterMerged(detail.merged, { stateFilter, hiddenLevels, hideFlagged })) : []),
    [detail, stateFilter, hiddenLevels, hideFlagged],
  )
  const runnerOf = useMemo(() => {
    const byId = new Map((detail?.runs ?? []).map((r) => [r.id, r.runner]))
    return (f: Finding) => byId.get(f.run_id) ?? '?'
  }, [detail])

  if (!group) {
    return (
      <div className="flex flex-1 items-center justify-center p-6 text-base-content/50" data-testid="group-detail">
        Loading the group…
      </div>
    )
  }

  const active = isGroupActive(group)
  const members = detail?.runs ?? []
  const total = detail ? runnerCount(detail) : 2
  const anyVerdict = !!detail?.merged.some((e) => e.findings.some((f) => f.triage_verdict))
  const hidden = (detail?.merged.length ?? 0) - visible.length

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto" data-testid="group-detail" data-group-id={group.id}>
      <div className="flex flex-wrap items-center gap-2 border-b border-base-300 px-3 py-1.5 text-sm">
        <span className="badge badge-sm badge-primary badge-outline">{KIND_LABEL[group.kind]}</span>
        <span className="font-medium">{group.pass}</span>
        <span className="text-base-content/60">{groupScopeName(group, members)}</span>
        <span className={`badge badge-sm ${GROUP_STATUS_BADGE[group.status] ?? 'badge-ghost'}`} data-testid="group-detail-status">
          {active && <span className="loading loading-xs loading-spinner" />}
          {group.status}
        </span>
        <span className="text-xs text-base-content/60" title="Total cost of the member runs">
          {formatCost(group.cost_usd)}
        </span>
        {active && (
          <button
            type="button"
            className="btn ml-auto btn-outline btn-xs btn-error"
            onClick={() => void cancelGroup(group.id)}
            data-testid="group-detail-cancel"
          >
            Cancel
          </button>
        )}
      </div>

      {/* Member runs: status at a glance; click one to open it on its own (prompt, triage, raw output). */}
      <div className="flex flex-wrap items-center gap-1 border-b border-base-300 px-3 py-1.5 text-xs" data-testid="group-members">
        <span className="text-base-content/60">Runs:</span>
        {members.map((run) => (
          <button
            key={run.id}
            type="button"
            className="btn h-auto min-h-0 gap-1 px-2 py-0.5 btn-ghost btn-xs"
            onClick={() => void selectRun(run.id)}
            title={`${run.pass} · ${run.scope_name} — open this run`}
            data-testid="group-member"
            data-run-id={run.id}
            data-status={run.status}
          >
            {group.kind === 'fanout' ? run.scope_name : run.runner}
            {run.model ? ` · ${run.model}` : ''}
            <span className={`badge badge-xs ${STATUS_BADGE[run.status] ?? 'badge-ghost'}`}>
              {isActive(run) && <span className="loading loading-xs loading-spinner" />}
              {run.status}
            </span>
          </button>
        ))}
      </div>

      <div className="flex flex-col gap-2 p-3">
        <FindingFilters />
        <div className="flex flex-wrap items-center gap-2 text-xs text-base-content/70">
          <span data-testid="merged-summary">
            {detail
              ? group.kind === 'both'
                ? `${detail.merged.length} merged ${detail.merged.length === 1 ? 'entry' : 'entries'} · ${
                    detail.merged.filter((e) => e.agreement > 1).length
                  } found by both runners`
                : `${detail.merged.length} finding${detail.merged.length === 1 ? '' : 's'} from ${members.length} runs`
              : 'loading…'}
            {active && ' · more arrive as the runs finish'}
          </span>
          {hidden > 0 && <span>({hidden} hidden by the filters)</span>}
          {anyVerdict && <HideFlaggedToggle className="ml-auto" />}
        </div>
        {!detail && <span className="loading loading-sm loading-spinner" />}

        <div className="flex flex-col gap-3" data-testid="merged-list">
          {sections.map((section) =>
            group.kind === 'fanout' ? (
              <section
                key={section.key}
                className="flex flex-col gap-1.5"
                data-testid="merged-section"
                data-section={section.title}
                data-run-id={section.run?.id}
              >
                <h4 className="flex items-center gap-2 text-xs font-semibold text-base-content/70">
                  {section.title}
                  {section.run && section.run.pass !== group.pass && (
                    <span className="font-normal text-base-content/50">({section.run.pass})</span>
                  )}
                  {section.run && (
                    <span className={`badge badge-xs ${STATUS_BADGE[section.run.status] ?? 'badge-ghost'}`}>
                      {section.run.status}
                    </span>
                  )}
                </h4>
                {section.entries.length === 0 && (
                  <div className="text-xs text-base-content/50" data-testid="merged-section-empty">
                    {section.run && isActive(section.run) ? 'running…' : 'No findings here.'}
                  </div>
                )}
                {section.entries.map((entry) => (
                  <MergedEntryCard
                    key={entry.key}
                    entry={entry}
                    total={1}
                    showAgreement={false}
                    runnerOf={runnerOf}
                    selected={entry.findings.some((f) => f.id === selectedFindingId)}
                    onOpen={openFinding}
                  />
                ))}
              </section>
            ) : (
              section.entries.map((entry) => (
                <MergedEntryCard
                  key={entry.key}
                  entry={entry}
                  total={total}
                  showAgreement
                  runnerOf={runnerOf}
                  selected={entry.findings.some((f) => f.id === selectedFindingId)}
                  onOpen={openFinding}
                />
              ))
            ),
          )}
          {detail && group.kind === 'both' && detail.merged.length === 0 && !active && (
            <div className="text-sm text-base-content/60" data-testid="merged-empty">
              No findings from either runner.
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// One merged entry
// ---------------------------------------------------------------------------

interface MergedEntryCardProps {
  entry: MergedFinding
  /** How many runners the group compares (the "2" of "2/2 agree"). */
  total: number
  /** Agreement badge and runner chips ("Run both"; not for a fan-out). */
  showAgreement: boolean
  runnerOf: (finding: Finding) => string
  selected: boolean
  onOpen: (finding: Finding) => void
}

function MergedEntryCard({ entry, total, showAgreement, runnerOf, selected, onOpen }: MergedEntryCardProps) {
  const [open, setOpen] = useState(false)
  const first = entry.findings[0]
  const severity = SEVERITY_STYLE[entry.severity] ?? SEVERITY_STYLE.minor
  const everyone = entry.agreement >= total && total > 1

  // Keep the selected entry in view while j / k move through the list.
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (selected) ref.current?.scrollIntoView({ block: 'nearest' })
  }, [selected])

  if (!first) return null
  return (
    <div
      ref={ref}
      role="button"
      tabIndex={-1}
      onClick={() => onOpen(first)}
      className={`cursor-pointer rounded-r border border-l-4 border-base-300 ${severity.band} bg-base-100 px-2 py-1.5 text-sm shadow-xs hover:bg-base-200/60 ${
        selected ? 'ring-2 ring-primary/60' : ''
      } ${entry.findings.every((f) => f.state !== 'open') ? 'opacity-60' : ''}`}
      data-testid="merged-entry"
      data-key={entry.key}
      data-agreement={entry.agreement}
      data-file={entry.file}
      data-severity={entry.severity}
      data-selected={selected ? 'true' : undefined}
    >
      <div className="flex flex-wrap items-center gap-1">
        {showAgreement && (
          <span
            className={`badge badge-xs ${everyone ? 'badge-success' : entry.agreement > 1 ? 'badge-info' : 'badge-ghost text-base-content/50'}`}
            title={
              entry.agreement > 1
                ? `${entry.agreement} of ${total} runners reported this`
                : `Only ${entry.runners.join(', ')} reported this`
            }
            data-testid="agreement-badge"
          >
            {entry.agreement > 1 ? `${entry.agreement}/${total} agree` : `${entry.agreement}/${total}`}
          </span>
        )}
        <span className={`badge badge-xs ${severity.badge}`}>{entry.severity}</span>
        <span className={`badge badge-xs border ${levelChip(entry.level)}`}>{entry.level}</span>
        {entry.category && <span className="badge badge-xs badge-outline">{entry.category}</span>}
        {showAgreement &&
          entry.runners.map((name) => (
            <span key={name} className="badge badge-xs badge-neutral" data-testid="runner-chip">
              {name}
            </span>
          ))}
        <span className="ml-auto font-mono text-xs text-base-content/60" data-testid="merged-location">
          {formatLines(entry.file, entry.line_start, entry.line_end)}
        </span>
        <button
          type="button"
          className="btn btn-ghost btn-xs"
          onClick={(e) => {
            e.stopPropagation()
            setOpen(!open)
          }}
          aria-expanded={open}
          title={open ? 'Fold' : showAgreement ? "Show each runner's problem and why" : 'Show the details'}
          data-testid="merged-expand"
        >
          {open ? '▾' : '▸'}
        </button>
      </div>
      {first.quote && (
        <blockquote className="mt-1 line-clamp-2 border-l-2 border-base-300 pl-2 text-base-content/70 italic" title={first.quote}>
          {first.quote}
        </blockquote>
      )}
      <p className="mt-1" data-testid="merged-problem">
        {first.problem}
      </p>
      {open && (
        <div className="mt-1 flex flex-col gap-1.5">
          {entry.findings.map((f) => (
            <div
              key={f.id}
              className="rounded border border-base-300 bg-base-200/50 px-2 py-1 text-xs"
              data-testid="merged-runner-finding"
              data-runner={runnerOf(f)}
              data-finding-id={f.id}
            >
              <div className="flex flex-wrap items-center gap-1">
                <span className="badge badge-xs badge-neutral">{runnerOf(f)}</span>
                <span className={`badge badge-xs ${(SEVERITY_STYLE[f.severity] ?? SEVERITY_STYLE.minor).badge}`}>{f.severity}</span>
                <span className="font-mono text-base-content/60">{formatLines(f.file, f.anchor_line_start, f.anchor_line_end)}</span>
                {f.triage_verdict && (
                  <span
                    className={`badge badge-xs ${VERDICT_STYLE[f.triage_verdict].badge}`}
                    title={f.triage_reason ?? ''}
                    data-testid="verdict-badge"
                  >
                    {f.triage_verdict}
                  </span>
                )}
                {f.state !== 'open' && <span className="text-base-content/50">{f.state}</span>}
              </div>
              {f.quote !== first.quote && <div className="mt-0.5 text-base-content/60 italic">“{f.quote}”</div>}
              <p className="mt-0.5">
                <span className="font-medium">Problem:</span> {f.problem}
              </p>
              {f.why && (
                <p className="text-base-content/60">
                  <span className="font-medium">Why:</span> {f.why}
                </p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

