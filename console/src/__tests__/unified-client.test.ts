import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, UnifiedApi } from "../api/unified";

describe("UnifiedApi", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("sends Idempotency-Key on mutations, not on reads", async () => {
    const calls: { method: string; idem: string | null }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        calls.push({
          method: init.method ?? "GET",
          idem:
            (init.headers as Record<string, string>)?.["idempotency-key"] ??
            null,
        });
        return new Response(JSON.stringify({ ok: true }), {
          status: init.method === "POST" ? 201 : 200,
        });
      }),
    );
    const api = new UnifiedApi({ baseUrl: "http://x" });
    await api.listApiKeys();
    await api.createApiKey("k");
    expect(calls[0].method).toBe("GET");
    expect(calls[0].idem).toBeNull();
    expect(calls[1].method).toBe("POST");
    expect(calls[1].idem).toMatch(/^ui_/);
  });

  it("decodes the canonical error body into ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              error: {
                code: "not_found",
                category: "resource",
                message: "no session",
                retryable: false,
                details: {},
                request_id: "req_9",
              },
            }),
            { status: 404 },
          ),
      ),
    );
    const api = new UnifiedApi({ baseUrl: "http://x" });
    await expect(api.getSession("sess_x")).rejects.toMatchObject({
      error: {
        code: "not_found",
        category: "resource",
        message: "no session",
        retryable: false,
        request_id: "req_9",
      },
    });
    const err = await api.getSession("sess_x").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(404);
  });

  it("sends Bearer token only when set", async () => {
    const auths: (string | null)[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_u: string, init: RequestInit) => {
        auths.push(
          (init.headers as Record<string, string>)?.authorization ?? null,
        );
        return new Response("{}", { status: 200 });
      }),
    );
    const api = new UnifiedApi({ baseUrl: "http://x" });
    await api.listApiKeys();
    api.setToken("tok_1");
    await api.listApiKeys();
    expect(auths).toEqual([null, "Bearer tok_1"]);
  });
});
