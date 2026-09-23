# RAVN Broker

A self-hosted connection broker for remote MCP servers. RAVN manages OAuth,
encrypted provider credentials, refresh, and short-lived sessions. Your
application owns user login and account-connection UI.

- Add a remote MCP URL and discover its OAuth settings. Supply pre-registered
  client credentials and choose provider scopes, or import bearer tokens.
  There are no built-in provider implementations or tool lists.
- A session holds up to eight connections and inherits each account's provider
  access. Attach or remove connections without replacing its token. There are no additional session tool permissions.
- Tools and their input/output schemas come from the authenticated MCP server.
  The provider enforces the account's permissions on execution; listing a tool
  does not prove the account can execute it.
- The local console lists applications with integration, connection, and active-session
  counts and links to each app's records and keys. It also shows activity and supports
  registering/disabling integrations, disconnecting accounts, and revoking sessions.

Account identity lookup is optional. Without it, RAVN verifies the credential
against the MCP service and uses your optional account label. Reauthorization
creates a new connection, which must be explicitly attached to sessions.

Tool calls are never automatically retried. Because RAVN does not infer whether
an arbitrary tool changes state, a lost response after dispatch is recorded as
an unknown outcome. Automated tests use synthetic providers; they do not prove
live compatibility with a particular service.

## Run

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked
uv run ravn init --directory .ravn
uv run ravn serve --console
```

Initialization creates only server configuration, storage paths, and an encryption
key. Applications and integrations are added later and saved in SQLite.
Run `uv run ravn console`, open **Applications → Create application**, then
**Application keys → Generate key**. Copy the key once into your trusted backend.
Add a remote service under **Settings → Integrations**. Application settings and
integration changes apply without restarting the server.

The same setup is available through the owner-only CLI:

```sh
uv run ravn application create --id my-agent --return-url http://127.0.0.1:8800/return
uv run ravn app-key create --app my-agent --output /absolute/private/backend.key
```

The app key identifies the application. Backend requests use
`Authorization: Bearer APP_KEY` and `X-Ravn-User-Id: USER_ID`; multi-organization
apps also supply `X-Ravn-Tenant-Id`. Derive these IDs from your verified user login.
Keep app keys on the backend; agents receive only RAVN session tokens.
See the [onboarding guide](docs/onboarding.md) and optional
[Gmail registration example](docs/integrations/gmail.json).

Discovery supports MCP resource metadata and OAuth/OIDC authorization metadata.
The first version supports pre-registered OAuth clients (S256 PKCE, Bearer tokens)
and bearer-token imports. Manual OAuth settings are available when metadata is
missing. Automatic client registration, arbitrary REST APIs, and provider-specific
OAuth response formats are outside this version's scope.

Run `uv run ravn console` in another terminal to open the operator console.
The broker uses port 8787; the console uses 8788.

## Demo database

`src/ravn/schema.sql` creates the current database layout directly. There is no
migration history or automatic upgrade of older layouts. Current databases keep
working; for an incompatible demo database, run `ravn init --directory .ravn-fresh`
and use `--config .ravn-fresh/ravn.yaml`. This creates separate state without
removing your existing database or key. Configuration uses version **2**.

## Develop

```sh
uv run --locked pytest
uv run --locked ruff format --check src tests
uv run --locked ruff check src tests
uv build
```

The console is server-rendered HTML with Jinja2. Edit
`src/ravn/templates/console.html` for pages and `src/ravn/static/console.css` for
styles. The small `console.js` handles sign-in, form submission, and interface
interactions. The original console layout, fonts, and icons are retained. There is no Node.js dependency, React app, or frontend build step.

## Deployment

Run one broker process per SQLite database. Keep `.ravn/` and its encryption
key private and back them up together. Use HTTPS beyond local development;
the operator console and admin socket are local-only. Application keys stay in
trusted backends; agents receive only session tokens for their attached connections.

Provider destinations are operator configuration. Outbound traffic requires
HTTPS on port 443 and publicly routable addresses; DNS pinning, response limits,
and redirect rejection remain enforced. Private-network and local/stdio MCP
servers are not supported by this transport.

[Apache-2.0](LICENSE) · [Font and icon notices](src/ravn/static/THIRD_PARTY_NOTICES.txt)
