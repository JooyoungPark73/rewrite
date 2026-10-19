# Phase 5 — Blind A/B, snapshots, git panel, coverage matrix, editor context menu

Status: implemented, Linux-verified 2026-10-18; macOS acceptance pending

## Scope (PLAN.md "Build phases" 5)

- `app/gitops.py`: status, diff (file / all), commit, push, pull, tag snapshot
  (`rewrite/rev-<n>-<slug>`), log, show file at ref. Shell out only.
- Snapshots table in state.db (`snapshots(id, tag, sha, note, created_at)`).
- A/B compare: `POST /api/compare {file, marker|line range, before: {snapshot|sha}}` →
  assembles two versions, randomizes order, uses the `ab-compare` pass with only the
  relevant paper.json entries, stores verdict + mapping; result reveals mapping only after
  the verdict is shown. Table `comparisons`.
- Coverage matrix (`GET /api/spec/coverage`): claims × sections with counts; frontend
  right rail in Spec view with empty-row/column highlighting.
- Editor context menu on selection: set as this paragraph's message, lift to claim, set as thesis.
- Git panel UI: status list, diff viewer, commit box, push/pull, "flag major revision".

## Manual checks

1. Commit from the panel; `git log` shows it. Push/pull against a local bare remote.
2. Flag major revision creates a tag; selecting it as "before" for a paragraph runs a blind compare and shows the verdict then the mapping.
3. Coverage matrix updates when a claim is linked/unlinked.
4. Selecting a sentence → "lift to claim" creates the claim and links the paragraph.

## Decisions fixed for implementation

- **git is shelled out** with `GIT_TERMINAL_PROMPT=0`, cwd = repo, 120 s timeout for push/pull
  (streamed), 20 s otherwise. Never `git add -A` blindly: commit takes an explicit `paths` list
  (default = every changed/untracked path from status, which already excludes gitignored files).
- **Snapshots** are annotated tags `rewrite/rev-<n>-<slug>` (n = 1 + highest existing n) on
  HEAD. If the tree is dirty, "flag major revision" first commits all changed paths with the message
  `Snapshot: <note>` and then tags; the tag message is the note. Snapshot rows live in `state.db`
  (`snapshots(id, tag, sha, note, created_at)`) and are also reconstructed from `git tag -l
  'rewrite/*'` so a cloned repo shows them.
- **A/B compare** uses the `ab-compare` pass (template vars `version_a`, `version_b`, plus
  `spec_outline` reduced to the relevant entries: message, the node, its section, linked claims).
  "Before" = the paragraph text at the chosen ref found by `% @p <node_id>` in that ref's version of
  the file (fallback: the given line range at that ref); "after" = the working tree. Order is
  randomised per comparison; the mapping is stored with the comparison but only returned by the API
  once `status == done` and the caller asks for it (`?reveal=1`), so the UI shows the verdict first
  and a "Reveal" button second. The model answers `{ "verdict": "A"|"B"|"tie", "reason": str }`;
  anything else → `failed` with the raw output kept. The run goes through the phase 4 runner
  machinery (same sandbox, same clean-context rules, stored as a `Run` with pass `ab-compare` and
  scope kind `selection`), so cancel/cost/prompt preview work unchanged. Never cached.
- **Coverage count** for (claim, section) = number of nodes in that section's subtree (the section
  node itself plus its paragraphs) whose `claims` contains the claim id.
- **Editor context menu** is frontend-only over phase 3 ops: the enclosing node is the one whose
  `% @p` marker most recently precedes the selection start (from `GET /api/spec/status` lines; none
  → offer "Adopt this paragraph first"). "Set as this paragraph's message" → `update {id, says}`;
  "Lift to claim" → `add_claim {says}` then `link_claim` on the enclosing node when there is one;
  "Set as thesis" → `update {id: "message", fields: {thesis}}`.
- **paper.json watcher**: the backend watcher also watches `paper.json`; an external change (content
  hash differs from the app's last write) emits `spec.changed` so pull/checkout refresh the UI.

## API contract (fixed; backend and frontend built in parallel)

Types:
```
GitFile    = { path: str, status: str ("M","A","D","R","??", ...), staged: bool }
GitStatus  = { branch: str|null, upstream: str|null, ahead: int, behind: int, head_sha: str|null,
               head_subject: str|null, files: GitFile[], clean: bool, is_repo: bool }
Commit     = { sha: str, subject: str, author: str, date: float, tags: str[] }
Snapshot   = { id: str, tag: str, sha: str, note: str, created_at: float }
Comparison = { id: str, file: str, node_id: str|null, line_start: int|null, line_end: int|null,
               before: { kind: "snapshot"|"sha", ref: str }, run_id: str|null,
               status: "queued"|"running"|"done"|"failed", verdict: "A"|"B"|"tie"|null,
               reason: str|null, mapping: { A: "before"|"after", B: "before"|"after" }|null,
               error: str|null, created_at: float, finished_at: float|null }
Coverage   = { claims: [{ id, says }], sections: [{ id, says, heading: str|null }],
               cells: { [claim_id]: { [section_id]: int } },
               unreferenced_claims: str[], empty_sections: str[] }
```
Endpoints:
- `GET /api/git/status` → `GitStatus` (`is_repo: false` with empty fields when the repo is not git).
- `GET /api/git/diff?path=&staged=false` → `{ diff: str }` (no path → whole tree; untracked files
  are shown as a full-add diff via `git diff --no-index /dev/null <path>` or equivalent).
- `GET /api/git/log?limit=50` → `{ commits: Commit[] }`.
- `GET /api/git/show?ref=&path=` → `{ content: str }`; 404 if the path is absent at that ref.
- `POST /api/git/commit { message, paths?: str[] }` → `{ sha, subject }`; 400 empty message or
  nothing to commit.
- `POST /api/git/push` / `POST /api/git/pull` → `{ started: true }`; output lines stream as WS
  `git.output { op: "push"|"pull", line }` and end with `git.finished { op, ok, error: str|null }`;
  409 if an op is already running. After a pull, the backend emits `file.changed` for changed
  files and `spec.changed` if paper.json changed.
- `GET /api/snapshots` → `{ snapshots: Snapshot[] }`; `POST /api/snapshots { note }` → `{ snapshot }`.
- `POST /api/compare { file, node_id?, line_start?, line_end?, before: {kind, ref}, runner?, model?,
  thinking? }` → `{ comparison }` (queued/running); 400 if neither node_id nor a line range, or if
  the paragraph cannot be found at the ref.
- `GET /api/compare?limit=50` → `{ comparisons: Comparison[] }` (mapping always null here).
- `GET /api/compare/{id}?reveal=false` → `{ comparison, before_text: str, after_text: str,
  run: Run|null }` — `mapping` is non-null only when `reveal=true` and status is done.
- `GET /api/spec/coverage` → `Coverage`.
- WS: `git.output`, `git.finished`, `compare.finished { comparison }` (mapping omitted),
  `spec.changed` on external paper.json edits.

## Status notes

### 2026-10-18 — backend half done

- New: `app/gitops.py` (status/diff/log/show/commit/tags, `GitManager`: one git op at a time,
  push/pull streaming, post-pull announcements), `app/snapshots.py`, `app/compare.py`
  (`CompareManager`, before/after extraction, verdict parsing), `app/coverage.py`,
  `app/specwatch.py` (spec-file hash registry), routers `app/api/git.py`,
  `app/api/snapshots.py`, `app/api/compare.py`, `GET /api/spec/coverage` in `app/api/spec.py`.
  `db.py` migration 4 (`snapshots`, `comparisons`). `skeleton.marker_paragraph` (marker rule on
  any version of a file). `prompts.py`: `PassDef.variables`, `get_compare_pass`,
  `reduced_spec`, `compare_text`, `assemble_compare_prompt`. `runs.py`: `plan_assembled` +
  `launch` split out of `plan`/`start_run`; a plan may carry its own answer `schema`,
  `interpret` (replaces findings parsing) and `on_finished` hook; `cache_key ""` = never cached.
  `events.py`: `git.output`, `git.finished`, `compare.finished`; the watcher also watches the
  root spec file. `write_spec` records the content hash before writing.
  `prompts/passes/ab-compare.md` now asks for `{"verdict", "reason"}`; `system.md` unchanged.
- Tests: 519 passed (+3 docker-gated skips), ruff check/format clean. New: test_git.py (23),
  test_snapshots.py (6), test_compare.py (20), test_coverage.py (4); test_events.py (+5),
  test_invariants.py (+2, LLM_MODULES gains compare.py/api/compare.py). conftest: autouse
  `isolated_git` (GIT_CONFIG_GLOBAL=/dev/null, fixed identity), `git_repo`, `remote` fixtures.

Endpoint shapes: contract shapes exactly; optional extras marked +:
- `GitStatus` + `remotes: str[]`; `GitFile` + `xy` (porcelain v2 code), `orig_path` (renames).
  `status` = index letter if set, else worktree letter; `"??"` untracked, `"U"` conflicts;
  `staged` = something about the path is in the index.
- `GET /api/git/diff` → `{diff}` + `truncated` (4 MB cap). staged=false = working tree vs HEAD
  (so a staged change still shows) + untracked files as `--no-index /dev/null` adds;
  staged=true = index vs HEAD; before the first commit HEAD is the empty tree. Outside git:
  `{diff: ""}`.
- `GET /api/git/log` → `Commit.date` = author date; tags from `%D`. Outside git / no commits: [].
- `GET /api/git/show` 404 path absent (or a directory) at the ref, 400 unknown/invalid ref
  (leading `-`, whitespace, `:` refused), 400 outside git, 413/415 too large / not UTF-8.
- `POST /api/git/commit` → `{sha, subject}` + `files`; runs `git add -- <paths>` then
  `git commit --only --cleanup=whitespace -F - -- <paths>` (changes staged elsewhere stay
  out; a `#` line survives). 400 empty message, nothing to commit, unknown/ignored path,
  malformed body; 409 busy; 403 foreign Origin.
- `POST /api/git/push|pull` → `{started: true}` + `op`, `command` (argv). 400 not a repo / no
  remote (checked before starting); 409 while commit/snapshot/push/pull runs.
  `git.finished` + `changed: str[]` (paths a pull changed).
- `POST /api/snapshots` → `{snapshot}` + `commit` (sha of the auto-commit or null). 400 empty
  note / not a repo; 409 busy. `GET /api/snapshots` outside git → `[]`.
- `Comparison` + `before_sha`, `before_lines`/`after_lines` ([first, last]), `revealed_at`.
  `GET /api/compare/{id}` + `version_a`, `version_b` (texts as the model saw them, for a blind
  A/B display before reveal), `raw_output`. `compare.finished` carries `mapping: null`.
- `POST /api/compare` errors: 400 (malformed body, neither node_id nor a full range, bad
  range, unknown/non-paragraph node, unknown snapshot/ref, file unreadable at the ref,
  paragraph not found at the ref or in the working tree, identical versions, not a git repo,
  unknown runner/thinking), 409 spec file missing/invalid, 403 foreign Origin.
- `GET /api/spec/coverage` → contract shape; `cells` is the full matrix (zeros included);
  `heading` from `\section{X}` (or the labelled heading for a `\label` anchor; null if
  planned). 409 for a broken spec file.

Live smoke (`rewrite serve` on a git copy of the sample paper, `origin` = local bare repo,
127.0.0.1:8775 per `ss`; this machine's real global git config, not the test isolation):
- init; adopted Introduction (`introduction-1`) and p-1..p-4 (markers 4/12/18/25); set
  message/section/p-3 `says` via ops; status listed 16 paths (state.db not among them);
  `POST /api/git/commit` → 64732bd with all 16 files, tree clean; `POST /api/snapshots {v1}`
  → `rewrite/rev-1-v1` on 64732bd, `commit: null`.
- Reworded p-3's first sentence in the working tree; `POST /api/compare` (before = snapshot
  id, claude sonnet low): `compare.finished` 3.7 s later, done, verdict **A**, reason "Version
  B opens with "the main alternative" without tying it to warm pools or their idle-memory
  cost. ..."; mapping null in the event, the list and the plain GET; `?reveal=true` →
  `{A: after, B: before}`. Run: pass ab-compare, scope selection 19-23, model_used
  claude-sonnet-5-5, 7,027 in / 177 out tokens, $0.0299, `--json-schema` = verdict schema.
  Prompt body (after system.md) contains none of: tag, snapshot id, sha, "revision", "edit",
  "older", "newer", "original", "before", "after"; outline = message + introduction-1 + p-3
  only. One real model call in total.
- `POST /api/git/push` → 3 `git.output` lines + `git.finished ok`; `git ls-remote` shows main
  at 64732bd and `refs/tags/rewrite/rev-1-v1` (peeled 64732bd).
- Second clone committed method.tex + a paper.json wording change and pushed; a local
  unpushed tag `rewrite/rev-9-unpushed` was made; `POST /api/git/pull` → 7 `git.output`
  lines, `spec.changed` (once), `file.changed sections/method.tex`, `git.finished ok changed
  [paper.json, sections/method.tex]`, then the watcher's duplicate `file.changed` (same
  mtime). The unpushed tag survived although this machine's config has
  `fetch.prune=true` + `fetch.pruneTags=true` (a plain `git pull` deletes it: verified).
  The uncommitted p-3 edit was kept; GET /api/spec shows the collaborator's message.
- Coverage: before claims `{claims: [], empty_sections: [introduction-1]}`; after adding c1,
  c2 and linking c1 to introduction-1 and p-3: `cells {c1: {introduction-1: 2},
  c2: {introduction-1: 0}}`, `unreferenced_claims [c2]`, `empty_sections []`.
- Server stopped; port free; no errors in its log.

Judgment calls (flag if wrong):
- Pull: `git -c fetch.pruneTags=false pull` (user's merge/rebase config otherwise applies;
  divergent branches without config fail with git's message). Changed paths come from
  `HEAD-before..HEAD-after` (`--name-status --no-renames`), not `HEAD@{1}`, so a no-op pull
  announces nothing. A pull holds `spec_lock` so a concurrent spec edit cannot overwrite
  pulled content. Re-anchoring is awaited for every changed `.tex` path before
  `git.finished` (the run manager's watcher consumer may repeat it; idempotent).
- Push: `--follow-tags` always; a branch with no upstream is pushed `-u <origin|only remote>
  HEAD`. `GIT_EDITOR=true`, `GIT_TERMINAL_PROMPT=0`, `GIT_LITERAL_PATHSPECS=1`, own process
  group; timeout kills the group (tested with a hanging ssh command).
- git output we parse/show pins `core.quotepath=off`, `color.ui=never`,
  `diff.mnemonicPrefix=false`, `diff.noprefix=false`, `log.showSignature=false` (this
  machine sets `diff.mnemonicprefix=true`).
- A paper repo inside a larger work tree works: status is limited to `-- .`, paths are made
  relative to the paper root, `show` uses `<sha>:./<path>`.
- The commit/snapshot/push/pull lock is one `GitManager.current`; commit and snapshot take it
  for their duration too (409 while a push/pull runs).
- Snapshots: n = 1 + max over existing `rewrite/rev-*` tags AND state.db rows (a deleted
  tag's number is not reused); slug = `spec.slugify(note, 32, "snapshot")`; tag message =
  note (`--cleanup=whitespace`); auto-commit message `Snapshot: <first line>` + rest. Rows for
  tags missing from git are kept in state.db but not listed. `before.ref` for kind
  `snapshot` accepts the snapshot id or its tag.
- Compare: versions sent to the model have LaTeX comments removed and `\author` stripped
  (comments can carry "revised" notes); `before_text`/`after_text` are the raw lines without
  the marker line. Identical versions (whitespace aside) → 400 instead of a wasted call.
  Without node_id, the enclosing marker in the working tree (if it is a spec paragraph) is
  used for the outline and scope name. The reduced outline is `render_outline` of a spec cut
  down to message + the paragraph's section holding only that paragraph + claims linked by
  either. `findings_schema` is the verdict schema and `source` is empty for this pass.
  Verdict parsing is strict on keys (`{verdict, reason}` exactly), lenient on case
  (`"a"` → `A`, `"TIE"` → `tie`); a reason with ≥ 8-word quoted replacement text fails
  (`rewrite_text`); no praise filter on `reason`. Each comparison has its own scope key
  (`ab-compare:<id>`), so several can run (concurrency cap 3 still applies). Comparison
  status follows the run while active; comparisons left active by a dead server become
  failed ("interrupted") at start-up. The first reveal is recorded (`revealed_at`).
- Compare runs appear in `GET /api/runs` (pass `ab-compare`) and emit `run.*` events but not
  `findings.updated`.
- Watcher: `spec.changed` for the spec file only when its hash differs from the app's last
  write or last announcement (one announcement per new content, whichever of the watcher and
  the pull hook sees it first).

Not verified / open:
- All four manual checks need the frontend; their backend halves ran live above.
- macOS. Push/pull against real Overleaf/GitHub remotes (credentials, ssh prompts): only
  local bare remotes here; with no tty and `GIT_TERMINAL_PROMPT=0` a credential prompt should
  fail rather than hang, which the timeout test only approximates.
- Merge conflicts from a pull are reported (`git.finished ok:false`) but conflicted files get
  no `file.changed` from the pull hook (HEAD does not move); the watcher covers .tex files.
- Paper repos initialised before phase 5 keep their old `ab-compare.md` (asks for
  `problems_a/problems_b`); with claude/codex structured output the verdict schema still
  forces `{verdict, reason}`, but a paste-style or text-only answer would fail. Re-copy the
  pass (delete it and run `rewrite init`) to update.
- codex was not used for a real comparison (one claude call only, as budgeted).

### 2026-10-18 — phase 5 complete (integration)

- Backend 519 passed (+3 docker-gated); frontend 48 e2e against the mock; live
  `frontend/e2e/live/p5-e2e.mjs` 27/27 on a prepared repo (bare origin, Introduction adopted,
  commit, snapshot v1, p-3 reworded): panel commit, push (`git.finished ok`, HEAD == upstream),
  "Flag major revision" → `rewrite/rev-2-v2`, blind compare of p-3 vs v1 with claude sonnet
  (verdict A, reason shown first, mapping hidden until Reveal, A = working tree), coverage cell
  click links a claim (one-line paper.json change), context menu Lift to claim.
- Live script fix: the selection step now uses the browser's actual selection (CodeMirror wraps
  long lines so Home/End work on visual rows).
- Regression: p1/p2/p3 live scripts still pass against the current build.
- Backend agent's finding: this machine's global git config has `fetch.pruneTags=true`, which
  deletes unpushed snapshot tags on pull; the app pulls with `-c fetch.pruneTags=false`.
- Manual checks 1–4 verified headlessly on Linux. Not verified: macOS; pull through the UI against
  a remote with new commits (API-level pull verified by the backend agent with a second clone);
  real Overleaf/GitHub remotes; a codex comparison.
