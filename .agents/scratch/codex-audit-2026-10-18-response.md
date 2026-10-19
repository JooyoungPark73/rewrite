# Response to the Codex audit of 2026-10-18

Every finding was checked against the cited code and reproduced where cheap; all eleven are
valid, and so are the acceptance-record points. Each item now has a regression test that
failed on the pre-fix code. Details and test names are in `phase-6-polish.md` under
"Audit fixes 2026-10-18 (backend)" and "(frontend)".

| # | Finding | Status | Where |
| --- | --- | --- | --- |
| 1 | Raw JSON draft from A overwrites B | fixed: drafts keyed by repo path, writes refused across a switch | `frontend/src/spec/RawJson.tsx`, `state/spec.ts`; `e2e/data-safety.spec.ts` |
| 2 | Repo switch discards unsaved source | fixed: save first, abort the switch on failure | `state/store.ts` |
| 3 | Navigation loses edits after failed/raced save | fixed: explicit save result + revision check, file stays open | `state/store.ts` |
| 4 | Raw save response erases newer typing | fixed: canonical text applied only if unchanged since submit | `spec/RawJson.tsx` |
| 5 | Marker adoption overwrites a concurrent save | fixed: per-repo source lock + `base_sha256` precondition (409); frontend sends it and shows a conflict bar | `backend/app/state.py`, `files.py`, `skeleton.py`, `api/*`; `tests/test_spec_api.py` |
| 6 | Undo after external reload removes incoming change | fixed: history cleared on external content; undo re-reads and refuses on drift | `state/spec.ts` |
| 7 | Codex reads the paper's ancestor AGENTS.md | fixed: `-c project_doc_max_bytes=0 -c project_root_markers=[]`; claude now `--setting-sources ""` (ancestor CLAUDE.md was loaded with `project`); probe lists outside instruction files in `/api/runners.context_leaks` and on runs | `backend/app/runner/context.py`, `config.py`; `tests/test_runner.py` |
| 8 | Section scope omits included bodies | fixed: sections resolved in document order across includes, one block per file | `backend/app/skeleton.py`, `prompts.py`; `tests/test_prompt_assembly.py` |
| 9 | Staged deletions/renames break commit | fixed: missing paths go through `git rm --cached` | `backend/app/gitops.py`; `tests/test_git.py` |
| 10 | Duplicate unlabeled headings anchor wrongly | fixed: ambiguous adoption → 409 asking for a `\label`; ambiguous marker → orphaned | `backend/app/skeleton.py`, `anchors.py` |
| 11 | Unbraced `\input` ignored | fixed: shared include regex accepts it | `backend/app/files.py`; `tests/test_files.py` |
| — | Acceptance records | status lines now "implemented, Linux-verified; macOS acceptance pending"; `p1-e2e.mjs` asserts and exits non-zero | `00-overview.md`, `CLAUDE.md`, `AGENTS.md`, briefs |

Verification after the fixes (2026-10-18): backend 583 passed + 3 docker-gated (19 pass with
`REWRITE_TEST_DOCKER=1`), ruff clean; frontend typecheck/lint/build clean, 66 Playwright
tests against the mock; live scripts p1–p6 all PASS on fresh repos against the real backend
(real claude and codex calls with the new isolation flags, including a repo carrying canary
CLAUDE.md/AGENTS.md at its root, reported as suppressed).

Remaining gaps, stated rather than hidden:
- macOS acceptance is still pending for every phase.
- Undo/redo has a few-millisecond window between its re-read and its PUT; closing it needs a
  server-side precondition on `PUT /api/spec` (not done).
- After a 409 on save there is no "overwrite anyway" button; the user reloads from disk.
- Codex still declares `functions.exec` (JavaScript), `request_user_input` and `collaboration.*`
  tools with every flag tried; shell/file tools are gone and any tool use aborts the run. See
  "Codex residual tool surface" in `phase-6-polish.md`.
- `--setting-sources ""` for claude was verified with a real logged-in run (sonnet, 2 findings).
