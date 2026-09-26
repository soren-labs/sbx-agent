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

function changesTabLink(taskId) {
  return `/tasks/${encodeURIComponent(taskId)}`;
}

/** The answer to "what happened and what do I do next" — one card. */
function resultCard(task, revisions, { onPublish, onRetry }) {
  const st = task.status;
  const rev = revisions?.length ? revisions[revisions.length - 1] : null;
  const delivery = rev?.delivery || {};
  const pr = delivery.pull_request || {};
  const prUrl = pr.url || pr.html_url;
  let tone = "info";
  let title = "";
  let body = null;
  const actions = [];
  const toChanges = (label, opts = {}) =>
    button(label, {
      variant: "secondary",
      size: "sm",
      iconName: "fileDiff",
      ...opts,
      onClick: () => navigate(changesTabLink(task.id), { tab: "changes" }),
    });

  if (st === "queued" || st === "awaiting_dispatch") {
    title = t("Queued — a sandbox is being allocated");
    body = h("p", { class: "muted" }, t("The run starts as soon as a provider account has a free slot."));
  } else if (st === "running") {
    title = t("Working on it");
    body = h("p", { class: "muted" }, t("Watch the live run in the conversation below."));
  } else if (st === "delivering") {
    if (task.delivery?.status === "pending") {
      tone = "warning";
      title = t("Ready to publish");
      body = h("p", { class: "muted" }, t("The run finished — publish to deliver the result."));
      actions.push(actionButton(t("Publish now"), onPublish, { variant: "primary", size: "sm", iconName: "upload", testid: "publish-now" }));
    } else {
      title = t("Publishing the result…");
    }
  } else if (st === "delivery_failed") {
    tone = "danger";
    title = t("Delivery failed");
    body = task.delivery?.error
      ? h(
          "code",
          { "data-testid": "delivery-error" },
          typeof task.delivery.error === "string"
            ? task.delivery.error
            : task.delivery.error.message || task.delivery.error.code || "",
        )
      : null;
    actions.push(actionButton(t("Publish now"), onPublish, { variant: "primary", size: "sm", iconName: "upload", testid: "publish-now" }));
  } else if (st === "error" || st === "expired") {
    tone = "danger";
    title = st === "expired" ? t("The task expired") : t("The task failed");
    body = h("p", { class: "muted" }, t("See the run's final events in the conversation for the reason."));
    actions.push(actionButton(t("Retry"), onRetry, { variant: "primary", size: "sm", iconName: "refresh", testid: "result-retry" }));
  } else if (st === "cancelled") {
    tone = "neutral";
    title = t("Cancelled");
    body = h("p", { class: "muted" }, t("Nothing else will run — send a follow-up below to continue the work."));
  } else if (rev && rev.status !== "ready") {
    tone = "danger";
    title = t("Changes could not be packaged");
    body = rev.error?.message ? h("code", null, rev.error.message) : null;
    actions.push(actionButton(t("Retry"), onRetry, { variant: "primary", size: "sm", iconName: "refresh", testid: "result-retry" }));
  } else if (rev) {
    if (delivery.merged) {
      tone = "success";
      title = t("Merged");
      body = h("p", { class: "muted" }, pr.number ? t("Pull request #{n} merged.", { n: pr.number }) : t("The changes are merged."));
    } else if (pr.number) {
      tone = "success";
      title = t("Pull request #{n} is {state}", { n: pr.number, state: pr.state || t("open") });
      actions.push(toChanges(t("Review changes")));
      if (prUrl) actions.push(h("a", { class: "btn btn-sm btn-secondary", href: prUrl, target: "_blank", rel: "noopener" }, icon("external", { size: 13 }), t("View PR")));
    } else if (delivery.status === "delivered") {
      tone = "success";
      title = t("Delivered to {branch}", { branch: delivery.branch || t("the work branch") });
      actions.push(toChanges(t("Review changes")));
    } else if (delivery.status === "failed") {
      tone = "danger";
      title = t("Delivery failed");
      body = delivery.error
        ? h("code", null, typeof delivery.error === "string" ? delivery.error : delivery.error.message || delivery.error.code || "")
        : null;
      actions.push(actionButton(t("Publish now"), onPublish, { variant: "primary", size: "sm", iconName: "upload", testid: "publish-now" }));
    } else {
      title = t("Revision {n} is ready to review", { n: rev.n });
      body = h("p", { class: "muted" }, t("Check the diff, then publish or open a pull request."));
      actions.push(toChanges(t("Review changes"), { variant: "primary" }));
      if (isTaskEnded(st)) actions.push(actionButton(t("Publish"), onPublish, { variant: "secondary", size: "sm", iconName: "upload", testid: "publish-now" }));
    }
  } else if (task.resolved?.source?.repo || task.request?.source?.repo) {
    title = t("Finished — no code changes were recorded");
    body = h("p", { class: "muted" }, t("The run ended without leaving a revision."));
  } else {
    title = t("Finished");
    body = h("p", { class: "muted" }, t("The result is the final reply in the conversation below."));
  }
  if (!title) return null;
  return card({
    title: t("Result"),
    iconName: "circleCheck",
    testid: "task-result",
    class: `result-card tone-${tone}`,
    body: h(
      "div",
      { class: "fields" },
      h("div", { class: "result-title" }, title),
      body,
      actions.length ? h("div", { class: "row", style: "gap:8px;flex-wrap:wrap" }, actions) : null,
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
  const state = { task: null, revisions: [], reviews: [], error: null };
  let agentView = null;
  let disposed = false;

  const headerEl = h("div");
  const goalEl = h("div");
  const resultEl = h("div");
  const agentEl = h("div");
  const el = h("div", { class: "page page-wide", "data-testid": "task-view" }, headerEl, goalEl, resultEl, agentEl);

  async function refresh() {
    try {
      const res = await api.getTask(taskId);
      state.task = res.task;
      state.error = null;
      if (state.task.agent_id && !["queued", "awaiting_dispatch"].includes(state.task.status)) {
        const [revs, rvws] = await Promise.all([
          api.listTaskRevisions(taskId),
          api.listTaskReviews(taskId),
        ]);
        state.revisions = revs.revisions || [];
        state.reviews = rvws.reviews || [];
      }
    } catch (err) {
      state.error = err;
    }
    if (disposed) return;
    renderHeader();
    renderExtra();
  }

  async function publishNow() {
    try {
      await api.deliverRevision(taskId, { revision: "latest" });
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
      actions.push(
        actionButton(task.status === "delivery_failed" ? t("Retry publish") : t("Retry"), retry, {
          variant: "secondary",
          iconName: "refresh",
          testid: "retry-task",
        }),
      );
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
    mount(resultEl, resultCard(task, state.revisions, { onPublish: publishNow, onRetry: retry }));
    if (task.agent_id && !agentView) {
      agentView = renderAgent({
        route,
        shell,
        agentId: task.agent_id,
        taskId: task.id,
        getTask: () => state.task,
        extraDetails: taskDetails(task),
      });
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
