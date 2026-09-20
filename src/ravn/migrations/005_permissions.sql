-- Per-session tool rules. A row only ever narrows a session below its stored
-- ceiling; absence of rows leaves the ceiling intact.
CREATE TABLE session_tool_rules (
    app_id TEXT NOT NULL, tenant_id TEXT NOT NULL, session_id TEXT NOT NULL,
    tool TEXT NOT NULL, allowed INTEGER NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (app_id, tenant_id, session_id, tool),
    FOREIGN KEY (app_id, tenant_id, session_id) REFERENCES sessions(app_id, tenant_id, id)
);
PRAGMA user_version = 5;
