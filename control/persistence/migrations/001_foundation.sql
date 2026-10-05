CREATE TABLE users (
 id text PRIMARY KEY, email text UNIQUE NOT NULL, verified_at timestamptz,
 identity_version integer NOT NULL DEFAULT 1, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE password_credentials (
 user_id text PRIMARY KEY REFERENCES users, hash text NOT NULL,
 algorithm text NOT NULL DEFAULT 'argon2id', version integer NOT NULL DEFAULT 1
);
CREATE TABLE workspaces (
 id text PRIMARY KEY, owner_id text UNIQUE NOT NULL REFERENCES users, name text NOT NULL,
 policy_version integer NOT NULL DEFAULT 1, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE workspace_memberships (
 workspace_id text REFERENCES workspaces, user_id text REFERENCES users,
 role text NOT NULL DEFAULT 'owner', status text NOT NULL DEFAULT 'active',
 PRIMARY KEY(workspace_id,user_id)
);
CREATE TABLE login_sessions (
 id text PRIMARY KEY, user_id text NOT NULL REFERENCES users, token_hash text UNIQUE NOT NULL,
 csrf_hash text NOT NULL, auth_epoch integer NOT NULL, expires_at timestamptz NOT NULL,
 revoked_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE email_verifications (
 id text PRIMARY KEY, user_id text NOT NULL REFERENCES users, token_hash text UNIQUE NOT NULL,
 purpose text NOT NULL DEFAULT 'verify', expires_at timestamptz NOT NULL, used_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE api_keys (
 id text PRIMARY KEY, user_id text NOT NULL REFERENCES users, workspace_id text REFERENCES workspaces,
 key_hash text UNIQUE NOT NULL, scopes text[] NOT NULL, revoked_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE projects (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, slug text NOT NULL,
 name text NOT NULL, current_version_id text, version integer NOT NULL DEFAULT 1,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(workspace_id,id), UNIQUE(workspace_id,slug)
);
CREATE TABLE project_versions (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, project_id text NOT NULL,
 ordinal integer NOT NULL, repository text NOT NULL, base_ref text NOT NULL,
 spec jsonb NOT NULL, spec_digest text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(workspace_id,id), UNIQUE(project_id,ordinal), UNIQUE(project_id,id),
 FOREIGN KEY(workspace_id,project_id) REFERENCES projects(workspace_id,id)
);
ALTER TABLE projects ADD FOREIGN KEY(id,current_version_id) REFERENCES project_versions(project_id,id);
CREATE TABLE connections (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces,
 creator_id text NOT NULL REFERENCES users, kind text NOT NULL,
 label text NOT NULL, acquisition_method text NOT NULL DEFAULT 'manual',
 state text NOT NULL DEFAULT 'configured' CHECK(state IN ('configured','disabled','revoked')),
 version integer NOT NULL DEFAULT 1, revocation_epoch integer NOT NULL DEFAULT 1,
 current_credential_id text, health text NOT NULL DEFAULT 'unverified',
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(workspace_id,id)
);
CREATE TABLE credential_versions (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces,
 connection_id text NOT NULL, ordinal integer NOT NULL, format text NOT NULL,
 envelope jsonb NOT NULL, expires_at timestamptz, revoked_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(workspace_id,id),
 UNIQUE(connection_id,ordinal), UNIQUE(connection_id,id),
 FOREIGN KEY(workspace_id,connection_id) REFERENCES connections(workspace_id,id)
);
ALTER TABLE connections ADD FOREIGN KEY(id,current_credential_id) REFERENCES credential_versions(connection_id,id);
CREATE TABLE connection_observations (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, connection_id text NOT NULL,
 credential_id text NOT NULL, scope text NOT NULL, status text NOT NULL,
 capabilities jsonb NOT NULL DEFAULT '{}', observed_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(workspace_id,connection_id) REFERENCES connections(workspace_id,id),
 FOREIGN KEY(connection_id,credential_id) REFERENCES credential_versions(connection_id,id)
);
CREATE TABLE sessions (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces,
 creator_id text NOT NULL REFERENCES users, project_version_id text,
 lifecycle text NOT NULL DEFAULT 'open' CHECK(lifecycle IN ('open','archived','closed')),
 role text NOT NULL DEFAULT 'developer', title text NOT NULL, provider_id text NOT NULL,
 effective_inputs jsonb NOT NULL DEFAULT '{}', version integer NOT NULL DEFAULT 1,
 next_event_seq bigint NOT NULL DEFAULT 1, next_message_ordinal bigint NOT NULL DEFAULT 1,
 next_turn_ordinal bigint NOT NULL DEFAULT 1, linked_from_session_id text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(workspace_id,id),
 FOREIGN KEY(workspace_id,project_version_id) REFERENCES project_versions(workspace_id,id),
 FOREIGN KEY(workspace_id,linked_from_session_id) REFERENCES sessions(workspace_id,id)
);
CREATE TABLE messages (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, session_id text NOT NULL,
 ordinal bigint NOT NULL, author_id text NOT NULL REFERENCES users,
 routing text NOT NULL CHECK(routing IN ('note','queue','steer')), content text NOT NULL,
 source_session_id text, source_message_id text,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(workspace_id,id),
 UNIQUE(session_id,ordinal), UNIQUE(session_id,id),
 FOREIGN KEY(workspace_id,session_id) REFERENCES sessions(workspace_id,id)
);
CREATE TABLE turns (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, session_id text NOT NULL,
 message_id text NOT NULL UNIQUE, ordinal bigint NOT NULL,
 state text NOT NULL DEFAULT 'queued' CHECK(state IN
 ('queued','preparing','running','cancelling','succeeded','failed','cancelled','interrupted')),
 reason text, settings jsonb NOT NULL DEFAULT '{}', result_contract jsonb,
 retry_of_turn_id text, cancel_requested boolean NOT NULL DEFAULT false,
 evidence_complete boolean NOT NULL DEFAULT false, outcome jsonb, version integer NOT NULL DEFAULT 1,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(workspace_id,id), UNIQUE(session_id,ordinal),
 FOREIGN KEY(workspace_id,session_id) REFERENCES sessions(workspace_id,id),
 FOREIGN KEY(session_id,message_id) REFERENCES messages(session_id,id),
 FOREIGN KEY(workspace_id,retry_of_turn_id) REFERENCES turns(workspace_id,id)
);
CREATE UNIQUE INDEX one_active_turn ON turns(session_id) WHERE state IN ('preparing','running','cancelling');
CREATE TABLE executor_leases (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, session_id text NOT NULL,
 backend text NOT NULL, handle text, generation bigint NOT NULL, allocation_operation_id text UNIQUE NOT NULL,
 connection_id text, credential_id text, state text NOT NULL DEFAULT 'allocating'
 CHECK(state IN ('allocating','ready','quiescing','released','lost')),
 expires_at timestamptz NOT NULL, fingerprint jsonb NOT NULL DEFAULT '{}',
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(workspace_id,id), UNIQUE(session_id,generation),
 FOREIGN KEY(workspace_id,session_id) REFERENCES sessions(workspace_id,id),
 FOREIGN KEY(workspace_id,connection_id) REFERENCES connections(workspace_id,id),
 FOREIGN KEY(connection_id,credential_id) REFERENCES credential_versions(connection_id,id)
);
CREATE UNIQUE INDEX one_live_lease ON executor_leases(session_id) WHERE state IN ('allocating','ready','quiescing');
CREATE TABLE worktrees (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, session_id text NOT NULL UNIQUE,
 repository text, base_sha text, generation bigint NOT NULL DEFAULT 0,
 availability text NOT NULL DEFAULT 'none' CHECK(availability IN ('none','restoring','live','checkpointed','unavailable')),
 last_snapshot_id text, recovery_watermark bigint,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(workspace_id,id),
 FOREIGN KEY(workspace_id,session_id) REFERENCES sessions(workspace_id,id)
);
CREATE TABLE executions (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, turn_id text NOT NULL,
 lease_id text NOT NULL, attempt_ordinal integer NOT NULL, operation_id text UNIQUE NOT NULL,
 state text NOT NULL DEFAULT 'preparing' CHECK(state IN ('preparing','started','stop_requested','succeeded','failed','cancelled','unknown')),
 credential_id text, runtime_epoch text, final_watermark bigint, outcome jsonb, native_id text,
 cli_version text, adapter_version text, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(workspace_id,id), UNIQUE(turn_id,attempt_ordinal),
 FOREIGN KEY(workspace_id,turn_id) REFERENCES turns(workspace_id,id),
 FOREIGN KEY(workspace_id,lease_id) REFERENCES executor_leases(workspace_id,id),
 FOREIGN KEY(workspace_id,credential_id) REFERENCES credential_versions(workspace_id,id)
);
CREATE UNIQUE INDEX one_live_execution ON executions(turn_id) WHERE state IN ('preparing','started','stop_requested');
CREATE TABLE native_context_bindings (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, session_id text NOT NULL,
 provider_id text NOT NULL, native_id text NOT NULL, lineage_id text NOT NULL,
 cli_version text NOT NULL, adapter_version text NOT NULL, state_manifest_digest text,
 account_connection_id text, checkpoint_id text, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(workspace_id,id), FOREIGN KEY(workspace_id,session_id) REFERENCES sessions(workspace_id,id)
);
CREATE TABLE session_events (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, session_id text NOT NULL,
 seq bigint NOT NULL, type text NOT NULL, schema_version integer NOT NULL DEFAULT 1,
 recorded_at timestamptz NOT NULL DEFAULT now(), observed_at timestamptz,
 actor jsonb NOT NULL DEFAULT '{}', source jsonb NOT NULL,
 causation_id text, correlation_id text, turn_id text, execution_id text,
 executor_lease_id text, lease_generation bigint, runtime_epoch text, local_seq bigint,
 delegation_id text, changeset_id text, delivery_id text, payload jsonb NOT NULL,
 UNIQUE(workspace_id,id), UNIQUE(session_id,seq),
 FOREIGN KEY(workspace_id,session_id) REFERENCES sessions(workspace_id,id),
 FOREIGN KEY(workspace_id,turn_id) REFERENCES turns(workspace_id,id),
 FOREIGN KEY(workspace_id,execution_id) REFERENCES executions(workspace_id,id),
 FOREIGN KEY(workspace_id,executor_lease_id) REFERENCES executor_leases(workspace_id,id)
);
CREATE UNIQUE INDEX runtime_source_dedupe ON session_events(executor_lease_id,runtime_epoch,local_seq) WHERE local_seq IS NOT NULL;
CREATE TABLE runtime_ingestion_offsets (
 lease_id text REFERENCES executor_leases, runtime_epoch text, ack bigint NOT NULL DEFAULT 0,
 PRIMARY KEY(lease_id,runtime_epoch)
);
CREATE TABLE message_parts (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, session_id text NOT NULL,
 execution_id text NOT NULL, provider_part_id text NOT NULL, kind text NOT NULL,
 revision bigint NOT NULL, sealed boolean NOT NULL DEFAULT false, content text NOT NULL,
 UNIQUE(execution_id,provider_part_id), FOREIGN KEY(workspace_id,session_id) REFERENCES sessions(workspace_id,id),
 FOREIGN KEY(workspace_id,execution_id) REFERENCES executions(workspace_id,id)
);
CREATE TABLE snapshots (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, kind text NOT NULL,
 state text NOT NULL DEFAULT 'preparing', worktree_id text, project_version_id text,
 generation bigint, input_digest text, content_digest text, manifest jsonb, event_watermark bigint,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(workspace_id,id),
 CHECK((kind='checkpoint' AND worktree_id IS NOT NULL AND project_version_id IS NULL) OR
 (kind='environment' AND worktree_id IS NULL AND project_version_id IS NOT NULL)),
 FOREIGN KEY(workspace_id,worktree_id) REFERENCES worktrees(workspace_id,id),
 FOREIGN KEY(workspace_id,project_version_id) REFERENCES project_versions(workspace_id,id)
);
ALTER TABLE worktrees ADD FOREIGN KEY(workspace_id,last_snapshot_id) REFERENCES snapshots(workspace_id,id);
CREATE TABLE worktree_operations (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, worktree_id text NOT NULL,
 kind text NOT NULL, state text NOT NULL DEFAULT 'pending', expected_generation bigint NOT NULL,
 fence bigint NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(workspace_id,worktree_id) REFERENCES worktrees(workspace_id,id)
);
CREATE UNIQUE INDEX one_barrier ON worktree_operations(worktree_id) WHERE state IN ('pending','executing');
CREATE TABLE changesets (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, session_id text NOT NULL,
 worktree_id text NOT NULL, source_turn_id text, generation bigint NOT NULL, manifest_version integer NOT NULL DEFAULT 1,
 state text NOT NULL DEFAULT 'preparing', subject_digest text, base_sha text, head_sha text, tree_sha text,
 origin text NOT NULL, automatic_eligible boolean NOT NULL DEFAULT false,
 manifest jsonb, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(workspace_id,id),
 FOREIGN KEY(workspace_id,session_id) REFERENCES sessions(workspace_id,id),
 FOREIGN KEY(workspace_id,worktree_id) REFERENCES worktrees(workspace_id,id),
 FOREIGN KEY(workspace_id,source_turn_id) REFERENCES turns(workspace_id,id)
);
CREATE TABLE changeset_files (
 changeset_id text REFERENCES changesets, path text, mode text NOT NULL,
 type text NOT NULL, content_digest text, blob_ref text, PRIMARY KEY(changeset_id,path)
);
CREATE TABLE deliveries (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, session_id text NOT NULL,
 changeset_id text NOT NULL, subject_digest text NOT NULL, repository text NOT NULL, target_ref text NOT NULL,
 base_ref text NOT NULL, expected_head text, expected_base text, mapped_head text, remote_head text,
 transport text NOT NULL DEFAULT 'pull_request', policy jsonb NOT NULL,
 author_id text NOT NULL REFERENCES users, connection_id text NOT NULL, state text NOT NULL DEFAULT 'pending',
 version integer NOT NULL DEFAULT 1, pr_number integer, pr_url text, reason text,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(workspace_id,id),
 FOREIGN KEY(workspace_id,session_id) REFERENCES sessions(workspace_id,id),
 FOREIGN KEY(workspace_id,changeset_id) REFERENCES changesets(workspace_id,id),
 FOREIGN KEY(workspace_id,connection_id) REFERENCES connections(workspace_id,id)
);
CREATE TABLE delivery_steps (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, delivery_id text NOT NULL,
 kind text NOT NULL, effect_id text NOT NULL, evidence jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), FOREIGN KEY(workspace_id,delivery_id) REFERENCES deliveries(workspace_id,id)
);
CREATE TABLE delivery_target_claims (
 repository text, target text, generation bigint NOT NULL DEFAULT 0, holder text,
 expires_at timestamptz, PRIMARY KEY(repository,target)
);
CREATE TABLE merge_requests (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, delivery_id text NOT NULL,
 expected_version integer NOT NULL, subject_digest text NOT NULL, expected_head text NOT NULL,
 expected_base text, method text NOT NULL, state text NOT NULL DEFAULT 'pending', evidence jsonb,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(workspace_id,id),
 FOREIGN KEY(workspace_id,delivery_id) REFERENCES deliveries(workspace_id,id)
);
CREATE UNIQUE INDEX one_merge ON merge_requests(delivery_id) WHERE state IN ('pending','executing','blocked');
CREATE TABLE delegations (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, parent_session_id text NOT NULL,
 child_session_id text NOT NULL UNIQUE, changeset_id text, subject_digest text, head_sha text,
 role text NOT NULL, state text NOT NULL DEFAULT 'active', contract jsonb NOT NULL, contract_digest text NOT NULL,
 budget jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(workspace_id,id),
 CHECK(parent_session_id<>child_session_id),
 FOREIGN KEY(workspace_id,parent_session_id) REFERENCES sessions(workspace_id,id),
 FOREIGN KEY(workspace_id,child_session_id) REFERENCES sessions(workspace_id,id),
 FOREIGN KEY(workspace_id,changeset_id) REFERENCES changesets(workspace_id,id)
);
CREATE TABLE delegation_results (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, delegation_id text NOT NULL UNIQUE,
 child_session_id text NOT NULL, completing_turn_id text NOT NULL, subject_digest text NOT NULL, head_sha text,
 kind text NOT NULL, verdict text NOT NULL, validated boolean NOT NULL, value jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(workspace_id,id),
 FOREIGN KEY(workspace_id,delegation_id) REFERENCES delegations(workspace_id,id),
 FOREIGN KEY(workspace_id,child_session_id) REFERENCES sessions(workspace_id,id),
 FOREIGN KEY(workspace_id,completing_turn_id) REFERENCES turns(workspace_id,id)
);
CREATE TABLE wait_subscriptions (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, delegation_id text NOT NULL,
 parent_session_id text NOT NULL, state text NOT NULL DEFAULT 'pending', expires_at timestamptz NOT NULL,
 evidence_result_id text, created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(workspace_id,delegation_id) REFERENCES delegations(workspace_id,id),
 FOREIGN KEY(workspace_id,parent_session_id) REFERENCES sessions(workspace_id,id)
);
CREATE TABLE service_desires (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, session_id text NOT NULL,
 name text NOT NULL, declaration jsonb NOT NULL, declaration_digest text NOT NULL,
 desired_state text NOT NULL DEFAULT 'stopped', version integer NOT NULL DEFAULT 1,
 UNIQUE(workspace_id,id), UNIQUE(session_id,name),
 FOREIGN KEY(workspace_id,session_id) REFERENCES sessions(workspace_id,id)
);
CREATE TABLE service_instances (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, session_id text NOT NULL,
 name text NOT NULL, lease_id text NOT NULL, lease_generation bigint NOT NULL,
 state text NOT NULL DEFAULT 'pending', port integer, observed_at timestamptz, UNIQUE(session_id,name,lease_id),
 FOREIGN KEY(workspace_id,session_id) REFERENCES sessions(workspace_id,id),
 FOREIGN KEY(workspace_id,lease_id) REFERENCES executor_leases(workspace_id,id)
);
CREATE TABLE resource_fences (
 workspace_id text REFERENCES workspaces, resource text, generation bigint NOT NULL DEFAULT 0,
 holder text, expires_at timestamptz, PRIMARY KEY(workspace_id,resource)
);
CREATE TABLE capacity_reservations (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, connection_id text NOT NULL,
 slot_ordinal integer NOT NULL, execution_id text, lease_id text, state text NOT NULL DEFAULT 'active',
 expires_at timestamptz NOT NULL, FOREIGN KEY(workspace_id,connection_id) REFERENCES connections(workspace_id,id),
 FOREIGN KEY(workspace_id,execution_id) REFERENCES executions(workspace_id,id),
 FOREIGN KEY(workspace_id,lease_id) REFERENCES executor_leases(workspace_id,id)
);
CREATE UNIQUE INDEX one_slot ON capacity_reservations(connection_id,slot_ordinal) WHERE state='active';
CREATE TABLE credential_grants (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, principal_id text NOT NULL REFERENCES users,
 connection_id text NOT NULL, credential_id text NOT NULL, purpose text NOT NULL,
 session_id text, lease_id text, operation_id text NOT NULL, epoch integer NOT NULL,
 expires_at timestamptz NOT NULL, revoked_at timestamptz,
 FOREIGN KEY(workspace_id,connection_id) REFERENCES connections(workspace_id,id),
 FOREIGN KEY(connection_id,credential_id) REFERENCES credential_versions(connection_id,id)
);
CREATE TABLE blobs (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, storage_key text NOT NULL,
 digest text NOT NULL, size bigint NOT NULL, class text NOT NULL, state text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(workspace_id,id)
);
CREATE TABLE jobs (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, kind text NOT NULL,
 target_family text NOT NULL, turn_id text, execution_id text, lease_id text, snapshot_id text,
 changeset_id text, worktree_id text, delivery_id text, delegation_id text, connection_id text, service_id text,
 effect_id text UNIQUE NOT NULL, dedupe_key text UNIQUE NOT NULL,
 state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','claimed','retry_wait','succeeded','failed','cancelled')),
 due_at timestamptz NOT NULL DEFAULT now(), deadline timestamptz NOT NULL DEFAULT now()+interval '24 hours',
 priority integer NOT NULL DEFAULT 0, attempts integer NOT NULL DEFAULT 0,
 max_attempts integer NOT NULL DEFAULT 12, claim_generation bigint NOT NULL DEFAULT 0,
 holder text, claim_expires_at timestamptz, last_error text,
 created_at timestamptz NOT NULL DEFAULT now(),
 CHECK(num_nonnulls(turn_id,execution_id,lease_id,snapshot_id,changeset_id,worktree_id,delivery_id,delegation_id,connection_id,service_id)=1),
 CHECK(CASE target_family WHEN 'turn' THEN turn_id IS NOT NULL WHEN 'execution' THEN execution_id IS NOT NULL
 WHEN 'executor' THEN lease_id IS NOT NULL WHEN 'snapshot' THEN snapshot_id IS NOT NULL
 WHEN 'changeset' THEN changeset_id IS NOT NULL WHEN 'worktree' THEN worktree_id IS NOT NULL
 WHEN 'delivery' THEN delivery_id IS NOT NULL WHEN 'delegation' THEN delegation_id IS NOT NULL
 WHEN 'connection' THEN connection_id IS NOT NULL WHEN 'service' THEN service_id IS NOT NULL ELSE false END),
 FOREIGN KEY(workspace_id,turn_id) REFERENCES turns(workspace_id,id),
 FOREIGN KEY(workspace_id,execution_id) REFERENCES executions(workspace_id,id),
 FOREIGN KEY(workspace_id,lease_id) REFERENCES executor_leases(workspace_id,id),
 FOREIGN KEY(workspace_id,snapshot_id) REFERENCES snapshots(workspace_id,id),
 FOREIGN KEY(workspace_id,changeset_id) REFERENCES changesets(workspace_id,id),
 FOREIGN KEY(workspace_id,worktree_id) REFERENCES worktrees(workspace_id,id),
 FOREIGN KEY(workspace_id,delivery_id) REFERENCES deliveries(workspace_id,id),
 FOREIGN KEY(workspace_id,delegation_id) REFERENCES delegations(workspace_id,id),
 FOREIGN KEY(workspace_id,connection_id) REFERENCES connections(workspace_id,id),
 FOREIGN KEY(workspace_id,service_id) REFERENCES service_desires(workspace_id,id)
);
CREATE INDEX due_jobs ON jobs(state,due_at,priority);
CREATE TABLE job_attempts (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, job_id text NOT NULL REFERENCES jobs,
 generation bigint NOT NULL, holder text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(job_id,generation)
);
CREATE TABLE outbox_messages (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, session_id text NOT NULL,
 event_id text NOT NULL, dedupe_key text UNIQUE NOT NULL, state text NOT NULL DEFAULT 'queued',
 created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(workspace_id,session_id) REFERENCES sessions(workspace_id,id),
 FOREIGN KEY(workspace_id,event_id) REFERENCES session_events(workspace_id,id)
);
CREATE TABLE command_deduplication (
 principal_id text REFERENCES users, workspace_id text REFERENCES workspaces, command_kind text, key text,
 fingerprint text NOT NULL, response jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(principal_id,workspace_id,command_kind,key)
);
CREATE TABLE audit_records (
 id text PRIMARY KEY, workspace_id text REFERENCES workspaces, actor_id text REFERENCES users,
 action text NOT NULL, target_id text NOT NULL, purpose text, result text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION deny_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'immutable_record'; END $$;
CREATE TRIGGER journal_append_only BEFORE UPDATE OR DELETE ON session_events FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER versions_immutable BEFORE UPDATE OR DELETE ON project_versions FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER results_immutable BEFORE UPDATE OR DELETE ON delegation_results FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER steps_immutable BEFORE UPDATE OR DELETE ON delivery_steps FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE FUNCTION guard_terminal() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.state IN ('succeeded','failed','cancelled','interrupted') AND NEW IS DISTINCT FROM OLD THEN
  RAISE EXCEPTION 'terminal_immutable';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER turns_terminal BEFORE UPDATE ON turns FOR EACH ROW EXECUTE FUNCTION guard_terminal();
CREATE FUNCTION guard_sealed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.state='ready' AND NEW IS DISTINCT FROM OLD THEN RAISE EXCEPTION 'sealed_immutable'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER changesets_sealed BEFORE UPDATE ON changesets FOR EACH ROW EXECUTE FUNCTION guard_sealed();
CREATE TRIGGER snapshots_sealed BEFORE UPDATE ON snapshots FOR EACH ROW EXECUTE FUNCTION guard_sealed();
