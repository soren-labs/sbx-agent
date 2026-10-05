// In-browser stand-in for the hosted control plane, used only by the
// credential-free demo build (VITE_HOSTED=1 VITE_API_MODE=mock).
type State = {
  user: { id: string; email: string } | null;
  modal: any | null;
  github: any[];
  codex: any | null;
  keys: any[];
  pending?: string;
};
const KEY = "sbx.demo.hosted";
const initial = (): State => ({
  user: null,
  modal: null,
  github: [
    {
      installation_id: 1,
      account_login: "soren-labs",
      repositories: ["soren-labs/sbx-browser", "soren-labs/docs", "soren-labs/website"],
    },
  ],
  codex: { id: "conn-codex", provider: "codex", state: "connected", version: 1, metadata: {} },
  keys: [],
});
function load(): State {
  try {
    return { ...initial(), ...JSON.parse(localStorage.getItem(KEY) ?? "{}") };
  } catch {
    return initial();
  }
}
const save = (s: State) => localStorage.setItem(KEY, JSON.stringify(s));
const delay = (ms = 260) => new Promise((r) => setTimeout(r, ms));
class MockError extends Error {}
const modalReady = () => ({
  id: "conn-modal",
  provider: "modal",
  state: "ready",
  version: 1,
  metadata: {
    progress: ["Workspace verified", "Runtime image published", "Sandbox smoke test"],
    runtime_version: "0.1.1",
  },
});

export async function mockHostedRequest(path: string, body?: any, method?: string): Promise<any> {
  await delay();
  const s = load();
  const verb = method ?? (body === undefined ? "GET" : "POST");
  const done = <T>(value: T) => (save(s), value);
  if (path === "/auth/me") {
    if (!s.user) throw new MockError("Please sign in to continue.");
    return { user: s.user };
  }
  if (path === "/auth/login") {
    if (!body.email || !body.password) throw new MockError("invalid credentials");
    s.user = { id: "user-demo", email: body.email };
    return done({ user: s.user });
  }
  if (path === "/auth/register") {
    s.pending = body.email;
    return done({ challenge_id: "demo-challenge", resend_after_s: 30 });
  }
  if (path === "/auth/verify") {
    if (!/^\d{6}$/.test(body.code)) throw new MockError("invalid code");
    return { registration_token: "demo-grant" };
  }
  if (path === "/auth/password") {
    s.user = { id: "user-demo", email: s.pending ?? "you@example.com" };
    return done({ user: s.user });
  }
  if (path === "/auth/logout") {
    s.user = null;
    return done({});
  }
  if (path === "/hosted/repositories")
    return { repositories: s.github.flatMap((i) => i.repositories.map((name: string) => ({ name }))) };
  if (path.startsWith("/hosted/connections/modal")) {
    if (path.endsWith("/authorize")) return { mock: true, state: "demo" };
    if (path.endsWith("/mock-approve") || path.endsWith("/provision") || verb === "POST") {
      s.modal = modalReady();
      return done({ connection: s.modal });
    }
    return { connection: s.modal, configured: true, mock: false, oauth_configured: true };
  }
  if (path.startsWith("/hosted/connections/github")) {
    if (path.endsWith("/authorize")) return { mock: true, state: "demo" };
    if (path.endsWith("/mock-approve")) {
      s.github = initial().github;
      return done({});
    }
    if (verb === "DELETE") {
      s.github = [];
      return done({});
    }
    return { installations: s.github, configured: true, mock: false, binding_mode: "direct" };
  }
  if (path.startsWith("/hosted/connections/codex")) {
    if (path.endsWith("/authorize")) return { mock: true, state: "demo" };
    if (path.endsWith("/mock-approve")) {
      s.codex = initial().codex;
      return done({});
    }
    if (path.endsWith("/refresh")) return {};
    if (verb === "DELETE") {
      s.codex = { ...initial().codex, state: "disabled" };
      return done({});
    }
    return { connection: s.codex, configured: true, mock: false };
  }
  if (path.startsWith("/hosted/api-keys")) {
    if (verb === "DELETE") {
      const id = path.split("/").at(-1);
      s.keys = s.keys.map((k) => (k.id === id ? { ...k, revoked_at: new Date().toISOString() } : k));
      return done({});
    }
    if (verb === "POST") {
      const expires = body.expires_in_days ? new Date(Date.now() + body.expires_in_days * 864e5).toISOString() : null;
      s.keys.push({ id: `key-${Date.now()}`, label: body.label, scopes: body.scopes, expires_at: expires, created_at: new Date().toISOString() });
      return done({ key: "sbx_demo_" + Math.random().toString(36).slice(2, 14) });
    }
    return { api_keys: s.keys };
  }
  if (path.includes("/review-sessions")) return { sessions: [] };
  throw new MockError("not available in demo");
}
