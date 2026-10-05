"""Unit of Work — the single authority transaction (RFC 167 §04).

A Session command transaction: authorize scoped rows → enforce dedupe → lock
Session/resource rows in defined order → verify lifecycle/versions/fences →
mutate typed projection → allocate sequence and append events → insert
deduped follow-up Jobs/outbox records → commit. No external call occurs under
the transaction.
"""

from __future__ import annotations

import psycopg

from .audit import AuditRepo
from .base import Rows
from .blobs import BlobReferenceRepo, BlobRepo
from .changes import ChangeSetFileRepo, ChangeSetRepo
from .connections import (
    ConnectionObservationRepo,
    ConnectionRepo,
    CredentialGrantRepo,
    CredentialVersionRepo,
)
from .database import Database
from .deduplication import DedupeRepo
from .delegation import (
    DelegationInputRepo,
    DelegationRepo,
    DelegationResultRepo,
    WaitSubscriptionRepo,
)
from .delivery import (
    DeliveryRepo,
    DeliveryStepRepo,
    DeliveryTargetClaimRepo,
    MergeRequestRepo,
)
from .events import EventRepo, IngestionOffsetRepo
from .execution import (
    CapacityRepo,
    ExecutionRepo,
    FenceRepo,
    LeaseRepo,
    NativeBindingRepo,
)
from .identity import (
    ApiKeyRepo,
    EmailVerificationRepo,
    LoginSessionRepo,
    PasswordCredentialRepo,
    PasswordResetRepo,
    UserRepo,
    WorkspaceRepo,
)
from .jobs import JobAttemptRepo, JobRepo
from .outbox import OutboxRepo
from .projects import EnvironmentBuildRepo, ProjectRepo, ProjectVersionRepo
from .services import ServiceDesireRepo, ServiceInstanceRepo
from .sessions import MessagePartRepo, MessageRepo, SessionRepo, TurnRepo
from .worktrees import SnapshotRepo, WorktreeOpRepo, WorktreeRepo


class SqlUnitOfWork:
    """Bound transaction exposing typed repositories. Enter → work → commit."""

    def __init__(self, db: Database, *, actor: dict | None = None) -> None:
        self.db = db
        self.actor = actor or {"kind": "system"}
        self.conn: psycopg.Connection = db.acquire()
        self.rows = Rows(self.conn)
        self._committed = False

        self.sessions = SessionRepo(self.conn)
        self.messages = MessageRepo(self.conn)
        self.turns = TurnRepo(self.conn)
        self.message_parts = MessagePartRepo(self.conn)
        self.events = EventRepo(self.conn)
        self.ingestion_offsets = IngestionOffsetRepo(self.conn)
        self.executions = ExecutionRepo(self.conn)
        self.leases = LeaseRepo(self.conn)
        self.native_bindings = NativeBindingRepo(self.conn)
        self.fences = FenceRepo(self.conn)
        self.capacity = CapacityRepo(self.conn)
        self.worktrees = WorktreeRepo(self.conn)
        self.worktree_ops = WorktreeOpRepo(self.conn)
        self.snapshots = SnapshotRepo(self.conn)
        self.jobs = JobRepo(self.conn)
        self.job_attempts = JobAttemptRepo(self.conn)
        self.outbox = OutboxRepo(self.conn)
        self.dedupe = DedupeRepo(self.conn)
        self.users = UserRepo(self.conn)
        self.password_credentials = PasswordCredentialRepo(self.conn)
        self.email_verifications = EmailVerificationRepo(self.conn)
        self.password_resets = PasswordResetRepo(self.conn)
        self.login_sessions = LoginSessionRepo(self.conn)
        self.api_keys = ApiKeyRepo(self.conn)
        self.workspaces = WorkspaceRepo(self.conn)
        self.projects = ProjectRepo(self.conn)
        self.project_versions = ProjectVersionRepo(self.conn)
        self.environment_builds = EnvironmentBuildRepo(self.conn)
        self.connections = ConnectionRepo(self.conn)
        self.credential_versions = CredentialVersionRepo(self.conn)
        self.credential_grants = CredentialGrantRepo(self.conn)
        self.connection_observations = ConnectionObservationRepo(self.conn)
        self.changesets = ChangeSetRepo(self.conn)
        self.changeset_files = ChangeSetFileRepo(self.conn)
        self.deliveries = DeliveryRepo(self.conn)
        self.delivery_steps = DeliveryStepRepo(self.conn)
        self.target_claims = DeliveryTargetClaimRepo(self.conn)
        self.merge_requests = MergeRequestRepo(self.conn)
        self.delegations = DelegationRepo(self.conn)
        self.delegation_inputs = DelegationInputRepo(self.conn)
        self.delegation_results = DelegationResultRepo(self.conn)
        self.wait_subscriptions = WaitSubscriptionRepo(self.conn)
        self.service_desires = ServiceDesireRepo(self.conn)
        self.service_instances = ServiceInstanceRepo(self.conn)
        self.blobs = BlobRepo(self.conn)
        self.blob_references = BlobReferenceRepo(self.conn)
        self.audit = AuditRepo(self.conn)

    def __enter__(self) -> SqlUnitOfWork:
        return self

    def commit(self) -> None:
        self.conn.commit()
        self._committed = True

    def rollback(self) -> None:
        self.conn.rollback()

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is not None or not self._committed:
                self.conn.rollback()
        finally:
            self.db.release(self.conn)
        return False
