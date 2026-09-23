"""Strict, operator-owned integration configuration. Callers cannot choose destinations."""

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


def https_endpoint(value: str) -> str:
    url = urlsplit(value)
    if (
        len(value) > 2048
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
        or "\\" in value
        or url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.port not in {None, 443}
        or url.query
        or url.fragment
    ):
        raise ValueError(
            "Provider endpoints must be HTTPS URLs on port 443 without credentials, query or fragment"
        )
    return value


class IdentityConfig(Model):
    endpoint: str
    id_field: str = Field(default="sub", min_length=1, max_length=128)
    display_field: str = Field(default="email", min_length=1, max_length=128)
    required_claims: dict[str, str | bool] = Field(default_factory=dict, max_length=16)

    @model_validator(mode="after")
    def validate_endpoint(self):
        https_endpoint(self.endpoint)
        return self


class OAuthSettings(Model):
    client_id: str = Field(min_length=1, max_length=512)
    authorization_endpoint: str
    token_endpoint: str
    issuer: str
    resource: str | None = None
    scopes: list[str] = Field(default_factory=list, max_length=64)
    authorization_params: dict[str, str] = Field(default_factory=dict, max_length=16)
    token_endpoint_auth_method: Literal["client_secret_post", "client_secret_basic", "none"] = (
        "client_secret_post"
    )
    rotating_refresh_tokens: bool = False

    @model_validator(mode="after")
    def validate_oauth(self):
        for value in (self.authorization_endpoint, self.token_endpoint, self.issuer):
            https_endpoint(value)
        if self.resource:
            https_endpoint(self.resource)
        if any(ord(c) < 33 or ord(c) > 126 for c in self.client_id):
            raise ValueError("Invalid OAuth client ID")
        if any(
            not scope or any(ord(c) < 33 or ord(c) > 126 or c in '\\"' for c in scope)
            for scope in self.scopes
        ):
            raise ValueError("Scopes must be nonempty OAuth scope strings")
        reserved = {
            "client_id",
            "client_secret",
            "redirect_uri",
            "state",
            "response_type",
            "scope",
            "code_challenge",
            "code_challenge_method",
            "code_verifier",
            "resource",
        }
        if reserved & self.authorization_params.keys():
            raise ValueError(
                "Extra authorization parameters cannot override OAuth security parameters"
            )
        if any(len(k) > 128 or len(v) > 2048 for k, v in self.authorization_params.items()):
            raise ValueError("OAuth authorization parameter exceeds the limit")
        return self


class IntegrationSettings(Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    app_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    transport: Literal["remote_mcp"] = "remote_mcp"
    endpoint: str
    identity: IdentityConfig | None = None
    enabled: bool = True
    oauth: OAuthSettings | None = None

    @model_validator(mode="after")
    def validate_endpoint(self):
        https_endpoint(self.endpoint)
        return self


class Limits(Model):
    request_bytes: int = Field(default=1048576, ge=1024, le=1048576)
    result_bytes: int = Field(default=4194304, ge=1024, le=4194304)
    call_timeout_seconds: int = Field(default=30, ge=1, le=120)


class Config(Model):
    version: Literal[2] = 2
    deployment_id: str = Field(min_length=1, max_length=128)
    server: ServerConfig
    storage: StorageConfig
    secrets: SecretsConfig
    sessions: SessionConfig = SessionConfig()
    limits: Limits = Limits()

    @model_validator(mode="after")
    def validate_namespace(self):
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
