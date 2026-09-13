import path from "node:path";
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: path.join(__dirname, "../tests/e2e"),
  testMatch: /.*\.spec\.ts/,
  fullyParallel: false,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:8787",
    extraHTTPHeaders: {
      Authorization: `Basic ${Buffer.from("sbx:sbx").toString("base64")}`,
    },
  },
  webServer: {
    command: "uv run python -m tests.fakes.mock_api --port 8787",
    port: 8787,
    reuseExistingServer: !process.env.CI,
    cwd: path.join(__dirname, ".."),
    timeout: 60_000,
  },
});
