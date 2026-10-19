// ContextMenu: the editor's selection menu (phase 5).
//
// Right-click on selected text in the LaTeX editor, or press Ctrl/Cmd-. with
// text selected, and this menu opens at the mouse (or below the selection):
//
//   “the selected words…”                in p-3
//   Set as this paragraph's message
//   Lift to claim
//   Set as argument (the message's `thesis` field)
//   Compare with earlier version…
//
// When the selection is not inside an adopted paragraph (no `% @p` marker
// above it in the same paragraph), the first two items are replaced by
// "Adopt this paragraph first, then set as its message / then lift to claim",
// and Compare is unavailable. What each item does, and how the enclosing
// paragraph is found, is in editor/contextActions.ts. With nothing selected
// the browser's own menu shows instead.
//
// Keys: Up/Down move between items, Enter runs one, Escape closes (and
// returns the focus to the editor). A click anywhere else, scrolling or
// resizing the window also closes it.
import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react'
import { runMenuAction, useEditorMenuStore, type MenuAction } from './contextActions'

interface Item {
  action: MenuAction
  label: string
  hint: string
  testId: string
  disabled?: boolean
}

export default function ContextMenu() {
  const menu = useEditorMenuStore((s) => s.menu)
  const close = useEditorMenuStore((s) => s.close)
  const boxRef = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null)

  // Keep the menu inside the window: measure it, then shift it left / up if needed.
  useLayoutEffect(() => {
    if (!menu || !boxRef.current) {
      setPos(null)
      return
    }
    const { width, height } = boxRef.current.getBoundingClientRect()
    const left = Math.max(4, Math.min(menu.x, window.innerWidth - width - 4))
    const top = menu.y + height > window.innerHeight - 4 ? Math.max(4, menu.y - height) : menu.y
    setPos({ left, top })
  }, [menu])

  // Focus the first item once placed, so the keyboard works at once.
  useEffect(() => {
    if (pos) boxRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')?.focus()
  }, [pos])

  // Close on a click elsewhere, scrolling, resizing or leaving the window.
  useEffect(() => {
    if (!menu) return
    const onDown = (e: MouseEvent) => {
      if (!boxRef.current?.contains(e.target as Node)) close()
    }
    const onScroll = (e: Event) => {
      if (!boxRef.current?.contains(e.target as Node)) close()
    }
    window.addEventListener('mousedown', onDown, true)
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', close)
    window.addEventListener('blur', close)
    return () => {
      window.removeEventListener('mousedown', onDown, true)
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('resize', close)
      window.removeEventListener('blur', close)
    }
  }, [menu, close])

  if (!menu) return null

  const items: Item[] = menu.nodeId
    ? [
        { action: 'message', label: "Set as this paragraph's message", hint: `update ${menu.nodeId}'s “says”`, testId: 'ctx-set-message' },
        { action: 'claim', label: 'Lift to claim', hint: `new claim, linked to ${menu.nodeId}`, testId: 'ctx-lift-claim' },
      ]
    : [
        { action: 'message', label: '…then set as its message', hint: 'adopt, then update “says”', testId: 'ctx-adopt-message' },
        { action: 'claim', label: '…then lift to claim', hint: 'adopt, then a new linked claim', testId: 'ctx-adopt-claim' },
      ]
  items.push(
    { action: 'thesis', label: 'Set as argument', hint: 'the message card’s Argument field (`thesis` in paper.json)', testId: 'ctx-set-thesis' },
    {
      action: 'compare',
      label: 'Compare with earlier version…',
      hint: menu.nodeId ? 'blind A/B against a snapshot or commit' : 'adopt the paragraph first',
      testId: 'ctx-compare',
      disabled: !menu.nodeId,
    },
  )

  const choose = (action: MenuAction) => {
    const current = menu
    close()
    void runMenuAction(current, action)
  }

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const buttons = [...(boxRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)') ?? [])]
    const i = buttons.indexOf(document.activeElement as HTMLButtonElement)
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      const next = e.key === 'ArrowDown' ? (i + 1) % buttons.length : (i - 1 + buttons.length) % buttons.length
      buttons[next]?.focus()
    } else if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      close()
      menu.restoreFocus?.()
    } else if (e.key === 'Tab') {
      e.preventDefault() // keep the focus inside the menu
    }
  }

  const preview = menu.text.length > 70 ? `${menu.text.slice(0, 67)}…` : menu.text
  return (
    <div
      ref={boxRef}
      role="menu"
      aria-label="Selection actions"
      onKeyDown={onKeyDown}
      // Invisible until measured, so it never flashes at the wrong spot.
      style={{ left: pos?.left ?? menu.x, top: pos?.top ?? menu.y, visibility: pos ? 'visible' : 'hidden' }}
      className="fixed z-50 w-80 rounded-box border border-base-300 bg-base-100 p-1 text-sm shadow-xl"
      data-testid="editor-context-menu"
      data-node-id={menu.nodeId ?? ''}
    >
      <div className="flex items-baseline gap-2 border-b border-base-200 px-2 pt-1 pb-1.5 text-xs">
        <span className="min-w-0 flex-1 truncate text-base-content/70 italic" title={menu.text}>
          “{preview}”
        </span>
        <span className="shrink-0 font-mono text-base-content/50" data-testid="ctx-node">
          {menu.nodeId ? `in ${menu.nodeId}` : 'not in an adopted paragraph'}
        </span>
      </div>
      <ul className="menu w-full p-0">
        {!menu.nodeId && (
          <li className="menu-title px-2 py-1 text-xs" data-testid="ctx-adopt-title">
            Adopt this paragraph first
          </li>
        )}
        {items.map((item) => (
          <li key={item.testId} className={item.disabled ? 'menu-disabled' : ''}>
            <button
              type="button"
              role="menuitem"
              disabled={item.disabled}
              onClick={() => choose(item.action)}
              className="flex flex-col items-start gap-0"
              data-testid={item.testId}
            >
              <span>{item.label}</span>
              <span className="text-xs text-base-content/50">{item.hint}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}
