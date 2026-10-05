CREATE TABLE tool_grants (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces,
 session_id text NOT NULL, turn_id text NOT NULL, execution_id text NOT NULL UNIQUE,
 lease_id text NOT NULL, lease_generation bigint NOT NULL, token_hash text NOT NULL UNIQUE,
 actions text[] NOT NULL, expires_at timestamptz NOT NULL, revoked_at timestamptz,
 FOREIGN KEY(workspace_id,session_id) REFERENCES sessions(workspace_id,id),
 FOREIGN KEY(workspace_id,turn_id) REFERENCES turns(workspace_id,id),
 FOREIGN KEY(workspace_id,execution_id) REFERENCES executions(workspace_id,id),
 FOREIGN KEY(workspace_id,lease_id) REFERENCES executor_leases(workspace_id,id)
);
