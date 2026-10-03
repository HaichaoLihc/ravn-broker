"""Agent-facing OAuth 2.1 authorization server: hashes only, single-use codes, rotating refresh tokens.

An access token is an ordinary RAVN session bound to a grant, so the /mcp
boundary, ceilings, rules and approvals apply to it unchanged. Protocol
validation (PKCE, redirect binding, error redirects, DCR) comes from the MCP SDK.
"""

import asyncio
import contextvars
import json
import re
import time
from collections import deque
from datetime import UTC, datetime
from urllib.parse import urlsplit

from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.metadata import MetadataHandler, ProtectedResourceMetadataHandler
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata, build_resource_metadata_url
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import (
    InvalidRedirectUriError,
    InvalidScopeError,
    OAuthClientInformationFull,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from pydantic import AnyHttpUrl, AnyUrl, BaseModel, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ravn import control
from ravn.common import Principal, RavnError, digest, identifier, now, token
from ravn.oauth_provider import deadline
from ravn.permissions import session_rules, set_session_rules
from ravn.store import event, finish, one, rows

CODE_TTL_SECONDS = 60
REQUEST_TTL_SECONDS = 600
MAX_PENDING_REQUESTS = 64
REGISTRATIONS_PER_MINUTE = 10
CODE = re.compile(r"rv_code_[a-f0-9]{32}_[A-Za-z0-9_-]{43}")
REFRESH = re.compile(r"rv_rt_[a-f0-9]{32}_[A-Za-z0-9_-]{43}")
SESSION = re.compile(r"rv_sess_[a-f0-9]{32}_[A-Za-z0-9_-]{43}")
CLIENT_ID = re.compile(r"[0-9a-f-]{36}")
CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")
SCOPE_TOKEN = re.compile(r"[\x21\x23-\x5b\x5d-\x7e]{1,64}")
REQUEST_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ravn_oauth_request_id", default=None
)
# Connection-state refusals end a grant's ability to issue; anything else is transient.
GRANT_ENDING = {401, 403, 404, 409}


def issuer(config) -> str:
    return config.server.public_url.rstrip("/")


def resource_url(config) -> str:
    return issuer(config) + "/mcp"


def prm_url(config) -> str:
    return str(build_resource_metadata_url(AnyHttpUrl(resource_url(config))))


def oauth_error(status: int, code: str, description: str) -> JSONResponse:
    return JSONResponse(
        {"error": code, "error_description": description},
        status_code=status,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def loopback(uri: str) -> bool:
    """An RFC 8252 loopback redirect: plain http to this machine, on an explicit port."""
    if len(uri) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in uri) or "#" in uri:
        return False
    try:
        url = urlsplit(uri)
        port = url.port
    except ValueError:
        return False
    return (
        url.scheme == "http"
        and url.hostname in {"localhost", "127.0.0.1", "::1"}
        and port is not None
        and port > 0
        and "@" not in url.netloc
        and url.path.startswith("/")
    )


def scope_list(value: str | None) -> list[str]:
    return value.split(" ") if value else []


class RavnClient(OAuthClientInformationFull):
    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        # Explicit only, and only the port may differ from a registered loopback URI
        # (RFC 8252 §7.3): native clients pick a free port per sign-in.
        if redirect_uri is None or not loopback(str(redirect_uri)):
            raise InvalidRedirectUriError("A registered loopback redirect_uri is required.")
        given = urlsplit(str(redirect_uri))
        for registered in self.redirect_uris or []:
            known = urlsplit(str(registered))
            if (known.scheme, known.hostname, known.path, known.query) == (
                given.scheme,
                given.hostname,
                given.path,
                given.query,
            ):
                return redirect_uri
        raise InvalidRedirectUriError("A registered loopback redirect_uri is required.")

    def validate_scope(self, requested_scope: str | None) -> list[str]:
        # Scopes carry no authority here; tools are governed by the ceiling and rules.
        if not requested_scope:
            return []
        values = requested_scope.split(" ")
        if len(values) > 8 or not all(SCOPE_TOKEN.fullmatch(v) for v in values):
            raise InvalidScopeError("Unsupported scope.")
        return values


class RavnCode(AuthorizationCode):
    code_id: str


class RavnRefresh(RefreshToken):
    grant_id: str


class RavnAccess(AccessToken):
    session_id: str
    app_id: str
    tenant_id: str


def target_of(row):
    return control.AuditTarget(row["app_id"], row["tenant_id"], row["user_id"])


def client_actor(client_id):
    return {"actor_kind": "oauth_client", "actor_id": client_id, "request_id": REQUEST_ID.get()}


def public_grant(row):
    return {
        k: row[k]
        for k in (
            "id",
            "client_id",
            "client_name",
            "app_id",
            "tenant_id",
            "user_id",
            "connection_id",
            "scope",
            "rotation",
            "created_at",
            "expires_at",
            "refreshed_at",
            "revoked_at",
            "revoke_reason",
            "active_sessions",
        )
    }


GRANT_SQL = (
    "SELECT g.*, c.name AS client_name, (SELECT count(*) FROM sessions s WHERE s.grant_id=g.id "
    "AND s.revoked_at IS NULL AND s.expires_at>?) AS active_sessions FROM oauth_grants g "
    "LEFT JOIN oauth_clients c ON c.id=g.client_id"
)


class McpOAuth:
    def __init__(self, service):
        self.service = service
        self.config = service.config
        self.store = service.store

    # Clients ---------------------------------------------------------------

    async def get_client(self, client_id: str) -> RavnClient | None:
        if not isinstance(client_id, str) or not CLIENT_ID.fullmatch(client_id):
            return None
        async with self.store.transaction() as db:
            row = await one(db, "SELECT metadata FROM oauth_clients WHERE id=?", (client_id,))
        return RavnClient.model_validate_json(row["metadata"]) if row else None

    async def register_client(self, info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in info.redirect_uris or []]
        if not uris or len(uris) > 8 or not all(loopback(u) for u in uris):
            raise RegistrationError(
                "invalid_redirect_uri", "Only http loopback redirect URIs with a port are accepted."
            )
        if info.client_name is not None and len(info.client_name) > 128:
            raise RegistrationError("invalid_client_metadata", "client_name is too long.")
        # Public clients only (RFC 7591 lets the server override): no recoverable secrets.
        info.token_endpoint_auth_method = "none"
        info.client_secret = None
        info.client_secret_expires_at = None
        info.grant_types = ["authorization_code", "refresh_token"]
        metadata = info.model_dump_json(exclude_none=True)
        if len(metadata) > 16384:
            raise RegistrationError("invalid_client_metadata", "Client metadata is too large.")
        # SDK errors are frozen dataclasses, which contextlib cannot re-raise through a
        # context manager, so decide inside and raise outside the transaction.
        full = False
        async with self.store.transaction() as db:
            # Only clients still in use hold a slot, so ended grants cannot fill the cap for good.
            stamp = now()
            count = await one(
                db,
                "SELECT count(*) AS n FROM oauth_clients c WHERE c.created_at>? OR EXISTS("
                "SELECT 1 FROM oauth_grants g WHERE g.client_id=c.id AND g.revoked_at IS NULL "
                "AND g.expires_at>?)",
                (deadline(-86400), stamp),
            )
            full = count["n"] >= self.config.mcp_oauth.max_clients
            if not full:
                await db.execute(
                    "INSERT INTO oauth_clients VALUES(?,?,?,?)",
                    (info.client_id, info.client_name, metadata, now()),
                )
                await event(
                    db,
                    control.AuditTarget(None),
                    "oauth.client_registered",
                    info.client_id,
                    **client_actor(info.client_id),
                )
        if full:
            raise RegistrationError(
                "invalid_client_metadata",
                "Too many registered agent clients; revoke unused grants first.",
            )

    # Authorization and consent ---------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if self.service.consent_url is None:
            raise AuthorizeError(
                "temporarily_unavailable", "Consent needs the console: start ravn serve --console."
            )
        if params.resource is not None and params.resource != resource_url(self.config):
            raise AuthorizeError("invalid_target", "Unknown resource.")
        if not CHALLENGE.fullmatch(params.code_challenge):
            raise AuthorizeError("invalid_request", "Malformed code_challenge.")
        if params.state is not None and len(params.state) > 1024:
            raise AuthorizeError("invalid_request", "state is too long.")
        rid, stamp = identifier("oreq"), now()
        async with self.store.transaction() as db:
            pending = await one(
                db,
                "SELECT count(*) AS n FROM oauth_requests WHERE status='pending' AND expires_at>?",
                (stamp,),
            )
            busy = pending["n"] >= MAX_PENDING_REQUESTS
            if busy:
                rid = None
            else:
                await db.execute(
                    "INSERT INTO oauth_requests(id,client_id,redirect_uri,code_challenge,state,scope,"
                    "resource,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,'pending',?,?)",
                    (
                        rid,
                        client.client_id,
                        str(params.redirect_uri),
                        params.code_challenge,
                        params.state,
                        " ".join(params.scopes or []),
                        resource_url(self.config),
                        stamp,
                        deadline(REQUEST_TTL_SECONDS),
                    ),
                )
        if rid is None:
            raise AuthorizeError("temporarily_unavailable", "Too many pending sign-ins.")
        return self.service.consent_url + "?request=" + rid

    async def consent_choices(self, db) -> list[dict]:
        """Connections a consenting operator may hand to an agent.

        The console operator is deployment-wide; app-delegated consent would plug in here.
        """
        result = []
        for conn in await rows(
            db,
            "SELECT * FROM connections WHERE status='active' ORDER BY app_id,tenant_id,created_at,id",
        ):
            integration = self.config.integration(conn["app_id"], conn["integration_id"])
            if (
                self.config.app(conn["app_id"]) is None
                or integration is None
                or not integration.enabled
                or not integration.schema_hashes
            ):
                continue
            result.append(
                {
                    "key": f"{conn['app_id']}/{conn['tenant_id']}/{conn['id']}",
                    **{
                        k: conn[k]
                        for k in (
                            "app_id",
                            "tenant_id",
                            "user_id",
                            "id",
                            "integration_id",
                            "display_name",
                            "label",
                        )
                    },
                }
            )
        return result

    @staticmethod
    def gone():
        return RavnError(
            409,
            "consent_unavailable",
            "This sign-in expired or was already decided. Restart it from your agent.",
        )

    async def pending_request(self, db, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(r"oreq_[0-9a-f]{32}", request_id):
            raise self.gone()
        row = await one(
            db,
            "SELECT r.*, c.name AS client_name FROM oauth_requests r "
            "JOIN oauth_clients c ON c.id=r.client_id WHERE r.id=?",
            (request_id,),
        )
        if row is None or row["status"] != "pending" or row["expires_at"] <= now():
            raise self.gone()
        return row

    async def pending_view(self, operator, request_id) -> dict:
        async with self.store.transaction() as db:
            row = await self.pending_request(db, request_id)
            choices = await self.consent_choices(db)
        redirect = urlsplit(row["redirect_uri"])
        return {
            "request_id": row["id"],
            "client_id": row["client_id"],
            "client_name": row["client_name"],
            "redirect": f"{redirect.hostname}:{redirect.port}",
            "scope": row["scope"],
            "expires_at": row["expires_at"],
            "connections": choices,
        }

    async def decide(self, operator, request_id, decision, connection_key, audit_request_id) -> str:
        audit = {
            "actor_kind": "local_operator",
            "actor_id": operator.id,
            "request_id": audit_request_id,
        }
        if decision not in {"approve", "deny"}:
            raise RavnError(400, "invalid_request", "Choose approve or deny.")
        async with self.store.transaction() as db:
            row = await self.pending_request(db, request_id)
            stamp = now()
            if decision == "deny":
                await self.close_request(db, row, "denied", operator, stamp)
                await event(
                    db, control.AuditTarget(None), "oauth.consent_denied", row["id"], **audit
                )
                return construct_redirect_uri(
                    row["redirect_uri"], error="access_denied", state=row["state"]
                )
            choice = next(
                (c for c in await self.consent_choices(db) if c["key"] == connection_key), None
            )
            if choice is None:
                raise RavnError(
                    409,
                    "connection_unavailable",
                    "That connection cannot be given to an agent. Choose another.",
                )
            code = token("code")
            await db.execute(
                "INSERT INTO oauth_codes(id,code_hash,request_id,client_id,app_id,tenant_id,user_id,"
                "connection_id,redirect_uri,code_challenge,scope,resource,status,created_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'issued',?,?)",
                (
                    identifier("ocode"),
                    digest(code),
                    row["id"],
                    row["client_id"],
                    choice["app_id"],
                    choice["tenant_id"],
                    choice["user_id"],
                    choice["id"],
                    row["redirect_uri"],
                    row["code_challenge"],
                    row["scope"],
                    row["resource"],
                    stamp,
                    deadline(CODE_TTL_SECONDS),
                ),
            )
            await self.close_request(db, row, "approved", operator, stamp)
            await event(db, target_of(choice), "oauth.consent_approved", row["id"], **audit)
            return construct_redirect_uri(row["redirect_uri"], code=code, state=row["state"])

    async def close_request(self, db, row, status, operator, stamp):
        cursor = await db.execute(
            "UPDATE oauth_requests SET status=?,operator_id=?,decided_at=? "
            "WHERE id=? AND status='pending'",
            (status, operator.id, stamp, row["id"]),
        )
        if not cursor.rowcount:
            raise self.gone()

    # Codes -----------------------------------------------------------------

    async def load_authorization_code(self, client, code: str) -> RavnCode | None:
        if not isinstance(code, str) or not CODE.fullmatch(code):
            return None
        async with self.store.transaction() as db:
            row = await one(db, "SELECT * FROM oauth_codes WHERE code_hash=?", (digest(code),))
            if row is None or row["client_id"] != client.client_id:
                return None
            if row["status"] in {"redeeming", "redeemed"}:
                await self.code_reused(db, row)
                return None
            if row["status"] != "issued":
                return None
        return RavnCode(
            code=code,
            code_id=row["id"],
            scopes=scope_list(row["scope"]),
            expires_at=epoch(row["expires_at"]),
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=AnyUrl(row["redirect_uri"]),
            redirect_uri_provided_explicitly=True,
            resource=row["resource"],
        )

    async def code_reused(self, db, row):
        # OAuth 2.1 §4.1.3: a replayed code revokes what it already issued.
        await db.execute("UPDATE oauth_codes SET status='reused' WHERE id=?", (row["id"],))
        if row["grant_id"]:
            await control.revoke_grant(
                db,
                target_of(row),
                row["grant_id"],
                reason="code_reuse",
                **client_actor(row["client_id"]),
            )

    async def exchange_authorization_code(self, client, code: RavnCode) -> OAuthToken:
        actor = client_actor(client.client_id)
        replayed = False
        async with self.store.transaction() as db:
            claimed = await db.execute(
                "UPDATE oauth_codes SET status='redeeming' WHERE id=? AND status='issued' "
                "AND expires_at>?",
                (code.code_id, now()),
            )
            row = await one(db, "SELECT * FROM oauth_codes WHERE id=?", (code.code_id,))
            if not claimed.rowcount:
                if row and row["status"] in {"redeeming", "redeemed"}:
                    await self.code_reused(db, row)
                replayed = True
            else:
                gid, stamp = identifier("grant"), now()
                await db.execute(
                    "INSERT INTO oauth_grants(id,client_id,app_id,tenant_id,user_id,connection_id,"
                    "scope,resource,rotation,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,0,?,?)",
                    (
                        gid,
                        client.client_id,
                        row["app_id"],
                        row["tenant_id"],
                        row["user_id"],
                        row["connection_id"],
                        row["scope"],
                        row["resource"],
                        stamp,
                        deadline(self.config.mcp_oauth.refresh_ttl_seconds),
                    ),
                )
                await db.execute(
                    "UPDATE oauth_codes SET grant_id=? WHERE id=?", (gid, code.code_id)
                )
        if replayed:
            raise TokenError("invalid_grant", "Authorization code is invalid or already used.")
        p = Principal(row["app_id"], row["tenant_id"], row["user_id"], grant_id=gid)
        cid = row["connection_id"]
        ttl = self.config.sessions.default_ttl_seconds
        try:
            tools = await self.service.session_tools(p, cid)
            replayed = False
            async with self.store.transaction() as db:
                done = await db.execute(
                    "UPDATE oauth_codes SET status='redeemed' WHERE id=? AND status='redeeming'",
                    (code.code_id,),
                )
                if not done.rowcount:
                    await control.revoke_grant(
                        db, target_of(row), gid, reason="code_reuse", **actor
                    )
                    replayed = True
                else:
                    session = await self.service.insert_session(db, p, cid, tools, ttl, audit=actor)
                    refresh = token("rt")
                    await db.execute(
                        "INSERT INTO oauth_refresh_tokens VALUES(?,?,0,?,NULL)",
                        (digest(refresh), gid, now()),
                    )
                    await event(db, p, "oauth.token_issued", gid, **actor)
        except BaseException as error:
            await finish(self.abandon(row, gid, actor))
            if isinstance(error, RavnError) and error.status in GRANT_ENDING:
                raise TokenError(
                    "invalid_grant", "The chosen connection cannot issue sessions."
                ) from None
            raise
        if replayed:
            raise TokenError("invalid_grant", "Authorization code is invalid or already used.")
        return OAuthToken(
            access_token=session["token"],
            expires_in=ttl,
            refresh_token=refresh,
            scope=row["scope"] or None,
        )

    async def abandon(self, row, gid, actor):
        async with self.store.transaction() as db:
            await control.revoke_grant(db, target_of(row), gid, reason="issue_failed", **actor)
            await db.execute(
                "UPDATE oauth_codes SET status='failed' WHERE grant_id=? AND status='redeeming'",
                (gid,),
            )

    # Refresh tokens --------------------------------------------------------

    async def load_refresh_token(self, client, value: str) -> RavnRefresh | None:
        if not isinstance(value, str) or not REFRESH.fullmatch(value):
            return None
        async with self.store.transaction() as db:
            row = await one(
                db,
                "SELECT t.rotated_at, g.* FROM oauth_refresh_tokens t "
                "JOIN oauth_grants g ON g.id=t.grant_id WHERE t.token_hash=?",
                (digest(value),),
            )
            if row is None or row["client_id"] != client.client_id:
                return None
            if row["rotated_at"] is not None:
                if row["revoked_at"] is None:
                    await self.refresh_reused(db, row)
                return None
            if row["revoked_at"] is not None or row["expires_at"] <= now():
                return None
        return RavnRefresh(
            token=value,
            grant_id=row["id"],
            client_id=row["client_id"],
            scopes=scope_list(row["scope"]),
            expires_at=int(epoch(row["expires_at"])),
            resource=row["resource"],
        )

    async def refresh_reused(self, db, grant):
        # RFC 9700 §4.14.2: a rotated token coming back means one copy leaked.
        actor = client_actor(grant["client_id"])
        await event(db, target_of(grant), "oauth.refresh_reuse_detected", grant["id"], **actor)
        await control.revoke_grant(
            db, target_of(grant), grant["id"], reason="refresh_reuse", **actor
        )

    async def exchange_refresh_token(self, client, refresh: RavnRefresh, scopes) -> OAuthToken:
        actor = client_actor(client.client_id)
        async with self.store.transaction() as db:
            grant = await one(db, "SELECT * FROM oauth_grants WHERE id=?", (refresh.grant_id,))
        p = Principal(grant["app_id"], grant["tenant_id"], grant["user_id"], grant_id=grant["id"])
        cid = grant["connection_id"]
        ended = TokenError("invalid_grant", "The grant can no longer issue sessions.")
        # The grant's lifetime is absolute: no session outlives it.
        left = (datetime.fromisoformat(grant["expires_at"]) - datetime.now(UTC)).total_seconds()
        ttl = min(self.config.sessions.default_ttl_seconds, int(left))
        if ttl < 60:
            raise ended
        reused = False
        try:
            # Upstream I/O happens before rotation, so a transient failure keeps the token valid.
            tools = await self.service.session_tools(p, cid)
            async with self.store.transaction() as db:
                rotated = await db.execute(
                    "UPDATE oauth_refresh_tokens SET rotated_at=? "
                    "WHERE token_hash=? AND rotated_at IS NULL",
                    (now(), digest(refresh.token)),
                )
                if not rotated.rowcount:
                    current = await one(db, "SELECT * FROM oauth_grants WHERE id=?", (grant["id"],))
                    if current["revoked_at"] is None:
                        await self.refresh_reused(db, current)
                    reused = True
                else:
                    await self.service.validate_principal(db, p)
                    previous = await one(
                        db,
                        "SELECT id FROM sessions WHERE grant_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                        (grant["id"],),
                    )
                    session = await self.service.insert_session(db, p, cid, tools, ttl, audit=actor)
                    if previous:
                        # Operator narrowing survives a refresh; it never widens the new ceiling.
                        ceiling = json.loads(
                            (
                                await one(
                                    db,
                                    "SELECT tools FROM sessions WHERE app_id=? AND tenant_id=? AND id=?",
                                    (p.app, p.tenant, session["id"]),
                                )
                            )["tools"]
                        )
                        rules = await session_rules(db, p.app, p.tenant, previous["id"])
                        kept = {k: v for k, v in rules.items() if k in ceiling}
                        if kept:
                            await set_session_rules(db, p.app, p.tenant, session["id"], kept)
                    replacement = token("rt")
                    old = await one(
                        db,
                        "SELECT generation FROM oauth_refresh_tokens WHERE token_hash=?",
                        (digest(refresh.token),),
                    )
                    await db.execute(
                        "INSERT INTO oauth_refresh_tokens VALUES(?,?,?,?,NULL)",
                        (digest(replacement), grant["id"], old["generation"] + 1, now()),
                    )
                    await db.execute(
                        "UPDATE oauth_grants SET rotation=rotation+1,refreshed_at=? WHERE id=?",
                        (now(), grant["id"]),
                    )
                    await event(db, p, "oauth.token_refreshed", grant["id"], **actor)
        except RavnError as error:
            if error.status in GRANT_ENDING:
                raise ended from None
            raise
        if reused:
            raise ended
        return OAuthToken(
            access_token=session["token"],
            expires_in=ttl,
            refresh_token=replacement,
            scope=" ".join(scopes) or None,
        )

    # Access tokens and revocation -----------------------------------------

    async def load_access_token(self, value: str) -> RavnAccess | None:
        if not isinstance(value, str) or not SESSION.fullmatch(value):
            return None
        async with self.store.transaction() as db:
            row = await one(
                db,
                "SELECT s.id, s.app_id, s.tenant_id, s.expires_at, s.revoked_at, g.client_id, "
                "g.scope, g.resource FROM sessions s JOIN oauth_grants g ON g.id=s.grant_id "
                "WHERE s.token_hash=?",
                (digest(value),),
            )
        if row is None or row["revoked_at"] or row["expires_at"] <= now():
            return None
        return RavnAccess(
            token=value,
            client_id=row["client_id"],
            scopes=scope_list(row["scope"]),
            expires_at=int(epoch(row["expires_at"])),
            resource=row["resource"],
            session_id=row["id"],
            app_id=row["app_id"],
            tenant_id=row["tenant_id"],
        )

    async def revoke_token(self, tok) -> None:
        actor = client_actor(tok.client_id)
        async with self.store.transaction() as db:
            if isinstance(tok, RavnRefresh):
                grant = await one(db, "SELECT * FROM oauth_grants WHERE id=?", (tok.grant_id,))
                if grant:
                    await control.revoke_grant(
                        db, target_of(grant), grant["id"], reason="client_revoked", **actor
                    )
            elif isinstance(tok, RavnAccess):
                session = await one(
                    db,
                    "SELECT * FROM sessions WHERE app_id=? AND tenant_id=? AND id=?",
                    (tok.app_id, tok.tenant_id, tok.session_id),
                )
                if session:
                    await control.revoke_session(db, target_of(session), session, **actor)

    # Operator views ----------------------------------------------------------

    async def list_grants(self, include_revoked=False) -> list[dict]:
        stamp = now()
        sql, params = GRANT_SQL, [stamp]
        if not include_revoked:
            sql += " WHERE g.revoked_at IS NULL AND g.expires_at>?"
            params.append(stamp)
        sql += " ORDER BY g.created_at DESC, g.id DESC LIMIT 200"
        async with self.store.transaction() as db:
            return [public_grant(row) for row in await rows(db, sql, params)]

    async def revoke_grant_by_id(self, grant_id, actor_kind, actor_id, request_id) -> dict:
        async with self.store.transaction() as db:
            row = await one(db, "SELECT * FROM oauth_grants WHERE id=?", (grant_id,))
            if row is None:
                raise RavnError(404, "not_found", "Grant not found.")
            await control.revoke_grant(
                db,
                target_of(row),
                grant_id,
                reason="operator",
                actor_kind=actor_kind,
                actor_id=actor_id,
                request_id=request_id,
            )
            return public_grant(await one(db, GRANT_SQL + " WHERE g.id=?", (now(), grant_id)))

    # Expiry ------------------------------------------------------------------

    async def cleanup(self):
        stamp, old = now(), deadline(-86400)
        async with self.store.transaction() as db:
            await db.execute(
                "UPDATE oauth_requests SET status='expired' WHERE status='pending' AND expires_at<=?",
                (stamp,),
            )
            await db.execute("DELETE FROM oauth_codes WHERE expires_at<?", (old,))
            await db.execute(
                "DELETE FROM oauth_requests WHERE expires_at<? "
                "AND id NOT IN (SELECT request_id FROM oauth_codes)",
                (old,),
            )
            # Reuse detection only matters while a grant can still refresh.
            await db.execute(
                "DELETE FROM oauth_refresh_tokens WHERE rotated_at IS NOT NULL AND grant_id IN "
                "(SELECT id FROM oauth_grants WHERE revoked_at IS NOT NULL OR expires_at<=?)",
                (stamp,),
            )
            await db.execute(
                "DELETE FROM oauth_clients WHERE created_at<? "
                "AND id NOT IN (SELECT client_id FROM oauth_grants) "
                "AND id NOT IN (SELECT client_id FROM oauth_requests) "
                "AND id NOT IN (SELECT client_id FROM oauth_codes)",
                (old,),
            )

    async def sweep(self):
        while True:
            await self.cleanup()
            await asyncio.sleep(60)


class RevokeRequest(BaseModel):
    token: str = Field(max_length=4096)
    token_type_hint: str | None = None
    client_id: str
    client_secret: str | None = None


class RevokeHandler:
    """RFC 7009 for public clients; the SDK's version requires a client_secret field."""

    def __init__(self, provider: McpOAuth, authenticator: ClientAuthenticator):
        self.provider, self.authenticator = provider, authenticator

    async def handle(self, request: Request) -> Response:
        try:
            client = await self.authenticator.authenticate_request(request)
        except AuthenticationError:
            return oauth_error(401, "invalid_client", "Unknown client.")
        try:
            body = RevokeRequest.model_validate(dict(await request.form()))
        except ValidationError:
            return oauth_error(400, "invalid_request", "Malformed revocation request.")
        found = await self.provider.load_access_token(body.token)
        if found is None:
            found = await self.provider.load_refresh_token(client, body.token)
        if found is not None and found.client_id == client.client_id:
            await self.provider.revoke_token(found)
        return Response(
            status_code=200, headers={"Cache-Control": "no-store", "Pragma": "no-cache"}
        )


def routes(service) -> list[Route]:
    config, provider = service.config, service.mcp_oauth
    base = issuer(config)
    built = build_metadata(
        AnyHttpUrl(base),
        None,
        ClientRegistrationOptions(enabled=True),
        RevocationOptions(enabled=True),
    )
    # Rebuilt from strings so the issuer keeps its exact form (no trailing slash).
    metadata = OAuthMetadata.model_validate(
        {
            **built.model_dump(mode="json", exclude_none=True),
            "issuer": base,
            "authorization_endpoint": base + "/authorize",
            "token_endpoint": base + "/token",
            "registration_endpoint": base + "/register",
            "revocation_endpoint": base + "/revoke",
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
        }
    )
    resource = ProtectedResourceMetadata.model_validate(
        {"resource": resource_url(config), "authorization_servers": [base]}
    )
    authenticator = ClientAuthenticator(provider)
    authorize = AuthorizationHandler(provider)
    token_handler = TokenHandler(provider, authenticator)
    register = RegistrationHandler(provider, options=ClientRegistrationOptions(enabled=True))
    revoke = RevokeHandler(provider, authenticator)
    registrations: deque[float] = deque()

    def traced(handler):
        async def endpoint(request: Request):
            marker = REQUEST_ID.set(getattr(request.state, "request_id", None))
            try:
                return await handler(request)
            finally:
                REQUEST_ID.reset(marker)

        return endpoint

    async def authorize_endpoint(request: Request):
        pairs = request.query_params.multi_items()
        if len(request.url.query) > 8192 or len(pairs) != len(dict(pairs)):
            return oauth_error(400, "invalid_request", "Repeated or oversized parameters.")
        return await authorize.handle(request)

    async def token_endpoint(request: Request):
        form = await request.form()
        if "resource" in form and form["resource"] != resource_url(config):
            return oauth_error(400, "invalid_target", "Unknown resource.")
        return await token_handler.handle(request)

    async def register_endpoint(request: Request):
        stamp = time.monotonic()
        while registrations and registrations[0] <= stamp - 60:
            registrations.popleft()
        if len(registrations) >= REGISTRATIONS_PER_MINUTE:
            return oauth_error(429, "temporarily_unavailable", "Too many registrations.")
        registrations.append(stamp)
        return await register.handle(request)

    return [
        Route(
            "/.well-known/oauth-authorization-server",
            MetadataHandler(metadata).handle,
            methods=["GET"],
        ),
        Route(
            urlsplit(prm_url(config)).path,
            ProtectedResourceMetadataHandler(resource).handle,
            methods=["GET"],
        ),
        Route("/authorize", traced(authorize_endpoint), methods=["GET"]),
        Route("/token", traced(token_endpoint), methods=["POST"]),
        Route("/register", traced(register_endpoint), methods=["POST"]),
        Route("/revoke", traced(revoke.handle), methods=["POST"]),
    ]
