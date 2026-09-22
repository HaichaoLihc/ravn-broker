#!/usr/bin/env python3
"""A local MCP server wrapping the real Gmail REST API.

Stands in for Google's own gmailmcp.googleapis.com, which requires Workspace
Developer Preview enrollment. This server holds no credentials of its own --
every request's `Authorization: Bearer <token>` is the caller's own Google
OAuth access token (broker forwards its stored per-connection credential
exactly as it does for GitHub/Slack, see src/ravn/github.py's `client()`),
and each tool call makes a real, per-caller-authenticated request to
googleapis.com. Nothing is cached or persisted here.

Exposes exactly the four tools src/ravn/manifest.py's GMAIL_LOCAL_SCHEMAS
reviews, with matching argument shapes and semantics to Google's own MCP
tool set (search_threads, get_thread, get_message, list_labels), so this is
a drop-in replacement for the endpoint in .ravn/ravn.yaml's gmail_local
integration -- no broker code changes, only:

    endpoint: http://127.0.0.1:8790/mcp

Run it:

    cd examples/gmail_agent && ../../.venv/bin/python3 mcp_server.py
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings

LISTEN = "127.0.0.1"
PORT = 8790


TOOLS = [
    types.Tool(
        name="search_threads",
        description="Search Gmail threads matching a query.",
        inputSchema={
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 20},
                "page_token": {"type": "string", "maxLength": 2048},
            },
        },
        annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    ),
    types.Tool(
        name="get_thread",
        description="Get a Gmail thread by id.",
        inputSchema={
            "type": "object",
            "additionalProperties": False,
            "required": ["thread_id"],
            "properties": {"thread_id": {"type": "string", "minLength": 1, "maxLength": 512}},
        },
        annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    ),
    types.Tool(
        name="get_message",
        description="Get a Gmail message by id.",
        inputSchema={
            "type": "object",
            "additionalProperties": False,
            "required": ["message_id"],
            "properties": {"message_id": {"type": "string", "minLength": 1, "maxLength": 512}},
        },
        annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    ),
    types.Tool(
        name="list_labels",
        description="List Gmail labels on the connected account.",
        inputSchema={"type": "object", "additionalProperties": False, "properties": {}},
        annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    ),
    # Deliberately NOT in src/ravn/manifest.py's GMAIL_LOCAL_SCHEMAS. Visible
    # here (list_tools() advertises it, Claude can see and attempt it) so
    # this file can demonstrate that visibility and permission are
    # independent: Service.execute() still refuses it on every call,
    # regardless of what the wrapper server advertises.
    types.Tool(
        name="create_draft",
        description="Create a draft email in the connected Gmail account.",
        inputSchema={
            "type": "object",
            "additionalProperties": False,
            "required": ["to", "subject", "body"],
            "properties": {
                "to": {"type": "string", "minLength": 1, "maxLength": 512},
                "subject": {"type": "string", "maxLength": 512},
                "body": {"type": "string", "maxLength": 100000},
            },
        },
        annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    ),
]


def _service(bearer: str):
    creds = Credentials(token=bearer)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _headers(msg) -> dict[str, str]:
    return {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}


async def list_tools(ctx, params):
    return types.ListToolsResult(tools=TOOLS)


async def call_tool(ctx, params):
    bearer = ctx.request.headers.get("authorization", "").removeprefix("Bearer ").strip() or None
    if not bearer:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="Missing bearer credential")],
            isError=True,
        )
    args = params.arguments or {}
    try:
        service = _service(bearer)
        if params.name == "search_threads":
            resp = (
                service.users()
                .threads()
                .list(
                    userId="me",
                    q=args["query"],
                    maxResults=args.get("page_size", 10),
                    pageToken=args.get("page_token"),
                )
                .execute()
            )
            threads = [
                {"id": t["id"], "snippet": t.get("snippet", "")} for t in resp.get("threads", [])
            ]
            payload = {"threads": threads, "next_page_token": resp.get("nextPageToken")}
        elif params.name == "get_thread":
            resp = service.users().threads().get(userId="me", id=args["thread_id"]).execute()
            payload = {
                "id": resp["id"],
                "messages": [
                    {
                        "id": m["id"],
                        "subject": _headers(m).get("Subject", ""),
                        "from": _headers(m).get("From", ""),
                        "snippet": m.get("snippet", ""),
                    }
                    for m in resp.get("messages", [])
                ],
            }
        elif params.name == "get_message":
            resp = service.users().messages().get(userId="me", id=args["message_id"]).execute()
            headers = _headers(resp)
            payload = {
                "id": resp["id"],
                "subject": headers.get("Subject", ""),
                "from": headers.get("From", ""),
                "snippet": resp.get("snippet", ""),
            }
        elif params.name == "list_labels":
            resp = service.users().labels().list(userId="me").execute()
            payload = {
                "labels": [{"id": lbl["id"], "name": lbl["name"]} for lbl in resp.get("labels", [])]
            }
        elif params.name == "create_draft":
            import base64
            from email.mime.text import MIMEText

            message = MIMEText(args["body"])
            message["to"] = args["to"]
            message["subject"] = args["subject"]
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
            resp = (
                service.users()
                .drafts()
                .create(userId="me", body={"message": {"raw": raw}})
                .execute()
            )
            payload = {"id": resp["id"], "message_id": resp["message"]["id"]}
        else:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Unknown tool: {params.name}")],
                isError=True,
            )
    except RefreshError:
        return types.CallToolResult(
            content=[
                types.TextContent(type="text", text="Google rejected the supplied credential")
            ],
            isError=True,
        )
    except HttpError as e:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Gmail API error: {e}")], isError=True
        )
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(payload))])


def build_app():
    mcp = Server("gmail-wrapper", version="0.1.0", on_list_tools=list_tools, on_call_tool=call_tool)
    mcp_app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    # mcp_app already serves at /mcp (streamable_http_app's default path).
    app.mount("/", mcp_app)
    return app


if __name__ == "__main__":
    print(f"Gmail MCP wrapper listening on http://{LISTEN}:{PORT}/mcp")
    print("Every request's Authorization: Bearer token is used as-is against the real Gmail API.")
    uvicorn.run(build_app(), host=LISTEN, port=PORT, log_level="warning")
