"""Server-rendered console, using the original console's layout and API."""

from pathlib import Path
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.staticfiles import StaticFiles

from ravn.common import RavnError

ROOT = Path(__file__).parent
TEMPLATES = Environment(
    loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html"])
)
NAV = [
    ("applications", "Applications", "bot"),
    ("connections", "Connections", "database"),
    ("sessions", "Sessions", "key-round"),
    ("calls", "Activity", "activity"),
    ("integrations", "Settings", "settings"),
]
GROUPS = {
    "applications": "applications",
    "connections": "connections",
    "sessions": "sessions",
    "calls": "calls",
    "events": "calls",
    "integrations": "integrations",
    "app-keys": "integrations",
    "application": "integrations",
    "server": "integrations",
}
FILTERS = {
    "connections": ["tenant_id", "user_id", "status"],
    "sessions": ["tenant_id", "user_id", "status", "connection_id"],
    "calls": [
        "tenant_id",
        "user_id",
        "status",
        "connection_id",
        "session_id",
        "tool",
        "from",
        "to",
    ],
    "events": ["tenant_id", "user_id", "kind", "subject_id", "from", "to", "scope"],
    "app-keys": [],
}
STATUSES = {
    "connections": ["active", "disconnected", "reconnect_required"],
    "sessions": ["active", "expired", "revoked"],
    "calls": ["running", "succeeded", "tool_error", "failed", "unknown", "denied"],
}
SUBTITLES = {
    "applications": "Registered applications using this RAVN server.",
    "connections": "User accounts available through RAVN.",
    "sessions": "One token for the accounts attached to each session.",
    "calls": "Recorded calls and management events. No arguments or results.",
    "integrations": "Manage integrations, application keys, and server configuration.",
}


def page_url(**params):
    return "/console/?" + urlencode({k: v for k, v in params.items() if v not in (None, "")})


def add_pages(app, console):
    @app.get("/")
    async def root():
        return RedirectResponse("/console/")

    @app.get("/console/", response_class=HTMLResponse)
    async def page(request: Request):
        p = getattr(request.state, "operator", None)
        context = {"operator": p, "nav": NAV, "url": page_url, "error": None}
        status = 200
        if p:
            query = dict(request.query_params)
            view = query.get("view", "connections")
            boot = await console.bootstrap(p, include_counts=view == "applications")
            if (
                not boot["applications"]
                and view in GROUPS
                and view != "server"
                and not query.get("app_id")
            ):
                view = "applications"
            app_id = query.get(
                "app_id", boot["applications"][0]["id"] if boot["applications"] else ""
            )
            current_app = next((a for a in boot["applications"] if a["id"] == app_id), {})
            group = GROUPS.get(view, "connections")
            _, title, icon = next(n for n in NAV if n[0] == group)
            back = {
                k: v
                for k, v in query.items()
                if k not in {"id", "map", "connection_cursor", "record_tenant"}
            }
            context.update(
                boot=boot,
                view=view,
                group=group,
                app_id=app_id,
                query=query,
                detail=None,
                listing=None,
                choices=None,
                related=None,
                filters=FILTERS.get(view, []),
                statuses=STATUSES.get(view, []),
                title=title,
                page_icon=icon,
                subtitle=SUBTITLES[group],
                multi=current_app.get("tenant_mode") == "multi",
                app_enabled=current_app.get("enabled", False),
                current_app=current_app,
                back_url=page_url(**back),
                auto_refresh=view in {"calls", "events"}
                and not query.get("cursor")
                and not query.get("id"),
            )
            try:
                allowed = {
                    "view",
                    "app_id",
                    "id",
                    "tenant_id",
                    "cursor",
                    "connection_cursor",
                    "map",
                    "record_tenant",
                } | set(FILTERS.get(view, []))
                if (
                    view not in GROUPS
                    or set(query) - allowed
                    or len(query) != len(request.query_params.multi_items())
                    or (not current_app and view not in {"applications", "server"})
                ):
                    raise RavnError(400, "invalid_request", "Invalid page or filters.")
                if view in FILTERS:
                    params = {
                        k: v for k, v in query.items() if k in FILTERS[view] + ["cursor"] and v
                    }
                    if params.get("scope") != "deployment":
                        params["app_id"] = app_id
                    context["listing"] = await console.listing(p, view.replace("-", "_"), params)
                identity = query.get("id")
                if identity:
                    if view in {"connections", "sessions", "calls"}:
                        item = await console.detail(
                            p,
                            view,
                            identity,
                            app_id,
                            query.get("record_tenant", query.get("tenant_id")),
                        )
                    elif view in {"events", "app-keys"}:
                        item = next(
                            (r for r in context["listing"]["data"] if r["id"] == identity), None
                        )
                        if item is None:
                            raise RavnError(
                                404,
                                "not_found",
                                "Record is no longer on this page. Refresh the list.",
                            )
                    else:
                        raise RavnError(400, "invalid_request", "This page has no record details.")
                    context["detail"] = item
                    if view == "connections" and query.get("map") == "1":
                        context["related"] = await console.listing(
                            p,
                            "sessions",
                            {
                                "app_id": app_id,
                                "tenant_id": item["tenant_id"],
                                "connection_id": item["id"],
                                "limit": "10",
                            },
                        )
                    if view == "sessions" and item["status"] == "active":
                        params = {
                            "app_id": app_id,
                            "tenant_id": item["tenant_id"],
                            "user_id": item["user_id"],
                            "status": "active",
                        }
                        if query.get("connection_cursor"):
                            params["cursor"] = query["connection_cursor"]
                        choices = await console.listing(p, "connections", params)
                        attached = {c["id"] for c in item["connections"]}
                        choices["data"] = [
                            c
                            for c in choices["data"]
                            if c["id"] not in attached and c["broker_access"] == "enabled"
                        ]
                        context["choices"] = choices
            except RavnError as exc:
                context["error"], status = exc.message, exc.status
        return HTMLResponse(
            TEMPLATES.get_template("console.html").render(**context), status_code=status
        )

    app.mount("/console/static", StaticFiles(directory=ROOT / "static"), name="console_static")
