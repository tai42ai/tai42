"""Errors an accounts provider raises through the contract.

Each family lets a generic application door catch ONE base type and reach every failure a
caller can correct, without importing any provider-private exception type — an accounts
plugin never imports the application package. Each carries the stable
:class:`~tai42_contract.errors.ErrorKind` the caller maps to a transport status.

:class:`LoginAttachError` is raised by ``attach_login`` (the setup/invite flow); the
:class:`MemberActionError` family is raised by ``invoke_member_action`` so the generic
invoke operation surfaces a provider's typed failure at the right status instead of a
generic server error. :class:`LastAdminError` is raised by the application's guarded
:class:`~tai42_contract.accounts.provider.AccountsAdminServices` methods, so a provider
reaches the last-admin refusal without importing the application package. The platform
reads only the typed kind and surfaces the message; it reads no provider-specific content.
"""

from __future__ import annotations

from tai42_contract.errors import ErrorKind


class LoginAttachError(Exception):
    """Base for a login attach the caller rejected on correctable input.

    Raised by :meth:`~tai42_contract.accounts.provider.LoginAttachingProvider.attach_login`
    when the credential itself is bad (a too-short password, say). The setup door
    maps it to a ``400`` and surfaces the message so the operator can retry.
    """

    # A correctable-input attach failure — a rejected input.
    __tai_error_kind__ = ErrorKind.BAD_INPUT


class LoginConflictError(LoginAttachError):
    """A login already exists for the principal, or the email is taken.

    The principal already has a login row, or another account owns the email. The
    setup door maps it to a ``409``; the state is unchanged, so the operator resolves
    the collision and retries.
    """

    # A login/email collision — a state conflict.
    __tai_error_kind__ = ErrorKind.CONFLICT


class MemberActionError(Exception):
    """Base for a member action a provider rejected with a correctable, typed failure.

    Raised by :meth:`~tai42_contract.accounts.provider.AccountsProvider.invoke_member_action`
    so the generic invoke operation reaches every correctable failure without a provider
    importing the application package. The base is a rejected input the caller can fix (the
    invoke operation maps it to ``422``); the subclasses name the other correctable outcomes.
    The message is surfaced to the caller; the platform reads only the typed kind, never any
    provider-specific content. A provider carries no action-specific vocabulary on these — it
    classifies its own failure into the generic kind.
    """

    # A correctable-input member-action failure — a rejected input.
    __tai_error_kind__ = ErrorKind.BAD_INPUT


class MemberActionNotFoundError(MemberActionError):
    """The action's target does not exist. The invoke operation maps it to ``404``."""

    # An unknown target — not found.
    __tai_error_kind__ = ErrorKind.NOT_FOUND


class MemberActionConflictError(MemberActionError):
    """The action conflicts with current state. The invoke operation maps it to ``409``.

    The state is unchanged, so the caller resolves the conflict and retries.
    """

    # A state conflict.
    __tai_error_kind__ = ErrorKind.CONFLICT


class MemberActionBadRequestError(MemberActionError):
    """The request to the action was malformed. The invoke operation maps it to ``400``."""

    # A malformed request.
    __tai_error_kind__ = ErrorKind.BAD_INPUT


class LastAdminError(Exception):
    """The change would leave no enabled admin principal; refused, nothing written."""

    # The deployment's admin standing forbids the change — a state conflict.
    __tai_error_kind__ = ErrorKind.CONFLICT


__all__ = [
    "LastAdminError",
    "LoginAttachError",
    "LoginConflictError",
    "MemberActionBadRequestError",
    "MemberActionConflictError",
    "MemberActionError",
    "MemberActionNotFoundError",
]
