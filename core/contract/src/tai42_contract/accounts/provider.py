"""The accounts provider contract: user accounts, login flows, sessions."""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, Protocol, runtime_checkable

from tai42_contract.access_control.identity import IdentityProvider
from tai42_contract.accounts.models import LoginMethod


@runtime_checkable
class AccountsAdminServices(Protocol):
    """Application-side policy services an accounts provider may invoke.

    Implemented by the application and INJECTED via
    ``AccountsProviderSettings.admin`` — accounts plugins never import the
    application package, so this Protocol is the only way plugin code can
    apply a role template, remove a principal's policy, or flip the disabled
    marker. Every method mutates application-owned policy state; the plugin
    never touches that state directly.
    """

    async def apply_role(self, user_id: str, role: str) -> None:
        """Copy the named role template into the user's enforced policy."""
        ...

    async def remove_policy(self, user_id: str) -> None:
        """Delete the user's enforced policy (and revoke keys it owned)."""
        ...

    async def set_user_disabled(self, user_id: str, disabled: bool) -> None:
        """Set/clear the disabled marker on the user's enforced policy."""
        ...


@runtime_checkable
class AccountsProviderSettings(Protocol):
    """Settings shape handed to an accounts-provider factory.

    ``redis`` and ``admin`` are typed loosely for the same reason
    ``IdentityProviderSettings.redis`` is ``Any``: the contract cannot name
    application or kit types. ``admin`` carries the application's
    ``AccountsAdminServices`` implementation.
    """

    redis: Any
    admin: Any


class AccountsProvider(IdentityProvider):
    """A user-accounts provider: login methods plus session-token validation.

    An accounts provider owns human accounts and the login flows that mint
    session tokens for them. It IS an identity provider: the session tokens
    it mints are validated through the inherited ``validate_token`` — the
    same seam every credential passes through — so installing an accounts
    provider never adds a second enforcement pathway.

    Session tokens are opaque strings minted and stored by the provider
    (recommended prefix ``tai-sess-`` to distinguish them from ``sk-`` API
    keys at a glance); the contract never parses token contents. Storage,
    hashing, and lifetime are provider-owned.

    Login/lifecycle HTTP routes (submit endpoints, redirect flows) are
    shipped by the provider plugin as ordinary router modules; the contract
    carries only the metadata that lets a generic login screen render them.
    """

    @abstractmethod
    def login_methods(self) -> list[LoginMethod]:
        """Declare the login methods this provider offers.

        Called by the application's public login-methods aggregator. Must be
        cheap and side-effect free: this is static, config-derived metadata,
        not I/O (sync by contract, like ``readiness_targets``).
        """
        ...

    @abstractmethod
    async def needs_bootstrap(self) -> bool:
        """Whether this provider still needs its first account created.

        A LIVE store read (async by contract): the aggregator calls it on
        every methods fetch so the owner-creation screen disappears the
        moment the owner exists. Providers with no bootstrap concept return
        ``False``.
        """
        ...

    @abstractmethod
    async def revoke_session(self, token: str) -> bool:
        """Revoke the session behind ``token`` if it is this provider's.

        Returns ``True`` when a session was found and revoked; ``False``
        when the token is not this provider's (wrong prefix, unknown). The
        application's single logout route dispatches across ALL registered
        accounts providers, so implementations must answer ``False`` for
        foreign tokens instead of raising. Backend errors still raise
        (fail closed).
        """
        ...


__all__ = ["AccountsAdminServices", "AccountsProvider", "AccountsProviderSettings"]
