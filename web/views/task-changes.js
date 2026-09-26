/** Task → Changes tab: revisions, diff, delivery, reviews and merge readiness. */
import { api } from "../lib/api.js";
import { h, mount } from "../lib/dom.js";
import { isTaskEnded, isTaskLive } from "../lib/domain.js";
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
  confirmDialog,
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

const DIFF_LINE_CAP = 400;

const DELIVERY_TONE = { delivered: "green", failed: "red", pending: "amber" };
const VERDICT_META = {
  approve: { tone: "green", label: "Approved", iconName: "check" },
  request_changes: { tone: "amber", label: "Changes requested", iconName: "warning" },
  comment: { tone: "neutral", label: "Comment", iconName: "message" },
};

/** `diff --git` text → [{path, lines, adds, dels, binary}]. */
function parseDiff(text) {
  const files = [];
  let cur = null;
  for (const line of String(text || "").split("\n")) {
    const head = line.match(/^diff --git a\/(.+?) b\/(.+)$/);
    if (head) {
      cur = { path: head[2], lines: [], adds: 0, dels: 0, binary: false };
      files.push(cur);
      continue;
    }
    if (!cur) continue;
    if (/^(index|new file|deleted file|old mode|new mode|similarity|rename|copy) /.test(line)) continue;
    if (line.startsWith("Binary files")) {
      cur.binary = true;
      continue;
    }
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    if (line.startsWith("+")) cur.adds += 1;
    if (line.startsWith("-")) cur.dels += 1;
    cur.lines.push(line);
  }
  return files;
}

function diffLineEl(line) {
  let cls = "diff-line";
  if (line.startsWith("@@")) cls += " is-hunk";
  else if (line.startsWith("+")) cls += " is-add";
  else if (line.startsWith("-")) cls += " is-del";
  return h("span", { class: cls }, line, "\n");
}

function diffBlock(files, artifactId) {
  const totalAdds = files.reduce((n, f) => n + f.adds, 0);
  const totalDels = files.reduce((n, f) => n + f.dels, 0);
  const shown = [];
  let count = 0;
  let truncated = false;
  for (const f of files) {
    const room = DIFF_LINE_CAP - count;
    const lines = f.lines.slice(0, Math.max(room, 0));
    if (lines.length < f.lines.length) truncated = true;
    count += lines.length;
    shown.push({ ...f, lines });
    if (count >= DIFF_LINE_CAP) {
      truncated = truncated || files.indexOf(f) < files.length - 1;
      break;
    }
  }
  return h(
    "div",
    { class: "fields", "data-testid": "diff-block" },
    h(
      "div",
      { class: "row", style: "gap:8px;align-items:center;flex-wrap:wrap" },
      badge(t("{n} files", { n: files.length }), { testid: "diff-files" }),
      totalAdds ? badge(`+${totalAdds}`, { tone: "green" }) : null,
      totalDels ? badge(`−${totalDels}`, { tone: "red" }) : null,
      h("span", { style: "flex:1" }),
      artifactId
        ? h(
            "a",
            { class: "btn btn-ghost btn-xs", href: href(`/artifacts/${encodeURIComponent(artifactId)}`) },
            icon("package", { size: 13 }),
            t("Artifact"),
          )
        : null,
    ),
    shown.map((f) =>
      h(
        "div",
        { class: "diff-file" },
        h(
          "div",
          { class: "diff-file-head" },
          icon("fileDiff", { size: 14 }),
          h("code", null, f.path),
          f.binary ? badge(t("binary"), { tone: "neutral" }) : null,
          f.adds ? h("span", { class: "diff-stat is-add" }, `+${f.adds}`) : null,
          f.dels ? h("span", { class: "diff-stat is-del" }, `−${f.dels}`) : null,
        ),
        f.binary
          ? h("p", { class: "muted" }, t("Binary file — no text diff."))
          : h("pre", { class: "diff" }, f.lines.map(diffLineEl)),
      ),
    ),
    truncated
      ? h(
          "p",
          { class: "muted" },
          t("Diff truncated for display — open the artifact for the full patch."),
        )
      : null,
  );
}

function step(label, value, { done, warn, iconName }) {
  return h(
    "div",
    { class: ["pipe-step", done && "is-done", warn && "is-warn"] },
    h("span", { class: "pipe-dot" }, icon(done ? "check" : iconName, { size: 14 })),
    h("span", { class: "pipe-label" }, label),
    h("span", { class: "pipe-sub" }, value || "—"),
  );
}

/** Latest review per revision: the merge gate reads it, so the UI does too. */
function currentReview(reviews, rev) {
  const own = reviews.filter((r) => r.revision_id === rev.id);
  if (!own.length) return null;
  return own.filter((r) => !r.stale).pop() || null;
}

function mergeChecklist(rev, reviews) {
  const delivery = rev.delivery || {};
  const pr = delivery.pull_request || {};
  const approved = reviews.some(
    (r) =>
      r.revision_id === rev.id &&
      !r.stale &&
      r.independent &&
      r.verdict === "approve" &&
      r.reviewed_head_sha === rev.head_sha,
  );
  const staleApprove = reviews.some(
    (r) => r.revision_id !== rev.id && r.stale && r.independent && r.verdict === "approve",
  );
  return [
    { ok: rev.status === "ready", label: t("Revision materialized") },
    { ok: delivery.status === "delivered", label: t("Changes delivered") },
    {
      ok: typeof pr.number === "number" && (pr.state === "open" || Boolean(delivery.merged)),
      label: t("Pull request open"),
      hint: typeof pr.number === "number" ? `#${pr.number} ${pr.state || "open"}` : t("none created"),
    },
    {
      ok: approved,
      label: t("Independent approval on this head"),
      hint: approved
        ? null
        : staleApprove
          ? t("an earlier approval went stale with the new revision")
          : t("add a review below"),
    },
  ];
}

function reviewerName(review) {
  const id = review.reviewer?.identity || t("unknown");
  return id.startsWith("key:") ? t("API key") : id;
}

export function renderTaskChanges({ taskId, getTask }) {
  const state = {
    revisions: null,
    reviews: [],
    error: null,
    open: null, // expanded revision id — defaults to latest once loaded
    diffs: new Map(), // revision id → {status:"loading"|"ok"|"missing"|"error", files?}
  };
  const el = h("div", { class: "stack", "data-testid": "changes-tab" });

  const latest = () =>
    state.revisions?.length
      ? state.revisions.reduce((a, b) => (a.n > b.n ? a : b))
      : null;
  const reviewsFor = (rev) => state.reviews.filter((r) => r.revision_id === rev.id);

  async function refresh() {
    try {
      const [revs, rvws] = await Promise.all([
        api.listTaskRevisions(taskId),
        api.listTaskReviews(taskId),
      ]);
      state.revisions = revs.revisions || [];
      state.reviews = rvws.reviews || [];
      state.error = null;
      if (state.revisions.length) {
        const ids = new Set(state.revisions.map((r) => r.id));
        if (!state.open || !ids.has(state.open)) state.open = latest().id;
      }
      for (const rev of state.revisions) {
        if (rev.id === state.open) void loadDiff(rev);
      }
    } catch (err) {
      state.error = err;
    }
    render();
  }

  async function loadDiff(rev) {
    if (!rev.artifact_id || state.diffs.get(rev.id)?.status === "loading") return;
    if (state.diffs.has(rev.id) && state.diffs.get(rev.id).status !== "error") return;
    state.diffs.set(rev.id, { status: "loading" });
    try {
      const res = await api.downloadArtifact(rev.artifact_id, "patch.diff");
      const text = await res.text();
      state.diffs.set(rev.id, { status: "ok", files: parseDiff(text) });
    } catch (err) {
      state.diffs.set(rev.id, { status: err.status === 404 ? "missing" : "error" });
    }
    render();
  }

  async function publish(rev) {
    try {
      await api.deliverRevision(taskId, { revision: rev.id });
      toast(t("Publishing…"), { tone: "info" });
    } catch (err) {
      toastError(err, t("Could not publish"));
    }
    await refresh();
  }

  async function merge(rev) {
    const ok = await confirmDialog({
      title: t("Merge this pull request?"),
      body: t("The delivered pull request merges on the remote. This cannot be undone from here."),
      confirmLabel: t("Merge"),
      tone: "primary",
      testid: "merge-confirm",
    });
    if (!ok) return;
    try {
      await api.mergeTask(taskId, { revision: rev.id });
      toast(t("Merged"), { tone: "success" });
    } catch (err) {
      toastError(err, t("Could not merge"));
    }
    await refresh();
  }

  function reviewDialog(rev) {
    const task = getTask() || {};
    const wantsPR = Boolean(
      task.request?.delivery?.pull_request || task.resolved?.git?.auto_create_pr,
    );
    const hasPR = Boolean(rev.delivery?.pull_request?.number);
    const st = { verdict: "approve", comment: "", findings: "" };
    const commentField = field(
      t("Comment"),
      h("textarea", {
        class: "textarea",
        rows: 3,
        "data-testid": "review-comment",
        placeholder: t("Posted to the pull request"),
        onInput: (ev) => {
          st.comment = ev.target.value;
        },
      }),
      { hint: t("Comments land on the delivered pull request.") },
    );
    const dlg = openDialog({
      title: t("Review revision {n}", { n: rev.n }),
      testid: "review-dialog",
      body: h(
        "div",
        { class: "fields" },
        field(
          t("Verdict"),
          segmented(
            [
              { value: "approve", label: t("Approve") },
              { value: "request_changes", label: t("Request changes") },
              { value: "comment", label: t("Comment") },
            ],
            st.verdict,
            (v) => {
              st.verdict = v;
            },
            { testid: "review-verdict" },
          ),
        ),
        field(
          t("Findings"),
          h("textarea", {
            class: "textarea mono",
            rows: 3,
            "data-testid": "review-findings",
            placeholder: t("path:line — what needs attention"),
            onInput: (ev) => {
              st.findings = ev.target.value;
            },
          }),
          { hint: t("One per line, e.g. src/app.py:42 — unused import. Optional.") },
        ),
        hasPR ? commentField : wantsPR ? h("p", { class: "muted" }, t("A comment field appears once the pull request exists.")) : null,
        rev !== latest()
          ? banner({
              tone: "warning",
              title: t("Reviewing an older revision"),
              body: t("A newer revision exists — this review is recorded as stale."),
              testid: "review-old-rev",
            })
          : null,
      ),
      footer: [
        button(t("Cancel"), { variant: "ghost", onClick: () => dlg.close() }),
        actionButton(
          t("Submit review"),
          async () => {
            const findings = st.findings
              .split("\n")
              .map((line) => line.trim())
              .filter(Boolean)
              .map((line) => {
                const m = line.match(/^(\S+?)(?::(\d+))?\s*[—:-]\s+(.+)$/);
                return m
                  ? { path: m[1], line: m[2] ? Number(m[2]) : undefined, message: m[3] }
                  : { message: line };
              });
            try {
              await api.createTaskReview(taskId, {
                revision: rev.id,
                verdict: st.verdict,
                findings: findings.length ? findings : undefined,
                comment: st.comment.trim() || undefined,
              });
            } catch (err) {
              toastError(err, t("Could not record the review"));
              return;
            }
            dlg.close();
            toast(t("Review recorded"), { tone: "success" });
            await refresh();
          },
          { variant: "primary", testid: "review-submit" },
        ),
      ],
    });
  }

  function deliveryBlock(rev) {
    const task = getTask() || {};
    const d = rev.delivery || {};
    const pr = d.pull_request || {};
    const prUrl = pr.url || pr.html_url;
    const wantsPR = Boolean(
      task.request?.delivery?.pull_request || task.resolved?.git?.auto_create_pr,
    );
    if (d.merged) {
      return banner({
        tone: "success",
        title: t("Merged"),
        body: h("span", null, pr.number ? `PR #${pr.number} · ` : "", d.merge_commit_sha ? mono(d.merge_commit_sha) : ""),
        testid: "delivery-merged",
      });
    }
    const canDeliver = isTaskEnded(task.status || "") || task.status === "delivering";
    return h(
      "div",
      { class: "fields" },
      h(
        "div",
        { class: "row", style: "gap:8px;align-items:center;flex-wrap:wrap" },
        d.status ? badge(t(d.status), { tone: DELIVERY_TONE[d.status] || "neutral", testid: "rev-delivery-status" }) : badge(t("not delivered"), { tone: "neutral", testid: "rev-delivery-status" }),
        d.branch ? h("code", null, d.branch) : null,
        pr.number
          ? badge(`PR #${pr.number}`, { tone: pr.state === "open" ? "blue" : "neutral", mono: true })
          : null,
        prUrl
          ? h("a", { class: "btn btn-ghost btn-xs", href: prUrl, target: "_blank", rel: "noopener" }, icon("external", { size: 13 }), t("View PR"))
          : null,
      ),
      d.error
        ? banner({
            tone: "danger",
            title: t("Delivery failed"),
            body: h("code", null, typeof d.error === "string" ? d.error : d.error.message || d.error.code || t("unknown")),
            testid: "rev-delivery-error",
          })
        : null,
      canDeliver && d.status !== "delivered"
        ? h(
            "div",
            { class: "row", style: "gap:8px" },
            actionButton(
              wantsPR ? t("Publish & open PR") : t("Publish changes"),
              () => publish(rev),
              { variant: "secondary", size: "sm", iconName: "upload", testid: `publish-rev-${rev.n}` },
            ),
          )
        : null,
      !canDeliver && !d.status
        ? h("p", { class: "muted" }, t("Publishes when the run finishes, per the task's delivery policy."))
        : null,
    );
  }

  function reviewsBlock(rev) {
    const reviews = reviewsFor(rev);
    const task = getTask() || {};
    const canReview = task.status !== "cancelled" && rev.status === "ready";
    return h(
      "div",
      { class: "fields" },
      reviews.length
        ? h(
            "ul",
            { class: "review-list", "data-testid": "review-list" },
            reviews.map((rv) =>
              h(
                "li",
                { class: "review-item" },
                h(
                  "div",
                  { class: "row", style: "gap:8px;align-items:center;flex-wrap:wrap" },
                  badge(t(VERDICT_META[rv.verdict]?.label || rv.verdict), {
                    tone: VERDICT_META[rv.verdict]?.tone || "neutral",
                  }),
                  rv.stale ? badge(t("Stale"), { tone: "amber", title: t("A newer revision landed after this review"), testid: "review-stale" }) : null,
                  !rv.independent ? badge(t("self-review"), { tone: "neutral", title: t("Written by the producing agent — does not satisfy the merge gate") }) : null,
                  h("span", { class: "subtle" }, reviewerName(rv)),
                  h("span", { class: "subtle" }, fmtRelative(rv.created_at)),
                  rv.comment_url ? h("a", { class: "subtle", href: rv.comment_url, target: "_blank", rel: "noopener" }, t("PR comment")) : null,
                ),
                (rv.findings || []).length
                  ? h(
                      "ul",
                      { class: "finding-list" },
                      rv.findings.map((f) =>
                        h(
                          "li",
                          null,
                          f.severity ? badge(f.severity, { tone: "neutral" }) : null,
                          f.path ? h("code", null, `${f.path}${f.line ? `:${f.line}` : ""}`) : null,
                          " ",
                          f.message,
                        ),
                      ),
                    )
                  : null,
              ),
            ),
          )
        : h("p", { class: "muted", "data-testid": "no-reviews" }, t("No reviews yet.")),
      canReview
        ? button(t("Add review"), {
            variant: "secondary",
            size: "sm",
            iconName: "circleCheck",
            testid: `add-review-${rev.n}`,
            onClick: () => reviewDialog(rev),
          })
        : null,
    );
  }

  function mergeBlock(rev) {
    const task = getTask() || {};
    const d = rev.delivery || {};
    const pr = d.pull_request || {};
    const wantsPR = Boolean(
      task.request?.delivery?.pull_request || task.resolved?.git?.auto_create_pr,
    );
    if (!wantsPR && !pr.number) return null;
    if (d.merged) return null;
    const items = mergeChecklist(rev, state.reviews);
    const ready = items.every((i) => i.ok);
    return card({
      title: t("Merge readiness"),
      iconName: "merge",
      testid: "merge-card",
      class: "rev-sub-card",
      body: h(
        "div",
        { class: "fields" },
        h(
          "ul",
          { class: "check-list", "data-testid": "merge-checklist" },
          items.map((i) =>
            h(
              "li",
              { class: ["check-item", i.ok && "is-ok"] },
              icon(i.ok ? "circleCheck" : "circleX", { size: 14 }),
              h("span", null, i.label, i.hint ? h("span", { class: "subtle" }, ` — ${i.hint}`) : null),
            ),
          ),
        ),
        actionButton(t("Merge pull request"), () => merge(rev), {
          variant: "primary",
          size: "sm",
          iconName: "merge",
          disabled: !ready,
          title: ready ? undefined : t("The checklist above shows what is missing."),
          testid: "merge-button",
        }),
      ),
    });
  }

  function technicalDetails(rev) {
    return h(
      "details",
      { class: "tech-details" },
      h("summary", null, t("Technical details")),
      kv([
        [t("Revision"), mono(rev.id)],
        [t("Run"), rev.run_id ? mono(rev.run_id) : null],
        [t("Artifact"), rev.artifact_id ? h("a", { href: href(`/artifacts/${encodeURIComponent(rev.artifact_id)}`) }, h("code", null, rev.artifact_id)) : null],
        [t("Repository"), rev.repo ? h("code", null, rev.repo) : null],
        [t("Base → head"), rev.base_sha || rev.head_sha ? h("code", null, `${shortSha(rev.base_sha)} → ${shortSha(rev.head_sha)}`) : null],
        [t("Created"), fmtDateTime(rev.created_at)],
        [t("Updated"), fmtDateTime(rev.updated_at)],
      ]),
    );
  }

  function pipeline(rev) {
    const d = rev.delivery || {};
    const pr = d.pull_request || {};
    const review = currentReview(state.reviews, rev);
    return h(
      "div",
      { class: "pipeline pipe-4", "data-testid": "rev-pipeline" },
      step(t("Revision {n}", { n: rev.n }), rev.status === "ready" ? shortSha(rev.head_sha) : t("failed"), {
        done: rev.status === "ready",
        warn: rev.status !== "ready",
        iconName: "fileDiff",
      }),
      step(
        t("Delivered"),
        d.merged ? t("merged") : pr.number ? `PR #${pr.number}` : d.status === "delivered" ? d.branch || t("branch") : d.status || "—",
        { done: d.status === "delivered", warn: d.status === "failed", iconName: "upload" },
      ),
      step(
        t("Reviewed"),
        review ? t(VERDICT_META[review.verdict]?.label || review.verdict) : reviewsFor(rev).length ? t("stale") : "—",
        { done: Boolean(review && review.verdict === "approve"), warn: Boolean(review && review.verdict === "request_changes") || (reviewsFor(rev).length > 0 && !review), iconName: "eye" },
      ),
      step(t("Merged"), d.merged ? shortSha(d.merge_commit_sha) : "—", { done: Boolean(d.merged), iconName: "merge" }),
    );
  }

  function revisionItem(rev) {
    const open = state.open === rev.id;
    const isLatest = rev === latest();
    const review = currentReview(state.reviews, rev);
    const d = rev.delivery || {};
    const head = h(
      "button",
      {
        class: "rev-head",
        type: "button",
        "aria-expanded": String(open),
        "data-testid": `rev-head-${rev.n}`,
        onClick: () => {
          state.open = open ? null : rev.id;
          if (!open) void loadDiff(rev);
          render();
        },
      },
      icon(open ? "chevronDown" : "chevronRight", { size: 14 }),
      h("span", { class: "rev-title" }, t("Revision {n}", { n: rev.n })),
      isLatest ? badge(t("latest"), { tone: "blue", testid: "rev-latest" }) : null,
      rev.status !== "ready" ? badge(t("failed"), { tone: "red" }) : null,
      d.merged
        ? badge(t("merged"), { tone: "violet" })
        : d.status === "delivered"
          ? badge(t("delivered"), { tone: "green" })
          : d.status === "failed"
            ? badge(t("delivery failed"), { tone: "red" })
            : null,
      review
        ? badge(t(VERDICT_META[review.verdict]?.label || review.verdict), { tone: VERDICT_META[review.verdict]?.tone || "neutral" })
        : reviewsFor(rev).some((r) => r.stale)
          ? badge(t("review stale"), { tone: "amber", testid: "rev-head-stale" })
          : null,
      rev.run_id ? h("span", { class: "subtle mono" }, rev.run_id) : null,
      h("span", { class: "subtle" }, fmtRelative(rev.created_at)),
    );
    if (!open) return h("li", { class: "rev-item" }, head);

    const diff = state.diffs.get(rev.id);
    return h(
      "li",
      { class: "rev-item is-open" },
      head,
      h(
        "div",
        { class: "rev-body" },
        rev.error
          ? banner({
              tone: "danger",
              title: t("Changes could not be packaged"),
              body: h("code", null, rev.error.message || rev.error.code || String(rev.error)),
              testid: "rev-error",
            })
          : null,
        pipeline(rev),
        card({
          title: t("Changes"),
          iconName: "fileDiff",
          class: "rev-sub-card",
          testid: `rev-diff-${rev.n}`,
          body:
            rev.status !== "ready"
              ? h("p", { class: "muted" }, t("No diff — the revision failed to materialize."))
              : !diff || diff.status === "loading"
                ? skeleton(4)
                : diff.status === "missing"
                  ? h("p", { class: "muted", "data-testid": "diff-missing" }, t("No diff recorded — the run left the tree unchanged."))
                  : diff.status === "error"
                    ? h("p", { class: "muted" }, t("Could not load the diff."), button(t("Retry"), { variant: "ghost", size: "xs", onClick: () => void loadDiff(rev) }))
                    : diff.files.length
                      ? diffBlock(diff.files, rev.artifact_id)
                      : h("p", { class: "muted", "data-testid": "diff-empty" }, t("The patch is empty — the run left the tree unchanged.")),
        }),
        card({ title: t("Delivery"), iconName: "upload", class: "rev-sub-card", testid: `rev-delivery-${rev.n}`, body: deliveryBlock(rev) }),
        card({ title: t("Review"), iconName: "eye", class: "rev-sub-card", testid: `rev-review-${rev.n}`, body: reviewsBlock(rev) }),
        mergeBlock(rev),
        technicalDetails(rev),
      ),
    );
  }

  function render() {
    const task = getTask() || {};
    if (state.error && !state.revisions) {
      mount(el, errorBanner(state.error, { retry: () => void refresh() }));
      return;
    }
    if (state.revisions === null) {
      mount(el, h("div", { class: "card" }, h("div", { class: "card-body" }, skeleton(5))));
      return;
    }
    const repo = task.resolved?.source?.repo || task.request?.source?.repo;
    if (!state.revisions.length) {
      mount(
        el,
        repo
          ? emptyState({
              iconName: "fileDiff",
              title: isTaskLive(task.status) ? t("Working — no revision yet") : t("No changes recorded"),
              body: isTaskLive(task.status)
                ? t("A revision with the diff materializes when a run finishes.")
                : t("Finished runs did not leave any code changes."),
              testid: "changes-empty",
            })
          : emptyState({
              iconName: "message",
              title: t("No repository"),
              body: t("This task did not target a repository — the result is in the conversation."),
              testid: "changes-norepo",
            }),
      );
      return;
    }
    const ordered = state.revisions.slice().sort((a, b) => b.n - a.n);
    mount(
      el,
      h("ul", { class: "rev-list" }, ordered.map(revisionItem)),
    );
  }

  return {
    el,
    refresh,
    /** Revisions currently known — the task header uses it for its summary. */
    get revisions() {
      return state.revisions;
    },
    get reviews() {
      return state.reviews;
    },
  };
}
