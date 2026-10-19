// SettingsDialog: compiler settings for the open paper (phase 2).
//
// Opened from the gear button in the header. It edits the [compiler] section
// of .rewrite/config.toml through the backend:
//
//   GET /api/config   on open, to fill the form and show Docker's status
//   PUT /api/config   on Save, then GET again so the form shows what is stored
//   POST /api/compile/docker/pull   "Pull image"; its output arrives as
//                     `docker.pull` WebSocket events, which the store collects
//                     in `dockerPull` (so a pull keeps going, and its log is
//                     kept, if the dialog is closed meanwhile)
//
// The Docker option is disabled when the backend found no `docker` on PATH.
// Pull uses the *saved* TeX Live year, so it is disabled while the form has
// unsaved changes.
import { useCallback, useEffect, useRef, useState } from 'react'
import { errorMessage, getConfig, putConfig, type AppConfig, type CompilerKind } from '../api/client'
import { useStore } from '../state/store'

const MIN_YEAR = 2018
const MAX_YEAR = 2030

/** The year typed in the form as a number, or null when it is not a valid year. */
function parseYear(text: string): number | null {
  if (!/^\d{4}$/.test(text.trim())) return null
  const year = Number(text)
  return year >= MIN_YEAR && year <= MAX_YEAR ? year : null
}

export default function SettingsDialog({ onClose }: { onClose: () => void }) {
  // What the backend last reported (null until the first GET finishes).
  const [config, setConfig] = useState<AppConfig | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  // Form fields. The year is kept as text so a half-typed value can be shown.
  const [kind, setKind] = useState<CompilerKind>('native')
  const [yearText, setYearText] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveMessage, setSaveMessage] = useState<{ ok: boolean; text: string } | null>(null)

  const pull = useStore((s) => s.dockerPull)
  const startDockerPull = useStore((s) => s.startDockerPull)

  /** Read the config. `fillForm` also resets the form fields to it. */
  const reload = useCallback(
    (fillForm: boolean) =>
      getConfig().then(
        (fresh) => {
          setConfig(fresh)
          setLoadError(null)
          if (fillForm) {
            setKind(fresh.compiler.kind)
            setYearText(String(fresh.compiler.texlive_year))
          }
        },
        (err: unknown) => setLoadError(errorMessage(err)),
      ),
    [],
  )

  useEffect(() => {
    void reload(true)
  }, [reload])

  // When a pull ends while the dialog is open, re-read the config so "image
  // present" is up to date. Only the status part changes; the user's unsaved
  // form edits are kept.
  const pullStatus = pull.status
  const seenPullStatus = useRef(pullStatus)
  useEffect(() => {
    if (pullStatus === seenPullStatus.current) return
    seenPullStatus.current = pullStatus
    if (pullStatus === 'done' || pullStatus === 'failed') void reload(false)
  }, [pullStatus, reload])

  // Keep the newest pull output in view.
  const logRef = useRef<HTMLPreElement>(null)
  useEffect(() => {
    const el = logRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [pull.lines.length])

  const year = parseYear(yearText)
  const dirty = !!config && (kind !== config.compiler.kind || year !== config.compiler.texlive_year)
  const dockerAvailable = config?.docker.available ?? false

  const save = async () => {
    if (year === null) return
    setSaving(true)
    setSaveMessage(null)
    try {
      await putConfig({ compiler: { kind, texlive_year: year } })
      await reload(true)
      setSaveMessage({ ok: true, text: 'Saved. The next compile uses these settings.' })
    } catch (err) {
      setSaveMessage({ ok: false, text: errorMessage(err) })
    } finally {
      setSaving(false)
    }
  }

  let pullDisabledReason: string | null = null
  if (!dockerAvailable) pullDisabledReason = 'Docker was not found on the backend’s PATH.'
  else if (dirty) pullDisabledReason = 'Save first: Pull fetches the image for the saved year.'
  else if (pull.status === 'running') pullDisabledReason = 'A pull is already running.'

  return (
    <div
      className="modal modal-open"
      role="dialog"
      aria-modal="true"
      aria-labelledby="settings-title"
      data-testid="settings-dialog"
      onKeyDown={(e) => {
        if (e.key === 'Escape') onClose()
      }}
    >
      <div className="modal-box max-w-xl">
        <h3 id="settings-title" className="text-lg font-bold">
          Settings
        </h3>
        <p className="pb-2 text-sm text-base-content/70">
          Stored in this paper’s <code>.rewrite/config.toml</code>.
        </p>

        {loadError && (
          <div role="alert" className="alert text-sm alert-soft alert-error" data-testid="settings-error">
            Could not read the settings: {loadError}
          </div>
        )}
        {!config && !loadError && <span className="loading loading-sm loading-spinner" />}

        {config && (
          <form
            className="flex flex-col gap-4"
            onSubmit={(e) => {
              e.preventDefault()
              void save()
            }}
          >
            {/* Compiler kind: native latexmk, or latexmk inside a TeX Live Docker image. */}
            <fieldset className="fieldset">
              <legend className="fieldset-legend">Compiler</legend>
              <label className="flex cursor-pointer items-center gap-2">
                <input
                  type="radio"
                  name="compiler-kind"
                  className="radio radio-sm"
                  checked={kind === 'native'}
                  onChange={() => setKind('native')}
                />
                Native <code>latexmk</code> on this machine
              </label>
              <label className={`flex items-center gap-2 ${dockerAvailable ? 'cursor-pointer' : 'opacity-60'}`}>
                <input
                  type="radio"
                  name="compiler-kind"
                  className="radio radio-sm"
                  checked={kind === 'docker'}
                  onChange={() => setKind('docker')}
                  disabled={!dockerAvailable}
                />
                Docker (TeX Live image, to match Overleaf’s TeX Live year)
              </label>
              {!dockerAvailable && (
                <p className="text-xs text-warning" data-testid="docker-unavailable-hint">
                  Docker was not found on the backend’s PATH. Install Docker and restart the backend to use it.
                </p>
              )}
            </fieldset>

            {/* TeX Live year: picks the texlive/texlive:TL<year>-historic image. */}
            <fieldset className="fieldset">
              <legend className="fieldset-legend">TeX Live year (Docker only)</legend>
              <input
                type="number"
                className={`input input-sm w-32 ${year === null ? 'input-error' : ''}`}
                min={MIN_YEAR}
                max={MAX_YEAR}
                step={1}
                value={yearText}
                onChange={(e) => setYearText(e.target.value)}
                aria-label="TeX Live year"
              />
              {year === null && (
                <p className="text-xs text-error">
                  Enter a year from {MIN_YEAR} to {MAX_YEAR}.
                </p>
              )}
            </fieldset>

            {/* Docker image status and pull. */}
            <fieldset className="fieldset">
              <legend className="fieldset-legend">Docker image</legend>
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <code data-testid="docker-image">{config.compiler.image}</code>
                <span
                  className={`badge badge-sm ${config.docker.image_present ? 'badge-success' : 'badge-ghost'}`}
                  data-testid="docker-image-status"
                >
                  {config.docker.image_present ? 'present' : 'not pulled'}
                </span>
                <span className="text-xs text-base-content/60" data-testid="docker-available">
                  docker {config.docker.available ? 'available' : 'not available'}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={() => void startDockerPull()}
                  disabled={pullDisabledReason !== null}
                  title={pullDisabledReason ?? `docker pull ${config.compiler.image}`}
                >
                  {pull.status === 'running' && <span className="loading loading-xs loading-spinner" />}
                  Pull image
                </button>
                <PullStatus status={pull.status} error={pull.error} />
              </div>
              {dirty && dockerAvailable && <p className="text-xs text-base-content/60">{pullDisabledReason}</p>}
              {(pull.lines.length > 0 || pull.status === 'running') && (
                <pre
                  ref={logRef}
                  className="max-h-40 min-h-16 overflow-auto rounded bg-base-200 p-2 font-mono text-xs whitespace-pre-wrap"
                  data-testid="docker-pull-log"
                >
                  {pull.lines.join('\n')}
                </pre>
              )}
            </fieldset>

            {saveMessage && (
              <div
                role={saveMessage.ok ? 'status' : 'alert'}
                className={`alert text-sm alert-soft ${saveMessage.ok ? 'alert-success' : 'alert-error'}`}
                data-testid="settings-save-message"
              >
                {saveMessage.text}
              </div>
            )}

            <div className="modal-action mt-0">
              <button type="button" className="btn btn-ghost" onClick={onClose}>
                Close
              </button>
              <button type="submit" className="btn btn-primary" disabled={saving || year === null || !dirty}>
                {saving && <span className="loading loading-xs loading-spinner" />}
                Save
              </button>
            </div>
          </form>
        )}

        {!config && (
          <div className="modal-action">
            <button type="button" className="btn btn-ghost" onClick={onClose}>
              Close
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

/** "pulling…", "done" or "failed: <reason>" next to the Pull button. */
function PullStatus({ status, error }: { status: string; error: string | null }) {
  if (status === 'running') {
    return (
      <span className="text-sm text-base-content/70" data-testid="docker-pull-status">
        pulling…
      </span>
    )
  }
  if (status === 'done') {
    return (
      <span className="text-sm text-success" data-testid="docker-pull-status">
        done
      </span>
    )
  }
  if (status === 'failed') {
    return (
      <span className="text-sm text-error" data-testid="docker-pull-status" title={error ?? undefined}>
        failed{error ? `: ${error}` : ''}
      </span>
    )
  }
  return null
}
