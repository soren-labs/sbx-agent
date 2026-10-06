import { describe, expect, it, vi } from "vitest";
import { createApiClient } from "../api/client";
import { ApiError } from "../api/errors";
import { mockFetch } from "../test/helpers";

const fast = { retryDelayMs: 0 };

describe("api client", () => {
  it("sends CSRF + a fresh Idempotency-Key on mutations, none on reads", async () => {
    const m = mockFetch([
      ["POST", "/api/auth/login", { json: { user: { id: "u" }, workspaces: [], auth: {}, csrf_token: "csrf-1", expires_at: "x" } }],
      ["POST", "/api/api-keys", { status: 201, json: { id: "k", key: "sbx_x" } }],
      ["GET", "/api/api-keys", { json: { items: [] } }],
    ]);
    const api = createApiClient({ fetch: m.fetch, ...fast });
    await api.auth.login("a@b.c", "pw");
    await api.apiKeys.create("one");
    await api.apiKeys.create("two");
    await api.apiKeys.list();

    const [login, one, two, list] = m.calls;
    expect(login.headers["X-CSRF-Token"]).toBeUndefined(); // no token before login
    expect(one.headers["X-CSRF-Token"]).toBe("csrf-1");
    expect(one.headers["Idempotency-Key"]).toBeTruthy();
    expect(two.headers["Idempotency-Key"]).toBeTruthy();
    expect(two.headers["Idempotency-Key"]).not.toBe(one.headers["Idempotency-Key"]);
    expect(list.headers["Idempotency-Key"]).toBeUndefined();
    expect(list.headers["X-CSRF-Token"]).toBeUndefined();
  });

  it("uses same-origin credentials and never touches web storage", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const seen: RequestInit[] = [];
    const api = createApiClient({
      fetch: (async (_u: RequestInfo | URL, init?: RequestInit) => {
        seen.push(init!);
        return new Response("{}", { status: 200 });
      }) as typeof fetch,
    });
    await api.auth.login("a@b.c", "secret-pw");
    expect(seen[0].credentials).toBe("same-origin");
    expect(setItem).not.toHaveBeenCalled();
    setItem.mockRestore();
  });

  it("maps canonical error bodies", async () => {
    const m = mockFetch([
      [
        "DELETE",
        "/api/connections/c1",
        {
          status: 409,
          json: { error: { code: "connection_in_use", category: "conflict", message: "in use", retryable: false, details: { executors: 2 }, request_id: "req_1", action: "release_dependents" } },
        },
      ],
      ["GET", "/api/me", { status: 500, body: "boom" }],
    ]);
    const api = createApiClient({ fetch: m.fetch, ...fast });
    const err = (await api.connections.disconnect("c1").catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 409, code: "connection_in_use", category: "conflict", requestId: "req_1", action: "release_dependents", retryable: false });
    expect(err.details).toEqual({ executors: 2 });
    const plain = (await api.auth.me().catch((e) => e)) as ApiError;
    expect(plain).toMatchObject({ status: 500, code: "http_500" });
  });

  it("retries an uncertain mutation with the same Idempotency-Key", async () => {
    let n = 0;
    const m = mockFetch([]);
    const api = createApiClient({
      fetch: (async (u: RequestInfo | URL, init?: RequestInit) => {
        if (n++ === 0) throw new TypeError("network down");
        return m.fetch(u, init);
      }) as typeof fetch,
      ...fast,
    });
    // second attempt reaches the recording double (404 is a definite answer)
    await api.sessions.sendMessage("s1", { content: "hi" }).catch(() => undefined);
    expect(n).toBe(2);
    expect(m.calls).toHaveLength(1);
    const first = m.calls[0].headers["Idempotency-Key"];
    expect(first).toBeTruthy();

    const keys: string[] = [];
    const down = createApiClient({
      fetch: (async (_u: RequestInfo | URL, init?: RequestInit) => {
        keys.push((init!.headers as Record<string, string>)["Idempotency-Key"]);
        throw new TypeError("offline");
      }) as typeof fetch,
      maxRetries: 2,
      ...fast,
    });
    const err = (await down.sessions.sendMessage("s1", { content: "hi" }).catch((e) => e)) as ApiError;
    expect(keys).toHaveLength(3);
    expect(new Set(keys).size).toBe(1);
    expect(err.code).toBe("outcome_unknown");
    expect(err.idempotencyKey).toBe(keys[0]);

    // the caller can retry the same logical request with the key it was told about
    await down.sessions.sendMessage("s1", { content: "hi" }, { idempotencyKey: err.idempotencyKey! }).catch(() => undefined);
    expect(keys[3]).toBe(keys[0]);
  });

  it("does not retry definite server answers", async () => {
    const m = mockFetch([["POST", "/api/sessions/s1/messages", { status: 422, json: { error: { code: "validation_failed", message: "bad" } } }]]);
    const api = createApiClient({ fetch: m.fetch, ...fast });
    await expect(api.sessions.sendMessage("s1", { content: "" })).rejects.toMatchObject({ code: "validation_failed" });
    expect(m.calls).toHaveLength(1);
  });

  it("signals unauthenticated responses", async () => {
    const onUnauthenticated = vi.fn();
    const m = mockFetch([["GET", "/api/me", { status: 401, json: { error: { code: "unauthenticated", message: "sign in" } } }]]);
    const api = createApiClient({ fetch: m.fetch, onUnauthenticated });
    await api.auth.me().catch(() => undefined);
    expect(onUnauthenticated).toHaveBeenCalledOnce();
  });

  it("parses SSE frames split across chunks and honours `after`", async () => {
    const enc = new TextEncoder();
    const env = (seq: number) => JSON.stringify({ id: `e${seq}`, seq, type: "turn.started", payload: {} });
    const frame = (seq: number) => `id: ${seq}\nevent: turn.started\ndata: ${env(seq)}\n\n`;
    const text = frame(1) + ": heartbeat\n\n" + frame(2);
    const cut = Math.floor(text.length / 2);
    const m = mockFetch([
      [
        "GET",
        "/api/sessions/s1/events",
        {
          body: new ReadableStream({
            start(c) {
              c.enqueue(enc.encode(text.slice(0, cut)));
              c.enqueue(enc.encode(text.slice(cut)));
              c.close();
            },
          }),
          headers: { "content-type": "text/event-stream" },
        },
      ],
    ]);
    const api = createApiClient({ fetch: m.fetch });
    const got: number[] = [];
    await api.sessions.streamEvents("s1", { after: 7, onEvent: (e) => got.push(e.seq) });
    expect(got).toEqual([1, 2]);
    expect(m.calls[0].query.get("after")).toBe("7");
    expect(m.calls[0].headers.Accept).toBe("text/event-stream");
  });
});
