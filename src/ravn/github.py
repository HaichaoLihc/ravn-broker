"""Fixed GitHub destinations and the official MCP client, with no credential passthrough."""

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

import httpx2
from jsonschema import Draft202012Validator
from mcp import Client, types
from mcp.client.streamable_http import streamable_http_client

from ravn.common import RavnError, canonical, strict_json
from ravn.config import Integration
from ravn.manifest import reviewed_tools


def provider_error(error):
    if isinstance(error, RavnError):
        return error
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            match = provider_error(child)
            if match:
                return match
    return None


class BoundedStream(httpx2.AsyncByteStream):
    def __init__(self, stream, limit: int):
        self.stream, self.limit = stream, limit

    async def __aiter__(self):
        total = 0
        async for chunk in self.stream:
            total += len(chunk)
            if total > self.limit:
                raise RavnError(502, "upstream_unavailable", "Upstream response exceeds the limit.")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class PinnedTransport(httpx2.AsyncBaseTransport):
    """Resolve public addresses once per request, connect to an IP, retain TLS SNI.

    A transport belongs to exactly one hostname, so IP pooling cannot accidentally
    reuse another origin's TLS connection. No environment proxies or HTTP redirects.
    """

    def __init__(self, host: str, limit: int):
        if host not in {
            "api.github.com",
            "api.githubcopilot.com",
            "github.com",
            "slack.com",
            "mcp.slack.com",
        }:
            raise ValueError("Unknown provider host")
        self.host, self.limit = host, limit
        self.inner = httpx2.AsyncHTTPTransport(trust_env=False, retries=0)

    async def handle_async_request(self, request):
        if (
            request.url.scheme != "https"
            or request.url.host != self.host
            or request.url.port not in {None, 443}
            or request.url.userinfo
        ):
            raise RavnError(502, "upstream_unavailable", "Unregistered upstream destination.")
        addresses = await asyncio.get_running_loop().getaddrinfo(
            self.host,
            443,
            type=socket.SOCK_STREAM,
        )
        ips = [address[4][0] for address in addresses]
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise RavnError(
                502, "upstream_unavailable", "Provider resolved to a prohibited address."
            )
        pinned = httpx2.Request(
            request.method,
            request.url.copy_with(host=ips[0]),
            headers={**dict(request.headers), "host": self.host, "accept-encoding": "identity"},
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": self.host},
        )
        response = await self.inner.handle_async_request(pinned)
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise RavnError(502, "upstream_unavailable", "Provider redirects are not permitted.")
        if response.headers.get("content-encoding", "identity") not in {"identity", ""}:
            await response.aclose()
            raise RavnError(
                502, "upstream_unavailable", "Compressed provider responses are unsupported."
            )
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=BoundedStream(response.stream, self.limit),
            extensions=response.extensions,
        )

    async def aclose(self):
        await self.inner.aclose()


class GitHub:
    host = "api.githubcopilot.com"
    endpoint = "https://api.githubcopilot.com/mcp/"
    label = "GitHub"
    extra_headers = {
        "X-MCP-Readonly": "true",
        "X-MCP-Toolsets": "issues",
        "X-MCP-Tools": "issue_read,list_issues",
    }

    def __init__(self, timeout=30, result_limit=4194304, transport_factory=None):
        self.timeout, self.result_limit = timeout, result_limit
        self.transport_factory = transport_factory or (
            lambda host: PinnedTransport(host, result_limit)
        )

    async def identify(self, credential: str) -> tuple[str, str]:
        try:
            async with (
                asyncio.timeout(10),
                httpx2.AsyncClient(
                    transport=self.transport_factory("api.github.com"),
                    trust_env=False,
                    follow_redirects=False,
                    timeout=10,
                ) as client,
            ):
                response = await client.get(
                    "https://api.github.com/user",
                    headers={
                        "Authorization": f"Bearer {credential}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2026-03-10",
                        "Accept-Encoding": "identity",
                    },
                )
                if response.status_code == 401:
                    raise RavnError(
                        401, "credential_invalid", "GitHub rejected the supplied credential."
                    )
                if response.status_code != 200:
                    raise RavnError(
                        502, "upstream_unavailable", "GitHub account verification failed."
                    )
                data = strict_json(response.content)
                account, login = data.get("id"), data.get("login")
                if type(account) is not int or account <= 0 or not isinstance(login, str):
                    raise ValueError("Invalid provider identity")
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login) or credential in login:
                    raise ValueError("Invalid provider login")
                return str(account), login
        except RavnError:
            raise
        except Exception:
            raise RavnError(
                502, "upstream_unavailable", "GitHub account verification failed."
            ) from None

    @asynccontextmanager
    async def client(self, credential: str):
        async def check_auth(response):
            if response.status_code == 401:
                raise RavnError(
                    401,
                    "credential_invalid",
                    "Provider rejected the account credential; reconnect.",
                )

        async with httpx2.AsyncClient(
            transport=self.transport_factory(self.host),
            trust_env=False,
            follow_redirects=False,
            timeout=self.timeout,
            event_hooks={"response": [check_auth]},
            headers={
                "Authorization": f"Bearer {credential}",
                **self.extra_headers,
                "Accept-Encoding": "identity",
            },
        ) as http_client:
            async with Client(
                streamable_http_client(self.endpoint, http_client=http_client),
                cache=None,
                read_timeout_seconds=self.timeout,
            ) as client:
                yield client

    async def catalogue(self, client) -> list[types.Tool]:
        tools, seen, cursor = [], set(), None
        for _ in range(10):
            page = await client.list_tools(cursor=cursor)
            tools.extend(page.tools)
            if len(tools) > 100:
                raise RavnError(502, "schema_rejected", "Upstream catalogue exceeds the limit.")
            cursor = page.next_cursor
            if cursor is None:
                return tools
            if cursor in seen or len(cursor) > 2048:
                break
            seen.add(cursor)
        raise RavnError(502, "schema_rejected", "Invalid upstream catalogue pagination.")

    async def inspect(self, credential: str) -> list[types.Tool]:
        async with asyncio.timeout(self.timeout), self.client(credential) as client:
            return await self.catalogue(client)

    async def discover(self, credential: str, integration: Integration) -> list[types.Tool]:
        try:
            return reviewed_tools(
                await self.inspect(credential), integration.schema_hashes, integration.connector
            )
        except RavnError:
            raise
        except Exception as error:
            if match := provider_error(error):
                raise match from None
            raise RavnError(
                502, "upstream_unavailable", f"{self.label} tool discovery failed."
            ) from None

    async def call(
        self,
        credential: str,
        integration: Integration,
        name: str,
        arguments: dict,
        before_dispatch: Callable[[], Awaitable[None]],
    ) -> types.CallToolResult:
        try:
            async with asyncio.timeout(self.timeout), self.client(credential) as client:
                upstream = await self.catalogue(client)
                reviewed = reviewed_tools(
                    upstream, integration.schema_hashes, integration.connector
                )
                if name not in {t.name for t in reviewed}:
                    raise RavnError(
                        403, "permission_denied", "Tool is unavailable or not approved."
                    )
                schema = next(t.input_schema for t in upstream if t.name == name)
                if next(Draft202012Validator(schema).iter_errors(arguments), None):
                    raise RavnError(
                        422, "invalid_arguments", "Arguments do not match the upstream schema."
                    )
                await before_dispatch()
                # The lower-level method exposes input_required without an automatic replay loop.
                result = await client.session.call_tool(name, arguments, allow_input_required=True)
                if not isinstance(result, types.CallToolResult) or result.result_type != "complete":
                    raise RavnError(
                        502,
                        "unsupported_upstream_interaction",
                        "Interactive upstream tools are unsupported.",
                    )
                raw = result.model_dump(mode="json", by_alias=True, exclude_none=True)
                if len(canonical(raw)) > self.result_limit:
                    raise RavnError(502, "upstream_unavailable", "Tool result exceeds the limit.")
                # Never return a known provider credential if an upstream echoes it.
                if credential in canonical(raw).decode():
                    raise RavnError(
                        502, "upstream_unavailable", "Unsafe upstream result was withheld."
                    )
                return result
        except RavnError:
            raise
        except Exception as error:
            if match := provider_error(error):
                raise match from None
            raise RavnError(
                502, "upstream_unavailable", f"{self.label} read operation failed."
            ) from None
