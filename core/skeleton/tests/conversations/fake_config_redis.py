"""An in-memory fake of the redis surface the per-target config store uses — the STRING +
SET commands plus the two config Lua scripts (dispatched by marker comment, re-implemented
here). Single-threaded async, so each faked ``eval`` runs atomically.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


class FakeConfigRedis:
    def __init__(self) -> None:
        self._strings: dict[str, str] = {}
        self._sets: dict[str, set[str]] = {}

    def seed_member(self, names_key: str, member: str) -> None:
        """Plant an index member with no backing row — the corrupt state the list read logs
        over and skips."""
        self._sets.setdefault(names_key, set()).add(member)

    async def get(self, key: str) -> str | None:
        return self._strings.get(key)

    async def smembers(self, key: str) -> set[str]:
        return set(self._sets.get(key, set()))

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self._strings.get(key) for key in keys]

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
        keys = [str(k) for k in keys_and_args[:numkeys]]
        argv = [str(a) for a in keys_and_args[numkeys:]]
        names_key, config_key = keys[0], keys[1]
        if "conversations:config:put:atomic" in script:
            member, config_json = argv[0], argv[1]
            existed = 1 if config_key in self._strings else 0
            self._strings[config_key] = config_json
            self._sets.setdefault(names_key, set()).add(member)
            return existed
        if "conversations:config:delete:atomic" in script:
            member = argv[0]
            removed = 1 if self._strings.pop(config_key, None) is not None else 0
            self._sets.get(names_key, set()).discard(member)
            return removed
        raise NotImplementedError(f"FakeConfigRedis.eval: unknown script {script[:60]!r}")


def make_config_client_ctx(fake: FakeConfigRedis):
    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs) -> AsyncIterator[FakeConfigRedis]:
        yield fake

    return _ctx
