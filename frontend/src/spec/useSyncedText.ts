// useSyncedText: the local text of one spec input (a row's `says`, a field,
// notes, a claim's text) kept in step with the value in paper.json.
//
// While the user types, the input's own text is authoritative: it is sent to
// the backend 400 ms after the last keystroke (or at once on blur, Enter,
// Tab, undo, ...) through `commit`, and a spec reload arriving meanwhile does
// not overwrite it. Once nothing is pending or in flight, the input follows
// the server value again, so undo, redo and changes from another tab show up.
//
//   const text = useSyncedText(`says:${node.id}`, node.says, (v) =>
//     applyOp({ type: 'update', id: node.id, says: v }))
//   <input value={text.value} onChange={(e) => text.onChange(e.target.value)} onBlur={text.flush} />
//
// `key` must be unique among mounted inputs; it registers the pending edit
// with the spec store so flushPendingEdits() can send it.
//
// useFocusOnRequest(id, ref) focuses the input when the store asks for it
// (e.g. the new row after Enter, the row after a Tab/Shift-Tab level change).
import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'
import { EDIT_DEBOUNCE_MS, registerPendingEdit, unregisterPendingEdit, useSpecStore } from '../state/spec'

export interface SyncedText {
  /** What the input should show. */
  value: string
  /** Call from the input's onChange. */
  onChange: (value: string) => void
  /** Send a pending edit now (no-op when nothing is pending). */
  flush: () => void
  /** Drop a pending edit without sending it (e.g. the node is about to be deleted). */
  cancel: () => void
}

export function useSyncedText(key: string, serverValue: string, commit: (value: string) => Promise<unknown>): SyncedText {
  const [value, setValue] = useState(serverValue)
  // Bumped when an in-flight commit settles, to re-run the "follow the
  // server" effect below with the newest server value.
  const [settled, setSettled] = useState(0)

  const pending = useRef<string | null>(null) // typed but not sent yet
  const inFlight = useRef(0) // commits sent but not answered yet
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const serverRef = useRef(serverValue)
  const commitRef = useRef(commit)
  useEffect(() => {
    serverRef.current = serverValue
    commitRef.current = commit
  })

  // Follow the server value whenever nothing local is pending or in flight.
  useEffect(() => {
    if (pending.current === null && inFlight.current === 0) setValue(serverValue)
  }, [serverValue, settled])

  const flush = useCallback(() => {
    clearTimeout(timer.current)
    timer.current = undefined
    unregisterPendingEdit(key)
    const text = pending.current
    pending.current = null
    if (text === null) return
    if (text === serverRef.current) {
      setSettled((n) => n + 1)
      return
    }
    inFlight.current++
    void commitRef.current(text).finally(() => {
      inFlight.current--
      setSettled((n) => n + 1)
    })
  }, [key])

  const cancel = useCallback(() => {
    clearTimeout(timer.current)
    timer.current = undefined
    unregisterPendingEdit(key)
    pending.current = null
  }, [key])

  const onChange = useCallback(
    (next: string) => {
      setValue(next)
      pending.current = next
      clearTimeout(timer.current)
      timer.current = setTimeout(flush, EDIT_DEBOUNCE_MS)
      registerPendingEdit(key, flush)
    },
    [flush, key],
  )

  // Unmounting (row deleted, moved to another list, tab closed) sends what was typed.
  const flushRef = useRef(flush)
  useEffect(() => {
    flushRef.current = flush
  }, [flush])
  useEffect(() => () => flushRef.current(), [])

  return { value, onChange, flush, cancel }
}

/** Focus `ref` (caret at the end) when useSpecStore().requestFocus(id) is called. */
export function useFocusOnRequest(id: string | null, ref: RefObject<HTMLInputElement | HTMLTextAreaElement | null>): void {
  // Only the row being asked re-renders: every other row selects null.
  const request = useSpecStore((s) => (id !== null && s.focusRequest?.id === id ? s.focusRequest : null))
  useEffect(() => {
    const el = ref.current
    if (!request || !el) return
    el.focus()
    const end = el.value.length
    el.setSelectionRange(end, end)
    useSpecStore.getState().clearFocusRequest(request.seq)
  }, [request, ref])
}
