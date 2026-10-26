"""The internal cross-mixin seam of the composed pipeline.

The shared seams the concrete :class:`ConfigService` wires in, the resolution methods the
validation mixin reaches through the assembled class's MRO, and the pipeline steps the profile
apply mixin reaches on the concrete class, declared once so each mixin type-checks its sibling
calls. The concrete implementations live on the resolution mixin and the concrete class.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager

    from tai42_contract.manifest import ExtensionElement

    from tai42_skeleton.config.deployment_env import DeploymentOwnership
    from tai42_skeleton.config.service.exclusion import WriteSpan
    from tai42_skeleton.config.service.results import _FleetPublisher, _ManifestStore, _ReloadAdmin


class _ConfigServiceBase(ABC):
    """The cross-mixin contract every pipeline concern mixin builds on.

    The shared seams the concrete :class:`ConfigService` holds, the resolution methods the
    validators call across the MRO, and the write steps the profile apply calls on the concrete
    class.
    """

    _config_manager: _ManifestStore
    _admin: _ReloadAdmin
    _bus: _FleetPublisher
    _combo_check: Callable[[str, Sequence[ExtensionElement]], None]

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
    def _stored_env_file(self) -> str | None:
        """The file the stored env is kept in, or ``None`` when the config manager keeps it elsewhere."""

    @abstractmethod
    def _converged_env(self, stored: Mapping[str, str], ownership: DeploymentOwnership) -> dict[str, str]:
        """The env a reload from the ``stored`` env under ``ownership`` leaves this process with."""

    @abstractmethod
    def effective_env(self, changes: Mapping[str, str], ownership: DeploymentOwnership) -> dict[str, str]:
        """The effective env an :meth:`ConfigService.apply_env_change` produces."""

    @abstractmethod
    def _effective_replace_env(self, profile_env: dict[str, str], ownership: DeploymentOwnership) -> dict[str, str]:
        """The effective env a profile REPLACE produces."""

    @abstractmethod
    def _write_exclusion(self) -> AbstractAsyncContextManager[WriteSpan]:
        """Hold the process-wide config-write lock, then the config store's exclusive write span."""

    @abstractmethod
    async def deployment_ownership(self, op_name: str) -> DeploymentOwnership:
        """The keys the deployment owns across the fleet for ``op_name``, read from the bus census."""

    @abstractmethod
    async def _refuse_held_unpicks(
        self, before: Mapping[str, Any], after: Mapping[str, Any], *, env: Mapping[str, str] | None = None
    ) -> None:
        """Refuse a write whose MCP selection change would stop picking a tool something still uses."""
