import type { EventEnvelope } from "../api/types";

/**
 * Opt-in latency trace for streamed parts. With `localStorage["sbx.streamTrace"] = "1"`
 * every part/tool event records when the runtime observed it, when the control plane
 * committed it, when this browser received it and when the frame containing it was
 * painted. Read it from `window.__sbxStreamTrace`. Off by default: no cost, no data.
 */
export interface StreamTraceEntry {
  seq: number;
  type: string;
  chars: number;
  /** ms since epoch; observed/recorded are server clocks, received/painted are the browser's. */
  observed: number | null;
  recorded: number | null;
  received: number;
  painted: number | null;
}

const LIMIT = 5000;

function enabled(): boolean {
  try {
    return typeof window !== "undefined" && window.localStorage.getItem("sbx.streamTrace") === "1";
  } catch {
    return false;
  }
}

const ms = (iso: unknown): number | null => {
  const value = typeof iso === "string" ? Date.parse(iso) : NaN;
  return Number.isFinite(value) ? value : null;
};

export function traceEvent(e: EventEnvelope): void {
  if (!/^(message\.part_|tool\.)/.test(e.type) || !enabled()) return;
  const store = ((window as unknown as { __sbxStreamTrace?: StreamTraceEntry[] }).__sbxStreamTrace ??= []);
  const entry: StreamTraceEntry = {
    seq: e.seq,
    type: e.type,
    chars: typeof e.payload?.content === "string" ? e.payload.content.length : 0,
    observed: ms(e.observed_at),
    recorded: ms(e.recorded_at),
    received: Date.now(),
    painted: null,
  };
  if (store.push(entry) > LIMIT) store.shift();
  // The frame after the next one is the first that can contain this event's DOM.
  requestAnimationFrame(() => requestAnimationFrame(() => (entry.painted = Date.now())));
}
