"""A dispatch that ends on a ``ResumeBuffered`` is recorded parked, and its binding updates do not apply.

A ``ResumeBuffered`` is what a resume that answered one ask of several returns: the step's other
asks are still open, so the run stays parked. ``e2e_resume_buffered`` (a tool that declares it can
pause) returns one when called with ``buffer`` set, else a plain result. A registered preset over
it carries a state binding whose update writes the call's ``mark`` onto the subject record named
by the call's ``key``.

Over the real MCP door:

* the control call (a plain result) answers normally, its update writes the record, and the runs
  index records the run ``success``;
* the buffered call on the same subject answers the reserved resume-buffered marker (not an
  error), the record keeps the control's ``mark`` (no update applied), and ``GET /api/runs``
  records the run ``parked`` with ``interactionId`` null (a fresh dispatch carries no resume
  origin, and the still-open ids are not a park of its own).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs("kind:states", "kind:versioning", "probe-tools", "topology:replicas"),
]

_TOOL = "e2e_resume_buffered"
_RESUME_BUFFERED_MARKER_KEY = "tai42:resume_buffered"


def _record_path(state: str, key: str) -> str:
    parts = [quote(part, safe="") for part in (state, "agent", "relay", "thread", key)]
    return "/api/states/{}/records/{}/{}/{}/{}".format(*parts)


async def _runs(api: ApiClient, preset: str, outcome: str) -> list[dict[str, Any]]:
    page: Any = await api.get(f"/api/runs?preset={quote(preset, safe='')}&outcome={outcome}")
    return list(page["items"])


async def test_a_resume_buffered_dispatch_over_mcp_records_parked_and_applies_no_update(
    async_park_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = async_park_stack.api()
    state = uniq("buffered")
    preset = uniq("buffered-preset")
    key = uniq("subject")

    await api.put(
        f"/api/states/{state}",
        json={
            "description": "e2e resume-buffered binding target",
            "schema": {"type": "object", "properties": {"mark": {"type": "string"}}},
            "subject_kinds": ["thread"],
            "default_subject_kind": "thread",
        },
    )
    await api.post(
        "/api/presets",
        json={
            "name": preset,
            "base_tool": _TOOL,
            "description": "a pausing tool under a state binding",
            "fixed_kwargs": {"remaining_ids": ["open-1", "open-2"]},
            "state_binding": {
                "states": [
                    {
                        "state": state,
                        "subject_expr": {
                            "content": '{target_kind: "agent", target_name: "relay", kind: "thread", key: .key}'
                        },
                        "updates": [{"jq": {"content": '[{op: "set", path: ["mark"], value: $input.mark}]'}}],
                    }
                ]
            },
        },
        retry_on_reloading=True,
    )

    async with async_park_stack.mcp() as mcp:
        control = await mcp.call_tool(
            preset, {"key": key, "mark": "control", "buffer": False}, raise_on_error=False, retry_on_reloading=True
        )
        buffered = await mcp.call_tool(
            preset, {"key": key, "mark": "buffered", "buffer": True}, raise_on_error=False, retry_on_reloading=True
        )

    assert control.is_error is False, control.content
    assert buffered.is_error is False, buffered.content
    assert buffered.structured_content == {_RESUME_BUFFERED_MARKER_KEY: {"remaining_ids": ["open-1", "open-2"]}}

    # The control's update wrote the record; the buffered call's update did not apply.
    record: Any = await api.get(_record_path(state, key))
    assert record["data"] == {"mark": "control"}

    async def both_rows_terminal() -> bool:
        return bool(await _runs(api, preset, "success")) and bool(await _runs(api, preset, "parked"))

    await wait_for_async(both_rows_terminal, deadline=30.0, message="the runs index never recorded both terminals")
    [succeeded] = await _runs(api, preset, "success")
    [parked] = await _runs(api, preset, "parked")
    assert succeeded["interactionId"] is None
    assert parked["interactionId"] is None
    assert parked["endedAt"] is not None
