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

// SOR-220: two connect paths. DEFAULT — `api.githubInstall()` returns the
// official github.com/apps/<public SBX App>/installations/new URL (broker
// mode); the browser installs the pre-registered App once and GitHub/broker
// bounce it back through /v1/github/install/callback → `?broker=connected`.
// ADVANCED/self-hosted — the manifest flow below posts {manifest} to
// settings/apps/new and GitHub returns via /v1/github/app/manifest/callback
// → `?manifest=connected` or `?manifest_error=<code>`.
function postManifest(res) {
  const url = httpUrl(res.manifest_url);
  if (!url) throw new Error("manifest_url is not an http(s) URL");
  const form = h(
    "form",
    { method: "POST", action: url, hidden: true, "data-testid": "manifest-form" },
    h("input", { name: "manifest", value: JSON.stringify(res.manifest) }),
  );
  document.body.append(form);
  form.submit();
}

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

  function manifestNotice() {
    const q = new URLSearchParams((location.hash.split("?")[1] || ""));
    if (q.get("manifest") === "connected") {
      mount(
        noticeEl,
        banner({
          tone: "success",
          title: t("GitHub App registered"),
          body: t("This deployment now has its own app — connect it below."),
          testid: "manifest-connected",
        }),
      );
    } else if (q.get("manifest_error")) {
      mount(
        noticeEl,
        banner({
          tone: "danger",
          title: t("GitHub App registration failed"),
          body: t("GitHub returned `{code}` — start the registration again.", { code: q.get("manifest_error") }),
          testid: "manifest-error",
        }),
      );
    }
    if (q.get("broker") === "connected") {
      mount(
        noticeEl,
        banner({
          tone: "success",
          title: t("GitHub connected"),
          body: t("The SBX App is installed — repository tokens are minted on demand."),
          testid: "broker-connected",
        }),
      );
    } else if (q.get("broker_error")) {
      mount(
        noticeEl,
        banner({
          tone: "danger",
          title: t("GitHub connect failed"),
          body: t("The broker returned `{code}` — start Connect GitHub again.", { code: q.get("broker_error") }),
          testid: "broker-error",
        }),
      );
    }
    if (q.get("manifest") || q.get("manifest_error") || q.get("broker") || q.get("broker_error")) {
      history.replaceState(null, "", `${location.pathname}${location.hash.split("?")[0]}`);
    }
  }

  // Default Connect GitHub (SOR-220): POST /v1/github/install resolves the
  // broker session and hands back the *GitHub App installation* URL — the
  // first GitHub page is always installations/new, never settings/apps/new.
  // mode "app" (deployment-local App) keeps the SOR-177 state handshake.
  async function connectGitHub() {
    try {
      const res = await api.githubInstall();
      const url = httpUrl(res.authorize_url);
      if (!url) throw new Error("authorize_url is not an http(s) URL");
      if (res.mode === "broker") {
        // The install page bounces straight back through the broker to
        // /v1/github/install/callback → this view shows ?broker=connected.
        window.location.assign(url);
        return;
      }
      if (res.state) sessionStorage.setItem(PENDING_KEY, res.state);
      window.open(url, "_blank", "noopener");
      toast(t("Finish the installation on GitHub, then come back here."), { tone: "neutral", timeout: 8000 });
    } catch (err) {
      toastError(err, t("Could not start Connect GitHub"));
    }
  }

  async function load() {
    let st;
    try {
      st = await api.githubStatus();
    } catch (err) {
      mount(body, errorBanner(err, { retry: load }));
      return;
    }
    manifestNotice();
    const posture = card({
      title: t("Authorization"),
      iconName: "github",
      testid: "github-status",
      actions: (st.installable || (st.broker && st.broker.url))
        ? [
            st.installable || st.installations.length
              ? actionButton(t("Sync"), async () => {
                  try {
                    await api.githubSync();
                    toast(t("Installations refreshed from GitHub"), { tone: "success" });
                  } catch (err) {
                    toastError(err, t("Sync failed"));
                  }
                  await load();
                }, { size: "sm", iconName: "refresh" })
              : null,
            actionButton(t("Connect GitHub"), connectGitHub, { size: "sm", variant: "primary", iconName: "external", testid: "github-connect" }),
          ]
        : null,
      body: h(
        "div",
        { class: "stack" },
        kv([
          [t("GitHub App"), st.configured ? badge(t("configured"), { tone: "green" }) : badge(t("not configured"), { tone: "neutral" })],
          st.source ? [t("Config source"), badge(st.source === "registry" ? t("registered") : st.source, { mono: true, testid: "github-source" })] : null,
          [t("App"), st.app_slug ? h("a", { href: st.app_url || `https://github.com/apps/${st.app_slug}`, target: "_blank", rel: "noopener noreferrer" }, st.app_slug) : null],
          [t("App id"), st.app_id ? h("code", null, st.app_id) : null],
          [t("Token fallback"), st.bridge_token ? badge(t("GH_TOKEN available"), { tone: "amber" }) : badge(t("none"), { tone: "neutral" })],
          st.broker && st.broker.url
            ? [t("Connect lane"), st.broker.bound ? badge(t("brokered"), { tone: "green", testid: "github-broker-bound" }) : badge(st.broker.healthy ? t("broker ready") : t("broker unreachable"), { tone: st.broker.healthy ? "neutral" : "amber", testid: "github-broker-health" })]
            : null,
        ]),
        !st.configured
          ? h(
              "div",
              { class: "stack", style: "gap:10px" },
              h("p", { class: "muted" }, t("Authorize repositories with one click — no personal tokens, no app registration. Connect GitHub installs the hosted SBX App and stores only installation metadata here.")),
              h(
                "div",
                null,
                actionButton(t("Connect GitHub"), connectGitHub, { variant: "primary", iconName: "github", testid: "github-connect-default" }),
              ),
              h(
                "details",
                { class: "muted", "data-testid": "github-advanced" },
                h("summary", null, t("Advanced: fully self-hosted — register your own GitHub App")),
                h("p", null, t("This deployment runs its own App and stores its private key itself (GitHub's manifest flow). Only needed when you can't or won't use the hosted broker.")),
                h(
                  "div",
                  { style: "margin:8px 0" },
                  actionButton(t("Create GitHub App"), async () => {
                    try {
                      postManifest(await api.githubManifest({}));
                    } catch (err) {
                      toastError(err, t("Could not start GitHub App registration"));
                    }
                  }, { variant: "ghost", iconName: "github", testid: "github-create-app" }),
                ),
                h("p", null, t("Or set the App env vars yourself (requires a redeploy):")),
                codeBlock("SBX_GITHUB_APP_ID=123456\nSBX_GITHUB_APP_SLUG=my-sbx-app\nSBX_GITHUB_APP_PRIVATE_KEY=<PEM, from a Modal Secret>\nSBX_GITHUB_EPHEMERAL=1"),
                h("p", { class: "field-hint" }, t("Set the App's Setup URL to this console's address so GitHub returns here after installation.")),
                h("a", { href: docsUrl("guides/github"), target: "_blank", rel: "noopener noreferrer" }, t("GitHub integration guide →")),
              ),
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
      back: { href: "#/integrations", label: t("Integrations") },
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
