"""Three explicit OAuth profiles. No metadata-controlled destinations or automatic retries."""

import asyncio
import base64
import hashlib
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx2

from ravn.common import RavnError, strict_json
from ravn.crypto import private_file

# Google reports some requested short scopes in their long URL form.
GOOGLE_SCOPE_ALIASES = {
    "email": "https://www.googleapis.com/auth/userinfo.email",
    "profile": "https://www.googleapis.com/auth/userinfo.profile",
}
PROFILES = {
    "github_app_pkce": {
        "host": "github.com",
        "authorize": "https://github.com/login/oauth/authorize",
        "token": "https://github.com/login/oauth/access_token",
        "issuer": "https://github.com",
        "pkce": True,
        "scope_separator": None,
        "authorize_params": {},
        "token_type": "bearer",
        "prefixes": ("ghu_",),
        "scope_pattern": r"[A-Za-z0-9_:, .-]*",
        "rotates_refresh": True,
        "requires_granted_scopes": False,
    },
    "slack_user_confidential": {
        "host": "slack.com",
        "authorize": "https://slack.com/oauth/v2_user/authorize",
        "token": "https://slack.com/api/oauth.v2.user.access",
        "issuer": "https://mcp.slack.com",
        "pkce": False,
        "scope_separator": ",",
        "authorize_params": {},
        "token_type": "user",
        "prefixes": ("xoxp-", "xoxe.xoxp-"),
        "scope_pattern": r"[A-Za-z0-9_:, .-]*",
        "rotates_refresh": True,
        "requires_granted_scopes": False,
    },
    "google_web_pkce": {
        "host": "oauth2.googleapis.com",
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "issuer": "https://accounts.google.com",
        "pkce": True,
        "scope_separator": " ",
        # Offline access returns a refresh token; forcing consent returns one on
        # every connect, and never merging older grants keeps authority exact.
        "authorize_params": {
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "false",
        },
        "token_type": "bearer",
        "prefixes": ("ya29.",),
        "scope_pattern": r"[A-Za-z0-9_:, ./-]*",
        # Google keeps the refresh token stable; a refresh response omits it.
        "rotates_refresh": False,
        # Google's consent screen lets users untick individual scopes.
        "requires_granted_scopes": True,
    },
}


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
        self.profile = PROFILES[self.config.profile]
        self.slack = integration.connector == "slack"
        self.host = self.profile["host"]
        self.endpoint = self.profile["token"]

    def authorize_url(self, state, verifier):
        params = {
            "client_id": self.config.client_id,
            "redirect_uri": self.callback,
            "state": state,
            "response_type": "code",
        }
        if self.profile["scope_separator"]:
            params["scope"] = self.profile["scope_separator"].join(self.config.scopes)
        if self.profile["pkce"]:
            params.update(
                code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode(),
                code_challenge_method="S256",
            )
        params.update(self.profile["authorize_params"])
        return self.profile["authorize"] + "?" + urlencode(params)

    async def exchange(self, code, verifier):
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.callback}
        if self.profile["pkce"]:
            data["code_verifier"] = verifier
        return await self.request(data)

    async def refresh(self, bundle):
        return await self.request(
            {"grant_type": "refresh_token", "refresh_token": bundle["refresh_token"]},
            previous=bundle,
        )

    def granted(self, scopes):
        aliases = GOOGLE_SCOPE_ALIASES if self.config.profile == "google_web_pkce" else {}
        return {aliases.get(scope, scope) for scope in scopes}

    async def request(self, data, previous=None):
        try:
            client_secret = secret(private_file(self.config.client_secret_file).decode())
            # Slack MCP metadata advertises client_secret_post; credentials are
            # form body fields, never URL parameters (same for GitHub and Google).
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
            if value.get("token_type", "").lower() != self.profile["token_type"]:
                raise ValueError("Unexpected token type")
            if not access.startswith(self.profile["prefixes"]):
                raise ValueError("Unexpected token authority")
            refresh = (
                secret(value["refresh_token"]) if value.get("refresh_token") is not None else None
            )
            if previous is not None and not refresh:
                if self.profile["rotates_refresh"]:
                    # Rotating profiles: absence is not permission to reuse the old token.
                    raise ValueError("Missing rotated refresh token")
                refresh = previous["refresh_token"]

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
                or not re.fullmatch(self.profile["scope_pattern"], scopes)
            ):
                raise ValueError("Invalid scope response")
            granted = sorted(set(scopes.replace(",", " ").split())) if scopes is not None else None
            if self.profile["requires_granted_scopes"] and not self.granted(
                self.config.scopes
            ) <= self.granted(granted or []):
                raise RavnError(
                    403,
                    "insufficient_scope",
                    "Grant every requested permission on the consent screen, then connect again.",
                )
            expires = expiry("expires_in")
            if refresh and not expires:
                raise ValueError("Refreshable tokens require an access-token lifetime")
            refresh_expires = expiry("refresh_token_expires_in")
            if refresh_expires is None and previous is not None and not value.get("refresh_token"):
                refresh_expires = previous.get("refresh_expires_at")
            return {
                "type": "oauth2",
                "access_token": access,
                "refresh_token": refresh,
                "expires_at": expires,
                "refresh_expires_at": refresh_expires,
                "granted_scopes": granted,
            }
        except RavnError:
            raise
        except Exception:
            raise RavnError(
                502,
                "oauth_exchange_failed",
                "Provider token exchange failed; start a new authorization.",
            ) from None
