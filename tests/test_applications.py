import json

import httpx
import pytest

from ravn.app import create_admin_app
from ravn.common import RavnError, digest
from ravn.console import COOKIE, PREFIX, Console, create_console_app
from ravn.service import Service
from ravn.store import one, rows

pytestmark = pytest.mark.anyio


async def test_empty_deployment_create_app_and_issue_key_without_restart(config):
    service = Service(config)
    await service.start()
    state = Console(service)
    try:
        ticket = state.ticket()["url"].split("#ticket=")[1]
        principal, cookie = await state.exchange(ticket, "test-login")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_console_app(state)),
            base_url=state.origin,
            cookies={COOKIE: cookie},
            headers={"Origin": state.origin, "X-CSRF-Token": principal.csrf},
        ) as http:
            assert not service.applications.items and not service.integrations.items
            for view in ("", "?view=applications", "?view=connections", "?view=server"):
                page = await http.get("/console/" + view)
                assert page.status_code == 200
                if "server" not in view:
                    assert "Create your first application" in page.text
            body = {"id": "assistant", "return_urls": ["https://app.example/return"]}
            created = await http.post(PREFIX + "/applications", json=body)
            assert created.status_code == 201
            assert (await http.post(PREFIX + "/applications", json=body)).status_code == 409
            issued = await http.post(
                PREFIX + "/app-keys", json={"app_id": "assistant", "label": "prod"}
            )
            assert issued.status_code == 201
            key = issued.json()["key"]
            p = await service.authenticate_app(key, "alice", None)
            assert p.app == "assistant" and p.user == "alice"
            assert (await service.page(p, "connections"))["data"] == []
            for path in (
                PREFIX + "/bootstrap",
                PREFIX + "/app-keys?app_id=assistant",
                "/console/?view=app-keys&app_id=assistant",
            ):
                response = await http.get(path)
                assert response.status_code == 200 and key not in response.text
            async with service.store.transaction() as db:
                stored = await one(db, "SELECT * FROM app_keys WHERE id=?", (issued.json()["id"],))
                assert stored["secret_hash"] == digest(key)
                assert key not in json.dumps(stored)
                events = await rows(db, "SELECT * FROM events WHERE app_id='assistant'")
                assert {e["kind"] for e in events} == {
                    "application.created",
                    "application_key.created",
                }
                assert all(e["actor_id"] == principal.id for e in events)
                assert key not in json.dumps(events)
        await service.close()
        await service.start()
        assert service.applications.get("assistant").return_urls == body["return_urls"]
        assert (await service.authenticate_app(key, "alice", None)).app == "assistant"
    finally:
        state.close()
        await service.close()


async def test_application_updates_enforce_access_and_return_urls(rig, console):
    _, http = console
    body = {"id": "demo", "enabled": False, "return_urls": ["https://app.example/return"]}
    session = await rig.session(await rig.connection())
    runtime = await rig.service.authenticate_session(session["token"])
    assert (await http.put(PREFIX + "/applications/demo", json=body)).status_code == 200
    assert (await rig.http.get("/v1/connections", headers=rig.headers())).status_code == 401
    with pytest.raises(RavnError):
        await rig.service.discover(runtime, session["connections"][0]["id"])
    assert (await http.post(PREFIX + "/app-keys", json={"app_id": "demo"})).status_code == 404
    body["enabled"] = True
    assert (await http.put(PREFIX + "/applications/demo", json=body)).status_code == 200
    assert (await rig.http.get("/v1/connections", headers=rig.headers())).status_code == 200
    assert rig.service.applications.get("demo").return_urls == body["return_urls"]
    assert (
        await http.put(PREFIX + "/applications/demo", json={**body, "tenant_mode": "multi"})
    ).status_code == 409
    assert (
        await http.put(PREFIX + "/applications/demo", json={**body, "id": "saas"})
    ).status_code == 400
    assert (
        await http.put(PREFIX + "/applications/missing", json={"id": "missing"})
    ).status_code == 404
    assert (
        await http.put(
            PREFIX + "/applications/demo", json={**body, "return_urls": ["http://evil.example/"]}
        )
    ).status_code == 400
    await rig.service.close()
    await rig.service.start()
    assert rig.service.applications.get("demo").return_urls == body["return_urls"]


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/applications", {"id": "new-app"}),
        ("PUT", "/applications/demo", {"id": "demo"}),
        ("POST", "/app-keys", {"app_id": "demo"}),
    ],
)
async def test_application_management_requires_operator_auth(rig, console, method, path, body):
    state, http = console
    assert (
        await http.request(method, PREFIX + path, json=body, headers={"X-CSRF-Token": "bad"})
    ).status_code == 403
    assert (
        await http.request(
            method, PREFIX + path, json=body, headers={"Origin": "https://other.example"}
        )
    ).status_code == 403
    assert (
        await http.request(method, PREFIX + path, json=body, headers=rig.headers())
    ).status_code == 401
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_console_app(state)), base_url=state.origin
    ) as anonymous:
        assert (await anonymous.get(PREFIX + "/applications")).status_code == 401
        assert (
            await anonymous.request(
                method, PREFIX + path, json=body, headers={"Origin": state.origin}
            )
        ).status_code == 401
    assert (await rig.http.request(method, "/admin/v1" + path, json=body)).status_code == 404


async def test_admin_api_can_register_app_and_key(rig):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_admin_app(rig.service)), base_url="http://admin"
    ) as admin:
        assert (
            await admin.post("/admin/v1/applications", json={"id": "new-app"})
        ).status_code == 201
        key = (await admin.post("/admin/v1/app-keys", json={"app_id": "new-app"})).json()["key"]
        assert (await rig.service.authenticate_app(key, "alice", None)).app == "new-app"
        listed = await admin.get("/admin/v1/applications")
        assert "new-app" in listed.text and key not in listed.text
