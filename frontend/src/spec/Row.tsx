// Row: one section or paragraph line of the outliner.
//
//   ⠿  ▾  ●  Introduction [ says ....................... ]  c1×  c3×   fields ▸   ⋯
//   |  |  |                                                |          |        menu: Lift to claim,
//   |  |  status dot (click: open the anchor in the editor)  |          |        Insert marker at the
//   |  collapse the section's paragraphs (sections only)   |          |        editor cursor,
//   drag handle (dnd-kit; see Outliner.tsx / SpecDnd.tsx)    |          |        Compare… (phase 5),
//                                  linked claims (× unlinks)  |        Reconcile (stale only),
//                                                                      Delete
//                                                      expander: the level's fields + notes
// A section anchored to a heading shows the heading text (muted) before its
// `says`, since `says` starts empty when a heading is adopted.
//
// The left color band shows the level (indigo section, teal paragraph). The
// row the editor cursor is in is tinted blue (data-active="true").
//
// Keys in the `says` input:
//   Enter        new sibling after this row (same level), focused
//   Tab          section -> paragraph (only when it has no paragraphs)
//   Shift-Tab    paragraph -> section (placed after its section)
//   Alt-Up/Down  move among siblings
//   Up/Down      focus the row above / below
//   Backspace    on an empty line: delete the row, focus the one above
//   Escape       leave the input
// Ctrl/Cmd-Z and Ctrl/Cmd-Shift-Z (undo/redo) are handled by SpecView.
import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode, useLayoutEffect } from 'react'
import type { Node as SpecNode, NodeStatus } from '../api/client'
import { LEVEL_FIELDS } from '../api/client'
import { useCompareStore } from '../state/compare'
import { useDraftStore } from '../state/drafts'
import { flushPendingEdits, useSpecStore } from '../state/spec'
import { useStore } from '../state/store'
import { paragraphStartLine } from './markers'
import TextField from './TextField'
import { claimColor, fieldLabel, findClaim, LEVEL_STYLE, locate, visibleRowIds } from './tree'
import { useFocusOnRequest, useSyncedText } from './useSyncedText'

export interface RowProps {
  node: SpecNode
  /** The drag handle element, built by the sortable wrapper (Outliner.tsx). */
  handle: ReactNode
  /** True when the editor cursor is inside this node's paragraph/section. */
  active: boolean
}

export default function Row({ node, handle, active }: RowProps) {
  const style = LEVEL_STYLE[node.level]
  const selected = useSpecStore((s) => s.selectedId === node.id)
  const expanded = useSpecStore((s) => !!s.expanded[node.id])
  const collapsed = useSpecStore((s) => !!s.collapsed[node.id])
  const toggleExpanded = useSpecStore((s) => s.toggleExpanded)
  const toggleCollapsed = useSpecStore((s) => s.toggleCollapsed)
  const isSection = node.level === 'section'

  // Bring the row the editor cursor is in into view (once the Spec tab shows).
  const rowRef = useRef<HTMLDivElement>(null)
  const specVisible = useStore((s) => s.activeTab === 'spec')
  useEffect(() => {
    if (active && specVisible) rowRef.current?.scrollIntoView({ block: 'nearest' })
  }, [active, specVisible])

  return (
    <div
      ref={rowRef}
      className={`group/row flex flex-col rounded-r border-l-4 ${style.band} ${
        active ? 'bg-sky-100' : style.tint
      } ${selected ? 'outline-1 outline-base-content/20' : ''}`}
      data-testid="spec-row"
      data-node-id={node.id}
      data-level={node.level}
      data-active={active ? 'true' : undefined}
      data-selected={selected ? 'true' : undefined}
    >
      <div className="flex min-w-0 items-center gap-1 py-0.5 pr-1 pl-0.5">
        {handle}
        {isSection ? (
          <button
            type="button"
            className="btn btn-square w-5 btn-ghost btn-xs text-base-content/60"
            onClick={() => toggleCollapsed(node.id)}
            title={collapsed ? 'Show paragraphs' : 'Hide paragraphs'}
            aria-label={collapsed ? 'Show paragraphs' : 'Hide paragraphs'}
            data-testid="row-collapse"
          >
            {collapsed ? '▸' : '▾'}
          </button>
        ) : (
          <span className="w-5" />
        )}
        <StatusDot node={node} />
        <RereadChip node={node} />
        <HeadingLabel node={node} />
        <SaysInput node={node} />
        <LinkedClaims node={node} />
        <button
          type="button"
          className={`btn btn-ghost btn-xs ${expanded ? '' : 'text-base-content/50'}`}
          onClick={() => toggleExpanded(node.id)}
          aria-expanded={expanded}
          title={`${expanded ? 'Hide' : 'Show'} ${LEVEL_FIELDS[node.level].map(fieldLabel).join(', ')} and notes`}
          data-testid="row-expander"
        >
          fields {expanded ? '▾' : '▸'}
        </button>
        <RowMenu node={node} />
      </div>
      {expanded && <FieldsEditor node={node} />}
    </div>
  )
}

/** "Introduction" for a section anchored to \section{Introduction} (or the label it is anchored to). */
function HeadingLabel({ node }: { node: SpecNode }) {
  if (node.level !== 'section' || !node.anchor) return null
  const marker = node.anchor.marker
  const m = /^\\(?:sub)?section\*?\s*(?:\[[^\]]*\])?\{(.*)\}\s*$/.exec(marker) ?? /^\\label\{(.*)\}\s*$/.exec(marker)
  const text = m ? m[1] : marker
  return (
    <span
      className="max-w-48 shrink-0 truncate text-xs text-base-content/50"
      title={`Anchored to ${marker} in ${node.anchor.file}`}
      data-testid="row-heading"
    >
      {text}
    </span>
  )
}

// ---------------------------------------------------------------------------
// The `says` line
// ---------------------------------------------------------------------------

const PLACEHOLDER: Record<string, string> = {
  section: 'What this section promises…',
  paragraph: 'What this paragraph says…',
}

function SaysInput({ node }: { node: SpecNode }) {
  const applyOp = useSpecStore((s) => s.applyOp)
  const select = useSpecStore((s) => s.select)
  const text = useSyncedText(`says:${node.id}`, node.says, (says) => applyOp({ type: 'update', id: node.id, says }))
  const inputRef = useRef<HTMLTextAreaElement>(null)
  useFocusOnRequest(node.id, inputRef)

  // The says line is a textarea that grows with its text (one visual line when it fits), so
  // a long promise or local message is readable in place. Re-measured on width changes too,
  // since wrapping depends on the width (see TextField.tsx for the same rule).
  useLayoutEffect(() => {
    const area = inputRef.current
    if (!area) return
    const grow = () => {
      area.style.height = 'auto'
      area.style.height = `${area.scrollHeight}px`
    }
    grow()
    if (typeof ResizeObserver === 'undefined') return
    let lastWidth = area.clientWidth
    let frame = 0
    // Re-measure on the next frame, not inside the callback: growing the element there
    // would resize it again in the same frame ("ResizeObserver loop" errors).
    const observer = new ResizeObserver(() => {
      if (area.clientWidth === lastWidth) return
      lastWidth = area.clientWidth
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(grow)
    })
    observer.observe(area)
    return () => {
      observer.disconnect()
      cancelAnimationFrame(frame)
    }
  }, [text.value])

  /** True when the caret sits on the first (ArrowUp) or last (ArrowDown) visual line, so the
   *  arrow should leave this row rather than move inside its wrapped text. */
  const caretAtEdge = (area: HTMLTextAreaElement, key: 'ArrowUp' | 'ArrowDown') => {
    if (area.selectionStart !== area.selectionEnd) return false
    const lines = area.value.split('\n')
    const lineHeight = parseFloat(getComputedStyle(area).lineHeight) || 20
    const visualLines = Math.max(1, Math.round(area.clientHeight / lineHeight))
    if (visualLines <= 1 && lines.length <= 1) return true
    // Measure the caret's row by the height of the text before it.
    const probe = document.createElement('div')
    const style = getComputedStyle(area)
    for (const prop of ['font', 'letterSpacing', 'lineHeight', 'padding', 'width', 'boxSizing', 'border'] as const) {
      probe.style[prop] = style[prop]
    }
    probe.style.position = 'absolute'
    probe.style.visibility = 'hidden'
    probe.style.whiteSpace = 'pre-wrap'
    probe.style.wordBreak = 'break-word'
    probe.textContent = area.value.slice(0, area.selectionStart) || ' '
    document.body.appendChild(probe)
    const caretRow = Math.max(1, Math.round(probe.clientHeight / lineHeight))
    probe.textContent = area.value || ' '
    const totalRows = Math.max(1, Math.round(probe.clientHeight / lineHeight))
    probe.remove()
    return key === 'ArrowUp' ? caretRow <= 1 : caretRow >= totalRows
  }

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.nativeEvent.isComposing) return // typing with an input method
    const store = useSpecStore.getState()
    const spec = store.spec
    const loc = spec ? locate(spec, node.id) : null
    if (!spec || !loc) return
    const plain = !e.altKey && !e.ctrlKey && !e.metaKey
    /** Run a structural op after sending the typed text, then refocus a row. */
    const structural = (op: Parameters<typeof applyOp>[0], focus: 'self' | 'new') => {
      flushPendingEdits()
      void applyOp(op).then((res) => {
        if (!res) return
        const target = focus === 'new' ? res.new_id : node.id
        if (target) store.requestFocus(target)
      })
    }

    if (e.key === 'Enter' && plain && !e.shiftKey) {
      e.preventDefault()
      if (node.level !== 'section' && node.level !== 'paragraph') return
      structural({ type: 'insert', parent_id: loc.parent.id, index: loc.index + 1, level: node.level }, 'new')
    } else if (e.key === 'Tab' && plain) {
      e.preventDefault()
      if (!e.shiftKey && node.level === 'section') {
        if (node.children.length > 0) {
          store.showToast(
            `"${node.id}" has ${node.children.length} paragraph(s); move or delete them before it can become a paragraph.`,
            'error',
          )
          return
        }
        structural({ type: 'set_level', id: node.id, level: 'paragraph' }, 'self')
      } else if (e.shiftKey && node.level === 'paragraph') {
        structural({ type: 'set_level', id: node.id, level: 'section' }, 'self')
      }
    } else if (e.altKey && !e.ctrlKey && !e.metaKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
      e.preventDefault()
      const to = loc.index + (e.key === 'ArrowUp' ? -1 : 1)
      if (to < 0 || to >= loc.parent.children.length) return
      structural({ type: 'move', id: node.id, parent_id: loc.parent.id, index: to }, 'self')
    } else if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') && plain && !e.shiftKey) {
      if (!caretAtEdge(e.currentTarget, e.key)) return // move inside the wrapped text first
      const rows = visibleRowIds(spec, store.collapsed)
      const next = rows[rows.indexOf(node.id) + (e.key === 'ArrowUp' ? -1 : 1)]
      if (next) {
        e.preventDefault()
        store.requestFocus(next)
      }
    } else if (e.key === 'Backspace' && plain && !e.shiftKey && text.value === '') {
      e.preventDefault()
      if (node.children.length > 0) {
        store.showToast(`"${node.id}" still has paragraphs; delete those first (or use ⋯ → Delete).`, 'error')
        return
      }
      const rows = visibleRowIds(spec, store.collapsed)
      const i = rows.indexOf(node.id)
      const focusAfter = rows[i - 1] ?? rows[i + 1] ?? null
      text.cancel() // the node goes away; no need to save its emptied text
      flushPendingEdits()
      void applyOp({ type: 'delete', id: node.id }).then((res) => {
        if (res && focusAfter) store.requestFocus(focusAfter)
      })
    } else if (e.key === 'Escape') {
      e.preventDefault()
      e.currentTarget.blur()
    }
  }

  return (
    <textarea
      ref={inputRef}
      rows={1}
      value={text.value}
      onChange={(e) => text.onChange(e.target.value)}
      onBlur={text.flush}
      onFocus={() => select(node.id)}
      onKeyDown={onKeyDown}
      placeholder={PLACEHOLDER[node.level] ?? ''}
      className={`min-h-7 min-w-0 flex-1 resize-none overflow-hidden rounded border border-transparent bg-transparent px-1.5 py-1 text-sm leading-5 outline-none placeholder:font-normal placeholder:text-base-content/35 hover:border-base-300 focus:border-primary focus:bg-base-100 ${
        node.level === 'section' ? 'font-semibold' : ''
      }`}
      aria-label={`${node.level} ${node.id} says`}
      data-testid="says-input"
      data-node-id={node.id}
    />
  )
}

// ---------------------------------------------------------------------------
// Status dot
// ---------------------------------------------------------------------------

const DOT_CLASS: Record<NodeStatus['status'], string> = {
  planned: 'border-2 border-base-content/40 bg-transparent',
  linked: 'bg-success',
  stale: 'bg-warning',
  orphaned: 'bg-error',
}

/** Tooltip text: the state and where the anchor is. */
function dotTitle(node: SpecNode, st: NodeStatus | undefined): string {
  const base = anchorTitle(node, st)
  return st?.reread ? `${base}\nRe-read: ${st.reread}. Edit "says" or ⋯ → Reconcile once checked.` : base
}

function anchorTitle(node: SpecNode, st: NodeStatus | undefined): string {
  const where = st?.file ? `${st.file}${st.line ? `:${st.line}` : ''}` : ''
  if (node.level === 'claim') {
    switch (st?.status) {
      case 'linked':
        return `stated at ${where}. Click to open it in the editor.`
      case 'stale':
        return `the sentence stating this claim is gone or reworded in ${where}; re-check the claim, then re-anchor or unlink it`
      case 'orphaned':
        return `${node.anchor?.file ?? 'its file'} is not in the include tree`
      default:
        return 'no sentence anchored: "Lift to claim" on the sentence that states it'
    }
  }
  switch (st?.status) {
    case 'linked':
      return `linked — ${where}. Click to open it in the editor.`
    case 'stale':
      return `stale — ${where} changed since "says" was last edited. Edit "says", or ⋯ → Reconcile if the prose is right.`
    case 'orphaned':
      return `orphaned — ${node.anchor?.marker ?? 'the marker'} not found in ${node.anchor?.file ?? st.file ?? 'the source'}`
    case 'planned':
      return 'planned — not in the .tex yet: adopt it from the skeleton, or drag the row onto the editor'
    default:
      return 'status not known yet'
  }
}

export function StatusDot({ node }: { node: SpecNode }) {
  const st = useSpecStore((s) => s.status[node.id])
  const kind = st?.status ?? (node.anchor ? null : 'planned')
  const clickable = kind === 'linked' || kind === 'stale' || kind === 'orphaned'
  const open = () => {
    if (!clickable) return
    void useStore.getState().goToLocation(st?.file ?? node.anchor?.file ?? null, st?.line ?? null)
  }
  return (
    <button
      type="button"
      onClick={open}
      disabled={!clickable}
      className={`flex h-5 w-4 shrink-0 items-center justify-center ${clickable ? 'cursor-pointer' : 'cursor-default'}`}
      title={dotTitle(node, st)}
      aria-label={dotTitle(node, st)}
      data-testid="status-dot"
      data-status={kind ?? 'unknown'}
      data-reread={st?.reread ? 'true' : undefined}
    >
      {/* A node to re-read after a change above it (NodeStatus.reread) gets the info colour
          and a ring, so it stands out from the green "linked" dots; the RereadChip next to
          the dot names the reason. */}
      <span
        className={`block h-2.5 w-2.5 rounded-full ${
          st?.reread ? 'bg-info ring-2 ring-info/40 ring-offset-1 ring-offset-base-100' : kind ? DOT_CLASS[kind] : 'bg-base-300'
        }`}
      />
    </button>
  )
}

/**
 * "re-read" next to the dot of a marked node, with the reason as its tooltip. Clicking it
 * reconciles the node ("I re-read it"), the same as ⋯ → Reconcile.
 */
export function RereadChip({ node }: { node: SpecNode }) {
  const reason = useSpecStore((s) => s.status[node.id]?.reread ?? null)
  const reconcile = useSpecStore((s) => s.reconcile)
  if (!reason) return null
  return (
    <button
      type="button"
      className="badge badge-xs badge-info badge-soft shrink-0 cursor-pointer"
      onClick={() => void reconcile(node.id)}
      title={`Re-read: ${reason}.\nThis node was written against the old version; re-read it, edit "says" if it no longer fits, or click here once checked.`}
      data-testid="reread-chip"
      data-node-id={node.id}
    >
      re-read
    </button>
  )
}

// ---------------------------------------------------------------------------
// Linked claims
// ---------------------------------------------------------------------------

function LinkedClaims({ node }: { node: SpecNode }) {
  const applyOp = useSpecStore((s) => s.applyOp)
  const spec = useSpecStore((s) => s.spec)
  if (node.claims.length === 0) return null
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-1">
      {node.claims.map((claimId) => (
        <span
          key={claimId}
          className={`badge h-5 gap-0.5 border px-1.5 font-mono badge-sm ${claimColor(claimId)}`}
          title={(spec && findClaim(spec, claimId)?.says) || claimId}
          data-testid="linked-claim"
          data-claim-id={claimId}
        >
          {claimId}
          <button
            type="button"
            className="opacity-50 hover:opacity-100"
            onClick={() => void applyOp({ type: 'unlink_claim', id: node.id, claim_id: claimId })}
            aria-label={`Unlink ${claimId} from ${node.id}`}
            title={`Unlink ${claimId}`}
          >
            ×
          </button>
        </span>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Fields + notes (the expander)
// ---------------------------------------------------------------------------

function FieldsEditor({ node }: { node: SpecNode }) {
  const applyOp = useSpecStore((s) => s.applyOp)
  // The level's fields first, then any other key found in the file.
  const keys = [...LEVEL_FIELDS[node.level], ...Object.keys(node.fields).filter((k) => !LEVEL_FIELDS[node.level].includes(k))]
  return (
    <div className="grid gap-2 py-2 pr-2 pl-12 sm:grid-cols-2" data-testid="row-fields">
      {keys.map((key) => (
        <TextField
          key={key}
          syncKey={`field:${node.id}:${key}`}
          value={node.fields[key] ?? ''}
          commit={(value) => applyOp({ type: 'update', id: node.id, fields: { [key]: value } })}
          label={fieldLabel(key)}
          caption
          rows={2}
          testId={`field-${key}`}
        />
      ))}
      <TextField
        syncKey={`notes:${node.id}`}
        value={node.notes}
        commit={(notes) => applyOp({ type: 'update', id: node.id, notes })}
        label="notes"
        caption
        rows={2}
        testId="notes-input"
        placeholder="Promote/demote moves displaced fields here."
      />
    </div>
  )
}

// ---------------------------------------------------------------------------
// The "⋯" menu
// ---------------------------------------------------------------------------

function RowMenu({ node }: { node: SpecNode }) {
  const [open, setOpen] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)
  const status = useSpecStore((s) => s.status[node.id]?.status)
  const reread = useSpecStore((s) => s.status[node.id]?.reread ?? null)
  const markerWithSays = useSpecStore((s) => s.markerWithSays)

  // Close when clicking anywhere else, or on Escape.
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!boxRef.current?.contains(e.target as globalThis.Node)) {
        setOpen(false)
        setConfirmDelete(false)
      }
    }
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === 'Escape') {
        setOpen(false)
        setConfirmDelete(false)
      }
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const close = () => {
    setOpen(false)
    setConfirmDelete(false)
  }
  const store = () => useSpecStore.getState()

  const liftToClaim = () => {
    close()
    flushPendingEdits()
    void store().applyOp({ type: 'lift_to_claim', id: node.id })
  }

  const insertAtCursor = () => {
    close()
    const { activeFile, cursorLine } = useStore.getState()
    if (!activeFile || !activeFile.path.endsWith('.tex')) {
      store().showToast('Open the .tex file in the editor and put the cursor in the paragraph first.', 'error')
      return
    }
    flushPendingEdits()
    const line = paragraphStartLine(activeFile.content.split('\n'), cursorLine)
    void store().insertMarker(node.id, activeFile.path, line)
  }

  const reconcile = () => {
    close()
    void store().reconcile(node.id)
  }

  // Phase 5: blind A/B compare of this paragraph against a snapshot or commit.
  const canCompare = node.level === 'paragraph' && !!node.anchor && (status === 'linked' || status === 'stale')
  // Phase 7: draft this paragraph from its spec entry (any paragraph; planned ones insert).
  const canDraft = node.level === 'paragraph'
  const draft = () => {
    close()
    useDraftStore.getState().openFor({
      nodeId: node.id,
      says: node.says,
      anchored: !!node.anchor && (status === 'linked' || status === 'stale'),
      file: node.anchor?.file ?? null,
    })
  }
  const compare = () => {
    close()
    if (!node.anchor) return
    useCompareStore.getState().openFor({ file: node.anchor.file, node_id: node.id, says: node.says })
  }

  const remove = () => {
    if (node.children.length > 0 && !confirmDelete) {
      setConfirmDelete(true)
      return
    }
    close()
    flushPendingEdits()
    void store().applyOp({ type: 'delete', id: node.id })
  }

  return (
    <div className="relative" ref={boxRef}>
      <button
        type="button"
        className="btn btn-square btn-ghost btn-xs"
        onClick={() => (open ? close() : setOpen(true))}
        aria-label={`Actions for ${node.id}`}
        aria-expanded={open}
        title="More actions"
        data-testid="row-menu-button"
      >
        ⋯
      </button>
      {open && (
        <ul
          className="menu absolute top-full right-0 z-30 w-72 rounded-box border border-base-300 bg-base-100 p-1 text-sm shadow-lg"
          role="menu"
          data-testid="row-menu"
        >
          <li>
            <button type="button" role="menuitem" onClick={liftToClaim} data-testid="menu-lift">
              Lift to claim
              <span className="text-xs text-base-content/50">copies “says” into the claim bank</span>
            </button>
          </li>
          {node.level === 'paragraph' && (
            <>
              <li>
                <button type="button" role="menuitem" onClick={insertAtCursor} data-testid="menu-insert-marker">
                  Insert marker into editor
                  <span className="text-xs text-base-content/50">above the paragraph at the editor cursor</span>
                </button>
              </li>
              <li>
                <label className="flex items-center gap-2" title="Also used when you drag a row onto the editor">
                  <input
                    type="checkbox"
                    className="checkbox checkbox-xs"
                    checked={markerWithSays}
                    onChange={(e) => store().setMarkerWithSays(e.target.checked)}
                    data-testid="menu-marker-says"
                  />
                  with “says” as a comment
                </label>
              </li>
            </>
          )}
          {canDraft && (
            <li>
              <button type="button" role="menuitem" onClick={draft} data-testid="menu-draft">
                Draft from spec…
                <span className="text-xs text-base-content/50">a model writes this paragraph from its says, function and claims; you decide what goes in</span>
              </button>
            </li>
          )}
          {canCompare && (
            <li>
              <button type="button" role="menuitem" onClick={compare} data-testid="menu-compare">
                Compare…
                <span className="text-xs text-base-content/50">blind A/B against a snapshot or commit</span>
              </button>
            </li>
          )}
          {(status === 'stale' || reread) && (
            <li>
              <button type="button" role="menuitem" onClick={reconcile} data-testid="menu-reconcile">
                Reconcile
                <span className="text-xs text-base-content/50">
                  {status === 'stale' && reread
                    ? `the prose is right and I re-read it (${reread})`
                    : status === 'stale'
                      ? 'the prose is right; clear “stale”'
                      : `I re-read it (${reread})`}
                </span>
              </button>
            </li>
          )}
          <li>
            <button
              type="button"
              role="menuitem"
              className={confirmDelete ? 'bg-error text-error-content' : 'text-error'}
              onClick={remove}
              data-testid="menu-delete"
            >
              {confirmDelete ? `Delete with its ${node.children.length} paragraph(s)?` : 'Delete'}
            </button>
          </li>
        </ul>
      )}
    </div>
  )
}
