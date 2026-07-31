"""C6 + C7 — a human-in-the-loop ``ask_user`` blocks a tool call on replica A;
the pending interaction is observed and answered via replica B; the waiter wakes
across workers (Redis blpop/rpush). The external-callback variant proves the
single-use answer claim (WATCH/MULTI)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import redis as redis_lib

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack


async def _find_pending(stack: TaiStack, port: int, question: str, *, deadline: float = 8.0) -> str:
    """Stream the interactions SSE on ``port`` until a pending interaction whose
    payload carries ``question`` appears; return its id."""
    url = f"http://{stack.host}:{port}/api/interactions/stream"

    async def probe() -> str | None:
        async with httpx.AsyncClient(timeout=2.0) as client:
            try:
                async with client.stream("GET", url) as response:
                    buffer = ""
                    async for chunk in response.aiter_text():
                        buffer += chunk
                        while "\n\n" in buffer:
                            frame, buffer = buffer.split("\n\n", 1)
                            interaction_id = _match(frame, question)
                            if interaction_id is not None:
                                return interaction_id
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


def _match(frame: str, question: str) -> str | None:
    for line in frame.splitlines():
        if line.startswith("data:"):
            try:
                data = json.loads(line[len("data:") :].strip())
            except json.JSONDecodeError:
                return None
            if question in json.dumps(data):
                return data.get("interaction_id") or data.get("id")
    return None


async def test_ask_user_blocked_on_a_answered_via_b(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    question = uniq("question")

    async def ask() -> object:
        async with replicas_stack.mcp(port=replicas_stack.port_a) as mcp:
            # A boot-time ask races the ~2s self-resync gate (a retriable ``reloading``);
            # poll past it so the call parks on the real question, not the reload gate.
            result = await mcp.call_tool("ask_user", {"question": question}, retry_on_reloading=True)
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        interaction_id = await _find_pending(replicas_stack, replicas_stack.port_b, question)
        api_b = replicas_stack.api(port=replicas_stack.port_b)
        await api_b.post(f"/api/interactions/{interaction_id}/answer", json={"answer": "yes-from-b"})
        answer = await asyncio.wait_for(ask_task, timeout=10.0)
    finally:
        if not ask_task.done():
            ask_task.cancel()
    assert "yes-from-b" in json.dumps(answer)


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
        interaction_id = await _find_pending(replicas_stack, replicas_stack.port_b, question)
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
