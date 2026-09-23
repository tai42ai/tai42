"""A caller ask is invisible on the human surfaces a user ask lives on.

A ``to="user"`` ask is a question for a person: it lists on ``/api/interactions``, streams on
the SSE tail, and is answered through the answer door. A ``to="caller"`` ask is a question for
another RUN, resolved by a run on its subject — so it must NOT leak onto any of those human
surfaces, and the human answer door must refuse it. This drives both asks on one stack and
checks the caller ask stays off the pending list and the stream while the user ask shows, and
that the answer door refuses the caller ask. The stack runs no backend seam, so the module is
``backendless``."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless


def _subject(key: str) -> dict[str, str]:
    return {"target_kind": "tool", "target_name": "held_run", "kind": "person", "key": key}


async def _submit(api: Any, tool_name: str, arguments: dict[str, Any], subject: dict[str, str] | None = None) -> str:
    body: dict[str, Any] = {"tool_name": tool_name, "arguments": arguments}
    if subject is not None:
        body["subject"] = subject
    submitted = await api.post("/api/tool-runs", json=body, expect=202, retry_on_reloading=True)
    return submitted["run_id"]


async def _pending_items(stack: TaiStack, port: int) -> list[dict[str, Any]]:
    url = f"http://{stack.host}:{port}/api/interactions"
    async with httpx.AsyncClient(timeout=2.0) as client:
        resp = await client.get(url, params={"page": 1, "pageSize": 200})
        resp.raise_for_status()
    return resp.json()["data"]["items"]


async def _await_run_result(api: Any, run_id: str, *, deadline: float = 20.0) -> Any:
    async def _reached() -> dict[str, Any] | None:
        view = await api.get(f"/api/tool-runs/{run_id}")
        return view if view["status"] == "succeeded" else None

    view = await wait_for_async(_reached, deadline=deadline, message=f"run {run_id} never succeeded")
    return view["result"]


async def test_a_caller_ask_stays_off_the_human_pending_surfaces(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)
    subject = _subject(uniq("subject"))
    user_question = uniq("user-question")
    caller_marker = uniq("caller-marker")

    # A user ask (a question for a person) submitted as a background run.
    await _submit(api_a, "ask", {"question": user_question})
    # A caller ask parked on the subject.
    await _submit(api_a, "held_run", {"marker": caller_marker}, subject)

    # Read the caller ask's id off its own subject (never a human surface).
    list_result = await _await_run_result(api_a, await _submit(api_b, "caller_list", {}, subject))
    assert len(list_result) == 1
    caller_ask_id = list_result[0]["id"]

    async def _user_ask_listed_without_the_caller_ask() -> list[dict[str, Any]] | None:
        items = await _pending_items(replicas_stack, replicas_stack.port_b)
        blob = json.dumps(items)
        if user_question not in blob:
            return None
        # The user ask is listed; the caller ask must be nowhere on this human surface.
        assert caller_marker not in blob
        assert all(item.get("interaction_id") != caller_ask_id for item in items)
        return items

    await wait_for_async(
        _user_ask_listed_without_the_caller_ask,
        deadline=10.0,
        message="the user ask never listed on the pending surface",
    )

    # The human answer door refuses the caller ask — it is not a human-answerable question.
    refused = await api_b.request_raw("POST", f"/api/interactions/{caller_ask_id}/answer", json={"answer": "no"})
    assert refused.status_code in {403, 404, 409}, refused.text


async def _await_status(api: Any, run_id: str, status: str, *, deadline: float = 20.0) -> dict[str, Any]:
    async def _reached() -> dict[str, Any] | None:
        view = await api.get(f"/api/tool-runs/{run_id}")
        return view if view["status"] == status else None

    return await wait_for_async(_reached, deadline=deadline, message=f"run {run_id} never reached {status}")


async def test_caller_asks_carry_a_separate_concurrency_cap_from_user_asks(
    caller_cap_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api_a = caller_cap_stack.api(port=caller_cap_stack.port_a)
    api_b = caller_cap_stack.api(port=caller_cap_stack.port_b)

    # The caller cap is pinned to 1: the first caller ask parks and fills it.
    first = await _submit(api_a, "held_run", {"marker": uniq("q1")}, _subject(uniq("s1")))
    await _await_status(api_a, first, "parked")

    # A second concurrent caller ask is refused by the caller cap, so its run fails.
    second = await _submit(api_b, "held_run", {"marker": uniq("q2")}, _subject(uniq("s2")))
    second_view = await _await_status(api_a, second, "failed")
    assert "caller" in (second_view["error"] or "").lower()

    # A user ask is bound by its OWN (default-high) cap, not the caller cap: it is admitted
    # while the caller cap is full.
    user = await _submit(api_b, "e2e_async_park_flow", {"question": uniq("uq"), "expiry_seconds": 3600})
    await _await_status(api_a, user, "succeeded")


def _frame_question(frame: str) -> str | None:
    """The ``question`` of an interactions SSE add-frame, or ``None`` for any other frame."""
    for line in frame.splitlines():
        if line.startswith("data:"):
            try:
                data = json.loads(line[len("data:") :].strip())
            except json.JSONDecodeError:
                return None
            return data.get("question") if isinstance(data, dict) else None
    return None


async def test_a_caller_ask_never_reaches_the_live_stream(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    subj = _subject(uniq("subject"))
    user_question = uniq("user-question")
    caller_marker = uniq("caller-marker")
    url = f"http://{replicas_stack.host}:{replicas_stack.port_b}/api/interactions/stream"

    async with httpx.AsyncClient(timeout=20.0) as client, client.stream("GET", url) as response:
        assert response.status_code == 200, response
        # Both asks are raised AFTER the tail cursor is captured, so each is a live-tail add: the
        # user ask must stream, the caller ask must be filtered off it.
        await _submit(api_a, "held_run", {"marker": caller_marker}, subj)
        await _submit(api_a, "e2e_async_park_flow", {"question": user_question, "expiry_seconds": 3600})

        async def _read_to_user_ask() -> bool:
            buffer = ""
            async for chunk in response.aiter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    frame, buffer = buffer.split("\n\n", 1)
                    question = _frame_question(frame)
                    # The caller ask must never appear on the human stream.
                    assert question != caller_marker, f"a caller ask leaked onto the stream: {frame!r}"
                    if question == user_question:
                        return True
            raise AssertionError("the stream ended before the user ask add frame")

        assert await asyncio.wait_for(_read_to_user_ask(), timeout=10.0)

    # The caller ask really did park (so its absence from the stream is the filter, not a no-op).
    list_run = await _submit(api_a, "caller_list", {}, subj)
    assert len(await _await_run_result(api_a, list_run)) == 1
