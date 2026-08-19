"""Item C — the F1 recipient allowlist, end to end, per medium.

An allowlisted caller-supplied recipient is delivered to (and its reply routes
home); an unlisted one is refused BEFORE any provider call — fail closed, nothing
sent, the blocked ask pruned. The empty-recipient / recipient-without-channel
rejections are skeleton-validation paths covered offline, not repeated here."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from ._support import ChannelCase, await_true, cancel_and_join, find_add, is_pending, post_inbound, tool_content_text

pytestmark = pytest.mark.backendless


async def test_allowlisted_recipient_is_delivered_to(channel_case: ChannelCase, uniq: Callable[[str], str]) -> None:
    case = channel_case
    stack = case.stack
    question = uniq(f"{case.name}_q")
    answer = uniq(f"{case.name}_a")

    async def ask() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool(
                "ask_user", {"question": question, "channel": case.name, "recipient": case.allowlisted_recipient}
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        records = await await_true(
            lambda: case.sends_matching(question),
            deadline=8.0,
            message=f"{case.name}: no send to the allowlisted recipient",
        )
        assert len(records) == 1
        assert case.outbound_recipient(records[0]) == case.allowlisted_recipient

        # The in-app add frame carries the caller-supplied delivery recipient verbatim
        # (a WHERE, echoed present-value onto the stream for display).
        add = await find_add(stack, stack.port_a, question)
        assert add["recipient"] == case.allowlisted_recipient

        # An allowlisted non-default recipient's reply routes home too.
        reply = case.build_reply(records[0], answer, recipient=case.allowlisted_recipient)
        case.assert_inbound_forwarded(await post_inbound(stack, case.inbound_path, reply))
        resolved = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        await cancel_and_join(ask_task)

    assert resolved == answer
    # Exactly one send to the allowlisted recipient, ever (no duplicate delivery).
    assert len(case.sends_matching(question)) == 1


async def test_unlisted_recipient_fails_closed(channel_case: ChannelCase, uniq: Callable[[str], str]) -> None:
    case = channel_case
    stack = case.stack
    question = uniq(f"{case.name}_q")

    async with stack.mcp(port=stack.port_a) as mcp:
        result = await mcp.call_tool(
            "ask_user",
            {"question": question, "channel": case.name, "recipient": case.unlisted_recipient},
            raise_on_error=False,
        )

    # The tool fails LOUDLY, carrying the stable refusal substring (the exception
    # class does not survive MCP serialization; the message does).
    assert result.is_error
    assert f"is not on {case.allowlist_env}" in tool_content_text(result)
    # Fail closed: nothing was sent, and the pruned question is pending nowhere.
    assert case.sends_matching(question) == []
    assert not await is_pending(stack, stack.port_a, question)
    assert not await is_pending(stack, stack.port_b, question)
