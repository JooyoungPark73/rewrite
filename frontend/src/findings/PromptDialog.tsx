// PromptDialog: shows the exact prompt of a run, for reading, copying into a
// web chat UI, and pasting that chat's JSON answer back.
//
//   Prompt · paragraph-function · Section: Introduction     [cached]
//   argv: claude -p --output-format stream-json ...   (the prompt goes on stdin)
//   ┌ monospace, scrollable ───────────────────────────────┐
//   │ the assembled prompt                                 │
//   └──────────────────────────────────────────────────────┘
//   [Copy]  Copied!
//   Paste back: ┌ textarea for {"findings": [...]} ┐  [Paste back]
//
// Used two ways:
//   - "Show prompt" in the run launcher: `load` calls POST /api/runs/preview
//     (nothing runs), and `pasteTarget` enables the paste-back box, which sends
//     POST /api/runs/paste; the answer becomes a run with runner "paste" whose
//     findings are validated exactly like a live run's.
//   - "Prompt" in a run's detail: `load` calls GET /api/runs/{id}/prompt (what
//     that run actually sent); no paste-back.
import { useEffect, useRef, useState } from 'react'
import { errorMessage, type Scope } from '../api/client'
import { useRunsStore } from '../state/runs'
import { copyText, shellJoin } from './format'

/** What the dialog shows; preview-only fields are optional. */
export interface PromptInfo {
  prompt: string
  command: string[]
  scope_name?: string
  cache_hit?: boolean
  model?: string | null
  thinking?: string | null
}

export interface PromptDialogProps {
  title: string
  load: () => Promise<PromptInfo>
  /** Enables paste-back for this pass and scope. */
  pasteTarget?: { pass: string; scope: Scope }
  onClose: () => void
}

export default function PromptDialog({ title, load, pasteTarget, onClose }: PromptDialogProps) {
  const paste = useRunsStore((s) => s.paste)
  const [info, setInfo] = useState<PromptInfo | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [copyStatus, setCopyStatus] = useState<string | null>(null)
  const [pasted, setPasted] = useState('')
  const [pasting, setPasting] = useState(false)

  // Load once when the dialog opens. `load` is a new function on every
  // render of the parent, so it is read through a ref instead of being a
  // dependency (which would reload the prompt on every parent render).
  const loadRef = useRef(load)
  useEffect(() => {
    let cancelled = false
    loadRef.current().then(
      (result) => !cancelled && setInfo(result),
      (err) => !cancelled && setError(errorMessage(err)),
    )
    return () => {
      cancelled = true
    }
  }, [])

  // Escape closes the dialog.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const copy = async () => {
    if (!info) return
    try {
      const how = await copyText(info.prompt)
      setCopyStatus(how === 'clipboard' ? 'Copied to the clipboard.' : 'Copied (fallback).')
    } catch {
      setCopyStatus('Copy failed: select the text and press Ctrl/Cmd-C.')
    }
  }

  const submitPaste = async () => {
    if (!pasteTarget || !pasted.trim()) return
    setPasting(true)
    const run = await paste({ ...pasteTarget, raw: pasted })
    setPasting(false)
    if (run) onClose()
  }

  return (
    <div className="modal modal-open" role="dialog" aria-modal="true" aria-labelledby="prompt-dialog-title">
      <div className="modal-box flex max-h-[92vh] w-11/12 max-w-5xl flex-col gap-2" data-testid="prompt-dialog">
        <div className="flex flex-wrap items-center gap-2">
          <h3 id="prompt-dialog-title" className="text-lg font-bold">
            {title}
          </h3>
          {info?.scope_name && <span className="text-sm text-base-content/60">{info.scope_name}</span>}
          {info?.model !== undefined && (
            <span className="badge badge-sm badge-ghost">model {info.model ?? 'CLI default'}</span>
          )}
          {info?.thinking !== undefined && <span className="badge badge-sm badge-ghost">thinking {info.thinking ?? '—'}</span>}
          {info?.cache_hit !== undefined && (
            <span
              className={`badge badge-sm ${info.cache_hit ? 'badge-success' : 'badge-ghost'}`}
              title={
                info.cache_hit
                  ? 'An earlier run had exactly this input: Run returns its findings at once (tick "force" to run anyway).'
                  : 'No earlier run had exactly this input.'
              }
              data-testid="prompt-cache-badge"
            >
              {info.cache_hit ? 'cached' : 'not cached'}
            </span>
          )}
          <button type="button" className="btn ml-auto btn-square btn-ghost btn-sm" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>

        {error && (
          <div role="alert" className="alert text-sm alert-soft alert-error" data-testid="prompt-error">
            {error}
          </div>
        )}
        {!info && !error && <span className="loading loading-md loading-spinner" />}

        {info && (
          <>
            <div className="text-xs">
              <span className="text-base-content/60">Command (the prompt goes in on stdin): </span>
              <code className="font-mono break-all" data-testid="prompt-command">
                {info.command.length ? shellJoin(info.command) : '—'}
              </code>
            </div>
            <pre
              className="min-h-32 flex-1 overflow-auto rounded border border-base-300 bg-base-200/50 p-2 font-mono text-xs whitespace-pre"
              data-testid="prompt-text"
            >
              {info.prompt}
            </pre>
            <div className="flex items-center gap-2">
              <button type="button" className="btn btn-sm" onClick={() => void copy()} data-testid="prompt-copy">
                Copy prompt
              </button>
              {copyStatus && (
                <span className="text-sm text-base-content/70" data-testid="copy-status">
                  {copyStatus}
                </span>
              )}
              <span className="ml-auto text-xs text-base-content/50">{info.prompt.length.toLocaleString('en-US')} characters</span>
            </div>
          </>
        )}

        {pasteTarget && (
          <div className="flex flex-col gap-1 border-t border-base-300 pt-2">
            <label className="text-sm font-medium" htmlFor="paste-back">
              Paste back
              <span className="ml-2 font-normal text-base-content/60">
                the JSON answer from a web UI, e.g. {'{"findings": [...]}'}; it is checked like any run.
              </span>
            </label>
            <textarea
              id="paste-back"
              className="textarea h-28 w-full font-mono text-xs"
              value={pasted}
              onChange={(e) => setPasted(e.target.value)}
              placeholder='{"findings": [ ... ]}'
              data-testid="paste-input"
            />
            <div className="flex justify-end">
              <button
                type="button"
                className="btn btn-sm btn-primary"
                onClick={() => void submitPaste()}
                disabled={!pasted.trim() || pasting}
                data-testid="paste-submit"
              >
                {pasting && <span className="loading loading-xs loading-spinner" />}
                Paste back
              </button>
            </div>
          </div>
        )}
      </div>
      <button type="button" className="modal-backdrop" onClick={onClose} aria-label="Close" />
    </div>
  )
}
