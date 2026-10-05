CREATE TABLE identity_notices (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces,
 user_id text NOT NULL REFERENCES users, purpose text NOT NULL,
 envelope jsonb NOT NULL, expires_at timestamptz NOT NULL,
 sent_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE email_verifications ADD COLUMN auth_epoch bigint NOT NULL DEFAULT 1;
ALTER TABLE jobs ADD COLUMN user_id text REFERENCES users;
ALTER TABLE jobs DROP CONSTRAINT jobs_check;
ALTER TABLE jobs DROP CONSTRAINT jobs_check1;
ALTER TABLE jobs ADD CONSTRAINT jobs_one_target CHECK(num_nonnulls(turn_id,execution_id,lease_id,snapshot_id,changeset_id,worktree_id,delivery_id,delegation_id,connection_id,service_id,user_id)=1);
ALTER TABLE jobs ADD CONSTRAINT jobs_typed_target CHECK(CASE target_family
 WHEN 'turn' THEN turn_id IS NOT NULL WHEN 'execution' THEN execution_id IS NOT NULL
 WHEN 'executor' THEN lease_id IS NOT NULL WHEN 'snapshot' THEN snapshot_id IS NOT NULL
 WHEN 'changeset' THEN changeset_id IS NOT NULL WHEN 'worktree' THEN worktree_id IS NOT NULL
 WHEN 'delivery' THEN delivery_id IS NOT NULL WHEN 'delegation' THEN delegation_id IS NOT NULL
 WHEN 'connection' THEN connection_id IS NOT NULL WHEN 'service' THEN service_id IS NOT NULL
 WHEN 'identity' THEN user_id IS NOT NULL ELSE false END);
