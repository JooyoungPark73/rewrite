// Drag and drop for the Spec view, built on dnd-kit.
//
// <SpecDndProvider> wraps the whole app (App.tsx), not just the Spec view,
// because one gesture crosses panes: dragging an outliner paragraph onto the
// LaTeX editor inserts its `% @p <id>` marker there. The editor lives in the
// Editor tab, so hovering the dragged row over the "Editor" tab for half a
// second switches to it ("spring-loaded" tab); then drop it on the paragraph.
//
// What a drop does (vocabulary in spec/dnd.ts):
//   paragraph row -> another paragraph row    move it there (same section:
//                                             reorder; other section: before it)
//   paragraph row -> a section's header       append it to that section
//   section row   -> another section row      reorder sections
//   claim chip    -> a row or section header  link the claim (link_claim op)
//   paragraph row -> the editor               insert its marker above the
//                                             paragraph under the mouse (adopt)
// Each drop is one op, so it is one undo step.
//
// Within one section the rows slide apart to show where the dragged row will
// land (dnd-kit sortable). Across sections the target row shows a line above
// it, and a section header lights up when a drop would append to it.
//
// The collision function decides which target is "over" for each kind of
// drag, ignoring targets that are hidden (zero size, e.g. the Spec view while
// the Editor tab shows).
import {
  closestCenter,
  DndContext,
  DragOverlay,
  MeasuringStrategy,
  PointerSensor,
  pointerWithin,
  useDndContext,
  useDroppable,
  useSensor,
  useSensors,
  type CollisionDetection,
  type DragEndEvent,
  type DragMoveEvent,
  type DragOverEvent,
  type DragStartEvent,
  type DroppableContainer,
} from '@dnd-kit/core'
import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import type { EditorHandle } from '../editor/Editor'
import { flushPendingEdits, useSpecStore } from '../state/spec'
import { useStore } from '../state/store'
import {
  EDITOR_DROP_ID,
  SpecDragContext,
  useSpecDrag,
  type DragData,
  type DropData,
  type SpecDragState,
} from './dnd'
import { paragraphStartLine } from './markers'
import { claimColor, findNode, LEVEL_STYLE, locate } from './tree'

/** How long a dragged paragraph must hover the Editor tab before it opens. */
const SPRING_LOADED_TAB_MS = 450

function dropData(container: DroppableContainer): DropData | undefined {
  return container.data.current as DropData | undefined
}

/** Picks the drop target under the pointer for each kind of drag (see header). */
const specCollision: CollisionDetection = (args) => {
  const drag = args.active.data.current as DragData | undefined
  if (!drag) return []
  // Hidden targets measure 0 x 0; never let them win.
  const shown = args.droppableContainers.filter((c) => {
    const r = args.droppableRects.get(c.id)
    return !!r && r.width > 0 && r.height > 0
  })
  const pick = (test: (d: DropData) => boolean) =>
    shown.filter((c) => {
      const d = dropData(c)
      return !!d && test(d)
    })
  const within = (test: (d: DropData) => boolean) => pointerWithin({ ...args, droppableContainers: pick(test) })
  const closest = (test: (d: DropData) => boolean) => closestCenter({ ...args, droppableContainers: pick(test) })
  const overOutliner = within((d) => d.kind === 'outliner').length > 0

  if (drag.kind === 'claim') {
    return within((d) => d.kind === 'section-header' || (d.kind === 'row' && d.level === 'paragraph'))
  }
  if (drag.level === 'section') {
    return overOutliner ? closest((d) => d.kind === 'row' && d.level === 'section') : []
  }
  // A paragraph: the editor and the Editor tab first, then section headers,
  // then the nearest paragraph row.
  const away = within((d) => d.kind === 'editor' || (d.kind === 'tab' && d.tab === 'editor'))
  if (away.length) return away
  if (!overOutliner) return []
  const header = within((d) => d.kind === 'section-header')
  if (header.length) return header
  return closest((d) => d.kind === 'row' && d.level === 'paragraph')
}

/** Carry out a finished drop (one op, or a marker insertion). */
function handleDrop(drag: DragData, target: DropData, insertAtEditor: (id: string) => void): void {
  const store = useSpecStore.getState()
  const spec = store.spec
  if (!spec) return

  if (drag.kind === 'claim') {
    const nodeId = target.kind === 'row' ? target.id : target.kind === 'section-header' ? target.sectionId : null
    const node = nodeId ? findNode(spec, nodeId) : null
    if (!node) return
    if (node.claims.includes(drag.claimId)) {
      store.showToast(`${drag.claimId} is already linked to ${node.id}.`)
      return
    }
    void store.applyOp({ type: 'link_claim', id: node.id, claim_id: drag.claimId })
    return
  }

  if (target.kind === 'editor') {
    if (drag.level === 'paragraph') insertAtEditor(drag.id)
    return
  }

  const from = locate(spec, drag.id)
  if (!from) return

  if (drag.level === 'section') {
    const targetSection =
      target.kind === 'row' ? (target.level === 'section' ? target.id : target.parentId) : null
    if (!targetSection || targetSection === drag.id) return
    const index = spec.tree.children.findIndex((s) => s.id === targetSection)
    if (index < 0 || index === from.index) return
    void store.applyOp({ type: 'move', id: drag.id, parent_id: spec.tree.id, index })
    return
  }

  // A paragraph.
  if (target.kind === 'section-header') {
    const section = findNode(spec, target.sectionId)
    if (!section) return
    const same = from.parent.id === section.id
    // "Append": the last position (counting the row itself when it is already there).
    const index = same ? section.children.length - 1 : section.children.length
    if (same && from.index === index) return
    void store.applyOp({ type: 'move', id: drag.id, parent_id: section.id, index })
  } else if (target.kind === 'row' && target.level === 'paragraph' && target.id !== drag.id) {
    const to = locate(spec, target.id)
    if (!to) return
    // Same section: take the target's place (rows slid to show it). Other
    // section: go right before the target row.
    void store.applyOp({ type: 'move', id: drag.id, parent_id: to.parent.id, index: to.index })
  }
}

export function SpecDndProvider({ editorRef, children }: { editorRef: RefObject<EditorHandle | null>; children: ReactNode }) {
  // Require a 5 px move before a drag starts, so clicks on handles and chips stay clicks.
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 5 } }))
  const [drag, setDrag] = useState<SpecDragState>({ active: null, editorDrop: null })
  const pointer = useRef<{ x: number; y: number } | null>(null)
  const tabTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)

  // Track the real pointer position while dragging (capture phase, so it is
  // updated before dnd-kit handles the same move).
  const dragging = drag.active !== null
  useEffect(() => {
    if (!dragging) return
    const onMove = (e: PointerEvent) => {
      pointer.current = { x: e.clientX, y: e.clientY }
    }
    window.addEventListener('pointermove', onMove, { capture: true })
    return () => window.removeEventListener('pointermove', onMove, { capture: true })
  }, [dragging])

  /** Marker line for the point under the mouse in the editor, or null. */
  const editorLineAtPointer = (): { line: number; top: number | null } | null => {
    const editor = editorRef.current
    const point = pointer.current
    const file = useStore.getState().activeFile
    if (!editor || !point || !file) return null
    const line = editor.lineAtCoords(point.x, point.y)
    if (line === null) return null
    const start = paragraphStartLine(file.content.split('\n'), line)
    return { line: start, top: editor.lineTop(start) }
  }

  const insertAtEditor = (id: string) => {
    const file = useStore.getState().activeFile
    const target = editorLineAtPointer()
    if (!file || !file.path.endsWith('.tex') || !target) {
      useSpecStore.getState().showToast('Open a .tex file in the editor, then drop the row on its paragraph.', 'error')
      return
    }
    void useSpecStore.getState().insertMarker(id, file.path, target.line)
  }

  const finish = () => {
    clearTimeout(tabTimer.current)
    pointer.current = null
    setDrag({ active: null, editorDrop: null })
  }

  const onDragStart = (event: DragStartEvent) => {
    const data = event.active.data.current as DragData | undefined
    const start = event.activatorEvent as PointerEvent | null
    pointer.current = start && 'clientX' in start ? { x: start.clientX, y: start.clientY } : null
    // Send any half-typed text first, so it lands before the drop's op.
    flushPendingEdits()
    setDrag({ active: data ?? null, editorDrop: null })
  }

  const onDragMove = (event: DragMoveEvent) => {
    const over = event.over
    if (over && (over.data.current as DropData | undefined)?.kind === 'editor') {
      const hit = editorLineAtPointer()
      const editorDrop = hit && hit.top !== null ? { line: hit.line, top: hit.top - over.rect.top } : null
      setDrag((d) =>
        d.editorDrop?.line === editorDrop?.line && d.editorDrop?.top === editorDrop?.top ? d : { ...d, editorDrop },
      )
    } else {
      setDrag((d) => (d.editorDrop ? { ...d, editorDrop: null } : d))
    }
  }

  const onDragOver = (event: DragOverEvent) => {
    clearTimeout(tabTimer.current)
    const target = event.over?.data.current as DropData | undefined
    if (target?.kind === 'tab' && target.tab === 'editor') {
      tabTimer.current = setTimeout(() => useStore.getState().setActiveTab('editor'), SPRING_LOADED_TAB_MS)
    }
  }

  const onDragEnd = (event: DragEndEvent) => {
    const data = event.active.data.current as DragData | undefined
    const target = event.over?.data.current as DropData | undefined
    // Read the pointer before finish() clears it.
    if (data && target) handleDrop(data, target, insertAtEditor)
    finish()
  }

  return (
    <DndContext
      sensors={sensors}
      collisionDetection={specCollision}
      // Re-measure targets while dragging: the Editor tab may open mid-drag.
      measuring={{ droppable: { strategy: MeasuringStrategy.Always } }}
      onDragStart={onDragStart}
      onDragMove={onDragMove}
      onDragOver={onDragOver}
      onDragEnd={onDragEnd}
      onDragCancel={finish}
    >
      <SpecDragContext.Provider value={drag}>
        {children}
        <RemeasureOnTabSwitch />
        <DragOverlay dropAnimation={null}>
          {drag.active?.kind === 'row' && <RowPreview id={drag.active.id} />}
          {drag.active?.kind === 'claim' && <ClaimChipPreview id={drag.active.claimId} />}
        </DragOverlay>
      </SpecDragContext.Provider>
    </DndContext>
  )
}

/** When the tab changes during a drag, the drop targets moved: measure them again. */
function RemeasureOnTabSwitch() {
  const activeTab = useStore((s) => s.activeTab)
  const { active, droppableContainers, measureDroppableContainers } = useDndContext()
  const dragging = active !== null
  // Latest container list in a ref: it changes on every render, but only a
  // tab switch should trigger a re-measure.
  const latest = useRef({ droppableContainers, measureDroppableContainers })
  useEffect(() => {
    latest.current = { droppableContainers, measureDroppableContainers }
  })
  useEffect(() => {
    if (!dragging) return
    const frame = requestAnimationFrame(() => {
      const { droppableContainers: containers, measureDroppableContainers: measure } = latest.current
      measure([...containers.keys()])
    })
    return () => cancelAnimationFrame(frame)
  }, [activeTab, dragging])
  return null
}

/** The floating copy of a row that follows the mouse. */
function RowPreview({ id }: { id: string }) {
  const node = useSpecStore((s) => (s.spec ? findNode(s.spec, id) : null))
  if (!node) return null
  const style = LEVEL_STYLE[node.level]
  return (
    <div
      className={`flex max-w-md items-center gap-2 rounded border border-base-300 border-l-4 ${style.band} bg-base-100 px-2 py-1 text-sm shadow-lg`}
      data-testid="drag-preview"
    >
      <span className="text-xs text-base-content/50">{style.label}</span>
      <span className="truncate">{node.says || <i className="text-base-content/40">(empty)</i>}</span>
    </div>
  )
}

function ClaimChipPreview({ id }: { id: string }) {
  return <span className={`badge border font-mono shadow-lg ${claimColor(id)}`}>{id}</span>
}

/**
 * Transparent drop target laid over the LaTeX editor (rendered by App.tsx
 * inside the editor pane). While a paragraph row hovers it, it shows where
 * the `% @p` marker will go.
 */
export function EditorDropZone() {
  const { active, editorDrop } = useSpecDrag()
  const { setNodeRef, isOver } = useDroppable({ id: EDITOR_DROP_ID, data: { kind: 'editor' } satisfies DropData })
  const accepting = active?.kind === 'row' && active.level === 'paragraph'
  return (
    // pointer-events-none: the overlay never blocks clicks or typing in the editor.
    <div ref={setNodeRef} className="pointer-events-none absolute inset-0 z-10" data-testid="editor-drop-zone">
      {accepting && (
        <div
          className={`absolute inset-0 border-2 border-dashed ${isOver ? 'border-primary bg-primary/5' : 'border-primary/30'}`}
        />
      )}
      {accepting && isOver && editorDrop && (
        <div
          className="absolute right-0 left-0 h-0.5 bg-primary"
          style={{ top: editorDrop.top }}
          data-testid="editor-drop-indicator"
          data-line={editorDrop.line}
        >
          <span className="absolute -top-2.5 right-2 badge badge-xs badge-primary">
            % @p marker above line {editorDrop.line}
          </span>
        </div>
      )}
    </div>
  )
}
