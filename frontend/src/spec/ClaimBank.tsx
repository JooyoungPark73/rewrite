// ClaimBank: the left column of the Spec view, one card per claim.
//
// A claim is a statement the paper argues, kept in a flat list because one
// claim is argued in several places (PLAN.md). Sections and paragraphs point
// at claims by id.
//
//   ┌──────────────────────────────┐
//   │ [c1]  linked ×2          ✕   │  the colored chip is draggable: drop it on
//   │ claim statement (says)       │  a section or paragraph row to link it
//   │ evidence                     │
//   │ scope                        │
//   └──────────────────────────────┘
//
// "+ claim" adds an empty claim and focuses it. Delete asks for a second click
// ("Delete?") and also removes the claim from every row that links it.
// Rows can also create claims: ⋯ → "Lift to claim" in the outliner.
import { useDraggable } from '@dnd-kit/core'
import { useMemo, useState } from 'react'
import type { Node as SpecNode } from '../api/client'
import { LEVEL_FIELDS } from '../api/client'
import { flushPendingEdits, useSpecStore } from '../state/spec'
import { claimDragId, type DragData } from './dnd'
import TextField from './TextField'
import { claimColor, claimUsage, fieldLabel } from './tree'

const NO_CLAIMS: SpecNode[] = []

export default function ClaimBank() {
  const claims = useSpecStore((s) => s.spec?.claims ?? NO_CLAIMS)
  const spec = useSpecStore((s) => s.spec)
  const applyOp = useSpecStore((s) => s.applyOp)
  const usage = useMemo(() => (spec ? claimUsage(spec) : {}), [spec])

  const addClaim = () => {
    flushPendingEdits()
    void applyOp({ type: 'add_claim', says: '' }).then((res) => {
      if (res?.new_id) useSpecStore.getState().requestFocus(res.new_id)
    })
  }

  return (
    <aside className="flex w-72 shrink-0 flex-col border-r border-base-300 bg-base-200/40" data-testid="claim-bank">
      <div className="flex items-center gap-2 border-b border-base-300 px-3 py-1.5">
        <span className="text-xs font-semibold tracking-wide text-base-content/60 uppercase">Claims</span>
        <span className="badge badge-ghost badge-sm">{claims.length}</span>
        <button
          type="button"
          className="btn ml-auto btn-ghost btn-xs"
          // Drop focus first: Space/Enter on a still-focused button would add another claim.
          onClick={(e) => {
            e.currentTarget.blur()
            addClaim()
          }}
          disabled={!spec}
          data-testid="add-claim"
        >
          + claim
        </button>
      </div>
      <div className="flex flex-1 flex-col gap-2 overflow-y-auto p-2">
        {claims.map((claim) => (
          <ClaimCard key={claim.id} claim={claim} uses={usage[claim.id] ?? 0} />
        ))}
        {spec && claims.length === 0 && (
          <p className="p-2 text-sm text-base-content/50">
            No claims yet. Add one here, or use ⋯ → Lift to claim on an outliner row.
          </p>
        )}
      </div>
    </aside>
  )
}

function ClaimCard({ claim, uses }: { claim: SpecNode; uses: number }) {
  const applyOp = useSpecStore((s) => s.applyOp)
  const [confirming, setConfirming] = useState(false)
  const { setNodeRef, listeners, attributes, isDragging } = useDraggable({
    id: claimDragId(claim.id),
    data: { kind: 'claim', claimId: claim.id } satisfies DragData,
  })
  const fieldKeys = [...LEVEL_FIELDS.claim, ...Object.keys(claim.fields).filter((k) => !LEVEL_FIELDS.claim.includes(k))]

  return (
    <div
      className="rounded border border-l-4 border-base-300 border-l-amber-500 bg-base-100 p-2 shadow-sm"
      data-testid="claim-card"
      data-claim-id={claim.id}
    >
      <div className="mb-1 flex items-center gap-2">
        <span
          ref={setNodeRef}
          {...listeners}
          {...attributes}
          className={`badge cursor-grab touch-none border font-mono select-none ${claimColor(claim.id)} ${
            isDragging ? 'opacity-40' : ''
          }`}
          title="Drag onto a section or paragraph to link this claim"
          data-testid="claim-chip"
        >
          {claim.id}
        </span>
        <span className="text-xs text-base-content/50">{uses ? `linked ×${uses}` : 'not linked'}</span>
        <div className="ml-auto flex items-center gap-1">
          {confirming ? (
            <>
              <button
                type="button"
                className="btn btn-xs btn-error"
                onClick={() => {
                  setConfirming(false)
                  void applyOp({ type: 'delete_claim', id: claim.id })
                }}
                data-testid="claim-delete-confirm"
              >
                Delete?
              </button>
              <button type="button" className="btn btn-ghost btn-xs" onClick={() => setConfirming(false)}>
                Cancel
              </button>
            </>
          ) : (
            <button
              type="button"
              className="btn btn-square btn-ghost btn-xs text-base-content/40 hover:text-error"
              onClick={() => setConfirming(true)}
              aria-label={`Delete claim ${claim.id}`}
              title="Delete this claim (also unlinks it everywhere)"
              data-testid="claim-delete"
            >
              ✕
            </button>
          )}
        </div>
      </div>
      <TextField
        syncKey={`claim:${claim.id}:says`}
        value={claim.says}
        commit={(says) => applyOp({ type: 'update_claim', id: claim.id, says })}
        label={`Claim ${claim.id} says`}
        placeholder="The claim, as one sentence…"
        rows={2}
        testId="claim-says"
        focusId={claim.id}
      />
      <div className="mt-1 flex flex-col gap-1">
        {fieldKeys.map((key) => (
          <TextField
            key={key}
            syncKey={`claim:${claim.id}:${key}`}
            value={claim.fields[key] ?? ''}
            commit={(value) => applyOp({ type: 'update_claim', id: claim.id, fields: { [key]: value } })}
            label={fieldLabel(key)}
            placeholder={fieldLabel(key)}
            testId={`claim-field-${key}`}
          />
        ))}
        {claim.notes && (
          <TextField
            syncKey={`claim:${claim.id}:notes`}
            value={claim.notes}
            commit={(notes) => applyOp({ type: 'update_claim', id: claim.id, notes })}
            label="notes"
            caption
            rows={2}
            testId="claim-notes"
          />
        )}
      </div>
    </div>
  )
}
