"""Observability router: one span's resolved value, and the trace export with references resolved.

Driven like ``test_observability.py``: the operation and the handlers run directly over a fake
in-memory reader installed through the monitoring registry."""

from __future__ import annotations

import json

import pytest
from tai42_contract.monitoring import MonitoringReadNotSupportedError

from tai42_skeleton.monitoring.registry import reset_monitoring
from tai42_skeleton.routers import observability as router

from .test_observability import _FakeReader, _install, _json, _obs, _q, _req, _trace


@pytest.fixture(autouse=True)
def _reset_backend():
    reset_monitoring()
    yield
    reset_monitoring()


# -- GET /api/observability/runs/{trace_id}/spans/{span_id}/resolved -----------------------


def _ref(span_id: str, field: str, pointer: str = "") -> dict:
    return {"$tai42_ref": {"span_id": span_id, "field": field, "pointer": pointer}}


def _install_reference_trace() -> _FakeReader:
    reader = _install(_FakeReader())
    reader.traces_by_id["t1"] = _trace(
        id="t1",
        observations=[
            _obs(id="a", output={"value": {"deep": "origin"}, "list": [1, 2]}),
            _obs(id="m", output={"outputs": {"n": _ref("a", "output", "/value")}}),
            _obs(id="b", input={"from_a": _ref("a", "output", "/value/deep"), "via": _ref("m", "output")}),
            _obs(id="lost", input={"gone": _ref("absent", "output")}),
        ],
    )
    return reader


async def _resolved(**query) -> tuple[int, dict]:
    from tai42_skeleton.operations.errors import OperationError
    from tai42_skeleton.operations.observability import ResolvedSpanValueQuery, get_resolved_span_value

    params = ResolvedSpanValueQuery.model_validate(query)
    path = {"trace_id": query.pop("trace_id", "t1"), "span_id": query.pop("span_id", "b")}
    try:
        return 200, await get_resolved_span_value(**path, field=params.field, pointer=params.pointer)
    except OperationError as exc:
        return exc.status, {"error": exc.message}


async def test_resolved_value_resolves_every_reference():
    _install_reference_trace()
    status, body = await _resolved(field="input")
    assert status == 200
    assert body == {
        "traceId": "t1",
        "spanId": "b",
        "field": "input",
        "pointer": "",
        "value": {"from_a": "origin", "via": {"outputs": {"n": {"deep": "origin"}}}},
    }


async def test_resolved_value_at_a_pointer_runs_through_a_reference_on_the_way():
    _install_reference_trace()
    status, body = await _resolved(field="input", pointer="/via/outputs/n/deep")
    assert status == 200
    assert body["value"] == "origin"


def test_resolved_value_pointer_must_be_rfc6901():
    from pydantic import ValidationError

    from tai42_skeleton.operations.observability import ResolvedSpanValueQuery

    with pytest.raises(ValidationError, match="pointer must be empty or start with '/'"):
        ResolvedSpanValueQuery.model_validate({"field": "input", "pointer": "via"})
    with pytest.raises(ValidationError):
        ResolvedSpanValueQuery.model_validate({"field": "metadata"})


async def test_resolved_value_route_answers_422_on_a_bad_pointer():
    _install_reference_trace()
    resp = await router.get_resolved_span_value(_req(_q(field="input", pointer="via"), trace_id="t1", span_id="b"))
    assert resp.status_code == 422


async def test_resolved_value_route_answers_200():
    _install_reference_trace()
    resp = await router.get_resolved_span_value(_req(_q(field="input", pointer="/from_a"), trace_id="t1", span_id="b"))
    assert resp.status_code == 200
    assert _json(resp)["data"]["value"] == "origin"


@pytest.mark.parametrize(("trace_id", "span_id"), [("t1", "nope"), ("missing", "b")])
async def test_resolved_value_unknown_span_or_trace_is_404(trace_id: str, span_id: str):
    _install_reference_trace()
    status, body = await _resolved(field="input", trace_id=trace_id, span_id=span_id)
    assert (status, body) == (404, {"error": "Run span not found"})


async def test_resolved_value_unresolved_reference_is_502():
    _install_reference_trace()
    status, body = await _resolved(field="input", span_id="lost")
    assert status == 502
    assert body["error"].endswith("(not yet available or lost)")


async def test_resolved_value_missing_pointer_is_502():
    _install_reference_trace()
    status, body = await _resolved(field="input", pointer="/nope")
    assert status == 502
    assert "names a path the record does not hold" in body["error"]


async def test_resolved_value_read_not_supported_is_501():
    reader = _install_reference_trace()
    reader.get_trace_error = MonitoringReadNotSupportedError("write-only backend")
    status, _ = await _resolved(field="input")
    assert status == 501


async def test_export_trace_resolves_references_on_request():
    _install_reference_trace()
    resp = await router.export_run_trace(_req(_q(resolve="true"), trace_id="t1"))
    assert resp.status_code == 502  # the "lost" span's reference cannot resolve
    reader = _install(_FakeReader())
    reader.traces_by_id["t1"] = _trace(
        id="t1", observations=[_obs(id="a", output={"v": 1}), _obs(id="b", input=_ref("a", "output", "/v"))]
    )
    resp = await router.export_run_trace(_req(_q(resolve="true"), trace_id="t1"))
    assert resp.status_code == 200
    spans = json.loads(bytes(resp.body))["spans"]
    assert next(s for s in spans if s["id"] == "b")["input"] == 1
    plain = await router.export_run_trace(_req("", trace_id="t1"))
    assert next(s for s in json.loads(bytes(plain.body))["spans"] if s["id"] == "b")["input"] == _ref(
        "a", "output", "/v"
    )


async def test_export_trace_refuses_a_bad_resolve_value():
    _install_reference_trace()
    resp = await router.export_run_trace(_req(_q(resolve="maybe"), trace_id="t1"))
    assert resp.status_code == 400
