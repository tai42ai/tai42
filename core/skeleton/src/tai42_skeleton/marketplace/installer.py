"""The marketplace installer — resolve, install, patch, reload, attribute.

:class:`Installer` orders the ``install`` / ``uninstall`` / ``update`` flows (plus
``preview`` and ``upgrade_all``) as abort-on-any-step sequences with an explicit
reverse unwind, delegating each concern to a sibling module (``locks``, ``resolve``,
``package_ops``, ``manifest_apply``, ``env_apply``, ``route_preflight``, ``notes``,
``unwind``). Every collaborator is injected so a test can fake each seam.

Every manifest write crosses :class:`~tai42_skeleton.config.service.ConfigService`,
reached FRESH per use because a reload swaps app internals. Only skeleton state fully
reverts on an unwind; the venv is only as transactional as pip itself (the
pip-transaction caveat rides the response text).
"""

from __future__ import annotations

import copy
import importlib.metadata
import logging
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.plugins import PluginSpec
from tai42_kit.db import apply_migrations

from tai42_skeleton.config.service import ApplyResult, ConfigService
from tai42_skeleton.db import plugin_migration_entry
from tai42_skeleton.marketplace import (
    env_apply,
    locks,
    manifest_apply,
    notes,
    package_ops,
    provides,
    resolve,
    route_preflight,
    unwind,
)
from tai42_skeleton.marketplace import routes as routes_mod
from tai42_skeleton.marketplace.client import RegistryClient
from tai42_skeleton.marketplace.compat import running_contract_version, update_targets
from tai42_skeleton.marketplace.errors import InstallStateError, InstallUnwindError, ManifestCollisionError
from tai42_skeleton.marketplace.manifest_patch import apply_provides, collisions, remove_provides
from tai42_skeleton.marketplace.pip import PipRunner, ensure_pip_available, run_pip
from tai42_skeleton.marketplace.prefix import (
    configured_prefix,
    ensure_prefix_writable,
    environment_distribution_version,
    prefix_has_distribution,
    uninstall_from_prefix,
)
from tai42_skeleton.marketplace.store import InstallRecord, MarketplaceInstallStore
from tai42_skeleton.operations._broadcast import FleetBroadcastError

logger = logging.getLogger(__name__)


class Installer:
    """Install, uninstall, and update marketplace plugins with abort-and-unwind.

    Each public method acquires the per-worker fast-path lock and then the
    fleet-wide advisory lock before touching any state, and holds both across the
    whole operation, so a lock-held-elsewhere refusal makes no store, registry,
    pip, or manifest call.
    """

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
        """The live config manager (read seam), or the injected fake. Reached live per
        use because a reload swaps app internals; only for pre-flight manifest READS —
        every WRITE crosses :meth:`_svc`."""
        if self._config_manager is not None:
            return self._config_manager
        return tai42_app.config.config_manager

    def _svc(self) -> ConfigService:
        """The config-mutation pipeline, or the injected fake. Wired live per use
        (:meth:`ConfigService.from_app`) because a reload swaps app internals; every
        manifest write — forward and unwind — crosses it."""
        if self._config_service is not None:
            return self._config_service
        return ConfigService.from_app()

    def _unwind_seams(self) -> dict[str, Any]:
        """The venv/config seams an unwind needs — a FRESH ``svc`` + ``cm`` (a prior
        reload may have swapped app internals) and the pip/prefix handles."""
        return {
            "svc": self._svc(),
            "cm": self._cm(),
            "pip_runner": self._pip_runner,
            "prefix": self._prefix,
            "prefix_uninstall": self._prefix_uninstall,
        }

    def _install_unwind_seams(self) -> dict[str, Any]:
        """The install unwind's seams — :meth:`_unwind_seams` plus the prefix-removal
        probes ``unwind_install`` uses to pip uninstall the just-installed package."""
        return {
            **self._unwind_seams(),
            "prefix_has_dist": self._prefix_has_dist,
            "env_dist_version": self._env_dist_version,
        }

    async def _run_plugin_migrations(self, spec: PluginSpec) -> None:
        """Apply the installed/upgraded plugin's schema migrations, when it declares
        a chain (opt-in: no ``migrations`` field is skipped).

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

    # -- preview ------------------------------------------------------------

    async def preview(
        self,
        ref: str,
        version: str | None = None,
        *,
        route_mounts: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Resolve a candidate install/update and report its routes and env picture
        WITHOUT changing any state (no pip, no manifest, no store write). A resolved
        public route under a reserved prefix, an unknown-item override, or a
        registry/contract fault raises loudly, exactly as the install would."""
        ns, name = resolve.parse_ref(ref)
        existing = await self._store.get(ref)
        resolved = await self._registry.resolve(ns, name, version)
        spec, _source = resolve.prepare_resolved(resolved)
        pinned_version = resolve.require(resolved, "version")
        return route_preflight.build_route_preview(
            spec,
            pinned_version,
            route_mounts,
            existing,
            owned_routes=self._owned_routes,
            reserved_prefixes=self._reserved_prefixes,
            effective_env=self._svc()._effective_env({}),
        )

    # -- install ------------------------------------------------------------

    async def install(
        self,
        ref: str,
        version: str | None = None,
        *,
        env: dict[str, str] | None = None,
        secret_keys: list[str] | None = None,
        route_mounts: dict[str, str] | None = None,
        accept_public_routes: bool = False,
    ) -> dict[str, Any]:
        """Install a marketplace plugin, aborting and unwinding on any failure.

        Ordered steps: not-installed + pip pre-flight; resolve the pinned version
        (the ONE pinning call, advisory/spec/contract checks included); collision +
        declared-route pre-flight BEFORE pip; ``pip install``; patch the manifest
        through the one door and reload; write the attribution row.

        ``env`` / ``secret_keys`` land in the env store in the SAME combined
        transaction as the provides entry when the spec declares install-time env;
        ``route_mounts`` remaps a route-carrying item's base and any public route
        requires ``accept_public_routes`` — both checked BEFORE pip. Each step past
        pip unwinds in reverse on failure (a failed unwind → :class:`InstallUnwindError`).
        """
        async with locks.operation_guard(self._fleet_lock):
            return await self._install_locked(ref, version, env, secret_keys, route_mounts, accept_public_routes)

    async def _install_locked(
        self,
        ref: str,
        version: str | None,
        env: dict[str, str] | None,
        secret_keys: list[str] | None,
        route_mounts: dict[str, str] | None,
        accept_public_routes: bool,
    ) -> dict[str, Any]:
        """Order the install steps: guards, resolve, package prep, env pre-check,
        collision + route pre-flight, then the package steps and the commit."""
        ns, name = resolve.parse_ref(ref)
        existing = await self._store.get(ref)
        if existing is not None:
            raise InstallStateError(f"{ref} is already installed (version {existing.version}); use update")

        resolved = await self._registry.resolve(ns, name, version)
        spec, source = resolve.prepare_resolved(resolved)
        pinned_version = resolve.require(resolved, "version")

        # Package prep runs only for a packaged spec: a descriptor-only plugin
        # (``spec.package is None``) installs nothing, so pip need not be present nor
        # the prefix writable. A set-but-unwritable prefix fails HERE for a packaged
        # spec, before any state change — never a silent fall back to the environment.
        if spec.package is not None:
            ensure_pip_available()
            if self._prefix is not None:
                self._prefix_ensure_writable(self._prefix)

        # Early, readable env pre-check before any pip work: a required var neither
        # supplied, stored, nor in the process env fails now, naming it.
        env_apply.precheck_required_env(self._cm(), spec, env)
        cm = self._cm()
        found = collisions(dict(cm.read_manifest_preserved()), spec)
        if found:
            raise ManifestCollisionError("; ".join(found))

        # Declared-route pre-flight BEFORE pip: an install requires acceptance of
        # every public row (no prior approval), and no route may clash with an
        # already-owned one. Persisted route_mounts cover every route-carrying item.
        resolved_mounts, resolved_route_list = route_preflight.route_preflight(
            spec,
            route_mounts,
            accept_public_routes,
            prior=None,
            exclude_ref=None,
            approved_public=None,
            owned_routes=self._owned_routes,
            reserved_prefixes=self._reserved_prefixes,
        )

        pip_output = await self._install_package_steps(spec, source, resolved, pinned_version)
        saved_manifest = copy.deepcopy(cm.read_manifest_preserved())
        apply_result = await self._commit_install(
            ref,
            pinned_version,
            spec,
            source,
            resolved,
            resolved_mounts,
            resolved_route_list,
            env,
            secret_keys,
            saved_manifest,
        )

        return {
            "ref": ref,
            "version": pinned_version,
            "package": spec.package,
            "advisories": resolve.require_list(resolved, "advisories"),
            "notes": notes.install_notes(spec),
            "reload": manifest_apply.reload_report(apply_result),
            "pip_output": pip_output,
            "routes": routes_mod.mounted_rows(resolved_route_list),
        }

    async def _install_package_steps(
        self, spec: PluginSpec, source: str, resolved: dict[str, Any], pinned_version: str
    ) -> str | None:
        """Land the package (or no-op for a descriptor-only spec): env-shadow guard,
        pip install, plugin migrations; on migration failure unwind the just-installed
        package and re-raise. Returns the pip output, or ``None`` when nothing was
        installed."""
        if spec.package is None:
            return None
        if self._prefix is not None:
            # A prefix install of a version the environment already shadows at a
            # DIFFERENT version is refused here, before any package/manifest/store write.
            package_ops.guard_env_shadow(
                spec.package, pinned_version, self._prefix, env_dist_version=self._env_dist_version
            )

        # pip install (github: fetch + verify + install the local tarball). A failure
        # propagates as-is; pip's own transaction handling is the boundary.
        pip_output = await package_ops.pip_install(
            self._pip_runner,
            self._prefix,
            spec.package,
            pinned_version,
            source,
            resolved.get("artifact_ref"),
            resolved.get("sha256"),
        )

        # Migrations run while the plugin is inert (package landed, manifest not yet
        # patched): a failure aborts with the manifest untouched, unwinding the package.
        try:
            await self._run_plugin_migrations(spec)
        except Exception as migration_error:
            await unwind.unwind_install(
                migration_error,
                package=spec.package,
                saved_manifest=None,
                env_restore=None,
                **self._install_unwind_seams(),
            )
            raise
        return pip_output

    async def _commit_install(
        self,
        ref: str,
        pinned_version: str,
        spec: PluginSpec,
        source: str,
        resolved: dict[str, Any],
        resolved_mounts: dict[str, str],
        resolved_route_list: list[routes_mod.ResolvedRoute],
        env: dict[str, str] | None,
        secret_keys: list[str] | None,
        saved_manifest: dict[str, Any],
    ) -> ApplyResult:
        """Commit the manifest+env+row transactionally: apply provides+env, write the
        attribution row, post-record remount reload; unwind on any failure."""
        manifest_persisted = False
        # ``env_restore`` captures the PRIOR store value of every key written, so the
        # unwind restores each rather than blind-deleting it.
        env_written = env_apply.env_to_write(env)
        env_restore: dict[str, str] = {}
        try:

            def mutator(document: dict[str, Any]) -> None:
                apply_provides(document, spec)

            apply_result = await env_apply.apply_provides_change(
                self._svc(),
                spec,
                mutator,
                env=env,
                secret_keys=secret_keys,
                env_to_write=env_written,
                env_restore=env_restore,
            )
            manifest_persisted = True
            repo_url, tag, artifact_ref, sha256 = resolve.pin_provenance(resolved, source)
            contract_version, skeleton_version = resolve.core_version_stamps()
            await self._store.record(
                ref,
                pinned_version,
                source,
                repo_url,
                tag,
                artifact_ref,
                sha256,
                spec.model_dump(mode="json"),
                contract_version=contract_version,
                skeleton_version=skeleton_version,
                route_mounts=resolved_mounts,
            )
            # With the row persisted, re-reload so the mount map remounts each route at
            # its remapped base. Route-declaring specs only; best-effort (never unwinds).
            if resolved_route_list:
                apply_result = await manifest_apply.remount_reload(
                    self._svc(), lambda: self._cm().read_manifest_preserved(), apply_result
                )
        except Exception as step_error:
            # The pipeline aborts a failed mutation with nothing persisted, so the
            # manifest needs restoring only when the change actually landed.
            persisted = manifest_persisted or isinstance(step_error, FleetBroadcastError)
            await unwind.unwind_install(
                step_error,
                package=spec.package,
                saved_manifest=saved_manifest if persisted else None,
                env_restore=env_restore,
                **self._install_unwind_seams(),
            )
            raise
        return apply_result

    # -- uninstall ----------------------------------------------------------

    async def uninstall(self, ref: str) -> dict[str, Any]:
        """Uninstall a marketplace-installed plugin — convergent and registry-free.

        Order: pip pre-flight; read the stored record (unknown ref → not-installed);
        reconstruct the spec from LOCAL truth; unpatch the manifest through the one
        door and reload to CONVERGENCE (a re-run re-attempts a prior failed deregister
        reload; skipped only for an all-env-selected plugin); pip uninstall; drop the
        row. There is no unwind-to-installed — re-running forward is the recovery.
        """
        async with locks.operation_guard(self._fleet_lock):
            return await self._uninstall_locked(ref)

    async def _uninstall_locked(self, ref: str) -> dict[str, Any]:
        row = await self._store.get(ref)
        if row is None:
            raise InstallStateError(f"{ref} is not installed", not_installed=True)
        spec = resolve.spec_from_row(row)
        if spec.package is not None and self._prefix is None:
            # The environment path shells ``pip uninstall``; the prefix path removes
            # the plugin's files by its RECORD and needs no pip. A descriptor-only
            # plugin (``spec.package is None``) removes no package, so needs no pip.
            ensure_pip_available()

        cm = self._cm()
        reload_result: dict[str, Any] | None
        changed = remove_provides(dict(cm.read_manifest_preserved()), spec)
        if changed or provides.has_manifest_provides(spec):
            # Converge the live registration to the stripped manifest BEFORE removing
            # the package. The apply runs even when nothing changed now but the spec
            # targets manifest fields, so a prior run's failed deregister reload is
            # re-attempted (idempotent) before the pip uninstall + row drop.
            def mutator(document: dict[str, Any]) -> None:
                remove_provides(document, spec)

            reload_result = manifest_apply.reload_report(await manifest_apply.apply_composed(self._svc(), mutator))
        else:
            # A plugin whose provides are all env-selected wrote no manifest
            # entry, so there is nothing to deregister — no apply, no reload.
            reload_result = None

        # A failure here leaves the app converged but the package present;
        # re-running uninstall (step 3 now a no-op) completes the removal. A
        # descriptor-only plugin installed no package, so there is nothing to remove.
        removed = (
            await package_ops.remove_package(
                spec.package,
                prefix=self._prefix,
                pip_runner=self._pip_runner,
                prefix_uninstall=self._prefix_uninstall,
                prefix_has_dist=self._prefix_has_dist,
                env_dist_version=self._env_dist_version,
            )
            if spec.package is not None
            else False
        )
        await self._store.delete(ref)

        removal_notes = notes.uninstall_notes(spec)
        if spec.package is not None and self._prefix is not None and not removed:
            # The env-shadowed no-op install landed nothing in the prefix; surface the
            # deregister-only removal in the result, never only a log line.
            env_version = self._env_dist_version(spec.package, self._prefix)
            removal_notes.append(
                f"{spec.package!r} was absent from the plugin prefix; the environment provides it at "
                f"{env_version} — removed no files, only deregistered and dropped the attribution record"
            )
        return {"ref": ref, "uninstalled": True, "reload": reload_result, "notes": removal_notes}

    # -- update -------------------------------------------------------------

    async def update(
        self,
        ref: str,
        version: str | None = None,
        *,
        env: dict[str, str] | None = None,
        secret_keys: list[str] | None = None,
        route_mounts: dict[str, str] | None = None,
        accept_public_routes: bool = False,
    ) -> dict[str, Any]:
        """Update an installed plugin to a newer (or named) version — an install of
        the new version with the same pre-flights, in one manifest read-modify-write.

        Order: parse the ref (before the store read, so an unparseable ref is never a
        phantom 404); pip pre-flight; read the stored record for the old spec and pin;
        resolve the target (target == installed → state error, a delivery-form switch
        refused); collision pre-flight for the NEW spec against the manifest with the
        OLD entries removed in memory (a rename must not self-collide); ``route_mounts``
        remap keeping surviving items' stored bases; ``pip install`` the new pin; one
        pipeline apply removing the old entries and applying the new; upsert the row.

        A Step 5/6 failure unwinds in order — reinstall the OLD pin (the old wheel back
        BEFORE the restore's reload), then restore the manifest — and a failed sub-step
        escalates to :class:`InstallUnwindError`.
        """
        async with locks.operation_guard(self._fleet_lock):
            return await self._update_locked(ref, version, env, secret_keys, route_mounts, accept_public_routes)

    async def _update_locked(
        self,
        ref: str,
        version: str | None,
        env: dict[str, str] | None,
        secret_keys: list[str] | None,
        route_mounts: dict[str, str] | None,
        accept_public_routes: bool,
    ) -> dict[str, Any]:
        """Order the update steps: guards, resolve, delivery-form guard, package
        prep, env pre-check, collision + route pre-flight, then the package steps and
        the commit."""
        ns, name = resolve.parse_ref(ref)
        row = await self._store.get(ref)
        if row is None:
            raise InstallStateError(f"{ref} is not installed", not_installed=True)
        old_spec = resolve.spec_from_row(row)

        resolved = await self._registry.resolve(ns, name, version)
        new_spec, source = resolve.prepare_resolved(resolved)
        pinned_version = resolve.require(resolved, "version")
        if pinned_version == row.version:
            raise InstallStateError(f"{ref} is already at {pinned_version}")

        # A version may not switch delivery form (packaged <-> descriptor-only) in
        # place — an in-place pip upgrade has no meaning across the boundary.
        if (old_spec.package is None) != (new_spec.package is None):
            raise InstallStateError("delivery form changed between versions; uninstall then install")

        # Package prep runs only for a packaged new spec (a descriptor-only plugin
        # installs nothing). Done after the resolve so the delivery form is known.
        if new_spec.package is not None:
            ensure_pip_available()
            if self._prefix is not None:
                self._prefix_ensure_writable(self._prefix)

        # Early, readable env pre-check before any pip work (mirrors install).
        env_apply.precheck_required_env(self._cm(), new_spec, env)
        cm = self._cm()
        preview = dict(cm.read_manifest_preserved())
        remove_provides(preview, old_spec)
        found = collisions(preview, new_spec)
        if found:
            raise ManifestCollisionError("; ".join(found))

        # Declared-route pre-flight BEFORE pip: keep surviving items' stored bases,
        # collision-check excluding the plugin's OWN routes, and require acceptance
        # only for public rows NOT already approved in the installed version.
        resolved_mounts, resolved_route_list = route_preflight.route_preflight(
            new_spec,
            route_mounts,
            accept_public_routes,
            prior=row.route_mounts,
            exclude_ref=ref,
            approved_public=route_preflight.approved_public_of(row),
            owned_routes=self._owned_routes,
            reserved_prefixes=self._reserved_prefixes,
        )

        pip_output = await self._update_package_steps(old_spec, new_spec, source, resolved, pinned_version, row)
        saved_manifest = copy.deepcopy(cm.read_manifest_preserved())
        apply_result = await self._commit_update(
            ref,
            pinned_version,
            old_spec,
            new_spec,
            source,
            resolved,
            resolved_mounts,
            resolved_route_list,
            env,
            secret_keys,
            row,
            saved_manifest,
        )

        return {
            "ref": ref,
            "version": pinned_version,
            "package": new_spec.package,
            "advisories": resolve.require_list(resolved, "advisories"),
            "notes": notes.install_notes(new_spec),
            "reload": manifest_apply.reload_report(apply_result),
            "pip_output": pip_output,
            "routes": routes_mod.mounted_rows(resolved_route_list),
        }

    async def _update_package_steps(
        self,
        old_spec: PluginSpec,
        new_spec: PluginSpec,
        source: str,
        resolved: dict[str, Any],
        pinned_version: str,
        row: InstallRecord,
    ) -> str | None:
        """Move the venv to the new pin: env-shadow guard; on the prefix path remove
        the old wheel first and reinstall it if the new install fails; pip install the
        new pin; plugin migrations, unwinding to the old pin on migration failure.
        Returns the pip output, or ``None`` for a descriptor-only update."""
        if new_spec.package is None:
            return None
        # The delivery-form check guarantees the old spec is packaged too.
        assert old_spec.package is not None
        if self._prefix is not None:
            # Refused before any state change, and before the old prefix wheel is
            # removed below, when the environment shadows the prefix at another version.
            package_ops.guard_env_shadow(
                new_spec.package, pinned_version, self._prefix, env_dist_version=self._env_dist_version
            )

        # On the prefix path pip cannot upgrade a distribution it does not see on its
        # own path, so the old version is removed first and reinstalled if the new
        # install then fails (a failed restore escalates to a loud unwind error). The
        # environment path pip upgrades in place; a failure propagates as-is.
        if self._prefix is not None:
            self._prefix_uninstall(old_spec.package, self._prefix)
            try:
                pip_output = await package_ops.pip_install(
                    self._pip_runner,
                    self._prefix,
                    new_spec.package,
                    pinned_version,
                    source,
                    resolved.get("artifact_ref"),
                    resolved.get("sha256"),
                )
            except Exception as install_error:
                try:
                    await package_ops.pip_install(
                        self._pip_runner,
                        self._prefix,
                        old_spec.package,
                        row.version,
                        row.source,
                        row.artifact_ref,
                        row.sha256,
                    )
                except Exception as restore_error:
                    raise InstallUnwindError(install_error, restore_error) from install_error
                raise
        else:
            pip_output = await package_ops.pip_install(
                self._pip_runner,
                self._prefix,
                new_spec.package,
                pinned_version,
                source,
                resolved.get("artifact_ref"),
                resolved.get("sha256"),
            )

        # Migrations run before the manifest patch (the new wheel is landed but inert),
        # so a failure unwinds to the OLD pin with the manifest untouched.
        try:
            await self._run_plugin_migrations(new_spec)
        except Exception as migration_error:
            await unwind.unwind_update(
                migration_error,
                old_package=old_spec.package,
                new_package=new_spec.package,
                row=row,
                saved_manifest=None,
                env_restore=None,
                **self._unwind_seams(),
            )
            raise
        return pip_output

    async def _commit_update(
        self,
        ref: str,
        pinned_version: str,
        old_spec: PluginSpec,
        new_spec: PluginSpec,
        source: str,
        resolved: dict[str, Any],
        resolved_mounts: dict[str, str],
        resolved_route_list: list[routes_mod.ResolvedRoute],
        env: dict[str, str] | None,
        secret_keys: list[str] | None,
        row: InstallRecord,
        saved_manifest: dict[str, Any],
    ) -> ApplyResult:
        """Commit the swap transactionally: one combined remove-old+apply-new
        manifest/env apply, upsert the row, remount reload; unwind on failure."""
        manifest_persisted = False
        env_written = env_apply.env_to_write(env)
        env_restore: dict[str, str] = {}
        try:

            def mutator(document: dict[str, Any]) -> None:
                remove_provides(document, old_spec)
                apply_provides(document, new_spec)

            apply_result = await env_apply.apply_provides_change(
                self._svc(),
                new_spec,
                mutator,
                env=env,
                secret_keys=secret_keys,
                env_to_write=env_written,
                env_restore=env_restore,
            )
            manifest_persisted = True
            repo_url, tag, artifact_ref, sha256 = resolve.pin_provenance(resolved, source)
            contract_version, skeleton_version = resolve.core_version_stamps()
            await self._store.record(
                ref,
                pinned_version,
                source,
                repo_url,
                tag,
                artifact_ref,
                sha256,
                new_spec.model_dump(mode="json"),
                contract_version=contract_version,
                skeleton_version=skeleton_version,
                route_mounts=resolved_mounts,
            )
            # With the upserted row persisted, re-reload so the mount map remounts each
            # route at its base. Route-declaring specs only; best-effort.
            if resolved_route_list:
                apply_result = await manifest_apply.remount_reload(
                    self._svc(), lambda: self._cm().read_manifest_preserved(), apply_result
                )
        except Exception as step_error:
            persisted = manifest_persisted or isinstance(step_error, FleetBroadcastError)
            await unwind.unwind_update(
                step_error,
                old_package=old_spec.package,
                new_package=new_spec.package,
                row=row,
                saved_manifest=saved_manifest if persisted else None,
                env_restore=env_restore,
                **self._unwind_seams(),
            )
            raise
        return apply_result

    # -- upgrade-all --------------------------------------------------------

    async def upgrade_all(self) -> list[dict[str, Any]]:
        """Upgrade every installed plugin to its latest COMPATIBLE version under ONE
        hold of the per-worker + fleet locks, returning one report entry per ref
        (``{ref, outcome, detail}`` with outcome ``upgraded`` / ``up-to-date`` /
        ``no-compatible-version`` / ``failed``). A per-ref failure NEVER aborts the
        batch — it is logged and reported so one broken ref never hides the rest.
        """
        async with locks.operation_guard(self._fleet_lock):
            return await self._upgrade_all_locked()

    async def _upgrade_all_locked(self) -> list[dict[str, Any]]:
        contract = running_contract_version()
        return [await self._upgrade_one(row, contract) for row in await self._store.list_installed()]

    async def _upgrade_one(self, row: InstallRecord, contract: str) -> dict[str, Any]:
        """One ref's upgrade attempt → its report entry. The target is picked
        from the registry's version rows (published + contract-compatible, the
        same computation the installed listing's ``update_available`` serves,
        so this upgrades exactly what that listing advertises); the move itself
        is the ordinary update flow against the picked pin."""
        ns, name = resolve.parse_ref(row.ref)
        try:
            versions = await self._registry.versions(ns, name)
            targets = update_targets(versions, installed_version=row.version, contract_version=contract)
            blocked = (
                f" ({targets.incompatible_newer} exists but does not support tai42-contract {contract})"
                if targets.incompatible_newer is not None
                else ""
            )
            if targets.latest_compatible is None:
                detail = f"no published version supports tai42-contract {contract}{blocked}"
                logger.error("upgrade-all: %s has no compatible version — %s", row.ref, detail)
                return {"ref": row.ref, "outcome": "no-compatible-version", "detail": detail}
            if not targets.update_available:
                return {
                    "ref": row.ref,
                    "outcome": "up-to-date",
                    "detail": f"{row.version} is the latest compatible version{blocked}",
                }
            result = await self._update_locked(row.ref, targets.latest_compatible, None, None, None, False)
            return {"ref": row.ref, "outcome": "upgraded", "detail": f"{row.version} -> {result['version']}"}
        except Exception as exc:
            # The explicit per-ref recovery path: logged with the traceback and
            # reported, so one broken ref never hides the rest of the batch.
            logger.exception("upgrade-all: upgrading %s failed", row.ref)
            return {"ref": row.ref, "outcome": "failed", "detail": str(exc)}
