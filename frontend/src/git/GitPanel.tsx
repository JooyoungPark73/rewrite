// GitPanel: the "Git" drawer (phase 5, PLAN.md "Revisions and git").
//
// Opened from the "Git" button in the header; it slides over the right side
// of the page without unmounting anything underneath (the editor keeps its
// undo history, the PDF stays rendered). Top to bottom:
//
//   Git   main → origin/main  ↑1 ↓0                         ⟳  ✕
//   HEAD 8517de8 Draft the method section
//   Changes (3)                                       all · none
//     [x] M   sections/intro.tex        <- click the name: its diff below
//     [x] ??  notes/todo.md
//   Diff of sections/intro.tex  (+ green, - red, @@ blue)
//   Commit   [message..................]  [Commit 2 files]
//   Remote   [Push ↑1] [Pull]   live output of git push / pull, then ok or the error
//   Revisions [Flag major revision…] -> note -> tag rewrite/rev-<n>-<slug>
//     snapshots: tag, note, date
//   Recent commits: sha, subject, tags, date
//
// Everything is a thin view over state/git.ts, which calls the backend; the
// backend shells out to the host's git (never `git add -A`: Commit sends the
// ticked paths). Unsaved editor text is saved before a commit, so what is
// committed is what the editor shows. Escape (with the focus in the drawer)
// or ✕ closes it.
import { useEffect, useRef, useState, type ReactNode } from 'react'
import type { Commit, GitFile } from '../api/client'
import { formatDate } from '../findings/format'
import { checkedPaths, useGitStore } from '../state/git'
import { useStore } from '../state/store'

/** Badge color and tooltip for git's short status letters. */
function statusStyle(status: string): { badge: string; title: string } {
  switch (status.trim()[0]) {
    case 'M':
      return { badge: 'badge-warning', title: 'modified' }
    case 'A':
      return { badge: 'badge-success', title: 'added' }
    case 'D':
      return { badge: 'badge-error', title: 'deleted' }
    case 'R':
      return { badge: 'badge-info', title: 'renamed' }
    case '?':
      return { badge: 'badge-success badge-outline', title: 'untracked (new file)' }
    case 'U':
      return { badge: 'badge-error badge-outline', title: 'unmerged (conflict)' }
    default:
      return { badge: 'badge-ghost', title: status }
  }
}

export default function GitPanel({ onClose }: { onClose: () => void }) {
  const status = useGitStore((s) => s.status)
  const statusError = useGitStore((s) => s.statusError)
  const loading = useGitStore((s) => s.loadingStatus)
  const refreshAll = useGitStore((s) => s.refreshAll)
  const panelRef = useRef<HTMLElement>(null)

  // Focus the drawer when it opens, so Escape works at once.
  useEffect(() => {
    panelRef.current?.focus()
  }, [])

  return (
    <aside
      ref={panelRef}
      tabIndex={-1}
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          e.stopPropagation()
          onClose()
        }
      }}
      className="fixed inset-y-0 right-0 z-40 flex w-[40rem] max-w-full flex-col border-l border-base-300 bg-base-100 shadow-2xl outline-none"
      aria-label="Git"
      data-native-undo // Ctrl/Cmd-Z in its text boxes is text undo, not spec undo (SpecView.tsx)
      data-testid="git-panel"
    >
      <div className="flex items-center gap-2 border-b border-base-300 px-3 py-2">
        <h2 className="font-semibold">Git</h2>
        {status?.is_repo && <BranchLine />}
        <div className="ml-auto flex items-center gap-1">
          {loading && <span className="loading loading-xs loading-spinner text-base-content/40" />}
          <button
            type="button"
            className="btn btn-ghost btn-xs"
            onClick={() => void refreshAll()}
            title="Re-read git status, log and snapshots"
            data-testid="git-refresh"
          >
            ⟳ Refresh
          </button>
          <button type="button" className="btn btn-square btn-ghost btn-sm" onClick={onClose} aria-label="Close the Git panel" data-testid="git-close">
            ✕
          </button>
        </div>
      </div>

      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
        {statusError && !status ? (
          <p className="p-4 text-sm text-error" data-testid="git-error">
            Could not read git status: {statusError}
          </p>
        ) : status && !status.is_repo ? (
          <p className="p-4 text-sm text-base-content/70" data-testid="git-not-repo">
            The paper folder is not a git repository. Run <code>git init</code> there (or clone it from Overleaf / GitHub) to
            use this panel.
          </p>
        ) : !status ? (
          <div className="p-4">
            <span className="loading loading-spinner" />
          </div>
        ) : (
          <>
            <HeadLine />
            <ChangesSection />
            <DiffSection />
            <CommitSection />
            <RemoteSection />
            <RevisionsSection />
            <LogSection />
          </>
        )}
      </div>
    </aside>
  )
}

/** A titled block of the drawer. */
function Section({ title, extra, children, testId }: { title: ReactNode; extra?: ReactNode; children: ReactNode; testId?: string }) {
  return (
    <section className="border-b border-base-300 px-3 py-2" data-testid={testId}>
      <div className="mb-1 flex items-center gap-2">
        <h3 className="text-xs font-semibold tracking-wide text-base-content/60 uppercase">{title}</h3>
        {extra}
      </div>
      {children}
    </section>
  )
}

/** "main → origin/main ↑1 ↓0" */
function BranchLine() {
  const status = useGitStore((s) => s.status)
  if (!status) return null
  return (
    <span className="flex min-w-0 items-center gap-1 font-mono text-xs" data-testid="git-branch">
      <span className="badge badge-sm badge-neutral">{status.branch ?? '(detached HEAD)'}</span>
      {status.upstream ? (
        <span className="truncate text-base-content/60">→ {status.upstream}</span>
      ) : (
        <span className="text-base-content/50" title="No upstream branch: push needs one (git push -u origin <branch>)">
          no upstream
        </span>
      )}
      <span className={status.ahead ? 'text-warning' : 'text-base-content/40'} title="Commits to push" data-testid="git-ahead">
        ↑{status.ahead}
      </span>
      <span className={status.behind ? 'text-info' : 'text-base-content/40'} title="Commits to pull" data-testid="git-behind">
        ↓{status.behind}
      </span>
    </span>
  )
}

function HeadLine() {
  const status = useGitStore((s) => s.status)
  if (!status?.head_sha) return <p className="px-3 py-1 text-xs text-base-content/50">No commits yet.</p>
  return (
    <p className="truncate px-3 py-1 text-xs text-base-content/60" data-testid="git-head">
      HEAD <span className="font-mono">{status.head_sha.slice(0, 7)}</span> {status.head_subject}
    </p>
  )
}

// ---------------------------------------------------------------------------
// Changed files
// ---------------------------------------------------------------------------

function ChangesSection() {
  const files = useGitStore((s) => s.status?.files ?? [])
  const setAllChecked = useGitStore((s) => s.setAllChecked)
  return (
    <Section
      title={`Changes (${files.length})`}
      extra={
        files.length > 0 && (
          <span className="ml-auto flex gap-1">
            <button type="button" className="btn btn-ghost btn-xs" onClick={() => setAllChecked(true)} data-testid="git-check-all">
              all
            </button>
            <button type="button" className="btn btn-ghost btn-xs" onClick={() => setAllChecked(false)} data-testid="git-check-none">
              none
            </button>
          </span>
        )
      }
    >
      {files.length === 0 ? (
        <p className="text-sm text-base-content/50" data-testid="git-clean">
          Working tree clean: nothing to commit.
        </p>
      ) : (
        <ul className="flex flex-col" data-testid="git-files">
          {files.map((f) => (
            <FileRow key={f.path} file={f} />
          ))}
        </ul>
      )}
    </Section>
  )
}

function FileRow({ file }: { file: GitFile }) {
  const checked = useGitStore((s) => !s.unchecked[file.path])
  const selected = useGitStore((s) => s.diffPath === file.path)
  const toggleChecked = useGitStore((s) => s.toggleChecked)
  const showDiff = useGitStore((s) => s.showDiff)
  const style = statusStyle(file.status)
  return (
    <li
      className={`flex items-center gap-2 rounded px-1 py-0.5 text-sm ${selected ? 'bg-primary/10' : 'hover:bg-base-200/60'}`}
      data-testid="git-file"
      data-path={file.path}
      data-status={file.status}
    >
      <input
        type="checkbox"
        className="checkbox checkbox-xs"
        checked={checked}
        onChange={() => toggleChecked(file.path)}
        aria-label={`Include ${file.path} in the commit`}
        data-testid="git-file-check"
      />
      <span className={`badge w-7 justify-center font-mono badge-xs ${style.badge}`} title={style.title + (file.staged ? ' (staged)' : '')}>
        {file.status}
      </span>
      <button
        type="button"
        className="min-w-0 flex-1 truncate text-left font-mono text-xs hover:underline"
        onClick={() => void showDiff(selected ? null : file.path)}
        title={selected ? 'Hide the diff' : 'Show the diff'}
        data-testid="git-file-open"
      >
        {file.path}
      </button>
    </li>
  )
}

// ---------------------------------------------------------------------------
// Diff viewer
// ---------------------------------------------------------------------------

/** What a diff line is, for its color. */
function lineKind(line: string): 'meta' | 'hunk' | 'add' | 'del' | 'ctx' {
  if (/^(diff --git|index |--- |\+\+\+ |new file|deleted file|similarity|rename |old mode|new mode|Binary files)/.test(line)) return 'meta'
  if (line.startsWith('@@')) return 'hunk'
  if (line.startsWith('+')) return 'add'
  if (line.startsWith('-')) return 'del'
  return 'ctx'
}

const LINE_CLASS: Record<ReturnType<typeof lineKind>, string> = {
  meta: 'font-semibold text-base-content/50',
  hunk: 'bg-sky-50 text-sky-800',
  add: 'bg-green-50 text-green-800',
  del: 'bg-red-50 text-red-800',
  ctx: 'text-base-content/80',
}

/** A unified diff with +/- line colors (also used by nothing else yet, so it lives here). */
function DiffView({ text }: { text: string }) {
  const lines = text.replace(/\n$/, '').split('\n')
  return (
    <pre className="max-h-80 overflow-auto rounded border border-base-300 bg-base-100 py-1 font-mono text-xs leading-5" data-testid="git-diff">
      {lines.map((line, i) => {
        const kind = lineKind(line)
        return (
          <div key={i} className={`px-2 whitespace-pre-wrap ${LINE_CLASS[kind]}`} data-kind={kind}>
            {line || ' '}
          </div>
        )
      })}
    </pre>
  )
}

function DiffSection() {
  const path = useGitStore((s) => s.diffPath)
  const diff = useGitStore((s) => s.diff)
  const error = useGitStore((s) => s.diffError)
  const loading = useGitStore((s) => s.diffLoading)
  const showDiff = useGitStore((s) => s.showDiff)
  if (!path) return null
  return (
    <Section
      title={
        <>
          Diff · <span className="font-mono normal-case">{path}</span>
        </>
      }
      extra={
        <span className="ml-auto flex items-center gap-1">
          {loading && <span className="loading loading-xs loading-spinner" />}
          <button type="button" className="btn btn-ghost btn-xs" onClick={() => void showDiff(null)} aria-label="Hide the diff">
            ✕
          </button>
        </span>
      }
      testId="git-diff-section"
    >
      {error ? (
        <p className="text-sm text-error">{error}</p>
      ) : diff === null ? null : diff === '' ? (
        <p className="text-sm text-base-content/50">No textual difference.</p>
      ) : (
        <DiffView text={diff} />
      )}
    </Section>
  )
}

// ---------------------------------------------------------------------------
// Commit
// ---------------------------------------------------------------------------

function CommitSection() {
  const message = useGitStore((s) => s.message)
  const setMessage = useGitStore((s) => s.setMessage)
  const committing = useGitStore((s) => s.committing)
  const result = useGitStore((s) => s.commitResult)
  const count = useGitStore((s) => checkedPaths(s).length)
  const total = useGitStore((s) => s.status?.files.length ?? 0)
  const dirtyFile = useStore((s) => (s.activeFile?.dirty ? s.activeFile.path : null))

  const commit = async () => {
    // Commit what the editor shows: save unsaved text first, then re-read status.
    const editor = useStore.getState()
    if (editor.activeFile?.dirty) {
      await editor.save({ compileAfter: false })
      await useGitStore.getState().loadStatus()
    }
    await useGitStore.getState().commit()
  }

  const disabled = committing || !message.trim() || (count === 0 && !dirtyFile)
  return (
    <Section title="Commit" testId="git-commit-section">
      <textarea
        className="textarea w-full textarea-sm"
        rows={2}
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        onKeyDown={(e) => {
          // Ctrl/Cmd-Enter commits, as in most git GUIs.
          if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault()
            if (!disabled) void commit()
          }
        }}
        placeholder="Commit message (Ctrl/Cmd-Enter to commit)"
        aria-label="Commit message"
        data-testid="git-message"
      />
      <div className="mt-1 flex flex-wrap items-center gap-2">
        <button type="button" className="btn btn-sm btn-primary" onClick={() => void commit()} disabled={disabled} data-testid="git-commit">
          {committing && <span className="loading loading-xs loading-spinner" />}
          Commit {count} of {total} file{total === 1 ? '' : 's'}
        </button>
        {dirtyFile && (
          <span className="text-xs text-warning" title="Commit saves it first">
            {dirtyFile} has unsaved edits (saved before committing)
          </span>
        )}
        {result && (
          <span className={`text-xs ${result.kind === 'ok' ? 'text-success' : 'text-error'}`} data-testid="git-commit-result">
            {result.text}
          </span>
        )}
      </div>
    </Section>
  )
}

// ---------------------------------------------------------------------------
// Push / pull
// ---------------------------------------------------------------------------

function RemoteSection() {
  const remote = useGitStore((s) => s.remote)
  const status = useGitStore((s) => s.status)
  const startRemote = useGitStore((s) => s.startRemote)
  const running = remote?.status === 'running'
  const logRef = useRef<HTMLPreElement>(null)

  // Keep the newest output line in view.
  const lineCount = remote?.lines.length ?? 0
  useEffect(() => {
    const el = logRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [lineCount])

  return (
    <Section title="Remote" testId="git-remote-section">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => void startRemote('push')}
          disabled={running}
          title={status?.upstream ? `git push to ${status.upstream}` : 'git push'}
          data-testid="git-push"
        >
          {running && remote?.op === 'push' && <span className="loading loading-xs loading-spinner" />}
          Push{status?.ahead ? ` ↑${status.ahead}` : ''}
        </button>
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => void startRemote('pull')}
          disabled={running}
          title={status?.upstream ? `git pull from ${status.upstream}` : 'git pull'}
          data-testid="git-pull"
        >
          {running && remote?.op === 'pull' && <span className="loading loading-xs loading-spinner" />}
          Pull{status?.behind ? ` ↓${status.behind}` : ''}
        </button>
        {remote && remote.status !== 'running' && (
          <span
            className={`text-sm font-medium ${remote.status === 'done' ? 'text-success' : 'text-error'}`}
            data-testid="git-remote-result"
            data-status={remote.status}
          >
            git {remote.op}: {remote.status === 'done' ? 'ok' : (remote.error ?? 'failed')}
          </span>
        )}
      </div>
      {remote && (
        <pre
          ref={logRef}
          className="mt-2 max-h-40 min-h-10 overflow-y-auto rounded bg-neutral p-2 font-mono text-xs whitespace-pre-wrap text-neutral-content"
          data-testid="git-remote-log"
        >
          {remote.lines.length === 0 ? (running ? `git ${remote.op}…` : '(no output)') : remote.lines.join('\n')}
        </pre>
      )}
    </Section>
  )
}

// ---------------------------------------------------------------------------
// Flag major revision + snapshots
// ---------------------------------------------------------------------------

function RevisionsSection() {
  const snapshots = useGitStore((s) => s.snapshots)
  const snapshotting = useGitStore((s) => s.snapshotting)
  const result = useGitStore((s) => s.snapshotResult)
  const flagRevision = useGitStore((s) => s.flagRevision)
  const dirty = useGitStore((s) => (s.status?.files.length ?? 0) > 0)
  const [asking, setAsking] = useState(false)
  const [note, setNote] = useState('')

  const submit = async () => {
    // Like Commit: the snapshot must contain what the editor shows.
    const editor = useStore.getState()
    if (editor.activeFile?.dirty) await editor.save({ compileAfter: false })
    const snapshot = await flagRevision(note)
    if (snapshot) {
      setAsking(false)
      setNote('')
    }
  }

  return (
    <Section title="Revisions" testId="git-revisions-section">
      {asking ? (
        <form
          className="flex flex-col gap-1"
          onSubmit={(e) => {
            e.preventDefault()
            void submit()
          }}
        >
          <label className="text-xs text-base-content/70" htmlFor="git-flag-note">
            A note for this revision (it names the tag and becomes its message)
            {dirty && ' — the changed files are committed first as “Snapshot: <note>”'}
          </label>
          <div className="flex gap-2">
            <input
              id="git-flag-note"
              autoFocus
              className="input input-sm flex-1"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="e.g. first full draft"
              data-testid="git-flag-note"
            />
            <button type="submit" className="btn btn-sm btn-primary" disabled={!note.trim() || snapshotting} data-testid="git-flag-submit">
              {snapshotting && <span className="loading loading-xs loading-spinner" />}
              Tag it
            </button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setAsking(false)}>
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => setAsking(true)}
          title="Tag HEAD as rewrite/rev-<n>-<note> so paragraphs can later be compared against this version"
          data-testid="git-flag"
        >
          ⚑ Flag major revision…
        </button>
      )}
      {result && (
        <p className={`mt-1 text-xs ${result.kind === 'ok' ? 'text-success' : 'text-error'}`} data-testid="git-snapshot-result">
          {result.text}
        </p>
      )}
      <ul className="mt-2 flex flex-col gap-0.5" data-testid="git-snapshots">
        {snapshots.length === 0 && <li className="text-xs text-base-content/50">No snapshots yet.</li>}
        {snapshots.map((s) => (
          <li key={s.id} className="flex items-baseline gap-2 text-xs" data-testid="git-snapshot" data-tag={s.tag}>
            <span className="font-mono font-medium">{s.tag}</span>
            <span className="min-w-0 flex-1 truncate text-base-content/70" title={s.note}>
              {s.note}
            </span>
            <span className="shrink-0 text-base-content/50">{formatDate(s.created_at)}</span>
          </li>
        ))}
      </ul>
    </Section>
  )
}

// ---------------------------------------------------------------------------
// Recent commits
// ---------------------------------------------------------------------------

function LogSection() {
  const commits = useGitStore((s) => s.commits)
  return (
    <Section title="Recent commits" testId="git-log-section">
      <ul className="flex flex-col gap-0.5" data-testid="git-log">
        {commits.length === 0 && <li className="text-xs text-base-content/50">No commits.</li>}
        {commits.map((c) => (
          <CommitRow key={c.sha} commit={c} />
        ))}
      </ul>
    </Section>
  )
}

function CommitRow({ commit }: { commit: Commit }) {
  return (
    <li className="flex items-baseline gap-2 text-xs" data-testid="git-commit-row" data-sha={commit.sha}>
      <span className="shrink-0 font-mono text-base-content/60">{commit.sha.slice(0, 7)}</span>
      <span className="min-w-0 flex-1 truncate" title={`${commit.subject}\n${commit.author}`} data-testid="git-commit-subject">
        {commit.subject}
      </span>
      {commit.tags.map((t) => (
        <span key={t} className="badge shrink-0 font-mono badge-xs badge-accent" title="tag">
          {t}
        </span>
      ))}
      <span className="shrink-0 text-base-content/50">{formatDate(commit.date)}</span>
    </li>
  )
}
