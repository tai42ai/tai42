"""Fakes for the redis-backed identity provider.

A small in-memory async stand-in for the kit ``RedisClient`` surface the provider
touches — ``get``/``set``/``delete`` (the ``user_id -> hash`` reverse lookup),
``hgetall``/``hset(key, mapping=...)`` (the identity record hash), ``scan_iter``
(enumeration), and a WATCH/MULTI pipeline (the provision write) — plus a factory
that yields it through the ``client_ctx`` seam the provider imports. No real redis.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable
from contextlib import asynccontextmanager

import pytest
from redis.exceptions import WatchError


class FakeRedis:
    """Matches exactly the redis operations the provider calls."""

    def __init__(
        self,
        *,
        strings: dict | None = None,
        hashes: dict | None = None,
        raise_hash: Exception | None = None,
    ) -> None:
        self._strings = strings or {}
        self._hashes = hashes or {}
        self._raise_hash = raise_hash
        # One-shot callback fired just before the first pipeline EXEC, to simulate a
        # concurrent commit inside the WATCH/EXEC window (that EXEC then raises
        # ``WatchError``). Cleared after it fires so the retry runs clean.
        self.on_first_exec: Callable[[], None] | None = None

    async def get(self, key):
        return self._strings.get(key)

    async def set(self, key, value):
        self._strings[key] = str(value)
        return True

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            present = key in self._strings or key in self._hashes
            self._strings.pop(key, None)
            self._hashes.pop(key, None)
            if present:
                removed += 1
        return removed

    async def hgetall(self, key):
        if self._raise_hash is not None:
            raise self._raise_hash
        # Copy so an in-place edit never leaks into the store; ``{}`` for a missing key.
        return dict(self._hashes.get(key, {}))

    async def hset(self, key, mapping):
        # Merge string-valued fields into the hash; return the count of new fields.
        existing = self._hashes.setdefault(key, {})
        stored = {field: str(value) for field, value in mapping.items()}
        new_fields = sum(1 for field in stored if field not in existing)
        existing.update(stored)
        return new_fields

    async def scan_iter(self, match):
        # SCAN matches keys of every type with GLOB semantics; ``fnmatch`` mirrors it.
        seen: set[str] = set()
        for store in (self._strings, self._hashes):
            for key in list(store):
                if key not in seen and fnmatch.fnmatchcase(key, match):
                    seen.add(key)
                    yield key

    def pipeline(self, transaction: bool = True, shard_hint: str | None = None) -> _FakePipeline:
        return _FakePipeline(self)


class _FakePipeline:
    """An in-memory redis-py async pipeline with WATCH/MULTI: post-``watch`` reads
    run against live state, writes are queued, and ``execute`` applies them only if
    no watched key changed (else raises ``WatchError``)."""

    def __init__(self, fake: FakeRedis) -> None:
        self._fake = fake
        self._queue: list[tuple[str, tuple]] = []
        self._watched: dict[str, object] = {}

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *exc) -> bool:
        self._queue = []
        self._watched = {}
        return False

    async def watch(self, *keys: str) -> None:
        # Snapshot each watched key's current value; a later EXEC compares against it.
        for key in keys:
            self._watched[key] = self._fake._strings.get(key)

    async def unwatch(self) -> None:
        self._watched = {}

    def multi(self) -> None:
        # No-op: the fake queues every write regardless; exists for the call site.
        return None

    async def get(self, key):
        # Immediate mode (post-watch, pre-multi) reads the live value directly.
        return self._fake._strings.get(key)

    def set(self, key, value) -> _FakePipeline:
        self._queue.append(("set", (key, value)))
        return self

    def hset(self, key, mapping) -> _FakePipeline:
        self._queue.append(("hset", (key, mapping)))
        return self

    def delete(self, *keys) -> _FakePipeline:
        self._queue.append(("delete", keys))
        return self

    async def execute(self, raise_on_error: bool = True) -> list:
        # Fire the concurrent-write hook before the watch comparison, so this EXEC
        # observes the changed key and aborts.
        if self._fake.on_first_exec is not None:
            hook = self._fake.on_first_exec
            self._fake.on_first_exec = None
            hook()
        for key, snapshot in self._watched.items():
            if self._fake._strings.get(key) != snapshot:
                self._queue = []
                self._watched = {}
                raise WatchError("watched key changed")
        results: list = []
        for op, args in self._queue:
            if op == "set":
                results.append(await self._fake.set(*args))
            elif op == "hset":
                key, mapping = args
                results.append(await self._fake.hset(key, mapping))
            elif op == "delete":
                results.append(await self._fake.delete(*args))
            else:  # pragma: no cover - unreachable guard
                raise AssertionError(f"unhandled queued op {op!r}")
        self._queue = []
        self._watched = {}
        return results


def make_client_ctx(fake: FakeRedis):
    """A drop-in for ``client_ctx`` yielding ``fake`` for any client class."""

    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield fake

    return _ctx


@pytest.fixture(autouse=True)
def _isolate_identity_registry():
    """Snapshot + restore the module-level identity-provider registry around each
    test so a registration never leaks into the next."""
    from tai42_contract.access_control import registry

    saved = dict(registry._REGISTRY)
    try:
        yield
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(saved)
