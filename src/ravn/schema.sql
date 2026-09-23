-- Current demo database layout. Historical upgrades are intentionally unsupported.

CREATE TABLE metadata (key TEXT PRIMARY KEY, value BLOB NOT NULL);

CREATE TABLE applications (
    id TEXT PRIMARY KEY, tenant_mode TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1, return_urls TEXT NOT NULL DEFAULT '[]'
);

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
    credential_type TEXT NOT NULL DEFAULT 'bearer',
    credential_version INTEGER NOT NULL DEFAULT 1, refresh_attempt TEXT,
    PRIMARY KEY (app_id, tenant_id, id)
);

CREATE TABLE calls (
    id TEXT NOT NULL, app_id TEXT NOT NULL, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    connection_id TEXT NOT NULL, session_id TEXT, tool TEXT NOT NULL, status TEXT NOT NULL,
    arguments_fingerprint TEXT NOT NULL, fingerprint_key_id TEXT NOT NULL,
    error_code TEXT, duration_ms INTEGER, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, completed_at TEXT,
    effect TEXT NOT NULL DEFAULT 'read', idempotency_key_hash TEXT,
    PRIMARY KEY (app_id, tenant_id, id),
    FOREIGN KEY (app_id, tenant_id, connection_id) REFERENCES connections(app_id, tenant_id, id)
);

CREATE TABLE events (
    id TEXT PRIMARY KEY, app_id TEXT, tenant_id TEXT, user_id TEXT,
    kind TEXT NOT NULL, subject_id TEXT NOT NULL, created_at TEXT NOT NULL,
    actor_kind TEXT, actor_id TEXT, subject_type TEXT, request_id TEXT
);

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

CREATE TABLE integrations (
    app_id TEXT NOT NULL REFERENCES applications(id),
    id TEXT NOT NULL,
    ciphertext BLOB NOT NULL,
    PRIMARY KEY (app_id, id)
);

CREATE TABLE sessions (
    id TEXT NOT NULL, app_id TEXT NOT NULL, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    issuing_key_id TEXT NOT NULL REFERENCES app_keys(id), token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT,
    PRIMARY KEY (app_id, tenant_id, id)
);

CREATE TABLE session_connections (
    app_id TEXT NOT NULL, tenant_id TEXT NOT NULL, session_id TEXT NOT NULL,
    connection_id TEXT NOT NULL, connection_epoch INTEGER NOT NULL,
    PRIMARY KEY (app_id, tenant_id, session_id, connection_id),
    FOREIGN KEY (app_id, tenant_id, session_id) REFERENCES sessions(app_id, tenant_id, id),
    FOREIGN KEY (app_id, tenant_id, connection_id) REFERENCES connections(app_id, tenant_id, id)
);

CREATE INDEX connections_owner ON connections(app_id, tenant_id, user_id, created_at DESC, id DESC);

CREATE INDEX calls_owner ON calls(app_id, tenant_id, user_id, created_at DESC, id DESC);

CREATE INDEX events_app_time ON events(app_id,created_at DESC,id DESC);

CREATE INDEX calls_app_time ON calls(app_id,created_at DESC,id DESC);

CREATE INDEX connections_app_time ON connections(app_id,created_at DESC,id DESC);

CREATE INDEX connect_sessions_owner ON connect_sessions(app_id,tenant_id,user_id,created_at);

CREATE INDEX connect_sessions_expiry ON connect_sessions(expires_at);

CREATE UNIQUE INDEX calls_idempotency ON calls(app_id,tenant_id,user_id,idempotency_key_hash)
    WHERE idempotency_key_hash IS NOT NULL;

CREATE INDEX sessions_owner ON sessions(app_id,tenant_id,user_id,created_at DESC,id DESC);

CREATE INDEX sessions_app_time ON sessions(app_id,created_at DESC,id DESC);

CREATE INDEX session_connections_connection ON session_connections(app_id,tenant_id,connection_id);

PRAGMA user_version = 9;
