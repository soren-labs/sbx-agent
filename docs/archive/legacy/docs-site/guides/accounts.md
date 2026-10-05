---
title: Provider accounts
sidebar:
  label: Provider accounts

description: Advanced account-pool behavior behind the normal Integrations → Connect provider flow.
---

Most users should manage provider logins from **Integrations** or with
`./sbx auth`. This page explains the account pool underneath that UI.

## Normal connection lifecycle

```bash
./sbx auth login --provider devin
./sbx auth status
```

`auth login` runs the provider's official login/OAuth flow, captures the
credential it creates, registers an account in the control plane and verifies
that credential from the cloud runtime. **Only verified/active accounts are
schedulable.**

Later:

```bash
./sbx auth verify --provider devin
./sbx auth relink --provider devin --relogin
./sbx auth logout --provider devin
```

The Console uses the same lifecycle and canonical account state.

## Automatic scheduling

A Task normally leaves `execution.account_id` unset/automatic. The scheduler
chooses a verified account for the requested provider/model with an available
slot. Rate-limited or invalid accounts are temporarily ineligible; another
eligible account can take the work.

Pin an account id only when policy/reproducibility requires it.

## Account state

Account metadata includes provider, label, status, slot limit and discovered
models/capabilities. Credential contents are never returned by the public API.

Typical states are presented in product terms such as active/verified,
needs-attention/cooling, disabled or invalid. Use:

```bash
./sbx auth status --provider devin
```

or the Integrations page instead of reading backing stores directly.

## Multiple accounts

Connect/import more than one login for the same provider when you need
additional subscription capacity or failover. Give each account its own slot
budget; automatic scheduling selects among eligible accounts.

The exact account records are available to admins under `/v1/accounts`; normal
Task clients should not depend on account ids.

## Import an existing login

If the provider is already logged in on the machine and you do not want to
run its login flow again:

```bash
./sbx auth import-existing --provider devin --from ~/.local/share/devin/credentials.toml
```

The path is provider-specific and is an advanced migration input, not a value
normal Task callers need to know.

## Scripted/legacy onboarding

The lower-level `control.onboarding` command and environment-based account
preseeding remain available for fleet automation and compatibility. They
operate on the same account/verification model but expose credential files,
Secret names and account ids directly. Prefer `sbx auth` unless you are
building operator automation that explicitly needs those internals.

Exact command flags for the installed version are generated in
[`/cli-help.txt`](/cli-help.txt).

## Credential refresh

Providers that rotate OAuth state can write refreshed credential material
inside the sandbox. SBX captures the provider's supported write-back and
atomically updates the managed account credential. If a grant becomes invalid,
relink the account; do not keep retrying Tasks against an unverified account.

## Delete/disable

Use `auth logout` or the admin account API/UI. Disabling/removing an account
makes it ineligible for new scheduling; active work is handled according to
the control-plane lifecycle rather than by editing credential files by hand.
