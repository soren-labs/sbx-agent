import { expect, test } from "@playwright/test";

// Read-only: signs in as stable test user A and reads what the API acceptance
// (tests/e2e_staging/acceptance.py) left behind. It starts no Turn and no compute.
const email = process.env.SBX_STAGING_USER_A_EMAIL ?? "";
const password = process.env.SBX_STAGING_USER_A_PASSWORD ?? "";
const expectedSha = process.env.SBX_STAGING_EXPECT_SHA;

test.beforeAll(() => {
  expect(email, "SBX_STAGING_USER_A_EMAIL is required").not.toBe("");
  expect(password, "SBX_STAGING_USER_A_PASSWORD is required").not.toBe("");
});

test("Console build and API report the same revision", async ({ page, request }) => {
  await page.goto("/login");
  const consoleSha = await page.locator('meta[name="sbx-build-sha"]').getAttribute("content");
  const apiSha = (await (await request.get("/api/healthz")).json()).git_sha;
  expect(consoleSha, "Console and backend must be built from one commit").toBe(apiSha);
  if (expectedSha) expect(apiSha).toBe(expectedSha);
});

test("test user signs in and reads a persisted acceptance Session", async ({ page }) => {
  // Anonymous visitors see the product landing page at /; signing in starts
  // from its header link.
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await page.getByRole("link", { name: "Sign in" }).first().click();
  await expect(page).toHaveURL(/\/login$/);
  const login = page.getByRole("form", { name: "Sign in" });
  await login.locator("#login-email").fill(email);
  await login.locator("#login-password").fill(password);
  await login.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("form", { name: "New Session" })).toBeVisible();

  await page.goto("/sessions");
  const session = page.getByRole("link", { name: /staging-acceptance/ }).first();
  await expect(session).toBeVisible();
  await session.click();
  await expect(page).toHaveURL(/\/sessions\/[^/]+/);
  await expect(page.getByText(/STAGING-OK-[0-9A-F]{8}/).first()).toBeVisible();
});
