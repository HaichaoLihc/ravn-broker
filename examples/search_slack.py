"""Search public Slack messages through RAVN using an ordinary MCP client."""

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
            if "slack_search_public" not in {t.name for t in tools.tools}:
                raise ValueError("slack_search_public is unavailable to this session")
            result = await mcp.call_tool("slack_search_public", {"query": args.query, "limit": 10})
            # Intentionally prints authorized Slack content, never a provider token.
            print(json.dumps(result.model_dump(mode="json", by_alias=True), indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-file", type=Path, required=True)
    parser.add_argument("--query", required=True)
    try:
        asyncio.run(run(parser.parse_args()))
    except Exception:
        print(
            "Read failed. Check session expiry, connection state, scopes, and schema pins; no automatic retry."
        )
        raise SystemExit(1) from None
