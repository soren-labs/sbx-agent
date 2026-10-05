---
title: CLI reference
description: Operator-facing sbx commands, with exact flags generated from the current argparse command model.
---

The repository launcher is `./sbx`; an installed package exposes the same CLI
as `sbx`. Use the launcher in a fresh clone because it can bootstrap `uv` and
the managed Python environment.

## Normal operator path

```bash
./sbx deploy
./sbx auth login --provider devin
./sbx github connect          # optional: private repos / PR delivery
./sbx doctor
./sbx open
```

| Command | Purpose |
| --- | --- |
| `deploy` | Idempotently deploy or update the core platform. A zero-provider deployment is valid. |
| `auth login` | Run the provider's official login, capture it and verify the account. |
| `auth status` | Inspect provider-account connection/verification state. |
| `github connect` | Open the official SBX GitHub App installation flow. |
| `doctor` | Verify platform/integration health. |
| `open` | Open the Console through a one-time signed-in browser handoff. |
| `status` | Show recorded deployment status/version/base URL. |
| `smoke` | Run a minimal real task against a connected provider. |
| `upgrade` | Redeploy while preserving durable stores. |
| `uninstall` | Stop/remove the deployment; purge flags are destructive. |

## Provider authentication

```bash
./sbx auth status
./sbx auth login --provider devin
./sbx auth verify --provider devin
./sbx auth relink --provider devin --relogin
./sbx auth logout --provider devin
```

`import-existing` and `pair` are migration/pairing paths. They are useful, but
normal users should start with `auth login` rather than locating credential
files manually.

## Advanced bootstrap/config commands

`init` remains useful when an operator wants to write config before deployment
or opt providers in ahead of time. Fresh-clone `deploy` does not require a
separate `init` step.

`credentials` scans provider credential locations without printing secret
contents; it is primarily an operator/debugging command.

## Exact flags for this version

The full help text is generated directly from the current CLI parser during
every docs build:

- [`/cli-help.txt`](/cli-help.txt)

For a local checkout, the same source of truth is always available as:

```bash
./sbx --help
./sbx <command> --help
./sbx auth <command> --help
```

Do not copy old flag lists from release notes or issue discussions; generated
help is authoritative for the installed version.
