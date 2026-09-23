"""Applications managed by the deployment owner and persisted in SQLite."""

import json

from fastapi import Request
from pydantic import Field

from ravn.common import RavnError, canonical
from ravn.config import Application, Model
from ravn.control import AuditTarget
from ravn.store import event, finish, rows


class KeyRequest(Model):
    app_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    label: str = Field(default="backend", min_length=1, max_length=128)


class Applications:
    def __init__(self, service):
        self.service = service
        self.items = {}

    def get(self, app_id):
        item = self.items.get(app_id)
        return item if item and item.enabled else None

    async def load(self):
        async with self.service.store.transaction() as db:
            self.items = {
                row["id"]: Application.model_validate(
                    {**row, "return_urls": json.loads(row["return_urls"])}
                )
                for row in await rows(db, "SELECT * FROM applications")
            }

    async def save(self, item, *, create=False, **audit):
        async with self.service.store.transaction() as db:
            old = self.items.get(item.id)
            if create and old:
                raise RavnError(409, "application_exists", "Application ID already exists.")
            if not create and old is None:
                raise RavnError(404, "not_found", "Application not found.")
            if old and old.tenant_mode != item.tenant_mode:
                raise RavnError(409, "invalid_request", "Tenant mode cannot change after creation.")
            await db.execute(
                "INSERT INTO applications(id,tenant_mode,enabled,return_urls) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled,return_urls=excluded.return_urls",
                (item.id, item.tenant_mode, item.enabled, canonical(item.return_urls).decode()),
            )
            await event(
                db,
                AuditTarget(item.id),
                "application.created" if create else "application.updated",
                item.id,
                **(audit or {"actor_kind": "local_operator", "actor_id": "unix-owner"}),
            )
        self.items[item.id] = item
        return item.model_dump(mode="json")


def add_application_routes(app, prefix, service, console=None):
    def audit(request):
        if console:
            console.ensure(request.state.operator)
        return {
            "actor_kind": "local_operator",
            "actor_id": request.state.operator.id if console else "admin_socket",
            "request_id": request.state.request_id,
        }

    @app.get(prefix + "/applications")
    async def listing(request: Request):
        audit(request)
        return {
            "data": [
                service.applications.items[k].model_dump(mode="json")
                for k in sorted(service.applications.items)
            ]
        }

    @app.post(prefix + "/applications", status_code=201)
    async def create(body: Application, request: Request):
        return await finish(service.applications.save(body, create=True, **audit(request)))

    @app.put(prefix + "/applications/{app_id}")
    async def update(app_id: str, body: Application, request: Request):
        context = audit(request)
        if body.id != app_id:
            raise RavnError(400, "invalid_request", "Application ID must match the URL.")
        return await finish(service.applications.save(body, **context))

    @app.post(prefix + "/app-keys", status_code=201)
    async def create_key(body: KeyRequest, request: Request):
        return await finish(service.create_key(body.app_id, body.label, **audit(request)))
