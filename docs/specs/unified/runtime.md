# Runtime protocol 1

`protocol/runtime.py` is the shared wire record. The protected daemon journal
uses SQLite WAL/FULL commits outside worktree and native-home. Intent precedes
spawn; `starting` after restart is ambiguous and never automatically replayed.
A preserved journal retains epoch. An empty replacement creates a new epoch.
Runtime IDs and local sequence numbers are evidence, not public Session IDs.

Authenticated TLS HTTP operations carry identical fences/digests/grants to the
outbound WebSocket transport. Modal uses its encrypted runtime tunnel for this
HTTP transport optimization; outbound WS enrollment is available, while HTTP
is the tested command transport. No provider operation uses sandbox.exec.

Each mutating operation carries operation ID/kind, Session/lease/generation,
resource fence, grant/expiry, schema version, and canonical request SHA256.
Repeated identical bodies replay stored status; changed bodies conflict.
Capture/restore/files and Turns are serialized under the runtime barrier.
Stop confirms supervised process groups with TERM/KILL escalation. Pressure
stops work with incomplete evidence rather than pretending terminal success.

Checkpoint contains bounded owner-private file manifests and allowlisted
OpenCode database files after provider process stop. It excludes auth/config,
logs, dependencies and environment files. It does not restore process memory,
PTY, sockets, or external effects. Apply validates paths and digests before
replacement. Host HOME/environment are never inherited by the CLI.

OpenCode 1.18.29 is experimental pending final real acceptance. Npm integrity:
`sha512-syIDVwlrYTgTOXzZe9SkInJWethbq6l3SNC762UeXyO0a9V0wGfd+U4yACvppwNBnhIsl0j2QPYYCyLpNaSomg==`.
Native run semantics were verified against
[official pinned run.ts](https://github.com/anomalyco/opencode/blob/v1.18.29/packages/opencode/src/cli/cmd/run.ts),
SHA256 bde848ca17f30834beb4ef0dfecb3b819730746ec2023c1ac55ecef7ca25dd37,
and the [auth boundary](https://github.com/anomalyco/opencode/blob/v1.18.29/packages/opencode/src/auth/index.ts).
Cumulative text replaces a revisioned part; unknown valid native frames are
ignored, absent terminal/usage remains unknown. Other adapters remain disabled
until independently installed/verified; no optional provider blocks Zen MVP.

Modal allocation passes the selected user's explicit client on lookup/create/
terminate and tags/names the stable allocation effect. SDK semantics:
[Sandbox reference](https://modal.com/docs/reference/modal.Sandbox).
Lost responses inspect the same effect. Unproven old compute stays quarantined;
its Worktree identity and accepted conversation remain durable in PostgreSQL.
