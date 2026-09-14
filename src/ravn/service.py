"""Shared authorization/execution engine, independent of REST and MCP envelopes."""

import asyncio
import contextlib
import hmac
import json
import re
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from mcp import types

from ravn import control
from ravn.common import Principal, RavnError, canonical, digest, identifier, now, token
from ravn.config import Config
from ravn.crypto import Cipher
from ravn.github import GitHub
from ravn.gmail import Gmail
from ravn.manifest import check_schema, is_write, schema_hash, schemas_for, validate_arguments
from ravn.oauth import Onboarding
from ravn.slack import Slack
from ravn.store import Store, event, finish, one, rows

ACTOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
# Provider answers that prove a dispatched request was refused, not executed.
DEFINITE_REFUSALS = {"credential_invalid", "provider_denied"}


class Replayed(RavnError):
    """Carries a known earlier outcome out of admission without dispatching."""

    def __init__(self, result):
        super().__init__(200, "idempotency_replayed", "Earlier outcome replayed.")
        self.result = result


CONNECTION_FIELDS = (
    "id",
    "integration_id",
    "status",
    "provider_account_id",
    "display_name",
    "label",
    "epoch",
    "revision",
    "created_at",
    "updated_at",
    "credential_type",
    "credential_version",
)


def public_connection(row):
    return {**{k: row[k] for k in CONNECTION_FIELDS}, "owner_kind": "user", "restrictions": None}


def public_session(row):
    status = (
        "revoked" if row["revoked_at"] else ("expired" if row["expires_at"] <= now() else "active")
    )
    return {
        k: row[k]
        for k in (
            "id",
            "connection_id",
            "connection_epoch",
            "created_at",
            "expires_at",
            "revoked_at",
        )
    } | {"status": status, "restrictions": None}


class Service:
    def __init__(self, config: Config, provider=None):
        self.config = config
        self.cipher = Cipher(config)
        self.store = Store(config, self.cipher)
        self.providers = (
            provider
            if isinstance(provider, dict)
            else {
                "github_cloud": provider
                or GitHub(config.limits.call_timeout_seconds, config.limits.result_bytes)
            }
        )
        self.providers.setdefault(
            "slack", Slack(config.limits.call_timeout_seconds, config.limits.result_bytes)
        )
        self.providers.setdefault(
            "gmail", Gmail(config.limits.call_timeout_seconds, config.limits.result_bytes)
        )
        self.provider = self.providers["github_cloud"]  # Backward-compatible test/inspection hook.
        self.oauth = Onboarding(self)
        self.oauth_sweeper = None
        self.ready = False
        self.counts = Counter()
        self.cursors = {}

    async def start(self):
        await self.store.open()
        await self.oauth.cleanup()
        self.oauth_sweeper = asyncio.create_task(self.oauth.sweep())
        self.ready = True

    async def close(self):
        self.ready = False
        if self.oauth_sweeper:
            self.oauth_sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.oauth_sweeper
        await self.store.close()

    def provider_for(self, integration):
        return self.providers[integration.connector]

    @asynccontextmanager
    async def slots(self, p: Principal, connection: str):
        keys = [("global",), ("app", p.app), ("connection", p.app, p.tenant, connection)]
        if any(self.counts[k] >= cap for k, cap in zip(keys, (64, 16, 4), strict=True)):
            raise RavnError(429, "rate_limited", "Too many concurrent provider operations.")
        for key in keys:
            self.counts[key] += 1
        try:
            yield
        finally:
            for key in keys:
                self.counts[key] -= 1
                if not self.counts[key]:
                    del self.counts[key]

    async def authenticate_app(
        self, bearer: str, user: str | None, tenant: str | None
    ) -> Principal:
        if not re.fullmatch(r"rv_app_[a-f0-9]{32}_[A-Za-z0-9_-]{43}", bearer):
            raise RavnError(401, "unauthenticated", "A valid application key is required.")
        key_id = "key_" + bearer.split("_", 3)[2]
        async with self.store.transaction() as db:
            key = await one(db, "SELECT * FROM app_keys WHERE id=?", (key_id,))
            if (
                not key
                or key["revoked_at"]
                or not hmac.compare_digest(key["secret_hash"], digest(bearer))
            ):
                raise RavnError(401, "unauthenticated", "A valid application key is required.")
            app = self.config.app(key["app_id"])
            if app is None:
                raise RavnError(401, "unauthenticated", "Application access is disabled.")
            if user is None or not ACTOR.fullmatch(user):
                raise RavnError(400, "invalid_request", "A valid X-Ravn-User-Id is required.")
            if app.tenant_mode == "single":
                if tenant not in {None, "default"}:
                    raise RavnError(
                        400, "invalid_request", "Single-tenant applications use default."
                    )
                tenant = "default"
            elif tenant is None or not ACTOR.fullmatch(tenant):
                raise RavnError(400, "invalid_request", "A valid X-Ravn-Tenant-Id is required.")
            return Principal(app.id, tenant, user, key_id=key_id)

    async def authenticate_session(self, bearer: str) -> Principal:
        if not re.fullmatch(r"rv_sess_[a-f0-9]{32}_[A-Za-z0-9_-]{43}", bearer):
            raise RavnError(401, "unauthenticated", "A valid runtime session token is required.")
        async with self.store.transaction() as db:
            row = await one(db, "SELECT * FROM sessions WHERE token_hash=?", (digest(bearer),))
            if row is None:
                raise RavnError(
                    401, "unauthenticated", "A valid runtime session token is required."
                )
            p = Principal(
                row["app_id"],
                row["tenant_id"],
                row["user_id"],
                session_id=row["id"],
                connection_id=row["connection_id"],
            )
            await self.authorize(db, p, row["connection_id"], execute=True)
            return p

    async def validate_principal(self, db, p: Principal):
        if self.config.app(p.app) is None:
            raise RavnError(401, "unauthenticated", "Application access is disabled.")
        if p.key_id:
            key = await one(db, "SELECT * FROM app_keys WHERE id=? AND app_id=?", (p.key_id, p.app))
            if key is None or key["revoked_at"]:
                raise RavnError(401, "unauthenticated", "Application key is no longer active.")
        elif not p.session_id:
            raise RavnError(401, "unauthenticated", "Authentication is required.")

    async def authorize(self, db, p: Principal, connection_id: str, *, execute=False, tool=None):
        await self.validate_principal(db, p)
        conn = await one(
            db,
            "SELECT * FROM connections WHERE app_id=? AND tenant_id=? AND user_id=? AND id=?",
            (*p.namespace, connection_id),
        )
        if conn is None or (p.session_id and connection_id != p.connection_id):
            raise RavnError(404, "not_found", "Connection not found.")
        integration = self.config.integration(p.app, conn["integration_id"])
        if execute:
            if conn["status"] == "reconnect_required":
                raise RavnError(
                    409,
                    "connection_reauth_required",
                    "Reconnect this account or import a new connection.",
                )
            if conn["status"] != "active" or integration is None or not integration.enabled:
                raise RavnError(409, "connection_disabled", "Connection is not executable.")
        if p.session_id:
            session = await one(
                db,
                "SELECT * FROM sessions WHERE app_id=? AND tenant_id=? AND user_id=? AND id=?",
                (*p.namespace, p.session_id),
            )
            if (
                session is None
                or session["revoked_at"]
                or session["expires_at"] <= now()
                or session["connection_epoch"] != conn["epoch"]
            ):
                raise RavnError(401, "unauthenticated", "Runtime session expired or was revoked.")
            if tool:
                ceiling = json.loads(session["tools"])
                if (
                    integration is None
                    or tool not in ceiling
                    or ceiling[tool] != integration.schema_hashes.get(tool)
                ):
                    raise RavnError(
                        403, "permission_denied", "Tool is outside this session's ceiling."
                    )
        return conn, integration

    async def credential(self, p: Principal, connection_id: str):
        return await self.oauth.credential(p, connection_id)

    async def import_connection(
        self, p: Principal, integration_id: str, credential: str, label=None
    ):
        integration = self.config.integration(p.app, integration_id)
        if integration is None:
            raise RavnError(404, "not_found", "Integration not found.")
        if not integration.enabled:
            raise RavnError(409, "connection_disabled", "Integration is disabled.")
        if any(ord(c) < 33 or ord(c) > 126 for c in credential):
            raise RavnError(400, "invalid_request", "Credential contains unsupported characters.")
        async with self.slots(p, "import"):
            account, display = await self.provider_for(integration).identify(credential)
        cid, created = identifier("conn"), now()
        cipher = self.cipher.encrypt(credential, p.app, p.tenant, cid)
        async with self.store.transaction() as db:
            await self.validate_principal(db, p)
            await db.execute(
                "INSERT INTO connections(id,app_id,tenant_id,user_id,integration_id,provider_account_id,"
                "display_name,label,status,ciphertext,key_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cid,
                    *p.namespace,
                    integration_id,
                    account,
                    display,
                    label,
                    "active",
                    cipher,
                    self.cipher.key_id,
                    created,
                    created,
                ),
            )
            await event(db, p, "connection.imported", cid)
            conn, _ = await self.authorize(db, p, cid)
            return public_connection(conn)

    async def connection(self, p, cid):
        async with self.store.transaction() as db:
            conn, _ = await self.authorize(db, p, cid)
            return public_connection(conn)

    async def disconnect(self, p, cid):
        async with self.store.transaction() as db:
            conn, _ = await self.authorize(db, p, cid)
            await control.disconnect(db, p, conn)
            conn, _ = await self.authorize(db, p, cid)
            return {"connection": public_connection(conn), "provider_revocation": "unsupported"}

    async def discover(self, p, cid):
        async with self.slots(p, cid):
            credential, integration = await self.credential(p, cid)
            try:
                tools = await self.provider_for(integration).discover(credential, integration)
            except RavnError as error:
                if error.code == "credential_invalid":
                    await self.oauth.rejected(p, cid, credential)
                raise
            async with self.store.transaction() as db:
                await self.authorize(db, p, cid, execute=True)
                if p.session_id:
                    session = await one(
                        db,
                        "SELECT tools FROM sessions WHERE app_id=? AND tenant_id=? AND id=?",
                        (p.app, p.tenant, p.session_id),
                    )
                    ceiling = json.loads(session["tools"])
                    tools = [
                        t
                        for t in tools
                        if t.name in ceiling
                        and ceiling[t.name] == integration.schema_hashes.get(t.name)
                    ]
            return tools

    async def inspect_connection(self, p, cid):
        async with self.slots(p, cid):
            credential, integration = await self.credential(p, cid)
            tools = await self.provider_for(integration).inspect(credential)
            result = {"schema_hashes": {}, "schemas_for_review": {}}
            for tool in tools:
                if tool.name not in schemas_for(integration.connector):
                    continue
                check_schema(tool.input_schema)
                if tool.output_schema is not None:
                    check_schema(tool.output_schema)
                if tool.name in result["schema_hashes"]:
                    raise RavnError(502, "schema_rejected", "Duplicate upstream tool name.")
                result["schema_hashes"][tool.name] = schema_hash(tool)
                result["schemas_for_review"][tool.name] = {
                    "input": tool.input_schema,
                    "output": tool.output_schema,
                }
            if credential in canonical(result).decode():
                raise RavnError(502, "schema_rejected", "Unsafe upstream schema was withheld.")
            async with self.store.transaction() as db:
                await self.authorize(db, p, cid, execute=True)
            return result

    async def create_session(self, p, cid, ttl=None):
        ttl = self.config.sessions.default_ttl_seconds if ttl is None else ttl
        if not 60 <= ttl <= self.config.sessions.max_ttl_seconds:
            raise RavnError(
                400, "invalid_request", "Requested lifetime exceeds the configured bounds."
            )
        tools = await self.discover(p, cid)
        if not tools:
            raise RavnError(
                409,
                "integration_not_ready",
                "No reviewed tools are available; inspect and pin schemas first.",
            )
        value = token("sess")
        sid = "sess_" + value.split("_", 3)[2]
        expiry = (
            (datetime.now(UTC) + timedelta(seconds=ttl))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        async with self.store.transaction() as db:
            conn, integration = await self.authorize(db, p, cid, execute=True)
            ceiling = {t.name: integration.schema_hashes[t.name] for t in tools}
            await db.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    sid,
                    *p.namespace,
                    cid,
                    conn["epoch"],
                    p.key_id,
                    digest(value),
                    json.dumps(ceiling),
                    now(),
                    expiry,
                ),
            )
            await event(db, p, "session.created", sid)
        return {
            "id": sid,
            "token": value,
            "mcp_url": self.config.server.public_url.rstrip("/") + "/mcp",
            "expires_at": expiry,
        }

    async def revoke_session(self, p, sid):
        async with self.store.transaction() as db:
            await self.validate_principal(db, p)
            row = await one(
                db,
                "SELECT * FROM sessions WHERE app_id=? AND tenant_id=? AND user_id=? AND id=?",
                (*p.namespace, sid),
            )
            if row is None:
                raise RavnError(404, "not_found", "Session not found.")
            await control.revoke_session(db, p, row)
            return public_session(row)

    async def previous_call(self, db, p, key_hash, fingerprint, request_id):
        """Resolve a keyed request against its earlier call without dispatching."""
        row = await one(
            db,
            "SELECT * FROM calls WHERE app_id=? AND tenant_id=? AND user_id=? AND idempotency_key_hash=?",
            (*p.namespace, key_hash),
        )
        if row is None:
            return None
        if not hmac.compare_digest(row["arguments_fingerprint"], fingerprint):
            raise RavnError(
                409,
                "idempotency_conflict",
                "This idempotency key was already used for a different request.",
            )
        if row["status"] == "running":
            raise RavnError(
                409,
                "call_in_progress",
                "The same request is still running; check its status instead of retrying.",
                call_id=row["id"],
            )
        if row["status"] == "unknown":
            raise RavnError(
                502,
                "outcome_unknown",
                "The earlier attempt may have executed. Reconcile before trying again.",
                call_id=row["id"],
            )
        # Known outcome. Results are never retained, so the replay carries none.
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text="This request already completed; its result is not retained.",
                )
            ],
            isError=row["status"] == "tool_error",
            _meta={
                "ravn/call-id": row["id"],
                "ravn/request-id": request_id,
                "ravn/idempotency-replayed": True,
            },
        )

    async def execute(self, p, cid, name, arguments, request_id, idempotency_key=None):
        async with self.store.transaction() as db:
            _, registered = await self.authorize(db, p, cid, execute=True, tool=name)
            validate_arguments(name, arguments, registered.connector)
        write = is_write(name, registered.connector)
        fingerprint = self.cipher.fingerprint([*p.namespace, cid, name, arguments])
        key_hash = (
            self.cipher.fingerprint([*p.namespace, "idempotency", idempotency_key])
            if idempotency_key
            else None
        )
        if key_hash:
            async with self.store.transaction() as db:
                replay = await self.previous_call(db, p, key_hash, fingerprint, request_id)
            if replay:
                return replay
        call_id, admitted, started = identifier("call"), False, time.monotonic()
        async with self.slots(p, cid):
            credential, integration = await self.credential(p, cid)

            async def admit():
                nonlocal admitted
                async with self.store.transaction() as db:
                    await self.authorize(db, p, cid, execute=True, tool=name)
                    # Transactions are serialized, so a concurrent duplicate that
                    # passed the early check is caught here, before dispatch.
                    if key_hash and (
                        replay := await self.previous_call(db, p, key_hash, fingerprint, request_id)
                    ):
                        raise Replayed(replay)
                    stamp = now()
                    await db.execute(
                        "INSERT INTO calls(id,app_id,tenant_id,user_id,connection_id,session_id,tool,"
                        "status,arguments_fingerprint,fingerprint_key_id,error_code,duration_ms,"
                        "created_at,updated_at,completed_at,effect,idempotency_key_hash) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            call_id,
                            *p.namespace,
                            cid,
                            p.session_id,
                            name,
                            "running",
                            fingerprint,
                            self.cipher.key_id,
                            None,
                            None,
                            stamp,
                            stamp,
                            None,
                            "write" if write else "read",
                            key_hash,
                        ),
                    )
                    admitted = True
                    await event(db, p, "call.admitted", call_id)

            try:
                result = await self.provider_for(integration).call(
                    credential, integration, name, arguments, admit
                )
                if not admitted:
                    raise RuntimeError("Provider bypassed dispatch admission")
                raw = result.model_dump(mode="json", by_alias=True, exclude_none=True)
                if (
                    credential in canonical(raw).decode()
                    or len(canonical(raw)) > self.config.limits.result_bytes
                ):
                    raise RavnError(
                        502, "upstream_unavailable", "Unsafe or oversized result was withheld."
                    )
                await self.finish_call(
                    p, call_id, "tool_error" if result.is_error else "succeeded", None, started
                )
                result.meta = {
                    **(result.meta or {}),
                    "ravn/call-id": call_id,
                    "ravn/request-id": request_id,
                }
                return result
            except BaseException as exc:
                if isinstance(exc, Replayed):
                    return exc.result
                if isinstance(exc, RavnError) and exc.code == "credential_invalid":
                    await self.oauth.rejected(p, cid, credential)
                # After dispatch, a write only definitely did not happen when the
                # provider refused the request itself. Everything else is ambiguous.
                ambiguous = write and not (
                    isinstance(exc, RavnError) and exc.code in DEFINITE_REFUSALS
                )
                if admitted:
                    code = exc.code if isinstance(exc, RavnError) else "upstream_unavailable"
                    status = "failed"
                    if ambiguous:
                        status, code = "unknown", "outcome_unknown"
                    elif isinstance(exc, asyncio.CancelledError):
                        status = "unknown"
                    await finish(self.finish_call(p, call_id, status, code, started))
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if admitted and ambiguous:
                    raise RavnError(
                        502,
                        "outcome_unknown",
                        "The write may have executed. Reconcile before trying again; nothing was retried.",
                        call_id=call_id,
                    ) from None
                if isinstance(exc, RavnError):
                    # Keep a call ID set by an earlier keyed call; never invent one.
                    if admitted:
                        exc.call_id = call_id
                    raise
                raise RavnError(
                    503,
                    "dependency_unavailable",
                    "Tool call could not complete.",
                    call_id=call_id if admitted else None,
                ) from None

    async def finish_call(self, p, call_id, status, code, started):
        async with self.store.transaction() as db:
            # A definite failure releases its idempotency key so the same logical
            # action can be retried, for example after reconnecting.
            cursor = await db.execute(
                "UPDATE calls SET status=?,error_code=?,duration_ms=?,updated_at=?,completed_at=?,"
                "idempotency_key_hash=CASE WHEN ?='failed' THEN NULL ELSE idempotency_key_hash END "
                "WHERE app_id=? AND tenant_id=? AND id=? AND status='running'",
                (
                    status,
                    code,
                    int((time.monotonic() - started) * 1000),
                    now(),
                    now(),
                    status,
                    p.app,
                    p.tenant,
                    call_id,
                ),
            )
            if cursor.rowcount:
                await event(db, p, "call." + status, call_id)

    async def call_status(self, p, call_id):
        async with self.store.transaction() as db:
            await self.validate_principal(db, p)
            row = await one(
                db,
                "SELECT * FROM calls WHERE app_id=? AND tenant_id=? AND user_id=? AND id=?",
                (*p.namespace, call_id),
            )
            if row is None:
                raise RavnError(404, "not_found", "Call not found.")
            return {
                "call_id": row["id"],
                "result_available": False,
                **{
                    k: row[k]
                    for k in (
                        "connection_id",
                        "session_id",
                        "tool",
                        "effect",
                        "status",
                        "arguments_fingerprint",
                        "fingerprint_key_id",
                        "duration_ms",
                        "created_at",
                        "updated_at",
                        "completed_at",
                    )
                },
                **({"error_code": row["error_code"]} if row["error_code"] else {}),
            }

    async def page(self, p, table, cursor=None, limit=None, connection_id=None):
        assert table in {"connections", "sessions"}
        stamp = time.monotonic()
        self.cursors = {k: v for k, v in self.cursors.items() if v[0] > stamp}
        context = (p.namespace, table)
        last = None
        if cursor is not None:
            entry = self.cursors.get(cursor)
            if (
                not entry
                or entry[1] != context
                or (limit is not None and limit != entry[2])
                or (connection_id is not None and connection_id != entry[3])
            ):
                raise RavnError(400, "invalid_cursor", "Invalid, expired, or mismatched cursor.")
            _, _, limit, connection_id, last = entry
        limit = 50 if limit is None else limit
        if not 1 <= limit <= 100:
            raise RavnError(400, "invalid_request", "Page size must be between 1 and 100.")
        sql = f"SELECT * FROM {table} WHERE app_id=? AND tenant_id=? AND user_id=?"
        params = list(p.namespace)
        if connection_id:
            sql += " AND connection_id=?"
            params.append(connection_id)
        if last:
            sql += " AND (created_at,id)<(?,?)"
            params.extend(last)
        sql += " ORDER BY created_at DESC,id DESC LIMIT ?"
        params.append(limit + 1)
        async with self.store.transaction() as db:
            await self.validate_principal(db, p)
            result = await rows(db, sql, params)
        next_cursor = None
        if len(result) > limit:
            if len(self.cursors) >= 1024:
                raise RavnError(429, "rate_limited", "Too many active list cursors.")
            next_cursor = token("cursor")
            edge = result[limit - 1]
            self.cursors[next_cursor] = (
                stamp + 300,
                context,
                limit,
                connection_id,
                (edge["created_at"], edge["id"]),
            )
        transform = public_connection if table == "connections" else public_session
        return {"data": [transform(row) for row in result[:limit]], "next_cursor": next_cursor}

    async def create_key(self, app_id, label):
        if self.config.app(app_id) is None:
            raise RavnError(404, "not_found", "Application not found or disabled.")
        value, created = token("app"), now()
        kid = "key_" + value.split("_", 3)[2]
        async with self.store.transaction() as db:
            await db.execute(
                "INSERT INTO app_keys VALUES(?,?,?,?,?,NULL)",
                (kid, app_id, digest(value), label, created),
            )
            await event(
                db,
                control.AuditTarget(app_id),
                "application_key.created",
                kid,
                actor_kind="local_operator",
                actor_id="unix-owner",
            )
        return {"id": kid, "app_id": app_id, "key": value, "created_at": created}

    async def revoke_key(self, key_id, revoke_sessions=False):
        async with self.store.transaction() as db:
            key = await one(db, "SELECT * FROM app_keys WHERE id=?", (key_id,))
            if key is None:
                raise RavnError(404, "not_found", "Application key not found.")
            await control.revoke_key(
                db,
                control.AuditTarget(key["app_id"]),
                key,
                revoke_sessions,
                actor_kind="local_operator",
                actor_id="unix-owner",
            )
        return {"id": key_id, "revoked": True, "sessions_revoked": revoke_sessions}
