ALTER TABLE outbox_messages
 ADD COLUMN kind text NOT NULL DEFAULT 'outbox.notify',
 ADD COLUMN due_at timestamptz NOT NULL DEFAULT now(),
 ADD COLUMN deadline timestamptz NOT NULL DEFAULT now()+interval '7 days',
 ADD COLUMN priority integer NOT NULL DEFAULT 0,
 ADD COLUMN attempts integer NOT NULL DEFAULT 0,
 ADD COLUMN max_attempts integer NOT NULL DEFAULT 12,
 ADD COLUMN claim_generation bigint NOT NULL DEFAULT 0,
 ADD COLUMN holder text,
 ADD COLUMN claim_expires_at timestamptz,
 ADD COLUMN last_error text;
CREATE TABLE outbox_attempts (
 id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES workspaces,
 job_id text NOT NULL REFERENCES outbox_messages,
 generation bigint NOT NULL, holder text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(job_id,generation)
);
