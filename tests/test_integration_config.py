"""Integrations are operator configuration, without built-in provider registries."""

import httpx2
import pytest
from pydantic import ValidationError

from ravn.common import RavnError
from ravn.config import Config
from ravn.integrations import IntegrationRegistration as Integration
from ravn.provider import RemoteMCP

pytestmark = pytest.mark.anyio


def integration(**patch):
    return Integration.model_validate(
        {
            "id": "custom",
            "app_id": "demo",
            "endpoint": "https://mcp.custom.example/tools",
            "identity": {
                "endpoint": "https://identity.custom.example/user",
                "id_field": "account",
                "display_field": "name",
                "required_claims": {"verified": True},
            },
            **patch,
        }
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://mcp.example/tools",
        "https://user:pass@mcp.example/tools",
        "https://mcp.example:444/tools",
        "https://mcp.example/tools?token=secret",
        "https://mcp.example/tools#fragment",
        "https://mcp.example\\@other.example/tools",
    ],
)
def test_provider_endpoints_reject_unsafe_urls(endpoint):
    with pytest.raises(ValidationError):
        integration(endpoint=endpoint)


def test_integrations_need_no_provider_code_or_initial_entries(config):
    value = config.model_dump(mode="json")
    assert "integrations" not in Config.model_validate(value).model_dump()
    assert "applications" not in value
    assert integration().endpoint == "https://mcp.custom.example/tools"


@pytest.mark.parametrize(
    "field,value",
    [("connector", "custom"), ("manifest", "old-manifest"), ("schema_hashes", {"tool": "a" * 64})],
)
def test_retired_provider_and_policy_config_fails_explicitly(field, value):
    with pytest.raises(ValidationError):
        integration(**{field: value})


async def test_identity_uses_configured_endpoint_and_field_mapping():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx2.Response(
            200, json={"account": "stable-id", "name": "Alice", "verified": True}
        )

    provider = RemoteMCP(
        integration(), transport_factory=lambda host: httpx2.MockTransport(respond)
    )
    assert await provider.identify("opaque-token") == ("stable-id", "Alice")
    assert str(seen[0].url) == "https://identity.custom.example/user"
    assert seen[0].headers["authorization"] == "Bearer opaque-token"


@pytest.mark.parametrize(
    "body",
    [
        {"account": "id", "name": "Alice", "verified": False},
        {"account": "id", "name": "Alice", "verified": 1},
        {"account": "id", "name": "opaque-token", "verified": True},
        {"account": None, "name": "Alice", "verified": True},
    ],
)
async def test_identity_rejects_unverified_or_malformed_claims(body):
    provider = RemoteMCP(
        integration(),
        transport_factory=lambda host: httpx2.MockTransport(
            lambda request: httpx2.Response(200, json=body)
        ),
    )
    with pytest.raises(RavnError):
        await provider.identify("opaque-token")


@pytest.mark.parametrize(
    "field", ["state", "redirect_uri", "scope", "client_secret", "code_challenge", "response_type"]
)
def test_authorization_extra_params_cannot_override_security_fields(tmp_path, field):
    with pytest.raises(ValidationError):
        integration(
            oauth={
                "client_id": "client",
                "client_secret": "test-secret",
                "authorization_endpoint": "https://auth.example/authorize",
                "token_endpoint": "https://auth.example/token",
                "issuer": "https://auth.example",
                "authorization_params": {field: "override"},
            }
        )
