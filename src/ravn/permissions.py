"""Per-session tool rules. Rules only ever narrow; they never grant."""

from ravn.common import now
from ravn.store import one, rows


async def session_rules(db, app_id: str, tenant_id: str, session_id: str) -> dict[str, bool]:
    found = await rows(
        db,
        "SELECT tool,allowed FROM session_tool_rules WHERE app_id=? AND tenant_id=? AND session_id=?",
        (app_id, tenant_id, session_id),
    )
    return {row["tool"]: bool(row["allowed"]) for row in found}


def effective_tools(ceiling: dict[str, str], integration, rules: dict[str, bool]) -> list[str]:
    """Names a session may still use: its ceiling, still pinned, and not denied.

    An absent rule leaves a tool allowed, so a session with no rules keeps the
    ceiling it was issued with.
    """
    if integration is None:
        return []
    return sorted(
        name
        for name, pin in ceiling.items()
        if integration.schema_hashes.get(name) == pin and rules.get(name, True)
    )


async def set_session_rules(db, app_id: str, tenant_id: str, session_id: str, tools: dict) -> None:
    stamp = now()
    for tool, allowed in tools.items():
        await db.execute(
            "INSERT INTO session_tool_rules VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(app_id,tenant_id,session_id,tool) "
            "DO UPDATE SET allowed=excluded.allowed,updated_at=excluded.updated_at",
            (app_id, tenant_id, session_id, tool, 1 if allowed else 0, stamp),
        )


async def session_ceiling(db, app_id: str, tenant_id: str, session_id: str):
    return await one(
        db,
        "SELECT tools FROM sessions WHERE app_id=? AND tenant_id=? AND id=?",
        (app_id, tenant_id, session_id),
    )
