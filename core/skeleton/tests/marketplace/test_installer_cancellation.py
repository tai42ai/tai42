"""A marketplace operation cancelled part-way never leaves a half-written plugin.

A cancellation that lands while an install, update, uninstall or upgrade-all writes its change
(the manifest and env write, its convergence, the install store row, the unused builds) waits
for that write to end — complete, or unwound when it fails — and then propagates as a
cancellation, however often it is repeated; where the change landed, the cancellation says the
fleet recycle roll it owes did not run. One that lands while the plugin's migrations run swaps
the environment back before it propagates.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

import pytest

from tai42_skeleton.marketplace import installer_base as installer_base_module

from ._specs import make_resolved, make_spec
from .test_installer import Harness, _tool_provides, layer_inputs
from .test_installer_migrations import _spec_with_migrations
from .test_installer_roll_hold import _RECYCLE_KEY, _World, keys, world  # noqa: F401  — fixtures


class _Pause:
    """A point inside an operation that holds the operation until the test lets it go on."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.released = asyncio.Event()

    async def wait(self) -> None:
        self.reached.set()
        await self.released.wait()


def _pausing(pause: _Pause, step: Callable[..., Awaitable[Any]]) -> Callable[..., Coroutine[Any, Any, Any]]:
    """``step``, held at ``pause`` before it runs."""

    async def paused(*args: Any, **kwargs: Any) -> Any:
        await pause.wait()
        return await step(*args, **kwargs)

    return paused


async def _cancelled_at(pause: _Pause, operation: Coroutine[Any, Any, Any], *, times: int = 1) -> asyncio.Task[Any]:
    """Run ``operation``, cancel it ``times`` times once it reaches ``pause``, then let it go on."""
    task = asyncio.ensure_future(operation)
    await pause.reached.wait()
    for _ in range(times):
        task.cancel()
        await asyncio.sleep(0)
    pause.released.set()
    return task


@pytest.fixture(autouse=True)
def _versions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")


@pytest.mark.parametrize("times", [1, 3])
async def test_an_install_cancelled_while_its_row_is_written_completes_and_then_is_cancelled(times: int) -> None:
    h = Harness()
    h.registry.resolved = make_resolved(make_spec(provides=_tool_provides("pkg.tools.uuid")), version="1.0.0")
    pause = _Pause()
    h.store.record = _pausing(pause, h.store.record)  # type: ignore[method-assign]

    task = await _cancelled_at(pause, h.installer().install("tai42/toolbox"), times=times)

    with pytest.raises(asyncio.CancelledError):
        await task
    # The install is whole: its row, its manifest entry and its environment all landed.
    assert h.store.rows["tai42/toolbox"].version == "1.0.0"
    assert h.cm._manifest["tools"] == [{"title": "pkg.tools.uuid", "module": "pkg.tools.uuid"}]
    assert layer_inputs(h) == ["tai42/toolbox 1.0.0"]
    assert h.events[-1] == "lock:release"


async def test_an_install_cancelled_while_its_manifest_change_converges_completes_and_then_is_cancelled() -> None:
    h = Harness()
    h.registry.resolved = make_resolved(make_spec(provides=_tool_provides("pkg.tools.uuid")), version="1.0.0")
    pause = _Pause()
    h.svc.converge = _pausing(pause, h.svc.converge)  # type: ignore[method-assign]

    task = await _cancelled_at(pause, h.installer().install("tai42/toolbox"))

    with pytest.raises(asyncio.CancelledError):
        await task
    assert h.store.rows["tai42/toolbox"].version == "1.0.0"
    assert h.cm._manifest["tools"] == [{"title": "pkg.tools.uuid", "module": "pkg.tools.uuid"}]
    assert layer_inputs(h) == ["tai42/toolbox 1.0.0"]


async def test_a_cancelled_install_whose_change_landed_says_its_fleet_recycle_roll_did_not_run(
    world: _World,  # noqa: F811
) -> None:
    pause = _Pause()
    world.store.record = _pausing(pause, world.store.record)  # type: ignore[method-assign]

    task = await _cancelled_at(pause, world.install("NEW"))

    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert raised.value.__notes__ == [
        "config changes landed, but the fleet recycle roll they owe did not complete: the operation was cancelled"
    ]
    assert world.config_store.env[_RECYCLE_KEY] == "NEW"
    assert world.store.rows["tai42/relay-srv"].version == "1.0.0"
    assert world.probe.rolls == 0
    world.assert_the_key_is_free()


async def test_the_unwind_of_a_failed_install_is_not_cut_short_by_repeated_cancels() -> None:
    h = Harness()
    h.registry.resolved = make_resolved(make_spec(), version="1.0.0")
    h.store.record_error = RuntimeError("pg down")
    pause = _Pause()
    # The unwind reverses the manifest patch through ``apply_change``; the forward write persists
    # and converges on its own.
    h.svc.apply_change = _pausing(pause, h.svc.apply_change)  # type: ignore[method-assign]

    task = await _cancelled_at(pause, h.installer().install("tai42/toolbox"), times=3)

    with pytest.raises(RuntimeError, match="pg down") as raised:
        await task
    # The unwind ran to its end: manifest restored, no row, the environment swapped back.
    assert h.cm._manifest == {}
    assert h.store.rows == {}
    assert layer_inputs(h) is None
    assert "the previous plugin environment was restored" in (raised.value.__notes__ or [])
    # The cancellations that landed meanwhile ride on the error.
    assert isinstance(raised.value.__context__, asyncio.CancelledError)


async def test_an_update_cancelled_while_its_row_is_written_completes_and_then_is_cancelled() -> None:
    old = make_spec(version="1.0.0", provides=_tool_provides("pkg.old"))
    new = make_spec(version="2.0.0", provides=_tool_provides("pkg.new"))
    h = Harness(manifest={"tools": [{"title": "pkg.old", "module": "pkg.old"}]})
    h.store.preload(old, version="1.0.0")
    h.registry.resolved = make_resolved(new, version="2.0.0")
    pause = _Pause()
    h.store.record = _pausing(pause, h.store.record)  # type: ignore[method-assign]

    task = await _cancelled_at(pause, h.installer().update("tai42/toolbox"), times=2)

    with pytest.raises(asyncio.CancelledError):
        await task
    assert h.store.rows["tai42/toolbox"].version == "2.0.0"
    assert h.cm._manifest["tools"] == [{"title": "pkg.new", "module": "pkg.new"}]
    assert layer_inputs(h) == ["tai42/toolbox 2.0.0"]


async def test_an_upgrade_all_cancelled_while_a_row_is_written_completes_that_upgrade_and_then_is_cancelled() -> None:
    old = make_spec(version="1.0.0", provides=_tool_provides("pkg.old"))
    new = make_spec(version="2.0.0", provides=_tool_provides("pkg.new"))
    h = Harness(manifest={"tools": [{"title": "pkg.old", "module": "pkg.old"}]})
    h.store.preload(old, version="1.0.0")
    h.registry.resolved = make_resolved(new, version="2.0.0")
    h.registry.versions_map = {
        "tai42/toolbox": [
            {"version": "2.0.0", "status": "published", "contract_range": ">=0.1,<1.0"},
            {"version": "1.0.0", "status": "published", "contract_range": ">=0.1,<1.0"},
        ]
    }
    pause = _Pause()
    h.store.record = _pausing(pause, h.store.record)  # type: ignore[method-assign]

    task = await _cancelled_at(pause, h.installer().upgrade_all())

    with pytest.raises(asyncio.CancelledError):
        await task
    assert h.store.rows["tai42/toolbox"].version == "2.0.0"
    assert h.cm._manifest["tools"] == [{"title": "pkg.new", "module": "pkg.new"}]
    assert layer_inputs(h) == ["tai42/toolbox 2.0.0"]


async def test_an_uninstall_cancelled_while_its_row_is_dropped_completes_and_then_is_cancelled() -> None:
    spec = make_spec(provides=_tool_provides("pkg.tools.uuid"))
    h = Harness(manifest={"tools": [{"title": "pkg.tools.uuid", "module": "pkg.tools.uuid"}]})
    h.store.preload(spec, version="1.0.0")
    pause = _Pause()
    h.store.delete = _pausing(pause, h.store.delete)  # type: ignore[method-assign]

    task = await _cancelled_at(pause, h.installer().uninstall("tai42/toolbox"), times=2)

    with pytest.raises(asyncio.CancelledError):
        await task
    # The uninstall is whole: no row, no manifest entry, the layer rebuilt without the plugin.
    assert h.store.rows == {}
    assert h.cm._manifest == {"tools": []}
    assert layer_inputs(h) == []


def _pause_migrations(monkeypatch: pytest.MonkeyPatch, pause: _Pause | None) -> None:
    """Stand in for the migration runner, held at ``pause`` when one is given."""

    async def paused_apply(entries: Any) -> list[Any]:
        if pause is not None:
            await pause.wait()
        return []

    monkeypatch.setattr(installer_base_module, "apply_migrations", paused_apply)
    monkeypatch.setattr(installer_base_module, "installed_plugin_entry", lambda distribution: object())


async def test_an_install_cancelled_while_its_migrations_run_swaps_the_environment_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness()
    h.registry.resolved = make_resolved(_spec_with_migrations(), version="1.0.0")
    pause = _Pause()
    _pause_migrations(monkeypatch, pause)

    task = await _cancelled_at(pause, h.installer().install("tai42/acct"))

    with pytest.raises(asyncio.CancelledError):
        await task
    # The first layer build is gone again, and nothing else was written.
    assert layer_inputs(h) is None
    assert h.svc.calls == 0
    assert h.store.rows == {}


async def test_an_update_cancelled_while_its_migrations_run_swaps_the_environment_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _spec_with_migrations(version="1.0.0")
    new = _spec_with_migrations(version="2.0.0")
    h = Harness()
    h.registry.resolved = make_resolved(old, version="1.0.0")
    _pause_migrations(monkeypatch, None)
    await h.installer().install("tai42/acct")
    h.registry.resolved = make_resolved(new, version="2.0.0")
    pause = _Pause()
    _pause_migrations(monkeypatch, pause)

    task = await _cancelled_at(pause, h.installer().update("tai42/acct"))

    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert layer_inputs(h) == ["tai42/acct 1.0.0"]
    assert h.store.rows["tai42/acct"].version == "1.0.0"
    assert "the previous plugin environment was restored" in (raised.value.__notes__ or [])
