import time

from protocol.build import runtime_source_digest

from control.application.execution import Execution
from control.application.ingest import Ingest
from control.domain.errors import DomainError, require
from control.domain.sessions import TURN_TERMINAL
from control.executors.port import AllocationSpec
from control.runtime_client.grants import runtime_token


class ExecutionHandler:
    def __init__(self, uow, claims, executor_factory, credentials, master):
        self.uow, self.claims = uow, claims
        self.execution, self.ingest = Execution(uow, claims), Ingest(uow, claims)
        self.executor_factory, self.credentials, self.master = executor_factory, credentials, master
        self.snapshot_reader = None
        self.environment_resolver = None
        self.child_inputs = None
        self.tooling_resolver = None

    def __call__(self, claim):
        try:
            self.run(claim)
        except DomainError as error:
            if error.code not in {"waiting_capacity", "version_conflict"}:
                with self.uow.transaction() as repo:
                    execution = repo.one(
                        "SELECT id FROM executions WHERE turn_id=%s "
                        "AND state IN ('preparing','started','stop_requested')",
                        (claim.row["turn_id"],),
                    )
                if execution:
                    self.execution.lost(claim, execution["id"], error.code)
            raise

    def run(self, claim):
        session, turn, execution, lease = self.execution.admit(claim)
        if turn["state"] in TURN_TERMINAL:
            return
        token = runtime_token(self.master, lease["id"], lease["generation"])
        backend = self.executor_factory(session, lease, token)
        spec = AllocationSpec(
            session["workspace_id"],
            session["id"],
            lease["id"],
            lease["generation"],
            runtime_source_digest(),
            token,
            connection_id=lease["connection_id"],
        )
        handle = lease["handle"] or backend.lookup(lease["allocation_operation_id"])
        if not handle:
            handle = backend.allocate(spec, lease["allocation_operation_id"])
        client = backend.connect_runtime(handle)
        require(
            client.hello["runtime_build_digest"] == runtime_source_digest(), "runtime_incompatible"
        )
        if lease["state"] == "ready" and lease["fingerprint"].get("image_digest"):
            require(
                client.hello.get("image_digest") == lease["fingerprint"]["image_digest"],
                "runtime_incompatible",
            )
        if lease["state"] == "allocating":
            self.execution.bind(claim, session, lease, handle, client.hello)
        inputs = session["effective_inputs"]
        clone_credential = {}
        if self.environment_resolver:
            inputs, clone_credential = self.environment_resolver(session, execution, lease)
        with self.uow.transaction() as repo:
            worktree = repo.one("SELECT * FROM worktrees WHERE session_id=%s", (session["id"],))
        if (
            worktree["last_snapshot_id"]
            and self.snapshot_reader
            and not lease.get("environment_prepared_at")
        ):
            manifest = self.snapshot_reader(session["workspace_id"], worktree["last_snapshot_id"])
            restore_id = lease["allocation_operation_id"] + "-restore"
            client.submit(restore_id, "worktree.restore", {"manifest": manifest})
            restored = client.wait(restore_id)
            if restored.get("error"):
                raise DomainError(restored["error"])
        if not lease.get("environment_prepared_at"):
            envop = lease["allocation_operation_id"] + "-environment"
            # Same deterministic prepare operation is always deduped at runtime.
            client.submit(
                envop,
                "environment.prepare",
                {
                    "repository": None
                    if worktree["last_snapshot_id"]
                    else inputs.get("repository"),
                    "base_sha": inputs.get("base_sha"),
                    "setup": inputs.get("resume", [])
                    if worktree["last_snapshot_id"]
                    else inputs.get("setup", []),
                    "env": inputs.get("env", {}),
                    "clone_credential": clone_credential,
                },
            )
            prepared = client.wait(envop)
            if prepared.get("error"):
                raise DomainError(prepared["error"])
            with self.uow.transaction() as repo:
                self.claims.assert_current(repo, claim)
                repo.execute(
                    "UPDATE executor_leases SET environment_prepared_at=coalesce(envir"
                    "onment_prepared_at,now()) WHERE id=%s",
                    (lease["id"],),
                )
        if self.child_inputs:
            self.child_inputs(session, execution, lease, client, claim)
        with self.uow.transaction() as repo:
            self.claims.assert_current(repo, claim)
            repo.execute(
                "UPDATE worktrees SET availability='live',repository=%s,base_sha=%s WH"
                "ERE session_id=%s",
                (inputs.get("repository"), inputs.get("base_sha"), session["id"]),
            )
            message = repo.one("SELECT content FROM messages WHERE id=%s", (turn["message_id"],))
            native = repo.one(
                "SELECT * FROM native_context_bindings WHERE session_id=%s "
                "ORDER BY created_at DESC LIMIT 1",
                (session["id"],),
            )
        resolved = self.credentials(session, execution, lease, "inference")
        if isinstance(resolved, tuple):
            credential, credential_id = resolved
            self.execution.bind_credential(claim, execution["id"], credential_id)
        else:
            credential = resolved
        payload = {
            "turn_id": turn["id"],
            "execution_id": execution["id"],
            "provider_id": session["provider_id"],
            "model": inputs.get("model", ""),
            "prompt": message["content"],
            "native_id": native["native_id"] if native else None,
            "credential_bundle": credential,
            "settings": {**turn["settings"], "env": inputs.get("env", {})},
            "timeout": 600,
            "tooling": self.tooling_resolver(session, execution, lease)
            if self.tooling_resolver
            else None,
        }
        # Replaced credential material cannot change an already accepted operation body.
        # Inspect accepted operation first, then only submit if never accepted.
        try:
            operation = client.get("/operations/" + execution["operation_id"])
        except DomainError as error:
            if error.code != "not_found":
                raise
            operation = client.submit(
                execution["operation_id"], "turn.resume" if native else "turn.start", payload
            )
        last_renewal = time.monotonic()

        def tick(row):
            nonlocal last_renewal
            if time.monotonic() - last_renewal > 15:
                self.claims.renew(claim)
                last_renewal = time.monotonic()
            self.execution.started(claim, session, turn, execution)
            with self.uow.transaction() as repo:
                cancelled = repo.one(
                    "SELECT cancel_requested FROM turns WHERE id=%s", (turn["id"],)
                )
            if cancelled["cancel_requested"]:
                client.submit(
                    execution["operation_id"] + "-cancel",
                    "turn.cancel",
                    {"operation_id": execution["operation_id"]},
                )
            batch = client.get(
                "/events", after=self.offset(lease["id"], client.hello["runtime_epoch"])
            )
            if batch["events"]:
                ack = self.ingest.batch(
                    claim, execution["id"], batch["runtime_epoch"], batch["events"]
                )
                client.post("/events/ack", {"local_seq": ack})

        if operation["state"] in {"starting", "started"} and execution["state"] == "preparing":
            # Existing live process may be adopted. A daemon restart loses pipes and
            # reports recovered operations; stop/quarantine is required, not replay.
            if execution["operation_id"] in client.hello.get("recovered_operation_ids", []):
                raise DomainError("outcome_unknown")
        result = client.wait(execution["operation_id"], tick=tick)
        self.execution.started(claim, session, turn, execution)
        tick({"state": "terminal"})
        self.ingest.finish(claim, execution["id"], client.hello["runtime_epoch"], result)

    def offset(self, lease_id, epoch):
        with self.uow.transaction() as repo:
            row = repo.one(
                "SELECT ack FROM runtime_ingestion_offsets WHERE lease_id=%s AND runtime_epoch=%s",
                (lease_id, epoch),
            )
            return row["ack"] if row else 0
