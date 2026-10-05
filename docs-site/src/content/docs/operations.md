---
title: Operations and recovery
---
Use PostgreSQL 17 and a private state directory containing the credential master and immutable objects. Back up the database and objects, and protect the separate master key; a database backup alone cannot decrypt credentials.

Start the API with `python -m control.serve --dsn YOUR_DISPOSABLE_OR_OPERATOR_DSN --state-dir PRIVATE_STATE --console-dir console/dist`. Build the Console with `npm --prefix console run build`. Cookies are Secure by default; the explicitly named insecure local cookie flag is only for loopback development. Provider credentials are product inputs, never deployment environment variables.

Save a checkpoint before releasing compute. No process memory or PTY is claimed restored. Unknown execution outcomes quarantine the old compute and retain provider capacity until isolation is confirmed. Credential disconnect/replacement is rejected while dependent compute remains unconfirmed, preserving teardown authority.

Terminals are real lease-local PTYs with a writer barrier and bounded polling UI. Preview uses a separate hostname/origin and bounded HTTP proxy; no Console cookies/admin headers are forwarded. Later caches, OAuth, extensions and automation are not enabled. Historical pre-rewrite documentation is archived and is not an active product contract.
