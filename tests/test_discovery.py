"""Discovery stays credential-free and only accepts bound, supported metadata."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import httpx2
import pytest

from ravn.common import RavnError
from ravn.console import PREFIX, create_console_app
from ravn.discovery import Discovery
from ravn.provider import PinnedTransport, RemoteMCP

pytestmark = pytest.mark.anyio
MCP = "https://mcp.records.example/mcp/"
RESOURCE = "https://mcp.records.example/.well-known/oauth-protected-resource"
ISSUER = "https://auth.example/tenant"
METADATA = "https://auth.example/.well-known/oauth-authorization-server/tenant"


def documents():
    return {
        MCP: httpx2.Response(
            401,
            headers={
                "WWW-Authenticate": f'Bearer resource_metadata="{RESOURCE}", scope="records.read"'
            },
        ),
        RESOURCE: {
            "resource": MCP,
            "authorization_servers": [ISSUER],
            "scopes_supported": ["records.read", "records.write"],
        },
        METADATA: {
            "issuer": ISSUER,
            "authorization_endpoint": "https://auth.example/authorize",
            "token_endpoint": "https://auth.example/token",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
            "scopes_supported": ["unrelated.scope"],
        },
    }


def discovery(docs, seen):
    def respond(request):
        seen.append(request)
        value = docs.get(str(request.url))
        if isinstance(value, httpx2.Response):
            return value
        return httpx2.Response(404 if value is None else 200, json=value)

    return Discovery(lambda host: httpx2.MockTransport(respond))


async def test_challenge_discovery_binds_resource_and_issuer_and_does_not_request_every_scope():
    seen = []
    result = await discovery(documents(), seen).inspect(MCP)
    assert [str(r.url) for r in seen] == [MCP, RESOURCE, METADATA]
    assert all("authorization" not in r.headers and "cookie" not in r.headers for r in seen)
    assert result["oauth"]["scopes"] == ["records.read"]
    assert result["supported_scopes"] == ["records.read", "records.write"]
    assert result["oauth"]["resource"] == MCP
    assert result["oauth"]["issuer"] == ISSUER
    assert result["authentication_methods"] == ["client_secret_post", "none"]
    assert "client_secret" not in result["oauth"]


async def test_path_root_and_oidc_fallbacks_with_no_challenge():
    docs = documents()
    docs[MCP] = httpx2.Response(405)
    del docs[RESOURCE]["scopes_supported"]
    oidc = "https://auth.example/tenant/.well-known/openid-configuration"
    docs[oidc] = docs.pop(METADATA)
    docs[oidc].pop("token_endpoint_auth_methods_supported")
    seen = []
    result = await discovery(docs, seen).inspect(MCP)
    assert [str(r.url) for r in seen] == [
        MCP,
        "https://mcp.records.example/.well-known/oauth-protected-resource/mcp/",
        RESOURCE,
        METADATA,
        "https://auth.example/.well-known/openid-configuration/tenant",
        oidc,
    ]
    assert result["oauth"]["token_endpoint_auth_method"] == "client_secret_basic"
    assert result["oauth"]["scopes"] == []
    assert result["supported_scopes"] == ["unrelated.scope"]


@pytest.mark.parametrize(
    "where,patch",
    [
        (RESOURCE, {"resource": "https://unrelated.example/mcp"}),
        (RESOURCE, {"authorization_servers": ["http://unsafe.example"]}),
        (RESOURCE, {"authorization_servers": []}),
        (RESOURCE, {"authorization_servers": [ISSUER, "https://second.example"]}),
        (METADATA, {"issuer": "https://different.example"}),
        (METADATA, {"token_endpoint": "https://auth.example/token?secret=do-not-echo"}),
        (METADATA, {"authorization_endpoint": "http://unsafe.example/authorize"}),
        (METADATA, {"code_challenge_methods_supported": []}),
        (METADATA, {"response_types_supported": ["token"]}),
        (METADATA, {"grant_types_supported": ["client_credentials"]}),
        (METADATA, {"token_endpoint_auth_methods_supported": ["private_key_jwt"]}),
        (METADATA, {"scopes_supported": "not-an-array"}),
    ],
)
async def test_mismatched_or_unsupported_metadata_is_not_accepted(where, patch):
    docs = documents()
    docs[where].update(patch)
    if "scopes_supported" in patch:
        docs[RESOURCE].pop("scopes_supported")
    with pytest.raises(RavnError) as error:
        await discovery(docs, []).inspect(MCP)
    assert "do-not-echo" not in str(error.value)
    assert error.value.status == 422


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(302, headers={"Location": "https://elsewhere.example"}),
        httpx2.Response(200, text="x" * 65537),
        httpx2.Response(200, text='{"resource":"one","resource":"two"}'),
        httpx2.Response(200, json=[]),
    ],
)
async def test_redirects_oversized_and_invalid_metadata_fail_closed(response):
    docs, seen = documents(), []
    docs[RESOURCE] = response
    with pytest.raises(RavnError):
        await discovery(docs, seen).inspect(MCP)
    assert len(seen) == 2


async def test_discovery_rejects_private_network_destinations(monkeypatch):
    monkeypatch.setattr(
        asyncio.get_running_loop(),
        "getaddrinfo",
        AsyncMock(return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]),
    )
    with pytest.raises(RavnError):
        await Discovery().inspect(MCP)


async def test_metadata_hosts_use_the_same_pinned_transport(monkeypatch):
    seen = []
    docs = documents()
    docs[RESOURCE]["authorization_servers"] = ["https://internal.example"]

    async def addresses(host, port, **kwargs):
        return [(2, 1, 6, "", ("10.0.0.1" if host == "internal.example" else "8.8.8.8", port))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", addresses)

    def factory(host):
        transport = PinnedTransport(host, 65536)

        def respond(request):
            seen.append(host)
            url = str(request.url.copy_with(host=host))
            value = docs.get(url)
            return value if isinstance(value, httpx2.Response) else httpx2.Response(200, json=value)

        transport.inner = httpx2.MockTransport(respond)
        return transport

    with pytest.raises(RavnError):
        await Discovery(factory).inspect(MCP)
    assert seen == ["mcp.records.example", "mcp.records.example"]


async def test_console_discovery_is_operator_only_and_does_not_register(rig, console):
    state, http = console
    rig.service.integrations.discovery = discovery(documents(), [])
    path = PREFIX + "/integrations/discover"
    before = rig.service.integrations.listing()
    result = await http.post(path, json={"endpoint": MCP})
    assert result.status_code == 200, result.text
    assert rig.service.integrations.listing() == before
    assert (
        await http.post(path, headers={"X-CSRF-Token": "wrong"}, json={"endpoint": MCP})
    ).status_code == 403
    assert (await http.post(path, json={"endpoint": "http://127.0.0.1"})).status_code == 400
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_console_app(state)), base_url=state.origin
    ) as anonymous:
        assert (
            await anonymous.post(path, headers={"Origin": state.origin}, json={"endpoint": MCP})
        ).status_code == 401
    registered = await http.post(
        PREFIX + "/integrations",
        json={
            "app_id": "demo",
            "id": "discovered",
            "endpoint": MCP,
            "oauth": {
                **result.json()["oauth"],
                "client_id": "client",
                "client_secret": "private-secret",
            },
        },
    )
    assert registered.status_code == 201, registered.text
    assert registered.json()["identity"] is None
    assert "private-secret" not in registered.text


async def test_token_import_without_identity_verifies_mcp_and_supports_sessions(rig, console):
    _, http = console
    await http.post(
        PREFIX + "/integrations", json={"app_id": "demo", "id": "plain", "endpoint": MCP}
    )
    item = rig.service.integrations.get("demo", "plain")
    rig.service.providers[("demo", "plain")] = RemoteMCP(item, transport_factory=rig.fake.transport)
    response = await rig.http.post(
        "/v1/connections/import",
        headers=rig.headers(),
        json={
            "integration_id": "plain",
            "credential": {"type": "bearer", "token": "test-token"},
            "label": "Work account",
        },
    )
    assert response.status_code == 201, response.text
    conn = response.json()
    assert rig.fake.headers  # tools/list was reached with the imported credential.
    assert conn["provider_account_id"] is None and not conn["reconnect_supported"]
    assert conn["display_name"] == "Work account"
    session = await rig.session(conn)
    async with rig.mcp(session) as client:
        assert (await client.list_tools()).tools

    async def deny(*args):
        raise RavnError(401, "credential_invalid", "Rejected")

    rig.service.providers[("demo", "plain")].inspect = deny
    response = await rig.http.post(
        "/v1/connections/import",
        headers=rig.headers(),
        json={
            "integration_id": "plain",
            "credential": {"type": "bearer", "token": "bad-token"},
        },
    )
    assert response.status_code == 401
    assert len((await rig.http.get("/v1/connections", headers=rig.headers())).json()["data"]) == 1
