# Connect real accounts

Register provider apps once, configure RAVN, then let users authorize their
accounts. Live provider compatibility still needs verification.

## 1. Register provider apps

| Provider | Required setup |
|---|---|
| GitHub | Create a GitHub App with user OAuth and Issues read permission; install it on the intended repositories. |
| Slack | Create an internal or Marketplace app, enable MCP, and add user scopes `search:read.public` and `channels:history`. Use confidential OAuth; enable token rotation for refresh. |
| Gmail | Enable Gmail and Gmail MCP APIs in a Google Cloud project with Workspace Developer Preview access. Create a web OAuth client and configure consent/test users. |

Provider instructions: [GitHub](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app),
[Slack](https://docs.slack.dev/ai/slack-mcp-server/),
[Gmail](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server).

Register each callback as:

```text
RAVN_PUBLIC_URL/oauth/callback/demo/github
RAVN_PUBLIC_URL/oauth/callback/demo/slack
RAVN_PUBLIC_URL/oauth/callback/demo/gmail
```

Set `server.public_url` in `.ravn/ravn.yaml` to that broker origin. Use a
provider-accepted HTTPS URL for deployed callbacks; loopback HTTP is for local
development only. Keep the console and admin socket private.

## 2. Configure integrations

Save each client secret in a separate file with permissions `0600`. Use its
absolute path below. Replace the generated `integrations` section in
`.ravn/ravn.yaml` with the providers you need:

```yaml
integrations:
  - id: github
    app_id: demo
    schema_hashes: {}
    oauth:
      profile: github_app_pkce
      client_id: YOUR_GITHUB_CLIENT_ID
      client_secret_file: /absolute/private/github.secret

  - id: slack
    app_id: demo
    connector: slack
    endpoint: https://mcp.slack.com/mcp
    manifest: builtin:slack-read-v1
    schema_hashes: {}
    oauth:
      profile: slack_user_confidential
      client_id: YOUR_SLACK_CLIENT_ID
      client_secret_file: /absolute/private/slack.secret
      scopes: [search:read.public, channels:history]

  - id: gmail
    app_id: demo
    connector: gmail
    endpoint: https://gmailmcp.googleapis.com/mcp/v1
    manifest: builtin:gmail-v1
    schema_hashes: {}
    oauth:
      profile: google_web_pkce
      client_id: YOUR_GOOGLE_CLIENT_ID
      client_secret_file: /absolute/private/gmail.secret
      scopes:
        - openid
        - email
        - https://www.googleapis.com/auth/gmail.readonly
        - https://www.googleapis.com/auth/gmail.compose
```

Omit `gmail.compose` if you will not enable draft creation. GitHub uses the
app's OAuth client ID/secret and user tokens. Slack requires user tokens and
an internal or Marketplace app; Gmail access depends on preview availability
and applicable Google verification requirements.

### 2c. Gmail without Developer Preview access

The `gmail` connector above needs Workspace Developer Preview enrollment for
Google's hosted `gmailmcp.googleapis.com`. Until you have that, use the
`gmail_local` connector instead — gated identically, against a self-hosted
wrapper over the real Gmail REST API:

```yaml
  - id: gmail
    app_id: demo
    connector: gmail_local
    endpoint: http://127.0.0.1:8790/mcp
    manifest: builtin:gmail-read-v1
    schema_hashes: {}
    oauth:
      profile: google_web_pkce
      client_id: YOUR_GOOGLE_CLIENT_ID
      client_secret_file: /absolute/private/gmail.secret
      scopes: [openid, email, https://www.googleapis.com/auth/gmail.readonly]
```

Its reviewed tools (`src/ravn/manifest.py`'s `GMAIL_LOCAL_SCHEMAS`) are
read-only, so `gmail.compose` is never required or accepted. See
[`examples/gmail_agent/README.md`](../examples/gmail_agent/README.md) for the
wrapper server and a full walkthrough, including a runnable agent and a
script that proves the gate refuses an unreviewed tool call.

Run `uv run ravn config-check`, then start or restart `uv run ravn serve`.

## 3. Connect and approve tools

With the broker running, create a backend key once:

```sh
uv run ravn app-key create --app demo --output .ravn/backend.key
uv run ravn connections connect --integration gmail \
  --app-key-file .ravn/backend.key --user alice
```

The command opens browser consent. Use `github` or `slack` for other providers.
Save the returned connection ID and inspect its actual tools:

```sh
uv run ravn integration-inspect --connection conn_REPLACE \
  --app-key-file .ravn/backend.key --user alice \
  --output .ravn/tool-review.json
```

Review `schemas_for_review`; copy approved `schema_hashes` into the matching
integration and restart RAVN. Empty pins block sessions. Incompatible upstream
schemas require adapter changes, not just new hashes.

Create a runtime session:

```sh
uv run ravn sessions create --connection conn_REPLACE \
  --app-key-file .ravn/backend.key --user alice \
  --output .ravn/session.json
```

Configure an MCP client with the returned `mcp_url` and an
`Authorization: Bearer <token>` header. Each provider connection needs its own
session. Discover tools before calling them. Session tokens expire; connections
remain until disconnected or reconnect is required.

## 4. Connect from your application

Your backend authenticates users and sends its application key plus
`X-Ravn-User-Id` on management requests. Multi-tenant apps also send
`X-Ravn-Tenant-Id`; initialize those with `--tenant-mode multi`.

1. Add your exact browser return URL to the application's `return_urls` config.
2. Start OAuth with `POST /v1/connect-sessions` (`integration_id`, `return_url`,
   `app_state`) and redirect to `authorization_url`.
3. Bind the callback to the original authenticated login using
   [ConnectBinding](../src/ravn/onboarding_client.py), then complete through
   `POST /v1/connect-sessions/{id}/complete` with `completion_code`. Store and
   consume these bindings atomically in your application's session store.
4. Create a session with `POST /v1/sessions` (`connection_id`, optional
   `ttl_seconds`). Give the MCP URL and session token to the agent client.
5. Stop access with `POST /v1/sessions/{id}/revoke` or
   `POST /v1/connections/{id}/disconnect`.

User identity comes from your backend's verified login, never browser-supplied
identity fields. Provider tokens remain inside RAVN. Tool execution is MCP-only.

For Gmail writes, optional MCP metadata `ravn/idempotency-key` identifies the
same logical draft request. Without a key, repeated calls can create duplicates.
An `outcome_unknown` error means the draft may exist; check Gmail before retrying.
Disconnecting stops RAVN access but does not revoke the provider's OAuth grant.
