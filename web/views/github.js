import { api } from "../lib/api.js";
import { h, httpUrl, mount } from "../lib/dom.js";
import { fmtDateTime, fmtRelative } from "../lib/format.js";
import { t } from "../lib/i18n.js";
import { icon } from "../lib/icons.js";
import {
  actionButton,
  badge,
  banner,
  card,
  codeBlock,
  confirmDialog,
  emptyState,
  errorBanner,
  field,
  kv,
  pageHeader,
  skeleton,
  toast,
  toastError,
} from "../lib/ui.js";
import { adminGate } from "./admin-gate.js";
import { docsUrl } from "./shell.js";

const CALLBACK_KEY = "sbx.console.github_callback";
const PENDING_KEY = "sbx.console.github_pending";

export function renderGithub() {
  const gate = adminGate(t("GitHub"), t("Let agents clone, push and open pull requests on private repositories."));
  if (gate) return gate;
  const body = h("div", null, skeleton(6));
  const noticeEl = h("div");

  async function completeCallback() {
    const raw = sessionStorage.getItem(CALLBACK_KEY);
    if (!raw) return;
    sessionStorage.removeItem(CALLBACK_KEY);
    const cb = JSON.parse(raw);
    const state = cb.state || sessionStorage.getItem(PENDING_KEY) || "";
    try {
      const res = await api.githubCallback({ installation_id: Number(cb.installation_id), state });
      sessionStorage.removeItem(PENDING_KEY);
      mount(noticeEl, banner({ tone: "success", title: t("GitHub connected"), body: t("Installation on {account} recorded.", { account: res.installation.account_login }), testid: "github-connected" }));
    } catch (err) {
      mount(noticeEl, errorBanner(err));
    }
  }

  function manualCallback() {
    const f = { id: "" };
    return h(
      "form",
      {
        class: "input-group",
        onSubmit: async (ev) => {
          ev.preventDefault();
          const state = sessionStorage.getItem(PENDING_KEY);
          if (!state) {
            toast(t("Start the authorization first."), { tone: "danger" });
            return;
          }
          sessionStorage.setItem(CALLBACK_KEY, JSON.stringify({ installation_id: f.id, state }));
          await completeCallback();
          await load();
        },
      },
      h("input", { class: "input mono", placeholder: t("installation id from the GitHub redirect"), inputmode: "numeric", onInput: (e) => (f.id = e.target.value.trim()) }),
      h("button", { class: "btn", type: "submit" }, t("Complete")),
    );
  }

  async function load() {
    let st;
    try {
      st = await api.githubStatus();
    } catch (err) {
      mount(body, errorBanner(err, { retry: load }));
      return;
    }
    const posture = card({
      title: t("Authorization"),
      iconName: "github",
      testid: "github-status",
      actions: st.installable
        ? [
            actionButton(t("Sync"), async () => {
              try {
                await api.githubSync();
                toast(t("Installations refreshed from GitHub"), { tone: "success" });
              } catch (err) {
                toastError(err, t("Sync failed"));
              }
              await load();
            }, { size: "sm", iconName: "refresh" }),
            actionButton(t("Connect GitHub"), async () => {
              try {
                const res = await api.githubAuthorize();
                const url = httpUrl(res.authorize_url);
                if (!url) throw new Error("authorize_url is not an http(s) URL");
                sessionStorage.setItem(PENDING_KEY, res.state);
                window.open(url, "_blank", "noopener");
                toast(t("Finish the installation on GitHub, then come back here."), { tone: "neutral", timeout: 8000 });
              } catch (err) {
                toastError(err, t("Could not start authorization"));
              }
            }, { size: "sm", variant: "primary", iconName: "external", testid: "github-connect" }),
          ]
        : null,
      body: h(
        "div",
        { class: "stack" },
        kv([
          [t("GitHub App"), st.configured ? badge(t("configured"), { tone: "green" }) : badge(t("not configured"), { tone: "neutral" })],
          [t("App"), st.app_slug ? h("a", { href: `https://github.com/apps/${st.app_slug}`, target: "_blank", rel: "noopener noreferrer" }, st.app_slug) : null],
          [t("App id"), st.app_id ? h("code", null, st.app_id) : null],
          [t("Token fallback"), st.bridge_token ? badge(t("GH_TOKEN available"), { tone: "amber" }) : badge(t("none"), { tone: "neutral" })],
        ]),
        !st.configured
          ? h(
              "div",
              { class: "stack", style: "gap:10px" },
              h("p", { class: "muted" }, t("Configure a GitHub App on the control plane to authorize repositories with one click — no personal tokens. Set these in the control plane environment (or its Modal Secret), then redeploy:")),
              codeBlock("SBX_GITHUB_APP_ID=123456\nSBX_GITHUB_APP_SLUG=my-sbx-app\nSBX_GITHUB_APP_PRIVATE_KEY=<PEM, from a Modal Secret>\nSBX_GITHUB_EPHEMERAL=1"),
              h("p", { class: "field-hint" }, t("Set the App's Setup URL to this console's address so GitHub returns here after installation.")),
              h("a", { href: docsUrl("guides/github"), target: "_blank", rel: "noopener noreferrer" }, t("GitHub integration guide →")),
            )
          : null,
        st.installable && sessionStorage.getItem(PENDING_KEY)
          ? field(t("Didn't come back automatically?"), manualCallback(), { hint: t("Paste the installation id from the URL GitHub redirected to.") })
          : null,
      ),
    });

    const installs = card({
      title: t("Installations"),
      subtitle: t("Repositories each installation authorizes. Tokens are minted per repo, short-lived, and never leave the control plane except into sandboxes."),
      iconName: "server",
      body: st.installations.length
        ? h(
            "div",
            { class: "table-wrap", style: "border:0" },
            h(
              "table",
              { class: "table", "data-testid": "github-installations" },
              h("thead", null, h("tr", null, [t("Account"), t("Repositories"), t("Synced"), ""].map((c) => h("th", null, c)))),
              h(
                "tbody",
                null,
                st.installations.map((inst) =>
                  h(
                    "tr",
                    null,
                    h("td", null, h("div", { class: "cell-title" }, h("strong", null, inst.account_login, inst.suspended ? badge(t("suspended"), { tone: "amber" }) : null), h("span", { class: "cell-sub" }, `${inst.account_type || ""} · #${inst.installation_id}`))),
                    h(
                      "td",
                      null,
                      inst.repository_selection === "all"
                        ? badge(t("All repositories"), { tone: "accent" })
                        : h("div", { class: "row", style: "gap:4px" }, (inst.repositories || []).map((r) => badge(r, { mono: true }))),
                    ),
                    h("td", { class: "muted nowrap", title: fmtDateTime(inst.synced_at || inst.recorded_at) }, fmtRelative(inst.synced_at || inst.recorded_at)),
                    h(
                      "td",
                      { class: "num" },
                      actionButton(t("Revoke"), async () => {
                        const ok = await confirmDialog({
                          title: t("Revoke {account}?", { account: inst.account_login }),
                          body: t("Uninstalls the App on GitHub (best effort) and forgets the installation and cached tokens. Reconnect any time."),
                          confirmLabel: t("Revoke"),
                        });
                        if (!ok) return;
                        try {
                          const res = await api.githubRevoke(inst.installation_id);
                          toast(res.remote_deleted ? t("Installation removed on GitHub") : t("Installation forgotten locally"), { tone: "success" });
                        } catch (err) {
                          toastError(err, t("Revoke failed"));
                        }
                        await load();
                      }, { size: "sm", variant: "ghost", iconName: "ban" }),
                    ),
                  ),
                ),
              ),
            ),
          )
        : emptyState({
            iconName: "github",
            title: t("No installations"),
            body: st.configured ? t("Connect GitHub and pick the repositories agents may use.") : t("Public repositories work without any GitHub setup."),
            compact: true,
          }),
    });

    mount(
      body,
      h(
        "div",
        { class: "stack" },
        posture,
        installs,
        card({
          title: t("Without the GitHub App"),
          iconName: "info",
          body: h(
            "ul",
            { class: "muted", style: "margin:0;padding-left:18px;display:grid;gap:6px" },
            h("li", null, t("Public repositories clone anonymously — nothing to configure.")),
            h("li", null, t("A fine-grained token in GH_TOKEN plus SBX_GITHUB_EPHEMERAL=1 still works as a fallback.")),
            h("li", null, t("Artifacts move work between agents without any shared remote.")),
          ),
        }),
      ),
    );
  }

  const el = h(
    "div",
    { class: "page" },
    pageHeader({
      title: t("GitHub"),
      subtitle: t("Let agents clone, push and open pull requests on private repositories."),
      testid: "page-title",
    }),
    noticeEl,
    body,
  );
  void (async () => {
    await completeCallback();
    await load();
  })();
  return { el, title: t("GitHub"), icon };
}
