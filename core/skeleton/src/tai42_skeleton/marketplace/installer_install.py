"""The install, uninstall, and preview flows of the marketplace installer.

:class:`_InstallFlow` orders ``preview`` / ``install`` / ``uninstall`` as
abort-on-any-step sequences with an explicit reverse unwind, delegating each concern to
a sibling module (``locks``, ``resolve``, ``environments``, ``manifest_apply``,
``env_apply``, ``route_preflight``, ``notes``, ``unwind``, ``cancel_hold``). It inherits
the shared config, pip and plugins-directory seams from
:class:`~tai42_skeleton.marketplace.installer_base._InstallerBase`.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from tai42_contract.plugins import PluginSpec

from tai42_skeleton.app.route_registry import CrossOwnerRouteCollisionError
from tai42_skeleton.config.service import ApplyResult, joined_span_release_errors
from tai42_skeleton.marketplace import (
    env_apply,
    manifest_apply,
    notes,
    provides,
    resolve,
    route_preflight,
)
from tai42_skeleton.marketplace import routes as routes_mod
from tai42_skeleton.marketplace.cancel_hold import run_to_end
from tai42_skeleton.marketplace.environments.change import OperationChange
from tai42_skeleton.marketplace.environments.targets import place, place_row
from tai42_skeleton.marketplace.errors import InstallStateError, ManifestCollisionError
from tai42_skeleton.marketplace.installer_base import _InstallerBase
from tai42_skeleton.marketplace.manifest_patch import ProvidesUndo, apply_provides, collisions, remove_provides
from tai42_skeleton.marketplace.operation_roll import guarded_operation
from tai42_skeleton.marketplace.pip import ensure_pip_available
from tai42_skeleton.operations._broadcast import FleetBroadcastError


class _InstallFlow(_InstallerBase):
    """Install, uninstall, and preview marketplace plugins with abort-and-unwind."""

    # -- preview ------------------------------------------------------------

    async def preview(
        self,
        ref: str,
        version: str | None = None,
        *,
        route_mounts: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Resolve a candidate install/update and report its routes and env picture without any change.

        No pip, no manifest, no store write. A resolved public route under a reserved prefix, an
        unknown-item override, or a registry/contract fault raises loudly, exactly as the install
        would.
        """
        ns, name = resolve.parse_ref(ref)
        existing = await self._store.get(ref)
        resolved = await self._registry.resolve(ns, name, version)
        spec, _source = resolve.prepare_resolved(resolved)
        pinned_version = resolve.require(resolved, "version")
        svc = self._svc()
        return route_preflight.build_route_preview(
            spec,
            pinned_version,
            route_mounts,
            existing,
            owned_routes=self._owned_routes,
            reserved_prefixes=self._reserved_prefixes,
            effective_env=svc.effective_env({}, await svc.deployment_ownership("marketplace_install_preview")),
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
        declared-route pre-flight BEFORE pip; build the plugin's environment (the layer
        rebuilt with it, or its own server environment) beside the one in use and swap it
        in; plugin migrations; patch the manifest through the one door and reload; write
        the attribution row; delete the unused builds; then, with the fleet key downgraded to
        shared, roll the fleet recycle the change owes (:func:`guarded_operation`), so a worker
        it restarts verifies the finished install.

        ``env`` / ``secret_keys`` land in the env store in the SAME combined
        transaction as the provides entry when the spec declares install-time env;
        ``route_mounts`` remaps a route-carrying item's base and any public route
        requires ``accept_public_routes`` — both checked BEFORE pip. Each step past
        the swap unwinds in reverse on failure, the environment swapped back first (a failed
        unwind → :class:`InstallUnwindError`). A cancellation while the migrations run swaps
        the environment back; one that lands while the change is written waits for that write
        to end whole — complete, or unwound when it fails — and then propagates.
        """
        async with guarded_operation(self._fleet_lock, self._svc, change=f"the install of {ref}") as operation:
            receipt = await self._install_locked(ref, version, env, secret_keys, route_mounts, accept_public_routes)
        receipt["reload"] = operation.settled(receipt["reload"])
        return receipt

    async def _install_locked(
        self,
        ref: str,
        version: str | None,
        env: dict[str, str] | None,
        secret_keys: list[str] | None,
        route_mounts: dict[str, str] | None,
        accept_public_routes: bool,
    ) -> dict[str, Any]:
        """Order the install steps under the held lock.

        Guards, resolve, package prep, env pre-check, collision + route pre-flight, then the
        package steps and the commit.
        """
        ns, name = resolve.parse_ref(ref)
        existing = await self._store.get(ref)
        if existing is not None:
            raise InstallStateError(f"{ref} is already installed (version {existing.version}); use update")

        resolved = await self._registry.resolve(ns, name, version)
        spec, source = resolve.prepare_resolved(resolved)
        pinned_version = resolve.require(resolved, "version")

        # Package prep runs only for a packaged spec: a descriptor-only plugin
        # (``spec.package is None``) installs nothing, so needs neither pip nor the
        # plugins directory. An unset plugins directory refuses HERE, before any state change.
        if spec.package is not None:
            ensure_pip_available()
            self._require_plugins_dir()

        # Early, readable env pre-check before any pip work: a required var neither
        # supplied, stored, nor in the process env fails now, naming it.
        env_apply.precheck_required_env(self._cm(), spec, env)
        found = collisions(self._svc().read_manifest_document(), spec)
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

        change = await self._install_package_steps(ref, spec, source, resolved, pinned_version)
        # A cancellation landing while the change is written waits for the commit to end whole.
        apply_result, environment_notes = await run_to_end(
            self._commit_install(
                ref,
                pinned_version,
                spec,
                source,
                resolved,
                resolved_mounts,
                resolved_route_list,
                env,
                secret_keys,
                change,
            )
        )

        return {
            "ref": ref,
            "version": pinned_version,
            "package": spec.package,
            "advisories": resolve.require_list(resolved, "advisories"),
            "notes": [*notes.install_notes(spec), *environment_notes],
            "reload": apply_result,
            "pip_output": None if change is None else change.pip_output,
            "routes": routes_mod.mounted_rows(resolved_route_list),
        }

    async def _install_package_steps(
        self, ref: str, spec: PluginSpec, source: str, resolved: dict[str, Any], pinned_version: str
    ) -> OperationChange | None:
        """Build the plugin's environment and make it current, then run its migrations.

        A descriptor-only spec builds nothing and returns ``None``. A migration failure or a
        cancellation while the migrations run swaps the environment back and re-raises. Returns
        the change for the commit to unwind or finish.
        """
        _repo_url, _tag, artifact_ref, sha256 = resolve.pin_provenance(resolved, source)
        placed = place(ref, spec, pinned_version, source, artifact_ref, sha256)
        if placed is None:
            return None
        change = await self._prepare_environment(ref, None, placed, subject=f"cannot install {ref} {pinned_version}")
        change.swap()

        # Migrations run while the plugin is inert (environment current, manifest not yet
        # patched): a failure swaps the environment back with the manifest untouched.
        try:
            await self._run_plugin_migrations(spec)
        except (Exception, asyncio.CancelledError) as migration_error:
            await self._unwind(migration_error, change=change, provides_undo=None, env_written=None)
            raise
        return change

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
        change: OperationChange | None,
    ) -> tuple[ApplyResult, list[str]]:
        """Commit the manifest+env+row transactionally, then finish ``change``.

        Apply provides+env, write the attribution row, post-record remount reload; on any
        failure swap ``change`` back and unwind the config writes. Once the row is written the
        unused builds are deleted (:meth:`OperationChange.finish`); returns the change's result
        and the environment notes of that finish.

        The forward provides reload runs BEFORE the attribution row exists, so its mount map
        (which reads the store) mounts every route-carrying item at its DECLARED base. A
        REMAPPED item therefore transiently registers at its declared base and, when that base
        already belongs to another owner, the reload raises a route collision. The pre-flight
        has already proved the RESOLVED base is clear (a same-base collision is refused there,
        never here), so that one failure is recovered by recording the row and remounting at
        the resolved base — the exact job the post-record remount already does. Every other
        reload failure, and a collision no remap resolves, propagates and unwinds.
        """
        manifest_persisted = False
        row_persisted = False
        # ``env_written`` records what the env write changes, so the unwind reverts exactly
        # that rather than blind-deleting a key.
        env_written = env_apply.EnvWrite()
        provides_undo: list[ProvidesUndo] = []
        try:

            def mutator(document: dict[str, Any]) -> None:
                # The last run is the one that persists, so its undo is the one kept.
                provides_undo[:] = [apply_provides(document, spec)]

            try:
                apply_result = await env_apply.apply_provides_change(
                    self._svc(),
                    spec,
                    mutator,
                    env=env,
                    secret_keys=secret_keys,
                    env_written=env_written,
                )
            except FleetBroadcastError as reload_error:
                if not (resolved_route_list and isinstance(reload_error.__cause__, CrossOwnerRouteCollisionError)):
                    raise
                # The provides patch persisted; the forward reload collided only because the
                # remapped item was still mounted at its declared base. Record the row and
                # remount at the resolved base — authoritative here (a failure is a real
                # install failure, not the best-effort degrade the non-collision path allows).
                manifest_persisted = True
                await self._record_install_row(ref, pinned_version, spec, source, resolved, resolved_mounts)
                row_persisted = True
                # The recovery reload records its report in the operation's deferred scope like
                # the forward one, so the operation's one recycle decision reads it. The forward
                # write landed, so its failed span release is the change's too and rides beside
                # the recovery's own.
                recovered = await self._svc().reapply_manifest()
                apply_result = dataclasses.replace(
                    recovered,
                    span_release_error=joined_span_release_errors(
                        reload_error.span_release_error, recovered.span_release_error
                    ),
                )
            else:
                manifest_persisted = True
                await self._record_install_row(ref, pinned_version, spec, source, resolved, resolved_mounts)
                row_persisted = True
                # With the row persisted, re-reload so the mount map remounts each route at
                # its remapped base. Route-declaring specs only; best-effort (never unwinds).
                if resolved_route_list:
                    apply_result = await manifest_apply.remount_reload(self._svc(), apply_result)
        except Exception as step_error:
            # The pipeline aborts a failed mutation with nothing persisted, so the
            # manifest needs restoring only when the change actually landed.
            persisted = manifest_persisted or isinstance(step_error, FleetBroadcastError)
            await self._unwind(
                step_error,
                change=change,
                provides_undo=provides_undo[0] if persisted else None,
                env_written=env_written,
            )
            # The row is written only after the manifest reload, so it needs dropping only on
            # the collision-recovery path, where it precedes the authoritative remount.
            if row_persisted:
                await self._store.delete(ref)
            raise
        return apply_result, [] if change is None else change.finish()

    async def _record_install_row(
        self,
        ref: str,
        pinned_version: str,
        spec: PluginSpec,
        source: str,
        resolved: dict[str, Any],
        resolved_mounts: dict[str, str],
    ) -> None:
        """Write the attribution row for a committed install, with its resolved route mounts."""
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

    # -- uninstall ----------------------------------------------------------

    async def uninstall(self, ref: str) -> dict[str, Any]:
        """Uninstall a marketplace-installed plugin — convergent and registry-free.

        Order: read the stored record (unknown ref → not-installed); reconstruct the spec
        from LOCAL truth; for a packaged plugin, rebuild the layer without it and swap it in
        (an ``mcp-server`` package builds nothing and its ``current`` is withdrawn); unpatch
        the manifest through the one door and converge it the way this installer's door
        converges — the fleet reloads now, or the deployment loads it at its next start (a
        re-run re-attempts a prior failed deregister; skipped only for an all-env-selected
        plugin); drop the row; delete the unused builds, or the server package's whole
        environment; then, with the fleet key downgraded to shared, roll the fleet recycle the
        convergence owes (:func:`guarded_operation`). A failed deregister or row delete swaps the
        environment back and re-raises, leaving whatever manifest change landed in place;
        re-running forward is the recovery. A cancellation that lands once the environment is
        swapped waits for the removal to end whole and then propagates. The receipt's
        ``manifest_written`` says whether the manifest was written.
        """
        async with guarded_operation(self._fleet_lock, self._svc, change=f"the uninstall of {ref}") as operation:
            receipt = await self._uninstall_locked(ref)
        receipt["reload"] = operation.settled(receipt["reload"])
        return receipt

    async def _uninstall_locked(self, ref: str) -> dict[str, Any]:
        row = await self._store.get(ref)
        if row is None:
            raise InstallStateError(f"{ref} is not installed", not_installed=True)
        spec = resolve.spec_from_row(row)
        placed = place_row(row)
        change: OperationChange | None = None
        if placed is not None:
            if not placed.runs_as_server:
                ensure_pip_available()
            change = await self._prepare_environment(ref, placed, None, subject=f"cannot uninstall {ref} {row.version}")
            change.swap()
        # A cancellation landing while the removal is written waits for the commit to end whole.
        manifest_written, reload_result, environment_notes = await run_to_end(self._commit_uninstall(ref, spec, change))
        return {
            "ref": ref,
            "uninstalled": True,
            "manifest_written": manifest_written,
            "reload": reload_result,
            "notes": [*notes.uninstall_notes(spec), *environment_notes],
        }

    async def _commit_uninstall(
        self, ref: str, spec: PluginSpec, change: OperationChange | None
    ) -> tuple[bool, ApplyResult | None, list[str]]:
        """Deregister ``spec``, drop its row, then finish ``change``.

        A failed deregister or row delete swaps ``change`` back and re-raises. Returns whether
        the manifest was written, the deregister's convergence result and the environment notes
        of the finish (:meth:`OperationChange.finish`).
        """
        try:
            manifest_written, reload_result = await self._deregister(spec)
        except Exception as deregister_error:
            await self._unwind(deregister_error, change=change, provides_undo=None, env_written=None)
            raise
        try:
            await self._store.delete(ref)
        except Exception as delete_error:
            await self._unwind(delete_error, change=change, provides_undo=None, env_written=None)
            raise
        return manifest_written, reload_result, [] if change is None else change.finish()

    async def _deregister(self, spec: PluginSpec) -> tuple[bool, ApplyResult | None]:
        """Persist the manifest without ``spec``'s entries and converge it the way this door converges.

        Returns whether the manifest was written and the convergence's result: ``None`` when
        nothing was written (every provide is env-selected) or when the door's convergence is
        the deployment's next start.
        """
        changed = remove_provides(self._svc().read_manifest_document(), spec)
        if changed or provides.has_manifest_provides(spec):
            # The write runs even when nothing changed now but the spec targets manifest
            # fields, so a prior run's failed deregister is re-attempted (idempotent).
            def mutator(document: dict[str, Any]) -> None:
                remove_provides(document, spec)

            persisted = await manifest_apply.persist_composed(self._svc(), mutator)
            return True, await self._converge(self._svc(), persisted)
        # A plugin whose provides are all env-selected wrote no manifest entry, so there is
        # nothing to deregister — no write, no convergence.
        return False, None
