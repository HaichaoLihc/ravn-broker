-- Agent-facing OAuth state. Only hashes of authorization codes and refresh
-- tokens are stored; access tokens are ordinary sessions (also hash-only).
-- A session belongs to exactly one of an application key or an agent grant,
-- so revoking a key never touches grant sessions and vice versa.
-- Store.open runs this step with foreign_keys OFF and checks them before COMMIT.
CREATE TABLE oauth_clients (
    id TEXT PRIMARY KEY, name TEXT, metadata TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE oauth_requests (
    id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES oauth_clients(id),
    redirect_uri TEXT NOT NULL, code_challenge TEXT NOT NULL, state TEXT,
    scope TEXT NOT NULL DEFAULT '', resource TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','approved','denied','expired')),
    operator_id TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, decided_at TEXT
);
CREATE INDEX oauth_requests_expiry ON oauth_requests(status, expires_at);
CREATE TABLE oauth_grants (
    id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES oauth_clients(id),
    app_id TEXT NOT NULL, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    connection_id TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '', resource TEXT NOT NULL,
    rotation INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    refreshed_at TEXT, revoked_at TEXT, revoke_reason TEXT,
    FOREIGN KEY (app_id, tenant_id, connection_id) REFERENCES connections(app_id, tenant_id, id)
);
CREATE INDEX oauth_grants_connection ON oauth_grants(app_id, tenant_id, connection_id);
CREATE INDEX oauth_grants_time ON oauth_grants(created_at DESC, id DESC);
CREATE TABLE oauth_codes (
    id TEXT PRIMARY KEY, code_hash TEXT NOT NULL UNIQUE,
    request_id TEXT NOT NULL REFERENCES oauth_requests(id),
    client_id TEXT NOT NULL REFERENCES oauth_clients(id),
    app_id TEXT NOT NULL, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    connection_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, code_challenge TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '', resource TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('issued','redeeming','redeemed','reused','failed')),
    grant_id TEXT REFERENCES oauth_grants(id), created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE oauth_refresh_tokens (
    token_hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES oauth_grants(id),
    generation INTEGER NOT NULL, created_at TEXT NOT NULL, rotated_at TEXT
);
CREATE INDEX oauth_refresh_grant ON oauth_refresh_tokens(grant_id);
CREATE TABLE sessions_v2 (
    id TEXT NOT NULL, app_id TEXT NOT NULL, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
    connection_id TEXT NOT NULL, connection_epoch INTEGER NOT NULL,
    issuing_key_id TEXT REFERENCES app_keys(id), token_hash TEXT NOT NULL UNIQUE,
    tools TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT,
    grant_id TEXT REFERENCES oauth_grants(id),
    PRIMARY KEY (app_id, tenant_id, id),
    FOREIGN KEY (app_id, tenant_id, connection_id) REFERENCES connections(app_id, tenant_id, id),
    CHECK ((issuing_key_id IS NULL) <> (grant_id IS NULL))
);
INSERT INTO sessions_v2(id, app_id, tenant_id, user_id, connection_id, connection_epoch,
    issuing_key_id, token_hash, tools, created_at, expires_at, revoked_at)
SELECT id, app_id, tenant_id, user_id, connection_id, connection_epoch,
    issuing_key_id, token_hash, tools, created_at, expires_at, revoked_at FROM sessions;
DROP TABLE sessions;
ALTER TABLE sessions_v2 RENAME TO sessions;
CREATE INDEX sessions_owner ON sessions(app_id, tenant_id, user_id, created_at DESC, id DESC);
CREATE INDEX sessions_connection ON sessions(app_id, tenant_id, connection_id);
CREATE INDEX sessions_app_time ON sessions(app_id, created_at DESC, id DESC);
CREATE INDEX sessions_grant ON sessions(grant_id) WHERE grant_id IS NOT NULL;
PRAGMA user_version = 7;
