/** Agent → Workspace tab: repo state, git pipeline and review/publish/merge. */
import { api } from "../lib/api.js";
import { h, mount } from "../lib/dom.js";
import { isAgentLive } from "../lib/domain.js";
import { fmtDateTime, fmtRelative, shortSha } from "../lib/format.js";
import { t } from "../lib/i18n.js";
import { icon } from "../lib/icons.js";
import { href } from "../lib/router.js";
import {
  actionButton,
  badge,
  banner,
  button,
  card,
  emptyState,
  errorBanner,
  field,
  kv,
  mono,
  openDialog,
  segmented,
  skeleton,
  toast,
  toastError,
} from "../lib/ui.js";

function step(label, value, { done, warn, iconName }) {
  return h(
    "div",
    { class: ["pipe-step", done && "is-done", warn && "is-warn"] },
    h("span", { class: "pipe-dot" }, icon(done ? "check" : iconName, { size: 14 })),
    h("span", { class: "pipe-label" }, label),
    h("span", { class: "pipe-sub" }, value || "—"),
  );
}

export function renderWorkspaceTab({ agentId, getAgent, onChanged }) {
  const el = h("div", { class: "stack", "data-testid": "workspace-tab" }, h("div", { class: "card" }, h("div", { class: "card-body" }, skeleton(5))));

  async function load() {
    let ws;
    try {
      ws = (await api.workspace(agentId)).workspace;
    } catch (err) {
      if (err.status === 404) {
        mount(
          el,
          emptyState({
            iconName: "branch",
            title: t("No repository workspace"),
            body: t("This agent was created without a repository. Declare `workspace` (repo, base ref, exact base sha) when creating an agent to get checkouts, artifacts, git publishing and review-gated merges."),
            actions: [h("a", { class: "btn btn-primary", href: "#/agents/new" }, icon("plus"), t("New agent with a repository"))],
            testid: "workspace-empty",
          }),
        );
        return;
      }
      mount(el, errorBanner(err, { retry: load }));
      return;
    }
    render(ws);
  }

  function render(ws) {
    const agent = getAgent();
    const liveIdle = agent && isAgentLive(agent.status) && agent.status === "idle";
    const git = ws.git || {};
    const headMoved = ws.head_sha && ws.checkout_sha && ws.head_sha !== ws.checkout_sha;
    const reviewedCurrent = ws.reviewed_head_sha && ws.reviewed_head_sha === ws.head_sha;
    const reviewedStale = ws.reviewed_head_sha && ws.reviewed_head_sha !== ws.head_sha;
    const pushedCurrent = ws.pushed_head_sha && ws.pushed_head_sha === ws.head_sha;
    const pr = ws.pull_request;
    const merged = ws.merge?.merged;
    const needLive = liveIdle ? null : t("Needs an idle agent with a live sandbox.");

    const actions = [];
    if (git.push) {
      actions.push(
        actionButton(t("Publish"), async () => {
          try {
            const res = await api.publish(agentId);
            toast(t("Published {branch}", { branch: res.workspace.branch || "" }), { tone: "success" });
            render(res.workspace);
            onChanged?.();
          } catch (err) {
            toastError(err, t("Publish failed"));
          }
        }, { iconName: "upload", disabled: !liveIdle, title: needLive, testid: "ws-publish" }),
      );
    }
    actions.push(button(t("Pin review"), { iconName: "shield", disabled: !ws.head_sha, testid: "ws-review", onClick: () => reviewDialog(ws) }));
    if (git.merge) {
      actions.push(
        actionButton(t("Merge"), async () => {
          try {
            const res = await api.merge(agentId);
            toast(t("Pull request merged"), { tone: "success" });
            render(res.workspace);
          } catch (err) {
            toastError(err, t("Merge refused"));
          }
        }, {
          variant: "primary",
          iconName: "merge",
          disabled: !liveIdle || !pr || !reviewedCurrent || merged,
          title: !reviewedCurrent ? t("Pin a review on the current head first.") : needLive,
          testid: "ws-merge",
        }),
      );
    }
    actions.push(button(t("Apply handoff"), { variant: "ghost", iconName: "handoff", disabled: !liveIdle, title: needLive, onClick: () => handoffDialog(agentId, load) }));

    mount(
      el,
      ws.publish_error ? banner({ tone: "danger", title: t("Last publish failed"), body: h("code", null, ws.publish_error), testid: "publish-error" }) : null,
      card({
        title: t("Change pipeline"),
        subtitle: t("From the pinned base to a reviewed, published and merged head."),
        iconName: "branch",
        actions,
        body: h(
          "div",
          { class: "pipeline", "data-testid": "ws-pipeline" },
          step(t("Base"), `${ws.base_ref} @ ${shortSha(ws.base_sha)}`, { done: Boolean(ws.checkout_sha), iconName: "commit" }),
          step(t("Head"), headMoved ? `${shortSha(ws.head_sha)} · ${t("moved")}` : shortSha(ws.head_sha), { done: Boolean(headMoved), iconName: "commit" }),
          step(t("Reviewed"), ws.reviewed_head_sha ? `${shortSha(ws.reviewed_head_sha)}${reviewedStale ? ` · ${t("stale")}` : ""}` : t("not pinned"), { done: reviewedCurrent, warn: reviewedStale, iconName: "shield" }),
          step(t("Published"), ws.pushed_head_sha ? `${ws.branch || ""} @ ${shortSha(ws.pushed_head_sha)}` : git.push ? t("not pushed") : t("push disabled"), { done: pushedCurrent, warn: ws.pushed_head_sha && !pushedCurrent, iconName: "upload" }),
          step(t("Merged"), merged ? shortSha(ws.merge.merge_commit_sha) : pr ? `#${pr.number} ${pr.state || ""}` : git.merge ? t("no PR yet") : t("merge disabled"), { done: merged, iconName: "merge" }),
        ),
      }),
      h(
        "div",
        { class: "grid-2" },
        card({
          title: t("Repository"),
          iconName: "server",
          body: kv(
            [
              [t("Repository"), h("code", { style: "overflow-wrap:anywhere" }, ws.repo)],
              [t("Workdir"), h("code", null, ws.workdir || "repo")],
              [t("Base"), h("span", { class: "row", style: "gap:6px" }, h("code", null, ws.base_ref), mono(ws.base_sha, { short: 12 }))],
              [t("Checkout"), mono(ws.checkout_sha, { short: 12 })],
              [t("Head"), h("span", { class: "row", style: "gap:6px" }, mono(ws.head_sha, { short: 12 }), headMoved ? badge(t("moved"), { tone: "blue" }) : null)],
              [t("Reviewed head"), ws.reviewed_head_sha ? h("span", { class: "row", style: "gap:6px" }, mono(ws.reviewed_head_sha, { short: 12 }), reviewedCurrent ? badge(t("current"), { tone: "green" }) : badge(t("stale"), { tone: "amber" })) : null],
              [t("Updated"), h("span", { title: fmtDateTime(ws.updated_at) }, fmtRelative(ws.updated_at))],
            ],
            { testid: "ws-record" },
          ),
        }),
        card({
          title: t("Git policy"),
          iconName: "pullRequest",
          body: ws.git
            ? h(
                "div",
                { class: "stack", style: "gap:14px" },
                h(
                  "div",
                  { class: "policy-list" },
                  [
                    ["push", t("push")],
                    ["auto_publish", t("auto publish")],
                    ["auto_create_pr", t("open PR")],
                    ["merge", t("review-gated merge")],
                    ["draft", t("draft PR")],
                  ].map(([k, label]) => badge(label, { tone: git[k] ? "green" : "neutral", title: git[k] ? t("enabled") : t("disabled") })),
                ),
                kv([
                  [t("Work branch"), ws.branch ? h("code", null, ws.branch) : null],
                  [t("PR target"), git.target ? h("code", null, git.target) : null],
                  [t("Pushed head"), ws.pushed_head_sha ? mono(ws.pushed_head_sha, { short: 12 }) : null],
                  [
                    t("Pull request"),
                    pr
                      ? h(
                          "span",
                          { class: "row", style: "gap:6px" },
                          pr.url ? h("a", { href: pr.url, target: "_blank", rel: "noopener noreferrer" }, `#${pr.number}`) : `#${pr.number}`,
                          pr.state ? badge(pr.state, { tone: pr.state === "open" ? "green" : "neutral" }) : null,
                          pr.draft ? badge(t("draft")) : null,
                        )
                      : null,
                  ],
                  [t("Merge commit"), merged ? mono(ws.merge.merge_commit_sha, { short: 12 }) : null],
                ]),
              )
            : h("p", { class: "muted" }, t("No git policy was declared. Add `git` at create time to push a work branch and open a pull request.")),
        }),
      ),
      h("p", { class: "subtle", style: "font-size:12.5px" }, t("Reviews are pinned to an exact sha: if the head moves after a review, merging is refused until it is reviewed again.")),
    );

    function reviewDialog(current) {
      const f = { head: current.head_sha || "", comment: "" };
      const { close } = openDialog({
        title: t("Pin a review"),
        description: t("Record the exact commit you reviewed. Merging requires this pin to match the head."),
        testid: "review-dialog",
        body: h(
          "div",
          { class: "fields" },
          field(t("Reviewed head sha"), h("input", { class: "input mono", value: f.head, "data-testid": "review-sha", onInput: (e) => (f.head = e.target.value.trim()) }), { hint: t("Defaults to the recorded head. A different sha is refused (head_sha_mismatch).") }),
          field(t("Comment on the pull request (optional)"), h("textarea", { class: "textarea", rows: 3, onInput: (e) => (f.comment = e.target.value) }), { hint: t("Posted as a PR comment — never as a GitHub approval. Needs a live sandbox and the GitHub bridge.") }),
        ),
        footer: [
          button(t("Cancel"), { onClick: () => close() }),
          actionButton(t("Pin review"), async () => {
            const body = {};
            if (f.head && f.head !== current.head_sha) body.head_sha = f.head;
            if (f.comment.trim()) body.comment = f.comment.trim();
            try {
              const res = await api.review(agentId, body);
              toast(t("Review pinned at {sha}", { sha: shortSha(res.workspace.reviewed_head_sha) }), { tone: "success" });
              close();
              render(res.workspace);
            } catch (err) {
              toastError(err, t("Review not recorded"));
            }
          }, { variant: "primary", testid: "review-submit" }),
        ],
      });
    }
  }

  void load();
  return { el, reload: load };
}

function handoffDialog(agentId, onDone) {
  const f = { kind: "artifact", artifact: "", head: "", ref: "", sha: "" };
  const body = h("div", { class: "fields" });
  const render = () =>
    mount(
      body,
      field(
        t("Hand off from"),
        segmented(
          [
            { value: "artifact", label: t("Artifact") },
            { value: "head", label: t("Commit") },
            { value: "pr", label: t("Pull request") },
          ],
          f.kind,
          (v) => {
            f.kind = v;
            render();
          },
        ),
      ),
      f.kind === "artifact"
        ? field(t("Artifact id"), h("input", { class: "input mono", value: f.artifact, placeholder: "art-…", onInput: (e) => (f.artifact = e.target.value) }))
        : f.kind === "head"
          ? field(t("Commit sha"), h("input", { class: "input mono", value: f.head, onInput: (e) => (f.head = e.target.value) }))
          : h(
              "div",
              { class: "fields-2" },
              field(t("Ref"), h("input", { class: "input mono", value: f.ref, placeholder: "refs/pull/42/head", onInput: (e) => (f.ref = e.target.value) })),
              field(t("Pinned head sha"), h("input", { class: "input mono", value: f.sha, onInput: (e) => (f.sha = e.target.value) })),
            ),
      h("p", { class: "field-hint" }, t("The agent must be idle with a live sandbox. Integrity and base checks fail closed.")),
    );
  render();
  const { close } = openDialog({
    title: t("Apply a handoff"),
    description: t("Bring another agent's work into this workspace."),
    body,
    testid: "handoff-dialog",
    footer: [
      button(t("Cancel"), { onClick: () => close() }),
      actionButton(t("Apply"), async () => {
        const payload =
          f.kind === "artifact" ? { artifact_id: f.artifact.trim() } : f.kind === "head" ? { head_sha: f.head.trim() } : { pull_request: { ref: f.ref.trim(), head_sha: f.sha.trim() } };
        try {
          await api.handoff(agentId, payload);
          toast(t("Handoff applied"), { tone: "success" });
          close();
          onDone?.();
        } catch (err) {
          toastError(err, t("Handoff failed"));
        }
      }, { variant: "primary" }),
    ],
  });
}
