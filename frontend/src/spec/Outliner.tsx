// Outliner: the center column of the Spec view.
//
//   ┌ Message ─────────────────────────────────────────┐  pinned at the top
//   │ says (the paper's takeaway)                      │  (sticky)
//   │ problem | stakes | thesis                        │
//   └──────────────────────────────────────────────────┘
//   ▾ ● Introduction promise ...            section row
//       ● paragraph message ...             paragraph rows, indented
//       ● ...
//       + paragraph
//   ▸ ● Method ...                           (collapsed)
//   + section
//   Skeleton from the .tex  (unadopted headings and paragraphs; SkeletonPanel.tsx)
//
// Each row is a <Row> (Row.tsx). Drag-and-drop (dnd-kit; the handlers are in
// SpecDnd.tsx): sections form one sortable list, the paragraphs of each
// section another. A section's header line is also a drop target: drop a
// paragraph there to append it to that section, or a claim chip to link it.
import { useDroppable } from '@dnd-kit/core'
import { SortableContext, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import type { Node as SpecNode } from '../api/client'
import { LEVEL_FIELDS } from '../api/client'
import { useActiveNodeId, useSpecStore } from '../state/spec'
import { headerDropId, OUTLINER_DROP_ID, useSpecDrag, type DragData, type DropData } from './dnd'
import Row from './Row'
import SkeletonPanel from './SkeletonPanel'
import TextField from './TextField'
import { fieldLabel } from './tree'

export default function Outliner() {
  const spec = useSpecStore((s) => s.spec)
  const loaded = useSpecStore((s) => s.loaded)
  const loadError = useSpecStore((s) => s.loadError)
  const errors = useSpecStore((s) => s.errors)
  const applyOp = useSpecStore((s) => s.applyOp)
  const activeId = useActiveNodeId()
  // The whole scroll area is a (silent) drop target: drags only snap to rows
  // while the pointer is over the outliner.
  const { setNodeRef } = useDroppable({ id: OUTLINER_DROP_ID, data: { kind: 'outliner' } satisfies DropData })

  if (!spec) {
    return (
      <div className="flex flex-1 items-center justify-center p-6 text-center text-base-content/60" data-testid="outliner">
        {!loaded ? (
          <span className="loading loading-spinner" />
        ) : loadError ? (
          <span>Could not load paper.json: {loadError}</span>
        ) : (
          <span>
            paper.json is missing or invalid{errors.length ? ` (${errors.length} problem${errors.length > 1 ? 's' : ''})` : ''}.
            <br />
            Fix it in the <b>Raw JSON</b> tab.
          </span>
        )}
      </div>
    )
  }

  const sections = spec.tree.children
  const addSection = () =>
    void applyOp({ type: 'insert', parent_id: spec.tree.id, index: sections.length, level: 'section' }).then((res) => {
      if (res?.new_id) useSpecStore.getState().requestFocus(res.new_id)
    })

  return (
    <div ref={setNodeRef} className="flex min-w-0 flex-1 flex-col overflow-y-auto" data-testid="outliner">
      <MessageCard node={spec.message} />
      <div className="flex flex-col gap-1 p-2">
        <SortableContext items={sections.map((s) => s.id)} strategy={verticalListSortingStrategy}>
          {sections.map((section) => (
            <SectionBlock key={section.id} section={section} rootId={spec.tree.id} activeId={activeId} />
          ))}
        </SortableContext>
        {sections.length === 0 && (
          <p className="px-2 py-3 text-sm text-base-content/50">
            No sections yet. Adopt headings from the skeleton below, or add a planned section.
          </p>
        )}
        <div>
          <button
            type="button"
            className="btn btn-ghost btn-xs"
            // Drop focus first: Space/Enter on a still-focused button would add another one.
            onClick={(e) => {
              e.currentTarget.blur()
              addSection()
            }}
            data-testid="add-section"
          >
            + section
          </button>
        </div>
      </div>
      <SkeletonPanel />
    </div>
  )
}

/** The paper's message: pinned at the top of the outliner. */
function MessageCard({ node }: { node: SpecNode }) {
  const applyOp = useSpecStore((s) => s.applyOp)
  return (
    <div
      className="sticky top-0 z-20 border-b border-l-4 border-base-300 border-l-purple-500 bg-purple-50 px-3 py-2"
      data-testid="message-card"
    >
      <div className="mb-1 flex items-baseline gap-2">
        <span className="text-xs font-semibold tracking-wide text-purple-800 uppercase">Message</span>
        <span className="font-mono text-xs text-base-content/40">{node.id}</span>
      </div>
      <TextField
        syncKey="message:says"
        focusId={node.id}
        value={node.says}
        commit={(says) => applyOp({ type: 'update', id: node.id, says })}
        label="Message says"
        placeholder="The one takeaway a reader should leave with…"
        rows={2}
        testId="message-says"
      />
      <div className="mt-1 grid grid-cols-3 gap-2">
        {LEVEL_FIELDS.message.map((key) => (
          <TextField
            key={key}
            syncKey={`message:${key}`}
            value={node.fields[key] ?? ''}
            commit={(value) => applyOp({ type: 'update', id: node.id, fields: { [key]: value } })}
            label={fieldLabel(key)}
            caption
            rows={2}
            testId={`message-field-${key}`}
          />
        ))}
      </div>
    </div>
  )
}

/** The parts of useSortable() a drag handle needs. */
type HandleProps = Pick<ReturnType<typeof useSortable>, 'attributes' | 'listeners' | 'setActivatorNodeRef'>

/** Drag handle (⠿) shown at the left of every row. */
function DragHandle({ attributes, listeners, setActivatorNodeRef, label }: HandleProps & { label: string }) {
  return (
    <button
      type="button"
      ref={setActivatorNodeRef}
      {...attributes}
      {...listeners}
      className="flex h-6 w-4 shrink-0 cursor-grab touch-none items-center justify-center text-base-content/30 hover:text-base-content/70"
      aria-label={label}
      title="Drag to move; drop a paragraph on the editor to insert its marker"
      data-testid="drag-handle"
    >
      ⠿
    </button>
  )
}

function SectionBlock({ section, rootId, activeId }: { section: SpecNode; rootId: string; activeId: string | null }) {
  const collapsed = useSpecStore((s) => !!s.collapsed[section.id])
  const applyOp = useSpecStore((s) => s.applyOp)
  const { active } = useSpecDrag()
  const { attributes, listeners, setActivatorNodeRef, setNodeRef, transform, transition, isDragging } = useSortable({
    id: section.id,
    data: { kind: 'row', level: 'section', id: section.id, parentId: rootId } satisfies DragData,
  })
  const { setNodeRef: setHeaderRef, isOver: overHeader } = useDroppable({
    id: headerDropId(section.id),
    data: { kind: 'section-header', sectionId: section.id } satisfies DropData,
  })
  // Light up the header when dropping here would append a paragraph or link a claim.
  const headerHot =
    overHeader &&
    (active?.kind === 'claim' || (active?.kind === 'row' && active.level === 'paragraph' && active.id !== section.id))

  const addParagraph = () =>
    void applyOp({ type: 'insert', parent_id: section.id, index: section.children.length, level: 'paragraph' }).then(
      (res) => {
        if (res?.new_id) useSpecStore.getState().requestFocus(res.new_id)
      },
    )

  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Translate.toString(transform), transition }}
      className={isDragging ? 'opacity-40' : ''}
      data-testid="section-block"
      data-node-id={section.id}
    >
      <div
        ref={setHeaderRef}
        className={`rounded ${headerHot ? 'outline-2 outline-primary' : ''}`}
        data-testid="section-header"
        data-drop-hot={headerHot ? 'true' : undefined}
      >
        <Row
          node={section}
          active={activeId === section.id}
          handle={
            <DragHandle
              attributes={attributes}
              listeners={listeners}
              setActivatorNodeRef={setActivatorNodeRef}
              label={`Move section ${section.id}`}
            />
          }
        />
      </div>
      {!collapsed && (
        <div className="mt-0.5 ml-6 flex flex-col gap-0.5">
          <SortableContext items={section.children.map((p) => p.id)} strategy={verticalListSortingStrategy}>
            {section.children.map((paragraph) => (
              <ParagraphItem key={paragraph.id} node={paragraph} parentId={section.id} activeId={activeId} />
            ))}
          </SortableContext>
          <div>
            <button
              type="button"
              className="btn h-5 min-h-0 px-1 text-xs font-normal text-base-content/40 btn-ghost btn-xs"
              onClick={(e) => {
                e.currentTarget.blur()
                addParagraph()
              }}
              data-testid="add-paragraph"
            >
              + paragraph
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

function ParagraphItem({ node, parentId, activeId }: { node: SpecNode; parentId: string; activeId: string | null }) {
  const { active } = useSpecDrag()
  const { attributes, listeners, setActivatorNodeRef, setNodeRef, transform, transition, isDragging, isOver } =
    useSortable({
      id: node.id,
      data: { kind: 'row', level: 'paragraph', id: node.id, parentId } satisfies DragData,
    })
  // From another section, the row lands right above this one: draw a line there.
  const landsAbove = isOver && active?.kind === 'row' && active.level === 'paragraph' && active.parentId !== parentId
  // A claim chip over this row would link to it.
  const claimHot = isOver && active?.kind === 'claim'
  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Translate.toString(transform), transition }}
      className={`relative rounded ${isDragging ? 'opacity-40' : ''} ${claimHot ? 'outline-2 outline-primary' : ''}`}
      data-drop-hot={claimHot || landsAbove ? 'true' : undefined}
    >
      {landsAbove && <div className="absolute -top-0.5 right-0 left-0 h-0.5 bg-primary" />}
      <Row
        node={node}
        active={activeId === node.id}
        handle={
          <DragHandle
            attributes={attributes}
            listeners={listeners}
            setActivatorNodeRef={setActivatorNodeRef}
            label={`Move paragraph ${node.id}`}
          />
        }
      />
    </div>
  )
}
