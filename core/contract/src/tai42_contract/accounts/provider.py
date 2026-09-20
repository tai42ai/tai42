"""The accounts provider contract: user accounts, login flows, sessions."""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, Literal, Protocol, runtime_checkable

from tai42_contract.access_control.identity import IdentityProvider
from tai42_contract.accounts.models import LoginAttachment, LoginCredential, LoginMethod


@runtime_checkable
class AccountsAdminServices(Protocol):
    """Application-side policy services an accounts provider may invoke.

    Implemented by the application and INJECTED via
    ``AccountsProviderSettings.admin`` — accounts plugins never import the
    application package, so this Protocol is the only way plugin code can
    create a principal, apply a role template, remove a principal's policy,
    or flip the disabled marker. Every method mutates application-owned
    principal and policy state; the plugin never touches that state directly.
    """

    async def create_principal(
        self,
        user_id: str,
        *,
        kind: Literal["human", "service"],
        display_name: str,
        created_by: str | None,
        role: str,
    ) -> None:
        """Create the principal row and apply its role.

        A ``human`` principal authenticates through an accounts provider; a
        ``service`` principal holds keys only. ``created_by`` is the principal
        id that created this one, or ``None`` for the setup door. Raises loudly
        if the principal already exists or the role is unknown.
        """
        ...

    async def apply_role(self, user_id: str, role: str) -> None:
        """Copy the named role template into the principal's enforced policy."""
        ...

    async def remove_policy(self, user_id: str) -> None:
        """Delete the principal's enforced policy and row (and revoke keys it owned)."""
        ...

    async def set_user_disabled(self, user_id: str, disabled: bool) -> None:
        """Set/clear the disabled marker on the principal."""
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


class LoginAttachingProvider(AccountsProvider):
    """An accounts provider that can attach an interactive login to an existing principal.

    A provider mixes this in when it owns an interactive credential it can set
    for a principal (a password, or a one-time invite link). The setup door and
    the invite flow ``isinstance``-check for it: a provider whose login lives at
    an external issuer (OIDC) does not implement it, and the caller reports the
    login as not attached.

    Ownership of a principal's disable/delete follows the login: the principals
    door asks every registered attaching provider :meth:`has_login`, and a
    principal some provider claims is managed through that provider's users door
    (which cleans its login row and runs its own guard), never the principals
    door.
    """

    @abstractmethod
    async def has_login(self, user_id: str) -> bool:
        """Whether this provider holds a login for the principal ``user_id``.

        The principals door reads this to decide who owns a principal's
        disable/delete: a ``True`` means the provider holds the credential, so the
        principal is managed through that provider's users door; a ``False`` means
        it does not (a service principal, an OIDC-provisioned human whose login
        lives at the issuer, the keys-only owner), and the principals door manages
        it directly. A backend error raises (fail closed), never a silent ``False``.
        """
        ...

    @abstractmethod
    async def attach_login(self, user_id: str, *, credential: LoginCredential) -> LoginAttachment:
        """Attach ``credential`` to the existing principal ``user_id``.

        A :class:`~tai42_contract.accounts.models.PasswordCredential` sets the
        password now; an :class:`~tai42_contract.accounts.models.InviteCredential`
        mints a one-time link returned on the :class:`~tai42_contract.accounts.models.LoginAttachment`.
        A correctable-input failure (a too-short password) raises
        :class:`~tai42_contract.accounts.errors.LoginAttachError`; a login already
        existing for the principal or a taken email raises
        :class:`~tai42_contract.accounts.errors.LoginConflictError`. The state is
        unchanged on either, so the caller surfaces the failure and stays retriable.
        """
        ...


__all__ = [
    "AccountsAdminServices",
    "AccountsProvider",
    "AccountsProviderSettings",
    "LoginAttachingProvider",
]
