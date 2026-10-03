"""Single-use human approvals for write tools; the database decides, events only wake."""

import asyncio
import json
import logging
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Literal

from ravn.common import Principal, RavnError, canonical, identifier, now
from ravn.control import AuditTarget
from ravn.store import event, finish, one, rows

ESCAPES = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]?|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[PX^_][^\x1b]*(?:\x1b\\)?|.?)",
    re.S,
)
WAITERS = 256
# Per session, so one prompt-injected agent cannot crowd out everyone else's writes.
SESSION_WAITERS = 4
ITEM_FIELDS = (
    "id",
    "app_id",
    "tenant_id",
    "user_id",
    "session_id",
    "connection_id",
    "tool",
    "created_at",
    "expires_at",
)


def clean(text: str, limit: int) -> str:
    """Make agent-controlled text safe to print in a terminal.

    Escape sequences go first, then every control and format character except
    newline and tab (C0, DEL, C1, bidi overrides, zero-width), so nothing can
    move the cursor, rewrite the prompt or reorder what the operator reads.
    """
    text = ESCAPES.sub("", text)
    text = "".join(
        c for c in text if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cf", "Zl", "Zp"}
    )
    return text if len(text) <= limit else text[:limit] + "\n... [truncated]"


def scrub(value):
    """Clean every string in decoded arguments, keys included, without cutting any."""
    if isinstance(value, str):
        return clean(value, len(value))
    if isinstance(value, dict):
        return {scrub(str(k)): scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


async def quietly(awaitable):
    """Run clean-up without letting its failure replace the error being handled.

    Rows left behind are expired by recover() on the next start.
    """
    try:
        await finish(awaitable)
    except Exception:
        logging.getLogger("ravn").warning("Could not settle an approval")


class Approvals:
    def __init__(self, service):
        self.service = service
        self.timeout = service.config.approvals.timeout_seconds
        self.waiters: dict[str, asyncio.Event] = {}
        self.owners: dict[str, str | None] = {}
        self.version = 0
        self.signal = asyncio.Event()

    def changed(self):
        self.version += 1
        self.signal.set()
        self.signal = asyncio.Event()

    async def wait(self, p, cid, name, arguments, fingerprint) -> str:
        """Hold a write until the operator decides, holding no slot or transaction.

        The waiter is registered before its row commits, so a decision cannot
        be missed. Waking proves nothing: the row's status is read back and is
        the only answer. Every way out but approval leaves the row terminal.
        """
        if len(self.waiters) >= WAITERS:
            raise RavnError(429, "rate_limited", "Too many writes are waiting for approval.")
        if sum(owner == p.session_id for owner in self.owners.values()) >= SESSION_WAITERS:
            raise RavnError(
                429, "rate_limited", "This session has too many writes waiting for approval."
            )
        approval_id = identifier("apr")
        woken = self.waiters[approval_id] = asyncio.Event()
        self.owners[approval_id] = p.session_id
        try:
            created = now()
            expiry = (
                (datetime.now(UTC) + timedelta(seconds=self.timeout))
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )
            ciphertext = self.service.cipher.encrypt(
                canonical(arguments).decode(), p.app, p.tenant, approval_id
            )
            async with self.service.store.transaction() as db:
                await db.execute(
                    "INSERT INTO approvals(id,app_id,tenant_id,user_id,connection_id,session_id,"
                    "tool,arguments_fingerprint,arguments_ciphertext,status,created_at,expires_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?)",
                    (
                        approval_id,
                        *p.namespace,
                        cid,
                        p.session_id,
                        name,
                        fingerprint,
                        ciphertext,
                        created,
                        expiry,
                    ),
                )
                await event(db, p, "approval.requested", approval_id)
            self.changed()
            try:
                async with asyncio.timeout(self.timeout):
                    await woken.wait()
            except TimeoutError:
                await finish(self.settle(p, approval_id, "expired"))
            async with self.service.store.transaction() as db:
                row = await one(db, "SELECT status FROM approvals WHERE id=?", (approval_id,))
        except asyncio.CancelledError:
            await quietly(self.settle(p, approval_id, "cancelled", only=("pending", "approved")))
            raise
        finally:
            self.waiters.pop(approval_id, None)
            self.owners.pop(approval_id, None)
        status = row["status"] if row else None
        if status == "approved":
            return approval_id
        if status == "denied":
            raise RavnError(403, "approval_denied", "The operator denied this action.")
        raise RavnError(403, "approval_expired", "No decision was made in time.")

    async def decide(self, approval_id: str, decision: Literal["approve", "deny"]) -> dict:
        status = {"approve": "approved", "deny": "denied"}[decision]
        async with self.service.store.transaction() as db:
            row = await one(
                db, "SELECT app_id,tenant_id,user_id FROM approvals WHERE id=?", (approval_id,)
            )
            if row is None:
                raise RavnError(404, "not_found", "Approval not found.")
            cursor = await db.execute(
                "UPDATE approvals SET status=?,decided_at=?,arguments_ciphertext=NULL "
                "WHERE id=? AND status='pending'",
                (status, now(), approval_id),
            )
            if not cursor.rowcount:
                raise RavnError(409, "approval_not_pending", "This approval is no longer pending.")
            await event(
                db,
                AuditTarget(row["app_id"], row["tenant_id"], row["user_id"]),
                "approval." + status,
                approval_id,
                actor_kind="local_operator",
                actor_id="unix-owner",
            )
        woken = self.waiters.get(approval_id)
        if woken:
            woken.set()
        self.changed()
        return {"id": approval_id, "status": status}

    async def consume(self, db, p, approval_id, fingerprint, call_id):
        cursor = await db.execute(
            "UPDATE approvals SET status='consumed',call_id=? WHERE id=? AND app_id=? AND "
            "tenant_id=? AND session_id=? AND status='approved' AND arguments_fingerprint=?",
            (call_id, approval_id, p.app, p.tenant, p.session_id, fingerprint),
        )
        if not cursor.rowcount:
            raise RavnError(403, "approval_invalid", "This approval cannot be used for this call.")

    async def release(self, p, approval_id):
        await self.settle(p, approval_id, "expired", only=("approved",))

    async def settle(self, p, approval_id, status, *, only=("pending",)) -> bool:
        async with self.service.store.transaction() as db:
            cursor = await db.execute(
                "UPDATE approvals SET status=?,decided_at=?,arguments_ciphertext=NULL "
                f"WHERE id=? AND status IN ({','.join('?' * len(only))})",
                (status, now(), approval_id, *only),
            )
            if cursor.rowcount and status in {"expired", "cancelled"}:
                await event(db, p, "approval." + status, approval_id)
        if cursor.rowcount:
            self.changed()
        return bool(cursor.rowcount)

    async def recover(self):
        """A restart drops every waiter, so nothing undecided or unused survives it."""
        async with self.service.store.transaction() as db:
            stale = await rows(
                db,
                "SELECT id,app_id,tenant_id,user_id FROM approvals "
                "WHERE status IN ('pending','approved')",
            )
            for row in stale:
                await db.execute(
                    "UPDATE approvals SET status='expired',decided_at=?,arguments_ciphertext=NULL "
                    "WHERE id=?",
                    (now(), row["id"]),
                )
                await event(
                    db,
                    Principal(row["app_id"], row["tenant_id"], row["user_id"]),
                    "approval.expired",
                    row["id"],
                    actor_kind="system",
                    actor_id="restart",
                )

    def item(self, row):
        try:
            text = self.service.cipher.decrypt(
                row["arguments_ciphertext"], row["app_id"], row["tenant_id"], row["id"]
            )
            # Never cut here: a cut JSON blob hides every key after it. The CLI
            # renders each argument separately and caps only long values.
            arguments = json.dumps(scrub(json.loads(text)), ensure_ascii=False, sort_keys=True)
        except Exception:
            arguments = "<unreadable>"
        return {**{k: row[k] for k in ITEM_FIELDS}, "arguments": arguments}

    async def pending(self, wait: int, after: int | None = None) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while True:
            signal, version = self.signal, self.version
            async with self.service.store.transaction() as db:
                found = await rows(
                    db, "SELECT * FROM approvals WHERE status='pending' ORDER BY created_at,id"
                )
            left = deadline - loop.time()
            if (after is None and found) or (after is not None and version != after) or left <= 0:
                return {"version": version, "data": [self.item(row) for row in found]}
            try:
                async with asyncio.timeout(left):
                    await signal.wait()
            except TimeoutError:
                pass
