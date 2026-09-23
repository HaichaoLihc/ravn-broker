"""Configured OAuth2 authorization-code flow with S256 PKCE and bounded token exchange."""

import asyncio
import base64
import hashlib
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote_plus, urlencode, urlsplit

import httpx2

from ravn.common import RavnError, strict_json


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
    def __init__(self, integration, provider, callback, *, client_secret=None):
        self.integration, self.provider, self.callback = integration, provider, callback
        self.config = integration.oauth
        self.client_secret = client_secret
        self.host = urlsplit(self.config.token_endpoint).hostname
        self.endpoint = self.config.token_endpoint

    def authorize_url(self, state, verifier):
        params = {
            "client_id": self.config.client_id,
            "redirect_uri": self.callback,
            "state": state,
            "response_type": "code",
        }
        if self.config.scopes:
            params["scope"] = " ".join(self.config.scopes)
        if self.config.resource:
            params["resource"] = self.config.resource
        params.update(
            code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode(),
            code_challenge_method="S256",
        )
        params.update(self.config.authorization_params)
        return self.config.authorization_endpoint + "?" + urlencode(params)

    async def exchange(self, code, verifier):
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.callback}
        data["code_verifier"] = verifier
        return await self.request(data)

    async def refresh(self, bundle):
        return await self.request(
            {"grant_type": "refresh_token", "refresh_token": bundle["refresh_token"]},
            previous=bundle,
        )

    async def request(self, data, previous=None):
        try:
            if self.config.resource:
                data = {**data, "resource": self.config.resource}
            headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
            if self.config.token_endpoint_auth_method == "none":
                data = {**data, "client_id": self.config.client_id}
            else:
                client_secret = secret(self.client_secret)
                if self.config.token_endpoint_auth_method == "client_secret_basic":
                    pair = quote_plus(self.config.client_id) + ":" + quote_plus(client_secret)
                    headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
                else:
                    data = {
                        **data,
                        "client_id": self.config.client_id,
                        "client_secret": client_secret,
                    }
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
                    headers=headers,
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
            if response.status_code != 200 or value.get("error"):
                raise ValueError("Token exchange failed")
            access = secret(value.get("access_token"))
            if value.get("token_type", "").lower() != "bearer":
                raise ValueError("Unexpected token type")
            refresh = (
                secret(value["refresh_token"]) if value.get("refresh_token") is not None else None
            )
            if previous is not None and not refresh:
                if self.config.rotating_refresh_tokens:
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
            if scopes is not None and (
                not isinstance(scopes, str)
                or len(scopes) > 4096
                or not re.fullmatch(r"[\x21\x23-\x5b\x5d-\x7e ]*", scopes)
            ):
                raise ValueError("Invalid scope response")
            granted = (
                sorted(set(scopes.split()))
                if scopes is not None
                else (
                    previous.get("granted_scopes") if previous else sorted(set(self.config.scopes))
                )
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
