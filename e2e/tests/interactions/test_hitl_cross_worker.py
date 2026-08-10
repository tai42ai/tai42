"""C6 + C7 — a human-in-the-loop ``ask_user`` blocks its caller on replica A, the
pending interaction is observed and answered via replica B, and the waiter wakes
across workers (Redis blpop/rpush). The first flow raises the ask INSIDE a background
tool run, so its add frame carries the submitting run id as ``origin`` (no
recipient/audience passed, so both keys are absent), and the run reaches its terminal
state on B once answered. The external-callback variant proves the single-use answer
claim (WATCH/MULTI)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import redis as redis_lib

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack


async def _find_pending(stack: TaiStack, port: int, question: str, *, deadline: float = 8.0) -> dict:
    """Stream the interactions SSE on ``port`` until a pending interaction whose
    payload carries ``question`` appears; return its add-frame data."""
    url = f"http://{stack.host}:{port}/api/interactions/stream"

    async def probe() -> dict | None:
        async with httpx.AsyncClient(timeout=2.0) as client:
            try:
                async with client.stream("GET", url) as response:
                    buffer = ""
                    async for chunk in response.aiter_text():
                        buffer += chunk
                        while "\n\n" in buffer:
                            frame, buffer = buffer.split("\n\n", 1)
                            data = _match(frame, question)
                            if data is not None:
                                return data
                            if "backlog_done" in frame:
                                return None
            except httpx.HTTPError:
                return None
        return None

    found = await wait_for_async(
        probe, deadline=deadline, message=f"pending interaction for {question!r} never appeared on B"
    )
    assert found is not None
    return found


def _match(frame: str, question: str) -> dict | None:
    for line in frame.splitlines():
        if line.startswith("data:"):
            try:
                data = json.loads(line[len("data:") :].strip())
            except json.JSONDecodeError:
                return None
            if question in json.dumps(data):
                return data
    return None


async def test_ask_user_blocked_on_a_answered_via_b(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    question = uniq("question")
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)

    # Raise the ask INSIDE a background tool run on A: the supervisor binds the run id
    # as the interaction origin, so the question the run parks on carries it. The submit
    # can race the ~2s boot-time self-resync gate, so poll past a retriable ``reloading``.
    submitted = await api_a.post(
        "/api/tool-runs",
        json={"tool_name": "ask_user", "arguments": {"question": question}},
        expect=202,
        retry_on_reloading=True,
    )
    run_id = submitted["run_id"]

    add = await _find_pending(replicas_stack, replicas_stack.port_b, question)
    # Attribution rides the add frame: ``origin`` IS the submitting run; no recipient or
    # audience was passed, so both keys are ABSENT (the additive-wire idiom).
    assert add["origin"] == run_id
    assert "recipient" not in add
    assert "audience" not in add

    await api_b.post(f"/api/interactions/{add['interaction_id']}/answer", json={"answer": "yes-from-b"})

    # The parked run woke across workers: its terminal record — carrying the answer —
    # is readable on B.
    async def terminal_on_b() -> dict | None:
        view = await api_b.get(f"/api/tool-runs/{run_id}")
        return view if view["status"] == "succeeded" else None

    view = await wait_for_async(terminal_on_b, deadline=10.0, message="the background ask never reached succeeded on B")
    assert view is not None
    assert "yes-from-b" in json.dumps(view["result"])


async def test_external_callback_answer_is_single_use(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    question = uniq("question")

    async def ask() -> object:
        # An EXTERNAL-format question mints the callback ticket the public door
        # claims against; the caller blocks on the callback exactly as a text ask
        # blocks on POST /answer.
        async with replicas_stack.mcp(port=replicas_stack.port_a) as mcp:
            result = await mcp.call_tool(
                "ask_user",
                {
                    "question": question,
                    "answer_format": "external",
                    "link": "https://ext.example/act?cb={callback_url}",
                },
                retry_on_reloading=True,
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        add = await _find_pending(replicas_stack, replicas_stack.port_b, question)
        interaction_id = add["interaction_id"]
        api_b = replicas_stack.api(port=replicas_stack.port_b)
        # Resolve the ticket for this interaction, then answer via the public
        # callback door twice: first answers, second is an idempotent duplicate.
        ticket = _resolve_ticket(replicas_stack, interaction_id)
        first = await api_b.request_raw("POST", f"/api/interactions/callback/{ticket}", json={"answer": "once"})
        assert first.status_code == 200
        assert first.json()["data"]["status"] == "answered"
        second = await api_b.request_raw("POST", f"/api/interactions/callback/{ticket}", json={"answer": "twice"})
        assert second.status_code == 200
        assert second.json()["data"]["status"] == "already_answered"
        answer = await asyncio.wait_for(ask_task, timeout=10.0)
    finally:
        if not ask_task.done():
            ask_task.cancel()
    # The waiter woke exactly once, with the FIRST answer.
    assert "once" in json.dumps(answer)
    assert "twice" not in json.dumps(answer)


def _resolve_ticket(stack: TaiStack, interaction_id: str) -> str:
    """Recover the callback ticket for ``interaction_id`` from Redis.

    The ticket is a bearer capability the admin API never serializes; the store
    maps ``interactions:ticket:<ticket> -> interaction_id`` (a ``SET`` with a
    TTL), so scan those keys for the one pointing at this interaction."""
    host, port = stack.infra.settings.redis_host_port
    client = redis_lib.Redis(host=host, port=port, db=stack.resources.redis_idx, decode_responses=True)
    try:
        for key in client.scan_iter(match="interactions:ticket:*"):
            if client.get(key) == interaction_id:
                return key.rsplit(":", 1)[-1]
    finally:
        client.close()
    raise AssertionError(f"no callback ticket in Redis for interaction {interaction_id}")
