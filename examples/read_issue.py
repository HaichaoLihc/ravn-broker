"""Existing MCP client example: only a RAVN session reaches the agent runtime."""

import argparse
import asyncio
import json
from pathlib import Path

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from ravn.crypto import private_file


async def run(args):
    session = json.loads(private_file(args.session_file))
    async with httpx2.AsyncClient(
        headers={"Authorization": "Bearer " + session["token"]},
        trust_env=False,
        follow_redirects=False,
        timeout=35,
    ) as http:
        async with Client(
            streamable_http_client(session["mcp_url"], http_client=http), cache=None
        ) as mcp:
            tools = await mcp.list_tools()
            if "issue_read" not in {t.name for t in tools.tools}:
                raise ValueError("issue_read is not available to this session")
            result = await mcp.call_tool(
                "issue_read",
                {
                    "method": "get",
                    "owner": args.owner,
                    "repo": args.repo,
                    "issue_number": args.issue,
                },
            )
            # Intentionally show the authorized tool result, not the provider credential.
            print(json.dumps(result.model_dump(mode="json", by_alias=True), indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-file", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", type=int, required=True)
    try:
        asyncio.run(run(parser.parse_args()))
    except Exception:
        print(
            "Read failed. Check session expiry, connection state, and reviewed schemas; no automatic retry."
        )
        raise SystemExit(1) from None
