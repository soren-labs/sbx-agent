import { expect, test, type Page } from "@playwright/test";
import { connectInference, signInViaApi, type SignedIn } from "./support";

// Issue #228 on a real local runtime behind the fake-provider control plane.
test.use({ viewport: { width: 1440, height: 900 } });

async function start(page: Page, user: SignedIn, task: string): Promise<string> {
  await page.goto("/");
  const composer = page.getByRole("form", { name: "New Session" });
  await composer.getByRole("textbox", { name: "Task" }).fill(task);
  await composer.getByRole("button", { name: "Start Session" }).click();
  await expect(page).toHaveURL(/\/sessions\/[^/]+/);
  return new URL(page.url()).pathname.split("/")[2];
}

function headers(user: SignedIn) {
  return { "X-CSRF-Token": user.csrf, "Idempotency-Key": crypto.randomUUID() };
}

test("queued follow-ups, cancel, history, archive and close behave as one lifecycle", async ({ page }, info) => {
  const shot = (name: string) => page.screenshot({ path: info.outputPath(`${name}.png`) });
  const user = await signInViaApi(page, "lifecycle");
  await connectInference(page, user);
  const id = await start(page, user, "first task [slow:8]");

  // AUD07: a follow-up queued while the first Turn runs; the first is then stopped.
  const composer = page.getByRole("textbox", { name: "Message", exact: true });
  await expect(page.getByText("ACK: first task")).toHaveCount(0);
  await composer.fill("second task");
  await composer.press("Enter");
  await expect(page.getByTestId("turn-status").first()).toBeVisible();
  await page.getByRole("button", { name: "Stop" }).first().click();
  await expect(page.getByText("ACK: second task")).toBeVisible({ timeout: 45_000 });
  const log = page.getByRole("log", { name: "Conversation" });
  await expect(log.getByText("Turn cancelled")).toHaveCount(1);
  await expect(log.locator('[data-testid="turn-status"][data-state="succeeded"]')).toHaveCount(1);
  await expect(log.getByRole("button", { name: "Retry" })).toHaveCount(1);
  await shot("aud07-one-result-per-turn");

  // AUD12: after a reload, Activity shows the committed history, not "No events yet".
  await page.goto(`/sessions/${id}/activity`);
  const timeline = page.getByRole("list", { name: "Events" });
  await expect(timeline.getByRole("listitem").first()).toBeVisible();
  await expect(page.getByText("No events yet.")).toHaveCount(0);
  expect(await timeline.getByRole("listitem").count()).toBeGreaterThan(5);
  await shot("aud12-activity-after-reload");

  // AUD18: Close asks first; "Archive instead" is the reversible path.
  await page.getByRole("button", { name: "Session actions" }).click();
  await page.getByRole("menuitem", { name: "Close session…" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("permanently?");
  await expect(dialog).toContainText("Releases its compute");
  await shot("aud18-close-confirmation");
  await dialog.getByRole("button", { name: "Archive instead" }).click();

  // AUD13: the header says Archived; the default sidebar list no longer shows it.
  await expect(page.getByTestId("session-status")).toHaveText("Archived");
  const aside = page.getByRole("complementary");
  await expect(aside.getByRole("link", { name: /first task/ })).toHaveCount(0);
  await aside.getByRole("button", { name: "Session list options" }).click();
  await page.getByRole("menuitem", { name: "Show archived" }).click();
  await expect(aside.getByRole("link", { name: /first task/ })).toBeVisible();
  await shot("aud13-archived-opt-in");

  // AUD22: once closed, the cancelled Turn no longer offers Retry, and the API agrees.
  await page.getByRole("button", { name: "Session actions" }).click();
  await page.getByRole("menuitem", { name: "Close session…" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Close permanently" }).click();
  await expect(page.getByTestId("session-status")).toHaveText("Closed");
  await page.goto(`/sessions/${id}`);
  await expect(page.getByText("Turn cancelled")).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry" })).toHaveCount(0);
  const turns = await (await page.request.get(`/api/sessions/${id}/turns`)).json();
  const cancelled = turns.items.find((t: { state: string }) => t.state === "cancelled");
  expect(cancelled.actions).not.toContain("retry");
  const retry = await page.request.post(`/api/turns/${cancelled.id}/retries`, { headers: headers(user) });
  expect(retry.status()).toBe(409);
  await shot("aud22-closed-no-retry");
});

test("tool output PostgreSQL cannot store still completes the Turn", async ({ page }, info) => {
  const user = await signInViaApi(page, "unstorable");
  await connectInference(page, user);
  // The fake CLI emits a bash tool whose output holds NUL and an unpaired surrogate.
  await start(page, user, 'odd output [tool-output:"before\\u0000after \\ud800 end"]');
  await expect(page.getByText(/ACK: odd output/)).toBeVisible({ timeout: 45_000 });
  await expect(page.locator('[data-testid="turn-status"][data-state="succeeded"]')).toBeVisible();
  const step = page.getByRole("button", { name: /cat odd\.bin/ });
  if (!(await step.isVisible())) await page.getByRole("button", { name: /Worked/ }).first().click();
  await step.click();
  await expect(page.getByText(/before�after � end/)).toBeVisible();
  await page.screenshot({ path: info.outputPath("aud01-unstorable-output-completed.png") });
});

test("sessions beyond the first page are reachable and searchable", async ({ page }, info) => {
  const user = await signInViaApi(page, "paging");
  await connectInference(page, user);
  // Sessions without a message start no Turn and no compute.
  for (let i = 0; i < 53; i++) {
    const r = await page.request.post(`/api/workspaces/${user.workspaceId}/sessions`, {
      headers: headers(user),
      data: { title: `fixture ${String(i).padStart(2, "0")}`, harness: { provider_id: "opencode", model: "test-model" }, executor: { backend: "local" } },
    });
    expect(r.status(), await r.text()).toBeLessThan(300);
  }
  await page.goto("/sessions");
  const table = page.getByRole("table", { name: "Sessions" });
  await expect(table.getByRole("link")).toHaveCount(50);
  await expect(table.getByRole("link", { name: /fixture 00/ })).toHaveCount(0);
  await page.getByRole("button", { name: "Load more" }).click();
  await expect(table.getByRole("link")).toHaveCount(53);
  await expect(table.getByRole("link", { name: /fixture 00/ })).toBeVisible();
  await page.screenshot({ path: info.outputPath("aud14-load-more.png") });
  await page.locator("#sessions-search").fill("fixture 00");
  await expect(table.getByRole("link")).toHaveCount(1);
  await expect(page).toHaveURL(/q=fixture/);
  await page.screenshot({ path: info.outputPath("aud14-search.png") });
});
