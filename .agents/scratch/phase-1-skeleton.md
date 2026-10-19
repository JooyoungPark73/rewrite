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

### macOS acceptance 2026-10-19

Machine: macOS 27.0.1 (arm64), uv 0.12.21 (Python 3.12.8), node 26.10.0 / npm 11.19.1, git 2.54.0
(Apple Git-157), MacTeX: latexmk 4.87 + synctex 1.5 (TeX Live 2026, `/Library/TeX/texbin`), Docker
Desktop 29.8.2, claude 2.1.303 (claude.ai team subscription), codex 0.163.0 (ChatGPT), gh 2.102.0
(keyring token invalid). Live scripts ran from `frontend/e2e/live/run-all.sh ~/rewrite-live`.

| Check | Result | How |
| --- | --- | --- |
| 1 `rewrite init` twice changes nothing | pass | second init on a committed repo: `git status --porcelain` empty |
| 2 `/api/health` tools + repo | pass | all five tools true, repo path resolved to `/private/var/...` |
| 3 file tree: main + every `\input` | pass | p1 live script (main.tex + 4 sections + refs.bib) |
| 4 Ctrl/Cmd-S writes, dirty marker clears | pass | p1 (ControlOrMeta = Cmd on macOS) and a headed-Chromium pass with a real `Meta+s` |
| 5 compile → `.rewrite/build/main.pdf`, PDF pane | pass | p1 (3 pages on canvas) |
| 6 `\undefinedmacro` in the error list, click opens the line | pass | p1 (`sections/method.tex:3`) |
| 7 listens on 127.0.0.1 only | pass | `lsof -nP -iTCP:8765 -sTCP:LISTEN` → `127.0.0.1:8765` only |
| 8 pytest / ruff / build / tsc | pass | 597 passed, 3 skipped (Docker-gated; 19/19 with `REWRITE_TEST_DOCKER=1`); ruff clean; typecheck, oxlint, build clean |

- p1 live: 18/18. First run failed at the `\undefinedmacro` step: the script pressed `Control+Home`
  / `Control+End`, which CodeMirror binds as `Mod-Home` / `Mod-End` (Cmd on macOS), so the text was
  typed on the wrong line. Script assumption, not an app defect: the live scripts and the headless
  suite now press `ControlOrMeta+<key>` (Playwright's platform modifier); `e2e/portability.spec.ts`
  guards against a bare `Control+` chord coming back. Headless suite: 60/66 before the fix, 68/68
  after (66 + the 2 guard tests).
- `open http://127.0.0.1:8767/` from the shell loaded the UI in the default browser (new `GET /` in
  the server log). The backend itself has no auto-open code path (PLAN.md mentions `open`; not built,
  not a macOS defect).

### 2026-10-19 — "+ New file" in the Files column

- The file tree is derived from `\input` / `\include`, so a file only shows once something
  inputs it. `POST /api/files/new {path, include_in?}` creates an empty file (folders on the
  way too), and for a `.tex` file adds `\input{<path without .tex>}` to `include_in` (default:
  the main file) before its `\end{document}` (appended when there is none; skipped when the
  file is already input or included). 409 if the path exists; PUT /api/file's path rules apply.
  `FileTree` gets a "+ New file" form at the bottom that creates, reloads the tree and opens the
  file. Tests: `tests/test_files_new_api.py` (7), e2e in `smoke.spec.ts`; verified against the
  real backend (file on disk, `\input` line in main.tex, tree row, compile still 3 pages).
