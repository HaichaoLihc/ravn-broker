"""Bypass Claude entirely: call a tool RAVN never reviewed, directly.

chat.py showed Claude declining to send mail because no send tool was in its
list -- honest, but it's proof that Claude behaved, not proof that RAVN
enforces anything. This script calls create_draft (a real tool the upstream
Gmail MCP server serves, per examples/gmail_agent/mcp_server.py's TOOLS list)
directly over the same RAVN session, with no LLM in the loop at all. If
RAVN's gate is real, this is refused by broker itself -- the same
enforcement
tests/test_gmail.py::test_gmail_call_tool_outside_reviewed_schema_is_denied
proves against a fake upstream, here proven live against your real account.

    uv run --locked --extra gmail python examples/gmail_agent/force_denied_call.py --session-file .ravn/gmail-session.json
"""

import argparse
import asyncio
import json
from pathlib import Path

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from ravn.crypto import private_file


async def run(session_file: Path) -> None:
    session = json.loads(private_file(session_file))
    async with httpx2.AsyncClient(
        headers={"Authorization": "Bearer " + session["token"]},
        trust_env=False,
        follow_redirects=False,
        timeout=35,
    ) as http:
        async with Client(
            streamable_http_client(session["mcp_url"], http_client=http), cache=None
        ) as mcp:
            tools = (await mcp.list_tools()).tools
            print(f"Tools RAVN exposes to this session: {[t.name for t in tools]}")
            print("Attempting create_draft anyway (not in that list; not reviewed) ...")
            try:
                result = await mcp.call_tool(
                    "create_draft",
                    {"to": "test@example.com", "subject": "test", "body": "hello from the agent"},
                )
                print("UNEXPECTED: call succeeded ->", result)
            except MCPError as e:
                print(f"DENIED by RAVN: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-file", type=Path, required=True)
    asyncio.run(run(parser.parse_args().session_file))
