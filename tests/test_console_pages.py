import html
import re
import time
from dataclasses import replace

import httpx
import pytest
from conftest import ALICE, BOB, add_application

from ravn.console import COOKIE, Console, create_console_app

pytestmark = pytest.mark.anyio


async def test_pages_require_operator_cookie_and_hide_data_when_expired(rig, console):
    state, http = console
    conn = await rig.connection()
    params = {"view": "connections", "app_id": "demo", "tenant_id": "default", "id": conn["id"]}
    assert conn["id"] in (await http.get("/console/", params=params)).text
    key = next(iter(state.sessions))
    state.sessions[key] = replace(state.sessions[key], deadline=time.monotonic() - 1)
    response = await http.get("/console/", params=params)
    assert response.status_code == 200
    assert "Open your console" in response.text and conn["id"] not in response.text
    assert '<meta name="csrf-token"' not in response.text
    anonymous = Console(rig.service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_console_app(anonymous)), base_url=anonymous.origin
    ) as client:
        assert "Open your console" in (await client.get("/console/")).text
        assert (await client.get("/console/", headers=rig.headers())).status_code == 401


@pytest.mark.parametrize(
    "view",
    [
        "applications",
        "connections",
        "sessions",
        "calls",
        "events",
        "app-keys",
        "integrations",
        "application",
        "server",
    ],
)
async def test_pages_render_without_frontend_fetches_and_never_expose_secrets(rig, console, view):
    _, http = console
    conn = await rig.connection()
    session = await rig.session(conn)
    response = await http.get("/console/", params={"view": view, "app_id": "demo"})
    assert response.status_code == 200, response.text
    assert '<nav aria-label="Console">' in response.text
    for secret in (ALICE, rig.key, session["token"], "token_hash", "ciphertext"):
        assert secret not in response.text
    if view == "connections":
        assert conn["id"] in response.text
    if view == "sessions":
        assert session["id"] in response.text


async def test_applications_count_all_tenants_and_link_to_the_correct_app(rig, console):
    state, http = console
    connection = await rig.connection()
    disconnected = await rig.connection()
    active, expired, revoked = [await rig.session(connection) for _ in range(3)]
    for tenant in ("acme", "other"):
        account = await rig.connection(tenant=tenant, key=rig.saas_key)
        await rig.session(account, tenant=tenant, key=rig.saas_key)
    async with rig.service.store.transaction() as db:
        await db.execute(
            "UPDATE sessions SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (expired["id"],)
        )
        await db.execute("UPDATE sessions SET revoked_at=created_at WHERE id=?", (revoked["id"],))
        await db.execute(
            "UPDATE connections SET status='disconnected' WHERE id=?", (disconnected["id"],)
        )
    await add_application(rig.service, "empty")
    await rig.service.applications.save(
        rig.service.applications.items["saas"].model_copy(update={"enabled": False})
    )
    principal = next(iter(state.sessions.values()))
    apps = {
        a["id"]: a for a in (await state.bootstrap(principal, include_counts=True))["applications"]
    }
    assert apps["demo"]["integration_count"] == apps["saas"]["integration_count"] == 1
    assert apps["demo"]["connection_count"] == apps["saas"]["connection_count"] == 2
    assert apps["demo"]["active_session_count"] == 1
    assert apps["saas"]["active_session_count"] == 2
    assert not apps["saas"]["enabled"]
    for name in ("integration_count", "connection_count", "active_session_count"):
        assert apps["empty"][name] == 0

    # The overview covers the deployment even when opened from an app-scoped page.
    page = await http.get("/console/?view=applications&app_id=saas")
    assert page.status_code == 200
    assert "All applications" in page.text and 'name="app_id"' not in page.text
    assert 'aria-label="demo active sessions: 1"' in page.text
    assert 'aria-label="saas active sessions: 2"' in page.text
    assert "disabled" in page.text
    for app_id in ("demo", "saas", "empty"):
        for view in ("connections", "integrations", "sessions", "app-keys"):
            target = f"/console/?view={view}&app_id={app_id}"
            if view == "sessions":
                target += "&status=active"
            assert html.escape(target, quote=True) in page.text
            destination = await http.get(target)
            assert destination.status_code == 200
            if view == "sessions":
                assert (active["id"] in destination.text) == (app_id == "demo")
                assert (
                    expired["id"] not in destination.text and revoked["id"] not in destination.text
                )


async def test_navigation_preserves_selected_application(console):
    _, http = console
    page = (await http.get("/console/?view=sessions&app_id=saas")).text
    for pattern in (
        r'<a href="([^"]+)"[^>]*><span class="icon icon-bot"',
        r'<div class="brand"><a href="([^"]+)"',
    ):
        target = html.unescape(re.search(pattern, page)[1])
        overview = (await http.get(target)).text
        sessions = re.search(r'<a href="([^"]+)"[^>]*><span class="icon icon-key-round"', overview)
        assert "app_id=saas" in html.unescape(sessions[1])


async def test_session_page_scopes_account_choices_and_reflects_api_mutations(rig, console):
    _, http = console
    first, second = await rig.connection(), await rig.connection()
    bob = await rig.connection("bob", BOB)
    other = await rig.connection(tenant="acme", key=rig.saas_key)
    session = await rig.session(first)
    params = {"view": "sessions", "app_id": "demo", "tenant_id": "default", "id": session["id"]}
    page = (await http.get("/console/", params=params)).text
    assert first["id"] in page and second["id"] in page
    assert bob["id"] not in page and other["id"] not in page
    assert 'name="_connection"' in page and session["expires_at"] in page
    path = f"/console/api/v1/sessions/{session['id']}/connections/{second['id']}"
    assert (
        await http.put(path, json={"app_id": "demo", "tenant_id": "default"})
    ).status_code == 200
    page = (await http.get("/console/", params=params)).text
    assert f"/connections/{second['id']}" in page
    assert f'<option value="{second["id"]}">' not in page
    response = await http.get("/console/", params={**params, "tenant_id": "other"})
    assert response.status_code == 404 and first["id"] not in response.text


async def test_untrusted_metadata_is_escaped_and_duplicate_cookies_rejected(rig, console):
    _, http = console
    conn = await rig.connection()
    attack = '<script>alert("provider")</script>'
    async with rig.service.store.transaction() as db:
        await db.execute("UPDATE connections SET display_name=? WHERE id=?", (attack, conn["id"]))
    response = await http.get("/console/?app_id=demo")
    assert attack not in response.text and "&lt;script&gt;" in response.text
    assert (await http.get("/console/?app_id=demo&app_id=saas")).status_code == 400
    cookie = http.cookies.get(COOKIE)
    response = await http.get(
        "/console/", headers={"Cookie": f"{COOKIE}={cookie}; {COOKIE}={cookie}"}
    )
    assert response.status_code == 401


async def test_server_rendered_pagination_and_deployment_activity(rig, console):
    _, http = console
    conn = await rig.connection()
    for _ in range(51):
        await rig.session(conn)
    page = await http.get("/console/?view=sessions&app_id=demo")
    link = re.search(r'href="([^"]+)">Next page', page.text)
    assert link is not None
    next_page = await http.get(html.unescape(link[1]))
    assert next_page.status_code == 200
    assert "1 loaded" in next_page.text and "Next page" not in next_page.text
    detail_link = re.search(r'class="row-link" href="([^"]+)"', next_page.text)
    detail = await http.get(html.unescape(detail_link[1]))
    assert detail.status_code == 200 and 'class="drawer"' in detail.text
    assert "1 loaded" in detail.text
    activity = await http.get("/console/?view=events&app_id=demo&scope=deployment")
    assert activity.status_code == 200 and "operator.login" in activity.text


async def test_connection_map_and_activity_drawers_preserve_real_metadata(rig, console):
    _, http = console
    connection = await rig.connection()
    session = await rig.session(connection)
    response = await http.get(
        "/console/",
        params={
            "view": "connections",
            "app_id": "demo",
            "record_tenant": "default",
            "id": connection["id"],
            "map": "1",
        },
    )
    assert response.status_code == 200
    assert 'class="drawer"' in response.text and "Access map" in response.text
    assert session["id"] in response.text and "Provider account" in response.text
    assert 'class="table-container"' in response.text
    activity = await http.get("/console/?view=events&app_id=demo&scope=deployment")
    link = re.search(r'class="row-link" href="([^"]+)"', activity.text)
    event = await http.get(html.unescape(link[1]))
    assert event.status_code == 200
    assert "Request ID" in event.text and "Actor ID" in event.text
    assert 'class="drawer"' in event.text


async def test_key_details_stay_in_selected_application_and_settings(rig, console):
    _, http = console
    page = await http.get("/console/?view=app-keys&app_id=demo")
    link = re.search(r'class="row-link" href="([^"]+)"', page.text)
    target = html.unescape(link[1])
    detail = await http.get(target)
    assert detail.status_code == 200 and "Revoke key" in detail.text
    assert 'name="revoke_sessions"' in detail.text and "Settings views" in detail.text
    assert rig.key not in detail.text
    assert (await http.get(target.replace("app_id=demo", "app_id=saas"))).status_code == 404


async def test_integration_setup_starts_with_address_and_keeps_manual_fallback(console):
    _, http = console
    page = (await http.get("/console/?view=integrations&app_id=demo")).text
    assert 'name="endpoint"' in page and 'id="discover-integration"' in page
    assert 'id="integration-settings" hidden disabled' in page
    assert "Configure manually" in page and "Bearer token" in page
    assert 'name="identity.endpoint"' not in page
    assert 'name="identity.id_field"' not in page
    assert 'id="integration-callback"' in page and 'id="scope-choices"' in page
