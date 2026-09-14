"""Optional local-owner console. No application key or runtime token grants access."""

import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import Field
from starlette.staticfiles import StaticFiles

from ravn import control
from ravn.app import Boundary, attach_errors, error_response
from ravn.common import RavnError, digest, identifier, now
from ravn.config import Model
from ravn.manifest import schemas_for
from ravn.service import ACTOR, public_connection, public_session
from ravn.store import event, one, rows

PREFIX = "/console/api/v1"
COOKIE = "ravn_operator"
STATIC = Path(__file__).parent / "static" / "console"


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
                "writes": False,
                "scope_editing": False,
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
            current = [
                n
                for n, pin in ceiling.items()
                if integration and integration.schema_hashes.get(n) == pin
            ]
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
            elif not current:
                reason = "no_reviewed_tools"
            return {
                **status,
                **namespace,
                "tools": sorted(ceiling),
                "current_tools": current if reason is None else [],
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
            "calls": {"running", "succeeded", "tool_error", "failed", "unknown"},
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


class ConsoleBoundary:
    def __init__(self, app, console):
        self.console = console
        self.app = Boundary(app, console.service, admin=True)

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
            if path.startswith(PREFIX):
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
                            (b"referrer-policy", b"no-referrer"),
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
