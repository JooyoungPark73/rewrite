// App: the top-level layout.
//
//   header   repo path (click to open another repo), tabs, tool health chips,
//            live-connection dot, Settings (gear), Compile button
//   main     Editor tab: file tree | editor + Problems panel | findings column | PDF
//            PDF tab:    the PDF alone
//            Spec tab:   the paper.json editor (spec/SpecView.tsx, phase 3)
//            Findings:   run launcher, runs, findings (findings/FindingsView.tsx, phase 4)
//
// On screens narrower than 1024px (Tailwind's `lg` breakpoint) the PDF is
// shown only in the PDF tab. Panes are hidden with CSS rather than unmounted,
// so the editor's undo history, the rendered PDF and the Spec view's state
// survive tab switches.
//
// Phase 3: the whole layout sits inside <SpecDndProvider> (spec/SpecDnd.tsx)
// because one drag crosses tabs: an outliner paragraph dragged onto the
// "Editor" tab opens it, and dropping it on the editor (<EditorDropZone>)
// inserts the node's `% @p` marker. Server events go to both stores.
//
// SyncTeX (phase 2) is wired here: the PdfViewer's double-click goes to
// store.syncReverse, its page overlay draws <SyncBoxes>, and the "Sync"
// button / Ctrl-Cmd-Alt-J call store.syncForward for the editor's cursor line.
//
// Review runs (phase 4): server events also go to the runs store
// (state/runs.ts), which is (re)loaded when a repo opens and refetches the
// findings of the file open in the editor whenever that file changes. The
// findings column (findings/FindingsSidebar.tsx) sits between the editor and
// the PDF on wide screens, toggled by the "Findings" button above the editor;
// the PDF narrows while it is open. `j` / `k` anywhere outside a text box
// step through the findings on screen (phase 6: through the merged entries
// when a run group is shown in the Findings tab).
//
// Phase 5: the "Git" button in the header (with the number of changed files)
// opens the git panel (git/GitPanel.tsx), a drawer laid over the right side
// so nothing underneath unmounts. The blind A/B compare dialog
// (compare/CompareDialog.tsx) and the editor's selection menu
// (editor/ContextMenu.tsx) are rendered here too; server events also go to
// the git and compare stores.
//
// "File changed on disk" (audit fixes 2026-10-18): when a save is refused
// because the open file changed on disk since it was loaded (state/store.ts,
// `conflict`), a yellow bar under the header says so. The edits stay in the
// editor; "Reload from disk" replaces them with the file on disk, and only
// when the user clicks it.
import { useEffect, useRef, useState } from 'react'
import { errorMessage, type Health, type ServerEvent, type ToolAvailability } from './api/client'
import { useServerEvents, type SocketStatus } from './api/ws'
import CompareDialog from './compare/CompareDialog'
import ErrorList from './compile/ErrorList'
import ContextMenu from './editor/ContextMenu'
import Editor, { type EditorHandle } from './editor/Editor'
import FileTree from './editor/FileTree'
import FindingsSidebar from './findings/FindingsSidebar'
import FindingsView, { RunsToast } from './findings/FindingsView'
import GitPanel from './git/GitPanel'
import PdfViewer from './pdf/PdfViewer'
import SyncBoxes from './pdf/SyncBoxes'
import SettingsDialog from './settings/SettingsDialog'
import { useTabDropTarget } from './spec/dnd'
import { EditorDropZone, SpecDndProvider } from './spec/SpecDnd'
import SpecView, { SpecToast } from './spec/SpecView'
import { useCompareStore } from './state/compare'
import { useGitStore } from './state/git'
import { useRunsStore, useVisibleFileFindings } from './state/runs'
import { useSpecStore } from './state/spec'
import { useStore, type Tab } from './state/store'

const TABS: { id: Tab; label: string }[] = [
  { id: 'editor', label: 'Editor' },
  { id: 'pdf', label: 'PDF' },
  { id: 'spec', label: 'Spec' },
  { id: 'findings', label: 'Findings' },
]

const TOOLS: (keyof ToolAvailability)[] = ['latexmk', 'docker', 'claude', 'codex', 'git']

const LAST_REPO_KEY = 'rewrite.lastRepo'

const PDF_TOOLBAR_TITLE = 'Double-click a spot in the PDF to open its source line in the editor (SyncTeX).'

/** Draws the SyncTeX highlight boxes over one PDF page (see pdf/SyncBoxes.tsx). */
const renderSyncOverlay = (page: number, scale: number) => <SyncBoxes page={page} scale={scale} />

export default function App() {
  const activeTab = useStore((s) => s.activeTab)
  const setActiveTab = useStore((s) => s.setActiveTab)
  const repo = useStore((s) => s.repo)
  const repoChecked = useStore((s) => s.repoChecked)
  const health = useStore((s) => s.health)
  const healthError = useStore((s) => s.healthError)
  const compiling = useStore((s) => s.compiling)
  const compile = useStore((s) => s.compile)
  const openPath = useStore((s) => s.activeFile?.path ?? null)
  const dirty = useStore((s) => s.activeFile?.dirty ?? false)
  const saving = useStore((s) => s.saving)
  const notice = useStore((s) => s.notice)
  const dismissNotice = useStore((s) => s.dismissNotice)
  const conflict = useStore((s) => s.conflict)
  const pdfVersion = useStore((s) => s.pdfVersion)
  const handleServerEvent = useStore((s) => s.handleServerEvent)
  const pdfPageCount = useStore((s) => s.pdfPageCount)
  const setPdfPageCount = useStore((s) => s.setPdfPageCount)
  const syncReverse = useStore((s) => s.syncReverse)
  const syncReveal = useStore((s) => s.syncReveal)
  const syncStatus = useStore((s) => s.syncStatus)
  const findingsOpen = useRunsStore((s) => s.sidebarOpen)
  const setFindingsOpen = useRunsStore((s) => s.setSidebarOpen)
  const fileFindingCount = useVisibleFileFindings().length
  const gitOpen = useGitStore((s) => s.open)
  const setGitOpen = useGitStore((s) => s.setOpen)
  const gitChanges = useGitStore((s) => (s.status?.is_repo ? s.status.files.length : null))
  const compareOpen = useCompareStore((s) => s.target !== null)

  // The Open dialog shows when the user asks for it, and always while no repo is open.
  const [dialogRequested, setDialogRequested] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)

  // The editor's live cursor line, for the Sync button and shortcut.
  const editorRef = useRef<EditorHandle>(null)

  // Live events from the backend (compile started/finished, file changed,
  // spec changed) go to both stores.
  const socket = useServerEvents((event: ServerEvent) => {
    handleServerEvent(event)
    useSpecStore.getState().handleServerEvent(event)
    useRunsStore.getState().handleServerEvent(event)
    useGitStore.getState().handleServerEvent(event)
    useCompareStore.getState().handleServerEvent(event)
  })

  // Phase 4: load passes, runners, runs and findings whenever a repo opens,
  // and the findings of the file open in the editor whenever it changes.
  const repoPath = repo?.path ?? null
  useEffect(() => {
    if (repoPath) void useRunsStore.getState().loadAll()
  }, [repoPath])
  // Phase 5: git status (for the header badge) whenever a repo opens; a
  // compare dialog left open for the previous repo is closed.
  useEffect(() => {
    if (!repoPath) return
    useGitStore.getState().reset()
    useCompareStore.getState().close()
  }, [repoPath])
  useEffect(() => {
    if (repoPath) void useRunsStore.getState().loadFindings()
  }, [repoPath, openPath])

  // On startup: ask the backend for tool health and the open repo.
  useEffect(() => {
    const { loadHealth, loadRepo } = useStore.getState()
    void loadHealth()
    void loadRepo()
  }, [])

  // After the socket reconnects (e.g. the backend restarted), refresh health
  // and pick up a repo opened in the meantime.
  const socketWasDown = useRef(false)
  useEffect(() => {
    if (socket === 'closed') socketWasDown.current = true
    if (socket === 'open' && socketWasDown.current) {
      socketWasDown.current = false
      const { loadHealth, loadRepo, repo: current } = useStore.getState()
      void loadHealth()
      if (!current) void loadRepo()
      else {
        // Run events may have been missed while the socket was down.
        const runs = useRunsStore.getState()
        void runs.loadRuns()
        void runs.loadFindings()
        if (runs.selectedRunId) void runs.selectRun(runs.selectedRunId)
        // Phase 6: group and triage events too.
        void runs.loadGroups()
        if (runs.selectedGroupId) void runs.selectGroup(runs.selectedGroupId)
        // Git and compare events may have been missed too.
        void useGitStore.getState().refreshAll()
        void useCompareStore.getState().loadHistory()
      }
    }
  }, [socket])

  // Ctrl/Cmd-S anywhere on the page saves, and Ctrl/Cmd-Alt-J shows the
  // editor's cursor line in the PDF. Inside the editor CodeMirror handles
  // these keys first and marks them handled (defaultPrevented), so they do
  // not run twice.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.defaultPrevented || !(e.ctrlKey || e.metaKey)) return
      if (!e.altKey && e.key.toLowerCase() === 's') {
        e.preventDefault()
        void useStore.getState().save()
      }
      // e.code, not e.key: with Alt held, macOS reports J as "∆".
      if (e.altKey && e.code === 'KeyJ') {
        e.preventDefault()
        void useStore.getState().syncForward({ line: editorRef.current?.cursorLine() ?? undefined, explicit: true })
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  // `j` / `k`: next / previous finding in the list on screen (the findings
  // column in the Editor tab, the selected run in the Findings tab). Ignored
  // while typing in a text box or the editor, and while a dialog is open.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || (e.key !== 'j' && e.key !== 'k')) return
      const target = e.target instanceof HTMLElement ? e.target : null
      if (target && (target.isContentEditable || target.closest('input, textarea, select, .cm-editor'))) return
      if (document.querySelector('.modal-open')) return
      const tab = useStore.getState().activeTab
      if (tab !== 'editor' && tab !== 'findings') return
      e.preventDefault()
      const runs = useRunsStore.getState()
      if (e.key === 'j') runs.nextFinding()
      else runs.prevFinding()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  // PDF pane visibility: its own tab everywhere, and beside the editor on wide
  // screens (narrower while the findings column is open).
  const pdfPaneClass =
    activeTab === 'pdf'
      ? 'flex flex-1'
      : activeTab === 'editor'
        ? `hidden lg:flex ${findingsOpen ? 'lg:w-[38%]' : 'lg:w-[45%]'} lg:shrink-0`
        : 'hidden'

  return (
    <SpecDndProvider editorRef={editorRef}>
    <div className="flex h-screen flex-col bg-base-100 text-base-content">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-base-300 px-3 py-1.5">
        <span className="font-semibold whitespace-nowrap">rewrite</span>
        <button
          type="button"
          className="btn max-w-md truncate font-mono btn-ghost btn-sm"
          onClick={() => setDialogRequested(true)}
          title={repo ? `${repo.path} (click to open another repo)` : 'Open a repo'}
          data-testid="repo-path"
        >
          {repo?.path ?? 'Open repo…'}
        </button>

        <div role="tablist" className="tabs-box tabs tabs-sm">
          {TABS.map((tab) => (
            <TabButton
              key={tab.id}
              tab={tab.id}
              label={tab.label}
              selected={activeTab === tab.id}
              onSelect={() => setActiveTab(tab.id)}
              marker={tab.id === 'editor' && dirty}
            />
          ))}
        </div>

        <div className="ml-auto flex items-center gap-2">
          <HealthChips health={health} error={healthError} />
          <SocketDot status={socket} />
          <button
            type="button"
            className={`btn btn-sm ${gitOpen ? 'btn-active' : 'btn-ghost'}`}
            onClick={() => setGitOpen(!gitOpen)}
            disabled={!repo}
            aria-pressed={gitOpen}
            title="Git: status, diff, commit, push / pull, flag major revision"
            data-testid="git-button"
          >
            Git
            {gitChanges !== null && (
              <span
                className={`badge badge-xs ${gitChanges > 0 ? 'badge-warning' : 'badge-ghost'}`}
                title={`${gitChanges} changed file${gitChanges === 1 ? '' : 's'}`}
                data-testid="git-badge"
              >
                {gitChanges}
              </span>
            )}
          </button>
          <button
            type="button"
            className="btn btn-square btn-ghost btn-sm text-lg"
            onClick={() => setSettingsOpen(true)}
            disabled={!repo}
            title="Settings: compiler (native or Docker) and TeX Live year"
            aria-label="Settings"
            data-testid="settings-button"
          >
            ⚙
          </button>
          <button
            type="button"
            className="btn btn-sm btn-primary"
            onClick={() => void compile()}
            disabled={!repo}
            title="Save and compile now"
          >
            {compiling && <span className="loading loading-xs loading-spinner" />}
            Compile
          </button>
        </div>
      </header>

      {notice && (
        <div role="alert" className="alert rounded-none py-1 text-sm alert-soft alert-error">
          <span>{notice}</span>
          <button type="button" className="btn btn-ghost btn-xs" onClick={dismissNotice} aria-label="Dismiss">
            ✕
          </button>
        </div>
      )}

      {conflict && conflict.path === openPath && (
        <div
          role="alert"
          className="alert rounded-none py-1 text-sm alert-soft alert-warning"
          title={conflict.detail}
          data-testid="file-conflict"
        >
          <span>
            <b>File changed on disk:</b> <span className="font-mono">{conflict.path}</span> was changed outside
            this editor after it was loaded, so your edits were not saved. They are still in the editor; copy
            what you want to keep, then reload to see the file as it is now.
          </span>
          <button
            type="button"
            className="btn btn-xs btn-warning"
            onClick={() => void useStore.getState().reloadFromDisk()}
            title="Replace the editor text, including your unsaved edits, with the file on disk"
            data-testid="conflict-reload"
          >
            Reload from disk
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-xs"
            onClick={() => useStore.getState().dismissConflict()}
            aria-label="Dismiss"
          >
            ✕
          </button>
        </div>
      )}

      <main className="flex min-h-0 flex-1">
        {/* Editor tab: file tree, editor and the Problems panel. */}
        <div className={`${activeTab === 'editor' ? 'flex' : 'hidden'} min-w-0 flex-1`}>
          <aside className="w-56 shrink-0 border-r border-base-300">
            <FileTree />
          </aside>
          <div className="flex min-w-0 flex-1 flex-col">
            <div className="flex items-center gap-2 border-b border-base-300 px-3 py-1 text-sm">
              <span className="truncate font-mono" data-testid="open-file-name">
                {openPath ?? ''}
              </span>
              {dirty && (
                <span className="text-warning" title="Unsaved changes (Ctrl/Cmd-S to save)" data-testid="dirty-marker">
                  ● unsaved
                </span>
              )}
              {saving && <span className="text-base-content/50">saving…</span>}
              <button
                type="button"
                className="btn ml-auto btn-ghost btn-xs"
                onClick={() =>
                  void useStore
                    .getState()
                    .syncForward({ line: editorRef.current?.cursorLine() ?? undefined, explicit: true })
                }
                disabled={!openPath || !pdfPageCount}
                title="Show the cursor line in the PDF (Ctrl/Cmd-Alt-J). Also happens by itself when the cursor rests on a line of a saved file."
                data-testid="sync-forward"
              >
                Sync →
              </button>
              <button
                type="button"
                className={`btn hidden btn-xs lg:inline-flex ${findingsOpen ? 'btn-active' : 'btn-ghost'}`}
                onClick={() => setFindingsOpen(!findingsOpen)}
                disabled={!repo}
                aria-pressed={findingsOpen}
                title={`${findingsOpen ? 'Hide' : 'Show'} the review findings of this file`}
                data-testid="findings-toggle"
              >
                Findings
                <span className={`badge badge-xs ${fileFindingCount > 0 ? 'badge-warning' : 'badge-ghost'}`}>
                  {fileFindingCount}
                </span>
              </button>
            </div>
            <div className="relative min-h-0 flex-1">
              <Editor ref={editorRef} />
              <EditorDropZone />
            </div>
            <ErrorList />
          </div>
          {/* Phase 4: review findings of the open file (wide screens; the Findings tab otherwise). */}
          {repo && findingsOpen && (
            <FindingsSidebar
              className="hidden w-80 shrink-0 border-l border-base-300 lg:flex"
              onClose={() => setFindingsOpen(false)}
            />
          )}
        </div>

        {/* PDF: its own tab, plus the right-hand split on wide screens. */}
        <div className={`${pdfPaneClass} min-w-0 flex-col border-l border-base-300`}>
          <PdfViewer
            version={repo ? pdfVersion : null}
            onPageDoubleClick={(page, x, y) => void syncReverse(page, x, y)}
            renderPageOverlay={renderSyncOverlay}
            reveal={syncReveal}
            onDocumentChange={setPdfPageCount}
            toolbarTitle={PDF_TOOLBAR_TITLE}
            status={
              syncStatus && (
                <span className="ml-2 truncate text-xs text-base-content/50" title={syncStatus.detail} data-testid="sync-status">
                  {syncStatus.text}
                </span>
              )
            }
          />
        </div>

        {/* Spec: mounted once a repo is open and kept mounted (hidden) on other tabs. */}
        {repo && (
          <div className={`${activeTab === 'spec' ? 'flex' : 'hidden'} min-w-0 flex-1`}>
            <SpecView active={activeTab === 'spec'} />
          </div>
        )}
        {/* Findings: kept mounted (hidden) like Spec, so launcher choices survive tab switches. */}
        {repo && (
          <div className={`${activeTab === 'findings' ? 'flex' : 'hidden'} min-w-0 flex-1`}>
            <FindingsView active={activeTab === 'findings'} />
          </div>
        )}
      </main>
      <SpecToast />
      <RunsToast />
      {/* Phase 5: git drawer, blind compare dialog, editor selection menu. */}
      {repo && gitOpen && <GitPanel onClose={() => setGitOpen(false)} />}
      {repo && compareOpen && <CompareDialog />}
      <ContextMenu />

      {(dialogRequested || (repoChecked && !repo)) && (
        <OpenRepoDialog canCancel={!!repo} onClose={() => setDialogRequested(false)} />
      )}
      {settingsOpen && repo && <SettingsDialog onClose={() => setSettingsOpen(false)} />}
    </div>
    </SpecDndProvider>
  )
}

/**
 * One header tab. The Editor tab is also a drop target: hovering it with a
 * dragged outliner paragraph opens it, so the row can be dropped on the
 * editor (see spec/SpecDnd.tsx).
 */
function TabButton({
  tab,
  label,
  selected,
  onSelect,
  marker,
}: {
  tab: Tab
  label: string
  selected: boolean
  onSelect: () => void
  marker: boolean
}) {
  const { setNodeRef, highlight } = useTabDropTarget(tab)
  return (
    <button
      ref={setNodeRef}
      type="button"
      role="tab"
      aria-selected={selected}
      className={`tab ${selected ? 'tab-active' : ''} ${highlight ? 'outline-2 outline-primary' : ''}`}
      onClick={onSelect}
      data-testid={`tab-${tab}`}
    >
      {label}
      {marker && <span className="ml-1 text-warning">●</span>}
    </button>
  )
}

/** Green chip = tool found on the backend's PATH, grey = missing. */
function HealthChips({ health, error }: { health: Health | null; error: string | null }) {
  if (error) {
    return (
      <span className="badge badge-sm badge-error" title={error}>
        backend offline
      </span>
    )
  }
  if (!health) return null
  return (
    <div className="hidden items-center gap-1 md:flex" data-testid="health-chips">
      {TOOLS.map((tool) => {
        const found = health.tools[tool]
        return (
          <span
            key={tool}
            className={`badge badge-sm ${found ? 'badge-success' : 'badge-ghost text-base-content/50'}`}
            title={`${tool}: ${found ? 'found on PATH' : 'not found on PATH'}`}
          >
            {tool}
          </span>
        )
      })}
    </div>
  )
}

/** Small dot for the live-event WebSocket: green open, yellow connecting, red down. */
function SocketDot({ status }: { status: SocketStatus }) {
  const color = status === 'open' ? 'status-success' : status === 'connecting' ? 'status-warning' : 'status-error'
  return <span className={`status ${color}`} title={`Live updates: ${status}`} data-testid="socket-status" />
}

/**
 * Modal asking for an absolute repo path. "Open" needs an initialized repo
 * (one with .rewrite/); "Init + Open" runs `rewrite init` first.
 * Backend errors (the `detail` string) are shown inline.
 */
function OpenRepoDialog({ canCancel, onClose }: { canCancel: boolean; onClose: () => void }) {
  const openRepo = useStore((s) => s.openRepo)
  const initRepo = useStore((s) => s.initRepo)
  const [path, setPath] = useState(() => {
    try {
      return localStorage.getItem(LAST_REPO_KEY) ?? ''
    } catch {
      return ''
    }
  })
  const [busy, setBusy] = useState<'open' | 'init' | null>(null)
  const [error, setError] = useState<string | null>(null)

  const run = async (kind: 'open' | 'init') => {
    const trimmed = path.trim()
    if (!trimmed) {
      setError('Enter the absolute path of a paper repo.')
      return
    }
    setBusy(kind)
    setError(null)
    try {
      await (kind === 'open' ? openRepo(trimmed) : initRepo(trimmed))
      onClose()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="modal modal-open" role="dialog" aria-modal="true" aria-labelledby="open-repo-title">
      <div className="modal-box">
        <h3 id="open-repo-title" className="text-lg font-bold">
          Open a paper repo
        </h3>
        <p className="py-2 text-sm text-base-content/70">
          Absolute path of a LaTeX git repo. <b>Init + Open</b> first creates its <code>.rewrite/</code> folder
          (same as <code>rewrite init</code>).
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault()
            void run('open')
          }}
        >
          <input
            autoFocus
            className="input w-full font-mono"
            placeholder="/Users/me/papers/my-paper"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            aria-label="Repo path"
          />
          {error && (
            <div role="alert" className="mt-3 alert text-sm alert-soft alert-error" data-testid="open-repo-error">
              {error}
            </div>
          )}
          <div className="modal-action">
            {canCancel && (
              <button type="button" className="btn btn-ghost" onClick={onClose}>
                Cancel
              </button>
            )}
            <button type="button" className="btn" onClick={() => void run('init')} disabled={busy !== null}>
              {busy === 'init' && <span className="loading loading-xs loading-spinner" />}
              Init + Open
            </button>
            <button type="submit" className="btn btn-primary" disabled={busy !== null}>
              {busy === 'open' && <span className="loading loading-xs loading-spinner" />}
              Open
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
