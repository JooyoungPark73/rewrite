// In-memory fake of the phase 5 git API, for mock/server.mjs.
//
// Follows the "Decisions fixed for implementation" and "API contract"
// sections of .agents/scratch/phase-5-ab-git-coverage.md closely enough to
// drive the git panel, the snapshots list and the compare dialog's "before"
// picker. Test scaffolding, written for readability; nothing here runs git.
//
// The fake repo:
// - HEAD is a map path -> text. The working tree is the mock's files
//   (server.mjs `state.files`), paper.json (`state.specRaw`) and one
//   untracked file outside the include tree, `notes/todo.md`.
// - At start HEAD has older versions of sections/intro.tex (line 2 differs)
//   and sections/method.tex (no line 4), so status shows two "M" files and
//   one "??" file. Editing anything in the UI shows up as a change too.
// - Three commits exist (newest first), branch main tracks origin/main and is
//   1 commit ahead. No snapshots yet.
// - commit {message, paths} copies those paths' working text into HEAD and
//   prepends a commit; push / pull stream three `git.output` lines over the
//   WebSocket ~150 ms apart and end with `git.finished` (push clears "ahead").
// - POST /api/snapshots commits a dirty tree as "Snapshot: <note>" and tags
//   HEAD `rewrite/rev-<n>-<slug>`.
// - Diffs are real unified diffs (3 lines of context) computed with a small
//   LCS; an untracked file diffs as a full add from /dev/null.
//
// Test-only hook (wired in server.mjs):
//   POST /__mock/git-config  {push_fails?, pull_fails?, is_repo?}
import { createHash } from 'node:crypto'

const UNTRACKED = {
  'notes/todo.md': ['# To do', '', '- tighten the introduction', '- add a figure for the method', ''].join('\n'),
}

const PUSH_LINES = [
  'Enumerating objects: 7, done.',
  'Writing objects: 100% (4/4), 512 bytes | 512.00 KiB/s, done.',
  'To /tmp/mock-remote.git',
]
const PULL_LINES = ['From /tmp/mock-remote.git', ' * branch            main       -> FETCH_HEAD', 'Already up to date.']

export class GitApiError extends Error {
  constructor(status, detail) {
    super(detail)
    this.status = status
    this.detail = detail
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const now = () => Date.now() / 1000

/** Lower-case words joined by "-", at most 32 characters (the snapshot tag's slug). */
export function slugify(text) {
  return (
    String(text)
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 32)
      .replace(/-+$/, '') || 'snapshot'
  )
}

// ---------------------------------------------------------------------------
// Unified diff
// ---------------------------------------------------------------------------

/** Split into lines, dropping the empty string after a trailing newline. */
function toLines(text) {
  if (text === '' || text == null) return []
  const lines = text.split('\n')
  if (lines[lines.length - 1] === '') lines.pop()
  return lines
}

/** Line-level edit script between a and b: [{op: ' '|'-'|'+', text}] (longest common subsequence). */
function editScript(a, b) {
  const n = a.length
  const m = b.length
  const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
  }
  const ops = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      ops.push({ op: ' ', text: a[i++] })
      j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) ops.push({ op: '-', text: a[i++] })
    else ops.push({ op: '+', text: b[j++] })
  }
  while (i < n) ops.push({ op: '-', text: a[i++] })
  while (j < m) ops.push({ op: '+', text: b[j++] })
  return ops
}

/** `git diff`-style text for one file; `before`/`after` null = absent. Empty string when equal. */
export function unifiedDiff(path, before, after, context = 3) {
  if (before === after) return ''
  const header = [`diff --git a/${path} b/${path}`]
  if (before == null) header.push('new file mode 100644', '--- /dev/null', `+++ b/${path}`)
  else if (after == null) header.push('deleted file mode 100644', `--- a/${path}`, '+++ /dev/null')
  else header.push(`--- a/${path}`, `+++ b/${path}`)

  const ops = editScript(toLines(before), toLines(after))
  // Old/new line number (1-based) at each op.
  let oldNo = 1
  let newNo = 1
  const numbered = ops.map((o) => {
    const entry = { ...o, oldNo, newNo }
    if (o.op !== '+') oldNo++
    if (o.op !== '-') newNo++
    return entry
  })
  const changed = numbered.map((o, k) => (o.op === ' ' ? -1 : k)).filter((k) => k >= 0)
  const hunks = []
  for (const k of changed) {
    const from = Math.max(0, k - context)
    const to = Math.min(numbered.length - 1, k + context)
    const last = hunks[hunks.length - 1]
    if (last && from <= last.to + 1) last.to = to
    else hunks.push({ from, to })
  }
  const body = []
  for (const h of hunks) {
    const slice = numbered.slice(h.from, h.to + 1)
    const oldLen = slice.filter((o) => o.op !== '+').length
    const newLen = slice.filter((o) => o.op !== '-').length
    const oldStart = oldLen ? slice.find((o) => o.op !== '+').oldNo : slice[0].oldNo - 1
    const newStart = newLen ? slice.find((o) => o.op !== '-').newNo : slice[0].newNo - 1
    body.push(`@@ -${oldStart},${oldLen} +${newStart},${newLen} @@`)
    for (const o of slice) body.push(`${o.op}${o.text}`)
  }
  return [...header, ...body].join('\n') + '\n'
}

// ---------------------------------------------------------------------------
// The API
// ---------------------------------------------------------------------------

/**
 * ctx: { files(): {path: text}, specRaw(): string, broadcast(event),
 *        sendJson(res, status, body), readBody(req) }
 */
export function createGitApi(ctx) {
  let state
  let shaCounter = 0

  const makeSha = (subject) => createHash('sha1').update(`${++shaCounter}:${subject}`).digest('hex')

  /** The working tree: included files, paper.json and the untracked extras. */
  const working = () => ({ ...ctx.files(), 'paper.json': ctx.specRaw(), ...state.extra })

  function reset() {
    shaCounter = 0
    const files = ctx.files()
    // HEAD: the current files, except two older versions (see the header).
    const head = { ...files, 'paper.json': ctx.specRaw() }
    head['sections/intro.tex'] = (files['sections/intro.tex'] ?? '').replace(
      'Mock papers are useful for testing user interfaces.',
      'Mock papers help when testing interfaces.',
    )
    head['sections/method.tex'] = (files['sections/method.tex'] ?? '')
      .split('\n')
      .filter((line) => !line.includes('\\undefinedmacro'))
      .join('\n')
    const day = 24 * 3600
    const commit = (subject, daysAgo, tree) => ({ sha: makeSha(subject), subject, author: 'Mock Author', date: now() - daysAgo * day, tags: [], tree })
    const withoutMethod = { ...head }
    delete withoutMethod['sections/method.tex']
    const commits = [
      commit('Draft the method section', 1, { ...head }),
      commit('Write the introduction', 3, withoutMethod),
      commit('Initial skeleton', 6, { 'main.tex': head['main.tex'] }),
    ].sort((a, b) => b.date - a.date)
    state = {
      head,
      extra: { ...UNTRACKED },
      commits,
      snapshots: [],
      branch: 'main',
      upstream: 'origin/main',
      ahead: 1,
      behind: 0,
      running: null,
      config: { push_fails: false, pull_fails: false, is_repo: true },
      log: [],
    }
  }
  reset()

  /** Changed paths, sorted: [{path, status, staged}]. */
  function changes() {
    const work = working()
    const paths = [...new Set([...Object.keys(state.head), ...Object.keys(work)])].sort()
    const out = []
    for (const path of paths) {
      const inHead = path in state.head
      const inWork = path in work
      if (inHead && inWork && state.head[path] === work[path]) continue
      out.push({ path, status: !inHead ? '??' : !inWork ? 'D' : 'M', staged: false })
    }
    return out
  }

  function status() {
    if (!state.config.is_repo) {
      return { branch: null, upstream: null, ahead: 0, behind: 0, head_sha: null, head_subject: null, files: [], clean: true, is_repo: false }
    }
    const files = changes()
    const head = state.commits[0]
    return {
      branch: state.branch,
      upstream: state.upstream,
      ahead: state.ahead,
      behind: state.behind,
      head_sha: head?.sha ?? null,
      head_subject: head?.subject ?? null,
      files,
      clean: files.length === 0,
      is_repo: true,
    }
  }

  function diff(path) {
    const work = working()
    const paths = path ? [path] : changes().map((c) => c.path)
    return paths.map((p) => unifiedDiff(p, state.head[p] ?? null, work[p] ?? null)).join('')
  }

  /** Commit `paths` (default: every changed path); returns the new commit. */
  function commit(message, paths) {
    const subject = String(message ?? '').trim().split('\n')[0]
    if (!subject) throw new GitApiError(400, 'The commit message is empty')
    const changed = changes().map((c) => c.path)
    const selected = Array.isArray(paths) ? paths.filter((p) => changed.includes(p)) : changed
    if (selected.length === 0) throw new GitApiError(400, 'Nothing to commit')
    const work = working()
    for (const p of selected) {
      if (p in work) state.head[p] = work[p]
      else delete state.head[p]
    }
    const entry = { sha: makeSha(subject), subject, author: 'Mock Author', date: now(), tags: [], tree: { ...state.head } }
    state.commits.unshift(entry)
    state.ahead += 1
    return entry
  }

  /** Commit, tag or "HEAD" -> commit; throws 404. */
  function resolveRef(ref) {
    const r = String(ref ?? '')
    const hit =
      (r === 'HEAD' ? state.commits[0] : null) ??
      state.commits.find((c) => c.tags.includes(r)) ??
      (r.length >= 4 ? state.commits.find((c) => c.sha.startsWith(r)) : null)
    if (!hit) throw new GitApiError(404, `unknown revision '${r}'`)
    return hit
  }

  /** A file's text at a ref; throws 404 when the ref or the file is not there. */
  function show(ref, path) {
    const commitEntry = resolveRef(ref)
    if (!(path in commitEntry.tree)) throw new GitApiError(404, `path '${path}' does not exist in '${ref}'`)
    return commitEntry.tree[path]
  }

  /** A snapshot by tag or id (the compare request names it by tag). */
  function findSnapshot(ref) {
    return state.snapshots.find((s) => s.tag === ref || s.id === ref) ?? null
  }

  function createSnapshot(note) {
    const text = String(note ?? '').trim()
    if (!text) throw new GitApiError(400, 'A snapshot needs a note')
    if (changes().length) commit(`Snapshot: ${text}`)
    const used = state.commits.flatMap((c) => c.tags).map((t) => /^rewrite\/rev-(\d+)-/.exec(t)?.[1]).filter(Boolean).map(Number)
    const n = Math.max(0, ...used) + 1
    const tag = `rewrite/rev-${n}-${slugify(text)}`
    const head = state.commits[0]
    head.tags.push(tag)
    const snapshot = { id: `snap-${n}`, tag, sha: head.sha, note: text, created_at: now() }
    state.snapshots.push(snapshot)
    return snapshot
  }

  /** A fake `git push` / `git pull`: three output lines, then git.finished. */
  async function remoteOp(op) {
    state.running = op
    const fails = op === 'push' ? state.config.push_fails : state.config.pull_fails
    const lines = op === 'push' ? PUSH_LINES : PULL_LINES
    for (const line of lines) {
      await sleep(150)
      ctx.broadcast({ type: 'git.output', op, line })
    }
    await sleep(150)
    state.running = null
    if (fails) {
      const error = op === 'push' ? "failed to push some refs to '/tmp/mock-remote.git'" : 'Could not read from remote repository.'
      ctx.broadcast({ type: 'git.finished', op, ok: false, error })
      return
    }
    if (op === 'push') state.ahead = 0
    else state.behind = 0
    ctx.broadcast({ type: 'git.finished', op, ok: true, error: null })
  }

  const publicCommit = (c) => ({ sha: c.sha, subject: c.subject, author: c.author, date: c.date, tags: [...c.tags] })

  /** Answer one /api/git/* or /api/snapshots request. */
  async function handle(req, res, url) {
    const route = `${req.method} ${url.pathname}`
    const body = req.method === 'GET' ? null : await ctx.readBody(req)
    if (req.method !== 'GET') state.log.push({ method: req.method, path: url.pathname, body })
    const send = (status, payload) => ctx.sendJson(res, status, payload)
    try {
      if (route === 'GET /api/git/status') return send(200, status())
      if (!state.config.is_repo) throw new GitApiError(400, 'The paper repo is not a git repository')

      if (route === 'GET /api/git/diff') return send(200, { diff: diff(url.searchParams.get('path') || null) })
      if (route === 'GET /api/git/log') {
        const limit = Number(url.searchParams.get('limit') ?? 50) || 50
        return send(200, { commits: state.commits.slice(0, limit).map(publicCommit) })
      }
      if (route === 'GET /api/git/show') {
        return send(200, { content: show(url.searchParams.get('ref'), url.searchParams.get('path') ?? '') })
      }
      if (route === 'POST /api/git/commit') {
        const entry = commit(body?.message, body?.paths)
        return send(200, { sha: entry.sha, subject: entry.subject })
      }
      if (route === 'POST /api/git/push' || route === 'POST /api/git/pull') {
        if (state.running) throw new GitApiError(409, `git ${state.running} is already running`)
        void remoteOp(route.endsWith('push') ? 'push' : 'pull')
        return send(200, { started: true })
      }
      if (route === 'GET /api/snapshots') return send(200, { snapshots: state.snapshots })
      if (route === 'POST /api/snapshots') return send(200, { snapshot: createSnapshot(body?.note) })
      return send(404, { detail: `Mock has no route for ${route}` })
    } catch (err) {
      if (err instanceof GitApiError) return send(err.status, { detail: err.detail })
      throw err
    }
  }

  return {
    reset,
    handle,
    show,
    findSnapshot,
    headSha: () => state.commits[0]?.sha ?? null,
    log: () => state.log,
    configure: (options) => {
      for (const key of ['push_fails', 'pull_fails', 'is_repo']) if (typeof options?.[key] === 'boolean') state.config[key] = options[key]
    },
  }
}

/** Paths this module answers. */
export function isGitPath(pathname) {
  return /^\/api\/(git|snapshots)(\/|$)/.test(pathname)
}
