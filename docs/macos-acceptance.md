# macOS acceptance

PLAN.md defines a phase as done when its manual checks pass on macOS. Everything so far was
built and verified on Linux. Paste the prompt below into Claude Code from the root of a fresh
clone on the Mac. Phases 4 to 6 make real claude/codex calls, so run phases 1 to 3 first.

## Prompt

```
Read AGENTS.md, PLAN.md and .agents/scratch/00-overview.md. This repo was built and verified on
Linux; PLAN.md's definition of done is that each phase's manual checks pass on macOS, which is
what you will do now. Do not refactor or redesign anything; fix only what breaks on macOS, with a
regression test for each fix, and report honestly what you could and could not verify.

1. Environment. Confirm or install: uv, node 20+, latexmk + synctex (`brew install --cask
   mactex-no-gui`, or BasicTeX plus `sudo tlmgr install latexmk synctex`), git, Docker Desktop
   (optional), and the `claude` and `codex` CLIs logged in with a claude.ai / ChatGPT
   subscription (`claude auth status`, `codex login status`). Record versions.

2. Automated suites: `cd backend && uv sync && uv run pytest -q && uv run ruff check .`, then
   `cd frontend && npm install && npx playwright install chromium && npm run typecheck && npm run
   lint && npm run build && npm run e2e`. Report exact counts.

3. macOS-specific unit paths: `uv run pytest -q tests/test_synctex.py tests/test_logparse.py`
   and then one real check of /private/var handling: copy examples/sample-paper under
   `$TMPDIR` (which lives in /private/var/folders), `uv run rewrite init` it, start
   `uv run rewrite serve --repo <that path> --port 8765`, compile via `POST /api/compile`, and
   confirm `GET /api/synctex/forward?file=sections/intro.tex&line=16` returns a box and that
   `GET /api/synctex/reverse` with that box's centre returns the same file and line. Also check
   the server listens on 127.0.0.1 only (`lsof -nP -iTCP:8765 -sTCP:LISTEN`).

4. Live acceptance scripts: `frontend/e2e/live/run-all.sh ~/rewrite-live 1 2 3` first (no model
   calls), then `frontend/e2e/live/run-all.sh ~/rewrite-live 4 5 6` (real claude/codex calls,
   roughly $0.05 per script). Every script must print RESULT: PASS. If one fails, read its log
   and screenshots in ~/rewrite-live, decide whether it is a macOS defect or a script
   assumption, and fix the right side.

5. Docker compiler (if Docker Desktop is running): `PUT /api/config` with
   `{"compiler":{"kind":"docker","texlive_year":2024}}`, pull the image through
   `POST /api/compile/docker/pull` (8 GB), compile, and rerun `run-all.sh ~/rewrite-live 2`.
   Confirm the build output is owned by your user.

6. Manual UI pass in a real browser: start the backend on an initialised copy of the sample
   paper, open http://127.0.0.1:8765, and walk the manual checks listed in each
   .agents/scratch/phase-N brief that the scripts cannot cover: the PDF double-click opens the
   right line, Cmd-S saves, the Spec outliner drag and keyboard, the git panel against a real
   GitHub remote of your own (push and pull), and `open` of the URL from the CLI.

7. Record results: append a "### macOS acceptance <date>" entry to each phase brief in
   .agents/scratch/ with pass/fail per check, update the Status lines and the table in
   00-overview.md to "macOS-verified" only for phases where every check passed, update AGENTS.md's
   "Current phase" paragraph accordingly, and commit with a message listing what was verified and
   what remains open. Do not mark anything verified that you did not run.
```

## Notes

- `frontend/e2e/live/run-all.sh` needs only the tools the Mac ships with plus uv, node and
  Python 3, and is bash 3.2 safe. Logs and screenshots land in the output directory given as
  its first argument.
- Each live script prints one PASS/FAIL line per manual check and exits non-zero on failure.
