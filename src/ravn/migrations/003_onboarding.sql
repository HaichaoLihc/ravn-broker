ALTER TABLE connections ADD COLUMN credential_type TEXT NOT NULL DEFAULT 'bearer';
ALTER TABLE connections ADD COLUMN credential_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE connections ADD COLUMN refresh_attempt TEXT;
CREATE TABLE connect_sessions (
    id TEXT PRIMARY KEY, app_id TEXT NOT NULL, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    issuing_key_id TEXT NOT NULL REFERENCES app_keys(id), integration_id TEXT NOT NULL,
    integration_fingerprint TEXT NOT NULL, return_url TEXT NOT NULL, app_state TEXT NOT NULL,
    reconnect_id TEXT, reconnect_epoch INTEGER, status TEXT NOT NULL,
    ticket_hash TEXT UNIQUE, state_hash TEXT UNIQUE, cookie_hash TEXT, verifier BLOB,
    staged BLOB, completion_hash TEXT, completion_expires_at TEXT,
    connection_id TEXT, failure_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX connect_sessions_owner ON connect_sessions(app_id,tenant_id,user_id,created_at);
CREATE INDEX connect_sessions_expiry ON connect_sessions(expires_at);
PRAGMA user_version = 3;
