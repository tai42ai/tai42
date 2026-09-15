"""Variant-set resolution from the selection env."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tai42_e2e.topology import InfraUnavailableError
from tai42_e2e.variants.backends import BACKENDS, BackendVariant
from tai42_e2e.variants.identities import IDENTITIES, IdentityVariant
from tai42_e2e.variants.storages import STORAGES, StorageVariant

if TYPE_CHECKING:
    from tai42_e2e.settings import HarnessSettings


@dataclass(frozen=True)
class Variants:
    """The one variant set a pytest process runs under."""

    backend: BackendVariant
    identity: IdentityVariant
    storage: StorageVariant


def _resolve[T](registry: dict[str, T], name: str, env_var: str) -> T:
    try:
        return registry[name]
    except KeyError:
        valid = ", ".join(sorted(registry))
        raise InfraUnavailableError(f"{env_var}={name!r} is not a known variant; valid values: {valid}") from None


def resolve_variants(settings: HarnessSettings) -> Variants:
    """Resolve the backend/identity/storage triple from the selection settings.
    An unknown name raises :class:`InfraUnavailableError` naming the valid values —
    surfaced at session start through the ``tests/conftest.py::infra`` exit
    path, never silently defaulted."""
    return Variants(
        backend=_resolve(BACKENDS, settings.backend, "TAI_E2E_BACKEND"),
        identity=_resolve(IDENTITIES, settings.identity, "TAI_E2E_IDENTITY"),
        storage=_resolve(STORAGES, settings.storage, "TAI_E2E_STORAGE"),
    )
