# runtime/

Sandbox-side components of the unified architecture:

- `daemon/` — the supervised `sbx-runtime` daemon: op journal + spool,
  session/TLS transport, worktree filesystem ops. Runs inside every
  ExecutorLease's compute.
- `harnesses/` — official-CLI adapters (opencode). A Harness is a thin
  adapter over the provider's own CLI; it never invents a model loop.
- `image.py` — named Modal image builders (`sbx-runtime`,
  `sbx-runtime-opencode`) + `python -m runtime.image` helpers
  (`--write-dockerfile`, `--manifest`, `--resolve-versions`,
  `--provider opencode`).
- `packages.txt` — single source of truth for image pins; `versions.py`
  resolves `latest` requests once on the build host and freezes them to
  `versions.lock.json` (`SBX_VERSIONS_LOCK` replays a frozen set).
- `entrypoint.sh` — sandbox PID 1: `$SBX_WORK` layout + graceful TERM.
- `security/` — path policy shared by daemon filesystem ops.

After changing anything under `runtime/daemon` or `protocol/`, republish the
named image (`make image`) — redeploying the control plane does not refresh
already-published images or running sandboxes.
