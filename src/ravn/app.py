"""FastAPI management and an official-SDK MCP app sharing one service instance."""

import asyncio
import re
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from mcp.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from pydantic import Field, SecretStr
from starlette.responses import JSONResponse, RedirectResponse

from ravn.applications import add_application_routes
from ravn.catalogue import tool_connection
from ravn.common import RavnError, identifier, strict_json
from ravn.config import Config, Model
from ravn.integrations import add_integration_routes
from ravn.service import MAX_SESSION_CONNECTIONS, Service

BODY_READ_TIMEOUT_SECONDS = 10
IDEMPOTENCY = re.compile(r"[A-Za-z0-9._:-]{8,128}")


class Credential(Model):
    type: Literal["bearer"]
    token: SecretStr = Field(min_length=1, max_length=16384)


class ImportRequest(Model):
    integration_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    credential: Credential
    label: str | None = Field(default=None, min_length=1, max_length=128)


class SessionRequest(Model):
    connection_ids: list[str] = Field(default_factory=list, max_length=MAX_SESSION_CONNECTIONS)
    ttl_seconds: int | None = Field(default=None, strict=True, ge=60, le=14400)


class ConnectRequest(Model):
    integration_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    return_url: str = Field(min_length=1, max_length=2048)
    app_state: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    reconnect_connection_id: str | None = Field(default=None, min_length=8, max_length=128)


class CompleteRequest(Model):
    completion_code: SecretStr = Field(min_length=32, max_length=512)
    label: str | None = Field(default=None, min_length=1, max_length=128)


class RevokeKeyRequest(Model):
    revoke_sessions: bool = False


def error_response(error: RavnError, request_id: str):
    headers = {"Cache-Control": "no-store", "X-Request-Id": request_id}
    if error.status == 401:
        headers["WWW-Authenticate"] = 'Bearer realm="ravn"'
    return JSONResponse(error.envelope(request_id), status_code=error.status, headers=headers)


class Boundary:
    """Auth on every MCP HTTP request; limits/strict parsing before framework decoding."""

    def __init__(self, app, service: Service, *, admin=False):
        self.app, self.service, self.admin = app, service, admin
        self.active = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = identifier("req")
        scope.setdefault("state", {})["request_id"] = request_id
        started = False

        async def safe_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message["headers"] = [
                    (k, v)
                    for k, v in message["headers"]
                    if k.lower() not in {b"cache-control", b"x-request-id"}
                ]
                message["headers"].extend(
                    [(b"cache-control", b"no-store"), (b"x-request-id", request_id.encode())]
                )
                if scope["path"].startswith("/oauth/"):
                    message["headers"].extend(
                        [
                            (b"referrer-policy", b"no-referrer"),
                            (b"x-content-type-options", b"nosniff"),
                            (
                                b"content-security-policy",
                                b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
                            ),
                        ]
                    )
            await send(message)

        admitted = False
        try:
            if self.active >= 64:
                raise RavnError(429, "rate_limited", "Too many in-flight requests.")
            self.active += 1
            admitted = True
            headers = {}
            sensitive = {
                b"authorization",
                b"host",
                b"content-length",
                b"content-type",
                b"origin",
                b"mcp-protocol-version",
                b"mcp-session-id",
                b"x-ravn-user-id",
                b"x-ravn-tenant-id",
                b"cookie",
                b"x-csrf-token",
                b"if-match",
            }
            if sum(len(k) + len(v) for k, v in scope["headers"]) > 16384:
                raise RavnError(400, "invalid_request", "Headers exceed the configured limit.")
            for key, value in scope["headers"]:
                key = key.lower()
                if key in headers and (key in sensitive or key.startswith(b"x-ravn-")):
                    raise RavnError(400, "invalid_request", "Repeated security-relevant header.")
                headers[key] = value.decode("latin-1")
            if not self.admin:
                public = urlsplit(self.service.config.server.public_url)
                port = self.service.config.server.listen.rsplit(":", 1)[1]
                allowed = {public.netloc.lower(), f"127.0.0.1:{port}", f"localhost:{port}"}
                if headers.get(b"host", "").lower() not in allowed:
                    raise RavnError(400, "invalid_request", "Unrecognized Host header.")
                if b"origin" in headers and headers[
                    b"origin"
                ] != self.service.config.server.public_url.rstrip("/"):
                    raise RavnError(
                        403, "permission_denied", "Browser cross-origin requests are unsupported."
                    )
            path = scope["path"]
            if path.startswith("/oauth/") and not self.service.ready:
                raise RavnError(503, "dependency_unavailable", "RAVN is not ready.")
            if path.startswith("/v1/") or path in {"/mcp", "/mcp/"} or self.admin:
                if not self.service.ready:
                    raise RavnError(503, "dependency_unavailable", "RAVN is not ready.")
                if not self.admin:
                    auth = headers.get(b"authorization", "")
                    if not auth.startswith("Bearer "):
                        raise RavnError(
                            401, "unauthenticated", "Bearer authentication is required."
                        )
                    if path.startswith("/v1/"):
                        if any(
                            k.startswith(b"x-ravn-")
                            and k not in {b"x-ravn-user-id", b"x-ravn-tenant-id"}
                            for k in headers
                        ):
                            raise RavnError(
                                400, "invalid_request", "Unsupported identity override."
                            )
                        principal = await self.service.authenticate_app(
                            auth[7:],
                            headers.get(b"x-ravn-user-id"),
                            headers.get(b"x-ravn-tenant-id"),
                        )
                    else:
                        if (
                            any(k.startswith(b"x-ravn-") for k in headers)
                            or b"mcp-session-id" in headers
                        ):
                            raise RavnError(
                                400,
                                "invalid_request",
                                "Runtime identity overrides and stateful sessions are unsupported.",
                            )
                        if headers.get(b"mcp-protocol-version", "2026-07-28") not in {
                            "2026-07-28",
                            "2025-11-25",
                        }:
                            raise RavnError(
                                400, "invalid_request", "Unsupported MCP protocol version."
                            )
                        principal = await self.service.authenticate_session(auth[7:])
                    scope["state"]["principal"] = principal
            body = bytearray()
            try:
                async with asyncio.timeout(BODY_READ_TIMEOUT_SECONDS):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        body.extend(message.get("body", b""))
                        if len(body) > self.service.config.limits.request_bytes:
                            raise RavnError(
                                413, "invalid_request", "Request body exceeds the configured limit."
                            )
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                raise RavnError(408, "invalid_request", "Request body read timed out.") from None
            if body:
                if headers.get(b"content-type", "").split(";")[0].strip() != "application/json":
                    raise RavnError(415, "invalid_request", "JSON content type is required.")
                try:
                    payload = strict_json(bytes(body))
                except (ValueError, UnicodeError, RecursionError):
                    raise RavnError(
                        400, "invalid_request", "Malformed or excessively nested JSON."
                    ) from None
                if not isinstance(payload, dict):
                    raise RavnError(400, "invalid_request", "A single JSON object is required.")
                if path == "/mcp" and payload.get("method") == "initialize":
                    params = payload.get("params", {})
                    if not isinstance(params, dict) or params.get("protocolVersion") not in {
                        "2025-11-25",
                        "2026-07-28",
                    }:
                        raise RavnError(400, "invalid_request", "Unsupported MCP protocol version.")
            sent = False

            async def replay_receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            await self.app(scope, replay_receive, safe_send)
        except RavnError as error:
            if started:
                raise
            await error_response(error, request_id)(scope, receive, send)
        except Exception:
            if started:
                raise
            await error_response(
                RavnError(503, "dependency_unavailable", "RAVN could not complete this request."),
                request_id,
            )(scope, receive, send)
        finally:
            if admitted:
                self.active -= 1


def attach_errors(app):
    @app.exception_handler(RavnError)
    async def ravn_error(request, error):
        return error_response(error, request.state.request_id)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Pydantic errors contain submitted values: never serialize error.errors().
        return error_response(
            RavnError(400, "invalid_request", "Invalid request fields or parameters."),
            request.state.request_id,
        )


def create_app(config: Config, *, provider=None) -> FastAPI:
    service = Service(config, provider)

    def principal(ctx):
        if ctx.request is None or not getattr(ctx.request.state, "principal", None):
            raise MCPError(-32000, "Runtime authentication is required.")
        return ctx.request.state.principal

    async def list_tools(ctx, params):
        try:
            if params and params.cursor:
                raise RavnError(
                    400, "invalid_cursor", "The broker catalogue has no continuation cursor."
                )
            p = principal(ctx)
            return await service.session_tools(p)
        except RavnError as error:
            raise MCPError(
                -32000, error.message, error.envelope(ctx.request.state.request_id)["error"]
            ) from None

    async def call_tool(ctx, params):
        try:
            if params.task or params.input_responses or params.request_state:
                raise RavnError(
                    400,
                    "unsupported_upstream_interaction",
                    "Only synchronous tool calls are supported.",
                )
            # A RAVN extension consumed here; providers only ever receive arguments.
            key = (params.meta or {}).get("ravn/idempotency-key")
            if key is not None and (not isinstance(key, str) or not IDEMPOTENCY.fullmatch(key)):
                raise RavnError(400, "invalid_request", "Malformed idempotency key.")
            p = principal(ctx)
            return await service.execute(
                p,
                tool_connection(params.name),
                params.name,
                params.arguments or {},
                ctx.request.state.request_id,
                idempotency_key=key,
                routed=True,
            )
        except RavnError as error:
            raise MCPError(
                -32000, error.message, error.envelope(ctx.request.state.request_id)["error"]
            ) from None

    mcp = Server("ravn", version="0.1.0", on_list_tools=list_tools, on_call_tool=call_tool)
    mcp_app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=config.limits.request_bytes,
        # Outer boundary validates exact Host/Origin values for both protocols.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def lifespan(app):
        await service.start()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await service.close()

    app = FastAPI(
        lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False
    )
    app.state.service = service
    app.add_middleware(Boundary, service=service)
    attach_errors(app)

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready():
        try:
            async with service.store.transaction() as db:
                await db.execute("SELECT 1")
            ok = service.ready
        except Exception:
            ok = False
        return JSONResponse({"status": "ok" if ok else "not_ready"}, status_code=200 if ok else 503)

    @app.post("/v1/connections/import", status_code=201)
    async def import_connection(request: Request, body: ImportRequest):
        return await service.import_connection(
            request.state.principal,
            body.integration_id,
            body.credential.token.get_secret_value(),
            body.label,
        )

    @app.post("/v1/connect-sessions", status_code=201)
    async def start_connect(request: Request, body: ConnectRequest):
        return await service.oauth.start(
            request.state.principal,
            body.integration_id,
            body.return_url,
            body.app_state,
            body.reconnect_connection_id,
        )

    @app.get("/v1/connect-sessions/{sid}")
    async def connect_status(request: Request, sid: str):
        return await service.oauth.status(request.state.principal, sid)

    @app.post("/v1/connect-sessions/{sid}/complete")
    async def complete_connect(request: Request, sid: str, body: CompleteRequest):
        return await service.oauth.complete(
            request.state.principal, sid, body.completion_code.get_secret_value(), body.label
        )

    @app.post("/v1/connect-sessions/{sid}/cancel")
    async def cancel_connect(request: Request, sid: str):
        return await service.oauth.cancel(request.state.principal, sid)

    @app.get("/oauth/start/{ticket}")
    async def oauth_start(ticket: str):
        if len(ticket) != 43:
            raise RavnError(400, "invalid_oauth_transaction", "Invalid authorization link.")
        sid, cookie, url = await service.oauth.browser_start(ticket)
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(
            service.oauth.cookie_name(sid),
            cookie,
            max_age=600,
            httponly=True,
            secure=config.server.public_url.startswith("https://"),
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/oauth/callback/{app_id}/{integration_id}")
    async def oauth_callback(request: Request, app_id: str, integration_id: str):
        pairs = list(request.query_params.multi_items())
        if (
            len(request.url.query) > 8192
            or len(pairs) != len(dict(pairs))
            or ("error" in request.query_params and "code" in request.query_params)
        ):
            raise RavnError(
                400, "invalid_oauth_transaction", "Repeated or invalid callback parameters."
            )
        cookie_names = [
            part.strip().split("=", 1)[0]
            for part in request.headers.get("cookie", "").split(";")
            if part.strip()
        ]
        if len(cookie_names) != len(set(cookie_names)):
            raise RavnError(400, "invalid_oauth_transaction", "Repeated browser cookie.")
        sid, url = await service.oauth.callback(
            app_id, integration_id, dict(pairs), request.cookies
        )
        response = RedirectResponse(url, status_code=303)
        response.delete_cookie(
            service.oauth.cookie_name(sid),
            path="/",
            secure=config.server.public_url.startswith("https://"),
            httponly=True,
            samesite="lax",
        )
        return response

    @app.get("/v1/connections")
    async def connections(request: Request, cursor: str | None = None, limit: int | None = None):
        return await service.page(request.state.principal, "connections", cursor, limit)

    @app.get("/v1/connections/{connection_id}")
    async def connection(request: Request, connection_id: str):
        result = await service.connection(request.state.principal, connection_id)
        return JSONResponse(result, headers={"ETag": f'"{result["revision"]}"'})

    @app.post("/v1/connections/{connection_id}/disconnect")
    async def disconnect(request: Request, connection_id: str):
        return await service.disconnect(request.state.principal, connection_id)

    @app.get("/v1/connections/{connection_id}/tool-schemas")
    async def inspect_connection(request: Request, connection_id: str):
        return await service.inspect_connection(request.state.principal, connection_id)

    @app.post("/v1/sessions", status_code=201)
    async def create_session(request: Request, body: SessionRequest):
        return await service.create_session(
            request.state.principal, body.connection_ids, body.ttl_seconds
        )

    @app.get("/v1/sessions")
    async def sessions(
        request: Request,
        cursor: str | None = None,
        limit: int | None = None,
        connection_id: str | None = None,
    ):
        return await service.page(request.state.principal, "sessions", cursor, limit, connection_id)

    @app.post("/v1/sessions/{session_id}/revoke")
    async def revoke_session(request: Request, session_id: str):
        return await service.revoke_session(request.state.principal, session_id)

    @app.get("/v1/sessions/{session_id}")
    async def session(session_id: str, request: Request):
        return await service.session(request.state.principal, session_id)

    @app.api_route(
        "/v1/sessions/{session_id}/connections/{connection_id}", methods=["PUT", "DELETE"]
    )
    async def session_connection(session_id: str, connection_id: str, request: Request):
        return await service.set_session_connection(
            request.state.principal,
            session_id,
            connection_id,
            request.method == "PUT",
            request.state.request_id,
        )

    @app.get("/v1/calls/{call_id}")
    async def call_status(request: Request, call_id: str):
        return await service.call_status(request.state.principal, call_id)

    # The SDK's own /mcp route remains /mcp; do not mount it at /mcp/mcp.
    app.mount("/", mcp_app)
    return app


def create_admin_app(service: Service, console=None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(Boundary, service=service, admin=True)
    attach_errors(app)
    add_integration_routes(app, "/admin/v1", service)
    add_application_routes(app, "/admin/v1", service)

    @app.post("/admin/v1/console-tickets", status_code=201)
    async def console_ticket():
        if console is None:
            raise RavnError(404, "not_found", "Console is disabled. Start ravn serve --console.")
        return console.ticket()

    @app.post("/admin/v1/app-keys/{key_id}/revoke")
    async def revoke_key(key_id: str, body: RevokeKeyRequest):
        return await service.revoke_key(key_id, body.revoke_sessions)

    return app
