# RAVN Broker

A self-hosted MCP broker for GitHub, Slack, and Gmail. RAVN handles OAuth,
credential storage and refresh, and short-lived sessions for agents. Your
application owns user login and account-connection UI.

- GitHub: read and list issues.
- Slack: search messages and read threads.
- Gmail: search/read mail and create drafts. Drafts are never sent.
- Local console: inspect connections, sessions, and activity; revoke access.

**Live provider compatibility remains unverified.** Automated tests use simulated
providers. Gmail writes are never automatically retried.

## Run

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked
uv run ravn init --directory .ravn --app demo
uv run ravn serve --console
```

Initialize only a new deployment. In another terminal, `uv run ravn console`
opens the local console. The broker uses port 8787; the console uses 8788.
Follow [provider setup](docs/onboarding.md) to connect accounts and enable tools.

## Develop

```sh
uv run --locked pytest
uv run --locked ruff format --check src tests
uv run --locked ruff check src tests
npm --prefix console ci --ignore-scripts
npm --prefix console run format:check
npm --prefix console test
npm --prefix console run build
uv build
```

Commit rebuilt assets in `src/ravn/static/console/` with console changes.

## Deployment

Run one broker process per SQLite database. Keep `.ravn/` and its encryption
key private and back them up together. Use HTTPS beyond local development;
the operator console and admin socket are local-only. Application keys stay in
trusted backends; agents receive only connection-bound session tokens.

[Apache-2.0](LICENSE) · [Third-party notices](console/public/THIRD_PARTY_NOTICES.txt)
