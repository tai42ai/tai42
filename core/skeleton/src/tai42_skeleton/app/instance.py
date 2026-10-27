"""The process app singleton and its startup/reload handlers."""

import functools
import inspect
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from tai42_kit.db import component_binding, component_store_configured, database_password_env
from tai42_kit.logging import install_record_redaction, logging_settings, setup_logging
from tai42_kit.settings import register_settings_reset

from tai42_skeleton import tool_meta, versioning
from tai42_skeleton.access_control import policy_version
from tai42_skeleton.access_control import settings as access_control_readiness
from tai42_skeleton.access_control.settings import access_control_settings
from tai42_skeleton.access_control.setup_gate import ensure_setup_token
from tai42_skeleton.access_control.startup import (
    check_accounts_providers_configured,
    check_always_public_routes,
    check_fenced_routes_resolvable,
    check_raw_path_routes_resolvable,
    check_route_actions,
    check_route_rows_canonical,
    check_spa_shell_public,
    declare_gate_state_to_kit,
    probe_identity_provider,
    seed_roles,
)
from tai42_skeleton.access_control.verifier import reset_registered_reserved_paths
from tai42_skeleton.app import bus_settings
from tai42_skeleton.app.fastmcp_settings import apply_fastmcp_logging, apply_fastmcp_settings
from tai42_skeleton.app.server import TaiMCP
from tai42_skeleton.connectors import settings as connectors_readiness
from tai42_skeleton.connectors.meta_log_redactor import (
    follow_connector_meta_token_key,
    register_connector_log_redaction,
)
from tai42_skeleton.conversations import settings as conversations_readiness
from tai42_skeleton.conversations.lifecycle import (
    redrive_pending_conversations,
    register_conversation_completion_tools,
    start_conversations_delivery_sweep,
    stop_conversations_delivery_sweep,
)
from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.hooks import settings as hooks_readiness
from tai42_skeleton.interactions import settings as interactions_readiness
from tai42_skeleton.marketplace import settings as marketplace_readiness
from tai42_skeleton.marketplace.advisories import start_advisories_poll, stop_advisories_poll
from tai42_skeleton.middleware.rate_limit import check_rate_limit_families
from tai42_skeleton.plugins.registry import rebuild_studio_plugin_registry
from tai42_skeleton.plugins.settings import check_hosted_ui_settings
from tai42_skeleton.routers import tool_runs_settings
from tai42_skeleton.settings import rate_limit
from tai42_skeleton.states import db as states_readiness
from tai42_skeleton.states.db import assert_states_schema_applied
from tai42_skeleton.sub_mcp import service as sub_mcp_service
from tai42_skeleton.sub_mcp import settings as sub_mcp_readiness

logger = logging.getLogger(__name__)

_app: TaiMCP | None = None


async def _invalidate_policy_cache() -> None:
    """The mount-change listener: drop every cached policy decision and capability projection."""
    await policy_version.bump_policy_version()


def built_app() -> TaiMCP | None:
    """The process app singleton when it is already built, else ``None``; never builds it."""
    return _app


def _declare_platform_readiness_and_drain(app: TaiMCP) -> None:
    """Register each platform subsystem's own readiness contributor and the tool-runs drain budget on ``app``.

    Every subsystem owns the gate that decides whether its backing store is wired; ``/ready``
    reads them through the app's readiness registry. The tool-runs module declares the drain
    budget a retire or recycle waits for its supervisors.
    """
    readiness = app.readiness
    readiness.register("access_control", access_control_readiness.readiness_targets)
    readiness.register("tool_runs", tool_runs_settings.readiness_targets)
    readiness.register("interactions", interactions_readiness.readiness_targets)
    readiness.register("rate_limit", rate_limit.readiness_targets)
    readiness.register("hooks", hooks_readiness.readiness_targets)
    readiness.register("sub_mcp", sub_mcp_readiness.readiness_targets)
    readiness.register("connectors", connectors_readiness.readiness_targets)
    readiness.register("versioning", versioning.readiness_targets)
    readiness.register("marketplace", marketplace_readiness.readiness_targets)
    readiness.register("tool_meta", tool_meta.readiness_targets)
    readiness.register("states", states_readiness.readiness_targets)
    readiness.register("conversations", conversations_readiness.readiness_targets)
    readiness.register("bus", bus_settings.readiness_targets)
    # Constructed fresh on every read, so the budget follows the env current at the call —
    # including a proposed env a config change is validated under.
    app.drain_budgets.register("tool_runs", lambda: tool_runs_settings.ToolRunsSettings().shutdown_drain_seconds)


def connectors_in_use() -> bool:
    """Whether this deployment uses connectors at all.

    Connectors are ON exactly when the skeleton database is configured — every
    skeleton store shares one binding, so the connector token store is live iff that
    database is.
    """
    return component_store_configured(SKELETON_COMPONENT)


async def rehydrate_versioned_presets_if_store_in_use() -> None:
    """Startup/reload handler: re-register every versioned preset from the store.

    Runs only when the versioned-document store is configured.

    ``reload_config()`` (and a cold boot) wipes the runtime tool registry, so this
    reloads the persisted presets from their active bodies — clearing the
    ``PresetManager`` spec map + quarantine set wholesale first, then re-registering
    every preset from its store row.
    A stale preset (foreign name / missing or preset-owned base) is QUARANTINED
    and surfaced as ``conflicted`` rather than raising, so one bad name never
    bricks boot; a genuinely unreachable Postgres still fails the boot/op loudly
    under the handler's ``raise_on_error``. The load is skipped when the skeleton
    database is unconfigured (``component_store_configured(SKELETON_COMPONENT)``),
    so a deployment with no versioned documents never opens a Postgres connection
    at boot.
    """
    if not component_store_configured(SKELETON_COMPONENT):
        logger.info(
            "presets: skipping versioned-preset rehydration — the skeleton database is not configured (%s)",
            database_password_env(component_binding(SKELETON_COMPONENT)),
        )
        return
    await build_app().preset_manager.rehydrate()


async def rehydrate_sub_mcp_apps() -> None:
    """Startup/reload handler: re-materialize every persisted sub-MCP app into this worker's router.

    Reads from the shared store into THIS worker's in-process router.

    ``reset()`` (run on every ``start()``/``reload_config()``) wipes the router's
    per-worker route cache, so this reloads the durable registrations — the store is
    the source of truth and the router is its cache. Each route is bound through the
    router directly (NOT the write service): rehydration must not re-write the store
    it is reading. The store's route list is snapshotted before iterating so the
    in-memory store can never mutate mid-iteration. No env gate (unlike the presets
    handler): the in-memory store needs no connection, and in Redis mode an
    unreachable Redis fails the boot/op loudly under the handler's ``raise_on_error``.
    """
    from tai42_skeleton.sub_mcp.store import get_sub_mcp_store

    router = build_app().sub_app.mcp_sub_app_router
    routes = await get_sub_mcp_store().list_routes()
    for slug, config in list(routes.items()):
        await router.bind_route(slug, config.tools, config.transport)


async def _apply_preset_tool_reload(action: str, name: str) -> None:
    """Worker-bus dispatch target for the ``"preset"`` tool kind: rebind or tear down one preset on this worker.

    ``reload`` re-reads the preset's ACTIVE store body and rebinds it — a missing
    store row raises :class:`~tai42_contract.presets.errors.PresetNotFoundError`, so a
    bogus fan-out fails loudly, and a sibling name conflict propagating out of
    register surfaces as a real fleet inconsistency.

    ``remove`` mirrors the delete route's own conflicted/non-conflicted split so the
    fan-out never touches a foreign tool: a QUARANTINED name was never registered as
    a preset (its name may be owned by a foreign tool), so only its quarantine entry
    is dropped — the foreign tool stays bound; any other name is a live preset whose
    registration is torn down (idempotent for an absent name). Registered in
    :func:`build_app` and invoked through ``TaiMCP._run_tool_reload``, which rejects
    any other action.
    """
    manager = build_app().preset_manager
    if action == "reload":
        await manager.reload(name)
    elif manager.is_quarantined(name):
        manager.drop_quarantine(name)
    else:
        await manager.remove(name)
        manager.drop_quarantine(name)


def apply_logging_settings() -> None:
    """Re-apply the root logger config from the fresh settings.

    The cold-boot env bridge and a config reload both reset the settings caches before their
    handlers run, so ``logging_settings()`` re-reads the current ``TAI_LOG_LEVEL`` and
    ``setup_logging``'s ``force=True`` swaps the root handler config to that level without a
    process restart. Registered only from the CLI entrypoints (via :func:`register_cli_process_state`),
    which own their process's root logger; an embedded app leaves the host's logging untouched.
    """
    setup_logging(logging_settings())


def register_cli_process_state() -> None:
    """Keep a CLI process's process-wide state on the applied env: its root logger and fastmcp's settings.

    Called only from the CLI entrypoints, which own their process. The root logger, configured
    before the bridge applies the stored env, is configured again once the bridge has applied it
    and after every config reload. fastmcp's settings, which fastmcp reads when it is imported,
    are read again once the bridge has applied the stored env
    (:func:`~tai42_skeleton.app.fastmcp_settings.apply_fastmcp_settings`), and fastmcp's logger is
    configured again from them. An embedded app touches neither the host's logging nor the
    host's fastmcp settings. Idempotent: handlers are keyed by their ``module.qualname``, so a
    repeat registration replaces the same entry.
    """
    app = build_app()
    app._on_boot_env_applied(apply_logging_settings)
    app._on_boot_env_applied(apply_fastmcp_settings)
    app._on_boot_env_applied(apply_fastmcp_logging)
    app.lifecycle.on_reload(apply_logging_settings)


def _when_access_control_enabled(handler: Callable[[], Any]) -> Callable[[], Any]:
    """``handler``, run only while the access-control gate is enabled as the epoch builds.

    The gate's setting is read when the handler runs, so the stored env the boot bridge applied
    and every reload decide it, never the env at the app's build. Keeps ``handler``'s name, its
    registry key, and whether it is a coroutine function.
    """
    if inspect.iscoroutinefunction(handler):

        @functools.wraps(handler)
        async def gated_async() -> Any:
            if access_control_settings().enable:
                return await handler()
            return None

        return gated_async

    @functools.wraps(handler)
    def gated() -> Any:
        if access_control_settings().enable:
            return handler()
        return None

    return gated


@asynccontextmanager
async def lifespan(app_):
    """The ASGI lifespan: enter the sub-MCP router's lifespan for the duration of the served process."""
    # Process-wide resource teardown (pooled clients, store/checkpoint pools,
    # monitoring flush) runs on ``app_context`` — the seam both this served path
    # and the backend worker cross — so it is not duplicated here. See
    # ``TaiMCPLifecycleMixin._teardown_resources``.
    app = build_app()
    async with app.sub_app.mcp_sub_app_router.lifespan(app_):
        yield


def _register_feature_lifecycles(app: TaiMCP) -> None:
    """Register the feature lifecycles that run whatever routers ``default_routers`` mounts.

    The conversation completion tools and re-drive run every epoch build, the delivery sweep
    on the serving loop after every swap; the advisory poll starts only where the
    ``marketplace_advisories`` operation is served; the hook-run sweep starts after every swap.
    """
    app.lifecycle.on_startup(register_conversation_completion_tools)
    app.lifecycle.on_startup(redrive_pending_conversations)
    app.lifecycle.on_post_swap(start_conversations_delivery_sweep)
    app.lifecycle.on_shutdown(stop_conversations_delivery_sweep)
    app.lifecycle.on_post_swap(start_advisories_poll)
    app.lifecycle.on_shutdown(stop_advisories_poll)
    # Every process that runs the app lifespan sweeps the kept hook runs, so any serve process
    # or backend worker runs, retries and reclaims them. Its stop is the lifespan's own
    # ordered shutdown step, ahead of the shutdown handlers.
    from tai42_skeleton.hooks.runs.sweep import start_hook_runs_sweep

    app.lifecycle.on_post_swap(start_hook_runs_sweep)


def _register_states_lifecycles(app: TaiMCP) -> None:
    """Register the states component's schema gate, its outbox sweep and its seed reconcile.

    Registered right after the skeleton's schema gate, so neither the sweep nor the reconcile
    ever runs against a pending-migration database.
    """
    # The states component's chain is a second skeleton-owned schema-gate: it
    # defaults to the same database as the skeleton (component_store_configured),
    # so it is verified whenever a database is bound, before any state read/write.
    app.lifecycle.on_startup(assert_states_schema_applied)
    # Every process that runs the app lifespan sweeps the states outbox (pending saves applied
    # after the reply), so any serve process or backend worker recovers any pending save.
    from tai42_skeleton.states.outbox.sweep import start_state_outbox_sweep, stop_state_outbox_sweep

    app.lifecycle.on_post_swap(start_state_outbox_sweep)
    app.lifecycle.on_shutdown(stop_state_outbox_sweep)

    # Reconcile the shipped state-template seeds once the store is live (a no-op while
    # the feature is off). A closure over the persistent app reads the LIVE per-epoch
    # service at call time, so a reload reconciles the freshly-registered seeds rather
    # than a boot-epoch snapshot.
    async def _reconcile_state_template_seeds() -> None:
        await app._states_service.apply_template_seeds()

    app.lifecycle.on_startup(_reconcile_state_template_seeds)


def _register_generation_checks(app: TaiMCP) -> None:
    """Register the checks that run over every generation, at boot and on every reload.

    Every public route of one rate-limit family declares the same budget: checked after the
    routers are re-imported, so a conflicting install fails before it serves. The hosted-UI
    page addresses are read the same way, so a malformed ``HOSTED_UI_*`` value refuses there
    instead of failing the login page or a Connect.
    """
    app.lifecycle.on_startup(check_rate_limit_families)
    app.lifecycle.on_reload(check_rate_limit_families)
    app.lifecycle.on_startup(check_hosted_ui_settings)
    app.lifecycle.on_reload(check_hosted_ui_settings)


def _register_storage_lifecycles(app: TaiMCP) -> None:
    """Register the storage declarations every epoch's fresh registries need.

    The platform's template-delete referees re-arm at boot and on every reload, into the
    epoch's fresh referee registry; consulted only by the delete gates, after boot, so their
    order among the handlers is free. The inbound-media storage marks (not backed up,
    unlisted) are declared into each epoch's fresh mark registry: every startup handler also
    runs at each reload build.
    """
    from tai42_skeleton.conversations.media_meta import declare_media_prefix_marks
    from tai42_skeleton.template.platform_referees import register_platform_template_referees

    app.lifecycle.on_startup(register_platform_template_referees)
    app.lifecycle.on_reload(register_platform_template_referees)
    app.lifecycle.on_startup(declare_media_prefix_marks)


def _install_connector_log_redaction() -> None:
    """Register the connector-secret log rule, keep it on the key each settings reset names, install the redaction."""
    register_connector_log_redaction()
    register_settings_reset(follow_connector_meta_token_key)
    install_record_redaction()


def build_app() -> TaiMCP:
    """Build (once) the process app singleton.

    Construction is deferred out of import time, mirroring ``cli.metrics.create_app``. The
    access-control handlers read the gate's setting as each epoch builds, never at this build,
    so the stored env the boot env bridge applies decides whether they run.

    Idempotent: the first call builds the app singleton; later calls return the
    same app. Provider descriptors come from the code-built registry — each
    manifest ``connectors`` entry is registered via ``register_connector`` during
    boot/reload registration — so the resolver's sync ``get_provider`` needs no
    per-boot Postgres read.

    The root-logger re-apply and fastmcp's settings re-read are NOT wired here:
    they are a CLI-seam concern registered via :func:`register_cli_process_state`,
    so an embedded app that builds this singleton never reconfigures the host's
    root logger or the host's fastmcp settings.
    The connector-secret log rule is registered and the kit's record redaction
    installed here regardless of caller, at its default ``"tai"`` scope: the
    fail-closed redaction covers tai's own log records in embed mode too, while a
    host app's own records pass through untouched. The rule is registered again on
    every settings reset (:func:`follow_connector_meta_token_key`), so after each env
    apply — the boot env bridge, a reload, a discarded reload's restore — it masks the
    connector-meta token key that env names. The CLI entrypoints widen the redaction to
    process scope beside their logging registration.
    """
    global _app
    if _app is None:
        _install_connector_log_redaction()
        # No ``auth=`` here: each epoch's ``ServingCore`` reads the access-control
        # adapter FRESH from settings, so a profile that flips ACCESS_CONTROL_*
        # rebuilds the adapter + verifier chain rather than pinning the boot one.
        app = TaiMCP(name="Tai", lifespan=lifespan)
        _declare_platform_readiness_and_drain(app)
        # The gate state is handed to the kit FIRST in every epoch build, whatever the
        # gate's setting, so any later handler that enqueues a callback job finds it.
        app.lifecycle.on_startup(declare_gate_state_to_kit)
        _register_states_lifecycles(app)
        _register_generation_checks(app)
        # The access-control handlers run only while the gate is enabled, read as each epoch
        # builds: the stored env the boot bridge applied and every reload decide it.

        # The configured identity providers probe their OWN record stores once at
        # startup, so a deployment against a backend a provider cannot use fails
        # LOUDLY at boot instead of on the first authenticated request.
        app.lifecycle.on_startup(_when_access_control_enabled(probe_identity_provider))
        # The control-plane role templates (admin/editor/viewer) are seeded before
        # traffic, so a bootstrap ``apply_role`` can never KeyError on a fresh deploy.
        app.lifecycle.on_startup(_when_access_control_enabled(seed_roles))
        # The route table is read once per epoch build: a row stored in a form no request
        # path reduces to fails the boot loudly, naming the rows to correct or delete.
        app.lifecycle.on_startup(_when_access_control_enabled(check_route_rows_canonical))
        # The setup token is fixed once (SET NX on the shared AC Redis) and logged once
        # by the winner while no principal exists, so a fresh deployment can initialize
        # itself through the public POST /api/setup door. Runs after the providers are
        # probed and roles seeded so the serviceability check can resolve the mint
        # provider and the mint path is ready.
        app.lifecycle.on_startup(_when_access_control_enabled(ensure_setup_token))
        # The pre-auth surface is enumerated (visible at every boot) and an
        # authenticated route on it fails the boot closed.
        app.lifecycle.on_startup(_when_access_control_enabled(check_always_public_routes))
        # The route surface outside the control plane is audited on every method: the
        # derived reserved set is printed, an unacknowledged public-by-declaration route
        # there (any method) fails the boot closed, and the control-plane terminal-deny
        # invariant is confirmed. Also on reload: a reload can add an authed GET route
        # outside the control plane while the HTTP-edge verifier outlives it, so a stale reserved set would
        # serve that route the anonymous shell. The reset must run AFTER the reimport.
        spa_shell_audit = _when_access_control_enabled(check_spa_shell_public)
        app.lifecycle.on_startup(spa_shell_audit)
        app.lifecycle.on_reload(spa_shell_audit)
        app.lifecycle.on_reload(_when_access_control_enabled(reset_registered_reserved_paths))
        # Every gated route must resolve to an authorization action-class
        # (read/write/fenced/secret) — an unclassifiable route fails the boot closed,
        # so allow-by-omission can never reach the enforcement path.
        app.lifecycle.on_startup(_when_access_control_enabled(check_route_actions))
        # Every registered fenced/secret route must resolve back to itself through the
        # gate's resolver, so the admin-only fence can never silently fail open. Also on
        # reload, which can mount a new fenced route: the audit rebuilds the route index
        # against the re-imported surface, so a reload-added fence that resolves
        # elsewhere fails the reload loudly instead of failing open until a restart.
        fenced_route_audit = _when_access_control_enabled(check_fenced_routes_resolvable)
        app.lifecycle.on_startup(fenced_route_audit)
        app.lifecycle.on_reload(fenced_route_audit)
        # Every raw-path-matched route (the record doors) must resolve back to itself
        # under an encoded-slash key, so a missing ``use_raw_path_key`` mark can never
        # silently make an encoded-slash record request read as the public SPA shell.
        # Also on reload: a reload re-imports the routers and re-marks the family.
        raw_path_route_audit = _when_access_control_enabled(check_raw_path_routes_resolvable)
        app.lifecycle.on_startup(raw_path_route_audit)
        app.lifecycle.on_reload(raw_path_route_audit)
        # A registered accounts provider left out of the resolution chain would mint
        # sessions that never authenticate — refuse to boot instead.
        app.lifecycle.on_startup(_when_access_control_enabled(check_accounts_providers_configured))
        # The Studio-plugin registry is a config-derived catalog built at startup
        # AND rebuilt on reload (both with ``raise_on_error``, so a listed package
        # missing its ``studio/`` dist fails the boot/op loudly), so a reload that
        # changes ``studio_plugins`` reflects without a process restart.
        app.lifecycle.on_startup(rebuild_studio_plugin_registry)
        app.lifecycle.on_reload(rebuild_studio_plugin_registry)
        # Versioned presets load at boot AND re-load on every in-place reload
        # (``reload_config()`` wipes the runtime tool registry), so a persisted
        # preset survives a restart and a reload. Gated + ``raise_on_error`` like
        # the connector catalog: the handler skips the Postgres open when the store
        # is unconfigured, and an unreachable Postgres fails the boot/op loudly.
        app.lifecycle.on_startup(rehydrate_versioned_presets_if_store_in_use)
        app.lifecycle.on_reload(rehydrate_versioned_presets_if_store_in_use)
        # Platform-internal rename referees (schedules/hooks/routes/extensions/parks/states) are
        # re-armed every epoch: the referee collection is reset on each start(), and these
        # in-house holders re-register here (the public register_rename_referee seam is for
        # plugins). Consulted only by the rename gate and its preview, after boot, so ordering
        # among the handlers is free.
        from tai42_skeleton.tools.platform_referees import (
            register_platform_detach_referees,
            register_platform_rename_referees,
        )

        app.lifecycle.on_startup(register_platform_rename_referees)
        app.lifecycle.on_reload(register_platform_rename_referees)
        # The platform's own door-binding holders (presets/conversation configs/hooks/
        # schedules) re-arm the template-detach referees the same way: the collection is
        # reset each start(), so these in-house holders re-register here. Consulted only by the
        # detach gate and its preview, after boot, so ordering among the handlers is free.
        app.lifecycle.on_startup(register_platform_detach_referees)
        app.lifecycle.on_reload(register_platform_detach_referees)
        _register_storage_lifecycles(app)
        # The platform's own state-consumer listers (hooks/schedules/agents) re-arm the
        # same way: the consumer-lister registry is reset on each start(), so these
        # platform-owned families re-register here (the public register_consumer_lister
        # seam is for plugins, which register their own consumer kinds). A state bound only by a hook or a
        # schedule is thus shown as a consumer and refused deletion.
        from tai42_skeleton.states.consumers import register_platform_consumer_listers

        app.lifecycle.on_startup(register_platform_consumer_listers)
        app.lifecycle.on_reload(register_platform_consumer_listers)
        # Declared preset seeds are applied AFTER the preset-rehydrate handler (handler
        # dicts run in insertion order): a seed CREATES through the operations-layer
        # internal path, which registers the tool, so ordering it here makes a seeded
        # preset LIVE in the same epoch — resolvable in the tool registry on the very first
        # boot, and before the sub-MCP rehydrate below that may compose one. Local import:
        # operations.presets imports this module, so a top-level import would cycle.
        from tai42_skeleton.operations.presets import apply_preset_seeds

        app.lifecycle.on_startup(apply_preset_seeds)
        app.lifecycle.on_reload(apply_preset_seeds)
        # Durable sub-MCP registrations rehydrate into this worker's router at boot
        # AND on every in-place reload (``reset()`` wipes the per-worker route cache),
        # so a registration survives a restart/reload and a sibling's registration
        # becomes visible on this worker's next reload. Registered AFTER the preset
        # handlers (handler dicts run in insertion order): a sub-MCP app may expose a
        # preset tool, whose registration must exist first. No env gate — the
        # in-memory store needs no connection and Redis mode fails loudly under
        # ``raise_on_error``.
        app.lifecycle.on_startup(rehydrate_sub_mcp_apps)
        app.lifecycle.on_reload(rehydrate_sub_mcp_apps)
        # Registered LAST in the startup order: a re-driven conversation turn may call a
        # preset or a sub-MCP tool rehydrated above.
        _register_feature_lifecycles(app)
        # A mount is a reachable surface, so a mount change must invalidate cached
        # capability projections exactly as a route-table edit does: the sub-MCP write
        # chokepoint notifies this listener after every durable mount change.
        sub_mcp_service.on_mount_changed(_invalidate_policy_cache)
        # Preset create/save/rollback/delete fan out on the worker bus
        # (``reload_tool``/``remove_tool`` with ``kind="preset"``); each subscribed
        # worker dispatches the op through this reloader so the fleet converges.
        app.admin.tool_reloader("preset")(_apply_preset_tool_reload)
        _app = app
    return _app


def __getattr__(name: str):
    # Expose the process app singleton as ``instance.app`` for importers
    # (``from tai42_skeleton.app.instance import app``) while deferring the heavy
    # build + settings read to first access (after the CLI env bootstrap) rather
    # than running it at import.
    if name == "app":
        return build_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
