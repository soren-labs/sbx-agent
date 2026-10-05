# Contributing

Read `AGENTS.md` and the thirteen unified RFC files before changing architectural boundaries. Keep pure domain rules independent of network/runtime implementation. Resource commands own typed projections, append events and enqueue shared durable Jobs atomically. No read-side settlement or private actor state may become authority.

Run isolated lint, disposable PostgreSQL tests, spec drift check, Console typecheck/tests/build and documentation checks. Use injected fake transports and temporary HOME/XDG; cloud credentials are never needed by core CI. Fixture secrets must be `REDACTED`.

Change generated API schemas with `uv run python scripts/unified_spec.py`; include generated specs/types in the same change. A new provider must expose truthful installed-version capabilities and pass its continuation, cancellation, loss and secret-isolation matrix before being enabled. Unsupported features must fail explicitly.
