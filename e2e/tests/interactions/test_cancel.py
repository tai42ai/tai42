"""Cancel — the operator withdraws one pending ask through the REST cancel door, and
the composed consumer seam behaves exactly like an expiry: the parked caller never
resumes, and a later reply forwarded to the callback door (the channel's bridge
target) gets a 404 — the precise signal the inbound ladder maps to a fresh bridged
turn (``channels.inbound.handle_inbound_answer``: door 404 -> release + BRIDGED).

There is no real inbound-channel ladder in the core e2e stack (the stub channel is
deliver-only and the HITL legs bridge replies by POSTing the ticket to the callback
door themselves), so the reachable composed proof is the callback-door 404 that the
ladder consumes; the ladder's 404 -> fresh-turn mapping itself is pinned by the
skeleton unit suite (``tests/channels/test_inbound_answer.py``) and per channel plugin.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import redis as redis_lib

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack


async def _find_pending(stack: TaiStack, port: int, question: str, *, deadline: float = 8.0) -> dict:
    url = f"http://{stack.host}:{port}/api/interactions"

    async def probe() -> dict | None:
        async with httpx.AsyncClient(timeout=2.0) as client:
            try:
                resp = await client.get(url, params={"page": 1, "pageSize": 200})
                resp.raise_for_status()
            except httpx.HTTPError:
                return None
            for item in resp.json()["data"]["items"]:
                if question in json.dumps(item):
                    return item
        return None

    found = await wait_for_async(
        probe, deadline=deadline, message=f"pending interaction for {question!r} never appeared"
    )
    assert found is not None
    return found


async def _is_pending(stack: TaiStack, port: int, question: str) -> bool:
    url = f"http://{stack.host}:{port}/api/interactions"
    async with httpx.AsyncClient(timeout=2.0) as client:
        resp = await client.get(url, params={"page": 1, "pageSize": 200})
        resp.raise_for_status()
        return any(question in json.dumps(item) for item in resp.json()["data"]["items"])


def _resolve_ticket(stack: TaiStack, interaction_id: str) -> str:
    """Recover the callback ticket for ``interaction_id`` from Redis (a bearer
    capability the admin API never serializes)."""
    host, port = stack.infra.settings.redis_host_port
    client = redis_lib.Redis(host=host, port=port, db=stack.resources.redis_idx, decode_responses=True)
    try:
        for key in client.scan_iter(match="interactions:ticket:*"):
            if client.get(key) == interaction_id:
                return key.rsplit(":", 1)[-1]
    finally:
        client.close()
    raise AssertionError(f"no callback ticket in Redis for interaction {interaction_id}")


async def test_cancel_withdraws_a_pending_ask_and_a_later_reply_bridges_fresh(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    question = uniq("question")

    async def ask() -> object:
        # An EXTERNAL ask mints the callback ticket the guest-reply forward targets; the
        # caller blocks on the callback exactly as a text ask blocks on POST /answer.
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
        ticket = _resolve_ticket(replicas_stack, interaction_id)
        api_b = replicas_stack.api(port=replicas_stack.port_b)

        # The operator WITHDRAWS the ask through the REST cancel door on replica B.
        cancelled = await api_b.post(f"/api/interactions/{interaction_id}/cancel")
        assert cancelled == {"interaction_id": interaction_id, "status": "cancelled"}

        # Gone from the pending inbox on BOTH replicas — the withdrawal is durable.
        assert not await _is_pending(replicas_stack, replicas_stack.port_a, question)
        assert not await _is_pending(replicas_stack, replicas_stack.port_b, question)

        # The parked caller never resumed: the cancel fired no continuation and pushed no
        # answer, so the blocking ask is still waiting (it will only ever time out).
        assert not ask_task.done()

        # A later reply forwarded to the callback door (the channel's bridge target) finds
        # the state gone and gets a 404 — the exact signal the inbound ladder maps to a
        # fresh bridged turn, identical to an expiry removal.
        late_callback = await api_b.request_raw("POST", f"/api/interactions/callback/{ticket}", json={"answer": "late"})
        assert late_callback.status_code == 404

        # The authenticated answer door likewise 404s a withdrawn ask.
        late_answer = await api_b.request_raw(
            "POST", f"/api/interactions/{interaction_id}/answer", json={"answer": "x"}
        )
        assert late_answer.status_code == 404

        # Re-cancel is a clean 404 (the store prune is idempotent; nothing double-torn).
        recancel = await api_b.request_raw("POST", f"/api/interactions/{interaction_id}/cancel")
        assert recancel.status_code == 404
    finally:
        if not ask_task.done():
            ask_task.cancel()
