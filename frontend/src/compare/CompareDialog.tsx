// CompareDialog: blind A/B comparison of one paragraph against an earlier
// version (phase 5, PLAN.md "Blind A/B compare").
//
// Opened from the outliner row's ⋯ menu ("Compare…") or the editor's
// right-click menu ("Compare with earlier version…"). State lives in
// state/compare.ts.
//
//   Blind A/B compare · p-3 · sections/intro.tex        [Compare | History (2)]  ✕
//   “says” of the paragraph
//   ── form ──────────────────────────────────────────────────────────────
//   Before  [rewrite/rev-1-v1 — first draft ▾]   (snapshots, then recent commits)
//   Runner  [default ▾] [model] [thinking ▾]                         [Start]
//   ── after Start ───────────────────────────────────────────────────────
//   ⟳ running …                                                     [Cancel]
//   Version B reads better
//   reason ...
//   [Reveal which is which]
//   ── after Reveal ──────────────────────────────────────────────────────
//   Version A · before (snapshot rewrite/rev-1-v1) | Version B · after (working tree) ✓
//   the two texts exactly as the model saw them
//   "The current text won."
//
// Blindness: before Reveal the dialog shows only the verdict and the reason,
// not the texts, because showing "Version A: <text>" would let the reader
// recognise which one is the old one. The API returns both texts but not the
// A/B mapping until it is asked with reveal=true, so the texts are placed
// under A and B only after Reveal.
import { useEffect, useMemo, useState } from 'react'
import type { Comparison, CompareRef, CompareRequest } from '../api/client'
import { formatDate, formatTime } from '../findings/format'
import RunnerControls from '../findings/RunnerControls'
import { isPending, useCompareStore } from '../state/compare'
import { useGitStore } from '../state/git'
import { useRunsStore } from '../state/runs'

/** The pass whose front matter gives the default model / thinking. */
const AB_PASS = 'ab-compare'

/** "snapshot rewrite/rev-1-v1" or "commit 8517de8". */
function describeRef(ref: CompareRef): string {
  return ref.kind === 'snapshot' ? `snapshot ${ref.ref}` : `commit ${ref.ref.slice(0, 7)}`
}

/** Headline for a verdict. */
function verdictText(verdict: Comparison['verdict']): string {
  if (verdict === 'A') return 'Version A reads better'
  if (verdict === 'B') return 'Version B reads better'
  if (verdict === 'tie') return 'Tie: neither version reads better'
  return ''
}

export default function CompareDialog() {
  const target = useCompareStore((s) => s.target)
  const view = useCompareStore((s) => s.view)
  const setView = useCompareStore((s) => s.setView)
  const close = useCompareStore((s) => s.close)
  const comparisons = useCompareStore((s) => s.comparisons)
  const detail = useCompareStore((s) => s.detail)
  const currentId = useCompareStore((s) => s.currentId)

  // The "before" picker needs the snapshots and the recent commits.
  useEffect(() => {
    void useGitStore.getState().loadHistory()
  }, [])

  // Escape closes the dialog.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [close])

  if (!target) return null
  // The header names the comparison on screen (a History item may be about
  // another paragraph), else the paragraph the dialog was opened for.
  const shown = currentId && detail ? detail.comparison : null
  const nodeLabel = shown ? (shown.node_id ?? `lines ${shown.line_start}–${shown.line_end}`) : (target.node_id ?? `lines ${target.line_start}–${target.line_end}`)
  const file = shown ? shown.file : target.file

  return (
    <div className="modal modal-open" role="dialog" aria-modal="true" aria-labelledby="compare-title">
      <div className="modal-box flex max-h-[92vh] w-11/12 max-w-5xl flex-col gap-3" data-native-undo data-testid="compare-dialog">
        <div className="flex flex-wrap items-center gap-2">
          <h3 id="compare-title" className="text-lg font-bold">
            Blind A/B compare
          </h3>
          <span className="badge font-mono badge-ghost" data-testid="compare-node">
            {nodeLabel}
          </span>
          <span className="font-mono text-sm text-base-content/60" data-testid="compare-file">
            {file}
          </span>
          <div role="tablist" className="tabs-box tabs ml-auto tabs-sm">
            <button type="button" role="tab" aria-selected={view === 'compare'} className={`tab ${view === 'compare' ? 'tab-active' : ''}`} onClick={() => setView('compare')} data-testid="compare-tab-compare">
              Compare
            </button>
            <button type="button" role="tab" aria-selected={view === 'history'} className={`tab ${view === 'history' ? 'tab-active' : ''}`} onClick={() => setView('history')} data-testid="compare-tab-history">
              History ({comparisons.length})
            </button>
          </div>
          <button type="button" className="btn btn-square btn-ghost btn-sm" onClick={close} aria-label="Close" data-testid="compare-close">
            ✕
          </button>
        </div>
        {!shown && target.says && (
          <p className="-mt-2 text-sm text-base-content/70 italic" data-testid="compare-says">
            “{target.says}”
          </p>
        )}
        <div className="min-h-0 flex-1 overflow-y-auto">{view === 'history' ? <History /> : currentId ? <Result /> : <StartForm />}</div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// The form: before, runner, Start
// ---------------------------------------------------------------------------

function StartForm() {
  const target = useCompareStore((s) => s.target)
  const start = useCompareStore((s) => s.start)
  const starting = useCompareStore((s) => s.starting)
  const error = useCompareStore((s) => s.error)
  const snapshots = useGitStore((s) => s.snapshots)
  const commits = useGitStore((s) => s.commits)
  const runners = useRunsStore((s) => s.runners)
  const pass = useRunsStore((s) => s.passes.find((p) => p.name === AB_PASS))

  const [beforeChoice, setBeforeChoice] = useState('')
  const [runner, setRunner] = useState('')
  const [model, setModel] = useState('')
  const [thinking, setThinking] = useState('')

  // Options: snapshots (newest first), then recent commits. Values are
  // "snapshot:<tag>" and "sha:<full sha>".
  const options = useMemo(
    () => [
      ...snapshots.map((s) => ({ value: `snapshot:${s.tag}`, group: 'snapshot' as const })),
      ...commits.map((c) => ({ value: `sha:${c.sha}`, group: 'commit' as const })),
    ],
    [snapshots, commits],
  )
  // Default: the newest snapshot, else the newest commit.
  const choice = options.some((o) => o.value === beforeChoice) ? beforeChoice : (options[0]?.value ?? '')
  const before: CompareRef | null = choice
    ? { kind: choice.startsWith('snapshot:') ? 'snapshot' : 'sha', ref: choice.slice(choice.indexOf(':') + 1) }
    : null

  const levelDefaults = runners?.defaults[pass?.level ?? 'paragraph']
  const runnerInfo = runners?.runners.find((r) => r.name === (runner || levelDefaults?.runner))
  const runnerUnavailable = !!runnerInfo && !runnerInfo.available

  if (!target) return null

  const submit = () => {
    if (!before) return
    const request: CompareRequest = { file: target.file, before }
    if (target.node_id) request.node_id = target.node_id
    else if (target.line_start && target.line_end) {
      request.line_start = target.line_start
      request.line_end = target.line_end
    }
    if (runner) request.runner = runner as CompareRequest['runner']
    if (model.trim()) request.model = model.trim()
    if (thinking) request.thinking = thinking
    void start(request)
  }

  return (
    <div className="flex flex-col gap-3 text-sm" data-testid="compare-form">
      <div className="flex flex-wrap items-center gap-2">
        <label className="w-16 font-medium" htmlFor="compare-before">
          Before
        </label>
        <select
          id="compare-before"
          className="select min-w-0 flex-1 select-sm"
          value={choice}
          onChange={(e) => setBeforeChoice(e.target.value)}
          disabled={options.length === 0}
          data-testid="compare-before-select"
        >
          {options.length === 0 && <option value="">no snapshots or commits yet</option>}
          {snapshots.length > 0 && (
            <optgroup label="Snapshots (flag major revision)">
              {snapshots.map((s) => (
                <option key={s.tag} value={`snapshot:${s.tag}`}>
                  {s.tag} — {s.note} ({formatDate(s.created_at)})
                </option>
              ))}
            </optgroup>
          )}
          {commits.length > 0 && (
            <optgroup label="Recent commits">
              {commits.map((c) => (
                <option key={c.sha} value={`sha:${c.sha}`}>
                  {c.sha.slice(0, 7)} {c.subject}
                </option>
              ))}
            </optgroup>
          )}
        </select>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <span className="w-16 font-medium">Runner</span>
        <RunnerControls
          runners={runners}
          levelDefaults={levelDefaults}
          defaultModel={pass?.model ?? levelDefaults?.model ?? null}
          defaultThinking={pass?.thinking ?? levelDefaults?.thinking ?? null}
          runner={runner}
          onRunner={setRunner}
          model={model}
          onModel={setModel}
          thinking={thinking}
          onThinking={setThinking}
          testIdPrefix="compare-"
          listId="compare-model-suggestions"
        />
      </div>
      <p className="text-xs text-base-content/60">
        The paragraph at “before” and the paragraph in the working tree go to a fresh model call in random order,
        labelled only A and B, with just the relevant spec entries. The model is not told that either one is an edit.
        You see the verdict first; which version was which stays hidden until you reveal it.
      </p>
      {error && (
        <div role="alert" className="alert py-2 text-sm alert-soft alert-error" data-testid="compare-error">
          {error}
        </div>
      )}
      <div className="flex items-center justify-end gap-2">
        {runnerUnavailable && <span className="text-xs text-error">{runnerInfo?.name} is not installed</span>}
        <button
          type="button"
          className="btn btn-sm btn-primary"
          onClick={submit}
          disabled={!before || starting || runnerUnavailable}
          data-testid="compare-start"
        >
          {starting && <span className="loading loading-xs loading-spinner" />}
          Start
        </button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// The comparison on screen: status, verdict, reveal
// ---------------------------------------------------------------------------

function Result() {
  const detail = useCompareStore((s) => s.detail)
  const revealed = useCompareStore((s) => s.revealed)
  const error = useCompareStore((s) => s.error)
  const cancel = useCompareStore((s) => s.cancel)
  const reveal = useCompareStore((s) => s.reveal)
  const newComparison = useCompareStore((s) => s.newComparison)
  const [cancelling, setCancelling] = useState(false)

  if (!detail) return <span className="loading loading-spinner" />
  const c = detail.comparison
  const pending = isPending(c)
  const mapping = revealed ? c.mapping : null

  return (
    <div className="flex flex-col gap-3" data-testid="compare-result" data-comparison-id={c.id}>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span
          className={`badge badge-sm ${c.status === 'done' ? 'badge-success' : c.status === 'failed' ? 'badge-error' : 'badge-info'}`}
          data-testid="compare-status"
          data-status={c.status}
        >
          {pending && <span className="loading loading-xs loading-spinner" />}
          {c.status}
        </span>
        <span className="text-base-content/70">
          {c.node_id ?? `lines ${c.line_start}–${c.line_end}`} now vs {describeRef(c.before)}
        </span>
        <span className="text-xs text-base-content/50">started {formatTime(c.created_at)}</span>
        {detail.run && (
          <span className="text-xs text-base-content/50" title="The run that carries the model call (also listed in the Findings tab)">
            run {detail.run.id}
            {detail.run.model ? ` · ${detail.run.model}` : ''}
            {detail.run.cost_usd != null ? ` · $${detail.run.cost_usd.toFixed(4)}` : ''}
          </span>
        )}
        {pending && (
          <button
            type="button"
            className="btn ml-auto btn-outline btn-xs btn-error"
            disabled={!c.run_id || cancelling}
            onClick={() => {
              setCancelling(true)
              void cancel().finally(() => setCancelling(false))
            }}
            data-testid="compare-cancel"
          >
            Cancel
          </button>
        )}
      </div>

      {pending && <p className="text-sm text-base-content/60">The model is reading both versions… A live run can take a minute or two.</p>}

      {c.status === 'failed' && (
        <div role="alert" className="alert py-2 text-sm alert-soft alert-error" data-testid="compare-failed">
          {c.error === 'cancelled' ? 'Cancelled.' : `The comparison failed: ${c.error ?? 'unknown error'}`}
        </div>
      )}

      {c.status === 'done' && (
        <div className="rounded-box border border-base-300 bg-base-200/40 p-3">
          <div className="text-lg font-semibold" data-testid="compare-verdict" data-verdict={c.verdict ?? ''}>
            {verdictText(c.verdict)}
          </div>
          {c.reason && (
            <p className="mt-1 text-sm whitespace-pre-wrap" data-testid="compare-reason">
              {c.reason}
            </p>
          )}
        </div>
      )}

      {c.status === 'done' && !mapping && (
        <div className="flex items-center gap-3">
          <button type="button" className="btn btn-sm btn-secondary" onClick={() => void reveal()} data-testid="compare-reveal">
            Reveal which is which
          </button>
          <span className="text-xs text-base-content/60">
            Read the verdict first. Revealing shows both texts and which one is the earlier version.
          </span>
        </div>
      )}

      {c.status === 'done' && mapping && <Revealed />}

      {error && (
        <div role="alert" className="alert py-2 text-sm alert-soft alert-error" data-testid="compare-error">
          {error}
        </div>
      )}

      <div className="flex justify-end">
        <button type="button" className="btn btn-ghost btn-sm" onClick={newComparison} data-testid="compare-new">
          New comparison
        </button>
      </div>
    </div>
  )
}

/** Both texts under their letters, with which one is before / after and which won. */
function Revealed() {
  const detail = useCompareStore((s) => s.detail)
  if (!detail?.comparison.mapping) return null
  const c = detail.comparison
  const mapping = c.mapping!
  const textOf = (side: 'before' | 'after') => (side === 'before' ? detail.before_text : detail.after_text)
  const labelOf = (side: 'before' | 'after') => (side === 'before' ? `before (${describeRef(c.before)})` : 'after (working tree)')
  const winner = c.verdict === 'tie' || !c.verdict ? 'tie' : mapping[c.verdict]
  const outcome =
    winner === 'after'
      ? { text: 'The current text won: the model preferred the working-tree version.', className: 'alert-success' }
      : winner === 'before'
        ? { text: `The earlier version won: the model preferred the paragraph at ${describeRef(c.before)}.`, className: 'alert-warning' }
        : { text: 'Tie: the model found neither version better.', className: 'alert-info' }

  return (
    <div className="flex flex-col gap-2" data-testid="compare-revealed">
      <div role="status" className={`alert py-2 text-sm alert-soft ${outcome.className}`} data-testid="compare-outcome" data-winner={winner}>
        {outcome.text}
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        {(['A', 'B'] as const).map((letter) => {
          const side = mapping[letter]
          const won = c.verdict === letter
          return (
            <div
              key={letter}
              className={`flex flex-col gap-1 rounded-box border p-3 ${won ? 'border-success bg-success/5' : 'border-base-300'}`}
              data-testid="compare-version"
              data-letter={letter}
              data-side={side}
            >
              <div className="flex items-center gap-2 text-sm">
                <span className="font-semibold">Version {letter}</span>
                <span className={`badge badge-sm ${side === 'after' ? 'badge-primary' : 'badge-ghost'}`} data-testid="compare-version-label">
                  {labelOf(side)}
                </span>
                {won && <span className="badge badge-sm badge-success">preferred</span>}
              </div>
              <pre className="max-h-72 overflow-auto rounded bg-base-200 p-2 font-mono text-xs whitespace-pre-wrap" data-testid="compare-version-text">
                {textOf(side)}
              </pre>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------

function History() {
  const comparisons = useCompareStore((s) => s.comparisons)
  const historyError = useCompareStore((s) => s.historyError)
  const show = useCompareStore((s) => s.show)
  if (historyError) return <p className="text-sm text-error">Could not load past comparisons: {historyError}</p>
  if (comparisons.length === 0) return <p className="text-sm text-base-content/50">No comparisons yet.</p>
  return (
    <table className="table table-sm" data-testid="compare-history">
      <thead>
        <tr>
          <th>Verdict</th>
          <th>Paragraph</th>
          <th>Before</th>
          <th>When</th>
        </tr>
      </thead>
      <tbody>
        {comparisons.map((c) => (
          <tr key={c.id} className="cursor-pointer hover:bg-base-200/60" onClick={() => void show(c.id)} data-testid="compare-history-row" data-comparison-id={c.id}>
            <td>
              {c.status === 'done' ? (
                <span className="font-medium">{c.verdict === 'tie' ? 'tie' : `${c.verdict} better`}</span>
              ) : (
                <span className={`badge badge-xs ${c.status === 'failed' ? 'badge-error' : 'badge-info'}`}>{c.status}</span>
              )}
            </td>
            <td className="font-mono text-xs">
              {c.node_id ?? `lines ${c.line_start}–${c.line_end}`} <span className="text-base-content/50">{c.file}</span>
            </td>
            <td className="font-mono text-xs">{describeRef(c.before)}</td>
            <td className="text-xs text-base-content/60">{formatDate(c.created_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
