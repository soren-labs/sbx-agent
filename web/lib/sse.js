import { apiUrl, authHeaders } from "./api.js";

/**
 * Authenticated SSE reader for `/v1/.../stream`.
 *
 * EventSource cannot send `Authorization`, so this follows the EventSource
 * algorithm over `fetch`: honours `retry:`, tracks the last `id`, and
 * reconnects with `Last-Event-ID`. Reconnects are bounded — after
 * `maxFailures` consecutive failures the caller falls back to polling the
 * durable run record (the ledger is the source of truth).
 */

function parseFrame(raw) {
  const frame = { event: "message", data: "", id: null, retry: null };
  const data = [];
  for (const line of raw.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const idx = line.indexOf(":");
    const field = idx === -1 ? line : line.slice(0, idx);
    let value = idx === -1 ? "" : line.slice(idx + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") frame.event = value;
    else if (field === "data") data.push(value);
    else if (field === "id") frame.id = value;
    else if (field === "retry") {
      const n = Number.parseInt(value, 10);
      if (Number.isFinite(n)) frame.retry = n;
    }
  }
  frame.data = data.join("\n");
  return frame;
}

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(new DOMException("aborted", "AbortError"));
      },
      { once: true },
    );
  });
}

export function openStream(
  path,
  { onEvent, onStatus, lastEventId = "", maxFailures = 6, retryMs = 1000 } = {},
) {
  const controller = new AbortController();
  let lastId = lastEventId;
  let failures = 0;
  let closed = false;

  const status = (value, detail) => onStatus?.(value, detail);

  const run = async () => {
    while (!closed) {
      status(failures ? "reconnecting" : "connecting");
      try {
        const headers = { Accept: "text/event-stream", ...authHeaders() };
        if (lastId) headers["Last-Event-ID"] = lastId;
        const res = await fetch(apiUrl(path), {
          headers,
          signal: controller.signal,
          cache: "no-store",
        });
        if (res.status === 401 || res.status === 403 || res.status === 404) {
          status("closed", { status: res.status });
          return;
        }
        if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
        failures = 0;
        status("live");
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        while (!closed) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true }).replace(/\r\n?/g, "\n");
          let sep = buf.indexOf("\n\n");
          while (sep !== -1) {
            const frame = parseFrame(buf.slice(0, sep));
            buf = buf.slice(sep + 2);
            if (frame.retry != null) retryMs = frame.retry;
            if (frame.id) lastId = frame.id;
            if (frame.data) {
              let payload = null;
              try {
                payload = JSON.parse(frame.data);
              } catch {
                payload = { type: "sbx.unparsed", raw: frame.data };
              }
              onEvent?.({ id: frame.id, type: payload?.type || frame.event, data: payload });
            }
            if (closed) return;
            sep = buf.indexOf("\n\n");
          }
        }
      } catch (err) {
        if (closed || err?.name === "AbortError") return;
      }
      if (closed) return;
      failures += 1;
      if (failures >= maxFailures) {
        status("gave_up");
        return;
      }
      try {
        await sleep(Math.min(retryMs * failures, 8000), controller.signal);
      } catch {
        return;
      }
    }
  };

  void run();
  return {
    close() {
      if (closed) return;
      closed = true;
      controller.abort();
      status("closed");
    },
    get lastEventId() {
      return lastId;
    },
  };
}
