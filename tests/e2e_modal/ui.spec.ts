import fs from "node:fs";
import path from "node:path";
import { expect, test, type Page, type Response } from "@playwright/test";

// Host-run gate: the web console against a deployed control plane via the
// local proxy in serve_ui.py. Needs a real bearer key in SBX_API_KEY.
const ART = process.env.SBX_UI_ARTIFACTS || path.join(__dirname, "artifacts");
const KEY = process.env.SBX_V1_API_KEY || process.env.SBX_API_KEY || "";
const NAME = process.env.SBX_UI_TITLE || `ui-gate-${Date.now()}`;
const TURN1 =
  process.env.SBX_UI_TURN1 ||
  'Work in the current directory. Create hello.py with def hello(): return "bye"  # INTENTIONAL BUG and test_hello.py unittest that asserts hello() == "hello". Run python test_hello.py (must fail). Do not fix the bug. Reply with UI_TURN1_DONE';
const TURN2 =
  process.env.SBX_UI_TURN2 ||
  'Fix hello.py so hello() returns "hello". Run python test_hello.py. Tests must pass. Reply with UI_TURN2_DONE';

function shot(page: Page, name: string) {
  fs.mkdirSync(ART, { recursive: true });
  return page.screenshot({ path: path.join(ART, name), fullPage: true });
}

function runBlock(page: Page, id: string) {
  return page.locator(`[data-testid=run][data-run="${id}"]`);
}

test("console: two runs on one agent, then closed is read-only without 5xx", async ({ page }) => {
  test.skip(!KEY, "SBX_API_KEY (or SBX_V1_API_KEY) is required");
  const statuses: number[] = [];
  page.on("response", (res: Response) => {
    statuses.push(res.status());
  });

  await page.goto("/");
  await page.getByTestId("connect-key").fill(KEY);
  await page.getByTestId("connect-submit").click();
  await expect(page.getByTestId("app-ready")).toBeVisible();
  await shot(page, "ui_01_ready.png");

  await page.goto("/#/agents/new");
  await page.getByTestId("f-prompt").fill(TURN1);
  await page.getByTestId("f-name").fill(NAME);
  await page.getByTestId("create-agent").click();
  await expect(page.getByTestId("agent-title")).toHaveText(NAME, { timeout: 90_000 });
  await expect(runBlock(page, "run-1")).toHaveAttribute("data-status", "FINISHED", {
    timeout: 420_000,
  });
  await expect(runBlock(page, "run-1").getByTestId("user-message")).toContainText("INTENTIONAL BUG");
  await expect(runBlock(page, "run-1").getByTestId("agent-message").first()).toBeVisible();
  await shot(page, "ui_02_run1.png");

  await page.getByTestId("composer").fill(TURN2);
  await page.getByTestId("send").click();
  await expect(runBlock(page, "run-2")).toHaveAttribute("data-status", "FINISHED", {
    timeout: 420_000,
  });
  await expect(runBlock(page, "run-2").getByTestId("user-message")).toContainText("Fix hello.py");
  await shot(page, "ui_03_run2.png");

  await page.getByTestId("close-agent").click();
  await page.getByTestId("confirm-ok").click();
  await expect(page.getByTestId("readonly-banner")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("composer")).toBeDisabled();
  await expect(page.getByTestId("agent-status")).toHaveAttribute("data-status", "closed");
  await expect(page.getByTestId("run")).toHaveCount(2);
  await shot(page, "ui_04_closed_readonly.png");

  expect(statuses.filter((s) => s >= 500)).toEqual([]);
});
