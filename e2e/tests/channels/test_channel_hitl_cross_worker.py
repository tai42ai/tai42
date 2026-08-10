"""Item B — the channel cross-worker loop, per medium (telegram/slack/twilio).

``ask_user(channel=...)`` blocks a tool call on replica A; the channel plugin
delivers ONE outbound send through its provider stub; a GENUINELY-signed inbound
reply arrives on replica B (the plugin's real signature verification runs); the
inbound handler correlates it through the shared Redis and forwards to the
callback door (also B-served, per ``INTERACTIONS_PUBLIC_BASE_URL``); the blocked
run resumes on A with the answer. A whole channel pipe — deliver → correlate →
inbound-verify → callback-forward — sits between the ask and its answer, across
process boundaries, with zero real network (the stub is in-process; an
unexpected provider call is a loud 500).

Out of scope (proven in each plugin's own suite): ``select`` rides the identical
Tier-2 path (different rendered text only); the provider redelivery/retry ladder.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from _support import (
    ChannelCase,
    await_true,
    cancel_and_join,
    count_correlation_keys,
    find_pending,
    is_pending,
    post_callback,
    post_inbound,
)

from tai42_e2e.waiting import wait_for_async

pytestmark = pytest.mark.backendless


async def _wait_one_send(case: ChannelCase, text: str) -> dict:
    """Wait until exactly one outbound send carrying ``text`` is recorded; return it."""

    async def probe() -> list[dict]:
        return case.sends_matching(text)

    records = await wait_for_async(
        probe, deadline=8.0, message=f"{case.name}: no outbound send carrying {text!r} was recorded"
    )
    assert len(records) == 1, f"{case.name}: expected exactly one send, saw {len(records)}"
    return records[0]


async def test_ask_via_channel_on_a_inbound_on_b_resolves_on_a(
    channel_case: ChannelCase, uniq: Callable[[str], str]
) -> None:
    case = channel_case
    stack = case.stack
    question = uniq(f"{case.name}_q")
    answer = uniq(f"{case.name}_a")

    async def ask() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool("ask_user", {"question": question, "channel": case.name})
        return result.data

    # Baseline the correlation keys BEFORE the ask, so the guard below waits for
    # THIS ask's key rather than leaning on any leftover.
    baseline_keys = count_correlation_keys(stack, case.correlation_prefix)
    ask_task = asyncio.create_task(ask())
    try:
        # The send lands on the default recipient (deliberately NOT allowlisted —
        # so this passing IS the trusted-default proof) with the medium's Tier-2
        # reply-correlation affordance.
        record = await _wait_one_send(case, question)
        assert case.outbound_recipient(record) == case.default_recipient
        case.assert_tier2_shape(record)

        # For text/select, telegram and slack write the correlation key AFTER the
        # send returns (twilio reserves before it), so the stub can record the send
        # a moment before the key exists. Wait for the key before posting the reply,
        # or a fast inbound could beat the write and be ignored (a CI-only flake).
        await await_true(
            lambda: count_correlation_keys(stack, case.correlation_prefix) > baseline_keys,
            deadline=5.0,
            message=f"{case.name}: Tier-2 delivery added no correlation key",
        )

        # The signed reply arrives on replica B (which never saw the ask); the
        # plugin verifies the signature and forwards through the shared Redis.
        reply = case.build_reply(record, answer, recipient=case.default_recipient)
        response = await post_inbound(stack, case.inbound_path, reply)
        case.assert_inbound_forwarded(response)

        resolved = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        await cancel_and_join(ask_task)

    # The BLPOP waiter woke across workers with the whole channel pipe in the middle.
    assert resolved == answer
    # The interaction left the pending set on BOTH replicas, and no duplicate send
    # ever happened (single-attempt policy — no idempotency key on these APIs).
    assert not await is_pending(stack, stack.port_a, question)
    assert not await is_pending(stack, stack.port_b, question)
    assert len(case.sends_matching(question)) == 1


async def test_inbound_rejected_fail_closed(channel_case: ChannelCase, uniq: Callable[[str], str]) -> None:
    case = channel_case
    stack = case.stack
    question = uniq(f"{case.name}_q")
    answer = uniq(f"{case.name}_a")

    async def ask() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool("ask_user", {"question": question, "channel": case.name})
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        record = await _wait_one_send(case, question)
        # Presence proof BEFORE the absence assert: the question is pending on B.
        await find_pending(stack, stack.port_b, question)

        # A wrong signature and a missing signature both fail closed with the
        # medium's one constant deny; the ask stays pending (unresolved).
        for mode in ("wrong", "missing"):
            reply = case.build_reply(record, answer, recipient=case.default_recipient, signature=mode)
            response = await post_inbound(stack, case.inbound_path, reply)
            assert response.status_code == case.deny_status, f"{case.name}: {mode} signature was not denied"
        assert await is_pending(stack, stack.port_a, question)
        assert not ask_task.done()

        # The CORRECT reply then resolves it — the success is the ordering sentinel.
        good = case.build_reply(record, answer, recipient=case.default_recipient)
        case.assert_inbound_forwarded(await post_inbound(stack, case.inbound_path, good))
        resolved = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        await cancel_and_join(ask_task)

    assert resolved == answer
    # Still exactly one send, ever — the denied inbounds triggered no re-send.
    assert len(case.sends_matching(question)) == 1


async def test_tier1_confirm_message_carries_callback_url(
    channel_case: ChannelCase, uniq: Callable[[str], str]
) -> None:
    case = channel_case
    stack = case.stack

    # In-spec presence proof for the correlation-absence assert below: a Tier-2
    # ask DOES add a correlation key, so the scan can see keys — a wrong prefix
    # anchor could not make the confirm absence pass vacuously.
    probe_q = uniq(f"{case.name}_probe")
    baseline = count_correlation_keys(stack, case.correlation_prefix)

    async def ask_probe() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool("ask_user", {"question": probe_q, "channel": case.name})
        return result.data

    probe_task = asyncio.create_task(ask_probe())
    try:
        probe_record = await _wait_one_send(case, probe_q)

        async def has_more_keys() -> bool:
            return count_correlation_keys(stack, case.correlation_prefix) > baseline

        await wait_for_async(
            has_more_keys, deadline=5.0, message=f"{case.name}: Tier-2 delivery added no correlation key"
        )
        # Clean up the throwaway ask so its key does not skew the confirm delta.
        good = case.build_reply(probe_record, uniq("ans"), recipient=case.default_recipient)
        case.assert_inbound_forwarded(await post_inbound(stack, case.inbound_path, good))
        await asyncio.wait_for(probe_task, timeout=15.0)
    finally:
        await cancel_and_join(probe_task)

    # Now the confirm ask: its message carries the callback URL as the answer
    # path, and it stores ZERO correlation keys (Tier-1).
    confirm_q = uniq(f"{case.name}_confirm")
    keys_before = count_correlation_keys(stack, case.correlation_prefix)

    async def ask_confirm() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool(
                "ask_user", {"question": confirm_q, "channel": case.name, "answer_format": "confirm"}
            )
        return result.data

    confirm_task = asyncio.create_task(ask_confirm())
    try:
        record = await _wait_one_send(case, confirm_q)
        callback_url = case.tier1_callback_url(record)
        assert "/api/interactions/callback/" in callback_url
        assert callback_url.startswith(f"http://{stack.host}:{stack.port_b}")
        # A confirm tap POSTs an empty body to the callback door (records True).
        response = await post_callback(callback_url, b"")
        assert response.status_code == 200
        resolved = await asyncio.wait_for(confirm_task, timeout=15.0)
    finally:
        await cancel_and_join(confirm_task)

    assert resolved is True
    assert count_correlation_keys(stack, case.correlation_prefix) == keys_before
    assert len(case.sends_matching(confirm_q)) == 1
