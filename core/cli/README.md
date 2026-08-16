# tai42-cli

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

The `tai` command-line client for operating a tai42 server over its HTTP API. It
ships the `tai` console script: thin, typed clients over the server's `/api/*`
surface — the same routes the Studio calls — for tools, presets, agents,
extensions, connectors, hooks, channels, storage, keys, scopes, roles,
configuration, and the rest of the operator surface. Human-readable tables by
default; raw JSON under `--json` for scripting. Typed package (`py.typed`).

## Position in the ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. `tai42-cli`
is the terminal client half of the `tai` command: it talks to a running server
over HTTP and depends only on the interface contract, so it installs and runs on
its own without the server package.

```
tai42-contract  <--  tai42-cli
(interfaces)      (the remote client)
```

Installing the server package adds its own local and runtime commands (`serve`,
`db`, `doctor`, `catalog`, `openapi`, `backend`, `metrics`, offline config/manifest
validation) to the same `tai` command through an entry-point group, so one binary
covers both remote and local operation when both are installed.

## Install

Requires **Python 3.13+**. Install from PyPI:

```bash
uv add tai42-cli
```

Or as a standalone tool:

```bash
uv tool install tai42-cli
```

## Usage

Point the client at a server and authenticate. The server URL resolves from the
`--server` flag, then `TAI_SERVER_URL`, then `~/.config/tai/config.toml`, then a
local default. The API key resolves from `--api-key-stdin`, then `TAI_API_KEY`,
then the config file, then an interactive prompt (there is deliberately no
`--api-key VALUE` flag — a value on the command line leaks through `ps` and shell
history). The HTTP read window resolves from `--timeout SECONDS`, then
`TAI_CLI_TIMEOUT_SECONDS`, then a 120s default sized for fleet-broadcast config
writes; values must be positive and finite.

```bash
export TAI_SERVER_URL=https://your-server.example
export TAI_API_KEY=sk-...

tai tools list
tai tools list --json
tai config env get
tai version
```

Install shell completion:

```bash
tai completion install bash > /etc/bash_completion.d/tai
```

## Development

```bash
uv venv --python 3.13
uv pip install --no-sources --editable ".[dev]"
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
uv run --no-sync pytest --cov --cov-report=term-missing
```

## Documentation

The whole platform — concepts, guides, and the generated reference — lives in
the unified documentation site:

- Layering & the contract/kit/cli/skeleton split: https://tai42.ai/concepts/layering
- Python SDK reference: https://tai42.ai/reference/python-sdk

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
