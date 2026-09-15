"""Shared seams and wiring for the marketplace installer flows.

:class:`_InstallerBase` holds the constructor that wires the registry, store, config,
pip and prefix collaborators (each defaulting live, an injected value winning) and the
infrastructure seams the install/uninstall/update flows all reach: the live config
read (:meth:`_cm`) and mutation (:meth:`_svc`) pipelines — fetched FRESH per use because
a reload swaps app internals — the unwind seam bundles, and the opt-in plugin-migration
step. The concrete flow classes inherit this base; :class:`~tai42_skeleton.marketplace.installer.Installer`
composes them into the public installer.
"""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.plugins import PluginSpec
from tai42_kit.db import apply_migrations

from tai42_skeleton.config.service import ConfigService
from tai42_skeleton.db import plugin_migration_entry
from tai42_skeleton.marketplace import locks, route_preflight
from tai42_skeleton.marketplace import routes as routes_mod
from tai42_skeleton.marketplace.client import RegistryClient
from tai42_skeleton.marketplace.pip import PipRunner, run_pip
from tai42_skeleton.marketplace.prefix import (
    configured_prefix,
    ensure_prefix_writable,
    environment_distribution_version,
    prefix_has_distribution,
    uninstall_from_prefix,
)
from tai42_skeleton.marketplace.store import MarketplaceInstallStore

logger = logging.getLogger(__name__)


class _InstallerBase:
    """Constructor wiring and the config/venv seams every installer flow shares."""

    def __init__(
        self,
        *,
        registry: RegistryClient | None = None,
        pip_runner: PipRunner = run_pip,
        store: MarketplaceInstallStore | None = None,
        config_service: ConfigService | None = None,
        fleet_lock: Callable[[], AbstractAsyncContextManager[None]] = locks.fleet_lock,
        config_manager: Any | None = None,
        prefix: str | None = None,
        prefix_uninstall: Callable[[str, str], None] = uninstall_from_prefix,
        prefix_ensure_writable: Callable[[str], None] = ensure_prefix_writable,
        prefix_has_dist: Callable[[str, str], bool] = prefix_has_distribution,
        env_dist_version: Callable[[str, str], str | None] = environment_distribution_version,
        owned_routes: Callable[[], list[routes_mod.OwnedRoute]] = route_preflight.live_owned_routes,
        reserved_prefixes: Callable[[], Sequence[str]] = route_preflight.live_reserved_prefixes,
    ) -> None:
        """Wire the registry, store, config, pip and prefix seams; each defaults live, an injected value wins."""
        self._registry = registry or RegistryClient()
        self._pip_runner = pip_runner
        self._store = store or MarketplaceInstallStore()
        self._config_service = config_service
        self._fleet_lock = fleet_lock
        self._config_manager = config_manager
        # ``None`` means read the configured prefix (env). Injected non-None wins so
        # a test can drive prefix behavior against a temp dir without env.
        self._prefix = configured_prefix() if prefix is None else prefix
        self._prefix_uninstall = prefix_uninstall
        self._prefix_ensure_writable = prefix_ensure_writable
        self._prefix_has_dist = prefix_has_dist
        self._env_dist_version = env_dist_version
        self._owned_routes = owned_routes
        self._reserved_prefixes = reserved_prefixes

    # -- infrastructure -----------------------------------------------------

    def _cm(self) -> Any:
        """The live config manager (read seam), or the injected fake.

        Reached live per use because a reload swaps app internals; only for pre-flight manifest
        READS — every WRITE crosses :meth:`_svc`.
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

    def _unwind_seams(self) -> dict[str, Any]:
        """The venv/config seams an unwind needs.

        A FRESH ``svc`` + ``cm`` (a prior reload may have swapped app internals) and the
        pip/prefix handles.
        """
        return {
            "svc": self._svc(),
            "cm": self._cm(),
            "pip_runner": self._pip_runner,
            "prefix": self._prefix,
            "prefix_uninstall": self._prefix_uninstall,
        }

    def _install_unwind_seams(self) -> dict[str, Any]:
        """The install unwind's seams.

        :meth:`_unwind_seams` plus the prefix-removal probes ``unwind_install`` uses to pip
        uninstall the just-installed package.
        """
        return {
            **self._unwind_seams(),
            "prefix_has_dist": self._prefix_has_dist,
            "env_dist_version": self._env_dist_version,
        }

    async def _run_plugin_migrations(self, spec: PluginSpec) -> None:
        """Apply the installed/upgraded plugin's schema migrations, when it declares a chain.

        Opt-in: no ``migrations`` field is skipped.

        Runs on a migration advisory lock DISTINCT from the marketplace lock this
        operation holds, so the nested lock never self-deadlocks. Called AFTER the
        package lands and BEFORE the manifest patch, so a failure aborts with the
        manifest untouched; applied files roll forward. The import machinery is
        refreshed first so the freshly installed files resolve in this process.
        """
        if spec.migrations is None:
            return
        importlib.invalidate_caches()
        entry = plugin_migration_entry(spec)
        if entry is None:
            return
        applied = await apply_migrations([entry])
        for item in applied:
            logger.info("marketplace: applied migration %s %04d_%s", item.component, item.version, item.name)
