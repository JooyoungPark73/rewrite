// Regression guard for the macOS acceptance fixes (2026-10-19).
//
// The headless suite and the live scripts were written on Linux and pressed
// `Control+<key>` for the app's Ctrl/Cmd shortcuts. CodeMirror binds those
// chords to `Mod-<key>`, which is Cmd on macOS, so `Control+a`, `Control+Home`
// and `Control+.` did nothing in a macOS Chromium and the tests failed or, worse,
// typed into the wrong place. Playwright's `ControlOrMeta` modifier presses the
// platform's own key, so every Ctrl/Cmd chord must use it. `Control+Alt+j` stays:
// the editor binds Ctrl-Alt-J on purpose next to Cmd-Alt-J (Chrome's console).
//
// The live p6 script counted `claude -p` processes with `pgrep -fc`; `-c` is
// GNU-only and macOS's BSD pgrep rejects it, which silently reported 0.
import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect, test } from '@playwright/test'

const E2E_DIR = dirname(fileURLToPath(import.meta.url))

function sources(): { file: string; text: string }[] {
  const out: { file: string; text: string }[] = []
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = join(dir, entry.name)
      if (entry.isDirectory()) walk(path)
      else if (/\.(ts|mjs)$/.test(entry.name) && entry.name !== 'portability.spec.ts') out.push({ file: path, text: readFileSync(path, 'utf8') })
    }
  }
  walk(E2E_DIR)
  return out
}

test('Ctrl/Cmd shortcuts are pressed with ControlOrMeta, never Control alone', () => {
  const offenders: string[] = []
  for (const { file, text } of sources()) {
    text.split('\n').forEach((line, i) => {
      // `Control+Alt+j` is the one chord the app binds with a bare Control on every platform.
      if (/press\(['"`]Control\+(?!Alt\+)/.test(line)) offenders.push(`${file}:${i + 1}: ${line.trim()}`)
    })
  }
  expect(offenders, 'use ControlOrMeta+<key> so the chord works on macOS too').toEqual([])
})

test('live scripts do not use the GNU-only `pgrep -c`', () => {
  const offenders: string[] = []
  for (const { file, text } of sources()) {
    text.split('\n').forEach((line, i) => {
      if (/^\s*(\/\/|\*)/.test(line)) return // comments may mention the flag
      if (/pgrep/.test(line) && /['"]-f?c['"]|-fc\b|pgrep -c/.test(line)) offenders.push(`${file}:${i + 1}: ${line.trim()}`)
    })
  }
  expect(offenders, 'count the lines of `pgrep -f` instead; BSD pgrep has no -c').toEqual([])
})
