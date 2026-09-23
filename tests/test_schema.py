import sqlite3

import pytest

from ravn.service import Service

pytestmark = pytest.mark.anyio


async def test_fresh_schema_has_current_tables_indexes_and_no_tool_policy(config):
    service = Service(config)
    await service.start()
    await service.close()
    with sqlite3.connect(config.storage.sqlite_path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {
            "metadata",
            "applications",
            "app_keys",
            "integrations",
            "connections",
            "sessions",
            "session_connections",
            "connect_sessions",
            "calls",
            "events",
        }
        assert "tools" not in {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM events WHERE app_id=? ORDER BY created_at DESC,id DESC LIMIT 50",
            ("demo",),
        ).fetchone()
        assert "events_app_time" in plan[3]


async def test_old_layout_is_rejected_without_changing_data(config):
    config.storage.sqlite_path.parent.mkdir(mode=0o700)
    with sqlite3.connect(config.storage.sqlite_path) as db:
        db.executescript(
            "CREATE TABLE legacy(value TEXT); INSERT INTO legacy VALUES('keep'); PRAGMA user_version=1;"
        )
    with pytest.raises(ValueError, match="Unsupported demo database layout"):
        await Service(config).start()
    with sqlite3.connect(config.storage.sqlite_path) as db:
        assert db.execute("SELECT * FROM legacy").fetchall() == [("keep",)]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("legacy",)
        ]
