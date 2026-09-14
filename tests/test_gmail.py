"""Gmail's fixed provider profile: configuration, identity, and destinations."""

import httpx2
import pytest
from pydantic import ValidationError

from ravn.common import RavnError
from ravn.config import GMAIL_COMPOSE, GMAIL_READONLY, Integration
from ravn.github import PinnedTransport
from ravn.gmail import Gmail

pytestmark = pytest.mark.anyio
READ_SCOPES = ["openid", "email", GMAIL_READONLY]
PIN = "a" * 64


def integration(tmp_path, scopes=READ_SCOPES, **patch):
    secret = tmp_path / "google-secret"
    secret.write_text("google-client-secret")
    secret.chmod(0o600)
    value = {
        "id": "gmail",
        "app_id": "demo",
        "connector": "gmail",
        "endpoint": "https://gmailmcp.googleapis.com/mcp/v1",
        "manifest": "builtin:gmail-v1",
        "oauth": {
            "profile": "google_web_pkce",
            "client_id": "google-client",
            "client_secret_file": str(secret),
            "scopes": scopes,
        },
        **patch,
    }
    return Integration.model_validate(value)


@pytest.mark.parametrize(
    "scopes, patch, message",
    [
        (READ_SCOPES, {"endpoint": "https://mcp.slack.com/mcp"}, "fixed provider profile"),
        (READ_SCOPES, {"manifest": "builtin:slack-read-v1"}, "fixed provider profile"),
        ([*READ_SCOPES, "https://www.googleapis.com/auth/gmail.modify"], {}, "only gmail.compose"),
        ([*READ_SCOPES, "https://mail.google.com/"], {}, "only gmail.compose"),
        (["openid", "email"], {}, "Gmail needs"),
        (READ_SCOPES, {"schema_hashes": {"create_draft": PIN}}, "requires the gmail.compose"),
        (READ_SCOPES, {"schema_hashes": {"label_thread": PIN}}, "reviewed tools"),
    ],
)
def test_gmail_config_rejects_other_authority(tmp_path, scopes, patch, message):
    with pytest.raises(ValidationError, match=message):
        integration(tmp_path, scopes, **patch)


def test_gmail_oauth_profile_must_match(tmp_path):
    value = integration(tmp_path).model_dump(mode="json")
    value["oauth"]["profile"] = "github_app_pkce"
    with pytest.raises(ValidationError, match="OAuth profile must match"):
        Integration.model_validate(value)


def test_compose_may_be_granted_before_draft_review(tmp_path):
    integration(tmp_path, [*READ_SCOPES, GMAIL_COMPOSE])
    approved = integration(
        tmp_path, [*READ_SCOPES, GMAIL_COMPOSE], schema_hashes={"create_draft": PIN}
    )
    assert approved.schema_hashes == {"create_draft": PIN}


@pytest.mark.parametrize(
    "status, body, code",
    [
        (401, {"error": "invalid_token"}, "credential_invalid"),
        (200, {"sub": "1098", "email": "dana@example.com", "email_verified": False}, "upstream"),
        (200, {"sub": "10 98", "email": "dana@example.com", "email_verified": True}, "upstream"),
        (200, {"sub": 1098, "email": "dana@example.com", "email_verified": True}, "upstream"),
        (
            200,
            {"sub": "1098", "email": "Dana <dana@example.com>", "email_verified": True},
            "upstream",
        ),
        (
            200,
            {"sub": "1098", "email": "ya29.secret@example.com", "email_verified": True},
            "upstream",
        ),
        (500, {}, "upstream"),
    ],
)
async def test_gmail_identity_is_strict(status, body, code):
    provider = Gmail(
        transport_factory=lambda host: httpx2.MockTransport(
            lambda request: httpx2.Response(status, json=body)
        )
    )
    with pytest.raises(RavnError) as error:
        await provider.identify("ya29.secret")
    assert error.value.code.startswith(code)
    assert "ya29.secret" not in str(error.value)


async def test_gmail_identity_uses_stable_subject_over_openid_host():
    seen = []

    def factory(host):
        seen.append(host)
        return httpx2.MockTransport(
            lambda request: (
                seen.append(str(request.url)),
                httpx2.Response(
                    200,
                    json={"sub": "109876", "email": "dana@example.com", "email_verified": True},
                ),
            )[1]
        )

    assert await Gmail(transport_factory=factory).identify("ya29.token") == (
        "109876",
        "dana@example.com",
    )
    assert seen == [
        "openidconnect.googleapis.com",
        "https://openidconnect.googleapis.com/v1/userinfo",
    ]


def test_only_googles_fixed_hosts_are_reachable():
    for host in [
        "gmailmcp.googleapis.com",
        "oauth2.googleapis.com",
        "openidconnect.googleapis.com",
    ]:
        PinnedTransport(host, 1024)
    for host in ["www.googleapis.com", "accounts.google.com", "gmail.googleapis.com"]:
        with pytest.raises(ValueError, match="Unknown provider host"):
            PinnedTransport(host, 1024)
