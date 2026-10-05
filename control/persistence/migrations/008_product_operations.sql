ALTER TABLE worktree_operations ADD COLUMN payload jsonb NOT NULL DEFAULT '{}';
ALTER TABLE worktree_operations ADD COLUMN result jsonb;
ALTER TABLE worktree_operations ADD COLUMN lease_id text;
ALTER TABLE worktree_operations ADD CONSTRAINT operation_lease FOREIGN KEY(workspace_id,lease_id) REFERENCES executor_leases(workspace_id,id);
CREATE TABLE preview_grants (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces, service_id text NOT NULL,
 lease_id text NOT NULL, token_hash text NOT NULL UNIQUE, expires_at timestamptz NOT NULL,
 FOREIGN KEY(workspace_id,service_id) REFERENCES service_desires(workspace_id,id),
 FOREIGN KEY(workspace_id,lease_id) REFERENCES executor_leases(workspace_id,id)
);
