"""Local setup and owner-only administration; secrets are files, never argv values."""

import argparse
import asyncio
import base64
import contextlib
import json
import logging
import os
import threading
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx
import uvicorn
import yaml

from ravn.app import create_admin_app, create_app
from ravn.approvals import clean, render_arguments
from ravn.common import RavnError, canonical
from ravn.config import load_config
from ravn.crypto import private_file
from ravn.github import GitHub
from ravn.gmail import Gmail
from ravn.gmail_api import GmailApi
from ravn.manifest import check_schema, schema_hash, schemas_for
from ravn.slack import Slack


def private_write(path: Path, content: str):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())


def initialize(args):
    directory = args.directory.resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    (directory / "state").mkdir(mode=0o700)
    (directory / "run").mkdir(mode=0o700)
    key = directory / "master.key"
    private_write(key, base64.b64encode(os.urandom(32)).decode() + "\n")
    config = {
        "version": 1,
        "deployment_id": "local-development",
        "server": {
            "listen": f"127.0.0.1:{args.port}",
            "public_url": f"http://127.0.0.1:{args.port}",
            "admin_socket": str(directory / "run/admin.sock"),
        },
        "storage": {"sqlite_path": str(directory / "state/ravn.db")},
        "secrets": {
            "managed": {
                "type": "encrypted_sqlite",
                "active_key_id": "local-v1",
                "key_source": {"type": "file", "path": str(key)},
            }
        },
        "applications": [
            {
                "id": args.app,
                "tenant_mode": args.tenant_mode,
                "return_urls": ["http://127.0.0.1:8800/return"],
            }
        ],
        "integrations": [{"id": "github", "app_id": args.app, "schema_hashes": {}}],
    }
    path = directory / "ravn.yaml"
    private_write(path, yaml.safe_dump(config, sort_keys=False))
    load_config(path)
    print(f"Created {path}. Review GitHub schema pins before issuing sessions.")


class AuxiliaryServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        # The public server is the sole process signal owner.
        yield


class AdminServer(AuxiliaryServer):
    async def startup(self, sockets=None):
        await super().startup(sockets)
        # Uvicorn defaults Unix sockets to 0666 regardless of our restrictive umask.
        os.chmod(self.config.uds, 0o600)


async def serve(config, *, console_enabled=False, console_port=8788):
    from ravn.console import STATIC, Console, create_console_app

    app = create_app(config)
    console = Console(app.state.service, console_port) if console_enabled else None
    if console and not (STATIC / "index.html").is_file():
        raise ValueError("Build the console assets first")
    host, port = config.server.listen.rsplit(":", 1)
    socket = config.server.admin_socket
    socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if socket.parent.stat().st_uid != os.getuid() or socket.parent.stat().st_mode & 0o077:
        raise ValueError("Admin socket directory must be owner-only")
    if socket.exists() and not socket.is_socket():
        raise ValueError("Refusing to replace a non-socket admin path")
    # Acquire the DB deployment lock before either listener can replace an old socket.
    async with app.router.lifespan_context(app):
        public = uvicorn.Server(
            uvicorn.Config(
                app,
                host=host,
                port=int(port),
                workers=1,
                lifespan="off",
                access_log=False,
                log_level="warning",
                proxy_headers=False,
                timeout_graceful_shutdown=30,
                limit_concurrency=128,
            )
        )
        admin = AdminServer(
            uvicorn.Config(
                create_admin_app(app.state.service, console),
                uds=str(socket),
                workers=1,
                lifespan="off",
                access_log=False,
                log_level="warning",
                proxy_headers=False,
                timeout_graceful_shutdown=30,
            )
        )
        servers = [public, admin]
        if console:
            servers.append(
                AuxiliaryServer(
                    uvicorn.Config(
                        create_console_app(console),
                        host="127.0.0.1",
                        port=console.port,
                        workers=1,
                        lifespan="off",
                        access_log=False,
                        log_level="warning",
                        proxy_headers=False,
                        timeout_graceful_shutdown=30,
                        limit_concurrency=64,
                    )
                )
            )
        tasks = [asyncio.create_task(server.serve()) for server in servers]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for server in servers:
                server.should_exit = True
            await asyncio.gather(*tasks, return_exceptions=True)
            if console:
                console.close()


async def admin_request(config, path, payload=None, method="POST", seconds=10):
    async with httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=str(config.server.admin_socket)),
        base_url="http://ravn-admin",
        trust_env=False,
        timeout=seconds,
    ) as client:
        response = await client.request(method, path, json=payload if method != "GET" else None)
        if response.status_code in {404, 409}:
            # Fixed server strings, never upstream text, so they are safe to show.
            error = response.json().get("error")
            if not isinstance(error, dict):
                raise RavnError(404, "not_found", "Not found; is the broker up to date?")
            raise RavnError(response.status_code, error["code"], error["message"])
        if response.is_error:
            raise ValueError("Operator request failed")
        return response.json()


def render(item) -> str:
    def field(key):
        return clean(str(item.get(key)), 256)

    try:
        stamp = datetime.now(UTC)
        age = int((stamp - datetime.fromisoformat(item["created_at"])).total_seconds())
        left = int((datetime.fromisoformat(item["expires_at"]) - stamp).total_seconds())
        timing = f"{max(age, 0)}s ago, {max(left, 0)}s left"
    except (KeyError, TypeError, ValueError):
        timing = "unknown"
    return "\n".join(
        [
            f"Approval {field('id')}",
            f"  tool:       {field('tool')}",
            f"  app:        {field('app_id')} (tenant {field('tenant_id')})",
            f"  user:       {field('user_id')}",
            f"  session:    {field('session_id')}",
            f"  connection: {field('connection_id')}",
            f"  requested:  {timing}",
            "  arguments:",
            *render_arguments(item.get("arguments")),
        ]
    )


async def prompt(text):
    """Read a line without tying shutdown to a blocked worker thread."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def settle(outcome, value):
        if not future.done():
            future.set_exception(value) if outcome == "error" else future.set_result(value)

    def read():
        try:
            value = input(text)
        except BaseException as error:
            loop.call_soon_threadsafe(settle, "error", error)
        else:
            loop.call_soon_threadsafe(settle, "ok", value)

    threading.Thread(target=read, daemon=True).start()
    return await future


async def approvals_command(config, args):
    if args.action in {"approve", "deny"}:
        await admin_request(
            config,
            f"/admin/v1/approvals/{quote(args.id, safe='')}/decision",
            {"decision": args.action},
        )
        print(f"{'Approved' if args.action == 'approve' else 'Denied'} {clean(args.id, 256)}.")
        return
    if args.action == "list":
        result = await admin_request(config, "/admin/v1/approvals?wait=0", method="GET")
        if not result["data"]:
            print("No pending approvals.")
        for item in result["data"]:
            print(render(item) + "\n")
        return
    print("Waiting for write approvals. Press Ctrl-C to stop; undecided requests stay pending.")
    version, seen = None, set()
    while True:
        result = await admin_request(
            config,
            "/admin/v1/approvals?wait=25" + (f"&after={version}" if version is not None else ""),
            method="GET",
            seconds=40,
        )
        version = result["version"]
        seen &= {item["id"] for item in result["data"]}
        for item in result["data"]:
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            print("\a" + render(item))
            try:
                choice = (await prompt("[a]pprove / [d]eny / [s]kip: ")).strip().lower()
            except EOFError:
                return
            if choice not in {"a", "d"}:
                continue
            decision = "approve" if choice == "a" else "deny"
            try:
                await admin_request(
                    config,
                    f"/admin/v1/approvals/{quote(item['id'], safe='')}/decision",
                    {"decision": decision},
                )
                print("Approved.\n" if decision == "approve" else "Denied.\n")
            except RavnError:
                print("Already decided or expired.\n")


def render_grant(item) -> str:
    def field(key):
        return clean(str(item.get(key)), 256)

    status = (
        f"revoked {field('revoked_at')} ({field('revoke_reason')})"
        if item.get("revoked_at")
        else "active"
    )
    return "\n".join(
        [
            f"Grant {field('id')}",
            f"  client:     {clean(str(item.get('client_name') or 'unnamed'), 128)} ({field('client_id')})",
            f"  app:        {field('app_id')} (tenant {field('tenant_id')})",
            f"  user:       {field('user_id')}",
            f"  connection: {field('connection_id')}",
            f"  created:    {field('created_at')}",
            f"  expires:    {field('expires_at')}",
            f"  status:     {status}",
            f"  refreshes:  {field('rotation')}, live sessions: {field('active_sessions')}",
        ]
    )


async def grants_command(config, args):
    if args.action == "revoke":
        await admin_request(config, f"/admin/v1/oauth-grants/{quote(args.id, safe='')}/revoke")
        print(f"Revoked grant {clean(args.id, 256)}.")
        return
    result = await admin_request(
        config, "/admin/v1/oauth-grants?all=" + ("1" if args.all else "0"), method="GET"
    )
    if not result["items"]:
        print("No agent grants." if args.all else "No active agent grants.")
    for item in result["items"]:
        print(render_grant(item) + "\n")


async def backend_request(config, args, method, path, payload=None):
    headers = {
        "Authorization": "Bearer " + private_file(args.app_key_file).decode(),
        "X-Ravn-User-Id": args.user,
    }
    if args.tenant:
        headers["X-Ravn-Tenant-Id"] = args.tenant
    async with httpx.AsyncClient(
        base_url=config.server.public_url,
        headers=headers,
        trust_env=False,
        follow_redirects=False,
        timeout=35,
    ) as client:
        response = await client.request(method, path, json=payload)
        if response.is_error:
            raise ValueError(
                "Backend request failed; inspect the connection or server configuration"
            )
        return response.json()


async def run(args):
    if args.command == "init":
        initialize(args)
        return
    config = load_config(args.config)
    if args.command == "serve":
        await serve(config, console_enabled=args.console, console_port=args.console_port)
        return
    if args.command == "console":
        result = await admin_request(config, "/admin/v1/console-tickets", {})
        if args.no_open or not webbrowser.open(result["url"]):
            print(result["url"])
        else:
            print("Opened the local operator console. Login link expires in 60 seconds.")
        return
    if args.command == "config-check":
        print("Configuration is valid; no external requests were made.")
        return
    if args.command == "approvals":
        await approvals_command(config, args)
        return
    if args.command == "grants":
        await grants_command(config, args)
        return
    if args.command == "integration-inspect":
        if args.connection:
            if not args.app_key_file or not args.user:
                raise ValueError("Connection inspection needs --app-key-file and --user")
            result = await backend_request(
                config, args, "GET", f"/v1/connections/{args.connection}/tool-schemas"
            )
            if args.output:
                private_write(args.output, json.dumps(result, indent=2) + "\n")
                print(f"Saved schemas for review to {args.output}; no pins were changed.")
            else:
                print(json.dumps(result, indent=2))
            return
        integration = config.integration(args.app, args.integration)
        if integration is None or not integration.enabled:
            raise ValueError("Integration not found or disabled")
        credential = private_file(args.token_file).decode()
        provider = {"slack": Slack, "gmail": Gmail, "gmail_api": GmailApi}.get(
            integration.connector, GitHub
        )(config.limits.call_timeout_seconds, config.limits.result_bytes)
        await provider.identify(credential)
        tools = await provider.inspect(credential)
        result = {"schema_hashes": {}, "schemas_for_review": {}}
        for tool in tools:
            if tool.name in schemas_for(integration.connector):
                check_schema(tool.input_schema)
                if tool.output_schema is not None:
                    check_schema(tool.output_schema)
                result["schema_hashes"][tool.name] = schema_hash(tool)
                result["schemas_for_review"][tool.name] = {
                    "input": tool.input_schema,
                    "output": tool.output_schema,
                }
        if credential in canonical(result).decode():
            raise ValueError("Unsafe upstream schema was withheld")
    elif args.command == "app-key":
        if args.action == "create":
            result = await admin_request(
                config, "/admin/v1/app-keys", {"app_id": args.app, "label": args.label}
            )
        else:
            result = await admin_request(
                config,
                f"/admin/v1/app-keys/{args.id}/revoke",
                {"revoke_sessions": args.revoke_sessions},
            )
    elif args.command == "connections":
        if args.action == "connect":
            from ravn.connect_cli import connect

            result = await connect(config, args, backend_request)
        elif args.action == "import":
            result = await backend_request(
                config,
                args,
                "POST",
                "/v1/connections/import",
                {
                    "integration_id": args.integration,
                    "credential": {
                        "type": "bearer",
                        "token": private_file(args.token_file).decode(),
                    },
                },
            )
        elif args.action == "list":
            result = await backend_request(config, args, "GET", "/v1/connections")
        else:
            result = await backend_request(
                config, args, "POST", f"/v1/connections/{args.id}/disconnect"
            )
    elif args.command == "sessions":
        if args.action == "create":
            payload = {"connection_id": args.connection}
            if args.ttl is not None:
                payload["ttl_seconds"] = args.ttl
            result = await backend_request(config, args, "POST", "/v1/sessions", payload)
        elif args.action == "list":
            result = await backend_request(config, args, "GET", "/v1/sessions")
        else:
            result = await backend_request(config, args, "POST", f"/v1/sessions/{args.id}/revoke")
    elif args.command == "connect-sessions":
        result = await backend_request(
            config,
            args,
            "POST" if args.action == "cancel" else "GET",
            f"/v1/connect-sessions/{args.id}" + ("/cancel" if args.action == "cancel" else ""),
        )
    else:
        result = await backend_request(config, args, "GET", f"/v1/calls/{args.id}")
    if getattr(args, "output", None):
        # Store a raw app key for backend clients; sessions remain structured MCP config.
        content = (
            result["key"]
            if args.command == "app-key" and args.action == "create"
            else json.dumps(result, indent=2)
        )
        private_write(args.output, content + "\n")
        print(f"Saved owner-only output to {args.output}; no existing file was overwritten.")
    else:
        print(json.dumps(result, indent=2))


def parser():
    root = argparse.ArgumentParser(description="RAVN: OAuth connections and controlled MCP access")
    sub = root.add_subparsers(dest="command", required=True)
    init = sub.add_parser(
        "init", help="Create owner-only local development state; refuses overwrite"
    )
    init.add_argument("--directory", type=Path, default=Path(".ravn"))
    init.add_argument("--app", default="demo")
    init.add_argument("--tenant-mode", choices=["single", "multi"], default="single")
    init.add_argument("--port", type=int, default=8787)

    def config(p):
        p.add_argument("--config", type=Path, default=Path(".ravn/ravn.yaml"))

    def actor(p):
        config(p)
        p.add_argument("--app-key-file", type=Path, required=True)
        p.add_argument("--user", required=True)
        p.add_argument("--tenant")

    for name in ("serve", "config-check"):
        command = sub.add_parser(name)
        config(command)
        if name == "serve":
            command.add_argument(
                "--console", action="store_true", help="Enable the local-only operator console"
            )
            command.add_argument("--console-port", type=int, default=8788)
    console = sub.add_parser("console", help="Sign in through the owner-only admin socket")
    config(console)
    console.add_argument(
        "--no-open",
        action="store_true",
        help="Print the short-lived sign-in link instead of opening it",
    )
    approve = sub.add_parser("approvals", help="Approve or deny pending writes")
    config(approve)
    acts = approve.add_subparsers(dest="action")
    # Accepted after the action too; SUPPRESS keeps a child default from erasing the parent's.
    for action in ("list", "approve", "deny"):
        child = acts.add_parser(action)
        child.add_argument("--config", type=Path, default=argparse.SUPPRESS)
        if action != "list":
            child.add_argument("id")
    grants = sub.add_parser("grants", help="List or revoke agent (MCP OAuth) grants")
    config(grants)
    grant_acts = grants.add_subparsers(dest="action", required=True)
    for action in ("list", "revoke"):
        child = grant_acts.add_parser(action)
        child.add_argument("--config", type=Path, default=argparse.SUPPRESS)
        if action == "list":
            child.add_argument("--all", action="store_true", help="Include revoked and expired")
        else:
            child.add_argument("id")
    inspect = sub.add_parser(
        "integration-inspect", help="Inspect candidate schemas; does not approve or save pins"
    )
    config(inspect)
    inspect.add_argument("--app")
    inspect.add_argument("--integration", default="github")
    inspection_source = inspect.add_mutually_exclusive_group(required=True)
    inspection_source.add_argument("--token-file", type=Path)
    inspection_source.add_argument("--connection")
    inspect.add_argument("--app-key-file", type=Path)
    inspect.add_argument("--user")
    inspect.add_argument("--tenant")
    inspect.add_argument("--output", type=Path)
    keys = sub.add_parser("app-key").add_subparsers(dest="action", required=True)
    create = keys.add_parser("create")
    config(create)
    create.add_argument("--app", required=True)
    create.add_argument("--label", default="backend")
    create.add_argument("--output", type=Path, help="Write a raw key to a new owner-only file")
    revoke = keys.add_parser("revoke")
    config(revoke)
    revoke.add_argument("id")
    revoke.add_argument("--revoke-sessions", action="store_true")
    for kind in ("connections", "sessions"):
        commands = sub.add_parser(kind).add_subparsers(dest="action", required=True)
        if kind == "connections":
            connect = commands.add_parser(
                "connect", help="Authorize through a browser-bound loopback helper"
            )
            actor(connect)
            connect.add_argument("--integration", default="github")
            connect.add_argument("--return-url", default="http://127.0.0.1:8800/return")
            connect.add_argument("--reconnect", help="Existing same-account connection ID")
            connect.add_argument("--no-open", action="store_true")

        actor(commands.add_parser("list"))
        revoke = commands.add_parser("disconnect" if kind == "connections" else "revoke")
        actor(revoke)
        revoke.add_argument("id")
        create = commands.add_parser("import" if kind == "connections" else "create")
        actor(create)
        if kind == "connections":
            create.add_argument("--token-file", type=Path, required=True)
            create.add_argument("--integration", default="github")
        else:
            create.add_argument("--connection", required=True)
            create.add_argument("--ttl", type=int)
            create.add_argument("--output", type=Path)
    show = sub.add_parser("calls").add_subparsers(dest="action", required=True).add_parser("show")
    actor(show)
    show.add_argument("id")
    flows = sub.add_parser("connect-sessions").add_subparsers(dest="action", required=True)
    for action in ("show", "cancel"):
        command = flows.add_parser(action)
        actor(command)
        command.add_argument("id")
    return root


def main():
    os.umask(0o077)
    # The SDK can log peer data at debug/error levels. No upstream payload logging.
    for name in ("mcp", "httpx", "httpx2", "httpcore", "httpcore2"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL)
    args = parser().parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except RavnError as error:
        print(f"{error.code}: {error.message}")
        raise SystemExit(1) from None
    except Exception:
        # Third-party exception messages can contain request credentials.
        print(
            "Operation failed. Check configuration, file permissions, and server availability. Details withheld to protect secrets."
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
