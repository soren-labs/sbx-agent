# Hosted GitHub connection

The default MVP setup is the [manual credential path](manual-credentials.md).
ChatGPT/Codex and GitHub App authorization are optional.

## Production App installation tokens (SOR-291)

Use `control.real_github.GitHubFactory` as the hosted factory. The VPS keeps
`SBX_GITHUB_APP_ID`, `SBX_GITHUB_APP_SLUG` and a mode-0600
`SBX_GITHUB_APP_PRIVATE_KEY_PATH`. OAuth client secrets/device flow are optional
and are not used by this installation-token path. Install SBX Agent through its
GitHub installation page, selecting the intended repositories. A trusted
operator then approves the association to an existing SBX user:

```sh
uv run python -m control.real_github --user-id USER_ID \
  --installation-id INSTALLATION_ID --repo owner/repository
```

Run only on the control plane with its protected environment. There is no
browser endpoint for this operation. The CLI verifies the App installation and
explicit repository set using GitHub before persisting the owner binding.
The UI explains operator approval and then lists only approved, currently
granted repositories. Browser installation callbacks cannot assert ownership
without that server-side binding. No private key or long-lived credential is
sent to browsers or sandboxes; Git operations receive short-lived repo tokens.

Disconnect is local to the SBX owner. Upstream uninstall/selection changes are
observed on live synchronization; production repository listing and minting
fail closed. A shared App installation is not deleted on one user's disconnect.
The real gate `uv run python -m deploy.hosted.gates.github` runs on the dedicated
`soren-labs/sbx-e2e-test` repository and merges only its disposable test PR.
No implementation PR is merged. See `docs/reviews/SOR-291-real.md` for evidence
and the deterministic-AI limitation of this GitHub stage.

The hosted Integrations page connects GitHub independently of Modal and AI.
`/hosted/connections/github/authorize` begins installation; the authenticated
callback consumes an expiring state owned by the current user. Installation
metadata uses the existing `InstallationRecord` format in PostgreSQL, scoped by
user. `/hosted/repositories` enumerates only that user's current explicit repo
selection. GitHub remains a repository connection, not a login provider.

`HostedGitHubService` reuses `GitHubAppService` for validation, repository-scoped
installation token minting and revocation. Hosted legacy GitHub routes resolve
the same user-bound service. Anonymous operator callbacks and App manifest
configuration cannot change a hosted user's installation. All-repository installs
are enumerated into explicit repository selection instead of authorizing every
future repository on an account.

Repository probes refuse repositories outside the user's installations. Existing
revision delivery, independent review and merge gates are reused, with per-repo
server credential resolution. Hosted sandbox exec replaces the operator GitHub
bridge with the current user's repo-scoped short-lived installation token.
Private App keys and longer-lived credentials stay on the VPS; connections use
the shared encryption vault. Tokens do not enter metadata or API responses.

In `SBX_CONNECTIONS_MODE=mock`, deterministic user-bound fake installations have
disjoint repositories. The fake GitHub remote stores upstream branch/PR/comment/
merge state durably for credential-free workflow validation. It exercises the
existing revision service; the review gate and exact-head comparisons still run.

For production, inject `github_factory` into `create_app`. Its client must
validate the authenticated GitHub authorization and enumerate **that user's**
verified installations, including callback installation access, rather than
returning every installation of the public SBX App. Use the existing App client
for scoped minting. Any long-lived user/broker credential must be kept in the
connection vault. Until that adapter is configured, installation is explicitly
unavailable. Real installation/permission changes, mint expiry and push/PR
acceptance require credentials; no external broker is silently used in hosted
mode.
