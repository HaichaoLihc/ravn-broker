import asyncio
import base64
from unittest.mock import AsyncMock

import httpx2
import pytest
from cryptography.exceptions import InvalidTag

from ravn.common import RavnError
from ravn.crypto import Cipher
from ravn.github import BoundedStream, GitHub, PinnedTransport
from ravn.service import Service
from ravn.store import one

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("login", ["credential-echo", "evil\nname", "x" * 40])
async def test_provider_identity_cannot_echo_secret_into_metadata(login):
    provider = GitHub(
        transport_factory=lambda host: httpx2.MockTransport(
            lambda request: httpx2.Response(200, json={"id": 1, "login": login})
        )
    )
    with pytest.raises(RavnError) as error:
        await provider.identify("credential-echo")
    assert error.value.code == "upstream_unavailable"
    assert "credential-echo" not in str(error.value)


async def test_second_server_refused_and_restart_preserves_data(config):
    first = Service(config)
    await first.start()
    key = await first.create_key("demo", "backend")
    second = Service(config)
    try:
        with pytest.raises(ValueError, match="Another RAVN process"):
            await second.start()
    finally:
        await first.close()
    await second.start()
    try:
        principal = await second.authenticate_app(key["key"], "alice", None)
        assert principal.app == "demo"
    finally:
        await second.close()


async def test_wrong_key_refuses_readiness_and_releases_lock(config):
    first = Service(config)
    await first.start()
    await first.close()
    path = config.secrets.managed.key_source.path
    original = path.read_bytes()
    path.write_bytes(base64.b64encode(b"y" * 32))
    with pytest.raises(InvalidTag):
        await Service(config).start()
    path.write_bytes(original)
    recovered = Service(config)
    await recovered.start()
    await recovered.close()


async def test_ciphertext_namespace_swapping_rejected(config):
    cipher = Cipher(config)
    encrypted = cipher.encrypt("fake-secret", "demo", "acme", "connection-a")
    assert cipher.decrypt(encrypted, "demo", "acme", "connection-a") == "fake-secret"
    for namespace in [("demo", "other", "connection-a"), ("demo", "acme", "connection-b")]:
        with pytest.raises(InvalidTag):
            cipher.decrypt(encrypted, *namespace)
    assert cipher.encrypt("fake-secret", "demo", "acme", "connection-a") != encrypted


async def test_world_readable_key_rejected(config):
    config.secrets.managed.key_source.path.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        Cipher(config)


async def test_cancelled_transaction_rolled_back_before_next_request(config):
    service = Service(config)
    await service.start()
    entered = asyncio.Event()

    async def interrupted():
        async with service.store.transaction() as db:
            await db.execute("INSERT INTO metadata VALUES('cancel-me','not-committed')")
            entered.set()
            await asyncio.Event().wait()

    try:
        task = asyncio.create_task(interrupted())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with service.store.transaction() as db:
            assert await one(db, "SELECT * FROM metadata WHERE key='cancel-me'") is None
            await db.execute("INSERT INTO metadata VALUES('next-request','committed')")
    finally:
        await service.close()


async def test_transactions_do_not_interleave(config):
    service = Service(config)
    await service.start()
    entered, release, second_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def first():
        async with service.store.transaction():
            entered.set()
            await release.wait()

    async def second():
        async with service.store.transaction():
            second_entered.set()

    try:
        one_task = asyncio.create_task(first())
        await entered.wait()
        two_task = asyncio.create_task(second())
        await asyncio.sleep(0)
        assert not second_entered.is_set()
        release.set()
        await asyncio.gather(one_task, two_task)
        assert second_entered.is_set()
    finally:
        await service.close()


async def test_restart_does_not_redispatch_running_call(config):
    service = Service(config)
    await service.start()
    async with service.store.transaction() as db:
        await db.execute(
            "INSERT INTO connections(id,app_id,tenant_id,user_id,integration_id,provider_account_id,display_name,label,status,epoch,revision,ciphertext,key_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "conn_test",
                "demo",
                "default",
                "alice",
                "github",
                "123",
                "Alice",
                None,
                "active",
                1,
                1,
                b"unused",
                "test-key",
                "2026-09-11T00:00:00Z",
                "2026-09-11T00:00:00Z",
            ),
        )
        await db.execute(
            "INSERT INTO calls(id,app_id,tenant_id,user_id,connection_id,session_id,tool,status,"
            "arguments_fingerprint,fingerprint_key_id,error_code,duration_ms,created_at,updated_at,"
            "completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "call_test",
                "demo",
                "default",
                "alice",
                "conn_test",
                None,
                "issue_read",
                "running",
                "a" * 64,
                "test-key",
                None,
                None,
                "2026-09-11T00:00:00Z",
                "2026-09-11T00:00:00Z",
                None,
            ),
        )
    await service.close()
    await service.start()
    try:
        async with service.store.transaction() as db:
            row = await one(db, "SELECT * FROM calls WHERE id='call_test'")
        assert row["status"] == "unknown" and row["error_code"] == "outcome_unknown"
    finally:
        await service.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://api.github.com/user",
        "https://evil.example/user",
        "https://api.github.com:444/user",
        "https://user:pass@api.github.com/user",
    ],
)
async def test_outbound_origin_restrictions(url):
    transport = PinnedTransport("api.github.com", 1024)
    try:
        with pytest.raises(RavnError):
            await transport.handle_async_request(httpx2.Request("GET", url))
    finally:
        await transport.aclose()


@pytest.mark.parametrize("ip", ["127.0.0.1", "169.254.169.254", "10.0.0.1", "::1", "fe80::1"])
async def test_dns_private_addresses_rejected(monkeypatch, ip):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "getaddrinfo", AsyncMock(return_value=[(None, None, None, None, (ip, 443))])
    )
    transport = PinnedTransport("api.github.com", 1024)
    try:
        with pytest.raises(RavnError):
            await transport.handle_async_request(
                httpx2.Request("GET", "https://api.github.com/user")
            )
    finally:
        await transport.aclose()


async def test_dns_ip_pinning_preserves_host_and_tls_name(monkeypatch):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "getaddrinfo", AsyncMock(return_value=[(None, None, None, None, ("8.8.8.8", 443))])
    )
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx2.Response(200, content=b"ok")

    transport = PinnedTransport("api.github.com", 1024)
    await transport.inner.aclose()
    transport.inner = httpx2.MockTransport(upstream)
    response = await transport.handle_async_request(
        httpx2.Request("GET", "https://api.github.com/user")
    )
    assert seen[0].url.host == "8.8.8.8"
    assert seen[0].headers["host"] == "api.github.com"
    assert seen[0].extensions["sni_hostname"] == "api.github.com"
    await response.aclose()
    await transport.aclose()


async def test_provider_redirect_not_followed(monkeypatch):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "getaddrinfo", AsyncMock(return_value=[(None, None, None, None, ("8.8.8.8", 443))])
    )
    transport = PinnedTransport("api.github.com", 1024)
    await transport.inner.aclose()
    transport.inner = httpx2.MockTransport(
        lambda _: httpx2.Response(302, headers={"Location": "https://evil.example"})
    )
    with pytest.raises(RavnError):
        await transport.handle_async_request(httpx2.Request("GET", "https://api.github.com/user"))
    await transport.aclose()


async def test_stream_response_limit():
    class Stream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 10
            yield b"y" * 10

    with pytest.raises(RavnError):
        async for _ in BoundedStream(Stream(), 15):
            pass
