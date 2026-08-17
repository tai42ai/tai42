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
# settings). ``_REGISTRY`` is the COMMITTED generation the request path resolves
# against; ``_pending`` is the generation an epoch build stages into, promoted to
# committed in ONE reference assignment (atomic under the GIL) only if the build
# succeeds. A failed build drops ``_pending`` and never touches ``_REGISTRY``.
# This module stays epoch-free — the skeleton owns the staging lifecycle.
_REGISTRY: dict[str, Callable[..., IdentityProvider]] = {}
_pending: dict[str, Callable[..., IdentityProvider]] | None = None


def _write_target() -> dict[str, Callable[..., IdentityProvider]]:
    """Where a registration lands: the staged generation while a build is staging,
    else the committed registry (boot writes straight to the empty committed map).
    Safe without thread-locals: the reload gate serialises builds one at a time and
    nothing on the serving path registers."""
    return _pending if _pending is not None else _REGISTRY


def same_factory(existing: object, factory: object) -> bool:
    """Whether two provider factories denote the SAME provider — the reload-safety
    predicate for both this registry and the accounts registry.

    True when the factories are the same object, OR when they share ``__module__``
    and ``__qualname__``: the hot-reload primitive pops a plugin's modules and
    re-executes their bodies, minting a FRESH class object each pass, so a
    re-registration after a reload carries a new object that is nonetheless the
    same declared provider. A genuinely different provider has a different qualified
    name, so a real name collision still reads as different. Lambdas (qualname
    ``<lambda>``) never match across distinct objects — a factory with no stable
    qualified identity is treated as different, never silently coalesced."""
    if existing is factory:
        return True
    existing_qualname = getattr(existing, "__qualname__", None)
    if existing_qualname is None or "<lambda>" in existing_qualname:
        return False
    same_module = getattr(existing, "__module__", None) == getattr(factory, "__module__", None)
    return same_module and existing_qualname == getattr(factory, "__qualname__", None)


def register_identity_provider(name: str, factory: Callable[..., IdentityProvider]) -> None:
    """Register a named identity-provider factory. RELOAD-SAFE: re-registering the
    SAME factory (by :func:`same_factory`) under a name it already holds is a quiet
    no-op, so the hot-reload primitive re-executing a plugin's module body does not
    raise; a DIFFERENT factory under an already-registered name still raises loudly."""
    target = _write_target()
    existing = target.get(name)
    if existing is not None:
        if same_factory(existing, factory):
            logger.debug("access_control: identity provider %s re-registered (reload no-op)", name)
            return
        raise ValueError(f"Identity provider {name!r} already registered")
    target[name] = factory
    logger.info("access_control: registered identity provider %s", name)


def get_identity_provider_factory(name: str) -> Callable[..., IdentityProvider]:
    """Resolve a factory from the COMMITTED generation — the request-path accessor
    (the live verifier's lazy resolve). Never sees a mid-build staged registration."""
    factory = _REGISTRY.get(name)
    if factory is None:
        raise KeyError(f"Unknown identity provider: {name!r}")
    return factory


def get_identity_provider_factory_staged(name: str) -> Callable[..., IdentityProvider]:
    """Resolve a factory from the STAGED generation if a build is staging, else the
    committed one — the build's own accessor (quarantine abort, startup probe, kind
    status), so a build decides against the generation it is assembling."""
    factory = _write_target().get(name)
    if factory is None:
        raise KeyError(f"Unknown identity provider: {name!r}")
    return factory


def reset_registry() -> None:
    """Clear the write-target registry — the STAGED generation while a build is staging
    (``start()`` clears the fresh staged map before it re-imports the plugin modules,
    never the committed one, so a reload leaves the live registry untouched), else
    the committed map (boot, and test isolation)."""
    _write_target().clear()


def begin_staging() -> None:
    """Open a fresh staged generation an epoch build registers into, leaving the
    committed registry serving the live generation untouched."""
    global _pending
    _pending = {}


def commit_staging() -> None:
    """Promote the staged generation to committed in one reference assignment (atomic
    under the GIL). A no-op if no build staged."""
    global _REGISTRY, _pending
    if _pending is not None:
        _REGISTRY = _pending  # pyright: ignore[reportConstantRedefinition]  # atomic generation swap
        _pending = None


def abort_staging() -> None:
    """Drop the staged generation on a failed build — the committed registry never saw
    any of its registrations."""
    global _pending
    _pending = None


__all__ = [
    "abort_staging",
    "begin_staging",
    "commit_staging",
    "get_identity_provider_factory",
    "get_identity_provider_factory_staged",
    "register_identity_provider",
    "reset_registry",
    "same_factory",
]
