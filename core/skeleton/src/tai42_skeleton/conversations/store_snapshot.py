"""A per-process snapshot of an indexed Redis row store, tagged with the store's write token.

Each write script of such a store sets the store's version key to a fresh token in the same
atomic unit as its write, so a reader that holds a snapshot under the token it reads back holds
exactly the store's current rows. A token rather than a counter: after a flush the key is absent
and every later write mints a token no snapshot holds, so no snapshot can match a different
store state.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass


def new_write_token() -> str:
    """A fresh write token for one store write."""
    return uuid.uuid4().hex


@dataclass(frozen=True)
class StoreSnapshot[KeyT, ModelT]:
    """The rows of an indexed store as loaded under ``token``.

    ``unreadable`` holds the index members whose row was missing or unparseable at load; a
    single-row read of one of them reads the row live, as an uncached read would.
    """

    token: str | None
    rows: Mapping[KeyT, ModelT]
    unreadable: frozenset[str]
