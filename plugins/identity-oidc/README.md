# tai42-identity-oidc

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

The validate-only **OIDC identity provider** for the TAI ecosystem — an
installable plugin that registers itself as the `"identity-oidc"` identity
provider and resolves an **issuer-minted JWT** presented on an API call to an
authenticated identity.

Importing the package registers the provider in `tai42-contract`'s module-level
identity-provider registry (`register_identity_provider("identity-oidc", ...)`),
with no `tai42_app` handle involved — so it registers in any process that imports
it, including ones that never `start()`. A deployment selects it by including
`identity-oidc` in the access-control `auth_providers` list.

Its only tai-* dependencies are `tai42-contract` (the identity ABCs and the
registry it registers through) and `tai42-kit[jwt]` (the OIDC discovery / JWKS /
JWT-verify helper, `tai42_kit.net.jwt`). It **never** imports the skeleton — the
plugin is contract-facing, and the import is banned by ruff.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. An identity
provider is how the runtime answers "who is this caller?": it resolves an
inbound credential to an authenticated identity, and access control decides what
that identity may do. This package is one such provider (issuer-minted OIDC
JWTs, validate-only); any package can back the same contract, so this repo is
this provider's own full doc home, and the documentation site covers the
platform-level story:

- Access-control concept: https://tai42.ai/concepts/access-control
- Build an identity provider (author guide): https://tai42.ai/guides/authors/identity-provider
- Ecosystem catalog: https://tai42.ai/reference/catalog

## Validate-only — no key minting, no stored state

This provider implements the base
`tai42_contract.access_control.identity.IdentityProvider` ABC — deliberately **not**
`ApiKeyIdentityProvider`. It mints no keys and holds no state: keys are managed at
the external issuer, so the skeleton's key-minting surface refuses this provider
loudly (Studio renders "keys are managed at the external issuer" rather than a raw
error). It is the ecosystem's proof of the mint-capability gate.

## How a token resolves

On each request the presented token runs a cheap structural gate first
(`looks_like_jwt` — three dot-separated base64url segments), so `sk-…` keys and
`tai-sess-…` session tokens fall through the provider chain with a clean `None`
and no network fetch. A JWT-shaped token is verified against the issuer's JWKS
(discovered once, cached with TTL and bounded unknown-`kid` refetch):

- `alg` must be in the configured allowlist **before** any key lookup, so
  `alg=none` and symmetric downgrades are rejected by construction.
- `iss` must match exactly, `aud` must contain the configured audience, and
  `exp` is required (with leeway).

A JWT-shaped token that fails verification **raises** — it is an attack or a
misconfiguration, never someone else's credential — and the request denies
(fail-closed). The verified claims become `AuthIdentity.claims`; the user id is
the **namespaced** subject `idp:{issuer}:{claim}` (`claim` defaults to `sub`).

The `idp:{issuer}:` prefix fences issuer subjects into their own slice of the
single flat policy `user_id` namespace they share with `usr-*` accounts, `sk-*`
key ids, and `oidc:*` login subjects — so a trusted issuer emitting a `sub` equal
to a privileged principal's id cannot inherit that principal's policy.

The reserved `owner_user_id` claim is stripped from the returned claims as
defense-in-depth: it is authoritative only from the key-mint path, and the
skeleton verifier's central strip already removes it for every non-mintable
provider.

## Deny-by-default

An issuer-authenticated subject with **no** operator-provisioned policy resolves
to the empty `AccessPolicy`, which holds no scopes, so every protected route
denies. There is no auto-provisioning and no default-policy path. Operators grant
access by creating a policy for each expected subject id under its namespaced
form — `idp:{issuer}:{sub}` — through the existing policy routes.

## Chaining note (v1)

Chaining two JWT providers against different issuers is out of scope for v1: a
single `identity-oidc` member verifies against one configured issuer.

## Configuration

All config is the plugin's own `TAI_IDENTITY_OIDC_*` env namespace (the plugin
never reads skeleton config):

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `TAI_IDENTITY_OIDC_ISSUER` | yes | — | The OIDC issuer URL (discovery is fetched from `{issuer}/.well-known/openid-configuration`). |
| `TAI_IDENTITY_OIDC_AUDIENCE` | yes | — | The audience the token's `aud` must contain. |
| `TAI_IDENTITY_OIDC_ALLOWED_ALGS` | no | `["RS256"]` | JSON array — the signing-algorithm allowlist. Never "any". |
| `TAI_IDENTITY_OIDC_CLAIM` | no | `sub` | The claim mapped into the namespaced user id. |
| `TAI_IDENTITY_OIDC_JWKS_TTL_SECONDS` | no | `3600` | JWKS cache TTL. |

A missing required setting raises a loud settings error at provider
construction (boot).

## Surface

The provider implements `tai42_contract.access_control.identity.IdentityProvider`:

| Method | Does |
|---|---|
| `validate_token(token)` | Gates non-JWT tokens to `None`; verifies a JWT against the issuer's JWKS and returns the `AuthIdentity` with the namespaced user id. A verification failure **raises** (fail-closed). |
| `healthcheck()` | Fetches the issuer's discovery + JWKS at boot; raises loudly if the issuer is unreachable or the documents are broken. |

`readiness_targets()` inherits the base empty tuple — the issuer is not a
kit-pooled client, so the boot `healthcheck()` is the reachability check.

## Requirements

Requires **Python 3.13+** and a reachable OIDC issuer publishing standard
discovery + JWKS. An unreachable or broken issuer is caught loudly by
`healthcheck()` at startup rather than failing per-request.

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-identity-oidc
```

Or from source — clone this repo and add it as an editable dependency; the
`tai42-*` dependencies resolve in-tree from the workspace.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/identity-oidc
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
