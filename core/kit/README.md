# tai42-kit

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Generic leaf helpers, settings primitives, pooled clients, and LLM/AI factories
for the TAI ecosystem. It provides the reusable building blocks the server is
composed from: data and text transforms, LangChain/FastMCP/MCP tool glue, the
pooled-client facade and concrete drivers (`redis`, `curl`, `mcp`, `postgres`,
`http`), the SSRF URL guard and safe download, MCP client transports over a UDS
socket, the settings machinery, LLM/embedding factories with checkpoint/store
backends, logging setup, and the backend liveness file with its
`tai42-backend-probe` console script. Heavier backends are gated behind extras and
imported lazily. Typed package (`py.typed`).

## Position in the ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows — the server
that hosts a capability and supplies the operational layer around it (manifest
loading, access control, OAuth connectors, background execution, monitoring,
storage, and human-in-the-loop steps).

Four packages; each depends only on the ones to its left:

```
                     tai42-kit  <--.
                   /  (helpers)     \
tai42-contract  <-+                  +--  tai42-skeleton
  (interfaces)     \                /    (the server)
                     tai42-cli  <--'
               (the remote client)
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

Or from source — clone this repo and add it as an editable dependency; the
`tai42-*` dependencies resolve in-tree from the workspace.

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

## Backend liveness

`tai42_kit.liveness` holds the file a backend loop process reports its progress
into (`LivenessFile`), its `TAI_BACKEND_LIVENESS_` settings (`PATH`, `INTERVAL`,
`GRACE`), and the reading of it (`check_liveness`). Each report writes the
process's pid and a horizon: the seconds within which the next pass is
promised, the report's own promise plus `INTERVAL`. The file is rewritten only
when the promise already in it would expire before the next pass, so a steady
loop writes about once per `INTERVAL`. A file holding the pid of another live
process is refused: each process needs its own path. Of two processes claiming
one file at once, the second is refused. A process removes its file when it
exits (a file left by a process that no longer exists is removed too), and a
report after that removal raises instead of writing the file again.

The `tai42-backend-probe` console script reads the file at
`TAI_BACKEND_LIVENESS_PATH` and prints one line: it exits 0 while the file's age
is at most its horizon plus the grace (`--grace`, default
`TAI_BACKEND_LIVENESS_GRACE`), and 1 when the file is stale or missing, so it
serves as an orchestrator's exec liveness probe.

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

- Layering & the contract/kit/cli/skeleton split: https://tai42.ai/concepts/layering
- Python SDK reference (this package's public API): https://tai42.ai/reference/python-sdk

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
