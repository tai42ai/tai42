"""Module-level accounts-provider registry — populated by direct import.

Handle-free for the same reason as the identity-provider registry
(:mod:`tai42_kit.access_control.registry`): accounts plugins never import the application package and
register at plain module import, before any app handle is usable. The application resolves and
ACTIVATES providers from its own configuration — registration alone activates nothing.

Two differences from the identity registry:

- :func:`register_accounts_provider` ALSO registers the factory into the identity registry under the
  same name — an accounts provider IS the token answerer for its own sessions, so registering into only
  one registry would make sessions mintable but not validatable (or vice versa). One plugin call, both
  registries, loud duplicate errors from either. The application stages and commits the two
  registries together.
- The registry is enumerable: the public login-methods aggregator asks EVERY registered accounts
  provider for its declared methods.
"""

from __future__ import annotations

from collections.abc import Callable

from tai42_contract.accounts.provider import AccountsProvider

from tai42_kit.access_control.registry import register_identity_provider
from tai42_kit.registry import NamedFactoryRegistry

_PROVIDERS: NamedFactoryRegistry[Callable[..., AccountsProvider]] = NamedFactoryRegistry("Accounts provider")


def register_accounts_provider(name: str, factory: Callable[..., AccountsProvider]) -> None:
    """Register a named accounts-provider factory in BOTH registries.

    RELOAD-SAFE: re-registering the SAME factory under a name it already holds is a quiet no-op in BOTH
    registries; a DIFFERENT factory under a held name raises ``ValueError``, in either registry. The
    accounts no-op returns before touching the identity registry, and the identity registry is written
    only when this name is new here (a refusal there leaves this registry untouched), so the two never
    drift.
    """
    _PROVIDERS.register(name, factory, before_add=lambda: register_identity_provider(name, factory))


def get_accounts_provider_factory(name: str) -> Callable[..., AccountsProvider]:
    """The factory under ``name`` in the COMMITTED generation; ``KeyError`` when unknown."""
    return _PROVIDERS.get(name)


def iter_accounts_provider_factories() -> list[tuple[str, Callable[..., AccountsProvider]]]:
    """A fresh name-sorted snapshot of the COMMITTED factories."""
    return _PROVIDERS.items()


def iter_accounts_provider_factories_staged() -> list[tuple[str, Callable[..., AccountsProvider]]]:
    """A fresh name-sorted snapshot of the STAGED generation while a build stages, else the committed one.

    The build's own accessor (the configured-providers boot check, kind status).
    """
    return _PROVIDERS.items_staged()


def reset_registry() -> None:
    """Clear the accounts write target only; the identity registry has its own lifecycle."""
    _PROVIDERS.reset()


def begin_staging() -> None:
    """Open a fresh staged generation the epoch build registers into."""
    _PROVIDERS.begin_staging()


def commit_staging() -> None:
    """Promote the staged generation in one reference assignment; a no-op when none is open."""
    _PROVIDERS.commit_staging()


def abort_staging() -> None:
    """Drop the staged generation on a failed build; a no-op when none is open."""
    _PROVIDERS.abort_staging()


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
