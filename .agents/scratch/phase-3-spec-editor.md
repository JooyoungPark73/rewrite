# Phase 3 — Spec editor

Status: implemented, Linux-verified 2026-10-18; macOS acceptance pending

## Scope (PLAN.md "Build phases" 3)

Node model and serializer, pydantic validator, outliner with keyboard and drag,
claim bank, skeleton adoption and `% @p` markers, status dots, Raw JSON tab.

## Data model (PLAN.md "paper.json data model")

```
Node { id, level: message|claim|section|paragraph, says: str, children: [Node],
       claims: [str], fields: dict[str,str], anchor: {file, marker}|null, notes?: str }
PaperSpec { version: 1, message: Node, claims: [Node], tree: Node }
```

- `tree` is a node of level `message`? No: `tree` root is the message node's structural twin —
  decide: `tree` is a node with `level: "message"` and `id` equal to `message.id`, whose
  `children` are sections. Keep `message` as the semantic card (says/fields) and `tree` as
  structure. Serializer writes sorted keys, 2-space indent, one node per line in
  `children` arrays (custom encoder), trailing newline. Stable ids: `<slug>-<n>` from
  the section heading / `p-<n>` for paragraphs; never reuse an id in the file.
- `app/spec.py`: load/validate/serialize, `write_spec(repo, spec)` — THE ONLY writer of
  paper.json. Every mutation goes through `POST /api/spec` (full document, validated).
  Undo/redo lives in the frontend store.
- Status computation (`GET /api/spec/status`): for each anchored node, `linked` if marker
  found, `orphaned` if not; `stale` if the paragraph text hash (stored in
  `state.db` table `anchor_hash(node_id, text_hash, says_hash)`) differs from the last
  time `says` was edited. `planned` when anchor is null.
- Skeleton: `GET /api/spec/skeleton` parses `\section`, `\subsection` and blank-line
  paragraph breaks from every file in the tree; returns headings and paragraphs with
  file/line and whether they are already adopted.
- Adoption: `POST /api/spec/adopt {kind: section|paragraph, file, line, ...}` → for a
  paragraph inserts `% @p <id>` above it (this writes the .tex, not paper.json) and
  returns the new node for the frontend to merge into the spec; the frontend then saves
  the spec. Section anchors are `{file, marker: "\\section{<heading>}"}`, fallback
  `\label{...}`.
- Promote/demote swaps `fields` keys per the level table; displaced fields go into `notes`.

## Frontend

- `spec/SpecView.tsx`: three columns — ClaimBank (left), Outliner (center), Coverage (right placeholder until phase 5) + Raw JSON tab.
- Outliner rows: editable `says` line, expander for `fields`, status dot, level color band.
  Keyboard: Enter sibling, Tab/Shift-Tab demote/promote, Alt-Up/Down move, Backspace on empty deletes. `dnd-kit` sortable tree for mouse.
- Claim chip drag onto a row links the claim.
- "Lift to claim" button on a row.
- Skeleton panel: unadopted headings/paragraphs with an "adopt" button.
- Editor integration: drag a spec node onto the editor inserts `% @p <id>`; selection context menu (set as paragraph message / lift to claim / set as thesis) is phase 5 per PLAN, but the marker insertion is here.
- Raw JSON tab with validation errors inline (CodeMirror json mode), save writes via the same endpoint.
- Undo/redo (Ctrl-Z / Ctrl-Shift-Z) on the tree; every committed change POSTs the spec.

## Tests

- serializer determinism (two serializations equal, keys sorted, one node per line),
  validator rejects bad levels / duplicate ids / dangling claim refs,
  promote/demote preserves data, skeleton parser on the fixture, adoption inserts the
  marker exactly once, status transitions (planned → linked → stale → orphaned),
  invariant test extended: only `app/spec.py` contains the paper.json write.

## Manual checks

1. Adopt `\section{Introduction}` from the skeleton panel; it appears as a section row with an empty says.
2. Adopt a paragraph; `% @p <id>` appears in the editor above it and the row shows "linked".
3. Edit that paragraph's text in the editor and save; the row turns "stale". Edit `says`; it returns to "linked".
4. Delete the marker line in the editor; the row turns "orphaned".
5. Tab on a paragraph row makes it a section (fields swap, notes keep the old fields); Shift-Tab reverses.
6. Drag a claim chip onto a section; the section's claims list updates and `paper.json` diff shows one line changed.
7. Ctrl-Z undoes the last tree change and the file on disk follows.
8. `git diff paper.json` after an edit is one or two lines.

## Decisions fixed for implementation

- **Tree root.** `tree` is a node with `level: "message"` and `id == message.id`, `says: ""`,
  `fields: {}`, `claims: []`, `anchor: null`, `children: [sections]`. The semantic message card is
  `message` (its `children` is always `[]`); `tree` carries structure only. The validator enforces this.
- **Node shape (all levels):** `{ id, level, says, children, claims, fields, anchor, notes }` —
  `anchor` is `null` or `{file, marker}`; `notes` is a string, `""` when empty (always present so the
  serializer is stable). Keys written sorted; 2-space indent; `children`/`claims` arrays with one
  element per line; trailing newline. Ids: `message`, `c<n>` for claims, `<slug>-<n>` for sections
  (slug from the heading, ≤24 chars), `p-<n>` for paragraphs; `<n>` increments past the highest
  existing numeric suffix of that prefix anywhere in the spec (never reused).
- **Level fields:** message `problem, stakes, thesis`; claim `evidence, scope`; section `figure_story`;
  paragraph `function`. Changing level moves the old level's non-empty fields into `notes` as
  `"<key>: <value>"` lines (appended), and starts the new level's fields empty.
- **Stale.** `state.db` (SQLite, `app/db.py`, created on first use) table
  `anchor_baseline(node_id TEXT PRIMARY KEY, text_hash TEXT, updated_at REAL)`. On every spec write,
  any anchored paragraph node whose `says` differs from the previous on-disk spec gets its baseline
  set to the current paragraph text hash. `GET /api/spec/status` computes: planned (no anchor),
  orphaned (marker not found), stale (text hash ≠ baseline; a node with no baseline gets one on
  first sight and is `linked`), else linked. `POST /api/spec/nodes/{id}/reconcile` re-baselines
  without editing (the "I checked, the prose is right" button). Sections: anchor by
  `\section{<heading>}` text (exact, whitespace-collapsed) with fallback to `\label{...}` inside
  the marker string (`marker` may be either `\section{...}` or `\label{...}`); sections are never
  stale, only planned/linked/orphaned.
- **Paragraph text** for a marker = lines after `% @p <id>` up to the next blank line, next
  `% @p`, or next `\section`/`\subsection`/`\end{document}`, with comments stripped and whitespace
  collapsed before hashing.
- **Logic lives in Python.** All structural edits are server ops so the data logic is testable in
  pytest and the frontend stays thin; the frontend keeps an undo/redo stack of full specs and undoes
  by `PUT /api/spec`.

## API contract (fixed; backend and frontend built in parallel)

Common types: `Node` as above; `PaperSpec = { version: 1, message: Node, claims: Node[], tree: Node }`;
`SpecError = { path: str, message: str }`; `SpecResponse = { spec: PaperSpec, raw: str }`.

- `GET /api/spec` → `{ spec: PaperSpec|null, raw: str, errors: SpecError[] }` (spec null when the
  file is missing/invalid; raw is the file text or "").
- `PUT /api/spec { spec }` → `SpecResponse`; 422 `{ detail: str, errors: SpecError[] }` on invalid.
- `PUT /api/spec/raw { raw }` → `SpecResponse` (parse, validate, re-serialize, write); 422 as above.
- `POST /api/spec/ops { op }` → `SpecResponse` after applying one op to the on-disk spec and writing it.
  Ops (discriminated by `op.type`):
  - `update { id, says?, fields?, notes? }` (fields merged key-wise; a key set to "" is kept as "")
  - `insert { parent_id, index, level: section|paragraph, says? }` → new node; response also
    carries `new_id` (SpecResponse + `new_id`)
  - `delete { id }` (claims referenced by nothing stay in the bank)
  - `move { id, parent_id, index }` (reparent/reorder; refuses to move a node into its own subtree)
  - `set_level { id, level: section|paragraph }` (promote/demote; a section demoted to paragraph
    keeps its children only if it has none — otherwise 400; a paragraph promoted becomes a sibling
    section after its parent section)
  - `lift_to_claim { id }` → adds claim `{says: node.says, fields: {evidence:"", scope:""}}`, links it
    on the node, response carries `new_id`
  - `link_claim { id, claim_id }`, `unlink_claim { id, claim_id }`
  - `add_claim { says, fields? }` (+`new_id`), `update_claim { id, says?, fields?, notes? }`,
    `delete_claim { id }` (also removes references)
  - `set_anchor { id, anchor: {file, marker}|null }`
  Errors: 404 unknown id, 400 invalid op, 422 if the result fails validation.
- `GET /api/spec/status` → `{ nodes: { [id]: { status: "planned"|"linked"|"stale"|"orphaned",
  file: str|null, line: int|null } } }` for every section and paragraph node.
- `POST /api/spec/nodes/{id}/reconcile` → `{ id, status }`.
- `GET /api/spec/skeleton` → `{ sections: [{ file, line, kind: "section"|"subsection", heading, label: str|null,
  adopted_id: str|null }], paragraphs: [{ file, line_start, line_end, preview (first 80 chars),
  section_heading: str|null, marker_id: str|null }] }` over every file in the include tree, in
  document order. A paragraph is a run of non-blank lines outside the preamble that is not a
  heading, not a pure-comment run, not inside `figure`/`table`/`equation`/`align`/`verbatim`
  environments, and not a `\begin`/`\end` line only.
- `POST /api/spec/adopt { kind: "section", file, line }` → `{ node, new_id }` (section node with
  `says: ""`, anchor on the heading; NOT yet in the spec — the frontend inserts it with an `insert`?
  No: to keep one write path the backend appends it under `tree` at the position matching document
  order and writes the spec; response is `SpecResponse + new_id`).
- `POST /api/spec/adopt { kind: "paragraph", file, line_start, parent_id?, id?, says? }` → inserts
  `% @p <id>` above `line_start` in the .tex (atomic write, emits `file.changed`), adds a paragraph
  node under `parent_id` (default: the section node whose anchor precedes it in the same file, else
  the last section, else 400) at the index matching document order, writes the spec; response
  `SpecResponse + new_id + { file, line }`. If `id` is given and already exists in the spec, it only
  (re)inserts the marker and sets the node's anchor (used by "drag spec node onto editor" and
  re-linking orphans). Marker insertion is idempotent: if the same marker already sits directly
  above `line_start`, nothing is written.
- WS events: `spec.changed { }` after any write to paper.json; `file.changed` as before.

## Status notes

### 2026-10-18 — backend half done

- New: `app/spec.py` (models, validator, serializer, `write_spec`, ops, ids), `app/skeleton.py`
  (pure LaTeX scan: headings, paragraphs, markers, marker text hash, `place_marker`),
  `app/anchors.py` (status, reconcile, adoption planning, document-order index),
  `app/db.py` (state.db, WAL, append-only `MIGRATIONS` keyed on `PRAGMA user_version`; phase 4
  appends `runs`/`findings` as entry 2+), `app/api/spec.py`. Small additions: `files.include_targets`,
  `AppState.spec_lock`, event `SpecChanged`. `rewrite init` now writes the empty spec with
  `spec.serialize` (canonical layout, `notes: ""` on every node), so the first edit is a 1-line diff.
- Tests: 325 pass (+3 docker-gated skips), ruff check/format clean. New files: test_spec_model.py,
  test_spec_ops.py, test_skeleton.py, test_spec_api.py, test_db.py; test_invariants.py and
  test_init.py extended. The invariant test now requires that the only functions that both name
  paper.json and call a write are `spec.write_spec` and `repo.init_repo`, that `write_spec` is
  referenced only by `app/api/spec.py`, and that `app/runner/` never references `write_spec`,
  `apply_op` or imports `app.api.spec`.
- Live smoke (`rewrite serve` on a git copy of the sample paper, curl only, 127.0.0.1:8766 per
  `ss`): adopted `\section{Introduction}` (→ `introduction-1`) and intro.tex paragraph 11 (→ `p-1`,
  `% @p p-1` landed on line 11); GET raw → PUT raw, PUT spec, PUT 4-space-indented raw all left
  the same sha256; after a commit, one `update` op gave `git diff --numstat` = `1 1 paper.json`;
  editing the prose → stale, reconcile → linked; foreign Origin → 403. WS showed `file.changed`,
  `spec.changed`, then the watcher's duplicate `file.changed` (same mtime).

Endpoint shapes (contract shapes exactly; optional extras marked +):
- `GET /api/spec` → `{spec|null, raw, errors}`; never 500 (missing, bad JSON, bad UTF-8, a
  directory: all `spec: null` + errors). + `SpecError.line/column` (set for JSON syntax errors).
- `PUT /api/spec {spec}`, `PUT /api/spec/raw {raw}` → `{spec, raw}`; 422 `{detail: str, errors}`.
  An unchanged spec is not rewritten and emits no event.
- `POST /api/spec/ops {op}` → `{spec, raw, new_id}` (+ `new_id: null` for ops that make no id).
  Malformed op / unknown `type` / unknown key → 400 with a string `detail` (not FastAPI's 422);
  404 unknown id; 400 invalid op; 422 `{detail, errors}` when the result fails validation;
  + 409 `{detail, errors}` when paper.json on disk is missing/invalid (also for status/adopt/reconcile).
- `GET /api/spec/status` → `{nodes: {id: {status, file, line}}}`; `line` is the heading line or the
  paragraph's marker line; orphaned → `file` = anchor file, `line` null.
- `POST /api/spec/nodes/{id}/reconcile` → `{id, status}` + `file`, `line`; 404 unknown, 400 for
  message/claim ids.
- `GET /api/spec/skeleton` → as contracted (works with a broken spec; `adopted_id` then null).
- `POST /api/spec/adopt` section → `{spec, raw, new_id}` + `file`, `line` (heading), + optional
  `says`; 409 already adopted, 400 not a heading / file not in the include tree.
  Paragraph → `{spec, raw, new_id, file, line}`; `line` = the marker line after the write.
- POSTs (`ops`, `adopt`, `reconcile`) refuse a foreign `Origin` with 403 (a body-less or
  Content-Type-less POST is a CORS "simple" request).

Judgment calls (flag if wrong):
- Tree is flat: root → sections → paragraphs. A `\subsection` adopts as a top-level section node
  with marker `\subsection{...}`; the validator rejects sections inside sections.
- Validator extras: paragraph marker must be exactly `@p <node id>`; `fields` keys must belong to
  the node's level (missing keys allowed, unknown keys rejected); claims may not link claims;
  a node may not link the same claim twice; JSON duplicate keys rejected; unknown keys rejected.
- `set_level` drops the anchor (a heading cannot anchor a paragraph nor a marker a section), so the
  node becomes planned; the old marker stays in the .tex until re-linked. Demoting a section
  appends it to the previous section; demoting the first section is 400. Field values are not
  restored from `notes` when switching back (per "starts the new level's fields empty").
- Ids are never reused: besides "past the highest suffix in the spec", the generator also skips
  marker ids found in the source and every id recorded in state.db (`spec_ids` table, written on
  each spec write, plus `anchor_baseline` rows). Inserted sections get `slug(says)-n` or
  `section-n`; slugs strip LaTeX commands and accents.
- Paragraph adoption: `line_start` may be any line of the paragraph; default parent adds one step
  to the brief's rule (same-file preceding section → else the adopted section that precedes the
  paragraph in document order → else the last section → else 400), so the abstract lands in the
  last section unless `parent_id` is given. A paragraph that already carries another node's marker
  → 409; one carrying an unowned marker (deleted node, hand-typed) is adopted under that id with no
  .tex write. Re-linking an id whose marker sits elsewhere moves it (other `% @p <id>` lines are
  removed, possibly in other files) so each marker id appears once. Adoption always re-baselines
  that paragraph, even when neither file changes. The .tex is checked against the scan before
  writing (409 if it changed in between); CRLF and indentation are preserved.
- Skeleton: lines that are only commands with arguments (`\maketitle`, `\label{..}`,
  `\bibliography{..}`, `\centering`) are not paragraph text; marker lines and `\input` lines split
  runs; excluded environments also include starred forms and gather/multline/eqnarray/
  lstlisting/minted/algorithm/wrapfigure/comment. Section label fallback: adoption stores
  `\label{...}` instead of `\section{...}` only when the heading text is not unique and has a label.

Not verified / open:
- All 8 manual checks need the frontend; only their backend halves are covered (pytest + curl).
- External changes to paper.json (git pull, another editor) emit no `spec.changed`: the watcher
  only watches .tex/.bib. Ops always re-read the file, so nothing is lost, but an open UI shows
  the old tree until it refetches.
- macOS not run (no path-specific code was added in this phase).

### 2026-10-18 — phase 3 complete (integration)

- Backend 325 passed (+3 docker-gated); frontend 26 e2e against the mock; live
  `frontend/e2e/live/p3-e2e.mjs` 24/24 on a fresh init of the sample paper.
- Manual checks 1–8 verified headlessly on Linux: adopt section/paragraph (marker on intro.tex:4),
  prose edit → stale, `says` edit → linked, reconcile → linked, marker deletion → orphaned,
  Tab/Shift-Tab with notes preservation, claim chip drag → 1-line paper.json diff, Ctrl-Z/Ctrl-Shift-Z.
- Not verified: macOS.
- Judgment calls to revisit later: `set_level` drops the anchor (a promoted/demoted node is `planned`
  until re-linked via ⋯ → Insert marker); "says as a comment" is a second write done by the frontend;
  the watcher does not watch `paper.json` itself (external edits need a refetch; phase 5 adds this
  with git pull).

### macOS acceptance 2026-10-19

| Check | Result | How |
| --- | --- | --- |
| 1 adopt `\section{Introduction}` → section row, empty says | pass | p3 live (`introduction-1`, linked) |
| 2 adopt a paragraph → `% @p` marker, row "linked" | pass | p3 (`p-1`, marker on intro.tex:4) |
| 3 prose edit → stale; says edit → linked | pass | p3 (stale, then linked via Reconcile) plus an API probe on the real backend: stale after a prose edit, linked again after an `update {says}` op |
| 4 delete the marker line → orphaned | pass | API probe: `PUT /api/file` without the `% @p p-9` line → `GET /api/spec/status` orphaned, line null |
| 5 Tab / Shift-Tab swap level, notes keep the old fields | pass | p3 |
| 6 drag a claim chip onto a section → claims updated, 1-line diff | pass | headless drag on the real backend: `design-1.claims = ["c1"]`, `git diff --numstat paper.json` = `1 1` |
| 7 Ctrl/Cmd-Z undoes and the file follows | pass | p3 (`ControlOrMeta+z`, `ControlOrMeta+Shift+z` rewrite paper.json) |
| 8 `git diff paper.json` after an edit is one or two lines | pass | `1 1 paper.json` after one says edit |

- p3 live: 24/24 after the chord fix (`Control+s/z` → `ControlOrMeta`).
- Outliner in a headed Chromium window (real mouse and keys): ArrowDown moves focus p-1 → p-2;
  Alt+ArrowDown / Alt+ArrowUp reorder and paper.json follows; dragging a paragraph's handle onto
  another paragraph row reorders; dragging it onto a section header appends to that section (the
  header lights up). The header drop failed twice before I brought the window to the front and
  moved the pointer in more steps; with those it passes, so the failures were the harness
  (unfocused headed window), not the app. Not verified: a human hand on a trackpad.
