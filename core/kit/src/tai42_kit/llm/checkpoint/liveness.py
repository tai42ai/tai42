"""Live-thread filters: each consumer registers the test that knows which of its own threads are live.

No single store knows every live checkpoint thread, so before the checkpoint sweep deletes a thread
past its horizon it asks every registered filter. A filter receives the store (provider and
connection string) and a page of candidate thread ids, and returns the subset it knows to be live;
a claimed thread is spared.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from types import MappingProxyType
from typing import Final

LiveThreadFilter = Callable[[str, str | None, Sequence[str]], Awaitable[Collection[str]]]

_OWNER_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_-]*$")

_filters: dict[str, LiveThreadFilter] = {}


def register_live_thread_filter(owner: str, fn: LiveThreadFilter) -> None:
    """Register ``fn`` as ``owner``'s live-thread filter; an owner registers once."""
    if not _OWNER_PATTERN.fullmatch(owner):
        raise ValueError(f"live-thread filter owner {owner!r} must match {_OWNER_PATTERN.pattern}")
    if owner in _filters:
        raise ValueError(f"live-thread filter {owner!r} is already registered")
    _filters[owner] = fn


def live_thread_filters() -> Mapping[str, LiveThreadFilter]:
    """A read-only view of the registered filters by owner."""
    return MappingProxyType(_filters)


async def live_threads(provider: str, conn_string: str | None, thread_ids: Sequence[str]) -> dict[str, str]:
    """Map each candidate a registered filter claims to the owner that claimed it.

    Every filter is called; a filter's raise propagates.
    """
    claimed: dict[str, str] = {}
    if not thread_ids:
        return claimed
    candidates = set(thread_ids)
    for owner, fn in list(_filters.items()):
        for thread_id in await fn(provider, conn_string, thread_ids):
            if thread_id in candidates:
                claimed.setdefault(thread_id, owner)
    return claimed
