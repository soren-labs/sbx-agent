-- ChangeSet / Delivery / Delegation / tool grants (RFC 04 tables, RFC 05 semantics).

CREATE TABLE changesets (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  session_id text NOT NULL,
  source_turn_id text REFERENCES turns(id),
  worktree_id text NOT NULL REFERENCES worktrees(id),
  worktree_generation bigint NOT NULL,
  state text NOT NULL DEFAULT 'capturing' CHECK (state IN ('capturing', 'ready', 'failed')),
  origin text NOT NULL CHECK (origin IN ('automatic', 'explicit', 'salvage')),
  automatic_eligible boolean NOT NULL DEFAULT false,
  manifest_version text,
  subject_digest text,
  repository text,
  base_sha text,
  baseline_tree text,
  head_sha text,
  tree_sha text,
  patch_blob_id text,
  file_count integer,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  sealed_at timestamptz,
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, session_id) REFERENCES sessions (workspace_id, id),
  CHECK (state <> 'ready' OR (subject_digest IS NOT NULL AND manifest_version IS NOT NULL AND tree_sha IS NOT NULL))
);
CREATE UNIQUE INDEX changesets_capture_dedupe ON changesets (source_turn_id, worktree_generation, origin)
  WHERE source_turn_id IS NOT NULL AND state <> 'failed';
CREATE INDEX changesets_session ON changesets (session_id, created_at DESC);
CREATE TRIGGER changesets_terminal_guard BEFORE UPDATE OF state ON changesets
  FOR EACH ROW EXECUTE FUNCTION sbx_terminal_guard('ready', 'failed');

CREATE OR REPLACE FUNCTION sbx_changeset_sealed_immutable() RETURNS trigger AS $$
BEGIN
  IF OLD.state = 'ready' AND (NEW.subject_digest IS DISTINCT FROM OLD.subject_digest
      OR NEW.tree_sha IS DISTINCT FROM OLD.tree_sha OR NEW.base_sha IS DISTINCT FROM OLD.base_sha
      OR NEW.patch_blob_id IS DISTINCT FROM OLD.patch_blob_id
      OR NEW.automatic_eligible IS DISTINCT FROM OLD.automatic_eligible) THEN
    RAISE EXCEPTION 'sealed ChangeSet % is immutable', OLD.id USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER changesets_sealed_immutable BEFORE UPDATE ON changesets
  FOR EACH ROW EXECUTE FUNCTION sbx_changeset_sealed_immutable();

CREATE TABLE changeset_files (
  changeset_id text NOT NULL REFERENCES changesets(id),
  path text NOT NULL,
  type text NOT NULL CHECK (type IN ('file', 'symlink', 'deleted')),
  mode text,
  content_digest text,
  blob_id text,
  PRIMARY KEY (changeset_id, path)
);
CREATE TRIGGER changeset_files_append_only BEFORE UPDATE OR DELETE ON changeset_files
  FOR EACH ROW EXECUTE FUNCTION sbx_reject_mutation();

CREATE TABLE deliveries (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  session_id text NOT NULL,
  changeset_id text NOT NULL REFERENCES changesets(id),
  subject_digest text NOT NULL,
  transport text NOT NULL CHECK (transport IN ('export', 'git_branch', 'pull_request', 'direct_base')),
  repository text NOT NULL,
  clone_url text NOT NULL,
  target_ref text NOT NULL,
  base_branch text NOT NULL,
  draft boolean NOT NULL DEFAULT true,
  title text NOT NULL,
  connection_id text REFERENCES connections(id),
  authorizing_principal text NOT NULL,
  authorization_kind text NOT NULL CHECK (authorization_kind IN ('explicit', 'automatic')),
  policy jsonb NOT NULL,
  state text NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending', 'executing', 'blocked', 'succeeded', 'failed', 'cancelled')),
  state_reason text,
  expected_old_head text,
  commit_sha text,
  pr_number integer,
  pr_url text,
  remote_head_sha text,
  pr_state text,
  pr_draft boolean,
  checks_state text,
  checks jsonb,
  observed_at timestamptz,
  version integer NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, session_id) REFERENCES sessions (workspace_id, id)
);
CREATE INDEX deliveries_changeset ON deliveries (changeset_id);

CREATE TABLE delivery_steps (
  id text PRIMARY KEY,
  delivery_id text NOT NULL REFERENCES deliveries(id),
  kind text NOT NULL,
  effect_id text NOT NULL,
  outcome text NOT NULL,
  expected jsonb NOT NULL DEFAULT '{}'::jsonb,
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  credential_version_id text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER delivery_steps_append_only BEFORE UPDATE OR DELETE ON delivery_steps
  FOR EACH ROW EXECUTE FUNCTION sbx_reject_mutation();

CREATE TABLE delivery_target_claims (
  target text PRIMARY KEY,
  generation bigint NOT NULL DEFAULT 1,
  holder_delivery_id text,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE merge_requests (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  delivery_id text NOT NULL REFERENCES deliveries(id),
  expected_head_sha text NOT NULL,
  expected_delivery_version integer NOT NULL,
  subject_digest text NOT NULL,
  method text NOT NULL CHECK (method IN ('merge', 'squash', 'rebase')),
  mark_ready boolean NOT NULL DEFAULT false,
  authorizing_principal text NOT NULL,
  state text NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending', 'executing', 'blocked', 'succeeded', 'failed', 'cancelled')),
  gate jsonb,
  merge_sha text,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX merge_requests_one_active ON merge_requests (delivery_id)
  WHERE state IN ('pending', 'executing');

CREATE TABLE delegations (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  parent_session_id text NOT NULL REFERENCES sessions(id),
  child_session_id text NOT NULL UNIQUE REFERENCES sessions(id),
  role text NOT NULL,
  state text NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending', 'active', 'waiting_result', 'succeeded', 'failed', 'cancelled')),
  state_reason text,
  result_contract jsonb NOT NULL,
  contract_digest text NOT NULL,
  subject_changeset_id text REFERENCES changesets(id),
  subject_digest text,
  depth integer NOT NULL,
  budget jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by text NOT NULL,
  cancel_requested_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (parent_session_id <> child_session_id)
);
CREATE INDEX delegations_parent ON delegations (parent_session_id, state);
CREATE TRIGGER delegations_terminal_guard BEFORE UPDATE OF state ON delegations
  FOR EACH ROW EXECUTE FUNCTION sbx_terminal_guard('succeeded', 'failed', 'cancelled');

CREATE TABLE delegation_inputs (
  id text PRIMARY KEY,
  delegation_id text NOT NULL REFERENCES delegations(id),
  kind text NOT NULL CHECK (kind IN ('changeset', 'blob', 'repository_sha', 'summary')),
  ref text NOT NULL,
  digest text,
  apply_to_worktree boolean NOT NULL DEFAULT false,
  applied_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE delegation_results (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  delegation_id text NOT NULL UNIQUE REFERENCES delegations(id),
  child_session_id text NOT NULL REFERENCES sessions(id),
  completing_turn_id text NOT NULL REFERENCES turns(id),
  kind text NOT NULL,
  contract_version integer NOT NULL,
  subject_digest text,
  head_sha text,
  verdict text,
  validation_status text NOT NULL CHECK (validation_status IN ('valid')),
  independent boolean NOT NULL,
  value jsonb NOT NULL,
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  published_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER delegation_results_append_only BEFORE UPDATE OR DELETE ON delegation_results
  FOR EACH ROW EXECUTE FUNCTION sbx_reject_mutation();

CREATE TABLE wait_subscriptions (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  delegation_id text NOT NULL REFERENCES delegations(id),
  waiter_session_id text NOT NULL REFERENCES sessions(id),
  wake text NOT NULL CHECK (wake IN ('message', 'none')),
  state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'satisfied', 'expired', 'cancelled')),
  deadline_at timestamptz,
  evidence jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  satisfied_at timestamptz
);

CREATE TABLE tool_grants (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  session_id text NOT NULL REFERENCES sessions(id),
  execution_id text REFERENCES executions(id),
  principal_id text NOT NULL,
  token_hash text NOT NULL UNIQUE,
  actions text[] NOT NULL,
  max_depth integer NOT NULL DEFAULT 2,
  max_children integer NOT NULL DEFAULT 5,
  auth_epoch integer NOT NULL,
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE jobs ADD CONSTRAINT jobs_changeset_fk FOREIGN KEY (changeset_id) REFERENCES changesets(id);
ALTER TABLE jobs ADD CONSTRAINT jobs_delivery_fk FOREIGN KEY (delivery_id) REFERENCES deliveries(id);
ALTER TABLE jobs ADD CONSTRAINT jobs_merge_request_fk FOREIGN KEY (merge_request_id) REFERENCES merge_requests(id);
ALTER TABLE jobs ADD CONSTRAINT jobs_delegation_fk FOREIGN KEY (delegation_id) REFERENCES delegations(id);
ALTER TABLE jobs ADD CONSTRAINT jobs_wait_fk FOREIGN KEY (wait_subscription_id) REFERENCES wait_subscriptions(id);
