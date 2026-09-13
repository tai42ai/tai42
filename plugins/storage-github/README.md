# tai42-storage-github

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

A GitHub-backed `Storage` provider for the TAI ecosystem. It stores content as
files in a GitHub repository and serves the full `tai42_contract.storage.Storage`
surface — the text methods (`load` / `list` / `upload` / `delete` / `delete_dir`)
plus the binary/media methods (`load_bytes` / `upload_bytes` / `stat`).

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. A `Storage`
backend is "where content physically lives" — a pluggable provider the runtime's
`ResourceManager` loads and renders content over. This package is one such
provider (GitHub); siblings back the same contract with S3 (`tai42-storage-s3`) and
the local filesystem (`tai42-storage-local`). The ecosystem is open-ended: any
package can back the same contract, so this repo is this provider's own full doc
home, and the documentation site covers the platform-level story:

- Storage & resources concept: https://tai42.ai/concepts/storage-and-resources
- Build a storage provider (author guide): https://tai42.ai/guides/authors/storage-provider
- Ecosystem catalog: https://tai42.ai/reference/catalog

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-storage-github
```

Or from source — clone this repo and add it as an editable dependency; the
`tai42-*` dependencies resolve in-tree from the workspace.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/storage-github
```

## Use

The backend is loaded by **import side-effect**: importing the `tai42_storage_github`
package runs its `@tai42_app.storage.register_storage` decorator, making
`GithubStorage` the active storage provider. Point a manifest's `storage_module`
at the package:

```yaml
# manifest.yml
storage_module: tai42_storage_github
```

## Configuration

Configure through `STORAGE_GITHUB_`-prefixed environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `STORAGE_GITHUB_USERNAME` | — | Repository owner (user or org). |
| `STORAGE_GITHUB_REPO` | — | Repository name. |
| `STORAGE_GITHUB_BRANCH` | `main` | Branch to read/write. |
| `STORAGE_GITHUB_TOKEN` | — | Access token; omit for a public repository. |
| `STORAGE_GITHUB_TIMEOUT_TOTAL` | `15.0` | HTTP timeout (seconds). |
| `STORAGE_GITHUB_MAX_CONNECTIONS` | `200` | Connection-pool ceiling. |
| `STORAGE_GITHUB_MAX_KEEPALIVE_CONNECTIONS` | `50` | Keep-alive pool size. |
| `STORAGE_GITHUB_KEEPALIVE_EXPIRY` | `300.0` | Keep-alive expiry (seconds). |

The token is held as a `SecretStr`; it authenticates requests but never appears in
a log line, repr, or error message.

## Reads and writes

- **Reads** (`load`, `load_bytes`) go through the raw endpoint
  (`raw.githubusercontent.com`), which serves files of any size. The Contents API
  is deliberately **not** used for reads: it silently returns an empty body for a
  file over 1 MiB, so a large object would read as empty bytes. The raw endpoint
  is CDN-cached, so a read shortly after a write may return the previous content
  for up to ~5 minutes.
- **Writes** (`upload`, `upload_bytes`) go through the Contents API (base64) and
  are capped conservatively at 1 MiB. A larger payload raises `ValueError` before
  the request rather than truncating; the API's own `422` is the backstop.
- **Listing** uses the recursive Git Trees API (one request) and raises loudly on
  a `truncated` response rather than acting on a partial listing.
- **`stat`** infers `content_type` from the path suffix — GitHub stores no
  per-object content-type.

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
