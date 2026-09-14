"""Customer browser → developer app → RAVN → simulated OAuth / real MCP SDK."""

import re
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import httpx2
import pytest

from examples.support_desk.__main__ import simulated_ravn
from examples.support_desk.app import AppError, Desk, Login, RunBody, create_app, hashed
from ravn.console import COOKIE, PREFIX, Console, create_console_app

pytestmark = pytest.mark.anyio
APP = "http://127.0.0.1:18880"
RAVN = "http://127.0.0.1:18881"
CONSENT = "http://127.0.0.1:18883"


class BrowserTransport(httpx.AsyncBaseTransport):
    def __init__(self, apps):
        self.apps = {port: httpx.ASGITransport(app=app) for port, app in apps.items()}

    async def handle_async_request(self, request):
        assert request.url.host == "127.0.0.1"
        return await self.apps[request.url.port].handle_async_request(request)


async def login(rig, client=None):
    client = client or rig.browser
    url = rig.desk.ticket()
    ticket = urlsplit(url).fragment.removeprefix("login=")
    response = await client.post("/api/login", headers={"Origin": APP}, json={"ticket": ticket})
    assert response.status_code == 200, response.text
    assert "httponly" in response.headers["set-cookie"].lower()
    state = await client.get("/api/state")
    assert state.status_code == 200, state.text
    return {"Origin": APP, "X-CSRF-Token": state.json()["csrf"]}


@pytest.fixture
async def desk_rig(tmp_path):
    async with simulated_ravn(tmp_path, APP, RAVN, CONSENT) as (broker, consent, key, providers):
        desk = Desk(
            origin=APP,
            ravn_url=RAVN,
            app_key=key,
            demo=True,
            transport=httpx.ASGITransport(app=broker),
            mcp_transport=httpx2.ASGITransport(app=broker),
        )
        app = create_app(desk)
        transport = BrowserTransport({18880: app, 18881: broker, 18883: consent})
        async with httpx.AsyncClient(
            base_url=APP, transport=transport, follow_redirects=False
        ) as browser:
            rig = SimpleNamespace(
                desk=desk,
                app=app,
                broker=broker,
                browser=browser,
                transport=transport,
                key=key,
                providers=providers,
            )
            rig.headers = await login(rig)
            yield rig


async def staged(rig, kind="github", decision="allow", reconnect=None, fetch=False):
    result = await rig.browser.post(
        "/api/connect",
        headers=rig.headers,
        json={
            "integration_id": kind,
            **({"reconnect_connection_id": reconnect} if reconnect else {}),
        },
    )
    assert result.status_code == 200, result.text
    start = await rig.browser.get(result.json()["authorization_url"])
    assert start.status_code == 303, start.text
    consent = await rig.browser.get(start.headers["location"])
    assert consent.status_code == 200, consent.text
    assert "SIMULATED PROVIDER" in consent.text
    ticket = re.search(r'name="ticket" value="([A-Za-z0-9_-]+)"', consent.text)[1]
    accepted = await rig.browser.post(
        CONSENT + "/consent",
        headers={"Origin": CONSENT, **({"Accept": "application/json"} if fetch else {})},
        data={"ticket": ticket, "decision": decision},
    )
    assert accepted.status_code == (200 if fetch else 303), accepted.text
    callback = await rig.browser.get(
        accepted.json()["redirect_url"] if fetch else accepted.headers["location"]
    )
    assert callback.status_code == 303, callback.text
    return callback.headers["location"]


async def connected(rig, kind="github", reconnect=None):
    target = await staged(rig, kind, reconnect=reconnect)
    response = await rig.browser.get(target)
    assert response.status_code == 303, response.text
    state = (await rig.browser.get("/api/state")).json()
    return next(
        c for c in state["connections"] if c["integration_id"] == kind and c["status"] == "active"
    )


DEFAULT_READS = {
    "github": (
        "issue_read",
        {"owner": "acme", "repo": "help-center", "issue_number": 42, "method": "get"},
    ),
    "slack": ("slack_search_public", {"query": "refund", "limit": 5}),
    "gmail": ("search_threads", {"query": "refund", "pageSize": 5}),
}
DRAFT = {
    "to": ["dana@example.com"],
    "subject": "Re: Refund for order #1042 has no confirmation",
    "body": "Hi Dana, your refund is processing.",
    "replyToMessageId": "18f2a0c4d5e6f701",
}


async def run_tool(rig, connection, tool=None, arguments=None, key=None):
    default_tool, default_arguments = DEFAULT_READS[connection["integration_id"]]
    return await rig.browser.post(
        "/api/run",
        headers=rig.headers,
        json={
            "connection_id": connection["id"],
            "tool": tool or default_tool,
            "arguments": arguments or default_arguments,
            **({"idempotency_key": key} if key else {}),
        },
    )


@pytest.mark.parametrize("kind", ["github", "slack", "gmail"])
async def test_full_connection_mcp_read_revoke_and_disconnect(desk_rig, kind):
    rig = desk_rig
    conn = await connected(rig, kind)
    first = await run_tool(rig, conn)
    assert first.status_code == 200, first.text
    result = first.json()
    assert result["status"] == "succeeded"
    assert "simulated" in result["text"]
    assert ("Refund" if kind == "github" else "1042") in result["text"]
    assert result["duration_ms"] >= 0
    assert result["call_id"].startswith("call_")
    assert len(result["steps"]) == 4
    assert rig.providers[kind].calls == [DEFAULT_READS[kind][0]]
    second = await run_tool(rig, conn)
    assert second.json()["session_id"] == result["session_id"]
    state = await rig.browser.get("/api/state")
    private = [rig.key, *rig.providers[kind].tokens, *rig.providers[kind].refreshes]
    private.extend(
        v["token"] for owner in rig.desk.logins.values() for v in owner.runtimes.values()
    )
    for value in private:
        assert value not in state.text + first.text + second.text
    sid = result["session_id"]
    revoked = await rig.browser.post(f"/api/sessions/{sid}/revoke", headers=rig.headers, json={})
    assert revoked.status_code == 200
    again = await run_tool(rig, conn)
    assert again.status_code == 200, again.text
    assert again.json()["session_id"] != sid
    disconnected = await rig.browser.post(
        f"/api/connections/{conn['id']}/disconnect", headers=rig.headers, json={}
    )
    assert disconnected.status_code == 200
    count = len(rig.providers[kind].calls)
    blocked = await run_tool(rig, conn)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "connection_disabled"
    assert len(rig.providers[kind].calls) == count


async def test_cancel_and_callback_replay(desk_rig):
    rig = desk_rig
    target = await staged(rig, decision="deny")
    assert (await rig.browser.get(target)).status_code == 303
    assert (await rig.browser.get(target)).status_code == 403
    state = (await rig.browser.get("/api/state")).json()
    assert state["connections"] == []
    assert "cancelled" in state["notice"]


@pytest.mark.parametrize("decision", ["allow", "deny"])
async def test_embedded_browser_fetch_consent(desk_rig, decision):
    rig = desk_rig
    target = await staged(rig, "slack", decision=decision, fetch=True)
    assert (await rig.browser.get(target)).status_code == 303
    assert len((await rig.browser.get("/api/state")).json()["connections"]) == (
        1 if decision == "allow" else 0
    )


async def test_external_session_revocation_never_silently_retries(desk_rig):
    rig = desk_rig
    connection = await connected(rig)
    first = (await run_tool(rig, connection)).json()
    owner = next(iter(rig.desk.logins.values()))
    # Revoke at RAVN without clearing the developer app's cached runtime.
    await rig.desk.ravn(owner, "POST", f"/v1/sessions/{first['session_id']}/revoke")
    blocked = await run_tool(rig, connection)
    assert blocked.status_code >= 400
    assert len(rig.providers["github"].calls) == 1
    next_explicit_run = await run_tool(rig, connection)
    assert next_explicit_run.status_code == 200, next_explicit_run.text
    assert next_explicit_run.json()["session_id"] != first["session_id"]


async def test_callback_bound_to_original_login(desk_rig):
    rig = desk_rig
    target = await staged(rig)
    async with httpx.AsyncClient(base_url=APP, transport=rig.transport) as another:
        await login(rig, another)
        assert (await another.get(target)).status_code == 403
    tampered = target.replace("app_state=", "app_state=wrong")
    assert (await rig.browser.get(tampered)).status_code == 403
    assert (await rig.browser.get(target)).status_code == 303
    assert (await rig.browser.get(target)).status_code == 403


async def test_browser_cannot_claim_another_actor_or_supply_authority(desk_rig):
    rig = desk_rig
    for headers in (
        {"Authorization": "Bearer " + rig.key},
        {"X-Ravn-User-Id": "victim"},
        {"Host": "evil.example"},
    ):
        response = await rig.browser.get("/api/state", headers=headers)
        assert response.status_code == 403
    body = {"integration_id": "github", "user_id": "victim"}
    response = await rig.browser.post("/api/connect", headers=rig.headers, json=body)
    assert response.status_code == 400


async def test_csrf_and_duplicate_json_are_rejected(desk_rig):
    rig = desk_rig
    for headers in (
        {},
        {"Origin": "http://evil.example"},
        {"Origin": APP},
        {"Origin": APP, "X-CSRF-Token": "wrong"},
    ):
        response = await rig.browser.post(
            "/api/connect", headers=headers, json={"integration_id": "github"}
        )
        assert response.status_code == 403
    headers = {**rig.headers, "Content-Type": "application/json"}
    assert (
        await rig.browser.post(
            "/api/connect",
            headers=headers,
            content='{"integration_id":"github","integration_id":"slack"}',
        )
    ).status_code == 400
    assert (
        await rig.browser.post(
            "/api/connect", headers=headers, content='{"padding":"' + "a" * 17000 + '"}'
        )
    ).status_code == 413


async def test_one_use_local_login_and_secret_free_static_files(desk_rig):
    rig = desk_rig
    link = rig.desk.ticket()
    body = {"ticket": urlsplit(link).fragment.removeprefix("login=")}
    assert (
        await rig.browser.post("/api/login", headers={"Origin": APP}, json=body)
    ).status_code == 200
    assert (
        await rig.browser.post("/api/login", headers={"Origin": APP}, json=body)
    ).status_code == 401
    for path in ("/", "/assets/app.js", "/assets/app.css"):
        response = await rig.browser.get(path)
        assert response.status_code == 200
        assert rig.key not in response.text
        assert response.headers["cache-control"] == "no-store"
    script = (await rig.browser.get("/assets/app.js")).text
    assert "innerHTML" not in script and "localStorage" not in script and "Bearer " not in script


async def test_write_tool_and_cross_user_connection_are_blocked(desk_rig):
    rig = desk_rig
    conn = await connected(rig)
    response = await run_tool(rig, conn, "add_issue_comment", {"body": "forbidden"})
    assert response.status_code == 403
    assert not rig.providers["github"].calls
    async with httpx.AsyncClient(base_url=APP, transport=rig.transport) as another:
        headers = await login(rig, another)
        # Trusted auth-layer assignment, not a browser-selectable user ID.
        cookie = another.cookies.get("support_desk_login")
        rig.desk.logins[hashed(cookie)].actor = ("support-desk", "default", "another-user")
        state = (await another.get("/api/state")).json()
        assert state["connections"] == []
        denied = await another.post(
            f"/api/connections/{conn['id']}/disconnect", headers=headers, json={}
        )
        assert denied.status_code == 404
    assert (await run_tool(rig, conn)).status_code == 200


async def test_reconnect_replaces_only_that_connections_runtime(desk_rig):
    rig = desk_rig
    gh, slack = await connected(rig), await connected(rig, "slack")
    before_gh = (await run_tool(rig, gh)).json()
    before_slack = (await run_tool(rig, slack)).json()
    replacement = await connected(rig, reconnect=gh["id"])
    assert replacement["id"] == gh["id"]
    assert replacement["epoch"] > gh["epoch"]
    assert (await run_tool(rig, gh)).json()["session_id"] != before_gh["session_id"]
    assert (await run_tool(rig, slack)).json()["session_id"] == before_slack["session_id"]


async def test_lost_completion_response_is_recovered_read_only(desk_rig):
    rig = desk_rig
    target = await staged(rig)
    original = rig.desk.ravn
    completes = 0

    async def lost(login, method, path, body=None):
        nonlocal completes
        result = await original(login, method, path, body)
        if path.endswith("/complete"):
            completes += 1
            raise AppError("dependency_unavailable", 503)
        return result

    rig.desk.ravn = lost
    assert (await rig.browser.get(target)).status_code == 303
    assert completes == 1
    assert len((await rig.browser.get("/api/state")).json()["connections"]) == 1


async def test_gmail_draft_is_saved_once_per_key_and_never_sent(desk_rig):
    rig = desk_rig
    gmail = await connected(rig, "gmail")
    assert gmail["display_name"] == "maya@acme-demo.example"
    thread = await run_tool(rig, gmail, "get_thread", {"threadId": "18f2a0c4d5e6f701"})
    assert thread.status_code == 200 and "never got a confirmation" in thread.json()["text"]
    # The app requires a key for writes, so a browser retry cannot duplicate one.
    assert (await run_tool(rig, gmail, "create_draft", DRAFT)).status_code == 400
    first = await run_tool(rig, gmail, "create_draft", DRAFT, key="draft-dana-0001")
    assert first.status_code == 200, first.text
    assert first.json()["effect"] == "write"
    assert "nothing was sent" in first.json()["steps"][-1]
    again = await run_tool(rig, gmail, "create_draft", DRAFT, key="draft-dana-0001")
    assert again.status_code == 200, again.text
    assert again.json()["call_id"] == first.json()["call_id"]
    assert "nothing new was saved" in again.json()["steps"][-1]
    assert len(rig.providers["gmail"].drafts) == 1
    changed = await run_tool(
        rig, gmail, "create_draft", {**DRAFT, "body": "Different."}, key="draft-dana-0001"
    )
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "idempotency_conflict"
    listed = await run_tool(rig, gmail, "list_drafts", {"pageSize": 5})
    assert listed.json()["text"].count("r-simulated-") == 1
    # Google's label writes exist upstream but are neither in the app nor reviewed in RAVN.
    blocked = await run_tool(rig, gmail, "label_thread", {"threadId": "18f2a0c4d5e6f701"})
    assert blocked.status_code == 403
    assert "label_thread" not in rig.providers["gmail"].calls
    assert "create_draft" in rig.providers["gmail"].calls
    state = await rig.browser.get("/api/state")
    for secret in [*rig.providers["gmail"].tokens, *rig.providers["gmail"].refreshes]:
        assert secret not in state.text + first.text + listed.text


async def test_gmail_lost_draft_reply_stays_unknown_and_is_not_resaved(desk_rig):
    rig = desk_rig
    gmail = await connected(rig, "gmail")
    rig.providers["gmail"].lose_response_after_draft = True
    lost = await run_tool(rig, gmail, "create_draft", DRAFT, key="draft-dana-0002")
    assert lost.status_code == 502 and lost.json()["error"]["code"] == "outcome_unknown"
    assert "Check Gmail Drafts" in lost.json()["error"]["message"]
    rig.providers["gmail"].lose_response_after_draft = False
    retry = await run_tool(rig, gmail, "create_draft", DRAFT, key="draft-dana-0002")
    assert retry.status_code == 502 and retry.json()["error"]["code"] == "outcome_unknown"
    assert len(rig.providers["gmail"].drafts) == 1
    activity = (await rig.browser.get("/api/state")).json()["activity"]
    assert activity[0]["status"] == "failed" and activity[0]["call_id"].startswith("call_")


async def test_invalid_arguments_and_simulated_not_found(desk_rig):
    rig = desk_rig
    gh = await connected(rig)
    bad = await run_tool(
        rig,
        gh,
        arguments={"owner": "acme", "repo": "help-center", "issue_number": -1, "method": "get"},
    )
    assert bad.status_code >= 400
    assert rig.providers["github"].calls == []
    response = await run_tool(
        rig, gh, arguments={"owner": "another", "repo": "repo", "issue_number": 42, "method": "get"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "tool_error"
    assert "No real GitHub request" in response.json()["text"]


async def test_resumed_demo_keeps_connection_and_call_history(tmp_path):
    async with simulated_ravn(tmp_path, APP, RAVN, CONSENT) as (broker, consent, key, providers):
        desk = Desk(
            origin=APP,
            ravn_url=RAVN,
            app_key=key,
            demo=True,
            transport=httpx.ASGITransport(app=broker),
            mcp_transport=httpx2.ASGITransport(app=broker),
        )
        transport = BrowserTransport({18880: create_app(desk), 18881: broker, 18883: consent})
        async with httpx.AsyncClient(base_url=APP, transport=transport) as browser:
            rig = SimpleNamespace(desk=desk, browser=browser)
            rig.headers = await login(rig)
            conn = await connected(rig, "slack")
            first = (await run_tool(rig, conn)).json()
    async with simulated_ravn(tmp_path, APP, RAVN, CONSENT, resume=True) as (
        broker,
        _,
        key,
        providers,
    ):
        desk = Desk(
            origin=APP,
            ravn_url=RAVN,
            app_key=key,
            demo=True,
            transport=httpx.ASGITransport(app=broker),
            mcp_transport=httpx2.ASGITransport(app=broker),
        )
        owner = Login(("support-desk", "default", "you"))
        saved = await desk.ravn(owner, "GET", f"/v1/connections/{conn['id']}")
        assert saved["id"] == conn["id"] and saved["status"] == "active"
        history = await desk.ravn(owner, "GET", f"/v1/calls/{first['call_id']}")
        assert history["status"] == "succeeded"
        result = await desk.execute(
            owner,
            RunBody(
                connection_id=conn["id"], tool="slack_search_public", arguments={"query": "refund"}
            ),
        )
        assert result["status"] == "succeeded"
        assert providers["slack"].calls == ["slack_search_public"]


async def test_demo_console_shares_store_and_preserves_other_console_cookie(desk_rig):
    rig = desk_rig
    conn = await connected(rig)
    await run_tool(rig, conn)
    console = Console(rig.broker.state.service, 18882, cookie_name="ravn_support_operator_18882")
    async with httpx.AsyncClient(
        base_url=console.origin, transport=httpx.ASGITransport(app=create_console_app(console))
    ) as http:
        http.cookies.set(COOKIE, "other-console-login", domain="127.0.0.1", path="/console")
        ticket = console.ticket()["url"].split("#ticket=")[1]
        response = await http.post(
            PREFIX + "/auth/exchange", headers={"Origin": console.origin}, json={"ticket": ticket}
        )
        assert response.status_code == 200
        assert http.cookies.get(COOKIE) == "other-console-login"
        assert http.cookies.get(console.cookie_name)
        connections = await http.get(PREFIX + "/connections", params={"app_id": "support-desk"})
        assert connections.status_code == 200, connections.text
        assert connections.json()["data"][0]["id"] == conn["id"]
        calls = await http.get(PREFIX + "/calls", params={"app_id": "support-desk"})
        assert calls.status_code == 200 and len(calls.json()["data"]) == 1
        logout = await http.post(
            PREFIX + "/auth/logout",
            headers={"Origin": console.origin, "X-CSRF-Token": response.json()["csrf_token"]},
            json={},
        )
        assert logout.status_code == 200
        assert http.cookies.get(COOKIE) == "other-console-login"
        assert not http.cookies.get(console.cookie_name)
    console.close()
