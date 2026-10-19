# rewrite — phase breakdown

Source of truth: `PLAN.md` at the repo root. This directory holds one brief per
phase, each with scope, file layout, API contract, tests and manual checks.
PLAN.md's definition of done is that a phase's manual checks pass on macOS.
Every phase below is implemented and its checks were run headlessly on Linux
(automated suites plus Playwright against the real backend); macOS acceptance
is pending for all of them and is not claimed. Each brief's status section
lists exactly what was and was not verified.

| Phase | Brief | Status |
| --- | --- | --- |
| 1 Skeleton | `phase-1-skeleton.md` | Linux-verified 2026-10-18; macOS pending |
| 2 SyncTeX + Docker | `phase-2-synctex-docker.md` | Linux-verified 2026-10-18; macOS pending |
| 3 Spec editor | `phase-3-spec-editor.md` | Linux-verified 2026-10-18; macOS pending |
| 4 Pass runner | `phase-4-pass-runner.md` | Linux-verified 2026-10-18; macOS pending |
| 5 A/B, git, coverage | `phase-5-ab-git-coverage.md` | Linux-verified 2026-10-18; macOS pending |
| 6 Polish | `phase-6-polish.md` | Linux-verified 2026-10-18; macOS pending (Tauri wrap documented, not built) |

## Environment facts (this machine, 2026-10-18)

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
