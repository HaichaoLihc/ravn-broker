"""gmail_local's fixed provider profile: a self-hosted stand-in for gmailmcp.

Covers what's specific to this connector: config validation (no gmail.compose,
loopback-only endpoint), GMAIL_LOCAL_SCHEMAS review, and LoopbackTransport's
127.0.0.1-only restriction. Identity reuses Gmail's real behavior (see
GoogleIdentity, exercised by test_gmail.py) since both connectors authenticate
the same real Google OAuth grant.
"""

import httpx2
import pytest
from mcp import types
from pydantic import ValidationError

from ravn.common import RavnError
from ravn.config import GMAIL_READONLY, Integration
from ravn.gmail import GmailLocal, LoopbackTransport
from ravn.manifest import GMAIL_LOCAL_SCHEMAS, reviewed_tools, schema_hash

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
        "connector": "gmail_local",
        "endpoint": "http://127.0.0.1:8790/mcp",
        "manifest": "builtin:gmail-read-v1",
        "oauth": {
            "profile": "google_web_pkce",
            "client_id": "google-client",
            "client_secret_file": str(secret),
            "scopes": scopes,
        },
        **patch,
    }
    return Integration.model_validate(value)


def test_gmail_local_accepts_exact_read_scopes(tmp_path):
    assert integration(tmp_path).connector == "gmail_local"


@pytest.mark.parametrize(
    "scopes, patch, message",
    [
        (READ_SCOPES, {"endpoint": "https://gmailmcp.googleapis.com/mcp/v1"}, "fixed provider"),
        (READ_SCOPES, {"manifest": "builtin:gmail-v1"}, "fixed provider"),
        (["openid", "email"], {}, "gmail_local needs exactly"),
        (
            [*READ_SCOPES, "https://www.googleapis.com/auth/gmail.compose"],
            {},
            "gmail_local needs exactly",
        ),
        (READ_SCOPES, {"schema_hashes": {"create_draft": PIN}}, "reviewed tools"),
        (READ_SCOPES, {"schema_hashes": {"list_drafts": PIN}}, "reviewed tools"),
    ],
)
def test_gmail_local_config_rejects_other_authority(tmp_path, scopes, patch, message):
    with pytest.raises(ValidationError, match=message):
        integration(tmp_path, scopes, **patch)


def test_gmail_local_oauth_profile_must_match(tmp_path):
    value = integration(tmp_path).model_dump(mode="json")
    value["oauth"]["profile"] = "github_app_pkce"
    with pytest.raises(ValidationError, match="OAuth profile must match"):
        Integration.model_validate(value)


def test_gmail_local_schemas_are_read_only_and_snake_case():
    assert set(GMAIL_LOCAL_SCHEMAS) == {
        "search_threads",
        "get_thread",
        "get_message",
        "list_labels",
    }
    assert "create_draft" not in GMAIL_LOCAL_SCHEMAS
    assert "thread_id" in GMAIL_LOCAL_SCHEMAS["get_thread"]["properties"]
    assert "message_id" in GMAIL_LOCAL_SCHEMAS["get_message"]["properties"]


def test_reviewed_tools_never_exposes_create_draft_for_gmail_local():
    upstream = [
        types.Tool(
            name="create_draft",
            description="Create a draft email.",
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "required": ["to", "subject", "body"],
                "properties": {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="list_labels",
            description="List Gmail labels.",
            inputSchema={"type": "object", "additionalProperties": False, "properties": {}},
        ),
    ]
    list_labels_hash = schema_hash(upstream[1])
    result = reviewed_tools(
        upstream,
        {"create_draft": PIN, "list_labels": list_labels_hash},
        connector="gmail_local",
    )
    assert [t.name for t in result] == ["list_labels"]


def test_loopback_transport_only_reaches_its_own_pinned_port():
    LoopbackTransport("127.0.0.1:8790", 1024)
    with pytest.raises(ValueError, match="127.0.0.1"):
        LoopbackTransport("gmailmcp.googleapis.com", 1024)
    with pytest.raises(ValueError, match="127.0.0.1"):
        LoopbackTransport("0.0.0.0:8790", 1024)


async def test_loopback_transport_refuses_other_destinations():
    transport = LoopbackTransport("127.0.0.1:8790", 1024)
    request = httpx2.Request("GET", "https://127.0.0.1:8790/mcp")
    with pytest.raises(RavnError, match="Unregistered upstream destination"):
        await transport.handle_async_request(request)
    request = httpx2.Request("GET", "http://127.0.0.1:9999/mcp")
    with pytest.raises(RavnError, match="Unregistered upstream destination"):
        await transport.handle_async_request(request)


async def test_gmail_local_identify_uses_googles_real_userinfo_endpoint():
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

    assert await GmailLocal(transport_factory=factory).identify("ya29.token") == (
        "109876",
        "dana@example.com",
    )
    assert seen == [
        "openidconnect.googleapis.com",
        "https://openidconnect.googleapis.com/v1/userinfo",
    ]


async def test_gmail_local_default_transport_routes_loopback_and_google_separately():
    provider = GmailLocal()
    loopback = provider.transport_factory("127.0.0.1:8790")
    assert isinstance(loopback, LoopbackTransport)
    google = provider.transport_factory("openidconnect.googleapis.com")
    assert not isinstance(google, LoopbackTransport)
