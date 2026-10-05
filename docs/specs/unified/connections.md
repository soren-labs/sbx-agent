# Manual Connection and Project contracts

The supported credential classes are product email/password, Modal token pair,
GitHub manual token, and OpenCode Zen API key. There is no OAuth prerequisite,
operator token fallback, Account mirror or mandatory Codex connection.

Credential versions use AES-256-GCM with a server-supplied keyring outside the
DB. Associated data binds Workspace/Connection/version/format. Metadata is
safe; ciphertext never enters Session events or Job payloads. Static keys have
no credential writeback. Version replacement invalidates observations/grants;
all delayed validation commits compare the selected version and current state.
Multiple Connections are allowed and selectors remain explicit, owner-bound.

Replacement/disconnect rejects `dependent_resources_active` while live or lost
unconfirmed compute depends on Modal/Zen authority. This intentionally retains
teardown authority. After verified termination, a tombstone prevents future
decryption/effects. Provider-side token revocation is not falsely claimed.

ProjectVersion is immutable; Sessions pin it and resolve an effective snapshot.
Actual Git base is resolved with the user's GitHub credential before the first
CLI Turn and kept on Worktree. Resume restores private checkpoint before any
cold setup; hooks are bounded and idempotence remains a declared requirement.
Git credentials use a temporary askpass file outside Worktree and are removed
before CLI inference. They are not URL/argv/image/native-history credentials.

Zen validation discovers metadata and labels inference as unverified. Models
with observed zero input/output cost sort first. Final live acceptance must
verify the selected key/model through official OpenCode, never a substitute
model API. Catalog GET is a pure projection of version-pinned observations.

Product passwords are Argon2id hashes, login/verification verifiers are hashes,
cookies have expiry/auth epoch, and cookie mutations require CSRF/origin.
Registration requires subsequent one-use email verification. The benchmark
can provision a verified test user through an operator-only application call;
this is not exposed as a public register override or real email delivery proof.
