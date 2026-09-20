"""Strict, operator-owned configuration. No dynamic provider destinations."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServerConfig(Model):
    listen: str = "127.0.0.1:8787"
    public_url: str = "http://127.0.0.1:8787"
    admin_socket: Path

    @model_validator(mode="after")
    def validate_origin(self):
        url = urlsplit(self.public_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("public_url must be an origin without credentials or a path")
        if url.scheme == "http" and url.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("Non-loopback public_url requires HTTPS")
        host, port = self.listen.rsplit(":", 1)
        if host not in {"127.0.0.1", "0.0.0.0"} or not 1 <= int(port) <= 65535:
            raise ValueError("listen must use an IPv4 loopback or wildcard address and port")
        if url.scheme == "http" and host != "127.0.0.1":
            raise ValueError("Development HTTP must bind loopback")
        return self


class StorageConfig(Model):
    sqlite_path: Path


class SessionConfig(Model):
    default_ttl_seconds: int = Field(default=3600, ge=60, le=14400)
    max_ttl_seconds: int = Field(default=14400, ge=60, le=14400)

    @model_validator(mode="after")
    def validate_default(self):
        if self.default_ttl_seconds > self.max_ttl_seconds:
            raise ValueError("Default session lifetime exceeds the maximum")
        return self


class KeySource(Model):
    type: Literal["file"] = "file"
    path: Path


class ManagedSecrets(Model):
    type: Literal["encrypted_sqlite"] = "encrypted_sqlite"
    active_key_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    key_source: KeySource


class SecretsConfig(Model):
    managed: ManagedSecrets


class Application(Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    tenant_mode: Literal["single", "multi"] = "single"
    enabled: bool = True
    return_urls: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_returns(self):
        for value in self.return_urls:
            url = urlsplit(value)
            if (
                len(value) > 2048
                or any(ord(c) < 33 or ord(c) > 126 for c in value)
                or "\\" in value
                or url.scheme not in {"https", "http"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or (url.scheme == "http" and url.hostname not in {"127.0.0.1", "localhost"})
            ):
                raise ValueError(
                    "Return URLs must be exact HTTPS URLs (HTTP loopback for development), without query or fragment"
                )
        return self


GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
SLACK_SCOPES = {
    "search:read.public",
    "search:read.private",
    "search:read.im",
    "search:read.mpim",
    "channels:history",
    "groups:history",
    "im:history",
    "mpim:history",
}
# Each connector is one fixed provider profile; configuration selects a row and
# can never point a connector at another destination or OAuth profile.
CONNECTORS = {
    "github_cloud": {
        "endpoint": "https://api.githubcopilot.com/mcp/",
        "manifest": "builtin:github-issues-v1",
        "oauth_profile": "github_app_pkce",
    },
    "slack": {
        "endpoint": "https://mcp.slack.com/mcp",
        "manifest": "builtin:slack-read-v1",
        "oauth_profile": "slack_user_confidential",
    },
    "gmail": {
        "endpoint": "https://gmailmcp.googleapis.com/mcp/v1",
        "manifest": "builtin:gmail-v1",
        "oauth_profile": "google_web_pkce",
    },
    # A self-hosted stand-in for Google's own gmailmcp.googleapis.com, gated
    # identically (same reviewed tool set, same OAuth profile) -- it exists
    # only because Google's hosted server requires Workspace Developer
    # Preview enrollment. See examples/gmail_agent/README.md.
    "gmail_local": {
        "endpoint": "http://127.0.0.1:8790/mcp",
        "manifest": "builtin:gmail-read-v1",
        "oauth_profile": "google_web_pkce",
    },
}


class OAuthConfig(Model):
    client_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    client_secret_file: Path
    # GitHub requires S256. Slack's confidential server profile does not enable
    # its irreversible public-client PKCE setting; this is not an auto-downgrade.
    # Google web clients are confidential and also use S256.
    profile: Literal["github_app_pkce", "slack_user_confidential", "google_web_pkce"]
    scopes: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_secret(self):
        if not self.client_secret_file.is_absolute():
            raise ValueError("OAuth client secret path must be absolute")
        return self


class Integration(Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    app_id: str
    connector: Literal["github_cloud", "slack", "gmail", "gmail_local"] = "github_cloud"
    transport: Literal["remote_mcp"] = "remote_mcp"
    endpoint: Literal[
        "https://api.githubcopilot.com/mcp/",
        "https://mcp.slack.com/mcp",
        "https://gmailmcp.googleapis.com/mcp/v1",
        "http://127.0.0.1:8790/mcp",
    ] = "https://api.githubcopilot.com/mcp/"
    enabled: bool = True
    manifest: Literal[
        "builtin:github-issues-v1",
        "builtin:slack-read-v1",
        "builtin:gmail-v1",
        "builtin:gmail-read-v1",
    ] = "builtin:github-issues-v1"
    schema_hashes: dict[str, str] = Field(default_factory=dict)
    oauth: OAuthConfig | None = None

    @model_validator(mode="after")
    def validate_pins(self):
        import re

        from ravn.manifest import schemas_for

        profile = CONNECTORS[self.connector]
        if self.endpoint != profile["endpoint"] or self.manifest != profile["manifest"]:
            raise ValueError(
                "Connector, endpoint, and manifest must match the fixed provider profile"
            )
        if not set(self.schema_hashes) <= set(schemas_for(self.connector)):
            raise ValueError("Only the connector's reviewed tools may be pinned")
        if any(not re.fullmatch(r"[a-f0-9]{64}", v) for v in self.schema_hashes.values()):
            raise ValueError("Schema pins must be SHA-256 hex digests")
        if self.oauth:
            if self.oauth.profile != profile["oauth_profile"]:
                raise ValueError("OAuth profile must match the connector")
            scopes = set(self.oauth.scopes)
            if self.connector == "gmail":
                required = {"openid", "email", GMAIL_READONLY}
                if not required <= scopes or not scopes <= required | {GMAIL_COMPOSE}:
                    raise ValueError(
                        "Gmail needs openid, email, and gmail.readonly; only gmail.compose may be added"
                    )
                # Compose may be granted before create_draft is reviewed, so onboarding
                # does not need a second consent; approving the write requires it.
                if "create_draft" in self.schema_hashes and GMAIL_COMPOSE not in scopes:
                    raise ValueError("Approving create_draft requires the gmail.compose scope")
            elif self.connector == "gmail_local":
                # No write tool is ever reviewed for this connector; compose is never needed.
                required = {"openid", "email", GMAIL_READONLY}
                if scopes != required:
                    raise ValueError("gmail_local needs exactly openid, email, and gmail.readonly")
            elif not scopes <= (SLACK_SCOPES if self.connector == "slack" else set()) or (
                self.connector == "slack" and not scopes
            ):
                raise ValueError(
                    "Configure only reviewed Slack read scopes; GitHub App permissions are configured at GitHub"
                )
        return self


class Limits(Model):
    request_bytes: int = Field(default=1048576, ge=1024, le=1048576)
    result_bytes: int = Field(default=4194304, ge=1024, le=4194304)
    call_timeout_seconds: int = Field(default=30, ge=1, le=120)


class Config(Model):
    version: Literal[1] = 1
    deployment_id: str = Field(min_length=1, max_length=128)
    server: ServerConfig
    storage: StorageConfig
    secrets: SecretsConfig
    sessions: SessionConfig = SessionConfig()
    limits: Limits = Limits()
    applications: list[Application] = Field(min_length=1)
    integrations: list[Integration] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_namespace(self):
        apps = [x.id for x in self.applications]
        integrations = [(x.app_id, x.id) for x in self.integrations]
        if len(set(apps)) != len(apps) or len(set(integrations)) != len(integrations):
            raise ValueError("Duplicate application or integration")
        if any(x.app_id not in apps for x in self.integrations):
            raise ValueError("Integration references an unknown application")
        paths = [
            self.server.admin_socket,
            self.storage.sqlite_path,
            self.secrets.managed.key_source.path,
        ]
        if any(not path.is_absolute() for path in paths):
            raise ValueError("Storage, key, and socket paths must be absolute")
        if len(set(paths)) != len(paths):
            raise ValueError("Storage, key, and socket paths must be distinct")
        return self

    def app(self, app_id: str) -> Application | None:
        return next((a for a in self.applications if a.id == app_id and a.enabled), None)

    def integration(self, app_id: str, integration_id: str) -> Integration | None:
        return next(
            (i for i in self.integrations if i.app_id == app_id and i.id == integration_id), None
        )


def load_config(path: Path) -> Config:
    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node):
        result = {}
        for key, value in node.value:
            key = loader.construct_object(key)
            if key in result:
                raise ValueError("Duplicate config key")
            result[key] = loader.construct_object(value)
        return result

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    if path.stat().st_size > 1048576:
        raise ValueError("Config is too large")
    return Config.model_validate(yaml.load(path.read_text(), Loader=UniqueLoader))
