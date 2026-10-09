// Shared Playwright helpers: real Google Chrome, dedicated throwaway test profile.
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";

export const BASE = process.env.SBX_CONSOLE_URL ?? "http://localhost:5174";
export const MAIL = path.join(os.homedir(), "sbx-phase1/stage/data/mail");

export async function launch({ profile = "default", viewport = { width: 1440, height: 900 }, video = null, mobile = false, colorScheme = "dark" } = {}) {
  const dir = path.join(os.homedir(), "sbx-phase1/chrome-profiles", profile);
  fs.mkdirSync(dir, { recursive: true });
  const ctx = await chromium.launchPersistentContext(dir, {
    channel: "chrome",
    headless: true,
    viewport,
    colorScheme,
    isMobile: mobile,
    hasTouch: mobile,
    deviceScaleFactor: mobile ? 2 : 1,
    ...(video ? { recordVideo: { dir: video, size: viewport } } : {}),
  });
  const page = ctx.pages()[0] ?? (await ctx.newPage());
  const errors = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text().slice(0, 300)); });
  page.on("pageerror", (e) => errors.push("pageerror: " + String(e).slice(0, 300)));
  return { ctx, page, errors };
}

export function verificationToken(email) {
  const files = fs.readdirSync(MAIL).sort().reverse();
  for (const f of files) {
    const data = JSON.parse(fs.readFileSync(path.join(MAIL, f), "utf8"));
    if (data.to === email && data.body.includes("token=")) return data.body.split("token=")[1].trim();
  }
  throw new Error("no verification mail for " + email);
}

export async function shot(page, dir, name, opts = {}) {
  fs.mkdirSync(dir, { recursive: true });
  await page.waitForTimeout(350);
  // The workspace scrolls inside <main>; grow the viewport to its content for a full capture.
  const size = page.viewportSize();
  const tall = opts.full === false ? null : await page.evaluate(() => {
    const main = document.querySelector("#workspace-main");
    return main && main.scrollHeight > main.clientHeight ? main.scrollHeight - main.clientHeight : 0;
  });
  if (tall) { await page.setViewportSize({ width: size.width, height: Math.min(size.height + tall + 8, 6000) }); await page.waitForTimeout(250); }
  await page.screenshot({ path: path.join(dir, name + ".png"), fullPage: true });
  if (tall) await page.setViewportSize(size);
  console.log("shot", name);
}

export async function signUp(page, email, password) {
  await page.goto(BASE + "/register");
  await page.fill("#reg-email", email);
  await page.fill("#reg-password", password);
  await page.click('button[type="submit"]');
  await page.waitForTimeout(1200);
}

export async function logIn(page, email, password) {
  await page.goto(BASE + "/login");
  await page.fill("#login-email", email);
  await page.fill("#login-password", password);
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.startsWith("/login"), { timeout: 15000 });
}
