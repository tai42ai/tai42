"""An in-process stand-in for PostgreSQL session-level advisory locks, exclusive and shared.

Each :meth:`SessionAdvisoryKeys.pinned_connection` is one database session. The statements a
marketplace lock runs are answered as PostgreSQL answers them:

- ``pg_try_advisory_lock(key)`` takes the key exclusively when no OTHER session holds it in
  either mode, and returns whether it did;
- ``pg_advisory_lock_shared(key)`` waits while another session holds the key exclusively, then
  takes it shared; a session holding the key exclusively takes it shared at once, beside its
  exclusive hold;
- ``pg_advisory_unlock(key)`` / ``pg_advisory_unlock_shared(key)`` release one hold of that mode
  held by this session, and return whether there was one.

Closing a session (the pinned connection's context exits) releases every hold it still has,
as the real session end does.
"""

from __future__ import annotations

import asyncio
import itertools
import re
from collections import Counter
from contextlib import asynccontextmanager
from typing import Any

_CALL = re.compile(r"SELECT (pg_\w+)\(%s\)")


class SessionAdvisoryKeys:
    """The advisory keys of one database, held by sessions in exclusive or shared mode."""

    def __init__(self) -> None:
        self._exclusive: dict[int, Counter[int]] = {}
        self._shared: dict[int, Counter[int]] = {}
        self._changed = asyncio.Condition()
        self._sessions = itertools.count(1)

    @asynccontextmanager
    async def pinned_connection(self, settings: Any = None):
        session = _Session(self, next(self._sessions))
        try:
            yield session
        finally:
            await self._release_session(session.id)

    def holders(self, key: int) -> dict[str, list[int]]:
        """The sessions holding ``key``, by mode."""
        return {
            "exclusive": sorted(self._exclusive.get(key, Counter())),
            "shared": sorted(self._shared.get(key, Counter())),
        }

    def _others(self, held: dict[int, Counter[int]], key: int, session: int) -> bool:
        return any(holder != session for holder in held.get(key, Counter()))

    async def _run(self, function: str, key: int, session: int) -> bool:
        async with self._changed:
            if function == "pg_try_advisory_lock":
                if self._others(self._exclusive, key, session) or self._others(self._shared, key, session):
                    return False
                self._exclusive.setdefault(key, Counter())[session] += 1
                return True
            if function == "pg_advisory_lock_shared":
                await self._changed.wait_for(lambda: not self._others(self._exclusive, key, session))
                self._shared.setdefault(key, Counter())[session] += 1
                return True
            if function in ("pg_advisory_unlock", "pg_advisory_unlock_shared"):
                held = self._exclusive if function == "pg_advisory_unlock" else self._shared
                holds = held.get(key, Counter())
                if holds[session] == 0:
                    return False
                holds[session] -= 1
                if holds[session] == 0:
                    del holds[session]
                self._changed.notify_all()
                return True
        raise AssertionError(f"unexpected advisory-lock function {function!r}")

    async def _release_session(self, session: int) -> None:
        async with self._changed:
            for held in (self._exclusive, self._shared):
                for holds in held.values():
                    holds.pop(session, None)
            self._changed.notify_all()


class _Session:
    """One pinned connection: an autocommit session whose cursor runs advisory-lock statements."""

    def __init__(self, keys: SessionAdvisoryKeys, session_id: int) -> None:
        self._keys = keys
        self.id = session_id
        self.autocommit = False

    async def set_autocommit(self, value: bool) -> None:
        self.autocommit = value

    def cursor(self) -> _Cursor:
        return _Cursor(self)


class _Cursor:
    def __init__(self, session: _Session) -> None:
        self._session = session
        self._row: tuple[bool] | None = None

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, sql: str, params: tuple[int, ...]) -> None:
        match = _CALL.fullmatch(" ".join(sql.split()))
        if match is None:
            raise AssertionError(f"unexpected SQL on the advisory-lock connection: {sql!r}")
        if not self._session.autocommit:
            raise AssertionError("an advisory-lock statement ran outside autocommit")
        (key,) = params
        self._row = (await self._session._keys._run(match.group(1), key, self._session.id),)

    async def fetchone(self) -> tuple[bool] | None:
        return self._row
