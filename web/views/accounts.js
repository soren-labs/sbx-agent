import { api } from "../lib/api.js";
import { h, httpUrl, mount } from "../lib/dom.js";
import { PROVIDER_META, PROVIDERS, providerLabel } from "../lib/domain.js";
import { fmtDateTime, fmtRelative } from "../lib/format.js";
import { t } from "../lib/i18n.js";
import { icon } from "../lib/icons.js";
import {
  actionButton,
  badge,
  banner,
  button,
  codeBlock,
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

const CONNECT_TERMINAL = ["verified", "materialized", "failed", "cancelled", "expired"];

// SOR-214: the functional connect seam — run the provider's own login via
// POST /v1/auth/connect and surface the canonical session live (hosted
// lane: browser/device URL scraped from the login output; pair lane: the
// single-use `sbx auth pair` command on deploys with no host CLI).
function connectDialog(onDone, opts = {}) {
  const relink = opts.account || null;
  const f = {
    provider: relink?.provider || "codex",
    label: relink?.label || "",
    maxConcurrent: "1",
    models: "",
  };
  let session = null;
  let timer = null;
  const statusEl = h("div", { "data-testid": "connect-status" });
  const body = h("div", { class: "fields" }, statusEl);

  const stopPoll = () => {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  };

  function renderStatus() {
    if (!session) {
      mount(statusEl);
      return;
    }
    const terminal = session.state !== "authenticating";
    mount(
      statusEl,
      h(
        "div",
        { class: "stack", style: "gap:10px;margin-top:8px" },
        h(
          "div",
          { class: "row" },
          badge(session.state, {
            tone: session.state === "verified" ? "green" : session.state === "materialized" ? "accent" : terminal ? "amber" : "accent",
            testid: "connect-state",
          }),
          h("span", { class: "muted" }, session.kind === "pair" ? t("local pairing — no provider CLI on the control plane") : t("hosted login")),
          session.state === "authenticating" ? h("span", { class: "muted" }, "…") : null,
        ),
        session.browser_url
          ? field(
              t("Finish the login in your browser"),
              h("a", { href: httpUrl(session.browser_url) || "#", target: "_blank", rel: "noopener noreferrer", "data-testid": "connect-url" }, session.browser_url),
            )
          : null,
        session.user_code
          ? field(t("Enter this code"), h("div", { "data-testid": "connect-code" }, codeBlock(session.user_code)))
          : null,
        session.pair_command
          ? field(
              t("Run on the machine that can sign in"),
              h("div", { "data-testid": "connect-pair" }, codeBlock(session.pair_command)),
              { hint: t("The ticket is single-use and short-lived; the CLI runs the vendor login locally and posts the captured credential back.") },
            )
          : null,
        session.error ? banner({ tone: "danger", body: session.error, testid: "connect-error" }) : null,
      ),
    );
  }

  async function refresh() {
    if (!session || !timer) return;
    try {
      const fresh = await api.connectSession(session.id);
      // The server persists only the ticket hash — the redeemable
      // pair_command is minted once at begin; keep showing it while the
      // session is still waiting on a pairing.
      if (session.pair_command && !fresh.pair_command) {
        fresh.pair_ticket = session.pair_ticket;
        fresh.pair_command = session.pair_command;
      }
      session = fresh;
    } catch {
      return;
    }
    renderStatus();
    if (CONNECT_TERMINAL.includes(session.state)) {
      stopPoll();
      if (session.state === "verified" || session.state === "materialized") {
        toast(t("Account {id} connected ({state})", { id: session.account_id, state: session.state }), { tone: "success" });
        onDone?.(session);
      }
    }
  }

  function render() {
    mount(
      body,
      banner({
        tone: "info",
        title: t("No token paste"),
        body: t("Runs the provider's official login — the same engine as `sbx auth` — then captures, imports and verifies the credential for you."),
      }),
      relink
        ? field(t("Provider"), providerTag(f.provider))
        : h(
            "div",
            { class: "fields-2" },
            field(
              t("Provider"),
              h(
                "select",
                {
                  class: "select",
                  "data-testid": "connect-provider-select",
                  onChange: (e) => (f.provider = e.target.value),
                },
                PROVIDERS.map((p) => h("option", { value: p, selected: p === f.provider }, providerLabel(p))),
              ),
            ),
            field(t("Label"), h("input", { class: "input", value: f.label, placeholder: t("e.g. team pro seat 2"), "data-testid": "connect-label", onInput: (e) => (f.label = e.target.value) })),
          ),
      statusEl,
    );
    renderStatus();
  }
  render();

  const dlg = openDialog({
    title: relink ? t("Reconnect {label}", { label: f.label }) : t("Connect a provider"),
    description: relink
      ? t("Re-authenticate the existing account in place — keeps its id, slots and scheduler state.")
      : t("Sign in with the provider's own flow — no credential file to upload."),
    size: "lg",
    testid: "connect-dialog",
    body,
    onClose: () => {
      stopPoll();
      if (session && session.state === "authenticating") {
        void api.connectCancel(session.id).catch(() => {});
      }
      onDone?.();
    },
    footer: [
      button(t("Cancel"), { onClick: () => dlg.close() }),
      actionButton(relink ? t("Reconnect") : t("Start login"), async () => {
        try {
          session = await api.authConnect({
            provider: f.provider,
            label: f.label.trim(),
            account_id: relink?.id || null,
            max_concurrent: Math.max(1, Number(f.maxConcurrent) || 1),
            models: f.models.split(",").map((m) => m.trim()).filter(Boolean),
          });
          renderStatus();
          if (session.state === "authenticating") {
            timer = setInterval(refresh, 1500);
          } else {
            await refresh();
          }
        } catch (err) {
          toastError(err, t("Connect failed"));
        }
      }, { variant: "primary", iconName: "link", testid: "connect-submit" }),
    ],
  });
}

const AUTH_STATE_TONES = {
  verified: "green",
  materialized: "accent",
  authenticating: "accent",
  reauth_required: "amber",
  unhealthy: "amber",
  unauthenticated: "neutral",
  disabled: "neutral",
};

function authStateBadge(account) {
  const state = account.auth_state;
  if (!state || state === "verified") return null;
  return badge(state, { tone: AUTH_STATE_TONES[state] || "neutral", mono: true, testid: "auth-state" });
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
          actions: [
            button(t("Connect provider"), { variant: "primary", iconName: "link", testid: "connect-provider", onClick: () => connectDialog(load) }),
            button(t("Import account"), { iconName: "plus", onClick: () => importDialog(load) }),
          ],
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
                    authStateBadge(a),
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
                  (a.auth_state === "reauth_required" || a.auth_state === "unauthenticated" || a.status === "invalid")
                    ? button(t("Reconnect"), {
                        size: "sm",
                        variant: "ghost",
                        iconName: "link",
                        title: t("Run the provider's login again for this account (SOR-214 relink)"),
                        testid: "account-relink",
                        onClick: () => connectDialog(load, { account: a }),
                      })
                    : null,
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
      actions: [
        button(t("Connect provider"), { variant: "primary", iconName: "link", testid: "connect-provider", onClick: () => connectDialog(load) }),
        button(t("Import account"), { iconName: "plus", testid: "import-account", onClick: () => importDialog(load) }),
      ],
      testid: "page-title",
    }),
    listEl,
  );
  void load();
  const poll = poller(load, 10000);
  return { el, title: t("Accounts"), dispose: () => poll.stop() };
}
