"""Errors a login-attaching accounts provider raises.

One family so the setup door catches :class:`LoginAttachError` and reaches every
attach failure a human can correct, without importing any provider-private
exception type. Each carries the stable :class:`~tai42_contract.errors.ErrorKind`
the caller maps to a transport status (a bad credential is a rejected input, a
login/email collision is a conflict).
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


__all__ = [
    "LoginAttachError",
    "LoginConflictError",
]
