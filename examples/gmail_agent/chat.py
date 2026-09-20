"""A real, talkable Gmail agent: Claude decides which RAVN-gated tool to call.

search_gmail.py is a fixed script -- one query, one tool, no reasoning. This
is the actual agent: an LLM in the loop, deciding from a natural-language
request which of the session's tools to call (if any), same as any agent
built with tool use. Every tool call still goes through the real RAVN gate
(src/ravn/gmail.py's Service.execute -> reviewed_tools()) -- Claude can only
ever call the tools RAVN exposes to this session; it cannot send mail or
exceed the reviewed schema no matter what you ask it to do.

Requires ANTHROPIC_API_KEY. Loaded from examples/gmail_agent/.env if present
(owner-only file, gitignored -- never commit it), otherwise from the
environment.

    printf 'ANTHROPIC_API_KEY=sk-ant-...\n' > examples/gmail_agent/.env && chmod 0600 examples/gmail_agent/.env
    uv run --locked --extra gmail python examples/gmail_agent/chat.py --session-file .ravn/gmail-session.json
"""

import argparse
import asyncio
import json
from pathlib import Path

import anthropic
import httpx2
from dotenv import load_dotenv
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from ravn.crypto import private_file

load_dotenv(Path(__file__).resolve().parent / ".env")

MODEL = "claude-opus-5"
SYSTEM = "You are a Gmail assistant."


def mcp_tool_to_claude_tool(tool) -> dict:
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.input_schema,
    }


async def call_mcp_tool(mcp: Client, name: str, arguments: dict) -> str:
    result = await mcp.call_tool(name, arguments)
    text = "".join(block.text for block in result.content if block.type == "text")
    return text or "(empty result)"


async def run_chat(session_file: Path) -> None:
    session = json.loads(private_file(session_file))
    client = anthropic.Anthropic()

    async with httpx2.AsyncClient(
        headers={"Authorization": "Bearer " + session["token"]},
        trust_env=False,
        follow_redirects=False,
        timeout=35,
    ) as http:
        async with Client(
            streamable_http_client(session["mcp_url"], http_client=http), cache=None
        ) as mcp:
            tools_result = await mcp.list_tools()
            claude_tools = [mcp_tool_to_claude_tool(t) for t in tools_result.tools]
            names = [t["name"] for t in claude_tools]
            print(f"Connected. Tools available to this session: {names}")
            print("Type a message, or 'quit' to exit.\n")

            messages: list[dict] = []
            while True:
                try:
                    raw = await asyncio.to_thread(input, "you> ")
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                user_input = raw.strip()
                if not user_input:
                    continue
                if user_input.lower() in {"quit", "exit"}:
                    break

                messages.append({"role": "user", "content": user_input})

                while True:
                    response = client.messages.create(
                        model=MODEL,
                        max_tokens=4096,
                        system=SYSTEM,
                        tools=claude_tools,
                        messages=messages,
                    )
                    messages.append({"role": "assistant", "content": response.content})

                    if response.stop_reason != "tool_use":
                        break

                    tool_results = []
                    for block in response.content:
                        if block.type != "tool_use":
                            continue
                        print(f"  [calling {block.name}({json.dumps(block.input)})]")
                        try:
                            text = await call_mcp_tool(mcp, block.name, block.input)
                            tool_results.append(
                                {"type": "tool_result", "tool_use_id": block.id, "content": text}
                            )
                        except Exception as e:
                            print(f"  [denied: {e}]")
                            tool_results.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": f"Tool call refused: {e}",
                                    "is_error": True,
                                }
                            )
                    messages.append({"role": "user", "content": tool_results})

                final_text = "\n".join(b.text for b in response.content if b.type == "text")
                print(f"agent> {final_text}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run_chat(args.session_file))
    except anthropic.AuthenticationError:
        raise SystemExit("ANTHROPIC_API_KEY is missing or invalid.") from None
