"""Item D — ``notify_user`` end to end.

D.1 (per medium): the external notify path is plain and stateless — ONE plain
send, no reply markup, no interaction, no correlation key. D.2: ``channel=None``
lands in the internal notifications sink and reads back cross-worker (written on
A, read on B through the shared interactions Redis) — no channel plugin involved.
D.3 (per medium): an unlisted recipient fails closed with the stable refusal
substring, nothing sent, nothing stored (notify has nothing to prune)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from tai42_e2e.netfixtures import FakeSlack, FakeTelegram, FakeTwilio
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

from ._support import (
    ChannelCase,
    await_true,
    cancel_and_join,
    count_correlation_keys,
    is_pending,
    post_inbound,
    tool_content_text,
)

pytestmark = pytest.mark.backendless


async def test_notify_external_is_plain_and_stateless(channel_case: ChannelCase, uniq: Callable[[str], str]) -> None:
    case = channel_case
    stack = case.stack

    # In-spec presence proof for the correlation-absence assert: a Tier-2 ask DOES
    # add a correlation key, so the scan can see keys.
    probe_q = uniq(f"{case.name}_probe")
    baseline = count_correlation_keys(stack, case.correlation_prefix)

    async def ask_probe() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool("ask_user", {"question": probe_q, "channel": case.name})
        return result.data

    probe_task = asyncio.create_task(ask_probe())
    try:
        probe_record = await await_true(
            lambda: case.sends_matching(probe_q), deadline=8.0, message=f"{case.name}: Tier-2 probe not delivered"
        )
        await await_true(
            lambda: count_correlation_keys(stack, case.correlation_prefix) > baseline,
            deadline=5.0,
            message=f"{case.name}: Tier-2 delivery added no correlation key",
        )
        good = case.build_reply(probe_record[0], uniq("ans"), recipient=case.default_recipient)
        case.assert_inbound_forwarded(await post_inbound(stack, case.inbound_path, good))
        await asyncio.wait_for(probe_task, timeout=15.0)
    finally:
        await cancel_and_join(probe_task)

    # notify_user completes without blocking (no interaction, no reply wait).
    message = uniq(f"{case.name}_notify")
    keys_before = count_correlation_keys(stack, case.correlation_prefix)
    async with stack.mcp(port=stack.port_a) as mcp:
        result = await mcp.call_tool(
            "notify_user", {"message": message, "channel": case.name, "recipient": case.allowlisted_recipient}
        )
    assert f"notification sent via '{case.name}'" in tool_content_text(result)

    records = case.sends_matching(message)
    assert len(records) == 1
    assert case.outbound_recipient(records[0]) == case.allowlisted_recipient
    case.assert_notify_plain(records[0], message)
    # Fire-and-forget: no interaction anywhere, and ZERO correlation keys added.
    assert not await is_pending(stack, stack.port_a, message)
    assert not await is_pending(stack, stack.port_b, message)
    assert count_correlation_keys(stack, case.correlation_prefix) == keys_before


async def test_notify_default_sink_lands_internally(
    channel_stack: TaiStack,
    fake_telegram: FakeTelegram,
    fake_slack: FakeSlack,
    fake_twilio: FakeTwilio,
    uniq: Callable[[str], str],
) -> None:
    message = uniq("sink_notify")

    async with channel_stack.mcp(port=channel_stack.port_a) as mcp:
        result = await mcp.call_tool("notify_user", {"message": message})
    assert "recorded to the internal sink" in tool_content_text(result)

    # Written on A, read on B through the shared interactions Redis (notifications
    # namespace) — cross-worker, no channel plugin involved.
    async def sink_has_message() -> bool:
        data = await channel_stack.api(port=channel_stack.port_b).get("/api/notifications")
        return any(n["message"] == message for n in data["notifications"])

    await wait_for_async(sink_has_message, deadline=8.0, message="notification never reached the internal sink on B")

    # No channel stub recorded anything for this message.
    for stub in (fake_telegram, fake_slack, fake_twilio):
        assert stub.sends_matching(message) == []


async def test_notify_unlisted_recipient_fails_closed(channel_case: ChannelCase, uniq: Callable[[str], str]) -> None:
    case = channel_case
    message = uniq(f"{case.name}_notify")

    async with case.stack.mcp(port=case.stack.port_a) as mcp:
        result = await mcp.call_tool(
            "notify_user",
            {"message": message, "channel": case.name, "recipient": case.unlisted_recipient},
            raise_on_error=False,
        )

    assert result.is_error
    assert f"is not on {case.allowlist_env}" in tool_content_text(result)
    # Nothing sent, and notify stores nothing — plain absence, nothing to prune.
    assert case.sends_matching(message) == []
