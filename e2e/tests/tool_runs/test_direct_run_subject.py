"""The direct tool-run doors carry a caller-named ``subject`` so an async park of the run
indexes under it.

The synchronous run-tool door (``POST /api/run-tool``) and the background submit door
(``POST /api/tool-runs``) both accept a ``{tool_name, arguments, subject}`` body and deposit the
same ``door="api"`` state context, so a tool whose ``ask(mode="async")`` parks lands the park on
the named subject and a later run on that same subject finds it, while a run on a different
subject does not.

Over ``replicas_stack`` (both doors mounted, the async-park probe tools present, access control
off): the probe ``e2e_async_park_flow`` parks a user ask as a side effect and returns the parked
``interaction_id``; ``list_parked`` run on the SAME subject lists that park (keyed by its question
marker), and run on a DIFFERENT subject lists nothing — the subject scoping the door deposits.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

# Far enough out that the park stays ``asking`` for the whole run — no expiry reaper races the
# subject read (the async ask only resolves on an answer or its deadline, neither of which the
# subject legs drive).
_PARK_EXPIRY_SECONDS = 3600.0


def _subject(key: str) -> dict[str, str]:
    return {"target_kind": "agent", "target_name": "a-42", "kind": "job", "key": key}


async def _run_tool(api: Any, tool_name: str, arguments: dict[str, Any], subject: dict[str, str]) -> Any:
    return await api.post(
        "/api/run-tool",
        json={"tool_name": tool_name, "arguments": arguments, "subject": subject},
        expect=200,
        retry_on_reloading=True,
    )


async def _parked_ids(api: Any, subject: dict[str, str]) -> list[str]:
    """The ids ``list_parked`` reports for ``subject`` — the platform's own subject-scoped read,
    driven through the run-tool door so it runs under that subject's deposited context."""
    entries = await _run_tool(api, "list_parked", {}, subject)
    return [entry["id"] for entry in entries]


async def _parked_entry(api: Any, subject: dict[str, str], interaction_id: str) -> dict[str, Any] | None:
    entries = await _run_tool(api, "list_parked", {}, subject)
    return next((entry for entry in entries if entry["id"] == interaction_id), None)


async def test_sync_run_tool_door_carries_subject(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = replicas_stack.api(port=replicas_stack.port_a)
    subject = _subject(uniq("subj"))
    other = _subject(uniq("other"))
    marker = uniq("sync-park")

    parked = await _run_tool(
        api, "e2e_async_park_flow", {"question": marker, "expiry_seconds": _PARK_EXPIRY_SECONDS}, subject
    )
    interaction_id = parked["interaction_id"]

    # The named subject lists the park the sync door indexed under it, carrying the question marker.
    entry = await _parked_entry(api, subject, interaction_id)
    assert entry is not None, f"the park {interaction_id!r} did not index under its subject"
    assert entry["status"] == "asking", entry
    assert marker in entry["question"], entry

    # A different subject scope sees nothing of it — the deposit is per-subject, not global.
    assert interaction_id not in await _parked_ids(api, other)


async def test_background_submit_door_carries_subject(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = replicas_stack.api(port=replicas_stack.port_a)
    subject = _subject(uniq("subj"))
    other = _subject(uniq("other"))
    marker = uniq("bg-park")

    submitted = await api.post(
        "/api/tool-runs",
        json={
            "tool_name": "e2e_async_park_flow",
            "arguments": {"question": marker, "expiry_seconds": _PARK_EXPIRY_SECONDS},
            "subject": subject,
        },
        expect=202,
        retry_on_reloading=True,
    )
    run_id = submitted["run_id"]

    async def _terminal() -> dict[str, Any] | None:
        view = await api.get(f"/api/tool-runs/{run_id}")
        return view if view["status"] == "succeeded" else None

    view = await wait_for_async(_terminal, deadline=15.0, message=f"background run {run_id} never succeeded")
    interaction_id = view["result"]["interaction_id"]

    # The submit door indexed the park under the named subject; the same subject lists it.
    entry = await _parked_entry(api, subject, interaction_id)
    assert entry is not None, f"the background-run park {interaction_id!r} did not index under its subject"
    assert marker in entry["question"], entry

    assert interaction_id not in await _parked_ids(api, other)
