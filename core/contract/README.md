# tai42-contract

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Pure interface contracts for the TAI ecosystem: the protocols, ABCs, pydantic
models, and enums every other package builds against. At runtime this package
imports nothing but `pydantic`, and its behaviour is limited to a narrow
whitelisted surface — the `tai42_app` forwarding handle, model-level
validators/normalizers, the storage path guard, and `Agent`'s default
`astream`/terminal-drain; everything else is a pydantic model, Protocol, ABC, or
enum. Vendor types (fastmcp, langchain, starlette, mcp) appear only inside
`TYPE_CHECKING` blocks, so they are never loaded when the code runs.

## Position in the ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows — the server
that hosts a capability and supplies the operational layer around it (manifest
loading, access control, OAuth connectors, background execution, monitoring,
storage, and human-in-the-loop steps). Everything plugs in against the
interfaces this package defines.

Three packages; each depends only on the ones to its left:

```
tai42-contract  <--  tai42-kit  <--  tai42-skeleton
(interfaces)      (helpers)     (the server)
```

`tai42-contract` is the leaf: it depends on no other tai-* package, so anything —
the skeleton, a plugin, a helper — can import it without pulling in an
implementation.

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server. Usually pulled in transitively via `tai42-kit` / `tai42-skeleton`, but it
installs directly too:

```bash
uv add tai42-contract
```

Or from source — clone this repo and add it as an editable dependency:

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/core/contract
```

## Development

```bash
uv venv --python 3.13
uv pip install --no-sources --editable ".[dev]"   # pydantic + tooling + vendor libs (for pyright)
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
uv run --no-sync pytest --cov --cov-report=term-missing                           # self-contained contract tests
```

## Documentation

The whole platform — concepts, guides, and the generated reference — lives in
the unified documentation site:

- Layering & the contract/kit/skeleton split: https://tai42.ai/concepts/layering
- Python SDK reference (this package's public API): https://tai42.ai/reference/python-sdk

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
