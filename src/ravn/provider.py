"""Configured remote MCP destinations, bounded transport, and provider identity lookup."""

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx2
from jsonschema import Draft202012Validator
from mcp import Client, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from ravn.catalogue import validate_tools
from ravn.common import RavnError, canonical, strict_json
from ravn.config import IntegrationSettings

logger = logging.getLogger(__name__)


def failure_kind(error):
    """Diagnostic structure only: peer messages, data, and URLs may contain secrets."""
    if isinstance(error, BaseExceptionGroup):
        return [failure_kind(child) for child in error.exceptions]
    if isinstance(error, MCPError):
        return f"MCPError({error.code})"
    if isinstance(error, httpx2.HTTPStatusError):
        return f"HTTPStatusError({error.response.status_code})"
    return type(error).__name__


def provider_error(error):
    if isinstance(error, RavnError):
        return error
    if isinstance(error, MCPError):
        return RavnError(
            502,
            "provider_protocol_error",
            f"Provider rejected an MCP request (JSON-RPC {error.code}). Check the service's MCP access settings and protocol support.",
        )
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


class RemoteMCP:
    def __init__(
        self,
        integration: IntegrationSettings,
        timeout=30,
        result_limit=4194304,
        transport_factory=None,
    ):
        self.integration = integration
        self.host = urlsplit(integration.endpoint).hostname
        self.endpoint = integration.endpoint
        self.timeout, self.result_limit = timeout, result_limit
        self.transport_factory = transport_factory or (
            lambda host: PinnedTransport(host, result_limit)
        )

    async def identify(self, credential: str) -> tuple[str, str]:
        identity = self.integration.identity
        if identity is None:
            # Verify access to the actual MCP service without inventing an account identity.
            await self.discover(credential, self.integration)
            return "", self.integration.id
        try:
            async with (
                asyncio.timeout(10),
                httpx2.AsyncClient(
                    transport=self.transport_factory(urlsplit(identity.endpoint).hostname),
                    trust_env=False,
                    follow_redirects=False,
                    timeout=10,
                ) as client,
            ):
                response = await client.get(
                    identity.endpoint,
                    headers={
                        "Authorization": f"Bearer {credential}",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                )
            if response.status_code == 401:
                raise RavnError(
                    401, "credential_invalid", "Provider rejected the supplied credential."
                )
            if response.status_code != 200 or len(response.content) > 65536:
                raise ValueError("Invalid identity response")
            data = strict_json(response.content)
            if not isinstance(data, dict):
                raise ValueError("Invalid identity response")
            for name, expected in identity.required_claims.items():
                if type(data.get(name)) is not type(expected) or data[name] != expected:
                    raise ValueError("Required identity claim is missing")
            account, display = data.get(identity.id_field), data.get(identity.display_field)
            if type(account) is int and account > 0:
                account = str(account)
            for value in (account, display):
                if (
                    not isinstance(value, str)
                    or not value.strip()
                    or len(value) > 255
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)
                    or credential in value
                ):
                    raise ValueError("Invalid provider identity")
            return account, display
        except RavnError:
            raise
        except Exception:
            raise RavnError(
                502, "upstream_unavailable", "Provider account verification failed."
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
            if response.status_code == 403:
                raise RavnError(
                    403,
                    "provider_denied",
                    "Provider refused this operation; check granted permissions and setup.",
                )

        async with httpx2.AsyncClient(
            transport=self.transport_factory(self.host),
            trust_env=False,
            follow_redirects=False,
            timeout=self.timeout,
            event_hooks={"response": [check_auth]},
            headers={
                "Authorization": f"Bearer {credential}",
                "Accept-Encoding": "identity",
            },
        ) as http_client:
            async with Client(
                streamable_http_client(self.endpoint, http_client=http_client),
                cache=None,
                read_timeout_seconds=self.timeout,
            ) as client:
                yield client

    async def catalogue(self, client, credential: str) -> list[types.Tool]:
        tools, seen, cursor = [], set(), None
        for _ in range(10):
            page = await client.list_tools(cursor=cursor)
            tools.extend(page.tools)
            if len(tools) > 100:
                raise RavnError(502, "schema_rejected", "Upstream catalogue exceeds the limit.")
            cursor = page.next_cursor
            if cursor is None:
                raw = canonical(
                    [
                        tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                        for tool in tools
                    ]
                )
                if len(raw) > self.result_limit or credential in raw.decode():
                    raise RavnError(
                        502,
                        "schema_rejected",
                        "Unsafe or oversized provider catalogue was withheld.",
                    )
                return validate_tools(tools)
            if cursor in seen or len(cursor) > 2048:
                break
            seen.add(cursor)
        raise RavnError(502, "schema_rejected", "Invalid upstream catalogue pagination.")

    async def inspect(self, credential: str) -> list[types.Tool]:
        async with asyncio.timeout(self.timeout), self.client(credential) as client:
            return await self.catalogue(client, credential)

    async def discover(self, credential: str, integration: IntegrationSettings) -> list[types.Tool]:
        try:
            return await self.inspect(credential)
        except RavnError:
            raise
        except Exception as error:
            logger.warning("MCP discovery failed: kind=%s", failure_kind(error))
            if match := provider_error(error):
                raise match from None
            raise RavnError(
                502, "upstream_unavailable", "Provider tool discovery failed."
            ) from None

    async def call(
        self,
        credential: str,
        integration: IntegrationSettings,
        name: str,
        arguments: dict,
        before_dispatch: Callable[[], Awaitable[None]],
        *,
        name_key=None,
    ) -> types.CallToolResult:
        try:
            async with asyncio.timeout(self.timeout), self.client(credential) as client:
                upstream = await self.catalogue(client, credential)
                matches = [
                    t for t in upstream if (name_key(t.name) if name_key else t.name) == name
                ]
                if len(matches) != 1:
                    raise RavnError(
                        403, "permission_denied", "Tool is not in the provider catalogue."
                    )
                tool = matches[0]
                schema = tool.input_schema
                if next(Draft202012Validator(schema).iter_errors(arguments), None):
                    raise RavnError(
                        422, "invalid_arguments", "Arguments do not match the upstream schema."
                    )
                await before_dispatch()
                # The lower-level method exposes input_required without an automatic replay loop.
                result = await client.session.call_tool(
                    tool.name, arguments, allow_input_required=True
                )
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
                502, "upstream_unavailable", "Provider tool operation failed."
            ) from None
