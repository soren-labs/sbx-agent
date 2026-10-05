import { act, render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { createApiClient } from "../api/client";
import type { Connection, Me, Session } from "../api/types";
import { I18nProvider } from "../i18n";
import { AuthProvider } from "../state/auth";
import { ApiProvider } from "../state/context";

export interface RecordedCall {
  method: string;
  path: string;
  query: URLSearchParams;
  headers: Record<string, string>;
  body: unknown;
}
export interface Reply {
  status?: number;
  json?: unknown;
  body?: BodyInit | null;
  headers?: Record<string, string>;
}
export type Handler = Reply | ((call: RecordedCall) => Reply | Promise<Reply>);
type Route = [method: string, path: string | RegExp, handler: Handler];

/** Minimal fetch double: routes by METHOD + path, records every call. */
export function mockFetch(routes: Route[]) {
  const calls: RecordedCall[] = [];
  const fn = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const call: RecordedCall = {
      method: (init?.method ?? "GET").toUpperCase(),
      path: url.pathname,
      query: url.searchParams,
      headers: Object.fromEntries(Object.entries((init?.headers ?? {}) as Record<string, string>)),
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
    };
    calls.push(call);
    const route = routes.find(([m, p]) => m === call.method && (typeof p === "string" ? p === call.path : p.test(call.path)));
    if (!route) return new Response(JSON.stringify({ error: { code: "not_found", message: `no mock for ${call.method} ${call.path}` } }), { status: 404 });
    const h = route[2];
    const r = typeof h === "function" ? await h(call) : h;
    if (r.body !== undefined) return new Response(r.body, { status: r.status ?? 200, headers: r.headers });
    return new Response(r.json === undefined ? null : JSON.stringify(r.json), {
      status: r.status ?? 200,
      headers: { "content-type": "application/json", ...r.headers },
    });
  }) as typeof fetch;
  return { fetch: fn, calls, find: (method: string, path: string | RegExp) => calls.filter((c) => c.method === method && (typeof path === "string" ? c.path === path : path.test(c.path))) };
}

export const ME: Me = {
  user: { id: "u1", email: "dev@example.com", email_verified: true },
  workspaces: [{ id: "w1", name: "Personal", kind: "personal" }],
  auth: { via: "cookie" },
};

export function connection(over: Partial<Connection> & Pick<Connection, "kind">): Connection {
  return {
    id: `c_${over.kind}`,
    label: over.kind,
    state: "configured",
    health: "ready",
    health_reason: null,
    external_identity: null,
    version: 1,
    credential: null,
    validation: null,
    catalog: null,
    ...over,
  };
}

export function session(over: Partial<Session> = {}): Session {
  return {
    id: "s1",
    lifecycle: "open",
    activity: "idle",
    role: "developer",
    title: "Fix the build",
    harness: { provider_id: "opencode", model: "big-pickle" },
    executor: { backend: "modal", lease_id: null, lease_state: null },
    worktree: { availability: "unavailable", generation: 1, base_sha: "abc" },
    parent_session_id: null,
    actions: ["send", "archive", "close"],
    version: 1,
    ...over,
  };
}

export const identityRoutes: Route[] = [["GET", "/api/me", { json: ME }]];

/** Renders under the real providers; `/api/me` must be routed (see identityRoutes). */
export async function renderApp(ui: ReactElement, fetchImpl: typeof fetch, route = "/") {
  const client = createApiClient({ fetch: fetchImpl, retryDelayMs: 0 });
  let view!: ReturnType<typeof render>;
  await act(async () => {
    view = render(
    <I18nProvider>
      <ApiProvider client={client}>
        <AuthProvider>
          <MemoryRouter initialEntries={[route]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
            {ui}
          </MemoryRouter>
        </AuthProvider>
      </ApiProvider>
    </I18nProvider>,
    );
  });
  return view;
}
