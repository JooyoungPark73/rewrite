# Phase 2 — SyncTeX both directions, path normalization, DockerLatexmk

Status: implemented, Linux-verified 2026-10-18; macOS acceptance pending

## Scope (PLAN.md "Build phases" 2)

SyncTeX in both directions, path normalization, `DockerLatexmk` with the pinned
TeX Live year.

## Backend

- `app/synctex.py`:
  - `forward(repo, main, file, line, col=0) -> [SyncBox{page, x, y, w, h}]` via `synctex view -i line:col:file -o build/main.pdf`.
  - `reverse(repo, main, page, x, y) -> SyncEdit{file, line, col}` via `synctex edit -o page:x:y:build/main.pdf`.
  - Parse the `Output:` / `Page:` / `x:` `y:` `h:` `v:` `W:` `H:` record blocks. Units: synctex reports in PDF points (bp) from top-left of page for `view`; `edit` takes PDF points.
  - `normalize_path(p, repo)`: resolve symlinks, strip `/private` prefix on macOS, map container path to host path under Docker (same path by design since the repo is mounted at its host path), return repo-relative posix path.
  - Runs synctex inside Docker when the compiler kind is docker and `synctex` is not on the host PATH.
- `app/compiler/docker.py`: `DockerLatexmk` runs
  `docker run --rm -v "$REPO:$REPO" -w "$REPO" -u $(id -u):$(id -g) texlive/texlive:TL{year}-historic latexmk -pdf -synctex=1 -interaction=nonstopmode -output-directory=.rewrite/build main.tex`.
  Image pull is explicit (`POST /api/compile/docker/pull` streams progress over WS) and `health` reports whether the image is present.
- API: `GET /api/synctex/forward?file=&line=&col=`, `GET /api/synctex/reverse?page=&x=&y=`, `GET/PUT /api/config` (compiler kind + year editable from UI).
- Tests: synctex parsing from fixture output; forward/reverse roundtrip on the compiled fixture (skipped if no synctex); docker compile test skipped unless `docker` present and `REWRITE_TEST_DOCKER=1`.

## Frontend

- Editor: cursor line change (debounced 300 ms) → forward sync → highlight box overlay on the PDF page canvas and scroll into view.
- PDF: double-click → canvas pixel → PDF point conversion (account for scale and device pixel ratio; y flips because pdf.js canvas origin is top-left while synctex `edit` expects top-left too — verify empirically and document) → reverse sync → open file, set cursor, flash the line.
- Settings dialog: compiler kind native/docker, TeX Live year, pull image button with progress.

## Manual checks

1. Place the cursor in a sentence in `sections/intro.tex`; the PDF scrolls to and highlights that line's box.
2. Double-click a word in the PDF; the editor opens the right file at the right line (±1).
3. Switch compiler to docker with year 2024, pull image, compile; PDF and both sync directions still work.
4. Paths that contain symlinks or `/private/var` normalize to the same repo-relative path (unit test).

## API contract (fixed; backend and frontend are built in parallel against this)

- `GET /api/synctex/forward?file=<repo-relative>&line=<int>&col=<int, default 0>` →
  `{ boxes: [{ page: int (1-based), x: float, y: float, w: float, h: float }] }`
  Coordinates in PDF points with the origin at the page's TOP-LEFT (pdf.js canvas convention;
  the backend converts from whatever `synctex view` reports). Empty `boxes` when no match.
  Errors: 409 no repo, 404 no synctex file yet (compile first), 400 bad args.
- `GET /api/synctex/reverse?page=<int, 1-based>&x=<float>&y=<float>` (x,y PDF points, origin top-left) →
  `{ file: str (repo-relative), line: int, col: int }`; 404 when synctex has no record.
- `GET /api/config` → `{ main: str, compiler: { kind: "native"|"docker", texlive_year: int, image: str },
  docker: { available: bool, image_present: bool } }`
- `PUT /api/config { compiler: { kind, texlive_year } }` → same as GET; writes `.rewrite/config.toml`
  (preserve the other sections; comments may be dropped as noted in phase 1).
- `POST /api/compile/docker/pull` → `{ started: true }`; progress as WS events
  `{type: "docker.pull", line: str}` and `{type: "docker.pull.finished", ok: bool, error: str|null}`.
- `CompileResult` unchanged; `synctex` path now always set when the compile succeeds.
- WS: `{type: "compile.finished", result}` unchanged.

## Status notes

### 2026-10-18 — backend half done

- New: `app/synctex.py`, `app/api/synctex.py`, `app/api/config.py`; `app/compiler/docker.py`
  implemented; `POST /api/compile/docker/pull` in `app/api/compile.py`; pull orchestration
  in `app/state.py`. Tests: 199 pass with `REWRITE_TEST_DOCKER=1` (196 + 3 skipped
  without it); ruff check and format clean.
- SyncTeX convention, measured on the sample paper against `pdftotext -bbox` (top-left
  origin): `synctex view` reports bp from the page's TOP-LEFT, `v` = the box's BOTTOM edge,
  `H` extends upwards. Title (main.tex:11) v=130.04 H=15.11 vs pdftotext 114.94–130.05;
  intro.tex:22 v=562.23 H=9.64 vs 552.59–562.22. API boxes are `x=h, y=v-H, w=W, h=H`.
  `synctex edit` takes the same top-left point; box centres map back to the same line.
  Details in the `app/synctex.py` docstring.
- pdfTeX records inputs as `<repo>/./sections/intro.tex`. `forward` passes that exact name,
  read from the .synctex.gz (cached by mtime), and falls back to the repo-relative and
  absolute forms. A file that is not an input returns `{boxes: []}` without running synctex.
- Docker command, verified here: `docker run --rm --pull=never --name rewrite-<hex>
  -v R:R -w R -u uid:gid -e HOME=/tmp -e max_print_line=10000 -e error_line=254
  -e half_error_line=238 texlive/texlive:TL<year>-historic latexmk ...`. `--pull=never`
  keeps pulls explicit; a missing image yields "Docker image ... is not present; pull it
  first". `--name` lets a timeout `docker kill` the container (verified for real with a
  1 s timeout; no leftover containers).
- Verified live against `rewrite serve` (127.0.0.1 only): GET/PUT config, a Docker
  compile with TL2024 (build files owned by the user's uid), forward and reverse sync with
  synctex both on the host and inside the image, and a real `docker pull` streamed over
  WS (4 `docker.pull` events then `docker.pull.finished`).
- Additions beyond the contract (all optional fields): `docker.pull*` events carry `image`;
  the pull response carries `image` and `already_running` (a second POST for the same image
  joins the running pull, and pulls of different images queue). `/api/health` has
  `docker_image_present`. `[compiler] container_root` (optional, unset by default) mounts
  the repo elsewhere and maps paths back. Errors: 503 when synctex is not available (or
  docker is not on PATH, for pull), 504 on synctex timeout, 403 for a cross-origin pull POST.
  Synctex query arguments are parsed by hand so bad ones are 400 with a string `detail`;
  PUT /api/config range and kind errors are also 400. A wrongly typed JSON body is still
  FastAPI's 422.
- Not verified: anything on macOS (`/private/var` and symlink handling are unit-tested
  only); pulling an image that is not already present (only an up-to-date pull ran);
  manual checks 1–3 in the browser (that needs the frontend half).

### 2026-10-18 — phase 2 complete (integration)

- Backend 196 passed + 3 docker-gated (all pass with `REWRITE_TEST_DOCKER=1`); frontend 13 e2e against the mock.
- Live: `frontend/e2e/live/p2-e2e.mjs` against `rewrite serve` on a fresh init of the sample paper:
  cursor on intro.tex:16 → one box on page 1 (y=498 pt, top-left origin) scrolled into view; double-click
  at the box centre → reverse returns intro.tex:16 and the editor lands there. 9/9 checks, no JS errors.
- Switched compiler to docker/2024 via `PUT /api/config`, compiled in `texlive/texlive:TL2024-historic`
  (log says TeX Live 2024, build owned by the user), and the same 9 sync checks pass against that build.
- Manual checks 1–3 verified headlessly on Linux; check 4 by unit tests. Not verified: macOS.
- Judgment calls: `--pull=never` on compile (pull is explicit), named containers killed on timeout,
  boxes stay until the next sync, no auto-sync while the file is dirty.

### macOS acceptance 2026-10-19

| Check | Result | How |
| --- | --- | --- |
| 1 cursor line → PDF box | pass | p2 live: intro.tex:16 → one box on page 1 (y = 498 pt), scrolled into view |
| 2 double-click in the PDF → right file and line | pass | p2 (reverse at the box centre → intro.tex:16) and a headed-Chromium double-click (intro.tex:14 → landed on 13, within ±1) |
| 3 docker / TL2024: pull, compile, both sync directions | pass | `PUT /api/config {docker, 2024}`; `POST /api/compile/docker/pull` pulled `texlive/texlive:TL2024-historic` (not present before); compile on an emptied build dir: log says TeX Live 2024, 2.2 s; every build file owned by uid 501 / gid 20 (mine); forward and reverse give the same box / line as the native build; p2 live 9/9 against the Docker build |
| 4 `/private/var` + symlink normalisation | pass | unit tests (`test_synctex.py` + `test_logparse.py`: 68 passed) and a real run: sample paper copied under `$TMPDIR` (`/var/folders/...`), served with the unresolved path; `.synctex.gz` records `/private/var/...`; forward returns a box and reverse at its centre returns `sections/intro.tex:16` |

- Docker-gated backend tests: `REWRITE_TEST_DOCKER=1 uv run pytest tests/test_docker.py` → 19 passed.
- Note for the next person: `run-all.sh` always inits a fresh repo (native compiler), so the Docker
  rerun of p2 was done by hand: server on 8765 for the TMPDIR repo with the docker config, then
  `node e2e/live/p2-e2e.mjs <repo> ~/rewrite-live`. p2/p3/p4 hard-code port 8765 (p1/p5/p6 honour
  `REWRITE_URL`); left as is.
