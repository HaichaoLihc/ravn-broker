"""Run the broker and console against a simulated Slack, for local development.

Live Slack MCP needs a workspace whose app can enable MCP. This serves the same
broker over real HTTP against an in-process fake, so an external agent can connect
and an operator can exercise the console without any provider account.

    uv run python tools/simulated_slack.py

It prints an application key, a connection, a session token and the console link.
Nothing here ships: `tools/` is outside the packaged `src/ravn`.
"""

import asyncio
import base64
import json
import sys
from pathlib import Path

import httpx
import httpx2
import uvicorn
from mcp import types
from mcp.server import Server
from mcp.server.transport_security import TransportSecuritySettings

from ravn.app import create_app
from ravn.config import Config
from ravn.console import Console, create_console_app
from ravn.github import GitHub
from ravn.manifest import SLACK_SCHEMAS, schema_hash
from ravn.slack import Slack

TOKEN = "xoxp-simulated-user-token-000000"
MESSAGES = [
    ("C01ENGINEERING", "1714000001.000100", "deploy", "Staging deploys run from the release job."),
    (
        "C01ENGINEERING",
        "1714000002.000200",
        "deploy",
        "Roll back with `make rollback` if it fails.",
    ),
    ("C02SETUPHELP", "1714000003.000300", "onboarding", "New laptops need Xcode CLT before brew."),
    (
        "C02SETUPHELP",
        "1714000004.000400",
        "onboarding",
        "Use pyenv 3.12 for the platform services.",
    ),
]


class FakeSlack:
    """Answers the Slack tools RAVN reviews, and nothing else."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.tools = [
            types.Tool(name=name, inputSchema=schema) for name, schema in SLACK_SCHEMAS.items()
        ]
        self.server = Server(
            "fake-slack", on_list_tools=self.list_tools, on_call_tool=self.call_tool
        )
        self.app = self.server.streamable_http_app(
            streamable_http_path="/mcp",
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

    async def list_tools(self, ctx, params):
        return types.ListToolsResult(tools=self.tools)

    async def call_tool(self, ctx, params):
        self.calls.append((params.name, params.arguments))
        arguments = params.arguments or {}
        if params.name == "slack_read_thread":
            found = [m for m in MESSAGES if m[0] == arguments.get("channel_id")]
            body = [{"user": "U01SIMULATED", "text": m[3], "ts": m[1]} for m in found]
        else:
            term = str(arguments.get("query", "")).lower()
            body = [
                {
                    "channel": {"id": m[0], "name": m[2]},
                    "username": "simulated.teammate",
                    "text": m[3],
                    "ts": m[1],
                    "permalink": f"https://simulated.slack.test/archives/{m[0]}/p{m[1]}",
                }
                for m in MESSAGES
                if term in m[3].lower() or term in m[2]
            ]
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps({"matches": body}))]
        )

    def transport(self, host):
        if host == "mcp.slack.com":
            return httpx2.ASGITransport(app=self.app)

        def identity(request):
            value = request.headers.get("authorization", "").removeprefix("Bearer ")
            if value != TOKEN:
                return httpx2.Response(200, json={"ok": False, "error": "invalid_auth"})
            return httpx2.Response(
                200, json={"ok": True, "team_id": "T01SIMULATED", "user_id": "U01SIMULATED"}
            )

        return httpx2.MockTransport(identity)


def build_config(root: Path, fake: FakeSlack) -> Config:
    key = root / "master.key"
    key.write_bytes(base64.b64encode(b"simulated-development-key-00000."))
    key.chmod(0o600)
    return Config.model_validate(
        {
            "deployment_id": "simulated-slack",
            "server": {"listen": "127.0.0.1:8787", "admin_socket": str(root / "run/admin.sock")},
            "storage": {"sqlite_path": str(root / "state/ravn.db")},
            "secrets": {"managed": {"active_key_id": "dev-key", "key_source": {"path": str(key)}}},
            "applications": [{"id": "demo"}],
            "integrations": [
                {
                    "id": "slack",
                    "app_id": "demo",
                    "connector": "slack",
                    "endpoint": "https://mcp.slack.com/mcp",
                    "manifest": "builtin:slack-read-v1",
                    "schema_hashes": {t.name: schema_hash(t) for t in fake.tools},
                }
            ],
        }
    )


def write_session(path: Path, runtime: dict) -> None:
    path.write_text(json.dumps(runtime, indent=2))
    path.chmod(0o600)


def prepare(argv) -> tuple[Path, FakeSlack, Config]:
    """Owner-only state, created before the loop starts so nothing blocks it."""
    root = Path(argv[1] if len(argv) > 1 else ".ravn-simulated").resolve()
    if root.exists():
        raise SystemExit(f"Refusing to reuse {root}. Remove it first so state cannot be mixed.")
    root.mkdir(mode=0o700, parents=True)
    (root / "run").mkdir(mode=0o700)
    fake = FakeSlack()
    return root, fake, build_config(root, fake)


async def main(root: Path, fake: FakeSlack, config: Config) -> int:
    app = create_app(
        config,
        provider={"github_cloud": GitHub(), "slack": Slack(transport_factory=fake.transport)},
    )
    console = Console(app.state.service, 8788)
    async with fake.server.session_manager.run(), app.router.lifespan_context(app):
        service = app.state.service
        app_key = (await service.create_key("demo", "simulated-backend"))["key"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787"
        ) as http:
            headers = {"Authorization": f"Bearer {app_key}", "X-Ravn-User-Id": "alice"}
            created = await http.post(
                "/v1/connections/import",
                headers=headers,
                json={
                    "integration_id": "slack",
                    "credential": {"type": "bearer", "token": TOKEN},
                },
            )
            created.raise_for_status()
            session = await http.post(
                "/v1/sessions",
                headers=headers,
                json={"connection_id": created.json()["id"]},
            )
            session.raise_for_status()
        runtime = session.json()
        await asyncio.to_thread(write_session, root / "session.json", runtime)

        print("\n  Simulated Slack broker\n")
        print(f"  MCP endpoint   {runtime['mcp_url']}")
        print(f"  Session token  {runtime['token']}")
        print(f"  Also written   {root / 'session.json'} (0600)")
        print(f"  Console        {console.ticket()['url']}")
        print("\n  Open the console, choose Permissions, and toggle a tool.")
        print("  The running agent is refused at its next call.\n")

        servers = [
            uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=8787,
                    lifespan="off",
                    access_log=False,
                    log_level="warning",
                )
            ),
            uvicorn.Server(
                uvicorn.Config(
                    create_console_app(console),
                    host="127.0.0.1",
                    port=8788,
                    lifespan="off",
                    access_log=False,
                    log_level="warning",
                )
            ),
        ]
        await asyncio.gather(*(s.serve() for s in servers))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main(*prepare(sys.argv))))
    except KeyboardInterrupt:
        print("\nStopped.")
