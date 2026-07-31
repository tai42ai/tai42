# tai42-identity-redis

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

The Redis-backed **api-key identity provider** for the TAI ecosystem — an
installable plugin that registers itself as the `"redis"` identity provider and
resolves an inbound api key to an authenticated identity.

Importing the package registers the provider in `tai42-contract`'s module-level
identity-provider registry (`register_identity_provider("redis", ...)`), with no
`tai42_app` handle involved — so it registers in any process that imports it,
including ones that never `start()`. A deployment selects it by including
`redis` in the access-control `auth_providers` list.

Its only tai-* dependencies are `tai42-contract` (the identity ABCs and the
registry it registers through) and `tai42-kit` (the Redis client, the hash typing
seams, and the api-key hash). It **never** imports the skeleton — the plugin is
contract-facing, and the import is banned by ruff.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. An identity
provider is how the runtime answers "who is this caller?": it resolves an
inbound credential to an authenticated identity, and access control decides what
that identity may do. This package is one such provider (api keys over Redis);
any package can back the same contract, so this repo is this provider's own full
doc home, and the documentation site covers the platform-level story:

- Access-control concept: https://tai42.ai/concepts/access-control
- Build an identity provider (author guide): https://tai42.ai/guides/authors/identity-provider
- Ecosystem catalog: https://tai42.ai/reference/catalog

## What it stores

The provider owns the whole api-key **identity record** in its own plain-Redis
storage:

- `ac:key:{sha256(raw)}` — a Redis hash `{"user_id", "description"}` (the
  identity a raw key resolves to).
- `ac:management:key:{user_id}` — the `user_id -> hash` reverse lookup, so a user
  id resolves to its stored hash for revoke/edit without a scan.

Key material is never stored — only the SHA-256 hash of the raw key.

## Surface

The provider implements `tai42_contract.access_control.identity.ApiKeyIdentityProvider`:

| Method | Does |
|---|---|
| `validate_token(token)` | Reads `ac:key:{hash}` and returns the `AuthIdentity`, or `None` for an unknown token. A backend error fails closed by **raising**. |
| `provision(user_id, description, *, owner_user_id=None)` | Mints a raw `sk-…` key, writes the identity record + reverse lookup in one transaction, and returns the **raw** key (surfaced once). When `owner_user_id` is given, it is stored in the identity record as the owner claim, so `validate_token` surfaces it for per-request attenuation. |
| `revoke(user_id)` | Deletes the identity record + reverse lookup; `False` if the user is unknown. |
| `update_description(user_id, description)` | Rewrites the record's `description`; `False` if the user is unknown. |
| `list_identities()` | SCANs `ac:key:*` and returns every stored `(user_id, description)`. |
| `healthcheck()` | Probes the provider's OWN Redis storage; raises loudly if the record store is unreachable or broken. |

## Requirements

Requires **Python 3.13+** and any plain Redis — no modules required. The identity
records are plain Redis hashes, so `redis`, `valkey`, or any module-less
`redis-server` works. An unreachable or broken store is caught loudly by
`healthcheck()` at startup rather than failing per-request.

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-identity-redis
```

Or from source — clone this repo and add it as an editable dependency. Clone
`tai-contract` and `tai-kit` beside this repo first — `[tool.uv.sources]`
resolves them from sibling paths.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/identity-redis
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

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
