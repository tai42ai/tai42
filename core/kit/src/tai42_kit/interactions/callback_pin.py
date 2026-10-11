"""The callback-URL pin: a stored callback URL receives an answer only when it is the exact shape the platform mints.

A callback URL is a bearer capability (its last segment is the ticket). Every component that POSTs an
answer to a stored one first pins it against the platform's callback prefix, so a poisoned or stale
entry can never ship a participant's answer to another host or another route. Pure: no I/O.
"""

from __future__ import annotations

import re
from enum import StrEnum
from urllib.parse import urlsplit

from tai42_contract.errors import ErrorKind

__all__ = ["CallbackPinReason", "CallbackUrlRefusedError", "pin_callback_url"]

# The alphabet ``secrets.token_urlsafe`` emits: the ticket the platform mints is one such segment.
_TICKET_RE = re.compile(r"[A-Za-z0-9_-]+")
_DEFAULT_PORTS = {"https": 443, "http": 80}


class CallbackPinReason(StrEnum):
    """Why a stored callback URL was refused."""

    HOST_MISMATCH = "host_mismatch"
    PATH_MISMATCH = "path_mismatch"
    MALFORMED_URL = "malformed_url"


class CallbackUrlRefusedError(ValueError):
    """A stored callback URL is not the shape the platform mints; nothing may be sent to it.

    The message and the attributes name the reason and the target host only: the URL itself embeds
    the ticket and never appears.
    """

    __tai_error_kind__ = ErrorKind.BAD_INPUT

    def __init__(self, reason: CallbackPinReason, *, target_host: str | None) -> None:
        """Name the refusal by its ``reason`` and the URL's ``target_host`` (``None`` when it has none)."""
        super().__init__(f"callback URL refused ({reason.value}); target host {target_host!r}")
        self.reason = reason
        self.target_host = target_host


def _effective_port(scheme: str, port: int | None) -> int | None:
    return port if port is not None else _DEFAULT_PORTS.get(scheme)


def pin_callback_url(callback_url: str, *, prefix: str) -> None:
    """Accept ``callback_url`` only when it is ``prefix`` followed by exactly one ticket segment.

    ``prefix`` is the absolute callback prefix (it ends with ``/``). Accepted: the same scheme, the
    same host (case-insensitive), the same effective port (an explicit default port is the same
    origin), no userinfo, no query, no fragment, no whitespace or control character, and a path equal
    to the prefix's path plus one ``[A-Za-z0-9_-]+`` segment. Raises :class:`CallbackUrlRefusedError`
    otherwise: ``MALFORMED_URL`` for an unparseable port or a control character, ``HOST_MISMATCH``
    for another scheme, host or port, ``PATH_MISMATCH`` for anything else.
    """
    base = urlsplit(prefix)
    if not base.path.endswith("/"):
        raise ValueError("callback prefix must end with '/'")
    target = urlsplit(callback_url)
    target_host = target.hostname
    if any(char <= " " or char == "\x7f" for char in callback_url):
        raise CallbackUrlRefusedError(CallbackPinReason.MALFORMED_URL, target_host=target_host)
    try:
        target_port = target.port
    except ValueError:
        raise CallbackUrlRefusedError(CallbackPinReason.MALFORMED_URL, target_host=target_host) from None
    if (
        target.scheme != base.scheme
        or target_host != base.hostname
        or _effective_port(target.scheme, target_port) != _effective_port(base.scheme, base.port)
    ):
        raise CallbackUrlRefusedError(CallbackPinReason.HOST_MISMATCH, target_host=target_host)
    ticket = target.path.removeprefix(base.path) if target.path.startswith(base.path) else ""
    if (
        target.username is not None
        or target.password is not None
        or "?" in callback_url
        or "#" in callback_url
        or not _TICKET_RE.fullmatch(ticket)
    ):
        raise CallbackUrlRefusedError(CallbackPinReason.PATH_MISMATCH, target_host=target_host)
