// Playwright configuration for the headless smoke tests in e2e/.
//
// `npm run e2e` starts two servers, then runs the tests in Chromium:
//   1. the mock backend (mock/server.mjs) on 127.0.0.1:8799
//   2. the Vite dev server on 127.0.0.1:5199, proxying /api and /ws to the mock
// Different ports from the usual 8000/5173, so a running real backend or dev
// server does not interfere.
import { defineConfig, devices } from '@playwright/test'

export const MOCK_URL = 'http://127.0.0.1:8799'
const UI_PORT = 5199

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  expect: { timeout: 10_000 },
  // The mock backend holds shared state, so tests run one at a time.
  workers: 1,
  fullyParallel: false,
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${UI_PORT}`,
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } }],
  webServer: [
    {
      command: 'node mock/server.mjs',
      url: `${MOCK_URL}/api/health`,
      env: { MOCK_PORT: '8799' },
      reuseExistingServer: false,
    },
    {
      command: `vite --port ${UI_PORT} --strictPort`,
      url: `http://127.0.0.1:${UI_PORT}`,
      env: { REWRITE_BACKEND: MOCK_URL },
      reuseExistingServer: false,
    },
  ],
})
