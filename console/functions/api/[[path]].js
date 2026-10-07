const DEFAULT_API_ORIGIN = "https://api.sbx-agent.com";

export async function onRequest({ request, env }) {
  const upstreamOrigin = env.SBX_API_ORIGIN || DEFAULT_API_ORIGIN;
  const incoming = new URL(request.url);
  const upstreamPath =
    incoming.pathname === "/api/readyz"
      ? "/readyz"
      : incoming.pathname === "/api/healthz"
        ? "/healthz"
        : incoming.pathname;
  const upstream = new URL(`${upstreamPath}${incoming.search}`, upstreamOrigin);

  const headers = new Headers(request.headers);
  headers.delete("host");

  const upstreamRequest = new Request(upstream, {
    method: request.method,
    headers,
    body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
    redirect: "manual",
  });

  const response = await fetch(upstreamRequest);
  const out = new Response(response.body, response);
  out.headers.set("X-SBX-Proxy", "cloudflare-pages");
  return out;
}
