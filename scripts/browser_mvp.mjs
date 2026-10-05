// Optional real-browser proof. Secrets arrive on protected stdin, never argv/storage/trace.
import { pathToFileURL } from "node:url";
let input = "";
for await (const chunk of process.stdin) input += chunk;
const settings = JSON.parse(input);
let browser,
  stage = "launch";
try {
  const { chromium } = await import(pathToFileURL(process.argv[2]).href);
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
  });
  const page = await context.newPage();
  stage = "login";
  await page.goto(settings.url);
  const login = page
    .getByRole("button", { name: "Sign in", exact: true })
    .locator("..");
  await login.locator("input[name=email]").fill(settings.email);
  await login.locator("input[name=password]").fill(settings.password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.getByRole("button", { name: "Sign out", exact: true }).waitFor();
  stage = "composer";
  await page
    .locator('select[name=model] option[value="' + settings.model + '"]')
    .waitFor({ state: "attached" });
  await page.locator("select[name=modal]").selectOption(settings.modal);
  await page.locator("select[name=github]").selectOption(settings.github);
  const disabled = await page
    .getByRole("button", { name: "Create Session and queue request" })
    .isDisabled();
  if (disabled) throw Error("composer unavailable");
  stage = "secret-scan";
  const content = await page.evaluate(() =>
    JSON.stringify({
      html: document.documentElement.outerHTML,
      values: Array.from(document.querySelectorAll("input")).map(
        (i) => i.value,
      ),
      local: { ...localStorage },
      session: { ...sessionStorage },
    }),
  );
  if (settings.secrets.some((secret) => content.includes(secret)))
    throw Error("secret exposed");
  await page.screenshot({ path: process.argv[3], fullPage: true });
  stage = "responsive";
  await page.setViewportSize({ width: 390, height: 844 });
  if (
    await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth + 2,
    )
  )
    throw Error("overflow");
  stage = "logout";
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await page.getByRole("button", { name: "Sign in", exact: true }).waitFor();
  console.log(
    JSON.stringify({
      passed: true,
      login: true,
      composer: true,
      secrets_absent: true,
      responsive: true,
      logout: true,
    }),
  );
} catch {
  // Playwright exception text can include fill values. Never print it or tracing data.
  console.log(JSON.stringify({ passed: false, stage }));
  process.exitCode = 1;
} finally {
  await browser?.close();
}
