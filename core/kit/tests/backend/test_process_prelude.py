"""The process prelude a runtime declares: the launch refuses a process not started with it.

A prelude must run before any other import, so the launch can only check that the process was
started with it (the marker the host's entry sets) and refuse before anything of the runtime starts.
A process started through a prelude must also have bound asyncio's future chaining to the threads
the prelude reshaped, or the launch refuses it the same way.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar, Self

import pytest
from tai42_contract.backend.runtime import PROCESS_PRELUDE_ENV, BackendRuntime, ProcessPreludeRequired

from tai42_kit.backend import ManagedBackend, check_runtime_declarations, future_chaining_bound

from .fakes import FakeApp, FakeOnLoopWorker, FakeSchedulerRuntime

_Runtimes = Mapping[str, type[BackendRuntime]]

_PRELUDE = "engine.patch:apply"


class _PreludeWorker(FakeOnLoopWorker):
    """A consuming runtime whose launch option declares a process prelude."""

    @classmethod
    def from_args(cls, args: Sequence[str]) -> Self:
        runtime = super().from_args(args)
        runtime.process_prelude = _PRELUDE
        return runtime


class _PreludeScheduler(FakeSchedulerRuntime):
    """A runtime that pulls no work and declares a process prelude."""

    @classmethod
    def from_args(cls, args: Sequence[str]) -> Self:
        runtime = super().from_args(args)
        runtime.process_prelude = _PRELUDE
        return runtime


class _PreludeBackend(ManagedBackend):
    label = "prelude"
    runtimes: ClassVar[_Runtimes] = {"worker": _PreludeWorker, "scheduler": _PreludeScheduler}


@pytest.mark.parametrize("marker", [None, "engine.other:apply"])
@pytest.mark.parametrize(("subcommand", "runtime_cls"), [("worker", _PreludeWorker), ("scheduler", _PreludeScheduler)])
async def test_a_process_not_started_with_the_declared_prelude_is_refused_before_anything_starts(
    monkeypatch: pytest.MonkeyPatch,
    liveness_path,
    subcommand: str,
    runtime_cls: type[FakeOnLoopWorker] | type[FakeSchedulerRuntime],
    marker: str | None,
) -> None:
    if marker is None:
        monkeypatch.delenv(PROCESS_PRELUDE_ENV, raising=False)
    else:
        monkeypatch.setenv(PROCESS_PRELUDE_ENV, marker)

    with pytest.raises(ProcessPreludeRequired) as refused:
        await _PreludeBackend().launch([subcommand])

    assert refused.value.prelude == _PRELUDE
    runtime = runtime_cls.latest
    assert runtime is not None
    # Nothing of the runtime ran: no liveness reporter bound, no build, no body, no teardown.
    assert runtime.calls == []
    assert runtime._progress is None
    assert not liveness_path.exists()


@pytest.mark.usefixtures("stock_future_chaining")
@pytest.mark.parametrize(("subcommand", "runtime_cls"), [("worker", _PreludeWorker), ("scheduler", _PreludeScheduler)])
async def test_a_process_started_with_the_prelude_but_its_future_chaining_unbound_is_refused_before_anything_starts(
    monkeypatch: pytest.MonkeyPatch,
    liveness_path,
    subcommand: str,
    runtime_cls: type[FakeOnLoopWorker] | type[FakeSchedulerRuntime],
) -> None:
    monkeypatch.setenv(PROCESS_PRELUDE_ENV, _PRELUDE)
    assert not future_chaining_bound()

    unbound = r"never bound asyncio's future chaining.*bind_future_chaining\(\)"
    with pytest.raises(RuntimeError, match=unbound) as refused:
        await _PreludeBackend().launch([subcommand])

    assert not isinstance(refused.value, ProcessPreludeRequired)
    runtime = runtime_cls.latest
    assert runtime is not None
    assert runtime.calls == []
    assert runtime._progress is None
    assert not liveness_path.exists()


@pytest.mark.usefixtures("bound_future_chaining")
async def test_a_process_started_with_the_declared_prelude_launches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROCESS_PRELUDE_ENV, _PRELUDE)

    await _PreludeBackend().launch(["scheduler"])

    runtime = _PreludeScheduler.latest
    assert runtime is not None
    assert runtime.calls == ["build", "run", "drained", "aclose"]


@pytest.mark.usefixtures("bound_future_chaining")
async def test_a_consuming_runtime_started_with_its_prelude_reaches_its_build(
    monkeypatch: pytest.MonkeyPatch, app: FakeApp
) -> None:
    class _Impatient(_PreludeBackend):
        ready_timeout = 0.01

    monkeypatch.setenv(PROCESS_PRELUDE_ENV, _PRELUDE)

    with pytest.raises(RuntimeError, match="did not become ready"):
        await _Impatient().launch(["worker"])

    runtime = _PreludeWorker.latest
    assert runtime is not None
    assert runtime.calls == ["build", "aclose"]


@pytest.mark.usefixtures("bound_future_chaining")
@pytest.mark.parametrize("marker", [None, _PRELUDE, "anything:else"])
async def test_a_runtime_that_declares_no_prelude_launches_whatever_the_marker(
    monkeypatch: pytest.MonkeyPatch, marker: str | None
) -> None:
    class _Plain(ManagedBackend):
        label = "plain"
        runtimes: ClassVar[_Runtimes] = {"scheduler": FakeSchedulerRuntime}

    if marker is None:
        monkeypatch.delenv(PROCESS_PRELUDE_ENV, raising=False)
    else:
        monkeypatch.setenv(PROCESS_PRELUDE_ENV, marker)

    await _Plain().launch(["scheduler"])

    runtime = FakeSchedulerRuntime.latest
    assert runtime is not None
    assert runtime.calls == ["build", "run", "drained", "aclose"]


@pytest.mark.usefixtures("stock_future_chaining")
async def test_a_runtime_that_declares_no_prelude_is_refused_in_a_prelude_process_its_host_left_unbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Plain(ManagedBackend):
        label = "plain"
        runtimes: ClassVar[_Runtimes] = {"scheduler": FakeSchedulerRuntime}

    monkeypatch.setenv(PROCESS_PRELUDE_ENV, _PRELUDE)
    assert not future_chaining_bound()

    with pytest.raises(RuntimeError, match="never bound asyncio's future chaining"):
        await _Plain().launch(["scheduler"])

    runtime = FakeSchedulerRuntime.latest
    assert runtime is not None
    assert runtime.calls == []


@pytest.mark.parametrize("prelude", ["engine.patch", "engine.patch:", ":apply", "engine patch:apply", "a:b:c", 7])
def test_conformance_rejects_a_prelude_that_is_not_a_module_callable_name(prelude: object) -> None:
    runtime = FakeSchedulerRuntime.from_args([])
    runtime.process_prelude = prelude  # type: ignore[assignment]

    assert check_runtime_declarations(runtime) == [
        f"FakeSchedulerRuntime declares process_prelude={prelude!r}, which is not a 'module:callable' name, "
        "so the host's entry cannot run it"
    ]


@pytest.mark.parametrize("prelude", [None, "engine:apply", "engine.patch._impl:apply_all"])
def test_conformance_accepts_no_prelude_or_a_module_callable_name(prelude: str | None) -> None:
    runtime = FakeSchedulerRuntime.from_args([])
    runtime.process_prelude = prelude

    assert check_runtime_declarations(runtime) == []


async def test_a_malformed_prelude_refuses_the_launch_as_any_unhonored_declaration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Malformed(FakeSchedulerRuntime):
        process_prelude = "not a name"

    class _Backend(ManagedBackend):
        label = "malformed"
        runtimes: ClassVar[_Runtimes] = {"scheduler": _Malformed}

    monkeypatch.setenv(PROCESS_PRELUDE_ENV, "not a name")

    with pytest.raises(TypeError, match="malformed launch refused"):
        await _Backend().launch(["scheduler"])
