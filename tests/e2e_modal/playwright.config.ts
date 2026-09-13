import path from "node:path";
import { defineConfig } from "@playwright/test";

const origin = process.env.SBX_UI_ORIGIN || "http://127.0.0.1:8790";
const user = process.env.SBX_BASIC_USER || process.env.SBX_API_USER || "sbx";
const password = process.env.SBX_BASIC_PASS || process.env.SBX_API_PASSWORD || "sbx";
const artifacts = process.env.SBX_UI_ARTIFACTS || path.join(__dirname, "artifacts");

export default defineConfig({
  testDir: __dirname,
  testMatch: /ui\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 780_000,
  expect: { timeout: 120_000 },
  reporter: "list",
  outputDir: path.join(artifacts, "playwright-output"),
  use: {
    baseURL: origin,
    viewport: { width: 1280, height: 800 },
    colorScheme: "dark",
    trace: "off",
    video: "off",
    screenshot: "off",
    extraHTTPHeaders: {
      Authorization: `Basic ${Buffer.from(`${user}:${password}`).toString("base64")}`,
    },
    httpCredentials: { username: user, password },
  },
});
