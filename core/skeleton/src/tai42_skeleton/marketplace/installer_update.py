"""The update and upgrade-all flows of the marketplace installer.

:class:`_UpdateFlow` orders ``update`` (an install of the new version with the same
pre-flights, in one manifest read-modify-write) and ``upgrade_all`` (every installed
plugin to its latest compatible version under one lock hold) as abort-on-any-step
sequences with an explicit reverse unwind, delegating each concern to a sibling module.
It inherits the shared config, pip and prefix seams from
:class:`~tai42_skeleton.marketplace.installer_base._InstallerBase`.
"""

from __future__ import annotations

import logging
from typing import Any

from tai42_contract.plugins import PluginSpec

from tai42_skeleton.config.service import ApplyResult, joined_span_release_errors
from tai42_skeleton.marketplace import (
    env_apply,
    locks,
    manifest_apply,
    notes,
    package_ops,
    resolve,
    route_preflight,
    unwind,
)
from tai42_skeleton.marketplace import routes as routes_mod
from tai42_skeleton.marketplace.compat import UpgradeOutcome, running_contract_version, update_targets
from tai42_skeleton.marketplace.errors import InstallStateError, InstallUnwindError, ManifestCollisionError
from tai42_skeleton.marketplace.installer_base import _InstallerBase
from tai42_skeleton.marketplace.manifest_patch import ProvidesUndo, collisions, remove_provides, update_provides
from tai42_skeleton.marketplace.pip import ensure_pip_available
from tai42_skeleton.marketplace.store import InstallRecord
from tai42_skeleton.operations._broadcast import FleetBroadcastError

logger = logging.getLogger(__name__)


def _upgrade_row(
    ref: str, outcome: UpgradeOutcome, detail: str, span_release_error: str | None = None
) -> dict[str, Any]:
    """One ref's upgrade-all report entry.

    ``span_release_error`` is the ref's config-store errors when the exclusive write span of a
    config write it landed failed to release, joined in write order — an upgraded ref's manifest
    write, or a failed ref's forward write and its unwind's restore writes (``None`` when each
    released, or the ref wrote nothing).
    """
    return {"ref": ref, "outcome": outcome, "detail": detail, "span_release_error": span_release_error}


class _UpdateFlow(_InstallerBase):
    """Update and batch-upgrade marketplace plugins with abort-and-unwind."""

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
        """Update an installed plugin to a newer (or named) version.

        An install of the new version with the same pre-flights, in one manifest
        read-modify-write.

        Order: parse the ref (before the store read, so an unparseable ref is never a
        phantom 404); pip pre-flight; read the stored record for the old spec and pin;
        resolve the target (target == installed → state error, a delivery-form switch
        refused); collision pre-flight for the NEW spec against the manifest with the
        OLD entries removed in memory (a rename must not self-collide); ``route_mounts``
        remap keeping surviving items' stored bases; ``pip install`` the new pin; one
        pipeline apply removing the old entries and applying the new; upsert the row.

        A Step 5/6 failure unwinds in order — reinstall the OLD pin (the old wheel back
        BEFORE the reversal's reload), then reverse the provides swap — and a failed sub-step
        escalates to :class:`InstallUnwindError`.
        """
        async with locks.operation_guard(self._fleet_lock):
            # A failed update answers non-2xx, so the release errors its landed writes collect
            # are not part of its answer (each failed release is logged at ERROR by the span).
            return await self._update_locked(
                ref, version, env, secret_keys, route_mounts, accept_public_routes, landed_release_errors=[]
            )

    async def _update_locked(
        self,
        ref: str,
        version: str | None,
        env: dict[str, str] | None,
        secret_keys: list[str] | None,
        route_mounts: dict[str, str] | None,
        accept_public_routes: bool,
        *,
        landed_release_errors: list[str | None],
    ) -> dict[str, Any]:
        """Order the update steps under the held lock.

        Guards, resolve, delivery-form guard, package prep, env pre-check, collision + route
        pre-flight, then the package steps and the commit. When the commit fails after config
        writes landed, ``landed_release_errors`` receives their span release errors in write
        order (see :meth:`_commit_update`); a success returns them on ``reload``.
        """
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
            landed_release_errors=landed_release_errors,
        )

        return {
            "ref": ref,
            "version": pinned_version,
            "package": new_spec.package,
            "advisories": resolve.require_list(resolved, "advisories"),
            "notes": notes.install_notes(new_spec),
            "reload": apply_result,
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
        """Move the venv to the new pin.

        Env-shadow guard; on the prefix path remove the old wheel first and reinstall it if the
        new install fails; pip install the new pin; plugin migrations, unwinding to the old pin on
        migration failure. Returns the pip output, or ``None`` for a descriptor-only update.
        """
        if new_spec.package is None:
            return None
        # The delivery-form check guarantees the old spec is packaged too.
        if old_spec.package is None:
            raise AssertionError
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
                    env_constraints=self._env_constraints,
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
                        env_constraints=self._env_constraints,
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
                env_constraints=self._env_constraints,
            )

        # Migrations run before the manifest patch (the new wheel is landed but inert),
        # so a failure unwinds to the OLD pin with the manifest untouched.
        try:
            await self._run_plugin_migrations(new_spec)
        except Exception as migration_error:
            # No manifest or env to restore, so this unwind writes no config.
            await unwind.unwind_update(
                migration_error,
                old_package=old_spec.package,
                new_package=new_spec.package,
                row=row,
                provides_undo=None,
                env_written=None,
                **self._update_unwind_seams(),
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
        *,
        landed_release_errors: list[str | None],
    ) -> ApplyResult:
        """Commit the swap transactionally.

        One combined remove-old+apply-new manifest/env apply, upsert the row, remount reload;
        unwind on failure. A failure appends to ``landed_release_errors`` the span release error of
        the forward write (from its result, or from the :class:`FleetBroadcastError` it raised after
        landing), then those of the unwind's restore writes (returned, or carried on the escalated
        :class:`InstallUnwindError`), and re-raises.
        """
        manifest_persisted = False
        forward_release_error: str | None = None
        env_values = env_apply.env_to_write(env)
        env_written = env_apply.EnvWrite()
        provides_undo: list[ProvidesUndo] = []
        try:

            def mutator(document: dict[str, Any]) -> None:
                # The last run is the one that persists, so its undo is the one kept.
                provides_undo[:] = [update_provides(document, old_spec, new_spec)]

            apply_result = await env_apply.apply_provides_change(
                self._svc(),
                new_spec,
                mutator,
                env=env,
                secret_keys=secret_keys,
                env_to_write=env_values,
                env_written=env_written,
            )
            manifest_persisted = True
            forward_release_error = apply_result.span_release_error
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
                apply_result = await manifest_apply.remount_reload(self._svc(), apply_result)
        except Exception as step_error:
            persisted = manifest_persisted or isinstance(step_error, FleetBroadcastError)
            if isinstance(step_error, FleetBroadcastError):
                forward_release_error = step_error.span_release_error
            landed_release_errors.append(forward_release_error)
            try:
                restore_release_error = await unwind.unwind_update(
                    step_error,
                    old_package=old_spec.package,
                    new_package=new_spec.package,
                    row=row,
                    provides_undo=provides_undo[0] if persisted else None,
                    env_written=env_written,
                    **self._update_unwind_seams(),
                )
            except InstallUnwindError as unwind_failure:
                landed_release_errors.append(unwind_failure.span_release_error)
                raise
            landed_release_errors.append(restore_release_error)
            raise
        return apply_result

    # -- upgrade-all --------------------------------------------------------

    async def upgrade_all(self) -> list[dict[str, Any]]:
        """Upgrade every installed plugin to its latest COMPATIBLE version under one lock hold.

        Held across the per-worker + fleet locks, returning one report entry per ref
        (``{ref, outcome, detail, span_release_error}`` with outcome ``upgraded`` /
        ``up-to-date`` / ``no-compatible-version`` / ``failed``). ``span_release_error`` is the
        ref's config-store errors when the write span of a config write it landed failed to
        release, joined in write order — an ``upgraded`` ref's manifest write, or a ``failed``
        ref's forward write and its unwind's restore writes — else ``None``. A per-ref failure
        NEVER aborts the batch — it is logged and reported so one broken ref never hides the rest.
        """
        async with locks.operation_guard(self._fleet_lock):
            return await self._upgrade_all_locked()

    async def _upgrade_all_locked(self) -> list[dict[str, Any]]:
        contract = running_contract_version()
        return [await self._upgrade_one(row, contract) for row in await self._store.list_installed()]

    async def _upgrade_one(self, row: InstallRecord, contract: str) -> dict[str, Any]:
        """One ref's upgrade attempt, resolved to its report entry.

        The target is picked from the registry's version rows (published + contract-compatible, the
        same computation the installed listing's ``update_available`` serves, so this upgrades
        exactly what that listing advertises); the move itself is the ordinary update flow against
        the picked pin.
        """
        ns, name = resolve.parse_ref(row.ref)
        landed_release_errors: list[str | None] = []
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
                return _upgrade_row(row.ref, "no-compatible-version", detail)
            if not targets.update_available:
                return _upgrade_row(row.ref, "up-to-date", f"{row.version} is the latest compatible version{blocked}")
            result = await self._update_locked(
                row.ref,
                targets.latest_compatible,
                None,
                None,
                None,
                False,
                landed_release_errors=landed_release_errors,
            )
            reload: ApplyResult = result["reload"]
            return _upgrade_row(row.ref, "upgraded", f"{row.version} -> {result['version']}", reload.span_release_error)
        except Exception as exc:
            # The explicit per-ref recovery path: logged with the traceback and
            # reported, so one broken ref never hides the rest of the batch.
            logger.exception("upgrade-all: upgrading %s failed", row.ref)
            return _upgrade_row(row.ref, "failed", str(exc), joined_span_release_errors(*landed_release_errors))
