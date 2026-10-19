// In-memory version of the phase 6 API (run groups, cross-runner merge,
// triage), for mock/server.mjs.
//
// Follows the "Decisions fixed for implementation" and "API contract"
// sections of .agents/scratch/phase-6-polish.md closely enough to drive the UI
// and its e2e tests. Test scaffolding, written for readability. Member runs
// are ordinary mock runs (mock/runs.mjs `createMember`), so they show in
// GET /api/runs, stream `run.*` events, use the cache and can be cancelled one
// by one.
//
// - POST /api/groups/both {pass, scope, claude?, codex?, force?}: one claude
//   and one codex run on the same pass and scope (400 unless both runners are
//   installed; codex is switched on with POST /__mock/runs-config
//   {codex_available: true}). codex takes 1.5 times as long as claude, so the
//   group shows 1/2 then 2/2. The fake codex answer shares one quote with claude's, so the
//   merged list is one agreement (2 runners) followed by claude's two other
//   findings.
// - POST /api/groups/fanout {pass, runner?, model?, thinking?, force?}: one
//   section run per linked section (`fanout: true` in the prompt; the canned
//   findings are cut down to that section's file) plus one message-drift run
//   over scope "ends" (one message-level finding). At most 3 members run at
//   once; the others wait "queued". Member i of N takes 2 s x (i + 1) / N, so
//   they finish one after the other over ~2 s (POST /__mock/groups-config
//   {fanout_ms} changes the 2 s).
// - Merge rule: two findings of different runs agree when they are in the
//   same file and their anchor lines overlap, or when their whitespace-
//   collapsed quotes share a common substring of at least half the shorter
//   quote. Entries: most agreement first, then file (include order), then
//   line; severity = the highest. A fan-out lists every finding as its own
//   entry (agreement 1), in member order.
// - Group status: queued -> running -> done | partial | failed, or cancelled
//   by POST /api/groups/{id}/cancel. `group.updated` is broadcast on every
//   member start and end.
// - Triage: POST /api/runs/{id}/triage makes a "triage" run (pass "triage")
//   that finishes after ~1 s (POST /__mock/groups-config {triage_ms,
//   triage_fail}) and gives every open finding a verdict by severity:
//   critical -> keep, major -> wrong, minor -> trivial. Verdicts are stored on
//   the findings (state unchanged), then `findings.updated` and
//   `triage.finished` are broadcast. 409 while a triage of the run is active,
//   400 when the run has no open findings.
// - POST /api/findings/dismiss-flagged {run_id} dismisses the run's open
//   findings marked wrong or trivial.
//
// Test-only hooks (wired in server.mjs):
//   GET  /__mock/groups-log      every write request this module answered
//   POST /__mock/groups-config   {fanout_ms?, triage_ms?, triage_fail?}
import { RunsApiError } from './runs.mjs'

const SEVERITY_RANK = { critical: 3, major: 2, minor: 1 }

/** Verdict and reason the fake triage gives a finding (by severity). */
const VERDICT_BY_SEVERITY = {
  critical: ['keep', 'The cited key really is missing; the claim has no support in the text.'],
  major: ['wrong', 'The paragraph does state its job in the next sentence; the finding misreads it.'],
  minor: ['trivial', 'A vague quantifier in a transition sentence; harmless for the argument.'],
}

/** Whitespace-collapsed text. */
const squash = (s) => String(s ?? '').replace(/\s+/g, ' ').trim()

/** Length of the longest common substring of a and b (dynamic programming). */
function longestCommon(a, b) {
  let best = 0
  let prev = new Array(b.length + 1).fill(0)
  for (let i = 1; i <= a.length; i++) {
    const row = new Array(b.length + 1).fill(0)
    for (let j = 1; j <= b.length; j++) {
      if (a[i - 1] === b[j - 1]) {
        row[j] = prev[j - 1] + 1
        if (row[j] > best) best = row[j]
      }
    }
    prev = row
  }
  return best
}

/** The phase 6 merge rule for two findings of different runs. */
function agree(a, b) {
  if (a.run_id === b.run_id || a.file !== b.file) return false
  if (a.anchor_line_start <= b.anchor_line_end && b.anchor_line_start <= a.anchor_line_end) return true
  const qa = squash(a.quote)
  const qb = squash(b.quote)
  const shorter = Math.min(qa.length, qb.length)
  return shorter > 0 && longestCommon(qa, qb) >= shorter / 2
}

/**
 * ctx: { runs: the runs module, linkedSections(): [section node], fileOrder(): [path],
 *        broadcast(event), sendJson(res, status, body), readBody(req) }
 */
export function createGroupsApi(ctx) {
  let state

  // Timers live on the member / triage runs (mock/runs.mjs), which its own reset clears.
  function reset() {
    state = { groups: new Map(), order: [], triages: [], seq: 0, triageSeq: 0, fanoutMs: 2000, triageMs: 1000, triageFail: false, log: [] }
  }
  reset()

  const now = () => Date.now() / 1000

  // ---- groups ----------------------------------------------------------------

  /** Findings of the members that have results, merged as the contract says. */
  function merged(g) {
    const order = ctx.fileOrder()
    const fileRank = (p) => {
      const i = order.indexOf(p)
      return i < 0 ? order.length : i
    }
    const memberIndex = new Map(g.members.map((rec, i) => [rec.run.id, i]))
    const findings = g.members
      .filter((rec) => rec.run.status === 'done' || rec.run.status === 'cached')
      .flatMap((rec) => rec.findings.map((f) => ({ ...f })))

    const entryOf = (list) => {
      const sorted = [...list].sort((a, b) => memberIndex.get(a.run_id) - memberIndex.get(b.run_id))
      const runIds = [...new Set(sorted.map((f) => f.run_id))]
      const runners = [...new Set(runIds.map((id) => g.members[memberIndex.get(id)].run.runner))]
      const top = sorted.reduce((best, f) => (SEVERITY_RANK[f.severity] > SEVERITY_RANK[best.severity] ? f : best), sorted[0])
      return {
        key: `${sorted[0].file}:${sorted[0].anchor_line_start}:${sorted[0].id}`,
        file: sorted[0].file,
        line_start: Math.min(...sorted.map((f) => f.anchor_line_start)),
        line_end: Math.max(...sorted.map((f) => f.anchor_line_end)),
        severity: top.severity,
        level: sorted[0].level,
        category: sorted[0].category,
        agreement: runIds.length,
        runners,
        findings: sorted,
      }
    }

    if (g.group.kind === 'fanout') {
      return findings
        .sort(
          (a, b) =>
            memberIndex.get(a.run_id) - memberIndex.get(b.run_id) ||
            fileRank(a.file) - fileRank(b.file) ||
            a.anchor_line_start - b.anchor_line_start,
        )
        .map((f) => entryOf([f]))
    }

    // Union-find over pairs that agree.
    const parent = findings.map((_, i) => i)
    const root = (i) => (parent[i] === i ? i : (parent[i] = root(parent[i])))
    for (let i = 0; i < findings.length; i++) {
      for (let j = i + 1; j < findings.length; j++) if (agree(findings[i], findings[j])) parent[root(i)] = root(j)
    }
    const components = new Map()
    findings.forEach((f, i) => {
      const r = root(i)
      if (!components.has(r)) components.set(r, [])
      components.get(r).push(f)
    })
    return [...components.values()]
      .map(entryOf)
      .sort((a, b) => b.agreement - a.agreement || fileRank(a.file) - fileRank(b.file) || a.line_start - b.line_start)
  }

  /** Recompute the group's status, cost and counts from its members. */
  function refresh(g) {
    const statuses = g.members.map((rec) => rec.run.status)
    let status
    if (g.cancelled) status = 'cancelled'
    else if (statuses.every((s) => s === 'queued')) status = 'queued'
    else if (statuses.some((s) => s === 'queued' || s === 'running')) status = 'running'
    else if (statuses.every((s) => s === 'done' || s === 'cached')) status = 'done'
    else if (statuses.every((s) => s === 'failed')) status = 'failed'
    else status = 'partial'
    const costs = g.members.map((rec) => rec.run.cost_usd).filter((c) => c != null)
    const entries = merged(g)
    Object.assign(g.group, {
      status,
      finished_at: ['queued', 'running'].includes(status) ? null : (g.group.finished_at ?? now()),
      cost_usd: costs.length ? Math.round(costs.reduce((a, b) => a + b, 0) * 1e6) / 1e6 : null,
      findings_count: g.members.reduce((n, rec) => n + rec.run.findings_count, 0),
      agreements_count: g.group.kind === 'both' ? entries.filter((e) => e.agreement > 1).length : 0,
    })
  }

  function announce(g) {
    refresh(g)
    ctx.broadcast({ type: 'group.updated', group: { ...g.group } })
  }

  /** Start queued members while fewer than 3 of this group's members are running. */
  function pump(g) {
    while (g.pending.length > 0 && g.active < 3) {
      const next = g.pending.shift()
      next.rec.started = true
      g.active++
      next.start()
    }
  }

  /** Build a group from its planned members ({rec, start}). */
  function createGroup(kind, pass, scope, planned) {
    const id = `group-${++state.seq}`
    const g = {
      group: {
        id,
        kind,
        pass,
        scope,
        run_ids: planned.map((m) => m.rec.run.id),
        status: 'queued',
        created_at: now(),
        finished_at: null,
        cost_usd: null,
        findings_count: 0,
        agreements_count: 0,
      },
      members: planned.map((m) => m.rec),
      // Members waiting for one of the 3 slots (cache hits are finished already).
      pending: planned.filter((m) => m.start),
      active: 0,
      cancelled: false,
    }
    for (const m of planned) {
      if (!m.start) continue
      const rec = m.rec
      rec.onStart = () => announce(g)
      rec.onEnd = () => {
        // Also called when a member is cancelled before it got a slot.
        if (rec.started) g.active--
        g.pending = g.pending.filter((p) => p.rec !== rec)
        if (!g.cancelled) pump(g)
        announce(g)
      }
    }
    state.groups.set(id, g)
    state.order.unshift(id)
    refresh(g)
    pump(g)
    return g
  }

  function startBoth(body) {
    const runners = ctx.runs.runners().runners
    for (const name of ['claude', 'codex']) {
      if (!runners.find((r) => r.name === name)?.available) {
        throw new RunsApiError(400, `"Run both" needs claude and codex; ${name} is not installed on this machine`)
      }
    }
    const planned = ['claude', 'codex'].map((runner, i) => {
      const opts = body?.[runner] ?? {}
      return ctx.runs.createMember(
        { pass: body?.pass, scope: body?.scope, runner, model: opts.model, thinking: opts.thinking, force: !!body?.force },
        { durationMs: Math.round(ctx.runs.durationMs() * (1 + 0.5 * i)), scopeKeySuffix: `:${runner}` },
      )
    })
    return createGroup('both', body.pass, planned[0].rec.run.scope, planned)
  }

  function startFanout(body) {
    const sections = ctx.linkedSections()
    if (sections.length === 0) throw new RunsApiError(400, 'Fan-out needs at least one linked section in paper.json')
    const options = { runner: body?.runner, model: body?.model, thinking: body?.thinking, force: !!body?.force }
    const n = sections.length + 1
    const duration = (i) => Math.max(300, Math.round((state.fanoutMs * (i + 1)) / n))
    const planned = sections.map((node, i) => {
      const output = ctx.runs.outputFor(body?.runner || 'claude').filter((f) => f.file === node.anchor.file)
      return ctx.runs.createMember(
        { ...options, pass: body?.pass, scope: { kind: 'section', ref: node.id, file: null, line_start: null, line_end: null }, fanout: true },
        { output, durationMs: duration(i), scopeKeySuffix: ':fanout' },
      )
    })
    const drift = [
      {
        file: 'sections/intro.tex',
        line_start: 2,
        line_end: 2,
        quote: 'Mock papers are useful for testing user interfaces.',
        level: 'message',
        category: 'drift',
        severity: 'major',
        problem: 'The introduction opens on a side remark; the message (mocks help) arrives only in the spec.',
        why: 'Abstract, introduction and conclusion must carry the same point.',
        json_ref: 'message',
      },
    ]
    planned.push(
      ctx.runs.createMember(
        { ...options, pass: 'message-drift', scope: { kind: 'ends', ref: null, file: null, line_start: null, line_end: null }, fanout: true },
        { output: drift, durationMs: duration(sections.length), scopeKeySuffix: ':fanout' },
      ),
    )
    return createGroup('fanout', body.pass, { kind: 'paper', ref: null, file: null, line_start: null, line_end: null }, planned)
  }

  function cancelGroup(g) {
    if (!['queued', 'running'].includes(g.group.status)) return
    g.cancelled = true
    g.pending = []
    for (const rec of g.members) ctx.runs.cancel(rec)
    announce(g)
  }

  // ---- triage -------------------------------------------------------------------

  function startTriage(rec, body) {
    if (state.triages.some((t) => t.run_id === rec.run.id && (t.status === 'queued' || t.status === 'running'))) {
      throw new RunsApiError(409, `A triage of ${rec.run.id} is already running`)
    }
    const open = rec.findings.filter((f) => f.state === 'open')
    if (open.length === 0) throw new RunsApiError(400, `Run ${rec.run.id} has no open findings to triage`)
    const runner = body?.runner || 'claude'
    if (!ctx.runs.runners().runners.find((r) => r.name === runner)?.available) {
      throw new RunsApiError(400, `${runner} is not installed on this machine`)
    }
    const triage = {
      id: `triage-${++state.triageSeq}`,
      run_id: rec.run.id,
      triage_run_id: null,
      status: 'queued',
      error: null,
      counts: { keep: 0, wrong: 0, trivial: 0 },
      created_at: now(),
      finished_at: null,
    }
    state.triages.push(triage)
    const end = (status, error = null) => {
      if (triage.status === 'done' || triage.status === 'failed') return
      Object.assign(triage, { status, error, finished_at: now() })
      ctx.broadcast({ type: 'triage.finished', triage: { ...triage } })
    }
    const numbered = open.map((f, i) => `${i + 1}. ${f.file}:${f.line_start}-${f.line_end} "${f.quote}" ${f.problem}`).join('\n')
    const ext = ctx.runs.createExternalRun({
      pass: 'triage',
      level: 'paragraph',
      scope: rec.run.scope,
      scope_name: rec.run.scope_name,
      runner,
      model: body?.model || 'sonnet',
      thinking: body?.thinking || 'medium',
      prompt: `Which of these findings are wrong or trivial?\n${numbered}\n`,
      onEnd: (status) => {
        if (status !== 'done') end('failed', `The triage run ended ${status}`)
      },
    })
    triage.triage_run_id = ext.run().id
    ext.later(() => {
      triage.status = 'running'
      ext.start()
    }, 30)
    ext.later(() => {
      if (state.triageFail) {
        ext.end('failed', 'not json', { error: 'Could not parse the triage answer' })
        return
      }
      const verdicts = open.map((f, i) => {
        const [verdict, reason] = VERDICT_BY_SEVERITY[f.severity] ?? VERDICT_BY_SEVERITY.minor
        // The verdict is stored on the finding row; its state is not changed.
        Object.assign(f, { triage_verdict: verdict, triage_reason: reason, triage_id: triage.id })
        triage.counts[verdict]++
        return { index: i + 1, verdict, reason }
      })
      ext.end('done', JSON.stringify({ verdicts }, null, 2), { input_tokens: 2100, output_tokens: 140, cost_usd: 0.0042 })
      ctx.broadcast({ type: 'findings.updated', run_id: rec.run.id, file: null })
      end('done')
    }, state.triageMs)
    return triage
  }

  // ---- HTTP ----------------------------------------------------------------------

  async function handle(req, res, url) {
    const route = `${req.method} ${url.pathname}`
    const body = req.method === 'GET' ? null : await ctx.readBody(req)
    if (req.method !== 'GET') state.log.push({ method: req.method, path: url.pathname, body })
    const send = (status, payload) => ctx.sendJson(res, status, payload)
    try {
      if (route === 'POST /api/groups/both') return send(200, { group: { ...startBoth(body).group } })
      if (route === 'POST /api/groups/fanout') return send(200, { group: { ...startFanout(body).group } })
      if (route === 'GET /api/groups') {
        const limit = Number(url.searchParams.get('limit') ?? 50) || 50
        return send(200, { groups: state.order.slice(0, limit).map((id) => ({ ...state.groups.get(id).group })) })
      }
      const groupMatch = /^\/api\/groups\/([^/]+)(\/cancel)?$/.exec(url.pathname)
      if (groupMatch) {
        const g = state.groups.get(decodeURIComponent(groupMatch[1]))
        if (!g) throw new RunsApiError(404, `No group ${groupMatch[1]}`)
        if (req.method === 'GET' && !groupMatch[2]) {
          refresh(g)
          return send(200, { group: { ...g.group }, runs: g.members.map((rec) => ({ ...rec.run })), merged: merged(g) })
        }
        if (req.method === 'POST' && groupMatch[2]) {
          cancelGroup(g)
          return send(200, { group: { ...g.group } })
        }
      }

      const triageMatch = /^\/api\/runs\/([^/]+)\/triage$/.exec(url.pathname)
      if (triageMatch) {
        const rec = ctx.runs.record(decodeURIComponent(triageMatch[1]))
        if (!rec) throw new RunsApiError(404, `No run ${triageMatch[1]}`)
        if (req.method === 'POST') return send(200, { triage: { ...startTriage(rec, body) } })
        if (req.method === 'GET') {
          const latest = [...state.triages].reverse().find((t) => t.run_id === rec.run.id) ?? null
          return send(200, { triage: latest ? { ...latest } : null, findings: rec.findings })
        }
      }

      if (route === 'POST /api/findings/dismiss-flagged') {
        const rec = ctx.runs.record(body?.run_id)
        if (!rec) throw new RunsApiError(404, `No run ${body?.run_id}`)
        let updated = 0
        for (const f of rec.findings) {
          if (f.state === 'open' && (f.triage_verdict === 'wrong' || f.triage_verdict === 'trivial')) {
            f.state = 'dismissed'
            updated++
          }
        }
        if (updated) ctx.broadcast({ type: 'findings.updated', run_id: rec.run.id, file: null })
        return send(200, { updated })
      }

      return send(404, { detail: `Mock has no route for ${route}` })
    } catch (err) {
      if (err instanceof RunsApiError) return send(err.status, { detail: err.detail })
      throw err
    }
  }

  return {
    reset,
    handle,
    log: () => state.log,
    configure: ({ fanout_ms, triage_ms, triage_fail }) => {
      if (Number.isFinite(fanout_ms) && fanout_ms >= 300) state.fanoutMs = fanout_ms
      if (Number.isFinite(triage_ms) && triage_ms >= 100) state.triageMs = triage_ms
      if (typeof triage_fail === 'boolean') state.triageFail = triage_fail
    },
  }
}

/** Paths this module answers (checked before the runs module's). */
export function isGroupsPath(pathname) {
  return /^\/api\/groups(\/|$)/.test(pathname) || /^\/api\/runs\/[^/]+\/triage$/.test(pathname) || pathname === '/api/findings/dismiss-flagged'
}
