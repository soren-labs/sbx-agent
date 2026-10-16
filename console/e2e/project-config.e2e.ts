import { readFile } from "node:fs/promises";
import { expect, test, type Page } from "@playwright/test";
import { connectInference, signInViaApi, type SignedIn } from "./support";

// #229: complete ProjectVersion specs, check argv, declared services and delivery capability,
// against the real control plane with fake providers (no cloud, no real GitHub).
test.use({
  viewport: { width: 1440, height: 900 },
  // Evidence runs record the chain: SBX_E2E_VIDEO=1 npx playwright test e2e/project-config.e2e.ts
  video: process.env.SBX_E2E_VIDEO ? { mode: "on", size: { width: 1440, height: 900 } } : "off",
});

const shot = (page: Page, name: string) => page.screenshot({ path: test.info().outputPath(`${name}.png`) });

function headers(user: SignedIn) {
  return { "X-CSRF-Token": user.csrf, "Idempotency-Key": crypto.randomUUID() };
}

/** Every section populated, as an SDK or an older Console might have written it. */
const FULL_SPEC = {
  repository: { full_name: "acme/demo", base_ref: "develop" },
  environment: { env: { APP_MODE: "test", LOG_LEVEL: "debug" } },
  checks: [{ name: "unit", argv: ["python3", "-c", "print('ok')"], timeout_seconds: 120 }],
  services: [{ name: "web", argv: ["python3", "-m", "http.server", "18960"], port: 18960, health: { path: "/" }, restart: "never" }],
  defaults: {
    harness: { provider_id: "claude", model: "claude-sonnet", effort: "high" },
    executor: { backend: "local", resource_class: "large" },
    turn_defaults: { max_minutes: 30 },
  },
  ship_policy: {
    transport: "git_branch",
    base_branch: "release",
    draft: false,
    required_results: [
      { kind: "ReviewAssessment", count: 2, verdict: "approve" },
      { kind: "TestResult", count: 1 },
    ],
    merge_methods: ["merge"],
    require_base_unchanged: true,
  },
  x_team_notes: { owner: "platform" },
};

async function createProject(page: Page, user: SignedIn, slug: string, spec: unknown) {
  const res = await page.request.post(`/api/workspaces/${user.workspaceId}/projects`, {
    headers: headers(user),
    data: { slug, name: slug, spec },
  });
  expect(res.status()).toBe(201);
  return res.json();
}

async function startSession(page: Page, user: SignedIn, body: Record<string, unknown>, text: string): Promise<string> {
  const res = await page.request.post(`/api/workspaces/${user.workspaceId}/sessions`, {
    headers: headers(user),
    data: { ...body, message: { content: text } },
  });
  expect(res.ok()).toBeTruthy();
  const { session_id: id, turn_id: turn } = await res.json();
  await expect(async () => {
    const t = await (await page.request.get(`/api/turns/${turn}`)).json();
    expect(t.turn.state).toBe("succeeded");
  }).toPass({ timeout: 45_000 });
  return id;
}

test("publishing a version from the Console keeps the complete spec (AUD03, AUD06)", async ({ page }) => {
  const user = await signInViaApi(page, "proj229");
  const project = await createProject(page, user, "full-spec", FULL_SPEC);
  const v1 = project.current_version.spec;

  await page.goto("/projects");
  await page.getByRole("button", { name: "Project actions" }).click();
  await page.getByRole("menuitem", { name: "Publish new version" }).click();
  const form = page.getByRole("form", { name: "Publish new version" });
  await expect(form.getByText("Reasoning effort: high")).toBeVisible();
  await expect(form.getByText("Delivery: ready-for-review branch to release")).toBeVisible();
  await expect(form.getByText("Required before merge: 2× ReviewAssessment (approve), 1× TestResult")).toBeVisible();
  await expect(form.getByLabel("Agent CLI")).toHaveValue("claude");
  await expect(form.getByRole("button", { name: "Publish version" })).toBeDisabled();

  await form.getByLabel("Model").fill("claude-opus");
  const checks = form.getByLabel("Checks");
  await expect(checks).toHaveValue(`unit=python3 -c 'print('"'"'ok'"'"')'`);
  await checks.fill(`${await checks.inputValue()}\nsmoke=python3 -c "print(123)"`);
  const argv = form.getByRole("list", { name: "Arguments of smoke" });
  await expect(argv.getByRole("listitem")).toHaveText(["python3", "-c", "print(123)"]);
  const changes = form.getByRole("list", { name: "Changes in this version" });
  await expect(changes.getByRole("listitem")).toHaveCount(3);
  await changes.scrollIntoViewIfNeeded();
  await shot(page, "aud03-aud06-publish-dialog");
  await form.getByRole("button", { name: "Publish version" }).click();
  await expect(form).toBeHidden();
  await expect(page.getByText(/· v2 ·/)).toBeVisible();

  const versions = await (await page.request.get(`/api/projects/${project.id}/versions`)).json();
  const v2 = versions.items[0].spec;
  expect(versions.items[0].ordinal).toBe(2);
  // Only the two edited fields differ; Harness, effort, services, environment, policy and
  // unknown fields are exactly as before.
  expect(v2).toEqual({
    ...v1,
    checks: [...v1.checks, { name: "smoke", argv: ["python3", "-c", "print(123)"], timeout_seconds: 600 }],
    defaults: { ...v1.defaults, harness: { ...v1.defaults.harness, model: "claude-opus" } },
  });
});

test("a declared service starts, shows its logs and stops (AUD17)", async ({ page }) => {
  const user = await signInViaApi(page, "svc229");
  await connectInference(page, user);

  // Declared through the Console: Projects → New project → Services.
  await page.goto("/projects");
  await page.getByRole("button", { name: "New project" }).first().click();
  const form = page.getByRole("form", { name: "Create Project" });
  await form.getByLabel("Slug").fill("svc-demo");
  await form.getByLabel("Name", { exact: true }).fill("Service demo");
  await form.getByLabel("Agent CLI").selectOption("opencode");
  await form.getByLabel("Compute").selectOption("local");
  await form.getByRole("button", { name: "Add service" }).click();
  const row = form.getByRole("group", { name: "Service …" });
  await row.getByLabel("Name").fill("worker");
  await form
    .getByRole("group", { name: "Service worker" })
    .getByLabel("Command")
    .fill(`python3 -u -c "import time; print('svc-229 ready', flush=True); time.sleep(600)"`);
  await expect(form.getByRole("list", { name: "Arguments of worker" }).getByRole("listitem")).toHaveText([
    "python3",
    "-u",
    "-c",
    "import time; print('svc-229 ready', flush=True); time.sleep(600)",
  ]);
  await shot(page, "aud17-declare-service");
  await form.getByRole("button", { name: "Create Project" }).click();
  await expect(form).toBeHidden();
  const projects = await (await page.request.get(`/api/workspaces/${user.workspaceId}/projects`)).json();
  const project = projects.items.find((p: { slug: string }) => p.slug === "svc-demo");

  const sid = await startSession(page, user, { project_id: project.id }, "hello [write:a.txt=1]");
  await page.goto(`/sessions/${sid}/services`);
  await expect(page.getByText("Declared by Project Service demo, version 1.")).toBeVisible();
  const svc = page.getByRole("listitem", { name: "worker" });
  try {
    await svc.getByRole("button", { name: "Logs" }).click();
    await expect(svc.getByText("Not started on the current compute yet.")).toBeVisible();
    await svc.getByRole("button", { name: "Start" }).click();
    await expect(svc.getByText("ready", { exact: true })).toBeVisible({ timeout: 20_000 });
    await expect(svc.getByLabel("Logs")).toContainText("svc-229 ready", { timeout: 20_000 });
    await expect(svc.getByRole("button", { name: "Start" })).toBeDisabled();
    await shot(page, "aud17-service-running-logs");
  } finally {
    await svc.getByRole("button", { name: "Stop" }).click();
  }
  await expect(svc.getByText("stopped", { exact: true })).toBeVisible({ timeout: 20_000 });
  await expect(svc.getByText("desired: stopped")).toBeVisible();
  await shot(page, "aud17-service-stopped");

  // A projectless Session says where services come from instead of promising discovery.
  const bare = await startSession(page, user, { harness: { provider_id: "opencode" }, executor: { backend: "local" } }, "hi");
  await page.goto(`/sessions/${bare}/services`);
  await expect(page.getByText("This Session has no Project, so it has no declared services.")).toBeVisible();
  await expect(page.getByText("Processes the agent or a terminal starts on their own are not listed.", { exact: false })).toBeVisible();
  await shot(page, "aud17-projectless-empty");
});

test("a ChangeSet without a repository offers the patch instead of a PR form (AUD15)", async ({ page }) => {
  const user = await signInViaApi(page, "dlv229");
  await connectInference(page, user);
  const sid = await startSession(
    page,
    user,
    { harness: { provider_id: "opencode" }, executor: { backend: "local" } },
    "notes [write:notes.txt=hello from 229]",
  );
  const captured = await page.request.post(`/api/sessions/${sid}/changesets`, { headers: headers(user), data: { origin: "explicit" } });
  expect(captured.status()).toBe(202);
  const csId = (await captured.json()).changeset.id;
  await expect(async () => {
    expect((await (await page.request.get(`/api/changesets/${csId}`)).json()).state).toBe("ready");
  }).toPass({ timeout: 30_000 });

  await page.goto(`/sessions/${sid}/changes`);
  await expect(page.getByText("This Session has no repository")).toBeVisible();
  await expect(page.getByLabel("Pull request title")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Request delivery" })).toHaveCount(0);
  await shot(page, "aud15-projectless-delivery");
  const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("link", { name: "Download patch" }).click()]);
  expect(download.suggestedFilename()).toBe(`${csId}.patch`);
  const patch = await readFile((await download.path())!, "utf8");
  expect(patch).toContain("notes.txt");
  expect(patch).toContain("+hello from 229");
  // No Delivery was created and nothing tried to reach GitHub.
  expect((await (await page.request.get(`/api/changesets/${csId}/deliveries`)).json()).items).toEqual([]);
});
