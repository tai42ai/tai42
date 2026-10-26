"""Validation on the resolved projection of a manifest / env / profile-replace change.

Covers the pydantic ``Manifest`` schema, the boundary refusals (X-band, unknown owned-prefix
settings, key material, a key the deployment sets, an unrecyclable change, dangling ``!ENV``,
incomplete admin pair), the
backend-needs-bus and backend-needs-hooks-store invariants in both directions, the
workers-need-shared-stores invariants (a multi-worker server keeps its sub-MCP registration
store and hooks registry shared), and the apply-window invariant (a bus-enabled config's fleet
apply window covers the retire's drain floor) against the post-change env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tai42_skeleton.app.boot_rules import (
    check_apply_window_covers_drain,
    check_backend_needs_bus,
    check_backend_needs_hooks_store,
    check_workers_need_bus,
    check_workers_need_hooks_store,
    check_workers_need_sub_mcp_store,
    declared_drain_budgets,
    serve_workers,
)
from tai42_skeleton.app.bus_settings import BusSettings, bus_settings
from tai42_skeleton.app.epoch import Epoch, current_epoch_or_none
from tai42_skeleton.config.boundary import (
    refuse_colliding_spellings,
    refuse_deployment_owned,
    refuse_incomplete_admin_pair,
    refuse_key_material,
    refuse_unrecyclable_env_change,
    refuse_unresolved_env,
    refuse_x_band,
)
from tai42_skeleton.config.service.base import _ConfigServiceBase
from tai42_skeleton.config.service.resolution import _environ
from tai42_skeleton.hooks.settings import HooksSettings
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.settings.owned_settings import refuse_unknown_owned_env_write
from tai42_skeleton.sub_mcp.settings import SubMcpSettings
from tai42_skeleton.tools.binding.errors import ExtensionComboRefusedError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from tai42_contract.manifest import ExtensionElement

    from tai42_skeleton.config.deployment_env import DeploymentOwnership


def _stored_after(stored: Mapping[str, str], changes: Mapping[str, str]) -> dict[str, str]:
    """The env a merge of ``changes`` into ``stored`` would store.

    An empty value removes its key (the store drops empties).
    """
    return {key: value for key, value in {**stored, **changes}.items() if value != ""}


def _refuse_unrecyclable_merge(
    stored: Mapping[str, str], changes: Mapping[str, str], ownership: DeploymentOwnership
) -> list[str]:
    """Refuse an env merge no recycle can carry; return its recycle-class keys.

    The env the merge would store is :func:`_stored_after`. The returned keys are the ones the
    merge changes that only a worker recycle carries, sorted.
    """
    proposed = _stored_after(stored, changes)
    _hot, recycle = refuse_unrecyclable_env_change(stored, proposed, ownership, change="this env change")
    return recycle


def _check_apply_window(effective: dict[str, str], *, stored_env_file: str | None) -> None:
    """Evaluate the apply-window invariant against the post-change env ``effective``.

    The bus settings and the declared drain budget are read fresh under ``effective`` (never
    the cached singletons, which hold the live env), so a change to either setting — or to
    the bus itself — is judged as the reloaded process would see it. A busless env uses no
    apply window. The drain-budget registry is reached outside the modeled env, so the app is
    never first built under it. ``stored_env_file`` is the stored env file :func:`_environ` skips.
    """
    with _environ(effective, stored_env_file=stored_env_file):
        bus = BusSettings()
    if not bus.enabled:
        return
    budgets = declared_drain_budgets()
    with _environ(effective, stored_env_file=stored_env_file):
        drain_budget = budgets.budget()
    check_apply_window_covers_drain(bus_enabled=True, apply_timeout=bus.apply_timeout, drain_budget=drain_budget)


def _check_bus_invariants(*, backend_module: str | None, workers: int, bus_configured: bool) -> None:
    """Evaluate the backend-needs-bus and workers-need-bus invariants for ``bus_configured``.

    ``bus_configured`` is whether the env of the change being validated configures the bus;
    ``workers`` is the server's worker count, read from the live process env by the caller.
    """
    check_backend_needs_bus(backend_module=backend_module, bus_configured=bus_configured)
    check_workers_need_bus(workers=workers, bus_configured=bus_configured)


def _check_store_invariants(*, backend_module: str | None, workers: int) -> None:
    """Evaluate the backend-needs-hooks-store and workers-need-shared-stores invariants under the current env.

    Called inside the modeled env of the change being validated, so the hooks and sub-MCP
    settings are read fresh (never the cached singletons) as the stores would read them after
    the change. ``workers`` is the server's worker count, read from the live process env by
    the caller: it is the stamp ``tai serve`` set at launch, which no change carries.
    """
    hooks_store_shared = not HooksSettings().in_memory
    check_backend_needs_hooks_store(backend_module=backend_module, hooks_store_shared=hooks_store_shared)
    check_workers_need_sub_mcp_store(workers=workers, sub_mcp_store_shared=not SubMcpSettings().in_memory)
    check_workers_need_hooks_store(workers=workers, hooks_store_shared=hooks_store_shared)


def _extension_combos(manifest: Manifest) -> Iterator[tuple[str, str, list[ExtensionElement]]]:
    """Every extension combo of ``manifest`` as ``(path, tool, combo)``, in document order."""
    for section, entries in (("tools", manifest.tools), ("mcp", manifest.mcp)):
        for entry in entries:
            for tool, combos in entry.extensions.items():
                for index, combo in enumerate(combos):
                    yield f"{section}[{entry.title}].extensions[{tool}][{index}]", tool, combo
    for tool, combos in manifest.api_tools.extensions.items():
        for index, combo in enumerate(combos):
            yield f"api_tools.extensions[{tool}][{index}]", tool, combo


@dataclass(frozen=True, eq=False)
class _ManifestValidationInputs:
    """What :meth:`_ValidationMixin._validate_manifest` reads besides the document, taken at one moment.

    The process env (the env ``!ENV`` markers and combo settings resolve against is derived from it
    and the stored env, which the write exclusion holds still), whether the bus is
    configured (the backend-needs-bus rule), and the live serving generation (the combo check
    dry-runs against its extension registry; a reload or a settings-profile apply installs a new one).
    """

    env: dict[str, str]
    bus_enabled: bool
    epoch: Epoch | None

    @classmethod
    def now(cls) -> _ManifestValidationInputs:
        return cls(env=dict(os.environ), bus_enabled=bus_settings().enabled, epoch=current_epoch_or_none())

    def unchanged(self) -> bool:
        """Whether the process env, the bus setting and the live serving generation are still these."""
        current = _ManifestValidationInputs.now()
        return current.env == self.env and current.bus_enabled == self.bus_enabled and current.epoch is self.epoch


@dataclass(frozen=True, eq=False)
class _EnvValidationInputs:
    """What an env-write validation reads, taken at one moment.

    The process inputs of :class:`_ManifestValidationInputs` plus the stored env (the change
    merges onto it) and the stored manifest (its ``!ENV`` markers resolve against the new env).
    """

    process: _ManifestValidationInputs
    stored_env: dict[str, str]
    manifest: dict[str, Any]


class _ValidationMixin(_ConfigServiceBase):
    """The validate-before-persist gate every pipeline entrypoint runs its change through.

    The resolved projection of a change is validated here before it is persisted.
    """

    def _validate_manifest(self, document: Mapping[str, Any], ownership: DeploymentOwnership) -> None:
        """Validate the resolved projection of a manifest change.

        Refuses any ``!ENV`` marker left dangling first, so it is named as dangling before the
        resolved projection, where it would read as ``"N/A"``, is validated; then checks the
        pydantic ``Manifest`` schema and the extension combos, then the backend-needs-bus,
        backend-needs-hooks-store, workers-need-bus and workers-need-shared-stores invariants.
        Markers and the projection resolve against the env a reload applies from the store
        (:meth:`_converged_env` of the stored env under ``ownership``, the fleet's deployment
        ownership), not this process's env alone: the write holds the store's exclusive span, so
        that env is the one every worker converges to. A manifest mutation leaves the env
        unchanged, so the invariants read the current bus and store configuration, after the
        projection.
        """
        env = self._converged_env(self._read_stored_env(), ownership)
        refuse_unresolved_env(document, env)
        with _environ(env, stored_env_file=self._stored_env_file()):
            manifest = self._validated_projection(document)
        workers = serve_workers()
        _check_bus_invariants(
            backend_module=manifest.backend_module, workers=workers, bus_configured=bus_settings().enabled
        )
        _check_store_invariants(backend_module=manifest.backend_module, workers=workers)

    def _validate_manifest_noting_inputs(
        self, document: Mapping[str, Any], ownership: DeploymentOwnership
    ) -> _ManifestValidationInputs:
        """Validate ``document`` as :meth:`_validate_manifest` does; return the inputs it was validated against.

        The inputs are taken before the validation reads them, so a change landing while it runs
        reads as changed. A caller that awaits before it persists validates again unless the
        document and :meth:`_ManifestValidationInputs.unchanged` both still hold.
        """
        inputs = _ManifestValidationInputs.now()
        self._validate_manifest(document, ownership)
        return inputs

    def _env_validation_inputs(self) -> _EnvValidationInputs:
        """The inputs an env-write validation is about to read; taken before it reads them."""
        return _EnvValidationInputs(
            process=_ManifestValidationInputs.now(),
            stored_env=self._read_stored_env(),
            manifest=self._read_preserved_manifest(),
        )

    def _env_validation_inputs_unchanged(self, inputs: _EnvValidationInputs) -> bool:
        """Whether the process inputs, the stored env and the stored manifest are still ``inputs``."""
        return (
            inputs.process.unchanged()
            and self._read_stored_env() == inputs.stored_env
            and self._read_preserved_manifest() == inputs.manifest
        )

    def _validate_env(self, changes: dict[str, str], ownership: DeploymentOwnership) -> list[str]:
        """Validate the effective config an env change produces; return the recycle-class keys it changes.

        Refuses an X-band key in the change PAYLOAD, a stored env that would set one name under two
        spellings, a change that sets a key the deployment sets, and a change no recycle can
        carry, resolves the persisted manifest's ``!ENV`` markers against the post-change env (so
        a marker that materializes a backend participates,
        and a dropped reference is caught as dangling), and evaluates the backend-needs-bus and
        backend-needs-hooks-store invariants against the post-change bus and hooks-store
        configuration (so removing either while a backend remains is rejected too), the
        workers-need-bus and workers-need-shared-stores invariants (so emptying the bus or a store
        a multi-worker server shares is rejected) and the apply-window invariant against the post-change env.
        """
        refuse_x_band(changes.keys())
        refuse_unknown_owned_env_write([key for key, value in changes.items() if value != ""])
        stored = self._read_stored_env()
        refuse_colliding_spellings(_stored_after(stored, changes), source="an env write")
        # Change-aware: refuse a key-material key only when the payload SETS it to a value
        # different from the current stored value (rotation-via-editor), never an unchanged
        # carry — the stored env is the real values a read-modify-write round-trip re-sends.
        refuse_key_material(changes, stored)
        # Change-aware the same way: a key the deployment sets keeps the deployment's value, so
        # a write that sets it to a new value is refused; removing or carrying its stored copy is not.
        refuse_deployment_owned(changes, stored, ownership)
        effective = self.effective_env(changes, ownership)
        refuse_incomplete_admin_pair(effective)
        workers = serve_workers()
        with _environ(effective, stored_env_file=self._stored_env_file()):
            preserved = self._read_preserved_manifest()
            refuse_unresolved_env(preserved, effective)
            manifest = self._validated_projection(preserved)
            # Resolve the bus through its settings against the effective env (a fresh
            # read, NOT the cached singleton) so a bus configured only via
            # ``TAI_DEFAULT_REDIS_URL`` — not ``TAI_BUS_REDIS_URL`` — is still seen as
            # enabled; a raw ``TAI_BUS_REDIS_URL`` read would falsely reject every
            # Studio env edit on a default-only deployment. The hooks and sub-MCP stores
            # are read the same way.
            _check_bus_invariants(
                backend_module=manifest.backend_module, workers=workers, bus_configured=BusSettings().enabled
            )
            _check_store_invariants(backend_module=manifest.backend_module, workers=workers)
        _check_apply_window(effective, stored_env_file=self._stored_env_file())
        # Last, so a change the bus invariants refuse is named by their rule.
        return _refuse_unrecyclable_merge(stored, changes, ownership)

    def _validate_env_and_manifest(
        self, changes: dict[str, str], document: Mapping[str, Any], ownership: DeploymentOwnership
    ) -> list[str]:
        """Validate a combined env-write + manifest-mutate before it persists; return the recycle-class keys it changes.

        Refuses an X-band key in the env change PAYLOAD, a stored env that would set one name under
        two spellings, a change that sets a key the deployment sets, and a change no recycle can
        carry, resolves the MUTATED manifest's ``!ENV`` markers against the post-change effective
        env (so the marker the mutator
        just wrote resolves against the value the env write supplies, and a dropped
        reference is caught as dangling), validates the resolved projection, and
        evaluates backend-needs-bus, backend-needs-hooks-store, workers-need-bus,
        workers-need-shared-stores and the apply-window invariant against the post-change env.
        """
        refuse_x_band(changes.keys())
        refuse_unknown_owned_env_write([key for key, value in changes.items() if value != ""])
        stored = self._read_stored_env()
        refuse_colliding_spellings(_stored_after(stored, changes), source="an env write")
        # Change-aware key-material refusal (see :meth:`_validate_env`): a CHANGE to a KEK /
        # signing key is refused, an unchanged carry is allowed. A key the deployment sets is
        # refused the same change-aware way.
        refuse_key_material(changes, stored)
        refuse_deployment_owned(changes, stored, ownership)
        effective = self.effective_env(changes, ownership)
        refuse_incomplete_admin_pair(effective)
        workers = serve_workers()
        with _environ(effective, stored_env_file=self._stored_env_file()):
            refuse_unresolved_env(document, effective)
            manifest = self._validated_projection(document)
            _check_bus_invariants(
                backend_module=manifest.backend_module, workers=workers, bus_configured=BusSettings().enabled
            )
            _check_store_invariants(backend_module=manifest.backend_module, workers=workers)
        _check_apply_window(effective, stored_env_file=self._stored_env_file())
        return _refuse_unrecyclable_merge(stored, changes, ownership)

    def _validate_replace(self, profile_env: dict[str, str], ownership: DeploymentOwnership) -> None:
        """Validate a whole-env REPLACE (a settings-profile apply) before it persists.

        The apply's validate-before-persist entry, mirroring :meth:`ConfigService.apply_replace`.

        Refuses any X-band key in the profile's DECLARED payload (NEVER the post-carry
        effective env — the applier legitimately CARRIES the whole X band across
        ``replace_env``), refuses a profile that sets one name under two spellings, refuses a
        profile that sets a key the deployment sets to a value other
        than its stored one, refuses a change that leaves a manifest ``!ENV`` marker
        dangling against the replace-effective env, and evaluates the backend-needs-bus,
        backend-needs-hooks-store, workers-need-bus, workers-need-shared-stores and apply-window
        invariants against that same env.
        """
        refuse_x_band(profile_env.keys())
        refuse_colliding_spellings(profile_env, source="a profile")
        refuse_unknown_owned_env_write([key for key, value in profile_env.items() if value != ""])
        # Change-aware key-material refusal: a profile snapshotted from the stored env carries
        # the KEK unchanged (allowed); only a profile that would SET key material to a new
        # value is refused (see :meth:`_validate_env`).
        stored = self._read_stored_env()
        refuse_key_material(profile_env, stored)
        # A profile may drop or carry unchanged a stored copy of a key the deployment sets, never
        # set one to a new value.
        refuse_deployment_owned(profile_env, stored, ownership)
        effective = self._effective_replace_env(profile_env, ownership)
        refuse_incomplete_admin_pair(effective)
        workers = serve_workers()
        # A connector reads its client-credential env STRAIGHT from ``os.environ`` at connect
        # time, so it resolves against the FULL process env (current ``os.environ`` overlaid
        # with the replace band), NOT the narrowed replace band: a deployment-supplied cred a
        # SPARSE profile omits stays live in the process env, so its connector must not read as
        # unset. Markers keep the narrowed ``effective`` band — a dropped stored key still
        # dangles. Captured BEFORE ``_environ`` swaps ``os.environ`` to the modeled band.
        connector_env = {**os.environ, **effective}
        with _environ(effective, stored_env_file=self._stored_env_file()):
            preserved = self._read_preserved_manifest()
            refuse_unresolved_env(preserved, effective, connector_env=connector_env)
            manifest = self._validated_projection(preserved)
            _check_bus_invariants(
                backend_module=manifest.backend_module, workers=workers, bus_configured=BusSettings().enabled
            )
            _check_store_invariants(backend_module=manifest.backend_module, workers=workers)
        _check_apply_window(effective, stored_env_file=self._stored_env_file())

    def _validated_projection(self, document: Mapping[str, Any]) -> Manifest:
        """Build and validate the RESOLVED in-memory projection of a PRESERVED document.

        The ``!ENV`` markers are materialized for validation only, then the projection is validated
        against the ``Manifest`` schema and every extension combo of ``tools``, ``mcp`` and
        ``api_tools`` is dry-run bound, in document order, with its settings resolved. Raises on an
        invalid document; a refused combo raises ``ValueError`` naming its path.
        """
        manifest = Manifest.model_validate(self._resolve(document))
        for path, tool, combo in _extension_combos(manifest):
            try:
                self._combo_check(tool, combo)
            except ExtensionComboRefusedError as exc:
                raise ValueError(f"{path}: {exc}") from exc
        return manifest
