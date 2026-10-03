"""Optional local-owner console. No application key or runtime token grants access."""

import hmac
import html
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import Field
from starlette.staticfiles import StaticFiles

from ravn import control
from ravn.app import Boundary, attach_errors, error_response, parse_form
from ravn.approvals import clean
from ravn.common import RavnError, digest, identifier, now
from ravn.config import Model
from ravn.manifest import is_write, schemas_for
from ravn.permissions import effective_tools, session_rules
from ravn.service import ACTOR, public_connection, public_session
from ravn.store import event, one, rows

PREFIX = "/console/api/v1"
COOKIE = "ravn_operator"
STATIC = Path(__file__).parent / "static" / "console"
# Server-rendered pages for agent sign-in; outside PREFIX so a signed-out browser
# gets instructions instead of a JSON 401.
OAUTH_PAGES = "/console/oauth/"
CONSENT = "/console/oauth/consent"
GRANTS = "/console/oauth/grants"
GRANTS_REVOKE = "/console/oauth/grants/revoke"


def effect_of(integration, name: str) -> str:
    return "write" if integration and is_write(name, integration.connector) else "read"


@dataclass(frozen=True)
class OperatorPrincipal:
    id: str
    secret_hash: str
    csrf: str
    deadline: float
    expires_at: str


class TicketRequest(Model):
    ticket: str = Field(min_length=40, max_length=128)


class TargetRequest(Model):
    app_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    tenant_id: str = Field(pattern=ACTOR.pattern)


class KeyTarget(Model):
    app_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    revoke_sessions: bool = Field(default=False, strict=True)


class PermissionRequest(Model):
    app_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    tenant_id: str = Field(pattern=ACTOR.pattern)
    # Tool name to allowed. Only names already in the session's ceiling are accepted.
    tools: dict[str, bool] = Field(min_length=1, max_length=64)


def invalid(message="Invalid console request."):
    return RavnError(400, "invalid_request", message)


class Console:
    def __init__(self, service, port=8788, *, cookie_name=COOKIE):
        if not 1 <= port <= 65535 or port == int(service.config.server.listen.rsplit(":", 1)[1]):
            raise ValueError("Console requires a distinct valid port")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,100}", cookie_name):
            raise ValueError("Console requires a valid cookie name")
        self.cookie_name = cookie_name
        self.service, self.port = service, port
        self.origin = f"http://127.0.0.1:{port}"
        self.tickets, self.sessions, self.cursors = {}, {}, {}
        service.consent_url = self.origin + CONSENT

    def sweep(self):
        stamp = time.monotonic()
        self.tickets = {k: v for k, v in self.tickets.items() if v > stamp}
        self.sessions = {k: v for k, v in self.sessions.items() if v.deadline > stamp}
        self.cursors = {k: v for k, v in self.cursors.items() if v["deadline"] > stamp}

    def close(self):
        self.tickets.clear()
        self.sessions.clear()
        self.cursors.clear()

    def ticket(self):
        self.sweep()
        if len(self.tickets) >= 16:
            raise RavnError(429, "rate_limited", "Too many pending console sign-ins.")
        value = secrets.token_urlsafe(32)
        self.tickets[digest(value)] = time.monotonic() + 60
        return {"url": self.origin + "/console/#ticket=" + value, "expires_in": 60}

    async def exchange(self, value, request_id):
        self.sweep()
        if self.tickets.pop(digest(value), None) is None:
            raise RavnError(
                401,
                "unauthenticated",
                "Login link expired or was already used. Run ravn console again.",
            )
        if len(self.sessions) >= 32:
            raise RavnError(429, "rate_limited", "Too many console sessions.")
        secret = secrets.token_urlsafe(32)
        p = OperatorPrincipal(
            identifier("op"),
            digest(secret),
            secrets.token_urlsafe(32),
            time.monotonic() + 3600,
            (datetime.now(UTC) + timedelta(hours=1))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
        )
        async with self.service.store.transaction() as db:
            # Recheck inside the serialized unit of work: concurrent exchanges
            # must not all pass the earlier capacity check while another awaits I/O.
            if len(self.sessions) >= 32:
                raise RavnError(429, "rate_limited", "Too many console sessions.")
            await event(
                db,
                control.AuditTarget(None),
                "operator.login",
                p.id,
                actor_kind="local_operator",
                actor_id=p.id,
                request_id=request_id,
            )
        self.sessions[p.secret_hash] = p
        return p, secret

    def authenticate(self, value):
        p = self.sessions.get(digest(value))
        self.ensure(p)
        return p

    def ensure(self, p):
        if p is None or self.sessions.get(p.secret_hash) is not p or p.deadline <= time.monotonic():
            raise RavnError(
                401, "unauthenticated", "Console session expired. Run ravn console again."
            )

    async def logout(self, p, request_id):
        self.sessions.pop(p.secret_hash, None)
        async with self.service.store.transaction() as db:
            await event(
                db,
                control.AuditTarget(None),
                "operator.logout",
                p.id,
                actor_kind="local_operator",
                actor_id=p.id,
                request_id=request_id,
            )

    async def bootstrap(self, p):
        async with self.service.store.transaction() as db:
            self.ensure(p)
            apps = await rows(db, "SELECT * FROM applications ORDER BY id")
        cfg = self.service.config
        for app in apps:
            configured = next((a for a in cfg.applications if a.id == app["id"]), None)
            app["enabled"] = bool(configured and configured.enabled)
            app["configured"] = configured is not None
        return {
            "deployment_id": cfg.deployment_id,
            "version": "0.1.0.dev0",
            "ready": self.service.ready,
            "applications": apps,
            "sessions": cfg.sessions.model_dump(),
            "integrations": [
                {
                    "app_id": i.app_id,
                    "id": i.id,
                    "enabled": i.enabled,
                    "endpoint": i.endpoint,
                    "schema_hashes": i.schema_hashes,
                    "reviewed_tools": [n for n in schemas_for(i.connector) if n in i.schema_hashes],
                    "tools": [
                        {"name": n, "effect": effect_of(i, n)}
                        for n in schemas_for(i.connector)
                        if n in i.schema_hashes
                    ],
                    "connector": i.connector,
                    "oauth_configured": i.oauth is not None,
                    "oauth_profile": i.oauth.profile if i.oauth else None,
                    "callback_url": cfg.server.public_url.rstrip("/")
                    + f"/oauth/callback/{i.app_id}/{i.id}"
                    if i.oauth
                    else None,
                }
                for i in cfg.integrations
            ],
            "features": {
                "oauth": True,
                # A reviewed write tool is pinned somewhere, so the console must not
                # keep claiming writes are unavailable.
                "writes": any(
                    effect_of(i, n) == "write" for i in cfg.integrations for n in i.schema_hashes
                ),
                "scope_editing": False,
                "session_permissions": True,
                "live_provider_verified": False,
            },
        }

    async def project(self, db, table, row):
        namespace = {k: row[k] for k in ("app_id", "tenant_id", "user_id") if k in row}
        if table == "connections":
            integration = self.service.config.integration(row["app_id"], row["integration_id"])
            enabled = (
                self.service.config.app(row["app_id"]) is not None
                and integration
                and integration.enabled
            )
            return {
                **public_connection(row),
                **namespace,
                "broker_access": "enabled" if row["status"] == "active" and enabled else "blocked",
                "provider_access": "not_checked",
                "verified_at_import": row["created_at"],
            }
        if table == "sessions":
            conn = await one(
                db,
                "SELECT * FROM connections WHERE app_id=? AND tenant_id=? AND id=?",
                (row["app_id"], row["tenant_id"], row["connection_id"]),
            )
            integration = (
                self.service.config.integration(row["app_id"], conn["integration_id"])
                if conn
                else None
            )
            ceiling = json.loads(row["tools"])
            pinned = [
                n
                for n, pin in ceiling.items()
                if integration and integration.schema_hashes.get(n) == pin
            ]
            rules = await session_rules(db, row["app_id"], row["tenant_id"], row["id"])
            current = effective_tools(ceiling, integration, rules)
            status = public_session(row)
            reason = None
            if status["status"] != "active":
                reason = status["status"]
            elif not conn or conn["status"] != "active" or conn["epoch"] != row["connection_epoch"]:
                reason = "connection_disabled"
            elif (
                not self.service.config.app(row["app_id"])
                or not integration
                or not integration.enabled
            ):
                reason = "integration_disabled"
            elif not pinned:
                reason = "no_reviewed_tools"
            elif not current:
                reason = "all_tools_denied"
            return {
                **status,
                **namespace,
                "tools": sorted(ceiling),
                "current_tools": current if reason is None else [],
                # Every name the operator may toggle, with its present decision.
                "permissions": [
                    {"name": n, "allowed": rules.get(n, True), "effect": effect_of(integration, n)}
                    for n in sorted(pinned)
                ],
                "blocked_reason": reason,
                "broker_access": "enabled" if reason is None else "blocked",
            }
        if table == "calls":
            return {
                k: row[k]
                for k in (
                    "id",
                    "app_id",
                    "tenant_id",
                    "user_id",
                    "connection_id",
                    "session_id",
                    "tool",
                    "effect",
                    "status",
                    "arguments_fingerprint",
                    "fingerprint_key_id",
                    "error_code",
                    "duration_ms",
                    "created_at",
                    "updated_at",
                    "completed_at",
                )
            }
        if table == "events":
            return {**row, "actor_kind": row["actor_kind"] or "legacy_unknown"}
        if table == "app_keys":
            return {k: row[k] for k in ("id", "app_id", "label", "created_at", "revoked_at")}
        raise invalid()

    async def detail(self, p, table, identity, app, tenant):
        self.validate_namespace(app, tenant)
        async with self.service.store.transaction() as db:
            self.ensure(p)
            row = await one(
                db,
                f"SELECT * FROM {table} WHERE app_id=? AND tenant_id=? AND id=?",
                (app, tenant, identity),
            )
            if row is None:
                raise RavnError(
                    404, "not_found", "Record not found in this application and tenant."
                )
            return await self.project(db, table, row)

    @staticmethod
    def validate_namespace(app, tenant=None):
        if (
            not app
            or not ACTOR.fullmatch(app)
            or (tenant is not None and not ACTOR.fullmatch(tenant))
        ):
            raise invalid("An explicit application and valid namespace are required.")

    async def listing(self, p, table, query):
        if table not in {"connections", "sessions", "calls", "events", "app_keys"}:
            raise invalid()
        self.sweep()
        self.ensure(p)
        query = dict(query)
        cursor = query.pop("cursor", None)
        allowed = {"app_id", "limit"}
        if table != "app_keys":
            allowed |= {"tenant_id", "user_id"}
        fields = {
            "connections": {"status"},
            "sessions": {"status", "connection_id"},
            "calls": {"status", "connection_id", "session_id", "tool"},
            "events": {"kind", "subject_id"},
            "app_keys": set(),
        }[table]
        allowed |= fields
        if table in {"calls", "events"}:
            allowed |= {"from", "to"}
        if table == "events":
            allowed.add("scope")
        if set(query) - allowed:
            raise invalid("Unsupported query filter.")
        deployment = table == "events" and query.get("scope") == "deployment"
        if query.get("scope", "application") not in {"application", "deployment"}:
            raise invalid()
        if deployment:
            if any(k in query for k in ("app_id", "tenant_id", "user_id")):
                raise invalid("Deployment events cannot have application filters.")
        else:
            self.validate_namespace(query.get("app_id"), query.get("tenant_id"))
        for field in {"user_id", "connection_id", "session_id", "subject_id"} & query.keys():
            if not ACTOR.fullmatch(query[field]):
                raise invalid()
        if any(len(v) > 256 for v in query.values()):
            raise invalid()
        statuses = {
            "connections": {"active", "disconnected", "reconnect_required"},
            "sessions": {"active", "expired", "revoked"},
            "calls": {"running", "succeeded", "tool_error", "failed", "unknown", "denied"},
        }
        if "status" in query and query["status"] not in statuses.get(table, set()):
            raise invalid("Unsupported record status.")
        try:
            limit = int(query.get("limit", "50"))
        except ValueError:
            raise invalid() from None
        if not 1 <= limit <= 100:
            raise invalid("Page size must be 1–100.")
        context = (p.id, table, tuple(sorted(query.items())))
        prior = self.cursors.get(cursor) if cursor else None
        if cursor and (prior is None or prior["context"] != context):
            raise RavnError(400, "invalid_cursor", "Cursor expired or filters changed.")
        cutoff = prior["cutoff"] if prior else now()
        sql = f"SELECT * FROM {table} WHERE " + ("app_id IS NULL" if deployment else "app_id=?")
        params = [] if deployment else [query["app_id"]]
        sql += " AND created_at<=?"
        params.append(cutoff)
        for field in ({"tenant_id", "user_id"} | fields) & query.keys():
            if table == "sessions" and field == "status":
                sql += " AND (CASE WHEN revoked_at IS NOT NULL THEN 'revoked' WHEN expires_at<=? THEN 'expired' ELSE 'active' END)=?"
                params.extend([cutoff, query[field]])
            else:
                sql += f" AND {field}=?"
                params.append(query[field])
        if table in {"calls", "events"}:
            try:
                end = datetime.fromisoformat(query.get("to", cutoff).replace("Z", "+00:00"))
                start = (
                    datetime.fromisoformat(query["from"].replace("Z", "+00:00"))
                    if "from" in query
                    else end - timedelta(days=1)
                )
                if (
                    start.tzinfo is None
                    or end.tzinfo is None
                    or not timedelta(0) <= end - start <= timedelta(days=30)
                ):
                    raise ValueError
            except ValueError:
                raise invalid("Choose a timezone-aware time range of at most 30 days.") from None
            sql += " AND created_at>=? AND created_at<=?"
            params.extend(
                d.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
                for d in (start, end)
            )
        if prior:
            sql += " AND (created_at,id)<(?,?)"
            params.extend(prior["last"])
        sql += " ORDER BY created_at DESC,id DESC LIMIT ?"
        params.append(limit + 1)
        async with self.service.store.transaction() as db:
            self.ensure(p)
            result = await rows(db, sql, params)
            data = [await self.project(db, table, row) for row in result[:limit]]
        next_cursor = None
        if len(result) > limit:
            if len(self.cursors) >= 256:
                raise RavnError(429, "rate_limited", "Too many active list cursors.")
            next_cursor = secrets.token_urlsafe(32)
            self.cursors[next_cursor] = {
                "context": context,
                "cutoff": cutoff,
                "deadline": time.monotonic() + 300,
                "last": (result[limit - 1]["created_at"], result[limit - 1]["id"]),
            }
        return {"data": data, "next_cursor": next_cursor, "as_of": cutoff}

    async def reduce_access(
        self, p, table, identity, app, tenant, request_id, *, revision=None, revoke_sessions=False
    ):
        async with self.service.store.transaction() as db:
            self.ensure(p)
            sql = f"SELECT * FROM {table} WHERE id=? AND app_id=?"
            params = [identity, app]
            if table != "app_keys":
                sql += " AND tenant_id=?"
                params.append(tenant)
            row = await one(db, sql, params)
            if row is None:
                raise RavnError(404, "not_found", "Record not found in this namespace.")
            target = control.AuditTarget(app, row.get("tenant_id"), row.get("user_id"))
            audit = {"actor_kind": "local_operator", "actor_id": p.id, "request_id": request_id}
            if table == "connections":
                if row["status"] != "disconnected" and revision != f'"{row["revision"]}"':
                    raise RavnError(
                        412, "revision_mismatch", "Connection changed. Reload and confirm again."
                    )
                await control.disconnect(db, target, row, **audit)
            elif table == "sessions":
                await control.revoke_session(db, target, row, **audit)
            else:
                await control.revoke_key(db, target, row, revoke_sessions, **audit)
        return {"id": identity, "committed": True}

    async def set_permissions(self, p, identity, app, tenant, tools, request_id):
        async with self.service.store.transaction() as db:
            self.ensure(p)
            row = await one(
                db,
                "SELECT * FROM sessions WHERE id=? AND app_id=? AND tenant_id=?",
                (identity, app, tenant),
            )
            if row is None:
                raise RavnError(404, "not_found", "Record not found in this namespace.")
            conn = await one(
                db,
                "SELECT * FROM connections WHERE app_id=? AND tenant_id=? AND id=?",
                (app, tenant, row["connection_id"]),
            )
            integration = (
                self.service.config.integration(app, conn["integration_id"]) if conn else None
            )
            ceiling = json.loads(row["tools"])
            # Refuse names this session could never call, so a stale page cannot
            # look like it granted something.
            unknown = sorted(set(tools) - set(ceiling))
            if unknown:
                raise RavnError(
                    422,
                    "invalid_request",
                    "These tools are outside this session's ceiling: " + ", ".join(unknown),
                )
            target = control.AuditTarget(app, row["tenant_id"], row["user_id"])
            await control.set_permissions(
                db,
                target,
                row,
                tools,
                actor_kind="local_operator",
                actor_id=p.id,
                request_id=request_id,
            )
            rules = await session_rules(db, app, tenant, identity)
            return {
                "id": identity,
                "committed": True,
                "permissions": [
                    {"name": n, "allowed": rules.get(n, True), "effect": effect_of(integration, n)}
                    for n in sorted(ceiling)
                    if integration and integration.schema_hashes.get(n) == ceiling[n]
                ],
            }


class ConsoleBoundary:
    def __init__(self, app, console):
        self.console = console
        self.app = Boundary(
            app, console.service, admin=True, forms=frozenset({CONSENT, GRANTS_REVOKE})
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = identifier("req")
        try:
            if sum(len(k) + len(v) for k, v in scope["headers"]) > 16384:
                raise invalid("Headers exceed the limit.")
            headers = {}
            for k, v in scope["headers"]:
                k = k.lower()
                if k in headers and k in {b"cookie", b"host", b"origin", b"x-csrf-token"}:
                    raise invalid("Repeated security header.")
                headers[k] = v.decode("latin1")
            if headers.get(b"host") != self.console.origin.removeprefix("http://"):
                raise invalid("Unrecognized console host.")
            origin = headers.get(b"origin")
            if origin is not None and origin != self.console.origin:
                raise RavnError(
                    403, "permission_denied", "Cross-origin console requests are forbidden."
                )
            path = scope["path"]
            pages = path.startswith(OAUTH_PAGES)
            if pages:
                if b"authorization" in headers or any(k.startswith(b"x-ravn-") for k in headers):
                    raise RavnError(
                        401,
                        "unauthenticated",
                        "Application and runtime credentials cannot access the console.",
                    )
                if scope["method"] not in {"GET", "HEAD"} and origin != self.console.origin:
                    raise RavnError(
                        403, "permission_denied", "Console forms require a same-origin request."
                    )
                values = [
                    part.strip().split("=", 1)[1]
                    for part in headers.get(b"cookie", "").split(";")
                    if part.strip().startswith(self.console.cookie_name + "=")
                ]
                if len(values) > 1:
                    raise invalid("Repeated console cookie.")
                operator = None
                if values:
                    try:
                        operator = self.console.authenticate(values[0])
                    except RavnError:
                        operator = None
                # The form posts its CSRF token as a field, checked by the route.
                scope.setdefault("state", {})["operator"] = operator
            elif path.startswith(PREFIX):
                if b"authorization" in headers or any(k.startswith(b"x-ravn-") for k in headers):
                    raise RavnError(
                        401,
                        "unauthenticated",
                        "Application and runtime credentials cannot access the console.",
                    )
                mutation = scope["method"] not in {"GET", "HEAD"}
                if mutation and origin != self.console.origin:
                    raise RavnError(
                        403, "permission_denied", "Console mutations require a same-origin request."
                    )
                if path != PREFIX + "/auth/exchange":
                    values = [
                        part.strip().split("=", 1)[1]
                        for part in headers.get(b"cookie", "").split(";")
                        if part.strip().startswith(self.console.cookie_name + "=")
                    ]
                    if len(values) != 1:
                        raise RavnError(
                            401, "unauthenticated", "Open the console with ravn console."
                        )
                    p = self.console.authenticate(values[0])
                    if mutation and not hmac.compare_digest(
                        headers.get(b"x-csrf-token", "").encode("latin1"), p.csrf.encode()
                    ):
                        raise RavnError(403, "permission_denied", "Invalid CSRF token.")
                    scope.setdefault("state", {})["operator"] = p

            async def secure_send(message):
                if message["type"] == "http.response.start":
                    message["headers"].extend(
                        [
                            (
                                b"content-security-policy",
                                b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'",
                            ),
                            # A form POST under no-referrer carries Origin: null, so the
                            # consent pages keep the origin for same-origin requests only.
                            (
                                b"referrer-policy",
                                b"same-origin" if pages else b"no-referrer",
                            ),
                            (b"x-content-type-options", b"nosniff"),
                            (b"x-frame-options", b"DENY"),
                        ]
                    )
                await send(message)

            await self.app(scope, receive, secure_send)
        except RavnError as exc:
            await error_response(exc, request_id)(scope, receive, send)


def create_console_app(console):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.add_middleware(ConsoleBoundary, console=console)
    attach_errors(app)

    def p(request):
        return request.state.operator

    def query(request):
        if len(request.query_params) != len(request.query_params.multi_items()):
            raise invalid("Repeated query parameter.")
        return dict(request.query_params)

    @app.post(PREFIX + "/auth/exchange")
    async def exchange(request: Request, body: TicketRequest):
        principal, secret = await console.exchange(body.ticket, request.state.request_id)
        response = JSONResponse(
            {"id": principal.id, "csrf_token": principal.csrf, "expires_at": principal.expires_at}
        )
        # Local-loopback HTTP only; never reuse these cookie settings for remote deployment.
        response.set_cookie(
            console.cookie_name,
            secret,
            max_age=3600,
            httponly=True,
            samesite="strict",
            path="/console",
        )
        return response

    @app.get(PREFIX + "/auth/session")
    async def session(request: Request):
        principal = p(request)
        return {
            "id": principal.id,
            "csrf_token": principal.csrf,
            "expires_at": principal.expires_at,
        }

    @app.post(PREFIX + "/auth/logout")
    async def logout(request: Request):
        await console.logout(p(request), request.state.request_id)
        response = JSONResponse({"logged_out": True})
        response.delete_cookie(console.cookie_name, path="/console")
        return response

    @app.get(PREFIX + "/bootstrap")
    async def bootstrap(request: Request):
        return await console.bootstrap(p(request))

    # Closures capture only hard-coded table names, never caller-controlled SQL identifiers.
    def add_resource(table, route, detail=True):
        async def listing(request: Request):
            return await console.listing(p(request), table, query(request))

        app.add_api_route(PREFIX + "/" + route, listing, methods=["GET"], name=route)
        if detail:

            async def get_detail(identity: str, request: Request, app_id: str, tenant_id: str):
                if set(query(request)) != {"app_id", "tenant_id"}:
                    raise invalid()
                return await console.detail(p(request), table, identity, app_id, tenant_id)

            app.add_api_route(
                PREFIX + "/" + route + "/{identity}",
                get_detail,
                methods=["GET"],
                name=route + "_detail",
            )

    for table, route in [
        ("connections", "connections"),
        ("sessions", "sessions"),
        ("calls", "calls"),
    ]:
        add_resource(table, route)
    add_resource("events", "events", False)
    add_resource("app_keys", "app-keys", False)

    @app.post(PREFIX + "/connections/{identity}/disconnect")
    async def disconnect(identity: str, body: TargetRequest, request: Request):
        return await console.reduce_access(
            p(request),
            "connections",
            identity,
            body.app_id,
            body.tenant_id,
            request.state.request_id,
            revision=request.headers.get("if-match"),
        )

    @app.post(PREFIX + "/sessions/{identity}/revoke")
    async def revoke(identity: str, body: TargetRequest, request: Request):
        return await console.reduce_access(
            p(request), "sessions", identity, body.app_id, body.tenant_id, request.state.request_id
        )

    @app.put(PREFIX + "/sessions/{identity}/permissions")
    async def set_permissions(identity: str, body: PermissionRequest, request: Request):
        return await console.set_permissions(
            p(request),
            identity,
            body.app_id,
            body.tenant_id,
            body.tools,
            request.state.request_id,
        )

    @app.post(PREFIX + "/app-keys/{identity}/revoke")
    async def revoke_key(identity: str, body: KeyTarget, request: Request):
        return await console.reduce_access(
            p(request),
            "app_keys",
            identity,
            body.app_id,
            None,
            request.state.request_id,
            revoke_sessions=body.revoke_sessions,
        )

    def page(title, text, status=200):
        return HTMLResponse(render_message(title, text), status_code=status)

    def signed_out():
        return page(
            "Sign in to RAVN",
            "Run `uv run ravn console` in a terminal to sign in to the console, "
            "then reload this page.",
        )

    async def form(request, fields):
        values = parse_form(await request.body())
        if set(values) - fields:
            raise invalid("Unsupported form field.")
        return values

    def csrf_ok(operator, values):
        return hmac.compare_digest(values.get("csrf", "").encode(), operator.csrf.encode())

    @app.get(CONSENT)
    async def consent_page(request: Request):
        if set(query(request)) != {"request"}:
            return page("Restart from your agent", "This sign-in link is malformed.", 400)
        operator = request.state.operator
        if operator is None:
            return signed_out()
        try:
            view = await console.service.mcp_oauth.pending_view(
                operator, request.query_params["request"]
            )
        except RavnError as error:
            return page("Restart from your agent", error.message, error.status)
        return HTMLResponse(render_consent(view, operator.csrf))

    @app.post(CONSENT)
    async def consent_decision(request: Request):
        operator = request.state.operator
        if operator is None:
            return page(
                "Sign in to RAVN",
                "Your console sign-in expired. Run `uv run ravn console`, then reload the consent page.",
                401,
            )
        values = await form(request, {"request", "csrf", "connection", "decision"})
        if not csrf_ok(operator, values):
            return page("Request refused", "Reload the consent page and try again.", 403)
        try:
            url = await console.service.mcp_oauth.decide(
                operator,
                values.get("request"),
                values.get("decision"),
                values.get("connection"),
                request.state.request_id,
            )
        except RavnError as error:
            return page("Consent not recorded", error.message, error.status)
        return RedirectResponse(url, status_code=303)

    @app.get(GRANTS)
    async def grants_page(request: Request):
        if query(request):
            raise invalid("Unsupported query filter.")
        operator = request.state.operator
        if operator is None:
            return signed_out()
        grants = await console.service.mcp_oauth.list_grants(include_revoked=True)
        return HTMLResponse(render_grants(grants, operator.csrf))

    @app.post(GRANTS_REVOKE)
    async def grants_revoke(request: Request):
        operator = request.state.operator
        if operator is None:
            return signed_out()
        values = await form(request, {"grant", "csrf"})
        if not csrf_ok(operator, values):
            return page("Request refused", "Reload the grants page and try again.", 403)
        try:
            await console.service.mcp_oauth.revoke_grant_by_id(
                values.get("grant", ""), "local_operator", operator.id, request.state.request_id
            )
        except RavnError as error:
            return page("Grant not revoked", error.message, error.status)
        return RedirectResponse(GRANTS, status_code=303)

    @app.get("/")
    async def root():
        return RedirectResponse("/console/")

    @app.get("/console/")
    async def index():
        if not (STATIC / "index.html").is_file():
            raise RavnError(
                503,
                "dependency_unavailable",
                "Console assets are missing. Build broker/console first.",
            )
        return FileResponse(STATIC / "index.html")

    if STATIC.is_dir():
        app.mount("/console/assets", StaticFiles(directory=STATIC / "assets", check_dir=False))
        app.mount(
            "/console/reference", StaticFiles(directory=STATIC / "reference", check_dir=False)
        )
    return app


STYLE = """
body{font:15px/1.5 system-ui,sans-serif;max-width:44rem;margin:2rem auto;padding:0 16px;
color:#1d1d1f;background:#fafaf9}
h1{font-size:1.4rem}code{background:#eee;padding:0 .3em;border-radius:3px}
.note{color:#555;font-size:.9rem}fieldset{border:1px solid #ccc;border-radius:6px;margin:1rem 0}
label{display:block;padding:.3rem 0}button{font:inherit;padding:.4rem 1rem;margin-right:.5rem}
table{border-collapse:collapse;width:100%;font-size:.85rem}td,th{border-bottom:1px solid #ddd;
padding:.3rem;text-align:left;vertical-align:top}
@media (prefers-color-scheme:dark){body{background:#1c1c1e;color:#eee}code{background:#333}
.note{color:#aaa}td,th{border-color:#444}}
"""


def text(value, limit=256) -> str:
    """Agent- or provider-controlled text, made inert for HTML."""
    return html.escape(clean("" if value is None else str(value), limit))


def document(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )


def render_message(title: str, message: str) -> str:
    return document(title, f"<h1>{html.escape(title)}</h1><p>{text(message, 1024)}</p>")


def render_consent(view: dict, csrf: str) -> str:
    choices = "".join(
        # Nothing is preselected: the list spans users and tenants, so a choice is explicit.
        f'<label><input type="radio" name="connection" value="{text(c["key"], 512)}" required>'
        f" {text(c['display_name'])}"
        f"{' (' + text(c['label']) + ')' if c['label'] else ''} "
        f'<span class="note">{text(c["integration_id"])} · app {text(c["app_id"])} · '
        f"tenant {text(c['tenant_id'])} · user {text(c['user_id'])}</span></label>"
        for c in view["connections"]
    )
    if not choices:
        choices = (
            '<p class="note">No connection can be given to an agent yet. Connect an account '
            "and pin its reviewed tools first.</p>"
        )
    return document(
        "Allow agent access",
        "<h1>Allow an agent to use RAVN?</h1>"
        f"<p><strong>{text(view['client_name'] or 'Unnamed client', 128)}</strong> "
        '<span class="note">(name is self-reported by the agent)</span></p>'
        f'<p class="note">Client {text(view["client_id"])} · returns to '
        f"{text(view['redirect'])} · request expires {text(view['expires_at'])}</p>"
        "<p>The agent gets a short-lived session for one connection, limited to its reviewed "
        "tools and any per-session rules. Writes still need approval.</p>"
        f'<form method="post" action="{CONSENT}">'
        f'<input type="hidden" name="request" value="{text(view["request_id"])}">'
        f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
        f"<fieldset><legend>Connection</legend>{choices}</fieldset>"
        '<button type="submit" name="decision" value="approve">Approve</button>'
        '<button type="submit" name="decision" value="deny" formnovalidate>Deny</button>'
        "</form>",
    )


def render_grants(grants: list[dict], csrf: str) -> str:
    rows_html = "".join(
        "<tr>"
        f"<td>{text(g['id'])}</td>"
        f"<td>{text(g['client_name'] or 'Unnamed client', 128)}<br>"
        f'<span class="note">{text(g["client_id"])}</span></td>'
        f"<td>{text(g['app_id'])} / {text(g['tenant_id'])} / {text(g['user_id'])}<br>"
        f'<span class="note">{text(g["connection_id"])}</span></td>'
        f"<td>{text(g['created_at'])}<br>expires {text(g['expires_at'])}</td>"
        f"<td>{text(g['rotation'])}</td><td>{text(g['active_sessions'])}</td>"
        + (
            f"<td>revoked {text(g['revoked_at'])}<br>{text(g['revoke_reason'])}</td>"
            if g["revoked_at"]
            else f'<td><form method="post" action="{GRANTS_REVOKE}">'
            f'<input type="hidden" name="grant" value="{text(g["id"])}">'
            f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
            '<button type="submit">Revoke</button></form></td>'
        )
        + "</tr>"
        for g in grants
    )
    return document(
        "Agent grants",
        "<h1>Agent grants</h1>"
        + (
            "<table><tr><th>Grant</th><th>Client</th><th>App / tenant / user</th>"
            "<th>Lifetime</th><th>Refreshes</th><th>Live sessions</th><th>Status</th></tr>"
            f"{rows_html}</table>"
            if grants
            else '<p class="note">No agent has been granted access.</p>'
        ),
    )
