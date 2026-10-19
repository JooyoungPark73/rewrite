// RunRow: one run in the Findings tab's runs column (phase 4; moved out of
// FindingsView.tsx in phase 6 so group rows can list their member runs too).
//
//   paragraph-function · Section: Introduction          [cached] [done]
//   claude · sonnet · medium   $0.0123   1,234 in / 321 out   4.1 s   14:03:21
//   3 findings · 1 rejected                                       [Cancel]
//
// `nested` draws it indented under its group's row (GroupView.tsx), with a
// "↳" in front; everything else is the same, including Cancel.
import type { Run } from '../api/client'
import { isActive, useRunsStore } from '../state/runs'
import { formatCost, formatDuration, formatTime, formatTokens, STATUS_BADGE } from './format'

export interface RunRowProps {
  run: Run
  selected: boolean
  onSelect: () => void
  /** Drawn as a member under a group row. */
  nested?: boolean
}

export default function RunRow({ run, selected, onSelect, nested = false }: RunRowProps) {
  const cancelRun = useRunsStore((s) => s.cancelRun)
  const running = isActive(run)
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === 'Enter') onSelect()
      }}
      className={`flex cursor-pointer flex-col gap-0.5 border-b border-base-200 py-1.5 pr-3 text-sm hover:bg-base-200/60 ${
        nested ? 'border-l-2 border-l-base-300 pl-7' : 'pl-3'
      } ${selected ? 'bg-primary/10' : ''}`}
      data-testid="run-row"
      data-run-id={run.id}
      data-status={run.status}
      data-runner={run.runner}
      data-nested={nested ? 'true' : undefined}
      data-selected={selected ? 'true' : undefined}
    >
      <div className="flex items-center gap-1">
        {nested && <span className="text-base-content/40">↳</span>}
        <span className="truncate font-medium">{run.pass}</span>
        <span className="truncate text-base-content/60">· {run.scope_name}</span>
        <span className="ml-auto flex shrink-0 items-center gap-1">
          {run.status === 'cached' && (
            <span className="badge badge-xs badge-accent" title={`Reused run ${run.cached_from ?? ''}`} data-testid="cached-badge">
              cached
            </span>
          )}
          <span className={`badge badge-xs ${STATUS_BADGE[run.status] ?? 'badge-ghost'}`} data-testid="run-status">
            {running && <span className="loading loading-xs loading-spinner" />}
            {run.status}
          </span>
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-x-2 text-xs text-base-content/60">
        <span>
          {run.runner}
          {run.model ? ` · ${run.model}` : ''}
          {run.thinking ? ` · ${run.thinking}` : ''}
        </span>
        <span title="Cost reported by the runner" data-testid="run-cost">
          {formatCost(run.cost_usd)}
        </span>
        <span title="Tokens in / out" data-testid="run-tokens">
          {formatTokens(run.input_tokens)} in / {formatTokens(run.output_tokens)} out
        </span>
        <span title="Wall time" data-testid="run-duration">
          {formatDuration(run.duration_ms)}
        </span>
        <span className="ml-auto">{formatTime(run.started_at)}</span>
      </div>
      <div className="flex items-center gap-2 text-xs">
        <span data-testid="run-counts">
          {run.findings_count} finding{run.findings_count === 1 ? '' : 's'}
          {run.rejected_count > 0 && <span className="text-warning"> · {run.rejected_count} rejected</span>}
        </span>
        {run.error && (
          <span className="truncate text-error" title={run.error}>
            {run.error}
          </span>
        )}
        {running && (
          <button
            type="button"
            className="btn ml-auto btn-outline btn-xs btn-error"
            onClick={(e) => {
              e.stopPropagation()
              void cancelRun(run.id)
            }}
            data-testid="run-cancel"
          >
            Cancel
          </button>
        )}
      </div>
    </div>
  )
}
