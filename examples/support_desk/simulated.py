"""Opt-in local provider simulator. Never used by the live application.

RAVN still executes its real OAuth, encrypted storage, authorization and MCP
paths. Only provider HTTP transports and their browser consent page are fake.
These schemas are fixtures, NOT pins approved for a real provider.
"""

import base64
import hashlib
import html
import json
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
from fastapi import FastAPI, Request
from mcp import types
from mcp.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)

from ravn.config import GMAIL_COMPOSE, GMAIL_READONLY
from ravn.manifest import GMAIL_SCHEMAS, SCHEMAS, SLACK_SCHEMAS
from ravn.oauth_provider import OAuthProvider

from .app import STATIC, hashed

# kind → (MCP host, MCP path, OAuth/identity hosts, schemas)
PROFILES = {
    "github": ("api.githubcopilot.com", "/mcp/", {"github.com", "api.github.com"}, SCHEMAS),
    "slack": ("mcp.slack.com", "/mcp", {"slack.com"}, SLACK_SCHEMAS),
    "gmail": (
        "gmailmcp.googleapis.com",
        "/mcp/v1",
        {"oauth2.googleapis.com", "openidconnect.googleapis.com"},
        GMAIL_SCHEMAS,
    ),
}
ACCESS_PREFIX = {"github": "ghu_", "slack": "xoxp-", "gmail": "ya29."}
# kind → (name, heading, account, what is granted) for the simulated consent page
CONSENT = {
    "github": (
        "GitHub",
        "Support Desk would like read access",
        "maya-demo · acme/help-center",
        "Read and list issues in the simulated repository. No write tools are exposed.",
    ),
    "slack": (
        "Slack",
        "Support Desk would like read access",
        "Acme demo workspace · Maya (TDEMO / UDEMO)",
        "Search public and private messages and read threads available to this simulated user. No write tools are exposed.",
    ),
    "gmail": (
        "Gmail",
        "Support Desk would like to read mail and create drafts",
        "maya@acme-demo.example (simulated Google account)",
        "Search and read mail, list labels and drafts, and save draft replies. Drafts are never sent, and no send tool exists.",
    ),
}
GMAIL_THREAD = "18f2a0c4d5e6f701"
GMAIL_MESSAGES = {
    "18f2a0c4d5e6f701": {
        "id": "18f2a0c4d5e6f701",
        "threadId": GMAIL_THREAD,
        "subject": "Refund for order #1042 has no confirmation",
        "sender": "dana@example.com",
        "toRecipients": ["support@acme-demo.example"],
        "date": "2026-09-10T16:04:00Z",
        "snippet": "I was told my refund was approved, but I never got a confirmation email.",
        "plaintextBody": "Hi Acme support,\n\nI was told my refund for order #1042 was approved, but I never got a confirmation email and it is not on the order page. Can you confirm it is on the way?\n\nThanks,\nDana",
        "labelIds": ["INBOX", "Label_7"],
    },
    "18f2a0c4d5e6f702": {
        "id": "18f2a0c4d5e6f702",
        "threadId": GMAIL_THREAD,
        "subject": "Re: Refund for order #1042 has no confirmation",
        "sender": "dana@example.com",
        "toRecipients": ["support@acme-demo.example"],
        "date": "2026-09-12T09:31:00Z",
        "snippet": "Just following up. Is there any update on the refund confirmation?",
        "plaintextBody": "Just following up. Is there any update on the refund confirmation for #1042?\n\nDana",
        "labelIds": ["INBOX", "UNREAD", "Label_7"],
    },
}
GMAIL_LABELS = [
    {"labelId": "INBOX", "name": "INBOX"},
    {"labelId": "Label_7", "name": "Support/Refunds", "threadsTotal": 1, "threadsUnread": 1},
]


class LossyTransport(httpx2.AsyncBaseTransport):
    """Test switch: a draft is created upstream, then its reply never arrives."""

    def __init__(self, provider):
        self.provider, self.inner = provider, httpx2.ASGITransport(app=provider.app)

    async def handle_async_request(self, request):
        calling = b"tools/call" in await request.aread()
        response = await self.inner.handle_async_request(request)
        if calling and self.provider.lose_response_after_draft:
            await response.aread()
            raise httpx2.ReadError("Simulated connection loss after dispatch", request=request)
        return response


class SimulatedProvider:
    def __init__(self, kind, secret):
        self.kind, self.secret = kind, secret
        self.tokens, self.refreshes, self.codes = set(), set(), {}
        self.calls = []
        self.drafts = []
        self.lose_response_after_draft = False
        self.mcp_host, path, self.auth_hosts, schemas = PROFILES[kind]
        self.tools = [types.Tool(name=n, inputSchema=s) for n, s in schemas.items()]
        if kind == "gmail":
            # Google's server also offers label writes that RAVN never reviews.
            self.tools += [
                types.Tool(name=name, inputSchema={"type": "object"})
                for name in ("label_thread", "create_label")
            ]
        self.server = Server(
            "simulated-" + kind, on_list_tools=self.list_tools, on_call_tool=self.call_tool
        )
        self.app = self.server.streamable_http_app(
            streamable_http_path=path,
            stateless_http=True,
            json_response=True,
            # In-memory ASGI transport only. This app never has a network listener.
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

    def authorized(self, ctx):
        return ctx.request.headers.get("authorization", "").removeprefix("Bearer ") in self.tokens

    async def list_tools(self, ctx, params):
        return types.ListToolsResult(tools=self.tools if self.authorized(ctx) else [])

    async def call_tool(self, ctx, params):
        if not self.authorized(ctx):
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="Simulated credential is not valid.")],
                isError=True,
            )
        self.calls.append(params.name)
        args = params.arguments or {}
        result = {"simulated": True, "provider": self.kind, "tool": params.name}
        error = False
        if self.kind == "gmail":
            error = self.gmail(params.name, args, result)
        elif self.kind == "github":
            if (
                args.get("owner") != "acme"
                or args.get("repo") != "help-center"
                or (params.name == "issue_read" and args.get("issue_number") != 42)
            ):
                result["message"] = (
                    "This simulation contains acme/help-center issue #42 only. No real GitHub request was made."
                )
                error = True
            else:
                issue = {
                    "number": 42,
                    "title": "Refund confirmation is missing from the order page",
                    "state": "open",
                    "author": "maya-demo",
                    "body": "The refund was approved, but the customer cannot see the confirmation. Support should check the refund discussion in Slack before replying.",
                    "labels": ["support", "billing"],
                }
                result["issue" if params.name == "issue_read" else "issues"] = (
                    issue if params.name == "issue_read" else [issue]
                )
        elif params.name == "slack_read_thread":
            if args.get("channel_id") != "C0123" or args.get("message_ts") != "1789142400.000100":
                result["message"] = (
                    "This simulation contains channel C0123, thread 1789142400.000100 only."
                )
                error = True
            else:
                result["messages"] = [
                    {
                        "user": "Maya",
                        "text": "Refund for order #1042 is approved. Confirmation email is delayed.",
                    },
                    {
                        "user": "Jules",
                        "text": "The email retry is queued. Please tell the customer their refund is processing.",
                    },
                ]
        else:
            query = args.get("query", "").strip().lower()
            result.update(
                query=args.get("query"),
                messages=[
                    {
                        "channel": "#customer-support",
                        "channel_id": "C0123",
                        "thread_ts": "1789142400.000100",
                        "user": "Maya",
                        "text": "Refund for order #1042 is approved. Confirmation email is delayed; see GitHub issue #42.",
                    }
                ]
                if query and any(word in query for word in ("refund", "1042", "confirmation"))
                else [],
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result))], isError=error
        )

    def gmail(self, name, args, result):
        """Fill result for one Gmail tool; return True for a tool-level error."""
        words = ("refund", "1042", "confirmation")
        if name == "search_threads":
            query = args.get("query", "").lower()
            first = GMAIL_MESSAGES["18f2a0c4d5e6f701"]
            result["threads"] = (
                [
                    {
                        "id": GMAIL_THREAD,
                        "subject": first["subject"],
                        "sender": first["sender"],
                        "snippet": first["snippet"],
                        "messageCount": len(GMAIL_MESSAGES),
                    }
                ]
                if any(word in query for word in words)
                else []
            )
            result["resultCountEstimate"] = len(result["threads"])
        elif name == "get_thread":
            if args.get("threadId") != GMAIL_THREAD:
                result["message"] = f"This simulation contains thread {GMAIL_THREAD} only."
                return True
            result["thread"] = {"id": GMAIL_THREAD, "messages": list(GMAIL_MESSAGES.values())}
        elif name == "get_message":
            if args.get("messageId") not in GMAIL_MESSAGES:
                result["message"] = "This simulation contains the two messages in its one thread."
                return True
            result["message"] = GMAIL_MESSAGES[args["messageId"]]
        elif name == "list_labels":
            result["labels"] = GMAIL_LABELS
        elif name == "list_drafts":
            query = args.get("query", "").lower()
            result["drafts"] = [
                d
                for d in self.drafts
                if not query or query in (d["subject"] + " " + d["plaintextBody"]).lower()
            ]
        elif name == "create_draft":
            reply_to = GMAIL_MESSAGES.get(args.get("replyToMessageId"))
            draft = {
                "id": "r-simulated-" + str(len(self.drafts) + 1),
                "subject": args.get("subject", ""),
                "threadId": reply_to["threadId"] if reply_to else None,
                "toRecipients": args.get("to", []),
                "ccRecipients": args.get("cc", []),
                "bccRecipients": args.get("bcc", []),
                "plaintextBody": args["body"],
                "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            self.drafts.append(draft)
            result.update(draft=draft, note="Draft saved. Nothing was sent.")
        return False

    def code(self, query):
        value = secrets.token_urlsafe(32)
        now = time.monotonic()
        self.codes = {k: v for k, v in self.codes.items() if v[1] > now}
        self.codes[hashed(value)] = (query, now + 300)
        return value

    def transport(self, host):
        if host == self.mcp_host:
            if self.kind == "gmail":
                return LossyTransport(self)
            return httpx2.ASGITransport(app=self.app)
        if host not in self.auth_hosts:
            raise ValueError("The simulator never permits external network access")

        async def request(req):
            if req.url.path in {"/login/oauth/access_token", "/api/oauth.v2.user.access", "/token"}:
                p = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
                valid = (
                    p.get("client_secret") == self.secret
                    and p.get("client_id") == "simulated-" + self.kind
                )
                refreshing = p.get("grant_type") == "refresh_token"
                if refreshing:
                    refresh = p.get("refresh_token")
                    valid = valid and refresh in self.refreshes
                    if self.kind != "gmail":  # Google keeps one stable refresh token.
                        self.refreshes.discard(refresh)
                else:
                    saved = self.codes.pop(hashed(p.get("code", "")), None)
                    valid = (
                        valid
                        and bool(saved)
                        and saved[1] > time.monotonic()
                        and p.get("redirect_uri") == saved[0]["redirect_uri"]
                    )
                    if valid and self.kind in {"github", "gmail"}:
                        challenge = (
                            base64.urlsafe_b64encode(
                                hashlib.sha256(p.get("code_verifier", "").encode()).digest()
                            )
                            .rstrip(b"=")
                            .decode()
                        )
                        valid = challenge == saved[0].get("code_challenge")
                if not valid:
                    return httpx2.Response(400, json={"error": "invalid_grant"})
                access = ACCESS_PREFIX[self.kind] + "simulated-" + secrets.token_urlsafe(24)
                self.tokens.add(access)
                if self.kind == "gmail":
                    value = {
                        "access_token": access,
                        "token_type": "Bearer",
                        "expires_in": 3599,
                        "scope": " ".join(
                            [
                                "openid",
                                "https://www.googleapis.com/auth/userinfo.email",
                                GMAIL_READONLY,
                                GMAIL_COMPOSE,
                            ]
                        ),
                    }
                    if not refreshing:
                        value["refresh_token"] = "simulated-refresh-" + secrets.token_urlsafe(24)
                        self.refreshes.add(value["refresh_token"])
                    return httpx2.Response(200, json=value)
                refresh = "simulated-refresh-" + secrets.token_urlsafe(24)
                self.refreshes.add(refresh)
                return httpx2.Response(
                    200,
                    json={
                        "ok": True,
                        "access_token": access,
                        "token_type": "user" if self.kind == "slack" else "bearer",
                        "refresh_token": refresh,
                        "expires_in": 3600,
                        "refresh_token_expires_in": 86400,
                        "scope": "search:read.public,search:read.private,channels:history,groups:history"
                        if self.kind == "slack"
                        else "",
                    },
                )
            if req.url.path not in {"/user", "/api/auth.test", "/v1/userinfo"}:
                return httpx2.Response(404)
            access = req.headers.get("authorization", "").removeprefix("Bearer ")
            if access not in self.tokens:
                return httpx2.Response(401, json={"ok": False, "error": "invalid_auth"})
            if self.kind == "gmail":
                return httpx2.Response(
                    200,
                    json={
                        "sub": "104200000000000000042",
                        "email": "maya@acme-demo.example",
                        "email_verified": True,
                    },
                )
            return httpx2.Response(
                200,
                json={
                    "ok": True,
                    "id": 101,
                    "login": "maya-demo",
                    "team_id": "TDEMO",
                    "user_id": "UDEMO",
                },
            )

        return httpx2.MockTransport(request)


class SimulatedOAuth(OAuthProvider):
    consent_origin: str

    def authorize_url(self, state, verifier):
        real_url = super().authorize_url(state, verifier)
        return (
            self.consent_origin
            + "/authorize/"
            + self.integration.id
            + "?"
            + urlsplit(real_url).query
        )


def consent_app(providers, origin, ravn_origin, app_id, app_origin):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    pending = {}

    @app.middleware("http")
    async def boundary(request, next_call):
        if request.headers.get("host") != urlsplit(origin).netloc or len(request.url.query) > 4096:
            return PlainTextResponse("Invalid local simulator request", status_code=403)
        response = await next_call(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                # Browsers also apply form-action to the 303 redirect chain.
                # Permit exactly the RAVN callback and customer app origins.
                "Content-Security-Policy": f"default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; form-action 'self' {ravn_origin} {app_origin}; frame-ancestors 'none'; base-uri 'none'",
            }
        )
        return response

    @app.get("/assets/app.css")
    async def style():
        return FileResponse(STATIC / "app.css")

    @app.get("/assets/consent.js")
    async def script():
        return FileResponse(STATIC / "consent.js")

    @app.get("/authorize/{kind}")
    async def authorize(kind: str, request: Request):
        query = dict(request.query_params)
        if (
            kind not in providers
            or query.get("redirect_uri") != ravn_origin + f"/oauth/callback/{app_id}/{kind}"
            or query.get("client_id") != "simulated-" + kind
            or not query.get("state")
            or len(query) != len(request.query_params.multi_items())
        ):
            return PlainTextResponse("Invalid simulated authorization", status_code=400)
        now = time.monotonic()
        for k in list(pending):
            if pending[k][2] <= now:
                del pending[k]
        if len(pending) >= 100:
            return PlainTextResponse("Too many simulated authorizations", status_code=429)
        ticket, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        pending[hashed(ticket)] = (kind, query, now + 300, hashed(browser))
        name, heading, account, access = CONSENT[kind]
        response = HTMLResponse(
            f'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Simulated {name} consent · Support Desk</title>
<link rel="stylesheet" href="/assets/app.css"><script defer src="/assets/consent.js"></script>
<body><header class="topbar"><span class="brand">Support Desk / local simulator</span></header>
<main><div class="eyebrow">SIMULATED PROVIDER · NO REAL ACCOUNT</div>
<div class="hero"><div><h1>Connect your<br>{name} context.</h1>
<p>This page stands in for {name}'s authorization screen.<br>It uses made-up accounts, credentials, and content.</p></div></div>
<section class="locked"><h2>{heading}</h2>
<p>Account: <strong>{account}</strong></p>
<p>{access}</p>
<p>Continue to test the real RAVN OAuth callback, encrypted credential storage, session creation, and MCP gateway. No request goes to the real {name} service.</p>
<form method="post" action="/consent"><input type="hidden" name="ticket" value="{html.escape(ticket, quote=True)}">
<button name="decision" value="allow" class="primary">Allow simulated access →</button>
<button name="decision" value="deny">Cancel</button></form>
<p id="consent-error" role="alert"></p></section>
<footer>This is a local testing fixture, not a provider login page.</footer></main></body></html>'''
        )
        response.set_cookie(
            "sim_consent_" + hashed(ticket)[:16],
            browser,
            httponly=True,
            samesite="lax",
            max_age=300,
            path="/consent",
        )
        return response

    @app.post("/consent")
    async def consent(request: Request):
        if (
            request.headers.get("origin") != origin
            or request.headers.get("content-type", "").split(";")[0]
            != "application/x-www-form-urlencoded"
        ):
            return PlainTextResponse("Invalid consent origin", status_code=403)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                return PlainTextResponse("Invalid consent", status_code=413)
        data = parse_qs(body.decode())
        if set(data) != {"ticket", "decision"} or any(len(v) != 1 for v in data.values()):
            return PlainTextResponse("Invalid consent", status_code=400)
        ticket = data["ticket"][0]
        saved = pending.get(hashed(ticket))
        cookie = "sim_consent_" + hashed(ticket)[:16]
        if (
            not saved
            or saved[2] <= time.monotonic()
            or saved[3] != hashed(request.cookies.get(cookie, ""))
            or data["decision"][0] not in {"allow", "deny"}
        ):
            return PlainTextResponse(
                "Consent expired. Start again from Support Desk.", status_code=403
            )
        pending.pop(hashed(ticket))
        kind, query, _, _ = saved
        params = {"state": query["state"]}
        if data["decision"][0] == "allow":
            params["code"] = providers[kind].code(query)
        else:
            params["error"] = "access_denied"
        response = RedirectResponse(
            query["redirect_uri"] + "?" + urlencode(params), status_code=303
        )
        if request.headers.get("accept") == "application/json":
            response = JSONResponse(
                {"redirect_url": query["redirect_uri"] + "?" + urlencode(params)}
            )
        response.delete_cookie(cookie, path="/consent")
        return response

    return app
