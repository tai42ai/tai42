"""The install, uninstall, and preview flows of the marketplace installer.

:class:`_InstallFlow` orders ``preview`` / ``install`` / ``uninstall`` as
abort-on-any-step sequences with an explicit reverse unwind, delegating each concern to
a sibling module (``locks``, ``resolve``, ``package_ops``, ``manifest_apply``,
``env_apply``, ``route_preflight``, ``notes``, ``unwind``). It inherits the shared config,
pip and prefix seams from :class:`~tai42_skeleton.marketplace.installer_base._InstallerBase`.
"""

from __future__ import annotations

import copy
from typing import Any

from tai42_contract.plugins import PluginSpec

from tai42_skeleton.app.route_registry import CrossOwnerRouteCollisionError
from tai42_skeleton.config.service import ApplyResult
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
from tai42_skeleton.marketplace.errors import InstallStateError, ManifestCollisionError
from tai42_skeleton.marketplace.installer_base import _InstallerBase
from tai42_skeleton.marketplace.manifest_patch import apply_provides, collisions, remove_provides
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
        """Land the package (or no-op for a descriptor-only spec).

        Env-shadow guard, pip install, plugin migrations; on migration failure unwind the
        just-installed package and re-raise. Returns the pip output, or ``None`` when nothing was
        installed.
        """
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
        """Commit the manifest+env+row transactionally.

        Apply provides+env, write the attribution row, post-record remount reload; unwind on any
        failure.

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
        # ``env_restore`` captures the PRIOR store value of every key written, so the
        # unwind restores each rather than blind-deleting it.
        env_written = env_apply.env_to_write(env)
        env_restore: dict[str, str] = {}
        try:

            def mutator(document: dict[str, Any]) -> None:
                apply_provides(document, spec)

            try:
                apply_result = await env_apply.apply_provides_change(
                    self._svc(),
                    spec,
                    mutator,
                    env=env,
                    secret_keys=secret_keys,
                    env_to_write=env_written,
                    env_restore=env_restore,
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
                return await self._svc().apply_replace(self._cm().read_manifest_preserved())
            manifest_persisted = True
            await self._record_install_row(ref, pinned_version, spec, source, resolved, resolved_mounts)
            row_persisted = True
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
            # The row is written only after the manifest reload, so it needs dropping only on
            # the collision-recovery path, where it precedes the authoritative remount.
            if row_persisted:
                await self._store.delete(ref)
            raise
        return apply_result

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
