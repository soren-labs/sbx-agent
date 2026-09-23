/** Hash router: `#/agents/abc?tab=workspace` → {name, params, query}. */

const ROUTES = [
  ["connect", /^\/connect$/],
  ["agents", /^\/agents$/],
  ["agent-new", /^\/agents\/new$/],
  ["agent", /^\/agents\/([^/]+)$/, ["id"]],
  ["workflows", /^\/workflows$/],
  ["workflow", /^\/workflows\/([^/]+)$/, ["id"]],
  ["artifacts", /^\/artifacts$/],
  ["artifact", /^\/artifacts\/([^/]+)$/, ["id"]],
  ["capacity", /^\/capacity$/],
  ["accounts", /^\/admin\/accounts$/],
  ["keys", /^\/admin\/keys$/],
  ["github", /^\/admin\/github$/],
  ["settings", /^\/settings$/],
];

export function parseHash(hash = window.location.hash) {
  const raw = hash.replace(/^#/, "") || "/agents";
  const [path, qs = ""] = raw.split("?");
  const query = Object.fromEntries(new URLSearchParams(qs));
  for (const [name, re, keys = []] of ROUTES) {
    const m = path.match(re);
    if (m) {
      const params = {};
      keys.forEach((key, idx) => {
        params[key] = decodeURIComponent(m[idx + 1]);
      });
      return { name, params, query, path };
    }
  }
  return { name: "not-found", params: {}, query, path };
}

export function href(path, query) {
  const qs = query
    ? new URLSearchParams(
        Object.entries(query).filter(([, v]) => v != null && v !== ""),
      ).toString()
    : "";
  return `#${path}${qs ? `?${qs}` : ""}`;
}

/** `silent` rewrites the URL (e.g. filter state) without re-rendering the route. */
export function navigate(path, query, { replace = false, silent = false } = {}) {
  const next = href(path, query);
  if (replace || silent) {
    history.replaceState(null, "", next);
    if (!silent) window.dispatchEvent(new HashChangeEvent("hashchange"));
  } else if (window.location.hash !== next) {
    window.location.hash = next;
  }
}
