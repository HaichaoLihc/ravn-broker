import asyncio
import base64
import hashlib
import json
from contextlib import AsyncExitStack
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import httpx2
import pytest
from conftest import FakeGitHub, Rig
from mcp import types
from mcp.server.transport_security import TransportSecuritySettings

from ravn.app import create_app
from ravn.common import RavnError
from ravn.config import GMAIL_COMPOSE, GMAIL_READONLY, Config
from ravn.github import GitHub
from ravn.gmail import Gmail
from ravn.manifest import GMAIL_SCHEMAS, SLACK_SCHEMAS, schema_hash
from ravn.oauth_provider import deadline
from ravn.slack import Slack
from ravn.store import one, rows

pytestmark = pytest.mark.anyio
RETURN = "http://127.0.0.1:8800/return"
APP_STATE = "app_transaction_0123456789"
# Google reports the requested `email` scope in its long form.
GMAIL_GRANTED = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    GMAIL_READONLY,
    GMAIL_COMPOSE,
]


class OAuthFake(FakeGitHub):
    def __init__(self, slack=False, gmail=False):
        super().__init__()
        self.slack, self.gmail = slack, gmail
        self.exchanges = []
        self.refreshes = []
        self.rotation_failure = False
        self.rotation_block = False
        self.rotation_entered = asyncio.Event()
        self.rotation_release = asyncio.Event()
        self.tokens = {}
        if gmail:
            self.tools = [types.Tool(name=n, inputSchema=s) for n, s in GMAIL_SCHEMAS.items()]
            self.app = self.server.streamable_http_app(
                streamable_http_path="/mcp/v1",
                stateless_http=True,
                json_response=True,
                transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
            )
        if slack:
            self.tools = [types.Tool(name=n, inputSchema=s) for n, s in SLACK_SCHEMAS.items()]
            self.tools.append(
                types.Tool(
                    name="slack_send_message",
                    inputSchema={"type": "object"},
                    annotations=types.ToolAnnotations(readOnlyHint=True),
                )
            )
            self.app = self.server.streamable_http_app(
                streamable_http_path="/mcp",
                stateless_http=True,
                json_response=True,
                transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
            )

    def transport(self, host):
        if host in {"api.githubcopilot.com", "mcp.slack.com", "gmailmcp.googleapis.com"}:
            return httpx2.ASGITransport(app=self.app)

        async def request(req):
            if req.url.path in {"/login/oauth/access_token", "/api/oauth.v2.user.access", "/token"}:
                params = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
                assert params["client_secret"] == "test-client-secret"
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
                prefix = "ya29." if self.gmail else "xoxp-" if self.slack else "ghu_"
                access = prefix + who + ("-new" if refresh else "-old")
                self.tokens[access] = (
                    "T222" if who == "workspace" else "T111",
                    "U222" if who == "bob" else "U111",
                )
                if self.gmail:
                    # Google's shape: URL scopes, and refresh never rotates the refresh token.
                    return httpx2.Response(
                        200,
                        json={
                            "access_token": access,
                            "token_type": "Bearer",
                            "expires_in": 3599,
                            "scope": " ".join(GMAIL_GRANTED),
                            **({} if refresh else {"refresh_token": "test-refresh-" + access}),
                        },
                    )
                return httpx2.Response(
                    200,
                    json={
                        "ok": True,
                        "access_token": access,
                        "token_type": "user" if self.slack else "bearer",
                        "refresh_token": "test-refresh-" + access,
                        "expires_in": 3600,
                        "refresh_token_expires_in": 86400,
                        "scope": "search:read.public" if self.slack else "",
                    },
                )
            access = req.headers.get("authorization", "").removeprefix("Bearer ")
            if access not in self.tokens:
                return httpx2.Response(401, json={"ok": False, "error": "invalid_auth"})
            team, user = self.tokens[access]
            if self.gmail:
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
async def oauth_rig(config, tmp_path):
    secret = tmp_path / "oauth-secret"
    secret.write_text("test-client-secret")
    secret.chmod(0o600)
    value = config.model_dump(mode="json")
    for app in value["applications"]:
        app["return_urls"] = [RETURN]
    for i in value["integrations"]:
        i["oauth"] = {
            "client_id": "test-github",
            "client_secret_file": str(secret),
            "profile": "github_app_pkce",
        }
    github, slack, gmail = OAuthFake(), OAuthFake(slack=True), OAuthFake(gmail=True)
    for app in value["applications"]:
        value["integrations"].append(
            {
                "id": "gmail",
                "app_id": app["id"],
                "connector": "gmail",
                "endpoint": "https://gmailmcp.googleapis.com/mcp/v1",
                "manifest": "builtin:gmail-v1",
                "schema_hashes": {t.name: schema_hash(t) for t in gmail.tools},
                "oauth": {
                    "client_id": "test-google",
                    "client_secret_file": str(secret),
                    "profile": "google_web_pkce",
                    "scopes": ["openid", "email", GMAIL_READONLY, GMAIL_COMPOSE],
                },
            }
        )
        value["integrations"].append(
            {
                "id": "slack",
                "app_id": app["id"],
                "connector": "slack",
                "endpoint": "https://mcp.slack.com/mcp",
                "manifest": "builtin:slack-read-v1",
                "schema_hashes": {
                    t.name: schema_hash(t) for t in slack.tools if t.name in SLACK_SCHEMAS
                },
                "oauth": {
                    "client_id": "test-slack",
                    "client_secret_file": str(secret),
                    "profile": "slack_user_confidential",
                    "scopes": ["search:read.public", "channels:history"],
                },
            }
        )
    config = Config.model_validate(value)
    app = create_app(
        config,
        provider={
            "github_cloud": GitHub(transport_factory=github.transport),
            "slack": Slack(transport_factory=slack.transport),
            "gmail": Gmail(transport_factory=gmail.transport),
        },
    )
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(github.server.session_manager.run())
        await stack.enter_async_context(slack.server.session_manager.run())
        await stack.enter_async_context(gmail.server.session_manager.run())
        await stack.enter_async_context(app.router.lifespan_context(app))
        http = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787"
            )
        )
        key = (await app.state.service.create_key("demo", "test"))["key"]
        saas = (await app.state.service.create_key("saas", "test"))["key"]
        rig = Rig(app, http, github, key, saas)
        rig.slack, rig.gmail = slack, gmail
        rig.fakes = {"github": github, "slack": slack, "gmail": gmail}
        yield rig


async def start(rig, provider="github", headers=None, reconnect=None):
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


async def stage(rig, provider="github", code="alice", headers=None, reconnect=None):
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


async def connected(rig, provider="github"):
    flow = await stage(rig, provider)
    result = await complete(rig, flow)
    assert result.status_code == 200, result.text
    return result.json()


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


@pytest.mark.parametrize("provider", ["github", "slack", "gmail"])
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
    assert conn["provider_account_id"] == ("T111:U111" if provider == "slack" else "101")
    assert all(prefix not in result.text for prefix in ("ghu_", "xoxp-", "ya29."))
    session = await rig.session(conn)
    async with rig.mcp(session) as client:
        tools = (await client.list_tools()).tools
        if provider == "slack":
            assert {t.name for t in tools} == set(SLACK_SCHEMAS)
            result = await client.call_tool("slack_search_public", {"query": "project ravn"})
            assert rig.slack.calls[-1][0] == "xoxp-alice-old"
            assert "x-mcp-toolsets" not in rig.slack.headers[-1]
        elif provider == "gmail":
            assert {t.name for t in tools} == set(GMAIL_SCHEMAS)
            assert conn["display_name"] == "alice@example.com"
            result = await client.call_tool("search_threads", {"query": "refund"})
            assert rig.gmail.calls[-1][0] == "ya29.alice-old"
            authorize = flow["authorize"]
            assert authorize["access_type"] == "offline" and authorize["prompt"] == "consent"
            verifier = rig.gmail.exchanges[0]["code_verifier"]
            assert (
                authorize["code_challenge"]
                == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
        else:
            result = await client.call_tool("list_issues", {"owner": "acme", "repo": "test"})
            assert rig.fake.calls[-1][0] == "ghu_alice-old"
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
        assert all(prefix not in repr(events) for prefix in ("ghu_", "xoxp-", "ya29."))


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
        await rig.http.get(flow["callback"].replace("demo/github", "demo/slack") + "?" + query)
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
        json={"integration_id": "github", "return_url": RETURN + suffix, "app_state": APP_STATE},
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


@pytest.mark.parametrize("provider", ["github", "slack", "gmail"])
async def test_reconnect_preserves_identity_invalidates_sessions(oauth_rig, provider):
    rig = oauth_rig
    conn = await connected(rig, provider)
    session = await rig.session(conn)
    mismatch = await stage(rig, provider, code="bob", reconnect=conn["id"])
    assert mismatch["return"]["status"] == "failed"
    assert (await rig.http.get(f"/v1/connections/{conn['id']}", headers=rig.headers())).json()[
        "epoch"
    ] == 1
    if provider == "slack":
        mismatch = await stage(rig, provider, code="workspace", reconnect=conn["id"])
        assert mismatch["return"]["status"] == "failed"
    flow = await stage(rig, provider, reconnect=conn["id"])
    result = await complete(rig, flow)
    assert result.json()["id"] == conn["id"] and result.json()["epoch"] == 2
    with pytest.raises(RavnError):
        await rig.service.authenticate_session(session["token"])


async def test_disconnect_wins_pending_reconnect(oauth_rig):
    rig = oauth_rig
    conn = await connected(rig)
    flow = await stage(rig, reconnect=conn["id"])
    await rig.http.post(f"/v1/connections/{conn['id']}/disconnect", headers=rig.headers())
    assert (await complete(rig, flow)).status_code == 409
    async with rig.service.store.transaction() as db:
        conn = await one(db, "SELECT * FROM connections WHERE id=?", (conn["id"],))
        assert conn["ciphertext"] is None and conn["status"] == "disconnected"


@pytest.mark.parametrize("provider", ["github", "slack", "gmail"])
async def test_refresh_single_flight_preserves_runtime_epoch(oauth_rig, provider):
    rig = oauth_rig
    conn = await connected(rig, provider)
    session = await rig.session(conn)
    await expire_credential(rig, conn["id"])
    p = await rig.service.authenticate_session(session["token"])
    values = await asyncio.gather(*(rig.service.discover(p, conn["id"]) for _ in range(4)))
    fake = rig.fakes[provider]
    assert len(fake.refreshes) == 1 and all(values)
    if provider == "gmail":
        # Google's refresh response omits the refresh token; the stable one is kept.
        async with rig.service.store.transaction() as db:
            row = await one(db, "SELECT * FROM connections WHERE id=?", (conn["id"],))
        bundle = json.loads(
            rig.service.cipher.decrypt(
                row["ciphertext"], row["app_id"], row["tenant_id"], row["id"]
            )
        )
        assert bundle["refresh_token"] == "test-refresh-ya29.alice-old"
        assert bundle["access_token"] == "ya29.alice-new"
    async with rig.service.store.transaction() as db:
        row = await one(db, "SELECT * FROM connections WHERE id=?", (conn["id"],))
        assert (
            row["epoch"] == 1 and row["credential_version"] == 2 and row["refresh_attempt"] is None
        )
    await rig.service.authenticate_session(session["token"])
    assert not rig.service.oauth.refresh_locks


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


async def test_slack_import_rejects_bot_token(oauth_rig):
    response = await oauth_rig.http.post(
        "/v1/connections/import",
        headers=oauth_rig.headers(),
        json={
            "integration_id": "slack",
            "credential": {"type": "bearer", "token": "xoxb-bot-token"},
        },
    )
    assert response.status_code == 400


async def test_slack_cross_provider_and_writes_blocked(oauth_rig):
    rig = oauth_rig
    conn = await connected(rig, "slack")
    session = await rig.session(conn)
    p = await rig.service.authenticate_session(session["token"])
    for name, args in [
        ("list_issues", {"owner": "acme", "repo": "test"}),
        ("slack_send_message", {"text": "hi"}),
    ]:
        with pytest.raises(RavnError):
            await rig.service.execute(p, conn["id"], name, args, "request")
    assert not rig.slack.calls and not rig.fake.calls


async def test_inspection_uses_stored_oauth_without_exposing_tokens(oauth_rig):
    rig = oauth_rig
    conn = await connected(rig, "slack")
    response = await rig.http.get(
        f"/v1/connections/{conn['id']}/tool-schemas", headers=rig.headers()
    )
    assert response.status_code == 200 and set(response.json()["schema_hashes"]) == set(
        SLACK_SCHEMAS
    )
    assert "xoxp-" not in response.text and "slack_send_message" not in response.text
    assert (
        await rig.http.get(f"/v1/connections/{conn['id']}/tool-schemas", headers=rig.headers("bob"))
    ).status_code == 404


async def test_http_auth_failure_marks_reconnect_not_provider_outage(oauth_rig):
    rig = oauth_rig
    conn = await connected(rig)
    original = rig.service.provider.transport_factory

    def rejected(host):
        if host == "api.githubcopilot.com":
            return httpx2.MockTransport(lambda req: httpx2.Response(401, json={"error": "SECRET"}))
        return original(host)

    rig.service.provider.transport_factory = rejected
    response = await rig.http.post(
        "/v1/sessions", headers=rig.headers(), json={"connection_id": conn["id"]}
    )
    assert response.status_code == 401 and "SECRET" not in response.text
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
        json={"integration_id": "github", "return_url": RETURN, "app_state": APP_STATE},
    )
    assert response.status_code == 429


async def test_two_browser_transactions_can_complete_independently(oauth_rig):
    rig = oauth_rig
    first, second = await start(rig), await start(rig, "slack")
    for flow in (first, second):
        response = await rig.http.get(
            flow["callback"], params={"state": flow["authorize"]["state"], "code": "alice"}
        )
        assert response.status_code == 303
    assert len(rig.fake.exchanges) == len(rig.slack.exchanges) == 1


@pytest.mark.parametrize("change", ["redirect", "connector", "scope", "profile"])
async def test_config_rejects_unregistered_oauth_destinations(oauth_rig, change):
    value = oauth_rig.service.config.model_dump(mode="json")
    if change == "redirect":
        value["applications"][0]["return_urls"] = ["https://app.example/return?redirect=evil"]
    elif change == "connector":
        value["integrations"][0]["endpoint"] = "https://evil.example/mcp"
    elif change == "scope":
        value["integrations"][-1]["oauth"]["scopes"] = ["chat:write"]
    else:
        value["integrations"][0]["oauth"]["profile"] = "slack_user_confidential"
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Config.model_validate(value)
