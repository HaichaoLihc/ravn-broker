ALTER TABLE calls ADD COLUMN effect TEXT NOT NULL DEFAULT 'read';
ALTER TABLE calls ADD COLUMN idempotency_key_hash TEXT;
CREATE UNIQUE INDEX calls_idempotency ON calls(app_id,tenant_id,user_id,idempotency_key_hash)
    WHERE idempotency_key_hash IS NOT NULL;
PRAGMA user_version = 4;
