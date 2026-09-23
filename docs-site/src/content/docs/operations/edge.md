---
title: Optional Edge Worker
description: Cloudflare Worker for static assets and `/v1` proxying.
---

## Why an edge?

The optional Cloudflare Worker (`deploy/sbx-edge`) serves three purposes:

1. **Static assets** — hosts the web console (`web/`) as static files
2. **Same-origin proxy** — forwards `/v1/*` to the control plane (bypasses CORS)
3. **Auth injection** — passes `Authorization` headers through

The control plane does **not** send CORS headers (intentionally), so same-origin serving is required. Without the Worker, the web console can only run on `localhost` with a dev server.

## Deployment

### Prerequisites

- [Wrangler CLI](https://developers.cloudflare.com/workers/wrangler/install-and-update/) (Cloudflare Workers CLI)
- Cloudflare account and a domain or workers.dev subdomain

### Steps

1. **Build the console:**
   ```bash
   cd web
   npm run build
   ```

2. **Configure wrangler:**
   ```bash
   cd deploy/sbx-edge
   # Edit wrangler.toml: set name, account_id, routes, etc.
   ```

3. **Deploy:**
   ```bash
   wrangler deploy
   ```

4. **Set secrets for HTTP Basic auth (optional):**
   ```bash
   wrangler secret put SBX_BASIC_USER
   # When prompted, enter the username (e.g., sbx)
   wrangler secret put SBX_BASIC_PASS
   # When prompted, enter the password (e.g., sbx)
   ```

### Configuration

Edit `wrangler.toml` to set your deployment details:

```toml
name = "sbx-edge"
account_id = "your-account-id"

[vars]
SBX_CONTROL_URL = "https://your-sbx-control.modal.run"

[[routes]]
pattern = "sbx.example.com"
custom_domain = true
```

The Secrets (`SBX_BASIC_USER`, `SBX_BASIC_PASS`) are never committed to the file; use `wrangler secret put` to set them instead.

## How it works

### `/v1/*` proxying

The Worker intercepts `POST /v1/agents`, `GET /v1/agents/{id}/runs/{runId}/stream`, etc. and forwards them to the control plane:

```javascript
// Pseudo-code
if (request.url.includes("/v1/")) {
  const controlUrl = env.SBX_CONTROL_URL;
  const response = await fetch(`${controlUrl}${path}`, {
    method: request.method,
    headers: request.headers,  // Authorization passed through
    body: request.body,
  });
  return response;
}
```

The Bearer token (`Authorization: Bearer sbx_<key>`) is passed through untouched.

### Static assets

The Worker also serves the built web console (`web/dist`):

```javascript
if (request.url.endsWith("/")) {
  return new Response(/* index.html */);
}
// CSS, JS, etc. from the build output
```

### Streaming

SSE streams (`/v1/.../stream`) are forwarded unbuffered (no compression), so `Last-Event-ID` reconnection works.

## Accessing the console

Once deployed:

```
https://sbx.example.com/
```

The console (running on the Worker) will:
1. Send API requests to the same origin (`/v1/*`)
2. The Worker proxies them to the control plane
3. Results flow back through the Worker to the console

## Alternatives (no Worker)

If you don't want to use a Cloudflare Worker:

1. **Local dev server** (`web/dev.server`):
   ```bash
   cd web
   npm run dev
   # Open http://localhost:5173
   # Set API_BASE_URL to your control plane
   ```

2. **Any reverse proxy** (nginx, Apache, etc.):
   ```nginx
   location /v1/ {
     proxy_pass https://sbx-control.example.com;
     proxy_pass_header Authorization;
     # ... other proxy directives
   }
   ```

3. **No console** — use curl or the Python client instead.

## Security considerations

- **No credentials in the Worker** — the Bearer token is passed from the browser and forwarded untouched
- **Same-origin only** — the control plane doesn't send CORS headers; cross-origin requests fail
- **HTTPS required** — the Worker runs on HTTPS by default; never expose over HTTP
- **Rate limiting** — Cloudflare Workers has built-in DDoS protection
EOFDEDGE
