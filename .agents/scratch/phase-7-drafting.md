# Phase 7 — Drafting from the spec

Source of truth: `PLAN.md` ("Decided 2026-10-20: drafting is a separate activity from
review"). This brief records the design agreed with the author on 2026-10-20.

## Scope

- One paragraph node at a time, on request (⋯ → "Draft from spec…" on a paragraph row). A
  draft realizes the node: its `says`, `function`, linked claims (evidence, scope), in the
  voice of the section's current LaTeX. Section and claim-sentence drafting can follow with
  the same stack (`prompts/drafts/<level>.md`); not built here.
- Separate prompt stack: `prompts/drafts/system.md` (drafting rules: say what the spec says,
  nothing invented, the authors' voice, one piece, compiling LaTeX, JSON only) and
  `prompts/drafts/paragraph.md` (front matter `kind: draft`). The paper's writing style
  (`[paper] style`) is appended as for review passes. Review `system.md` is not used.
- Nothing is written by the model call. The answer `{draft, notes}` is stored on a `drafts`
  row and shown in a dialog; the author edits it and chooses **Replace paragraph** (the node
  is anchored and its marker found) or **Insert here** (an unanchored node; the marker line
  is written with the text and the frontend then re-links the node through the existing
  `adopt {id}` path, so the LLM modules never touch paper.json). The text is written under
  the marker preceded by `% drafted by <runner> (<model>), run <id>: review before keeping`;
  a replaced paragraph comes up stale and re-read applies to it as to any edit.
- Never cached; one draft run per node at a time (scope key `draft:<node id>`); runs are
  ordinary runs (cancel, cost, prompt preview, live output) with kind-specific parsing: the
  answer must be a JSON object `{draft: non-empty str, notes?: str}`; a draft containing a
  heading, a `% @p` line or `\begin{document}` is rejected (failed run, raw output kept).

## API

- `POST /api/drafts {node_id, runner?, model?, thinking?}` → `{draft}` (status queued/running);
  400 unknown or non-paragraph node, unavailable runner, prompt errors; 409 a draft of that
  node is active.
- `GET /api/drafts?limit=` → `{drafts}` newest first; `GET /api/drafts/{id}` → `{draft, run}`.
- `POST /api/drafts/{id}/apply {mode: "replace"}` or `{mode: "insert", file, line}` →
  `{draft, file, line_start, line_end}`; 400 wrong mode for the node's state; 409 already
  applied or not done. Writes the `.tex` under the source lock and re-anchors findings.
- WS `draft.finished {draft}`.
- `Draft = {id, node_id, run_id, status: queued|running|done|failed, text, notes, error,
  created_at, finished_at, applied_at, applied_mode, applied_file, applied_line_start,
  applied_line_end}`.

## Tests

`tests/test_drafts.py` with the fake CLIs: prompt assembly (variables, style, no review
system.md), parse rules, lifecycle, apply replace/insert, 409s, invariants (drafts.py and
api/drafts.py join LLM_MODULES: no paper.json writes).

## Status notes

### 2026-10-20 — built

- Backend: `app/drafts.py` (DraftManager on the shared RunManager, `parse_draft`, `apply`),
  `app/api/drafts.py`, `prompts.assemble_draft_prompt` + `get_draft_template` +
  `load_draft_system` + `bib_keys`, `PassKind` gains `draft`, `RunPlan.free_text` (no
  structured-output schema), event `draft.finished`, state.db migration 7 (`drafts`).
  **Answer format changed from the plan**: not JSON. The first real draft came back with
  `\ref` and `\textsc` mangled, because the model wrote single-backslash LaTeX inside a JSON
  string where `\r` and `\t` are escapes. Drafts answer as one fenced ```latex block plus an
  optional `Notes:` line; `parse_draft` reads that. The JSON remark in the brief above is
  superseded.
- Frontend: `state/drafts.ts`, `spec/DraftDialog.tsx` (runner controls, live output, editable
  draft, Replace paragraph / Insert at cursor / Copy, history), ⋯ → "Draft from spec…" on
  paragraph rows, event wiring in App. Insert re-links the node through `adopt {id}`.
- Tests: `tests/test_drafts.py` (6: prompt stack, parse rules incl. backslashes, lifecycle +
  replace, insert + re-link, request errors and the one-draft-per-node 409, failed answer
  keeps the raw output); `e2e/drafts.spec.ts` (3) against `mock/drafts.mjs`.
- Verified on macOS with a real call: p-8 of the example drafted by claude sonnet in 6 s
  ($0.03), `\ref{fig:pipeline}` intact, Replace wrote the comment line and the text under the
  marker, the node came up stale, the editor opened at the line. Observed: the model's
  `Notes:` line can carry a literal tab where it wrote `\todo` in backticks (informational
  text only; the draft itself is read from the fenced block and is clean).
