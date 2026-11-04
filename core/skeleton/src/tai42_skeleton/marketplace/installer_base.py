"""Shared seams and wiring for the marketplace installer flows.

:class:`_InstallerBase` holds the constructor that wires the registry, store, config,
pip and plugins-directory collaborators (each defaulting live, an injected value winning)
and the infrastructure seams the install/uninstall/update flows all reach: the live config
read (:meth:`_cm`) and mutation (:meth:`_svc`) pipelines — fetched FRESH per use because
a reload swaps app internals — the build of the plugin environments an operation
changes, the unwind, and the opt-in plugin-migration step. The concrete flow classes
inherit this base; :class:`~tai42_skeleton.marketplace.installer.Installer` composes them
into the public installer.
"""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.plugins import PluginSpec
from tai42_kit.db import AppliedMigration, MigrationDiscoveryError, MigrationError, apply_migrations

from tai42_skeleton.config.service import ConfigService
from tai42_skeleton.db import installed_plugin_entry
from tai42_skeleton.marketplace import env_apply, locks, manifest_apply, route_preflight, unwind
from tai42_skeleton.marketplace import routes as routes_mod
from tai42_skeleton.marketplace.client import RegistryClient
from tai42_skeleton.marketplace.environments.change import OperationChange, prepare_change
from tai42_skeleton.marketplace.environments.settings import configured_plugins_dir, require_plugins_dir
from tai42_skeleton.marketplace.environments.targets import PlacedPin, place_row, targets_of
from tai42_skeleton.marketplace.errors import PluginMigrationError
from tai42_skeleton.marketplace.manifest_patch import ProvidesUndo
from tai42_skeleton.marketplace.pip import PipRunner, run_pip
from tai42_skeleton.marketplace.store import MarketplaceInstallStore

logger = logging.getLogger(__name__)


class _InstallerBase:
    """Constructor wiring and the config and plugins-directory seams every installer flow shares."""

    def __init__(
        self,
        *,
        registry: RegistryClient | None = None,
        pip_runner: PipRunner = run_pip,
        store: MarketplaceInstallStore | None = None,
        config_service: ConfigService | None = None,
        fleet_lock: Callable[[], AbstractAsyncContextManager[locks.FleetHold]] = locks.fleet_lock,
        config_manager: Any | None = None,
        plugins_dir: Callable[[], Path | None] = configured_plugins_dir,
        owned_routes: Callable[[], list[routes_mod.OwnedRoute]] = route_preflight.live_owned_routes,
        reserved_prefixes: Callable[[], Sequence[str]] = route_preflight.live_reserved_prefixes,
        converge: manifest_apply.Convergence = manifest_apply.converge_fleet,
    ) -> None:
        """Wire the registry, store, config, pip and plugins-directory seams; each defaults live, injected wins.

        ``converge`` is how a deregistered manifest converges for the door this installer
        serves: the fleet now (the default), or the deployment's next start.
        """
        self._registry = registry or RegistryClient()
        self._pip_runner = pip_runner
        self._store = store or MarketplaceInstallStore()
        self._config_service = config_service
        self._fleet_lock = fleet_lock
        self._config_manager = config_manager
        self._plugins_dir = plugins_dir
        self._owned_routes = owned_routes
        self._reserved_prefixes = reserved_prefixes
        self._converge = converge

    # -- infrastructure -----------------------------------------------------

    def _cm(self) -> Any:
        """The live config manager (read seam), or the injected fake.

        Reached live per use because a reload swaps app internals; only for stored-env READS —
        a manifest preview reads :meth:`ConfigService.read_manifest_document` and every WRITE
        crosses :meth:`_svc`.
        """
        if self._config_manager is not None:
            return self._config_manager
        return tai42_app.config.config_manager

    def _svc(self) -> ConfigService:
        """The config-mutation pipeline, or the injected fake.

        Wired live per use (:meth:`ConfigService.from_app`) because a reload swaps app internals;
        every manifest write — forward and unwind — crosses it.
        """
        if self._config_service is not None:
            return self._config_service
        return ConfigService.from_app()

    def _require_plugins_dir(self) -> Path:
        """The plugins directory a packaged operation builds in; unset refuses (:func:`require_plugins_dir`)."""
        return require_plugins_dir(self._plugins_dir())

    async def _prepare_environment(
        self, ref: str, old: PlacedPin | None, new: PlacedPin | None, *, subject: str
    ) -> OperationChange:
        """Build the environments an operation moving ``ref`` from pin ``old`` to pin ``new`` changes.

        ``old`` is ``None`` for an install, ``new`` for an uninstall. The other rows keep their
        pins and ``new`` joins them (:func:`prepare_change`). The builds are made beside the
        environments in use; the caller swaps them in.
        """
        plugins_dir = self._require_plugins_dir()
        others = [place_row(row) for row in await self._store.list_installed() if row.ref != ref]
        targets = targets_of(others if new is None else [*others, new])
        return await prepare_change(plugins_dir, targets, old, new, pip_runner=self._pip_runner, subject=subject)

    async def _unwind(
        self,
        step_error: BaseException,
        *,
        change: OperationChange | None,
        provides_undo: ProvidesUndo | None,
        env_written: env_apply.EnvWrite | None,
    ) -> str | None:
        """Reverse the operation's environment swap, manifest patch and env write (:func:`unwind.unwind_operation`).

        A FRESH ``svc`` + ``cm`` are reached, since a prior reload may have swapped app internals.
        """
        return await unwind.unwind_operation(
            step_error,
            change=change,
            provides_undo=provides_undo,
            env_written=env_written,
            svc=self._svc(),
            cm=self._cm(),
        )

    async def _run_plugin_migrations(self, spec: PluginSpec) -> None:
        """Apply the installed/upgraded plugin's schema migrations, when it declares a chain.

        Opt-in: no ``migrations`` field is skipped.

        The chain is resolved from the installed distribution's packaged spec, never by
        importing the plugin (:func:`~tai42_skeleton.db.installed_plugin_entry`), so the folder
        applied here is the one ``tai db migrate`` applies and the boot-time schema gate
        checks. Runs on a migration advisory lock DISTINCT from the marketplace lock this
        operation holds, so the nested lock never self-deadlocks. Called AFTER the
        new environment is current and BEFORE the manifest patch, so a failure swaps the
        environment back with the manifest untouched; applied files roll forward. The import
        machinery is refreshed first so the freshly built files resolve in this process. A
        chain the runner refuses or fails raises :class:`PluginMigrationError` carrying the
        runner's text.
        """
        if spec.migrations is None:
            return
        try:
            applied = await self._apply_plugin_chain(spec)
        except MigrationError as refused:
            raise PluginMigrationError(
                f"{spec.ref}: its schema migrations could not be applied: {refused}"
            ) from refused
        for item in applied:
            logger.info("marketplace: applied migration %s %04d_%s", item.component, item.version, item.name)

    @staticmethod
    async def _apply_plugin_chain(spec: PluginSpec) -> list[AppliedMigration]:
        """Resolve ``spec``'s chain from its installed distribution and apply it; none resolved applies nothing."""
        if spec.package is None:
            raise MigrationDiscoveryError(f"plugin {spec.name!r} declares migrations but no package")
        importlib.invalidate_caches()
        entry = installed_plugin_entry(spec.package)
        if entry is None:
            return []
        return await apply_migrations([entry])
