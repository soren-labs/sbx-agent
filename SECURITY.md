# Security

Report vulnerabilities privately to the repository maintainers; do not publish live credentials or exploit production data.

Provider secrets live only in authenticated encrypted CredentialVersion envelopes. The operator master key stays outside PostgreSQL in private durable state. Secrets are never returned by Connection reads, copied to the Console cache, placed in Session events, or used through an ambient host profile. Runtime grants are scoped and expire; provider processes receive only their selected Zen key and private HOME/XDG.

Sandbox code is untrusted. Modal is the production isolation boundary; Local Executor is development-only. Files, captures and checkpoint restores reject traversal and external symlinks. Preview runs on a separate origin and strips Console authority. GitHub effects require exact immutable subject and freshly checked remote head; independent child review does not grant shipping authority.

Do not revoke the only teardown credential while live or ambiguous compute depends on it. Confirm isolation before releasing quota. Back up the database and credential-master/object volume together. Check HTTP output, events, logs, snapshots, exported subjects and repository diffs for secret leaks. The opt-in real acceptance script restricts test GitHub writes to `soren-labs/sbx-e2e-test` and records cleanup evidence.
