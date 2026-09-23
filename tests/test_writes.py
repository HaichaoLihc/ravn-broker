"""Provider operations: optional idempotency keys, no automatic retry, ambiguous outcomes stay unknown."""

import asyncio
import base64
import json

import httpx
import httpx2
import pytest
from conftest import Rig, add_application
from fake_tools import MAIL_SCHEMAS
from mcp import types
from mcp.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError

from ravn.app import create_app
from ravn.config import Config
from ravn.integrations import IntegrationRegistration
from ravn.provider import RemoteMCP

pytestmark = pytest.mark.anyio
TOKEN = "mail_alice-mail-credential"
DRAFT = {"to": ["dana@example.com"], "subject": "Re: Refund #1042", "body": "It is processing."}


class FakeMail:
    def __init__(self):
        self.calls, self.metas = [], []
        self.listing = 0
        self.both_listing = asyncio.Event()
        self.block_list = False
        self.release = asyncio.Event()
        self.lose_response = False
        self.deny_next = False
        self.tools = [types.Tool(name=n, inputSchema=s) for n, s in MAIL_SCHEMAS.items()]
        self.server = Server("fake-mail", on_list_tools=self.list_tools, on_call_tool=self.call)
        self.app = self.server.streamable_http_app(
            streamable_http_path="/mcp/v1",
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

    async def list_tools(self, ctx, params):
        self.listing += 1
        if self.listing == 2:
            self.both_listing.set()
        if self.block_list:
            await self.release.wait()
        return types.ListToolsResult(tools=self.tools)

    async def call(self, ctx, params):
        self.calls.append((params.name, params.arguments))
        self.metas.append(params.meta)
        draft = {"id": f"r-{len(self.calls)}", **(params.arguments or {})}
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(draft))]
        )

    def transport(self, host):
        if host == "mcp.mail.example":
            return Faulty(self)
        return httpx2.MockTransport(
            lambda request: httpx2.Response(
                200, json={"sub": "109876", "email": "alice@example.com", "email_verified": True}
            )
        )


class Faulty(httpx2.AsyncBaseTransport):
    """Refuses before the server sees a call, or drops the reply after it ran."""

    def __init__(self, fake):
        self.fake, self.inner = fake, httpx2.ASGITransport(app=fake.app)

    async def handle_async_request(self, request):
        calling = b"tools/call" in await request.aread()
        if calling and self.fake.deny_next:
            self.fake.deny_next = False
            return httpx2.Response(403, json={"error": {"status": "PERMISSION_DENIED"}})
        response = await self.inner.handle_async_request(request)
        if calling and self.fake.lose_response:
            await response.aread()
            raise httpx2.ReadError("connection dropped after dispatch", request=request)
        return response


@pytest.fixture
async def mail(tmp_path):
    key = tmp_path / "master.key"
    key.write_bytes(base64.b64encode(b"y" * 32))
    key.chmod(0o600)
    fake = FakeMail()
    config = Config.model_validate(
        {
            "deployment_id": "write-tests",
            "server": {"admin_socket": str(tmp_path / "run/admin.sock")},
            "storage": {"sqlite_path": str(tmp_path / "state/ravn.db")},
            "secrets": {"managed": {"active_key_id": "test-key", "key_source": {"path": str(key)}}},
        }
    )
    item = IntegrationRegistration.model_validate(
        {
            "id": "mail",
            "app_id": "demo",
            "endpoint": "https://mcp.mail.example/mcp/v1",
            "identity": {
                "endpoint": "https://identity.mail.example/userinfo",
                "required_claims": {"email_verified": True},
            },
        }
    )
    app = create_app(config, provider=RemoteMCP(item, transport_factory=fake.transport))
    async with fake.server.session_manager.run(), app.router.lifespan_context(app):
        service = app.state.service
        await add_application(service)
        await service.integrations.create(item)
        app_key = (await service.create_key("demo", "test"))["key"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787"
        ) as http:
            rig = Rig(app, http, fake, app_key, None)
            response = await http.post(
                "/v1/connections/import",
                headers=rig.headers(),
                json={"integration_id": "mail", "credential": {"type": "bearer", "token": TOKEN}},
            )
            assert response.status_code == 201, response.text
            rig.conn = response.json()
            rig.runtime = await rig.session(rig.conn)
            yield rig


async def draft(mcp, key=None, arguments=DRAFT, name="create_draft"):
    meta = {"ravn/idempotency-key": key} if key else None
    return await mcp.session.call_tool(mcp.ravn_name(name), arguments, meta=meta)


async def refused(mcp, **kwargs):
    with pytest.raises(MCPError) as error:
        await draft(mcp, **kwargs)
    return error.value.data


async def call_status(rig, call_id):
    response = await rig.http.get(f"/v1/calls/{call_id}", headers=rig.headers())
    assert response.status_code == 200, response.text
    return response.json()


async def test_catalogue_preserves_provider_schemas(mail):
    async with mail.mcp(mail.runtime) as mcp:
        tools = {t.meta["ravn/tool-name"]: t for t in (await mcp.list_tools()).tools}
    assert set(tools) == set(MAIL_SCHEMAS)
    assert tools["create_draft"].input_schema == MAIL_SCHEMAS["create_draft"]


async def test_keyless_submissions_each_dispatch(mail):
    async with mail.mcp(mail.runtime) as mcp:
        first, second = await draft(mcp), await draft(mcp)
    assert len(mail.fake.calls) == 2
    assert first.meta["ravn/call-id"] != second.meta["ravn/call-id"]
    assert (await call_status(mail, first.meta["ravn/call-id"]))["effect"] == "unknown"


async def test_same_key_dispatches_once_and_replays_without_result(mail):
    async with mail.mcp(mail.runtime) as mcp:
        first = await draft(mcp, key="draft-1042-a")
        again = await draft(mcp, key="draft-1042-a")
    assert mail.fake.calls == [("create_draft", DRAFT)]
    # The RAVN extension is consumed locally, never forwarded upstream.
    assert not any(meta and "ravn/idempotency-key" in meta for meta in mail.fake.metas)
    assert again.meta["ravn/idempotency-replayed"] is True
    assert again.meta["ravn/call-id"] == first.meta["ravn/call-id"]
    assert "not retained" in again.content[0].text and "dana@example.com" not in str(again)
    status = await call_status(mail, first.meta["ravn/call-id"])
    assert status["status"] == "succeeded" and "idempotency" not in json.dumps(status)


async def test_same_key_with_different_content_conflicts(mail):
    async with mail.mcp(mail.runtime) as mcp:
        await draft(mcp, key="draft-1042-b")
        data = await refused(mcp, key="draft-1042-b", arguments={**DRAFT, "body": "Changed."})
    assert data["code"] == "idempotency_conflict"
    assert len(mail.fake.calls) == 1


async def test_concurrent_duplicates_dispatch_once(mail):
    mail.fake.block_list = True

    async def attempt():
        async with mail.mcp(mail.runtime) as mcp:
            try:
                return await draft(mcp, key="draft-1042-c")
            except MCPError as error:
                return error.data["code"]

    tasks = [asyncio.create_task(attempt()) for _ in range(2)]
    # Both requests pass the early key check before either reaches admission.
    async with asyncio.timeout(5):
        await mail.fake.both_listing.wait()
    mail.fake.release.set()
    outcomes = await asyncio.gather(*tasks)
    assert len(mail.fake.calls) == 1
    # The loser either sees the call still running or replays its known outcome.
    losers = [
        o for o in outcomes if o == "call_in_progress" or o.meta.get("ravn/idempotency-replayed")
    ]
    assert len(losers) == 1, outcomes


async def test_lost_reply_is_unknown_and_never_redispatched(mail):
    mail.fake.lose_response = True
    async with mail.mcp(mail.runtime) as mcp:
        data = await refused(mcp, key="draft-1042-d")
        assert data["code"] == "outcome_unknown" and data["retryable"] is False
        call_id = data["details"]["call_id"]
        mail.fake.lose_response = False
        replay = await refused(mcp, key="draft-1042-d")
        assert replay["code"] == "outcome_unknown" and replay["details"]["call_id"] == call_id
        assert len(mail.fake.calls) == 1
        # Without a key, a resubmission is a new action and can execute again.
        await draft(mcp)
    assert len(mail.fake.calls) == 2
    status = await call_status(mail, call_id)
    assert (status["status"], status["error_code"]) == ("unknown", "outcome_unknown")


async def test_definite_provider_refusal_fails_and_releases_the_key(mail):
    mail.fake.deny_next = True
    async with mail.mcp(mail.runtime) as mcp:
        data = await refused(mcp, key="draft-1042-e")
        assert data["code"] == "provider_denied"
        assert (await call_status(mail, data["details"]["call_id"]))["status"] == "failed"
        result = await draft(mcp, key="draft-1042-e")
    assert not result.is_error and len(mail.fake.calls) == 1
    connections = await mail.http.get("/v1/connections", headers=mail.headers())
    assert connections.json()["data"][0]["status"] == "active"


@pytest.mark.parametrize(
    "name, arguments, code",
    [
        (
            "label_thread",
            {"threadId": "18f2a0c4d5e6f701", "labelIds": ["TRASH"]},
            "permission_denied",
        ),
        (
            "create_draft",
            {**DRAFT, "htmlBody": "<img src=https://t.example/p>"},
            "invalid_arguments",
        ),
        ("create_draft", {**DRAFT, "attachments": [{"content": "AAAA"}]}, "invalid_arguments"),
        ("create_draft", {**DRAFT, "to": ["Dana <dana@example.com>"]}, "invalid_arguments"),
        ("create_draft", {"to": ["dana@example.com"]}, "invalid_arguments"),
    ],
)
async def test_unknown_tools_and_invalid_arguments_never_reach_provider(
    mail, name, arguments, code
):
    async with mail.mcp(mail.runtime) as mcp:
        data = await refused(mcp, name=name, arguments=arguments)
    assert data["code"] == code
    assert mail.fake.calls == []
