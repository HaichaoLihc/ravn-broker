import asyncio
from datetime import UTC, datetime

import pytest
from conftest import ALICE, BOB
from mcp.shared.exceptions import MCPError

from ravn.app import Boundary
from ravn.common import RavnError
from ravn.store import one

ARGS = {"method": "get", "owner": "acme", "repo": "issues", "issue_number": 42}
pytestmark = pytest.mark.anyio


async def test_body_deadline_releases_request_slot(rig, monkeypatch):
    monkeypatch.setattr("ravn.app.BODY_READ_TIMEOUT_SECONDS", 0.01)
    messages = []

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    async def unreachable(*args):
        pytest.fail("A stalled body must not reach a handler")

    boundary = Boundary(unreachable, rig.service)
    await boundary(
        {"type": "http", "path": "/healthz", "headers": [(b"host", b"127.0.0.1:8787")]},
        receive,
        send,
    )
    assert messages[0]["status"] == 408
    assert boundary.active == 0


async def test_unreviewed_integration_cannot_issue_sessions(rig):
    conn = await rig.connection()
    rig.service.config.integration("demo", "github").schema_hashes.clear()
    response = await rig.http.post(
        "/v1/sessions", headers=rig.headers(), json={"connection_id": conn["id"]}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "integration_not_ready"
    assert not rig.fake.calls


async def test_rejects_future_restrictions_and_malformed_idempotency_keys(rig):
    conn = await rig.connection()
    response = await rig.http.post(
        "/v1/sessions",
        headers=rig.headers(),
        json={"connection_id": conn["id"], "restrictions": {"tools": ["issue_read"]}},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_constraint"
    session = await rig.session(conn)
    async with rig.mcp(session) as mcp:
        for key in ["short", "has spaces in it", 12345678]:
            with pytest.raises(MCPError) as error:
                await mcp.session.call_tool("issue_read", ARGS, meta={"ravn/idempotency-key": key})
            assert error.value.data["code"] == "invalid_request"
    assert not rig.fake.calls


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_mcp_vertical_slice_and_call_status(rig, mode):
    conn = await rig.connection()
    session = await rig.session(conn)
    async with rig.mcp(session, mode) as mcp:
        tools = await mcp.list_tools()
        assert {t.name for t in tools.tools} == {"issue_read", "list_issues"}
        result = await mcp.call_tool("issue_read", ARGS)
        assert result.content[0].text == "alice issue"
        cid = result.meta["ravn/call-id"]
    status = await rig.http.get(f"/v1/calls/{cid}", headers=rig.headers())
    assert status.status_code == 200, status.text
    data = status.json()
    assert data["status"] == "succeeded" and data["result_available"] is False
    assert data["session_id"] == session["id"]
    assert len(data["arguments_fingerprint"]) == 64
    assert "result" not in data and "arguments" not in data
    assert len(rig.fake.calls) == 1
    for headers in rig.fake.headers:
        assert headers["authorization"] == f"Bearer {ALICE}"
        assert headers["x-mcp-readonly"] == "true"
        assert "x-ravn-user-id" not in headers


async def test_concurrent_alice_bob_are_isolated(rig):
    alice = await rig.session(await rig.connection())
    bob = await rig.session(await rig.connection("bob", BOB), "bob")

    async def call(session):
        async with rig.mcp(session) as mcp:
            return (await mcp.call_tool("issue_read", ARGS)).content[0].text

    assert await asyncio.gather(call(alice), call(bob)) == ["alice issue", "bob issue"]
    assert {c[0] for c in rig.fake.calls} == {ALICE, BOB}


async def test_no_plaintext_credentials_or_results_in_database(rig):
    session = await rig.session(await rig.connection())
    async with rig.mcp(session) as mcp:
        await mcp.call_tool("issue_read", ARGS)
    async with rig.service.store.transaction() as db:
        dump = "\n".join([line async for line in db.iterdump()])
    for secret in [ALICE, rig.key, session["token"], "alice issue"]:
        assert secret not in dump


@pytest.mark.parametrize("operation", ["get", "disconnect", "session"])
async def test_cross_user_access_rejected(rig, operation):
    conn = await rig.connection()
    headers = rig.headers("bob")
    if operation == "get":
        response = await rig.http.get(f"/v1/connections/{conn['id']}", headers=headers)
    elif operation == "disconnect":
        response = await rig.http.post(f"/v1/connections/{conn['id']}/disconnect", headers=headers)
    else:
        response = await rig.http.post(
            "/v1/sessions", headers=headers, json={"connection_id": conn["id"]}
        )
    assert response.status_code == 404
    assert not rig.fake.calls


async def test_cross_app_and_tenant_isolation(rig):
    conn = await rig.connection(tenant="acme", key=rig.saas_key)
    for headers in [rig.headers(tenant="other", key=rig.saas_key), rig.headers()]:
        response = await rig.http.get(f"/v1/connections/{conn['id']}", headers=headers)
        assert response.status_code == 404
    listing = await rig.http.get(
        "/v1/connections", headers=rig.headers(tenant="other", key=rig.saas_key)
    )
    assert listing.json()["data"] == []


@pytest.mark.parametrize(
    "headers",
    [
        {"tenant": "not-default"},
        {"tenant": ""},
        {"user": ""},
    ],
)
async def test_invalid_actor_headers(rig, headers):
    response = await rig.http.get("/v1/connections", headers=rig.headers(**headers))
    assert response.status_code == 400


async def test_multi_tenant_requires_tenant(rig):
    response = await rig.http.get("/v1/connections", headers=rig.headers(key=rig.saas_key))
    assert response.status_code == 400


async def test_session_cannot_call_management_and_app_key_cannot_call_mcp(rig):
    session = await rig.session(await rig.connection())
    response = await rig.http.get("/v1/connections", headers=rig.headers(key=session["token"]))
    assert response.status_code == 401
    response = await rig.http.post(
        "/mcp",
        headers={"Authorization": "Bearer " + rig.key},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "name,args",
    [
        ("add_issue_comment", {}),
        ("delete_repository", {}),
        ("issue_read", {**ARGS, "method": "get_comments"}),
        ("issue_read", {**ARGS, "owner": "../alice"}),
        ("issue_read", {**ARGS, "repo": "repo?token=secret"}),
        ("issue_read", {**ARGS, "connection_id": "other"}),
        ("issue_read", {**ARGS, "issue_number": True}),
    ],
)
async def test_writes_and_argument_escape_hatches_denied(rig, name, args):
    session = await rig.session(await rig.connection())
    async with rig.mcp(session) as mcp:
        with pytest.raises(MCPError):
            await mcp.call_tool(name, args)
    assert rig.fake.calls == []


async def test_session_ttl_and_revocation(rig):
    conn = await rig.connection()
    before = datetime.now(UTC)
    session = await rig.session(conn)
    lifetime = (
        datetime.fromisoformat(session["expires_at"].replace("Z", "+00:00")) - before
    ).total_seconds()
    assert 3595 <= lifetime <= 3610
    for ttl in [59, 14401, True, "3600"]:
        response = await rig.http.post(
            "/v1/sessions",
            headers=rig.headers(),
            json={"connection_id": conn["id"], "ttl_seconds": ttl},
        )
        assert response.status_code == 400
    revoke = await rig.http.post(f"/v1/sessions/{session['id']}/revoke", headers=rig.headers())
    assert revoke.status_code == 200
    assert revoke.json()["status"] == "revoked"
    response = await rig.http.post(
        "/mcp", headers={"Authorization": "Bearer " + session["token"]}, json={}
    )
    assert response.status_code == 401


async def test_expired_session_rejected(rig):
    session = await rig.session(await rig.connection())
    async with rig.service.store.transaction() as db:
        await db.execute(
            "UPDATE sessions SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (session["id"],)
        )
    response = await rig.http.post(
        "/mcp", headers={"Authorization": "Bearer " + session["token"]}, json={}
    )
    assert response.status_code == 401


async def test_disconnect_stops_execution_not_history(rig):
    conn = await rig.connection()
    session = await rig.session(conn)
    async with rig.mcp(session) as mcp:
        call = await mcp.call_tool("issue_read", ARGS)
    for _ in range(2):
        response = await rig.http.post(
            f"/v1/connections/{conn['id']}/disconnect", headers=rig.headers()
        )
        assert response.status_code == 200
        assert response.json()["provider_revocation"] == "unsupported"
        assert response.json()["connection"]["epoch"] == 2
    async with rig.service.store.transaction() as db:
        row = await one(db, "SELECT ciphertext FROM connections WHERE id=?", (conn["id"],))
        assert row["ciphertext"] is None
    response = await rig.http.get(f"/v1/calls/{call.meta['ravn/call-id']}", headers=rig.headers())
    assert response.status_code == 200
    response = await rig.http.post(
        "/v1/sessions", headers=rig.headers(), json={"connection_id": conn["id"]}
    )
    assert response.status_code == 409


async def test_revocation_during_discovery_prevents_dispatch(rig):
    conn = await rig.connection()
    session = await rig.session(conn)
    async with rig.mcp(session) as mcp:
        rig.fake.block_list = True
        pending = asyncio.create_task(mcp.call_tool("issue_read", ARGS))
        await asyncio.wait_for(rig.fake.entered.wait(), 5)
        await rig.http.post(f"/v1/sessions/{session['id']}/revoke", headers=rig.headers())
        rig.fake.release.set()
        with pytest.raises(MCPError):
            await pending
    assert rig.fake.calls == []


async def test_already_admitted_read_may_finish_after_revocation(rig):
    conn = await rig.connection()
    session = await rig.session(conn)
    async with rig.mcp(session) as mcp:
        await mcp.list_tools()
        rig.fake.block_call = True
        pending = asyncio.create_task(mcp.call_tool("issue_read", ARGS))
        await asyncio.wait_for(rig.fake.entered.wait(), 5)
        await rig.http.post(f"/v1/connections/{conn['id']}/disconnect", headers=rig.headers())
        rig.fake.release.set()
        assert (await pending).content[0].text == "alice issue"


async def test_schema_drift_fails_closed(rig):
    session = await rig.session(await rig.connection())
    rig.fake.tools[0].input_schema = {"type": "object"}
    async with rig.mcp(session) as mcp:
        with pytest.raises(MCPError):
            await mcp.call_tool("issue_read", ARGS)
    assert rig.fake.calls == []


async def test_upstream_error_and_credential_echo_are_not_success(rig):
    session = await rig.session(await rig.connection())
    async with rig.mcp(session) as mcp:
        rig.fake.fail = True
        result = await mcp.call_tool("issue_read", ARGS)
        assert result.is_error
        rig.fake.fail, rig.fake.echo = False, True
        with pytest.raises(MCPError) as error:
            await mcp.call_tool("issue_read", ARGS)
        assert ALICE not in str(error.value)


async def test_import_and_validation_errors_never_echo_credentials(rig):
    value = "invalid_very_sensitive_secret_123"
    for credential in [{"type": "bearer", "token": value}, {"type": "incorrect", "token": value}]:
        response = await rig.http.post(
            "/v1/connections/import",
            headers=rig.headers(),
            json={"integration_id": "github", "credential": credential},
        )
        assert response.status_code in {400, 401}
        assert value not in response.text


async def test_duplicate_headers_and_json_keys_rejected(rig):
    headers = list(rig.headers().items()) + [("X-Ravn-User-Id", "bob")]
    assert (await rig.http.get("/v1/connections", headers=headers)).status_code == 400
    body = '{"integration_id":"github","integration_id":"evil"}'
    response = await rig.http.post(
        "/v1/connections/import",
        content=body,
        headers={**rig.headers(), "Content-Type": "application/json"},
    )
    assert response.status_code == 400


async def test_request_size_host_and_origin_limits(rig):
    response = await rig.http.post(
        "/v1/connections/import",
        content=b"x" * 1048577,
        headers={**rig.headers(), "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    for extra in [{"Host": "attacker.example"}, {"Origin": "https://attacker.example"}]:
        response = await rig.http.get("/v1/connections", headers={**rig.headers(), **extra})
        assert response.status_code in {400, 403}


async def test_pagination_context_binding_and_no_token_leaks(rig):
    conn = await rig.connection()
    sessions = [await rig.session(conn) for _ in range(3)]
    response = await rig.http.get("/v1/sessions?limit=1", headers=rig.headers())
    assert len(response.json()["data"]) == 1
    cursor = response.json()["next_cursor"]
    assert cursor
    for session in sessions:
        assert session["token"] not in response.text
    page = await rig.http.get("/v1/sessions", params={"cursor": cursor}, headers=rig.headers())
    assert page.status_code == 200 and len(page.json()["data"]) == 1
    assert page.json()["data"][0]["id"] != response.json()["data"][0]["id"]
    foreign = await rig.http.get(
        "/v1/sessions", params={"cursor": cursor}, headers=rig.headers("bob")
    )
    assert foreign.status_code == 400


async def test_key_rotation_and_compromise_revocation(rig):
    conn = await rig.connection()
    session = await rig.session(conn)
    new = await rig.service.create_key("demo", "replacement")
    old_id = "key_" + rig.key.split("_", 3)[2]
    await rig.service.revoke_key(old_id)
    assert (await rig.http.get("/v1/connections", headers=rig.headers())).status_code == 401
    assert (
        await rig.http.get("/v1/connections", headers=rig.headers(key=new["key"]))
    ).status_code == 200
    await rig.service.authenticate_session(session["token"])
    await rig.service.revoke_key(old_id, revoke_sessions=True)
    with pytest.raises(RavnError):
        await rig.service.authenticate_session(session["token"])


async def test_extension_routes_not_misrepresented_as_shipped(rig):
    for path in [
        "/openapi.json",
        "/docs",
        "/v1/connect-sessions",
        "/v1/calls",
        "/admin/v1/app-keys",
    ]:
        response = await rig.http.get(path, headers=rig.headers())
        assert response.status_code == 404


async def test_foreign_call_history_hidden(rig):
    session = await rig.session(await rig.connection())
    async with rig.mcp(session) as mcp:
        result = await mcp.call_tool("issue_read", ARGS)
    response = await rig.http.get(
        f"/v1/calls/{result.meta['ravn/call-id']}", headers=rig.headers("bob")
    )
    assert response.status_code == 404
