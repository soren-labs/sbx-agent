import { expect, test } from "@playwright/test";
import { connectInference, PASSWORD, signInViaApi, uniqueEmail, verificationPath } from "./support";

test("register, verify the email link and sign in", async ({ page }) => {
  const email = uniqueEmail("signup");

  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
  await page.getByRole("link", { name: "Create account" }).click();

  const register = page.getByRole("form", { name: "Create account" });
  await register.locator("#reg-email").fill(email);
  await register.locator("#reg-password").fill(PASSWORD);
  await register.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByRole("heading", { name: "Check your email" })).toBeVisible();

  await page.goto(await verificationPath(page.request, email));
  await expect(page.getByText("Email verified. You can sign in now.")).toBeVisible();
  await page.getByRole("link", { name: "Sign in" }).click();

  const login = page.getByRole("form", { name: "Sign in" });
  await login.locator("#login-email").fill(email);
  await login.locator("#login-password").fill(PASSWORD);
  await login.getByRole("button", { name: "Sign in" }).click();

  await expect(page.getByRole("form", { name: "New Session" })).toBeVisible();
  await expect(page).toHaveURL(/\/$/);
});

test("wrong password is rejected without signing in", async ({ page }) => {
  const user = await signInViaApi(page, "badpw");
  await page.context().clearCookies();

  await page.goto("/login");
  const login = page.getByRole("form", { name: "Sign in" });
  await login.locator("#login-email").fill(user.email);
  await login.locator("#login-password").fill("not the right password");
  await login.getByRole("button", { name: "Sign in" }).click();

  await expect(page.getByText("Sign-in failed")).toBeVisible();
  await expect(page).toHaveURL(/\/login$/);
});

test("a Session runs a Turn on the fake provider and survives a reload", async ({ page }) => {
  const user = await signInViaApi(page, "turn");
  await connectInference(page, user);

  await page.goto("/");
  const composer = page.getByRole("form", { name: "New Session" });
  await composer.getByRole("textbox", { name: "Task" }).fill("browser smoke [write:note.txt=hello]");
  await composer.getByRole("button", { name: "Start Session" }).click();

  await expect(page).toHaveURL(/\/sessions\/[^/]+/);
  // The fake OpenCode CLI answers "ACK: <prompt>".
  const reply = page.getByText("ACK: browser smoke");
  await expect(reply).toBeVisible({ timeout: 45_000 });

  await page.reload();
  await expect(reply).toBeVisible();

  await page.getByRole("link", { name: "Sessions", exact: true }).first().click();
  await expect(page.getByRole("link", { name: /browser smoke/ }).first()).toBeVisible();
});
