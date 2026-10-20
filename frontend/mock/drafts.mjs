// In-memory version of the phase 7 API (drafts from the spec):
//   POST /api/drafts {node_id, runner?, model?, thinking?}  -> {draft} (queued; done after
//        `duration_ms`, default 400, set with POST /__mock/drafts-config)
//   GET  /api/drafts?node_id=&limit=                         -> {drafts} newest first
//   GET  /api/drafts/{id}                                    -> {draft, run}
//   POST /api/drafts/{id}/apply {mode, file?, line?, text?}  -> {draft, file, line_start, line_end}
// The fake answer is "Drafted: <says>" as one LaTeX sentence (plus a Notes line). `apply`
// edits the fake files the way the real backend does: replace the paragraph under the node's
// marker (comment line + text), or insert marker + comment + text before `line`.
//
// ctx: { files(), setFile(path, text), spec(), runs (mock/runs.mjs), broadcast, sendJson, readBody }
export function createDraftsApi(ctx) {
  let state
  function reset() {
    state = { drafts: new Map(), order: [], seq: 0, durationMs: 400, log: [] }
  }
  reset()
  const now = () => Date.now() / 1000

  function node(spec, id) {
    for (const s of spec.tree.children) {
      if (s.id === id) return { node: s, parent: spec.tree }
      for (const p of s.children) if (p.id === id) return { node: p, parent: s }
    }
    return null
  }

  function start(body) {
    const spec = ctx.spec()
    const id = String(body?.node_id ?? '').trim()
    if (!id) throw err(400, 'node_id is empty')
    const found = spec ? node(spec, id) : null
    if (!found) throw err(404, `unknown node "${id}"`)
    if (found.node.level !== 'paragraph') throw err(400, `${id} is a ${found.node.level}; drafts target paragraphs`)
    if ([...state.drafts.values()].some((d) => d.node_id === id && (d.status === 'queued' || d.status === 'running'))) {
      throw err(409, `a draft of ${id} is already active; wait for it or cancel its run`)
    }
    const runner = body.runner ?? 'claude'
    if (runner !== 'claude' && runner !== 'codex') throw err(400, `unknown runner "${runner}"`)
    const draft = {
      id: `drf${++state.seq}`,
      node_id: id,
      run_id: null,
      status: 'queued',
      text: null,
      notes: null,
      error: null,
      created_at: now(),
      finished_at: null,
      applied_at: null,
      applied_mode: null,
      applied_file: null,
      applied_line_start: null,
      applied_line_end: null,
    }
    const finishWith = (fields) => {
      Object.assign(draft, { finished_at: now(), ...fields })
      ctx.broadcast({ type: 'draft.finished', draft: { ...draft } })
    }
    const says = found.node.says || `paragraph ${id}`
    const text = `Drafted: ${says.replace(/[.]+$/, '')}, as \\textsc{Mock} shows~\\cite{nobody}.`
    const prompt = `# Drafting from the spec\n\n## Draft: paragraph ${id}\n\nsays: ${says}\n`
    const run = ctx.runs.createExternalRun({
      pass: 'draft-paragraph',
      level: 'paragraph',
      scope: { kind: 'selection', ref: null, file: found.node.anchor?.file ?? null, line_start: null, line_end: null },
      scope_name: `paragraph ${id}`,
      runner,
      model: body.model ?? null,
      thinking: body.thinking ?? 'medium',
      prompt,
      onEnd: (status) => {
        if (status === 'cancelled' && draft.status !== 'done') finishWith({ status: 'failed', error: 'cancelled' })
      },
    })
    draft.run_id = run.run().id
    state.drafts.set(draft.id, draft)
    state.order.unshift(draft.id)
    setTimeout(() => {
      if (draft.status !== 'queued') return
      draft.status = 'running'
      run.start?.()
      setTimeout(() => {
        if (draft.status !== 'running') return
        run.end('done', '```latex\n' + text + '\n```\nNotes: nothing left open.\n', { input_tokens: 900, output_tokens: 60, cost_usd: 0.0031 })
        finishWith({ status: 'done', text, notes: 'nothing left open.' })
      }, state.durationMs)
    }, 50)
    return draft
  }

  function apply(id, body) {
    const draft = state.drafts.get(id)
    if (!draft) throw err(404, `unknown draft "${id}"`)
    if (draft.status !== 'done' || !draft.text) throw err(409, `draft ${id} is ${draft.status}; only a finished draft can be applied`)
    if (draft.applied_at) throw err(409, `draft ${id} was already applied (${draft.applied_mode})`)
    const text = (typeof body?.text === 'string' ? body.text : draft.text).replace(/^\n+|\n+$/g, '')
    if (!text.trim()) throw err(400, 'the text to insert is empty')
    const files = ctx.files()
    const comment = `% drafted by claude, draft ${id}: review before keeping`
    const newLines = text.split('\n')
    const markerRe = new RegExp(`^\\s*%\\s*@p\\s+${draft.node_id}\\s*$`)
    let file = null
    let markerIdx = -1
    for (const [path, content] of Object.entries(files)) {
      const i = content.split('\n').findIndex((l) => markerRe.test(l))
      if (i >= 0) { file = path; markerIdx = i; break }
    }
    let lineStart
    if (body?.mode === 'replace') {
      if (file === null) throw err(400, `${draft.node_id} has no marker in the source; use insert with a file and line`)
      const lines = files[file].split('\n')
      let last = markerIdx + 1
      while (last < lines.length && lines[last].trim() && !/^\s*%\s*@p\s/.test(lines[last]) && !/^\s*\\(sub)*section/.test(lines[last])) last++
      const updated = [...lines.slice(0, markerIdx + 1), comment, ...newLines, ...lines.slice(last)]
      ctx.setFile(file, updated.join('\n'))
      lineStart = markerIdx + 3
    } else if (body?.mode === 'insert') {
      if (file !== null) throw err(400, `${draft.node_id} already has a marker in ${file}; use replace`)
      if (typeof body.file !== 'string' || !Number.isInteger(body.line) || body.line < 1) throw err(400, 'insert needs a file and a 1-based line')
      const content = files[body.file]
      if (content === undefined) throw err(404, `file not found: ${body.file}`)
      const lines = content.split('\n')
      const at = Math.min(body.line - 1, lines.length)
      let block = [`% @p ${draft.node_id}`, comment, ...newLines, '']
      if (at > 0 && lines[at - 1].trim()) block = ['', ...block]
      ctx.setFile(body.file, [...lines.slice(0, at), ...block, ...lines.slice(at)].join('\n'))
      file = body.file
      lineStart = at + (block[0] === '' ? 1 : 0) + 3
    } else throw err(400, 'mode must be replace or insert')
    Object.assign(draft, { applied_at: now(), applied_mode: body.mode, applied_file: file, applied_line_start: lineStart, applied_line_end: lineStart + newLines.length - 1 })
    ctx.broadcast({ type: 'file.changed', path: file })
    return { draft: { ...draft }, file, line_start: lineStart, line_end: lineStart + newLines.length - 1 }
  }

  function err(status, detail) {
    const e = new Error(detail)
    e.status = status
    e.detail = detail
    return e
  }

  return {
    reset,
    configure: ({ duration_ms } = {}) => {
      if (Number.isInteger(duration_ms)) state.durationMs = duration_ms
    },
    log: () => state.log,
    async handle(req, res, url) {
      const route = `${req.method} ${url.pathname}`
      const send = (status, body) => ctx.sendJson(res, status, body)
      try {
        if (route === 'POST /api/drafts') {
          const body = (await ctx.readBody(req)) ?? {}
          state.log.push({ method: 'POST', path: url.pathname, body })
          return send(200, { draft: { ...start(body) } })
        }
        if (route === 'GET /api/drafts') {
          const nodeId = url.searchParams.get('node_id')
          const limit = Number(url.searchParams.get('limit') ?? 50)
          const drafts = state.order.map((id) => state.drafts.get(id)).filter((d) => !nodeId || d.node_id === nodeId).slice(0, limit)
          return send(200, { drafts: drafts.map((d) => ({ ...d })) })
        }
        const m = url.pathname.match(/^\/api\/drafts\/([^/]+)(\/apply)?$/)
        if (m && req.method === 'GET' && !m[2]) {
          const draft = state.drafts.get(m[1])
          if (!draft) return send(404, { detail: `unknown draft "${m[1]}"` })
          return send(200, { draft: { ...draft }, run: draft.run_id ? ctx.runs.runDetail(draft.run_id) : null })
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

export function isDraftsPath(pathname) {
  return pathname === '/api/drafts' || pathname.startsWith('/api/drafts/')
}
