CREATE TABLE imported_references (
 source_system text NOT NULL, source_record_id text NOT NULL, source_version text NOT NULL,
 workspace_id text NOT NULL REFERENCES workspaces, session_id text NOT NULL,
 source_digest text NOT NULL, disposition jsonb NOT NULL,
 PRIMARY KEY(source_system,source_record_id,source_version),
 FOREIGN KEY(workspace_id,session_id) REFERENCES sessions(workspace_id,id)
);
CREATE TRIGGER import_provenance_immutable BEFORE UPDATE OR DELETE ON imported_references FOR EACH ROW EXECUTE FUNCTION deny_mutation();
