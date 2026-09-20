# Gmail agent, gated by the real broker

A minimal agent that reads Gmail through **RAVN's actual broker enforcement**
(`src/ravn/gmail.py`, `Service.execute`, `reviewed_tools()`), not a
standalone reimplementation. See [`docs/onboarding.md`](../../docs/onboarding.md#2c-gmail)
for the full Gmail connector reference; this is the short path to running it.

Two connectors are available:

| Connector | Upstream MCP server | Requires |
|---|---|---|
| `gmail` | Google's own `gmailmcp.googleapis.com` | Workspace Developer Preview enrollment ([developers.google.com/workspace/preview](https://developers.google.com/workspace/preview)) |
| `gmail_local` | `mcp_server.py` in this folder, a self-hosted wrapper over the real Gmail REST API | Nothing beyond OAuth — works today |

Both are gated identically (each has its own reviewed tool set pinned per
integration, same OAuth flow) — `gmail_local` exists only because Google's
hosted server is gated behind Preview access. Its reviewed tools are
`src/ravn/manifest.py`'s `GMAIL_LOCAL_SCHEMAS` (`search_threads`,
`get_thread`, `get_message`, `list_labels` — read-only, no `create_draft`).

## 1. Google Cloud setup

Create an OAuth 2.0 client ID of type **Web application** in Google Cloud
Console (APIs & Services → Credentials) — not "Desktop app"; broker's OAuth
profile (`google_web_pkce`) is a confidential server-side flow with a client
secret, matching how the GitHub/Slack profiles already work. Add
`http://127.0.0.1:8787/oauth/callback/demo/gmail` as an authorized redirect URI.

Put the client secret in its own owner-only file:

```sh
mkdir -p .ravn
printf '%s' 'YOUR_CLIENT_SECRET' > .ravn/gmail-client-secret
chmod 0600 .ravn/gmail-client-secret
```

## 2. Install the extra dependencies

The Gmail connector and `mcp_server.py` need the Google API client libraries,
kept as an optional dependency group so the base broker install doesn't need
them:

```sh
uv sync --locked --extra gmail
```

## 3. Configure the integration

After `ravn init` (see the main [README.md](../../README.md)), add to
`.ravn/ravn.yaml`:

```yaml
applications:
  - id: demo
    return_urls:
      - http://127.0.0.1:8800/return
      - http://127.0.0.1:8801/return
      - http://127.0.0.1:8802/return

integrations:
  - id: gmail
    app_id: demo
    connector: gmail_local
    transport: remote_mcp
    endpoint: http://127.0.0.1:8790/mcp
    manifest: builtin:gmail-read-v1
    schema_hashes: {}
    oauth:
      profile: google_web_pkce
      client_id: YOUR_CLIENT_ID
      client_secret_file: /absolute/path/to/.ravn/gmail-client-secret
      scopes:
        - openid
        - email
        - https://www.googleapis.com/auth/gmail.readonly
```

`openid` and `email` are required alongside `gmail.readonly` — broker's
`identify()` step calls Google's real `userinfo` endpoint (even for
`gmail_local`) to get a stable account ID, and that 401s without them.
`gmail_local` never reviews a write tool, so `gmail.compose` is neither
required nor accepted for this connector (unlike the hosted `gmail`
connector, whose config-check requires it because Google's own MCP server's
access checks need it — see [docs/onboarding.md](../../docs/onboarding.md)).

## 4. Start the wrapper server and broker

```sh
cd examples/gmail_agent && ../../.venv/bin/python3 mcp_server.py
```

In another terminal:

```sh
uv run --locked --extra gmail ravn config-check
uv run --locked --extra gmail ravn serve --console
```

## 5. Connect and review schemas

In another terminal:

```sh
uv run --locked --extra gmail ravn app-key create --app demo --output .ravn/backend.key
uv run --locked --extra gmail ravn connections connect --integration gmail --app-key-file .ravn/backend.key --user alice
```

This opens a real Google consent screen in your browser — `gmail_local` still
uses real OAuth against real Google, only the tool-call hop afterward is
local. If the CLI's loopback callback port (8800 by default) fails to bind
(`Operation failed...`, often after a previous attempt), pass a different
registered port explicitly: `--return-url http://127.0.0.1:8801/return`. If
the browser shows "Link already used or invalid" on the very first click,
retry with a completely fresh `connections connect` invocation rather than
reusing the same link. After connecting, inspect and pin the schemas
(operator review is required — inspecting never auto-approves):

```sh
uv run --locked --extra gmail ravn integration-inspect --connection conn_REPLACE --app-key-file .ravn/backend.key --user alice --output .ravn/gmail-review.json
```

Copy the approved `schema_hashes` from that file into the `gmail` integration
in `.ravn/ravn.yaml`, then restart `ravn serve`.

## 6. Issue a session and run the agent

```sh
uv run --locked --extra gmail ravn sessions create --app-key-file .ravn/backend.key --user alice --connection conn_REPLACE --output .ravn/gmail-session.json
```

From the repo root:

```sh
uv run --locked --extra gmail python examples/gmail_agent/search_gmail.py --session-file .ravn/gmail-session.json --query "from:boss@example.com"
```

Only `.ravn/gmail-session.json` (a short-lived RAVN bearer token + `mcp_url`)
reaches `search_gmail.py`. It never sees your Google OAuth token — that stays
encrypted inside RAVN, and `mcp_server.py` only ever holds it for the
duration of one forwarded request, never persisting it. If `search_gmail.py`
asks for a tool outside `GMAIL_LOCAL_SCHEMAS` (e.g. `create_draft`), the gate
refuses it before the call ever reaches Gmail — see
`tests/test_gmail.py` for that enforcement proven against a fake upstream,
and `src/ravn/manifest.py`'s `GMAIL_LOCAL_SCHEMAS` for what's actually
reviewed.

## 7. A real, talkable agent

`search_gmail.py` is a fixed script — one query, one tool call, no reasoning.
`chat.py` is the real agent: an LLM (Claude, via the Anthropic API) decides
which of the session's tools to call from a natural-language request. The
gate is unchanged — Claude only ever sees the tools RAVN exposes to this
session, so it structurally cannot send or delete mail no matter what's
asked, regardless of prompt injection or a user's own request.

```sh
export ANTHROPIC_API_KEY=sk-ant-...
uv run --locked --extra gmail python examples/gmail_agent/chat.py --session-file .ravn/gmail-session.json
```

(Or put the key in `examples/gmail_agent/.env` — copy `.env.example`, fill
it in, and `chmod 0600` it; it's gitignored.)

Type naturally (`do I have any recent emails about X?`). Ask it to send or
delete mail and it will decline — not because it chose to, but because no
such tool was ever in the list it received; RAVN's `reviewed_tools()` filters
the real upstream catalogue (which does include `create_draft` and write
tools) down to the reviewed ones before the session's tool list is ever
built. This is `src/ravn/manifest.py`'s `GMAIL_LOCAL_SCHEMAS` allowlist
generically at work — nothing in the gate is hardcoded to Gmail or to any
specific tool name; the same `Service.execute` → `reviewed_tools()` →
`validate_arguments()` path enforces GitHub, Slack, and every Gmail
connector identically, driven entirely by which tools are pinned in config.

To prove the gate itself refuses a call — not just that Claude declined to
try — `force_denied_call.py` skips the LLM and calls `create_draft` directly
by name. `create_draft` is a real tool the upstream Gmail MCP server serves
(see `mcp_server.py`'s `TOOLS` list), but it was never added to
`GMAIL_LOCAL_SCHEMAS`, so it's absent from every session's tool list
regardless of who or what is asking:

```sh
uv run --locked --extra gmail python examples/gmail_agent/force_denied_call.py --session-file .ravn/gmail-session.json
```

Expected output:

```
Tools RAVN exposes to this session: ['get_message', 'get_thread', 'list_labels', 'search_threads']
Attempting create_draft anyway (not in that list; not reviewed) ...
DENIED by RAVN: Tool is outside this session's ceiling.
```

That denial happens inside broker's own `Service.execute`, before the call
ever reaches Gmail's real API — the same enforcement path
`tests/test_gmail.py::test_gmail_call_tool_outside_reviewed_schema_is_denied`
proves against a fake upstream, here proven against a real account.

## Switching to Google's hosted server later

Once your Cloud project has Developer Preview access, change the integration
block to use the `gmail` connector instead:

```yaml
    connector: gmail
    endpoint: https://gmailmcp.googleapis.com/mcp/v1
    manifest: builtin:gmail-v1
    oauth:
      profile: google_web_pkce
      scopes:
        - openid
        - email
        - https://www.googleapis.com/auth/gmail.readonly
        - https://www.googleapis.com/auth/gmail.compose   # required by Google's own access checks
```

Then re-run `integration-inspect` and re-pin — Google's real output schemas
differ slightly from the wrapper's (they use `$ref`/`$defs` for reusable
types like `Label`; `src/ravn/manifest.py`'s `check_schema()` rejects any
`$ref`, so a hosted-server integration must review against fields that don't
use them), so the hashes will change. `mcp_server.py` can be stopped.

## Revoke access

```sh
uv run --locked --extra gmail ravn sessions revoke sess_REPLACE --app-key-file .ravn/backend.key --user alice
uv run --locked --extra gmail ravn connections disconnect conn_REPLACE --app-key-file .ravn/backend.key --user alice
```

Disconnect deletes RAVN's copy of the Google credential and stops new
sessions; it does not revoke the grant on Google's side (revoke that
separately in your Google Account's connected-apps settings if needed).
