import { describe, it, expect } from "vitest";
import { apply, initial, Cache } from "./session";
import type { Session, Event } from "../api/types";
const session = { event_watermark: 3 } as Session;
const event = {
  id: "e4",
  seq: 4,
  type: "message.part_replaced",
  schema_version: 1,
  recorded_at: "2026-10-05T00:00:00Z",
  payload: { text: "whole text", revision: 2 },
} as Event;
describe("committed history", () => {
  it("dedupes reconnect and asks for a snapshot on gaps or schema changes", () => {
    const once = apply(initial(session), event);
    expect(apply(once, event)).toBe(once);
    expect(apply(once, { ...event, seq: 6 }).resetRequired).toBe(true);
    expect(
      apply(once, { ...event, seq: 5, schema_version: 2 }).resetRequired,
    ).toBe(true);
    expect(once.events).toHaveLength(1);
  });
  it("purges cache on logout and owner change", () => {
    const cache = new Cache();
    cache.setOwner("one");
    cache.set("s", initial(session));
    cache.setOwner("two");
    expect(cache.get("s")).toBeUndefined();
    cache.set("s", initial(session));
    cache.clear();
    expect(cache.get("s")).toBeUndefined();
  });
});
