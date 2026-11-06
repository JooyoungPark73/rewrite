// RevisionDialog: "Rewrite…" on a finding card (an author comment or a model finding).
//
//   ┌ Rewrite for finding … ─────────────────────────────────────────────┐
//   │ Comment: Too verbose; say it in one clause.                        │
//   │ Rewrite (•) the quoted span  ( ) the whole paragraph               │
//   │ Runner [claude ▾] model [sonnet] thinking [low ▾]     [Propose]    │
//   │ ── diff: words removed struck, words added green ───────────────── │
//   │ ┌ proposed text (editable) ─────────────────────────────────────┐  │
//   │ Notes: …                                                            │
//   │ [Approve: write as \aigen{…}]  [Copy]                     [Close]  │
//   └─────────────────────────────────────────────────────────────────────┘
//
// Approve writes the (edited) text in place of the span wrapped in \aigen{...} per
// paragraph, with the original kept on a "% aigen-was:" comment line above; main.tex gets
// the macro if it lacks it; the finding is marked addressed; the paragraph turns stale.
// An empty text drops the span. The run itself is an ordinary run (cancel from the run list).
//
// 2026-11-05: the runner / model / thinking pickers default to config.toml's
// `[models] rewrite` (codex, gpt-6-astra, medium unless the paper sets another), and the
// last picks are remembered in this browser (localStorage), so the next "Rewrite…" opens
// with them. Picking "default (...)" again forgets that pick.
//
// 2026-11-06: a comment across paragraphs rewrites all of them. "The whole paragraph"
// becomes "the N paragraphs it spans" (counted from the skeleton, as the backend does). The
// `% @p` lines between the paragraphs go to the model and must come back unchanged; on
// Approve each changed paragraph gets its own % aigen-was: line and \aigen{...} under its
// marker, and a paragraph the rewrite left as it was stays untouched.
import { useEffect, useMemo, useState } from 'react'
import type { Finding, Revision, RevisionSpan, SkeletonParagraph } from '../api/client'
import { formatTime } from '../findings/format'
import RunnerControls from '../findings/RunnerControls'
import { isPending, useReviseStore } from '../state/revise'
import { useRunsStore } from '../state/runs'
import { useSpecStore } from '../state/spec'
import { wordDiff } from './wordDiff'

/** A `% @p <id>` line (the same rule as the backend's skeleton.MARKER_RE). */
const MARKER_LINE = /^[ \t]*%+[ \t]*@p[ \t]+\S+[ \t]*$/m

/**
 * The paragraphs a finding's lines touch, in order: those whose text lines overlap the
 * finding's (app/skeleton.py `paragraphs_in`), else the one at its first line.
 */
function paragraphsSpanned(paragraphs: SkeletonParagraph[], finding: Finding): SkeletonParagraph[] {
  const a = finding.anchor_line_start
  const b = finding.anchor_line_end
  const inFile = paragraphs.filter((p) => p.file === finding.file)
  const overlapping = inFile.filter((p) => p.line_start <= b && p.line_end >= a)
  return overlapping.length > 0 ? overlapping : inFile.filter((p) => p.line_start <= a && a <= p.line_end)
}

/** How the diff names what a proposal replaces. */
function spanWords(revision: Revision): string {
  const across = MARKER_LINE.test(revision.original)
  if (revision.span === 'quote') return across ? 'the quoted span, across paragraphs' : 'the quoted span'
  return across || revision.original.trim().includes('\n\n') ? 'the whole paragraphs' : 'the whole paragraph'
}

/** The runner / model / thinking picked last time ('' = the default). */
interface Picks {
  runner: string
  model: string
  thinking: string
}

const PICKS_KEY = 'rewrite.revise.picks'

function loadPicks(): Picks {
  const none = { runner: '', model: '', thinking: '' }
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(PICKS_KEY) ?? 'null')
    if (!saved || typeof saved !== 'object') return none
    const text = (key: keyof Picks) => {
      const value = (saved as Record<string, unknown>)[key]
      return typeof value === 'string' ? value : ''
    }
    return { runner: text('runner'), model: text('model'), thinking: text('thinking') }
  } catch {
    // Storage blocked (private window) or not JSON: start from the defaults.
    return none
  }
}

function savePicks(picks: Picks) {
  try {
    localStorage.setItem(PICKS_KEY, JSON.stringify(picks))
  } catch {
    // Storage blocked: the picks last until the dialog closes, as before.
  }
}

export default function RevisionDialog() {
  const finding = useReviseStore((s) => s.finding)
  const detail = useReviseStore((s) => s.detail)
  const text = useReviseStore((s) => s.text)
  const setText = useReviseStore((s) => s.setText)
  const history = useReviseStore((s) => s.history)
  const starting = useReviseStore((s) => s.starting)
  const applying = useReviseStore((s) => s.applying)
  const error = useReviseStore((s) => s.error)
  const start = useReviseStore((s) => s.start)
  const approve = useReviseStore((s) => s.approve)
  const show = useReviseStore((s) => s.show)
  const close = useReviseStore((s) => s.close)

  const runners = useRunsStore((s) => s.runners)
  const loadRunners = useRunsStore((s) => s.loadRunners)
  // `rewrite` is config.toml's `[models] rewrite`; an older backend only has the levels.
  const levelDefaults = runners?.defaults.rewrite ?? runners?.defaults.sentence
  const [span, setSpan] = useState<RevisionSpan>('quote')
  const [picks, setPicks] = useState<Picks>(loadPicks)
  const { runner, model, thinking } = picks
  const pick = (change: Partial<Picks>) => {
    const next = { ...picks, ...change }
    setPicks(next)
    savePicks(next)
  }
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!runners) void loadRunners()
  }, [runners, loadRunners])

  // The paragraphs "the whole paragraph(s)" would cover, from a fresh skeleton.
  const skeleton = useSpecStore((s) => s.skeleton)
  const findingId = finding?.id
  useEffect(() => {
    if (findingId) void useSpecStore.getState().refreshSkeleton()
  }, [findingId])
  const spanned = useMemo(() => (finding && skeleton ? paragraphsSpanned(skeleton.paragraphs, finding) : []), [finding, skeleton])

  const revision = detail?.revision ?? null
  const run = detail?.run ?? null
  const live = useRunsStore((s) => (run?.id ? s.live[run.id] : undefined))
  const pending = isPending(revision)
  const diff = useMemo(() => (revision?.status === 'done' ? wordDiff(revision.original, text) : []), [revision, text])

  if (!finding) return null
  // Every proposal for this finding except the one on screen (a reopened dialog shows none
  // yet, so a single earlier proposal must be listed too).
  const earlier = history.filter((r) => r.id !== revision?.id)
  const canApprove = revision?.status === 'done' && !revision.applied_at && finding.state !== 'dismissed'
  const across = !!revision && MARKER_LINE.test(revision.original)
  const propose = () => void start({ span, runner: runner || null, model: model || null, thinking: thinking || null })
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      setCopied(false)
    }
  }

  return (
    <div className="modal modal-open" role="dialog" aria-modal="true" aria-labelledby="revision-title">
      <div className="modal-box flex max-h-[92vh] w-11/12 max-w-4xl flex-col gap-3" data-native-undo data-testid="revision-dialog">
        <div className="flex items-start gap-3">
          <div className="min-w-0 flex-1">
            <h3 id="revision-title" className="text-lg font-bold">
              Rewrite for {finding.category === 'comment' ? 'your comment' : `the ${finding.category} finding`} at{' '}
              <span className="font-mono text-base">
                {finding.file}:{finding.anchor_line_start}
              </span>
            </h3>
            <p className="text-sm" data-testid="revision-comment">
              <span className="font-medium">{finding.category === 'comment' ? 'Comment' : 'Problem'}:</span> {finding.problem}
              {finding.why && <span className="text-base-content/60"> — {finding.why}</span>}
            </p>
            <p className="line-clamp-2 text-xs text-base-content/60" title={finding.quote}>
              “{finding.quote}”
            </p>
          </div>
          <button type="button" className="btn btn-ghost btn-sm" onClick={close} aria-label="Close" data-testid="revision-close">
            ✕
          </button>
        </div>

        <div className="flex flex-wrap items-center gap-3 text-sm">
          <span className="w-16 font-medium">Rewrite</span>
          <label className="flex cursor-pointer items-center gap-1">
            <input type="radio" className="radio radio-xs" name="revision-span" checked={span === 'quote'} onChange={() => setSpan('quote')} disabled={pending} data-testid="span-quote" />
            the quoted span
          </label>
          <label className="flex cursor-pointer items-center gap-1">
            <input type="radio" className="radio radio-xs" name="revision-span" checked={span === 'paragraph'} onChange={() => setSpan('paragraph')} disabled={pending} data-testid="span-paragraph" />
            <span
              title={spanned.length > 1 ? `Paragraphs ${spanned.map((p) => p.marker_id ?? '(not adopted)').join(', ')}` : undefined}
              data-testid="span-paragraph-label"
            >
              {spanned.length > 1 ? `the ${spanned.length} paragraphs it spans` : 'the whole paragraph'}
            </span>
          </label>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="w-16 font-medium">Runner</span>
          <RunnerControls
            runners={runners}
            levelDefaults={levelDefaults}
            // The default model belongs to the default runner; another runner uses its CLI's own.
            defaultModel={!runner || runner === levelDefaults?.runner ? (levelDefaults?.model ?? null) : null}
            defaultThinking={levelDefaults?.thinking ?? null}
            runner={runner}
            onRunner={(value) => pick({ runner: value })}
            model={model}
            onModel={(value) => pick({ model: value })}
            thinking={thinking}
            onThinking={(value) => pick({ thinking: value })}
            testIdPrefix="revision-"
            listId="revision-model-suggestions"
          />
          <button type="button" className="btn btn-sm btn-primary ml-auto" onClick={propose} disabled={starting || pending} data-testid="revision-propose">
            {(starting || pending) && <span className="loading loading-xs loading-spinner" />}
            {revision ? 'Propose again' : 'Propose a rewrite'}
          </button>
        </div>
        <p className="text-xs text-base-content/60">
          The model gets the comment, the text as it reads now, the paragraph and section around it, the paragraph's spec
          entry and the field's standard of evidence, and must change only what the comment asks. Nothing is written
          until you approve; the approved text goes in as <code>\aigen{'{'}…{'}'}</code> with the original kept on a{' '}
          <code>% aigen-was:</code> line, for the co-authors to accept by removing the wrapper. Across paragraphs, the{' '}
          <code>% @p</code> lines between them stay as they are and each changed paragraph gets its own wrapper.
        </p>

        {error && (
          <div role="alert" className="alert text-sm alert-soft alert-error" data-testid="revision-error">
            {error}
          </div>
        )}
        {pending && (
          <div className="rounded border border-base-300 px-3 py-2" data-testid="revision-live">
            <div className="mb-1 flex items-center gap-2 text-xs font-medium text-base-content/70">
              Proposing <span className="loading loading-xs loading-dots" />
              {run?.runner === 'codex' && <span className="font-normal text-base-content/50">codex sends no partial output; the answer arrives whole</span>}
            </div>
            <pre className="max-h-32 overflow-y-auto rounded bg-neutral p-2 font-mono text-xs whitespace-pre-wrap text-neutral-content">
              {live && live.length > 0 ? live.map((c) => c.text).join('') : 'waiting for output…'}
            </pre>
          </div>
        )}
        {revision?.status === 'failed' && (
          <div role="alert" className="alert text-sm alert-soft alert-warning" data-testid="revision-failed">
            The rewrite failed: {revision.error ?? 'unknown error'}. The raw answer is on run {revision.run_id} in the Findings tab.
          </div>
        )}

        {revision?.status === 'done' && (
          <>
            <div className="min-h-0 overflow-y-auto rounded border border-base-300 px-3 py-2 text-sm leading-6" data-testid="revision-diff">
              <div className="mb-1 text-xs font-medium text-base-content/70">Diff against the current text ({spanWords(revision)})</div>
              {text.trim() === '' ? (
                <p>
                  <del className="rounded bg-error/15 px-0.5 text-error" data-testid="diff-removed">{revision.original}</del>{' '}
                  <span className="text-xs text-base-content/60">(the span is dropped)</span>
                </p>
              ) : (
                <p className="font-mono text-xs whitespace-pre-wrap">
                  {diff.map((run, i) =>
                    run.kind === 'equal' ? (
                      <span key={i}>{run.text} </span>
                    ) : run.kind === 'removed' ? (
                      <del key={i} className="rounded bg-error/15 px-0.5 text-error" data-testid="diff-removed">
                        {run.text}{' '}
                      </del>
                    ) : (
                      <ins key={i} className="rounded bg-success/15 px-0.5 text-success no-underline" data-testid="diff-added">
                        {run.text}{' '}
                      </ins>
                    ),
                  )}
                </p>
              )}
            </div>
            <label className="flex min-h-0 flex-col gap-1">
              <span className="text-xs font-medium text-base-content/70">
                {across
                  ? 'Proposed text (edit before approving; keep the % @p lines as they are, with text under each)'
                  : 'Proposed text (edit before approving; empty = drop the span)'}
              </span>
              <textarea
                className="textarea min-h-28 w-full font-mono text-xs leading-5"
                value={text}
                onChange={(e) => setText(e.target.value)}
                readOnly={!!revision.applied_at}
                data-testid="revision-text"
              />
            </label>
            {revision.notes && (
              <p className="text-xs text-base-content/70" data-testid="revision-notes">
                <span className="font-medium">Notes from the model:</span> {revision.notes}
              </p>
            )}
            {revision.applied_at && (
              <p className="text-xs text-success" data-testid="revision-applied">
                Approved and written to {revision.applied_file}:{revision.applied_line_start}.
              </p>
            )}
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                className="btn btn-sm btn-primary"
                disabled={!canApprove || applying}
                onClick={() => void approve()}
                title={text.trim() ? 'Write the text above as \\aigen{…}, keep the original on a % aigen-was: line, mark the finding addressed' : 'Drop the span: keep only the % aigen-was: line'}
                data-testid="revision-approve"
              >
                {applying && <span className="loading loading-xs loading-spinner" />}
                {text.trim() ? 'Approve: write as \\aigen{…}' : 'Approve: drop the span'}
              </button>
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => void copy()} data-testid="revision-copy">
                {copied ? 'Copied' : 'Copy'}
              </button>
            </div>
          </>
        )}

        {earlier.length > 0 && (
          <div className="text-xs text-base-content/60" data-testid="revision-history">
            Earlier proposals:{' '}
            {earlier.map((r) => (
              <button key={r.id} type="button" className="link link-hover mr-2" onClick={() => void show(r.id)} data-testid="revision-history-item">
                {formatTime(r.created_at)} {r.status}
                {r.applied_at ? ' · approved' : ''}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
