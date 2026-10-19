# Optional Tauri wrap

**Status: not implemented.** Phase 6 of `PLAN.md` lists the Tauri wrap as optional and this
build skips it. This page records how it would be done, so it can be picked up later without
redesigning anything.

## What it would add, and what it would not

A native window and a dock icon around the app as it exists today. Nothing else changes: the
backend stays a Python process bound to `127.0.0.1`, the browser UI stays the same React build,
and every per-paper file stays under the paper repo's `.rewrite/`.

## Shape

```
Tauri shell (Rust, a few dozen lines)
  ├─ on launch: start the sidecar  rewrite serve --port <port> [--repo <path>]
  ├─ wait until  GET http://127.0.0.1:<port>/api/health  answers
  ├─ open one window on  http://127.0.0.1:<port>/        (the backend serves frontend/dist)
  └─ on exit: kill the sidecar (its process group, so running LLM CLIs and latexmk stop too)
```

- **Load the backend's URL, not bundled assets.** The frontend uses relative `/api/...` URLs
  and opens the WebSocket on `window.location.host` (`src/api/ws.ts`), so a window pointed at
  `http://127.0.0.1:<port>/` works with **no frontend code changes**. Bundling the assets as
  `tauri://localhost` would make every request cross-origin, and the backend rightly refuses
  writes from a foreign `Origin` (403), so that route would need CORS changes.
- **Port.** Either the fixed `8765` used by the live test scripts, or a free port picked by
  the Rust side and passed as `--port`; the window URL is built from the same number.
- **Repo.** Pass `--repo` if the shell knows it (e.g. from a folder picker), or leave it out
  and let the UI's existing "Open a paper repo" dialog choose. One window = one backend = one
  paper, as today (PLAN.md: one paper repo per backend process).

## Development and release

- **Dev:** run the backend as usual (`cd backend && uv run rewrite serve --port 8765`, after
  `npm run build` in `frontend/`) and set Tauri's `build.devUrl` to `http://127.0.0.1:8765`.
  `cargo tauri dev` then only opens the window; no sidecar is needed.
- **Release:** build a one-file backend binary with PyInstaller, e.g.
  `uv run --with pyinstaller pyinstaller --onefile --name rewrite --add-data ../frontend/dist:frontend/dist app/cli.py`,
  and register it as a sidecar (`bundle.externalBin: ["binaries/rewrite"]`; Tauri expects the
  file named with the target triple, e.g. `rewrite-aarch64-apple-darwin`). One backend change
  would be needed here: `FRONTEND_DIST` in `backend/app/main.py` is resolved from the source tree,
  so a frozen build must point it at the bundled copy (`sys._MEIPASS`) or read it from an
  environment variable. `latexmk`, `docker`, `git`, `claude` and `codex` stay on the user's `PATH`
  as today; they are not bundled.
- **macOS:** the sidecar must be code-signed and notarized with the app. A GUI app does not
  inherit the shell's `PATH`, so the shell should pass a `PATH` that includes the MacTeX and
  Homebrew directories (`/Library/TeX/texbin`, `/opt/homebrew/bin`) and wherever `claude` and
  `codex` are installed. WSL users keep using the browser.

## Where the configuration would live

In the **app repo**, in a new `desktop/` folder (the usual `src-tauri/` layout):
`desktop/src-tauri/tauri.conf.json` (window URL, sidecar, bundle id), `Cargo.toml`, and
`src/main.rs` (start / health-wait / kill). Nothing goes into the paper repo, and nothing new
into `.rewrite/config.toml`.

CLAUDE.md forbids writing to global paths. Tauri itself would not need to: use no plugin that
persists state (window-state, store), and open the window with `"incognito": true` so the
webview keeps no profile on disk. The UI's few `localStorage` conveniences (last repo path,
findings column open) are then forgotten between launches, which is acceptable.
