"""Manifest/env resolution and the secret seal — materialize a PRESERVED document's
``!ENV`` markers for validation and the seal, read the persisted manifest/env, and derive
the effective env a change or profile replace would produce."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

from pyaml_env import parse_config
from tai42_kit.utils.data import dump_manifest

from tai42_skeleton.config.boundary import x_band_env_keys
from tai42_skeleton.config.secret_seal import seal_resolved_secrets
from tai42_skeleton.config.service.base import _ConfigServiceBase

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from ruamel.yaml.comments import CommentedMap


class _ResolutionMixin(_ConfigServiceBase):
    """The one resolution the pipeline uses for both validation and the secret seal, plus
    the persisted-store reads and the effective-env derivations."""

    def _resolve(self, document: Mapping[str, Any]) -> dict[str, Any]:
        """The RESOLVED projection of a PRESERVED document — ``!ENV`` markers
        materialized against the current process env, structure otherwise intact. The
        one resolution the pipeline uses for both validation and the secret seal."""
        return cast("dict[str, Any]", parse_config(data=dump_manifest(cast("CommentedMap", document))) or {})

    def _seal_secrets(self, document: dict[str, Any], current_preserved: Mapping[str, Any]) -> None:
        """Seal *document* against the current manifest before it persists.

        *current_preserved* is the CURRENT manifest in its preserved view (``!ENV``
        markers) — the pre-mutation document for :meth:`ConfigService.apply_change`, the
        persisted manifest for :meth:`ConfigService.apply_replace`. It is resolved once to
        know each marker's resolved value, then
        :func:`~tai42_skeleton.config.secret_seal.seal_resolved_secrets` retags any leaf of
        *document* that still equals a resolved secret back to its marker (a resolved
        round-trip preserves the operator's markers) and raises a
        :class:`~tai42_skeleton.config.secret_seal.ResolvedSecretError` on a stranded
        resolved secret with no marker origin. A pure in-place mutation whose leaves
        already carry markers is a no-op. *document* is mutated in place."""
        seal_resolved_secrets(document, cast("dict[str, Any]", current_preserved), self._resolve(current_preserved))

    def _read_preserved_manifest(self) -> dict[str, Any]:
        """The persisted manifest in its PRESERVED view, or an empty document when no
        manifest exists yet — a deployment with no manifest registers no backend, so
        the env-change invariant has nothing to reject."""
        try:
            return self._config_manager.read_manifest_preserved()
        except FileNotFoundError:
            return {}

    def _effective_env(self, changes: dict[str, str]) -> dict[str, str]:
        """The effective env an :meth:`ConfigService.apply_env_change` produces, as the
        reloaded process would see it.

        The stored env is merged with ``changes`` (empties are dropped — the store
        filters them), then overlaid onto the current process env, which a reload
        applies with ``os.environ.update``. A key the change empties is treated as
        removed so a bus-removing change is visible to the invariant."""
        stored = self._read_stored_env()
        removed = {key for key, value in changes.items() if value == ""}
        merged = {key: value for key, value in {**stored, **changes}.items() if value != ""}
        effective = {key: value for key, value in os.environ.items() if key not in removed}
        effective.update(merged)
        return effective

    def _effective_replace_env(self, profile_env: dict[str, str]) -> dict[str, str]:
        """The effective env a profile REPLACE produces, as the reloaded process would
        see it.

        The profile env becomes the WHOLE stored env — keys the profile omits are
        DELETED — while the deployment X band is carried untouched from the current
        process env (X keys never enter a profile). So the old stored keys drop out,
        the profile keys overlay, and the carried X band overlays last; system env the
        store never owned is left in place."""
        stored = self._read_stored_env()
        carried = {key: value for key, value in os.environ.items() if key in x_band_env_keys()}
        effective = {key: value for key, value in os.environ.items() if key not in stored}
        effective.update(profile_env)
        effective.update(carried)
        return effective

    def _read_stored_env(self) -> dict[str, str]:
        """The stored env map, treating a never-written store as empty."""
        try:
            return self._config_manager.read_env()
        except FileNotFoundError:
            return {}


@contextmanager
def _environ(env: dict[str, str]) -> Iterator[None]:
    """Temporarily replace ``os.environ`` with ``env`` for the duration of the block,
    restoring it exactly afterwards. Used to resolve a manifest's ``!ENV`` markers
    against a proposed post-change env without mutating the real process env."""
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)
