-- Resident machines: a Linux workstation or VPS the operator registered for one workspace.
-- The control plane reaches it only over SSH with the operator's fixed parameters in
-- `target` (host, port, user, key and known_hosts paths, remote checkout); `target` is never
-- returned by the API and cannot be set through it. The private key itself is not stored here.
CREATE TABLE resident_machines (
  id text PRIMARY KEY,
  workspace_id text NOT NULL REFERENCES workspaces(id),
  name text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9._-]{0,62}$'),
  target jsonb NOT NULL,
  state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'disabled', 'removed')),
  max_leases integer NOT NULL DEFAULT 4 CHECK (max_leases BETWEEN 1 AND 64),
  created_by text REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  removed_at timestamptz,
  CHECK ((state = 'removed') = (removed_at IS NOT NULL)),
  UNIQUE (workspace_id, id)
);
CREATE UNIQUE INDEX resident_machines_name ON resident_machines (workspace_id, name)
  WHERE state <> 'removed';

-- A Session on the resident executor pins exactly one machine of its own workspace, and
-- every lease records the machine it runs on (capacity and cleanup are derived from leases).
ALTER TABLE sessions ADD COLUMN resident_machine_id text;
ALTER TABLE sessions ADD CONSTRAINT sessions_resident_machine
  FOREIGN KEY (workspace_id, resident_machine_id) REFERENCES resident_machines (workspace_id, id);
ALTER TABLE sessions ADD CONSTRAINT sessions_resident_backend
  CHECK ((executor_backend = 'resident') = (resident_machine_id IS NOT NULL));
CREATE INDEX sessions_resident_machine ON sessions (resident_machine_id)
  WHERE resident_machine_id IS NOT NULL;

ALTER TABLE executor_leases ADD COLUMN resident_machine_id text;
ALTER TABLE executor_leases ADD CONSTRAINT executor_leases_resident_machine
  FOREIGN KEY (workspace_id, resident_machine_id) REFERENCES resident_machines (workspace_id, id);
CREATE INDEX executor_leases_resident_live ON executor_leases (resident_machine_id)
  WHERE state IN ('allocating', 'ready', 'quiescing');
