# RAVN: Minimal Technical Design

Status: design target; see the root README for implemented features\
Date: 2026-09-11\
Revision: FastAPI, optional idempotency, and practical MCP access

**Implementation update:** The Python broker lives in [this repository](../../README.md). Live GitHub/Slack/Gmail compatibility remains unverified. A Gmail connector (Google's hosted Gmail MCP server) adds the first reviewed write, `create_draft`, over MCP with optional idempotency keys, no automatic retry, and `outcome_unknown` for ambiguous results. Milestone 2 now adds GitHub App user OAuth, Slack user OAuth and remote MCP reads, browser-bound completion, single-flight refresh, same-account reconnect, CLI onboarding, and a small Python browser-login binding helper. An optional operator console reuses the supplied HTML. Writes other than Gmail drafts, REST execution, application-facing activity listing, and the broader milestone-3 helper remain future work. See the [onboarding guide](../onboarding.md) for the implemented contract and provider setup.

## 1. What we are building

**RAVN is a lightweight, self-hosted connection broker that gives agents authenticated MCP access without exposing provider credentials to agent runtimes.**

The developer owns the agent, user login, and application UI. RAVN handles account connections, credential storage/refresh, connection-use authorization, and tool execution. No RAVN dashboard, new login system, or agent framework.

Why run it? To share that infrastructure across users and agent runtimes instead of rebuilding it in each application. For one trusted script with one token, direct integration may be simpler. GitHub is the first target integration, not the entire product value.

Self-hosting means no third-party credential-broker dependency—not that credentials never leave your infrastructure. RAVN sends them to the configured provider. The intended distinction is a small, MCP-first, headless service; self-hosting alone is not unique. See [Nango's deployment options](https://github.com/NangoHQ/nango#open-source-vs-paid) and [Obot](https://github.com/obot-platform/obot).

## 2. Two interfaces, different jobs

```text
Your trusted backend -- REST: connect/manage/create session --> RAVN
Your agent's MCP client -- MCP: discover/call tools ----------> RAVN
                                                               |
                                                provider credential
                                                               v
                                                   GitHub MCP server
```

| Interface | Who uses it | Purpose |
|---|---|---|
| REST management | Trusted backend or setup helper | Connect accounts, create/revoke sessions, disconnect |
| MCP | Agent's existing MCP client | Discover and call tools |
| REST tool calls | Trusted backend, if preferred | Alternative to MCP; not a second required integration |

Both call interfaces use the **same authorization and execution engine**. Developers do not implement tool calling twice. One active server is the MVP limit: if RAVN is unavailable, tool calls routed through it stop.

| Part | Choice |
|---|---|
| Server/CLI | Python + FastAPI; one Uvicorn worker; Python CLI |
| Deployment | One container; Python package for development; no standalone-binary promise |
| MCP | [Official Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk), server and client |
| Storage | SQLite; one active server; separately supplied encryption key |
| Client support | One small TypeScript onboarding/session helper; HTTP and Python examples |
| First integration | GitHub's remote MCP server; GitHub App user authorization, plus personal-token import |

Use async I/O for the broker and keep GitHub's remote server. Embedding its Go library would add a cross-language boundary; it is not part of this Python MVP. FastAPI owns REST, while the official MCP SDK owns the MCP protocol. [FastAPI concurrency](https://fastapi.tiangolo.com/async/)

## 3. Identity, without agent registration

| Identity | Example | How supplied |
|---|---|---|
| Application | Your Support SaaS backend | Derived from its RAVN application key |
| Tenant | Acme, a customer of your SaaS | Explicit for multi-tenant apps; `default` for single-tenant apps |
| User | Alice | Your backend's verified user ID |
| Runtime session | This agent's access to Alice's GitHub connection | Short-lived RAVN token |

The application key can act as any user within its application. Keep it in the trusted backend, never an isolated agent or browser. It is not an individual agent's identity.

Single-tenant apps supply only the user ID. Multi-tenant apps supply tenant and user separately. Tenant mode is operator configuration, not something an agent chooses.

A runtime token is an opaque random secret stored only as a hash. It binds one connection and expires after **one hour by default**. The backend can request a run-appropriate lifetime, capped by operator policy (up to four hours in this design). Revocation can stop access earlier; expiry remains a safety backstop. No permanent agent registration or second OAuth consent is required.

## 4. Developer journey

### A. Configure once

Start one RAVN service, register your application, and configure GitHub. For production user onboarding, register a GitHub App, install it on the intended repositories, and configure its user-authorization callback and client credentials. Use user tokens, not installation tokens, for personal connections. Testing our exact GitHub App user token against remote MCP remains a live compatibility requirement; the user waived milestone 0 to proceed with local milestone 1 implementation. This is not a claim that GitHub App OAuth already works. [GitHub user authorization](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app)

A limited personal token is supported for quick development and intentional bring-your-own-token deployments. It does not provide automatic OAuth onboarding or refresh.

### B. Connect Alice once

Your backend starts a connection flow. Alice authorizes GitHub; RAVN stages the credential. The browser returns to your app, which verifies the original login transaction before completing the connection. RAVN also rejects a completion from a different application/tenant/user. Both checks are required; matching user IDs alone does not prove the browser belongs to the initiating user.

Your application stores the connection ID, not the GitHub credential. A fixed internal agent can have an authorized account preconfigured during setup; it does not need a new account-connection flow for every run.

### C. Give the agent MCP access

Illustrative Python HTTP integration; endpoints are proposed, not shipping:

```python
# Inside an async backend handler. Multi-tenant apps also send X-Ravn-Tenant-Id.
import httpx

async with httpx.AsyncClient(base_url=ravn_url, headers={
    "Authorization": f"Bearer {ravn_app_key}",
    "X-Ravn-User-Id": verified_user_id,
}) as client:
    response = await client.post("/v1/sessions", json={
        "connection_id": alice_github_connection_id,
    })
    response.raise_for_status()
    session = response.json()

mcp_config = {
    "url": session["mcp_url"],
    "headers": {"Authorization": f"Bearer {session['token']}"},
}
```

Give this configuration to the agent's existing MCP client, not the model prompt. Integrating backends can use any language.
The MCP client discovers tools and calls them. RAVN checks access, injects the GitHub credential, and returns results. The agent does not connect accounts or call the management API.

Clients with configurable auth headers can use the reviewed read and write tools; per-call keys are optional. For a fixed-token client, choose a lifetime covering the run. Longer runs need tested backend-managed replacement/reconnection; expiry must not silently replay a write. The provider account stays connected.

### D. Inspect activity or disconnect

Use `GET /v1/calls` or `ravn calls list` to inspect a user's activity by session, connection, and time. Listings contain tool, status, timing, and a keyed argument fingerprint—not comment bodies or raw results.

Disconnect stops future RAVN use of an account. Revoke a session to stop only that session. Neither undoes an already-dispatched action. These are management operations, not extra steps on every tool call.

## 5. Small API surface

| Surface | Operations |
|---|---|
| Account connection | Start/status/complete/cancel OAuth; import personal credential |
| Connection management | List/get/disconnect |
| Runtime access | Create/list/revoke sessions |
| Agent tools | `/mcp`: tool discovery and execution |
| Activity | REST call listing/status, also available through the CLI |
| Backend alternative | REST tool discovery and execution |

Management uses the application key and verified user context. MCP uses the connection-bound session token only. Neither accepts arbitrary provider URLs or secret-store paths. Exact routes and schemas are in the [API contract](headless-ravn-v1.openapi.yaml).

## 6. Essential safety, not optional complexity

- Check application/tenant/user ownership and current connection/session state on every applicable operation.
- Encrypt credentials; retain encryption key IDs for rotation; never log tokens or raw tool arguments/results.
- Expire OAuth transactions after 10 minutes; staged completion gets at most 5 minutes within that limit.
- Refresh credentials safely; return `reconnect_required` when credentials are unusable or rotation is uncertain.
- Start with reviewed GitHub issue-reading tools, then enable comments after write-safety tests. Provider read-only settings supplement local checks.
- **Keys are optional:** REST `Idempotency-Key` or MCP `params._meta["ravn/idempotency-key"]` can identify a logical action. Same-key/request protection lasts while its record is retained (30 days by default). Without a key, repeated submissions may execute twice.
- Never automatically retry a potentially executed write. Record `unknown` and return `outcome_unknown` when the response channel is available; a lost connection cannot always deliver that error. Future high-impact actions require separately designed provider-backed safeguards.
- Store call status, not results, by default. Optional result retention is encrypted and capped at 15 minutes.
- Allow overlapping application keys for rotation. Return URLs must exactly match operator configuration.
- Back up SQLite consistently, configuration, and required encryption-key versions. A live database may include WAL files; copying one file is not a backup strategy. Keep key material separately protected.

## 7. Build order

| Milestone | Deliverable | Acceptance test |
|---|---|---|
| 0. Compatibility proof — skipped by request | Disposable GitHub App user-token test; live compatibility remains unverified | No live test or GitHub write performed |
| 1. Read-only slice — implemented locally | FastAPI/SQLite, token import, sessions, inbound MCP, shared executor, bootstrap CLI | SDK-backed fake-provider tests cover two users, cross-tenant denial, revocation, persistence and cancellation; real local CLI startup tested |
| 2. Real onboarding — implemented locally, live gate open | GitHub App user OAuth; requested Slack extension; completion helper, refresh, reconnect/disconnect | Fake-provider OAuth + real SDK tests cover replay, isolation, rotation uncertainty, reconnect races and real loopback CLI completion. Live provider flow/MCP still requires configured apps |
| 3. Developer-ready MVP | CLI/helper, activity listing, REST alternative, keyed and keyless writes | Ordinary MCP writes work; keyed retries deduplicate; uncertain writes never auto-retry |

Later: finer tool/repository restrictions, service-owned connections, external vaults, more integrations, a full Python SDK, richer events, and replicas. Recursive delegation and automatic desktop OAuth sign-in to RAVN remain outside this MVP.

**First experience: authorize an account once, configure MCP access, then let the agent call tools.**

Further detail: [implementation reference](headless-ravn-v1-reference.md) and [OpenAPI contract](headless-ravn-v1.openapi.yaml). These describe the full design target; [the broker README](../../README.md) identifies the implemented subset and its limitations.
