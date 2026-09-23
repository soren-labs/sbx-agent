import { api } from "../lib/api.js";
import { getConnection } from "../lib/config.js";
import { h, mount } from "../lib/dom.js";
import { fmtDateTime, fmtRelative } from "../lib/format.js";
import { t } from "../lib/i18n.js";
import { icon } from "../lib/icons.js";
import {
  actionButton,
  badge,
  banner,
  button,
  confirmDialog,
  copyButton,
  errorBanner,
  field,
  openDialog,
  pageHeader,
  skeleton,
  toast,
  toastError,
  toggle,
} from "../lib/ui.js";
import { adminGate } from "./admin-gate.js";

function createDialog(onDone) {
  const f = { label: "", admin: false };
  const body = h("div", { class: "fields" });
  const renderForm = () =>
    mount(
      body,
      field(t("Label"), h("input", { class: "input", placeholder: t("e.g. ci-pipeline"), "data-testid": "key-label", onInput: (e) => (f.label = e.target.value) }), {
        hint: t("Helps you recognise the key later. The key itself is shown once."),
      }),
      toggle(t("Admin scope"), f.admin, (v) => {
        f.admin = v;
      }, { hint: t("Can manage accounts, API keys and the GitHub integration. Leave off for automation."), testid: "key-admin" }),
    );
  renderForm();
  let created = null;
  const footer = h("div", { class: "row" });
  const { close } = openDialog({
    title: t("Create an API key"),
    testid: "key-dialog",
    body,
    footer,
    onClose: () => created && onDone?.(),
  });
  mount(
    footer,
    button(t("Cancel"), { onClick: () => close() }),
    actionButton(t("Create key"), async () => {
      try {
        created = await api.createKey({ label: f.label.trim(), scopes: f.admin ? ["agents", "admin"] : ["agents"] });
      } catch (err) {
        toastError(err, t("Could not create the key"));
        return;
      }
      mount(
        body,
        h(
          "div",
          { class: "secret-reveal", "data-testid": "key-reveal" },
          h("strong", null, icon("warning", { size: 14 }), " ", t("Copy this key now — it will not be shown again.")),
          h("code", { "data-testid": "key-plaintext" }, created.key),
          h("div", { class: "row" }, copyButton(created.key, { label: t("Copy key") })),
        ),
        h("p", { class: "field-hint" }, t("The control plane stores only its sha256 hash.")),
      );
      mount(footer, button(t("Done"), { variant: "primary", onClick: () => close() }));
    }, { variant: "primary", iconName: "key", testid: "key-submit" }),
  );
}

export function renderKeys() {
  const gate = adminGate(t("API keys"), t("Bearer keys for the /v1 API."));
  if (gate) return gate;
  const listEl = h("div", null, h("div", { class: "card" }, h("div", { class: "card-body" }, skeleton(4))));
  const currentId = getConnection().identity?.key_id;

  async function load() {
    let keys;
    try {
      keys = (await api.listKeys()).api_keys || [];
    } catch (err) {
      mount(listEl, errorBanner(err, { retry: load }));
      return;
    }
    keys.sort((a, b) => Number(Boolean(a.revoked_at)) - Number(Boolean(b.revoked_at)) || (b.created_at || "").localeCompare(a.created_at || ""));
    mount(
      listEl,
      h(
        "div",
        { class: "table-wrap" },
        h(
          "table",
          { class: "table", "data-testid": "keys-table" },
          h("thead", null, h("tr", null, [t("Key"), t("Scopes"), t("Created"), t("Status"), ""].map((c) => h("th", null, c)))),
          h(
            "tbody",
            null,
            keys.map((k) =>
              h(
                "tr",
                { "data-testid": "key-row", "data-id": k.id },
                h("td", null, h("div", { class: "cell-title" }, h("strong", null, k.label || t("(no label)"), k.id === currentId ? badge(t("this browser"), { tone: "accent" }) : null), h("span", { class: "cell-sub mono" }, k.id))),
                h("td", null, h("div", { class: "row", style: "gap:4px" }, k.scopes.map((s) => badge(s, { tone: s === "admin" ? "violet" : "neutral" })))),
                h("td", { class: "muted nowrap", title: fmtDateTime(k.created_at) }, fmtRelative(k.created_at)),
                h("td", null, k.revoked_at ? badge(t("revoked {when}", { when: fmtRelative(k.revoked_at) }), { tone: "red" }) : badge(t("active"), { tone: "green" })),
                h(
                  "td",
                  { class: "num" },
                  k.revoked_at
                    ? null
                    : button(t("Revoke"), {
                        size: "sm",
                        variant: "ghost",
                        iconName: "ban",
                        testid: "key-revoke",
                        onClick: async () => {
                          const self = k.id === currentId;
                          const ok = await confirmDialog({
                            title: t("Revoke {label}?", { label: k.label || k.id }),
                            body: self ? t("This is the key this browser is using — you will be disconnected immediately.") : t("Clients using this key get 401 from now on. This cannot be undone."),
                            confirmLabel: t("Revoke key"),
                          });
                          if (!ok) return;
                          try {
                            await api.revokeKey(k.id);
                            toast(t("Key revoked"), { tone: "success" });
                          } catch (err) {
                            toastError(err, t("Could not revoke the key"));
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
    { class: "page" },
    pageHeader({
      title: t("API keys"),
      subtitle: t("Each key is a Bearer token for /v1. Agents, runs and workflows are scoped to the key that created them."),
      actions: [button(t("Create key"), { variant: "primary", iconName: "plus", testid: "create-key", onClick: () => createDialog(load) })],
      testid: "page-title",
    }),
    banner({ tone: "neutral", title: t("Keys are stored as sha256 hashes only"), body: t("A lost key cannot be recovered — create a new one and revoke the old.") }),
    h("div", { style: "margin-top:16px" }, listEl),
  );
  void load();
  return { el, title: t("API keys") };
}
