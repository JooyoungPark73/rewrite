// SkeletonPanel: what the backend found in the .tex files (GET
// /api/spec/skeleton) that is not in the spec yet, at the bottom of the
// outliner. "From the tex up" in PLAN.md.
//
//   ▾ Skeleton from the .tex   [3 not adopted]            ☐ show adopted  ↻
//     section     Introduction        sections/intro.tex:1     [Adopt]
//        "Function-as-a-Service (FaaS) platforms bill tenants…"  :4–8  [Adopt]
//        ...
//
// - Adopt on a heading makes a section node anchored to that heading
//   (POST /api/spec/adopt {kind: "section"}); its `says` starts empty.
// - Adopt on a paragraph inserts `% @p <id>` above it in the .tex and adds a
//   paragraph node, under the section adopted for its heading when there is
//   one (otherwise the backend picks the section before it).
// - Paragraphs are grouped under the nearest heading above them in the same
//   file (or, for a file pulled in by \input, the heading named in
//   `section_heading`). Adopted entries are hidden unless "show adopted" is on.
// - Clicking a file:line opens it in the editor.
import { useMemo, useState } from 'react'
import type { Skeleton, SkeletonParagraph, SkeletonSection } from '../api/client'
import { useSpecStore } from '../state/spec'
import { useStore } from '../state/store'

interface Group {
  /** null for paragraphs that come before any heading (e.g. the abstract). */
  heading: SkeletonSection | null
  paragraphs: SkeletonParagraph[]
}

/** Put every paragraph under the heading it belongs to (see the header comment). */
function groupSkeleton(skeleton: Skeleton): Group[] {
  const groups: Group[] = skeleton.sections.map((heading) => ({ heading, paragraphs: [] }))
  const beforeAnyHeading: Group = { heading: null, paragraphs: [] }
  for (const paragraph of skeleton.paragraphs) {
    let target: Group | null = null
    for (const g of groups) {
      const h = g.heading!
      if (h.file === paragraph.file && h.line < paragraph.line_start && (!target || h.line > target.heading!.line)) {
        target = g
      }
    }
    if (!target && paragraph.section_heading !== null) {
      target = groups.find((g) => g.heading!.heading === paragraph.section_heading) ?? null
    }
    ;(target ?? beforeAnyHeading).paragraphs.push(paragraph)
  }
  return beforeAnyHeading.paragraphs.length ? [beforeAnyHeading, ...groups] : groups
}

export default function SkeletonPanel() {
  const skeleton = useSpecStore((s) => s.skeleton)
  const adopt = useSpecStore((s) => s.adopt)
  const refreshSkeleton = useSpecStore((s) => s.refreshSkeleton)
  const [open, setOpen] = useState(true)
  const [showAdopted, setShowAdopted] = useState(false)
  // Which Adopt button is waiting for the backend ("file:line").
  const [busy, setBusy] = useState<string | null>(null)

  const groups = useMemo(() => (skeleton ? groupSkeleton(skeleton) : []), [skeleton])
  const unadopted = skeleton
    ? skeleton.sections.filter((s) => !s.adopted_id).length + skeleton.paragraphs.filter((p) => !p.marker_id).length
    : 0

  const run = async (key: string, task: () => Promise<unknown>) => {
    setBusy(key)
    try {
      await task()
    } finally {
      setBusy(null)
    }
  }

  const open_ = (file: string, line: number) => void useStore.getState().goToLocation(file, line)

  const body = groups.map((group, gi) => {
    const paragraphs = group.paragraphs.filter((p) => showAdopted || !p.marker_id)
    const h = group.heading
    const showHeading = h ? showAdopted || !h.adopted_id || paragraphs.length > 0 : paragraphs.length > 0
    if (!showHeading && paragraphs.length === 0) return null
    const parentId = h?.adopted_id ?? undefined
    return (
      <div key={h ? `${h.file}:${h.line}` : `before-${gi}`} className="mb-1.5">
        {h ? (
          <div
            className="flex items-center gap-2 rounded px-1 py-0.5 hover:bg-base-200"
            data-testid="skeleton-heading"
            data-heading={h.heading}
            data-adopted={h.adopted_id ?? ''}
          >
            <span className="badge badge-ghost badge-xs">{h.kind}</span>
            <span className="min-w-0 flex-1 truncate font-medium">{h.heading}</span>
            <button type="button" className="link font-mono text-xs text-base-content/50" onClick={() => open_(h.file, h.line)}>
              {h.file}:{h.line}
            </button>
            {h.adopted_id ? (
              <span className="font-mono text-xs text-success">→ {h.adopted_id}</span>
            ) : (
              <button
                type="button"
                className="btn btn-outline btn-xs btn-primary"
                disabled={busy !== null}
                onClick={() => void run(`${h.file}:${h.line}`, () => adopt({ kind: 'section', file: h.file, line: h.line }))}
                data-testid="adopt-heading"
              >
                {busy === `${h.file}:${h.line}` && <span className="loading loading-xs loading-spinner" />}
                Adopt
              </button>
            )}
          </div>
        ) : (
          <div className="px-1 py-0.5 text-xs text-base-content/50">Before the first heading</div>
        )}
        {paragraphs.map((p) => (
          <div
            key={`${p.file}:${p.line_start}`}
            className="ml-5 flex items-center gap-2 rounded px-1 py-0.5 hover:bg-base-200"
            data-testid="skeleton-paragraph"
            data-file={p.file}
            data-line={p.line_start}
            data-marker={p.marker_id ?? ''}
          >
            <span className="min-w-0 flex-1 truncate text-base-content/80" title={p.preview}>
              {p.preview}
            </span>
            <button
              type="button"
              className="link font-mono text-xs whitespace-nowrap text-base-content/50"
              onClick={() => open_(p.file, p.line_start)}
            >
              {p.file.split('/').pop()}:{p.line_start}–{p.line_end}
            </button>
            {p.marker_id ? (
              <span className="font-mono text-xs text-success">@p {p.marker_id}</span>
            ) : (
              <button
                type="button"
                className="btn btn-outline btn-xs btn-primary"
                disabled={busy !== null}
                onClick={() =>
                  void run(`${p.file}:${p.line_start}`, () =>
                    adopt({ kind: 'paragraph', file: p.file, line_start: p.line_start, parent_id: parentId }),
                  )
                }
                title="Insert % @p <id> above this paragraph and add it to the spec"
                data-testid="adopt-paragraph"
              >
                {busy === `${p.file}:${p.line_start}` && <span className="loading loading-xs loading-spinner" />}
                Adopt
              </button>
            )}
          </div>
        ))}
      </div>
    )
  })

  return (
    <section className="mt-auto border-t border-base-300 bg-base-200/30" data-testid="skeleton-panel">
      <div className="flex items-center gap-2 px-3 py-1.5 text-sm">
        <button
          type="button"
          className="btn btn-ghost btn-xs font-semibold"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          data-testid="skeleton-toggle"
        >
          {open ? '▾' : '▸'} Skeleton from the .tex
        </button>
        <span className="badge badge-sm" data-testid="skeleton-count">
          {unadopted} not adopted
        </span>
        <label className="ml-auto flex items-center gap-1 text-xs text-base-content/70">
          <input
            type="checkbox"
            className="checkbox checkbox-xs"
            checked={showAdopted}
            onChange={(e) => setShowAdopted(e.target.checked)}
            data-testid="skeleton-show-adopted"
          />
          show adopted
        </label>
        <button
          type="button"
          className="btn btn-square btn-ghost btn-xs"
          onClick={() => void refreshSkeleton()}
          title="Re-read the .tex files"
          aria-label="Refresh skeleton"
        >
          ↻
        </button>
      </div>
      {open && (
        <div className="max-h-[45vh] overflow-y-auto px-3 pb-3 text-sm">
          {!skeleton && <span className="loading loading-xs loading-spinner" />}
          {body}
          {skeleton && body.every((b) => b === null) && (
            <p className="text-base-content/50">Everything in the .tex is adopted.</p>
          )}
        </div>
      )}
    </section>
  )
}
