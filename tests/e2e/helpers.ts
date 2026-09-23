import fs from "node:fs";
import path from "node:path";
import { expect, type Locator, type Page } from "@playwright/test";

export const ART = path.join(__dirname, "artifacts");

/** Test-only key for the throwaway local plane started by serve_console.py. */
export const KEY = process.env.SBX_CONSOLE_DEV_KEY || "sbx_e2e_local_only";

export function shot(page: Page, name: string, fullPage = false) {
  fs.mkdirSync(ART, { recursive: true });
  // Transient toasts would cover the docs screenshots synced from these files.
  return page.screenshot({
    path: path.join(ART, name),
    fullPage,
    style: ".toasts { display: none !important; }",
  });
}

export async function connect(page: Page, key = KEY) {
  await page.goto("/");
  await expect(page.getByTestId("connect-view")).toBeVisible();
  await page.getByTestId("connect-key").fill(key);
  await page.getByTestId("connect-submit").click();
  await expect(page.getByTestId("app-ready")).toBeVisible();
}

export function run(page: Page, id: string): Locator {
  return page.locator(`[data-testid=run][data-run="${id}"]`);
}

export async function waitRun(page: Page, id: string, status: string, timeout = 45_000) {
  await expect(run(page, id)).toHaveAttribute("data-status", status, { timeout });
}

export async function waitAgent(page: Page, status: string, timeout = 45_000) {
  await expect(page.getByTestId("agent-status")).toHaveAttribute("data-status", status, {
    timeout,
  });
}

/** Fill the New agent form's prompt/name and submit; returns the agent id. */
export async function createAgent(page: Page, prompt: string, name: string) {
  await page.goto("/#/agents/new");
  await expect(page.getByTestId("page-title")).toBeVisible();
  await page.getByTestId("f-prompt").fill(prompt);
  await page.getByTestId("f-name").fill(name);
  await page.getByTestId("create-agent").click();
  await expect(page.getByTestId("agent-title")).toHaveText(name, { timeout: 30_000 });
  return (await page.getByTestId("agent-id").locator("code").innerText()).trim();
}

export async function devInfo(page: Page) {
  const res = await page.request.get("/__dev/info");
  expect(res.ok()).toBeTruthy();
  return (await res.json()) as {
    demo_workspace: { repo: string; base_ref: string; base_sha: string };
  };
}
