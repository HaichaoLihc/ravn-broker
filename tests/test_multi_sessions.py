"""One runtime token, independent connections, and live membership checks."""

import asyncio

import httpx2
import pytest
from conftest import ALICE, BOB
from mcp import types
from mcp.shared.exceptions import MCPError

from ravn.catalogue import tool_name
from ravn.common import RavnError
from ravn.console import PREFIX
from ravn.integrations import IntegrationRegistration
from ravn.provider import RemoteMCP
from ravn.store import one, rows

pytestmark = pytest.mark.anyio
ARGS = {"owner": "acme", "repo": "test"}


async def create(rig, *connections):
    response = await rig.http.post(
        "/v1/sessions",
        headers=rig.headers(),
        json={"connection_ids": [c["id"] for c in connections]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def path(session, connection):
    return f"/v1/sessions/{session['id']}/connections/{connection['id']}"


async def test_one_token_routes_colliding_tools_and_membership_changes(rig):
    a, b = await rig.connection(), await rig.connection(credential=BOB)
    session = await create(rig, a)
    async with rig.mcp(session) as client:
        before = {t.name for t in (await client.list_tools()).tools}
        response = await rig.http.put(path(session, b), headers=rig.headers())
        assert response.status_code == 200
        assert (
            response.json()["expires_at"] == session["expires_at"]
            and "token" not in response.json()
        )
        assert (await rig.http.put(path(session, b), headers=rig.headers())).status_code == 200
        catalogue = await client.list_tools()
        assert len(catalogue.tools) == 4 and len({t.name for t in catalogue.tools}) == 4
        assert before <= {t.name for t in catalogue.tools}
        for conn, expected in ((a, "alice issue"), (b, "bob issue")):
            advertised = next(
                t
                for t in catalogue.tools
                if t.meta["ravn/connection-id"] == conn["id"]
                and t.meta["ravn/tool-name"] == "list_issues"
            )
            result = await client.call_tool(advertised.name, ARGS)
            assert result.content[0].text == expected
            record = (
                await rig.http.get(
                    f"/v1/calls/{result.meta['ravn/call-id']}", headers=rig.headers()
                )
            ).json()
            assert record["session_id"] == session["id"] and record["connection_id"] == conn["id"]
        assert {c[0] for c in rig.fake.calls} == {ALICE, BOB}
        assert (await rig.http.delete(path(session, a), headers=rig.headers())).status_code == 200
        assert (await rig.http.delete(path(session, a), headers=rig.headers())).status_code == 200
        with pytest.raises(MCPError) as refused:
            await client.call_tool(tool_name(a["id"], "list_issues"), ARGS)
        assert refused.value.data["code"] == "connection_not_attached"
        assert all(
            t.meta["ravn/connection-id"] == b["id"] for t in (await client.list_tools()).tools
        )
        assert not (await client.call_tool(tool_name(b["id"], "list_issues"), ARGS)).is_error
    listing = await rig.http.get(
        "/v1/sessions", headers=rig.headers(), params={"connection_id": a["id"]}
    )
    assert listing.json()["data"] == []
    assert (
        await rig.http.get("/v1/sessions", headers=rig.headers(), params={"connection_id": b["id"]})
    ).json()["data"][0]["id"] == session["id"]
    assert (await rig.http.get(f"/v1/connections/{a['id']}", headers=rig.headers())).json()[
        "status"
    ] == "active"


async def test_empty_session_reuses_token_and_revoke_blocks_all(rig):
    session = await create(rig)
    a, b = await rig.connection(), await rig.connection(credential=BOB)
    async with rig.mcp(session) as client:
        assert (await client.list_tools()).tools == []
        for conn in (a, b):
            assert (
                await rig.http.put(path(session, conn), headers=rig.headers())
            ).status_code == 200
        assert len((await client.list_tools()).tools) == 4
    await rig.http.post(f"/v1/sessions/{session['id']}/revoke", headers=rig.headers())
    assert (await rig.http.put(path(session, a), headers=rig.headers())).status_code == 409
    assert (await rig.http.delete(path(session, a), headers=rig.headers())).status_code == 409
    with pytest.raises(RavnError):
        await rig.service.authenticate_session(session["token"])


async def test_membership_scope_runtime_and_console_boundaries(rig, console):
    _, http = console
    a = await rig.connection()
    b = await rig.connection("bob", BOB)
    tenant = await rig.connection(tenant="other", key=rig.saas_key)
    app = await rig.connection(tenant="default", key=rig.saas_key)
    session = await create(rig, a)
    for foreign in (b, tenant, app):
        for method in ("PUT", "DELETE"):
            assert (
                await rig.http.request(method, path(session, foreign), headers=rig.headers())
            ).status_code == 404
            assert (
                await http.request(
                    method,
                    PREFIX + path(session, foreign).removeprefix("/v1"),
                    json={"app_id": "demo", "tenant_id": "default"},
                )
            ).status_code == 404
    assert (
        await rig.http.get(f"/v1/sessions/{session['id']}", headers=rig.headers("bob"))
    ).status_code == 404
    for method in ("PUT", "DELETE"):
        assert (
            await rig.http.request(
                method,
                path(session, a),
                headers={"Authorization": "Bearer " + session["token"], "X-Ravn-User-Id": "alice"},
            )
        ).status_code == 401
        assert (
            await http.request(
                method,
                PREFIX + path(session, a).removeprefix("/v1"),
                headers={"X-CSRF-Token": "wrong"},
                json={"app_id": "demo", "tenant_id": "default"},
            )
        ).status_code == 403
    async with rig.mcp(session) as client:
        for name in ("list_issues", tool_name(b["id"], "list_issues")):
            with pytest.raises(MCPError):
                await client.call_tool(name, ARGS)
    assert not rig.fake.calls


async def test_console_add_remove_and_filter_share_api_state(rig, console):
    _, http = console
    a, b = await rig.connection(), await rig.connection(credential=BOB)
    session = await create(rig, a)
    target = PREFIX + path(session, b).removeprefix("/v1")
    namespace = {"app_id": "demo", "tenant_id": "default"}
    for _ in range(2):
        response = await http.put(target, json=namespace)
        assert response.status_code == 200 and len(response.json()["connections"]) == 2
    assert (
        len(
            (await rig.http.get(f"/v1/sessions/{session['id']}", headers=rig.headers())).json()[
                "connections"
            ]
        )
        == 2
    )
    assert (
        await http.get(PREFIX + "/sessions", params={**namespace, "connection_id": b["id"]})
    ).json()["data"][0]["id"] == session["id"]
    assert (await http.request("DELETE", target, json=namespace)).status_code == 200
    assert (
        await http.get(PREFIX + "/sessions", params={**namespace, "connection_id": b["id"]})
    ).json()["data"] == []
    async with rig.service.store.transaction() as db:
        events = await rows(
            db, "SELECT * FROM events WHERE subject_id=?", (session["id"] + ":" + b["id"],)
        )
        assert [e["kind"] for e in events] == [
            "session.connection_attached",
            "session.connection_removed",
        ]
        assert all(e["actor_kind"] == "local_operator" for e in events)


async def test_outage_and_disabled_integration_do_not_hide_other_tools(rig):
    value = rig.service.integrations.get("demo", "records").model_dump(mode="json") | {
        "id": "other"
    }
    await rig.service.integrations.create(
        IntegrationRegistration.model_validate(value), actor_kind="local_operator", actor_id="test"
    )
    integration = rig.service.integrations.get("demo", "other")
    provider = RemoteMCP(integration, transport_factory=rig.fake.transport)
    rig.service.providers[("demo", "other")] = provider
    a = await rig.connection()
    b = (
        await rig.http.post(
            "/v1/connections/import",
            headers=rig.headers(),
            json={"integration_id": "other", "credential": {"type": "bearer", "token": BOB}},
        )
    ).json()
    session = await create(rig, a, b)
    provider.transport_factory = lambda host: httpx2.MockTransport(
        lambda request: httpx2.Response(503)
    )
    async with rig.mcp(session) as client:
        result = await client.list_tools()
        assert {t.meta["ravn/connection-id"] for t in result.tools} == {a["id"]}
        assert result.meta["ravn/unavailable-connections"][0]["connection_id"] == b["id"]
        assert not (await client.call_tool(tool_name(a["id"], "list_issues"), ARGS)).is_error
        await rig.service.integrations.disable(
            "demo", "other", actor_kind="local_operator", actor_id="test"
        )
        assert len((await client.list_tools()).tools) == 2
        await rig.http.post(f"/v1/connections/{a['id']}/disconnect", headers=rig.headers())
        assert (await client.list_tools()).tools == []
    assert (await rig.http.get(f"/v1/sessions/{session['id']}", headers=rig.headers())).json()[
        "status"
    ] == "active"


@pytest.mark.parametrize("change", ["detach", "reauthorize"])
async def test_membership_and_epoch_rechecked_before_dispatch(rig, change):
    a, b = await rig.connection(), await rig.connection(credential=BOB)
    session = await create(rig, a, b)
    principal = await rig.service.authenticate_session(session["token"])
    rig.fake.block_list = True
    pending = asyncio.create_task(
        rig.service.execute(principal, a["id"], "list_issues", ARGS, "req_race")
    )
    await asyncio.wait_for(rig.fake.entered.wait(), 2)
    if change == "detach":
        assert (await rig.http.delete(path(session, a), headers=rig.headers())).status_code == 200
    else:
        async with rig.service.store.transaction() as db:
            await db.execute("UPDATE connections SET epoch=epoch+1 WHERE id=?", (a["id"],))
        assert (await rig.http.put(path(session, a), headers=rig.headers())).status_code == 200
    rig.fake.block_list = False
    rig.fake.release.set()
    with pytest.raises(RavnError):
        await pending
    assert not rig.fake.calls
    assert not (
        await rig.service.execute(principal, b["id"], "list_issues", ARGS, "req_other")
    ).is_error


async def test_creation_atomic_limits_duplicates_and_no_implicit_membership(rig):
    connections = [await rig.connection() for _ in range(9)]
    for ids in (
        [connections[0]["id"]] * 2,
        [c["id"] for c in connections],
        [connections[0]["id"], "conn_missing"],
    ):
        response = await rig.http.post(
            "/v1/sessions", headers=rig.headers(), json={"connection_ids": ids}
        )
        assert response.status_code in {400, 404}
    assert (await rig.http.get("/v1/sessions", headers=rig.headers())).json()["data"] == []
    session = await create(rig, *connections[:8])
    assert (
        await rig.http.put(path(session, connections[8]), headers=rig.headers())
    ).status_code == 400
    async with rig.service.store.transaction() as db:
        await db.execute(
            "UPDATE sessions SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (session["id"],)
        )
    assert (
        await rig.http.put(path(session, connections[0]), headers=rig.headers())
    ).status_code == 409


async def test_restart_preserves_existing_tokens_lifetimes_and_history(rig):
    conn = await rig.connection()
    session = await create(rig, conn)
    principal = await rig.service.authenticate_session(session["token"])
    call = await rig.service.execute(principal, conn["id"], "list_issues", ARGS, "req_before")
    async with rig.service.store.transaction() as db:
        before = await one(db, "SELECT * FROM sessions WHERE id=?", (session["id"],))
    await rig.service.close()
    await rig.service.start()
    async with rig.service.store.transaction() as db:
        assert await one(db, "SELECT * FROM sessions WHERE id=?", (session["id"],)) == before
        assert (await one(db, "PRAGMA user_version"))["user_version"] == 9
        assert await rows(db, "PRAGMA foreign_key_check") == []
    principal = await rig.service.authenticate_session(session["token"])
    assert (await rig.service.session(principal, session["id"]))["connections"][0]["id"] == conn[
        "id"
    ]
    assert (await rig.service.call_status(principal, call.meta["ravn/call-id"]))[
        "status"
    ] == "succeeded"


async def test_arbitrary_tool_names_are_portable_unique_and_resolve_exactly(rig):
    names = ["search", "h" + "a" * 24, "long_tool_" * 12, "查找", "a__b"]
    rig.fake.tools = [types.Tool(name=n, inputSchema={"type": "object"}) for n in names]
    conn = await rig.connection()
    session = await create(rig, conn)
    async with rig.mcp(session) as client:
        tools = (await client.list_tools()).tools
        assert len({t.name for t in tools}) == len(names)
        for tool in tools:
            assert len(tool.name) <= 64 and tool.name.isascii()
            await client.call_tool(tool.name, {})
            assert rig.fake.calls[-1][1] == tool.meta["ravn/tool-name"]
