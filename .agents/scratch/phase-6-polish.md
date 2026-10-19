# Phase 6 — Polish

Status: implemented, Linux-verified 2026-10-18; macOS acceptance pending

## Scope (PLAN.md "Build phases" 6)

- Cross-runner merge: run the same pass on both runners; merge findings by anchor overlap; agreements first.
- Triage call: send a run's findings + scoped text to a fresh call asking which are wrong or trivial; mark results.
- Per-section fan-out with the full spec as global context, concurrency 3, plus one message-drift call over abstract/intro/conclusion.
- Optional Tauri wrap: skipped unless time permits; document how.

## Manual checks

1. "Run both" shows merged list with agreement badges.
2. Triage marks trivial findings and the filter hides them.
3. Fan-out on a 5-section paper launches ≤3 concurrent processes.

## Decisions fixed for implementation

- **Run groups.** "Run both" and "Fan out" create a `RunGroup` that owns several ordinary runs;
  each member run is a normal phase 4 run (same cache, cancel, cost, prompt preview), so nothing
  about single runs changes. Group status: `queued` → `running` → `done` (all members done/cached),
  `partial` (some failed/cancelled), `failed` (all failed), `cancelled`.
- **Merge rule (cross-runner).** Two findings from different runs agree when they are in the same
  file and their anchor line ranges overlap, or their whitespace-collapsed quotes share ≥ 50 % of
  the shorter quote's characters as a common substring. Merged entries are sorted by agreement
  count descending, then file (include order), then line. Severity of a merged entry = the highest.
- **Triage** is the one explicit place prior findings enter a prompt (PLAN "Clean context"). A new
  pass file `prompts/passes/triage.md` with front matter `kind: triage` (new optional key, default
  `review`; `ab-compare` gets `kind: compare`; non-review passes are not runnable via
  `POST /api/runs`). Template vars: the usual ones plus `findings` (the run's open findings as a
  numbered list with file/lines/quote/problem/why). The model answers
  `{ "verdicts": [ { "index": int, "verdict": "keep"|"wrong"|"trivial", "reason": str } ] }`.
  Verdicts are stored on the finding rows (`triage_verdict`, `triage_reason`, `triage_id`); the
  finding `state` is NOT changed automatically; the UI offers "Dismiss all flagged".
- **Fan-out** over scope `paper`: one member run per section node that is `linked` (scope
  `section`) with the full spec outline as global context (the outline is already full in every
  prompt; fan-out additionally prefixes the pass body with a short "This is one section of a longer
  paper; the full outline is above" note via a template var `fanout: true`), plus one
  `message-drift` run over scope kind `ends`. New scope kind **`ends`** = the `abstract`
  environment from the main file (if any) + the first section + the last section (by document
  order; sections resolved like scope `section`), concatenated with file headers. Concurrency is
  the existing cap of 3; member runs queue behind it.
- **Tauri wrap** is skipped in this build. `docs/tauri.md` documents how it would be done
  (sidecar backend, loopback URL, no code changes required).

## API contract (fixed; backend and frontend built in parallel)

Types:
```
RunGroup      = { id, kind: "both"|"fanout", pass, scope: Scope, run_ids: str[],
                  status: "queued"|"running"|"done"|"partial"|"failed"|"cancelled",
                  created_at, finished_at: float|null, cost_usd: float|null,
                  findings_count: int, agreements_count: int }
MergedFinding = { key: str, file, line_start, line_end, severity, level, category,
                  agreement: int, runners: str[], findings: Finding[] }
Triage        = { id, run_id, triage_run_id: str|null, status: "queued"|"running"|"done"|"failed",
                  error: str|null, counts: { keep: int, wrong: int, trivial: int }, created_at, finished_at }
Finding gains:  triage_verdict: "keep"|"wrong"|"trivial"|null, triage_reason: str|null
Pass gains:     kind: "review"|"triage"|"compare"
Scope.kind gains "ends"
```
Endpoints:
- `POST /api/groups/both { pass, scope, claude?: {model?, thinking?}, codex?: {model?, thinking?}, force? }`
  → `{ group }`; 400 if either runner is unavailable.
- `POST /api/groups/fanout { pass, runner?, model?, thinking?, force? }` → `{ group }` (scope is
  always paper; 400 if no section is linked).
- `GET /api/groups?limit=50` → `{ groups: RunGroup[] }`; `GET /api/groups/{id}` → `{ group, runs: Run[],
  merged: MergedFinding[] }` (for `fanout`, `merged` is simply every finding as a singleton entry,
  agreement 1, so the UI can use one view); `POST /api/groups/{id}/cancel` → `{ group }`.
- `POST /api/runs/{id}/triage { runner?, model?, thinking? }` → `{ triage }`; 409 if a triage for
  that run is active; 400 if the run has no open findings.
- `GET /api/runs/{id}/triage` → `{ triage: Triage|null, findings: Finding[] }` (latest triage).
- `POST /api/findings/dismiss-flagged { run_id }` → `{ updated: int }` (sets `state: dismissed` on
  findings with verdict wrong/trivial).
- WS: `group.updated { group }` on every member change; `triage.finished { triage }`;
  existing run/finding events unchanged.

## Status notes

### 2026-10-18 — backend half done

- New: `app/merge.py` (pure merge rule), `app/groups.py` (`GroupManager`: plan all members,
  launch on the RunManager, follow run events + each member's completion hook, cancel),
  `app/triage.py` (`TriageManager`, verdict schema/parser, verdict storage, dismiss-flagged),
  `app/api/groups.py`; triage endpoints in `app/api/runs.py`, dismiss-flagged in
  `app/api/findings.py`. `db.py` migration 5: `run_groups` (exactly the listed columns),
  `triages`, `findings.triage_verdict/triage_reason/triage_id`. `prompts.py`: pass `kind`
  (front matter, default `review`; only review passes are runnable), scope kind `ends`,
  `linked_sections`, `abstract_range`, `fanout` flag + `FANOUT_NOTE`, `get_triage_pass`,
  `assemble_triage_prompt`. `runs.py`: `RunPlan.scope_variant` + `busy()`. Events
  `group.updated`, `triage.finished`. `prompts/passes/triage.md` (new), `ab-compare.md` gains
  `kind: compare`.
- Tests: 559 passed (+3 docker-gated skips), ruff check/format clean. New: test_merge.py (7),
  test_groups.py (13, incl. the concurrency cap: 5 members of a fake CLI sleeping 1 s that
  appends +/- to a log, peak exactly 3), test_triage.py (8); test_prompt_assembly.py (+6: ends,
  fanout note + cache key, kinds), test_invariants.py (+2, LLM_MODULES gains groups/merge/
  triage/api/groups; hostile triage + group output leaves the repo untouched), test_db.py (+1).
  test_prompts/test_init/test_prompt_assembly updated for the new pass and `kind`.

Endpoint shapes: contract shapes exactly; optional extras marked +:
- `RunGroup`, `MergedFinding`, `GroupDetail` as contracted. `merged` for `both` puts agreements
  first; `key` = member finding ids joined by `+`; `line_start/line_end` = the members' anchor
  ranges joined; `runners` and `findings` in run order (claude, codex). `cost_usd` = sum of the
  members that report a cost (codex reports none; cached members count 0.0), null if none.
  `finished_at` = the last member's `finished_at` once the status is final and nothing is active.
- `Triage` + `sent` (findings judged), `ignored` (verdict entries dropped). `Pass` + `kind`;
  triage and compare passes have `runnable: false` and `error: null`.
- Errors (string `detail`): groups 400 malformed body / unknown or non-review pass / bad scope /
  bad thinking / unknown runner / either runner unavailable / no linked section (fanout); 409 a
  member's pass+scope already active; 404 unknown group; 403 foreign Origin. Triage 400 no open
  findings / no usable triage pass ("run `rewrite init`") / unresolvable scope / bad runner
  settings / malformed body; 404 unknown run; 409 a triage of that run is active.
  dismiss-flagged 400 body not exactly `{run_id: str}`, 404 unknown run.
- WS `group.updated` is published whenever the group's visible state (status, member statuses,
  counts, cost, finished_at) differs from the last publication; changes that land together are
  coalesced into one event. The judged run also gets `findings.updated {run_id}` after a triage
  (and after dismiss-flagged when something changed). Triage runs emit run.* events, no
  `findings.updated` of their own.

Judgment calls (flag if wrong):
- "Run both" members share a pass and scope, which the phase-4 one-run-per-scope rule forbids.
  They carry the runner as a scope *variant*: variants of one key run side by side; a plain run
  of that key and any variant exclude each other (409 either way). Plain single runs unchanged.
- Merge: the same-file condition applies to both agreement tests (overlap and quote share);
  an entry holds at most one finding per run (greedy best-first: quote share, then overlapping
  lines, then list order), so agreement = number of runs and no self-agreement. Level and
  category come from the first run's member. All findings of the members are merged whatever
  their state; quotes compared case-sensitively after whitespace collapse.
- Status: `queued` only while every member is queued; a cache hit + queued members is
  `running`. All members cancelled individually → `cancelled`; a group cancel is sticky even if
  some members had already finished `done`. Cancel of a group with nothing active is a no-op.
- Fan-out: members in document order of the linked sections; runner = request, else the pass
  level's runner, else `[runner] default`, used for every member; model/thinking from the
  request apply to all members, else per member level defaults (so message-drift gets
  opus/high when not given). The fan-out note is prefixed in code (between system.md and the
  pass body; worded so its position does not matter) unless the pass body uses `fanout`
  itself (`fanout` is a template variable for every pass, false outside a fan-out). The note
  is part of the cache key (`AssembledPrompt.variant`), so a member never reuses a plain run's
  answer. The message-drift member gets no note and shares the cache with a plain
  message-drift run on `ends`. Group creation pre-checks every member (400/409) before
  launching any; if a launch still fails, the members already started are cancelled.
- `ends`: first `\begin{abstract}`..`\end{abstract}` of the main file (comments ignored), then
  the first and last linked section (one block if only one is linked); planned/orphaned
  sections ignored; normalized scope is `{kind: "ends"}` (scope key `<pass>:ends:`).
- Triage: findings numbered from 1 with their current anchor lines; the scope is the judged
  run's stored scope resolved against the current source; `findings_schema` carries the
  verdict schema; scope key `triage:<run id>`; never cached. Entries with a bad index,
  repeated index (first wins), unknown verdict, extra keys or rewrite text in `reason` are
  ignored (counted). A sent finding with no verdict has its earlier verdict cleared;
  findings that were not open keep theirs. dismiss-flagged touches only `open` findings
  (an `addressed` finding with a wrong/trivial verdict stays addressed).

Live smoke (`rewrite serve` on a git copy of the sample paper, 127.0.0.1:8781 per `ss`;
init, then all 4 sections + 12 section paragraphs adopted via the API, all `linked`):
- Both (paragraph-function, Introduction; claude sonnet/low + codex default/low): group
  `done` in 13.3 s. claude 4.6 s, 9,094/261 tokens, $0.0390, 1 finding; codex 13.3 s,
  18,697/240 tokens, no cost, 1 finding. Merged: 1 entry, agreement 2, runners
  [claude, codex], intro.tex:26 "This paper makes two observations." (claude multiple-jobs,
  codex topic-sentence); `agreements_count` 1, group cost $0.0390. 3 `group.updated`.
- Fan-out (paragraph-function, claude haiku/low): 5 member runs (introduction-1, design-1,
  evaluation-1, conclusion-1, message-drift on `ends`), group `done` in 22.1 s, 7 findings
  (design 3, message-drift 4, others 0), $0.0140 in total. Status trail: 3 running + 2
  queued → ... → done. The `ends` prompt held main.tex 21-29 (abstract), intro.tex 1-29 and
  conclusion.tex 1-8; member prompts carried the fan-out note. Process watcher (every 0.1 s,
  `pgrep -c -f '^claude -p '` anchored so the pgrep shell itself is not counted, plus distinct
  process groups): 175 samples, peak 3 processes / 3 groups, never 4. 6 `group.updated`.
- Triage of the message-drift run (4 open findings; claude sonnet/low): done in 4.7 s,
  9,399/345 tokens, $0.0410; keep 2, trivial 2, wrong 0, ignored 0; states unchanged; triage
  run `cache_key` NULL; prompt (12,155 chars) held the numbered findings and the `ends`
  source. `POST /api/findings/dismiss-flagged` → `{updated: 2}`, again → `{updated: 0}`.
- All 8 calls together $0.094 (codex uncosted). paper.json last written by the adoptions
  (before the first call); server stopped, port free, no error in its log.

Not verified / open:
- All three manual checks need the frontend; their backend halves ran live above.
- macOS. A codex member in a fan-out (only claude was used); a real cancel of a live group
  (tested with fake CLIs only: both process groups gone).
- `docs/tauri.md` is the frontend agent's deliverable.

### 2026-10-18 — phase 6 complete (integration)

- Backend 559 passed (+3 docker-gated); frontend 54 e2e against the mock; live
  `frontend/e2e/live/p6-e2e.mjs` 33/33 on a fully adopted sample paper: "Run both" (claude sonnet +
  codex, merged view, agreement badges consistent with the API), triage of the claude member
  (verdicts and counts match, badges, hide filter), fan-out (5 member runs = 4 sections +
  message-drift over `ends`, at most 3 at once, section grouping).
- Integration fix: `runs.py` now completes a run (stores findings, publishes `run.finished`) while
  still holding its concurrency slot, so a queued run cannot announce `run.started` before the
  previous `run.finished`; the real process cap of 3 was never exceeded, but the UI count was.
- Regression after the change: p4 live 19/19 on a fresh repo; full backend suite green.
- Manual checks 1–3 verified headlessly on Linux. Not verified: macOS; codex members in a fan-out;
  cancelling a group of live CLI processes (fake CLIs only); the Tauri wrap is documented only
  (`docs/tauri.md`), not built.

### Audit fixes 2026-10-18 (backend)

Items 5, 7, 8, 9, 10, 11 of `codex-audit-2026-10-18.md`. The regression tests were run on
the old code first and failed for the audited reason, then the fix made them pass; the
exceptions are noted per item (tests of a new interface can only fail by not finding it).
Results: backend 583 passed + 3 docker-gated skips (was 559 + 3), `ruff check` and
`ruff format --check` clean, `REWRITE_TEST_DOCKER=1 pytest tests/test_docker.py` 19
passed. Installed CLIs moved since phase 4 (codex-cli 0.163.0, claude 2.1.303); their
`--help` output is identical to the phase-4 captures.

**5. Marker adoption vs. a concurrent save.**
- Tests: `test_spec_api.py::test_marker_write_refuses_a_file_changed_after_the_scan` (the
  audit's barrier reproduction: pause `place_marker` before its write, save new prose with
  `write_text_file`, resume → now 409, new prose kept, no marker or node; before: 200 and
  the old prose restored), `::test_file_save_waits_for_a_marker_edit[base-revision|no-base]`
  (the same interleaving through `PUT /api/file`: before the fix the save completed in the
  middle of the edit), `::test_save_with_a_stale_base_revision_is_409`,
  `test_git.py::test_pull_holds_the_source_lock` (these two test new interfaces).
- Fix: `AppState.source_lock(root)`, one asyncio lock per resolved repo path, held by
  `PUT /api/file`, by `POST /api/spec/adopt` (section and paragraph adoption, re-linking;
  taken after `spec_lock`) and by `git pull` (after `spec_lock`). Commit and snapshot do not
  touch the working tree and do not take it; compile does not write sources. `place_marker`
  writes conditionally: `files.write_text_file(..., base_sha256=...)` re-hashes the file
  right before the atomic rename and raises `FileConflictError` → 409 "changed on disk";
  files that only lose a stale marker are written first and the paragraph's file last.
- API extras: `GET /api/file` and `PUT /api/file` return `sha256` (of the UTF-8 content);
  `PUT /api/file` accepts optional `base_sha256` and answers 409 when the file is no longer
  that revision (also when it does not exist).
- Limitation: without `base_sha256` a save is serialized but still last-writer-wins: a stale
  editor buffer saved right after an adoption overwrites the new marker (the prose on disk
  is what the user saved; the node shows `orphaned`). The frontend should send
  `base_sha256` (frontend work, not done here). A save waits while a pull runs (up to the
  120 s remote timeout). External editors are covered only by the check right before the
  rename (a microsecond window remains; POSIX has no compare-and-rename).

**7. Instruction files outside the sandbox.**
- Tests: `test_runner.py::test_codex_production_argv_ignores_ancestor_instruction_files`
  (runs `codex <production -c/--disable options> debug prompt-input` from the sandbox of a
  canary paper, empty `CODEX_HOME`; control run without the `-c` options shows the root
  canary), `::test_claude_production_argv_ignores_ancestor_instruction_files` (local
  `claude -p /context` with an empty `CLAUDE_CONFIG_DIR` and no credentials; control with
  `--setting-sources project` lists the root CLAUDE.md), `::test_context_probe_lists_
  ancestor_instruction_files`, `test_runs_api.py::test_context_leaks_in_runners_and_on_runs`,
  `test_config.py::test_phase4_config_gets_the_context_boundary_in_memory`,
  `::test_explicit_context_settings_are_kept`. Both CLI tests skip when the CLI is missing;
  the probe and API tests test new interfaces.
- Codex evidence (0.163.0, no model call: `codex debug prompt-input` plus the real
  `codex exec` request captured by a local HTTP server configured as a custom
  `model_provider`): with the phase-4 argv the request held the paper root's AGENTS.md, the
  sandbox AGENTS.md and the descriptions of repo skills in the paper's `.agents/skills` and
  `.codex/skills`. `-c project_doc_max_bytes=0` drops both AGENTS.md but not the skills;
  `-c project_root_markers=[]` makes the sandbox the project root (no ancestor AGENTS.md, no
  repo skills; the sandbox AGENTS.md stays). With both, the request held none of them. An
  AGENTS.md above the git root is never read; the paper's `.codex/config.toml` is not
  applied (untrusted project).
- Claude evidence (2.1.303, no model call: local `/context` with an empty
  `CLAUDE_CONFIG_DIR`, plus the real `/v1/messages` request captured by a local server via
  `ANTHROPIC_BASE_URL` and a dummy API key): **yes, with the production argv
  (`--setting-sources project`) the paper root's `CLAUDE.md` is loaded**, and so are
  `.claude/CLAUDE.md` there and any `CLAUDE.md` further up to `/` (even above the git root);
  all were in the captured request. A flag does prevent it: `--setting-sources ""` loads no
  setting source; `/context` then lists no memory files and no project agents or skills, and
  the captured request held no canary (root, `.claude/`, above-root, sandbox, or a user
  CLAUDE.md in `CLAUDE_CONFIG_DIR`). The paper root's `.claude/settings.json` is not read
  either way (its SessionStart hook did not run; the sandbox's own did). `CLAUDE.local.md`
  loads only with the `local` source. Also found, not used: `--settings
  '{"claudeMdExcludes":["**/CLAUDE.md"]}'` and env `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` drop
  memory files but not project agents.
- Fix: codex default `command` gains `-c project_doc_max_bytes=0 -c
  project_root_markers=[]` (both, since the first alone leaves repo skills); claude
  `isolation_args` become `["--setting-sources", "", "--strict-mcp-config"]` (was
  `project`). The sandbox still gets CLAUDE.md/AGENTS.md (PLAN layout); the CLIs no longer
  read them, which is fine because the prompt starts with system.md. In-memory fill for old
  configs: a codex command that does not set one of the two keys gets `-c key=value`
  appended (an explicit other value opts out); claude's exact phase-4 `isolation_args` are
  replaced. config.toml comments document both. With the final production argv built by
  the app, both captured requests held the prompt and no canary.
- Probe (`app/runner/context.py`): walks from the sandbox's parent to `/` and lists
  `CLAUDE.md`, `.claude/CLAUDE.md`, `CLAUDE.local.md`, `.claude/settings*.json`,
  `.claude/agents`, `.claude/skills` (claude) and `AGENTS.md`, `AGENTS.override.md`,
  `.agents/skills`, `.codex` (codex), skipping the CLI's own config dir (reported as
  `global_instructions`). `suppressed` comes from the argv in use: claude by the setting
  sources (settings files are always suppressed: cwd only); codex docs by either key, skills
  and `.codex` only by `project_root_markers=[]`. Exposed as `GET /api/runners`
  `context_leaks: [{runner, path, suppressed}]` (available runners only) and, per run,
  `Run.context_warning` (db migration 6, `runs.context_warning`; also a `run.event` status
  line; a cached run copies its source run's warning).
- Not verified: a logged-in (OAuth) claude run with `--setting-sources ""` (that needs a
  paid model call; the capture used API-key auth against a local server; `/context` with
  the real config dir was not used). A managed/system CLAUDE.md cannot be turned off by any
  flag (none on this machine; not probed). For codex the probe judges by flags only, so an
  entry above the git root can be reported as not suppressed although codex would not read
  it. Outside this item: the codex request still offers the tools `request_user_input,
  view_image, multi_agent_v1, get_goal, create_goal, update_goal, web_search` despite
  `--disable shell_tool --disable unified_exec`; the runner aborts a run that uses one.

**8. Section scope across includes.**
- Tests: `test_prompt_assembly.py::test_section_scope_follows_includes_in_document_order`
  (heading in main.tex with the body in `intro-body.tex`, a nested include, and a section
  boundary inside an included file), `::test_ends_and_fanout_prompts_include_section_bodies`
  (ends scope, a fan-out member prompt, and a body edit changing the cache key),
  `test_skeleton.py::test_section_segments_follow_document_order` (added after the fix, for
  the new scan function).
- Fix: the scanner records document order (`DocScan.order`, `end_ordinal`);
  `skeleton.section_segments` runs from the heading to the next heading of the same or a
  higher level (or `\end{document}`) in document order across `\input`/`\include`, as
  per-file segments. Blank lines at segment edges and, at the very end, include lines and
  back matter (`\bibliographystyle`, `\bibliography`, `\printbibliography`, `\appendix`,
  `\clearpage`, `\cleardoublepage`, `\newpage`) are left out, so the `\input` that leads
  into the next section is not part of this one; comment lines (markers) stay. Section,
  `ends` and fan-out prompts emit one `=== file: <path> (lines a-b) ===` block per segment
  (joined by a blank line) and pass every segment file for anchoring. `Scope.line_end` is
  the section's last line in the heading's own file. A section that lives in one file and
  does not end in back matter gets the same source as before (cache keys unchanged; the
  sample-paper scope tests still pass unchanged); a single-file paper's last section no
  longer carries its trailing `\bibliography` lines.

**9. Staged deletions and renames in commit/snapshot.**
- Tests: `test_git.py::test_commit_after_a_staged_deletion[all|subset]`,
  `::test_commit_after_a_staged_rename[all|subset]` (each with an unrelated staged file that
  must stay staged in the subset case), `test_snapshots.py::test_snapshot_after_a_staged_
  deletion_and_rename`.
- Fix: `commit()` stages paths that exist with `git add --` and drops missing ones from the
  index with `git rm -r -q --cached --ignore-unmatch --` (`git add -A -- <path>` was checked
  and fails the same way for a path in neither the index nor the working tree; `git commit
  --only -- <deleted path>` handles it). Selecting the new path of a staged rename also
  selects its old path, because the git panel sends `path` only: otherwise the commit would
  record a copy and leave the deletion staged. `--only` semantics unchanged.

**10. Duplicate unlabeled headings.**
- Tests: `test_spec_api.py::test_duplicate_unlabeled_heading_is_not_adopted` (two
  `\section{Discussion}` in one file: adopting either, repeatedly, is 409 with a `\label`
  hint and leaves the spec unchanged), `::test_duplicate_labeled_headings_anchor_by_label`
  (passed before the fix too; it pins the label path), `::test_existing_ambiguous_heading_
  marker_is_orphaned`. Updated `test_skeleton.py::test_section_resolution` and
  `::test_section_resolution_prefers_anchor_file`, which pinned first-match resolution.
- Fix: `section_marker` returns `\section{X}` when the text is unique, else a unique
  `\label{...}`, else `\section{X}` when every duplicate is in another file (the anchor's
  `file` then selects it), else `AmbiguousHeadingError` → adoption 409 asking for a
  `\label{...}` after the heading. `find_section` resolves only a single match (the only
  one in the anchor's file, else the only one overall); an ambiguous marker is unresolved,
  i.e. `orphaned`. Never by position.

**11. Unbraced `\input`.**
- Tests: `test_files.py::test_unbraced_input_in_tree_skeleton_and_whole_paper_prompt`
  (`\input section`, `\input "part two"`, `\input sub/third.tex % comment`,
  `\input  fourth\relax`: tree, skeleton paragraphs, whole-paper prompt and its cache key,
  and a real `NativeLatexmk` compile whose `.fls` lists the same files; the compile part is
  skipped without latexmk), `::test_unbraced_input_skips_computed_commented_and_other_
  commands`.
- Fix: the shared include regex in `app/files.py` also accepts TeX's primitive form for
  `\input` only (bare name up to a space, brace, `%` or the next macro, or a quoted name);
  computed names and commented lines are still skipped. `\include`/`\subfile` keep needing
  braces (macro arguments).

Other existing tests changed because they pinned the old behaviour: `test_runner.py` (claude
and codex argv), `test_config.py::test_phase1_config_gets_phase4_keys_in_memory`,
`test_api.py::test_file_roundtrip` (+`sha256`). No frontend files were touched; no manual
UI check was run for these items.

### Codex residual tool surface (probed 2026-10-18 with a fake provider, no model call)

With the production argv (`--sandbox read-only --disable shell_tool --disable unified_exec
-c project_doc_max_bytes=0 -c project_root_markers=[]`) the request codex 0.163 sends still
declares these tools: `functions.exec` (a JavaScript orchestration tool, "code mode"),
`functions.wait`, `functions.request_user_input[_async]`, `clock.sleep` and the
`collaboration.*` multi-agent tools. Adding `--disable code_mode_host`, `--disable multi_agent`,
`--disable goals`, `--disable browser_use`, `--disable computer_use`, `--disable apps`,
`-c web_search=disabled` or the `features.*=false` forms changes nothing in the declared set
(no "ignored setting" warning either). The shell and file tools are gone, so the model cannot
read the repo; the runner aborts and marks `failed` any run in which codex emits a tool event.
Treat "tools disabled" for codex as "shell/file tools removed + any tool use aborts the run",
not as an empty tool list. Claude with `--tools ""` declares only `StructuredOutput`.

### Audit fixes 2026-10-18 (frontend)

Items 1, 2, 3, 4, 6 of `codex-audit-2026-10-18.md`, the frontend half of item 5 (requested by
the coordinator after the backend added `sha256` / `base_sha256`), and the p1 acceptance script.
Every regression is a Playwright test in the new `frontend/e2e/data-safety.spec.ts` (mock
backend, `page.route()` to hold or reject single requests). Each was run on the old code first
and failed for the audited reason, then passed after the fix (40/40 on `--repeat-each=4` for
items 1-4 and 6). Results: `npm run typecheck`, `npm run lint` (0 warnings), `npm run build`
clean; `npm run e2e` **66 passed** (was 54; +12).

Mock (`frontend/mock/server.mjs`): per-repo files / paper.json / baselines (a repo switch parks
the open repo's data), `POST /__mock/repo {path, files?, spec_raw?}` to create repo B,
`{silent: true}` on `POST /__mock/spec-raw` (an external change whose `spec.changed` the UI
missed), `repo` on every `/__mock/saves` and `/__mock/spec-log` entry, and the backend's
`sha256` / `base_sha256` / 409 behaviour on `GET`/`PUT /api/file`.

**1. Raw JSON draft of repo A written into repo B.**
- Tests: `item 1: a dirty Raw JSON draft of repo A never reaches repo B, and comes back with A`
  (tab stays mounted; old code: editor still showed A's draft in B), `item 1: a draft kept while
  the Raw JSON tab is closed is not shown for another repo` (unmount/remount; old code restored
  A's draft in B), `item 1: an outliner edit made for repo A while repo B loads is not written
  into B` (B's GET /api/spec held; old code wrote A's `says` into B's same-id node).
- Fix: `spec.ts` records `repoPath` (the repo its spec/raw came from; `load()` is last-wins);
  `saveRaw(raw, repoPath)` refuses unless that repo is open and no switch is in flight;
  `applyOp`/`adopt` drop writes (toast) in the same window; `reload()` skips while the newly
  opened repo's `load()` is pending. `RawJson.tsx` keeps drafts in a `Map<repoPath, text>`
  (a map rather than one `{repoPath, text}` slot, so drafts of two repos never evict each
  other); on a repo change while mounted it files the dirty text under the old repo and shows
  the new repo's paper.json (or its own draft) in a fresh EditorState, so Ctrl-Z cannot bring
  the old repo's text back; Save is disabled until the shown spec is the open repo's.

**2. Opening another repo discarded unsaved source.**
- Tests: `item 2: unsaved source is saved to repo A before the backend opens repo B` (exactly
  one PUT, tagged repo A, before `POST /api/repo/open`; old code: zero saves),
  `item 2: when that save fails, repo A stays open with its edits and an error` (PUT → 500: no
  open request, dialog shows the error, buffer intact and dirty; old code switched).
- Fix: `store.ts` `openRepo`/`initRepo` go through `switchRepo()`: `saveUntilClean()` first
  (throws for the Open dialog on failure), then `switchingRepo = true` around the backend call;
  `adoptRepo` replaces the saved buffer in the same tick. Nothing is saved after the backend
  switched. Editor.tsx drops typing while `switchingRepo` (transaction filter; the modal
  covers the editor anyway).

**3. File navigation lost edits after a failed save / typing during the save.**
- Tests: `item 3: a failed save keeps the file open with its edits instead of switching`
  (old code opened method.tex and dropped the text), `item 3: text typed while the switch-save
  is in flight is saved before the switch` (PUT held, more typing, release; old code saved
  only the first text).
- Fix: `save()` returns `SaveResult` (`{ok: true, savedRevision}` | `{ok: false, error}`);
  `OpenFile.revision` counts edits. `openFile()` returns a boolean and replaces the buffer only
  when nothing is unsaved: `saveUntilClean()` saves again while the user typed during a save
  (max 5 rounds), and the dirty check is repeated after the target file was fetched. On
  failure the file stays open with a notice. `goToLocation` uses the result.

**4. Raw JSON save response erased text typed while it was pending.**
- Tests: `item 4: Raw JSON text typed while a save is in flight survives its response
  (button)` and `(keyboard)` (PUT /api/spec/raw held; old code reset the editor to
  `FIRST SAVE` and cleared the dirty mark).
- Fix: `RawJson.tsx` captures the editor revision at submit; on success the canonical text is
  applied only if nothing was typed since, otherwise it becomes the saved baseline and the
  text stays unsaved. One save at a time (button and Ctrl/Cmd-S); an answer that arrives after
  a repo switch is ignored.

**5 (frontend half). Saving a stale buffer over a file changed on disk.**
- Test: `item 5: a save over a file changed on disk is refused (409); the buffer stays until
  Reload` (external PUT without base, then Ctrl-S; old code overwrote the external version).
- Fix: `OpenFile.sha256` from GET and every successful PUT, sent as `base_sha256`
  (`api.putFile(path, content, base)`); a 409 is a failed save (buffer kept, switches refused)
  that sets `conflict`; App.tsx shows "File changed on disk" with "Reload from disk", the only
  path that replaces the buffer (`reloadFromDisk`). The `% says:` comment write in spec.ts also
  sends its base revision. No "overwrite anyway" option: after a conflict the user copies
  what they want to keep and reloads (a decision to revisit if that is too strict).

**6. Undo after an external spec reload removed the incoming change.**
- Tests: `item 6: an external change clears undo history, so undo cannot remove it` (old code:
  Ctrl-Z PUT the old spec and the external field was gone), `item 6: undo re-checks paper.json
  first and refuses when it changed unnoticed` (external change with no `spec.changed`; old
  code PUT the old spec).
- Fix: `spec.ts` `reload()` clears undo/redo when it brings in content it did not write and
  toasts "History cleared: paper.json changed outside the editor"; undo/redo `GET /api/spec`
  first and refuse (history cleared, file shown as it is) when `raw` differs from what the
  history was built on. Remaining window: between that GET and the PUT (milliseconds); a
  server-side precondition on `PUT /api/spec` would close it (backend, not done).

**Acceptance script.** `frontend/e2e/live/p1-e2e.mjs` rewritten in the p2+ style: one
`check()` per manual check 3-6 (tree vs. the `\input`s of main.tex on disk, dirty marker,
save on disk, marker cleared, main.pdf, page count, drawn canvas, error row at
`sections/method.tex:3`, click → file and line), plus HTTP/JS error checks allowing one 404 for
`/api/pdf` before the first compile and SyncTeX "no match" 404s; `process.exit(1)` on any
FAIL; `REWRITE_URL` honoured; the two edited .tex files are restored afterwards.
Real backend (Linux, `rewrite serve` on fresh inits of the sample paper, after the backend
item-5 change): **18/18 PASS**, exit 0; a negative run with `sections/` read-only gave 4 FAILs,
exit 1 (and showed the item-3 fix live: the UI refused to leave the unsaved file).

Also run against the real backend (ad-hoc probe, not committed): items 1, 2, 5, 6 end to end
with two initialised repos (24/24): A's dirty main.tex saved into A before B opened; B's Raw
JSON draft never reached A and came back with B; an on-disk paper.json edit picked up by the
spec watcher cleared undo and survived Ctrl-Z; an on-disk .tex edit under a dirty buffer gave
the conflict bar, disk untouched, Reload then a normal save.

Not verified: macOS (PLAN's definition of done); a human pass over the new conflict bar and
the "History cleared" toast wording. README (frontend) updated: saving section, spec undo
notes, the new test file.

### Subscription-only runner auth (2026-10-18)

Goal: the CLIs run on the user's Claude Code / Codex subscription (claude.ai / ChatGPT
login), never an API key, unless `[runner] allow_api_keys = true` (new, default false, in
the config.toml template).

- `child_env(base=None, *, allow_api_keys=False)` (app/runner/base.py `API_KEY_ENV`) strips
  ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL, OPENAI_API_KEY,
  OPENAI_BASE_URL, CODEX_API_KEY, CLAUDE_CODE_USE_{BEDROCK,VERTEX,FOUNDRY} and
  CLAUDE_CODE_SKIP_{BEDROCK,VERTEX,FOUNDRY}_AUTH; CLAUDE_CODE_OAUTH_TOKEN (a login token)
  stays. CODEX_API_KEY was not in the brief: `codex exec` honours it (and `codex login
  status` does not show it), so it is stripped too. The flag goes `RunnerConfig` →
  `make_runner(..., allow_api_keys=)` → `CliRunner` → the spawn, at all three call sites in
  app/runs.py (runners_info argv, plan, launch).
- app/runner/auth.py: `auth_status(runner, cli, *, allow_api_keys, cwd)` runs
  `claude auth status` (JSON on stdout) / `codex login status` (one line on stderr) with the
  run's environment, 15 s timeout (process group killed), never raises. Verified on claude
  2.1.303 / codex-cli 0.163.0: claude authMethod is claude.ai | oauth_token | api_key |
  api_key_helper | third_party | none (a Console login reports api_key: it is a managed key;
  there is no "console-oauth" value); codex says "Logged in using ChatGPT", "... an API key
  - sk-...***" (masked key dropped), "Not logged in", plus workload identity / access token /
  Bedrock wordings. Subscription methods: claude.ai and oauth_token (claude), ChatGPT (codex);
  anything else gets a warning, worded "logged in with an API key; ..." for key methods.
  `claude login` is not a subcommand on 2.1.303, so the error says `claude auth login`.
- `AuthCache` on AppState (handed to RunManager), 60 s per (runner, executable,
  allow_api_keys). A "not logged in" answer is not cached, so logging in shows up on the next
  GET /api/runners. No warm-up at startup (it would run the real CLIs in every API test).
- GET /api/runners: `auth` {logged_in, method, detail} per runner found on PATH (null
  otherwise); `logged_in: false` → `available: false`, `error: "not logged in: run `claude
  auth login`"` / `codex login`; a non-subscription login → `warning`, runner stays available.
- Frontend: `RunnerInfo.auth` / `warning` / `error` (optional); RunnerControls shows
  "via claude.ai" / "via ChatGPT" (muted, `runner-auth`) under the runner select and the
  warning in amber (`runner-warning`); a not-logged-in runner's option reads "(not logged
  in)" instead of "(not found)". mock/runs.mjs runners carry auth (codex auth null while
  "not installed"); runs.spec.ts asserts the note.
- Tests (all pass): test_runner.py `test_child_env_strips_api_keys_unless_allowed`,
  `test_auth_status_parses_the_cli_answer[claude-subscription|claude-logged-out|
  claude-api-key|claude-setup-token|codex-chatgpt|codex-logged-out|codex-api-key]` (fake
  executables printing the captured outputs), `test_auth_status_never_raises` (garbage,
  missing CLI, empty command, hang → timeout, Bedrock), `test_auth_status_uses_the_run_
  environment`, `test_auth_status_with_the_installed_cli[claude|codex]` (real CLIs, both
  logged in here); `test_run_streams_prompt_on_stdin_in_the_sandbox` now also checks the keys
  are gone. test_runs_api.py: `test_runners_endpoint` (auth from the fake CLIs, which now
  answer the status subcommands without recording a call), `test_runners_report_login_state`
  (monkeypatched check: not logged in, API key, no negative caching, cache hit,
  allow_api_keys passed), `test_api_keys_are_kept_out_of_runs_unless_allowed` (both runners,
  end to end). Backend 597 passed / 3 skipped (Docker); ruff clean; frontend typecheck, lint,
  build, e2e 66/66.
- Not verified: macOS (keychain login on the real CLIs); a real run with an API key in the
  server environment (only fake CLIs check the stripped environment). `claude auth status`
  is not given `--setting-sources`, so an `env` block in ~/.claude/settings.json could make it
  report a login a run (which loads no settings) would not use.

### macOS acceptance 2026-10-19

| Check | Result | How |
| --- | --- | --- |
| 1 "Run both" → merged list with agreement badges | pass | p6 live, first run 33/33 (+1 data-dependent skip): claude + codex members done, agreements_count matched the merged list, badges and runner chips in API order, agreement entry first and unfolded. Second run 32/32 (+2 skips: the runners agreed on nothing, triage flagged nothing) |
| 2 triage marks trivial findings, the filter hides them | pass / data-dependent | triage ran as a fresh `triage` run, verdicts on every open finding, counts and badges match the API, "hide wrong/trivial" works; both runs the model returned only `keep`, so "Dismiss all flagged" was skipped |
| 3 fan-out on a 5-section paper launches ≤ 3 processes | pass | 4 section runs + 1 message-drift over `ends`; max 3 at once by API status; `claude -p` processes seen: 3 |

- Script fix: p6 counted processes with `pgrep -fc`; BSD pgrep has no `-c`, so macOS reported 0
  (the real check uses the API and was unaffected). Now counts the lines of `pgrep -f`;
  guarded by `e2e/portability.spec.ts`.
- Headless suite fix: `groups.spec.ts` read the merged entries' agreement once right after the
  status badge (which follows the `group.updated` event) while the detail refetch was still in
  flight (150 ms debounce); on macOS that raced every time. It now polls. Not an app defect: the
  merged view refreshes within the debounce.
- Keychain logins of the real CLIs verified (see phase 4). Tauri wrap still not built.
