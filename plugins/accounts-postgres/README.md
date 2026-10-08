# tai42-accounts-postgres

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

The Postgres-backed **accounts provider** for the TAI ecosystem — an installable
plugin that owns human user accounts, password login, sessions, and invites. It
registers itself as the `"accounts-postgres"` provider and mints/validates its own
`tai-sess-…` session tokens.

Importing the package registers the provider in `tai42-kit`'s module-level
accounts registry (`tai42_kit.accounts.registry`,
`register_accounts_provider("accounts-postgres", ...)`), which
ALSO lands the factory in the identity registry under the same name — an accounts
provider is the token answerer for its own sessions, so one registration keeps
sessions both mintable and validatable. No `tai42_app` handle is involved, so it
registers in any process that imports it. A deployment selects it by including
`accounts-postgres` in the access-control `auth_providers` list.

Its only tai-* dependencies are `tai42-contract` (the accounts ABC, the injected
admin-services and settings Protocols, and the login-method metadata models) and
`tai42-kit` (the registry it registers through, the Postgres and Redis clients and
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

Three plugin-owned tables in the platform database, created by the plugin's own
migration chain:

- `accounts_users` — one row per human user: the opaque, stable `user_id`
  (`usr-…`), the normalized email, the argon2id password hash (NULL until an
  invite is accepted), the role-template name, and the disabled flag.
- `accounts_sessions` — one row per live login: the SHA-256 hash of the
  `tai-sess-…` token (the raw token is never stored), with sliding-idle
  (`last_seen_at`) and absolute (`absolute_expires_at`) expiry.
- `accounts_invites` — one pending invite per user: the SHA-256 hash of the
  `tai-inv-…` token, its TTL, and its single-use consumption marker.

Rate-limit counters live in Redis (reached through the injected `settings.redis`),
namespaced per deployment.

The plugin NEVER touches the skeleton's `access_control_policies` /
`access_control_routes` tables or any `ac:*` Redis key directly — all policy and
role writes go through the injected `AccountsAdminServices`
(`apply_role` / `remove_policy` / `set_user_disabled`).

## HTTP surface

Public (`/api/login/*`, always-public prefix):

| Route | Does |
|---|---|
| `POST /api/login/password` | Verify email + password, mint a session. Failures-only throttling; uniform 401; argon2 verify on every attempt (503 load-shed under a hash flood). |
| `POST /api/login/invite/accept` | Consume an invite, set the first password, mint a session. |

Authed (`/api/auth/users*`, reserved prefix):

| Route | Does |
|---|---|
| `PUT /api/auth/users/me/password` | Change your own password; every OTHER session is revoked. |

**Self-service password route stays.** `PUT /api/auth/users/me/password` is
**self-service**, not member administration: it acts on the caller's OWN credential,
targets no member row, and is open to every signed-in user (carved out of the
admin fence with `self_service=True`). It is therefore NOT part of the generic
member-actions seam and is the one route this module still ships. Member
administration — inviting a user, sending a new login link, cancelling an
invitation, changing a member's role or access, and removing a member — is no
longer bespoke routes or a bespoke Studio page here: the provider **declares**
those actions (`member_actions`) and performs them (`invoke_member_action`)
through the platform's generic member-actions seam, which the Studio's own generic
Members page renders. The one-time invite link a new invite or a resend produces
surfaces on that action's result.

Logout is NOT here — the skeleton owns the single `POST /api/auth/logout`
dispatcher; this plugin contributes `revoke_session`.

## Configuration

Behavior config is the plugin's own `TAI_ACCOUNTS_*` namespace; the Postgres
connection resolves through the central database registry on the component's
binding (the plugin never reads skeleton config):

| Env var | Default | Meaning |
|---|---|---|
| `TAI_DB_BINDING_TAI42_ACCOUNTS_POSTGRES` | `default` | The named database in the central registry the plugin's tables live in. The bound database is declared under `TAI_DATABASE_<NAME>_PG_*`; the `default` binding is `TAI_DATABASE_DEFAULT_PG_*`. |
| `TAI_ACCOUNTS_SESSION_IDLE_SECONDS` | `86400` | Sliding-idle session expiry. |
| `TAI_ACCOUNTS_SESSION_ABSOLUTE_SECONDS` | `2592000` | Absolute session cap from mint. |
| `TAI_ACCOUNTS_INVITE_TTL_SECONDS` | `259200` | Invite validity from mint. |
| `TAI_ACCOUNTS_LOGIN_BACKOFF_THRESHOLD` | `5` | Consecutive per-account failures before backoff. |
| `TAI_ACCOUNTS_LOGIN_BACKOFF_CAP_SECONDS` | `900` | Max per-account backoff lock. |
| `TAI_ACCOUNTS_LOGIN_IP_MAX_ATTEMPTS` | `30` | Per-IP failed attempts per window. |
| `TAI_ACCOUNTS_LOGIN_IP_WINDOW_SECONDS` | `900` | Per-IP fixed window. |
| `TAI_ACCOUNTS_LOGIN_HASH_CONCURRENCY` | `2 × CPU count` | Max concurrent argon2 verifies (load-shed above). |
| `TAI_ACCOUNTS_LOGIN_HASH_WAIT_SECONDS` | `2.0` | Wait before a login sheds with 503 under hash saturation. |
| `TAI_ACCOUNTS_REDIS_KEY_PREFIX` | = `pg_db` | Per-deployment Redis namespace (derived from `pg_db` when unset). |

**First owner.** The first owner is created by the platform's one-step setup door,
not by this plugin. That door creates the owner principal and, when this provider is
configured, attaches the owner's login through it (a password set now, or an invite
link the owner follows later). The provider implements the login-attachment seam and
ships no first-owner route of its own.

> **Shared Redis / shared `pg_db`:** two deployments that share one Redis AND one
> `pg_db` must set distinct `TAI_ACCOUNTS_REDIS_KEY_PREFIX` values, or they will
> cross-read each other's rate-limit counters.

> **Proxies:** the per-IP throttle reads the direct peer — there is no
> `X-Forwarded-For` parsing. A deployment behind a shared proxy must throttle at
> its ingress, or all callers collapse to one throttled IP.

## Schema migrations + startup guard

The plugin ships an ordered SQL migration chain under `migrations/` and declares
it in `tai-plugin.yml` (`migrations: migrations`). The shared migration runner
applies it — run by the operator with `tai db migrate`, or automatically by the
marketplace install/upgrade flow once the package lands. Applied migrations are
recorded in the per-database `tai_schema_history` table under the component
`tai42-accounts-postgres`, on the DDL-privileged migrator identity.

```bash
tai db migrate     # apply every pending migration across all discovered components
tai db status      # per-component applied / pending / checksum verdicts
```

The provider's boot healthcheck asserts the chain is fully applied — a pending
migration or a checksum mismatch fails startup loudly naming `tai db migrate` —
and fires only when the accounts store is configured (the database bound by
`TAI_DB_BINDING_TAI42_ACCOUNTS_POSTGRES` resolves a non-empty password). It NEVER
auto-applies.

The accounts kind requires access control ENABLED. If the routes are mounted while
`ACCESS_CONTROL_ENABLE=false` (so the provider is never instantiated and the admin
services are never injected), boot fails loudly rather than serving a broken door.

## Deployment wiring

In the deployment manifest:

```yaml
lifecycle_modules: ["tai42_accounts_postgres"]                 # provider registration
routers_modules: ["tai42_accounts_postgres.routes_login",
                  "tai42_accounts_postgres.routes_users"]       # login + self-service password
```

Member administration is the Studio's own generic Members page over the provider's
declared member actions, so the plugin ships no Studio bundle of its own.

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
- **Owner login attached at setup:** the platform setup door owns first-owner
  creation; this provider only attaches the owner's login (password or invite) to the
  already-created owner principal, never creating a second owner.

Invite and session tokens are **shown once** — the raw invite link appears only in
the result of the invite / resend member action, and the raw session token only in
the login response that mints it.

## Requirements

Requires **Python 3.13+**, a Postgres reachable through the database bound by
`TAI_DB_BINDING_TAI42_ACCOUNTS_POSTGRES` (default `TAI_DATABASE_DEFAULT_PG_*`),
and a Redis reachable through the injected access-control Redis. Apply the schema with
`tai db migrate` before first serve; an out-of-date schema is caught loudly at
boot.

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

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
