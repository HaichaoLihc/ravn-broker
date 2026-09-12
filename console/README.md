# RAVN local operator console

This is the UI from the supplied **Ravn Console.html**, adapted to the Python
broker. It is not a Sites project and has no hosted-service dependency.

`src/reference/components.js` contains 20 extracted original UI primitives;
`tokens.css` and `public/reference/` contain the supplied colors, fonts and icons.
`src/reference/provenance.json` records the attachment's SHA-256 and source names.
We retain the original navigation/table/drawer/graph visual system. The old mock
agent, delegation, permission and risk data is not included in the runtime.

`src/main.tsx` binds the supplied components to authenticated console APIs.
`src/api.ts` uses the operator cookie and in-memory CSRF nonce. No secrets go into
localStorage/sessionStorage, and no app bearer is used by the browser.

## Build and run

```sh
npm ci
npm run format:check
npm run build
npm test
```

The build writes packaged assets to `../src/ravn/static/console/`. Run
`uv run ravn serve --console` and `uv run ravn console` from the broker directory.
Serve the built UI with FastAPI; a standalone Vite origin is not authorized for
operator API requests. No CORS relaxation is provided for development.

`npm run format` formats the authored console and Support Desk UI sources.
The extracted reference primitives/assets remain unchanged. Commit rebuilt
`../src/ravn/static/console/` assets with source changes; the Python package ships them.

For an updated copy of the same HTML format, the mechanical extractor is:

```sh
node scripts/extract-reference.mjs /absolute/path/to/Ravn\ Console.html
```

It reads embedded resources without executing the attachment. It removes the
artifact loader, browser Babel, mock product views, design-tweak tooling and remote
icon requests. Small adaptations remove unused header actions and make destructive
confirmation overlays cover the entire viewport. React is installed as a normal
locked build dependency, rather than copying the development runtime in the HTML.

## Current scope

- Connections and sessions: filtered, cursor-paginated tables and detail drawers.
- Revocation/disconnect: real API mutations with CSRF and explicit confirmation.
- Calls/events: metadata-only activity; 24-hour UI window, five-second polling,
  paused while reading details or older pages. The API permits explicit windows
  up to 30 days. This is not a complete record of pre-admission authorization denials.
- Application keys: metadata and routine/compromise revocation, never secret reveal.
- Settings: read-only file-owned configuration; no GitHub health probe on page load.
- Access map: at most ten sessions on the selected connection; full listing is
  available separately. It is not an agent-delegation graph.

The first version is **local-owner administration only**. Do not reverse-proxy it
to the internet or treat it as a customer-facing multi-operator access system.
The cookie is intentionally non-Secure only because this version is loopback HTTP;
remote HTTPS/SSO requires its own reviewed authentication/deployment design.

Validation includes real local CLI startup/sign-in, operator API isolation and
revocation tests, frontend client tests, and a browser check of the empty local
console. No live GitHub account or credentials are used by these checks.

## Source and licensing

The authored UI and embedded assets were supplied by the user. The original
component comments are retained. React packages carry their upstream license;
embedded icons identify Lucide 0.544.0 and fonts identify Geist/Geist Mono.
Bundled dependency notices are in `public/THIRD_PARTY_NOTICES.txt` and ship with
the console build. The provenance record identifies the user-supplied design;
including upstream notices does not establish ownership of that supplied design.
