"""Registration is persistent operator state, usable without a server restart."""

import asyncio
import json

import httpx
import pytest
from conftest import ALICE
from cryptography.exceptions import InvalidTag

from ravn.app import create_admin_app
from ravn.common import RavnError
from ravn.console import PREFIX, create_console_app
from ravn.provider import RemoteMCP
from ravn.service import Service
from ravn.store import one, rows

pytestmark = pytest.mark.anyio
SECRET = "registration-test-secret"


def registration(**patch):
    return {
        "app_id": "demo",
        "id": "dynamic",
        "endpoint": "https://mcp.records.example/mcp/",
        "identity": {
            "endpoint": "https://identity.records.example/user",
            "id_field": "id",
            "display_field": "login",
        },
        "oauth": {
            "client_id": "client",
            "client_secret": SECRET,
            "authorization_endpoint": "https://auth.example/authorize",
            "token_endpoint": "https://auth.example/token",
            "issuer": "https://auth.example",
            "scopes": ["records.read"],
        },
        **patch,
    }


async def test_console_register_admin_list_restart_and_encryption(rig, console):
    state, http = console
    response = await http.post(PREFIX + "/integrations", json=registration())
    assert response.status_code == 201, response.text
    item = response.json()
    assert item["callback_url"] == "http://127.0.0.1:8787/oauth/callback/demo/dynamic"
    assert SECRET not in response.text and "client_secret" not in item["oauth"]
    assert (
        await http.post(
            PREFIX + "/integrations", json=registration(endpoint="https://other.example/mcp")
        )
    ).status_code == 409
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_admin_app(rig.service)), base_url="http://admin"
    ) as admin:
        listed = await admin.get("/admin/v1/integrations")
        assert item in listed.json()["data"] and SECRET not in listed.text
        assert (
            await admin.post("/admin/v1/integrations", json=registration(app_id="saas"))
        ).status_code == 201
    bootstrap = await http.get(PREFIX + "/bootstrap")
    assert item in bootstrap.json()["integrations"] and SECRET not in bootstrap.text
    async with rig.service.store.transaction() as db:
        stored = await one(db, "SELECT * FROM integrations WHERE app_id='demo' AND id='dynamic'")
        assert SECRET.encode() not in stored["ciphertext"]
        assert SECRET in rig.service.cipher.decrypt(
            stored["ciphertext"], "demo", "", "integration:dynamic"
        )
        with pytest.raises(InvalidTag):
            rig.service.cipher.decrypt(stored["ciphertext"], "saas", "", "integration:dynamic")
        events = await rows(
            db, "SELECT * FROM events WHERE kind='integration.created' AND subject_id='dynamic'"
        )
        assert {e["actor_id"] for e in events} == {
            "admin_socket",
            next(iter(state.sessions.values())).id,
        }
        assert SECRET not in json.dumps(events)
    # SQLite is authoritative across restart.
    await rig.service.close()
    restarted = Service(rig.service.config)
    try:
        await restarted.start()
        restored = restarted.integrations.get("demo", "dynamic")
        assert restored.oauth.client_secret.get_secret_value() == SECRET
        assert restarted.provider_for(restored).integration.endpoint == item["endpoint"]
    finally:
        await restarted.close()


async def test_registered_integration_calls_and_disable_blocks_sessions(rig, console):
    _, http = console
    assert (
        await http.post(PREFIX + "/integrations", json=registration(oauth=None))
    ).status_code == 201
    integration = rig.service.integrations.get("demo", "dynamic")
    rig.service.providers[("demo", "dynamic")] = RemoteMCP(
        integration, transport_factory=rig.fake.transport
    )
    response = await rig.http.post(
        "/v1/connections/import",
        headers=rig.headers(),
        json={"integration_id": "dynamic", "credential": {"type": "bearer", "token": ALICE}},
    )
    assert response.status_code == 201, response.text
    conn = response.json()
    session = await rig.session(conn)
    async with rig.mcp(session) as client:
        assert (await client.list_tools()).tools
        assert not (
            await client.call_tool(
                client.ravn_name("list_issues"), {"owner": "acme", "repo": "test"}
            )
        ).is_error
    path = PREFIX + "/integrations/demo/dynamic/disable"
    assert (await http.post(path)).json()["enabled"] is False
    assert (await http.post(path)).status_code == 200
    principal = await rig.service.authenticate_session(session["token"])
    with pytest.raises(RavnError) as error:
        await rig.service.discover(principal, conn["id"])
    assert error.value.code == "connection_disabled"
    assert (
        await rig.http.post(
            "/v1/sessions", headers=rig.headers(), json={"connection_ids": [conn["id"]]}
        )
    ).status_code == 409
    detail = await http.get(PREFIX + f"/connections/{conn['id']}?app_id=demo&tenant_id=default")
    assert detail.json()["broker_access"] == "blocked"
    await rig.service.close()
    await rig.service.start()
    assert not rig.service.integrations.get("demo", "dynamic").enabled
    principal = await rig.service.authenticate_session(session["token"])
    with pytest.raises(RavnError):
        await rig.service.discover(principal, conn["id"])


async def test_disable_wins_before_tool_dispatch(rig, console):
    _, http = console
    session = await rig.session(await rig.connection())
    p = await rig.service.authenticate_session(session["token"])
    rig.fake.block_list = True
    call = asyncio.create_task(
        rig.service.execute(
            p,
            session["connections"][0]["id"],
            "list_issues",
            {"owner": "acme", "repo": "test"},
            "req_disable",
        )
    )
    await asyncio.wait_for(rig.fake.entered.wait(), 2)
    assert (await http.post(PREFIX + "/integrations/demo/records/disable")).status_code == 200
    rig.fake.release.set()
    with pytest.raises(RavnError):
        await call
    assert not rig.fake.calls
    await rig.service.close()
    await rig.service.start()
    assert not rig.service.integrations.get(
        "demo", "records"
    ).enabled  # Restart cannot re-enable it.


@pytest.mark.parametrize(
    "patch",
    [
        {"endpoint": "http://unsafe.example/mcp"},
        {"id": "../other"},
        {"oauth": {**registration()["oauth"], "client_secret_file": "/private/secret"}},
        {"oauth": {**registration()["oauth"], "client_secret": "bad\nsecret"}},
        {
            "oauth": {
                **registration()["oauth"],
                "authorization_params": {"redirect_uri": "https://bad.example"},
            }
        },
    ],
)
async def test_invalid_registration_is_not_saved_or_echoed(console, patch):
    _, http = console
    response = await http.post(PREFIX + "/integrations", json=registration(**patch))
    assert response.status_code == 400 and SECRET not in response.text
    assert not any(
        i["id"] == "dynamic" for i in (await http.get(PREFIX + "/integrations")).json()["data"]
    )


async def test_registration_auth_and_namespace_boundaries(rig, console):
    state, http = console
    body = registration()
    assert (
        await http.post(PREFIX + "/integrations", json={**body, "app_id": "missing"})
    ).status_code == 404
    assert (await http.post(PREFIX + "/integrations/demo/missing/disable")).status_code == 404
    assert (
        await http.post(PREFIX + "/integrations", headers={"X-CSRF-Token": "wrong"}, json=body)
    ).status_code == 403
    assert (
        await http.post(
            PREFIX + "/integrations", headers={"Origin": "https://other.example"}, json=body
        )
    ).status_code == 403
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_console_app(state)), base_url=state.origin
    ) as anonymous:
        assert (await anonymous.get(PREFIX + "/integrations")).status_code == 401
        assert (
            await anonymous.post(
                PREFIX + "/integrations", headers={"Origin": state.origin}, json=body
            )
        ).status_code == 401
    for headers in (
        rig.headers(),
        {"Authorization": "Bearer " + (await rig.session(await rig.connection()))["token"]},
    ):
        assert (
            await rig.http.post("/admin/v1/integrations", headers=headers, json=body)
        ).status_code == 404
        assert (
            await http.post(PREFIX + "/integrations", headers=headers, json=body)
        ).status_code == 401


async def test_public_oauth_client_registration_without_secret(rig, console):
    _, http = console
    oauth = {**registration()["oauth"], "token_endpoint_auth_method": "none"}
    del oauth["client_secret"]
    result = await http.post(
        PREFIX + "/integrations", json=registration(oauth=oauth, identity=None)
    )
    assert result.status_code == 201, result.text
    assert result.json()["oauth_configured"]
    assert "client_secret" not in result.json()["oauth"]
    await rig.service.close()
    await rig.service.start()
    assert rig.service.integrations.get("demo", "dynamic").oauth.client_secret is None
    oauth["token_endpoint_auth_method"] = "client_secret_post"
    assert (
        await http.post(
            PREFIX + "/integrations", json=registration(id="missing-secret", oauth=oauth)
        )
    ).status_code == 400
