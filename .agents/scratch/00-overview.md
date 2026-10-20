# rewrite — phase breakdown

Source of truth: `PLAN.md` at the repo root. This directory holds one brief per
phase, each with scope, file layout, API contract, tests and manual checks.
PLAN.md's definition of done is that a phase's manual checks pass on macOS.
Every phase below is implemented; its checks were run headlessly on Linux
(2026-10-18) and then on macOS (2026-10-19: automated suites, the live scripts
against the real backend with real claude/codex calls, headed-Chromium passes
for the gestures, and API probes). Phases 1, 2, 3 and 5 are macOS-verified;
phases 4 and 6 have two checks each that could only be verified in part (see
their briefs). Each brief's status section lists exactly what was and was not
verified, with a `### macOS acceptance 2026-10-19` table per phase.

| Phase | Brief | Status |
| --- | --- | --- |
| 1 Skeleton | `phase-1-skeleton.md` | macOS-verified 2026-10-19 (Linux 2026-10-18) |
| 2 SyncTeX + Docker | `phase-2-synctex-docker.md` | macOS-verified 2026-10-19 (Linux 2026-10-18) |
| 3 Spec editor | `phase-3-spec-editor.md` | macOS-verified 2026-10-19 (Linux 2026-10-18) |
| 4 Pass runner | `phase-4-pass-runner.md` | Linux-verified 2026-10-18; macOS 2026-10-19: checks 1–5 pass, 6–7 partial (clipboard copy not verified; a rewrite-baiting pass yields no rejected findings under claude's structured output, validator proven via paste-back) |
| 5 A/B, git, coverage | `phase-5-ab-git-coverage.md` | macOS-verified 2026-10-19 (Linux 2026-10-18); push/pull against a real GitHub remote still open (gh token invalid on this machine) |
| 6 Polish | `phase-6-polish.md` | Linux-verified 2026-10-18; macOS 2026-10-19: checks 1 and 3 pass, 2 partial (the model flagged nothing trivial, so "Dismiss all flagged" never ran); Tauri wrap documented, not built |
| 7 Drafting from the spec | `phase-7-drafting.md` | built and verified 2026-10-20 (backend tests, mock e2e, one real draft through the dialog on macOS) |
| 8 Revise (comments, rewrite proposals, \aigen) | `phase-8-revise.md` | built and verified 2026-10-20 (backend tests, mock e2e, one real rewrite on macOS) |

## Environment facts (macOS acceptance machine, 2026-10-19)

- macOS 27.0.1 on arm64, uv 0.12.21 (Python 3.12.8), node 26.10.0, npm 11.19.1, git 2.54.0
  (Apple Git-157), MacTeX (TeX Live 2026): latexmk 4.87, synctex CLI 1.5, Docker Desktop 29.8.2,
  claude 2.1.303 (claude.ai team subscription), codex 0.163.0 (ChatGPT), gh 2.102.0 with an
  invalid keyring token (SSH to GitHub works).
- `$TMPDIR` is under `/var/folders` (→ `/private/var`); the sample paper served from there
  compiles and syncs both ways.
- Logs and screenshots of the acceptance runs: `~/rewrite-live/` (not in the repo).

## Environment facts (Linux build machine, 2026-10-18)

- Linux (Ubuntu 24.04-class), Python 3.12.3, uv 0.13.1, node 24, npm 11, docker 29, git 2.43.
- `claude` 2.1.302 and `codex` 0.162.1 are installed. Help output captured for
  phase 4 in `cli-help/` next to this file.
- TeX Live + latexmk installed from apt during phase 1 (not present at start).
- macOS is the primary target in PLAN.md; we build and verify on Linux here and
  keep macOS-specific paths (`/private/var`) handled in code.

## Conventions carried through every phase

- Backend: `backend/` is a uv project, package `app`, run with
  `uv run uvicorn app.main:app --reload`. `ruff` + `pytest`. Pydantic at every
  boundary. Bind `127.0.0.1` only.
- Frontend: `frontend/` is Vite + React + TS + Tailwind + DaisyUI. Dev server
  proxies `/api` and `/ws` to `127.0.0.1:8000`. Heavily commented; the author
  does not write TypeScript.
- Per-paper state only under `<paper repo>/.rewrite/`. Never global paths.
- `paper.json` is written only by the spec editor endpoint (phase 3). A test
  asserts no module under `app/runner/` or `app/prompts.py` references a
  `paper.json` write.
- The backend serves one paper repo per process, chosen by `--repo` / env
  `REWRITE_REPO`, or opened via `POST /api/repo/open` from the UI.
- Default prompt set lives in `prompts/` at the app repo root and is copied by
  `rewrite init`.

## Shared API shape (established in phase 1, extended later)

All JSON. Errors are `{ "detail": str }` with 4xx/5xx.

- `GET /api/health` → `{ ok, version, tools: {latexmk, docker, claude, codex, git}: bool, repo: str|null }`
- `GET /api/repo` → `{ path, main, initialized }`
- `POST /api/repo/open {path}` → same; 400 if no `.rewrite/`
- `POST /api/repo/init {path}` → runs init, returns `GET /api/repo`
- `GET /api/files` → `{ main, files: [{path, included_by}] }` from `\input`/`\include` resolution
- `GET /api/file?path=` → `{ path, content, mtime }`
- `PUT /api/file {path, content}` → `{ path, mtime }`
- `POST /api/compile` → `CompileResult` `{ ok, pdf: str|null, log: str, errors: [{file,line,message,severity}], duration_ms }`
- `GET /api/pdf` → the built PDF (application/pdf)
- `WS /ws/events` → server events: `{type: "compile.started"|"compile.finished"|"file.changed", ...}`
