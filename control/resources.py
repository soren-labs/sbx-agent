"""SOR-129 session resources: explicit per-agent Modal Secret + MCP refs.

Agents may declare resource refs at create time (``POST /v1/agents``
``resources``):

- ``secrets`` — Modal Secret *names* attached to that agent's sandbox only
  (``SandboxSpec.resource_secrets``). The control plane resolves names,
  never values: Secret material stays inside Modal and inside the one
  sandbox. Names are allowlist-validated (``SBX_RESOURCE_SECRETS``) and the
  control plane's own credential Secrets — account ``sbx-acct-*``, the
  Codex auth Secret, basic/v1-bootstrap auth, the GitHub bridge Secret —
  are structurally excluded so a session ref can never mount a foreign
  account's credential.
- ``mcp`` — names of MCP server entries in the deployment registry
  (``SBX_MCP_REGISTRY`` JSON). Resolved server configs are handed to
  ``runner init`` through ``SBX_MCP_SERVERS`` as URL/transport/header
  *templates* where credential material rides ``${env:VAR}`` indirection —
  no secret value ever crosses the API, the runner env, or the generated
  ``mcp_config.json``.

Validation is fail-closed at request time: an unknown or disallowed ref is
``invalid_resource``; MCP on a provider with no MCP channel is the
machine-readable ``unsupported`` — never silently ignored. Resource values
are never returned or logged (only ref names are), and generated MCP
config lives under the sandbox ``$HOME`` — outside the workspace and
inside the artifact denylist — so nothing is baked into artifacts or
snapshots.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from control.config import (
    ACCOUNT_SECRET_PREFIX,
    BASIC_SECRET_NAME,
    CODEX_SECRET_NAME,
    V1_BOOTSTRAP_SECRET_NAME,
)

# Control-plane env configuring the resource registry.
RESOURCE_SECRETS_ENV = "SBX_RESOURCE_SECRETS"  # comma-separated allowlist
MCP_REGISTRY_ENV = "SBX_MCP_REGISTRY"  # JSON {name: {url, transport?, headers?, providers?}}
# Env handed to ``runner init`` carrying the resolved MCP entries
# (config templates only — never secret values). Consumed by
# ``runtime.runner.mcp`` inside the sandbox.
MCP_SERVERS_ENV = "SBX_MCP_SERVERS"

# Canonical v1 error codes for resource validation failures.
INVALID_RESOURCE = "invalid_resource"
UNSUPPORTED = "unsupported"

# Providers whose runner materializes generated MCP config (the SOR-77
# ``mcp_config.json`` machinery). Every other provider is ``unsupported``
# for MCP refs — refused at create time, never silently dropped.
MCP_CAPABLE_PROVIDERS = frozenset({"devin"})

_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SECRET_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
_ENV_REF_RE = re.compile(r"\$\{env:[A-Za-z_][A-Za-z0-9_]*\}")
# Header names that carry credential material: their values must route
# through ``${env:VAR}`` indirection so no raw token is ever written into
# the registry, the API, or the generated config file.
_SECRET_HEADER_RE = re.compile(
    r"(authorization|token|secret|api[-_]?key|credential|cookie|password)", re.IGNORECASE
)


class ResourceError(Exception):
    """Domain error for session-resource validation (code → v1 error code)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class McpServerSpec:
    """One validated MCP registry entry.

    ``providers`` restricts the agent providers the entry supports;
    ``None`` means "every MCP-capable provider". ``headers`` values are
    config templates — credential material must use ``${env:VAR}``
    indirection, enforced at parse time for credential-shaped names.
    """

    name: str
    url: str
    transport: str = "http"
    headers: dict[str, str] = field(default_factory=dict)
    providers: tuple[str, ...] | None = None

    def runner_entry(self) -> dict[str, Any]:
        """Entry shape forwarded to ``runner init`` via ``SBX_MCP_SERVERS``."""
        entry: dict[str, Any] = {
            "name": self.name,
            "url": self.url,
            "transport": self.transport,
        }
        if self.headers:
            entry["headers"] = dict(self.headers)
        return entry


def _parse_mcp_spec(name: str, spec: Any) -> McpServerSpec | None:
    """Validate one registry entry; ``None`` when malformed.

    A malformed entry is unreferenceable — it resolves as an unknown ref
    (``invalid_resource``) rather than partially injecting into a sandbox.
    """
    if not _MCP_NAME_RE.match(name) or not isinstance(spec, Mapping):
        return None
    url = spec.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    transport = spec.get("transport", "http")
    if not isinstance(transport, str) or not transport.strip():
        return None
    headers = spec.get("headers") or {}
    if not isinstance(headers, Mapping):
        return None
    clean_headers: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return None
        if _SECRET_HEADER_RE.search(key) and not _ENV_REF_RE.search(value):
            # Credential material must ride env indirection — a literal
            # Authorization/token value would be a raw secret on disk.
            return None
        clean_headers[key] = value
    providers = spec.get("providers")
    if providers is not None:
        if not isinstance(providers, (list, tuple)) or not all(
            isinstance(p, str) and p for p in providers
        ):
            return None
        providers = tuple(providers)
    return McpServerSpec(
        name=name,
        url=url,
        transport=transport,
        headers=clean_headers,
        providers=providers,
    )


def _parse_mcp_registry(raw: str | None) -> dict[str, McpServerSpec]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, McpServerSpec] = {}
    for name, spec in data.items():
        parsed = _parse_mcp_spec(str(name), spec)
        if parsed is not None:
            out[parsed.name] = parsed
    return out


def _reserved_secret_names(env: Mapping[str, str]) -> set[str]:
    """Control-plane credential Secret names a session ref must never mount."""
    names = {
        CODEX_SECRET_NAME,
        BASIC_SECRET_NAME,
        V1_BOOTSTRAP_SECRET_NAME,
        env.get("SBX_CODEX_SECRET_NAME") or CODEX_SECRET_NAME,
        env.get("SBX_BASIC_SECRET_NAME") or BASIC_SECRET_NAME,
        env.get("SBX_V1_BOOTSTRAP_SECRET_NAME") or V1_BOOTSTRAP_SECRET_NAME,
    }
    github_secret = env.get("SBX_GITHUB_SECRET_NAME")
    if github_secret:
        names.add(github_secret)
    return names


def _reserved_secret_prefixes(env: Mapping[str, str]) -> tuple[str, ...]:
    """Per-account credential Secret prefixes (``sbx-acct-<id>``)."""
    prefix = env.get("SBX_ACCOUNT_SECRET_PREFIX") or ACCOUNT_SECRET_PREFIX
    return tuple({ACCOUNT_SECRET_PREFIX, prefix})


def supports_mcp(provider: str) -> bool:
    """Whether ``provider`` has an MCP channel (``mcp_config.json`` writer)."""
    return provider in MCP_CAPABLE_PROVIDERS


class ResourceRegistry:
    """Deployment allowlist for session resources.

    Built from env by :meth:`from_env` (``SBX_RESOURCE_SECRETS`` +
    ``SBX_MCP_REGISTRY``) or constructed directly in tests. An empty
    registry allows nothing — refs are opt-in by the operator, never
    ambient.
    """

    def __init__(
        self,
        *,
        secrets: tuple[str, ...] | list[str] = (),
        mcp: Mapping[str, McpServerSpec] | None = None,
        reserved_names: set[str] | None = None,
        reserved_prefixes: tuple[str, ...] = (ACCOUNT_SECRET_PREFIX,),
    ) -> None:
        self._secrets = frozenset(secrets)
        self._mcp = dict(mcp or {})
        self._reserved_names = (
            set(reserved_names)
            if reserved_names is not None
            else {
                CODEX_SECRET_NAME,
                BASIC_SECRET_NAME,
                V1_BOOTSTRAP_SECRET_NAME,
            }
        )
        self._reserved_prefixes = tuple(reserved_prefixes)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ResourceRegistry:
        env = os.environ if env is None else env
        secrets = [
            name.strip()
            for name in (env.get(RESOURCE_SECRETS_ENV) or "").split(",")
            if name.strip()
        ]
        return cls(
            secrets=tuple(secrets),
            mcp=_parse_mcp_registry(env.get(MCP_REGISTRY_ENV)),
            reserved_names=_reserved_secret_names(env),
            reserved_prefixes=_reserved_secret_prefixes(env),
        )

    def secret_error(self, name: Any) -> str | None:
        """Why ``name`` may not be attached as a session resource, else None."""
        if not isinstance(name, str) or not _SECRET_NAME_RE.match(name):
            return f"malformed secret ref {name!r}"
        if name in self._reserved_names or name.startswith(self._reserved_prefixes):
            return f"secret ref {name!r} is reserved for control-plane credentials"
        if name not in self._secrets:
            return f"secret ref {name!r} is not allowlisted"
        return None

    def mcp_server(self, name: Any) -> McpServerSpec | None:
        if not isinstance(name, str):
            return None
        return self._mcp.get(name)


def _dedup(names: list[Any]) -> list[Any]:
    out: list[Any] = []
    seen: set[Any] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def resolve_resources(
    decl: Any,
    *,
    provider: str,
    registry: ResourceRegistry,
) -> dict[str, Any] | None:
    """Validate a create-time ``resources`` declaration into resolved refs.

    Returns ``{"secrets": [names], "mcp": [runner entries]}`` or ``None``
    when nothing was declared. ``runner entries`` carry config templates
    only (``${env:VAR}`` indirection) — never secret values.

    Raises :class:`ResourceError` — ``invalid_resource`` for malformed,
    unknown or disallowed refs; ``unsupported`` for MCP on a provider with
    no MCP channel or an entry that excludes the provider.
    """
    if decl is None:
        return None
    secrets = _dedup(list(getattr(decl, "secrets", None) or []))
    mcp_refs = _dedup(list(getattr(decl, "mcp", None) or []))
    if not secrets and not mcp_refs:
        return None
    for name in secrets:
        error = registry.secret_error(name)
        if error is not None:
            raise ResourceError(INVALID_RESOURCE, error)
    specs: list[McpServerSpec] = []
    for ref in mcp_refs:
        spec = registry.mcp_server(ref)
        if spec is None:
            raise ResourceError(INVALID_RESOURCE, f"unknown MCP resource {ref!r}")
        specs.append(spec)
    if specs:
        if not supports_mcp(provider):
            raise ResourceError(
                UNSUPPORTED,
                f"provider {provider!r} does not support MCP resources",
            )
        for spec in specs:
            if spec.providers is not None and provider not in spec.providers:
                raise ResourceError(
                    UNSUPPORTED,
                    f"MCP resource {spec.name!r} does not support provider {provider!r}",
                )
    return {
        "secrets": secrets,
        "mcp": [spec.runner_entry() for spec in specs],
    }


def resource_refs(resolved: Mapping[str, Any] | None) -> dict[str, list[str]] | None:
    """Ref names only — the agent-facing echo of resolved resources.

    Names are references, never values; MCP server entries collapse to
    their registry names so URL/header templates stay out of the API
    surface.
    """
    if not resolved:
        return None
    return {
        "secrets": list(resolved.get("secrets") or []),
        "mcp": [str(entry.get("name")) for entry in resolved.get("mcp") or []],
    }
