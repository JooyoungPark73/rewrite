// Popover: a button that opens a small floating panel under it (phase 6).
//
//   [Run both ▾]
//   ┌──────────────────────────────┐
//   │ claude [model] [thinking]    │   <- `children(close)`
//   │ codex  [model] [thinking]    │
//   │                  [Start both]│
//   └──────────────────────────────┘
//
// Used for the per-runner options of "Run both" (findings/RunLauncher.tsx)
// and the runner / model / thinking of "Triage" (findings/TriageBar.tsx).
// The panel closes on Escape, on a click outside it, or when the content
// calls `close()` (e.g. after starting). It is not a modal: the rest of the
// page keeps working, and `j` / `k` are ignored while the focus is in one of
// its inputs (App.tsx skips keys typed into form fields).
import { useEffect, useRef, useState, type ReactNode } from 'react'

export interface PopoverProps {
  /** Text of the button that opens the panel. */
  label: ReactNode
  /** DaisyUI classes of that button, e.g. "btn btn-sm". */
  buttonClass?: string
  disabled?: boolean
  /** Tooltip of the button (also explains why it is disabled). */
  title?: string
  testId?: string
  /** Align the panel's right edge with the button (for buttons near the right side). */
  alignRight?: boolean
  /** The panel's content; call `close()` to shut it. */
  children: (close: () => void) => ReactNode
}

export default function Popover({ label, buttonClass = 'btn btn-sm', disabled, title, testId, alignRight, children }: PopoverProps) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLSpanElement>(null)

  // Close on a click outside, or Escape anywhere.
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  // A disabled button cannot keep a panel open.
  const shown = open && !disabled

  return (
    <span ref={rootRef} className="relative inline-flex">
      <button
        type="button"
        className={`${buttonClass} ${shown ? 'btn-active' : ''}`}
        onClick={() => setOpen(!open)}
        disabled={disabled}
        title={title}
        aria-expanded={shown}
        data-testid={testId}
      >
        {label} <span className="text-xs opacity-60">▾</span>
      </button>
      {shown && (
        <div
          className={`absolute top-full z-40 mt-1 flex w-max max-w-[90vw] flex-col gap-2 rounded-box border border-base-300 bg-base-100 p-3 text-sm shadow-lg ${
            alignRight ? 'right-0' : 'left-0'
          }`}
          role="dialog"
          data-testid={testId ? `${testId}-panel` : undefined}
        >
          {children(() => setOpen(false))}
        </div>
      )}
    </span>
  )
}
