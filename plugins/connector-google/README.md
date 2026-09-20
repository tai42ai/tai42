# connector-google

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

The **Google Workspace** OAuth connector provider for the TAI ecosystem — Gmail,
Calendar, Drive, Docs, Sheets, Slides, Chat, and People over Google's hosted
Workspace MCP servers.

This is a **descriptor-only plugin**: a `tai-plugin.yml` and its docs, with no
Python package and nothing to install but the manifest entry. It declares one
connector `provider` (a `ProviderDescriptor`: OAuth endpoints, per-sub-service
scopes, and the hosted MCP server URLs); the runtime's connector engine drives
the OAuth flow and reaches the servers generically, keyed off the descriptor.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. A connector
provider describes how the runtime connects a third-party service — OAuth
endpoints, scopes, and the MCP servers that expose it as tools. This listing is
one such provider (Google); a sibling backs the same contract for Atlassian
(`tai42/connector-atlassian`).

- Connectors concept: https://tai42.ai/concepts/connectors
- Ecosystem catalog: https://tai42.ai/reference/catalog

## What it declares

- **Provider** `google` (OAuth, category `communication`, origin `system`).
- **OAuth endpoints** — Google's authorize / token / revoke URLs, plus
  `access_type=offline` + `prompt=consent` (a refresh token on first consent) and
  `include_granted_scopes=true` (incremental authorization).
- **Client credentials by env name** — `CONNECTORS_GOOGLE_CLIENT_ID` and
  `CONNECTORS_GOOGLE_CLIENT_SECRET`, resolved from the process environment at
  connect time.
- **Sub-services** — `gmail`, `calendar`, `drive`, `docs`, `sheets`, `slides`,
  `chat`, `people`, each an HTTP MCP server on Google's hosted Workspace MCP
  endpoints, with per-product scopes. Drive stays scoped to `drive.readonly` +
  `drive.file` — never the full `drive` scope.

Google's hosted Workspace MCP servers are in **Developer Preview**; see
`docs/index.mdx` for the operator setup (own OAuth client, Developer Preview
Program enrollment, redirect URI).

## Install

```bash
tai plugins install tai42/connector-google \
  --env CONNECTORS_GOOGLE_CLIENT_ID=...apps.googleusercontent.com \
  --secret CONNECTORS_GOOGLE_CLIENT_SECRET
```

See `docs/index.mdx` for the full setup, the manifest `connectors:` alternative,
and the per-service scope table.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
