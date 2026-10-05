ALTER TABLE connections ADD COLUMN concurrency_limit integer NOT NULL DEFAULT 2 CHECK(concurrency_limit>0);
ALTER TABLE workspaces ADD COLUMN max_live_leases integer NOT NULL DEFAULT 4 CHECK(max_live_leases>0);
