// FindingsSidebar: the review findings of the file open in the editor.
//
// On wide screens it is a collapsible column (w-80) between the editor and
// the PDF, opened and closed with the "Findings" button above the editor. On
// narrow screens (where that column is hidden) the Findings tab shows the same
// list. Both read the runs store, so filters and the selection are shared.
//
//   Findings · sections/intro.tex  3            ×
//   [message][claim][section][paragraph][sentence]   open|all
//   j / k: next / previous
//   ┌ card ┐  (FindingCard.tsx)
//   └──────┘
//
// Clicking a card selects the finding: its lines get a strong tint in the
// editor, which scrolls to them (without taking the keyboard focus, so `j` and
// `k` keep stepping through the list). The list hides findings of a run that a
// newer run of the same pass on the same scope replaced (see state/runs.ts).
import { useRunsStore, useVisibleFileFindings } from '../state/runs'
import { useStore } from '../state/store'
import FindingCard, { FindingFilters } from './FindingCard'

export interface FindingsSidebarProps {
  /** data-testid of the root, so tests can tell the editor column from the narrow-screen copy. */
  testId?: string
  /** Shows a close button (the editor column); omitted in the Findings tab. */
  onClose?: () => void
  className?: string
}

export default function FindingsSidebar({ testId = 'findings-sidebar', onClose, className = '' }: FindingsSidebarProps) {
  const findings = useVisibleFileFindings()
  const total = useRunsStore((s) => s.fileFindings.length)
  const selectedId = useRunsStore((s) => s.selectedFindingId)
  const selectFinding = useRunsStore((s) => s.selectFinding)
  const openFinding = useRunsStore((s) => s.openFinding)
  const path = useStore((s) => s.activeFile?.path ?? null)
  const setActiveTab = useStore((s) => s.setActiveTab)

  return (
    <section className={`flex min-h-0 flex-col bg-base-200/40 ${className}`} data-testid={testId}>
      <header className="flex items-center gap-2 border-b border-base-300 px-2 py-1 text-sm">
        <span className="font-medium">Findings</span>
        <span className="truncate font-mono text-xs text-base-content/50" title={path ?? ''}>
          {path ?? ''}
        </span>
        <span className="badge badge-sm" data-testid="sidebar-count" title="Findings listed (after filters)">
          {findings.length}
        </span>
        {onClose && (
          <button
            type="button"
            className="btn ml-auto btn-square btn-ghost btn-xs"
            onClick={onClose}
            title="Hide the findings column"
            aria-label="Hide findings"
          >
            ✕
          </button>
        )}
      </header>
      <div className="flex flex-col gap-1 border-b border-base-300 px-2 py-1">
        <FindingFilters />
        <span className="text-xs text-base-content/50">
          <kbd className="kbd kbd-xs">j</kbd> / <kbd className="kbd kbd-xs">k</kbd> next / previous (outside text boxes)
        </span>
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-1.5 overflow-y-auto p-2" data-testid="sidebar-list">
        {findings.map((finding) => (
          <FindingCard
            key={finding.id}
            finding={finding}
            selected={finding.id === selectedId}
            showFile={false}
            onOpen={(f) =>
              // In the Findings tab (narrow screens) the editor must be shown first.
              useStore.getState().activeTab === 'editor'
                ? selectFinding(f.id, { reveal: true, moveCursor: true })
                : openFinding(f)
            }
          />
        ))}
        {findings.length === 0 && (
          <div className="p-3 text-center text-sm text-base-content/50" data-testid="sidebar-empty">
            {!path ? (
              'No file open.'
            ) : total > 0 ? (
              'Nothing matches the filters.'
            ) : (
              <>
                No findings in this file.{' '}
                <button type="button" className="link" onClick={() => setActiveTab('findings')}>
                  Run a pass
                </button>{' '}
                from the Findings tab.
              </>
            )}
          </div>
        )}
      </div>
    </section>
  )
}
