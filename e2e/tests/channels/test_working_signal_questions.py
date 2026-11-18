"""The working-on-it indicator while a run's questions to its user stand, end to end.

One run asks its user two questions at once (``e2e_sibling_asks_park``) and finishes only when
both are answered (``e2e_sibling_asks_resume``). While a question stands the user is the one who
owes an answer, so no indicator shows; once the last one is answered the run owes the user its
reply, so the indicator shows until that reply goes out:

* web, answered one at a time: no active ``chat.working`` frame follows the first answer; active
  frames follow the second, up to the reply;
* web, two questions to the caller that one message answers (the route's ``resume_expr`` resumes
  both from ``$parked``): active frames arrive while the run works, up to the reply;
* the ``stub_working`` counting channel, answered one at a time: no indicator call follows the
  first answer; indicator calls follow the second.

The web legs read the frames the way the page does, off the chat stream; the stub leg reads the
calls the stub channel recorded.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from typing import cast

import pytest
import redis as redis_lib

from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_absent_async, wait_for_async
from tai42_e2e.webchat import WebChatClient
from tai42_e2e_fixtures.stub_addressing import STUB_DEFAULT_IDENTITY

pytestmark = pytest.mark.needs(
    "kind:identity",
    "probe-tools",
    "store:redis",
    "setting:conversations:redis",
    "setting:seeded-access-control",
    "setting:tai42_e2e_fixtures.stub_channel",
)

_WORKING = "chat.working"

# How long the fire that completes the run holds it open before its reply: past the web channel's
# refresh interval, so at least one refresh lands after the probe shows the run resumed.
_HOLD_SECONDS = 7.0

# How long a leg watches for an indicator after the first answer: past the immediate assert an
# opened stretch sends, and past one stub refresh interval.
_QUIET_SECONDS = 3.0

# The web route's door contract when one message answers every standing caller question: a
# message resumes each with its text, and starts the run only when none stands.
_RESUME_ALL_EXPR = (
    '.message as $text | [$parked[] | select(.status == "asking" and .to == "caller") | {id: .id, payload: $text}]'
)
_START_UNLESS_PARKED = "if ($parked | length) > 0 then null else {start} end"


def _park_start_expr(marker: str, channel: str, *, recipient_prefix: str = "") -> str:
    recipient = f'("{recipient_prefix}" + .sender)' if recipient_prefix else ".sender"
    return f'{{marker: "{marker}", channel: "{channel}", recipient: {recipient}, hold_seconds: {_HOLD_SECONDS}}}'


def _caller_park_start_expr(marker: str) -> str:
    start = f'{{marker: "{marker}", to: "caller", hold_seconds: {_HOLD_SECONDS}}}'
    return _START_UNLESS_PARKED.format(start=start)


async def _create_route(
    stack: TaiStack,
    uniq: Callable[[str], str],
    *,
    channel: str,
    our_identity: str,
    start_expr: str,
    resume_expr: str | None = None,
) -> None:
    execution_key = uniq("wsq-exec")
    await stack.api(stack.port_b).post(
        "/api/auth/api-keys",
        json={"user_id": execution_key, "description": "e2e working-signal questions key", "scopes": ["e2e-all"]},
    )
    body: dict[str, object] = {
        "door": "channel",
        "target_kind": "tool",
        "target_name": "e2e_sibling_asks_park",
        "execution_key": execution_key,
        "channel": channel,
        "our_identity": our_identity,
        "start_expr": {"content": start_expr},
        "reply_expr": {"content": ".result.answer"},
    }
    if resume_expr is not None:
        body["resume_expr"] = {"content": resume_expr}
    await stack.api(stack.port_b).post(f"/api/conversations/{uniq('wsq-route').replace('_', '-')}", json=body)


async def _wait_records(stack: TaiStack, key: str, count: int, *, deadline: float = 30.0) -> list[dict]:
    async def probe() -> list[dict] | None:
        records = [json.loads(raw) for raw in stack.records(key)]
        return records if len(records) >= count else None

    return await wait_for_async(probe, deadline=deadline, message=f"fewer than {count} probe records under {key!r}")


async def _parked_questions(stack: TaiStack, marker: str) -> tuple[str, str]:
    (park,) = await _wait_records(stack, f"sibling_asks_park:{marker}", 1)
    first, second = park["interaction_ids"]
    return first, second


def _active(frames: list[tuple[str, dict]]) -> list[dict]:
    return [data for event, data in frames if event == _WORKING and data["active"]]


def _reply_out(text: str) -> Callable[[str, dict], bool]:
    return lambda event, data: event == "chat.message" and data["direction"] == "out" and data["text"] == text


class _StreamTail:
    """Every frame the visitor's chat stream sends, collected live until the run's reply arrives.

    The stream is read through ``WebChatClient.frames``, whose predicate sees every frame: the
    predicate records each one and accepts the reply, so ``frames`` holds what arrived so far and
    the task ends at the reply."""

    def __init__(self, web: WebChatClient, reply: str) -> None:
        self.frames: list[tuple[str, dict]] = []
        self._is_reply = _reply_out(reply)
        self._task = asyncio.create_task(web.frames(until=self._take, deadline=90.0))

    def _take(self, event: str, data: dict) -> bool:
        self.frames.append((event, data))
        return self._is_reply(event, data)

    def active_since(self, since_ms: int) -> list[dict]:
        """The active working frames the store wrote at or after ``since_ms``, by their stream id."""
        return [data for data in _active(self.frames) if int(data["id"].split("-")[0]) >= since_ms]

    async def until_reply(self) -> None:
        await asyncio.wait_for(asyncio.shield(self._task), timeout=90.0)

    async def close(self) -> None:
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, AssertionError):
            await self._task


@contextlib.asynccontextmanager
async def _tail(web: WebChatClient, reply: str) -> AsyncIterator[_StreamTail]:
    tail = _StreamTail(web, reply)
    try:
        yield tail
    finally:
        await tail.close()


def _store_now_ms(stack: TaiStack) -> int:
    """The store's clock in milliseconds: the time part of the stream id of a frame written now."""
    client = redis_lib.Redis.from_url(stack.resources.redis_url)
    try:
        seconds, micros = cast("tuple[int, int]", client.time())
    finally:
        client.close()
    return seconds * 1000 + micros // 1000


async def _open_web_visitor(
    stack: TaiStack, uniq: Callable[[str], str], marker: str, *, to_caller: bool = False
) -> WebChatClient:
    identity = uniq("wsq-site").replace("_", "-")
    if to_caller:
        start_expr, resume_expr = _caller_park_start_expr(marker), _RESUME_ALL_EXPR
    else:
        start_expr, resume_expr = _park_start_expr(marker, "web", recipient_prefix=f"{identity}:"), None
    await _create_route(
        stack, uniq, channel="web", our_identity=identity, start_expr=start_expr, resume_expr=resume_expr
    )
    web, page = await WebChatClient.open_page(stack.origin(stack.port_b), identity, store_url=stack.resources.redis_url)
    assert page.status_code == 200, page.text
    return web


@pytest.mark.needs("kind:channels:web")
async def test_web_shows_no_working_frame_while_a_sibling_question_stands_then_shows_it_until_the_reply(
    working_signal_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    stack, _root_token = working_signal_stack
    marker = uniq("wsq-web")
    web = await _open_web_visitor(stack, uniq, marker)

    sent = await web.send(uniq("wsq-web-msg"))
    assert sent.status_code == 200, sent.text
    first, second = await _parked_questions(stack, marker)

    async with _tail(web, "one+two") as tail:
        first_answered_ms = _store_now_ms(stack)
        answered = await web.answer(first, "one")
        assert answered.status_code == 200, answered.text
        (resumed,) = await _wait_records(stack, f"sibling_asks_resume:{marker}", 1)
        assert resumed["buffered"] is True

        async def shown_while_a_question_stands() -> list[dict] | None:
            return tail.active_since(first_answered_ms) or None

        await wait_absent_async(
            shown_while_a_question_stands,
            window=_QUIET_SECONDS,
            message="a working frame showed while a question to the user stood",
        )

        answered = await web.answer(second, "two")
        assert answered.status_code == 200, answered.text
        resumed_both = await _wait_records(stack, f"sibling_asks_resume:{marker}", 2)
        assert [record["buffered"] for record in resumed_both] == [True, False]
        run_resumed_ms = _store_now_ms(stack)
        await tail.until_reply()

    assert tail.active_since(run_resumed_ms), f"no working frame while the run owed its reply: {tail.frames!r}"


@pytest.mark.needs("kind:channels:web")
async def test_web_shows_working_frames_while_one_message_answering_both_caller_questions_is_worked_on(
    working_signal_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    stack, _root_token = working_signal_stack
    marker = uniq("wsq-webone")
    web = await _open_web_visitor(stack, uniq, marker, to_caller=True)

    sent = await web.send(uniq("wsq-webone-msg"))
    assert sent.status_code == 200, sent.text
    await _parked_questions(stack, marker)

    answer = uniq("wsq-webone-ans")
    async with _tail(web, f"{answer}+{answer}") as tail:
        answered = await web.send(answer)
        assert answered.status_code == 200, answered.text
        resumed = await _wait_records(stack, f"sibling_asks_resume:{marker}", 2)
        assert [record["buffered"] for record in resumed] == [True, False]
        run_resumed_ms = _store_now_ms(stack)
        await tail.until_reply()

    assert tail.active_since(run_resumed_ms), f"no working frame while the run worked on the answers: {tail.frames!r}"


def _active_calls_to(stack: TaiStack, recipient: str, *, besides: str) -> list[dict]:
    """The active indicator calls the counting stub recorded for ``recipient``, leaving out the
    ones recorded under the inbound ``besides`` (the turn's own loop, which asserts from the moment
    its target runs)."""
    calls: list[dict] = []
    for key in stack.record_keys():
        if key.startswith("working_signal:") and key != f"working_signal:{besides}":
            records = (json.loads(raw) for raw in stack.records(key))
            calls.extend(record for record in records if record["recipient"] == recipient and record["active"])
    return calls


async def test_stub_indicator_is_not_called_while_a_sibling_question_stands_then_is_called_until_the_reply(
    working_signal_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    stack, root_token = working_signal_stack
    marker = uniq("wsq-stub")
    user = uniq("wsquser").replace("_", "-")
    inbound_id = uniq("wsqpmid").replace("_", "-")
    # The ask names no sender identity, so the stub sends it from its default identity; the route
    # carries the same identity, so the questions go to the very user the run owes its reply.
    await _create_route(
        stack,
        uniq,
        channel="stub_working",
        our_identity=STUB_DEFAULT_IDENTITY,
        start_expr=_park_start_expr(marker, "stub_working"),
    )
    async with stack.mcp(port=stack.port_a, auth=root_token) as mcp:
        accepted = await mcp.call_tool(
            "e2e_channel_inbound",
            {
                "channel": "stub_working",
                "our_identity": STUB_DEFAULT_IDENTITY,
                "client_address": user,
                "provider_message_id": inbound_id,
                "text": uniq("wsq-stub-msg"),
            },
        )
    assert not accepted.is_error, accepted
    first, second = await _parked_questions(stack, marker)

    answered = await stack.api(stack.port_a).post(f"/api/interactions/{first}/answer", json={"answer": "one"})
    assert answered["status"] == "answered"
    (resumed,) = await _wait_records(stack, f"sibling_asks_resume:{marker}", 1)
    assert resumed["buffered"] is True

    async def called_while_a_question_stands() -> list[dict] | None:
        return _active_calls_to(stack, user, besides=inbound_id) or None

    await wait_absent_async(
        called_while_a_question_stands,
        window=_QUIET_SECONDS,
        message="the indicator was called while a question to the user stood",
    )

    answered = await stack.api(stack.port_a).post(f"/api/interactions/{second}/answer", json={"answer": "two"})
    assert answered["status"] == "answered"
    resumed_both = await _wait_records(stack, f"sibling_asks_resume:{marker}", 2)
    assert [record["buffered"] for record in resumed_both] == [True, False]

    async def called_while_the_reply_is_owed() -> list[dict] | None:
        return _active_calls_to(stack, user, besides=inbound_id) or None

    await wait_for_async(
        called_while_the_reply_is_owed,
        deadline=_HOLD_SECONDS,
        message="the indicator was never called while the run owed its reply",
    )
