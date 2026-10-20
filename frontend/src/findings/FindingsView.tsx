// FindingsView: the "Findings" tab (phase 4).
//
//   ┌ RunLauncher: pass, scope, runner, model, thinking, Show prompt, Run ───────┐
//   ├ Runs ─────────────────┬ Selected run ─────────────────────────────────────┤
//   │ paragraph-function    │ header: pass, scope, model, cost, tokens, Prompt   │
//   │ Section: Intro  done  │ Live output (while it runs)                        │
//   │ $0.0123 1.2k/321 4.1s │ filters                                            │
//   │ 3 findings 1 rejected │ sections/intro.tex                                 │
//   │ ...          [Cancel] │   cards (FindingCard.tsx), click -> Editor tab     │
//   │                       │ ▸ Rejected (1): reason + the raw JSON              │
//   │                       │ ▸ Raw output                                       │
//   └───────────────────────┴────────────────────────────────────────────────────┘
//
// Runs update live from WebSocket events (state/runs.ts). Clicking a finding
// switches to the Editor tab, opens its file and highlights its lines; `j` /
// `k` step through the cards on this tab without leaving it.
//
// On narrow screens the editor's findings column is hidden, so this tab also
// shows the findings of the file open in the editor (FindingsSidebar).
//
// Phase 6: the runs column also lists run groups ("Run both", "Fan out";
// GroupView.tsx) with their member runs under them, and selecting a group
// shows its merged view on the right instead of a run's detail. A single
// run's detail gains the triage bar (TriageBar.tsx) above its findings.
import { useEffect, useMemo, useRef, useState } from 'react'
import { getRunPrompt, type Finding } from '../api/client'
import { buildRunListItems } from '../state/groups'
import { isActive, useRunsStore, useVisibleRunFindings, type LiveChunk } from '../state/runs'
import FindingCard, { FindingFilters } from './FindingCard'
import FindingsSidebar from './FindingsSidebar'
import { STATUS_BADGE } from './format'
import { GroupDetailPane, GroupRow } from './GroupView'
import PromptDialog from './PromptDialog'
import RunLauncher from './RunLauncher'
import RunRow from './RunRow'
import TriageBar from './TriageBar'

export default function FindingsView({ active }: { active: boolean }) {
  // Pass files are re-read by the backend when they change on disk: refresh
  // the list (and the runs) each time this tab is opened.
  useEffect(() => {
    if (!active) return
    const store = useRunsStore.getState()
    void store.loadPasses()
    void store.loadRuns()
    void store.loadGroups()
  }, [active])

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col" data-testid="findings-view">
      <RunLauncher />
      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        <FindingsSidebar testId="findings-sidebar-narrow" className="max-h-[40vh] border-b border-base-300 lg:hidden" />
        <RunList />
        <DetailPane />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Runs list
// ---------------------------------------------------------------------------

function RunList() {
  const runs = useRunsStore((s) => s.runs)
  const groups = useRunsStore((s) => s.groups)
  const selectedRunId = useRunsStore((s) => s.selectedRunId)
  const selectedGroupId = useRunsStore((s) => s.selectedGroupId)
  const selectRun = useRunsStore((s) => s.selectRun)
  // Groups interleaved with the runs that belong to no group, newest first.
  const items = useMemo(() => buildRunListItems(runs, groups), [runs, groups])
  return (
    <aside
      className="flex max-h-[40vh] min-h-0 shrink-0 flex-col border-b border-base-300 lg:max-h-none lg:w-96 lg:border-r lg:border-b-0"
      data-testid="run-list"
    >
      <div className="border-b border-base-300 px-3 py-1 text-sm font-medium">Runs</div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {items.length === 0 && <div className="p-3 text-sm text-base-content/50">No runs yet. Pick a pass above and press Run.</div>}
        {items.map((item) =>
          item.kind === 'group' ? (
            <GroupRow
              key={item.group.id}
              group={item.group}
              members={item.members}
              selected={item.group.id === selectedGroupId}
              selectedRunId={selectedRunId}
            />
          ) : (
            <RunRow
              key={item.run.id}
              run={item.run}
              selected={item.run.id === selectedRunId}
              onSelect={() => void selectRun(item.run.id)}
            />
          ),
        )}
      </div>
    </aside>
  )
}

// ---------------------------------------------------------------------------
// Selected run
// ---------------------------------------------------------------------------

/** Right side: the selected group's merged view, or the selected run's detail. */
function DetailPane() {
  const groupSelected = useRunsStore((s) => s.selectedGroupId !== null)
  return groupSelected ? <GroupDetailPane /> : <RunDetailPane />
}

function RunDetailPane() {
  const selectedRunId = useRunsStore((s) => s.selectedRunId)
  const run = useRunsStore((s) => s.runs.find((r) => r.id === s.selectedRunId) ?? s.detail?.run ?? null)
  const detail = useRunsStore((s) => (s.detail?.run.id === s.selectedRunId ? s.detail : null))
  const live = useRunsStore((s) => (s.selectedRunId ? s.live[s.selectedRunId] : undefined))
  const findings = useVisibleRunFindings()
  const selectedFindingId = useRunsStore((s) => s.selectedFindingId)
  const openFinding = useRunsStore((s) => s.openFinding)
  const [promptOpen, setPromptOpen] = useState(false)

  // Group the (already ordered) findings by file.
  const byFile = useMemo(() => {
    const groups: [string, Finding[]][] = []
    for (const f of findings) {
      const last = groups[groups.length - 1]
      if (last && last[0] === f.file) last[1].push(f)
      else groups.push([f.file, [f]])
    }
    return groups
  }, [findings])

  if (!selectedRunId || !run) {
    return (
      <div className="flex flex-1 items-center justify-center p-6 text-base-content/50" data-testid="run-detail">
        Select a run to see its findings.
      </div>
    )
  }

  const running = isActive(run)
  const total = detail?.findings.length ?? 0

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto" data-testid="run-detail" data-run-id={run.id}>
      <div className="flex flex-wrap items-center gap-2 border-b border-base-300 px-3 py-1.5 text-sm">
        <span className="font-medium">{run.pass}</span>
        <span className="text-base-content/60">{run.scope_name}</span>
        <span className={`badge badge-sm ${STATUS_BADGE[run.status] ?? 'badge-ghost'}`} data-testid="detail-status">
          {running && <span className="loading loading-xs loading-spinner" />}
          {run.status}
        </span>
        {run.git_sha && <span className="font-mono text-xs text-base-content/50">@{run.git_sha.slice(0, 7)}</span>}
        <button type="button" className="btn ml-auto btn-ghost btn-xs" onClick={() => setPromptOpen(true)} data-testid="run-prompt">
          Prompt
        </button>
      </div>

      {/* Phase 6: triage the run's findings (verdict badges, hide, dismiss flagged). */}
      {detail && <TriageBar run={run} findings={detail.findings} />}

      {/* Streamed output: open while the run is going, folded away afterwards (Raw output has it all). */}
      {running ? (
        <LiveOutput chunks={live ?? []} running runner={run.runner} startedAt={run.started_at} />
      ) : (
        live &&
        live.length > 0 && (
          <details className="border-b border-base-300 px-3 py-1 text-xs" data-testid="live-output-done">
            <summary className="cursor-pointer text-base-content/70">Live output (as streamed)</summary>
            <LiveOutput chunks={live} running={false} />
          </details>
        )
      )}

      <div className="flex flex-col gap-2 p-3">
        <FindingFilters />
        {!detail && !running && <span className="loading loading-sm loading-spinner" />}
        {detail && total === 0 && !running && (
          <div className="text-sm text-base-content/60" data-testid="no-findings">
            {run.status === 'failed' || run.status === 'cancelled' ? `This run ${run.status}.` : 'No findings: nothing to fix here.'}
          </div>
        )}
        {detail && total > 0 && findings.length === 0 && (
          <div className="text-sm text-base-content/60">Nothing matches the filters ({total} hidden).</div>
        )}
        <div className="flex flex-col gap-3" data-testid="run-findings">
          {byFile.map(([file, list]) => (
            <section key={file} className="flex flex-col gap-1.5" data-testid="file-group" data-file={file}>
              <h4 className="font-mono text-xs font-semibold text-base-content/70">{file}</h4>
              {list.map((f) => (
                <FindingCard key={f.id} finding={f} selected={f.id === selectedFindingId} showFile={false} onOpen={openFinding} />
              ))}
            </section>
          ))}
        </div>

        {detail && detail.rejected.length > 0 && (
          <details className="rounded border border-warning/40 bg-warning/5 p-2 text-sm" data-testid="rejected">
            <summary className="cursor-pointer font-medium">Rejected ({detail.rejected.length})</summary>
            <p className="mt-1 text-xs text-base-content/60">
              Findings the validator refused (rewrites, praise, unknown fields...). They are never shown as findings.
            </p>
            {detail.rejected.map((r) => (
              <div key={r.index} className="mt-2" data-testid="rejected-item">
                <div className="text-xs">
                  #{r.index} <span className="badge badge-xs badge-warning" data-testid="rejected-reason">{r.reason}</span>
                </div>
                <pre className="mt-1 max-h-48 overflow-auto rounded bg-base-200 p-2 font-mono text-xs whitespace-pre-wrap">
                  {JSON.stringify(r.raw, null, 2)}
                </pre>
              </div>
            ))}
          </details>
        )}

        {detail && !running && detail.raw_output != null && (
          <details className="rounded border border-base-300 p-2 text-sm" data-testid="raw-output">
            <summary className="cursor-pointer font-medium">Raw output</summary>
            <pre className="mt-1 max-h-96 overflow-auto rounded bg-base-200 p-2 font-mono text-xs whitespace-pre-wrap">
              {detail.raw_output || '(empty)'}
            </pre>
          </details>
        )}
      </div>

      {promptOpen && (
        <PromptDialog title={`Prompt sent by ${run.id}`} load={() => getRunPrompt(run.id)} onClose={() => setPromptOpen(false)} />
      )}
    </div>
  )
}

/** Streamed output of a run (model text plus status lines), kept scrolled to the end. */
/**
 * The streamed output of a run. claude streams text (and the structured-output JSON) as it is
 * produced. codex does not: `codex exec --json` emits nothing between `turn.started` and the
 * finished `agent_message` (measured 2026-10-19: 21 s of silence on a 250-word answer; plain
 * `codex exec` is the same), so for codex the panel shows the elapsed time and says why.
 */
function LiveOutput({
  chunks,
  running,
  runner,
  startedAt,
}: {
  chunks: LiveChunk[]
  running: boolean
  runner?: string
  startedAt?: number | null
}) {
  const ref = useRef<HTMLPreElement>(null)
  useEffect(() => {
    const el = ref.current
    if (el) el.scrollTop = el.scrollHeight
  }, [chunks])
  // Elapsed seconds since the run started, ticking while it runs.
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => {
    if (!running) return
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => clearInterval(timer)
  }, [running])
  const elapsed = running && startedAt ? Math.max(0, Math.round(now - startedAt)) : null
  const noPartialOutput = runner === 'codex'
  return (
    <div className="border-b border-base-300 px-3 py-2">
      <div className="mb-1 flex items-center gap-2 text-xs font-medium text-base-content/70">
        Live output
        {running && <span className="loading loading-xs loading-dots" />}
        {elapsed !== null && (
          <span className="font-normal text-base-content/50" data-testid="live-elapsed">
            {elapsed}s
          </span>
        )}
        {running && noPartialOutput && (
          <span
            className="font-normal text-base-content/50"
            title="codex exec prints its answer only when it is complete; there is no partial output to show."
            data-testid="live-no-partial"
          >
            codex sends no partial output; the answer arrives whole
          </span>
        )}
      </div>
      <pre
        ref={ref}
        className="max-h-48 min-h-10 overflow-y-auto rounded bg-neutral p-2 font-mono text-xs whitespace-pre-wrap text-neutral-content"
        data-testid="live-output"
      >
        {chunks.length === 0
          ? running
            ? 'waiting for output…'
            : ''
          : chunks.map((c, i) =>
              c.kind === 'status' ? (
                <span key={i} className="block text-neutral-content/60 italic">
                  [{c.text.trim()}]
                </span>
              ) : (
                <span key={i}>{c.text}</span>
              ),
            )}
      </pre>
    </div>
  )
}


/** Short messages from the runs store ("already running", ...), bottom left; shown on every tab. */
export function RunsToast() {
  const toast = useRunsStore((s) => s.toast)
  const dismiss = useRunsStore((s) => s.dismissToast)
  if (!toast) return null
  return (
    <div className="toast z-50 toast-start toast-bottom">
      <div
        role={toast.kind === 'error' ? 'alert' : 'status'}
        className={`alert alert-soft py-2 text-sm ${toast.kind === 'error' ? 'alert-warning' : 'alert-info'}`}
        data-testid="runs-toast"
      >
        <span className="max-w-md">{toast.text}</span>
        <button type="button" className="btn btn-ghost btn-xs" onClick={dismiss} aria-label="Dismiss">
          ✕
        </button>
      </div>
    </div>
  )
}
