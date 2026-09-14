# sbx-edge

Cloudflare Worker in front of `sbx.sorenforge.com`:

- `/api/*` → Modal `sbx-control` with HTTP Basic from Worker secrets
- other paths → static files from `web/` (`[assets] directory = "../../web"`)
- `WEB_ORIGIN` is a Vercel fallback if assets are missing

Secrets (`SBX_BASIC_USER`, `SBX_BASIC_PASS`) are `wrangler secret put` only. Do not commit them.
