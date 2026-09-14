/**
 * Public edge for sbx.sorenforge.com.
 *
 * /api/*  → Modal sbx-control with HTTP Basic (secrets stay on the Worker)
 * /v1/*   → Modal sbx-control public API; the client's
 *           `Authorization: Bearer sbx_<key>` is passed through untouched —
 *           the Worker never injects credentials. SSE bodies stream unbuffered.
 * everything else → Worker static assets (`web/`), with WEB_ORIGIN as fallback
 *
 * Strips WWW-Authenticate on /api/* so the SPA can show a banner instead of a
 * browser login dialog if upstream auth is misconfigured.
 */

const HOP = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailers",
  "transfer-encoding",
  "upgrade",
  "host",
  "cf-connecting-ip",
  "cf-ipcountry",
  "cf-ray",
  "cf-visitor",
  "x-forwarded-for",
  "x-forwarded-proto",
]);

function filterHeaders(headers) {
  const out = new Headers();
  for (const [k, v] of headers.entries()) {
    if (HOP.has(k.toLowerCase())) continue;
    out.set(k, v);
  }
  return out;
}

function basicToken(user, pass) {
  const bytes = new TextEncoder().encode(`${user}:${pass}`);
  let bin = "";
  bytes.forEach((b) => {
    bin += String.fromCharCode(b);
  });
  return btoa(bin);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const control = (env.SBX_CONTROL_URL || "").replace(/\/+$/, "");
    const web = (env.WEB_ORIGIN || "").replace(/\/+$/, "");
    const user = env.SBX_BASIC_USER || "";
    const pass = env.SBX_BASIC_PASS || "";

    const isApi = url.pathname === "/api" || url.pathname.startsWith("/api/");
    const isV1 = url.pathname === "/v1" || url.pathname.startsWith("/v1/");
    if (isApi || isV1) {
      if (!control || (isApi && (!user || !pass))) {
        return Response.json({ error: "proxy_unconfigured", code: 500 }, { status: 500 });
      }
      const dest = `${control}${url.pathname}${url.search}`;
      const headers = filterHeaders(request.headers);
      if (isApi) {
        headers.set("Authorization", `Basic ${basicToken(user, pass)}`);
      }
      // /v1: client `Authorization` passes through as-is; Basic is never
      // injected. `Last-Event-ID` and other SSE headers are already kept by
      // filterHeaders, and `upstream.body` is streamed without buffering.
      const init = {
        method: request.method,
        headers,
        redirect: "manual",
      };
      if (request.method !== "GET" && request.method !== "HEAD") {
        init.body = request.body;
      }
      const upstream = await fetch(dest, init);
      const outHeaders = filterHeaders(upstream.headers);
      if (isApi) {
        outHeaders.delete("www-authenticate");
      }
      return new Response(upstream.body, {
        status: upstream.status,
        statusText: upstream.statusText,
        headers: outHeaders,
      });
    }

    if (env.ASSETS) {
      return env.ASSETS.fetch(request);
    }
    if (!web) {
      return Response.json({ error: "web_origin_unconfigured", code: 500 }, { status: 500 });
    }
    const dest = `${web}${url.pathname}${url.search}`;
    const headers = filterHeaders(request.headers);
    const init = {
      method: request.method,
      headers,
      redirect: "manual",
    };
    if (request.method !== "GET" && request.method !== "HEAD") {
      init.body = request.body;
    }
    return fetch(dest, init);
  },
};
