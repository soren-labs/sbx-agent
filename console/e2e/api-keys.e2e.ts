import { expect, test, type Page } from "@playwright/test";
import { signInViaApi, type SignedIn } from "./support";

// #230: scoped API keys, retry-safe issuance and immediate revocation, against the real
// control plane. Keys here are throwaway test keys in a disposable database; the one-time
// value is still blurred (and the blur verified) before any screenshot or video frame.
test.use({
  viewport: { width: 1440, height: 900 },
  // Evidence runs record the chain: SBX_E2E_VIDEO=1 npx playwright test e2e/api-keys.e2e.ts
  video: process.env.SBX_E2E_VIDEO ? { mode: "on", size: { width: 1440, height: 900 } } : "off",
});

const shot = (page: Page, name: string) => page.screenshot({ path: test.info().outputPath(`${name}.png`) });

/** Blur the one-time key and prove the rule applies before anything is captured. */
async function blurSecrets(page: Page) {
  await page.addStyleTag({ content: ".key-once { filter: blur(6px) !important; }" });
  const filter = await page.evaluate(() => {
    const probe = document.createElement("code");
    probe.className = "key-once";
    document.body.appendChild(probe);
    const f = getComputedStyle(probe).filter;
    probe.remove();
    return f;
  });
  expect(filter).toContain("blur");
}

async function createKey(page: Page, name: string, access: RegExp): Promise<string> {
  await page.getByRole("button", { name: "Create key" }).first().click();
  const dialog = page.getByRole("dialog", { name: "Create key" });
  await dialog.getByLabel("Key name").fill(name);
  await dialog.getByRole("radio", { name: access }).check();
  await dialog.getByRole("button", { name: "Create key" }).click();
  const reveal = page.getByTestId("key-reveal");
  await expect(reveal).toBeVisible();
  expect(await reveal.locator(".key-once").evaluate((el) => getComputedStyle(el).filter)).toContain("blur");
  const key = (await reveal.locator(".key-once").textContent())!.trim();
  expect(key).toMatch(/^sbx_key_/);
  return key;
}

const bearer = (key: string) => ({ Authorization: `Bearer ${key}`, "Idempotency-Key": crypto.randomUUID() });

async function project(page: Page, user: SignedIn, key: string, slug: string) {
  return page.request.post(`/api/workspaces/${user.workspaceId}/projects`, {
    headers: bearer(key),
    data: { slug, name: slug, spec: { repository: null } },
  });
}

test("scoped keys: read queries, write mutates, full manages keys, revoke is immediate (AUD21)", async ({ page }) => {
  const user = await signInViaApi(page, "keys230");
  await page.goto("/settings?tab=keys");
  await blurSecrets(page);

  await page.getByRole("button", { name: "Create key" }).first().click();
  const dialog = page.getByRole("dialog", { name: "Create key" });
  await expect(dialog.getByRole("radio", { name: /Read & write/ })).toBeChecked();
  await shot(page, "aud21-scope-picker");
  await dialog.getByRole("button", { name: "Cancel" }).click();

  const read = await createKey(page, "dashboard (read)", /Read only/);
  await expect(page.getByTestId("key-reveal")).toContainText("Access: Read only.");
  await shot(page, "aud21-reveal-read-blurred");
  await page.getByRole("button", { name: "I have copied it" }).click();
  const write = await createKey(page, "ci (write)", /Read & write/);
  await page.getByRole("button", { name: "I have copied it" }).click();
  const full = await createKey(page, "key admin (full)", /Full access/);
  await page.getByRole("button", { name: "I have copied it" }).click();
  // After dismissal and reload no plaintext is anywhere on the page or in the list API.
  await page.reload();
  const list = page.getByRole("list", { name: "API keys" });
  await expect(list.getByRole("listitem", { name: "dashboard (read)" })).toContainText("Read only");
  await expect(list.getByRole("listitem", { name: "ci (write)" })).toContainText("Read & write");
  await expect(list.getByRole("listitem", { name: "key admin (full)" })).toContainText("Full access");
  for (const k of [read, write, full]) {
    expect(await page.content()).not.toContain(k);
    expect(await (await page.request.get("/api/api-keys")).text()).not.toContain(k);
  }
  await shot(page, "aud21-list-scopes");

  const projects = `/api/workspaces/${user.workspaceId}/projects`;
  expect((await page.request.get(projects, { headers: bearer(read) })).status()).toBe(200);
  expect((await project(page, user, read, "by-read")).status()).toBe(403);
  expect((await project(page, user, write, "by-write")).status()).toBe(201);
  const byWrite = await page.request.post("/api/api-keys", { headers: bearer(write), data: { name: "x", scopes: ["read"] } });
  expect(byWrite.status()).toBe(403);
  const child = await page.request.post("/api/api-keys", { headers: bearer(full), data: { name: "child", scopes: ["read"] } });
  expect(child.status()).toBe(201);
  const revokedChild = await page.request.delete(`/api/api-keys/${(await child.json()).id}`, { headers: bearer(full) });
  expect(revokedChild.status()).toBe(200);

  // Revoke read and write keys in the UI; both are refused at once.
  await page.reload();
  for (const name of ["dashboard (read)", "ci (write)"]) {
    await list.getByRole("listitem", { name }).getByRole("button", { name: "Revoke" }).click();
    const confirm = page.getByRole("dialog", { name: "Revoke API key" });
    await expect(confirm).toContainText("refused immediately");
    await confirm.getByRole("button", { name: "Revoke key" }).click();
    await expect(list.getByRole("listitem", { name })).toContainText("revoked");
  }
  for (const key of [read, write]) {
    expect((await page.request.get(projects, { headers: bearer(key) })).status()).toBe(401);
    expect((await project(page, user, key, `after-${key.slice(-4)}`)).status()).toBe(401);
  }
  await shot(page, "aud21-revoked");
  // Leave nothing usable behind.
  await list.getByRole("listitem", { name: "key admin (full)" }).getByRole("button", { name: "Revoke" }).click();
  await page.getByRole("dialog", { name: "Revoke API key" }).getByRole("button", { name: "Revoke key" }).click();
  await expect(list.getByRole("listitem", { name: "key admin (full)" })).toContainText("revoked");
  expect((await page.request.get(projects, { headers: bearer(full) })).status()).toBe(401);
});

test("a create whose responses are lost is retried into the same single key (AUD20)", async ({ page }) => {
  await signInViaApi(page, "lost230");
  await page.goto("/settings?tab=keys");
  await blurSecrets(page);
  // The server receives and commits each attempt; the browser never sees the response.
  const keys: string[] = [];
  let lose = 3; // the client's first attempt and its two automatic retries
  await page.route("**/api/api-keys", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    keys.push(route.request().headers()["idempotency-key"]);
    const response = await route.fetch();
    expect(response.status()).toBe(201);
    if (lose-- > 0) return route.abort("connectionreset");
    return route.fulfill({ response });
  });

  await page.getByRole("button", { name: "Create key" }).first().click();
  const dialog = page.getByRole("dialog", { name: "Create key" });
  await dialog.getByLabel("Key name").fill("nightly");
  await dialog.getByRole("button", { name: "Create key" }).click();
  await expect(dialog.getByText("The key may have been created")).toBeVisible();
  await expect(dialog.getByLabel("Key name")).toBeDisabled();
  await shot(page, "aud20-outcome-unknown");
  await dialog.getByRole("button", { name: "Try again" }).click();
  const reveal = page.getByTestId("key-reveal");
  await expect(reveal).toBeVisible();
  expect(await reveal.locator(".key-once").evaluate((el) => getComputedStyle(el).filter)).toContain("blur");
  const key = (await reveal.locator(".key-once").textContent())!.trim();
  await shot(page, "aud20-same-key-after-retry");

  expect(keys).toHaveLength(4);
  expect(new Set(keys).size).toBe(1);
  const listed = (await (await page.request.get("/api/api-keys")).json()).items;
  expect(listed.filter((k: { name: string }) => k.name === "nightly")).toHaveLength(1);
  expect((await page.request.get("/api/me", { headers: bearer(key) })).status()).toBe(200);
  await page.getByRole("button", { name: "I have copied it" }).click();
  await page.unroute("**/api/api-keys");
  // Clean up: revoke the test key.
  const list = page.getByRole("list", { name: "API keys" });
  await list.getByRole("listitem", { name: "nightly" }).getByRole("button", { name: "Revoke" }).click();
  await page.getByRole("dialog", { name: "Revoke API key" }).getByRole("button", { name: "Revoke key" }).click();
  await expect(list.getByRole("listitem", { name: "nightly" })).toContainText("revoked");
  expect((await page.request.get("/api/me", { headers: bearer(key) })).status()).toBe(401);
});
