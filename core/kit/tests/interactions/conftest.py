"""Shared fixtures for the park index tests: an in-process Redis with Lua, and a bound redelivery horizon."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app

from tai42_kit.clients.settings import RedisConnectionSettings

REDELIVERY_HORIZON_SECONDS = 500


@pytest.fixture
def fake_redis() -> Any:
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.FakeAsyncRedis(decode_responses=True)


@pytest.fixture
def horizon_app() -> Iterator[None]:
    app = SimpleNamespace(interactions=SimpleNamespace(redelivery_horizon_seconds=lambda: REDELIVERY_HORIZON_SECONDS))
    with tai42_app.bound(app):
        yield


@pytest.fixture
def make_index(fake_redis: Any, horizon_app: None) -> Callable[[str], Any]:
    from tai42_kit.interactions.park_index import ParkIndex

    @contextlib.asynccontextmanager
    async def _client() -> AsyncIterator[Any]:
        yield fake_redis

    def _make(namespace: str) -> ParkIndex:
        return ParkIndex(namespace, RedisConnectionSettings(redis_url="redis://unused"), client=_client)

    return _make
