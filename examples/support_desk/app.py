"""Customer app, not a RAVN admin UI. Only REST + the public binding helper + MCP.

Local test login is intentionally distinct from production SSO. No provider
credential, backend app key, or runtime bearer is ever returned to this browser.
"""

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import httpx2
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import FileResponse, JSONResponse, RedirectResponse

from ravn.onboarding_client import ConnectBinding

STATIC = Path(__file__).with_name("static")
COOKIE = "support_desk_login"
HINTS = {
    "login_required": "Open a fresh private Support Desk sign-in link from the local launcher.",
    "unauthenticated": "Your agent session expired or was stopped. Start a new session and try again.",
    "connection_disabled": "This connection is disconnected or disabled. Connect an account first.",
    "connection_reauth_required": "This account needs authorization again. Use Reconnect in Connections.",
    "credential_invalid": "The provider rejected this account's credential. Reconnect it.",
    "integration_not_ready": "Provider OAuth or reviewed tool schemas are not configured in RAVN yet. See the developer setup guide.",
    "invalid_return_url": "The developer must register this app's /connect/return URL in RAVN's return_urls.",
    "schema_drift": "Provider tools changed. The developer must review schemas and restart RAVN.",
    "permission_denied": "This action is not allowed by the connection or runtime session.",
    "not_found": "That connection or session does not belong to this account, or no longer exists.",
    "invalid_arguments": "Check the tool fields and try again.",
    "rate_limited": "Another operation is running. Wait for it to finish.",
    "upstream_unavailable": "The provider could not complete the read. No automatic retry was made.",
    "dependency_unavailable": "Cannot reach RAVN. Check the developer-side server configuration.",
}


def hashed(value):
    return hashlib.sha256(value.encode()).hexdigest()


class AppError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginBody(Body):
    ticket: str = Field(min_length=32, max_length=128)


class ConnectBody(Body):
    integration_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    reconnect_connection_id: str | None = Field(default=None, pattern=r"^conn_[a-f0-9]{32}$")


class RunBody(Body):
    connection_id: str = Field(pattern=r"^conn_[a-f0-9]{32}$")
    tool: str = Field(min_length=1, max_length=100)
    arguments: dict


@dataclass(repr=False)
class Login:
    actor: tuple[str, str, str]
    id: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    expires: float = field(default_factory=lambda: time.monotonic() + 3600)
    bindings: dict = field(default_factory=dict)
    runtimes: dict = field(default_factory=dict)
    activity: list = field(default_factory=list)
    notice: str | None = None
    busy: bool = False


class Desk:
    def __init__(
        self,
        *,
        origin,
        ravn_url,
        app_key,
        app_id="support-desk",
        tenant="default",
        user="you",
        demo=False,
        transport=None,
        mcp_transport=None,
    ):
        self.origin, self.ravn_url, self.app_key = origin, ravn_url.rstrip("/"), app_key
        for value in (origin, self.ravn_url):
            url = urlsplit(value)
            if (
                url.scheme not in {"https", "http"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path not in {"", "/"}
                or (url.scheme == "http" and url.hostname != "127.0.0.1")
            ):
                raise ValueError("Use HTTPS or an exact 127.0.0.1 origin")
        if not re.fullmatch(r"rv_app_[a-f0-9]{32}_[A-Za-z0-9_-]{43}", app_key):
            raise ValueError("A backend RAVN application key is required")
        self.actor, self.demo = (app_id, tenant, user), demo
        self.transport, self.mcp_transport = transport, mcp_transport
        self.logins, self.tickets = {}, {}
        self.active = 0

    def ticket(self):
        value = secrets.token_urlsafe(32)
        self.tickets = {hashed(value): time.monotonic() + 300}
        return self.origin + "/#login=" + value

    def login(self, request):
        now = time.monotonic()
        self.logins = {k: v for k, v in self.logins.items() if v.expires > now}
        names = [
            part.strip().split("=", 1)[0]
            for part in request.headers.get("cookie", "").split(";")
            if part.strip()
        ]
        if len(names) != len(set(names)):
            raise AppError("login_required", 401)
        value = self.logins.get(hashed(request.cookies.get(COOKIE, "")))
        if not value:
            raise AppError("login_required", 401)
        value.bindings = {k: v for k, v in value.bindings.items() if v.expires > now}
        return value

    async def ravn(self, login, method, path, body=None):
        try:
            async with httpx.AsyncClient(
                base_url=self.ravn_url,
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                timeout=35,
            ) as client:
                response = await client.request(
                    method,
                    path,
                    json=body,
                    headers={
                        "Authorization": "Bearer " + self.app_key,
                        "X-Ravn-User-Id": login.actor[2],
                        "X-Ravn-Tenant-Id": login.actor[1],
                    },
                )
            if len(response.content) > 1048576:
                raise AppError("dependency_unavailable", 502)
            data = response.json()
            if response.is_error:
                code = data.get("error", {}).get("code", "dependency_unavailable")
                raise AppError(
                    code if code in HINTS else "dependency_unavailable", response.status_code
                )
            return data
        except AppError:
            raise
        except Exception:
            raise AppError("dependency_unavailable", 503) from None

    async def execute(self, login, body):
        if login.busy or self.active >= 8:
            raise AppError("rate_limited", 429)
        login.busy = True
        self.active += 1
        started = time.monotonic()
        step = "connection"
        entry = {
            "id": secrets.token_hex(8),
            "tool": body.tool,
            "connection_id": body.connection_id,
            "created_at": time.time(),
            "status": "running",
            "steps": [],
        }
        login.activity.insert(0, entry)
        del login.activity[50:]
        try:
            connection = await self.ravn(login, "GET", f"/v1/connections/{body.connection_id}")
            if connection["status"] != "active":
                raise AppError(
                    "connection_reauth_required"
                    if connection["status"] == "reconnect_required"
                    else "connection_disabled",
                    409,
                )
            allowed = {
                "github": {"issue_read", "list_issues"},
                "slack": {
                    "slack_search_public",
                    "slack_search_public_and_private",
                    "slack_read_thread",
                },
            }
            if body.tool not in allowed.get(connection["integration_id"], set()):
                raise AppError("permission_denied", 403)
            entry["steps"].append("Account ownership checked by RAVN")
            step = "session"
            runtime = login.runtimes.get(body.connection_id)
            if runtime is None:
                runtime = await self.ravn(
                    login,
                    "POST",
                    "/v1/sessions",
                    {"connection_id": body.connection_id, "ttl_seconds": 600},
                )
                if runtime["mcp_url"] != self.ravn_url + "/mcp":
                    raise AppError("dependency_unavailable", 502)
                login.runtimes[body.connection_id] = runtime
                entry["steps"].append("A 10-minute connection-bound session created")
            else:
                entry["steps"].append("Existing connection-bound session selected")
            entry["session_id"] = runtime["id"]
            step = "mcp"
            async with (
                asyncio.timeout(40),
                httpx2.AsyncClient(
                    headers={"Authorization": "Bearer " + runtime["token"]},
                    transport=self.mcp_transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=35,
                ) as http,
            ):
                async with Client(
                    streamable_http_client(runtime["mcp_url"], http_client=http), cache=None
                ) as client:
                    tools = await client.list_tools()
                    if body.tool not in {t.name for t in tools.tools}:
                        raise AppError("permission_denied", 403)
                    entry["steps"].append("MCP tool discovered through RAVN")
                    result = await client.call_tool(body.tool, body.arguments)
            text = "\n\n".join(item.text for item in result.content if item.type == "text")
            if len(text.encode()) > 262144 or any(
                secret in text for secret in (self.app_key, runtime["token"])
            ):
                raise AppError("upstream_unavailable", 502)
            entry["status"] = "tool_error" if result.is_error else "succeeded"
            meta = result.meta or {}
            if isinstance(meta.get("ravn/call-id"), str) and re.fullmatch(
                r"call_[a-f0-9]{32}", meta["ravn/call-id"]
            ):
                entry["call_id"] = meta["ravn/call-id"]
            entry["steps"].append("Read result received; no write was requested")
            entry["duration_ms"] = int((time.monotonic() - started) * 1000)
            return {**entry, "text": text or "The tool returned no text content."}
        except Exception as exc:
            code = exc.code if isinstance(exc, AppError) else "upstream_unavailable"
            # MCP errors from RAVN contain a structured, sanitized code. Never
            # serialize arbitrary SDK exceptions/headers into a browser response.
            data = getattr(getattr(exc, "error", None), "data", None)
            if isinstance(data, dict) and data.get("code") in HINTS:
                code = data["code"]
            if step == "mcp":
                # Do not silently retry under replacement authority. A subsequent
                # explicit user click can request a new session.
                login.runtimes.pop(body.connection_id, None)
            entry.update(status="failed", error_code=code)
            raise AppError(code, exc.status if isinstance(exc, AppError) else 502) from None
        finally:
            if entry["status"] == "running":
                entry["status"] = "interrupted"
            entry["duration_ms"] = int((time.monotonic() - started) * 1000)
            login.busy = False
            self.active -= 1


def create_app(desk):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.state.desk = desk

    @app.middleware("http")
    async def boundary(request, next_call):
        try:
            headers = request.headers
            if (
                headers.get("host") != urlsplit(desk.origin).netloc
                or any(
                    len(headers.getlist(h)) > 1
                    for h in ("host", "cookie", "origin", "x-csrf-token", "content-length")
                )
                or any(h.startswith("x-ravn-") or h == "authorization" for h in headers)
                or len(request.url.query) > 8192
            ):
                raise AppError("permission_denied", 403)
            if request.method not in {"GET", "HEAD"}:
                if headers.get("origin") != desk.origin:
                    raise AppError("permission_denied", 403)
                if headers.get("content-type", "").split(";")[0] != "application/json":
                    raise AppError("invalid_arguments", 415)
                async with asyncio.timeout(5):
                    body = bytearray()
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > 16384:
                            raise AppError("invalid_arguments", 413)

                def pairs(items):
                    value = dict(items)
                    if len(value) != len(items):
                        raise ValueError("Duplicate field")
                    return value

                json.loads(body, object_pairs_hook=pairs)
                request._body = bytes(body)
                if request.url.path != "/api/login":
                    login = desk.login(request)
                    if not hmac.compare_digest(
                        hashed(headers.get("x-csrf-token", "")), hashed(login.csrf)
                    ):
                        raise AppError("permission_denied", 403)
            response = await next_call(request)
        except AppError as exc:
            response = JSONResponse(
                {
                    "error": {
                        "code": exc.code,
                        "message": HINTS.get(exc.code, "Request could not be completed."),
                    }
                },
                status_code=exc.status,
            )
        except Exception:
            response = JSONResponse(
                {
                    "error": {
                        "code": "invalid_request",
                        "message": "Request could not be completed. No automatic retry was made.",
                    }
                },
                status_code=400,
            )
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; object-src 'none'",
            }
        )
        return response

    @app.exception_handler(AppError)
    async def failure(request, exc):
        return JSONResponse(
            {
                "error": {
                    "code": exc.code,
                    "message": HINTS.get(exc.code, "Request could not be completed."),
                }
            },
            status_code=exc.status,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return JSONResponse(
            {"error": {"code": "invalid_arguments", "message": HINTS["invalid_arguments"]}},
            status_code=400,
        )

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/assets/{name}")
    async def asset(name: str):
        if name not in {"app.js", "app.css"}:
            raise AppError("not_found", 404)
        return FileResponse(STATIC / name)

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "mode": "simulated" if desk.demo else "live"}

    @app.post("/api/login")
    async def login(request: Request, body: LoginBody):
        expiry = desk.tickets.pop(hashed(body.ticket), 0)
        if expiry <= time.monotonic() or len(desk.logins) >= 16:
            raise AppError("login_required", 401)
        secret = secrets.token_urlsafe(32)
        desk.logins[hashed(secret)] = Login(desk.actor)
        response = JSONResponse({"ok": True})
        response.set_cookie(
            COOKIE,
            secret,
            httponly=True,
            secure=desk.origin.startswith("https://"),
            samesite="lax",
            max_age=3600,
            path="/",
        )
        return response

    @app.get("/api/state")
    async def state(request: Request):
        login = desk.login(request)
        connections = await desk.ravn(login, "GET", "/v1/connections?limit=100")
        sessions = await desk.ravn(login, "GET", "/v1/sessions?limit=100")
        notice, login.notice = login.notice, None
        return {
            "user": login.actor[2],
            "tenant": login.actor[1],
            "mode": "simulated" if desk.demo else "live",
            "csrf": login.csrf,
            "notice": notice,
            "connections": connections["data"],
            "sessions": sessions["data"],
            "activity": login.activity,
            "more_connections": bool(connections.get("next_cursor")),
            "more_sessions": bool(sessions.get("next_cursor")),
        }

    @app.post("/api/connect")
    async def connect(request: Request, body: ConnectBody):
        login = desk.login(request)
        if body.integration_id not in {"github", "slack"} or len(login.bindings) >= 8:
            raise AppError("invalid_arguments", 400)
        binding = ConnectBinding.for_login(login.actor, login.id)
        flow = await desk.ravn(
            login,
            "POST",
            "/v1/connect-sessions",
            {
                "integration_id": body.integration_id,
                "return_url": desk.origin + "/connect/return",
                "app_state": binding.app_state,
                "reconnect_connection_id": body.reconnect_connection_id,
            },
        )
        binding.session_id = flow["id"]
        if not flow["authorization_url"].startswith(desk.ravn_url + "/oauth/start/"):
            raise AppError("dependency_unavailable", 502)
        login.bindings[flow["id"]] = binding
        return {"authorization_url": flow["authorization_url"]}

    @app.get("/connect/return")
    async def returned(request: Request):
        login = desk.login(request)
        pairs = list(request.query_params.multi_items())
        query = dict(pairs)
        binding = login.bindings.get(query.get("session_id"))
        if len(pairs) != len(query) or binding is None:
            raise AppError("permission_denied", 403)
        try:
            binding.validate(login.actor, login.id, query)
        except ValueError:
            raise AppError("permission_denied", 403) from None
        if query.get("status") in {"denied", "failed"}:
            login.bindings.pop(binding.session_id, None)
            login.notice = "Authorization was cancelled or did not complete. No account was added."
            return RedirectResponse("/", status_code=303)
        try:
            code = binding.consume(login.actor, login.id, query)
        except ValueError:
            raise AppError("permission_denied", 403) from None
        try:
            connected = await desk.ravn(
                login,
                "POST",
                f"/v1/connect-sessions/{binding.session_id}/complete",
                {"completion_code": code},
            )
            connection_id = connected["id"]
        except AppError:
            # Read-only recovery: never replay one-time completion blindly.
            recovered = await desk.ravn(login, "GET", f"/v1/connect-sessions/{binding.session_id}")
            if recovered["status"] != "completed":
                login.notice = "Connection was not completed. Please start authorization again."
                return RedirectResponse("/", status_code=303)
            connection_id = recovered["connection_id"]
        finally:
            login.bindings.pop(binding.session_id, None)
        login.runtimes.pop(connection_id, None)  # Reconnect changes this connection's authority.
        login.notice = "Account connected. Your assistant can now use its approved read tools."
        return RedirectResponse("/", status_code=303)

    @app.post("/api/run")
    async def run(request: Request, body: RunBody):
        return await desk.execute(desk.login(request), body)

    @app.post("/api/sessions/{sid}/revoke")
    async def revoke(request: Request, sid: str):
        login = desk.login(request)
        if not re.fullmatch(r"sess_[a-f0-9]{32}", sid):
            raise AppError("not_found", 404)
        await desk.ravn(login, "POST", f"/v1/sessions/{sid}/revoke")
        login.runtimes = {cid: r for cid, r in login.runtimes.items() if r["id"] != sid}
        return {"ok": True}

    @app.post("/api/connections/{cid}/disconnect")
    async def disconnect(request: Request, cid: str):
        login = desk.login(request)
        if not re.fullmatch(r"conn_[a-f0-9]{32}", cid):
            raise AppError("not_found", 404)
        await desk.ravn(login, "POST", f"/v1/connections/{cid}/disconnect")
        login.runtimes.pop(cid, None)
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(request: Request):
        desk.login(request)
        desk.logins.pop(hashed(request.cookies.get(COOKIE, "")), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    return app
