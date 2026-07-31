"""Module-level identity-provider registry — populated by direct import.

Unlike the connector registry, an identity plugin cannot register through the
bound ``tai42_app`` handle. The plugin depends on tai42-contract (and tai42-kit) only
and bans ``tai42_skeleton``, yet must be able to register; and ``tai42_app`` raises
on ANY member access before ``bind()`` runs (``bind()`` runs only inside
``start()``), so a handle-based registration would crash at plugin import in any
process that imports the plugin before ``start()`` — the metrics entrypoint
never calls ``start()`` at all. So the plugin imports this module and calls
:func:`register_identity_provider` at its own module import, with no app handle
involved. Because the registry is plain module state, it fills in ANY process
that imports the plugin module, with no ``bind()`` anywhere.

The registry stores FACTORIES, not descriptors — the deliberate difference from
the connector registry: a connector registers a serializable descriptor, whereas
an identity provider is a live object holding a store/connection, so a plugin
registers a callable that builds the provider from settings.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from tai42_contract.access_control.identity import IdentityProvider

logger = logging.getLogger(__name__)

# Provider name -> factory (a callable that builds a live IdentityProvider from
# settings).
_REGISTRY: dict[str, Callable[..., IdentityProvider]] = {}


def register_identity_provider(name: str, factory: Callable[..., IdentityProvider]) -> None:
    if name in _REGISTRY:
        raise ValueError(f"Identity provider {name!r} already registered")
    _REGISTRY[name] = factory
    logger.info("access_control: registered identity provider %s", name)


def get_identity_provider_factory(name: str) -> Callable[..., IdentityProvider]:
    factory = _REGISTRY.get(name)
    if factory is None:
        raise KeyError(f"Unknown identity provider: {name!r}")
    return factory


def reset_registry() -> None:
    """Clear the identity-provider registry.

    Called by ``start()`` before it re-imports the manifest's plugin modules,
    which re-run their module-level ``register_identity_provider(...)`` calls.
    Without this the duplicate guard in :func:`register_identity_provider` would
    raise on every reload. The skeleton ships no concrete provider, so a reload
    that re-imports the identity plugin re-registers it without tripping the guard.
    """
    _REGISTRY.clear()


__all__ = [
    "get_identity_provider_factory",
    "register_identity_provider",
    "reset_registry",
]
