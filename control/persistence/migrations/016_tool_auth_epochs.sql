ALTER TABLE tool_grants ADD COLUMN auth_epoch bigint NOT NULL DEFAULT 0;
UPDATE tool_grants SET revoked_at=coalesce(revoked_at,now());
