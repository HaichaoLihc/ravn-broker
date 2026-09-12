import asyncio
import sqlite3
import time
from dataclasses import replace
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from conftest import ALICE, BOB

from ravn.app import create_admin_app
from ravn.common import RavnError, digest
from ravn.console import COOKIE, PREFIX, Console, create_console_app
from ravn.service import Service
from ravn.store import one

pytestmark = pytest.mark.anyio


async def test_schema_one_migration_preserves_history(config):
    path = config.storage.sqlite_path
    path.parent.mkdir(mode=0o700)
    with sqlite3.connect(path) as db:
        db.executescript(files("ravn").joinpath("migrations/001_initial.sql").read_text())
        db.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?)",
            (
                "evt_legacy",
                "demo",
                "default",
                "alice",
                "session.created",
                "sess_old",
                "2026-09-11T00:00:00.000000Z",
            ),
        )
    service = Service(config)
    await service.start()
    try:
        async with service.store.transaction() as db:
            assert (await one(db, "PRAGMA user_version"))["user_version"] == 3
            row = await one(db, "SELECT * FROM events WHERE id='evt_legacy'")
            assert row["user_id"] == "alice" and row["actor_kind"] is None
            plan = await one(
                db,
                "EXPLAIN QUERY PLAN SELECT * FROM events WHERE app_id=? ORDER BY created_at DESC,id DESC LIMIT 50",
                ("demo",),
            )
            assert "events_app_time" in plan["detail"]
    finally:
        await service.close()


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


async def test_local_auth_ticket_single_use_expiry_and_cookie(rig):
    state = Console(rig.service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_console_app(state)), base_url=state.origin
    ) as http:
        assert (await http.get(PREFIX + "/bootstrap")).status_code == 401
        ticket = state.ticket()["url"].split("#ticket=")[1]
        assert ticket not in repr(state.tickets)
        response = await http.post(
            PREFIX + "/auth/exchange", headers={"Origin": state.origin}, json={"ticket": ticket}
        )
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/console" in cookie
        value = http.cookies.get(COOKIE)
        assert value not in repr(state.sessions)
        assert (
            await http.post(
                PREFIX + "/auth/exchange", headers={"Origin": state.origin}, json={"ticket": ticket}
            )
        ).status_code == 401
        expired = state.ticket()["url"].split("#ticket=")[1]
        state.tickets[digest(expired)] = time.monotonic() - 1
        assert (
            await http.post(
                PREFIX + "/auth/exchange",
                headers={"Origin": state.origin},
                json={"ticket": expired},
            )
        ).status_code == 401
        state.close()
        assert (await http.get(PREFIX + "/bootstrap")).status_code == 401


async def test_logout_and_session_expiration(console):
    state, http = console
    assert (await http.get(PREFIX + "/auth/session")).status_code == 200
    key = next(iter(state.sessions))
    state.sessions[key] = replace(state.sessions[key], deadline=time.monotonic() - 1)
    assert (await http.get(PREFIX + "/bootstrap")).status_code == 401
    ticket = state.ticket()["url"].split("#ticket=")[1]
    response = await http.post(PREFIX + "/auth/exchange", json={"ticket": ticket})
    http.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    assert (await http.post(PREFIX + "/auth/logout", json={})).status_code == 200
    assert (await http.get(PREFIX + "/bootstrap")).status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Host": "evil.example"},
        {"Authorization": "Bearer rv_app_fake"},
        {"X-Ravn-User-Id": "alice"},
    ],
)
async def test_wrong_origin_host_or_credential_type_rejected(console, headers):
    _, http = console
    assert (await http.get(PREFIX + "/bootstrap", headers=headers)).status_code in {400, 401, 403}


@pytest.mark.parametrize(
    "headers",
    [
        {"X-CSRF-Token": "bad"},
        {"X-CSRF-Token": ""},
        {"Origin": ""},
    ],
)
async def test_mutations_require_csrf_and_origin(console, headers):
    _, http = console
    assert (await http.post(PREFIX + "/auth/logout", headers=headers, json={})).status_code == 403
    assert (await http.get(PREFIX + "/bootstrap")).status_code == 200


async def test_duplicate_cookie_and_security_headers_rejected(console):
    state, http = console
    value = http.cookies.get(COOKIE)
    for headers in [
        [("Cookie", COOKIE + "=" + value), ("Cookie", COOKIE + "=" + value)],
        [("Cookie", COOKIE + "=" + value + "; " + COOKIE + "=" + value)],
        [("Host", urlsplit(state.origin).netloc), ("Host", "evil.example")],
    ]:
        response = await http.get(PREFIX + "/bootstrap", headers=headers)
        assert response.status_code in {400, 401}


async def test_ports_do_not_expose_each_others_authority(rig, console):
    _, http = console
    for path in ["/v1/connections", "/mcp", "/admin/v1/app-keys", "/admin/v1/console-tickets"]:
        assert (await http.post(path, json={})).status_code == 404
    for path in [PREFIX + "/bootstrap", PREFIX + "/connections", "/admin/v1/console-tickets"]:
        assert (await rig.http.get(path)).status_code == 404
    response = await rig.http.get(
        "/v1/connections", headers={"Cookie": COOKIE + "=" + http.cookies.get(COOKIE)}
    )
    assert response.status_code == 401


async def test_admin_ticket_route_is_optional(rig):
    for enabled in (False, True):
        state = Console(rig.service) if enabled else None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_admin_app(rig.service, state)),
            base_url="http://ravn-admin",
        ) as admin:
            response = await admin.post("/admin/v1/console-tickets", json={})
            assert response.status_code == (201 if enabled else 404)


async def test_operator_queries_all_users_but_no_secrets(rig, console):
    _, http = console
    alice = await rig.connection()
    bob = await rig.connection("bob", BOB)
    session = await rig.session(alice)
    async with rig.mcp(session) as mcp:
        await mcp.call_tool(
            "issue_read", {"owner": "acme", "repo": "demo", "method": "get", "issue_number": 1}
        )
    serialized = []
    for endpoint in [
        "bootstrap",
        "connections?app_id=demo",
        "sessions?app_id=demo",
        "calls?app_id=demo",
        "events?app_id=demo",
        "app-keys?app_id=demo",
    ]:
        response = await http.get(PREFIX + "/" + endpoint)
        assert response.status_code == 200, response.text
        serialized.append(response.text)
    result = (await http.get(PREFIX + "/connections?app_id=demo")).json()
    assert {row["id"] for row in result["data"]} == {alice["id"], bob["id"]}
    filtered = await http.get(PREFIX + "/connections?app_id=demo&user_id=bob")
    assert [r["id"] for r in filtered.json()["data"]] == [bob["id"]]
    for value in [
        ALICE,
        BOB,
        rig.key,
        session["token"],
        "alice issue",
        "ciphertext",
        "secret_hash",
        "token_hash",
        "master.key",
    ]:
        assert value not in "\n".join(serialized)
    # The ordinary backend API is still user-scoped, not broadened by console queries.
    assert (
        await rig.http.get(f"/v1/connections/{alice['id']}", headers=rig.headers("bob"))
    ).status_code == 404


@pytest.mark.parametrize("target", ["connections", "sessions", "calls"])
async def test_details_cannot_cross_namespace(rig, console, target):
    _, http = console
    conn = await rig.connection(tenant="acme", key=rig.saas_key)
    session = await rig.session(conn, tenant="acme", key=rig.saas_key)
    async with rig.mcp(session) as mcp:
        result = await mcp.call_tool(
            "issue_read", {"owner": "acme", "repo": "demo", "method": "get", "issue_number": 1}
        )
    identity = {
        "connections": conn["id"],
        "sessions": session["id"],
        "calls": result.meta["ravn/call-id"],
    }[target]
    assert (
        await http.get(PREFIX + f"/{target}/{identity}?app_id=saas&tenant_id=acme")
    ).status_code == 200
    for params in ["app_id=saas&tenant_id=other", "app_id=demo&tenant_id=acme"]:
        assert (await http.get(PREFIX + f"/{target}/{identity}?{params}")).status_code == 404


async def test_operator_disconnect_uses_revision_and_revokes_sessions(rig, console):
    _, http = console
    conn = await rig.connection()
    session = await rig.session(conn)
    path = PREFIX + f"/connections/{conn['id']}/disconnect"
    payload = {"app_id": "demo", "tenant_id": "default"}
    assert (
        await http.post(path, json={**payload, "tenant_id": "other"}, headers={"If-Match": '"1"'})
    ).status_code == 404
    assert (await http.post(path, json=payload)).status_code == 412
    assert (await http.post(path, json=payload, headers={"If-Match": '"99"'})).status_code == 412
    assert (await http.post(path, json=payload, headers={"If-Match": '"1"'})).status_code == 200
    assert (await http.post(path, json=payload, headers={"If-Match": '"1"'})).status_code == 200
    detail = (
        await http.get(PREFIX + f"/sessions/{session['id']}?app_id=demo&tenant_id=default")
    ).json()
    assert detail["status"] == "revoked" and detail["current_tools"] == []
    async with rig.service.store.transaction() as db:
        row = await one(db, "SELECT ciphertext FROM connections WHERE id=?", (conn["id"],))
        assert row["ciphertext"] is None
    assert (
        await rig.http.post(
            "/mcp", headers={"Authorization": "Bearer " + session["token"]}, json={}
        )
    ).status_code in {401, 409}


async def test_single_session_revoke_leaves_other_session_working(rig, console):
    _, http = console
    conn = await rig.connection()
    first, second = await rig.session(conn), await rig.session(conn)
    path = PREFIX + f"/sessions/{first['id']}/revoke"
    assert (
        await http.post(path, json={"app_id": "demo", "tenant_id": "default"})
    ).status_code == 200
    assert (
        await http.post(path, json={"app_id": "demo", "tenant_id": "default"})
    ).status_code == 200
    await rig.service.authenticate_session(second["token"])
    events = (await http.get(PREFIX + "/events?app_id=demo&kind=session.revoked")).json()["data"]
    assert len(events) == 1 and events[0]["actor_kind"] == "local_operator"
    assert events[0]["user_id"] == "alice" and events[0]["actor_id"].startswith("op_")
    assert events[0]["request_id"].startswith("req_")


async def test_operator_can_reduce_access_on_disabled_app(rig, console):
    _, http = console
    conn = await rig.connection()
    session = await rig.session(conn)
    rig.service.config = rig.service.config.model_copy(
        update={
            "applications": [
                a.model_copy(update={"enabled": False}) for a in rig.service.config.applications
            ]
        }
    )
    assert (await http.get(PREFIX + "/bootstrap")).json()["applications"][0]["enabled"] is False
    assert (
        await http.post(
            PREFIX + f"/sessions/{session['id']}/revoke",
            json={"app_id": "demo", "tenant_id": "default"},
        )
    ).status_code == 200
    assert (
        await http.post(
            PREFIX + f"/connections/{conn['id']}/disconnect",
            json={"app_id": "demo", "tenant_id": "default"},
            headers={"If-Match": '"1"'},
        )
    ).status_code == 200


async def test_key_revoke_routine_then_compromise(rig, console):
    _, http = console
    session = await rig.session(await rig.connection())
    key = (await http.get(PREFIX + "/app-keys?app_id=demo")).json()["data"][0]
    path = PREFIX + f"/app-keys/{key['id']}/revoke"
    assert (await http.post(path, json={"app_id": "saas"})).status_code == 404
    assert (
        await http.post(path, json={"app_id": "demo", "revoke_sessions": False})
    ).status_code == 200
    await rig.service.authenticate_session(session["token"])
    assert (
        await http.post(path, json={"app_id": "demo", "revoke_sessions": True})
    ).status_code == 200
    response = await rig.http.post(
        "/mcp", headers={"Authorization": "Bearer " + session["token"]}, json={}
    )
    assert response.status_code == 401


async def test_cursor_bound_to_operator_filters_and_app(rig, console):
    state, http = console
    for _ in range(3):
        await rig.connection()
    first = (await http.get(PREFIX + "/connections?app_id=demo&limit=1")).json()
    cursor = first["next_cursor"]
    second = await http.get(
        PREFIX + "/connections", params={"app_id": "demo", "limit": 1, "cursor": cursor}
    )
    assert second.status_code == 200 and second.json()["data"][0]["id"] != first["data"][0]["id"]
    assert (
        await http.get(
            PREFIX + "/connections", params={"app_id": "saas", "limit": 1, "cursor": cursor}
        )
    ).status_code == 400
    ticket = state.ticket()["url"].split("#ticket=")[1]
    await http.post(PREFIX + "/auth/exchange", json={"ticket": ticket})
    assert (
        await http.get(
            PREFIX + "/connections", params={"app_id": "demo", "limit": 1, "cursor": cursor}
        )
    ).status_code == 400


@pytest.mark.parametrize(
    "path",
    [
        "/connections",
        "/connections?app_id=demo&app_id=saas",
        "/connections?app_id=demo&limit=101",
        "/connections?app_id=demo&sql=anything",
        "/events?scope=deployment&app_id=demo",
        "/calls?app_id=demo&from=2026-01-01T00:00:00Z&to=2026-09-11T00:00:00Z",
        "/calls?app_id=demo&from=2026-09-11T00:00:00",
        "/sessions?app_id=demo&connection_id=bad%20id",
    ],
)
async def test_invalid_queries_fail_closed(console, path):
    _, http = console
    assert (await http.get(PREFIX + path)).status_code == 400


async def test_deployment_events_do_not_leak_into_app_activity(console):
    _, http = console
    app = (await http.get(PREFIX + "/events?app_id=demo")).json()["data"]
    deployment = (await http.get(PREFIX + "/events?scope=deployment")).json()["data"]
    assert all(r["app_id"] == "demo" for r in app)
    assert deployment and all(r["app_id"] is None for r in deployment)


async def test_original_ui_assets_served_locally_with_csp(console):
    _, http = console
    response = await http.get("/console/")
    assert response.status_code == 200
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert (await http.get("/console/reference/database.svg")).status_code == 200
    assert (
        await http.get("/console/reference/a2797872-d5ec-41e6-8e2f-67f534b6588f.woff2")
    ).status_code == 200
    assert (await http.get("/console/reference/../../master.key")).status_code == 404


async def test_audit_failure_rolls_back_revocation(rig, console, monkeypatch):
    _, http = console
    conn = await rig.connection()

    async def fail(*args, **kwargs):
        raise RuntimeError("Synthetic audit failure")

    monkeypatch.setattr("ravn.control.event", fail)
    response = await http.post(
        PREFIX + f"/connections/{conn['id']}/disconnect",
        json={"app_id": "demo", "tenant_id": "default"},
        headers={"If-Match": '"1"'},
    )
    assert response.status_code == 503
    row = (
        await http.get(PREFIX + f"/connections/{conn['id']}?app_id=demo&tenant_id=default")
    ).json()
    assert row["status"] == "active" and row["revision"] == 1


async def test_console_revoke_before_admission_blocks_pending_read(rig, console):
    _, http = console
    session = await rig.session(await rig.connection())
    principal = await rig.service.authenticate_session(session["token"])
    rig.fake.block_list = True
    task = asyncio.create_task(
        rig.service.execute(
            principal,
            principal.connection_id,
            "issue_read",
            {"owner": "acme", "repo": "demo", "method": "get", "issue_number": 1},
            "req_test",
        )
    )
    await rig.fake.entered.wait()
    await http.post(
        PREFIX + f"/sessions/{session['id']}/revoke",
        json={"app_id": "demo", "tenant_id": "default"},
    )
    rig.fake.release.set()
    with pytest.raises(RavnError):
        await task
    assert not rig.fake.calls
