import { defineConfig } from '@playwright/test';

// E2E runs against the running app (FastAPI serves the built React UI on :8000).
// Uses the system-installed Google Chrome so no browser download is needed.
export default defineConfig({
  testDir: './e2e',
  // Generous timeouts: the single-process backend makes blocking yfinance calls,
  // and yfinance is frequently rate-limited, so one slow page can briefly stall
  // the server. Riding it out keeps the suite from flaking on upstream latency.
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,            // single worker — shared paper-account state + gentle on yfinance
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:8000',
    channel: 'chrome',
    headless: true,
    viewport: { width: 1440, height: 900 },
    actionTimeout: 15_000,
    navigationTimeout: 60_000,
  },
  webServer: {
    command: 'cd .. && DISABLE_SCHEDULER=1 .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --log-level warning',
    url: 'http://127.0.0.1:8000/bot/status',
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
