# tai42-connector-google

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

The Google OAuth connector provider for the TAI ecosystem — Gmail, Calendar, and
Drive over stdio MCP servers.

A pure plugin: its **only** dependency is `tai42-contract`. It declares one
`ProviderDescriptor` and registers it through the `tai42_app`
handle when the manifest loads `tai42_connector.google.core.connector`. It carries
**no** OAuth, probe, or launch code — the skeleton connector engine drives all of
that generically, keyed off the descriptor. It never imports the skeleton.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. A connector
provider is a pure-data plugin describing how the runtime connects a third-party
service — OAuth endpoints, scopes, and the MCP servers that expose it as tools.
This package is one such provider (Google); a sibling backs the same contract
for Atlassian (`tai42-connector-atlassian`). The ecosystem is open-ended: any
package can declare a provider, so this repo is this provider's own full doc
home, and the documentation site covers the platform-level story:

- Connectors concept: https://tai42.ai/concepts/connectors
- Build a connector (author guide): https://tai42.ai/guides/authors/connector
- Ecosystem catalog: https://tai42.ai/reference/catalog

## What it declares

- **Provider** `google` (OAuth, category `communication`).
- **OAuth endpoints** — Google's authorize / token / revoke URLs, plus the
  `access_type=offline` + `prompt=consent` params Google needs to mint a refresh
  token on first consent, and `include_granted_scopes=true` to enable incremental
  authorization (scopes granted in earlier consents carry forward).
- **Client credentials by env name** — `client_id_env=CONNECTORS_GOOGLE_CLIENT_ID`,
  `client_secret_env=CONNECTORS_GOOGLE_CLIENT_SECRET`. The engine resolves these
  from the process environment at connect time.
- **Sub-services** — `gmail`, `calendar`, `drive`, each a pkg-launched stdio MCP
  server named by `entry_point` (`tai-mcp-google-gmail` / `-calendar` / `-drive`).
  Launch is driven by the provider's `pkg_manager` (`uvx`); the engine resolver
  synthesizes the command. Scope sensitivity varies: `gmail.readonly` and
  `drive.readonly` are **restricted** scopes (require Google's annual CASA
  third-party security assessment); `gmail.send` and `calendar.events` are
  **sensitive** scopes (require app verification); `openid`, `email`, and
  `drive.file` are non-sensitive.

## How it loads

Add `tai42_connector.google.core.connector` to the manifest. Importing it calls
`tai42_app.connectors.register_connector(descriptor)` through the bound handle —
the same manifest + handle mechanism as the storage / backend plugins, but a
plain call because a connector ships pure data, not behavior.

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-connector-google
```

Or from source — clone this repo and add it as an editable dependency. Clone
`tai-contract` beside this repo first — `[tool.uv.sources]` resolves it from
the sibling path.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/connector-google
```

## Development

```bash
uv venv --python 3.13
uv pip install --no-sources --group dev --editable .
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
uv run --no-sync pytest --cov --cov-report=term-missing
```

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
