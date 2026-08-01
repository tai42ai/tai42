# tai42-accounts-postgres

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

The Postgres-backed **accounts provider** for the TAI ecosystem — an installable
plugin that owns human user accounts, password login, sessions, and invites, and
ships a Studio users-admin UI. It registers itself as the `"accounts-postgres"`
provider and mints/validates its own `tai-sess-…` session tokens.

Importing the package registers the provider in `tai42-contract`'s module-level
accounts registry (`register_accounts_provider("accounts-postgres", ...)`), which
ALSO lands the factory in the identity registry under the same name — an accounts
provider is the token answerer for its own sessions, so one registration keeps
sessions both mintable and validatable. No `tai42_app` handle is involved, so it
registers in any process that imports it. A deployment selects it by including
`accounts-postgres` in the access-control `auth_providers` list.

Its only tai-* dependencies are `tai42-contract` (the accounts ABC, the injected
admin-services and settings Protocols, the login-method metadata models, and the
registry it registers through) and `tai42-kit` (the Postgres and Redis clients and
the session/invite hash). It **never** imports the skeleton — the plugin is
contract-facing, and the import is banned by ruff.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. An accounts
provider owns human sign-in: it authenticates a person, mints the session token
their browser carries, and answers that token back as an identity on every later
call. This package is one such provider (Postgres-backed accounts with password
login, sessions, and invites); any package can back the same contract, so this
repo is this provider's own full doc home, and the documentation site covers the
platform-level story:

- Accounts concept: https://tai42.ai/concepts/accounts
- Build an accounts provider (author guide): https://tai42.ai/guides/authors/accounts-provider
- Ecosystem catalog: https://tai42.ai/reference/catalog

## What it stores

Three plugin-owned tables in the platform database (its own DDL, its own apply
step):

- `accounts_users` — one row per human user: the opaque, stable `user_id`
  (`usr-…`), the normalized email, the argon2id password hash (NULL until an
  invite is accepted), the role-template name, and the disabled flag.
- `accounts_sessions` — one row per live login: the SHA-256 hash of the
  `tai-sess-…` token (the raw token is never stored), with sliding-idle
  (`last_seen_at`) and absolute (`absolute_expires_at`) expiry.
- `accounts_invites` — one pending invite per user: the SHA-256 hash of the
  `tai-inv-…` token, its TTL, and its single-use consumption marker.

Rate-limit counters and the shared bootstrap token live in Redis (reached through
the injected `settings.redis`), namespaced per deployment.

The plugin NEVER touches the skeleton's `access_control_policies` /
`access_control_routes` tables or any `ac:*` Redis key directly — all policy and
role writes go through the injected `AccountsAdminServices`
(`apply_role` / `remove_policy` / `set_user_disabled`).

## HTTP surface

Public (`/api/login/*`, always-public prefix):

| Route | Does |
|---|---|
| `POST /api/login/password` | Verify email + password, mint a session. Failures-only throttling; uniform 401; argon2 verify on every attempt (503 load-shed under a hash flood). |
| `POST /api/login/bootstrap` | Create the first owner behind the secure-by-default gate. |
| `POST /api/login/invite/accept` | Consume an invite, set the first password, mint a session. |

Authed (`/api/auth/users*`, reserved prefix, admin-fenced by the seeded role
conditions — except `PUT /users/me/password`, self-service):

| Route | Does |
|---|---|
| `GET /api/auth/users` | List accounts (never a hash, never a token). |
| `POST /api/auth/users` | Create a user with a NULL password and a one-time invite. |
| `PUT /api/auth/users/{user_id}` | Change role and/or disabled state (credentials-die-first on disable; last-admin guard). |
| `DELETE /api/auth/users/{user_id}` | Delete a user (policy first, then plugin rows; last-admin guard). |
| `POST /api/auth/users/{user_id}/invite` | Regenerate the invite (only while the password is unset). |
| `PUT /api/auth/users/me/password` | Change your own password; every OTHER session is revoked. |

Logout is NOT here — the skeleton owns the single `POST /api/auth/logout`
dispatcher; this plugin contributes `revoke_session`.

## Configuration

All from the plugin's own `TAI_ACCOUNTS_*` namespace (the plugin never reads
skeleton config):

| Env var | Default | Meaning |
|---|---|---|
| `TAI_ACCOUNTS_PG_HOST` / `_PG_PORT` / `_PG_DB` / `_PG_USER` / `_PG_PASSWORD` | `localhost` / `5432` / `tai` / `postgres` / (empty) | Postgres connection for the plugin's tables. |
| `TAI_ACCOUNTS_SESSION_IDLE_SECONDS` | `86400` | Sliding-idle session expiry. |
| `TAI_ACCOUNTS_SESSION_ABSOLUTE_SECONDS` | `2592000` | Absolute session cap from mint. |
| `TAI_ACCOUNTS_INVITE_TTL_SECONDS` | `259200` | Invite validity from mint. |
| `TAI_ACCOUNTS_LOGIN_BACKOFF_THRESHOLD` | `5` | Consecutive per-account failures before backoff. |
| `TAI_ACCOUNTS_LOGIN_BACKOFF_CAP_SECONDS` | `900` | Max per-account backoff lock. |
| `TAI_ACCOUNTS_LOGIN_IP_MAX_ATTEMPTS` | `30` | Per-IP failed attempts per window. |
| `TAI_ACCOUNTS_LOGIN_IP_WINDOW_SECONDS` | `900` | Per-IP fixed window. |
| `TAI_ACCOUNTS_LOGIN_HASH_CONCURRENCY` | `2 × CPU count` | Max concurrent argon2 verifies (load-shed above). |
| `TAI_ACCOUNTS_LOGIN_HASH_WAIT_SECONDS` | `2.0` | Wait before a login sheds with 503 under hash saturation. |
| `TAI_ACCOUNTS_BOOTSTRAP_TOKEN` | (unset) | Operator-supplied first-owner token; overrides the auto-token. |
| `TAI_ACCOUNTS_BOOTSTRAP_OPEN` | `false` | Local/dev opt-out that DISABLES the gate (never the default). |
| `TAI_ACCOUNTS_REDIS_KEY_PREFIX` | = `pg_db` | Per-deployment Redis namespace (derived from `pg_db` when unset). |

**First-owner bootstrap gate (secure by default).** With neither
`TAI_ACCOUNTS_BOOTSTRAP_TOKEN` nor `TAI_ACCOUNTS_BOOTSTRAP_OPEN` set, the gate is
ON and the effective token is auto-generated ONCE at startup and printed to the
server log. It is shared across all processes through Redis (`SET NX`): the first
worker or replica to start wins the write and logs it; every other process reads
the same value — so the default gate is deterministic under BOTH multiple uvicorn
workers AND multiple replicas, with no explicit token. An explicit
`TAI_ACCOUNTS_BOOTSTRAP_TOKEN` still overrides it. `TAI_ACCOUNTS_BOOTSTRAP_OPEN=true`
is the only ungated configuration and logs a loud open-window warning every boot
while no owner exists.

> **Shared Redis / shared `pg_db`:** two deployments that share one Redis AND one
> `pg_db` must set distinct `TAI_ACCOUNTS_REDIS_KEY_PREFIX` values, or they will
> cross-read each other's rate-limit counters and bootstrap token.

> **Proxies:** the per-IP throttle reads the direct peer — there is no
> `X-Forwarded-For` parsing. A deployment behind a shared proxy must throttle at
> its ingress, or all callers collapse to one throttled IP.

## Schema apply + startup guard

The plugin cannot ride the skeleton's `tai db` CLI (it never imports the
skeleton), so it applies its own DDL:

```bash
python -m tai42_accounts_postgres.db apply     # create the three tables (idempotent)
python -m tai42_accounts_postgres.db tables     # list the tables the DDL declares
```

`apply` connects through `TAI_ACCOUNTS_PG_*`. The provider's boot healthcheck
verifies the schema exists and, if it is missing, fails startup loudly naming the
apply command above — it NEVER auto-applies.

The accounts kind requires access control ENABLED. If the routes are mounted while
`ACCESS_CONTROL_ENABLE=false` (so the provider is never instantiated and the admin
services are never injected), boot fails loudly rather than serving a broken door.

## Deployment wiring

In the deployment manifest:

```yaml
lifecycle_modules: ["tai42_accounts_postgres"]                 # provider registration
routers_modules: ["tai42_accounts_postgres.routes_login",
                  "tai42_accounts_postgres.routes_users"]       # the HTTP surface
studio_plugins: ["tai42_accounts_postgres"]                    # the users-admin UI
```

and in access control (example alongside the api-key provider):

```
ACCESS_CONTROL_ENABLE=true
ACCESS_CONTROL_AUTH_PROVIDERS=["accounts-postgres","redis"]
```

## Security model

- **argon2id** password hashing (RFC 9106 library defaults).
- **Hashed at rest:** session and invite tokens are stored only as their SHA-256
  hash; the raw token appears exactly once, in the response that mints it.
- **Uniform login failure:** unknown email and wrong password return a
  byte-identical generic 401, and BOTH run a real argon2 verify (a dummy-hash
  verify on unknown email) so timing does not enumerate users.
- **Failures-only rate limiting:** a correct password is never blocked — only a
  failed attempt records against the per-account and per-IP counters (the per-IP
  dimension reads the direct peer; proxied deployments throttle at ingress). The
  argon2 verify is additionally bounded by a concurrency semaphore that sheds
  with a 503 under a hash flood. Redis being down fails the throttle CLOSED.
- **No-email invites:** the plugin returns an origin-relative `login_path` for the
  admin to hand over; it never sends email and never fabricates an absolute URL.
- **Secure-by-default bootstrap:** the first-owner gate is ON by default via an
  auto-generated one-time token (shared across processes through Redis), compared
  constant-time; the only open configuration is explicit and logs a loud warning.

Invite and session tokens are **shown once** — the create/regenerate response is
the only place the raw invite link or session token appears.

## Requirements

Requires **Python 3.13+**, a Postgres reachable through `TAI_ACCOUNTS_PG_*`, and a
Redis reachable through the injected access-control Redis. Apply the schema with
`python -m tai42_accounts_postgres.db apply` before first serve; a missing schema is
caught loudly at boot.

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-accounts-postgres
```

Or from source — clone this repo and add it as an editable dependency; the
`tai42-*` dependencies resolve in-tree from the workspace.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/accounts-postgres
```

## Development

Two toolchains, one repo — CI gates both.

**Python** (from the repo root):

```bash
uv venv --python 3.13
uv pip install --no-sources --editable ".[dev]"
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
uv run --no-sync pytest --cov --cov-report=term-missing
```

Coverage is gated at 95% (`fail_under` in `pyproject.toml`). CI installs with
`uv sync --locked --python 3.13 --extra dev`, so a stale `uv.lock` fails there.

**Studio bundle**:

```bash
pnpm install --frozen-lockfile
pnpm typecheck
pnpm run format:check
pnpm test
pnpm run build
git diff --exit-code src/tai42_accounts_postgres/studio/
```

The last line is the freshness gate. After any change under `studio-src/` (or the
JS toolchain), run `pnpm run build` and commit the regenerated
`src/tai42_accounts_postgres/studio/` alongside the source — a stale committed
bundle fails CI.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
