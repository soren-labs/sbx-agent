import { describe, expect, it } from "vitest";
import type { EventEnvelope, Message } from "../api/types";
import { initialLiveState, liveReducer, messageText, type LiveState } from "../state/session-events";
import { SessionLive } from "../state/session-live";

let id = 0;
const ev = (seq: number, type: string, payload: Record<string, unknown> = {}): EventEnvelope => ({ id: `e${seq}-${++id}`, seq, type, payload });
const part = (seq: number, revision: number, content: string, key = "p1"): EventEnvelope =>
  ev(seq, revision === 1 ? "message.part_added" : "message.part_updated", { message_id: "m1", part_key: key, kind: "text", revision, mode: "replace", content });
const msg = (over: Partial<Message> = {}): Message => ({ id: "m1", ordinal: 2, role: "assistant", content: [], turn_id: "t1", state: "streaming", parts: [], ...over });
const seeded = (messages: Message[] = [msg()], watermark = 0): LiveState => liveReducer(initialLiveState(), { type: "snapshot", messages, watermark });
const apply = (s: LiveState, ...items: EventEnvelope[]) => liveReducer(s, { type: "events", items });
const text = (s: LiveState) => messageText(s.messages.find((m) => m.id === "m1")!);

describe("event reducer", () => {
  it("dedupes by seq and event id when events are replayed", () => {
    let s = seeded();
    s = apply(s, ev(1, "turn.started"), ev(2, "turn.queued"));
    const replay = apply(s, ev(2, "turn.queued"), ev(3, "turn.succeeded"));
    expect(replay.events.map((e) => e.seq)).toEqual([1, 2, 3]);
    expect(replay.lastSeq).toBe(3);
    const same = ev(3, "turn.started");
    expect(apply(apply(s, same), same).events.filter((e) => e.id === same.id)).toHaveLength(1);
  });

  it("replaces message parts by revision and never appends cumulative text twice", () => {
    let s = seeded();
    s = apply(s, part(1, 1, "Hel"), part(2, 2, "Hello"));
    expect(text(s)).toBe("Hello");
    // replay of an already-applied revision (new seq/id, same revision) is ignored
    s = apply(s, part(3, 2, "Hello"));
    expect(text(s)).toBe("Hello");
    // stale revision is ignored; newer replaces
    s = apply(s, part(4, 1, "Hel"), part(5, 3, "Hello world"));
    expect(text(s)).toBe("Hello world");
    expect(s.messages[0].parts).toHaveLength(1);
  });

  it("does not regress a part when a snapshot already contains a later revision", () => {
    const snap = msg({ parts: [{ key: "p1", kind: "text", revision: 3, content: "Hello world" }] });
    let s = seeded([snap], 5);
    s = apply(s, part(6, 2, "Hello")); // stale revision arriving after snapshot
    expect(text(s)).toBe("Hello world");
    expect(s.lastSeq).toBe(6);
  });

  it("merges refreshed messages by highest revision", () => {
    let s = seeded([msg({ parts: [{ key: "p1", kind: "text", revision: 4, content: "newer" }] })]);
    s = liveReducer(s, { type: "merge_messages", messages: [msg({ parts: [{ key: "p1", kind: "text", revision: 2, content: "older" }] })] });
    expect(text(s)).toBe("newer");
  });

  it("flags a gap as a resync signal and stops applying", () => {
    let s = seeded([msg()], 10);
    s = apply(s, ev(11, "turn.started"), ev(13, "turn.succeeded"));
    expect(s.needsResync).toBe(true);
    expect(s.lastSeq).toBe(11);
    expect(s.events.map((e) => e.seq)).toEqual([11]);
    expect(apply(s, ev(12, "turn.queued")).events).toHaveLength(1); // frozen until resync

    s = liveReducer(s, { type: "snapshot", messages: [msg()], watermark: 14 });
    expect(s.needsResync).toBe(false);
    expect(s.lastSeq).toBe(14);
    s = apply(s, ev(15, "turn.started"));
    expect(s.lastSeq).toBe(15);
  });

  it("accepts a persistent hole after one resync instead of looping", () => {
    let s = seeded([msg()], 0);
    s = apply(s, ev(2, "turn.started"));
    expect(s.needsResync).toBe(true);
    s = liveReducer(s, { type: "snapshot", messages: [msg()], watermark: 0 });
    s = apply(s, ev(2, "turn.started"));
    expect(s.needsResync).toBe(false);
    expect(s.lastSeq).toBe(2);
  });

  it("creates a placeholder for unknown messages and requests a refetch", () => {
    const s = apply(seeded([], 0), ev(1, "message.part_added", { message_id: "mx", part_key: "p", kind: "text", revision: 1, content: "hi" }));
    expect(s.messages[0].id).toBe("mx");
    expect(s.dirty.messages).toBe(true);
  });

  it("applies tool events as one revisioned part and marks projections dirty", () => {
    let s = seeded();
    s = apply(s, ev(1, "tool.started", { message_id: "m1", tool_id: "c1", name: "bash", status: "running" }), ev(2, "tool.completed", { message_id: "m1", tool_id: "c1", status: "completed", output: "ok" }));
    const tool = s.messages[0].parts[0];
    expect(tool.kind).toBe("tool");
    expect(tool.data).toMatchObject({ name: "bash", status: "completed", output: "ok" });
    s = apply(s, ev(3, "turn.succeeded"), ev(4, "delivery.progressed"));
    expect(s.dirty).toMatchObject({ turns: true, session: true, deliveries: true });
  });
});

describe("SessionLive", () => {
  it("reconnects from the last applied seq without creating a Turn", async () => {
    const afters: number[] = [];
    let attempt = 0;
    const sendMessage = () => {
      throw new Error("must not POST on reconnect");
    };
    const api = {
      sessions: {
        get: async () => ({ session: { id: "s1", actions: [] }, event_watermark: 0 }),
        messages: async () => ({ items: [], event_watermark: 0 }),
        turns: async () => ({ items: [] }),
        executor: async () => ({}),
        sendMessage,
        streamEvents: async (_s: string, o: { after: number; signal?: AbortSignal; onEvent: (e: EventEnvelope) => void }) => {
          afters.push(o.after);
          if (attempt++ === 0) {
            o.onEvent(ev(1, "turn.started"));
            o.onEvent(ev(2, "turn.queued"));
            throw new Error("connection dropped");
          }
          await new Promise<void>((resolve) => o.signal?.addEventListener("abort", () => resolve()));
        },
      },
    };
    const live = new SessionLive(api as never, "s1", { reconnectMs: 5, refreshDebounceMs: 1000 });
    live.start();
    await vi_waitFor(() => afters.length >= 2);
    live.stop();
    expect(afters).toEqual([0, 2]);
    expect(live.getState().lastSeq).toBe(2);
  });

  it("refetches the snapshot on invalid_cursor", async () => {
    let snapshots = 0;
    let attempt = 0;
    const api = {
      sessions: {
        get: async () => ({ session: { id: "s1", actions: [] }, event_watermark: 0 }),
        messages: async () => ({ items: [], event_watermark: ++snapshots * 10 }),
        turns: async () => ({ items: [] }),
        executor: async () => ({}),
        streamEvents: async (_s: string, o: { after: number; signal?: AbortSignal }) => {
          if (attempt++ === 0) {
            const { ApiError } = await import("../api/errors");
            throw new ApiError({ status: 409, code: "history_reset_required" });
          }
          await new Promise<void>((resolve) => o.signal?.addEventListener("abort", () => resolve()));
        },
      },
    };
    const live = new SessionLive(api as never, "s1", { reconnectMs: 5 });
    live.start();
    await vi_waitFor(() => attempt >= 2);
    live.stop();
    expect(snapshots).toBe(2);
    expect(live.getState().lastSeq).toBe(20);
  });
});

async function vi_waitFor(cond: () => boolean, ms = 2000) {
  const start = Date.now();
  while (!cond()) {
    if (Date.now() - start > ms) throw new Error("timeout");
    await new Promise((r) => setTimeout(r, 5));
  }
}
