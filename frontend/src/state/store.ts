// Application state (zustand store).
//
// One store holds everything the panes share: backend health, the open repo,
// the file list, the file open in the editor, the last compile result, and
// which tab is showing. Components read slices with `useStore(s => s.x)` and
// call actions such as `useStore.getState().save()`.
//
// Flow of the main loop:
//   edit (setContent) -> save (PUT /api/file) -> compile after 800 ms of quiet
//   -> compileResult + pdfVersion bump -> PdfViewer refetches /api/pdf.
// The Compile button skips the 800 ms wait.
//
// SyncTeX (phase 2):
//   forward  editor cursor settles on a line (300 ms, Editor.tsx) -> setCursorLine
//            -> syncForward -> GET /api/synctex/forward -> syncBoxes (drawn over
//            the PDF by pdf/SyncBoxes.tsx) + syncReveal (PdfViewer scrolls to it).
//            Skipped while the file has unsaved edits, because the line numbers
//            then no longer match the compiled PDF. The "Sync" button and
//            Ctrl/Cmd-Alt-J force it.
//   reverse  double-click on the PDF -> syncReverse(page, x, y)
//            -> GET /api/synctex/reverse -> goToLocation(file, line).
//
// Phase 4: the editor also reports its selected lines (`editorSelection`),
// which the run launcher offers as the "selection" scope. Runs, findings and
// their UI state live in their own store, state/runs.ts.
//
// Never losing typed text (audit fixes 2026-10-18):
//   save() reports its outcome ({ok, savedRevision} or {ok: false, error})
//   instead of hiding a failure. Before the editor shows another file
//   (openFile) or the backend opens another repo (openRepo / initRepo), the
//   open file is saved until everything typed is on disk; text typed while a
//   save was in flight is saved in another round. If a save fails, the switch
//   does not happen: the file (or repo) stays open with its edits and the
//   error is shown. While the backend is switching repos the editor ignores
//   typing (`switchingRepo`, Editor.tsx), because text typed into the old
//   paper at that moment could no longer be saved into it.
//
// Changed on disk (audit item 5, frontend half). The open file remembers the
// revision it was loaded from (`sha256`, from GET /api/file and from every
// successful save) and each save sends it as `base_sha256`. If the file
// changed on disk in between (another editor, a marker insertion, git pull),
// the backend answers 409 and writes nothing; the save then fails like any
// other (buffer kept, still unsaved, switches refused) and `conflict` makes
// the App show "File changed on disk" with a Reload button. Only that button
// (reloadFromDisk) replaces the buffer with the file on disk.
import { create } from 'zustand'
import * as api from '../api/client'
import type { CompileResult, FileContent, FileEntry, Health, RepoInfo, ServerEvent, SyncBox } from '../api/client'

export type Tab = 'editor' | 'pdf' | 'spec' | 'findings'

/** The file currently loaded in the editor. */
export interface OpenFile {
  path: string
  /** Current editor text (updated on every edit). */
  content: string
  /** Text as last read from or written to disk; `dirty` compares against it. */
  savedContent: string
  dirty: boolean
  mtime: number
  /**
   * Revision on disk that `savedContent` is (sha256 from the backend), sent as
   * `base_sha256` with the next save. Null when the backend reports none.
   */
  sha256: string | null
  /**
   * Bumped every time `content` changes (typing or a reload). A save reports
   * the revision it wrote (`SaveResult.savedRevision`), so callers can tell
   * whether text typed after the save started is still unsaved.
   */
  revision: number
  /**
   * Increments every time `content` is (re)loaded from the backend rather than
   * typed by the user. The Editor swaps its CodeMirror state only when this
   * changes, so ordinary typing never resets the editor.
   */
  loadId: number
}

/**
 * Outcome of save(). `ok` means the open file's text as of `savedRevision` is
 * on disk (or there was nothing unsaved); compare it with
 * `activeFile.revision` to see whether newer typing is still unsaved.
 * On failure the text stays in the editor, still marked unsaved.
 */
export type SaveResult = { ok: true; savedRevision: number } | { ok: false; error: string }

/** A save refused because the file changed on disk since it was loaded (HTTP 409). */
export interface FileConflict {
  path: string
  /** The backend's explanation. */
  detail: string
}

/** Where the PDF pane should scroll: a SyncTeX box plus a unique `seq` per request. */
export interface RevealRequest extends SyncBox {
  seq: number
}

/** Short SyncTeX status for the PDF header, e.g. "synctex: no match". */
export interface SyncStatus {
  text: string
  /** Longer explanation, shown as a tooltip. */
  detail?: string
}

/** Progress of `docker pull` (Settings dialog), fed by WebSocket events. */
export interface DockerPullState {
  status: 'idle' | 'running' | 'done' | 'failed'
  /** Output lines, oldest first (capped at DOCKER_LOG_MAX_LINES). */
  lines: string[]
  error: string | null
}

/** Lines selected in the editor (only set while the selection is not empty). */
export interface EditorSelection {
  path: string
  /** 1-based first and last selected line. */
  line_start: number
  line_end: number
}

/** A request for the editor to jump to a line; the Editor consumes and clears it. */
export interface JumpRequest {
  path: string
  line: number
  /** Unique per request so two jumps to the same line both fire. */
  seq: number
}

export interface StoreState {
  health: Health | null
  /** Set when GET /api/health fails (backend down). */
  healthError: string | null
  repo: RepoInfo | null
  /** True once the first repo lookup finished (so the UI knows whether to prompt). */
  repoChecked: boolean
  /**
   * True while POST /api/repo/open (or init) is in flight: the backend may
   * already have the new repo open while the editor still shows the old one,
   * so the editor ignores typing and spec writes are refused.
   */
  switchingRepo: boolean
  mainFile: string | null
  files: FileEntry[]
  /** The file shown in the editor (named activeFile because openFile(path) is the action). */
  activeFile: OpenFile | null
  saving: boolean
  /** 1-based line of the editor cursor (debounced). Phase 2 uses it for SyncTeX. */
  cursorLine: number
  /** The editor's non-empty selection, as whole lines (phase 4 "selection" scope). */
  editorSelection: EditorSelection | null
  compileResult: CompileResult | null
  /** True while any compile runs, including ones started by another client. */
  compiling: boolean
  /** Bumped after every finished compile; PdfViewer refetches when it changes. */
  pdfVersion: number
  activeTab: Tab
  pendingJump: JumpRequest | null
  /** Last error from a background action, shown as a dismissible toast. */
  notice: string | null
  /** Set when a save of the open file got 409 (changed on disk); cleared by a reload or a good save. */
  conflict: FileConflict | null
  /** Page count of the PDF on screen, or null when none is shown (set by PdfViewer). */
  pdfPageCount: number | null
  /** Highlight boxes from the last forward SyncTeX search (cleared on recompile). */
  syncBoxes: SyncBox[]
  /** Bumped for every new set of boxes, so the overlay can replay its fade-in. */
  syncSeq: number
  /** Set when the PDF pane should scroll a box into view. */
  syncReveal: RevealRequest | null
  syncStatus: SyncStatus | null
  dockerPull: DockerPullState

  loadHealth: () => Promise<void>
  /** Ask the backend which repo is open (if any) and load it. */
  loadRepo: () => Promise<void>
  /**
   * Open another repo. The open file is saved first; if that fails, nothing
   * is switched and the returned promise rejects with the reason (the Open
   * dialog shows it).
   */
  openRepo: (path: string) => Promise<void>
  /** Like openRepo, after running `rewrite init` on the path. */
  initRepo: (path: string) => Promise<void>
  loadFiles: () => Promise<void>
  /**
   * Show `path` in the editor. Unsaved edits of the open file are saved
   * first; resolves false (and keeps the open file, edits and all) when that
   * save fails or the file cannot be read.
   */
  openFile: (path: string) => Promise<boolean>
  setContent: (content: string) => void
  /**
   * Re-read `path` from disk if it is the open file and has no unsaved edits
   * (e.g. after the backend inserted a `% @p` marker into it). Resolves once
   * the editor has the new text, so a jump issued afterwards lands in it.
   */
  reloadFile: (path: string) => Promise<void>
  /**
   * "File changed on disk" -> Reload: replace the open file's text, unsaved
   * edits included, with the file on disk. Only called when the user asks.
   */
  reloadFromDisk: () => Promise<void>
  /** Hide the conflict banner (the edits stay, unsaved). */
  dismissConflict: () => void
  /** Save the open file if it has unsaved edits (see SaveResult). */
  save: (options?: { compileAfter?: boolean }) => Promise<SaveResult>
  /** Compile 800 ms after the last call (used after saves). */
  scheduleCompile: () => void
  /** Compile now (saving the open file first if it has unsaved changes). */
  compile: () => Promise<void>
  goToLocation: (file: string | null, line: number | null) => Promise<void>
  clearPendingJump: (seq: number) => void
  /** Called by the Editor once the cursor has rested for 300 ms; may trigger a forward sync. */
  setCursorLine: (line: number) => void
  /** Called by the Editor whenever its selection changes (null = nothing selected). */
  setEditorSelection: (selection: EditorSelection | null) => void
  setPdfPageCount: (count: number | null) => void
  /**
   * Forward SyncTeX for the open file. `line` defaults to the store's
   * cursorLine. Without `explicit`, it is skipped when the file has unsaved
   * edits or when the same file:line was the last one synced.
   */
  syncForward: (options?: { line?: number; explicit?: boolean }) => Promise<void>
  /** Reverse SyncTeX: a point on the PDF (points from the page's top-left) -> open file at line. */
  syncReverse: (page: number, x: number, y: number) => Promise<void>
  /** POST /api/compile/docker/pull; progress arrives over the WebSocket. */
  startDockerPull: () => Promise<void>
  setActiveTab: (tab: Tab) => void
  dismissNotice: () => void
  handleServerEvent: (event: ServerEvent) => void
}

export const COMPILE_DEBOUNCE_MS = 800
/**
 * How many saves in a row openFile/openRepo try while the user keeps typing
 * during each one, before giving up on the switch (the text stays open).
 */
const MAX_SAVE_ROUNDS = 5
/** Older `docker pull` lines are dropped beyond this many. */
export const DOCKER_LOG_MAX_LINES = 500

// Module-level bookkeeping that the UI never renders, so it stays out of the
// store to avoid needless re-renders.
let compileTimer: ReturnType<typeof setTimeout> | undefined
let compileInFlight = false // our own POST /api/compile is pending
let compileQueued = false // another compile was requested while one ran
let saveChain: Promise<unknown> = Promise.resolve() // serializes saves
let openToken = 0 // last openFile() call wins if several overlap
let loadCounter = 0 // source of OpenFile.loadId
let revisionCounter = 0 // source of OpenFile.revision
let jumpCounter = 0 // source of JumpRequest.seq
let filesReloadTimer: ReturnType<typeof setTimeout> | undefined
let syncToken = 0 // last syncForward() request wins if several overlap
let revealCounter = 0 // source of RevealRequest.seq
// "path:line" of the last forward sync, so resting the cursor on the same
// line again does not refetch (and re-scroll) the same boxes.
let lastForwardKey: string | null = null
// "path:line" that the last reverse sync jumped to, and when. The forward sync
// that the jump itself triggers (about 300 ms later) shows its box without
// scrolling the PDF, because the user is looking at that spot already.
let lastReverse: { key: string; at: number } | null = null
const REVERSE_ECHO_MS = 3000

/** State reset whenever a new PDF version is expected: old boxes would be stale. */
function freshPdfState(s: StoreState): Partial<StoreState> {
  lastForwardKey = null
  return { pdfVersion: s.pdfVersion + 1, syncBoxes: [], syncReveal: null, syncStatus: null }
}

/** Turn a failed SyncTeX request into a quiet header status (never a toast). */
function syncErrorStatus(err: unknown): SyncStatus {
  // 404: no SyncTeX record there, or no SyncTeX file yet; 409: no repo open.
  if (err instanceof api.ApiError && (err.status === 404 || err.status === 409)) {
    return { text: 'synctex: no match', detail: err.detail }
  }
  return { text: 'synctex: error', detail: api.errorMessage(err) }
}

/**
 * Map a path from the TeX log or a server event to a path in the file list:
 * strips "./" and the repo prefix, and adds ".tex" when TeX left it off.
 */
function resolveRepoPath(raw: string, repo: RepoInfo | null, files: FileEntry[]): string {
  let p = raw.replace(/\\/g, '/')
  if (repo && p.startsWith(repo.path.replace(/\/$/, '') + '/')) {
    p = p.slice(repo.path.replace(/\/$/, '').length + 1)
  }
  while (p.startsWith('./')) p = p.slice(2)
  const known = new Set(files.map((f) => f.path))
  if (!known.has(p) && known.has(`${p}.tex`)) p = `${p}.tex`
  return p
}

function isCompileResult(value: unknown): value is CompileResult {
  return !!value && typeof value === 'object' && Array.isArray((value as CompileResult).errors)
}

export const useStore = create<StoreState>()((set, get) => {
  /** Shared tail of openRepo/initRepo/loadRepo: reset per-repo state and load it. */
  const adoptRepo = async (repo: RepoInfo) => {
    clearTimeout(compileTimer)
    set((s) => ({
      repo,
      repoChecked: true,
      mainFile: repo.main,
      files: [],
      activeFile: null,
      conflict: null,
      editorSelection: null,
      compileResult: null,
      pendingJump: null,
      ...freshPdfState(s), // fetch this repo's existing PDF, if any
    }))
    try {
      localStorage.setItem('rewrite.lastRepo', repo.path)
    } catch {
      // Storage can be unavailable (private mode); remembering the path is optional.
    }
    await get().loadFiles()
    const main = get().mainFile
    if (main) await get().openFile(main)
    void get().loadHealth() // health also reports the open repo
  }

  /**
   * Save the open file until everything typed is on disk: if the user kept
   * typing while a save was in flight, that newer text is saved in another
   * round. Fails when a save fails (its notice is already shown) or the text
   * is still changing after MAX_SAVE_ROUNDS saves.
   */
  const saveUntilClean = async (compileAfter: boolean): Promise<SaveResult> => {
    let result: SaveResult = { ok: true, savedRevision: get().activeFile?.revision ?? 0 }
    for (let round = 0; round < MAX_SAVE_ROUNDS; round++) {
      if (!get().activeFile?.dirty) return result
      result = await get().save({ compileAfter })
      if (!result.ok) return result
    }
    if (!get().activeFile?.dirty) return result
    return { ok: false, error: 'the text kept changing while it was being saved' }
  }

  /**
   * Shared body of openRepo/initRepo. The open file is saved BEFORE the
   * backend switches: once it has the other repo open, a save would land
   * there. If the save fails, nothing is switched and the error is thrown
   * for the Open dialog to show; the current repo stays open with its edits.
   */
  const switchRepo = async (switchBackend: () => Promise<RepoInfo>) => {
    const open = get().activeFile
    const saved = await saveUntilClean(false)
    if (!saved.ok) {
      throw new Error(
        `Did not switch: ${open?.path ?? 'the open file'} could not be saved (${saved.error}). ` +
          'The current repo stays open with your unsaved edits.',
      )
    }
    set({ switchingRepo: true })
    let repo: RepoInfo
    try {
      repo = await switchBackend()
    } finally {
      set({ switchingRepo: false })
    }
    // adoptRepo replaces the (saved) buffer in the same tick, before any typing can happen.
    await adoptRepo(repo)
  }

  return {
    health: null,
    healthError: null,
    repo: null,
    repoChecked: false,
    switchingRepo: false,
    mainFile: null,
    files: [],
    activeFile: null,
    saving: false,
    cursorLine: 1,
    editorSelection: null,
    compileResult: null,
    compiling: false,
    pdfVersion: 0,
    activeTab: 'editor',
    pendingJump: null,
    notice: null,
    conflict: null,
    pdfPageCount: null,
    syncBoxes: [],
    syncSeq: 0,
    syncReveal: null,
    syncStatus: null,
    dockerPull: { status: 'idle', lines: [], error: null },

    loadHealth: async () => {
      try {
        const health = await api.getHealth()
        set({ health, healthError: null })
      } catch (err) {
        set({ health: null, healthError: api.errorMessage(err) })
      }
    },

    loadRepo: async () => {
      try {
        const repo = await api.getRepo()
        await adoptRepo(repo)
      } catch {
        // No repo open yet (or backend down): the App shows the Open dialog.
        set({ repo: null, repoChecked: true })
      }
    },

    // openRepo/initRepo let errors propagate so the Open dialog can show the
    // backend's `detail` message (or the failed save) inline.
    openRepo: (path) => switchRepo(() => api.openRepo(path)),

    initRepo: (path) => switchRepo(() => api.initRepo(path)),

    loadFiles: async () => {
      try {
        const list = await api.getFiles()
        set({ files: list.files, mainFile: list.main })
      } catch (err) {
        set({ notice: `Could not list files: ${api.errorMessage(err)}` })
      }
    },

    openFile: async (path) => {
      if (get().activeFile?.path === path) return true
      const token = ++openToken
      for (let round = 0; ; round++) {
        // Unsaved edits are saved before switching away (Overleaf-style
        // autosave), so the per-file editor cache never holds text that is not
        // on disk. The buffer is replaced only once everything typed is saved.
        const from = get().activeFile?.path
        const saved = await saveUntilClean(true)
        if (token !== openToken) return false // a later openFile() call superseded this one
        if (!saved.ok) {
          set({
            notice: `Save failed: ${saved.error}. Stayed on ${from} so its unsaved edits are kept; ${path} was not opened.`,
          })
          return false
        }
        let file: FileContent
        try {
          file = await api.getFile(path)
        } catch (err) {
          if (token === openToken) set({ notice: `Could not open ${path}: ${api.errorMessage(err)}` })
          return false
        }
        if (token !== openToken) return false
        // Typing while the new file was being fetched made the old one dirty
        // again: save that too (next round) rather than dropping it.
        if (get().activeFile?.dirty) {
          if (round + 1 < MAX_SAVE_ROUNDS) continue
          set({ notice: `${from} kept changing while it was being saved; ${path} was not opened.` })
          return false
        }
        set({
          activeFile: {
            path,
            content: file.content,
            savedContent: file.content,
            dirty: false,
            mtime: file.mtime,
            sha256: file.sha256 || null,
            loadId: ++loadCounter,
            revision: ++revisionCounter,
          },
          conflict: null,
          cursorLine: 1,
          editorSelection: null,
        })
        return true
      }
    },

    setContent: (content) => {
      const file = get().activeFile
      if (!file || file.content === content) return
      set({ activeFile: { ...file, content, dirty: content !== file.savedContent, revision: ++revisionCounter } })
    },

    reloadFile: async (path) => {
      const activeFile = get().activeFile
      if (!activeFile || activeFile.path !== path || activeFile.dirty) return
      try {
        const fresh = await api.getFile(path)
        const now = get().activeFile
        // Our own saves also trigger reloads; the content is then identical
        // and nothing happens, so the editor's undo history is kept.
        if (!now || now.path !== path || now.dirty || fresh.content === now.content) return
        set({
          activeFile: {
            ...now,
            content: fresh.content,
            savedContent: fresh.content,
            mtime: fresh.mtime,
            sha256: fresh.sha256 || null,
            loadId: ++loadCounter,
            revision: ++revisionCounter,
          },
          conflict: null,
        })
      } catch {
        // The file may have been deleted; the refreshed tree shows that.
      }
    },

    reloadFromDisk: async () => {
      const file = get().activeFile
      if (!file) return
      try {
        const fresh = await api.getFile(file.path)
        const now = get().activeFile
        if (!now || now.path !== file.path) return
        set({
          activeFile: {
            ...now,
            content: fresh.content,
            savedContent: fresh.content,
            dirty: false,
            mtime: fresh.mtime,
            sha256: fresh.sha256 || null,
            loadId: ++loadCounter, // the Editor shows the new text (fresh undo history)
            revision: ++revisionCounter,
          },
          conflict: null,
        })
      } catch (err) {
        set({ notice: `Could not reload ${file.path}: ${api.errorMessage(err)}` })
      }
    },

    dismissConflict: () => set({ conflict: null }),

    save: ({ compileAfter = true } = {}) => {
      // Chain saves so two quick Ctrl-S presses never race each other.
      const run = async (): Promise<SaveResult> => {
        const file = get().activeFile
        if (!file || !file.dirty) return { ok: true, savedRevision: file?.revision ?? 0 }
        const { content, revision } = file
        set({ saving: true })
        try {
          // Based on the revision the buffer was loaded from: 409 if it changed on disk.
          const saved = await api.putFile(file.path, content, file.sha256 ?? undefined)
          set((s) => {
            const now = s.activeFile
            if (!now || now.path !== file.path) return {}
            // The user may have kept typing while the request was in flight,
            // so the file is only clean if nothing changed since `content`.
            return {
              activeFile: {
                ...now,
                savedContent: content,
                mtime: saved.mtime,
                sha256: saved.sha256 || null,
                dirty: now.content !== content,
              },
              conflict: null,
            }
          })
          if (compileAfter) get().scheduleCompile()
          return { ok: true, savedRevision: revision }
        } catch (err) {
          // The text stays in the editor, still marked unsaved.
          const error = api.errorMessage(err)
          if (api.isFileConflict(err)) set({ conflict: { path: file.path, detail: error } })
          else set({ notice: `Save failed: ${error}` })
          return { ok: false, error }
        } finally {
          set({ saving: false })
        }
      }
      const result = saveChain.then(run, run)
      saveChain = result
      return result
    },

    scheduleCompile: () => {
      clearTimeout(compileTimer)
      compileTimer = setTimeout(() => void get().compile(), COMPILE_DEBOUNCE_MS)
    },

    compile: async () => {
      clearTimeout(compileTimer)
      if (!get().repo) return
      if (compileInFlight) {
        // Run once more when the current compile finishes, so the PDF always
        // reflects the latest save.
        compileQueued = true
        return
      }
      compileInFlight = true
      set({ compiling: true })
      try {
        if (get().activeFile?.dirty) await get().save({ compileAfter: false })
        const result = await api.compile()
        set((s) => ({ compileResult: result, ...freshPdfState(s) }))
      } catch (err) {
        set({ notice: `Compile failed: ${api.errorMessage(err)}` })
      } finally {
        compileInFlight = false
        set({ compiling: false })
        if (compileQueued) {
          compileQueued = false
          void get().compile()
        }
      }
    },

    goToLocation: async (file, line) => {
      const { repo, files, mainFile } = get()
      const path = file ? resolveRepoPath(file, repo, files) : mainFile
      if (!path) return
      set({ activeTab: 'editor' })
      if (!(await get().openFile(path))) return // open refused or failed; a notice is already shown
      set({ pendingJump: { path, line: Math.max(1, line ?? 1), seq: ++jumpCounter } })
    },

    clearPendingJump: (seq) => {
      if (get().pendingJump?.seq === seq) set({ pendingJump: null })
    },

    setCursorLine: (line) => {
      if (get().cursorLine !== line) set({ cursorLine: line })
      // Automatic forward sync. syncForward itself skips it when the file has
      // unsaved edits or this line was the last one synced; and because the
      // Editor reports only after 300 ms without typing or cursor movement,
      // this never fires in the middle of typing.
      void get().syncForward({ line })
    },

    setPdfPageCount: (count) => {
      if (get().pdfPageCount !== count) set({ pdfPageCount: count })
    },

    setEditorSelection: (selection) => {
      const now = get().editorSelection
      const same =
        now === selection ||
        (!!now &&
          !!selection &&
          now.path === selection.path &&
          now.line_start === selection.line_start &&
          now.line_end === selection.line_end)
      if (!same) set({ editorSelection: selection })
    },

    syncForward: async ({ line, explicit = false } = {}) => {
      const { activeFile, cursorLine, pdfPageCount } = get()
      if (!activeFile || !pdfPageCount) return // nothing to sync to (no PDF on screen)
      const target = Math.max(1, line ?? cursorLine)
      const key = `${activeFile.path}:${target}`
      if (!explicit && (activeFile.dirty || key === lastForwardKey)) return
      lastForwardKey = key
      // Scroll the PDF unless this sync just echoes a reverse-sync jump.
      const echo = !!lastReverse && lastReverse.key === key && Date.now() - lastReverse.at < REVERSE_ECHO_MS
      const reveal = explicit || !echo
      lastReverse = null
      const token = ++syncToken
      try {
        const boxes = await api.synctexForward(activeFile.path, target, 0)
        if (token !== syncToken) return // a newer request superseded this one
        if (boxes.length === 0) {
          set({ syncBoxes: [], syncReveal: null, syncStatus: { text: 'synctex: no match', detail: `Nothing in the PDF for ${key}` } })
          return
        }
        set((s) => ({
          syncBoxes: boxes,
          syncSeq: s.syncSeq + 1,
          syncReveal: reveal ? { ...boxes[0], seq: ++revealCounter } : s.syncReveal,
          syncStatus: null,
        }))
      } catch (err) {
        if (token !== syncToken) return
        // Forget the key so moving back to this line retries.
        lastForwardKey = null
        set({ syncBoxes: [], syncReveal: null, syncStatus: syncErrorStatus(err) })
      }
    },

    syncReverse: async (page, x, y) => {
      if (!get().repo) return
      try {
        const hit = await api.synctexReverse(page, x, y)
        const { repo, files } = get()
        lastReverse = { key: `${resolveRepoPath(hit.file, repo, files)}:${Math.max(1, hit.line)}`, at: Date.now() }
        set({ syncStatus: null })
        await get().goToLocation(hit.file, hit.line)
      } catch (err) {
        set({ syncStatus: syncErrorStatus(err) })
      }
    },

    startDockerPull: async () => {
      if (get().dockerPull.status === 'running') return
      set({ dockerPull: { status: 'running', lines: [], error: null } })
      try {
        const res = await api.dockerPull()
        if (!res.started) {
          set({ dockerPull: { ...get().dockerPull, status: 'failed', error: 'The backend did not start the pull.' } })
        }
      } catch (err) {
        set({ dockerPull: { ...get().dockerPull, status: 'failed', error: api.errorMessage(err) } })
      }
    },

    setActiveTab: (tab) => set({ activeTab: tab }),

    dismissNotice: () => set({ notice: null }),

    handleServerEvent: (event) => {
      switch (event.type) {
        case 'compile.started':
          set({ compiling: true })
          break
        case 'compile.finished':
          // Our own compile() applies its result from the HTTP response; this
          // branch covers compiles started elsewhere (another tab, a watcher).
          if (!compileInFlight) {
            set((s) => ({
              compiling: false,
              ...freshPdfState(s),
              compileResult: isCompileResult(event.result) ? event.result : s.compileResult,
            }))
          }
          break
        case 'docker.pull': {
          if (typeof event.line !== 'string') break
          const pull = get().dockerPull
          // A pull started from another tab also shows up here.
          const lines = pull.status === 'running' ? [...pull.lines, event.line] : [event.line]
          set({
            dockerPull: { status: 'running', lines: lines.slice(-DOCKER_LOG_MAX_LINES), error: null },
          })
          break
        }
        case 'docker.pull.finished': {
          const pull = get().dockerPull
          set({
            dockerPull: {
              lines: pull.lines,
              status: event.ok ? 'done' : 'failed',
              error: event.ok ? null : (event.error ?? 'docker pull failed'),
            },
          })
          break
        }
        case 'file.changed': {
          const { repo, files, activeFile } = get()
          if (typeof event.path !== 'string') break
          const path = resolveRepoPath(event.path, repo, files)
          // A new or removed \input changes the tree; refresh it (coalesced).
          if (path.endsWith('.tex')) {
            clearTimeout(filesReloadTimer)
            filesReloadTimer = setTimeout(() => void get().loadFiles(), 300)
          }
          // Reload the open file if it changed on disk and has no unsaved edits.
          if (activeFile && activeFile.path === path && !activeFile.dirty) void get().reloadFile(path)
          break
        }
      }
    },
  }
})
