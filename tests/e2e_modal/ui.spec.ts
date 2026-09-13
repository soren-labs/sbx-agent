import fs from "node:fs";
import path from "node:path";
import { expect, test, type Page, type Response } from "@playwright/test";

const ART = process.env.SBX_UI_ARTIFACTS || path.join(__dirname, "artifacts");
const TITLE = process.env.SBX_UI_TITLE || `wp2h-ui-${Date.now()}`;
const TURN1 =
  process.env.SBX_UI_TURN1 ||
  'Work in /work. Create hello.py with def hello(): return "bye"  # INTENTIONAL BUG and test_hello.py unittest that asserts hello() == "hello". Run python test_hello.py (must fail). Do not fix the bug. Reply with UI_TURN1_DONE';
const TURN2 =
  process.env.SBX_UI_TURN2 ||
  'Fix hello.py so hello() returns "hello". Run python test_hello.py. Tests must pass. Reply with UI_TURN2_DONE';

function shot(page: Page, name: string) {
  fs.mkdirSync(ART, { recursive: true });
  return page.screenshot({ path: path.join(ART, name), fullPage: true });
}

async function waitIdle(page: Page, timeout = 420_000) {
  await expect(page.getByTestId("session-status")).toHaveText("空闲", { timeout });
}

test("UI two turns then closed is read-only without 500", async ({ page }) => {
  const statuses: number[] = [];
  page.on("response", (res: Response) => {
    statuses.push(res.status());
  });

  await page.goto("/");
  await expect(page.getByTestId("app-ready")).toBeVisible();
  await expect(page.getByTestId("error-banner")).toHaveCount(0);
  await shot(page, "ui_01_ready.png");

  await page.getByTestId("new-title").fill(TITLE);
  await page.getByTestId("new-model").selectOption("gpt-5.6-luna");
  await page.getByTestId("new-session").click();
  await expect(page.getByTestId("session-title")).toHaveText(TITLE, { timeout: 90_000 });
  await waitIdle(page);
  await shot(page, "ui_02_idle.png");

  await page.getByTestId("composer").fill(TURN1);
  await page.getByTestId("send").click();
  await expect(page.getByTestId("user-message")).toContainText("INTENTIONAL BUG", {
    timeout: 30_000,
  });
  await waitIdle(page);
  await expect(page.getByTestId("user-message")).toHaveCount(1);
  await expect(page.getByTestId("agent-message").first()).toBeVisible();
  await shot(page, "ui_03_turn1.png");

  await page.getByTestId("composer").fill(TURN2);
  await page.getByTestId("send").click();
  await expect(page.getByTestId("user-message")).toHaveCount(2, { timeout: 30_000 });
  await waitIdle(page);
  await expect(page.getByTestId("agent-message")).toHaveCount(2, { timeout: 60_000 });
  await shot(page, "ui_04_turn2.png");

  await page.getByTestId("close-session").click();
  await expect(page.getByTestId("readonly-banner")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("composer")).toBeDisabled();
  await expect(page.getByTestId("session-status")).toHaveText("已关闭");
  await expect(page.getByTestId("error-banner")).toHaveCount(0);
  await shot(page, "ui_05_closed_readonly.png");

  expect(statuses.filter((s) => s >= 500)).toEqual([]);
});
