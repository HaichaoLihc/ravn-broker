"""Run the broker against a simulated Gmail, to try the write approval gate locally.

An external agent connects to the printed MCP endpoint and asks for a draft; the
call waits until an operator decides it in another terminal:

    uv run python tools/simulated_gmail.py
    uv run ravn approvals --config .ravn-gmail/ravn.yaml

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
import yaml
from mcp import types
from mcp.server import Server
from mcp.server.transport_security import TransportSecuritySettings

from ravn.app import create_admin_app, create_app
from ravn.cli import AdminServer
from ravn.config import Config
from ravn.github import GitHub
from ravn.gmail import Gmail
from ravn.manifest import GMAIL_SCHEMAS, schema_hash

TOKEN = "ya29.simulated-google-access-token"
THREADS = [
    {
        "id": "18f0a1b2c3d4e5f6",
        "subject": "Refund #1042",
        "from": "dana@example.com",
        "snippet": "Any update on my refund?",
    },
]


class FakeGmail:
    """Answers the Gmail tools RAVN reviews, and remembers every draft it made."""

    def __init__(self):
        self.drafts: list[dict] = []
        self.tools = [types.Tool(name=n, inputSchema=s) for n, s in GMAIL_SCHEMAS.items()]
        self.server = Server("fake-gmail", on_list_tools=self.list_tools, on_call_tool=self.call)
        self.app = self.server.streamable_http_app(
            streamable_http_path="/mcp/v1",
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

    async def list_tools(self, ctx, params):
        return types.ListToolsResult(tools=self.tools)

    async def call(self, ctx, params):
        arguments = params.arguments or {}
        if params.name == "create_draft":
            draft = {"id": f"r-{len(self.drafts) + 1}", **arguments}
            self.drafts.append(draft)
            print(f"\n  [simulated Gmail] draft created: {json.dumps(draft)}\n", flush=True)
            payload = {"draft": draft}
        elif params.name == "list_drafts":
            payload = {"drafts": self.drafts}
        else:
            payload = {"threads": THREADS}
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload))]
        )

    def transport(self, host):
        if host == "gmailmcp.googleapis.com":
            return httpx2.ASGITransport(app=self.app)
        return httpx2.MockTransport(
            lambda request: httpx2.Response(
                200, json={"sub": "109876", "email": "alice@example.com", "email_verified": True}
            )
        )


def prepare(argv) -> tuple[Path, FakeGmail, Config]:
    root = Path(argv[1] if len(argv) > 1 else ".ravn-gmail").resolve()
    if root.exists():
        raise SystemExit(f"Refusing to reuse {root}. Remove it first so state cannot be mixed.")
    root.mkdir(mode=0o700, parents=True)
    (root / "run").mkdir(mode=0o700)
    key = root / "master.key"
    key.write_bytes(base64.b64encode(b"simulated-development-key-00000."))
    key.chmod(0o600)
    fake = FakeGmail()
    config = Config.model_validate(
        {
            "deployment_id": "simulated-gmail",
            "server": {"listen": "127.0.0.1:8787", "admin_socket": str(root / "run/admin.sock")},
            "storage": {"sqlite_path": str(root / "state/ravn.db")},
            "secrets": {"managed": {"active_key_id": "dev-key", "key_source": {"path": str(key)}}},
            "applications": [{"id": "demo"}],
            "integrations": [
                {
                    "id": "gmail",
                    "app_id": "demo",
                    "connector": "gmail",
                    "endpoint": "https://gmailmcp.googleapis.com/mcp/v1",
                    "manifest": "builtin:gmail-v1",
                    "schema_hashes": {t.name: schema_hash(t) for t in fake.tools},
                }
            ],
        }
    )
    # Written so `ravn approvals --config` can reach this process's admin socket.
    (root / "ravn.yaml").write_text(yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False))
    (root / "ravn.yaml").chmod(0o600)
    return root, fake, config


async def main(root: Path, fake: FakeGmail, config: Config) -> int:
    app = create_app(
        config,
        provider={"github_cloud": GitHub(), "gmail": Gmail(transport_factory=fake.transport)},
    )
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
                json={"integration_id": "gmail", "credential": {"type": "bearer", "token": TOKEN}},
            )
            created.raise_for_status()
            session = await http.post(
                "/v1/sessions", headers=headers, json={"connection_id": created.json()["id"]}
            )
            session.raise_for_status()
        runtime = session.json()
        mcp = {
            "mcpServers": {
                "ravn-gmail": {
                    "type": "http",
                    "url": runtime["mcp_url"],
                    "headers": {"Authorization": f"Bearer {runtime['token']}"},
                    # Outlasts the 300 s approval wait; Claude Code's idle default is also 300 s.
                    "timeout": 360000,
                }
            }
        }
        for name, content in (("session.json", runtime), ("mcp.json", mcp)):
            (root / name).write_text(json.dumps(content, indent=2))
            (root / name).chmod(0o600)

        print("\n  Simulated Gmail broker\n")
        print(f"  MCP endpoint   {runtime['mcp_url']}")
        print(f"  Agent config   {root / 'mcp.json'} (0600)")
        print(f"  Approve with   uv run ravn approvals --config {root / 'ravn.yaml'}\n", flush=True)

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
            AdminServer(
                uvicorn.Config(
                    create_admin_app(service),
                    uds=str(config.server.admin_socket),
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
