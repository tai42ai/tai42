"""Module-level registry for accounts providers.

Handle-free on purpose, for the same reason as the identity-provider
registry (see ``tai42_contract.access_control.registry``): accounts plugins
never import the application package (ruff banned-api in every plugin repo)
and register at plain module import, before any app handle is usable. The
application resolves and ACTIVATES providers from its own configuration —
registration alone activates nothing.

Two deliberate differences from the identity registry:

- ``register_accounts_provider`` ALSO registers the factory into the
  identity registry under the same name — an accounts provider IS the token
  answerer for its own sessions, and registering into only one registry
  would make sessions mintable but not validatable (or vice versa). One
  plugin call, both registries, loud duplicate errors from either.
- The registry is enumerable: the public login-methods aggregator must ask
  EVERY registered accounts provider for its declared methods.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tai42_contract.access_control.registry import register_identity_provider

if TYPE_CHECKING:
    from collections.abc import Callable

    from tai42_contract.accounts.provider import AccountsProvider

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, Callable[..., AccountsProvider]] = {}


def register_accounts_provider(name: str, factory: Callable[..., AccountsProvider]) -> None:
    """Register a named accounts-provider factory in BOTH registries.

    Registers ``factory`` here and — because an accounts provider is the
    identity answerer for its own session tokens — into the identity
    registry under the same name. A duplicate in either registry raises.
    """
    if name in _REGISTRY:
        raise ValueError(f"Accounts provider {name!r} already registered")
    register_identity_provider(name, factory)
    _REGISTRY[name] = factory
    logger.info("accounts: registered accounts provider %s", name)


def get_accounts_provider_factory(name: str) -> Callable[..., AccountsProvider]:
    """Return the factory registered under ``name``; unknown names raise KeyError."""
    factory = _REGISTRY.get(name)
    if factory is None:
        raise KeyError(f"Unknown accounts provider: {name!r}")
    return factory


def iter_accounts_provider_factories() -> list[tuple[str, Callable[..., AccountsProvider]]]:
    """Name-sorted snapshot of all registered factories.

    Returns a new list: callers may iterate freely without touching registry
    state. Sorted for deterministic aggregator output.
    """
    return sorted(_REGISTRY.items())


def reset_registry() -> None:
    """Clear the accounts registrations (application start() re-imports plugin modules).

    Clears only this registry: the application's start() resets the identity
    registry itself before re-importing plugin modules.
    """
    _REGISTRY.clear()


__all__ = [
    "get_accounts_provider_factory",
    "iter_accounts_provider_factories",
    "register_accounts_provider",
    "reset_registry",
]
