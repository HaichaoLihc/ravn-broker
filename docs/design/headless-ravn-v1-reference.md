# RAVN v1: Detailed Implementation Reference

Status: **full design target; milestones 1–2 implemented locally, not a production release**\
Date: 2026-09-11\
Revision: Python/FastAPI, optional idempotency, run-scoped sessions, and activity inspection\
Audience: implementers, integrators, and reviewers\
Start here: [Minimal technical design](headless-ravn-v1.md)\
Companion: [OpenAPI contract](headless-ravn-v1.openapi.yaml)

**Implementation status:** [broker/README.md](../../broker/README.md) is the source of truth for milestones 1–2. OAuth/refresh, same-account reconnect, staged browser-bound completion, a CLI/helper, and the requested Slack read-only integration are implemented locally alongside the original read slice and HTML-based operator console. See [onboarding setup](../../broker/docs/onboarding.md). The user explicitly skipped milestone 0; fake-provider/SDK/local-process tests do not establish live GitHub or Slack interoperability. Writes, REST execution, application-facing activity listing, and broader release hardening remain future work. The implemented callback path is `/oauth/callback/{app_id}/{integration_id}` to preserve application-local integration IDs.

This reference explains implementation details and later extensions. It is **not a requirement to build every feature in the first milestone**. The minimal design is the entry point and defines the build order. Inbound MCP and connection-bound runtime sessions are core. Fine-grained restrictions, service-owned connections, external vault adapters, extended event APIs, and their associated tables/routes are extensions. Ownership checks, safe OAuth completion, secure credential handling, and safe write outcomes are core requirements, not optional hardening.

## 1. Executive decision

Build a small, self-hostable service that connects agents to external services without requiring developers to rebuild OAuth connections, credential storage and refresh, credential injection, connection ownership checks, and operational diagnostics.

**Product promise:** authorize accounts once, configure an agent's existing MCP client, and call tools without exposing provider credentials to the agent or depending on a third-party credential broker. No RAVN dashboard or new agent framework.

The target is applications managing access across users and agent runtimes. A single trusted script with one token may be better served by direct integration; there is no arbitrary ten-connector threshold for value. GitHub proves the first end-to-end slice. Self-hosting alone is not unique: [Nango](https://github.com/NangoHQ/nango#open-source-vs-paid) also offers it. The proposed distinction is a small, headless, MCP-first service, not an unverified claim that competitors cannot self-host. Credentials still travel to configured upstream providers.

RAVN exposes REST management to trusted backends and MCP tools to session-authenticated agent clients. REST tool execution remains a supported alternative; integrators do not need both execution paths. It resolves an authorized connection, obtains its credential, optionally narrows the requested action, executes against a registered destination, and returns the result. It does not give callers a general-purpose secret-reading API.

### 1.1 Settled product requirements

- The product is headless: REST management, MCP tools, one small TypeScript helper, configuration, and CLI; no mandatory RAVN dashboard.
- Connect/create-session/disconnect are trusted setup and management operations, not steps an agent performs on each call. Fixed internal agents can use an account authorized during setup; authorization is preconfigured, not omitted.
- The integrating developer owns end-user login and UI. Customer-facing and employee-facing applications use the same engine.
- Personal connections and explicitly authorized service-owned connections are different ownership modes.
- Authentication, application/tenant isolation, and connection-use authorization are mandatory.
- Provider OAuth permissions remain a provider-enforced ceiling. Additional RAVN tool/resource restrictions are optional; there is no mandatory duplicate permissions editor.
- Credentials remain in the trusted broker/secret-storage boundary, not the model prompt or isolated agent runtime.
- RAVN manages only registered integrations and calls routed through it. It cannot govern bypass traffic merely by existing.
- A connector and MCP are different concepts: a connector supplies service-specific behavior; MCP supplies a tool-call protocol. Existing upstream MCP implementations should be reused.
- Recursive subagent delegation and cross-service authority chains are not requirements of the initial release.
- This is a clean reimplementation proposal. Existing Rust crates, demos, and older architecture documents are not assumed to implement this design and are not modified by this document.

### 1.2 Concrete engineering choices proposed here

These choices make the scope implementable; they are recommendations rather than previously approved product commitments.

| Area | v1 decision |
|---|---|
| Runtime | Python 3.12+, FastAPI + Uvicorn, one worker process; OCI container primary distribution |
| CLI | Python `ravn` console entry point, distributed with the server package/image |
| Backend API | Versioned JSON/HTTP API; OpenAPI 3.1 contract |
| Agent interface | Primary: tools-only inbound MCP with a connection-bound Bearer token; alternative: backend-mediated REST calls |
| Client support | One small TypeScript onboarding/session helper; HTTP and Python examples; full Python SDK deferred |
| Persistence | SQLite WAL via `aiosqlite`; exactly one active server process per database |
| Secrets | Encrypted managed-credential records; read-only Vault KV v2 references are an extension |
| First connector | GitHub Cloud: a curated remote-MCP tool subset plus provider OAuth/account validation |
| Other enterprise endpoints | Extension: registered remote MCP over HTTPS with reviewed schemas and credential binding |
| Fine-grained policy | Extension: exact tool lists and curated GitHub repository restrictions |
| Direct-client sessions | Core for MCP: random opaque tokens bound to one connection; unnecessary for trusted REST execution |
| Operator interface | Configuration file plus owner-only Unix-domain administration socket |
| Licensing | Recommend Apache-2.0 for newly written components; verify third-party licensing before copying anything |

No performance or compatibility claim in this document is a measured result. Examples, endpoint names, and commands below are proposed interfaces until implemented and tested.

## 2. Scope and release boundaries

### 2.1 Minimal release

1. Register an application and its permitted integrations through operator configuration.
2. Authenticate a trusted backend using an application key.
3. Create personal OAuth connections using a browser redirect and an application-verified completion step.
4. Import a supported personal Bearer credential server-side.
5. Store connection metadata; refresh supported credentials; reconnect, disconnect, and inspect status.
6. Discover and call reviewed GitHub tools through inbound MCP; retain REST tool execution as a backend alternative using the same engine.
7. Issue, list, and revoke short-lived connection-bound sessions. No permanent agent registration.
8. Enforce connection ownership, registered destinations, and active state.
9. Produce redacted operational logs, call outcomes, health checks, and actionable errors.
10. Accept keyed and keyless reviewed GitHub writes. Prevent duplicate local dispatch only when a stable retained key is supplied; never automatically retry a potentially executed write.
11. List sanitized call activity by actor, connection, session, status, and time through the API/CLI.

Extensions detailed below are not release prerequisites: fine-grained restrictions; service-owned connections and their ACLs; external vault references; generic remote-MCP integrations; and queryable event APIs. Add their modules, routes, and tables only when implementing the corresponding extension.

### 2.2 Explicitly excluded

- Agent/LLM hosting, orchestration, memory, model routing, sandboxing, or execution of model-generated code.
- RAVN-hosted user login, SSO directory, SCIM/group synchronization, or a policy-editor UI.
- An OAuth authorization server for clients signing into RAVN, dynamic client registration, or universal automatic desktop-MCP sign-in.
- Arbitrary HTTP forward-proxy behavior, runtime-selected hosts, or arbitrary vault paths.
- Running local/stdio MCP packages, Docker workloads, or third-party connector scripts inside the broker.
- Automatic OpenAPI-to-tool import, A2A, MCP prompts/resources, sampling, elicitation, tasks, and cross-connection tool federation.
- A general-purpose vault, raw credential export, or provider-token brokerage directly into agents.
- Shared monetary budgets, human-approval workflows, inherited delegation chains, cryptographic provenance, and policy inference from prompts.
- PostgreSQL, Redis, multiple active replicas, Kubernetes controllers, or multi-region operation in v1.
- Exactly-once execution across arbitrary upstream services, instant cancellation of already-dispatched actions, or comprehensive prompt-injection prevention.

The architecture leaves interfaces for future adapters and stores; it does not ship speculative implementations. GitHub Enterprise Server, GitHub App installation-token minting, and arbitrary OAuth providers require their own tested profiles before being advertised.

## 3. Concepts and responsibilities

| Concept | Meaning | Owner |
|---|---|---|
| Operator | Person/team deploying RAVN, configuring allowed networks and secrets | Customer infrastructure team |
| Application | One trusted integrating backend within one environment | Developer |
| Tenant | Customer/organization namespace; fixed `default` for single-tenant apps | Application |
| Actor | Application-defined authenticated user or service principal | Application |
| Integration | Reviewed destination + transport + auth profile + supported tools | Operator |
| Connection | One personal account or service credential bound to an integration | User, or explicitly configured service authority |
| Connect session | Temporary OAuth transaction; not an executable connection | RAVN + application browser transaction |
| Runtime session | Short-lived credential for one connection; required on MCP, not REST | Trusted backend issues; agent client uses |
| Restriction | Optional local limit on tools/resources | Operator/application/user via trusted application code |
| Call | One logical tool invocation and its dispatch/outcome record | RAVN |

### 3.1 Responsibility boundary

| Developer/application must do | RAVN must do |
|---|---|
| Authenticate its users and derive tenant/actor from verified login | Authenticate the application key and isolate its namespace |
| Own the user-facing connect/settings experience | Return authorization URLs and perform provider callback/token handling |
| Verify the original browser transaction before account linking | Stage credentials and activate only after verified completion |
| Decide business authorization: e.g. customer owns order, refund eligible | Enforce use of the selected connection and configured tool constraints |
| Keep broad application keys outside untrusted agent code | Accept narrower direct-client tokens and never forward RAVN tokens upstream |
| Configure provider OAuth apps and obtain any provider approvals | Reuse those configured OAuth apps and manage resulting credentials |
| Isolate agents that could otherwise read secrets or bypass the broker | Secure the broker's own destinations and credential injection |

RAVN trusts the authenticated application to assert its users. It does not independently prove that Alice logged into the application. A compromised application key can impersonate users in that application's configured namespace; this is a documented trust boundary, not an end-to-end human identity guarantee.

### 3.2 Employees, customers, and service-owned authority

Personal employee and customer connections follow the same flow. Different identities or UI do not require different broker implementations.

A merchant-owned connection is different: a customer asking for a refund does not acquire the merchant's Stripe authority. The RefundService application authenticates to RAVN using its own application identity, performs its own business checks, and uses an explicitly configured service connection. Any customer request ID is provenance, not authorization. Do not let a customer-controlled generic tool loop select that merchant connection.

Only register the entry points RAVN governs. If RefundService invokes an internal FraudService directly, RAVN does not control that hop. If the hop is routed through RAVN, register a separate integration and authorize that service caller. No global permission vocabulary is inherited across these service boundaries.

## 4. Architecture and deployment boundary

```text
 Customer/employee browser                  Operator CLI
       |                                      |
       | existing app login/UI                | owner-only Unix socket
       v                                      v
 Trusted application backend -------> RAVN process
   app key + verified actor           |  control API + config
       |                              |  connection/OAuth manager
       | runtime token                |  authorization + dispatch admission
       v                              |  credential resolver + refresh
 Isolated agent/MCP client ----------> |  MCP server -> MCP client adapter
   one-connection token               |  call ledger + audit
                                      |
                            +---------+----------+
                            |                    |
                       SQLite WAL           Secret storage
                     metadata/ledger      encrypted records or
                                          approved Vault references
                                      |
                                      | separate provider credential
                                      v
                           Registered MCP/API destination
```

The diagram includes optional branches, not extra dependencies for the minimal release. The initial managed secret store and metadata store share SQLite transactions. The external Vault extension supports imported, read-only credential references only.

### 4.1 Runtime modules

| Module | Responsibilities | Must not do |
|---|---|---|
| `config` | Strict YAML parsing, immutable integration revisions, validation/reload | Execute templates or load arbitrary code |
| `authn` | Application/session token verification; principal construction | Accept app ID or actor override from model input |
| `connections` | Lifecycle, ownership, personal import, shared-connection ACL | Treat a connection ID as authorization |
| `oauth` | Start/callback/staging/completion and provider validation | Activate credentials solely after provider redirect |
| `credentials` | Encrypted bundles, vault references, refresh single-flight | Return raw credentials to SDKs |
| `policy` | Mandatory checks and optional supported restrictions | Infer authority from prompts or untrusted tool annotations |
| `executor` | Admission, idempotency, dispatch, deadlines, outcomes | Blindly retry ambiguous writes |
| `mcp` | Terminate downstream MCP; originate independent upstream requests | Transparently relay inbound auth/headers |
| `connectors` | Reviewed provider profiles and tool schemas/semantics | Fetch unknown URLs or run uploaded connector code |
| `store` | Transactions, migrations, revisions, state constraints | Permit multiple uncoordinated active processes |
| `events` | Sanitized append-only-in-application events and queries | Claim tamper-proof storage against the operator |
| `admin` / `cli` | Local operator-only configuration and key lifecycle | Expose administration to arbitrary public clients |

### 4.2 Shared execution engine

REST and MCP adapters only decode protocol input, construct the applicable authenticated principal, and encode results. Both call the same discovery/authorization/executor services, credential resolver, argument validator, admission gate, and call ledger. Neither adapter may independently authorize a tool or inject provider credentials. An application key authenticates the REST backend; a session token authenticates MCP. Both resolve to the same internal app/tenant/actor/connection binding. Parity tests must demonstrate identical allow/deny and write-outcome behavior for equivalent bindings.

### 4.3 Python/FastAPI implementation choices

- Python 3.12+ and FastAPI for REST routes, schema validation, dependency injection, and application lifespan. Use Uvicorn with exactly one worker; do not infer safe multi-worker operation from async support. [FastAPI async guidance](https://fastapi.tiangolo.com/async/)
- The official `mcp` Python SDK owns inbound/outbound MCP, including version-specific envelopes and transports. Pin a tested stable release; do not mix v1 examples with the current v2 API or substitute an unrelated similarly named package. [Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)
- Compose the SDK's supported ASGI application at the exact external `/mcp` path, with lifecycle startup/shutdown coordinated by the parent FastAPI lifespan. Assert the final mounted path and auth behavior in tests; do not hand-code a partial JSON-RPC endpoint.
- HTTPX async clients for permitted outbound HTTP, with explicit deadlines, redirect rejection, environment-proxy behavior disabled, and a reviewed address-pinning transport. Authlib's async OAuth integration may provide protocol mechanics; RAVN owns persistence and serialized refresh, not an independent client's automatic refresh callback. Pin and test the chosen OAuth dependency before enabling the provider profile.
- SQLite through `aiosqlite`, with explicit parameterized queries and packaged SQL migrations. Its per-connection request queue does not by itself isolate multi-statement transactions; use a serialized write unit-of-work and bounded, separately leased read connections. [aiosqlite](https://aiosqlite.omnilib.dev/en/stable/)
- `secrets` for random tokens/nonces; `hashlib`/`hmac` for verifiers and domain-separated fingerprints; constant-time verifier comparisons; `cryptography` AESGCM with 256-bit keys and fresh 96-bit nonces for encryption. No custom cipher. [Authenticated encryption](https://cryptography.io/en/latest/hazmat/primitives/aead/)
- Pydantic validates control API models; reject unknown write fields. A JSON Schema validator checks reviewed tool inputs with remote resolution disabled. Duplicate JSON keys must be rejected before ordinary parsing/Pydantic can discard them.
- Pin dependencies in `pyproject.toml` and `uv.lock`; pin the Python base image and record dependency/license inventories. Install from the lockfile in CI/container builds, not floating `latest`. Package Python sources, migrations, and manifests together; no standalone native-binary claim.

### 4.4 Async lifecycle and concurrency rules

One event loop owns the server's connection-refresh locks, admission gate, and process state. Bounded coroutine concurrency overlaps external I/O; it is not a CPU-throughput claim. Hold the OS deployment lock for the process lifetime. Refuse a second worker/process, including accidental multi-worker Uvicorn/Gunicorn or replica configuration.

Use `asyncio.Lock` for refresh single-flight and admission, not an assumption that the GIL makes a transaction atomic. Lock ordering is fixed: refresh preparation completes before dispatch admission; admission acquires the serialized DB write unit-of-work, never the reverse. CLI mutations go through the owner-only admin socket, not direct DB writes.

Never perform network I/O inside an SQLite transaction. Avoid synchronous HTTP/DB work in async handlers; isolate unavoidable blocking operations in bounded worker threads without mutating loop-owned security state there. Cancellation-safe transactions must finish commit/rollback before releasing their connection. Cancellation during a provider write or rotating refresh uses the same uncertainty rules as a lost response.

Initialize database, key material, clients, SDK lifespan, and task supervision once. Shutdown drains in-flight work before closing these resources. Durable cleanup/revocation jobs are recorded in SQLite; FastAPI background tasks alone are not a durable work queue.

Auth enforcement must cover the mounted MCP ASGI app as well as REST. Test middleware order and sub-app lifespan explicitly. Disable public interactive API docs by default. Replace default validation/exception output with sanitized envelopes: framework errors can include rejected input, and secret-masking types are not encryption or permission checks.

### 4.5 Distribution and availability

Ship a versioned OCI image plus an installable Python package with a `ravn` CLI for development. Production startup fixes one worker and no code reload. Client applications remain language-independent through HTTP and MCP; deploying the container does not require clients to install Python.

The single active instance is a deliberate single point of failure. If it, its database, or required key store is unavailable, affected calls fail closed. There is no automatic direct-provider bypass or local credential fallback. One container is not one backup file: database state, configuration, encryption-key versions, and deployment metadata must be recoverable.

## 5. Developer integration journey

### 5.1 One-time operator setup

1. Install a pinned, verified container image, or install the locked Python package in an isolated development environment.
2. Run `ravn init` to generate local development state and a config template. This is explicitly development mode, binds loopback, and creates a durable local encryption key with owner-only permissions.
3. Configure an application and a GitHub integration; production supplies its key material and TLS/public callback settings explicitly.
4. Register a GitHub App with the exact RAVN user-authorization callback, minimal repository permissions, and expiring user tokens enabled. Install it on the intended repositories, or guide each customer/admin through installation. User authorization and installation are separate provider steps; the helper does not eliminate provider approvals.
5. Start `ravn serve --config ravn.yaml` and create an application key with the local operator CLI.
6. Put that key in the integrating backend's secret configuration. Do not pass it into agent containers, client browsers, or LLM context.

### 5.2 Personal OAuth connection

Application code adds two routes: start connection and finish connection. The SDK supplies helpers but does not own the application's session store or user authentication.

Illustrative TypeScript, not a shipping package/API:

```typescript
const ravn = new Ravn({ baseUrl: config.ravnUrl, appKey: secrets.ravnAppKey });

// POST /settings/github/connect, authenticated and CSRF-protected by your app.
const actor = { tenantId: login.tenantId, userId: login.userId };
const appState = randomNonce();
const connect = await ravn.forUser(actor).connections.connect({
  integrationId: "github-cloud",
  returnUrl: "https://app.example.com/settings/github/complete",
  appState,
});
await appSessionStore.bind(login.sessionId, connect.id, appState, actor);
return redirect(connect.authorizationUrl);

// Later: GET /settings/github/complete, authenticated by your application.
// Verify/claim, but retain the browser-bound transaction for safe recovery.
await appSessionStore.verifyAndClaim(login.sessionId, query.session_id, query.app_state);
const connection = await ravn.forUser(actor).connections.complete(query.session_id, {
  completionCode: query.completion_code,
});
// Store this opaque connection ID, never a provider credential.
await applicationDB.saveConnection(actor, connection.id);
await appSessionStore.markCompleted(login.sessionId, query.session_id);
```

The example omits framework-specific responses and error handling, not identity validation. A completion handler must recover `actor` from its verified login and bound server-side transaction, not from query parameters. Failed or interrupted completion may be safely retried as described in section 7.

### 5.3 Primary execution: configure the agent's MCP client

The trusted backend calls `POST /v1/sessions` for the explicitly selected, authorized connection. It passes only the returned token and `/mcp` URL to the agent's MCP client, never the application key or provider credential. The token cannot manage accounts, switch users/connections, or mint sessions.

```typescript
// Trusted backend; omit tenantId only for an application configured single-tenant.
const session = await ravn.forUser({ userId: login.userId }).sessions.create({
  connectionId,
});
const mcpConfig = {
  url: session.mcpUrl,
  headers: { Authorization: `Bearer ${session.token}` },
};
// Install mcpConfig in the existing MCP client, not in the model prompt.
```

The client handles discovery/calls. No application-specific tool dispatcher or schema translation is needed for the tested MCP path. Runtime-token expiry does not disconnect the provider account. The trusted backend can create a replacement after rechecking access; a helper may invoke a required backend-owned renewal callback. Never place the broad app key in the isolated runtime to enable renewal.

One endpoint is shared; the token selects one connection. This is not an automatically aggregated endpoint exposing every account owned by a user. Multiple connections use separate client bindings until federation is designed. Header-configurable clients can read; write-capable clients must satisfy section 11.5. Automatic desktop OAuth login to RAVN is not included.

### 5.4 Alternative execution: trusted backend REST

```typescript
const result = await ravn.forUser({
  tenantId: login.tenantId,
  userId: login.userId,
}).connections.call(connectionId, {
  tool: "issue_read",
  arguments: { method: "get", owner: "acme", repo: "backend", issue_number: 42 },
});
```

The tool dispatcher resolves `connectionId` server-side from the authenticated user's approved selection. There is **no mandatory agent registration, runtime token, or extra restriction policy** in this path. The application key remains in the trusted dispatcher, not with the model.

### 5.5 Existing credential and service connection

- Personal-token import is supported both for development and intentional bring-your-own-token deployments; it is not secretly dev-only. OAuth is the default customer-onboarding journey. Imported PATs are not refreshable by RAVN: expiry/revocation requires replacement or reconnect.
- A personal API token already held by the trusted application can be imported through `POST /v1/connections/import`. This endpoint accepts only supported credential shapes, verifies the provider account, encrypts the token, and returns metadata.
- A service-owned vault reference is operator configuration, not a user-supplied URL/path. The operator explicitly lists permitted service principals and applications. There is no implicit sharing with everyone in a tenant.
- Existing credentials collected from users are the application's UI responsibility. Neither the SDK nor examples should suggest pasting secrets into an agent conversation.

For a fixed internal agent, authorize/import an account under its configured owner once and retain only the connection ID in agent configuration. A trusted launcher/backend issues runtime access for runs and handles renewal; the agent only calls MCP. This does not make a personal connection tenant-wide or remove the separate service-owned ACL requirement.

## 6. Authentication and authorization contract

### 6.1 Application identity

Application keys have the shape `rv_app_<public_id>_<secret>`. The secret contains at least 32 bytes from a cryptographically secure RNG, encoded base64url without padding. The database stores the public ID, SHA-256 secret hash, application ID, label, creation time, and revocation state. Random high-entropy keys do not require a password KDF. Compare fixed-size hashes using constant-time comparison.

Each deployment is one environment. Production and development have independent keys, databases, connection records, and provider registrations. The authenticated key determines `app_id`; body/path/header app overrides are rejected.

All app-facing `/v1` routes require the application key and `X-Ravn-User-Id`. Multi-tenant applications additionally require `X-Ravn-Tenant-Id`:

```http
Authorization: Bearer rv_app_<id>_<secret>
X-Ravn-Tenant-Id: tenant_123
X-Ravn-User-Id: user_456
```

Each application has operator-controlled `tenant_mode: single | multi`, default `single`. In single mode, omitted tenant resolves to the literal `default`; explicitly supplied `default` is accepted, any other value is rejected with `400 invalid_request`. In multi mode, a missing/empty tenant is rejected with `400 invalid_request`; there is no fallback. Every persisted record, lookup, idempotency namespace, and runtime token still contains the resolved tenant. Changing mode with existing application state is rejected on reload and requires an explicit migration plan/new application, never implicit identity remapping.

Application ID is derived from the application key, never independently supplied. The app key identifies the integrating backend, not an agent. Do not encode tenant/user pairs into ad hoc concatenated identifiers.

Tenant and user IDs are opaque, case-sensitive application identifiers, 1-128 UTF-8 bytes, with control characters prohibited. Reject repeated identity/auth headers rather than choosing one. The SDK takes them only from trusted application code. Applications are trusted to assert actors within their namespace; they cannot assert an actor in a different application.

The application can use a designated service principal such as `refund-worker` for service-owned execution. This is still a backend assertion, not independent workload attestation. Optional caller labels such as agent display name and originating customer request ID are audit metadata only.

### 6.2 Mandatory authorization checks

Every operation authenticates the application/key and checks resource namespace, ownership, and operation-specific permission. Status, reconnect, and disconnect must remain available to an authorized owner when credentials are unusable; they do not require an executable connection or a supported tool. An operator-disabled integration blocks new authorization/execution but must not prevent inspection and local disconnect.

Discovery/execution additionally apply the following checks where relevant:

1. Application and key are active.
2. Integration is active and assigned to that application.
3. The resource belongs to the authenticated application and resolved tenant.
4. Personal connection owner matches the authenticated actor, or the service connection's explicit ACL permits the principal.
5. Connection is executable; credentials are usable or safely refreshable.
6. If a runtime session is used, it is active, unexpired, bound to this connection, and its recorded connection epoch is current.
7. Destination and supported tool are registered.
8. Every configured restriction admits the operation.

Use `404 not_found` for absent or inaccessible objects to reduce cross-tenant enumeration. Use `403 permission_denied` when the actor may see the connection but its policy denies the operation. Random IDs are never a substitute for these checks.

### 6.3 Optional restrictions, not duplicated OAuth scopes

```json
{
  "tools": ["issue_read", "list_issues"],
  "resources": { "repositories": ["acme/backend"] }
}
```

- Missing or `null` restrictions mean no additional RAVN narrowing; mandatory checks still run.
- Empty `tools: []` means no tools allowed. Empty `repositories: []` means no repository is allowed. Empty objects normalize to `null` after validation.
- Exact, case-sensitive tool names only; no regex, glob, expressions, prompt interpretation, or executable conditions.
- Repository names are validated `owner/repo` identifiers and normalized for GitHub's case-insensitive namespace. No slash/path escapes, query strings, percent-encoded separators, or secondary repository arguments outside the allowed set.
- The curated GitHub adapter validates repository-bearing arguments for each supported method. Arbitrary search/GraphQL/shell tools are not made safe by attaching a repository policy.
- Unknown restriction keys and constraints unsupported by the connector return `422 unsupported_constraint`; never silently ignore them.
- Tool descriptions and `readOnlyHint`/`idempotentHint` metadata are untrusted hints, not security classifications.

The effective RAVN decision is the conjunction of application/integration limits, current connection restrictions, and any runtime-session ceiling. The provider then independently authorizes the call using its current token and account permissions. RAVN does not pretend it can fully reproduce provider authorization from OAuth scope strings.

For v1, repository restrictions are **namespace/path restrictions**, not immutable repository-identity grants. A renamed/transferred/recreated repository can change what a path refers to. Applications needing stronger identity binding should use provider-native selected-repository permissions; immutable resource-ID policies are a separately gated feature.

### 6.4 Session ceiling and policy updates

At session creation, snapshot the resolved supported tool set and the intersection of applicable resource restrictions. Store this ceiling plus optional caller narrowing. Recheck current application, connection, and integration policies on each call.

Consequences:

- A later restriction takes effect on the next admitted call.
- A later policy expansion or newly added tool does not widen an already-issued session. Issue a new session for expanded access.
- Credential replacement/reconnect increments the connection epoch and invalidates old sessions. Routine refresh of the same authorized credential bundle changes credential version, not authority epoch.
- A tool-schema/security-semantics revision is not a harmless cache refresh: quarantine it or explicitly approve a new integration revision; existing sessions do not automatically gain its new behavior.

### 6.5 Runtime tokens

`rv_sess_<public_id>_<secret>` uses the same entropy/hash approach as application keys. Store application, tenant, actor, connection ID, connection epoch, issuing app-key ID, application security epoch, session ceiling, issued/expiry times, and revoked timestamp.

- Default TTL: 3600 seconds (one hour). API range: 60-14400 seconds (one minute to four hours). Operators may adjust `sessions.default_ttl_seconds` and lower `sessions.max_ttl_seconds`; validate `60 <= default <= max <= 14400`. Reject requests above the configured maximum with `400 invalid_request` instead of silently shortening a run's requested access. `expires_at` is authoritative.
- The backend selects a lifetime appropriate for the run; fixed-token clients are supported without assuming a hot-swap hook. Longer runs need a tested renewal/reconnection path. Keep both early revocation and expiry; no never-expiring sessions.
- Check expiry at every admission. Token expiry alone does not cancel an already-admitted upstream operation. Replacement tokens require fresh authorization and do not authorize replay of an uncertain write.
- Return plaintext once, with `Cache-Control: no-store`; never return it in list/get APIs.
- No refresh token, delegation, JWT/JWKS, offline verification, or self-renewal in v1.
- `/mcp` accepts only runtime tokens, not application keys or provider tokens.
- `/v1` accepts only application keys, not runtime tokens.
- Actor/connection override headers on `/mcp` are rejected. MCP transport session IDs are not auth tokens.
- A token is a bearer capability, not proof that a particular autonomous agent executable is running. Anyone who steals it has its authority until expiry/revocation.

### 6.6 Rotation, revocation, and dispatch linearization

An application may have multiple active keys; the database must not impose one-active-key uniqueness. Routine rotation creates a replacement, permits an overlap while backends deploy it, then revokes the old key. Previously issued runtime sessions may survive routine key revocation. A separate compromise operation revokes sessions issued by that key; application disable/security-epoch increment revokes all application sessions.

Use one in-process admission gate for access-control mutations and dispatch admission. Under that gate and a short database transaction, re-read current state, verify authorization, and commit a call's `dispatching` transition. Do not hold the gate during network or vault I/O.

**Guarantee:** a revocation committed before dispatch admission prevents that dispatch. A request admitted earlier may complete after revocation, even if the network send happens slightly later. Cancellation is best effort and cannot undo a comment/refund already executed. This is the precise meaning of “next call,” not a promise of globally instantaneous rollback.

Fail closed if the authoritative state cannot be read. No positive authorization cache in v1.

## 7. OAuth onboarding and account-linking state machine

### 7.1 Two different browser bindings

Provider OAuth state/PKCE binds the OAuth exchange. The application's login transaction binds the newly connected provider account to the intended application user. Both are required. Possession of a connect URL is not proof of the intended user's identity.

```text
pending -> authorizing -> awaiting_completion -> completed
    \          \                 \
     +----------+-----------------+-> denied | cancelled | expired | failed
```

The API may expose the initial state as `pending`; `authorizing` is internal. Connect sessions are single-use and expire after 10 minutes. Successful provider callback opens at most a 5-minute completion window, bounded by the transaction's absolute expiry. Completed IDs remain queryable for 24 hours; transaction secrets are removed immediately on terminal cleanup.

### 7.2 Exact flow

1. The app's authenticated, CSRF-protected start handler creates a random `app_state` and persists an association with its original browser-login session, actor, and intended integration.
2. `POST /v1/connect-sessions` stores the actor, exact registered `return_url`, integration revision, app state, and optional target connection/reconnect epoch. It returns the session ID and a one-use `authorization_url` carrying a high-entropy start ticket.
3. `/oauth/start/{ticket}` atomically consumes the ticket and establishes a transaction-specific, HttpOnly, Secure, SameSite=Lax browser cookie, then redirects to the fixed provider authorization endpoint. Production uses HTTPS. Do not use a blanket cookie across transactions without mapping state to the correct transaction.
4. Use Authorization Code flow with PKCE S256 where the tested provider supports it. Generate fresh OAuth state and verifier per transaction. A provider profile without supported PKCE requires explicit review, not silent downgrade. Do not support implicit/password grants.
5. The provider redirects to an exact configured `/oauth/callback/{integration_id}`. Validate cookie binding, state, provider/issuer, expected redirect and integration revision, expiry, and duplicate sensitive query parameters before token exchange.
6. Exchange the authorization code server-side. Verify stable provider account identity using the provider API; do not infer it from an unverified token payload. Save the entire credential bundle encrypted in staging. Staged credentials cannot be used for tool calls or sessions.
7. Redirect with HTTP 303 to the exact application's allowlisted completion URL, adding `session_id`, `app_state`, and one-use `completion_code`. Do not expose provider tokens or the provider authorization code. Do not reveal completion codes through status polling.
8. The app verifies the same original browser/login transaction, user/tenant, and app state, then calls `/complete` with its app key and completion code. A generic server-side poller must not auto-complete a transaction merely because status changed.
9. RAVN atomically consumes the completion code and activates the connection or swaps the verified same-account credential for reconnect. Only now is the connection executable.

Use separate callback paths and issuer verification to prevent provider mix-up. Configure exact redirect matching where supported. These controls follow OAuth's security guidance; the staged application completion protocol is a RAVN design choice. [OAuth security BCP](https://www.rfc-editor.org/rfc/rfc9700.html), [PKCE](https://www.rfc-editor.org/rfc/rfc7636.html), [issuer identification](https://www.rfc-editor.org/rfc/rfc9207.html)

### 7.3 Failure, replay, and completion recovery

- A callback with invalid state/cookie is rejected; do not consume an unrelated valid transaction.
- A valid callback atomically claims the exchange. Duplicate callback requests do not repeat token exchange. If token exchange has an ambiguous result, fail that transaction and restart authorization.
- Provider denial returns to the fixed app URL with the session ID, app state, and a safe status code. Never echo raw provider diagnostics into the redirect.
- Completion after expiry, actor mismatch, invalid app state association, cancellation, or connection epoch change fails closed.
- A successfully consumed completion code cannot activate a second connection. If the response was lost, an authenticated GET on the original session returns `completed` and the resulting connection ID, without a token/code.
- A failed reconnect preserves the old credential and connection status unless it independently became unusable. While refresh/reconnect is pending, the current usable connection can continue until an explicit disable/disconnect.
- Reconnect must verify the same stable provider account/workspace identity. Account replacement creates a new connection and requires new application selection; it is not an implicit operation behind a stable ID.
- Disconnect invalidates all pending reconnects by epoch. OAuth completion and credential-refresh commit must compare the original epoch so they cannot resurrect the connection.

Callback/start/completion responses use `Cache-Control: no-store` and `Referrer-Policy: no-referrer`; no third-party scripts, analytics, or resource loads. Strip query strings and secret-bearing bodies from access logs at both RAVN and the reverse proxy. The integrating app must do the same on its completion route.

## 8. Connection and credential lifecycle

### 8.1 Connection states

| State | Executable? | Transition |
|---|---|---|
| `active` | Yes, subject to all checks | Verified OAuth completion/import |
| `reconnect_required` | No | Definitive invalid grant, expired unrecoverable credential, or uncertain refresh |
| `disabled` | No | Operator disables integration/connection |
| `disconnected` | No; terminal for that connection ID | Authorized disconnect |

Refresh is an internal operation/lease, not an externally authorizing state. OAuth pending state belongs to a connect-session record, not an active connection. Provider outages do not automatically turn a connection into `reconnect_required` unless there is evidence its credential is unusable.

Every connection has a monotonically increasing `epoch` for authority invalidation, `revision` for optimistic updates, and `credential_version` for credential/cache changes. Do not conflate them.

### 8.2 Credential bundle

Managed bundles contain only provider-required fields:

```json
{
  "type": "oauth2",
  "access_token": "REDACTED",
  "refresh_token": "REDACTED",
  "token_type": "Bearer",
  "expires_at": "2026-09-11T21:00:00Z",
  "refresh_expires_at": null,
  "granted_scopes": [],
  "provider_account_id": "1234567",
  "provider_metadata": {}
}
```

The shape is illustrative; never publish it through normal RAVN APIs. Missing refresh or expiry fields are valid when the provider issues non-expiring/non-refreshable credentials. Persist actual response fields instead of assuming fixed lifetimes. A provider may represent permissions outside OAuth scope strings.

### 8.3 Refresh algorithm

1. An operation sees expiry within a 60-second safety window, or a reviewed provider-specific signal requires refresh.
2. Acquire a per-connection single-flight mutex. Re-read the current bundle and epoch after acquiring it.
3. If another request already refreshed it, use the new version. If no refresh is needed, release immediately.
4. Persist a refresh attempt ID, expected credential version, and connection epoch before the network call.
5. Send one refresh request to the configured token endpoint. Never allow arbitrary token endpoints from runtime input.
6. On success, atomically replace the entire access/refresh/expiry/scope bundle with compare-and-swap on the original version and epoch. If no replacement refresh token is returned, preserve the previous one only when the provider contract permits it.
7. Invalidate credential-bound upstream sessions/caches. Release waiters.
8. On definitive `invalid_grant`, mark `reconnect_required`. On an unambiguously pre-send network failure, return a transient failure. On potentially completed rotation with lost response or crash before durable commit, mark refresh outcome uncertain and require reconnect unless the provider has a reviewed recovery mechanism.

Failure and uncertain-outcome state changes also compare the expected connection epoch **and credential version**. A late failed refresh must not overwrite a newer successful reconnect. If either differs, record the stale attempt outcome without changing current connection state.

A mutex/CAS does not solve the remote rotation crash window. Do not replay a possibly single-use refresh token just because a local process restarted. The single-instance constraint is important; future replicas require durable fencing, not simply another mutex.

Never automatically request broader OAuth scopes during tool execution. Report that new consent is required. On an upstream 401, a connector may do one safe refresh; retry of the actual tool call still obeys the write-safety rules in section 12.

### 8.4 Local disconnect versus provider revocation

Disconnect atomically marks the connection disconnected, increments epoch, revokes its sessions, invalidates pending reconnects, and prevents new dispatch admissions. Return this local outcome without waiting for the provider.

- Managed OAuth credentials: enqueue provider revocation only if supported and appropriate; record `pending`, `succeeded`, `failed`, or `unsupported`. Delete retained cleanup credentials after completion or 24 hours maximum.
- Imported personal credentials: immediately block use and delete RAVN's encrypted copy. Do not automatically revoke the provider token, which may be used elsewhere.
- Imported external vault references: drop the RAVN binding; **do not delete or revoke a shared credential owned elsewhere**.
- Provider revocation may affect other uses of the same grant and must follow the connector's documented semantics.
- In-flight calls may finish. Backups and third-party copies are not erased by a database status change.

Do not advertise local disconnect as universal provider-token revocation. [OAuth token revocation](https://www.rfc-editor.org/rfc/rfc7009.html)

## 9. Secrets and storage boundaries

| Secret/data | Location | Use / exposure |
|---|---|---|
| Application key plaintext | Integrating backend secret configuration | Authenticates app requests; returned once by operator CLI |
| Application/session token hash | SQLite | Verification only; normal lists show prefix/ID, never plaintext |
| Runtime token plaintext | Isolated client process memory | One connection, short lifetime; no management authority |
| OAuth app client secret | Operator secret source; trusted RAVN memory when needed | Provider token exchange; never exposed to clients |
| Provider access/refresh tokens | Encrypted managed secret records | Decrypted only for provider calls/refresh |
| Existing company API credential | External vault | Read through approved reference; never copied into config or exported |
| Encryption master key | Production secret injection/file outside DB; dev owner-only key file | Wrap/encrypt managed secret records |
| Fingerprint HMAC key | Purpose-derived in trusted memory from a versioned master key | Request deduplication and diagnostic fingerprints; never returned or stored as plaintext in SQLite |
| Vault authentication | Workload identity or short-lived least-privilege token | Read only configured mount/path bindings |
| OAuth verifier/staged bundles | Encrypted temporary records | Removed after activation/expiry |
| Start/state/completion secrets | Hash where verification-only; encrypted where retrieval required | Short-lived; never logged or returned by polling |
| Connection metadata and call events | SQLite | Redacted operational metadata; still tenant-sensitive |
| Optional retained tool results | Separate encrypted result records | Disabled by default; explicit bounded TTL if enabled |

### 9.1 Managed encrypted store

Use AES-256-GCM with a fresh random 96-bit nonce per encryption and an explicit key ID. Authenticated associated data includes deployment ID, application ID, tenant ID, connection/staging ID, credential version, and schema version. This prevents swapping ciphertext between connection rows without detection.

The master key is separate from database files and backups. Production refuses to start without an explicitly configured persistent key source; no silently regenerated key on restart. Development-generated keys are mode `0600`, stored in a directory mode `0700`, and clearly documented as development custody, not a production vault.

Use only reviewed library cryptography. RAVN's credential-specific encrypted persistence does not make it a general-purpose vault: no arbitrary secret namespace API, password manager, or secret export is provided. Python immutable strings/bytes and runtime-managed copies do not provide reliable zeroization; minimize plaintext lifetime and prevent logging/core-dump exposure rather than claiming guaranteed erasure.

Key rotation: configure a new active key ID, retain old decrypt-only keys, migrate records in small transactions, verify all records and backups, then retire old keys after the documented backup-retention period. Loss of all decrypting key material makes stored credentials unrecoverable; reconnect is the recovery path.

For fingerprints, derive a separate 32-byte key with the library's HKDF-SHA-256, using the versioned random master key as input, UTF-8 deployment ID as salt, and `ravn/fingerprint/v1` as info. Do not reuse the AES key directly for HMAC. The stored fingerprint key ID identifies that master-key version; retain it for verification until its keyed call records expire. Domain-separate canonical request and diagnostic-argument inputs. This adds no separate operator credential. [HKDF library](https://cryptography.io/en/latest/hazmat/primitives/key-derivation-functions/#hkdf)

### 9.2 External secret references

The v1 external adapter is read-only Vault KV v2. An operator binding declares Vault endpoint, mount/path, field, allowed application, and credential audience/destination. Runtime callers reference a connection ID, never a Vault path. Use TLS and a least-privilege policy granting only required reads; no Vault root token.

Fetch external secrets just in time, with per-call timeout and concurrent-read coalescing; do not serve stale cached secrets after Vault failure in v1. Use returned version metadata to invalidate stateful upstream clients when the secret changes. Rotation detection is not external secret deletion, and RAVN cannot invalidate copies outside its control.

Writing managed OAuth bundles to external Vault is deferred. The internal `ManagedSecretStore` interface permits a later implementation, but two-store atomicity and provider-refresh recovery must be designed and tested before enabling it. [Vault KV v2 API](https://developer.hashicorp.com/vault/api-docs/secret/kv/kv-v2)

## 10. Connectors, integrations, and tool registration

### 10.1 Separate concerns

An integration composes:

1. **Transport adapter:** how to list/call tools, initially `remote_mcp`.
2. **Credential profile:** Bearer import/reference or a curated provider OAuth flow.
3. **Tool manifest:** supported names, reviewed input schemas, side-effect classification, and optional resource extraction.
4. **Destination policy:** exact approved origins, network ranges, headers, and timeouts.

A provider configuration does not automatically supply tool implementations. An upstream MCP server may already do that. Registering an arbitrary MCP server does not prove its tools are safe or its resource constraints understandable.

### 10.2 Internal interfaces

Illustrative Python protocol boundaries; these types are internal and not shipping APIs:

```python
from __future__ import annotations
from typing import Protocol

class ToolAdapter(Protocol):
    async def list_tools(self, binding: AuthorizedBinding) -> ToolCatalog: ...
    async def call(self, binding: AuthorizedBinding, call: ValidatedCall) -> ToolResult: ...

class ManagedSecretStore(Protocol):
    async def read(self, ref: SecretVersion) -> CredentialBundle: ...
    async def compare_and_swap(
        self, expected: SecretVersion, next_bundle: CredentialBundle
    ) -> SecretVersion: ...
    async def delete(self, ref: SecretVersion) -> None: ...

class ExternalSecretResolver(Protocol):
    async def resolve(
        self, binding: ApprovedSecretBinding
    ) -> tuple[CredentialBundle, str]: ...

class ResourceValidator(Protocol):
    def validate(
        self, tool: ReviewedTool, arguments: dict[str, object], limits: Restrictions
    ) -> None: ...
```

`AuthorizedBinding` and `ValidatedCall` are immutable internal values produced by authentication/validation, not directly deserialized from a runtime request. Request deadlines/cancellation are supplied by the executor. Secret access is injected only into trusted adapters; public results never contain credentials. Types improve review but do not enforce runtime authorization by themselves.

### 10.3 Integration registration rules

- Operator supplies endpoints, auth profiles, return-URL allowlists, and supported tools through configuration.
- `ravn integration inspect` performs authenticated discovery only on a configured destination and writes a candidate manifest. It does not automatically approve new tools.
- Approve a manifest by configuration revision. Pin the upstream schema fingerprint separately from RAVN's published supported-input schema. Unexpected executable upstream schema changes quarantine affected tools until reviewed. Narrow published schemas to supported methods/fields, validate them on every call, and reject rather than forward unsupported input.
- New upstream tools are not automatically exposed. This is a compatibility/support boundary, not a mandatory end-user permission matrix.
- Runtime callers cannot register destinations, choose auth headers, load code, select proxy environment variables, or change connector templates.
- Structural changes to endpoint, credential audience, auth provider, or connection ownership require a new integration/binding identity when active connections exist. Simple disable/restriction changes can be applied atomically.

### 10.4 GitHub Cloud first connector

Registered destination: `https://api.githubcopilot.com/mcp/`. The default provider profile is **GitHub App user authorization** plus personal PAT import. GitHub documents remote OAuth using a host-registered GitHub App or OAuth App, but the exact user-token flow and curated tools require live acceptance tests before release. Do not infer support for installation tokens from support for user tokens. [GitHub MCP server](https://github.com/github/github-mcp-server)

GitHub App user tokens act within the access shared by the user and app. Configure expiring user tokens and fine-grained repository permissions; the first read-only slice requests Issues read and required metadata access. Comment support needs a reviewed Issues write configuration. Installation/repository selection is a provider-admin step, not authorization of an individual RAVN agent. User OAuth and refresh use configured client ID/secret; no app private key or installation-token minting is required for this profile. [GitHub user-token flow](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app)

The helper distinguishes installing the GitHub App from authorizing the current user. Configure a provider installation link separately from RAVN's OAuth callback. Never trust a browser-supplied installation ID as proof of access: use the user token against the provider's installation/repository discovery endpoints when diagnosing installation access. A valid user connection can lack access to a requested repository; return an actionable permission error, not a request to silently mint a more privileged installation token. GitHub's permission checks remain authoritative; repository listings are not cached authorization grants.

Curated initial tools:

| Tool | Supported subset | RAVN classification |
|---|---|---|
| `list_issues` | Reviewed owner/repo-bound listing and bounded pagination | Read |
| `issue_read` | Initially `method=get` for one owner/repo/issue number | Read |
| `add_issue_comment` | One owner/repo/issue number and bounded comment body | Mutating |

Pin exact upstream schemas from the tested version and publish the narrower supported schemas. For example, publish `issue_read.method` as `const: get`; reject unsupported methods and any `add_issue_comment` fields that enable behavior outside adding a comment. If names/schemas differ, update the manifest and tests rather than silently adapting unverified behavior. Repository restrictions validate every supported tool's owner/repo fields. No unrestricted search, GraphQL, shell, arbitrary URL fetching, or generic REST passthrough is included.

Use operator-controlled GitHub tool selection headers if helpful, but enforce the curated manifest locally as well. Do not let a model set `X-MCP-Tools`, `X-MCP-Readonly`, or the destination. [GitHub remote configuration](https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md)

The GitHub App user profile uses configured permissions, not an OAuth `repo` scope. A separately tested OAuth App profile could be added later; its `repo` scope is broad. OAuth App tokens can also expire and refresh, so token lifetime alone is not the reason for the GitHub App choice. Support actual returned expiry/refresh fields and provider configuration rather than hard-coded lifetimes. [GitHub OAuth scopes](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps), [GitHub OAuth flow](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)

GitHub account verification uses an explicit `https://api.github.com` profile and stable numeric account ID; this additional origin is registered for identity validation, not exposed as a model-selectable proxy.

### 10.5 Remote versus embedded GitHub implementation

Use the remote server first: it preserves the provider-neutral MCP transport boundary and avoids linking provider tool implementations into RAVN. GitHub's remote deployment is independently updated; pinning RAVN's Python dependencies does not pin that deployment. Reviewed schema fingerprints, drift quarantine, and live compatibility tests remain required.

Embedding can pin tool code and avoid the hosted Copilot endpoint, but integrating the Go library into this Python service also introduces a cross-language dependency boundary. A separately deployed server would instead add a runtime component. GitHub labels its exported Go API unstable. Keep it a separate deployment-driven adapter decision, not an MVP requirement. A local sidecar would also add a deployment component and is not implicitly included. [GitHub library guidance](https://github.com/github/github-mcp-server#library-usage)

## 11. MCP protocol contract

Inbound `/mcp` is the primary agent execution interface and part of the MVP. Outbound MCP is the GitHub transport. REST remains the management interface and an alternative execution adapter.

RAVN terminates an incoming protocol request and creates a new upstream request. It is not a transparent HTTP proxy. Downstream and upstream authentication, protocol version, session state, and client metadata are independent.

### 11.1 Compatibility profile

| Direction | Required release test matrix |
|---|---|
| REST caller -> RAVN | RAVN `/v1` JSON API |
| RAVN -> remote MCP | Modern `2026-07-28` and legacy `2025-11-25` tools-only profiles, independently tested |
| MCP client -> RAVN | Same tested versions, explicit connection-bound Bearer token |
| Older MCP, HTTP+SSE legacy transport, stdio | Not advertised as supported by v1 |

Modern MCP uses per-request metadata and does not use the old initialization/protocol-session model. Legacy compatibility follows the legacy initialization and transport-session rules. Use the SDK rather than treating all MCP versions identically. Modern transport is POST-only; legacy methods are dispatched only under its tested profile. [Modern transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http), [legacy transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)

Version selection/probing uses discovery/read-only protocol operations, never replay of `tools/call`. Record successful upstream version by integration revision and security context. A protocol error on a mutating call is not permission to retry under another version.

The first tools-only implementation may buffer bounded request-scoped SSE into a final result. It must support cancellation/deadlines where available but must not advertise streaming/progress features it discards. Do not advertise tool-list-change subscriptions in v1; clients explicitly re-list.

### 11.2 Inbound authentication limitation

The MVP uses a **backend-provisioned Bearer profile**: the application obtains a runtime token and configures its client's authorization header. This is not a complete public MCP OAuth resource-server/login implementation and does not promise automatic sign-in from all desktop clients. Implementing authorization-server discovery, client registration, browser consent, and resource indicators for client-to-RAVN OAuth is separate work. [MCP authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)

No inbound RAVN token is forwarded to GitHub. Upstream credentials are separately selected from the connection. A downstream MCP session ID, if using a legacy profile, must be bound to the authenticated runtime token and cannot authorize a request alone.

### 11.3 Tool discovery

1. Authenticate and authorize before any upstream request.
2. Resolve/refresh the connection credential and list tools using that connection only.
3. Intersect upstream availability with the integration's reviewed manifest and current optional policies.
4. Validate names, schemas, content bounds, and schema fingerprints. Disable remote schema references; never fetch tool icons or returned URLs automatically.
5. Return deterministic sorted tools and opaque context-bound pagination cursors.
6. Recheck authorization at execution: listing a tool does not authorize every argument/resource it might accept.

Tool metadata is untrusted data; RAVN is not guaranteeing that descriptions or results are safe instructions for an LLM. Schema validation is a structural check, not prompt-injection prevention. [MCP tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)

### 11.4 Isolation and caching

- One runtime session is bound to one connection; tool names can stay unchanged because cross-connection federation is out of scope.
- HTTP transport sockets can be pooled, but per-request auth headers must be newly constructed. Never mutate shared client default headers with user credentials.
- For legacy upstreams that use protocol sessions, keep a bounded lazy pool keyed by app, resolved tenant, actor, connection ID/epoch, credential version, integration revision, protocol version, and policy/session-ceiling fingerprint. Reuse only within that exact context; never share cookie jars across contexts. Default: 128 entries maximum, five-minute idle expiry; evict idle entries first, reject excess admission rather than evicting in-flight entries. Destroy stale entries on refresh, disconnect, or relevant authority changes. Modern stateless requests do not create synthetic protocol sessions. Shared TCP/TLS connection pooling is independent of authorization.
- Share only immutable operator-reviewed manifest/schema data by integration revision/hash. Credential-dependent upstream discovery and the filtered tool view stay keyed by app, tenant, actor, integration revision, connection ID/epoch, credential version, protocol profile, and policy/session ceiling. Initial discovery-cache TTL is 30 seconds; authorization is still live. Never cache an authenticated user's tools/list response globally just because the provider is the same.
- A cursor is an opaque server-side record bound to the same context and a catalog fingerprint, with a five-minute TTL. Never accept another actor's cursor or transparently expose raw upstream cursors.
- Refresh/credential change invalidates credential-dependent upstream state. Disconnect/revocation invalidates sessions and discovery contexts. A final admission check prevents use of stale cached authority.
- Legacy reverse requests for sampling, elicitation, or tasks are rejected as unsupported, not relayed into the application automatically. Modern `resultType: input_required` / `inputRequests` is likewise unsupported: do not fulfill requests or label the action successful. Return `unsupported_upstream_interaction` if non-execution is known; if a mutation may already have occurred, record `unknown` and return `outcome_unknown` without retry.
- Validate header/body protocol metadata consistency. Outbound protocol headers are generated from validated upstream requests; incoming headers are not copied wholesale.

### 11.5 Client compatibility and write safety

A release must name the tested MCP client/SDK versions and demonstrate:

- Configurable Bearer headers and tools/list + tools/call with the selected protocol profile, including reviewed writes without an idempotency key.
- Fixed-token runs with a suitable requested lifetime. For longer runs, a tested backend-managed replacement/reconnection path; never place an app key in the agent runtime.
- No RAVN-side automatic replay of a potentially executed write, including after protocol failure or token replacement. Document and disable unsafe automatic retries in the tested client configuration; RAVN cannot control arbitrary client or model retries.
- Optional per-call metadata support, where available: `params._meta["ravn/idempotency-key"]`. It is a RAVN extension, not an MCP-standard guarantee. Keep the same key across retries of one intended action; never use one static key for all writes.

Ordinary tested MCP clients can read and write without custom per-call headers or a custom tool dispatcher. Keys add duplicate-dispatch protection; they are not a prerequisite for MVP GitHub comments. REST uses the optional `Idempotency-Key` header and the same executor. The small helper may expose optional metadata/key utilities, but no proprietary transport adapter is required for writes.

For valid authenticated MCP calls, preserve successful/provider tool results in the negotiated result format. RAVN-local failures use a negotiated protocol error (JSON-RPC profiles: code `-32000`, safe `data` containing stable `code`, `request_id`, optional `call_id`/`call_status`, and `retryable`). Invalid authentication is rejected by the transport before execution. Do not return a REST JSON envelope as an MCP result.

A keyed duplicate still running returns `call_in_progress` with the existing call ID and no new dispatch. A completed keyed replay without retained content returns `result_unavailable`, the known terminal call status, and `retryable: false`; it does not mean the business action failed. A potentially executed write is recorded as `unknown` and returns `outcome_unknown` with `retryable: false` when a response can be delivered. A crash or broken response channel can prevent delivery: a lost response also means uncertainty, not permission to repeat.

The trusted backend can inspect `GET /v1/calls` or `GET /v1/calls/{id}`; agent tokens cannot access management routes. Inspection does not itself establish that repeating an action is safe. Without a stable key, a repeated submission is a new call and may execute twice. Helpers must surface these states without inventing success content. Exact wire encodings must pass the selected SDK compatibility tests.

## 12. Tool execution, retries, and outcomes

This section is shared core for MCP and REST, whether or not finer policies are enabled.

### 12.1 Execution pipeline

1. Enforce request size, content type, method, and syntactic bounds. Reject duplicate JSON keys and duplicate security-relevant headers.
2. Build the authenticated application/actor or runtime-session principal.
3. Load the connection through application/tenant/actor-constrained queries.
4. Resolve the reviewed integration/tool and validate argument schema and any supported resource restrictions.
5. Create a call-ledger record for every admitted attempt; if a key is supplied, atomically reserve or look up its existing logical call. The key is nullable, not required.
6. Fetch or refresh the provider credential. No network request is made to a model-selected destination.
7. Under the admission gate, recheck application, integration, connection, token, policy, and epoch; commit the call's `dispatching` transition and audit admission.
8. Release the gate and send a new request with the independently selected provider credential and deadline.
9. Validate/bound the result; persist the terminal outcome and a sanitized event before returning where possible.
10. Return provider tool errors as tool results, distinct from RAVN auth/transport failures. If the result cannot be durably recorded after a possible upstream write, report an uncertain outcome and never blindly execute again.

### 12.2 Call state machine

```text
reserved -> dispatching -> succeeded
    |           |       -> tool_error
    |           |       -> unknown
    |           |       -> cancelled (only when no ambiguity about execution)
    +-> rejected / failed_before_dispatch
```

Public API states are `running`, `succeeded`, `tool_error`, `denied`, `failed`, and `unknown`. Internal `reserved`/`dispatching` map to `running`; authorization rejection maps to `denied`; proven pre-execution failure/cancellation maps to `failed`. Internal states remain distinguishable in storage. A restart with a durable `dispatching` call but no terminal record becomes `unknown`, not pending work to resend. Even a crash between dispatch admission and actual network send is conservatively unknown.

Cancellation before admission can be definite. Cancellation after dispatch is generally `unknown` unless the provider gives a reviewed definitive outcome. Client disconnect does not prove the provider cancelled.

### 12.3 Optional idempotency contract

- Keys are optional for all MVP tool calls, including reviewed GitHub comment writes. REST accepts `Idempotency-Key`; MCP accepts `params._meta["ravn/idempotency-key"]`. Supplied keys must be strings of 8-128 ASCII characters matching `[A-Za-z0-9._:-]+`; malformed keys are rejected before execution.
- RAVN consumes its own metadata field and does not pass it to GitHub. Generate outbound protocol metadata separately; do not copy arbitrary inbound headers or metadata wholesale. No per-call HTTP header extension is required for MCP.
- Without a key, accept the authorized call and assign a call ID. Every subsequent submission is a new attempt that may execute again. Identical arguments, JSON-RPC request IDs, session tokens, and generated call IDs are not deduplication keys.
- A new intentional action gets a new key. A retry of that same action reuses the original key, including across replacement runtime tokens. A helper that generates keys exposes them for workflow-level persistence/reuse; it must not generate a fresh key on every transport retry.
- Namespace uniqueness: `(app_id, tenant_id, actor_id, idempotency_key)`. The request fingerprint includes connection ID, exact tool name, canonical JSON arguments, and semantic request version, but not the replaceable session token. Different content under the same key returns `409 idempotency_conflict`.
- Use a keyed HMAC of canonical input for the fingerprint, not raw arguments or an unsalted hash of low-entropy input. Record the fingerprint key ID for verification through key rotation. Use a reviewed canonical JSON implementation; reject values it cannot represent. Diagnostic argument fingerprints use a separate domain (section 13.5), not implicit deduplication.
- Atomically insert the keyed reservation before dispatch. Concurrent duplicates observe the existing call state (`202` for REST, `call_in_progress` for MCP) without another dispatch. A proven pre-dispatch failure may resume only after an atomic check that admission never occurred.
- Execution replays still require current execution authorization before returning results. A disabled/disconnected connection blocks that path; it does not erase sanitized history for its still-authorized owner.
- Completed or unknown calls are never re-executed merely because the response was lost or not retained. A known completed REST replay returns the existing outcome with `result_available: false` when appropriate; an unknown replay returns `outcome_unknown`. MCP equivalents are in section 11.5.
- Call-ledger/key retention is 30 days by default. Deduplication holds only while the record is retained in the current database; an older backup or expired retention cannot provide it. Keep the effective window explicit in deployment/helper documentation.

This is local duplicate-dispatch protection, not exactly-once upstream execution. No key can make a lost, potentially executed GitHub write automatically safe to repeat. Future high-impact tools such as payments/refunds need a separate provider-backed idempotency and reconciliation design before support; they are outside this MVP.

### 12.4 Retry rules

| Situation | Automatic action |
|---|---|
| Invalid input/ownership/policy | Do not retry |
| Proven failure before dispatch admission | Bounded retry may be allowed after an atomic non-dispatch check; a generic timeout/5xx is not proof |
| Reviewed read operation: 429/transient network/5xx | Up to two retries with jitter; respect `Retry-After` and total deadline |
| Mutating/unknown tool after request may have reached upstream | No retry, with or without a key; record `unknown` if the outcome is unresolved |
| Provider auth rejection | Refresh once if appropriate; replay only when connector proves the original action did not execute or the read is safe |
| Future provider-backed idempotent action | Requires a separately reviewed adapter; not implemented for generic GitHub MCP writes |
| Runtime token expired | Trusted backend may issue a new token; this does not authorize replay of a possibly executed write |

RAVN helpers automatically retry safe reads only; they do not automatically retry tool-call writes. Executor recovery before admission requires proof as above. No layer may interpret all POSTs, all `retryable=true` errors, or the mere presence of a key as safe to replay. Keyless caller resubmissions can still duplicate an action; RAVN cannot prevent a client/model from issuing a new logical request.

### 12.5 Result handling

Preserve validated MCP content and structured-result semantics; preserve `isError` as a tool outcome, not a successful business action. Do not fetch resource links, execute tool output, or render provider HTML inside an administrative origin.

RAVN-generated errors contain stable codes and safe messages, never raw provider auth responses or arbitrary upstream error bodies. Known credential literals must be redacted if detected in diagnostics/results. This is defense in depth: a malicious upstream could transform/exfiltrate a secret it legitimately receives, so registered upstreams remain inside the trusted service boundary. RAVN is not a universal DLP system.

Store audit metadata separately from result bodies. Result persistence is off by default. An optional per-application encrypted result cache has a maximum 15-minute TTL and the same ownership checks; it must be an explicit choice because tool outputs may contain personal/company data.

## 13. HTTP API and SDK contract

The [OpenAPI file](headless-ravn-v1.openapi.yaml) is the machine-readable app-facing contract. The minimal guide separates REST management from MCP tools. Runtime-session routes are core; fine-policy and extended event operations are tagged extensions. REST tool routes are a supported alternative, not required in an MCP integration. MCP JSON-RPC and the operator socket are specified here separately rather than pretending they are ordinary REST resources.

### 13.1 General conventions

- Base path `/v1`; UTF-8 JSON; RFC 3339 UTC timestamps.
- IDs are opaque prefixes plus at least 128 random bits; never contain user names, emails, secrets, or sequential counters.
- All user-scoped REST routes require the application key and user header; the tenant header is conditional on configured tenant mode (section 6.1). Bodies cannot override ownership. Unknown JSON fields are rejected for write requests except schema-approved tool arguments.
- JSON responses use `Cache-Control: no-store` for credentials, connect operations, account metadata, and tool results. Reverse proxies must not cache user-varying discovery/results publicly.
- Request IDs are server-generated. An optional validated caller trace ID can be recorded separately; neither is authority.
- Pagination: default 50, maximum 100, opaque cursor, context-bound and expiring. No total-count requirement in v1.
- Optional PATCH requires an `If-Match` header containing the quoted revision; stale updates return `412 revision_conflict`, and a missing header returns `428 precondition_required`. Return updated `ETag` on metadata reads/updates. The editable `label` is distinct from the provider-derived `display_name`.
- Repeated disconnect/revoke requests are idempotent and return the current state. They do not imply erasure of external credentials or already-completed operations.
- Do not redirect authenticated REST requests. Disable SDK cross-origin redirect-following for credential-bearing calls.

### 13.2 Public response envelope

Successful object responses use the object schema directly. Lists use `data` and optional `next_cursor`. Tool execution returns a call identity/outcome and a result when available:

```json
{
  "call_id": "call_EXAMPLE",
  "status": "succeeded",
  "result_available": true,
  "result": {
    "content": [{ "type": "text", "text": "Example issue data" }],
    "is_error": false
  }
}
```

Example error, including a call ID when one exists:

```json
{
  "error": {
    "code": "outcome_unknown",
    "message": "The upstream action may have completed. Do not automatically repeat it.",
    "request_id": "req_EXAMPLE",
    "retryable": false,
    "details": { "call_id": "call_EXAMPLE" }
  }
}
```

### 13.3 Stable errors

| HTTP | Code | Meaning |
|---|---|---|
| 400 | `invalid_request` | Invalid headers/body/parameters |
| 400 | `invalid_cursor` | Expired, malformed, or context-mismatched continuation cursor |
| 401 | `unauthenticated` | Missing/invalid/revoked/expired applicable credential |
| 403 | `permission_denied` | Known connection/tool outside permitted policy |
| 404 | `not_found` | Missing object or no visibility to it |
| 409 | `connection_reauth_required` | Verified credential is no longer usable |
| 409 | `connection_disabled` | Visible connection is not executable |
| 409 | `idempotency_conflict` | Same key, different logical request |
| 409 | `provider_account_mismatch` | Reconnect attempted to replace identity |
| 410 | `connect_session_expired` | Onboarding transaction expired |
| 412 | `revision_conflict` | Stale optimistic update |
| 422 | `invalid_arguments` | Tool input fails reviewed schema |
| 422 | `unsupported_constraint` | Cannot enforce requested restriction |
| 428 | `precondition_required` | Required optimistic-update header missing |
| 429 | `rate_limited` | Local or mapped upstream limit, safe retry metadata only |
| 502 | `upstream_unavailable` | Provider/transport failure with known safe semantics |
| 502 | `unsupported_upstream_interaction` | Upstream requires a feature outside the supported profile; non-execution is known |
| 502 | `outcome_unknown` | Action may have executed; automatic retry prohibited |
| 503 | `dependency_unavailable` | DB/vault/key service unavailable; fail closed |
| 504 | `deadline_exceeded` | Only if non-execution/safe read outcome is known; otherwise unknown |

`202` returns the existing call with `status=running` for a concurrent idempotent replay and never means a second queued dispatch. A provider `isError` tool result normally returns HTTP 200 with `status=tool_error`, not a RAVN auth error.

### 13.4 Helper and example behavior

- Ship one small TypeScript helper for immutable user context, connect/complete, connection management, session issuance, and MCP client configuration. A thin REST call wrapper is an optional convenience for backend execution.
- The `forUser({userId, tenantId?})` handle does not log anyone in; omission of tenant is valid only for configured single-tenant applications. Never mutate a globally shared SDK user's identity between requests.
- The OAuth completion helper requires an application login/session-store hook. It verifies/claims the original browser transaction, retains recovery state across transient errors, and marks it complete only after success. Raw HTTP is supported, but the safe helper is the recommended web integration.
- Use run-appropriate session lifetimes first. Longer-run renewal is initiated by trusted backend code or a helper invoking an explicitly supplied backend callback. The agent never receives the broad app key. Optional per-call idempotency follows section 11.5.
- Typed errors expose safe codes/request IDs and outcomes. No token, connect ticket, completion code, auth header, or unsanitized provider error may leak through exception serialization or telemetry.
- Keep credential retrieval, refresh, and policy logic in the server. REST/MCP protocol conversion is RAVN's responsibility; ordinary MCP users do not build a custom tool dispatcher.
- Provide a TypeScript web example, raw HTTP examples, and a small Python HTTP/MCP-client example. A full Python SDK is deferred. Test existing client libraries instead of claiming framework-independent three-line OAuth or universal MCP write compatibility.

### 13.5 Activity listing and diagnostic metadata

Core `GET /v1/calls` and `ravn calls list` answer “what tools did this user's agent call yesterday?” without requiring a dashboard or a known call ID.

- Authenticate the application key and existing user/conditional-tenant headers. Query only that actor's app/tenant namespace; neither a session ID nor a filter grants access. Runtime tokens cannot use this API.
- Optional filters: `connection_id`, `session_id`, `status`, `created_from` (inclusive), and `created_before` (exclusive). Times are UTC RFC 3339. First-page upper bound defaults to server now; lower bound defaults to 24 hours before that bound. Reject a future upper bound, an inverted/empty interval, or a range over 30 days.
- Return newest first by `(created_at, call_id)`, default 50 and maximum 100 rows. Use keyset pagination, never arbitrary offsets. The opaque five-minute cursor binds principal, normalized filters, page size, initial upper bound, and last ordering key. Continuations may omit filters/limit to reuse them; supplied values must match. Preserve whether a parameter was omitted before applying defaults. Reject expired/mismatched cursors with `400 invalid_cursor`. This bounds creation-time membership, not a frozen snapshot of changing call statuses.
- Summaries contain call, connection, and originating session ID (null for REST), tool, status, timestamps, duration when measurable, and a safe error code. Include `arguments_fingerprint` plus `fingerprint_key_id` when arguments were validated. Never include raw arguments, result content, credentials, or the original idempotency key, even if optional result retention is enabled.
- The argument fingerprint is a domain-separated HMAC-SHA-256 over canonical arguments and app/tenant/actor/connection/tool context. It supports comparing validated arguments within a context/key version, not reconstructing what was written or deduplicating requests. Key rotation can change it. Pre-validation rejections may have no fingerprint; unauthenticated requests never create user-visible call records.
- Sanitized activity remains visible to a still-authorized owner after session expiry/revocation or connection disconnect, for the retention period. Preserve namespace and session/connection tombstones accordingly. For service-owned extensions, current ACLs still govern visibility. Optional result-body retrieval additionally requires current execution authorization; history is not an execution capability.
- Arbitrary redacted-argument logging is not a core toggle: reliable field-level redaction needs a reviewed per-tool schema and explicit retention controls. Raw content stays off by default. A fingerprint identifies equality, not the contents of a comment; inspect the provider or explicitly enable the separately governed result-cache extension when appropriate.

`GET /v1/calls/{call_id}` supplies the same diagnostic fields plus result-availability information; result bodies are returned only under the opt-in cache and current authorization. The richer `/v1/events` query remains an extension.

## 14. Persistence model

### 14.1 Storage rules

SQLite uses WAL, foreign keys, a 5-second busy timeout, and `synchronous=FULL`. The database resides on a local persistent filesystem, not NFS/shared network storage. Hold an OS-level exclusive deployment lock so a second active process fails startup; SQLite writer locking alone is insufficient for process-local refresh/admission coordination.

Use SQL migrations packaged with the Python distribution, applied transactionally before readiness. Refuse unknown newer schema versions. No automatic destructive migration or data rewrite during startup. Configuration changes and database authority-state transitions must be serialized through the admission gate.

### 14.2 Required records and constraints

`app_id`, `tenant_id`, and owner columns below are repeated deliberately. Every resource query includes its security namespace; use composite foreign keys/indexes to make accidental unscoped joins harder.

| Table | Important fields / constraints |
|---|---|
| `applications` | `id PK`, `tenant_mode` (`single`/`multi`), `enabled`, `security_epoch`, `config_revision`, timestamps |
| `app_keys` | `id PK`, `app_id FK`, `secret_hash UNIQUE`, label, created/revoked times; plaintext never stored |
| `integrations` | `(app_id,id) PK`, connector, config revision/hash, enabled state, reviewed manifest hash; structural config is operator-owned |
| `connections` | `(app_id,tenant_id,id) PK`, integration FK, owner kind, owner user or service binding, stable provider account ID, status/reason, epoch, revision, credential version, optional restrictions JSON, timestamps |
| `managed_secrets` | `(app_id,tenant_id,subject_id,version) PK`, key ID, nonce, ciphertext, schema version; encrypted bundle only |
| `connect_sessions` | `(app_id,tenant_id,id) PK`, actor, integration revision, return URL, app state, state/status, secret hashes, encrypted temporary fields, target reconnect ID/epoch, expiry, staged-secret reference, completed connection ID |
| `refresh_attempts` | attempt ID, connection composite FK, expected credential version/epoch, `started/succeeded/failed/uncertain`, timestamps, safe reason; no raw token response |
| `calls` | `(app_id,tenant_id,id) PK`, actor, connection FK, nullable originating session ID, tool, request ID, nullable idempotency key/request fingerprint/key ID, diagnostic argument HMAC/key ID, internal dispatch state, public outcome, safe error code, timestamps/duration, integration/connection policy revisions |
| `events` | random ID + ordering timestamp/sequence, app/tenant/actor, connection/call references, event type, safe metadata; no args/results/secrets |
| `sessions` (core for MCP) | token public ID/hash, app/tenant/actor, connection/epoch, issuing key, app security epoch, ceiling JSON, expiry/revoked timestamps |
| `service_bindings` / `service_acl` (extension) | operator-configured connection/secret reference and exact permitted app/tenant/service-principal entries |
| `result_cache` (extension) | call composite FK, encrypted result/key metadata, expiry; disabled by default |
| `discovery_cursors` | hashed cursor ID, security-context fingerprint, catalog offset/fingerprint, expiry; no raw credential |
| `activity_cursors` | hashed cursor ID, actor namespace, normalized filters/page size/upper bound, last ordering key, expiry; no raw args/results |

Required uniqueness/indexes:

- `calls(app_id,tenant_id,actor_id,idempotency_key)` unique when key is present.
- Connections indexed by `(app_id,tenant_id,owner_user_id,status,created_at,id)`.
- Connect sessions indexed by expiry/status and reconnect target/epoch.
- Sessions indexed by connection/epoch, issuing key, expiry, and actor.
- Events/calls indexed by actor namespace and descending `(created_at,id)`. Calls also have actor-scoped connection/time and session/time indexes for activity queries.
- Every managed secret/staging reference must resolve within the same app/tenant. Ownership cannot be changed with PATCH.
- Labels and notes are untrusted display data, not query fragments or authorization material.

### 14.3 Atomic operations

| Operation | Must be one serialized commit |
|---|---|
| OAuth activation | consume completion code, validate state/epoch, bind verified account, attach secret version, activate connection, event |
| Managed refresh | compare version/epoch, replace encrypted token bundle, update attempt/version, event |
| Disconnect | state/epoch change, invalidate pending reconnects and sessions, cleanup work, event |
| Policy reduction | revision update and event; session current-policy checks apply immediately |
| Dispatch admission | final checks + call transition and admission event |
| Terminal outcome | call status/error + optional encrypted result + event |

External network operations never occur inside SQLite write transactions. If a remote action succeeded and the terminal transaction fails, the persisted `dispatching` record remains a warning against replay. Broker availability does not override conservative side-effect handling.

### 14.4 Retention and cleanup

- OAuth transaction secrets: remove immediately when terminal; sweep expired transactions every minute. Keep sanitized transaction status up to 24 hours.
- Call identities/idempotency fingerprints and operational events: 30 days by default, configurable explicitly. Document storage growth and the dedupe window.
- Session provenance/tombstones remain while retained calls reference them (normally 30 days after the last call). Token verifiers may be removed after expiry/revocation; history queries do not require an active runtime token. Connection cleanup similarly preserves referenced sanitized metadata until call expiry.
- Activity cursors expire after five minutes; sweep without extending the call-retention window.
- Optional retained results: at most 15 minutes, encrypted; default off.
- Disconnected-connection metadata: 30 days for diagnostics; credentials deleted after cleanup policy, not retained simply for audit.
- External secret references: detach only; never erase the source vault entry.
- Backups follow separate customer retention. Deleting a live record does not promise immediate cryptographic erasure of backups.

## 15. Configuration and CLI

### 15.1 Minimal production configuration shape

This YAML is a proposed configuration schema. Secret references are typed values resolved by the operator, not arbitrary template evaluation. Never place actual secrets in committed examples.

```yaml
version: 1
deployment_id: ravn-prod-eu

server:
  listen: 127.0.0.1:8787
  public_url: https://ravn.example.com
  admin_socket: /run/ravn/admin.sock

storage:
  sqlite_path: /var/lib/ravn/ravn.db

sessions:
  default_ttl_seconds: 3600
  max_ttl_seconds: 14400

secrets:
  managed:
    type: encrypted_sqlite
    active_key_id: key-2026-09
    key_source:
      type: file
      path: /run/secrets/ravn-master-key

applications:
  - id: support-assistant
    enabled: true
    tenant_mode: single  # use multi for SaaS; tenant header then becomes mandatory
    return_urls:
      - https://app.example.com/settings/github/complete

integrations:
  - id: github-cloud
    app_id: support-assistant
    connector: github_cloud
    transport: remote_mcp
    endpoint: https://api.githubcopilot.com/mcp/
    manifest: builtin:github-issues-v1
    auth:
      personal_bearer_import: true
      oauth:
        profile: github_app_user
        client_id_source:
          type: env
          name: RAVN_GITHUB_CLIENT_ID
        client_secret_source:
          type: file
          path: /run/secrets/github-client-secret
        callback_url: https://ravn.example.com/oauth/callback/github-cloud
        use_pkce: true
        require_expiring_user_tokens: true
    outbound:
      allowed_origins:
        - https://api.githubcopilot.com
        - https://api.github.com
        - https://github.com
      allow_private_networks: false

limits:
  request_bytes: 1048576
  result_bytes: 4194304
  call_timeout_seconds: 30
```

Configure the GitHub App's repository permissions and installations on GitHub; this file cannot grant or widen them. `github_app_user` does not send `repo` scopes. `require_expiring_user_tokens` validates the configured onboarding response and rejects a new connection without the required refresh/expiry fields; it does not invent expiration for imported PATs. The first read-only manifest causes RAVN to generate the provider read-only/tool-selection headers. Enable comments only with the reviewed write manifest, provider permission, and write-capable client path.

`tenant_mode` is fixed once application data exists. Single mode resolves omitted tenant to `default`; multi mode requires the header. This is an operator-owned setting, never a caller-controlled opt-out of isolation.

### 15.2 Configuration application

- Parse strictly; reject duplicate keys, unknown fields, invalid origins, missing key sources, and unsupported constraints before serving requests.
- `ravn config check` validates without activation and reports safe diagnostics only.
- `ravn config reload` validates a complete replacement snapshot, then applies it atomically. Existing connections are not deleted just because configuration is removed; affected integrations are disabled.
- Secret values, account identity, endpoints/audiences, and permission expansions must never change silently under a pre-existing integration identity. Structural changes require a new integration revision/identity and explicit reconnect/migration as appropriate.
- Maintain a config hash/version in events. Config is not persisted with plaintext resolved secrets.

### 15.3 CLI surface

| Command | Behavior |
|---|---|
| `ravn init` | Explicit local development config/storage/key creation; refuses overwrite |
| `ravn serve --config <path>` | Start one process; fail readiness if state/keys/config invalid |
| `ravn config check --config <path>` | Read-only validation |
| `ravn config reload` | Operator-only atomic apply |
| `ravn app-key create --app <id> --label <label>` | Generate key; reveal once; log only key ID |
| `ravn app-key revoke <key-id>` | Routine revocation of backend credential |
| `ravn app-key revoke <key-id> --revoke-sessions` | Compromise containment for issued runtime sessions |
| `ravn integration inspect <id>` | Discover candidate metadata from a registered destination; never auto-approve |
| `ravn connections list --app <id> --tenant <id> --user <id>` | Scoped metadata listing |
| `ravn connections disconnect <id> ...` | Explicit scoped disconnect; displays local vs provider outcome |
| `ravn calls list --app <id> --user <id> [--tenant <id>] [--connection <id>] [--session <id>] [--from <time>] [--before <time>] [--status <status>]` | Actor-scoped metadata-only activity; same filter/time/cursor limits as REST |
| `ravn calls show <id> ...` | Sanitized outcome/diagnostic metadata |
| `ravn sessions revoke <id> ...` | Core direct-client session revocation |
| `ravn doctor` | Redacted local configuration/dependency checks; no unsolicited external mutation |

CLI supports `--json` for non-secret metadata and machine-readable errors. Secrets are not command-line arguments: use an owner-only file, environment injection, or a non-echoing interactive prompt. Displayed one-time keys are intentionally sensitive; never emit them in general logs or telemetry.

### 15.4 Operator socket

The owner-only Unix socket is not the app-facing API. Its directory is mode `0700`, socket `0600`; validate local peer UID where supported. Only the service owner/operator should mount it. No unauthenticated TCP equivalent is shipped.

Internal operator routes: `POST /admin/v1/app-keys`, `POST /admin/v1/app-keys/{id}/revoke`, `POST /admin/v1/config/reload`, `POST /admin/v1/integrations/{id}/inspect`, and explicitly scoped read/disable/revoke operations used by the CLI. Reuse the same service-layer authorization/state logic, with an explicit operator principal, not arbitrary SQL shortcuts.

An operator may manage all applications in the deployment; an application key may not. The Unix socket, DB/key files, and process host are trusted administrative boundaries. Windows-native operation is not promised for v1; use a Linux container or add a separately reviewed local-auth implementation.

## 16. Network security and operational limits

### 16.1 Outbound destination enforcement

1. Destinations come only from operator-approved integration/auth profiles.
2. Require HTTPS in production. Reject URL userinfo/fragments, unsupported schemes, ambiguous host encodings, and unapproved ports.
3. Normalize and validate hosts; resolve DNS at connection time, validate all chosen addresses, and dial a pinned allowed address. Preserve the expected hostname for TLS verification. Revalidate on reconnect.
4. Public SaaS profiles deny loopback, private, link-local, multicast, reserved, and metadata endpoints, including IPv4-mapped IPv6 forms.
5. Internal-service profiles require exact operator host/port and explicit CIDR permissions. A user cannot enable private-network access through tool arguments.
6. Disable automatic redirects for token and execution calls. Any provider-specific exception requires exact target revalidation and no cross-origin forwarding of credentials.
7. Do not forward incoming `Authorization`, cookies, `Host`, `X-Forwarded-*`, or unapproved custom headers. Generate upstream auth/protocol headers yourself.
8. Disable automatic environment proxy behavior unless an operator explicitly configures a reviewed egress proxy. Proxy use is part of the trust boundary.
9. Apply the same restrictions to OAuth metadata/discovery, identity validation, schema loading, and health probes. Never auto-fetch icons, schema `$ref` URLs, or result links.

These controls protect the broker. An upstream tool that itself fetches arbitrary URLs or runs arbitrary code has its own security boundary and must not be assumed safe because its MCP endpoint is allowlisted.

### 16.2 Inbound deployment

- Local development binds loopback. Production runs behind TLS termination or explicitly configured TLS; trust forwarded headers only from configured proxy addresses.
- Fixed public base URL determines callback origins; never infer it from arbitrary `Host`/forwarded headers.
- No broad CORS policy is required: management calls originate from trusted backends. Browser start/callback paths support navigation, not credential-bearing cross-origin API access.
- Request header limits and read-header deadlines prevent slow-header abuse. Use per-route/context timeouts compatible with bounded MCP streaming instead of a blanket connection write timeout that silently truncates valid calls.
- Run as a non-root user with read-only root filesystem where practical; writable persistent directory and secret mounts narrowly scoped. No Docker socket or shell execution privileges.

### 16.3 Initial limits

These are starting defaults, not measured universal limits. Operators may lower them; documented caps prevent unbounded values.

| Limit | Default |
|---|---|
| REST/MCP request body | 1 MiB |
| Tool result after bounded decoding | 4 MiB |
| Reviewed tool schema | 256 KiB per tool; depth 32; no remote references |
| Catalog | 100 tools maximum per integration in v1 |
| Legacy upstream session pool | 128 contexts maximum; five-minute idle expiry; no cross-context auth sharing |
| GitHub comment body | 64 KiB maximum; obey tighter provider limits |
| Tool call deadline | 30 seconds; operator maximum 120 seconds |
| OAuth/token/vault operation | 10-second deadline |
| HTTP read-header timeout | 5 seconds |
| Concurrent calls | 64 globally; 16/app; 4/connection |
| Waiting requests | Bounded queue of 128; excess returns 429/503 before dispatch |
| Connection starts | 10/minute per app/tenant/actor, plus an operator-set global cap |
| Runtime session TTL | One hour default; 1-240 minutes per request within the configured maximum |
| Connect transaction | 10 minutes total; completion at most 5 minutes within it |
| Schema discovery cache | 30 seconds, security-context partitioned |

Do not hold global queue slots during OAuth human consent. Apply rate limits to unauthenticated failures too, keyed without leaking whether an application/user exists.

## 17. Deployment, recovery, and observability

### 17.1 Startup and readiness

Startup order: parse config -> load durable keys -> acquire single-instance lock -> open/migrate DB -> reconcile configuration -> recover incomplete state -> start HTTP/admin listeners -> mark ready.

- `/healthz` means the process is alive; no database details or secrets.
- `/readyz` means config, database, keys, and required local state are usable. Do not require every external SaaS to be online for global readiness.
- A failing external Vault binding fails calls that need it; surface dependency status without turning unrelated working connections into failures.
- Restart does not retry incomplete writes/refreshes automatically. Recover `dispatching` calls to unknown and uncertain refresh attempts to reconnect-required when safe recovery cannot be proved.

Graceful shutdown stops accepting new calls, permits up to 30 seconds for admitted operations, records remaining uncertainty, closes upstream sessions, flushes durable state, and releases the deployment lock. Never report a cancelled write as definitely unexecuted merely because shutdown timed out.

### 17.2 Backup and restore

- Use SQLite's supported online backup mechanism or stop/checkpoint safely. Copying only the main DB file while WAL is active is not a complete backup procedure.
- Encrypt backup media and manage its key access separately. Document which master-key versions are needed to restore.
- Restoring an older database can resurrect revoked sessions, replay consumed OAuth transactions, and lose idempotency history. Restore requires maintenance mode, a new deployment security epoch, invalidation of all pending connect/runtime sessions, and reconciliation of unresolved writes before resuming.
- Do not replay business operations from an older backup. Application-level idempotency and provider reconciliation are needed for high-impact workflows.
- Publish a tested runbook for migration backup/restore. No zero-downtime multi-replica guarantee exists in v1.

### 17.3 Logs and events

Every relevant action records timestamp, application, tenant, actor, connection ID, originating runtime session ID when applicable, integration/tool name, request/call ID, decision/outcome, safe reason code, duration, and applicable policy/credential revision identifiers. Validated tool calls, keyed or keyless, also store the diagnostic argument HMAC/key ID described in section 13.5. Never include provider tokens, app/session secrets, raw args/results, authorization/completion codes, URL query strings, or upstream authentication bodies.

Account names/user IDs can themselves be personal information; minimize/export according to customer policy. Do not put user/connection IDs into high-cardinality metrics labels.

Recommended metrics: requests and outcomes by route/integration, local authorization denials, upstream latency, credential refresh success/failure/uncertainty, DB/vault errors, queue depth, active requests, schema drift, disconnected connections, and unknown write outcomes. Metrics are operator-only, not public by default.

Audit events are durable application records but not cryptographically immutable against an operator with DB access. External append-only audit export is a future extension; avoid stronger compliance claims.

### 17.4 Proposed performance acceptance target

On a documented 2-vCPU/4-GiB Linux test machine with local SSD and a fixed-latency fake upstream, measure 100 read calls/second and 50 concurrent clients. Target p95 broker overhead under 25 ms excluding upstream latency, OAuth refresh, and external vault I/O; report p50/p95/p99, memory, saturation, and error rate.

This is an engineering target to validate, not an existing benchmark or service SLA. Security/isolation correctness takes priority over the target. Add a real-provider latency breakdown without load-testing third-party APIs beyond authorized limits.

## 18. Testing and release acceptance

Tests use synthetic credentials and local fake OAuth/MCP/vault servers by default. Live GitHub checks require explicitly supplied disposable test accounts/repositories and authorization for writes. Do not use real customer data or committed tokens.

### 18.1 Core security and lifecycle tests

| ID | Test | Required outcome |
|---|---|---|
| A01 | Wrong/revoked application key | 401; no upstream call |
| A02 | Valid key attempts another application's resource | 404; no metadata leak |
| A03 | Same app, wrong tenant or personal owner | 404 across get/list/tools/call/disconnect/history |
| A04 | Model supplies actor/app/connection overrides | Rejected or ignored only where explicitly non-authoritative; never changes identity |
| A05 | Duplicate auth/identity headers | Rejected deterministically |
| O01 | OAuth callback with wrong state/cookie/provider | No token activation |
| O02 | Attacker-created connect link opened by another user | Cannot complete under attacker's browser transaction |
| O03 | Completion code/state reused or polled | Replay denied; polling never exposes secrets |
| O04 | App return URL modified, wildcard/origin confusion | Rejected before redirect |
| O05 | Provider callback succeeds but app never completes | Credential remains unusable and expires |
| O06 | Token exchange/completion response lost | No repeated unsafe exchange or duplicate connection; status recovery works |
| O07 | Reconnect uses a different provider account | Rejected; old connection unchanged |
| O08 | Disconnect races with OAuth reconnect completion | Disconnect epoch wins; no resurrection |
| C01 | Encrypted blob moved to another tenant/connection | AEAD authentication fails |
| C02 | Missing/wrong encryption key after restart | Not ready; no new silent key |
| C03 | Concurrent refresh attempts | At most one in-flight refresh per connection; bundle version coherent |
| C04 | Rotated refresh response lost/process crash | Uncertainty surfaced; old token not blindly retried |
| C05 | Disconnect during refresh/vault fetch | No admission after disconnect commit |
| C06 | Provider 403/rate limit is not invalid grant | Does not unnecessarily destroy a working connection |
| C07 | Imported external vault connection disconnect | Binding removed; source secret untouched |
| E01 | Same idempotency key, concurrent same request | One local dispatch; other requests observe state |
| E02 | Same key, changed tool/arguments/connection | 409; no dispatch |
| E03 | Upstream writes then drops response | Unknown; no automatic retry |
| E04 | Restart with durable dispatching record | Unknown, never queued for replay |
| E05 | Completed result not retained | Existing outcome returned; no re-execution |
| E06 | Permission removed before result replay | No unauthorized result disclosure; retained sanitized history follows current owner/ACL visibility |
| E07 | Authorized keyless comment over ordinary MCP or REST | Executes without a custom key adapter; recorded in the call ledger |
| E08 | Identical keyless submissions or reused JSON-RPC ID | Not falsely deduplicated; documentation warns of possible duplicate actions |
| E09 | Same logical key across MCP and REST | Same namespace/fingerprint yields one local dispatch |
| E10 | Optional key malformed; valid MCP metadata supplied | Malformed input rejected; valid RAVN key consumed and not sent upstream |
| H01 | List calls by user, connection, session, status, and time | Scoped metadata only; no args/results even with result cache enabled |
| H02 | Cursor crossover, mismatch, expiry, oversized time range | Rejected; no cross-user/tenant metadata or unbounded query |
| H03 | Session expired/revoked or connection disconnected | Still-authorized owner can inspect retained sanitized history, not execute |
| F01 | Concurrent async DB operations and request cancellation | No transaction interleaving; admission/outcome commits coherent |
| F02 | Second server process or multi-worker launch | Fails deployment guard; no duplicate process-local refresh coordination |
| F03 | MCP mounted app, lifecycle, and validation errors | Runtime auth applies on every supported route; no credential echoed by validation or error serialization |
| N01 | Redirect/DNS rebinding/private metadata target | No credential-bearing request to disallowed target |
| N02 | Malicious remote schema/icon/result link | No automatic fetch |
| N03 | Raw upstream auth error echoes token | Safe error; no secret in logs/exception serialization |
| N04 | Excess body/schema/queue/concurrency | Bounded rejection without memory exhaustion |

### 18.2 MCP/session core and extension tests

S01–S03, S06–S07, P01, and M01–M07 are core release gates. S04–S05 and P02–P03 apply to the fine-policy extension; V01–V02 and C07 apply to the vault/service-owned extension.

| ID | Test | Required outcome |
|---|---|---|
| S01 | Runtime token calls management API | Rejected |
| S02 | Runtime token selects different actor/connection | Rejected |
| S03 | Expired/revoked token with valid legacy MCP session ID | Rejected |
| S04 | Current connection policy narrows | Next admitted call denied as appropriate |
| S05 | Connection policy expands | Existing session ceiling does not expand |
| S06 | App-key compromise revocation | Issued sessions invalidated as requested |
| S07 | Default TTL, configured/requested bounds, expiry during admitted call | One-hour default; invalid TTL rejected, not shortened; next admission denied after expiry without replay/cancelling an admitted write |
| P01 | Hidden tool invoked directly | Denied despite bypassing discovery |
| P02 | Unsupported resource restriction | Explicit error; no silent unrestricted call |
| P03 | GitHub resource/path/case/extra-argument bypass | Reviewed namespace constraint holds |
| M01 | Modern/legacy protocol matrix both directions | Version-correct transport and metadata |
| M02 | Protocol fallback after failed write | No replay |
| M03 | Concurrent Alice/Bob upstream clients/cursors | No credential, cookie, schema, session, or result crossover |
| M04 | Legacy reverse request or modern input-required result | Explicit unsupported outcome, never false success |
| M05 | Optional keyed MCP write retried, including after token replacement | Same logical key; one dispatch; no static client-wide key |
| M06 | Completed MCP replay without body; runtime renewal | Stable outcome error without re-execution; renewed transport uses only a new session token |
| M07 | Keyless write followed by timeout/lost response | RAVN does not retry; persists unknown and delivers outcome_unknown only if the response channel remains available |
| V01 | Runtime requests arbitrary vault reference | Rejected; operator allowlist cannot be bypassed |
| V02 | Same-tenant user selects shared service connection without ACL | Denied |

### 18.3 GitHub live gates

**Milestone 0 was skipped at the user's request so milestone 1 could proceed. It has not passed, and live GitHub interoperability remains unverified.** Before claiming the proposed GitHub App profile works, obtain a disposable token through our proposed GitHub App user-authorization flow, not just a manually imported PAT. Verify stable account identity, authenticated remote tools/list, a private disposable issue read, one explicitly authorized disposable comment, and an expected provider-permission denial. Any future live write requires a separately authorized disposable target. If any part fails, stop and review the adapter/auth architecture; do not silently substitute broader OAuth App or installation authority.

Before advertising any supported auth mode, also verify it against GitHub's actual current service: account identity, authenticated tools/list, read of a private disposable issue, expected denial with insufficient provider permission, and local disconnect. GitHub App user OAuth additionally requires real callback/PKCE, configured expiration/refresh, selected-repository access, and expected denial when the app or user lacks access. Demonstrate installation guidance without substituting installation authority for the user's token. Comment support requires one authorized disposable write, reliable call ID/outcome recording, and fake-server ambiguous-write tests.

Record sanitized protocol/schema fixtures and exact dependency/connector versions. A test double is not proof of hosted-provider interoperability. Do not advertise GitHub App installation tokens or Enterprise Server support without their own live gates.

### 18.4 Developer-experience gates

- A new developer can complete the documented GitHub flow without any RAVN dashboard.
- The same authorized connection executes through an existing MCP client and REST with identical policy and call-ledger decisions; developers need only one execution interface.
- The TypeScript helper, raw HTTP, and Python example pass equivalent onboarding/management fixtures. Client users need not install the server's Python runtime when using a deployed RAVN endpoint.
- A single-tenant example omits tenant successfully; multi-tenant omission fails, and identical user IDs in two tenants remain isolated.
- An operator-preconfigured personal connection supports repeated internal agent runs without repeated account consent. Runtime access remains authenticated and expires/revokes correctly.
- Helper code does not implement separate credential refresh, scopes, or authorization logic.
- Examples never pass the application key into an untrusted agent runtime and never derive actor headers from model arguments.
- CLI help, return codes, config errors, and reconnect diagnostics identify the next action without leaking secrets.
- Release includes checksums/signatures, SBOM/license inventory, pinned dependencies, vulnerability scanning, tests, and a clear compatibility matrix.

## 19. Build plan and decision log

### 19.1 Minimal milestones

0. **GitHub compatibility proof — skipped by explicit user request:** the intended proof uses a disposable GitHub App user token from our own proposed flow for identity, remote tools/list, private issue read, one authorized disposable comment, and expected denial. No live test or write was performed. The waiver permits local implementation, not a compatibility claim.
1. **Read-only vertical slice — implemented locally:** Python/FastAPI/SQLite, mounted Python MCP SDK, shared executor, and bootstrap CLI; personal PAT import -> account validation/encryption -> session issuance -> existing MCP client discovery/read -> call status -> revoke/disconnect. Local tests cover Alice/Bob and tenant isolation, one-process locking, cancellation-safe transactions, restart recovery, and real CLI/socket startup. Reviewed upstream schema hashes must be supplied explicitly; none were auto-approved without the waived live test.
2. **Real onboarding:** GitHub App installation guidance and user authorization -> callback/staged completion -> refresh -> same-account reconnect -> cancellation/expiry. Ship the browser-binding helper and test provider-token rotation separately from runtime-session replacement.
3. **Developer-ready MVP:** TypeScript helper, Python CLI, core activity listing, REST execution alternative through the shared engine, HTTP/Python examples, keyed and keyless comment writes, compatibility tests, and container deployment/recovery runbooks.

The token-import slice is not the full OAuth product. Inbound MCP and session lifecycle are not deferred extensions. Ordinary tested clients can write after the executor/client safety gates pass; optional keys improve retry handling without becoming a required integration step.

### 19.2 Independent extensions

After the core works, prioritize from demonstrated demand:

1. Optional exact tool/repository restrictions.
2. Service-owned bindings and read-only external Vault references.
3. Additional reviewed remote MCP integrations/providers.
4. A full Python SDK, when examples and standard clients are insufficient.
5. Extended event queries and encrypted short-lived result retention.

These need not ship together. Keep PostgreSQL/replicas, full client-to-RAVN OAuth, embedded provider tools, arbitrary OpenAPI import, and delegation in separate design reviews.

### 19.3 Alternatives and rationale

| Alternative | Decision |
|---|---|
| SDK-only, no broker | Useful for trusted single-process apps, but duplicates credential lifecycle across runtimes and cannot isolate credentials from agent code; not the selected product |
| Python/FastAPI backend | Selected for team familiarity and an async, I/O-heavy service; distribution is a container/package |
| Go backend | Viable alternative for native-binary distribution; not selected for this implementation |
| TypeScript backend | Viable alternative; not needed to serve TypeScript clients |
| Rust backend | Viable; not required solely for anticipated performance in this initial I/O-heavy scope |
| JWT sessions and own issuer | Opaque server-side sessions make local revocation simpler; federation/offline verification is not a v1 goal |
| Mandatory session token for every backend call | Rejected; trusted REST calls use app credentials directly; MCP clients use connection-bound runtime tokens |
| MCP-only with no management surface | Rejected; account authorization and revocation still need trusted setup, even when hidden behind helpers |
| REST-only agent integration | Rejected as the default; existing MCP clients should not need a custom tool dispatcher |
| Delete tenant from the data model | Rejected; default it for single-tenant apps, keep explicit isolation for multi-tenant SaaS |
| Two full SDKs at launch | Deferred; one small TypeScript helper and interoperable HTTP/MCP examples first |
| Mandatory agent scope matrix | Rejected; connection ownership is mandatory, extra narrowing is optional |
| Shared superuser credential for all customers | Rejected unless explicitly a service-owned business workflow with separate authorization |
| Expose arbitrary upstream tools on discovery | Rejected; registration approves a reviewed support surface |
| Mandatory enterprise vault/Redis/Postgres | Rejected for first milestone; separate adapters/features when required |
| Automatic retries for reliability | Only safe operations; ambiguous writes/rotating refresh are surfaced conservatively |

### 19.4 Questions to settle before public release

These are not blockers to the first vertical slice; defaults above are sufficient to begin. They require explicit release decisions:

- Exact Python, FastAPI, Uvicorn, MCP SDK, and dependency versions; supported image/OS/CPU matrix and validated dependency licenses.
- Complete the waived live compatibility proof before advertising the GitHub App user-token profile; finalize supported installation/user-consent UX and repeat the gate on the pinned release. No silent broader OAuth App fallback.
- Tested MCP client versions, fixed-token runs and optional auth replacement, keyless writes, and optional metadata-key behavior; which extension, if any, to add beyond core: resource restrictions or company vault bindings.
- Final package names and Apache-2.0 licensing approval; no assumption that an unregistered package name is available.
- Data retention defaults and optional result-cache settings for the first deployment audience.
- Measured capacity and limits from acceptance tests; no marketing performance promise before measurement.

## 20. Reference implementations and standards

References were checked during design on 2026-09-11. They inform the design, not certify the implementation. Features claimed by another repository are not evidence that RAVN has implemented them.

| Reference | What to learn / avoid assuming |
|---|---|
| [Obot](https://github.com/obot-platform/obot), [Go dependencies](https://github.com/obot-platform/obot/blob/main/go.mod) | Go MCP/auth/credential infrastructure; do not inherit the entire broader platform |
| [Nango provider definitions](https://github.com/NangoHQ/nango/tree/master/packages/providers), [headless authentication](https://nango.dev/docs/guides/auth/customize-connect-ui) | Separate provider-specific auth configuration and connection lifecycle from UI; inspect [ELv2 restrictions](https://github.com/NangoHQ/nango/blob/master/LICENSE) before reuse |
| [Agentgateway](https://github.com/agentgateway/agentgateway) | Terminating gateway/protocol/security patterns; not automatically a hosted per-user connection product |
| [Composio SDKs](https://github.com/ComposioHQ/composio) | Thin user-scoped SDK experience; public SDK source is not the full hosted backend |
| [Grantex SDK](https://github.com/mishrasanjeev/grantex/tree/main/packages/sdk-py) | SDK resource organization; do not adopt its broader protocol/features merely to wrap HTTP |
| [Agentic Fabriq SDK](https://github.com/agenticfabriq/agentic-fabriq-sdk) | Client/app/user separation; does not establish open-source availability of its complete gateway/vault platform |
| [Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk), [FastAPI](https://fastapi.tiangolo.com/async/) | Selected protocol/server libraries; actual mounted-app behavior and compatibility still require tests |
| [GitHub remote MCP](https://github.com/github/github-mcp-server), [remote settings](https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md) | Supported upstream auth/configuration; live tests still required |

Standards linked at the relevant sections are the source of protocol requirements. RAVN-specific choices—opaque runtime tokens, staged app completion, one-process SQLite, namespace restrictions, conservative call outcomes—are design decisions, not claims that a standard prescribes this exact product.

## 21. Definition of done

The minimal product is complete when an independent developer can deploy one RAVN service, configure GitHub, connect two users through their own application, configure their existing MCP clients using separate connection-bound tokens, execute supported tools without a custom dispatcher or exposed provider credentials, demonstrate cross-user isolation, handle reconnect/disconnect and uncertain outcomes correctly, use ordinary keyless writes plus optional keyed deduplication without automatic replay, and inspect user/session activity through code or CLI.

It is **not** defined by connector count, the number of policy features, or having a dashboard. Extensions may follow without changing that basic integration experience.
