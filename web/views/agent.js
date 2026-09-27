import { api } from "../lib/api.js";
import { h, mount } from "../lib/dom.js";
import { isAgentEnded, isAgentLive, isRunLive } from "../lib/domain.js";
import { fmtDateTime, fmtDuration, fmtMemory, fmtNumber, fmtRange, fmtRelative, fmtUsd } from "../lib/format.js";
import { t } from "../lib/i18n.js";
import { icon } from "../lib/icons.js";
import { href, navigate } from "../lib/router.js";
import { openStream } from "../lib/sse.js";
import { prompts } from "../lib/store.js";
import {
  actionButton,
  badge,
  banner,
  button,
  card,
  confirmDialog,
  emptyState,
  errorBanner,
  jsonView,
  kv,
  mono,
  pageHeader,
  poller,
  providerTag,
  segmented,
  skeleton,
  statusBadge,
  tabs,
  toast,
  toastError,
  toggle,
} from "../lib/ui.js";
import { workflowChip } from "./agents.js";
import { artifactTable, snapshotDialog } from "./artifacts.js";
import { renderTaskChanges } from "./task-changes.js";
import { createRunBlock } from "./timeline.js";
import { renderWorkspaceTab } from "./workspace.js";

const AUTO_ACTIVITY_RUNS = 3;

export function renderAgent({ route, shell, agentId: agentIdOverride, taskId, getTask, extraDetails }) {
  const agentId = agentIdOverride || route.params.id;
  const embedded = Boolean(taskId);
  const tabBase = embedded ? `/tasks/${encodeURIComponent(taskId)}` : `/agents/${encodeURIComponent(agentId)}`;
  const TAB_IDS = embedded
    ? ["conversation", "changes", "details"]
    : ["conversation", "workspace", "artifacts", "details"];
  const tab = TAB_IDS.includes(route.query.tab) ? route.query.tab : "conversation";
  const state = {
    agent: null,
    runs: [],
    usage: null,
    workspace: undefined,
    error: null,
    sending: false,
  };
  const blocks = new Map();
  const streams = new Map();
  const idleTimers = new Map();
  let changesView = null;
  let disposed = false;

  const headerEl = h("div");
  const tabsEl = h("div");
  const bodyEl = h("div");
  const el = h("div", { class: "page page-wide", "data-testid": "agent-view" }, headerEl, tabsEl, bodyEl);

  // ---------------------------------------------------------- data
  const activeRun = () => state.runs.find((r) => isRunLive(r.status));
  const live = () => state.agent && isAgentLive(state.agent.status);

  async function loadAgent() {
    const [agent, runs] = await Promise.all([api.getAgent(agentId), api.listRuns(agentId)]);
    state.agent = agent;
    state.runs = (runs.runs || []).slice().sort((a, b) => (a.created_at || "").localeCompare(b.created_at || ""));
    state.error = null;
  }

  async function loadUsage() {
    try {
      state.usage = await api.usage(agentId);
    } catch {
      // Usage is decorative; the header still renders without it.
    }
  }

  async function loadWorkspace() {
    try {
      state.workspace = (await api.workspace(agentId)).workspace;
    } catch (err) {
      state.workspace = err.status === 404 ? null : state.workspace ?? null;
    }
  }

  async function refresh() {
    try {
      await loadAgent();
    } catch (err) {
      state.error = err;
    }
    if (disposed) return;
    renderHeader();
    if (tab === "conversation") syncConversation();
    if (tab === "changes") void changesView?.refresh();
  }

  // ------------------------------------------------------- streams
  function closeStream(runId) {
    streams.get(runId)?.close();
    streams.delete(runId);
    clearTimeout(idleTimers.get(runId));
    idleTimers.delete(runId);
  }

  function armIdleClose(runId) {
    // A terminal run's replay has no live tail; close once it goes quiet.
    clearTimeout(idleTimers.get(runId));
    const block = blocks.get(runId);
    if (!block || isRunLive(block.run.status)) return;
    idleTimers.set(runId, setTimeout(() => closeStream(runId), 4000));
  }

  function openRunStream(runId) {
    if (streams.has(runId) || disposed) return;
    const block = blocks.get(runId);
    const handle = openStream(api.streamPath(agentId, runId), {
      onStatus: (value) => {
        block?.setStreamState(value);
        if (value === "live") armIdleClose(runId);
        if (value === "gave_up") {
          // The stream is done but the run may have settled unseen —
          // refresh state and usage once instead of polling for it.
          void (async () => {
            await refresh();
            await loadUsage();
            renderHeader();
            if (tab === "conversation") renderAside();
          })();
        }
      },
      onEvent: (ev) => {
        blocks.get(runId)?.applyEvent(ev);
        if (ev.type === "sbx.turn_finished") {
          closeStream(runId);
          void (async () => {
            await refresh();
            await loadUsage();
            renderHeader();
            shell?.bumpLive();
            if (state.workspace) {
              await loadWorkspace();
            }
          })();
        } else {
          armIdleClose(runId);
        }
      },
    });
    streams.set(runId, handle);
  }

  // --------------------------------------------------------- header
  function renderHeader() {
    const a = state.agent;
    if (!a) {
      mount(headerEl, embedded ? null : pageHeader({ title: agentId, back: { href: "#/agents", label: t("Agents") } }));
      return;
    }
    const run = activeRun();
    const actions = [];
    // Embedded (task page): the task header owns Cancel/Retry; the run-scoped
    // action stays only on the standalone agent page.
    if (run && !embedded) {
      actions.push(
        actionButton(t("Cancel run"), async () => {
          try {
            await api.cancelRun(agentId, run.id);
            toast(t("Run cancelled"), { tone: "success" });
          } catch (err) {
            toastError(err, t("Could not cancel"));
          }
          await refresh();
        }, { variant: "danger", iconName: "stop", testid: "cancel-run" }),
      );
    }
    if (state.workspace && live() && !embedded) {
      actions.push(button(t("Snapshot"), { iconName: "camera", testid: "snapshot", onClick: () => snapshotDialog(agentId, state.runs, () => navigate(tabBase, { tab: "artifacts" })) }));
    }
    if (!isAgentEnded(a.status)) {
      actions.push(
        actionButton(t(embedded ? "Close" : "Close agent"), async () => {
          const ok = await confirmDialog({
            title: t("Close this agent?"),
            body: t("The sandbox is reclaimed and any running run is cancelled. Run history and artifacts stay available read-only."),
            confirmLabel: t("Close agent"),
          });
          if (!ok) return;
          try {
            await api.closeAgent(agentId);
            toast(t("Agent closed"), { tone: "success" });
          } catch (err) {
            toastError(err, t("Could not close the agent"));
          }
          for (const id of [...streams.keys()]) closeStream(id);
          await refresh();
          shell?.bumpLive();
        }, { variant: "secondary", iconName: "x", testid: "close-agent" }),
      );
    }
    if (embedded) {
      // The task page owns the page header; the embedded agent keeps its
      // tabs, run actions and lifecycle banners.
      mount(
        headerEl,
        a.status === "creating"
          ? banner({ tone: "info", title: t("Starting the sandbox"), body: t("Cold starts take a few seconds. The first run begins as soon as the provider CLI is ready."), testid: "cold-start" })
          : null,
        isAgentEnded(a.status)
          ? banner({
              tone: a.status === "lost" ? "danger" : "neutral",
              title: { closed: t("This agent is closed"), timed_out: t("This agent timed out"), lost: t("This agent's sandbox was lost") }[a.status] || t("This agent has ended"),
              body: t("Its history is read-only. Start a new task to continue the work — hand off an artifact to keep the changes."),
              testid: "readonly-banner",
            })
          : null,
      );
      mount(
        tabsEl,
        h(
          "div",
          { class: "tab-row" },
          tabs(
            [
              { id: "conversation", label: t("Conversation"), iconName: "message", href: href(tabBase), count: state.runs.length || null },
              { id: "changes", label: t("Changes"), iconName: "fileDiff", href: href(tabBase, { tab: "changes" }) },
              { id: "details", label: t("Details"), iconName: "info", href: href(tabBase, { tab: "details" }) },
            ],
            tab,
            { testid: "agent-tabs" },
          ),
          actions.length ? h("div", { class: "tab-actions" }, actions) : null,
        ),
      );
      return;
    }
    mount(
      headerEl,
      pageHeader({
        title: a.name || a.id,
        testid: "agent-title",
        back: { href: "#/agents", label: t("Agents") },
        actions,
        meta: [
          statusBadge("agent", a.status, { testid: "agent-status" }),
          h("span", null, providerTag(a.provider), h("span", { class: "mono" }, a.model)),
          a.reasoning_effort ? badge(`${t("effort")} ${a.reasoning_effort}`) : null,
          h("span", { title: t("Account") }, icon("users", { size: 13 }), a.account_id),
          mono(a.id, { testid: "agent-id" }),
          workflowChip(a.metadata),
          h("span", { title: fmtDateTime(a.created_at) }, icon("clock", { size: 13 }), `${t("created")} ${fmtRelative(a.created_at)}`),
        ],
      }),
      a.status === "creating"
        ? banner({ tone: "info", title: t("Starting the sandbox"), body: t("Cold starts take a few seconds. The first run begins as soon as the provider CLI is ready."), testid: "cold-start" })
        : null,
      isAgentEnded(a.status)
        ? banner({
            tone: a.status === "lost" ? "danger" : "neutral",
            title: { closed: t("This agent is closed"), timed_out: t("This agent timed out"), lost: t("This agent's sandbox was lost") }[a.status] || t("This agent has ended"),
            body: t("Its history is read-only. Start a new agent to continue the work — hand off an artifact to keep the changes."),
            testid: "readonly-banner",
          })
        : null,
    );
    mount(
      tabsEl,
      tabs(
        [
          { id: "conversation", label: t("Conversation"), iconName: "message", href: href(tabBase), count: state.runs.length || null },
          { id: "workspace", label: t("Workspace"), iconName: "branch", href: href(tabBase, { tab: "workspace" }) },
          { id: "artifacts", label: t("Artifacts"), iconName: "package", href: href(tabBase, { tab: "artifacts" }) },
          { id: "details", label: t("Details"), iconName: "info", href: href(tabBase, { tab: "details" }) },
        ],
        tab,
        { testid: "agent-tabs" },
      ),
    );
  }

  // --------------------------------------------------- conversation
  const conversationEl = h("div", { class: "conversation", "data-testid": "conversation" });
  const composerEl = h("div", { class: "composer" });
  const asideEl = h("aside", { class: "agent-aside" });
  const composerState = { text: "", useContract: false, schema: '{\n  "type": "object"\n}', enforcement: "strict", optionsOpen: false };

  function syncConversation() {
    if (!state.agent) return;
    const agentLive = live();
    if (!state.runs.length) {
      mount(conversationEl, emptyState({ iconName: "message", title: t("No runs yet"), compact: true }));
    } else if (conversationEl.querySelector(".empty")) {
      mount(conversationEl);
    }
    state.runs.forEach((run, idx) => {
      let block = blocks.get(run.id);
      if (!block) {
        block = createRunBlock({
          agentId,
          provider: state.agent.provider,
          run,
          agentLive,
          onLoadActivity: (runId) => openRunStream(runId),
        });
        blocks.set(run.id, block);
        conversationEl.append(block.el);
        const recent = idx >= state.runs.length - AUTO_ACTIVITY_RUNS;
        // Ended agents replay the durable transcript captured at turn end.
        if (isRunLive(run.status) ? agentLive : recent) openRunStream(run.id);
      } else {
        block.update(run);
        block.setAgentLive(agentLive);
        if (isRunLive(run.status) && agentLive) openRunStream(run.id);
      }
    });
    renderComposer();
    renderAside();
  }

  function composerBlockReason() {
    const a = state.agent;
    if (!a) return t("Loading…");
    if (isAgentEnded(a.status)) return t("This agent has ended — history is read-only.");
    if (a.status === "creating") return t("Waiting for the sandbox to start…");
    if (activeRun()) return t("A run is in progress. Cancel it or wait for it to finish.");
    return null;
  }

  async function send() {
    const text = composerState.text.trim();
    if (!text || state.sending || composerBlockReason()) return;
    const body = { prompt: { text } };
    if (composerState.useContract) {
      try {
        body.output_contract = { schema: JSON.parse(composerState.schema), enforcement: composerState.enforcement };
      } catch (err) {
        toast(t("The output contract schema is not valid JSON."), { tone: "danger", detail: err.message });
        return;
      }
    }
    state.sending = true;
    renderComposer();
    try {
      const created = embedded ? await api.createTaskRun(taskId, body) : await api.createRun(agentId, body);
      const run = created?.run ?? created;
      composerState.text = "";
      prompts.set(agentId, run.id, text);
      state.runs.push(run);
      syncConversation();
      openRunStream(run.id);
      shell?.bumpLive();
      queueMicrotask(() => blocks.get(run.id)?.el.scrollIntoView({ block: "start", behavior: "smooth" }));
    } catch (err) {
      toastError(err, t("Could not send"));
    } finally {
      state.sending = false;
      renderComposer();
      await refresh();
    }
  }

  // Persistent textarea: re-creating it on every poll would drop keystrokes.
  const composerTextarea = h("textarea", {
    rows: 2,
    "data-testid": "composer",
    "aria-label": t("Follow-up message"),
    onInput: (ev) => {
      composerState.text = ev.target.value;
      ev.target.style.height = "auto";
      ev.target.style.height = `${Math.min(ev.target.scrollHeight, 260)}px`;
      renderComposerRow();
    },
    onKeydown: (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        void send();
      }
    },
  });
  const composerOptions = h("div");
  const composerRow = h("div", { class: "composer-row" });
  mount(composerEl, h("div", { class: "composer-box" }, composerTextarea, composerOptions, composerRow));

  function renderComposerRow() {
    const reason = composerBlockReason();
    const disabled = Boolean(reason) || state.sending;
    mount(
      composerRow,
      h(
        "div",
        { class: "row", style: "gap:6px" },
        button(t("Options"), {
          variant: "ghost",
          size: "xs",
          iconName: composerState.optionsOpen ? "chevronDown" : "chevronRight",
          onClick: () => {
            composerState.optionsOpen = !composerState.optionsOpen;
            renderComposer();
          },
        }),
        h("span", { class: "composer-hint" }, reason ? "" : t("Enter to send · Shift+Enter for a new line")),
      ),
      button(state.sending ? t("Sending…") : t("Send"), {
        variant: "primary",
        size: "sm",
        iconName: "send",
        disabled: disabled || !composerState.text.trim(),
        busy: state.sending,
        testid: "send",
        onClick: () => void send(),
      }),
    );
  }

  function renderComposer() {
    const reason = composerBlockReason();
    composerTextarea.disabled = Boolean(reason);
    composerTextarea.placeholder = reason || t("Send a follow-up — the agent keeps its sandbox and session.");
    if (composerTextarea.value !== composerState.text) composerTextarea.value = composerState.text;
    mount(
      composerOptions,
      composerState.optionsOpen
        ? h(
            "div",
            { class: "fields", style: "padding-top:4px" },
            toggle(t("Require structured output for this run"), composerState.useContract, (v) => {
              composerState.useContract = v;
              renderComposer();
            }),
            composerState.useContract
              ? h(
                  "div",
                  { class: "fields" },
                  h("textarea", {
                    class: "textarea mono",
                    rows: 5,
                    value: composerState.schema,
                    onInput: (ev) => {
                      composerState.schema = ev.target.value;
                    },
                  }),
                  segmented(
                    [
                      { value: "strict", label: t("Strict") },
                      { value: "warn", label: t("Warn") },
                    ],
                    composerState.enforcement,
                    (v) => {
                      composerState.enforcement = v;
                    },
                    { size: "sm" },
                  ),
                )
              : null,
          )
        : null,
    );
    renderComposerRow();
  }

  function renderAside() {
    const a = state.agent;
    if (!a) return;
    const u = state.usage?.usage ?? a.usage;
    const stat = (label, value) => h("div", { class: "stat" }, h("span", { class: "stat-label" }, label), h("span", { class: "stat-value" }, value));
    mount(
      asideEl,
      card({
        title: t("Usage"),
        iconName: "zap",
        class: "aside-card",
        testid: "usage-card",
        body: h(
          "div",
          { class: "usage-grid" },
          stat(t("Input tokens"), u ? fmtNumber(u.input_tokens) : "—"),
          stat(t("Output tokens"), u ? fmtNumber(u.output_tokens) : "—"),
          stat(t("Cached input"), u ? fmtNumber(u.cached_input_tokens) : "—"),
          stat(t("Est. cost"), fmtUsd(state.usage?.cost_estimate_usd ?? a.cost_estimate_usd)),
          stat(t("Sandbox time"), state.usage ? fmtDuration(state.usage.sandbox_seconds) : "—"),
          stat(t("Runs"), String(state.runs.length)),
        ),
      }),
      card({
        title: t("Sandbox"),
        iconName: "cpu",
        class: "aside-card",
        body: kv([
          [t("CPU"), a.compute ? `${fmtRange(a.compute.cpu)} ${t("cores")}` : null],
          [t("Memory"), a.compute ? fmtRange(a.compute.memory_mib, fmtMemory) : null],
          [t("Secrets"), a.resources?.secrets?.length ? a.resources.secrets.map((s) => badge(s, { mono: true })) : null],
          [t("MCP"), a.resources?.mcp?.length ? a.resources.mcp.map((s) => badge(s, { mono: true })) : null],
        ]),
      }),
    );
  }

  // ------------------------------------------------------- details
  function renderDetails() {
    const a = state.agent;
    mount(
      bodyEl,
      extraDetails || null,
      h(
        "div",
        { class: "grid-2" },
        card({
          title: t("Agent"),
          iconName: "bot",
          body: kv([
            [t("Id"), mono(a.id)],
            [t("Name"), a.name],
            [t("Status"), statusBadge("agent", a.status)],
            [t("Provider"), providerTag(a.provider)],
            [t("Model"), h("code", null, a.model)],
            [t("Reasoning effort"), a.reasoning_effort],
            [t("Account"), h("code", null, a.account_id)],
            [t("Created"), fmtDateTime(a.created_at)],
            [t("Updated"), fmtDateTime(a.updated_at)],
          ]),
        }),
        h(
          "div",
          { class: "stack" },
          card({
            title: t("Workflow binding"),
            iconName: "workflow",
            body: a.metadata
              ? kv([
                  [t("Workflow"), h("a", { href: href(`/workflows/${encodeURIComponent(a.metadata.workflow_id)}`) }, a.metadata.workflow_id)],
                  [t("Task"), h("code", null, a.metadata.task_id)],
                  [t("Role"), a.metadata.role],
                  [t("Parent task"), a.metadata.parent_task_id ? h("code", null, a.metadata.parent_task_id) : null],
                ])
              : h("p", { class: "muted" }, t("Not bound to a workflow.")),
          }),
          card({
            title: t("Compute & resources"),
            iconName: "cpu",
            body: kv([
              [t("CPU (request – limit)"), a.compute ? fmtRange(a.compute.cpu) : null],
              [t("Memory (request – limit)"), a.compute ? fmtRange(a.compute.memory_mib, fmtMemory) : null],
              [t("Secrets"), a.resources?.secrets?.length ? a.resources.secrets.map((s) => badge(s, { mono: true })) : null],
              [t("MCP servers"), a.resources?.mcp?.length ? a.resources.mcp.map((s) => badge(s, { mono: true })) : null],
            ]),
          }),
        ),
      ),
      h("div", { style: "margin-top:16px" }, card({ title: t("Raw record"), subtitle: `GET /v1/agents/${a.id}`, iconName: "braces", body: jsonView(a, { testid: "agent-json" }) })),
    );
  }

  // ---------------------------------------------------------- body
  function renderBody() {
    if (state.error && !state.agent) {
      mount(
        bodyEl,
        state.error.status === 404
          ? emptyState({ iconName: "search", title: t("Agent not found"), body: t("It may belong to another API key, or the id is wrong."), actions: [button(embedded ? t("Back to tasks") : t("Back to agents"), { onClick: () => navigate(embedded ? "/tasks" : "/agents") })] })
          : errorBanner(state.error, { retry: () => void start() }),
      );
      return;
    }
    if (!state.agent) {
      mount(bodyEl, h("div", { class: "card" }, h("div", { class: "card-body" }, skeleton(6))));
      return;
    }
    if (tab === "conversation") {
      mount(bodyEl, h("div", { class: "agent-layout" }, h("div", null, conversationEl, composerEl), asideEl));
      syncConversation();
    } else if (tab === "changes" && embedded) {
      if (!changesView) changesView = renderTaskChanges({ taskId, getTask });
      mount(bodyEl, changesView.el);
      void changesView.refresh();
    } else if (tab === "workspace") {
      mount(bodyEl, renderWorkspaceTab({ agentId, getAgent: () => state.agent, onChanged: () => void refresh() }).el);
    } else if (tab === "artifacts") {
      const holder = h("div", null, skeleton(3));
      mount(
        bodyEl,
        card({
          title: t("Artifacts from this agent"),
          subtitle: t("Durable snapshots of the workspace. They outlive the sandbox."),
          iconName: "package",
          actions: state.workspace && live() ? button(t("Snapshot now"), { size: "sm", iconName: "camera", onClick: () => snapshotDialog(agentId, state.runs, () => void loadArtifacts()) }) : null,
          body: holder,
        }),
      );
      const loadArtifacts = async () => {
        try {
          const res = await api.listArtifacts({ agent_id: agentId });
          mount(
            holder,
            (res.artifacts || []).length
              ? artifactTable(res.artifacts, { hideProducer: true })
              : emptyState({
                  iconName: "package",
                  title: t("No artifacts yet"),
                  body: state.workspace ? t("Snapshot the workspace to keep the patch, bundle and files after the sandbox is gone.") : t("Artifacts need a repository workspace."),
                  compact: true,
                }),
          );
        } catch (err) {
          mount(holder, errorBanner(err, { retry: loadArtifacts }));
        }
      };
      void loadArtifacts();
    } else {
      renderDetails();
    }
  }

  async function start() {
    try {
      await loadAgent();
    } catch (err) {
      state.error = err;
    }
    if (disposed) return;
    renderHeader();
    renderBody();
    await Promise.all([loadUsage(), loadWorkspace()]);
    if (disposed) return;
    renderHeader();
    if (tab === "conversation") renderAside();
    // An agent that was already terminal on arrival can never change —
    // don't let the refresh poll tick even once.
    if (state.agent && isAgentEnded(state.agent.status)) poll.stop();
  }

  renderHeader();
  renderBody();
  void start();

  // Status/runs refresh only; usage is loaded at start and on
  // turn_finished / stream give-up — an ended agent cannot change, so
  // the poll stops outright instead of ticking forever (SOR-202).
  const poll = poller(
    async () => {
      await refresh();
      if (state.agent && isAgentEnded(state.agent.status)) poll.stop();
    },
    () => (state.agent && (state.agent.status === "creating" || activeRun()) ? 2500 : 10000),
  );

  return {
    el,
    title: t("Agent"),
    dispose() {
      disposed = true;
      poll.stop();
      for (const id of [...streams.keys()]) closeStream(id);
      for (const block of blocks.values()) block.destroy();
    },
  };
}
