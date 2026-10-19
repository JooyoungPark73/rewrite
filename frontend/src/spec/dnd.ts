// Shared drag-and-drop vocabulary for the Spec view (dnd-kit).
//
// Every draggable and droppable carries a small `data` object saying what it
// is, so one collision function (spec/SpecDnd.tsx) and one drop handler can
// tell a claim chip from an outliner row, or the editor from a section header.
//
// Draggables
//   outliner row     id = node id, data { kind: 'row', level, id, parentId }
//                    (rows are dnd-kit sortables, so they are drop targets too)
//   claim chip       id = "claim:<claim id>", data { kind: 'claim', claimId }
// Other drop targets
//   section header   "header:<section id>"  drop a paragraph = append it there;
//                                           drop a claim chip = link it
//   editor           "drop:editor"          drop a paragraph = insert its marker
//   Editor tab       "drop:tab-editor"      hover with a paragraph = switch to the editor
//   outliner area    "drop:outliner"        only tells whether the pointer is over the outliner
import { useDroppable } from '@dnd-kit/core'
import { createContext, useContext } from 'react'
import type { TreeLevel } from '../api/client'

export type DragData =
  | { kind: 'row'; id: string; level: TreeLevel; parentId: string }
  | { kind: 'claim'; claimId: string }

export type DropData =
  | { kind: 'row'; id: string; level: TreeLevel; parentId: string }
  | { kind: 'section-header'; sectionId: string }
  | { kind: 'editor' }
  | { kind: 'tab'; tab: string }
  | { kind: 'outliner' }

export const EDITOR_DROP_ID = 'drop:editor'
export const OUTLINER_DROP_ID = 'drop:outliner'
export const tabDropId = (tab: string) => `drop:tab-${tab}`
export const headerDropId = (sectionId: string) => `header:${sectionId}`
export const claimDragId = (claimId: string) => `claim:${claimId}`

/** What is being dragged right now, shared with the drop targets for highlighting. */
export interface SpecDragState {
  active: DragData | null
  /**
   * While a paragraph row hovers the editor: the line its marker would be
   * inserted above, and that line's top edge in px from the editor pane's top.
   */
  editorDrop: { line: number; top: number } | null
}

export const SpecDragContext = createContext<SpecDragState>({ active: null, editorDrop: null })

/** The current drag (null when nothing is being dragged). */
export function useSpecDrag(): SpecDragState {
  return useContext(SpecDragContext)
}

/** Makes a header tab button a drop target; the Editor tab opens when a paragraph row hovers it. */
export function useTabDropTarget(tab: string) {
  const { active } = useSpecDrag()
  const { setNodeRef, isOver } = useDroppable({ id: tabDropId(tab), data: { kind: 'tab', tab } satisfies DropData })
  const highlight = isOver && tab === 'editor' && active?.kind === 'row' && active.level === 'paragraph'
  return { setNodeRef, highlight }
}
