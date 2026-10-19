// ErrorList: the collapsible "Problems" panel under the editor.
//
// Shows the errors, warnings and info messages parsed from the last compile
// (CompileResult.errors). Clicking a row calls store.goToLocation, which opens
// the file and moves the cursor to the line. Warnings and info can be hidden
// with the toggle; errors are always shown. "Log" shows the raw TeX log, for
// failures the parser could not attribute to a line.
import { useState } from 'react'
import type { CompileResult, LogError, Severity } from '../api/client'
import { useStore } from '../state/store'

const ICONS: Record<Severity, { glyph: string; className: string; label: string }> = {
  error: { glyph: '✖', className: 'text-error', label: 'Error' },
  warning: { glyph: '▲', className: 'text-warning', label: 'Warning' },
  info: { glyph: 'ℹ', className: 'text-info', label: 'Info' },
}

/** Treat anything unexpected from the backend as "info". */
function severityOf(e: LogError): Severity {
  return e.severity === 'error' || e.severity === 'warning' ? e.severity : 'info'
}

export default function ErrorList() {
  const result = useStore((s) => s.compileResult)
  const compiling = useStore((s) => s.compiling)
  const goToLocation = useStore((s) => s.goToLocation)
  const mainFile = useStore((s) => s.mainFile)

  // The panel is open unless the user collapsed it. Collapsing is remembered
  // per compile result, so a new compile that has errors pops it open again.
  const [collapsedFor, setCollapsedFor] = useState<CompileResult | null | undefined>(undefined)
  const [showMinor, setShowMinor] = useState(true)
  const [showLog, setShowLog] = useState(false)

  const all = result?.errors ?? []
  const errorCount = all.filter((e) => severityOf(e) === 'error').length
  const warningCount = all.filter((e) => severityOf(e) === 'warning').length
  const infoCount = all.length - errorCount - warningCount
  const visible = showMinor ? all : all.filter((e) => severityOf(e) === 'error')
  const open = collapsedFor === undefined || (collapsedFor !== result && errorCount > 0)
  const setOpen = (value: boolean) => setCollapsedFor(value ? undefined : result)

  return (
    <section className="flex min-h-0 flex-col border-t border-base-300 bg-base-100 text-sm" data-testid="error-list">
      <header className="flex items-center gap-2 px-2 py-1">
        <button
          type="button"
          className="btn btn-ghost btn-xs"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          title={open ? 'Collapse' : 'Expand'}
        >
          {open ? '▾' : '▸'} Problems
        </button>
        <span className="badge badge-sm badge-error" data-testid="error-count" title="Errors">
          {errorCount}
        </span>
        <span className="badge badge-sm badge-warning" title="Warnings">
          {warningCount}
        </span>
        {compiling && <span className="loading loading-spinner loading-xs" />}
        {result && (
          <span className="text-xs text-base-content/50">
            {result.ok ? 'compiled' : 'failed'} in {(result.duration_ms / 1000).toFixed(1)} s
          </span>
        )}
        <label className="ml-auto flex cursor-pointer items-center gap-1 text-xs">
          <input
            type="checkbox"
            className="checkbox checkbox-xs"
            checked={showMinor}
            onChange={(e) => setShowMinor(e.target.checked)}
          />
          warnings &amp; info ({warningCount + infoCount})
        </label>
        <button
          type="button"
          className={`btn btn-xs ${showLog ? 'btn-active' : 'btn-ghost'}`}
          onClick={() => {
            setShowLog(!showLog)
            setOpen(true)
          }}
          disabled={!result}
        >
          Log
        </button>
      </header>

      {open && (
        <div className="max-h-48 min-h-0 overflow-y-auto border-t border-base-200">
          {!result && <div className="px-3 py-2 text-base-content/50">Not compiled yet</div>}
          {result && showLog && (
            <pre className="px-3 py-2 font-mono text-xs whitespace-pre-wrap text-base-content/80">{result.log}</pre>
          )}
          {result && !showLog && visible.length === 0 && (
            <div className="px-3 py-2 text-base-content/50">No problems</div>
          )}
          {result && !showLog && visible.length > 0 && (
            <ul>
              {visible.map((e, i) => {
                const sev = ICONS[severityOf(e)]
                const file = e.file ?? mainFile ?? '?'
                const where = e.line ? `${file}:${e.line}` : file
                return (
                  <li key={i}>
                    <button
                      type="button"
                      data-testid="error-row"
                      onClick={() => void goToLocation(e.file, e.line)}
                      className="flex w-full items-baseline gap-2 px-3 py-1 text-left hover:bg-base-200"
                      title={e.message}
                    >
                      <span className={sev.className} aria-label={sev.label}>
                        {sev.glyph}
                      </span>
                      <span className="shrink-0 font-mono text-xs text-base-content/70">{where}</span>
                      <span className="truncate">{e.message.split('\n')[0]}</span>
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}
