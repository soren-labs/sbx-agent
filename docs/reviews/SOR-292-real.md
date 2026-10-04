# SOR-292 real native Codex connection

Stack base: `hosted-alpha/sor-291-real-github`, `0312966` (PR #159).

The dedicated test-user authorization completed through the official Codex
CLI device flow before this stage. Its private native cache stayed under
`/home/zheng/.config/sbx/codex-test-user`; the development-agent login remained
separate and healthy. No development-agent auth file was read or modified.

The production adapter uses official Codex app-server device authorization,
completion notifications and managed account refresh. Browser UI exposes only
the verification URL/device code and polls its owner-bound state. Trusted
operator import validates an approved dedicated native cache and encrypts
selected fields into the broker. It never copies the raw auth file to the repo
or compute. Grant fields in the temporary broker-native cache live on tmpfs;
native RPC runs with an explicit isolated auth location and stripped environment.

The existing PostgreSQL claim/CAS mechanism serializes rotation per user.
Native cached account metadata after a failed refresh cannot be counted as a
successful rotation. Permanent native failure requires reauthorization; transient
failure retains encrypted state and cooldown. Runtime auth rejection joins the
broker's current version and retries exactly once. A second rejection invalidates
only that credential version.

Real testing found two native format/version requirements: TokenData needs an
empty refresh field plus `last_refresh`; external-token mode prevents sandbox
refresh. The older runtime returned `model_unavailable`, so sandbox Codex is now
pinned to the working control-plane version 0.159.2. Generated Dockerfiles and
the image pin assertion were updated together. Hosted completion/cancellation
removes the native access cache before another operation obtains a new lease.

Opt-in real gate PASS (exit 0): official native access/refresh/expiry rotation;
three concurrent leases sharing one version; three real Modal coding Sessions
across a forced broker expiry boundary; successful native unittest commands in
all three; a completed second native turn; a fault-triggered 401 recovery using
real native rotation; encrypted PostgreSQL broker reconstruction retaining the
latest grant; invalid-grant injection → reauth_required → dedicated-login import.
The gate joined completion watchers and cleaned all three sandboxes and its
throwaway PostgreSQL container. Native provider secrets were absent from the
captured event streams. The dedicated native cache retained the latest rotation.

Focused checks: 31 passed. Image checks: 13 passed, 1 optional Docker check skipped.
Console typecheck and tests: 122 passed. `make lint` PASS; `make test`: 3,436 passed, 14 skipped.

Limits: the 401 trigger and invalid-grant injection are deliberate faults; native
refresh and coding use real providers, but these faults are not an upstream
account revocation. Revoking the reusable account would require new human
consent. Upstream OAuth token expiry remains provider-controlled; SBX's
five-minute lease metadata cannot shorten the provider's JWT expiry. No claim
is made that SBX has a separately registered hosted Sign in with Codex client.

Frozen contracts and old PR histories remain unchanged. Deployment is SOR-293.
