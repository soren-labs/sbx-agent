ALTER TABLE jobs ADD COLUMN input_credential_id text;
ALTER TABLE jobs ADD FOREIGN KEY(workspace_id,input_credential_id) REFERENCES credential_versions(workspace_id,id);
UPDATE jobs SET input_credential_id=effect_id WHERE kind='connection.validate';
