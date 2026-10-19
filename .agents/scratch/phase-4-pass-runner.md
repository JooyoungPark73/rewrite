# Phase 4 — Pass runner

Status: implemented, Linux-verified 2026-10-18; macOS acceptance pending

## Scope (PLAN.md "Build phases" 4)

Prompt loading with front matter, both runners with config-driven flags verified
against the installed CLIs, DB schema for runs and findings, streaming and cancel,
sidebar with highlights and j/k, finding states, re-anchoring, caching, cost
display, show and copy prompt, rule validator.

## CLI flag verification (do first; record results in config.toml comments and here)

Help captured in `cli-help/`. Verify on this machine:
- claude: `-p`, `--output-format stream-json` (requires `--verbose`), `--input-format text`,
  `--model`, `--effort <level>` (thinking), `--tools ""` (disable tools) or `--disallowedTools`,
  `--no-session-persistence`, `--json-schema <schema>` (structured output), `--bare`
  (skips hooks/settings → may also skip CLAUDE.md injection — test whether it suppresses
  the global ~/.claude/CLAUDE.md and record), `--setting-sources` (set to "project" to drop user settings).
- codex: `exec`, `--json`, `-m`, `-c model_reasoning_effort=<level>`, `--sandbox read-only`,
  `--output-schema <file>`, `--ephemeral`, `--skip-git-repo-check`, `--ignore-user-config`,
  `--ignore-rules` (check if this suppresses global AGENTS.md), `-o <file>` last message.
- Run a 1-token smoke prompt through each (if logged in) and store the raw event stream
  as a test fixture under `backend/tests/fixtures/`.

## Backend

- `app/prompts.py`: load `.rewrite/prompts/system.md` and `passes/*.md`; YAML front
  matter (`name, level, scope, allow_rewrites, model, thinking`) + Jinja2 body; hot reload
  by mtime; template vars `spec_outline, spec_json, source, scope_name, claims, findings_schema`.
  Assemble: system.md + pass body with the scoped numbered `.tex` and `\author{}` stripped.
- `app/db.py`: SQLite at `.rewrite/state.db`, tables `runs(id, pass, scope_kind, scope_ref,
  runner, model, thinking, git_sha, prompt_hash, scope_hash, spec_hash, prompt, raw_output,
  status, started_at, finished_at, input_tokens, output_tokens, cost_usd, error)`,
  `findings(id, run_id, file, line_start, line_end, quote, level, category, severity, problem,
  why, json_ref, state open|addressed|dismissed, anchor_state ok|moved|stale, rejected_reason)`.
- `app/runner/base.py` Runner protocol + `RunResult`; `claude.py`, `codex.py`: spawn with
  `start_new_session=True`, cwd sandbox, prompt on stdin, parse stream events, emit over the
  event bus, cancel via `os.killpg`. Concurrency semaphore 3; one run per scope key.
- `app/findings.py`: schema (no replacement field), validator (reject quoted replacement
  text unless allow_rewrites; strip preamble; fenced-JSON fallback), anchoring by exact
  quote then line range, re-anchor on file change (watchfiles) → `anchor_state`.
- Cache: key = sha256(pass text, scope text, paper.json, model, thinking, runner); hit returns the cached run.
- API: `GET /api/passes`, `POST /api/runs {pass, scope, runner, model, thinking}`, `GET /api/runs`,
  `GET /api/runs/{id}`, `POST /api/runs/{id}/cancel`, `GET /api/runs/{id}/prompt` (show/copy),
  `POST /api/runs/paste {pass, scope, raw}` (paste-back), `PATCH /api/findings/{id} {state}`,
  `GET /api/findings?file=`.
- WS events: `run.started`, `run.event` (streamed text / tool-less), `run.finished`, `run.failed`, `findings.updated`.

## Frontend

- Findings view + right sidebar in Editor: cards (severity color, level, category, quote,
  problem, why), click → editor highlight (CodeMirror decoration) + spec node highlight,
  `j`/`k` navigate, state buttons, level filter, run picker with cost/tokens/time, Show
  prompt dialog with Copy and a Paste-back box, Cancel button.
- Run launcher: pass, scope (paper / section / file / selection), runner, model, thinking.

## Tests

- front matter parsing, template rendering, author stripping, validator (rewrite rejection,
  preamble strip, fenced JSON), anchoring + re-anchoring, cache hits, DB roundtrip,
  runner event parsing from fixtures, cancel kills the process group (use a fake runner
  command `sleep`), `paper.json` invariant (runner/prompts/findings modules never write it,
  and a run with a malicious output containing paper.json content does not change the file).

## Manual checks

1. Run `paragraph-function` on one section with claude; events stream, findings appear, each highlights the right span.
2. Same with codex.
3. Cancel mid-run; the process is gone (`pgrep`).
4. Re-run unchanged scope → instant cached result with the cache badge.
5. Edit the quoted paragraph; the finding re-anchors or shows stale.
6. Show prompt matches what was sent (compare with stored prompt); copy prompt works; paste-back parses.
7. A pass file edited to ask for rewrites produces rejected findings visible in run detail.

## Verified on this machine (2026-10-18, before phase 4 started)

- `echo <prompt> | claude -p --output-format stream-json --verbose --tools "" --no-session-persistence --model sonnet`
  works from a scratch cwd; the `system/init` event reports `tools: []`; final `result` event carries
  `total_cost_usd`, `usage.{input_tokens,output_tokens,cache_*}`, `duration_ms`, `is_error`, `result` (text).
  Fixture: `cli-help/claude-stream.jsonl`.
- `echo <prompt> | codex exec --json --sandbox read-only --skip-git-repo-check --ephemeral -c model_reasoning_effort=low`
  works; events `thread.started`, `turn.started`, `item.completed{item.type: agent_message, text}`,
  `turn.completed{usage}`. stderr prints "Reading prompt from stdin...". No cost field (compute from
  tokens or leave null). Fixture: `cli-help/codex-stream.jsonl`.
- Both are logged in here, so phase 4 can run a real smoke test end to end.

## Decisions fixed for implementation

- **Thinking mapping.** UI exposes `low | medium | high`. claude: `--effort low|medium|high`
  (installed CLI also accepts xhigh/max; config maps high → "high" by default, user may edit).
  codex: `-c model_reasoning_effort=low|medium|high`.
- **Model.** `model: null` or `""` → no model flag (the CLI's own default). Config `[models]` keyed by
  level gives the default `model` and `thinking` per pass level; `[runner.claude].models` and
  `[runner.codex].models` are suggestion lists for the UI picker (free text allowed).
- **Scope key** (one active run per scope): `f"{pass}:{scope.kind}:{scope.ref or scope.file}"`.
- **Cache key:** sha256 over (pass file text, system.md text, scoped source text after author strip,
  paper.json bytes, runner, model, thinking). A hit creates a new run row with `status: "cached"` and
  `cached_from` pointing at the original run; its findings are copied so states are independent.
- **Severity:** `minor | major | critical`. **Category:** free string from the pass (the schema lists
  the expected ones per level in the prompt but the validator only requires non-empty).
- **Findings schema** (what the model must return): `{ "findings": [Finding] }` where Finding has
  exactly `file, line_start, line_end, quote, level, category, severity, problem, why, json_ref`
  (`json_ref` nullable). Unknown keys → the finding is rejected with reason `unknown_field:<k>`
  (this is how a `replacement`/`rewrite`/`suggestion` key gets caught). Additionally, unless the
  pass has `allow_rewrites: true`, any of `problem`/`why` containing a quoted span of ≥ 8 words
  introduced by "replace with", "rewrite as", "change to", "instead write", "should read", "→" is
  rejected with `rewrite_text`. Praise filter: a finding whose `problem` is empty or matches
  /^(good|great|nice|well|excellent|clear|strong)\b/i is rejected `praise`.
- **Preamble strip + parse:** take the last ```json fenced block if any; else the first `{` to the
  last `}`; `claude` with `--json-schema` returns structured output in the result event — prefer
  that when present (`structured_output` field) and fall back to text parsing.
- **Anchoring:** exact `quote` search in the scoped file (whitespace-collapsed match, first
  occurrence nearest `line_start`) → `anchor_state: "ok"` with the real `anchor_line_start/end`;
  else use `line_start/line_end` as given → `anchor_state: "unverified"`; on later file change,
  re-search by quote → `"ok"`/`"moved"`, else `"stale"`. Re-anchoring runs on `file.changed` and
  on `PUT /api/file` for open findings in that file; emits `findings.updated`.
- **Source numbering:** `source` is `"{n:>4} | {line}"` per line; `\author{...}` (balanced braces,
  multi-line) is replaced by `\author{[removed]}` before numbering so line numbers stay aligned.
- **Scope resolution:** `paper` = all files in include order (each file block prefixed with
  `=== file: <path> ===`); `section` = the section node's anchored file from its heading line up to
  the next `\section`/end (ref = node id; planned/orphaned section → 400); `file` = one file;
  `selection` = `{file, line_start, line_end}` ± 0 lines with the whole file available? No: just the
  selected lines, with 3 lines of context before/after marked as context.
- **Sandbox:** `.rewrite/sandbox/` contains only CLAUDE.md and AGENTS.md (regenerated from
  system.md on every run if system.md is newer). Process: `start_new_session=True`, cwd sandbox,
  env minus `CLAUDECODE`/`CLAUDE_CODE_*` vars (so a nested claude works), prompt on stdin, stdout
  read line by line; cancel = `os.killpg(SIGTERM)` then SIGKILL after 3 s.
- **Clean context:** no `--resume`/`--continue`/`--session-id` ever; `--no-session-persistence`
  for claude, `--ephemeral` for codex. Global instruction files: test whether `claude --bare` /
  `--setting-sources ""` and `codex --ignore-user-config --ignore-rules` suppress `~/.claude/CLAUDE.md`
  / `~/.codex/AGENTS.md` by writing a canary line into a TEMPORARY global file? NO — never write to
  global paths. Instead: read `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` if they exist, note in
  `GET /api/runners` (`global_instructions: {path, present}`) and in config.toml comments which
  flags the installed CLIs offer (`--bare`, `--setting-sources`, `--ignore-user-config`,
  `--ignore-rules`); use `--setting-sources project` for claude and `--ignore-user-config
  --ignore-rules` for codex when the installed version accepts them (probe `--help` once at startup
  and cache), and record in config `[runner.<x>].suppress_global = true|false`.

## API contract (fixed; backend and frontend built in parallel)

Types:
```
Scope   = { kind: "paper"|"section"|"file"|"selection", ref: str|null, file: str|null,
            line_start: int|null, line_end: int|null }
Pass    = { name, level, scope: str (default scope kind), allow_rewrites: bool, model: str|null,
            thinking: str|null, file: str, description: str (first body line, ≤120 chars) }
Run     = { id: str, pass: str, level: str, scope: Scope, scope_name: str, runner: "claude"|"codex"|"paste",
            model: str|null, thinking: str|null, status: "queued"|"running"|"done"|"failed"|"cancelled"|"cached",
            cached_from: str|null, git_sha: str|null, started_at: float|null, finished_at: float|null,
            duration_ms: int|null, input_tokens: int|null, output_tokens: int|null, cost_usd: float|null,
            error: str|null, findings_count: int, rejected_count: int }
Finding = { id: str, run_id: str, file: str, line_start: int, line_end: int, quote: str, level: str,
            category: str, severity: "minor"|"major"|"critical", problem: str, why: str, json_ref: str|null,
            state: "open"|"addressed"|"dismissed", anchor_state: "ok"|"unverified"|"moved"|"stale",
            anchor_line_start: int, anchor_line_end: int, created_at: float }
Rejected = { index: int, reason: str, raw: object }
```
Endpoints:
- `GET /api/passes` → `{ passes: Pass[] }` (hot-reloaded from `.rewrite/prompts/passes/`; files
  with a bad front matter appear with `error: str` and are not runnable).
- `GET /api/runners` → `{ runners: [{ name, available: bool, models: str[], suppress_global: bool }],
  thinking_levels: ["low","medium","high"], defaults: { [level]: { runner: str, model: str|null, thinking: str } },
  global_instructions: [{ runner, path, present: bool }] }`
- `POST /api/runs/preview { pass, scope, runner?, model?, thinking? }` → `{ prompt: str, scope_name: str,
  cache_hit: bool, model: str|null, thinking: str|null, command: str[] }` (`command` is the argv that
  would run, for display).
- `POST /api/runs { pass, scope, runner?, model?, thinking?, force?: bool }` → `{ run: Run }` at once
  (status queued/running/cached). 409 `{detail}` if the scope key already has an active run. 400 for
  unknown pass / unresolvable scope / unavailable runner.
- `GET /api/runs?limit=50` → `{ runs: Run[] }` newest first.
- `GET /api/runs/{id}` → `{ run: Run, findings: Finding[], rejected: Rejected[], raw_output: str|null }`
- `GET /api/runs/{id}/prompt` → `{ prompt: str, command: str[] }`
- `POST /api/runs/{id}/cancel` → `{ run: Run }` (204-like no-op if already finished → returns the run).
- `POST /api/runs/paste { pass, scope, raw }` → `{ run: Run }` (runner "paste", status done/failed, findings validated exactly as a live run).
- `GET /api/findings?file=&state=&run_id=` → `{ findings: Finding[] }` (all filters optional; default
  excludes nothing).
- `PATCH /api/findings/{id} { state }` → `{ finding: Finding }`.
- WS events: `run.started { run }`, `run.event { run_id, kind: "text"|"status", text }` (text deltas
  or status lines, throttled to ≤ 10/s), `run.finished { run }`, `run.failed { run }`,
  `findings.updated { run_id: str|null, file: str|null }`.

## Status notes

### 2026-10-18 — backend half done

- New: `app/prompts.py` (passes: YAML front matter + Jinja2 body, mtime/size hot reload,
  `render_outline`, `render_claims`, author strip, numbering, scope resolution,
  `assemble_prompt`, `ensure_sandbox`), `app/findings.py` (schema, parsing, validator,
  anchoring), `app/runner/{base,claude,codex,paste}.py`, `app/runstore.py` (rows),
  `app/runs.py` (RunManager: plan, cache, scope lock, slots, execution, cancel, paste,
  re-anchoring), `app/api/runs.py`, `app/api/findings.py`. `db.py` migration 2 (`runs`,
  `findings`, `rejected_findings`; cache key is a column on runs) and migration 3
  (`findings.origin_line_*`). `PUT /api/file` is now async and re-anchors after a save;
  the run manager also re-anchors on every watcher `file.changed`. `pyyaml` is a runtime dep.
- Tests: 459 passed (+3 docker-gated skips), ruff check/format clean. New: test_findings.py,
  test_prompt_assembly.py, test_runner.py, test_runs_api.py (fake CLIs printing real
  stream shapes); test_invariants.py, test_config.py, test_db.py, test_prompts.py extended.
  Fixtures in `backend/tests/fixtures/runner/` (captured help texts, real streams incl. the
  two smoke review runs).

Verified argv (probe of `--help` decides the optional groups; prompt always on stdin):
- claude 2.1.302: `claude -p --output-format stream-json --verbose --tools "" --no-session-persistence
  --setting-sources project --strict-mcp-config --include-partial-messages [--model M]
  --effort low|medium|high --json-schema <schema JSON>`
- codex 0.162.1: `codex exec --json --sandbox read-only --skip-git-repo-check --ephemeral
  --disable shell_tool --disable unified_exec --ignore-user-config --ignore-rules [-m M]
  -c model_reasoning_effort=low|medium|high --output-schema <tmp>/findings.schema.json`

Findings from the CLIs (empirical, no global path written):
- claude `--json-schema` works with `--tools ""`: init lists only the synthetic
  `StructuredOutput` tool; `result.structured_output` carries the answer. Any other tool in
  init (or a tool_use block) aborts the run.
- `--setting-sources project`: the init event has no CLAUDE.md field; it drops user plugin
  skills (28→19) and claude.ai MCP servers (9→0). With a temp `CLAUDE_CONFIG_DIR` holding a
  canary CLAUDE.md, local `/context` (no API call) lists the User memory file by default and
  not with `--setting-sources project` (the sandbox's project CLAUDE.md stays). Help text:
  "Comma-separated list of setting sources to load (user, project, local)". `--bare` is not
  usable: it reads only ANTHROPIC_API_KEY (no OAuth).
- codex `--sandbox read-only` still let the model run `ls /` (command_execution items);
  `--disable shell_tool --disable unified_exec` stops it, so they are `no_tools_args`.
  A codex tool item aborts the run.
- codex always injects `$CODEX_HOME/AGENTS.md` (temp CODEX_HOME canary via
  `codex debug prompt-input`; `-c project_doc_max_bytes=0` only drops the project doc).
  `--ignore-user-config` skips config.toml (and the user's model/effort), `--ignore-rules`
  skips execpolicy rules; neither names AGENTS.md → `[runner.codex] suppress_global = false`.
  `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` do not exist on this machine.
- codex events do not name the model; with `--ignore-user-config` it uses its built-in
  default (catalog priority 1 is gpt-6.1-sol; the user config's gpt-6-astra/xhigh is skipped).

Live smoke (`rewrite serve` on a git copy of the sample paper, 127.0.0.1:8771 per `ss`;
Introduction + 4 paragraphs adopted via the API, `paragraph-function`, scope section):
- claude sonnet/low: done in 13.4 s, 3 findings, 0 rejected, 18,755 in / 1,714 out tokens,
  $0.0578, git_sha recorded; all 3 anchored `ok` at 13-14, 19, 28 (quotes found in those
  lines). WS: run.started, 4 status + 35 text run.events (min gap 0.099 s), run.finished.
  Stored prompt == preview prompt (10,392 chars), stored argv == preview argv.
- Re-run: `cached` in 0.03 s, cached_from the first run, findings copied (cost 0.0).
- codex default/low: done in 21.8 s, 5 findings, 0 rejected, 18,549 in / 710 out, cost null,
  all anchored `ok`. Stored argv = preview argv except the schema placeholder → temp path.
- Cancel: claude ran as pid 456552 in its own group (pgid = pid); cancel sent ~0.55 s after
  start, response `cancelled`; the process group was gone and `pgrep -af "claude -p"` empty.
- Re-anchor on save (watcher on): +2 lines → `moved` 13→15, 19→21, 28→30, with
  `findings.updated {file}`; rewording a quoted sentence → `stale`.
- A pass edited to demand replacement sentences (haiku/low, selection 13-16, $0.0025):
  1 finding rejected `rewrite_text`, visible in GET /api/runs/{id}.

Endpoint shapes: contract shapes exactly; optional extras marked +:
- Pass + `error`, `runnable` (ab-compare: runnable false, it needs version_a/version_b).
- Run + `created_at`, `model_used` (model the CLI reported), `allow_rewrites`.
- GET /api/runners runner + `path`, `error`, `flags {model, thinking, schema, isolation,
  stream}`, `missing_flags`. Preview + `runner`, `scope` (normalized), `cache_key`.
  GET /api/runs/{id} + `stderr`. Rejected index -1 = an extra top-level key in the answer.
- Errors: 400 `{detail}` for unknown/unrunnable pass, unresolvable scope (planned/orphaned
  section, protected or missing file, bad range), unknown/unavailable runner, bad thinking,
  broken spec, malformed body (validated by hand, not FastAPI's 422); 409 active scope key;
  404 unknown run/finding; 403 cross-origin POST/PATCH.
- WS: `run.started` when the process starts (a queued run first gets a `run.event` status);
  `run.finished` for done/cached; `run.failed` for failed/cancelled.

Judgment calls (flag if wrong):
- Config: `[runner.<cli>]` keys `command, model_flag, thinking_args, thinking, schema_args,
  no_tools_args, isolation_args, suppress_global, stream_args, models`; `[runner] timeout_s`
  (1200). Loader-fills approach: missing keys in a `[runner.claude|codex]` table take that
  CLI's defaults in memory (pydantic before-validator); the file is not rewritten and
  `rewrite init` keeps an existing config.toml. `[models.<level>]` gained `runner`
  (absent → `[runner] default`).
- `suppress_global` is a recorded fact per CLI; the API reports it only when the CLI also
  accepts `isolation_args`.
- Model: request value wins (`""` = CLI default, no flag; null = defaults); front matter
  and `[models.<level>].model` apply only when the run uses that level's runner, so a
  claude model name never reaches codex.
- Author strip also replaces `\affiliation`, `\email`, `\institute`. Section, file and
  selection sources also carry a `=== file: <path> (lines a-b) ===` header so the model can
  fill `file`. The outline includes the claim bank; default passes use only
  `{{ spec_outline }}` (no duplicate `{{ claims }}`). Pass description = first line of a
  leading `{# ... #}` comment.
- A finding whose file is not in scope is mapped by quote, path suffix/basename, or (one-file
  scope, same extension) to the scope file; otherwise rejected `invalid:file`. Line ranges
  given backwards are reordered.
- Re-anchor: `ok` when the quote is back at its first anchor (migration 3), `moved`
  elsewhere, `stale` when gone; dismissed/addressed findings are not re-anchored.
- Cached run: cost 0.0, tokens null, duration 0, findings re-anchored and `open`. Paste
  runs are never cached, take no scope lock and never seed the cache.
- Env for the CLIs: also drops CLAUDE_PID and CLAUDE_EFFORT (a parent session's effort could
  override `--effort`); keeps CLAUDE_CODE_OAUTH_TOKEN and provider switches. codex's schema
  file lives in a per-run temp dir (deleted after), so the sandbox holds only CLAUDE.md and
  AGENTS.md. The `--help` probe is cached per process (restart after upgrading a CLI).
- Runs left queued/running by a dead server are marked failed ("interrupted") at startup.
- Praise rule is exactly as decided (`^(good|great|nice|well|excellent|clear|strong)\b`):
  it would also reject e.g. "Strong claim without evidence"; system.md asks the model to
  start `problem` with what it concerns to avoid that.

Not verified:
- Manual checks 1-7 in the browser (the frontend half); their backend halves ran live above.
- macOS. The 20-minute timeout only with a 0.5 s timeout in tests. Whether
  `codex exec --ignore-user-config` would still inject a real ~/.codex/AGENTS.md (not
  observable without moving auth; help text and prompt-input say it is not suppressed).
- Which model codex used by default (events do not say).

### 2026-10-18 — phase 4 complete (integration)

- Backend 459 passed (+3 docker-gated); frontend 39 e2e against the mock; live
  `frontend/e2e/live/p4-e2e.mjs` 19/19 with a real claude sonnet run (2 findings, $0.033, 8.6k/434
  tokens, 18 stream events, cached on re-run, preview prompt identical to the stored prompt) and
  19/19 with a real codex run (2 findings, 17.2k/375 tokens, no cost reported).
- Verified via API on the same server: cancel 0.6 s after start → `cancelled`, no `claude -p` left;
  inserting lines moved a finding 19→20 (`moved`), rewording its quote → `stale`; paste-back with a
  `replacement` key and a praise finding → both rejected (`unknown_field:replacement`, `praise`);
  sandbox contains only CLAUDE.md and AGENTS.md.
- Manual checks 1–7 verified headlessly on Linux. Not verified: macOS; the real 20-minute timeout;
  whether codex's `--ignore-user-config` drops a real `~/.codex/AGENTS.md` (backend agent found
  `$CODEX_HOME/AGENTS.md` is always injected → `suppress_global = false` for codex).
- Judgment calls: codex gets `--disable shell_tool --disable unified_exec` because `--sandbox
  read-only` alone still let the model run `ls`; any tool event aborts the run; the editor column
  shows only the newest run per pass+scope.

### macOS acceptance 2026-10-19

| Check | Result | How |
| --- | --- | --- |
| 1 `paragraph-function` on one section with claude: stream, findings, highlights | pass | p4 live 20/20: 3 findings, 22 stream events, $0.0341, 7,061/584 tokens, highlight on the quoted text, j/k, Addressed |
| 2 same with codex | pass | API probe: codex (thinking low) on the Introduction → done, 1 finding, 17,620/274 tokens, no cost; also the codex member of the p6 "Run both" group |
| 3 cancel mid-run, process gone | pass | cancel 1.5 s after start → `cancelled`; `pgrep -f 'claude -p'` 0 → 1 → 0 |
| 4 re-run unchanged scope → cached badge | pass | p4 (`status cached`, `cached_from` set) |
| 5 edit the quoted paragraph → re-anchor or stale | pass | two lines inserted above the quote → `moved` (5 → 7); quote reworded → `stale` |
| 6 Show prompt = stored prompt; copy prompt; paste-back parses | pass / partial | Show prompt identical to `GET /api/runs/{id}/prompt` (9,743 chars); paste-back of a fenced JSON answer → 1 finding, 2 rejected (`unknown_field:replacement`, `praise`). Clipboard "copy prompt" not verified (no clipboard in the harness) |
| 7 a pass edited to ask for rewrites → rejected findings in run detail | partial | a hot-loaded `rewrite-bait` pass demanding a `replacement` key ran on claude sonnet: 2 findings, 0 rejected — claude's `--json-schema` structured output never lets the model emit the extra key (same on Linux, where rejection was verified through paste-back only). The validator and the run-detail "rejected" list are verified via paste-back (check 6) |

- Both CLIs log in through the macOS keychain: `GET /api/runners` reports claude `logged_in` via
  claude.ai (team subscription) and codex via ChatGPT; `~/.claude/CLAUDE.md` and
  `~/.codex/AGENTS.md` absent. Sandbox holds only `AGENTS.md` and `CLAUDE.md` after the runs.
- Cost of the whole acceptance: roughly $0.35 across p4, p5, p6 (twice) and the API probes.

### 2026-10-19 — thinking levels xhigh and max

- The UI and `THINKING_LEVELS` now expose `low | medium | high | xhigh | max` for both runners.
  claude 2.1.303 lists them in `--effort`; codex 0.163.0's API answers "Supported values are:
  none, minimal, low, medium, high, xhigh, max" to an unknown `model_reasoning_effort`, and a
  `max` run started normally. The default `[runner.*].thinking` tables map all five; an older
  config.toml with three keys passes the new levels through unchanged
  (`RunnerCliConfig.thinking.get(level, level)`). Tests: `test_xhigh_and_max_thinking_reach_both_clis`,
  `test_old_three_level_thinking_table_passes_new_levels_through`.

### 2026-10-19 — writing styles (the field's standard of evidence)

- `prompts/styles/<name>.md` (front matter `name`, `title`, `description`; body with five fixed
  headings), copied by `rewrite init`, loaded by `prompts.load_styles` (README.md skipped).
  `[paper] style` in config.toml names one; `GET/PUT /api/config` expose `paper.style` and the
  `styles` list (400 for a name without a file). `system_with_style` appends the body to
  system.md as "## The field's standard of evidence" in `assemble_prompt`, `assemble_triage_prompt`
  and `assemble_compare_prompt`; the result is `AssembledPrompt.system_text`, so the cache key and
  `prompt_hash` cover it. Passes opt out with `style: false` (latex-hygiene, ab-compare). A
  configured style with no file is a PromptError at run time, never a silent drop.
- Eleven styles drafted from Alan Synn's "Same Computer Science, Different Senses of Reality".
  Tests: `tests/test_styles.py` (7); Settings dropdown e2e in `synctex.spec.ts`.
