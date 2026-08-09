"""The marketplace installer — resolve, install, patch, reload, attribute.

:class:`Installer` performs the three environment-mutating flows the marketplace
surface exposes — ``install``, ``uninstall``, ``update`` — each as an ordered
sequence with abort-on-any-step and an explicit reverse unwind, so a mid-flight
failure never leaves the app half-registered.

Every collaborator is injected (the registry client, the pip runner, the
attribution store, the config-mutation pipeline, the fleet lock, and the config
manager), so a test can fake each seam. The defaults are the real ones.

The one manifest write door
---------------------------
Every manifest edit crosses :class:`~tai42_skeleton.config.service.ConfigService`:
:meth:`~tai42_skeleton.config.service.ConfigService.apply_change` takes a mutator
that patches the manifest in place, and the pipeline validates the RESOLVED
projection of the compose (``!ENV`` markers materialized, so a marker on a
non-string field validates against its resolved value) — a compose the resolved
schema or the backend-needs-bus invariant rejects raises loudly inside the
transaction instead of corrupting the stored manifest, and the marketplace maps
that fault to a typed :class:`~tai42_skeleton.marketplace.errors.ManifestComposeError`
(see :meth:`_apply_composed`). The pipeline owns the transaction, the persist, the
local reload through the process reload gate, and the confirmed fleet broadcast on
the worker bus. The reload re-reads the
persisted manifest and re-imports every manifest-named module, so a package
pip-installed moments earlier is importable on the very next reload with no
process restart. An unwind restores the pre-change manifest through the same
pipeline (:meth:`~tai42_skeleton.config.service.ConfigService.apply_replace`), so a
rollback reload+broadcast reaches the fleet too.

Concurrency
-----------
Two layers, each honest about what it covers:

- The fleet-wide **PostgreSQL advisory lock** (:func:`_fleet_lock`) is the
  correctness layer for the VENV: it serializes marketplace operations across
  every worker PROCESS, so two pips never mutate one venv at once. It is held
  across the ENTIRE operation (pip run, manifest apply, attribution write). The
  manifest change's own cross-process atomicity is owned by the pipeline's
  transaction, not this lock.
- The per-worker :data:`_operation_lock` is the UX fast path: a second request
  arriving in the SAME worker while an operation runs is refused immediately with
  a retriable :class:`OperationInProgressError` instead of queueing for minutes.
  It is a fast path only — never the mutual-exclusion story, since each worker is
  a separate process with its own copy of it.

The honest boundary: the fleet lock serializes MARKETPLACE operations against
each other so no two overlap a single venv; the manifest pipeline serializes the
manifest write itself across the whole fleet.

The pip transaction boundary
----------------------------
An unwind (or an uninstall) fully reverts the manifest, the attribution row, and
the live registration, but the venv is only as transactional as pip itself:
``pip uninstall`` removes just the named distribution, so dependencies pip
installed or upgraded in place during the attempt remain. Only skeleton state
reverts completely; that caveat rides the install/update response text.

The prefix env-shadow rule
--------------------------
With a prefix configured, the prefix sits at the END of ``sys.path`` so the
environment shadows it for any package present in both. A prefix install/update
whose distribution the environment already provides at the SAME version is a
harmless no-op (nothing lands in the prefix; the manifest wiring is the value) and
proceeds; at a DIFFERENT version it is refused loudly before any package,
manifest, or attribution state changes
(:meth:`_guard_env_shadow`), because the prefix copy would never import. Uninstall
mirrors this: a distribution absent from the prefix but present in the environment
is the env-shadowed no-op install's footprint, so the manifest is stripped and the
row dropped with no file removal (:meth:`_remove_package` returns ``False`` and the
fact rides the response notes); absent from BOTH is a loud failure. Removal stays
ordered strip → remove → drop-row so a genuine removal fault (a ``RECORD`` escape,
a disk error) halts with the row intact for a fixed re-run — the tolerant path, not
a reorder, is what closes the dangling-row trap for the env-shadowed case.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.metadata
import logging
import os
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import ValidationError
from tai42_contract.app import tai42_app
from tai42_contract.plugins import KIND_MANIFEST_BINDINGS, PluginItem, PluginSpec
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import apply_migrations, component_store_settings
from tai42_kit.utils.data.env_markers import scan_env_marker_refs

from tai42_skeleton.app.boot_rules import BackendNeedsBusError
from tai42_skeleton.config.service import ApplyResult, ConfigService
from tai42_skeleton.db import SKELETON_COMPONENT, plugin_migration_entry
from tai42_skeleton.marketplace.client import RegistryClient
from tai42_skeleton.marketplace.compat import running_contract_version, update_targets
from tai42_skeleton.marketplace.errors import (
    ContractIncompatibleError,
    EnvironmentShadowError,
    InstallEnvError,
    InstallStateError,
    InstallUnwindError,
    LocalStateError,
    MalformedRefError,
    ManifestCollisionError,
    ManifestComposeError,
    OperationInProgressError,
    PluginPrefixError,
    RegistryResponseError,
    VersionRefusedError,
)
from tai42_skeleton.marketplace.manifest_patch import apply_provides, collisions, remove_provides
from tai42_skeleton.marketplace.pip import (
    PipRunner,
    ensure_pip_available,
    fetch_verified_artifact,
    install_args,
    run_pip,
    uninstall_args,
)
from tai42_skeleton.marketplace.prefix import (
    configured_prefix,
    ensure_prefix_writable,
    environment_distribution_version,
    prefix_has_distribution,
    uninstall_from_prefix,
)
from tai42_skeleton.marketplace.store import InstallRecord, MarketplaceInstallStore
from tai42_skeleton.operations._broadcast import FleetBroadcastError, fleet_fanout

logger = logging.getLogger(__name__)

# The operator's "treat these env keys as secret" marks (comma-separated key names),
# the same var ``set_mcp_secret_env`` appends to so the Studio editor masks the value.
_SECRET_MARKS_VAR = "TAI_ENV_SECRET_KEYS"

# Per-worker fast-path lock: a second same-worker operation is refused
# immediately rather than queued. NOT the correctness layer — each uvicorn worker
# is a separate process with its own instance of this.
_operation_lock = asyncio.Lock()

# Fixed key for the fleet-wide session advisory lock that serializes marketplace
# operations. Session- and transaction-scoped advisory locks share ONE key space
# in PostgreSQL, and this feature's DSN commonly targets the same database as the
# connector store (which takes ``0x7461695F636F6E6E`` for category creation), so
# this MUST be a distinct value or the two would block against each other across
# the fleet. "tai42_mktp" as ASCII bytes; the high byte 0x74 keeps it a positive
# bigint.
_MARKETPLACE_LOCK_KEY = 0x7461695F6D6B7470  # "tai42_mktp"


def _reload_report(result: ApplyResult) -> dict[str, Any]:
    """The manifest apply's local reload result with the standard fleet fan-out
    summary folded in under ``fanout`` — the ``reload`` field every
    install/uninstall/update response carries. Reuses the shared
    :func:`~tai42_skeleton.operations._broadcast.fleet_fanout` shaper so the key AND the
    value shape match every other fleet-report-embedding writer (backup, the
    operations ``apply_response``)."""
    return {**result.local, "fanout": fleet_fanout(result.fleet)}


@asynccontextmanager
async def _fleet_lock() -> AsyncIterator[None]:
    """Hold the fleet-wide marketplace advisory lock for the context body.

    Opens a DEDICATED one-shot ``PostgresClient`` (``fresh=True``, pool bounds
    pinned to 1) rather than a shared-pool checkout: the kit's fresh path closes
    the dedicated pool and its connection on ANY exit — normal return, exception,
    or a ``CancelledError`` from a client disconnect during the minutes-long pip
    run — so the session-scoped lock releases deterministically with the
    connection. A shared checkout would only RETURN the connection on exit (never
    close it), and a cancellation skipping the explicit unlock would put a
    still-locked session back in the shared pool, wedging the fleet with 503s
    until restart.

    The connection is set to autocommit BEFORE the try-lock: psycopg defaults
    autocommit off, so otherwise the lock SELECT would open a transaction idling
    for the whole pip run, and a managed-PG ``idle_in_transaction_session_timeout``
    would kill the session mid-install and silently drop the lock. Autocommit
    closes that hole but NOT the idle-SESSION one: a managed-PG
    ``idle_session_timeout`` or a load-balancer idle drop can still kill the whole
    session mid-pip and release the lock while pip is still mutating the venv — an
    ACCEPTED residual risk whose mitigation is to disable ``idle_session_timeout``
    (and keep TCP keepalives on) for this DSN so a long pip run never trips a
    cutoff.

    ``pg_try_advisory_lock`` returning false means another worker holds it →
    :class:`OperationInProgressError` (retriable 503). The ``pg_advisory_unlock``
    in the ``finally`` is the polite release; the connection close is the
    guarantee, so its failure on a possibly-dead connection is logged and
    suppressed, never allowed to mask the body's original error. A crashed or
    cancelled worker therefore cannot leave the fleet wedged.

    Side benefit of the dedicated client: a psycopg ``OperationalError`` raised
    inside the body stays on this client and cannot evict the SHARED pool through
    the kit's disconnection rewrap.
    """
    kwargs = component_store_settings(SKELETON_COMPONENT).client_kwargs()
    kwargs["min_size"] = 1
    kwargs["max_size"] = 1
    async with (
        client_ctx(PostgresClient, fresh=True, **kwargs) as pool,
        pool.connection() as conn,
    ):
        await conn.set_autocommit(True)
        async with conn.cursor() as cur:
            await cur.execute("SELECT pg_try_advisory_lock(%s)", (_MARKETPLACE_LOCK_KEY,))
            row = await cur.fetchone()
        if not (row and row[0]):
            raise OperationInProgressError("another marketplace operation is in progress; retry shortly")
        try:
            yield
        finally:
            try:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT pg_advisory_unlock(%s)", (_MARKETPLACE_LOCK_KEY,))
            except Exception:
                logger.warning(
                    "marketplace: advisory unlock failed; the connection close releases the lock", exc_info=True
                )


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
        fleet_lock: Callable[[], AbstractAsyncContextManager[None]] = _fleet_lock,
        config_manager: Any | None = None,
        prefix: str | None = None,
        prefix_uninstall: Callable[[str, str], None] = uninstall_from_prefix,
        prefix_ensure_writable: Callable[[str], None] = ensure_prefix_writable,
        prefix_has_dist: Callable[[str, str], bool] = prefix_has_distribution,
        env_dist_version: Callable[[str, str], str | None] = environment_distribution_version,
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

    # -- infrastructure -----------------------------------------------------

    def _cm(self) -> Any:
        """The live config manager (read seam), or the injected fake.

        Reached live per use because a reload swaps app internals; never cached
        across a reload — unless a fake was injected, in which case it is used
        throughout. Used only for the pre-flight manifest READS; every WRITE crosses
        :meth:`_svc`.
        """
        if self._config_manager is not None:
            return self._config_manager
        return tai42_app.config.config_manager

    def _svc(self) -> ConfigService:
        """The config-mutation pipeline, or the injected fake.

        Wired live per use (:meth:`ConfigService.from_app`) because a reload swaps
        app internals; every manifest write — forward and unwind — crosses it, so a
        write is always validated, persisted, locally reloaded, and broadcast.
        """
        if self._config_service is not None:
            return self._config_service
        return ConfigService.from_app()

    @asynccontextmanager
    async def _guard(self) -> AsyncIterator[None]:
        """Acquire the per-worker lock then the fleet lock, both for the body.

        Refuses immediately (no store/registry/pip/manifest call) when either
        lock is held elsewhere.
        """
        if _operation_lock.locked():
            raise OperationInProgressError("another marketplace operation is in progress; retry shortly")
        async with _operation_lock, self._fleet_lock():
            yield

    async def _apply_composed(self, mutator: Callable[[dict[str, Any]], None]) -> ApplyResult:
        """Apply a manifest mutation through the pipeline, mapping a composed-manifest
        fault to the typed compose error.

        The pipeline validates the RESOLVED projection of the mutated document (``!ENV``
        markers materialized) before it persists, so a marker on a non-string field
        (e.g. ``api_tools.expose_destructive``) validates against its resolved value —
        the marketplace does no schema validation of its own. Because the marketplace
        fully controls the composed document, a resolved-projection schema failure
        (:class:`~pydantic.ValidationError`) OR a registered-backend-without-a-bus refusal
        (:class:`~tai42_skeleton.app.boot_rules.BackendNeedsBusError`, which the whole-fleet
        boundary would otherwise let escape untyped) is a registry-spec + local fault,
        re-raised as :class:`ManifestComposeError` so the boundary attributes it a loud
        500 rather than a bare one. Either raises inside the transaction, so nothing
        persists. A resolved-secret leak (a ``ResolvedSecretError``) cannot arise here:
        the structural provides patch ingests no resolved secret value, so the pipeline's
        secret seal is a no-op."""
        try:
            return await self._svc().apply_change(mutator)
        except (ValidationError, BackendNeedsBusError) as exc:
            raise ManifestComposeError(f"the composed manifest is invalid: {exc}") from exc

    async def _pip_install(
        self, package: str, version: str, source: str, artifact_ref: str | None, sha256: str | None
    ) -> str:
        """Run the ``pip install`` for a pinned version and return its output.

        A pypi source pins ``package==version`` directly. A github source first
        downloads the registry-named artifact and verifies its sha256 through
        :func:`fetch_verified_artifact` into a temporary directory kept alive
        across the whole pip run (pip must read the local tarball before it is
        cleaned up), then installs that verified tarball — never a mutable
        ``git+url@tag`` clone. A checksum mismatch or fetch failure raises out of
        here with no pip call and no fallback.

        When a prefix is configured, ``--prefix`` targets it: pip resolves shared
        dependencies against the running environment and installs the plugin plus
        only its genuinely-new dependencies under the prefix (the image's packages
        are never duplicated there).
        """
        if source == "github":
            with tempfile.TemporaryDirectory() as tmp:
                verified = await fetch_verified_artifact(package, version, artifact_ref or "", sha256 or "", Path(tmp))
                return await self._pip_runner(install_args(package, version, source, verified, prefix=self._prefix))
        return await self._pip_runner(install_args(package, version, source, prefix=self._prefix))

    async def _run_plugin_migrations(self, spec: PluginSpec) -> None:
        """Apply the installed/upgraded plugin's schema migrations, when it declares
        a chain.

        A plugin with no ``migrations`` field owns no tables and is skipped — the
        feature is opt-in. The chain runs under the DDL-privileged migrator identity
        (the ``TAI_DB_*`` namespace ``tai db migrate`` uses), on a migration advisory
        lock DISTINCT from the marketplace lock this operation already holds, so the
        nested lock can never self-deadlock. Called AFTER the package lands and
        BEFORE the manifest patch, so a failure aborts the operation with the
        manifest untouched; applied files roll forward, so a re-run applies only the
        remainder.

        The wheel landed moments ago, so the import machinery is refreshed first —
        both the distribution→import-package resolution and the packaged migrations
        directory read must see the freshly installed files in this running process.
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

    async def _remove_package(self, package: str) -> bool:
        """Remove ``package``'s own distribution and report whether files were removed.

        Environment path (no prefix): ``pip uninstall`` — always removes, returns
        ``True``. Prefix path: remove from the prefix by its ``RECORD`` when present
        there (``True``); when ABSENT from the prefix but present in the ENVIRONMENT
        nothing ever landed in the prefix (an env-shadowed no-op install), so remove
        no files and return ``False`` — never a loud failure for a state the install
        rule made legitimate. Absent from BOTH the prefix and the environment is a
        loud :class:`PluginPrefixError`. Only the named distribution goes on every
        path; dependencies stay."""
        if self._prefix is None:
            await self._pip_runner(uninstall_args(package))
            return True
        if self._prefix_has_dist(package, self._prefix):
            self._prefix_uninstall(package, self._prefix)
            return True
        if self._env_dist_version(package, self._prefix) is not None:
            return False
        raise PluginPrefixError(
            f"{package!r} is installed in neither the plugin prefix {self._prefix} nor the environment; "
            "cannot remove it"
        )

    def _guard_env_shadow(self, package: str, pinned_version: str, prefix: str) -> None:
        """Refuse a prefix install/update the running environment would shadow.

        The prefix sits at the END of ``sys.path``, so a distribution the environment
        already provides wins for every version. Same version → the prefix install is
        a harmless no-op (the manifest wiring is the whole value), so proceed. A
        DIFFERENT environment version would silently hide the prefix copy → refuse
        loudly before any package, manifest, or attribution state changes,
        naming both versions."""
        env_version = self._env_dist_version(package, prefix)
        if env_version is not None and env_version != pinned_version:
            raise EnvironmentShadowError(
                f"the environment provides {package} {env_version}, which sits ahead of the plugin prefix on "
                f"sys.path and would shadow a prefix install of {pinned_version}; refusing to install a version "
                f"the environment will hide (re-pin to {env_version} or rebuild the image)"
            )

    # -- install ------------------------------------------------------------

    async def install(
        self,
        ref: str,
        version: str | None = None,
        *,
        env: dict[str, str] | None = None,
        secret_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        """Install a marketplace plugin, aborting and unwinding on any failure.

        Steps, in order: parse the ref and pre-flight local state (not already
        installed, pip present); resolve the pinned version via the registry (the
        ONE pinning call — critical-advisory refusal, spec validation, and
        contract-range compatibility included); collision pre-flight against the
        manifest BEFORE pip; ``pip install`` the locally-composed pin; patch the
        manifest through the one door and reload; write the attribution row.

        ``env`` / ``secret_keys`` are accepted only for an mcp-server install: an
        mcp entry's ``!ENV`` markers are satisfied by writing these values to the env
        store in the SAME combined transaction that writes the entry (never deferred).
        ``secret_keys`` marks the given keys secret (appended to ``TAI_ENV_SECRET_KEYS``).

        Each step past the pip install unwinds the applied steps in reverse on
        failure (env keys THIS install wrote deleted, manifest restored, live app
        converged back, pip uninstall) and re-raises; a failed unwind escalates to
        :class:`InstallUnwindError`. Only skeleton state fully reverts — see the
        pip-transaction caveat.
        """
        async with self._guard():
            return await self._install_locked(ref, version, env, secret_keys)

    async def _install_locked(
        self, ref: str, version: str | None, env: dict[str, str] | None, secret_keys: list[str] | None
    ) -> dict[str, Any]:
        ns, name = _parse_ref(ref)
        existing = await self._store.get(ref)
        if existing is not None:
            raise InstallStateError(f"{ref} is already installed (version {existing.version}); use update")
        ensure_pip_available()
        if self._prefix is not None:
            # A set-but-unwritable prefix fails HERE, before the registry call and
            # any state change — never a silent fall back to the environment.
            self._prefix_ensure_writable(self._prefix)

        resolved = await self._registry.resolve(ns, name, version)
        spec, source = self._prepare_resolved(resolved)
        pinned_version = _require(resolved, "version")

        cm = self._cm()
        found = collisions(dict(cm.read_manifest_preserved()), spec)
        if found:
            raise ManifestCollisionError("; ".join(found))

        if self._prefix is not None:
            # A prefix install of a version the environment already shadows at a
            # DIFFERENT version is refused here, before any package/manifest/store write.
            self._guard_env_shadow(spec.package, pinned_version, self._prefix)

        # Step 3 — pip install (github: fetch + verify + install the local
        # tarball). A failure propagates as-is: the venv may hold a
        # partially-resolved state pip itself left, and pip's own transaction
        # handling is the boundary.
        pip_output = await self._pip_install(
            spec.package, pinned_version, source, resolved.get("artifact_ref"), resolved.get("sha256")
        )

        # Step 3.5 — run the plugin's schema migrations, when it declares a chain.
        # The package has landed but the manifest is not yet patched, so the plugin
        # is inert: a migration failure aborts the install with the manifest
        # untouched (no half-active plugin), unwinding the just-installed package.
        try:
            await self._run_plugin_migrations(spec)
        except Exception as migration_error:
            await self._unwind_install(migration_error, package=spec.package, saved_manifest=None)
            raise

        saved_manifest = copy.deepcopy(cm.read_manifest_preserved())
        manifest_persisted = False
        # The env keys THIS install writes to the store (deployment/already-set vars are
        # omitted, so no pre-existing marker can reference a key this install owns).
        # ``env_restore`` captures the PRIOR store value of EVERY key this install writes
        # (the value keys AND the ``TAI_ENV_SECRET_KEYS`` marks var) — populated by the
        # combined pipeline under its C9d lock, and consumed by the unwind to restore each
        # key to its prior value (delete when it was absent), diffed against the live store
        # so a pre-persist refusal reverts nothing.
        env_written = self._env_to_write(env)
        env_restore: dict[str, str] = {}
        try:
            # Step 4 — manifest patch through the pipeline: the mutator applies the
            # provides, the pipeline validates the resolved compose (a compose the
            # resolved schema or the backend-needs-bus invariant rejects raises inside
            # the transaction so nothing persists — mapped to the typed compose error),
            # persists, reloads locally, and broadcasts. An mcp-server spec routes
            # through the COMBINED env+manifest pipeline so the entry is written exactly
            # once alongside its required env values.
            def mutator(document: dict[str, Any]) -> None:
                apply_provides(document, spec)

            apply_result = await self._apply_provides_change(
                spec, mutator, env=env, secret_keys=secret_keys, env_to_write=env_written, env_restore=env_restore
            )
            manifest_persisted = True
            # Step 5 — attribution write, stamped with the core versions doing it.
            repo_url, tag, artifact_ref, sha256 = _pin_provenance(resolved, source)
            contract_version, skeleton_version = _core_version_stamps()
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
            )
        except Exception as step_error:
            # The pipeline aborts a failed mutation with nothing persisted, so the
            # manifest needs restoring only when the change actually landed — the
            # attribution step failed after a good apply, or the apply's local reload
            # failed AFTER its persist committed (:class:`FleetBroadcastError`).
            persisted = manifest_persisted or isinstance(step_error, FleetBroadcastError)
            await self._unwind_install(
                step_error,
                package=spec.package,
                saved_manifest=saved_manifest if persisted else None,
                env_restore=env_restore,
            )
            raise

        return {
            "ref": ref,
            "version": pinned_version,
            "package": spec.package,
            "advisories": _require_list(resolved, "advisories"),
            "notes": _install_notes(spec),
            "reload": _reload_report(apply_result),
            "pip_output": pip_output,
        }

    async def _unwind_install(
        self,
        step_error: Exception,
        *,
        package: str,
        saved_manifest: dict[str, Any] | None,
        env_restore: dict[str, str] | None = None,
    ) -> None:
        """Reverse an install whose Step 4/5 failed: revert THIS install's env write
        (the combined pipeline's C9a leaves it standing as an inert orphan, but the
        installer-unwind layer above it restores exactly this install's contribution —
        each key it wrote back to its captured PRIOR store value, so a newly-created key
        is deleted, an overwritten pre-existing key keeps its operator value, and the
        ``TAI_ENV_SECRET_KEYS`` marks var keeps an operator's OTHER marks), restore the
        manifest through the pipeline (converging the live app and the fleet back) when
        the change had persisted, then pip uninstall the freshly-installed package. A
        failed sub-step escalates to :class:`InstallUnwindError`; otherwise the caller
        re-raises the original step error."""
        try:
            if saved_manifest is not None:
                await self._svc().apply_replace(saved_manifest)
            # Revert AFTER the manifest restore so no restored marker references a
            # key being dropped (on the orphan path the manifest never persisted, so
            # the live manifest already names none of these keys).
            await self._revert_env_write(env_restore)
        except Exception as unwind_error:
            raise InstallUnwindError(step_error, unwind_error) from step_error
        try:
            await self._remove_package(package)
        except Exception as unwind_error:
            raise InstallUnwindError(step_error, unwind_error) from step_error

    @staticmethod
    def _env_to_write(env: dict[str, str] | None) -> dict[str, str]:
        """The subset of ``env`` this install actually writes to the store: keys the
        process env does not already provide. A deployment/already-set var is omitted
        (its marker resolves from the live env), so no pre-existing marker can end up
        referencing a key this install owns."""
        return {key: value for key, value in (env or {}).items() if key not in os.environ}

    async def _revert_env_write(self, env_restore: dict[str, str] | None) -> None:
        """Revert THIS install's env contribution through the ConfigService env door,
        restoring every key this install wrote to the PRIOR store value ``prepare``
        captured (its exact prior string, or ``""`` to delete a key that was ABSENT
        before) — never a blind delete, so a key this install merely OVERWROTE keeps its
        operator value and the ``TAI_ENV_SECRET_KEYS`` marks var keeps an operator's OTHER
        marks. The change set is DIFFED against the live store, so when nothing actually
        persisted (the pre-persist dangling refusal) it is EMPTY and no env change fires —
        no fleet broadcast on a no-op. Runs AFTER any manifest restore, so no restored
        marker references a dropped key."""
        if not env_restore:
            return
        try:
            current = self._cm().read_env()
        except FileNotFoundError:
            current = {}
        changes = {key: value for key, value in env_restore.items() if current.get(key, "") != value}
        if changes:
            await self._svc().apply_env_change(changes)

    async def _apply_provides_change(
        self,
        spec: PluginSpec,
        mutator: Callable[[dict[str, Any]], None],
        *,
        env: dict[str, str] | None,
        secret_keys: list[str] | None,
        env_to_write: dict[str, str],
        env_restore: dict[str, str],
    ) -> ApplyResult:
        """Persist a provides patch through the pipeline.

        An mcp-server spec routes through the COMBINED env+manifest pipeline
        (:meth:`~tai42_skeleton.config.service.ConfigService.apply_env_and_change`): the
        supplied env values land in the store and the ``{title, config}`` entry is
        written in ONE unit, so the entry is written exactly once (the standalone
        ``apply_provides`` never runs a second time). An unsatisfied required marker is
        the pipeline's dangling refusal, surfaced as :class:`InstallEnvError` naming each
        missing var + json-pointer BEFORE anything persists. The C9a non-atomicity
        contract is preserved (a manifest-persist failure leaves the env write standing
        and raises ``OrphanEnvWriteError``); the installer-unwind above reverts it.

        Under the pipeline's C9d lock (against the SAME stored-env snapshot the write
        derives from) this records the PRIOR store value of EVERY key it will write into
        ``env_restore`` — the value keys AND, when ``secret_keys`` are supplied, the
        ``TAI_ENV_SECRET_KEYS`` marks var it MERGES into (each key's exact prior string, or
        ``""`` when the key was absent). The unwind restores exactly those, so it reverts
        this install's contribution without deleting an operator's pre-existing store value
        or clobbering their other secret marks.

        Every other spec routes through ``apply_change``; ``env`` / ``secret_keys`` are
        meaningless there and supplying them is a loud input error."""
        if not _mcp_entry_items(spec):
            if env or secret_keys:
                raise InstallEnvError(
                    f"env / secret_keys are only accepted for an mcp-server install; {spec.ref} provides "
                    "no mcp-server item"
                )
            return await self._apply_composed(mutator)

        marks = list(secret_keys or [])

        async def prepare(stored: dict[str, str]) -> tuple[dict[str, str], Callable[[dict[str, Any]], None]]:
            changes = dict(env_to_write)
            if marks:
                # Append to the marks read from the STORED env (never the settings
                # cache, stale until a reload), mirroring ``set_mcp_secret_env``.
                prior = stored.get(_SECRET_MARKS_VAR)
                existing = [m.strip() for m in (prior or "").split(",") if m.strip()]
                changes[_SECRET_MARKS_VAR] = ",".join(dict.fromkeys([*existing, *marks]))
            # Capture the PRIOR store value of EVERY key this install writes (value keys
            # AND the marks var) from the STORED snapshot: its exact prior string when
            # present, else ``""`` (the key was absent). The unwind restores each to this,
            # so an overwritten pre-existing store key is never blind-deleted and the marks
            # var keeps an operator's OTHER marks.
            for key in changes:
                env_restore[key] = stored.get(key, "")
            return changes, mutator

        try:
            return await self._svc().apply_env_and_change(prepare, manifest_pointer="mcp")
        except (ValidationError, BackendNeedsBusError) as exc:
            raise ManifestComposeError(f"the composed manifest is invalid: {exc}") from exc
        except ValueError as exc:
            # A dangling-marker refusal (each missing required var + json-pointer) or
            # another env-boundary refusal — raised before anything persisted.
            raise InstallEnvError(str(exc)) from exc

    # -- uninstall ----------------------------------------------------------

    async def uninstall(self, ref: str) -> dict[str, Any]:
        """Uninstall a marketplace-installed plugin — convergent and
        registry-free.

        Order: pip pre-flight (a pipless environment fails HERE, before the
        manifest is touched); read the stored record (unknown ref → not-installed
        state error); reconstruct the installed ``PluginSpec`` from LOCAL truth
        (a corrupt row → :class:`LocalStateError`); unpatch the manifest through
        the one door and reload to CONVERGENCE (a re-run whose manifest is already
        stripped still reloads when the spec targets manifest fields, so a prior
        run's failed deregister reload is re-attempted; the reload is skipped only
        for a plugin with no manifest provides — all env-selected); pip uninstall;
        drop the attribution row.

        There is no unwind-to-installed: re-running forward is the recovery, and
        every failure states exactly which steps remain. Only skeleton state
        reverts fully — see the pip-transaction caveat.
        """
        async with self._guard():
            return await self._uninstall_locked(ref)

    async def _uninstall_locked(self, ref: str) -> dict[str, Any]:
        if self._prefix is None:
            # The environment path shells ``pip uninstall``; the prefix path removes
            # the plugin's files by its RECORD and needs no pip.
            ensure_pip_available()
        row = await self._store.get(ref)
        if row is None:
            raise InstallStateError(f"{ref} is not installed", not_installed=True)
        spec = _spec_from_row(row)

        cm = self._cm()
        reload_result: dict[str, Any] | None
        changed = remove_provides(dict(cm.read_manifest_preserved()), spec)
        if changed or _has_manifest_provides(spec):
            # Converge the live registration to the stripped manifest through the
            # pipeline BEFORE removing the package. The apply runs even when nothing
            # changed now but the spec targets manifest fields: a prior run may have
            # persisted the stripped manifest and then failed the deregister reload
            # (the store row still exists), leaving the tools live — and a re-run MUST
            # re-attempt the persist+reload so it does not pip uninstall + drop the row
            # while the tools stay registered. The mutator re-strips the seam's own
            # document (idempotent when it was already clean).
            def mutator(document: dict[str, Any]) -> None:
                remove_provides(document, spec)

            reload_result = _reload_report(await self._apply_composed(mutator))
        else:
            # A plugin whose provides are all env-selected wrote no manifest
            # entry, so there is nothing to deregister — no apply, no reload.
            reload_result = None

        # A failure here leaves the app converged but the package present;
        # re-running uninstall (step 3 now a no-op) completes the removal.
        removed = await self._remove_package(spec.package)
        await self._store.delete(ref)

        notes = _uninstall_notes(spec)
        if self._prefix is not None and not removed:
            # The env-shadowed no-op install landed nothing in the prefix; the
            # uninstall stripped the manifest and dropped the row but removed no
            # files. Surface that in the result, never only a log line.
            env_version = self._env_dist_version(spec.package, self._prefix)
            notes.append(
                f"{spec.package!r} was absent from the plugin prefix; the environment provides it at "
                f"{env_version} — removed no files, only deregistered and dropped the attribution record"
            )
        return {"ref": ref, "uninstalled": True, "reload": reload_result, "notes": notes}

    # -- update -------------------------------------------------------------

    async def update(
        self,
        ref: str,
        version: str | None = None,
        *,
        env: dict[str, str] | None = None,
        secret_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update an installed plugin to a newer (or named) version — an install
        of the new version with the same pre-flights, in one manifest
        read-modify-write.

        Order: parse the ref (a malformed ref is the caller's 400, checked before
        the store read so an unparseable ref is never a phantom 404); pip
        pre-flight; read the stored record (unknown ref →
        not-installed state error) for the old spec and pin; resolve the target
        (kill/advisory/spec-validation/contract checks; target == installed →
        state error); collision pre-flight for the NEW spec against the manifest
        with the OLD spec's entries removed in memory (a rename inside one plugin
        must not self-collide); ``pip install`` the new pin (upgrades in place);
        one pipeline apply removing the old entries and applying the new (persist +
        reload + broadcast); upsert the attribution row.

        A Step 5/6 failure unwinds in order — reinstall the OLD pin (a github old pin
        re-fetched and re-verified from the stored row's artifact_ref + sha256), then
        restore the manifest through the pipeline (the old wheel must be back before
        the restore's reload, or reloading the old manifest against the new wheel
        fails when a module moved) — and a failed sub-step escalates to
        :class:`InstallUnwindError`. Only skeleton state reverts fully — see the
        pip-transaction caveat.
        """
        async with self._guard():
            return await self._update_locked(ref, version, env, secret_keys)

    async def _update_locked(
        self, ref: str, version: str | None, env: dict[str, str] | None, secret_keys: list[str] | None
    ) -> dict[str, Any]:
        ns, name = _parse_ref(ref)
        ensure_pip_available()
        if self._prefix is not None:
            self._prefix_ensure_writable(self._prefix)
        row = await self._store.get(ref)
        if row is None:
            raise InstallStateError(f"{ref} is not installed", not_installed=True)
        old_spec = _spec_from_row(row)

        resolved = await self._registry.resolve(ns, name, version)
        new_spec, source = self._prepare_resolved(resolved)
        pinned_version = _require(resolved, "version")
        if pinned_version == row.version:
            raise InstallStateError(f"{ref} is already at {pinned_version}")

        cm = self._cm()
        preview = dict(cm.read_manifest_preserved())
        remove_provides(preview, old_spec)
        found = collisions(preview, new_spec)
        if found:
            raise ManifestCollisionError("; ".join(found))

        if self._prefix is not None:
            # The environment shadows the prefix, so it cannot hold a version the
            # environment already provides at a different one — refused before any
            # state change, and before the old prefix wheel is removed below.
            self._guard_env_shadow(new_spec.package, pinned_version, self._prefix)

        # Step 4 — install the new pin (github: fetch + verify + install the local
        # tarball). On the environment path pip upgrades in place and a failure
        # propagates as-is (nothing skeleton-side has changed yet; pip's own
        # transaction is the boundary). On the prefix path pip cannot upgrade a
        # distribution it does not see on its own path, so the old version is
        # removed first and reinstalled if the new install then fails — a failed
        # step 4 never leaves the prefix plugin-less, and a failed restore of the
        # old pin there escalates to a loud unwind error.
        if self._prefix is not None:
            self._prefix_uninstall(old_spec.package, self._prefix)
            try:
                pip_output = await self._pip_install(
                    new_spec.package, pinned_version, source, resolved.get("artifact_ref"), resolved.get("sha256")
                )
            except Exception as install_error:
                try:
                    await self._pip_install(old_spec.package, row.version, row.source, row.artifact_ref, row.sha256)
                except Exception as restore_error:
                    raise InstallUnwindError(install_error, restore_error) from install_error
                raise
        else:
            pip_output = await self._pip_install(
                new_spec.package, pinned_version, source, resolved.get("artifact_ref"), resolved.get("sha256")
            )

        # Run the new version's schema migrations before the manifest patch, the
        # same ordering install uses: the new wheel has landed but is not yet
        # active, so a migration failure unwinds to the OLD pin with the manifest
        # untouched, never a half-migrated active plugin.
        try:
            await self._run_plugin_migrations(new_spec)
        except Exception as migration_error:
            await self._unwind_update(
                migration_error,
                old_package=old_spec.package,
                new_package=new_spec.package,
                row=row,
                saved_manifest=None,
            )
            raise

        saved_manifest = copy.deepcopy(cm.read_manifest_preserved())
        manifest_persisted = False
        # The env keys THIS update writes to the store. A new version adding a required
        # marker is loudly refused unless its value is supplied here (the dangling
        # refusal → InstallEnvError, below). ``env_restore`` captures the PRIOR store
        # value of every key this update writes (value keys AND the ``TAI_ENV_SECRET_KEYS``
        # marks var) so the unwind restores each rather than blind-deleting it.
        env_written = self._env_to_write(env)
        env_restore: dict[str, str] = {}
        try:
            # Step 5 — one pipeline apply: the mutator removes the old provides and
            # applies the new; the pipeline validates the resolved compose (mapped to
            # the typed compose error on a fault), persists, reloads, and broadcasts.
            # An mcp-server new spec routes through the combined env+manifest pipeline.
            def mutator(document: dict[str, Any]) -> None:
                remove_provides(document, old_spec)
                apply_provides(document, new_spec)

            apply_result = await self._apply_provides_change(
                new_spec,
                mutator,
                env=env,
                secret_keys=secret_keys,
                env_to_write=env_written,
                env_restore=env_restore,
            )
            manifest_persisted = True
            # Step 6 — attribution upsert, stamped with the core versions doing it.
            repo_url, tag, artifact_ref, sha256 = _pin_provenance(resolved, source)
            contract_version, skeleton_version = _core_version_stamps()
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
            )
        except Exception as step_error:
            persisted = manifest_persisted or isinstance(step_error, FleetBroadcastError)
            await self._unwind_update(
                step_error,
                old_package=old_spec.package,
                new_package=new_spec.package,
                row=row,
                saved_manifest=saved_manifest if persisted else None,
                env_restore=env_restore,
            )
            raise

        return {
            "ref": ref,
            "version": pinned_version,
            "package": new_spec.package,
            "advisories": _require_list(resolved, "advisories"),
            "notes": _install_notes(new_spec),
            "reload": _reload_report(apply_result),
            "pip_output": pip_output,
        }

    async def _unwind_update(
        self,
        step_error: Exception,
        *,
        old_package: str,
        new_package: str,
        row: InstallRecord,
        saved_manifest: dict[str, Any] | None,
        env_restore: dict[str, str] | None = None,
    ) -> None:
        """Reverse an update whose Step 5/6 failed: revert THIS update's env write (every
        key it wrote restored to its captured PRIOR store value — a newly-created key
        deleted, an overwritten one kept, the ``TAI_ENV_SECRET_KEYS`` marks var back to its
        prior value), reinstall the OLD pin, then restore the manifest through the
        pipeline (when the change had persisted).
        The old wheel goes back BEFORE the restore's reload so the old manifest never
        loads against the new wheel. A github old pin is reinstalled through the SAME
        fetch-and-verify path as a forward install, using the stored row's
        ``artifact_ref`` + ``sha256`` — the old version's integrity is re-checked,
        never cloned from a mutable tag. On the prefix path the NEW version's files
        are removed first, or the old and new dist-info would coexist in the prefix.
        A failed sub-step (including an integrity mismatch on the old artifact)
        escalates to :class:`InstallUnwindError`."""
        try:
            if self._prefix is not None:
                self._prefix_uninstall(new_package, self._prefix)
            await self._pip_install(old_package, row.version, row.source, row.artifact_ref, row.sha256)
            if saved_manifest is not None:
                await self._svc().apply_replace(saved_manifest)
            # Revert the env write this update made AFTER the old manifest is back, so
            # the restored old entry never references a dropped key.
            await self._revert_env_write(env_restore)
        except Exception as unwind_error:
            raise InstallUnwindError(step_error, unwind_error) from step_error

    # -- upgrade-all --------------------------------------------------------

    async def upgrade_all(self) -> list[dict[str, Any]]:
        """Upgrade every installed plugin to its latest COMPATIBLE version and
        return a complete per-ref report.

        Runs the whole batch under ONE hold of the per-worker + fleet locks —
        never interleaving with another marketplace operation — and each ref
        gets exactly one report entry ``{ref, outcome, detail}``:

        * ``upgraded`` — a newer compatible version existed and the ordinary
          update flow (same pre-flights, same unwind) moved onto it;
        * ``up-to-date`` — the installed version already IS the latest
          compatible one (a newer-but-incompatible version is named in the
          detail, never silently omitted);
        * ``no-compatible-version`` — no published version supports the running
          contract at all (a loud failure entry: the plugin runs only by grace
          of its quarantine/compat status, and nothing here can fix it);
        * ``failed`` — the probe or the update flow raised; the error is logged
          with its traceback and its message becomes the detail.

        A per-ref failure NEVER aborts the batch — the report is complete by
        construction, which is the whole point of a fleet-wide upgrade pass.
        """
        async with self._guard():
            return await self._upgrade_all_locked()

    async def _upgrade_all_locked(self) -> list[dict[str, Any]]:
        contract = running_contract_version()
        report: list[dict[str, Any]] = []
        for row in await self._store.list_installed():
            report.append(await self._upgrade_one(row, contract))
        return report

    async def _upgrade_one(self, row: InstallRecord, contract: str) -> dict[str, Any]:
        """One ref's upgrade attempt → its report entry. The target is picked
        from the registry's version rows (published + contract-compatible, the
        same computation the installed listing's ``update_available`` serves,
        so this upgrades exactly what that listing advertises); the move itself
        is the ordinary update flow against the picked pin."""
        ns, name = _parse_ref(row.ref)
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
            result = await self._update_locked(row.ref, targets.latest_compatible, None, None)
            return {"ref": row.ref, "outcome": "upgraded", "detail": f"{row.version} -> {result['version']}"}
        except Exception as exc:
            # The explicit per-ref recovery path: logged with the traceback and
            # reported, so one broken ref never hides the rest of the batch.
            logger.exception("upgrade-all: upgrading %s failed", row.ref)
            return {"ref": row.ref, "outcome": "failed", "detail": str(exc)}

    # -- shared resolve handling -------------------------------------------

    def _prepare_resolved(self, resolved: dict[str, Any]) -> tuple[PluginSpec, str]:
        """Validate and vet a resolve response, returning ``(spec, source)``.

        Refuses a non-withdrawn critical advisory, validates the shipped
        ``PluginSpec`` (a malformed spec is a registry-data fault → 502, never a
        caller 400), requires the github artifact provenance (repository_url + tag
        for display, artifact_ref + sha256 for the verified fetch), and checks the
        plugin's ``contract_range`` against the installed ``tai42-contract`` version.
        """
        for advisory in _require_list(resolved, "advisories"):
            if advisory.get("withdrawn_at") is None and advisory.get("severity") == "critical":
                summary = advisory.get("summary", "no summary")
                raise VersionRefusedError(f"a critical advisory affects this version: {summary}")

        try:
            spec = PluginSpec.model_validate(_require(resolved, "spec"))
        except ValidationError as exc:
            raise RegistryResponseError(f"registry served an invalid plugin spec: {exc}", status=None) from exc

        source = _require(resolved, "source")
        if source == "github":
            if not resolved.get("repository_url") or not resolved.get("tag"):
                raise RegistryResponseError(
                    "registry resolve response for a github source is missing repository_url or tag",
                    status=None,
                )
            if not resolved.get("artifact_ref") or not resolved.get("sha256"):
                raise RegistryResponseError(
                    "registry resolve response for a github source is missing artifact_ref or sha256",
                    status=None,
                )
        elif source != "pypi":
            raise RegistryResponseError(f"registry returned an unknown install source {source!r}", status=None)

        self._check_contract(_require(resolved, "contract_range"))
        return spec, source

    def _check_contract(self, contract_range: str) -> None:
        """Require the installed ``tai42-contract`` version to satisfy the plugin's
        ``contract_range``.

        ``prereleases=True`` so a dev-versioned tai42-contract (an editable checkout
        reporting e.g. ``0.5.0.dev3``) inside the range still passes — a developer
        environment is not spuriously refused. A malformed range is registry data
        → 502, never a caller 400.
        """
        # ``contract_range`` is a non-null string by construction — ``_require``
        # rejects a null and the registry client's resolve boundary types the field
        # when present — so this check owns only the string's FORMAT: a non-PEP440
        # specifier set is garbled registry data → 502, never a caller 400.
        try:
            specifier = SpecifierSet(contract_range)
        except InvalidSpecifier as exc:
            raise RegistryResponseError(
                f"registry served an unusable contract_range {contract_range!r}: {exc}", status=None
            ) from exc
        installed = running_contract_version()
        if not specifier.contains(installed, prereleases=True):
            raise ContractIncompatibleError(
                f"plugin requires tai42-contract {contract_range}, but {installed} is installed"
            )


def _core_version_stamps() -> tuple[str, str]:
    """The ``(tai42-contract, tai42-skeleton)`` versions running right now — the
    diagnostics stamp every attribution write carries."""
    return running_contract_version(), importlib.metadata.version("tai42-skeleton")


def _parse_ref(ref: str) -> tuple[str, str]:
    """Split ``namespace/name`` into its two lowercase halves.

    Raises :class:`MalformedRefError` (surfaced by the boundary as a 400) on
    anything but exactly one ``/`` with two non-empty lowercase halves. The typed
    error is distinct from a server-side invariant fault, so the operation layer
    maps ONLY a malformed ref to a bad-request response.
    """
    parts = ref.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise MalformedRefError(f"ref must be 'namespace/name', got {ref!r}")
    namespace, name = parts
    if namespace != namespace.lower() or name != name.lower():
        raise MalformedRefError(f"ref must be lowercase 'namespace/name', got {ref!r}")
    return namespace, name


def _spec_from_row(row: InstallRecord) -> PluginSpec:
    """Reconstruct the stored ``PluginSpec`` from an attribution row — LOCAL
    truth, no registry call. A row that no longer validates is corrupt local
    state (:class:`LocalStateError`, a 500), never the caller's request."""
    try:
        return PluginSpec.model_validate(row.spec)
    except ValidationError as exc:
        raise LocalStateError(f"the stored spec for {row.ref} is corrupt: {exc}") from exc


def _pin_provenance(resolved: dict[str, Any], source: str) -> tuple[str | None, str | None, str | None, str | None]:
    """The ``(repository_url, tag, artifact_ref, sha256)`` to store for the pin:
    the resolve values only for a github source, else all ``None`` — so a pypi row
    keeps every pin column NULL even when the resolve response carries them. The
    stored ``artifact_ref`` + ``sha256`` are what let update-unwind reinstall the
    old github pin through the same verified fetch path."""
    if source == "github":
        return (
            resolved.get("repository_url"),
            resolved.get("tag"),
            resolved.get("artifact_ref"),
            resolved.get("sha256"),
        )
    return None, None, None, None


def _has_manifest_provides(spec: PluginSpec) -> bool:
    """Whether the spec provides any item that wires into a manifest field (a
    non-env-selected item).

    Such a plugin's live registration must be converged by an uninstall reload
    even when the manifest is already stripped — a prior partial run may have
    persisted the stripped manifest but failed the deregister reload, so the tools
    stay live until a re-run reloads.
    """
    for item in spec.provides:
        binding = KIND_MANIFEST_BINDINGS.get(item.kind)
        if binding is not None and binding.mode != "env_selected":
            return True
    return False


def _env_selected_items(spec: PluginSpec) -> list[PluginItem]:
    """The spec's provides items that wire into no manifest field (the
    env-selected ``config`` kind) — pip install/uninstall is their whole
    registration."""
    items: list[PluginItem] = []
    for item in spec.provides:
        binding = KIND_MANIFEST_BINDINGS.get(item.kind)
        if binding is not None and binding.mode == "env_selected":
            items.append(item)
    return items


def _mcp_entry_items(spec: PluginSpec) -> list[PluginItem]:
    """The spec's provides items that write an ``mcp`` entry (an mcp-server item
    carrying transport config, no module). A spec never mixes mcp-server with any
    other kind, so a non-empty result means this whole install is an mcp-server one."""
    items: list[PluginItem] = []
    for item in spec.provides:
        binding = KIND_MANIFEST_BINDINGS.get(item.kind)
        if binding is not None and binding.mode == "mcp_entry":
            items.append(item)
    return items


def _install_notes(spec: PluginSpec) -> list[str]:
    """Activation notes: one per env-selected item (installed but inactive until
    ``TAI_CONFIG_MODE`` selects it, and only providers the skeleton's fixed
    mode→module map covers can be selected), plus one per mcp-server item naming
    the mounted server title."""
    notes = [
        f"{item.name!r} is installed but inactive: TAI_CONFIG_MODE selects config providers from the "
        "skeleton's fixed mode->module map, so activation needs the mode to exist there "
        "(tai42-config-k8s is the one provider covered today; a new provider needs a skeleton-side map entry)"
        for item in _env_selected_items(spec)
    ]
    notes.extend(
        f"mounted MCP server {item.name!r} in the manifest 'mcp' section; its tools go live on the reload"
        for item in _mcp_entry_items(spec)
    )
    return notes


def _uninstall_notes(spec: PluginSpec) -> list[str]:
    """Removal notes: one per env-selected item (if ``TAI_CONFIG_MODE`` currently
    selects the removed provider, the next boot fails importing it until re-pointed
    or unset), plus one per mcp-server item — its manifest entry is removed but the
    stored env values its ``!ENV`` markers referenced are LEFT (never silently
    delete operator secrets); the note names those orphaned vars, scanned from the
    entry BEFORE removal."""
    notes = [
        f"{item.name!r} was a config provider: if TAI_CONFIG_MODE currently selects it, the next boot will "
        "fail importing the removed provider until you re-point or unset TAI_CONFIG_MODE"
        for item in _env_selected_items(spec)
    ]
    for item in _mcp_entry_items(spec):
        assert item.mcp is not None  # guaranteed for the mcp-server kind
        orphaned = sorted({ref.var for ref in scan_env_marker_refs(item.mcp.model_dump(exclude_none=True))})
        if orphaned:
            notes.append(
                f"removed MCP server {item.name!r}; its stored env value(s) are LEFT untouched — "
                f"{', '.join(orphaned)} may now be orphaned (remove them by hand if no other entry uses them)"
            )
        else:
            notes.append(f"removed MCP server {item.name!r} from the manifest 'mcp' section")
    return notes


def _require(resolved: dict[str, Any], key: str) -> Any:
    """A required resolve-response field — present AND non-null — or a typed
    registry-data fault (502).

    The client boundary type-checks a field only when it is present and non-null
    (a null there is legitimate for the github-only optional pins), so ``required``
    has to mean non-null here: a null in an always-present field (``version``,
    ``contract_range``, ``spec``, ``source``) is missing data, and rejecting it
    keeps the ``str``-typed parsers downstream honest — they never see ``None``."""
    value = resolved.get(key)
    if value is None:
        raise RegistryResponseError(f"registry resolve response is missing {key!r}", status=None)
    return value


def _require_list(resolved: dict[str, Any], key: str) -> list[Any]:
    """A required list-shaped resolve-response field, or a typed registry-data
    fault (502)."""
    value = _require(resolved, key)
    if not isinstance(value, list):
        raise RegistryResponseError(f"registry resolve response {key!r} is not a list", status=None)
    return value
