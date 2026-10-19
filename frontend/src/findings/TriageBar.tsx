// TriageBar: the triage call for one run, in the run's detail (phase 6).
//
//   [Triage ▾]  Triage: 1 keep · 1 wrong · 1 trivial   [x] hide wrong/trivial   [Dismiss all flagged (2)]
//
// "Triage" opens a small panel (runner / model / thinking, then "Start
// triage"): POST /api/runs/{id}/triage sends the run's OPEN findings plus the
// scoped text to a fresh model call that marks each one keep, wrong or
// trivial. This is the one place where earlier findings enter a prompt
// (PLAN.md "Clean context"). While it runs the status line says so; when
// `triage.finished` arrives every card shows its verdict badge (FindingCard).
// Verdicts never change a finding's state by themselves:
//   - "hide wrong/trivial" only hides flagged cards in the Findings tab,
//   - "Dismiss all flagged (n)" dismisses the n open flagged findings
//     (POST /api/findings/dismiss-flagged), with a toast giving the counts.
// Also exports HideFlaggedToggle, reused by the merged view of a group.
import { useState } from 'react'
import { passKind, type Finding, type Run, type TriageRequest } from '../api/client'
import { flaggedCounts, VERDICT_STYLE } from '../state/groups'
import { useRunsStore } from '../state/runs'
import Popover from './Popover'
import RunnerControls from './RunnerControls'

export default function TriageBar({ run, findings }: { run: Run; findings: Finding[] }) {
  const passes = useRunsStore((s) => s.passes)
  const runners = useRunsStore((s) => s.runners)
  const triage = useRunsStore((s) => s.triages[run.id])
  const startTriage = useRunsStore((s) => s.startTriage)
  const dismissFlagged = useRunsStore((s) => s.dismissFlagged)
  const [runner, setRunner] = useState('')
  const [model, setModel] = useState('')
  const [thinking, setThinking] = useState('')
  const [busy, setBusy] = useState(false)

  // Only finished review runs have findings to judge (not a triage or compare run itself).
  const pass = passes.find((p) => p.name === run.pass)
  if (pass && passKind(pass) !== 'review') return null
  if (run.status !== 'done' && run.status !== 'cached') return null

  // Defaults shown in the pickers: the triage pass's front matter, else its level's config.
  const triagePass = passes.find((p) => passKind(p) === 'triage')
  const levelDefaults = runners?.defaults[triagePass?.level ?? run.level]
  const open = findings.filter((f) => f.state === 'open').length
  const active = triage?.status === 'queued' || triage?.status === 'running'
  const anyVerdict = findings.some((f) => f.triage_verdict)
  const flagged = flaggedCounts(findings)

  const start = async (close: () => void) => {
    const request: TriageRequest = {}
    if (runner === 'claude' || runner === 'codex') request.runner = runner
    if (model.trim()) request.model = model.trim()
    if (thinking) request.thinking = thinking
    close()
    setBusy(true)
    await startTriage(run.id, request)
    setBusy(false)
  }

  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-base-300 px-3 py-1.5 text-sm" data-testid="triage-bar">
      <Popover
        label="Triage"
        buttonClass="btn btn-xs"
        testId="triage-button"
        disabled={open === 0 || active || busy}
        title={
          open === 0
            ? 'No open findings to triage'
            : active
              ? 'A triage of this run is running'
              : `Ask a fresh model call which of the ${open} open findings are wrong or trivial`
        }
      >
        {(close) => (
          <>
            <div className="max-w-sm text-xs text-base-content/70">
              Sends the {open} open finding{open === 1 ? '' : 's'} and the scoped text to a fresh call that marks each one
              keep, wrong or trivial. Nothing is dismissed automatically.
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <RunnerControls
                runners={runners}
                levelDefaults={levelDefaults}
                defaultModel={triagePass?.model ?? levelDefaults?.model ?? null}
                defaultThinking={triagePass?.thinking ?? levelDefaults?.thinking ?? null}
                runner={runner}
                onRunner={setRunner}
                model={model}
                onModel={setModel}
                thinking={thinking}
                onThinking={setThinking}
                testIdPrefix="triage-"
                listId="triage-model-suggestions"
              />
            </div>
            <div className="flex justify-end">
              <button type="button" className="btn btn-sm btn-primary" onClick={() => void start(close)} data-testid="triage-start">
                Start triage
              </button>
            </div>
          </>
        )}
      </Popover>

      <span className="text-xs" data-testid="triage-status" data-status={triage?.status ?? 'none'}>
        {active && (
          <span className="flex items-center gap-1 text-base-content/70">
            <span className="loading loading-xs loading-spinner" />
            Triage running: a fresh call is judging {open} open finding{open === 1 ? '' : 's'}…
          </span>
        )}
        {triage?.status === 'done' && (
          <span title="Verdicts of the latest triage call">
            Triage:{' '}
            {(['keep', 'wrong', 'trivial'] as const).map((v, i) => (
              <span key={v}>
                {i > 0 && ' · '}
                <span className={`badge badge-xs ${VERDICT_STYLE[v].badge}`} data-testid={`triage-count-${v}`}>
                  {triage.counts[v] ?? 0} {v}
                </span>
              </span>
            ))}
          </span>
        )}
        {triage?.status === 'failed' && <span className="text-error">Triage failed: {triage.error ?? 'unknown error'}</span>}
        {!triage && !active && open > 0 && <span className="text-base-content/50">not triaged</span>}
      </span>

      {anyVerdict && (
        <span className="ml-auto flex items-center gap-2">
          <HideFlaggedToggle />
          <button
            type="button"
            className="btn btn-outline btn-xs"
            onClick={() => void dismissFlagged(run.id)}
            disabled={flagged.total === 0}
            title={`Dismiss the open findings triage marked wrong (${flagged.wrong}) or trivial (${flagged.trivial})`}
            data-testid="dismiss-flagged"
          >
            Dismiss all flagged ({flagged.total})
          </button>
        </span>
      )}
    </div>
  )
}

/** The "hide wrong/trivial" switch (Findings tab only; the editor column always shows them). */
export function HideFlaggedToggle({ className = '' }: { className?: string }) {
  const hideFlagged = useRunsStore((s) => s.hideFlagged)
  const setHideFlagged = useRunsStore((s) => s.setHideFlagged)
  return (
    <label className={`flex cursor-pointer items-center gap-1 text-xs ${className}`} title="Hide findings triage marked wrong or trivial">
      <input
        type="checkbox"
        className="checkbox checkbox-xs"
        checked={hideFlagged}
        onChange={(e) => setHideFlagged(e.target.checked)}
        data-testid="hide-flagged"
      />
      hide wrong/trivial
    </label>
  )
}
