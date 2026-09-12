CREATE TABLE events_v2 (
    id TEXT PRIMARY KEY, app_id TEXT, tenant_id TEXT, user_id TEXT,
    kind TEXT NOT NULL, subject_id TEXT NOT NULL, created_at TEXT NOT NULL,
    actor_kind TEXT, actor_id TEXT, subject_type TEXT, request_id TEXT
);
INSERT INTO events_v2(id,app_id,tenant_id,user_id,kind,subject_id,created_at)
    SELECT id,app_id,tenant_id,user_id,kind,subject_id,created_at FROM events;
DROP TABLE events;
ALTER TABLE events_v2 RENAME TO events;
CREATE INDEX events_app_time ON events(app_id,created_at DESC,id DESC);
CREATE INDEX calls_app_time ON calls(app_id,created_at DESC,id DESC);
CREATE INDEX sessions_app_time ON sessions(app_id,created_at DESC,id DESC);
CREATE INDEX connections_app_time ON connections(app_id,created_at DESC,id DESC);
PRAGMA user_version = 2;
