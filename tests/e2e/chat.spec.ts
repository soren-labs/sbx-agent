import fs from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const ART = path.join(__dirname, "artifacts");

function shot(page: Page, name: string) {
  fs.mkdirSync(ART, { recursive: true });
  return page.screenshot({
    path: path.join(ART, name),
    fullPage: true,
  });
}

test.describe.configure({ mode: "serial" });

test.describe("WP1-D chat page against mock_api", () => {
  test("create, stream, expand, reload, send, stop, close, 429", async ({ page, request }) => {
    const sseLastEventIds: string[] = [];
    page.on("request", (req) => {
      if (req.url().includes("/events")) {
        sseLastEventIds.push(req.headers()["last-event-id"] ?? "");
      }
    });

    await page.goto("/");
    await expect(page.getByTestId("app-ready")).toBeVisible();
    await shot(page, "01_home.png");

    const title = `e2e-${Date.now()}`;
    await page.getByTestId("new-title").fill(title);
    await page.getByTestId("new-model").selectOption("gpt-5.6-luna");
    await page.getByTestId("new-session").click();

    await expect(page.getByTestId("cold-start")).toBeVisible();
    await shot(page, "02_cold_start.png");

    await expect(page.getByTestId("session-title")).toHaveText(title);
    await expect(page.getByTestId("session-status")).toHaveText("空闲", { timeout: 15_000 });
    await shot(page, "03_session_idle.png");

    await expect(page.getByTestId("agent-message")).toBeVisible({ timeout: 25_000 });
    await expect(page.getByTestId("command-block")).toBeVisible();
    await expect(page.getByTestId("file-block")).toBeVisible();
    await expect(page.getByTestId("reasoning-block")).toBeVisible();
    await shot(page, "04_stream_rendered.png");

    await page.getByTestId("command-block").locator("summary").click();
    await expect(page.getByTestId("command-output")).toBeVisible();
    await shot(page, "05_command_expanded.png");

    await page.getByTestId("file-block").locator("summary").click();
    await expect(page.getByTestId("file-list")).toBeVisible();
    await expect(page.getByTestId("file-list")).toContainText("hello.txt");
    await shot(page, "06_file_change_expanded.png");

    await page.getByTestId("reasoning-block").locator("summary").click();
    await expect(page.getByTestId("reasoning-block")).toContainText("I will write");
    await shot(page, "07_reasoning_expanded.png");

    const agentCount = await page.getByTestId("agent-message").count();
    const commandCount = await page.getByTestId("command-block").count();
    const fileCount = await page.getByTestId("file-block").count();
    expect(agentCount).toBeGreaterThan(0);

    await page.reload();
    await expect(page.getByTestId("session-title")).toHaveText(title);
    await expect(page.getByTestId("agent-message")).toHaveCount(agentCount, { timeout: 25_000 });
    await expect(page.getByTestId("command-block")).toHaveCount(commandCount);
    await expect(page.getByTestId("file-block")).toHaveCount(fileCount);
    await shot(page, "08_reload_no_dup.png");

    expect(sseLastEventIds.some((id) => id !== "")).toBeTruthy();

    await page.getByTestId("composer").fill("Continue the workspace file.");
    await page.getByTestId("send").click();
    await expect(page.getByTestId("user-message")).toHaveCount(1);
    await expect(page.getByTestId("user-message")).toContainText("Continue the workspace file.");
    await expect(page.getByTestId("stop-turn")).toBeVisible();
    await shot(page, "09_send_running.png");

    await page.getByTestId("composer").fill("this should 409");
    await page.getByTestId("send").click();
    await expect(page.getByTestId("error-banner")).toContainText("409");
    await shot(page, "10_conflict_409.png");

    await expect(page.getByTestId("stop-turn")).toHaveCount(0, { timeout: 20_000 });
    await expect(page.getByTestId("session-cost")).not.toHaveText("$0.00");
    await expect(page.getByTestId("session-usage")).not.toHaveText("in 0 · cache 0 · out 0");
    await shot(page, "11_usage_cost.png");

    await page.getByTestId("composer").fill("stop me");
    await page.getByTestId("send").click();
    await expect(page.getByTestId("stop-turn")).toBeVisible();
    await page.getByTestId("stop-turn").click();
    await expect(page.getByTestId("stop-turn")).toHaveCount(0, { timeout: 5_000 });
    await expect(page.getByTestId("session-status")).not.toHaveText("进行中");
    await shot(page, "12_stopped.png");

    await page.getByTestId("close-session").click();
    await expect(page.getByTestId("readonly-banner")).toBeVisible();
    await expect(page.getByTestId("composer")).toBeDisabled();
    await shot(page, "13_closed_readonly.png");

    await page.locator('[data-testid=session-item][data-id=seed-timeout]').click();
    await expect(page.getByTestId("session-title")).toHaveText("已超时 · 只读历史");
    await expect(page.getByTestId("readonly-banner")).toContainText("已超时");
    await expect(page.getByTestId("composer")).toBeDisabled();
    await expect(page.getByTestId("agent-message").first()).toBeVisible({ timeout: 25_000 });
    await shot(page, "14_timed_out_history.png");

    await page.locator('[data-testid=session-item][data-id=seed-lost]').click();
    await expect(page.getByTestId("session-title")).toHaveText("已丢失 · 只读历史");
    await expect(page.getByTestId("readonly-banner")).toContainText("已丢失");
    await shot(page, "15_lost_history.png");

    await page.emulateMedia({ colorScheme: "light" });
    await shot(page, "16_light_scheme.png");
    await page.emulateMedia({ colorScheme: "dark" });

    await page.getByTestId("new-title").fill("limit-a");
    await page.getByTestId("new-session").click();
    await expect(page.getByTestId("session-title")).toHaveText("limit-a");
    await page.getByTestId("new-title").fill("limit-b");
    await page.getByTestId("new-session").click();
    await expect(page.getByTestId("session-title")).toHaveText("limit-b");
    await page.getByTestId("new-title").fill("limit-c");
    await page.getByTestId("new-session").click();
    await expect(page.getByTestId("error-banner")).toContainText("429");
    await shot(page, "17_concurrency_429.png");

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

  test("401 friendly copy without credentials", async ({ browser }) => {
    const context = await browser.newContext({
      baseURL: "http://127.0.0.1:8787",
      extraHTTPHeaders: {},
      colorScheme: "dark",
      viewport: { width: 1280, height: 800 },
    });
    const page = await context.newPage();
    await page.addInitScript(() => {
      Object.assign(window, { SBX_API_USER: "wrong", SBX_API_PASSWORD: "wrong" });
    });
    await page.goto("/");
    await expect(page.getByTestId("error-banner")).toContainText("401");
    await shot(page, "18_unauthorized_401.png");
    await context.close();
  });
});

test("mock_api smoke: create session and read it back", async ({ request }) => {
  const created = await request.post("/api/sessions", {
    data: { title: "e2e-smoke", model: "gpt-5" },
  });
  expect(created.status()).toBe(201);
  const body = await created.json();
  expect(body.session_id).toBeTruthy();

  const got = await request.get(`/api/sessions/${body.session_id}`);
  expect(got.status()).toBe(200);
  const session = await got.json();
  expect(session.id).toBe(body.session_id);
  expect(session.title).toBe("e2e-smoke");
  expect(["creating", "idle"]).toContain(session.status);
  await request.delete(`/api/sessions/${body.session_id}`);
});
