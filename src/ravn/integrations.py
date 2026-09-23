"""Operator-owned integration registry, shared by the admin API and console."""

from fastapi import Request
from pydantic import Field, SecretStr, field_validator, model_validator

from ravn.common import RavnError, canonical
from ravn.config import IntegrationSettings, OAuthSettings
from ravn.control import AuditTarget
from ravn.discovery import Discovery, DiscoveryRequest
from ravn.oauth_provider import secret
from ravn.store import event, finish, rows


class OAuthRegistration(OAuthSettings):
    client_secret: SecretStr | None = Field(default=None, min_length=1, max_length=16384)

    @field_validator("client_secret")
    @classmethod
    def validate_secret(cls, value):
        if value is not None:
            secret(value.get_secret_value())
        return value

    @model_validator(mode="after")
    def require_secret(self):
        if self.token_endpoint_auth_method != "none" and self.client_secret is None:
            raise ValueError("This authentication method requires a client secret")
        return self


class IntegrationRegistration(IntegrationSettings):
    oauth: OAuthRegistration | None = None


class Integrations:
    def __init__(self, service):
        self.service = service
        self.items = {}
        self.discovery = Discovery()

    def get(self, app_id, integration_id):
        return self.items.get((app_id, integration_id))

    def public(self, item):
        return item.model_dump(mode="json", exclude={"oauth": {"client_secret"}}) | {
            "oauth_configured": item.oauth is not None,
            "callback_url": self.service.config.server.public_url.rstrip("/")
            + f"/oauth/callback/{item.app_id}/{item.id}"
            if item.oauth
            else None,
        }

    def listing(self):
        return [self.public(self.items[key]) for key in sorted(self.items)]

    async def write(self, db, item):
        value = item.model_dump(mode="json")
        if item.oauth and item.oauth.client_secret:
            value["oauth"]["client_secret"] = item.oauth.client_secret.get_secret_value()
        ciphertext = self.service.cipher.encrypt(
            canonical(value).decode(), item.app_id, "", "integration:" + item.id
        )
        await db.execute(
            "INSERT INTO integrations VALUES(?,?,?) "
            "ON CONFLICT(app_id,id) DO UPDATE SET ciphertext=excluded.ciphertext",
            (item.app_id, item.id, ciphertext),
        )

    async def load(self):
        async with self.service.store.transaction() as db:
            items = {}
            for row in await rows(db, "SELECT * FROM integrations"):
                value = self.service.cipher.decrypt(
                    row["ciphertext"], row["app_id"], "", "integration:" + row["id"]
                )
                items[(row["app_id"], row["id"])] = IntegrationRegistration.model_validate_json(
                    value
                )
        self.items = items

    async def create(self, item, **audit):
        async with self.service.store.transaction() as db:
            if self.service.applications.get(item.app_id) is None:
                raise RavnError(404, "not_found", "Enabled application not found.")
            if self.get(item.app_id, item.id):
                raise RavnError(
                    409, "integration_exists", "Integration ID already exists. Use a new ID."
                )
            await self.write(db, item)
            await event(
                db,
                AuditTarget(item.app_id),
                "integration.created",
                item.id,
                **(audit or {"actor_kind": "local_operator", "actor_id": "unix-owner"}),
            )
        self.items[(item.app_id, item.id)] = item
        return self.public(item)

    async def disable(self, app_id, integration_id, **audit):
        async with self.service.store.transaction() as db:
            item = self.get(app_id, integration_id)
            if item is None:
                raise RavnError(404, "not_found", "Integration not found.")
            if item.enabled:
                item = item.model_copy(update={"enabled": False})
                await self.write(db, item)
                await event(db, AuditTarget(app_id), "integration.disabled", item.id, **audit)
        self.items[(app_id, integration_id)] = item
        return self.public(item)


def add_integration_routes(app, prefix, service, console=None):
    def audit(request):
        if console:
            console.ensure(request.state.operator)
        return {
            "actor_kind": "local_operator",
            "actor_id": request.state.operator.id if console else "admin_socket",
            "request_id": request.state.request_id,
        }

    @app.get(prefix + "/integrations")
    async def listing(request: Request):
        audit(request)
        return {"data": service.integrations.listing()}

    @app.post(prefix + "/integrations", status_code=201)
    async def create(body: IntegrationRegistration, request: Request):
        # Complete both the commit and cache publication even if the caller disconnects.
        return await finish(service.integrations.create(body, **audit(request)))

    @app.post(prefix + "/integrations/discover")
    async def discover(body: DiscoveryRequest, request: Request):
        audit(request)
        return await service.integrations.discovery.inspect(body.endpoint)

    @app.post(prefix + "/integrations/{app_id}/{integration_id}/disable")
    async def disable(app_id: str, integration_id: str, request: Request):
        return await finish(service.integrations.disable(app_id, integration_id, **audit(request)))
