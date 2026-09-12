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
