"""The live interactions stream refuses a resume it cannot honour.

A ``Last-Event-ID`` older than the oldest event the stream still holds answers ``410`` with
the code ``interactions-resume-expired`` (the client reconnects without the id and re-reads
the paged list); a value that is not a stream id answers ``400`` with the code
``interactions-resume-malformed``. Both answer before the event stream opens. The stack runs
no backend seam, so the module is ``backendless``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs("kind:tool_runs", "kind:interactions", "probe-tools", "topology:replicas"),
]


async def _park_one_ask(stack: TaiStack, question: str) -> None:
    """Park one user ask, so the events stream holds at least one event."""
    api = stack.api(port=stack.port_a)
    submitted = await api.post(
        "/api/tool-runs",
        json={"tool_name": "e2e_async_park_driver", "arguments": {"question": question, "expiry_seconds": 3600}},
        expect=202,
    )
    run_id = submitted["run_id"]

    async def _succeeded() -> dict[str, Any] | None:
        view = await api.get(f"/api/tool-runs/{run_id}")
        return view if view["status"] == "succeeded" else None

    await wait_for_async(_succeeded, deadline=20.0, message=f"run {run_id} never parked its ask")


async def _open_stream(stack: TaiStack, last_event_id: str) -> httpx.Response:
    url = f"{stack.origin(stack.port_b)}/api/interactions/stream"
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.get(url, headers={"Last-Event-ID": last_event_id})


async def test_a_resume_from_before_the_retained_window_is_gone(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    await _park_one_ask(replicas_stack, uniq("question"))

    response = await _open_stream(replicas_stack, "0-1")

    assert response.status_code == 410, response.text
    body = response.json()
    assert body["code"] == "interactions-resume-expired"
    assert "'0-1'" in body["error"]


async def test_a_resume_from_a_value_that_is_not_a_stream_id_is_refused(replicas_stack: TaiStack) -> None:
    response = await _open_stream(replicas_stack, "not-an-id")

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "interactions-resume-malformed"
