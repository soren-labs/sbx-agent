import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { mockHostedRequest } from "../hosted/mock";

beforeEach(() => {
  vi.stubEnv("VITE_HOSTED", "1");
  vi.stubEnv("VITE_API_MODE", "mock");
  localStorage.clear();
});
afterEach(() => { vi.unstubAllEnvs(); vi.restoreAllMocks(); });

it.each([
  ["0", "mock"], ["1", "http"], ["0", "http"],
])("gates mock state behind hosted=%s and mode=%s", async (hosted, mode) => {
  vi.stubEnv("VITE_HOSTED", hosted);
  vi.stubEnv("VITE_API_MODE", mode);
  await expect(mockHostedRequest("/hosted/connections/github", { token: "REDACTED" })).rejects.toThrow("disabled");
  expect(localStorage.length).toBe(0);
});

it("simulates manual connect, replace, validate and disconnect without persisting secrets", async () => {
  await mockHostedRequest("/auth/login", { email: "demo@example.test", password: "REDACTED" });
  for (const [provider, body] of [
    ["github", { token: "REDACTED" }],
    ["opencode", { api_key: "REDACTED" }],
    ["modal", { token_id: "REDACTED", token_secret: "REDACTED" }],
  ] as const) {
    const path = `/hosted/connections/${provider}`;
    expect((await mockHostedRequest(path)).connection).toBeNull();
    await mockHostedRequest(path, body);
    await mockHostedRequest(path, body);
    await mockHostedRequest(`${path}/${provider === "modal" ? "provision" : "validate"}`, {});
    const status = await mockHostedRequest(path);
    expect(status.connection.state).toBe(provider === "modal" ? "ready" : "connected");
    if (provider === "github") expect((await mockHostedRequest("/hosted/repositories")).repositories).not.toHaveLength(0);
    expect(localStorage.getItem(localStorage.key(0)!)).not.toContain("REDACTED");
    await mockHostedRequest(path, {}, "DELETE");
    expect((await mockHostedRequest(path)).connection.state).toBe("disabled");
    if (provider === "github") expect((await mockHostedRequest("/hosted/repositories")).repositories).toEqual([]);
  }
  expect((await mockHostedRequest("/hosted/connections/codex")).connection).toBeNull();
}, 15000);

it("uses HTTP outside explicit mock mode and preserves actionable Modal 409 guidance", async () => {
  vi.stubEnv("VITE_API_MODE", "http");
  vi.resetModules();
  const { hostedRequest } = await import("../hosted/api");
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
    error: "modal_resources_require_cleanup_before_disconnect",
  }), { status: 409 }));
  await expect(hostedRequest("/hosted/connections/modal", {}, "DELETE")).rejects.toThrow("Close your Sessions");
  expect(fetch).toHaveBeenCalledOnce();
  expect(localStorage.length).toBe(0);
});

it("offers only connected Zen models in the hosted mock composer", async () => {
  vi.resetModules();
  const { PrototypeSessionApi } = await import("../prototype/demo");
  const api = new PrototypeSessionApi();
  expect(await api.listModels()).toEqual([]);
  await mockHostedRequest("/hosted/connections/opencode", { api_key: "REDACTED" });
  expect(await api.listModels()).toEqual([expect.objectContaining({ provider: "opencode", model: "opencode/space-bunny-free" })]);
  expect(await api.listProviders()).toEqual([expect.objectContaining({ id: "opencode", readiness: "ready", accountsAvailable: 1 })]);
  await mockHostedRequest("/hosted/connections/opencode", {}, "DELETE");
  expect(await api.listModels()).toEqual([]);
});
