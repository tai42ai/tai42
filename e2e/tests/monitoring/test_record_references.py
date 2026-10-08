"""Store-once references end to end: a record that references another record's value, written
through the collector into the self-hosted Langfuse, read back and resolved through the platform.

The fixture tool ``e2e_record_probe`` runs a synthetic two-node graph: ``first`` records a large
output, ``second`` records as its input a reference to it. Opt-in like the rest of the
monitoring suite (the compose ``monitoring`` profile plus the ``otel-collector`` service)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs(
        "kind:monitoring:langfuse",
        "helper:langfuse-server",
        "setting:LANGFUSE_HOST=compose-langfuse",
        "probe-tools",
    ),
]

_FIRST_VALUE = "alpha-" * 2000


async def test_a_reference_reads_back_and_resolves_to_the_referenced_value(monitoring_stack: TaiStack) -> None:
    api = monitoring_stack.api()
    probe = await api.post("/api/run-tool", json={"tool_name": "e2e_record_probe", "arguments": {}})
    trace_id, first, second = probe["trace_id"], probe["first_span"], probe["second_span"]

    async def trace_with_both_records() -> dict[str, Any] | None:
        try:
            detail = await api.get(f"/api/observability/runs/{trace_id}/trace")
        except Exception:
            return None
        ids = {span.get("id") for span in detail.get("spans", [])}
        return detail if {first, second} <= ids else None

    detail = await wait_for_async(
        trace_with_both_records, deadline=90.0, message="the probe's two records never reached the backend"
    )
    second_span = next(span for span in detail["spans"] if span["id"] == second)
    assert second_span["input"] == {
        "from_first": {"$tai42_ref": {"span_id": first, "field": "output", "pointer": "/value"}}
    }, second_span["input"]

    resolved = await api.get(f"/api/observability/runs/{trace_id}/spans/{second}/resolved?field=input")
    assert resolved["value"] == {"from_first": _FIRST_VALUE}
    assert (resolved["traceId"], resolved["spanId"], resolved["field"], resolved["pointer"]) == (
        trace_id,
        second,
        "input",
        "",
    )

    at_pointer = await api.get(
        f"/api/observability/runs/{trace_id}/spans/{second}/resolved?field=input&pointer=/from_first"
    )
    assert at_pointer["value"] == _FIRST_VALUE

    exported = await api.request_raw("GET", f"/api/observability/runs/{trace_id}/trace/export?resolve=true")
    assert exported.status_code == 200, exported.text
    exported_second = next(span for span in json.loads(exported.text)["spans"] if span["id"] == second)
    assert exported_second["input"] == {"from_first": _FIRST_VALUE}
