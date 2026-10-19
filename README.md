# rewrite

A local, Overleaf-like editor for a LaTeX git repo plus an LLM flaw-finder that reviews the
paper against a human-owned `paper.json` semantic spec. See `PLAN.md` for the design and
`.agents/scratch/` for the phase briefs and what was verified in each phase.

## Requirements

- Python 3.12 and `uv`; Node 20+ and npm
- `latexmk` (TeX Live) on PATH, or Docker for the pinned `texlive/texlive:TL<year>-historic` image
- `git`; optionally the `claude` and `codex` CLIs, logged in, for review passes

## Run

The short way is the Makefile at the repo root:

```bash
make setup                   # uv sync, npm install, build the UI (once)
make start REPO=~/paper      # backend on 127.0.0.1:8765 serving the built UI; opens the browser
make stop                    # stop it (make status / make logs / make restart also exist)
make dev REPO=~/paper        # development: backend --reload on :8000 + Vite on :5173
make test                    # backend pytest + ruff, frontend typecheck + lint + build + e2e
```

`REPO` defaults to `$REWRITE_REPO`. With neither, `make start` shows a Finder folder picker on
macOS (a folder without `.rewrite/` is initialised first); cancelling it, or any other platform,
leaves the choice to the UI, whose "Open a paper repo" dialog has a **Browse…** folder browser
(`GET /api/fs/dirs`). The pid and log of `make start` live under `.run/` in this repo
(gitignored). By hand:

```bash
# backend (serves the built frontend from frontend/dist when it exists)
cd backend && uv sync && uv run rewrite serve --repo /path/to/paper [--port 8000]

# frontend in dev mode (proxies /api and /ws to 127.0.0.1:8000)
cd frontend && npm install && npm run dev

# production build of the UI
cd frontend && npm run build
```

`examples/sample-paper-with-spec/` is the sample paper with a fully filled `paper.json` and a
README that explains what each level of the spec is for and how to fill it.

Point the app at a paper repo once with `uv run rewrite init /path/to/paper` (idempotent):
it creates `.rewrite/` (config, prompts, sandbox, build dir), adds the gitignore lines and an
empty `paper.json`. The backend binds to `127.0.0.1` only.

## Checks

```bash
cd backend && uv run pytest -q && uv run ruff check . && uv run ruff format --check .
cd frontend && npm run typecheck && npm run lint && npm run build && npm run e2e   # e2e uses frontend/mock
REWRITE_TEST_DOCKER=1 uv run pytest -q tests/test_docker.py                    # real Docker compile
```

Live end-to-end scripts that drive the real backend with Playwright live in
`frontend/e2e/live/` (`p1-e2e.mjs` … `p6-e2e.mjs`); each prints PASS/FAIL lines and expects a
server on `127.0.0.1:8765` serving a freshly initialised copy of `examples/sample-paper`.

## Layout

```
backend/    FastAPI app (app/), tests
frontend/   Vite + React + TypeScript UI, Playwright e2e, mock backend for the e2e suite
prompts/    default prompt set copied into a paper repo by `rewrite init`
examples/   sample-paper used by the tests and the live checks
docs/       notes (e.g. the optional Tauri wrap)
```
