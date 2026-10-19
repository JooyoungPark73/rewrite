// LabelRefs: the figure / table / equation references found in a piece of spec
// text, resolved against the paper's \label targets (GET /api/spec/labels).
//
// Spec text should name figures by label ("fig:pipeline", or "\ref{fig:pipeline}"),
// never by number: numbers change whenever a figure moves. Each reference becomes
// a chip: a known label shows its kind and caption and opens the .tex at the
// \label line when clicked; an unknown one is marked so the typo (or the missing
// figure) is visible. Rendered under the text fields of TextField.tsx.
import { useMemo } from 'react'
import type { LabelInfo } from '../api/client'
import { useStore } from '../state/store'
import { useSpecStore } from '../state/spec'

// `\ref{...}`, `\autoref{...}`, `\cref{...}`, `\eqref{...}` (with any *-form), or a bare
// `prefix:name` token with one of the usual prefixes.
const REF_RE = /\\(?:auto|c|C|eq|name|page|v)?ref\*?\{([^{}]+)\}|(?<![\w:\\])((?:fig|tab|eq|sec|alg|lst|app|thm|lem|def|cor)[a-z]*:[A-Za-z0-9_.:+-]*[A-Za-z0-9_+-])/g

/** The distinct labels referred to in `text`, in order of appearance. */
function findLabelRefs(text: string): string[] {
  const out: string[] = []
  for (const m of text.matchAll(REF_RE)) {
    for (const name of (m[1] ?? m[2]).split(',')) {
      const label = name.trim()
      if (label && !out.includes(label)) out.push(label)
    }
  }
  return out
}

const KIND_LABEL: Record<string, string> = {
  figure: 'Figure',
  table: 'Table',
  equation: 'Equation',
  listing: 'Listing',
  section: 'Section',
  other: 'Label',
}

export default function LabelRefs({ text }: { text: string }) {
  const labels = useSpecStore((s) => s.labels)
  const goToLocation = useStore((s) => s.goToLocation)
  const refs = useMemo(() => findLabelRefs(text), [text])
  const byName = useMemo(() => new Map(labels.map((l) => [l.label, l])), [labels])
  if (refs.length === 0) return null
  return (
    <div className="mt-1 flex flex-wrap gap-1" data-testid="label-refs">
      {refs.map((name) => {
        const info: LabelInfo | undefined = byName.get(name)
        if (!info) {
          return (
            <span
              key={name}
              className="badge badge-xs badge-soft badge-warning font-mono"
              title={`No \\label{${name}} in the paper (a typo, or a figure that is not written yet)`}
              data-testid="label-ref"
              data-label={name}
              data-found="false"
            >
              {name}?
            </span>
          )
        }
        const kind = KIND_LABEL[info.kind] ?? info.kind
        const caption = info.caption ? ` — ${info.caption}` : ''
        return (
          <button
            key={name}
            type="button"
            className="badge badge-xs badge-soft badge-info max-w-full cursor-pointer gap-1 font-mono"
            title={`${kind} at ${info.file}:${info.line}${caption}\nClick to open it in the editor.`}
            onClick={() => void goToLocation(info.file, info.line)}
            data-testid="label-ref"
            data-label={name}
            data-found="true"
            data-kind={info.kind}
          >
            <span>{name}</span>
            {info.caption && <span className="truncate font-sans normal-case opacity-70">{info.caption}</span>}
          </button>
        )
      })}
    </div>
  )
}
