// RunLauncher: choose a review pass, a scope and a model, then preview or run it.
//
//   Pass [paragraph-function ▾]  "For each paragraph in ... check it against ..."
//   Scope (•) paper  ( ) section [Introduction ▾]  ( ) file [intro.tex ▾]  ( ) selection intro.tex:3–7
//   Runner [default (claude) ▾]  Model [ default (sonnet) ]  Thinking [default (medium) ▾]
//   [Show prompt]  [Run]  [ ] force
//
// - Passes come from GET /api/passes, grouped by level from coarse (message)
//   to fine (sentence). Choosing a pass switches the scope to its default.
// - Sections come from paper.json (the Spec tab's store); only sections
//   anchored in the .tex can be run. Files come from the file tree; the file
//   open in the editor is the default. "selection" uses the lines selected in
//   the editor and is disabled while nothing is selected.
// - Runner, model and thinking may stay on "default": the request then omits
//   them and the backend uses the pass's front matter or config.toml's
//   per-level defaults (shown in the placeholders). The model box also offers
//   the runner's suggestions but accepts any name.
// - "Show prompt" opens PromptDialog with POST /api/runs/preview (nothing runs);
//   "Run" sends POST /api/runs ("force" skips the cache). A refusal (409: the
//   same pass is already running on that scope) shows as a toast.
// - The runner / model / thinking pickers are findings/RunnerControls.tsx,
//   shared with the blind compare dialog (phase 5).
//
// Phase 6:
// - Only passes of kind "review" are listed (the triage and ab-compare passes
//   are started from a run and from the compare dialog).
// - "Run both ▾" opens a small panel with a model and thinking per runner and
//   "Start both": the same pass and scope on claude and on codex at once
//   (POST /api/groups/both). Enabled only while both runners are installed.
// - "Fan out" (only with scope = paper) splits the paper into one run per
//   linked section plus one message-drift run over the abstract, the first
//   and the last section, at most 3 at a time (POST /api/groups/fanout). It
//   uses the runner / model / thinking chosen in the main row.
// - "force" applies to both buttons too. The new group is selected in the
//   Findings tab, where its merged view appears.
import { useMemo, useState } from 'react'
import {
  FINDING_LEVELS,
  passKind,
  type BothRequest,
  type FanoutRequest,
  type Pass,
  type RunnerName,
  type RunnerOptions,
  type RunRequest,
  type Scope,
  type ScopeKind,
} from '../api/client'
import { useRunsStore } from '../state/runs'
import { useSpecStore } from '../state/spec'
import { useStore } from '../state/store'
import Popover from './Popover'
import PromptDialog from './PromptDialog'
import RunnerControls from './RunnerControls'

/** The two runners of "Run both", in display order. */
const BOTH_RUNNERS: RunnerName[] = ['claude', 'codex']

const SCOPE_KINDS: ScopeKind[] = ['paper', 'section', 'file', 'selection']

/** "Introduction" from an anchor marker like \section{Introduction}. */
function headingOf(marker: string): string {
  const m = /^\\(?:sub)?section\*?\s*(?:\[[^\]]*\])?\{(.*)\}\s*$/.exec(marker) ?? /^\\label\{(.*)\}\s*$/.exec(marker)
  return m ? m[1] : marker
}

/** Passes grouped by level, coarse to fine; unknown levels go last. */
function groupPasses(passes: Pass[]): [string, Pass[]][] {
  const order = [...FINDING_LEVELS, ...new Set(passes.map((p) => p.level).filter((l) => !FINDING_LEVELS.includes(l as never)))]
  return order
    .map((level): [string, Pass[]] => [level, passes.filter((p) => p.level === level)])
    .filter(([, list]) => list.length > 0)
}

export default function RunLauncher() {
  const allPasses = useRunsStore((s) => s.passes)
  const passesError = useRunsStore((s) => s.passesError)
  const runners = useRunsStore((s) => s.runners)
  const startRun = useRunsStore((s) => s.startRun)
  const startBoth = useRunsStore((s) => s.startBoth)
  const startFanout = useRunsStore((s) => s.startFanout)
  const preview = useRunsStore((s) => s.preview)
  const spec = useSpecStore((s) => s.spec)
  const status = useSpecStore((s) => s.status)
  const files = useStore((s) => s.files)
  const activePath = useStore((s) => s.activeFile?.path ?? null)
  const selection = useStore((s) => s.editorSelection)

  // The user's choices. Empty strings mean "default" / "not chosen yet".
  const [passName, setPassName] = useState('')
  const [scopeKind, setScopeKind] = useState<ScopeKind | ''>('')
  const [sectionRef, setSectionRef] = useState('')
  const [fileChoice, setFileChoice] = useState('')
  const [runner, setRunner] = useState('')
  const [model, setModel] = useState('')
  const [thinking, setThinking] = useState('')
  const [force, setForce] = useState(false)
  const [starting, setStarting] = useState(false)
  const [showPrompt, setShowPrompt] = useState<{ request: RunRequest } | null>(null)
  // Phase 6: model / thinking per runner for "Run both" ("" = default).
  const [bothOptions, setBothOptions] = useState<Record<RunnerName, { model: string; thinking: string }>>({
    claude: { model: '', thinking: '' },
    codex: { model: '', thinking: '' },
  })

  // Only review passes can be run from here (phase 6: triage / compare passes are started elsewhere).
  const passes = useMemo(() => allPasses.filter((p) => passKind(p) === 'review'), [allPasses])
  const groups = useMemo(() => groupPasses(passes), [passes])
  // Default pass: the first runnable one, in the grouped order.
  const pass =
    passes.find((p) => p.name === passName) ?? groups.flatMap(([, list]) => list).find((p) => !p.error) ?? null
  const levelDefaults = pass ? runners?.defaults[pass.level] : undefined

  // Scope kind: the user's choice, else the pass's default scope.
  const kind: ScopeKind =
    scopeKind || (pass && SCOPE_KINDS.includes(pass.scope as ScopeKind) ? (pass.scope as ScopeKind) : 'paper')

  // Sections of paper.json; only anchored (linked or stale) ones can be run.
  const sections = useMemo(
    () =>
      (spec?.tree.children ?? []).map((node) => {
        const st = status[node.id]?.status
        const runnable = !!node.anchor && st !== 'planned' && st !== 'orphaned'
        const heading = node.anchor ? headingOf(node.anchor.marker) : ''
        const label = [heading, node.says].filter(Boolean).join(' — ') || node.id
        // Fan-out takes "linked" sections only (not stale ones).
        return { id: node.id, label, runnable, linked: st === 'linked', note: st && !runnable ? ` (${st})` : '' }
      }),
    [spec, status],
  )
  const section = sections.find((s) => s.id === sectionRef && s.runnable) ?? sections.find((s) => s.runnable) ?? null
  const texFiles = files.filter((f) => f.path.endsWith('.tex')).map((f) => f.path)
  const file = fileChoice || (activePath && texFiles.includes(activePath) ? activePath : (texFiles[0] ?? ''))

  /** The scope as the API wants it, or null when the chosen kind cannot be used now. */
  const scope: Scope | null = (() => {
    const none = { ref: null, file: null, line_start: null, line_end: null }
    if (kind === 'paper') return { kind, ...none }
    if (kind === 'section') return section ? { kind, ...none, ref: section.id } : null
    if (kind === 'file') return file ? { kind, ...none, file } : null
    return selection ? { kind, ref: null, file: selection.path, line_start: selection.line_start, line_end: selection.line_end } : null
  })()

  const runnerInfo = runners?.runners.find((r) => r.name === (runner || levelDefaults?.runner))
  const runnerUnavailable = !!runnerInfo && !runnerInfo.available
  const defaultModel = pass?.model ?? levelDefaults?.model ?? null
  const defaultThinking = pass?.thinking ?? levelDefaults?.thinking ?? null
  const blocked = !pass || !!pass.error || !scope || runnerUnavailable

  // Phase 6: "Run both" needs both CLIs; "Fan out" needs scope paper and a linked section.
  const missingRunners = BOTH_RUNNERS.filter((name) => !runners?.runners.find((r) => r.name === name)?.available)
  const bothBlocked = !pass || !!pass.error || !scope || missingRunners.length > 0
  const linkedCount = sections.filter((s) => s.linked).length
  const fanoutBlocked = !pass || !!pass.error || runnerUnavailable || linkedCount === 0
  /** Model a runner uses by default: the pass / level model applies only on the level's own runner. */
  const defaultModelFor = (name: RunnerName) => ((levelDefaults?.runner ?? 'claude') === name ? defaultModel : null)

  const runBoth = async (close: () => void) => {
    if (!pass || !scope || bothBlocked) return
    const req: BothRequest = { pass: pass.name, scope }
    for (const name of BOTH_RUNNERS) {
      const options: RunnerOptions = {}
      if (bothOptions[name].model.trim()) options.model = bothOptions[name].model.trim()
      if (bothOptions[name].thinking) options.thinking = bothOptions[name].thinking
      if (Object.keys(options).length > 0) req[name] = options
    }
    if (force) req.force = true
    close()
    setStarting(true)
    await startBoth(req)
    setStarting(false)
  }

  const runFanout = async () => {
    if (!pass || fanoutBlocked) return
    const req: FanoutRequest = { pass: pass.name }
    if (runner) req.runner = runner as RunnerName
    if (model.trim()) req.model = model.trim()
    if (thinking) req.thinking = thinking
    if (force) req.force = true
    setStarting(true)
    await startFanout(req)
    setStarting(false)
  }

  const request = (): RunRequest | null => {
    if (!pass || !scope) return null
    const req: RunRequest = { pass: pass.name, scope }
    if (runner) req.runner = runner as RunRequest['runner']
    if (model.trim()) req.model = model.trim()
    if (thinking) req.thinking = thinking
    return req
  }

  const run = async () => {
    const req = request()
    if (!req || blocked) return
    setStarting(true)
    await startRun(force ? { ...req, force: true } : req)
    setStarting(false)
  }

  const choosePass = (name: string) => {
    setPassName(name)
    setScopeKind('') // back to the new pass's default scope
  }

  const globalFiles = (runners?.global_instructions ?? []).filter(
    (g) => g.present && g.runner === runnerInfo?.name && !runnerInfo?.suppress_global,
  )

  return (
    <div className="flex flex-col gap-2 border-b border-base-300 bg-base-100 px-3 py-2 text-sm" data-testid="run-launcher">
      {/* Pass */}
      <div className="flex flex-wrap items-center gap-2">
        <label className="w-16 font-medium" htmlFor="run-pass">
          Pass
        </label>
        <select
          id="run-pass"
          className="select w-56 select-sm"
          value={pass?.name ?? ''}
          onChange={(e) => choosePass(e.target.value)}
          data-testid="pass-select"
        >
          {passes.length === 0 && <option value="">{passesError ? 'could not load passes' : 'no passes'}</option>}
          {groups.map(([level, list]) => (
            <optgroup key={level} label={level}>
              {list.map((p) => (
                <option key={p.name} value={p.name} disabled={!!p.error} title={p.error ?? p.description}>
                  {p.name}
                  {p.error ? ' (error)' : ''}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
        {pass && (
          <span className="flex min-w-0 flex-1 items-center gap-1" data-testid="pass-description">
            <span className="badge badge-sm badge-ghost">{pass.level}</span>
            {pass.allow_rewrites && (
              <span className="badge badge-sm badge-warning" title="This pass may quote corrected text (LaTeX only)">
                rewrites allowed
              </span>
            )}
            {pass.error ? (
              <span className="truncate text-error" title={pass.error}>
                {pass.error}
              </span>
            ) : (
              <span className="truncate text-base-content/70" title={pass.description}>
                {pass.description}
              </span>
            )}
          </span>
        )}
      </div>

      {/* Scope */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1" role="radiogroup" aria-label="Scope">
        <span className="w-16 font-medium">Scope</span>
        {SCOPE_KINDS.map((k) => {
          const disabled = (k === 'selection' && !selection) || (k === 'section' && sections.every((s) => !s.runnable))
          return (
            <span key={k} className="flex items-center gap-1">
              <label className={`flex items-center gap-1 ${disabled ? 'text-base-content/40' : 'cursor-pointer'}`}>
                <input
                  type="radio"
                  className="radio radio-xs"
                  name="run-scope"
                  value={k}
                  checked={kind === k}
                  disabled={disabled}
                  onChange={() => setScopeKind(k)}
                  data-testid={`scope-${k}`}
                />
                {k}
              </label>
              {k === 'section' && (
                <select
                  className="select w-52 select-xs"
                  value={section?.id ?? ''}
                  onChange={(e) => {
                    setSectionRef(e.target.value)
                    setScopeKind('section')
                  }}
                  disabled={sections.length === 0}
                  title={sections.length === 0 ? 'No sections in paper.json yet (adopt one in the Spec tab)' : undefined}
                  data-testid="scope-section-select"
                >
                  {sections.length === 0 && <option value="">no sections</option>}
                  {sections.map((s) => (
                    <option key={s.id} value={s.id} disabled={!s.runnable}>
                      {s.label}
                      {s.note}
                    </option>
                  ))}
                </select>
              )}
              {k === 'file' && (
                <select
                  className="select w-44 select-xs"
                  value={file}
                  onChange={(e) => {
                    setFileChoice(e.target.value)
                    setScopeKind('file')
                  }}
                  data-testid="scope-file-select"
                >
                  {texFiles.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
              )}
              {k === 'selection' && (
                <span className="font-mono text-xs text-base-content/60" data-testid="scope-selection-label">
                  {selection
                    ? `${selection.path}:${selection.line_start}–${selection.line_end}`
                    : '(select lines in the editor)'}
                </span>
              )}
            </span>
          )
        })}
      </div>

      {/* Runner, model, thinking, buttons */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="w-16 font-medium">Runner</span>
        <RunnerControls
          runners={runners}
          levelDefaults={levelDefaults}
          defaultModel={defaultModel}
          defaultThinking={defaultThinking}
          runner={runner}
          onRunner={setRunner}
          model={model}
          onModel={setModel}
          thinking={thinking}
          onThinking={setThinking}
          listId="run-model-suggestions"
        />
        <div className="ml-auto flex items-center gap-2">
          {runnerUnavailable && <span className="text-xs text-error">{runnerInfo?.name} is not installed</span>}
          {globalFiles.map((g) => (
            <span key={g.path} className="text-xs text-warning" title="The CLI also reads this global file; keep it harmless to prose review.">
              also reads {g.path}
            </span>
          ))}
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => {
              const req = request()
              if (req) setShowPrompt({ request: req })
            }}
            disabled={!pass || !!pass.error || !scope}
            title="Show the exact prompt (nothing runs); copy it or paste an answer back"
            data-testid="show-prompt"
          >
            Show prompt
          </button>
          <label className="flex cursor-pointer items-center gap-1 text-xs" title="Run even if an identical earlier run could be reused">
            <input
              type="checkbox"
              className="checkbox checkbox-xs"
              checked={force}
              onChange={(e) => setForce(e.target.checked)}
              data-testid="run-force"
            />
            force
          </label>
          <button
            type="button"
            className="btn btn-sm btn-primary"
            onClick={() => void run()}
            disabled={blocked || starting}
            data-testid="run-button"
          >
            {starting && <span className="loading loading-xs loading-spinner" />}
            Run
          </button>
          {/* Phase 6: the same pass and scope on claude and codex, merged by anchor. */}
          <Popover
            label="Run both"
            testId="run-both-button"
            disabled={bothBlocked || starting}
            alignRight
            title={
              missingRunners.length > 0
                ? `Run both needs claude and codex; ${missingRunners.join(' and ')} ${missingRunners.length === 1 ? 'is' : 'are'} not installed`
                : 'Run this pass on claude AND codex; their findings are merged by anchor, agreements first'
            }
          >
            {(close) => (
              <>
                <div className="text-xs text-base-content/70">
                  Same pass and scope on both runners. Empty = the default shown.
                </div>
                {BOTH_RUNNERS.map((name) => (
                  <div key={name} className="flex items-center gap-2" data-testid={`both-row-${name}`}>
                    <span className="w-14 font-medium">{name}</span>
                    <RunnerControls
                      runners={runners}
                      levelDefaults={levelDefaults}
                      defaultModel={defaultModelFor(name)}
                      defaultThinking={defaultThinking}
                      runner={name}
                      onRunner={() => {}}
                      model={bothOptions[name].model}
                      onModel={(value) => setBothOptions((o) => ({ ...o, [name]: { ...o[name], model: value } }))}
                      thinking={bothOptions[name].thinking}
                      onThinking={(value) => setBothOptions((o) => ({ ...o, [name]: { ...o[name], thinking: value } }))}
                      showRunner={false}
                      testIdPrefix={`both-${name}-`}
                      listId={`both-${name}-model-suggestions`}
                    />
                  </div>
                ))}
                <div className="flex justify-end">
                  <button
                    type="button"
                    className="btn btn-sm btn-primary"
                    onClick={() => void runBoth(close)}
                    disabled={bothBlocked}
                    data-testid="run-both-start"
                  >
                    Start both
                  </button>
                </div>
              </>
            )}
          </Popover>
          {kind === 'paper' && (
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => void runFanout()}
              disabled={fanoutBlocked || starting}
              title={
                linkedCount === 0
                  ? 'Fan out needs at least one linked section in paper.json (adopt sections in the Spec tab)'
                  : `Fan out: one run per linked section (${linkedCount}) with the full spec as context, plus one message-drift run over the abstract, introduction and conclusion — ${linkedCount + 1} runs, at most 3 at once`
              }
              data-testid="fanout-button"
            >
              Fan out
            </button>
          )}
        </div>
      </div>

      {showPrompt && (
        <PromptDialog
          title={`Prompt · ${showPrompt.request.pass}`}
          load={() => preview(showPrompt.request)}
          pasteTarget={{ pass: showPrompt.request.pass, scope: showPrompt.request.scope }}
          onClose={() => setShowPrompt(null)}
        />
      )}
    </div>
  )
}
