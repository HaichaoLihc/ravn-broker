"""Run from broker/: python -m examples.support_desk --demo (or --live)."""

import argparse
import asyncio
import base64
import contextlib
import json
import logging
import os
import secrets
import socket
import tempfile
import webbrowser
from contextlib import AsyncExitStack
from pathlib import Path

import uvicorn

from ravn.crypto import private_file

from .app import Desk, create_app


def private_write(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(text)


@contextlib.asynccontextmanager
async def simulated_ravn(directory, app_origin, ravn_origin, consent_origin, *, resume=False):
    # Demo-only dependencies never enter the developer app's live code path.
    from ravn.app import create_app as broker_app
    from ravn.config import Config
    from ravn.github import GitHub
    from ravn.manifest import schema_hash
    from ravn.slack import Slack
    from ravn.store import rows

    from .simulated import SimulatedOAuth, SimulatedProvider, consent_app

    if resume:
        info = directory.stat()
        if (
            info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or not (directory / "ravn.db").is_file()
        ):
            raise ValueError("Resume requires an owner-only existing demo directory")
        secret = private_file(directory / "oauth.secret").decode()
        if not secret.startswith("simulated-client-"):
            raise ValueError("Only simulated-provider deployments can be resumed here")
    else:
        secret = "simulated-client-" + secrets.token_urlsafe(24)
        private_write(directory / "master.key", base64.b64encode(os.urandom(32)).decode())
        private_write(directory / "oauth.secret", secret)
    providers = {kind: SimulatedProvider(kind, secret) for kind in ("github", "slack")}
    integrations = []
    for kind, provider in providers.items():
        integrations.append(
            {
                "id": kind,
                "app_id": "support-desk",
                "connector": "slack" if kind == "slack" else "github_cloud",
                "endpoint": "https://mcp.slack.com/mcp"
                if kind == "slack"
                else "https://api.githubcopilot.com/mcp/",
                "manifest": "builtin:slack-read-v1"
                if kind == "slack"
                else "builtin:github-issues-v1",
                "schema_hashes": {t.name: schema_hash(t) for t in provider.tools},
                "oauth": {
                    "client_id": "simulated-" + kind,
                    "client_secret_file": str(directory / "oauth.secret"),
                    "profile": "slack_user_confidential" if kind == "slack" else "github_app_pkce",
                    "scopes": [
                        "search:read.public",
                        "search:read.private",
                        "channels:history",
                        "groups:history",
                    ]
                    if kind == "slack"
                    else [],
                },
            }
        )
    config = Config.model_validate(
        {
            "deployment_id": "support-desk-simulation",
            "server": {
                "listen": "127.0.0.1:" + ravn_origin.rsplit(":", 1)[1],
                "public_url": ravn_origin,
                "admin_socket": str(directory / "run/admin.sock"),
            },
            "storage": {"sqlite_path": str(directory / "ravn.db")},
            "secrets": {
                "managed": {
                    "active_key_id": "simulation-only",
                    "key_source": {"path": str(directory / "master.key")},
                }
            },
            "applications": [
                {"id": "support-desk", "return_urls": [app_origin + "/connect/return"]}
            ],
            "integrations": integrations,
        }
    )
    app = broker_app(
        config,
        provider={
            "github_cloud": GitHub(transport_factory=providers["github"].transport),
            "slack": Slack(transport_factory=providers["slack"].transport),
        },
    )
    service = app.state.service

    def oauth(integration):
        instance = SimulatedOAuth(
            integration,
            service.provider_for(integration),
            ravn_origin + f"/oauth/callback/support-desk/{integration.id}",
        )
        instance.consent_origin = consent_origin
        return instance

    service.oauth.provider = oauth
    async with AsyncExitStack() as stack:
        for p in providers.values():
            await stack.enter_async_context(p.server.session_manager.run())
        await stack.enter_async_context(app.router.lifespan_context(app))
        if resume:
            # The fixture provider is memory-only. Restore only its own synthetic
            # credentials from this demo's encrypted store, never live tokens.
            async with service.store.transaction() as db:
                saved = await rows(
                    db, "SELECT * FROM connections WHERE status='active' AND ciphertext IS NOT NULL"
                )
            for connection in saved:
                kind = connection["integration_id"]
                if kind not in providers or connection["credential_type"] != "oauth2":
                    raise ValueError("This is not a supported simulated connection")
                bundle = json.loads(
                    service.cipher.decrypt(
                        connection["ciphertext"],
                        connection["app_id"],
                        connection["tenant_id"],
                        connection["id"],
                    )
                )
                access, refresh = bundle["access_token"], bundle.get("refresh_token")
                if not access.startswith(
                    "xoxp-simulated-" if kind == "slack" else "ghu_simulated-"
                ) or (refresh and not refresh.startswith("simulated-refresh-")):
                    raise ValueError("Refusing to simulate a real provider credential")
                providers[kind].tokens.add(access)
                if refresh:
                    providers[kind].refreshes.add(refresh)
        key = (await service.create_key("support-desk", "local simulated app"))["key"]
        yield (
            app,
            consent_app(providers, consent_origin, ravn_origin, "support-desk", app_origin),
            key,
            providers,
        )


class Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        yield  # asyncio.run owns signal handling for the group of local servers.


async def run(args):
    if not 1024 <= args.port <= 65532:
        raise ValueError("Choose a local port between 1024 and 65532")
    app_origin = f"http://127.0.0.1:{args.port}"
    if (args.console or args.resume) and not args.demo:
        raise ValueError(
            "--console and --resume are demo-only; live mode uses your existing operator console"
        )
    # Bind all required ports first; fail before generating credentials or opening
    # a browser if a port already belongs to another server.
    async with AsyncExitStack() as stack:
        ports = [args.port, args.port + 1, args.port + 3] if args.demo else [args.port]
        if args.console:
            ports.append(args.port + 2)
        sockets = []
        for port in ports:
            sock = socket.socket()
            stack.callback(sock.close)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
            sock.listen(128)
            sock.setblocking(False)
            sockets.append(sock)
        directory = (
            args.resume.resolve()
            if args.resume
            else Path(tempfile.mkdtemp(prefix="ravn-support-desk-"))
        )
        # Each launch gets fresh owner-only sign-in files without overwriting
        # prior files or application state in a resumed directory.
        links = Path(tempfile.mkdtemp(prefix="sign-in-", dir=directory))
        servers = []
        console = None
        if args.demo:
            ravn_origin = f"http://127.0.0.1:{args.port + 1}"
            consent_origin = f"http://127.0.0.1:{args.port + 3}"
            broker, consent, key, _ = await stack.enter_async_context(
                simulated_ravn(
                    directory, app_origin, ravn_origin, consent_origin, resume=bool(args.resume)
                )
            )
            desk = Desk(origin=app_origin, ravn_url=ravn_origin, app_key=key, demo=True)
            apps = [create_app(desk), broker, consent]
            if args.console:
                from ravn.console import Console, create_console_app

                # Same Service/Store instance as the gateway, never a second DB
                # writer. A distinct cookie avoids replacing the other console's
                # host-scoped cookie on 127.0.0.1.
                console = Console(
                    broker.state.service,
                    args.port + 2,
                    cookie_name=f"ravn_support_operator_{args.port + 2}",
                )
                stack.callback(console.close)
                apps.append(create_console_app(console))
        else:
            if not args.app_key_file or not args.ravn_url:
                raise ValueError(
                    "--live requires --ravn-url and --app-key-file (not a secret in argv)"
                )
            desk = Desk(
                origin=app_origin,
                ravn_url=args.ravn_url,
                app_key=private_file(args.app_key_file.resolve()).decode().strip(),
                app_id=args.app_id,
                tenant=args.tenant_id,
                user=args.user_id,
            )
            apps = [create_app(desk)]
        for app, port in zip(apps, ports, strict=True):
            servers.append(
                Server(
                    uvicorn.Config(
                        app,
                        host="127.0.0.1",
                        port=port,
                        lifespan="off",
                        access_log=False,
                        log_level="warning",
                        proxy_headers=False,
                    )
                )
            )
        tasks = [
            asyncio.create_task(server.serve(sockets=[sock]))
            for server, sock in zip(servers, sockets, strict=True)
        ]
        try:
            async with asyncio.timeout(10):
                while not all(s.started for s in servers):
                    for task in tasks:
                        if task.done():
                            task.result()
                            raise RuntimeError("A local server did not start")
                    await asyncio.sleep(0.05)
            login_url = desk.ticket()
            private_write(links / "login.url", login_url)
            print(
                f"Support Desk: {app_origin}\nMode: {'SIMULATED providers; real local RAVN' if args.demo else 'LIVE provider accounts'}\nState directory: {directory}\nPrivate one-use login link file: {links / 'login.url'}\nOpen that link within 5 minutes. Login lasts 1 hour. Ctrl-C stops the example.",
                flush=True,
            )
            if not args.no_open:
                webbrowser.open(login_url)
            if console:
                console_url = console.ticket()["url"]
                private_write(links / "console.url", console_url)
                print(
                    f"RAVN demo console: {console.origin}/console/\nPrivate console sign-in link file (60 seconds): {links / 'console.url'}",
                    flush=True,
                )
                if not args.no_open:
                    webbrowser.open(console_url)
            await asyncio.gather(*tasks)
        finally:
            for server in servers:
                server.should_exit = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(
        description="Developer-owned Support Desk app + RAVN integration"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--demo",
        action="store_true",
        help="New isolated RAVN, fake GitHub/Slack, no external accounts",
    )
    mode.add_argument(
        "--live", action="store_true", help="Connect this app to your configured RAVN server"
    )
    parser.add_argument("--port", type=int, default=18880)
    parser.add_argument(
        "--console", action="store_true", help="Run the demo's operator console at --port + 2"
    )
    parser.add_argument(
        "--resume",
        type=Path,
        help="Reuse an existing owner-only simulated demo state directory; browser logins are renewed",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Write the private login URL to a mode-0600 file without opening a browser",
    )
    parser.add_argument("--ravn-url")
    parser.add_argument("--app-key-file", type=Path)
    parser.add_argument("--app-id", default="support-desk")
    parser.add_argument("--user-id", default="you")
    parser.add_argument("--tenant-id", default="default")
    args = parser.parse_args()
    # Library INFO logs can contain OAuth callback URLs / one-use parameters.
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError):
        # Do not stringify arbitrary OS/SDK exceptions that might contain data.
        parser.exit(
            1,
            "Cannot start Support Desk. Check local ports, origin configuration, and owner-only credential files.\n",
        )


if __name__ == "__main__":
    main()
