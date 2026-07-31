"""Request-scoped caller-identity context.

The host binds the calling user's id into a context variable for the duration of
each request; any plugin reads it back through :func:`get_current_user_id`
without depending on the host. The value defaults to ``None`` — an anonymous
caller, or any code path running outside a bound request.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

__all__ = [
    "get_current_user_id",
    "reset_request_user_id",
    "set_request_user_id",
]

_current_user_id: ContextVar[str | None] = ContextVar("tai42_current_user_id", default=None)


def get_current_user_id() -> str | None:
    """Return the calling user's id for the current request, or ``None`` when no
    caller is bound — an anonymous request, or code running outside a request."""
    return _current_user_id.get()


def set_request_user_id(user_id: str | None) -> Token[str | None]:
    """Bind ``user_id`` as the current context's caller and return the reset token.

    The host calls this once per request; pass the returned token to
    :func:`reset_request_user_id` to restore the previous value.
    """
    return _current_user_id.set(user_id)


def reset_request_user_id(token: Token[str | None]) -> None:
    """Restore the caller id to the value captured in ``token`` by the matching
    :func:`set_request_user_id` call."""
    _current_user_id.reset(token)
