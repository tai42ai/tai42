"""``check_backend_declarations`` — the certification, run as a test.

Both shipped fakes clear the bar with no host-shaped code of their own, which is
the whole claim: a binding writes runtime classes and gets the lifecycle. The red
cases are the ways a binding silently loses a guarantee — an overridden template,
a self-installed signal handler, a consuming runtime the host cannot find by name.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar, Self

import pytest
from tai42_contract.backend.runtime import BackendRuntime, ExecutionMode

from tai42_kit.backend import ManagedBackend, check_backend_declarations, check_runtime_declarations
from tests.backend._self_wiring import DelegatingBackend, SelfWiringBackend
from tests.backend.fakes import (
    FakeForkingBackend,
    FakeOnLoopBackend,
    FakeOnLoopWorker,
    FakeSchedulerRuntime,
    FakeThreadWorker,
)

# A launch-subcommand map, annotated so a test class declares it as the ClassVar
# the base already types it as.
_Runtimes = Mapping[str, type[BackendRuntime]]


@pytest.mark.parametrize("backend_cls", [FakeOnLoopBackend, FakeForkingBackend])
def test_a_shipped_fake_is_certified(backend_cls: type[ManagedBackend]) -> None:
    assert check_backend_declarations(backend_cls()) == []


@pytest.mark.parametrize("runtime_cls", [FakeOnLoopWorker, FakeThreadWorker, FakeSchedulerRuntime])
def test_each_fake_runtime_honors_its_declarations(runtime_cls: type[BackendRuntime]) -> None:
    assert check_runtime_declarations(runtime_cls) == []


def test_a_backend_outside_the_shared_base_is_not_certified() -> None:
    class _Standalone:
        pass

    problems = check_backend_declarations(_Standalone())
    assert problems == ["_Standalone is not a ManagedBackend, so the shared launch lifecycle does not drive it"]


def test_an_overridden_template_is_reported() -> None:
    class _OverridingBackend(FakeOnLoopBackend):
        async def launch(self, args: Sequence[str]) -> None:
            return None

    problems = check_backend_declarations(_OverridingBackend())
    assert any("overrides launch" in problem for problem in problems)


def test_a_runtime_that_installs_its_own_signal_handler_is_reported() -> None:
    problems = check_backend_declarations(SelfWiringBackend())
    assert any("installs its own signal handler (class body)" in problem for problem in problems)


def test_a_runtime_that_installs_out_of_line_is_reported() -> None:
    # The verdict names the MODULE, not the class body: the install is one call
    # away, and a class-body-only scan would certify this backend.
    problems = check_backend_declarations(DelegatingBackend())
    assert any("installs its own signal handler (module)" in problem for problem in problems)


def test_a_runtime_with_no_readable_source_is_reported() -> None:
    # A runtime the certifier cannot read is not a runtime it can certify.
    dynamic = type("_DynamicWorker", (FakeOnLoopWorker,), {})

    class _DynamicBackend(FakeOnLoopBackend):
        runtimes: ClassVar[_Runtimes] = {"worker": dynamic}

    problems = check_backend_declarations(_DynamicBackend())
    assert any("has no readable source" in problem for problem in problems)


def test_a_consuming_runtime_under_another_name_is_reported() -> None:
    # The host sniffs argv for the canonical name before this module is imported,
    # so a differently-named consumer never receives the resolved manifest.
    class _PullerRuntime(FakeOnLoopWorker):
        name = "puller"

    class _PullerBackend(FakeOnLoopBackend):
        runtimes: ClassVar[_Runtimes] = {"puller": _PullerRuntime}

    problems = check_backend_declarations(_PullerBackend())
    assert any("names its consuming runtime something other than 'worker'" in problem for problem in problems)


def test_a_subcommand_that_disagrees_with_the_runtime_name_is_reported() -> None:
    class _MislabelledBackend(FakeOnLoopBackend):
        runtimes: ClassVar[_Runtimes] = {"beat": FakeSchedulerRuntime}

    problems = check_backend_declarations(_MislabelledBackend())
    assert any("maps subcommand 'beat' to FakeSchedulerRuntime, which names itself 'scheduler'" in p for p in problems)


def test_a_backend_with_no_runtimes_is_reported() -> None:
    class _EmptyBackend(ManagedBackend):
        label = "empty"
        runtimes: ClassVar[_Runtimes] = {}

    problems = check_backend_declarations(_EmptyBackend())
    assert problems == ["_EmptyBackend declares no runtimes, so no launch subcommand resolves"]


def test_a_backend_missing_its_declarations_is_reported_not_raised() -> None:
    # The certifier's job is to hand a binding the full list of what it still
    # owes; an AttributeError on the first gap hides every gap after it.
    class _UndeclaredBackend(ManagedBackend):
        pass

    problems = check_backend_declarations(_UndeclaredBackend())
    assert problems == [
        "_UndeclaredBackend declares no label, so its operator-facing messages cannot name it",
        "_UndeclaredBackend declares no runtimes, so no launch subcommand resolves",
    ]


def test_a_runtime_missing_its_declarations_is_reported_not_raised() -> None:
    class _UndeclaredRuntime(BackendRuntime):
        @classmethod
        def from_args(cls, args: Sequence[str]) -> Self:
            return cls()

    problems = check_runtime_declarations(_UndeclaredRuntime)
    assert problems == [
        "_UndeclaredRuntime declares no name, so no launch subcommand can select it",
        "_UndeclaredRuntime declares no mode, so the host cannot tell how to drive its run body",
    ]


def test_a_nameless_runtime_is_not_also_reported_as_mismatched() -> None:
    # One gap, one verdict: a runtime with no name cannot also "disagree" with
    # the subcommand it is mapped to.
    class _NamelessRuntime(BackendRuntime):
        mode = ExecutionMode.inline

        @classmethod
        def from_args(cls, args: Sequence[str]) -> Self:
            return cls()

        def run_blocking(self) -> None:
            return None

    class _NamelessBackend(ManagedBackend):
        label = "nameless"
        runtimes: ClassVar[_Runtimes] = {"scheduler": _NamelessRuntime}

    problems = check_backend_declarations(_NamelessBackend())
    assert problems == ["_NamelessRuntime declares no name, so no launch subcommand can select it"]


def test_an_instance_is_checked_as_readily_as_a_class() -> None:
    # ``pool_turnover_required`` can be decided by a launch option, so the launch
    # path checks the live object.
    runtime = FakeOnLoopWorker.from_args([])
    runtime.pool_turnover_required = True

    assert check_runtime_declarations(FakeOnLoopWorker) == []
    assert any("implements no turn_over_pool" in problem for problem in check_runtime_declarations(runtime))


def test_the_certification_thought_experiment_needs_one_class() -> None:
    # A brand-new backend, written from the contract alone: one runtime class and
    # a two-line backend, no kit edit anywhere.
    class NewEngineWorker(BackendRuntime):
        name = "worker"
        mode = ExecutionMode.on_loop
        consumes_work = True

        @classmethod
        def from_args(cls, args: Sequence[str]) -> Self:
            return cls()

        async def run_on_loop(self) -> None:
            return None

        def request_drain(self) -> None:
            return None

    class NewEngineBackend(ManagedBackend):
        label = "new-engine"
        runtimes: ClassVar[_Runtimes] = {"worker": NewEngineWorker}

    assert check_backend_declarations(NewEngineBackend()) == []


def test_a_mode_that_only_looks_like_one_is_reported() -> None:
    # ``ExecutionMode`` is a ``StrEnum``, so a plain ``"on_loop"`` compares EQUAL to
    # the member. The host dispatches on IDENTITY, so this runtime would match none
    # of the three modes and its body would never be driven — an equality-based
    # check would certify it.
    class _StringModeRuntime(FakeOnLoopWorker):
        mode = "on_loop"  # pyright: ignore[reportAssignmentType]

    assert _StringModeRuntime.mode == ExecutionMode.on_loop
    problems = check_runtime_declarations(_StringModeRuntime)
    assert problems == [
        "_StringModeRuntime declares mode='on_loop', which is not an ExecutionMode member; the host dispatches "
        "on identity, so a look-alike string matches no mode at all"
    ]
