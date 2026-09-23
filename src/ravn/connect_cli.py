"""Single-use loopback onboarding helper; not a remote user-login service."""

import asyncio
import contextlib
import hmac
import secrets
import socket
import webbrowser
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Request
from starlette.responses import HTMLResponse, RedirectResponse

from ravn.common import RavnError, digest
from ravn.onboarding_client import ConnectBinding


async def connect(config, args, backend_request):
    url = urlsplit(args.return_url)
    if (
        url.scheme != "http"
        or url.hostname != "127.0.0.1"
        or not url.port
        or url.path != "/return"
        or url.query
        or url.fragment
        or url.username
        or url.password
    ):
        raise ValueError("CLI return URL must be http://127.0.0.1:PORT/return")
    if url.port == int(config.server.listen.rsplit(":", 1)[1]):
        raise ValueError("CLI callback needs its own port")
    # Bind before creating the transaction; never hand a live flow to an occupied port.
    listener = socket.socket()
    listener.bind(("127.0.0.1", url.port))
    listener.listen(16)
    listener.setblocking(False)
    login, ticket = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    actor = ("cli", args.tenant or "default", args.user)
    binding = ConnectBinding.for_login(actor, login)
    cookie_name = "ravn_connect_" + secrets.token_hex(8)
    done = asyncio.get_running_loop().create_future()
    used_ticket = False
    flow = None
    server = None
    server_task = None
    try:
        payload = {
            "integration_id": args.integration,
            "return_url": args.return_url,
            "app_state": binding.app_state,
        }
        if args.reconnect:
            payload["reconnect_connection_id"] = args.reconnect
        flow = await backend_request(config, args, "POST", "/v1/connect-sessions", payload)
        binding.session_id = flow["id"]
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.middleware("http")
        async def local_boundary(request, next_call):
            pairs = list(request.query_params.multi_items())
            cookie_names = [
                c.strip().split("=", 1)[0]
                for c in request.headers.get("cookie", "").split(";")
                if c.strip()
            ]
            if (
                request.headers.get("host") != url.netloc
                or len(pairs) != len(dict(pairs))
                or len(cookie_names) != len(set(cookie_names))
                or len(str(request.url)) > 8192
                or any(len(request.headers.getlist(h)) > 1 for h in ("host", "cookie", "origin"))
                or request.headers.get("origin", "http://" + url.netloc) != "http://" + url.netloc
            ):
                response = HTMLResponse("Invalid browser request.", status_code=400)
            else:
                response = await next_call(request)
            response.headers.update(
                {
                    "Cache-Control": "no-store",
                    "Referrer-Policy": "no-referrer",
                    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
                    "X-Content-Type-Options": "nosniff",
                }
            )
            return response

        @app.get("/start/{value}")
        async def start(value: str):
            nonlocal used_ticket
            if used_ticket or not hmac.compare_digest(digest(value), digest(ticket)):
                return HTMLResponse("Link already used or invalid.", status_code=400)
            used_ticket = True
            response = RedirectResponse(flow["authorization_url"], status_code=303)
            response.set_cookie(cookie_name, login, max_age=600, httponly=True, samesite="lax")
            return response

        @app.get("/return")
        async def returned(request: Request):
            try:
                binding.validate(
                    actor, request.cookies.get(cookie_name, ""), dict(request.query_params)
                )
                if request.query_params.get("status") in {"denied", "failed"}:
                    binding.consumed = True
                    error = ValueError("Provider authorization did not complete")
                    if request.query_params["status"] == "failed":
                        failure_code = "connection_failed"
                        with contextlib.suppress(Exception):
                            status = await backend_request(
                                config, args, "GET", f"/v1/connect-sessions/{flow['id']}"
                            )
                            failure_code = status.get("failure_code", failure_code)
                        error = RavnError(
                            502,
                            failure_code,
                            "Connection failed. Check the RAVN server log for the failed stage and provider error code.",
                        )
                    if not done.done():
                        done.set_exception(error)
                    return HTMLResponse(
                        "Authorization did not complete. Return to the terminal.", status_code=400
                    )
                code = binding.consume(
                    actor, request.cookies.get(cookie_name, ""), dict(request.query_params)
                )
            except ValueError:
                return HTMLResponse(
                    "Authorization was denied, expired, or does not match this browser. Return to the terminal and cancel or start again.",
                    status_code=400,
                )
            try:
                result = await backend_request(
                    config,
                    args,
                    "POST",
                    f"/v1/connect-sessions/{flow['id']}/complete",
                    {"completion_code": code, "label": getattr(args, "label", None)},
                )
            except Exception:
                # Recovery is a read, never replay the completion or OAuth exchange.
                try:
                    status = await backend_request(
                        config, args, "GET", f"/v1/connect-sessions/{flow['id']}"
                    )
                    if status["status"] != "completed":
                        raise ValueError("Completion did not commit")
                    result = await backend_request(
                        config, args, "GET", f"/v1/connections/{status['connection_id']}"
                    )
                except Exception:
                    if not done.done():
                        done.set_exception(
                            ValueError("Completion failed; inspect connect-session status")
                        )
                    return HTMLResponse(
                        "Connection could not be completed. Check the terminal.", status_code=502
                    )
            if not done.done():
                done.set_result(result)
            response = HTMLResponse(
                "<h1>Account connected</h1><p>You can close this tab and return to the terminal.</p>"
            )
            response.delete_cookie(cookie_name)
            return response

        server = uvicorn.Server(
            uvicorn.Config(
                app,
                access_log=False,
                log_level="critical",
                proxy_headers=False,
                lifespan="off",
                timeout_graceful_shutdown=5,
            )
        )
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        async with asyncio.timeout(10):
            while not server.started:
                if server_task.done():
                    server_task.result()
                    raise ValueError("Callback listener did not start")
                await asyncio.sleep(0.02)
        local_url = "http://" + url.netloc + "/start/" + ticket
        print(f"Waiting for browser authorization (10 minutes). Connect session: {flow['id']}")
        if args.no_open or not webbrowser.open(local_url):
            print(local_url)
        async with asyncio.timeout(600):
            return await done
    finally:
        if flow and (not done.done() or done.cancelled() or done.exception() is not None):
            with contextlib.suppress(Exception):
                await backend_request(
                    config, args, "POST", f"/v1/connect-sessions/{flow['id']}/cancel"
                )
        if server:
            server.should_exit = True
        if server_task:
            await server_task
        listener.close()
