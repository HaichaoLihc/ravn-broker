"""Gmail's REST API for sending; one user's OAuth grant, never domain-wide delegation."""

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from email.message import EmailMessage
from email.policy import SMTP

import httpx2
from mcp import types

from ravn.common import RavnError, strict_json
from ravn.config import Integration
from ravn.gmail import Gmail
from ravn.manifest import GMAIL_SEND_SCHEMAS, reviewed_tools


def encode(arguments: dict) -> str:
    """The RFC 2822 message Gmail sends, base64url-encoded as its API expects."""
    message = EmailMessage(policy=SMTP)
    message["To"] = ", ".join(arguments["to"])
    for header in ("cc", "bcc"):
        if arguments.get(header):
            # Gmail delivers to Bcc and strips the header from what recipients see.
            message[header.title()] = ", ".join(arguments[header])
    message["Subject"] = arguments["subject"]
    message.set_content(arguments["body"])
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


class GmailApi(Gmail):
    host = "gmail.googleapis.com"
    endpoint = "https://gmail.googleapis.com/gmail/v1"
    label = "Gmail"

    async def inspect(self, credential: str) -> list[types.Tool]:
        # A REST API publishes no MCP catalogue: the reviewed schema is the contract,
        # and pinning its hash still makes any change here need operator review.
        return [types.Tool(name=n, inputSchema=s) for n, s in GMAIL_SEND_SCHEMAS.items()]

    async def call(
        self,
        credential: str,
        integration: Integration,
        name: str,
        arguments: dict,
        before_dispatch: Callable[[], Awaitable[None]],
    ) -> types.CallToolResult:
        reviewed = reviewed_tools(
            await self.inspect(credential), integration.schema_hashes, integration.connector
        )
        if name not in {t.name for t in reviewed}:
            raise RavnError(403, "permission_denied", "Tool is unavailable or not approved.")
        try:
            raw = encode(arguments)
        except Exception:
            raise RavnError(422, "invalid_arguments", "The email could not be encoded.") from None
        try:
            async with (
                asyncio.timeout(self.timeout),
                httpx2.AsyncClient(
                    transport=self.transport_factory(self.host),
                    trust_env=False,
                    follow_redirects=False,
                    timeout=self.timeout,
                ) as client,
            ):
                await before_dispatch()
                response = await client.post(
                    f"{self.endpoint}/users/me/messages/send",
                    headers={
                        "Authorization": f"Bearer {credential}",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                    json={"raw": raw},
                )
        except RavnError:
            raise
        except Exception:
            raise RavnError(502, "upstream_unavailable", "Gmail send failed.") from None
        # Google answers these before sending anything; every other failure is ambiguous.
        if response.status_code == 401:
            raise RavnError(401, "credential_invalid", "Google rejected the account credential.")
        if response.status_code in {400, 403}:
            raise RavnError(
                403, "provider_denied", "Gmail refused this send; check granted permissions."
            )
        if response.status_code != 200:
            raise RavnError(502, "upstream_unavailable", "Gmail send failed.")
        try:
            data = strict_json(response.content)
            sent = {"id": str(data["id"]), "threadId": str(data["threadId"]), "status": "sent"}
        except Exception:
            raise RavnError(502, "upstream_unavailable", "Gmail send failed.") from None
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(sent))])
