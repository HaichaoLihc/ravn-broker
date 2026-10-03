-- Human approvals for write tools. A row is single-use and bound to the
-- arguments fingerprint; ciphertext is dropped once the request leaves 'pending'.
CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    app_id TEXT NOT NULL, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    connection_id TEXT NOT NULL, session_id TEXT NOT NULL, tool TEXT NOT NULL,
    arguments_fingerprint TEXT NOT NULL, arguments_ciphertext BLOB,
    status TEXT NOT NULL CHECK (status IN ('pending','approved','denied','expired','cancelled','consumed')),
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL, decided_at TEXT, call_id TEXT,
    FOREIGN KEY (app_id, tenant_id, session_id) REFERENCES sessions(app_id, tenant_id, id)
);
CREATE INDEX approvals_status ON approvals(status, created_at);
PRAGMA user_version = 6;
