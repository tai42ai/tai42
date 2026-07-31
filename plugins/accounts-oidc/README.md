# tai42-accounts-oidc

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

The **OIDC / OAuth2 login accounts provider** for the TAI ecosystem — an
installable plugin that lets humans sign in through an external identity provider
(Auth0, Google, Okta, Keycloak, Microsoft Entra, or GitHub) and mints opaque
`tai-sess-…` session tokens backed by Redis.

Importing the package registers the provider in `tai42-contract`'s module-level
accounts registry via `register_accounts_provider("accounts-oidc", …)`. That one
call ALSO lands the factory in the identity registry under the same name — an
accounts provider IS the token answerer for its own sessions. No `tai42_app` handle
is involved, so it registers in any process that imports it. A deployment selects
it by including `accounts-oidc` in the access-control `auth_providers` list; the
plugin never inserts itself.

Its only tai-\* dependencies are `tai42-contract` (the accounts ABC, the login-method
models, and the dual-registry registration) and `tai42-kit` (the Redis client, the
session-token hash, and the OIDC/JWT helper). It **never** imports the skeleton —
the plugin is contract-facing, and the import is banned by ruff.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. An accounts
provider owns human sign-in: it authenticates a person, mints the session token
their browser carries, and answers that token back as an identity on every later
call. This package is one such provider (OIDC / OAuth2 login with Redis-backed
sessions); any package can back the same contract, so this repo is this
provider's own full doc home, and the documentation site covers the
platform-level story:

- Accounts concept: https://tai42.ai/concepts/accounts
- Build an accounts provider (author guide): https://tai42.ai/guides/authors/accounts-provider
- Ecosystem catalog: https://tai42.ai/reference/catalog

## The login flow

```
Browser → GET  /api/login/oidc/{provider}/authorize  → 302 issuer (state + PKCE + nonce)
Issuer  → GET  /api/login/oidc/{provider}/callback   → code→token exchange,
                                                        id_token verified (tai42-kit),
                                                        session minted (Redis),
                                                        302 /login?sso={code}
SPA     → POST /api/login/sso/exchange {code}         → {token, user_id} (once)
SPA     → any request with the token                  → validate_token
```

The OAuth callback is a top-level browser navigation, so the session token is
**never** handed back in a URL. The callback mints the session, stores a one-time
SSO code in Redis (`acc:oidc:sso:{code}`, 30 s TTL, single-use `GETDEL`), and 302s
to `/login?sso={code}`; the SPA POSTs that code to `/api/login/sso/exchange` and
receives the frozen `{"data": {"token": …, "user_id": …}}` shape exactly once.

Both the OAuth `redirect_uri` and the `/login?sso=` hand-back are derived from
`TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL` — **never** the inbound request's Host/origin,
so a Host-header-injecting client cannot steer where the auth code or hand-back
land. The `state` parameter is HMAC-signed (`state_key`) AND stored server-side in
Redis with the PKCE verifier and nonce (600 s TTL, single-use `GETDEL`). PKCE is
S256, always.

## Configuration

All config is the plugin's own `TAI_ACCOUNTS_OIDC_*` env namespace.

| Env var | Required | Meaning |
|---|---|---|
| `TAI_ACCOUNTS_OIDC_PROVIDERS` | yes (JSON) | The list of configured login providers (see below). |
| `TAI_ACCOUNTS_OIDC_STATE_KEY` | yes | HMAC key for the tamper-evident OAuth `state` parameter. |
| `TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL` | yes | The public origin the flow returns to (e.g. `https://studio.example.com`). `https` required (plain `http` only for loopback). |
| `TAI_ACCOUNTS_OIDC_SESSION_IDLE_SECONDS` | no (86400) | Sliding idle window; a session dies once unused this long. |
| `TAI_ACCOUNTS_OIDC_SESSION_ABSOLUTE_SECONDS` | no (2592000) | Hard cap from mint, never extended by activity. |

A missing `STATE_KEY` or `PUBLIC_BASE_URL` raises a `ValueError` naming the env var
at provider construction (boot), never a silent default.

`TAI_ACCOUNTS_OIDC_PROVIDERS` is a JSON array; each row:

```json
[
  {
    "name": "google",
    "preset": "google",
    "client_id": "…apps.googleusercontent.com",
    "client_secret": "…"
  },
  {
    "name": "corp",
    "preset": "okta",
    "issuer": "https://corp.okta.com",
    "client_id": "…",
    "client_secret": "…",
    "scopes": ["openid", "email", "profile"],
    "claim": "sub",
    "display": { "label": "Sign in with Corp SSO", "icon": null }
  }
]
```

- `name` — URL-safe slug (`^[a-z0-9-]+$`), unique across the list (a duplicate
  raises at settings load).
- `preset` — one of `auth0`, `google`, `okta`, `keycloak`, `azure`, `github`, or
  omit it for a raw OIDC issuer. A preset fills the button label and, for `google`,
  the fixed issuer.
- `issuer` — the OIDC issuer URL. Required unless a preset fixes it (`google`); the
  per-tenant presets (`auth0`/`okta`/`keycloak`/`azure`) and a raw row require it,
  and a row missing its required issuer raises at settings load.
- `client_id` / `client_secret` — the OAuth client credentials.
- `scopes` — default `["openid", "email", "profile"]`.
- `claim` — the id_token (or GitHub userinfo) claim mapped into the user id;
  default `sub` (GitHub defaults to `id`).
- `display.label` / `display.icon` — button text (a preset provides a default) and
  an optional operator-supplied inline SVG. **No brand marks ship in this repo.**

### Preset table

| Preset | Kind | Issuer | Default label |
|---|---|---|---|
| `auth0` | OIDC | required (per-tenant) | Continue with Auth0 |
| `google` | OIDC | fixed `https://accounts.google.com` | Continue with Google |
| `okta` | OIDC | required (per-tenant) | Continue with Okta |
| `keycloak` | OIDC | required (per-tenant) | Continue with Keycloak |
| `azure` | OIDC | required (per-tenant) | Continue with Microsoft |
| `github` | OAuth2 | n/a (fixed endpoints) | Continue with GitHub |

`github` is the plain-OAuth2 path: no OIDC discovery, no id_token. It uses GitHub's
fixed authorize/token endpoints and identifies the caller via `GET /user`, mapping
by the numeric `id` claim; user ids resolve as `oidc:github:{id}`.

## Identities and access

A caller authenticated through provider `{name}` resolves to the user id
`oidc:{name}:{claim}`. **There is no auto-provisioning.** An issuer-authenticated
subject with no pre-provisioned policy resolves to the empty access policy — every
protected route denies. Operators grant access by creating a policy per expected
subject id through the existing policy routes. This is the only v1 behavior; the
plugin ships no default-role or auto-policy path.

The session record — and therefore the identity's claims — holds ONLY plugin-minted
fields (`user_id`, `created_at`, `absolute_deadline`); the issuer's JWT claims are
used at the callback to derive the user id and are never carried into the identity,
so no external `owner_user_id` can reach the backend attenuation through this member.

## Sessions

Sessions are Redis TTL records at `acc:oidc:sess:{sha256(token)}`. Idle expiry
rides the Redis TTL (slid on each successful validation); the absolute deadline is
an independent hard cap enforced in `validate_token`. Only the SHA-256 hash of the
token is stored.

### Running both `tai-sess-` accounts members

If a deployment installs BOTH this provider and `tai42-accounts-postgres`, they share
the `tai-sess-` prefix and can only tell "not mine" from "mine" by a store lookup.
Because a provider error propagates (never falls through to a later provider —
correct for security), a store outage in an EARLIER `auth_providers` member
fail-CLOSES a LATER member's sessions: an `accounts-oidc` (Redis-backed) session
presented while an earlier `accounts-postgres` is down hits the Postgres provider
first, raises, and denies — even though that token belongs to the healthy provider.
This is fail-closed (safe) and inherent to the shared prefix; an operator running
both members should know an earlier member's backend health gates later members'
logins.

## Requirements

Requires **Python 3.13+**, any plain Redis, and reachable OIDC issuers. An
unreachable issuer or Redis is caught loudly by `healthcheck()` at startup rather
than failing per-request. Chaining two different JWT-issuer providers against
distinct issuers is out of scope for v1.

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-accounts-oidc
```

Or from source — clone this repo and add it as an editable dependency. Clone
`tai-contract` and `tai-kit` beside this repo first — `[tool.uv.sources]`
resolves them from sibling paths.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/accounts-oidc
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
