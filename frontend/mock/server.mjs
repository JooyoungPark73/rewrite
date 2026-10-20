// Mock backend for frontend development and the Playwright e2e tests.
//
// Implements the API contract from .agents/scratch/00-overview.md with canned
// data and an in-memory fake paper repo, so the UI can be exercised without
// the real FastAPI backend, LaTeX or a paper on disk. Not used in production.
//
//   node mock/server.mjs                 listens on 127.0.0.1:8799 (MOCK_PORT overrides)
//   npm run dev:mock                     Vite dev server proxied to this mock
//
// Fake behaviour:
// - The repo /tmp/mock-paper is open at startup (MOCK_START_EMPTY=1 starts
//   with no repo, to exercise the Open dialog). Opening any other path fails
//   with 400 and a `detail` message unless it was created with /api/repo/init.
// - Compiling scans the files for "\undefinedmacro" and reports an
//   "Undefined control sequence" error at that file and line, plus one canned
//   warning. Before the first compile GET /api/pdf returns 404.
// - The PDF is a small 2-page document generated in code (see makePdf).
//   Pages are US Letter, 612 x 792 points.
//
// Phase 2 fake behaviour:
// - SyncTeX uses a fixed formula instead of a real .synctex.gz:
//     forward  file:line -> one box { page, x: 72, y: 100 + 14 * (line - 1), w: 468, h: 10 }
//              page 2 for sections/method.tex, page 1 for every other file.
//              No box (empty list) for a blank line or a line past the end.
//     reverse  page 1 -> sections/intro.tex, page 2 -> sections/method.tex,
//              line = floor((y - 100) / 14) + 1; 404 above the first line,
//              below the last line, or on another page.
//   Both answer 404 before the first compile (no SyncTeX data yet).
// - GET/PUT /api/config keep the compiler settings in memory.
// - POST /api/compile/docker/pull emits a few `docker.pull` lines over the
//   WebSocket, then `docker.pull.finished`. Pulling the TL2030 image fails
//   (that year does not exist yet), to exercise the failure path.
//
// Phase 3 fake behaviour (the spec editor; logic in mock/spec.mjs):
// - paper.json lives in memory as text (state.specRaw), starting as the empty
//   spec `rewrite init` writes. GET/PUT /api/spec, PUT /api/spec/raw,
//   POST /api/spec/ops, GET /api/spec/status, GET /api/spec/skeleton,
//   POST /api/spec/adopt and POST /api/spec/nodes/{id}/reconcile follow the
//   contract in .agents/scratch/phase-3-spec-editor.md.
// - Every write broadcasts `spec.changed` and `file.changed` for paper.json
//   (the real backend's file watcher would report it too). Adopting a
//   paragraph writes `% @p <id>` into the fake .tex file and broadcasts
//   `file.changed` for it. Saving a .tex file (PUT /api/file) can make an
//   anchored paragraph "stale".
//
// Phase 4 fake behaviour (review passes, runs and findings; logic in
// mock/runs.mjs): nine passes, claude "installed" and codex not, fake runs that
// stream `run.event` text for ~1.5 s and finish with 3 findings + 1 rejected,
// cache hits, cancel, preview, paste-back, finding states, and re-anchoring
// (`findings.updated`) whenever a .tex file changes.
//
// Phase 5 fake behaviour (logic in mock/git.mjs and mock/compare.mjs): a fake
// git repo whose HEAD differs from the mock files (two "M" files and one
// untracked "??" file), diff, log with tags, commit, push/pull streaming
// `git.output` then `git.finished`, snapshots tagged rewrite/rev-<n>-<slug>,
// blind A/B comparisons that go queued -> running -> done in ~1.5 s with a
// hidden mapping (revealed only with ?reveal=true), and GET /api/spec/coverage
// computed from the in-memory spec (mock/spec.mjs).
//
// Phase 6 fake behaviour (logic in mock/groups.mjs, member runs from
// mock/runs.mjs): "Run both" groups (claude + codex; codex must be switched on
// with /__mock/runs-config {codex_available: true}) whose findings merge into
// one agreement plus two singletons, fan-out groups with one run per linked
// section plus a message-drift run over scope "ends" that finish one after the
// other over ~2 s (at most 3 at once), group cancel, a triage call that marks
// findings keep / wrong / trivial after ~1 s, dismiss-flagged, and the
// `group.updated` / `triage.finished` events.
//
// Several repos (audit fixes 2026-10-18): each repo path has its own fake
// files, paper.json and "stale" baselines. The open repo's copies sit directly
// on `state` (state.files, state.specRaw, ...); opening another repo parks
// them in state.otherRepos[path] and brings in that repo's own (a repo never
// seen before starts with the initial files and the empty spec). Runs, git
// and compare data stay shared between repos.
//
// File revisions (audit fix for item 5, 2026-10-18), as in the real backend:
// GET /api/file and PUT /api/file return `sha256` (of the UTF-8 content), and a
// PUT with `base_sha256` is refused with 409 when the file is no longer that
// revision. A PUT without it (how tests make an "external" change) always writes.
//
// Test-only endpoints (not part of the real API):
//   POST /__mock/repo      {path, files?, spec_raw?}: create (or replace) an initialized
//                          repo at `path` with these files / paper.json text
//   GET  /__mock/saves     every PUT /api/file body received, in order, with the
//                          repo that was open when it arrived ({repo, path, content})
//   GET  /__mock/synctex   every SyncTeX request received, in order
//   POST /__mock/docker    {available: bool}: pretend docker is (not) on PATH
//   POST /__mock/reset     restore the initial state
//   GET  /__mock/spec-log  every spec write request ({repo, method, path, body}), in order
//   POST /__mock/spec-raw  {raw, silent?}: replace paper.json's text as if edited by hand
//                          (not logged; used by tests to seed a spec, or as an "external"
//                          change). Announces `spec.changed` unless `silent` is true, which
//                          stands in for an event the UI missed (socket down).
//   GET  /__mock/runs-log  every write request to /api/runs* and /api/findings*
//   POST /__mock/runs-config  {duration_ms?, codex_available?}: how long a fake run takes
//                          (default 1500), and whether codex counts as installed (default no)
//   GET  /__mock/groups-log    every write request to /api/groups*, triage and dismiss-flagged
//   POST /__mock/groups-config {fanout_ms?, triage_ms?, triage_fail?}
//   GET  /__mock/p5-log    every write request to /api/git*, /api/snapshots, /api/compare
//   POST /__mock/git-config      {push_fails?, pull_fails?, is_repo?}
//   POST /__mock/compare-config  {duration_ms?, winner?: "after"|"before"|"tie", fail?}
import { createHash } from 'node:crypto'
import http from 'node:http'
import { WebSocketServer } from 'ws'
import { createCompareApi, isComparePath } from './compare.mjs'
import { createGitApi, isGitPath } from './git.mjs'
import { createGroupsApi, isGroupsPath } from './groups.mjs'
import { createRunsApi, isRunsPath } from './runs.mjs'
import * as specApi from './spec.mjs'

const PORT = Number(process.env.MOCK_PORT ?? 8799)
const MOCK_REPO = '/tmp/mock-paper'

// Mock SyncTeX geometry, in PDF points from the page's top-left corner.
// e2e/synctex.spec.ts uses the same numbers.
const SYNC_TOP = 100 // y of line 1's box
const SYNC_STEP = 14 // distance between consecutive lines
const SYNC_X = 72
const SYNC_W = 468
const SYNC_H = 10
const SYNC_PAGE_OF = { 'sections/method.tex': 2 } // others: page 1
const SYNC_FILE_ON_PAGE = { 1: 'sections/intro.tex', 2: 'sections/method.tex' }

const INITIAL_FILES = {
  'main.tex': [
    '\\documentclass{article}',
    '\\title{A Mock Paper}',
    '\\begin{document}',
    '\\maketitle',
    '\\input{sections/intro}',
    '\\input{sections/method}',
    '\\end{document}',
    '',
  ].join('\n'),
  'sections/intro.tex': [
    '\\section{Introduction}',
    'Mock papers are useful for testing user interfaces.',
    'This sentence cites a missing key~\\cite{nobody}.',
    '',
  ].join('\n'),
  'sections/method.tex': [
    '\\section{Method}',
    'We describe the method here.',
    'It has several steps.',
    'This line has \\undefinedmacro in it.',
    'The end.',
    '',
  ].join('\n'),
}

/** The per-repo part of the state: files, their mtimes, paper.json and baselines. */
function freshRepoData(files = INITIAL_FILES, specRaw = specApi.serialize(specApi.emptySpec())) {
  return {
    files: { ...files },
    mtimes: Object.fromEntries(Object.keys(files).map((p) => [p, 1_700_000_000])),
    specRaw,
    baselines: {},
    rereads: {},
  }
}

/** Take the open repo's data off `state` (see "Several repos" above). */
function takeRepoData() {
  return { files: state.files, mtimes: state.mtimes, specRaw: state.specRaw, baselines: state.baselines, rereads: state.rereads }
}

/** Make `path` the open repo, parking the previous repo's data. */
function switchRepo(path) {
  if (path === state.repo) return
  if (state.repo !== null) {
    state.otherRepos[state.repo] = takeRepoData()
    Object.assign(state, state.otherRepos[path] ?? freshRepoData())
  }
  // With no repo open yet (MOCK_START_EMPTY) the data on `state` is adopted as is.
  delete state.otherRepos[path]
  state.repo = path
}

let state
// The folder tree GET /api/fs/dirs answers with (names only; see the route).
const FAKE_HOME = '/Users/me'
const FAKE_TREE = {
  '/': ['Users'],
  '/Users': ['me'],
  '/Users/me': ['Documents', 'papers'],
  '/Users/me/Documents': [],
  '/Users/me/papers': ['draft', 'my-paper'],
  '/Users/me/papers/draft': [],
  '/Users/me/papers/my-paper': ['sections'],
  '/Users/me/papers/my-paper/sections': [],
}

function reset() {
  state = {
    repo: process.env.MOCK_START_EMPTY === '1' ? null : MOCK_REPO,
    initialized: new Set([MOCK_REPO]),
    // Data of the repos that are not open, by path.
    otherRepos: {},
    ...freshRepoData(),
    saves: [],
    compiles: 0,
    pdf: null,
    syncRequests: [],
    dockerAvailable: true,
    compiler: { kind: 'native', texlive_year: 2024 },
    pulledImages: new Set(),
    pulling: false,
    // Phase 3: a log of spec writes (paper.json and baselines come from freshRepoData).
    specLog: [],
  }
}

/** paper.json parsed and validated: {spec (null if invalid), errors}. */
function readSpec() {
  let parsed
  try {
    parsed = JSON.parse(state.specRaw)
  } catch (err) {
    return { spec: null, errors: [{ path: '', message: `Invalid JSON: ${err.message}` }] }
  }
  const spec = specApi.normalize(parsed)
  const errors = specApi.validate(spec)
  return { spec: errors.length ? null : spec, errors }
}

/** The current spec for an edit; 409 when the file on disk is invalid. */
function currentSpec() {
  const { spec } = readSpec()
  if (!spec) throw new specApi.SpecApiError(409, 'paper.json is invalid; fix it in the Raw JSON tab first')
  return spec
}

/**
 * Validate and store a new spec; returns the SpecResponse body. Like the real
 * backend, an unchanged spec is not rewritten and announces nothing.
 * `forceBaseline` lists paragraph ids to re-baseline even if `says` is the same.
 */
function writeSpec(next, previous, forceBaseline = []) {
  const errors = specApi.validate(next)
  if (errors.length) throw new specApi.SpecApiError(422, 'The spec is invalid', errors)
  specApi.rebaseline(previous, next, state.files, state.baselines, forceBaseline)
  specApi.propagateRereads(previous, next, state.rereads)
  const raw = specApi.serialize(next)
  if (raw !== state.specRaw) {
    state.specRaw = raw
    broadcast({ type: 'spec.changed' })
    broadcast({ type: 'file.changed', path: 'paper.json' })
  }
  return { spec: next, raw: state.specRaw }
}

/** Store a .tex file written by the backend itself (adopt) and announce it. */
function writeTex(path, content) {
  state.files[path] = content
  state.mtimes[path] = Date.now() / 1000
  broadcast({ type: 'file.changed', path })
  runsApi.fileChanged(path)
}

const imageFor = (year) => `texlive/texlive:TL${year}-historic`

// The writing styles the mock "repo" has (GET /api/config `styles`).
const MOCK_STYLES = [
  { name: 'systems', title: 'Systems, architecture, databases and networking', description: 'Truth by measurement under a stated workload, system and metric.', file: '.rewrite/prompts/styles/systems.md' },
  { name: 'machine-learning', title: 'Machine learning', description: 'Truth by benchmarks, generalisation tests and distribution shift.', file: '.rewrite/prompts/styles/machine-learning.md' },
  { name: 'hci-design', title: 'HCI and design research', description: 'Truth by interpretation of people, practice and context.', file: '.rewrite/prompts/styles/hci-design.md' },
]

function configBody() {
  const image = imageFor(state.compiler.texlive_year)
  return {
    main: 'main.tex',
    paper: { style: state.style ?? '' },
    styles: MOCK_STYLES,
    compiler: { ...state.compiler, image },
    docker: { available: state.dockerAvailable, image_present: state.pulledImages.has(image) },
  }
}

/** Simulated `docker pull`: a few progress lines over the WebSocket, then the result. */
async function fakePull(image) {
  state.pulling = true
  const fails = image.includes('TL2030')
  const lines = fails
    ? [`Pulling ${image}...`, `Error response from daemon: manifest for ${image} not found: manifest unknown`]
    : [
        `${image.split(':')[1]}: Pulling from texlive/texlive`,
        'a1b2c3d4e5f6: Pulling fs layer',
        'a1b2c3d4e5f6: Downloading  512MB/1.2GB',
        'a1b2c3d4e5f6: Download complete',
        'a1b2c3d4e5f6: Pull complete',
        `Status: Downloaded newer image for ${image}`,
      ]
  for (const line of lines) {
    await sleep(120)
    broadcast({ type: 'docker.pull', line })
  }
  await sleep(120)
  state.pulling = false
  if (fails) {
    broadcast({ type: 'docker.pull.finished', ok: false, error: `manifest for ${image} not found` })
  } else {
    state.pulledImages.add(image)
    broadcast({ type: 'docker.pull.finished', ok: true, error: null })
  }
}
reset()

// ---------------------------------------------------------------------------
// Tiny PDF writer: builds a valid PDF with correct xref byte offsets.
// ---------------------------------------------------------------------------
function makePdf(label) {
  const pages = [1, 2]
  const objects = [] // objects[i] is the body of object i + 1
  const fontId = 3 + pages.length * 2
  objects.push('<< /Type /Catalog /Pages 2 0 R >>')
  objects.push(`<< /Type /Pages /Kids [${pages.map((_, i) => `${3 + i * 2} 0 R`).join(' ')}] /Count ${pages.length} >>`)
  for (const [i, n] of pages.entries()) {
    const stream = [
      '0.2 0.4 0.8 rg 72 640 468 40 re f', // a blue bar so the canvas has visible pixels
      'BT /F1 24 Tf 72 700 Td (A Mock Paper) Tj ET',
      `BT /F1 12 Tf 72 600 Td (Page ${n} - ${label}) Tj ET`,
    ].join('\n')
    objects.push(
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 ${fontId} 0 R >> >> /Contents ${4 + i * 2} 0 R >>`,
    )
    objects.push(`<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}\nendstream`)
  }
  objects.push('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')

  let out = '%PDF-1.4\n'
  const offsets = []
  objects.forEach((body, i) => {
    offsets.push(Buffer.byteLength(out))
    out += `${i + 1} 0 obj\n${body}\nendobj\n`
  })
  const xrefAt = Buffer.byteLength(out)
  out += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`
  for (const off of offsets) out += `${String(off).padStart(10, '0')} 00000 n \n`
  out += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xrefAt}\n%%EOF\n`
  return Buffer.from(out, 'latin1')
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function sendJson(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify(body))
}

function readBody(req) {
  return new Promise((resolve) => {
    let data = ''
    req.on('data', (chunk) => (data += chunk))
    req.on('end', () => {
      try {
        resolve(data ? JSON.parse(data) : {})
      } catch {
        resolve(null)
      }
    })
  })
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const repoInfo = () => ({ path: state.repo, main: 'main.tex', initialized: true })

/** The revision token of a text file: sha256 of its UTF-8 bytes (like the backend). */
const sha256 = (text) => createHash('sha256').update(text, 'utf8').digest('hex')

function compileErrors() {
  const errors = []
  for (const [path, text] of Object.entries(state.files)) {
    text.split('\n').forEach((line, i) => {
      if (line.includes('\\undefinedmacro')) {
        errors.push({ file: path, line: i + 1, message: 'Undefined control sequence.\nl.4 This line has \\undefinedmacro', severity: 'error' })
      }
    })
  }
  errors.push({ file: 'sections/intro.tex', line: 3, message: "Citation `nobody' on page 1 undefined", severity: 'warning' })
  return errors
}

// ---------------------------------------------------------------------------
// WebSocket /ws/events
// ---------------------------------------------------------------------------
const wss = new WebSocketServer({ noServer: true })
function broadcast(event) {
  const text = JSON.stringify(event)
  for (const client of wss.clients) if (client.readyState === 1) client.send(text)
}

// Phase 4: passes, runs and findings (mock/runs.mjs). It reads the fake files
// and paper.json through these accessors, since `state` is replaced on reset.
const runsApi = createRunsApi({
  files: () => state.files,
  spec: () => readSpec().spec,
  specRaw: () => state.specRaw,
  broadcast,
  sendJson,
  readBody,
})

// Phase 5: the fake git repo and the blind A/B compare (mock/git.mjs,
// mock/compare.mjs). Same accessor style as the runs module.
const gitApi = createGitApi({
  files: () => state.files,
  specRaw: () => state.specRaw,
  broadcast,
  sendJson,
  readBody,
})
// Phase 6: run groups and triage (mock/groups.mjs) on top of the runs module.
const groupsApi = createGroupsApi({
  runs: runsApi,
  linkedSections: () => {
    const { spec } = readSpec()
    if (!spec) return []
    const status = specApi.computeStatus(spec, state.files, state.baselines, state.rereads)
    return spec.tree.children.filter((n) => n.anchor && status[n.id]?.status === 'linked')
  },
  fileOrder: () => Object.keys(state.files),
  broadcast,
  sendJson,
  readBody,
})
const compareApi = createCompareApi({
  files: () => state.files,
  spec: () => readSpec().spec,
  git: gitApi,
  runs: runsApi,
  broadcast,
  sendJson,
  readBody,
})

// ---------------------------------------------------------------------------
// HTTP routes
// ---------------------------------------------------------------------------
async function handle(req, res) {
  const url = new URL(req.url, `http://${req.headers.host}`)
  const route = `${req.method} ${url.pathname}`

  if (route === 'GET /__mock/saves') return sendJson(res, 200, state.saves)
  if (route === 'GET /__mock/synctex') return sendJson(res, 200, state.syncRequests)
  if (route === 'POST /__mock/docker') {
    const body = await readBody(req)
    state.dockerAvailable = !!body?.available
    return sendJson(res, 200, { ok: true })
  }
  if (route === 'POST /__mock/reset') {
    reset()
    runsApi.reset()
    groupsApi.reset()
    gitApi.reset()
    compareApi.reset()
    return sendJson(res, 200, { ok: true })
  }
  if (route === 'GET /__mock/p5-log') return sendJson(res, 200, [...gitApi.log(), ...compareApi.log()])
  if (route === 'POST /__mock/git-config') {
    gitApi.configure((await readBody(req)) ?? {})
    return sendJson(res, 200, { ok: true })
  }
  if (route === 'POST /__mock/compare-config') {
    compareApi.configure((await readBody(req)) ?? {})
    return sendJson(res, 200, { ok: true })
  }
  if (route === 'GET /__mock/runs-log') return sendJson(res, 200, runsApi.log())
  if (route === 'GET /__mock/groups-log') return sendJson(res, 200, groupsApi.log())
  if (route === 'POST /__mock/groups-config') {
    groupsApi.configure((await readBody(req)) ?? {})
    return sendJson(res, 200, { ok: true })
  }
  if (route === 'POST /__mock/runs-config') {
    runsApi.configure((await readBody(req)) ?? {})
    return sendJson(res, 200, { ok: true })
  }
  if (route === 'GET /__mock/spec-log') return sendJson(res, 200, state.specLog)
  if (route === 'POST /__mock/spec-raw') {
    const body = await readBody(req)
    state.specRaw = typeof body?.raw === 'string' ? body.raw : state.specRaw
    state.baselines = {}
    state.rereads = {}
    if (!body?.silent) broadcast({ type: 'spec.changed' })
    return sendJson(res, 200, { ok: true })
  }
  if (route === 'POST /__mock/repo') {
    const body = await readBody(req)
    const path = typeof body?.path === 'string' ? body.path : ''
    if (!path.startsWith('/')) return sendJson(res, 400, { detail: 'path must be absolute' })
    const data = freshRepoData(
      body.files && typeof body.files === 'object' ? body.files : INITIAL_FILES,
      typeof body.spec_raw === 'string' ? body.spec_raw : undefined,
    )
    state.initialized.add(path)
    if (path === state.repo) Object.assign(state, data)
    else state.otherRepos[path] = data
    return sendJson(res, 200, { ok: true })
  }

  if (route === 'GET /api/health') {
    return sendJson(res, 200, {
      ok: true,
      version: 'mock',
      tools: { latexmk: true, docker: state.dockerAvailable, claude: true, codex: false, git: true },
      repo: state.repo,
    })
  }

  // GET /api/fs/dirs: a small fake folder tree for the Open dialog's browser. Folders that
  // were "initialised" (state.initialized, incl. the open repo) report initialized: true.
  if (route === 'GET /api/fs/dirs') {
    const raw = (url.searchParams.get('path') ?? '').trim() || FAKE_HOME
    if (!raw.startsWith('/')) return sendJson(res, 400, { detail: `path must be absolute: ${raw}` })
    const path = raw.length > 1 ? raw.replace(/\/+$/, '') : raw
    const names = FAKE_TREE[path]
    if (!names) return sendJson(res, 404, { detail: `no such directory: ${raw}` })
    const dirs = names.map((name) => {
      const full = path === '/' ? `/${name}` : `${path}/${name}`
      return { name, path: full, initialized: state.initialized.has(full), has_tex: /paper|draft/.test(name), is_git: /paper/.test(name) }
    })
    return sendJson(res, 200, { path, parent: path === '/' ? null : path.slice(0, path.lastIndexOf('/')) || '/', home: FAKE_HOME, dirs })
  }

  if (route === 'POST /api/repo/open' || route === 'POST /api/repo/init') {
    const body = await readBody(req)
    const path = typeof body?.path === 'string' ? body.path : ''
    if (!path.startsWith('/')) return sendJson(res, 400, { detail: `Not an absolute path: '${path}'` })
    if (route === 'POST /api/repo/init') state.initialized.add(path)
    if (!state.initialized.has(path)) {
      return sendJson(res, 400, { detail: `${path} has no .rewrite/ directory; run "rewrite init" first` })
    }
    switchRepo(path)
    return sendJson(res, 200, repoInfo())
  }

  // Everything below needs an open repo.
  if (url.pathname.startsWith('/api/') && !state.repo) return sendJson(res, 409, { detail: 'No repo is open' })

  if (route === 'GET /api/repo') return sendJson(res, 200, repoInfo())

  if (route === 'GET /api/files') {
    return sendJson(res, 200, {
      main: 'main.tex',
      files: Object.keys(state.files).map((path) => ({ path, included_by: path === 'main.tex' ? null : 'main.tex' })),
    })
  }

  // POST /api/files/new: an empty file (409 if it exists); a .tex file is \input from main.tex
  // before its \end{document}, as the real backend does.
  if (route === 'POST /api/files/new') {
    const body = await readBody(req)
    const path = typeof body?.path === 'string' ? body.path.trim() : ''
    if (!path || path.startsWith('/') || path.includes('..')) return sendJson(res, 400, { detail: `bad path: ${path}` })
    if (!/\.[A-Za-z0-9]+$/.test(path)) return sendJson(res, 400, { detail: `give the file an extension (for example ${path}.tex)` })
    if (path in state.files) return sendJson(res, 409, { detail: `${path} already exists` })
    state.files[path] = ''
    state.mtimes[path] = Date.now() / 1000
    let included_by = null
    if (path.endsWith('.tex')) {
      const target = typeof body?.include_in === 'string' && body.include_in ? body.include_in : 'main.tex'
      if (!(target in state.files)) return sendJson(res, 404, { detail: `file not found: ${target}` })
      const line = `\\input{${path.slice(0, -4)}}`
      const text = state.files[target]
      state.files[target] = text.includes('\\end{document}') ? text.replace(/\\end\{document\}/, `${line}\n\n\\end{document}`) : `${text}${text.endsWith('\n') || !text ? '' : '\n'}${line}\n`
      state.mtimes[target] = Date.now() / 1000
      included_by = target
      broadcast({ type: 'file.changed', path: target })
    }
    return sendJson(res, 200, {
      path,
      included_by,
      files: { main: 'main.tex', files: Object.keys(state.files).map((p) => ({ path: p, included_by: p === 'main.tex' ? null : 'main.tex' })) },
    })
  }

  if (route === 'GET /api/file') {
    const path = url.searchParams.get('path') ?? ''
    if (!(path in state.files)) return sendJson(res, 404, { detail: `No such file: ${path}` })
    return sendJson(res, 200, { path, content: state.files[path], mtime: state.mtimes[path], sha256: sha256(state.files[path]) })
  }

  if (route === 'PUT /api/file') {
    const body = await readBody(req)
    if (!body || typeof body.path !== 'string' || typeof body.content !== 'string') {
      return sendJson(res, 422, { detail: [{ msg: 'path and content are required' }] })
    }
    const onDisk = state.files[body.path]
    if (typeof body.base_sha256 === 'string' && (onDisk === undefined || sha256(onDisk) !== body.base_sha256)) {
      return sendJson(res, 409, {
        detail: `${body.path} changed on disk since the version this edit is based on (another save, a marker insertion or a git pull); reload it and apply the edit again`,
      })
    }
    state.files[body.path] = body.content
    state.mtimes[body.path] = Date.now() / 1000
    state.saves.push({ repo: state.repo, path: body.path, content: body.content })
    sendJson(res, 200, { path: body.path, mtime: state.mtimes[body.path], sha256: sha256(body.content) })
    // The real backend's file watcher reports our own writes too.
    broadcast({ type: 'file.changed', path: body.path })
    // Phase 4: open findings in the file are re-anchored (findings.updated).
    runsApi.fileChanged(body.path)
    return
  }

  if (route === 'POST /api/compile') {
    broadcast({ type: 'compile.started' })
    const started = Date.now()
    await sleep(300)
    state.compiles += 1
    state.pdf = makePdf(`compile #${state.compiles}`)
    const errors = compileErrors()
    const result = {
      ok: !errors.some((e) => e.severity === 'error'),
      pdf: '.rewrite/build/main.pdf',
      log: `This is a mock latexmk log (compile #${state.compiles}).\n! Undefined control sequence.\n`,
      errors,
      duration_ms: Date.now() - started,
    }
    broadcast({ type: 'compile.finished', ok: result.ok, result })
    return sendJson(res, 200, result)
  }

  if (route === 'GET /api/pdf') {
    if (!state.pdf) return sendJson(res, 404, { detail: 'No PDF has been built yet' })
    res.writeHead(200, { 'Content-Type': 'application/pdf', 'Content-Length': state.pdf.length })
    return res.end(state.pdf)
  }

  // ---- Phase 2: SyncTeX ----------------------------------------------------
  if (route === 'GET /api/synctex/forward') {
    const file = url.searchParams.get('file') ?? ''
    const line = Number(url.searchParams.get('line'))
    const col = Number(url.searchParams.get('col') ?? 0)
    state.syncRequests.push({ kind: 'forward', file, line, col })
    if (!file || !Number.isInteger(line) || line < 1) return sendJson(res, 400, { detail: 'file and line >= 1 are required' })
    if (!state.pdf) return sendJson(res, 404, { detail: 'No SyncTeX data yet; compile first' })
    const lines = (state.files[file] ?? '').split('\n')
    if (!(file in state.files) || line > lines.length || lines[line - 1].trim() === '') {
      return sendJson(res, 200, { boxes: [] })
    }
    const box = { page: SYNC_PAGE_OF[file] ?? 1, x: SYNC_X, y: SYNC_TOP + SYNC_STEP * (line - 1), w: SYNC_W, h: SYNC_H }
    return sendJson(res, 200, { boxes: [box] })
  }

  if (route === 'GET /api/synctex/reverse') {
    const page = Number(url.searchParams.get('page'))
    const x = Number(url.searchParams.get('x'))
    const y = Number(url.searchParams.get('y'))
    state.syncRequests.push({ kind: 'reverse', page, x, y })
    if (!Number.isInteger(page) || page < 1 || !Number.isFinite(x) || !Number.isFinite(y)) {
      return sendJson(res, 400, { detail: 'page, x and y are required' })
    }
    if (!state.pdf) return sendJson(res, 404, { detail: 'No SyncTeX data yet; compile first' })
    const file = SYNC_FILE_ON_PAGE[page]
    const line = Math.floor((y - SYNC_TOP) / SYNC_STEP) + 1
    const count = file ? state.files[file].split('\n').length : 0
    if (!file || y < SYNC_TOP || line > count) return sendJson(res, 404, { detail: 'SyncTeX has no record at this position' })
    return sendJson(res, 200, { file, line, col: 0 })
  }

  // ---- Phase 2: config and Docker -----------------------------------------
  if (route === 'GET /api/config') return sendJson(res, 200, configBody())

  if (route === 'PUT /api/config') {
    const body = await readBody(req)
    if (body?.paper && typeof body.paper.style === 'string') {
      const style = body.paper.style.trim()
      if (style && !MOCK_STYLES.some((s) => s.name === style)) {
        return sendJson(res, 400, { detail: `paper.style '${style}' has no file .rewrite/prompts/styles/${style}.md` })
      }
      state.style = style
    }
    if (body?.compiler) {
      const kind = body.compiler.kind
      const year = body.compiler.texlive_year
      if (kind !== 'native' && kind !== 'docker') {
        return sendJson(res, 422, { detail: [{ msg: "compiler.kind must be 'native' or 'docker'" }] })
      }
      if (!Number.isInteger(year) || year < 2018 || year > 2030) {
        return sendJson(res, 422, { detail: [{ msg: 'compiler.texlive_year must be an integer from 2018 to 2030' }] })
      }
      state.compiler = { kind, texlive_year: year }
    }
    return sendJson(res, 200, configBody())
  }

  if (route === 'POST /api/compile/docker/pull') {
    if (!state.dockerAvailable) return sendJson(res, 400, { detail: 'docker is not on PATH' })
    if (state.pulling) return sendJson(res, 409, { detail: 'A docker pull is already running' })
    void fakePull(imageFor(state.compiler.texlive_year))
    return sendJson(res, 200, { started: true })
  }

  // ---- Phase 6: run groups, triage (before the runs routes they extend) --------
  if (isGroupsPath(url.pathname)) return groupsApi.handle(req, res, url)

  // ---- Phase 4: passes, runs, findings -----------------------------------------
  if (isRunsPath(url.pathname)) return runsApi.handle(req, res, url)

  // ---- Phase 5: git, snapshots, blind compare ----------------------------------
  if (isGitPath(url.pathname)) return gitApi.handle(req, res, url)
  if (isComparePath(url.pathname)) return compareApi.handle(req, res, url)

  // ---- Phase 3: the spec ----------------------------------------------------
  if (url.pathname.startsWith('/api/spec')) {
    try {
      return await handleSpec(req, res, url)
    } catch (err) {
      if (err instanceof specApi.SpecApiError) {
        return sendJson(res, err.status, err.errors.length ? { detail: err.detail, errors: err.errors } : { detail: err.detail })
      }
      throw err
    }
  }

  sendJson(res, 404, { detail: `Mock has no route for ${route}` })
}

/** /api/spec/* routes. Throws SpecApiError for 4xx answers. */
async function handleSpec(req, res, url) {
  const route = `${req.method} ${url.pathname}`
  const body = req.method === 'GET' ? null : await readBody(req)
  if (req.method !== 'GET') state.specLog.push({ repo: state.repo, method: req.method, path: url.pathname, body })

  if (route === 'GET /api/spec') {
    const { spec, errors } = readSpec()
    return sendJson(res, 200, { spec, raw: state.specRaw, errors })
  }

  if (route === 'PUT /api/spec') {
    const previous = readSpec().spec
    const next = specApi.normalize(body?.spec)
    return sendJson(res, 200, writeSpec(next, previous))
  }

  if (route === 'PUT /api/spec/raw') {
    let parsed
    try {
      parsed = JSON.parse(typeof body?.raw === 'string' ? body.raw : '')
    } catch (err) {
      throw new specApi.SpecApiError(422, 'paper.json is not valid JSON', [{ path: '', message: err.message }])
    }
    const previous = readSpec().spec
    return sendJson(res, 200, writeSpec(specApi.normalize(parsed), previous))
  }

  if (route === 'POST /api/spec/ops') {
    const previous = currentSpec()
    const { spec, new_id } = specApi.applyOp(previous, body?.op)
    const written = writeSpec(spec, previous)
    return sendJson(res, 200, new_id ? { ...written, new_id } : written)
  }

  if (route === 'GET /api/spec/status') {
    const { spec } = readSpec()
    return sendJson(res, 200, { nodes: spec ? specApi.computeStatus(spec, state.files, state.baselines, state.rereads) : {} })
  }

  if (route === 'GET /api/spec/coverage') {
    return sendJson(res, 200, specApi.coverage(currentSpec()))
  }

  // GET /api/spec/labels: every \label in the fake files, with the enclosing environment's
  // kind and caption (a small version of the backend's scan_labels).
  if (route === 'GET /api/spec/labels') {
    const labels = []
    const seen = new Set()
    for (const [file, text] of Object.entries(state.files)) {
      const stack = []
      text.split('\n').forEach((raw, i) => {
        const line = raw.replace(/(^|[^\\])%.*$/, '$1')
        const events = []
        for (const m of line.matchAll(/\\(begin|end)\s*\{([^{}]+)\}/g)) events.push({ at: m.index, env: m[1], name: m[2] })
        for (const m of line.matchAll(/\\caption\s*(?:\[[^\]]*\])?\s*\{([^{}]*)\}/g)) events.push({ at: m.index, caption: m[1] })
        for (const m of line.matchAll(/\\label\s*\{([^{}]+)\}/g)) events.push({ at: m.index, label: m[1].trim() })
        events.sort((a, b) => a.at - b.at)
        for (const e of events) {
          if (e.env === 'begin') stack.push({ name: e.name, caption: null, found: [] })
          else if (e.env === 'end') { const top = stack.pop(); if (top) for (const f of top.found) f.caption ??= top.caption }
          else if (e.caption !== undefined) { const top = stack.at(-1); if (top) { top.caption = e.caption.replace(/\s+/g, ' ').trim(); for (const f of top.found) f.caption ??= top.caption } }
          else if (e.label && !seen.has(e.label)) {
            seen.add(e.label)
            const env = [...stack].reverse().map((s) => s.name).find((n) => /^(figure|table|equation|align|lstlisting|algorithm)/.test(n))
            const kind = !env ? (/^sec:/.test(e.label) ? 'section' : 'other') : /^figure/.test(env) ? 'figure' : /^table/.test(env) ? 'table' : /^(equation|align)/.test(env) ? 'equation' : 'listing'
            const info = { label: e.label, kind, file, line: i + 1, caption: stack.at(-1)?.caption ?? null }
            stack.at(-1)?.found.push(info)
            labels.push(info)
          }
        }
      })
    }
    return sendJson(res, 200, { labels })
  }

  if (route === 'GET /api/spec/skeleton') {
    return sendJson(res, 200, specApi.skeleton(readSpec().spec, state.files))
  }

  if (route === 'POST /api/spec/adopt') {
    const previous = currentSpec()
    const result = specApi.adopt(previous, state.files, body)
    // Validate before touching the .tex, so a refused adopt writes nothing.
    const errors = specApi.validate(result.spec)
    if (errors.length) throw new specApi.SpecApiError(422, 'The spec is invalid', errors)
    for (const [path, text] of Object.entries(result.fileTexts)) writeTex(path, text)
    const written = writeSpec(result.spec, previous, body.kind === 'paragraph' ? [result.new_id] : [])
    return sendJson(res, 200, { ...written, new_id: result.new_id, file: result.file, line: result.line })
  }

  const reconcile = /^\/api\/spec\/nodes\/([^/]+)\/reconcile$/.exec(url.pathname)
  if (req.method === 'POST' && reconcile) {
    const id = decodeURIComponent(reconcile[1])
    const status = specApi.reconcile(currentSpec(), state.files, state.baselines, id, state.rereads)
    return sendJson(res, 200, { id, ...status })
  }

  return sendJson(res, 404, { detail: `Mock has no route for ${route}` })
}

const server = http.createServer((req, res) => {
  handle(req, res).catch((err) => sendJson(res, 500, { detail: String(err) }))
})

server.on('upgrade', (req, socket, head) => {
  if (new URL(req.url, 'http://x').pathname !== '/ws/events') return socket.destroy()
  wss.handleUpgrade(req, socket, head, (ws) => wss.emit('connection', ws, req))
})

server.listen(PORT, '127.0.0.1', () => {
  console.log(`mock backend on http://127.0.0.1:${PORT} (repo ${state.repo ?? 'none'})`)
})
