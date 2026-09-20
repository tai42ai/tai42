"""The marketplace stack profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_e2e.manifests.feature_env import _base_env, _switch
from tai42_e2e.manifests.tool_entries import _CORE_ROUTERS, _PROJECTED_API_TOOLS, _builtin_entries, _probe_tools_entry
from tai42_e2e.topology import StackConfig, StackResources, Topology

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants


def build_marketplace_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1) with the marketplace client wired at the harness-run
    registry: the marketplace router, a short advisories poll, and the package
    index the installer resolves wheels from.

    One worker so an install's manifest patch + reload is observed on a deterministic
    process (a fleet could serve the post-reload tool listing off a not-yet-reloaded
    worker). No backend, no metrics sidecar — nothing marketplace-shaped touches
    either."""
    if res.marketplace_url is None or res.package_index_url is None:
        raise RuntimeError(
            "build_marketplace_stack requires resources.marketplace_url and resources.package_index_url; "
            "the marketplace_stack fixture allocates the registry + package index and passes them as resource_kwargs"
        )
    manifest = {
        "default_routers": "none",
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.marketplace"],
        # The probe entry attaches a proxy branch, so the proxy extension must load
        # alongside prometheus or extension validation aborts boot.
        "extensions_modules": ["tai42_toolbox.extensions.prometheus", "tai42_toolbox.extensions.proxy"],
        "storage_module": variants.storage.module,
        "tools": [_probe_tools_entry(with_backend_branches=False), *_builtin_entries()],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env["MARKETPLACE_URL"] = res.marketplace_url
    env["MARKETPLACE_ADVISORIES_POLL"] = "true"
    env["MARKETPLACE_ADVISORIES_INTERVAL_S"] = "1"
    # The attribution store binds to the ``default`` database (skeleton component)
    # _base_env already declares — the stack's own per-run clone.
    # The installer shells ``sys.executable -m pip install`` inheriting the worker env,
    # so this pip knob reaches it; the PEP 503 root is /simple/. REAL marketplace-pypi
    # drops the fixture index so pip resolves the tai42 packages from real pypi.org (the
    # registry-side ingest repoint — MP_PYPI_BASE_URL / MP_GITHUB_API_BASE — lives in the
    # harness-run marketplace runner, ``marketplace.py``, outside these two files).
    if not _switch().is_real("marketplace-pypi"):
        env["PIP_INDEX_URL"] = f"{res.package_index_url}/simple/"
    return StackConfig(
        name="marketplace",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )


def build_marketplace_prefix_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The marketplace stack with a persistent plugin prefix configured
    (``TAI_PLUGINS_PREFIX``) — the restart-survival home.

    The prefix is a directory under the stack root (a sibling of ``storage/``), so
    it OUTLIVES a serve-process restart and is torn down only with the stack. With
    it set, an install lands the plugin's own distribution UNDER the prefix (never
    in the shared editable venv), and boot re-adds the prefix to ``sys.path`` so the
    plugin's tools re-import after a restart. Same single-worker, backendless shape
    as the marketplace stack it derives from."""
    from dataclasses import replace
    from pathlib import Path

    base = build_marketplace_stack(res, variants)
    prefix_dir = Path(res.storage_root).parent / "plugin-prefix"
    env = {**base.env, "TAI_PLUGINS_PREFIX": str(prefix_dir)}
    return replace(base, name="marketplace-prefix", env=env)


def build_marketplace_quarantine_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The marketplace-prefix stack with the zeta compat fixture's tool module
    already wired into the manifest — the home of the boot-quarantine spec.

    The manifest carries the installer-shaped config row
    (``{"title": <module>, "module": <module>}``) for zeta's tool module, exactly
    what an install's manifest patch persists. The spec's fixture completes the
    picture BEFORE boot: it installs the zeta wheel into the plugin prefix and
    seeds its attribution row, forging the state a core upgrade strands an
    installed plugin in — present on disk, wired in the manifest, attributed in
    the store, its declared contract range excluding the running contract."""
    from dataclasses import replace

    from tai42_e2e.fixture_catalog import ZETA_TOOLS_MODULE

    base = build_marketplace_prefix_stack(res, variants)
    manifest = {
        **base.manifest,
        "tools": [*base.manifest["tools"], {"title": ZETA_TOOLS_MODULE, "module": ZETA_TOOLS_MODULE}],
    }
    return replace(base, name="marketplace-quarantine", manifest=manifest)


def build_marketplace_authz_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The marketplace stack with access control ON — the home of the declared-public
    route tier / per-method pin.

    Same marketplace wiring as ``build_marketplace_stack`` (registry client, package
    index, install door) with the identity provider + Postgres policy store wired ON,
    so an installed plugin's declared-PUBLIC route answers unauthenticated while its
    sibling AUTHED route/method still rejects an anonymous caller. Booted with
    ``seed_auth=True`` (the fixture passes it to ``boot_stack``) so the install door is
    reachable with the seeded root key and the readiness probes stay public."""
    from dataclasses import replace

    base = build_marketplace_stack(res, variants)
    manifest = {**base.manifest, "lifecycle_modules": [variants.identity.lifecycle_module]}
    env = {**base.env, "ACCESS_CONTROL_ENABLE": "true", **variants.identity.auth_provider_env()}
    return replace(base, name="marketplace-authz", manifest=manifest, env=env, auth=True)


def build_marketplace_connectors_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The marketplace stack with the connectors surface mounted — the home of the
    descriptor-only (``source='spec'``) connector install lifecycle.

    Same single-worker marketplace wiring as ``build_marketplace_stack`` (registry
    client, package index, install door) PLUS the connectors router over a live connector
    store (bound to the ``default`` database, random per-stack crypto keys) so an
    installed connector descriptor's provider is listed at ``GET /api/connectors/providers``
    and a no-auth connect launches its managed stdio MCP server. The stack's OAuth/MCP
    stub base (``res.idp_base_url``) is the value the seeded iota descriptor's endpoints
    are rendered against, so the installed manifest's connector matches this stack's IdP.
    One worker so an install's manifest patch + reload is observed on a deterministic
    process."""
    if res.marketplace_url is None or res.package_index_url is None:
        raise RuntimeError(
            "build_marketplace_connectors_stack requires resources.marketplace_url and resources.package_index_url; "
            "the fixture allocates the registry + package index and passes them as resource_kwargs"
        )
    manifest = {
        "default_routers": "none",
        "routers_modules": [
            *_CORE_ROUTERS,
            "tai42_skeleton.routers.marketplace",
            "tai42_skeleton.routers.connectors",
        ],
        # The probe entry attaches a proxy branch, so the proxy extension must load
        # alongside prometheus or extension validation aborts boot.
        "extensions_modules": ["tai42_toolbox.extensions.prometheus", "tai42_toolbox.extensions.proxy"],
        "storage_module": variants.storage.module,
        "tools": [_probe_tools_entry(with_backend_branches=False), *_builtin_entries()],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env["MARKETPLACE_URL"] = res.marketplace_url
    env["MARKETPLACE_ADVISORIES_POLL"] = "true"
    env["MARKETPLACE_ADVISORIES_INTERVAL_S"] = "1"
    if not _switch().is_real("marketplace-pypi"):
        env["PIP_INDEX_URL"] = f"{res.package_index_url}/simple/"
    # The connector store binds to the ``default`` database (skeleton component) _base_env
    # already declares, so it is live; its Redis cache rides the shared feature Redis.
    if res.connectors_kek is not None:
        env["CONNECTORS_KEK"] = res.connectors_kek
    if res.connectors_state_hmac_key is not None:
        env["CONNECTORS_STATE_HMAC_KEY"] = res.connectors_state_hmac_key
    return StackConfig(
        name="marketplace-connectors",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
        # The connect flow signs the deployment origin and validates the request-derived
        # redirect_uri against this allowlist fail-closed; the app port is known only at
        # boot, so the stack fills it with its own origin.
        origin_allowlist_env_keys=["CONNECTORS_REDIRECT_URI_ALLOWLIST"],
    )
