"""Validation on the resolved projection of a manifest / env / profile-replace change.

Covers the pydantic ``Manifest`` schema, the boundary refusals (X-band, key material, dangling
``!ENV``, incomplete admin pair), and the backend-needs-bus invariant in both directions.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from tai42_skeleton.app.boot_rules import check_backend_needs_bus
from tai42_skeleton.app.bus_settings import BusSettings, bus_settings
from tai42_skeleton.config.boundary import (
    refuse_incomplete_admin_pair,
    refuse_key_material,
    refuse_unresolved_env,
    refuse_x_band,
)
from tai42_skeleton.config.service.base import _ConfigServiceBase
from tai42_skeleton.config.service.resolution import _environ
from tai42_skeleton.manifest import Manifest

if TYPE_CHECKING:
    from collections.abc import Mapping


class _ValidationMixin(_ConfigServiceBase):
    """The validate-before-persist gate every pipeline entrypoint runs its change through.

    The resolved projection of a change is validated here before it is persisted.
    """

    def _validate_manifest(self, document: Mapping[str, Any]) -> None:
        """Validate the resolved projection of a manifest change.

        Checks the pydantic ``Manifest`` schema plus the backend-needs-bus invariant, and refuses
        any ``!ENV`` marker left dangling against the current env. The env is unchanged by a
        manifest mutation, so markers resolve against the current process env and the bus
        configuration is the current one.
        """
        manifest = self._validated_projection(document)
        check_backend_needs_bus(backend_module=manifest.backend_module, bus_configured=bus_settings().enabled)
        refuse_unresolved_env(document, dict(os.environ))

    def _validate_env(self, changes: dict[str, str]) -> None:
        """Validate the effective config an env change produces.

        Refuses an X-band key in the change PAYLOAD, resolves the persisted manifest's ``!ENV``
        markers against the post-change env (so a marker that materializes a backend participates,
        and a dropped reference is caught as dangling), and evaluates the backend-needs-bus
        invariant against the post-change bus configuration (so removing the bus while a backend
        remains is rejected too).
        """
        refuse_x_band(changes.keys())
        # Change-aware: refuse a key-material key only when the payload SETS it to a value
        # different from the current stored value (rotation-via-editor), never an unchanged
        # carry — the stored env is the real values a read-modify-write round-trip re-sends.
        refuse_key_material(changes, self._read_stored_env())
        effective = self._effective_env(changes)
        refuse_incomplete_admin_pair(effective)
        with _environ(effective):
            preserved = self._read_preserved_manifest()
            refuse_unresolved_env(preserved, effective)
            manifest = self._validated_projection(preserved)
            # Resolve the bus through its settings against the effective env (a fresh
            # read, NOT the cached singleton) so a bus configured only via
            # ``TAI_DEFAULT_REDIS_URL`` — not ``TAI_BUS_REDIS_URL`` — is still seen as
            # enabled; a raw ``TAI_BUS_REDIS_URL`` read would falsely reject every
            # Studio env edit on a default-only deployment.
            bus_configured = BusSettings().enabled
        check_backend_needs_bus(backend_module=manifest.backend_module, bus_configured=bus_configured)

    def _validate_env_and_manifest(self, changes: dict[str, str], document: Mapping[str, Any]) -> None:
        """Validate a combined env-write + manifest-mutate before it persists.

        Refuses an X-band key in the env change PAYLOAD, resolves the MUTATED manifest's
        ``!ENV`` markers against the post-change effective env (so the marker the mutator
        just wrote resolves against the value the env write supplies, and a dropped
        reference is caught as dangling), validates the resolved projection, and
        evaluates backend-needs-bus against the post-change bus configuration.
        """
        refuse_x_band(changes.keys())
        # Change-aware key-material refusal (see :meth:`_validate_env`): a CHANGE to a KEK /
        # signing key is refused, an unchanged carry is allowed.
        refuse_key_material(changes, self._read_stored_env())
        effective = self._effective_env(changes)
        refuse_incomplete_admin_pair(effective)
        with _environ(effective):
            refuse_unresolved_env(document, effective)
            manifest = self._validated_projection(document)
            bus_configured = BusSettings().enabled
        check_backend_needs_bus(backend_module=manifest.backend_module, bus_configured=bus_configured)

    def _validate_replace(self, profile_env: dict[str, str]) -> None:
        """Validate a whole-env REPLACE (a settings-profile apply) before it persists.

        The apply's validate-before-persist entry, mirroring :meth:`ConfigService.apply_replace`.

        Refuses any X-band key in the profile's DECLARED payload (NEVER the post-carry
        effective env — the applier legitimately CARRIES the whole X band across
        ``replace_env``), refuses a change that leaves a manifest ``!ENV`` marker
        dangling against the replace-effective env, and evaluates the backend-needs-bus
        invariant against that same env.
        """
        refuse_x_band(profile_env.keys())
        # Change-aware key-material refusal: a profile snapshotted from the stored env carries
        # the KEK unchanged (allowed); only a profile that would SET key material to a new
        # value is refused (see :meth:`_validate_env`).
        refuse_key_material(profile_env, self._read_stored_env())
        effective = self._effective_replace_env(profile_env)
        refuse_incomplete_admin_pair(effective)
        # A connector reads its client-credential env STRAIGHT from ``os.environ`` at connect
        # time, so it resolves against the FULL process env (current ``os.environ`` overlaid
        # with the replace band), NOT the narrowed replace band: a deployment-supplied cred a
        # SPARSE profile omits stays live in the process env, so its connector must not read as
        # unset. Markers keep the narrowed ``effective`` band — a dropped stored key still
        # dangles. Captured BEFORE ``_environ`` swaps ``os.environ`` to the modeled band.
        connector_env = {**os.environ, **effective}
        with _environ(effective):
            preserved = self._read_preserved_manifest()
            refuse_unresolved_env(preserved, effective, connector_env=connector_env)
            manifest = self._validated_projection(preserved)
            bus_configured = BusSettings().enabled
        check_backend_needs_bus(backend_module=manifest.backend_module, bus_configured=bus_configured)

    def _validated_projection(self, document: Mapping[str, Any]) -> Manifest:
        """Build and validate the RESOLVED in-memory projection of a PRESERVED document.

        The ``!ENV`` markers are materialized for validation only, then the projection is validated
        against the ``Manifest`` schema. Raises on an invalid document.
        """
        return Manifest.model_validate(self._resolve(document))
