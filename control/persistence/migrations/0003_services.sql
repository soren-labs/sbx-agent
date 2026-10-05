-- Service desired state persists across leases; each realization is a new instance (RFC 03).
CREATE TABLE service_desires (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  session_id text NOT NULL REFERENCES sessions(id),
  name text NOT NULL,
  declaration jsonb NOT NULL,
  declaration_digest text NOT NULL,
  desired text NOT NULL CHECK (desired IN ('running', 'stopped')),
  version integer NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (session_id, name)
);

CREATE TABLE service_instances (
  id text PRIMARY KEY,
  workspace_id text NOT NULL,
  session_id text NOT NULL REFERENCES sessions(id),
  name text NOT NULL,
  lease_id text NOT NULL REFERENCES executor_leases(id),
  lease_generation integer NOT NULL,
  state text NOT NULL CHECK (state IN ('pending', 'starting', 'ready', 'degraded', 'stopping', 'stopped', 'failed')),
  observed jsonb NOT NULL DEFAULT '{}'::jsonb,
  observed_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (session_id, name, lease_id)
);

ALTER TABLE jobs ADD CONSTRAINT jobs_service_desire_fk FOREIGN KEY (service_desire_id) REFERENCES service_desires(id);
