-- Phase 5: request-level Idempotency-Key records for the unified /api.
-- A key binds (workspace, key) to one committed request fingerprint; a
-- replay with a different payload is a conflict, a replay with the same
-- payload returns the recorded response.

CREATE TABLE api_idempotency_keys (
    workspace_id  text NOT NULL REFERENCES workspaces(id),
    key           text NOT NULL,
    method        text NOT NULL,
    path          text NOT NULL,
    request_hash  text NOT NULL,
    status        int,
    response      jsonb,
    resource_ids  jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, key)
);

CREATE INDEX api_idempotency_created ON api_idempotency_keys(created_at);
