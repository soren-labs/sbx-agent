ALTER TABLE native_context_bindings ADD COLUMN account_credential_id text;
ALTER TABLE native_context_bindings ADD FOREIGN KEY(workspace_id,account_credential_id) REFERENCES credential_versions(workspace_id,id);
