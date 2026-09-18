# Design targets

These documents were retained from the original RAVN repository for context.
They describe a broader design, including unimplemented APIs. Use the
[root README](../../README.md) and [onboarding guide](../onboarding.md) for the
current contract.

| Document | Purpose |
|---|---|
| [Minimal technical design](headless-ravn-v1.md) | Architecture and intended developer journey |
| [Detailed reference](headless-ravn-v1-reference.md) | Design decisions and planned extensions |
| [OpenAPI design](headless-ravn-v1.openapi.yaml) | Proposed API; not the current server specification |
| [Console design](broker-console-v1.md) | Operator-console scope and proposed extensions |

## Implemented boundary

- GitHub and Slack read tools; Gmail read tools and draft creation over MCP.
- OAuth, credential storage/refresh, connection ownership, expiring sessions,
  revocation, and operational records.
- Local operator console and the single-tester Support Desk example.

REST tool execution, application-facing activity listing, per-session resource
restrictions, the TypeScript helper, external vaults, and multi-user production
authentication are not implemented. Live provider compatibility is unverified.
