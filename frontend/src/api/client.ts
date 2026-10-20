// Typed wrappers for every backend HTTP endpoint.
//
// The shapes below mirror the API contract in .agents/scratch/00-overview.md
// (phase 1) and the "API contract" sections of phase-2-synctex-docker.md,
// phase-3-spec-editor.md (the paper.json spec editor),
// phase-4-pass-runner.md (review passes, runs and findings),
// phase-5-ab-git-coverage.md (git panel, snapshots, blind A/B compare,
// coverage matrix) and phase-6-polish.md (run groups: "Run both" and
// per-section fan-out, the cross-runner merge, and the triage call).
// Every function returns parsed JSON typed with these interfaces, or throws an
// ApiError whose `detail` is the backend's `{ "detail": "..." }` message, so UI
// code can show it directly.
//
// All URLs are relative ("/api/..."): in development Vite proxies them to the
// backend on 127.0.0.1:8000, and in production the backend serves this app
// from the same origin.

// ---------------------------------------------------------------------------
// Response shapes
// ---------------------------------------------------------------------------

/** Which external tools the backend found on PATH. */
export interface ToolAvailability {
  latexmk: boolean
  docker: boolean
  claude: boolean
  codex: boolean
  git: boolean
}

/** GET /api/health */
export interface Health {
  ok: boolean
  version: string
  tools: ToolAvailability
  /** Absolute path of the open paper repo, or null if none is open. */
  repo: string | null
}

/** GET /api/repo, POST /api/repo/open, POST /api/repo/init */
export interface RepoInfo {
  /** Absolute path of the paper repo. */
  path: string
  /** Main .tex file, relative to the repo root (e.g. "main.tex"). */
  main: string
  /** True when the repo has a .rewrite/ directory. */
  initialized: boolean
}

/** One entry of GET /api/files. */
export interface FileEntry {
  /** Repo-relative path, e.g. "sections/intro.tex". */
  path: string
  /** Repo-relative path of the file whose \input/\include pulled this one in; null for the main file. */
  included_by: string | null
}

/** GET /api/files */
export interface FileList {
  main: string
  files: FileEntry[]
}

/** GET /api/file?path= */
export interface FileContent {
  path: string
  content: string
  /** Modification time as reported by the backend (seconds since the epoch). */
  mtime: number
  /**
   * Revision token: sha256 of the content's UTF-8 bytes. Sent back as
   * `base_sha256` when saving, so the backend can refuse (409) a save over a
   * file that changed on disk in between. Missing from older backends.
   */
  sha256?: string
}

/** PUT /api/file */
export interface SavedFile {
  path: string
  mtime: number
  /** Revision token of the text just written (see FileContent.sha256). */
  sha256?: string
}

export type Severity = 'error' | 'warning' | 'info'

/** One parsed problem from the TeX log. */
export interface LogError {
  /** Repo-relative file, or null when the log does not say which file. */
  file: string | null
  /** 1-based line number, or null when unknown. */
  line: number | null
  message: string
  severity: Severity
}

/** POST /api/compile */
export interface CompileResult {
  ok: boolean
  /** Path of the built PDF (relative to the repo), or null if none was produced. */
  pdf: string | null
  /** Raw latexmk / TeX log text. */
  log: string
  errors: LogError[]
  duration_ms: number
}

// ---- Phase 2: SyncTeX ------------------------------------------------------
//
// Coordinates are PDF points (1/72 inch) measured from the TOP-LEFT corner of
// the page, with y growing downwards: the same convention pdf.js uses when it
// draws a page on a canvas. See pdf/coords.ts for the conversion to pixels.

/** One highlight rectangle from GET /api/synctex/forward. */
export interface SyncBox {
  /** 1-based page number. */
  page: number
  /** Left edge, in points from the page's left edge. */
  x: number
  /** Top edge, in points from the page's top edge. */
  y: number
  /** Width in points. */
  w: number
  /** Height in points. */
  h: number
}

/** GET /api/synctex/forward */
export interface ForwardSyncResult {
  /** Empty when SyncTeX has no record for the line (e.g. a blank line or the preamble). */
  boxes: SyncBox[]
}

/** GET /api/synctex/reverse: the source position under a point of the PDF. */
export interface SyncEdit {
  /** Repo-relative path, e.g. "sections/intro.tex". */
  file: string
  /** 1-based line. */
  line: number
  /** Column; SyncTeX rarely knows it, so expect 0 (or -1). */
  col: number
}

// ---- Phase 2: config and Docker ------------------------------------------

export type CompilerKind = 'native' | 'docker'

/** GET /api/config and PUT /api/config: the UI-editable part of .rewrite/config.toml. */
export interface AppConfig {
  /** Main .tex file, relative to the repo root. */
  main: string
  paper: {
    /** The writing style: a file in .rewrite/prompts/styles/ without .md, or "" for none. */
    style: string
  }
  /** The styles available in this repo (prompts/styles/*.md), for the Settings dropdown. */
  styles: StyleInfo[]
  compiler: {
    kind: CompilerKind
    /** TeX Live year for the Docker image (texlive/texlive:TL<year>-historic). */
    texlive_year: number
    /** Full Docker image name for the configured year. */
    image: string
  }
  docker: {
    /** True when `docker` is on the backend's PATH. */
    available: boolean
    /** True when the image above is already pulled. */
    image_present: boolean
  }
}

/** One writing style (prompts/styles/<name>.md): the field's standard of evidence. */
export interface StyleInfo {
  name: string
  title: string
  description: string
  /** Repo-relative path of the file. */
  file: string
}

/** Body of PUT /api/config; each part is optional and only the given parts change. */
export interface ConfigUpdate {
  paper?: { style: string }
  compiler?: { kind: CompilerKind; texlive_year: number }
}

// ---- Phase 3: the paper.json spec ------------------------------------------
//
// paper.json is a tree of nodes (PLAN.md "paper.json data model"):
//   message (the paper's takeaway, one card) -> sections -> paragraphs,
// plus a flat bank of claims that sections and paragraphs point at by id.
// `tree` is the structural root: a node with level "message" and the same id
// as `message`, whose children are the sections. `message` itself holds the
// takeaway text and fields; its children are always empty.

/** The four node levels. */
export type NodeLevel = 'message' | 'claim' | 'section' | 'paragraph'

/** Where a node lives in the LaTeX source. */
export interface Anchor {
  /** Repo-relative .tex file, e.g. "sections/intro.tex". */
  file: string
  /**
   * What to search for in that file: "@p <id>" for paragraphs (the
   * `% @p <id>` comment line above the paragraph), "\section{Heading}" or
   * "\label{...}" for sections.
   */
  marker: string
}

/** One node of paper.json. Every level has the same shape. */
export interface Node {
  id: string
  level: NodeLevel
  /** The one-line meaning: takeaway, claim statement, section promise or paragraph message. */
  says: string
  children: Node[]
  /** Ids of claims (from PaperSpec.claims) this node argues. */
  claims: string[]
  /** Level-specific fields, see LEVEL_FIELDS. */
  fields: Record<string, string>
  /** null while the node is only planned (not yet in the .tex). */
  anchor: Anchor | null
  /** Free text; promote/demote moves the displaced fields here as "key: value" lines. */
  notes: string
}

/** The whole paper.json document. */
export interface PaperSpec {
  version: 1
  message: Node
  claims: Node[]
  tree: Node
}

/** Which `fields` each level has (PLAN.md table). */
export const LEVEL_FIELDS: Record<NodeLevel, string[]> = {
  message: ['problem', 'stakes', 'thesis'],
  claim: ['evidence', 'scope'],
  section: ['figure_story'],
  paragraph: ['function'],
}

/** One validation problem; `path` points into the document, e.g. "tree.children[0].id". */
export interface SpecError {
  path: string
  message: string
  /** 1-based position in the text, set for JSON syntax errors. */
  line?: number | null
  column?: number | null
}

/** GET /api/spec. `spec` is null when paper.json is missing or invalid (see `errors`). */
export interface SpecFile {
  spec: PaperSpec | null
  /** The file's text, or "" when it does not exist. */
  raw: string
  errors: SpecError[]
}

/**
 * Answer of every spec write: the new document and its serialized text.
 * Ops that create a node (insert, lift_to_claim, add_claim, adopt) also
 * return `new_id`; adopting a paragraph also returns where the marker went.
 */
export interface SpecResponse {
  spec: PaperSpec
  raw: string
  new_id?: string
  file?: string
  line?: number
}

/**
 * Status of a section or paragraph node (the dot in front of each row):
 *   planned   no anchor yet
 *   linked    the anchor resolves in the source
 *   stale     the anchored paragraph changed since `says` was last edited
 *   orphaned  the marker is no longer found in the source
 */
export type NodeStatusKind = 'planned' | 'linked' | 'stale' | 'orphaned'

export interface NodeStatus {
  status: NodeStatusKind
  /** Where the anchor is (null when planned or not found). */
  file: string | null
  /** 1-based line of the marker or heading (null when planned or not found). */
  line: number | null
  /**
   * Why this node should be re-read after a spec change above it ("the promise of
   * intro-1 changed", "claim c2 changed", "the message changed"), or null. Cleared by
   * Reconcile or by editing the node. Claims have a status too: planned (no anchor),
   * linked (its sentence is in the file), stale (reworded or gone), orphaned (file not
   * in the include tree).
   */
  reread: string | null
}

/** GET /api/spec/status */
export interface SpecStatus {
  nodes: Record<string, NodeStatus>
}

/** A \section or \subsection heading found in the .tex (GET /api/spec/skeleton). */
export interface SkeletonSection {
  file: string
  line: number
  kind: 'section' | 'subsection'
  heading: string
  label: string | null
  /** Id of the spec node already anchored to this heading, or null. */
  adopted_id: string | null
}

/** A prose paragraph found in the .tex (GET /api/spec/skeleton). */
export interface SkeletonParagraph {
  file: string
  line_start: number
  line_end: number
  /** First 80 characters of the paragraph. */
  preview: string
  /** Heading of the section it sits in, or null before the first heading. */
  section_heading: string | null
  /** Id from a `% @p <id>` marker above it, or null when not adopted. */
  marker_id: string | null
}

/** GET /api/spec/skeleton: headings and paragraphs in document order. */
export interface Skeleton {
  sections: SkeletonSection[]
  paragraphs: SkeletonParagraph[]
}

/** One `\label{...}` of the paper (GET /api/spec/labels). */
export interface LabelInfo {
  label: string
  /** figure | table | equation | listing | section | other */
  kind: string
  file: string
  line: number
  /** The environment's caption, or null. */
  caption: string | null
}

/** GET /api/spec/labels: every label in document order, so spec text can refer to figures by label. */
export function getLabels(): Promise<{ labels: LabelInfo[] }> {
  return getJson<{ labels: LabelInfo[] }>('/api/spec/labels')
}

/** Levels that can sit in the tree below the message (Tab / Shift-Tab switch between them). */
export type TreeLevel = 'section' | 'paragraph'

/**
 * One edit, sent to POST /api/spec/ops. The backend applies it to the
 * on-disk spec, validates and writes paper.json, and answers with the new
 * spec. `index` is the position in the parent's children AFTER the edit
 * (for `move`: the node is taken out first, then inserted at `index`).
 */
export type SpecOp =
  /** Change a node's text. `fields` is merged key by key. */
  | { type: 'update'; id: string; says?: string; fields?: Record<string, string>; notes?: string }
  /** New empty section/paragraph under `parent_id` at `index`; the answer carries `new_id`. */
  | { type: 'insert'; parent_id: string; index: number; level: TreeLevel; says?: string }
  /** Remove a node and its children (claims stay in the bank). */
  | { type: 'delete'; id: string }
  /** Reorder or reparent. */
  | { type: 'move'; id: string; parent_id: string; index: number }
  /** Promote (paragraph -> section) or demote (section -> paragraph); fields swap, old ones go to notes. */
  | { type: 'set_level'; id: string; level: TreeLevel }
  /** Copy the node's `says` into a new claim and link it; the answer carries `new_id`. */
  | { type: 'lift_to_claim'; id: string }
  | { type: 'link_claim'; id: string; claim_id: string }
  | { type: 'unlink_claim'; id: string; claim_id: string }
  /** New claim; the answer carries `new_id`. */
  | { type: 'add_claim'; says: string; fields?: Record<string, string>; anchor?: Anchor | null }
  | { type: 'update_claim'; id: string; says?: string; fields?: Record<string, string>; notes?: string }
  /** Remove a claim and every reference to it. */
  | { type: 'delete_claim'; id: string }
  | { type: 'set_anchor'; id: string; anchor: Anchor | null }

/** Body of POST /api/spec/adopt. */
export type AdoptRequest =
  /** Make a section node for the heading at file:line. */
  | { kind: 'section'; file: string; line: number }
  /**
   * Insert `% @p <id>` above the paragraph at file:line_start (any line of
   * it) and add a paragraph node for it. With `id` of an existing node, only
   * the marker is (re)inserted (moved, if it was elsewhere) and that node's
   * anchor set. `says`, when given, becomes the node's `says`.
   */
  | { kind: 'paragraph'; file: string; line_start: number; parent_id?: string; id?: string; says?: string }

/** POST /api/spec/nodes/{id}/reconcile */
export interface ReconcileResult {
  id: string
  status: NodeStatusKind
}

// ---- Phase 4: review passes, runs and findings -------------------------------
//
// A *pass* is a prompt template (.rewrite/prompts/passes/<name>.md) run
// over a *scope* (whole paper, one section, one file, or a selection). Each
// run spawns `claude` or `codex` (or is pasted back from a web UI) and yields
// *findings*: problems anchored to a quote and a line range in a .tex file.
// The model never writes prose: there is no rewrite field, and findings that
// try to sneak one in are *rejected* (shown in the run detail, never applied).

/**
 * What a run reviews. Unused members are null.
 * "ends" (phase 6) is the abstract plus the first and the last section; only
 * the message-drift member of a fan-out uses it, the launcher does not offer it.
 */
export type ScopeKind = 'paper' | 'section' | 'file' | 'selection' | 'ends'

export interface Scope {
  kind: ScopeKind
  /** Section node id (kind "section"), else null. */
  ref: string | null
  /** Repo-relative file (kinds "file" and "selection"), else null. */
  file: string | null
  /** 1-based first and last line (kind "selection"), else null. */
  line_start: number | null
  line_end: number | null
}

/** Granularity of a pass and of its findings, coarse to fine. */
export type FindingLevel = 'message' | 'claim' | 'section' | 'paragraph' | 'sentence'

/** The five levels in coarse-to-fine order (for grouping and filter chips). */
export const FINDING_LEVELS: FindingLevel[] = ['message', 'claim', 'section', 'paragraph', 'sentence']

/**
 * What a pass file is for (front matter `kind`, phase 6):
 *   review   an ordinary review pass that yields findings (the launcher lists these)
 *   triage   the triage call over one run's findings (started from a run, not the launcher)
 *   compare  the blind A/B comparison (started from the compare dialog)
 */
export type PassKind = 'review' | 'triage' | 'compare'

/** One pass from GET /api/passes (the front matter of its prompt file). */
export interface Pass {
  name: string
  /** Usually one of FINDING_LEVELS. */
  level: string
  /** Default scope kind for this pass ("paper", "section", ...). */
  scope: string
  /** True only for passes whose text is not the author's voice (latex-hygiene). */
  allow_rewrites: boolean
  /** Model from the front matter; null = the config default for the level. */
  model: string | null
  thinking: string | null
  /** Prompt file, relative to the repo. */
  file: string
  /** First line of the prompt body (at most 120 characters). */
  description: string
  /** Set when the front matter is broken; such a pass cannot be run. */
  error?: string | null
  /** Phase 6. Missing (an older backend) means "review". */
  kind?: PassKind
  /** Backend extra: false for passes POST /api/runs refuses (ab-compare, triage). */
  runnable?: boolean
}

/** A pass's kind, treating a missing `kind` (older backends) as "review". */
export function passKind(pass: Pass): PassKind {
  return pass.kind ?? 'review'
}

export type RunnerName = 'claude' | 'codex'
export type RunStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | 'cached'

/** One run of a pass (GET /api/runs, POST /api/runs, WebSocket run.* events). */
export interface Run {
  id: string
  pass: string
  level: string
  scope: Scope
  /** Human-readable scope, e.g. "Section: Introduction". */
  scope_name: string
  /** "paste" for findings pasted back from a web UI. */
  /** 'author' is a human comment stored as a run of one finding (phase 8, Revise). */
  runner: RunnerName | 'paste' | 'author'
  model: string | null
  thinking: string | null
  status: RunStatus
  /** For a cache hit: the run whose results were reused. */
  cached_from: string | null
  git_sha: string | null
  /** Seconds since the epoch. */
  started_at: number | null
  finished_at: number | null
  duration_ms: number | null
  input_tokens: number | null
  output_tokens: number | null
  cost_usd: number | null
  error: string | null
  findings_count: number
  rejected_count: number
  /** Backend extra: when the run was accepted (seconds since the epoch). */
  created_at?: number
}

export type FindingSeverity = 'minor' | 'major' | 'critical'
export type FindingState = 'open' | 'addressed' | 'dismissed'
/**
 * Where the finding's quote is now:
 *   ok          found where the model said (or re-found unchanged)
 *   unverified  the quote was not found; the model's line numbers are used as given
 *   moved       re-found after an edit, at other lines
 *   stale       no longer found after an edit
 */
export type AnchorState = 'ok' | 'unverified' | 'moved' | 'stale'

/** One problem found by a run. There is deliberately no "replacement" field. */
export interface Finding {
  id: string
  run_id: string
  file: string
  /** Lines as the model reported them. */
  line_start: number
  line_end: number
  /** Exact text from the source that the finding is about. */
  quote: string
  level: string
  category: string
  severity: FindingSeverity
  problem: string
  why: string
  /** Id of the paper.json node the finding refers to, if any. */
  json_ref: string | null
  state: FindingState
  anchor_state: AnchorState
  /** Lines where the quote actually is (what the editor highlights). */
  anchor_line_start: number
  anchor_line_end: number
  created_at: number
  /**
   * Phase 6: the latest triage call's verdict on this finding (null = not
   * triaged). The verdict never changes `state` by itself; "Dismiss all
   * flagged" does that on request.
   */
  triage_verdict?: TriageVerdict | null
  /** The triage model's one-line reason for its verdict. */
  triage_reason?: string | null
}

/** A finding the validator refused (no rewrites, no praise, unknown fields...). */
export interface Rejected {
  /** Position in the model's findings list. */
  index: number
  /** e.g. "rewrite_text", "praise", "unknown_field:replacement". */
  reason: string
  /** The finding exactly as the model returned it. */
  raw: unknown
}

/**
 * How a runner's CLI is logged in (`claude auth status` / `codex login status`).
 * A null field means the backend could not tell.
 */
export interface RunnerAuth {
  logged_in: boolean | null
  /** e.g. "claude.ai" (claude) or "ChatGPT" (codex); "api_key" / "API key" for a key. */
  method: string | null
  /** Subscription type, API-key source, or why the login could not be checked. */
  detail: string | null
}

/** One runner from GET /api/runners. */
export interface RunnerInfo {
  name: RunnerName
  /** False when the CLI is not on the backend's PATH, or is not logged in. */
  available: boolean
  /** Model name suggestions for the picker (free text is allowed too). */
  models: string[]
  /** Whether the CLI is started with flags that drop global instruction files. */
  suppress_global: boolean
  /** Why the runner is unavailable ("... was not found on PATH", "not logged in: run ..."). */
  error?: string | null
  /** The CLI's login; null or missing when the CLI was not found. */
  auth?: RunnerAuth | null
  /** Set when the login is not a claude.ai / ChatGPT subscription (e.g. an API key). */
  warning?: string | null
}

/** Default runner, model and thinking level for one pass level (config.toml [models]). */
export interface LevelDefaults {
  runner: string
  model: string | null
  thinking: string
}

/** GET /api/runners */
export interface RunnersInfo {
  runners: RunnerInfo[]
  thinking_levels: string[]
  /** Keyed by pass level ("message", "paragraph", ...). */
  defaults: Record<string, LevelDefaults>
  /** Global instruction files (~/.claude/CLAUDE.md, ...) the CLIs may still read. */
  global_instructions: { runner: string; path: string; present: boolean }[]
}

/** Body of POST /api/runs and POST /api/runs/preview. Omitted options use the defaults. */
export interface RunRequest {
  pass: string
  scope: Scope
  runner?: RunnerName
  model?: string
  thinking?: string
  /** Run even when an identical earlier run could be reused (POST /api/runs only). */
  force?: boolean
}

/** POST /api/runs/preview: the exact prompt a run would send, without running it. */
export interface PromptPreview {
  prompt: string
  scope_name: string
  /** True when POST /api/runs would answer from the cache right away. */
  cache_hit: boolean
  model: string | null
  thinking: string | null
  /** The argv that would run (the prompt goes in on stdin, not in argv). */
  command: string[]
}

/** GET /api/runs/{id}/prompt: the prompt a run actually sent. */
export interface StoredPrompt {
  prompt: string
  command: string[]
}

/** GET /api/runs/{id} */
export interface RunDetail {
  run: Run
  findings: Finding[]
  rejected: Rejected[]
  /** The model's raw output; null while the run is still going. */
  raw_output: string | null
}

/** Body of POST /api/runs/paste: findings JSON pasted back from a web UI. */
export interface PasteRequest {
  pass: string
  scope: Scope
  /** The model's answer, e.g. `{"findings": [...]}` (fenced or with a preamble is fine). */
  raw: string
}

/** Optional filters of GET /api/findings. */
export interface FindingsQuery {
  file?: string
  state?: FindingState
  run_id?: string
}

// ---- Phase 5: git, snapshots, blind A/B compare, coverage ---------------------
//
// The backend shells out to the host's `git` for everything; nothing here
// reimplements git. "Flag major revision" makes a *snapshot*: an annotated
// tag `rewrite/rev-<n>-<slug>` on HEAD (after committing a dirty tree), so
// a paragraph can later be compared, blind, against that version.

/** One changed path from GET /api/git/status. */
export interface GitFile {
  /** Repo-relative path. */
  path: string
  /** git's short status: "M" modified, "A" added, "D" deleted, "R" renamed, "??" untracked, ... */
  status: string
  /** True when the change is already in the index (staged). */
  staged: boolean
}

/** GET /api/git/status */
export interface GitStatus {
  /** Current branch, or null on a detached HEAD. */
  branch: string | null
  /** Upstream branch such as "origin/main", or null when none is set. */
  upstream: string | null
  /** Commits on this branch that the upstream does not have (to push). */
  ahead: number
  /** Commits on the upstream that this branch does not have (to pull). */
  behind: number
  head_sha: string | null
  head_subject: string | null
  files: GitFile[]
  /** True when nothing is changed or untracked. */
  clean: boolean
  /** False when the paper repo is not a git repository (every other field is then empty). */
  is_repo: boolean
}

/** One entry of GET /api/git/log, newest first. */
export interface Commit {
  sha: string
  subject: string
  author: string
  /** Commit date in seconds since the epoch. */
  date: number
  /** Tags pointing at this commit (snapshots are tags named rewrite/rev-<n>-<slug>). */
  tags: string[]
}

/** A "flag major revision" snapshot: a tag plus the note it was made with. */
export interface Snapshot {
  id: string
  /** e.g. "rewrite/rev-2-first-full-draft" */
  tag: string
  sha: string
  note: string
  /** Seconds since the epoch. */
  created_at: number
}

/** POST /api/git/commit: the new commit. */
export interface CommitResult {
  sha: string
  subject: string
}

/** Which earlier version a comparison uses as "before". `ref` is the snapshot's tag, or a commit sha. */
export interface CompareRef {
  kind: 'snapshot' | 'sha'
  ref: string
}

export type CompareStatus = 'queued' | 'running' | 'done' | 'failed'
export type CompareVerdict = 'A' | 'B' | 'tie'
/** Which version the model saw as "A" and which as "B". */
export interface CompareMapping {
  A: 'before' | 'after'
  B: 'before' | 'after'
}

/**
 * One blind A/B comparison of a paragraph (POST /api/compare, GET /api/compare).
 * `mapping` stays null until the comparison is done AND the UI asked for it
 * (GET /api/compare/{id}?reveal=true), so the verdict is read blind first.
 */
export interface Comparison {
  id: string
  file: string
  node_id: string | null
  line_start: number | null
  line_end: number | null
  before: CompareRef
  /** The phase 4 run that carries the model call (cancel, cost and prompt work through it). */
  run_id: string | null
  status: CompareStatus
  verdict: CompareVerdict | null
  /** The model's one-paragraph reason for its verdict. */
  reason: string | null
  mapping: CompareMapping | null
  error: string | null
  created_at: number
  finished_at: number | null
}

/** GET /api/compare/{id}: the comparison, both texts, and its run. */
export interface ComparisonDetail {
  comparison: Comparison
  /** The paragraph at the "before" ref. */
  before_text: string
  /** The paragraph in the working tree. */
  after_text: string
  run: Run | null
}

/** Body of POST /api/compare. Give `node_id` (a paragraph with a marker) or a line range. */
export interface CompareRequest {
  file: string
  node_id?: string
  line_start?: number
  line_end?: number
  before: CompareRef
  runner?: RunnerName
  model?: string
  thinking?: string
}

/** GET /api/spec/coverage: where each claim is argued, per section. */
export interface Coverage {
  claims: { id: string; says: string }[]
  /** `heading` is the \section text the node is anchored to, or null when planned. */
  sections: { id: string; says: string; heading: string | null }[]
  /**
   * cells[claim][section] = how many nodes in that section's subtree (the
   * section itself and its paragraphs) link the claim. Missing = 0.
   */
  cells: Record<string, Record<string, number>>
  /** Claims linked from no section or paragraph (empty rows). */
  unreferenced_claims: string[]
  /** Sections that link no claim anywhere in their subtree (empty columns). */
  empty_sections: string[]
}

// ---- Phase 6: run groups, cross-runner merge, triage ---------------------------
//
// "Run both" and "Fan out" create a *run group* that owns several ordinary
// runs (each one is a normal phase 4 run: same cache, cancel, cost, prompt).
//   both    the same pass and scope on claude and on codex; their findings
//           are merged by anchor overlap, agreements first
//   fanout  scope "paper" split into one run per linked section, plus one
//           message-drift run over scope "ends"; at most 3 run at once
// The *triage* call sends one run's findings back to a fresh model call that
// marks each one keep / wrong / trivial; the verdicts are stored on the
// findings and never change their state by themselves.

export type GroupKind = 'both' | 'fanout'
/**
 * queued -> running -> done (every member done or cached), partial (some
 * failed or were cancelled), failed (all failed), or cancelled (group cancel).
 */
export type GroupStatus = 'queued' | 'running' | 'done' | 'partial' | 'failed' | 'cancelled'

/** One run group (GET /api/groups, POST /api/groups/both|fanout, `group.updated`). */
export interface RunGroup {
  id: string
  kind: GroupKind
  pass: string
  /** For a fan-out this is always the whole paper. */
  scope: Scope
  /** The member runs, in the order they were planned. */
  run_ids: string[]
  status: GroupStatus
  created_at: number
  finished_at: number | null
  /** Sum over the members; null when no member reported a cost (codex). */
  cost_usd: number | null
  findings_count: number
  /** Merged entries found by more than one runner ("both" groups). */
  agreements_count: number
}

/**
 * One entry of a group's merged findings. Findings of different runs agree
 * when they are in the same file and their lines overlap (or their quotes
 * share at least half of the shorter one). Entries arrive sorted: most
 * agreement first, then file, then line. For a fan-out every finding is its
 * own entry (agreement 1).
 */
export interface MergedFinding {
  key: string
  file: string
  line_start: number
  line_end: number
  /** The highest severity among `findings`. */
  severity: FindingSeverity
  level: string
  category: string
  /** How many runs reported it. */
  agreement: number
  /** Which runners reported it ("claude", "codex"). */
  runners: string[]
  /** The ordinary findings behind the entry, one per run (state, highlight, triage as usual). */
  findings: Finding[]
}

/** GET /api/groups/{id} */
export interface GroupDetail {
  group: RunGroup
  runs: Run[]
  merged: MergedFinding[]
}

/** Per-runner options of "Run both"; omitted keys use the defaults. */
export interface RunnerOptions {
  model?: string
  thinking?: string
}

/** Body of POST /api/groups/both. 400 when either runner is unavailable. */
export interface BothRequest {
  pass: string
  scope: Scope
  claude?: RunnerOptions
  codex?: RunnerOptions
  force?: boolean
}

/** Body of POST /api/groups/fanout (the scope is always the whole paper). 400 when no section is linked. */
export interface FanoutRequest {
  pass: string
  runner?: RunnerName
  model?: string
  thinking?: string
  force?: boolean
}

export type TriageVerdict = 'keep' | 'wrong' | 'trivial'
export type TriageStatus = 'queued' | 'running' | 'done' | 'failed'

/** One triage call over a run's open findings. */
export interface Triage {
  id: string
  /** The run whose findings are judged. */
  run_id: string
  /** The model call itself (an ordinary run of the "triage" pass). */
  triage_run_id: string | null
  status: TriageStatus
  error: string | null
  counts: Record<TriageVerdict, number>
  created_at: number
  finished_at: number | null
}

/** Body of POST /api/runs/{id}/triage; omitted options use the defaults. */
export interface TriageRequest {
  runner?: RunnerName
  model?: string
  thinking?: string
}

/** GET /api/runs/{id}/triage: the latest triage (null if none) and the run's findings with verdicts. */
export interface TriageResult {
  triage: Triage | null
  findings: Finding[]
}

/** Messages pushed by the backend on WS /ws/events (see api/ws.ts). */
export type ServerEvent =
  | { type: 'compile.started' }
  | { type: 'compile.finished'; result?: CompileResult; ok?: boolean }
  | { type: 'file.changed'; path: string }
  /** One line of `docker pull` output (phase 2). */
  | { type: 'docker.pull'; line: string }
  /** `docker pull` ended; `error` is set when ok is false. */
  | { type: 'docker.pull.finished'; ok: boolean; error: string | null }
  /** paper.json was written (phase 3), by this tab or another one. */
  | { type: 'spec.changed' }
  /** A run was accepted and its process started (phase 4). */
  | { type: 'run.started'; run: Run }
  /** Streamed model text ("text") or a progress line ("status") of a running run. */
  | { type: 'run.event'; run_id: string; kind: 'text' | 'status'; text: string }
  /** A run ended with results (status done or cached). */
  | { type: 'run.finished'; run: Run }
  /** A run ended without results (status failed or cancelled). */
  | { type: 'run.failed'; run: Run }
  /** Findings changed: new results, a state change, or re-anchoring after a file edit. */
  | { type: 'findings.updated'; run_id: string | null; file: string | null }
  /** One line of `git push` / `git pull` output (phase 5). */
  | { type: 'git.output'; op: 'push' | 'pull'; line: string }
  /** `git push` / `git pull` ended; `error` is set when ok is false; `changed`: paths a pull changed. */
  | { type: 'git.finished'; op: 'push' | 'pull'; ok: boolean; error: string | null; changed?: string[] }
  /** A blind comparison ended (done or failed). `mapping` is never included here. */
  | { type: 'compare.finished'; comparison: Comparison }
  /** A draft from the spec (phase 7) reached done or failed. */
  | { type: 'draft.finished'; draft: Draft }
  /** A rewrite proposal (phase 8, Revise) reached done or failed. */
  | { type: 'revision.finished'; revision: Revision }
  /** A run group changed (phase 6): sent whenever one of its member runs changes. */
  | { type: 'group.updated'; group: RunGroup }
  /** A triage call ended (done or failed); the verdicts are on the findings now. */
  | { type: 'triage.finished'; triage: Triage }

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

/** Thrown for any non-2xx response or network failure. */
export class ApiError extends Error {
  /** HTTP status code; 0 means the request never reached the backend. */
  readonly status: number
  /** Human-readable message, usually the backend's `detail` string. */
  readonly detail: string
  /** The parsed JSON error body, when there was one (e.g. a 422 with `errors`). */
  readonly body: unknown

  constructor(status: number, detail: string, body: unknown = null) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.body = body
  }
}

/**
 * The validation errors of a failed spec write (422 `{detail, errors}`), or
 * an empty list when the error carries none.
 */
export function specErrorsOf(err: unknown): SpecError[] {
  if (!(err instanceof ApiError) || !err.body || typeof err.body !== 'object') return []
  const errors = (err.body as { errors?: unknown }).errors
  if (!Array.isArray(errors)) return []
  return errors
    .filter((e): e is { path?: unknown; message?: unknown; line?: unknown; column?: unknown } => !!e && typeof e === 'object')
    .map((e) => ({
      path: String(e.path ?? ''),
      message: String(e.message ?? ''),
      line: typeof e.line === 'number' ? e.line : null,
      column: typeof e.column === 'number' ? e.column : null,
    }))
}

/** Turn any thrown value into a message suitable for the UI. */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail
  if (err instanceof Error) return err.message
  return String(err)
}

// ---------------------------------------------------------------------------
// Low-level helpers
// ---------------------------------------------------------------------------

/** Read an error response: the `detail` message plus the parsed body (if JSON). */
async function readError(res: Response): Promise<{ detail: string; body: unknown }> {
  const text = await res.text().catch(() => '')
  let body: unknown = null
  try {
    body = JSON.parse(text)
  } catch {
    // Not JSON: fall through to the generic message below.
  }
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail
    if (typeof detail === 'string') return { detail, body }
    // FastAPI request-validation errors (422) use a list of {msg, loc}.
    if (Array.isArray(detail)) {
      const joined = detail
        .map((d) => (d && typeof d === 'object' && 'msg' in d ? String(d.msg) : JSON.stringify(d)))
        .join('; ')
      return { detail: joined, body }
    }
    return { detail: JSON.stringify(detail), body }
  }
  if (res.status >= 500 && !text) {
    return { detail: `Backend error (HTTP ${res.status}). Is the backend running on 127.0.0.1:8000?`, body }
  }
  return { detail: text || `HTTP ${res.status} ${res.statusText}`, body }
}

/** fetch() that converts network failures and non-2xx responses into ApiError. */
async function send(url: string, init?: RequestInit): Promise<Response> {
  let res: Response
  try {
    res = await fetch(url, init)
  } catch {
    throw new ApiError(0, 'Cannot reach the backend. Is it running?')
  }
  if (!res.ok) {
    const { detail, body } = await readError(res)
    throw new ApiError(res.status, detail, body)
  }
  return res
}

async function getJson<T>(url: string): Promise<T> {
  const res = await send(url)
  return (await res.json()) as T
}

async function sendJson<T>(method: 'POST' | 'PUT' | 'PATCH', url: string, body?: unknown): Promise<T> {
  const res = await send(url, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  return (await res.json()) as T
}

// ---------------------------------------------------------------------------
// Endpoints
// ---------------------------------------------------------------------------

/** GET /api/health: tool availability and the open repo. */
export function getHealth(): Promise<Health> {
  return getJson<Health>('/api/health')
}

/** GET /api/repo: the currently open repo (4xx when none is open). */
export function getRepo(): Promise<RepoInfo> {
  return getJson<RepoInfo>('/api/repo')
}

/** POST /api/repo/open: open an already initialized repo (400 if it has no .rewrite/). */
export function openRepo(path: string): Promise<RepoInfo> {
  return sendJson<RepoInfo>('POST', '/api/repo/open', { path })
}

/** POST /api/repo/init: run `rewrite init` on the path, then open it. */
export function initRepo(path: string): Promise<RepoInfo> {
  return sendJson<RepoInfo>('POST', '/api/repo/init', { path })
}

/** One folder in a GET /api/fs/dirs listing. */
export interface DirEntry {
  name: string
  /** Absolute path. */
  path: string
  /** Has `.rewrite/`, so "Open" would accept it. */
  initialized: boolean
  /** Holds `.tex` files directly: probably a LaTeX project. */
  has_tex: boolean
  is_git: boolean
}

/** GET /api/fs/dirs: the sub-folders of `path` (home when omitted), for the Open dialog's browser. */
export interface DirListing {
  path: string
  parent: string | null
  home: string
  dirs: DirEntry[]
}

export function listDirs(path?: string): Promise<DirListing> {
  const query = path ? `?path=${encodeURIComponent(path)}` : ''
  return getJson<DirListing>(`/api/fs/dirs${query}`)
}

/** POST /api/files/new: create an empty file (folders too); a .tex file is also \input from `include_in` (default: main). */
export interface FileCreated {
  path: string
  /** The file that got the \input line, or null for a non-.tex file. */
  included_by: string | null
  files: FileList
}

export function createFile(path: string, includeIn?: string | null): Promise<FileCreated> {
  return sendJson<FileCreated>('POST', '/api/files/new', includeIn ? { path, include_in: includeIn } : { path })
}

/** GET /api/files: the main file plus every file reached via \input / \include. */
export async function getFiles(): Promise<FileList> {
  const list = await getJson<FileList>('/api/files')
  // Be lenient about `included_by`: accept a list of parents (use the first)
  // as well as a single string, so the tree still renders if the backend
  // reports every includer.
  return {
    main: list.main,
    files: list.files.map((f) => {
      const raw: unknown = f.included_by
      const parent = Array.isArray(raw) ? (raw[0] ?? null) : raw
      return { path: f.path, included_by: typeof parent === 'string' ? parent : null }
    }),
  }
}

/** GET /api/file?path=: read one file. */
export function getFile(path: string): Promise<FileContent> {
  return getJson<FileContent>(`/api/file?path=${encodeURIComponent(path)}`)
}

/**
 * PUT /api/file: write one file. With `baseSha256` (the revision the text was
 * edited from) the backend answers 409 instead of writing when the file on
 * disk is no longer that revision.
 */
export function putFile(path: string, content: string, baseSha256?: string): Promise<SavedFile> {
  return sendJson<SavedFile>('PUT', '/api/file', baseSha256 ? { path, content, base_sha256: baseSha256 } : { path, content })
}

/** True for the 409 a save gets when the file changed on disk since its base revision. */
export function isFileConflict(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409
}

/** POST /api/compile: run the compiler and wait for the result. */
export function compile(): Promise<CompileResult> {
  return sendJson<CompileResult>('POST', '/api/compile')
}

/**
 * GET /api/pdf: the built PDF as bytes, or null when nothing has been built
 * yet (404). `version` only busts the browser cache after a recompile.
 */
export async function fetchPdf(version: number): Promise<ArrayBuffer | null> {
  try {
    const res = await send(`/api/pdf?v=${version}`, { cache: 'no-store' })
    return await res.arrayBuffer()
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null
    throw err
  }
}

// ---- Phase 2: SyncTeX -------------------------------------------------------

/**
 * GET /api/synctex/forward: where a source line appears in the PDF.
 * Returns the highlight boxes (empty when SyncTeX has no record for the line).
 * Throws ApiError 404 when there is no SyncTeX data yet (nothing compiled).
 */
export async function synctexForward(file: string, line: number, col = 0): Promise<SyncBox[]> {
  const query = new URLSearchParams({ file, line: String(line), col: String(col) })
  const result = await getJson<ForwardSyncResult>(`/api/synctex/forward?${query}`)
  return Array.isArray(result.boxes) ? result.boxes : []
}

/**
 * GET /api/synctex/reverse: the source line under a point of the PDF.
 * `x` and `y` are PDF points from the page's top-left corner.
 * Throws ApiError 404 when SyncTeX has no record there (e.g. a margin).
 */
export function synctexReverse(page: number, x: number, y: number): Promise<SyncEdit> {
  // Two decimals (1/7200 inch) is far finer than SyncTeX's own resolution.
  const query = new URLSearchParams({ page: String(page), x: x.toFixed(2), y: y.toFixed(2) })
  return getJson<SyncEdit>(`/api/synctex/reverse?${query}`)
}

// ---- Phase 2: config and Docker --------------------------------------------

/** GET /api/config: compiler settings plus Docker availability. */
export function getConfig(): Promise<AppConfig> {
  return getJson<AppConfig>('/api/config')
}

/** PUT /api/config: change the compiler kind and TeX Live year; returns the new config. */
export function putConfig(update: ConfigUpdate): Promise<AppConfig> {
  return sendJson<AppConfig>('PUT', '/api/config', update)
}

/**
 * POST /api/compile/docker/pull: start pulling the configured TeX Live image.
 * Returns at once; progress arrives as `docker.pull` / `docker.pull.finished`
 * WebSocket events.
 */
export function dockerPull(): Promise<{ started: boolean }> {
  return sendJson<{ started: boolean }>('POST', '/api/compile/docker/pull')
}

// ---- Phase 3: the paper.json spec -------------------------------------------
//
// paper.json is only ever written by these endpoints (the spec editor). Every
// write answers with the whole new spec plus its serialized text, and the
// backend then broadcasts a `spec.changed` WebSocket event.

/** GET /api/spec: the spec, its file text, and validation errors (spec null when invalid). */
export function getSpec(): Promise<SpecFile> {
  return getJson<SpecFile>('/api/spec')
}

/** PUT /api/spec: replace the whole spec (used by undo/redo). 422 + `errors` when invalid. */
export function putSpec(spec: PaperSpec): Promise<SpecResponse> {
  return sendJson<SpecResponse>('PUT', '/api/spec', { spec })
}

/** PUT /api/spec/raw: save hand-edited JSON text (Raw JSON tab). 422 + `errors` when invalid. */
export function putSpecRaw(raw: string): Promise<SpecResponse> {
  return sendJson<SpecResponse>('PUT', '/api/spec/raw', { raw })
}

/** POST /api/spec/ops: apply one edit to the spec on disk. */
export function applySpecOp(op: SpecOp): Promise<SpecResponse> {
  return sendJson<SpecResponse>('POST', '/api/spec/ops', { op })
}

/** GET /api/spec/status: planned / linked / stale / orphaned for every section and paragraph. */
export function getSpecStatus(): Promise<SpecStatus> {
  return getJson<SpecStatus>('/api/spec/status')
}

/** POST /api/spec/nodes/{id}/reconcile: "the prose is right", clears `stale` without editing. */
export function reconcileNode(id: string): Promise<ReconcileResult> {
  return sendJson<ReconcileResult>('POST', `/api/spec/nodes/${encodeURIComponent(id)}/reconcile`)
}

/** GET /api/spec/skeleton: headings and paragraphs parsed from the .tex files. */
export function getSkeleton(): Promise<Skeleton> {
  return getJson<Skeleton>('/api/spec/skeleton')
}

/** POST /api/spec/adopt: turn a heading or paragraph of the .tex into a spec node (see AdoptRequest). */
export function adopt(request: AdoptRequest): Promise<SpecResponse> {
  return sendJson<SpecResponse>('POST', '/api/spec/adopt', request)
}

// ---- Phase 4: passes, runs and findings ---------------------------------------

/** GET /api/passes: every pass file (re-read by the backend when a file changes). */
export async function getPasses(): Promise<Pass[]> {
  const res = await getJson<{ passes: Pass[] }>('/api/passes')
  return Array.isArray(res.passes) ? res.passes : []
}

/** GET /api/runners: claude / codex availability, model suggestions, per-level defaults. */
export function getRunners(): Promise<RunnersInfo> {
  return getJson<RunnersInfo>('/api/runners')
}

/** POST /api/runs/preview: assemble the prompt for "Show prompt" without running anything. */
export function previewRun(request: RunRequest): Promise<PromptPreview> {
  return sendJson<PromptPreview>('POST', '/api/runs/preview', request)
}

/**
 * POST /api/runs: start a run. Answers at once (status queued, running, or
 * cached for a cache hit); progress arrives over the WebSocket. 409 when the
 * same pass is already running on the same scope.
 */
export async function startRun(request: RunRequest): Promise<Run> {
  return (await sendJson<{ run: Run }>('POST', '/api/runs', request)).run
}

/** GET /api/runs: the newest runs first. */
export async function listRuns(limit = 50): Promise<Run[]> {
  const res = await getJson<{ runs: Run[] }>(`/api/runs?limit=${limit}`)
  return Array.isArray(res.runs) ? res.runs : []
}

/** GET /api/runs/{id}: one run with its findings, rejected findings and raw output. */
export async function getRun(id: string): Promise<RunDetail> {
  const res = await getJson<RunDetail>(`/api/runs/${encodeURIComponent(id)}`)
  return {
    run: res.run,
    findings: Array.isArray(res.findings) ? res.findings : [],
    rejected: Array.isArray(res.rejected) ? res.rejected : [],
    raw_output: res.raw_output ?? null,
  }
}

/** GET /api/runs/{id}/prompt: the prompt the run sent, and its argv. */
export function getRunPrompt(id: string): Promise<StoredPrompt> {
  return getJson<StoredPrompt>(`/api/runs/${encodeURIComponent(id)}/prompt`)
}

/** POST /api/runs/{id}/cancel: stop a queued or running run (no-op once it ended). */
export async function cancelRun(id: string): Promise<Run> {
  return (await sendJson<{ run: Run }>('POST', `/api/runs/${encodeURIComponent(id)}/cancel`)).run
}

/** POST /api/runs/paste: turn findings JSON pasted from a web UI into a run. */
export async function pasteRun(request: PasteRequest): Promise<Run> {
  return (await sendJson<{ run: Run }>('POST', '/api/runs/paste', request)).run
}

/** GET /api/findings: findings of every run, optionally filtered. */
export async function getFindings(query: FindingsQuery = {}): Promise<Finding[]> {
  const params = new URLSearchParams()
  if (query.file) params.set('file', query.file)
  if (query.state) params.set('state', query.state)
  if (query.run_id) params.set('run_id', query.run_id)
  const qs = params.toString()
  const res = await getJson<{ findings: Finding[] }>(`/api/findings${qs ? `?${qs}` : ''}`)
  return Array.isArray(res.findings) ? res.findings : []
}

/** PATCH /api/findings/{id}: mark a finding open, addressed or dismissed. */
export async function setFindingState(id: string, state: FindingState): Promise<Finding> {
  return (await sendJson<{ finding: Finding }>('PATCH', `/api/findings/${encodeURIComponent(id)}`, { state })).finding
}

// ---- Phase 5: git ---------------------------------------------------------------

/** GET /api/git/status: branch, ahead/behind, changed and untracked files. */
export async function getGitStatus(): Promise<GitStatus> {
  const res = await getJson<GitStatus>('/api/git/status')
  return { ...res, files: Array.isArray(res.files) ? res.files : [] }
}

/**
 * GET /api/git/diff: unified diff text of one path (untracked files show as
 * a full add), or of the whole tree when `path` is omitted.
 */
export async function getGitDiff(path?: string, staged = false): Promise<string> {
  const params = new URLSearchParams({ staged: String(staged) })
  if (path) params.set('path', path)
  return (await getJson<{ diff: string }>(`/api/git/diff?${params}`)).diff ?? ''
}

/** GET /api/git/log: the newest commits first, with their tags. */
export async function getGitLog(limit = 50): Promise<Commit[]> {
  const res = await getJson<{ commits: Commit[] }>(`/api/git/log?limit=${limit}`)
  return Array.isArray(res.commits) ? res.commits : []
}

/** GET /api/git/show: a file's text at a commit, tag or snapshot. 404 when the file is absent there. */
export async function gitShow(ref: string, path: string): Promise<string> {
  const params = new URLSearchParams({ ref, path })
  return (await getJson<{ content: string }>(`/api/git/show?${params}`)).content
}

/** POST /api/git/commit: commit exactly `paths` (all changed paths when omitted). 400 for an empty message. */
export function gitCommit(message: string, paths?: string[]): Promise<CommitResult> {
  return sendJson<CommitResult>('POST', '/api/git/commit', paths ? { message, paths } : { message })
}

/**
 * POST /api/git/push: starts `git push` and answers at once; the output
 * arrives as `git.output` events and the end as `git.finished`. 409 when a
 * push or pull is already running.
 */
export function gitPush(): Promise<{ started: boolean }> {
  return sendJson<{ started: boolean }>('POST', '/api/git/push')
}

/** POST /api/git/pull: like gitPush, for `git pull`. */
export function gitPull(): Promise<{ started: boolean }> {
  return sendJson<{ started: boolean }>('POST', '/api/git/pull')
}

/** GET /api/snapshots: every "flag major revision" snapshot. */
export async function getSnapshots(): Promise<Snapshot[]> {
  const res = await getJson<{ snapshots: Snapshot[] }>('/api/snapshots')
  return Array.isArray(res.snapshots) ? res.snapshots : []
}

/** POST /api/snapshots: commit a dirty tree as "Snapshot: <note>", then tag HEAD. */
export async function createSnapshot(note: string): Promise<Snapshot> {
  return (await sendJson<{ snapshot: Snapshot }>('POST', '/api/snapshots', { note })).snapshot
}

// ---- Phase 5: blind A/B compare --------------------------------------------------

/** POST /api/compare: start a blind comparison (answers at once, status queued or running). */
export async function startCompare(request: CompareRequest): Promise<Comparison> {
  return (await sendJson<{ comparison: Comparison }>('POST', '/api/compare', request)).comparison
}

/** GET /api/compare: past comparisons, newest first (mapping always null here). */
export async function listComparisons(limit = 50): Promise<Comparison[]> {
  const res = await getJson<{ comparisons: Comparison[] }>(`/api/compare?limit=${limit}`)
  return Array.isArray(res.comparisons) ? res.comparisons : []
}

/**
 * GET /api/compare/{id}: the comparison with both texts. With `reveal` (and
 * only once it is done) the answer also says which version was A and which B.
 */
export function getComparison(id: string, reveal = false): Promise<ComparisonDetail> {
  return getJson<ComparisonDetail>(`/api/compare/${encodeURIComponent(id)}?reveal=${reveal ? 'true' : 'false'}`)
}

// ---- Phase 5: coverage -----------------------------------------------------------

/** GET /api/spec/coverage: the claims x sections matrix of the Spec view's right rail. */
export async function getCoverage(): Promise<Coverage> {
  const res = await getJson<Coverage>('/api/spec/coverage')
  return {
    claims: Array.isArray(res.claims) ? res.claims : [],
    sections: Array.isArray(res.sections) ? res.sections : [],
    cells: res.cells ?? {},
    unreferenced_claims: Array.isArray(res.unreferenced_claims) ? res.unreferenced_claims : [],
    empty_sections: Array.isArray(res.empty_sections) ? res.empty_sections : [],
  }
}

// ---- Phase 6: run groups and triage --------------------------------------------------

/** POST /api/groups/both: the same pass and scope on claude and codex at once. */
export async function startBothGroup(request: BothRequest): Promise<RunGroup> {
  return (await sendJson<{ group: RunGroup }>('POST', '/api/groups/both', request)).group
}

/** POST /api/groups/fanout: one run per linked section plus a message-drift run over the paper's ends. */
export async function startFanoutGroup(request: FanoutRequest): Promise<RunGroup> {
  return (await sendJson<{ group: RunGroup }>('POST', '/api/groups/fanout', request)).group
}

/** GET /api/groups: the newest groups first. */
export async function listGroups(limit = 50): Promise<RunGroup[]> {
  const res = await getJson<{ groups: RunGroup[] }>(`/api/groups?limit=${limit}`)
  return Array.isArray(res.groups) ? res.groups : []
}

/** GET /api/groups/{id}: the group, its member runs and the merged findings. */
export async function getGroup(id: string): Promise<GroupDetail> {
  const res = await getJson<GroupDetail>(`/api/groups/${encodeURIComponent(id)}`)
  return {
    group: res.group,
    runs: Array.isArray(res.runs) ? res.runs : [],
    merged: Array.isArray(res.merged) ? res.merged : [],
  }
}

/** POST /api/groups/{id}/cancel: stop every member that is still queued or running. */
export async function cancelGroup(id: string): Promise<RunGroup> {
  return (await sendJson<{ group: RunGroup }>('POST', `/api/groups/${encodeURIComponent(id)}/cancel`)).group
}

/**
 * POST /api/runs/{id}/triage: ask a fresh model call which of the run's open
 * findings are wrong or trivial. Answers at once; `triage.finished` follows.
 * 409 while a triage of that run is running; 400 when it has no open findings.
 */
export async function startTriage(runId: string, request: TriageRequest = {}): Promise<Triage> {
  return (await sendJson<{ triage: Triage }>('POST', `/api/runs/${encodeURIComponent(runId)}/triage`, request)).triage
}

/** GET /api/runs/{id}/triage: the latest triage of a run (or null) and its findings with verdicts. */
export async function getTriage(runId: string): Promise<TriageResult> {
  const res = await getJson<TriageResult>(`/api/runs/${encodeURIComponent(runId)}/triage`)
  return { triage: res.triage ?? null, findings: Array.isArray(res.findings) ? res.findings : [] }
}

/** POST /api/findings/dismiss-flagged: dismiss the run's findings that triage marked wrong or trivial. */
export async function dismissFlagged(runId: string): Promise<number> {
  return (await sendJson<{ updated: number }>('POST', '/api/findings/dismiss-flagged', { run_id: runId })).updated
}

// ---- Phase 7: drafting from the spec -------------------------------------------------

/** One draft of a paragraph node (POST /api/drafts). The model call writes nothing; `apply`
 *  does, when the author asks. */
export interface Draft {
  id: string
  node_id: string
  run_id: string | null
  status: 'queued' | 'running' | 'done' | 'failed'
  /** The LaTeX paragraph, once done. */
  text: string | null
  /** The model's note for the authors (what the spec left open), possibly empty. */
  notes: string | null
  error: string | null
  created_at: number
  finished_at: number | null
  applied_at: number | null
  applied_mode: 'replace' | 'insert' | null
  applied_file: string | null
  applied_line_start: number | null
  applied_line_end: number | null
}

export interface DraftDetail {
  draft: Draft
  run: Run | null
}

export interface DraftRequest {
  node_id: string
  runner?: string | null
  model?: string | null
  thinking?: string | null
}

/** Body of POST /api/drafts/{id}/apply: replace the paragraph under the node's marker, or
 *  insert a new marker + the text before `line` of `file`. `text` is the author's edit. */
export interface DraftApply {
  mode: 'replace' | 'insert'
  file?: string
  line?: number
  text?: string
}

export interface DraftApplied {
  draft: Draft
  file: string
  line_start: number
  line_end: number
}

export async function startDraft(request: DraftRequest): Promise<Draft> {
  return (await sendJson<{ draft: Draft }>('POST', '/api/drafts', request)).draft
}

export function getDraft(id: string): Promise<DraftDetail> {
  return getJson<DraftDetail>(`/api/drafts/${encodeURIComponent(id)}`)
}

export async function listDrafts(nodeId?: string, limit = 20): Promise<Draft[]> {
  const query = nodeId ? `?node_id=${encodeURIComponent(nodeId)}&limit=${limit}` : `?limit=${limit}`
  return (await getJson<{ drafts: Draft[] }>(`/api/drafts${query}`)).drafts
}

export function applyDraft(id: string, body: DraftApply): Promise<DraftApplied> {
  return sendJson<DraftApplied>('POST', `/api/drafts/${encodeURIComponent(id)}/apply`, body)
}

// ---- Phase 8: Revise (comments and rewrite proposals) -------------------------------------

/** Body of POST /api/findings/comment: an author comment on a span, stored as a finding. */
export interface CommentRequest {
  file: string
  line_start: number
  line_end: number
  quote: string
  problem: string
  why?: string
  json_ref?: string | null
  severity?: FindingSeverity
}

export async function addComment(request: CommentRequest): Promise<Finding> {
  return (await sendJson<{ finding: Finding }>('POST', '/api/findings/comment', request)).finding
}

export type RevisionSpan = 'quote' | 'paragraph'

/** One rewrite proposal for a finding (POST /api/revisions). */
export interface Revision {
  id: string
  finding_id: string
  run_id: string | null
  status: 'queued' | 'running' | 'done' | 'failed'
  span: RevisionSpan
  file: string
  line_start: number
  line_end: number
  /** The span as it read when the proposal was made. */
  original: string
  /** The proposed replacement ("" = drop the span), once done. */
  text: string | null
  notes: string | null
  error: string | null
  created_at: number
  finished_at: number | null
  applied_at: number | null
  applied_file: string | null
  applied_line_start: number | null
  applied_line_end: number | null
}

export interface RevisionDetail {
  revision: Revision
  run: Run | null
  finding: Finding | null
}

export interface RevisionRequest {
  finding_id: string
  span?: RevisionSpan
  runner?: string | null
  model?: string | null
  thinking?: string | null
}

export interface RevisionApplied {
  revision: Revision
  file: string
  line_start: number
  line_end: number
  /** `\aigen{...}` wrappers in the paper after this write. */
  outstanding: number
}

export interface Outstanding {
  count: number
  by_file: Record<string, number>
}

export async function startRevision(request: RevisionRequest): Promise<Revision> {
  return (await sendJson<{ revision: Revision }>('POST', '/api/revisions', request)).revision
}

export function getRevision(id: string): Promise<RevisionDetail> {
  return getJson<RevisionDetail>(`/api/revisions/${encodeURIComponent(id)}`)
}

export async function listRevisions(findingId?: string, limit = 20): Promise<Revision[]> {
  const query = findingId ? `?finding_id=${encodeURIComponent(findingId)}&limit=${limit}` : `?limit=${limit}`
  return (await getJson<{ revisions: Revision[] }>(`/api/revisions${query}`)).revisions
}

/** Approve: write the text (the author's edit) as \aigen{...} with the original kept on a comment line. */
export function applyRevision(id: string, text: string): Promise<RevisionApplied> {
  return sendJson<RevisionApplied>('POST', `/api/revisions/${encodeURIComponent(id)}/apply`, { text })
}

export function getOutstanding(): Promise<Outstanding> {
  return getJson<Outstanding>('/api/revisions/outstanding')
}
