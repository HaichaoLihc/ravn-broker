"""Sending through Gmail's REST API: only after a person approves, never retried."""

import asyncio
import base64
import email
import json
from email.policy import default

import httpx
import httpx2
import pytest
from conftest import Rig
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from ravn.app import create_app
from ravn.config import GMAIL_SEND, Config, Integration
from ravn.github import GitHub
from ravn.gmail_api import GmailApi, encode
from ravn.manifest import GMAIL_SEND_SCHEMAS, schema_hash
from ravn.store import rows

pytestmark = pytest.mark.anyio
TOKEN = "ya29.alice-gmail-send-credential"
EMAIL = {"to": ["dana@example.com"], "subject": "Refund #1042", "body": "It was approved."}
SCOPES = ["openid", "email", GMAIL_SEND]


class FakeGoogle:
    """Gmail's send endpoint and Google's userinfo, recording every send."""

    def __init__(self):
        self.sent, self.answer = [], 200

    def transport(self, host):
        def handle(request):
            if host == "openidconnect.googleapis.com":
                return httpx2.Response(
                    200,
                    json={"sub": "109876", "email": "alice@example.com", "email_verified": True},
                )
            assert request.url.path == "/gmail/v1/users/me/messages/send"
            assert request.headers["authorization"] == f"Bearer {TOKEN}"
            if self.answer == "drop":
                raise httpx2.ReadError("connection dropped after send", request=request)
            if self.answer != 200:
                return httpx2.Response(self.answer, json={"error": {"code": self.answer}})
            raw = json.loads(request.content)["raw"]
            self.sent.append(
                email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=default)
            )
            return httpx2.Response(200, json={"id": f"m-{len(self.sent)}", "threadId": "t-1"})

        return httpx2.MockTransport(handle)


@pytest.fixture
async def gmail_api(tmp_path):
    key = tmp_path / "master.key"
    key.write_bytes(base64.b64encode(b"z" * 32))
    key.chmod(0o600)
    fake = FakeGoogle()
    tools = await GmailApi().inspect(TOKEN)
    config = Config.model_validate(
        {
            "deployment_id": "send-tests",
            "server": {"admin_socket": str(tmp_path / "run/admin.sock")},
            "storage": {"sqlite_path": str(tmp_path / "state/ravn.db")},
            "secrets": {"managed": {"active_key_id": "test-key", "key_source": {"path": str(key)}}},
            "applications": [{"id": "demo"}],
            "integrations": [
                {
                    "id": "gmail-send",
                    "app_id": "demo",
                    "connector": "gmail_api",
                    "endpoint": "https://gmail.googleapis.com/gmail/v1",
                    "manifest": "builtin:gmail-send-v1",
                    "schema_hashes": {t.name: schema_hash(t) for t in tools},
                }
            ],
        }
    )
    app = create_app(
        config,
        provider={
            "github_cloud": GitHub(),
            "gmail_api": GmailApi(transport_factory=fake.transport),
        },
    )
    async with app.router.lifespan_context(app):
        service = app.state.service
        app_key = (await service.create_key("demo", "test"))["key"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787"
        ) as http:
            rig = Rig(app, http, fake, app_key, None)
            response = await http.post(
                "/v1/connections/import",
                headers=rig.headers(),
                json={
                    "integration_id": "gmail-send",
                    "credential": {"type": "bearer", "token": TOKEN},
                },
            )
            assert response.status_code == 201, response.text
            rig.conn = response.json()
            rig.runtime = await rig.session(rig.conn)
            yield rig


def send(rig, arguments=EMAIL):
    """Call send_email in the background; the result, or the refusal's data."""

    async def run():
        async with rig.mcp(rig.runtime) as mcp:
            try:
                return await mcp.session.call_tool("send_email", arguments)
            except MCPError as error:
                return error.data

    return asyncio.create_task(run())


async def decide(rig, decision):
    async with asyncio.timeout(5):
        while not (found := (await rig.service.approvals.pending(1))["data"]):
            pass
    [item] = found
    await rig.service.approvals.decide(item["id"], decision)
    return item


async def finished(task):
    async with asyncio.timeout(5):
        return await task


async def test_the_tool_is_listed_as_a_write(gmail_api):
    async with gmail_api.mcp(gmail_api.runtime) as mcp:
        [tool] = (await mcp.list_tools()).tools
    assert tool.name == "send_email"
    assert tool.annotations.read_only_hint is False
    assert "approves each send" in tool.description


async def test_an_approved_email_is_sent_once(gmail_api):
    task = send(gmail_api, {**EMAIL, "cc": ["boss@example.com"], "bcc": ["audit@example.com"]})
    item = await decide(gmail_api, "approve")
    assert json.loads(item["arguments"])["to"] == ["dana@example.com"]
    result = await finished(task)
    assert not result.is_error
    assert json.loads(result.content[0].text) == {"id": "m-1", "threadId": "t-1", "status": "sent"}
    [message] = gmail_api.fake.sent
    assert message["To"] == "dana@example.com" and message["Cc"] == "boss@example.com"
    assert message["Bcc"] == "audit@example.com" and message["Subject"] == "Refund #1042"
    assert message.get_content().strip() == "It was approved."


async def test_a_denied_email_never_reaches_google(gmail_api):
    task = send(gmail_api)
    await decide(gmail_api, "deny")
    assert (await finished(task))["code"] == "approval_denied"
    assert gmail_api.fake.sent == []


@pytest.mark.parametrize(
    "patch",
    [
        {"subject": "Hi\r\nBcc: attacker@example.com"},
        {"subject": "Hi\nX-Injected: yes"},
        {"to": ["dana@example.com\r\nBcc: attacker@example.com"]},
        {"to": ["Dana <dana@example.com>"]},
        {"to": []},
        {"html": "<b>rich</b>"},
    ],
)
async def test_header_injection_is_refused_before_any_approval(gmail_api, patch):
    result = await finished(send(gmail_api, {**EMAIL, **patch}))
    assert result["code"] == "invalid_arguments"
    async with gmail_api.service.store.transaction() as db:
        assert await rows(db, "SELECT id FROM approvals") == []


async def test_a_refusal_from_google_is_a_definite_failure(gmail_api):
    gmail_api.fake.answer = 403
    task = send(gmail_api)
    await decide(gmail_api, "approve")
    assert (await finished(task))["code"] == "provider_denied"


async def test_a_lost_reply_is_unknown_and_never_retried(gmail_api):
    gmail_api.fake.answer = "drop"
    task = send(gmail_api)
    await decide(gmail_api, "approve")
    assert (await finished(task))["code"] == "outcome_unknown"
    async with gmail_api.service.store.transaction() as db:
        [call] = await rows(db, "SELECT status FROM calls")
    assert call["status"] == "unknown"


def test_the_message_is_plain_text_with_only_the_given_headers():
    message = email.message_from_bytes(base64.urlsafe_b64decode(encode(EMAIL)), policy=default)
    assert message.get_content_type() == "text/plain"
    assert {k.lower() for k in message.keys()} <= {
        "to",
        "subject",
        "content-type",
        "content-transfer-encoding",
        "mime-version",
    }


def test_send_scope_is_exact(tmp_path):
    secret = tmp_path / "google-secret"
    secret.write_text("google-client-secret")
    secret.chmod(0o600)

    def integration(scopes):
        return Integration.model_validate(
            {
                "id": "gmail-send",
                "app_id": "demo",
                "connector": "gmail_api",
                "endpoint": "https://gmail.googleapis.com/gmail/v1",
                "manifest": "builtin:gmail-send-v1",
                "oauth": {
                    "profile": "google_web_pkce",
                    "client_id": "client.apps.googleusercontent.com",
                    "client_secret_file": str(secret),
                    "scopes": scopes,
                },
            }
        )

    integration(SCOPES)
    for scopes in (
        ["openid", "email"],
        [*SCOPES, "https://www.googleapis.com/auth/gmail.readonly"],
    ):
        with pytest.raises(ValidationError):
            integration(scopes)


async def test_the_schema_is_the_reviewed_contract():
    [tool] = await GmailApi().inspect(TOKEN)
    assert tool.input_schema == GMAIL_SEND_SCHEMAS["send_email"]
