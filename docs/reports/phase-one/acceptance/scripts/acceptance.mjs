// Phase One acceptance: a brand-new user goes from signup to a real coding Session, a GitHub
// pull request and an SBX API key used against the API; a second user proves isolation.
// Real Chrome (throwaway profiles), real DeepSeek / Modal / GitHub. Secrets are read from
// local files, typed into masked fields and never written to any artifact.
import { launch, shot, signUp, logIn, verificationToken, BASE } from "./lib.mjs";
import fs from "node:fs"; import os from "node:os"; import path from "node:path";
const home = os.homedir();
const out = process.env.OUT ?? home + "/sbx-phase1/shots/acceptance";
const API = process.env.SBX_API_URL ?? "http://localhost:8800";
const REPO = "soren-labs/sbx-e2e-test";
const run = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14);
const HARNESS = process.env.HARNESS ?? "Claude Code";
const DEEPSEEK = fs.readFileSync(home + "/.config/sbx-dev/deepseek_api_key", "utf8").trim();
const GH = fs.readFileSync(home + "/.config/sbx-dev/e2e_github_token", "utf8").trim();
const toml = fs.readFileSync(home + "/.modal.toml", "utf8");
const MODAL = { id: /token_id\s*=\s*"([^"]+)"/.exec(toml)[1], secret: /token_secret\s*=\s*"([^"]+)"/.exec(toml)[1] };
const report = { run, repo: REPO, harness: HARNESS, steps: [] };
const step = (name, data = {}) => { report.steps.push({ name, ok: true, ...data }); console.log("STEP", name, JSON.stringify(data)); };
const exact = { exact: true };
const password = "acceptance password " + run.slice(-6);
const alice = { email: `alice-${run}@example.test`, profile: "acc-alice-" + run };
const bob = { email: `bob-${run}@example.test`, profile: "acc-bob-" + run };

async function register(page, user, prefix) {
  await signUp(page, user.email, password); await shot(page, out, prefix + "-register-submitted");
  await page.goto(BASE + "/login");
  await page.fill("#login-email", user.email); await page.fill("#login-password", password); await page.click('button[type="submit"]');
  await page.waitForTimeout(1500); await shot(page, out, prefix + "-login-before-verification");
  await page.goto(BASE + "/verify-email?token=" + verificationToken(user.email)); await page.waitForTimeout(1200); await shot(page, out, prefix + "-email-verified");
  await logIn(page, user.email, password); await page.waitForTimeout(1200);
}

// ---------------------------------------------------------------- Alice: setup (not recorded)
let a = await launch({ profile: alice.profile });
await register(a.page, alice, "a01"); step("alice_registered_verified_logged_in");
await shot(a.page, out, "a02-home-setup-checklist");
await a.page.goto(BASE + "/connections"); await a.page.waitForSelector('[data-testid="section-inference_api"]');
const inf = a.page.locator('[data-testid="section-inference_api"]');
await inf.getByLabel("API key", exact).fill(DEEPSEEK);
await shot(a.page, out, "a03-inference-form");
await inf.getByRole("button", { name: "Connect" }).click();
await a.page.waitForSelector('[data-testid="connection-inference_api"] [data-health="ready"]', { timeout: 90000 });
const modal = a.page.locator('[data-testid="section-modal"]');
await modal.getByLabel("Label", exact).fill("My Modal workspace");
await modal.getByLabel("Modal token ID", exact).fill(MODAL.id); await modal.getByLabel("Modal token secret", exact).fill(MODAL.secret);
await modal.getByRole("button", { name: "Connect" }).click();
const github = a.page.locator('[data-testid="section-github"]');
await github.getByLabel("Label", exact).fill("GitHub"); await github.getByLabel("GitHub token", exact).fill(GH);
await github.getByRole("button", { name: "Connect" }).click();
await a.page.waitForSelector('[data-testid="connection-modal"] [data-health="ready"]', { timeout: 120000 });
await a.page.waitForSelector('[data-testid="connection-github"] [data-health="ready"]', { timeout: 60000 });
await shot(a.page, out, "a04-connections-ready"); step("alice_bound_inference_modal_github");
await a.ctx.close();

// ---------------------------------------------------------------- Alice: Session, PR, API key (recorded)
a = await launch({ profile: alice.profile, video: path.join(out, "video") });
const page = a.page;
await page.addInitScript(() => { const s = document.createElement("style"); s.textContent = ".key-once{filter:blur(7px)!important}"; document.documentElement.appendChild(s); });
await page.goto(BASE + "/"); await page.waitForSelector('[data-testid="agent-model-chip"]');
await page.locator('[data-testid="agent-model-chip"]').click();
await page.locator(".model-popover").getByRole("button", { name: HARNESS, exact: true }).click();
await page.waitForTimeout(700); await shot(page, out, "a05-cli-and-model-picker", { full: false });
await page.keyboard.press("Escape");
await page.locator(".repo-chip").click();
await page.getByLabel("Select repository", exact).fill(REPO);
await shot(page, out, "a06-repository-picker", { full: false });
await page.keyboard.press("Escape");
const file = `docs/acceptance-${run}.md`;
await page.locator("#new-prompt").fill(`Add a new file ${file} with a short heading and one sentence saying this file was written by an SBX Session during Phase One acceptance run ${run}. Run \`git status --short\` to confirm, then summarise what you did.`);
await shot(page, out, "a07-composer-ready", { full: false });
await page.locator(".start-session-button").click();
await page.waitForURL(/\/sessions\/sess_/, { timeout: 30000 });
const sid = page.url().split("/sessions/")[1].split(/[/?]/)[0];
const api = (p, init) => page.evaluate(async ([p, init]) => { const r = await fetch(p, init); return { status: r.status, body: await r.json().catch(() => null) }; }, [p, init]);
const turnDone = async (n, timeout) => { const end = Date.now() + timeout; while (Date.now() < end) { const t = (await api(`/api/sessions/${sid}/turns`)).body.items[n]; if (t && ["succeeded", "failed", "cancelled", "interrupted"].includes(t.state)) return t.state; await page.waitForTimeout(1500); } return "timeout"; };
await page.waitForTimeout(5000); await shot(page, out, "a08-session-working", { full: false });
const t1 = await turnDone(0, 600000); await page.waitForTimeout(1500); await shot(page, out, "a09-session-turn1");
if (t1 !== "succeeded") throw new Error("turn 1 " + t1);
await page.locator("#followup").fill("In one sentence: which file did you add, and in which repository?");
await page.locator('form.followup button[type="submit"]').click();
const t2 = await turnDone(1, 300000); await page.waitForTimeout(1500); await shot(page, out, "a10-session-turn2");
step("alice_real_session", { session: sid, turns: [t1, t2], executor: "modal" });

// Changes -> ChangeSet -> Delivery (real pull request in the e2e repository)
await page.goto(`${BASE}/sessions/${sid}/changes`); await page.waitForTimeout(2500); await shot(page, out, "a11-changes-live");
await page.getByRole("button", { name: "Capture ChangeSet" }).click();
await page.waitForSelector('form[aria-label="Request Delivery"] button[type="submit"]:not([disabled])', { timeout: 120000 });
await page.getByRole("button", { name: "Show diff" }).click().catch(() => {});
await shot(page, out, "a12-changeset-ready");
await page.getByLabel("Pull request title").fill(`SBX Phase One acceptance ${run}`);
await page.getByRole("button", { name: "Request Delivery" }).click();
let pr = null;
for (let i = 0; i < 120 && !pr; i++) {
  await page.waitForTimeout(2000);
  const sets = (await api(`/api/sessions/${sid}/changesets`)).body.items;
  const ds = (await api(`/api/changesets/${sets[0].id}/deliveries`)).body.items;
  const d = ds[0]; if (d && ["failed", "cancelled"].includes(d.state)) throw new Error("delivery " + d.state + " " + JSON.stringify(d.error ?? d.reason));
  if (d && d.pull_request && d.pull_request.number) pr = d;
}
if (!pr) throw new Error("no pull request");
await page.waitForTimeout(2500); await shot(page, out, "a13-delivery-pull-request");
step("alice_delivery_pull_request", { number: pr.pull_request.number, url: pr.pull_request.url ?? pr.pull_request.html_url, branch: pr.target_ref, state: pr.state });
report.pr = { number: pr.pull_request.number, branch: pr.target_ref };

// API key in the UI, then the real API with that key
await page.goto(BASE + "/settings"); await page.waitForSelector("#key-name");
await page.fill("#key-name", "acceptance-" + run); await page.getByRole("button", { name: "Create key" }).click();
await page.waitForSelector(".key-once"); const key = (await page.locator(".key-once").innerText()).trim();
await shot(page, out, "a14-api-key-created-blurred");
const call = async (p, init = {}) => { const r = await fetch(API + p, { ...init, headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json", ...(init.headers ?? {}) } }); return { status: r.status, body: await r.json().catch(() => null) }; };
const me = await call("/api/me"); const ws = me.body.workspaces[0].id;
const sessions = await call(`/api/workspaces/${ws}/sessions`);
const conns = await call(`/api/workspaces/${ws}/connections`);
const send = await call(`/api/sessions/${sid}/messages`, { method: "POST", headers: { "Idempotency-Key": "acc-" + run }, body: JSON.stringify({ content: "Reply with exactly: API KEY TURN OK" }) });
let apiTurn = "timeout"; for (let i = 0; i < 150; i++) { const t = (await call(`/api/sessions/${sid}/turns`)).body.items[2]; if (t && ["succeeded", "failed", "cancelled", "interrupted"].includes(t.state)) { apiTurn = t.state; break; } await new Promise((r) => setTimeout(r, 2000)); }
const msgs = (await call(`/api/sessions/${sid}/messages`)).body.items;
const reply = JSON.stringify(msgs[msgs.length - 1].parts ?? []).includes("API KEY TURN OK");
const noKey = await fetch(API + "/api/me"); const badKey = await fetch(API + "/api/me", { headers: { Authorization: "Bearer sbx_key_invalid" } });
const leak = JSON.stringify(conns.body).includes(DEEPSEEK) || JSON.stringify(conns.body).includes(GH) || JSON.stringify(conns.body).includes(MODAL.secret);
step("alice_api_key_calls", { me: me.status, via: me.body.auth.via, sessions: sessions.status, session_count: sessions.body.items.length, connections: conns.status, connection_kinds: conns.body.items.map((c) => c.kind).sort(), secrets_in_response: leak, send_message: send.status, api_turn: apiTurn, reply_ok: reply, without_key: noKey.status, invalid_key: badKey.status });
await page.getByRole("button", { name: /dismiss|done|ok|hide/i }).first().click().catch(() => {});
await page.goto(`${BASE}/sessions/${sid}`); await page.waitForTimeout(2500); await shot(page, out, "a15-session-after-api-turn");
await page.setViewportSize({ width: 390, height: 844 });
for (const [p, n] of [["", "home"], ["connections", "connections"], [`sessions/${sid}`, "session"], ["settings", "settings"]]) { await page.goto(`${BASE}/${p}`); await page.waitForTimeout(1500); await shot(page, out, "a16-mobile-" + n); }
await page.locator(".mobile-toggle").click(); await page.waitForTimeout(500); await shot(page, out, "a16-mobile-navigation", { full: false });
const aliceConn = conns.body.items.map((c) => c.id);
await a.ctx.close();

// ---------------------------------------------------------------- Bob: no external credentials
const b = await launch({ profile: bob.profile });
await register(b.page, bob, "b01"); await shot(b.page, out, "b02-home-setup-checklist");
const bapi = (p, init) => b.page.evaluate(async ([p, init]) => { const r = await fetch(p, init); return { status: r.status, body: await r.json().catch(() => null) }; }, [p, init]);
await b.page.goto(`${BASE}/sessions/${sid}`); await b.page.waitForTimeout(2000); await shot(b.page, out, "b03-alice-session-not-found");
await b.page.goto(BASE + "/sessions"); await b.page.waitForTimeout(1500); await shot(b.page, out, "b04-sessions-empty");
await b.page.goto(BASE + "/connections"); await b.page.waitForTimeout(1500); await shot(b.page, out, "b05-connections-empty");
await b.page.goto(BASE + "/"); await b.page.waitForSelector("#new-prompt");
await b.page.locator("#new-prompt").fill("Say hello"); await b.page.locator(".start-session-button").click(); await b.page.waitForTimeout(2500);
await shot(b.page, out, "b06-session-refused-without-connections");
const bme = (await bapi("/api/me")).body; const csrf = (await b.ctx.cookies()).find((c) => c.name === "sbx_csrf")?.value ?? "";
const probes = {
  session: (await bapi(`/api/sessions/${sid}`)).status,
  events: (await bapi(`/api/sessions/${sid}/events`)).status,
  messages: (await bapi(`/api/sessions/${sid}/messages`)).status,
  files: (await bapi(`/api/sessions/${sid}/files?path=`)).status,
  changesets: (await bapi(`/api/sessions/${sid}/changesets`)).status,
  alice_workspace_sessions: (await bapi(`/api/workspaces/${ws}/sessions`)).status,
  alice_workspace_connections: (await bapi(`/api/workspaces/${ws}/connections`)).status,
  alice_models: (await bapi(`/api/models?workspace_id=${ws}`)).status,
  connection_reads: await Promise.all(aliceConn.map(async (id) => (await bapi(`/api/connections/${id}`)).status)),
  post_message: (await bapi(`/api/sessions/${sid}/messages`, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, "Idempotency-Key": "bob-" + run }, body: JSON.stringify({ content: "intrude" }) })).status,
  use_alice_connection: (await bapi(`/api/workspaces/${bme.workspaces[0].id}/sessions`, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, "Idempotency-Key": "bob2-" + run }, body: JSON.stringify({ harness: { provider_id: "opencode" }, executor: { backend: "local" }, connections: { inference: aliceConn[0] } }) })).status,
  delete_alice_connection: (await bapi(`/api/connections/${aliceConn[0]}`, { method: "DELETE", headers: { "X-CSRF-Token": csrf, "Idempotency-Key": "bob3-" + run } })).status,
  own_sessions: (await bapi(`/api/workspaces/${bme.workspaces[0].id}/sessions`)).body.items.length,
  own_connections: (await bapi(`/api/workspaces/${bme.workspaces[0].id}/connections`)).body.items.length,
  alice_key_sees_bob_workspace: (await call(`/api/workspaces/${bme.workspaces[0].id}/sessions`)).status,
};
step("bob_isolation", probes);
await b.ctx.close();

// ---------------------------------------------------------------- cleanup of this run's resources
const revoke = await call("/api/api-keys"); const mine = revoke.body.items.find((k) => k.name === "acceptance-" + run);
report.cleanup = {};
report.cleanup.release_executor = (await call(`/api/sessions/${sid}/executor/releases`, { method: "POST", headers: { "Idempotency-Key": "rel-" + run } })).status;
report.cleanup.revoke_api_key = mine ? (await call(`/api/api-keys/${mine.id}`, { method: "DELETE", headers: { "Idempotency-Key": "rev-" + run } })).status : "not found";
report.cleanup.key_after_revoke = (await call("/api/me")).status;
fs.writeFileSync(path.join(out, "acceptance-result.json"), JSON.stringify(report, null, 1));
console.log("DONE", JSON.stringify(report.cleanup));
