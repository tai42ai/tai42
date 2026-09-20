"""Coverage for the hash/scan typing seams homed alongside ``RedisClient``.

``hgetall``/``hset_mapping``/``scan_iter`` add no runtime behavior beyond a cast:
a call must delegate straight through to the underlying redis-py surface
(``client.hgetall``, ``client.hset(key, mapping=...)``, ``client.scan_iter``).
A small in-memory fake stands in for the async redis client — no real redis.
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING, Any, cast

import pytest

pytest.importorskip("redis")

from tai42_kit.clients.impl.redis import hgetall, hset_mapping, scan_iter

if TYPE_CHECKING:
    from redis.asyncio import Redis as AsyncRedis


class _FakeRedis:
    """Matches only the redis operations the typing seams delegate to."""

    def __init__(self, store: dict[str, dict[str, str]] | None = None) -> None:
        self._store: dict[str, dict[str, str]] = store or {}
        self.hset_calls: list[tuple[str, dict[str, str]]] = []

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._store.get(key, {}))

    async def hset(self, key: str, mapping: dict[str, str]) -> int:
        # Record the call so the test can assert the exact write shape.
        self.hset_calls.append((key, dict(mapping)))
        existing = self._store.setdefault(key, {})
        new_fields = sum(1 for field in mapping if field not in existing)
        existing.update(mapping)
        return new_fields

    async def scan_iter(self, match: str):
        for key in list(self._store):
            if fnmatch.fnmatchcase(key, match):
                yield key


def _client(fake: _FakeRedis) -> AsyncRedis:
    return cast("AsyncRedis", fake)


async def test_hgetall_reads_the_stored_field_map():
    fake = _FakeRedis({"ac:key:abc": {"user_id": "u1", "description": "d"}})
    assert await hgetall(_client(fake), "ac:key:abc") == {"user_id": "u1", "description": "d"}


async def test_hgetall_missing_key_returns_empty_map():
    fake = _FakeRedis()
    assert await hgetall(_client(fake), "absent") == {}


async def test_hset_mapping_writes_exactly_the_mapping_and_returns_new_field_count():
    fake = _FakeRedis()
    added = await hset_mapping(_client(fake), "ac:key:abc", {"user_id": "u1", "description": "d"})
    assert fake.hset_calls == [("ac:key:abc", {"user_id": "u1", "description": "d"})]
    assert added == 2


async def test_hset_mapping_round_trips_through_hgetall():
    fake = _FakeRedis()
    await hset_mapping(_client(fake), "doc", {"user_id": "u", "description": "d"})
    assert await hgetall(_client(fake), "doc") == {"user_id": "u", "description": "d"}


async def test_scan_iter_yields_glob_matches():
    fake = _FakeRedis({"ac:key:a": {}, "ac:key:b": {}, "ac:ctx:c": {}})
    found = [key async for key in scan_iter(_client(fake), "ac:key:*")]
    assert sorted(found) == ["ac:key:a", "ac:key:b"]


async def test_scan_iter_no_match_yields_nothing():
    fake = _FakeRedis({"ac:key:a": {}})
    found = [key async for key in scan_iter(_client(fake), "nomatch:*")]
    assert found == []


@pytest.mark.parametrize(
    "mapping",
    [
        {"used": "3"},
        {"user_id": "u", "description": "d"},
        {"a": "1", "b": "2", "c": "3"},
    ],
)
async def test_hset_mapping_then_hgetall_round_trip(mapping: dict[str, Any]):
    fake = _FakeRedis()
    await hset_mapping(_client(fake), "doc", mapping)
    assert await hgetall(_client(fake), "doc") == mapping
