---
title: Custom domains
description: Put the control plane/console and documentation behind stable public hostnames.
---

A deployment works on its native Modal URLs. Stable product/documentation
hostnames are optional but recommended for long-lived installations.

## Control plane and console

The simplest setup is to point users at the native control-plane URL printed
by `sbx deploy`; the console is same-origin with `/v1`.

If you front it with the repository's Cloudflare Worker, remember that the
worker bundles the `web/` console at deploy time. After an SBX upgrade,
redeploy the worker as well or the custom domain can serve an older Console
while `/v1` points at the new backend.

See [Edge](/self-hosting/edge/) for the worker configuration.

## Documentation

Build/deploy the docs with their final canonical URL:

```bash
DOCS_SITE_URL=https://docs.example.com make docs-deploy
```

Then configure DNS/custom-domain routing at your hosting/DNS provider to the
printed docs origin. Verify:

```text
/
/llms.txt
/llms-full.txt
/openapi.json
/version.json
/sitemap-index.xml
```

all resolve on the public hostname before publishing it as the canonical docs
URL.
