"""A dead collector is counted on ``/metrics`` and raised as the ``monitoring_export_failed``
platform event, never silent.

The stack exports to an address nothing listens on; one tool run produces records whose export
fails; the run itself succeeds. A hook on the topic records the event's payload through the
fixture recorder tool."""

from __future__ import annotations

import json
from collections.abc import Callable

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

_TOPIC = "monitoring_export_failed"


async def test_a_dead_collector_is_counted_and_alerted(
    dead_collector_monitoring_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    stack = dead_collector_monitoring_stack
    api = stack.api()
    probe_key = uniq("export-failed")
    await api.post(
        "/api/hooks",
        json={
            "name": uniq("export-failed-hook").replace("_", "-"),
            "topic": _TOPIC,
            "tool": "e2e_record",
            "start_expr": {"content": f'{{key: "{probe_key}", value: (. | tojson)}}'},
            "execution_key": uniq("exec"),
        },
    )

    result = await api.post("/api/run-tool", json={"tool_name": "e2e_echo_monitor", "arguments": {"payload": "p"}})
    assert result is not None

    async def export_failures_counted() -> float | None:
        value = stack.app_scrape().sample("tai42_monitoring_export_failures_total", {})
        return value if value is not None and value > 0 else None

    await wait_for_async(
        export_failures_counted, deadline=60.0, message="the failed export was never counted on /metrics"
    )

    async def alert_recorded() -> list[str] | None:
        records = stack.records(probe_key)
        return records or None

    records = await wait_for_async(
        alert_recorded, deadline=60.0, message="no monitoring_export_failed event reached the hook"
    )
    assert len(records) == 1, records
    payload = json.loads(json.loads(records[0])["value"])
    assert payload["export_failures"] >= 1, payload
