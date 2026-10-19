# rewrite — Implementation Plan

Exported 2026-10-18 from the planning conversation.

## What it is and what is decided

A local, Overleaf-like editor for a LaTeX git repo, plus an LLM flaw-finder that reviews the paper against a human-owned `paper.json` semantic spec. The LLM never writes prose, never praises, never sees editing history, and never touches `paper.json`. Reconciling critique into the spec is a human action done in the app's spec editor.

Everything the app needs for a paper lives inside that paper's git repo under `.rewrite/`. Nothing is stored in user-scope or global directories.

| Area | Decision |
| --- | --- |
| Frontend | Vite + React + TypeScript, Tailwind + DaisyUI |
| Editor | CodeMirror 6, legacy `stex` mode, decorations for highlights, `dnd-kit` for the spec tree |
| PDF | `pdfjs-dist` on canvas; SyncTeX in both directions |
| Backend | Python 3.12 + FastAPI, SQLite, Jinja2 templates, `watchfiles`, pydantic |
| Packaging | Native backend bound to `127.0.0.1`, browser UI; Tauri wrap optional later |
| TeX | Native `latexmk` by default; Docker `texlive/texlive:TLxxxx-historic` as a config toggle to match Overleaf's TeX Live year |
| LLM | Shell out to `claude -p` and `codex exec`, native, user-selected model and thinking level per run |
| git | Shell out to host `git`; Overleaf and GitHub are remotes, nothing more |
| Platform | macOS primary; WSL works when the repo lives in the Linux filesystem |
| Security | No auth; loopback binding is the boundary |

## Repository layout

Two repos: the app repo (what Claude Code and Codex build) and each paper repo (what the app opens). Every per-paper component sits at the paper repo's root under `.rewrite/`; the SQLite database and build output are gitignored there.

Paper repo:

```
main.tex, sections/*.tex, figs/, refs.bib
paper.json                   semantic state, human-owned, versioned
.rewrite/
  config.toml                main file, compiler, runner, model/thinking defaults, flag mappings
  prompts/system.md          reviewer system prompt
  prompts/passes/*.md        one editing pass per file
  sandbox/                   cwd for LLM runs: CLAUDE.md and AGENTS.md written by the app from system.md
  state.db                   gitignored: runs, findings, comparisons, snapshots
  build/                     gitignored: PDF, synctex, logs
```

`rewrite init <repo>` creates `.rewrite/` from the app's default prompt set and appends `.rewrite/state.db*` and `.rewrite/build/` to `.gitignore`. Overleaf tolerates the extra files; `paper.json` and the prompts sync to Overleaf and GitHub with the paper, so collaborators get the same spec and the same passes.

LLM runs use `.rewrite/sandbox/` as cwd, so the only project-level instruction files the CLIs see are the ones the app wrote. Global instruction files (`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`) are still injected by the CLIs; phase 4 checks the installed versions for a flag that disables them and records the result in `config.toml`.

App repo:

```
backend/      fastapi app: compiler/, runner/, synctex.py, gitops.py, prompts.py, spec.py, db.py
frontend/     vite react app: editor/, pdf/, spec/, findings/
prompts/      default prompt set copied into a paper repo on init
PLAN.md       this document, exported
CLAUDE.md     points at PLAN.md, names the phase in progress, lists conventions
AGENTS.md     same content for Codex
.claude/skills/   repo-local skills for the implementers (phase briefs, testing recipe, synctex debugging)
```

Implementer instructions are repo-local too: no global skills, no global CLAUDE.md or AGENTS.md content is assumed. Each phase in the Build phases section is written to be pasted as one brief.

## paper.json data model

`paper.json` is a save format, not the thing the user edits. The spec editor edits a tree; the file is a deterministic serialization (stable ids, sorted keys, one node per line) so git diffs stay readable and the LLM still reads a fixed JSON file. The only writer is the human through the editor; the app never writes it from model output.

Every node has the same shape, at one of four levels:

```json
{ "id": "intro-3", "level": "paragraph", "says": "...",
  "children": [], "claims": ["c1"],
  "fields": { "function": "..." },
  "anchor": { "file": "sections/intro.tex", "marker": "@p intro-3" } }
```

| Level | `says` means | Level-specific `fields` | Anchor in LaTeX |
| --- | --- | --- | --- |
| message | the intended takeaway | problem, stakes, thesis | none (root) |
| claim | the claim statement | evidence, scope | none (flat bank, referenced by id) |
| section | the section's promise | figure\_story | `\section` heading text, fallback `\label` |
| paragraph | the local message | function | `% @p <id>` comment line above the paragraph |

- The tree is message → sections → paragraphs. Claims are a flat bank, not tree nodes, because one claim is argued in several places; sections and paragraphs point at claims by id.
- Sentences live only in `.tex`. Nobody maintains sentence-level JSON by hand.
- Promote or demote keeps `says` and swaps `fields`; the displaced fields move to a `notes` field so nothing is lost.
- On-disk shape: `{ "message": node, "claims": [node], "tree": node, "version": 1 }`. A pydantic model validates on load; errors show inline in the Raw JSON tab.
- The spec may be ahead of the `.tex`: a node with no anchor is `planned`, which is how outlining before writing works.
- Prompt templates receive the same JSON, plus a Jinja helper that renders the tree as an indented outline, which models follow better than nested JSON.

## Spec editor

The spec is edited as an outliner with drag-and-drop, not as Scratch-style blocks: the hierarchy encodes containment, which is what an outliner (Workflowy, Notion toggles) does, and the keys are already familiar. It is the third top-level view beside Editor and PDF.

Layout:

- Left column, claim bank: one card per claim with `says`, evidence, scope and a colored chip. Dragging a chip onto a section or paragraph links it.
- Center, the tree: the message card pinned at the top, sections as collapsible rows, paragraphs nested inside. Each row is one editable line (`says`) with an expander for `fields`. Rows are color-coded by level to match the figure's bands.
- Right rail, coverage: a claims × sections matrix showing where each claim is argued. Empty rows are unreferenced claims; empty columns are sections that argue nothing.
- Raw JSON tab: the serialized file with validation errors inline, for when the outliner gets in the way.

Interactions, keyboard-first with `dnd-kit` for mouse:

- `Enter` adds a sibling, `Tab` / `Shift-Tab` demotes / promotes (paragraph ↔ section), drag reorders or reparents.
- "Lift to claim" copies a node's `says` into the claim bank and links the node to the new claim.
- Undo and redo on the tree; every change writes `paper.json` immediately.

Mapping to LaTeX runs in both directions, and the spec is allowed to be ahead of the source:

- From the tex up: the backend parses `\section` and `\subsection` headings and paragraph breaks and shows an unadopted skeleton in the Spec view. One click adopts a heading as a section node with an empty `says`. Adopting a paragraph inserts `% @p <id>` above it in the source.
- From the spec down: dragging a spec node onto the editor inserts the marker, optionally with `says` as a comment. Selecting text in CodeMirror opens a context menu: set as this paragraph's message, lift to claim, set as thesis. That is how sentences move between levels: across the two panes rather than inside one.
- Never anchor by position; it breaks on every reorder.

Each node shows a status dot:

| Status | Meaning | Cleared when |
| --- | --- | --- |
| planned | no anchor yet | the node is adopted or a marker is inserted |
| linked | anchor resolves | — |
| stale | the anchored paragraph changed since `says` was last edited | the user edits either side |
| orphaned | marker no longer found in the source | re-linked or deleted |

The stale state is the human half of the figure's "reconcile affected ancestors" arrow: the app says something moved, the user decides whether the spec or the prose is right.

## Backend architecture and interfaces

The backend is a thin FastAPI process that owns the repo on disk and shells out for everything heavy. Two interfaces carry the parts that vary by machine or by choice, and both have two implementations from the start:

Runtime architecture (text form): Browser UI (React, localhost only; Editor · PDF · Spec · Findings) ⇄ HTTP + WebSocket ⇄ FastAPI backend (Compiler · Runner · SyncTeX · git ops · spec validator · SQLite state.db). The backend spawns three subprocess kinds: `latexmk` (native or Docker, synctex on, output to build/), the LLM CLIs (`claude -p`, `codex exec`; cwd `.rewrite/sandbox/`, tools disabled) and `git` (status, diff, commit, push, pull with host credentials). The backend reads and writes the paper repo on disk (`*.tex`, `paper.json`, `.rewrite/`); git talks to Overleaf and GitHub as plain remotes.

The browser talks only to the backend; the backend reads and writes the paper repo and spawns `latexmk`, the LLM CLIs and `git` as subprocesses, with the LLM CLIs confined to the sandbox directory.

```python
class Compiler(Protocol):      # NativeLatexmk, DockerLatexmk
    def compile(repo: Path, main: str) -> CompileResult   # pdf, synctex, parsed errors

class Runner(Protocol):        # ClaudeRunner, CodexRunner
    def run(prompt: str, schema: dict, model: str, thinking: str) -> RunResult
    # findings, usage, raw output, event stream
```

Compiler details:

- Native: `latexmk -pdf -synctex=1 -interaction=nonstopmode -output-directory=.rewrite/build`.
- Docker: same command inside `texlive/texlive:TLxxxx-historic`, repo mounted at its host path (`-v "$REPO:$REPO" -w "$REPO"`) so SyncTeX and log paths need no translation. Year set in `config.toml`.
- Log parser returns `(file, line, message, severity)`; errors and warnings become a clickable list.

Runner details:

- Prompt goes in via stdin, never argv.
- cwd is `.rewrite/sandbox/`, tools disabled, structured-output flag when the installed CLI supports it, fenced-JSON parse as fallback.
- `claude -p --model <m> --output-format stream-json` and `codex exec -m <m> -c model_reasoning_effort=<level> --sandbox read-only --json`. Exact flags live in `config.toml` under `[runner.claude]` and `[runner.codex]`; phase 4 verifies them against `claude --help` and `codex exec --help` on the target machine.
- Thinking level is exposed as low / medium / high in the UI and mapped per runner in config.
- Events are forwarded over a WebSocket for progress; cancel kills the process group. One run per scope at a time, concurrency cap of 3 overall.
- Known caveat: both CLIs inject global instruction files regardless of cwd. Record in config whether the installed version has a flag to suppress them; if not, keep the global files harmless to prose review.

## Workflows

Four workflows, in the order a writing session uses them.

**Edit, compile, sync.** The file tree comes from resolving `\input` and `\include` from the main file. Save triggers a debounced compile, then the parsed error list. Forward sync: cursor line → `synctex view` → a highlight box on the PDF. Reverse sync: double-click on the PDF → canvas pixels to PDF points → `synctex edit` → open the file at that line. Paths are normalized before matching (`/private/var` vs `/var` on macOS, container vs host under Docker).

**Review pass.** A pass is a prompt template plus a scope: whole paper, one section, one file, or the current selection. Each run assembles a fresh prompt (system.md, the pass body, `paper.json` rendered as an outline, the scoped `.tex` with line numbers), strips the `\author{}` block, spawns the runner, and stores the prompt, raw output, git SHA, content hashes and token usage. Findings have this shape:

```json
{ "file": "sections/intro.tex", "line_start": 41, "line_end": 44, "quote": "...",
  "level": "paragraph", "category": "function", "severity": "major",
  "problem": "...", "why": "...", "json_ref": "intro-3" }
```

There is no rewrite field. Findings anchor by exact-quote search with the line range as fallback, because models miscount lines; after an edit they re-anchor by quote or go stale. The sidebar lists findings as cards, a click highlights the span in the editor and the node in the Spec view, `j` and `k` step through them, and each finding is open, addressed or dismissed. A level filter lets coarse passes be worked before fine ones.

**Blind A/B compare.** Select a paragraph and a "before" (a snapshot or a commit). The app sends both versions in randomized order to a fresh call with only the relevant `paper.json` entries and asks which better serves `local_message`, without saying that either is an edit. The verdict is logged and the mapping revealed only afterwards.

**Revisions and git.** Status, diff, commit, push and pull from a panel; "flag major revision" makes a tagged snapshot for later comparison. The app shells out to `git` and never reimplements it.

## Prompt templates

Prompts are Markdown files with YAML front matter and a Jinja2 body, hot-reloaded from `.rewrite/prompts/`, so editing a file and rerunning is the whole workflow for improving a pass.

```markdown
---
name: paragraph-function
level: paragraph
scope: section          # default scope
allow_rewrites: false   # true only where the text is not the author's voice
model: null             # null → config default for this level
thinking: null
---
For each paragraph in {{ scope_name }}, check it against its entry in
{{ spec_outline }} ...
```

Template variables: `spec_outline` (the tree as an indented outline), `spec_json`, `source` (numbered `.tex`), `scope_name`, `claims`, `findings_schema`. The system prompt presents the text as a submitted draft being screened, never as the user's own paper.

Default pass set, coarse to fine:

| Pass | Level | Checks | Rewrites |
| --- | --- | --- | --- |
| message-drift | message | abstract, intro and conclusion against `message`; "this is not the real point" | no |
| claims-evidence | claim | where each claim's evidence is; overclaiming against `scope` | no |
| section-promises | section | does each section deliver its `promise`; figure story coherence | no |
| paragraph-function | paragraph | one job per paragraph, topic sentence, paragraphs that should move | no |
| clarity | sentence | characters as subjects, actions as verbs, nominalization, passive, hedges, filler words, repeated phrasing | no |
| epistemic-calibration | sentence | sentences stronger or weaker than the evidence | no |
| reviewer-2 | message | skeptical reviewer, weaknesses only | no |
| latex-hygiene | sentence | undefined refs, missing bib keys, notation inconsistency | yes |
| ab-compare | paragraph | blind comparison template | no |

Model tiering lives in `config.toml` as one table keyed by level: a strong model with high thinking for message, claim and section passes; a cheap fast model for sentence passes. Any run can override it.

## Rule enforcement

The four rules are enforced by code, not only by prompt wording, so a prompt edit cannot silently weaken them.

| Rule | Mechanism |
| --- | --- |
| No rewrites | The findings schema has no replacement field; a validator rejects findings that contain quoted replacement text unless the pass sets `allow_rewrites: true` |
| No praise | The schema admits only problems; zero findings is the only positive signal; any free-text preamble is stripped before parsing |
| Clean context | Every run is a new process with a fully assembled prompt; no session ids are reused; no editing history or prior findings enter a prompt except in the explicit triage call |
| Read-only `paper.json` | Runner cwd is `.rewrite/sandbox/` with tools disabled, so the CLI cannot reach the file; the app never writes `paper.json` from model output |

A finding that fails validation is logged with the raw output and shown as "rejected" in the run detail, so a misbehaving prompt is visible rather than quietly dropped.

## LLM interaction extras

These are the parts of the runner that make it pleasant to iterate on, grouped by the phase that builds them.

Phase 4:

- Prompts via stdin; streaming events over WebSocket; cancel by killing the process group.
- "Show prompt" previews the exact assembled prompt; "copy prompt" puts it on the clipboard for use in a web UI, with a paste-back box for JSON findings.
- Content-hash caching keyed on pass hash, scope hash, `paper.json` hash, model and thinking level; an unchanged scope returns cached findings at once.
- Every run stores the raw prompt and raw output for replay and run-to-run diff.
- Token count, cost and wall time per run in the sidebar.

Phase 5 and 6:

- Run both runners: the same pass to Claude and Codex, findings merged by anchor overlap, agreements listed first.
- Triage call: send one run's findings plus the scoped text to a fresh call asking which are wrong or trivial.
- Per-section fan-out for long papers with the full spec as global context, concurrency 3, plus one `message-drift` call over abstract, intro and conclusion.
- SDKs (Claude Agent SDK, Codex app-server) only if event parsing becomes a burden; subprocess gives one interface for both CLIs.

## Platform notes

macOS is the primary target; WSL is supported without being a design constraint.

- macOS: `brew install --cask mactex-no-gui`, or BasicTeX plus `tlmgr install latexmk`. The backend opens the URL with `open`.
- WSL: `texlive` from apt; keep the repo inside the Linux filesystem, not `/mnt/c/`, or file watching and git both slow down; `wslview` opens the URL and `localhost` forwards to Windows automatically.
- Startup detects `latexmk`, `docker`, `claude`, `codex` and `git` on `PATH` and shows which runners and compilers are available.
- Docker is only ever the compiler. LLM CLIs stay native because their login lives in the host user's keychain or config dir; git stays native because push and pull need host credentials.
- File watching uses `watchfiles` (FSEvents on Mac, inotify on Linux).

## Build phases

Each phase is one brief for Claude Code or Codex, kept under `.claude/skills/phases/` in the app repo. Finish phases 1 and 2 before any LLM work: they are the Overleaf replacement and the part most likely to need platform debugging.

1. Skeleton: FastAPI + Vite scaffold, open a repo, `rewrite init`, file tree from `\input` resolution, CodeMirror editor with save, `NativeLatexmk`, PDF render, error list, `Compiler` and `Runner` interfaces stubbed, `.rewrite/` layout and gitignore handling.
2. SyncTeX in both directions, path normalization, `DockerLatexmk` with the pinned TeX Live year.
3. Spec editor: node model and serializer, pydantic validator, outliner with keyboard and drag, claim bank, skeleton adoption and `% @p` markers, status dots, Raw JSON tab.
4. Pass runner: prompt loading with front matter, both runners with config-driven flags verified against the installed CLIs, DB schema for runs and findings, streaming and cancel, sidebar with highlights and `j`/`k`, finding states, re-anchoring, caching, cost display, show and copy prompt, rule validator.
5. Blind A/B compare, revision snapshots, git panel, coverage matrix, editor context menu (lift to claim, set message, set thesis).
6. Polish: cross-runner merge, triage call, per-section fan-out, optional Tauri wrap.

Definition of done per phase: the phase's brief lists the manual checks (for example, "double-click a word in the PDF opens the right line"); a phase ends when those pass on macOS.

## Assumed defaults

These were chosen without an explicit decision and are easy to flip later.

- The model sees `.tex` source only; `pdftotext` input is a later per-pass option.
- `latex-hygiene` keeps rewrites allowed, since LaTeX syntax is not the author's voice.
- Python backend rather than all-TypeScript: confirmed by the author, who works in Python and not TypeScript. The frontend stays TypeScript but is built entirely by Claude Code and Codex.
- No auth on the app; loopback binding is the security boundary.
- One paper repo open per backend process; multiple papers mean multiple processes on different ports.
