# rewrite (instructions for Claude Code and Codex)

Read `PLAN.md` before doing anything. It is the source of truth for architecture,
file layout, data model and phase order. Do not redesign what it decides; if you
believe a decision is wrong, say so and stop rather than silently deviating.

## Current phase

All six phases are implemented and Linux-verified (2026-10-18); macOS acceptance,
PLAN.md's definition of done, is still pending for every phase. The optional Tauri
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

- Backend: `cd backend && uv run uvicorn app.main:app --reload`
- Frontend: `cd frontend && npm run dev`
- Point the app at a paper repo: `rewrite init <repo>` then open it in the UI.
