# connector-github

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

The GitHub OAuth connector provider for the TAI ecosystem — one connection against
GitHub's hosted MCP server (`https://api.githubcopilot.com/mcp/`), one sub-service
per MCP toolset.

This is a **descriptor-only** plugin: it ships no Python package. `tai-plugin.yml`
declares one `ProviderDescriptor`; installing the listing appends that descriptor
to the manifest `connectors:` list, and the skeleton connector engine drives the
OAuth flow and the MCP transport generically off the descriptor. Nothing is
pip-installed.

## What it declares

- **Provider** `github` (OAuth, category `dev-tools`, origin `system`).
- **OAuth endpoints** — GitHub's `authorize` / `token` pair; no revoke endpoint.
  Register a **GitHub App** with "Expire user authorization tokens" ENABLED so the
  token response carries a `refresh_token`.
- **Client credentials by env name** — `client_id_env=CONNECTORS_GITHUB_CLIENT_ID`,
  `client_secret_env=CONNECTORS_GITHUB_CLIENT_SECRET`.
- **Sub-services** — one per GitHub MCP toolset (`repos`, `issues`,
  `pull_requests`, `actions`, `discussions`, `code_security`, `secret_protection`,
  `dependabot`, `notifications`, `orgs`, `users`, `gists`), each selecting its
  toolset through the `X-MCP-Toolsets` header.

## Install

See `docs/index.mdx`. In short:

```bash
tai plugins install tai42/connector-github \
  --env CONNECTORS_GITHUB_CLIENT_ID=... \
  --secret CONNECTORS_GITHUB_CLIENT_SECRET
```

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
