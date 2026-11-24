"""The agents' park index lives until a settings reset; the reset drops it and the next read builds a fresh one."""

from __future__ import annotations

import gc
import weakref
from collections.abc import Iterator

import pytest
from tai42_kit.settings import reset_all_settings

from tai42_agents._internal.park.park_binding import agents_park_index
from tai42_agents.settings import agents_park_redis_settings


@pytest.fixture
def park_redis_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TAI_AGENTS_REDIS_URL", "redis://park-index-reset:6379/0")
    reset_all_settings()
    try:
        yield
    finally:
        reset_all_settings()


@pytest.mark.usefixtures("park_redis_url")
def test_reads_between_resets_share_one_index() -> None:
    assert agents_park_index() is agents_park_index()


@pytest.mark.usefixtures("park_redis_url")
def test_a_settings_reset_releases_the_index_and_the_settings_it_was_built_over() -> None:
    settings = weakref.ref(agents_park_redis_settings())
    index = weakref.ref(agents_park_index())

    reset_all_settings()
    gc.collect()

    assert settings() is None
    assert index() is None


@pytest.mark.usefixtures("park_redis_url")
def test_the_read_after_a_reset_builds_a_fresh_index() -> None:
    before = agents_park_index()

    reset_all_settings()

    after = agents_park_index()
    assert after is not before
    assert after is agents_park_index()
