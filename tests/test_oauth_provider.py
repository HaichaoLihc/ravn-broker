"""Configured OAuth endpoints, opaque bearer tokens, and refresh behavior."""

import base64
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from ravn.common import RavnError
from ravn.integrations import IntegrationRegistration as Integration
from ravn.oauth_provider import OAuthProvider
from ravn.provider import RemoteMCP

pytestmark = pytest.mark.anyio


@pytest.fixture
def adapter():
    return Integration(
        id="custom",
        app_id="demo",
        endpoint="https://mcp.example/mcp",
        identity={"endpoint": "https://identity.example/userinfo"},
        oauth={
            "client_id": "custom-client",
            "client_secret": "client-secret-test",
            "authorization_endpoint": "https://auth.example/authorize",
            "token_endpoint": "https://auth.example/token",
            "issuer": "https://auth.example",
            "scopes": ["records.read", "https://example.org/scopes/write"],
            "authorization_params": {"access_type": "offline"},
        },
    )


def oauth(adapter, response, requests=None):
    def handler(request):
        if requests is not None:
            requests.append(request)
        return response

    provider = RemoteMCP(adapter, transport_factory=lambda host: httpx2.MockTransport(handler))
    return OAuthProvider(
        adapter,
        provider,
        "https://ravn.example/oauth/callback/demo/custom",
        client_secret=adapter.oauth.client_secret.get_secret_value()
        if adapter.oauth.client_secret
        else None,
    )


def token_response(**patch):
    return httpx2.Response(
        200,
        json={
            "access_token": "opaque-provider-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            **patch,
        },
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"access_token": "bad\nheader"},
        {"access_token": ""},
        {"access_token": None},
        {"token_type": "bot"},
        {"token_type": 1},
        {"expires_in": True},
        {"expires_in": -10},
        {"expires_in": None, "refresh_token": "refresh"},
        {"scope": "read\nwrite"},
    ],
)
async def test_rejects_malformed_token_responses(adapter, patch):
    with pytest.raises(RavnError) as error:
        await oauth(adapter, token_response(**patch)).exchange("code", "verifier")
    assert error.value.code == "oauth_exchange_failed"
    assert "opaque-provider-token" not in str(error.value)


def test_authorization_uses_configured_endpoints_scopes_and_pkce(adapter):
    url = oauth(adapter, None).authorize_url("state", "verifier")
    assert url.startswith("https://auth.example/authorize?")
    params = parse_qs(urlsplit(url).query)
    assert params["scope"] == [" ".join(adapter.oauth.scopes)]
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"] == ["state"] and params["access_type"] == ["offline"]
    assert "client_secret" not in params


@pytest.mark.parametrize("method", ["client_secret_post", "client_secret_basic"])
async def test_exchange_auth_and_verifier_do_not_leak_into_url(adapter, method):
    adapter = adapter.model_copy(
        update={"oauth": adapter.oauth.model_copy(update={"token_endpoint_auth_method": method})}
    )
    requests = []
    result = await oauth(adapter, token_response(scope="records.read"), requests).exchange(
        "code", "verifier"
    )
    req = requests[0]
    assert str(req.url) == "https://auth.example/token"
    body = parse_qs(req.content.decode())
    assert body["code_verifier"] == ["verifier"]
    if method == "client_secret_post":
        assert body["client_secret"] == ["client-secret-test"]
    else:
        assert "client_secret" not in body
        assert (
            base64.b64decode(req.headers["authorization"].removeprefix("Basic "))
            == b"custom-client:client-secret-test"
        )
    # Partial consent is recorded as granted, rather than widened to requested scopes.
    assert result["granted_scopes"] == ["records.read"]


async def test_refresh_preserves_stable_token_and_omitted_scopes(adapter):
    previous = {
        "refresh_token": "stable",
        "granted_scopes": ["records.read"],
        "refresh_expires_at": "2026-12-01T00:00:00Z",
    }
    result = await oauth(adapter, token_response()).refresh(previous)
    assert result["refresh_token"] == "stable"
    assert result["granted_scopes"] == ["records.read"]
    assert result["refresh_expires_at"] == previous["refresh_expires_at"]
    rotated = await oauth(adapter, token_response(refresh_token="new")).refresh(previous)
    assert rotated["refresh_token"] == "new"


async def test_rotating_profile_requires_replacement_refresh_token(adapter):
    adapter = adapter.model_copy(
        update={"oauth": adapter.oauth.model_copy(update={"rotating_refresh_tokens": True})}
    )
    with pytest.raises(RavnError):
        await oauth(adapter, token_response()).refresh({"refresh_token": "single-use"})


async def test_invalid_grant_requests_reconnect_without_diagnostics(adapter):
    with pytest.raises(RavnError) as error:
        await oauth(
            adapter,
            httpx2.Response(400, json={"error": "invalid_grant", "description": "echoed-secret"}),
        ).refresh({"refresh_token": "old"})
    assert error.value.code == "connection_reauth_required"
    assert "echoed-secret" not in str(error.value)


async def test_nonexpiring_token_without_refresh_is_supported(adapter):
    result = await oauth(adapter, token_response(expires_in=None, scope="")).exchange(
        "code", "verifier"
    )
    assert result["expires_at"] is None and result["refresh_token"] is None
    assert result["granted_scopes"] == []


async def test_missing_client_secret_rejected_before_request(adapter):
    adapter = adapter.model_copy(
        update={"oauth": adapter.oauth.model_copy(update={"client_secret": None})}
    )
    requests = []
    with pytest.raises(RavnError):
        await oauth(adapter, token_response(), requests).exchange("code", "verifier")
    assert requests == []


async def test_public_client_sends_no_secret_and_binds_resource(adapter):
    adapter = adapter.model_copy(
        update={
            "oauth": adapter.oauth.model_copy(
                update={
                    "token_endpoint_auth_method": "none",
                    "client_secret": None,
                    "resource": "https://mcp.example/mcp",
                }
            )
        }
    )
    requests = []
    provider = oauth(adapter, token_response(), requests)
    query = parse_qs(urlsplit(provider.authorize_url("state", "verifier")).query)
    assert query["resource"] == ["https://mcp.example/mcp"]
    await provider.exchange("code", "verifier")
    body = parse_qs(requests[0].content.decode())
    assert body["client_id"] == ["custom-client"]
    assert body["resource"] == ["https://mcp.example/mcp"]
    assert "client_secret" not in body and "authorization" not in requests[0].headers
