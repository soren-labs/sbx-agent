import fs from "node:fs";
import path from "node:path";
import { expect, type Page, type Request } from "@playwright/test";

export const ART = path.join(__dirname, "artifacts");

export function shot(page: Page, name: string) {
  fs.mkdirSync(ART, { recursive: true });
  return page.screenshot({
    path: path.join(ART, name),
    fullPage: true,
  });
}

export function trackLastEventIds(page: Page, bucket: string[]) {
  page.on("request", (req: Request) => {
    if (req.url().includes("/events")) {
      bucket.push(req.headers()["last-event-id"] ?? "");
    }
  });
}

export async function waitAppReady(page: Page) {
  await page.goto("/");
  await expect(page.getByTestId("app-ready")).toBeVisible();
}

export async function waitIdle(page: Page, timeout = 30_000) {
  await expect(page.getByTestId("session-status")).toHaveText("空闲", { timeout });
}

export async function openSessionByTitle(page: Page, title: string) {
  const item = page.locator('[data-testid=session-item]', { hasText: title }).first();
  await expect(item).toBeVisible({ timeout: 15_000 });
  await item.click();
  await expect(page.getByTestId("session-title")).toHaveText(title);
}
