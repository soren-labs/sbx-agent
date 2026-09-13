import path from "node:path";
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: path.join(__dirname, "../tests/e2e"),
  testMatch: /.*\.spec\.ts/,
  fullyParallel: false,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 20_000 },
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:8787",
    extraHTTPHeaders: {
      Authorization: `Basic ${Buffer.from("sbx:sbx").toString("base64")}`,
    },
    viewport: { width: 1280, height: 800 },
    colorScheme: "dark",
  },
  webServer: {
    command: "uv run python -m tests.fakes.mock_api --port 8787",
    port: 8787,
    reuseExistingServer: !process.env.CI,
    cwd: path.join(__dirname, ".."),
    timeout: 60_000,
    env: {
      ...process.env,
      SBX_SSE_INTERVAL_SECONDS: "0.12",
      SBX_SSE_KEEPALIVE_SECONDS: "15",
      SBX_SSE_DROP_FIRST_AFTER: "2",
      SBX_SSE_RETRY_MS: "200",
      SBX_MOCK_CREATE_DELAY_S: "0.8",
      SBX_MOCK_TURN_INTERVAL_SECONDS: "0.35",
    },
  },
});
