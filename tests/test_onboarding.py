import asyncio
import base64
import hashlib
import json
from contextlib import AsyncExitStack
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import httpx2
import pytest
from conftest import FakeProvider, Rig, add_application, records_integration
from fake_tools import MAIL_SCHEMAS
from mcp import types
from mcp.server.transport_security import TransportSecuritySettings

from ravn.app import create_admin_app, create_app
from ravn.common import RavnError
from ravn.integrations import IntegrationRegistration
from ravn.oauth_provider import deadline
from ravn.provider import RemoteMCP
from ravn.store import one, rows

pytestmark = pytest.mark.anyio
RETURN = "http://127.0.0.1:8800/return"
APP_STATE = "app_transaction_0123456789"
MAIL_GRANTED = ["openid", "email", "mail.read", "mail.draft"]


class OAuthFake(FakeProvider):
    def __init__(self, mail=False):
        super().__init__()
        self.mail = mail
        self.client_secret = "test-client-secret"
        self.exchanges = []
        self.refreshes = []
        self.rotation_failure = False
        self.rotation_block = False
        self.rotation_entered = asyncio.Event()
        self.rotation_release = asyncio.Event()
        self.tokens = {}
        if mail:
            self.tools = [types.Tool(name=n, inputSchema=s) for n, s in MAIL_SCHEMAS.items()]
            self.app = self.server.streamable_http_app(
                streamable_http_path="/mcp/v1",
                stateless_http=True,
                json_response=True,
                transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
            )

    def transport(self, host):
        if host in {"mcp.records.example", "mcp.mail.example"}:
            return httpx2.ASGITransport(app=self.app)

        async def request(req):
            if req.url.path in {"/token"}:
                params = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
                assert params["client_secret"] == self.client_secret
                assert not req.url.query
                refresh = params["grant_type"] == "refresh_token"
                (self.refreshes if refresh else self.exchanges).append(params)
                who = "alice"
                if refresh:
                    self.rotation_entered.set()
                    if self.rotation_block:
                        await self.rotation_release.wait()
                    if self.rotation_failure:
                        raise httpx2.ReadTimeout("possibly consumed refresh: SECRET", request=req)
                else:
                    who = params["code"]
                if who == "bad":
                    return httpx2.Response(
                        200, json={"error": "bad_verification_code", "echo": "SECRET"}
                    )
                prefix = "mail_" if self.mail else "record_"
                access = prefix + who + ("-new" if refresh else "-old")
                self.tokens[access] = (
                    "T222" if who == "workspace" else "T111",
                    "U222" if who == "bob" else "U111",
                )
                if self.mail:
                    # Stable refresh profile: URL scopes, and refresh never rotates the refresh token.
                    return httpx2.Response(
                        200,
                        json={
                            "access_token": access,
                            "token_type": "Bearer",
                            "expires_in": 3599,
                            "scope": " ".join(MAIL_GRANTED),
                            **({} if refresh else {"refresh_token": "test-refresh-" + access}),
                        },
                    )
                return httpx2.Response(
                    200,
                    json={
                        "ok": True,
                        "access_token": access,
                        "token_type": "bearer",
                        "refresh_token": "test-refresh-" + access,
                        "expires_in": 3600,
                        "refresh_token_expires_in": 86400,
                        "scope": "records.read",
                    },
                )
            access = req.headers.get("authorization", "").removeprefix("Bearer ")
            if access not in self.tokens:
                return httpx2.Response(401, json={"ok": False, "error": "invalid_auth"})
            team, user = self.tokens[access]
            if self.mail:
                bob = user == "U222"
                return httpx2.Response(
                    200,
                    json={
                        "sub": "202" if bob else "101",
                        "email": "bob@example.com" if bob else "alice@example.com",
                        "email_verified": True,
                    },
                )
            return httpx2.Response(
                200,
                json={
                    "ok": True,
                    "id": 202 if user == "U222" else 101,
                    "login": "bob" if user == "U222" else "alice",
                    "team_id": team,
                    "user_id": user,
                },
            )

        return httpx2.MockTransport(request)


@pytest.fixture
async def oauth_rig(config):
    records, mail = OAuthFake(), OAuthFake(mail=True)
    fakes = {"records": records, "mail": mail}
    app = create_app(config)
    async with AsyncExitStack() as stack:
        for fake in fakes.values():
            await stack.enter_async_context(fake.server.session_manager.run())
        await stack.enter_async_context(app.router.lifespan_context(app))
        service = app.state.service
        for app_id, mode in [("demo", "single"), ("saas", "multi")]:
            await add_application(service, app_id, tenant_mode=mode, return_urls=[RETURN])
            for kind in ("records", "mail"):
                value = records_integration(app_id).model_dump(mode="json")
                if kind == "mail":
                    value.update(
                        id="mail",
                        endpoint="https://mcp.mail.example/mcp/v1",
                        identity={
                            "endpoint": "https://identity.mail.example/userinfo",
                            "required_claims": {"email_verified": True},
                        },
                    )
                value["oauth"] = {
                    "client_id": "test-client",
                    "client_secret": "test-client-secret",
                    "authorization_endpoint": "https://auth.example/authorize",
                    "token_endpoint": "https://auth.example/token",
                    "issuer": "https://auth.example",
                    "rotating_refresh_tokens": kind == "records",
                    "scopes": MAIL_GRANTED if kind == "mail" else ["records.read"],
                    "authorization_params": {"access_type": "offline", "prompt": "consent"},
                }
                item = IntegrationRegistration.model_validate(value)
                await service.integrations.create(item)
                service.providers[(app_id, kind)] = RemoteMCP(
                    item, transport_factory=fakes[kind].transport
                )

        http = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787"
            )
        )
        key = (await app.state.service.create_key("demo", "test"))["key"]
        saas = (await app.state.service.create_key("saas", "test"))["key"]
        rig = Rig(app, http, records, key, saas)
        rig.mail, rig.fakes = mail, fakes
        yield rig


async def start(rig, provider="records", headers=None, reconnect=None):
    headers = headers or rig.headers()
    payload = {"integration_id": provider, "return_url": RETURN, "app_state": APP_STATE}
    if reconnect:
        payload["reconnect_connection_id"] = reconnect
    response = await rig.http.post("/v1/connect-sessions", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    flow = response.json()
    response = await rig.http.get(flow["authorization_url"])
    assert response.status_code == 303, response.text
    query = {k: v[0] for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()}
    flow["authorize"] = query
    flow["callback"] = query["redirect_uri"]
    return flow


async def stage(rig, provider="records", code="alice", headers=None, reconnect=None):
    flow = await start(rig, provider, headers, reconnect)
    response = await rig.http.get(
        flow["callback"], params={"state": flow["authorize"]["state"], "code": code}
    )
    assert response.status_code == 303, response.text
    flow["return"] = {
        k: v[0] for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()
    }
    return flow


async def complete(rig, flow, headers=None):
    return await rig.http.post(
        f"/v1/connect-sessions/{flow['id']}/complete",
        headers=headers or rig.headers(),
        json={"completion_code": flow["return"]["completion_code"]},
    )


async def connected(rig, provider="records"):
    flow = await stage(rig, provider)
    result = await complete(rig, flow)
    assert result.status_code == 200, result.text
    return result.json()


@pytest.mark.parametrize("phase", ["token_exchange", "account_verification"])
async def test_oauth_failure_log_identifies_stage_without_credentials(
    oauth_rig, monkeypatch, caplog, phase
):
    rig = oauth_rig
    if phase == "account_verification":

        async def fail_identity(_):
            raise ValueError("SECRET")

        monkeypatch.setattr(rig.service.providers[("demo", "records")], "identify", fail_identity)
    flow = await stage(rig, code="bad" if phase == "token_exchange" else "alice")
    assert flow["return"]["status"] == "failed"
    assert f"session={flow['id']} phase={phase}" in caplog.text
    assert "SECRET" not in caplog.text
    assert "test-client-secret" not in caplog.text


async def expire_credential(rig, cid):
    async with rig.service.store.transaction() as db:
        row = await one(db, "SELECT * FROM connections WHERE id=?", (cid,))
        bundle = json.loads(
            rig.service.cipher.decrypt(row["ciphertext"], row["app_id"], row["tenant_id"], cid)
        )
        bundle["expires_at"] = deadline(-1)
        await db.execute(
            "UPDATE connections SET ciphertext=? WHERE id=?",
            (
                rig.service.cipher.encrypt(
                    json.dumps(bundle), row["app_id"], row["tenant_id"], cid
                ),
                cid,
            ),
        )


async def test_registered_oauth_uses_saved_secret_for_connect_and_refresh(oauth_rig):
    rig = oauth_rig
    body = rig.service.integrations.get("demo", "records").model_dump(mode="json")
    body["id"] = "registered"
    body["oauth"]["client_secret"] = "registered-client-secret"
    rig.fake.client_secret = "registered-client-secret"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_admin_app(rig.service)), base_url="http://admin"
    ) as admin:
        response = await admin.post("/admin/v1/integrations", json=body)
        assert response.status_code == 201, response.text
    item = rig.service.integrations.get("demo", "registered")
    rig.service.providers[("demo", "registered")] = RemoteMCP(
        item, transport_factory=rig.fake.transport
    )
    conn = await connected(rig, "registered")
    assert rig.fake.exchanges[-1]["client_secret"] == "registered-client-secret"
    session = await rig.session(conn)
    await expire_credential(rig, conn["id"])
    async with rig.mcp(session) as client:
        assert (await client.list_tools()).tools
    assert rig.fake.refreshes[-1]["grant_type"] == "refresh_token"
    assert rig.fake.refreshes[-1]["client_secret"] == "registered-client-secret"
    # Registered secrets persist without configuration or secret files.
    await rig.service.close()
    await rig.service.start()
    assert (
        rig.service.integrations.get("demo", "registered").oauth.client_secret.get_secret_value()
        == "registered-client-secret"
    )
    rig.fake.client_secret = "test-client-secret"
    assert (await connected(rig))["status"] == "active"


@pytest.mark.parametrize("provider", ["records", "mail"])
async def test_oauth_stages_then_activates_and_runs_real_mcp_sdk(oauth_rig, provider):
    rig = oauth_rig
    flow = await stage(rig, provider)
    assert flow["return"]["app_state"] == APP_STATE
    assert (await rig.http.get("/v1/connections", headers=rig.headers())).json()["data"] == []
    status = await rig.http.get(f"/v1/connect-sessions/{flow['id']}", headers=rig.headers())
    assert status.json()["status"] == "awaiting_completion"
    assert "completion_code" not in status.text and "refresh_token" not in status.text
    result = await complete(rig, flow)
    assert result.status_code == 200, result.text
    conn = result.json()
    assert conn["provider_account_id"] == "101"
    assert all(prefix not in result.text for prefix in ("record_", "mail_"))
    session = await rig.session(conn)
    async with rig.mcp(session) as client:
        tools = (await client.list_tools()).tools
        if provider == "mail":
            assert {t.name for t in tools} == {client.ravn_name(n) for n in MAIL_SCHEMAS}
            assert conn["display_name"] == "alice@example.com"
            result = await client.call_tool(client.ravn_name("search_threads"), {"query": "refund"})
            assert rig.mail.calls[-1][0] == "mail_alice-old"
            authorize = flow["authorize"]
            assert authorize["access_type"] == "offline" and authorize["prompt"] == "consent"
            verifier = rig.mail.exchanges[0]["code_verifier"]
            assert (
                authorize["code_challenge"]
                == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
        else:
            result = await client.call_tool(
                client.ravn_name("list_issues"), {"owner": "acme", "repo": "test"}
            )
            assert rig.fake.calls[-1][0] == "record_alice-old"
            verifier = rig.fake.exchanges[0]["code_verifier"]
            assert (
                flow["authorize"]["code_challenge"]
                == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
        assert not result.is_error
    assert (await complete(rig, flow)).status_code == 409
    status = (
        await rig.http.get(f"/v1/connect-sessions/{flow['id']}", headers=rig.headers())
    ).json()
    assert status["connection_id"] == conn["id"] and status["status"] == "completed"
    async with rig.service.store.transaction() as db:
        row = await one(db, "SELECT * FROM connect_sessions WHERE id=?", (flow["id"],))
        assert all(
            row[k] is None
            for k in (
                "staged",
                "verifier",
                "completion_hash",
                "ticket_hash",
                "state_hash",
                "cookie_hash",
            )
        )
        events = await rows(db, "SELECT * FROM events")
        assert "test-refresh" not in repr(events)
        assert all(prefix not in repr(events) for prefix in ("record_", "mail_"))


async def test_browser_cookie_state_replay_and_duplicate_queries(oauth_rig):
    rig = oauth_rig
    flow = await start(rig)
    assert (await rig.http.get(flow["authorization_url"])).status_code == 400
    assert (
        await rig.http.get(flow["callback"], params={"state": "wrong", "code": "alice"})
    ).status_code == 400
    query = urlencode({"state": flow["authorize"]["state"], "code": "alice"})
    assert (await rig.http.get(flow["callback"] + "?" + query + "&code=bob")).status_code == 400
    cookie = rig.http.cookies
    rig.http.cookies = httpx.Cookies()
    assert (await rig.http.get(flow["callback"] + "?" + query)).status_code == 400
    rig.http.cookies = cookie
    assert (
        await rig.http.get(flow["callback"].replace("demo/records", "demo/mail") + "?" + query)
    ).status_code == 400
    assert (await rig.http.get(flow["callback"] + "?" + query)).status_code == 303
    assert (await rig.http.get(flow["callback"] + "?" + query)).status_code == 400
    assert len(rig.fake.exchanges) == 1


async def test_wrong_actor_tenant_key_and_completion_code(oauth_rig):
    rig = oauth_rig
    headers = rig.headers(tenant="one", key=rig.saas_key)
    flow = await stage(rig, headers=headers)
    for wrong in [
        rig.headers("bob", "one", rig.saas_key),
        rig.headers(tenant="two", key=rig.saas_key),
        rig.headers(),
    ]:
        assert (await complete(rig, flow, wrong)).status_code == 404
    bad = await rig.http.post(
        f"/v1/connect-sessions/{flow['id']}/complete",
        headers=headers,
        json={"completion_code": "x" * 43},
    )
    assert bad.status_code == 400
    assert (await complete(rig, flow, headers)).status_code == 200


@pytest.mark.parametrize("suffix", ["?next=https://evil.example", "/extra", "#fragment"])
async def test_exact_return_allowlist(oauth_rig, suffix):
    response = await oauth_rig.http.post(
        "/v1/connect-sessions",
        headers=oauth_rig.headers(),
        json={"integration_id": "records", "return_url": RETURN + suffix, "app_state": APP_STATE},
    )
    assert response.status_code == 400


@pytest.mark.parametrize("action", ["cancel", "expire", "revoke_key"])
async def test_staged_credentials_cleared_on_terminal(oauth_rig, action):
    rig = oauth_rig
    flow = await stage(rig)
    if action == "cancel":
        assert (
            await rig.http.post(f"/v1/connect-sessions/{flow['id']}/cancel", headers=rig.headers())
        ).status_code == 200
    elif action == "expire":
        async with rig.service.store.transaction() as db:
            await db.execute(
                "UPDATE connect_sessions SET completion_expires_at=? WHERE id=?",
                (deadline(-1), flow["id"]),
            )
        await rig.service.oauth.cleanup()
    else:
        p = await rig.service.authenticate_app(rig.key, "alice", None)
        await rig.service.revoke_key(p.key_id)
    assert (await complete(rig, flow)).status_code in {401, 409, 410}
    async with rig.service.store.transaction() as db:
        row = await one(db, "SELECT * FROM connect_sessions WHERE id=?", (flow["id"],))
        assert row["staged"] is None and row["completion_hash"] is None


async def test_provider_denial_and_failure_never_return_provider_diagnostics(oauth_rig):
    rig = oauth_rig
    flow = await start(rig)
    r = await rig.http.get(
        flow["callback"],
        params={
            "state": flow["authorize"]["state"],
            "error": "RAW_SECRET",
            "error_description": "RAW_SECRET",
        },
    )
    assert r.status_code == 303 and "RAW_SECRET" not in r.headers["location"]
    assert "status=denied" in r.headers["location"]
    flow = await stage(rig, code="bad")
    assert flow["return"]["status"] == "failed"
    assert "SECRET" not in str(flow)


@pytest.mark.parametrize("provider", ["records", "mail"])
async def test_reconnect_preserves_identity_invalidates_attachments(oauth_rig, provider):
    rig = oauth_rig
    conn = await connected(rig, provider)
    session = await rig.session(conn)
    mismatch = await stage(rig, provider, code="bob", reconnect=conn["id"])
    assert mismatch["return"]["status"] == "failed"
    assert (await rig.http.get(f"/v1/connections/{conn['id']}", headers=rig.headers())).json()[
        "epoch"
    ] == 1
    flow = await stage(rig, provider, reconnect=conn["id"])
    result = await complete(rig, flow)
    assert result.json()["id"] == conn["id"] and result.json()["epoch"] == 2
    principal = await rig.service.authenticate_session(session["token"])
    with pytest.raises(RavnError):
        await rig.service.discover(principal, conn["id"])
    response = await rig.http.put(
        f"/v1/sessions/{session['id']}/connections/{conn['id']}", headers=rig.headers()
    )
    assert response.status_code == 200
    assert await rig.service.discover(principal, conn["id"])


async def test_disconnect_wins_pending_reconnect(oauth_rig):
    rig = oauth_rig
    conn = await connected(rig)
    flow = await stage(rig, reconnect=conn["id"])
    await rig.http.post(f"/v1/connections/{conn['id']}/disconnect", headers=rig.headers())
    assert (await complete(rig, flow)).status_code == 409
    async with rig.service.store.transaction() as db:
        conn = await one(db, "SELECT * FROM connections WHERE id=?", (conn["id"],))
        assert conn["ciphertext"] is None and conn["status"] == "disconnected"


@pytest.mark.parametrize("provider", ["records", "mail"])
async def test_refresh_single_flight_preserves_runtime_epoch(oauth_rig, provider):
    rig = oauth_rig
    conn = await connected(rig, provider)
    session = await rig.session(conn)
    await expire_credential(rig, conn["id"])
    p = await rig.service.authenticate_session(session["token"])
    values = await asyncio.gather(*(rig.service.discover(p, conn["id"]) for _ in range(4)))
    fake = rig.fakes[provider]
    assert len(fake.refreshes) == 1 and all(values)
    if provider == "mail":
        # The provider's refresh response omits the refresh token; the stable one is kept.
        async with rig.service.store.transaction() as db:
            row = await one(db, "SELECT * FROM connections WHERE id=?", (conn["id"],))
        bundle = json.loads(
            rig.service.cipher.decrypt(
                row["ciphertext"], row["app_id"], row["tenant_id"], row["id"]
            )
        )
        assert bundle["refresh_token"] == "test-refresh-mail_alice-old"
        assert bundle["access_token"] == "mail_alice-new"
    async with rig.service.store.transaction() as db:
        row = await one(db, "SELECT * FROM connections WHERE id=?", (conn["id"],))
        assert (
            row["epoch"] == 1 and row["credential_version"] == 2 and row["refresh_attempt"] is None
        )
    await rig.service.authenticate_session(session["token"])
    assert not rig.service.oauth.refresh_locks


@pytest.mark.parametrize("action", ["detach", "revoke", "expire"])
async def test_refresh_preserves_shared_connection_when_caller_loses_access(oauth_rig, action):
    rig = oauth_rig
    conn = await connected(rig)
    first, second = await rig.session(conn), await rig.session(conn)
    caller = await rig.service.authenticate_session(first["token"])
    other = await rig.service.authenticate_session(second["token"])
    await expire_credential(rig, conn["id"])
    rig.fake.rotation_block = True
    pending = asyncio.create_task(rig.service.discover(caller, conn["id"]))
    await asyncio.wait_for(rig.fake.rotation_entered.wait(), 2)
    try:
        if action == "detach":
            response = await rig.http.delete(
                f"/v1/sessions/{first['id']}/connections/{conn['id']}", headers=rig.headers()
            )
            assert response.status_code == 200
        elif action == "revoke":
            response = await rig.http.post(
                f"/v1/sessions/{first['id']}/revoke", headers=rig.headers()
            )
            assert response.status_code == 200
        else:
            async with rig.service.store.transaction() as db:
                await db.execute(
                    "UPDATE sessions SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
                    (first["id"],),
                )
    finally:
        rig.fake.rotation_release.set()
    with pytest.raises(RavnError):
        await pending
    current = (await rig.http.get(f"/v1/connections/{conn['id']}", headers=rig.headers())).json()
    assert current["status"] == "active"
    assert await rig.service.discover(other, conn["id"])
    assert len(rig.fake.refreshes) == 1


@pytest.mark.parametrize("race", ["disconnect", "reconnect", "cancel", "lost_response"])
async def test_refresh_uncertainty_and_concurrent_lifecycle(oauth_rig, race):
    rig = oauth_rig
    conn = await connected(rig)
    p = await rig.service.authenticate_app(rig.key, "alice", None)
    flow = await stage(rig, reconnect=conn["id"])
    await expire_credential(rig, conn["id"])
    rig.fake.rotation_block = True
    task = asyncio.create_task(rig.service.discover(p, conn["id"]))
    await rig.fake.rotation_entered.wait()
    if race == "disconnect":
        await rig.service.disconnect(p, conn["id"])
    elif race == "reconnect":
        assert (await complete(rig, flow)).status_code == 200
        rig.fake.rotation_failure = True  # Late failure must not poison new authority.
    elif race == "cancel":
        task.cancel()
    else:
        rig.fake.rotation_failure = True
    rig.fake.rotation_release.set()
    with pytest.raises((RavnError, asyncio.CancelledError)):
        await task
    row = (await rig.http.get(f"/v1/connections/{conn['id']}", headers=rig.headers())).json()
    assert row["status"] == {"disconnect": "disconnected", "reconnect": "active"}.get(
        race, "reconnect_required"
    )
    if race == "lost_response":
        with pytest.raises(RavnError):
            await rig.service.discover(p, conn["id"])
        assert len(rig.fake.refreshes) == 1


@pytest.mark.parametrize("refresh_fails", [False, True])
async def test_late_rejection_during_refresh_defers_to_refresh_outcome(oauth_rig, refresh_fails):
    rig = oauth_rig
    conn = await connected(rig)
    p = await rig.service.authenticate_app(rig.key, "alice", None)
    old, _ = await rig.service.credential(p, conn["id"])
    await expire_credential(rig, conn["id"])
    rig.fake.rotation_block = True
    rig.fake.rotation_failure = refresh_fails
    pending = asyncio.create_task(rig.service.discover(p, conn["id"]))
    await asyncio.wait_for(rig.fake.rotation_entered.wait(), 2)
    try:
        await rig.service.oauth.rejected(p, conn["id"], old)
    finally:
        rig.fake.rotation_release.set()
    if refresh_fails:
        with pytest.raises(RavnError):
            await pending
    else:
        assert await pending
        # The same old response is harmless after the new credential is saved, too.
        await rig.service.oauth.rejected(p, conn["id"], old)
    current = (await rig.http.get(f"/v1/connections/{conn['id']}", headers=rig.headers())).json()
    assert current["status"] == ("reconnect_required" if refresh_fails else "active")


async def test_restart_uncertain_refresh_and_interrupted_exchange(oauth_rig):
    rig = oauth_rig
    conn = await connected(rig)
    flow = await start(rig)
    async with rig.service.store.transaction() as db:
        await db.execute(
            "UPDATE connections SET refresh_attempt='interrupted' WHERE id=?", (conn["id"],)
        )
        await db.execute(
            "UPDATE connect_sessions SET status='exchanging' WHERE id=?", (flow["id"],)
        )
    await rig.service.close()
    await rig.service.start()
    async with rig.service.store.transaction() as db:
        assert (await one(db, "SELECT * FROM connections WHERE id=?", (conn["id"],)))[
            "status"
        ] == "reconnect_required"
        row = await one(db, "SELECT * FROM connect_sessions WHERE id=?", (flow["id"],))
        assert row["status"] == "failed" and row["verifier"] is None
    assert not rig.fake.refreshes


async def test_inspection_uses_stored_oauth_without_exposing_tokens(oauth_rig):
    rig = oauth_rig
    conn = await connected(rig, "mail")
    response = await rig.http.get(
        f"/v1/connections/{conn['id']}/tool-schemas", headers=rig.headers()
    )
    assert response.status_code == 200 and set(response.json()["schema_hashes"]) == set(
        MAIL_SCHEMAS
    )
    assert "mail_alice-old" not in response.text
    assert (
        await rig.http.get(f"/v1/connections/{conn['id']}/tool-schemas", headers=rig.headers("bob"))
    ).status_code == 404


async def test_http_auth_failure_marks_reconnect_not_provider_outage(oauth_rig):
    rig = oauth_rig
    conn = await connected(rig)
    original = rig.service.provider_for(
        rig.service.integrations.get("demo", "records")
    ).transport_factory

    def rejected(host):
        if host == "mcp.records.example":
            return httpx2.MockTransport(lambda req: httpx2.Response(401, json={"error": "SECRET"}))
        return original(host)

    rig.service.provider_for(
        rig.service.integrations.get("demo", "records")
    ).transport_factory = rejected
    session = await rig.session(conn)
    principal = await rig.service.authenticate_session(session["token"])
    with pytest.raises(RavnError) as error:
        await rig.service.discover(principal, conn["id"])
    assert error.value.code == "credential_invalid" and "SECRET" not in str(error.value)
    assert (await rig.http.get(f"/v1/connections/{conn['id']}", headers=rig.headers())).json()[
        "status"
    ] == "reconnect_required"


async def test_schema_change_and_revoked_key_block_pending_flow(oauth_rig):
    rig = oauth_rig
    flow = await start(rig)
    p = await rig.service.authenticate_app(rig.key, "alice", None)
    await rig.service.revoke_key(p.key_id)
    response = await rig.http.get(
        flow["callback"], params={"state": flow["authorize"]["state"], "code": "alice"}
    )
    assert response.status_code == 400
    assert not rig.fake.exchanges


async def test_pending_limit_and_completion_expiry_bounded(oauth_rig):
    rig = oauth_rig
    flow = await start(rig)
    expiry = deadline(30)
    async with rig.service.store.transaction() as db:
        await db.execute(
            "UPDATE connect_sessions SET expires_at=? WHERE id=?", (expiry, flow["id"])
        )
    await rig.http.get(
        flow["callback"], params={"state": flow["authorize"]["state"], "code": "alice"}
    )
    row = (await rig.http.get(f"/v1/connect-sessions/{flow['id']}", headers=rig.headers())).json()
    assert row["completion_expires_at"] == expiry
    for _ in range(7):
        await start(rig)
    response = await rig.http.post(
        "/v1/connect-sessions",
        headers=rig.headers(),
        json={"integration_id": "records", "return_url": RETURN, "app_state": APP_STATE},
    )
    assert response.status_code == 429


async def test_two_browser_transactions_can_complete_independently(oauth_rig):
    rig = oauth_rig
    first, second = await start(rig), await start(rig, "mail")
    for flow in (first, second):
        response = await rig.http.get(
            flow["callback"], params={"state": flow["authorize"]["state"], "code": "alice"}
        )
        assert response.status_code == 303
    assert len(rig.fake.exchanges) == len(rig.mail.exchanges) == 1


async def test_multi_service_session_survives_refresh_and_other_account_reauthorization(oauth_rig):
    rig = oauth_rig
    records, mail = await connected(rig), await connected(rig, "mail")
    response = await rig.http.post(
        "/v1/sessions", headers=rig.headers(), json={"connection_ids": [records["id"], mail["id"]]}
    )
    assert response.status_code == 201
    session = response.json()
    await expire_credential(rig, mail["id"])
    async with rig.mcp(session) as client:
        result = await client.list_tools()
        assert {t.meta["ravn/connection-id"] for t in result.tools} == {records["id"], mail["id"]}
        assert rig.mail.refreshes
        flow = await stage(rig, reconnect=records["id"])
        assert (await complete(rig, flow)).status_code == 200
        result = await client.list_tools()
        assert {t.meta["ravn/connection-id"] for t in result.tools} == {mail["id"]}
        assert result.meta["ravn/unavailable-connections"] == [
            {"connection_id": records["id"], "code": "connection_not_attached"}
        ]
        assert (
            await rig.http.put(
                f"/v1/sessions/{session['id']}/connections/{records['id']}", headers=rig.headers()
            )
        ).status_code == 200
        assert {t.meta["ravn/connection-id"] for t in (await client.list_tools()).tools} == {
            records["id"],
            mail["id"],
        }
        await rig.http.post(f"/v1/connections/{records['id']}/disconnect", headers=rig.headers())
        assert {t.meta["ravn/connection-id"] for t in (await client.list_tools()).tools} == {
            mail["id"]
        }


async def test_oauth_without_identity_creates_distinct_connections_and_refreshes(oauth_rig):
    rig = oauth_rig
    item = rig.service.integrations.get("demo", "records").model_copy(update={"identity": None})
    rig.service.integrations.items[("demo", "records")] = item
    rig.service.providers[("demo", "records")] = RemoteMCP(
        item, transport_factory=rig.fake.transport
    )
    flow = await stage(rig)
    response = await rig.http.post(
        f"/v1/connect-sessions/{flow['id']}/complete",
        headers=rig.headers(),
        json={"completion_code": flow["return"]["completion_code"], "label": "Work account"},
    )
    assert response.status_code == 200, response.text
    conn = response.json()
    assert rig.fake.headers  # Access was verified through MCP, without a userinfo request.
    assert conn["provider_account_id"] is None and conn["display_name"] == "Work account"
    assert not conn["reconnect_supported"]
    session = await rig.session(conn)
    await expire_credential(rig, conn["id"])
    p = await rig.service.authenticate_session(session["token"])
    credential, _ = await rig.service.credential(p, conn["id"])
    assert credential.endswith("-new") and len(rig.fake.refreshes) == 1
    updated = (await rig.http.get(f"/v1/connections/{conn['id']}", headers=rig.headers())).json()
    assert updated["epoch"] == conn["epoch"] and updated["provider_account_id"] is None
    denied = await rig.http.post(
        "/v1/connect-sessions",
        headers=rig.headers(),
        json={
            "integration_id": "records",
            "return_url": RETURN,
            "app_state": APP_STATE,
            "reconnect_connection_id": conn["id"],
        },
    )
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "new_connection_required"
    replacement = (await complete(rig, await stage(rig, code="bob"))).json()
    assert replacement["id"] != conn["id"]
    existing = (await rig.http.get(f"/v1/sessions/{session['id']}", headers=rig.headers())).json()
    assert [c["id"] for c in existing["connections"]] == [conn["id"]]
    async with rig.mcp(session) as client:
        assert (await client.list_tools()).tools
