ALTER TABLE executions ADD COLUMN isolation_confirmed boolean NOT NULL DEFAULT false;
ALTER TABLE executor_leases ADD COLUMN cleanup_confirmed boolean NOT NULL DEFAULT false;
