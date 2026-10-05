---
title: Authentication
description: Bearer keys, scopes, bootstrap/admin access and runtime-minted keys.
---

All public API calls use a Bearer key:

```http
Authorization: Bearer sbx_...
```

## Scopes

The main scopes are:

- **`agents`** — task/agent/run/revision/delivery/review operations;
- **`admin`** — administrative operations such as account verification and API-key management.

The bootstrap key created by `sbx deploy` is the durable admin credential for
the deployment. It is written locally to:

```text
~/.local/state/sbx/bootstrap.key
```

and backed by the deployment's bootstrap Secret.

## Verify a key

```bash
curl -fsS "$SBX_BASE_URL/v1/me" \
  -H "Authorization: Bearer $SBX_API_KEY"
```

The response identifies the calling key and its scopes without returning the
key itself.

## Minted API keys

Admins can mint narrower keys through the console or `/v1/api-keys`. The
plaintext token is shown once; the server stores only its hash.

Current v0.1.1 behavior: runtime-minted keys are held by the live control
plane and do **not** survive a control-plane restart/redeploy. Keep the
bootstrap admin key in a secure operator location so you can mint replacement
keys after an upgrade.

Do not embed long-lived keys in URLs, repository files, issue comments or
agent prompts.

## Console handoff

`sbx open` uses a one-time browser grant. The long-lived bootstrap key does
not need to appear in the browser URL.
