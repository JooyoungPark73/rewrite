// CommentBox: the editor's selection menu → "Comment…" → a note on the selected text.
// The comment becomes a finding (POST /api/findings/comment): it gets a card in the
// Findings column with a "comment" badge, the highlight in the editor, re-anchoring and
// the open / addressed / dismissed states, and "Rewrite…" on the card asks a model for a
// change that answers it.
//
// "Edit…" on a comment's card opens the same box with the comment's text (the store's
// `commentTarget.edit`); saving sends PATCH /api/findings/{id}/comment. The span and the
// anchor stay; only the text changes.
import { useState } from 'react'
import { useReviseStore, type CommentTarget } from '../state/revise'

export default function CommentBox() {
  const target = useReviseStore((s) => s.commentTarget)
  if (!target) return null
  // A new key per comment: the form's fields start from that comment's text every time.
  return <CommentForm key={target.edit?.findingId ?? 'new'} target={target} />
}

function CommentForm({ target }: { target: CommentTarget }) {
  const busy = useReviseStore((s) => s.commentBusy)
  const error = useReviseStore((s) => s.commentError)
  const submit = useReviseStore((s) => s.submitComment)
  const close = useReviseStore((s) => s.closeComment)
  const [problem, setProblem] = useState(target.edit?.problem ?? '')
  const [why, setWhy] = useState(target.edit?.why ?? '')
  const editing = !!target.edit
  const where = `${target.file}:${target.lineStart}${target.lineEnd !== target.lineStart ? `-${target.lineEnd}` : ''}`
  return (
    <div className="modal modal-open" role="dialog" aria-modal="true" aria-labelledby="comment-title">
      <div className="modal-box flex w-11/12 max-w-2xl flex-col gap-3" data-native-undo data-testid="comment-box" data-editing={editing ? 'true' : undefined}>
        <div className="flex items-start gap-3">
          <div className="min-w-0 flex-1">
            <h3 id="comment-title" className="text-lg font-bold">
              {editing ? 'Edit comment' : 'Comment'} on {where}
            </h3>
            <p className="line-clamp-3 text-sm text-base-content/70" title={target.quote} data-testid="comment-quote">
              “{target.quote}”
            </p>
            {target.nodeId && (
              <p className="text-xs text-base-content/50">
                in paragraph <span className="font-mono">{target.nodeId}</span>
              </p>
            )}
          </div>
          <button type="button" className="btn btn-ghost btn-sm" onClick={close} aria-label="Close" data-testid="comment-close">
            ✕
          </button>
        </div>
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            void submit(problem, why)
          }}
        >
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-base-content/70">What should change</span>
            <textarea
              autoFocus
              className="textarea w-full textarea-sm leading-snug"
              rows={3}
              placeholder="Too verbose: say it in one clause. / These two sentences disconnect: add a bridge. / Not needed: drop it. / Split into two paragraphs."
              value={problem}
              onChange={(e) => setProblem(e.target.value)}
              data-testid="comment-problem"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-base-content/70">Why (optional)</span>
            <input className="input input-sm w-full" value={why} onChange={(e) => setWhy(e.target.value)} placeholder="The reader already knows what a warm pool is." data-testid="comment-why" />
          </label>
          {error && (
            <div role="alert" className="alert text-sm alert-soft alert-error" data-testid="comment-error">
              {error}
            </div>
          )}
          <div className="flex items-center justify-end gap-2">
            <button type="button" className="btn btn-ghost btn-sm" onClick={close}>
              Cancel
            </button>
            <button type="submit" className="btn btn-sm btn-primary" disabled={busy || !problem.trim()} data-testid="comment-submit">
              {busy && <span className="loading loading-xs loading-spinner" />}
              {editing ? 'Save comment' : 'Add comment'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
