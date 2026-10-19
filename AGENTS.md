# rewrite (instructions for Claude Code and Codex)

Read `PLAN.md` before doing anything. It is the source of truth for architecture,
file layout, data model and phase order. Do not redesign what it decides; if you
believe a decision is wrong, say so and stop rather than silently deviating.

## Current phase

All six phases are implemented, Linux-verified (2026-10-18) and run through their macOS
acceptance (2026-10-19). Phases 1, 2, 3 and 5 are macOS-verified (PLAN.md's definition of
done). Phase 4 and phase 6 each have two checks verified only in part (clipboard copy,
rewrite rejection from a live pass, triage flagging nothing trivial) and phase 5's git panel
was exercised against a local bare remote, not a real GitHub remote; see the
`### macOS acceptance 2026-10-19` tables in `.agents/scratch/phase-N-*.md`. The optional Tauri
wrap is documented in `docs/tauri.md` but not built. Briefs and per-phase status live in `.agents/scratch/`.
See "Build phases" in PLAN.md for the scope of each phase. Do not start phase N+1 work while phase N's manual checks are open.

## Conventions

- Backend: Python 3.12, FastAPI, `uv` for dependencies, `ruff` for lint/format,
  `pytest` for tests. Type hints everywhere; pydantic models at every boundary.
- Frontend: Vite + React + TypeScript, Tailwind + DaisyUI. The author does not
  write TypeScript, so keep the frontend conventional and well-commented.
- Everything per-paper lives in the paper repo under `.rewrite/`. Never write
  to `~/.config`, `~/.local` or any global path.
- `paper.json` is written only by the spec editor. No code path may write it
  from LLM output. Treat this as a hard invariant and add a test for it.
- LLM CLIs run with cwd `.rewrite/sandbox/`, tools disabled, prompt on stdin.
  Flags live in `config.toml`; verify them with `claude --help` and
  `codex exec --help` on this machine before hard-coding anything.
- Bind the server to `127.0.0.1` only.
- Before marking a phase done, run its manual checks from the phase brief and
  list which ones you could not verify yourself.

## Running

- `make start [REPO=<paper repo>]` / `make stop` / `make status`: the built UI served by the
  backend on 127.0.0.1:8765 (pid and log under `.run/`, gitignored). Without REPO, macOS gets a
  Finder folder picker; the UI's Open dialog can also browse folders (`GET /api/fs/dirs`). `make dev REPO=...` runs
  the backend with `--reload` on :8000 and Vite on :5173. `make test` runs every suite.
- By hand — backend: `cd backend && uv run uvicorn app.main:app --reload`;
  frontend: `cd frontend && npm run dev`.
- Point the app at a paper repo: `make init REPO=<repo>` (or `rewrite init <repo>`) then open it in the UI.
