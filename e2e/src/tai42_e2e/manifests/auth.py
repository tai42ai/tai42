"""The auth, accounts and OIDC stack profiles."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from tai42_e2e.manifests.feature_env import _base_env, _switch
from tai42_e2e.manifests.tool_entries import (
    _CORE_ROUTERS,
    _EXTENSION_MODULES,
    _PROJECTED_API_TOOLS,
    _builtin_entries,
    _probe_tools_entry,
)
from tai42_e2e.topology import StackConfig, StackResources, Topology

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants


def build_auth_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS with access control ON: the pluggable identity provider (records
    in its own store) + the Postgres policy store."""
    manifest = {
        "default_routers": "none",
        "lifecycle_modules": [variants.identity.lifecycle_module],
        # A deliver-only stub channel (registers on import, mounts no route) so the
        # isolation suite can drive a channel-delivered ask_user — the ticket-contained
        # mode where the callback URL rides the channel — and pin the add-frame carries
        # no ticket.
        "channel_modules": ["tai42_e2e_fixtures.stub_channel"],
        # The login router mounts the always-public claim-exchange door (POST
        # /api/login/claim) the owned-key onboarding leg exchanges against; the
        # notifications router mounts the internal sink's read/send doors the isolation
        # suite drives to prove per-identity audience filtering.
        "routers_modules": [
            *_CORE_ROUTERS,
            "tai42_skeleton.routers.api_keys",
            "tai42_skeleton.routers.login",
            "tai42_skeleton.routers.notifications",
        ],
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env["ACCESS_CONTROL_ENABLE"] = "true"
    env.update(variants.identity.auth_provider_env())
    # Pin BOTH rate-limit windows: exercise the 10-second burst window (L=10),
    # keep the per-minute window high enough that it can never trip first.
    env["TAI_RATE_LIMIT_FAMILIES__UNIVERSAL_WEBHOOK__BURST"] = "10"
    env["TAI_RATE_LIMIT_FAMILIES__UNIVERSAL_WEBHOOK__LIMIT"] = "1000"
    # Small recent-runs / notifications windows so the owned-key completeness pins can
    # overflow the shared window with a handful of records within the suite timeout (the
    # per-identity index/feed must still return the addressed identity's own record).
    env["TAI_TOOL_RUNS_RECENT_RUNS_LIMIT"] = "3"
    env["INTERACTIONS_NOTIFICATIONS_FEED_MAX"] = "5"
    # A channel-delivered ask_user mints a callback ticket + URL from the public base URL,
    # so this setting is required; the host is never dialed, but it must be an https value.
    env["INTERACTIONS_PUBLIC_BASE_URL"] = "https://e2e.local"
    return StackConfig(
        name="auth",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=True,
        run_metrics=True,
        auth=True,
    )


# The keys-bootstrap stack's known first-key bootstrap token. Pinning a known value
# drives the gated first-admin-key mint deterministically (the auto-token is logged only
# by the SET-NX winner, so a spec could not read it) while still exercising the gate. The
# auto-token SET-NX convergence rests on the skeleton's own unit tests.
_KEYS_BOOTSTRAP_TOKEN = "e2e-keys-bootstrap-token"


def build_keys_bootstrap_stack(res: StackResources, variants: Variants) -> StackConfig:
    """Access control ON with the redis key provider and NO seeded key — a one-worker,
    busless stack so readiness is HTTP ``/health`` alone (no authed MCP drain), the fresh
    install the first-key bootstrap door serves. The public ``/api/keys/bootstrap`` door
    (its own ``routers_modules`` entry) mints the first admin key behind the pinned token;
    the ``api_keys`` router carries the authed ``/api/auth/me`` the minted key then reaches."""
    manifest = {
        "default_routers": "none",
        "lifecycle_modules": [variants.identity.lifecycle_module],
        "routers_modules": [
            *_CORE_ROUTERS,
            "tai42_skeleton.routers.api_keys",
            "tai42_skeleton.routers.keys_bootstrap",
        ],
        "extensions_modules": _EXTENSION_MODULES,
        "tools": [*_builtin_entries()],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env["ACCESS_CONTROL_ENABLE"] = "true"
    env.update(variants.identity.auth_provider_env())
    env["ACCESS_CONTROL_BOOTSTRAP_TOKEN"] = _KEYS_BOOTSTRAP_TOKEN
    return StackConfig(
        name="keys-bootstrap",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=True,
    )


# The accounts stack's known first-owner bootstrap token. Pinning a known value drives
# the gated bootstrap path deterministically (the auto-token is logged only by the
# SET-NX winner, so a spec could not read it) while still exercising the gate. The
# auto-token SET-NX convergence rests on the plugin's own unit tests.
_ACCOUNTS_BOOTSTRAP_TOKEN = "e2e-accounts-bootstrap-token"


# The oidc stack's login provider and the coordinates the two OIDC members share with
# the in-process signing issuer. ``_OIDC_CLIENT_ID`` must equal the ``OAuthIdp``'s
# construction client (the ``aud`` it stamps into id_tokens), which ``accounts-oidc``
# verifies; ``_OIDC_MACHINE_AUDIENCE`` is the audience ``identity-oidc`` requires on
# issuer-minted machine JWTs.
_OIDC_PROVIDER_NAME = "e2e"


_OIDC_CLIENT_ID = "e2e-client"


_OIDC_CLIENT_SECRET = "e2e-secret"


_OIDC_STATE_KEY = "e2e-oidc-state-key"


_OIDC_MACHINE_AUDIENCE = "e2e-machine"


def build_accounts_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS with access control ON, the Postgres accounts provider alongside
    the redis key provider.

    ``accounts-postgres`` owns password login, opaque ``tai-sess-`` sessions, and
    invites in its own ``accounts_*`` tables (applied into the e2e template DB, so
    the per-stack clone carries them); ``redis`` keeps ``sk-`` API keys validatable
    on the same deployment. The public ``/api/login`` aggregator + the plugin's
    login/users routers are mounted, plus the authed ``/api/system/kinds`` door.
    Seeded with a root key (``seed_auth=True``) so a spec can compare key-auth and
    session-auth against one stack. The first-owner bootstrap token is pinned to a
    known value (see ``_ACCOUNTS_BOOTSTRAP_TOKEN``)."""
    manifest = {
        "default_routers": "none",
        "lifecycle_modules": ["tai42_identity_redis", "tai42_accounts_postgres"],
        "routers_modules": [
            *_CORE_ROUTERS,
            "tai42_skeleton.routers.api_keys",
            "tai42_skeleton.routers.login",
            "tai42_skeleton.routers.system_kinds",
            "tai42_accounts_postgres.routes_login",
            "tai42_accounts_postgres.routes_users",
        ],
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env["ACCESS_CONTROL_ENABLE"] = "true"
    # Ordered resolution: the accounts provider claims its own session tokens, the
    # redis provider claims sk- keys; a non-matching provider is a MISS, not an error.
    env["ACCESS_CONTROL_AUTH_PROVIDERS"] = json.dumps(["accounts-postgres", "redis"])
    # The access-control policy store binds to the ``default`` database (skeleton
    # component); the accounts plugin binds to the SAME clone (its component's binding
    # also defaults to ``default``), the template carrying both schemas — both resolve
    # through the default database _base_env already declares; no per-store PG env here.
    env["TAI_ACCOUNTS_BOOTSTRAP_TOKEN"] = _ACCOUNTS_BOOTSTRAP_TOKEN
    # The plugin's rate-limit counters + bootstrap token ride the same ACCESS_CONTROL_REDIS_URL
    # the identity-provider factory receives; sessions live in Postgres, so no plugin Redis
    # env exists. /api/login needs no path/pattern env — its always-public prefix makes the
    # login namespace public code-side.
    return StackConfig(
        name="accounts",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=True,
        run_metrics=True,
        auth=True,
    )


def build_oidc_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The accounts stack plus the two OIDC members, both pointed at the in-process
    signing issuer (``oidc_idp.OAuthIdp``).

    ``accounts-oidc`` adds browser-less OIDC login (authorize -> issuer -> callback
    mints a ``tai-sess-`` session, subjects namespaced ``oidc:{provider}:{sub}``);
    ``identity-oidc`` validates issuer-minted machine JWTs (subjects namespaced
    ``idp:{issuer}:{sub}``). ``TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL`` is filled at boot
    with replica B's own origin (loopback ``http`` is accepted for e2e), so a login
    spec drives the flow against replica B; ``TAI_IDENTITY_OIDC_AUDIENCE`` is the
    audience the issuer stamps into machine JWTs a spec mints for replica B.

    The ``oidc`` seam swaps the in-process issuer for a real Auth0 tenant (HARNESS-MAP:
    ``AUTH0_*`` -> a ``TAI_ACCOUNTS_OIDC_PROVIDERS`` row ``preset:"auth0"`` +
    ``TAI_IDENTITY_OIDC_ISSUER`` / ``_AUDIENCE``); the ``github-login`` seam ADDS a real
    ``preset:"github"`` provider row (fed from ``GITHUB_LOGIN_*`` — real-only, no mock
    issuer exists). Both are inbound, so the login redirect origin routes to the public
    base URL. All-mock (default) is byte-for-byte today's in-process-issuer wiring."""
    from dataclasses import replace

    switch = _switch()
    oidc_real = switch.is_real("oidc")
    gh_real = switch.is_real("github-login")

    if not oidc_real and res.oidc_issuer_base_url is None:
        raise RuntimeError("build_oidc_stack requires resources.oidc_issuer_base_url (the signing OIDC issuer origin)")

    base = build_accounts_stack(res, variants)
    manifest = {**base.manifest}
    manifest["lifecycle_modules"] = [*base.manifest["lifecycle_modules"], "tai42_accounts_oidc", "tai42_identity_oidc"]
    manifest["routers_modules"] = [*base.manifest["routers_modules"], "tai42_accounts_oidc.routes"]
    env = {**base.env}
    env["ACCESS_CONTROL_AUTH_PROVIDERS"] = json.dumps(["accounts-postgres", "accounts-oidc", "identity-oidc", "redis"])
    # accounts-oidc login provider row(s). MOCK: one row whose issuer is the in-process
    # IdP (client_id is the IdP's construction client — the id_token ``aud`` the callback
    # verifies; the secret is a fixture value the stub IdP never checks). REAL oidc: a real
    # Auth0 row (preset fills the label; the operator supplies the per-tenant issuer).
    providers: list[dict] = []
    if oidc_real:
        providers.append(
            {
                "name": "auth0",
                "preset": "auth0",
                "issuer": os.environ["AUTH0_ISSUER"],
                "client_id": os.environ["AUTH0_CLIENT_ID"],
                "client_secret": os.environ["AUTH0_CLIENT_SECRET"],
                "claim": "sub",
            }
        )
        # identity-oidc validates machine JWTs against the same real issuer + API audience.
        env["TAI_IDENTITY_OIDC_ISSUER"] = os.environ["AUTH0_ISSUER"]
        env["TAI_IDENTITY_OIDC_AUDIENCE"] = os.environ["AUTH0_AUDIENCE"]
    else:
        # The guard above raised unless the in-process issuer origin is present here.
        assert res.oidc_issuer_base_url is not None
        providers.append(
            {
                "name": _OIDC_PROVIDER_NAME,
                "issuer": res.oidc_issuer_base_url,
                "client_id": _OIDC_CLIENT_ID,
                "client_secret": _OIDC_CLIENT_SECRET,
                "claim": "sub",
            }
        )
        # identity-oidc: validate-only, same issuer, the machine-JWT audience. RS256 is
        # the default allowed alg (the issuer signs RS256); the subject claim is ``sub``.
        env["TAI_IDENTITY_OIDC_ISSUER"] = res.oidc_issuer_base_url
        env["TAI_IDENTITY_OIDC_AUDIENCE"] = _OIDC_MACHINE_AUDIENCE
    if gh_real:
        # A real GitHub OAuth app via the plain-OAuth2 ``github`` preset (fixed endpoints,
        # no discovery/id_token). Real-only — the in-process issuer has no github mode.
        providers.append(
            {
                "name": "github",
                "preset": "github",
                "client_id": os.environ["GITHUB_LOGIN_CLIENT_ID"],
                "client_secret": os.environ["GITHUB_LOGIN_CLIENT_SECRET"],
            }
        )
    env["TAI_ACCOUNTS_OIDC_PROVIDERS"] = json.dumps(providers)
    env["TAI_ACCOUNTS_OIDC_STATE_KEY"] = _OIDC_STATE_KEY
    # A real login provider registers its OAuth redirect at the public origin, so the
    # login base URL routes there instead of replica-B loopback; empty on all-mock.
    oidc_public_keys = ["TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL"] if (oidc_real or gh_real) else []
    return replace(
        base,
        name="oidc",
        manifest=manifest,
        env=env,
        replica_b_origin_env_keys=["TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL"],
        public_base_url_env_keys=oidc_public_keys,
        public_base_url=switch.public_base_url,
    )
