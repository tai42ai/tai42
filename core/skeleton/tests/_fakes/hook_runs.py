"""A harness for tests whose hook runs must actually run in the test's event loop.

Every test starts from fresh hook-run runtime state (the root conftest's autouse fixture calls
:func:`fresh_runtime`). A test that accepts events and needs their attempts to run takes the
``hook_runs`` fixture: it puts the caller on the serving loop, so an accept spawns the attempts,
routes every runtime read of the current manager to the manager the test drives, and at teardown
waits for every attempt and asserts none is left.
"""

from __future__ import annotations

import collections
from typing import TYPE_CHECKING

import pytest

from tai42_skeleton.hooks.runs import runtime

if TYPE_CHECKING:
    from tai42_skeleton.hooks.managers.base_hooks_manager import BaseHooksManager


def fresh_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the test its own attempt registry, stopping flag and slot count (monkeypatch restores them)."""
    monkeypatch.setattr(runtime, "_ATTEMPTS", {})
    monkeypatch.setattr(runtime, "_STOPPING", False)
    monkeypatch.setattr(runtime, "_SLOTS_IN_USE", 0)
    monkeypatch.setattr(runtime, "_WAITERS", collections.deque())


class HookRunsHarness:
    """Drives the hook-run runtime for one test."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.manager: BaseHooksManager | None = None
        monkeypatch.setattr(runtime, "on_serving_loop", lambda: True)
        monkeypatch.setattr(runtime, "manager", self._current)

    def _current(self) -> BaseHooksManager:
        if self.manager is None:
            raise RuntimeError("the test drives hook runs without naming its manager (hook_runs.use(manager))")
        return self.manager

    def use(self, manager: BaseHooksManager) -> BaseHooksManager:
        """Make ``manager`` the one every hook-run step reads; returns it."""
        self.manager = manager
        return manager

    def off_serving_loop(self) -> None:
        """Make an accept write its jobs unclaimed and spawn nothing, as off the serving loop."""
        self._monkeypatch.setattr(runtime, "on_serving_loop", lambda: False)

    def back_on_serving_loop(self) -> None:
        """Undo :meth:`off_serving_loop`: accepts and sweep claims spawn their attempts again."""
        self._monkeypatch.setattr(runtime, "on_serving_loop", lambda: True)

    async def drain(self) -> None:
        """Wait until no attempt runs in this process."""
        await runtime.drain_hook_runs()

    async def close(self) -> None:
        await runtime.drain_hook_runs()
        live = runtime.live_attempt_ids()
        if live:
            raise AssertionError(f"hook-run attempts were left running: {sorted(live)}")


class MarkRecorder:
    """A ``mark_acted`` for a direct ``_run_hook`` call: records every notice and the start spec it carries."""

    def __init__(self) -> None:
        self.notices: list = []

    async def __call__(self, act, start) -> None:
        self.notices.append((act, start))
