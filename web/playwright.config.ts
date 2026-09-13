import path from "node:path";
import { defineConfig } from "@playwright/test";

const repoRoot = path.join(__dirname, "..");
const python = path.join(repoRoot, ".venv", "bin", "python");
const basic = Buffer.from("sbx:sbx").toString("base64");
const authHeaders = { Authorization: `Basic ${basic}` };

export default defineConfig({
  testDir: path.join(__dirname, "../tests/e2e"),
  testMatch: /.*\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 20_000 },
  reporter: "list",
  use: {
    viewport: { width: 1280, height: 800 },
    colorScheme: "dark",
  },
  projects: [
    {
      name: "mock-api",
      testMatch: /chat\.spec\.ts/,
      use: {
        baseURL: "http://127.0.0.1:8787",
        extraHTTPHeaders: authHeaders,
      },
    },
    {
      name: "local-control",
      testMatch: /control\.spec\.ts/,
      timeout: 120_000,
      use: {
        baseURL: "http://127.0.0.1:8788",
        extraHTTPHeaders: authHeaders,
        httpCredentials: { username: "sbx", password: "sbx" },
      },
    },
  ],
  webServer: [
    {
      command: `${python} -m tests.fakes.mock_api --port 8787`,
      url: "http://127.0.0.1:8787/",
      reuseExistingServer: !process.env.CI,
      cwd: repoRoot,
      timeout: 60_000,
      env: {
        ...process.env,
        PYTHONPATH: repoRoot,
        SBX_SSE_INTERVAL_SECONDS: "0.12",
        SBX_SSE_KEEPALIVE_SECONDS: "15",
        SBX_SSE_DROP_FIRST_AFTER: "2",
        SBX_SSE_RETRY_MS: "200",
        SBX_MOCK_CREATE_DELAY_S: "0.8",
        SBX_MOCK_TURN_INTERVAL_SECONDS: "0.35",
      },
    },
    {
      command: `${python} tests/e2e/serve_local.py --port 8788`,
      url: "http://127.0.0.1:8788/",
      reuseExistingServer: !process.env.CI,
      cwd: repoRoot,
      timeout: 60_000,
      env: {
        ...process.env,
        SBX_BACKEND: "local",
        PYTHONUNBUFFERED: "1",
        PYTHONPATH: repoRoot,
      },
    },
  ],
});
