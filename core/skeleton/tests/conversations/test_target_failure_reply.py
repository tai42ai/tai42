"""The participant-facing line a failed turn carries, on every conversation door.

A target that fails with ``RunTerminalFailed(outcome, reply=...)`` (or returns ``RunFailed(reply=...)``
from a resume) has its ``reply`` shown to the participant whoever sent the turn. With no reply, a
turn the participant sent (a message) gets the route's ``reply_texts.error``, else the built-in
line; a turn the participant did not send (an event) gets no line at all. Either way the internal
detail of the opaque ``outcome`` stays in the record's ``error`` field under ``answer_status`` ``error``.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

import pytest
from pydantic import ValidationError
from tai42_contract.conversations import ConversationEvent, ConversationEventSubmission, ReplyTexts
from tai42_contract.interactions import (
    PARK_COMPLETION_FAILED,
    PARK_COMPLETION_SUCCEEDED,
    PARK_COMPLETION_WITHDRAWN,
    RunTerminalFailed,
)
from tai42_contract.template import TemplatedText
from tests.agent._memoryless import MemorylessTestAgent

from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.turn import accessors as accessors_module
from tai42_skeleton.conversations.turn import outcome as outcome_module
from tai42_skeleton.conversations.turn import tool_turn as tool_turn_module
from tai42_skeleton.interactions.terminal_failure import failed_outcome_detail
from tai42_skeleton.operations.errors import PermissionDeniedError
from tai42_skeleton.runs.chokepoint import delivery_fire

from .conftest import (
    FakeChannel,
    FakeManager,
    _channel_route,
    _connected,
    _EchoInput,
    _settle,
    _store,
    _tool_api_route,
    _tool_channel_route,
    _wire,
    _wire_tool,
)

_REPLY = "Sorry, that option is no longer available. Pick another?"
_ROUTE_TEXT = "Lo sentimos, algo salió mal. Inténtalo de nuevo."
_OUTCOME = {"status": "error", "error": "upstream 503: internal stack detail"}

_INVALID_REPLIES = [
    pytest.param("", id="empty"),
    pytest.param("   ", id="blank"),
    pytest.param("x" * 2001, id="too-long"),
    pytest.param(5, id="non-string"),
]


async def _accept_turn() -> str:
    message_id = await turn_module.accept(
        "twilio",
        "+15550001111",
        "+15550002222",
        "+15550002222",
        "hi",
        "PID1",
        medium_message_id=None,
        reply_to_medium_message_id=None,
    )
    await _settle()
    return message_id


def _raising(failure: RunTerminalFailed):
    def _fn(_kw):
        raise failure

    return _fn


# --- the one chokepoint ------------------------------------------------------------------


@pytest.mark.parametrize("inbound_kind", ["message", "event"])
def test_the_failure_line_is_the_targets_reply_whoever_sent_the_turn(inbound_kind):
    failed = outcome_module._FailedOutcome(error="detail", reply=_REPLY)
    route = _tool_channel_route(reply_texts=ReplyTexts(error=_ROUTE_TEXT))
    assert outcome_module.failure_line(route, failed, inbound_kind=inbound_kind) == _REPLY
    assert outcome_module.failure_line(None, failed, inbound_kind=inbound_kind) == _REPLY


def test_a_message_turn_without_a_reply_gets_the_route_text_else_the_built_in_line():
    failed = outcome_module._FailedOutcome(error="detail", reply=None)
    route = _tool_channel_route(reply_texts=ReplyTexts(error=_ROUTE_TEXT))
    assert outcome_module.failure_line(route, failed, inbound_kind="message") == _ROUTE_TEXT
    assert outcome_module.failure_line(_tool_channel_route(), failed, inbound_kind="message") == (
        outcome_module._ERROR_ANSWER_TEXT
    )
    assert outcome_module.failure_line(None, failed, inbound_kind="message") == outcome_module._ERROR_ANSWER_TEXT


def test_an_event_turn_without_a_reply_gets_no_line():
    failed = outcome_module._FailedOutcome(error="detail", reply=None)
    route = _tool_channel_route(reply_texts=ReplyTexts(error=_ROUTE_TEXT))
    assert outcome_module.failure_line(route, failed, inbound_kind="event") is None
    assert outcome_module.failure_line(None, failed, inbound_kind="event") is None


def test_a_terminal_failure_carries_its_reply_and_the_detail():
    failed = outcome_module._terminal_failure(RunTerminalFailed(_OUTCOME, reply=_REPLY))
    assert failed == outcome_module._FailedOutcome(error=failed_outcome_detail(_OUTCOME), reply=_REPLY)


# --- a live turn (tool route, agent route) -----------------------------------------------


async def test_a_tool_turn_failing_with_a_reply_answers_with_the_reply(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route(reply_texts=ReplyTexts(error=_ROUTE_TEXT))), channel)
    _wire_tool(monkeypatch, _raising(RunTerminalFailed(_OUTCOME, reply=_REPLY)))

    message_id = await _accept_turn()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    # The target's reply wins over the route's reply_texts.error...
    assert record.answer == _REPLY
    assert [n.message for n in channel.sends] == [_REPLY]
    # ...and the internal detail stays in the record's error field, never in the answer.
    assert record.error == failed_outcome_detail(_OUTCOME)
    assert "upstream 503" not in record.answer


async def test_a_tool_turn_failing_without_a_reply_answers_with_the_route_text(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route(reply_texts=ReplyTexts(error=_ROUTE_TEXT))), channel)
    _wire_tool(monkeypatch, _raising(RunTerminalFailed(_OUTCOME)))

    message_id = await _accept_turn()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == _ROUTE_TEXT
    assert record.error == failed_outcome_detail(_OUTCOME)


class _FailingAgent(MemorylessTestAgent):
    tool_name = "echo"
    ToolInput = _EchoInput

    def __init__(self, failure: RunTerminalFailed) -> None:
        self.failure = failure

    async def run(self, *, user_message: TemplatedText | None = None, thread_id: str | None = None, **kwargs):
        raise self.failure


@pytest.mark.parametrize(
    ("reply", "expected"),
    [pytest.param(_REPLY, _REPLY, id="reply"), pytest.param(None, _ROUTE_TEXT, id="no-reply")],
)
async def test_an_agent_turn_failing_answers_with_its_reply_else_the_route_text(env, monkeypatch, reply, expected):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route(reply_texts=ReplyTexts(error=_ROUTE_TEXT))), channel)
    agent = _FailingAgent(RunTerminalFailed(_OUTCOME, reply=reply))
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})

    message_id = await _accept_turn()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == expected
    assert [n.message for n in channel.sends] == [expected]
    assert record.error == failed_outcome_detail(_OUTCOME)


# --- an event turn (a provider or system notice the participant did not send) ----------


def _event_submission(*, address: str, event_id: str = "evt-1", wait_seconds: int = 0) -> ConversationEventSubmission:
    return ConversationEventSubmission(
        address=address,
        event=ConversationEvent(event_id=event_id, kind="provider.update", payload={}),
        wait_seconds=wait_seconds,
    )


async def _open_channel_thread(env, monkeypatch, channel: FakeChannel) -> None:
    """Open the ``tool-line`` thread with one ordinary message turn; the caller re-wires its tool."""
    env.seed_route("tool-line")
    _wire_tool(monkeypatch, lambda _kw: "seed")
    await _accept_turn()
    channel.sends.clear()


async def _open_api_thread(env, monkeypatch) -> None:
    """Open the ``tool-api`` thread ``u-7`` with one ordinary api message turn."""
    env.seed_route("tool-api")
    monkeypatch.setattr(delivery_module, "_post_callback", _posting([]))
    _wire_tool(monkeypatch, lambda _kw: "seed")
    await turn_module.submit_api_message("tool-api", "u-7", "hi", "alice", wait_seconds=0, client_connected=_connected)
    await _settle()


def _posting(posted: list):
    async def _post(url, body, signature, timeout_seconds):
        posted.append(json.loads(body))
        return 200

    return _post


async def test_an_event_turn_failing_without_a_reply_delivers_nothing(env, monkeypatch, caplog):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route(reply_texts=ReplyTexts(error=_ROUTE_TEXT))), channel)
    await _open_channel_thread(env, monkeypatch, channel)
    _wire_tool(monkeypatch, _raising(RunTerminalFailed(_OUTCOME)))

    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.conversations.turn"):
        result = await turn_module.submit_event(
            "tool-line", _event_submission(address="+15550002222"), "svc-int", client_connected=_connected
        )
        await _settle()

    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.SILENT
    assert record.answer_status == "error"
    assert record.answer is None
    assert record.answer_parts is None
    assert record.error == failed_outcome_detail(_OUTCOME)
    assert channel.sends == []
    assert any(r.levelno == logging.ERROR and result.message_id in r.getMessage() for r in caplog.records)


async def test_an_event_turn_failing_with_a_reply_answers_with_the_reply(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route(reply_texts=ReplyTexts(error=_ROUTE_TEXT))), channel)
    await _open_channel_thread(env, monkeypatch, channel)
    _wire_tool(monkeypatch, _raising(RunTerminalFailed(_OUTCOME, reply=_REPLY)))

    result = await turn_module.submit_event(
        "tool-line", _event_submission(address="+15550002222"), "svc-int", client_connected=_connected
    )
    await _settle()

    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == _REPLY
    assert record.error == failed_outcome_detail(_OUTCOME)
    assert [n.message for n in channel.sends] == [_REPLY]


async def test_a_platform_error_on_an_event_turn_delivers_nothing(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".count", reply_texts=ReplyTexts(error=_ROUTE_TEXT))
    _wire(monkeypatch, FakeManager(route), channel)
    await _open_channel_thread(env, monkeypatch, channel)
    _wire_tool(monkeypatch, lambda _kw: {"count": 42})

    result = await turn_module.submit_event(
        "tool-line", _event_submission(address="+15550002222"), "svc-int", client_connected=_connected
    )
    await _settle()

    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.SILENT
    assert record.answer_status == "error"
    assert record.answer is None
    assert record.error
    assert channel.sends == []


async def test_a_form_turn_failing_before_the_target_answers_with_the_route_text(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route(reply_texts=ReplyTexts(error=_ROUTE_TEXT))), channel)
    tools = _wire_tool(monkeypatch, lambda _kw: "unreachable")
    monkeypatch.setattr(tool_turn_module, "evaluate_door_contract", _door_contract_error)

    message_id = await turn_module.accept(
        "twilio",
        "+15550001111",
        "+15550002222",
        "+15550002222",
        "submitted",
        "PID-FORM",
        medium_message_id=None,
        reply_to_medium_message_id=None,
        form={"choice": "b"},
    )
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.inbound_form == {"choice": "b"}
    assert tools.calls == []
    assert record.answer_status == "error"
    assert record.answer == _ROUTE_TEXT
    assert [n.message for n in channel.sends] == [_ROUTE_TEXT]


async def test_an_api_event_turn_failing_without_a_reply_waits_out_an_error_with_no_answer(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_tool_api_route(reply_texts=ReplyTexts(error=_ROUTE_TEXT))))
    await _open_api_thread(env, monkeypatch)
    posted: list = []
    monkeypatch.setattr(delivery_module, "_post_callback", _posting(posted))
    _wire_tool(monkeypatch, _raising(RunTerminalFailed(_OUTCOME)))

    result = await turn_module.submit_event(
        "tool-api", _event_submission(address="u-7", wait_seconds=5), "alice", client_connected=_connected
    )
    await _settle()

    assert result.answer is not None
    assert result.answer.status == "error"
    assert result.answer.answer is None
    assert posted == []


async def test_an_api_event_turn_failing_without_a_reply_posts_an_error_with_no_answer(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_tool_api_route(reply_texts=ReplyTexts(error=_ROUTE_TEXT))))
    await _open_api_thread(env, monkeypatch)
    posted: list = []
    monkeypatch.setattr(delivery_module, "_post_callback", _posting(posted))
    _wire_tool(monkeypatch, _raising(RunTerminalFailed(_OUTCOME)))

    result = await turn_module.submit_event(
        "tool-api", _event_submission(address="u-7"), "alice", client_connected=_connected
    )
    await _settle()

    assert posted == [{"message_id": result.message_id, "thread_id": result.thread_id, "status": "error"}]
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert record.error == failed_outcome_detail(_OUTCOME)


async def test_an_api_event_turn_failing_with_a_reply_posts_the_reply(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_tool_api_route(reply_texts=ReplyTexts(error=_ROUTE_TEXT))))
    await _open_api_thread(env, monkeypatch)
    posted: list = []
    monkeypatch.setattr(delivery_module, "_post_callback", _posting(posted))
    _wire_tool(monkeypatch, _raising(RunTerminalFailed(_OUTCOME, reply=_REPLY)))

    result = await turn_module.submit_event(
        "tool-api", _event_submission(address="u-7"), "alice", client_connected=_connected
    )
    await _settle()

    assert posted == [
        {"message_id": result.message_id, "thread_id": result.thread_id, "status": "error", "answer": _REPLY}
    ]


# --- the completion addresses ------------------------------------------------------------


async def _fire(tool, **kw):
    with delivery_fire(kw["completion_id"]):
        return await tool(**kw)


_COMPLETION_TOOLS = [
    pytest.param(turn_module.deliver_tool_completion, _tool_channel_route, "bridge:tool-line:+15550002222", id="tool"),
    pytest.param(turn_module.deliver_agent_completion, _channel_route, "bridge:line:+15550002222", id="agent"),
]


@pytest.mark.parametrize(("tool", "make_route", "thread_id"), _COMPLETION_TOOLS)
@pytest.mark.parametrize("inbound_kind", ["message", "event"])
async def test_a_failed_completion_carrying_a_reply_delivers_the_reply(
    env, monkeypatch, tool, make_route, thread_id, inbound_kind
):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(make_route(reply_texts=ReplyTexts(error=_ROUTE_TEXT))), channel)

    out = await _fire(
        tool,
        thread_id=thread_id,
        completion_id="c-1",
        result=_OUTCOME,
        status=PARK_COMPLETION_FAILED,
        reply=_REPLY,
        inbound_kind=inbound_kind,
    )
    await _settle()

    assert out == {"message_id": "c-1"}
    record = await _store().get_record("c-1")
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == _REPLY
    assert record.error == failed_outcome_detail(_OUTCOME)
    assert [n.message for n in channel.sends] == [_REPLY]


@pytest.mark.parametrize(("tool", "make_route", "thread_id"), _COMPLETION_TOOLS)
async def test_a_failed_completion_without_a_reply_keeps_the_route_text(env, monkeypatch, tool, make_route, thread_id):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(make_route(reply_texts=ReplyTexts(error=_ROUTE_TEXT))), channel)

    out = await _fire(
        tool,
        thread_id=thread_id,
        completion_id="c-1",
        result=_OUTCOME,
        status=PARK_COMPLETION_FAILED,
        inbound_kind="message",
    )
    await _settle()

    assert out == {"message_id": "c-1"}
    record = await _store().get_record("c-1")
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == _ROUTE_TEXT
    assert record.error == failed_outcome_detail(_OUTCOME)
    assert [n.message for n in channel.sends] == [_ROUTE_TEXT]


@pytest.mark.parametrize(("tool", "make_route", "thread_id"), _COMPLETION_TOOLS)
async def test_a_failed_completion_of_an_event_turn_without_a_reply_records_the_error_and_sends_nothing(
    env, monkeypatch, caplog, tool, make_route, thread_id
):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(make_route(reply_texts=ReplyTexts(error=_ROUTE_TEXT))), channel)

    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.conversations.turn"):
        out = await _fire(
            tool,
            thread_id=thread_id,
            completion_id="c-1",
            result=_OUTCOME,
            status=PARK_COMPLETION_FAILED,
            inbound_kind="event",
        )
        await _settle()

    assert out == {"message_id": "c-1"}
    record = await _store().get_record("c-1")
    assert record is not None
    assert record.delivery_status is DeliveryStatus.SILENT
    assert record.answer_status == "error"
    assert record.answer is None
    assert record.error == failed_outcome_detail(_OUTCOME)
    assert record.inbound_kind == "event"
    assert "error" not in record.caller_view()
    assert channel.sends == []
    assert any(r.levelno == logging.ERROR and "c-1" in r.getMessage() for r in caplog.records)


async def test_a_completion_of_an_event_turn_whose_result_cannot_be_mapped_records_the_error_and_sends_nothing(
    env, monkeypatch
):
    channel = FakeChannel()
    _wire(
        monkeypatch,
        FakeManager(_tool_channel_route(reply_expr=".count", reply_texts=ReplyTexts(error=_ROUTE_TEXT))),
        channel,
    )

    out = await _fire(
        turn_module.deliver_tool_completion,
        thread_id="bridge:tool-line:+15550002222",
        completion_id="c-1",
        result={"count": 42},
        status=PARK_COMPLETION_SUCCEEDED,
        inbound_kind="event",
    )
    await _settle()

    assert out == {"message_id": "c-1"}
    record = await _store().get_record("c-1")
    assert record is not None
    assert record.delivery_status is DeliveryStatus.SILENT
    assert record.answer_status == "error"
    assert record.error
    assert channel.sends == []


async def test_an_api_completion_of_an_event_turn_without_a_reply_posts_an_error_with_no_answer(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_tool_api_route(reply_texts=ReplyTexts(error=_ROUTE_TEXT))))
    env.seed_route("tool-api")
    posted: list = []
    monkeypatch.setattr(delivery_module, "_post_callback", _posting(posted))

    await _fire(
        turn_module.deliver_tool_completion,
        thread_id="bridge:tool-api:u-7",
        completion_id="c-1",
        result=_OUTCOME,
        status=PARK_COMPLETION_FAILED,
        inbound_kind="event",
    )
    await _settle()

    assert posted == [{"message_id": "c-1", "thread_id": "bridge:tool-api:u-7", "status": "error"}]
    record = await _store().get_record("c-1")
    assert record is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert record.error == failed_outcome_detail(_OUTCOME)


@pytest.mark.parametrize(("tool", "make_route", "thread_id"), _COMPLETION_TOOLS)
@pytest.mark.parametrize("reply", _INVALID_REPLIES)
async def test_a_completion_refuses_an_invalid_reply_and_writes_no_record(
    env, monkeypatch, tool, make_route, thread_id, reply
):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(make_route()), channel)

    with pytest.raises(ValidationError):
        await _fire(
            tool,
            thread_id=thread_id,
            completion_id="c-1",
            result=_OUTCOME,
            status=PARK_COMPLETION_FAILED,
            reply=reply,
            inbound_kind="message",
        )
    await _settle()

    assert await _store().get_record("c-1") is None
    assert channel.sends == []


@pytest.mark.parametrize(("tool", "make_route", "thread_id"), _COMPLETION_TOOLS)
@pytest.mark.parametrize("status", [PARK_COMPLETION_SUCCEEDED, PARK_COMPLETION_WITHDRAWN, None, "stopped"])
async def test_a_completion_refuses_a_reply_on_a_non_failed_status(
    env, monkeypatch, tool, make_route, thread_id, status
):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(make_route()), channel)

    with pytest.raises(ValueError, match="only a failed terminal carries a reply"):
        await _fire(
            tool,
            thread_id=thread_id,
            completion_id="c-1",
            result="hello",
            status=status,
            reply=_REPLY,
            inbound_kind="message",
        )
    await _settle()

    assert await _store().get_record("c-1") is None
    assert channel.sends == []


# --- the platform-owned failures keep the fixed text --------------------------------------


@asynccontextmanager
async def _deny_bind(execution_key, *, bound_fingerprint):
    raise PermissionDeniedError("the execution key carries no authority")
    yield  # pragma: no cover - makes this an async generator


async def _door_contract_error(*_args, **_kwargs):
    raise RuntimeError("door contract blew up")


def _tool_raises(_kw):
    raise RuntimeError("tool blew up")


@pytest.mark.parametrize(
    ("reply_expr", "tool_fn", "patch"),
    [
        pytest.param(None, _tool_raises, None, id="generic-exception"),
        pytest.param(None, lambda _kw: {"unexpected": "dict"}, None, id="wrong-typed-result"),
        pytest.param(".count", lambda _kw: {"count": 42}, None, id="reply-expr-error"),
        pytest.param(None, lambda _kw: "unreachable", ("bind_execution_identity", _deny_bind), id="permission-denied"),
        pytest.param(
            None, lambda _kw: "unreachable", ("evaluate_door_contract", _door_contract_error), id="door-contract-error"
        ),
    ],
)
async def test_a_platform_owned_tool_turn_failure_keeps_the_route_text(env, monkeypatch, reply_expr, tool_fn, patch):
    channel = FakeChannel()
    _wire(
        monkeypatch,
        FakeManager(_tool_channel_route(reply_expr=reply_expr, reply_texts=ReplyTexts(error=_ROUTE_TEXT))),
        channel,
    )
    _wire_tool(monkeypatch, tool_fn)
    if patch is not None:
        monkeypatch.setattr(tool_turn_module, *patch)

    message_id = await _accept_turn()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == _ROUTE_TEXT
    assert [n.message for n in channel.sends] == [_ROUTE_TEXT]


class _EmptyAgent(MemorylessTestAgent):
    tool_name = "echo"
    ToolInput = _EchoInput

    async def run(self, **kwargs):
        return ""


@pytest.mark.parametrize(
    "registry",
    [pytest.param(lambda: {"echo": _EmptyAgent()}, id="empty-answer"), pytest.param(dict, id="unregistered-agent")],
)
async def test_a_platform_owned_agent_turn_failure_keeps_the_built_in_line(env, monkeypatch, registry):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", registry)

    message_id = await _accept_turn()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == outcome_module._ERROR_ANSWER_TEXT


# === the route reply chokepoint ===============================================


def test_reply_text_returns_a_set_member_else_the_default():
    route = _tool_channel_route(reply_texts=ReplyTexts(slow_down="Más despacio."))
    assert outcome_module.reply_text(route, "slow_down", "Slow down.") == "Más despacio."
    assert outcome_module.reply_text(route, "error", "Built in.") == "Built in."
    assert outcome_module.reply_text(_tool_channel_route(), "slow_down", "Slow down.") == "Slow down."
    assert outcome_module.reply_text(None, "slow_down", "Slow down.") == "Slow down."


def test_reply_text_refuses_an_unknown_member_loudly():
    with pytest.raises(KeyError, match="slowdown"):
        outcome_module.reply_text(None, "slowdown", "Slow down.")
