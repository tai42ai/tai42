"""The bridge thread-id grammar: the one place a thread id is built and parsed.

A route-keyed thread is ``bridge:{route_name}:{client_address}``; a LINKED person's aggregated
thread is ``bridge:@person:{person_id}``. A route name carries no ``:`` (``ROUTE_NAME_RE``), so the
first ``:`` after the prefix ends the route segment exactly, and ``@person`` can never be a route
name. Pure string functions, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from tai42_contract.errors import ErrorKind

# The reserved thread namespace the messaging bridge alone writes.
BRIDGE_THREAD_PREFIX: Final = "bridge:"

# The sub-namespace a LINKED person's aggregated thread is keyed under.
PERSON_THREAD_PREFIX: Final = f"{BRIDGE_THREAD_PREFIX}@person:"


@dataclass(frozen=True)
class RouteThreadId:
    """A parsed route-keyed thread id."""

    route_name: str
    client_address: str


@dataclass(frozen=True)
class PersonThreadId:
    """A parsed person thread id."""

    person_id: str


class ThreadIdError(ValueError):
    """A thread id outside the bridge grammar."""

    __tai_error_kind__ = ErrorKind.BAD_INPUT


def route_thread_id(route_name: str, client_address: str) -> str:
    """The thread id of ``client_address``'s conversation on route ``route_name``."""
    return f"{BRIDGE_THREAD_PREFIX}{route_name}:{client_address}"


def person_thread_id(person_id: str) -> str:
    """The aggregated thread id of the linked person ``person_id``."""
    return f"{PERSON_THREAD_PREFIX}{person_id}"


def parse_thread_id(thread_id: str) -> RouteThreadId | PersonThreadId:
    """The route-keyed or person thread ``thread_id`` names.

    Raises :class:`ThreadIdError` for an id outside the ``bridge:`` namespace, a person thread with
    an empty id, and a route-keyed id with an empty route or an empty address.
    """
    if thread_id.startswith(PERSON_THREAD_PREFIX):
        person_id = thread_id[len(PERSON_THREAD_PREFIX) :]
        if not person_id:
            raise ThreadIdError(f"person thread {thread_id!r} names no person")
        return PersonThreadId(person_id=person_id)
    if not thread_id.startswith(BRIDGE_THREAD_PREFIX):
        raise ThreadIdError(f"thread {thread_id!r} is not a reserved bridge thread")
    route_name, _, client_address = thread_id[len(BRIDGE_THREAD_PREFIX) :].partition(":")
    if not route_name or not client_address:
        raise ThreadIdError(f"thread {thread_id!r} is not a route-keyed bridge thread")
    return RouteThreadId(route_name=route_name, client_address=client_address)


def person_id_of(thread_id: str) -> str | None:
    """The person a person thread id names, or ``None`` for any other id (a route-keyed one included)."""
    try:
        parsed = parse_thread_id(thread_id)
    except ThreadIdError:
        return None
    return parsed.person_id if isinstance(parsed, PersonThreadId) else None
