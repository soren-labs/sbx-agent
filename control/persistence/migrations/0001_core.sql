-- Unified SBX core schema (RFC 04 relational tables and invariants).
-- Typed columns for every authorization/constraint/gate field; JSONB only for
-- versioned payload/spec/capability/result extensions.

CREATE OR REPLACE FUNCTION sbx_reject_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'append-only table %: % rejected', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'check_violation';
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION sbx_terminal_guard() RETURNS trigger AS $$
BEGIN
  IF OLD.state = ANY (TG_ARGV::text[]) AND NEW.state IS DISTINCT FROM OLD.state THEN
    RAISE EXCEPTION 'terminal state % of %.% is immutable', OLD.state, TG_TABLE_NAME, OLD.id
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

-- Identity -------------------------------------------------------------------
CREATE TABLE users (
  id text PRIMARY KEY,
  email text NOT NULL UNIQUE,
  email_verified_at timestamptz,
  identity_version integer NOT NULL DEFAULT 1,
  auth_epoch integer NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE password_credentials (
  user_id text PRIMARY KEY REFERENCES users(id),
  algorithm text NOT NULL,
  hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE email_verifications (
  id text PRIMARY KEY,
  user_id text NOT NULL REFERENCES users(id),
  purpose text NOT NULL CHECK (purpose IN ('verify_email', 'password_reset')),
  token_hash text NOT NULL UNIQUE,
  expires_at timestamptz NOT NULL,
  consumed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE login_attempts (
  id bigserial PRIMARY KEY,
  email text NOT NULL,
  succeeded boolean NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX login_attempts_email ON login_attempts (email, created_at);

CREATE TABLE workspaces (
  id text PRIMARY KEY,
  owner_user_id text NOT NULL REFERENCES users(id),
  kind text NOT NULL DEFAULT 'personal' CHECK (kind IN ('personal', 'team')),
  name text NOT NULL,
  policy_version integer NOT NULL DEFAULT 1,
  policy jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX workspaces_personal_owner ON workspaces (owner_user_id) WHERE kind = 'personal';

CREATE TABLE workspace_memberships (
  workspace_id text NOT NULL REFERENCES workspaces(id),
  user_id text NOT NULL REFERENCES users(id),
  role text NOT NULL CHECK (role IN ('owner', 'member')),
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'removed')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, user_id)
);
CREATE INDEX workspace_memberships_user ON workspace_memberships (user_id);

CREATE TABLE login_sessions (
  id text PRIMARY KEY,
  user_id text NOT NULL REFERENCES users(id),
  token_hash text NOT NULL UNIQUE,
  csrf_hash text NOT NULL,
  auth_epoch integer NOT NULL,
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX login_sessions_user ON login_sessions (user_id, expires_at);

CREATE TABLE api_keys (
  id text PRIMARY KEY,
  user_id text NOT NULL REFERENCES users(id),
  workspace_id text NOT NULL REFERENCES workspaces(id),
  name text NOT NULL,
  key_hash text NOT NULL UNIQUE,
  display_prefix text NOT NULL,
  scopes text[] NOT NULL DEFAULT ARRAY['*'],
  expires_at timestamptz,
  revoked_at timestamptz,
  last_used_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX api_keys_user ON api_keys (user_id);

-- Projects ---------------------------------------------------------------------
CREATE TABLE projects (
  id text PRIMARY KEY,
  workspace_id text NOT NULL REFERENCES workspaces(id),
  slug text NOT NULL,
  name text NOT NULL,
  current_version_id text,
  version integer NOT NULL DEFAULT 1,
  created_by text NOT NULL REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, slug),
  UNIQUE (workspace_id, id)
);

CREATE TABLE project_versions (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  project_id text NOT NULL,
  ordinal integer NOT NULL,
  repo_provider text,
  repo_owner text,
  repo_name text,
  base_ref text,
  spec jsonb NOT NULL,
  spec_digest text NOT NULL,
  created_by text NOT NULL REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, ordinal),
  UNIQUE (project_id, id),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, project_id) REFERENCES projects (workspace_id, id)
);
CREATE TRIGGER project_versions_immutable BEFORE UPDATE OR DELETE ON project_versions
  FOR EACH ROW EXECUTE FUNCTION sbx_reject_mutation();
ALTER TABLE projects ADD CONSTRAINT projects_current_version_same_project
  FOREIGN KEY (id, current_version_id) REFERENCES project_versions (project_id, id)
  DEFERRABLE INITIALLY DEFERRED;

-- Connections ------------------------------------------------------------------
CREATE TABLE connections (
  id text PRIMARY KEY,
  workspace_id text NOT NULL REFERENCES workspaces(id),
  kind text NOT NULL CHECK (kind IN ('modal', 'github', 'opencode_zen', 'codex')),
  label text NOT NULL,
  created_by text NOT NULL REFERENCES users(id),
  acquisition text NOT NULL DEFAULT 'manual'
    CHECK (acquisition IN ('manual', 'native_upload', 'oauth', 'device')),
  config_state text NOT NULL DEFAULT 'configured'
    CHECK (config_state IN ('configured', 'disabled', 'revoked')),
  health text NOT NULL DEFAULT 'unverified'
    CHECK (health IN ('unverified', 'verifying', 'ready', 'degraded', 'reauth_required')),
  health_reason text,
  current_credential_version_id text,
  external_identity text,
  priority integer NOT NULL DEFAULT 0,
  slot_limit integer NOT NULL DEFAULT 4 CHECK (slot_limit > 0),
  cooldown_until timestamptz,
  version integer NOT NULL DEFAULT 1,
  revocation_epoch integer NOT NULL DEFAULT 1,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, id)
);
CREATE INDEX connections_workspace_kind ON connections (workspace_id, kind, config_state);

CREATE TABLE credential_versions (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  connection_id text NOT NULL,
  ordinal integer NOT NULL,
  format text NOT NULL,
  key_id text NOT NULL,
  nonce bytea NOT NULL,
  ciphertext bytea NOT NULL,
  fingerprint text NOT NULL,
  expires_at timestamptz,
  revoked_at timestamptz,
  created_by text NOT NULL REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (connection_id, ordinal),
  UNIQUE (connection_id, id),
  FOREIGN KEY (workspace_id, connection_id) REFERENCES connections (workspace_id, id)
);
ALTER TABLE connections ADD CONSTRAINT connections_current_credential_same_connection
  FOREIGN KEY (id, current_credential_version_id) REFERENCES credential_versions (connection_id, id)
  DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE connection_observations (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  connection_id text NOT NULL,
  credential_version_id text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('validation', 'catalog')),
  status text NOT NULL CHECK (status IN ('ready', 'invalid', 'degraded', 'error')),
  connection_version integer NOT NULL,
  safe_details jsonb NOT NULL DEFAULT '{}'::jsonb,
  quota_consuming boolean NOT NULL DEFAULT false,
  observed_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz,
  FOREIGN KEY (connection_id, credential_version_id) REFERENCES credential_versions (connection_id, id),
  FOREIGN KEY (workspace_id, connection_id) REFERENCES connections (workspace_id, id)
);
CREATE INDEX connection_observations_latest ON connection_observations (connection_id, kind, observed_at DESC);

CREATE TABLE credential_grants (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  connection_id text NOT NULL,
  credential_version_id text NOT NULL,
  purpose text NOT NULL CHECK (purpose IN ('compute', 'inference', 'source_control', 'teardown')),
  principal_id text NOT NULL,
  session_id text,
  execution_id text,
  lease_id text,
  revocation_epoch integer NOT NULL,
  expires_at timestamptz NOT NULL,
  redeemed_at timestamptz,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (connection_id, credential_version_id) REFERENCES credential_versions (connection_id, id)
);

-- Sessions and journal -----------------------------------------------------------
CREATE TABLE sessions (
  id text PRIMARY KEY,
  workspace_id text NOT NULL REFERENCES workspaces(id),
  lifecycle text NOT NULL DEFAULT 'open' CHECK (lifecycle IN ('open', 'archived', 'closed')),
  role text NOT NULL DEFAULT 'developer',
  title text NOT NULL DEFAULT '',
  labels text[] NOT NULL DEFAULT '{}',
  created_by text NOT NULL REFERENCES users(id),
  project_id text,
  project_version_id text,
  harness_provider text NOT NULL,
  harness_model text,
  executor_backend text NOT NULL,
  resource_class text NOT NULL DEFAULT 'standard',
  compute_connection_id text,
  inference_connection_id text,
  source_connection_id text,
  effective_spec jsonb NOT NULL,
  effective_spec_digest text NOT NULL,
  parent_session_id text REFERENCES sessions(id),
  linked_from_session_id text REFERENCES sessions(id),
  active_turn_id text,
  active_lease_id text,
  version integer NOT NULL DEFAULT 1,
  next_event_seq bigint NOT NULL DEFAULT 1,
  next_message_ordinal integer NOT NULL DEFAULT 1,
  next_turn_ordinal integer NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz,
  closed_at timestamptz,
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, project_version_id) REFERENCES project_versions (workspace_id, id),
  FOREIGN KEY (workspace_id, compute_connection_id) REFERENCES connections (workspace_id, id),
  FOREIGN KEY (workspace_id, inference_connection_id) REFERENCES connections (workspace_id, id),
  FOREIGN KEY (workspace_id, source_connection_id) REFERENCES connections (workspace_id, id)
);
CREATE INDEX sessions_workspace_updated ON sessions (workspace_id, updated_at DESC, id DESC);
CREATE INDEX sessions_parent ON sessions (parent_session_id);

CREATE TABLE messages (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  session_id text NOT NULL,
  ordinal integer NOT NULL,
  author_kind text NOT NULL CHECK (author_kind IN ('user', 'agent', 'system', 'session')),
  author_id text,
  role text NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
  routing text NOT NULL CHECK (routing IN ('note', 'queue', 'steer', 'output')),
  content jsonb NOT NULL,
  content_digest text NOT NULL,
  source_message_id text REFERENCES messages(id),
  source_session_id text REFERENCES sessions(id),
  reply_to_message_id text REFERENCES messages(id),
  turn_id text,
  state text NOT NULL DEFAULT 'accepted' CHECK (state IN ('accepted', 'streaming', 'completed')),
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  UNIQUE (session_id, ordinal),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, session_id) REFERENCES sessions (workspace_id, id)
);

CREATE OR REPLACE FUNCTION sbx_message_content_immutable() RETURNS trigger AS $$
BEGIN
  IF OLD.role = 'user' AND (NEW.content IS DISTINCT FROM OLD.content
     OR NEW.content_digest IS DISTINCT FROM OLD.content_digest) THEN
    RAISE EXCEPTION 'accepted input content is immutable' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER messages_content_immutable BEFORE UPDATE ON messages
  FOR EACH ROW EXECUTE FUNCTION sbx_message_content_immutable();

CREATE TABLE message_parts (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  session_id text NOT NULL,
  message_id text NOT NULL REFERENCES messages(id),
  part_key text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('text', 'reasoning', 'tool', 'file', 'diagnostic')),
  ordinal integer NOT NULL,
  revision integer NOT NULL DEFAULT 1,
  content text NOT NULL DEFAULT '',
  data jsonb NOT NULL DEFAULT '{}'::jsonb,
  sealed boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (message_id, part_key),
  FOREIGN KEY (workspace_id, session_id) REFERENCES sessions (workspace_id, id)
);

CREATE TABLE turns (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  session_id text NOT NULL,
  ordinal integer NOT NULL,
  triggering_message_id text NOT NULL UNIQUE REFERENCES messages(id),
  state text NOT NULL DEFAULT 'queued' CHECK (state IN
    ('queued', 'preparing', 'running', 'cancelling', 'succeeded', 'failed', 'cancelled', 'interrupted')),
  reason text,
  settings jsonb NOT NULL,
  result_contract jsonb,
  retry_of_turn_id text REFERENCES turns(id),
  cancel_requested_at timestamptz,
  cancel_actor text,
  error_code text,
  error_message text,
  evidence_complete boolean,
  outcome jsonb,
  usage jsonb,
  output_message_id text REFERENCES messages(id),
  unknown_acknowledged_at timestamptz,
  version integer NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  UNIQUE (session_id, ordinal),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, session_id) REFERENCES sessions (workspace_id, id)
);
CREATE UNIQUE INDEX turns_one_active_per_session ON turns (session_id)
  WHERE state IN ('preparing', 'running', 'cancelling');
CREATE INDEX turns_session_state ON turns (session_id, state, ordinal);
CREATE TRIGGER turns_terminal_guard BEFORE UPDATE OF state ON turns
  FOR EACH ROW EXECUTE FUNCTION sbx_terminal_guard('succeeded', 'failed', 'cancelled', 'interrupted');
ALTER TABLE messages ADD CONSTRAINT messages_turn_fk FOREIGN KEY (turn_id) REFERENCES turns(id);

CREATE TABLE session_events (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  session_id text NOT NULL,
  seq bigint NOT NULL,
  type text NOT NULL,
  schema_version integer NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  observed_at timestamptz,
  actor text NOT NULL,
  source text NOT NULL,
  causation_id text,
  correlation_id text,
  turn_id text,
  execution_id text,
  executor_lease_id text,
  lease_generation integer,
  delegation_id text,
  changeset_id text,
  delivery_id text,
  runtime_epoch text,
  local_seq bigint,
  payload jsonb NOT NULL,
  UNIQUE (session_id, seq),
  FOREIGN KEY (workspace_id, session_id) REFERENCES sessions (workspace_id, id)
);
CREATE UNIQUE INDEX session_events_runtime_source ON session_events
  (executor_lease_id, runtime_epoch, local_seq) WHERE local_seq IS NOT NULL;
CREATE INDEX session_events_turn ON session_events (turn_id);
CREATE TRIGGER session_events_append_only BEFORE UPDATE OR DELETE ON session_events
  FOR EACH ROW EXECUTE FUNCTION sbx_reject_mutation();

-- Execution, leases, capacity -------------------------------------------------------
CREATE TABLE executor_leases (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  session_id text NOT NULL,
  backend text NOT NULL,
  allocation_operation_id text NOT NULL UNIQUE,
  generation integer NOT NULL,
  state text NOT NULL DEFAULT 'allocating'
    CHECK (state IN ('allocating', 'ready', 'quiescing', 'released', 'lost')),
  state_reason text,
  handle jsonb,
  compute_connection_id text,
  compute_credential_version_id text,
  image_digest text NOT NULL,
  resource_class text NOT NULL,
  protocol_version text,
  runtime_epoch text,
  capabilities jsonb,
  quarantined boolean NOT NULL DEFAULT false,
  expires_at timestamptz,
  observed_status text,
  observed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  bound_at timestamptz,
  released_at timestamptz,
  UNIQUE (workspace_id, id),
  UNIQUE (session_id, generation),
  FOREIGN KEY (workspace_id, session_id) REFERENCES sessions (workspace_id, id),
  FOREIGN KEY (workspace_id, compute_connection_id) REFERENCES connections (workspace_id, id)
);
CREATE UNIQUE INDEX executor_leases_one_live ON executor_leases (session_id)
  WHERE state IN ('allocating', 'ready', 'quiescing');
CREATE INDEX executor_leases_connection_live ON executor_leases (compute_connection_id)
  WHERE state IN ('allocating', 'ready', 'quiescing');
CREATE TRIGGER executor_leases_terminal_guard BEFORE UPDATE OF state ON executor_leases
  FOR EACH ROW EXECUTE FUNCTION sbx_terminal_guard('released', 'lost');

CREATE TABLE executions (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  session_id text NOT NULL,
  turn_id text NOT NULL REFERENCES turns(id),
  attempt_ordinal integer NOT NULL,
  executor_lease_id text REFERENCES executor_leases(id),
  operation_id text NOT NULL UNIQUE,
  state text NOT NULL DEFAULT 'preparing' CHECK (state IN
    ('preparing', 'started', 'stop_requested', 'succeeded', 'failed', 'cancelled', 'unknown')),
  launch_evidence text NOT NULL DEFAULT 'none'
    CHECK (launch_evidence IN ('none', 'accepted', 'started', 'unknown')),
  inference_connection_id text,
  credential_version_id text,
  harness_provider text NOT NULL,
  cli_version text,
  adapter_version text,
  image_digest text,
  runtime_epoch text,
  native_binding_id text,
  final_watermark bigint,
  outcome jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  UNIQUE (turn_id, attempt_ordinal),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, session_id) REFERENCES sessions (workspace_id, id)
);
CREATE UNIQUE INDEX executions_one_live_per_turn ON executions (turn_id)
  WHERE state IN ('preparing', 'started', 'stop_requested');
CREATE TRIGGER executions_terminal_guard BEFORE UPDATE OF state ON executions
  FOR EACH ROW EXECUTE FUNCTION sbx_terminal_guard('succeeded', 'failed', 'cancelled', 'unknown');

CREATE TABLE native_context_bindings (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  session_id text NOT NULL,
  provider_id text NOT NULL,
  native_id text NOT NULL,
  lineage_id text NOT NULL,
  cli_version text,
  adapter_version text,
  state_manifest_digest text,
  account_affinity text,
  checkpoint_ref text,
  execution_id text NOT NULL REFERENCES executions(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, session_id) REFERENCES sessions (workspace_id, id)
);
CREATE INDEX native_context_bindings_session ON native_context_bindings (session_id, created_at DESC);

CREATE TABLE runtime_ingestion_offsets (
  executor_lease_id text NOT NULL REFERENCES executor_leases(id),
  runtime_epoch text NOT NULL,
  acked_local_seq bigint NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (executor_lease_id, runtime_epoch)
);

CREATE TABLE resource_fences (
  resource text PRIMARY KEY,
  epoch bigint NOT NULL DEFAULT 1,
  holder text,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE capacity_reservations (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  connection_id text NOT NULL,
  slot_ordinal integer NOT NULL,
  execution_id text REFERENCES executions(id),
  lease_id text REFERENCES executor_leases(id),
  state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'quarantined', 'released')),
  expires_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  released_at timestamptz,
  FOREIGN KEY (workspace_id, connection_id) REFERENCES connections (workspace_id, id)
);
CREATE UNIQUE INDEX capacity_reservations_one_slot ON capacity_reservations (connection_id, slot_ordinal)
  WHERE state IN ('active', 'quarantined');

-- Worktree, snapshots, blobs ------------------------------------------------------------
CREATE TABLE worktrees (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  session_id text NOT NULL UNIQUE,
  repository text,
  base_ref text,
  base_sha text,
  baseline_digest text,
  generation bigint NOT NULL DEFAULT 0,
  availability text NOT NULL DEFAULT 'none'
    CHECK (availability IN ('none', 'restoring', 'live', 'checkpointed', 'unavailable')),
  last_snapshot_id text,
  recovery_point jsonb,
  version integer NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, session_id) REFERENCES sessions (workspace_id, id)
);

CREATE TABLE worktree_operations (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  worktree_id text NOT NULL REFERENCES worktrees(id),
  kind text NOT NULL CHECK (kind IN ('capture', 'checkpoint', 'apply', 'restore')),
  state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'completed', 'aborted')),
  expected_generation bigint,
  input jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);
CREATE UNIQUE INDEX worktree_operations_one_barrier ON worktree_operations (worktree_id)
  WHERE state = 'active';

CREATE TABLE snapshots (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('environment', 'checkpoint')),
  state text NOT NULL DEFAULT 'preparing' CHECK (state IN ('preparing', 'ready', 'failed')),
  project_version_id text,
  worktree_id text REFERENCES worktrees(id),
  worktree_generation bigint,
  input_digest text,
  content_digest text,
  manifest jsonb,
  backend_ref text,
  compatibility jsonb,
  event_watermark bigint,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  sealed_at timestamptz,
  CHECK ((kind = 'environment' AND project_version_id IS NOT NULL AND worktree_id IS NULL)
      OR (kind = 'checkpoint' AND worktree_id IS NOT NULL AND project_version_id IS NULL)),
  FOREIGN KEY (workspace_id, project_version_id) REFERENCES project_versions (workspace_id, id)
);
CREATE TRIGGER snapshots_terminal_guard BEFORE UPDATE OF state ON snapshots
  FOR EACH ROW EXECUTE FUNCTION sbx_terminal_guard('ready', 'failed');

CREATE TABLE blobs (
  id text PRIMARY KEY,
  workspace_id text NOT NULL REFERENCES workspaces(id),
  storage_key text NOT NULL UNIQUE,
  digest text NOT NULL,
  size_bytes bigint NOT NULL,
  content_class text NOT NULL,
  state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'sealed', 'tombstoned')),
  created_at timestamptz NOT NULL DEFAULT now(),
  sealed_at timestamptz,
  UNIQUE (workspace_id, id)
);

CREATE TABLE blob_references (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  blob_id text NOT NULL,
  entity_kind text NOT NULL,
  entity_id text NOT NULL,
  retention text NOT NULL DEFAULT 'entity',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (blob_id, entity_kind, entity_id),
  FOREIGN KEY (workspace_id, blob_id) REFERENCES blobs (workspace_id, id)
);

-- Jobs, outbox, dedupe, audit ------------------------------------------------------------
CREATE TABLE jobs (
  id text PRIMARY KEY,
  workspace_id text NOT NULL REFERENCES workspaces(id),
  kind text NOT NULL,
  target_family text NOT NULL,
  turn_id text REFERENCES turns(id),
  execution_id text REFERENCES executions(id),
  lease_id text REFERENCES executor_leases(id),
  project_version_id text REFERENCES project_versions(id),
  snapshot_id text REFERENCES snapshots(id),
  changeset_id text,
  worktree_operation_id text REFERENCES worktree_operations(id),
  delivery_id text,
  merge_request_id text,
  delegation_id text,
  wait_subscription_id text,
  connection_id text REFERENCES connections(id),
  service_desire_id text,
  session_id text REFERENCES sessions(id),
  outbox_id text,
  dedupe_key text NOT NULL,
  effect_id text NOT NULL,
  input jsonb NOT NULL DEFAULT '{}'::jsonb,
  input_version integer NOT NULL DEFAULT 1,
  priority integer NOT NULL DEFAULT 40,
  state text NOT NULL DEFAULT 'queued'
    CHECK (state IN ('queued', 'claimed', 'retry_wait', 'succeeded', 'failed', 'cancelled')),
  due_at timestamptz NOT NULL DEFAULT now(),
  deadline_at timestamptz,
  attempts integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 20,
  claim_generation integer NOT NULL DEFAULT 0,
  claim_holder text,
  claim_expires_at timestamptz,
  last_error_code text,
  last_error text,
  result jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  CHECK (num_nonnulls(turn_id, execution_id, lease_id, project_version_id, snapshot_id,
    changeset_id, worktree_operation_id, delivery_id, merge_request_id, delegation_id,
    wait_subscription_id, connection_id, service_desire_id, outbox_id,
    CASE WHEN target_family = 'session' THEN session_id END) = 1),
  CHECK (CASE target_family
    WHEN 'turn' THEN turn_id IS NOT NULL
    WHEN 'execution' THEN execution_id IS NOT NULL
    WHEN 'lease' THEN lease_id IS NOT NULL
    WHEN 'project_version' THEN project_version_id IS NOT NULL
    WHEN 'snapshot' THEN snapshot_id IS NOT NULL
    WHEN 'changeset' THEN changeset_id IS NOT NULL
    WHEN 'worktree_operation' THEN worktree_operation_id IS NOT NULL
    WHEN 'delivery' THEN delivery_id IS NOT NULL
    WHEN 'merge_request' THEN merge_request_id IS NOT NULL
    WHEN 'delegation' THEN delegation_id IS NOT NULL
    WHEN 'wait_subscription' THEN wait_subscription_id IS NOT NULL
    WHEN 'connection' THEN connection_id IS NOT NULL
    WHEN 'service_desire' THEN service_desire_id IS NOT NULL
    WHEN 'session' THEN session_id IS NOT NULL
    WHEN 'outbox' THEN outbox_id IS NOT NULL
    ELSE false END)
);
CREATE UNIQUE INDEX jobs_active_dedupe ON jobs (kind, dedupe_key)
  WHERE state IN ('queued', 'claimed', 'retry_wait');
CREATE INDEX jobs_due ON jobs (priority DESC, due_at) WHERE state IN ('queued', 'retry_wait');
CREATE INDEX jobs_claim_expiry ON jobs (claim_expires_at) WHERE state = 'claimed';
CREATE TRIGGER jobs_terminal_guard BEFORE UPDATE OF state ON jobs
  FOR EACH ROW EXECUTE FUNCTION sbx_terminal_guard('succeeded', 'failed', 'cancelled');

CREATE TABLE job_attempts (
  id text PRIMARY KEY,
  job_id text NOT NULL REFERENCES jobs(id),
  generation integer NOT NULL,
  holder text NOT NULL,
  reclaimed boolean NOT NULL DEFAULT false,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  outcome text,
  error_code text,
  UNIQUE (job_id, generation)
);

CREATE TABLE outbox_messages (
  id text PRIMARY KEY,
  workspace_id text NOT NULL REFERENCES workspaces(id),
  destination text NOT NULL,
  dedupe_key text NOT NULL UNIQUE,
  session_id text REFERENCES sessions(id),
  event_seq bigint,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'delivered', 'dead_letter')),
  created_at timestamptz NOT NULL DEFAULT now(),
  delivered_at timestamptz
);

CREATE TABLE command_deduplication (
  principal_id text NOT NULL,
  workspace_id text NOT NULL,
  command_kind text NOT NULL,
  key text NOT NULL,
  request_digest text NOT NULL,
  response jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  PRIMARY KEY (principal_id, workspace_id, command_kind, key)
);

CREATE TABLE audit_records (
  id text PRIMARY KEY,
  workspace_id text,
  actor text NOT NULL,
  action text NOT NULL,
  purpose text,
  target_kind text NOT NULL,
  target_id text NOT NULL,
  target_version integer,
  result text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER audit_records_append_only BEFORE UPDATE OR DELETE ON audit_records
  FOR EACH ROW EXECUTE FUNCTION sbx_reject_mutation();
