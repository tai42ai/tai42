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

from tai42_contract.access_control.registry import register_identity_provider, same_factory

if TYPE_CHECKING:
    from collections.abc import Callable

    from tai42_contract.accounts.provider import AccountsProvider

logger = logging.getLogger(__name__)

# ``_REGISTRY`` is the COMMITTED generation; ``_pending`` the generation an epoch
# build stages into, promoted atomically on success and dropped on failure,
# mirroring the identity registry. The accounts and identity registries stage/commit
# TOGETHER (the skeleton drives both), so this dual-registering module stays coherent.
_REGISTRY: dict[str, Callable[..., AccountsProvider]] = {}
_pending: dict[str, Callable[..., AccountsProvider]] | None = None


def _write_target() -> dict[str, Callable[..., AccountsProvider]]:
    return _pending if _pending is not None else _REGISTRY


def register_accounts_provider(name: str, factory: Callable[..., AccountsProvider]) -> None:
    """Register a named accounts-provider factory in BOTH registries.

    Registers ``factory`` here and — because an accounts provider is the
    identity answerer for its own session tokens — into the identity
    registry under the same name. Both registries stage together, so a staged
    accounts registration lands beside its staged identity twin.

    RELOAD-SAFE: re-registering the SAME factory (by
    :func:`~tai42_contract.access_control.registry.same_factory`) under a name it
    already holds is a quiet no-op in BOTH registries, so the hot-reload primitive
    re-executing this plugin's module body does not raise. A DIFFERENT factory under
    an already-registered name still raises loudly, in either registry. The accounts
    no-op returns before touching the identity registry, and the identity registry
    is only written when this name is new here, so the two registries never drift.
    """
    target = _write_target()
    existing = target.get(name)
    if existing is not None:
        if same_factory(existing, factory):
            logger.debug("accounts: accounts provider %s re-registered (reload no-op)", name)
            return
        raise ValueError(f"Accounts provider {name!r} already registered")
    register_identity_provider(name, factory)
    target[name] = factory
    logger.info("accounts: registered accounts provider %s", name)


def get_accounts_provider_factory(name: str) -> Callable[..., AccountsProvider]:
    """Return the factory registered under ``name`` in the COMMITTED generation;
    unknown names raise KeyError."""
    factory = _REGISTRY.get(name)
    if factory is None:
        raise KeyError(f"Unknown accounts provider: {name!r}")
    return factory


def iter_accounts_provider_factories() -> list[tuple[str, Callable[..., AccountsProvider]]]:
    """Name-sorted snapshot of the COMMITTED factories.

    Returns a new list: callers may iterate freely without touching registry
    state. Sorted for deterministic aggregator output.
    """
    return sorted(_REGISTRY.items())


def iter_accounts_provider_factories_staged() -> list[tuple[str, Callable[..., AccountsProvider]]]:
    """Name-sorted snapshot of the STAGED generation if a build is staging, else the
    committed one — the build's own accessor (the configured-providers boot check,
    kind status)."""
    return sorted(_write_target().items())


def reset_registry() -> None:
    """Clear the write-target accounts registrations — the STAGED generation while a
    build is staging (never the committed one), else the committed map (boot, test
    isolation). Clears only this registry: the identity registry has its own lifecycle."""
    _write_target().clear()


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


__all__ = [
    "abort_staging",
    "begin_staging",
    "commit_staging",
    "get_accounts_provider_factory",
    "iter_accounts_provider_factories",
    "iter_accounts_provider_factories_staged",
    "register_accounts_provider",
    "reset_registry",
]
