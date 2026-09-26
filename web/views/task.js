import { api } from "../lib/api.js";
import { h, mount } from "../lib/dom.js";
import { isTaskEnded, isTaskLive, repoName, taskTitle } from "../lib/domain.js";
import { fmtDateTime, fmtRelative } from "../lib/format.js";
import { t } from "../lib/i18n.js";
import { icon } from "../lib/icons.js";
import { href, navigate } from "../lib/router.js";
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
  skeleton,
  statusBadge,
  toast,
  toastError,
} from "../lib/ui.js";
import { renderAgent } from "./agent.js";

const DELIVERY_BADGE = { pending: "amber", delivered: "green", failed: "red" };

function deliveryCard(task, { onPublish }) {
  const d = task.delivery;
  if (!d) return null;
  const pr = d.pull_request || {};
  const prUrl = pr.url || pr.html_url;
  return card({
    title: t("Delivery"),
    iconName: "pullRequest",
    testid: "delivery-card",
    actions: [
      prUrl ? h("a", { class: "btn btn-sm", href: prUrl, target: "_blank", rel: "noopener" }, icon("externalLink", { size: 13 }), t("View PR")) : null,
      d.status !== "delivered" && isTaskEnded(task.status) && task.agent_id
        ? actionButton(t("Publish now"), onPublish, { variant: "secondary", size: "sm", iconName: "upload", testid: "publish-now" })
        : null,
    ],
    body: h(
      "div",
      { class: "fields" },
      h(
        "div",
        { class: "row", style: "gap:8px;align-items:center" },
        badge(t(d.status), { tone: DELIVERY_BADGE[d.status] || "neutral", testid: "delivery-status" }),
        d.branch ? h("code", null, d.branch) : null,
        d.pushed_head_sha ? mono(d.pushed_head_sha.slice(0, 8)) : null,
        pr.number ? badge(`PR #${pr.number}`, { mono: true }) : null,
      ),
      d.error ? banner({ tone: "danger", title: t("Delivery failed"), body: h("code", null, String(d.error)), testid: "delivery-error" }) : null,
      d.status === "pending" && isTaskLive(task.status) ? h("p", { class: "muted" }, t("Publishes automatically when the run finishes.")) : null,
    ),
  });
}

function goalCard(task) {
  const text = task.prompt?.text || (task.request?.prompt || {}).text || "";
  const pre = h("pre", { class: "task-goal-text" }, text);
  const wrap = h("div", { class: "task-goal is-clamped" }, pre);
  const toggleBtn = button(t("Expand"), {
    variant: "ghost",
    size: "xs",
    testid: "goal-expand",
    onClick: () => {
      const open = wrap.classList.toggle("is-clamped");
      toggleBtn.textContent = open ? t("Expand") : t("Collapse");
    },
  });
  // Only offer the toggle when the text actually overflows the clamp.
  queueMicrotask(() => {
    if (pre.scrollHeight <= pre.clientHeight + 2) toggleBtn.style.display = "none";
  });
  return card({
    title: t("Goal"),
    iconName: "message",
    actions: toggleBtn,
    body: wrap,
    testid: "task-goal",
  });
}

function taskDetails(task) {
  const req = task.request || {};
  const src = req.source || {};
  const rs = task.resolved?.source;
  const del = req.delivery || {};
  const meta = req.metadata;
  const transitions = task.transitions || [];
  return h(
    "div",
    { style: "margin-bottom:16px", class: "stack" },
    card({
      title: t("Task"),
      iconName: "listTodo",
      testid: "task-card",
      body: kv([
        [t("Id"), mono(task.id)],
        [t("Name"), req.name],
        [t("Status"), statusBadge("task", task.status)],
        [t("Repository"), rs?.repo || src.repo ? h("code", null, rs?.repo || src.repo) : null],
        [t("Starting point"), rs ? h("code", null, `${rs.base_ref || ""} @ ${String(rs.base_sha || "").slice(0, 8)}`) : src.ref ? h("code", null, src.ref) : null],
        [t("Delivery"), del.pull_request ? t("Pull request") : Object.keys(del).length ? t("Branch push") : null],
        [t("Workflow"), meta?.workflow_id ? h("a", { href: href(`/workflows/${encodeURIComponent(meta.workflow_id)}`) }, meta.workflow_id) : null],
        [t("Created"), fmtDateTime(task.created_at)],
        [t("Updated"), fmtDateTime(task.updated_at)],
      ]),
    }),
    transitions.length
      ? card({
          title: t("History"),
          iconName: "clock",
          body: h(
            "ul",
            { class: "transition-list", "data-testid": "task-transitions" },
            transitions.map((tr) =>
              h(
                "li",
                null,
                statusBadge("task", tr.status),
                h("span", { class: "muted" }, tr.reason ? ` ${tr.reason}` : ""),
                h("span", { class: "subtle" }, ` · ${fmtRelative(tr.at)}`),
              ),
            ),
          ),
        })
      : null,
    card({ title: t("Raw task record"), subtitle: `GET /v1/tasks/${task.id}`, iconName: "braces", body: jsonView(task, { testid: "task-json" }) }),
  );
}

export function renderTask({ route, shell }) {
  const taskId = route.params.id;
  const state = { task: null, error: null };
  let agentView = null;
  let disposed = false;

  const headerEl = h("div");
  const goalEl = h("div");
  const deliveryEl = h("div");
  const agentEl = h("div");
  const el = h("div", { class: "page page-wide", "data-testid": "task-view" }, headerEl, goalEl, deliveryEl, agentEl);

  async function refresh() {
    try {
      const res = await api.getTask(taskId);
      state.task = res.task;
      state.error = null;
    } catch (err) {
      state.error = err;
    }
    if (disposed) return;
    renderHeader();
    renderExtra();
  }

  async function publishNow() {
    try {
      await api.deliverTask(taskId);
      toast(t("Publishing…"), { tone: "info" });
    } catch (err) {
      toastError(err, t("Could not publish"));
    }
    await refresh();
  }

  async function retry() {
    try {
      await api.retryTask(taskId, {});
      toast(t("Retry queued"), { tone: "success" });
    } catch (err) {
      toastError(err, t("Could not retry"));
    }
    await refresh();
  }

  async function cancel() {
    const ok = await confirmDialog({
      title: t("Cancel this task?"),
      body: t("The running work stops; anything already published stays published."),
      confirmLabel: t("Cancel task"),
    });
    if (!ok) return;
    try {
      await api.cancelTask(taskId);
      toast(t("Task cancelled"), { tone: "success" });
    } catch (err) {
      toastError(err, t("Could not cancel"));
    }
    await refresh();
  }

  function renderHeader() {
    const task = state.task;
    if (state.error && !task) {
      mount(
        headerEl,
        pageHeader({ title: taskId, back: { href: "#/tasks", label: t("Tasks") } }),
        state.error.status === 404
          ? emptyState({ iconName: "search", title: t("Task not found"), body: t("It may belong to another API key, or the id is wrong."), actions: [button(t("Back to tasks"), { onClick: () => navigate("/tasks") })], testid: "task-404" })
          : errorBanner(state.error, { retry: () => void refresh() }),
      );
      return;
    }
    if (!task) {
      mount(headerEl, pageHeader({ title: taskId, back: { href: "#/tasks", label: t("Tasks") } }), h("div", { class: "card" }, h("div", { class: "card-body" }, skeleton(4))));
      return;
    }
    const ex = task.resolved?.execution || {};
    const src = task.resolved?.source || task.request?.source;
    const actions = [];
    if (isTaskLive(task.status)) {
      actions.push(actionButton(t("Cancel"), cancel, { variant: "danger", iconName: "stop", testid: "cancel-task" }));
    } else if (task.status !== "cancelled") {
      actions.push(actionButton(t("Retry"), retry, { variant: "secondary", iconName: "refresh", testid: "retry-task" }));
    }
    mount(
      headerEl,
      pageHeader({
        title: taskTitle(task),
        testid: "task-title",
        back: { href: "#/tasks", label: t("Tasks") },
        actions,
        meta: [
          statusBadge("task", task.status, { testid: "task-status" }),
          ex.provider ? h("span", null, providerTag(ex.provider), h("span", { class: "mono" }, ex.model || "")) : null,
          ex.reasoning_effort ? badge(`${t("effort")} ${ex.reasoning_effort}`) : null,
          src?.repo ? h("span", { title: src.repo }, icon("github", { size: 13 }), repoName(src.repo)) : null,
          mono(task.id, { testid: "task-id" }),
          h("span", { title: fmtDateTime(task.created_at) }, icon("clock", { size: 13 }), `${t("created")} ${fmtRelative(task.created_at)}`),
        ],
      }),
    );
  }

  function renderExtra() {
    const task = state.task;
    if (!task) return;
    mount(goalEl, goalCard(task));
    mount(deliveryEl, deliveryCard(task, { onPublish: publishNow }));
    if (task.agent_id && !agentView) {
      agentView = renderAgent({ route, shell, agentId: task.agent_id, taskId: task.id, extraDetails: taskDetails(task) });
      mount(agentEl, agentView.el);
    }
    if (!task.agent_id) {
      mount(
        agentEl,
        banner({
          tone: "neutral",
          title: t("This task never got an agent"),
          body: t("Resolution failed before a sandbox was allocated — see the task details for the reason."),
          testid: "no-agent",
        }),
        taskDetails(task),
      );
    }
  }

  mount(headerEl, pageHeader({ title: taskId, back: { href: "#/tasks", label: t("Tasks") } }), skeleton(4));
  void refresh();

  const poll = poller(() => void refresh(), () => (state.task && isTaskEnded(state.task.status) ? 30000 : 5000));

  return {
    el,
    title: t("Task"),
    dispose() {
      disposed = true;
      poll.stop();
      agentView?.dispose?.();
    },
  };
}
