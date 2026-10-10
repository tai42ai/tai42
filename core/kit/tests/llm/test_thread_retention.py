"""A checkpoint thread's retention declared by its owner: the platform's two values are its default and its ceiling."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest

from tai42_kit.llm.checkpoint import (
    ThreadRetention,
    ThreadRetentionError,
    checkpoint_park_horizon,
    mark_threads_active,
    mark_threads_finished,
    platform_retention,
    resolve_retention,
)
from tai42_kit.llm.checkpoint.checkpoint import saver_view_waiting
from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
from tai42_kit.settings import reset_all_settings


@pytest.fixture(autouse=True)
async def _platform(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_WAITING_MINUTES", "600")
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_FINISHED_MINUTES", "120")
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT", "memory")
    reset_all_settings()
    yield
    await checkpoint_registry().close_all()
    reset_all_settings()


def test_the_platform_retention_is_the_two_settings():
    assert platform_retention() == ThreadRetention(waiting_minutes=600, finished_minutes=120)


def test_nothing_declared_resolves_to_the_platform_pair():
    assert resolve_retention() == ThreadRetention(600, 120)


def test_a_declared_pair_is_kept():
    assert resolve_retention(waiting_minutes=30, finished_minutes=5) == ThreadRetention(30, 5)


def test_a_declared_waiting_alone_caps_the_platform_finished_at_it():
    assert resolve_retention(waiting_minutes=60) == ThreadRetention(60, 60)
    assert resolve_retention(waiting_minutes=300) == ThreadRetention(300, 120)


def test_a_declared_finished_alone_keeps_the_platform_waiting():
    assert resolve_retention(finished_minutes=10) == ThreadRetention(600, 10)


def test_the_platform_values_themselves_are_admitted():
    assert resolve_retention(waiting_minutes=600, finished_minutes=120) == ThreadRetention(600, 120)


def test_a_waiting_above_the_platform_is_refused_naming_both_values():
    with pytest.raises(ThreadRetentionError) as excinfo:
        resolve_retention(waiting_minutes=601)
    assert str(excinfo.value) == (
        "checkpoint retention waiting_minutes (601 min) exceeds the platform's 600 min "
        "(LLM_PROVIDER_CHECKPOINT_RETENTION_WAITING_MINUTES)"
    )
    assert isinstance(excinfo.value, ValueError)


def test_a_finished_above_the_platform_is_refused_naming_both_values():
    with pytest.raises(ThreadRetentionError) as excinfo:
        resolve_retention(waiting_minutes=300, finished_minutes=121)
    assert str(excinfo.value) == (
        "checkpoint retention finished_minutes (121 min) exceeds the platform's 120 min "
        "(LLM_PROVIDER_CHECKPOINT_RETENTION_FINISHED_MINUTES)"
    )


def test_a_finished_longer_than_the_waiting_is_refused():
    with pytest.raises(ThreadRetentionError) as excinfo:
        resolve_retention(waiting_minutes=10, finished_minutes=20)
    assert str(excinfo.value) == (
        "checkpoint retention finished_minutes (20 min) must not exceed waiting_minutes (10 min)"
    )


@pytest.mark.parametrize("field", ["waiting_minutes", "finished_minutes"])
@pytest.mark.parametrize("value", [0, -5])
def test_a_non_positive_value_is_refused(field, value):
    with pytest.raises(ThreadRetentionError, match=f"checkpoint retention {field} \\({value} min\\) must be positive"):
        resolve_retention(**{field: value})


def test_a_retention_value_is_immutable():
    retention = ThreadRetention(10, 5)
    with pytest.raises(AttributeError):
        retention.waiting_minutes = 20  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# A retention value is bounded where it is built, so no seam can be handed an unchecked one
# --------------------------------------------------------------------------- #
def test_a_retention_value_above_the_platform_waiting_cannot_be_built():
    with pytest.raises(ThreadRetentionError) as excinfo:
        ThreadRetention(waiting_minutes=601, finished_minutes=60)
    assert str(excinfo.value) == (
        "checkpoint retention waiting_minutes (601 min) exceeds the platform's 600 min "
        "(LLM_PROVIDER_CHECKPOINT_RETENTION_WAITING_MINUTES)"
    )


def test_a_retention_value_above_the_platform_finished_cannot_be_built():
    with pytest.raises(ThreadRetentionError) as excinfo:
        ThreadRetention(waiting_minutes=300, finished_minutes=121)
    assert str(excinfo.value) == (
        "checkpoint retention finished_minutes (121 min) exceeds the platform's 120 min "
        "(LLM_PROVIDER_CHECKPOINT_RETENTION_FINISHED_MINUTES)"
    )


def test_a_retention_value_finished_longer_than_waiting_cannot_be_built():
    with pytest.raises(ThreadRetentionError) as excinfo:
        ThreadRetention(waiting_minutes=10, finished_minutes=20)
    assert str(excinfo.value) == (
        "checkpoint retention finished_minutes (20 min) must not exceed waiting_minutes (10 min)"
    )


@pytest.mark.parametrize(("waiting", "finished", "field"), [(0, 0, "waiting_minutes"), (10, -1, "finished_minutes")])
def test_a_retention_value_not_positive_cannot_be_built(waiting, finished, field):
    with pytest.raises(ThreadRetentionError, match=f"checkpoint retention {field} \\(-?\\d+ min\\) must be positive"):
        ThreadRetention(waiting_minutes=waiting, finished_minutes=finished)


async def _view(retention: ThreadRetention) -> object:
    return saver_view_waiting("redis", retention)


async def _checkpointer(retention: ThreadRetention) -> object:
    return await checkpoint_registry().get_checkpointer("memory", None, retention)


async def _finished(retention: ThreadRetention) -> object:
    return await mark_threads_finished(["t"], retention=retention)


async def _active(retention: ThreadRetention) -> object:
    return await mark_threads_active(["t"], retention=retention)


async def _ledger_start(retention: ThreadRetention) -> object:
    return await (await checkpoint_registry().ledger("memory", None)).start(["t"], retention)


async def _park_horizon(retention: ThreadRetention) -> object:
    return checkpoint_park_horizon("redis", retention)


@pytest.mark.parametrize(
    "seam",
    [_view, _checkpointer, _finished, _active, _ledger_start, _park_horizon],
    ids=["saver-view", "get-checkpointer", "mark-finished", "mark-active", "ledger-start", "park-horizon"],
)
async def test_no_retention_seam_is_reached_by_a_value_above_the_platform(
    seam: Callable[[ThreadRetention], Awaitable[object]],
):
    with pytest.raises(ThreadRetentionError, match="exceeds the platform's 600 min"):
        await seam(ThreadRetention(waiting_minutes=20000, finished_minutes=50000))


async def test_every_retention_seam_takes_a_bounded_value():
    bounded = ThreadRetention(waiting_minutes=30, finished_minutes=5)
    assert await _view(bounded) == 30
    assert await _park_horizon(bounded) == timedelta(minutes=30)
    await _active(bounded)
    ledger = await checkpoint_registry().ledger("memory", None)
    assert await ledger.declared_waiting(["t"]) == {"t": 30}
