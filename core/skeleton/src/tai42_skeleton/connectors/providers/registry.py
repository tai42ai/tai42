"""Provider registry — the engine's in-memory map of supported third-parties.

The catalog endpoint reads the registry, so adding a provider needs no UI
changes. The skeleton ships no concrete provider: registration is
manifest-driven. A provider plugin module (named in the manifest, or installed
from the marketplace) calls ``tai42_app.connectors.register_connector(descriptor)``
on import, which forwards to :func:`register_connector` here. Descriptors are
validated when built so a misconfigured provider fails deployment loudly rather
than at first user click.

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
# registration runs at import time, before the DB is reachable. A provider's
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
# Provider plugin modules call register_connector at import with a descriptor.
# The single registry maps provider id -> descriptor.

_REGISTRY: dict[str, ProviderDescriptor] = {}


def register_connector(descriptor: ProviderDescriptor) -> None:
    if descriptor.id in _REGISTRY:
        raise ValueError(f"Provider {descriptor.id!r} already registered")
    # Registration runs at import time, before the DB is reachable, so the
    # category check goes against the code-side seed constants.
    if descriptor.category not in SEED_CATEGORY_IDS:
        raise ValueError(
            f"provider {descriptor.id!r} category {descriptor.category!r} is not "
            f"a seed category (expected one of {', '.join(SEED_CATEGORY_IDS)})"
        )
    _REGISTRY[descriptor.id] = descriptor
    logger.info("connectors: registered provider %s", descriptor.id)


def reset_registry() -> None:
    """Clear the code-built provider registry.

    Called by ``start()`` before it re-imports the manifest's connector plugin
    modules, which re-run their module-level ``register_connector(...)`` calls.
    Without this the duplicate guard in :func:`register_connector` would raise on
    every reload. Mirrors the ``_agents`` reset in the lifecycle mixin's
    ``start()``; the within-one-load duplicate guard is preserved.
    """
    _REGISTRY.clear()


def get_provider(provider_id: str) -> ProviderDescriptor:
    descriptor = _REGISTRY.get(provider_id)
    if descriptor is None:
        raise KeyError(f"Unknown Connectors provider: {provider_id!r}")
    return descriptor


def list_providers() -> list[ProviderDescriptor]:
    return list(_REGISTRY.values())
