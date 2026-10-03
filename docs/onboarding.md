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

### Sending email

Google's Gmail MCP server only drafts. To let an agent send, add a separate
integration on the Gmail REST API, which needs no Developer Preview. Add the
`gmail.send` scope to your OAuth consent screen, reuse the same Google client,
and connect it like any other integration:

```yaml
  - id: gmail-send
    app_id: demo
    connector: gmail_api
    endpoint: https://gmail.googleapis.com/gmail/v1
    manifest: builtin:gmail-send-v1
    schema_hashes: {}
    oauth:
      profile: google_web_pkce
      client_id: YOUR_GOOGLE_CLIENT_ID
      client_secret_file: /absolute/private/gmail.secret
      scopes: [openid, email, https://www.googleapis.com/auth/gmail.send]
```

It exposes one tool, `send_email` (plain text; `to`, `cc`, `bcc`, `subject`,
`body`), and cannot read the mailbox. A sent email cannot be recalled, so every
send waits for approval like any other write.

### Approving writes

Every write tool in the manifest (Gmail `create_draft` and `send_email`) waits for a
person before it runs. Keep `ravn approvals` open in a second terminal on the
broker's machine; it uses the owner-only admin socket. Each request shows the
tool, user, session and arguments, rings the bell, and asks
`[a]pprove / [d]eny / [s]kip`. Skipped requests stay pending.
`ravn approvals list`, `ravn approvals approve <id>` and
`ravn approvals deny <id>` do the same without the prompt.

Every argument is listed, recipients and subject first and never cut; each
recipient is on its own line. Any other value longer than 2000 characters is
cut with its full length shown. A session may have at most 4 requests waiting.

An approval runs the arguments as the agent submitted them, once; the display
only strips control characters and cuts long values. A denial returns
`approval_denied` to the agent; no decision within `approvals.timeout_seconds`
(default 300) returns `approval_expired`. Restarting the broker expires every
pending request. Revoking the session while a request waits stops it too.

Raise the agent client's tool timeout above the approval timeout, plus time for
the provider call, or the client gives up and cancels the request: in Codex,
`tool_timeout_sec = 360` under `[mcp_servers.<name>]`; in Claude Code, a
per-server `"timeout": 360000` (milliseconds) in its MCP config, or both
`MCP_TOOL_TIMEOUT` and `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` set to `360000`. The
idle timeout defaults to 300 seconds, the same as the approval timeout, so
leaving it unset races the operator.

#### Approving in the agent's own terminal

`approvals.channel` chooses who is asked:

```yaml
approvals:
  channel: both   # operator (default) | client | both
```

- `operator`: only `ravn approvals`, as above.
- `client`: the agent's MCP client shows the request in its own interface
  (an MCP elicitation) and the person there answers. A client that cannot show
  one is refused with `approval_unavailable`; a dismissed prompt denies.
- `both`: the client's prompt when it can show one; otherwise, or when the
  prompt is dismissed, the request waits for `ravn approvals`. Either may decide
  first.

In Claude Code the request appears as a dialog in the same terminal, with the
tool, recipients, subject and body, and Approve / Deny. Headless runs
(`claude -p`) cannot show it and dismiss it. Nothing is held open while the
person reads, so the client tool timeouts above do not apply to this path.

The answer is bound like an operator's: it unlocks only that session's pending
request for those exact arguments, once. It is, however, delivered by the
agent's client, and a client can be configured to answer prompts itself. Use
`operator` where the agent's machine is not trusted; the audit log records
`agent_client` or `local_operator` as the approver.
