"""The presets facet's one store gate over the stored-preset population.

With no skeleton database every reader of the population reads an empty one and no store
is opened; a configured store that cannot be read raises through every reader.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import pytest

import tai42_skeleton.versioning.store as store_module
from tai42_skeleton.app import instance
from tai42_skeleton.states import consumers as consumers_mod
from tai42_skeleton.tools import platform_referees


@pytest.fixture
def no_skeleton_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)

    @asynccontextmanager
    async def forbid_client_ctx(client_cls, settings=None, **kwargs):
        raise AssertionError("versioned store opened with no skeleton database")
        yield  # pragma: no cover - unreachable, satisfies the context-manager protocol

    monkeypatch.setattr(store_module, "client_ctx", forbid_client_ctx)


@pytest.fixture
def unreadable_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")

    @asynccontextmanager
    async def refusing_client_ctx(client_cls, settings=None, **kwargs):
        raise ConnectionRefusedError("connection refused")
        yield  # pragma: no cover - unreachable, satisfies the context-manager protocol

    monkeypatch.setattr(store_module, "client_ctx", refusing_client_ctx)


async def test_population_reads_answer_empty_with_no_skeleton_database(no_skeleton_database) -> None:
    assert await instance.app.presets.list_active_versioned_bodies() == {}
    assert await instance.app.presets.list_active_bodies() == {}


async def test_population_readers_answer_empty_with_no_skeleton_database(
    no_skeleton_database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def declared(_state: str) -> set[str]:
        return {"thread"}

    monkeypatch.setattr(consumers_mod, "_declared_subject_kinds", declared)
    assert await consumers_mod.presets_lister("status") == []
    assert await platform_referees._preset_detach_referee("status", "t1") == []
    assert await instance.app.preset_manager.stored_preset_names() == frozenset()


async def test_rehydrate_handler_logs_the_off_line_and_registers_nothing(
    no_skeleton_database, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="tai42_skeleton.app.instance"):
        await instance.rehydrate_versioned_presets_if_store_in_use()
    assert "presets: skipping versioned-preset rehydration" in caplog.text
    assert instance.app.preset_manager.registered_names() == frozenset()


async def test_population_reads_raise_on_a_configured_store_that_cannot_be_read(unreadable_store) -> None:
    with pytest.raises(ConnectionRefusedError, match="connection refused"):
        await instance.app.presets.list_active_versioned_bodies()
    with pytest.raises(ConnectionRefusedError, match="connection refused"):
        await instance.app.presets.list_active_bodies()
