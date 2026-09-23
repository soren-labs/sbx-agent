import path from "node:path";
import { defineConfig } from "@playwright/test";

const repoRoot = path.join(__dirname, "..");
const python = path.join(repoRoot, ".venv", "bin", "python");
// Test-only key for the throwaway local plane (never a real credential).
const key = process.env.SBX_CONSOLE_DEV_KEY || "sbx_e2e_local_only";
const port = 8791;

export default defineConfig({
  testDir: path.join(__dirname, "../tests/e2e"),
  testMatch: /.*\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 20_000 },
  reporter: "list",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    viewport: { width: 1280, height: 800 },
    colorScheme: "dark",
    locale: "en-US",
    acceptDownloads: true,
  },
  projects: [{ name: "console", testMatch: /console\.spec\.ts/ }],
  webServer: {
    // Real control plane (SBX_BACKEND=local) + fake provider CLIs + web/.
    command: `${python} tests/e2e/serve_console.py --port ${port}`,
    url: `http://127.0.0.1:${port}/__dev/info`,
    reuseExistingServer: false,
    cwd: repoRoot,
    timeout: 60_000,
    env: {
      ...process.env,
      PYTHONPATH: repoRoot,
      PYTHONUNBUFFERED: "1",
      SBX_CONSOLE_DEV_KEY: key,
    },
  },
});
