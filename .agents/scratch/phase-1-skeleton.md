# Phase 1 — Skeleton

Status: implemented, Linux-verified 2026-10-18; macOS acceptance pending

## Scope (from PLAN.md "Build phases" 1)

FastAPI + Vite scaffold, open a repo, `rewrite init`, file tree from
`\input` resolution, CodeMirror editor with save, `NativeLatexmk`, PDF render,
error list, `Compiler` and `Runner` interfaces stubbed, `.rewrite/` layout
and gitignore handling.

## Backend layout

```
backend/
  pyproject.toml          uv project; deps: fastapi, uvicorn[standard], pydantic, pydantic-settings,
                          watchfiles, jinja2, tomli-w (py3.12 has tomllib), python-multipart
                          dev: pytest, pytest-asyncio, httpx, ruff
  app/
    __init__.py
    main.py               create_app(), lifespan, CORS off (same origin via vite proxy), routers, --repo arg
    config.py             pydantic model for .rewrite/config.toml, load/save
    repo.py               Repo object: root, rewrite dir, main file, init(), is_initialized
    cli.py                `rewrite` entry point: `rewrite init <repo>`, `rewrite serve [--repo] [--port]`
    files.py              tex file tree: resolve \input/\include/\subfile from main, read/write files safely (no path escape)
    compiler/
      __init__.py         Compiler Protocol, CompileResult, LogError models
      native.py           NativeLatexmk
      docker.py           DockerLatexmk STUB (raises NotImplementedError until phase 2)
      logparse.py         TeX log parser → [LogError(file,line,message,severity)]
    runner/
      __init__.py         Runner Protocol, RunResult models (stubbed; ClaudeRunner/CodexRunner raise NotImplementedError)
    events.py             in-process event bus → WebSocket broadcast
    api/
      __init__.py
      repo.py, files.py, compile.py, ws.py
  tests/
    conftest.py           tmp paper repo fixture (main.tex + sections/ + refs.bib)
    test_init.py          init creates layout, prompts copied, .gitignore appended idempotently
    test_files.py         \input resolution, path escape rejection, save roundtrip
    test_logparse.py      parses a fixture log with an error and a warning
    test_compile.py       compiles the fixture repo with native latexmk (skipped when latexmk missing)
    test_invariants.py    no code under app/runner or app/prompts.py writes paper.json (grep-based)
prompts/                  (app repo root) default prompt set: system.md + passes/*.md (phase 1 ships
                          the files with front matter and bodies from PLAN.md's table; refined in phase 4)
```

`config.toml` defaults written by init:

```toml
[paper]
main = "main.tex"

[compiler]
kind = "native"            # native | docker
texlive_year = 2024        # docker only

[runner]
default = "claude"
concurrency = 3

[runner.claude]
# verified in phase 4 against `claude --help`
command = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--tools", "", "--no-session-persistence"]
model_flag = "--model"
thinking = { low = "low", medium = "medium", high = "high" }   # mapped to --effort in phase 4

[runner.codex]
command = ["codex", "exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral"]
model_flag = "-m"
thinking = { low = "low", medium = "medium", high = "high" }   # -c model_reasoning_effort=<level>

[models]
message = { model = "", thinking = "high" }
claim = { model = "", thinking = "high" }
section = { model = "", thinking = "high" }
paragraph = { model = "", thinking = "medium" }
sentence = { model = "", thinking = "low" }
```

Init also writes `sandbox/CLAUDE.md` and `sandbox/AGENTS.md` from `prompts/system.md`,
creates `build/`, appends `.rewrite/state.db*` and `.rewrite/build/` to `.gitignore`
(idempotent), and writes an empty valid `paper.json` only if none exists
(`{"version":1,"message":{...},"claims":[],"tree":{...}}`) — this is the single
non-editor writer of paper.json and is allowed because it's a human-run CLI command
with no LLM input; document that in the test.

## Frontend layout

```
frontend/
  package.json            vite, react, typescript, tailwindcss, daisyui, @codemirror/*, @codemirror/legacy-modes (stex), pdfjs-dist
  vite.config.ts          proxy /api and /ws → http://127.0.0.1:8000
  src/
    main.tsx, App.tsx     top-level layout: header (repo path, compile button, status), tabs Editor | PDF | Spec | Findings (Spec/Findings are placeholders)
    api/client.ts         typed fetch wrappers for every endpoint in 00-overview.md
    api/ws.ts             WebSocket hook for /ws/events with reconnect
    editor/FileTree.tsx   list of files from GET /api/files
    editor/Editor.tsx     CodeMirror 6 with stex legacy mode, Ctrl/Cmd-S save, dirty indicator
    pdf/PdfViewer.tsx     pdfjs-dist canvas render, all pages stacked, zoom, re-fetch after compile.finished
    compile/ErrorList.tsx errors/warnings from CompileResult, click → open file at line
    state/store.ts        small zustand store (or React context) for repo, open file, compile result
```

Layout: left file tree (w-56), center editor, right PDF (split view). On narrow
screens tabs switch panes. Save → debounced compile (800 ms) via the store.

## Manual checks (phase definition of done)

1. `rewrite init <tmp repo>` creates `.rewrite/` with config, prompts, sandbox files, and gitignore lines; running it twice changes nothing.
2. `GET /api/health` reports which tools are on PATH and the open repo.
3. Opening the repo in the UI shows the file tree with `main.tex` and every `\input` file.
4. Editing a file and pressing Ctrl-S writes the file to disk; the dirty marker clears.
5. Compile produces `.rewrite/build/main.pdf` and the PDF pane shows it.
6. A deliberate LaTeX error (`\undefinedmacro`) appears in the error list with file and line; clicking it opens the file at that line.
7. Server listens on 127.0.0.1 only (`ss -ltnp`).
8. `pytest` and `ruff check` pass; `npm run build` and `tsc --noEmit` pass.

## Status notes

(filled in as the phase runs)

### 2026-10-18 — phase 1 complete

- Backend: 107 pytest tests pass, ruff clean. Frontend: build, typecheck, oxlint and 6 Playwright
  tests against `frontend/mock/server.mjs` pass.
- Live check script: `frontend/e2e/live/p1-e2e.mjs <repo> <screenshot dir>` against
  `rewrite serve --repo <repo> --port 8765` serving `frontend/dist`.
- Manual checks 1–8 verified on Linux (checks 3–6 via Playwright on the real backend:
  tree lists main + 4 inputs + refs.bib; Ctrl-S writes to disk and clears the dirty mark;
  compile produces `.rewrite/build/main.pdf` rendered on 3 canvases; `\undefinedmacro`
  shows as `sections/method.tex:3` and clicking it opens the file with the cursor on line 3).
- Not verified: anything on macOS (PLAN.md's definition of done names macOS). The
  `/private/var` log-path case is handled in code but untested against a real macOS log.
- Deviations recorded by the implementers: Tailwind v4 + DaisyUI 5, oxlint instead of ESLint;
  `PUT /api/file` refuses `paper.json`, `.git/`, build and sandbox paths; Host/Origin checks
  on the server; `.bib` files appear in the tree; `CompileResult` carries `synctex` and `exit_code`.
