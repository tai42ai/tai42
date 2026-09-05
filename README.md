# tai42

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

The tai42 monorepo: the core libraries, the plugins, and the end-to-end test
harness, in one uv workspace. Every package is published to PyPI under the
`tai42-` name; the workspace resolves the packages against each other in-tree.

## Layout

| Path | Package | What it is |
| --- | --- | --- |
| `core/contract` | `tai42-contract` | Shared protocols, manifest types, plugin spec |
| `core/kit` | `tai42-kit` | Generic leaf helpers, settings, clients, LLM factories |
| `core/skeleton` | `tai42-skeleton` | The application skeleton plugins extend |
| `plugins/*` | `tai42-<name>` | Channels, backends, storages, connectors, identity, accounts, tools, webhook verifiers, config providers, monitoring |
| `e2e` | `tai42-e2e` | Publishable plugin-testing harness (fixtures, stacks, pytest plugin) |

## Quickstart

```sh
git clone https://github.com/tai42ai/tai42
cd tai42
uv sync --locked
```

`uv sync` installs every workspace member editable into one `.venv`; the
`tai42-*` dependencies resolve to their in-tree siblings, not from PyPI.

Work on a single package with `--package`:

```sh
uv run --package tai42-kit pytest --cov --cov-report=term-missing
uv run --package tai42-kit ruff check .
```

Run each package's own checks from its directory so its coverage floor, warning
filters, markers, and ruff/pyright configs apply. A root `pyright` run is
unsupported — there is no root config, so it falls back to defaults and reports
false positives; run it per package (`scripts/pyright-all.sh` sweeps them all).

## Naming

| Surface | Form |
| --- | --- |
| Distribution — PyPI, `pip install`, dependency pins | `tai42-<name>` |
| Import package | `tai42_<name>` |
| Repository path | `core/<name>`, `plugins/<name>`, `e2e` |

## Releases

release-please runs in manifest mode over the whole workspace. A merged
conventional commit scoped to a package raises a release PR for that package;
merging it tags `tai42-<name>-v<version>` and publishes the package to PyPI.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
