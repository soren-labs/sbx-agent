-- Harness-neutral BYOK inference Connections. The vendor-specific kinds stay valid so
-- stored Connections, CredentialVersions and the Sessions pinned to them are preserved;
-- the application no longer creates them.
ALTER TABLE connections DROP CONSTRAINT connections_kind_check;
ALTER TABLE connections ADD CONSTRAINT connections_kind_check
  CHECK (kind IN ('modal', 'github', 'inference_api', 'opencode_zen', 'codex'));

-- Non-secret settings of a CredentialVersion (endpoints, default model). They version
-- with the secret so a replacement swaps both atomically; plaintext keys never land here.
ALTER TABLE credential_versions ADD COLUMN public_config jsonb NOT NULL DEFAULT '{}'::jsonb;
