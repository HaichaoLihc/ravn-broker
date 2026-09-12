CREATE TABLE metadata (key TEXT PRIMARY KEY, value BLOB NOT NULL);
CREATE TABLE applications (id TEXT PRIMARY KEY, tenant_mode TEXT NOT NULL);
CREATE TABLE app_keys (
    id TEXT PRIMARY KEY, app_id TEXT NOT NULL REFERENCES applications(id),
    secret_hash TEXT NOT NULL UNIQUE, label TEXT NOT NULL,
    created_at TEXT NOT NULL, revoked_at TEXT
);
CREATE TABLE connections (
    id TEXT NOT NULL, app_id TEXT NOT NULL REFERENCES applications(id),
    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, integration_id TEXT NOT NULL,
    provider_account_id TEXT NOT NULL, display_name TEXT NOT NULL, label TEXT,
    status TEXT NOT NULL, epoch INTEGER NOT NULL DEFAULT 1,
    revision INTEGER NOT NULL DEFAULT 1, ciphertext BLOB, key_id TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (app_id, tenant_id, id)
);
CREATE INDEX connections_owner ON connections(app_id, tenant_id, user_id, created_at DESC, id DESC);
CREATE TABLE sessions (
    id TEXT NOT NULL, app_id TEXT NOT NULL, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    connection_id TEXT NOT NULL, connection_epoch INTEGER NOT NULL,
    issuing_key_id TEXT NOT NULL REFERENCES app_keys(id), token_hash TEXT NOT NULL UNIQUE,
    tools TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT,
    PRIMARY KEY (app_id, tenant_id, id),
    FOREIGN KEY (app_id, tenant_id, connection_id) REFERENCES connections(app_id, tenant_id, id)
);
CREATE INDEX sessions_owner ON sessions(app_id, tenant_id, user_id, created_at DESC, id DESC);
CREATE INDEX sessions_connection ON sessions(app_id, tenant_id, connection_id);
CREATE TABLE calls (
    id TEXT NOT NULL, app_id TEXT NOT NULL, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    connection_id TEXT NOT NULL, session_id TEXT, tool TEXT NOT NULL, status TEXT NOT NULL,
    arguments_fingerprint TEXT NOT NULL, fingerprint_key_id TEXT NOT NULL,
    error_code TEXT, duration_ms INTEGER, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, completed_at TEXT,
    PRIMARY KEY (app_id, tenant_id, id),
    FOREIGN KEY (app_id, tenant_id, connection_id) REFERENCES connections(app_id, tenant_id, id)
);
CREATE INDEX calls_owner ON calls(app_id, tenant_id, user_id, created_at DESC, id DESC);
CREATE TABLE events (
    id TEXT PRIMARY KEY, app_id TEXT NOT NULL, tenant_id TEXT,
    user_id TEXT, kind TEXT NOT NULL, subject_id TEXT NOT NULL, created_at TEXT NOT NULL
);
PRAGMA user_version = 1;
