// DraftDialog: "Draft from spec…" for one paragraph node (phase 7).
//
//   ┌ Draft from spec: p-7 ─────────────────────────────────────────────┐
//   │ says: Pages in W_f are installed before the vCPU starts ...        │
//   │ Runner [claude ▾] model [sonnet] thinking [low ▾]   [Generate]     │
//   │ ── live output while it runs ──────────────────────────────────── │
//   │ ┌ draft (editable) ─────────────────────────────────────────────┐ │
//   │ │ \textsc{Prefetch} reads the pages of $W_f$ ...                 │ │
//   │ └────────────────────────────────────────────────────────────────┘ │
//   │ Notes: ...                                                          │
//   │ [Replace paragraph] [Insert at cursor] [Copy]            [Close]   │
//   │ Earlier drafts of p-7: 10:42 done · 10:31 failed                   │
//   └─────────────────────────────────────────────────────────────────────┘
//
// The model call writes nothing. "Replace paragraph" rewrites the text under the node's
// marker (the node comes up stale, to be re-read); "Insert at cursor" writes a new marker
// and the text at the editor's cursor line and links the node to it. Both go through
// POST /api/drafts/{id}/apply with the author's edited text. The draft run is an ordinary
// run: the live output comes from the runs store, Cancel works from the run list.
import { useEffect, useState } from 'react'
import { formatTime } from '../findings/format'
import RunnerControls from '../findings/RunnerControls'
import { isPending, useDraftStore } from '../state/drafts'
import { useRunsStore } from '../state/runs'
import { useStore } from '../state/store'

export default function DraftDialog() {
  const target = useDraftStore((s) => s.target)
  const detail = useDraftStore((s) => s.detail)
  const text = useDraftStore((s) => s.text)
  const setText = useDraftStore((s) => s.setText)
  const history = useDraftStore((s) => s.history)
  const starting = useDraftStore((s) => s.starting)
  const applying = useDraftStore((s) => s.applying)
  const error = useDraftStore((s) => s.error)
  const start = useDraftStore((s) => s.start)
  const apply = useDraftStore((s) => s.apply)
  const show = useDraftStore((s) => s.show)
  const close = useDraftStore((s) => s.close)

  const runners = useRunsStore((s) => s.runners)
  const loadRunners = useRunsStore((s) => s.loadRunners)
  const levelDefaults = runners?.defaults.paragraph
  const [runner, setRunner] = useState('')
  const [model, setModel] = useState('')
  const [thinking, setThinking] = useState('')
  useEffect(() => {
    if (!runners) void loadRunners()
  }, [runners, loadRunners])

  const activeFile = useStore((s) => s.activeFile?.path ?? null)
  const cursorLine = useStore((s) => s.cursorLine)
  const run = detail?.run ?? null
  const live = useRunsStore((s) => (run?.id ? s.live[run.id] : undefined))
  const draft = detail?.draft ?? null
  const pending = isPending(draft)
  const [copied, setCopied] = useState(false)

  if (!target) return null
  // Every draft of this node except the one on screen. When the dialog is reopened nothing is
  // on screen yet, so a single earlier draft must be listed too (to pick it up again).
  const earlier = history.filter((d) => d.id !== draft?.id)
  const canReplace = target.anchored && draft?.status === 'done' && !draft.applied_at
  const canInsert = !target.anchored && draft?.status === 'done' && !draft.applied_at && !!activeFile

  const generate = () => {
    void start({ runner: runner || null, model: model || null, thinking: thinking || null })
  }
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
    <div className="modal modal-open" role="dialog" aria-modal="true" aria-labelledby="draft-title">
      <div className="modal-box flex max-h-[92vh] w-11/12 max-w-4xl flex-col gap-3" data-native-undo data-testid="draft-dialog">
        <div className="flex items-start gap-3">
          <div className="min-w-0 flex-1">
            <h3 id="draft-title" className="text-lg font-bold">
              Draft from spec: <span className="font-mono">{target.nodeId}</span>
            </h3>
            <p className="truncate text-sm text-base-content/70" title={target.says}>
              says: {target.says || <i>(no local message yet; write one first for a useful draft)</i>}
            </p>
          </div>
          <button type="button" className="btn btn-ghost btn-sm" onClick={close} aria-label="Close" data-testid="draft-close">
            ✕
          </button>
        </div>

        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="w-16 font-medium">Runner</span>
          <RunnerControls
            runners={runners}
            levelDefaults={levelDefaults}
            defaultModel={levelDefaults?.model ?? null}
            defaultThinking={levelDefaults?.thinking ?? null}
            runner={runner}
            onRunner={setRunner}
            model={model}
            onModel={setModel}
            thinking={thinking}
            onThinking={setThinking}
            testIdPrefix="draft-"
            listId="draft-model-suggestions"
          />
          <button type="button" className="btn btn-sm btn-primary ml-auto" onClick={generate} disabled={starting || pending} data-testid="draft-generate">
            {(starting || pending) && <span className="loading loading-xs loading-spinner" />}
            {draft ? 'Generate again' : 'Generate'}
          </button>
        </div>
        <p className="text-xs text-base-content/60">
          The model gets the spec, this paragraph's local message, function and linked claims, the section's LaTeX
          for voice, and the paper's labels and bib keys. It returns one paragraph; nothing is written until you
          choose Replace or Insert below. Its prompt stack is separate from the review passes.
        </p>

        {error && (
          <div role="alert" className="alert text-sm alert-soft alert-error" data-testid="draft-error">
            {error}
          </div>
        )}

        {pending && (
          <div className="rounded border border-base-300 px-3 py-2" data-testid="draft-live">
            <div className="mb-1 flex items-center gap-2 text-xs font-medium text-base-content/70">
              Drafting <span className="loading loading-xs loading-dots" />
              {run?.runner === 'codex' && <span className="font-normal text-base-content/50">codex sends no partial output; the answer arrives whole</span>}
            </div>
            <pre className="max-h-40 overflow-y-auto rounded bg-neutral p-2 font-mono text-xs whitespace-pre-wrap text-neutral-content">
              {live && live.length > 0 ? live.map((c) => c.text).join('') : 'waiting for output…'}
            </pre>
          </div>
        )}

        {draft?.status === 'failed' && (
          <div role="alert" className="alert text-sm alert-soft alert-warning" data-testid="draft-failed">
            The draft failed: {draft.error ?? 'unknown error'}. The raw answer is on run {draft.run_id} in the Findings tab.
          </div>
        )}

        {draft?.status === 'done' && (
          <>
            <label className="flex min-h-0 flex-1 flex-col gap-1">
              <span className="text-xs font-medium text-base-content/70">Draft (edit before inserting)</span>
              <textarea
                className="textarea min-h-40 w-full flex-1 font-mono text-xs leading-5"
                value={text}
                onChange={(e) => setText(e.target.value)}
                readOnly={!!draft.applied_at}
                data-testid="draft-text"
              />
            </label>
            {draft.notes && (
              <p className="text-xs text-base-content/70" data-testid="draft-notes">
                <span className="font-medium">Notes from the model:</span> {draft.notes}
              </p>
            )}
            {draft.applied_at && (
              <p className="text-xs text-success" data-testid="draft-applied">
                Applied ({draft.applied_mode}) to {draft.applied_file}:{draft.applied_line_start}.
              </p>
            )}
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                className="btn btn-sm btn-primary"
                disabled={!canReplace || applying || !text.trim()}
                onClick={() => void apply('replace')}
                title={
                  target.anchored
                    ? 'Replace the paragraph under this node’s marker with the text above (the node turns stale until you re-read it)'
                    : 'This node has no marker in the source yet; use Insert at cursor'
                }
                data-testid="draft-replace"
              >
                {applying && <span className="loading loading-xs loading-spinner" />}
                Replace paragraph
              </button>
              <button
                type="button"
                className="btn btn-sm"
                disabled={!canInsert || applying || !text.trim()}
                onClick={() => activeFile && void apply('insert', { file: activeFile, line: cursorLine })}
                title={
                  target.anchored
                    ? 'This node already has a marker; use Replace paragraph'
                    : activeFile
                      ? `Insert a % @p ${target.nodeId} marker and the text at ${activeFile}:${cursorLine}, and link the node to it`
                      : 'Open the .tex file and place the cursor where the paragraph goes'
                }
                data-testid="draft-insert"
              >
                Insert at cursor{activeFile && !target.anchored ? ` (${activeFile.split('/').pop()}:${cursorLine})` : ''}
              </button>
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => void copy()} data-testid="draft-copy">
                {copied ? 'Copied' : 'Copy'}
              </button>
            </div>
          </>
        )}

        {earlier.length > 0 && (
          <div className="text-xs text-base-content/60" data-testid="draft-history">
            Earlier drafts of {target.nodeId}:{' '}
            {earlier.map((d) => (
              <button key={d.id} type="button" className="link link-hover mr-2" onClick={() => void show(d.id)} data-testid="draft-history-item">
                {formatTime(d.created_at)} {d.status}
                {d.applied_at ? ' · applied' : ''}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
