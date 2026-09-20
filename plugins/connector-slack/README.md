# connector-slack

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

The Slack OAuth connector provider for the TAI ecosystem — one connection against
Slack's hosted MCP server (`https://mcp.slack.com/mcp`).

This is a **descriptor-only** plugin: it ships no Python package. `tai-plugin.yml`
declares one `ProviderDescriptor`; installing the listing appends that descriptor
to the manifest `connectors:` list, and the skeleton connector engine drives the
OAuth flow and the MCP transport generically off the descriptor. Nothing is
pip-installed.

## What it declares

- **Provider** `slack` (OAuth, category `communication`, origin `system`).
- **OAuth endpoints** — Slack's USER-token `authorize` / `token` pair
  (`oauth/v2_user/authorize` and `api/oauth.v2.user.access`); no revoke endpoint.
- **Client credentials by env name** — `client_id_env=CONNECTORS_SLACK_CLIENT_ID`,
  `client_secret_env=CONNECTORS_SLACK_CLIENT_SECRET`.
- **Sub-service** `slack` — Slack's hosted MCP server over Streamable HTTP, with
  the user-token scopes covering read and post across channels, DMs, and search.

## Install

See `docs/index.mdx`. In short:

```bash
tai plugins install tai42/connector-slack \
  --env CONNECTORS_SLACK_CLIENT_ID=... \
  --secret CONNECTORS_SLACK_CLIENT_SECRET
```

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
