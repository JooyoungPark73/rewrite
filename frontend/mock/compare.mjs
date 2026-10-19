// In-memory fake of the phase 5 blind A/B compare API, for mock/server.mjs.
//
// Follows the "Decisions fixed for implementation" and "API contract"
// sections of .agents/scratch/phase-5-ab-git-coverage.md closely enough to
// drive the compare dialog. Test scaffolding, written for readability:
// - "before" is the paragraph below `% @p <node_id>` in the file as it is at
//   the chosen ref (a snapshot's tag, or a commit sha) in the fake git repo
//   (mock/git.mjs), falling back to the given line range at that ref; "after"
//   is the same paragraph in the working tree. 400 when neither a node nor a
//   line range is given, or when the paragraph is not found at the ref.
// - The order is randomised per comparison (the mapping says which version
//   the model saw as A). The comparison carries a phase 4 run (pass
//   "ab-compare", scope kind "selection") registered through
//   runs.createExternalRun, so POST /api/runs/{id}/cancel stops it.
// - Timeline: queued at once, running after 300 ms, done after ~1.5 s
//   (configurable) with a verdict that prefers the working tree ("after") by
//   default and a canned reason. A cancelled run makes the comparison
//   "failed" with error "cancelled". `compare.finished` is broadcast at the
//   end, without the mapping.
// - GET /api/compare/{id} returns the mapping only with ?reveal=true (or 1)
//   and only once the comparison is done.
//
// Test-only hook (wired in server.mjs):
//   POST /__mock/compare-config  {duration_ms?, winner?: "after"|"before"|"tie", fail?: bool}

export class CompareApiError extends Error {
  constructor(status, detail) {
    super(detail)
    this.status = status
    this.detail = detail
  }
}

const BLANK = /^\s*$/
const ANY_MARKER = /^\s*%\s*@p\s+\S+/
const HEADING = /^\s*\\(section|subsection)\*?\{/
const COMMENT = /^\s*%/

const markerRe = (id) => new RegExp(`^\\s*%\\s*@p\\s+${id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*$`)

/**
 * The paragraph text in `text`: the lines under `% @p <nodeId>` up to the next
 * blank line, marker, heading or \end{document} (comment lines left out), or
 * else lines lineStart..lineEnd. Null when neither is found.
 */
export function paragraphIn(text, nodeId, lineStart, lineEnd) {
  if (text == null) return null
  const lines = text.split('\n')
  if (nodeId) {
    const at = lines.findIndex((l) => markerRe(nodeId).test(l))
    if (at >= 0) {
      const body = []
      for (let i = at + 1; i < lines.length; i++) {
        const t = lines[i]
        if (BLANK.test(t) || ANY_MARKER.test(t) || HEADING.test(t) || /\\end\{document\}/.test(t)) break
        if (!COMMENT.test(t)) body.push(t)
      }
      if (body.length) return body.join('\n')
    }
  }
  if (Number.isInteger(lineStart) && Number.isInteger(lineEnd) && lineStart >= 1 && lineEnd >= lineStart && lineEnd <= lines.length) {
    return lines.slice(lineStart - 1, lineEnd).join('\n')
  }
  return null
}

const REASONS = {
  win: (w, l) =>
    `Version ${w} states the paragraph's local message in its first sentence and every later sentence supports it; ` +
    `version ${l} opens with background and only reaches the point at the end, so its job in the argument is easy to miss.`,
  tie: () => 'Both versions deliver the local message equally well; their differences are wording only and do not change how well the paragraph does its job.',
}

/**
 * ctx: { files(), spec(), git (mock/git.mjs API), runs (mock/runs.mjs API),
 *        broadcast(event), sendJson(res, status, body), readBody(req) }
 */
export function createCompareApi(ctx) {
  let state

  function reset() {
    state = { comparisons: [], texts: new Map(), seq: 0, config: { duration_ms: 1500, winner: 'after', fail: false }, log: [] }
  }
  reset()

  const now = () => Date.now() / 1000
  /** What list endpoints and events show: never the mapping. */
  const blind = (c) => ({ ...c, mapping: null })

  /** The "before" text for a request; throws CompareApiError(400/404). */
  function beforeText(body) {
    const before = body.before ?? {}
    if (before.kind !== 'snapshot' && before.kind !== 'sha') throw new CompareApiError(400, 'before.kind must be "snapshot" or "sha"')
    let ref = String(before.ref ?? '')
    if (before.kind === 'snapshot') {
      const snap = ctx.git.findSnapshot(ref)
      if (!snap) throw new CompareApiError(400, `No snapshot "${ref}"`)
      ref = snap.sha
    }
    let text
    try {
      text = ctx.git.show(ref, body.file)
    } catch (err) {
      throw new CompareApiError(400, `${body.file} cannot be read at ${before.ref}: ${err.detail ?? err.message}`)
    }
    const paragraph = paragraphIn(text, body.node_id, body.line_start, body.line_end)
    if (paragraph == null) {
      throw new CompareApiError(400, `The paragraph ${body.node_id ? `"% @p ${body.node_id}"` : 'at those lines'} is not in ${body.file} at ${before.ref}`)
    }
    return paragraph
  }

  function start(body) {
    if (!body || typeof body.file !== 'string' || !body.file) throw new CompareApiError(400, 'file is required')
    const hasRange = Number.isInteger(body.line_start) && Number.isInteger(body.line_end)
    if (!body.node_id && !hasRange) throw new CompareApiError(400, 'Give node_id or line_start and line_end')
    const before = beforeText(body)
    const after = paragraphIn(ctx.files()[body.file], body.node_id, body.line_start, body.line_end)
    if (after == null) throw new CompareApiError(400, `The paragraph is not in the working tree's ${body.file}`)

    const id = `cmp-${++state.seq}`
    const mapping = Math.random() < 0.5 ? { A: 'before', B: 'after' } : { A: 'after', B: 'before' }
    const comparison = {
      id,
      file: body.file,
      node_id: body.node_id ?? null,
      line_start: hasRange ? body.line_start : null,
      line_end: hasRange ? body.line_end : null,
      before: { kind: body.before.kind, ref: String(body.before.ref) },
      run_id: null,
      status: 'queued',
      verdict: null,
      reason: null,
      mapping,
      error: null,
      created_at: now(),
      finished_at: null,
    }
    state.comparisons.unshift(comparison)
    state.texts.set(id, { before, after })

    const versionA = mapping.A === 'before' ? before : after
    const versionB = mapping.B === 'before' ? before : after
    const prompt = [
      '## Pass: blind comparison',
      `Scope: ${body.file} (${body.node_id ?? `lines ${body.line_start}-${body.line_end}`})`,
      '',
      '### Version A',
      versionA,
      '',
      '### Version B',
      versionB,
      '',
      'Return one JSON object and nothing else: {"verdict": "A" | "B" | "tie", "reason": "..."}',
    ].join('\n')

    const finishWith = (fields) => {
      Object.assign(comparison, { finished_at: now(), ...fields })
      ctx.broadcast({ type: 'compare.finished', comparison: blind(comparison) })
    }
    const run = ctx.runs.createExternalRun({
      scope: { kind: 'selection', ref: body.node_id ?? null, file: body.file, line_start: body.line_start ?? null, line_end: body.line_end ?? null },
      scope_name: `A/B ${body.node_id ?? body.file} vs ${comparison.before.ref}`,
      runner: body.runner || 'claude',
      model: body.model || 'sonnet',
      thinking: body.thinking || 'medium',
      prompt,
      // Called once when the run ends; a cancel arrives here too.
      onEnd: (status) => {
        if (status === 'cancelled' && comparison.status !== 'done') finishWith({ status: 'failed', error: 'cancelled' })
      },
    })
    comparison.run_id = run.run().id

    const { duration_ms, winner, fail } = state.config
    run.later(() => {
      comparison.status = 'running'
      run.start()
    }, 300)
    run.later(() => {
      if (fail) {
        const raw = 'I think the second one is somewhat better overall.'
        run.end('failed', raw, { error: 'The answer is not {"verdict", "reason"}' })
        finishWith({ status: 'failed', error: `The model's answer is not {"verdict": "A"|"B"|"tie", "reason": ...}: ${raw}` })
        return
      }
      const verdict = winner === 'tie' ? 'tie' : mapping.A === winner ? 'A' : 'B'
      const loser = verdict === 'A' ? 'B' : 'A'
      const reason = verdict === 'tie' ? REASONS.tie() : REASONS.win(verdict, loser)
      run.end('done', JSON.stringify({ verdict, reason }), { input_tokens: Math.round(prompt.length / 4), output_tokens: 87, cost_usd: 0.0041 })
      finishWith({ status: 'done', verdict, reason })
    }, Math.max(400, duration_ms))
    return comparison
  }

  /** Answer one /api/compare request. */
  async function handle(req, res, url) {
    const route = `${req.method} ${url.pathname}`
    const body = req.method === 'GET' ? null : await ctx.readBody(req)
    if (req.method !== 'GET') state.log.push({ method: req.method, path: url.pathname, body })
    const send = (status, payload) => ctx.sendJson(res, status, payload)
    try {
      if (route === 'POST /api/compare') return send(200, { comparison: blind(start(body)) })
      if (route === 'GET /api/compare') {
        const limit = Number(url.searchParams.get('limit') ?? 50) || 50
        return send(200, { comparisons: state.comparisons.slice(0, limit).map(blind) })
      }
      const one = /^\/api\/compare\/([^/]+)$/.exec(url.pathname)
      if (req.method === 'GET' && one) {
        const comparison = state.comparisons.find((c) => c.id === decodeURIComponent(one[1]))
        if (!comparison) throw new CompareApiError(404, `No comparison ${one[1]}`)
        const reveal = ['true', '1', 'yes'].includes(String(url.searchParams.get('reveal')).toLowerCase())
        const texts = state.texts.get(comparison.id)
        const detail = await ctx.runs.runDetail(comparison.run_id)
        return send(200, {
          comparison: reveal && comparison.status === 'done' ? { ...comparison } : blind(comparison),
          before_text: texts.before,
          after_text: texts.after,
          run: detail,
        })
      }
      return send(404, { detail: `Mock has no route for ${route}` })
    } catch (err) {
      if (err instanceof CompareApiError) return send(err.status, { detail: err.detail })
      throw err
    }
  }

  return {
    reset,
    handle,
    log: () => state.log,
    configure: (options) => {
      if (Number.isFinite(options?.duration_ms)) state.config.duration_ms = options.duration_ms
      if (['after', 'before', 'tie'].includes(options?.winner)) state.config.winner = options.winner
      if (typeof options?.fail === 'boolean') state.config.fail = options.fail
    },
  }
}

/** Paths this module answers. */
export function isComparePath(pathname) {
  return /^\/api\/compare(\/|$)/.test(pathname)
}
