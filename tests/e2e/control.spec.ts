import { expect, test } from "@playwright/test";
import {
  openSessionByTitle,
  shot,
  trackLastEventIds,
  waitAppReady,
  waitIdle,
} from "./helpers";

test.describe.configure({ mode: "serial" });

const ctx = {
  title: `wp2g-${Date.now()}`,
};

test.describe("WP2-G Playwright vs local control + fake_codex", () => {
  test.beforeAll(async ({ request }) => {
    const leftover = (await (await request.get("/api/sessions")).json()) as Array<{
      id: string;
      status: string;
    }>;
    for (const sess of leftover) {
      if (["creating", "idle", "running"].includes(sess.status)) {
        await request.delete(`/api/sessions/${sess.id}`);
      }
    }
  });

  test("1. Basic Auth login shows an empty session list", async ({ page }) => {
    await waitAppReady(page);
    await expect(page.getByTestId("empty-sessions")).toBeVisible();
    await expect(page.getByTestId("session-item")).toHaveCount(0);
    await shot(page, "wp2g_01_login_empty.png");
  });

  test("2. create session: creating hint then idle", async ({ page }) => {
    await waitAppReady(page);
    await page.route("**/api/sessions", async (route) => {
      const url = new URL(route.request().url());
      if (route.request().method() !== "POST" || url.pathname !== "/api/sessions") {
        await route.continue();
        return;
      }
      const response = await route.fetch();
      await new Promise((r) => setTimeout(r, 700));
      await route.fulfill({ response });
    });

    await page.getByTestId("new-title").fill(ctx.title);
    await page.getByTestId("new-model").selectOption("gpt-5.6-luna");
    await page.getByTestId("new-session").click();
    await expect(page.getByTestId("cold-start")).toBeVisible();
    await expect(page.getByTestId("new-session")).toHaveText("创建中…");
    await shot(page, "wp2g_02_creating.png");

    await expect(page.getByTestId("session-title")).toHaveText(ctx.title, { timeout: 30_000 });
    await waitIdle(page);
    await expect(page.getByTestId("cold-start")).toHaveCount(0);
    await shot(page, "wp2g_03_idle.png");
  });

  test("3. send message: stream, collapsible command/file, usage and cost", async ({
    page,
  }) => {
    await waitAppReady(page);
    await openSessionByTitle(page, ctx.title);
    await waitIdle(page);

    await page.getByTestId("composer").fill("Write hello.txt in the workspace.");
    await page.getByTestId("send").click();
    await expect(page.getByTestId("user-message")).toContainText("Write hello.txt");
    await expect(page.getByTestId("agent-message")).toBeVisible({ timeout: 25_000 });
    await expect(page.getByTestId("agent-message")).toContainText("Created hello.txt");
    await expect(page.getByTestId("command-block")).toBeVisible();
    await expect(page.getByTestId("file-block")).toBeVisible();
    await shot(page, "wp2g_04_stream.png");

    await page.getByTestId("command-block").locator("summary").click();
    await expect(page.getByTestId("command-output")).toBeVisible();
    await shot(page, "wp2g_05_command_expanded.png");

    await page.getByTestId("file-block").locator("summary").click();
    await expect(page.getByTestId("file-list")).toBeVisible();
    await expect(page.getByTestId("file-list")).toContainText("hello.txt");
    await shot(page, "wp2g_06_file_expanded.png");

    await waitIdle(page);
    await expect(page.getByTestId("session-usage")).not.toHaveText("in 0 · cache 0 · out 0");
    await expect(page.getByTestId("session-cost")).not.toHaveText("$0.00");
    await shot(page, "wp2g_07_usage_cost.png");
  });

  test("4. second turn resume keeps history", async ({ page }) => {
    await waitAppReady(page);
    await openSessionByTitle(page, ctx.title);
    await waitIdle(page);

    await expect(page.getByTestId("agent-message")).toContainText("Created hello.txt");
    await page.getByTestId("composer").fill("Continue the workspace file.");
    await page.getByTestId("send").click();
    await expect(page.getByTestId("user-message")).toHaveCount(2);
    await expect(page.getByTestId("agent-message")).toHaveCount(2, { timeout: 25_000 });
    await expect(page.getByTestId("agent-message").nth(1)).toContainText("Appended a resume line");
    await expect(page.getByTestId("agent-message").first()).toContainText("Created hello.txt");
    await expect(page.getByTestId("user-message").first()).toContainText("Write hello.txt");
    await waitIdle(page);
    await shot(page, "wp2g_08_resume_history.png");
  });

  test("5. hang then stop returns idle within 5s", async ({ page }) => {
    await waitAppReady(page);
    await openSessionByTitle(page, ctx.title);
    await waitIdle(page);

    await page.getByTestId("composer").fill("hang");
    await page.getByTestId("send").click();
    await expect(page.getByTestId("stop-turn")).toBeVisible({ timeout: 10_000 });
    const t0 = Date.now();
    await page.getByTestId("stop-turn").click();
    await expect(page.getByTestId("session-status")).toHaveText("空闲", { timeout: 5_000 });
    expect(Date.now() - t0).toBeLessThan(5_000);
    await expect(page.getByTestId("stop-turn")).toHaveCount(0);
    await shot(page, "wp2g_09_hang_stop.png");
  });

  test("6. reload does not drop or duplicate events (Last-Event-ID)", async ({ page }) => {
    const sseLastEventIds: string[] = [];
    trackLastEventIds(page, sseLastEventIds);

    await waitAppReady(page);
    await openSessionByTitle(page, ctx.title);

    await expect(page.getByTestId("agent-message")).toHaveCount(2, { timeout: 25_000 });
    const agentCount = await page.getByTestId("agent-message").count();
    const commandCount = await page.getByTestId("command-block").count();
    const fileCount = await page.getByTestId("file-block").count();
    const userCount = await page.getByTestId("user-message").count();

    await page.reload();
    await expect(page.getByTestId("session-title")).toHaveText(ctx.title);
    await expect(page.getByTestId("agent-message")).toHaveCount(agentCount, { timeout: 25_000 });
    await expect(page.getByTestId("command-block")).toHaveCount(commandCount);
    await expect(page.getByTestId("file-block")).toHaveCount(fileCount);
    await expect(page.getByTestId("user-message")).toHaveCount(userCount);

    const resumed = page.waitForRequest(
      (req) => req.url().includes("/events") && Boolean(req.headers()["last-event-id"]),
      { timeout: 15_000 },
    );
    await page.evaluate(() => window.dispatchEvent(new Event("offline")));
    await resumed;
    await expect(page.getByTestId("agent-message")).toHaveCount(agentCount, { timeout: 20_000 });
    await expect(page.getByTestId("command-block")).toHaveCount(commandCount);
    expect(sseLastEventIds.some((id) => id !== "")).toBeTruthy();
    await shot(page, "wp2g_10_reload.png");
  });

  test("7. close session: list closed, detail read-only", async ({ page }) => {
    await waitAppReady(page);
    await openSessionByTitle(page, ctx.title);
    await page.getByTestId("close-session").click();
    await expect(page.getByTestId("readonly-banner")).toBeVisible();
    await expect(page.getByTestId("composer")).toBeDisabled();
    await expect(page.getByTestId("session-status")).toHaveText("已关闭");
    const closed = page.locator(`[data-testid=session-item]`, { hasText: ctx.title });
    await expect(closed).toHaveAttribute("data-status", "closed");
    await shot(page, "wp2g_11_closed_readonly.png");
  });

  test("8. third live session shows friendly 429 copy", async ({ page }) => {
    await waitAppReady(page);
    await page.getByTestId("new-title").fill("limit-a");
    await page.getByTestId("new-session").click();
    await expect(page.getByTestId("session-title")).toHaveText("limit-a", { timeout: 30_000 });
    await waitIdle(page);

    await page.getByTestId("new-title").fill("limit-b");
    await page.getByTestId("new-session").click();
    await expect(page.getByTestId("session-title")).toHaveText("limit-b", { timeout: 30_000 });
    await waitIdle(page);

    await page.getByTestId("new-title").fill("limit-c");
    await page.getByTestId("new-session").click();
    await expect(page.getByTestId("error-banner")).toContainText("429");
    await expect(page.getByTestId("error-banner")).toContainText("上限");
    await shot(page, "wp2g_12_concurrency_429.png");
  });
});
