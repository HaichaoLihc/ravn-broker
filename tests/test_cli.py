"""A real local CLI process/HTTP/Unix-socket smoke test; never contacts GitHub."""

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from ravn.approvals import clean
from ravn.cli import render


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "ravn.cli", *map(str, args)],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )


@pytest.mark.parametrize("with_console", [False, True])
def test_cli_init_serve_admin_and_backend(with_console):
    # Short path matters on macOS, which limits Unix socket paths to 104 bytes.
    with tempfile.TemporaryDirectory(prefix="ravn-cli-", dir="/tmp") as temporary:
        root = Path(temporary) / "state"
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            console_port = reserved.getsockname()[1]
        cli("init", "--directory", root, "--port", port)
        config = root / "ravn.yaml"
        cli("config-check", "--config", config)
        process = subprocess.Popen(
            [sys.executable, "-m", "ravn.cli", "serve", "--config", str(config)]
            + (["--console", "--console-port", str(console_port)] if with_console else []),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        secret = None
        ticket = None
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=1
            ) as client:
                deadline = time.monotonic() + 10
                while True:
                    assert process.poll() is None, "CLI server exited before readiness"
                    try:
                        ready = client.get("/readyz").status_code == 200
                    except httpx.TransportError:
                        ready = False
                    if ready and (root / "run/admin.sock").exists():
                        break
                    assert time.monotonic() < deadline, "CLI server did not become ready"
                    time.sleep(0.05)

                key_file = root / "backend.key"
                cli("app-key", "create", "--config", config, "--app", "demo", "--output", key_file)
                secret = key_file.read_text().strip()
                assert secret.startswith("rv_app_")
                assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
                assert stat.S_IMODE((root / "run/admin.sock").stat().st_mode) == 0o600
                assert stat.S_IMODE((root / "state").stat().st_mode) == 0o700
                assert client.get("/healthz").status_code == 200
                assert client.post("/admin/v1/app-keys", json={}).status_code == 404
                assert cli("approvals", "--config", config, "list").stdout == (
                    "No pending approvals.\n"
                )
                decided = subprocess.run(
                    [sys.executable, "-m", "ravn.cli", "approvals", "--config", str(config)]
                    + ["approve", "apr_missing"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                assert decided.returncode == 1
                assert decided.stdout.startswith("not_found:")
                assert client.get("/v1/connections").status_code == 401
                result = cli(
                    "connections",
                    "list",
                    "--config",
                    config,
                    "--app-key-file",
                    key_file,
                    "--user",
                    "alice",
                )
                assert json.loads(result.stdout)["data"] == []
                assert secret not in result.stdout
                if with_console:
                    login_url = cli("console", "--config", config, "--no-open").stdout.strip()
                    ticket = login_url.split("#ticket=")[1]
                    assert login_url.startswith(f"http://127.0.0.1:{console_port}/console/")
                    with httpx.Client(
                        base_url=f"http://127.0.0.1:{console_port}", trust_env=False, timeout=2
                    ) as browser:
                        assert browser.get("/console/").status_code == 200
                        response = browser.post(
                            "/console/api/v1/auth/exchange",
                            headers={"Origin": f"http://127.0.0.1:{console_port}"},
                            json={"ticket": ticket},
                        )
                        assert response.status_code == 200
                        assert (
                            browser.get("/console/api/v1/bootstrap").json()["applications"][0]["id"]
                            == "demo"
                        )
                    assert client.get("/console/api/v1/bootstrap").status_code == 404
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise AssertionError("CLI server failed to shut down") from None
        assert process.returncode in {0, -signal.SIGINT}
        if secret:
            assert secret not in stdout + stderr
        if ticket:
            assert ticket not in stdout + stderr
        assert "Traceback" not in stderr
        assert os.stat(root / "state/ravn.db").st_mode & 0o077 == 0


HOSTILE = (
    "a\x1b[31mred\x1b[0m"
    "\x1b]52;c;cGF5bG9hZA==\x07"
    "\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\"
    "\rover\x07\x9b2J\u202eevil\u200bz"
    "\n\tkept\x1b"
)


def test_clean_strips_every_terminal_control():
    text = clean(HOSTILE, 1000)
    assert text == "aredlinkover2Jevilz\n\tkept"
    assert not any(c in text for c in "\x1b\r\x07\x9b\u202e\u200b")
    long = clean("x" * 10000, 4000)
    assert long.endswith("[truncated]") and len(long) < 4100


def test_render_never_passes_escapes_through():
    item = {
        key: HOSTILE
        for key in (
            "id",
            "app_id",
            "tenant_id",
            "user_id",
            "session_id",
            "connection_id",
            "tool",
            "arguments",
        )
    }
    item |= {"created_at": "2026-01-01T00:00:00.000000Z", "expires_at": HOSTILE}
    text = render(item)
    assert "\x1b" not in text and "\r" not in text and "\x9b" not in text
    assert "kept" in text and "requested:  unknown" in text


def test_render_shows_every_argument_when_a_value_is_long():
    arguments = {
        "body": "b" * 10000,
        "cc": "boss@example.com",
        "subject": "Quarterly numbers",
        "to": "attacker@example.com",
    }
    item = {"id": "a1", "tool": "create_draft", "arguments": json.dumps(arguments)}
    text = render(item)
    assert "to: attacker@example.com" in text
    assert "cc: boss@example.com" in text and "subject: Quarterly numbers" in text
    # Recipients come before the long body, and the cut says how much was hidden.
    assert text.index("to: attacker") < text.index("body: ")
    assert "[truncated, 10000 chars total]" in text and "b" * 10000 not in text


def test_render_never_cuts_recipients():
    padding = [f"{'x' * 240}{i}@example.com" for i in range(19)]
    arguments = {"to": [*padding, "real-target@example.com"], "subject": "Hi", "body": "b"}
    text = render({"id": "a1", "tool": "create_draft", "arguments": json.dumps(arguments)})
    assert "- real-target@example.com" in text and "truncated" not in text
    assert all(f"- {address}" in text for address in padding)
