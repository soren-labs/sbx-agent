"""The operator's private server config file and the control-plane settings it holds.

The file is ``$XDG_CONFIG_HOME/sbx/server.env`` (``--config`` or ``SBX_SERVER_CONFIG``
override it): ``KEY=VALUE`` lines of ``SBX_*`` settings, directory ``0700`` and file
``0600`` because it holds the vault keyring and the runtime master key. A process
environment variable overrides the same key from the file. Values are never printed;
reports name only where each setting came from.
"""

from __future__ import annotations

import base64
import binascii
import getpass
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_ENV = "SBX_SERVER_CONFIG"
EXECUTOR_NAMES = ("local", "modal")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8800

# Settings the file may hold: the control-plane variables of control/composition.py
# plus the listen address and the managed-PostgreSQL switch used by ``sbx server``.
SERVER_KEYS = (
    "SBX_DATABASE_URL",
    "SBX_VAULT_KEYS",
    "SBX_RUNTIME_MASTER_KEY",
    "SBX_DATA_DIR",
    "SBX_PUBLIC_URL",
    "SBX_ALLOWED_ORIGINS",
    "SBX_COOKIE_SECURE",
    "SBX_EXECUTORS",
    "SBX_WORKER_THREADS",
    "SBX_RESEND_API_KEY",
    "SBX_MAIL_FROM",
    "SBX_INFERENCE_ALLOW_PRIVATE_URLS",
    "SBX_LISTEN_HOST",
    "SBX_LISTEN_PORT",
    "SBX_MANAGED_POSTGRES",
)
SECRET_KEYS = frozenset(
    {"SBX_DATABASE_URL", "SBX_VAULT_KEYS", "SBX_RUNTIME_MASTER_KEY", "SBX_RESEND_API_KEY"}
)
REQUIRED_KEYS = ("SBX_DATABASE_URL", "SBX_VAULT_KEYS", "SBX_RUNTIME_MASTER_KEY")


class ConfigError(Exception):
    """An operator-facing configuration problem with the action that fixes it."""

    def __init__(self, message: str, action: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.action = action


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


def default_config_path() -> Path:
    explicit = os.environ.get(CONFIG_ENV)
    if explicit:
        return Path(explicit).expanduser()
    return _xdg("XDG_CONFIG_HOME", ".config") / "sbx" / "server.env"


def default_data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / "sbx"


# --------------------------------------------------------------------------- validation
def problems(values: Mapping[str, str]) -> list[tuple[str, str, str]]:
    """``(key, message, action)`` for each invalid or missing control-plane setting.

    Shared by ``UnifiedConfig.from_env`` (start-up refusal) and ``sbx server check``.
    """
    out: list[tuple[str, str, str]] = []
    missing = [k for k in REQUIRED_KEYS if not values.get(k)]
    for key in missing:
        out.append(
            (
                key,
                f"{key} is not set",
                "run `sbx server init` to create a private server config, or export "
                + ", ".join(REQUIRED_KEYS),
            )
        )
    vault = values.get("SBX_VAULT_KEYS")
    if vault:
        error = vault_spec_error(vault)
        if error:
            out.append(
                (
                    "SBX_VAULT_KEYS",
                    error,
                    "use KID:BASE64KEY entries of 32 random bytes "
                    "(`openssl rand -base64 32`); keep existing keys to decrypt stored credentials",
                )
            )
    master = values.get("SBX_RUNTIME_MASTER_KEY")
    if master:
        try:
            raw = bytes.fromhex(master)
        except ValueError:
            raw = None
        if raw is None or len(raw) < 32:
            out.append(
                (
                    "SBX_RUNTIME_MASTER_KEY",
                    "SBX_RUNTIME_MASTER_KEY must be at least 32 bytes of hex",
                    "generate one with `openssl rand -hex 32`",
                )
            )
    executors = [e for e in (values.get("SBX_EXECUTORS") or "local,modal").split(",") if e]
    unknown = sorted(set(executors) - set(EXECUTOR_NAMES))
    if unknown or not executors:
        out.append(
            (
                "SBX_EXECUTORS",
                f"unknown executor backend(s): {', '.join(unknown)}"
                if unknown
                else "SBX_EXECUTORS names no executor backend",
                f"use a comma list of {', '.join(EXECUTOR_NAMES)}",
            )
        )
    threads = values.get("SBX_WORKER_THREADS")
    if threads is not None and (not threads.isdigit() or int(threads) < 1):
        out.append(("SBX_WORKER_THREADS", "SBX_WORKER_THREADS must be a positive integer", ""))
    port = values.get("SBX_LISTEN_PORT")
    if port is not None and (not port.isdigit() or not 0 < int(port) < 65536):
        out.append(("SBX_LISTEN_PORT", "SBX_LISTEN_PORT must be a TCP port number", ""))
    if values.get("SBX_RESEND_API_KEY") and not values.get("SBX_MAIL_FROM"):
        # Resend rejects senders outside a verified domain; fail at startup, not per email.
        out.append(
            (
                "SBX_MAIL_FROM",
                "SBX_MAIL_FROM is required when SBX_RESEND_API_KEY is set",
                "set a sender on a domain verified in Resend, or unset SBX_RESEND_API_KEY "
                "to use the local mail outbox",
            )
        )
    return out


def vault_spec_error(spec: str) -> str | None:
    for item in spec.split(","):
        kid, _, b64 = item.strip().partition(":")
        if not kid or not b64:
            return "SBX_VAULT_KEYS must be KID:BASE64KEY[,KID:BASE64KEY...]"
        try:
            key = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            return f"vault key {kid} is not valid base64"
        if len(key) != 32:
            return f"vault key {kid} must decode to 32 bytes"
    return None


# ------------------------------------------------------------------------ file format
def parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or key not in SERVER_KEYS:
            raise ConfigError(
                f"server config line {number}: expected one of the SBX_* server settings",
                "fix or remove that line",
            )
        values[key] = value.strip()
    return values


def render(values: Mapping[str, str]) -> str:
    lines = [
        "# SBX server configuration (private: holds the vault keyring and runtime master key).",
        "# Keep this file 0600 and back it up separately from the database: without",
        "# SBX_VAULT_KEYS stored credentials cannot be decrypted. Environment variables",
        "# override these values. Never commit or paste this file.",
    ]
    lines += [f"{key}={values[key]}" for key in SERVER_KEYS if values.get(key) not in (None, "")]
    return "\n".join(lines) + "\n"


def mode_of(path: Path) -> int | None:
    try:
        return path.stat().st_mode & 0o777
    except FileNotFoundError:
        return None


@dataclass
class ServerSettings:
    """Effective server settings: process environment over the config file over defaults."""

    path: Path
    file_exists: bool
    file_values: dict[str, str] = field(default_factory=dict)
    values: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(
        cls, path: Path | None = None, environ: Mapping[str, str] | None = None
    ) -> ServerSettings:
        path = (Path(path) if path else default_config_path()).expanduser().absolute()
        environ = os.environ if environ is None else environ
        file_values: dict[str, str] = {}
        exists = path.exists()
        if exists:
            try:
                text = path.read_text()
            except PermissionError:
                raise ConfigError(
                    f"cannot read the server config {path}", "run as the user who owns it"
                ) from None
            file_values = parse(text)
        values: dict[str, str] = {}
        sources: dict[str, str] = {}
        for key in SERVER_KEYS:
            if environ.get(key):
                values[key], sources[key] = environ[key], "environment"
            elif key in file_values:
                values[key], sources[key] = file_values[key], "file"
        if "SBX_DATA_DIR" not in values:
            # A config file implies a private data dir next to the user's other XDG data;
            # without one the historical ./.sbx-data default of the bare process applies.
            default = str(default_data_dir()) if exists else "./.sbx-data"
            values["SBX_DATA_DIR"], sources["SBX_DATA_DIR"] = default, "default"
        values["SBX_DATA_DIR"] = str(Path(values["SBX_DATA_DIR"]).expanduser().absolute())
        return cls(path, exists, file_values, values, sources)

    # -------------------------------------------------------------------- accessors
    @property
    def data_dir(self) -> Path:
        return Path(self.values["SBX_DATA_DIR"])

    @property
    def host(self) -> str:
        return self.values.get("SBX_LISTEN_HOST") or DEFAULT_HOST

    @property
    def port(self) -> int:
        raw = self.values.get("SBX_LISTEN_PORT")
        return int(raw) if raw and raw.isdigit() else DEFAULT_PORT

    @property
    def managed_postgres(self) -> bool:
        return self.values.get("SBX_MANAGED_POSTGRES") == "1"

    @property
    def executors(self) -> list[str]:
        return [e for e in (self.values.get("SBX_EXECUTORS") or "local,modal").split(",") if e]

    @property
    def url(self) -> str:
        host = self.host
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    def child_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """Environment for ``control.composition``: exactly this instance's settings.

        Server settings in the base that are not effective here are dropped, and
        ``SBX_SERVER_CONFIG`` names the selected file, so a separate instance (``--config``)
        never inherits another config's optional settings.
        """
        env = {
            k: v
            for k, v in (os.environ if base is None else base).items()
            if k not in SERVER_KEYS and k != CONFIG_ENV
        }
        env.update(self.values)
        env[CONFIG_ENV] = str(self.path)
        return env

    def describe(self) -> dict[str, object]:
        """Where each setting comes from; secret values are never included."""
        settings = {}
        for key in SERVER_KEYS:
            if key not in self.values:
                continue
            shown = "(set)" if key in SECRET_KEYS else self.values[key]
            settings[key] = {"source": self.sources.get(key, "default"), "value": shown}
        return {
            "config_file": str(self.path),
            "config_file_exists": self.file_exists,
            "settings": settings,
        }


# ------------------------------------------------------------------------------ init
def generate_vault_spec(kid: str = "k1") -> str:
    return f"{kid}:{base64.b64encode(secrets.token_bytes(32)).decode()}"


def generate_master_key() -> str:
    return secrets.token_hex(32)


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def write_new(path: Path, values: Mapping[str, str]) -> None:
    """Create the config file 0600; never overwrite one (it holds the only vault keys)."""
    private_dir(path.parent)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ConfigError(
            f"a server config already exists at {path}",
            "keep it (it holds the vault keys for stored credentials); use `sbx server check`, "
            "or pass --config for a separate instance",
        ) from None
    with os.fdopen(fd, "w") as fh:
        fh.write(render(values))
    os.chmod(path, 0o600)


def current_user() -> str:
    return getpass.getuser()
