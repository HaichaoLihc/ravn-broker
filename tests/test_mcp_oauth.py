"""RAVN as an MCP OAuth 2.1 authorization server for agents."""

import base64
import hashlib
import json
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx
import httpx2
import pytest
from conftest import ALICE
from mcp import Client
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata

from ravn.app import create_admin_app
from ravn.cli import parser
from ravn.common import RavnError
from ravn.console import (
    CONSENT,
    GRANTS,
    GRANTS_REVOKE,
    PREFIX,
    Console,
    create_console_app,
    render_consent,
)
from ravn.service import Service
from ravn.store import one, rows

pytestmark = pytest.mark.anyio
ISSUER = "http://127.0.0.1:8787"
RESOURCE = ISSUER + "/mcp"
REDIRECT = "http://127.0.0.1:53682/callback"
PRM = ISSUER + "/.well-known/oauth-protected-resource/mcp"


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


async def register(rig, uris=(REDIRECT,), **extra):
    return await rig.http.post(
        "/register",
        json={
            "redirect_uris": list(uris),
            "client_name": "Test Agent",
            "token_endpoint_auth_method": "none",
            **extra,
        },
    )


async def client_id(rig):
    response = await register(rig)
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


async def authorize(rig, cid, challenge, *, redirect=REDIRECT, state="state-1", **extra):
    params = {
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": redirect,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "resource": RESOURCE,
        **extra,
    }
    return await rig.http.get(
        "/authorize", params={k: v for k, v in params.items() if v is not None}
    )


def query_of(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def request_of(response):
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith("http://127.0.0.1:8788" + CONSENT + "?request=oreq_")
    return query_of(location)["request"]


def key_of(conn, app="demo", tenant="default"):
    return f"{app}/{tenant}/{conn['id']}"


async def consent(console, rid, conn_key=None, decision="approve", **extra):
    state, http = console
    form = {"request": rid, "csrf": http.headers["X-CSRF-Token"], "decision": decision, **extra}
    if conn_key is not None:
        form["connection"] = conn_key
    return await http.post(CONSENT, data=form)


async def token(rig, **form):
    return await rig.http.post("/token", data=form)


async def code_for(rig, console, conn, cid=None, *, redirect=REDIRECT):
    cid = cid or await client_id(rig)
    verifier, challenge = pkce()
    rid = request_of(await authorize(rig, cid, challenge, redirect=redirect))
    response = await consent(console, rid, key_of(conn))
    assert response.status_code == 303, response.text
    callback = query_of(response.headers["location"])
    assert callback["state"] == "state-1"
    return cid, callback["code"], verifier


async def grant(rig, console, conn=None):
    conn = conn or await rig.connection()
    cid, code, verifier = await code_for(rig, console, conn)
    response = await token(
        rig,
        grant_type="authorization_code",
        code=code,
        redirect_uri=REDIRECT,
        client_id=cid,
        code_verifier=verifier,
        resource=RESOURCE,
    )
    assert response.status_code == 200, response.text
    return cid, conn, response.json()


async def refresh(rig, cid, value):
    return await token(rig, grant_type="refresh_token", refresh_token=value, client_id=cid)


async def db_rows(rig, sql, params=()):
    async with rig.service.store.transaction() as db:
        return await rows(db, sql, params)


async def mcp_status(rig, access):
    return (
        await rig.http.post(
            "/mcp",
            headers={
                "Authorization": "Bearer " + access,
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    ).status_code


async def test_metadata_documents_name_one_issuer(rig):
    response = await rig.http.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    data = response.json()
    assert data["issuer"] == ISSUER
    assert data["authorization_endpoint"] == ISSUER + "/authorize"
    assert data["token_endpoint"] == ISSUER + "/token"
    assert data["registration_endpoint"] == ISSUER + "/register"
    assert data["revocation_endpoint"] == ISSUER + "/revoke"
    assert data["response_types_supported"] == ["code"]
    assert {"authorization_code", "refresh_token"} <= set(data["grant_types_supported"])
    assert data["code_challenge_methods_supported"] == ["S256"]
    assert data["token_endpoint_auth_methods_supported"] == ["none"]
    assert response.headers["referrer-policy"] == "no-referrer"
    prm = (await rig.http.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert prm["resource"] == RESOURCE
    assert prm["authorization_servers"] == [ISSUER]


async def test_mcp_401_points_at_resource_metadata(rig):
    response = await rig.http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"
    assert response.headers["www-authenticate"] == f'Bearer resource_metadata="{PRM}"'
    response = await rig.http.post(
        "/mcp", headers={"Authorization": "Bearer rv_sess_bogus"}, json={}
    )
    assert response.headers["www-authenticate"] == (
        f'Bearer resource_metadata="{PRM}", error="invalid_token"'
    )
    response = await rig.http.get("/v1/connections")
    assert response.headers["www-authenticate"] == 'Bearer realm="ravn"'


async def test_bodies_stay_strict(rig):
    # JSON stays mandatory outside the form endpoints.
    response = await rig.http.post(
        "/v1/connections/import", headers=rig.headers(), data={"integration_id": "github"}
    )
    assert response.status_code == 415
    response = await rig.http.post(
        "/token",
        content="grant_type=refresh_token&grant_type=authorization_code",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_request",
        "error_description": "Repeated form field.",
    }
    assert response.headers["cache-control"] == "no-store"
    response = await rig.http.post("/token", json={"grant_type": "refresh_token"})
    assert response.status_code == 415 and response.json()["error"] == "invalid_request"
    response = await rig.http.post(
        "/token", data={"grant_type": "x"}, headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 403 and response.json()["error"] == "invalid_request"


async def test_registration_is_loopback_public_and_audited(rig):
    response = await register(rig, ["http://localhost:53682/callback"])
    assert response.status_code == 201, response.text
    body = response.json()
    assert "client_secret" not in body and body["token_endpoint_auth_method"] == "none"
    [row] = await db_rows(rig, "SELECT * FROM oauth_clients WHERE id=?", (body["client_id"],))
    assert row["name"] == "Test Agent"
    [evt] = await db_rows(rig, "SELECT * FROM events WHERE kind='oauth.client_registered'")
    assert evt["actor_kind"] == "oauth_client" and evt["subject_id"] == body["client_id"]
    assert evt["request_id"].startswith("req_")
    # A client asking for a secret is registered as public anyway.
    response = await register(rig, token_endpoint_auth_method="client_secret_post")
    assert response.status_code == 201 and "client_secret" not in response.json()


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.example/cb",
        "http://example.com:80/cb",
        "http://127.0.0.1/cb",
        "http://127.0.0.1:5000/cb#frag",
        "http://user@127.0.0.1:5000/cb",
        "ftp://127.0.0.1:5000/cb",
    ],
)
async def test_registration_rejects_non_loopback(rig, uri):
    response = await register(rig, [uri])
    assert response.status_code == 400
    assert response.json()["error"] in {"invalid_redirect_uri", "invalid_client_metadata"}
    assert not await db_rows(rig, "SELECT * FROM oauth_clients")


async def test_registration_cap_and_rate_limit(rig, monkeypatch):
    monkeypatch.setattr(
        rig.service.mcp_oauth,
        "config",
        rig.service.config.model_copy(
            update={"mcp_oauth": rig.service.config.mcp_oauth.model_copy(update={"max_clients": 1})}
        ),
    )
    assert (await register(rig)).status_code == 201
    response = await register(rig)
    assert response.status_code == 400 and response.json()["error"] == "invalid_client_metadata"
    for _ in range(8):
        await register(rig)
    response = await register(rig)
    assert response.status_code == 429
    assert response.json()["error"] == "temporarily_unavailable"
    assert len(await db_rows(rig, "SELECT * FROM oauth_clients")) == 1


async def test_authorize_rejections(rig, console):
    cid = await client_id(rig)
    _, challenge = pkce()
    # Errors about the client or redirect are shown directly, never redirected.
    response = await authorize(rig, "00000000-0000-0000-0000-000000000000", challenge)
    assert response.status_code == 400 and "location" not in response.headers
    response = await authorize(rig, cid, challenge, redirect="http://127.0.0.1:53682/other")
    assert response.status_code == 400 and "location" not in response.headers
    response = await authorize(rig, cid, challenge, redirect=None)
    assert response.status_code == 400 and "location" not in response.headers
    # Other errors go back to the registered redirect with state.
    for extra in (
        {"code_challenge": None},
        {"code_challenge_method": "plain"},
        {"resource": "http://127.0.0.1:8787/other"},
        {"code_challenge": "short"},
    ):
        response = await authorize(rig, cid, challenge, **extra)
        assert response.status_code == 302, extra
        location = response.headers["location"]
        assert location.startswith(REDIRECT), extra
        params = query_of(location)
        assert params["state"] == "state-1" and "code" not in params
        if "resource" in extra:
            assert params["error"] == "invalid_target"
    response = await rig.http.get(
        "/authorize?client_id=" + cid + "&client_id=" + cid + "&response_type=code"
    )
    assert response.status_code == 400 and "location" not in response.headers
    # A different port on a registered loopback URI is the same redirect (RFC 8252).
    request_of(await authorize(rig, cid, challenge, redirect="http://127.0.0.1:61000/callback"))
    assert len(await db_rows(rig, "SELECT * FROM oauth_requests")) == 1


async def test_authorize_without_console_is_temporarily_unavailable(rig):
    cid = await client_id(rig)
    _, challenge = pkce()
    response = await authorize(rig, cid, challenge)
    assert response.status_code == 302
    params = query_of(response.headers["location"])
    assert params["error"] == "temporarily_unavailable" and params["state"] == "state-1"


async def test_full_flow_issues_a_grant_session(rig, console):
    cid, conn, tokens = await grant(rig, console)
    assert tokens["token_type"] == "Bearer"
    assert tokens["access_token"].startswith("rv_sess_")
    assert tokens["refresh_token"].startswith("rv_rt_")
    assert 3500 <= tokens["expires_in"] <= 3600
    [session] = await db_rows(rig, "SELECT * FROM sessions WHERE grant_id IS NOT NULL")
    assert session["issuing_key_id"] is None and session["connection_id"] == conn["id"]
    [row] = await db_rows(rig, "SELECT * FROM oauth_grants")
    assert row["id"] == session["grant_id"] and row["client_id"] == cid
    assert row["resource"] == RESOURCE and row["rotation"] == 0
    kinds = [e["kind"] for e in await db_rows(rig, "SELECT kind FROM events ORDER BY rowid")]
    assert "oauth.consent_approved" in kinds and "oauth.token_issued" in kinds
    [created] = await db_rows(
        rig, "SELECT * FROM events WHERE kind='session.created' AND subject_id=?", (session["id"],)
    )
    assert created["actor_kind"] == "oauth_client" and created["actor_id"] == cid
    [request] = await db_rows(rig, "SELECT * FROM oauth_requests")
    assert request["status"] == "approved"


async def test_token_response_is_no_store(rig, console):
    conn = await rig.connection()
    cid, code, verifier = await code_for(rig, console, conn)
    response = await token(
        rig,
        grant_type="authorization_code",
        code=code,
        redirect_uri=REDIRECT,
        client_id=cid,
        code_verifier=verifier,
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("fault", ["verifier", "redirect", "client", "resource", "expired"])
async def test_code_exchange_failures(rig, console, fault):
    conn = await rig.connection()
    cid, code, verifier = await code_for(rig, console, conn)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": cid,
        "code_verifier": verifier,
    }
    if fault == "verifier":
        form["code_verifier"] = pkce()[0]
    elif fault == "redirect":
        form["redirect_uri"] = "http://127.0.0.1:53683/callback"
    elif fault == "client":
        form["client_id"] = await client_id(rig)
    elif fault == "resource":
        form["resource"] = "http://127.0.0.1:8787/other"
    else:
        async with rig.service.store.transaction() as db:
            await db.execute("UPDATE oauth_codes SET expires_at='2000-01-01T00:00:00.000000Z'")
    response = await token(rig, **form)
    assert response.status_code == 400
    error = response.json()["error"]
    expected = {"resource": "invalid_target", "redirect": "invalid_request"}.get(
        fault, "invalid_grant"
    )
    assert error == expected
    assert not await db_rows(rig, "SELECT * FROM sessions WHERE grant_id IS NOT NULL")


async def test_code_reuse_revokes_what_it_issued(rig, console):
    conn = await rig.connection()
    cid, code, verifier = await code_for(rig, console, conn)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": cid,
        "code_verifier": verifier,
    }
    first = await token(rig, **form)
    assert first.status_code == 200
    again = await token(rig, **form)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    [row] = await db_rows(rig, "SELECT * FROM oauth_grants")
    assert row["revoked_at"] and row["revoke_reason"] == "code_reuse"
    assert await mcp_status(rig, first.json()["access_token"]) == 401
    response = await refresh(rig, cid, first.json()["refresh_token"])
    assert response.status_code == 400


async def test_consent_denied(rig, console):
    cid = await client_id(rig)
    _, challenge = pkce()
    rid = request_of(await authorize(rig, cid, challenge))
    response = await consent(console, rid, decision="deny")
    assert response.status_code == 303
    params = query_of(response.headers["location"])
    assert response.headers["location"].startswith(REDIRECT)
    assert params == {"error": "access_denied", "state": "state-1"}
    [evt] = await db_rows(rig, "SELECT * FROM events WHERE kind='oauth.consent_denied'")
    assert evt["actor_kind"] == "local_operator" and evt["subject_id"] == rid
    response = await consent(console, rid, decision="approve")
    assert response.status_code == 409 and "location" not in response.headers
    assert "Restart it from your agent" in response.text


async def test_consent_page_signed_out_then_in(rig, console):
    state, signed_in = console
    conn = await rig.connection()
    cid = await client_id(rig)
    _, challenge = pkce()
    rid = request_of(await authorize(rig, cid, challenge))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_console_app(state)), base_url=state.origin
    ) as anonymous:
        response = await anonymous.get(CONSENT, params={"request": rid})
        assert response.status_code == 200 and "uv run ravn console" in response.text
        assert response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        response = await anonymous.post(
            CONSENT,
            data={"request": rid, "csrf": "x", "decision": "approve"},
            headers={"Origin": state.origin},
        )
        assert response.status_code == 401
    [row] = await db_rows(rig, "SELECT * FROM oauth_requests")
    assert row["status"] == "pending"
    response = await signed_in.get(CONSENT, params={"request": rid})
    assert response.status_code == 200
    assert "Test Agent" in response.text and "self-reported" in response.text
    assert "127.0.0.1:53682" in response.text and key_of(conn) in response.text
    assert response.headers["referrer-policy"] == "same-origin"
    assert "<script" not in response.text
    assert (await consent(console, rid, key_of(conn))).status_code == 303


async def test_consent_post_guards(rig, console):
    state, http = console
    conn = await rig.connection()
    cid = await client_id(rig)
    _, challenge = pkce()
    rid = request_of(await authorize(rig, cid, challenge))
    csrf = http.headers["X-CSRF-Token"]
    base = {"request": rid, "connection": key_of(conn), "decision": "approve"}
    for form, headers, status in [
        (base, {}, 403),
        ({**base, "csrf": "wrong"}, {}, 403),
        ({**base, "csrf": csrf}, {"Origin": "http://127.0.0.1:8787"}, 403),
        ({**base, "csrf": csrf, "connection": "demo/default/conn_missing"}, {}, 409),
        ({**base, "csrf": csrf, "extra": "1"}, {}, 400),
    ]:
        response = await http.post(CONSENT, data=form, headers=headers)
        assert response.status_code == status, (form, response.text)
        assert "location" not in response.headers
    request = http.build_request("POST", CONSENT, data={**base, "csrf": csrf})
    del request.headers["Origin"]
    assert (await http.send(request)).status_code == 403
    assert not await db_rows(rig, "SELECT * FROM oauth_codes")
    [row] = await db_rows(rig, "SELECT * FROM oauth_requests")
    assert row["status"] == "pending"
    # A disconnected connection is not eligible.
    await rig.http.post(f"/v1/connections/{conn['id']}/disconnect", headers=rig.headers())
    response = await consent(console, rid, key_of(conn))
    assert response.status_code == 409
    assert not await db_rows(rig, "SELECT * FROM oauth_codes")


async def test_refresh_rotates_and_detects_reuse(rig, console):
    cid, _, first = await grant(rig, console)
    second = await refresh(rig, cid, first["refresh_token"])
    assert second.status_code == 200, second.text
    second = second.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert second["access_token"] != first["access_token"]
    [row] = await db_rows(rig, "SELECT * FROM oauth_grants")
    assert row["rotation"] == 1 and row["refreshed_at"]
    # The earlier session keeps running to its own expiry.
    assert await mcp_status(rig, first["access_token"]) == 200
    assert await mcp_status(rig, second["access_token"]) == 200
    reused = await refresh(rig, cid, first["refresh_token"])
    assert reused.status_code == 400 and reused.json()["error"] == "invalid_grant"
    [row] = await db_rows(rig, "SELECT * FROM oauth_grants")
    assert row["revoked_at"] and row["revoke_reason"] == "refresh_reuse"
    kinds = {e["kind"] for e in await db_rows(rig, "SELECT kind FROM events")}
    assert {"oauth.refresh_reuse_detected", "oauth.grant_revoked", "oauth.token_refreshed"} <= kinds
    assert await mcp_status(rig, first["access_token"]) == 401
    assert await mcp_status(rig, second["access_token"]) == 401
    assert (await refresh(rig, cid, second["refresh_token"])).status_code == 400


async def test_concurrent_refresh_ends_the_grant(rig, console):
    cid, _, first = await grant(rig, console)
    results = []

    async def attempt():
        results.append((await refresh(rig, cid, first["refresh_token"])).status_code)

    async with anyio.create_task_group() as group:
        group.start_soon(attempt)
        group.start_soon(attempt)
    assert sorted(results) in ([200, 400], [400, 400])
    [row] = await db_rows(rig, "SELECT * FROM oauth_grants")
    assert row["revoked_at"]


async def test_refresh_copies_session_rules(rig, console):
    state, http = console
    cid, conn, first = await grant(rig, console)
    [session] = await db_rows(rig, "SELECT * FROM sessions WHERE grant_id IS NOT NULL")
    tool = sorted(json.loads(session["tools"]))[0]
    response = await http.put(
        PREFIX + f"/sessions/{session['id']}/permissions",
        json={"app_id": "demo", "tenant_id": "default", "tools": {tool: False}},
    )
    assert response.status_code == 200, response.text
    current = first
    for _ in range(2):
        response = await refresh(rig, cid, current["refresh_token"])
        assert response.status_code == 200, response.text
        current = response.json()
    [newest] = await db_rows(
        rig,
        "SELECT * FROM sessions WHERE grant_id IS NOT NULL ORDER BY created_at DESC,id DESC LIMIT 1",
    )
    rules = await db_rows(
        rig, "SELECT tool,allowed FROM session_tool_rules WHERE session_id=?", (newest["id"],)
    )
    assert rules == [{"tool": tool, "allowed": 0}]
    async with rig.mcp({"token": current["access_token"]}) as mcp:
        names = {t.name for t in (await mcp.list_tools()).tools}
    assert tool not in names and names


async def test_transient_refresh_failure_keeps_the_token(rig, console, monkeypatch):
    cid, _, first = await grant(rig, console)
    original = rig.service.session_tools

    async def unavailable(p, cid):
        raise RavnError(503, "upstream_unavailable", "Provider unavailable.")

    monkeypatch.setattr(rig.service, "session_tools", unavailable)
    response = await refresh(rig, cid, first["refresh_token"])
    assert response.status_code == 503
    assert response.json()["error"] == "temporarily_unavailable"
    monkeypatch.setattr(rig.service, "session_tools", original)
    response = await refresh(rig, cid, first["refresh_token"])
    assert response.status_code == 200, response.text


async def test_refresh_fails_for_ended_grants(rig, console):
    cid, conn, first = await grant(rig, console)
    async with rig.service.store.transaction() as db:
        await db.execute("UPDATE oauth_grants SET expires_at='2000-01-01T00:00:00.000000Z'")
    assert (await refresh(rig, cid, first["refresh_token"])).status_code == 400
    assert len(await db_rows(rig, "SELECT * FROM sessions WHERE grant_id IS NOT NULL")) == 1


async def test_revocation_endpoint(rig, console):
    cid, _, tokens = await grant(rig, console)
    response = await rig.http.post("/revoke", data={"token": "rv_rt_unknown", "client_id": cid})
    assert response.status_code == 200
    [row] = await db_rows(rig, "SELECT * FROM oauth_grants")
    assert row["revoked_at"] is None
    # Another client cannot revoke this client's tokens.
    other = await client_id(rig)
    response = await rig.http.post(
        "/revoke", data={"token": tokens["access_token"], "client_id": other}
    )
    assert response.status_code == 200
    assert await mcp_status(rig, tokens["access_token"]) == 200
    response = await rig.http.post(
        "/revoke", data={"token": tokens["access_token"], "client_id": cid}
    )
    assert response.status_code == 200
    assert await mcp_status(rig, tokens["access_token"]) == 401
    [row] = await db_rows(rig, "SELECT * FROM oauth_grants")
    assert row["revoke_reason"] == "session_revoked"

    cid, _, tokens = await grant(rig, console)
    response = await rig.http.post(
        "/revoke",
        data={
            "token": tokens["refresh_token"],
            "client_id": cid,
            "token_type_hint": "refresh_token",
        },
    )
    assert response.status_code == 200
    assert await mcp_status(rig, tokens["access_token"]) == 401
    revoked = await db_rows(rig, "SELECT * FROM oauth_grants WHERE revoke_reason='client_revoked'")
    assert len(revoked) == 1
    assert (await refresh(rig, cid, tokens["refresh_token"])).status_code == 400


async def test_admin_lists_and_revokes_grants(rig, console):
    cid, conn, tokens = await grant(rig, console)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_admin_app(rig.service)),
        base_url="http://ravn-admin",
    ) as admin:
        [item] = (await admin.get("/admin/v1/oauth-grants")).json()["items"]
        assert item["client_id"] == cid and item["client_name"] == "Test Agent"
        assert item["connection_id"] == conn["id"] and item["active_sessions"] == 1
        assert tokens["access_token"] not in json.dumps(item)
        response = await admin.post("/admin/v1/oauth-grants/grant_missing/revoke")
        assert response.status_code == 404
        assert response.json()["error"]["message"] == "Grant not found."
        response = await admin.post(f"/admin/v1/oauth-grants/{item['id']}/revoke")
        assert response.status_code == 200 and response.json()["revoked_at"]
        assert (await admin.get("/admin/v1/oauth-grants")).json()["items"] == []
        assert len((await admin.get("/admin/v1/oauth-grants?all=1")).json()["items"]) == 1
    assert await mcp_status(rig, tokens["access_token"]) == 401
    assert (await refresh(rig, cid, tokens["refresh_token"])).status_code == 400
    [evt] = await db_rows(rig, "SELECT * FROM events WHERE kind='oauth.grant_revoked'")
    assert evt["actor_kind"] == "local_operator" and evt["actor_id"] == "unix-owner"


async def test_console_grants_page(rig, console):
    state, http = console
    cid, conn, tokens = await grant(rig, console)
    response = await http.get(GRANTS)
    assert response.status_code == 200 and "Test Agent" in response.text
    assert tokens["refresh_token"] not in response.text
    [row] = await db_rows(rig, "SELECT * FROM oauth_grants")
    response = await http.post(GRANTS_REVOKE, data={"grant": row["id"], "csrf": "wrong"})
    assert response.status_code == 403
    response = await http.post(
        GRANTS_REVOKE, data={"grant": row["id"], "csrf": http.headers["X-CSRF-Token"]}
    )
    assert response.status_code == 303 and response.headers["location"] == GRANTS
    assert await mcp_status(rig, tokens["access_token"]) == 401


async def test_disconnect_revokes_grants(rig, console):
    state, http = console
    cid, conn, tokens = await grant(rig, console)
    response = await rig.http.post(
        f"/v1/connections/{conn['id']}/disconnect", headers=rig.headers()
    )
    assert response.status_code == 200
    [row] = await db_rows(rig, "SELECT * FROM oauth_grants")
    assert row["revoke_reason"] == "connection_disconnected"
    assert (await refresh(rig, cid, tokens["refresh_token"])).status_code == 400
    # Through the console too.
    conn = await rig.connection("bob", ALICE)
    cid, conn, tokens = await grant(rig, console, conn)
    detail = (
        await http.get(
            PREFIX + f"/connections/{conn['id']}", params={"app_id": "demo", "tenant_id": "default"}
        )
    ).json()
    response = await http.post(
        PREFIX + f"/connections/{conn['id']}/disconnect",
        json={"app_id": "demo", "tenant_id": "default"},
        headers={"If-Match": f'"{detail["revision"]}"'},
    )
    assert response.status_code == 200, response.text
    assert not await db_rows(
        rig, "SELECT * FROM sessions WHERE grant_id IS NOT NULL AND revoked_at IS NULL"
    )
    assert (await refresh(rig, cid, tokens["refresh_token"])).status_code == 400


async def test_console_session_revoke_ends_the_grant(rig, console):
    state, http = console
    cid, conn, tokens = await grant(rig, console)
    [session] = await db_rows(rig, "SELECT * FROM sessions WHERE grant_id IS NOT NULL")
    response = await http.post(
        PREFIX + f"/sessions/{session['id']}/revoke",
        json={"app_id": "demo", "tenant_id": "default"},
    )
    assert response.status_code == 200
    assert (await refresh(rig, cid, tokens["refresh_token"])).status_code == 400


async def test_oauth_session_uses_the_ceiling_and_tools(rig, console):
    cid, conn, tokens = await grant(rig, console)
    async with rig.mcp({"token": tokens["access_token"]}) as mcp:
        names = {t.name for t in (await mcp.list_tools()).tools}
        assert "issue_read" in names
        result = await mcp.call_tool(
            "issue_read", {"owner": "acme", "repo": "demo", "method": "get", "issue_number": 1}
        )
        assert result.content[0].text == "alice issue"


async def test_legacy_sessions_and_key_revocation_unaffected(rig, console):
    cid, conn, tokens = await grant(rig, console)
    legacy = await rig.session(conn)
    [key] = await db_rows(rig, "SELECT * FROM app_keys WHERE app_id='demo'")
    await rig.service.revoke_key(key["id"], revoke_sessions=True)
    async with rig.service.store.transaction() as db:
        row = await one(db, "SELECT * FROM sessions WHERE id=?", (legacy["id"],))
        assert row["revoked_at"] and row["issuing_key_id"] == key["id"]
    assert await mcp_status(rig, legacy["token"]) == 401
    assert await mcp_status(rig, tokens["access_token"]) == 200


async def test_database_holds_no_token_material(rig, console):
    conn = await rig.connection()
    cid, code, verifier = await code_for(rig, console, conn)
    response = await token(
        rig,
        grant_type="authorization_code",
        code=code,
        redirect_uri=REDIRECT,
        client_id=cid,
        code_verifier=verifier,
    )
    tokens = response.json()
    second = (await refresh(rig, cid, tokens["refresh_token"])).json()
    path = rig.service.config.storage.sqlite_path
    dump = "\n".join(sqlite3.connect(path).iterdump())
    for value in (code, tokens["access_token"], tokens["refresh_token"], second["refresh_token"]):
        assert value not in dump


async def test_grants_cli_parses_config_after_action(tmp_path):
    args = parser().parse_args(
        ["grants", "revoke", "grant_x", "--config", str(tmp_path / "c.yaml")]
    )
    assert args.id == "grant_x" and args.config == tmp_path / "c.yaml"
    args = parser().parse_args(["grants", "list", "--all"])
    assert args.all is True


async def test_migration_from_v6_keeps_sessions(config):
    path = config.storage.sqlite_path
    path.parent.mkdir(mode=0o700)
    with sqlite3.connect(path) as db:
        for name in [
            "001_initial",
            "002_console",
            "003_onboarding",
            "004_writes",
            "005_permissions",
            "006_approvals",
        ]:
            db.executescript(files("ravn").joinpath(f"migrations/{name}.sql").read_text())
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("INSERT INTO applications VALUES('demo','single')")
        db.execute(
            "INSERT INTO app_keys VALUES('key_1','demo','hash','k','2026-01-01T00:00:00.000000Z',NULL)"
        )
        db.execute(
            "INSERT INTO connections(id,app_id,tenant_id,user_id,integration_id,provider_account_id,"
            "display_name,status,key_id,created_at,updated_at) VALUES('conn_1','demo','default','alice',"
            "'github','1','alice','active','test-key','2026-01-01T00:00:00.000000Z','2026-01-01T00:00:00.000000Z')"
        )
        db.execute(
            "INSERT INTO sessions VALUES('sess_1','demo','default','alice','conn_1',1,'key_1','th','{}',"
            "'2026-01-01T00:00:00.000000Z','2026-01-01T01:00:00.000000Z',NULL)"
        )
        db.execute(
            "INSERT INTO session_tool_rules VALUES('demo','default','sess_1','issue_read',0,'2026-01-01T00:00:00.000000Z')"
        )
        db.execute(
            "INSERT INTO approvals(id,app_id,tenant_id,user_id,connection_id,session_id,tool,"
            "arguments_fingerprint,status,created_at,expires_at) VALUES('apr_1','demo','default',"
            "'alice','conn_1','sess_1','send_email','fp','expired','2026-01-01T00:00:00.000000Z',"
            "'2026-01-01T00:05:00.000000Z')"
        )
    service = Service(config)
    await service.start()
    try:
        async with service.store.transaction() as db:
            assert (await one(db, "PRAGMA user_version"))["user_version"] == 7
            row = await one(db, "SELECT * FROM sessions WHERE id='sess_1'")
            assert row["issuing_key_id"] == "key_1" and row["grant_id"] is None
            assert await one(db, "SELECT * FROM session_tool_rules WHERE session_id='sess_1'")
            assert await one(db, "SELECT * FROM approvals WHERE id='apr_1'")
            assert not await rows(db, "PRAGMA foreign_key_check")
            assert (await one(db, "PRAGMA foreign_keys"))["foreign_keys"] == 1
            indexes = {r["name"] for r in await rows(db, "PRAGMA index_list(sessions)")}
            assert {
                "sessions_owner",
                "sessions_connection",
                "sessions_app_time",
                "sessions_grant",
            } <= indexes
        with pytest.raises(sqlite3.IntegrityError):
            async with service.store.transaction() as db:
                await db.execute(
                    "INSERT INTO sessions VALUES('sess_2','demo','default','alice','conn_1',1,NULL,"
                    "'th2','{}','2026-01-01T00:00:00.000000Z','2026-01-01T01:00:00.000000Z',NULL,NULL)"
                )
    finally:
        await service.close()


async def test_sdk_client_completes_discovery_and_sign_in(rig, console):
    """The official MCP client: discovery, DCR, authorize, consent, token, tools/list."""
    await rig.connection()
    conn_key = None
    callback = {}

    class Memory:
        def __init__(self):
            self.tokens = self.client = None

        async def get_tokens(self):
            return self.tokens

        async def set_tokens(self, tokens):
            self.tokens = tokens

        async def get_client_info(self):
            return self.client

        async def set_client_info(self, client):
            self.client = client

    async def redirect_handler(url):
        nonlocal conn_key
        response = await rig.http.get(url)
        rid = request_of(response)
        [conn] = await db_rows(rig, "SELECT id FROM connections")
        conn_key = key_of(conn)
        decided = await consent(console, rid, conn_key)
        assert decided.status_code == 303, decided.text
        callback.update(query_of(decided.headers["location"]))

    async def callback_handler():
        return AuthorizationCodeResult(code=callback["code"], state=callback.get("state"))

    storage = Memory()
    auth = OAuthClientProvider(
        RESOURCE,
        OAuthClientMetadata(
            redirect_uris=[REDIRECT], client_name="SDK Agent", token_endpoint_auth_method="none"
        ),
        storage,
        redirect_handler,
        callback_handler,
    )
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=rig.app), auth=auth) as http:
        async with Client(
            streamable_http_client(RESOURCE, http_client=http), mode="auto", cache=None
        ) as client:
            names = {t.name for t in (await client.list_tools()).tools}
    assert "issue_read" in names
    assert storage.tokens.access_token.startswith("rv_sess_")
    [row] = await db_rows(rig, "SELECT * FROM oauth_clients")
    assert row["name"] == "SDK Agent"


async def test_send_email_with_oauth_token_needs_approval(tmp_path):
    from test_gmail_api import EMAIL, broker, decide, finished, send

    async with broker(tmp_path) as gmail:
        state = Console(gmail.service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_console_app(state)), base_url=state.origin
        ) as http:
            ticket = parse_qs(urlsplit(state.ticket()["url"]).fragment)["ticket"][0]
            response = await http.post(
                PREFIX + "/auth/exchange", headers={"Origin": state.origin}, json={"ticket": ticket}
            )
            http.headers.update(
                {"Origin": state.origin, "X-CSRF-Token": response.json()["csrf_token"]}
            )
            cid, conn, tokens = await grant(gmail, (state, http), gmail.conn)
        state.close()
        gmail.runtime = {"token": tokens["access_token"]}
        task = send(gmail, EMAIL)
        await decide(gmail, "approve")
        result = await finished(task)
        assert not isinstance(result, dict) and not result.is_error
        assert len(gmail.fake.sent) == 1
        task = send(gmail, EMAIL)
        await decide(gmail, "deny")
        result = await finished(task)
        assert isinstance(result, dict) and result["code"] == "approval_denied"
        assert len(gmail.fake.sent) == 1


async def test_ended_clients_free_their_registration_slot(rig, monkeypatch):
    monkeypatch.setattr(
        rig.service.mcp_oauth,
        "config",
        rig.service.config.model_copy(
            update={"mcp_oauth": rig.service.config.mcp_oauth.model_copy(update={"max_clients": 1})}
        ),
    )
    assert (await register(rig)).status_code == 201
    async with rig.service.store.transaction() as db:
        await db.execute("UPDATE oauth_clients SET created_at='2000-01-01T00:00:00.000000Z'")
    assert (await register(rig)).status_code == 201


async def test_a_refreshed_session_never_outlives_its_grant(rig, console):
    cid, _, first = await grant(rig, console)
    ends = (datetime.now(UTC) + timedelta(minutes=10)).isoformat(timespec="microseconds")
    ends = ends.replace("+00:00", "Z")
    async with rig.service.store.transaction() as db:
        await db.execute("UPDATE oauth_grants SET expires_at=?", (ends,))
    response = await refresh(rig, cid, first["refresh_token"])
    assert response.status_code == 200 and response.json()["expires_in"] <= 600
    [newest] = await db_rows(
        rig,
        "SELECT expires_at FROM sessions WHERE grant_id IS NOT NULL ORDER BY created_at DESC LIMIT 1",
    )
    assert newest["expires_at"] <= ends


def test_the_consent_form_preselects_nothing():
    connection = {
        "key": "k",
        "display_name": "a@example.com",
        "label": None,
        "integration_id": "gmail-send",
        "app_id": "demo",
        "tenant_id": "default",
        "user_id": "luke",
    }
    view = {
        "connections": [connection, {**connection, "key": "k2"}],
        "client_name": "Claude Code",
        "client_id": "c",
        "redirect": "http://localhost:1/callback",
        "expires_at": "soon",
        "request_id": "r",
    }
    page = render_consent(view, "csrf")
    assert " checked" not in page and page.count(" required>") == 2
    assert 'value="deny" formnovalidate' in page
