"""Import the manifest's plugin modules into the serving surface under the compat gate."""

import logging

from tai42_contract.app import tai42_app

from tai42_skeleton.app import lifecycle as _lifecycle
from tai42_skeleton.app.lifecycle.off_loop import run_blocking
from tai42_skeleton.app.lifecycle.state import LifecycleState
from tai42_skeleton.app.mount_map import bind_module
from tai42_skeleton.exceptions.exceptions import TaiValidationError
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
        from tai42_skeleton.plugins.quarantine import reset_quarantine

        activate_prefix()

        # This pass owns the plugin-quarantine generation: reset here, then every
        # additive-module import below (and the Studio-plugin registry rebuild
        # handler that runs after start()) repopulates it. One package→dist
        # snapshot serves every compat verdict of the pass.
        reset_quarantine()
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
        # ``_import_additive_plugin``). Reserved-prefix violations fail the build here.
        self._mount_map = self._build_mount_map()

        # Register the manifest's declarative connector providers before importing
        # any manifest module. A connector is pure data (no import path), so it is
        # registered here during boot/reload through the module-global app handle's
        # ``connectors`` facet — the same ``AppConnectors.register_connector`` seam
        # any holder of the handle uses. ``start()`` cleared the write-target
        # generation above, so every (re)load re-registers from the current manifest.
        # A duplicate id across entries is a boot/epoch-build failure: the registry's
        # duplicate guard raises here rather than quarantining and continuing.
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

        # Identity/accounts providers register only on lifecycle-module import, so
        # their registries are settled here — abort now if a configured provider
        # quarantined instead of quarantine-and-continuing into an unauthable boot.
        self._abort_if_auth_provider_quarantined()

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

        # The scalar slots ABORT boot on incompat/import failure instead of
        # quarantining: the server cannot run without its backend/sandbox/storage/
        # monitoring, so a skipped slot would be a silently crippled server.
        self._import_core_slots(dist_map, executed_modules)

        self._import_extension_modules(dist_map, executed_modules)

        quarantined_tool_modules = self._import_tool_modules(dist_map, executed_modules)

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

        self._validate_tool_registry(quarantined_tool_modules)

    def _import_additive_role(
        self, modules: list[str] | None, kind: str, dist_map: dict[str, list[str]], executed: set[str]
    ) -> set[str]:
        """Import one additive role group, returning the set of modules that quarantined.

        The group is one of lifecycle/webhook_verifier/channel/router/middleware/agents. Each import runs
        the module's registration side-effect; the registries were reset at the top of start(), so every
        (re)load re-registers cleanly.
        """
        quarantined: set[str] = set()
        for module in modules or []:
            if not self._import_additive_plugin(module, kind, dist_map, executed):
                quarantined.add(module)
        return quarantined

    def _import_core_slots(self, dist_map: dict[str, list[str]], executed: set[str]) -> None:
        """Import the four scalar-slot modules (backend/sandbox/storage/monitoring).

        Each aborts boot on incompat or import failure rather than quarantining — the server cannot run
        without its scalar slots.
        """
        manifest = self._require_live_manifest()
        self._import_core_plugin(manifest.backend_module, "backend_module", dist_map, executed)
        self._import_core_plugin(manifest.sandbox_module, "sandbox_module", dist_map, executed)
        self._import_core_plugin(manifest.storage_module, "storage_module", dist_map, executed)
        self._import_core_plugin(manifest.monitoring_module, "monitoring_module", dist_map, executed)

    def _import_extension_modules(self, dist_map: dict[str, list[str]], executed: set[str]) -> set[str]:
        """Import the extensions modules, returning the set of quarantined extensions modules.

        Runs the quarantine-aware extension validation after the imports.
        """
        manifest = self._require_live_manifest()
        quarantined_extension_modules: set[str] = set()
        for extension in manifest.extensions_modules or []:
            if not self._import_additive_plugin(extension, "extensions", dist_map, executed):
                quarantined_extension_modules.add(extension)
        try:
            self._extension_registry.validation()
        except TaiValidationError:
            # A quarantined extensions module cannot say WHICH extension names it
            # would have registered, so its missing extensions are indistinguishable
            # from the quarantine's own footprint — attributed to it loudly here
            # instead of aborting the boot the quarantine just saved. With no
            # quarantined extensions module the failure is genuine manifest
            # misconfiguration and stays a loud abort.
            if not quarantined_extension_modules:
                raise
            logger.error(
                "extension validation failed with quarantined extensions module(s) %s; "
                "continuing — tools using their extensions fail at bind/call time",
                sorted(quarantined_extension_modules),
                exc_info=True,
            )
        return quarantined_extension_modules

    def _import_tool_modules(self, dist_map: dict[str, list[str]], executed: set[str]) -> set[str]:
        """Import the tools modules, returning the set of quarantined tool modules.

        Their included tool names join the validation ignore set.
        """
        manifest = self._require_live_manifest()
        quarantined_tool_modules: set[str] = set()
        for cfg in manifest.tools:
            if not self._import_additive_plugin(cfg.module, "tools", dist_map, executed):
                quarantined_tool_modules.add(cfg.module)
        return quarantined_tool_modules

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

            for cfg, kind in failures:
                self._record_failed_mcp(cfg, kind)

        mcp_health.retain({cfg.title for cfg in manifest.mcp or []})

    def _validate_tool_registry(self, quarantined_tool_modules: set[str]) -> None:
        """Assemble the tool-validation ignore set and run ``_tool_registry.validation``.

        A quarantined tools module's included tool names are legitimately absent
        (the module never imported), so they join the failed-MCP ignore set —
        otherwise the validation would abort the very boot the quarantine saved.
        """
        manifest = self._require_live_manifest()
        ignore = set(self._missing_tools_ignore())
        for module in quarantined_tool_modules:
            ignore |= manifest.include_module_tools_map.get(module, frozenset())
        self._tool_registry.validation(ignore=frozenset(ignore))

    def _import_additive_plugin(
        self, module: str, kind: str, dist_map: dict[str, list[str]], executed_modules: set[str]
    ) -> bool:
        """Import one ADDITIVE manifest module under the plugin-compat gate.

        An incompatible module is never imported (importing it is exactly what
        crash-loops or misbehaves), and ANY exception its import raises — not
        only ImportError; contract drift surfaces as AttributeError/TypeError
        just as readily — quarantines the module instead of aborting boot. Both
        paths record a quarantine entry (one loud log line each) and return
        ``False`` so the caller can account for the module's absent
        contributions; ``True`` means the module imported. An unknown verdict
        (no dist mapping / no declared range) proceeds with a logged note,
        never a silent pass. Imports are function-local to keep the app import
        chain free of the marketplace package.

        ``executed_modules`` is the pass's ledger of already-run module bodies: a
        module a prior role loop's package walk already executed under its own
        binding (the accounts-postgres shape — a root package under a non-route role
        whose route submodules are also their own router entries) is not re-imported
        here, so each module body runs EXACTLY once per pass. On success the modules
        this import ran are added to the ledger.
        """
        from tai42_skeleton.app.http import plugin_owner
        from tai42_skeleton.marketplace.compat import module_compat
        from tai42_skeleton.plugins.quarantine import quarantine_plugin

        if module in executed_modules:
            return True
        verdict = module_compat(module, dist_map)
        if verdict.status == "incompatible":
            quarantine_plugin(module, f"{kind} module not loaded: {verdict.reason}")
            return False
        if verdict.status == "unknown":
            logger.info("plugin compat unknown for %s module %s: %s", kind, module, verdict.reason)
        binding = self._mount_map.get(module)
        # Savepoint the FastMCP route table before a BOUND module imports, so a failure
        # can roll back exactly the routes it committed. A bindingless (core/operator)
        # module records no owner-isolable rows, so it takes no savepoint/rollback — but a
        # route submodule this walk sweeps in under ITS OWN binding is guarded per-module
        # by the importer through the savepoint/rollback handles passed below.
        savepoint = self._http_surface.route_table_savepoint() if binding is not None else None
        reloaded: list[str] = []
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
            # route-sibling that only the extras pop.
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
        except Exception as exc:
            if binding is not None and savepoint is not None:
                # Roll back so a quarantined declared-route module serves NOTHING: the
                # rows it committed before a mid-import custom_route raise, or before a
                # post-import _verify_all_registered raise, must leave no trace in the
                # shape index, _routes, or the FastMCP route table.
                self._http_surface.rollback_module_routes(binding, savepoint)
            logger.exception("%s module %s failed to import; quarantining it", kind, module)
            quarantine_plugin(module, f"{kind} module failed to import: {exc}")
            return False
        executed_modules.update(reloaded)
        return True

    def _import_core_plugin(
        self, module: str | None, slot: str, dist_map: dict[str, list[str]], executed_modules: set[str]
    ) -> None:
        """Import one SCALAR-slot module (backend/storage/monitoring), aborting boot on incompat or import failure.

        Aborts with the typed :class:`CorePluginBootError` — the server cannot run without its scalar slots,
        so a quarantine-and-continue would be a silently crippled server. ``None`` (slot unset) is a no-op.
        The error names the plugin, the versions in play (via the compat reason), and the remedy.

        ``executed_modules`` is the pass's run-once ledger: a slot module a prior
        role loop's walk already executed is not re-imported, and the modules this
        import runs join the ledger.
        """
        if not module:
            return
        if module in executed_modules:
            return
        from tai42_skeleton.marketplace.compat import CorePluginBootError, module_compat

        verdict = module_compat(module, dist_map)
        if verdict.status == "incompatible":
            raise CorePluginBootError(
                f"{slot} plugin {module!r} cannot boot: {verdict.reason}; the server cannot run without its {slot}"
            )
        if verdict.status == "unknown":
            logger.info("plugin compat unknown for %s %s: %s", slot, module, verdict.reason)
        try:
            reloaded = _lifecycle.import_or_reload_package(
                module,
                mount_map=self._mount_map,
                route_savepoint=self._http_surface.route_table_savepoint,
                route_rollback=self._http_surface.rollback_module_routes,
            )
        except Exception as exc:
            raise CorePluginBootError(
                f"{slot} plugin {module!r} failed to import: {exc}; the server cannot run without its {slot} — "
                "fix or update the plugin, or point the manifest at a working one"
            ) from exc
        executed_modules.update(reloaded)

    def _abort_if_auth_provider_quarantined(self) -> None:
        """Abort boot when a configured auth provider quarantined — the auth-slot twin of the scalar-slot abort.

        Identity/accounts providers are ADDITIVE, so a broken one quarantines rather
        than aborting; but a quarantined auth provider leaves the server BOOTED yet
        unauthable, and the quarantine's own report (marketplace listing / Studio)
        sits behind the very auth that is gone — so the additive loud-failure premise
        collapses for the one kind whose failure hides its own report. An accounts
        provider registers into the identity registry too, so every configured
        provider resolves through ``auth_providers``.

        The gate fires when BOTH hold this boot: a configured provider did not
        register AND some lifecycle module quarantined. It CANNOT prove the
        quarantine caused the missing provider — an operator typo in a provider
        name plus an unrelated quarantine trips the same predicate — so the error
        enumerates the two facts SEPARATELY (the unresolved provider names; the
        quarantined lifecycle modules with reasons) with no causal claim, and a
        remedy covering both. With the gate off, no unresolved provider, or no
        lifecycle quarantine, boot is untouched; a misconfigured provider name
        with nothing quarantined is left to the identity-provider startup probe.
        """
        manifest = self._require_live_manifest()
        from tai42_contract.access_control.registry import get_identity_provider_factory_staged

        from tai42_skeleton.access_control.settings import access_control_settings
        from tai42_skeleton.marketplace.compat import CorePluginBootError
        from tai42_skeleton.plugins.quarantine import quarantined_plugins_staged

        settings = access_control_settings()
        if not settings.enable:
            return

        def _registered(name: str) -> bool:
            # Read the STAGED generation: this build's own decision keys on what THIS
            # build imported, not the live epoch's registry.
            try:
                get_identity_provider_factory_staged(name)
            except KeyError:
                return False
            else:
                return True

        unresolved = [name for name in settings.auth_providers if not _registered(name)]
        lifecycle_modules = set(manifest.lifecycle_modules or [])
        quarantined = {
            module: reason for module, reason in quarantined_plugins_staged().items() if module in lifecycle_modules
        }
        if unresolved and quarantined:
            detail = "; ".join(f"{module} ({reason})" for module, reason in sorted(quarantined.items()))
            raise CorePluginBootError(
                f"access control is enabled but configured auth provider(s) {sorted(unresolved)} did not register, "
                f"and lifecycle module(s) quarantined this boot: {detail}; the server would boot unauthable with any "
                "quarantine surfaced only behind the missing auth — fix the provider name(s) if misspelled, and/or "
                "resolve the quarantined plugin(s), or point the manifest at a working provider"
            )
