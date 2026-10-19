// TextField: one plain editable text of the spec (a field such as
// "evidence", a node's notes, the message's takeaway, a claim's statement).
//
// It is an <input>, or a <textarea> when `rows` is given. A textarea starts at
// `rows` lines and grows with its content (no inner scrollbar), so a long thesis
// or evidence is readable without resizing. Typing is sent to the backend as
// one op through `commit`, 400 ms after the last keystroke or on blur (see
// useSyncedText.ts). Escape leaves the field. Figure / table / equation
// references in the text ("fig:pipeline", "\ref{tab:latency}") show as chips
// under the field (LabelRefs.tsx). The outliner rows' own `says` line is a
// different component (Row.tsx) because it also handles the outliner keys
// (Enter, Tab, ...).
import { useLayoutEffect, useRef, type KeyboardEvent } from 'react'
import LabelRefs from './LabelRefs'
import { useFocusOnRequest, useSyncedText } from './useSyncedText'

export interface TextFieldProps {
  /** Unique key of this input among all mounted spec inputs, e.g. "field:intro-1:figure_story". */
  syncKey: string
  /** The value in paper.json. */
  value: string
  /** Sends the new text (an update op); resolves when the backend answered. */
  commit: (value: string) => Promise<unknown>
  /** Accessible name; also shown as a small caption when `caption` is true. */
  label: string
  caption?: boolean
  placeholder?: string
  /** Number of rows for a textarea; omitted = single-line input. */
  rows?: number
  testId?: string
  /** Focus this field when the store's requestFocus(focusId) is called. */
  focusId?: string
  className?: string
}

export default function TextField({
  syncKey,
  value,
  commit,
  label,
  caption = false,
  placeholder,
  rows,
  testId,
  focusId,
  className = '',
}: TextFieldProps) {
  const text = useSyncedText(syncKey, value, commit)
  const inputRef = useRef<HTMLInputElement>(null)
  const areaRef = useRef<HTMLTextAreaElement>(null)
  useFocusOnRequest(focusId ?? null, rows ? areaRef : inputRef)

  // Grow the textarea to fit its text (down to `rows` lines) whenever the text changes,
  // and again whenever its width changes (columns settle after the first paint, panels
  // open and close), since wrapping depends on the width.
  useLayoutEffect(() => {
    const area = areaRef.current
    if (!area) return
    const grow = () => {
      area.style.height = 'auto'
      area.style.height = `${area.scrollHeight}px`
    }
    grow()
    if (typeof ResizeObserver === 'undefined') return
    let lastWidth = area.clientWidth
    const observer = new ResizeObserver(() => {
      if (area.clientWidth !== lastWidth) {
        lastWidth = area.clientWidth
        grow()
      }
    })
    observer.observe(area)
    return () => observer.disconnect()
  }, [text.value, rows])

  const common = {
    value: text.value,
    placeholder,
    'aria-label': label,
    'data-testid': testId,
    onBlur: text.flush,
    onKeyDown: (e: KeyboardEvent<HTMLInputElement | HTMLTextAreaElement>) => {
      if (e.key === 'Escape') e.currentTarget.blur()
    },
  }

  return (
    <label className={`flex min-w-0 flex-col ${className}`}>
      {caption && <span className="text-[11px] tracking-wide text-base-content/50 uppercase">{label}</span>}
      {rows ? (
        <textarea
          ref={areaRef}
          rows={rows}
          className="textarea min-h-0 w-full resize-none overflow-hidden textarea-sm leading-snug"
          onChange={(e) => text.onChange(e.target.value)}
          {...common}
        />
      ) : (
        <input
          ref={inputRef}
          className="input input-sm w-full"
          onChange={(e) => text.onChange(e.target.value)}
          {...common}
        />
      )}
      <LabelRefs text={text.value} />
    </label>
  )
}
