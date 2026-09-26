"""One-time maintainer handoff for registering the PUBLIC Sorenforge SBX
GitHub App (SOR-220 default connect path).

This is NOT the end-user install flow — it exists so a maintainer can
create the shared public App exactly once via GitHub's official App
Manifest registration, without ever touching the App's private key:

- ``GET /`` renders a page that auto-POSTs the manifest to GitHub
  (``settings/apps/new``) — public App, Contents + Pull requests RW,
  ``request_oauth_on_install: false``, ``setup_url`` pointed at the
  broker's future install callback.
- ``GET /callback`` receives GitHub's manifest ``code``, exchanges it
  server-side (``/app-manifests/{code}/conversions``), and writes the
  returned ``app_id`` / ``slug`` / ``pem`` straight into the Modal Secret
  ``sbx-broker-github-app`` — the PEM never leaves the broker boundary,
  is never rendered, logged, or copied by the maintainer.
- ``GET /__status`` exposes only non-secret posture for the operator.
- ``DELETE`` of this app after registration is the teardown; the Secret
  and the ``sbx-broker-registration`` Dict (metadata only) remain.

Run: ``modal deploy broker/register.py`` (Modal credentials required).

Note: no ``from __future__ import annotations`` here on purpose — the
FastAPI handlers inside ``web()`` annotate with locally-imported types
(``Request``), which postponed evaluation would fail to resolve.
"""

import secrets as _secrets
from typing import Any

import modal

APP_NAME = "sbx-github-app-register"
SECRET_NAME = "sbx-github-app-broker"
STATUS_DICT = "sbx-broker-registration"

# The org that owns the public App. A personal-account fallback button is
# rendered too — the manifest identical, only the GitHub endpoint differs.
OWNER_ORG = "soren-labs"
APP_DISPLAY_NAME = "Sorenforge SBX"
APP_HOMEPAGE = "https://github.com/soren-labs/sbx-browser"
# Where GitHub sends the browser after a user installs the App — the
# broker's install callback (recorded on the App at registration).
BROKER_SETUP_URL = "https://github-broker.sorenforge.com/v1/github/install/callback"

app = modal.App(APP_NAME)
image = modal.Image.debian_slim().pip_install("fastapi[standard]", "httpx")

# One nonce per deployment ties the GitHub callback to a page we rendered —
# a stray/foreign manifest code is never exchanged against our Secret lane.
_NONCE = _secrets.token_urlsafe(16)


def _manifest(redirect_url: str) -> dict[str, Any]:
    return {
        "name": APP_DISPLAY_NAME,
        "url": APP_HOMEPAGE,
        "redirect_url": redirect_url,
        "setup_url": BROKER_SETUP_URL,
        "setup_on_update": False,
        "public": True,
        "default_permissions": {
            "contents": "write",
            "pull_requests": "write",
        },
        "default_events": [],
        "request_oauth_on_install": False,
    }


@app.function(image=image)
@modal.asgi_app()
def web():
    import json
    from html import escape

    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse

    api = FastAPI(title="sbx public GitHub App registration (one-time)")

    def _status_dict():
        return modal.Dict.from_name(STATUS_DICT, create_if_missing=True)

    @api.get("/", response_class=HTMLResponse)
    def launcher(request: Request) -> HTMLResponse:
        callback = str(request.base_url).rstrip("/") + "/callback"
        manifest = _manifest(callback)
        manifest["state"] = _NONCE
        org_target = f"https://github.com/organizations/{OWNER_ORG}/settings/apps/new"
        personal_target = "https://github.com/settings/apps/new"
        payload = escape(json.dumps(manifest), quote=True)
        return HTMLResponse(
            f"""<!doctype html><html><head><meta charset="utf-8">
<title>Register the public SBX GitHub App</title>
<style>body{{font-family:system-ui;max-width:44rem;margin:4rem auto;padding:0 1rem}}
code{{background:#eee;padding:2px 5px;border-radius:4px}}small{{color:#666}}</style>
</head><body>
<h2>Register the public SBX GitHub App</h2>
<p>One click: this auto-POSTs the App Manifest to GitHub and creates the
<em>public</em> <b>{escape(APP_DISPLAY_NAME)}</b> App under
<code>{escape(OWNER_ORG)}</code> with Contents + Pull requests (read/write)
and no OAuth-on-install. The App's private key is exchanged server-side
into the broker Secret — it is never displayed or downloadable.</p>
<form id="f" method="post" action="{org_target}">
  <input type="hidden" name="manifest" value="{payload}">
</form>
<form id="p" method="post" action="{personal_target}">
  <input type="hidden" name="manifest" value="{payload}">
</form>
<noscript><p>JavaScript required.</p></noscript>
<p><small>If the auto-submit doesn't fire:
<button form="f">register under {escape(OWNER_ORG)}</button>
&nbsp;or&nbsp; <button form="p">register under your personal account</button>.</small></p>
<script>document.getElementById("f").submit();</script>
</body></html>"""
        )

    @api.get("/callback", response_class=HTMLResponse)
    def callback(code: str = "", state: str = "") -> HTMLResponse:
        if not code or state != _NONCE:
            return HTMLResponse(
                "<h2>Registration failed</h2><p>Missing or foreign state/code — "
                "start again from the launcher page.</p>",
                status_code=400,
            )
        try:
            resp = httpx.post(
                f"https://api.github.com/app-manifests/{code}/conversions",
                headers={"Accept": "application/vnd.github+json"},
                timeout=20.0,
            )
            data = resp.json() if resp.content else {}
        except Exception:
            return HTMLResponse(
                "<h2>Registration failed</h2><p>Could not reach GitHub.</p>",
                status_code=502,
            )
        if resp.status_code >= 400 or not data.get("pem") or not data.get("slug"):
            return HTMLResponse(
                "<h2>Registration failed</h2>"
                f"<p>GitHub manifest exchange returned HTTP {resp.status_code}.</p>",
                status_code=502,
            )
        # Private material goes straight into the broker Secret boundary —
        # client_secret/webhook_secret are not stored (no OAuth/webhooks).
        objects = modal.Secret.objects
        objects.delete(SECRET_NAME, allow_missing=True)
        objects.create(
            SECRET_NAME,
            env_dict={
                "SBX_BROKER_APP_ID": str(data["id"]),
                "SBX_BROKER_APP_SLUG": str(data["slug"]),
                "SBX_BROKER_APP_PRIVATE_KEY": str(data["pem"]),
            },
        )
        meta = {
            "status": "registered",
            "app_id": str(data["id"]),
            "app_slug": str(data["slug"]),
            "html_url": str(data.get("html_url") or ""),
        }
        _status_dict().put("registration", meta)
        slug = escape(str(data["slug"]))
        return HTMLResponse(
            f"""<h2>App registered: {slug}</h2>
<p>The private key is stored in the Modal Secret <code>{escape(SECRET_NAME)}</code>
(it was never shown to you — no copy is needed or possible).</p>
<p>User-facing install URL:
<code>https://github.com/apps/{slug}/installations/new</code></p>
<p>You can now take this registration app down.</p>"""
        )

    @api.get("/__status")
    def status() -> JSONResponse:
        try:
            meta = _status_dict().get("registration")
        except Exception:
            meta = None
        body = dict(meta) if isinstance(meta, dict) else {"status": "pending"}
        return JSONResponse(body)

    return api
