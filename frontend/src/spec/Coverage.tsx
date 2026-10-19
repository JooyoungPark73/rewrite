// Coverage: the right rail of the Spec view (phase 5) — the claims × sections
// matrix from PLAN.md: where each claim is argued.
//
//              Intro  Method  Results     <- section headings, written vertically
//   [c1] Snapshot…   2      ·       1      <- count = nodes in that section (the
//   [c2] Pages are…  ·      ·       ·         section itself + its paragraphs)
//                                             that link the claim; empty when 0
//   - a row tinted amber is an unreferenced claim (argued nowhere);
//   - a column tinted rose is a section that argues no claim;
//   - a ringed cell means the section node itself links the claim (the rest
//     of the count comes from its paragraphs);
//   - hovering a cell names the claim and the section in full;
//   - clicking a cell links the claim to the section node, or unlinks it if
//     the section already links it: one link_claim / unlink_claim op, so one
//     undo step. Paragraph links are edited in the outliner (drag a chip).
//
// The numbers come from GET /api/spec/coverage. They are re-read whenever
// paper.json changes: after our own edits and on `spec.changed` from the
// backend, both of which change the spec store's `raw` text.
import { useEffect, useState } from 'react'
import * as api from '../api/client'
import type { Coverage as CoverageData } from '../api/client'
import { flushPendingEdits, useSpecStore } from '../state/spec'
import { claimColor, findNode } from './tree'

/** Short label for a column: the heading, else the promise, else the id. */
function sectionLabel(s: CoverageData['sections'][number]): string {
  return s.heading || s.says || s.id
}

export default function Coverage() {
  const raw = useSpecStore((s) => s.raw)
  const spec = useSpecStore((s) => s.spec)
  const applyOp = useSpecStore((s) => s.applyOp)
  const [data, setData] = useState<CoverageData | null>(null)
  const [error, setError] = useState<string | null>(null)
  /** The paper.json text the matrix on screen was read for (loading while it differs from `raw`). */
  const [loadedFor, setLoadedFor] = useState<string | null>(null)
  /** "claim|section" of a click whose op has not answered yet (ignores double clicks). */
  const [busy, setBusy] = useState<string | null>(null)
  const loading = !!raw && loadedFor !== raw

  // Re-read the matrix whenever paper.json's text changes.
  useEffect(() => {
    if (!raw) return
    let cancelled = false
    api.getCoverage().then(
      (res) => {
        if (cancelled) return
        setData(res)
        setError(null)
        setLoadedFor(raw)
      },
      (err: unknown) => {
        if (cancelled) return
        setError(api.errorMessage(err))
        setLoadedFor(raw)
      },
    )
    return () => {
      cancelled = true
    }
  }, [raw])

  const toggle = async (claimId: string, sectionId: string) => {
    const key = `${claimId}|${sectionId}`
    if (busy || !spec) return
    const section = findNode(spec, sectionId)
    if (!section) return
    flushPendingEdits()
    setBusy(key)
    const linked = section.claims.includes(claimId)
    await applyOp({ type: linked ? 'unlink_claim' : 'link_claim', id: sectionId, claim_id: claimId })
    setBusy(null)
  }

  const unreferenced = new Set(data?.unreferenced_claims ?? [])
  const emptySections = new Set(data?.empty_sections ?? [])

  return (
    <aside
      className="hidden w-80 shrink-0 flex-col border-l border-base-300 bg-base-200/40 lg:flex"
      data-testid="coverage"
    >
      <div className="flex items-center gap-2 border-b border-base-300 px-3 py-1.5">
        <h2 className="text-xs font-semibold tracking-wide text-base-content/60 uppercase">Coverage</h2>
        {data && (
          <span className="text-xs text-base-content/50">
            {data.claims.length} claim{data.claims.length === 1 ? '' : 's'} × {data.sections.length} section
            {data.sections.length === 1 ? '' : 's'}
          </span>
        )}
        {(loading || busy) && <span className="loading ml-auto loading-xs loading-spinner text-base-content/40" />}
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-2">
        {error && !data ? (
          <p className="text-sm text-error" data-testid="coverage-error">
            Could not load coverage: {error}
          </p>
        ) : !spec ? (
          <p className="text-sm text-base-content/50">paper.json is missing or invalid.</p>
        ) : !data ? (
          <span className="loading loading-sm loading-spinner" />
        ) : data.claims.length === 0 || data.sections.length === 0 ? (
          <p className="text-sm text-base-content/50" data-testid="coverage-empty">
            {data.claims.length === 0 ? 'No claims yet: add one in the claim bank.' : 'No sections yet.'} The matrix shows
            where each claim is argued once there are claims and sections.
          </p>
        ) : (
          <table className="border-separate border-spacing-0 text-xs" data-testid="coverage-matrix">
            <thead>
              <tr>
                <th className="sticky top-0 left-0 z-20 bg-base-200" />
                {data.sections.map((s) => (
                  <th
                    key={s.id}
                    className={`sticky top-0 z-10 h-28 px-0.5 pb-1 align-bottom font-normal ${
                      emptySections.has(s.id) ? 'bg-rose-100' : 'bg-base-200'
                    }`}
                    title={`${s.heading ? `${s.heading} — ` : ''}${s.says || '(no promise yet)'} (${s.id})${
                      emptySections.has(s.id) ? '\nArgues no claim.' : ''
                    }`}
                    data-testid="coverage-section"
                    data-section-id={s.id}
                    data-empty={emptySections.has(s.id) ? 'true' : undefined}
                  >
                    {/* Written bottom-to-top so long headings fit in a narrow column. */}
                    <div className="mx-auto max-h-24 rotate-180 truncate text-left [writing-mode:vertical-rl]">{sectionLabel(s)}</div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.claims.map((c) => {
                const unref = unreferenced.has(c.id)
                return (
                  <tr key={c.id} data-testid="coverage-row" data-claim-id={c.id} data-unreferenced={unref ? 'true' : undefined}>
                    <th
                      className={`sticky left-0 z-10 max-w-36 border-b border-base-300 py-0.5 pr-1 text-left font-normal ${
                        unref ? 'bg-amber-100' : 'bg-base-100'
                      }`}
                      title={`${c.id}: ${c.says || '(empty claim)'}${unref ? '\nNot argued anywhere yet.' : ''}`}
                    >
                      <div className="flex items-center gap-1">
                        <span className={`badge h-4 shrink-0 border px-1 font-mono badge-xs ${claimColor(c.id)}`}>{c.id}</span>
                        <span className="truncate text-base-content/70">{c.says || <i className="text-base-content/40">empty</i>}</span>
                      </div>
                    </th>
                    {data.sections.map((s) => {
                      const count = data.cells[c.id]?.[s.id] ?? 0
                      const node = spec ? findNode(spec, s.id) : null
                      const own = !!node?.claims.includes(c.id)
                      const tint = unref ? 'bg-amber-50' : emptySections.has(s.id) ? 'bg-rose-50' : ''
                      const title =
                        `${c.id} “${c.says || '(empty)'}” × ${sectionLabel(s)}: ` +
                        (count ? `argued in ${count} place${count === 1 ? '' : 's'}` : 'not argued here') +
                        (own ? ' (the section itself links it)' : '') +
                        `.\nClick to ${own ? 'unlink it from' : 'link it to'} the section.`
                      return (
                        <td key={s.id} className={`border-b border-base-300 p-0.5 text-center ${tint}`}>
                          <button
                            type="button"
                            className={`h-6 w-6 rounded font-mono ${
                              count ? 'bg-teal-100 font-semibold text-teal-900' : 'hover:bg-base-300/60'
                            } ${own ? 'ring-1 ring-teal-600' : ''} disabled:cursor-wait`}
                            onClick={() => void toggle(c.id, s.id)}
                            disabled={busy !== null}
                            title={title}
                            aria-label={title}
                            data-testid="coverage-cell"
                            data-claim-id={c.id}
                            data-section-id={s.id}
                            data-count={count}
                            data-own={own ? 'true' : undefined}
                          >
                            {count || ''}
                          </button>
                        </td>
                      )
                    })}
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      {data && data.claims.length > 0 && data.sections.length > 0 && (
        <div className="flex flex-col gap-0.5 border-t border-base-300 px-3 py-1.5 text-xs text-base-content/60" data-testid="coverage-summary">
          <span>
            <span className="mr-1 inline-block h-2.5 w-2.5 rounded-sm bg-amber-200 align-middle" />
            {unreferenced.size} unreferenced claim{unreferenced.size === 1 ? '' : 's'}
          </span>
          <span>
            <span className="mr-1 inline-block h-2.5 w-2.5 rounded-sm bg-rose-200 align-middle" />
            {emptySections.size} section{emptySections.size === 1 ? '' : 's'} arguing nothing
          </span>
          <span>Click a cell to link or unlink the claim on the section (ringed = linked by the section itself).</span>
        </div>
      )}
    </aside>
  )
}
