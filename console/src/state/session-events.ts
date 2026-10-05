import type { EventEnvelope, Message, MessagePart } from "../api/types";

/** Which server projections an applied event invalidates (refetched, never guessed). */
export interface Dirty {
  session: boolean;
  messages: boolean;
  turns: boolean;
  changes: boolean;
  deliveries: boolean;
  delegations: boolean;
  services: boolean;
  executor: boolean;
}

export const CLEAN: Dirty = {
  session: false,
  messages: false,
  turns: false,
  changes: false,
  deliveries: false,
  delegations: false,
  services: false,
  executor: false,
};

export interface LiveState {
  messages: Message[];
  /** Watermark of the snapshot the state was seeded from. */
  watermark: number;
  /** Highest committed seq applied; replay resumes after it. */
  lastSeq: number;
  /** Bounded recent events for the Activity feed. */
  events: EventEnvelope[];
  /** A gap/reset was detected: the owner must fetch a fresh snapshot. */
  needsResync: boolean;
  /** lastSeq at which the last resync happened (guards resync loops). */
  resyncedAt: number | null;
  dirty: Dirty;
}

export const MAX_EVENTS = 500;

export const initialLiveState = (): LiveState => ({
  messages: [],
  watermark: 0,
  lastSeq: 0,
  events: [],
  needsResync: false,
  resyncedAt: null,
  dirty: { ...CLEAN },
});

export type LiveAction =
  /** Authoritative snapshot (+ its watermark): replaces messages, resets cursor. */
  | { type: "snapshot"; messages: Message[]; watermark: number }
  /** Refresh of messages while streaming: merged by revision, cursor untouched. */
  | { type: "merge_messages"; messages: Message[] }
  | { type: "events"; items: EventEnvelope[] }
  | { type: "clear_dirty"; keys: (keyof Dirty)[] };

const sortMessages = (m: Message[]) => [...m].sort((a, b) => a.ordinal - b.ordinal);

function mergePart(a: MessagePart | undefined, b: MessagePart): MessagePart {
  if (!a) return b;
  if (a.sealed && !b.sealed) return a;
  return b.revision >= a.revision ? b : a;
}

function mergeMessage(local: Message | undefined, server: Message): Message {
  if (!local) return server;
  const parts = new Map(local.parts.map((p) => [p.key, p]));
  for (const p of server.parts) parts.set(p.key, mergePart(parts.get(p.key), p));
  return { ...server, parts: [...parts.values()] };
}

const EVENT_DIRTY: [RegExp, (keyof Dirty)[]][] = [
  [/^message\.(accepted|routed|completed)$/, ["messages", "session"]],
  [/^turn\./, ["turns", "session", "messages"]],
  [/^(session|execution|usage)\./, ["session"]],
  [/^(executor|worktree|snapshot)\./, ["session", "executor", "changes"]],
  [/^changeset\./, ["changes"]],
  [/^delivery\./, ["deliveries", "changes"]],
  [/^delegation\./, ["delegations"]],
  [/^service\./, ["services"]],
];

function applyPart(messages: Message[], e: EventEnvelope, partDirty: { unknown: boolean }): Message[] {
  const p = e.payload ?? {};
  const messageId = typeof p.message_id === "string" ? p.message_id : null;
  if (!messageId) return messages;

  let key: string;
  let kind: string;
  let revision: number | null = null;
  let content = "";
  let data: Record<string, unknown> | undefined;
  let append = false;
  if (e.type.startsWith("tool.")) {
    const toolId = String(p.tool_id ?? "");
    if (!toolId) return messages;
    key = `tool:${toolId}`;
    kind = "tool";
    data = Object.fromEntries(
      ["tool_id", "name", "status", "title", "input", "output", "error"]
        .filter((k) => p[k] !== undefined && p[k] !== null)
        .map((k) => [k, p[k]]),
    );
  } else {
    if (typeof p.part_key !== "string") return messages;
    key = p.part_key;
    kind = typeof p.kind === "string" ? p.kind : "text";
    revision = typeof p.revision === "number" ? p.revision : 1;
    content = typeof p.content === "string" ? p.content : "";
    append = p.mode === "append";
  }

  let msg = messages.find((m) => m.id === messageId);
  let next = messages;
  if (!msg) {
    partDirty.unknown = true;
    const ordinal = messages.reduce((n, m) => Math.max(n, m.ordinal), 0) + 1;
    msg = {
      id: messageId,
      ordinal,
      role: "assistant",
      content: [],
      turn_id: e.turn_id ?? null,
      state: "streaming",
      parts: [],
    };
    next = [...messages, msg];
  }
  const existing = msg.parts.find((x) => x.key === key);
  let part: MessagePart;
  if (!existing) {
    part = { key, kind, revision: revision ?? 1, content, data, sealed: false };
  } else {
    if (existing.sealed) return messages;
    const nextRev = revision ?? existing.revision + 1;
    // Replayed/stale revision: never apply twice (cumulative text must not duplicate).
    if (nextRev <= existing.revision) return messages;
    part =
      kind === "tool"
        ? { ...existing, revision: nextRev, data: { ...(existing.data ?? {}), ...(data ?? {}) } }
        : { ...existing, revision: nextRev, content: append ? existing.content + content : content };
  }
  const parts = existing ? msg.parts.map((x) => (x.key === key ? part : x)) : [...msg.parts, part];
  const updated: Message = { ...msg, parts };
  return next.map((m) => (m.id === messageId ? updated : m));
}

export function liveReducer(state: LiveState, action: LiveAction): LiveState {
  switch (action.type) {
    case "snapshot":
      return {
        ...state,
        messages: sortMessages(action.messages),
        watermark: action.watermark,
        lastSeq: action.watermark,
        needsResync: false,
        resyncedAt: state.needsResync ? action.watermark : state.resyncedAt,
        events: state.events.filter((e) => e.seq <= action.watermark),
      };
    case "merge_messages": {
      const byId = new Map(state.messages.map((m) => [m.id, m]));
      for (const m of action.messages) byId.set(m.id, mergeMessage(byId.get(m.id), m));
      return { ...state, messages: sortMessages([...byId.values()]) };
    }
    case "clear_dirty": {
      const dirty = { ...state.dirty };
      for (const k of action.keys) dirty[k] = false;
      return { ...state, dirty };
    }
    case "events": {
      if (state.needsResync) return state;
      let s = state;
      const ordered = [...action.items].sort((a, b) => a.seq - b.seq);
      const seen = new Set(s.events.map((e) => e.id));
      const dirty = { ...s.dirty };
      let messages = s.messages;
      let lastSeq = s.lastSeq;
      const events = [...s.events];
      const partDirty = { unknown: false };
      for (const e of ordered) {
        if (e.seq <= lastSeq || seen.has(e.id)) continue; // dedupe by seq / id
        if (e.seq > lastSeq + 1 && s.resyncedAt !== lastSeq) {
          // Gap in the committed sequence: stop and ask for a fresh snapshot.
          return { ...s, messages, lastSeq, events, dirty, needsResync: true };
        }
        lastSeq = e.seq;
        seen.add(e.id);
        events.push(e);
        if (/^(message\.part_|tool\.)/.test(e.type)) {
          messages = applyPart(messages, e, partDirty);
        } else {
          for (const [re, keys] of EVENT_DIRTY) {
            if (re.test(e.type)) for (const k of keys) dirty[k] = true;
          }
        }
      }
      if (partDirty.unknown) dirty.messages = true;
      s = {
        ...s,
        messages,
        lastSeq,
        events: events.length > MAX_EVENTS ? events.slice(-MAX_EVENTS) : events,
        dirty,
      };
      return s;
    }
  }
}

/** Text a Message renders as: parts when present, else its authored content. */
export function messageText(m: Message): string {
  const parts = m.parts.filter((p) => p.kind === "text");
  if (parts.length) return parts.map((p) => p.content).join("\n\n");
  return m.content.map((c) => c.text ?? "").join("\n\n");
}
