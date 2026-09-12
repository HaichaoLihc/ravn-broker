"""Strict provider token parsing: only advertised user authority, never fallback."""

import asyncio

import httpx2
import pytest

from ravn.common import RavnError
from ravn.config import Integration
from ravn.github import GitHub
from ravn.oauth_provider import OAuthProvider

pytestmark = pytest.mark.anyio


@pytest.fixture
def adapter(tmp_path):
    secret = tmp_path / "client-secret"
    secret.write_text("client-secret-test")
    secret.chmod(0o600)
    integration = Integration(
        id="github",
        app_id="demo",
        oauth={
            "profile": "github_app_pkce",
            "client_id": "github-app-client",
            "client_secret_file": secret,
        },
    )
    return integration


@pytest.mark.parametrize(
    "patch",
    [
        {"access_token": "ghs_installation_not_user"},
        {"access_token": "gho_oauth_app_not_github_app"},
        {"access_token": "ghu_bad\nheader"},
        {"token_type": "bot"},
        {"expires_in": True},
        {"expires_in": -10},
        {"expires_in": None, "refresh_token": "rotatable-but-no-expiry"},
        {"scope": "client-secret-test\n"},
    ],
)
async def test_reject_malformed_or_broader_authority(adapter, patch):
    value = {"access_token": "ghu_user", "token_type": "bearer", "expires_in": 3600, **patch}
    provider = GitHub(
        transport_factory=lambda host: httpx2.MockTransport(
            lambda req: httpx2.Response(200, json=value)
        )
    )
    oauth = OAuthProvider(adapter, provider, "https://ravn.example/oauth/callback/demo/github")
    with pytest.raises(RavnError) as exc:
        await oauth.exchange("temporary-code", "pkce-verifier")
    assert "client-secret-test" not in str(exc.value) and "ghu_" not in str(exc.value)


async def test_rotating_response_must_supply_new_refresh(adapter):
    provider = GitHub(
        transport_factory=lambda host: httpx2.MockTransport(
            lambda req: httpx2.Response(
                200, json={"access_token": "ghu_new", "token_type": "bearer", "expires_in": 3600}
            )
        )
    )
    with pytest.raises(RavnError):
        await OAuthProvider(adapter, provider, "https://ravn.example/callback").refresh(
            {"refresh_token": "old-once-only"}
        )


async def test_nonexpiring_user_grant_and_invalid_grant(adapter):
    provider = GitHub(
        transport_factory=lambda host: httpx2.MockTransport(
            lambda req: httpx2.Response(
                200, json={"access_token": "ghu_user", "token_type": "bearer", "scope": ""}
            )
        )
    )
    oauth = OAuthProvider(adapter, provider, "https://ravn.example/callback")
    result = await oauth.exchange("code", "verifier")
    assert (
        result["refresh_token"] is None
        and result["expires_at"] is None
        and result["granted_scopes"] == []
    )
    provider.transport_factory = lambda host: httpx2.MockTransport(
        lambda req: httpx2.Response(
            400, json={"error": "invalid_grant", "description": "echoed-old-secret"}
        )
    )
    with pytest.raises(RavnError) as exc:
        await oauth.refresh({"refresh_token": "old"})
    assert exc.value.code == "connection_reauth_required" and "echoed" not in str(exc.value)


async def test_client_secret_file_permissions_enforced(adapter):
    await asyncio.to_thread(adapter.oauth.client_secret_file.chmod, 0o644)

    def unexpected(host):
        raise AssertionError("No provider request permitted with unsafe secret storage")

    with pytest.raises(RavnError):
        await OAuthProvider(
            adapter, GitHub(transport_factory=unexpected), "https://ravn.example/callback"
        ).exchange("code", "verifier")
