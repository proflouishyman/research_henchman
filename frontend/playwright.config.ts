// Playwright configuration for UI smoke tests.
// Runs against the already-running uvicorn server on port 8000.
// Only chromium — avoids multi-browser overhead for local smoke checks.

import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests/e2e',
  // No global retries — fail loudly on structural breakage.
  retries: 0,
  // Run tests sequentially to avoid race conditions on shared server state.
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:8000',
    headless: true,
    // Generous timeout — SPA hydration can take a moment on first load.
    actionTimeout: 10000,
    navigationTimeout: 15000,
  },
  projects: [
    {
      name: 'chromium',
      use: { browserName: 'chromium' },
    },
  ],
  // Do not spin up a webserver — uvicorn is already running.
  reporter: [['list'], ['html', { open: 'never' }]],
})
