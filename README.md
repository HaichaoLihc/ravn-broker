# RAVN Broker

A headless MCP broker built with FastAPI and the official Python MCP SDK. It
exposes reviewed read tools, plus one reviewed write: Gmail drafts, which are
never sent. Applications keep their own login and UI; RAVN manages provider
connections, credentials, and short-lived agent access.

**Live GitHub, Slack, and Gmail compatibility remains unverified.** Tests use
local fake identity/OAuth providers and real MCP SDK clients/servers, not live accounts.

**[Connect real GitHub, Slack, and Gmail accounts](docs/onboarding.md).**
Connect from the CLI with `ravn connections connect --integration slack`, or
use the same REST onboarding flow from your own authenticated backend.

**Try the customer experience: [Support Desk example](examples/support_desk/README.md).**
A separate FastAPI developer app and end-user UI: connect GitHub/Slack/Gmail, run
read tools, save Gmail drafts, stop sessions, and disconnect. From the repository root, run
`uv run --locked python -m examples.support_desk --demo` for an isolated RAVN
with clearly labeled simulated providers, or use `--live` with your configured broker.

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). From the repository root:

```sh
uv sync --locked
uv run --locked python -m examples.support_desk --demo --console
```

The launcher opens Support Desk with simulated providers and a local operator
console. No provider registration is required for this demo. For real accounts,
follow [OAuth setup](docs/onboarding.md) and the [Support Desk live-mode guide](examples/support_desk/README.md#use-real-accounts-instead).

## Repository layout

| Path | Purpose |
|---|---|
| `src/ravn/` | Broker, CLI, provider adapters, migrations, and packaged console |
| `console/` | Operator console source and build tools |
| `examples/` | Support Desk and small MCP clients |
| `tests/` | Python tests with simulated providers |
| `docs/onboarding.md` | Current provider setup and application integration |
| `docs/design/` | Broader design targets; some features are not implemented |

See [documentation](docs/README.md) and [repository origin](docs/repository-origin.md).

## What works

- Personal bearer-token import, GitHub account verification, AES-256-GCM credential storage.
- Application keys, single-tenant defaults, explicit multi-tenant/user isolation.
- One-connection runtime sessions: one-hour default, configurable up to four hours.
- `/mcp` discovery and read calls: `issue_read` with `method=get`, and owner/repo-only `list_issues`.
- Sanitized call status, connection/session listing, disconnect and session/key revocation.
- One-process SQLite WAL, deployment lock, cancellation-safe transactions, restart recovery.
- Owner-only Unix-socket administration; application keys cannot administer the deployment.
- Optional local operator console, reusing the components/styles from the supplied HTML.
- GitHub App user OAuth (PKCE), Slack confidential user OAuth, browser-bound staging/completion.
- Encrypted token bundles, single-flight refresh, safe same-account reconnect and recovery.
- Slack's official remote MCP: public/private search and thread reads (reviewed pins required).
- Google's hosted Gmail MCP (Developer Preview): search, thread/message/label/draft reads, and
  `create_draft`. Google web OAuth with PKCE, granted-scope checks, and non-rotating refresh.
- Reviewed writes over MCP: optional `ravn/idempotency-key`, no automatic retry, and
  ambiguous outcomes recorded as `unknown` (`outcome_unknown`).
- Browser CLI onboarding, a small application-login binding helper, stored-credential schema inspection.

## Operator console

From the repository root, after initialization, use two terminals:

```sh
uv run ravn serve --console
```

```sh
uv run ravn console
```

The console is at `http://127.0.0.1:8788/console/`. The second command opens a
single-use sign-in link through the owner-only admin socket. Use `--config PATH`
on both commands for a non-default configuration, `--console-port PORT` on the
server to change the console port, or `ravn console --no-open` to print a link.
The link is a secret, expires after 60 seconds, and should not be shared.

Connections, Sessions, Activity (Calls/Events), and Settings use real local server
data. You can revoke a session/key or disconnect a connection; connecting accounts
and creating sessions remain in your backend/CLI. Settings show OAuth profiles
and provider callback URLs without secrets. The connection drawer includes a focused
access map. No existing provider credential or runtime token is revealed.

The console is disabled by default, always binds loopback, and uses a distinct
operator cookie—not application credentials. It is **not a remote admin or SSO
deployment**. One-hour browser sessions end on logout/restart. Readiness and saved
connection state do not establish live provider availability. No Sites service,
cloud hosting, external runtime assets, or production demo data is used.

Existing schema 1 to 3 databases migrate transactionally to schema 4 on startup,
preserving prior calls, events, and imported credentials. Missing legacy actor attribution stays unknown.
Back up existing state consistently before upgrading. Runtime console sessions
and single-use login tickets are memory-only; operational records remain in SQLite.

The built UI ships in the Python package. Node is needed only to modify/rebuild it:

```sh
cd console
npm ci
npm run build
npm test
```

The console development sources live in `console/`. See [console development notes](console/README.md).

Not included: writes other than Gmail drafts, REST tool execution (writes and
idempotency keys are MCP-only), application-facing activity listing, TypeScript
helper, or vault adapters.
The larger [design API](docs/design/headless-ravn-v1.openapi.yaml) is a target,
not a claim that all its routes ship here. Unsupported session restrictions fail
explicitly; do not pass them to this implementation.

## Install and test

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). Validated with Python
3.12.11, FastAPI 0.141.1 and MCP SDK 2.2.0; CI uses Python 3.13. Exact dependencies are in `uv.lock`.

```sh
uv sync --locked
uv run --locked pytest
uv run --locked ruff format --check src tests examples
uv run --locked ruff check src tests examples
```

Both modern `2026-07-28` and legacy `2025-11-25` inbound MCP are tested using
stateless Streamable HTTP. There are no legacy transport session IDs to trust/share.
Upstream connections are fresh per operation; provider auth and downstream auth are
independent. No tool-list notifications, progress streaming, or interactive tools are
advertised. The SDK owns protocol negotiation; reads are not automatically retried.

The `RAVN broker` GitHub Actions workflow runs the Python tests/style checks,
checks authored UI formatting, rebuilds the console, checks packaged-asset drift,
and builds the Python package. Provider tests use isolated local simulators;
the workflow does not require GitHub, Slack, or Gmail credentials.

## Connect a real account

These are steps for **you** to run when ready; they have not been run against GitHub.

1. Initialize local state (refuses to overwrite an existing directory):

   ```sh
   uv run ravn init --directory .ravn --app demo
   ```

2. Put a limited GitHub personal token in a file you own with mode `0600`, outside
   source control. Grant only the repository/Issues read access you need. Never
   paste it into an agent prompt or pass its value on the command line.

3. Inspect the two upstream tool schemas without executing them:

   ```sh
   uv run ravn integration-inspect --app demo --token-file /absolute/path/github-token --output .ravn/schema-review.json
   ```

   Review `schemas_for_review`, then copy the `schema_hashes` mapping into the
   GitHub integration in `.ravn/ravn.yaml`. This explicit operator step approves
   a remote schema version; inspection never auto-approves it. Empty pins expose
   no tools. Changed pins require a server restart and fresh runtime sessions.
   Unexpected upstream schema drift fails closed, even when a tool name is unchanged.

4. Start the server in one terminal:

   ```sh
   uv run ravn serve
   ```

5. In another terminal, create an app key and import Alice's credential:

   ```sh
   uv run ravn app-key create --app demo --output .ravn/backend.key
   uv run ravn connections import --app-key-file .ravn/backend.key --user alice --token-file /absolute/path/github-token
   ```

   Keep the returned connection ID. The GitHub token is not returned.

6. Issue a session using that connection ID, then use a normal MCP client:

   ```sh
   uv run ravn sessions create --app-key-file .ravn/backend.key --user alice --connection conn_REPLACE --output .ravn/session.json
   uv run python examples/read_issue.py --session-file .ravn/session.json --owner YOUR_OWNER --repo YOUR_REPO --issue 1
   ```

   Only `mcp_url` and the session bearer go into the agent's MCP client configuration.
   The app key stays in the trusted backend; the GitHub credential stays in RAVN.
   Discovery should precede calling, including so clients can cache result schemas
   before expiry/revocation. SDKs can make additional discovery requests themselves.

7. Inspect the `ravn/call-id` returned in MCP result metadata or stop access:

   ```sh
   uv run ravn calls show call_REPLACE --app-key-file .ravn/backend.key --user alice
   uv run ravn sessions revoke sess_REPLACE --app-key-file .ravn/backend.key --user alice
   uv run ravn connections disconnect conn_REPLACE --app-key-file .ravn/backend.key --user alice
   ```

For multi-tenant SaaS, initialize with `--tenant-mode multi` and add `--tenant`
to backend commands. For OAuth, Slack, and Gmail, follow [onboarding](docs/onboarding.md).
The backend derives user/tenant from its own verified login.
RAVN does not authenticate your application's end users for you.

## Implemented HTTP surfaces

| Surface | Authentication |
|---|---|
| `GET /healthz`, `GET /readyz` | Non-sensitive health only |
| `POST /v1/connect-sessions`; `GET /v1/connect-sessions/{id}`; `POST .../{id}/complete`; `POST .../{id}/cancel` | App bearer + user/conditional tenant headers |
| `GET /oauth/start/{ticket}`; `GET /oauth/callback/{app_id}/{integration_id}` | Browser transaction ticket/state/cookie; no app key in browser |
| `POST /v1/connections/import`; `GET /v1/connections`; `GET /v1/connections/{id}` | App bearer + user/conditional tenant headers |
| `POST /v1/connections/{id}/disconnect` | Same |
| `GET /v1/connections/{id}/tool-schemas` | Same; inspection never approves pins |
| `POST /v1/sessions`; `GET /v1/sessions`; `POST /v1/sessions/{id}/revoke` | Same |
| `GET /v1/calls/{id}` | Same; no retained result bodies |
| `/mcp` | Runtime bearer only; no actor/connection override headers |
| `/admin/v1/app-keys` and `.../{id}/revoke` | Separate owner-only Unix socket, never the public listener |

## Deployment limits and recovery

This is a development release. Live GitHub/Slack/Gmail
interoperability, production load testing, backup/restore drills, automatic retention,
master-key rotation tooling, and a pinned release container remain release work.

- Exactly one worker/process per database. Run `ravn serve`, not multi-worker Gunicorn.
- The DB and admin directories must be owner-only (`0700`); key files are `0600`.
  Do not use shared/NFS storage. A missing/wrong master key fails startup.
- Configuration is immutable while running. Restart to apply changes; existing
  tenant modes cannot be changed by editing configuration. Keep deployment ID stable.
- Routine app-key rotation permits overlapping keys. Use
  `ravn app-key revoke KEY_ID --revoke-sessions` for compromise containment.
- Disconnect deletes RAVN's credential copy and stops new admission. It does not
  revoke a provider token/grant or cancel an already-admitted upstream call.
- Interrupted/uncertain refresh requires reconnect; no refresh or tool call is
  automatically replayed. Reconnect invalidates existing runtime sessions.
- Calls record tool, status, timing and a keyed argument fingerprint; no raw
  arguments/results. Ciphertext may still exist in SQLite pages/WAL/backups after
  deletion; local disconnect is not a physical-erasure guarantee.
- Back up the stopped database (or use SQLite's online backup API), configuration,
  and the separately protected master key. Never copy only a live WAL-mode DB file.
  Do not serve an older backup without invalidating its runtime sessions and
  reconciling authority state. Automated restore is not implemented here.
- Fixed provider hosts, public-address DNS pinning with preserved TLS SNI,
  redirect rejection, request/result limits, and bounded concurrency are enforced.
  A vault cannot protect secrets from a compromised broker with decrypt authority.
- TLS termination is required beyond loopback. Do not expose the admin socket.
  The supported CLI disables access/SDK payload logs; do not enable debug payload
  logging or inject unreviewed middleware/telemetry around credential-bearing routes.

The Python MCP SDK uses `httpx2`; backend REST examples/CLI use `httpx`. Both are
explicit locked dependencies. There is no custom implementation of the MCP wire protocol.

## License

[Apache-2.0](LICENSE). Bundled console dependencies retain their
[third-party notices](console/public/THIRD_PARTY_NOTICES.txt).
