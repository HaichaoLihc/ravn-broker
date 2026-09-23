"""Read MCP/OAuth metadata without credentials, using the broker's bounded transport."""

import asyncio
from urllib.parse import urlsplit

import httpx2
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    extract_resource_metadata_from_www_auth,
    extract_scope_from_www_auth,
)
from mcp.shared.auth_utils import check_resource_allowed
from pydantic import field_validator

from ravn.common import RavnError, strict_json
from ravn.config import Model, OAuthSettings, https_endpoint
from ravn.provider import PinnedTransport

LIMIT = 65536


class DiscoveryRequest(Model):
    endpoint: str

    _endpoint = field_validator("endpoint")(https_endpoint)


def strings(document, key, default=()):
    value = document.get(key, list(default))
    if (
        not isinstance(value, list)
        or len(value) > 256
        or any(not isinstance(item, str) or not item or len(item) > 2048 for item in value)
    ):
        raise ValueError("Invalid metadata list")
    return value


class Discovery:
    def __init__(self, transport_factory=None):
        self.transport_factory = transport_factory or (lambda host: PinnedTransport(host, LIMIT))
        self.active = 0

    async def fetch(self, url, *, probe=False):
        https_endpoint(url)
        async with httpx2.AsyncClient(
            transport=self.transport_factory(urlsplit(url).hostname),
            trust_env=False,
            follow_redirects=False,
            timeout=5,
        ) as client:
            async with client.stream(
                "GET", url, headers={"Accept": "application/json", "Accept-Encoding": "identity"}
            ) as response:
                if probe:
                    # A streamable HTTP server may leave GET open. Only its challenge is needed.
                    return response
                if response.status_code in {404, 405}:
                    return None
                if response.status_code != 200:
                    raise ValueError("Metadata request failed")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > LIMIT:
                        raise ValueError("Oversized metadata")
                value = strict_json(bytes(content))
                if not isinstance(value, dict):
                    raise ValueError("Metadata must be an object")
                return value

    async def first(self, urls):
        for url in dict.fromkeys(urls):
            value = await self.fetch(url)
            if value is not None:
                return value
        raise RavnError(
            422,
            "discovery_unavailable",
            "No authorization metadata found. Use manual OAuth configuration or import a bearer token.",
        )

    async def inspect(self, endpoint):
        if self.active >= 2:
            raise RavnError(429, "rate_limited", "Another discovery is running; try again shortly.")
        self.active += 1
        try:
            async with asyncio.timeout(20):
                return await self.discover(endpoint)
        except RavnError:
            raise
        except Exception:
            raise RavnError(
                422,
                "discovery_failed",
                "Could not validate authorization metadata. Check the public HTTPS MCP address, or configure OAuth manually.",
            ) from None
        finally:
            self.active -= 1

    async def discover(self, endpoint):
        response = await self.fetch(endpoint, probe=True)
        resource = await self.first(
            build_protected_resource_metadata_discovery_urls(
                extract_resource_metadata_from_www_auth(response), endpoint
            )
        )
        target = https_endpoint(resource["resource"])
        if not check_resource_allowed(endpoint, target):
            raise ValueError("Metadata describes a different resource")
        issuers = strings(resource, "authorization_servers")
        if len(issuers) != 1:
            raise RavnError(
                422,
                "unsupported_authorization",
                "This server does not advertise a single authorization server. Choose one using manual OAuth configuration.",
            )
        issuer = https_endpoint(issuers[0])
        metadata = await self.first(
            build_oauth_authorization_server_metadata_discovery_urls(issuer, endpoint)
        )
        if metadata.get("issuer") != issuer:
            raise ValueError("Authorization issuer mismatch")
        if (
            "S256" not in strings(metadata, "code_challenge_methods_supported")
            or ("code" not in strings(metadata, "response_types_supported"))
            or "authorization_code"
            not in strings(metadata, "grant_types_supported", ["authorization_code", "implicit"])
        ):
            raise RavnError(
                422,
                "unsupported_authorization",
                "This version requires OAuth authorization code with S256 PKCE. This server does not advertise support.",
            )
        supported = strings(
            metadata, "token_endpoint_auth_methods_supported", ["client_secret_basic"]
        )
        methods = [
            m for m in ("client_secret_post", "client_secret_basic", "none") if m in supported
        ]
        if not methods:
            raise RavnError(
                422,
                "unsupported_authorization",
                "This server requires an unsupported client authentication method.",
            )
        # Resource scopes take precedence; authorization servers may serve several resources.
        scopes = (
            strings(resource, "scopes_supported")
            if "scopes_supported" in resource
            else strings(metadata, "scopes_supported")
        )
        required = (extract_scope_from_www_auth(response) or "").split()
        settings = OAuthSettings(
            client_id="pending",
            authorization_endpoint=metadata["authorization_endpoint"],
            token_endpoint=metadata["token_endpoint"],
            issuer=issuer,
            resource=target,
            scopes=required,
            token_endpoint_auth_method=methods[0],
        )
        return {
            "endpoint": endpoint,
            "oauth": settings.model_dump(exclude={"client_id"}),
            "supported_scopes": scopes,
            "authentication_methods": methods,
            "registration": "pre_registered",
        }
