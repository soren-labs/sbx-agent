import { api } from "../lib/api.js";
import { h, mount } from "../lib/dom.js";
import { PROVIDER_META, PROVIDERS, providerLabel } from "../lib/domain.js";
import { fmtDateTime, fmtRelative } from "../lib/format.js";
import { t } from "../lib/i18n.js";
import { icon } from "../lib/icons.js";
import {
  actionButton,
  badge,
  banner,
  button,
  confirmDialog,
  emptyState,
  errorBanner,
  field,
  openDialog,
  pageHeader,
  poller,
  progressBar,
  providerTag,
  skeleton,
  statusBadge,
  toast,
  toastError,
} from "../lib/ui.js";
import { adminGate } from "./admin-gate.js";

function importDialog(onDone) {
  const f = { provider: "codex", label: "", maxConcurrent: "1", models: "", files: [] };
  const body = h("div", { class: "fields" });
  const addFile = (path = PROVIDER_META[f.provider].credential) => {
    f.files.push({ path, content: "", name: "" });
  };
  addFile();

  function render() {
    mount(
      body,
      banner({
        tone: "info",
        title: t("Credentials go straight to your control plane"),
        body: t("Files are sent over HTTPS, stored in your Modal workspace and restored with mode 0600 inside sandboxes. They are never returned by the API or shown here again."),
      }),
      h(
        "div",
        { class: "fields-2" },
        field(
          t("Provider"),
          h(
            "select",
            {
              class: "select",
              "data-testid": "acct-provider",
              onChange: (e) => {
                f.provider = e.target.value;
                f.files = [];
                addFile();
                render();
              },
            },
            PROVIDERS.map((p) => h("option", { value: p, selected: p === f.provider }, providerLabel(p))),
          ),
        ),
        field(t("Label"), h("input", { class: "input", value: f.label, placeholder: t("e.g. team pro seat 2"), "data-testid": "acct-label", onInput: (e) => (f.label = e.target.value) }), { required: true }),
      ),
      h(
        "div",
        { class: "fields-2" },
        field(t("Concurrent runs (slots)"), h("input", { class: "input", type: "number", min: "1", value: f.maxConcurrent, onInput: (e) => (f.maxConcurrent = e.target.value) })),
        field(t("Models (comma separated)"), h("input", { class: "input mono", value: f.models, placeholder: t("optional"), onInput: (e) => (f.models = e.target.value) })),
      ),
      field(
        t("Credential files"),
        h(
          "div",
          { class: "stack", style: "gap:8px" },
          f.files.map((file, idx) =>
            h(
              "div",
              { class: "cred-file" },
              h("input", { class: "input mono", value: file.path, title: t("Path relative to $HOME inside the sandbox"), onInput: (e) => (file.path = e.target.value) }),
              h(
                "label",
                { class: ["btn btn-sm", file.content && "tone-green"] },
                icon(file.content ? "check" : "upload"),
                file.content ? t("Loaded ({n} bytes)", { n: file.content.length }) : t("Choose file"),
                h("input", {
                  type: "file",
                  hidden: true,
                  "data-testid": `acct-file-${idx}`,
                  onChange: async (e) => {
                    const picked = e.target.files?.[0];
                    if (!picked) return;
                    file.content = await picked.text();
                    file.name = picked.name;
                    render();
                  },
                }),
              ),
              f.files.length > 1 ? button("", { variant: "ghost", size: "sm", iconName: "trash", title: t("Remove"), onClick: () => { f.files.splice(idx, 1); render(); } }) : h("span"),
            ),
          ),
          h("div", null, button(t("Add another file"), { variant: "ghost", size: "sm", iconName: "plus", onClick: () => { addFile(""); render(); } })),
        ),
        {
          hint: t("The same file the provider CLI writes on login — run `{login}` locally, then pick `~/{path}`.", {
            login: PROVIDER_META[f.provider].login,
            path: PROVIDER_META[f.provider].credential,
          }),
        },
      ),
    );
  }
  render();

  const { close } = openDialog({
    title: t("Import an account"),
    description: t("Adds a provider login to the scheduling pool."),
    size: "lg",
    testid: "import-dialog",
    body,
    footer: [
      button(t("Cancel"), { onClick: () => close() }),
      actionButton(t("Import account"), async () => {
        if (!f.label.trim()) {
          toast(t("A label is required."), { tone: "danger" });
          return;
        }
        const files = Object.fromEntries(f.files.filter((x) => x.path.trim() && x.content).map((x) => [x.path.trim(), x.content]));
        const payload = {
          provider: f.provider,
          label: f.label.trim(),
          max_concurrent: Math.max(1, Number(f.maxConcurrent) || 1),
          models: f.models.split(",").map((m) => m.trim()).filter(Boolean),
        };
        if (Object.keys(files).length) payload.credential = { files };
        try {
          const acct = await api.createAccount(payload);
          toast(t("Account {id} imported", { id: acct.id }), { tone: "success" });
          close();
          onDone?.(acct);
        } catch (err) {
          toastError(err, t("Import failed"));
        }
      }, { variant: "primary", iconName: "upload", testid: "acct-submit" }),
    ],
  });
}

export function renderAccounts() {
  const gate = adminGate(t("Accounts"), t("Provider logins in the scheduling pool."));
  if (gate) return gate;
  const listEl = h("div", null, h("div", { class: "card" }, h("div", { class: "card-body" }, skeleton(4))));

  async function load() {
    let accounts;
    try {
      accounts = (await api.listAccounts()).accounts || [];
    } catch (err) {
      mount(listEl, errorBanner(err, { retry: load }));
      return;
    }
    if (!accounts.length) {
      mount(
        listEl,
        emptyState({
          iconName: "users",
          title: t("No accounts yet"),
          body: t("Agents run under your own provider subscriptions. Import the credential file a provider CLI writes on login."),
          actions: [button(t("Import account"), { variant: "primary", iconName: "plus", onClick: () => importDialog(load) })],
          testid: "accounts-empty",
        }),
      );
      return;
    }
    mount(
      listEl,
      h(
        "div",
        { class: "table-wrap" },
        h(
          "table",
          { class: "table", "data-testid": "accounts-table" },
          h("thead", null, h("tr", null, [t("Account"), t("Provider"), t("Status"), t("Slots"), t("Models"), t("Last used"), ""].map((c) => h("th", null, c)))),
          h(
            "tbody",
            null,
            accounts.map((a) =>
              h(
                "tr",
                { "data-testid": "account-row", "data-id": a.id },
                h("td", null, h("div", { class: "cell-title" }, h("strong", null, a.label), h("span", { class: "cell-sub mono" }, a.id))),
                h("td", null, providerTag(a.provider)),
                h(
                  "td",
                  null,
                  h(
                    "div",
                    { class: "cell-title" },
                    statusBadge("account", a.status),
                    a.status === "cooling" && a.cooldown_until ? h("span", { class: "cell-sub", title: fmtDateTime(a.cooldown_until) }, `${t("until")} ${fmtRelative(a.cooldown_until)}`) : null,
                    a.last_error ? h("span", { class: "cell-sub mono" }, a.last_error) : null,
                  ),
                ),
                h("td", { style: "min-width:120px" }, h("div", { class: "cell-title" }, h("span", { class: "mono" }, `${a.running ?? 0} / ${a.max_concurrent}`), progressBar(a.running ?? 0, a.max_concurrent, { tone: (a.running ?? 0) >= a.max_concurrent ? "amber" : "accent" }))),
                h("td", null, h("div", { class: "row", style: "gap:4px" }, (a.models || []).map((m) => badge(m, { mono: true })))),
                h("td", { class: "muted nowrap" }, a.last_used_at ? fmtRelative(a.last_used_at) : t("never")),
                h(
                  "td",
                  { class: "num nowrap" },
                  actionButton(t("Verify"), async () => {
                    try {
                      const res = await api.verifyAccount(a.id);
                      toast(res.status === "active" ? t("{id} verified — credential works", { id: a.id }) : t("{id} is {status}", { id: a.id, status: res.status }), {
                        tone: res.status === "active" ? "success" : "danger",
                        detail: res.last_error || undefined,
                      });
                    } catch (err) {
                      toastError(err, t("Verify failed"));
                    }
                    await load();
                  }, { size: "sm", variant: "ghost", iconName: "shield", title: t("Probe the credential in a throwaway sandbox"), testid: "account-verify" }),
                  button("", {
                    size: "sm",
                    variant: "ghost",
                    iconName: "trash",
                    title: t("Remove account"),
                    onClick: async () => {
                      const ok = await confirmDialog({
                        title: t("Remove {label}?", { label: a.label }),
                        body: t("The account and its stored credential are deleted. Agents already running keep their sandbox until they end."),
                        confirmLabel: t("Remove account"),
                      });
                      if (!ok) return;
                      try {
                        await api.deleteAccount(a.id);
                        toast(t("Account removed"), { tone: "success" });
                      } catch (err) {
                        toastError(err, t("Could not remove the account"));
                      }
                      await load();
                    },
                  }),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }

  const el = h(
    "div",
    { class: "page page-wide" },
    pageHeader({
      title: t("Accounts"),
      subtitle: t("Provider logins agents run under. The scheduler spreads runs across active accounts, respects each account's slots and cools down accounts that hit rate limits."),
      actions: [button(t("Import account"), { variant: "primary", iconName: "plus", testid: "import-account", onClick: () => importDialog(load) })],
      testid: "page-title",
    }),
    listEl,
  );
  void load();
  const poll = poller(load, 10000);
  return { el, title: t("Accounts"), dispose: () => poll.stop() };
}
