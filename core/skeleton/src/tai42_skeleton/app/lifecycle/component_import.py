"""Import the manifest's plugin modules into the serving surface under the compat gate."""

import logging

from tai42_contract.app import tai42_app

from tai42_skeleton.app import lifecycle as _lifecycle
from tai42_skeleton.app.lifecycle.off_loop import run_blocking
from tai42_skeleton.app.lifecycle.state import LifecycleState
from tai42_skeleton.app.mount_map import bind_module
from tai42_skeleton.operations.projection import project_operations
from tai42_skeleton.tools import mcp_health

logger = logging.getLogger(__name__)


class ComponentImportMixin(LifecycleState):
    """Lifecycle mixin that imports the manifest's plugin modules and runs their registration side-effects."""

    def _initialize_components(self):
        manifest = self._require_live_manifest()

        # Put the configured plugin prefix on sys.path BEFORE any manifest module
        # imports below, so a plugin whose distribution lives in the persistent
        # prefix imports at boot after a restart. Appended at the END so the
        # environment shadows the prefix for anything present in both; a no-op when
        # no prefix is configured. Imported here (not at module scope) to keep the
        # app import chain free of the marketplace package.
        from tai42_skeleton.marketplace.compat import distribution_map
        from tai42_skeleton.marketplace.prefix import activate_prefix

        activate_prefix()

        # One package→dist snapshot serves every compat verdict of the pass.
        dist_map = distribution_map()

        # Repopulate the operation registry that start() cleared at its top. A leaf
        # module declares its operations with @operation at IMPORT, and that
        # decorator fires exactly once per interpreter; a plain re-import of a router
        # that only ``from operations.<domain> import <op>`` never re-registers a
        # leaf that stayed cached in sys.modules, so without this the projection
        # below would find an empty registry and project nothing. The first call
        # re-imports the leaves to fire each @operation into the registry (never the
        # operations package or its infra, so the registry singleton the projection
        # and authz hold is preserved) and snapshots the records; a reload replays that
        # stable in-memory snapshot instead of re-importing, so the reload adds no
        # sys.modules churn on the loop-affine path. Runs BEFORE the routers below so
        # each route re-attaches its template + method to the metadata record already IN
        # the registry (the record the projection reads and the tool-edge authorization
        # synthesizes its concrete path from).
        #
        # Imported here rather than at module scope: the app import chain pulls the
        # operations package in, so a module-level import of it would be circular.
        from tai42_skeleton.operations import reregister_operations

        reregister_operations()

        # Build the module → mount-binding map BEFORE any manifest module imports, so
        # each declared plugin route resolves its absolute path + public flag from the
        # declaration while its module imports below (bound through
        # ``_import_manifest_module``). Reserved-prefix violations fail the build here.
        self._mount_map = self._build_mount_map()

        # Register the manifest's declarative connector providers before importing
        # any manifest module. A connector is pure data (no import path), so it is
        # registered here during boot/reload through the module-global app handle's
        # ``connectors`` facet — the same ``AppConnectors.register_connector`` seam
        # any holder of the handle uses. ``start()`` cleared the write-target
        # generation above, so every (re)load re-registers from the current manifest.
        # A duplicate id across entries is a boot/epoch-build failure: the registry's
        # duplicate guard raises here rather than continuing.
        for descriptor in manifest.connectors:
            tai42_app.connectors.register_connector(descriptor)

        # The pass's run-once ledger: a package walk under one role can sweep in a
        # module that is ALSO its own manifest entry under another role (a root package
        # listed under a non-route role whose route submodules are their own router
        # entries). The ledger records every module body a role loop runs so a later
        # loop does not run it again — exactly one execution per module per pass. Local,
        # so a fresh epoch build starts with an empty ledger (never leaks across reloads).
        executed_modules: set[str] = set()

        self._import_additive_role(manifest.lifecycle_modules, "lifecycle", dist_map, executed_modules)

        # Import-only verifier plugins: each import runs the module's
        # ``tai42_app.webhook_verifiers.register(...)`` side-effect — the registry
        # was reset at the top of start(), so every (re)load re-registers cleanly.
        self._import_additive_role(manifest.webhook_verifier_modules, "webhook_verifier", dist_map, executed_modules)

        # Import-only channel plugins: each import runs the module's
        # ``tai42_app.channels.register(...)`` side-effect and binds the plugin's
        # inbound HTTP route. Imported like the verifier modules — the registry
        # was reset at the top of start(), so every (re)load re-registers cleanly.
        self._import_additive_role(manifest.channel_modules, "channel", dist_map, executed_modules)

        # Import the composed effective router set (defaults + extras + a single
        # last catch-all, per default_routers). Each import runs the module's
        # @custom_route decorators, registering routes into the FastMCP route table.
        # The epoch build rebuilds the serving ASGI app from that table and the atomic
        # swap installs it, so a router first imported on a live reload serves the
        # instant the swap completes — no restart.
        self._import_additive_role(self._effective_router_modules(), "router", dist_map, executed_modules)

        self._import_additive_role(manifest.middlewares_modules, "middleware", dist_map, executed_modules)

        self._import_core_slots(dist_map, executed_modules)

        self._import_extension_modules(dist_map, executed_modules)

        self._import_tool_modules(dist_map, executed_modules)

        # Importing an agents-module fires its @tai42_app.agents.agent decorator, which
        # registers the agent + auto-generates its run tool. Done after tools so
        # an agent tool can reference a base tool already loaded above.
        self._import_additive_role([cfg.module for cfg in manifest.agents], "agents", dist_map, executed_modules)

        self._load_manifest_mcps()

        # Project the operation surface into MCP tools. Runs AFTER the router
        # modules registered their operations and AFTER base tools/MCPs bound (so
        # extension combos over a projected op resolve at bind time), and BEFORE
        # validation and the preset-rehydration reload handlers re-bake — the
        # pinned order registries/routes -> operations -> projection -> extension
        # wraps -> preset rebakes.
        project_operations(self, manifest.api_tools)

        self._validate_tool_registry()

    def _import_additive_role(
        self, modules: list[str] | None, kind: str, dist_map: dict[str, list[str]], executed: set[str]
    ) -> None:
        """Import one additive role group, aborting boot on the first module that cannot load.

        The group is one of lifecycle/webhook_verifier/channel/router/middleware/agents. Each import runs
        the module's registration side-effect; the registries were reset at the top of start(), so every
        (re)load re-registers cleanly.
        """
        for module in modules or []:
            self._import_manifest_module(module, kind, dist_map, executed)

    def _import_core_slots(self, dist_map: dict[str, list[str]], executed: set[str]) -> None:
        """Import the four scalar-slot modules (backend/sandbox/storage/monitoring).

        Each aborts boot on incompat or import failure through the shared abort seam — the server cannot
        run without its scalar slots.
        """
        manifest = self._require_live_manifest()
        self._import_manifest_module(manifest.backend_module, "backend", dist_map, executed)
        self._import_manifest_module(manifest.sandbox_module, "sandbox", dist_map, executed)
        self._import_manifest_module(manifest.storage_module, "storage", dist_map, executed)
        self._import_manifest_module(manifest.monitoring_module, "monitoring", dist_map, executed)

    def _import_extension_modules(self, dist_map: dict[str, list[str]], executed: set[str]) -> None:
        """Import the extensions modules, then validate the extension registry.

        A module that cannot load aborts boot through the shared abort seam; with every
        declared extensions module imported, a validation failure is genuine manifest
        misconfiguration and stays a loud abort.
        """
        manifest = self._require_live_manifest()
        for extension in manifest.extensions_modules or []:
            self._import_manifest_module(extension, "extensions", dist_map, executed)
        self._extension_registry.validation()

    def _import_tool_modules(self, dist_map: dict[str, list[str]], executed: set[str]) -> None:
        """Import the tools modules, aborting boot on the first module that cannot load."""
        manifest = self._require_live_manifest()
        for cfg in manifest.tools:
            self._import_manifest_module(cfg.module, "tools", dist_map, executed)

    def _load_manifest_mcps(self) -> None:
        """Probe and bind the manifest's MCP servers, recording the failed ones.

        Then prune the passive health store to the CONFIGURED titles.

        Health history follows the manifest: a title dropped from config drops its
        history; surviving titles keep continuity across reloads. Fires on every
        epoch build against the CONFIGURED titles (a failed/unavailable title keeps
        its history), so an empty manifest clears the store.
        """
        manifest = self._require_live_manifest()
        if manifest.mcp:
            successes, failures = run_blocking(self._load_mcps)

            for cfg, tools in successes:
                self._mcp_tools(cfg, tools)

            for cfg, exc in failures:
                self._record_failed_mcp(cfg, exc)

        mcp_health.retain({cfg.title for cfg in manifest.mcp or []})

    def _validate_tool_registry(self) -> None:
        """Assemble the tool-validation ignore set and run ``_tool_registry.validation``.

        The ignore set is the failed-MCP tools whose absence is legitimate (their server
        was unreachable at probe time); every manifest tool module has imported, so a tool
        still missing here is a genuine validation failure.
        """
        self._tool_registry.validation(ignore=frozenset(self._missing_tools_ignore()))

    def _import_manifest_module(
        self, module: str | None, kind: str, dist_map: dict[str, list[str]], executed_modules: set[str]
    ) -> None:
        """Import one manifest-named module under the plugin-compat gate — the shared boot-abort seam.

        Every manifest-declared module — an additive role (lifecycle/webhook_verifier/
        channel/router/middleware/agents/extensions/tools) or a scalar slot
        (backend/sandbox/storage/monitoring) — flows through here. A module judged
        INCOMPATIBLE is never imported (importing it is exactly what crash-loops or
        misbehaves), and an exception its import raises — not only ImportError;
        contract drift surfaces as AttributeError/TypeError just as readily — aborts
        boot with a typed :class:`CorePluginBootError` naming the module, its kind and
        the reason. A manifest names a module the operator chose to load, so a module
        that cannot load is corrupt configuration, not a degradation to serve around.
        The ONE exception left with its own type is
        :class:`CrossOwnerRouteCollisionError`: the module imports fine and the mount
        collision is resolved by remapping the item's base, so the marketplace install
        door and the post-record remount reload act on it — at boot, with no remount to
        follow, it still propagates and aborts boot. An unknown verdict (no dist mapping
        / no declared range) proceeds with a logged note, never a silent pass. Imports
        are function-local to keep the app import chain free of the marketplace package.
        ``None`` (an unset scalar slot) is a no-op.

        ``executed_modules`` is the pass's ledger of already-run module bodies: a
        module a prior role loop's package walk already executed under its own
        binding (the accounts-postgres shape — a root package under a non-route role
        whose route submodules are also their own router entries) is not re-imported
        here, so each module body runs EXACTLY once per pass. On success the modules
        this import ran are added to the ledger.
        """
        if not module:
            return
        from tai42_skeleton.app.http import plugin_owner
        from tai42_skeleton.app.route_registry import CrossOwnerRouteCollisionError
        from tai42_skeleton.marketplace.compat import CorePluginBootError, module_compat

        if module in executed_modules:
            return
        verdict = module_compat(module, dist_map)
        if verdict.status == "incompatible":
            raise CorePluginBootError(
                f"{kind} plugin {module!r} is incompatible and cannot load: {verdict.reason}; "
                "a manifest-declared plugin that cannot load aborts boot — fix or update the plugin, "
                "remove it from the manifest, or point the manifest at a working one"
            )
        if verdict.status == "unknown":
            logger.info("plugin compat unknown for %s module %s: %s", kind, module, verdict.reason)
        binding = self._mount_map.get(module)
        try:
            # A mount-bound module resolves its declared routes through the binding
            # carried on the contextvar for the span of its import; the bind also
            # verifies every declared row registered once the import completes. A bound
            # plugin may register its routes in a SIBLING of the manifest leaf (the leaf
            # imports the sibling for the ``@custom_route`` side-effect); re-importing
            # the leaf alone leaves that sibling cached in ``sys.modules`` so its
            # decorators never re-fire and its routes drop from the rebuilt epoch. Pop
            # those sibling module(s) — the ones the live epoch recorded for THIS owner,
            # never a wider set — so they re-fire under this same binding on reload. A
            # self-registering leaf (its routes declared in the leaf itself) records only
            # itself, so its extra set is empty and no other module is disturbed. Only a
            # route-registering module is re-fired here, so a non-route import side-effect
            # must live in a manifest-listed module to run on reload, never in a
            # route-sibling that only the extras pop. A bindingless (core/scalar-slot)
            # module records no owner-isolable rows, so it needs no extra set.
            extra = (
                _lifecycle.route_registry.owner_route_modules(plugin_owner(binding)) - {module}
                if binding is not None
                else ()
            )
            with bind_module(binding):
                reloaded = _lifecycle.import_or_reload_package(
                    module,
                    extra,
                    mount_map=self._mount_map,
                    route_savepoint=self._http_surface.route_table_savepoint,
                    route_rollback=self._http_surface.rollback_module_routes,
                )
        except CrossOwnerRouteCollisionError:
            # A cross-owner route mount collision is a resolvable DOMAIN condition, not an
            # import/compat failure: the module imports fine, and the remedy is remapping
            # the item's mount base (the marketplace install door surfaces it as a 409 and
            # the post-record remount reload applies the remap). It keeps its own type
            # through this seam so those callers can act on it. At boot — with no remount to
            # follow — it propagates and aborts boot loudly.
            raise
        except Exception as exc:
            raise CorePluginBootError(
                f"{kind} plugin {module!r} failed to import: {exc}; "
                "a manifest-declared plugin that cannot load aborts boot — fix or update the plugin, "
                "remove it from the manifest, or point the manifest at a working one"
            ) from exc
        executed_modules.update(reloaded)
