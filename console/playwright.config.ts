import { defineConfig, devices } from "@playwright/test";

// Browser tests for the built Console against a real control plane that uses
// fake providers (tests/e2e_browser/server.py). No cloud credentials are read.
const API_PORT = 8811;
const WEB_PORT = 4173;
const ORIGIN = `http://127.0.0.1:${WEB_PORT}`;
const CI = Boolean(process.env.CI);

export default defineConfig({
  testDir: "e2e",
  testMatch: "**/*.e2e.ts",
  outputDir: "test-results",
  fullyParallel: true,
  forbidOnly: CI,
  retries: CI ? 1 : 0,
  workers: CI ? 2 : undefined,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: CI ? [["github"], ["html", { open: "never" }]] : [["list"]],
  use: {
    baseURL: ORIGIN,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    locale: "en-US",
  },
  metadata: { apiOrigin: `http://127.0.0.1:${API_PORT}` },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: `uv run python -m tests.e2e_browser.server --port ${API_PORT} --origin ${ORIGIN}`,
      cwd: "..",
      url: `http://127.0.0.1:${API_PORT}/readyz`,
      reuseExistingServer: !CI,
      timeout: 120_000,
      gracefulShutdown: { signal: "SIGTERM", timeout: 15_000 },
    },
    {
      // Serves console/dist: run `npm run build` first.
      command: `npm run preview -- --host 127.0.0.1 --port ${WEB_PORT} --strictPort`,
      env: { SBX_API_PROXY_TARGET: `http://127.0.0.1:${API_PORT}` },
      url: ORIGIN,
      reuseExistingServer: !CI,
      timeout: 60_000,
    },
  ],
});
