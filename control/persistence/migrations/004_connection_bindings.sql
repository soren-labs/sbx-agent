ALTER TABLE sessions ADD COLUMN modal_connection_id text;
ALTER TABLE sessions ADD COLUMN zen_connection_id text;
ALTER TABLE sessions ADD COLUMN github_connection_id text;
ALTER TABLE sessions ADD FOREIGN KEY(workspace_id,modal_connection_id) REFERENCES connections(workspace_id,id);
ALTER TABLE sessions ADD FOREIGN KEY(workspace_id,zen_connection_id) REFERENCES connections(workspace_id,id);
ALTER TABLE sessions ADD FOREIGN KEY(workspace_id,github_connection_id) REFERENCES connections(workspace_id,id);
CREATE TABLE auth_command_receipts (
 command_kind text, key text, fingerprint text NOT NULL, response jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(command_kind,key)
);
