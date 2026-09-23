# Slack MCP setup

1. Create a Slack app for your workspace and find its Client ID and Client Secret.
2. In the Slack app's **Features → Agents** page, turn on **Slack Model Context
   Protocol (MCP) Server → Enable Slack MCP Server**. OAuth consent alone does not
   enable MCP access. The separate Agent experience switch is not required.
3. Register the integration in RAVN with `https://mcp.slack.com/mcp`, read the
   service settings, enter the client credentials, and select the user scopes you need.
4. Copy RAVN's callback URL into Slack's **OAuth & Permissions → Redirect URLs**
   and save it. For application `test` and integration `slack` in local development:
   `http://127.0.0.1:8787/oauth/callback/test/slack`.
5. Start a connection through the application backend or `ravn connections connect`,
   then approve Slack's consent page before the 10-minute connection flow expires.

If OAuth succeeds but MCP verification fails, check the MCP switch in step 2.
Slack returns JSON-RPC `-32600` when this feature is disabled. RAVN reports
`provider_protocol_error`; its server log identifies the failed stage and RPC code
without logging tokens or the provider's response payload.

These are deployment setup instructions; RAVN does not contain Slack-specific
runtime permissions, tools, or OAuth endpoints.

Reference: [Slack MCP server documentation](https://docs.slack.dev/ai/slack-mcp-server/).
