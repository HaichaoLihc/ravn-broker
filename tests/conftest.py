import asyncio
import base64
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import pytest
from fake_tools import SCHEMAS
from mcp import Client, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.transport_security import TransportSecuritySettings

from ravn.app import create_app
from ravn.catalogue import tool_name
from ravn.config import Application, Config
from ravn.console import PREFIX, Console, create_console_app
from ravn.integrations import IntegrationRegistration
from ravn.provider import RemoteMCP

ALICE = "test_alice_provider_secret_123456789"
BOB = "test_bob_provider_secret_123456789"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.headers = []
        self.block_list = False
        self.block_call = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = False
        self.echo = False
        self.tools = [types.Tool(name=name, inputSchema=schema) for name, schema in SCHEMAS.items()]
        self.server = Server(
            "fake-records", on_list_tools=self.list_tools, on_call_tool=self.call_tool
        )
        self.app = self.server.streamable_http_app(
            streamable_http_path="/mcp/",
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

    async def list_tools(self, ctx, params):
        self.headers.append(dict(ctx.request.headers))
        if self.block_list:
            self.entered.set()
            await self.release.wait()
        return types.ListToolsResult(tools=self.tools)

    async def call_tool(self, ctx, params):
        credential = ctx.request.headers["authorization"].removeprefix("Bearer ")
        self.calls.append((credential, params.name, params.arguments))
        if self.block_call:
            self.entered.set()
            await self.release.wait()
        if self.fail:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="Issue unavailable")], isError=True
            )
        value = credential if self.echo else ("alice issue" if credential == ALICE else "bob issue")
        return types.CallToolResult(content=[types.TextContent(type="text", text=value)])

    def transport(self, host):
        if host == "mcp.records.example":
            return httpx2.ASGITransport(app=self.app)

        def identity(request):
            value = request.headers.get("authorization", "").removeprefix("Bearer ")
            if value not in {ALICE, BOB}:
                return httpx2.Response(401, json={"error": "secret echo: " + value})
            return httpx2.Response(
                200,
                json={
                    "id": 101 if value == ALICE else 202,
                    "login": "alice" if value == ALICE else "bob",
                },
            )

        return httpx2.MockTransport(identity)


@pytest.fixture
def config(tmp_path):
    key = tmp_path / "master.key"
    key.write_bytes(base64.b64encode(b"x" * 32))
    key.chmod(0o600)
    return Config.model_validate(
        {
            "deployment_id": "test-deployment",
            "server": {"admin_socket": str(tmp_path / "run/admin.sock")},
            "storage": {"sqlite_path": str(tmp_path / "state/ravn.db")},
            "secrets": {"managed": {"active_key_id": "test-key", "key_source": {"path": str(key)}}},
        }
    )


def records_integration(app_id="demo", **patch):
    return IntegrationRegistration.model_validate(
        {
            "id": "records",
            "app_id": app_id,
            "endpoint": "https://mcp.records.example/mcp/",
            "identity": {
                "endpoint": "https://identity.records.example/user",
                "id_field": "id",
                "display_field": "login",
            },
            **patch,
        }
    )


async def add_application(service, app_id="demo", **patch):
    return await service.applications.save(Application(id=app_id, **patch), create=True)


class Rig:
    def __init__(self, app, http, fake, key, saas_key):
        self.app, self.http, self.fake, self.key, self.saas_key = app, http, fake, key, saas_key
        self.service = app.state.service

    def headers(self, user="alice", tenant=None, key=None):
        value = {"Authorization": f"Bearer {key or self.key}", "X-Ravn-User-Id": user}
        if tenant is not None:
            value["X-Ravn-Tenant-Id"] = tenant
        return value

    async def connection(self, user="alice", credential=ALICE, tenant=None, key=None):
        response = await self.http.post(
            "/v1/connections/import",
            headers=self.headers(user, tenant, key),
            json={
                "integration_id": "records",
                "credential": {"type": "bearer", "token": credential},
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    async def session(self, connection, user="alice", tenant=None, key=None, ttl=None):
        body = {"connection_ids": [connection["id"]]}
        if ttl is not None:
            body["ttl_seconds"] = ttl
        response = await self.http.post(
            "/v1/sessions", headers=self.headers(user, tenant, key), json=body
        )
        assert response.status_code == 201, response.text
        return response.json()

    @asynccontextmanager
    async def mcp(self, session, mode="auto"):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app),
            headers={"Authorization": "Bearer " + session["token"]},
        ) as http:
            async with Client(
                streamable_http_client("http://127.0.0.1:8787/mcp", http_client=http),
                mode=mode,
                cache=None,
            ) as client:
                client.ravn_name = lambda name: tool_name(session["connections"][0]["id"], name)
                yield client


@pytest.fixture
async def rig(config):
    fake = FakeProvider()
    app = create_app(config)
    async with fake.server.session_manager.run(), app.router.lifespan_context(app):
        service = app.state.service
        for app_id, tenant_mode in [("demo", "single"), ("saas", "multi")]:
            await add_application(service, app_id, tenant_mode=tenant_mode)
            item = records_integration(app_id)
            await service.integrations.create(item)
            service.providers[(app_id, "records")] = RemoteMCP(
                item, transport_factory=fake.transport
            )
        key = (await service.create_key("demo", "test"))["key"]
        saas_key = (await service.create_key("saas", "test"))["key"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787"
        ) as http:
            yield Rig(app, http, fake, key, saas_key)


@pytest.fixture
async def console(rig):
    state = Console(rig.service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_console_app(state)), base_url=state.origin
    ) as http:
        ticket = parse_qs(urlsplit(state.ticket()["url"]).fragment)["ticket"][0]
        response = await http.post(
            PREFIX + "/auth/exchange", headers={"Origin": state.origin}, json={"ticket": ticket}
        )
        assert response.status_code == 200, response.text
        http.headers.update({"Origin": state.origin, "X-CSRF-Token": response.json()["csrf_token"]})
        yield state, http
    state.close()
