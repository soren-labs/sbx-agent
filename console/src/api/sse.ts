import type { EventEnvelope } from "./types";

/** Incremental `text/event-stream` parser; yields data payloads of complete events. */
export class SseParser {
  private buf = "";
  private data: string[] = [];

  push(chunk: string): string[] {
    this.buf += chunk;
    const out: string[] = [];
    let idx: number;
    while ((idx = this.buf.search(/\r?\n/)) >= 0) {
      const line = this.buf.slice(0, idx);
      this.buf = this.buf.slice(idx + (this.buf[idx] === "\r" ? 2 : 1));
      if (line === "") {
        if (this.data.length) out.push(this.data.join("\n"));
        this.data = [];
      } else if (line.startsWith("data:")) {
        this.data.push(line.slice(5).replace(/^ /, ""));
      }
    }
    return out;
  }
}

export function parseEnvelope(raw: string): EventEnvelope | null {
  try {
    const v = JSON.parse(raw) as EventEnvelope;
    return typeof v?.seq === "number" && typeof v.type === "string" ? v : null;
  } catch {
    return null;
  }
}
