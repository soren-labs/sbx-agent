import { apiUrl, authHeader } from "./config.js";

/**
 * Authenticated SSE reader.
 *
 * Native `EventSource` cannot set `Authorization` (and Chromium strips
 * `user:pass@` from the URL), so we follow the EventSource algorithm with
 * `fetch`: honor `retry:`, persist last id, reconnect with `Last-Event-ID`.
 */

function parseFrame(raw) {
  const frame = { event: "message", data: "", id: "", retry: null, comment: false };
  const lines = raw.split(/\r?\n/);
  const dataLines = [];
  for (const line of lines) {
    if (line === "" || line.startsWith(":")) {
      if (line.startsWith(":")) frame.comment = true;
      continue;
    }
    const idx = line.indexOf(":");
    const field = idx === -1 ? line : line.slice(0, idx);
    let value = idx === -1 ? "" : line.slice(idx + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") frame.event = value;
    else if (field === "data") dataLines.push(value);
    else if (field === "id") frame.id = value;
    else if (field === "retry") {
      const n = Number.parseInt(value, 10);
      if (Number.isFinite(n)) frame.retry = n;
    }
  }
  frame.data = dataLines.join("\n");
  return frame;
}

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    if (signal) {
      signal.addEventListener(
        "abort",
        () => {
          clearTimeout(t);
          reject(new DOMException("aborted", "AbortError"));
        },
        { once: true },
      );
    }
  });
}

export function subscribeSessionEvents(sessionId, { onEvent, onOpen, onError } = {}) {
  const abort = new AbortController();
  let lastId = "";
  let retryMs = 300;
  let stopped = false;

  const run = async () => {
    while (!stopped) {
      try {
        const headers = {
          Accept: "text/event-stream",
          ...authHeader(),
        };
        if (lastId) headers["Last-Event-ID"] = lastId;
        const res = await fetch(apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/events`), {
          headers,
          signal: abort.signal,
          cache: "no-store",
        });
        if (!res.ok) {
          onError?.(res.status, 2);
          await sleep(retryMs, abort.signal);
          continue;
        }
        if (!res.body) {
          onError?.("empty-body", 2);
          await sleep(retryMs, abort.signal);
          continue;
        }
        onOpen?.();
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        while (!stopped) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          buf = buf.replace(/\r\n/g, "\n");
          let sep;
          while ((sep = buf.indexOf("\n\n")) !== -1) {
            const raw = buf.slice(0, sep);
            buf = buf.slice(sep + 2);
            const frame = parseFrame(raw);
            if (frame.retry != null) retryMs = frame.retry;
            if (frame.id) lastId = frame.id;
            if (!frame.data) continue;
            let payload;
            try {
              payload = JSON.parse(frame.data);
            } catch {
              continue;
            }
            onEvent?.({
              id: frame.id || lastId,
              type: payload.type || frame.event,
              data: payload,
            });
          }
        }
        onError?.("disconnect", 0);
      } catch (err) {
        if (stopped || abort.signal.aborted) return;
        onError?.(err, 0);
      }
      if (stopped) return;
      try {
        await sleep(retryMs, abort.signal);
      } catch {
        return;
      }
    }
  };

  void run();
  return () => {
    stopped = true;
    abort.abort();
  };
}
