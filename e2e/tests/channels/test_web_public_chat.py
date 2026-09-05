"""The web channel's ask/answer half, driven through the plugin's own PUBLIC doors.

``ask_user(channel="web", recipient="<identity>:<visitor id>")`` blocks a tool call on
replica A; the web channel appends the question to that pair's transcript; the visitor's
SSE door on replica B replays it; the visitor POSTs the answer to the public answer door on
B, which forwards it to the interactions callback (also B-served); the blocked run resumes
on A. No vendor and no stub sit in the middle — the doors under test ARE the medium, and
the visitor's ``tai_web_session`` cookie is their whole credential.

That cookie carries a SECRET token only: the conversation address it stands for is
registered server-side and never sent to the client, so the fail-closed negatives here are
a missing cookie, an unregistered token, and another visitor's registered session.

The web message door (the other half of a web conversation) bridges through
``conversations.accept``, which this backendless stack carries no backend for: that round
trip is the bridge suite's ``test_l12_web_public_chat``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable

import pytest

from tai42_e2e.stack import TaiStack
from tai42_e2e.webchat import SESSION_COOKIE, mint_unregistered_token

from ._support import (
    WebChannelCase,
    await_true,
    cancel_and_join,
    count_correlation_keys,
    find_pending,
    is_pending,
    post_callback,
    tool_content_text,
)

pytestmark = pytest.mark.backendless

# A minimal form answer schema: a text field and an integer field, both required — the
# smallest shape that exercises the page widget, the by-id answer door, and the callback
# door's schema validation (a wrong-typed field is the 400 negative).
_FORM_SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string"}, "amount": {"type": "integer"}},
    "required": ["label", "amount"],
}


def _stream_cap(stack: TaiStack) -> int:
    """The per-visitor concurrent-SSE cap this stack CONFIGURES, read back off its own
    ``CHANNEL_WEB_MAX_STREAMS_PER_VISITOR`` env — so re-tuning the profile moves the leg
    with it instead of turning it red against a number restated here.

    The cap is counted per process, and a REPLICAS stack boots each port as its own
    ``--workers 1`` master, so the streams a spec drives against one port are the streams
    that process holds."""
    return int(stack.config.env["CHANNEL_WEB_MAX_STREAMS_PER_VISITOR"])


@pytest.fixture
async def web_case(channel_stack: TaiStack) -> WebChannelCase:
    """A fresh page-minted visitor session per spec, so no spec reads another's
    transcript."""
    return await WebChannelCase.open(channel_stack)


def _ask_over_web(case: WebChannelCase, question: str, **extra: object) -> asyncio.Task:
    """Start a blocking ``ask_user`` over the web channel on replica A, addressed at this
    case's visitor pair — web has no operator default recipient, so the address is always
    the visitor's own registered session."""

    async def ask() -> object:
        async with case.stack.mcp(port=case.stack.port_a) as mcp:
            result = await mcp.call_tool(
                "ask_user",
                {"question": question, "channel": case.name, "recipient": case.default_recipient, **extra},
            )
        return result.data

    return asyncio.create_task(ask())


async def _wait_question(case: WebChannelCase, question: str) -> dict:
    """Wait until exactly one transcript question carrying ``question`` is written."""
    records = await await_true(
        lambda: case.sends_matching(question),
        deadline=8.0,
        message=f"web: no transcript entry carrying {question!r} was written",
    )
    assert len(records) == 1, f"web: expected exactly one transcript entry, saw {len(records)}"
    return records[0]


async def test_ask_over_web_replays_on_the_stream_and_the_answer_door_resolves_it(
    web_case: WebChannelCase, uniq: Callable[[str], str]
) -> None:
    case = web_case
    stack = case.stack
    question = uniq("web_q")
    answer = uniq("web_a")

    baseline_keys = count_correlation_keys(stack, case.correlation_prefix)
    ask_task = _ask_over_web(case, question)
    try:
        record = await _wait_question(case, question)
        # A text question is answered by interaction id through this plugin's own door, so
        # the frame carries the id and NOT the callback ticket.
        case.assert_tier2_shape(record)
        await await_true(
            lambda: count_correlation_keys(stack, case.correlation_prefix) > baseline_keys,
            deadline=5.0,
            message="web: delivery reserved no pending-question record",
        )

        # The visitor's own door replays it: the page sees the question through the SSE
        # backlog, not through the store behind it.
        replayed = await case.web.frames()
        questions = [data for event, data in replayed if event == "chat.question"]
        assert [data["question"] for data in questions] == [question]
        assert questions[0]["interaction_id"] == record["interaction_id"]

        response = await case.web.answer(record["interaction_id"], answer)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "answered"

        resolved = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        await cancel_and_join(ask_task)

    # The blocked run woke on A with the whole web pipe — deliver, replay, answer door,
    # callback forward — in the middle.
    assert resolved == answer
    assert not await is_pending(stack, stack.port_a, question)
    assert not await is_pending(stack, stack.port_b, question)
    # The settled question is recorded for the page too, and the reservation is gone.
    settled = await case.web.frames()
    assert any(event == "chat.answered" and data["answer"] == answer for event, data in settled)
    assert count_correlation_keys(stack, case.correlation_prefix) == baseline_keys


async def test_answer_door_fails_closed_without_the_owning_session(
    web_case: WebChannelCase, uniq: Callable[[str], str], channel_stack: TaiStack
) -> None:
    case = web_case
    stack = case.stack
    question = uniq("web_q")
    answer = uniq("web_a")

    # A SECOND registered visitor — a real session that simply owns another conversation.
    other = await WebChannelCase.open(channel_stack)
    assert other.web.visitor_id != case.web.visitor_id

    ask_task = _ask_over_web(case, question)
    try:
        record = await _wait_question(case, question)
        # Presence proof BEFORE the absence asserts: the question is pending on B.
        await find_pending(stack, stack.port_b, question)
        interaction_id = record["interaction_id"]

        # No cookie at all: the door cannot derive an address, so it refuses with the code
        # the page acts on (it reloads the chat URL to be minted a session).
        no_session = await case.web.answer(interaction_id, answer, cookies={})
        assert no_session.status_code == 401, no_session.text
        assert no_session.json()["code"] == "session_missing"

        # A well-formed token that was never REGISTERED: it passes the cookie's format gate
        # and still resolves to no visitor, so it is the same "no session" refusal — it
        # never becomes a fresh address of its own.
        invented = await case.web.answer(interaction_id, answer, cookies={SESSION_COOKIE: mint_unregistered_token()})
        assert invented.status_code == 401, invented.text
        assert invented.json()["code"] == "session_missing"

        # Another visitor's REGISTERED session: the question exists but is not this
        # conversation's, and is reported as not found — never as "exists, but not yours".
        foreign = await case.web.answer(interaction_id, answer, cookies=other.web.cookies)
        assert foreign.status_code == 404, foreign.text

        # A structurally-invalid answer is refused at the door (422) before the record is
        # claimed: the door forwards one scalar (text/confirm/select) or an object (form),
        # so a list is neither and is rejected on shape alone, whatever the question's format.
        bad_shape = await case.web.answer(interaction_id, [answer])
        assert bad_shape.status_code == 422, bad_shape.text

        assert await is_pending(stack, stack.port_a, question)
        assert not ask_task.done()

        # The owning session then resolves it — the success is the ordering sentinel.
        good = await case.web.answer(interaction_id, answer)
        assert good.status_code == 200, good.text
        resolved = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        await cancel_and_join(ask_task)

    assert resolved == answer
    # The refused answers appended nothing: still exactly one question in the transcript.
    assert len(case.sends_matching(question)) == 1


async def test_external_question_carries_the_ticket_and_a_late_web_answer_is_409(
    web_case: WebChannelCase, uniq: Callable[[str], str]
) -> None:
    case = web_case
    stack = case.stack
    question = uniq("web_ext_q")
    external_answer = uniq("web_ext_a")
    late_answer = uniq("web_late_a")

    # No ``link``: the channel owns delivery for every format, and for ``external`` it is
    # the transcript frame's own ``callback_url`` the widget opens.
    ask_task = _ask_over_web(case, question, answer_format="external")
    try:
        record = await _wait_question(case, question)
        # ``external`` is the ONE format whose widget opens the ticket itself, so it is the
        # one format the frame carries it for — and it resolves on replica B, the worker
        # that never asked.
        callback_url = case.tier1_callback_url(record)
        assert callback_url.startswith(f"http://{stack.host}:{stack.port_b}")

        # The visitor follows the link and answers OUT of band; the blocked run wakes.
        forwarded = await post_callback(callback_url, json.dumps({"answer": external_answer}).encode())
        assert forwarded.status_code == 200, forwarded.text
        resolved = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        await cancel_and_join(ask_task)
    # An external answer resolves as the callback door's own payload, not a bare string.
    assert external_answer in json.dumps(resolved)

    # The plugin's own pending record was never claimed, so the page can still POST an
    # answer for it — the interactions door recorded SOMEONE ELSE's, so the door reports
    # 409 and writes no ``chat.answered`` frame that would show a value never recorded.
    late = await case.web.answer(record["interaction_id"], late_answer)
    assert late.status_code == 409, late.text
    replayed = await case.web.frames()
    assert not any(event == "chat.answered" and data["answer"] == late_answer for event, data in replayed)


async def test_form_over_web_delivers_the_schema_and_answers_with_a_typed_dict(
    web_case: WebChannelCase, uniq: Callable[[str], str]
) -> None:
    case = web_case
    stack = case.stack
    question = uniq("web_form_q")
    good_answer = {"label": uniq("web_form_label"), "amount": 3}

    baseline_keys = count_correlation_keys(stack, case.correlation_prefix)
    ask_task = _ask_over_web(case, question, answer_format="form", schema=_FORM_SCHEMA)
    try:
        record = await _wait_question(case, question)
        # A form question carries its answer schema for the page's form widget and, like
        # every by-id format, NOT the callback ticket (the visitor answers through this
        # plugin's own door, never the callback link).
        assert record["answer_format"] == "form"
        assert record["schema"] == _FORM_SCHEMA
        assert "callback_url" not in record, "a form question leaked its callback ticket"
        await await_true(
            lambda: count_correlation_keys(stack, case.correlation_prefix) > baseline_keys,
            deadline=5.0,
            message="web: form delivery reserved no pending-question record",
        )

        # An object that fails the schema is refused with the door's 400, and the question
        # stays answerable — the record is restored, so the visitor can correct and re-send.
        bad = await case.web.answer(record["interaction_id"], {"label": "x", "amount": "not-an-int"})
        assert bad.status_code == 400, bad.text
        assert await is_pending(stack, stack.port_a, question)
        assert not ask_task.done()

        # A conforming object answers by id through the plugin's own door and returns the
        # validated dict.
        good = await case.web.answer(record["interaction_id"], good_answer)
        assert good.status_code == 200, good.text
        assert good.json()["data"]["status"] == "answered"
        resolved = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        await cancel_and_join(ask_task)

    # The blocked run woke on A with the whole web pipe in the middle, and the typed dict —
    # not a bare string — is what came back.
    assert resolved == good_answer
    assert not await is_pending(stack, stack.port_a, question)
    assert not await is_pending(stack, stack.port_b, question)
    settled = await case.web.frames()
    assert any(event == "chat.answered" and data["answer"] == good_answer for event, data in settled)
    assert count_correlation_keys(stack, case.correlation_prefix) == baseline_keys


async def _notify_over_web(case: WebChannelCase, message: str, **extra: object) -> str:
    """Drive ``notify_user`` over the web channel on replica A, addressed at this case's
    visitor pair, and return the tool's confirmation text. Fire-and-forget: it returns as
    soon as the transcript entry is written, no interaction and no reply."""
    async with case.stack.mcp(port=case.stack.port_a) as mcp:
        result = await mcp.call_tool(
            "notify_user",
            {"message": message, "channel": case.name, "recipient": case.default_recipient, **extra},
        )
    return tool_content_text(result)


async def test_notify_media_card_over_web_replays_on_the_stream(
    web_case: WebChannelCase, uniq: Callable[[str], str]
) -> None:
    case = web_case
    message = uniq("web_card")
    image_url = "https://example.com/a.png"

    confirmation = await _notify_over_web(
        case, message, media=[{"kind": "image", "url": image_url, "caption": "a pattern"}]
    )
    assert "notification sent via 'web'" in confirmation

    def _is_card(event: str, data: dict) -> bool:
        return event == "chat.media" and data.get("text") == message

    frames = await case.web.frames(until=_is_card)
    card = next(data for event, data in frames if _is_card(event, data))
    # The card is an agent turn (``out``) carrying the text and the https image item; a
    # media notification never leaves a separate chat.message entry behind.
    assert card["direction"] == "out"
    assert card["media"] == [{"kind": "image", "url": image_url, "caption": "a pattern"}]
    assert "options" not in card

    # Replay after reconnect: a fresh stream open replays the same card verbatim off the
    # durable transcript backlog — the new message shape survives reconnect/replay.
    replayed = await case.web.frames()
    assert any(
        event == "chat.media" and data["text"] == message and data["media"] == card["media"] for event, data in replayed
    )


async def test_notify_link_only_over_web_replays_on_the_stream(
    web_case: WebChannelCase, uniq: Callable[[str], str]
) -> None:
    case = web_case
    message = uniq("web_link_card")
    link_url = "https://example.com/a"

    confirmation = await _notify_over_web(case, message, media=[{"kind": "link", "url": link_url, "caption": "Item A"}])
    assert "notification sent via 'web'" in confirmation

    def _is_card(event: str, data: dict) -> bool:
        return event == "chat.media" and data.get("text") == message

    frames = await case.web.frames(until=_is_card)
    card = next(data for event, data in frames if _is_card(event, data))
    # A links-only notification lands as a chat.media card carrying the link item on its
    # media array (a safe outbound link element the page renders as an anchor) — never
    # folded into the body text, so the card holds media and the reducer's
    # at-least-one-of media/options contract holds.
    assert card["direction"] == "out"
    assert card["media"] == [{"kind": "link", "url": link_url, "caption": "Item A"}]
    assert "options" not in card

    # Replay after reconnect: a fresh stream open replays the same card verbatim off the
    # durable transcript backlog — the links-only card shape survives reconnect/replay.
    replayed = await case.web.frames()
    assert any(
        event == "chat.media" and data["text"] == message and data["media"] == card["media"] for event, data in replayed
    )


async def test_notify_list_over_web_carries_tappable_options_on_the_card(
    web_case: WebChannelCase, uniq: Callable[[str], str]
) -> None:
    case = web_case
    message = uniq("web_list")
    option = uniq("web_item")

    options = [{"kind": "reply", "text": option}, {"kind": "reply", "text": "Item B"}]
    confirmation = await _notify_over_web(case, message, options=options)
    assert "notification sent via 'web'" in confirmation

    def _is_card(event: str, data: dict) -> bool:
        return event == "chat.media" and data.get("text") == message

    frames = await case.web.frames(until=_is_card)
    card = next(data for event, data in frames if _is_card(event, data))
    # Each option rides the card as its serialized reply frame item (optional keys omitted).
    assert card["options"] == options
    assert "media" not in card

    # The tappable option list is durable: a reconnect replay carries it unchanged (a tap
    # sends the option text through the message door — the cross-worker round trip of that
    # send is the bridge suite's ``test_l25_web_notify_options``).
    replayed = await case.web.frames()
    assert any(
        event == "chat.media" and data["text"] == message and data["options"] == options for event, data in replayed
    )


async def test_stream_door_refuses_over_the_per_visitor_cap(web_case: WebChannelCase) -> None:
    case = web_case
    cap = _stream_cap(case.stack)

    async with contextlib.AsyncExitStack() as held:
        for _ in range(cap):
            open_stream = await held.enter_async_context(case.web.hold_stream())
            assert open_stream.status_code == 200, open_stream.text
        # Each open stream pins a dedicated Redis connection, so the door refuses the next
        # one outright rather than sending SSE headers it cannot serve. The refusal names
        # WHICH ceiling it hit: this session's, not the process-wide total's.
        async with case.web.hold_stream() as refused:
            assert refused.status_code == 503, refused.text
            assert "too many open chat streams for this session" in refused.json()["error"]
