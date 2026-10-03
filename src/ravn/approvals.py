"""Single-use human approvals for write tools; the database decides, events only wake."""

import asyncio
import contextlib
import json
import logging
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Literal

from mcp import types

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
ELICITATION = "ravn_approval"
# No fields: the client's own Accept and Decline buttons are the decision.
DECISION = {"type": "object", "properties": {}}
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


# Short fields that decide where a write goes come first, so a long body can
# never push them out of view.
FIRST = ("to", "cc", "bcc", "subject", "replyToMessageId")
VALUE_LIMIT = 2000


def render_arguments(raw) -> list[str]:
    try:
        arguments = json.loads(raw)
    except (TypeError, ValueError):
        arguments = None
    if not isinstance(arguments, dict):
        return ["    " + clean(str(raw), 4000).replace("\n", "\n    ")]
    keys = [k for k in FIRST if k in arguments]
    keys += sorted(k for k in arguments if k not in FIRST)
    lines, cut = [], False
    for key in keys:
        value = arguments[key]
        items = value if isinstance(value, list) else [value]
        texts = []
        for item in items:
            text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False, indent=2)
            text = clean(text, len(text))
            # Where a write goes is never cut; the manifest already bounds those fields.
            if key not in FIRST and len(text) > VALUE_LIMIT:
                cut = True
                text = f"{text[:VALUE_LIMIT]}\n... [truncated, {len(text)} chars total]"
            texts.append(text)
        if isinstance(value, list):
            text = "".join("\n- " + t for t in texts) if texts else "[]"
        else:
            text = texts[0]
        lines.append(f"    {clean(key, 256)}: " + text.replace("\n", "\n      "))
    if not keys:
        lines.append("    (none)")
    if cut:
        lines.append("    ! Some values are truncated; every argument is listed above.")
    return lines


def summary(name, arguments) -> str:
    """One line saying what the write does and to whom.

    Clients fold a long prompt after its first lines, so recipients and subject
    go here, uncut, where they cannot be folded away.
    """
    line = f"RAVN: approve {clean(name, 100)}"
    if arguments.get("to"):
        line += " to " + ", ".join(clean(str(a), 254) for a in arguments["to"])
    for field in ("cc", "bcc"):
        if arguments.get(field):
            line += f" ({field}: " + ", ".join(clean(str(a), 254) for a in arguments[field]) + ")"
    if isinstance(arguments.get("subject"), str):
        line += f' with subject "{clean(arguments["subject"], 998)}"'
    return line + "?"


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
        self.version = 0
        self.signal = asyncio.Event()

    def changed(self):
        self.version += 1
        self.signal.set()
        self.signal = asyncio.Event()

    def admit_waiter(self, p):
        if len(self.waiters) >= WAITERS:
            raise RavnError(429, "rate_limited", "Too many writes are waiting for approval.")

    async def create(self, p, cid, name, arguments, fingerprint, approval_id):
        """Record a pending request; a session may only have a few at once."""
        expiry = (
            (datetime.now(UTC) + timedelta(seconds=self.timeout))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        ciphertext = self.service.cipher.encrypt(
            canonical(arguments).decode(), p.app, p.tenant, approval_id
        )
        async with self.service.store.transaction() as db:
            # Counted in the database: a client-answered request has no waiter here.
            found = await one(
                db,
                "SELECT COUNT(*) AS n FROM approvals WHERE app_id=? AND tenant_id=? "
                "AND session_id=? AND status='pending' AND expires_at>?",
                (p.app, p.tenant, p.session_id, now()),
            )
            if found["n"] >= SESSION_WAITERS:
                raise RavnError(
                    429, "rate_limited", "This session has too many writes waiting for approval."
                )
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
                    now(),
                    expiry,
                ),
            )
            await event(db, p, "approval.requested", approval_id)
        self.changed()

    async def wait(self, p, cid, name, arguments, fingerprint) -> str:
        """Hold a write until the operator decides, holding no slot or transaction."""
        self.admit_waiter(p)
        approval_id = identifier("apr")
        woken = self.waiters[approval_id] = asyncio.Event()
        try:
            await self.create(p, cid, name, arguments, fingerprint, approval_id)
        except BaseException:
            self.waiters.pop(approval_id, None)
            raise
        return await self.outcome(p, approval_id, woken, self.timeout)

    async def outcome(self, p, approval_id, woken, seconds) -> str:
        """Wait for a decision on a registered waiter, then read the row's answer.

        The waiter is registered before anyone could decide, so a decision cannot
        be missed. Waking proves nothing: the row's status is the only answer.
        Every way out but approval leaves the row terminal.
        """
        try:
            if await self.status(approval_id) == "pending":
                try:
                    async with asyncio.timeout(max(seconds, 0)):
                        await woken.wait()
                except TimeoutError:
                    await finish(self.settle(p, approval_id, "expired"))
            status = await self.status(approval_id)
        except asyncio.CancelledError:
            await quietly(self.settle(p, approval_id, "cancelled", only=("pending", "approved")))
            raise
        finally:
            self.waiters.pop(approval_id, None)
        if status == "approved":
            return approval_id
        if status == "denied":
            raise RavnError(403, "approval_denied", "The operator denied this action.")
        raise RavnError(403, "approval_expired", "No decision was made in time.")

    async def status(self, approval_id):
        async with self.service.store.transaction() as db:
            row = await one(db, "SELECT status FROM approvals WHERE id=?", (approval_id,))
        return row["status"] if row else None

    async def ask(self, p, cid, name, arguments, fingerprint) -> types.InputRequiredResult:
        """Ask the person at the agent's client, through an MCP elicitation.

        Nothing waits here: the client shows the prompt and calls again with the
        answer and this approval's id. The operator can still decide meanwhile.
        """
        approval_id = identifier("apr")
        await self.create(p, cid, name, arguments, fingerprint, approval_id)
        lines = render_arguments(json.dumps(scrub(arguments)))
        message = "\n".join(
            [summary(name, arguments), "", *(line.removeprefix("    ") for line in lines)]
        )
        return types.InputRequiredResult(
            input_requests={
                ELICITATION: types.ElicitRequest(
                    params=types.ElicitRequestFormParams(message=message, requested_schema=DECISION)
                )
            },
            request_state=approval_id,
        )

    async def answer(self, p, approval_id, name, fingerprint, response, *, fallback) -> str:
        """Apply the person's answer from the client to the approval it was asked for.

        The id only names a pending request of this session for these exact
        arguments; anything else is refused. Dismissing the prompt hands the
        decision to the operator when `fallback` allows it, and denies otherwise.
        """
        async with self.service.store.transaction() as db:
            row = await one(
                db,
                "SELECT status,expires_at FROM approvals WHERE id=? AND app_id=? AND tenant_id=? "
                "AND session_id=? AND tool=? AND arguments_fingerprint=?",
                (approval_id, p.app, p.tenant, p.session_id, name, fingerprint),
            )
        if row is None or row["status"] == "consumed":
            raise RavnError(403, "approval_invalid", "This approval cannot be used for this call.")
        if row["status"] == "pending" and row["expires_at"] <= now():
            await self.settle(p, approval_id, "expired")
        action = response.action if isinstance(response, types.ElicitResult) else None
        decision = {"accept": "approve", "decline": "deny"}.get(action)
        if decision:
            with contextlib.suppress(RavnError):
                await self.decide(approval_id, decision, actor=("agent_client", p.session_id))
        elif fallback:
            self.admit_waiter(p)
            woken = self.waiters[approval_id] = asyncio.Event()
            remaining = (
                datetime.fromisoformat(row["expires_at"]) - datetime.now(UTC)
            ).total_seconds()
            return await self.outcome(p, approval_id, woken, remaining)
        else:
            with contextlib.suppress(RavnError):
                await self.decide(approval_id, "deny", actor=("agent_client", p.session_id))
        # A decision made elsewhere first still stands; the row is the only answer.
        status = await self.status(approval_id)
        if status == "approved":
            return approval_id
        if status == "denied":
            raise RavnError(403, "approval_denied", "The approval was denied.")
        raise RavnError(403, "approval_expired", "No decision was made in time.")

    async def decide(
        self,
        approval_id: str,
        decision: Literal["approve", "deny"],
        *,
        actor=("local_operator", "unix-owner"),
    ) -> dict:
        status = {"approve": "approved", "deny": "denied"}[decision]
        async with self.service.store.transaction() as db:
            row = await one(
                db, "SELECT app_id,tenant_id,user_id FROM approvals WHERE id=?", (approval_id,)
            )
            if row is None:
                raise RavnError(404, "not_found", "Approval not found.")
            cursor = await db.execute(
                "UPDATE approvals SET status=?,decided_at=?,arguments_ciphertext=NULL "
                "WHERE id=? AND status='pending' AND expires_at>?",
                (status, now(), approval_id, now()),
            )
            if not cursor.rowcount:
                raise RavnError(409, "approval_not_pending", "This approval is no longer pending.")
            await event(
                db,
                AuditTarget(row["app_id"], row["tenant_id"], row["user_id"]),
                "approval." + status,
                approval_id,
                actor_kind=actor[0],
                actor_id=actor[1],
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
            # A client may never come back to answer; its request lapses here.
            stamp = now()
            for row in [r for r in found if r["expires_at"] <= stamp]:
                await self.settle(
                    Principal(row["app_id"], row["tenant_id"], row["user_id"]), row["id"], "expired"
                )
            found = [r for r in found if r["expires_at"] > stamp]
            left = deadline - loop.time()
            if (after is None and found) or (after is not None and version != after) or left <= 0:
                return {"version": version, "data": [self.item(row) for row in found]}
            try:
                async with asyncio.timeout(left):
                    await signal.wait()
            except TimeoutError:
                pass
