-- Sessions can run on a subscription Machine Slot instead of an inference API Connection,
-- and pin a reasoning effort next to the model. The lease that mounts a Slot's Volume
-- records it, so the Slot's single holder is always derivable from durable lease state.
ALTER TABLE sessions ADD COLUMN machine_slot_id text REFERENCES machine_slots(id);
ALTER TABLE sessions ADD COLUMN harness_effort text;
ALTER TABLE sessions ADD CONSTRAINT sessions_one_inference_source
  CHECK (machine_slot_id IS NULL OR inference_connection_id IS NULL);
ALTER TABLE executor_leases ADD COLUMN machine_slot_id text REFERENCES machine_slots(id);
CREATE INDEX sessions_machine_slot ON sessions (machine_slot_id) WHERE machine_slot_id IS NOT NULL;
