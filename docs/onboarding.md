# Connect an account and give an agent access

An **integration** configures a provider's MCP endpoint and authentication.
A **connection** stores one user's authorized account credentials.
A **session** grants temporary access to up to eight attached connections. Its authority inherits
from those connections; RAVN does not translate OAuth scopes into tool permissions.

## 1. Create an application and obtain its key

```sh
uv run ravn init --directory .ravn
uv run ravn serve --console
# In another terminal:
uv run ravn console
```

A new deployment starts with no applications or integrations. In **Applications**,
select **Create application** and enter an ID such as `my-agent`. Leave return URLs
empty until you know your app's browser return handler. For the CLI helper, add
`http://127.0.0.1:8800/return`. Tenant mode is fixed after creation.

The Console opens **Application keys** after creation. Choose a label and select
**Generate key**, then **Copy key**. The full key is returned only on creation;
RAVN stores a hash. Save it in your backend's secret storage. Generate a replacement
if it is lost, and revoke obsolete keys from the same page.

**Settings → Application** edits return URLs and enables/disables the application.
Disabling immediately blocks new backend requests and tool dispatch while preserving
records. Applications are stored in SQLite; there are no application or integration
entries in the server configuration file.

CLI equivalent:

```sh
uv run ravn application create --id my-agent --return-url http://127.0.0.1:8800/return
uv run ravn app-key create --app my-agent --output /absolute/private/backend.key
```

Owner-only admin API (Unix socket):

- `GET /admin/v1/applications` lists applications.
- `POST /admin/v1/applications` accepts `id`, optional `tenant_mode` (`single` or
  `multi`), `enabled`, and `return_urls`.
- `PUT /admin/v1/applications/{id}` replaces application settings; ID and tenant
  mode cannot change.
- `POST /admin/v1/app-keys` accepts `app_id` and optional `label`; its response
  contains `id`, `app_id`, the one-time `key`, and `created_at`.

The Console uses these handlers under `/console/api/v1` with its operator cookie
and CSRF check. An application cannot register itself or issue its own keys through
the public broker API. The deployment owner creates the app and gives the generated
key to the application's backend.

For example, once the key is stored in `RAVN_APP_KEY`, your backend can list one
user's connections:

```sh
curl http://127.0.0.1:8787/v1/connections \
  -H "Authorization: Bearer $RAVN_APP_KEY" \
  -H 'X-Ravn-User-Id: alice'
```

The key selects the application automatically. User IDs must come from your
application's authenticated user session. Use `X-Ravn-Tenant-Id` too for `multi`
apps. Agents receive session tokens, never this backend application key.

## 2. Register the integration

Open **Settings → Integrations → Add integration** under the desired application.
Enter an integration ID and the remote MCP URL, then select **Read service settings**.
RAVN reads the MCP resource metadata and the authorization server's OAuth/OIDC
metadata. Enter your pre-registered client ID and, if required, client secret.
Select only the provider scopes you need and copy the generated callback URL into
your provider application's redirect URLs. Published scopes are suggestions, not
a new RAVN permission system; the initial selection uses the server's challenge
if it specifies required scopes, otherwise nothing is selected.

**Configure manually** exposes OAuth URLs and client authentication when discovery
is unavailable. Select **Bearer token** to register a service without OAuth, then
import each user's credential separately. No account identity endpoint is required.
Changing the MCP URL clears discovered settings so credentials cannot be saved
against a stale destination.

Save once; the integration is immediately available to connection and session APIs.
SQLite is the authoritative store. OAuth secrets are encrypted with the deployment
key and omitted from every API response. Keep the database and key backed up together.
The console also lists integrations and can disable them. Disabling blocks new
connections, OAuth completion, sessions, and tool dispatch; already admitted calls
may finish. It preserves credentials and history and does not revoke provider tokens.

To keep management small, there is no edit, delete, or re-enable operation yet.
Use a new integration ID and reconnect accounts to change endpoints or credentials.
Application settings, including allowed `return_urls`, are managed in **Settings → Application**.

### Admin API

The same operations are available on the existing owner-only Unix socket, not the
public broker port. Application keys and session tokens cannot register services.

- `GET /admin/v1/integrations` returns `{ "data": [...] }` without client secrets.
- `POST /admin/v1/integrations/discover` accepts `{ "endpoint": "https://mcp.example.com/mcp" }`.
  It returns `oauth`, `supported_scopes`, and `authentication_methods`, without
  saving anything or sending credentials. Add `client_id`, the required
  `client_secret`, and selected `scopes` to the returned `oauth` object to register.
- `POST /admin/v1/integrations` registers an integration and returns its callback URL.
- `POST /admin/v1/integrations/{app_id}/{id}/disable` disables it immediately.

Create an owner-only `integration.json` file (`chmod 600`) with this shape,
substituting the provider's actual values:

```json
{
  "app_id": "my-agent",
  "id": "records",
  "endpoint": "https://mcp.example.com/mcp",
  "oauth": {
    "client_id": "YOUR_CLIENT_ID",
    "client_secret": "YOUR_CLIENT_SECRET",
    "authorization_endpoint": "https://auth.example.com/authorize",
    "token_endpoint": "https://auth.example.com/token",
    "issuer": "https://auth.example.com",
    "resource": "https://mcp.example.com/mcp",
    "scopes": ["records.read"]
  }
}
```

```sh
curl --unix-socket .ravn/run/admin.sock http://ravn-admin/admin/v1/integrations \
  -H 'Content-Type: application/json' --data-binary @integration.json
curl --unix-socket .ravn/run/admin.sock http://ravn-admin/admin/v1/integrations
curl --unix-socket .ravn/run/admin.sock -X POST \
  http://ravn-admin/admin/v1/integrations/my-agent/records/disable
```

Discovery reads public metadata; registration saves the resulting configuration.
Neither proves an authenticated account can call tools. Without an identity
verifier, token import and OAuth completion staging verify MCP initialization
and tool listing before saving a usable connection.
The browser console uses the same handlers with its operator session and CSRF check.

### Callback URLs

The provider issues the client ID and secret when you register your application.
Register this exact provider
callback URL, substituting your public origin, application ID, and integration ID:

```text
https://YOUR_RAVN_HOST/oauth/callback/my-agent/records
```

The application's `return_urls` are different: RAVN sends the browser there after
handling the provider callback, so your backend can verify the original login
transaction and complete the connection.

Supported authentication is bearer-token import or pre-registered OAuth2
Authorization Code with S256 PKCE and Bearer access tokens. OAuth supports
`client_secret_post`, `client_secret_basic`, and `none` (public client, no secret);
scopes are provider-defined, space-separated strings. Discovered resource URLs
are included in authorization and token requests. Automatic client registration,
nonstandard token responses, and other authentication methods are unsupported.
Optional `authorization_params` supplies provider options such as offline access,
but cannot override state, callback, client credentials, scopes, resource, or PKCE.

An optional `identity` object can still be supplied through the registration API for
services that expose an authenticated GET returning a JSON object. Its `endpoint`,
`id_field`, `display_field`, and optional `required_claims` configure a verified,
stable account identity. Existing configurations continue to work. Without this
object, the public `provider_account_id` is null and `reconnect_supported` is false;
RAVN never substitutes a label or a local connection ID for verified identity.

OAuth `POST /v1/connect-sessions/{id}/complete` and bearer import accept an optional
`label` (1–128 characters), used as the connection's display name. The CLI exposes
it as `--label`. Without a label or identity lookup, the integration ID is displayed.

Same-account `reconnect_connection_id` is only accepted with verified identity.
Otherwise authorize a **new** connection, then explicitly attach it to any sessions
that should use it and remove the old attachment. Routine refresh still updates
the existing grant and preserves its epoch; it does not require a userinfo call.

All server-to-provider endpoints require HTTPS on port 443. RAVN rejects private
IP resolutions, redirects, and oversized responses. Only the operator can change
these destinations; session callers cannot supply them.

The [Gmail example](integrations/gmail.json) is a registration request example. No provider
names, token prefixes, scope vocabulary, or tool schemas are built into the broker.

## 3. Connect an account

If the broker is not already running, check the configuration and start it:

```sh
uv run ravn config-check --config .ravn/ravn.yaml
uv run ravn serve --config .ravn/ravn.yaml --console
```

In another terminal:

```sh
uv run ravn app-key create --config .ravn/ravn.yaml \
  --app my-agent --output /absolute/private/backend.key
uv run ravn connections connect --config .ravn/ravn.yaml \
  --integration records --label 'Work account' \
  --app-key-file /absolute/private/backend.key --user alice
```

The CLI uses the configured loopback return URL by default. It opens consent,
checks the returning browser transaction, and completes the connection. A real
application performs the same steps through `POST /v1/connect-sessions`, its own
browser return handler, and `POST /v1/connect-sessions/{id}/complete`. The app must
derive the user and tenant from verified login, never model-supplied values.

Alternatively, omit `oauth` and import a provider bearer token from a private
file. RAVN verifies access to the MCP service when no optional identity verifier is set:

```sh
uv run ravn connections import --config .ravn/ravn.yaml \
  --integration records --token-file /absolute/private/provider.token \
  --app-key-file /absolute/private/backend.key --user alice
```

Imported tokens have no managed refresh. OAuth refresh follows the configured
rotation behavior. Reconnect is required when credentials expire without usable
refresh or a refresh outcome becomes uncertain.

## 4. Create a session

```sh
uv run ravn sessions create --config .ravn/ravn.yaml \
  --connection CONNECTION_A --connection CONNECTION_B --ttl 1800 \
  --app-key-file /absolute/private/backend.key --user alice \
  --output /absolute/private/session.json
```

The output contains `id`, `token`, `mcp_url`, and `expires_at`. Configure the agent's
MCP client with that URL and `Authorization: Bearer SESSION_TOKEN`. Keep the backend
key and provider credentials outside the agent runtime.

All attached connections must belong to the same application, tenant, and user.
Repeat `--connection` for each account; omit it to create an empty session with no
tools. Creation checks ownership and connection state without contacting providers.

The management API accepts `POST /v1/sessions` with
`{"connection_ids": ["CONNECTION_A", "CONNECTION_B"], "ttl_seconds": 1800}`.
The old singular `connection_id` request field is no longer accepted.
`GET /v1/sessions/{id}` returns the session and attached connections, including
any `blocked_reason`. Runtime tokens cannot call these management APIs.

Change membership through the application backend or console session details:

- `PUT /v1/sessions/{id}/connections/{connection_id}` attaches an existing account.
- `DELETE /v1/sessions/{id}/connections/{connection_id}` removes it from this session.
- Both operations are idempotent and preserve the token and expiry. Removing the
  last connection leaves a valid session with no tools. Saved accounts and other
  sessions remain intact. Adding an authorized account requires no new OAuth flow.

```sh
uv run ravn sessions attach SESSION_ID --connection CONNECTION_B \
  --app-key-file /absolute/private/backend.key --user alice
uv run ravn sessions remove SESSION_ID --connection CONNECTION_A \
  --app-key-file /absolute/private/backend.key --user alice
uv run ravn sessions show SESSION_ID \
  --app-key-file /absolute/private/backend.key --user alice
```

The console's **Sessions → session details → Attached connections** offers the
same operations, scoped to the session owner's accounts. It also offers explicit
reattachment after an account is reauthorized. There are no session tool policies.

MCP `tools/list` combines usable connections. Names are connection-specific, for
example `conn_<id>__search`, so accounts may expose identically named tools.
Long or nonportable upstream names get a hashed suffix to keep names within 64
ASCII characters. Use the returned name unchanged; RAVN maps it to the exact
upstream name and credential. Tool metadata includes `ravn/connection-id` and
`ravn/tool-name`. Refresh the agent's tool list after changing membership.

Unavailable connections do not hide working connections' tools. The list response
reports their IDs and error codes in `_meta["ravn/unavailable-connections"]`;
an empty list can mean no accounts are attached or all are unavailable.

Each attachment stores the connection's authorization **epoch**. Reauthorization
or disconnect changes that epoch, blocking old attachments without revoking the
whole session. Reauthorize an account, then explicitly attach it again to grant
its current authorization when verified identity supports same-account reconnect.
Otherwise authorize and attach a new connection. Routine OAuth refresh preserves the epoch.

RAVN forwards the authenticated upstream tool catalogue and its schemas, without
an embedded allowlist or schema pins. It validates schemas and arguments before
dispatch and rechecks session/connection state immediately before executing.
Schemas containing external references or excessive size/depth are rejected.
The provider decides which operations the credential may perform; a server may
advertise tools that later fail for insufficient scope. Newly advertised tools
are available to existing active sessions. An agent's instructions are not
additional authorization constraints.

You can inspect current schemas for diagnostics; this does not grant permissions:

```sh
uv run ravn integration-inspect --config .ravn/ravn.yaml \
  --connection CONNECTION_ID --app-key-file /absolute/private/backend.key --user alice
```

## 5. Stop access and inspect outcomes

```sh
uv run ravn sessions revoke SESSION_ID --config .ravn/ravn.yaml \
  --app-key-file /absolute/private/backend.key --user alice
uv run ravn connections disconnect CONNECTION_ID --config .ravn/ravn.yaml \
  --app-key-file /absolute/private/backend.key --user alice
```

Revoking one session stops its future calls. Disconnecting stops future use of the
connection while leaving other connections in the same sessions usable. Neither operation undoes an action
already dispatched or revokes the provider's token remotely.

The console shows connections, sessions, calls, and management events. Session
details manage attached connections, with no additional tool permission editor. Optional MCP metadata `ravn/idempotency-key` identifies
a logical action and prevents duplicate local dispatch while its record exists.
No tool call is automatically retried. An `outcome_unknown` means the operation
may have executed; reconcile with the provider before trying again. Tool effects
are recorded as `unknown`, rather than inferred from names or untrusted hints.

## Demo database and console

RAVN initializes new databases from `src/ravn/schema.sql`. This demo does not
carry historical database migrations. Databases using the current layout remain
usable across restarts; incompatible layouts are rejected without being changed.
To start fresh, run `ravn init --directory .ravn-fresh`, then
`ravn serve --config .ravn-fresh/ravn.yaml --console`. Use the same `--config`
with `ravn console`. Keep the old state directory and encryption key if you need
its data; the new deployment starts with no accounts or sessions.

The console is HTML rendered by Python, with ordinary navigation, filters, and
forms. Activity contains Calls and Events; Settings contains Application, Integrations,
Application keys, and Server. Records open in a right-side detail drawer.
JavaScript exchanges the single-use sign-in ticket and submits mutations
to the existing console API with the operator cookie and CSRF token. There is no
React, Node.js, or frontend compilation step. Edit `src/ravn/templates/console.html`
and `src/ravn/static/console.css` directly.
