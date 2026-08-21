"""Request-scoped caller-identity context.

The host binds the calling user's id into a context variable for the duration of
each request; any plugin reads it back through :func:`get_current_user_id`
without depending on the host. The value defaults to ``None`` — an anonymous
caller, or any code path running outside a bound request.

Alongside the id, the host binds whether the caller is authorized to READ SECRETS
(the ``action=secret`` admin fence) into a parallel variable read back through
:func:`caller_may_read_secrets`. It defaults to ``False`` — fail-closed: an
anonymous caller, or code running outside a bound request, is never treated as
secret-capable. A plugin gates a host-secret-exposing primitive on it so the reach
of that primitive is by construction identical to the reach of the secret fence.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

__all__ = [
    "caller_may_read_secrets",
    "get_current_user_id",
    "reset_request_secret_capability",
    "reset_request_user_id",
    "set_request_secret_capability",
    "set_request_user_id",
]

_current_user_id: ContextVar[str | None] = ContextVar("tai42_current_user_id", default=None)
_current_secret_capability: ContextVar[bool] = ContextVar("tai42_current_secret_capability", default=False)


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


def caller_may_read_secrets() -> bool:
    """Whether the current caller is authorized to read the platform's secrets — the
    ``action=secret`` admin fence.

    Defaults to ``False`` (fail-closed): no caller bound, an anonymous request, or
    code running outside a bound request is never secret-capable. The host binds the
    authoritative value once per request, computed from the same admin discriminator
    the secret fence enforces."""
    return _current_secret_capability.get()


def set_request_secret_capability(capable: bool) -> Token[bool]:
    """Bind whether the current caller may read secrets and return the reset token.

    The host calls this once per authenticated request, paired with
    :func:`set_request_user_id`; pass the returned token to
    :func:`reset_request_secret_capability` to restore the previous value.
    """
    return _current_secret_capability.set(capable)


def reset_request_secret_capability(token: Token[bool]) -> None:
    """Restore the secret-read capability to the value captured in ``token`` by the
    matching :func:`set_request_secret_capability` call."""
    _current_secret_capability.reset(token)
