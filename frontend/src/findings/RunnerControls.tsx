// RunnerControls: the runner / model / thinking pickers shared by the run
// launcher (findings/RunLauncher.tsx) and the blind compare dialog
// (compare/CompareDialog.tsx).
//
//   [default (claude) ▾]  [ default (sonnet)      ]  [thinking: default (medium) ▾]
//
// An empty string means "default": the request then leaves the option out
// and the backend uses the pass's front matter or config.toml's per-level
// default, which the placeholders show. The model box offers the runner's
// suggestions (a <datalist>) but accepts any name. `testIdPrefix` keeps the
// test ids unique when both users are on the page ("" for the launcher,
// "compare-" for the dialog), and `listId` does the same for the datalist.
//
// Phase 6: `showRunner={false}` hides the runner picker when the runner is
// fixed (the per-runner rows of "Run both"); `runner` then names that runner,
// so the model suggestions are its own. Also used by the Triage popover
// (prefix "triage-").
//
// Under the runner picker, a small note says which login the chosen runner's
// CLI uses ("via claude.ai", "via ChatGPT"; from GET /api/runners `auth`), and
// the backend's `warning` is shown in amber when that login is not a
// subscription (e.g. an API key). A runner that is not logged in is disabled.
import type { LevelDefaults, RunnersInfo } from '../api/client'

export interface RunnerControlsProps {
  runners: RunnersInfo | null
  /** Defaults of the pass's level (runner, model, thinking), for the "default (...)" labels. */
  levelDefaults: LevelDefaults | undefined
  /** Model / thinking the request would use by default (front matter, else level default). */
  defaultModel: string | null
  defaultThinking: string | null
  runner: string
  onRunner: (runner: string) => void
  model: string
  onModel: (model: string) => void
  thinking: string
  onThinking: (thinking: string) => void
  testIdPrefix?: string
  listId: string
  /** False when the runner is fixed (only model and thinking are shown). Default true. */
  showRunner?: boolean
}

export default function RunnerControls({
  runners,
  levelDefaults,
  defaultModel,
  defaultThinking,
  runner,
  onRunner,
  model,
  onModel,
  thinking,
  onThinking,
  testIdPrefix = '',
  listId,
  showRunner = true,
}: RunnerControlsProps) {
  const runnerInfo = runners?.runners.find((r) => r.name === (runner || levelDefaults?.runner))
  return (
    <>
      {showRunner && (
        // A column: the picker, then the login note and warning under it.
        <div className="flex flex-col gap-0.5">
          <select
            className="select w-40 select-sm"
            value={runner}
            onChange={(e) => onRunner(e.target.value)}
            aria-label="Runner"
            data-testid={`${testIdPrefix}runner-select`}
          >
            <option value="">default ({levelDefaults?.runner ?? '?'})</option>
            {(runners?.runners ?? []).map((r) => (
              <option key={r.name} value={r.name} disabled={!r.available} title={r.error ?? undefined}>
                {r.name}
                {r.available ? '' : r.auth?.logged_in === false ? ' (not logged in)' : ' (not found)'}
              </option>
            ))}
          </select>
          {runnerInfo?.auth?.method && (
            <span
              className="text-xs text-base-content/60"
              title={runnerInfo.auth.detail ?? undefined}
              data-testid={`${testIdPrefix}runner-auth`}
            >
              via {runnerInfo.auth.method}
            </span>
          )}
          {runnerInfo?.warning && (
            <span className="max-w-60 text-xs text-amber-600" data-testid={`${testIdPrefix}runner-warning`}>
              {runnerInfo.warning}
            </span>
          )}
        </div>
      )}
      <input
        className="input input-sm w-44"
        list={listId}
        value={model}
        onChange={(e) => onModel(e.target.value)}
        placeholder={`default (${defaultModel ?? 'CLI default'})`}
        title="Model name; empty = the default shown. Suggestions come from config.toml."
        aria-label="Model"
        data-testid={`${testIdPrefix}model-input`}
      />
      <datalist id={listId}>
        {(runnerInfo?.models ?? []).map((m) => (
          <option key={m} value={m} />
        ))}
      </datalist>
      <select
        className="select w-52 select-sm"
        value={thinking}
        onChange={(e) => onThinking(e.target.value)}
        aria-label="Thinking"
        data-testid={`${testIdPrefix}thinking-select`}
      >
        <option value="">thinking: default ({defaultThinking ?? '?'})</option>
        {(runners?.thinking_levels ?? ['low', 'medium', 'high']).map((t) => (
          <option key={t} value={t}>
            thinking: {t}
          </option>
        ))}
      </select>
    </>
  )
}
