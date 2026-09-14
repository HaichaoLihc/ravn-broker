"""Google's hosted Gmail MCP server; one user's OAuth grant, never domain-wide delegation."""

import asyncio
import re

import httpx2

from ravn.common import RavnError, strict_json
from ravn.github import GitHub

EMAIL = re.compile(r"[^@\s,;<>\"]{1,64}@[A-Za-z0-9.-]{1,253}")


class Gmail(GitHub):
    # Reuse the bounded remote-MCP mechanics, not GitHub authentication/headers.
    host = "gmailmcp.googleapis.com"
    endpoint = "https://gmailmcp.googleapis.com/mcp/v1"
    label = "Gmail"
    extra_headers = {}
    # Google answers missing scopes and disabled APIs with 403 before executing.
    denied_status = 403

    async def identify(self, credential: str) -> tuple[str, str]:
        try:
            async with (
                asyncio.timeout(10),
                httpx2.AsyncClient(
                    transport=self.transport_factory("openidconnect.googleapis.com"),
                    trust_env=False,
                    follow_redirects=False,
                    timeout=10,
                ) as client,
            ):
                response = await client.get(
                    "https://openidconnect.googleapis.com/v1/userinfo",
                    headers={
                        "Authorization": f"Bearer {credential}",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                )
            if response.status_code == 401:
                raise RavnError(
                    401, "credential_invalid", "Google rejected the supplied credential."
                )
            if response.status_code != 200:
                raise ValueError("Invalid identity")
            data = strict_json(response.content)
            # `sub` is Google's stable account ID; an address can be renamed.
            account, email = data.get("sub"), data.get("email")
            if (
                not isinstance(account, str)
                or not re.fullmatch(r"[A-Za-z0-9]{1,255}", account)
                or data.get("email_verified") is not True
                or not isinstance(email, str)
                or not EMAIL.fullmatch(email)
                or credential in email
            ):
                raise ValueError("Invalid identity")
            return account, email
        except RavnError:
            raise
        except Exception:
            raise RavnError(
                502, "upstream_unavailable", "Google account verification failed."
            ) from None
