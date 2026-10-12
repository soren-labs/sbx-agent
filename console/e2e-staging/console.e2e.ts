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

test("Console build and API report the expected revision", async ({ page, request }) => {
  test.skip(!expectedSha, "SBX_STAGING_EXPECT_SHA not set");
  await page.goto("/login");
  await expect(page.locator('meta[name="sbx-build-sha"]')).toHaveAttribute("content", expectedSha!);
  const health = await request.get("/api/healthz");
  expect((await health.json()).git_sha).toBe(expectedSha);
});

test("test user signs in and reads a persisted acceptance Session", async ({ page }) => {
  await page.goto("/");
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
