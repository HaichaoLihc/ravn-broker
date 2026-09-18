# RAVN broker console — proposed design

Date: 2026-09-11\
Status: minimal local implementation available; see [broker console](../../console/README.md)\
Baseline: [implemented milestones 1–2](../../README.md)

Implementation update: the optional loopback console now reuses the supplied HTML's actual components, tokens, fonts and icons. No Sites project or deployment is involved. Core operator queries, local login, revocation, disconnect, activity, read-only settings and a bounded connection access map are implemented. The UI uses a fixed 24-hour activity window; broader API time filters and deployment-level events are not separate UI controls yet. This document remains the fuller design reference, not a production-readiness claim.

## 1. Product decision

Add an **optional operator console for the person running RAVN**. It does not replace the application's end-user UI, and developers can continue using RAVN headlessly.

First version: inspect connections, runtime sessions, recorded calls, and configuration; revoke sessions/application keys and disconnect connections. Keep account import, session issuance, application registration, and schema approval in the existing CLI/backend/configuration workflow. No OAuth wizard, agent registration, fine-grained permission editor, delegation engine, or risk scoring is introduced by this console.

Implemented launch commands:

```sh
ravn serve --console
ravn console
```

The first command enables a separate loopback console listener alongside the existing broker and Unix admin socket. The second authenticates through the owner-only Unix socket and opens a short-lived, single-use console login link. No application key is pasted into the console.

Example: choose application `support`, filter tenant `acme` and user `alice`, open Alice's GitHub connection, inspect its runtime sessions and issue-read calls, then revoke one session. Other sessions keep working. Disconnecting the connection instead blocks new calls through every session bound to it. Neither action promises to cancel a call admitted before revocation committed.

## 2. Reuse the supplied UI, not its old authorization model

Reference: the supplied `Ravn Console.html`. Its embedded template, React components, design tokens, and mock data were inspected as source. Browser rendering of the local attachment was blocked, so visual parity has not been verified.

The artifact contains reusable `NavRail`, `TopBar`, `DataTable`, `Drawer`, `ConfirmModal`, `Toast`, `StatusBadge`, `TimelineRow`, and `AgentGraph` components. Preserve its neutral paper surfaces, violet selection, restrained teal/amber/red status colors, Geist/Geist Mono typography, compact tables, and right-side detail drawers.

| Supplied UI | Broker adaptation |
|---|---|
| Agent graph | **Access map**, inside a connection's detail view; an ownership/session-binding map, not a delegation graph |
| Agents | **Sessions**: connection, owner, expiration, access state, last recorded call |
| Permissions | **Tool access** section in the session drawer; read-only |
| Services | **Connections**; distinguish Alice's GitHub account from the configured GitHub integration |
| Delegation activity | **Activity → Calls** |
| Audit log | **Activity → Events**, explicitly an operational log, not a tamper-proof audit trail |
| Configuration | **Settings → Integrations / Application keys / Server** |
| Delegation rules, Risk center, simulation, Request access | Omit; there is no supporting feature in milestone 1 |

Sidebar: **Connections · Sessions · Activity · Settings**. Start at Connections. Keep the graph as a focused detail tab, so there is no need to invent a deployment-wide agent inventory or build a graph aggregation service.

The top bar has an application selector, an exact tenant filter for multi-tenant applications, and an optional user filter. Deployment ID is a read-only badge. Remove the fabricated organization/environment switchers. Tenant and user are separate fields, never concatenated identifiers. Clearing a filter means “all within this operator's authority,” not a change to authorization.

### Screen contents

**Connections:** account/display name, integration, tenant, user, saved connection state, creation time. Drawer: full namespace, connection ID, revision, verification-at-import time, sessions, recent calls, and Disconnect. Empty state explains the CLI import workflow using placeholders, never real credentials.

**Sessions:** session ID, owner, connection, token expiry/revocation, and current broker access state. Drawer: session tool ceiling, currently configured tool intersection, expiry, and recent calls. Revoke is the only mutation. Do not call sessions permanent “agents.” Do not show an expired/revoked token, or offer to recover one.

**Tool access:** show `issue_read` restricted to `method=get`, and `list_issues` with owner/repo arguments only. Label these “Allowed by RAVN.” GitHub's token/repository permissions are a separate upstream limit and are **not enumerated by milestone 1**. The UI must not claim that all repositories are accessible or that it can edit GitHub permissions.

**Access map:** render a selected connection's owner, its loaded sessions, and configured GitHub MCP destination. Edges mean `owns`, `bound to`, and `routes to`. Fetch the selected connection and a bounded page of its sessions; label “showing N sessions” and offer Load more. No graph endpoint or graph table is needed. Keep status distinctions between revoked/expired tokens and a disabled integration.

**Activity:** Calls table uses time, tenant/user, session, tool, status, duration, and safe error code. Detail drawer adds IDs and diagnostic argument fingerprint, never request arguments or result bodies. Events uses the supplied timeline component. Milestone 1 records admitted calls; pre-admission denials are not a complete existing history. Do not imply that missing records mean no rejected attempts occurred.

**Settings:** display configured integrations, reviewed tool/schema pins, enabled state, TTLs, version, and local readiness. “Configured” or “schema pins present” is not “GitHub verified healthy.” Application keys show ID, label, created/revoked state and a Revoke action; creation remains via CLI. No secret reveal, config file editor, arbitrary URL registration, or live provider probe in this version.

## 3. Runtime and authentication

Keep one Python process, one service instance, one SQLite connection/lock, and one execution engine. Add an optional, distinct FastAPI console app bound to `127.0.0.1:8788`, serving `/console/` assets and `/console/api/v1/*`. Do not expose the existing owner-trusting Unix admin app over TCP. Do not add cookie authentication to `/v1/*` or `/mcp`.

Proposed local-owner login:

1. `ravn console` calls the existing Unix socket using its filesystem ownership boundary.
2. A new socket-only operation issues a random 256-bit ticket: hashed in memory, one use, expires in 60 seconds, bound to this process and configured console origin.
3. Open `/console/#ticket=...`. Frontend removes the fragment immediately and exchanges the ticket in a JSON POST body. Never put the ticket in query strings, logs, analytics, or persistent browser storage.
4. Exchange atomically consumes the ticket and sets an opaque, host-only `HttpOnly`, `SameSite=Strict` browser-session cookie scoped to `/console`. Session records are hashed in memory, have a one-hour absolute lifetime, and disappear on server restart/logout. Ticket and session registries are bounded and expiry-swept.
5. Validate exact Host and Origin; deny cross-origin requests. Mutations require a session-bound CSRF header as well as the cookie. SameSite is defense in depth, not the only CSRF check. Use `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, and frame protection.

HTTP is an explicit local-development exception; the first implementation must refuse non-loopback console binding. HTTPS deployments must use `Secure` cookies. Remote administration, SSO, multiple named operators and RBAC need a separate design before exposing this console. A local-owner session represents the deployment operator, not proof of a particular employee identity. Browser cookies are not isolated by port; the cookie path and independent API authentication are defenses, not a promise of protection from other malicious local processes.

The cookie/CSRF choices follow [OWASP session guidance](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html) and [OWASP CSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html). The local-owner workflow and timeouts above are RAVN design choices.

## 4. Exact additional API surface

All paths in the following table are relative to **`/console/api/v1`**, on the separate console listener. They are new browser-authenticated routes, not public aliases for existing application routes.

| Method and path | Purpose / implementation reuse |
|---|---|
| `POST /auth/exchange` | Consume `{ticket}`; create operator browser session and CSRF token |
| `GET /auth/session` | Current operator session, expiry, and CSRF token; no session bearer value |
| `POST /auth/logout` | Revoke operator browser session and clear cookie; CSRF required |
| `GET /bootstrap` | Sanitized deployment/version/readiness, application inventory, integration metadata, TTLs and feature flags |
| `GET /connections` | Operator-scoped cross-user connection listing; extend shared query layer |
| `GET /connections/{id}` | Sanitized detail, owner namespace and derived broker access state |
| `POST /connections/{id}/disconnect` | Existing atomic disconnect behavior, reached through explicit operator authorization |
| `GET /sessions` | Cross-user session listing; connection/status filters and sanitized tool summaries |
| `GET /sessions/{id}` | Session detail, tool ceiling and current broker limits; no token |
| `POST /sessions/{id}/revoke` | Existing revocation state transition under operator authorization |
| `GET /calls` | New bounded query over the existing call ledger |
| `GET /calls/{id}` | Existing call-status projection under operator authorization |
| `GET /events` | New bounded query over sanitized operational events; explicit application or deployment scope |
| `GET /app-keys` | New metadata-only application-key listing |
| `POST /app-keys/{id}/revoke` | Reuse current key revocation; body includes explicit `revoke_sessions` choice |

One additional **Unix-socket-only** route: `POST /admin/v1/console-tickets`. It fails if the console is disabled and uses the server-configured origin, not a caller-supplied URL. Existing CLI key-creation and revocation routes remain unchanged.

No new `/v1/*` or MCP method is necessary for this console. In particular, do not make application keys eligible for operator API access. An eventual application-facing `GET /v1/calls` can reuse the query layer but is a separate API addition, not required here.

### Scope and response rules

- Application-data queries require `app_id`. Tenant and user filters are optional on listings because the first console principal is a deployment owner. Detail requests require `app_id` and `tenant_id`; mutation bodies carry the same explicit namespace. Server resolves the actual owner, never trusts a browser-provided user as proof of authority. A mismatched namespace returns 404. Bootstrap and authentication have no application scope. The one event-query exception is `GET /events?scope=deployment`, which requires deployment-owner authority, rejects app/tenant/user filters, and returns only events with no app; `scope=application` is the default and requires `app_id`.
- Key actions require `app_id` and must verify the key belongs to it. `revoke_sessions=false` is the explicit routine-revocation default; UI explains that already-issued runtime sessions survive. Compromise mode sets it to true and explains the wider impact.
- Bootstrap includes disabled/unconfigured application records needed to inspect old data and revoke access, not just currently enabled applications. Configuration metadata is allowlisted; never serialize the whole config model, filesystem key paths, token hashes, ciphertext, or secrets.
- Lists return `{data, next_cursor, as_of}`. Default limit 50, maximum 100; stable descending `(created_at,id)` ordering. Bind five-minute opaque cursors to operator session, endpoint, application, all filters and initial query cutoff. No unbounded export or fake counts from the first page.
- Connection/session filters: exact tenant/user/connection as applicable and validated status. Calls add session/tool/status/from/to; events add kind/subject/from/to. Calls/events default to the last 24 hours, maximum requested window 30 days. Timestamps are UTC; display the user's timezone with an exact-time tooltip.
- First version uses fixed ordering, not client-side sorting/search over an incomplete page. Only expose filters the backend implements. No unrestricted SQL, regular-expression filters or arbitrary JSON fields.
- Connection detail supplies `revision`. Disconnect requires matching `If-Match` and returns 412 on a changed active record so the UI refetches and reconfirms. An already-disconnected connection or already-revoked session is an idempotent success.
- Mutations respond only after the state change and audit event commit. No optimistic success toast. If a response is lost, refetch current state before offering retry. Do not implement call replay or “retry this tool” buttons.

## 5. Shared service and storage changes

**Reuse, rather than fork, the management logic.** Introduce an explicit `OperatorPrincipal` and separate authorization entry points for operator reads/control actions. Existing application/runtime principals keep their current restrictions. Operator routes resolve and authorize a target, then use the same transaction/state-transition helpers for disconnect/revoke. Do not fabricate an application principal, bypass authorization with a fake user, or put raw mutation SQL in UI route handlers. Inspection and access reduction must remain possible for disabled applications/integrations; execution must remain blocked.

The current `connections`, `sessions`, `calls`, `app_keys`, and `events` tables already hold most screen data. No agent, grant, policy, graph, integration database, or persistent console-session table is needed. Application/integration configuration remains file-owned.

One additive migration should improve event attribution: `actor_kind`, `actor_id`, `subject_type`, and `request_id` (nullable for historical rows). Keep the affected app/tenant/user separate from who performed the operation. New CLI/application/runtime/operator events identify their actual actor category using non-secret identifiers. Existing rows are labeled legacy/unknown where attribution was not stored; never invent a historical operator.

Record application-key creation/revocation, operator login/logout, session revocation, and connection disconnect as sanitized events. Resource mutations and their events commit in the same transaction. Deployment-level login events appear in Settings → Server through the explicit deployment event query; app-filtered activity must not accidentally return them or other applications' events. Rebuild the event table transactionally to allow an absent app for deployment-level events, preserving existing records. New console sessions are issued only after their login event commits; logout revokes the in-memory session even if recording the event fails. No claim of tamper resistance against the host operator.

Add indexes for app-scoped call/event chronology and the supported session/connection filters, validated using query plans. The console performs no provider I/O merely to render a page. A derived “broker access disabled” reason can use configuration, session expiry/revocation, and connection epoch; upstream credential validity remains unknown until a real provider operation checks it.

## 6. Frontend integration and build order

Source location: `console/` in this standalone repository. Use React + TypeScript with a build-time bundler to reuse the supplied React components directly; rewriting them to Svelte would not help this task. Extract reusable authored components and tokens, replace `window.RavnData` and toast-only mock actions with a typed API client, and preserve/review third-party asset licenses.

Do not ship the HTML artifact wrapper, browser Babel compiler, React development bundles, design-tweak panel, external CDN scripts, mock “prod” accounts or unimplemented buttons. Bundle assets locally into the Python package; production users need no Node server. Preserve keyboard focus, accessible drawers/modals, status text alongside color, and narrow-screen table scrolling. Render metadata as text, never provider HTML. Restrict scripts/connections to the console origin and forbid framing, plugins and base-tag overrides; do not enable `unsafe-eval` to preserve the mockup's runtime Babel behavior.

Poll the visible activity view every five seconds; pause while hidden, cancel old requests when app/tenant changes, and visibly mark stale/error states. Polling does not extend the console session's absolute lifetime. Refresh affected lists/detail after a successful mutation. Empty, loading, disconnected, forbidden, and backend-unavailable are distinct states, not all empty tables.

Implementation order:

1. Console listener, ticket/cookie/CSRF flow, explicit operator principal, sanitized query projections and event migration.
2. Reused shell + Connections/Sessions tables and drawers; read-only Settings and Calls/Events activity.
3. Revoke/disconnect confirmations, key-revocation choice, focused access map, integration tests and visual comparison to the supplied UI.

Acceptance gates: UI disabled by default; public/agent ports cannot reach admin routes; unauthenticated/cross-origin requests denied; single-use/expired tickets rejected; every detail/action namespace tested; operator actions do not change app-user API isolation; restart invalidates console sessions; no credentials appear in console responses/storage/logs; failed mutations never show success; revocation admission-race semantics preserved; real pagination and empty/error states; no fake graph data or unsupported scope editing.

## 7. Later, only if needed

Browser-based personal-token import or one-time key delivery, OAuth connection UX, runtime-session issuance, mutable integration configuration/schema review, fine-grained scope editing, cross-deployment views, SSO/RBAC, large-fleet graph aggregation and streaming events are separate additions. They should not silently enter the milestone-1 console merely because the reference mockup contains matching buttons.
