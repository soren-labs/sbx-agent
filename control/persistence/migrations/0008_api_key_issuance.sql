-- API-key issuance is idempotent per (user, Idempotency-Key): a retried create returns the
-- key it already minted instead of minting another. The plaintext is never stored; only a
-- vault-sealed copy is kept for a short replay window, then erased (timer sweep, revoke).
ALTER TABLE api_keys ADD COLUMN issuance_key text;
ALTER TABLE api_keys ADD COLUMN issuance_digest text;
ALTER TABLE api_keys ADD COLUMN replay_sealed jsonb;
ALTER TABLE api_keys ADD COLUMN replay_until timestamptz;
CREATE UNIQUE INDEX api_keys_issuance ON api_keys (user_id, issuance_key)
  WHERE issuance_key IS NOT NULL;
CREATE INDEX api_keys_replay_sweep ON api_keys (replay_until) WHERE replay_sealed IS NOT NULL;
ALTER TABLE api_keys ADD CONSTRAINT api_keys_replay_bounded
  CHECK (replay_sealed IS NULL OR (replay_until IS NOT NULL AND issuance_key IS NOT NULL));
