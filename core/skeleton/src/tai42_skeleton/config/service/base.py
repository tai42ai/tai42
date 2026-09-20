"""The internal cross-mixin seam of the composed pipeline.

The shared seams the concrete :class:`ConfigService` wires in, plus the resolution methods the
validation mixin reaches through the assembled class's MRO, declared once so each mixin
type-checks its sibling calls. The concrete implementations live on the resolution mixin and the
concrete class.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tai42_skeleton.config.service.results import _FleetPublisher, _ManifestStore, _ReloadAdmin


class _ConfigServiceBase(ABC):
    """The cross-mixin contract every pipeline concern mixin builds on.

    The shared seams the concrete :class:`ConfigService` holds, plus the resolution methods the
    validators call across the MRO.
    """

    _config_manager: _ManifestStore
    _admin: _ReloadAdmin
    _bus: _FleetPublisher

    @abstractmethod
    def _resolve(self, document: Mapping[str, Any]) -> dict[str, Any]:
        """The RESOLVED projection of a PRESERVED document.

        ``!ENV`` markers are materialized against the current process env.
        """

    @abstractmethod
    def _read_preserved_manifest(self) -> dict[str, Any]:
        """The persisted manifest in its PRESERVED view, or an empty document when none exists yet."""

    @abstractmethod
    def _read_stored_env(self) -> dict[str, str]:
        """The stored env map, treating a never-written store as empty."""

    @abstractmethod
    def _effective_env(self, changes: dict[str, str]) -> dict[str, str]:
        """The effective env an :meth:`ConfigService.apply_env_change` produces."""

    @abstractmethod
    def _effective_replace_env(self, profile_env: dict[str, str]) -> dict[str, str]:
        """The effective env a profile REPLACE produces."""
