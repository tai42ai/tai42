"""The auth and accounts stack profiles."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tai42_e2e.manifests.feature_env import _base_env
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


def _auth_manifest(variants: Variants) -> dict:
    """The REPLICAS access-control manifest the auth and owned-keys stacks share: the
    pluggable identity provider, the stub channel, and the api-keys / login /
    notifications routers on top of the core set."""
    return {
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


def _auth_env(res: StackResources, variants: Variants) -> dict[str, str]:
    """The REPLICAS access-control env the auth and owned-keys stacks share: access
    control on, the identity provider's own auth-provider env, the pinned rate-limit
    windows, and the small recent-runs / notifications windows the completeness pins
    overflow."""
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
    return env


def build_auth_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS with access control ON: the pluggable identity provider (records
    in its own store) + the Postgres policy store."""
    return StackConfig(
        name="auth",
        topology=Topology.REPLICAS,
        manifest=_auth_manifest(variants),
        env=_auth_env(res, variants),
        run_backend=True,
        run_metrics=True,
        auth=True,
    )


def build_owned_keys_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The auth stack plus the Postgres accounts provider — the owned-key suite's own
    deployment.

    The suite mints owners two ways: a human principal's accounts session mints
    self-owned keys from below (subset-checked), and the seeded admin mints keys owned
    by a service principal. Both need the accounts provider (password login, opaque
    ``tai-sess-`` sessions, invites) and the principals door alongside the identity
    provider that answers ``sk-`` keys, so this builder adds the accounts lifecycle
    module, the principals + accounts login/users routers, and orders the accounts
    provider ahead of the identity provider in the resolution chain. Every other axis is
    the shared auth profile, so ``auth_stack`` stays untouched for its own suites."""
    manifest = _auth_manifest(variants)
    manifest["lifecycle_modules"] = [variants.identity.lifecycle_module, "tai42_accounts_postgres"]
    manifest["routers_modules"] = [
        *manifest["routers_modules"],
        "tai42_skeleton.routers.principals",
        "tai42_accounts_postgres.routes_login",
        "tai42_accounts_postgres.routes_users",
    ]
    env = _auth_env(res, variants)
    # Ordered resolution: the accounts provider claims its own session tokens, the
    # identity provider claims sk- keys; a non-matching provider is a MISS, not an error.
    env["ACCESS_CONTROL_AUTH_PROVIDERS"] = json.dumps(["accounts-postgres", variants.identity.name])
    return StackConfig(
        name="owned-keys",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=True,
        run_metrics=True,
        auth=True,
    )


# The setup door's known token. Pinning a known value drives the gated ``POST /api/setup``
# initialize path deterministically (the auto-token is logged only by the SET-NX winner, so
# a spec could not read it) while still exercising the gate. The auto-token SET-NX
# convergence rests on the skeleton's own unit tests. The Studio UI lane matches it through
# ``e2e/ui/tests/helpers.ts`` ``SETUP_TOKEN``.
_SETUP_TOKEN = "e2e-setup-token"


def build_setup_stack(res: StackResources, variants: Variants) -> StackConfig:
    """Access control ON with the redis key provider and NO seeded key — a one-worker,
    busless stack so readiness is HTTP ``/health`` alone (no authed MCP drain), the fresh
    install the setup door serves. The public ``/api/setup`` door (its own ``routers_modules``
    entry) initializes the deployment behind the pinned setup token; ``login`` makes
    ``needs_setup`` observable, ``principals`` carries the principal listing, and ``api_keys``
    carries the authed ``/api/auth/me`` the owner's minted key then reaches."""
    manifest = {
        "default_routers": "none",
        "lifecycle_modules": [variants.identity.lifecycle_module],
        "routers_modules": [
            *_CORE_ROUTERS,
            "tai42_skeleton.routers.api_keys",
            "tai42_skeleton.routers.setup",
            "tai42_skeleton.routers.login",
            "tai42_skeleton.routers.principals",
        ],
        "extensions_modules": _EXTENSION_MODULES,
        "tools": [*_builtin_entries()],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env["ACCESS_CONTROL_ENABLE"] = "true"
    env.update(variants.identity.auth_provider_env())
    env["TAI_SETUP_TOKEN"] = _SETUP_TOKEN
    return StackConfig(
        name="setup",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=True,
    )


def build_accounts_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS with access control ON, the Postgres accounts provider alongside
    the redis key provider.

    ``accounts-postgres`` owns password login, opaque ``tai-sess-`` sessions, and
    invites in its own ``accounts_*`` tables (applied into the e2e template DB, so
    the per-stack clone carries them); ``redis`` keeps ``sk-`` API keys validatable
    on the same deployment. The public ``/api/login`` aggregator + the plugin's
    login/users routers are mounted, plus the authed ``/api/system/kinds`` door.
    Seeded with a root key (``seed_auth=True``) so a spec can compare key-auth and
    session-auth against one stack. The setup door is mounted (``routers.setup``) behind the
    pinned ``TAI_SETUP_TOKEN``; the deployment is seeded, so the door answers 409 and a
    fresh owner-through-setup spec uses ``build_accounts_fresh_stack`` instead."""
    manifest = {
        "default_routers": "none",
        "lifecycle_modules": ["tai42_identity_redis", "tai42_accounts_postgres"],
        "routers_modules": [
            *_CORE_ROUTERS,
            "tai42_skeleton.routers.api_keys",
            "tai42_skeleton.routers.login",
            "tai42_skeleton.routers.setup",
            "tai42_skeleton.routers.principals",
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
    env["TAI_SETUP_TOKEN"] = _SETUP_TOKEN
    # The plugin's rate-limit counters ride the same ACCESS_CONTROL_REDIS_URL the
    # identity-provider factory receives; sessions live in Postgres, so no plugin Redis
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


def build_accounts_fresh_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The accounts provider on a one-worker, busless stack with NO seeded owner — the fresh
    install the setup door initializes.

    A fresh access-controlled deployment has no credential, and one cannot be seeded without
    a principal (which would flip ``needs_setup`` false), so the authed MCP readiness drain
    can never run here. A one-worker, backend-less stack keeps readiness to HTTP ``/health``
    alone. Cross-replica session resolution is proven on the seeded REPLICAS accounts stack
    (``test_boot_reload_no_quarantine``); this stack proves setup + login-attach + login on a
    real fresh accounts deployment."""
    manifest = {
        "default_routers": "none",
        "lifecycle_modules": ["tai42_identity_redis", "tai42_accounts_postgres"],
        "routers_modules": [
            *_CORE_ROUTERS,
            "tai42_skeleton.routers.api_keys",
            "tai42_skeleton.routers.login",
            "tai42_skeleton.routers.setup",
            "tai42_skeleton.routers.principals",
            "tai42_skeleton.routers.system_kinds",
            "tai42_accounts_postgres.routes_login",
            "tai42_accounts_postgres.routes_users",
        ],
        "extensions_modules": _EXTENSION_MODULES,
        "tools": [*_builtin_entries()],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env["ACCESS_CONTROL_ENABLE"] = "true"
    env["ACCESS_CONTROL_AUTH_PROVIDERS"] = json.dumps(["accounts-postgres", "redis"])
    env["TAI_SETUP_TOKEN"] = _SETUP_TOKEN
    return StackConfig(
        name="accounts-fresh",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=True,
    )
