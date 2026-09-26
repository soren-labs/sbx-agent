import { api } from "../lib/api.js";
import { getConnection, hasScope } from "../lib/config.js";
import { debounce, h, mount } from "../lib/dom.js";
import { explainApiError, PROVIDER_META, PROVIDERS, providerLabel } from "../lib/domain.js";
import { t } from "../lib/i18n.js";
import { icon } from "../lib/icons.js";
import { navigate } from "../lib/router.js";
import { prompts } from "../lib/store.js";
import { banner, button, card, codeBlock, field, pageHeader, segmented, skeleton, toast, toggle } from "../lib/ui.js";
import { chipInput, DEFAULT_SCHEMA, effortOptions, num, providerModels, rangeValue, uuid } from "./form-parts.js";

/** A ready task's resolved plan rendered as a human sentence + evidence. */
function resolvedSummary(resolved) {
  if (!resolved) return null;
  const ex = resolved.execution || {};
  const src = resolved.source;
  const parts = [];
  if (ex.provider) {
    parts.push(
      h(
        "span",
        null,
        `${t("Runs on")} `,
        h("strong", null, providerLabel(ex.provider)),
        ex.model ? ` · ${ex.model}` : "",
        ex.reasoning_effort ? ` · ${t("effort")} ${ex.reasoning_effort}` : "",
      ),
    );
  }
  if (src?.repo) {
    parts.push(
      h(
        "span",
        null,
        `${t("on")} `,
        h("code", null, src.slug || src.repo),
        src.base_sha ? ` @ ${String(src.base_sha).slice(0, 8)}` : src.base_ref ? ` @ ${src.base_ref}` : "",
      ),
    );
  }
  return parts.length ? h("p", { class: "resolved-line" }, parts.reduce((acc, el) => (acc.push(el, " "), acc), [])) : null;
}

const CHECK_ICON = { pass: "circleCheck", warn: "warning", fail: "circleX" };

export function renderNewTask({ route, shell }) {
  const q = route.query;
  const f = {
    prompt: q.prompt || "",
    name: q.name || "",
    repo: q.repo || "",
    ref: q.ref || q.base_ref || "",
    delivery: q.delivery === "pr" ? "pr" : q.delivery === "branch" ? "branch" : q.delivery === "none" ? "none" : null,
    branch: q.branch || "",
    prTitle: q.pr_title || "",
    prTarget: q.target || "",
    prDraft: q.draft === "true",
    provider: PROVIDERS.includes(q.provider) ? q.provider : "auto",
    model: q.model || "",
    effort: q.effort || "",
    // advanced
    account: "auto",
    useWorkflow: Boolean(q.workflow_id),
    workflowId: q.workflow_id || "",
    taskId: q.task_id || "",
    role: q.role || "worker",
    parentTaskId: q.parent_task_id || "",
    useContract: false,
    schema: DEFAULT_SCHEMA,
    enforcement: "strict",
    cpuMin: "",
    cpuMax: "",
    memMin: "",
    memMax: "",
    secrets: [],
    mcp: [],
    idleTimeout: "",
  };
  const idempotencyKey = uuid();
  let models = [];
  let accounts = [];
  let errors = {};
  let submitting = false;
  let preflightSeq = 0;

  // ------------------------------------------------------------ body
  function build() {
    const body = { prompt: { text: f.prompt } };
    if (f.name.trim()) body.name = f.name.trim();
    if (f.repo.trim()) {
      body.source = { repo: f.repo.trim() };
      if (f.ref.trim()) body.source.ref = f.ref.trim();
    }
    const execution = {};
    if (f.provider !== "auto") execution.provider = f.provider;
    if (f.account && f.account !== "auto") execution.account_id = f.account;
    if (f.model) execution.model = f.model;
    if (f.effort) execution.reasoning_effort = f.effort;
    if (Object.keys(execution).length) body.execution = execution;
    const deliveryMode = f.delivery || (f.repo.trim() ? "pr" : "none");
    if (deliveryMode !== "none" && f.repo.trim()) {
      body.delivery = {};
      if (deliveryMode === "branch") body.delivery.auto_publish = true;
      if (f.branch.trim()) body.delivery.branch = f.branch.trim();
      if (deliveryMode === "pr") {
        const pr = {};
        if (f.prTitle.trim()) pr.title = f.prTitle.trim();
        if (f.prTarget.trim()) pr.target = f.prTarget.trim();
        if (f.prDraft) pr.draft = true;
        body.delivery.pull_request = pr;
      }
    }
    if (f.useWorkflow) {
      body.metadata = { workflow_id: f.workflowId.trim(), task_id: f.taskId.trim(), role: f.role.trim() };
      if (f.parentTaskId.trim()) body.metadata.parent_task_id = f.parentTaskId.trim();
    }
    if (f.useContract) {
      let schema;
      try {
        schema = JSON.parse(f.schema);
      } catch {
        schema = "<invalid JSON>";
      }
      body.output_contract = { schema, enforcement: f.enforcement };
    }
    const cpu = rangeValue(f.cpuMin, f.cpuMax);
    const mem = rangeValue(f.memMin, f.memMax);
    if (cpu !== undefined || mem !== undefined) {
      body.compute = {};
      if (cpu !== undefined) body.compute.cpu = cpu;
      if (mem !== undefined) body.compute.memory_mib = mem;
    }
    if (f.secrets.length || f.mcp.length) {
      body.resources = {};
      if (f.secrets.length) body.resources.secrets = [...f.secrets];
      if (f.mcp.length) body.resources.mcp = [...f.mcp];
    }
    const idle = num(f.idleTimeout);
    if (idle != null) body.idle_timeout_s = idle;
    return body;
  }

  function validate() {
    const e = {};
    if (!f.prompt.trim()) e.prompt = t("Describe the task for the agent.");
    if (f.useWorkflow && (!f.workflowId.trim() || !f.taskId.trim() || !f.role.trim())) {
      e.workflow = t("Workflow id, task id and role are all required.");
    }
    if (f.useContract) {
      try {
        const parsed = JSON.parse(f.schema);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) e.schema = t("The schema must be a JSON object.");
      } catch (err) {
        e.schema = `${t("Invalid JSON")}: ${err.message}`;
      }
    }
    for (const [key, a, b] of [["cpu", f.cpuMin, f.cpuMax], ["memory", f.memMin, f.memMax]]) {
      const x = num(a);
      const y = num(b);
      if (Number.isNaN(x) || Number.isNaN(y)) e.compute = t("Compute values must be numbers.");
      else if (x != null && y != null && x > y) e.compute = t("The {key} request must not exceed the limit.", { key });
    }
    const idle = num(f.idleTimeout);
    if (Number.isNaN(idle) || (idle != null && idle < 1)) e.idle = t("Must be a positive number of seconds.");
    return e;
  }

  // --------------------------------------------------------- preflight
  const preflightEl = h("div", { class: "readiness", "data-testid": "preflight" });

  function renderPreflightIdle(text) {
    mount(preflightEl, h("p", { class: "muted" }, text));
  }

  async function runPreflight() {
    const seq = ++preflightSeq;
    if (!f.prompt.trim()) {
      renderPreflightIdle(t("Describe the task to check readiness."));
      return;
    }
    mount(preflightEl, skeleton(2));
    try {
      const res = await api.taskPreflight(build());
      if (seq !== preflightSeq || !preflightEl.isConnected) return;
      const checks = res.checks || [];
      mount(
        preflightEl,
        h(
          "ul",
          { class: "check-list", "data-testid": "preflight-checks" },
          checks.map((c) =>
            h(
              "li",
              { class: ["check", `check-${c.status}`] },
              icon(CHECK_ICON[c.status] || "info", { size: 15 }),
              h("span", null, h("strong", null, c.name), " ", h("span", { class: "muted" }, c.detail)),
            ),
          ),
        ),
        res.ok ? resolvedSummary(res.resolved) : null,
        !res.ok && res.error ? h("p", { class: "field-error" }, `${res.error.code}: ${res.error.message}`) : null,
      );
      if (!res.ok) {
        submitBtn.title = t("Resolve the failing check above first");
      } else {
        submitBtn.title = "";
      }
    } catch (err) {
      if (seq !== preflightSeq || !preflightEl.isConnected) return;
      const info = explainApiError(err);
      mount(preflightEl, h("p", { class: "field-error", "data-testid": "preflight-error" }, `${info.title}`));
    }
  }
  const refreshPreflight = debounce(() => void runPreflight(), 450);

  function touched() {
    refreshPreflight();
    refreshPreview();
  }

  // --------------------------------------------------------- preview
  const previewTabs = { current: "json" };
  const previewBody = h("div");
  function renderPreview() {
    const body = build();
    const json = JSON.stringify(body, null, 2);
    const base = getConnection().baseUrl || window.location.origin;
    let text = json;
    if (previewTabs.current === "curl") {
      text = `curl -X POST "${base}/v1/tasks" \\\n  -H "Authorization: Bearer $SBX_API_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '${json.replace(/'/g, "'\\''")}'`;
    } else if (previewTabs.current === "python") {
      text = `import os, httpx\n\nbody = ${json.replace(/\btrue\b/g, "True").replace(/\bfalse\b/g, "False").replace(/\bnull\b/g, "None")}\n\nres = httpx.post(\n    f"{os.environ['SBX_BASE_URL']}/v1/tasks",\n    headers={"Authorization": f"Bearer {os.environ['SBX_API_KEY']}"},\n    json=body,\n)\nres.raise_for_status()\ntask = res.json()["task"]`;
    }
    mount(previewBody, codeBlock(text, { testid: "request-preview" }));
  }
  const refreshPreview = debounce(renderPreview, 80);

  // ------------------------------------------------------------ inputs
  const input = (key, attrs = {}) =>
    h("input", {
      class: ["input", attrs.mono && "mono"],
      value: f[key],
      id: `f-${key}`,
      "data-testid": `f-${key}`,
      ...attrs,
      mono: null,
      onInput: (ev) => {
        f[key] = ev.target.value;
        touched();
      },
    });

  const errorSlot = h("div");

  const promptArea = h("textarea", {
    class: "textarea",
    id: "f-prompt",
    rows: 5,
    placeholder: t("e.g. Add a /health endpoint with a test, then run the test suite."),
    "data-testid": "f-prompt",
    onInput: (ev) => {
      f.prompt = ev.target.value;
      touched();
    },
    onKeydown: (ev) => {
      if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) {
        ev.preventDefault();
        void submit();
      }
    },
  });
  promptArea.value = f.prompt;

  const promptHint = h("p", { class: "field-hint" }, t("⌘/Ctrl + Enter to submit."));
  const promptError = h("p", { class: "field-error", hidden: true });
  const taskCard = card({
    title: t("Task"),
    iconName: "message",
    body: h(
      "div",
      { class: "fields" },
      h(
        "div",
        { class: "field" },
        h("label", { class: "field-label", for: "f-prompt" }, t("What should the agent do?"), h("span", { class: "req", "aria-hidden": "true" }, " *")),
        promptArea,
        promptError,
        promptHint,
      ),
      field(t("Title"), input("name", { placeholder: t("Optional — a short title is generated from the prompt") }), { htmlFor: "f-name" }),
    ),
  });

  // ------------------------------------------------------- repository
  const repoInput = input("repo", { mono: true, placeholder: "owner/repo or https://github.com/owner/repo" });
  // The delivery choices unlock on a non-empty repo — re-render them per
  // keystroke instead of waiting for a full section render.
  repoInput.addEventListener("input", () => renderDelivery());
  const repoCard = card({
    title: t("Repository"),
    subtitle: t("Optional — needed when the task changes code."),
    iconName: "branch",
    body: h(
      "div",
      { class: "fields" },
      field(t("Repository"), repoInput, {
        htmlFor: "f-repo",
        hint: t("Private GitHub repos need the GitHub integration. Leave empty for a plain sandbox task."),
      }),
      field(t("Starting point"), input("ref", { mono: true, placeholder: t("Default branch — or a branch, tag or commit") }), {
        htmlFor: "f-ref",
        hint: t("Blank uses the repository's default branch; the exact commit is resolved for you."),
      }),
    ),
  });

  // ------------------------------------------------------------- AI
  function aiControls() {
    const options = [
      h("option", { value: "auto" }, t("Automatic — pick a free provider and model")),
      ...PROVIDERS.map((p) => {
        const pm = providerModels(models, p, "auto");
        const free = pm.reduce((n, m) => Math.max(n, m.accounts_available || 0), 0);
        return h(
          "option",
          { value: p, selected: f.provider === p, disabled: models.length > 0 && !pm.length },
          `${providerLabel(p)}${models.length ? (pm.length ? ` — ${t("{n} free account(s)", { n: free })}` : ` — ${t("not enabled")}`) : ` — ${t(PROVIDER_META[p].tier === "stable" ? "Stable" : "Experimental")}`}`,
        );
      }),
    ];
    const providerSelect = h(
      "select",
      {
        class: "select",
        id: "f-provider",
        "data-testid": "f-provider",
        onChange: (ev) => {
          f.provider = ev.target.value;
          f.model = "";
          f.effort = "";
          renderDynamic();
          touched();
        },
      },
      options,
    );
    if (f.provider === "auto") return [field(t("AI"), providerSelect, { htmlFor: "f-provider", hint: t("The scheduler picks the provider and account with a free slot and the capability you need.") })];

    const provModels = providerModels(models, f.provider, f.account);
    if (f.model && provModels.length && !provModels.some((m) => m.model === f.model)) f.model = "";
    const selectedRows = f.model ? provModels.filter((m) => m.model === f.model || (m.aliases || []).includes(f.model)) : provModels;
    const efforts = effortOptions(selectedRows);
    if (f.effort && !efforts.includes(f.effort)) f.effort = "";
    const provAccounts = accounts.filter((a) => a.provider === f.provider);

    const modelSelect = h(
      "select",
      { class: "select", id: "f-model", "data-testid": "f-model", onChange: (ev) => { f.model = ev.target.value; renderDynamic(); touched(); } },
      h("option", { value: "" }, provModels.length ? t("Default ({model})", { model: provModels[0].model }) : t("Provider default")),
      provModels.map((m) => h("option", { value: m.model, selected: f.model === m.model }, `${m.display_name || m.model} (${m.model}) — ${t("{n} free", { n: m.accounts_available || 0 })}`)),
    );
    const effortControl = segmented(
      [{ value: "", label: t("Default") }, ...efforts.map((v) => ({ value: v, label: t(v) }))],
      f.effort,
      (v) => { f.effort = v; touched(); },
      { testid: "f-effort" },
    );
    const accountControl = hasScope("admin")
      ? h(
          "select",
          { class: "select", "data-testid": "f-account", onChange: (ev) => { f.account = ev.target.value; renderDynamic(); touched(); } },
          h("option", { value: "auto" }, t("Auto — scheduler picks a free account")),
          provAccounts.map((a) => h("option", { value: a.id, selected: f.account === a.id, disabled: a.status !== "active" }, `${a.label} (${a.id}) — ${a.running}/${a.max_concurrent} · ${a.status}`)),
        )
      : h("input", { class: "input mono", value: f.account, "data-testid": "f-account", onInput: (ev) => { f.account = ev.target.value.trim() || "auto"; refreshPreview(); }, onChange: () => { renderDynamic(); refreshPreflight(); } });

    return [
      field(t("AI"), providerSelect, { htmlFor: "f-provider" }),
      h("div", { class: "fields-2" }, field(t("Model"), modelSelect, { htmlFor: "f-model" }), field(t("Account"), accountControl)),
      field(t("Reasoning effort"), effortControl, {
        hint: efforts.length ? t("Applies to every run of this task.") : t("{provider} has no native effort setting.", { provider: providerLabel(f.provider) }),
      }),
    ];
  }

  const aiBody = h("div", { class: "fields" });
  const aiCard = card({ title: t("AI"), subtitle: t("Which agent does the work."), iconName: "sparkles", body: aiBody });

  // --------------------------------------------------------- delivery
  const hasRepo = () => Boolean(f.repo.trim());
  const deliveryBody = h("div");
  function deliveryChoice(value, iconName, title, body, testid) {
    const disabled = value !== "none" && !hasRepo();
    return h(
      "button",
      {
        type: "button",
        role: "radio",
        class: ["choice", (f.delivery || (hasRepo() ? "pr" : "none")) === value && "is-active"],
        "aria-checked": String((f.delivery || (hasRepo() ? "pr" : "none")) === value),
        "data-testid": testid,
        "data-value": value,
        disabled,
        title: disabled ? t("Add a repository first") : null,
        onClick: () => {
          f.delivery = value;
          renderDynamic();
          touched();
        },
      },
      icon(iconName, { size: 18 }),
      h("span", { class: "choice-title" }, title),
      h("span", { class: "choice-sub" }, body),
    );
  }

  function renderDelivery() {
    const current = f.delivery || (hasRepo() ? "pr" : "none");
    mount(
      deliveryBody,
      h(
        "div",
        { class: "choice-grid", role: "radiogroup", "data-testid": "f-delivery" },
        deliveryChoice("none", "message", t("Report only"), t("The answer lands in the conversation; nothing is pushed.")),
        deliveryChoice("branch", "branch", t("Push to a branch"), t("The agent's changes are pushed to a new branch.")),
        deliveryChoice("pr", "pullRequest", t("Open a pull request"), t("Push the branch and open a PR for review.")),
      ),
      current === "branch"
        ? field(t("Branch name"), input("branch", { mono: true, placeholder: t("auto-generated from the task") }), { hint: t("Created on the resolved base commit.") })
        : null,
      current === "pr"
        ? h(
            "div",
            { class: "fields", style: "margin-top:12px" },
            field(t("PR title"), input("prTitle", { placeholder: t("defaults to the task title") })),
            h(
              "div",
              { class: "fields-2" },
              field(t("PR target"), input("prTarget", { mono: true, placeholder: f.ref || t("default branch") }), { hint: t("Defaults to the starting point above.") }),
              h("div", { style: "align-self:end" }, toggle(t("Open as draft"), f.prDraft, (v) => { f.prDraft = v; touched(); }, { testid: "f-draft" })),
            ),
          )
        : null,
    );
  }

  const deliveryCard = card({
    title: t("Result"),
    subtitle: t("Where the finished work goes."),
    iconName: "pullRequest",
    body: deliveryBody,
  });

  // -------------------------------------------------------- advanced
  const schemaArea = h("textarea", {
    class: "textarea mono",
    rows: 7,
    "data-testid": "f-schema",
    onInput: (ev) => {
      f.schema = ev.target.value;
      touched();
    },
  });
  schemaArea.value = f.schema;

  const advancedBody = h("div", { class: "card-body fields" });
  function renderAdvanced() {
    mount(
      advancedBody,
      errors.compute ? h("p", { class: "field-error" }, errors.compute) : null,
      errors.workflow ? h("p", { class: "field-error" }, errors.workflow) : null,
      field(
        t("Workflow binding"),
        toggle(t("Attach to a workflow"), f.useWorkflow, (v) => { f.useWorkflow = v; renderAdvanced(); touched(); }, { testid: "toggle-workflow" }),
      ),
      f.useWorkflow
        ? h(
            "div",
            { class: "fields" },
            h("div", { class: "fields-2" }, field(t("Workflow id"), input("workflowId", { mono: true, placeholder: "release-42" }), { required: true }), field(t("Task id"), input("taskId", { mono: true, placeholder: "implement" }), { required: true })),
            h("div", { class: "fields-2" }, field(t("Role"), input("role", { placeholder: "worker / reviewer" }), { required: true }), field(t("Parent task id"), input("parentTaskId", { mono: true }))),
          )
        : null,
      field(
        t("Structured output"),
        toggle(t("Require a JSON result"), f.useContract, (v) => { f.useContract = v; renderAdvanced(); touched(); }, { testid: "toggle-contract", hint: t("The final message must match a schema.") }),
      ),
      f.useContract
        ? h(
            "div",
            { class: "fields" },
            field(t("JSON Schema"), schemaArea, { error: errors.schema, hint: t("Assertion keywords only; $ref, pattern and conditionals are refused.") }),
            field(
              t("Enforcement"),
              segmented(
                [
                  { value: "strict", label: t("Strict — invalid output fails the run") },
                  { value: "warn", label: t("Warn — keep the run, attach a diagnostic") },
                ],
                f.enforcement,
                (v) => { f.enforcement = v; touched(); },
              ),
            ),
          )
        : null,
      h(
        "div",
        { class: "fields-2" },
        field(t("CPU cores (request – limit)"), h("div", { class: "input-group" }, input("cpuMin", { type: "number", step: "0.125", min: "0.125", placeholder: "1" }), h("span", { class: "subtle" }, "–"), input("cpuMax", { type: "number", step: "0.125", min: "0.125", placeholder: "2" })), { hint: t("Default 1 – 2 cores.") }),
        field(t("Memory MiB (request – limit)"), h("div", { class: "input-group" }, input("memMin", { type: "number", step: "128", min: "128", placeholder: "1024" }), h("span", { class: "subtle" }, "–"), input("memMax", { type: "number", step: "128", min: "128", placeholder: "8192" })), { hint: t("Default 1024 – 8192 MiB. Cost is estimated at the request.") }),
      ),
      field(t("Secrets"), chipInput(f.secrets, () => touched(), { placeholder: t("Modal Secret names, Enter to add"), testid: "f-secrets" }), { hint: t("Only names allow-listed by the operator (SBX_RESOURCE_SECRETS).") }),
      field(t("MCP servers"), chipInput(f.mcp, () => touched(), { placeholder: t("Registry names, Enter to add"), testid: "f-mcp" }), { hint: t("From the deployment's MCP registry. Only Devin supports MCP today.") }),
      field(t("Idle timeout (seconds)"), input("idleTimeout", { type: "number", min: "1", placeholder: "300" }), { error: errors.idle, hint: t("How long an idle agent is kept before it is reclaimed.") }),
    );
  }

  const advancedCard = h(
    "details",
    { class: "card", "data-testid": "advanced" },
    h("summary", { class: "card-header", style: "cursor:pointer;list-style:none" }, h("div", { class: "card-title" }, icon("cpu"), h("div", null, h("h2", null, t("Advanced")), h("p", { class: "muted" }, t("Account pinning, workflow binding, structured output, compute, secrets and MCP.")))), icon("chevronDown", { className: "muted-icon" })),
    advancedBody,
  );

  const previewDetails = h(
    "details",
    { class: "card", "data-testid": "api-preview" },
    h(
      "summary",
      { class: "card-header", style: "cursor:pointer;list-style:none" },
      h("div", { class: "card-title" }, icon("code"), h("div", null, h("h2", null, t("API request")), h("p", { class: "muted" }, t("Exactly what the console will send to POST /v1/tasks.")))),
      icon("chevronDown", { className: "muted-icon" }),
    ),
    h(
      "div",
      { class: "card-body fields" },
      h(
        "div",
        null,
        segmented(
          [
            { value: "json", label: "JSON" },
            { value: "curl", label: "cURL" },
            { value: "python", label: "Python" },
          ],
          previewTabs.current,
          (v) => {
            previewTabs.current = v;
            renderPreview();
          },
          { size: "sm", testid: "preview-tabs" },
        ),
      ),
      previewBody,
    ),
  );

  const dynamicSections = h("div", { class: "stack" }, deliveryCard, advancedCard);
  function renderDynamic() {
    mount(aiBody, ...aiControls());
    renderDelivery();
    renderAdvanced();
  }

  // ---------------------------------------------------------- submit
  async function submit() {
    if (submitting) return;
    errors = validate();
    if (Object.keys(errors).length) {
      renderAdvanced();
      errorSlot.replaceChildren(banner({ tone: "danger", title: t("Fix the highlighted fields."), testid: "form-error" }));
      promptError.hidden = !errors.prompt;
      promptError.textContent = errors.prompt || "";
      promptHint.hidden = Boolean(errors.prompt);
      promptArea.setAttribute("aria-invalid", errors.prompt ? "true" : "false");
      document.querySelector(".field-error")?.scrollIntoView({ block: "center", behavior: "smooth" });
      if (errors.schema || errors.workflow || errors.compute || errors.idle) advancedCard.open = true;
      return;
    }
    errorSlot.replaceChildren();
    submitting = true;
    submitBtn.disabled = true;
    submitBtn.classList.add("is-busy");
    try {
      const res = await api.createTask(build(), idempotencyKey);
      const task = res.task || {};
      if (res.agent?.id && res.run?.id) prompts.set(res.agent.id, res.run.id, f.prompt);
      toast(t("Task created"), { tone: "success" });
      shell?.bumpLive();
      navigate(`/tasks/${encodeURIComponent(task.id || res.agent?.id || "")}`);
    } catch (err) {
      const info = explainApiError(err);
      errorSlot.replaceChildren(
        banner({ tone: "danger", title: info.title, body: h("span", { class: "muted" }, `${info.code}${info.detail && info.detail !== info.title ? ` · ${info.detail}` : ""}`), testid: "form-error" }),
      );
      errorSlot.scrollIntoView({ block: "center", behavior: "smooth" });
    } finally {
      submitting = false;
      submitBtn.disabled = false;
      submitBtn.classList.remove("is-busy");
    }
  }

  const submitBtn = button(t("Create task"), { variant: "primary", iconName: "zap", testid: "create-task", onClick: () => void submit() });

  const el = h(
    "div",
    { class: "page page-narrow" },
    pageHeader({
      title: t("New task"),
      subtitle: t("Describe the outcome; the console resolves the repository, the agent and the delivery for you."),
      back: { href: "#/tasks", label: t("Tasks") },
      testid: "page-title",
    }),
    h(
      "div",
      { class: "stack" },
      errorSlot,
      taskCard,
      repoCard,
      aiCard,
      card({ title: t("Readiness"), subtitle: t("Live checks — repo access, ref resolution and account capacity."), iconName: "shield", body: preflightEl }),
      dynamicSections,
      previewDetails,
      h("div", { class: "form-footer" }, h("a", { class: "btn", href: "#/tasks" }, t("Cancel")), submitBtn),
    ),
  );

  renderDynamic();
  renderPreview();
  renderPreflightIdle(t("Readiness checks run as you fill in the form."));
  (async () => {
    try {
      models = (await api.models()).models || [];
    } catch {
      models = [];
    }
    if (hasScope("admin")) {
      try {
        accounts = (await api.listAccounts()).accounts || [];
      } catch {
        accounts = [];
      }
    }
    if (el.isConnected || document.contains(el)) {
      renderDynamic();
      renderPreview();
      void runPreflight();
    }
  })();
  queueMicrotask(() => promptArea.focus());
  return { el, title: t("New task") };
}
