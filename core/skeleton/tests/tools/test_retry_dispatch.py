"""The retry policy at the real ``run_tool`` dispatch chokepoint.

Drives ``ToolBinding.run_tool`` (the one seam every consumer shares) against a
mock ``FunctionTool``: a registry-declared policy re-fires ONLY the resolved
invocation — authorization and resolution happen once per logical dispatch —
while a policy-less tool keeps today's single attempt with zero added
monitoring. Also pins the composition rule with a body that retries internally
(the channel-delivery shape): the seam adds attempts only for a tool that
DECLARED them.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from tai42_contract.channels import ChannelDeliveryError
from tai42_contract.tools import ToolRetryBackoff, ToolRetryPolicy

import tai42_skeleton.tools.retry as retry_module
from tai42_skeleton.app import server as server_module
from tai42_skeleton.monitoring import init_monitoring, reset_monitoring
from tai42_skeleton.tools import binding as binding_module
from tai42_skeleton.tools.binding import ToolBinding
from tai42_skeleton.tools.retry import ToolRetryRegistry

from .._fakes.recording_monitoring import RecordingMonitoring


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    recorded: list[float] = []

    async def _capture(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(retry_module, "_sleep", _capture)
    return recorded


@pytest.fixture
def backend() -> Iterator[RecordingMonitoring]:
    reset_monitoring()
    backend = RecordingMonitoring()
    init_monitoring(backend)
    yield backend
    reset_monitoring()


def _function_tool(fn):
    tool = MagicMock(spec=binding_module.FunctionTool)
    tool.fn = fn
    return tool


def _async_return(value):
    async def _coro(*_args, **_kwargs):
        return value

    return _coro


def _binding_for(fn, *, policies: dict[str, ToolRetryPolicy] | None = None) -> ToolBinding:
    app_mock = MagicMock(spec=server_module.TaiMCP)
    registry = ToolRetryRegistry()
    for name, policy in (policies or {}).items():
        registry.register(name, policy)
    app_mock._tool_retry_registry = registry
    # A raw (non-preset) dispatch: the runs-index chokepoint is out of the
    # picture, exercised separately through the real bind path.
    app_mock.preset_manager.is_registered.return_value = False
    binding = ToolBinding(app_mock)
    binding.get_tool = _async_return(_function_tool(fn))
    return binding


def _policy(**overrides) -> ToolRetryPolicy:
    fields = {
        "max_attempts": 3,
        "idempotent": True,
        "backoff": ToolRetryBackoff(initial_seconds=0.5, multiplier=2.0, cap_seconds=30.0),
    }
    fields.update(overrides)
    return ToolRetryPolicy.model_validate(fields)


def _flaky_fn(fail_times: int):
    calls = {"n": 0}

    def flaky(x: int) -> int:
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise ChannelDeliveryError("medium 503", retryable=True)
        return x * 2

    return flaky, calls


async def test_declared_policy_retries_the_dispatch_to_success(sleeps):
    flaky, calls = _flaky_fn(2)
    binding = _binding_for(flaky, policies={"flaky": _policy()})

    assert await binding.run_tool("flaky", {"x": 7}) == 14
    assert calls["n"] == 3
    assert sleeps == [0.5, 1.0]


async def test_no_policy_dispatch_fails_on_the_first_attempt(sleeps):
    flaky, calls = _flaky_fn(1)
    binding = _binding_for(flaky)

    with pytest.raises(ChannelDeliveryError):
        await binding.run_tool("flaky", {"x": 7})
    assert calls["n"] == 1
    assert sleeps == []


async def test_no_policy_dispatch_adds_zero_spans(sleeps, backend):
    # The byte-identical guarantee, observed at the seam: with a trace active a
    # policy-less run_tool emits exactly the spans it emits today — none from
    # this seam.
    backend.writer.active_trace_id = "trace-1"
    flaky, calls = _flaky_fn(0)
    binding = _binding_for(flaky)

    assert await binding.run_tool("flaky", {"x": 2}) == 4
    assert calls["n"] == 1
    assert backend.writer.spans == []


async def test_policy_only_covers_the_named_tool(sleeps):
    flaky, calls = _flaky_fn(1)
    binding = _binding_for(flaky, policies={"other_tool": _policy()})

    with pytest.raises(ChannelDeliveryError):
        await binding.run_tool("flaky", {"x": 7})
    assert calls["n"] == 1


async def test_unknown_error_is_not_retried_even_with_a_policy(sleeps):
    calls = {"n": 0}

    def broken(x: int) -> int:
        calls["n"] += 1
        raise RuntimeError("mystery")

    binding = _binding_for(broken, policies={"broken": _policy()})
    with pytest.raises(RuntimeError):
        await binding.run_tool("broken", {"x": 7})
    assert calls["n"] == 1
    assert sleeps == []


def _internally_retrying_fn(inner_attempts: int):
    """A body owning its own attempt loop (the channel-delivery shape): each
    invocation burns ``inner_attempts`` internal tries, then raises its last
    transient error."""
    tries = {"n": 0}

    def send(x: int) -> int:
        for _ in range(inner_attempts):
            tries["n"] += 1
        raise ChannelDeliveryError("exhausted internal budget", retryable=True)

    return send, tries


async def test_internal_retry_composes_by_declaration_ownership(sleeps):
    # No declared policy: the seam adds ZERO outer attempts on top of the
    # body's own loop — the terminal error propagates after one invocation,
    # however transient it is typed.
    send, tries = _internally_retrying_fn(3)
    binding = _binding_for(send)
    with pytest.raises(ChannelDeliveryError):
        await binding.run_tool("send", {"x": 1})
    assert tries["n"] == 3
    assert sleeps == []

    # A tool that DOES declare a policy is explicitly asserting the outer
    # attempts on top of its internal budget — the multiplication is the
    # declarer's own.
    send, tries = _internally_retrying_fn(3)
    binding = _binding_for(send, policies={"send": _policy(max_attempts=2)})
    with pytest.raises(ChannelDeliveryError):
        await binding.run_tool("send", {"x": 1})
    assert tries["n"] == 6
