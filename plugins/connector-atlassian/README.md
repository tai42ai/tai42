# connector-atlassian

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

The **Atlassian** OAuth connector provider for the TAI ecosystem — Jira,
Confluence, and Compass over Atlassian's hosted Rovo MCP endpoint.

This is a **descriptor-only plugin**: a `tai-plugin.yml` and its docs, with no
Python package and nothing to install but the manifest entry. It declares one
connector `provider` (a `ProviderDescriptor`: OAuth endpoints, per-sub-service
granular scopes, and the hosted MCP endpoint); the runtime's connector engine
drives the OAuth flow and reaches the endpoint generically, keyed off the
descriptor.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. A connector
provider describes how the runtime connects a third-party service — OAuth
endpoints, scopes, and the MCP servers that expose it as tools. This listing is
one such provider (Atlassian); a sibling backs the same contract for Google
(`tai42/connector-google`).

- Connectors concept: https://tai42.ai/concepts/connectors
- Ecosystem catalog: https://tai42.ai/reference/catalog

## Provider

| Field | Value |
| --- | --- |
| id | `atlassian` |
| kind | `oauth` |
| origin | `system` |
| category | `dev-tools` |
| authorize | `https://auth.atlassian.com/authorize` |
| token | `https://auth.atlassian.com/oauth/token` |
| client id env | `CONNECTORS_ATLASSIAN_CLIENT_ID` |
| client secret env | `CONNECTORS_ATLASSIAN_CLIENT_SECRET` |
| sub-services | `jira`, `confluence`, `compass` (all on `https://mcp.atlassian.com/v1/mcp/authv2`) |

The operator provisions an Atlassian OAuth 2.0 (3LO) app and sets the two
credential env vars on the API process environment.

## Install

```bash
tai plugins install tai42/connector-atlassian \
  --env CONNECTORS_ATLASSIAN_CLIENT_ID=... \
  --secret CONNECTORS_ATLASSIAN_CLIENT_SECRET
```

See `docs/index.mdx` for the full setup, the manifest `connectors:` alternative,
and the per-service granular-scope table.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
