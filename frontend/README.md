# rewrite frontend

Vite + React + TypeScript, Tailwind v4 + DaisyUI 5, CodeMirror 6, pdf.js.
See `../PLAN.md` for what the app is; this folder is only the browser UI.

## Commands

| What | Command |
| --- | --- |
| Install | `npm install` (first time also `npx playwright install chromium` for e2e) |
| Dev server (needs the backend on 127.0.0.1:8000) | `npm run dev`, then open http://127.0.0.1:5173 |
| Dev against the mock backend, no Python needed | `npm run mock` in one terminal, `npm run dev:mock` in another |
| Production build into `dist/` (served by the backend) | `npm run build` |
| Type-check only | `npm run typecheck` |
| Lint (oxlint, as shipped by the Vite template) | `npm run lint` |
| Headless smoke tests (starts mock + dev server itself) | `npm run e2e` |

## Layout

```
src/
  main.tsx, App.tsx     entry point; header, tabs, open-repo dialog, pane layout
  index.css             Tailwind/DaisyUI setup and CodeMirror sizing
  api/client.ts         typed wrappers for every /api endpoint; ApiError
  api/ws.ts             useServerEvents(): /ws/events with auto-reconnect
  state/store.ts        zustand store: repo, files, open file, compile, tabs
  state/spec.ts         second zustand store for the Spec view: paper.json, status, skeleton,
                        undo/redo, write queue, WebSocket reloads
  editor/FileTree.tsx   \input/\include tree from GET /api/files
  editor/Editor.tsx     CodeMirror 6 (stex), Ctrl/Cmd-S, Ctrl/Cmd-Alt-J sync, per-file undo history
  pdf/PdfViewer.tsx     pdf.js canvases, zoom, SyncTeX hooks (double-click, overlay, scroll-to-box)
  pdf/SyncBoxes.tsx     forward-SyncTeX highlight boxes drawn over a page
  pdf/coords.ts         PDF points <-> CSS pixels (the convention shared with the backend)
  settings/SettingsDialog.tsx  compiler kind, TeX Live year, docker pull with live log
  compile/ErrorList.tsx Problems panel; click jumps to file:line
  spec/SpecView.tsx     the Spec tab: Outliner | Raw JSON, undo/redo keys, toasts
  spec/ClaimBank.tsx    left column: claim cards with draggable chips
  spec/Outliner.tsx     center: message card, sortable sections and paragraphs
  spec/Row.tsx          one outliner row: status dot, says (keyboard), claim chips, fields, ⋯ menu
  spec/SkeletonPanel.tsx  unadopted headings/paragraphs from the .tex with "Adopt"
  spec/Coverage.tsx     right rail placeholder (phase 5)
  spec/RawJson.tsx      paper.json in CodeMirror; save shows validation errors
  spec/SpecDnd.tsx      dnd-kit context for the whole app: row moves, claim links, drop on editor
  spec/dnd.ts, tree.ts, markers.ts, useSyncedText.ts, TextField.tsx   helpers
  state/runs.ts         third zustand store (phase 4): passes, runners, runs, findings, filters,
                        selected finding, live output, j/k, WebSocket run events
  findings/FindingsView.tsx   the Findings tab: launcher, runs list, selected run, live/raw output
  findings/RunLauncher.tsx    pass / scope / runner / model / thinking, Show prompt, Run (+ force)
  findings/PromptDialog.tsx   exact prompt + argv, Copy, paste-back box
  findings/FindingsSidebar.tsx  findings of the open file (column beside the editor)
  findings/FindingCard.tsx    one finding card + the level / state filter chips
  findings/format.ts          number formats, dates, colors, clipboard, json_ref -> Spec tab
  findings/RunnerControls.tsx runner / model / thinking pickers (launcher, compare dialog, Run both, Triage)
  findings/RunRow.tsx         one run in the runs column (also a group's member rows, indented)
  findings/GroupView.tsx      phase 6: group row (both / fan-out, progress, cost, Cancel) and the
                              merged view (agreement badges, runner chips, per-section blocks)
  findings/TriageBar.tsx      phase 6: Triage popover, status line, hide wrong/trivial, Dismiss all flagged
  findings/Popover.tsx        small button + floating panel (Run both, Triage)
  state/groups.ts             phase 6 pure helpers: runs column items, progress, merged blocks, filters
  state/git.ts          phase 5 store: git status, ticked files, diff, log, snapshots, push/pull output
  git/GitPanel.tsx      the "Git" drawer: status, diff, commit, push/pull, flag major revision
  state/compare.ts      phase 5 store: the blind A/B compare dialog (target, comparison, reveal, history)
  compare/CompareDialog.tsx   blind A/B compare: before picker, runner, Start, verdict, Reveal, History
  spec/Coverage.tsx     right rail of the Spec view: claims x sections matrix, click = link/unlink
  editor/ContextMenu.tsx      the editor's selection menu (right-click or Ctrl/Cmd-.)
  editor/contextActions.ts    what its items do, and how the enclosing paragraph is found
mock/server.mjs         fake backend for development and e2e (incl. a formula-based SyncTeX)
mock/spec.mjs           the mock's in-memory paper.json: ops, status, skeleton, adopt, coverage
mock/runs.mjs           the mock's passes, runners, fake runs, findings, cache, paste, re-anchoring
mock/git.mjs            the mock's fake git repo: status, diff, log, commit, push/pull, snapshots
mock/compare.mjs        the mock's blind comparisons (queued -> running -> done, hidden mapping)
mock/groups.mjs         the mock's run groups (Run both, fan-out), merge rule, group cancel, triage,
                        dismiss-flagged (member runs come from mock/runs.mjs)
e2e/smoke.spec.ts       Playwright tests, phase 1 (config: playwright.config.ts)
e2e/synctex.spec.ts     Playwright tests, phase 2: SyncTeX both ways, Settings, docker pull
e2e/spec.spec.ts        Playwright tests, phase 3: the Spec view
e2e/runs.spec.ts        Playwright tests, phase 4: launcher, runs, findings, highlights, j/k
e2e/git-compare.spec.ts Playwright tests, phase 5: git panel, snapshots, compare, coverage, selection menu
e2e/groups.spec.ts      Playwright tests, phase 6: Run both + merged view, fan-out, group cancel, triage
e2e/data-safety.spec.ts Playwright regressions for the 2026-10-18 audit: repo switches, failed or
                        slow saves, typing during saves, undo after external spec changes
e2e/live/p1-e2e.mjs … p6-e2e.mjs   scripts for the REAL backend on 127.0.0.1:8765
                        serving dist/ (`node e2e/live/p3-e2e.mjs <repo> <screenshot dir>`; not
                        part of `npm run e2e`; p3 expects a fresh `rewrite init` of the sample
                        paper, p4 the same with the Introduction section adopted, and takes
                        `[--runner claude|codex] [--model sonnet]`; p5 expects a git repo with a
                        bare `origin`, the Introduction adopted with paragraphs, a snapshot "v1"
                        and an uncommitted rewording in p-3, see the script's header;
                        p6 a repo with ALL sections adopted and both claude and codex
                        installed, `[--model sonnet]`;
                        REWRITE_URL overrides the address)
```

## Saving source files

- Ctrl/Cmd-S saves the open file; a compile follows 800 ms later.
- Opening another file, or another repo from the header, first saves the open file until
  everything typed is on disk (text typed while a save is in flight is saved in another
  round). If a save fails, nothing switches: the file and repo stay open with their edits
  and the error is shown (in the Open dialog for a repo switch).
- Each save names the revision the text was loaded from (`base_sha256`, the `sha256` of the
  last GET or PUT). If the file changed on disk in between (another editor, a marker
  insertion, git pull), the backend refuses with 409: a yellow "File changed on disk" bar
  appears, the edits stay in the editor, and only its "Reload from disk" button replaces
  them with the file on disk.

## SyncTeX in the UI

- Forward: when the cursor rests on a line for 300 ms in a file with no unsaved
  edits, the line is highlighted in the PDF (yellow box) and the PDF scrolls to
  it unless it is already visible. "Sync →" in the editor header, or
  Ctrl-Alt-J (Cmd-Alt-J on macOS too), forces it, unsaved edits or not.
- Reverse: double-click a spot in the PDF to open its source line.
- Problems show as a quiet "synctex: …" text in the PDF toolbar, never a toast.

## Spec view (phase 3)

- Every edit is one request (`POST /api/spec/ops`, `/api/spec/adopt`, `PUT /api/spec/raw`);
  the backend writes paper.json and answers with the whole new spec. Writes are queued so
  they never overtake each other.
- Undo/redo (Ctrl/Cmd-Z, Ctrl/Cmd-Shift-Z, or the buttons) keep full snapshots of the spec
  (100 deep) and restore one with `PUT /api/spec`. They restore paper.json only: a `% @p`
  marker written into a .tex file stays (the editor's own undo removes it). Inside a
  CodeMirror editor (Raw JSON) Ctrl-Z undoes text instead.
- When paper.json changes outside this editor (another tab, git pull, a hand edit), the
  undo/redo history is cleared ("History cleared: paper.json changed outside the editor"),
  and undo/redo re-read paper.json before `PUT /api/spec` and refuse if it changed since,
  so an undo can never wipe out a change made elsewhere.
- Unsaved Raw JSON text belongs to its repo: opening another repo files it away and shows
  that repo's paper.json; reopening the first repo brings the draft back. No spec write
  is ever sent for a repo other than the one open.
- Typing in a `says` or field box is sent 400 ms after the last key, or on blur; while
  an edit is pending the box keeps its own text even if the spec reloads.
- Outliner keys in a row's text: Enter new sibling, Tab / Shift-Tab demote / promote,
  Alt-Up/Down move, Up/Down change rows, Backspace on an empty row deletes it, Escape leaves.
- Drag a row by its ⠿ handle; drop on a section's header to append a paragraph there.
  Drag a claim chip onto a row to link it. Drag a paragraph onto the "Editor" tab, wait
  for it to open, and drop it on a paragraph in the editor to insert its marker (the
  ⋯ menu does the same at the editor cursor, optionally with `says` as a comment line).
- Status dots: grey ring planned, green linked, amber stale, red orphaned. Click one to
  open its anchor in the editor. The row the editor cursor is in is tinted blue.

## Review runs and findings (phase 4)

- **Findings tab.** Top: the run launcher (pass grouped by level, scope paper / section /
  file / selection, runner, model, thinking). "Show prompt" previews the exact prompt
  (POST /api/runs/preview, nothing runs) with Copy and a paste-back box for a web UI's
  JSON answer; "Run" starts it ("force" skips the cache; 409 "already running" is a
  toast). Left: runs, newest first, with status, cached badge, cost, tokens, wall time,
  counts and Cancel. Center: the selected run's findings grouped by file, "Rejected (n)"
  with the reason and raw JSON, live output while it runs, raw output afterwards.
- **Editor column.** The "Findings" button above the editor opens a column (wide screens)
  with the open file's findings; on narrow screens the Findings tab lists them instead.
  Findings of a run that a newer run of the same pass and scope replaced are not listed.
- **Highlights.** Every listed finding tints its lines by severity; the selected one gets
  a stronger tint, a bar in the margin and its quote underlined. They stay until the
  selection changes (a CodeMirror StateField in editor/Editor.tsx), and follow the text
  while typing; after a save the backend re-anchors and the lines are refreshed.
- **Keys.** `j` / `k` select the next / previous finding of the list on screen, ordered by
  file then line, whenever the focus is not in a text box or the editor. In the Editor
  tab the editor scrolls to it without taking the focus, so the keys keep working.
- **States.** Addressed / Dismiss / Reopen on each card (PATCH /api/findings/{id}); the
  open / all switch and the level chips filter both lists. A `json_ref` chip opens that
  node in the Spec tab.


## Git, blind compare, coverage, selection menu (phase 5)

- **Git panel.** The "Git" button in the header (its badge counts changed files) opens a
  drawer over the right side; the editor and PDF stay mounted underneath. It shows the
  branch, its upstream and ↑ahead / ↓behind, the changed files with git's status letters and
  a checkbox each (all ticked; an un-tick is kept while the file stays changed), the diff of
  the clicked file (+ green, - red), a commit box (Commit sends exactly the ticked paths;
  Ctrl/Cmd-Enter commits; unsaved editor text is saved first), Push / Pull with their live
  output (`git.output` events) and the result, "⚑ Flag major revision…" (asks for a note,
  then `POST /api/snapshots`: the tag is `rewrite/rev-<n>-<slug>`), the snapshots and
  the recent commits. Status is re-read after every action and on `file.changed` /
  `spec.changed`. Escape (with the focus in the drawer) or ✕ closes it.
- **Blind A/B compare.** From an outliner paragraph's ⋯ → "Compare…" (linked or stale
  paragraphs) or the editor's selection menu. Pick a "before" (snapshots first, newest
  selected; then recent commits), optionally runner / model / thinking, and Start. While it
  runs: a status badge, and Cancel (the run's own cancel endpoint). When done: the verdict
  ("Version A reads better", "Version B…", "Tie") and the reason only — no texts, because
  the A/B mapping is hidden. "Reveal which is which" asks `GET /api/compare/{id}?reveal=true`
  and then shows both texts as the model saw them, labelled "before (snapshot …)" /
  "after (working tree)", and whether the current text won. The History tab lists past
  comparisons; the model call is also an ordinary `ab-compare` run in the Findings tab.
- **Coverage.** The Spec view's right rail is the claims × sections matrix of
  `GET /api/spec/coverage`: a cell counts the nodes of the section (section + paragraphs)
  that link the claim. Amber rows are unreferenced claims, rose columns sections arguing
  nothing, a ringed cell is linked by the section itself. Clicking a cell links the claim to
  the section node or unlinks it (one op, one undo step). Re-read on every paper.json change.
- **Selection menu.** Right-click on selected text in the editor (or Ctrl/Cmd-. with text
  selected): Set as this paragraph's message (`update` says), Lift to claim (`add_claim`,
  then `link_claim`), Set as thesis (`update` the message's thesis), Compare with earlier
  version…. The selection is whitespace-collapsed. The paragraph is the one whose
  `% @p <id>` marker most recently precedes the selection start within the same paragraph
  (no blank line or heading in between), read from the editor's text. Outside an adopted
  paragraph the first two items become "Adopt this paragraph first, then …". A toast says
  what changed, with "Undo" (the spec store's undo, once per write the action made).

## Run both, fan-out, triage (phase 6)

- **Pass select.** Only passes of kind `review` are listed; `triage` and `ab-compare` are
  started from a run and from the compare dialog.
- **Run both ▾** (next to Run, enabled while both claude and codex are installed) opens a
  small panel with a model and thinking level per runner, then "Start both": the same pass
  and scope on both runners (`POST /api/groups/both`). **Fan out** (only with scope = paper)
  starts one run per linked section plus one message-drift run over the abstract, the first
  and the last section (`POST /api/groups/fanout`), at most 3 at once; it uses the runner /
  model / thinking of the main row. "force" applies to both.
- **Runs column.** A group is one row: kind badge (both / fan-out), status, "2/5 done" with a
  small progress bar, total cost, findings (and agreements), Cancel while it runs (cancels
  every member still queued or running). Its member runs are listed under it (▸ / ▾; open
  while the group is selected or running) and are ordinary runs: click one to see it alone,
  with its prompt, raw output and the triage bar.
- **Merged view.** Selecting a group shows its merged findings in the backend's order
  (agreements first). "Run both": each entry has an agreement badge ("2/2 agree" in green,
  "1/2" muted) and the runners that reported it; ▸ unfolds each runner's own problem and
  why. Fan-out: the entries sit under one heading per member run (its section, or the
  message-drift run). Clicking an entry highlights its first finding in the editor like a
  normal card; `j` / `k` step through the entries. The level and open/all filters apply.
- **Editor column.** A merged entry's findings are ordinary findings, listed per run. The
  "newest run per pass + scope" rule treats a group as one unit: the claude and codex runs of
  one "Run both" are both listed, and a newer run or group on the same pass and scope
  replaces them.
- **Triage.** In a finished review run's detail, "Triage ▾" (runner / model / thinking)
  sends the run's open findings to a fresh model call (`POST /api/runs/{id}/triage`). The
  status line shows it running, then "Triage: n keep · n wrong · n trivial"; each card gets
  a verdict badge (keep green, wrong red, trivial grey) whose tooltip is the reason, and a
  click on the badge unfolds the reason. Verdicts never change a finding's state:
  "hide wrong/trivial" only hides them in the Findings tab, and "Dismiss all flagged (n)"
  dismisses them on request (`POST /api/findings/dismiss-flagged`), with a toast giving the
  counts.
- **Tauri.** Not implemented; see `../docs/tauri.md` for how a wrap would work (no frontend
  changes needed).
