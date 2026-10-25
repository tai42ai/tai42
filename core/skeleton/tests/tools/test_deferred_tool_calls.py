"""The ``tool`` deferred-call kind: capture in the caller's context, a run under that context after the reply."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.monitoring import (
    MonitoringLevel,
    RunAttribution,
    SpanKind,
    TraceContext,
    get_ambient_trace_context,
)
from tai42_contract.secrets import SecretValue
from tai42_contract.states.errors import DeferredCallRefusedError
from tai42_contract.states.models import StateContext, SubjectCandidates

from tai42_skeleton.authz.execution_identity import (
    capture_fire_identity,
    get_execution_identity,
    reset_execution_identity,
    set_execution_identity,
)
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.monitoring import init_monitoring, reset_monitoring
from tai42_skeleton.operations import PermissionDeniedError
from tai42_skeleton.states.context import current_state_context, state_context
from tai42_skeleton.states.outbox.calls import deferred_call_kind
from tai42_skeleton.tools import deferred as deferred_mod
from tai42_skeleton.tools.attribution import get_run_attribution, run_attribution
from tai42_skeleton.tools.binding.errors import UnknownToolError

from .._fakes.recording_monitoring import RecordingMonitoring

_TRACE = "a" * 32
_CALLER_SPAN = "b" * 16
_DEFERRED_SPAN = "c" * 16

_CTX = StateContext(
    door="conversation",
    candidates=SubjectCandidates(target_kind="agent", target_name="a", by_kind={"thread": "t1"}),
    actor="alice",
)


class _Tools:
    def __init__(self, known: dict[str, Any], result: Any = None) -> None:
        self.known = known
        self.result = result
        self.runs: list[dict[str, Any]] = []
        self.error: Exception | None = None

    async def get_tool(self, name: str) -> Any:
        if name not in self.known:
            raise UnknownToolError(name)
        return self.known[name]

    async def run_tool(self, name: str, arguments: dict[str, Any], *, extras: Any = None) -> Any:
        trace = get_ambient_trace_context()
        self.runs.append(
            {
                "name": name,
                "arguments": arguments,
                "extras": dict(extras or {}),
                "identity": get_execution_identity(),
                "attribution": get_run_attribution(),
                "context": current_state_context(),
                "trace": trace,
            }
        )
        if self.error is not None:
            raise self.error
        return self.result


class _Interactions:
    def __init__(self, kind: str) -> None:
        self.kind = kind

    async def normalise_started(self, value: Any) -> Any:
        return SimpleNamespace(kind=self.kind)


@pytest.fixture
def backend() -> Iterator[RecordingMonitoring]:
    reset_monitoring()
    backend = RecordingMonitoring()
    init_monitoring(backend)
    yield backend
    reset_monitoring()


@pytest.fixture
def tools(backend: RecordingMonitoring) -> Iterator[_Tools]:
    registry = _Tools({"echo": SimpleNamespace(meta={}), "durable": SimpleNamespace(meta={"tai42/crash_resume": True})})
    monitoring = SimpleNamespace(active=backend)
    with tai42_app.bound(SimpleNamespace(tools=registry, interactions=_Interactions("result"), monitoring=monitoring)):
        yield registry


async def _capture_under_the_callers_span(backend: RecordingMonitoring, kind: deferred_mod.ToolCallKind) -> dict:
    writer = backend.writer
    writer.active_trace_id = _TRACE
    writer.next_span_id = _CALLER_SPAN
    with writer.start_span(name="caller", kind=SpanKind.TOOL):
        payload = await kind.capture("echo", {"x": 1})
    writer.active_trace_id = None
    writer.next_span_id = _DEFERRED_SPAN
    return payload


@pytest.fixture
def bound_binds(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []

    @asynccontextmanager
    async def _bind(user_id: str, *, bound_fingerprint: str):
        seen.append((user_id, bound_fingerprint))
        token = set_execution_identity(CallerIdentity(user_id=user_id, execution_key_fingerprint=bound_fingerprint))
        try:
            yield
        finally:
            reset_execution_identity(token)

    monkeypatch.setattr("tai42_skeleton.authz.execution.bind_execution_identity", _bind)
    return seen


def test_the_tool_kind_is_registered_at_import() -> None:
    assert isinstance(deferred_call_kind("tool"), deferred_mod.ToolCallKind)


async def test_capture_records_the_callers_identity_attribution_context_and_trace(
    tools: _Tools, bound_binds: list, backend: RecordingMonitoring
) -> None:
    kind = deferred_mod.ToolCallKind()
    token = set_execution_identity(CallerIdentity(user_id="key-1", execution_key_fingerprint="fp-1"))
    try:
        with run_attribution(RunAttribution(user_id="u", tags=["t"])), state_context(_CTX):
            payload = await _capture_under_the_callers_span(backend, kind)
    finally:
        reset_execution_identity(token)
    assert payload == {
        "tool": "echo",
        "arguments": {"x": 1},
        "identity": ["key-1", "fp-1"],
        "attribution": RunAttribution(user_id="u", tags=["t"]).model_dump(mode="json"),
        "state_context": _CTX.model_dump(mode="json"),
        "trace_id": _TRACE,
        "parent_span_id": _CALLER_SPAN,
    }
    assert capture_fire_identity() == (None, "")


async def test_apply_runs_the_tool_under_the_captured_identity_and_context(
    tools: _Tools, bound_binds: list, backend: RecordingMonitoring
) -> None:
    kind = deferred_mod.ToolCallKind()
    token = set_execution_identity(CallerIdentity(user_id="key-1", execution_key_fingerprint="fp-1"))
    try:
        with run_attribution(RunAttribution(user_id="u")), state_context(_CTX):
            payload = await _capture_under_the_callers_span(backend, kind)
    finally:
        reset_execution_identity(token)
    await kind.apply(payload, idempotency_key="7:0")
    (run,) = tools.runs
    assert run["name"] == "echo"
    assert run["arguments"] == {"x": 1}
    assert run["extras"] == {"tai42/idempotency_key": "7:0"}
    assert run["identity"].user_id == "key-1"
    assert run["attribution"] == RunAttribution(user_id="u")
    assert run["context"] == _CTX
    assert run["trace"] == TraceContext(trace_id=_TRACE, parent_span_id=_DEFERRED_SPAN)
    assert bound_binds == [("key-1", "fp-1")]
    assert get_execution_identity() is None
    assert current_state_context() is None


async def test_a_call_with_no_captured_identity_runs_with_none(tools: _Tools, bound_binds: list) -> None:
    kind = deferred_mod.ToolCallKind()
    payload = await kind.capture("echo", {})
    await kind.apply(payload, idempotency_key="1:0")
    assert tools.runs[0]["identity"] is None
    assert bound_binds == []


async def test_applied_the_call_runs_inside_one_deferred_call_span_under_the_captured_span(
    tools: _Tools, backend: RecordingMonitoring
) -> None:
    kind = deferred_mod.ToolCallKind()
    tools.result = {"y": 2}
    payload = await _capture_under_the_callers_span(backend, kind)
    await kind.apply(payload, idempotency_key="1:0")
    assert [s["name"] for s in backend.writer.spans] == ["caller", "deferred-call:echo"]
    deferred = backend.writer.spans[1]
    assert deferred["name"] == "deferred-call:echo"
    assert deferred["kind"] is SpanKind.CHAIN
    assert deferred["trace_context"] == TraceContext(trace_id=_TRACE, parent_span_id=_CALLER_SPAN)
    assert deferred["input"] == {"x": 1}
    assert [u["output"] for u in deferred["span"].updates] == [{"y": 2}]
    assert deferred["span"].ended
    assert tools.runs[0]["trace"] == TraceContext(trace_id=_TRACE, parent_span_id=_DEFERRED_SPAN)
    assert get_ambient_trace_context() is None


async def test_a_raising_target_leaves_the_deferred_call_span_at_error(
    tools: _Tools, backend: RecordingMonitoring
) -> None:
    kind = deferred_mod.ToolCallKind()
    payload = await _capture_under_the_callers_span(backend, kind)
    tools.error = RuntimeError("target boom")
    with pytest.raises(RuntimeError, match="target boom"):
        await kind.apply(payload, idempotency_key="1:0")
    deferred = backend.writer.spans[-1]
    assert deferred["input"] == {"x": 1}
    assert [(u["output"], u["level"], u["status_message"]) for u in deferred["span"].updates] == [
        (None, MonitoringLevel.ERROR, "target boom")
    ]
    assert deferred["span"].ended


async def test_a_payload_with_no_trace_opens_no_span(tools: _Tools, backend: RecordingMonitoring) -> None:
    kind = deferred_mod.ToolCallKind()
    payload = await kind.capture("echo", {})
    assert (payload["trace_id"], payload["parent_span_id"]) == (None, None)
    await kind.apply(payload, idempotency_key="1:0")
    assert backend.writer.spans == []
    assert tools.runs[0]["trace"] is None


async def test_a_revoked_key_refuses_the_call_loudly(tools: _Tools, monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def _revoked(user_id: str, *, bound_fingerprint: str):
        raise PermissionDeniedError("the execution key was rotated")
        yield

    monkeypatch.setattr("tai42_skeleton.authz.execution.bind_execution_identity", _revoked)
    kind = deferred_mod.ToolCallKind()
    with pytest.raises(PermissionDeniedError, match="rotated"):
        await kind.apply({"tool": "echo", "arguments": {}, "identity": ["key-1", "fp-1"]}, idempotency_key="1:0")
    assert tools.runs == []


async def test_a_call_that_parks_is_refused(tools: _Tools, monkeypatch: pytest.MonkeyPatch) -> None:
    kind = deferred_mod.ToolCallKind()
    with (
        tai42_app.bound(SimpleNamespace(tools=tools, interactions=_Interactions("parked"))),
        pytest.raises(DeferredCallRefusedError, match="deferred call 'echo' parked; a call after the reply"),
    ):
        await kind.apply({"tool": "echo", "arguments": {}}, idempotency_key="1:0")


@pytest.mark.parametrize("outcome", ["asks", "parked"])
async def test_a_refused_parked_call_leaves_the_deferred_call_span_at_error_with_no_output(
    tools: _Tools, backend: RecordingMonitoring, outcome: str
) -> None:
    kind = deferred_mod.ToolCallKind()
    payload = await _capture_under_the_callers_span(backend, kind)
    message = "deferred call 'echo' parked; a call after the reply cannot wait for an answer"
    with (
        tai42_app.bound(
            SimpleNamespace(
                tools=tools, interactions=_Interactions(outcome), monitoring=SimpleNamespace(active=backend)
            )
        ),
        pytest.raises(DeferredCallRefusedError, match=message),
    ):
        await kind.apply(payload, idempotency_key="1:0")
    deferred = backend.writer.spans[-1]
    assert deferred["name"] == "deferred-call:echo"
    assert deferred["input"] == {"x": 1}
    assert [(u["output"], u["level"], u["status_message"]) for u in deferred["span"].updates] == [
        (None, MonitoringLevel.ERROR, message)
    ]
    assert deferred["span"].ended


async def test_capture_refuses_an_unknown_tool_and_arguments_that_are_not_plain_json(tools: _Tools) -> None:
    kind = deferred_mod.ToolCallKind()
    with pytest.raises(DeferredCallRefusedError, match="deferred call names unknown tool 'nope'"):
        await kind.capture("nope", {})
    with pytest.raises(
        DeferredCallRefusedError, match="deferred call to 'echo' carries arguments that are not plain JSON"
    ):
        await kind.capture("echo", {"secret": SecretValue("s")})


async def test_only_a_tool_declaring_crash_resume_is_resumable(tools: _Tools) -> None:
    kind = deferred_mod.ToolCallKind()
    assert await kind.resumable({"tool": "durable"}) is True
    assert await kind.resumable({"tool": "echo"}) is False
    assert await kind.resumable({"tool": "gone"}) is False
