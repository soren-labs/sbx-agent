-- Subscription Machine Slots: one independent official login = one Slot + one private Modal
-- Volume v2 in the owner's workspace. The control plane stores names and states only; the
-- login itself is written by the provider's CLI inside the VM and never reaches this database.
CREATE TABLE machine_slots (
  id text PRIMARY KEY,
  workspace_id text NOT NULL REFERENCES workspaces(id),
  created_by text NOT NULL REFERENCES users(id),
  provider text NOT NULL,
  label text NOT NULL CHECK (length(label) BETWEEN 1 AND 80),
  account_alias text CHECK (account_alias IS NULL OR length(account_alias) <= 80),
  compute_connection_id text NOT NULL REFERENCES connections(id),
  volume_name text NOT NULL,
  -- false when the Slot adopted an existing Volume: SBX then never deletes it.
  volume_managed boolean NOT NULL DEFAULT true,
  state text NOT NULL DEFAULT 'login_pending'
    CHECK (state IN ('login_pending', 'ready', 'needs_login', 'error', 'deleting', 'deleted')),
  state_reason text,
  capabilities jsonb NOT NULL DEFAULT '{}'::jsonb,
  current_login_attempt_id text,
  -- At most one VM (Setup or Worker) holds the Slot's Volume at a time.
  holder_kind text CHECK (holder_kind IN ('setup', 'worker')),
  holder_id text,
  holder_generation bigint NOT NULL DEFAULT 0,
  verified_at timestamptz,
  last_used_at timestamptz,
  version integer NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz,
  CHECK ((holder_kind IS NULL) = (holder_id IS NULL)),
  UNIQUE (workspace_id, id)
);
CREATE INDEX machine_slots_workspace ON machine_slots (workspace_id, provider) WHERE state <> 'deleted';
CREATE UNIQUE INDEX machine_slots_volume ON machine_slots (compute_connection_id, volume_name)
  WHERE state <> 'deleted';

CREATE TABLE slot_login_attempts (
  id text PRIMARY KEY,
  workspace_id text NOT NULL REFERENCES workspaces(id),
  slot_id text NOT NULL REFERENCES machine_slots(id),
  mode text NOT NULL CHECK (mode IN ('login', 'verify')),
  state text NOT NULL DEFAULT 'starting'
    CHECK (state IN ('starting', 'awaiting_user', 'verifying', 'succeeded', 'failed',
                     'expired', 'cancelled')),
  operation_id text NOT NULL UNIQUE,
  handle jsonb,
  verification_url text,
  -- One-time device code, present only while the user still has to enter it.
  user_code text,
  code_expires_at timestamptz,
  deadline_at timestamptz NOT NULL,
  cancel_requested boolean NOT NULL DEFAULT false,
  error_code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  CHECK (user_code IS NULL OR state = 'awaiting_user')
);
CREATE INDEX slot_login_attempts_slot ON slot_login_attempts (slot_id, created_at DESC);
CREATE UNIQUE INDEX slot_login_attempts_one_live ON slot_login_attempts (slot_id)
  WHERE state IN ('starting', 'awaiting_user', 'verifying');

-- Durable Jobs can target a Machine Slot.
ALTER TABLE jobs ADD COLUMN machine_slot_id text REFERENCES machine_slots(id);
ALTER TABLE jobs DROP CONSTRAINT jobs_check;
ALTER TABLE jobs DROP CONSTRAINT jobs_check1;
ALTER TABLE jobs ADD CONSTRAINT jobs_one_target
  CHECK (num_nonnulls(turn_id, execution_id, lease_id, project_version_id, snapshot_id,
    changeset_id, worktree_operation_id, delivery_id, merge_request_id, delegation_id,
    wait_subscription_id, connection_id, service_desire_id, outbox_id, machine_slot_id,
    CASE WHEN target_family = 'session' THEN session_id END) = 1);
ALTER TABLE jobs ADD CONSTRAINT jobs_target_family
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
    WHEN 'machine_slot' THEN machine_slot_id IS NOT NULL
    ELSE false END);
