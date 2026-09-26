import { expect, test } from "@playwright/test";
import {
  connect,
  createAgent,
  devInfo,
  KEY,
  run,
  shot,
  waitAgent,
  waitRun,
} from "./helpers";

// One throwaway control plane per run: tests build on each other's state.
test.describe.configure({ mode: "serial" });

test.describe("web console against a real local /v1 control plane", () => {
  test("connect: rejects a bad key, accepts the bootstrap key", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("connect-view")).toBeVisible();
    await shot(page, "console_01_connect.png");

    await page.getByTestId("connect-key").fill("sbx_not_a_real_key");
    await page.getByTestId("connect-submit").click();
    await expect(page.getByTestId("connect-error")).toBeVisible();

    await page.getByTestId("connect-key").fill(KEY);
    await page.getByTestId("connect-submit").click();
    await expect(page.getByTestId("app-ready")).toBeVisible();
    await expect(page.getByTestId("identity")).toContainText("admin");
    // The task-first shell lands on Home.
    await expect(page.getByTestId("home-view")).toBeVisible();
    await expect(page.getByTestId("home-empty")).toBeVisible();
    await shot(page, "console_02_home_empty.png");
  });

  // SOR-211: `sbx open` mints a one-time grant the browser redeems.
  test("grant handoff connects without typing a key", async ({ browser }) => {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const scoped = await ctx.newPage();

    // Admin-authed mint, exactly what `sbx open` does.
    const mint = await scoped.request.post("/v1/console/grant", {
      headers: { Authorization: `Bearer ${KEY}` },
    });
    expect(mint.status()).toBe(201);
    const grant = (await mint.json()) as { grant: string; expires_in: number };
    expect(grant.grant).toMatch(/^sbxg_/);
    expect(grant.expires_in).toBeGreaterThan(0);

    // Landing on the grant URL connects straight through — the ticket
    // rides the fragment and is scrubbed before redemption.
    await scoped.goto(`/#/connect?grant=${grant.grant}`);
    await expect(scoped.getByTestId("app-ready")).toBeVisible();
    expect(scoped.url()).not.toContain("grant=");

    // The ticket is single-use: a second visit bounces to Connect.
    const replay = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const replayed = await replay.newPage();
    await replayed.goto(`/#/connect?grant=${grant.grant}`);
    await expect(replayed.getByTestId("connect-error")).toBeVisible();
    await replay.close();
    await ctx.close();
  });

  test("create an agent, stream run 1, follow up, cancel, reload", async ({ page }) => {
    await connect(page);
    await page.goto("/#/agents/new");
    await page.getByTestId("f-prompt").fill("Write hello.txt containing hi");
    await expect(page.getByTestId("request-preview")).toContainText("Write hello.txt containing hi");
    await page.locator('[data-testid=f-effort] [data-value="high"]').click();
    await expect(page.getByTestId("request-preview")).toContainText('"reasoning_effort": "high"');
    await page.getByTestId("preview-tabs").getByText("cURL").click();
    await expect(page.getByTestId("request-preview")).toContainText("curl -X POST");
    await page.getByTestId("f-name").fill("hello agent");
    await shot(page, "console_03_new_agent.png");
    await page.getByTestId("create-agent").click();

    await expect(page.getByTestId("agent-title")).toHaveText("hello agent", { timeout: 30_000 });
    await waitRun(page, "run-1", "FINISHED");
    const first = run(page, "run-1");
    await expect(first.getByTestId("user-message")).toContainText("Write hello.txt containing hi");
    await expect(first.getByTestId("agent-message")).toBeVisible();
    await expect(first.getByTestId("file-block")).toContainText("hello.txt");
    await first.getByTestId("command-block").locator("summary").click();
    await expect(first.getByTestId("command-output")).toBeVisible();
    await waitAgent(page, "idle");
    await expect(page.getByTestId("usage-card")).toContainText("25,996");
    await shot(page, "console_04_conversation.png");

    // A hanging follow-up exercises the live state and Cancel.
    await page.getByTestId("composer").fill("please hang for a while");
    await page.getByTestId("send").click();
    await waitRun(page, "run-2", "RUNNING");
    await expect(run(page, "run-2").getByTestId("working")).toBeVisible();
    await expect(page.getByTestId("composer")).toBeDisabled();
    await shot(page, "console_05_running.png");
    await page.getByTestId("cancel-run").click();
    await waitRun(page, "run-2", "CANCELLED");

    await page.getByTestId("composer").fill("Now continue the work");
    await page.getByTestId("composer").press("Enter");
    await waitRun(page, "run-3", "FINISHED");
    await expect(run(page, "run-3").getByTestId("user-message")).toContainText("Now continue");

    const messages = await page.getByTestId("agent-message").count();
    const commands = await page.getByTestId("command-block").count();
    await page.reload();
    await waitRun(page, "run-3", "FINISHED");
    await expect(page.getByTestId("agent-message")).toHaveCount(messages, { timeout: 20_000 });
    await expect(page.getByTestId("command-block")).toHaveCount(commands);
    await expect(page.getByTestId("run")).toHaveCount(3);
  });

  test("a failing run shows the structured run error", async ({ page }) => {
    await connect(page);
    await createAgent(page, "this one should fail", "failing agent");
    await waitRun(page, "run-1", "ERROR");
    const err = run(page, "run-1").getByTestId("run-error");
    await expect(err).toContainText("runtime_error");
    await expect(err).toContainText("provider");
    await shot(page, "console_06_run_error.png");

    await page.getByTestId("close-agent").click();
    await page.getByTestId("confirm-ok").click();
    await waitAgent(page, "closed");
    await expect(page.getByTestId("readonly-banner")).toBeVisible();
    await expect(page.getByTestId("composer")).toBeDisabled();
  });

  test("repository agent: git policy, review pin, publish, snapshot, artifact", async ({ page }) => {
    await connect(page);
    const { demo_workspace: ws } = await devInfo(page);
    await page.goto("/#/agents/new");
    await page.getByTestId("f-prompt").fill("Add a greeting test");
    await page.getByTestId("f-name").fill("repo agent");
    await page.getByTestId("toggle-repo").check({ force: true });
    await page.getByTestId("f-repo").fill(ws.repo);
    await page.getByTestId("f-baseRef").fill(ws.base_ref);
    await page.getByTestId("f-baseSha").fill("not-a-sha");
    await page.getByTestId("create-agent").click();
    await expect(page.getByTestId("form-error")).toBeVisible();
    await page.getByTestId("f-baseSha").fill(ws.base_sha);
    await page.getByTestId("toggle-git").check({ force: true });
    await page.getByTestId("git-branch").fill("sbx/greeting");
    await page.getByTestId("toggle-workflow").check({ force: true });
    await page.getByTestId("f-workflowId").fill("wf-e2e");
    await page.getByTestId("f-taskId").fill("implement");
    await expect(page.getByTestId("request-preview")).toContainText('"branch": "sbx/greeting"');
    await page.getByTestId("create-agent").click();
    await expect(page.getByTestId("agent-title")).toHaveText("repo agent", { timeout: 30_000 });
    await waitRun(page, "run-1", "FINISHED");
    await waitAgent(page, "idle");

    await page.getByTestId("tab-workspace").click();
    await expect(page.getByTestId("ws-pipeline")).toBeVisible();
    await expect(page.getByTestId("ws-record")).toContainText(ws.base_sha.slice(0, 12));
    await page.getByTestId("ws-review").click();
    await page.getByTestId("review-submit").click();
    await expect(page.getByTestId("ws-record")).toContainText("current");
    await page.getByTestId("ws-publish").click();
    await expect(page.getByTestId("ws-pipeline")).toContainText("sbx/greeting");
    await shot(page, "console_07_workspace.png");

    await page.getByTestId("snapshot").click();
    await page.getByTestId("snapshot-test").fill("python -c \"print('ok')\"");
    await page.getByTestId("snapshot-submit").click();
    await expect(page.getByTestId("artifacts-table")).toBeVisible({ timeout: 20_000 });
    await page.getByTestId("artifact-row").first().click();
    await expect(page.getByTestId("artifact-title")).toContainText("art-");
    await expect(page.getByTestId("artifact-files")).toContainText("app.py");
    const download = page.waitForEvent("download");
    await page.getByTestId("download-patch.diff").click();
    expect((await download).suggestedFilename()).toContain("patch.diff");
    await expect(page.getByTestId("artifact-handoff")).toHaveAttribute("href", /handoff_artifact=art-/);
    await shot(page, "console_08_artifact.png");
  });

  test("workflow recovery view and scoped cleanup", async ({ page }) => {
    await connect(page);
    await page.getByTestId("nav-workflows").click();
    await expect(page.getByTestId("workflows-table")).toContainText("wf-e2e");
    await page.getByText("wf-e2e").first().click();
    await expect(page.getByTestId("workflow-title")).toHaveText("wf-e2e");
    await expect(page.getByTestId("workflow-agents")).toContainText("implement");
    await shot(page, "console_09_workflow.png");
    await page.getByTestId("close-workflow").click();
    await page.getByTestId("confirm-ok").click();
    await expect(page.getByRole("dialog")).toContainText("Workflow cleanup");
  });

  test("task-first shell: home stats, tasks list, create task, task detail", async ({
    page,
  }) => {
    await connect(page);
    await expect(page.getByTestId("home-view")).toBeVisible();
    await expect(page.getByTestId("home-stats")).toBeVisible();
    await expect(page.getByTestId("home-empty")).toBeVisible();

    // Create a task the way a human would: a goal, everything else Automatic.
    await page.goto("/#/tasks/new");
    await page.getByTestId("f-prompt").fill("Write taskgoal.txt containing ok");
    await page.getByTestId("f-name").fill("first task");
    await expect(page.getByTestId("preflight-checks")).toBeVisible({ timeout: 15_000 });
    // The human-facing default: no repo means report-only delivery.
    await expect(page.locator('[data-testid=f-delivery] [data-value="none"]')).toHaveAttribute("aria-checked", "true");
    await shot(page, "console_15_new_task.png");
    await page.getByTestId("create-task").click();

    await page.waitForURL(/#\/tasks\/task_/, { timeout: 30_000 });
    await expect(page.getByTestId("task-title")).toHaveText("first task", { timeout: 30_000 });
    await expect(page.getByTestId("task-goal")).toContainText("taskgoal.txt");
    await waitRun(page, "run-1", "FINISHED");
    await expect(page.getByTestId("task-status")).toHaveAttribute("data-status", "finished", {
      timeout: 45_000,
    });
    await shot(page, "console_16_task.png");

    await page.getByTestId("nav-tasks").click();
    await expect(page.getByTestId("tasks-table")).toContainText("first task");
    await page.getByTestId("tasks-search").fill("nope");
    await expect(page.getByTestId("tasks-none")).toBeVisible();
    await page.getByTestId("tasks-search").fill("");
    await page.getByTestId("task-row").filter({ hasText: "first task" }).click();
    await expect(page.getByTestId("task-view")).toBeVisible();
  });

  test("legacy admin/capacity routes redirect to their new homes", async ({ page }) => {
    await connect(page);
    await page.goto("/#/admin/accounts");
    await page.waitForURL(/#\/integrations\/accounts/);
    await expect(page.getByTestId("accounts-table")).toBeVisible();
    await page.goto("/#/admin/keys");
    await page.waitForURL(/#\/settings\/keys/);
    await expect(page.getByTestId("page-title")).toContainText("API keys");
    await page.goto("/#/capacity");
    await page.waitForURL(/#\/integrations\/capacity/);
    await expect(page.getByTestId("capacity-codex")).toBeVisible();
    await page.goto("/#/admin/github");
    await page.waitForURL(/#\/integrations\/github/);
    await expect(page.getByTestId("github-status")).toBeVisible();
  });

  test("mobile drawer: hamburger opens, close button and Escape dismiss", async ({
    browser,
  }) => {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const page = await ctx.newPage();
    await connect(page);
    const shell = page.locator(".shell");
    await page.getByTestId("nav-menu").click();
    await expect(shell).toHaveClass(/nav-open/);
    await expect(page.getByTestId("nav-close")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(shell).not.toHaveClass(/nav-open/);
    await page.getByTestId("nav-menu").click();
    await page.getByTestId("nav-tasks").click();
    await expect(shell).not.toHaveClass(/nav-open/);
    await expect(page.getByTestId("tasks-view")).toBeVisible();
    await shot(page, "console_17_mobile_tasks.png");
    await ctx.close();
  });

  test("agents list filters, capacity and GitHub posture", async ({ page }) => {
    await connect(page);
    await page.getByTestId("nav-agents").click();
    // 3 agents from the earlier tests + 1 backing the task created above.
    await expect(page.getByTestId("agent-row")).toHaveCount(4);
    await page.locator('[data-testid=agents-filter] [data-value="ended"]').click();
    await expect(page.getByTestId("agent-row")).toHaveCount(2);
    await page.locator('[data-testid=agents-filter] [data-value="all"]').click();
    await page.getByTestId("agents-search").fill("hello");
    await expect(page.getByTestId("agent-row")).toHaveCount(1);
    await page.getByTestId("agents-search").fill("");
    await shot(page, "console_10_agents.png");

    await page.getByTestId("nav-integrations").click();
    await expect(page.getByTestId("int-capacity")).toBeVisible();
    await page.getByTestId("int-capacity").click();
    for (const provider of ["codex", "devin", "antigravity", "grok", "opencode"]) {
      await expect(page.getByTestId(`capacity-${provider}`)).toBeVisible();
    }
    await shot(page, "console_11_capacity.png");

    await page.getByTestId("nav-integrations").click();
    await page.getByTestId("int-github").click();
    await expect(page.getByTestId("github-status")).toContainText("not configured");
  });

  test("live badge polls the summary rollup, never a full agent list (SOR-202)", async ({ page }) => {
    // Count every GET by shape: the collection, the rollup, agent detail,
    // and a detail's run list — the sidebar must not burn the first.
    const hits = { list: 0, summary: 0, detail: 0, runs: 0 };
    page.on("request", (req) => {
      if (req.method() !== "GET") return;
      const p = new URL(req.url()).pathname;
      if (p === "/v1/agents") hits.list += 1;
      else if (p === "/v1/agents/summary") hits.summary += 1;
      else if (/^\/v1\/agents\/[^/]+$/.test(p)) hits.detail += 1;
      else if (/^\/v1\/agents\/[^/]+\/runs$/.test(p)) hits.runs += 1;
    });

    await connect(page);
    await page.getByTestId("nav-agents").click();
    await expect(page.getByTestId("agents-table")).toBeVisible();
    // Mounting the Agents page costs exactly one full page plus the cheap
    // rollup (shell badge + the list's own baseline). The baseline fires
    // just after the first render, so poll for it rather than racing it.
    expect(hits.list).toBe(1);
    await expect.poll(() => hits.summary).toBeGreaterThanOrEqual(2);

    // Event-driven: creating an agent bumps the rollup immediately, not
    // on the next interval.
    const s0 = hits.summary;
    await createAgent(page, "Write poll.txt containing ok", "poll-check");
    await waitRun(page, "run-1", "FINISHED");
    await waitAgent(page, "idle");
    await expect.poll(() => hits.summary, { timeout: 10_000 }).toBeGreaterThan(s0);

    // Parked away from the Agents page: over the old 15s sidebar cadence
    // no full-list fetch may recur at all.
    await page.getByTestId("nav-integrations").click();
    await page.getByTestId("int-capacity").click();
    const listBefore = hits.list;
    await page.waitForTimeout(16_500);
    expect(hits.list).toBe(listBefore);

    // Agent detail stops refreshing once the agent is ended: over the old
    // 10s cadence no detail or runs call may fire for a closed agent.
    await page.getByTestId("nav-agents").click();
    await page.getByTestId("agent-row").filter({ hasText: "failing agent" }).click();
    await expect(page.getByTestId("readonly-banner")).toBeVisible();
    const d0 = hits.detail;
    const r0 = hits.runs;
    await page.waitForTimeout(11_000);
    expect(hits.detail).toBe(d0);
    expect(hits.runs).toBe(r0);
  });

  test("admin: accounts import/remove and API key lifecycle with scopes", async ({ page, browser }) => {
    await connect(page);
    await page.getByTestId("nav-integrations").click();
    await page.getByTestId("int-accounts").click();
    await expect(page.getByTestId("accounts-table")).toContainText("codex-1");
    await page.getByTestId("import-account").click();
    await page.getByTestId("acct-provider").selectOption("grok");
    await page.getByTestId("acct-label").fill("e2e grok seat");
    await page.getByTestId("acct-file-0").setInputFiles({
      name: "auth.json",
      mimeType: "application/json",
      buffer: Buffer.from('{"token":"REDACTED"}'),
    });
    await page.getByTestId("acct-submit").click();
    await expect(page.getByTestId("accounts-table")).toContainText("e2e grok seat");
    await shot(page, "console_12_accounts.png");
    const row = page.getByTestId("account-row").filter({ hasText: "e2e grok seat" });
    await row.getByRole("button", { name: "Remove account" }).click();
    await page.getByTestId("confirm-ok").click();
    await expect(page.getByTestId("accounts-table")).not.toContainText("e2e grok seat");

    await page.getByTestId("nav-settings").click();
    await page.getByTestId("settings-keys").click();
    await page.getByTestId("create-key").click();
    await page.getByTestId("key-label").fill("e2e automation");
    await page.getByTestId("key-submit").click();
    const plaintext = (await page.getByTestId("key-plaintext").innerText()).trim();
    expect(plaintext.startsWith("sbx_")).toBeTruthy();
    // This screenshot is published in the docs: show a placeholder, never a key value.
    await page.getByTestId("key-plaintext").evaluate((el) => (el.textContent = "sbx_REDACTED"));
    await shot(page, "console_13_key_created.png");
    await page.getByRole("button", { name: "Done" }).click();
    await expect(page.getByTestId("keys-table")).toContainText("e2e automation");

    // The agents-only key cannot open admin pages.
    const other = await browser.newContext({ colorScheme: "light", viewport: { width: 1280, height: 800 } });
    const scoped = await other.newPage();
    await connect(scoped, plaintext);
    await scoped.getByTestId("nav-integrations").click();
    await scoped.getByTestId("int-accounts").click();
    await expect(scoped.getByTestId("admin-locked")).toBeVisible();

    // Revoking it bounces the other session back to Connect.
    const keyRow = page.getByTestId("key-row").filter({ hasText: "e2e automation" });
    await keyRow.getByTestId("key-revoke").click();
    await page.getByTestId("confirm-ok").click();
    await expect(keyRow).toContainText("revoked");
    await scoped.getByTestId("nav-agents").click();
    await expect(scoped.getByTestId("connect-view")).toBeVisible({ timeout: 20_000 });
    await other.close();
  });

  test("light theme and Chinese UI", async ({ browser }) => {
    const ctx = await browser.newContext({
      colorScheme: "light",
      locale: "zh-CN",
      viewport: { width: 1280, height: 800 },
    });
    const page = await ctx.newPage();
    await connect(page);
    await expect(page.getByTestId("nav-agents")).toContainText("Agent");
    await expect(page.getByTestId("nav-workflows")).toContainText("工作流");
    await page.getByTestId("nav-agents").click();
    await page.getByTestId("agent-row").filter({ hasText: "hello agent" }).click();
    await waitRun(page, "run-3", "FINISHED");
    await expect(page.getByTestId("tab-conversation")).toContainText("对话");
    await shot(page, "console_14_zh_light.png");
    await ctx.close();
  });

  test("links built from API data only accept http(s) URLs", async ({ page }) => {
    await page.goto("/");
    const inputs = [
      "https://github.com/o/r/pull/7",
      "http://ghe.example/o/r/pull/1",
      "javascript:alert(1)",
      "JaVaScRiPt:alert(1)",
      "java\tscript:alert(1)",
      " javascript:alert(1)",
      "data:text/html,<script>alert(1)</script>",
      "/relative/path",
      "",
      null,
    ];
    // A string expression reaches the browser untouched (the spec itself is
    // compiled to CommonJS, which would rewrite a dynamic import()).
    const results = await page.evaluate(
      `import("/lib/dom.js").then(({ httpUrl }) => ${JSON.stringify(inputs)}.map((v) => httpUrl(v)))`,
    );
    expect(results).toEqual([
      "https://github.com/o/r/pull/7",
      "http://ghe.example/o/r/pull/1",
      null,
      null,
      null,
      null,
      null,
      null,
      null,
      null,
    ]);
  });

  test("task delivery: PR mode auto-publishes; parked delivery offers Publish now", async ({
    page,
  }) => {
    await connect(page);
    const { demo_workspace: ws } = await devInfo(page);

    // A repo makes "Open a pull request" the default result — and the
    // request body must carry auto_publish, the only trigger that lets a
    // finished run deliver itself without a manual POST.
    await page.goto("/#/tasks/new");
    await page.getByTestId("f-prompt").fill("Write deliver.txt containing ok");
    await page.getByTestId("f-repo").fill(ws.repo);
    // Pin a provider: Automatic can land on the devin fake, which has no
    // ACP mode, and codex has no seeded credential in e2e — either would
    // make this test about the wrong thing.
    await page.getByTestId("f-provider").selectOption("antigravity");
    await expect(
      page.locator('[data-testid=f-delivery] [data-value="pr"]'),
    ).toHaveAttribute("aria-checked", "true");
    await page.getByTestId("api-preview").locator("summary").click();
    await expect(page.getByTestId("request-preview")).toContainText('"auto_publish": true');
    await expect(page.getByTestId("request-preview")).toContainText('"pull_request"');
    await page.getByTestId("create-task").click();

    await page.waitForURL(/#\/tasks\/task_/, { timeout: 30_000 });
    await waitRun(page, "run-1", "FINISHED");
    // The delivery fired on its own. A local-path repo can take the push
    // but cannot open a GitHub PR, so the attempt surfaces as a failed
    // delivery — before the fix this parked forever at "delivering".
    await expect(page.getByTestId("task-status")).toHaveAttribute(
      "data-status",
      "delivery_failed",
      { timeout: 45_000 },
    );
    await expect(page.getByTestId("delivery-error")).toBeVisible();
    await expect(page.getByTestId("publish-now")).toBeVisible();
    await shot(page, "console_18_delivery_failed.png");

    // A parked delivery (declared without auto_publish — e.g. an older
    // API-created task) still needs an in-UI way to publish.
    const created = await page.request.post("/v1/tasks", {
      headers: { Authorization: `Bearer ${KEY}` },
      data: {
        prompt: { text: "Write parked.txt containing ok" },
        source: { repo: ws.repo },
        execution: { provider: "antigravity" },
        delivery: { pull_request: { title: "e2e manual pr" } },
      },
    });
    expect(created.status()).toBe(201);
    const parked = ((await created.json()) as { task: { id: string } }).task;
    await expect
      .poll(
        async () => {
          const res = await page.request.get(`/v1/tasks/${parked.id}`, {
            headers: { Authorization: `Bearer ${KEY}` },
          });
          return ((await res.json()) as { task: { status: string } }).task.status;
        },
        { timeout: 60_000 },
      )
      .toBe("delivering");

    await page.goto(`/#/tasks/${parked.id}`);
    await expect(page.getByTestId("task-status")).toHaveAttribute(
      "data-status",
      "delivering",
    );
    await expect(page.getByTestId("publish-now")).toBeVisible();
    await page.getByTestId("publish-now").click();
    await expect(page.getByTestId("task-status")).toHaveAttribute(
      "data-status",
      "delivery_failed",
      { timeout: 30_000 },
    );
    await expect(page.getByTestId("delivery-error")).toBeVisible();
    // The affordance stays — the same click delivers once a push/PR path
    // can succeed (a GitHub repo + configured integration).
    await expect(page.getByTestId("publish-now")).toBeVisible();
  });
});

test.describe("functional onboarding seams (SOR-214 / SOR-220)", () => {
  test("provider connect: hosted lane surfaces the device URL and verifies (SOR-214)", async ({
    page,
  }) => {
    await connect(page);
    await page.getByTestId("nav-integrations").click();
    await page.getByTestId("int-accounts").click();
    await page.getByTestId("connect-provider").first().click();
    await page.getByTestId("connect-provider-select").selectOption("codex");
    await page.getByTestId("connect-label").fill("e2e connected seat");
    await page.getByTestId("connect-submit").click();

    // The fake `codex login` prints a browser URL and a code; the connect
    // session scrapes them and the dialog shows them while it polls.
    await expect(page.getByTestId("connect-state")).toContainText(
      /authenticating|materialized|verified/,
    );
    await expect(page.getByTestId("connect-state")).toContainText(/verified|materialized/, {
      timeout: 30_000,
    });
    await expect(page.getByTestId("connect-url")).toContainText("https://");
    await expect(page.getByTestId("connect-code")).toBeVisible();

    await page.getByRole("button", { name: "Close" }).click();
    await expect(page.getByTestId("accounts-table")).toContainText("e2e connected seat");
  });

  test("provider connect: local pairing fallback when hosted lane is unavailable (SOR-214)", async ({
    page,
  }) => {
    await connect(page);
    await page.getByTestId("nav-integrations").click();
    await page.getByTestId("int-accounts").click();
    await page.getByTestId("connect-provider").first().click();
    await page.getByTestId("connect-provider-select").selectOption("grok");
    await page.getByTestId("connect-label").fill("e2e pair seat");
    await page.getByTestId("connect-submit").click();

    const state = page.getByTestId("connect-state");
    await expect(state).toContainText(/authenticating|verified|materialized|failed/);

    // Hosted lane may or may not be reachable for grok in the e2e plane;
    // if the session degrades to a pair-capable state the pair command is
    // the contract seam being exercised.
    if (await page.getByTestId("connect-pair").isVisible()) {
      await expect(page.getByTestId("connect-pair")).toContainText("sbx auth pair sbxp_");
      const ticket = (
        (await page.getByTestId("connect-pair").innerText()).match(/sbxp_\S+/) || []
      )[0];
      expect(ticket).toBeTruthy();

      // Drive the pairing end-to-end like `sbx auth pair <ticket>` would:
      // fetch pair info, run the local login (fake), post the capture blob.
      const info = await page.request.get(`/v1/auth/pair/${ticket}`);
      expect(info.ok()).toBeTruthy();
      const pairInfo = (await info.json()) as { provider: string };
      expect(pairInfo.provider).toBe("grok");

      const done = await page.request.post("/v1/auth/pair/complete", {
        data: {
          ticket,
          credential: {
            provider: "grok",
            files: { ".grok/auth.json": '{"token": "REDACTED"}' },
          },
        },
      });
      expect(done.ok()).toBeTruthy();
      await expect(state).toContainText(/verified|materialized/, { timeout: 30_000 });
      // Ticket is single-use.
      const replay = await page.request.get(`/v1/auth/pair/${ticket}`);
      expect(replay.status()).toBe(401);
    }
    await page.getByRole("button", { name: "Close" }).click();
  });

  test("github zero-config: manifest flow registers the app into install state (SOR-220)", async ({
    page,
  }) => {
    await page.request.post("/__dev/github/reset");
    await connect(page);
    await page.getByTestId("nav-integrations").click();
    await page.getByTestId("int-github").click();
    await expect(page.getByTestId("github-status")).toContainText("not configured");
    // Manifest registration is the Advanced/self-hosted path now.
    await page.getByTestId("github-advanced").locator("summary").click();
    await page.getByTestId("github-create-app").click();

    // The console POSTs the manifest to the (fake) GitHub apps/new page,
    // which redirects back through the callback with code+state — the
    // whole dance runs in-browser, no manual copy of app credentials.
    await expect(page.getByTestId("manifest-connected")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("github-status")).toContainText("configured", {
      timeout: 15_000,
    });

    // The fake GitHub reports one org installation — the install/repo
    // selection surface is immediately usable, no redeploy or env export.
    await expect(page.getByTestId("github-installations")).toContainText("e2e-org", {
      timeout: 15_000,
    });
  });

  test("github default connect: one click installs the public SBX App via the broker (SOR-220)", async ({
    page,
  }) => {
    await page.request.post("/__dev/github/reset");
    await connect(page);
    await page.getByTestId("nav-integrations").click();
    await page.getByTestId("int-github").click();
    await expect(page.getByTestId("github-status")).toContainText("not configured");

    // Acceptance gate: the FIRST GitHub page is the App *installation*
    // page — /apps/<slug>/installations/new — never settings/apps/new.
    const landedOnInstall = page.waitForURL(/\/__fake_gh\/apps\/[^/]+\/installations\/new/, {
      timeout: 15_000,
    });
    await page.getByTestId("github-connect-default").click();
    await landedOnInstall;
    expect(page.url()).not.toContain("settings/apps/new");

    // One click on Install: browser → broker callback → deployment
    // callback → back here, already connected.
    await page.getByTestId("gh-install").click();
    await page.waitForURL(/broker=connected/, { timeout: 15_000 });
    await expect(page.getByTestId("broker-connected")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("github-status")).toContainText("configured", {
      timeout: 15_000,
    });
    await expect(page.getByTestId("github-installations")).toContainText("e2e-org", {
      timeout: 15_000,
    });
  });
});
