import { hasScope } from "../lib/config.js";
import { h } from "../lib/dom.js";
import { t } from "../lib/i18n.js";
import { emptyState, pageHeader } from "../lib/ui.js";

/** Admin pages render a lock screen for keys without the admin scope. */
export function adminGate(title, subtitle) {
  if (hasScope("admin")) return null;
  return {
    el: h(
      "div",
      { class: "page" },
      pageHeader({ title, subtitle, testid: "page-title" }),
      emptyState({
        iconName: "lock",
        title: t("Admin scope required"),
        body: t("This key only has the agents scope. Connect with a key that has the admin scope to manage accounts, API keys and the GitHub integration."),
        actions: [h("a", { class: "btn", href: "#/connect" }, t("Switch key"))],
        testid: "admin-locked",
      }),
    ),
    title,
  };
}
