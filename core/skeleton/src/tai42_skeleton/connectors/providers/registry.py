"""Provider registry — the engine's in-memory map of supported third-parties.

The catalog endpoint reads the registry, so adding a provider needs no UI
changes. The skeleton ships no concrete provider: registration is
manifest-driven. Each ``connectors`` entry in the manifest is registered through
``tai42_app.connectors.register_connector(descriptor)`` during boot/reload, which
forwards to :func:`register_connector` here. Descriptors are validated when built
so a misconfigured provider fails deployment loudly rather than at first user click.

The descriptor models live in :mod:`tai42_contract.connectors.providers`; this
module owns only the registry STATE (the code-built ``_REGISTRY``) and the
registration / lookup functions.
"""

from __future__ import annotations

import logging

from tai42_contract.connectors.errors import (  # noqa: F401  (re-exported)
    OperatorMisconfiguredError,
)
from tai42_contract.connectors.providers import (  # noqa: F401  (re-exported)
    ConfigFieldSpec,
    McpServerDescriptor,
    OAuthEndpoints,
    ProviderDescriptor,
    SubServiceDescriptor,
)

logger = logging.getLogger(__name__)

# Code-side mirror of the connector_category seed rows in the init SQL.
# register_connector validates registry descriptors against it because
# registration runs during boot/reload, before the DB is reachable. A provider's
# category is a foreign key into connector_category, so a descriptor must name a
# seeded category.
SEED_CATEGORY_IDS = (
    "communication",
    "productivity",
    "dev-tools",
    "data",
    "ai-ml",
    "other",
)


# -- Registry ----------------------------------------------------------------
# The reload seam calls register_connector for each manifest ``connectors`` entry.
# ``_REGISTRY`` is the COMMITTED generation the request path (catalog, resolver)
# reads; ``_pending`` is the generation an epoch build stages into, promoted
# atomically on success and dropped on failure — so a failed build leaves the
# live catalog untouched.

_REGISTRY: dict[str, ProviderDescriptor] = {}
_pending: dict[str, ProviderDescriptor] | None = None


def _write_target() -> dict[str, ProviderDescriptor]:
    return _pending if _pending is not None else _REGISTRY


def register_connector(descriptor: ProviderDescriptor) -> None:
    target = _write_target()
    if descriptor.id in target:
        raise ValueError(f"Provider {descriptor.id!r} already registered")
    # Registration runs during boot/reload, before the DB is reachable, so the
    # category check goes against the code-side seed constants.
    if descriptor.category not in SEED_CATEGORY_IDS:
        raise ValueError(
            f"provider {descriptor.id!r} category {descriptor.category!r} is not "
            f"a seed category (expected one of {', '.join(SEED_CATEGORY_IDS)})"
        )
    target[descriptor.id] = descriptor
    logger.info("connectors: registered provider %s", descriptor.id)


def reset_registry() -> None:
    """Clear the write-target provider registry — the STAGED generation while a build is
    staging (``start()`` clears the fresh staged map before re-registering the
    manifest's ``connectors`` entries, never the committed one), else the committed
    map (boot, test isolation)."""
    _write_target().clear()


def get_provider(provider_id: str) -> ProviderDescriptor:
    """Resolve a descriptor from the COMMITTED generation — the request-path accessor."""
    descriptor = _REGISTRY.get(provider_id)
    if descriptor is None:
        raise KeyError(f"Unknown Connectors provider: {provider_id!r}")
    return descriptor


def list_providers() -> list[ProviderDescriptor]:
    """Every COMMITTED descriptor — the request-path catalog view."""
    return list(_REGISTRY.values())


def list_providers_staged() -> list[ProviderDescriptor]:
    """Every descriptor in the STAGED generation if a build is staging, else the
    committed one — the build's own view (kind status)."""
    return list(_write_target().values())


def begin_staging() -> None:
    """Open a fresh staged generation the epoch build registers into."""
    global _pending
    _pending = {}


def commit_staging() -> None:
    """Promote the staged generation to committed in one reference assignment."""
    global _REGISTRY, _pending
    if _pending is not None:
        _REGISTRY = _pending  # pyright: ignore[reportConstantRedefinition]  # atomic generation swap
        _pending = None


def abort_staging() -> None:
    """Drop the staged generation on a failed build."""
    global _pending
    _pending = None
