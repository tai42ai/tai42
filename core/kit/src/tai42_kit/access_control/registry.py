"""Module-level identity-provider registry — populated by direct import.

An identity plugin cannot register through the bound ``tai42_app`` handle: the plugin depends on
tai42-contract and tai42-kit only and bans ``tai42_skeleton``, and ``tai42_app`` raises on ANY member
access before ``bind()`` runs (``bind()`` runs only inside ``start()``), so a handle-based registration
would crash at plugin import in any process that imports the plugin before ``start()`` — the metrics
entrypoint never calls ``start()`` at all. So the plugin imports this module and calls
:func:`register_identity_provider` at its own module import. The registry is plain module state: it
fills in ANY process that imports the plugin module, with no ``bind()`` anywhere.

The registry stores FACTORIES, not descriptors: an identity provider is a live object holding a
store/connection, so a plugin registers a callable that builds the provider from settings. The
application drives the staging lifecycle; this module stays epoch-free.
"""

from __future__ import annotations

from collections.abc import Callable

from tai42_contract.access_control.identity import IdentityProvider

from tai42_kit.registry import NamedFactoryRegistry

_PROVIDERS: NamedFactoryRegistry[Callable[..., IdentityProvider]] = NamedFactoryRegistry("Identity provider")


def register_identity_provider(name: str, factory: Callable[..., IdentityProvider]) -> None:
    """Register a named identity-provider factory.

    RELOAD-SAFE: re-registering the SAME factory (by :func:`tai42_kit.registry.same_factory`) under a
    name it already holds is a quiet no-op; a DIFFERENT factory under that name raises ``ValueError``.
    """
    _PROVIDERS.register(name, factory)


def get_identity_provider_factory(name: str) -> Callable[..., IdentityProvider]:
    """Resolve a factory from the COMMITTED generation — the request-path accessor; ``KeyError`` when unknown."""
    return _PROVIDERS.get(name)


def get_identity_provider_factory_staged(name: str) -> Callable[..., IdentityProvider]:
    """Resolve a factory from the STAGED generation while a build stages, else the committed one.

    The build's own accessor (startup probe, kind status), so a build decides against the generation
    it is assembling.
    """
    return _PROVIDERS.get_staged(name)


def iter_identity_provider_names_staged() -> list[str]:
    """Name-sorted names of the identity providers in the generation being resolved.

    Reads the STAGED generation while a build stages, else the committed one. The derived default for
    the access-control auth-provider chain: when no chain is configured, every registered identity
    provider is resolved.
    """
    return _PROVIDERS.names_staged()


def reset_registry() -> None:
    """Clear the write target — the staged generation while a build stages, else the committed one."""
    _PROVIDERS.reset()


def begin_staging() -> None:
    """Open a fresh staged generation an epoch build registers into; the committed one keeps serving."""
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
    "get_identity_provider_factory",
    "get_identity_provider_factory_staged",
    "iter_identity_provider_names_staged",
    "register_identity_provider",
    "reset_registry",
]
