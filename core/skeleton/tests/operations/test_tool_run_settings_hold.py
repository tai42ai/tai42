"""The tool-run supervisor's call tree holds no settings instance across an await.

A tool run's supervisor is a root task that can outlive the serving generation that
admitted it (a background submit, a hook or trigger fire through ``run_recorded``). Every
coroutine on its call tree — the supervisor, its liveness refresher, the record creation,
the visit it starts through — reads its configuration through the cached accessor at the
point of use and keeps only primitives and what it derives. Each case parks the run at an
await through the REAL cached accessors, retires the generation the way the reload's
retire step does (advance the epoch, reset, sweep), then lets the run finish.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_kit.clients.base import advance_client_epoch
from tai42_kit.settings import reset_all_settings, sweep_stale_settings

from tai42_skeleton.interactions import visit as visit_module
from tai42_skeleton.interactions.settings import interactions_settings
from tai42_skeleton.operations import tool_runs as ops
from tai42_skeleton.operations.tool_runs import ToolRunStore
from tai42_skeleton.routers.tool_runs_settings import tool_runs_settings

from .._fakes.interactions_redis import FakeRedis as InteractionsFakeRedis
from .._fakes.tool_runs_redis import FakeRedis, _FakePipeline

_HELD_TYPES = (".ToolRunsSettings", ".InteractionsSettings")


class _Tools:
    """The ``app.tools`` seam a run dispatches through; ``run_tool`` and ``get_tool`` are replaceable."""

    def __init__(self) -> None:
        self.result: object = {"ok": 1}

    async def get_tools(self) -> dict[str, SimpleNamespace]:
        return {"alpha": SimpleNamespace(name="alpha", meta=None)}

    async def get_tool(self, key: str) -> SimpleNamespace:
        return SimpleNamespace(name=key, meta=None)

    async def run_tool(self, key: str, arguments: dict, *, offload_sync: bool = False, extras: Any = None) -> object:
        return self.result


@pytest.fixture
async def gate() -> asyncio.Future[None]:
    return asyncio.get_running_loop().create_future()


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Tools]:
    """Configure both stores, wire their fakes, and read settings through the real cached accessors."""
    monkeypatch.setenv("TAI_TOOL_RUNS_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")
    tool_runs_settings.cache_clear()
    interactions_settings.cache_clear()
    runs_fake = FakeRedis()
    interactions_fake = InteractionsFakeRedis()

    @asynccontextmanager
    async def runs_ctx(client_cls: Any, s: Any = None, *, fresh: bool = False, **kwargs: Any) -> AsyncIterator[Any]:
        yield runs_fake

    @asynccontextmanager
    async def interactions_ctx(
        client_cls: Any, s: Any = None, *, fresh: bool = False, **kwargs: Any
    ) -> AsyncIterator[Any]:
        yield interactions_fake

    monkeypatch.setattr(ops, "client_ctx", runs_ctx)
    monkeypatch.setattr(visit_module, "client_ctx", interactions_ctx)
    monkeypatch.setattr(ops, "_ACTIVE_RUNS", 0)
    bound = _Tools()
    monkeypatch.setattr(
        tai42_app,
        "_impl",
        SimpleNamespace(
            tools=bound,
            interactions=SimpleNamespace(
                visit=visit_module.visit,
                park_answer=visit_module.park_answer,
                normalise_started=visit_module.normalise_started,
            ),
        ),
    )
    yield bound
    for task in list(ops._SUPERVISORS):
        task.cancel()


@pytest.fixture
def parked_at(monkeypatch: pytest.MonkeyPatch, gate: asyncio.Future[None]) -> Callable[..., None]:
    """Replace ``owner.name`` with a coroutine that waits on the gate, then returns ``value``."""

    def _park(owner: object, name: str, value: Any = None) -> None:
        async def _parked(*args: Any, **kwargs: Any) -> Any:
            await gate
            return value

        monkeypatch.setattr(owner, name, _parked)

    return _park


async def _drain_supervisors() -> None:
    tasks = list(ops._SUPERVISORS)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _held_while_parked(
    door: Callable[[], Coroutine[Any, Any, Any]],
    gate: asyncio.Future[None],
    caplog: pytest.LogCaptureFixture,
) -> list[Any]:
    """Run ``door`` until the run parks on ``gate``, retire the settings generation, sweep, then finish it."""
    task = asyncio.create_task(door())
    for _ in range(20):
        await asyncio.sleep(0)
    assert not task.done(), task.exception() if task.done() else None
    try:
        retired = advance_client_epoch()
        reset_all_settings()
        with caplog.at_level(logging.ERROR, logger="tai42_kit.settings.cache_registry"):
            held = [h for h in sweep_stale_settings(retired) if h.settings_type.endswith(_HELD_TYPES)]
    finally:
        gate.set_result(None)
        with suppress(Exception):
            await task
        await _drain_supervisors()
    return held


def _assert_nothing_held(held: list[Any], caplog: pytest.LogCaptureFixture) -> None:
    assert held == []
    assert "ToolRunsSettings" not in caplog.text
    assert "InteractionsSettings" not in caplog.text


async def _submit_and_wait() -> None:
    await ops.submit_run("alpha", {"x": 1})
    await _drain_supervisors()


async def _fire() -> None:
    await ops.run_recorded("alpha", {"x": 1})


@pytest.mark.parametrize("door", [_submit_and_wait, _fire], ids=["background_submit", "hook_fire"])
async def test_a_run_suspended_in_its_tool_holds_no_retired_settings(
    tools: _Tools,
    caplog: pytest.LogCaptureFixture,
    gate: asyncio.Future[None],
    parked_at: Callable[..., None],
    door: Callable[[], Coroutine[Any, Any, Any]],
) -> None:
    parked_at(tools, "run_tool", {"ok": 1})
    _assert_nothing_held(await _held_while_parked(door, gate, caplog), caplog)


@pytest.mark.parametrize("door", [_submit_and_wait, _fire], ids=["background_submit", "hook_fire"])
async def test_a_run_suspended_on_its_terminal_write_holds_no_retired_settings(
    tools: _Tools,
    caplog: pytest.LogCaptureFixture,
    gate: asyncio.Future[None],
    parked_at: Callable[..., None],
    door: Callable[[], Coroutine[Any, Any, Any]],
) -> None:
    parked_at(ToolRunStore, "mark_terminal_if_running", True)
    _assert_nothing_held(await _held_while_parked(door, gate, caplog), caplog)


async def test_a_hook_fire_suspended_reading_the_tool_registration_holds_no_retired_settings(
    tools: _Tools, caplog: pytest.LogCaptureFixture, gate: asyncio.Future[None], parked_at: Callable[..., None]
) -> None:
    parked_at(tools, "get_tool", SimpleNamespace(name="alpha", meta=None))
    _assert_nothing_held(await _held_while_parked(_fire, gate, caplog), caplog)


@pytest.mark.parametrize("door", [_submit_and_wait, _fire], ids=["background_submit", "hook_fire"])
async def test_a_run_suspended_creating_its_record_holds_no_retired_settings(
    tools: _Tools,
    caplog: pytest.LogCaptureFixture,
    gate: asyncio.Future[None],
    parked_at: Callable[..., None],
    door: Callable[[], Coroutine[Any, Any, Any]],
) -> None:
    parked_at(_FakePipeline, "execute", [])
    _assert_nothing_held(await _held_while_parked(door, gate, caplog), caplog)


@pytest.mark.parametrize(
    ("door", "first_await", "value"),
    [
        (lambda: ops.get_run("r-1"), "get_run", None),
        (lambda: ops.list_tool_runs("alpha"), "recent_run_ids", []),
    ],
    ids=["get_run", "list_tool_runs"],
)
async def test_a_tool_run_read_suspended_on_the_store_holds_no_retired_settings(
    tools: _Tools,
    caplog: pytest.LogCaptureFixture,
    gate: asyncio.Future[None],
    parked_at: Callable[..., None],
    door: Callable[[], Coroutine[Any, Any, Any]],
    first_await: str,
    value: Any,
) -> None:
    parked_at(ToolRunStore, first_await, value)
    _assert_nothing_held(await _held_while_parked(door, gate, caplog), caplog)
