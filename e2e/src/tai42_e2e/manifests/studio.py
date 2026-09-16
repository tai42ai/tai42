"""The studio stack profile."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tai42_e2e.manifests.auth import _SETUP_TOKEN
from tai42_e2e.manifests.channels import _web_channel_env
from tai42_e2e.manifests.connectors import (
    _FIXTURE_IDP_BASE_FALLBACK,
    _fixture_connector_descriptors,
)
from tai42_e2e.manifests.feature_env import _base_env, _memory_agent_state_env
from tai42_e2e.manifests.tool_entries import (
    _EXTENSION_MODULES,
    _PROJECTED_API_TOOLS,
    _builtin_entries,
    _probe_tools_entry,
    _toolbox_tools_entry,
)
from tai42_e2e.seeding import STUDIO_PATH_PATTERNS
from tai42_e2e.topology import StackConfig, StackResources, Topology

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants


def build_studio_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(2) + backend + metrics, access control ON, serving the REAL
    built Studio through the skeleton — the browser-e2e profile. One app port
    (the browser origin: the SPA and ``/api`` share it). Loads the selected identity
    provider, the Postgres accounts provider (password login + opaque ``tai-sess-``
    sessions), the github webhook verifier, and the fixture OAuth connector provider.

    Served surface — ``default_routers="all"``: the skeleton mounts its whole
    ``DEFAULT_API_ROUTERS`` set (a curated manifest that omits one leaves its Studio page
    dark), then this manifest's extras, then the SPA catch-all last. So the browser suite
    drives every nav page against the same router set production serves. The only extras
    named here are the accounts plugin's own login + users routes.

    The accounts + redis key providers coexist so the login screen renders its password
    form and keeps the key-paste fallback; ``studio_plugins`` carries the accounts plugin
    so its users-admin page mounts into the Studio shell (its API routers alone mount no
    page). The setup door's gate is pinned to ``_SETUP_TOKEN``.

    TRAP: ``/api/login``'s public-ness comes from the code-side
    ``always_public_path_prefixes`` default, not a route row or ``ACCESS_CONTROL_PATH_PATTERNS``.
    Any stack that sets ``ACCESS_CONTROL_ALWAYS_PUBLIC_PATH_PREFIXES`` REPLACES that default
    wholesale (pydantic env-list semantics) and must re-include ``/api/login``."""
    if res.studio_dist_path is None:
        raise RuntimeError("build_studio_stack requires resources.studio_dist_path (the built Studio dist)")
    manifest = {
        "default_routers": "all",
        "lifecycle_modules": [
            variants.identity.lifecycle_module,
            # Importing the accounts provider registers "accounts-postgres" in both the
            # accounts and identity registries (it answers its own tai-sess- sessions).
            # Its accounts_* tables ride the per-stack clone of the e2e template DB.
            "tai42_accounts_postgres",
            "tai42_webhook_verifier_github",
        ],
        # The fixture OAuth connector provider rides the manifest ``connectors`` field —
        # registered SUT-side through the ``tai42_app.connectors.register_connector`` facet.
        "connectors": _fixture_connector_descriptors(res.idp_base_url or _FIXTURE_IDP_BASE_FALLBACK),
        # The ``"all"`` default set already mounts every core + feature router; the only
        # routers NOT in it are the accounts plugin's own routes, so they are the sole
        # extras named here. The SPA catch-all is force-appended last by the loader.
        "routers_modules": [
            "tai42_accounts_postgres.routes_login",
            "tai42_accounts_postgres.routes_users",
        ],
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True),
            _toolbox_tools_entry(),
            # The reference plugin's demo tools: studio_demo_echo (the custom tool
            # panel's subject) plus studio_demo_form/fail (auto-form fallbacks).
            {"title": "reference-plugin-tools", "module": "reference_plugin.tools"},
            # notify_user projects via ``api_tools`` (the notifications router registers
            # the op); notify_user(channel=None) records to the internal sink the
            # notifications screen renders.
            *_builtin_entries(),
        ],
        "agents": [
            {"title": "tai-agents", "module": "tai42_agents.tools_agent", "include": ["tools_agent"]},
        ],
        # Installed studio plugins whose built ``studio/`` dists the skeleton serves via
        # the injected import map: the reference plugin and the accounts plugin (its
        # API routers alone register no Studio page, so it must be listed here).
        "studio_plugins": ["reference_plugin", "tai42_accounts_postgres"],
        # The web channel: its PUBLIC chat page, asset, stream and answer doors serve the
        # browser widget alongside the Studio, so the ``ui/`` suite can drive a real
        # web-channel ``ask_user`` in the rendered widget. The doors are ``public: true``
        # and bypass access control by their own declaration (as in build_bridge_stack).
        "channel_modules": ["tai42_channel_web.register"],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "notify_user", "reload_config"],
    }
    env = _base_env(res, variants)
    # The web channel's own transcript store + limiter windows, so the widget's chat page
    # mints a session and its stream/answer doors work under the browser origin.
    env.update(_web_channel_env(res))
    env["ACCESS_CONTROL_ENABLE"] = "true"
    # Ordered resolution: the accounts provider claims tai-sess- tokens, the key provider
    # claims sk- keys; a non-matching provider is a MISS, not an error.
    env["ACCESS_CONTROL_AUTH_PROVIDERS"] = json.dumps(["accounts-postgres", variants.identity.name])
    # The access-control policy store and the accounts plugin's Postgres — whose
    # accounts_* tables share this stack's database, the template carrying both
    # schemas — bind to the ``default`` database _base_env already declares; no
    # per-store PG env here.
    # The setup door's gate, pinned to a known value (see ``_SETUP_TOKEN``).
    env["TAI_SETUP_TOKEN"] = _SETUP_TOKEN
    # Tier one of the route mapping: request-path regex -> route template. Tier two
    # (template -> resource id) is seeded into the PG route store by
    # ``seeding.seed_studio_auth`` before boot.
    env["ACCESS_CONTROL_PATH_PATTERNS"] = json.dumps(STUDIO_PATH_PATTERNS)
    env["STUDIO_DIST_PATH"] = res.studio_dist_path
    # Lift the ``root`` rate-limit family for the browser leg. Every SPA request — the
    # index shell AND every JS/CSS asset a page load fans out to — charges the single
    # ``root`` family (``/{spa_path:path}`` has no static stem). Human-paced per-IP
    # traffic never approaches the default budget, but the serial UI suite fires many
    # page loads in quick succession from ONE client bucket (the Playwright loopback),
    # and their aggregate burst occasionally trips the default root ceiling (120/10s),
    # 429-ing a critical JS chunk so the app never boots — a blank page that surfaces as
    # a 60s nav-click timeout. Lift it for the harness exactly as channels_web /
    # interactions_callback / trigger are lifted for their own harness fan-outs.
    env["TAI_RATE_LIMIT_FAMILIES__ROOT__LIMIT"] = "100000"
    env["TAI_RATE_LIMIT_FAMILIES__ROOT__BURST"] = "100000"
    # No failed-MCP reprobe-interval override: the reprobe probes OFF the reload gate (only the
    # brief snapshot and apply take the gate, never the network probe), so the never-recovering
    # stub MCPs the secret-ref / mcp specs seed no longer stall reload-gated writes — the default
    # reprobe interval is safe under the browser run and needs no deferral.
    # The github webhook verifier reads its secret from this env var; a bound-but-unsigned
    # delivery then fails verification with a clean 401 rather than a 500.
    if res.gh_webhook_secret is not None:
        env["E2E_GH_WEBHOOK_SECRET"] = res.gh_webhook_secret
    # ask_user mints its callback ticket against the stack's OWN app origin, filled at boot
    # (app_origin_env_keys below): the web channel's answer door forwards the widget's answer
    # to that callback URL, so it must resolve back on this single-port stack, not an
    # off-host placeholder.
    env.update(_memory_agent_state_env())
    if res.llm_base_url is not None:
        env["LLM_BASE_URL"] = res.llm_base_url
        env["LLM_API_KEY"] = "e2e-test"
        env["LLM_MODEL"] = "e2e-scripted"
    # The connectors surface: the connector store binds to the ``default`` database
    # (skeleton component) _base_env already declares, so it is live (store-configured);
    # its Redis cache rides the shared ``_redis_feature_env``. Wire the fixture provider's
    # crypto keys + stub-IdP client credentials when the runner supplied them — mirroring
    # build_connectors_stack. The fixture OAuth endpoints are stamped into the manifest
    # ``connectors`` descriptor above.
    if res.connectors_kek is not None:
        env["CONNECTORS_KEK"] = res.connectors_kek
    if res.connectors_state_hmac_key is not None:
        env["CONNECTORS_STATE_HMAC_KEY"] = res.connectors_state_hmac_key
    if res.idp_base_url is not None:
        env["E2E_IDP_CLIENT_ID"] = "e2e-client"
        env["E2E_IDP_CLIENT_SECRET"] = "e2e-secret"
    # Optional marketplace wiring for the browser leg. The marketplace router is always
    # mounted under ``"all"``, so this block only points it at the harness-run registry;
    # with marketplace_url unset the router still answers non-404 but has no registry to
    # browse (the marketplace specs gate themselves on TAI_E2E_MARKETPLACE).
    if res.marketplace_url is not None:
        env["MARKETPLACE_URL"] = res.marketplace_url
        env["MARKETPLACE_ADVISORIES_POLL"] = "true"
        env["MARKETPLACE_ADVISORIES_INTERVAL_S"] = "1"
        # The attribution store binds to the ``default`` database (skeleton component)
        # _base_env already declares — the stack's own PG clone.
        if res.package_index_url is not None:
            env["PIP_INDEX_URL"] = f"{res.package_index_url}/simple/"  # pip's PEP 503 root on the fixture server
    return StackConfig(
        name="studio",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=2,
        run_backend=True,
        run_metrics=True,
        auth=True,
        # The OAuth connect flow signs the deployment origin and validates it against
        # CONNECTORS_REDIRECT_URI_ALLOWLIST fail-closed; the app port is only known at
        # boot, so the stack fills this with its own origin.
        origin_allowlist_env_keys=["CONNECTORS_REDIRECT_URI_ALLOWLIST"],
        # The ask_user callback base the web channel's answer door forwards to must be this
        # stack's own reachable origin (single app port, known only at boot).
        app_origin_env_keys=["INTERACTIONS_PUBLIC_BASE_URL"],
    )


def build_studio_setup_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The built Studio over an UNSEEDED accounts-enabled deployment — the fresh install
    the setup door serves, for the login spec's ``needs_setup=true`` flow.

    Serves the real Studio dist through the whole default router set (``"all"``) — the same
    ``/api`` surface the shell drives — with the Postgres accounts provider (password login +
    ``needs_setup`` observability) beside the redis key provider (the key-paste fallback), the
    setup door behind the pinned ``TAI_SETUP_TOKEN``, and the two-tier route map the ``studio``
    resource resolves through.

    BUSLESS by construction so it boots with NO credential: an owner cannot be seeded without
    flipping ``needs_setup`` false, so the authed MCP readiness drain — which needs an admin
    key — can never run here. One worker, no task backend, no metrics keeps ``needs_bus`` false,
    so readiness is HTTP ``/health`` alone (as ``build_accounts_fresh_stack``). It carries no
    backend, agents, channels, connectors, or backend-branch probe tools — the login flow runs
    none of them, and each would demand the bus the busless stack does not run. The runner seeds
    only the route table (no owner, no key); the setup door initializes the deployment live, and
    the same stack serves the post-setup ``needs_setup=false`` sign-in."""
    if res.studio_dist_path is None:
        raise RuntimeError("build_studio_setup_stack requires resources.studio_dist_path (the built Studio dist)")
    manifest = {
        "default_routers": "all",
        "lifecycle_modules": [variants.identity.lifecycle_module, "tai42_accounts_postgres"],
        # The "all" set already mounts every core + feature router (incl. the SPA catch-all);
        # the accounts plugin's own login + users routes are the only extras.
        "routers_modules": [
            "tai42_accounts_postgres.routes_login",
            "tai42_accounts_postgres.routes_users",
        ],
        "extensions_modules": _EXTENSION_MODULES,
        "storage_module": variants.storage.module,
        "tools": [*_builtin_entries()],
        "studio_plugins": ["tai42_accounts_postgres"],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "notify_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env["ACCESS_CONTROL_ENABLE"] = "true"
    env["ACCESS_CONTROL_AUTH_PROVIDERS"] = json.dumps(["accounts-postgres", variants.identity.name])
    env["TAI_SETUP_TOKEN"] = _SETUP_TOKEN
    env["ACCESS_CONTROL_PATH_PATTERNS"] = json.dumps(STUDIO_PATH_PATTERNS)
    env["STUDIO_DIST_PATH"] = res.studio_dist_path
    # Lift the ``root`` rate-limit family for the browser leg (every SPA asset charges it), as
    # ``build_studio_stack`` does: the serial suite's page loads burst past the default ceiling.
    env["TAI_RATE_LIMIT_FAMILIES__ROOT__LIMIT"] = "100000"
    env["TAI_RATE_LIMIT_FAMILIES__ROOT__BURST"] = "100000"
    return StackConfig(
        name="studio-setup",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=True,
    )
