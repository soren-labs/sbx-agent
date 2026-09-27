---
title: Configuration
description: Config precedence, common deployment knobs and the generated exact field reference.
---

A fresh `./sbx deploy` works without a pre-existing config file. When the file
is absent, deploy initializes `~/.config/sbx/config.toml` with the current
code defaults.

## Precedence

For deployment configuration the resolution order is:

1. supported command-line flags;
2. environment overrides;
3. `~/.config/sbx/config.toml` (or `$SBX_CONFIG`);
4. code defaults for the installed version.

Use `./sbx config` to inspect the resolved **non-secret** view instead of
guessing which layer won.

## Exact generated reference

The docs build derives every config field, TOML path, environment override and
code default from `sbx.config.BootstrapConfig` / `_FIELD_MAP`:

- [`/config-reference.json`](/config-reference.json)

This JSON is the exact reference for the version that built the docs. It is
preferable to copying large default tables into prose, where they drift.

## Common settings

### Providers

The fresh-deploy default is **no providers**. The platform is still healthy;
connect provider accounts after deployment. Operators can also preselect
runtime images with `deploy.providers` / `SBX_PROVIDERS`.

```toml
[deploy]
providers = ["codex", "devin"]
```

### Concurrency and lifecycle

Common deploy fields include:

```toml
[deploy]
max_concurrent = 8
idle_timeout_s = 300
sandbox_idle_timeout_s = 1800
turn_max_seconds = 900
sandbox_timeout_s = 14400
```

The exact defaults are versioned in `config-reference.json`. Keep
post-session idle retention, native sandbox idle timeout and hard timeout as
separate concepts; changing one should not silently redefine the others.

### Control-plane warmth

The control-plane web function has separate Modal scaling knobs such as
`control_scaledown_window_s`, `control_min_containers` and
`control_buffer_containers`. They affect the web/control plane, not the Task
sandbox lifecycle.

### Resource allowlists

`SBX_RESOURCE_SECRETS` and `SBX_MCP_REGISTRY` are advanced execution-policy
inputs. Only explicitly allowed resources should be requestable by a task.

## Accounts and models

Normal provider connection uses:

```bash
./sbx auth login --provider devin
```

Provider-specific account pool/model environment overrides still exist for
fleet operators. Prefer the Integrations UI/`sbx auth` lifecycle unless you
have a reproducible reason to manage the lower-level pool declaration.

## GitHub

The default GitHub path is `./sbx github connect` with the hosted SBX GitHub
App/broker. The older `github.*` / `SBX_GITHUB_*` App/PAT fields are advanced
self-hosting alternatives; they are not prerequisites for a normal install.
See [GitHub](/integrations/github/).

## Secrets

Config stores **names and non-secret settings**, not provider tokens/private
keys. Secret material belongs in the managed provider-account/GitHub/Modal
credential path documented by the relevant integration page.
