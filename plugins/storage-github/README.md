# tai42-storage-github

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

A GitHub-backed `Storage` provider for the TAI ecosystem. It stores content as
files in a GitHub repository and serves the full `tai42_contract.storage.Storage`
surface — the text methods (`load` / `list` / `upload` / `delete` / `delete_dir`)
plus the metadata read `stat`, the bounded read `load_range` and the binary
methods (`load_bytes` / `upload_bytes` / `load_object`).

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

- **Reads** (`load`, `load_bytes`, `load_object`) are one Contents API call each at the
  configured branch, asking for the raw form
  (`Accept: application/vnd.github.raw+json`), which serves every byte of a file
  of any size. A directory answers its JSON listing and reads as
  `FileNotFoundError`. `load_object` infers the content type from the path suffix,
  as `stat` does.
- **Ranged reads** (`load_range`) resolve the branch's head commit through the
  Git refs API, then read `raw.githubusercontent.com` at that commit with a
  `Range` header and `Accept-Encoding: identity` (the raw host compresses, and a
  compressed answer ranges over the encoded bytes): two calls, one of them REST.
  Reading at the commit, never at the branch name, means a ranged read answers the
  branch's current bytes, not a cached copy. An answer that ignores the range or
  is still encoded raises; there is no whole-object read in its place. A
  zero-length range sends no request.
- **Rate limit**: GitHub allows 60 REST calls an hour without a token and 5000
  with one; `stat`, `load` and `load_bytes` cost one REST call each and
  `load_range` one.
- **Writes** (`upload`, `upload_bytes`) go through the Contents API (base64) and
  are capped conservatively at 1 MiB. A larger payload raises `ValueError` before
  the request rather than truncating; the API's own `422` is the backstop.
- **Listing** uses the recursive Git Trees API (one request) and raises loudly on
  a `truncated` response rather than acting on a partial listing. `list(directory)`
  reads the tree of that directory alone (`<branch>:<directory>`), so it grows with
  the directory, not the repository; a directory that does not exist answers `[]`
  once the branch is confirmed to exist. `delete_dir` lists the directory the same
  way.
- **`stat`** is one Contents API call asking for the object form
  (`Accept: application/vnd.github.object+json`): it answers the file's `size`,
  and infers `content_type` from the path suffix — GitHub stores no per-object
  content-type. A missing path and a directory raise `FileNotFoundError`.

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
