# Connect GitHub, Slack, or Gmail

OAuth onboarding is implemented and tested locally. **Live provider compatibility is
still unverified.** You register your own provider app; users authorize it. No
provider app, workspace, account, or credential is created by installing RAVN.

The journey: configure a provider once → connect in a browser → review tool
schemas → give an ordinary MCP client a connection-bound RAVN session.

## 1. Common setup

Work in the repository root. Run `uv sync --locked`. For new deployments only, initialize
with `uv run ravn init --directory .ravn --app demo`. Do not reinitialize existing
state. Add a return URL to the existing application in `.ravn/ravn.yaml`:

```yaml
applications:
  - id: demo
    tenant_mode: single
    return_urls:
      - http://127.0.0.1:8800/return
      # - https://app.example.com/settings/connections/return
```

Newly initialized configs already contain the CLI URL. Two URLs have different jobs:

| URL | Register where | Purpose |
|---|---|---|
| `RAVN_PUBLIC_URL/oauth/callback/demo/github`, `/demo/slack`, or `/demo/gmail` | Provider app settings | Provider returns its authorization code to RAVN |
| `http://127.0.0.1:8800/return` or your app's exact HTTPS URL | RAVN application's `return_urls` | RAVN returns a one-time completion code to your app/helper |

For provider deployments, set `server.public_url` to your externally reachable
HTTPS origin behind a TLS reverse proxy. Loopback HTTP is development-only;
provider callback registration rules still apply. Never expose the console or
Unix socket through the proxy. Strip OAuth-route query strings from proxy logs.
RAVN's CLI disables access and payload logging.

Put each provider **client secret** in a separate owner-only (`0600`) regular
file outside source control. Configure its absolute path, not its value. Never
put credentials in command arguments, YAML values, browser code, or agent prompts.

## 2A. GitHub App

Create a **GitHub App**, set its user-authorization callback to
`RAVN_PUBLIC_URL/oauth/callback/demo/github`, and install it on the intended
repositories with Issues read permission. Use its **OAuth client ID and client
secret**, not an installation token or the App private key. Enable user-token
expiration to exercise refresh.

Add `oauth` to your existing integration; preserve any reviewed schema pins:

```yaml
integrations:
  - id: github
    app_id: demo
    schema_hashes: {}
    oauth:
      profile: github_app_pkce
      client_id: REPLACE_WITH_GITHUB_CLIENT_ID
      client_secret_file: /absolute/private/path/github-client-secret
```

RAVN always uses PKCE S256 and accepts GitHub App user tokens (`ghu_`), without
silently substituting broader OAuth App or installation authority. Configure
GitHub App permissions at GitHub, not as OAuth scope strings. See
[user authorization](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app)
and [token refresh](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/refreshing-user-access-tokens).

## 2B. Slack

Create an **internal Slack app**, or use your Marketplace-published app. Enable
the Slack MCP Server feature in its Agents settings. Slack currently prohibits
unlisted distributed apps from using its hosted MCP server. Workspace/admin
approval and provider scopes remain Slack's responsibility. See
[Slack MCP overview](https://docs.slack.dev/ai/slack-mcp-server/) and
[app setup](https://docs.slack.dev/ai/slack-mcp-server/developing/).

Register `RAVN_PUBLIC_URL/oauth/callback/demo/slack` as its redirect URL. Add
**user scopes**, starting with `search:read.public` and `channels:history`.
Enable token rotation for refreshable credentials. Add this integration alongside GitHub:

```yaml
  - id: slack
    app_id: demo
    connector: slack
    transport: remote_mcp
    endpoint: https://mcp.slack.com/mcp
    manifest: builtin:slack-read-v1
    schema_hashes: {}
    oauth:
      profile: slack_user_confidential
      client_id: REPLACE_WITH_SLACK_CLIENT_ID
      client_secret_file: /absolute/private/path/slack-client-secret
      scopes:
        - search:read.public
        - channels:history
```

This explicit profile uses Slack's **confidential, server-side user-only flow**:
`https://slack.com/oauth/v2_user/authorize` and
`https://slack.com/api/oauth.v2.user.access`, with client credentials in the POST
body. It does not request bot tokens or enable Slack's public-client PKCE mode.
This is a fixed profile, not an automatic downgrade: Slack says enabling public
PKCE is a one-way app conversion. Use a separate confidential app if yours has
already opted into it. References: [token endpoint](https://docs.slack.dev/reference/methods/oauth.v2.user.access/),
[MCP metadata](https://mcp.slack.com/.well-known/oauth-authorization-server),
[PKCE implications](https://docs.slack.dev/authentication/using-pkce/),
[rotation](https://docs.slack.dev/authentication/using-token-rotation/).

| Read tool | RAVN-supported arguments |
|---|---|
| `slack_search_public` | `query`, optional `limit` (1–20) and `cursor` |
| `slack_search_public_and_private` | Same; appropriate private/DM user scopes required |
| `slack_read_thread` | `channel_id`, `message_ts`; relevant channel history scopes required |

Approve only tools you need; do not add all private/DM scopes by default. The
upstream schema and RAVN's narrower schema both validate arguments. Renamed
parameters/incompatible schemas fail closed and require adapter review; merely
changing a hash cannot fix incompatible arguments. No posts, reactions, uploads,
or other writes are enabled. See [Slack's official tool guidance](https://github.com/slackapi/slack-skills-plugin/blob/main/skills/slack-search/SKILL.md).

## 2C. Gmail

RAVN connects to Google's hosted Gmail MCP server,
`https://gmailmcp.googleapis.com/mcp/v1`. It is in the Google Workspace
Developer Preview, so its tools can change; pinned schemas then fail closed.
See [Gmail MCP setup](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server).

In Google Cloud:

1. Create or pick a project. Enable the **Gmail API** and the **Gmail MCP API**,
   and enroll the project in the Workspace Developer Preview.
2. Configure the OAuth consent screen. Use **Internal** for a Workspace
   organization. For **External**, add your testers as test users.
3. Create an OAuth client of type **Web application**. Add
   `RAVN_PUBLIC_URL/oauth/callback/demo/gmail` as an authorized redirect URI.

```yaml
  - id: gmail
    app_id: demo
    connector: gmail
    transport: remote_mcp
    endpoint: https://gmailmcp.googleapis.com/mcp/v1
    manifest: builtin:gmail-v1
    schema_hashes: {}
    oauth:
      profile: google_web_pkce
      client_id: REPLACE_WITH_GOOGLE_CLIENT_ID
      client_secret_file: /absolute/private/path/google-client-secret
      scopes:
        - openid
        - email
        - https://www.googleapis.com/auth/gmail.readonly
        - https://www.googleapis.com/auth/gmail.compose
```

`openid`, `email`, and `gmail.readonly` are required. Leave out `gmail.compose`
if you will not approve drafting; RAVN refuses a `create_draft` pin without it.
No other Gmail scope is accepted.

The `google_web_pkce` profile uses a confidential web client **and** PKCE S256.
It asks for offline access with a fresh consent each time, so Google returns a
refresh token. Google lets people untick scopes on its consent screen; RAVN
refuses the connection unless every configured scope was granted. Google does
not rotate refresh tokens, so RAVN keeps the stored one after a refresh. The
account ID is Google's stable `sub`, and reconnect must use the same account.

| Tool | RAVN-supported arguments |
|---|---|
| `search_threads` | `query` (Gmail search syntax), optional `pageSize` (1 to 20), `pageToken` |
| `get_thread` | `threadId`, optional `messageFormat` (`MINIMAL`, `FULL_CONTENT`, `METADATA_ONLY`) |
| `get_message` | `messageId`, optional `messageFormat` |
| `list_labels` | optional `pageSize`, `pageToken` |
| `list_drafts` | optional `query`, `pageSize` (1 to 20), `pageToken` |
| `create_draft` | `body` (plain text), optional `to`, `cc`, `bcc` (plain addresses, up to 20 each), `subject`, `replyToMessageId` |

`create_draft` saves a draft and never sends it. HTML bodies and attachments are
not accepted. Label, unlabel, and create-label tools are never exposed.

`gmail.readonly` and `gmail.compose` are **restricted scopes**. An External app
serving more than its test users needs Google's app verification and a yearly
security assessment. In Testing mode, refresh tokens expire after 7 days.

### Writes and retries

A write can carry an optional idempotency key in MCP
`params._meta["ravn/idempotency-key"]` (8 to 128 of `A-Z a-z 0-9 . _ : -`).
RAVN uses it locally and never forwards it to the provider.

- **Same key, same request:** RAVN does not call Gmail again. It returns the
  earlier outcome with `ravn/idempotency-replayed: true` and no result body.
- **Same key, different request:** `idempotency_conflict`.
- **Still running:** `call_in_progress`.
- **No key:** every submission is a new action and can save another draft.

RAVN never retries a write. If Gmail may have saved the draft but RAVN cannot be
sure (a timeout or dropped connection after sending), the call is recorded as
`unknown` and returns `outcome_unknown` with `retryable: false`. Check Drafts
before trying again. A clear refusal from Google (HTTP 401 or 403) is recorded
as `failed` and frees the key for a retry.

## 3. Connect and run

Validate configuration, then start/restart the server:

```sh
uv run ravn config-check
uv run ravn serve --console
```

In another terminal, create a backend key if needed, then connect:

```sh
uv run ravn app-key create --app demo --output .ravn/backend.key
uv run ravn connections connect --integration slack --app-key-file .ravn/backend.key --user alice
```

Use `--integration github` for GitHub or `--integration gmail` for Gmail. The command opens a one-use local browser
link, sets its own browser-binding cookie, waits up to 10 minutes, then prints
**connection metadata only**. `--no-open` prints the sensitive link instead;
do not share it. This local operator/development helper is not authentication
of a remote customer as Alice.

Save the connection ID. Inspect tool schemas with the credential already in
RAVN; no exporting/copying OAuth tokens is needed:

```sh
uv run ravn integration-inspect --connection conn_REPLACE --app-key-file .ravn/backend.key --user alice --output .ravn/slack-review.json
```

Review `schemas_for_review`. Copy the approved `schema_hashes` entries into the
matching integration and restart. Inspection never approves tools automatically.
Empty pins allow account connection but block session creation.

```sh
uv run ravn sessions create --app-key-file .ravn/backend.key --user alice --connection conn_REPLACE --output .ravn/slack-session.json
uv run python examples/search_slack.py --session-file .ravn/slack-session.json --query "project status"
uv run python examples/read_gmail.py --session-file .ravn/gmail-session.json --query "is:unread"
```

Only the session URL/token goes to your MCP client. The provider credential
stays in RAVN; the application key stays in your backend. GitHub, Slack, and
Gmail use **separate connection-bound sessions**, not one aggregated token. Multi-tenant
commands also require `--tenant`. The optional console remains the supplied HTML
UI, with real connection/OAuth lifecycle metadata and no secret values.

## 4. Your application's integration

Your app owns a CSRF-protected start handler, a return handler, verified user
login, and a server-side transaction store. `ConnectBinding` is a small helper
for matching the original login transaction, not an authentication system:

```python
from ravn.onboarding_client import ConnectBinding

# Authenticated, CSRF-protected start handler:
actor = (application_id, verified_tenant_id, verified_user_id)
binding = ConnectBinding.for_login(actor, verified_browser_login_id)
response = await ravn_http.post("/v1/connect-sessions", json={
    "integration_id": "slack",
    "return_url": registered_return_url,
    "app_state": binding.app_state,
})  # ravn_http carries the backend key and verified actor headers.
response.raise_for_status()
flow = response.json()
binding.session_id = flow["id"]
# Save binding in YOUR authenticated browser-session store, then redirect
# this browser to flow["authorization_url"]. Never send the app key to it.

# Return handler: retrieve that original binding and re-authenticate browser.
# Reject duplicate query parameters; handle a matching denial without completing.
code = binding.consume(actor, verified_browser_login_id, callback_query)
response = await ravn_http.post(
    f"/v1/connect-sessions/{binding.session_id}/complete",
    json={"completion_code": code},
)
response.raise_for_status()
connection_id = response.json()["id"]
```

The login ID must distinguish browser login sessions, even for the same user.
Never derive user/tenant/login identity from untrusted callback parameters.
For multiple workers, store and consume bindings atomically in your own shared
session store instead of this in-memory object. Never auto-complete by polling.
The application key can assert any user in its app; RAVN cannot verify your
customer's login for you.

If completion's response is lost, **read** its status: `completed` returns the
committed connection ID. Do not replay consent or create another connection
blindly. Return routes must omit queries from access logs, use no-referrer and
no-store responses, and load no third-party resources.

## Storage and lifecycle

- OAuth transactions expire after 10 minutes; staging gets at most five minutes
  within that limit. Every transition checks expiry. A 30-second sweeper wipes
  expired staging; terminal metadata remains queryable for 24 hours.
- Access/refresh tokens form one AES-GCM encrypted bundle, bound to deployment,
  application, tenant, and connection (transaction ID while staged). PKCE
  verifiers are encrypted. Start tickets, state, browser-binding secrets, and
  completion codes are hashed where only verification is needed.
- On-demand refresh starts within 60 seconds of expiry. Concurrent callers
  share one per-connection lock. A durable attempt marker precedes network I/O.
  Success changes credential version, not session authority epoch.
- Failed/lost/cancelled refresh and restart with an unfinished rotation require
  reconnect. This version conservatively includes possibly pre-send failures.
  No automatic refresh or tool-call replay occurs. Ordinary provider outages
  outside refresh do not disable connections; a definitive HTTP 401 for the
  current credential marks it `reconnect_required`.
- Reconnect must preserve the provider account (Slack: workspace **and** user).
  It replaces credentials atomically, increments epoch, and revokes old sessions.
  Disconnect wins pending reconnect/refresh; stale failures cannot poison a
  newer successful reconnect.
- Disconnect deletes RAVN's copy, not the provider OAuth grant. Provider-side
  revocation is **unsupported**; revoke separately when needed. WAL/backups can
  retain ciphertext, and already-admitted calls may finish.

```sh
uv run ravn connections connect --integration slack --reconnect conn_REPLACE --app-key-file .ravn/backend.key --user alice
uv run ravn connect-sessions show cs_REPLACE --app-key-file .ravn/backend.key --user alice
uv run ravn connect-sessions cancel cs_REPLACE --app-key-file .ravn/backend.key --user alice
```

## Added APIs and upgrade

| Method/path | Purpose |
|---|---|
| `POST /v1/connect-sessions` | Start OAuth or same-account reconnect |
| `GET /v1/connect-sessions/{id}` | Sanitized status/recovery; never exposes completion code |
| `POST /v1/connect-sessions/{id}/complete` | Consume browser-delivered completion code |
| `POST /v1/connect-sessions/{id}/cancel` | Cancel and clear staged credentials |
| `GET /v1/connections/{id}/tool-schemas` | Inspect candidate schemas, not approve them |
| `GET /oauth/start/{ticket}` | One-use browser start and transaction cookie |
| `GET /oauth/callback/{app_id}/{integration_id}` | Bound provider callback |

All `/v1` routes require an app bearer and verified user/tenant headers. Browser
OAuth routes validate ticket/state/cookie/expiry instead; they never accept
provider URLs. Callback paths include application ID because integration IDs
are application-local.

Schema 1 to 3 databases migrate transactionally to schema 4, which adds each
call's effect (read or write) and idempotency record. Old imported credentials
remain readable without gaining refresh capability. Back up the database
consistently and the key separately. Older binaries cannot use schema 4.

Before a live release, verify consent/callback, provider identity, a private
disposable read, expected permission denial, expiry/refresh, reconnect, and
disconnect using your configured app. Schema pins must come from the actual
provider, not our fixtures. Local fake-provider and SDK tests do not establish
live interoperability. No live tests or external writes were performed here.
