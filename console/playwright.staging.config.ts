import { defineConfig, devices } from "@playwright/test";

// Browser tests against the persistent staging Console. The browser runs on the
// machine that starts the tests (a GitHub-hosted runner), never on the staging host.
const baseURL = process.env.SBX_STAGING_CONSOLE_URL ?? "https://staging.sbx-agent.com";
if (/^https?:\/\/(www\.)?sbx-agent\.com/.test(baseURL)) {
  throw new Error("Refusing to run staging browser tests against production");
}

export default defineConfig({
  testDir: "e2e-staging",
  testMatch: "**/*.e2e.ts",
  outputDir: "test-results-staging",
  workers: 1,
  retries: 1,
  timeout: 90_000,
  expect: { timeout: 20_000 },
  reporter: process.env.CI ? [["github"], ["html", { open: "never", outputFolder: "playwright-report-staging" }]] : [["list"]],
  use: { baseURL, trace: "retain-on-failure", screenshot: "only-on-failure", locale: "en-US" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
