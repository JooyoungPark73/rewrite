// FindingCard: one review finding, as shown in the editor's findings column
// and in the Findings tab. Also exports FindingFilters (the level chips and
// the open/all switch shared by both lists).
//
//   ┃ major  paragraph  function                 intro.tex:41–44  moved
//   ┃ "quote from the source, in italics…"
//   ┃ Problem: what is wrong.
//   ┃ Why: why it matters for the paper's argument.
//   ┃ ⟶ intro-3                         [Addressed] [Dismiss]   (or [Reopen])
//
// The left band is the severity color. Clicking the card (anywhere except a
// button) calls `onOpen`: the parent highlights the finding in the editor.
// The `json_ref` chip jumps to that node in the Spec tab. State buttons send
// PATCH /api/findings/{id}; there is no "apply" or "rewrite" button, by design.
//
// Phase 6: after a triage call the card shows its verdict as a badge (keep
// green, wrong red, trivial grey). Hovering shows the model's reason; a click
// on the badge unfolds it under the card. The verdict never changes the
// finding's state by itself.
import { useEffect, useRef, useState, type MouseEvent } from 'react'
import { FINDING_LEVELS, type Finding, type FindingState } from '../api/client'
import { VERDICT_STYLE } from '../state/groups'
import { useReviseStore } from '../state/revise'
import { useRunsStore } from '../state/runs'
import { ANCHOR_BADGE, formatLines, levelChip, SEVERITY_STYLE, showSpecNode } from './format'

export interface FindingCardProps {
  finding: Finding
  selected: boolean
  onOpen: (finding: Finding) => void
  /** Show the file name with the lines (the Findings tab groups by file, so it can omit the folder). */
  showFile?: boolean
}

export default function FindingCard({ finding, selected, onOpen, showFile = true }: FindingCardProps) {
  const setFindingState = useRunsStore((s) => s.setFindingState)
  const severity = SEVERITY_STYLE[finding.severity] ?? SEVERITY_STYLE.minor
  const anchor = finding.anchor_state !== 'ok' ? ANCHOR_BADGE[finding.anchor_state] : null
  const verdict = finding.triage_verdict ? VERDICT_STYLE[finding.triage_verdict] : null
  const [reasonOpen, setReasonOpen] = useState(false)

  // Keep the selected card in view while j / k move through the list.
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (selected) ref.current?.scrollIntoView({ block: 'nearest' })
  }, [selected])

  /** State buttons must not also "open" the card. */
  const setState = (e: MouseEvent, state: FindingState) => {
    e.stopPropagation()
    void setFindingState(finding.id, state)
  }

  return (
    <div
      ref={ref}
      role="button"
      tabIndex={-1}
      onClick={() => onOpen(finding)}
      className={`cursor-pointer rounded-r border border-l-4 border-base-300 ${severity.band} bg-base-100 px-2 py-1.5 text-sm shadow-xs hover:bg-base-200/60 ${
        selected ? 'ring-2 ring-primary/60' : ''
      } ${finding.state !== 'open' ? 'opacity-60' : ''}`}
      data-testid="finding-card"
      data-finding-id={finding.id}
      data-severity={finding.severity}
      data-level={finding.level}
      data-state={finding.state}
      data-anchor-state={finding.anchor_state}
      data-verdict={finding.triage_verdict ?? undefined}
      data-selected={selected ? 'true' : undefined}
    >
      <div className="flex flex-wrap items-center gap-1">
        <span className={`badge badge-xs ${severity.badge}`} data-testid="finding-severity">
          {finding.severity}
        </span>
        {finding.category === 'comment' && (
          <span className="badge badge-xs badge-soft badge-accent" title="Your own comment (not a model finding)" data-testid="comment-badge">
            comment
          </span>
        )}
        <span className={`badge badge-xs border ${levelChip(finding.level)}`}>{finding.level}</span>
        {finding.category && <span className="badge badge-xs badge-outline">{finding.category}</span>}
        <span
          className="ml-auto font-mono text-xs text-base-content/60"
          title={`${finding.file}, lines ${finding.anchor_line_start}–${finding.anchor_line_end} (the model said ${finding.line_start}–${finding.line_end})`}
          data-testid="finding-location"
        >
          {showFile
            ? formatLines(finding.file, finding.anchor_line_start, finding.anchor_line_end)
            : finding.anchor_line_end > finding.anchor_line_start
              ? `${finding.anchor_line_start}–${finding.anchor_line_end}`
              : `${finding.anchor_line_start}`}
        </span>
        {anchor && (
          <span className={`badge badge-xs ${anchor.className}`} title={anchor.title} data-testid="anchor-badge">
            {anchor.label}
          </span>
        )}
        {verdict && (
          <button
            type="button"
            className={`badge cursor-pointer badge-xs ${verdict.badge}`}
            onClick={(e) => {
              e.stopPropagation()
              setReasonOpen(!reasonOpen)
            }}
            title={`Triage: ${verdict.label}${finding.triage_reason ? ` — ${finding.triage_reason}` : ''} (click to ${reasonOpen ? 'hide' : 'show'} the reason)`}
            aria-expanded={reasonOpen}
            data-testid="verdict-badge"
            data-verdict={finding.triage_verdict ?? undefined}
          >
            {verdict.label}
          </button>
        )}
      </div>

      {finding.quote && (
        <blockquote className="mt-1 line-clamp-3 border-l-2 border-base-300 pl-2 text-base-content/70 italic" title={finding.quote}>
          {finding.quote}
        </blockquote>
      )}
      <p className="mt-1" data-testid="finding-problem">
        {finding.problem}
      </p>
      {finding.why && <p className="mt-0.5 text-xs text-base-content/60">{finding.why}</p>}
      {verdict && reasonOpen && (
        <p className="mt-1 rounded bg-base-200 px-1.5 py-0.5 text-xs" data-testid="verdict-reason">
          <span className="font-medium">Triage ({verdict.label}):</span> {finding.triage_reason || 'no reason given'}
        </p>
      )}

      <div className="mt-1 flex flex-wrap items-center gap-1">
        {finding.json_ref && (
          <button
            type="button"
            className="badge cursor-pointer badge-xs font-mono badge-ghost hover:badge-primary"
            onClick={(e) => {
              e.stopPropagation()
              showSpecNode(finding.json_ref!)
            }}
            title="Show this node in the Spec tab"
            data-testid="finding-json-ref"
          >
            ⟶ {finding.json_ref}
          </button>
        )}
        <span className="ml-auto flex gap-1">
          {/* Phase 8 (Revise): a model proposes a rewrite for the quoted span or the paragraph. */}
          <button
            type="button"
            className="btn btn-ghost btn-xs"
            onClick={(e) => {
              e.stopPropagation()
              useReviseStore.getState().openFor(finding)
            }}
            title="Ask a model for a rewrite that answers this finding; you see the diff and approve it (written as \\aigen{…})"
            data-testid="finding-rewrite"
          >
            ✎ Rewrite…
          </button>
          {finding.state === 'open' ? (
            <>
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                onClick={(e) => setState(e, 'addressed')}
                title="I fixed this"
                data-testid="finding-addressed"
              >
                ✓ Addressed
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                onClick={(e) => setState(e, 'dismissed')}
                title="Not a real problem"
                data-testid="finding-dismiss"
              >
                ✕ Dismiss
              </button>
            </>
          ) : (
            <>
              <span className="self-center text-xs text-base-content/50">{finding.state}</span>
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                onClick={(e) => setState(e, 'open')}
                data-testid="finding-reopen"
              >
                ↺ Reopen
              </button>
            </>
          )}
        </span>
      </div>
    </div>
  )
}

/**
 * Level chips (click to hide / show a level) and the open / all switch.
 * The filters live in the runs store, so the editor column and the Findings
 * tab always agree.
 */
export function FindingFilters() {
  const hiddenLevels = useRunsStore((s) => s.hiddenLevels)
  const toggleLevel = useRunsStore((s) => s.toggleLevel)
  const stateFilter = useRunsStore((s) => s.stateFilter)
  const setStateFilter = useRunsStore((s) => s.setStateFilter)
  return (
    <div className="flex flex-wrap items-center gap-1" data-testid="finding-filters">
      {FINDING_LEVELS.map((level) => {
        const on = !hiddenLevels[level]
        return (
          <button
            key={level}
            type="button"
            className={`badge cursor-pointer badge-sm border ${on ? levelChip(level) : 'border-base-300 bg-base-100 text-base-content/40 line-through'}`}
            onClick={() => toggleLevel(level)}
            aria-pressed={on}
            title={on ? `Hide ${level} findings` : `Show ${level} findings`}
            data-testid={`level-chip-${level}`}
          >
            {level}
          </button>
        )
      })}
      <div className="join ml-auto">
        {(['open', 'all'] as const).map((value) => (
          <button
            key={value}
            type="button"
            className={`btn join-item btn-xs ${stateFilter === value ? 'btn-active' : ''}`}
            onClick={() => setStateFilter(value)}
            aria-pressed={stateFilter === value}
            data-testid={`state-filter-${value}`}
          >
            {value}
          </button>
        ))}
      </div>
    </div>
  )
}
