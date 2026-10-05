import { it, expect, vi, afterEach } from "vitest";
import { Client } from "./client";
afterEach(() => vi.unstubAllGlobals());
it("network uncertainty reuses the exact mutation body and key", async () => {
  const fetch = vi
    .fn()
    .mockRejectedValueOnce(new TypeError("lost"))
    .mockResolvedValueOnce({
      ok: true,
      json: async () => ({ turn_id: "turn_one" }),
    });
  vi.stubGlobal("fetch", fetch);
  const result = await new Client().request(
    "/api/sessions/s/messages",
    "POST",
    { content: "work" },
    "stable",
  );
  expect(result).toEqual({ turn_id: "turn_one" });
  expect(fetch.mock.calls[0][1]).toEqual(fetch.mock.calls[1][1]);
  expect(fetch.mock.calls[0][1].headers["Idempotency-Key"]).toBe("stable");
});
