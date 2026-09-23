"""Browser-bound OAuth staging, explicit application completion, and rotation recovery."""

import asyncio
import hmac
import json
import logging
import secrets
from contextlib import asynccontextmanager
from urllib.parse import urlencode

from ravn.common import Principal, RavnError, canonical, digest, identifier, now
from ravn.oauth_provider import OAuthProvider, deadline
from ravn.store import event, finish, one

LIVE = ("pending", "authorizing", "exchanging", "awaiting_completion")
WIPE = "ticket_hash=NULL,state_hash=NULL,cookie_hash=NULL,verifier=NULL,staged=NULL,completion_hash=NULL"
logger = logging.getLogger(__name__)


def public_flow(row):
    return {
        k: row[k]
        for k in (
            "id",
            "integration_id",
            "expires_at",
            "completion_expires_at",
            "connection_id",
            "created_at",
            "updated_at",
        )
    } | {
        "status": "pending" if row["status"] in {"authorizing", "exchanging"} else row["status"],
        **({"failure_code": row["failure_code"]} if row["failure_code"] else {}),
    }


class Onboarding:
    def __init__(self, service):
        self.service = service
        self.store, self.cipher, self.config = service.store, service.cipher, service.config
        self.refresh_locks = {}

    def provider(self, integration):
        callback = (
            self.config.server.public_url.rstrip("/")
            + f"/oauth/callback/{integration.app_id}/{integration.id}"
        )
        registered = self.service.integrations.get(integration.app_id, integration.id)
        return OAuthProvider(
            integration,
            self.service.provider_for(integration),
            callback,
            client_secret=(
                registered.oauth.client_secret.get_secret_value()
                if registered.oauth.client_secret
                else None
            ),
        )

    def fingerprint(self, integration):
        return digest(integration.model_dump_json())

    async def cleanup(self):
        async with self.store.transaction() as db:
            await db.execute(
                f"UPDATE connect_sessions SET status='expired',{WIPE},updated_at=? WHERE status IN ('pending','authorizing','exchanging','awaiting_completion') AND (expires_at<=? OR completion_expires_at<=?)",
                (now(), now(), now()),
            )
            await db.execute(
                "DELETE FROM connect_sessions WHERE status NOT IN ('pending','authorizing','exchanging','awaiting_completion') AND updated_at<?",
                (deadline(-86400),),
            )

    async def sweep(self):
        while True:
            await self.cleanup()
            await asyncio.sleep(30)

    async def row(self, db, p, sid):
        await self.service.validate_principal(db, p)
        row = await one(
            db,
            "SELECT * FROM connect_sessions WHERE app_id=? AND tenant_id=? AND user_id=? AND id=?",
            (*p.namespace, sid),
        )
        if row is None:
            raise RavnError(404, "not_found", "Connect session not found.")
        return row

    async def live(self, db, row):
        p = Principal(row["app_id"], row["tenant_id"], row["user_id"], key_id=row["issuing_key_id"])
        await self.service.validate_principal(db, p)
        integration = self.service.integrations.get(p.app, row["integration_id"])
        if (
            not integration
            or not integration.enabled
            or not integration.oauth
            or self.fingerprint(integration) != row["integration_fingerprint"]
            or row["return_url"] not in self.service.applications.get(p.app).return_urls
        ):
            raise RavnError(409, "connection_disabled", "OAuth configuration changed; start again.")
        if row["expires_at"] <= now() or (
            row["completion_expires_at"] and row["completion_expires_at"] <= now()
        ):
            raise RavnError(410, "connect_session_expired", "Connect session expired; start again.")
        if row["reconnect_id"]:
            conn, _ = await self.service.authorize(db, p, row["reconnect_id"])
            if conn["epoch"] != row["reconnect_epoch"] or conn["status"] == "disconnected":
                raise RavnError(409, "connection_disabled", "Connection changed; start again.")
        return p, integration

    async def terminal(self, db, p, row, status, code=None):
        await db.execute(
            f"UPDATE connect_sessions SET status=?,failure_code=?,{WIPE},updated_at=? WHERE id=?",
            (status, code, now(), row["id"]),
        )
        await event(db, p, "connect_session." + status, row["id"])

    async def start(self, p, integration_id, return_url, app_state, reconnect_id=None):
        await self.cleanup()
        integration = self.service.integrations.get(p.app, integration_id)
        if not integration or not integration.enabled or not integration.oauth:
            raise RavnError(
                409, "integration_not_ready", "OAuth is not configured for this integration."
            )
        sid, ticket, created, expires = (
            identifier("cs"),
            secrets.token_urlsafe(32),
            now(),
            deadline(600),
        )
        async with self.store.transaction() as db:
            await self.service.validate_principal(db, p)
            if return_url not in self.service.applications.get(p.app).return_urls:
                raise RavnError(
                    400, "invalid_return_url", "Return URL must exactly match application settings."
                )
            if not self.service.integrations.get(p.app, integration_id).enabled:
                raise RavnError(409, "connection_disabled", "Integration is disabled.")
            count = await one(
                db,
                "SELECT count(*) AS n FROM connect_sessions WHERE status IN ('pending','authorizing','exchanging','awaiting_completion')",
            )
            actor_count = await one(
                db,
                "SELECT count(*) AS n FROM connect_sessions WHERE app_id=? AND tenant_id=? AND user_id=? AND status IN ('pending','authorizing','exchanging','awaiting_completion')",
                p.namespace,
            )
            if count["n"] >= 256 or actor_count["n"] >= 8:
                raise RavnError(
                    429,
                    "rate_limited",
                    "Too many pending authorizations; cancel or wait for expiry.",
                )
            epoch = None
            if reconnect_id:
                conn, _ = await self.service.authorize(db, p, reconnect_id)
                if not integration.identity or not conn["provider_account_id"]:
                    raise RavnError(
                        409,
                        "new_connection_required",
                        "Account identity is unavailable. Connect again without reconnect_connection_id, then attach the new connection to the sessions that should use it.",
                    )
                if conn["integration_id"] != integration_id or conn["status"] == "disconnected":
                    raise RavnError(409, "connection_disabled", "Connection cannot be reconnected.")
                epoch = conn["epoch"]
            await db.execute(
                "INSERT INTO connect_sessions(id,app_id,tenant_id,user_id,issuing_key_id,integration_id,integration_fingerprint,return_url,app_state,reconnect_id,reconnect_epoch,status,ticket_hash,created_at,updated_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?)",
                (
                    sid,
                    *p.namespace,
                    p.key_id,
                    integration_id,
                    self.fingerprint(integration),
                    return_url,
                    app_state,
                    reconnect_id,
                    epoch,
                    digest(ticket),
                    created,
                    created,
                    expires,
                ),
            )
            await event(db, p, "connect_session.created", sid)
        return {
            "id": sid,
            "status": "pending",
            "authorization_url": self.config.server.public_url.rstrip("/")
            + "/oauth/start/"
            + ticket,
            "expires_at": expires,
        }

    async def status(self, p, sid):
        await self.cleanup()
        async with self.store.transaction() as db:
            return public_flow(await self.row(db, p, sid))

    async def cancel(self, p, sid):
        await self.cleanup()
        async with self.store.transaction() as db:
            row = await self.row(db, p, sid)
            if row["status"] in LIVE:
                await self.terminal(db, p, row, "cancelled")
            return public_flow(await self.row(db, p, sid))

    async def browser_start(self, ticket):
        await self.cleanup()
        state, cookie, verifier = [secrets.token_urlsafe(32) for _ in range(3)]
        async with self.store.transaction() as db:
            row = await one(
                db,
                "SELECT * FROM connect_sessions WHERE ticket_hash=? AND status='pending'",
                (digest(ticket),),
            )
            if row is None:
                raise RavnError(
                    400,
                    "invalid_oauth_transaction",
                    "Authorization link is invalid, used, or expired.",
                )
            p, integration = await self.live(db, row)
            await db.execute(
                "UPDATE connect_sessions SET status='authorizing',ticket_hash=NULL,state_hash=?,cookie_hash=?,verifier=?,updated_at=? WHERE id=?",
                (
                    digest(state),
                    digest(cookie),
                    self.cipher.encrypt(verifier, p.app, p.tenant, row["id"]),
                    now(),
                    row["id"],
                ),
            )
        return row["id"], cookie, self.provider(integration).authorize_url(state, verifier)

    def returned(self, row, **params):
        return (
            row["return_url"]
            + "?"
            + urlencode({"session_id": row["id"], "app_state": row["app_state"], **params})
        )

    async def callback(self, app_id, integration_id, query, cookies):
        await self.cleanup()
        state = query.get("state", "")
        async with self.store.transaction() as db:
            row = await one(
                db,
                "SELECT * FROM connect_sessions WHERE state_hash=? AND app_id=? AND integration_id=? AND status='authorizing'",
                (digest(state), app_id, integration_id),
            )
            if row is None or not hmac.compare_digest(
                row["cookie_hash"], digest(cookies.get(self.cookie_name(row["id"]), ""))
            ):
                raise RavnError(
                    400,
                    "invalid_oauth_transaction",
                    "OAuth browser binding is invalid or already consumed.",
                )
            p, integration = await self.live(db, row)
            if query.get("iss") and query["iss"] != integration.oauth.issuer:
                raise RavnError(400, "invalid_oauth_transaction", "Unexpected OAuth issuer.")
            if "error" in query:
                await self.terminal(db, p, row, "denied", "authorization_denied")
                return row["id"], self.returned(row, status="denied")
            code = query.get("code", "")
            if not code or len(code) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in code):
                raise RavnError(
                    400, "invalid_oauth_transaction", "Missing or invalid authorization code."
                )
            verifier = self.cipher.decrypt(row["verifier"], p.app, p.tenant, row["id"])
            await db.execute(
                "UPDATE connect_sessions SET status='exchanging',state_hash=NULL,cookie_hash=NULL,verifier=NULL,updated_at=? WHERE id=?",
                (now(), row["id"]),
            )
        phase = "token_exchange"
        try:
            async with self.service.slots(p, "oauth"):
                bundle = await self.provider(integration).exchange(code, verifier)
                phase = "account_verification"
                account, display = await self.service.provider_for(integration).identify(
                    bundle["access_token"]
                )
            bundle.update(provider_account_id=account, display_name=display)
            completion = secrets.token_urlsafe(32)
            async with self.store.transaction() as db:
                current = await self.row(db, p, row["id"])
                await self.live(db, current)
                if current["status"] != "exchanging":
                    raise RavnError(
                        409, "connect_session_not_pending", "Authorization is no longer pending."
                    )
                if row["reconnect_id"]:
                    conn, _ = await self.service.authorize(db, p, row["reconnect_id"])
                    if conn["provider_account_id"] != account:
                        raise RavnError(
                            409,
                            "provider_account_mismatch",
                            "Reconnect must use the same provider account and workspace.",
                        )
                await db.execute(
                    "UPDATE connect_sessions SET status='awaiting_completion',staged=?,completion_hash=?,completion_expires_at=?,updated_at=? WHERE id=?",
                    (
                        self.cipher.encrypt(canonical(bundle).decode(), p.app, p.tenant, row["id"]),
                        digest(completion),
                        min(deadline(300), row["expires_at"]),
                        now(),
                        row["id"],
                    ),
                )
                await event(db, p, "connect_session.awaiting_completion", row["id"])
            return row["id"], self.returned(row, completion_code=completion)
        except BaseException as exc:
            code = exc.code if isinstance(exc, RavnError) else "oauth_exchange_failed"
            # Our errors contain fixed messages; never log upstream exceptions or payloads.
            logger.warning(
                "OAuth connection failed: session=%s phase=%s code=%s reason=%s",
                row["id"],
                phase,
                code,
                exc.message if isinstance(exc, RavnError) else type(exc).__name__,
            )

            async def fail():
                async with self.store.transaction() as db:
                    current = await one(
                        db, "SELECT * FROM connect_sessions WHERE id=?", (row["id"],)
                    )
                    if current and current["status"] == "exchanging":
                        await self.terminal(db, p, current, "failed", code)

            await finish(fail())
            if isinstance(exc, asyncio.CancelledError):
                raise
            return row["id"], self.returned(row, status="failed")

    def cookie_name(self, sid):
        return (
            ("__Host-" if self.config.server.public_url.startswith("https://") else "")
            + "ravn_oauth_"
            + sid
        )

    async def complete(self, p, sid, completion, label=None):
        await self.cleanup()
        async with self.store.transaction() as db:
            row = await self.row(db, p, sid)
            await self.live(db, row)
            if row["status"] != "awaiting_completion":
                raise RavnError(
                    409,
                    "connect_session_not_pending",
                    "Connect session is not awaiting completion; check its status.",
                )
            if not hmac.compare_digest(row["completion_hash"], digest(completion)):
                raise RavnError(400, "completion_code_invalid", "Invalid completion code.")
            bundle = json.loads(self.cipher.decrypt(row["staged"], p.app, p.tenant, sid))
            cid = row["reconnect_id"] or identifier("conn")
            ciphertext = self.cipher.encrypt(canonical(bundle).decode(), p.app, p.tenant, cid)
            if row["reconnect_id"]:
                conn, _ = await self.service.authorize(db, p, cid)
                if conn["provider_account_id"] != bundle["provider_account_id"]:
                    raise RavnError(409, "provider_account_mismatch", "Provider account changed.")
                await db.execute(
                    "UPDATE connections SET ciphertext=?,credential_type='oauth2',credential_version=credential_version+1,refresh_attempt=NULL,status='active',epoch=epoch+1,revision=revision+1,updated_at=? WHERE app_id=? AND tenant_id=? AND id=?",
                    (ciphertext, now(), p.app, p.tenant, cid),
                )
            else:
                await db.execute(
                    "INSERT INTO connections(id,app_id,tenant_id,user_id,integration_id,provider_account_id,display_name,label,status,ciphertext,key_id,created_at,updated_at,credential_type) VALUES(?,?,?,?,?,?,?,?,'active',?,?,?,?,'oauth2')",
                    (
                        cid,
                        *p.namespace,
                        row["integration_id"],
                        bundle["provider_account_id"],
                        bundle["display_name"],
                        label,
                        ciphertext,
                        self.cipher.key_id,
                        now(),
                        now(),
                    ),
                )
            await self.terminal(db, p, row, "completed")
            await db.execute("UPDATE connect_sessions SET connection_id=? WHERE id=?", (cid, sid))
            await event(
                db,
                p,
                "connection.reconnected" if row["reconnect_id"] else "connection.connected",
                cid,
            )
            conn, _ = await self.service.authorize(db, p, cid)
            from ravn.service import public_connection

            return public_connection(conn)

    @asynccontextmanager
    async def refresh_lock(self, key):
        entry = self.refresh_locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                del self.refresh_locks[key]

    async def credential(self, p, cid):
        async with self.refresh_lock((p.app, p.tenant, cid)):
            async with self.store.transaction() as db:
                conn, integration = await self.service.authorize(db, p, cid, execute=True)
                raw = self.cipher.decrypt(conn["ciphertext"], p.app, p.tenant, cid)
                if conn["credential_type"] == "bearer":
                    return raw, integration
                bundle = json.loads(raw)
                if not bundle["expires_at"] or bundle["expires_at"] > deadline(60):
                    return bundle["access_token"], integration
                attempt = identifier("refresh")
                await db.execute(
                    "UPDATE connections SET refresh_attempt=? WHERE app_id=? AND tenant_id=? AND id=?",
                    (attempt, p.app, p.tenant, cid),
                )
            try:
                if (
                    not integration.oauth
                    or not bundle["refresh_token"]
                    or (bundle["refresh_expires_at"] and bundle["refresh_expires_at"] <= now())
                ):
                    raise RavnError(
                        409,
                        "connection_reauth_required",
                        "Credential expired; reconnect this account.",
                    )
                replacement = await self.provider(integration).refresh(bundle)
                # Refresh remains bound to the existing grant. No extra MCP call is needed
                # when there is no optional identity verifier.
                account, display = conn["provider_account_id"], conn["display_name"]
                if integration.identity:
                    account, display = await self.service.provider_for(integration).identify(
                        replacement["access_token"]
                    )
                if account != conn["provider_account_id"]:
                    raise RavnError(
                        409,
                        "provider_account_mismatch",
                        "Refreshed credential changed provider identity.",
                    )
                replacement.update(provider_account_id=account, display_name=display)
                async with self.store.transaction() as db:
                    # Preserve rotated credentials for every session sharing this grant,
                    # even if this caller lost access while the provider was refreshing.
                    current = await one(
                        db,
                        "SELECT * FROM connections WHERE app_id=? AND tenant_id=? AND user_id=? AND id=?",
                        (*p.namespace, cid),
                    )
                    if (
                        current is None
                        or current["status"] != "active"
                        or current["epoch"] != conn["epoch"]
                        or current["credential_version"] != conn["credential_version"]
                        or current["refresh_attempt"] != attempt
                    ):
                        raise RavnError(
                            409,
                            "connection_changed",
                            "Connection changed during refresh; request a new session.",
                        )
                    await db.execute(
                        "UPDATE connections SET ciphertext=?,credential_version=credential_version+1,refresh_attempt=NULL,updated_at=? WHERE app_id=? AND tenant_id=? AND id=?",
                        (
                            self.cipher.encrypt(
                                canonical(replacement).decode(), p.app, p.tenant, cid
                            ),
                            now(),
                            p.app,
                            p.tenant,
                            cid,
                        ),
                    )
                    await event(db, p, "credential.refreshed", cid)
            except BaseException as exc:

                async def fail():
                    async with self.store.transaction() as db:
                        result = await db.execute(
                            "UPDATE connections SET status='reconnect_required',refresh_attempt=NULL,revision=revision+1,updated_at=? WHERE app_id=? AND tenant_id=? AND id=? AND status='active' AND epoch=? AND credential_version=? AND refresh_attempt=?",
                            (
                                now(),
                                p.app,
                                p.tenant,
                                cid,
                                conn["epoch"],
                                conn["credential_version"],
                                attempt,
                            ),
                        )
                        if result.rowcount:
                            await event(db, p, "credential.reconnect_required", cid)

                await finish(fail())
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise RavnError(
                    409,
                    "connection_reauth_required",
                    "Credential refresh could not be safely completed; reconnect this account.",
                ) from None
            async with self.store.transaction() as db:
                await self.service.authorize(db, p, cid, execute=True)
            return replacement["access_token"], integration

    async def rejected(self, p, cid, credential):
        # A late 401 for an old credential must not disable a successful refresh
        # or reconnect. Compare the exact credential currently stored.
        async with self.store.transaction() as db:
            conn = await one(
                db,
                "SELECT * FROM connections WHERE app_id=? AND tenant_id=? AND id=? AND status='active'",
                (p.app, p.tenant, cid),
            )
            # An in-flight refresh owns the outcome for this old credential.
            if not conn or conn["refresh_attempt"]:
                return
            raw = self.cipher.decrypt(conn["ciphertext"], p.app, p.tenant, cid)
            current = (
                json.loads(raw)["access_token"] if conn["credential_type"] == "oauth2" else raw
            )
            if hmac.compare_digest(current, credential):
                await db.execute(
                    "UPDATE connections SET status='reconnect_required',revision=revision+1,updated_at=? WHERE app_id=? AND tenant_id=? AND id=?",
                    (now(), p.app, p.tenant, cid),
                )
                await event(db, p, "credential.rejected", cid)
