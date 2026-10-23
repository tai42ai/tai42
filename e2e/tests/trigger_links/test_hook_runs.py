"""A trigger-link fire is a kept hook run that carries the link's execution identity.

A fire runs the hook as the link's execution key and leaves no run behind. Once that key is
revoked, the next fire is still answered 200 (the event is kept), and its run is held as failed
with ``unauthorized`` before the hook acted, reported to the operator feed, until an operator
discards it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

from ._trigger_support import mint_link, no_auth, record_values, register_record_hook, wait_records

pytestmark = pytest.mark.needs("kind:identity", "setting:seeded-access-control", "probe-tools", "store:redis")


async def _runs(admin: ApiClient, topic: str, status: str | None = None) -> list[dict[str, Any]]:
    query = "limit=200" if status is None else f"status={status}&limit=200"
    page = await admin.get(f"/api/hook-runs?{query}")
    return [run for run in page["items"] if run["topic"] == topic]


async def test_a_fire_after_its_execution_key_is_revoked_is_held_failed_as_unauthorized(
    trigger_stack: TaiStack, uniq: Callable[[str], str], exec_key: str
) -> None:
    stack = trigger_stack
    admin = stack.api(port=stack.port_a)
    topic = uniq("topic").replace("_", "-")
    rkey = uniq("rec")
    await register_record_hook(
        admin, topic, name=uniq("hook"), execution_key=exec_key, tool_kwargs={"key": rkey}, start_expr="{value: .x}"
    )
    link = await mint_link(admin, topic, ttl_seconds=None, execution_key=exec_key)
    public = no_auth(stack)

    first = await public.request_raw("POST", f"/trigger/{link['token']}", json={"x": "one"})
    assert (first.status_code, first.json()) == (200, {"status": "accepted"})
    assert wait_records(stack, rkey, count=1) == ["one"]

    async def finished() -> bool:
        return not await _runs(admin, topic)

    await wait_for_async(finished, deadline=10.0, message="the first fire's hook run was never finished")

    await admin.delete(f"/api/auth/api-keys/{exec_key}")
    second = await public.request_raw("POST", f"/trigger/{link['token']}", json={"x": "two"})
    assert (second.status_code, second.json()) == (200, {"status": "accepted"})

    async def failed() -> dict[str, Any] | None:
        runs = await _runs(admin, topic, "failed")
        return runs[0] if runs else None

    run = await wait_for_async(failed, deadline=10.0, message="the revoked-key fire was not held as failed")
    assert (run["last_error_kind"], run["acted"], run["attempts"]) == ("unauthorized", "", 1)
    assert record_values(stack, rkey) == ["one"]

    async def notified() -> bool:
        feed = await admin.get("/api/notifications")
        return any(run["id"] in note["message"] for note in feed["notifications"])

    await wait_for_async(notified, deadline=10.0, message=f"no operator notification names hook run {run['id']}")

    assert await admin.delete(f"/api/hook-runs/{run['id']}") == {"discarded": run["id"]}
    assert await _runs(admin, topic) == []
