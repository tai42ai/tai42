"""Fakes for the redis-backed conversation routing-row store.

``FakeRedis`` covers the STRING + SET operations the manager calls plus the two atomic
put/delete ``eval`` scripts. Single-threaded async, so each script runs atomically.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any


class FakeRedis:
    def __init__(self) -> None:
        self._strings: dict[str, str] = {}
        self._sets: dict[str, set[str]] = {}
        #: The route thread indexes (keyspace 8) the put script reads its door-flip
        #: refusal from: ``{route_threads_key: thread count}``.
        self.threads_held: dict[str, int] = {}

    # -- direct surface the manager reads ------------------------------------
    async def get(self, key: str) -> str | None:
        return self._strings.get(key)

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self._strings.get(k) for k in keys]

    async def smembers(self, key: str) -> set[str]:
        return set(self._sets.get(key, set()))

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
        """Emulate the atomic put/delete Lua scripts by their marker comment.

        Signature-compatible with ``redis.eval(script, numkeys, *keys, *args)``.
        """
        if "conversations:route:put:atomic" in script:
            names_key, route_key, threads_key, route_name, route_json, door = keys_and_args
            existing = self._strings.get(route_key)
            if existing is not None:
                stored_door = json.loads(existing)["door"]
                held = self.threads_held.get(threads_key, 0)
                if stored_door != door and held > 0:
                    return [held, stored_door]
            self._strings[route_key] = route_json
            self._sets.setdefault(names_key, set()).add(route_name)
            return 1 if existing is not None else 0
        if "conversations:route:delete:atomic" in script:
            names_key, route_key, route_name = keys_and_args
            removed = 1 if self._strings.pop(route_key, None) is not None else 0
            self._sets.get(names_key, set()).discard(route_name)
            return removed
        raise NotImplementedError("FakeRedis.eval only emulates the put/delete route scripts")


def make_client_ctx(fake: FakeRedis):
    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield fake

    return _ctx
