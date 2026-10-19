// In-memory version of the phase 4 runs API, for mock/server.mjs.
//
// Follows the "Decisions fixed for implementation" and "API contract"
// sections of .agents/scratch/phase-4-pass-runner.md closely enough to drive
// the UI and its e2e tests. Test scaffolding, written for readability:
// - The nine default passes of PLAN.md with their levels and default scopes.
// - Runners: claude is "installed", codex is not (matching GET /api/health).
// - A live run is fake: it streams a status line and a few text deltas as
//   `run.event` over ~1.5 s (configurable), then finishes with the same model
//   output every time: 3 valid findings (critical, major, minor; one with a
//   json_ref pointing at the spec's first section, or "message") and 1 that the
//   validator rejects with reason "rewrite_text". Tokens, cost and duration are
//   made up.
// - The cache key is (pass, assembled prompt, paper.json, runner, model,
//   thinking): re-running an unchanged scope answers at once with a new run of
//   status "cached" whose findings are copies (states included).
// - Paste-back parses the last ```json block, else the first "{" to the last
//   "}", and validates exactly like a live run (unknown keys, rewrite text,
//   praise).
// - Anchoring: whitespace-collapsed exact quote search, nearest to line_start,
//   else the model's lines ("unverified"). When a .tex file changes (PUT
//   /api/file or a marker insert), open findings in it are searched again:
//   same lines -> unchanged, other lines -> "moved", not found -> "stale"; then
//   `findings.updated` is broadcast.
//
// Test-only hooks (wired in server.mjs):
//   GET  /__mock/runs-log      every write request to /api/runs* and /api/findings*
//   POST /__mock/runs-config   {duration_ms}: how long a fake run takes
//
// Phase 5: `createExternalRun` lets mock/compare.mjs register the run that
// carries a blind A/B comparison (pass "ab-compare", scope kind "selection"),
// so it shows in GET /api/runs and POST /api/runs/{id}/cancel stops it exactly
// like a review run. The compare module drives its timeline and is told when
// the run ends (`onEnd`), including a cancel.
//
// Phase 6 (run groups and triage live in mock/groups.mjs, which uses the
// helpers exported at the bottom):
// - Passes carry `kind`: "review" for the nine review passes, "compare" for
//   ab-compare and "triage" for the new triage pass. POST /api/runs and the
//   preview refuse a pass whose kind is not "review" (400).
// - codex can be switched "installed" with POST /__mock/runs-config
//   {codex_available: true} (it is not, by default, as before). A codex run
//   answers with ONE finding, on the same quote as claude's intro.tex:2 finding
//   (so "Run both" merges into one agreement plus claude's two other findings),
//   and reports no cost.
// - `createMember` starts a run for a group member: a fixed runner, an
//   optional canned output (fan-out members only see their section's
//   findings), its own duration, and a scope key per runner (so the two
//   members of "Run both" do not refuse each other with 409).
// - Scope kind "ends" (abstract + first + last linked section) for the
//   fan-out's message-drift member.
// - Every finding has `triage_verdict` / `triage_reason` (null until triaged).

const PASSES = [
  ['message-drift', 'message', 'paper', 'Abstract, introduction and conclusion checked against the message: is this the real point?'],
  ['triage', 'paragraph', 'paper', "Which of one run's findings are wrong or trivial (started from a run, phase 6)."],
  ['reviewer-2', 'message', 'paper', 'A skeptical program-committee member lists weaknesses only.'],
  ['claims-evidence', 'claim', 'paper', "Where each claim's evidence is, and overclaiming against its scope."],
  ['section-promises', 'section', 'paper', 'Does each section deliver its promise; is the figure story coherent?'],
  ['paragraph-function', 'paragraph', 'section', 'One job per paragraph, a topic sentence, and paragraphs that should move.'],
  ['ab-compare', 'paragraph', 'selection', 'Blind comparison of two versions of a paragraph (phase 5).'],
  ['clarity', 'sentence', 'section', 'Characters as subjects, actions as verbs, nominalizations, passives, hedges, filler.'],
  ['epistemic-calibration', 'sentence', 'section', 'Sentences stronger or weaker than their evidence.'],
  ['latex-hygiene', 'sentence', 'paper', 'Undefined refs, missing bib keys, notation inconsistency.'],
].map(([name, level, scope, description]) => ({
  name,
  level,
  scope,
  allow_rewrites: name === 'latex-hygiene',
  model: null,
  thinking: null,
  file: `.rewrite/prompts/passes/${name}.md`,
  description,
  error: null,
  kind: name === 'ab-compare' ? 'compare' : name === 'triage' ? 'triage' : 'review',
  runnable: name !== 'ab-compare' && name !== 'triage',
}))

/** GET /api/runners as the mock starts; `runnersInfo()` applies the codex switch. */
const RUNNERS = {
  runners: [
    {
      name: 'claude',
      available: true,
      models: ['sonnet', 'opus', 'haiku'],
      suppress_global: true,
      auth: { logged_in: true, method: 'claude.ai', detail: 'max subscription' },
      warning: null,
    },
    {
      name: 'codex',
      available: false,
      models: ['gpt-5.5', 'gpt-5.5-mini'],
      suppress_global: true,
      auth: { logged_in: true, method: 'ChatGPT', detail: null },
      warning: null,
    },
  ],
  thinking_levels: ['low', 'medium', 'high', 'xhigh', 'max'],
  defaults: {
    message: { runner: 'claude', model: 'opus', thinking: 'high' },
    claim: { runner: 'claude', model: 'opus', thinking: 'high' },
    section: { runner: 'claude', model: 'opus', thinking: 'high' },
    paragraph: { runner: 'claude', model: 'sonnet', thinking: 'medium' },
    sentence: { runner: 'claude', model: null, thinking: 'low' },
  },
  global_instructions: [
    { runner: 'claude', path: '~/.claude/CLAUDE.md', present: true },
    { runner: 'codex', path: '~/.codex/AGENTS.md', present: false },
  ],
}

const SYSTEM_PROMPT = [
  'You are screening a submitted draft for problems before it goes to reviewers.',
  'Report problems only. Never praise, never rewrite the text, never suggest replacement wording.',
].join('\n')

const FINDING_KEYS = ['file', 'line_start', 'line_end', 'quote', 'level', 'category', 'severity', 'problem', 'why', 'json_ref']
const SEVERITIES = ['minor', 'major', 'critical']

/** The fake model's answer: 3 valid findings and 1 with rewrite text. */
export function fakeModelFindings(jsonRef) {
  return [
    {
      file: 'sections/intro.tex',
      line_start: 2,
      line_end: 2,
      quote: 'Mock papers are useful for testing user interfaces.',
      level: 'paragraph',
      category: 'function',
      severity: 'major',
      problem: 'The paragraph never says what job it does in the argument; it reads as a loose remark.',
      why: 'The introduction must set up the problem before the reader meets the method.',
      json_ref: jsonRef,
    },
    {
      file: 'sections/intro.tex',
      line_start: 3,
      line_end: 3,
      quote: 'This sentence cites a missing key~\\cite{nobody}.',
      level: 'claim',
      category: 'evidence',
      severity: 'critical',
      problem: 'The claim rests on a citation that does not resolve to any reference.',
      why: 'An unsupported claim in the introduction weakens the thesis it is meant to carry.',
      json_ref: null,
    },
    {
      file: 'sections/method.tex',
      line_start: 3,
      line_end: 3,
      quote: 'It has several steps.',
      level: 'sentence',
      category: 'vagueness',
      severity: 'minor',
      problem: '"Several steps" names neither the number nor the steps.',
      why: 'A vague quantifier leaves the reader unable to follow the method.',
      json_ref: null,
    },
    {
      file: 'sections/method.tex',
      line_start: 2,
      line_end: 2,
      quote: 'We describe the method here.',
      level: 'sentence',
      category: 'clarity',
      severity: 'minor',
      problem:
        'The opening is empty. Replace with "We present a three-step method that restores snapshot pages in the order they are needed."',
      why: 'The first sentence should carry the section promise.',
      json_ref: null,
    },
  ]
}

/** The fake codex answer: one finding, on the same sentence as claude's first one. */
export function fakeCodexFindings(jsonRef) {
  return [
    {
      file: 'sections/intro.tex',
      line_start: 2,
      line_end: 2,
      quote: 'Mock papers are useful for testing user interfaces.',
      level: 'paragraph',
      category: 'topic-sentence',
      severity: 'minor',
      problem: 'The opening sentence states a general remark instead of the paragraph topic.',
      why: 'Readers use the first sentence to decide what the paragraph is for.',
      json_ref: jsonRef,
    },
  ]
}

export class RunsApiError extends Error {
  constructor(status, detail) {
    super(detail)
    this.status = status
    this.detail = detail
  }
}

// ---------------------------------------------------------------------------
// Validation and parsing (shared by live runs and paste-back)
// ---------------------------------------------------------------------------

/** True when text contains a quoted replacement of >= 8 words ("replace with ..."). */
function hasRewriteText(text) {
  if (typeof text !== 'string') return false
  const re = /(replace with|rewrite as|change to|instead write|should read|→)\s*:?\s*["“']([^"”']+)["”']/gi
  for (const m of text.matchAll(re)) if (m[2].trim().split(/\s+/).length >= 8) return true
  return false
}

const PRAISE = /^(good|great|nice|well|excellent|clear|strong)\b/i

/** Split the model's findings into valid ones and rejected ones ({index, reason, raw}). */
export function validateFindings(list, allowRewrites) {
  const valid = []
  const rejected = []
  list.forEach((raw, index) => {
    const reject = (reason) => rejected.push({ index, reason, raw })
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return reject('not_an_object')
    const unknown = Object.keys(raw).find((k) => !FINDING_KEYS.includes(k))
    if (unknown) return reject(`unknown_field:${unknown}`)
    const missing = FINDING_KEYS.find((k) => !(k in raw) && k !== 'json_ref')
    if (missing) return reject(`missing_field:${missing}`)
    if (!SEVERITIES.includes(raw.severity)) return reject('bad_severity')
    if (!Number.isInteger(raw.line_start) || !Number.isInteger(raw.line_end)) return reject('bad_lines')
    if (!allowRewrites && (hasRewriteText(raw.problem) || hasRewriteText(raw.why))) return reject('rewrite_text')
    if (typeof raw.problem !== 'string' || !raw.problem.trim() || PRAISE.test(raw.problem.trim())) return reject('praise')
    if (typeof raw.category !== 'string' || !raw.category.trim()) return reject('missing_field:category')
    valid.push({ ...raw, json_ref: raw.json_ref ?? null })
  })
  return { valid, rejected }
}

/** The model's text -> its findings list; throws Error when no JSON object is found. */
export function parseModelOutput(text) {
  const fences = [...String(text).matchAll(/```json\s*\n([\s\S]*?)```/g)]
  let body
  if (fences.length) body = fences[fences.length - 1][1]
  else {
    const first = text.indexOf('{')
    const last = text.lastIndexOf('}')
    if (first < 0 || last <= first) throw new Error('No JSON object found in the output')
    body = text.slice(first, last + 1)
  }
  const parsed = JSON.parse(body)
  if (!parsed || !Array.isArray(parsed.findings)) throw new Error('The JSON has no "findings" list')
  return parsed.findings
}

// ---------------------------------------------------------------------------
// Anchoring
// ---------------------------------------------------------------------------

/** Lines of `quote` in `text` (whitespace-collapsed match nearest to line `near`), or null. */
export function findQuote(text, quote, near) {
  const needle = String(quote ?? '').replace(/\s+/g, ' ').trim()
  if (!needle) return null
  let hay = ''
  const lineOf = []
  let line = 1
  let space = true
  for (const ch of text) {
    if (/\s/.test(ch)) {
      if (!space) {
        hay += ' '
        lineOf.push(line)
      }
      space = true
    } else {
      hay += ch
      lineOf.push(line)
      space = false
    }
    if (ch === '\n') line++
  }
  let best = null
  for (let i = hay.indexOf(needle); i >= 0; i = hay.indexOf(needle, i + 1)) {
    const hit = { start: lineOf[i], end: lineOf[i + needle.length - 1] }
    if (!best || Math.abs(hit.start - near) < Math.abs(best.start - near)) best = hit
  }
  return best
}

// ---------------------------------------------------------------------------
// The API
// ---------------------------------------------------------------------------

/**
 * ctx: { files(): {path: text}, spec(): parsed paper.json or null, specRaw(): string,
 *        broadcast(event), sendJson(res, status, body), readBody(req) }
 */
export function createRunsApi(ctx) {
  let state

  function reset() {
    for (const rec of state?.records.values() ?? []) rec.timers.forEach(clearTimeout)
    state = { records: new Map(), order: [], cache: new Map(), seq: 0, findingSeq: 0, durationMs: 1500, log: [], codexAvailable: false }
  }

  /** GET /api/runners, with codex switched on or off by POST /__mock/runs-config. */
  function runnersInfo() {
    return {
      ...RUNNERS,
      // Like the backend: no login check (auth null) for a CLI that is not installed.
      runners: RUNNERS.runners.map((r) =>
        r.name === 'codex'
          ? { ...r, available: state.codexAvailable, auth: state.codexAvailable ? r.auth : null }
          : { ...r },
      ),
    }
  }
  reset()

  // ---- scope and prompt ----------------------------------------------------

  const numbered = (text, from = 1, to = Infinity) =>
    text
      .split('\n')
      .map((line, i) => ({ n: i + 1, line }))
      .filter(({ n }) => n >= from && n <= to)
      .map(({ n, line }) => `${String(n).padStart(4)} | ${line}`)
      .join('\n')

  const headingOf = (marker) => /\{(.*)\}/.exec(marker)?.[1] ?? marker

  /** Normalize a request scope and assemble its source; throws RunsApiError(400). */
  function resolveScope(input) {
    const files = ctx.files()
    const kind = input?.kind
    const scope = {
      kind,
      ref: input?.ref ?? null,
      file: input?.file ?? null,
      line_start: input?.line_start ?? null,
      line_end: input?.line_end ?? null,
    }
    if (kind === 'paper') {
      const source = Object.keys(files)
        .filter((p) => p.endsWith('.tex'))
        .map((p) => `=== file: ${p} ===\n${numbered(files[p])}`)
        .join('\n\n')
      return { scope, scope_name: 'the whole paper', source }
    }
    if (kind === 'section') {
      const node = (ctx.spec()?.tree.children ?? []).find((n) => n.id === scope.ref)
      if (!node) throw new RunsApiError(400, `No section "${scope.ref}" in paper.json`)
      if (!node.anchor) throw new RunsApiError(400, `Section "${scope.ref}" is planned (not anchored in the source)`)
      const text = files[node.anchor.file]
      const lines = (text ?? '').split('\n')
      const start = lines.findIndex((l) => l.trim() === node.anchor.marker.trim())
      if (text == null || start < 0) throw new RunsApiError(400, `Section "${scope.ref}" is orphaned (marker not found)`)
      let end = lines.findIndex((l, i) => i > start && /^\s*\\section\b/.test(l))
      if (end < 0) end = lines.length
      return {
        scope,
        scope_name: `Section: ${headingOf(node.anchor.marker)}`,
        source: `=== file: ${node.anchor.file} ===\n${numbered(text, start + 1, end)}`,
      }
    }
    if (kind === 'file') {
      if (!(scope.file in files)) throw new RunsApiError(400, `No such file: ${scope.file}`)
      return { scope, scope_name: scope.file, source: `=== file: ${scope.file} ===\n${numbered(files[scope.file])}` }
    }
    if (kind === 'ends') {
      // Phase 6: the abstract (none in the mock's main.tex) + the first and the
      // last linked section, each with its file header.
      const sections = (ctx.spec()?.tree.children ?? []).filter((n) => n.anchor && files[n.anchor.file] != null)
      if (sections.length === 0) throw new RunsApiError(400, 'Scope "ends" needs at least one anchored section')
      const ends = sections.length === 1 ? [sections[0]] : [sections[0], sections[sections.length - 1]]
      const source = ends
        .map((n) => {
          const text = files[n.anchor.file]
          const lines = text.split('\n')
          const start = Math.max(0, lines.findIndex((l) => l.trim() === n.anchor.marker.trim()))
          let end = lines.findIndex((l, i) => i > start && /^\s*\\section\b/.test(l))
          if (end < 0) end = lines.length
          return `=== file: ${n.anchor.file} ===\n${numbered(text, start + 1, end)}`
        })
        .join('\n\n')
      return { scope: { kind, ref: null, file: null, line_start: null, line_end: null }, scope_name: 'Abstract, first and last section', source }
    }
    if (kind === 'selection') {
      const text = files[scope.file]
      const { line_start: a, line_end: b } = scope
      if (text == null) throw new RunsApiError(400, `No such file: ${scope.file}`)
      if (!Number.isInteger(a) || !Number.isInteger(b) || a < 1 || b < a) {
        throw new RunsApiError(400, 'A selection needs 1 <= line_start <= line_end')
      }
      return {
        scope,
        scope_name: `${scope.file}:${a}–${b}`,
        source: `=== file: ${scope.file} (selection; 3 lines of context around it) ===\n${numbered(text, a - 3, b + 3)}`,
      }
    }
    throw new RunsApiError(400, `Unknown scope kind: ${kind}`)
  }

  function specOutline() {
    const spec = ctx.spec()
    if (!spec) return '(paper.json is invalid)'
    const out = [`message: ${spec.message.says || '(empty)'}`]
    for (const section of spec.tree.children) {
      out.push(`  section ${section.id}: ${section.says || '(empty)'}`)
      for (const p of section.children) out.push(`    paragraph ${p.id}: ${p.says || '(empty)'}`)
    }
    return out.join('\n')
  }

  /** Everything a run (or a preview) needs; throws RunsApiError for a bad request. */
  function plan(body, { forPaste = false } = {}) {
    const pass = PASSES.find((p) => p.name === body?.pass)
    if (!pass) throw new RunsApiError(400, `Unknown pass: ${body?.pass}`)
    if (pass.error) throw new RunsApiError(400, `Pass ${pass.name} has a broken front matter: ${pass.error}`)
    if (pass.kind !== 'review') throw new RunsApiError(400, `Pass ${pass.name} is a ${pass.kind} pass and cannot be run on its own`)
    const resolved = resolveScope(body.scope)
    const defaults = RUNNERS.defaults[pass.level] ?? { runner: 'claude', model: null, thinking: 'medium' }
    const runner = forPaste ? 'paste' : (body.runner || defaults.runner)
    if (!forPaste) {
      const info = runnersInfo().runners.find((r) => r.name === runner)
      if (!info) throw new RunsApiError(400, `Unknown runner: ${runner}`)
      if (!info.available) throw new RunsApiError(400, `${runner} is not installed on this machine`)
    }
    // A pasted answer came from some web UI: model and thinking are unknown.
    // Like the real backend, the level's model applies only on the level's runner.
    const model = forPaste ? null : body.model || (runner === defaults.runner ? pass.model || defaults.model : null) || null
    const thinking = forPaste ? null : body.thinking || pass.thinking || defaults.thinking || null
    const prompt = [
      SYSTEM_PROMPT,
      '',
      `## Pass: ${pass.name}`,
      `Scope: ${resolved.scope_name}`,
      '',
      pass.description,
      ...(body.fanout ? ['', 'This is one section of a longer paper; the full outline is above.'] : []),
      '',
      '### Spec outline',
      specOutline(),
      '',
      '### Source (line numbers on the left)',
      resolved.source,
      '',
      '### Output',
      'Return one JSON object and nothing else: {"findings": [{"file", "line_start", "line_end", "quote", "level", "category", "severity", "problem", "why", "json_ref"}]}',
      '',
    ].join('\n')
    const command =
      runner === 'codex'
        ? ['codex', 'exec', '--json', '--sandbox', 'read-only', '--skip-git-repo-check', '--ephemeral', ...(model ? ['-m', model] : []), '-c', `model_reasoning_effort=${thinking}`]
        : runner === 'claude'
          ? ['claude', '-p', '--output-format', 'stream-json', '--verbose', '--tools', '', '--no-session-persistence', '--setting-sources', 'project', ...(model ? ['--model', model] : []), '--effort', thinking]
          : []
    const cacheKey = JSON.stringify([pass.name, prompt, ctx.specRaw(), runner, model, thinking])
    const scopeKey = `${pass.name}:${resolved.scope.kind}:${resolved.scope.ref ?? resolved.scope.file}`
    return { pass, ...resolved, runner, model, thinking, prompt, command, cacheKey, scopeKey }
  }

  // ---- runs and findings ---------------------------------------------------

  const now = () => Date.now() / 1000

  function newRecord(p, status) {
    const id = `run-${++state.seq}`
    const run = {
      id,
      pass: p.pass.name,
      level: p.pass.level,
      scope: p.scope,
      scope_name: p.scope_name,
      runner: p.runner,
      model: p.model,
      thinking: p.thinking,
      status,
      cached_from: null,
      git_sha: 'a1b2c3d4e5f6',
      started_at: now(),
      finished_at: null,
      duration_ms: null,
      input_tokens: null,
      output_tokens: null,
      cost_usd: null,
      error: null,
      findings_count: 0,
      rejected_count: 0,
    }
    const rec = { run, prompt: p.prompt, command: p.command, raw_output: null, findings: [], rejected: [], timers: [], cacheKey: p.cacheKey, scopeKey: p.scopeKey, allowRewrites: p.pass.allow_rewrites, onStart: null, onEnd: null }
    state.records.set(id, rec)
    state.order.unshift(id)
    return rec
  }

  /** Validate and anchor the model's findings, store them on the record. */
  function applyOutput(rec, list) {
    const files = ctx.files()
    const { valid, rejected } = validateFindings(list, rec.allowRewrites)
    rec.rejected = rejected
    rec.findings = valid.map((f) => {
      const hit = files[f.file] != null ? findQuote(files[f.file], f.quote, f.line_start) : null
      return {
        id: `f-${++state.findingSeq}`,
        run_id: rec.run.id,
        ...f,
        state: 'open',
        anchor_state: hit ? 'ok' : 'unverified',
        anchor_line_start: hit ? hit.start : f.line_start,
        anchor_line_end: hit ? hit.end : f.line_end,
        created_at: now(),
        triage_verdict: null,
        triage_reason: null,
      }
    })
    rec.run.findings_count = rec.findings.length
    rec.run.rejected_count = rec.rejected.length
  }

  function finish(rec, status, fields = {}) {
    rec.timers.forEach(clearTimeout)
    rec.timers = []
    Object.assign(rec.run, { status, finished_at: now(), duration_ms: Math.round((now() - rec.run.started_at) * 1000), ...fields })
    const ok = status === 'done' || status === 'cached'
    ctx.broadcast({ type: ok ? 'run.finished' : 'run.failed', run: { ...rec.run } })
    if (ok) ctx.broadcast({ type: 'findings.updated', run_id: rec.run.id, file: null })
    // Phase 5: tell the module that owns an external run (cancel included).
    rec.onEnd?.(status)
  }

  /**
   * Phase 5: a run whose timeline another mock module drives (the blind
   * compare). Returns helpers to mark it running and to end it; `onEnd(status)`
   * is called once when it ends, also when POST /api/runs/{id}/cancel ends it.
   */
  function createExternalRun({ pass = 'ab-compare', level = 'paragraph', scope, scope_name, runner, model, thinking, prompt, onEnd }) {
    const passInfo = PASSES.find((p) => p.name === pass) ?? { name: pass, level, allow_rewrites: false }
    const rec = newRecord(
      {
        pass: passInfo,
        scope,
        scope_name,
        runner,
        model,
        thinking,
        prompt,
        command: ['claude', '-p', '--output-format', 'stream-json', ...(model ? ['--model', model] : []), '--effort', thinking ?? 'medium'],
        cacheKey: null,
        scopeKey: `${pass}:${scope.kind}:${scope.file}:${scope.line_start ?? ''}`,
      },
      'queued',
    )
    rec.onEnd = onEnd
    return {
      run: () => ({ ...rec.run }),
      /** Add a timer that a cancel clears. */
      later: (fn, ms) => rec.timers.push(setTimeout(fn, ms)),
      start: () => {
        rec.run.status = 'running'
        ctx.broadcast({ type: 'run.started', run: { ...rec.run } })
      },
      end: (status, rawOutput, fields = {}) => {
        if (!isActive(rec)) return
        rec.raw_output = rawOutput
        finish(rec, status, fields)
      },
      isActive: () => isActive(rec),
    }
  }

  /** The canned answer of a runner (claude: 3 valid + 1 rejected; codex: 1). */
  function outputFor(runner) {
    const firstSection = ctx.spec()?.tree.children[0]?.id ?? 'message'
    return runner === 'codex' ? fakeCodexFindings(firstSection) : fakeModelFindings(firstSection)
  }

  /**
   * The fake process: a status line, a few text deltas, then the canned output.
   * Phase 6: a group member may bring its own `output` and `durationMs`.
   */
  function startFake(rec, { output = outputFor(rec.run.runner), durationMs = state.durationMs } = {}) {
    const total = Math.max(200, durationMs)
    const raw = `I screened the draft against the spec outline.\n\`\`\`json\n${JSON.stringify({ findings: output }, null, 2)}\n\`\`\`\n`
    const events = [
      { kind: 'status', text: `${rec.run.runner} started (model ${rec.run.model ?? 'default'}, thinking ${rec.run.thinking})` },
      { kind: 'text', text: 'I screened the draft against the spec outline.\n' },
      { kind: 'text', text: '```json\n{"findings": [\n' },
      { kind: 'text', text: '  {"file": "sections/intro.tex", "severity": "major", ...},\n' },
      { kind: 'text', text: '  {"file": "sections/intro.tex", "severity": "critical", ...},\n' },
      { kind: 'text', text: '  ...\n]}\n```\n' },
    ]
    rec.timers.push(
      setTimeout(() => {
        rec.run.status = 'running'
        ctx.broadcast({ type: 'run.started', run: { ...rec.run } })
        rec.onStart?.()
      }, 30),
    )
    events.forEach((e, i) => {
      rec.timers.push(
        setTimeout(() => ctx.broadcast({ type: 'run.event', run_id: rec.run.id, ...e }), 60 + ((total - 120) * i) / events.length),
      )
    })
    rec.timers.push(
      setTimeout(() => {
        rec.raw_output = raw
        applyOutput(rec, output)
        state.cache.set(rec.cacheKey, rec.run.id)
        finish(rec, 'done', {
          input_tokens: Math.round(rec.prompt.length / 4),
          output_tokens: 321,
          // codex reports no cost (phase 4 finding).
          cost_usd: rec.run.runner === 'codex' ? null : 0.0123,
        })
      }, total),
    )
  }

  function cachedCopy(p, original) {
    const rec = newRecord(p, 'cached')
    rec.raw_output = original.raw_output
    rec.rejected = original.rejected
    rec.findings = original.findings.map((f) => ({
      ...f,
      id: `f-${++state.findingSeq}`,
      run_id: rec.run.id,
      triage_verdict: null,
      triage_reason: null,
    }))
    Object.assign(rec.run, {
      cached_from: original.run.id,
      findings_count: rec.findings.length,
      rejected_count: rec.rejected.length,
      input_tokens: 0,
      output_tokens: 0,
      cost_usd: 0,
    })
    finish(rec, 'cached', { duration_ms: 0 })
    return rec
  }

  const isActive = (rec) => rec.run.status === 'queued' || rec.run.status === 'running'
  const allFindings = () => state.order.flatMap((id) => state.records.get(id).findings)

  /** A .tex file changed: re-anchor its open findings; announce if anything moved. */
  function fileChanged(path) {
    const text = ctx.files()[path]
    if (text == null) return
    let changed = false
    for (const f of allFindings()) {
      if (f.file !== path || f.state !== 'open') continue
      const hit = findQuote(text, f.quote, f.anchor_line_start)
      if (hit) {
        if (hit.start !== f.anchor_line_start || hit.end !== f.anchor_line_end) {
          Object.assign(f, { anchor_state: 'moved', anchor_line_start: hit.start, anchor_line_end: hit.end })
          changed = true
        } else if (f.anchor_state === 'stale' || f.anchor_state === 'unverified') {
          f.anchor_state = 'ok'
          changed = true
        }
      } else if (f.anchor_state !== 'stale' && f.anchor_state !== 'unverified') {
        f.anchor_state = 'stale'
        changed = true
      }
    }
    if (changed) ctx.broadcast({ type: 'findings.updated', run_id: null, file: path })
  }

  /** Answer one /api/passes, /api/runners, /api/runs* or /api/findings* request. */
  async function handle(req, res, url) {
    const route = `${req.method} ${url.pathname}`
    const body = req.method === 'GET' ? null : await ctx.readBody(req)
    if (req.method !== 'GET') state.log.push({ method: req.method, path: url.pathname, body })
    const send = (status, payload) => ctx.sendJson(res, status, payload)
    try {
      if (route === 'GET /api/passes') return send(200, { passes: PASSES })
      if (route === 'GET /api/runners') return send(200, runnersInfo())

      if (route === 'POST /api/runs/preview') {
        const p = plan(body)
        return send(200, {
          prompt: p.prompt,
          scope_name: p.scope_name,
          cache_hit: state.cache.has(p.cacheKey),
          model: p.model,
          thinking: p.thinking,
          command: p.command,
        })
      }

      if (route === 'POST /api/runs') {
        const p = plan(body)
        const active = state.order.map((id) => state.records.get(id)).find((r) => isActive(r) && r.scopeKey === p.scopeKey)
        if (active) throw new RunsApiError(409, `${p.pass.name} is already running on ${p.scope_name} (${active.run.id})`)
        const cachedId = body.force ? null : state.cache.get(p.cacheKey)
        if (cachedId) return send(200, { run: { ...cachedCopy(p, state.records.get(cachedId)).run } })
        const rec = newRecord(p, 'queued')
        startFake(rec)
        return send(200, { run: { ...rec.run } })
      }

      if (route === 'POST /api/runs/paste') {
        const p = plan(body, { forPaste: true })
        const rec = newRecord(p, 'running')
        rec.raw_output = String(body.raw ?? '')
        let list
        try {
          list = parseModelOutput(rec.raw_output)
        } catch (err) {
          finish(rec, 'failed', { error: `Could not parse the pasted output: ${err.message}` })
          return send(200, { run: { ...rec.run } })
        }
        applyOutput(rec, list)
        finish(rec, 'done')
        return send(200, { run: { ...rec.run } })
      }

      if (route === 'GET /api/runs') {
        const limit = Number(url.searchParams.get('limit') ?? 50) || 50
        return send(200, { runs: state.order.slice(0, limit).map((id) => ({ ...state.records.get(id).run })) })
      }

      const runMatch = /^\/api\/runs\/([^/]+)(\/prompt|\/cancel)?$/.exec(url.pathname)
      if (runMatch) {
        const rec = state.records.get(decodeURIComponent(runMatch[1]))
        if (!rec) throw new RunsApiError(404, `No run ${runMatch[1]}`)
        if (req.method === 'GET' && !runMatch[2]) {
          return send(200, { run: { ...rec.run }, findings: rec.findings, rejected: rec.rejected, raw_output: rec.raw_output })
        }
        if (req.method === 'GET' && runMatch[2] === '/prompt') return send(200, { prompt: rec.prompt, command: rec.command })
        if (req.method === 'POST' && runMatch[2] === '/cancel') {
          if (isActive(rec)) finish(rec, 'cancelled')
          return send(200, { run: { ...rec.run } })
        }
      }

      if (route === 'GET /api/findings') {
        const file = url.searchParams.get('file')
        const st = url.searchParams.get('state')
        const runId = url.searchParams.get('run_id')
        const findings = allFindings().filter(
          (f) => (!file || f.file === file) && (!st || f.state === st) && (!runId || f.run_id === runId),
        )
        return send(200, { findings })
      }

      const findingMatch = /^\/api\/findings\/([^/]+)$/.exec(url.pathname)
      if (req.method === 'PATCH' && findingMatch) {
        const f = allFindings().find((x) => x.id === decodeURIComponent(findingMatch[1]))
        if (!f) throw new RunsApiError(404, `No finding ${findingMatch[1]}`)
        if (!['open', 'addressed', 'dismissed'].includes(body?.state)) throw new RunsApiError(400, `Bad state: ${body?.state}`)
        f.state = body.state
        ctx.broadcast({ type: 'findings.updated', run_id: f.run_id, file: f.file })
        return send(200, { finding: f })
      }

      return send(404, { detail: `Mock has no route for ${route}` })
    } catch (err) {
      if (err instanceof RunsApiError) return send(err.status, { detail: err.detail })
      throw err
    }
  }

  /**
   * Phase 6: plan one member run of a group. `body` is a POST /api/runs body
   * (runner set by the group, `fanout: true` for fan-out members). A cache hit
   * (unless body.force) gives a finished "cached" run at once and `start` is
   * null; otherwise the run is created "queued" and `start()` begins the fake
   * process (the group calls it when one of its 3 slots is free).
   * Throws RunsApiError (400) like POST /api/runs.
   */
  function createMember(body, { output, durationMs, scopeKeySuffix = '' } = {}) {
    const p = plan(body)
    const cachedId = body.force ? null : state.cache.get(p.cacheKey)
    if (cachedId) return { rec: cachedCopy(p, state.records.get(cachedId)), start: null }
    const rec = newRecord(p, 'queued')
    rec.scopeKey = p.scopeKey + scopeKeySuffix
    return { rec, start: () => startFake(rec, { output: output ?? outputFor(rec.run.runner), durationMs: durationMs ?? state.durationMs }) }
  }

  /** Phase 6: end an active run as "cancelled" (group cancel). */
  function cancel(rec) {
    if (isActive(rec)) finish(rec, 'cancelled')
  }

  return {
    reset,
    handle,
    fileChanged,
    createExternalRun,
    /** Phase 5: a copy of run `id` (for GET /api/compare/{id}), or null. */
    runDetail: (id) => (id && state.records.has(id) ? { ...state.records.get(id).run } : null),
    log: () => state.log,
    configure: ({ duration_ms, codex_available }) => {
      if (Number.isFinite(duration_ms) && duration_ms >= 200) state.durationMs = duration_ms
      if (typeof codex_available === 'boolean') state.codexAvailable = codex_available
    },
    // Phase 6 helpers for mock/groups.mjs.
    createMember,
    cancel,
    outputFor,
    runners: runnersInfo,
    passes: () => PASSES,
    /** The internal record of run `id` (its findings array is live), or undefined. */
    record: (id) => state.records.get(id),
    isActive,
    durationMs: () => state.durationMs,
    broadcast: (event) => ctx.broadcast(event),
  }
}

/** Paths this module answers. */
export function isRunsPath(pathname) {
  return /^\/api\/(passes|runners|runs|findings)(\/|$)/.test(pathname)
}
