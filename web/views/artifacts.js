import { api, unwrapArtifact } from "../lib/api.js";
import { h, mount } from "../lib/dom.js";
import { fmtBytes, fmtDateTime, fmtRelative, shortSha } from "../lib/format.js";
import { t } from "../lib/i18n.js";
import { icon } from "../lib/icons.js";
import { href, navigate } from "../lib/router.js";
import {
  actionButton,
  badge,
  button,
  card,
  emptyState,
  errorBanner,
  field,
  kv,
  mono,
  openDialog,
  pageHeader,
  skeleton,
  toast,
  toastError,
} from "../lib/ui.js";

/** Fetch a member with the Bearer header and save it via a blob URL. */
export async function downloadMember(artifactId, member) {
  try {
    const res = await api.downloadArtifact(artifactId, member);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = h("a", { href: url, download: `${artifactId}-${member.replace(/\//g, "_")}` });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch (err) {
    toastError(err, t("Download failed"));
  }
}

function testsBadge(tests) {
  if (!tests?.length) return h("span", { class: "subtle" }, "—");
  const failed = tests.filter((x) => x.exit_code !== 0).length;
  return failed
    ? badge(t("{n} failed", { n: failed }), { tone: "red" })
    : badge(t("{n} passed", { n: tests.length }), { tone: "green" });
}

export function artifactTable(artifacts, { hideProducer } = {}) {
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      { class: "table", "data-testid": "artifacts-table" },
      h(
        "thead",
        null,
        h(
          "tr",
          null,
          [t("Artifact"), t("Base → head"), t("Files"), t("Tests"), hideProducer ? null : t("Produced by"), t("Created")]
            .filter(Boolean)
            .map((c) => h("th", null, c)),
        ),
      ),
      h(
        "tbody",
        null,
        artifacts.map((a) =>
          h(
            "tr",
            {
              class: "is-link",
              "data-testid": "artifact-row",
              onClick: (ev) => {
                if (ev.target.closest("a,button")) return;
                navigate(`/artifacts/${encodeURIComponent(a.artifact_id)}`);
              },
            },
            h(
              "td",
              null,
              h(
                "div",
                { class: "cell-title" },
                h("a", { href: href(`/artifacts/${encodeURIComponent(a.artifact_id)}`) }, h("strong", { class: "mono" }, a.artifact_id)),
                h("span", { class: "cell-sub mono", title: a.repo }, a.repo || ""),
              ),
            ),
            h("td", { class: "mono nowrap" }, `${shortSha(a.base_sha)} → ${shortSha(a.head_sha)}`),
            h("td", null, String(a.files?.length ?? 0)),
            h("td", null, testsBadge(a.tests)),
            hideProducer
              ? null
              : h("td", null, a.producer?.agent_id ? h("a", { href: href(`/agents/${encodeURIComponent(a.producer.agent_id)}`), class: "mono" }, a.producer.agent_id) : "—"),
            h("td", { class: "nowrap muted", title: fmtDateTime(a.created_at) }, fmtRelative(a.created_at)),
          ),
        ),
      ),
    ),
  );
}

export function snapshotDialog(agentId, runs, onDone) {
  const f = { run: "", test: "" };
  const { close } = openDialog({
    title: t("Snapshot the workspace"),
    description: t("Packages the patch, a git bundle and every collected file into a durable artifact."),
    testid: "snapshot-dialog",
    body: h(
      "div",
      { class: "fields" },
      field(
        t("Attach to run"),
        h(
          "select",
          { class: "select", onChange: (e) => (f.run = e.target.value) },
          h("option", { value: "" }, t("Latest run")),
          [...runs].reverse().map((r) => h("option", { value: r.id }, `${r.id} · ${r.status}`)),
        ),
        { hint: t("The run record gets an artifact:// reference.") },
      ),
      field(t("Test command (optional)"), h("input", { class: "input mono", placeholder: "pytest -q", "data-testid": "snapshot-test", onInput: (e) => (f.test = e.target.value) }), {
        hint: t("Runs in the workdir; the exit code is recorded in the manifest."),
      }),
      h("p", { class: "field-hint" }, t("Credential-like content in the workspace aborts the snapshot — nothing is stored.")),
    ),
    footer: [
      button(t("Cancel"), { onClick: () => close() }),
      actionButton(t("Create snapshot"), async () => {
        const body = {};
        if (f.run) body.run_id = f.run;
        if (f.test.trim()) body.test_command = f.test.trim();
        try {
          const art = unwrapArtifact(await api.createArtifact(agentId, body));
          toast(t("Artifact {id} created", { id: art.artifact_id }), { tone: "success" });
          close();
          onDone?.(art);
        } catch (err) {
          toastError(err, t("Snapshot failed"));
        }
      }, { variant: "primary", iconName: "camera", testid: "snapshot-submit" }),
    ],
  });
}

export function renderArtifacts({ route }) {
  const state = { agentId: route.query.agent || "" };
  const listEl = h("div", null, h("div", { class: "card" }, h("div", { class: "card-body" }, skeleton(4))));

  async function load() {
    try {
      const res = await api.listArtifacts({ agent_id: state.agentId || undefined });
      const items = (res.artifacts || []).slice().sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
      mount(
        listEl,
        items.length
          ? artifactTable(items)
          : emptyState({
              iconName: "package",
              title: state.agentId ? t("No artifacts from this agent") : t("No artifacts yet"),
              body: t("Artifacts are sha256-verified snapshots of an agent's workspace — patch, git bundle and files. Create one from an agent with a repository, then hand it to another agent."),
              testid: "artifacts-empty",
            }),
      );
    } catch (err) {
      mount(listEl, errorBanner(err, { retry: load }));
    }
  }

  const filter = h("input", {
    class: "input mono",
    placeholder: t("Filter by producing agent id"),
    value: state.agentId,
    style: "max-width:320px",
    "data-testid": "artifacts-agent-filter",
    onChange: (ev) => {
      state.agentId = ev.target.value.trim();
      navigate("/artifacts", { agent: state.agentId || null }, { silent: true });
      void load();
    },
  });

  const el = h(
    "div",
    { class: "page page-wide" },
    pageHeader({
      title: t("Artifacts"),
      subtitle: t("Durable, checksum-verified workspace snapshots. They survive sandbox teardown and power cross-agent handoffs."),
      testid: "page-title",
    }),
    h("div", { class: "toolbar" }, h("div", { class: "input-affix grow", style: "max-width:340px" }, icon("search", { size: 14 }), filter)),
    listEl,
  );
  void load();
  return { el, title: t("Artifacts") };
}

export function renderArtifact({ route }) {
  const id = route.params.id;
  const body = h("div", null, h("div", { class: "card" }, h("div", { class: "card-body" }, skeleton(6))));
  const header = h("div", null, pageHeader({ title: id, back: { href: "#/artifacts", label: t("Artifacts") } }));

  async function load() {
    let art;
    try {
      art = unwrapArtifact(await api.getArtifact(id));
    } catch (err) {
      mount(
        body,
        err.status === 404 ? emptyState({ iconName: "search", title: t("Artifact not found") }) : errorBanner(err, { retry: load }),
      );
      return;
    }
    // base_ref is not in the manifest; the producer's workspace knows it.
    let baseRef = "";
    if (art.producer?.agent_id) {
      try {
        baseRef = (await api.workspace(art.producer.agent_id)).workspace?.base_ref || "";
      } catch {
        baseRef = "";
      }
    }
    const handoffHref = href("/agents/new", {
      repo: art.repo,
      base_ref: baseRef || null,
      base_sha: art.base_sha,
      handoff_artifact: art.artifact_id,
    });
    mount(
      header,
      pageHeader({
        title: art.artifact_id,
        testid: "artifact-title",
        back: { href: "#/artifacts", label: t("Artifacts") },
        actions: [
          h("a", { class: "btn btn-primary", href: handoffHref, "data-testid": "artifact-handoff" }, icon("handoff"), t("Hand off to a new agent")),
          button(t("manifest.json"), { iconName: "download", onClick: () => downloadMember(art.artifact_id, "manifest.json") }),
        ],
        meta: [
          badge(art.format || "patch", { mono: true }),
          h("span", { title: fmtDateTime(art.created_at) }, icon("clock", { size: 13 }), fmtRelative(art.created_at)),
          art.producer?.agent_id ? h("a", { href: href(`/agents/${encodeURIComponent(art.producer.agent_id)}`) }, icon("bot", { size: 13 }), art.producer.agent_id) : null,
          art.producer?.run_id ? h("span", { class: "mono" }, art.producer.run_id) : null,
        ],
      }),
    );
    const payloads = Object.entries(art.payloads || {});
    mount(
      body,
      h(
        "div",
        { class: "stack" },
        art.warnings?.length
          ? h("div", { class: "stack", style: "gap:8px" }, art.warnings.map((w) => h("div", { class: "banner tone-warning" }, icon("warning"), h("div", { class: "banner-body" }, w))))
          : null,
        h(
          "div",
          { class: "grid-2" },
          card({
            title: t("Source"),
            iconName: "branch",
            body: kv([
              [t("Repository"), h("code", { style: "overflow-wrap:anywhere" }, art.repo || "—")],
              [t("Base sha"), mono(art.base_sha, { short: 12 })],
              [t("Head sha"), mono(art.head_sha, { short: 12 })],
              [t("Schema version"), art.schema_version != null ? String(art.schema_version) : null],
            ]),
          }),
          card({
            title: t("Payloads"),
            subtitle: t("Apply patch.diff on the base, or fetch repo.bundle for the exact head."),
            iconName: "package",
            body: payloads.length
              ? h(
                  "div",
                  { class: "stack", style: "gap:10px" },
                  payloads.map(([member, sha]) =>
                    h(
                      "div",
                      { class: "spread" },
                      h("div", { class: "cell-title" }, h("strong", { class: "mono" }, member), h("span", { class: "cell-sub mono", title: sha }, `sha256 ${String(sha).slice(0, 16)}…`)),
                      button(t("Download"), { size: "sm", iconName: "download", testid: `download-${member}`, onClick: () => downloadMember(art.artifact_id, member) }),
                    ),
                  ),
                )
              : h("p", { class: "muted" }, t("No payloads.")),
          }),
        ),
        art.tests?.length
          ? card({
              title: t("Tests"),
              iconName: "flask",
              body: h(
                "div",
                { class: "stack", style: "gap:8px" },
                art.tests.map((x) => h("div", { class: "spread" }, h("code", null, x.command), badge(`exit ${x.exit_code}`, { tone: x.exit_code === 0 ? "green" : "red", mono: true }))),
              ),
            })
          : null,
        card({
          title: t("Files ({n})", { n: art.files?.length || 0 }),
          iconName: "file",
          body: h(
            "div",
            { class: "table-wrap", style: "border:0" },
            h(
              "table",
              { class: "table", "data-testid": "artifact-files" },
              h("thead", null, h("tr", null, h("th", null, t("Path")), h("th", { class: "num" }, t("Size")), h("th", null, "sha256"), h("th", null, ""))),
              h(
                "tbody",
                null,
                (art.files || []).map((file) =>
                  h(
                    "tr",
                    null,
                    h("td", { class: "mono" }, file.path),
                    h("td", { class: "num" }, fmtBytes(file.size)),
                    h("td", { class: "mono subtle", title: file.sha256 }, String(file.sha256).slice(0, 12)),
                    h("td", { class: "num" }, button("", { variant: "ghost", size: "xs", iconName: "download", title: t("Download"), onClick: () => downloadMember(art.artifact_id, `files/${file.path}`) })),
                  ),
                ),
              ),
            ),
          ),
        }),
      ),
    );
  }

  void load();
  return { el: h("div", { class: "page page-wide" }, header, body), title: id };
}
