"""Real loopback browser helper; fake backend, no external OAuth or browser launch."""

import asyncio
import socket
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

from ravn.connect_cli import connect

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "outcome", ["success", "lost_completion_response", "denied", "unavailable"]
)
async def test_cli_browser_binding_and_completion_recovery(config, monkeypatch, outcome):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    args = SimpleNamespace(
        return_url=f"http://127.0.0.1:{port}/return",
        user="alice",
        tenant=None,
        integration="slack",
        reconnect=None,
        no_open=False,
    )
    opened = asyncio.get_running_loop().create_future()

    def open_browser(url):
        opened.set_result(url)
        return True

    monkeypatch.setattr("ravn.connect_cli.webbrowser.open", open_browser)
    calls, saved = [], {}

    async def backend(config, args, method, path, payload=None):
        calls.append((method, path))
        if path == "/v1/connect-sessions":
            saved.update(payload)
            return {"id": "cs_test", "authorization_url": "https://provider.example/authorize"}
        if path.endswith("/complete"):
            assert payload["completion_code"] == "x" * 43
            if outcome != "success":
                raise ValueError("Response lost")
            return {"id": "conn_test"}
        if path == "/v1/connect-sessions/cs_test":
            if outcome == "unavailable":
                raise ValueError("Backend unavailable")
            return {"status": "completed", "connection_id": "conn_test"}
        return {"id": "conn_test"}

    task = asyncio.create_task(connect(config, args, backend))
    try:
        async with asyncio.timeout(5):
            start = await opened
            origin = "http://" + urlsplit(start).netloc
            query = {
                "session_id": "cs_test",
                "app_state": saved["app_state"],
                "completion_code": "x" * 43,
            }
            if outcome == "denied":
                query.pop("completion_code")
                query["status"] = "denied"
            async with httpx.AsyncClient(base_url=origin, trust_env=False) as browser:
                assert (await browser.get("/return", params=query)).status_code == 400
                assert (await browser.get(start)).status_code == 303
                assert (await browser.get(start)).status_code == 400
                assert (
                    await browser.get("/return", params={**query, "app_state": "wrong"})
                ).status_code == 400
                response = await browser.get("/return", params=query)
                assert response.status_code == {"denied": 400, "unavailable": 502}.get(outcome, 200)
            if outcome in {"denied", "unavailable"}:
                with pytest.raises(ValueError):
                    await task
                assert ("POST", "/v1/connect-sessions/cs_test/cancel") in calls
            else:
                assert await task == {"id": "conn_test"}
                assert calls.count(("POST", "/v1/connect-sessions/cs_test/complete")) == 1
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
