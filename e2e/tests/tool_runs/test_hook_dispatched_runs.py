"""A hook-dispatched tool fire is recorded in the tool-runs store: it
lists under ``GET /api/tool-runs?tool_name=...`` and is gettable by run id with a
terminal status, exactly as a run submitted through ``POST /api/tool-runs`` (the
recording lifecycle lives in Redis, so the fire on replica B is observable on A)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from tai42_e2e import wait_for, wait_for_async
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs(
    "kind:tool_runs",
    "probe-tools",
    "store:redis",
    "topology:replicas",
)


async def test_hook_fired_run_is_recorded_and_listable(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    topic = uniq("topic").replace("_", "-")
    rkey = uniq("rec")
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)

    await api_a.post(
        "/api/hooks",
        json={
            "name": uniq("hook"),
            "topic": topic,
            "tool": "e2e_record",
            "tool_kwargs": {"key": rkey, "value": "fired"},
            "execution_key": uniq("exec"),
        },
    )
    await api_b.request_raw("POST", f"/universal_webhook/{topic}", json={"any": "payload"})

    # The delivery on B dispatched the hook's tool (side-channel record it RPUSH'd).
    wait_for(
        lambda: len(replicas_stack.records(rkey)) > 0,
        deadline=5.0,
        message="the hook bound on A never fired its tool from the webhook on B",
    )

    # That fire was recorded in the tool-runs store: it lists under the tool name and
    # is gettable by its run id with a terminal ``succeeded`` status — the same record
    # lifecycle a submitted run writes, under the hook's own execution key.
    async def recorded_terminal() -> dict | None:
        listed = await api_a.get("/api/tool-runs?tool_name=e2e_record")
        for entry in listed:
            view = await api_a.get(f"/api/tool-runs/{entry['run_id']}")
            if view["status"] == "succeeded":
                return view
        return None

    predicate: Callable[[], Awaitable[dict | None]] = recorded_terminal
    view = await wait_for_async(
        predicate,
        deadline=10.0,
        message="the hook-fired run never appeared recorded terminal in the tool-runs store",
    )
    assert view["tool_name"] == "e2e_record"
    assert view["run_id"]


async def test_a_hook_fired_failed_terminal_records_the_plain_message_and_its_outcome(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    topic = uniq("topic").replace("_", "-")
    reason = uniq("reason")
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)
    await api_a.post(
        "/api/hooks",
        json={
            "name": uniq("hook"),
            "topic": topic,
            "tool": "e2e_failed_terminal",
            "tool_kwargs": {"reason": reason},
            "execution_key": uniq("exec"),
        },
    )
    await api_b.request_raw("POST", f"/universal_webhook/{topic}", json={"any": "payload"})

    async def recorded_failure() -> dict | None:
        listed = await api_a.get("/api/tool-runs?tool_name=e2e_failed_terminal")
        for entry in listed:
            view = await api_a.get(f"/api/tool-runs/{entry['run_id']}")
            if view["status"] == "failed" and view.get("outcome") == {"reason": reason}:
                return view
        return None

    view = await wait_for_async(
        recorded_failure, deadline=10.0, message="the hook-fired failed terminal never recorded its outcome"
    )
    assert view["error"] == "tool run failed", view
