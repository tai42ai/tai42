"""``BackendRuntime`` declaration surface: the two host-agreed constants and the
default bodies a binding either honors or trips over.

Unlike a plain ABC stub, each default here carries a decision — a declaration
the binding did not implement must raise NAMING the class, and a runtime with no
cold stop must degrade to "keep draining" rather than to a crash — so the
defaults are exercised."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import Self

import pytest

from tai42_contract.backend import (
    CONSUMING_RUNTIME,
    REGISTRY_MUTATING_FLEET_OPS,
    Backend,
    BackendRuntime,
    ExecutionMode,
)


class _BareRuntime(BackendRuntime):
    """Declares nothing beyond the required ``from_args``, so every default body
    is reachable."""

    name = "bare"
    mode = ExecutionMode.on_loop

    @classmethod
    def from_args(cls, args: Sequence[str]) -> Self:
        return cls()


def test_from_args_is_the_only_abstract_member():
    # A binding that parses its own options satisfies the ABC; everything else
    # has a default, so an added member never breaks an external implementor.
    assert BackendRuntime.__abstractmethods__ == frozenset({"from_args"})
    _BareRuntime.from_args([])


def test_execution_mode_members():
    assert [m.value for m in ExecutionMode] == ["on_loop", "worker_thread", "inline"]


def test_consuming_runtime_is_the_manifest_export_name():
    # The CLI sniffs argv for this literal before the plugin module is imported.
    assert CONSUMING_RUNTIME == "worker"


def test_registry_mutating_ops_exclude_query_and_recycle():
    # A query mutates nothing and a recycle ends the process, so neither can be
    # a reason to turn a pool over.
    assert "list_failed_mcps" not in REGISTRY_MUTATING_FLEET_OPS
    assert "recycle" not in REGISTRY_MUTATING_FLEET_OPS
    expected = frozenset(
        {
            "reload_config",
            "reload_mcp",
            "deregister_mcp",
            "reload_tool",
            "remove_tool",
            "reload_failed_mcps",
        }
    )
    assert expected == REGISTRY_MUTATING_FLEET_OPS


def test_build_and_aclose_default_to_nothing():
    runtime = _BareRuntime()
    assert asyncio.run(runtime.build()) is None
    assert asyncio.run(runtime.aclose()) is None


def _call_run_on_loop(runtime: BackendRuntime) -> None:
    asyncio.run(runtime.run_on_loop())


def _call_turn_over_pool(runtime: BackendRuntime) -> None:
    asyncio.run(runtime.turn_over_pool(reason="reload_config", budget=5.0))


_UNHONORED: list[tuple[Callable[[BackendRuntime], None], str]] = [
    (_call_run_on_loop, "mode=on_loop"),
    (BackendRuntime.run_blocking, "blocking mode"),
    (BackendRuntime.request_drain, "consumes work"),
    (_call_turn_over_pool, "pool turnover"),
]


@pytest.mark.parametrize(("call", "expected"), _UNHONORED)
def test_unhonored_declaration_raises_naming_the_class(call: Callable[[BackendRuntime], None], expected: str) -> None:
    runtime = _BareRuntime()
    with pytest.raises(NotImplementedError, match=expected) as exc:
        call(runtime)
    assert "_BareRuntime" in str(exc.value)


def test_default_request_terminate_keeps_draining(caplog: pytest.LogCaptureFixture):
    # No cold stop declared: report it and let the warm drain run out rather than
    # raising into the host's signal path.
    with caplog.at_level(logging.WARNING, logger="tai42_contract.backend.runtime"):
        assert _BareRuntime().request_terminate() is None
    assert "_BareRuntime declares no cold stop" in caplog.text


def test_backend_abc_is_unchanged_by_the_runtime_module():
    # The runtime surface is purely additive: ``Backend`` gains no abstract
    # member, so an external implementor of ``launch`` keeps working.
    assert Backend.__abstractmethods__ == frozenset({"launch"})
