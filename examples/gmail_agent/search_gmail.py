"""The Gmail agent: only a RAVN session reaches this script. No Google OAuth
token, no provider credential -- just a RAVN-issued session bearer.

Gating happens entirely inside the real broker: Service.execute ->
GmailLocal.call (src/ravn/gmail.py) -> reviewed_tools()/GMAIL_LOCAL_SCHEMAS
(src/ravn/manifest.py). This script cannot call a tool that isn't in that
reviewed set, no matter what it asks for.

    uv run python examples/gmail_agent/search_gmail.py --session-file .ravn/gmail-session.json --query "from:boss@example.com"
"""

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
            names = {t.name for t in tools.tools}
            print(f"tools available to this session: {sorted(names)}")
            if "search_threads" not in names:
                raise ValueError("search_threads is not available to this session")
            result = await mcp.call_tool("search_threads", {"query": args.query, "page_size": 5})
            # Intentionally show the authorized tool result, not the Google credential.
            print(json.dumps(result.model_dump(mode="json", by_alias=True), indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-file", type=Path, required=True)
    parser.add_argument("--query", required=True)
    try:
        asyncio.run(run(parser.parse_args()))
    except Exception:
        print(
            "Search failed. Check session expiry, connection state, and reviewed schemas; "
            "no automatic retry."
        )
        raise SystemExit(1) from None
