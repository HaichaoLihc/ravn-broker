"""Reviewed writes: optional idempotency keys, no automatic retry, ambiguous outcomes stay unknown."""

import asyncio
import base64
import contextlib
import json
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import pytest
from conftest import Rig
from mcp import types
from mcp.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from ravn.app import create_admin_app, create_app
from ravn.common import Principal, RavnError
from ravn.config import ApprovalsConfig, Config
from ravn.console import PREFIX, Console, create_console_app
from ravn.github import GitHub
from ravn.gmail import Gmail
from ravn.manifest import GMAIL_SCHEMAS, schema_hash
from ravn.store import rows

pytestmark = pytest.mark.anyio
TOKEN = "ya29.alice-gmail-credential"
DRAFT = {"to": ["dana@example.com"], "subject": "Re: Refund #1042", "body": "It is processing."}


class FakeGmail:
    def __init__(self):
        self.calls, self.metas = [], []
        self.listing = 0
        self.both_listing = asyncio.Event()
        self.block_list = False
        self.release = asyncio.Event()
        self.lose_response = False
        self.deny_next = False
        self.tools = [types.Tool(name=n, inputSchema=s) for n, s in GMAIL_SCHEMAS.items()]
        # An upstream write RAVN has not reviewed must never become reachable.
        self.tools.append(
            types.Tool(
                name="label_thread",
                inputSchema={"type": "object"},
                annotations=types.ToolAnnotations(readOnlyHint=True),
            )
        )
        self.server = Server("fake-gmail", on_list_tools=self.list_tools, on_call_tool=self.call)
        self.app = self.server.streamable_http_app(
            streamable_http_path="/mcp/v1",
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

    async def list_tools(self, ctx, params):
        self.listing += 1
        if self.listing == 2:
            self.both_listing.set()
        if self.block_list:
            await self.release.wait()
        return types.ListToolsResult(tools=self.tools)

    async def call(self, ctx, params):
        self.calls.append((params.name, params.arguments))
        self.metas.append(params.meta)
        draft = {"id": f"r-{len(self.calls)}", **(params.arguments or {})}
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(draft))]
        )

    def transport(self, host):
        if host == "gmailmcp.googleapis.com":
            return Faulty(self)
        return httpx2.MockTransport(
            lambda request: httpx2.Response(
                200, json={"sub": "109876", "email": "alice@example.com", "email_verified": True}
            )
        )


class Faulty(httpx2.AsyncBaseTransport):
    """Refuses before the server sees a call, or drops the reply after it ran."""

    def __init__(self, fake):
        self.fake, self.inner = fake, httpx2.ASGITransport(app=fake.app)

    async def handle_async_request(self, request):
        calling = b"tools/call" in await request.aread()
        if calling and self.fake.deny_next:
            self.fake.deny_next = False
            return httpx2.Response(403, json={"error": {"status": "PERMISSION_DENIED"}})
        response = await self.inner.handle_async_request(request)
        if calling and self.fake.lose_response:
            await response.aread()
            raise httpx2.ReadError("connection dropped after dispatch", request=request)
        return response


@pytest.fixture
async def gmail(tmp_path):
    key = tmp_path / "master.key"
    key.write_bytes(base64.b64encode(b"y" * 32))
    key.chmod(0o600)
    fake = FakeGmail()
    config = Config.model_validate(
        {
            "deployment_id": "write-tests",
            "server": {"admin_socket": str(tmp_path / "run/admin.sock")},
            "storage": {"sqlite_path": str(tmp_path / "state/ravn.db")},
            "secrets": {"managed": {"active_key_id": "test-key", "key_source": {"path": str(key)}}},
            "applications": [{"id": "demo"}],
            "integrations": [
                {
                    "id": "gmail",
                    "app_id": "demo",
                    "connector": "gmail",
                    "endpoint": "https://gmailmcp.googleapis.com/mcp/v1",
                    "manifest": "builtin:gmail-v1",
                    "schema_hashes": {
                        t.name: schema_hash(t) for t in fake.tools if t.name in GMAIL_SCHEMAS
                    },
                }
            ],
        }
    )
    app = create_app(
        config,
        provider={"github_cloud": GitHub(), "gmail": Gmail(transport_factory=fake.transport)},
    )
    async with fake.server.session_manager.run(), app.router.lifespan_context(app):
        service = app.state.service
        app_key = (await service.create_key("demo", "test"))["key"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787"
        ) as http:
            rig = Rig(app, http, fake, app_key, None)
            response = await http.post(
                "/v1/connections/import",
                headers=rig.headers(),
                json={"integration_id": "gmail", "credential": {"type": "bearer", "token": TOKEN}},
            )
            assert response.status_code == 201, response.text
            rig.conn = response.json()
            rig.runtime = await rig.session(rig.conn)
            # Stands in for the operator, so write tests that are not about the
            # gate see it approve; gate tests switch it off and decide themselves.
            rig.auto_approve = True

            async def approver():
                version = None
                while True:
                    result = await service.approvals.pending(1, version)
                    version = result["version"]
                    for item in result["data"] if rig.auto_approve else []:
                        with contextlib.suppress(RavnError):
                            await service.approvals.decide(item["id"], "approve")

            task = asyncio.create_task(approver())
            try:
                yield rig
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


async def draft(mcp, key=None, arguments=DRAFT, name="create_draft"):
    meta = {"ravn/idempotency-key": key} if key else None
    return await mcp.session.call_tool(name, arguments, meta=meta)


async def refused(mcp, **kwargs):
    with pytest.raises(MCPError) as error:
        await draft(mcp, **kwargs)
    return error.value.data


async def call_status(rig, call_id):
    response = await rig.http.get(f"/v1/calls/{call_id}", headers=rig.headers())
    assert response.status_code == 200, response.text
    return response.json()


async def test_catalogue_marks_the_draft_write_and_hides_unreviewed_writes(gmail):
    async with gmail.mcp(gmail.runtime) as mcp:
        tools = {t.name: t for t in (await mcp.list_tools()).tools}
    assert set(tools) == set(GMAIL_SCHEMAS)
    assert tools["create_draft"].annotations.read_only_hint is False
    assert "never sent" in tools["create_draft"].description
    assert tools["search_threads"].annotations.read_only_hint is True


async def test_keyless_submissions_each_dispatch(gmail):
    async with gmail.mcp(gmail.runtime) as mcp:
        first, second = await draft(mcp), await draft(mcp)
    assert len(gmail.fake.calls) == 2
    assert first.meta["ravn/call-id"] != second.meta["ravn/call-id"]
    assert (await call_status(gmail, first.meta["ravn/call-id"]))["effect"] == "write"


async def test_same_key_dispatches_once_and_replays_without_result(gmail):
    async with gmail.mcp(gmail.runtime) as mcp:
        first = await draft(mcp, key="draft-1042-a")
        again = await draft(mcp, key="draft-1042-a")
    assert gmail.fake.calls == [("create_draft", DRAFT)]
    # The RAVN extension is consumed locally, never forwarded upstream.
    assert not any(meta and "ravn/idempotency-key" in meta for meta in gmail.fake.metas)
    assert again.meta["ravn/idempotency-replayed"] is True
    assert again.meta["ravn/call-id"] == first.meta["ravn/call-id"]
    assert "not retained" in again.content[0].text and "dana@example.com" not in str(again)
    status = await call_status(gmail, first.meta["ravn/call-id"])
    assert status["status"] == "succeeded" and "idempotency" not in json.dumps(status)


async def test_same_key_with_different_content_conflicts(gmail):
    async with gmail.mcp(gmail.runtime) as mcp:
        await draft(mcp, key="draft-1042-b")
        data = await refused(mcp, key="draft-1042-b", arguments={**DRAFT, "body": "Changed."})
    assert data["code"] == "idempotency_conflict"
    assert len(gmail.fake.calls) == 1


async def test_concurrent_duplicates_dispatch_once(gmail):
    gmail.fake.block_list = True

    async def attempt():
        async with gmail.mcp(gmail.runtime) as mcp:
            try:
                return await draft(mcp, key="draft-1042-c")
            except MCPError as error:
                return error.data["code"]

    tasks = [asyncio.create_task(attempt()) for _ in range(2)]
    # Both requests pass the early key check before either reaches admission.
    async with asyncio.timeout(5):
        await gmail.fake.both_listing.wait()
    gmail.fake.release.set()
    outcomes = await asyncio.gather(*tasks)
    assert len(gmail.fake.calls) == 1
    # The loser either sees the call still running or replays its known outcome.
    losers = [
        o for o in outcomes if o == "call_in_progress" or o.meta.get("ravn/idempotency-replayed")
    ]
    assert len(losers) == 1, outcomes


async def test_lost_reply_is_unknown_and_never_redispatched(gmail):
    gmail.fake.lose_response = True
    async with gmail.mcp(gmail.runtime) as mcp:
        data = await refused(mcp, key="draft-1042-d")
        assert data["code"] == "outcome_unknown" and data["retryable"] is False
        call_id = data["details"]["call_id"]
        gmail.fake.lose_response = False
        replay = await refused(mcp, key="draft-1042-d")
        assert replay["code"] == "outcome_unknown" and replay["details"]["call_id"] == call_id
        assert len(gmail.fake.calls) == 1
        # Without a key, a resubmission is a new action and can execute again.
        await draft(mcp)
    assert len(gmail.fake.calls) == 2
    status = await call_status(gmail, call_id)
    assert (status["status"], status["error_code"]) == ("unknown", "outcome_unknown")


async def test_definite_provider_refusal_fails_and_releases_the_key(gmail):
    gmail.fake.deny_next = True
    async with gmail.mcp(gmail.runtime) as mcp:
        data = await refused(mcp, key="draft-1042-e")
        assert data["code"] == "provider_denied"
        assert (await call_status(gmail, data["details"]["call_id"]))["status"] == "failed"
        result = await draft(mcp, key="draft-1042-e")
    assert not result.is_error and len(gmail.fake.calls) == 1
    connections = await gmail.http.get("/v1/connections", headers=gmail.headers())
    assert connections.json()["data"][0]["status"] == "active"


@pytest.mark.parametrize(
    "name, arguments, code",
    [
        (
            "label_thread",
            {"threadId": "18f2a0c4d5e6f701", "labelIds": ["TRASH"]},
            "permission_denied",
        ),
        (
            "create_draft",
            {**DRAFT, "htmlBody": "<img src=https://t.example/p>"},
            "invalid_arguments",
        ),
        ("create_draft", {**DRAFT, "attachments": [{"content": "AAAA"}]}, "invalid_arguments"),
        ("create_draft", {**DRAFT, "to": ["Dana <dana@example.com>"]}, "invalid_arguments"),
        ("create_draft", {"to": ["dana@example.com"]}, "invalid_arguments"),
    ],
)
async def test_unreviewed_writes_and_rich_fields_never_reach_gmail(gmail, name, arguments, code):
    async with gmail.mcp(gmail.runtime) as mcp:
        data = await refused(mcp, name=name, arguments=arguments)
    assert data["code"] == code
    assert gmail.fake.calls == []


@asynccontextmanager
async def operator(service):
    """A console client for this deployment, as the Permissions tab is."""
    state = Console(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_console_app(state)), base_url=state.origin
    ) as http:
        ticket = parse_qs(urlsplit(state.ticket()["url"]).fragment)["ticket"][0]
        response = await http.post(
            PREFIX + "/auth/exchange", headers={"Origin": state.origin}, json={"ticket": ticket}
        )
        http.headers.update({"Origin": state.origin, "X-CSRF-Token": response.json()["csrf_token"]})
        yield http
    state.close()


async def set_draft(http, session_id, allowed):
    response = await http.put(
        PREFIX + f"/sessions/{session_id}/permissions",
        json={"app_id": "demo", "tenant_id": "default", "tools": {"create_draft": allowed}},
    )
    assert response.status_code == 200, response.text
    return {p["name"]: p["allowed"] for p in response.json()["permissions"]}


async def test_a_write_survives_being_denied_and_allowed_again(gmail):
    """Re-allowing a write tool restores it; a denial leaves nothing stuck behind."""
    async with operator(gmail.service) as http:
        sid = gmail.runtime["id"]
        async with gmail.mcp(gmail.runtime) as mcp:
            await draft(mcp)
        assert len(gmail.fake.calls) == 1

        assert (await set_draft(http, sid, False))["create_draft"] is False
        async with gmail.mcp(gmail.runtime) as mcp:
            assert (await refused(mcp))["code"] == "permission_denied"
        assert len(gmail.fake.calls) == 1, "a denied write still reached Gmail"

        assert (await set_draft(http, sid, True))["create_draft"] is True
        async with gmail.mcp(gmail.runtime) as mcp:
            assert "create_draft" in {t.name for t in (await mcp.list_tools()).tools}
            await draft(mcp)
        assert len(gmail.fake.calls) == 2, "the write did not recover after being re-allowed"


async def test_the_cycle_holds_on_one_long_lived_connection(gmail):
    """An agent keeps one MCP session open across an operator's changes."""
    async with operator(gmail.service) as http:
        sid = gmail.runtime["id"]
        async with gmail.mcp(gmail.runtime) as mcp:
            await draft(mcp)
            await set_draft(http, sid, False)
            assert (await refused(mcp))["code"] == "permission_denied"
            await set_draft(http, sid, True)
            await draft(mcp)
    assert len(gmail.fake.calls) == 2


async def test_repeated_toggling_settles_on_the_last_decision(gmail):
    async with operator(gmail.service) as http:
        sid = gmail.runtime["id"]
        for allowed in (False, True, False, True, False):
            await set_draft(http, sid, allowed)
        async with gmail.mcp(gmail.runtime) as mcp:
            assert (await refused(mcp))["code"] == "permission_denied"
        assert (await set_draft(http, sid, True))["create_draft"] is True
        async with gmail.mcp(gmail.runtime) as mcp:
            await draft(mcp)
    assert len(gmail.fake.calls) == 1


async def test_a_denied_write_does_not_consume_its_idempotency_key(gmail):
    """Otherwise a refusal would poison the key and block the retry after re-allowing."""
    async with operator(gmail.service) as http:
        sid = gmail.runtime["id"]
        await set_draft(http, sid, False)
        async with gmail.mcp(gmail.runtime) as mcp:
            assert (await refused(mcp, key="draft-77"))["code"] == "permission_denied"
        await set_draft(http, sid, True)
        async with gmail.mcp(gmail.runtime) as mcp:
            result = await draft(mcp, key="draft-77")
    assert gmail.fake.calls == [("create_draft", DRAFT)]
    assert result.meta.get("ravn/idempotency-replayed") is not True


# Human approval of writes. Each test drives the operator's decision itself.


def start(rig, **kwargs):
    """Call create_draft in the background; the result, or the refusal's data."""

    async def run():
        async with rig.mcp(rig.runtime) as mcp:
            try:
                return await draft(mcp, **kwargs)
            except MCPError as error:
                return error.data

    return asyncio.create_task(run())


async def pending(rig, n=1):
    async with asyncio.timeout(5):
        while True:
            data = (await rig.service.approvals.pending(1))["data"]
            if len(data) >= n:
                return data


async def finished(task):
    async with asyncio.timeout(5):
        return await task


async def approvals(rig):
    async with rig.service.store.transaction() as db:
        return await rows(db, "SELECT * FROM approvals ORDER BY created_at")


async def kinds(rig, subject=None):
    async with rig.service.store.transaction() as db:
        found = await rows(db, "SELECT kind, subject_id FROM events ORDER BY created_at, rowid")
    return [e["kind"] for e in found if subject is None or e["subject_id"] == subject]


def principal(rig):
    return Principal(
        "demo",
        "default",
        "alice",
        session_id=rig.runtime["id"],
        connection_id=rig.conn["id"],
    )


def test_approval_timeout_is_bounded():
    assert ApprovalsConfig().timeout_seconds == 300
    for value in (0, 3601):
        with pytest.raises(ValidationError):
            ApprovalsConfig(timeout_seconds=value)


async def test_an_approved_write_runs_exactly_once(gmail):
    gmail.auto_approve = False
    service = gmail.service
    original, credentials = service.credential, []

    async def spy(*args):
        # Discovery fetches one too; only a fetch during a wait is a leak.
        credentials.append(bool(service.approvals.waiters))
        return await original(*args)

    service.credential = spy
    task = start(gmail)
    [item] = await pending(gmail)
    # Waiting holds no slot, credential or transaction.
    assert not service.counts and True not in credentials and gmail.fake.calls == []
    assert not service.store.lock.locked()
    assert item["tool"] == "create_draft" and json.loads(item["arguments"]) == DRAFT
    [row] = await approvals(gmail)
    assert row["status"] == "pending" and row["arguments_ciphertext"] is not None
    assert (row["app_id"], row["tenant_id"], row["user_id"]) == ("demo", "default", "alice")
    assert (row["session_id"], row["connection_id"]) == (gmail.runtime["id"], gmail.conn["id"])
    assert row["expires_at"] > row["created_at"]

    assert await service.approvals.decide(item["id"], "approve") == {
        "id": item["id"],
        "status": "approved",
    }
    result = await finished(task)
    assert not result.is_error and gmail.fake.calls == [("create_draft", DRAFT)]
    [row] = await approvals(gmail)
    assert row["status"] == "consumed" and row["arguments_ciphertext"] is None
    assert row["call_id"] == result.meta["ravn/call-id"]
    assert await kinds(gmail, item["id"]) == ["approval.requested", "approval.approved"]
    assert (await call_status(gmail, row["call_id"]))["status"] == "succeeded"


async def test_a_denied_write_never_reaches_gmail(gmail):
    gmail.auto_approve = False
    task = start(gmail)
    [item] = await pending(gmail)
    await gmail.service.approvals.decide(item["id"], "deny")
    data = await finished(task)
    assert data["code"] == "approval_denied"
    assert gmail.fake.calls == []
    [row] = await approvals(gmail)
    assert row["status"] == "denied" and row["arguments_ciphertext"] is None
    assert row["decided_at"] and row["call_id"] is None
    assert "call.denied" in await kinds(gmail)
    assert await kinds(gmail, item["id"]) == ["approval.requested", "approval.denied"]


async def test_an_undecided_write_expires(gmail):
    gmail.auto_approve = False
    gmail.service.approvals.timeout = 0.2
    data = await finished(start(gmail))
    assert data["code"] == "approval_expired"
    assert gmail.fake.calls == []
    [row] = await approvals(gmail)
    assert row["status"] == "expired" and row["arguments_ciphertext"] is None
    assert await kinds(gmail, row["id"]) == ["approval.requested", "approval.expired"]
    assert "call.denied" in await kinds(gmail)
    # A decision after the fact changes nothing.
    with pytest.raises(RavnError) as error:
        await gmail.service.approvals.decide(row["id"], "approve")
    assert error.value.status == 409 and error.value.code == "approval_not_pending"
    assert (await approvals(gmail))[0]["status"] == "expired"


async def test_an_approval_is_bound_to_its_arguments_and_used_once(gmail):
    gmail.auto_approve = False
    service, p = gmail.service, principal(gmail)
    task = asyncio.create_task(
        service.approvals.wait(p, gmail.conn["id"], "create_draft", DRAFT, "fp-a")
    )
    [item] = await pending(gmail)
    await service.approvals.decide(item["id"], "approve")
    approval = await finished(task)
    assert approval == item["id"]

    async def consume(fingerprint, who=p):
        async with service.store.transaction() as db:
            await service.approvals.consume(db, who, approval, fingerprint, "call_x")

    for fingerprint, who in (
        ("fp-b", p),
        ("fp-a", Principal("demo", "default", "alice", session_id="sess_other")),
    ):
        with pytest.raises(RavnError) as error:
            await consume(fingerprint, who)
        assert (error.value.status, error.value.code) == (403, "approval_invalid")
    await consume("fp-a")
    with pytest.raises(RavnError) as error:
        await consume("fp-a")
    assert error.value.code == "approval_invalid"
    [row] = await approvals(gmail)
    assert (row["status"], row["call_id"]) == ("consumed", "call_x")


async def test_revoking_the_session_while_pending_stops_the_write(gmail):
    gmail.auto_approve = False
    task = start(gmail)
    [item] = await pending(gmail)
    response = await gmail.http.post(
        f"/v1/sessions/{gmail.runtime['id']}/revoke", headers=gmail.headers()
    )
    assert response.status_code == 200, response.text
    await gmail.service.approvals.decide(item["id"], "approve")
    data = await finished(task)
    assert data["code"] == "unauthenticated"
    assert gmail.fake.calls == []
    [row] = await approvals(gmail)
    assert row["status"] == "expired" and row["call_id"] is None
    assert "approval.expired" in await kinds(gmail, item["id"])


async def test_reads_never_ask_for_approval(rig):
    session = await rig.session(await rig.connection())
    async with rig.mcp(session) as mcp:
        result = await mcp.call_tool(
            "issue_read", {"owner": "acme", "repo": "demo", "method": "get", "issue_number": 1}
        )
    assert not result.is_error
    assert await approvals(rig) == []
    assert not [k for k in await kinds(rig) if k.startswith("approval.")]


async def test_a_restart_expires_undecided_and_unused_approvals(gmail):
    async with gmail.service.store.transaction() as db:
        for approval_id, status in (("apr_a", "pending"), ("apr_b", "approved")):
            await db.execute(
                "INSERT INTO approvals(id,app_id,tenant_id,user_id,connection_id,session_id,tool,"
                "arguments_fingerprint,arguments_ciphertext,status,created_at,expires_at) "
                "VALUES(?,'demo','default','alice',?,?,'create_draft','fp',?,?,"
                "'2026-01-01T00:00:00.000000Z','2026-01-01T00:05:00.000000Z')",
                (approval_id, gmail.conn["id"], gmail.runtime["id"], b"sealed", status),
            )
    await gmail.service.approvals.recover()
    found = await approvals(gmail)
    assert {(r["id"], r["status"]) for r in found} == {("apr_a", "expired"), ("apr_b", "expired")}
    assert all(r["arguments_ciphertext"] is None and r["decided_at"] for r in found)
    async with gmail.service.store.transaction() as db:
        audit = await rows(db, "SELECT * FROM events WHERE kind='approval.expired'")
    assert {(e["subject_id"], e["actor_kind"], e["actor_id"]) for e in audit} == {
        ("apr_a", "system", "restart"),
        ("apr_b", "system", "restart"),
    }


async def test_concurrent_writes_each_wait_for_their_own_decision(gmail):
    gmail.auto_approve = False
    other = {**DRAFT, "body": "Other."}
    first, second = start(gmail), start(gmail, arguments=other)
    items = {json.loads(i["arguments"])["body"]: i["id"] for i in await pending(gmail, 2)}
    await gmail.service.approvals.decide(items["Other."], "deny")
    await gmail.service.approvals.decide(items[DRAFT["body"]], "approve")
    ok, denied = await finished(first), await finished(second)
    assert not ok.is_error and denied["code"] == "approval_denied"
    assert gmail.fake.calls == [("create_draft", DRAFT)]
    statuses = {r["id"]: r["status"] for r in await approvals(gmail)}
    assert statuses == {items[DRAFT["body"]]: "consumed", items["Other."]: "denied"}


async def test_one_session_cannot_crowd_out_the_queue(gmail):
    gmail.auto_approve = False
    waiting = [start(gmail, arguments={**DRAFT, "body": f"Spam {i}."}) for i in range(4)]
    items = await pending(gmail, 4)
    refusal = await finished(start(gmail, arguments={**DRAFT, "body": "One more."}))
    assert refusal["code"] == "rate_limited"
    assert len(await approvals(gmail)) == 4
    for item in items:
        await gmail.service.approvals.decide(item["id"], "deny")
    for task in waiting:
        assert (await finished(task))["code"] == "approval_denied"
    assert gmail.fake.calls == []


async def test_a_cancelled_wait_cancels_its_approval(gmail):
    gmail.auto_approve = False
    service = gmail.service
    task = asyncio.create_task(
        service.approvals.wait(principal(gmail), gmail.conn["id"], "create_draft", DRAFT, "fp")
    )
    [item] = await pending(gmail)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await finished(task)
    [row] = await approvals(gmail)
    assert (row["id"], row["status"]) == (item["id"], "cancelled")
    assert row["arguments_ciphertext"] is None
    assert service.approvals.waiters == {}
    assert await kinds(gmail, item["id"]) == ["approval.requested", "approval.cancelled"]


async def test_a_replayed_write_is_not_asked_about_again(gmail):
    async with gmail.mcp(gmail.runtime) as mcp:
        first = await draft(mcp, key="draft-approve-1")
        again = await draft(mcp, key="draft-approve-1")
    assert again.meta["ravn/idempotency-replayed"] is True
    assert again.meta["ravn/call-id"] == first.meta["ravn/call-id"]
    [row] = await approvals(gmail)
    assert (row["status"], row["call_id"]) == ("consumed", first.meta["ravn/call-id"])
    assert len(gmail.fake.calls) == 1


async def test_a_denied_tool_is_refused_before_any_approval(gmail):
    async with operator(gmail.service) as http:
        await set_draft(http, gmail.runtime["id"], False)
    async with gmail.mcp(gmail.runtime) as mcp:
        assert (await refused(mcp))["code"] == "permission_denied"
    assert await approvals(gmail) == []


async def test_admin_approval_endpoints(gmail):
    gmail.auto_approve = False
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_admin_app(gmail.service)),
        base_url="http://ravn-admin",
    ) as admin:
        missing = await admin.post(
            "/admin/v1/approvals/apr_missing/decision", json={"decision": "approve"}
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_found"
        # Invalid fields are refused without echoing them, as everywhere else.
        assert (await admin.get("/admin/v1/approvals?wait=26")).status_code == 400
        assert (await admin.get("/admin/v1/approvals?after=-1")).status_code == 400

        empty = (await admin.get("/admin/v1/approvals")).json()
        assert empty["data"] == []
        # A long poll returns as soon as a request arrives, not at its deadline.
        poll = asyncio.create_task(
            admin.get(f"/admin/v1/approvals?wait=20&after={empty['version']}")
        )
        await asyncio.sleep(0.05)
        task = start(gmail)
        async with asyncio.timeout(5):
            listed = (await poll).json()
        [item] = listed["data"]
        assert item["tool"] == "create_draft" and json.loads(item["arguments"]) == DRAFT
        assert set(item) == {
            "id",
            "app_id",
            "tenant_id",
            "user_id",
            "session_id",
            "connection_id",
            "tool",
            "created_at",
            "expires_at",
            "arguments",
        }
        maybe = await admin.post(
            f"/admin/v1/approvals/{item['id']}/decision", json={"decision": "maybe"}
        )
        assert maybe.status_code == 400 and "maybe" not in maybe.text
        decided = await admin.post(
            f"/admin/v1/approvals/{item['id']}/decision", json={"decision": "approve"}
        )
        assert decided.json() == {"id": item["id"], "status": "approved"}
        assert not (await finished(task)).is_error
        again = await admin.post(
            f"/admin/v1/approvals/{item['id']}/decision", json={"decision": "deny"}
        )
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "approval_not_pending"
        assert (await approvals(gmail))[0]["status"] == "consumed"
        assert len(gmail.fake.calls) == 1


async def test_the_interactive_loop_decides_and_skips(gmail, monkeypatch, capsys):
    from ravn import cli

    gmail.auto_approve = False
    admin = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_admin_app(gmail.service)),
        base_url="http://ravn-admin",
    )

    async def request(config, path, payload=None, method="POST", seconds=10):
        response = await admin.request(method, path, json=payload if method != "GET" else None)
        if response.status_code in {404, 409}:
            error = response.json()["error"]
            raise RavnError(response.status_code, error["code"], error["message"])
        return response.json()

    answers, asked = iter(["s", "a"]), []

    async def prompt(text):
        asked.append(text)
        return next(answers)

    monkeypatch.setattr(cli, "admin_request", request)
    monkeypatch.setattr(cli, "prompt", prompt)
    first = start(gmail)
    await pending(gmail)
    second = start(gmail, arguments={**DRAFT, "body": "Second."})
    await pending(gmail, 2)
    loop = asyncio.create_task(cli.approvals_command(None, type("Args", (), {"action": None})))
    try:
        # The skipped request is not asked about again; the second is approved.
        assert not (await finished(second)).is_error
        assert len(asked) == 2
    finally:
        loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop
        await admin.aclose()
    out = capsys.readouterr().out
    assert out.count("\a") == 2 and "Approved." in out
    statuses = sorted(r["status"] for r in await approvals(gmail))
    assert statuses == ["consumed", "pending"]
    await gmail.service.approvals.decide((await pending(gmail))[0]["id"], "deny")
    assert (await finished(first))["code"] == "approval_denied"
