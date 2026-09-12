"""Two explicit OAuth profiles. No metadata-controlled destinations or automatic retries."""

import asyncio
import base64
import hashlib
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx2

from ravn.common import RavnError, strict_json
from ravn.crypto import private_file


def deadline(seconds):
    return (
        (datetime.now(UTC) + timedelta(seconds=seconds))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def secret(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 16384
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
    ):
        raise ValueError("Malformed token")
    return value


class OAuthProvider:
    def __init__(self, integration, provider, callback):
        self.integration, self.provider, self.callback = integration, provider, callback
        self.config = integration.oauth
        self.slack = integration.connector == "slack"
        self.host = "slack.com" if self.slack else "github.com"
        self.endpoint = (
            "https://slack.com/api/oauth.v2.user.access"
            if self.slack
            else "https://github.com/login/oauth/access_token"
        )

    def authorize_url(self, state, verifier):
        params = {
            "client_id": self.config.client_id,
            "redirect_uri": self.callback,
            "state": state,
            "response_type": "code",
        }
        if self.slack:
            params["scope"] = ",".join(self.config.scopes)
        else:
            params.update(
                code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode(),
                code_challenge_method="S256",
            )
        endpoint = (
            "https://slack.com/oauth/v2_user/authorize"
            if self.slack
            else "https://github.com/login/oauth/authorize"
        )
        return endpoint + "?" + urlencode(params)

    async def exchange(self, code, verifier):
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.callback}
        if not self.slack:
            data["code_verifier"] = verifier
        return await self.request(data)

    async def refresh(self, bundle):
        return await self.request(
            {"grant_type": "refresh_token", "refresh_token": bundle["refresh_token"]}, rotating=True
        )

    async def request(self, data, rotating=False):
        try:
            client_secret = secret(private_file(self.config.client_secret_file).decode())
            # Slack MCP metadata advertises client_secret_post; credentials are
            # form body fields, never URL parameters (same for GitHub).
            data = {**data, "client_id": self.config.client_id, "client_secret": client_secret}
            async with (
                asyncio.timeout(15),
                httpx2.AsyncClient(
                    transport=self.provider.transport_factory(self.host),
                    trust_env=False,
                    follow_redirects=False,
                    timeout=15,
                ) as client,
            ):
                response = await client.post(
                    self.endpoint,
                    data=data,
                    headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                )
            if len(response.content) > 65536:
                raise ValueError("Oversized token response")
            value = strict_json(response.content)
            if not isinstance(value, dict):
                raise ValueError("Invalid token response")
            if value.get("error") in {
                "invalid_grant",
                "invalid_refresh_token",
                "refresh_token_expired",
                "bad_refresh_token",
                "token_revoked",
                "token_expired",
            }:
                raise RavnError(
                    409, "connection_reauth_required", "Provider authorization must be renewed."
                )
            if (
                response.status_code != 200
                or value.get("error")
                or (self.slack and value.get("ok") is not True)
            ):
                raise ValueError("Token exchange failed")
            access = secret(value.get("access_token"))
            expected = "user" if self.slack else "bearer"
            if value.get("token_type", "").lower() != expected:
                raise ValueError("Unexpected token type")
            if not access.startswith(("xoxp-", "xoxe.xoxp-") if self.slack else ("ghu_",)):
                raise ValueError("Unexpected token authority")
            refresh = (
                secret(value["refresh_token"]) if value.get("refresh_token") is not None else None
            )
            if rotating and not refresh:
                # Both profiles rotate: absence is not permission to reuse the old token.
                raise ValueError("Missing rotated refresh token")

            def expiry(key):
                seconds = value.get(key)
                if seconds is None:
                    return None
                if type(seconds) is not int or not 1 <= seconds <= 31536000:
                    raise ValueError("Invalid lifetime")
                return deadline(seconds)

            scopes = value.get("scope")
            if scopes is None and self.slack:
                scopes = value.get("authed_user", {}).get("scope")
            if scopes is not None and (
                not isinstance(scopes, str)
                or len(scopes) > 4096
                or not re.fullmatch(r"[A-Za-z0-9_:, .-]*", scopes)
            ):
                raise ValueError("Invalid scope response")
            expires = expiry("expires_in")
            if refresh and not expires:
                raise ValueError("Refreshable tokens require an access-token lifetime")
            return {
                "type": "oauth2",
                "access_token": access,
                "refresh_token": refresh,
                "expires_at": expires,
                "refresh_expires_at": expiry("refresh_token_expires_in"),
                "granted_scopes": sorted(set(scopes.replace(",", " ").split()))
                if scopes is not None
                else None,
            }
        except RavnError:
            raise
        except Exception:
            raise RavnError(
                502,
                "oauth_exchange_failed",
                "Provider token exchange failed; start a new authorization.",
            ) from None
