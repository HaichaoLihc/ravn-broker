"""Per-session tool rules: they narrow a live session and never grant."""

import pytest
from conftest import ALICE
from mcp.shared.exceptions import MCPError

from ravn.common import RavnError
from ravn.console import PREFIX
from ravn.store import one, rows

pytestmark = pytest.mark.anyio

ISSUE = {"owner": "acme", "repo": "demo", "method": "get", "issue_number": 1}


async def deny(http, session, tools, *, app="demo", tenant="default", expect=200):
    response = await http.put(
        PREFIX + f"/sessions/{session['id']}/permissions",
        json={"app_id": app, "tenant_id": tenant, "tools": tools},
    )
    assert response.status_code == expect, response.text
    return response


async def test_denied_tool_disappears_from_the_catalogue(rig, console):
    _, http = console
    session = await rig.session(await rig.connection())
    async with rig.mcp(session) as mcp:
        assert {t.name for t in (await mcp.list_tools()).tools} == {"issue_read", "list_issues"}
    await deny(http, session, {"list_issues": False})
    async with rig.mcp(session) as mcp:
        assert {t.name for t in (await mcp.list_tools()).tools} == {"issue_read"}


async def test_denied_tool_never_reaches_the_provider(rig, console):
    _, http = console
    session = await rig.session(await rig.connection())
    await deny(http, session, {"issue_read": False})
    rig.fake.calls.clear()
    async with rig.mcp(session) as mcp:
        with pytest.raises(MCPError) as error:
            await mcp.call_tool("issue_read", ISSUE)
    assert error.value.data["code"] == "permission_denied"
    assert rig.fake.calls == []


async def test_change_applies_to_an_already_issued_session(rig, console):
    """The operator revokes a tool while the agent is mid-run, without reissuing."""
    _, http = console
    session = await rig.session(await rig.connection())
    async with rig.mcp(session) as mcp:
        assert (await mcp.call_tool("issue_read", ISSUE)).meta["ravn/call-id"]
        await deny(http, session, {"issue_read": False})
        with pytest.raises(MCPError) as error:
            await mcp.call_tool("issue_read", ISSUE)
    assert error.value.data["code"] == "permission_denied"


async def test_restoring_a_tool_cannot_exceed_the_ceiling(rig, console):
    _, http = console
    session = await rig.session(await rig.connection())
    await deny(http, session, {"issue_read": False})
    await deny(http, session, {"issue_read": True})
    async with rig.mcp(session) as mcp:
        assert (await mcp.call_tool("issue_read", ISSUE)).meta["ravn/call-id"]
    # A name the session was never issued stays refused, however it is spelled.
    await deny(http, session, {"add_issue_comment": True}, expect=422)
    async with rig.mcp(session) as mcp:
        assert {t.name for t in (await mcp.list_tools()).tools} == {"issue_read", "list_issues"}


async def test_denying_every_tool_blocks_the_session(rig, console):
    _, http = console
    session = await rig.session(await rig.connection())
    await deny(http, session, {"issue_read": False, "list_issues": False})
    row = (
        await http.get(PREFIX + f"/sessions/{session['id']}?app_id=demo&tenant_id=default")
    ).json()
    assert row["blocked_reason"] == "all_tools_denied" and row["broker_access"] == "blocked"
    assert row["current_tools"] == []
    assert [p["name"] for p in row["permissions"]] == ["issue_read", "list_issues"]
    assert all(p["allowed"] is False for p in row["permissions"])


async def test_projection_reports_each_decision_and_effect(rig, console):
    _, http = console
    session = await rig.session(await rig.connection())
    await deny(http, session, {"list_issues": False})
    row = (
        await http.get(PREFIX + f"/sessions/{session['id']}?app_id=demo&tenant_id=default")
    ).json()
    assert row["current_tools"] == ["issue_read"]
    assert row["permissions"] == [
        {"name": "issue_read", "allowed": True, "effect": "read"},
        {"name": "list_issues", "allowed": False, "effect": "read"},
    ]


async def denials(rig):
    async with rig.service.store.transaction() as db:
        return await rows(db, "SELECT * FROM events WHERE kind='call.denied' ORDER BY created_at")


async def test_a_refused_call_is_recorded(rig, console):
    """The refusal rolls its own transaction back, so it needs its own event."""
    _, http = console
    session = await rig.session(await rig.connection())
    await deny(http, session, {"issue_read": False})
    async with rig.mcp(session) as mcp:
        with pytest.raises(MCPError):
            await mcp.call_tool("issue_read", ISSUE)
    found = await denials(rig)
    assert len(found) == 1, "a denied call left no trace in the activity log"
    assert found[0]["actor_kind"] == "runtime_session"
    assert found[0]["actor_id"] == session["id"]
    assert found[0]["subject_id"].endswith(":issue_read")
    assert found[0]["request_id"].startswith("req_")


async def test_a_refusal_outside_the_ceiling_is_also_recorded(rig, console):
    session = await rig.session(await rig.connection())
    async with rig.mcp(session) as mcp:
        with pytest.raises(MCPError):
            await mcp.call_tool("add_issue_comment", {"body": "hi"})
    assert len(await denials(rig)) == 1


async def test_a_successful_call_records_no_denial(rig, console):
    session = await rig.session(await rig.connection())
    async with rig.mcp(session) as mcp:
        await mcp.call_tool("issue_read", ISSUE)
    assert await denials(rig) == []


async def test_every_refusal_is_recorded_not_just_the_first(rig, console):
    _, http = console
    session = await rig.session(await rig.connection())
    await deny(http, session, {"issue_read": False})
    async with rig.mcp(session) as mcp:
        for _ in range(3):
            with pytest.raises(MCPError):
                await mcp.call_tool("issue_read", ISSUE)
    assert len(await denials(rig)) == 3


async def test_recording_a_denial_never_masks_the_refusal(rig, console, monkeypatch):
    """An audit-store failure must not turn a refusal into something else."""
    _, http = console
    session = await rig.session(await rig.connection())
    await deny(http, session, {"issue_read": False})

    async def fail(*args, **kwargs):
        raise RuntimeError("Synthetic audit failure")

    monkeypatch.setattr("ravn.service.event", fail)
    async with rig.mcp(session) as mcp:
        with pytest.raises(MCPError) as error:
            await mcp.call_tool("issue_read", ISSUE)
    assert error.value.data["code"] == "permission_denied"


async def test_change_is_audited_as_an_operator_action(rig, console):
    _, http = console
    session = await rig.session(await rig.connection())
    await deny(http, session, {"list_issues": False})
    async with rig.service.store.transaction() as db:
        row = await one(
            db,
            "SELECT * FROM events WHERE kind='session.permissions_updated' AND subject_id=?",
            (session["id"],),
        )
    assert row["actor_kind"] == "local_operator" and row["actor_id"].startswith("op_")
    assert row["request_id"].startswith("req_") and row["subject_type"] == "session"
    assert row["app_id"] == "demo" and row["user_id"] == "alice"


async def test_audit_failure_rolls_back_the_change(rig, console, monkeypatch):
    _, http = console
    session = await rig.session(await rig.connection())

    async def fail(*args, **kwargs):
        raise RuntimeError("Synthetic audit failure")

    monkeypatch.setattr("ravn.control.event", fail)
    await deny(http, session, {"issue_read": False}, expect=503)
    async with rig.service.store.transaction() as db:
        assert await rows(db, "SELECT * FROM session_tool_rules") == []
    async with rig.mcp(session) as mcp:
        assert (await mcp.call_tool("issue_read", ISSUE)).meta["ravn/call-id"]


@pytest.mark.parametrize(
    "headers", [{"X-CSRF-Token": "wrong"}, {"Origin": "http://evil.example"}, {"X-CSRF-Token": ""}]
)
async def test_mutation_requires_csrf_and_origin(rig, console, headers):
    _, http = console
    session = await rig.session(await rig.connection())
    response = await http.put(
        PREFIX + f"/sessions/{session['id']}/permissions",
        headers=headers,
        json={"app_id": "demo", "tenant_id": "default", "tools": {"issue_read": False}},
    )
    assert response.status_code in {400, 403}, response.text
    async with rig.mcp(session) as mcp:
        assert (await mcp.call_tool("issue_read", ISSUE)).meta["ravn/call-id"]


async def test_cannot_edit_another_namespace(rig, console):
    _, http = console
    conn = await rig.connection(tenant="acme", key=rig.saas_key)
    session = await rig.session(conn, tenant="acme", key=rig.saas_key)
    for app, tenant in [("demo", "acme"), ("saas", "other"), ("demo", "default")]:
        await deny(http, session, {"issue_read": False}, app=app, tenant=tenant, expect=404)
    async with rig.mcp(session) as mcp:
        assert (await mcp.call_tool("issue_read", ISSUE)).meta["ravn/call-id"]


async def test_runtime_session_cannot_change_its_own_permissions(rig):
    """The agent holds only a session token; the console port is not reachable with it."""
    session = await rig.session(await rig.connection())
    response = await rig.http.put(
        PREFIX + f"/sessions/{session['id']}/permissions",
        headers={"Authorization": "Bearer " + session["token"]},
        json={"app_id": "demo", "tenant_id": "default", "tools": {"issue_read": True}},
    )
    assert response.status_code == 404


async def test_rules_are_per_session_not_per_connection(rig, console):
    _, http = console
    conn = await rig.connection()
    first, second = await rig.session(conn), await rig.session(conn)
    await deny(http, first, {"issue_read": False})
    async with rig.mcp(first) as mcp:
        with pytest.raises(MCPError):
            await mcp.call_tool("issue_read", ISSUE)
    async with rig.mcp(second) as mcp:
        assert (await mcp.call_tool("issue_read", ISSUE)).meta["ravn/call-id"]


async def test_unreadable_rules_fail_closed(rig, monkeypatch):
    session = await rig.session(await rig.connection())

    async def fail(*args, **kwargs):
        raise RuntimeError("Synthetic rule-store failure")

    monkeypatch.setattr("ravn.service.session_rules", fail)
    rig.fake.calls.clear()
    async with rig.mcp(session) as mcp:
        with pytest.raises(MCPError) as error:
            await mcp.call_tool("issue_read", ISSUE)
    assert error.value.data["code"] == "permission_denied"
    assert rig.fake.calls == []


async def test_a_new_connection_for_the_same_user_is_unaffected(rig, console):
    _, http = console
    await deny(http, await rig.session(await rig.connection()), {"issue_read": False})
    later = await rig.session(await rig.connection(credential=ALICE))
    async with rig.mcp(later) as mcp:
        assert (await mcp.call_tool("issue_read", ISSUE)).meta["ravn/call-id"]


async def test_denied_tool_is_rejected_before_argument_validation(rig, console):
    """A denial must not become an argument error that hints the tool exists."""
    _, http = console
    session = await rig.session(await rig.connection())
    await deny(http, session, {"issue_read": False})
    async with rig.mcp(session) as mcp:
        with pytest.raises(MCPError) as error:
            await mcp.call_tool("issue_read", {"owner": "..", "repo": "demo"})
    assert error.value.data["code"] == "permission_denied"


async def test_empty_and_oversized_bodies_are_refused(rig, console):
    _, http = console
    session = await rig.session(await rig.connection())
    path = PREFIX + f"/sessions/{session['id']}/permissions"
    for tools in [{}, {f"tool_{n}": False for n in range(65)}]:
        response = await http.put(
            path, json={"app_id": "demo", "tenant_id": "default", "tools": tools}
        )
        assert response.status_code in {400, 422}, response.text
    async with rig.service.store.transaction() as db:
        assert await rows(db, "SELECT * FROM session_tool_rules") == []


async def test_revoked_session_permissions_cannot_be_edited_back_into_use(rig, console):
    _, http = console
    session = await rig.session(await rig.connection())
    assert (
        await http.post(
            PREFIX + f"/sessions/{session['id']}/revoke",
            json={"app_id": "demo", "tenant_id": "default"},
        )
    ).status_code == 200
    await deny(http, session, {"issue_read": True})
    # The token is refused at authentication, before any tool rule is consulted.
    with pytest.raises(RavnError) as error:
        await rig.service.authenticate_session(session["token"])
    assert error.value.code == "unauthenticated"
    row = (
        await http.get(PREFIX + f"/sessions/{session['id']}?app_id=demo&tenant_id=default")
    ).json()
    assert row["broker_access"] == "blocked" and row["current_tools"] == []


@pytest.mark.parametrize(
    "arguments,valid",
    [
        ({"channel_id": "C0123ABC"}, True),
        ({"channel_id": "C0123ABC", "limit": 50}, True),
        # Slack's own schema lets channel_id be a user ID, which reads a DM.
        ({"channel_id": "U0123ABC"}, False),
        ({"channel_id": "D0123ABC"}, False),
        ({"channel_id": "G0123ABC"}, False),
        ({"channel_id": "C0123ABC", "limit": 500}, False),
        ({"channel_id": "C0123ABC", "latest": "1789873723.5"}, False),
        ({}, False),
    ],
)
def test_read_channel_never_accepts_a_direct_message(arguments, valid):
    from ravn.common import RavnError
    from ravn.manifest import validate_arguments

    if valid:
        validate_arguments("slack_read_channel", arguments, "slack")
        return
    with pytest.raises(RavnError) as error:
        validate_arguments("slack_read_channel", arguments, "slack")
    assert error.value.code == "invalid_arguments"
