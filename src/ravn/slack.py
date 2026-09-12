"""Slack's official remote MCP server; personal user tokens, never bot authority."""

import asyncio
import re

import httpx2

from ravn.common import RavnError, strict_json
from ravn.github import GitHub


class Slack(GitHub):
    # Reuse the bounded remote-MCP mechanics, not GitHub authentication/headers.
    host = "mcp.slack.com"
    endpoint = "https://mcp.slack.com/mcp"
    label = "Slack"
    extra_headers = {}

    async def identify(self, credential: str) -> tuple[str, str]:
        if not credential.startswith(("xoxp-", "xoxe.xoxp-")):
            raise RavnError(
                400,
                "credential_invalid",
                "Slack requires a user token, not a bot or browser-session credential.",
            )
        try:
            async with (
                asyncio.timeout(10),
                httpx2.AsyncClient(
                    transport=self.transport_factory("slack.com"),
                    trust_env=False,
                    follow_redirects=False,
                    timeout=10,
                ) as client,
            ):
                response = await client.post(
                    "https://slack.com/api/auth.test",
                    headers={
                        "Authorization": f"Bearer {credential}",
                        "Accept-Encoding": "identity",
                    },
                )
            data = strict_json(response.content)
            if (
                response.status_code == 200
                and data.get("ok") is False
                and data.get("error")
                in {"invalid_auth", "token_revoked", "token_expired", "account_inactive"}
            ):
                raise RavnError(
                    401, "credential_invalid", "Slack rejected the supplied user credential."
                )
            if response.status_code != 200 or data.get("ok") is not True or data.get("bot_id"):
                raise ValueError("Invalid identity")
            team, user = data.get("team_id"), data.get("user_id")
            if (
                not isinstance(team, str)
                or not re.fullmatch(r"T[A-Z0-9]{2,31}", team)
                or not isinstance(user, str)
                or not re.fullmatch(r"[UW][A-Z0-9]{2,31}", user)
            ):
                raise ValueError("Invalid identity")
            # Workspace is part of stable identity; reconnect cannot switch it.
            return f"{team}:{user}", f"{team} / {user}"
        except RavnError:
            raise
        except Exception:
            raise RavnError(
                502, "upstream_unavailable", "Slack account verification failed."
            ) from None
