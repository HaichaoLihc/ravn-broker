"""Strict provider token parsing: only advertised user authority, never fallback."""

import asyncio
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from ravn.common import RavnError
from ravn.config import GMAIL_COMPOSE, GMAIL_READONLY, Integration
from ravn.github import GitHub
from ravn.gmail import Gmail
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


GOOGLE_SCOPES = ["openid", "email", GMAIL_READONLY, GMAIL_COMPOSE]
GRANTED = " ".join(
    ["openid", "https://www.googleapis.com/auth/userinfo.email", GMAIL_READONLY, GMAIL_COMPOSE]
)


@pytest.fixture
def google(tmp_path):
    secret = tmp_path / "google-secret"
    secret.write_text("google-secret-test")
    secret.chmod(0o600)
    return Integration(
        id="gmail",
        app_id="demo",
        connector="gmail",
        endpoint="https://gmailmcp.googleapis.com/mcp/v1",
        manifest="builtin:gmail-v1",
        oauth={
            "profile": "google_web_pkce",
            "client_id": "google-client",
            "client_secret_file": secret,
            "scopes": GOOGLE_SCOPES,
        },
    )


def google_oauth(google, respond, requests=None):
    def handler(request):
        if requests is not None:
            requests.append((str(request.url), parse_qs(request.content.decode())))
        return respond(request)

    provider = Gmail(transport_factory=lambda host: httpx2.MockTransport(handler))
    return OAuthProvider(google, provider, "https://ravn.example/oauth/callback/demo/gmail")


def token_response(**patch):
    value = {
        "access_token": "ya29.user",
        "token_type": "Bearer",
        "expires_in": 3599,
        "refresh_token": "1//stable",
        "scope": GRANTED,
    }
    return {k: v for k, v in {**value, **patch}.items() if v is not None}


def test_google_authorize_requests_offline_pkce_with_exact_scopes(google):
    url = google_oauth(google, None).authorize_url("state-value", "verifier-value")
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert (parts.scheme, parts.netloc, parts.path) == (
        "https",
        "accounts.google.com",
        "/o/oauth2/v2/auth",
    )
    assert query["scope"] == [" ".join(GOOGLE_SCOPES)]
    assert query["code_challenge_method"] == ["S256"] and query["code_challenge"][0]
    assert query["access_type"] == ["offline"] and query["prompt"] == ["consent"]
    assert query["include_granted_scopes"] == ["false"]


async def test_google_exchange_sends_verifier_in_body_and_keeps_url_scopes(google):
    requests = []
    oauth = google_oauth(google, lambda r: httpx2.Response(200, json=token_response()), requests)
    result = await oauth.exchange("code", "verifier-value")
    url, body = requests[0]
    assert url == "https://oauth2.googleapis.com/token"
    assert body["code_verifier"] == ["verifier-value"] and body["client_id"] == ["google-client"]
    assert result["refresh_token"] == "1//stable" and GMAIL_COMPOSE in result["granted_scopes"]


@pytest.mark.parametrize(
    "scope",
    [
        "openid https://www.googleapis.com/auth/userinfo.email " + GMAIL_READONLY,
        "openid email " + GMAIL_COMPOSE,
        None,
    ],
)
async def test_google_unticked_scope_refuses_connection(google, scope):
    oauth = google_oauth(google, lambda r: httpx2.Response(200, json=token_response(scope=scope)))
    with pytest.raises(RavnError) as exc:
        await oauth.exchange("code", "verifier")
    assert exc.value.code == "insufficient_scope" and "ya29" not in str(exc.value)


@pytest.mark.parametrize(
    "patch",
    [
        {"access_token": "ghu_not_google"},
        {"token_type": "user"},
        {"scope": GRANTED + "\n"},
        {"expires_in": None},
    ],
)
async def test_google_rejects_other_token_shapes(google, patch):
    oauth = google_oauth(google, lambda r: httpx2.Response(200, json=token_response(**patch)))
    with pytest.raises(RavnError) as exc:
        await oauth.exchange("code", "verifier")
    assert exc.value.code == "oauth_exchange_failed"


async def test_google_refresh_keeps_its_stable_refresh_token(google):
    oauth = google_oauth(
        google, lambda r: httpx2.Response(200, json=token_response(refresh_token=None))
    )
    previous = {"refresh_token": "1//stable", "refresh_expires_at": "2026-12-01T00:00:00.000000Z"}
    result = await oauth.refresh(previous)
    assert result["access_token"] == "ya29.user" and result["refresh_token"] == "1//stable"
    assert result["refresh_expires_at"] == previous["refresh_expires_at"]
    rotated = google_oauth(
        google, lambda r: httpx2.Response(200, json=token_response(refresh_token="1//new"))
    )
    assert (await rotated.refresh(previous))["refresh_token"] == "1//new"


async def test_client_secret_file_permissions_enforced(adapter):
    await asyncio.to_thread(adapter.oauth.client_secret_file.chmod, 0o644)

    def unexpected(host):
        raise AssertionError("No provider request permitted with unsafe secret storage")

    with pytest.raises(RavnError):
        await OAuthProvider(
            adapter, GitHub(transport_factory=unexpected), "https://ravn.example/callback"
        ).exchange("code", "verifier")
