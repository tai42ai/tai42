# tai42-kit

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Generic leaf helpers, settings primitives, pooled clients, and LLM/AI factories
for the TAI ecosystem. It provides the reusable building blocks the server is
composed from: data and text transforms, LangChain/FastMCP/MCP tool glue, the
pooled-client facade and concrete drivers (`redis`, `curl`, `mcp`, `postgres`,
`http`), the SSRF URL guard and safe download, MCP client transports over a UDS
socket, the settings machinery, LLM/embedding factories with checkpoint/store
backends, and logging setup. Heavier backends are gated behind extras and
imported lazily. Typed package (`py.typed`).

## Position in the ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows — the server
that hosts a capability and supplies the operational layer around it (manifest
loading, access control, OAuth connectors, background execution, monitoring,
storage, and human-in-the-loop steps).

Three packages; each depends only on the ones to its left:

```
tai42-contract  <--  tai42-kit  <--  tai42-skeleton
(interfaces)      (helpers)     (the server)
```

`tai42-kit` obeys the leaf rule: its only tai-* dependency is `tai42-contract`. It
implements the contract's `BaseClient` Protocol and consumes its manifest types;
among tai-* packages it depends on nothing else.

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-kit
```

Or from source — clone this repo and add it as an editable dependency. Clone
`tai-contract` beside this repo first — `[tool.uv.sources]` resolves it from
the sibling path.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/core/kit
```

Backends are gated behind extras, so install the ones you need — e.g. the
pooled-client drivers `tai42-kit[redis]`, `tai42-kit[postgres]`, `tai42-kit[curl]`, the
checkpoint/store backends `tai42-kit[langgraph-checkpoint-postgres]`,
`tai42-kit[langgraph-checkpoint-sqlite]`, and LLM-provider backends like
`tai42-kit[anthropic]`, `tai42-kit[google]`, `tai42-kit[mistral]`, `tai42-kit[xai]`,
`tai42-kit[ollama]`, `tai42-kit[huggingface]`.

## Development

```bash
uv venv --python 3.13
uv pip install --no-sources --editable ".[dev,llm,jq,uvicorn,redis,curl,postgres]"
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright       # 0 errors; missing optional ML backends report as warnings
uv run --no-sync pytest --cov --cov-report=term-missing        # optional-extra tests skip if their extra is absent
```

See `CONTRIBUTING.md` for the rules.

## Documentation

The whole platform — concepts, guides, and the generated reference — lives in
the unified documentation site:

- Layering & the contract/kit/skeleton split: https://tai42.ai/concepts/layering
- Python SDK reference (this package's public API): https://tai42.ai/reference/python-sdk

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
