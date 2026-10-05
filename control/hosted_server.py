"""VPS factory: validated production env plus injectable credentialed adapters."""

import importlib
import os

from control.connections import SecretVault


def production_config(env):
    if env.get("SBX_HOSTED") != "1" or env.get("SBX_STATE_BACKEND") != "postgres":
        raise ValueError("hosted production requires SBX_HOSTED=1 and PostgreSQL state")
    if not env.get("DATABASE_URL", "").startswith(("postgres://", "postgresql://")):
        raise ValueError("hosted production requires DATABASE_URL")
    if (
        env.get("SBX_CONNECTIONS_MODE") != "production"
        or env.get("SBX_AUTH_EMAIL_MODE") != "production"
    ):
        raise ValueError(
            "hosted production requires credentialed adapters; mock mode is development-only"
        )
    if env.get("SBX_BROWSER_ORIGINS") != "https://sbx-agent.com":
        raise ValueError("hosted production browser origin must be https://sbx-agent.com")
    if not env.get("SBX_HOSTED_ADAPTER_FACTORY"):
        raise ValueError("configure SBX_HOSTED_ADAPTER_FACTORY before production acceptance")
    if env.get("SBX_OPERATOR_AUTH_ENABLED") == "1" and (
        not env.get("SBX_BASIC_USER")
        or not env.get("SBX_BASIC_PASS")
        or env["SBX_BASIC_PASS"] == "sbx"
    ):
        raise ValueError("operator migration auth requires explicit production credentials")


def create_hosted_app():
    production_config(os.environ)
    vault = SecretVault.from_env()
    if vault is None:
        raise ValueError("hosted production requires SBX_CONNECTION_ENCRYPTION_KEY")
    module, separator, name = os.environ["SBX_HOSTED_ADAPTER_FACTORY"].partition(":")
    if not separator or not module or not name:
        raise ValueError("SBX_HOSTED_ADAPTER_FACTORY must be module:callable")
    try:
        adapters = getattr(importlib.import_module(module), name)()
    except Exception:
        raise ValueError("hosted production adapters could not be initialized") from None
    required = {
        "email_sender",
        "modal_provider",
        "github_factory",
        "codex_provider",
        "compute_provider",
    }
    if (
        not isinstance(adapters, dict)
        or set(adapters) != required
        or any(value is None for key, value in adapters.items() if key != "github_factory")
    ):
        raise ValueError("adapter factory must provide the five hosted production adapters")
    # Factory injection supplies real adapters without writing credentials to images.
    from control.app import create_app

    return create_app(
        hosted=True,
        state_backend="postgres",
        connection_vault=vault,
        # Compute executes inside the user image, not this VPS virtualenv.
        runner_cmd=["python", "-m", "runtime.runner"],
        max_concurrent=5,
        **adapters,
    )
