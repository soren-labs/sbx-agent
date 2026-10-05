-- 0001_initial: unified SBX relational schema (RFC 167 §04).
-- Typed projections + append-only session_events journal + durable jobs.
-- JSONB is limited to versioned payload/spec/capability/result extensions;
-- authorization/constraint/gate fields are typed columns.

BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version      integer PRIMARY KEY,
    applied_at   timestamptz NOT NULL DEFAULT now(),
    description  text NOT NULL
);

-- ---------------------------------------------------------------- identity
CREATE TABLE users (
    id                 text PRIMARY KEY,
    email_normalized   text NOT NULL UNIQUE,
    display_name       text,
    verified_at        timestamptz,
    identity_version   integer NOT NULL DEFAULT 1,
    auth_epoch         integer NOT NULL DEFAULT 1,
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE password_credentials (
    user_id        text PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    algorithm      text NOT NULL,              -- e.g. argon2id
    password_hash  text NOT NULL,
    version        integer NOT NULL DEFAULT 1,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE email_verifications (
    id             text PRIMARY KEY,
    user_id        text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash     text NOT NULL UNIQUE,
    expires_at     timestamptz NOT NULL,
    consumed_at    timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE password_resets (
    id             text PRIMARY KEY,
    user_id        text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash     text NOT NULL UNIQUE,
    expires_at     timestamptz NOT NULL,
    consumed_at    timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE login_sessions (
    id             text PRIMARY KEY,
    user_id        text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash     text NOT NULL UNIQUE,
    auth_epoch     integer NOT NULL,
    expires_at     timestamptz NOT NULL,
    revoked_at     timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX login_sessions_user ON login_sessions(user_id, expires_at);

CREATE TABLE api_keys (
    id             text PRIMARY KEY,
    user_id        text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label          text,
    key_hash       text NOT NULL UNIQUE,
    scopes         jsonb NOT NULL DEFAULT '[]',
    auth_epoch     integer NOT NULL,
    expires_at     timestamptz,
    revoked_at     timestamptz,
    last_used_at   timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX api_keys_user ON api_keys(user_id);

-- ------------------------------------------------------------- workspaces
CREATE TABLE workspaces (
    id              text PRIMARY KEY,
    owner_user_id   text NOT NULL REFERENCES users(id),
    name            text NOT NULL,
    policy_version  integer NOT NULL DEFAULT 1,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX workspaces_personal_owner
    ON workspaces(owner_user_id) WHERE name = 'personal';

CREATE TABLE workspace_memberships (
    workspace_id  text NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id       text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role          text NOT NULL,
    status        text NOT NULL DEFAULT 'active',
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, user_id)
);

-- --------------------------------------------------------------- projects
CREATE TABLE projects (
    id                   text PRIMARY KEY,
    workspace_id         text NOT NULL REFERENCES workspaces(id),
    slug                 text NOT NULL,
    name                 text NOT NULL,
    current_version_id   text,           -- FK added after project_versions
    metadata             jsonb NOT NULL DEFAULT '{}',
    version              integer NOT NULL DEFAULT 1,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, slug),
    UNIQUE (workspace_id, id)
);

CREATE TABLE project_versions (
    id              text PRIMARY KEY,
    workspace_id    text NOT NULL,
    project_id      text NOT NULL,
    ordinal         integer NOT NULL,
    repository      text,                -- sanitized owner/name identity
    base_ref        text NOT NULL DEFAULT 'main',
    environment     jsonb NOT NULL DEFAULT '{}',  -- EnvironmentSpec
    services        jsonb NOT NULL DEFAULT '[]',  -- ServiceDeclaration[]
    defaults        jsonb NOT NULL DEFAULT '{}',  -- ExecutionDefaults
    ship_policy     jsonb NOT NULL DEFAULT '{}',  -- ShipPolicy
    spec_digest     text NOT NULL,
    created_by      text REFERENCES users(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, ordinal),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, project_id)
        REFERENCES projects(workspace_id, id) ON DELETE CASCADE
);

ALTER TABLE projects
    ADD CONSTRAINT projects_current_version_fk
    FOREIGN KEY (current_version_id) REFERENCES project_versions(id);

CREATE TABLE environment_builds (
    id                  text PRIMARY KEY,
    workspace_id        text NOT NULL REFERENCES workspaces(id),
    project_version_id  text NOT NULL REFERENCES project_versions(id),
    environment_key     text NOT NULL,   -- exact input key
    operation_id        text UNIQUE,
    state               text NOT NULL,   -- queued|building|ready|failed
    image_ref           text,
    snapshot_id         text,            -- environment Snapshot once sealed
    last_error          jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id)
);
CREATE INDEX environment_builds_key
    ON environment_builds(workspace_id, project_version_id, environment_key);

-- ------------------------------------------------------------- connections
CREATE TABLE connections (
    id                  text PRIMARY KEY,
    workspace_id        text NOT NULL REFERENCES workspaces(id),
    kind                text NOT NULL,   -- modal|github|opencode_zen|codex
    label               text,
    created_by          text NOT NULL REFERENCES users(id),
    allowed_principals  jsonb NOT NULL DEFAULT '[]',
    allowed_purposes    jsonb NOT NULL DEFAULT '[]',
    acquisition         text NOT NULL,   -- manual|native_upload|oauth|device
    state               text NOT NULL,   -- configured|disabled|revoked
    health              text NOT NULL DEFAULT 'unverified',
    current_credential_version_id text,
    external_identity   jsonb NOT NULL DEFAULT '{}',
    capability_observations jsonb NOT NULL DEFAULT '{}',
    version             integer NOT NULL DEFAULT 1,
    revocation_epoch    integer NOT NULL DEFAULT 0,
    cooldown_until      timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id)
);
CREATE INDEX connections_kind ON connections(workspace_id, kind, state);

CREATE TABLE credential_versions (
    id              text PRIMARY KEY,
    workspace_id    text NOT NULL,
    connection_id   text NOT NULL,
    ordinal         integer NOT NULL,
    state           text NOT NULL DEFAULT 'active',  -- active|superseded|revoked
    format          text NOT NULL,
    ciphertext      bytea NOT NULL,
    key_id          text NOT NULL,
    nonce           bytea NOT NULL,
    aad             jsonb NOT NULL DEFAULT '{}',
    fingerprint     jsonb NOT NULL DEFAULT '{}',     -- safe metadata only
    expires_at      timestamptz,
    revoked_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (connection_id, ordinal),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, connection_id)
        REFERENCES connections(workspace_id, id) ON DELETE CASCADE
);

ALTER TABLE connections
    ADD CONSTRAINT connections_current_cred_fk
    FOREIGN KEY (current_credential_version_id) REFERENCES credential_versions(id);

CREATE TABLE connection_observations (
    id                    text PRIMARY KEY,
    workspace_id          text NOT NULL,
    connection_id         text NOT NULL,
    credential_version_id text REFERENCES credential_versions(id),
    kind                  text NOT NULL,   -- validation|capability|health
    scope                 text NOT NULL,   -- resource/purpose scoped
    result                jsonb NOT NULL DEFAULT '{}',
    expires_at            timestamptz,
    observed_at           timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id, connection_id)
        REFERENCES connections(workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX connection_observations_conn
    ON connection_observations(connection_id, kind, observed_at);

-- ------------------------------------------------------------------ vault
CREATE TABLE secret_bindings (
    id             text PRIMARY KEY,
    workspace_id   text NOT NULL REFERENCES workspaces(id),
    name           text NOT NULL,
    purpose        text NOT NULL,
    allowed_roles  jsonb NOT NULL DEFAULT '[]',
    current_secret_version_id text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, name),
    UNIQUE (workspace_id, id)
);

CREATE TABLE secret_versions (
    id               text PRIMARY KEY,
    workspace_id     text NOT NULL,
    secret_binding_id text NOT NULL,
    ordinal          integer NOT NULL,
    ciphertext       bytea NOT NULL,
    key_id           text NOT NULL,
    nonce            bytea NOT NULL,
    aad              jsonb NOT NULL DEFAULT '{}',
    expires_at       timestamptz,
    revoked_at       timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (secret_binding_id, ordinal),
    FOREIGN KEY (workspace_id, secret_binding_id)
        REFERENCES secret_bindings(workspace_id, id) ON DELETE CASCADE
);

CREATE TABLE credential_grants (
    id                    text PRIMARY KEY,
    workspace_id          text NOT NULL,
    connection_id         text NOT NULL,
    credential_version_id text NOT NULL REFERENCES credential_versions(id),
    purpose               text NOT NULL,
    principal_id          text,
    session_id            text,
    execution_id          text,
    lease_id              text,
    revocation_epoch      integer NOT NULL,
    expires_at            timestamptz NOT NULL,
    redeemed_at           timestamptz,
    revoked_at            timestamptz,
    created_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, connection_id)
        REFERENCES connections(workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX credential_grants_expiry ON credential_grants(expires_at)
    WHERE redeemed_at IS NULL AND revoked_at IS NULL;

CREATE TABLE refresh_claims (
    connection_id     text PRIMARY KEY REFERENCES connections(id) ON DELETE CASCADE,
    holder            text NOT NULL,
    generation        integer NOT NULL,
    expires_at        timestamptz NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------- sessions
CREATE TABLE sessions (
    id                    text PRIMARY KEY,
    workspace_id          text NOT NULL REFERENCES workspaces(id),
    role                  text NOT NULL DEFAULT 'author',
    title                 text,
    labels                jsonb NOT NULL DEFAULT '[]',
    created_by            text REFERENCES users(id),
    lifecycle             text NOT NULL DEFAULT 'open',
    project_version_id    text REFERENCES project_versions(id),
    projectless_spec      jsonb,                -- projectless effective spec
    harness               jsonb NOT NULL,       -- HarnessBinding
    effective_input       jsonb NOT NULL DEFAULT '{}',
    effective_input_digest text NOT NULL,
    linked_from_session_id text,
    version               integer NOT NULL DEFAULT 1,
    next_event_seq        integer NOT NULL DEFAULT 1,
    next_message_ordinal  integer NOT NULL DEFAULT 1,
    next_turn_ordinal     integer NOT NULL DEFAULT 1,
    active_turn_id        text,
    active_lease_id       text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    closed_at             timestamptz,
    UNIQUE (workspace_id, id)
);
CREATE INDEX sessions_list
    ON sessions(workspace_id, updated_at DESC, id);
CREATE INDEX sessions_project ON sessions(project_version_id)
    WHERE project_version_id IS NOT NULL;
CREATE INDEX sessions_lifecycle ON sessions(workspace_id, lifecycle);

CREATE TABLE messages (
    id              text PRIMARY KEY,
    workspace_id    text NOT NULL,
    session_id      text NOT NULL,
    ordinal         integer NOT NULL,
    author          jsonb NOT NULL,        -- MessageAuthor
    role            text NOT NULL DEFAULT 'user',
    routing         text NOT NULL,         -- note|queue|steer
    content         jsonb NOT NULL,        -- immutable accepted input
    attachment_refs jsonb NOT NULL DEFAULT '[]',
    reply_to_message_id   text,
    source_message_id     text,
    routed_routing        text,            -- effective routing after policy
    routed_turn_id        text,
    routed_execution_id   text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, ordinal),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, session_id)
        REFERENCES sessions(workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX messages_session ON messages(session_id, ordinal);

CREATE TABLE turns (
    id               text PRIMARY KEY,
    workspace_id     text NOT NULL,
    session_id       text NOT NULL,
    ordinal          integer NOT NULL,
    message_id       text NOT NULL,        -- triggering Message (unique)
    retry_of_turn_id text,
    state            text NOT NULL,        -- TurnState
    reason           text,
    resolved_settings jsonb NOT NULL DEFAULT '{}',
    result_contract  jsonb,
    cancel_intent    jsonb,
    outcome          jsonb NOT NULL DEFAULT '{}',
    version          integer NOT NULL DEFAULT 1,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    completed_at     timestamptz,
    UNIQUE (session_id, ordinal),
    UNIQUE (message_id),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, session_id)
        REFERENCES sessions(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, message_id)
        REFERENCES messages(workspace_id, id)
);
-- At most one Turn may be preparing/running/cancelling per Session.
CREATE UNIQUE INDEX turns_active_per_session ON turns(session_id)
    WHERE state IN ('preparing', 'running', 'cancelling');
CREATE INDEX turns_state ON turns(workspace_id, state);

CREATE TABLE turn_messages (
    turn_id     text NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    message_id  text NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    link_kind   text NOT NULL,             -- trigger|steer_ack
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (turn_id, message_id)
);

CREATE TABLE message_parts (
    id              text PRIMARY KEY,
    workspace_id    text NOT NULL,
    session_id      text NOT NULL,
    message_id      text NOT NULL,
    part_id         text NOT NULL,         -- stable provider part id
    ordinal         integer NOT NULL,
    kind            text NOT NULL,         -- text|tool|reasoning|attachment|diagnostic
    revision        integer NOT NULL DEFAULT 1,
    content         jsonb NOT NULL DEFAULT '{}',
    sealed          boolean NOT NULL DEFAULT false,
    completeness    text NOT NULL DEFAULT 'partial',  -- partial|completed
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (message_id, part_id),
    UNIQUE (message_id, ordinal),
    FOREIGN KEY (workspace_id, session_id)
        REFERENCES sessions(workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX message_parts_session ON message_parts(session_id, message_id);

-- ---------------------------------------------------------- session journal
CREATE TABLE session_events (
    id                text PRIMARY KEY,
    workspace_id      text NOT NULL,
    session_id        text NOT NULL,
    seq               integer NOT NULL,
    type              text NOT NULL,
    schema_version    integer NOT NULL,
    recorded_at       timestamptz NOT NULL DEFAULT now(),
    observed_at       timestamptz,
    actor             jsonb NOT NULL,
    source            text NOT NULL,
    causation_id      text,
    correlation_id    text,
    turn_id           text,
    execution_id      text,
    executor_lease_id text,
    lease_generation  integer,
    delegation_id     text,
    changeset_id      text,
    delivery_id       text,
    runtime_epoch     text,
    local_seq         integer,
    adapter_version   text,
    cli_version       text,
    payload           jsonb NOT NULL DEFAULT '{}',
    UNIQUE (session_id, seq),
    FOREIGN KEY (workspace_id, session_id)
        REFERENCES sessions(workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX session_events_turn ON session_events(session_id, turn_id)
    WHERE turn_id IS NOT NULL;
CREATE INDEX session_events_causation ON session_events(causation_id)
    WHERE causation_id IS NOT NULL;
-- Runtime source dedupe: unique (lease, epoch, local_seq)
CREATE UNIQUE INDEX session_events_runtime_tuple
    ON session_events(executor_lease_id, runtime_epoch, local_seq)
    WHERE source = 'runtime' AND runtime_epoch IS NOT NULL AND local_seq IS NOT NULL;

-- Append-only enforcement: no ordinary update/delete.
CREATE OR REPLACE FUNCTION reject_event_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'session_events is append-only';
END $$;
CREATE TRIGGER session_events_append_only
    BEFORE UPDATE OR DELETE ON session_events
    FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();

-- Mutable contiguous ack per runtime source.
CREATE TABLE runtime_ingestion_offsets (
    id                text PRIMARY KEY,
    executor_lease_id text NOT NULL,
    runtime_epoch     text NOT NULL,
    session_id        text NOT NULL,
    committed_local_seq integer NOT NULL DEFAULT 0,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (executor_lease_id, runtime_epoch)
);

-- ------------------------------------------------------ execution / leases
CREATE TABLE executor_leases (
    id                  text PRIMARY KEY,
    workspace_id        text NOT NULL REFERENCES workspaces(id),
    session_id          text NOT NULL,
    backend             text NOT NULL,     -- modal|local
    generation          integer NOT NULL,
    state               text NOT NULL,     -- LeaseState
    handle              jsonb NOT NULL DEFAULT '{}',  -- opaque backend metadata
    expires_at          timestamptz,
    observed_at         timestamptz,
    image_fingerprint   text,
    protocol_fingerprint text,
    allocation_operation_id text UNIQUE,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, session_id)
        REFERENCES sessions(workspace_id, id) ON DELETE CASCADE
);
-- At most one active (non-historical) lease per Session.
CREATE UNIQUE INDEX executor_leases_active_per_session
    ON executor_leases(session_id)
    WHERE state IN ('allocating', 'ready', 'quiescing');
CREATE INDEX executor_leases_expiry ON executor_leases(expires_at)
    WHERE state IN ('allocating', 'ready', 'quiescing');

CREATE TABLE native_context_bindings (
    id                    text PRIMARY KEY,
    workspace_id          text NOT NULL,
    session_id            text NOT NULL,
    provider_id           text NOT NULL,
    native_id             text NOT NULL,
    lineage_id            text NOT NULL,
    cli_version           text,
    adapter_version       text,
    state_manifest_digest text,
    account_affinity      text,
    checkpoint_ref        text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    UNIQUE (session_id, provider_id, lineage_id, native_id),
    FOREIGN KEY (workspace_id, session_id)
        REFERENCES sessions(workspace_id, id) ON DELETE CASCADE
);

CREATE TABLE executions (
    id                  text PRIMARY KEY,
    workspace_id        text NOT NULL,
    session_id          text NOT NULL,
    turn_id             text NOT NULL,
    attempt_ordinal     integer NOT NULL,
    operation_id        text NOT NULL UNIQUE,
    state               text NOT NULL,     -- ExecutionState
    executor_lease_id   text,
    lease_generation    integer,
    runtime_epoch       text,
    credential_version_id text,
    native_binding_id   text REFERENCES native_context_bindings(id),
    cli_version         text,
    adapter_version     text,
    image_ref           text,
    runtime_version     text,
    final_watermark     integer,
    outcome_evidence    jsonb NOT NULL DEFAULT '{}',
    reason              text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (turn_id, attempt_ordinal),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, session_id)
        REFERENCES sessions(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, turn_id)
        REFERENCES turns(workspace_id, id) ON DELETE CASCADE
);
-- One nonterminal Execution per Turn.
CREATE UNIQUE INDEX executions_nonterminal_per_turn ON executions(turn_id)
    WHERE state IN ('preparing', 'started', 'stop_requested');

CREATE TABLE resource_fences (
    id              text PRIMARY KEY,
    workspace_id    text NOT NULL,
    resource_name   text NOT NULL,   -- e.g. lease:<id>, delivery_target:<repo>:<ref>
    fence_type      text NOT NULL,
    generation      integer NOT NULL,
    holder          text,
    expires_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, resource_name)
);

CREATE TABLE capacity_reservations (
    id               text PRIMARY KEY,
    workspace_id     text NOT NULL,
    connection_id    text NOT NULL,
    slot_ordinal     integer NOT NULL,
    execution_id     text,
    executor_lease_id text,
    state            text NOT NULL,  -- held|released|expired
    expires_at       timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, connection_id)
        REFERENCES connections(workspace_id, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX capacity_reservations_active_slot
    ON capacity_reservations(connection_id, slot_ordinal)
    WHERE state = 'held';

-- --------------------------------------------------------------- worktrees
CREATE TABLE worktrees (
    id                text PRIMARY KEY,
    workspace_id      text NOT NULL,
    session_id        text NOT NULL UNIQUE,
    repository        text,
    base_sha          text,
    generation        integer NOT NULL DEFAULT 0,
    availability      text NOT NULL DEFAULT 'none',
    last_snapshot_id  text,
    recovery_point    jsonb NOT NULL DEFAULT '{}',
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, session_id)
        REFERENCES sessions(workspace_id, id) ON DELETE CASCADE
);

CREATE TABLE worktree_operations (
    id                 text PRIMARY KEY,
    workspace_id       text NOT NULL,
    worktree_id        text NOT NULL,
    kind               text NOT NULL,     -- activate|capture|checkpoint|apply|restore|turn
    operation_id       text UNIQUE,       -- durable operation identity
    fence_generation   integer NOT NULL,
    expected_generation integer,
    state              text NOT NULL,     -- active|completed|failed|aborted
    detail             jsonb NOT NULL DEFAULT '{}',
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id, worktree_id)
        REFERENCES worktrees(workspace_id, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX worktree_operations_active_barrier
    ON worktree_operations(worktree_id) WHERE state = 'active';

CREATE TABLE snapshots (
    id                  text PRIMARY KEY,
    workspace_id        text NOT NULL,
    kind                text NOT NULL,     -- environment|checkpoint
    state               text NOT NULL,     -- preparing|ready|failed
    project_version_id  text REFERENCES project_versions(id),
    worktree_id         text,
    worktree_generation integer,
    input_digest        text,
    content_digest      text,
    manifest            jsonb NOT NULL DEFAULT '{}',
    blob_refs           jsonb NOT NULL DEFAULT '[]',
    backend_refs        jsonb NOT NULL DEFAULT '{}',
    compatibility       jsonb NOT NULL DEFAULT '{}',
    event_watermark     integer,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    -- Exactly one owner kind.
    CHECK ((project_version_id IS NULL) <> (worktree_id IS NULL))
);
CREATE INDEX snapshots_env_key
    ON snapshots(project_version_id, input_digest)
    WHERE kind = 'environment' AND state = 'ready';

-- ------------------------------------------------------------------- blobs
CREATE TABLE blobs (
    id           text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    storage_key  text NOT NULL,
    digest       text NOT NULL,
    size         bigint NOT NULL,
    class        text NOT NULL,    -- attachment|manifest|trace|payload|native_state
    state        text NOT NULL,    -- uploading|sealed|tombstoned
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    UNIQUE (workspace_id, storage_key)
);

CREATE TABLE blob_references (
    id              text PRIMARY KEY,
    workspace_id    text NOT NULL,
    blob_id         text NOT NULL,
    entity_kind     text NOT NULL,
    entity_id       text NOT NULL,
    retention_class text NOT NULL DEFAULT 'default',
    created_at      timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id, blob_id)
        REFERENCES blobs(workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX blob_references_entity
    ON blob_references(entity_kind, entity_id);

-- -------------------------------------------------------------- changesets
CREATE TABLE changesets (
    id                  text PRIMARY KEY,
    workspace_id        text NOT NULL,
    session_id          text NOT NULL,
    worktree_id         text NOT NULL,
    worktree_generation integer NOT NULL,
    subject_digest      text NOT NULL,
    manifest_version    integer NOT NULL,
    source_turn_id      text,
    repository          text,
    base_sha            text,
    head_sha            text,
    tree_sha            text,
    capture_origin      text NOT NULL,   -- automatic|explicit|salvage
    automatic_eligible  boolean NOT NULL DEFAULT false,
    manifest            jsonb NOT NULL DEFAULT '{}',
    payload_refs        jsonb NOT NULL DEFAULT '{}',
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, session_id)
        REFERENCES sessions(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, worktree_id)
        REFERENCES worktrees(workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX changesets_session ON changesets(session_id, created_at);
CREATE UNIQUE INDEX changesets_capture_dedupe
    ON changesets(session_id, source_turn_id, worktree_generation)
    WHERE source_turn_id IS NOT NULL;

CREATE TABLE changeset_files (
    changeset_id    text NOT NULL REFERENCES changesets(id) ON DELETE CASCADE,
    path            text NOT NULL,
    file_type       text NOT NULL,   -- file|symlink|deleted
    mode            integer NOT NULL,
    content_digest  text,
    symlink_target  text,
    blob_id         text,
    PRIMARY KEY (changeset_id, path)
);

-- -------------------------------------------------------------- deliveries
CREATE TABLE deliveries (
    id                   text PRIMARY KEY,
    workspace_id         text NOT NULL,
    session_id           text NOT NULL,
    changeset_id         text NOT NULL,
    subject_digest       text NOT NULL,
    target               jsonb NOT NULL,       -- repository/ref/existing PR
    transport            text NOT NULL,        -- export|git_branch|pull_request|direct_base
    ship_policy          jsonb NOT NULL,       -- pinned policy snapshot
    authorizing_principal text NOT NULL,
    connection_id        text,
    expected_remote      jsonb NOT NULL DEFAULT '{}',
    state                text NOT NULL,        -- DeliveryState
    version              integer NOT NULL DEFAULT 1,
    effect_evidence      jsonb NOT NULL DEFAULT '{}',
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, changeset_id)
        REFERENCES changesets(workspace_id, id),
    FOREIGN KEY (workspace_id, session_id)
        REFERENCES sessions(workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX deliveries_changeset ON deliveries(changeset_id);

CREATE TABLE delivery_steps (
    id             text PRIMARY KEY,
    workspace_id   text NOT NULL,
    delivery_id    text NOT NULL,
    ordinal        integer NOT NULL,
    kind           text NOT NULL,   -- verify|push|pull_request|reconcile|export|merge
    effect_id      text NOT NULL,
    expected       jsonb NOT NULL DEFAULT '{}',
    result         jsonb NOT NULL DEFAULT '{}',
    state          text NOT NULL,   -- pending|succeeded|failed|skipped
    observed_at    timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (delivery_id, ordinal),
    UNIQUE (delivery_id, kind, effect_id),
    FOREIGN KEY (workspace_id, delivery_id)
        REFERENCES deliveries(workspace_id, id) ON DELETE CASCADE
);

CREATE TABLE delivery_target_claims (
    id          text PRIMARY KEY,
    workspace_id text NOT NULL,
    repository  text NOT NULL,
    ref_or_pr   text NOT NULL,
    generation  integer NOT NULL,
    holder      text NOT NULL,     -- delivery_id or merge_request_id
    expires_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, repository, ref_or_pr)
);

CREATE TABLE merge_requests (
    id                    text PRIMARY KEY,
    workspace_id          text NOT NULL,
    delivery_id           text NOT NULL,
    changeset_id          text NOT NULL,
    subject_digest        text NOT NULL,
    expected_head_sha     text NOT NULL,
    expected_base_sha     text,
    expected_delivery_version integer NOT NULL,
    merge_method          text NOT NULL,
    authorizing_principal text NOT NULL,
    state                 text NOT NULL,   -- MergeRequestState
    gate_evidence         jsonb NOT NULL DEFAULT '{}',
    result                jsonb NOT NULL DEFAULT '{}',
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, delivery_id)
        REFERENCES deliveries(workspace_id, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX merge_requests_active_per_delivery
    ON merge_requests(delivery_id)
    WHERE state IN ('pending', 'executing', 'blocked');

-- ------------------------------------------------------------- delegations
CREATE TABLE delegations (
    id                 text PRIMARY KEY,
    workspace_id       text NOT NULL,
    parent_session_id  text NOT NULL,
    child_session_id   text NOT NULL UNIQUE,  -- one spawning parent per child
    role               text NOT NULL,
    state              text NOT NULL,         -- DelegationState
    result_contract    jsonb NOT NULL,
    input_refs         jsonb NOT NULL DEFAULT '{}',
    input_digest       text,
    budget             jsonb NOT NULL DEFAULT '{}',
    grant_snapshot     jsonb NOT NULL DEFAULT '{}',
    version            integer NOT NULL DEFAULT 1,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, parent_session_id)
        REFERENCES sessions(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, child_session_id)
        REFERENCES sessions(workspace_id, id) ON DELETE CASCADE,
    CHECK (parent_session_id <> child_session_id)
);
CREATE INDEX delegations_parent ON delegations(parent_session_id, state);

CREATE TABLE delegation_inputs (
    id             text PRIMARY KEY,
    workspace_id   text NOT NULL,
    delegation_id  text NOT NULL,
    kind           text NOT NULL,   -- changeset|blob|sha|summary|instruction
    ref            text NOT NULL,
    digest         text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id, delegation_id)
        REFERENCES delegations(workspace_id, id) ON DELETE CASCADE
);

CREATE TABLE delegation_results (
    id                  text PRIMARY KEY,
    workspace_id        text NOT NULL,
    delegation_id       text NOT NULL UNIQUE,  -- one published final result
    completing_turn_id  text,
    child_session_id    text NOT NULL,
    contract_version    integer NOT NULL,
    subject_digest      text,
    head_sha            text,
    verdict             text,                  -- typed gate field
    validation_status   text NOT NULL,         -- valid|invalid
    value               jsonb NOT NULL,        -- typed ReviewAssessment/etc
    evidence_refs       jsonb NOT NULL DEFAULT '[]',
    published_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, delegation_id)
        REFERENCES delegations(workspace_id, id) ON DELETE CASCADE
);

CREATE TABLE wait_subscriptions (
    id                  text PRIMARY KEY,
    workspace_id        text NOT NULL,
    delegation_id       text NOT NULL,
    subscriber_session_id text NOT NULL,
    predicate           jsonb NOT NULL,        -- e.g. {"result":"terminal"}
    state               text NOT NULL,         -- pending|satisfied|expired|cancelled
    deadline_at         timestamptz,
    satisfied_by_result_id text,
    result_version      integer,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, delegation_id)
        REFERENCES delegations(workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX wait_subscriptions_pending
    ON wait_subscriptions(delegation_id) WHERE state = 'pending';

-- ---------------------------------------------------------------- services
CREATE TABLE service_desires (
    id                 text PRIMARY KEY,
    workspace_id       text NOT NULL,
    session_id         text NOT NULL,
    name               text NOT NULL,
    declaration_digest text NOT NULL,
    desired_state      text NOT NULL,    -- running|stopped
    version            integer NOT NULL DEFAULT 1,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, name),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, session_id)
        REFERENCES sessions(workspace_id, id) ON DELETE CASCADE
);

CREATE TABLE service_instances (
    id                text PRIMARY KEY,
    workspace_id      text NOT NULL,
    session_id        text NOT NULL,
    name              text NOT NULL,
    executor_lease_id text NOT NULL,
    lease_generation  integer NOT NULL,
    state             text NOT NULL,     -- ServiceInstanceState
    observed          jsonb NOT NULL DEFAULT '{}',
    health_at         timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, name, executor_lease_id),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, session_id)
        REFERENCES sessions(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, executor_lease_id)
        REFERENCES executor_leases(workspace_id, id) ON DELETE CASCADE
);

-- ------------------------------------------------------------- jobs/outbox
CREATE TABLE jobs (
    id                text PRIMARY KEY,
    workspace_id      text NOT NULL REFERENCES workspaces(id),
    kind              text NOT NULL,       -- JobKind
    target_family     text NOT NULL,       -- TargetFamily
    -- typed target FK columns; exactly one non-null per family
    turn_id               text,
    execution_id          text,
    executor_lease_id     text,
    snapshot_id           text,
    changeset_id          text,
    delivery_id           text,
    merge_request_id      text,
    delegation_id         text,
    connection_id         text,
    credential_version_id text,
    service_instance_id   text,
    service_desire_id     text,
    worktree_id           text,
    session_id            text,
    project_version_id    text,
    environment_build_id  text,
    outbox_message_id     text,
    effect_id         text NOT NULL UNIQUE,   -- stable across reclaims
    dedupe_key        text NOT NULL,          -- (kind,target,intent_version)
    payload           jsonb NOT NULL DEFAULT '{}',
    priority          integer NOT NULL DEFAULT 100,
    due_at            timestamptz NOT NULL DEFAULT now(),
    deadline_at       timestamptz,
    attempt_limit     integer NOT NULL DEFAULT 8,
    attempts          integer NOT NULL DEFAULT 0,
    state             text NOT NULL DEFAULT 'queued',
    claim_generation  integer NOT NULL DEFAULT 0,
    claim_holder      text,
    claim_expires_at  timestamptz,
    last_error        jsonb,
    result            jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id)
);
CREATE UNIQUE INDEX jobs_active_dedupe ON jobs(dedupe_key)
    WHERE state IN ('queued', 'claimed', 'retry_wait');
CREATE INDEX jobs_due ON jobs(state, due_at, priority)
    WHERE state IN ('queued', 'retry_wait');
CREATE INDEX jobs_claim_expiry ON jobs(claim_expires_at)
    WHERE state = 'claimed';
CREATE INDEX jobs_target ON jobs(target_family, kind);

CREATE TABLE job_attempts (
    id           text PRIMARY KEY,
    workspace_id text NOT NULL,
    job_id       text NOT NULL,
    ordinal      integer NOT NULL,
    holder       text NOT NULL,
    generation   integer NOT NULL,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    outcome      text,       -- succeeded|retry|failed|expired|interrupted
    error        jsonb,
    evidence     jsonb NOT NULL DEFAULT '{}',
    UNIQUE (job_id, ordinal),
    FOREIGN KEY (workspace_id, job_id)
        REFERENCES jobs(workspace_id, id) ON DELETE CASCADE
);

CREATE TABLE outbox_messages (
    id           text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    destination  text NOT NULL,   -- session:<id>|webhook:<id>|principal:<id>
    subscriber   text,
    event_id     text,
    kind         text NOT NULL,   -- wake|notify|webhook
    dedupe_key   text NOT NULL UNIQUE,  -- e.g. (subscription_id,result_version)
    payload      jsonb NOT NULL DEFAULT '{}',
    state        text NOT NULL DEFAULT 'queued',  -- queued|delivered|failed
    due_at       timestamptz NOT NULL DEFAULT now(),
    attempts     integer NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id)
);

CREATE TABLE command_deduplication (
    id             text PRIMARY KEY,
    principal_id   text NOT NULL,
    workspace_id   text NOT NULL,
    command_kind   text NOT NULL,
    key            text NOT NULL,
    request_digest text NOT NULL,
    response       jsonb NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (principal_id, workspace_id, command_kind, key)
);

CREATE TABLE audit_records (
    id          text PRIMARY KEY,
    workspace_id text,
    actor       jsonb NOT NULL,
    action      text NOT NULL,
    purpose     text,
    target_kind text,
    target_id   text,
    target_version integer,
    result      text NOT NULL,
    detail      jsonb NOT NULL DEFAULT '{}',   -- never secret bodies
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_records_target ON audit_records(target_kind, target_id);

INSERT INTO schema_migrations (version, description)
    VALUES (1, 'unified initial schema (RFC 167)')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
