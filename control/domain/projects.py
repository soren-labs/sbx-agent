"""Project and immutable ProjectVersion inputs (RFC 167 §02).

A ProjectVersion pins sanitized repository identity, base-ref selector, setup
and resume hook digests, image/dependency inputs, non-secret environment,
secret/Connection binding refs, service declarations, Harness/model/effort
defaults, idle/budget/concurrency policy and ShipPolicy.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .delivery import ShipPolicy
from .errors import DomainError


@dataclass(frozen=True)
class ServiceDeclaration:
    """One declared service; realization is a ServiceInstance per lease."""

    name: str
    argv: tuple[str, ...]
    cwd: str = "."
    preferred_port: int | None = None
    health_probe: dict = field(default_factory=dict)
    env_refs: tuple[str, ...] = ()
    restart_policy: str = "on_failure"  # always|on_failure|never
    startup_order: int = 0
    preview: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("-", "").replace("_", "").isalnum():
            raise DomainError("validation_failed", f"invalid service name {self.name!r}")
        if not self.argv:
            raise DomainError("validation_failed", "service argv must be non-empty")

    def digest(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True).encode()
        return "sha256:" + hashlib.sha256(blob).hexdigest()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "preferred_port": self.preferred_port,
            "health_probe": self.health_probe,
            "env_refs": list(self.env_refs),
            "restart_policy": self.restart_policy,
            "startup_order": self.startup_order,
            "preview": self.preview,
        }


@dataclass(frozen=True)
class EnvironmentSpec:
    """Non-secret environment and build inputs."""

    base_image: str | None = None
    image_digest: str | None = None
    os: str = "linux"
    arch: str = "x86_64"
    resource_class: str = "standard"
    setup_commands: tuple[tuple[str, ...], ...] = ()
    resume_commands: tuple[tuple[str, ...], ...] = ()
    dependencies: dict = field(default_factory=dict)
    env: dict = field(default_factory=dict)  # non-secret only
    secret_binding_refs: tuple[str, ...] = ()
    connection_refs: tuple[str, ...] = ()
    setup_digest: str | None = None
    resume_digest: str | None = None

    def to_dict(self) -> dict:
        return {
            "base_image": self.base_image,
            "image_digest": self.image_digest,
            "os": self.os,
            "arch": self.arch,
            "resource_class": self.resource_class,
            "setup_commands": [list(c) for c in self.setup_commands],
            "resume_commands": [list(c) for c in self.resume_commands],
            "dependencies": self.dependencies,
            "env": self.env,
            "secret_binding_refs": list(self.secret_binding_refs),
            "connection_refs": list(self.connection_refs),
            "setup_digest": self.setup_digest,
            "resume_digest": self.resume_digest,
        }


@dataclass(frozen=True)
class ExecutionDefaults:
    provider_id: str | None = None
    model: str | None = None
    effort: str | None = None
    executor_backend: str = "modal"
    resource_class: str = "standard"
    network_policy: dict = field(default_factory=dict)
    idle_timeout_seconds: int | None = None
    budget: dict = field(default_factory=dict)
    concurrency: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, {}, ())}


@dataclass
class ProjectVersion:
    """Immutable definition under one Project."""

    id: str
    workspace_id: str
    project_id: str
    ordinal: int
    repository: str | None
    base_ref: str = "main"
    environment: EnvironmentSpec = field(default_factory=EnvironmentSpec)
    services: tuple[ServiceDeclaration, ...] = ()
    defaults: ExecutionDefaults = field(default_factory=ExecutionDefaults)
    ship_policy: ShipPolicy = field(default_factory=ShipPolicy)
    spec_digest: str | None = None
    created_by: str | None = None
    created_at: object = None

    def compute_digest(self) -> str:
        body = {
            "repository": self.repository,
            "base_ref": self.base_ref,
            "environment": self.environment.to_dict(),
            "services": [s.to_dict() for s in self.services],
            "defaults": self.defaults.to_dict(),
            "ship_policy": self.ship_policy.to_dict(),
        }
        blob = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(blob).hexdigest()

    def environment_key(self, resolved_source_sha: str | None) -> str:
        """Exact environment cache key: spec + resolved source + fingerprints."""
        body = {
            "spec_digest": self.spec_digest,
            "resolved_source_sha": resolved_source_sha,
            "setup_digest": self.environment.setup_digest,
            "dependency_digests": sorted(
                f"{k}:{v}" for k, v in self.environment.dependencies.items()
            ),
            "image_digest": self.environment.image_digest,
            "os": self.environment.os,
            "arch": self.environment.arch,
        }
        blob = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(blob).hexdigest()


@dataclass
class Project:
    id: str
    workspace_id: str
    slug: str
    name: str
    current_version_id: str | None
    metadata: dict = field(default_factory=dict)
    version: int = 1
    created_at: object = None
    updated_at: object = None
