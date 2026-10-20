// In-memory version of the phase 8 API (Revise):
//   POST /api/findings/comment {file, line_start, line_end, quote, problem, why?, json_ref?}
//        -> {finding}: a finding of a new "author" run (pass "comment", done).
//   POST /api/revisions {finding_id, span?, runner?, model?, thinking?} -> {revision}; done after
//        `duration_ms` (default 400; POST /__mock/revise-config) with a canned rewrite.
//   GET  /api/revisions?finding_id=  GET /api/revisions/outstanding  GET /api/revisions/{id}
//   POST /api/revisions/{id}/apply {text?} -> {revision, file, line_start, line_end, outstanding}
//        edits the fake file like the backend: "% aigen-was: <original>" above, the span
//        replaced by \aigen{...} per paragraph (removed when the text is empty), the macro
//        block added to main.tex once, the finding marked addressed.
// ctx: { files(), setFile(path, text), runs (mock/runs.mjs: createExternalRun, addFinding,
//        findingById, setFindingState, runDetail), broadcast, sendJson, readBody }
export function createReviseApi(ctx) {
  let state
  function reset() {
    state = { revisions: new Map(), order: [], seq: 0, durationMs: 400, log: [] }
  }
  reset()
  const now = () => Date.now() / 1000
  const collapse = (t) => t.replace(/\s+/g, ' ').trim()

  function err(status, detail) {
    const e = new Error(detail)
    e.status = status
    e.detail = detail
    return e
  }

  /** (start, end) offsets of `needle` in `haystack`, whitespace collapsed; null when absent. */
  function findCollapsed(haystack, needle) {
    const want = collapse(needle)
    if (!want) return null
    const positions = []
    let collapsed = ''
    let prevSpace = true
    for (let i = 0; i < haystack.length; i++) {
      const ch = haystack[i]
      if (/\s/.test(ch)) {
        if (!prevSpace) { collapsed += ' '; positions.push(i) }
        prevSpace = true
      } else { collapsed += ch; positions.push(i); prevSpace = false }
    }
    const lead = collapsed.length - collapsed.trimStart().length
    const at = collapsed.trim().indexOf(want)
    if (at < 0) return null
    return [positions[at + lead], positions[at + lead + want.length - 1] + 1]
  }

  function comment(body) {
    const file = String(body?.file ?? '')
    const text = ctx.files()[file]
    if (text === undefined) throw err(404, `file not found: ${file}`)
    if (!String(body?.problem ?? '').trim()) throw err(400, 'the comment is empty')
    const a = Number(body.line_start), b = Number(body.line_end)
    if (!(a >= 1) || !(b >= a)) throw err(400, `not a line range: ${body.line_start}-${body.line_end}`)
    const run = ctx.runs.createExternalRun({
      pass: 'comment', level: body.json_ref ? 'paragraph' : 'sentence',
      scope: { kind: 'selection', ref: null, file, line_start: a, line_end: b },
      scope_name: `comment on ${file}:${a}${b !== a ? `-${b}` : ''}`,
      runner: 'author', model: null, thinking: null, prompt: '', onEnd: () => {},
    })
    run.start()
    const finding = ctx.runs.addFinding(run.run().id, {
      file, line_start: a, line_end: b, quote: collapse(String(body.quote ?? '')), level: body.json_ref ? 'paragraph' : 'sentence',
      category: 'comment', severity: body.severity ?? 'major', problem: String(body.problem).trim(), why: String(body.why ?? '').trim(),
      json_ref: body.json_ref ?? null,
    })
    run.end('done', '', { input_tokens: 0, output_tokens: 0, cost_usd: null })
    ctx.broadcast({ type: 'findings.updated', run_id: run.run().id, file })
    return finding
  }

  /** The span a rewrite replaces, as the file reads now. */
  function locateSpan(finding, span) {
    const text = ctx.files()[finding.file]
    if (text === undefined) throw err(404, `file not found: ${finding.file}`)
    const lines = text.split('\n')
    let a = finding.anchor_line_start, b = finding.anchor_line_end
    if (span === 'paragraph') {
      // Expand to the blank-line / marker / heading delimited paragraph.
      a = finding.anchor_line_start; b = finding.anchor_line_end
      const stop = (l) => !l.trim() || /^\s*%\s*@p\s/.test(l) || /^\s*\\(sub)*section/.test(l)
      while (a > 1 && !stop(lines[a - 2])) a--
      while (b < lines.length && !stop(lines[b])) b++
      const original = lines.slice(a - 1, b).join('\n')
      return { file: finding.file, line_start: a, line_end: b, char_start: null, char_end: null, original }
    }
    const joined = lines.slice(a - 1, b).join('\n')
    const hit = findCollapsed(joined, finding.quote)
    if (!hit) return { file: finding.file, line_start: a, line_end: b, char_start: null, char_end: null, original: joined }
    return { file: finding.file, line_start: a, line_end: b, char_start: hit[0], char_end: hit[1], original: joined.slice(hit[0], hit[1]) }
  }

  function start(body) {
    const finding = ctx.runs.findingById(String(body?.finding_id ?? ''))
    if (!finding) throw err(404, `unknown finding "${body?.finding_id}"`)
    const span = body.span ?? 'quote'
    if (span !== 'quote' && span !== 'paragraph') throw err(400, `span must be quote or paragraph`)
    if ([...state.revisions.values()].some((r) => r.finding_id === finding.id && (r.status === 'queued' || r.status === 'running'))) {
      throw err(409, `a rewrite of ${finding.id} is already active`)
    }
    const runner = body.runner ?? 'claude'
    if (runner !== 'claude' && runner !== 'codex') throw err(400, `unknown runner "${runner}"`)
    const located = locateSpan(finding, span)
    const revision = {
      id: `rev${++state.seq}`, finding_id: finding.id, run_id: null, status: 'queued', span, file: located.file,
      line_start: located.line_start, line_end: located.line_end, original: located.original, text: null, notes: null, error: null,
      created_at: now(), finished_at: null, applied_at: null, applied_file: null, applied_line_start: null, applied_line_end: null,
    }
    const finishWith = (fields) => {
      Object.assign(revision, { finished_at: now(), ...fields })
      ctx.broadcast({ type: 'revision.finished', revision: { ...revision } })
    }
    const proposal = span === 'paragraph'
      ? `Rewritten paragraph after “${finding.problem}”.\n\nSecond part of the rewritten paragraph~\\cite{nobody}.`
      : `Rewritten span after “${finding.problem}”~\\cite{nobody}.`
    const run = ctx.runs.createExternalRun({
      pass: 'draft-rewrite', level: 'sentence',
      scope: { kind: 'selection', ref: null, file: located.file, line_start: located.line_start, line_end: located.line_end },
      scope_name: `rewrite in ${located.file}:${located.line_start}`, runner, model: body.model ?? null, thinking: body.thinking ?? 'medium',
      prompt: `# Drafting from the spec\n\n## Rewrite\n\n${finding.problem}\n\n${located.original}\n`,
      onEnd: (status) => { if (status === 'cancelled' && revision.status !== 'done') finishWith({ status: 'failed', error: 'cancelled' }) },
    })
    revision.run_id = run.run().id
    state.revisions.set(revision.id, revision)
    state.order.unshift(revision.id)
    setTimeout(() => {
      if (revision.status !== 'queued') return
      revision.status = 'running'
      run.start()
      setTimeout(() => {
        if (revision.status !== 'running') return
        run.end('done', '```latex\n' + proposal + '\n```\nNotes: changed only the span.\n', { input_tokens: 700, output_tokens: 40, cost_usd: 0.002 })
        finishWith({ status: 'done', text: proposal, notes: 'changed only the span.' })
      }, state.durationMs)
    }, 50)
    return revision
  }

  const MACRO = '\\providecommand{\\aigen}[1]{{\\textcolor{red}{[~AIGEN:~#1~]}}}'
  function ensurePreamble() {
    const files = ctx.files()
    if (Object.values(files).some((t) => /\\(?:new|provide|renew)command\s*\{?\\aigen\b/.test(t))) return
    const main = files['main.tex'] ?? ''
    const lines = main.split('\n')
    const at = lines.findIndex((l) => l.includes('\\begin{document}'))
    const block = ['% rewrite: text a model proposed on a comment, awaiting co-author approval.', '\\usepackage{xcolor}', MACRO, '% \\renewcommand{\\aigen}[1]{#1}', '']
    if (at < 0) return
    ctx.setFile('main.tex', [...lines.slice(0, at), ...block, ...lines.slice(at)].join('\n'))
  }
  const wrap = (text) => text.trim().split(/\n\s*\n/).filter((p) => p.trim()).map((p) => `\\aigen{${p.trim()}}`).join('\n\n')
  function outstanding() {
    const by_file = {}
    for (const [path, text] of Object.entries(ctx.files())) {
      if (!path.endsWith('.tex')) continue
      const n = text.split('\n').map((l) => l.replace(/(^|[^\\])%.*$/, '$1')).join('\n').split('\\aigen{').length - 1
      if (n) by_file[path] = n
    }
    return { count: Object.values(by_file).reduce((a, b) => a + b, 0), by_file }
  }

  function apply(id, body) {
    const revision = state.revisions.get(id)
    if (!revision) throw err(404, `unknown revision "${id}"`)
    if (revision.status !== 'done' || revision.text === null) throw err(409, `revision ${id} is ${revision.status}`)
    if (revision.applied_at) throw err(409, `revision ${id} was already applied`)
    const finding = ctx.runs.findingById(revision.finding_id)
    if (!finding) throw err(404, 'finding gone')
    const span = locateSpan(finding, revision.span)
    if (collapse(span.original) !== collapse(revision.original)) throw err(409, 'the text changed since this rewrite was proposed; propose it again')
    const newText = String(body?.text ?? revision.text).replace(/^\n+|\n+$/g, '')
    const lines = ctx.files()[span.file].split('\n')
    const joined = lines.slice(span.line_start - 1, span.line_end).join('\n')
    const wasLine = `% aigen-was: ${collapse(span.original)}`
    let newJoined
    if (span.char_start !== null) newJoined = joined.slice(0, span.char_start) + (newText.trim() ? wrap(newText) : '') + joined.slice(span.char_end)
    else newJoined = newText.trim() ? wrap(newText) : ''
    const block = [wasLine, ...(newJoined ? newJoined.split('\n') : [])]
    lines.splice(span.line_start - 1, span.line_end - span.line_start + 1, ...block)
    ctx.setFile(span.file, lines.join('\n'))
    if (newText.trim()) ensurePreamble()
    ctx.runs.setFindingState(finding.id, 'addressed')
    Object.assign(revision, { applied_at: now(), applied_file: span.file, applied_line_start: span.line_start + 1, applied_line_end: span.line_start + Math.max(1, block.length - 1) })
    ctx.broadcast({ type: 'file.changed', path: span.file })
    ctx.broadcast({ type: 'findings.updated', run_id: null, file: span.file })
    return { revision: { ...revision }, file: span.file, line_start: revision.applied_line_start, line_end: revision.applied_line_end, outstanding: outstanding().count }
  }

  return {
    reset,
    configure: ({ duration_ms } = {}) => { if (Number.isInteger(duration_ms)) state.durationMs = duration_ms },
    log: () => state.log,
    async handle(req, res, url) {
      const route = `${req.method} ${url.pathname}`
      const send = (status, body) => ctx.sendJson(res, status, body)
      try {
        if (route === 'POST /api/findings/comment') {
          const body = (await ctx.readBody(req)) ?? {}
          state.log.push({ method: 'POST', path: url.pathname, body })
          return send(200, { finding: comment(body) })
        }
        if (route === 'POST /api/revisions') {
          const body = (await ctx.readBody(req)) ?? {}
          state.log.push({ method: 'POST', path: url.pathname, body })
          return send(200, { revision: { ...start(body) } })
        }
        if (route === 'GET /api/revisions') {
          const fid = url.searchParams.get('finding_id')
          return send(200, { revisions: state.order.map((id) => state.revisions.get(id)).filter((r) => !fid || r.finding_id === fid).map((r) => ({ ...r })) })
        }
        if (route === 'GET /api/revisions/outstanding') return send(200, outstanding())
        const m = url.pathname.match(/^\/api\/revisions\/([^/]+)(\/apply)?$/)
        if (m && req.method === 'GET' && !m[2]) {
          const r = state.revisions.get(m[1])
          if (!r) return send(404, { detail: `unknown revision "${m[1]}"` })
          return send(200, { revision: { ...r }, run: r.run_id ? ctx.runs.runDetail(r.run_id) : null, finding: ctx.runs.findingById(r.finding_id) })
        }
        if (m && req.method === 'POST' && m[2]) {
          const body = (await ctx.readBody(req)) ?? {}
          state.log.push({ method: 'POST', path: url.pathname, body })
          return send(200, apply(m[1], body))
        }
        return send(404, { detail: 'Not Found' })
      } catch (e) {
        if (e && e.status) return send(e.status, { detail: e.detail })
        throw e
      }
    },
  }
}

export function isRevisePath(pathname) {
  return pathname === '/api/findings/comment' || pathname === '/api/revisions' || pathname.startsWith('/api/revisions/')
}
