# Support Desk — a developer-owned customer app

This is a working **end-user UI + FastAPI application backend**, separate from
the RAVN operator console. No frontend build, LLM, or LLM API key is required.
The runner executes explicitly selected tools (reads, plus Gmail drafts that are
never sent); it does not generate answers or autonomously choose actions.

## Try it immediately

From `broker/`:

```sh
uv sync --locked
uv run --locked python -m examples.support_desk --demo
```

The launcher opens a private one-use login link. Then:

1. Click **Connect Slack**, then **Allow simulated access**.
2. Choose **Search public messages**, leave `refund` as the query, and run it.
3. Inspect the result and access trace. A RAVN call ID appears in Recent activity.
4. Click **Stop session**. The connection remains; an explicit new run creates
   a new session. This is not a persistent deny switch.
5. Click **Disconnect**. RAVN disables the connection and future calls cannot
   use it. Already-running reads may finish. The browser clears its last result.
6. Try GitHub: **Read an issue** → `acme` / `help-center` / `42`. Or connect both
   services and switch accounts. Reconnect replaces only that connection's authority.
7. Try Gmail, the full support story: **Connect Gmail** → **Search threads**
   (`refund`) → **Read a thread** → **Draft a reply (not sent)**. The draft form
   is prefilled as a reply to Dana. Then **List drafts** shows exactly one draft.

For Slack threads, the fixture is channel `C0123`, timestamp
`1789142400.000100`. Search matches queries containing `refund`, `1042`, or
`confirmation`; other queries return no matches. GitHub contains one fixture
issue; another repository/issue returns an explicit simulated not-found result.
Gmail has one thread, `18f2a0c4d5e6f701`, with messages `18f2a0c4d5e6f701` and
`18f2a0c4d5e6f702`; search matches the same three words. Simulated drafts live
in memory and are cleared when the demo stops, including with `--resume`.

### Gmail drafts

Drafting is the only write in this example, and drafts are never sent. Google's
Gmail MCP server has no send tool, and RAVN does not expose its label tools.

Each draft you create gets its own idempotency key from the browser. If the
result is uncertain (for example, the connection drops after Gmail saved it),
the app says so and the button changes to **Try again with the same draft**.
That retry reuses the key, so RAVN reports the earlier outcome instead of saving
a second copy. Editing the draft, or clicking the button again after a success,
starts a new draft with a new key.

**Demo means simulated provider accounts, OAuth, credentials, and data.** The
application REST calls, browser-bound RAVN onboarding, encrypted credential
storage, session creation/revocation, schema validation, MCP discovery/execution,
and RAVN call records are real. App → RAVN uses localhost HTTP; RAVN → provider
uses in-memory HTTP transports running real MCP SDK servers. No external
provider network request is permitted by the simulator. This is not a live
GitHub, Slack, or Gmail compatibility test.

The isolated demo starts these loopback listeners:

| Port | Role |
| --- | --- |
| 18880 | Support Desk backend + user UI |
| 18881 | A separate real RAVN broker, with simulated provider adapters |
| 18882 | Optional RAVN operator console (`--console`) sharing that broker's data |
| 18883 | Clearly labeled simulated OAuth consent pages |

`--port N` moves them to N, N+1, N+3. Occupied ports fail startup; existing RAVN
servers/data are not replaced. The existing 18787/18788 broker/console is untouched.
Add `--console` to run the demo's operator console on port N+2. It uses the
**same service and database** as the demo gateway, so connections, sessions and
calls appear there. It is not `/console/` on the gateway's port. Console and
customer logins remain separate, and its cookie does not replace the original
18788 console's login.

```sh
uv run --locked python -m examples.support_desk --demo --console
```

To keep a previous demo's connections and call history, stop it and restart
with `--resume /absolute/path/to/ravn-support-desk-DIRECTORY --console --demo`.
Use the same port setting. Only owner-only simulated deployments are supported.
The launcher restores synthetic provider credentials in memory; no real token
can be restored into the simulator. In-progress authorizations and browser
logins must be restarted, while stored connections, sessions, and call records
remain. A new backend application key is created for the restarted test app.

`--no-open` writes the private login URL to a mode-0600 file and prints **its path**,
not the secret. Open the link within five minutes. Login lasts one hour. Restart
to generate a new link. Do not share the link or paste it into logs/chat.

With `--console`, another private link file is printed for the operator console;
open it within **60 seconds**. Both browser logins last one hour. Fresh links
are placed in a new owner-only `sign-in-*` subdirectory on every launch. If a
link expires, restart with `--resume` to obtain new links without losing history.

Ctrl-C stops the local servers. Unless `--resume` is supplied, every launch uses a fresh owner-only temporary
directory; its path is printed with the login file. Simulated SQLite records and
keys remain there until your OS cleans the temp directory. They are not added to
your repository. A fresh demo does not reuse previous simulator credentials.

## Use real accounts instead

There is **no silent fallback** from live providers to simulation. Live mode
starts only the customer app and talks to your existing RAVN over its public API.
It does not read RAVN's database, master key, or admin socket.

1. Complete the [provider OAuth setup](../../docs/onboarding.md) in your RAVN
   deployment. Configure integration IDs `github`, `slack`, and/or `gmail` for
   one application. The UI shows all three; an unconfigured one fails explicitly.
2. Add this exact URL to that application's `return_urls` in RAVN configuration:
   `http://127.0.0.1:18880/connect/return`. Restart RAVN for config changes.
   This is **not** the provider OAuth callback; providers redirect to RAVN's
   `/oauth/callback/{app_id}/{integration_id}` URL, as described in onboarding.
3. Create a backend application key in an owner-only file. For example, with
   your existing `demo` application:

   ```sh
   uv run ravn app-key create --config /absolute/path/ravn.yaml --app demo --output /absolute/path/support-desk.key
   ```

4. Start this example (use the app ID that owns the key):

   ```sh
   uv run --locked python -m examples.support_desk --live \
     --ravn-url http://127.0.0.1:18787 \
     --app-key-file /absolute/path/support-desk.key \
     --app-id demo --user-id you
   ```

5. Connect your real account in the browser. If provider schema pins are empty,
   onboarding can complete but tool execution is not ready. Follow the
   [stored-credential schema review workflow](../../docs/onboarding.md), approve
   **real** provider schemas, restart RAVN, and run again. Never copy this
   simulator's fixture pins to a live deployment.

For multi-tenant applications add `--tenant-id YOUR_TENANT`. The IDs are assigned
by this trusted local test launcher, **not supplied by the browser**. The app ID
must match the key. GitHub, Slack, and Gmail live compatibility is still
unverified; this example does not bypass RAVN's fail-closed schema or OAuth profile checks.

## What the developer implements

| Browser action | Developer app route | Backend → RAVN |
| --- | --- | --- |
| Connect / Reconnect | `POST /api/connect` | Create connect session; save actor + login binding |
| OAuth returns | `GET /connect/return` | Validate binding; consume completion once; recover via status read if needed |
| View accounts / sessions | `GET /api/state` | List actor-owned connections and sessions |
| Run read tool | `POST /api/run` | Check ownership, create/reuse 600-second runtime, MCP `tools/list` then `tools/call` |
| Create draft | `POST /api/run` with `idempotency_key` | Same, and the key goes in MCP `_meta["ravn/idempotency-key"]`; never retried automatically |
| Stop session | `POST /api/sessions/{id}/revoke` | Revoke actor-owned runtime, discard cached handle |
| Disconnect | `POST /api/connections/{id}/disconnect` | Disable actor-owned connection, discard runtime handle |

The app sends its backend application key plus **verified** tenant/user identity
to RAVN management endpoints. Its MCP client sends only the connection-bound
runtime bearer. RAVN authenticates/authorizes and injects the provider credential.
The browser receives connection/session metadata and tool results, never those
three credentials. OAuth completion parameters necessarily traverse the browser
but are one-use, login-bound, and consumed by the backend.

Files:

- `app.py`: integration and local login; only RAVN REST + the small public
  `ConnectBinding` helper + MCP SDK. Runtime tokens live in backend memory.
- `static/`: dependency-free user UI, same-origin fetch, safe text rendering.
- `__main__.py`: local launcher; demo bootstrap is separate from live mode.
- `simulated.py`: demo-only provider transports, fixtures, and consent page.

## Exact access scope and example limitations

- End users choose accounts and can disconnect them or stop sessions. **There
  are no per-user scope toggles or resource-level policy editor.** This milestone
  uses RAVN's operator-reviewed tool manifests, not dynamic session scopes.
  Typed repository/channel fields are tool arguments, not persistent permission
  restrictions. Provider OAuth grants can be broader than RAVN's exposed tools.
- Gmail `create_draft` is the only write. A Gmail session can read and draft;
  there is no read-only Gmail session yet. An `outcome_unknown` result means the
  draft may exist, so check Drafts before trying again.
- Disconnect disables RAVN access; it does not uninstall the provider app or
  revoke the provider OAuth grant. Manage that separately at the provider.
- Local login is for **one tester**, not production authentication. It uses a
  one-use ticket, HttpOnly/SameSite cookie, exact Host/Origin checks, CSRF token,
  bounded bodies and per-login call exclusion. Never expose this example publicly.
  Loopback browser cookies are host-scoped, not port-isolated; other local apps
  on 127.0.0.1 are inside this development trust boundary.
- Production apps must replace test login with their verified SSO/session,
  serve HTTPS, persist/atomically consume login bindings and session handles in
  their own shared session store, add rate/tenant limits and retention controls,
  and handle multi-worker lifecycle. An application key is intentionally broad
  within its app; don't let browser fields impersonate users.
- App activity is in-memory metadata for this login (50 entries; UI shows six).
  Provider text is returned to the browser but not retained in this activity
  list. RAVN retains its normal operational records. Results themselves can
  contain sensitive account data; don't log them in a production integration.
- Connections/sessions show at most 100 records with an explicit truncation
  notice. No production pagination UI, background agent, streaming chat, sending
  mail, delegation, custom scopes, external vault, or user invitation flow is included.
- Restarting the app loses login/session handles, not live RAVN connections.
  Existing runtimes expire after at most ten minutes. No automatic read retry
  or silent replacement of a rejected bearer occurs within a run; the user can
  explicitly run again after resolving the failure.

## Verify

From `broker/`:

```sh
uv run --locked python -m pytest tests/test_support_desk.py
uv run --locked ruff check examples/support_desk tests/test_support_desk.py
```

Tests exercise both providers through the real RAVN OAuth and MCP stack,
cancellation, callback/login replay protection, CSRF, actor isolation, credential
non-disclosure, reconnect, session revocation, disconnect, write-tool enforcement,
single-save drafts per idempotency key, uncertain draft outcomes,
invalid arguments, and lost-completion-response recovery. Browser UI smoke
testing uses the demo; it does not authorize a real provider account.
