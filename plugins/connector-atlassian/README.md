# tai42-connector-atlassian

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

The **Atlassian** OAuth connector provider for the TAI ecosystem — Jira,
Confluence, and Compass.

This is a **pure-data plugin**. It declares one `ProviderDescriptor` (scopes,
OAuth endpoints, the per-sub-service MCP server endpoints, and the env-var names
that hold the OAuth client credentials) and registers it by calling
`tai42_app.connectors.register_connector(descriptor)` when the manifest loads it.
It carries **no** OAuth / probe / launch code — all of that is generic in the
connector engine, keyed off the descriptor — and **no** skeleton import.

It depends on **`tai42-contract` only**.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. A connector
provider is a pure-data plugin describing how the runtime connects a third-party
service — OAuth endpoints, scopes, and the MCP servers that expose it as tools.
This package is one such provider (Atlassian); a sibling backs the same contract
for Google (`tai42-connector-google`). The ecosystem is open-ended: any package
can declare a provider, so this repo is this provider's own full doc home, and
the documentation site covers the platform-level story:

- Connectors concept: https://tai42.ai/concepts/connectors
- Build a connector (author guide): https://tai42.ai/guides/authors/connector
- Ecosystem catalog: https://tai42.ai/reference/catalog

## Provider

| Field | Value |
| --- | --- |
| id | `atlassian` |
| kind | `oauth` |
| category | `dev-tools` |
| authorize | `https://auth.atlassian.com/authorize` |
| token | `https://auth.atlassian.com/oauth/token` |
| client id env | `CONNECTORS_ATLASSIAN_CLIENT_ID` |
| client secret env | `CONNECTORS_ATLASSIAN_CLIENT_SECRET` |
| sub-services | `jira`, `confluence`, `compass` (all on `https://mcp.atlassian.com/v1/mcp/authv2`) |

The operator must provision an Atlassian OAuth 2.0 (3LO) app and set the two
credential env vars on the API process environment.

## Layout

`src/tai42_connector/atlassian/core/connector.py` builds the descriptor and
registers it on import. `tai42_connector` is a namespace package, so each provider
ships as its own package under the shared namespace.

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-connector-atlassian
```

Or from source — clone this repo and add it as an editable dependency; the
`tai42-*` dependencies resolve in-tree from the workspace.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/connector-atlassian
```

## Development

```bash
uv venv --python 3.13
uv pip install --no-sources --group dev --editable .   # public: tai42-contract + tooling
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
uv run --no-sync pytest --cov --cov-report=term-missing                                # self-contained tests
```

A clone needs **nothing private**: the install resolves every dependency from
PyPI, so nothing else in the workspace needs to be present for the gates to run.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
