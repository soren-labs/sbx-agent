import { h } from "../lib/dom.js";
import { t } from "../lib/i18n.js";
import { emptyState } from "../lib/ui.js";

export function renderNotFound() {
  return {
    el: h(
      "div",
      { class: "page" },
      emptyState({
        iconName: "search",
        title: t("Page not found"),
        actions: [h("a", { class: "btn btn-primary", href: "#/agents" }, t("Go to agents"))],
      }),
    ),
    title: t("Not found"),
  };
}
