"""Sessions inherit connection access; lifecycle boundaries still apply."""

import pytest
from mcp import types
from mcp.shared.exceptions import MCPError

from ravn.console import PREFIX
from ravn.store import rows

pytestmark = pytest.mark.anyio


async def test_new_provider_tools_work_without_broker_code_or_session_rules(rig):
    session = await rig.session(await rig.connection())
    custom = types.Tool(
        name="custom_action",
        description="An operation supplied by the provider.",
        inputSchema={
            "type": "object",
            "required": ["payload"],
            "properties": {"payload": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    rig.fake.tools.append(custom)
    async with rig.mcp(session) as client:
        advertised = next(
            t for t in (await client.list_tools()).tools if t.name == client.ravn_name(custom.name)
        )
        assert advertised.input_schema == custom.input_schema
        assert advertised.description == custom.description
        result = await client.call_tool(client.ravn_name(custom.name), {"payload": "hello"})
        assert not result.is_error
    assert rig.fake.calls[-1][1:] == (custom.name, {"payload": "hello"})


async def test_removed_provider_tool_cannot_be_called_directly(rig):
    session = await rig.session(await rig.connection())
    rig.fake.tools.clear()
    async with rig.mcp(session) as client:
        with pytest.raises(MCPError) as error:
            await client.call_tool(client.ravn_name("issue_read"), {})
        assert error.value.data["code"] == "permission_denied"
    assert not rig.fake.calls
    async with rig.service.store.transaction() as db:
        calls = await rows(db, "SELECT * FROM calls WHERE session_id=?", (session["id"],))
    assert len(calls) == 1 and calls[0]["status"] == "denied"


async def test_console_reports_inherited_access_without_permission_editor(rig, console):
    _, http = console
    session = await rig.session(await rig.connection())
    path = PREFIX + f"/sessions/{session['id']}"
    detail = (await http.get(path + "?app_id=demo&tenant_id=default")).json()
    assert detail["access_policy"] == "connection" and detail["broker_access"] == "enabled"
    assert "permissions" not in detail and "tools" not in detail
    response = await http.put(
        path + "/permissions",
        json={"app_id": "demo", "tenant_id": "default", "tools": {"issue_read": False}},
    )
    assert response.status_code == 404


@pytest.mark.parametrize("description", ["credential", "oversized"])
async def test_catalogue_does_not_expose_credentials_or_unbounded_metadata(rig, description):
    from conftest import ALICE

    session = await rig.session(await rig.connection())
    rig.fake.tools[0].description = ALICE if description == "credential" else "x" * 4096
    if description == "oversized":
        provider = rig.service.provider_for(rig.service.integrations.get("demo", "records"))
        provider.result_limit = 1024
    async with rig.mcp(session) as client:
        result = await client.list_tools()
        assert result.tools == []
        assert result.meta["ravn/unavailable-connections"][0]["code"] == "schema_rejected"
        assert ALICE not in result.model_dump_json()
