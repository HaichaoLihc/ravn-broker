"""Google's hosted Gmail MCP server; one user's OAuth grant, never domain-wide delegation."""

import asyncio
import re

import httpx2

from ravn.common import RavnError, strict_json
from ravn.github import BoundedStream, GitHub, PinnedTransport

EMAIL = re.compile(r"[^@\s,;<>\"]{1,64}@[A-Za-z0-9.-]{1,253}")


class GoogleIdentity:
    """Shared identity check against Google's real userinfo endpoint.

    Both the hosted gmailmcp.googleapis.com connector and the local wrapper
    connector authenticate the same real Google OAuth grant, so both call
    this -- there is no separate, weaker identity path for the local one.
    """

    async def identify(self, credential: str) -> tuple[str, str]:
        try:
            async with (
                asyncio.timeout(10),
                httpx2.AsyncClient(
                    transport=self.transport_factory("openidconnect.googleapis.com"),
                    trust_env=False,
                    follow_redirects=False,
                    timeout=10,
                ) as client,
            ):
                response = await client.get(
                    "https://openidconnect.googleapis.com/v1/userinfo",
                    headers={
                        "Authorization": f"Bearer {credential}",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                )
            if response.status_code == 401:
                raise RavnError(
                    401, "credential_invalid", "Google rejected the supplied credential."
                )
            if response.status_code != 200:
                raise ValueError("Invalid identity")
            data = strict_json(response.content)
            # `sub` is Google's stable account ID; an address can be renamed.
            account, email = data.get("sub"), data.get("email")
            if (
                not isinstance(account, str)
                or not re.fullmatch(r"[A-Za-z0-9]{1,255}", account)
                or data.get("email_verified") is not True
                or not isinstance(email, str)
                or not EMAIL.fullmatch(email)
                or credential in email
            ):
                raise ValueError("Invalid identity")
            return account, email
        except RavnError:
            raise
        except Exception:
            raise RavnError(
                502, "upstream_unavailable", "Google account verification failed."
            ) from None


class Gmail(GoogleIdentity, GitHub):
    # Reuse the bounded remote-MCP mechanics, not GitHub authentication/headers.
    host = "gmailmcp.googleapis.com"
    endpoint = "https://gmailmcp.googleapis.com/mcp/v1"
    label = "Gmail"
    extra_headers = {}
    # Google answers missing scopes and disabled APIs with 403 before executing.
    denied_status = 403


class LoopbackTransport(httpx2.AsyncBaseTransport):
    """Plain HTTP to 127.0.0.1 only -- a development stand-in, not a hardened
    public-internet transport. Never reuse this for a real provider host.
    """

    def __init__(self, host: str, limit: int):
        if not re.fullmatch(r"127\.0\.0\.1:[1-9][0-9]{0,4}", host):
            raise ValueError("gmail_local only talks to a 127.0.0.1 port")
        self.host, self.limit = host, limit
        self.inner = httpx2.AsyncHTTPTransport(trust_env=False, retries=0)

    async def handle_async_request(self, request):
        if request.url.scheme != "http" or f"{request.url.host}:{request.url.port}" != self.host:
            raise RavnError(502, "upstream_unavailable", "Unregistered upstream destination.")
        response = await self.inner.handle_async_request(request)
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise RavnError(502, "upstream_unavailable", "Provider redirects are not permitted.")
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=BoundedStream(response.stream, self.limit),
            extensions=response.extensions,
        )

    async def aclose(self):
        await self.inner.aclose()


class GmailLocal(GoogleIdentity, GitHub):
    """examples/gmail_agent/mcp_server.py, a self-hosted wrapper over the real
    Gmail REST API. Identity still comes from Google's real userinfo endpoint
    (LoopbackTransport only covers the tool-call hop); the wrapper never sees
    or stores the underlying Google credential beyond forwarding one request.
    """

    host = "127.0.0.1:8790"
    endpoint = "http://127.0.0.1:8790/mcp"
    label = "Gmail (local)"
    extra_headers = {}
    denied_status = 403

    def __init__(self, timeout=30, result_limit=4194304, transport_factory=None):
        super().__init__(timeout, result_limit, transport_factory)
        if transport_factory is None:
            # Only the loopback tool-call hop uses LoopbackTransport; identify()
            # above still resolves and pins to Google's real, public host.
            self.transport_factory = lambda host: (
                LoopbackTransport(host, result_limit)
                if host == self.host
                else PinnedTransport(host, result_limit)
            )
