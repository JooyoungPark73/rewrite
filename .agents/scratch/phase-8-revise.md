# Phase 8 — Revise: comments and rewrite proposals

Source of truth: `PLAN.md` ("Decided 2026-10-20 as well: Revise"). Design agreed with the author
on 2026-10-20: keep the original as a comment line (not the `changes` package); offer rewrites
on model findings too. With phases 7 and 8 the app covers Write, Revise and Review.

## Design

- **Comments are findings.** `POST /api/findings/comment {file, line_start, line_end, quote,
  problem, why?, json_ref?, severity?}` stores a run (runner `author`, pass `comment`, done) with
  one finding anchored by the quote (`findings.anchor_new`), level from `json_ref`'s node,
  category `comment`. Same card (with a "comment" badge), highlight, re-anchoring and states.
- **Revisions** (`app/revise.py`, `app/api/revise.py`): `POST /api/revisions {finding_id,
  span: quote|paragraph, runner?, model?, thinking?}` locates the span as the file reads now
  (the quote inside the finding's anchor lines, whitespace-collapsed, or the paragraph by the
  skeleton), assembles `prompts/drafts/rewrite.md` on the drafting system prompt (+ style), and
  runs it as an ordinary free-text run (one per finding at a time; never cached). The answer is a
  fenced LaTeX block (an empty block = drop the span) plus `Notes:`; `parse_rewrite` rejects
  headings, `% @p` lines and document environments.
- **Apply** (`POST /api/revisions/{id}/apply {text?}`): refuses when the span changed since the
  proposal (409); writes `% aigen-was: <original, one line>` above the span and the approved
  text as `\aigen{...}` per paragraph in place of the span (a partial-line span is replaced
  inside its line); ensures the preamble block in main.tex once (`\usepackage{xcolor}` only
  when neither xcolor nor color is loaded, `\providecommand{\aigen}...`, a commented
  `\renewcommand{\aigen}[1]{#1}`); marks the finding addressed; returns the outstanding
  count. `GET /api/revisions/outstanding` counts `\aigen{` in the include tree (comments
  ignored). `latex-hygiene` gains the `aigen-leftover` category. Event `revision.finished`;
  state.db migration 8 (`revisions`).
- **UI**: editor selection menu → "Comment…" → CommentBox; finding card → "Rewrite…" →
  RevisionDialog (span radio, runner controls, live output, word diff via `revise/wordDiff.ts`,
  editable text, Approve / Copy, history); header badge "N AI edits pending" (refreshed on
  `file.changed`). `state/revise.ts`. The runs store's `Run.runner` admits `author`.
- Tests: `tests/test_revise.py` (8), `e2e/revise.spec.ts` (2) against `mock/revise.mjs`.

## Status notes

### 2026-10-20 — built and checked live

- Backend 645 passed / 3 skipped, ruff clean; frontend typecheck, lint, build, e2e 81/81.
- Live on the example (claude sonnet, $0.001): a comment "Too verbose; say it in one sentence
  of at most 20 words" on p-2's second sentence (anchored across lines 14-16) got the proposal
  "Warm pools trade memory for latency, a poor trade for rarely invoked functions, whose idle
  instances are evicted unused."; Approve wrote the `% aigen-was:` line and the wrapper inside
  line 15, added the macro block before `\begin{document}`, outstanding = 1, the header showed
  "1 AI edit pending".
- Known shape: when the span starts mid-line, the `% aigen-was:` line sits between two lines
  of the same paragraph. A comment-only line does not break a LaTeX paragraph and the marker
  rule skips comments, so it renders and anchors correctly; it only looks odd in the source.
- Not built: "Accept all" / "Reject" of wrappers in the app (co-authors do it in Overleaf as
  agreed); re-anchoring a comment whose quote disappeared (add a fresh comment instead).
