import { expect, test, type Page } from "@playwright/test";
import { connectInference, signInViaApi } from "./support";

// Issue #227: the workspace chain (files, terminal, live changes, review link) on a real
// local runtime behind the fake-provider control plane, at the audited desktop size.
test.use({
  viewport: { width: 1440, height: 900 },
  // Evidence runs record the whole chain: SBX_E2E_VIDEO=1 npx playwright test e2e/workspace.e2e.ts
  video: process.env.SBX_E2E_VIDEO ? { mode: "on", size: { width: 1440, height: 900 } } : "off",
});

async function startSession(page: Page, prefix: string): Promise<{ id: string; csrf: string }> {
  const user = await signInViaApi(page, prefix);
  await connectInference(page, user);
  await page.goto("/");
  const composer = page.getByRole("form", { name: "New Session" });
  await composer.getByRole("textbox", { name: "Task" }).fill("workspace audit [write:existing.txt=hello]");
  await composer.getByRole("button", { name: "Start Session" }).click();
  await expect(page).toHaveURL(/\/sessions\/[^/]+/);
  await expect(page.getByText("ACK: workspace audit")).toBeVisible({ timeout: 45_000 });
  return { id: new URL(page.url()).pathname.split("/")[2], csrf: user.csrf };
}

const tab = (page: Page, name: string) => page.getByRole("tab", { name, exact: true });

test("files, terminal, live changes and review link work as one chain", async ({ page }, info) => {
  const { id, csrf } = await startSession(page, "workspace");
  const shot = (name: string) => page.screenshot({ path: info.outputPath(`${name}.png`) });

  // AUD04: a missing path opens a blank editor and saving creates it.
  await page.goto(`/sessions/${id}/files`);
  await page.getByLabel("Open or create file").fill("notes/created-in-browser.txt");
  await page.getByRole("button", { name: "Open", exact: true }).click();
  await expect(page.getByTestId("files-new-hint")).toBeVisible();
  await page.getByRole("textbox", { name: "notes/created-in-browser.txt", exact: true }).fill("made in the browser\n");
  await shot("aud04-new-file-editor");
  await page.getByRole("button", { name: "Create file" }).click();
  await expect(page.getByRole("button", { name: "Edit" })).toBeVisible();
  await expect(page.getByLabel("File viewer")).toContainText("made in the browser");
  await shot("aud04-created");

  // Changes is opened once so its live observation is cached before the terminal runs.
  await tab(page, "Changes").click();
  await page.getByLabel("ChangeSet").selectOption("live");
  await expect(page.getByRole("list", { name: "Live changes" })).toContainText("created-in-browser.txt");

  // AUD08: colours and control sequences are rendered, never shown raw.
  await tab(page, "Terminal").click();
  await page.getByRole("button", { name: "Start terminal" }).click();
  const term = page.getByTestId("terminal-screen");
  await expect(term).toBeVisible();
  const input = page.getByLabel("Command input");
  await input.fill(`printf '\\033[31mRED-227\\033[0m 界面\\nprogress 10%%\\rprogress 100%%\\n'; touch from-terminal.txt; git status --short`);
  await page.locator(".term-tab__input").getByRole("button", { name: "Send" }).click();
  // The carriage return overwrote "progress 10%" in place: one line, the final value.
  await expect(term.locator(".terminal__line", { hasText: /^progress 100%\s*$/ })).toHaveCount(1);
  await expect(term).toContainText("from-terminal.txt");
  await expect(term.locator(".term-fg-red", { hasText: "RED-227" })).toBeVisible();
  await expect(term.locator(".terminal__line", { hasText: /^RED-227 界面\s*$/ })).toHaveCount(1);
  await expect(term).not.toContainText("\u001b");
  await expect(term).not.toContainText("[?2004h");
  await shot("aud08-terminal-rendered");

  // AUD09: switching panels keeps the running terminal and its output; no second shell.
  await tab(page, "Files").click();
  await tab(page, "Terminal").click();
  await expect(term.locator(".terminal__line", { hasText: /^progress 100%\s*$/ })).toHaveCount(1);
  await expect(page.getByRole("button", { name: "Start terminal" })).toHaveCount(0);
  const listed = await page.request.get(`/api/sessions/${id}/terminals`);
  expect((await listed.json()).items).toHaveLength(1);
  await shot("aud09-terminal-after-switch");

  // AUD05: Live changes shows the terminal's file without a page reload.
  await tab(page, "Changes").click();
  await expect(page.getByRole("list", { name: "Live changes" })).toContainText("from-terminal.txt");
  await shot("aud05-live-changes-after-terminal");

  // AUD11: Review changes from a workspace sub-route lands on the session's changes route.
  await page.goto(`/sessions/${id}/files`);
  await page.getByRole("link", { name: "Review changes" }).click();
  await expect(page).toHaveURL(new RegExp(`/sessions/${id}/changes$`));
  await expect(page.getByText("Page not found")).toHaveCount(0);
  await shot("aud11-review-changes");

  // Narrow screens reach the same terminal through the Workspace pane.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/sessions/${id}/terminal`);
  await expect(term.locator(".terminal__line", { hasText: /^progress 100%\s*$/ })).toHaveCount(1);
  await shot("aud09-terminal-narrow");
  await page.setViewportSize({ width: 1440, height: 900 });

  // Clean up the shell this test started (capture below also quiesces terminals).
  await tab(page, "Terminal").click();
  await page.getByRole("button", { name: "Close", exact: true }).click();
  await expect(page.getByRole("button", { name: "Start terminal" })).toBeVisible();

  // AUD02: a file over the read limit opens as a read-only preview; nothing to save back.
  const big = "a".repeat(1_000_019 - 16) + "TAIL-MARKER-227\n";
  const put = await page.request.put(`/api/sessions/${id}/files`, {
    headers: { "X-CSRF-Token": csrf, "Idempotency-Key": crypto.randomUUID() },
    data: { path: "big.txt", content: big, expected_digest: "absent" },
  });
  expect(put.status()).toBe(200);
  await tab(page, "Files").click();
  await page.getByRole("button", { name: /big\.txt/ }).click();
  await expect(page.getByTestId("files-readonly")).toContainText("Large file: read-only preview");
  await expect(page.getByTestId("files-readonly")).toContainText("of 1,000,019 bytes");
  await expect(page.locator(".files.editor").getByRole("button", { name: "Edit" })).toHaveCount(0);
  await shot("aud02-large-file-readonly");

  // AUD19: a captured ChangeSet labels new files A and numbers only source lines.
  await tab(page, "Changes").click();
  await page.getByRole("button", { name: "Capture ChangeSet" }).click();
  await expect(page.getByLabel("ChangeSet")).not.toHaveValue("live", { timeout: 30_000 });
  await expect(page.getByText(/explicit · ready · 4 files/)).toBeVisible({ timeout: 30_000 });
  const files = page.getByRole("list", { name: /files/i });
  // Before the patch is parsed the badges are neutral, never a guessed "M".
  await expect(files.locator(".chg-badge", { hasText: "M" })).toHaveCount(0);
  await page.getByRole("button", { name: "Show diff" }).click();
  await expect(page.getByLabel("Diff")).toBeVisible({ timeout: 30_000 });
  await expect(files.getByRole("button", { name: /from-terminal\.txt/ }).locator(".chg-badge")).toHaveText("A");
  await expect(files.locator(".chg-badge", { hasText: "M" })).toHaveCount(0);
  await files.getByRole("button", { name: /existing\.txt/ }).click();
  const diff = page.getByLabel("Diff");
  const header = diff.locator(".diff__line", { hasText: "+++ b/existing.txt" });
  await expect(header.locator(".diff__no").nth(1)).toHaveText("");
  await expect(diff.locator(".diff__line--add", { hasText: "+hello" }).locator(".diff__no").nth(1)).toHaveText("1");
  await shot("aud19-changeset-diff");
});
