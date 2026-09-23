"""Atomic access-reduction operations shared by authenticated app and operator callers.

Callers must authorize and resolve their target inside the same transaction.
These helpers deliberately do not grant authority or authenticate callers.
"""

from dataclasses import dataclass

from ravn.common import now
from ravn.store import event


@dataclass(frozen=True)
class AuditTarget:
    """Affected namespace only; never accepted by authentication/authorization."""

    app: str | None
    tenant: str | None = None
    user: str | None = None


async def disconnect(db, p, connection, **audit):
    if connection["status"] == "disconnected":
        return
    await db.execute(
        "UPDATE connections SET status='disconnected',ciphertext=NULL,refresh_attempt=NULL,epoch=epoch+1,"
        "revision=revision+1,updated_at=? WHERE app_id=? AND tenant_id=? AND id=?",
        (now(), p.app, p.tenant, connection["id"]),
    )
    from ravn.oauth import WIPE

    await db.execute(
        f"UPDATE connect_sessions SET status='cancelled',{WIPE},updated_at=? WHERE app_id=? AND tenant_id=? AND reconnect_id=? AND status IN ('pending','authorizing','exchanging','awaiting_completion')",
        (now(), p.app, p.tenant, connection["id"]),
    )
    await event(db, p, "connection.disconnected", connection["id"], **audit)


async def revoke_session(db, p, session, **audit):
    if session["revoked_at"] is not None:
        return
    session["revoked_at"] = now()
    await db.execute(
        "UPDATE sessions SET revoked_at=? WHERE app_id=? AND tenant_id=? AND id=?",
        (session["revoked_at"], p.app, p.tenant, session["id"]),
    )
    await event(db, p, "session.revoked", session["id"], **audit)


async def revoke_key(db, p, key, revoke_sessions, **audit):
    await db.execute(
        "UPDATE app_keys SET revoked_at=COALESCE(revoked_at,?) WHERE id=?", (now(), key["id"])
    )
    from ravn.oauth import WIPE

    await db.execute(
        f"UPDATE connect_sessions SET status='cancelled',{WIPE},updated_at=? WHERE issuing_key_id=? AND status IN ('pending','authorizing','exchanging','awaiting_completion')",
        (now(), key["id"]),
    )
    if revoke_sessions:
        await db.execute(
            "UPDATE sessions SET revoked_at=COALESCE(revoked_at,?) WHERE issuing_key_id=?",
            (now(), key["id"]),
        )
    await event(
        db,
        p,
        "application_key.compromised" if revoke_sessions else "application_key.revoked",
        key["id"],
        **audit,
    )
