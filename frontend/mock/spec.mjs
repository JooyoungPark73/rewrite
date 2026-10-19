// In-memory version of the phase 3 spec API, for mock/server.mjs.
//
// Follows the "Decisions fixed for implementation" and "API contract"
// sections of .agents/scratch/phase-3-spec-editor.md closely enough to drive
// the UI and its e2e tests. It is test scaffolding, written for readability,
// not a second implementation to keep in sync byte for byte:
// - serialize() is sorted-key, 2-space JSON (the real backend additionally
//   puts each node of a `children`/`claims` array on its own line).
// - Status uses a small string hash of the paragraph text as the "stale"
//   baseline, kept in a plain object instead of state.db.
// - The skeleton parser knows \section, \subsection, blank lines, a few
//   environments and one-command lines such as \maketitle or \input{...}.
//
// Everything here is a pure function of (spec, files, baselines) except where
// noted; server.mjs owns the state and the HTTP/WebSocket plumbing.
//
// Phase 5 adds coverage(spec) for GET /api/spec/coverage (the claims x
// sections matrix): a cell counts the nodes of the section's subtree (the
// section itself plus its paragraphs) that link the claim.

/** Thrown for a request the real backend would refuse (status 400/404/409/422). */
export class SpecApiError extends Error {
  constructor(status, detail, errors = []) {
    super(detail)
    this.status = status
    this.detail = detail
    this.errors = errors
  }
}

export const LEVEL_FIELDS = {
  message: ['problem', 'stakes', 'thesis'],
  claim: ['evidence', 'scope'],
  section: ['figure_story'],
  paragraph: ['function'],
}

const blankFields = (level) => Object.fromEntries(LEVEL_FIELDS[level].map((k) => [k, '']))

function makeNode(id, level, says = '') {
  return { id, level, says, children: [], claims: [], fields: blankFields(level), anchor: null, notes: '' }
}

/** A fresh spec, like the one `rewrite init` writes. */
export function emptySpec() {
  const message = makeNode('message', 'message')
  const tree = { ...makeNode('message', 'message'), fields: {} }
  return { version: 1, message, claims: [], tree }
}

const clone = (value) => structuredClone(value)

// ---------------------------------------------------------------------------
// Serialize and validate
// ---------------------------------------------------------------------------

function sortKeys(value) {
  if (Array.isArray(value)) return value.map(sortKeys)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map((k) => [k, sortKeys(value[k])]))
  }
  return value
}

/** Deterministic text of paper.json: sorted keys, 2-space indent, trailing newline. */
export function serialize(spec) {
  return JSON.stringify(sortKeys(spec), null, 2) + '\n'
}

/** Fill in optional keys (notes, fields, ...) so a hand-written node validates. */
function normalizeNode(node) {
  if (!node || typeof node !== 'object') return node
  return {
    ...node,
    says: node.says ?? '',
    children: Array.isArray(node.children) ? node.children.map(normalizeNode) : node.children ?? [],
    claims: node.claims ?? [],
    fields: node.fields ?? {},
    anchor: node.anchor ?? null,
    notes: node.notes ?? '',
  }
}

export function normalize(spec) {
  if (!spec || typeof spec !== 'object') return spec
  return {
    ...spec,
    message: normalizeNode(spec.message),
    claims: Array.isArray(spec.claims) ? spec.claims.map(normalizeNode) : spec.claims,
    tree: normalizeNode(spec.tree),
  }
}

/** Validation errors as [{path, message}]; empty when the spec is valid. */
export function validate(spec) {
  const errors = []
  const err = (path, message) => errors.push({ path, message })
  if (!spec || typeof spec !== 'object' || Array.isArray(spec)) return [{ path: '', message: 'expected an object' }]
  if (spec.version !== 1) err('version', 'must be 1')

  const ids = new Map() // id -> path, to report duplicates
  const checkNode = (node, path, level) => {
    if (!node || typeof node !== 'object') return err(path, 'expected a node object')
    if (typeof node.id !== 'string' || !node.id) err(`${path}.id`, 'must be a non-empty string')
    if (node.level !== level) err(`${path}.level`, `must be "${level}", got ${JSON.stringify(node.level)}`)
    if (typeof node.says !== 'string') err(`${path}.says`, 'must be a string')
    if (typeof node.notes !== 'string') err(`${path}.notes`, 'must be a string')
    if (!Array.isArray(node.children)) err(`${path}.children`, 'must be a list')
    if (!Array.isArray(node.claims) || node.claims.some((c) => typeof c !== 'string')) {
      err(`${path}.claims`, 'must be a list of claim ids')
    }
    if (!node.fields || typeof node.fields !== 'object' || Object.values(node.fields).some((v) => typeof v !== 'string')) {
      err(`${path}.fields`, 'must map names to strings')
    }
    if (node.anchor !== null && (typeof node.anchor?.file !== 'string' || typeof node.anchor?.marker !== 'string')) {
      err(`${path}.anchor`, 'must be null or {file, marker}')
    }
  }
  const register = (node, path) => {
    if (typeof node?.id !== 'string') return
    if (ids.has(node.id)) err(`${path}.id`, `duplicate id "${node.id}" (also at ${ids.get(node.id)})`)
    else ids.set(node.id, path)
  }

  checkNode(spec.message, 'message', 'message')
  register(spec.message, 'message')
  if (Array.isArray(spec.message?.children) && spec.message.children.length) err('message.children', 'must be empty')

  if (!Array.isArray(spec.claims)) err('claims', 'must be a list')
  const claims = Array.isArray(spec.claims) ? spec.claims : []
  claims.forEach((c, i) => {
    checkNode(c, `claims[${i}]`, 'claim')
    register(c, `claims[${i}]`)
  })
  const claimIds = new Set(claims.map((c) => c?.id))

  checkNode(spec.tree, 'tree', 'message')
  if (spec.tree?.id !== spec.message?.id) err('tree.id', 'must equal message.id')
  const sections = Array.isArray(spec.tree?.children) ? spec.tree.children : []
  const checkRefs = (node, path) => {
    for (const ref of Array.isArray(node?.claims) ? node.claims : []) {
      if (!claimIds.has(ref)) err(`${path}.claims`, `unknown claim "${ref}"`)
    }
  }
  sections.forEach((s, i) => {
    const sp = `tree.children[${i}]`
    checkNode(s, sp, 'section')
    register(s, sp)
    checkRefs(s, sp)
    ;(Array.isArray(s?.children) ? s.children : []).forEach((p, j) => {
      const pp = `${sp}.children[${j}]`
      checkNode(p, pp, 'paragraph')
      register(p, pp)
      checkRefs(p, pp)
      if (Array.isArray(p?.children) && p.children.length) err(`${pp}.children`, 'a paragraph has no children')
    })
  })
  return errors
}

// ---------------------------------------------------------------------------
// Ids and lookups
// ---------------------------------------------------------------------------

function* allNodes(spec) {
  yield spec.message
  yield* spec.claims
  for (const s of spec.tree.children) {
    yield s
    yield* s.children
  }
}

/** `<prefix><n>` with n one past the highest number already used with that prefix. */
function nextId(spec, prefix) {
  let max = 0
  const re = new RegExp(`^${prefix.replace(/[.*+?^${}()|[\]\\-]/g, '\\$&')}(\\d+)$`)
  for (const node of allNodes(spec)) {
    const m = re.exec(node.id)
    if (m) max = Math.max(max, Number(m[1]))
  }
  return `${prefix}${max + 1}`
}

/** Section id prefix from a heading: lowercase words joined by "-", at most 24 chars. */
function slug(heading) {
  const s = heading
    .replace(/\\[a-zA-Z]+/g, ' ')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 24)
    .replace(/-+$/, '')
  return s || 'section'
}

/** {node, parent, index} for a section or paragraph id; null if absent. */
function locate(spec, id) {
  for (const [i, s] of spec.tree.children.entries()) {
    if (s.id === id) return { node: s, parent: spec.tree, index: i }
    for (const [j, p] of s.children.entries()) if (p.id === id) return { node: p, parent: s, index: j }
  }
  return null
}

function mustLocate(spec, id) {
  const loc = locate(spec, id)
  if (!loc) throw new SpecApiError(404, `No section or paragraph with id "${id}"`)
  return loc
}

function mustClaim(spec, id) {
  const claim = spec.claims.find((c) => c.id === id)
  if (!claim) throw new SpecApiError(404, `No claim with id "${id}"`)
  return claim
}

/** A tree node or the message (which `update` and claim links also accept). */
function mustNode(spec, id) {
  if (id === spec.message.id) return spec.message
  return mustLocate(spec, id).node
}

/**
 * Swap a node's fields for another level's; non-empty old ones go to notes.
 * The anchor is dropped (a heading cannot anchor a paragraph, nor a marker a
 * section), so the node becomes planned, as in the real backend.
 */
function changeLevel(node, level) {
  const displaced = Object.entries(node.fields)
    .filter(([, v]) => v !== '')
    .map(([k, v]) => `${k}: ${v}`)
  node.notes = [node.notes, ...displaced].filter(Boolean).join('\n')
  node.fields = blankFields(level)
  node.level = level
  node.anchor = null
}

const clampIndex = (index, length) => Math.min(Math.max(0, Number.isInteger(index) ? index : length), length)

// ---------------------------------------------------------------------------
// Ops (POST /api/spec/ops)
// ---------------------------------------------------------------------------

/** Apply one op to a copy of `spec`; returns {spec, new_id?}. Throws SpecApiError. */
export function applyOp(input, op) {
  const spec = clone(input)
  if (!op || typeof op.type !== 'string') throw new SpecApiError(400, 'op.type is required')
  let newId

  switch (op.type) {
    case 'update': {
      const node = mustNode(spec, op.id)
      if (typeof op.says === 'string') node.says = op.says
      if (op.fields) Object.assign(node.fields, op.fields)
      if (typeof op.notes === 'string') node.notes = op.notes
      break
    }
    case 'insert': {
      const parent = op.parent_id === spec.tree.id ? spec.tree : mustLocate(spec, op.parent_id).node
      const expected = parent === spec.tree ? 'section' : parent.level === 'section' ? 'paragraph' : null
      if (op.level !== expected) {
        throw new SpecApiError(400, `A ${op.level} cannot go under ${parent === spec.tree ? 'the root' : `a ${parent.level}`}`)
      }
      newId = nextId(spec, op.level === 'section' ? 'section-' : 'p-')
      parent.children.splice(clampIndex(op.index, parent.children.length), 0, makeNode(newId, op.level, op.says ?? ''))
      break
    }
    case 'delete': {
      const { parent, index } = mustLocate(spec, op.id)
      parent.children.splice(index, 1)
      break
    }
    case 'move': {
      const { node, parent, index } = mustLocate(spec, op.id)
      const target = op.parent_id === spec.tree.id ? spec.tree : mustLocate(spec, op.parent_id).node
      if (target === node || node.children.some((c) => c === target)) {
        throw new SpecApiError(400, 'Cannot move a node into itself')
      }
      const ok = (node.level === 'section' && target === spec.tree) || (node.level === 'paragraph' && target.level === 'section' && target !== spec.tree)
      if (!ok) throw new SpecApiError(400, `A ${node.level} cannot go under ${target === spec.tree ? 'the root' : `a ${target.level}`}`)
      parent.children.splice(index, 1)
      target.children.splice(clampIndex(op.index, target.children.length), 0, node)
      break
    }
    case 'set_level': {
      const { node, parent, index } = mustLocate(spec, op.id)
      if (op.level !== 'section' && op.level !== 'paragraph') throw new SpecApiError(400, 'level must be section or paragraph')
      if (node.level === op.level) break
      if (op.level === 'section') {
        // Promote: becomes the section right after its old section.
        parent.children.splice(index, 1)
        changeLevel(node, 'section')
        spec.tree.children.splice(spec.tree.children.indexOf(parent) + 1, 0, node)
      } else {
        // Demote: only without paragraphs; goes to the end of the section before it.
        if (node.children.length) {
          throw new SpecApiError(400, `Section "${node.id}" has paragraphs; it can become a paragraph only when it has none`)
        }
        if (index === 0) throw new SpecApiError(400, `There is no section before "${node.id}" to hold it as a paragraph`)
        const previous = spec.tree.children[index - 1]
        spec.tree.children.splice(index, 1)
        changeLevel(node, 'paragraph')
        previous.children.push(node)
      }
      break
    }
    case 'lift_to_claim': {
      const node = mustNode(spec, op.id)
      newId = nextId(spec, 'c')
      spec.claims.push(makeNode(newId, 'claim', node.says))
      node.claims.push(newId)
      break
    }
    case 'link_claim': {
      const node = mustNode(spec, op.id)
      mustClaim(spec, op.claim_id)
      if (!node.claims.includes(op.claim_id)) node.claims.push(op.claim_id)
      break
    }
    case 'unlink_claim': {
      const node = mustNode(spec, op.id)
      node.claims = node.claims.filter((c) => c !== op.claim_id)
      break
    }
    case 'add_claim': {
      newId = nextId(spec, 'c')
      const claim = makeNode(newId, 'claim', op.says ?? '')
      Object.assign(claim.fields, op.fields ?? {})
      spec.claims.push(claim)
      break
    }
    case 'update_claim': {
      const claim = mustClaim(spec, op.id)
      if (typeof op.says === 'string') claim.says = op.says
      if (op.fields) Object.assign(claim.fields, op.fields)
      if (typeof op.notes === 'string') claim.notes = op.notes
      break
    }
    case 'delete_claim': {
      mustClaim(spec, op.id)
      spec.claims = spec.claims.filter((c) => c.id !== op.id)
      for (const node of allNodes(spec)) node.claims = node.claims.filter((c) => c !== op.id)
      break
    }
    case 'set_anchor': {
      mustNode(spec, op.id).anchor = op.anchor ?? null
      break
    }
    default:
      throw new SpecApiError(400, `Unknown op type "${op.type}"`)
  }
  return { spec, new_id: newId }
}

// ---------------------------------------------------------------------------
// The .tex side: document order, markers, paragraph text
// ---------------------------------------------------------------------------

const HEADING = /^\s*\\(section|subsection)\*?\{(.*)\}\s*$/
const SKIPPED_ENV = /^\s*\\begin\{(figure|table|equation|align|verbatim)\*?\}/
const ONE_COMMAND = /^\s*\\[a-zA-Z@]+\*?(\[[^\]]*\])?(\{[^{}]*\})*\s*(%.*)?$/
const BLANK = /^\s*$/
const COMMENT = /^\s*%/
const ANY_MARKER = /^\s*%\s*@p\s+(\S+)/

const markerLine = (id) => new RegExp(`^\\s*%\\s*@p\\s+${id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*$`)
const collapse = (text) => text.replace(/\s+/g, ' ').trim()
const stripComment = (line) => line.replace(/(^|[^\\])%.*$/, '$1')

/** Every line of the paper in reading order, following \input / \include from main.tex. */
export function documentLines(files, main = 'main.tex') {
  const out = []
  const seen = new Set()
  const visit = (file) => {
    if (seen.has(file) || !(file in files)) return
    seen.add(file)
    files[file].split('\n').forEach((text, i) => {
      out.push({ file, line: i + 1, text })
      const m = /^\s*\\(?:input|include)\{([^}]+)\}/.exec(text)
      if (m) visit(m[1].endsWith('.tex') ? m[1] : `${m[1]}.tex`)
    })
  }
  visit(main)
  return out
}

/** Text of the paragraph below the marker at `markerIndex` (0-based), comments stripped. */
function paragraphText(lines, markerIndex) {
  const body = []
  for (let i = markerIndex + 1; i < lines.length; i++) {
    const t = lines[i]
    if (BLANK.test(t) || ANY_MARKER.test(t) || HEADING.test(t) || /\\end\{document\}/.test(t)) break
    body.push(stripComment(t))
  }
  return collapse(body.join(' '))
}

/** Small stable string hash (FNV-1a), standing in for the backend's text hash. */
export function textHash(text) {
  let h = 0x811c9dc5
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i)
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return h.toString(16)
}

/** Where a node's anchor is: {line (1-based), hash} for markers, {line} for headings; null if not found. */
function resolveAnchor(anchor, files) {
  const lines = files[anchor.file]?.split('\n')
  if (!lines) return null
  if (anchor.marker.startsWith('@p')) {
    const re = markerLine(anchor.marker.slice(2).trim())
    const idx = lines.findIndex((l) => re.test(l))
    return idx < 0 ? null : { line: idx + 1, hash: textHash(paragraphText(lines, idx)) }
  }
  const wanted = collapse(anchor.marker)
  const idx = lines.findIndex((l) => collapse(l).startsWith(wanted) || collapse(l).includes(wanted))
  return idx < 0 ? null : { line: idx + 1 }
}

// ---------------------------------------------------------------------------
// Status (GET /api/spec/status) and baselines
// ---------------------------------------------------------------------------

function* treeNodes(spec) {
  for (const s of spec.tree.children) {
    yield s
    yield* s.children
  }
}

/**
 * planned / linked / stale / orphaned for every section and paragraph.
 * Mutates `baselines`: a paragraph seen for the first time gets one.
 */
export function computeStatus(spec, files, baselines) {
  const nodes = {}
  for (const node of treeNodes(spec)) {
    if (!node.anchor) {
      nodes[node.id] = { status: 'planned', file: null, line: null }
      continue
    }
    const hit = resolveAnchor(node.anchor, files)
    if (!hit) {
      nodes[node.id] = { status: 'orphaned', file: node.anchor.file, line: null }
      continue
    }
    let status = 'linked'
    if (hit.hash !== undefined) {
      if (!(node.id in baselines)) baselines[node.id] = hit.hash
      if (baselines[node.id] !== hit.hash) status = 'stale'
    }
    nodes[node.id] = { status, file: node.anchor.file, line: hit.line }
  }
  return nodes
}

/**
 * After a write: paragraphs whose `says` changed (or that are new, or listed
 * in `force`) take the current text as their baseline.
 */
export function rebaseline(previous, next, files, baselines, force = []) {
  const before = new Map(previous ? [...treeNodes(previous)].map((n) => [n.id, n]) : [])
  for (const node of treeNodes(next)) {
    if (!node.anchor || !node.anchor.marker.startsWith('@p')) continue
    const old = before.get(node.id)
    if (!force.includes(node.id) && old && old.says === node.says && old.anchor?.marker === node.anchor.marker) continue
    const hit = resolveAnchor(node.anchor, files)
    if (hit?.hash !== undefined) baselines[node.id] = hit.hash
  }
}

/** POST /api/spec/nodes/{id}/reconcile: re-baseline one node; returns its {status, file, line}. */
export function reconcile(spec, files, baselines, id) {
  const loc = mustLocate(spec, id)
  const hit = loc.node.anchor ? resolveAnchor(loc.node.anchor, files) : null
  if (hit?.hash !== undefined) baselines[id] = hit.hash
  return computeStatus(spec, files, baselines)[id]
}

// ---------------------------------------------------------------------------
// Skeleton (GET /api/spec/skeleton)
// ---------------------------------------------------------------------------

export function skeleton(spec, files) {
  const sections = []
  const paragraphs = []
  let inPreamble = true
  let env = null
  let heading = null
  let run = [] // consecutive text/comment lines

  const flush = () => {
    let i = 0
    let marker = null
    while (i < run.length && COMMENT.test(run[i].text)) {
      const m = ANY_MARKER.exec(run[i].text)
      if (m) marker = m[1]
      i++
    }
    const body = run.slice(i)
    run = []
    if (!body.some((e) => !COMMENT.test(e.text))) return // nothing but comments
    paragraphs.push({
      file: body[0].file,
      line_start: body[0].line,
      line_end: body[body.length - 1].line,
      preview: collapse(body.map((e) => stripComment(e.text)).join(' ')).slice(0, 80),
      section_heading: heading,
      marker_id: marker,
    })
  }

  const doc = documentLines(files)
  for (const [k, entry] of doc.entries()) {
    const t = entry.text
    if (inPreamble) {
      if (/\\begin\{document\}/.test(t)) inPreamble = false
      continue
    }
    if (env) {
      if (new RegExp(`\\\\end\\{${env}\\*?\\}`).test(t)) env = null
      continue
    }
    if (run.length && run[0].file !== entry.file) flush()
    const envStart = SKIPPED_ENV.exec(t)
    if (envStart) {
      flush()
      if (!new RegExp(`\\\\end\\{${envStart[1]}\\*?\\}`).test(t)) env = envStart[1]
      continue
    }
    const h = HEADING.exec(t)
    if (h) {
      flush()
      heading = h[2]
      const next = doc[k + 1]
      const label = next && next.file === entry.file ? /^\s*\\label\{([^}]*)\}/.exec(next.text)?.[1] ?? null : null
      const marker = `\\${h[1]}{${h[2]}}`
      const adopted = spec ? [...treeNodes(spec)].find((n) => n.anchor?.file === entry.file && n.anchor.marker === marker) : null
      sections.push({ file: entry.file, line: entry.line, kind: h[1], heading: h[2], label, adopted_id: adopted?.id ?? null })
      continue
    }
    if (BLANK.test(t) || ONE_COMMAND.test(t)) {
      flush()
      continue
    }
    run.push(entry)
  }
  flush()
  return { sections, paragraphs }
}

// ---------------------------------------------------------------------------
// Adopt (POST /api/spec/adopt)
// ---------------------------------------------------------------------------

/** Reading position of file:line, for "insert at the position matching document order". */
function docPosition(files, file, line) {
  const idx = documentLines(files).findIndex((e) => e.file === file && e.line === line)
  return idx < 0 ? Number.MAX_SAFE_INTEGER : idx
}

function anchorPosition(files, anchor) {
  if (!anchor) return null
  const hit = resolveAnchor(anchor, files)
  return hit ? docPosition(files, anchor.file, hit.line) : null
}

/** Index in `list` before the first node anchored after `position` (else the end). */
function indexForPosition(files, list, position) {
  const i = list.findIndex((n) => {
    const p = anchorPosition(files, n.anchor)
    return p !== null && p > position
  })
  return i < 0 ? list.length : i
}

/**
 * Adopt a heading or a paragraph. Returns {spec, new_id, file, line,
 * fileTexts}; `fileTexts` maps each .tex file that had to change to its new
 * content (the caller stores them and broadcasts file.changed).
 */
export function adopt(input, files, req) {
  const spec = clone(input)
  const lines = files[req?.file]?.split('\n')
  if (!lines) throw new SpecApiError(400, `${req?.file} is not part of the document`)

  if (req.kind === 'section') {
    const h = HEADING.exec(lines[req.line - 1] ?? '')
    if (!h) throw new SpecApiError(400, `no \\section or \\subsection heading at ${req.file}:${req.line}`)
    const marker = `\\${h[1]}{${h[2]}}`
    const existing = [...treeNodes(spec)].find((n) => n.anchor?.file === req.file && n.anchor.marker === marker)
    if (existing) throw new SpecApiError(409, `this heading is already adopted as '${existing.id}'`)
    const node = makeNode(nextId(spec, `${slug(h[2])}-`), 'section', req.says ?? '')
    node.anchor = { file: req.file, marker }
    const position = docPosition(files, req.file, req.line)
    spec.tree.children.splice(indexForPosition(files, spec.tree.children, position), 0, node)
    return { spec, new_id: node.id, file: req.file, line: req.line, fileTexts: {} }
  }

  if (req.kind !== 'paragraph') throw new SpecApiError(400, 'kind must be "section" or "paragraph"')
  // Any line of the paragraph (or its marker line) picks it.
  const paragraph = skeleton(spec, files).paragraphs.find(
    (p) => p.file === req.file && ((p.line_start <= req.line_start && req.line_start <= p.line_end) || markerAbove(lines, p) === req.line_start),
  )
  if (!paragraph) throw new SpecApiError(400, `no paragraph at ${req.file}:${req.line_start}`)

  const existing = req.id ? locate(spec, req.id) : null
  if (existing && existing.node.level !== 'paragraph') throw new SpecApiError(400, `'${req.id}' is a ${existing.node.level}, not a paragraph`)
  let id = existing?.node.id ?? req.id ?? null
  if (paragraph.marker_id && paragraph.marker_id !== id) {
    if (locate(spec, paragraph.marker_id)) throw new SpecApiError(409, `this paragraph is already adopted as '${paragraph.marker_id}'`)
    if (id) throw new SpecApiError(409, `this paragraph carries the marker '% @p ${paragraph.marker_id}' that no node owns; delete that line first`)
    id = paragraph.marker_id // an unowned marker: adopt under that id
  }
  id = id ?? nextId(spec, 'p-')

  // Place the marker above the paragraph; remove the same marker anywhere else.
  const fileTexts = {}
  let markerAt = markerAbove(lines, paragraph)
  if (paragraph.marker_id !== id) {
    const re = markerLine(id)
    for (const [path, text] of Object.entries(files)) {
      const own = path === req.file
      if (!own && !text.split('\n').some((l) => re.test(l))) continue
      const out = []
      let shift = 0
      text.split('\n').forEach((line, i) => {
        if (own && i + 1 === paragraph.line_start) out.push(`% @p ${id}`)
        if (re.test(line)) {
          if (own && i + 1 < paragraph.line_start) shift++
          return
        }
        out.push(line)
      })
      fileTexts[path] = out.join('\n')
      if (own) markerAt = paragraph.line_start - shift
    }
  }
  const after = { ...files, ...fileTexts }
  const anchor = { file: req.file, marker: `@p ${id}` }

  if (existing) {
    existing.node.anchor = anchor
    if (typeof req.says === 'string') existing.node.says = req.says
  } else {
    // Parent: as asked, else the section anchored last before it in the same file, else the last section.
    let parent = null
    if (req.parent_id) {
      parent = locate(spec, req.parent_id)?.node ?? null
      if (!parent) throw new SpecApiError(404, `unknown parent id '${req.parent_id}'`)
      if (parent.level !== 'section') throw new SpecApiError(400, `parent '${req.parent_id}' is a ${parent.level}, not a section`)
    } else {
      const here = docPosition(after, req.file, markerAt)
      for (const s of spec.tree.children) {
        const p = s.anchor?.file === req.file ? anchorPosition(after, s.anchor) : null
        if (p !== null && p < here) parent = s
      }
      parent = parent ?? spec.tree.children.at(-1) ?? null
      if (!parent) throw new SpecApiError(400, 'adopt a section first: there is no section to put the paragraph in')
    }
    const node = makeNode(id, 'paragraph', req.says ?? '')
    node.anchor = anchor
    parent.children.splice(indexForPosition(after, parent.children, docPosition(after, req.file, markerAt)), 0, node)
  }
  return { spec, new_id: id, file: req.file, line: markerAt, fileTexts }
}

/** 1-based line of the `% @p` marker right above a skeleton paragraph (or of where it would go). */
function markerAbove(lines, paragraph) {
  for (let i = paragraph.line_start - 2; i >= 0; i--) {
    if (ANY_MARKER.test(lines[i])) return i + 1
    if (!COMMENT.test(lines[i])) break
  }
  return paragraph.line_start
}

// ---------------------------------------------------------------------------
// Coverage (GET /api/spec/coverage, phase 5)
// ---------------------------------------------------------------------------

/** "Introduction" from an anchor marker such as \section{Introduction}; null for planned or \label anchors. */
function headingOf(anchor) {
  if (!anchor) return null
  return /^\\(?:sub)?section\*?\{(.*)\}\s*$/.exec(anchor.marker)?.[1] ?? null
}

/** Claims x sections matrix: every pair is present in `cells` (0 when unlinked). */
export function coverage(spec) {
  const sections = spec.tree.children
  const cells = {}
  for (const claim of spec.claims) {
    cells[claim.id] = {}
    for (const section of sections) {
      const nodes = [section, ...section.children]
      cells[claim.id][section.id] = nodes.filter((n) => n.claims.includes(claim.id)).length
    }
  }
  const total = (claimId) => Object.values(cells[claimId]).reduce((a, b) => a + b, 0)
  return {
    claims: spec.claims.map((c) => ({ id: c.id, says: c.says })),
    sections: sections.map((s) => ({ id: s.id, says: s.says, heading: headingOf(s.anchor) })),
    cells,
    unreferenced_claims: spec.claims.filter((c) => total(c.id) === 0).map((c) => c.id),
    empty_sections: sections.filter((s) => spec.claims.every((c) => cells[c.id][s.id] === 0)).map((s) => s.id),
  }
}
