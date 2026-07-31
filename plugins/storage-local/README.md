# tai42-storage-local

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Local-filesystem storage backend for the TAI ecosystem. It implements the
`tai42_contract.storage.Storage` contract over a directory tree on the local disk,
serving both text and binary content, and registers itself as the active storage
provider when its package is imported.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. A `Storage`
backend answers the question of *where content physically lives* — a template, a
document, or a media file loaded by `id`. This package is the local-filesystem
implementation; siblings back the same contract with S3 (`tai42-storage-s3`) and
GitHub (`tai42-storage-github`). The ecosystem is open-ended: any package can back
the same contract, so this repo is this provider's own full doc home, and the
documentation site covers the platform-level story:

- Storage & resources concept: https://tai42.ai/concepts/storage-and-resources
- Build a storage provider (author guide): https://tai42.ai/guides/authors/storage-provider
- Ecosystem catalog: https://tai42.ai/reference/catalog

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-storage-local
```

Or from source — clone this repo and add it as an editable dependency. Clone
`tai-contract` and `tai-kit` beside this repo first — `[tool.uv.sources]`
resolves them from sibling paths.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/storage-local
```

## Discovery

A backend is activated by a manifest, not an entry point. Name this package in the
manifest's `storage_module` field; the runtime imports it, and the
`@tai42_app.storage.register_storage` decorator fires as an import side-effect to
register `LocalStorage` as the active provider:

```yaml
storage_module: tai42_storage_local
```

There is no auto-discovery — importing the package *is* the registration.

## Configuration

Settings are read from the environment (or a `.env` file) with the
`STORAGE_LOCAL_` prefix:

| Variable                    | Default        | Description                                             |
| --------------------------- | -------------- | ------------------------------------------------------- |
| `STORAGE_LOCAL_ROOT_PATH`   | `./templates`  | The base directory every stored path is resolved under. |
| `STORAGE_LOCAL_CREATE_DIRS` | `true`         | Create missing parent directories on write.             |

## Contract surface

`LocalStorage` implements the full eight-method `Storage` contract:

- **Text:** `load` (raises `FileNotFoundError` when missing), `list`, `upload`,
  `delete`, `delete_dir`.
- **Binary/media:** `load_bytes` / `upload_bytes` read and write raw bytes
  directly (no encoding); `stat` inherits the contract's `mimetypes`-based path
  inference, since the local filesystem stores no content-type metadata.

Every path is resolved under the configured root and rejected if it escapes it (a
path-boundary check, not a string prefix), and `delete_dir` refuses the storage
root via the shared `assert_not_root` guard.

## Development

```bash
uv venv --python 3.13
uv pip install --no-sources --group dev --editable .
uv run --no-sync pytest --cov --cov-report=term-missing
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
```

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
