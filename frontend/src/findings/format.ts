// Small display helpers shared by the findings components (phase 4):
// number formatting for the runs list, colors per severity / level / status,
// the "copy to clipboard" helper, and jumping from a finding to its spec node.
//
// Tailwind only generates classes it can see written out in full in the
// source, so every class name below is spelled out literally.
import type { AnchorState, FindingLevel, RunStatus } from '../api/client'
import { findClaim, findNode } from '../spec/tree'
import { useRunsStore } from '../state/runs'
import { useSpecStore } from '../state/spec'
import { useStore } from '../state/store'

// ---------------------------------------------------------------------------
// Numbers
// ---------------------------------------------------------------------------

/** "$0.0123", or "—" when the runner reports no cost (codex). */
export function formatCost(usd: number | null): string {
  return usd == null ? '—' : `$${usd.toFixed(4)}`
}

/** "1,234", or "—" when unknown. */
export function formatTokens(n: number | null): string {
  return n == null ? '—' : n.toLocaleString('en-US')
}

/** "850 ms", "12.3 s", "2m 05s", or "—". */
export function formatDuration(ms: number | null): string {
  if (ms == null) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`
  const minutes = Math.floor(ms / 60_000)
  const seconds = Math.round((ms % 60_000) / 1000)
  return `${minutes}m ${String(seconds).padStart(2, '0')}s`
}

/** "14:03:21" from seconds since the epoch, or "" when unknown. */
export function formatTime(epochSeconds: number | null): string {
  if (epochSeconds == null) return ''
  return new Date(epochSeconds * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

/** "2026-10-18 14:03" from seconds since the epoch (git dates, snapshots, comparisons). */
export function formatDate(epochSeconds: number | null): string {
  if (epochSeconds == null) return ''
  const d = new Date(epochSeconds * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** "intro.tex:41–44" (just the line when start = end). */
export function formatLines(file: string, start: number, end: number): string {
  const name = file.split('/').pop() ?? file
  return end > start ? `${name}:${start}–${end}` : `${name}:${start}`
}

/** An argv list as a copy-pasteable shell command (quotes only what needs it). */
export function shellJoin(argv: string[]): string {
  return argv.map((a) => (/^[\w\-.,/:=@+%]+$/.test(a) ? a : `'${a.replace(/'/g, `'\\''`)}'`)).join(' ')
}

// ---------------------------------------------------------------------------
// Colors
// ---------------------------------------------------------------------------

/** Left color band and badge per severity. */
export const SEVERITY_STYLE: Record<string, { band: string; badge: string }> = {
  critical: { band: 'border-l-red-600', badge: 'badge-error' },
  major: { band: 'border-l-amber-500', badge: 'badge-warning' },
  minor: { band: 'border-l-sky-400', badge: 'badge-info' },
}

/** Level chips, matching the spec outliner's colors; "sentence" has no spec level. */
export const LEVEL_CHIP: Record<FindingLevel, string> = {
  message: 'bg-purple-100 text-purple-900 border-purple-300',
  claim: 'bg-amber-100 text-amber-900 border-amber-300',
  section: 'bg-indigo-100 text-indigo-900 border-indigo-300',
  paragraph: 'bg-teal-100 text-teal-900 border-teal-300',
  sentence: 'bg-slate-100 text-slate-800 border-slate-300',
}

export function levelChip(level: string): string {
  return LEVEL_CHIP[level as FindingLevel] ?? 'bg-base-200 text-base-content border-base-300'
}

/** Badge text and color for an anchor state other than "ok". */
export const ANCHOR_BADGE: Record<Exclude<AnchorState, 'ok'>, { label: string; className: string; title: string }> = {
  unverified: {
    label: 'unverified',
    className: 'badge-ghost',
    title: 'The quote was not found in the file; the lines are the ones the model reported.',
  },
  moved: { label: 'moved', className: 'badge-info', title: 'The quote was found again at other lines after an edit.' },
  stale: { label: 'stale', className: 'badge-warning', title: 'The quote is no longer in the file.' },
}

export const STATUS_BADGE: Record<RunStatus, string> = {
  queued: 'badge-ghost',
  running: 'badge-info',
  done: 'badge-success',
  cached: 'badge-success',
  failed: 'badge-error',
  cancelled: 'badge-neutral',
}

// ---------------------------------------------------------------------------
// Clipboard
// ---------------------------------------------------------------------------

/**
 * Put text on the clipboard. Uses navigator.clipboard where the browser
 * allows it, else the old hidden-textarea + execCommand('copy') trick (which
 * also works on plain http). Throws when neither works.
 */
export async function copyText(text: string): Promise<'clipboard' | 'fallback'> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
      return 'clipboard'
    }
  } catch {
    // Permission denied or not focused: try the fallback below.
  }
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.opacity = '0'
  area.dataset.testid = 'copy-fallback'
  document.body.appendChild(area)
  area.select()
  let ok = false
  try {
    ok = document.execCommand('copy')
  } finally {
    area.remove()
  }
  if (!ok) throw new Error('The browser refused to copy')
  return 'fallback'
}

// ---------------------------------------------------------------------------
// Finding -> spec node
// ---------------------------------------------------------------------------

/**
 * Show a finding's `json_ref` in the Spec tab: switch tabs and select / focus
 * that row (or claim card, or the message card). A toast explains a ref that
 * is not in paper.json.
 */
export function showSpecNode(id: string): void {
  const specStore = useSpecStore.getState()
  const spec = specStore.spec
  const known = !!spec && (spec.message.id === id || !!findNode(spec, id) || !!findClaim(spec, id))
  if (!known) {
    useRunsStore.getState().showToast(`"${id}" is not a node of paper.json (any more).`, 'error')
    return
  }
  useStore.getState().setActiveTab('spec')
  specStore.requestFocus(id)
}
